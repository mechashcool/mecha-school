"""Independent guardrail watchdog for the attendance round.

Runs as its own process. Every 2 s it samples the host, the target process,
the experiment PostgreSQL, the generator process and the generator's live
statistics; it writes monitor.csv and a heartbeat. When a guardrail is
breached it writes STOP.json (mode `recovery` or `halt`); if the generator
stops publishing live stats, or ignores a halt, it terminates the generator
process after verifying the process identity.

Modes:
  --baseline N   sample for N seconds and write baseline.json (no guarding)
  (default)      guard until the generator exits, then keep observing for
                 --post-seconds to measure recovery

All thresholds are recorded in watchdog_config.json with the baseline they
were adjusted to. Percentile rules require a minimum sample count.
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import glob
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import guard_rules  # noqa: E402
import outbox_monitor  # noqa: E402

import psutil  # noqa: E402
import psycopg2  # noqa: E402

LOG_PATTERNS = {
    'pool_timeout': re.compile(r'QueuePool limit|TimeoutError: QueuePool|too many clients|remaining connection slots'),
    'db_operational_error': re.compile(r'OperationalError'),
    'worker_timeout': re.compile(r'WORKER TIMEOUT'),
    'worker_boot': re.compile(r'Booting worker'),
    'memory_error': re.compile(r'MemoryError|Out of memory|oom-kill', re.I),
    'traceback': re.compile(r'Traceback \(most recent call last\)'),
}


def now_utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')


class Watchdog:
    def __init__(self, a):
        self.a = a
        self.root = os.path.abspath(a.root)
        self.cfg = common.load_config(self.root)
        self.sec = common.load_secrets(self.root)
        self.out = os.path.abspath(a.out)
        os.makedirs(self.out, exist_ok=True)
        tgt = json.load(open(os.path.join(self.root, 'run', 'target.json')))
        self.target = self._verified_proc(tgt['pid'], tgt['create_time'])
        self.gen = None
        if a.generator_pid:
            p = psutil.Process(a.generator_pid)
            self.gen = p
            self.gen_create = p.create_time()
        self.db = psycopg2.connect(**dict(common.pg_dsn(self.cfg, self.sec), application_name='attlt-watchdog'))
        self.db.autocommit = True
        self.pg_procs = self._pg_processes()
        self.log_path = tgt['log']
        self.log_pos = os.path.getsize(self.log_path) if os.path.exists(self.log_path) else 0
        self.log_counts = collections.Counter()
        self.last_disk = psutil.disk_io_counters()
        self.last_t = time.monotonic()
        self.history = collections.deque(maxlen=120)
        self.fired = []
        self.stop_written = None
        self.max_attendance_id = self._scalar('SELECT COALESCE(max(id),0) FROM student_attendance')
        self.cross_school_rows = 0
        psutil.cpu_percent(None)
        self.target.cpu_percent(None)
        if self.gen:
            self.gen.cpu_percent(None)
        for p in self.pg_procs:
            try:
                p.cpu_percent(None)
            except Exception:
                pass
        self.baseline = None
        bp = os.path.join(self.out, 'baseline.json')
        if os.path.exists(bp):
            self.baseline = json.load(open(bp))
        self.breach = guard_rules.SustainedBreach(time.monotonic)
        self.th = self._thresholds()
        # Durable-outbox monitoring is OPT-IN. Without --outbox-monitor this is
        # None, no outbox query is ever issued, no worker is required, and every
        # existing AI Face round behaves exactly as it always has.
        self.outbox = None
        self._outbox_sample = None
        if getattr(self.a, 'outbox_monitor', False):
            self.outbox = self._build_outbox_sampler()

    # ── helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _verified_proc(pid, create_time):
        p = psutil.Process(pid)
        if abs(p.create_time() - create_time) > 1.0:
            raise SystemExit(f'pid {pid} identity mismatch')
        return p

    def _pg_processes(self):
        data_dir = os.path.normcase(os.path.abspath(os.path.join(self.root, 'pgdata')))
        post = None
        pidfile = os.path.join(data_dir, 'postmaster.pid')
        if os.path.exists(pidfile):
            post = int(open(pidfile).readline().strip())
        procs = []
        if post:
            try:
                pp = psutil.Process(post)
                procs = [pp] + pp.children(recursive=True)
            except psutil.Error:
                pass
        return procs

    def _scalar(self, sql, args=None):
        with self.db.cursor() as c:
            c.execute(sql, args)
            return c.fetchone()[0]

    def _build_outbox_sampler(self):
        """Reuse the watchdog's existing database connection and cadence.

        No second connection, no second loop, no second watchdog: the sampler
        is driven from sample() like every other metric. The school ids come
        from the experiment's own institute fixtures, so the query can only see
        rows this experiment created.
        """
        fx_path = os.path.join(self.root, 'run', 'institute_fixtures.json')
        if not os.path.exists(fx_path):
            raise SystemExit(
                '--outbox-monitor requires run/institute_fixtures.json; this '
                'round has no institute fixtures. Omit the flag for an AI Face '
                'round.')
        with open(fx_path, encoding='utf-8') as fh:
            fx = json.load(fh)
        school_ids = sorted({sch['school_id'] for sch in fx['schools'].values()})
        if not school_ids:
            raise SystemExit('--outbox-monitor: institute fixtures name no schools')

        def fetch(ids):
            # Selects `status` and `school_id` only — never a title, body,
            # data_json, dedup key or device token.
            with self.db.cursor() as c:
                c.execute(outbox_monitor.COUNTS_SQL, (list(ids),))
                return {row[0]: row[1] for row in c.fetchall()}

        print(f'[watchdog] outbox monitoring ENABLED for schools {school_ids}',
              flush=True)
        return outbox_monitor.OutboxSampler(
            fetch, school_ids=school_ids,
            worker_state=lambda: outbox_monitor.read_worker_state(self.root),
            clock=time.monotonic)

    def _sample_outbox(self, s):
        """Add the outbox metrics to one monitoring sample. No-op when off.

        On a failed sample NOTHING numeric is written: a missing key is what
        makes the guard layer fail closed, so substituting a zero here would
        defeat the whole mechanism.
        """
        if self.outbox is None:
            return
        o = self.outbox.sample(window_s=self.th['outbox_no_drain_window_s'])
        self._outbox_sample = o
        s['outbox_collector_ok'] = bool(o.get('collector_ok'))
        if not o.get('collector_ok'):
            s['outbox_collector_error'] = o.get('collector_error')
            s['outbox_collector_failures'] = o.get(
                'consecutive_collector_failures')
            return
        for name in outbox_monitor.STATUSES:
            s['outbox_' + name] = o['outbox_' + name]
        s['outbox_total_jobs'] = o['total_jobs']
        s['outbox_backlog'] = o['outbox_backlog']
        s['outbox_backlog_falling'] = o['outbox_backlog_falling']
        s['outbox_worker_alive'] = o.get('worker_alive')
        s['outbox_worker_pid'] = o.get('worker_pid')
        s['outbox_unplanned_worker_restarts'] = o.get(
            'unplanned_worker_restarts')

    def _evaluate_outbox(self):
        """(halt, recovery) from the outbox guard rules. ([], []) when off.

        The sample handed to the rules carries the watchdog's own database and
        log symptom counters, so an OperationalError, a pool timeout or a
        traceback in the target log stops the round through the outbox rules as
        well as the existing ones.

        `draining` is the worker being alive: with the worker deliberately
        stopped a growing backlog is the expected result, not a stuck queue.
        """
        if self.outbox is None:
            return [], []
        o = dict(self._outbox_sample or {})
        o.setdefault('db_operational_errors', self.log_counts.get('operational_error', 0))
        o.setdefault('db_pool_timeouts', self.log_counts.get('pool_timeout', 0))
        o.setdefault('worker_tracebacks', self.log_counts.get('traceback', 0))
        o.setdefault('isolation_violations', self.cross_school_rows)
        reasons = guard_rules.outbox_breaches(
            o, self.th, draining=bool(o.get('worker_alive')),
            sustained=self.breach)
        return guard_rules.classify_outbox_reasons(reasons)

    def _thresholds(self):
        # No baseline auto-lowering. Fixed floor (default 20%) unless an explicit
        # degraded-host override is passed, which stamps compliance OVERRIDDEN.
        th = guard_rules.build_thresholds(
            min_mem_pct=self.a.min_mem_pct, allow_degraded_host=self.a.allow_degraded_host,
            mem_total_gb=round(psutil.virtual_memory().total / 2**30, 2),
            live_health_url=self.a.live_health_url or None)
        gate = guard_rules.startup_gate(self.baseline or {}, min_mem_pct=self.a.min_mem_pct,
                                        allow_degraded_host=self.a.allow_degraded_host) if self.baseline else None
        json.dump({'thresholds': th, 'baseline': self.baseline, 'startup_gate': gate},
                  open(os.path.join(self.out, 'watchdog_config.json'), 'w'), indent=2)
        return th

    # ── sampling ──────────────────────────────────────────────────────────────
    def _cached(self, procs):
        """cpu_percent() needs the SAME Process object across samples."""
        cache = self.__dict__.setdefault('_pcache', {})
        out = []
        for q in procs:
            key = (q.pid, q.create_time()) if q.is_running() else None
            if key is None:
                continue
            if key not in cache:
                cache[key] = q
                q.cpu_percent(None)
            out.append(cache[key])
        return out

    def _proc_tree(self, p):
        cpu = rss = thr = 0.0
        try:
            if not p.is_running():
                return None
            procs = self._cached([p] + p.children(recursive=True))
        except psutil.Error:
            return None
        for q in procs:
            try:
                cpu += q.cpu_percent(None)
                rss += q.memory_info().rss
                thr += q.num_threads()
            except psutil.Error:
                pass
        return {'cpu_pct': round(cpu, 1), 'rss_mb': round(rss / 2**20, 1), 'threads': int(thr)}

    def _read_log(self):
        if not os.path.exists(self.log_path):
            return 0
        size = os.path.getsize(self.log_path)
        grown = size - self.log_pos
        if grown > 0:
            with open(self.log_path, 'rb') as fh:
                fh.seek(self.log_pos)
                chunk = fh.read(min(grown, 8 * 2**20)).decode('utf-8', 'replace')
            for name, rx in LOG_PATTERNS.items():
                self.log_counts[name] += len(rx.findall(chunk))
        self.log_pos = size
        return max(grown, 0)

    def _live(self):
        snaps = []
        for f in glob.glob(os.path.join(self.out, 'live_p*.json')):
            try:
                snaps.append((os.path.getmtime(f), json.load(open(f, encoding='utf-8'))))
            except Exception:
                pass
        return snaps

    def sample(self):
        t = time.monotonic()
        dt_s = max(1e-3, t - self.last_t)
        self.last_t = t
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        disk = psutil.disk_io_counters()
        du = psutil.disk_usage(self.root)
        io_ms = ((disk.read_time - self.last_disk.read_time) + (disk.write_time - self.last_disk.write_time))
        busy = getattr(disk, 'busy_time', None)
        s = {
            'wall_utc': now_utc(), 'mono': round(t, 3),
            'host_cpu_pct': psutil.cpu_percent(None),
            'host_mem_available_pct': round(vm.available / vm.total * 100, 2),
            'host_mem_available_gb': round(vm.available / 2**30, 3),
            'host_swap_used_gb': round(sw.used / 2**30, 3),
            'disk_free_gb': round(du.free / 2**30, 3),
            'disk_read_mb_s': round((disk.read_bytes - self.last_disk.read_bytes) / 2**20 / dt_s, 3),
            'disk_write_mb_s': round((disk.write_bytes - self.last_disk.write_bytes) / 2**20 / dt_s, 3),
            'disk_io_time_ratio': round(io_ms / (dt_s * 1000), 3),
            'disk_busy_pct': (round((busy - self.last_disk.busy_time) / (dt_s * 10), 1)
                              if busy is not None else None),
        }
        self.last_disk = disk
        tp = self._proc_tree(self.target)
        s['target_alive'] = tp is not None
        if tp:
            s.update({f'target_{k}': v for k, v in tp.items()})
        pg_cpu = pg_rss = 0.0
        for p in self._cached(self._pg_processes()):
            try:
                pg_cpu += p.cpu_percent(None)
                pg_rss += p.memory_info().rss
            except psutil.Error:
                pass
        s['pg_cpu_pct'] = round(pg_cpu, 1)
        s['pg_rss_mb'] = round(pg_rss / 2**20, 1)
        if self.gen:
            gp = self._proc_tree(self.gen) if self.gen.is_running() else None
            s['generator_alive'] = gp is not None
            if gp:
                s.update({f'generator_{k}': v for k, v in gp.items()})
        try:
            with self.db.cursor() as c:
                c.execute("""SELECT count(*),
                                    count(*) FILTER (WHERE state='active'),
                                    count(*) FILTER (WHERE wait_event_type='Lock'),
                                    COALESCE(EXTRACT(EPOCH FROM max(now()-query_start) FILTER (WHERE state='active')), 0)
                             FROM pg_stat_activity WHERE datname=%s AND application_name <> 'attlt-watchdog'""",
                          (self.cfg['db_name'],))
                n, act, lockw, longest = c.fetchone()
                c.execute('SHOW max_connections')
                maxc = int(c.fetchone()[0])
                c.execute('SELECT xact_commit, blks_read, tup_inserted FROM pg_stat_database WHERE datname=%s',
                          (self.cfg['db_name'],))
                xc, br, ti = c.fetchone()
            s.update({'db_connections': n, 'db_active': act, 'db_lock_waits': lockw,
                      'db_longest_active_s': round(float(longest), 3), 'db_max_connections': maxc,
                      'db_xact_commit': xc, 'db_blks_read': br, 'db_tup_inserted': ti})
        except Exception as exc:
            s['db_error'] = type(exc).__name__
        grown = self._read_log()
        s['target_log_growth_mb_min'] = round(grown / 2**20 / dt_s * 60, 3)
        for k2, v in self.log_counts.items():
            s[f'log_{k2}'] = v
        if self.th.get('live_service_health_url'):
            t1 = time.perf_counter()
            try:
                with urllib.request.urlopen(self.th['live_service_health_url'], timeout=5) as r:
                    s['live_health_status'] = r.status
            except Exception as exc:
                s['live_health_status'] = type(exc).__name__
            s['live_health_ms'] = round((time.perf_counter() - t1) * 1000, 1)
        self._sample_outbox(s)
        return s

    # ── rules ─────────────────────────────────────────────────────────────────
    def _sustained(self, key, cond, secs):
        return self.breach(key, cond, secs)

    def evaluate(self, s, live):
        th = self.th
        halt, rec = [], []
        # generator liveness
        if self.gen is not None:
            if not s.get('generator_alive', False):
                return halt, rec
            if live:
                newest = max(m for m, _ in live)
                if time.time() - newest > th['generator_stale_s']:
                    halt.append(f'generator live stats stale for {time.time() - newest:.0f}s')
        if not s['target_alive']:
            halt.append('target process died')
        if s.get('log_memory_error'):
            halt.append('memory error / OOM pattern in target log')
        if s.get('db_error'):
            rec.append(f"db monitoring query failed: {s['db_error']}")
        # correctness / isolation (immediate)
        for _, snap in live:
            if snap.get('violations'):
                halt.append(f"generator reported {snap['violations']} correctness violation(s)")
            if snap.get('unexpected_device_commands'):
                halt.append('unexpected server→device command observed')
        if self.cross_school_rows:
            halt.append(f'{self.cross_school_rows} attendance rows with school_id != student.school_id')
        # host
        if self._sustained('cpu', s['host_cpu_pct'] > th['host_cpu_pct'], th['host_cpu_sustain_s']):
            rec.append(f"host CPU > {th['host_cpu_pct']}% for {th['host_cpu_sustain_s']}s")
        if self._sustained('mem', s['host_mem_available_pct'] < th['mem_available_floor_pct'], th['mem_sustain_s']):
            rec.append(f"MemAvailable < {th['mem_available_floor_pct']}% for {th['mem_sustain_s']}s")
        if s['disk_free_gb'] < th['disk_free_floor_gb']:
            halt.append(f"disk free {s['disk_free_gb']} GB below floor")
        if self.baseline and self.baseline['host'].get('disk_free_gb') and \
                self.baseline['host']['disk_free_gb'] - s['disk_free_gb'] > th['disk_decline_gb']:
            halt.append('disk free declined by more than 2 GB during the round')
        if s['target_log_growth_mb_min'] > th['log_growth_mb_per_min']:
            rec.append(f"target log growing {s['target_log_growth_mb_min']} MB/min")
        if self._sustained('io', s['disk_io_time_ratio'] > th['io_time_ratio'], th['io_sustain_s']):
            rec.append(f"disk I/O time ratio > {th['io_time_ratio']} for {th['io_sustain_s']}s")
        if self.baseline and s['host_swap_used_gb'] - self.baseline['host']['swap_used_gb'] > th['swap_growth_gb']:
            rec.append('swap usage grew by more than 1 GB')
        # database
        if s.get('log_pool_timeout'):
            rec.append('database pool timeout / connection exhaustion in target log')
        if s.get('db_connections') and s['db_connections'] >= th['db_connections_frac_of_max'] * s['db_max_connections']:
            rec.append('database connections >= 90% of max_connections')
        # generator traffic
        agg_ops = collections.defaultdict(lambda: {'n': 0, 'errors': 0, 'p95': []})
        backlog = 0
        send_delay = []
        missed = reads = 0
        gen_cpu = []
        for _, snap in live:
            for op, v in (snap.get('ops_window') or {}).items():
                agg_ops[op]['n'] += v['n']
                agg_ops[op]['errors'] += v['errors']
                if v['p95_ms'] is not None:
                    agg_ops[op]['p95'].append(v['p95_ms'])
            backlog += snap.get('event_backlog') or 0
            backlog += snap.get('pending_acks') or 0
            if snap.get('send_delay_p95_s_window') is not None:
                send_delay.append(snap['send_delay_p95_s_window'])
            missed += (snap.get('counters') or {}).get('parent_read.missed_schedule', 0)
            reads += (snap.get('counters') or {}).get('parent_attendance_read.count', 0)
            if snap.get('generator'):
                gen_cpu.append(snap['generator']['cpu_pct'])
        r = agg_ops.get('parent_attendance_read')
        if self._sustained('read_p95', bool(r and r['n'] >= th['min_samples_for_percentile'] and r['p95']
                                            and max(r['p95']) > th['parent_read_p95_ms']), th['parent_read_sustain_s']):
            rec.append(f"parent read P95 > {th['parent_read_p95_ms']} ms for {th['parent_read_sustain_s']}s")
        w = agg_ops.get('ws_sendlog_ack')
        if self._sustained('ack_p95', bool(w and w['n'] >= th['min_samples_for_percentile'] and w['p95']
                                           and max(w['p95']) > th['ack_p95_ms']), th['ack_sustain_s']):
            rec.append(f"device ack P95 > {th['ack_p95_ms']} ms for {th['ack_sustain_s']}s")
        tot_n = sum(v['n'] for op, v in agg_ops.items() if op != 'parent_cross_school_denied')
        tot_e = sum(v['errors'] for op, v in agg_ops.items() if op != 'parent_cross_school_denied')
        if tot_n >= th['min_samples_for_error_rate'] and tot_e / tot_n > th['error_rate']:
            rec.append(f'error rate {tot_e}/{tot_n} > 1% in the last 20 s window')
        self.history.append((time.monotonic(), backlog))
        old = [b for (m, b) in self.history if time.monotonic() - m >= th['backlog_growth_window_s']]
        if backlog > th['event_backlog'] and old and backlog > old[-1]:
            rec.append(f'event backlog {backlog} and growing')
        if send_delay and max(send_delay) > th['send_delay_p95_s']:
            rec.append(f'scheduled-event send delay P95 {max(send_delay):.1f}s > {th["send_delay_p95_s"]}s')
        if self._sustained('gen_cpu', bool(gen_cpu) and max(gen_cpu) > th['generator_cpu_pct_of_one_core'],
                           th['generator_sustain_s']):
            rec.append('load generator saturated (CPU of its event loop > 90% for 20 s)')
        if reads >= 200 and missed / max(1, reads + missed) > th['missed_read_fraction']:
            rec.append(f'parent read schedule missed {missed}/{reads + missed} (> 5%)')
        if th.get('live_service_health_url') and s.get('live_health_status') != 200:
            if self._sustained('live', True, 10):
                halt.append('LIVE SERVICE health degraded')
        else:
            self.breach.reset('live')
        # Durable outbox. ([], []) unless --outbox-monitor is on.
        o_halt, o_rec = self._evaluate_outbox()
        halt += o_halt
        rec += o_rec
        return halt, rec

    def check_attribution(self):
        """Incremental DB check: new attendance rows whose school differs from the student's school."""
        with self.db.cursor() as c:
            c.execute("""SELECT count(*) FILTER (WHERE sa.school_id <> s.school_id), COALESCE(max(sa.id), %s)
                         FROM student_attendance sa JOIN students s ON s.id = sa.student_id
                         WHERE sa.id > %s""", (self.max_attendance_id, self.max_attendance_id))
            bad, mx = c.fetchone()
        self.cross_school_rows += bad
        self.max_attendance_id = mx

    def write_stop(self, mode, reasons, s):
        if self.stop_written and (self.stop_written['mode'] == 'halt' or mode == 'recovery'):
            return
        rec = {'mode': mode, 'reason': '; '.join(reasons), 'at_utc': now_utc(), 'evidence': s}
        tmp = os.path.join(self.out, '.STOP.tmp')
        json.dump(rec, open(tmp, 'w'), default=str)
        os.replace(tmp, os.path.join(self.out, 'STOP.json'))
        self.stop_written = dict(rec, mono=time.monotonic())
        self.fired.append(rec)
        with open(os.path.join(self.out, 'watchdog_events.jsonl'), 'a') as fh:
            fh.write(json.dumps(rec, default=str) + '\n')
        print(f'[watchdog] STOP {mode}: {rec["reason"]}', flush=True)

    def kill_generator(self, why):
        if not self.gen or not self.gen.is_running():
            return
        if abs(self.gen.create_time() - self.gen_create) > 1.0:
            return
        cmd = ' '.join(self.gen.cmdline()).lower()
        if 'locust' not in cmd and 'thread_driver' not in cmd:
            return
        procs = [self.gen] + self.gen.children(recursive=True)
        for p in procs:
            try:
                p.terminate()
            except psutil.Error:
                pass
        psutil.wait_procs(procs, timeout=10)
        with open(os.path.join(self.out, 'watchdog_events.jsonl'), 'a') as fh:
            fh.write(json.dumps({'action': 'generator_terminated', 'why': why, 'at_utc': now_utc()}) + '\n')
        print(f'[watchdog] generator terminated: {why}', flush=True)

    # ── main loops ────────────────────────────────────────────────────────────
    def run_baseline(self, seconds):
        rows = []
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            time.sleep(2)
            rows.append(self.sample())
        def agg(key, fn):
            vals = [r[key] for r in rows if r.get(key) is not None]
            return fn(vals) if vals else None
        base = {'seconds': seconds, 'samples': len(rows), 'at_utc': now_utc(), 'host': {
            'cpu_pct_avg': agg('host_cpu_pct', lambda v: round(sum(v) / len(v), 1)),
            'cpu_pct_max': agg('host_cpu_pct', max),
            'mem_available_pct_min': agg('host_mem_available_pct', min),
            'mem_available_gb_min': agg('host_mem_available_gb', min),
            'swap_used_gb': agg('host_swap_used_gb', max),
            'disk_free_gb': agg('disk_free_gb', min),
            'disk_io_time_ratio_avg': agg('disk_io_time_ratio', lambda v: round(sum(v) / len(v), 3))},
            'target': {'cpu_pct_avg': agg('target_cpu_pct', lambda v: round(sum(v) / len(v), 1)),
                       'rss_mb': agg('target_rss_mb', max), 'threads': agg('target_threads', max)},
            'db': {'connections_max': agg('db_connections', max), 'active_max': agg('db_active', max)},
            'pg': {'cpu_pct_avg': agg('pg_cpu_pct', lambda v: round(sum(v) / len(v), 1)), 'rss_mb': agg('pg_rss_mb', max)}}
        json.dump(base, open(os.path.join(self.out, 'baseline.json'), 'w'), indent=2)
        self._write_monitor(rows, 'baseline_monitor.csv')
        print(json.dumps(base, indent=2))

    def _write_monitor(self, rows, name):
        keys = sorted({k for r in rows for k in r})
        with open(os.path.join(self.out, name), 'w', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)

    def run_guard(self):
        mon_path = os.path.join(self.out, 'monitor.csv')
        with open(os.path.join(self.out, 'watchdog_heartbeat.json'), 'w') as fh:
            json.dump({'at_utc': now_utc(), 'note': 'guard started'}, fh)
        rows = []
        post_until = None
        last_attr = 0
        while True:
            time.sleep(2)
            s = self.sample()
            gen_done = self.gen is not None and not s.get('generator_alive', False)
            if time.monotonic() - last_attr >= 10:
                try:
                    self.check_attribution()
                except Exception as exc:
                    s['attribution_check_error'] = type(exc).__name__
                last_attr = time.monotonic()
            live = self._live()
            s['phase'] = 'post' if gen_done else 'round'
            if live:
                s['gen_rel'] = max(sn.get('rel', 0) for _, sn in live)
                s['gen_mode'] = ','.join(sorted({sn.get('mode', '') for _, sn in live}))
                s['gen_active_parents'] = sum(sn.get('active_parents', 0) for _, sn in live)
                s['gen_event_backlog'] = sum(sn.get('event_backlog', 0) for _, sn in live)
                s['gen_pending_acks'] = sum(sn.get('pending_acks', 0) for _, sn in live)
            s['cross_school_rows'] = self.cross_school_rows
            rows.append(s)
            if not gen_done:
                halt, rec = self.evaluate(s, live)
                if halt:
                    self.write_stop('halt', halt, s)
                elif rec:
                    self.write_stop('recovery', rec, s)
                if live and s.get('generator_alive'):
                    newest = max(m for m, _ in live)
                    if time.time() - newest > self.th['generator_stale_s'] + 10:
                        self.kill_generator('live stats stale — generator unresponsive')
                if self.stop_written and self.stop_written['mode'] == 'halt' and \
                        time.monotonic() - self.stop_written['mono'] > self.th['halt_ignored_s']:
                    self.kill_generator('halt not honoured within 45 s')
            else:
                if post_until is None:
                    post_until = time.monotonic() + self.a.post_seconds
                    print('[watchdog] generator exited — observing recovery', flush=True)
                if time.monotonic() >= post_until:
                    break
            with open(os.path.join(self.out, '.watchdog_heartbeat.tmp'), 'w') as fh:
                json.dump({'at_utc': now_utc()}, fh)
            os.replace(os.path.join(self.out, '.watchdog_heartbeat.tmp'),
                       os.path.join(self.out, 'watchdog_heartbeat.json'))
            if len(rows) % 5 == 0:
                self._write_monitor(rows, 'monitor.csv')
            if self.gen is None and self.a.max_seconds and len(rows) * 2 >= self.a.max_seconds:
                break
        self._write_monitor(rows, 'monitor.csv')
        json.dump({'fired': self.fired, 'log_counts': dict(self.log_counts),
                   'cross_school_rows': self.cross_school_rows, 'ended_utc': now_utc()},
                  open(os.path.join(self.out, 'watchdog_summary.json'), 'w'), indent=2, default=str)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--baseline', type=int, default=0)
    ap.add_argument('--generator-pid', type=int, default=0)
    ap.add_argument('--post-seconds', type=int, default=120)
    ap.add_argument('--max-seconds', type=int, default=0)
    ap.add_argument('--live-health-url', default='')
    ap.add_argument('--min-mem-pct', type=float, default=20.0,
                    help='required MemAvailable floor (default 20%%, the requested guardrail)')
    ap.add_argument('--allow-degraded-host', action='store_true',
                    help='LOCAL VALIDATION ONLY: proceed on a host below the floor; '
                         'stamps resource-guard compliance OVERRIDDEN (safety not certified)')
    ap.add_argument('--outbox-monitor', action='store_true',
                    help='sample the durable notification outbox every cycle and '
                         'enforce the outbox guard rules. Requires '
                         'run/institute_fixtures.json. OFF by default, so AI '
                         'Face rounds are unaffected.')
    ap.add_argument('--check-gate', action='store_true',
                    help='evaluate the startup safety gate against baseline.json and exit '
                         '(0 = safe to start, 3 = unsafe/rejected)')
    a = ap.parse_args()
    if a.check_gate:
        bp = os.path.join(os.path.abspath(a.out), 'baseline.json')
        baseline = json.load(open(bp)) if os.path.exists(bp) else {}
        gate = guard_rules.startup_gate(baseline, min_mem_pct=a.min_mem_pct,
                                        allow_degraded_host=a.allow_degraded_host)
        json.dump(gate, open(os.path.join(os.path.abspath(a.out), 'startup_gate.json'), 'w'), indent=2)
        print(json.dumps(gate, indent=2))
        raise SystemExit(0 if gate['ok'] else 3)
    w = Watchdog(a)
    if a.baseline:
        w.run_baseline(a.baseline)
    else:
        w.run_guard()


if __name__ == '__main__':
    main()

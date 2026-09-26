"""Independent host + production-health sentinel, OUTSIDE the private namespace.

The in-namespace watchdog cannot see production (that is the point of the
namespace), so this second, independent process runs on the host network and
guards the two things only the host can observe:

  * host memory (fixed floor, default 20 % MemAvailable, sustained 10 s) and
    disk headroom — the same fixed thresholds as guard_rules, no baseline
    lowering and no degraded-host override,
  * the live production health endpoint (read-only GET).

On a breach it writes STOP.json into the round output directory, which the
generator polls; if the halt is not honoured within --halt-grace seconds it
terminates ONLY processes whose identity (pid + create_time + exact cmdline)
matches a generator recorded in the experiment manifest, together with their
children. It never touches production processes, never uses broad kills, and
never signals anything it cannot prove the experiment owns.

    python health_sentinel.py --root <root> --out <root>/results/round1 \
        --live-health-url https://<domain>/ops/health --min-mem-pct 20
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guard_rules  # noqa: E402
import manifest  # noqa: E402

import psutil  # noqa: E402


def now_utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')


class Sentinel:
    def __init__(self, a):
        self.a = a
        self.root = os.path.abspath(a.root)
        self.out = os.path.abspath(a.out)
        os.makedirs(self.out, exist_ok=True)
        self.breach = guard_rules.SustainedBreach(time.monotonic)
        self.rows = []
        self.events = []
        self.stop_written = None
        self.seen_generator = False
        self.generator_gone_at = None
        self.terminated = []
        psutil.cpu_percent(None)

    # ── ownership ─────────────────────────────────────────────────────────────
    def owned_generators(self):
        """Processes the manifest proves this experiment started as generators."""
        try:
            m = manifest.load(self.root)
        except OSError:
            return []
        found = []
        for res in m.get('resources', []):
            if res.get('kind') != 'process' or not str(res.get('role', '')).startswith('generator'):
                continue
            try:
                p = psutil.Process(res['pid'])
                if abs(p.create_time() - res['create_time']) > 1.0:
                    continue
                if p.cmdline() != res['cmdline']:
                    continue
            except (psutil.Error, KeyError):
                continue
            cmd = ' '.join(res['cmdline']).lower()
            if 'locust' not in cmd and 'thread_driver' not in cmd and 'aiface_load.py' not in cmd:
                continue        # never signal anything that is not the load generator
            found.append(p)
        return found

    def terminate_owned(self, why):
        procs = []
        for p in self.owned_generators():
            try:
                procs.extend([p] + p.children(recursive=True))
            except psutil.Error:
                pass
        if not procs:
            return
        for q in procs:
            try:
                q.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(procs, timeout=15)
        for q in alive:
            try:
                q.kill()
            except psutil.Error:
                pass
        rec = {'action': 'generator_terminated_by_sentinel', 'why': why, 'at_utc': now_utc(),
               'pids': [q.pid for q in procs]}
        self.terminated.append(rec)
        self.events.append(rec)
        self._append_event(rec)
        print(f'[sentinel] terminated experiment-owned generator ({why})', flush=True)

    # ── sampling ──────────────────────────────────────────────────────────────
    def sample(self):
        vm = psutil.virtual_memory()
        du = psutil.disk_usage(self.root)
        s = {'wall_utc': now_utc(), 'host_cpu_pct': psutil.cpu_percent(None),
             'host_mem_available_pct': round(vm.available / vm.total * 100, 2),
             'host_mem_available_gb': round(vm.available / 2**30, 3),
             'host_swap_used_gb': round(psutil.swap_memory().used / 2**30, 3),
             'disk_free_gb': round(du.free / 2**30, 3)}
        if self.a.live_health_url:
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(self.a.live_health_url, timeout=5) as r:
                    s['production_health_status'] = r.status
            except Exception as exc:
                s['production_health_status'] = type(exc).__name__
            s['production_health_ms'] = round((time.perf_counter() - t0) * 1000, 1)
        return s

    def evaluate(self, s):
        halt = []
        mem = s['host_mem_available_pct']
        if mem < guard_rules.ABSOLUTE_MEM_EXHAUSTION_PCT:
            halt.append(f'MemAvailable {mem}% below the absolute exhaustion floor '
                        f'{guard_rules.ABSOLUTE_MEM_EXHAUSTION_PCT}%')
        if self.breach('mem', mem < self.a.min_mem_pct, self.a.mem_sustain_s):
            halt.append(f'MemAvailable {mem}% below the {self.a.min_mem_pct}% floor '
                        f'for {self.a.mem_sustain_s}s')
        if s['disk_free_gb'] < self.a.min_disk_gb:
            halt.append(f"disk free {s['disk_free_gb']} GB below the {self.a.min_disk_gb} GB floor")
        if self.a.live_health_url:
            bad = s.get('production_health_status') != 200
            if self.breach('production_health', bad, self.a.health_sustain_s):
                halt.append(f"PRODUCTION health endpoint {s.get('production_health_status')} "
                            f'for {self.a.health_sustain_s}s')
            # Opt-in (0 = off): material production latency degradation.
            if self.a.health_slow_ms and self.breach(
                    'production_slow', (s.get('production_health_ms') or 0) > self.a.health_slow_ms,
                    self.a.health_slow_sustain_s):
                halt.append(f"PRODUCTION health slower than {self.a.health_slow_ms} ms "
                            f'for {self.a.health_slow_sustain_s}s')
        # Opt-in (0 = off): host-wide CPU, measured outside the namespace too.
        if self.a.max_cpu_pct and self.breach('cpu', s['host_cpu_pct'] >= self.a.max_cpu_pct,
                                              self.a.cpu_sustain_s):
            halt.append(f"host CPU >= {self.a.max_cpu_pct}% for {self.a.cpu_sustain_s}s")
        return halt

    # ── output ────────────────────────────────────────────────────────────────
    def _append_event(self, rec):
        with open(os.path.join(self.out, 'sentinel_events.jsonl'), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec, default=str) + '\n')

    def write_stop(self, reasons, s):
        if self.stop_written:
            return
        rec = {'mode': 'halt', 'reason': 'sentinel: ' + '; '.join(reasons), 'at_utc': now_utc(),
               'source': 'health_sentinel (outside the private namespace)', 'evidence': s}
        tmp = os.path.join(self.out, '.STOP.sentinel.tmp')
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(rec, fh, default=str)
        os.replace(tmp, os.path.join(self.out, 'STOP.json'))
        with open(os.path.join(self.out, 'sentinel_stop.json'), 'w', encoding='utf-8') as fh:
            json.dump(rec, fh, indent=2, default=str)
        self.stop_written = dict(rec, mono=time.monotonic())
        self.events.append(rec)
        self._append_event(rec)
        print(f'[sentinel] STOP halt: {rec["reason"]}', flush=True)

    def write_csv(self):
        if not self.rows:
            return
        keys = sorted({k for r in self.rows for k in r})
        with open(os.path.join(self.out, 'sentinel_monitor.csv'), 'w', newline='', encoding='utf-8') as fh:
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            w.writerows(self.rows)

    # ── main loop ─────────────────────────────────────────────────────────────
    def run(self):
        started = time.monotonic()
        print(f'[sentinel] guarding: mem floor {self.a.min_mem_pct}% sustained '
              f'{self.a.mem_sustain_s}s, production health '
              f'{self.a.live_health_url or "(not configured)"}', flush=True)
        while True:
            time.sleep(self.a.interval)
            s = self.sample()
            gens = self.owned_generators()
            if gens:
                self.seen_generator = True
                self.generator_gone_at = None
            elif self.seen_generator and self.generator_gone_at is None:
                self.generator_gone_at = time.monotonic()
                print('[sentinel] generator finished — observing recovery', flush=True)
            s['owned_generators'] = len(gens)
            s['phase'] = 'round' if gens else ('post' if self.seen_generator else 'pre')
            self.rows.append(s)
            if gens:
                halt = self.evaluate(s)
                if halt:
                    self.write_stop(halt, s)
                if self.stop_written and time.monotonic() - self.stop_written['mono'] > self.a.halt_grace:
                    self.terminate_owned('halt not honoured within %ds' % self.a.halt_grace)
            if len(self.rows) % 5 == 0:
                self.write_csv()
            if self.generator_gone_at and time.monotonic() - self.generator_gone_at >= self.a.tail_seconds:
                break
            if time.monotonic() - started >= self.a.max_seconds:
                self.events.append({'note': 'max-seconds reached', 'at_utc': now_utc()})
                break
        self.write_csv()
        summary = {
            'started_utc': self.rows[0]['wall_utc'] if self.rows else None, 'ended_utc': now_utc(),
            'samples': len(self.rows), 'scope': 'host (outside the private network namespace)',
            'thresholds': {'min_mem_pct': self.a.min_mem_pct, 'mem_sustain_s': self.a.mem_sustain_s,
                           'absolute_mem_exhaustion_pct': guard_rules.ABSOLUTE_MEM_EXHAUSTION_PCT,
                           'min_disk_gb': self.a.min_disk_gb, 'health_sustain_s': self.a.health_sustain_s,
                           'max_cpu_pct': self.a.max_cpu_pct, 'cpu_sustain_s': self.a.cpu_sustain_s,
                           'health_slow_ms': self.a.health_slow_ms,
                           'health_slow_sustain_s': self.a.health_slow_sustain_s,
                           'live_health_url_configured': bool(self.a.live_health_url),
                           'lowering_or_override_possible': False},
            'stop_written': self.stop_written, 'terminated': self.terminated, 'events': self.events,
            'host_mem_available_pct_min': min((r['host_mem_available_pct'] for r in self.rows), default=None),
            'host_cpu_pct_max': max((r['host_cpu_pct'] for r in self.rows), default=None),
            'disk_free_gb_min': min((r['disk_free_gb'] for r in self.rows), default=None),
            'production_health_non_200': sorted({str(r.get('production_health_status'))
                                                 for r in self.rows
                                                 if r.get('production_health_status') not in (200, None)}),
            'production_health_ms_max': max((r.get('production_health_ms') or 0 for r in self.rows), default=None),
        }
        with open(os.path.join(self.out, 'sentinel_summary.json'), 'w', encoding='utf-8') as fh:
            json.dump(summary, fh, indent=2, default=str)
        print(json.dumps(summary, indent=2, default=str), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--live-health-url', default='')
    ap.add_argument('--min-mem-pct', type=float, default=20.0)
    ap.add_argument('--mem-sustain-s', type=float, default=10.0)
    ap.add_argument('--health-sustain-s', type=float, default=10.0)
    ap.add_argument('--min-disk-gb', type=float, default=5.0)
    ap.add_argument('--halt-grace', type=float, default=45.0)
    ap.add_argument('--interval', type=float, default=2.0)
    ap.add_argument('--tail-seconds', type=float, default=150.0)
    ap.add_argument('--max-seconds', type=float, default=1800.0)
    ap.add_argument('--max-cpu-pct', type=float, default=0.0,
                    help='halt when host-wide CPU >= this for --cpu-sustain-s (0 = off)')
    ap.add_argument('--cpu-sustain-s', type=float, default=20.0)
    ap.add_argument('--health-slow-ms', type=float, default=0.0,
                    help='halt when production health takes longer than this for '
                         '--health-slow-sustain-s (0 = off)')
    ap.add_argument('--health-slow-sustain-s', type=float, default=20.0)
    a = ap.parse_args()
    if a.min_mem_pct < guard_rules.DEFAULT_MIN_MEM_PCT:
        raise SystemExit(f'refusing to run with a memory floor below the fixed '
                         f'{guard_rules.DEFAULT_MIN_MEM_PCT}% guardrail')
    Sentinel(a).run()


if __name__ == '__main__':
    main()

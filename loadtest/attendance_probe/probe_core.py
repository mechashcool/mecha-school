"""Framework-independent engine for the timed attendance round.

Adapters:
  * locustfile.py     — Locust (gevent) adapter used for the real round (Linux)
  * thread_driver.py  — OS-thread adapter for small-scale LOCAL tooling validation

The engine owns: the clock, the 10-stage schedule, device check-in scheduling
over persistent AI Face connections, parent read pacing, response validation
against deterministic fixtures, per-event accounting, rolling live statistics,
stage-completion checks, and the stop protocol (STOP file from the watchdog,
internal correctness halts, watchdog-heartbeat loss).

It never slows the offered load to hide overload: events that are overdue by
more than MAX_OVERDUE_S are recorded as `unsent_overdue` (missed schedule), not
sent late in a catch-up burst; parent reads that fall a full interval behind are
counted as missed, not replayed.
"""
from __future__ import annotations

import bisect
import collections
import csv
import datetime as dt
import json
import os
import threading
import time
from zoneinfo import ZoneInfo

import common
from aiface_client import AckTimeout, AiFaceDevice, DeviceDisconnected

ACK_TIMEOUT_S = 30.0
MAX_OVERDUE_S = 30.0
DRAIN_S = 30.0
RECOVERY_S = 60.0
WINDOW_S = 20.0
RECOVERY_PARENTS = 10
CROSS_READ_EVERY = 200          # every 200th parent performs one cross-school read


def _pct(values, p):
    if not values:
        return None
    v = sorted(values)
    idx = min(len(v) - 1, max(0, int(round(p / 100.0 * (len(v) - 1)))))
    return v[idx]


class Round:
    def __init__(self, root: str, out_dir: str, *, part_index=0, part_count=1, stage_limit=9,
                 expect_watchdog=True, host_guard=('127.0.0.1', 'host.docker.internal')):
        self.root = root
        self.cfg = common.load_config(root)
        self.fx = json.load(open(os.path.join(root, 'run', 'fixtures.json'), encoding='utf-8'))
        tok = json.load(open(os.path.join(root, 'secrets', 'tokens.json')))
        self.tokens = tok['tokens']
        self.tokens_expire = dt.datetime.fromisoformat(tok['expires_at'])
        if self.tokens_expire - dt.datetime.now(dt.timezone.utc) < dt.timedelta(minutes=30):
            raise SystemExit('pre-issued tokens expire within 30 minutes — run preauth.py again')
        if self.cfg['target_host'] not in host_guard:
            raise SystemExit(f"target host {self.cfg['target_host']} not in allowed test hosts {host_guard}")
        self.out = out_dir
        os.makedirs(out_dir, exist_ok=True)
        self.part_index, self.part_count = part_index, part_count
        self.stages = [s for s in common.STAGES if s[0] <= stage_limit]
        self.last_stage_end = self.stages[-1][2]
        self.max_k = self.stages[-1][3]
        self.expect_watchdog = expect_watchdog
        self.test_date = dt.datetime.now(ZoneInfo(self.cfg['school_timezone'])).date()
        if self.test_date.isoformat() <= self.cfg['history_end_date']:
            raise SystemExit('test date is not after the seeded history — fixtures would not be fresh')
        self.history_dates = set(d.isoformat() for d in common.history_dates(self.cfg))
        self.http_base = f"http://{self.cfg['target_host']}:{self.cfg['http_port']}"
        self.ws_url = f"ws://{self.cfg['target_host']}:{self.cfg['ws_port']}/"

        # partition
        dps = self.cfg['devices_per_school']
        self.devices = [(s, d) for s in range(self.cfg['num_schools']) for d in range(dps)
                        if common.device_index(self.cfg, s, d) % part_count == part_index]
        dev_set = set(self.devices)
        self.part_ks = []
        self.device_sched = collections.defaultdict(list)
        for k in range(self.max_k):
            lay = common.layout(self.cfg, k)
            key = (lay['school_idx'], lay['device_idx'])
            if key in dev_set:
                self.part_ks.append(k)
                self.device_sched[key].append(k)

        # state
        self.lock = threading.RLock()
        self.t0 = None
        self.t0_wall = None
        self.mode = 'pending'
        self.mode_reason = None
        self.mode_at = None
        self.events = {}                     # k → dict
        self.counters = collections.Counter()
        self.samples = collections.deque()   # (rel, op, ms, ok)
        self.active_parents = 0
        self.max_active_parents = 0
        self._dev_slot = 0
        self._par_slot = 0
        self.dev_objs = {}
        self.violations = []
        self._stage_done = set()
        self._files = {}
        self._req_buf = []
        self._evt_buf = []
        self._threads_started = False
        self.registered = set()             # device SNs registered at least once

    # ── clock & control ───────────────────────────────────────────────────────
    def start(self):
        with self.lock:
            if self.t0 is not None:
                return
            self.t0 = time.monotonic()
            self.t0_wall = time.time()
            self.mode = 'normal'
            self._write_json('round_start.json', {
                'experiment_id': self.cfg['experiment_id'], 'start_wall_epoch': self.t0_wall,
                'start_utc': dt.datetime.fromtimestamp(self.t0_wall, dt.timezone.utc).isoformat(),
                'test_date': self.test_date.isoformat(), 'stage_limit': self.stages[-1][0],
                'part_index': self.part_index, 'part_count': self.part_count,
                'devices': len(self.devices), 'students_in_partition': len(self.part_ks)})
            self._control('normal', 'round started')
        if not self._threads_started:
            self._threads_started = True
            for fn in (self._live_loop, self._stop_file_loop, self._stage_loop, self._flush_loop):
                threading.Thread(target=fn, daemon=True, name=fn.__name__).start()

    def devices_ready(self) -> bool:
        return len(self.registered) >= len(self.devices)

    def rel(self) -> float:
        return 0.0 if self.t0 is None else time.monotonic() - self.t0

    def _control(self, mode, reason):
        rec = {'rel': round(self.rel(), 3), 'wall_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
               'mode': mode, 'reason': reason}
        with open(os.path.join(self.out, f'control_p{self.part_index}.jsonl'), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec) + '\n')

    def request_recovery(self, reason):
        with self.lock:
            if self.mode != 'normal':
                return
            self.mode, self.mode_reason, self.mode_at = 'recovery', reason, self.rel()
        self._control('recovery', reason)

    def request_halt(self, reason):
        with self.lock:
            if self.mode == 'halt':
                return
            self.mode, self.mode_reason, self.mode_at = 'halt', reason, self.rel()
        self._control('halt', reason)

    def pending_acks(self) -> int:
        with self.lock:
            return sum(1 for e in self.events.values() if e['status'] == 'sent')

    def finished(self) -> bool:
        if self.t0 is None:
            return False
        t = self.rel()
        if self.mode == 'normal':
            return t >= self.last_stage_end + RECOVERY_S
        if self.mode == 'recovery':
            return t >= self.mode_at + RECOVERY_S
        if self.mode == 'halt':
            return t >= self.mode_at + DRAIN_S or (t >= self.mode_at + 2 and self.pending_acks() == 0)
        return False

    def in_recovery_phase(self) -> bool:
        return self.mode == 'recovery' or (self.mode == 'normal' and self.rel() >= self.last_stage_end)

    def arrivals_allowed(self) -> bool:
        return self.mode == 'normal' and self.rel() < self.last_stage_end

    def _partition_count_below(self, n) -> int:
        return bisect.bisect_left(self.part_ks, n)

    def target_parent_sessions(self) -> int:
        if self.t0 is None:
            return 0
        if self.mode == 'halt':
            return 0
        if self.in_recovery_phase():
            return min(RECOVERY_PARENTS, self._partition_count_below(self.max_k))
        return self._partition_count_below(common.target_students_at(self.rel()))

    def spawn_rate_hint(self) -> float:
        st = common.stage_at(self.rel())
        if not st or st[0] > 9:
            return 50.0
        return max(10.0, 2.0 * common.intended_event_rate(st) / max(1, self.part_count))

    # ── recording ─────────────────────────────────────────────────────────────
    def record(self, op, ms, ok, *, k=None, status=None, nbytes=None, err=None):
        rel = self.rel()
        with self.lock:
            self.samples.append((rel, op, ms, ok))
            self.counters[f'{op}.count'] += 1
            if not ok:
                self.counters[f'{op}.errors'] += 1
            self._req_buf.append((round(time.time(), 3), round(rel, 3), op, k, int(bool(ok)), status,
                                  None if ms is None else round(ms, 2), nbytes, err))

    def _event(self, k, **fields):
        with self.lock:
            e = self.events.setdefault(k, {'k': k})
            e.update(fields)
            self._evt_buf.append(dict(e, rec_rel=round(self.rel(), 3)))

    def violation(self, kind, **detail):
        rec = {'rel': round(self.rel(), 3), 'kind': kind, **detail}
        with self.lock:
            self.violations.append(rec)
        with open(os.path.join(self.out, f'violations_p{self.part_index}.jsonl'), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec, default=str) + '\n')
        self.request_halt(f'correctness violation: {kind}')

    # ── devices ───────────────────────────────────────────────────────────────
    def next_device_slot(self) -> int:
        with self.lock:
            slot = self._dev_slot
            self._dev_slot += 1
            return slot

    def _connect(self, dev: AiFaceDevice, on_request):
        backoff = 1.0
        while not self.finished():
            try:
                t = dev.connect(timeout=15)
                with self.lock:
                    self.registered.add(dev.sn)
                self.record('ws_reg_ack', t * 1000, True)
                on_request('WS', 'reg_ack', t * 1000, None)
                return True
            except Exception as exc:
                self.record('ws_reg_ack', None, False, err=type(exc).__name__)
                on_request('WS', 'reg_ack', 0, exc)
                with self.lock:
                    self.counters['ws_connect_failures'] += 1
                dev.close()
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
        return False

    def run_device(self, slot: int, on_request=lambda *a: None, sleep=time.sleep):
        if slot >= len(self.devices):
            while not self.finished():
                sleep(1)
            return
        s, d = self.devices[slot]
        sn = common.device_sn(self.cfg, s, d)
        dev = AiFaceDevice(self.ws_url, sn)
        with self.lock:
            self.dev_objs[sn] = dev
        if not self._connect(dev, on_request):
            return
        while self.t0 is None:
            sleep(0.2)
        date_s = self.test_date.isoformat()
        sched = self.device_sched[(s, d)]
        for i, k in enumerate(sched):
            if not self.arrivals_allowed():
                for rest in sched[i:]:
                    self._event(rest, status='unsent_stopped', sn=sn,
                                due_rel=round(common.arrival_offset(rest), 3))
                    with self.lock:
                        self.counters['events.unsent_stopped'] += 1
                break
            due = common.arrival_offset(k)
            while self.rel() < due and self.arrivals_allowed():
                sleep(min(0.25, max(0.0, due - self.rel())))
            if not self.arrivals_allowed():
                self._event(k, status='unsent_stopped', sn=sn, due_rel=round(due, 3))
                with self.lock:
                    self.counters['events.unsent_stopped'] += 1
                continue
            delay = self.rel() - due
            if delay > MAX_OVERDUE_S:
                self._event(k, status='unsent_overdue', sn=sn, due_rel=round(due, 3), delay_s=round(delay, 3))
                with self.lock:
                    self.counters['events.unsent_overdue'] += 1
                continue
            self._send_one(dev, k, sn, due, date_s, on_request)
        # keep the persistent connection (answering server polls) until the round ends
        while not self.finished():
            if not dev.connected and not self.finished():
                with self.lock:
                    self.counters['ws.idle_disconnects'] += 1
                self._connect(dev, on_request)
            sleep(1)
        dev.close()

    def _send_one(self, dev, k, sn, due, date_s, on_request):
        lay = common.layout(self.cfg, k)
        t_rec = common.device_time_for(k).strftime('%H:%M:%S')
        rec = AiFaceDevice.record(lay['enrollid'], date_s, t_rec)
        stage = common.stage_for_count(k)[0][0]
        for attempt in (0, 1):
            if not dev.connected:
                with self.lock:
                    self.counters['ws.reconnect_attempts'] += 1
                if not self._connect(dev, on_request):
                    break
            try:
                slot = dev.begin_sendlog([rec])
            except DeviceDisconnected:
                continue
            sent_rel = self.rel()
            self._event(k, status='sent', sn=sn, stage=stage, due_rel=round(due, 3),
                        sent_rel=round(sent_rel, 3), send_delay_s=round(sent_rel - due, 3),
                        logindex=slot['logindex'], retries=attempt, device_time=t_rec)
            with self.lock:
                self.counters['events.sent_frames'] += 1
            try:
                dev.wait_ack(slot, ACK_TIMEOUT_S)
                ms = (slot['ack_t'] - slot['sent_t']) * 1000
                self._event(k, status='acked', ack_rel=round(self.rel(), 3), ack_ms=round(ms, 2))
                self.record('ws_sendlog_ack', ms, True, k=k)
                on_request('WS', 'sendlog_ack', ms, None)
                return
            except AckTimeout as exc:
                self._event(k, status='ack_timeout')
                self.record('ws_sendlog_ack', ACK_TIMEOUT_S * 1000, False, k=k, err='AckTimeout')
                on_request('WS', 'sendlog_ack', ACK_TIMEOUT_S * 1000, exc)
                return                      # do not resend: server may still be processing it
            except DeviceDisconnected as exc:
                self._event(k, status='disconnected_before_ack')
                self.record('ws_sendlog_ack', None, False, k=k, err='Disconnected')
                on_request('WS', 'sendlog_ack', 0, exc)
                with self.lock:
                    self.counters['ws.disconnect_before_ack'] += 1
                continue                    # reconnect and resend once (server dedups if committed)
            except Exception as exc:
                self._event(k, status='bad_ack', err=str(exc)[:120])
                self.record('ws_sendlog_ack', None, False, k=k, err='BadAck')
                on_request('WS', 'sendlog_ack', 0, exc)
                return

    # ── parents ───────────────────────────────────────────────────────────────
    def next_parent_slot(self):
        with self.lock:
            slot = self._par_slot
            self._par_slot += 1
            return slot

    def parent_started(self):
        with self.lock:
            self.active_parents += 1
            self.max_active_parents = max(self.max_active_parents, self.active_parents)

    def parent_stopped(self):
        with self.lock:
            self.active_parents -= 1

    def run_parent(self, slot, http_get, *, stopped=lambda: False, sleep=time.sleep):
        """http_get(path, token, name) -> (status, body_bytes, latency_ms, error_or_None)"""
        if slot >= len(self.part_ks):
            return
        k = self.part_ks[slot]
        fxs = self.fx['students'][k]
        token = self.tokens[fxs['username']]
        path = f"/api/mobile/v1/parent/children/{fxs['student_db_id']}/attendance"
        next_read = common.arrival_offset(k) + common.parent_stagger(k)
        did_cross = (k % CROSS_READ_EVERY) != 0 or k + 1 >= self.max_k
        while not stopped() and not self.finished() and self.mode != 'halt':
            now = self.rel()
            if now < next_read:
                sleep(min(1.0, next_read - now))
                continue
            behind = now - next_read
            if behind >= common.PARENT_READ_INTERVAL:
                missed = int(behind // common.PARENT_READ_INTERVAL)
                with self.lock:
                    self.counters['parent_read.missed_schedule'] += missed
                next_read += missed * common.PARENT_READ_INTERVAL
            status, body, ms, err = http_get(path, token, 'parent_attendance_read')
            ok = status == 200 and err is None
            if ok:
                problem = self._validate(k, body)
                if problem:
                    ok = False
            self.record('parent_attendance_read', ms, ok, k=k, status=status,
                        nbytes=len(body) if body else 0, err=err if err else (None if ok else 'invalid'))
            if not did_cross:
                did_cross = True
                other = self.fx['students'][k + 1]
                st2, body2, ms2, err2 = http_get(
                    f"/api/mobile/v1/parent/children/{other['student_db_id']}/attendance", token,
                    'parent_cross_school_denied')
                denied = st2 == 404 and other['student_code'].encode() not in (body2 or b'')
                self.record('parent_cross_school_denied', ms2, denied, k=k, status=st2)
                if st2 == 200:
                    self.violation('cross_school_read_allowed', k=k, other_k=k + 1)
            next_read += common.PARENT_READ_INTERVAL

    def _validate(self, k, body):
        try:
            js = json.loads(body)
        except Exception:
            return 'json'
        fxs = self.fx['students'][k]
        lay = common.layout(self.cfg, k)
        recs = js.get('records')
        if js.get('ok') is not True or js.get('student_id') != fxs['student_db_id'] or not isinstance(recs, list):
            self.violation('wrong_student_or_shape', k=k, got_student=js.get('student_id'))
            return 'student'
        rng = js.get('range') or {}
        summ = js.get('summary') or {}
        if summ.get('total') != len(recs):
            self.violation('summary_total_mismatch', k=k)
            return 'summary'
        for key in ('present', 'late', 'absent', 'on_leave', 'excused'):
            if summ.get(key) != sum(1 for r in recs if r.get('status') == key):
                self.violation('summary_count_mismatch', k=k, key=key)
                return 'summary'
        prev = None
        date_s = self.test_date.isoformat()
        for r in recs:
            d = r.get('date')
            if not (rng.get('start') <= d <= rng.get('end')) or (prev and d > prev):
                self.violation('record_range_or_order', k=k)
                return 'range'
            prev = d
            if d == date_s:
                ev = self.events.get(k)
                exp_t = common.device_time_for(k)
                if not ev or ev.get('sent_rel') is None:
                    self.violation('today_record_before_checkin_sent', k=k)
                    return 'today'
                if (r.get('check_in') != common.hhmm(exp_t)
                        or r.get('status') != common.expected_checkin_status(self.cfg, exp_t)
                        or r.get('check_out') is not None or r.get('source') != 'aiface'
                        or r.get('notes') != f"AI Face {date_s} {exp_t.strftime('%H:%M:%S')}"):
                    self.violation('today_record_mismatch', k=k)
                    return 'today'
                with self.lock:
                    self.counters['parent_read.saw_today_checkin'] += 1
            elif d in self.history_dates:
                e = common.expected_history(self.cfg, lay['school_idx'], lay['local_idx'], dt.date.fromisoformat(d))
                if (r.get('status') != e['status'] or r.get('check_in') != common.hhmm(e['check_in'])
                        or r.get('check_out') != common.hhmm(e['check_out'])):
                    self.violation('history_record_mismatch', k=k, date=d)
                    return 'history'
            else:
                self.violation('unexpected_record_date', k=k, date=d)
                return 'date'
        return None

    # ── background loops ──────────────────────────────────────────────────────
    def _write_json(self, name, obj):
        # Unique tmp per writer so concurrent writers never fight over one temp
        # name; tolerate a vanished/renamed dir without killing the writer thread.
        os.makedirs(self.out, exist_ok=True)
        tmp = os.path.join(self.out, f'.{name}.{os.getpid()}.{threading.get_ident()}.tmp')
        try:
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(obj, fh, default=str)
            os.replace(tmp, os.path.join(self.out, name))
        except OSError:
            try:
                os.path.exists(tmp) and os.remove(tmp)
            except OSError:
                pass

    def _flush_loop(self):
        req_path = os.path.join(self.out, f'requests_p{self.part_index}.csv')
        evt_path = os.path.join(self.out, f'events_p{self.part_index}.jsonl')
        new = not os.path.exists(req_path)
        with open(req_path, 'a', newline='', encoding='utf-8') as rf, open(evt_path, 'a', encoding='utf-8') as ef:
            w = csv.writer(rf)
            if new:
                w.writerow(['wall_ts', 'rel_s', 'op', 'k', 'ok', 'status', 'latency_ms', 'bytes', 'error'])
            while True:
                time.sleep(1.0)
                with self.lock:
                    rb, self._req_buf = self._req_buf, []
                    eb, self._evt_buf = self._evt_buf, []
                if rb:
                    w.writerows(rb)
                    rf.flush()
                for e in eb:
                    ef.write(json.dumps(e) + '\n')
                if eb:
                    ef.flush()

    def live_snapshot(self):
        now = self.rel()
        with self.lock:
            while self.samples and self.samples[0][0] < now - 60:
                self.samples.popleft()
            win = [s for s in self.samples if s[0] >= now - WINDOW_S]
            ops = {}
            for op in {s[1] for s in win}:
                lat = [s[2] for s in win if s[1] == op and s[2] is not None and s[3]]
                n = sum(1 for s in win if s[1] == op)
                errs = sum(1 for s in win if s[1] == op and not s[3])
                ops[op] = {'n': n, 'errors': errs, 'p50_ms': _pct(lat, 50), 'p95_ms': _pct(lat, 95),
                           'p99_ms': _pct(lat, 99), 'rate_per_s': round(n / WINDOW_S, 2)}
            due = self._partition_count_below(common.target_students_at(now)) if self.arrivals_allowed() else None
            st = collections.Counter(e['status'] for e in self.events.values())
            send_delays = [e.get('send_delay_s', 0) for e in self.events.values()
                           if e.get('sent_rel') is not None and e['sent_rel'] >= now - WINDOW_S]
            sent_total = sum(v for k2, v in st.items() if k2 not in ('unsent_stopped', 'unsent_overdue'))
            backlog = (due - sent_total - st['unsent_overdue']) if due is not None else 0
            dev_stats = collections.Counter()
            for dv in self.dev_objs.values():
                dev_stats.update(dv.stats)
                dev_stats['connected'] += int(dv.connected)
            snap = {
                'wall_epoch': time.time(), 'rel': round(now, 2), 'mode': self.mode, 'mode_reason': self.mode_reason,
                'stage': (common.stage_at(now) or (None,))[0], 'part_index': self.part_index,
                'target_parents': self.target_parent_sessions(), 'active_parents': self.active_parents,
                'max_active_parents': self.max_active_parents,
                'events_status': dict(st), 'events_due_now': due, 'event_backlog': max(0, backlog),
                'pending_acks': st['sent'], 'send_delay_p95_s_window': _pct(send_delays, 95),
                'send_delay_max_s_window': max(send_delays) if send_delays else None,
                'ops_window': ops, 'counters': dict(self.counters), 'violations': len(self.violations),
                'devices': dict(dev_stats), 'unexpected_device_commands': dev_stats['unexpected_commands'],
            }
        try:
            import psutil
            p = psutil.Process()
            snap['generator'] = {'cpu_pct': p.cpu_percent(None), 'rss_mb': round(p.memory_info().rss / 2**20, 1),
                                 'threads': p.num_threads()}
        except Exception:
            pass
        return snap

    def _live_loop(self):
        path = f'live_p{self.part_index}.json'
        hist = os.path.join(self.out, f'live_history_p{self.part_index}.jsonl')
        while True:
            snap = self.live_snapshot()
            if snap['unexpected_device_commands'] and not getattr(self, '_cmd_violation', False):
                self._cmd_violation = True
                self.violation('unexpected_server_to_device_command')
            self._write_json(path, snap)
            with open(hist, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(snap, default=str) + '\n')
            time.sleep(2.0)

    def _stop_file_loop(self):
        stop_path = os.path.join(self.out, 'STOP.json')
        hb_path = os.path.join(self.out, 'watchdog_heartbeat.json')
        while True:
            time.sleep(1.0)
            if os.path.exists(stop_path):
                try:
                    req = json.load(open(stop_path, encoding='utf-8'))
                except Exception:
                    req = {'mode': 'halt', 'reason': 'unreadable STOP file'}
                if req.get('mode') == 'recovery':
                    self.request_recovery('watchdog: ' + str(req.get('reason')))
                else:
                    self.request_halt('watchdog: ' + str(req.get('reason')))
            if self.expect_watchdog and self.t0 is not None and self.mode == 'normal' and self.rel() > 15:
                try:
                    age = time.time() - os.path.getmtime(hb_path)
                except OSError:
                    age = 1e9
                if age > 15:
                    self.request_recovery(f'watchdog heartbeat lost ({age:.0f}s)')

    def _stage_loop(self):
        while self.t0 is None:
            time.sleep(0.2)
        for no, start, end, target in self.stages:
            # Parent sessions are sampled inside the hold period (1 s before the
            # stage ends): after the final stage the scheduled recovery reduces
            # sessions to 10 exactly at `end`, which must not count as a miss.
            while self.rel() < end - 1.0:
                if self.mode != 'normal':
                    return
                time.sleep(0.25)
            with self.lock:
                active = self.active_parents
            while self.rel() < end + 0.5:
                if self.mode != 'normal':
                    return
                time.sleep(0.25)
            prev = 0 if no == 1 else common.STAGES[no - 2][3]
            ks = [k for k in self.part_ks if prev <= k < target]
            with self.lock:
                st = collections.Counter(self.events.get(k, {}).get('status', 'not_sent') for k in ks)
            expected_parents = self._partition_count_below(target)
            unsent = st['not_sent'] + st['unsent_overdue'] + st['unsent_stopped']
            pending = st['sent'] + st['ack_timeout'] + st['disconnected_before_ack'] + st['bad_ack']
            ok = unsent == 0 and pending <= max(2, int(0.01 * len(ks))) and active >= 0.98 * expected_parents
            mark = {'stage': no, 'rel': round(self.rel(), 3), 'intended_students': len(ks),
                    'status': dict(st), 'active_parents': active, 'expected_parents': expected_parents,
                    'completed': ok}
            with open(os.path.join(self.out, f'stage_marks_p{self.part_index}.jsonl'), 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(mark) + '\n')
            if not ok:
                self.request_recovery(f'stage {no} not completed in its allotted time '
                                      f'(unsent={unsent}, unacked={pending}, parents={active}/{expected_parents})')
                return

    def finish(self):
        time.sleep(1.5)       # let the flush loop drain
        with self.lock:
            final = {'mode': self.mode, 'mode_reason': self.mode_reason, 'mode_at_rel': self.mode_at,
                     'end_rel': round(self.rel(), 3), 'end_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                     'counters': dict(self.counters), 'max_active_parents': self.max_active_parents,
                     'violations': self.violations,
                     'events_final': list(self.events.values())}
        self._write_json(f'round_end_p{self.part_index}.json', final)

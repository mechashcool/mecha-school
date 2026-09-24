"""Worker-recycling check with event-level database reconciliation.

Runs INSIDE the Linux runner container on the private internal network. It is a
mechanism test, not a capacity test.

What it establishes, per variant:
  * how often the single Gunicorn worker recycles, and the effective
    max_requests the ARCHIVED gunicorn.conf.py resolves under the target's own
    environment (measured, not assumed);
  * whether AI Face devices get ConnectionRefusedError while a worker is being
    replaced, and how long parent reads stall around each handover;
  * for every unique attendance event: whether it is actually committed, located
    by its own dedup identity ("AI Face <YYYY-MM-DD HH:MM:SS>" inside
    student_attendance.notes) — never by "does this student have any row today".

Ledger rules:
  * exactly one unique event per student, so an event maps to exactly one row;
  * a pending event is RETAINED and retried with the SAME identity after a
    disconnect or ack timeout, so a lost acknowledgement is distinguishable from
    a lost event;
  * a missing acknowledgement is NOT treated as proof of non-delivery — the
    reconciliation reports unacknowledged-but-committed events separately.

NOT verified here: real-device getnewlog(stn=true) offline recovery. The
synthetic client re-sends from its own memory; it does not emulate device-side
log storage. That path stays explicitly unverified.

CONFIGURATION VARIANT: the unpatched/patched variants force frequent recycling
with a small GUNICORN_MAX_REQUESTS. The patched-default variant uses no override
at all, which is the shipped behaviour.

Writes /out/recycle_check_v2.json plus /out/recycle2/<variant>/.
"""
import collections
import datetime as dt
import http.client
import json
import os
import runpy
import secrets
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, '/tooling')
os.environ['ATTLT_BIN_DIR'] = '/usr/local/bin'
import aiface_client  # noqa: E402
import common  # noqa: E402
import target as target_mod  # noqa: E402
import psycopg2  # noqa: E402
import pytz  # noqa: E402

ROOT = '/out/exp'
OUTBASE = '/out/recycle2'
PGHOST = os.environ['ATTLT_PG_HOST']
PGUSER = os.environ['ATTLT_PG_USER']
PGPW = os.environ['ATTLT_PG_PASSWORD']

MAX_REQUESTS = int(os.environ.get('RECYCLE_MAX_REQUESTS', '300'))
DURATION = float(os.environ.get('RECYCLE_DURATION', '100'))
READ_THREADS = int(os.environ.get('RECYCLE_READ_THREADS', '4'))
READ_RATE = float(os.environ.get('RECYCLE_READ_RATE', '4'))      # per thread, per second
DEVICES = int(os.environ.get('RECYCLE_DEVICES', '4'))
EVENT_EVERY = float(os.environ.get('RECYCLE_EVENT_EVERY', '5'))
ACK_TIMEOUT = float(os.environ.get('RECYCLE_ACK_TIMEOUT', '15'))
RECONNECT_BACKOFF = float(os.environ.get('RECYCLE_RECONNECT_BACKOFF', '0.25'))
MAX_ATTEMPTS = int(os.environ.get('RECYCLE_MAX_ATTEMPTS', '6'))
NUM_SCHOOLS = 2
STUDENTS_PER_SCHOOL = 60
TZ = pytz.timezone('Asia/Baghdad')


def sh(cmd, **kw):
    print('  $', ' '.join(cmd), flush=True)
    return subprocess.run(cmd, **kw)


def wait_db():
    for _ in range(40):
        try:
            psycopg2.connect(host=PGHOST, user=PGUSER, password=PGPW,
                             dbname='postgres', connect_timeout=3).close()
            return
        except Exception:
            time.sleep(1)
    raise SystemExit('db not reachable')


def setup_root(variant):
    if os.path.exists(ROOT):
        shutil.rmtree(ROOT)
    for d in ('secrets', 'run', 'logs', 'results', 'guard'):
        os.makedirs(os.path.join(ROOT, d), exist_ok=True)
    shutil.copytree('/app_src_ro', os.path.join(ROOT, 'app_src'), symlinks=False)
    overlaid = []
    if variant.startswith('patched'):
        for src, dst in (('/patch/ai_face_ws.py', 'app/services/ai_face_ws.py'),
                         ('/patch/gunicorn.conf.py', 'gunicorn.conf.py')):
            shutil.copyfile(src, os.path.join(ROOT, 'app_src', dst))
            overlaid.append(dst)
    os.makedirs(os.path.join(ROOT, 'guard', 'firebase_admin'), exist_ok=True)
    with open(os.path.join(ROOT, 'guard', 'firebase_admin', '__init__.py'), 'w') as fh:
        fh.write('raise ImportError("firebase_admin blocked in attlt recycle check")\n')
    exp_id = f'attlt-20260917-7b7cb5-recycle2-{variant}'
    gunicorn_env = {'WEB_CONCURRENCY': '1', 'GUNICORN_THREADS': '4', 'GUNICORN_TIMEOUT': '120',
                    'GUNICORN_GRACEFUL_TIMEOUT': '30',
                    'SQLALCHEMY_POOL_SIZE': '5', 'SQLALCHEMY_MAX_OVERFLOW': '10',
                    'SQLALCHEMY_POOL_TIMEOUT': '30'}
    if variant != 'patched-default':
        # configuration variant: force recycling so several handovers happen quickly
        gunicorn_env['GUNICORN_MAX_REQUESTS'] = str(MAX_REQUESTS)
        gunicorn_env['GUNICORN_MAX_REQUESTS_JITTER'] = '0'
    cfg = {
        'experiment_id': exp_id, 'target_host': '127.0.0.1',
        'http_port': 18180, 'ws_port': 18188, 'pg_mode': 'existing', 'pg_host': PGHOST, 'pg_port': 5432,
        'db_name': 'core_school_attendance_load_test', 'app_revision': 'container',
        'app_revision_full': os.environ.get('RECYCLE_REVISION', 'unknown'),
        'num_schools': NUM_SCHOOLS, 'students_per_school': STUDENTS_PER_SCHOOL, 'devices_per_school': 2,
        'history_calendar_days': 5, 'history_end_date': None, 'school_timezone': 'Asia/Baghdad',
        'att_start_time': '07:00:00', 'att_late_threshold': '07:45:00', 'att_absence_threshold': '09:00:00',
        'att_departure_time': '13:00:00', 'target_server': 'gunicorn', 'gunicorn_env': gunicorn_env,
    }
    json.dump(cfg, open(os.path.join(ROOT, 'experiment.json'), 'w'), indent=2)
    sec = {'pg_user': PGUSER, 'pg_password': PGPW, 'app_secret_key': secrets.token_urlsafe(48),
           'jwt_secret_key': secrets.token_urlsafe(48), 'ops_metrics_token': secrets.token_urlsafe(16),
           'parent_password': 'Lt-' + secrets.token_urlsafe(12)}
    json.dump(sec, open(os.path.join(ROOT, 'secrets', 'secrets.json'), 'w'), indent=2)
    json.dump({'experiment_id': exp_id, 'tool_dir': '/tooling', 'root': ROOT, 'resources': []},
              open(os.path.join(ROOT, 'manifest.json'), 'w'), indent=2)
    return cfg, sec, overlaid


def reset_database(cfg):
    """A FRESH database per variant — no rows may survive from the previous one."""
    c = psycopg2.connect(host=PGHOST, user=PGUSER, password=PGPW, dbname='postgres')
    c.autocommit = True
    k = c.cursor()
    for _ in range(10):
        k.execute("""SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                     WHERE datname = 'core_school_attendance_load_test'
                       AND pid <> pg_backend_pid()""")
        try:
            k.execute('DROP DATABASE IF EXISTS core_school_attendance_load_test')
            break
        except psycopg2.errors.ObjectInUse:
            time.sleep(1.0)
    else:
        raise SystemExit('could not drop the check database')
    k.execute('CREATE DATABASE core_school_attendance_load_test')
    c.close()
    # ownership marker — seed.py refuses to touch a database without it
    c = psycopg2.connect(host=PGHOST, user=PGUSER, password=PGPW, dbname=cfg['db_name'])
    c.autocommit = True
    k = c.cursor()
    k.execute('CREATE TABLE IF NOT EXISTS attlt_owner (experiment_id text PRIMARY KEY, '
              'created_at timestamptz DEFAULT now())')
    k.execute('INSERT INTO attlt_owner (experiment_id) VALUES (%s) ON CONFLICT DO NOTHING',
              (cfg['experiment_id'],))
    c.close()


def effective_gunicorn_settings(cfg, sec):
    """Resolve what the ARCHIVED gunicorn.conf.py yields under the target's own
    environment — the same file and the same variables Gunicorn itself sees."""
    env = target_mod.build_env(cfg, sec)
    keep = dict(os.environ)
    cwd = os.getcwd()
    try:
        os.environ.clear()
        os.environ.update(env)
        os.chdir(os.path.join(ROOT, 'app_src'))
        ns = runpy.run_path(os.path.join(ROOT, 'app_src', 'gunicorn.conf.py'))
        return {'workers': ns['workers'], 'threads': ns['threads'],
                'max_requests': ns['max_requests'],
                'max_requests_jitter': ns['max_requests_jitter'],
                'graceful_timeout': ns['graceful_timeout'],
                'env_override_present': 'GUNICORN_MAX_REQUESTS' in env,
                'recycles_after_requests': (None if ns['max_requests'] == 0 else
                                            [ns['max_requests'],
                                             ns['max_requests'] + ns['max_requests_jitter']])}
    finally:
        os.chdir(cwd)
        os.environ.clear()
        os.environ.update(keep)


def build_ledger(cfg, fx, test_date):
    """One unique event per student: identity = (student, school-local punch time)."""
    by_sn = collections.defaultdict(list)
    for rec in fx['students']:
        by_sn[rec['device_sn']].append(rec)
    sns = sorted(by_sn)[:DEVICES]
    per_device = int(DURATION // EVENT_EVERY) + 3
    ledger, per_sn = [], {}
    k = 0
    for sn in sns:
        evs = []
        for rec in by_sn[sn][:per_device]:
            t = common.device_time_for(k)          # 06:45:00–08:44:59, deterministic
            time_str = f"{test_date.isoformat()} {t.strftime('%H:%M:%S')}"
            ev = {'event_id': k, 'sn': sn, 'enrollid': rec['enrollid'],
                  'student_db_id': rec['student_db_id'], 'school_id': rec['school_id'],
                  'time_str': time_str, 'dedup_tag': f'AI Face {time_str}',
                  'attempts': 0, 'acked': False, 'ack_at': None, 'gave_up': False,
                  'last_error': None}
            evs.append(ev)
            ledger.append(ev)
            k += 1
        per_sn[sn] = evs
    return ledger, per_sn


class Driver:
    def __init__(self, cfg, fx, tokens, per_sn):
        self.cfg, self.fx, self.tokens, self.per_sn = cfg, fx, tokens, per_sn
        self.stop = threading.Event()
        self.reads = []                       # (t, ms, status)
        self.read_ok = 0
        self.read_errors = collections.Counter()
        self.ws_errors = collections.Counter()
        self.reconnects = []
        self.lock = threading.Lock()
        self.t0 = time.monotonic()

    def rel(self):
        return round(time.monotonic() - self.t0, 2)

    def read_loop(self, offset):
        """Persistent keep-alive connection, like the round's FastHttpUser."""
        students = self.fx['students']
        i = offset
        interval = 1.0 / READ_RATE
        conn = None
        while not self.stop.is_set():
            rec = students[i % len(students)]
            i += READ_THREADS
            token = self.tokens['tokens'][rec['username']]
            path = f"/api/mobile/v1/parent/children/{rec['student_db_id']}/attendance"
            t1 = time.perf_counter()
            status = None
            try:
                if conn is None:
                    conn = http.client.HTTPConnection('127.0.0.1', self.cfg['http_port'], timeout=120)
                conn.request('GET', path, headers={'Authorization': f'Bearer {token}',
                                                   'Connection': 'keep-alive'})
                resp = conn.getresponse()
                resp.read()
                status = resp.status
            except Exception as exc:
                with self.lock:
                    self.read_errors[type(exc).__name__] += 1
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
            ms = (time.perf_counter() - t1) * 1000
            with self.lock:
                self.reads.append((self.rel(), round(ms, 1), status))
                if status == 200:
                    self.read_ok += 1
                elif status is not None:
                    self.read_errors[f'http_{status}'] += 1
            time.sleep(max(0.0, interval - (time.perf_counter() - t1)))
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def device_loop(self, sn):
        """Retain the pending event and retry the SAME identity after a failure."""
        url = f"ws://127.0.0.1:{self.cfg['ws_port']}"
        dev = aiface_client.AiFaceDevice(url, sn)
        events = self.per_sn[sn]
        connected = False
        i = 0
        next_due = time.monotonic()
        while not self.stop.is_set() and i < len(events):
            if not connected:
                try:
                    dev.connect(timeout=20)
                    connected = True
                    with self.lock:
                        self.reconnects.append({'t': self.rel(), 'sn': sn, 'outcome': 'connected'})
                except Exception as exc:
                    name = type(exc).__name__
                    with self.lock:
                        self.ws_errors[name] += 1
                        self.reconnects.append({'t': self.rel(), 'sn': sn, 'outcome': name})
                    time.sleep(RECONNECT_BACKOFF)
                    continue
            now = time.monotonic()
            if now < next_due:
                time.sleep(min(0.2, next_due - now))
                continue
            ev = events[i]
            ev['attempts'] += 1
            try:
                slot = dev.begin_sendlog([aiface_client.AiFaceDevice.record(
                    ev['enrollid'], ev['time_str'][:10], ev['time_str'][11:])])
                ack = dev.wait_ack(slot, timeout=ACK_TIMEOUT)
                if ack.get('result'):
                    ev['acked'] = True
                    ev['ack_at'] = self.rel()
                    i += 1
                    next_due = time.monotonic() + EVENT_EVERY
                else:
                    ev['last_error'] = 'ack_result_false'
            except Exception as exc:
                ev['last_error'] = type(exc).__name__
                with self.lock:
                    self.ws_errors[f'send_{type(exc).__name__}'] += 1
                connected = False
                dev.close()
            if not ev['acked'] and ev['attempts'] >= MAX_ATTEMPTS:
                ev['gave_up'] = True
                i += 1
                next_due = time.monotonic() + EVENT_EVERY
        dev.close()


def observe_workers(log_path, stop, out):
    pos = 0
    while not stop.is_set():
        try:
            size = os.path.getsize(log_path)
            if size > pos:
                with open(log_path, 'r', errors='replace') as fh:
                    fh.seek(pos)
                    chunk = fh.read(size - pos)
                pos = size
                for line in chunk.splitlines():
                    for key in ('Autorestarting worker', 'Worker exiting', 'Booting worker',
                                'master owns the AI Face WS', 'AI Face WS server listening',
                                'already in use', 'receiver disabled'):
                        if key in line:
                            out.append({'mono': round(time.monotonic(), 2),
                                        'at': dt.datetime.now().isoformat(timespec='seconds'),
                                        'line': line.strip()[:200]})
                            break
        except OSError:
            pass
        time.sleep(0.4)


def reconcile(cfg, sec, ledger, test_date):
    conn = psycopg2.connect(**common.pg_dsn(cfg, sec))
    cur = conn.cursor()
    cur.execute("""SELECT sa.student_id, sa.school_id, s.school_id, COALESCE(sa.notes, ''), sa.check_in
                   FROM student_attendance sa JOIN students s ON s.id = sa.student_id
                   WHERE sa.date = %s""", (test_date,))
    rows = cur.fetchall()
    conn.close()
    by_student = {}
    row_count = collections.Counter()
    for sid, att_school, stu_school, notes, check_in in rows:
        row_count[sid] += 1
        by_student.setdefault(sid, []).append(
            {'att_school': att_school, 'student_school': stu_school, 'notes': notes,
             'check_in': str(check_in) if check_in else None})

    out = {
        'test_date': test_date.isoformat(),
        'identity_used': 'dedup tag "AI Face <punch time>" inside student_attendance.notes',
        'unique_events_planned': len(ledger),
        'unique_events_attempted': 0, 'send_attempts_total': 0, 'retry_attempts': 0,
        'acknowledged': 0, 'verified_committed': 0, 'unacked_but_committed': 0,
        'acked_but_absent': 0, 'unacked_and_absent': 0, 'gave_up': 0,
        'attribution_mismatches': [], 'duplicate_rows': {}, 'duplicate_tag_occurrences': [],
        'rows_for_test_date': len(rows), 'rows_not_in_ledger': 0,
        'examples': {'unacked_but_committed': [], 'absent': []},
    }
    ledger_students = {e['student_db_id'] for e in ledger if e['attempts'] > 0}
    for ev in ledger:
        if ev['attempts'] == 0:
            continue
        out['unique_events_attempted'] += 1
        out['send_attempts_total'] += ev['attempts']
        out['retry_attempts'] += ev['attempts'] - 1
        if ev['acked']:
            out['acknowledged'] += 1
        if ev['gave_up']:
            out['gave_up'] += 1
        recs = by_student.get(ev['student_db_id'], [])
        hit = next((r for r in recs if ev['dedup_tag'] in r['notes']), None)
        if hit is None:
            if ev['acked']:
                out['acked_but_absent'] += 1
            else:
                out['unacked_and_absent'] += 1
            if len(out['examples']['absent']) < 5:
                out['examples']['absent'].append(
                    {'event_id': ev['event_id'], 'student': ev['student_db_id'],
                     'attempts': ev['attempts'], 'acked': ev['acked'],
                     'last_error': ev['last_error'], 'rows_for_student': len(recs)})
            continue
        if hit['notes'].count(ev['dedup_tag']) > 1:
            out['duplicate_tag_occurrences'].append(ev['event_id'])
        if hit['att_school'] != ev['school_id'] or hit['student_school'] != ev['school_id']:
            out['attribution_mismatches'].append(
                {'event_id': ev['event_id'], 'student': ev['student_db_id'],
                 'expected_school': ev['school_id'], 'row_school': hit['att_school'],
                 'student_school': hit['student_school']})
        if ev['acked']:
            out['verified_committed'] += 1
        else:
            out['unacked_but_committed'] += 1
            if len(out['examples']['unacked_but_committed']) < 5:
                out['examples']['unacked_but_committed'].append(
                    {'event_id': ev['event_id'], 'student': ev['student_db_id'],
                     'attempts': ev['attempts'], 'last_error': ev['last_error']})
    out['duplicate_rows'] = {str(sid): n for sid, n in row_count.items() if n > 1}
    out['rows_not_in_ledger'] = sum(1 for sid in row_count if sid not in ledger_students)
    out['events_absent_from_db'] = out['acked_but_absent'] + out['unacked_and_absent']
    out['committed_total'] = out['verified_committed'] + out['unacked_but_committed']
    return out


def run_variant(variant):
    print(f'\n================ VARIANT: {variant} ================', flush=True)
    outdir = os.path.join(OUTBASE, variant)
    if os.path.exists(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir, exist_ok=True)
    cfg, sec, overlaid = setup_root(variant)
    reset_database(cfg)
    py = '/usr/local/bin/python'
    if sh([py, '/tooling/seed.py', '--root', ROOT]).returncode != 0:
        return {'variant': variant, 'FAILED': 'seed'}
    cfg = common.load_config(ROOT)

    test_date = dt.datetime.now(TZ).date()
    hist_end = dt.date.fromisoformat(cfg['history_end_date'])
    if hist_end >= test_date:
        return {'variant': variant, 'FAILED': f'seeded history ends {hist_end} — not before the '
                                              f'school-local test date {test_date}'}

    fx = json.load(open(os.path.join(ROOT, 'run', 'fixtures.json'), encoding='utf-8'))
    load_ids = [r['student_db_id'] for r in fx['students']]
    conn = psycopg2.connect(**common.pg_dsn(cfg, sec))
    cur = conn.cursor()
    cur.execute('SELECT count(*) FROM student_attendance WHERE date = %s AND student_id = ANY(%s)',
                (test_date, load_ids))
    pre_existing = cur.fetchone()[0]
    conn.close()
    if pre_existing:
        return {'variant': variant,
                'FAILED': f'{pre_existing} attendance rows already exist for {test_date} — '
                          'the fixtures are not fresh, reconciliation would be meaningless'}
    print(f'  test_date={test_date} (Asia/Baghdad); seeded history ends {hist_end}; '
          f'pre-existing rows for the test date: 0', flush=True)

    eff = effective_gunicorn_settings(cfg, sec)
    print(f'  effective gunicorn settings: {eff}', flush=True)

    target_mod.start(cfg, sec)
    sh([py, '/tooling/preauth.py', '--root', ROOT])
    tokens = json.load(open(os.path.join(ROOT, 'secrets', 'tokens.json'), encoding='utf-8'))
    tinfo = json.load(open(os.path.join(ROOT, 'run', 'target.json')))

    ledger, per_sn = build_ledger(cfg, fx, test_date)
    drv = Driver(cfg, fx, tokens, per_sn)
    stop_obs = threading.Event()
    worker_events = []
    threads = [threading.Thread(target=observe_workers, args=(tinfo['log'], stop_obs, worker_events),
                                daemon=True)]
    for n in range(READ_THREADS):
        threads.append(threading.Thread(target=drv.read_loop, args=(n,), daemon=True))
    for sn in per_sn:
        threads.append(threading.Thread(target=drv.device_loop, args=(sn,), daemon=True))
    t_start = time.monotonic()
    for t in threads:
        t.start()
    print(f'  driving {DURATION}s: {READ_THREADS}x{READ_RATE}/s parent reads, '
          f'{len(per_sn)} devices, {len(ledger)} unique events planned', flush=True)
    time.sleep(DURATION)
    drv.stop.set()
    time.sleep(3)
    stop_obs.set()
    time.sleep(1)

    rec = reconcile(cfg, sec, ledger, test_date)
    target_mod.stop(cfg)

    lat = sorted(r[1] for r in drv.reads)

    def pct(p):
        return round(lat[min(len(lat) - 1, int(len(lat) * p))], 1) if lat else None

    by_second = collections.defaultdict(float)
    for t, ms, _ in drv.reads:
        by_second[int(t)] = max(by_second[int(t)], ms)
    stalls = []
    for e in worker_events:
        if 'Worker exiting' in e['line']:
            rel = e['mono'] - t_start
            window = [ms for t, ms, _ in drv.reads if rel <= t <= rel + 15]
            stalls.append({'at_s': round(rel, 1),
                           'max_read_ms_next_15s': round(max(window), 1) if window else None})

    res = {
        'variant': variant,
        'source': ('archived tested revision (unpatched)' if variant == 'unpatched'
                   else f'archived tested revision + patch overlay: {overlaid}'),
        'configuration': {'forced_recycling': variant != 'patched-default',
                          'note': (f'configuration variant: GUNICORN_MAX_REQUESTS forced to '
                                   f'{MAX_REQUESTS}' if variant != 'patched-default'
                                   else 'shipped default: no GUNICORN_MAX_REQUESTS in the environment')},
        'effective_gunicorn': eff,
        'duration_s': DURATION,
        'worker_boots': sum(1 for e in worker_events if 'Booting worker' in e['line']),
        'recycles_observed': max(0, sum(1 for e in worker_events if 'Booting worker' in e['line']) - 1),
        'worker_events': [{'at': e['at'], 'line': e['line']} for e in worker_events],
        'http': {'requests': len(drv.reads), 'successful_200': drv.read_ok,
                 'p50_ms': pct(0.50), 'p95_ms': pct(0.95), 'p99_ms': pct(0.99),
                 'max_ms': lat[-1] if lat else None, 'errors': dict(drv.read_errors),
                 'slowest_10': sorted(drv.reads, key=lambda r: -r[1])[:10],
                 'max_latency_per_second': {str(k): round(v, 1) for k, v in sorted(by_second.items())},
                 'stall_after_each_worker_exit': stalls},
        'websocket': {'errors': dict(drv.ws_errors),
                      'connection_refused': sum(v for k, v in drv.ws_errors.items()
                                                if 'ConnectionRefused' in k),
                      'disconnects': sum(v for k, v in drv.ws_errors.items() if k.startswith('send_')),
                      'reconnect_timeline': drv.reconnects},
        'reconciliation': rec,
        'not_verified': ['real-device getnewlog(stn=true) offline recovery — the synthetic client '
                         're-sends from memory and does not emulate device-side log storage'],
    }
    res['PASS'] = (rec['events_absent_from_db'] == 0 and rec['acked_but_absent'] == 0
                   and not rec['attribution_mismatches'] and not rec['duplicate_rows']
                   and not rec['duplicate_tag_occurrences'] and rec['rows_not_in_ledger'] == 0
                   and res['websocket']['connection_refused'] == 0
                   and rec['unique_events_attempted'] > 0)
    json.dump(res, open(os.path.join(outdir, 'result.json'), 'w'), indent=2, default=str)
    json.dump(ledger, open(os.path.join(outdir, 'ledger.json'), 'w'), indent=2, default=str)
    try:
        shutil.copyfile(tinfo['log'], os.path.join(outdir, 'target.log'))
    except OSError:
        pass
    print(json.dumps({k: v for k, v in res.items()
                      if k not in ('worker_events', 'http')}, indent=2, default=str)[:2500], flush=True)
    return res


def main():
    wait_db()
    os.makedirs(OUTBASE, exist_ok=True)
    out = {'started_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
           'purpose': 'worker-recycling mechanism check with event-level DB reconciliation',
           'not_a_capacity_measurement': True}
    variants = (os.environ.get('RECYCLE_VARIANTS') or 'unpatched,patched,patched-default').split(',')
    out['variants'] = [run_variant(v.strip()) for v in variants]
    by = {v['variant']: v for v in out['variants'] if 'FAILED' not in v}
    out['comparison'] = {
        'effective_max_requests': {k: by[k]['effective_gunicorn']['max_requests'] for k in by},
        'successful_http_reads': {k: by[k]['http']['successful_200'] for k in by},
        'recycles_observed': {k: by[k]['recycles_observed'] for k in by},
        'connection_refused': {k: by[k]['websocket']['connection_refused'] for k in by},
        'device_disconnects': {k: by[k]['websocket']['disconnects'] for k in by},
        'read_p99_ms': {k: by[k]['http']['p99_ms'] for k in by},
        'read_max_ms': {k: by[k]['http']['max_ms'] for k in by},
        'unique_events_attempted': {k: by[k]['reconciliation']['unique_events_attempted'] for k in by},
        'send_attempts_total': {k: by[k]['reconciliation']['send_attempts_total'] for k in by},
        'acknowledged': {k: by[k]['reconciliation']['acknowledged'] for k in by},
        'verified_committed': {k: by[k]['reconciliation']['verified_committed'] for k in by},
        'unacked_but_committed': {k: by[k]['reconciliation']['unacked_but_committed'] for k in by},
        'events_absent_from_db': {k: by[k]['reconciliation']['events_absent_from_db'] for k in by},
        'duplicates': {k: by[k]['reconciliation']['duplicate_rows'] for k in by},
        'attribution_mismatches': {k: len(by[k]['reconciliation']['attribution_mismatches']) for k in by},
    }
    out['finished_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
    json.dump(out, open('/out/recycle_check_v2.json', 'w'), indent=2, default=str)
    print('\n===== SUMMARY =====', flush=True)
    print(json.dumps(out['comparison'], indent=2), flush=True)
    raise SystemExit(0 if all(v.get('PASS') for v in out['variants']
                              if v['variant'].startswith('patched')) else 1)


if __name__ == '__main__':
    main()

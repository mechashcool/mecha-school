"""STAGED institute attendance + durable outbox load test.

Runs INSIDE the runner container, on the internal-only compose network. This
is the staged ladder the smoke test was the single-row rehearsal for.

Order, stopping at the first failure rather than continuing:

  1  prove the network is internal; verify the environment identity
  2  safety gates for BOTH processes, before a row is written
  3  seed the synthetic population and layer the institute fixtures on it
  4  preflight: the ladder's arithmetic must be deterministic and in budget
  5  start the target (outbox enabled), issue instructor tokens
  6  baseline + startup gate + the guarding watchdog with --outbox-monitor
  7  PROVE the watchdog sampled a real queue on its own timer (monitor.csv)
  8  run the ladder, reconciling against the database after every stage

Every stage is gated: the round stops on the first reconciliation violation,
the first watchdog STOP, or the first safety-gate refusal. Nothing is relaxed
or retried to make a stage pass.

Writes /exp/results/institute_load/. The report holds counts, verdicts,
latencies and timings only — never a token, a credential or a payload.
"""
import collections
import datetime as dt
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

sys.path.insert(0, '/tooling')
os.environ['ATTLT_BIN_DIR'] = '/usr/local/bin'      # use the image interpreter

import common                          # noqa: E402
import environment_identity as ident   # noqa: E402
import guard_rules                     # noqa: E402
import institute_common as ic          # noqa: E402
import institute_generator as igen     # noqa: E402
import institute_stages as stages      # noqa: E402
import outbox_monitor as omon          # noqa: E402
import outbox_reconcile as orec        # noqa: E402
import safety_gates                    # noqa: E402
import target as target_mod            # noqa: E402
import worker_control as wc            # noqa: E402

import psycopg2                        # noqa: E402

ROOT = '/exp'
OUT = os.path.join(ROOT, 'results', 'institute_load')
TOOL = '/tooling'
PY = '/usr/local/bin/python'

REPORT = {'started_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
          'preflight': {}, 'stages': [], 'errors': []}

# Worker throughput controls used per mode. Every one of these is an existing
# production environment variable; no application sleep is introduced.
NORMAL_BATCH, NORMAL_POLL = 20, 1.0
THROTTLED_BATCH, THROTTLED_POLL = 5, 2.0
KILL_BATCH, KILL_POLL = 200, 1.0      # one big claim, so a SIGKILL strands rows


class RoundFailed(SystemExit):
    pass


def say(*a):
    print(*a, flush=True)


def section(name):
    say('\n' + '═' * 72)
    say('  ' + name)
    say('═' * 72)


def save():
    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(OUT, '.summary.tmp')
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(REPORT, fh, indent=2, default=str, ensure_ascii=False)
    os.replace(tmp, os.path.join(OUT, 'summary.json'))


# ═══════════════════════════════════════════════════════════════════════════
#  1–2  isolation, identity, gates
# ═══════════════════════════════════════════════════════════════════════════

def prove_internal_network() -> dict:
    """Positive proof that nothing here can leave the private network.

    Four probes: three MUST fail and one MUST succeed. Evidence, not an
    assertion — if a public route works, the network is not internal.
    """
    results, ok = {}, True
    socket.setdefaulttimeout(5)
    try:
        socket.getaddrinfo('pypi.org', 443)
        results['dns_public'] = 'RESOLVED — network is not internal'
        ok = False
    except Exception as exc:
        results['dns_public'] = f'failed as required ({type(exc).__name__})'
    for label, addr in (('tcp_8888_dns', ('8.8.8.8', 53)),
                        ('tcp_cloudflare', ('1.1.1.1', 443))):
        try:
            socket.create_connection(addr, timeout=5).close()
            results[label] = 'CONNECTED — network is not internal'
            ok = False
        except Exception as exc:
            results[label] = f'failed as required ({type(exc).__name__})'
    try:
        socket.create_connection(('127.0.0.1', 7788), timeout=2).close()
        results['local_7788'] = 'SOMETHING IS LISTENING ON 7788'
        ok = False
    except Exception as exc:
        results['local_7788'] = f'nothing listening ({type(exc).__name__})'
    try:
        socket.create_connection((os.environ['ATTLT_PG_HOST'], 5432),
                                 timeout=5).close()
        results['db_reachable'] = 'yes (required)'
    except Exception as exc:
        results['db_reachable'] = f'NO — {type(exc).__name__}'
        ok = False
    socket.setdefaulttimeout(None)
    results['internal_network_proven'] = ok
    return results


def step_identity():
    section('1  network isolation and environment identity')
    proof = prove_internal_network()
    say(json.dumps(proof, indent=2))
    if not proof['internal_network_proven']:
        REPORT['preflight']['isolation'] = proof
        raise RoundFailed('network isolation NOT proven — stopping')

    cfg = common.load_config(ROOT)
    identity = ident.verify(ROOT, experiment_id=cfg['experiment_id'],
                            tag=common.tag(cfg))
    card = {
        'experiment_tag': identity['experiment_tag'],
        'experiment_id': identity['experiment_id'],
        'source_commit': identity['app_source_commit'],
        'database_name': identity['database_name'],
        'database_host': identity['database_host'],
        'database_classification': identity['database_host_classification'],
        'database_isolated': identity['database_isolated'],
        'network_isolation': 'internal docker network, proven from inside',
        'firebase_mode': identity['firebase_mode'],
        'http_port': identity['target_http_port'],
        'ws_port': identity['target_ws_port'],
        'production_reachable': identity['production_network_reachable'],
        'synthetic_prefixes': {
            k: identity[k] for k in identity if k.startswith('synthetic_')},
    }
    assert identity['database_host_classification'] in ('container', 'loopback')
    assert identity['firebase_mode'] == 'fake-local'
    assert identity['production_network_reachable'] is False
    assert identity['target_ws_port'] != ident.PRODUCTION_WS_PORT
    REPORT['preflight']['isolation'] = {'network_proof': proof,
                                        'identity': card}
    say(json.dumps(card, indent=2))
    return cfg


def step_gates(cfg, sec):
    section('2  safety gates — both processes, before any row is written')
    prefixes = ic.experiment_prefixes(common.tag(cfg))
    identity = ident.load(ROOT)
    target_env = target_mod.build_env(cfg, sec, ws_enabled=False,
                                      outbox_enabled=True)
    worker_env = wc.build_worker_env(cfg, sec)
    wc.write_fake_credential(ROOT)
    out = {
        'target': safety_gates.run_all(
            env=target_env, cfg=cfg, identity=identity, prefixes=prefixes,
            role='target', netns_proof_ok=True),
        'worker': safety_gates.run_all(
            env=worker_env, cfg=cfg, identity=identity, prefixes=prefixes,
            role='worker', netns_proof_ok=True),
        'target_aiface_ws': target_env['AIFACE_WS_ENABLED'],
        'target_outbox_enabled': target_env['INSTITUTE_ATTENDANCE_OUTBOX_ENABLED'],
        'worker_aiface_ws': worker_env['AIFACE_WS_ENABLED'],
        'worker_outbox_enabled': worker_env['INSTITUTE_ATTENDANCE_OUTBOX_ENABLED'],
        'worker_role': worker_env['MECHA_PROCESS_ROLE'],
        'worker_flask_env': worker_env['FLASK_ENV'],
        'worker_lease_seconds': worker_env['OUTBOX_LEASE_SECONDS'],
        'schedulers_off': {k: target_env[k] for k in
                           ('ATTENDANCE_SCHEDULER_DISABLED',
                            'FEE_REMINDER_SCHEDULER_DISABLED',
                            'HIKVISION_AUTO_SYNC',
                            'DURABLE_PUSH_QUEUE_ENABLED',
                            'SYNC_JOURNAL_ENABLED', 'SYNC_SIGNAL_ENABLED')},
    }
    REPORT['preflight']['gates'] = out
    say(json.dumps(out, indent=2))


# ═══════════════════════════════════════════════════════════════════════════
#  3  seed
# ═══════════════════════════════════════════════════════════════════════════

def sh(cmd, **kw):
    say('  $', ' '.join(cmd))
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        raise RoundFailed(f'command failed ({r.returncode}): {" ".join(cmd)}')
    return r


def step_seed(cfg, sec):
    section('3  seed the synthetic population + institute fixtures')
    env = target_mod.build_env(cfg, sec, ws_enabled=False, outbox_enabled=True)
    app_src = os.path.join(ROOT, 'app_src')

    conn = psycopg2.connect(**common.pg_dsn(cfg, sec))
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute('CREATE TABLE IF NOT EXISTS attlt_owner (experiment_id text)')
    cur.execute('SELECT count(*) FROM attlt_owner')
    if cur.fetchone()[0] == 0:
        cur.execute('INSERT INTO attlt_owner VALUES (%s)',
                    (cfg['experiment_id'],))
    conn.close()

    t0 = time.time()
    sh([f'{os.path.dirname(PY)}/flask', 'db', 'upgrade'], cwd=app_src, env=env)
    t1 = time.time()
    sh([PY, f'{TOOL}/seed.py', '--root', ROOT, '--inside'], cwd=app_src, env=env)
    t2 = time.time()
    sh([PY, f'{TOOL}/institute_fixtures.py', '--root', ROOT, '--inside'],
       cwd=app_src, env=env)
    t3 = time.time()

    with open(os.path.join(ROOT, 'run', 'institute_fixtures.json'),
              encoding='utf-8') as fh:
        inst = json.load(fh)
    counts = row_counts(cfg, sec)
    payload = {
        'migrate_seconds': round(t1 - t0, 1),
        'seed_seconds': round(t2 - t1, 1),
        'institute_fixture_seconds': round(t3 - t2, 1),
        'config': {k: cfg[k] for k in ('num_schools', 'students_per_school',
                                       'devices_per_school',
                                       'history_calendar_days')},
        'institute_totals': inst['totals'],
        'row_counts': counts,
        'test_date': inst['test_date'],
    }
    REPORT['preflight']['fixtures'] = payload
    say(json.dumps(payload, indent=2))
    return inst


def row_counts(cfg, sec) -> dict:
    conn = psycopg2.connect(**common.pg_dsn(cfg, sec))
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor()
    out = {}
    for table in ('schools', 'students', 'users', 'employees',
                  'institute_study_groups', 'institute_group_enrollments',
                  'institute_attendance_sessions',
                  'institute_attendance_records', 'mobile_device_tokens',
                  'notifications', 'notification_outbox', 'parent_students'):
        cur.execute(f'SELECT count(*) FROM {table}')
        out[table] = cur.fetchone()[0]
    conn.close()
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  4  preflight: the ladder's arithmetic
# ═══════════════════════════════════════════════════════════════════════════

def step_plan(cfg, inst):
    section('4  preflight — ladder arithmetic and determinism')
    budget = stages.budget(cfg['num_schools'])
    alloc = stages.allocate(cfg['num_schools'])

    peak = stages.peak_stopped_backlog()
    ceiling = guard_rules.OUTBOX_THRESHOLDS['outbox_backlog_ceiling']
    if peak >= ceiling:
        raise RoundFailed(f'planned stopped-worker backlog {peak} would reach '
                          f'the watchdog ceiling {ceiling}')

    plans, predicted = {}, {}
    for stage in stages.LADDER:
        af = stages.absent_fraction_for(stage['transitions_per_session'])
        a = igen.build_plan(cfg, inst, waves=1, rate=1.0, absent_fraction=af)
        b = igen.build_plan(cfg, inst, waves=1, rate=1.0, absent_fraction=af)
        if json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True):
            raise RoundFailed(f'{stage["name"]}: plan is not deterministic')
        entries = stages.entries_for(a, inst, alloc[stage['name']])
        exp = igen.expected_outbox_jobs(entries, inst, cfg)
        want_tr = stages.stage_transitions(stage)
        want_jobs = stages.stage_jobs(stage)
        if exp['newly_absent_transitions'] != want_tr:
            raise RoundFailed(
                f'{stage["name"]}: generator predicts '
                f'{exp["newly_absent_transitions"]} transitions, the ladder '
                f'declares {want_tr} — arithmetic is not deterministic')
        if exp['expected_jobs'] != want_jobs:
            raise RoundFailed(
                f'{stage["name"]}: generator predicts {exp["expected_jobs"]} '
                f'jobs, the ladder declares {want_jobs}')
        sessions = {e['session_id'] for e in entries}
        if len(sessions) != len(entries):
            raise RoundFailed(f'{stage["name"]}: duplicate session in stage')
        plans[stage['name']] = entries
        predicted[stage['name']] = exp

    all_sessions = [s for st in plans.values() for e in st
                    for s in [e['session_id']]]
    if len(set(all_sessions)) != len(all_sessions):
        raise RoundFailed('a session is used by more than one stage — the '
                          'second use would create no transitions')

    payload = {
        'budget': budget,
        'peak_planned_stopped_backlog_jobs': peak,
        'watchdog_backlog_ceiling': ceiling,
        'headroom_jobs': ceiling - peak,
        'sessions_used_by_ladder': len(all_sessions),
        'sessions_disjoint_across_stages': True,
        'deterministic': True,
        'tokens_per_parent_active': predicted[stages.LADDER[0]['name']][
            'active_tokens_per_parent'],
    }
    REPORT['preflight']['ladder'] = payload
    say(json.dumps(payload, indent=2))
    return plans, predicted, alloc


# ═══════════════════════════════════════════════════════════════════════════
#  5–6  target, tokens, watchdog
# ═══════════════════════════════════════════════════════════════════════════

def step_target(cfg, sec, inst):
    section('5  start the target and issue instructor tokens')
    target_mod.start(cfg, sec, ws_enabled=False, outbox_enabled=True)
    env = target_mod.build_env(cfg, sec, ws_enabled=False, outbox_enabled=True)
    sh([PY, f'{TOOL}/institute_generator.py', 'issue-tokens', '--root', ROOT,
        '--issue-tokens-inside'], cwd=os.path.join(ROOT, 'app_src'), env=env)
    with open(os.path.join(ROOT, igen.TOKENS_FILE), encoding='utf-8') as fh:
        tokens = json.load(fh)['tokens']
    say(f'issued {len(tokens)} instructor tokens (values never reported)')
    return tokens


def step_watchdog(baseline_seconds=20):
    """Baseline, startup gate, then the guarding watchdog with outbox sampling.

    The watchdog is given a SENTINEL process as its generator. The sentinel
    holds a pipe open and exits when the runner closes it, which is what makes
    the watchdog leave its guard loop cleanly and write watchdog_summary.json
    instead of being killed with the round half-recorded.
    """
    section('6  baseline, startup gate, guarding watchdog (--outbox-monitor)')
    wd = os.path.join(TOOL, 'watchdog.py')
    sh([PY, wd, '--root', ROOT, '--out', OUT, '--baseline',
        str(baseline_seconds)])

    gate = subprocess.run([PY, wd, '--root', ROOT, '--out', OUT,
                           '--check-gate'], capture_output=True, text=True)
    say(gate.stdout.strip() or gate.stderr.strip())
    gate_json = json.load(open(os.path.join(OUT, 'startup_gate.json')))
    REPORT['preflight']['startup_gate'] = gate_json
    if gate.returncode != 0 or not gate_json.get('ok'):
        raise RoundFailed(f'startup safety gate REFUSED the round: '
                          f'{gate_json.get("reasons")}')
    if gate_json.get('compliance') != guard_rules.COMPLIANCE_ENFORCED:
        raise RoundFailed(f'resource-guard compliance is '
                          f'{gate_json.get("compliance")}, not ENFORCED')

    sentinel = subprocess.Popen([PY, '-c', 'import sys; sys.stdin.read()'],
                                stdin=subprocess.PIPE)
    log = open(os.path.join(ROOT, 'logs', 'watchdog.log'), 'ab')
    proc = subprocess.Popen(
        [PY, wd, '--root', ROOT, '--out', OUT,
         '--generator-pid', str(sentinel.pid), '--post-seconds', '20',
         '--outbox-monitor'],
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    say(f'watchdog pid {proc.pid}, sentinel pid {sentinel.pid}')
    return proc, sentinel


def step_live_outbox_proof(timeout=90):
    """The watchdog must be seen sampling a REAL queue on its OWN timer.

    Unit tests proved the wiring against fakes. This reads the artifact the
    watchdog writes by itself and refuses to continue unless the outbox
    columns are present and populated.
    """
    section('7  live proof: the watchdog is sampling the real outbox')
    path = os.path.join(OUT, 'monitor.csv')
    required = ['outbox_collector_ok', 'outbox_pending', 'outbox_processing',
                'outbox_retry', 'outbox_sent', 'outbox_dead',
                'outbox_cancelled', 'outbox_total_jobs', 'outbox_backlog',
                'outbox_backlog_falling', 'outbox_worker_alive',
                'outbox_unplanned_worker_restarts']
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        time.sleep(3)
        rows = read_monitor()
        if not rows:
            continue
        last = rows[-1]
        if all(k in last and last[k] not in (None, '') for k in required):
            break
    if last is None:
        raise RoundFailed('the watchdog wrote no monitor.csv — it is not '
                          'sampling')
    missing = [k for k in required if k not in last or last[k] in (None, '')]
    if missing:
        raise RoundFailed(f'monitor.csv outbox fields missing/null: {missing} '
                          f'— the watchdog is not sampling the real queue')
    if str(last['outbox_collector_ok']).lower() not in ('true', '1'):
        raise RoundFailed(f'outbox collector reported NOT ok: '
                          f'{last.get("outbox_collector_error")}')
    payload = {'monitor_rows': len(read_monitor()),
               'sampled_fields': {k: last[k] for k in required},
               'collector_ok': True,
               'note': 'written by watchdog.py on its own 2 s timer, against '
                       'the isolated experiment database'}
    REPORT['preflight']['live_outbox_sample'] = payload
    say(json.dumps(payload, indent=2))


def read_monitor() -> list:
    import csv
    path = os.path.join(OUT, 'monitor.csv')
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline='', encoding='utf-8') as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def watchdog_stop_file():
    path = os.path.join(OUT, 'STOP.json')
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding='utf-8'))
        except (OSError, ValueError):
            return {'mode': 'halt', 'reason': 'STOP.json unreadable'}
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  The driver
# ═══════════════════════════════════════════════════════════════════════════

def submit_one(base, entry, token, timeout=60.0):
    body = json.dumps({'records': [
        {'student_id': sid, 'status': st}
        for sid, st in sorted(entry['intended_status_map'].items())]
    }).encode('utf-8')
    url = (f"{base}/api/mobile/v1/teacher/institute/sessions/"
           f"{entry['session_id']}/attendance")
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('Authorization', 'Bearer ' + token)
    started = time.time()
    status_code, keys, error = None, None, None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status_code = resp.status
            payload = json.loads(resp.read().decode('utf-8') or '{}')
            keys = sorted(payload)
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        error = f'HTTP {exc.code}'
    except Exception as exc:
        error = type(exc).__name__
    return started, round((time.time() - started) * 1000, 2), status_code, \
        keys, error


def drive(cfg, entries, tokens, *, rate, concurrency, ledger_path):
    """Submit one stage's entries, paced to `rate` requests/second.

    Each worker thread owns the entries whose index is congruent to its own
    modulo the thread count, and sleeps until that entry's scheduled offset, so
    concurrency never changes the intended arrival schedule.
    """
    base = f"http://{cfg.get('target_host', '127.0.0.1')}:{cfg['http_port']}"
    rows, lock = [], threading.Lock()
    t0 = time.time()

    def run(worker_idx):
        for i in range(worker_idx, len(entries), concurrency):
            e = entries[i]
            due = t0 + (i / rate if rate > 0 else 0.0)
            delay = due - time.time()
            if delay > 0:
                time.sleep(delay)
            started, ms, code, keys, error = submit_one(
                base, e, tokens[e['instructor_username']])
            rec = ic.ledger_record(
                experiment_id=cfg['experiment_id'], seq=e['seq'],
                school_idx=e['school_idx'], school_id=e['school_id'],
                group_id=e['group_id'], session_id=e['session_id'],
                wave=e['wave'], student_ids=e['student_ids'],
                absent_student_ids=e['absent_student_ids'],
                intended_status_map=e['intended_status_map'],
                transition_id=e['transition_id'],
                submitted_at=round(started - t0, 4), response_status=code,
                response_body_keys=keys, latency_ms=ms, attempt=1,
                retried=False, error=error)
            with lock:
                rows.append(rec)

    threads = [threading.Thread(target=run, args=(w,), daemon=True)
               for w in range(max(1, concurrency))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0
    rows.sort(key=lambda r: r['seq'])
    with open(ledger_path, 'a', encoding='utf-8') as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + '\n')
    return rows, elapsed


def latency_stats(rows) -> dict:
    ok = [r['latency_ms'] for r in rows
          if r['response_status'] == 200 and not r['error']]
    if not ok:
        return {'n': 0}
    s = sorted(ok)

    def pct(p):
        if len(s) == 1:
            return s[0]
        idx = min(len(s) - 1, max(0, int(round((p / 100) * (len(s) - 1)))))
        return s[idx]

    return {'n': len(s), 'min_ms': s[0], 'max_ms': s[-1],
            'mean_ms': round(statistics.fmean(s), 2),
            'p50_ms': pct(50), 'p95_ms': pct(95), 'p99_ms': pct(99)}


# ═══════════════════════════════════════════════════════════════════════════
#  Observation and reconciliation
# ═══════════════════════════════════════════════════════════════════════════

class FakeTally:
    """Accumulate the fake Firebase ledger ACROSS worker generations.

    The fake's counters live in the worker process, so a restart resets the
    file. Sealing the current generation before every stop/kill and merging the
    salted fingerprint histograms is what makes a duplicate send caused by a
    lease reclaim visible at all — within one generation it would look like two
    unrelated sends in two different files.
    """

    def __init__(self, root):
        self._path = os.path.join(root, 'run', 'fake_fcm_sends.json')
        self._sealed = {'attempts': 0, 'successes': 0, 'failures': 0}
        self._fingerprints = collections.Counter()

    def _current(self) -> dict:
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, encoding='utf-8') as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def seal(self) -> None:
        cur = self._current()
        if not cur:
            return
        for k in ('attempts', 'successes', 'failures'):
            self._sealed[k] += int(cur.get(k, 0))
        self._fingerprints.update(cur.get('fingerprint_counts') or {})
        try:
            os.remove(self._path)
        except OSError:
            pass

    def merged(self) -> dict:
        cur = self._current()
        fps = collections.Counter(self._fingerprints)
        fps.update(cur.get('fingerprint_counts') or {})
        return {
            'fake_attempts': self._sealed['attempts'] + int(cur.get('attempts', 0)),
            'fake_successes': self._sealed['successes'] + int(cur.get('successes', 0)),
            'fake_failures': self._sealed['failures'] + int(cur.get('failures', 0)),
            'fake_distinct': len(fps),
            'fake_duplicate_sends': sum(n - 1 for n in fps.values() if n > 1),
        }


def observe(cfg, sec, school_ids, session_ids, test_date, tally, reclaims):
    conn = psycopg2.connect(**common.pg_dsn(cfg, sec))
    conn.set_session(readonly=True, autocommit=True)
    try:
        obs = orec.collect(conn.cursor(), school_ids=school_ids,
                           session_ids=session_ids, test_date=test_date)
    finally:
        conn.close()
    obs.update(tally.merged())
    obs['worker_reclaims'] = reclaims
    return obs


def reconcile(ledger_rows, obs, tokens_per_parent, *, drain_complete):
    expected = orec.expected_from_ledger(
        ledger_rows, tokens_per_parent=tokens_per_parent)
    return orec.verdict(orec.compute(expected, obs),
                        drain_complete=drain_complete)


def count_reclaims(root) -> int:
    """How many stale leases the worker has reclaimed, from its own log."""
    path = os.path.join(root, 'logs', 'worker.log')
    if not os.path.exists(path):
        return 0
    total = 0
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            if 'reclaimed' in line and 'stale lease' in line:
                for word in line.split():
                    if word.isdigit():
                        total += int(word)
                        break
    return total


def db_activity(cfg, sec) -> dict:
    conn = psycopg2.connect(**common.pg_dsn(cfg, sec))
    conn.set_session(readonly=True, autocommit=True)
    cur = conn.cursor()
    cur.execute("""SELECT count(*), count(*) FILTER (WHERE state='active'),
                          count(*) FILTER (WHERE wait_event_type='Lock')
                     FROM pg_stat_activity WHERE datname=%s""",
                (cfg['db_name'],))
    n, active, locks = cur.fetchone()
    cur.execute('SHOW max_connections')
    maxc = int(cur.fetchone()[0])
    cur.execute("""SELECT xact_commit, xact_rollback, deadlocks, blks_read
                     FROM pg_stat_database WHERE datname=%s""",
                (cfg['db_name'],))
    xc, xr, dl, br = cur.fetchone()
    conn.close()
    return {'connections': n, 'active': active, 'lock_waits': locks,
            'max_connections': maxc, 'xact_commit': xc,
            'xact_rollback': xr, 'deadlocks': dl, 'blocks_read': br}


# ═══════════════════════════════════════════════════════════════════════════
#  Queue observation
# ═══════════════════════════════════════════════════════════════════════════

def sample_backlog(sampler, th) -> dict:
    s = sampler.sample(window_s=th['outbox_no_drain_window_s'])
    if not s.get('collector_ok'):
        raise RoundFailed(f'outbox collector failed: {s.get("collector_error")}')
    return s


def drain_to_zero(sampler, th, *, timeout, interval=0.25):
    """Watch the backlog fall to zero, at a finer cadence than the watchdog.

    The watchdog samples every 2 s for SAFETY. This samples much faster purely
    to record the shape of the drain, because a fast drain would otherwise be
    three points on a curve.
    """
    curve, t0 = [], time.time()
    falling_seen = False
    while time.time() - t0 < timeout:
        s = sample_backlog(sampler, th)
        curve.append({'t': round(time.time() - t0, 2),
                      'backlog': s['outbox_backlog'],
                      'pending': s['outbox_pending'],
                      'processing': s['outbox_processing'],
                      'retry': s['outbox_retry'],
                      'sent': s['outbox_sent'],
                      'falling': s['outbox_backlog_falling']})
        falling_seen = falling_seen or bool(s['outbox_backlog_falling'])
        if s['outbox_backlog'] == 0:
            break
        time.sleep(interval)
    return {'seconds': round(time.time() - t0, 2), 'curve': curve,
            'drained': bool(curve and curve[-1]['backlog'] == 0),
            'backlog_falling_observed': falling_seen,
            'max_backlog': max((c['backlog'] for c in curve), default=0)}


def peak_resources(from_row=0) -> dict:
    rows = read_monitor()[from_row:]

    def mx(key, cast=float):
        vals = [cast(r[key]) for r in rows
                if r.get(key) not in (None, '', 'None')]
        return max(vals) if vals else None

    def mn(key, cast=float):
        vals = [cast(r[key]) for r in rows
                if r.get(key) not in (None, '', 'None')]
        return min(vals) if vals else None

    return {
        'samples': len(rows),
        'host_cpu_pct_max': mx('host_cpu_pct'),
        'host_mem_available_pct_min': mn('host_mem_available_pct'),
        'host_swap_used_gb_max': mx('host_swap_used_gb'),
        'disk_free_gb_min': mn('disk_free_gb'),
        'target_cpu_pct_max': mx('target_cpu_pct'),
        'target_rss_mb_max': mx('target_rss_mb'),
        'target_threads_max': mx('target_threads'),
        'db_connections_max': mx('db_connections', int),
        'db_active_max': mx('db_active', int),
        'db_lock_waits_max': mx('db_lock_waits', int),
        'db_longest_active_s_max': mx('db_longest_active_s'),
        'outbox_backlog_max': mx('outbox_backlog', int),
        'outbox_dead_max': mx('outbox_dead', int),
        'outbox_cancelled_max': mx('outbox_cancelled', int),
        'outbox_unplanned_restarts_max': mx('outbox_unplanned_worker_restarts', int),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  8  the ladder
# ═══════════════════════════════════════════════════════════════════════════

class Round:
    def __init__(self, cfg, sec, inst, plans, tokens):
        self.cfg, self.sec, self.inst = cfg, sec, inst
        self.plans, self.tokens = plans, tokens
        self.school_ids = [s['school_id'] for s in inst['schools'].values()]
        self.session_ids = [g['session_id'] for s in inst['schools'].values()
                            for g in s['groups']]
        self.test_date = inst['test_date']
        self.tpp = ic.TOKENS_PER_PARENT
        self.th = guard_rules.build_thresholds()
        self.sampler = omon.sampler_for_experiment(ROOT, cfg, sec,
                                                   self.school_ids)
        self.tally = FakeTally(ROOT)
        self.ledger_path = os.path.join(OUT, 'institute_events.jsonl')
        self.rows = []
        self.worker_mode = None

    # ── worker lifecycle ──────────────────────────────────────────────────
    def worker_running(self) -> bool:
        return bool(wc.status(self.cfg).get('running'))

    def start_worker(self, *, batch_size, poll_seconds):
        wc.start(self.cfg, self.sec, netns_proof_ok=True,
                 batch_size=batch_size, poll_seconds=poll_seconds)

    def stop_worker(self):
        # Seal AFTER the process is gone. The fake's counters live in the
        # worker, so sealing first would let a still-running worker re-flush
        # the same sends into a fresh file and be counted twice.
        if self.worker_running():
            wc.stop(self.cfg)
            self.tally.seal()

    def kill_worker(self):
        info = wc.kill(self.cfg)
        self.tally.seal()
        return info

    # ── checks ────────────────────────────────────────────────────────────
    def guard_check(self, stage_name):
        stop = watchdog_stop_file()
        if stop:
            raise RoundFailed(f'{stage_name}: watchdog STOP ({stop["mode"]}): '
                              f'{stop["reason"]}')

    def observe(self):
        return observe(self.cfg, self.sec, self.school_ids, self.session_ids,
                       self.test_date, self.tally, count_reclaims(ROOT))

    def reconcile(self, *, drain_complete):
        obs = self.observe()
        return reconcile(self.rows, obs, self.tpp,
                         drain_complete=drain_complete), obs

    def drain(self, timeout=240):
        return drain_to_zero(self.sampler, self.th, timeout=timeout)

    # ── one stage ─────────────────────────────────────────────────────────
    def run_stage(self, stage) -> dict:
        name = stage['name']
        section(f'STAGE {name} — {stage["purpose"]}')
        mon_before = len(read_monitor())
        rec = {'name': name, 'purpose': stage['purpose'],
               'worker_mode': stage['worker'],
               'planned': {
                   'sessions': stage['slots'],
                   'absent_per_session': stage['transitions_per_session'],
                   'transitions': stages.stage_transitions(stage),
                   'jobs': stages.stage_jobs(stage),
                   'target_transitions_per_s': stage['target_tps'],
                   'request_rate_per_s': round(stages.request_rate(stage), 3),
                   'concurrency': stage['concurrency']},
               'started_utc': dt.datetime.now(dt.timezone.utc).isoformat()}

        entries = self.plans[name]
        mode = stage['worker']

        if mode == stages.WORKER_RUNNING:
            if not self.worker_running():
                self.start_worker(batch_size=NORMAL_BATCH,
                                  poll_seconds=NORMAL_POLL)
            rec['worker_before'] = wc.status(self.cfg).get('running')
            rec.update(self._drive(stage, entries))
            rec['drain'] = self.drain()
            drain_complete = True

        elif mode == stages.WORKER_STOPPED:
            self.stop_worker()
            if self.worker_running():
                raise RoundFailed(f'{name}: worker did not stop')
            rec['worker_before'] = False
            fake_before = self.tally.merged()['fake_attempts']
            rec.update(self._drive(stage, entries))
            after = sample_backlog(self.sampler, self.th)
            rec['backlog_after_stage'] = after['outbox_backlog']
            rec['worker_alive_during_stage'] = bool(after['worker_alive'])
            rec['fake_sends_before'] = fake_before
            rec['fake_sends_after'] = self.tally.merged()['fake_attempts']
            rec['fake_sends_unchanged_while_stopped'] = (
                rec['fake_sends_after'] == fake_before)
            rec['stuck_drain_rule_fired'] = bool(watchdog_stop_file())
            drain_complete = False

        elif mode == stages.WORKER_DRAINING:
            before = sample_backlog(self.sampler, self.th)
            rec['backlog_before_start'] = before['outbox_backlog']
            if before['outbox_backlog'] <= 0:
                raise RoundFailed(f'{name}: nothing to drain — the previous '
                                  f'stopped stage left no backlog')
            self.start_worker(batch_size=THROTTLED_BATCH,
                              poll_seconds=THROTTLED_POLL)
            rec['worker_throttle'] = {'OUTBOX_BATCH_SIZE': THROTTLED_BATCH,
                                      'OUTBOX_POLL_SECONDS': THROTTLED_POLL}
            rec.update(self._drive(stage, entries))
            rec['drain'] = self.drain(timeout=300)
            drain_complete = True

        elif mode == stages.WORKER_KILL_CYCLE:
            rec.update(self._kill_cycle(stage, entries))
            drain_complete = True

        else:
            raise RoundFailed(f'{name}: unknown worker mode {mode!r}')

        self.guard_check(name)
        verdict, obs = self.reconcile(drain_complete=drain_complete)
        rec['reconciliation'] = verdict
        rec['outbox_status_counts'] = obs['outbox_status_counts']
        rec['db'] = db_activity(self.cfg, self.sec)
        rec['resources'] = peak_resources(mon_before)
        rec['worker_status'] = {k: v for k, v in wc.status(self.cfg).items()
                                if k != 'log'}
        rec['finished_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
        rec['PASS'] = bool(verdict['correct']) and not watchdog_stop_file()
        REPORT['stages'].append(rec)
        save()
        say(json.dumps({k: rec[k] for k in
                        ('name', 'worker_mode', 'submitted', 'accepted',
                         'latency', 'PASS') if k in rec}, indent=2))
        if not verdict['correct']:
            raise RoundFailed(f'{name}: reconciliation FAILED: '
                              f'{verdict["violations"]}')
        self.guard_check(name)
        return rec

    def _drive(self, stage, entries) -> dict:
        rows, elapsed = drive(
            self.cfg, entries, self.tokens,
            rate=stages.request_rate(stage),
            concurrency=stage['concurrency'], ledger_path=self.ledger_path)
        self.rows.extend(rows)
        accepted = [r for r in rows
                    if r['response_status'] == 200 and not r['error']]
        transitions = sum(len(r['absent_student_ids']) for r in accepted)
        codes = collections.Counter(
            str(r['response_status']) if not r['error'] else r['error']
            for r in rows)
        return {
            'submitted': len(rows),
            'accepted': len(accepted),
            'rejected': len(rows) - len(accepted),
            'response_codes': dict(codes),
            'transitions_submitted': transitions,
            'elapsed_s': round(elapsed, 2),
            'achieved_transitions_per_s': round(transitions / max(elapsed, 1e-6), 2),
            'achieved_requests_per_s': round(len(rows) / max(elapsed, 1e-6), 2),
            'latency': latency_stats(rows),
        }

    def _kill_cycle(self, stage, entries) -> dict:
        """Mode C: build a backlog, let one big batch be claimed, SIGKILL it."""
        out = {}
        self.stop_worker()
        out.update(self._drive(stage, entries))
        pre = sample_backlog(self.sampler, self.th)
        out['backlog_before_worker'] = pre['outbox_backlog']

        self.start_worker(batch_size=KILL_BATCH, poll_seconds=KILL_POLL)
        out['claim_batch_size'] = KILL_BATCH
        claimed, deadline = 0, time.time() + 20
        while time.time() < deadline:
            s = sample_backlog(self.sampler, self.th)
            claimed = s['outbox_processing']
            if claimed > 0:
                break
            if s['outbox_backlog'] == 0:
                break
            time.sleep(0.05)
        out['processing_observed_before_kill'] = claimed

        before_kill = wc.status(self.cfg)
        killed = self.kill_worker()
        out['killed_worker'] = {'pid': (killed or {}).get('pid'),
                                'identity_verified': killed is not None,
                                'was_running': bool(before_kill.get('running'))}
        if killed is None:
            raise RoundFailed('kill refused — worker identity did not verify')
        time.sleep(1.0)
        if self.worker_running():
            # The first run of this stage stalled here: a reaped-but-not-waited
            # zombie still reported alive, so start() below silently declined to
            # launch a replacement and the claimed rows were never reclaimed.
            raise RoundFailed('the killed worker still reports running — a '
                              'replacement would be refused and the claimed '
                              'rows could never be reclaimed')
        out['stranded_processing_after_kill'] = None
        stranded = sample_backlog(self.sampler, self.th)
        out['stranded_processing_after_kill'] = stranded['outbox_processing']
        out['backlog_after_kill'] = stranded['outbox_backlog']

        reclaims_before = count_reclaims(ROOT)
        before_pid = (killed or {}).get('pid')
        self.start_worker(batch_size=NORMAL_BATCH, poll_seconds=NORMAL_POLL)
        restarted = wc.status(self.cfg)
        out['restarted_worker_pid'] = restarted.get('pid')
        if not restarted.get('running') or restarted.get('pid') == before_pid:
            raise RoundFailed('no replacement worker is running after the '
                              'kill — the reclaim could never happen')
        lease = wc.EXPERIMENT_LEASE_SECONDS
        out['lease_seconds'] = lease
        out['drain'] = self.drain(timeout=lease + 180)
        out['reclaims_logged'] = count_reclaims(ROOT) - reclaims_before
        out['unplanned_worker_restarts'] = sample_backlog(
            self.sampler, self.th).get('unplanned_worker_restarts')
        return out


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    os.makedirs(OUT, exist_ok=True)
    cfg = step_identity()
    sec = common.load_secrets(ROOT)
    step_gates(cfg, sec)
    inst = step_seed(cfg, sec)
    plans, predicted, alloc = step_plan(cfg, inst)
    tokens = step_target(cfg, sec, inst)
    wd_proc, sentinel = step_watchdog()
    REPORT['watchdog'] = {'pid': wd_proc.pid, 'outbox_monitor': True}
    try:
        step_live_outbox_proof()
        rnd = Round(cfg, sec, inst, plans, tokens)
        rnd.start_worker(batch_size=NORMAL_BATCH, poll_seconds=NORMAL_POLL)
        for stage in stages.LADDER:
            if wd_proc.poll() is not None:
                raise RoundFailed('the watchdog exited — refusing to run a '
                                  'stage unguarded')
            rnd.run_stage(stage)
        REPORT['VERDICT'] = 'PASS'
    finally:
        try:
            REPORT['final'] = final_summary(locals().get('rnd'))
        except Exception as exc:
            REPORT['errors'].append(f'final summary: {exc}')
        try:
            sentinel.stdin.close()
            wd_proc.wait(timeout=90)
        except Exception:
            try:
                wd_proc.terminate()
            except Exception:
                pass
        for path, key in ((os.path.join(OUT, 'watchdog_summary.json'),
                           'watchdog_summary'),
                          (os.path.join(OUT, 'watchdog_config.json'),
                           'watchdog_config')):
            if os.path.exists(path):
                try:
                    REPORT[key] = json.load(open(path, encoding='utf-8'))
                except (OSError, ValueError):
                    pass
        save()


def final_summary(rnd) -> dict:
    out = {'monitor_rows': len(read_monitor()),
           'resources_whole_round': peak_resources(0)}
    if rnd is None:
        return out
    try:
        verdict, obs = rnd.reconcile(drain_complete=False)
        out['cumulative_reconciliation'] = verdict
        out['final_outbox_status_counts'] = obs['outbox_status_counts']
        out['fake_firebase'] = rnd.tally.merged()
        out['worker_reclaims_logged'] = count_reclaims(ROOT)
        out['db'] = db_activity(rnd.cfg, rnd.sec)
    except Exception as exc:
        out['error'] = f'{type(exc).__name__}: {exc}'
    # The latency question the whole exercise exists to answer.
    by_mode = collections.defaultdict(list)
    for st in REPORT['stages']:
        if st.get('latency', {}).get('n'):
            by_mode[st['worker_mode']].append((st['name'], st['latency']))
    out['latency_by_worker_mode'] = {m: dict(v) for m, v in by_mode.items()}
    probes = {st['name']: st.get('latency') for st in REPORT['stages']
              if st['name'].startswith('LP')}
    out['latency_probes_identical_shape'] = probes
    return out


if __name__ == '__main__':
    code = 0
    try:
        main()
    except RoundFailed as exc:
        REPORT['VERDICT'] = 'FAIL'
        REPORT['errors'].append(str(exc))
        say(f'\nSTAGED ROUND FAILED: {exc}')
        code = 1
    except Exception:
        REPORT['VERDICT'] = 'ERROR'
        REPORT['errors'].append(traceback.format_exc())
        say('\nSTAGED ROUND ERROR:\n' + traceback.format_exc())
        code = 2
    finally:
        try:
            cfg = common.load_config(ROOT)
            wc.stop(cfg)
            target_mod.stop(cfg)
        except Exception as exc:
            REPORT['errors'].append(f'teardown: {exc}')
        REPORT['finished_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
        save()
        say(f'\nsummary written to {os.path.join(OUT, "summary.json")}')
        say(f'VERDICT: {REPORT.get("VERDICT")}')
    sys.exit(code)

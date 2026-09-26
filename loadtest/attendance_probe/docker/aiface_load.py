"""AI Face attendance + durable outbox LOAD VALIDATION (one isolated round).

Runs INSIDE the runner container on the internal-only compose network, exactly
like docker/institute_load.py, and reuses its building blocks unchanged:
environment identity, safety gates, seed.py + institute_fixtures.py (the SAME
deterministic synthetic population the institute validation used), target.py,
worker_control.py, watchdog.py (--outbox-monitor), outbox_monitor.py and the
synthetic AI Face device in aiface_client.py.

What is new here is only what the AI Face path needs:
  * a device driver that speaks the real WebSocket protocol (reg → sendlog →
    ACK), one closed-loop session per synthetic device, many concurrently;
  * a commit-before-ACK proof from pg_xact_commit_timestamp();
  * a socket audit: any TCP peer outside the private network halts the round;
  * an exact reconciliation of attendance, outbox jobs and fake-FCM sends.

Order, stopping at the first failure:
  1  prove the network is internal; verify identity + tested commit
  2  safety gates (target + worker) and firebase import resolution
  3  seed the previously validated population; verify it structurally
  4  start target (WS on the alternate port, AI Face outbox ON), connect devices
  5  start the worker (production batch/poll/lease), watchdog + live proof
  6  composite startup gate
  7  tiny school-isolation runtime sanity check
  8  queue baseline, Stage A, reconcile, ≥70 s clean baseline, Stage B, reconcile

Writes /exp/results/aiface_load/. Counts, verdicts, latencies and timings only
— never a token string, a credential or a DSN.

VPS mode (ATTLT_AIFACE_MODE=vps, driven by vps_aiface_run.sh)
──────────────────────────────────────────────────────────────
Same round, same checks, same stage shape, with three differences forced by
the environment and nothing else:
  * it runs inside an `unshare --net` namespace (loopback only) instead of an
    internal compose network, against the PRESERVED 10-school × 1,000-student
    VPS database on 127.0.0.1:<pg_port>. Nothing is seeded: step 3 verifies the
    dataset vps_prepare_db.py already checked, migrated and gave fake tokens;
  * the 600 load students are the first 600 arrival indices (60 per school),
    i.e. the same per-school population size as the validated run;
  * reconciliation is scoped to the three test dates, and everything outside
    them (60 days of history) must be byte-for-byte unchanged, proven by an
    md5 fingerprint taken before and after. Pre-existing rows in
    notifications / push_notifications are excluded by a baseline id/count.
The watchdog is started in its own session with THIS process as its guarded
generator, so it can terminate the driver and cannot be taken down with it.
"""
import collections
import csv
import datetime as dt
import hashlib
import json
import os
import queue
import socket
import statistics
import subprocess
import sys
import threading
import time
import traceback

MODE = (os.environ.get('ATTLT_AIFACE_MODE') or 'docker').strip().lower()
if MODE not in ('docker', 'vps'):
    raise SystemExit(f'ATTLT_AIFACE_MODE={MODE!r} — must be docker or vps')
if MODE == 'vps':
    TOOL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, TOOL)
else:
    TOOL = '/tooling'
    sys.path.insert(0, '/tooling')
    os.environ['ATTLT_BIN_DIR'] = '/usr/local/bin'      # use the image interpreter

import common                          # noqa: E402
import environment_identity as ident   # noqa: E402
import guard_rules                     # noqa: E402
import institute_common as ic          # noqa: E402
import outbox_monitor as omon          # noqa: E402
import safety_gates                    # noqa: E402
import target as target_mod            # noqa: E402
import worker_control as wc            # noqa: E402
from aiface_client import AiFaceDevice, AckTimeout, DeviceDisconnected  # noqa: E402

import psutil                          # noqa: E402
import psycopg2                        # noqa: E402

if MODE == 'vps':
    ROOT = os.path.abspath(os.environ['ATTLT_AIFACE_ROOT'])
    # The TARGET interpreter: app requirements + the firebase import guard.
    # The driver itself runs under venv-gen (sys.executable).
    PY = os.path.join(ROOT, 'venv-target', 'bin', 'python')
    GENPY = sys.executable
else:
    ROOT = '/exp'
    PY = '/usr/local/bin/python'
    GENPY = PY
OUT = os.path.join(ROOT, 'results', 'aiface_load')
FAKE_FIREBASE_PREFIX = os.path.join(TOOL, 'fake_firebase') + os.sep
DONE_FILE = os.path.join(OUT, 'ROUND_COMPLETE.json')

REPORT = {'started_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'mode': MODE,
          'preflight': {}, 'stages': {}, 'errors': []}

# Pre-existing state excluded from reconciliation (VPS: preserved database).
# Docker starts from an empty database, so these stay at zero there and every
# query below reads exactly what it always read.
BASELINE = {'push_max_id': 0, 'notifications_count': 0, 'test_dates': None}

# ── Round shape ──────────────────────────────────────────────────────────────
# 12 schools are seeded (the run5 population); 10 are selected for load.
SELECTED_SCHOOLS = list(range(10))
STAGE_A_RATE = 10.5            # transitions/s, aggregate
STAGE_B_RATE = 21.0
STAGE_A_CHECKINS = 300         # students checked in on day D1 ...
STAGE_A_CHECKOUTS = 200        # ... of whom this many also check out
IDLE_BASELINE_S = 80           # >= 70 s required, > 60 s no-drain window
ACK_TIMEOUT_S = 60.0
SANITY_ENROLLID = 9001         # mis-linked mapping for the cross-school case
# VPS: the preserved dataset has 1,000 students per school. The load uses the
# first 600 arrival indices — 60 per school, the validated run's population.
VPS_SELECTED_STUDENTS = 600
# VPS watchdog limits (the requested hard limits). Docker keeps its defaults.
VPS_HOST_CPU_PCT = 80.0
VPS_DB_CONN_FRAC = 0.8

# Worker settings = the production unit (deploy/mecha-school-outbox-worker.service).
WORKER_BATCH, WORKER_POLL, WORKER_LEASE, WORKER_MAX_ATTEMPTS = 20, 5.0, 300, 5

# Structural fingerprint of the previously validated dataset, as recorded in
# run5_p2_verification_PASS/summary.json (preflight.fixtures.row_counts) for
# bootstrap --schools 12 --students 60. The reconstruction must match exactly.
RUN5_ROW_COUNTS = {
    'schools': 15, 'students': 722, 'users': 734, 'employees': 12,
    'institute_study_groups': 36, 'institute_group_enrollments': 720,
    'institute_attendance_sessions': 36, 'institute_attendance_records': 0,
    'mobile_device_tokens': 1440, 'notifications': 0,
    'notification_outbox': 0, 'parent_students': 722,
}
RUN5_INSTITUTE_TOTALS = {'groups': 36, 'enrollments': 720, 'sessions': 36,
                         'tokens': 1440, 'instructors': 12}

STOP = threading.Event()        # set on watchdog STOP or socket-audit breach
STOP_REASON = {}


class RoundFailed(SystemExit):
    pass


def say(*a):
    print(*a, flush=True)


def section(name):
    say('\n' + '═' * 72 + '\n  ' + name + '\n' + '═' * 72)


def save():
    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(OUT, '.summary.tmp')
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(REPORT, fh, indent=2, default=str, ensure_ascii=False)
    os.replace(tmp, os.path.join(OUT, 'summary.json'))


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(
        timespec='milliseconds') if ts else None


def pct(values, p):
    s = sorted(values)
    if not s:
        return None
    if len(s) == 1:
        return s[0]
    return s[min(len(s) - 1, max(0, int(round((p / 100) * (len(s) - 1)))))]


def lat_stats(values):
    if not values:
        return {'n': 0}
    return {'n': len(values), 'min_ms': round(min(values), 2),
            'p50_ms': round(pct(values, 50), 2), 'p95_ms': round(pct(values, 95), 2),
            'p99_ms': round(pct(values, 99), 2), 'max_ms': round(max(values), 2),
            'mean_ms': round(statistics.fmean(values), 2)}


def ro_conn(cfg, sec, app_name):
    conn = psycopg2.connect(**dict(common.pg_dsn(cfg, sec), application_name=app_name))
    conn.set_session(readonly=True, autocommit=True)
    return conn


# ═══════════════════════════════════════════════════════════════════════════
#  1  isolation + identity
# ═══════════════════════════════════════════════════════════════════════════

def default_route_present() -> bool:
    """True if the container has a default IPv4 route (i.e. a way off-net)."""
    with open('/proc/net/route') as fh:
        next(fh)
        for line in fh:
            f = line.split()
            if len(f) > 2 and f[1] == '00000000':
                return True
    return False


def prove_internal_network() -> dict:
    """Positive proof nothing here can leave the private network. No request is
    ever sent to a production host: the forbidden host is only resolved."""
    results, ok = {}, True
    socket.setdefaulttimeout(5)
    results['default_route_present'] = default_route_present()
    if results['default_route_present']:
        ok = False
    try:
        socket.getaddrinfo('pypi.org', 443)
        results['dns_public'] = 'RESOLVED — network is not internal'
        ok = False
    except Exception as exc:
        results['dns_public'] = f'failed as required ({type(exc).__name__})'
    forbidden = (os.environ.get('ATTLT_FORBIDDEN_HOST') or '').strip()
    if not forbidden and os.environ.get('ATTLT_FORBIDDEN_TARGETS'):
        with open(os.environ['ATTLT_FORBIDDEN_TARGETS'], encoding='utf-8') as fh:
            forbidden = (json.load(fh).get('production_db_hostname') or '').strip()
        os.environ['ATTLT_FORBIDDEN_HOST'] = forbidden     # read back by step_identity
    results['forbidden_host_supplied'] = bool(forbidden and forbidden != 'example.invalid')
    results['forbidden_host_sha256_12'] = hashlib.sha256(forbidden.encode()).hexdigest()[:12]
    results['forbidden_host_classification'] = ident.classify_db_host(forbidden)
    import ipaddress
    try:
        ipaddress.ip_address(forbidden)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False
    if is_ip_literal:
        # An IP literal "resolves" without DNS; unreachability is then proven
        # only by the TCP probes of ATTLT_FORBIDDEN_TARGETS.
        results['forbidden_host_dns'] = 'unresolvable as required (IP literal — TCP-probed instead)'
        if not os.environ.get('ATTLT_FORBIDDEN_TARGETS'):
            ok = False
    else:
        try:
            socket.getaddrinfo(forbidden, 5432)
            results['forbidden_host_dns'] = 'RESOLVED — production host is resolvable'
            ok = False
        except Exception as exc:
            results['forbidden_host_dns'] = f'unresolvable as required ({type(exc).__name__})'
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
        socket.create_connection((os.environ['ATTLT_PG_HOST'],
                                  int(os.environ.get('ATTLT_PG_PORT') or 5432)),
                                 timeout=5).close()
        results['experiment_db_reachable'] = 'yes (required)'
    except Exception as exc:
        results['experiment_db_reachable'] = f'NO — {type(exc).__name__}'
        ok = False
    # VPS: resolved addresses of production PostgreSQL/Supabase, Redis, the
    # public AI Face port and Firebase, captured OUTSIDE the namespace. Every
    # one must be unconnectable from here. Only labels and outcomes are kept.
    tfile = os.environ.get('ATTLT_FORBIDDEN_TARGETS')
    if tfile:
        with open(tfile, encoding='utf-8') as fh:
            targets = json.load(fh).get('targets', [])
        probes = []
        for t in targets:
            try:
                socket.create_connection((t['ip'], int(t['port'])), timeout=3).close()
                probes.append({'label': t['label'], 'port': t['port'], 'connected': True})
                ok = False
            except Exception as exc:
                probes.append({'label': t['label'], 'port': t['port'], 'connected': False,
                               'error': type(exc).__name__})
        results['forbidden_target_probes'] = {
            'count': len(probes), 'connected': sum(1 for p in probes if p['connected']),
            'labels': sorted({p['label'] for p in probes}),
            'errors': dict(collections.Counter(p.get('error', 'CONNECTED') for p in probes))}
        if not probes:
            results['forbidden_target_probes']['note'] = 'NO TARGETS — cannot prove unreachability'
            ok = False
    socket.setdefaulttimeout(None)
    results['internal_network_proven'] = ok
    return results


def step_identity():
    section('1  network isolation, identity, tested commit')
    proof = prove_internal_network()
    say(json.dumps(proof, indent=2))
    REPORT['preflight']['network_proof'] = proof
    if not proof['internal_network_proven']:
        raise RoundFailed('network isolation NOT proven — stopping')
    if not proof['forbidden_host_supplied']:
        raise RoundFailed('the production DB host was not supplied for the '
                          'negative resolution probe — cannot prove unreachability')

    cfg = common.load_config(ROOT)
    identity = ident.verify(ROOT, experiment_id=cfg['experiment_id'], tag=common.tag(cfg))
    expected = os.environ['ATTLT_EXPECTED_COMMIT'].strip()
    card = {
        'experiment_id': identity['experiment_id'],
        'experiment_tag': identity['experiment_tag'],
        'source_commit': identity['app_source_commit'],
        'expected_commit': expected,
        'commit_matches': identity['app_source_commit'] == expected == cfg['app_revision_full'],
        'database_host': identity['database_host'],
        'database_name': identity['database_name'],
        'database_classification': identity['database_host_classification'],
        'database_isolated': identity['database_isolated'],
        'firebase_mode': identity['firebase_mode'],
        'http_port': identity['target_http_port'],
        'ws_port': identity['target_ws_port'],
        'production_ws_port': ident.PRODUCTION_WS_PORT,
        'production_reachable': identity['production_network_reachable'],
    }
    REPORT['preflight']['identity'] = card
    say(json.dumps(card, indent=2))
    if not card['commit_matches']:
        raise RoundFailed('tested source commit does not match the expected commit')
    if card['database_classification'] not in ('container', 'loopback') or \
            card['firebase_mode'] != 'fake-local' or card['production_reachable'] or \
            card['ws_port'] == ident.PRODUCTION_WS_PORT:
        raise RoundFailed('identity card does not prove isolation')
    card['production_host_differs_from_effective_host'] = (
        os.environ.get('ATTLT_FORBIDDEN_HOST', '').strip().lower()
        != str(card['database_host']).strip().lower())
    if not card['production_host_differs_from_effective_host']:
        raise RoundFailed('effective DB host equals the production host')
    return cfg


# ═══════════════════════════════════════════════════════════════════════════
#  2  gates
# ═══════════════════════════════════════════════════════════════════════════

def target_env(cfg, sec):
    return target_mod.build_env(cfg, sec, ws_enabled=True, outbox_enabled=False,
                                aiface_outbox_enabled=True)


def worker_kw():
    return dict(batch_size=WORKER_BATCH, poll_seconds=WORKER_POLL,
                lease_seconds=WORKER_LEASE, max_attempts=WORKER_MAX_ATTEMPTS,
                fake_mode='success', aiface_outbox_enabled=True)


def forbidden_files_mounted() -> list:
    """Real credential files must not exist anywhere this container can read."""
    bad_names = {'.env', 'firebase-key.json', 'github_actions_mecha',
                 '.env.backup-before-security'}
    hits = []
    for base in (TOOL, ROOT):
        for d, _dirs, files in os.walk(base):
            for f in files:
                if f in bad_names:
                    hits.append(os.path.join(d, f))
    return hits


def firebase_resolution(env, code):
    r = subprocess.run([PY, '-c', code], env=env, capture_output=True, text=True,
                       cwd=os.path.join(ROOT, 'app_src'), timeout=60)
    return r.returncode, (r.stdout.strip().splitlines() or [''])[-1], r.stderr.strip()[-300:]


def step_gates(cfg, sec):
    section('2  safety gates — target + worker, before any row is written')
    prefixes = ic.experiment_prefixes(common.tag(cfg))
    identity = ident.load(ROOT)
    wc.write_fake_credential(ROOT)
    tenv = target_env(cfg, sec)
    wenv = wc.build_worker_env(cfg, sec, **worker_kw())
    out = {
        'target': safety_gates.run_all(env=tenv, cfg=cfg, identity=identity,
                                       prefixes=prefixes, role='target', netns_proof_ok=True),
        'worker': safety_gates.run_all(env=wenv, cfg=cfg, identity=identity,
                                       prefixes=prefixes, role='worker', netns_proof_ok=True),
    }
    from urllib.parse import urlsplit
    checks = {
        'target_AIFACE_ATTENDANCE_OUTBOX_ENABLED': tenv['AIFACE_ATTENDANCE_OUTBOX_ENABLED'],
        'target_INSTITUTE_ATTENDANCE_OUTBOX_ENABLED': tenv['INSTITUTE_ATTENDANCE_OUTBOX_ENABLED'],
        'target_AIFACE_WS_ENABLED': tenv['AIFACE_WS_ENABLED'],
        'target_AIFACE_WS_PORT': tenv['AIFACE_WS_PORT'],
        'worker_AIFACE_ATTENDANCE_OUTBOX_ENABLED': wenv['AIFACE_ATTENDANCE_OUTBOX_ENABLED'],
        'worker_AIFACE_WS_ENABLED': wenv['AIFACE_WS_ENABLED'],
        'worker_role': wenv['MECHA_PROCESS_ROLE'],
        'worker_batch_poll_lease_attempts': [wenv['OUTBOX_BATCH_SIZE'], wenv['OUTBOX_POLL_SECONDS'],
                                             wenv['OUTBOX_LEASE_SECONDS'], wenv['OUTBOX_MAX_ATTEMPTS']],
        'target_db_host': urlsplit(tenv['DATABASE_URL']).hostname,
        'target_db_name': urlsplit(tenv['DATABASE_URL']).path.lstrip('/'),
        'worker_db_host': urlsplit(wenv['DATABASE_URL']).hostname,
        'worker_db_name': urlsplit(wenv['DATABASE_URL']).path.lstrip('/'),
        'schedulers_off': {k: tenv[k] for k in safety_gates.REQUIRED_OFF},
        'credential_vars_empty': {k: (tenv.get(k, '') == '' and wenv.get(k, '') == '')
                                  for k in safety_gates.PRODUCTION_CREDENTIAL_VARS
                                  if k in tenv or k in wenv},
        'target_GOOGLE_APPLICATION_CREDENTIALS_empty': tenv['GOOGLE_APPLICATION_CREDENTIALS'] == '',
        'worker_GOOGLE_APPLICATION_CREDENTIALS_is_fake_file':
            wenv['GOOGLE_APPLICATION_CREDENTIALS'] == wc.fake_credential_path(ROOT),
        'credential_files_mounted': forbidden_files_mounted(),
        'runner_process_has_db_url': bool(os.environ.get('DATABASE_URL')),
    }
    # Import resolution under each process's EXACT environment.
    rc, line, err = firebase_resolution(tenv, 'import firebase_admin')
    checks['target_firebase_admin_import'] = ('blocked by guard' if rc != 0 and 'blocked' in err
                                              else f'NOT BLOCKED (rc={rc})')
    rc, line, err = firebase_resolution(
        wenv, 'import json, firebase_admin as f; print(json.dumps({"fake": getattr(f, "ATTLT_FAKE", False), '
              '"file": f.__file__}))')
    try:
        wf = json.loads(line)
    except ValueError:
        wf = {'fake': False, 'file': None, 'error': err}
    checks['worker_firebase_admin'] = wf
    out['checks'] = checks
    REPORT['preflight']['gates'] = out
    say(json.dumps(out, indent=2))

    problems = []
    if checks['target_AIFACE_ATTENDANCE_OUTBOX_ENABLED'] != 'true':
        problems.append('AI Face outbox flag is not on in the target')
    if checks['worker_AIFACE_ATTENDANCE_OUTBOX_ENABLED'] != 'true':
        problems.append('AI Face outbox flag is not on in the worker')
    if checks['target_INSTITUTE_ATTENDANCE_OUTBOX_ENABLED'] != 'false':
        problems.append('institute outbox flag must stay at its default in the target')
    if checks['target_AIFACE_WS_PORT'] != str(cfg['ws_port']) or cfg['ws_port'] == 7788:
        problems.append('WS port is not the isolated alternate port')
    if checks['worker_AIFACE_WS_ENABLED'] != 'false':
        problems.append('worker would bind the WS port')
    for who in ('target', 'worker'):
        if checks[f'{who}_db_host'] != cfg['pg_host'] or checks[f'{who}_db_name'] != cfg['db_name']:
            problems.append(f'{who} DATABASE_URL is not the experiment database')
    if checks['credential_files_mounted']:
        problems.append(f'credential files readable: {checks["credential_files_mounted"]}')
    if checks['runner_process_has_db_url']:
        problems.append('runner process environment carries DATABASE_URL')
    if not checks['target_firebase_admin_import'].startswith('blocked'):
        problems.append('firebase_admin is importable in the target')
    if not (wf.get('fake') is True and str(wf.get('file', '')).startswith(FAKE_FIREBASE_PREFIX)):
        problems.append('worker does not resolve the fake firebase_admin')
    if problems:
        raise RoundFailed('safety gates REFUSED: ' + '; '.join(problems))
    return checks


# ═══════════════════════════════════════════════════════════════════════════
#  3  seed — the SAME deterministic population as run5
# ═══════════════════════════════════════════════════════════════════════════

def sh(cmd, **kw):
    say('  $', ' '.join(cmd))
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        raise RoundFailed(f'command failed ({r.returncode}): {" ".join(cmd)}')
    return r


def row_counts(cfg, sec, tables) -> dict:
    conn = ro_conn(cfg, sec, 'attlt-tool')
    cur = conn.cursor()
    out = {}
    for table in tables:
        cur.execute(f'SELECT count(*) FROM {table}')
        out[table] = cur.fetchone()[0]
    conn.close()
    return out


def step_seed(cfg, sec):
    section('3  reconstruct the previously validated synthetic population')
    env = target_mod.build_env(cfg, sec, ws_enabled=False, outbox_enabled=False,
                               aiface_outbox_enabled=True)
    app_src = os.path.join(ROOT, 'app_src')
    conn = psycopg2.connect(**common.pg_dsn(cfg, sec))
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute('CREATE TABLE IF NOT EXISTS attlt_owner (experiment_id text)')
    cur.execute('SELECT count(*) FROM attlt_owner')
    if cur.fetchone()[0] == 0:
        cur.execute('INSERT INTO attlt_owner VALUES (%s)', (cfg['experiment_id'],))
    cur.execute('SHOW track_commit_timestamp')
    tct = cur.fetchone()[0]
    conn.close()
    if tct != 'on':
        raise RoundFailed('track_commit_timestamp is not on — commit-before-ACK '
                          'cannot be proven')
    t0 = time.time()
    sh([f'{os.path.dirname(PY)}/flask', 'db', 'upgrade'], cwd=app_src, env=env)
    t1 = time.time()
    sh([PY, f'{TOOL}/seed.py', '--root', ROOT, '--inside'], cwd=app_src, env=env)
    t2 = time.time()
    sh([PY, f'{TOOL}/institute_fixtures.py', '--root', ROOT, '--inside'], cwd=app_src, env=env)
    t3 = time.time()
    with open(os.path.join(ROOT, 'run', 'institute_fixtures.json'), encoding='utf-8') as fh:
        inst = json.load(fh)
    with open(os.path.join(ROOT, 'run', 'fixtures.json'), encoding='utf-8') as fh:
        fx = json.load(fh)
    counts = row_counts(cfg, sec, list(RUN5_ROW_COUNTS) + [
        'attendance_devices', 'device_student_mappings', 'student_attendance',
        'push_notifications'])
    mismatch = {k: (counts[k], v) for k, v in RUN5_ROW_COUNTS.items() if counts[k] != v}
    if inst['totals'] != RUN5_INSTITUTE_TOTALS:
        mismatch['institute_totals'] = (inst['totals'], RUN5_INSTITUTE_TOTALS)
    payload = {
        'migrate_seconds': round(t1 - t0, 1), 'seed_seconds': round(t2 - t1, 1),
        'institute_fixture_seconds': round(t3 - t2, 1),
        'config': {k: cfg[k] for k in ('num_schools', 'students_per_school',
                                       'devices_per_school', 'history_calendar_days')},
        'institute_totals': inst['totals'], 'row_counts': counts,
        'matches_run5_fingerprint': not mismatch, 'mismatch': mismatch,
        'track_commit_timestamp': tct,
    }
    REPORT['preflight']['fixtures'] = payload
    say(json.dumps(payload, indent=2))
    if mismatch:
        raise RoundFailed(f'reconstructed dataset differs from run5: {mismatch}')
    return fx, inst


PRESERVED_TABLES = ('schools', 'students', 'users', 'parent_students', 'attendance_devices',
                    'device_student_mappings', 'mobile_device_tokens', 'student_attendance',
                    'notifications', 'push_notifications', 'notification_outbox')


def history_fingerprint(cfg, sec, test_dates) -> dict:
    """count + md5 over every student_attendance row OUTSIDE the test dates.
    Taken before the target starts and again at the end: equal fingerprints
    prove the round changed nothing but its own three dates."""
    conn = ro_conn(cfg, sec, 'attlt-tool')
    cur = conn.cursor()
    cur.execute("SELECT count(*), COALESCE(max(id), 0), md5(COALESCE(string_agg("
                "concat_ws('|', id, student_id, school_id, academic_year_id, date, status, "
                "check_in, check_out, source), ',' ORDER BY id), '')) "
                "FROM student_attendance WHERE NOT (date = ANY(%s::date[]))", (list(test_dates),))
    n, mx, md5 = cur.fetchone()
    cur.execute('SELECT count(*) FROM student_attendance sa JOIN students s ON s.id = sa.student_id '
                'WHERE sa.school_id <> s.school_id')
    cross_all = cur.fetchone()[0]
    conn.close()
    return {'rows_outside_test_dates': n, 'max_id_outside_test_dates': mx, 'md5': md5,
            'cross_school_rows_whole_table': cross_all}


def step_preserved(cfg, sec):
    """VPS: nothing is seeded. vps_prepare_db.py has verified the preserved
    dataset, applied the allow-listed migrations after a verified cold backup
    and added the fake tokens; this re-reads its fingerprint and refuses to
    continue if the database has changed since."""
    section('3  preserved 10-school × 1,000-student dataset (read-only verification)')
    with open(os.path.join(ROOT, 'run', 'prepared.json'), encoding='utf-8') as fh:
        prep = json.load(fh)
    with open(os.path.join(ROOT, 'run', 'fixtures.json'), encoding='utf-8') as fh:
        fx = json.load(fh)
    conn = ro_conn(cfg, sec, 'attlt-tool')
    cur = conn.cursor()
    cur.execute('SHOW track_commit_timestamp')
    tct = cur.fetchone()[0]
    cur.execute('SELECT experiment_id FROM attlt_owner')
    owner = [r[0] for r in cur.fetchall()]
    cur.execute('SELECT version_num FROM alembic_version')
    heads = sorted(r[0] for r in cur.fetchall())
    conn.close()
    counts = row_counts(cfg, sec, PRESERVED_TABLES)
    mismatch = {k: (counts[k], v) for k, v in prep['row_counts_after_prepare'].items()
                if counts.get(k) != v}
    payload = {
        'dataset': prep['dataset'], 'test_dates': prep['test_dates'],
        'alembic_heads': heads, 'alembic_expected': prep['migrations']['after'],
        'migrations_applied': prep['migrations']['applied'],
        'tokens': prep['tokens'], 'row_counts': counts,
        'unchanged_since_prepare': not mismatch, 'mismatch': mismatch,
        'track_commit_timestamp': tct, 'owner_marker': owner,
        'backup': prep.get('backup'),
    }
    REPORT['preflight']['fixtures'] = payload
    say(json.dumps(payload, indent=2, default=str))
    if tct != 'on':
        raise RoundFailed('track_commit_timestamp is not on — commit-before-ACK cannot be proven')
    if owner != [cfg['experiment_id']]:
        raise RoundFailed('database ownership marker does not name this experiment')
    if heads != sorted(prep['migrations']['after']):
        raise RoundFailed(f'alembic head changed since prepare: {heads}')
    if mismatch:
        raise RoundFailed(f'database changed since vps_prepare_db.py: {mismatch}')
    if not prep.get('PASS'):
        raise RoundFailed('vps_prepare_db.py did not PASS')
    return fx, None


def capture_baseline(cfg, sec, dates):
    """VPS: record what already exists, BEFORE the target starts."""
    conn = ro_conn(cfg, sec, 'attlt-tool')
    cur = conn.cursor()
    cur.execute('SELECT COALESCE(max(id), 0) FROM push_notifications')
    BASELINE['push_max_id'] = cur.fetchone()[0]
    cur.execute('SELECT count(*) FROM notifications')
    BASELINE['notifications_count'] = cur.fetchone()[0]
    cur.execute('SELECT count(*) FROM notification_outbox')
    outbox = cur.fetchone()[0]
    cur.execute('SELECT count(*) FROM student_attendance WHERE date = ANY(%s::date[])', (list(dates),))
    on_test_dates = cur.fetchone()[0]
    conn.close()
    BASELINE['test_dates'] = list(dates)
    BASELINE['history_before'] = history_fingerprint(cfg, sec, dates)
    BASELINE['outbox_rows_before'] = outbox
    BASELINE['attendance_rows_on_test_dates_before'] = on_test_dates
    # The fingerprint just read ~every history page. Flush whatever that
    # dirtied now, so no checkpoint of it lands inside a load stage.
    t0 = time.time()
    ck = psycopg2.connect(**dict(common.pg_dsn(cfg, sec), application_name='attlt-tool'))
    ck.autocommit = True
    ck.cursor().execute('CHECKPOINT')
    ck.close()
    BASELINE['pre_round_checkpoint_s'] = round(time.time() - t0, 2)
    REPORT['preflight']['preexisting_baseline'] = dict(BASELINE)
    say(json.dumps(BASELINE, indent=2, default=str))
    if outbox or on_test_dates:
        raise RoundFailed(f'test dates or outbox not empty before the round: outbox={outbox} '
                          f'attendance_on_test_dates={on_test_dates}')
    if BASELINE['history_before']['cross_school_rows_whole_table']:
        raise RoundFailed('preserved dataset already contains cross-school attendance rows')


def step_app_isolation_check(cfg, sec):
    """Import the application with the target environment and report what the
    database binding and notification stack actually resolve to."""
    section('3b  application import: effective DB binding and notification stack')
    res = target_mod.isolation_check(cfg, sec)
    REPORT['preflight']['app_isolation_check'] = res
    return res


# ═══════════════════════════════════════════════════════════════════════════
#  Population view + plans
# ═══════════════════════════════════════════════════════════════════════════

class Population:
    """The selected subset of the seeded population, read from the fixtures
    and verified against the database. Nothing is created here."""

    def __init__(self, cfg, sec, fx, inst):
        self.cfg, self.sec, self.fx, self.inst = cfg, sec, fx, inst
        self.tag = common.tag(cfg)
        self.students = []                 # selected load students, by k
        for k, rec in enumerate(fx['students']):
            if rec is None:
                continue
            if MODE == 'vps' and k >= VPS_SELECTED_STUDENTS:
                continue
            lay = common.layout(cfg, k)
            if lay['school_idx'] in SELECTED_SCHOOLS:
                self.students.append({**rec, 'k': k, 'school_idx': lay['school_idx'],
                                      'device_idx': lay['device_idx']})
        self.by_sid = {s['student_db_id']: s for s in self.students}
        self.all_by_sid = {}
        for k, rec in enumerate(fx['students']):
            if rec is not None:
                self.all_by_sid[rec['student_db_id']] = {**rec, 'k': k}
        self.school_ids = sorted({fx['schools'][str(s)]['id'] for s in SELECTED_SCHOOLS})
        self.all_school_ids = sorted(v['id'] for v in fx['schools'].values())
        self.year_of_school = {v['id']: v['year_id'] for v in fx['schools'].values()}
        self.devices = {sn: d for sn, d in fx['devices'].items()
                        if d['school_idx'] in SELECTED_SCHOOLS}
        self._load_db()

    def _load_db(self):
        conn = ro_conn(self.cfg, self.sec, 'attlt-tool')
        cur = conn.cursor()
        cur.execute('SELECT user_id, student_id FROM parent_students')
        self.parents_of = collections.defaultdict(list)
        for uid, sid in cur.fetchall():
            self.parents_of[sid].append(uid)
        cur.execute('SELECT id, user_id, school_id, is_active, fcm_token FROM mobile_device_tokens')
        self.tokens = {}
        for tid, uid, sch, active, tok in cur.fetchall():
            self.tokens[tid] = {'user_id': uid, 'school_id': sch, 'active': bool(active),
                                'fp': hashlib.sha256((self.cfg['experiment_id'] + '|' + tok)
                                                     .encode('utf-8')).hexdigest()[:16],
                                'tagged': ic.is_experiment_token(self.tag, tok)}
        cur.execute('SELECT id, school_id FROM users')
        self.user_school = dict(cur.fetchall())
        cur.execute('SELECT id, school_id FROM students')
        self.student_school = dict(cur.fetchall())
        cur.execute('SELECT id, school_id, device_sn FROM attendance_devices')
        self.device_rows = {sn: {'id': i, 'school_id': s} for i, s, sn in cur.fetchall()}
        cur.execute('SELECT device_id, employee_no_string, student_id, school_id, is_active '
                    'FROM device_student_mappings')
        self.mappings = {(d, e): {'student_id': s, 'school_id': sc, 'active': a}
                         for d, e, s, sc, a in cur.fetchall()}
        conn.close()

    def active_tokens(self, user_id, school_id):
        return [tid for tid, t in self.tokens.items()
                if t['user_id'] == user_id and t['school_id'] == school_id and t['active']]

    def verify(self) -> dict:
        """Every selected student maps through its OWN school's device."""
        bad = []
        for s in self.students:
            dev = self.device_rows.get(s['device_sn'])
            m = self.mappings.get((dev['id'] if dev else None, str(s['enrollid'])))
            if not dev or dev['school_id'] != s['school_id'] or not m or \
                    m['student_id'] != s['student_db_id'] or not m['active'] or \
                    self.student_school.get(s['student_db_id']) != s['school_id']:
                bad.append(s['k'])
        per_parent_tokens = collections.Counter(
            len(self.active_tokens(p, s['school_id']))
            for s in self.students for p in self.parents_of[s['student_db_id']])
        parents_per_student = collections.Counter(len(self.parents_of[s['student_db_id']])
                                                  for s in self.students)
        return {
            'selected_schools': len(SELECTED_SCHOOLS),
            'selected_school_indices': SELECTED_SCHOOLS,
            'seeded_load_schools': self.cfg['num_schools'],
            'selected_students': len(self.students),
            'selected_devices': len(self.devices),
            'students_per_selected_school': dict(collections.Counter(
                s['school_idx'] for s in self.students)),
            'parents_per_student_histogram': dict(parents_per_student),
            'active_tokens_per_parent_histogram': dict(per_parent_tokens),
            'all_tokens_tagged': all(t['tagged'] for t in self.tokens.values()),
            'mapping_violations': bad,
        }

    def jobs_for(self, sid, on_date: str, action: str) -> set:
        """Expected dedup keys — mirrors notification_outbox._scan_dedup_key and
        stage_scan_deliveries(): each linked parent × each ACTIVE token of that
        parent registered in the STUDENT'S school."""
        school = self.student_school[sid]
        ymd = on_date.replace('-', '')
        keys = set()
        for uid in self.parents_of.get(sid, ()):
            for tid in self.active_tokens(uid, school):
                keys.add(f'school_attendance_scan:{sid}:{ymd}:{action}:{uid}:{tid}')
        return keys


def local_today(cfg):
    import pytz
    return dt.datetime.now(pytz.timezone(cfg['school_timezone'])).date()


def checkout_time_for(k: int) -> dt.time:
    """Synthetic afternoon departure scan, 13:00:00–14:59:59 (>= departure)."""
    sec = (k * 7919) % 7200
    return (dt.datetime(2000, 1, 1, 13, 0, 0) + dt.timedelta(seconds=sec)).time()


def ev(phase, stu, on_date, t, action, *, device_sn=None, enrollid=None):
    return {'phase': phase, 'k': stu['k'], 'school_idx': stu.get('school_idx'),
            'school_id': stu['school_id'], 'student_id': stu['student_db_id'],
            'device_sn': device_sn or stu['device_sn'],
            'enrollid': enrollid if enrollid is not None else stu['enrollid'],
            'date': on_date.isoformat(), 'time': t.strftime('%H:%M:%S'), 'action': action}


def build_plans(pop, cfg):
    today = local_today(cfg)
    d0, d1, d2 = today - dt.timedelta(days=2), today - dt.timedelta(days=1), today
    studs = sorted(pop.students, key=lambda s: s['k'])   # interleaved across schools
    a_set = studs[:STAGE_A_CHECKINS]
    ci = [ev('A', s, d1, common.device_time_for(s['k']), 'check_in') for s in a_set]
    co = [ev('A', s, d1, checkout_time_for(s['k']), 'check_out')
          for s in a_set[:STAGE_A_CHECKOUTS]]
    # CI[0:100], then CI[100:] interleaved with CO[0:]: every check-out is
    # scheduled >= ~100 events after its own check-in on the same device.
    stage_a = ci[:100]
    for x, y in zip(ci[100:], co):
        stage_a += [x, y]
    stage_b = [ev('B', s, d2, common.device_time_for(s['k']), 'check_in') for s in studs]
    for i, e in enumerate(stage_a):
        e['seq'] = i
    for i, e in enumerate(stage_b):
        e['seq'] = i
    # ordering proof for Stage A: check-out after check-in, same device
    pos = {(e['student_id'], e['action']): i for i, e in enumerate(stage_a)}
    order_ok = all(pos[(sid, 'check_in')] < i for (sid, act), i in pos.items()
                   if act == 'check_out')
    same_device = all(e['device_sn'] == pop.by_sid[e['student_id']]['device_sn'] for e in stage_a + stage_b)
    keys = [(e['student_id'], e['date'], e['action']) for e in stage_a + stage_b]
    return {'dates': {'sanity': d0.isoformat(), 'A': d1.isoformat(), 'B': d2.isoformat()},
            'A': stage_a, 'B': stage_b,
            'checks': {'stage_a_events': len(stage_a), 'stage_b_events': len(stage_b),
                       'checkout_after_checkin_on_same_device': order_ok and same_device,
                       'transitions_unique': len(keys) == len(set(keys)),
                       'schools_in_A': len({e['school_id'] for e in stage_a}),
                       'schools_in_B': len({e['school_id'] for e in stage_b}),
                       'devices_in_A': len({e['device_sn'] for e in stage_a}),
                       'devices_in_B': len({e['device_sn'] for e in stage_b})}}


# ═══════════════════════════════════════════════════════════════════════════
#  Background instruments
# ═══════════════════════════════════════════════════════════════════════════

class Instruments:
    """Live stats for the watchdog, commit-before-ACK verifier, socket audit,
    worker process sampler, DB connection breakdown and the fine backlog curve.
    """

    def __init__(self, cfg, sec, pop, devices):
        self.cfg, self.sec, self.pop, self.devices = cfg, sec, pop, devices
        self.lock = threading.Lock()
        self.acks = collections.deque()          # (wall, ms, ok)
        self.inflight = 0
        self.due_unsent = 0
        self.ackq = queue.Queue()
        self.ack_proofs = []
        self.sock_peers = collections.Counter()
        self.sock_violations = []
        self.worker_rows = []
        self.db_rows = []
        self.curve = []
        self.curve_on = threading.Event()
        self._stop = threading.Event()
        # VPS: the live snapshot is the watchdog's liveness signal for THIS
        # process. It keeps being written through reconciliation and teardown
        # (until the process exits), so a slow teardown never reads as a hung
        # generator. Docker stops it with everything else, as before.
        self._live_stop = threading.Event()
        self.t0 = time.time()
        self.allowed_ips = self._allowed_ips()

    def _allowed_ips(self):
        ips = {'127.0.0.1', '::1', '0.0.0.0', '::'}
        db_ip = socket.gethostbyname(self.cfg['pg_host'])
        ips.add(db_ip)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((db_ip, 5432))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
        return ips

    def start(self):
        for fn in (self._live_loop, self._ack_verify_loop, self._socket_loop,
                   self._worker_loop, self._db_loop, self._curve_loop, self._stop_watch_loop):
            threading.Thread(target=fn, daemon=True, name=fn.__name__).start()

    def shutdown(self):
        self._stop.set()
        if MODE != 'vps':
            self._live_stop.set()

    # ── watchdog feed ────────────────────────────────────────────────────
    def record_ack(self, wall, ms, ok):
        with self.lock:
            self.acks.append((wall, ms, ok))

    def _live_loop(self):
        path = os.path.join(OUT, 'live_p0.json')
        while not self._live_stop.is_set():
            now = time.time()
            with self.lock:
                while self.acks and now - self.acks[0][0] > 20:
                    self.acks.popleft()
                win = list(self.acks)
                snap = {'rel': round(now - self.t0, 2), 'mode': 'aiface-outbox',
                        'ops_window': {'ws_sendlog_ack': {
                            'n': len(win), 'errors': sum(1 for w in win if not w[2]),
                            'p95_ms': pct([w[1] for w in win if w[2]], 95)}},
                        'violations': len(self.sock_violations),
                        'unexpected_device_commands': sum(
                            d.stats['unexpected_commands'] for d in self.devices.values()),
                        'pending_acks': self.inflight, 'event_backlog': self.due_unsent,
                        'active_parents': 0}
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump(snap, fh)
            os.replace(tmp, path)
            self._live_stop.wait(2.0)

    def _stop_watch_loop(self):
        path = os.path.join(OUT, 'STOP.json')
        while not self._stop.is_set():
            if os.path.exists(path) and not STOP.is_set():
                try:
                    STOP_REASON['watchdog'] = json.load(open(path, encoding='utf-8'))
                except (OSError, ValueError):
                    STOP_REASON['watchdog'] = {'mode': 'halt', 'reason': 'STOP.json unreadable'}
                STOP.set()
            self._stop.wait(0.5)

    # ── commit-before-ACK ────────────────────────────────────────────────
    def _ack_verify_loop(self):
        conn = ro_conn(self.cfg, self.sec, 'attlt-ackverify')
        cur = conn.cursor()
        while not self._stop.is_set():
            try:
                item = self.ackq.get(timeout=0.5)
            except queue.Empty:
                continue
            cur.execute("SELECT check_in, check_out, "
                        "EXTRACT(EPOCH FROM pg_xact_commit_timestamp(xmin))::float8 "
                        "FROM student_attendance WHERE student_id=%s AND date=%s",
                        (item['student_id'], item['date']))
            row = cur.fetchone()
            res = {'phase': item['phase'], 'seq': item['seq'], 'action': item['action'],
                   'ack_wall': item['ack_wall']}
            if row is None:
                res['state'] = 'no_row'
            else:
                ci, co, cts = row
                superseded = item['action'] == 'check_in' and co is not None
                res['state'] = 'superseded' if superseded else 'verified'
                res['commit_ts'] = cts
                res['margin_ms'] = round((item['ack_wall'] - cts) * 1000, 3) if cts else None
            with self.lock:
                self.ack_proofs.append(res)
        conn.close()

    # ── socket audit ─────────────────────────────────────────────────────
    @staticmethod
    def _peers():
        out = []
        for path, v6 in (('/proc/net/tcp', False), ('/proc/net/tcp6', True)):
            try:
                with open(path) as fh:
                    next(fh)
                    for line in fh:
                        f = line.split()
                        state = f[3]
                        if state == '0A':          # LISTEN
                            continue
                        rip, rport = f[2].split(':')
                        out.append((_hex_ip(rip, v6), int(rport, 16)))
            except OSError:
                pass
        return out

    def _socket_loop(self):
        while not self._stop.is_set():
            for ip, port in self._peers():
                key = f'{ip}:{port}' if port in (5432, 18180, 18188) else f'{ip}:*'
                self.sock_peers[key] += 1
                norm = ip[7:] if ip.startswith('::ffff:') else ip
                if norm not in self.allowed_ips:
                    v = {'at': utc_now().isoformat(), 'remote_ip': norm, 'remote_port': port}
                    with self.lock:
                        self.sock_violations.append(v)
                    STOP_REASON['socket_audit'] = v
                    STOP.set()
            self._stop.wait(1.0)

    # ── worker process + DB breakdown ────────────────────────────────────
    def _worker_loop(self):
        cache = {}
        while not self._stop.is_set():
            st = omon.read_worker_state(ROOT)
            row = {'wall_utc': utc_now().isoformat(), 'pid': st.get('pid'), 'alive': st.get('alive')}
            try:
                if st.get('alive'):
                    p = cache.get(st['pid'])
                    if p is None:
                        p = cache[st['pid']] = psutil.Process(st['pid'])
                        p.cpu_percent(None)
                    row['cpu_pct'] = p.cpu_percent(None)
                    row['rss_mb'] = round(p.memory_info().rss / 2**20, 1)
                    row['threads'] = p.num_threads()
            except psutil.Error:
                pass
            self.worker_rows.append(row)
            self._stop.wait(2.0)

    def _db_loop(self):
        conn = ro_conn(self.cfg, self.sec, 'attlt-dbsampler')
        cur = conn.cursor()
        while not self._stop.is_set():
            try:
                cur.execute("SELECT COALESCE(application_name,''), state, wait_event_type "
                            "FROM pg_stat_activity WHERE datname=%s", (self.cfg['db_name'],))
                rows = cur.fetchall()
                app = [r for r in rows if not r[0].startswith('attlt-')]
                cur.execute('SELECT deadlocks, xact_commit, xact_rollback FROM pg_stat_database '
                            'WHERE datname=%s', (self.cfg['db_name'],))
                dl, xc, xr = cur.fetchone()
                self.db_rows.append({
                    'wall_utc': utc_now().isoformat(), 'all_connections': len(rows),
                    'app_connections': len(app),
                    'app_active': sum(1 for r in app if r[1] == 'active'),
                    'lock_waits': sum(1 for r in rows if r[2] == 'Lock'),
                    'deadlocks': dl, 'xact_commit': xc, 'xact_rollback': xr})
            except Exception as exc:
                self.db_rows.append({'wall_utc': utc_now().isoformat(),
                                     'error': type(exc).__name__})
            self._stop.wait(2.0)
        conn.close()

    # ── fine-grained backlog curve (0.5 s) ───────────────────────────────
    def _curve_loop(self):
        conn = ro_conn(self.cfg, self.sec, 'attlt-sampler')
        cur = conn.cursor()
        while not self._stop.is_set():
            if not self.curve_on.is_set():
                self._stop.wait(0.2)
                continue
            try:
                cur.execute(omon.COUNTS_SQL, (self.pop.all_school_ids,))
                c = omon.derive({r[0]: r[1] for r in cur.fetchall()})
                self.curve.append({'wall': time.time(), 'backlog': c['outbox_backlog'],
                                   **{s: c[s] for s in omon.STATUSES}})
            except Exception as exc:
                self.curve.append({'wall': time.time(), 'error': type(exc).__name__})
            self._stop.wait(0.5)
        conn.close()


def _hex_ip(h, v6):
    if not v6:
        return socket.inet_ntop(socket.AF_INET, bytes.fromhex(h)[::-1])
    b = bytes.fromhex(h)
    b = b''.join(b[i:i + 4][::-1] for i in range(0, 16, 4))
    return socket.inet_ntop(socket.AF_INET6, b)


def listening_ports() -> set:
    ports = set()
    for path in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            with open(path) as fh:
                next(fh)
                for line in fh:
                    f = line.split()
                    if f[3] == '0A':
                        ports.add(int(f[1].split(':')[1], 16))
        except OSError:
            pass
    return ports


# ═══════════════════════════════════════════════════════════════════════════
#  Device driver
# ═══════════════════════════════════════════════════════════════════════════

def connect_devices(cfg, sns) -> dict:
    url = f"ws://127.0.0.1:{cfg['ws_port']}/"
    devs, reg = {}, {}
    for i, sn in enumerate(sorted(sns)):
        d = AiFaceDevice(url, sn, logindex_start=100000 * (i + 1))
        reg[sn] = round(d.connect(timeout=20) * 1000, 2)
        devs[sn] = d
    return devs, reg


def drive(events, devices, inst, *, rate, ledger_path):
    """Send `events` at `rate`/s aggregate. One thread per device: each waits
    for its event's scheduled instant, sends ONE record in ONE sendlog frame,
    and waits for the ACK before its next frame — exactly how firmware behaves.
    Concurrency comes from many device sessions, never from pipelining."""
    start = time.time() + 1.0
    per_dev = collections.defaultdict(list)
    for i, e in enumerate(events):
        e['due_wall'] = start + i / rate
        per_dev[e['device_sn']].append(e)

    def run(sn, evs):
        dev = devices[sn]
        for e in evs:
            if STOP.is_set():
                e['error'] = 'not_sent_stop'
                continue
            delay = e['due_wall'] - time.time()
            if delay > 0:
                time.sleep(delay)
            if STOP.is_set():
                e['error'] = 'not_sent_stop'
                continue
            rec = AiFaceDevice.record(e['enrollid'], e['date'], e['time'])
            with inst.lock:
                inst.inflight += 1
            e['sent_wall'] = time.time()
            e['lateness_ms'] = round((e['sent_wall'] - e['due_wall']) * 1000, 2)
            try:
                slot = dev.begin_sendlog([rec])
                dev.wait_ack(slot, ACK_TIMEOUT_S)
                e['ack_wall'] = time.time() - (time.perf_counter() - slot['ack_t'])
                e['ack_ms'] = round((slot['ack_t'] - slot['sent_t']) * 1000, 3)
                e['ack_ok'] = True
            except (AckTimeout, DeviceDisconnected, RuntimeError) as exc:
                e['ack_ok'] = False
                e['error'] = f'{type(exc).__name__}: {str(exc)[:120]}'
            finally:
                with inst.lock:
                    inst.inflight -= 1
            inst.record_ack(e.get('ack_wall') or time.time(), e.get('ack_ms') or 0.0,
                            e.get('ack_ok', False))
            if e.get('ack_ok'):
                inst.ackq.put({k: e[k] for k in ('phase', 'seq', 'student_id', 'date',
                                                 'action', 'ack_wall')})

    def backlog_meter():
        while not done.is_set():
            now = time.time()
            n = sum(1 for e in events if e['due_wall'] <= now and 'sent_wall' not in e
                    and 'error' not in e)
            with inst.lock:
                inst.due_unsent = n
            done.wait(0.5)
        with inst.lock:
            inst.due_unsent = 0

    done = threading.Event()
    threading.Thread(target=backlog_meter, daemon=True).start()
    threads = [threading.Thread(target=run, args=(sn, evs), daemon=True)
               for sn, evs in per_dev.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    done.set()
    with open(ledger_path, 'a', encoding='utf-8') as fh:
        for e in events:
            fh.write(json.dumps({k: v for k, v in e.items()}, ensure_ascii=False) + '\n')
    sent = [e for e in events if 'sent_wall' in e]
    ok = [e for e in sent if e.get('ack_ok')]
    first = min((e['sent_wall'] for e in sent), default=None)
    last_sent = max((e['sent_wall'] for e in sent), default=None)
    last_ack = max((e['ack_wall'] for e in ok), default=None)
    span = (last_sent - first) if sent and last_sent > first else None
    return {
        'intended': len(events), 'sent': len(sent), 'acked_ok': len(ok),
        'ack_failures': len(sent) - len(ok),
        'not_sent_due_to_stop': sum(1 for e in events if e.get('error') == 'not_sent_stop'),
        'errors': dict(collections.Counter(e['error'].split(':')[0] for e in events
                                           if e.get('error'))),
        'first_sent_utc': iso(first), 'last_sent_utc': iso(last_sent),
        'last_ack_utc': iso(last_ack),
        'send_window_s': round(span, 3) if span else None,
        'achieved_transitions_per_s_send_window':
            round((len(ok) - 1) / span, 3) if span else None,
        'achieved_transitions_per_s_until_last_ack':
            round(len(ok) / (last_ack - first), 3) if ok and last_ack > first else None,
        'schedule_lateness_ms': lat_stats([e['lateness_ms'] for e in sent]),
        'ack_latency': lat_stats([e['ack_ms'] for e in ok]),
        'devices_used': len(per_dev),
        'schools_used': len({e['school_id'] for e in events}),
        'max_concurrent_device_sessions': len(per_dev),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  Reconciliation
# ═══════════════════════════════════════════════════════════════════════════

def fetch_state(cfg, sec):
    conn = ro_conn(cfg, sec, 'attlt-reconcile')
    cur = conn.cursor()
    att_sql = ('SELECT sa.id, sa.student_id, sa.school_id, sa.academic_year_id, sa.date, '
               'sa.status, sa.check_in, sa.check_out, sa.source, s.school_id '
               'FROM student_attendance sa JOIN students s ON s.id = sa.student_id')
    if BASELINE['test_dates']:
        # VPS: only the round's own dates; history is proven unchanged separately.
        cur.execute(att_sql + ' WHERE sa.date = ANY(%s::date[])', (BASELINE['test_dates'],))
    else:
        cur.execute(att_sql)
    att = cur.fetchall()
    cur.execute('SELECT id, school_id, event_type, user_id, device_token_id, dedup_key, status, '
                'attempts, created_at, completed_at FROM notification_outbox')
    jobs = cur.fetchall()
    cur.execute('SELECT count(*) FROM notifications')
    notifications = cur.fetchone()[0] - BASELINE['notifications_count']
    cur.execute('SELECT p.status, p.school_id, u.school_id FROM push_notifications p '
                'JOIN users u ON u.id = p.user_id WHERE p.id > %s', (BASELINE['push_max_id'],))
    push = cur.fetchall()
    conn.close()
    return att, jobs, notifications, push


def read_fake_ledger():
    path = os.path.join(ROOT, 'run', 'fake_fcm_sends.json')
    if not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def reconcile(cfg, sec, pop, events, *, drain_complete, rejected=()):
    """Exact comparison of everything that was SENT against the database and
    the fake Firebase ledger. `events` is the cumulative ledger (sanity + stages).
    `rejected` are cross-school events that must have produced NOTHING."""
    att, jobs, notifications, push = fetch_state(cfg, sec)
    sent = [e for e in events if 'sent_wall' in e]
    # expected attendance per (student, date)
    exp_att = {}
    for e in sent:
        slot = exp_att.setdefault((e['student_id'], e['date']),
                                  {'check_in': None, 'check_out': None, 'school_id': e['school_id']})
        slot[e['action']] = e['time']
    rows = collections.defaultdict(list)
    for (rid, sid, sch, yr, d, status, ci, co, src, stu_sch) in att:
        rows[(sid, d.isoformat())].append({'school_id': sch, 'year': yr, 'status': status,
                                           'check_in': ci.strftime('%H:%M:%S') if ci else None,
                                           'check_out': co.strftime('%H:%M:%S') if co else None,
                                           'source': src, 'student_school': stu_sch})
    missing, wrong_value, wrong_status, wrong_year, wrong_source = [], [], [], [], []
    for key, want in exp_att.items():
        got = rows.get(key)
        if not got:
            missing.append(key)
            continue
        r = got[0]
        if r['check_in'] != want['check_in'] or r['check_out'] != want['check_out']:
            wrong_value.append(key)
        want_status = (common.expected_checkin_status(cfg, dt.time.fromisoformat(want['check_in']))
                       if want['check_in'] else None)
        if r['status'] != want_status:
            wrong_status.append(key)
        if r['year'] != pop.year_of_school.get(r['school_id']):
            wrong_year.append(key)
        if r['source'] != 'aiface':
            wrong_source.append(key)
    unexpected = [k for k in rows if k not in exp_att]
    logical_dups = sum(len(v) - 1 for v in rows.values() if len(v) > 1)
    cross_att = sum(1 for v in rows.values() for r in v if r['school_id'] != r['student_school'])
    foreign = [k for k, v in rows.items()
               if k in exp_att and v[0]['school_id'] != exp_att[k]['school_id']]
    rejected_rows = [(e['target_student_id'], e['date']) for e in rejected
                     if (e['target_student_id'], e['date']) in rows]

    # expected jobs
    exp_keys = set()
    for e in sent:
        exp_keys |= pop.jobs_for(e['student_id'], e['date'], e['action'])
    act = {}
    for (jid, sch, etype, uid, tid, key, status, attempts, created, completed) in jobs:
        act[key] = {'id': jid, 'school_id': sch, 'event_type': etype, 'user_id': uid,
                    'token_id': tid, 'status': status, 'attempts': attempts,
                    'created_at': created, 'completed_at': completed}
    missing_jobs = exp_keys - set(act)
    orphan_jobs = set(act) - exp_keys
    cross_jobs = []
    for key, j in act.items():
        parts = key.split(':')
        try:
            sid, uid, tid = int(parts[1]), int(parts[4]), int(parts[5])
        except (IndexError, ValueError):
            cross_jobs.append(j['id'])
            continue
        tok = pop.tokens.get(j['token_id'], {})
        if (pop.student_school.get(sid) != j['school_id']
                or pop.user_school.get(j['user_id']) != j['school_id']
                or tok.get('school_id') != j['school_id'] or tok.get('user_id') != j['user_id']
                or uid != j['user_id'] or tid != j['token_id']
                or j['user_id'] not in pop.parents_of.get(sid, ())):
            cross_jobs.append(j['id'])
    rejected_jobs = [k for k in act for e in rejected
                     if k.startswith(f"school_attendance_scan:{e['target_student_id']}:"
                                     f"{e['date'].replace('-', '')}:")]
    status_counts = collections.Counter(j['status'] for j in act.values())
    event_types = collections.Counter(j['event_type'] for j in act.values())
    max_attempts = max((j['attempts'] for j in act.values()), default=0)

    # fake Firebase, per token fingerprint
    exp_fp = collections.Counter()
    for key in exp_keys:
        exp_fp[pop.tokens[int(key.split(':')[5])]['fp']] += 1
    fake = read_fake_ledger()
    got_fp = collections.Counter(fake.get('fingerprint_counts') or {})
    explained_dups = sum(min(got_fp[fp], n) - 1 for fp, n in exp_fp.items()
                         if n > 1 and got_fp[fp] > 1)
    unexplained_dups = sum(max(0, got_fp[fp] - exp_fp.get(fp, 0)) for fp in got_fp)
    missing_sends = sum(max(0, n - got_fp.get(fp, 0)) for fp, n in exp_fp.items())
    unknown_fps = [fp for fp in got_fp if fp not in exp_fp]
    rejected_fp_extra = 0
    for e in rejected:
        for uid in pop.parents_of.get(e['target_student_id'], ()):
            for tid in pop.active_tokens(uid, pop.student_school[e['target_student_id']]):
                fp = pop.tokens[tid]['fp']
                rejected_fp_extra += max(0, got_fp.get(fp, 0) - exp_fp.get(fp, 0))

    push_cross = sum(1 for (_s, psch, usch) in push if psch != usch)
    push_by_status = collections.Counter(p[0] for p in push)

    out = {
        'attendance': {
            'expected_transitions': len(sent),
            'expected_rows': len(exp_att), 'committed_rows': sum(len(v) for v in rows.values()),
            'missing_attendance': len(missing), 'wrong_check_times': len(wrong_value),
            'wrong_status': len(wrong_status), 'wrong_academic_year': len(wrong_year),
            'wrong_source': len(wrong_source), 'unexpected_attendance': len(unexpected),
            'logical_duplicates': logical_dups, 'cross_school_attendance': cross_att,
            'foreign_school_mutation': len(foreign) + len(rejected_rows),
            'rejected_event_rows': len(rejected_rows),
        },
        'outbox': {
            'expected_jobs': len(exp_keys), 'actual_jobs': len(act),
            'distinct_dedup_keys': len(act), 'missing_jobs': len(missing_jobs),
            'orphan_jobs': len(orphan_jobs), 'cross_school_jobs': len(cross_jobs),
            'rejected_event_jobs': len(rejected_jobs),
            'event_types': dict(event_types), 'max_attempts': max_attempts,
            **{s: status_counts.get(s, 0) for s in omon.STATUSES},
            'backlog': sum(status_counts.get(s, 0) for s in omon.BACKLOG_STATUSES),
        },
        'fake_firebase': {
            'attempts': fake.get('attempts', 0), 'successes': fake.get('successes', 0),
            'failures': fake.get('failures', 0), 'mode': fake.get('mode'),
            'experiment_id_matches': fake.get('experiment_id') == cfg['experiment_id'],
            'distinct_fingerprints': len(got_fp), 'expected_distinct_tokens': len(exp_fp),
            'explained_duplicate_deliveries': explained_dups,
            'explained_by': 'the same parent device legitimately receives one push per '
                            'transition (check-in, check-out, and each date)',
            'unexplained_duplicate_deliveries': unexplained_dups,
            'missing_deliveries': missing_sends, 'unknown_fingerprints': len(unknown_fps),
            'sends_attributable_to_rejected_event': rejected_fp_extra,
        },
        'in_app_notifications_rows': notifications,
        'push_notification_log': {'rows': len(push), 'by_status': dict(push_by_status),
                                  'cross_school_rows': push_cross},
    }
    A, O, F = out['attendance'], out['outbox'], out['fake_firebase']
    v = []
    for k in ('missing_attendance', 'wrong_check_times', 'wrong_status', 'wrong_academic_year',
              'wrong_source', 'unexpected_attendance', 'logical_duplicates',
              'cross_school_attendance', 'foreign_school_mutation'):
        if A[k]:
            v.append(f'attendance.{k}={A[k]}')
    for k in ('missing_jobs', 'orphan_jobs', 'cross_school_jobs', 'rejected_event_jobs',
              'dead', 'cancelled'):
        if O[k]:
            v.append(f'outbox.{k}={O[k]}')
    if O['actual_jobs'] != O['expected_jobs']:
        v.append('outbox job count != expected')
    if set(O['event_types']) - {'school_attendance_scan'}:
        v.append(f'unexpected outbox event types {O["event_types"]}')
    if notifications != 0:
        v.append(f'in-app Notification rows created: {notifications}')
    if push_cross:
        v.append(f'push log cross-school rows={push_cross}')
    if F['unexplained_duplicate_deliveries'] or F['unknown_fingerprints'] or \
            F['sends_attributable_to_rejected_event'] or F['failures']:
        v.append('fake firebase ledger shows unexplained/unknown/failed sends')
    if drain_complete:
        if O['backlog']:
            v.append(f'backlog not zero after drain: {O["backlog"]}')
        if O['sent'] != O['expected_jobs'] or F['missing_deliveries'] or \
                F['attempts'] != O['expected_jobs'] or F['successes'] != O['expected_jobs']:
            v.append('deliveries do not match expected jobs')
        if push_by_status.get('sent', 0) != O['sent']:
            v.append('push log sent rows != sent jobs')
    if BASELINE['test_dates']:
        # VPS: everything outside the three test dates must be untouched.
        hist = history_fingerprint(cfg, sec, BASELINE['test_dates'])
        before = BASELINE['history_before']
        out['history_outside_test_dates'] = {
            'rows_before': before['rows_outside_test_dates'],
            'rows_now': hist['rows_outside_test_dates'],
            'md5_unchanged': hist['md5'] == before['md5'],
            'cross_school_rows_whole_table': hist['cross_school_rows_whole_table']}
        if hist['md5'] != before['md5'] or \
                hist['rows_outside_test_dates'] != before['rows_outside_test_dates']:
            v.append('attendance OUTSIDE the test dates changed during the round')
        if hist['cross_school_rows_whole_table']:
            v.append(f"cross-school attendance rows in the whole table: "
                     f"{hist['cross_school_rows_whole_table']}")
    out['violations'] = v
    out['correct'] = not v
    out['drain_complete'] = drain_complete
    out['_jobs'] = act       # internal; stripped before reporting
    return out


def public(recon):
    return {k: v for k, v in recon.items() if not k.startswith('_')}


# ═══════════════════════════════════════════════════════════════════════════
#  Watchdog + monitor helpers (same contract as institute_load.py)
# ═══════════════════════════════════════════════════════════════════════════

def read_monitor() -> list:
    path = os.path.join(OUT, 'monitor.csv')
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline='', encoding='utf-8') as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def monitor_between(t_from, t_to):
    out = []
    for r in read_monitor():
        try:
            w = dt.datetime.fromisoformat(r['wall_utc'].replace('Z', '+00:00')).timestamp()
        except (KeyError, ValueError):
            continue
        if t_from <= w <= t_to:
            out.append(r)
    return out


def resources(rows) -> dict:
    def vals(key, cast=float):
        return [cast(r[key]) for r in rows if r.get(key) not in (None, '', 'None')]

    def mx(key, cast=float):
        v = vals(key, cast)
        return max(v) if v else None

    def mn(key, cast=float):
        v = vals(key, cast)
        return min(v) if v else None
    return {
        'monitor_samples': len(rows),
        'host_cpu_pct_max': mx('host_cpu_pct'),
        'host_mem_available_pct_min': mn('host_mem_available_pct'),
        'host_mem_available_gb_min': mn('host_mem_available_gb'),
        'target_cpu_pct_max': mx('target_cpu_pct'), 'target_rss_mb_max': mx('target_rss_mb'),
        'target_threads_max': mx('target_threads'),
        'db_connections_max_incl_harness': mx('db_connections', int),
        'db_active_max': mx('db_active', int), 'db_lock_waits_max': mx('db_lock_waits', int),
        'db_longest_active_s_max': mx('db_longest_active_s'),
        'outbox_backlog_max_2s_sampling': mx('outbox_backlog', int),
        'outbox_dead_max': mx('outbox_dead', int), 'outbox_cancelled_max': mx('outbox_cancelled', int),
        'outbox_unplanned_restarts_max': mx('outbox_unplanned_worker_restarts', int),
        'collector_failures': sum(1 for r in rows
                                  if str(r.get('outbox_collector_ok')).lower() not in ('true', '1')),
        'log_traceback_max': mx('log_traceback', int),
        'log_pool_timeout_max': mx('log_pool_timeout', int),
        'log_db_operational_error_max': mx('log_db_operational_error', int),
    }


def window_rows(rows, t_from, t_to):
    out = []
    for r in rows:
        try:
            w = dt.datetime.fromisoformat(r['wall_utc']).timestamp()
        except (KeyError, ValueError):
            continue
        if t_from <= w <= t_to:
            out.append(r)
    return out


def worker_resources(inst, t_from, t_to):
    rows = window_rows(inst.worker_rows, t_from, t_to)
    cpu = [r['cpu_pct'] for r in rows if 'cpu_pct' in r]
    rss = [r['rss_mb'] for r in rows if 'rss_mb' in r]
    return {'samples': len(rows), 'worker_cpu_pct_max': max(cpu) if cpu else None,
            'worker_cpu_pct_mean': round(statistics.fmean(cpu), 1) if cpu else None,
            'worker_rss_mb_max': max(rss) if rss else None,
            'worker_pids_seen': sorted({r['pid'] for r in rows if r.get('pid')})}


def db_resources(inst, t_from, t_to):
    rows = [r for r in window_rows(inst.db_rows, t_from, t_to) if 'error' not in r]
    if not rows:
        return {'samples': 0}
    return {'samples': len(rows),
            'app_connections_max': max(r['app_connections'] for r in rows),
            'app_active_max': max(r['app_active'] for r in rows),
            'all_connections_max_incl_harness': max(r['all_connections'] for r in rows),
            'lock_waits_max': max(r['lock_waits'] for r in rows),
            'deadlocks_delta': rows[-1]['deadlocks'] - rows[0]['deadlocks'],
            'xact_rollback_delta': rows[-1]['xact_rollback'] - rows[0]['xact_rollback']}


def log_counts(path, patterns) -> dict:
    out = {k: 0 for k in patterns}
    if not os.path.exists(path):
        return out
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            for k, p in patterns.items():
                if p in line:
                    out[k] += 1
    return out


TARGET_PATTERNS = {
    'outbox_jobs_committed_lines': '[aiface] outbox:',
    'inline_parent_notification_sent': 'parent notification sent',
    'inline_notification_error': 'Notification error for student_id',
    'school_mismatch_rejections': 'SCHOOL MISMATCH',
    'attendance_engine_errors': 'Attendance engine error',
    'sendlog_unexpected_errors': '[sendlog] Unexpected error',
    'tracebacks': 'Traceback (most recent call last)',
    'queuepool_limit': 'QueuePool limit',
    'operational_error': 'OperationalError',
    'ws_unexpected_errors': 'WS unexpected error',
    'worker_timeout': 'WORKER TIMEOUT',
}
WORKER_PATTERNS = {
    'tracebacks': 'Traceback (most recent call last)',
    'failed_to_settle': 'failed to settle',
    'dead_jobs': 'DEAD after',
    'school_mismatch': 'school mismatch',
    'sweep_failed': 'sweep failed',
    'reclaimed': 'reclaimed',
}


# ═══════════════════════════════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════════════════════════════

def start_watchdog():
    section('5b  watchdog: baseline, resource gate, guarding watchdog (--outbox-monitor)')
    wd = os.path.join(TOOL, 'watchdog.py')
    if MODE == 'vps':
        return start_watchdog_vps(wd)
    sh([PY, wd, '--root', ROOT, '--out', OUT, '--baseline', '20'])
    gate = subprocess.run([PY, wd, '--root', ROOT, '--out', OUT, '--check-gate'],
                          capture_output=True, text=True)
    resource_gate = json.load(open(os.path.join(OUT, 'startup_gate.json')))
    os.replace(os.path.join(OUT, 'startup_gate.json'), os.path.join(OUT, 'resource_gate.json'))
    REPORT['preflight']['resource_gate'] = resource_gate
    if gate.returncode != 0 or not resource_gate.get('ok') or \
            resource_gate.get('compliance') != guard_rules.COMPLIANCE_ENFORCED:
        raise RoundFailed(f'watchdog resource gate REFUSED: {resource_gate}')
    sentinel = subprocess.Popen([PY, '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
    log = open(os.path.join(ROOT, 'logs', 'watchdog.log'), 'ab')
    proc = subprocess.Popen([PY, wd, '--root', ROOT, '--out', OUT,
                             '--generator-pid', str(sentinel.pid), '--post-seconds', '20',
                             '--outbox-monitor'],
                            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    say(f'watchdog pid {proc.pid}, sentinel pid {sentinel.pid}')
    return proc, sentinel, resource_gate


def start_watchdog_vps(wd):
    """VPS: the watchdog guards THIS process (--generator-pid), in its own
    session, with the requested hard limits: host-wide CPU >= 80 % for 20 s,
    DB connections >= 80 % of max_connections, any deadlock, sustained lock
    waits, error rate > 1 % over 20 s, MemAvailable < 20 %."""
    limits = ['--host-cpu-pct', str(VPS_HOST_CPU_PCT), '--db-conn-frac', str(VPS_DB_CONN_FRAC)]
    sh([GENPY, wd, '--root', ROOT, '--out', OUT, '--baseline', '20'] + limits)
    gate = subprocess.run([GENPY, wd, '--root', ROOT, '--out', OUT, '--check-gate'],
                          capture_output=True, text=True)
    resource_gate = json.load(open(os.path.join(OUT, 'startup_gate.json')))
    os.replace(os.path.join(OUT, 'startup_gate.json'), os.path.join(OUT, 'resource_gate.json'))
    REPORT['preflight']['resource_gate'] = resource_gate
    if gate.returncode != 0 or not resource_gate.get('ok') or \
            resource_gate.get('compliance') != guard_rules.COMPLIANCE_ENFORCED:
        raise RoundFailed(f'watchdog resource gate REFUSED: {resource_gate}')
    log = open(os.path.join(ROOT, 'logs', 'watchdog.log'), 'ab')
    proc = subprocess.Popen([GENPY, wd, '--root', ROOT, '--out', OUT,
                             '--generator-pid', str(os.getpid()), '--post-seconds', '20',
                             '--outbox-monitor', '--outbox-schools-from-fixtures',
                             '--done-file', DONE_FILE] + limits,
                            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            start_new_session=True)
    say(f'watchdog pid {proc.pid} (own session), guarding generator pid {os.getpid()}')
    return proc, None, resource_gate


def register_generator():
    """VPS: record THIS process in the manifest as the round's generator, so
    the host-side health sentinel can prove its identity before signalling it."""
    if MODE != 'vps':
        return
    import manifest
    p = psutil.Process(os.getpid())
    manifest.add_resource(ROOT, 'process', role='generator-aiface', pid=p.pid,
                          create_time=p.create_time(), cmdline=p.cmdline(),
                          note='AI Face load driver (aiface_load.py, VPS mode)')


def live_outbox_proof(timeout=90):
    required = ['outbox_collector_ok', 'outbox_pending', 'outbox_processing', 'outbox_retry',
                'outbox_sent', 'outbox_dead', 'outbox_cancelled', 'outbox_total_jobs',
                'outbox_backlog', 'outbox_backlog_falling', 'outbox_worker_alive',
                'outbox_unplanned_worker_restarts']
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        time.sleep(3)
        rows = read_monitor()
        if rows:
            last = rows[-1]
            if all(last.get(k) not in (None, '') for k in required):
                break
    if last is None or any(last.get(k) in (None, '') for k in required):
        raise RoundFailed('watchdog is not sampling the real outbox (monitor.csv)')
    if str(last['outbox_collector_ok']).lower() not in ('true', '1'):
        raise RoundFailed('outbox collector reported NOT ok')
    return {'monitor_rows': len(read_monitor()), 'sampled': {k: last[k] for k in required}}


def wait_backlog_zero(cfg, sec, pop, timeout):
    t0 = time.time()
    conn = ro_conn(cfg, sec, 'attlt-tool')
    cur = conn.cursor()
    try:
        while time.time() - t0 < timeout:
            if STOP.is_set():
                return False
            cur.execute(omon.COUNTS_SQL, (pop.all_school_ids,))
            c = omon.derive({r[0]: r[1] for r in cur.fetchall()})
            if c['outbox_backlog'] == 0:
                return True
            time.sleep(0.5)
    finally:
        conn.close()
    return False


def job_states(cfg, sec, pop):
    conn = ro_conn(cfg, sec, 'attlt-tool')
    cur = conn.cursor()
    cur.execute(omon.COUNTS_SQL, (pop.all_school_ids,))
    c = omon.derive({r[0]: r[1] for r in cur.fetchall()})
    cur.execute('SELECT count(*) FROM notification_outbox')
    total_all = cur.fetchone()[0]
    conn.close()
    return {**{s: c[s] for s in omon.STATUSES}, 'backlog': c['outbox_backlog'],
            'total': c['total_jobs'], 'total_all_schools': total_all}


def sanity_check(cfg, sec, pop, devices, inst, events_all, rejected_all):
    section('7  school-isolation runtime sanity check')
    dates_d0 = (local_today(cfg) - dt.timedelta(days=2))
    studs = sorted(pop.students, key=lambda s: s['k'])
    a = next(s for s in studs if s['school_idx'] == 0)          # school A student
    b = next(s for s in studs if s['school_idx'] == 1)          # school B student
    dev_a_sn = common.device_sn(cfg, 0, 0)
    dev_a = pop.device_rows[dev_a_sn]
    # Controlled mis-linked mapping: school A device → school B student. The
    # exact defect the guard exists for. Test-database fixture row only.
    conn = psycopg2.connect(**dict(common.pg_dsn(cfg, sec), application_name='attlt-tool'))
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute('INSERT INTO device_student_mappings (school_id, device_id, employee_no_string, '
                'student_id, is_active, created_at, updated_at) '
                'VALUES (%s, %s, %s, %s, true, now(), now()) RETURNING id',
                (dev_a['school_id'], dev_a['id'], str(SANITY_ENROLLID), b['student_db_id']))
    map_id = cur.fetchone()[0]
    before = job_states(cfg, sec, pop)
    fake_before = read_fake_ledger().get('attempts', 0)

    ok_ev = ev('sanity', a, dates_d0, dt.time(7, 10, 0), 'check_in')
    ok_ev['seq'] = 0
    bad_ev = ev('sanity', a, dates_d0, dt.time(7, 11, 0), 'cross_school_reject',
                device_sn=dev_a_sn, enrollid=SANITY_ENROLLID)
    bad_ev.update({'seq': 1, 'target_student_id': b['student_db_id'],
                   'target_school_id': b['school_id']})
    res = {'mapping_fixture': {'id': map_id, 'device_school_id': dev_a['school_id'],
                               'student_school_id': b['school_id'], 'enrollid': SANITY_ENROLLID}}
    for e in (ok_ev, bad_ev):
        d = devices[e['device_sn']]
        e['sent_wall'] = time.time()
        slot = d.begin_sendlog([AiFaceDevice.record(e['enrollid'], e['date'], e['time'])])
        d.wait_ack(slot, ACK_TIMEOUT_S)
        e['ack_wall'] = time.time() - (time.perf_counter() - slot['ack_t'])
        e['ack_ms'] = round((slot['ack_t'] - slot['sent_t']) * 1000, 3)
        e['ack_ok'] = True
    inst.ackq.put({k: ok_ev[k] for k in ('phase', 'seq', 'student_id', 'date', 'action', 'ack_wall')})
    drained = wait_backlog_zero(cfg, sec, pop, 120)
    time.sleep(1.0)
    # Rejected event: the ACK is still result:true (firmware must clear its
    # queue), so it is NOT counted as a transition; `bad_ev` has no 'sent_wall'
    # in the transition ledger — it is checked separately as `rejected`.
    rej = dict(bad_ev)
    bad_ev_ack = {k: rej[k] for k in ('sent_wall', 'ack_wall', 'ack_ms')}
    rej.pop('sent_wall')
    events_all.append(ok_ev)
    rejected_all.append(rej)
    recon = reconcile(cfg, sec, pop, events_all, drain_complete=True, rejected=rejected_all)
    after = job_states(cfg, sec, pop)
    cur.execute('SELECT count(*) FROM student_attendance WHERE student_id=%s AND date=%s',
                (b['student_db_id'], dates_d0))
    b_rows = cur.fetchone()[0]
    if MODE == 'vps':
        # The preserved school B has 60 days of history; only the sanity date
        # can show a mutation caused by this check.
        cur.execute('SELECT count(*) FROM student_attendance WHERE school_id=%s AND date=%s',
                    (b['school_id'], dates_d0))
    else:
        cur.execute('SELECT count(*) FROM student_attendance WHERE school_id=%s', (b['school_id'],))
    b_school_rows = cur.fetchone()[0]
    cur.execute('SELECT count(*) FROM student_attendance WHERE student_id=%s AND date=%s',
                (a['student_db_id'], dates_d0))
    a_rows = cur.fetchone()[0]
    cur.execute('SELECT count(*) FROM notification_outbox WHERE dedup_key LIKE %s',
                (f"school_attendance_scan:{b['student_db_id']}:%",))
    b_jobs = cur.fetchone()[0]
    # Restore the dataset to exactly the previously validated shape.
    cur.execute('DELETE FROM device_student_mappings WHERE id=%s', (map_id,))
    conn.close()
    tlog = log_counts(os.path.join(ROOT, 'logs', 'target.log'), TARGET_PATTERNS)
    fake_after = read_fake_ledger()
    res.update({
        'case1_same_school': {'school_id': a['school_id'], 'device_sn': ok_ev['device_sn'],
                              'student_id': a['student_db_id'], 'ack_ms': ok_ev['ack_ms'],
                              'attendance_rows': a_rows,
                              'jobs_created': after['total'] - before['total'],
                              'fake_sends': fake_after.get('attempts', 0) - fake_before,
                              'expected_jobs': len(pop.jobs_for(a['student_db_id'],
                                                                ok_ev['date'], 'check_in'))},
        'case2_cross_school': {'device_school_id': dev_a['school_id'],
                               'student_school_id': b['school_id'],
                               'student_id': b['student_db_id'],
                               'ack': bad_ev_ack,
                               'foreign_attendance_rows_for_date': b_rows,
                               'foreign_school_attendance_rows_total': b_school_rows,
                               'foreign_jobs': b_jobs,
                               'fake_sends_attributable':
                                   recon['fake_firebase']['sends_attributable_to_rejected_event'],
                               'target_log_school_mismatch_lines': tlog['school_mismatch_rejections']},
        'drained': drained, 'fixture_mapping_removed': True,
        'fake_ledger_written_by_worker': bool(fake_after) and
            fake_after.get('experiment_id') == cfg['experiment_id'],
        'reconciliation': public(recon),
    })
    c1, c2 = res['case1_same_school'], res['case2_cross_school']
    res['PASS'] = (drained and recon['correct'] and c1['attendance_rows'] == 1
                   and c1['jobs_created'] == c1['expected_jobs'] == 2 and c1['fake_sends'] == 2
                   and c2['foreign_attendance_rows_for_date'] == 0
                   and c2['foreign_school_attendance_rows_total'] == 0
                   and c2['foreign_jobs'] == 0 and c2['fake_sends_attributable'] == 0
                   and c2['target_log_school_mismatch_lines'] == 1
                   and res['fake_ledger_written_by_worker'])
    REPORT['sanity'] = res
    save()
    say(json.dumps({k: res[k] for k in ('case1_same_school', 'case2_cross_school', 'PASS')},
                   indent=2, default=str))
    if not res['PASS']:
        raise RoundFailed('school-isolation runtime sanity check FAILED')


def run_stage(name, events, rate, cfg, sec, pop, devices, inst, events_all, rejected_all):
    section(f'STAGE {name} — {len(events)} transitions at {rate}/s across '
            f'{len({e["school_id"] for e in events})} schools, '
            f'{len({e["device_sn"] for e in events})} device sessions')
    if STOP.is_set():
        raise RoundFailed(f'{name}: STOP already set: {STOP_REASON}')
    opening = job_states(cfg, sec, pop)
    if any(opening[s] for s in ('pending', 'processing', 'retry', 'dead', 'cancelled')):
        raise RoundFailed(f'{name}: queue not clean at stage start: {opening}')
    worker_before = wc.status(cfg)
    inst.curve.clear()
    inst.curve_on.set()
    t_start = time.time()
    drv = drive(events, devices, inst, rate=rate,
                ledger_path=os.path.join(OUT, 'aiface_events.jsonl'))
    t_sent = time.time()
    events_all.extend(events)
    drained = wait_backlog_zero(cfg, sec, pop, 900)
    t_zero = time.time()
    time.sleep(1.5)
    inst.curve_on.clear()
    time.sleep(0.6)
    if STOP.is_set():
        REPORT['stages'][name] = {'driver': drv, 'STOPPED': STOP_REASON}
        save()
        raise RoundFailed(f'{name}: STOP during stage: {json.dumps(STOP_REASON, default=str)[:600]}')

    recon = reconcile(cfg, sec, pop, events_all, drain_complete=drained, rejected=rejected_all)
    jobs = recon.pop('_jobs')
    stage_keys = set()
    for e in events:
        if 'sent_wall' in e:
            stage_keys |= pop.jobs_for(e['student_id'], e['date'], e['action'])
    created = sorted(jobs[k]['created_at'].replace(tzinfo=dt.timezone.utc).timestamp()
                     for k in stage_keys if k in jobs)
    completed = sorted(jobs[k]['completed_at'].replace(tzinfo=dt.timezone.utc).timestamp()
                       for k in stage_keys if k in jobs and jobs[k]['completed_at'])
    curve = [c for c in inst.curve if 'error' not in c]
    peak = max(curve, key=lambda c: c['backlog']) if curve else None
    first_job = created[0] if created else None
    zero_after_peak = next((c['wall'] for c in curve
                            if peak and c['wall'] >= peak['wall'] and c['backlog'] == 0), None)
    send_first = min((e['sent_wall'] for e in events if 'sent_wall' in e), default=t_start)
    send_last = max((e['sent_wall'] for e in events if 'sent_wall' in e), default=t_sent)
    prod_rate = (len(created) / (created[-1] - created[0])
                 if len(created) > 1 and created[-1] > created[0] else None)
    drain_rate_overall = (len(completed) / (completed[-1] - first_job)
                          if completed and first_job and completed[-1] > first_job else None)
    post = [t for t in completed if t > send_last]
    drain_rate_after_input = (len(post) / (post[-1] - send_last)
                              if len(post) > 1 and post[-1] > send_last else None)
    in_window = [t for t in completed if send_first <= t <= send_last]
    worker_rate_during_input = (len(in_window) / (send_last - send_first)
                                if send_last > send_first else None)
    # ACK latency while the backlog was GROWING (first job → peak)
    growing = [e['ack_ms'] for e in events if e.get('ack_ok') and first_job and peak
               and first_job <= e['ack_wall'] <= peak['wall']]
    # sends completed AFTER the ACK of their transition = outside the ACK path
    ack_of = {(e['student_id'], e['date'].replace('-', ''), e['action']): e['ack_wall']
              for e in events if e.get('ack_ok')}
    after_ack = before_ack = 0
    for k in stage_keys:
        j = jobs.get(k)
        if not j or not j['completed_at']:
            continue
        p = k.split(':')
        a_w = ack_of.get((int(p[1]), p[2], p[3]))
        if a_w is None:
            continue
        if j['completed_at'].replace(tzinfo=dt.timezone.utc).timestamp() > a_w:
            after_ack += 1
        else:
            before_ack += 1
    w_from, w_to = t_start, t_zero + 2
    rec = {
        'requested_rate_per_s': rate, 'driver': drv,
        'window_utc': {'start': iso(t_start), 'input_end': iso(t_sent), 'backlog_zero': iso(t_zero)},
        'worker_settings': {'batch': WORKER_BATCH, 'poll_s': WORKER_POLL, 'lease_s': WORKER_LEASE,
                            'max_attempts': WORKER_MAX_ATTEMPTS, 'workers': 1},
        'queue_opening': opening,
        'outbox_jobs_created': len(created),
        'outbox_production_jobs_per_s': round(prod_rate, 2) if prod_rate else None,
        'worker_jobs_per_s_first_job_to_last_completion': round(drain_rate_overall, 2) if drain_rate_overall else None,
        'worker_jobs_per_s_during_input': round(worker_rate_during_input, 2) if worker_rate_during_input else None,
        'worker_jobs_per_s_after_input_stopped': round(drain_rate_after_input, 2) if drain_rate_after_input else None,
        'peak_backlog_fine_0_5s': peak['backlog'] if peak else 0,
        'peak_backlog_utc': iso(peak['wall']) if peak else None,
        'first_job_utc': iso(first_job),
        'first_job_to_zero_s': round(t_zero - first_job, 2) if first_job else None,
        'peak_to_zero_s': round(zero_after_peak - peak['wall'], 2) if peak and zero_after_peak else None,
        'input_end_to_zero_s': round(t_zero - send_last, 2),
        'drained': drained,
        'ack_latency_all': drv['ack_latency'],
        'ack_latency_while_backlog_growing': lat_stats(growing),
        # Informational: a job may legitimately be claimed and delivered in the
        # milliseconds between the commit and the ACK frame reaching the device.
        'deliveries_completed_after_their_ack': after_ack,
        'deliveries_completed_before_their_ack_race': before_ack,
        'resources_watchdog': resources(monitor_between(w_from, w_to)),
        'worker_process': worker_resources(inst, w_from, w_to),
        'db_breakdown': db_resources(inst, w_from, w_to),
        'worker_status_before': {k: worker_before.get(k) for k in ('pid', 'running', 'started_at')},
        'worker_status_after': {k: v for k, v in wc.status(cfg).items() if k not in ('log', 'fake_fcm')},
        'backlog_curve_points': len(curve),
        'reconciliation_cumulative': public(recon),
    }
    rec['worker_restarted'] = rec['worker_status_before'].get('pid') != rec['worker_status_after'].get('pid')
    rec['PASS'] = (recon['correct'] and drained and drv['ack_failures'] == 0
                   and drv['sent'] == drv['intended'] and not rec['worker_restarted']
                   and not STOP.is_set())
    with open(os.path.join(OUT, f'backlog_curve_{name}.csv'), 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=['wall_utc', 'backlog'] + list(omon.STATUSES))
        w.writeheader()
        for c in curve:
            w.writerow({'wall_utc': iso(c['wall']), 'backlog': c['backlog'],
                        **{s: c[s] for s in omon.STATUSES}})
    REPORT['stages'][name] = rec
    save()
    say(json.dumps({k: rec[k] for k in ('driver', 'peak_backlog_fine_0_5s', 'first_job_to_zero_s',
                                        'ack_latency_while_backlog_growing', 'PASS')},
                   indent=2, default=str))
    if not rec['PASS']:
        raise RoundFailed(f'{name} FAILED: violations={recon["violations"]} '
                          f'ack_failures={drv["ack_failures"]} drained={drained} '
                          f'worker_restarted={rec["worker_restarted"]}')
    return rec


def idle_baseline(cfg, sec, pop):
    section(f'IDLE BASELINE — {IDLE_BASELINE_S}s, nothing sent, nothing restarted')
    opening = job_states(cfg, sec, pop)
    if any(opening[s] for s in ('pending', 'processing', 'retry', 'dead', 'cancelled')):
        raise RoundFailed(f'queue not clean before baseline: {opening}')
    wpid = wc.status(cfg).get('pid')
    started = time.time()
    while time.time() - started < IDLE_BASELINE_S:
        if STOP.is_set():
            raise RoundFailed(f'STOP during idle baseline: {STOP_REASON}')
        time.sleep(1.0)
    finished = time.time()
    # monitor.csv is flushed every 5 rows (~10 s); wait for a row past the end.
    deadline = time.time() + 30
    while time.time() < deadline and not monitor_between(finished, finished + 60):
        time.sleep(2)
    rows = monitor_between(started, finished)
    stamps = [dt.datetime.fromisoformat(r['wall_utc'].replace('Z', '+00:00')).timestamp() for r in rows]
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    bad = [r['wall_utc'] for r in rows
           if str(r.get('outbox_collector_ok')).lower() not in ('true', '1')
           or r.get('outbox_backlog') in (None, '') or int(r['outbox_backlog']) != 0]
    closing = job_states(cfg, sec, pop)
    out = {'start_utc': iso(started), 'end_utc': iso(finished),
           'duration_s': round(finished - started, 2),
           'monitor_samples': len(rows),
           'first_sample_utc': rows[0]['wall_utc'] if rows else None,
           'last_sample_utc': rows[-1]['wall_utc'] if rows else None,
           'covered_s': round(stamps[-1] - stamps[0], 2) if len(stamps) > 1 else 0,
           'max_gap_between_samples_s': round(max(gaps), 2) if gaps else None,
           'max_backlog': max((int(r['outbox_backlog']) for r in rows
                               if r.get('outbox_backlog') not in (None, '')), default=None),
           'collector_failures': sum(1 for r in rows if str(r.get('outbox_collector_ok')).lower()
                                     not in ('true', '1')),
           'rows_violating': bad, 'opening_job_states': opening, 'closing_job_states': closing,
           'worker_pid_unchanged': wc.status(cfg).get('pid') == wpid,
           'total_jobs_unchanged': opening['total'] == closing['total'],
           'no_drain_window_s': guard_rules.OUTBOX_THRESHOLDS['outbox_no_drain_window_s'],
           'watchdog_stop_present': os.path.exists(os.path.join(OUT, 'STOP.json'))}
    out['PASS'] = (not bad and out['covered_s'] >= 70 and len(rows) >= 30
                   and (out['max_gap_between_samples_s'] or 99) <= 6 and out['max_backlog'] == 0
                   and out['worker_pid_unchanged'] and out['total_jobs_unchanged']
                   and not out['watchdog_stop_present'])
    REPORT['idle_baseline'] = out
    save()
    say(json.dumps(out, indent=2, default=str))
    if not out['PASS']:
        raise RoundFailed(f'idle baseline NOT clean: {out}')


def ack_commit_summary(inst):
    with inst.lock:
        proofs = list(inst.ack_proofs)
    verified = [p for p in proofs if p['state'] == 'verified']
    margins = [p['margin_ms'] for p in verified if p.get('margin_ms') is not None]
    return {'checked': len(proofs), 'verified': len(verified),
            'superseded_by_later_checkout': sum(1 for p in proofs if p['state'] == 'superseded'),
            'row_missing_at_ack': sum(1 for p in proofs if p['state'] == 'no_row'),
            'commit_after_ack': sum(1 for m in margins if m < 0),
            'margin_ack_minus_commit_ms': lat_stats(margins),
            'method': 'pg_xact_commit_timestamp(xmin) of the student_attendance row read '
                      'right after the ACK, compared with the ACK receive time (same host clock)'}


def main():
    os.makedirs(OUT, exist_ok=True)
    register_generator()
    cfg = step_identity()
    sec = common.load_secrets(ROOT)
    step_gates(cfg, sec)
    if MODE == 'vps':
        fx, inst_fx = step_preserved(cfg, sec)
    else:
        fx, inst_fx = step_seed(cfg, sec)
    step_app_isolation_check(cfg, sec)
    pop = Population(cfg, sec, fx, inst_fx)
    pv = pop.verify()
    REPORT['preflight']['population'] = pv
    say(json.dumps(pv, indent=2))
    if pv['mapping_violations'] or pv['selected_students'] != 600 or \
            pv['active_tokens_per_parent_histogram'] != {2: 600} or not pv['all_tokens_tagged']:
        raise RoundFailed(f'population does not match the validated shape: {pv}')
    plans = build_plans(pop, cfg)
    REPORT['preflight']['plans'] = {'dates': plans['dates'], **plans['checks'],
                                    'stage_a_checkins': STAGE_A_CHECKINS,
                                    'stage_a_checkouts': STAGE_A_CHECKOUTS}
    if not (plans['checks']['checkout_after_checkin_on_same_device']
            and plans['checks']['transitions_unique']):
        raise RoundFailed('stage plan is not well formed')
    if MODE == 'vps':
        dates = sorted(plans['dates'].values())
        if dates != sorted(REPORT['preflight']['fixtures']['test_dates']):
            raise RoundFailed(f'plan dates {dates} differ from the dates vps_prepare_db.py '
                              f'verified empty {REPORT["preflight"]["fixtures"]["test_dates"]}')
        capture_baseline(cfg, sec, dates)

    section('4  start target: gunicorn + AI Face WS on the alternate port, outbox ON')
    target_mod.start(cfg, sec, ws_enabled=True, outbox_enabled=False, aiface_outbox_enabled=True)
    for _ in range(60):
        if cfg['ws_port'] in listening_ports():
            break
        time.sleep(1)
    ports = listening_ports()
    devices, reg = connect_devices(cfg, list(pop.devices))
    REPORT['preflight']['ws'] = {'listening_ports': sorted(ports),
                                 'ws_port_listening': cfg['ws_port'] in ports,
                                 'port_7788_listening': 7788 in ports,
                                 'devices_connected': len(devices),
                                 'reg_ack_ms': lat_stats(list(reg.values()))}
    say(json.dumps(REPORT['preflight']['ws'], indent=2))
    if 7788 in ports or cfg['ws_port'] not in ports or len(devices) != 20:
        raise RoundFailed('WS port state is not as required')

    section('5  start the outbox worker (production unit settings)')
    wc.start(cfg, sec, netns_proof_ok=True, **worker_kw())
    inst = Instruments(cfg, sec, pop, devices)
    inst.start()
    wd_proc, sentinel, resource_gate = start_watchdog()
    REPORT['watchdog'] = {'pid': wd_proc.pid, 'outbox_monitor': True}
    events_all, rejected_all = [], []
    try:
        live = live_outbox_proof()
        REPORT['preflight']['live_outbox_sample'] = live
        # ── composite startup gate ────────────────────────────────────────
        gates = REPORT['preflight']['gates']['checks']
        wst = wc.status(cfg)
        app_iso = REPORT['preflight']['app_isolation_check']
        idc = REPORT['preflight']['identity']
        tst = json.load(open(os.path.join(ROOT, 'run', 'target.json')))
        sg = {
            'experiment_id': cfg['experiment_id'], 'experiment_tag': common.tag(cfg),
            'tested_commit': idc['source_commit'], 'expected_commit': idc['expected_commit'],
            'commit_ok': idc['commit_matches'],
            'isolated_db_host': idc['database_host'], 'isolated_db_name': idc['database_name'],
            'db_classification': idc['database_classification'],
            'app_effective_db_host': app_iso.get('db_host'),
            'app_effective_db_name': app_iso.get('db_name'),
            'production_db_unresolvable': REPORT['preflight']['network_proof']['forbidden_host_dns']
                                          .startswith('unresolvable'),
            'no_default_route': not REPORT['preflight']['network_proof']['default_route_present'],
            'production_db_connections_from_experiment': len(inst.sock_violations),
            'fake_firebase_active_in_worker': gates['worker_firebase_admin'].get('fake') is True,
            'real_firebase_blocked_in_target': gates['target_firebase_admin_import'].startswith('blocked'),
            'app_notification_backend_in_target': app_iso.get('notification_backend'),
            'app_fcm_service_enabled_in_target': app_iso.get('fcm_service_enabled'),
            'port_7788_untouched': not REPORT['preflight']['ws']['port_7788_listening'],
            'alternate_ws_port_active': REPORT['preflight']['ws']['ws_port_listening'],
            'ws_port': cfg['ws_port'],
            'AIFACE_ATTENDANCE_OUTBOX_ENABLED_target': gates['target_AIFACE_ATTENDANCE_OUTBOX_ENABLED'],
            'AIFACE_ATTENDANCE_OUTBOX_ENABLED_worker': gates['worker_AIFACE_ATTENDANCE_OUTBOX_ENABLED'],
            'target_process_flag_recorded': tst.get('aiface_outbox_enabled'),
            'watchdog_active': wd_proc.poll() is None,
            'resource_gate': resource_gate,
            'outbox_collector_active': str(live['sampled']['outbox_collector_ok']).lower() in ('true', '1'),
            'worker_active': bool(wst.get('running')),
            'unrelated_schedulers_disabled': all(
                v == safety_gates.REQUIRED_OFF[k] for k, v in gates['schedulers_off'].items()),
            'no_real_credentials_exposed': (not gates['credential_files_mounted']
                                            and all(gates['credential_vars_empty'].values())
                                            and gates['target_GOOGLE_APPLICATION_CREDENTIALS_empty']
                                            and gates['worker_GOOGLE_APPLICATION_CREDENTIALS_is_fake_file']),
            'socket_audit_active': True,
            'socket_audit_allowed_ips': sorted(inst.allowed_ips),
        }
        required_true = ['commit_ok', 'production_db_unresolvable', 'no_default_route',
                         'fake_firebase_active_in_worker', 'real_firebase_blocked_in_target',
                         'port_7788_untouched', 'alternate_ws_port_active', 'watchdog_active',
                         'outbox_collector_active', 'worker_active',
                         'unrelated_schedulers_disabled', 'no_real_credentials_exposed']
        failed = [k for k in required_true if sg[k] is not True]
        want_class, want_host = (('loopback', cfg['pg_host']) if MODE == 'vps'
                                 else ('container', 'db'))
        if sg['db_classification'] != want_class or sg['app_effective_db_host'] != want_host or \
                sg['app_effective_db_name'] != cfg['db_name']:
            failed.append('db identity')
        if MODE == 'vps':
            np_ = REPORT['preflight']['network_proof'].get('forbidden_target_probes') or {}
            sg['production_targets_probed'] = np_.get('count', 0)
            sg['production_targets_connected'] = np_.get('connected')
            if not np_.get('count') or np_.get('connected') != 0:
                failed.append('production targets not proven unreachable')
        if sg['AIFACE_ATTENDANCE_OUTBOX_ENABLED_target'] != 'true' or \
                sg['AIFACE_ATTENDANCE_OUTBOX_ENABLED_worker'] != 'true':
            failed.append('AI Face outbox flag')
        if sg['production_db_connections_from_experiment'] != 0:
            failed.append('socket audit violation')
        if sg['app_fcm_service_enabled_in_target'] is not False:
            failed.append('fcm service enabled in target')
        sg['failed'] = failed
        sg['ok'] = not failed
        with open(os.path.join(OUT, 'startup_gate.json'), 'w', encoding='utf-8') as fh:
            json.dump(sg, fh, indent=2, default=str)
        REPORT['startup_gate'] = sg
        say(json.dumps(sg, indent=2, default=str))
        if failed:
            raise RoundFailed(f'STARTUP GATE FAILED: {failed}')

        sanity_check(cfg, sec, pop, devices, inst, events_all, rejected_all)

        section('8  initial queue baseline')
        qb = job_states(cfg, sec, pop)
        REPORT['queue_baseline'] = {**qb, 'note': 'sent/total are the 2 jobs of the sanity '
                                    'check (not deleted: deleting rows would destroy evidence)'}
        say(json.dumps(REPORT['queue_baseline'], indent=2))
        if any(qb[s] for s in ('pending', 'processing', 'retry', 'dead', 'cancelled')):
            raise RoundFailed(f'queue not clean before Stage A: {qb}')

        run_stage('A', plans['A'], STAGE_A_RATE, cfg, sec, pop, devices, inst,
                  events_all, rejected_all)
        idle_baseline(cfg, sec, pop)
        run_stage('B', plans['B'], STAGE_B_RATE, cfg, sec, pop, devices, inst,
                  events_all, rejected_all)
        REPORT['VERDICT'] = 'PASS'
    finally:
        try:
            time.sleep(3)
            final = reconcile(cfg, sec, pop, events_all, drain_complete=False,
                              rejected=rejected_all)
            final.pop('_jobs', None)
            REPORT['final_reconciliation'] = final
            REPORT['ack_commit_proof'] = ack_commit_summary(inst)
            REPORT['socket_audit'] = {'violations': inst.sock_violations,
                                      'peers_seen': dict(inst.sock_peers),
                                      'allowed_ips': sorted(inst.allowed_ips)}
            REPORT['device_stats'] = {sn: d.stats for sn, d in devices.items()}
            REPORT['worker_process_whole_round'] = worker_resources(inst, 0, time.time() + 10)
            REPORT['db_breakdown_whole_round'] = db_resources(inst, 0, time.time() + 10)
        except Exception as exc:
            REPORT['errors'].append(f'final reconciliation: {type(exc).__name__}: {exc}')
        if MODE == 'vps':
            # Tell the watchdog the round is over: it stops enforcing, observes
            # --post-seconds of recovery and writes watchdog_summary.json. It is
            # NOT waited for here; vps_aiface_run.sh collects it.
            with open(DONE_FILE, 'w', encoding='utf-8') as fh:
                json.dump({'at_utc': utc_now().isoformat(), 'verdict': REPORT.get('VERDICT')}, fh)
            try:
                with inst.lock:
                    proofs = list(inst.ack_proofs)
                with open(os.path.join(OUT, 'ack_commit_proofs.csv'), 'w', newline='') as fh:
                    w = csv.DictWriter(fh, fieldnames=['phase', 'seq', 'action', 'state',
                                                       'ack_wall', 'commit_ts', 'margin_ms'])
                    w.writeheader()
                    for pr in proofs:
                        w.writerow({k: pr.get(k) for k in w.fieldnames})
            except Exception as exc:
                REPORT['errors'].append(f'ack proof csv: {type(exc).__name__}: {exc}')
        else:
            try:
                sentinel.stdin.close()
                wd_proc.wait(timeout=90)
            except Exception:
                try:
                    wd_proc.terminate()
                except Exception:
                    pass
        inst.shutdown()
        for d in devices.values():
            d.close()
        for path, key in ((os.path.join(OUT, 'watchdog_summary.json'), 'watchdog_summary'),
                          (os.path.join(OUT, 'watchdog_config.json'), 'watchdog_config')):
            if os.path.exists(path):
                try:
                    REPORT[key] = json.load(open(path, encoding='utf-8'))
                except (OSError, ValueError):
                    pass
        REPORT['watchdog_stop'] = STOP_REASON or None
        REPORT['resources_whole_round'] = resources(read_monitor())
        with open(os.path.join(OUT, 'worker_samples.csv'), 'w', newline='') as fh:
            keys = sorted({k for r in inst.worker_rows for k in r})
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            w.writerows(inst.worker_rows)
        with open(os.path.join(OUT, 'db_samples.csv'), 'w', newline='') as fh:
            keys = sorted({k for r in inst.db_rows for k in r})
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            w.writerows(inst.db_rows)
        save()


if __name__ == '__main__':
    code = 0
    try:
        main()
    except RoundFailed as exc:
        REPORT['VERDICT'] = 'SAFETY_STOPPED' if STOP_REASON else 'FAIL'
        REPORT['errors'].append(str(exc))
        say(f'\nROUND FAILED: {exc}')
        code = 1
    except SystemExit as exc:          # gate / identity refusals raise SystemExit subclasses
        REPORT['VERDICT'] = 'FAIL'
        REPORT['errors'].append(f'{type(exc).__name__}: {exc}')
        say(f'\nROUND REFUSED: {exc}')
        code = 1
    except Exception:
        REPORT['VERDICT'] = 'ERROR'
        REPORT['errors'].append(traceback.format_exc())
        say('\nROUND ERROR:\n' + traceback.format_exc())
        code = 2
    finally:
        try:
            cfg = common.load_config(ROOT)
            wc.stop(cfg)
            target_mod.stop(cfg)
        except Exception as exc:
            REPORT['errors'].append(f'teardown: {exc}')
        try:
            REPORT['logs'] = {
                'target': log_counts(os.path.join(ROOT, 'logs', 'target.log'), TARGET_PATTERNS),
                'worker': log_counts(os.path.join(ROOT, 'logs', 'worker.log'), WORKER_PATTERNS)}
        except Exception as exc:
            REPORT['errors'].append(f'log scan: {exc}')
        REPORT['finished_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
        save()
        say(f'\nsummary written to {os.path.join(OUT, "summary.json")}')
        say(f'VERDICT: {REPORT.get("VERDICT")}')
    sys.exit(code)

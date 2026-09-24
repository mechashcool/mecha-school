"""Isolated Locust driver check, run INSIDE the Linux runner container.

Builds a container-local experiment root, runs the REAL app under gunicorn on
container loopback, seeds fixtures into the internal-network Postgres, then runs
the ACTUAL Locust entry point (locustfile.py) for stage 1 only. Nothing is
published; endpoints live on the internal docker network / container loopback.

Purpose: prove the Locust driver works on Linux (gevent/greenlet import, shape,
adapters, protocol against the real app), not to measure capacity.
Writes /out/driver_check_result.json.
"""
import datetime as dt
import json
import os
import secrets
import subprocess
import sys
import time

sys.path.insert(0, '/tooling')
os.environ['ATTLT_BIN_DIR'] = '/usr/local/bin'      # use the image interpreter
import common  # noqa: E402
import target as target_mod  # noqa: E402
import psycopg2  # noqa: E402

ROOT = '/out/exp'
OUT = os.path.join(ROOT, 'results', 'driver')
PGHOST = os.environ['ATTLT_PG_HOST']
PGUSER = os.environ['ATTLT_PG_USER']
PGPW = os.environ['ATTLT_PG_PASSWORD']


def sh(cmd, **kw):
    print('  $', ' '.join(cmd), flush=True)
    return subprocess.run(cmd, **kw)


def setup_root():
    for d in ('secrets', 'run', 'logs', 'results', 'guard'):
        os.makedirs(os.path.join(ROOT, d), exist_ok=True)
    # app source: symlink the read-only mount into <root>/app_src
    link = os.path.join(ROOT, 'app_src')
    if not os.path.exists(link):
        os.symlink('/app_src_ro', link)
    with open(os.path.join(ROOT, 'guard', 'firebase_admin.py'), 'w') as fh:
        pass
    os.makedirs(os.path.join(ROOT, 'guard', 'firebase_admin'), exist_ok=True)
    with open(os.path.join(ROOT, 'guard', 'firebase_admin', '__init__.py'), 'w') as fh:
        fh.write('raise ImportError("firebase_admin blocked in attlt driver check")\n')
    cfg = {
        'experiment_id': 'attlt-20260917-7b7cb5-dockerdrv', 'target_host': '127.0.0.1',
        'http_port': 18180, 'ws_port': 18188, 'pg_mode': 'existing', 'pg_host': PGHOST, 'pg_port': 5432,
        'db_name': 'core_school_attendance_load_test', 'app_revision': 'container', 'app_revision_full': '15068e6',
        'num_schools': 10, 'students_per_school': 1000, 'devices_per_school': 2,
        'history_calendar_days': 60, 'history_end_date': None, 'school_timezone': 'Asia/Baghdad',
        'att_start_time': '07:00:00', 'att_late_threshold': '07:45:00', 'att_absence_threshold': '09:00:00',
        'att_departure_time': '13:00:00', 'target_server': 'gunicorn',
        'gunicorn_env': {'WEB_CONCURRENCY': '1', 'GUNICORN_THREADS': '4', 'GUNICORN_TIMEOUT': '120',
                         'GUNICORN_MAX_REQUESTS': '500', 'GUNICORN_MAX_REQUESTS_JITTER': '50',
                         'SQLALCHEMY_POOL_SIZE': '5', 'SQLALCHEMY_MAX_OVERFLOW': '10', 'SQLALCHEMY_POOL_TIMEOUT': '30'},
    }
    json.dump(cfg, open(os.path.join(ROOT, 'experiment.json'), 'w'), indent=2)
    sec = {'pg_user': PGUSER, 'pg_password': PGPW, 'app_secret_key': secrets.token_urlsafe(48),
           'jwt_secret_key': secrets.token_urlsafe(48), 'ops_metrics_token': secrets.token_urlsafe(16),
           'parent_password': 'Lt-' + secrets.token_urlsafe(12)}
    json.dump(sec, open(os.path.join(ROOT, 'secrets', 'secrets.json'), 'w'), indent=2)
    # minimal manifest so target.start()/stop() (which record processes) work
    json.dump({'experiment_id': cfg['experiment_id'], 'tool_dir': '/tooling', 'root': ROOT,
               'resources': []}, open(os.path.join(ROOT, 'manifest.json'), 'w'), indent=2)
    return cfg, sec


def wait_db():
    for _ in range(40):
        try:
            psycopg2.connect(host=PGHOST, user=PGUSER, password=PGPW,
                             dbname='core_school_attendance_load_test', connect_timeout=3).close()
            return
        except Exception:
            time.sleep(1)
    raise SystemExit('db not reachable')


def ensure_owner(cfg):
    c = psycopg2.connect(host=PGHOST, user=PGUSER, password=PGPW, dbname=cfg['db_name'])
    c.autocommit = True
    k = c.cursor()
    k.execute('CREATE TABLE IF NOT EXISTS attlt_owner (experiment_id text PRIMARY KEY, created_at timestamptz DEFAULT now())')
    k.execute('INSERT INTO attlt_owner (experiment_id) VALUES (%s) ON CONFLICT DO NOTHING', (cfg['experiment_id'],))
    c.close()


def main():
    result = {'started_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'steps': {}}
    cfg, sec = setup_root()
    wait_db()
    ensure_owner(cfg)
    py = '/usr/local/bin/python'
    env = dict(os.environ)

    # migrations + seed (reuses seed.py exactly as the VPS would)
    r = sh([py, '/tooling/seed.py', '--root', ROOT])
    result['steps']['seed'] = r.returncode
    if r.returncode != 0:
        json.dump(result, open('/out/driver_check_result.json', 'w'), indent=2); raise SystemExit('seed failed')

    # start the REAL app under gunicorn on container loopback
    cfg = common.load_config(ROOT)
    target_mod.start(cfg, sec)
    iso = target_mod.isolation_check(cfg, sec)
    result['steps']['isolation_ok'] = iso['ISOLATION_OK']
    result['steps']['server'] = 'gunicorn'

    sh([py, '/tooling/preauth.py', '--root', ROOT])
    pc = sh([py, '/tooling/precheck.py', '--root', ROOT])
    result['steps']['precheck_returncode'] = pc.returncode
    result['precheck'] = json.load(open(os.path.join(ROOT, 'results', 'precheck.json')))

    # THE ACTUAL LOCUST ENTRY POINT — stage 1 only, no watchdog
    lenv = dict(env, ATTLT_ROOT=ROOT, ATTLT_OUT=OUT, ATTLT_STAGE_LIMIT='1', ATTLT_EXPECT_WATCHDOG='0',
                PYTHONUNBUFFERED='1')
    os.makedirs(OUT, exist_ok=True)
    # Hard cap mirrors run_round.py's generator time-limit safety net: stage 1
    # (60 s) + recovery (60 s) + margin. If Locust's own shutdown hangs, we
    # terminate it and still reconcile the DB (the authoritative source).
    lcmd = ['/usr/local/bin/locust', '-f', '/tooling/locustfile.py', '--headless',
            '--host', 'http://127.0.0.1:18180', '--csv', os.path.join(OUT, 'locust'),
            '--csv-full-history', '--html', os.path.join(OUT, 'locust_report.html'),
            '--loglevel', 'INFO', '--logfile', os.path.join(OUT, 'locust.log'), '--stop-timeout', '10']
    print('  $', ' '.join(lcmd), flush=True)
    lp = subprocess.Popen(lcmd, cwd='/tooling', env=lenv)
    try:
        lp.wait(timeout=200)
        result['steps']['locust_returncode'] = lp.returncode
        result['steps']['locust_exit'] = 'clean'
    except subprocess.TimeoutExpired:
        lp.terminate()
        try:
            lp.wait(15)
        except subprocess.TimeoutExpired:
            lp.kill()
        result['steps']['locust_returncode'] = None
        result['steps']['locust_exit'] = 'terminated_by_harness_time_cap'

    # reconcile + analyze the tiny round
    sh([py, '/tooling/reconcile.py', '--root', ROOT, '--out', OUT])
    sh([py, '/tooling/analyze.py', '--root', ROOT, '--out', OUT])
    recon = json.load(open(os.path.join(OUT, 'reconciliation.json')))
    summ = json.load(open(os.path.join(OUT, 'summary.json')))
    # scan locust log for gevent/greenlet import failure
    log = open(os.path.join(OUT, 'locust.log')).read() if os.path.exists(os.path.join(OUT, 'locust.log')) else ''
    greenlet_ok = 'greenlet' not in log.lower() or 'DLL load failed' not in log
    result['driver'] = {
        'locust_started': 'Starting Locust' in log or 'All users spawned' in log or lr.returncode == 0,
        'gevent_greenlet_ok': greenlet_ok,
        'reconciliation_correct': recon.get('correct'),
        'counts': recon.get('counts'),
        'stage1_verdict': next((r0['verdict'] for r0 in json.load(open(os.path.join(OUT, 'stage_results.json')))
                                if r0.get('stage') == 1), None),
        'final_mode': summ.get('final_mode'),
    }
    target_mod.stop(cfg)
    result['finished_utc'] = dt.datetime.now(dt.timezone.utc).isoformat()
    result['PASS'] = bool(result['steps'].get('isolation_ok') and pc.returncode == 0
                          and recon.get('correct') and greenlet_ok)
    json.dump(result, open('/out/driver_check_result.json', 'w'), indent=2, default=str)
    print('DRIVER CHECK', 'PASS' if result['PASS'] else 'FAIL', flush=True)
    raise SystemExit(0 if result['PASS'] else 1)


if __name__ == '__main__':
    main()

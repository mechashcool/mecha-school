"""Create the isolated runtime for one attendance-probe experiment.

Steps (each recorded in the manifest before/after it happens):
  1. experiment.json (non-secret) + secrets/secrets.json (random test-only secrets)
  2. PostgreSQL:
       --pg-mode local-cluster : initdb a brand-new experiment-owned cluster
                                 (own data dir, 127.0.0.1 only, own port)
       --pg-mode existing      : use an existing server via ATTLT_PG_ADMIN_DSN
                                 (env var, never printed) and create a dedicated
                                 role + database there
     In both cases the database must NOT already exist; an ownership marker
     table and a database COMMENT are written.
  3. Clean application source via `git archive <revision>` (no .env, no key
     files, no uncommitted work) + import guard that blocks firebase_admin.
  4. Dedicated virtual environments for the target and the generator.

Nothing here touches production services, the repository working tree, the
project .venv, or any pre-existing database.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import manifest  # noqa: E402

DB_NAME = 'core_school_attendance_load_test'
IS_WINDOWS = os.name == 'nt'


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            s.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def venv_python(venv_dir: str) -> str:
    return os.path.join(venv_dir, 'Scripts' if IS_WINDOWS else 'bin',
                        'python.exe' if IS_WINDOWS else 'python')


def run(cmd, **kw):
    print('  $', ' '.join(str(c) for c in cmd))
    subprocess.run(cmd, check=True, **kw)


def step_config(root, args):
    cfg_path = os.path.join(root, 'experiment.json')
    if os.path.exists(cfg_path):
        print('experiment.json exists — keeping it')
        return json.load(open(cfg_path, encoding='utf-8'))
    for p in (args.http_port, args.ws_port) + ((args.pg_port,) if args.pg_mode == 'local-cluster' else ()):
        if not port_free(p):
            raise SystemExit(f'port {p} is in use — choose another')
    m = manifest.load(root)
    cfg = {
        'experiment_id': m['experiment_id'],
        'target_host': '127.0.0.1',
        'http_port': args.http_port,
        'ws_port': args.ws_port,
        'pg_mode': args.pg_mode,
        'pg_host': args.pg_host,
        'pg_port': args.pg_port,
        'db_name': DB_NAME,
        'app_revision': args.revision,
        'num_schools': 10,
        'students_per_school': 1000,
        'devices_per_school': 2,
        'history_calendar_days': 60,
        'history_end_date': None,          # set by seed.py
        'school_timezone': 'Asia/Baghdad',
        'att_start_time': '07:00:00',
        'att_late_threshold': '07:45:00',
        'att_absence_threshold': '09:00:00',
        'att_departure_time': '13:00:00',
        'target_server': 'gunicorn' if not IS_WINDOWS else 'waitress',
        'gunicorn_env': {                  # production defaults from gunicorn.conf.py, unchanged
            'WEB_CONCURRENCY': '1', 'GUNICORN_THREADS': '4', 'GUNICORN_TIMEOUT': '120',
            # GUNICORN_MAX_REQUESTS / _JITTER are deliberately NOT set: worker
            # recycling is exactly what round 1 tripped over, so the archived
            # gunicorn.conf.py must decide it. Hard-coding 500 here would mask
            # the fix (and silently re-impose the failing configuration).
            'SQLALCHEMY_POOL_SIZE': '5', 'SQLALCHEMY_MAX_OVERFLOW': '10',
            'SQLALCHEMY_POOL_TIMEOUT': '30',
        },
    }
    json.dump(cfg, open(cfg_path, 'w', encoding='utf-8'), indent=2)
    sec = {
        'pg_user': f"attlt_{m['experiment_id'].rsplit('-', 1)[-1]}",
        'pg_password': secrets.token_urlsafe(24),
        'app_secret_key': secrets.token_urlsafe(48),
        'jwt_secret_key': secrets.token_urlsafe(48),
        'ops_metrics_token': secrets.token_urlsafe(24),
        'parent_password': 'Lt-' + secrets.token_urlsafe(12),
    }
    sec_path = os.path.join(root, 'secrets', 'secrets.json')
    json.dump(sec, open(sec_path, 'w', encoding='utf-8'), indent=2)
    manifest.add_resource(root, 'file', path=cfg_path, note='experiment config (non-secret)')
    manifest.add_resource(root, 'file', path=sec_path, note='test-only secrets; never include in reports')
    manifest.add_resource(root, 'ports', http=args.http_port, ws=args.ws_port,
                          pg=args.pg_port if args.pg_mode == 'local-cluster' else None,
                          bind='127.0.0.1 (http/ws via launcher), pg 127.0.0.1')
    return cfg


def pg_bin(name: str) -> str:
    base = os.environ.get('ATTLT_PG_BIN', r'C:\Program Files\PostgreSQL\18\bin' if IS_WINDOWS else '')
    exe = name + ('.exe' if IS_WINDOWS else '')
    return os.path.join(base, exe) if base else exe


def step_pg_local_cluster(root, cfg, sec):
    data_dir = os.path.join(root, 'pgdata')
    log_file = os.path.join(root, 'logs', 'postgres.log')
    if not os.path.exists(data_dir):
        manifest.add_resource(root, 'pg_cluster', data_dir=data_dir, port=cfg['pg_port'],
                              superuser=sec['pg_user'], status='creating',
                              note='experiment-owned PostgreSQL cluster; stop with pg_ctl -D <data_dir> stop')
        pwfile = os.path.join(root, 'secrets', 'pgpass.tmp')
        with open(pwfile, 'w', encoding='utf-8') as fh:
            fh.write(sec['pg_password'])
        try:
            run([pg_bin('initdb'), '-D', data_dir, '-U', sec['pg_user'], '--pwfile', pwfile,
                 '--auth-host=scram-sha-256', '--auth-local=scram-sha-256', '-E', 'UTF8',
                 '--no-locale'])
        finally:
            os.remove(pwfile)
        with open(os.path.join(data_dir, 'postgresql.auto.conf'), 'a', encoding='utf-8') as fh:
            # unix_socket_directories = '' — TCP only on 127.0.0.1.
            # A portable/relocated PostgreSQL build defaults to a socket directory
            # (/var/run/postgresql) that need not exist on the host, and the
            # postmaster then refuses to start. Round 1 hit exactly that. The
            # experiment only ever connects over 127.0.0.1:<port>, so no socket
            # directory is needed and none is created outside the experiment root.
            fh.write(f"\n# attlt {cfg['experiment_id']}\nlisten_addresses = '127.0.0.1'\n"
                     f"port = {cfg['pg_port']}\nmax_connections = 100\n"
                     f"unix_socket_directories = ''\n")
        manifest.write_owner_marker(data_dir, cfg['experiment_id'])
        manifest.update_resource(root, 'pg_cluster', {'data_dir': data_dir}, status='created')
    status = subprocess.run([pg_bin('pg_ctl'), '-D', data_dir, 'status'], capture_output=True, text=True)
    if 'server is running' not in status.stdout:
        # Detach stdio: the postmaster must not inherit the caller's pipes.
        run([pg_bin('pg_ctl'), '-D', data_dir, '-l', log_file, '-w', 'start'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    manifest.update_resource(root, 'pg_cluster', {'data_dir': data_dir}, status='running',
                             log=log_file)


def step_database(root, cfg, sec):
    import psycopg2
    admin_dsn = os.environ.get('ATTLT_PG_ADMIN_DSN')
    if cfg['pg_mode'] == 'local-cluster':
        conn = psycopg2.connect(host=cfg['pg_host'], port=cfg['pg_port'], user=sec['pg_user'],
                                password=sec['pg_password'], dbname='postgres')
    else:
        if not admin_dsn:
            raise SystemExit('ATTLT_PG_ADMIN_DSN is required for --pg-mode existing')
        conn = psycopg2.connect(admin_dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute('SELECT 1 FROM pg_database WHERE datname=%s', (cfg['db_name'],))
    exists = cur.fetchone() is not None
    if exists:
        # Only continue if the ownership marker proves it is THIS experiment's DB.
        c2 = psycopg2.connect(host=cfg['pg_host'], port=cfg['pg_port'], user=sec['pg_user'],
                              password=sec['pg_password'], dbname=cfg['db_name'])
        k = c2.cursor()
        try:
            k.execute('SELECT experiment_id FROM attlt_owner')
            owner = [r[0] for r in k.fetchall()]
        except Exception:
            owner = []
        c2.close()
        if owner != [cfg['experiment_id']]:
            raise SystemExit(f"database {cfg['db_name']} already exists and is NOT owned by this "
                             'experiment — refusing to touch it')
        print('database exists and is owned by this experiment — keeping it')
        return
    if cfg['pg_mode'] == 'existing':
        cur.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (sec['pg_user'],))
        if cur.fetchone():
            raise SystemExit(f"role {sec['pg_user']} already exists — refusing")
        manifest.add_resource(root, 'pg_role', name=sec['pg_user'], server=f"{cfg['pg_host']}:{cfg['pg_port']}")
        cur.execute(f'CREATE ROLE "{sec["pg_user"]}" LOGIN PASSWORD %s', (sec['pg_password'],))
    manifest.add_resource(root, 'database', name=cfg['db_name'],
                          server=f"{cfg['pg_host']}:{cfg['pg_port']}",
                          ownership_marker='table attlt_owner + COMMENT ON DATABASE')
    cur.execute(f'CREATE DATABASE "{cfg["db_name"]}" OWNER "{sec["pg_user"]}" ENCODING \'UTF8\' TEMPLATE template0')
    cur.execute(f'COMMENT ON DATABASE "{cfg["db_name"]}" IS %s', (f"attlt:{cfg['experiment_id']}",))
    conn.close()
    c2 = psycopg2.connect(host=cfg['pg_host'], port=cfg['pg_port'], user=sec['pg_user'],
                          password=sec['pg_password'], dbname=cfg['db_name'])
    c2.autocommit = True
    k = c2.cursor()
    k.execute('CREATE TABLE attlt_owner (experiment_id text PRIMARY KEY, created_at timestamptz DEFAULT now())')
    k.execute('INSERT INTO attlt_owner (experiment_id) VALUES (%s)', (cfg['experiment_id'],))
    c2.close()


def step_app_source(root, cfg, repo):
    dest = os.path.join(root, 'app_src')
    if os.path.exists(dest):
        print('app_src exists — keeping it')
        return
    rev = subprocess.run(['git', '-C', repo, 'rev-parse', cfg['app_revision']], capture_output=True,
                         text=True, check=True).stdout.strip()
    manifest.add_resource(root, 'directory', path=dest, revision=rev,
                          note='git archive of the tested revision (no .env, no untracked files)')
    blob = subprocess.run(['git', '-C', repo, 'archive', '--format=tar', rev], capture_output=True,
                          check=True).stdout
    os.makedirs(dest)
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        tf.extractall(dest, filter='data')
    for forbidden in ('.env', 'firebase-key.json', 'github_actions_mecha', 'github_actions_mecha.pub'):
        p = os.path.join(dest, forbidden)
        if os.path.exists(p):
            raise SystemExit(f'archived source unexpectedly contains {forbidden} — aborting')
    manifest.write_owner_marker(dest, cfg['experiment_id'])
    guard = os.path.join(root, 'guard', 'firebase_admin')
    os.makedirs(guard)
    with open(os.path.join(guard, '__init__.py'), 'w', encoding='utf-8') as fh:
        fh.write('raise ImportError("firebase_admin is blocked inside the attendance load-test '
                 'target (isolation guard: no real push notifications)")\n')
    manifest.write_owner_marker(os.path.join(root, 'guard'), cfg['experiment_id'])
    manifest.add_resource(root, 'directory', path=os.path.join(root, 'guard'),
                          note='import guard placed first on PYTHONPATH of the target process')
    manifest.update_resource(root, 'directory', {'path': dest}, revision=rev)
    cfg['app_revision_full'] = rev
    json.dump({k: v for k, v in cfg.items() if k != 'root'},
              open(os.path.join(root, 'experiment.json'), 'w', encoding='utf-8'), indent=2)


def step_venvs(root, cfg, repo, args):
    target = os.path.join(root, 'venv-target')
    gen = os.path.join(root, 'venv-gen')
    py = args.python or sys.executable
    if not os.path.exists(target):
        manifest.add_resource(root, 'venv', path=target, note='target runtime')
        run([py, '-m', 'venv', target])
        manifest.write_owner_marker(target, cfg['experiment_id'])
        if args.target_requirements == 'pinned':
            req = os.path.join(root, 'app_src', 'requirements.txt')
            run([venv_python(target), '-m', 'pip', 'install', '-q', '-r', req, 'gunicorn'])
        else:
            # Mirror the package set of an existing, working interpreter
            # (used when the pinned set has no wheels for the local Python).
            frozen = subprocess.run([args.mirror_python, '-m', 'pip', 'freeze'], capture_output=True,
                                    text=True, check=True).stdout
            req = os.path.join(root, 'run', 'target-requirements.txt')
            open(req, 'w', encoding='utf-8').write(frozen)
            extra = ['waitress'] if IS_WINDOWS else ['gunicorn']
            run([venv_python(target), '-m', 'pip', 'install', '-q', '-r', req] + extra)
        # never let the target import the real firebase_admin even if installed
        manifest.update_resource(root, 'venv', {'path': target}, status='ready')
    if not os.path.exists(gen):
        manifest.add_resource(root, 'venv', path=gen, note='load generator + watchdog + reconciliation')
        run([py, '-m', 'venv', gen])
        manifest.write_owner_marker(gen, cfg['experiment_id'])
        run([venv_python(gen), '-m', 'pip', 'install', '-q', 'locust', 'websocket-client',
             'psycopg2-binary', 'psutil'])
        manifest.update_resource(root, 'venv', {'path': gen}, status='ready')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--repo', default=manifest.REPO_ROOT)
    ap.add_argument('--revision', default='origin/main')
    ap.add_argument('--http-port', type=int, default=18180)
    ap.add_argument('--ws-port', type=int, default=18188)
    ap.add_argument('--pg-mode', choices=['local-cluster', 'existing'], default='local-cluster')
    ap.add_argument('--pg-host', default='127.0.0.1')
    ap.add_argument('--pg-port', type=int, default=55480)
    ap.add_argument('--python', default=None)
    ap.add_argument('--target-requirements', choices=['pinned', 'mirror'], default='pinned')
    ap.add_argument('--mirror-python', default=None)
    ap.add_argument('--steps', default='config,pg,db,src,venv')
    args = ap.parse_args()
    root = os.path.abspath(args.root)
    steps = args.steps.split(',')
    cfg = step_config(root, args) if 'config' in steps else json.load(open(os.path.join(root, 'experiment.json')))
    sec = json.load(open(os.path.join(root, 'secrets', 'secrets.json'), encoding='utf-8'))
    if 'pg' in steps and cfg['pg_mode'] == 'local-cluster':
        step_pg_local_cluster(root, cfg, sec)
    if 'db' in steps:
        step_database(root, cfg, sec)
    if 'src' in steps:
        step_app_source(root, cfg, args.repo)
    if 'venv' in steps:
        step_venvs(root, cfg, args.repo, args)
    print('setup complete:', root)


if __name__ == '__main__':
    main()

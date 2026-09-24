"""Start / stop / inspect the isolated attendance test application instance.

Isolation controls applied to the target process:
  * runs from <root>/app_src (git archive; no .env, no key files); refuses to
    start if any ancestor directory of config/settings.py contains a .env
    (load_dotenv() in config/settings.py walks up the tree)
  * environment is rebuilt from scratch: every application key is set
    explicitly, so load_dotenv() (override=False) cannot inject anything
  * FCM / Firebase / Supabase / Redis credentials are empty strings
  * <root>/guard is first on PYTHONPATH and makes `import firebase_admin` fail
  * schedulers (auto-absent, fee reminder), Hikvision sync, durable queue off
  * HTTP bound to 127.0.0.1; test DB only; production pool/worker defaults
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import manifest  # noqa: E402

IS_WINDOWS = os.name == 'nt'
PASSTHROUGH = ('SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP',
               'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'HOME', 'LANG', 'LC_ALL',
               'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE')


def venv_bin(root, name):
    # ATTLT_BIN_DIR lets the containerised driver check use the image's global
    # interpreter/console scripts instead of a per-root venv. Not set on the VPS.
    override = os.environ.get('ATTLT_BIN_DIR')
    if override:
        return os.path.join(override, name + ('.exe' if IS_WINDOWS else ''))
    return os.path.join(root, 'venv-target', 'Scripts' if IS_WINDOWS else 'bin',
                        name + ('.exe' if IS_WINDOWS else ''))


def build_env(cfg: dict, sec: dict, *, ws_enabled=True,
              outbox_enabled=False) -> dict:
    root = cfg['root']
    env = {k: os.environ[k] for k in PASSTHROUGH if k in os.environ}
    env['PATH'] = os.pathsep.join([os.path.dirname(venv_bin(root, 'python')),
                                   os.environ.get('SYSTEMROOT', '') + r'\System32' if IS_WINDOWS else '/usr/bin:/bin'])
    uploads = os.path.join(root, 'run', 'uploads')
    os.makedirs(uploads, exist_ok=True)
    env.update({
        'PYTHONPATH': os.pathsep.join([os.path.join(root, 'guard'), os.path.join(root, 'app_src')]),
        'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONUNBUFFERED': '1',
        'FLASK_APP': 'wsgi.py',
        'FLASK_ENV': 'production',
        'SECRET_KEY': sec['app_secret_key'],
        'JWT_SECRET_KEY': sec['jwt_secret_key'],
        'REGISTRATION_TOKEN_KEY': '',
        'DATABASE_URL': common.sqlalchemy_url(cfg, sec),
        'TEST_DATABASE_URL': '',
        'UPLOAD_FOLDER': uploads,
        'MAX_CONTENT_LENGTH': str(16 * 1024 * 1024),
        'PORT': str(cfg['http_port']),
        'AIFACE_WS_ENABLED': 'true' if ws_enabled else 'false',
        'AIFACE_WS_PORT': str(cfg['ws_port']),
        'AIFACE_DEVICE_IP': '',
        'ATTENDANCE_SCHEDULER_DISABLED': 'true',
        'FEE_REMINDER_SCHEDULER_DISABLED': 'true',
        'HIKVISION_AUTO_SYNC': 'false',
        'REDIS_URL': '',
        'RATELIMIT_STORAGE_URI': 'memory://',
        'DURABLE_PUSH_QUEUE_ENABLED': 'false',
        'FCM_SERVICE_ACCOUNT_JSON': '',
        'FIREBASE_SERVICE_ACCOUNT_JSON': '',
        'GOOGLE_APPLICATION_CREDENTIALS': '',
        'SUPABASE_URL': '',
        'SUPABASE_SERVICE_ROLE_KEY': '',
        'SUPABASE_SERVICE_KEY': '',
        'PRIVATE_UPLOADS_ENABLED': 'false',
        'OBSERVABILITY_ENABLED': 'true',
        'OPS_METRICS_TOKEN': sec['ops_metrics_token'],
        'SYNC_JOURNAL_ENABLED': 'false',
        'SYNC_SIGNAL_ENABLED': 'false',
        # OFF by default, so the AI Face round keeps the exact production
        # default and the legacy inline notification path it has always
        # exercised. The institute/outbox round opts in explicitly: the rows
        # are staged by submit_attendance() inside THIS process, so the flag
        # has to be here and not only in the worker.
        'INSTITUTE_ATTENDANCE_OUTBOX_ENABLED': 'true' if outbox_enabled else 'false',
    })
    env.update(cfg['gunicorn_env'])
    return env


def assert_no_dotenv_in_ancestors(cfg):
    d = os.path.join(cfg['root'], 'app_src', 'config')
    while True:
        if os.path.exists(os.path.join(d, '.env')):
            raise SystemExit(f'.env found at {d} — load_dotenv() could read it; refusing to start')
        parent = os.path.dirname(d)
        if parent == d:
            return
        d = parent


def _pid_file(cfg):
    return os.path.join(cfg['root'], 'run', 'target.json')


def proc_identity(pid):
    import psutil
    p = psutil.Process(pid)
    return {'pid': pid, 'create_time': p.create_time(), 'cmdline': p.cmdline()}


def http_ok(cfg, path='/ops/health', timeout=3):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{cfg['http_port']}{path}", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def start(cfg, sec, *, ws_enabled=True, outbox_enabled=False):
    # ws_enabled defaults to True so every existing caller keeps its behaviour.
    # The institute/outbox smoke test passes False: that round never speaks the
    # AI Face protocol, so nothing should bind a WebSocket port at all.
    assert_no_dotenv_in_ancestors(cfg)
    if os.path.exists(_pid_file(cfg)):
        info = json.load(open(_pid_file(cfg)))
        if _alive(info):
            print('target already running', info['pid'])
            return info
    env = build_env(cfg, sec, ws_enabled=ws_enabled,
                    outbox_enabled=outbox_enabled)
    app_src = os.path.join(cfg['root'], 'app_src')
    if IS_WINDOWS:
        cmd = [venv_bin(cfg['root'], 'python'), os.path.join(common.TOOL_DIR, 'serve_waitress.py')]
    else:
        cmd = [venv_bin(cfg['root'], 'gunicorn'), '-c', 'gunicorn.conf.py',
               '--bind', f"127.0.0.1:{cfg['http_port']}", 'wsgi:application']
    log_path = os.path.join(cfg['root'], 'logs', 'target.log')
    log = open(log_path, 'ab')
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    proc = subprocess.Popen(cmd, cwd=app_src, env=env, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=flags,
                            start_new_session=not IS_WINDOWS)
    ident = proc_identity(proc.pid)
    ident.update({'log': log_path, 'http_port': cfg['http_port'],
                  'ws_port': cfg['ws_port'] if ws_enabled else None,
                  'ws_enabled': ws_enabled, 'outbox_enabled': outbox_enabled,
                  'started_at': manifest.utcnow_iso()})
    json.dump(ident, open(_pid_file(cfg), 'w'), indent=2)
    manifest.add_resource(cfg['root'], 'process', role='target-app', **ident)
    for _ in range(90):
        if http_ok(cfg):
            print('target healthy pid', proc.pid)
            return ident
        if proc.poll() is not None:
            raise SystemExit(f'target exited early (code {proc.returncode}); see {log_path}')
        time.sleep(1)
    raise SystemExit('target did not become healthy in 90 s')


def _alive(info):
    try:
        import psutil
        p = psutil.Process(info['pid'])
        return abs(p.create_time() - info['create_time']) < 1.0
    except Exception:
        return False


def stop(cfg):
    import psutil
    if not os.path.exists(_pid_file(cfg)):
        print('no target pid file')
        return
    info = json.load(open(_pid_file(cfg)))
    if not _alive(info):
        print('target not running (or pid reused) — nothing stopped')
        return
    p = psutil.Process(info['pid'])
    if p.cmdline() != info['cmdline']:
        print('pid identity mismatch — refusing to stop')
        return
    children = p.children(recursive=True)
    p.terminate()
    gone, alive = psutil.wait_procs([p] + children, timeout=30)
    for a in alive:
        a.kill()
    manifest.update_resource(cfg['root'], 'process', {'pid': info['pid']}, stopped_at=manifest.utcnow_iso())
    print('target stopped', info['pid'])


def isolation_check(cfg, sec):
    """Import the app with the target environment (WS/schedulers off) and
    report what the notification stack and database binding resolve to."""
    assert_no_dotenv_in_ancestors(cfg)
    env = build_env(cfg, sec, ws_enabled=False)
    code = r'''
import os, json
from urllib.parse import urlparse
from app import create_app
app = create_app('production')
with app.app_context():
    from app.services import fcm_service
    from app.services.notifications import NotificationService
    from app.models import db
    u = urlparse(str(db.engine.url.render_as_string(hide_password=True)))
    try:
        import firebase_admin  # noqa
        fb = 'importable'
    except ImportError as e:
        fb = 'blocked: ' + str(e)[:60]
    print(json.dumps({
        'fcm_service_enabled': fcm_service.is_enabled(),
        'notification_backend': NotificationService.backend.name,
        'firebase_admin_import': fb,
        'db_host': u.hostname, 'db_port': u.port, 'db_name': u.path.lstrip('/'),
        'env_DATABASE_URL_host': urlparse(os.environ['DATABASE_URL']).hostname,
        'fcm_env_empty': all(os.environ.get(k, '') == '' for k in ('FCM_SERVICE_ACCOUNT_JSON','FIREBASE_SERVICE_ACCOUNT_JSON','GOOGLE_APPLICATION_CREDENTIALS')),
        'supabase_url_empty': app.config.get('SUPABASE_URL') == '',
        'redis_url_empty': app.config.get('REDIS_URL') == '',
        'pool': {k: app.config['SQLALCHEMY_ENGINE_OPTIONS'].get(k) for k in ('pool_size','max_overflow','pool_timeout')},
        'config_class': type(app.config).__name__, 'debug': app.debug,
    }))
'''
    out = subprocess.run([venv_bin(cfg['root'], 'python'), '-c', code], cwd=os.path.join(cfg['root'], 'app_src'),
                         env=env, capture_output=True, text=True, timeout=180)
    last = [line for line in out.stdout.splitlines() if line.startswith('{')]
    if not last:
        print(out.stderr[-3000:])
        raise SystemExit('isolation check failed to run')
    res = json.loads(last[-1])
    ok = (not res['fcm_service_enabled'] and res['notification_backend'] == 'devlog'
          and res['firebase_admin_import'].startswith('blocked') and res['db_host'] == cfg['pg_host']
          and res['db_port'] == cfg['pg_port'] and res['db_name'] == cfg['db_name']
          and res['fcm_env_empty'] and res['supabase_url_empty'] and res['redis_url_empty'])
    res['ISOLATION_OK'] = ok
    path = os.path.join(cfg['root'], 'results', 'isolation_check.json')
    json.dump(res, open(path, 'w'), indent=2)
    print(json.dumps(res, indent=2))
    if not ok:
        raise SystemExit('ISOLATION CHECK FAILED')
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['start', 'stop', 'status', 'isolation-check'])
    ap.add_argument('--root', required=True)
    a = ap.parse_args()
    cfg = common.load_config(os.path.abspath(a.root))
    sec = common.load_secrets(cfg['root'])
    if a.action == 'start':
        start(cfg, sec)
    elif a.action == 'stop':
        stop(cfg)
    elif a.action == 'isolation-check':
        isolation_check(cfg, sec)
    else:
        info = json.load(open(_pid_file(cfg))) if os.path.exists(_pid_file(cfg)) else None
        print(json.dumps({'pid_file': info, 'alive': bool(info and _alive(info)), 'healthy': http_ok(cfg)}, indent=2))


if __name__ == '__main__':
    main()

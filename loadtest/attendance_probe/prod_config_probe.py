"""Verify the tested revision and document production↔experiment configuration
differences — WITHOUT printing any value that could be sensitive.

Run as root on the VPS (it reads the production process environment and, if
present, the production .env). The output contains only:

  * key NAMES and presence booleans,
  * values from an explicit numeric whitelist (worker/thread/pool counts, ports),
  * derived facts about the production database location (host class, port,
    whether it is the same host as the app, whether its name collides with the
    experiment database) — never the URL, user, password or host name,
  * gunicorn settings parsed statically (never executed) from the production
    file and from the archived revision under test.

Writes <root>/results/config_differences.json.
"""
from __future__ import annotations

import argparse
import ast
import ipaddress
import json
import os
import re
import subprocess
import sys
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import manifest  # noqa: E402

# Names only — presence is reported, values never are (except the numeric whitelist).
WATCHED_KEYS = [
    'FLASK_ENV', 'PORT', 'WEB_CONCURRENCY', 'GUNICORN_THREADS', 'GUNICORN_TIMEOUT',
    'GUNICORN_MAX_REQUESTS', 'GUNICORN_MAX_REQUESTS_JITTER', 'AIFACE_WS_ENABLED', 'AIFACE_WS_PORT',
    'ATTENDANCE_SCHEDULER_DISABLED', 'HIKVISION_AUTO_SYNC', 'REDIS_URL', 'DURABLE_PUSH_QUEUE_ENABLED',
    'SQLALCHEMY_POOL_SIZE', 'SQLALCHEMY_MAX_OVERFLOW', 'SQLALCHEMY_POOL_TIMEOUT',
    'RATELIMIT_STORAGE_URI', 'OPS_METRICS_TOKEN', 'FIREBASE_SERVICE_ACCOUNT_JSON',
    'GOOGLE_APPLICATION_CREDENTIALS', 'FCM_SERVICE_ACCOUNT_JSON', 'SUPABASE_URL',
    'SUPABASE_SERVICE_ROLE_KEY', 'DATABASE_URL', 'SECRET_KEY', 'JWT_SECRET_KEY',
]
NUMERIC_WHITELIST = {
    'PORT', 'WEB_CONCURRENCY', 'GUNICORN_THREADS', 'GUNICORN_TIMEOUT', 'GUNICORN_MAX_REQUESTS',
    'GUNICORN_MAX_REQUESTS_JITTER', 'AIFACE_WS_PORT', 'SQLALCHEMY_POOL_SIZE',
    'SQLALCHEMY_MAX_OVERFLOW', 'SQLALCHEMY_POOL_TIMEOUT',
}
BOOLISH_WHITELIST = {'AIFACE_WS_ENABLED', 'ATTENDANCE_SCHEDULER_DISABLED', 'HIKVISION_AUTO_SYNC',
                     'DURABLE_PUSH_QUEUE_ENABLED', 'FLASK_ENV'}
BOOLISH_ALLOWED = {'0', '1', 'true', 'false', 'yes', 'no', 'on', 'off',
                   'production', 'development', 'testing', 'staging'}


def safe_value(key, raw):
    """Return a value ONLY if the whitelist proves it cannot be a secret."""
    if raw is None:
        return None
    raw = raw.strip()
    if key in NUMERIC_WHITELIST and re.fullmatch(r'\d{1,6}', raw):
        return int(raw)
    if key in BOOLISH_WHITELIST and raw.lower() in BOOLISH_ALLOWED:
        return raw.lower()
    return '(set — value withheld)'


# ── gunicorn.conf.py: parsed statically, never executed ──────────────────────

def _unwrap(node):
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and \
            node.func.id in ('int', 'float', 'str', 'bool'):
        if not node.args:
            break
        node = node.args[0]
    return node


def parse_gunicorn_conf(path):
    try:
        src = open(path, encoding='utf-8').read()
    except OSError as exc:
        return {'available': False, 'error': type(exc).__name__}
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return {'available': False, 'error': f'SyntaxError: {exc.lineno}'}
    out = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1 or \
                not isinstance(stmt.targets[0], ast.Name):
            continue
        name = stmt.targets[0].id
        node = _unwrap(stmt.value)
        try:
            out[name] = {'literal': ast.literal_eval(node)}
            continue
        except (ValueError, SyntaxError):
            pass
        if isinstance(node, ast.Call):
            fn = node.func
            envget = (isinstance(fn, ast.Attribute) and fn.attr in ('get', 'getenv')) or \
                     (isinstance(fn, ast.Name) and fn.id == 'getenv')
            if envget and node.args:
                try:
                    key = ast.literal_eval(node.args[0])
                    default = ast.literal_eval(node.args[1]) if len(node.args) > 1 else None
                    out[name] = {'from_env': key, 'default': default}
                    continue
                except (ValueError, SyntaxError):
                    pass
        out[name] = {'expression': True}
    return {'available': True, 'settings': out}


# ── environment: names and presence only ─────────────────────────────────────

def proc_env(pid):
    try:
        raw = open(f'/proc/{pid}/environ', 'rb').read().decode('utf-8', 'replace')
    except OSError as exc:
        return None, type(exc).__name__
    env = {}
    for item in raw.split('\0'):
        if '=' in item:
            k, v = item.split('=', 1)
            env[k] = v
    return env, None


def dotenv_keys(path):
    try:
        lines = open(path, encoding='utf-8', errors='replace').read().splitlines()
    except OSError as exc:
        return None, type(exc).__name__
    env = {}
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith('#') or '=' not in ln:
            continue
        k, v = ln.split('=', 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env, None


def host_class(host):
    if not host:
        return 'unset'
    if host in ('localhost',) or host.startswith('/'):
        return 'loopback-or-unix-socket'
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return 'hostname (not an IP) — name withheld'
    if ip.is_loopback:
        return 'loopback'
    if ip.is_private:
        return 'private'
    return 'public'


def describe_db(url, app_host_is_local=True):
    """Derive ONLY non-identifying facts from a database URL."""
    if not url:
        return {'present': False}
    try:
        u = urlsplit(url)
    except ValueError:
        return {'present': True, 'parse_error': True}
    name = (u.path or '').lstrip('/').split('?')[0]
    cls = host_class(u.hostname)
    return {
        'present': True,
        'scheme': (u.scheme or '').split('+')[0],
        'host_class': cls,
        'port': u.port,
        'on_same_host_as_app': cls in ('loopback', 'loopback-or-unix-socket'),
        'database_name_length': len(name),
        'is_the_experiment_database': name == 'core_school_attendance_load_test',
        'note': 'host, user and password are never recorded',
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--repo', required=True)
    ap.add_argument('--prod-pid', type=int, required=True)
    ap.add_argument('--expected-revision', default='')
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    cfg = json.load(open(os.path.join(root, 'experiment.json'), encoding='utf-8'))
    env_git = dict(os.environ, GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='safe.directory',
                   GIT_CONFIG_VALUE_0=a.repo, GIT_OPTIONAL_LOCKS='0')

    def git(*args):
        r = subprocess.run(['git', '-C', a.repo, *args], capture_output=True, text=True, env=env_git)
        return r.stdout.strip() if r.returncode == 0 else ''

    head = git('rev-parse', 'HEAD')
    out = {
        'at_utc': manifest.utcnow_iso(),
        'revision': {
            'production_checkout_head': head,
            'production_working_tree_clean': git('status', '--porcelain') == '',
            'experiment_revision_requested': cfg.get('app_revision'),
            'experiment_revision_archived': cfg.get('app_revision_full'),
            'same_revision': bool(head) and head == (cfg.get('app_revision_full') or a.expected_revision),
        },
        'gunicorn_conf_production_file': parse_gunicorn_conf(os.path.join(a.repo, 'gunicorn.conf.py')),
        'gunicorn_conf_archived_revision': parse_gunicorn_conf(os.path.join(root, 'app_src', 'gunicorn.conf.py')),
        'experiment_gunicorn_env': cfg.get('gunicorn_env'),
    }
    pf, pf_err = proc_env(a.prod_pid)
    de, de_err = dotenv_keys(os.path.join(a.repo, '.env'))
    merged = dict(de or {}, **(pf or {}))     # process environment wins
    out['production_environment'] = {
        'process_environment_readable': pf is not None, 'process_environment_error': pf_err,
        'dotenv_present': de is not None, 'dotenv_error': de_err,
        'dotenv_key_names': sorted(de.keys()) if de else [],
        'watched': {k: {'in_process_env': bool(pf and k in pf), 'in_dotenv': bool(de and k in de),
                        'value': safe_value(k, merged.get(k)) if k in merged else None}
                    for k in WATCHED_KEYS},
        'note': 'only whitelisted numeric/boolean values are recorded; everything else is withheld',
    }
    out['production_database'] = describe_db(merged.get('DATABASE_URL'))
    out['experiment_database'] = {'name': cfg['db_name'], 'host_class': host_class(cfg['pg_host']),
                                  'port': cfg['pg_port'], 'mode': cfg['pg_mode'],
                                  'dedicated_cluster': cfg['pg_mode'] == 'local-cluster'}

    # ── differences that matter for interpreting the measurement ─────────────
    diffs = []
    w = out['production_environment']['watched']
    for key, exp in (cfg.get('gunicorn_env') or {}).items():
        prod = w.get(key, {}).get('value')
        if prod is None:
            conf = (out['gunicorn_conf_archived_revision'].get('settings') or {})
            default = next((v.get('default') for v in conf.values()
                            if isinstance(v, dict) and v.get('from_env') == key), None)
            diffs.append(f'{key}: production unset (falls back to {default!r} in gunicorn.conf.py); '
                         f'experiment sets {exp!r}')
        elif str(prod) != str(exp):
            diffs.append(f'{key}: production {prod!r} vs experiment {exp!r}')
    pa = out['gunicorn_conf_production_file'].get('settings')
    ar = out['gunicorn_conf_archived_revision'].get('settings')
    if pa is not None and ar is not None and pa != ar:
        changed = sorted(set(pa) | set(ar))
        diffs.append('gunicorn.conf.py differs between the production file and the archived revision: '
                     + ', '.join(k for k in changed if pa.get(k) != ar.get(k)))
    if out['production_database'].get('present'):
        pdb = out['production_database']
        diffs.append(f"production database is {pdb['host_class']} on port {pdb['port']}; the experiment "
                     f"uses a dedicated {out['experiment_database']['host_class']} cluster on port "
                     f"{out['experiment_database']['port']} (separate database "
                     f"{out['experiment_database']['name']})")
        if pdb.get('is_the_experiment_database'):
            diffs.append('CRITICAL: production DATABASE_URL points at the experiment database name')
    for k in ('REDIS_URL', 'GOOGLE_APPLICATION_CREDENTIALS', 'FIREBASE_SERVICE_ACCOUNT_JSON',
              'FCM_SERVICE_ACCOUNT_JSON', 'SUPABASE_URL', 'DURABLE_PUSH_QUEUE_ENABLED'):
        if w.get(k, {}).get('value') is not None:
            diffs.append(f'{k} is configured in production but is empty/blocked in the experiment '
                         f'(no Redis, no FCM, no Supabase)')
    if not out['revision']['same_revision']:
        diffs.append('WARNING: the archived revision is not the production checkout HEAD')
    out['differences'] = diffs

    os.makedirs(os.path.join(root, 'results'), exist_ok=True)
    path = os.path.join(root, 'results', 'config_differences.json')
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    print('wrote', path)


if __name__ == '__main__':
    main()

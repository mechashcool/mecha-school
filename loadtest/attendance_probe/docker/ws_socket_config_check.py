"""Checks for the two application-side defects, against the REAL app files.

Mounted read-only at /repo, so this exercises app/services/ai_face_ws.py and
gunicorn.conf.py themselves — not copies.

A. inherited-FD validation: every invalid AIFACE_WS_FD must be rejected without
   raising and without ever closing a descriptor we did not verify.
B. master/worker configuration timing: when AIFACE_WS_ENABLED / AIFACE_WS_PORT
   exist ONLY in .env, the Gunicorn master (which never imports the app, so
   never runs load_dotenv()) must still resolve the same values the worker will.

Writes /out/ws_socket_config_check.json. No secrets are read or printed.
"""
import importlib.util
import json
import os
import runpy
import socket
import sys
import tempfile

RESULT = {'checks': [], 'failures': []}


def record(name, passed, **detail):
    RESULT['checks'].append(dict(name=name, passed=bool(passed), **detail))
    if not passed:
        RESULT['failures'].append(name)
    print(('  PASS  ' if passed else '  FAIL  ') + name + (f'   {detail}' if detail else ''),
          flush=True)


def load_helper():
    """Fresh import of the real app module (no Flask import at module level)."""
    sys.modules.pop('aifw_real', None)
    spec = importlib.util.spec_from_file_location('aifw_real', '/repo/app/services/ai_face_ws.py')
    m = importlib.util.module_from_spec(spec)
    sys.modules['aifw_real'] = m
    spec.loader.exec_module(m)
    return m


def fd_is_open(fd):
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


# ── A. inherited-FD validation ───────────────────────────────────────────────
def part_a():
    print('A. inherited-FD validation (real app/services/ai_face_ws.py)', flush=True)
    os.environ['AIFACE_WS_PORT'] = '18188'

    os.environ.pop('AIFACE_WS_FD', None)
    record('A1 no AIFACE_WS_FD -> worker binds the port itself',
           load_helper()._inherited_listen_socket() is None)

    for label, value, was in (('A2 non-numeric', 'not-a-number', 'ValueError from int()'),
                              ('A3 negative fd', '-1', 'UNCAUGHT ValueError before the fix'),
                              ('A4 huge fd', '99999999999999999999', 'UNCAUGHT OverflowError before the fix'),
                              ('A5 closed fd', '999999', 'OSError'),
                              ('A6 empty', '', 'falsy')):
        os.environ['AIFACE_WS_FD'] = value
        try:
            got = load_helper()._inherited_listen_socket()
            ok, err = got is None, None
        except BaseException as exc:          # noqa: BLE001 — the defect was an escaping exception
            ok, err = False, f'{type(exc).__name__}: {exc}'
        record(f'{label} -> rejected, no exception', ok, previously=was, raised=err)

    # a descriptor that is NOT a listening socket must be left open (detach, never close)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(('127.0.0.1', 0))
    os.environ['AIFACE_WS_FD'] = str(probe.fileno())
    got = load_helper()._inherited_listen_socket()
    record('A7 bound-but-not-listening fd -> rejected and left open',
           got is None and fd_is_open(probe.fileno()))
    probe.close()

    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind(('0.0.0.0', 18188))
    lsock.listen(128)
    os.environ['AIFACE_WS_FD'] = str(lsock.fileno())
    m = load_helper()
    got = m._inherited_listen_socket()
    record('A8 valid master socket -> adopted and cached',
           got is not None and got.fileno() == lsock.fileno()
           and m._inherited_listen_socket() is got, fd=lsock.fileno())
    if got is not None:
        got.detach()
    lsock.close()

    other = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    other.bind(('0.0.0.0', 18999))
    other.listen(8)
    os.environ['AIFACE_WS_FD'] = str(other.fileno())
    got = load_helper()._inherited_listen_socket()
    record('A9 listening on the wrong port -> rejected and left open',
           got is None and fd_is_open(other.fileno()))
    other.close()
    os.environ.pop('AIFACE_WS_FD', None)


# ── B. master/worker configuration timing ────────────────────────────────────
class FakeServer:
    pass


def resolve_master(app_dir):
    """Run the real when_ready() with cwd=app_dir and report the bound port."""
    cwd = os.getcwd()
    os.chdir(app_dir)
    try:
        ns = runpy.run_path(os.path.join(app_dir, 'gunicorn.conf.py'))
        srv = FakeServer()
        ns['when_ready'](srv)
        sock = getattr(srv, '_aiface_ws_sock', None)
        if sock is None:
            return None, ns
        port = sock.getsockname()[1]
        sock.close()
        os.environ.pop('AIFACE_WS_FD', None)
        return port, ns
    finally:
        os.chdir(cwd)


def resolve_worker(app_dir, use_dotenv=True):
    """What the worker resolves: config/settings.py load_dotenv() then the module default."""
    keep = dict(os.environ)
    cwd = os.getcwd()
    os.chdir(app_dir)
    try:
        if use_dotenv:
            from dotenv import load_dotenv
            load_dotenv(os.path.join(app_dir, '.env'))
        return int(os.environ.get('AIFACE_WS_PORT', 7788))
    finally:
        os.chdir(cwd)
        os.environ.clear()
        os.environ.update(keep)


def make_app_dir(env_lines):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, '.env'), 'w') as fh:
        fh.write('\n'.join(env_lines) + '\n')
    with open('/repo/gunicorn.conf.py') as src, open(os.path.join(d, 'gunicorn.conf.py'), 'w') as dst:
        dst.write(src.read())
    return d


def part_b():
    print('B. master/worker configuration timing (real gunicorn.conf.py)', flush=True)
    for k in ('AIFACE_WS_PORT', 'AIFACE_WS_ENABLED', 'AIFACE_WS_FD', 'PORT'):
        os.environ.pop(k, None)

    # B1: port only in .env — the master must agree with the worker
    d = make_app_dir(['SECRET_KEY=not-a-real-secret', 'AIFACE_WS_PORT=17999'])
    master_port, _ = resolve_master(d)
    worker_port = resolve_worker(d)
    env_only = int(os.environ.get('AIFACE_WS_PORT', 7788))      # what os.environ alone would give
    record('B1 port only in .env -> master and worker agree',
           master_port == worker_port == 17999,
           master_port=master_port, worker_port=worker_port,
           os_environ_only_would_be=env_only,
           pre_fix_mismatch_demonstrated=(env_only != worker_port))

    # B2: receiver disabled in .env — the master must not bind anything
    d = make_app_dir(['AIFACE_WS_ENABLED=false', 'AIFACE_WS_PORT=17998'])
    master_port, _ = resolve_master(d)
    record('B2 disabled in .env -> master binds nothing', master_port is None,
           master_port=master_port)

    # B3: a real environment variable still wins over .env (python-dotenv precedence)
    d = make_app_dir(['AIFACE_WS_PORT=17997'])
    os.environ['AIFACE_WS_PORT'] = '17996'
    master_port, _ = resolve_master(d)
    os.environ.pop('AIFACE_WS_PORT', None)
    record('B3 environment variable overrides .env', master_port == 17996, master_port=master_port)

    # B4: no .env and no variable -> documented default
    d = tempfile.mkdtemp()
    with open('/repo/gunicorn.conf.py') as src, open(os.path.join(d, 'gunicorn.conf.py'), 'w') as dst:
        dst.write(src.read())
    master_port, _ = resolve_master(d)
    record('B4 no .env, no variable -> default 7788', master_port == 7788, master_port=master_port)

    # B5: unparsable port in .env -> refuse to bind rather than bind the default
    d = make_app_dir(['AIFACE_WS_PORT=not-a-port'])
    master_port, _ = resolve_master(d)
    record('B5 unparsable port in .env -> master binds nothing', master_port is None,
           master_port=master_port)

    # B6: ${VARIABLE} expansion where the referenced variable differs between the
    # environment and .env. load_dotenv(override=False) — what config/settings.py
    # calls — resolves the reference against os.environ first; dotenv_values()
    # hardcodes override=True and resolves against the file first. The master must
    # follow the worker. https://bbc2.github.io/python-dotenv/#variable-expansion
    d = make_app_dir(['AIFACE_WS_BASE=17001', 'AIFACE_WS_PORT=${AIFACE_WS_BASE}'])
    os.environ['AIFACE_WS_BASE'] = '17002'          # same name, different value
    master_port, _ = resolve_master(d)
    worker_port = resolve_worker(d)                 # real load_dotenv(override=False)
    from dotenv import dotenv_values                # the wrong-precedence reading
    wrong = int(dotenv_values(os.path.join(d, '.env'))['AIFACE_WS_PORT'])
    os.environ.pop('AIFACE_WS_BASE', None)
    record('B6 ${VARIABLE} expansion follows the worker precedence',
           master_port == worker_port == 17002 and wrong == 17001,
           master_port=master_port, worker_port=worker_port,
           dotenv_values_would_be=wrong,
           note='environment wins over .env for expansion under override=False')

    # B7/B8/B9: a resolved port outside 1-65535 must be a configuration error, not
    # an OverflowError escaping when_ready() and aborting the master.
    for label, value in (('B7 port -1', '-1'), ('B8 port 65536', '65536'),
                         ('B9 enormous port', '99999999999999999999')):
        d = make_app_dir([f'AIFACE_WS_PORT={value}'])
        try:
            master_port, _ = resolve_master(d)
            ok, err = master_port is None, None
        except BaseException as exc:                # noqa: BLE001 — the defect was an escaping error
            ok, err = False, f'{type(exc).__name__}: {exc}'
        record(f'{label} -> rejected as configuration error, nothing bound', ok,
               raised=err, previously='uncaught OverflowError from bind()')


if __name__ == '__main__':
    part_a()
    part_b()
    RESULT['PASS'] = not RESULT['failures']
    with open('/out/ws_socket_config_check.json', 'w') as fh:
        json.dump(RESULT, fh, indent=2)
    print('\nWS SOCKET + CONFIG CHECK:', 'PASS' if RESULT['PASS'] else f"FAIL {RESULT['failures']}",
          flush=True)
    raise SystemExit(0 if RESULT['PASS'] else 1)

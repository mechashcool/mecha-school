"""Supervise the SECOND experiment process: app.services.outbox_worker.

The worker is deliberately a separate, independently controllable process, so
a round can stop it, throttle it, kill it and restart it without touching the
target application. That is what makes the three queue modes possible:

    Mode A  worker stopped        attendance commits, backlog accumulates
    Mode B  worker throttled      OUTBOX_BATCH_SIZE / OUTBOX_POLL_SECONDS only
    Mode C  worker restarted      lease reclaim after an interrupted batch

Nothing here changes application behaviour. The worker runs the real
app.services.outbox_worker module, under the real ROLE_OUTBOX_WORKER lifecycle
gate, against the isolated database, with the local fake Firebase shadowing the
real package on PYTHONPATH. Every knob used is an existing environment
variable that the production worker already reads.

Environment construction is a pure function (`build_worker_env`) so the safety
gates can be evaluated, and unit-tested, without starting anything.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import environment_identity as ident  # noqa: E402
import institute_common as ic  # noqa: E402
import manifest  # noqa: E402
import outbox_monitor  # noqa: E402
import safety_gates  # noqa: E402
import target  # noqa: E402

FAKE_FIREBASE_DIR = os.path.join(common.TOOL_DIR, 'fake_firebase')

# Experiment-only lease. The production default is 300 s, which is far longer
# than a whole round, so a reclaim could never be observed. This value is set
# for the experiment process only and never written to a production unit.
EXPERIMENT_LEASE_SECONDS = 30

ROLE = 'outbox-worker'


def _pid_file(cfg) -> str:
    return os.path.join(cfg['root'], 'run', 'worker.json')


def fake_credential_path(root: str) -> str:
    return os.path.join(root, 'run', 'fake-firebase-credential.json')


def write_fake_credential(root: str) -> str:
    """An obviously-fake, non-secret credential file inside the experiment root.

    fcm_service checks that GOOGLE_APPLICATION_CREDENTIALS names a readable
    file before it initialises, so one must exist. The fake
    credentials.Certificate() never opens it, so its contents are irrelevant —
    which is exactly why they are written as a visible placeholder rather than
    anything that could be mistaken for a key.
    """
    path = fake_credential_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump({
            'type': 'service_account',
            'project_id': 'attlt-fake',
            'note': 'NOT A CREDENTIAL. The attlt fake firebase_admin never '
                    'reads this file. It exists only so fcm_service finds a '
                    'readable path. Contains no key material.',
            'client_email': 'attlt-fake@invalid.invalid',
        }, fh, indent=2)
    return path


def build_worker_env(cfg: dict, sec: dict, *, batch_size: int = 20,
                     poll_seconds: float = 5.0,
                     lease_seconds: int = EXPERIMENT_LEASE_SECONDS,
                     max_attempts: int = 5,
                     fake_mode: str = 'success') -> dict:
    """The worker's complete environment. Pure: builds a dict, starts nothing.

    Starts from the target's environment so every isolation control already
    proven for the app (empty credentials, schedulers off, isolated DATABASE_URL,
    no .env) applies identically here, then adds only what the worker needs.
    """
    env = target.build_env(cfg, sec, ws_enabled=False)

    # The worker must never serve HTTP or bind the AI Face socket.
    env['AIFACE_WS_ENABLED'] = 'false'

    # Declare the role explicitly. app/lifecycle.py then refuses to start any
    # shared background service in this process, and the worker's own
    # set_role() call agrees with it.
    env['MECHA_PROCESS_ROLE'] = ROLE
    env['FLASK_ENV'] = 'production'
    env['INSTITUTE_ATTENDANCE_OUTBOX_ENABLED'] = 'true'

    env['OUTBOX_BATCH_SIZE'] = str(int(batch_size))
    env['OUTBOX_POLL_SECONDS'] = str(poll_seconds)
    env['OUTBOX_LEASE_SECONDS'] = str(int(lease_seconds))
    env['OUTBOX_MAX_ATTEMPTS'] = str(int(max_attempts))

    # Fake Firebase: first on PYTHONPATH so `import firebase_admin` resolves
    # here, and the markers without which it refuses to import at all.
    env['PYTHONPATH'] = os.pathsep.join(
        [FAKE_FIREBASE_DIR, os.path.join(cfg['root'], 'app_src')])
    env['ATTLT_FAKE_FIREBASE'] = '1'
    env['ATTLT_FAKE_FIREBASE_MODE'] = fake_mode
    env['ATTLT_EXPERIMENT_ID'] = cfg['experiment_id']
    env['ATTLT_EXPERIMENT_ROOT'] = cfg['root']
    env['GOOGLE_APPLICATION_CREDENTIALS'] = fake_credential_path(cfg['root'])
    return env


def _check_gates(cfg, env, *, netns_proof_ok=None, on_vps=False,
                 sentinel_running=None):
    identity = ident.verify(cfg['root'], experiment_id=cfg['experiment_id'],
                            tag=common.tag(cfg))
    safety_gates.run_all(env=env, cfg=cfg, identity=identity,
                         prefixes=ic.experiment_prefixes(common.tag(cfg)),
                         role='worker', on_vps=on_vps,
                         sentinel_running=sentinel_running,
                         netns_proof_ok=netns_proof_ok)
    return identity


def start(cfg, sec, **kw):
    gate_kw = {k: kw.pop(k) for k in ('netns_proof_ok', 'on_vps',
                                      'sentinel_running') if k in kw}
    if os.path.exists(_pid_file(cfg)):
        info = json.load(open(_pid_file(cfg)))
        if target._alive(info):
            print('worker already running', info['pid'])
            return info
    write_fake_credential(cfg['root'])
    env = build_worker_env(cfg, sec, **kw)
    _check_gates(cfg, env, **gate_kw)

    app_src = os.path.join(cfg['root'], 'app_src')
    cmd = [target.venv_bin(cfg['root'], 'python'), '-m',
           'app.services.outbox_worker', 'run']
    log_path = os.path.join(cfg['root'], 'logs', 'worker.log')
    log = open(log_path, 'ab')
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if target.IS_WINDOWS else 0
    proc = subprocess.Popen(cmd, cwd=app_src, env=env, stdout=log,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            creationflags=flags,
                            start_new_session=not target.IS_WINDOWS)
    info = target.proc_identity(proc.pid)
    info.update({'log': log_path, 'role': ROLE,
                 'batch_size': env['OUTBOX_BATCH_SIZE'],
                 'poll_seconds': env['OUTBOX_POLL_SECONDS'],
                 'lease_seconds': env['OUTBOX_LEASE_SECONDS'],
                 'fake_firebase_mode': env['ATTLT_FAKE_FIREBASE_MODE'],
                 'started_at': manifest.utcnow_iso()})
    json.dump(info, open(_pid_file(cfg), 'w'), indent=2)
    # `info` already carries role=ROLE; passing it again would collide.
    manifest.add_resource(cfg['root'], 'process', **info)
    # Tell the monitor this restart was ours, so it is not counted as a crash.
    outbox_monitor.record_lifecycle_event(cfg['root'], 'start')
    time.sleep(1.0)
    if proc.poll() is not None:
        raise SystemExit(f'worker exited immediately (code {proc.returncode}); '
                         f'see {log_path}')
    print('worker started', proc.pid)
    return info


def stop(cfg, *, timeout: int = 60):
    """SIGTERM first: the worker finishes its batch and settles in-flight rows."""
    import psutil
    if not os.path.exists(_pid_file(cfg)):
        print('no worker pid file')
        return None
    info = json.load(open(_pid_file(cfg)))
    if not target._alive(info):
        print('worker not running (or pid reused) — nothing stopped')
        return None
    p = psutil.Process(info['pid'])
    if p.cmdline() != info['cmdline']:
        print('worker pid identity mismatch — refusing to stop')
        return None
    children = p.children(recursive=True)
    p.terminate()
    _, alive = psutil.wait_procs([p] + children, timeout=timeout)
    for a in alive:
        a.kill()
    manifest.update_resource(cfg['root'], 'process', {'pid': info['pid']},
                             stopped_at=manifest.utcnow_iso())
    outbox_monitor.record_lifecycle_event(cfg['root'], 'stop')
    print('worker stopped', info['pid'])
    return info


def kill(cfg):
    """Mode C: SIGKILL, so the lease is left held and reclaim must recover it."""
    import psutil
    if not os.path.exists(_pid_file(cfg)):
        print('no worker pid file')
        return None
    info = json.load(open(_pid_file(cfg)))
    if not target._alive(info):
        return None
    p = psutil.Process(info['pid'])
    if p.cmdline() != info['cmdline']:
        print('worker pid identity mismatch — refusing to kill')
        return None
    for q in [p] + p.children(recursive=True):
        q.kill()
    manifest.update_resource(cfg['root'], 'process', {'pid': info['pid']},
                             killed_at=manifest.utcnow_iso())
    outbox_monitor.record_lifecycle_event(cfg['root'], 'kill')
    print('worker killed (lease left held)', info['pid'])
    return info


def status(cfg) -> dict:
    out = {'running': False}
    if os.path.exists(_pid_file(cfg)):
        info = json.load(open(_pid_file(cfg)))
        out.update({k: info.get(k) for k in
                    ('pid', 'role', 'started_at', 'batch_size', 'poll_seconds',
                     'lease_seconds', 'fake_firebase_mode', 'log')})
        out['running'] = target._alive(info)
    fake = os.path.join(cfg['root'], 'run', 'fake_fcm_sends.json')
    if os.path.exists(fake):
        with open(fake, encoding='utf-8') as fh:
            data = json.load(fh)
        out['fake_fcm'] = {k: data.get(k) for k in
                           ('attempts', 'successes', 'failures',
                            'distinct_fingerprints', 'duplicate_sends', 'mode')}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['start', 'stop', 'restart', 'kill',
                                       'status', 'env-check'])
    ap.add_argument('--root', required=True)
    ap.add_argument('--batch-size', type=int, default=20)
    ap.add_argument('--poll-seconds', type=float, default=5.0)
    ap.add_argument('--lease-seconds', type=int, default=EXPERIMENT_LEASE_SECONDS)
    ap.add_argument('--max-attempts', type=int, default=5)
    ap.add_argument('--fake-mode', default='success',
                    choices=['success', 'unregistered', 'transient', 'senderid'])
    ap.add_argument('--netns-proof-ok', action='store_true')
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    cfg, sec = common.load_config(root), common.load_secrets(root)
    kw = dict(batch_size=a.batch_size, poll_seconds=a.poll_seconds,
              lease_seconds=a.lease_seconds, max_attempts=a.max_attempts,
              fake_mode=a.fake_mode)
    if a.action == 'start':
        start(cfg, sec, netns_proof_ok=a.netns_proof_ok or None, **kw)
    elif a.action == 'stop':
        stop(cfg)
    elif a.action == 'kill':
        kill(cfg)
    elif a.action == 'restart':
        stop(cfg)
        start(cfg, sec, netns_proof_ok=a.netns_proof_ok or None, **kw)
    elif a.action == 'env-check':
        env = build_worker_env(cfg, sec, **kw)
        _check_gates(cfg, env, netns_proof_ok=a.netns_proof_ok or None)
        print(json.dumps({'gates': 'PASS', 'role': env['MECHA_PROCESS_ROLE'],
                          'outbox_enabled': env['INSTITUTE_ATTENDANCE_OUTBOX_ENABLED'],
                          'firebase': 'fake-local'}, indent=2))
    else:
        print(json.dumps(status(cfg), indent=2))


if __name__ == '__main__':
    main()

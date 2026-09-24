"""Verify the effective runtime configuration BEFORE any load is generated.

Round 1 failed because the single worker recycled every ~500 requests and took
the AI Face listener down with it. Round 2 must not start unless the runtime it
is about to measure is actually the fixed one:

  * the ARCHIVED gunicorn.conf.py, resolved under the target's own environment,
    yields workers=1, threads=4, max_requests=0 (no recycling by request count);
  * the master owns the AI Face WS listening socket and the worker ADOPTED it
    (not "bound by this process", and no EADDRINUSE fallback);
  * the receiver is listening and no worker has recycled yet.

Run it after `target.py start`, inside the private network namespace.
Writes <root>/results/preload_verification.json and exits non-zero on failure.

    python preload_verify.py --root <root> [--expect-workers 1]
        [--expect-threads 4] [--expect-max-requests 0]
"""
from __future__ import annotations

import argparse
import json
import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import manifest  # noqa: E402
import target as target_mod  # noqa: E402

MASTER_OWNS = 'master owns the AI Face WS listening socket'
WORKER_ADOPTED = 'inherited master socket'
WORKER_BOUND_ITSELF = 'bound by this process'
PORT_IN_USE = 'already in use'


def effective_gunicorn(root, cfg, sec):
    """Resolve the archived gunicorn.conf.py under the target's own environment."""
    env = target_mod.build_env(cfg, sec)
    keep = dict(os.environ)
    cwd = os.getcwd()
    try:
        os.environ.clear()
        os.environ.update(env)
        os.chdir(os.path.join(root, 'app_src'))
        ns = runpy.run_path(os.path.join(root, 'app_src', 'gunicorn.conf.py'))
        return {'workers': ns['workers'], 'threads': ns['threads'],
                'worker_class': ns.get('worker_class'),
                'max_requests': ns['max_requests'],
                'max_requests_jitter': ns['max_requests_jitter'],
                'graceful_timeout': ns['graceful_timeout'],
                'timeout': ns['timeout'],
                'max_requests_env_override_present': 'GUNICORN_MAX_REQUESTS' in env}
    finally:
        os.chdir(cwd)
        os.environ.clear()
        os.environ.update(keep)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--expect-workers', type=int, default=1)
    ap.add_argument('--expect-threads', type=int, default=4)
    ap.add_argument('--expect-max-requests', type=int, default=0)
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    cfg = common.load_config(root)
    sec = common.load_secrets(root)
    res = {'at_utc': manifest.utcnow_iso(), 'experiment_id': cfg['experiment_id'],
           'app_revision': cfg.get('app_revision_full'), 'failures': []}

    eff = effective_gunicorn(root, cfg, sec)
    res['effective_gunicorn'] = eff
    res['expected'] = {'workers': a.expect_workers, 'threads': a.expect_threads,
                       'max_requests': a.expect_max_requests}
    for key, want in (('workers', a.expect_workers), ('threads', a.expect_threads),
                      ('max_requests', a.expect_max_requests)):
        if eff[key] != want:
            res['failures'].append(f'effective {key} is {eff[key]}, expected {want}')
    if eff['max_requests_env_override_present']:
        res['failures'].append('GUNICORN_MAX_REQUESTS is present in the target environment — '
                               'the experiment must let gunicorn.conf.py decide')

    tinfo = json.load(open(os.path.join(root, 'run', 'target.json'), encoding='utf-8'))
    log_path = tinfo['log']
    text = ''
    if os.path.exists(log_path):
        with open(log_path, 'r', encoding='utf-8', errors='replace') as fh:
            text = fh.read()
    res['listener'] = {
        'master_owns_socket': MASTER_OWNS in text,
        'worker_adopted_inherited_socket': WORKER_ADOPTED in text,
        'worker_bound_it_itself': WORKER_BOUND_ITSELF in text,
        'port_in_use_error': PORT_IN_USE in text,
        'worker_boots': text.count('Booting worker'),
    }
    res['recycles_before_load'] = max(0, res['listener']['worker_boots'] - 1)
    if not res['listener']['master_owns_socket']:
        res['failures'].append('the master did not report owning the AI Face WS listening socket')
    if not res['listener']['worker_adopted_inherited_socket']:
        res['failures'].append('the worker did not adopt the inherited master socket')
    if res['listener']['worker_bound_it_itself']:
        res['failures'].append('the worker bound the WS port itself — the fix is not in effect')
    if res['listener']['port_in_use_error']:
        res['failures'].append('a port-in-use error appears in the target log')
    if res['recycles_before_load']:
        res['failures'].append(f"{res['recycles_before_load']} worker recycle(s) already happened "
                               'before load started')

    res['PASS'] = not res['failures']
    out = os.path.join(root, 'results', 'preload_verification.json')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump(res, fh, indent=2)
    print(json.dumps(res, indent=2))
    if not res['PASS']:
        print('PRE-LOAD VERIFICATION FAILED — refusing to generate load', flush=True)
    raise SystemExit(0 if res['PASS'] else 1)


if __name__ == '__main__':
    main()

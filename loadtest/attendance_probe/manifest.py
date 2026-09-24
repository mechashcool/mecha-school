"""Experiment ownership manifest.

Every resource the attendance load probe creates is recorded here BEFORE (or
immediately after) it is created, so a later cleanup can remove exactly the
experiment-owned resources and nothing else.

The manifest lives at <experiment_root>/manifest.json. Secrets are never stored
in it (they live in <experiment_root>/secrets/, which reports never include).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import secrets
import socket
import sys
import tempfile

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
# ATTLT_REPO lets the tooling run from outside the repository (VPS transfer
# package) while still pointing the read-only `git archive` / repository
# snapshot at the real checkout.
REPO_ROOT = os.path.abspath(os.environ.get('ATTLT_REPO') or os.path.join(TOOL_DIR, '..', '..'))
DEFAULT_EXPERIMENTS_DIR = os.path.abspath(os.path.join(REPO_ROOT, '..', '..', '_loadtest'))
OWNERSHIP_MARKER_FILE = '.attlt_owner'


def utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds')


def new_experiment_id() -> str:
    return 'attlt-' + _dt.datetime.now().strftime('%Y%m%d') + '-' + secrets.token_hex(3)


def experiment_root(exp_id: str, base: str | None = None) -> str:
    return os.path.join(base or DEFAULT_EXPERIMENTS_DIR, exp_id)


def manifest_path(root: str) -> str:
    return os.path.join(root, 'manifest.json')


def load(root: str) -> dict:
    with open(manifest_path(root), encoding='utf-8') as fh:
        return json.load(fh)


def save(root: str, data: dict) -> None:
    """Atomic write so a crash never leaves a half-written manifest."""
    fd, tmp = tempfile.mkstemp(dir=root, prefix='.manifest.', suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, manifest_path(root))


def add_resource(root: str, kind: str, **fields) -> dict:
    data = load(root)
    entry = {'kind': kind, 'recorded_at': utcnow_iso(), **fields}
    data.setdefault('resources', []).append(entry)
    save(root, data)
    return entry


def update_resource(root: str, kind: str, match: dict, **fields) -> None:
    data = load(root)
    for res in data.get('resources', []):
        if res.get('kind') == kind and all(res.get(k) == v for k, v in match.items()):
            res.update(fields)
            res['updated_at'] = utcnow_iso()
    save(root, data)


def write_owner_marker(directory: str, exp_id: str) -> None:
    """Drop an ownership marker inside a directory the experiment created."""
    with open(os.path.join(directory, OWNERSHIP_MARKER_FILE), 'w', encoding='utf-8') as fh:
        fh.write(exp_id + '\n')


def owner_marker_matches(directory: str, exp_id: str) -> bool:
    try:
        with open(os.path.join(directory, OWNERSHIP_MARKER_FILE), encoding='utf-8') as fh:
            return fh.read().strip() == exp_id
    except OSError:
        return False


def create(exp_id: str | None = None, base: str | None = None) -> str:
    exp_id = exp_id or new_experiment_id()
    root = experiment_root(exp_id, base)
    if os.path.exists(root):
        raise SystemExit(f'refusing to reuse existing experiment directory: {root}')
    os.makedirs(root)
    write_owner_marker(root, exp_id)
    for sub in ('secrets', 'logs', 'run', 'results'):
        os.makedirs(os.path.join(root, sub))
        write_owner_marker(os.path.join(root, sub), exp_id)
    save(root, {
        'experiment_id': exp_id,
        'purpose': 'Core School student-attendance progressive load probe',
        'created_at': utcnow_iso(),
        'created_on_host': socket.gethostname(),
        'tool_dir': TOOL_DIR,
        'root': root,
        'resources': [
            {'kind': 'directory', 'path': root, 'owner_marker': OWNERSHIP_MARKER_FILE,
             'recorded_at': utcnow_iso(), 'note': 'experiment root'},
            {'kind': 'directory', 'path': TOOL_DIR, 'recorded_at': utcnow_iso(),
             'note': 'tooling source (kept for later rounds; no owner marker, preserve by default)'},
        ],
    })
    return root


if __name__ == '__main__':
    base = sys.argv[1] if len(sys.argv) > 1 else None
    print(create(base=base))

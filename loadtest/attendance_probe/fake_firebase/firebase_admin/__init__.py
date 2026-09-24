"""A local, offline stand-in for the `firebase_admin` package.

This directory is placed on PYTHONPATH for the EXPERIMENT outbox worker only,
where it shadows the real package. The production outbox worker code then runs
completely unmodified: app/services/fcm_service.py imports firebase_admin,
builds a messaging.Message and calls messaging.send() exactly as it always
does, and interprets the returned message id exactly like a real success.

Three independent locks keep this out of production
───────────────────────────────────────────────────
 1. It is not installed anywhere. It is only reachable when something puts
    loadtest/attendance_probe/fake_firebase/ on sys.path, which only
    worker_control.py does, for a process it builds itself.
 2. It REFUSES TO IMPORT unless the experiment markers are present:
    ATTLT_FAKE_FIREBASE=1, a non-empty ATTLT_EXPERIMENT_ID, and an experiment
    root whose .attlt_owner marker file contains that same id.
 3. It performs no network I/O of any kind. There is no socket, no http
    client, and no import of one, anywhere in this package.

If the real firebase_admin is also importable, whichever appears first on
sys.path wins — so worker_control.py prepends this directory and the startup
gate asserts that firebase_admin.ATTLT_FAKE is True before any load begins.
A process that expected the fake and got the real package fails closed.

Nothing here ever logs, prints, stores or returns a token string. Sends are
counted by a salted fingerprint so duplicates can be detected without the
token ever leaving memory.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

ATTLT_FAKE = True
__version__ = 'attlt-fake-0'


class FakeFirebaseNotPermitted(ImportError):
    """Raised when the fake is reachable but the experiment markers are not."""


def _require_experiment() -> tuple[str, str]:
    """Fail closed unless this really is an experiment runtime."""
    if os.environ.get('ATTLT_FAKE_FIREBASE') != '1':
        raise FakeFirebaseNotPermitted(
            'attlt fake firebase_admin: ATTLT_FAKE_FIREBASE=1 is required. '
            'This package must never be importable in production.')
    exp_id = (os.environ.get('ATTLT_EXPERIMENT_ID') or '').strip()
    if not exp_id:
        raise FakeFirebaseNotPermitted(
            'attlt fake firebase_admin: ATTLT_EXPERIMENT_ID is required')
    root = (os.environ.get('ATTLT_EXPERIMENT_ROOT') or '').strip()
    if not root:
        raise FakeFirebaseNotPermitted(
            'attlt fake firebase_admin: ATTLT_EXPERIMENT_ROOT is required')
    marker = os.path.join(root, '.attlt_owner')
    try:
        with open(marker, encoding='utf-8') as fh:
            owner = fh.read().strip()
    except OSError as exc:
        raise FakeFirebaseNotPermitted(
            f'attlt fake firebase_admin: ownership marker unreadable: {exc}')
    if owner != exp_id:
        raise FakeFirebaseNotPermitted(
            'attlt fake firebase_admin: ownership marker does not match '
            'ATTLT_EXPERIMENT_ID')
    return exp_id, root


EXPERIMENT_ID, EXPERIMENT_ROOT = _require_experiment()

# firebase_admin exposes this; fcm_service reads it to decide whether the
# httpTimeout application option is supported. Declaring it keeps the
# production code on its normal branch.
_CONFIG_VALID_KEYS = {'credential', 'databaseURL', 'storageBucket',
                      'projectId', 'databaseAuthVariableOverride',
                      'serviceAccountId', 'httpTimeout'}

_apps: dict = {}
_lock = threading.Lock()


class App:
    def __init__(self, name, credential, options):
        self.name = name
        self.credential = credential
        self.options = dict(options or {})


def initialize_app(credential=None, options=None, name='[DEFAULT]'):
    with _lock:
        app = App(name, credential, options)
        _apps[name] = app
        _ledger().note_init(options or {})
        return app


def get_app(name='[DEFAULT]'):
    return _apps[name]


def delete_app(app):
    _apps.pop(getattr(app, 'name', '[DEFAULT]'), None)


# ── Send ledger ──────────────────────────────────────────────────────────────

class _Ledger:
    """Counts sends. Stores a salted fingerprint, never a token."""

    def __init__(self, path: str, salt: str):
        self._path = path
        self._salt = salt
        self._lock = threading.Lock()
        self.attempts = 0
        self.successes = 0
        self.failures = 0
        self.init_options: dict = {}
        self.fingerprints: dict = {}

    def fingerprint(self, token: str) -> str:
        return hashlib.sha256(
            (self._salt + '|' + (token or '')).encode('utf-8')).hexdigest()[:16]

    def note_init(self, options: dict) -> None:
        self.init_options = {k: v for k, v in options.items() if k != 'credential'}
        self.flush()

    def record(self, token: str, ok: bool) -> str:
        with self._lock:
            self.attempts += 1
            fp = self.fingerprint(token)
            self.fingerprints[fp] = self.fingerprints.get(fp, 0) + 1
            if ok:
                self.successes += 1
            else:
                self.failures += 1
            self.flush_locked()
            return fp

    def flush(self) -> None:
        with self._lock:
            self.flush_locked()

    def flush_locked(self) -> None:
        if not self._path:
            return
        payload = {
            'experiment_id': EXPERIMENT_ID,
            'mode': mode(),
            'attempts': self.attempts,
            'successes': self.successes,
            'failures': self.failures,
            'distinct_fingerprints': len(self.fingerprints),
            'duplicate_sends': sum(n - 1 for n in self.fingerprints.values() if n > 1),
            'init_options': self.init_options,
            'note': 'fingerprints are salted sha256 prefixes; no token is stored',
            'fingerprint_counts': self.fingerprints,
            'updated_at': time.time(),
        }
        tmp = self._path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, self._path)


_ledger_obj: _Ledger | None = None


def _ledger() -> _Ledger:
    global _ledger_obj
    if _ledger_obj is None:
        run_dir = os.path.join(EXPERIMENT_ROOT, 'run')
        os.makedirs(run_dir, exist_ok=True)
        _ledger_obj = _Ledger(os.path.join(run_dir, 'fake_fcm_sends.json'),
                              EXPERIMENT_ID)
    return _ledger_obj


def ledger() -> _Ledger:
    """Public accessor used by the reconciler and the harness tests."""
    return _ledger()


def mode() -> str:
    """'success' (default), 'unregistered', 'transient' or 'senderid'.

    Failure modes exist for a later retry/dead-state test. They are opt-in via
    ATTLT_FAKE_FIREBASE_MODE and default to success.
    """
    return (os.environ.get('ATTLT_FAKE_FIREBASE_MODE') or 'success').strip().lower()


def reset_for_tests() -> None:
    global _ledger_obj
    _ledger_obj = None
    _apps.clear()

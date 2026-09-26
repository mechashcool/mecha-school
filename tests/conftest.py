"""
pytest configuration — the fail-closed boundary for the whole test session.

This module runs in ``pytest_configure``, BEFORE pytest imports a single test
module, therefore before anything imports ``config.settings`` or ``app``.  That
ordering is the whole point: several application modules read ``os.environ``
directly **at import time**, so by the time ``create_app()`` is called it is
already too late to disable them from ``app.config``.

Concretely, the two import-time readers that made the old guard insufficient:

  * ``app/services/fcm_service.py`` calls ``_init_firebase()`` at module level.
    It reads ``FIREBASE_SERVICE_ACCOUNT_JSON`` / ``FCM_SERVICE_ACCOUNT_JSON`` /
    ``GOOGLE_APPLICATION_CREDENTIALS`` straight from ``os.environ``.  Importing
    the module is enough to initialise a live Firebase app.
  * ``app/services/redis_client.py`` reads ``REDIS_URL`` from ``os.environ`` on
    every call, never from ``app.config``.

The previous approach — write empty/sentinel values into a ``.env.test`` — was
NOT a boundary.  Two ways it failed:

  1. On Windows, setting an environment variable to the empty string *deletes*
     it, so ``load_dotenv()`` then happily restored the real production value
     from the normal ``.env`` and Firebase initialised from the live key.
  2. Even without that quirk, it depended on a file that may be stale, partly
     filled in, or absent.

So the guard here does not neutralise credentials by overwriting them with
harmless-looking values.  It:

  1. neutralises ``load_dotenv`` itself, so the normal ``.env`` is never read
     during a test run at all;
  2. *removes* every integration variable from ``os.environ``;
  3. requires an explicitly approved, local ``TEST_DATABASE_URL`` and aborts the
     session before any SQLAlchemy engine or external SDK exists;
  4. makes calling ``firebase_admin.initialize_app`` an error, and blocks
     outbound TCP to anything that is not loopback.

Nothing in here changes application behaviour outside a test run.
"""
import os
import re
import sys
from urllib.parse import urlsplit, unquote

# ── What "production" looks like ─────────────────────────────────────────────
_PROD_INDICATORS = (
    'render.com',
    'supabase.co',
    'supabase.io',
    'pooler.supabase.com',
    'neon.tech',
    '.fl0.io',
    'amazonaws.com',
    'azure.com',
    'digitalocean.com',
)

_LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1', '[::1]'}

# A test database must be *named* like one. Prevents an approved-but-wrong URL
# (e.g. someone points TEST_DATABASE_URL at a local copy of real data called
# "Mecha-School") from being accepted.
_TEST_DB_NAME_RE = re.compile(r'^[A-Za-z0-9_]+_test$')

# Every variable that could let a test reach a real external service. These are
# REMOVED from the environment, not blanked — an absent variable is the only
# state every consumer in this codebase treats as "disabled".
_INTEGRATION_VARS = (
    # Application database. Tests use TEST_DATABASE_URL and nothing else.
    'DATABASE_URL',
    # Firebase / FCM
    'FIREBASE_SERVICE_ACCOUNT_JSON', 'FCM_SERVICE_ACCOUNT_JSON',
    'GOOGLE_APPLICATION_CREDENTIALS', 'GOOGLE_CLOUD_PROJECT',
    'FIREBASE_PROJECT_ID',
    # Supabase Storage
    'SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY', 'SUPABASE_SERVICE_KEY',
    'SUPABASE_BUCKET', 'SUPABASE_STORAGE_BUCKET_MEDIA',
    'SUPABASE_PUBLIC_BRANDING_BUCKET',
    # Redis / durable queue
    'REDIS_URL', 'RATELIMIT_STORAGE_URI', 'RATELIMIT_STORAGE_URL',
    # Attendance devices and the face-recognition listener
    'HIKVISION_HOST', 'HIKVISION_USER', 'HIKVISION_PASSWORD',
    'AIFACE_WS_HOST', 'AIFACE_WS_PORT',
    # Mail / error reporting / misc webhooks
    'MAIL_SERVER', 'MAIL_USERNAME', 'MAIL_PASSWORD',
    'SENTRY_DSN', 'WEBHOOK_URL',
)

# Explicit "off" switches the application understands. Set AFTER scrubbing so a
# consumer that defaults to enabled (e.g. DURABLE_PUSH_QUEUE_ENABLED defaults to
# 'true') is switched off rather than left at its default.
_FORCED_OFF = {
    'DURABLE_PUSH_QUEUE_ENABLED': 'false',
    'PRIVATE_UPLOADS_ENABLED': 'false',
    'OBSERVABILITY_ENABLED': 'false',
    'ATTENDANCE_SCHEDULER_DISABLED': 'true',
    'FEE_REMINDER_SCHEDULER_DISABLED': 'true',
    'HIKVISION_AUTO_SYNC': 'false',
    'AIFACE_WS_ENABLED': 'false',
    'SYNC_JOURNAL_ENABLED': 'false',
    'SYNC_SIGNAL_ENABLED': 'false',
    'FLASK_ENV': 'testing',
}


def _mask(url):
    """Hide credentials before a URL is ever printed."""
    return re.sub(r'://([^:/@]+):[^@/]+@', r'://\1:***@', url or '')


class UnsafeTestEnvironment(Exception):
    """Raised when the test environment cannot be proven safe."""


# ── 1. The normal .env must never be read during tests ───────────────────────

def _neutralise_dotenv():
    """Make ``load_dotenv()`` a no-op for the rest of the process.

    ``config/settings.py`` calls ``load_dotenv()`` at import time. That file
    holds the real production DATABASE_URL and live Google credentials. Patching
    the loader — rather than trying to out-guess every value it would set — is
    what makes "tests cannot load production configuration" a property of the
    run instead of a property of a file someone has to maintain.
    """
    import dotenv
    import dotenv.main

    def _blocked(*args, **kwargs):
        return False

    dotenv.load_dotenv = _blocked
    dotenv.main.load_dotenv = _blocked
    # find_dotenv is what locates the file; make it find nothing.
    dotenv.find_dotenv = lambda *a, **k: ''
    dotenv.main.find_dotenv = lambda *a, **k: ''
    return _blocked


def _scrub_environment():
    """Remove integration credentials, then force the explicit off-switches."""
    removed = [name for name in _INTEGRATION_VARS if name in os.environ]
    for name in _INTEGRATION_VARS:
        os.environ.pop(name, None)
    for name, value in _FORCED_OFF.items():
        os.environ[name] = value
    return removed


# ── 2. The test database must be local, named like a test DB, and approved ───

def _validate_test_database_url(url, approved):
    """Return the database name, or raise ``UnsafeTestEnvironment``.

    Deliberately self-contained: it imports nothing from the application, so the
    decision to connect is made before any application code exists in the
    process.  ``config.settings.TestingConfig`` re-validates independently.
    """
    if not url or not url.strip():
        raise UnsafeTestEnvironment(
            'TEST_DATABASE_URL is not set.\n'
            '  Tests never fall back to DATABASE_URL, to .env, or to any '
            'built-in default.\n'
            '  Point TEST_DATABASE_URL at an isolated local test database and '
            'set TEST_DATABASE_APPROVED to that database name.'
        )

    lowered = url.lower()
    for indicator in _PROD_INDICATORS:
        if indicator in lowered:
            raise UnsafeTestEnvironment(
                f'TEST_DATABASE_URL looks like a hosted/production database '
                f'(matched {indicator!r}).\n  URL: {_mask(url)}'
            )

    try:
        parts = urlsplit(url)
    except Exception as exc:
        raise UnsafeTestEnvironment(f'TEST_DATABASE_URL is malformed: {exc}')

    if not parts.scheme.startswith('postgresql'):
        raise UnsafeTestEnvironment(
            f'TEST_DATABASE_URL must use a postgresql:// scheme, '
            f'got {parts.scheme!r}.'
        )

    host = (parts.hostname or '').lower()
    if host not in _LOOPBACK_HOSTS:
        raise UnsafeTestEnvironment(
            f'TEST_DATABASE_URL host {host!r} is not loopback. Tests may only '
            f'run against 127.0.0.1 / ::1 / localhost.'
        )

    name = unquote((parts.path or '').lstrip('/'))
    if not name:
        raise UnsafeTestEnvironment(
            f'TEST_DATABASE_URL names no database: {_mask(url)}'
        )
    if not _TEST_DB_NAME_RE.match(name):
        raise UnsafeTestEnvironment(
            f'Refusing to use database {name!r}: a test database name must '
            f'match {_TEST_DB_NAME_RE.pattern} (e.g. core_school_sync_test).'
        )

    # Explicit approval: the operator must name the database a second time, so
    # an inherited or copy-pasted URL alone can never authorise a run.
    if (approved or '').strip() != name:
        raise UnsafeTestEnvironment(
            f'Database {name!r} is not explicitly approved for testing.\n'
            f'  Set TEST_DATABASE_APPROVED={name} to confirm this is a '
            f'disposable, isolated test database.\n'
            f'  TEST_DATABASE_APPROVED is currently '
            f'{(approved or "<unset>")!r}.'
        )

    return name


# ── 3. Outbound SDKs and non-loopback network are blocked outright ───────────

def _block_firebase():
    """Make initialising Firebase an error during tests.

    Belt-and-braces behind the environment scrub: if a future test or fixture
    passes an explicit credential object, initialisation still cannot happen.
    ``fcm_service._init_firebase()`` catches exceptions and logs "push
    notifications disabled", so raising here degrades cleanly to FCM-off.
    """
    try:
        import firebase_admin
    except Exception:
        return False

    def _blocked(*args, **kwargs):
        raise RuntimeError(
            'firebase_admin.initialize_app() is blocked during tests '
            '(tests/conftest.py). No test may create a live Firebase app.'
        )

    firebase_admin.initialize_app = _blocked
    return True


def _block_external_network():
    """Refuse outbound TCP to anything that is not loopback.

    Only affects sockets created through Python's ``socket`` module. libpq
    (psycopg2) connects in C and is unaffected, which is fine: the database URL
    is validated separately and is required to be loopback.
    """
    import socket

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _is_local(address):
        # Non-INET families (AF_UNIX) and odd shapes are left alone.
        if not isinstance(address, tuple) or not address:
            return True
        host = str(address[0]).strip('[]').lower()
        return (host in _LOOPBACK_HOSTS or host.startswith('127.')
                or host in ('', '0.0.0.0', '::'))

    def connect(self, address):
        if not _is_local(address):
            raise OSError(
                f'Blocked outbound connection to {address!r} during tests '
                f'(tests/conftest.py). Tests must not reach external services.'
            )
        return real_connect(self, address)

    def connect_ex(self, address):
        if not _is_local(address):
            raise OSError(
                f'Blocked outbound connection to {address!r} during tests '
                f'(tests/conftest.py).'
            )
        return real_connect_ex(self, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex


# ── pytest entry point ───────────────────────────────────────────────────────

def pytest_configure(config):
    import pytest

    # Anything already imported would have read the real .env before we could
    # stop it. Fail loudly instead of running in an unknown state.
    for module in ('config.settings', 'app'):
        if module in sys.modules:
            pytest.exit(
                f'\n{module!r} was imported before tests/conftest.py could '
                f'establish the safety boundary. Aborting.\n',
                returncode=2,
            )

    _neutralise_dotenv()
    _scrub_environment()

    try:
        name = _validate_test_database_url(
            os.environ.get('TEST_DATABASE_URL'),
            os.environ.get('TEST_DATABASE_APPROVED'),
        )
    except UnsafeTestEnvironment as exc:
        pytest.exit(
            '\n'
            '╔══════════════════════════════════════════════════════════════╗\n'
            '║  BLOCKED: the test environment is not provably isolated.     ║\n'
            '╚══════════════════════════════════════════════════════════════╝\n'
            f'  {exc}\n',
            returncode=2,
        )

    # Safety net: any code path that still resolves DATABASE_URL (a stray
    # create_app('development'), a management helper) now lands on the isolated
    # test database instead of localhost:5432 or the production URL. Tests
    # themselves use TestingConfig, which reads TEST_DATABASE_URL only.
    os.environ['DATABASE_URL'] = os.environ['TEST_DATABASE_URL']

    _block_firebase()
    _block_external_network()

    config._test_database_name = name

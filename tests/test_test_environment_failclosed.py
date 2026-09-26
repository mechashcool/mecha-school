"""Part B1.1 — the test environment must fail closed.

Two separate properties are pinned here.

**The validator rejects everything that is not a proven-isolated database.**
It is exercised directly rather than by launching sub-sessions, because the
guard runs in ``pytest_configure`` and calls ``pytest.exit`` — by the time any
test body runs, the decision has already been made.  A sub-process test proves
the wiring end to end and is included as well, but the table-driven cases are
what cover the individual rejection reasons.

**Live credentials in the real .env cannot reach an integration.**  The trap
this closes is concrete: ``.env`` on this machine holds a production Supabase
``DATABASE_URL`` and a real ``GOOGLE_APPLICATION_CREDENTIALS`` path, and an
earlier version of the test setup let ``load_dotenv()`` restore both, after
which importing ``app.services.fcm_service`` initialised a live Firebase app.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import (
    UnsafeTestEnvironment,
    _validate_test_database_url,
    _INTEGRATION_VARS,
    _mask,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── The validator rejects unsafe configuration ───────────────────────────────

@pytest.mark.parametrize('url, approved, reason', [
    (None,  'x_test', 'missing'),
    ('',    'x_test', 'empty'),
    ('   ', 'x_test', 'blank'),
    # Production hosts, even when the name and approval look right.
    ('postgresql://u:p@db.abcdefgh.supabase.co:5432/core_school_sync_test',
     'core_school_sync_test', 'supabase host'),
    ('postgresql://u:p@aws-1-eu-central-1.pooler.supabase.com:6543/x_test',
     'x_test', 'supabase pooler'),
    ('postgresql://u:p@dpg-abc.oregon-postgres.render.com/x_test',
     'x_test', 'render host'),
    ('postgresql://u:p@ep-x.eu-central-1.aws.neon.tech/x_test',
     'x_test', 'neon host'),
    # Remote host that is not on the indicator list — still not loopback.
    ('postgresql://u:p@10.0.0.5:5432/core_school_sync_test',
     'core_school_sync_test', 'non-loopback host'),
    ('postgresql://u:p@db.internal:5432/core_school_sync_test',
     'core_school_sync_test', 'non-loopback hostname'),
    # Local, but not a test database name.
    ('postgresql://u:p@127.0.0.1:5432/Mecha-School', 'Mecha-School',
     'real database name'),
    ('postgresql://u:p@127.0.0.1:5432/DB1', 'DB1', 'real database name'),
    ('postgresql://u:p@localhost:5432/almuhandis_db', 'almuhandis_db',
     'development database'),
    ('postgresql://u:p@127.0.0.1:55432/', '', 'no database named'),
    # Correct-looking database, but not explicitly approved.
    ('postgresql://u:p@127.0.0.1:55432/core_school_sync_test', None,
     'approval missing'),
    ('postgresql://u:p@127.0.0.1:55432/core_school_sync_test', '',
     'approval empty'),
    ('postgresql://u:p@127.0.0.1:55432/core_school_sync_test', 'other_test',
     'approval mismatch'),
    # Wrong scheme entirely.
    ('mysql://u:p@127.0.0.1:3306/core_school_sync_test',
     'core_school_sync_test', 'wrong scheme'),
    ('sqlite:///core_school_sync_test', 'core_school_sync_test',
     'wrong scheme'),
])
def test_unsafe_test_database_url_is_rejected(url, approved, reason):
    with pytest.raises(UnsafeTestEnvironment):
        _validate_test_database_url(url, approved)


def test_the_previous_hardcoded_testing_uri_is_now_rejected():
    """TestingConfig used to hardcode this; it must no longer be acceptable.

    It targets port 5432 — the developer's normal PostgreSQL service, the same
    instance that hosts the real `Mecha-School` and `DB1` databases.
    """
    old = 'postgresql://postgres:password@localhost:5432/almuhandis_test'
    # Not approved -> rejected. Approval is the operator's explicit act.
    with pytest.raises(UnsafeTestEnvironment):
        _validate_test_database_url(old, None)


def test_a_local_approved_test_database_is_accepted():
    url = 'postgresql://u:p@127.0.0.1:55432/core_school_sync_test'
    assert _validate_test_database_url(url, 'core_school_sync_test') == \
        'core_school_sync_test'


def test_validator_never_leaks_credentials_in_its_message():
    url = 'postgresql://someuser:sup3rs3cret@db.abcdefgh.supabase.co/x_test'
    with pytest.raises(UnsafeTestEnvironment) as excinfo:
        _validate_test_database_url(url, 'x_test')
    assert 'sup3rs3cret' not in str(excinfo.value)
    assert '***' in str(excinfo.value)
    assert 'sup3rs3cret' not in _mask(url)


# ── The live session really is running on the approved database ──────────────

def test_session_runs_on_the_approved_isolated_database():
    url = os.environ['TEST_DATABASE_URL']
    name = _validate_test_database_url(
        url, os.environ.get('TEST_DATABASE_APPROVED'))
    assert name.endswith('_test')
    from urllib.parse import urlsplit
    assert urlsplit(url).port != 5432, \
        'must not use the normal PostgreSQL service port'


def test_integration_variables_are_absent_from_the_environment():
    """Scrubbed, not blanked — every consumer treats absent as disabled.

    DATABASE_URL is excluded: conftest deliberately re-points it at the test
    database afterwards as a safety net for any stray non-testing config.
    """
    still_present = [n for n in _INTEGRATION_VARS
                     if n != 'DATABASE_URL' and n in os.environ]
    assert still_present == [], f'not scrubbed: {still_present}'


def test_database_url_safety_net_points_at_the_test_database():
    assert os.environ.get('DATABASE_URL') == os.environ['TEST_DATABASE_URL']


def test_dotenv_is_neutralised_for_the_whole_session():
    """`.env` holds production values; loading it during tests must be impossible."""
    import dotenv
    assert dotenv.load_dotenv() is False
    assert dotenv.find_dotenv() == ''
    # And it really did not repopulate anything.
    assert 'GOOGLE_APPLICATION_CREDENTIALS' not in os.environ
    assert 'SUPABASE_SERVICE_ROLE_KEY' not in os.environ


def test_real_dotenv_file_still_contains_production_values():
    """Guards the guard.

    If `.env` were empty or missing, every assertion above would pass for the
    wrong reason. This proves the dangerous values genuinely exist on disk and
    are being kept out by the boundary, not by their absence.
    """
    env_file = REPO_ROOT / '.env'
    if not env_file.exists():
        pytest.skip('no .env on this machine — nothing to protect against')
    raw = env_file.read_text(encoding='utf-8', errors='replace')
    interesting = [k for k in ('DATABASE_URL', 'GOOGLE_APPLICATION_CREDENTIALS',
                               'SUPABASE_SERVICE_ROLE_KEY', 'SUPABASE_URL',
                               'FIREBASE_SERVICE_ACCOUNT_JSON')
                   if f'\n{k}=' in f'\n{raw}']
    assert interesting, '.env has none of the sensitive keys — check the guard'
    # Never print the values themselves; the key names are enough.
    assert 'DATABASE_URL' in interesting


# ── Live credentials cannot initialise an integration ────────────────────────

def test_firebase_cannot_be_initialised_during_tests():
    import firebase_admin
    import app.services.fcm_service as fcm

    assert fcm.is_enabled() is False, \
        'FCM initialised during tests — a live push could be sent'
    assert firebase_admin._apps == {}, 'a Firebase app exists during tests'

    # Even an explicit call with a credential object is refused.
    with pytest.raises(RuntimeError, match='blocked during tests'):
        firebase_admin.initialize_app()


def test_supabase_storage_is_not_configured_during_tests():
    from app import create_app
    app = create_app('testing')
    assert app.config['SUPABASE_URL'] == ''
    assert app.config['SUPABASE_SERVICE_KEY'] == ''


def test_redis_and_durable_queue_are_not_configured_during_tests():
    from app.services.redis_client import redis_configured
    assert redis_configured() is False
    from app import create_app
    app = create_app('testing')
    assert app.config['DURABLE_PUSH_QUEUE_ENABLED'] is False


def test_outbound_network_to_a_public_host_is_blocked():
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError, match='Blocked outbound connection'):
            sock.connect(('142.250.185.78', 443))     # a public address
    finally:
        sock.close()


def test_loopback_connections_still_work():
    """The guard must not break the local database or local test servers."""
    import socket
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(('127.0.0.1', 0))
    server.listen(1)
    port = server.getsockname()[1]
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.settimeout(5)
        client.connect(('127.0.0.1', port))           # must NOT raise
    finally:
        client.close()
        server.close()


def test_no_background_service_thread_is_started_under_testing():
    """create_app('testing') must start no scheduler, listener, or consumer."""
    import threading
    from app import create_app
    before = {t.name for t in threading.enumerate()}
    create_app('testing')
    started = {t.name for t in threading.enumerate()} - before
    forbidden = ('aiface', 'hikvision', 'attendance', 'fee', 'durable', 'sync')
    for name in started:
        assert not any(f in name.lower() for f in forbidden), \
            f'background service started during tests: {name}'


# ── End-to-end: a bad configuration aborts the session before connecting ─────

def _run_pytest_with(env_overrides, target='tests/test_p1_stable_urls.py'):
    env = dict(os.environ)
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        [sys.executable, '-m', 'pytest', target, '-q', '-p', 'no:cacheprovider'],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        # The abort banner is box-drawing UTF-8; the Windows default (cp1252)
        # cannot decode it and would crash the reader thread.
        encoding='utf-8', errors='replace', timeout=300,
    )


def test_session_aborts_when_test_database_url_is_missing():
    result = _run_pytest_with({'TEST_DATABASE_URL': None,
                               'TEST_DATABASE_APPROVED': None})
    assert result.returncode == 2, (result.stdout + result.stderr)[-2000:]
    assert 'not provably isolated' in (result.stdout + result.stderr)
    assert 'TEST_DATABASE_URL is not set' in (result.stdout + result.stderr)


def test_session_aborts_on_a_production_like_url():
    result = _run_pytest_with({
        'TEST_DATABASE_URL':
            'postgresql://u:p@aws-1-eu-central-1.pooler.supabase.com:6543/postgres_test',
        'TEST_DATABASE_APPROVED': 'postgres_test',
    })
    assert result.returncode == 2, (result.stdout + result.stderr)[-2000:]
    assert 'not provably isolated' in (result.stdout + result.stderr)
    assert 'p@' not in (result.stdout + result.stderr), 'password leaked into output'


def test_session_aborts_when_the_database_is_not_approved():
    result = _run_pytest_with({'TEST_DATABASE_APPROVED': None})
    assert result.returncode == 2, (result.stdout + result.stderr)[-2000:]
    assert 'not explicitly approved' in (result.stdout + result.stderr)

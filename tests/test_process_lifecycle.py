# -*- coding: utf-8 -*-
"""Process-role isolation for background services.

The production incident this guards
───────────────────────────────────
create_app() decided whether to start the shared background services from
``sys.argv[1]``:

    _cli_cmd = sys.argv[1] if len(sys.argv) >= 2 else ''
    _skip_schedulers = _cli_cmd in ('db', 'shell', 'routes', 'digest', 'collect')

Two real invocations slipped through:

  * ``python -m app.services.outbox_worker run``  → argv[1] == 'run'
    The outbox worker initialised the full application and immediately ran an
    auto-attendance scheduler tick across schools.
  * ``python -m flask --app manage db current``   → argv[1] == '--app'
    A read-only migration query logged
    "[attendance] auto-attendance scheduler started (interval=300s)".

A process now declares its role, and only ROLE_WEB may start the shared
services. These tests assert that on the real create_app(), not on a mock.

No network, no real Firebase, no production database — the app is built with
the 'testing' config against the isolated local test database, and the one
place that needs the scheduler visible patches the starter functions instead
of letting real threads run.
"""
import importlib
import os as _os
import threading
import unittest
from unittest.mock import patch

import pytest

from app import lifecycle


# Every shared background service create_app() may start, with the module and
# attribute create_app() imports it from.
SHARED_SERVICE_STARTERS = (
    ('app.services.hikvision', 'start_auto_sync'),
    ('app.services.ai_face_ws', 'start_ai_face_ws_server'),
    ('app.services.auto_attendance', 'start_auto_attendance_scheduler'),
    ('app.services.fee_reminder', 'start_fee_reminder_scheduler'),
    ('app.services.durable_queue', 'start_consumer'),
)


@pytest.fixture(autouse=True)
def _clean_role():
    """Every test starts with no declared role and leaves none behind."""
    lifecycle.reset_role_for_tests()
    yield
    lifecycle.reset_role_for_tests()


def _build_with_spies(role, *, config='testing', skip_argv=True):
    """Build the app under `role`, recording which starters were called.

    The starters are patched, so nothing real is ever started: no thread, no
    socket, no port 7788 bind, no Redis consumer. What is being measured is
    whether create_app() *would* have started them.

    `skip_argv=False` neutralises the legacy argv heuristic and app.testing so
    the lifecycle gate is the ONLY thing left that can stop the services —
    otherwise a passing test would prove nothing about the new mechanism.
    """
    from app import create_app

    called = []
    patches = []
    for mod_name, attr in SHARED_SERVICE_STARTERS:
        mod = importlib.import_module(mod_name)
        patches.append(patch.object(
            mod, attr,
            side_effect=lambda *a, _n=attr, **k: called.append(_n)))

    if role is not None:
        lifecycle.set_role(role)

    for p in patches:
        p.start()
    try:
        if skip_argv:
            app = create_app(config)
        else:
            # The honest control. TESTING must be FALSE *during* create_app()
            # (setting app.testing afterwards is too late — the gate has
            # already run), and argv[1] is 'run', exactly the value the old
            # heuristic failed to match. So neither the testing flag nor the
            # argv check can explain the result: only the role gate can.
            #
            # 'production' is safe here because tests/conftest.py has already
            # pointed DATABASE_URL at the isolated local test database.
            with patch('sys.argv', ['prog', 'run']):
                app = create_app('production')
            assert app.testing is False, (
                'this control is meaningless unless TESTING is off')
        return app, called
    finally:
        for p in patches:
            p.stop()


# ═════════════════════════════════════════════════════════════════════════════
#  1-5. The outbox worker starts nothing but its own loop
# ═════════════════════════════════════════════════════════════════════════════

def test_outbox_worker_role_starts_no_shared_services():
    _app, called = _build_with_spies(lifecycle.ROLE_OUTBOX_WORKER)
    assert called == [], f'outbox worker must start nothing, started: {called}'


def test_outbox_worker_starts_nothing_even_when_argv_and_testing_would_allow_it():
    """The lifecycle gate alone must be sufficient.

    argv[1] == 'run' is exactly what the old heuristic failed on, and
    app.testing is off — so if this passes, the role is doing the work.
    """
    _app, called = _build_with_spies(
        lifecycle.ROLE_OUTBOX_WORKER, skip_argv=False)
    assert called == [], f'the role gate must stand alone, started: {called}'


def test_outbox_worker_does_not_start_the_attendance_scheduler():
    _app, called = _build_with_spies(
        lifecycle.ROLE_OUTBOX_WORKER, skip_argv=False)
    assert 'start_auto_attendance_scheduler' not in called


def test_outbox_worker_does_not_start_aiface_or_bind_7788():
    _app, called = _build_with_spies(
        lifecycle.ROLE_OUTBOX_WORKER, skip_argv=False)
    assert 'start_ai_face_ws_server' not in called

    # And nothing in this process is listening on the AI Face port.
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(1.0)
        connected = probe.connect_ex(('127.0.0.1', 7788)) == 0
    finally:
        probe.close()
    assert not connected, 'the worker must never bind or serve port 7788'


def test_outbox_worker_does_not_start_the_durable_push_consumer():
    _app, called = _build_with_spies(
        lifecycle.ROLE_OUTBOX_WORKER, skip_argv=False)
    assert 'start_consumer' not in called


def test_outbox_worker_does_not_start_hikvision_or_fee_reminder():
    _app, called = _build_with_spies(
        lifecycle.ROLE_OUTBOX_WORKER, skip_argv=False)
    assert 'start_auto_sync' not in called
    assert 'start_fee_reminder_scheduler' not in called


def test_outbox_worker_creates_no_unrelated_background_thread():
    """Measured on real threads, with nothing patched out."""
    lifecycle.set_role(lifecycle.ROLE_OUTBOX_WORKER)
    before = {t.name for t in threading.enumerate()}

    from app import create_app
    create_app('testing')

    new = {t.name for t in threading.enumerate()} - before
    # Names create_app() gives its background threads.
    forbidden = {'aiface-ws', 'auto-attendance-scheduler',
                 'fee-reminder-scheduler', 'mecha-durable-consumer',
                 'hikvision-auto-sync'}
    assert not (new & forbidden), f'unrelated threads started: {new & forbidden}'


def test_async_dispatch_pool_is_not_started_by_app_construction():
    """The dispatch pool is lazy — constructing the app must not create it."""
    from app.services import async_dispatch
    async_dispatch._executor = None

    lifecycle.set_role(lifecycle.ROLE_OUTBOX_WORKER)
    from app import create_app
    create_app('testing')

    assert async_dispatch._executor is None, (
        'async-dispatch workers must not be created by app construction')
    assert not any(t.name.startswith('mecha-dispatch')
                   for t in threading.enumerate())


def test_worker_build_app_declares_its_role():
    """_build_app() is the real entry point the systemd unit reaches."""
    from app.services import outbox_worker
    with patch('app.create_app') as fake_create:
        outbox_worker._build_app()
    assert lifecycle.current_role() == lifecycle.ROLE_OUTBOX_WORKER
    assert fake_create.called


def test_worker_main_declares_its_role_before_doing_anything():
    from app.services import outbox_worker
    with patch.object(outbox_worker, '_cmd_status') as status:
        outbox_worker.main(['status'])
    assert lifecycle.current_role() == lifecycle.ROLE_OUTBOX_WORKER
    assert status.called


# ═════════════════════════════════════════════════════════════════════════════
#  6-7. Feature flag behaviour inside the worker
# ═════════════════════════════════════════════════════════════════════════════

def test_disabled_feature_claims_nothing(app_ctx):
    """With the flag false the worker must not touch a single row."""
    from app.models import db, NotificationOutbox
    from app.services import notification_outbox as outbox

    app, ctx = app_ctx
    app.config['INSTITUTE_ATTENDANCE_OUTBOX_ENABLED'] = False

    assert outbox.enabled() is False

    with patch.object(outbox, 'claim_batch') as claim, \
         patch.object(outbox, 'reclaim_stale') as reclaim, \
         patch.object(outbox, 'mark_sent') as sent, \
         patch.object(outbox, 'mark_retry') as retry, \
         patch.object(outbox, 'mark_dead') as dead, \
         patch('app.services.fcm_service.send_to_device_token') as send:
        from app.services import outbox_worker

        stats = outbox_worker.run_once('w-disabled', batch_size=5,
                                       lease_seconds=300, max_attempts=5)
        assert stats['disabled'] is True
        assert stats['claimed'] == 0

        # And through the full loop entry point.
        outbox_worker.run(app=app, max_loops=1, poll_seconds=0.01)

        # Nothing claimed, delivered, retried or otherwise modified.
        claim.assert_not_called()
        reclaim.assert_not_called()
        sent.assert_not_called()
        retry.assert_not_called()
        dead.assert_not_called()
        send.assert_not_called()


def test_enabled_feature_processes_a_mocked_job(app_ctx):
    """The positive control: with the flag on, a job is delivered."""
    from app.services import notification_outbox as outbox
    from app.services import outbox_worker

    app, ctx = app_ctx
    app.config['INSTITUTE_ATTENDANCE_OUTBOX_ENABLED'] = True

    assert outbox.enabled() is True

    claimed = []
    with patch.object(outbox, 'reclaim_stale', return_value=0), \
         patch.object(outbox, 'claim_batch', return_value=[]) as claim:
        stats = outbox_worker.run_once('w-test', batch_size=5,
                                       lease_seconds=300, max_attempts=5)
        claim.assert_called_once()
    assert stats['claimed'] == 0
    assert stats['sent'] == 0


# ═════════════════════════════════════════════════════════════════════════════
#  8. The web role keeps its existing lifecycle
# ═════════════════════════════════════════════════════════════════════════════

def test_web_role_still_starts_every_shared_service():
    """The whole point of the default: production must be unchanged."""
    _app, called = _build_with_spies(lifecycle.ROLE_WEB, skip_argv=False)
    assert set(called) == {name for _m, name in SHARED_SERVICE_STARTERS}, (
        f'the web process must keep starting all shared services, got {called}')


def test_undeclared_role_defaults_to_web():
    """No entry point that omits the role loses its background services."""
    assert lifecycle.current_role() == lifecycle.ROLE_WEB
    _app, called = _build_with_spies(None, skip_argv=False)
    assert set(called) == {name for _m, name in SHARED_SERVICE_STARTERS}


def _statement_line(src, pattern):
    """Line number of the first CODE line matching `pattern`.

    Comment lines are skipped deliberately: both files explain the ordering in
    prose that mentions create_app(), and matching that text would compare a
    comment against a statement.
    """
    import re
    for i, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if re.search(pattern, line):
            return i
    return None


def test_wsgi_entry_point_declares_the_web_role():
    """Gunicorn's entry point must state it explicitly."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1].joinpath('wsgi.py').read_text(
        encoding='utf-8')
    role_line = _statement_line(src, r'^\s*set_role\(\s*ROLE_WEB\s*\)')
    build_line = _statement_line(src, r'=\s*create_app\(')
    assert role_line, 'wsgi.py must call set_role(ROLE_WEB)'
    assert build_line, 'wsgi.py must construct the app'
    assert role_line < build_line, (
        'the role must be declared BEFORE the app is constructed')


def test_testing_config_never_starts_services_regardless_of_role():
    """Belt and braces: app.testing wins even under the web role."""
    _app, called = _build_with_spies(lifecycle.ROLE_WEB, skip_argv=True)
    assert called == [], f'tests must never start services, started: {called}'


# ═════════════════════════════════════════════════════════════════════════════
#  9. Flask CLI / Alembic start nothing
# ═════════════════════════════════════════════════════════════════════════════

def test_cli_role_starts_no_shared_services():
    _app, called = _build_with_spies(lifecycle.ROLE_CLI, skip_argv=False)
    assert called == [], f'CLI must start nothing, started: {called}'


def test_manage_entry_point_declares_the_cli_role():
    """`flask --app manage db current` imports manage.py — the exact command
    that logged the scheduler start in production."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1].joinpath('manage.py').read_text(
        encoding='utf-8')
    role_line = _statement_line(src, r'^\s*set_role\(\s*ROLE_CLI\s*\)')
    build_line = _statement_line(src, r'=\s*create_app\(')
    assert role_line, 'manage.py must call set_role(ROLE_CLI)'
    assert build_line, 'manage.py must construct the app'
    assert role_line < build_line, (
        'the role must be declared BEFORE the app is constructed')


# ═════════════════════════════════════════════════════════════════════════════
#  The gate itself
# ═════════════════════════════════════════════════════════════════════════════

def test_only_the_web_role_may_start_background_services():
    for role in lifecycle.ROLES:
        lifecycle.reset_role_for_tests()
        lifecycle.set_role(role)
        allowed = lifecycle.background_services_allowed()
        assert allowed == (role == lifecycle.ROLE_WEB), role


def test_role_is_readable_from_the_environment():
    """So a systemd unit can state it as defence in depth."""
    import os
    os.environ[lifecycle.ROLE_ENV_VAR] = lifecycle.ROLE_OUTBOX_WORKER
    try:
        assert lifecycle.current_role() == lifecycle.ROLE_OUTBOX_WORKER
        assert lifecycle.background_services_allowed() is False
    finally:
        os.environ.pop(lifecycle.ROLE_ENV_VAR, None)


def test_conflicting_role_change_is_refused():
    """A process cannot be flipped into 'web' after declaring itself a worker."""
    lifecycle.set_role(lifecycle.ROLE_OUTBOX_WORKER)
    kept = lifecycle.set_role(lifecycle.ROLE_WEB)
    assert kept == lifecycle.ROLE_OUTBOX_WORKER
    assert lifecycle.background_services_allowed() is False

    # Explicit override is still possible for tooling that needs it.
    assert lifecycle.set_role(lifecycle.ROLE_WEB, override=True) == \
        lifecycle.ROLE_WEB


def test_redeclaring_the_same_role_is_idempotent():
    lifecycle.set_role(lifecycle.ROLE_CLI)
    assert lifecycle.set_role(lifecycle.ROLE_CLI) == lifecycle.ROLE_CLI


def test_unknown_role_is_rejected():
    with pytest.raises(ValueError):
        lifecycle.set_role('root')


def test_unknown_environment_role_falls_back_to_web():
    """A typo must not silently disable production background services."""
    import os
    os.environ[lifecycle.ROLE_ENV_VAR] = 'webb'
    try:
        assert lifecycle.current_role() == lifecycle.ROLE_WEB
    finally:
        os.environ.pop(lifecycle.ROLE_ENV_VAR, None)


@pytest.fixture
def app_ctx():
    """An application under the outbox-worker role, with a live context."""
    from app import create_app
    lifecycle.set_role(lifecycle.ROLE_OUTBOX_WORKER)
    app = create_app('testing')
    ctx = app.app_context()
    ctx.push()
    try:
        yield app, ctx
    finally:
        ctx.pop()


# ─────────────────────────────────────────────────────────────────────────────
# The tracked systemd unit template
# ─────────────────────────────────────────────────────────────────────────────
# Second production incident, same class of defect: the worker inherited
# GOOGLE_APPLICATION_CREDENTIALS=firebase-key.json from the shared .env. A
# relative path resolves against WorkingDirectory=/var/www/mecha-school, so the
# worker loaded a stale service-account key sitting in the deploy checkout while
# the web tier used the protected /etc/mecha-school one. Firebase answered
# "invalid_grant: Invalid JWT Signature" and five outbox attempts died.
#
# The unit now pins the absolute path. These assertions run against the tracked
# file itself so a future edit cannot quietly reintroduce a relative credential.

UNIT_PATH = _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
    'deploy', 'mecha-school-outbox-worker.service')

FIREBASE_CREDENTIAL_PATH = '/etc/mecha-school/firebase-key.json'
DEPLOY_DIR = '/var/www/mecha-school'


def _unit_text():
    with open(UNIT_PATH, encoding='utf-8') as fh:
        return fh.read()


def _unit_directives():
    """Non-comment, non-blank lines only — the part systemd actually acts on."""
    out = []
    for raw in _unit_text().splitlines():
        line = raw.strip()
        if line and not line.startswith('#'):
            out.append(line)
    return out


def _unit_environment():
    """Every Environment= assignment as a dict (last assignment wins)."""
    env = {}
    for line in _unit_directives():
        if line.startswith('Environment='):
            key, _, value = line[len('Environment='):].partition('=')
            env[key.strip()] = value.strip()
    return env


def test_unit_pins_the_absolute_firebase_credential_path():
    assert _unit_environment().get('GOOGLE_APPLICATION_CREDENTIALS') == \
        FIREBASE_CREDENTIAL_PATH


def test_unit_never_uses_a_relative_firebase_credential():
    """The exact production defect. A relative value resolves against CWD."""
    text = _unit_text()
    assert 'Environment=GOOGLE_APPLICATION_CREDENTIALS=firebase-key.json' not in text
    value = _unit_environment().get('GOOGLE_APPLICATION_CREDENTIALS', '')
    assert value.startswith('/'), f'credential path must be absolute, got {value!r}'


def test_unit_loads_no_credential_from_the_deploy_checkout():
    """/var/www/mecha-school is the git checkout, never a credential store."""
    value = _unit_environment().get('GOOGLE_APPLICATION_CREDENTIALS', '')
    assert not value.startswith(DEPLOY_DIR), \
        f'credential must not live in the deploy checkout: {value!r}'
    for line in _unit_directives():
        for token in line.replace('=', ' ').split():
            if DEPLOY_DIR in token:
                assert not token.endswith('.json'), \
                    f'JSON credential referenced inside the checkout: {token!r}'
                assert 'firebase' not in token.lower(), \
                    f'Firebase credential referenced inside the checkout: {token!r}'


def test_unit_embeds_no_credential_material():
    """The key is referenced by path only — never inlined into the unit."""
    text = _unit_text()
    assert 'FIREBASE_SERVICE_ACCOUNT_JSON' not in text
    assert 'PRIVATE KEY' not in text
    assert '"private_key"' not in text


def test_unit_uses_the_dotvenv_interpreter():
    exec_start = [l for l in _unit_directives() if l.startswith('ExecStart=')]
    assert len(exec_start) == 1, exec_start
    assert f'{DEPLOY_DIR}/.venv/bin/python' in exec_start[0]
    assert f'{DEPLOY_DIR}/venv/bin/python' not in exec_start[0]


def test_unit_keeps_the_outbox_feature_disabled_by_default():
    assert _unit_environment().get('INSTITUTE_ATTENDANCE_OUTBOX_ENABLED') == 'false'


def test_unit_declares_the_outbox_worker_role():
    assert _unit_environment().get(lifecycle.ROLE_ENV_VAR) == \
        lifecycle.ROLE_OUTBOX_WORKER
    assert lifecycle.ROLE_OUTBOX_WORKER not in lifecycle._BACKGROUND_SERVICE_ROLES


def test_unit_starts_only_the_outbox_loop():
    """No web server, no scheduler, no AI Face socket in what systemd runs."""
    exec_lines = [l for l in _unit_directives()
                  if l.startswith(('ExecStart', 'ExecStartPre', 'ExecStartPost',
                                   'ExecReload'))]
    assert len(exec_lines) == 1, exec_lines
    assert exec_lines[0].endswith('-m app.services.outbox_worker run')
    directives = '\n'.join(_unit_directives()).lower()
    for forbidden in ('gunicorn', 'wsgi', 'flask run', 'hikvision',
                      'ai_face', '7788', 'celery'):
        assert forbidden not in directives, \
            f'{forbidden!r} must not appear in the worker unit directives'


if __name__ == '__main__':
    unittest.main()

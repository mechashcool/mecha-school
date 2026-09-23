# -*- coding: utf-8 -*-
"""Phase 0 — permanent FCM token classification and Firebase HTTP timeout.

Production evidence this covers (24 h on commit 6783285):

    49 FCM send failures, 48 of them error=NotRegistered, ~42 distinct token
    prefixes, and 0 "deactivated stale token" events.

Root cause: _is_stale_token lower-cased the message and looked for the marker
'unregistered'. The string 'notregistered' does not contain it — that needs a
leading "un" — so FCM's own bare spelling never matched, the row kept
is_active=True, and the same dead token was retried on every later
notification, forever, each retry costing a blocking HTTPS round trip inside an
attendance request.

Scope guarded here: classification and timeout only. No attendance logic, no
Redis, no outbox, no schema.

No database, no live network, no real Firebase, no production token.
"""
import logging

import pytest

import app.services.fcm_service as fcm


# A token long enough to prove truncation actually truncates. Fabricated —
# never a production value.
FAKE_TOKEN = 'fA1' + ('k' * 60) + 'END'
FAKE_TOKEN_2 = 'zZ9' + ('m' * 60) + 'END'


# ═════════════════════════════════════════════════════════════════════════════
#  Exception doubles
# ═════════════════════════════════════════════════════════════════════════════

def _firebase_exc(name, message, code=None):
    """Build a stand-in for a firebase-admin typed error.

    Classification keys off the exception CLASS NAME and the `code` attribute,
    exactly as the real SDK exposes them (messaging.UnregisteredError,
    messaging.SenderIdMismatchError, FirebaseError.code), so these doubles
    exercise the real structured path without importing credentials or
    touching the network.
    """
    exc_type = type(name, (Exception,), {})
    exc = exc_type(message)
    if code is not None:
        exc.code = code
    return exc


def real_messaging_errors():
    """The genuine firebase-admin classes when the package is importable."""
    try:
        from firebase_admin import messaging
        return messaging
    except Exception:            # pragma: no cover
        return None


# ═════════════════════════════════════════════════════════════════════════════
#  1-5. Permanent token failures
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('message', [
    'NotRegistered',                                   # the production string
    'NOT_REGISTERED',
    'not-registered',
    'Not Registered',
    'UNREGISTERED',
    'registration-token-not-registered',
    'Requested entity was not found.',
    'The registration token is not a valid FCM registration token',
    'SenderIdMismatch',
    'SENDER_ID_MISMATCH',
])
def test_permanent_token_messages_are_permanent(message):
    """Text fallback: every wording FCM uses for a dead token."""
    assert fcm._is_stale_token(message) is True, message
    assert fcm._is_permanent_token_failure(Exception(message)) is True, message


def test_production_notregistered_string_is_now_permanent():
    """The exact regression. Before this fix both assertions returned False."""
    assert fcm._is_stale_token('NotRegistered') is True
    assert fcm._is_permanent_token_failure(Exception('NotRegistered')) is True


@pytest.mark.parametrize('name,code', [
    ('UnregisteredError', 'NOT_FOUND'),
    ('SenderIdMismatchError', 'PERMISSION_DENIED'),
])
def test_typed_errors_are_permanent_regardless_of_wording(name, code):
    """Structured path: the CLASS decides, not the message.

    Both carry codes (NOT_FOUND / PERMISSION_DENIED) that are not token-
    specific on their own, and a message that matches no text marker — so a
    pass here can only come from the typed check.
    """
    exc = _firebase_exc(name, 'some wording we have never seen', code=code)
    assert fcm._is_permanent_token_failure(exc) is True
    assert fcm._is_stale_token(str(exc)) is False, (
        'the message alone must NOT be what classified it')


def test_real_firebase_admin_types_are_classified_permanent():
    """Against the genuine SDK classes, not only the doubles."""
    messaging = real_messaging_errors()
    if messaging is None:                    # pragma: no cover
        pytest.skip('firebase_admin not importable')
    for cls_name in ('UnregisteredError', 'SenderIdMismatchError'):
        cls = getattr(messaging, cls_name, None)
        if cls is None:                      # pragma: no cover
            pytest.skip(f'{cls_name} missing from this firebase-admin build')
        assert cls.__name__ in fcm._PERMANENT_TOKEN_EXC_NAMES, cls_name


# ═════════════════════════════════════════════════════════════════════════════
#  6-10. Transient failures must NEVER deactivate
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('exc', [
    TimeoutError('timed out'),
    ConnectionError('connection reset by peer'),
    OSError('network unreachable'),
    _firebase_exc('DeadlineExceededError', 'Deadline exceeded',
                  code='DEADLINE_EXCEEDED'),
    _firebase_exc('QuotaExceededError', '429 quota exceeded',
                  code='RESOURCE_EXHAUSTED'),
    _firebase_exc('UnavailableError', '503 Service Unavailable',
                  code='UNAVAILABLE'),
    _firebase_exc('InternalError', '500 Internal Server Error',
                  code='INTERNAL'),
    _firebase_exc('ThirdPartyAuthError', 'credential problem',
                  code='UNAUTHENTICATED'),
    _firebase_exc('FirebaseError', 'INVALID_ARGUMENT', code='INVALID_ARGUMENT'),
    _firebase_exc('FirebaseError', 'INVALID_ARGUMENT: Invalid JSON payload '
                  'received.', code='INVALID_ARGUMENT'),
    _firebase_exc('FirebaseError', 'INVALID_ARGUMENT: The size of the message '
                  'payload exceeded the maximum allowed size',
                  code='INVALID_ARGUMENT'),
])
def test_transient_failures_never_deactivate(exc):
    assert fcm._is_permanent_token_failure(exc) is False, repr(exc)


def test_invalid_argument_naming_the_token_is_still_permanent():
    """The pre-existing carve-out must survive: INVALID_ARGUMENT deactivates
    ONLY when the text also names the token as the problem."""
    exc = _firebase_exc(
        'FirebaseError',
        'INVALID_ARGUMENT: The registration token is not a valid FCM '
        'registration token', code='INVALID_ARGUMENT')
    assert fcm._is_permanent_token_failure(exc) is True


def test_classifier_never_raises():
    """A classifier that throws inside an except block would turn a delivery
    failure into a request failure."""
    class Hostile(Exception):
        def __str__(self):
            raise RuntimeError('boom')

        @property
        def code(self):
            raise RuntimeError('boom')

    assert fcm._is_permanent_token_failure(Hostile()) is False
    assert fcm._is_permanent_token_failure(None) is False


# ═════════════════════════════════════════════════════════════════════════════
#  11-13. Deactivation behaviour in send_push_to_user
# ═════════════════════════════════════════════════════════════════════════════

class FakeToken:
    def __init__(self, token, active=True):
        self.fcm_token = token
        self.is_active = active


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter_by(self, **kw):
        return self

    def all(self):
        return self._rows


def _install_fake_tokens(monkeypatch, rows, commit=None, query=None):
    """Swap the module attributes send_push_to_user imports at call time.

    ``send_push_to_user`` does ``from app.models import db, MobileDeviceToken``
    INSIDE the function, so replacing the module attributes gives it fakes
    without touching SQLAlchemy at all — no engine, no session registry, no
    application context, no database.
    """
    import app.models as models

    class FakeSession:
        def commit(self):
            if commit is not None:
                commit()

        def rollback(self):
            pass

    class FakeDb:
        session = FakeSession()

    class FakeModel:
        pass

    FakeModel.query = FakeQuery(rows) if query is None else query

    monkeypatch.setattr(models, 'MobileDeviceToken', FakeModel, raising=False)
    monkeypatch.setattr(models, 'db', FakeDb, raising=False)
    monkeypatch.setattr(fcm, '_fcm_enabled', True)


def test_only_the_failing_token_row_is_deactivated(monkeypatch):
    """One user, three devices; only the dead one loses its registration."""
    good1 = FakeToken(FAKE_TOKEN)
    dead = FakeToken(FAKE_TOKEN_2)
    good2 = FakeToken('gG7' + ('p' * 60))
    _install_fake_tokens(monkeypatch, [good1, dead, good2])

    def fake_send(token, title, body, data=None):
        if token == FAKE_TOKEN_2:
            return False, None, 'NotRegistered', True
        return True, 'msg-id', None, False

    monkeypatch.setattr(fcm, '_send_one', fake_send)

    sent, failed = fcm.send_push_to_user(7, 't', 'b', {})

    assert (sent, failed) == (2, 1)
    assert dead.is_active is False, 'the dead token must be deactivated'
    assert good1.is_active is True, 'a healthy token must survive'
    assert good2.is_active is True, 'a healthy token must survive'


def test_transient_failure_keeps_every_token_active(monkeypatch):
    a = FakeToken(FAKE_TOKEN)
    b = FakeToken(FAKE_TOKEN_2)
    _install_fake_tokens(monkeypatch, [a, b])
    monkeypatch.setattr(
        fcm, '_send_one',
        lambda token, title, body, data=None: (False, None, 'Deadline exceeded', False))

    sent, failed = fcm.send_push_to_user(7, 't', 'b', {})

    assert (sent, failed) == (0, 2)
    assert a.is_active is True and b.is_active is True, (
        'a timeout must never cost a device its token')


def test_reprocessing_the_same_permanent_failure_is_idempotent(monkeypatch):
    """Running the same dead-token send twice changes nothing the second time."""
    commits = []
    dead = FakeToken(FAKE_TOKEN_2)
    _install_fake_tokens(monkeypatch, [dead], commit=lambda: commits.append(1))
    monkeypatch.setattr(
        fcm, '_send_one',
        lambda token, title, body, data=None: (False, None, 'NotRegistered', True))

    first = fcm.send_push_to_user(7, 't', 'b', {})
    assert dead.is_active is False
    assert commits == [1]

    second = fcm.send_push_to_user(7, 't', 'b', {})
    assert first == second, 'the return contract must not drift'
    assert dead.is_active is False, 'still deactivated, not re-toggled'
    assert commits == [1], (
        'an already-inactive row must not trigger a second write')


def test_deactivation_database_failure_cannot_break_the_caller(monkeypatch):
    """A commit failure is contained: the caller still gets its counts."""
    def exploding_commit():
        raise RuntimeError('database is down')

    dead = FakeToken(FAKE_TOKEN_2)
    _install_fake_tokens(monkeypatch, [dead], commit=exploding_commit)
    monkeypatch.setattr(
        fcm, '_send_one',
        lambda token, title, body, data=None: (False, None, 'NotRegistered', True))

    sent, failed = fcm.send_push_to_user(7, 't', 'b', {})

    assert (sent, failed) == (0, 1), 'counts must still be returned truthfully'


def test_token_query_failure_is_still_contained(monkeypatch):
    """Pre-existing containment must survive this change."""
    class ExplodingQuery:
        def filter_by(self, **kw):
            raise RuntimeError('database is down')

    _install_fake_tokens(monkeypatch, [], query=ExplodingQuery())

    assert fcm.send_push_to_user(7, 't', 'b', {}) == (0, 1)


# ═════════════════════════════════════════════════════════════════════════════
#  14-15. Firebase HTTP timeout
# ═════════════════════════════════════════════════════════════════════════════

def test_default_timeout_is_ten_seconds(monkeypatch):
    monkeypatch.delenv('FCM_HTTP_TIMEOUT_SECONDS', raising=False)
    assert fcm._resolve_http_timeout() == 10.0
    assert fcm._DEFAULT_HTTP_TIMEOUT == 10.0


@pytest.mark.parametrize('raw,expected', [
    ('5', 5.0),
    ('2.5', 2.5),
    ('  30  ', 30.0),
])
def test_valid_timeout_configuration_is_used(monkeypatch, raw, expected):
    monkeypatch.setenv('FCM_HTTP_TIMEOUT_SECONDS', raw)
    assert fcm._resolve_http_timeout() == expected


@pytest.mark.parametrize('raw', [
    'abc', '', '   ', '0', '-1', '-0.5', 'nan', 'inf', '-inf', 'Infinity',
])
def test_invalid_timeout_configuration_falls_back_safely(monkeypatch, raw):
    """Zero, negative, NaN and infinity are rejected.

    Deliberate documented fallback rather than a hard failure: this module is
    imported at start-up, so raising would turn a typo in an optional tuning
    value into a full outage.
    """
    monkeypatch.setenv('FCM_HTTP_TIMEOUT_SECONDS', raw)
    value = fcm._resolve_http_timeout()
    assert value == 10.0, raw
    assert value > 0 and value == value        # not NaN


def test_installed_firebase_admin_supports_http_timeout():
    """Feature detection, not a version assumption."""
    import firebase_admin
    assert fcm._http_timeout_supported(firebase_admin) is True, (
        'httpTimeout is a documented application option and firebase_admin '
        'lists it in _CONFIG_VALID_KEYS')


def test_unsupported_build_degrades_instead_of_inventing(monkeypatch):
    """A build that explicitly dropped the option gets nothing passed."""
    class DroppedOption:
        _CONFIG_VALID_KEYS = ['projectId', 'storageBucket']

    assert fcm._http_timeout_supported(DroppedOption) is False


def test_unknown_sdk_shape_fails_open(monkeypatch):
    """requirements.txt pins 6.5.0 while a dev box may hold a newer build.

    If the probe cannot read the key list it must still pass the documented
    option: an unrecognised explicit option is ignored by the SDK, whereas
    skipping it would lose the timeout in production while tests pass locally.
    """
    assert fcm._http_timeout_supported(object()) is True

    class Hostile:
        @property
        def _CONFIG_VALID_KEYS(self):
            raise RuntimeError('boom')

    assert fcm._http_timeout_supported(Hostile()) is True


def test_firebase_receives_the_configured_timeout(monkeypatch):
    """The resolved value actually reaches initialize_app(options=...)."""
    captured = {}

    import firebase_admin

    def fake_initialize_app(cred, options=None, **kw):
        captured['options'] = options
        return object()

    monkeypatch.setenv('FCM_HTTP_TIMEOUT_SECONDS', '7')
    monkeypatch.setenv('FIREBASE_SERVICE_ACCOUNT_JSON', '{"type":"service_account"}')
    monkeypatch.delenv('GOOGLE_APPLICATION_CREDENTIALS', raising=False)
    monkeypatch.setattr(firebase_admin, 'initialize_app', fake_initialize_app)
    monkeypatch.setattr(firebase_admin.credentials, 'Certificate',
                        lambda *a, **k: object())
    monkeypatch.setattr(firebase_admin, '_apps', {}, raising=False)

    fcm._init_firebase()

    assert captured.get('options') == {'httpTimeout': 7.0}, (
        'the configured timeout must reach Firebase Admin')
    assert fcm._http_timeout_applied == 7.0


# ═════════════════════════════════════════════════════════════════════════════
#  16-17. Logging safety and unchanged success behaviour
# ═════════════════════════════════════════════════════════════════════════════

def test_no_complete_token_is_ever_logged(monkeypatch, caplog):
    """Only the existing 16-character prefix may appear."""
    dead = FakeToken(FAKE_TOKEN_2)
    _install_fake_tokens(monkeypatch, [dead])
    monkeypatch.setattr(
        fcm, '_send_one',
        lambda token, title, body, data=None: (False, None, 'NotRegistered', True))

    with caplog.at_level(logging.DEBUG, logger='mecha.fcm'):
        fcm.send_push_to_user(7, 't', 'b', {})
    text = caplog.text

    assert FAKE_TOKEN_2 not in text, 'the full token must never be logged'
    assert FAKE_TOKEN_2[:16] in text, 'the truncated prefix is still logged'
    # Nothing longer than the documented prefix leaked.
    assert FAKE_TOKEN_2[:17] not in text


def test_successful_send_behaviour_is_unchanged(monkeypatch):
    """A healthy send still returns (1, 0), logs no failure and touches
    nothing."""
    good = FakeToken(FAKE_TOKEN)
    commits = []
    _install_fake_tokens(monkeypatch, [good], commit=lambda: commits.append(1))
    monkeypatch.setattr(
        fcm, '_send_one',
        lambda token, title, body, data=None: (True, 'msg-123', None, False))

    assert fcm.send_push_to_user(7, 't', 'b', {}) == (1, 0)
    assert good.is_active is True
    assert commits == [], 'a clean send must not write to the database'


def test_disabled_service_contract_is_unchanged(monkeypatch):
    monkeypatch.setattr(fcm, '_fcm_enabled', False)
    assert fcm.send_push_to_user(7, 't', 'b', {}) == (0, 0)
    assert fcm._send_one('tok', 't', 'b', {}) == (
        False, None, 'fcm-disabled-or-missing-token', False)


def test_send_one_returns_four_elements_with_permanent_flag(monkeypatch):
    """The private contract the deactivation logic depends on."""
    monkeypatch.setattr(fcm, '_fcm_enabled', True)

    class FakeMessaging:
        Message = staticmethod(lambda **kw: object())
        Notification = staticmethod(lambda **kw: object())
        AndroidConfig = staticmethod(lambda **kw: object())
        AndroidNotification = staticmethod(lambda **kw: object())
        APNSConfig = staticmethod(lambda **kw: object())
        APNSPayload = staticmethod(lambda **kw: object())
        Aps = staticmethod(lambda **kw: object())

        @staticmethod
        def send(msg):
            raise _firebase_exc('UnregisteredError', 'NotRegistered',
                                code='NOT_FOUND')

    monkeypatch.setattr(fcm, '_messaging', FakeMessaging)

    ok, msg_id, error, permanent = fcm._send_one(FAKE_TOKEN, 't', 'b', {})
    assert ok is False and msg_id is None
    assert permanent is True
    assert 'NotRegistered' in error

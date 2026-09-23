"""
FCM multi-device push service.

Initialization priority:
1. FIREBASE_SERVICE_ACCOUNT_JSON — env var holding the full service-account JSON string
   (Render secret files / environment variables)
2. GOOGLE_APPLICATION_CREDENTIALS — file path to the service-account JSON
   (standard Google auth convention, local dev / VPS)
3. Neither → FCM disabled; all sends are silent no-ops, the app never crashes.

VPS deployment note
───────────────────
If GOOGLE_APPLICATION_CREDENTIALS=/root/firebase-key.json and Gunicorn runs as a
non-root user, the file will NOT be readable (root's home dir is mode 700).
Symptoms: "[FCM] file exists but is NOT readable" in logs, all pushes are no-ops.
Fix (choose one):
  a) chmod 640 /root/firebase-key.json && chgrp <gunicorn-group> /root/firebase-key.json
  b) cp /root/firebase-key.json /etc/mecha-school/firebase-key.json
     chmod 640 /etc/mecha-school/firebase-key.json
     update GOOGLE_APPLICATION_CREDENTIALS= in systemd service
  c) Set FIREBASE_SERVICE_ACCOUNT_JSON="$(cat /root/firebase-key.json)" in the
     systemd EnvironmentFile — the JSON string is passed directly, no file needed.

Public API:
    is_enabled() -> bool
    send_push_to_user(user_id, title, body, data=None) -> (success_count, fail_count)
"""
from __future__ import annotations

import json
import logging
import math
import os

log = logging.getLogger('mecha.fcm')

_fcm_enabled: bool = False
_messaging = None   # firebase_admin.messaging module, assigned after successful init

# Documented safe default for FCM_HTTP_TIMEOUT_SECONDS. Firebase Admin's own
# default is _http_client.DEFAULT_TIMEOUT_SECONDS = 120 s, which is exactly
# GUNICORN_TIMEOUT: one hung FCM call could occupy a worker thread right up to
# the point the master kills it. Pushes are display-only, so a much shorter
# bound is correct.
_DEFAULT_HTTP_TIMEOUT = 10.0
_http_timeout_applied = None    # the value actually handed to Firebase, or None


def _resolve_http_timeout() -> float:
    """FCM_HTTP_TIMEOUT_SECONDS as a positive finite float.

    Deliberate safe fallback rather than a hard failure: this module is
    imported at application start-up, and refusing to boot the whole web
    service over a malformed optional tuning value would turn a typo into an
    outage. The bad value is reported at ERROR level and the documented
    default is used, so the mistake is visible without being fatal.

    Rejected: non-numeric, zero, negative, NaN and infinity.
    """
    raw = os.environ.get('FCM_HTTP_TIMEOUT_SECONDS')
    if raw is None or not str(raw).strip():
        return _DEFAULT_HTTP_TIMEOUT
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        log.error('[FCM] FCM_HTTP_TIMEOUT_SECONDS=%r is not a number — '
                  'using the default %ss', raw, _DEFAULT_HTTP_TIMEOUT)
        return _DEFAULT_HTTP_TIMEOUT
    # math.isfinite rejects both NaN and ±inf; NaN also fails every comparison,
    # so the finiteness check must come first.
    if not math.isfinite(value) or value <= 0:
        log.error('[FCM] FCM_HTTP_TIMEOUT_SECONDS=%r must be a positive finite '
                  'number — using the default %ss', raw, _DEFAULT_HTTP_TIMEOUT)
        return _DEFAULT_HTTP_TIMEOUT
    return value


def _http_timeout_supported(firebase_admin_module) -> bool:
    """True when this Firebase Admin build should be given the option.

    ``httpTimeout`` is a documented application option that
    firebase_admin.messaging reads when it builds its HTTP client. Support is
    probed from the SDK's own list of valid configuration keys rather than
    assumed from a version number, and we never reach into private internals
    or patch the SDK.

    Deliberately fails OPEN. requirements.txt pins firebase-admin==6.5.0 while
    a developer machine may hold a newer build, so the probe must not be the
    single point of failure:

      * the key list exists and contains httpTimeout  -> supported;
      * the key list exists and does NOT contain it   -> the option was
        genuinely dropped, so pass nothing;
      * the key list is missing/unreadable            -> unknown SDK shape.
        Pass the documented option anyway: explicit options are stored
        verbatim on the App and an unrecognised one is ignored, so this cannot
        break initialisation — whereas skipping it would silently lose the
        timeout in production while tests pass locally.
    """
    try:
        valid_keys = getattr(firebase_admin_module, '_CONFIG_VALID_KEYS', None)
        if valid_keys is None:
            return True
        return 'httpTimeout' in valid_keys
    except Exception:
        return True


def _init_firebase() -> None:
    global _fcm_enabled, _messaging

    json_str  = os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON', '').strip()
    file_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', '').strip()

    if not json_str and not file_path:
        log.warning(
            '[FCM] DISABLED — push notifications will NOT be sent. '
            'Neither FIREBASE_SERVICE_ACCOUNT_JSON nor GOOGLE_APPLICATION_CREDENTIALS '
            'is set in the process environment. '
            'VPS fix: add one of these to the systemd EnvironmentFile and restart gunicorn. '
            'Recommended: FIREBASE_SERVICE_ACCOUNT_JSON="$(cat /path/to/firebase-key.json)" '
            'so no file-permission issues arise.'
        )
        return

    try:
        import firebase_admin
        from firebase_admin import credentials, messaging as fb_messaging
    except ImportError:
        log.warning('[FCM] firebase-admin not installed — push notifications disabled')
        return

    if firebase_admin._apps:
        # Already initialized by the legacy FCMBackend in notifications.py; reuse it.
        _messaging   = fb_messaging
        _fcm_enabled = True
        log.warning('[FCM] attaching to already-initialized Firebase app')
        return

    try:
        if json_str:
            try:
                sa_dict = json.loads(json_str)
            except ValueError as exc:
                log.error('[FCM] FIREBASE_SERVICE_ACCOUNT_JSON is set but contains '
                          'invalid JSON — push notifications disabled. Error: %s', exc)
                return
            cred   = credentials.Certificate(sa_dict)
            source = 'FIREBASE_SERVICE_ACCOUNT_JSON (env string)'
        else:
            resolved_path = file_path
            if not os.path.isfile(resolved_path):
                # A relative path (e.g. "firebase-key.json") only resolves when the
                # process CWD is the project root — which is NOT guaranteed under
                # gunicorn/Render. Fall back to resolving it against the project
                # root (three levels up from app/services/fcm_service.py) so the
                # credential is found regardless of the current working directory.
                project_root = os.path.dirname(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))))
                candidate = os.path.join(project_root, file_path)
                if os.path.isfile(candidate):
                    resolved_path = candidate
                else:
                    # File truly missing — check if it exists at all (permission issue
                    # on the parent directory can make isfile() return False even when
                    # the file physically exists, e.g. /root/ is mode 700 on VPS).
                    _uid = getattr(os, 'getuid', lambda: 'N/A')()
                    _gid = getattr(os, 'getgid', lambda: 'N/A')()
                    log.error(
                        '[FCM] GOOGLE_APPLICATION_CREDENTIALS=%r — file not found. '
                        'Process uid=%s gid=%s cwd=%r project_root=%r. '
                        'On VPS: if the file is in /root/ and Gunicorn runs as a '
                        'non-root user, /root/ is not traversable (mode 700). '
                        'Fix: copy the key to /etc/mecha-school/firebase-key.json '
                        'and update GOOGLE_APPLICATION_CREDENTIALS, OR set '
                        'FIREBASE_SERVICE_ACCOUNT_JSON with the full JSON content.',
                        file_path, _uid, _gid, os.getcwd(), project_root,
                    )
                    return

            # File path resolves — now check read permission separately.
            # os.path.isfile() can return True while os.access(R_OK) returns False
            # when the file exists but belongs to another user (e.g. root) and
            # the process has no read permission.
            if not os.access(resolved_path, os.R_OK):
                _uid = getattr(os, 'getuid', lambda: 'N/A')()
                _gid = getattr(os, 'getgid', lambda: 'N/A')()
                log.error(
                    '[FCM] GOOGLE_APPLICATION_CREDENTIALS=%r exists at %r '
                    'but is NOT readable by this process (uid=%s gid=%s). '
                    'Fix: chmod 640 %s  OR  set FIREBASE_SERVICE_ACCOUNT_JSON '
                    'with the full JSON content in the systemd EnvironmentFile.',
                    file_path, resolved_path, _uid, _gid, resolved_path,
                )
                return

            cred   = credentials.Certificate(resolved_path)
            source = f'GOOGLE_APPLICATION_CREDENTIALS ({resolved_path})'

        global _http_timeout_applied
        timeout = _resolve_http_timeout()
        if _http_timeout_supported(firebase_admin):
            firebase_admin.initialize_app(cred, options={'httpTimeout': timeout})
            _http_timeout_applied = timeout
        else:
            # Never fabricate the option or patch SDK internals: initialise
            # exactly as before and make the gap explicit in the logs.
            firebase_admin.initialize_app(cred)
            _http_timeout_applied = None
            log.warning(
                '[FCM] this firebase-admin build does not list httpTimeout as a '
                'valid application option — requests keep the SDK default '
                '(120s). FCM_HTTP_TIMEOUT_SECONDS has no effect here.')
        _messaging   = fb_messaging
        _fcm_enabled = True
        log.warning('[FCM] ENABLED — initialized from %s (http_timeout=%s)',
                    source,
                    f'{_http_timeout_applied}s' if _http_timeout_applied
                    else 'SDK default')
    except Exception as exc:
        log.error('[FCM] initialization failed (%s): %s — push notifications disabled',
                  type(exc).__name__, exc)


_init_firebase()


# ─── Public helpers ───────────────────────────────────────────────────────────

def is_enabled() -> bool:
    return _fcm_enabled


def _send_one(token: str, title: str, body: str,
              data: dict | None = None) -> tuple[bool, str | None, str | None, bool]:
    """
    Send to one FCM token.
    Returns (success, msg_id, error_str, permanent_token_failure).

    The fourth element is decided from the STRUCTURED exception before it is
    flattened to text, so classification never depends on how Firebase happens
    to word a message. It is False for every success and for every transient
    failure; only a failure that identifies this exact registration token as
    dead sets it True.

    AndroidConfig priority=high ensures the notification appears in the status
    bar even when the app is in background or completely closed.
    """
    if not _fcm_enabled or not token:
        return False, None, 'fcm-disabled-or-missing-token', False
    try:
        from app.utils.observability import observe_external
        str_data = {k: str(v) for k, v in (data or {}).items()}
        msg = _messaging.Message(
            token=token,
            notification=_messaging.Notification(title=title, body=body),
            # Android: priority=high wakes the device and shows the notification bar
            android=_messaging.AndroidConfig(
                priority='high',
                notification=_messaging.AndroidNotification(
                    sound='default',
                    channel_id='high_importance_channel',
                ),
            ),
            # iOS: badge + sound
            apns=_messaging.APNSConfig(
                payload=_messaging.APNSPayload(
                    aps=_messaging.Aps(sound='default'),
                ),
            ),
            data=str_data,
        )
        with observe_external('fcm'):
            msg_id = _messaging.send(msg)
        # P3: DEBUG — one line per DEVICE was hot-path log noise at INFO, and
        # the notification title (private school/student content) does not
        # belong in production logs. Failures below stay at ERROR.
        log.debug('[FCM] ✓ sent  token=%.16s…  msg_id=%s', token, msg_id)
        return True, msg_id, None, False
    except Exception as exc:
        permanent = _is_permanent_token_failure(exc)
        log.error('[FCM] ✗ send failed  token=%.16s…  permanent=%s  error=%s',
                  token, permanent, exc)
        return False, None, str(exc), permanent


def _normalise_error(text: str) -> str:
    """Lower-case and drop separators so wording variants compare equal.

    'NotRegistered', 'NOT_REGISTERED', 'not-registered' and 'Not Registered'
    all become 'notregistered'. Applied to the markers as well as the message,
    so the two sides can never drift apart.
    """
    return ''.join(ch for ch in str(text).lower() if ch.isalnum())


# Exceptions that identify THIS registration token as permanently dead. Named
# explicitly rather than by their parent classes: UnregisteredError is a
# NotFoundError and SenderIdMismatchError is a PermissionDeniedError, and
# neither parent means "bad token" on its own.
_PERMANENT_TOKEN_EXC_NAMES = frozenset({
    'UnregisteredError',        # NotRegistered / UNREGISTERED
    'SenderIdMismatchError',    # token belongs to a different sender/project
})

# Error codes that are ALWAYS transient — retry later, never touch the token.
# ThirdPartyAuthError/UNAUTHENTICATED is a project-credential problem, and
# INVALID_ARGUMENT usually describes the message payload, not the token.
_TRANSIENT_CODES = frozenset({
    'UNAVAILABLE', 'INTERNAL', 'DEADLINE_EXCEEDED', 'RESOURCE_EXHAUSTED',
    'ABORTED', 'CANCELLED', 'UNKNOWN', 'UNAUTHENTICATED',
})


def _is_permanent_token_failure(exc) -> bool:
    """True only when `exc` proves this one registration token is dead.

    Structured first: firebase-admin raises typed errors
    (messaging.UnregisteredError, messaging.SenderIdMismatchError) whose class
    is unambiguous. Codes known to be transient short-circuit to False so a
    Firebase outage, a quota rejection or a timeout can never cost a valid
    device its token. Only when neither applies do we fall back to the
    normalised text.

    Never raises: a classifier that throws inside an exception handler would
    turn a delivery failure into a request failure.
    """
    if exc is None:
        return False
    try:
        if isinstance(exc, _transient_network_exc_types()):
            return False                    # timeouts / connection resets

        if type(exc).__name__ in _PERMANENT_TOKEN_EXC_NAMES:
            return True

        code = getattr(exc, 'code', None)
        if isinstance(code, str) and code.upper() in _TRANSIENT_CODES:
            return False

        return _is_stale_token(str(exc))
    except Exception:       # pragma: no cover — classification must never throw
        log.warning('[FCM] could not classify send failure — '
                    'treating it as transient and keeping the token')
        return False


def _transient_network_exc_types() -> tuple:
    """Socket/HTTP exception types that are always transient.

    Resolved lazily and defensively: requests is present in this project, but a
    classifier must not depend on an import succeeding.
    """
    types = [TimeoutError, ConnectionError, OSError]
    try:
        import requests.exceptions as _rexc
        types.extend([_rexc.Timeout, _rexc.ConnectionError])
    except Exception:
        pass
    return tuple(types)


def _is_stale_token(error: str) -> bool:
    """True only for errors that UNAMBIGUOUSLY identify the registration token
    itself as dead/invalid — those rows are safe to deactivate.

    Push fix: bare 'INVALID_ARGUMENT' was previously in this list, but FCM
    returns that code for many non-token problems (payload shape/size, field
    errors, request issues). Treating it as "stale token" mass-deactivated
    VALID device rows on transient payload/API errors, and — because the app
    only re-registered tokens at login — those devices went permanently
    silent. INVALID_ARGUMENT now deactivates only when the error text also
    names the token as the problem (firebase-admin: "The registration token
    is not a valid FCM registration token").

    Token-health fix (production, 48 failures/24 h): FCM also reports a dead
    token as the bare string 'NotRegistered'. Lower-casing alone did not match
    it — 'notregistered' does not contain the marker 'unregistered', because
    that needs a leading "un" — so those tokens were never deactivated and were
    retried on every subsequent notification, forever. Comparison now strips
    separators from BOTH sides, so NotRegistered / NOT_REGISTERED /
    not-registered / 'Not Registered' all normalise to the same token and match.
    """
    if not error:
        return False
    markers = (
        'registration-token-not-registered',
        'invalid-registration-token',
        'unregistered',
        'notregistered',                    # FCM's own bare spelling
        'requested entity was not found',
        'not a valid fcm registration token',
        'senderidmismatch',                 # wrong project owns this token
    )
    el = _normalise_error(error)
    return any(_normalise_error(m) in el for m in markers)


class TokenSendResult:
    """Detailed outcome of ONE push to ONE registration token.

    Added for the durable notification outbox, which needs to know *why* a
    delivery failed — transient errors are retried, permanent token failures
    are terminal. The aggregate ``(success_count, fail_count)`` contract of
    send_push_to_user()/send_push_batch() is deliberately untouched, so every
    existing caller keeps working unchanged.
    """
    __slots__ = ('ok', 'message_id', 'error', 'permanent', 'deactivated')

    def __init__(self, ok, message_id=None, error=None, permanent=False,
                 deactivated=False):
        self.ok = ok
        self.message_id = message_id
        self.error = error              # short text, never a token
        self.permanent = permanent      # this registration is dead
        self.deactivated = deactivated  # this exact row was set inactive

    @property
    def transient(self) -> bool:
        return (not self.ok) and (not self.permanent)

    def __repr__(self):
        return (f'<TokenSendResult ok={self.ok} permanent={self.permanent} '
                f'deactivated={self.deactivated}>')


def send_to_device_token(token_row, title: str, body: str,
                         data: dict | None = None) -> TokenSendResult:
    """Send ONE notification to ONE MobileDeviceToken row. Never raises.

    The outbox stores a device-token ID, never the token string, so this is the
    seam where the row becomes an actual send. Deactivation is applied to that
    exact row only — never to the user's other devices — and is left UNCOMMITTED
    so the caller can decide the transaction boundary.

    Returns a TokenSendResult; the caller classifies retry vs terminal from
    .permanent / .transient rather than from error text.
    """
    if token_row is None:
        return TokenSendResult(False, error='missing-device-token',
                               permanent=True)
    if not _fcm_enabled:
        # Not a token problem: the service is off. Stays retryable.
        return TokenSendResult(False, error='fcm-disabled')

    ok_flag, msg_id, error, permanent = _send_one(
        token_row.fcm_token, title, body, data)

    deactivated = False
    if permanent and getattr(token_row, 'is_active', False):
        # Exactly the failing row. Idempotent: an already-inactive row is
        # skipped. Not committed here — the caller owns the transaction.
        token_row.is_active = False
        deactivated = True
        log.warning('[FCM] deactivated stale token  user_id=%s  token=%.16s…',
                    getattr(token_row, 'user_id', None), token_row.fcm_token)

    return TokenSendResult(
        ok=bool(ok_flag), message_id=msg_id,
        error=_safe_error_text(error), permanent=permanent,
        deactivated=deactivated)


def _safe_error_text(error, limit: int = 200) -> str | None:
    """A short error string safe to persist: bounded, no token, no secret.

    Firebase messages never contain the registration token, but the bound is
    enforced anyway so a pathological message cannot bloat a database column.
    """
    if not error:
        return None
    text = ' '.join(str(error).split())
    return text[:limit]


def send_push_to_user(user_id: int, title: str, body: str,
                      data: dict | None = None,
                      _role: str | None = None) -> tuple[int, int]:
    """
    Send FCM push to every active MobileDeviceToken for this user.
    Stale/invalid tokens are automatically deactivated.
    Returns (success_count, fail_count). Never raises.

    _role is an optional caller hint for log context (e.g. 'parent', 'teacher');
    it is never used for authorization — identity is always resolved from user_id.
    """
    if not _fcm_enabled:
        log.warning('[FCM] disabled — push skipped user_id=%s title=%r', user_id, title)
        return 0, 0

    # Local import — avoids circular dependency at module load time.
    from app.models import db, MobileDeviceToken

    try:
        tokens = MobileDeviceToken.query.filter_by(user_id=user_id, is_active=True).all()
    except Exception as exc:
        log.error('[FCM] failed to query device tokens for user_id=%s: %s', user_id, exc)
        # Count as a delivery failure — nothing reached this user. Returning
        # (0, 0) here made batch logs read "failed=0" while delivery silently
        # failed, hiding DB problems from the delivery accounting.
        return 0, 1

    role_tag = f'role={_role} ' if _role else ''
    if not tokens:
        # P3: INFO — a device-less user is a normal state (app not installed /
        # not yet re-registered), and in school-wide fan-outs this fired once
        # per user at WARNING. Still visible under the gunicorn 'mecha' logger.
        log.info(
            '[FCM] no active device tokens for user_id=%s %s— push skipped '
            '(user must log in to the mobile app on the VPS domain and '
            'call /auth/register-device or /me/device-token to register)',
            user_id, role_tag,
        )
        return 0, 0

    log.debug('[FCM] pushing to user_id=%s %stokens=%d',
              user_id, role_tag, len(tokens))
    success_count = fail_count = deactivated = 0

    for dt in tokens:
        ok_flag, msg_id, error, permanent = _send_one(
            dt.fcm_token, title, body, data)
        if ok_flag:
            success_count += 1
        else:
            fail_count += 1
            # Only THIS row is touched. Every other token of the same user is
            # left active, the user is untouched, and no notification history
            # is removed. Assigning False to an already-False row is a no-op,
            # so reprocessing the same failure is idempotent.
            if permanent and dt.is_active:
                dt.is_active = False
                deactivated += 1
                # Wording kept verbatim from before this fix so existing log
                # monitoring and dashboards keep matching.
                log.warning('[FCM] deactivated stale token  user_id=%s  token=%.16s…',
                            user_id, dt.fcm_token)

    if deactivated:
        # A persistence failure here must never reach the caller: the push has
        # already been attempted and attendance/notification work upstream is
        # committed. Worst case the token stays active and is re-classified on
        # the next send.
        try:
            db.session.commit()
        except Exception as exc:
            log.error('[FCM] failed to persist token deactivations (%s) — '
                      'tokens stay active and will be re-checked next send',
                      type(exc).__name__)
            try:
                db.session.rollback()
            except Exception:
                log.exception('[FCM] rollback after failed deactivation '
                              'commit also failed')

    # P3: per-user RESULT stays at WARNING only when something went wrong
    # (failure/deactivation visibility is a hard requirement); clean sends log
    # at DEBUG — the per-batch BATCH RESULT line remains the INFO-level signal.
    if fail_count or deactivated:
        log.warning('[FCM] RESULT user_id=%s %ssent=%d  failed=%d  deactivated=%d',
                    user_id, role_tag, success_count, fail_count, deactivated)
    else:
        log.debug('[FCM] RESULT user_id=%s %ssent=%d', user_id, role_tag, success_count)
    return success_count, fail_count


from app.services.durable_queue import durable_task


@durable_task('fcm.send_push_batch')
def send_push_batch(items) -> tuple[int, int]:
    """Send a batch of pushes. ``items`` is a list of primitive tuples
    ``(user_id, title, body, data)`` — never ORM objects. P3: registered as a
    durable task, so with Redis configured the batch survives worker
    recycling; the JSON round trip turns tuples into lists, which the
    per-item unpacking below accepts unchanged.

    Designed to run on the async_dispatch background thread (P0): each element
    resolves the target's own active MobileDeviceToken rows via
    send_push_to_user(), so delivery is always per-user isolated — a token can
    never receive another user's notification. Never raises.
    Returns (success_count, fail_count).
    """
    sent = failed = 0
    for item in items:
        try:
            user_id, title, body, data = item
            ok_n, fail_n = send_push_to_user(user_id, title, body, data)
            sent   += ok_n
            failed += fail_n
        except Exception as exc:
            failed += 1
            log.error('[FCM] batch item failed item=%r error=%s', item[:1], exc)
    log.warning('[FCM] BATCH RESULT items=%d sent=%d failed=%d',
                len(items), sent, failed)
    return sent, failed


def notify_investors(school_id: int, title: str, body: str,
                     data: dict | None = None) -> tuple[int, int]:
    """
    Send FCM push to every active investor_viewer user for the given school.

    school_id MUST come from the server-side Revenue/Expense object — never
    from client-supplied input.  Never raises.  Returns (success_count, fail_count).
    """
    if not _fcm_enabled:
        log.warning('[FCM] disabled — investor push skipped school_id=%s', school_id)
        return 0, 0

    from app.models import db, User, Role   # local import — avoids circular dep

    try:
        investor_role = Role.query.filter_by(name='investor_viewer').first()
        if not investor_role:
            log.info('[FCM] investor_viewer role not found — no investor push sent')
            return 0, 0

        # bypass_tenant_scope + explicit school_id filter: avoids relying on the
        # request-level ORM school scope, which may differ in some callers.
        investors = (
            User.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(
                role_id   = investor_role.id,
                school_id = school_id,
                is_active = True,
            )
            .all()
        )
    except Exception as exc:
        log.error('[FCM] investor query failed school_id=%s: %s', school_id, exc)
        return 0, 0

    if not investors:
        log.info('[FCM] no active investor_viewer for school_id=%s — push skipped', school_id)
        return 0, 0

    total_ok = total_fail = 0
    for user in investors:
        ok_c, fail_c = send_push_to_user(
            user.id, title, body, data, _role='investor_viewer')
        total_ok   += ok_c
        total_fail += fail_c

    log.warning('[FCM] investor push school_id=%s sent=%d failed=%d',
                school_id, total_ok, total_fail)
    return total_ok, total_fail

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
import os

log = logging.getLogger('mecha.fcm')

_fcm_enabled: bool = False
_messaging = None   # firebase_admin.messaging module, assigned after successful init


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

        firebase_admin.initialize_app(cred)
        _messaging   = fb_messaging
        _fcm_enabled = True
        log.warning('[FCM] ENABLED — initialized from %s', source)
    except Exception as exc:
        log.error('[FCM] initialization failed (%s): %s — push notifications disabled',
                  type(exc).__name__, exc)


_init_firebase()


# ─── Public helpers ───────────────────────────────────────────────────────────

def is_enabled() -> bool:
    return _fcm_enabled


def _send_one(token: str, title: str, body: str,
              data: dict | None = None) -> tuple[bool, str | None, str | None]:
    """
    Send to one FCM token. Returns (success, msg_id, error_str).

    AndroidConfig priority=high ensures the notification appears in the status
    bar even when the app is in background or completely closed.
    """
    if not _fcm_enabled or not token:
        return False, None, 'fcm-disabled-or-missing-token'
    try:
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
        msg_id = _messaging.send(msg)
        log.info('[FCM] ✓ sent  token=%.16s…  msg_id=%s  title=%r', token, msg_id, title)
        return True, msg_id, None
    except Exception as exc:
        log.error('[FCM] ✗ send failed  token=%.16s…  error=%s', token, exc)
        return False, None, str(exc)


def _is_stale_token(error: str) -> bool:
    if not error:
        return False
    markers = (
        'registration-token-not-registered',
        'invalid-registration-token',
        'UNREGISTERED',
        'INVALID_ARGUMENT',
        'Requested entity was not found',
    )
    el = error.lower()
    return any(m.lower() in el for m in markers)


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
        return 0, 0

    role_tag = f'role={_role} ' if _role else ''
    if not tokens:
        log.warning(
            '[FCM] no active device tokens for user_id=%s %s— push skipped '
            '(user must log in to the mobile app on the VPS domain and '
            'call /auth/register-device or /me/device-token to register)',
            user_id, role_tag,
        )
        return 0, 0

    log.warning('[FCM] pushing to user_id=%s %stokens=%d title=%r',
                user_id, role_tag, len(tokens), title)
    success_count = fail_count = deactivated = 0

    for dt in tokens:
        ok_flag, msg_id, error = _send_one(dt.fcm_token, title, body, data)
        if ok_flag:
            success_count += 1
        else:
            fail_count += 1
            if _is_stale_token(error or ''):
                dt.is_active = False
                deactivated += 1
                log.warning('[FCM] deactivated stale token  user_id=%s  token=%.16s…',
                            user_id, dt.fcm_token)

    if deactivated:
        try:
            db.session.commit()
        except Exception as exc:
            log.error('[FCM] failed to persist token deactivations: %s', exc)
            db.session.rollback()

    log.warning('[FCM] RESULT user_id=%s %ssent=%d  failed=%d  deactivated=%d',
                user_id, role_tag, success_count, fail_count, deactivated)
    return success_count, fail_count


def send_push_batch(items) -> tuple[int, int]:
    """Send a batch of pushes. ``items`` is a list of primitive tuples
    ``(user_id, title, body, data)`` — never ORM objects.

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

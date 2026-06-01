"""
FCM multi-device push service.

Initialization priority:
1. FIREBASE_SERVICE_ACCOUNT_JSON — env var holding the full service-account JSON string
   (Render secret files / environment variables)
2. GOOGLE_APPLICATION_CREDENTIALS — file path to the service-account JSON
   (standard Google auth convention, local dev)
3. Neither → FCM disabled; all sends are silent no-ops, the app never crashes.

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
            'To enable, set one of: '
            'FIREBASE_SERVICE_ACCOUNT_JSON (full JSON string, recommended for Render) '
            'or GOOGLE_APPLICATION_CREDENTIALS (file path, for local dev).'
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
            if not os.path.isfile(file_path):
                log.error('[FCM] GOOGLE_APPLICATION_CREDENTIALS is set to %r '
                          'but file does not exist — push notifications disabled.', file_path)
                return
            cred   = credentials.Certificate(file_path)
            source = f'GOOGLE_APPLICATION_CREDENTIALS ({file_path})'

        firebase_admin.initialize_app(cred)
        _messaging   = fb_messaging
        _fcm_enabled = True
        log.warning('[FCM] ENABLED — initialized from %s', source)
    except Exception as exc:
        log.error('[FCM] initialization failed (%s): %s', type(exc).__name__, exc)


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
        return True, msg_id, None
    except Exception as exc:
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
                      data: dict | None = None) -> tuple[int, int]:
    """
    Send FCM push to every active MobileDeviceToken for this user.
    Stale/invalid tokens are automatically deactivated.
    Returns (success_count, fail_count). Never raises.
    """
    if not _fcm_enabled:
        return 0, 0

    # Local import — avoids circular dependency at module load time.
    from app.models import db, MobileDeviceToken

    try:
        tokens = MobileDeviceToken.query.filter_by(user_id=user_id, is_active=True).all()
    except Exception as exc:
        log.error('[FCM] failed to query device tokens for user_id=%s: %s', user_id, exc)
        return 0, 0

    if not tokens:
        log.debug('[FCM] no active device tokens for user_id=%s — push skipped', user_id)
        return 0, 0

    log.debug('[FCM] pushing to user_id=%s — %d active token(s)', user_id, len(tokens))
    success_count = fail_count = deactivated = 0

    for dt in tokens:
        ok, msg_id, error = _send_one(dt.fcm_token, title, body, data)
        if ok:
            success_count += 1
            log.debug('[FCM] ✓ token=%.16s… msg_id=%s', dt.fcm_token, msg_id)
        else:
            fail_count += 1
            log.warning('[FCM] ✗ token=%.16s… error=%s', dt.fcm_token, error)
            if _is_stale_token(error or ''):
                dt.is_active = False
                deactivated += 1
                log.info('[FCM] deactivated stale token  user_id=%s  token=%.16s…',
                         user_id, dt.fcm_token)

    if deactivated:
        try:
            db.session.commit()
        except Exception as exc:
            log.error('[FCM] failed to persist token deactivations: %s', exc)
            db.session.rollback()

    log.warning('[FCM] user_id=%s — sent=%d  failed=%d  deactivated=%d',
               user_id, success_count, fail_count, deactivated)
    return success_count, fail_count

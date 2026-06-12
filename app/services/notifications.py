"""
Mecha-School — Notification Service
===================================
Pluggable dispatcher for parent push-notifications.

Two back-ends:
  * FCMBackend   — Firebase Cloud Messaging (HTTP v1 API)
  * DevLogBackend — writes to stdout + persists a PushNotification row.

Which back-end is used is decided at app-startup time:
  - If `FCM_SERVICE_ACCOUNT_JSON` env var points at a valid service-account
    JSON file, FCMBackend is selected.
  - Otherwise DevLogBackend is selected so local dev / CI still works.

Public API (stable — Phase 3 routes + hardware endpoint rely on this):

    NotificationService.send_to_user(user_id: int, title: str, body: str, data: dict)
    NotificationService.send_to_parents_of_student(student_id, title, body, data)
    NotificationService.broadcast(announcement_id)  # legacy disabled

Every call returns a list[PushNotification] of log rows it just persisted,
regardless of back-end, so callers always get an audit trail even in dev.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Iterable

from app.models import db, User, Student, PushNotification, parent_students

log = logging.getLogger('mecha.notifications')


# ─────────────────────────────────────────────────────────────────────────────
#  Back-ends
# ─────────────────────────────────────────────────────────────────────────────

class _Backend:
    name: str = 'base'

    def send_one(self, token: str, title: str, body: str,
                 data: dict | None = None) -> tuple[bool, str | None, str | None]:
        """
        Return (success, fcm_message_id, error_text).
        Sub-classes override.
        """
        raise NotImplementedError


class DevLogBackend(_Backend):
    name = 'devlog'

    def send_one(self, token, title, body, data=None):
        log.info('[DEV-FCM] → token=%s… title=%r body=%r data=%s',
                 (token or '')[:16], title, body, data or {})
        # Fake id so downstream code that stores it has something non-null.
        return True, f'devlog-{datetime.utcnow().timestamp():.0f}', None


class FCMBackend(_Backend):
    name = 'fcm'

    def __init__(self, service_account_path: str):
        try:
            # Lazy import — only needed when FCM is actually configured.
            import firebase_admin
            from firebase_admin import credentials, messaging
        except ImportError as e:
            log.warning('firebase_admin not installed — falling back to devlog. (%s)', e)
            self._broken = True
            return

        self._broken = False
        self._messaging = messaging
        if not firebase_admin._apps:
            cred = credentials.Certificate(service_account_path)
            firebase_admin.initialize_app(cred)

    def send_one(self, token, title, body, data=None):
        if self._broken or not token:
            return False, None, 'fcm-not-configured-or-missing-token'
        try:
            message = self._messaging.Message(
                token=token,
                notification=self._messaging.Notification(title=title, body=body),
                data={k: str(v) for k, v in (data or {}).items()},
            )
            msg_id = self._messaging.send(message)
            return True, msg_id, None
        except Exception as ex:
            log.exception('FCM send failed')
            return False, None, str(ex)


# ─────────────────────────────────────────────────────────────────────────────
#  Service facade
# ─────────────────────────────────────────────────────────────────────────────

class _NotificationService:
    def __init__(self):
        # If fcm_service has claimed Firebase, use DevLogBackend here to avoid
        # duplicate FCM sends — fcm_service handles all active device tokens.
        from app.services.fcm_service import is_enabled as _fcm_svc_enabled
        sa_path = os.environ.get('FCM_SERVICE_ACCOUNT_JSON')
        if _fcm_svc_enabled():
            self.backend: _Backend = DevLogBackend()
        elif sa_path and os.path.isfile(sa_path):
            self.backend = FCMBackend(sa_path)
        else:
            self.backend = DevLogBackend()
        log.info('NotificationService initialised with %s backend', self.backend.name)

    # ── low-level helpers ─────────────────────────────────────────────────────

    def _persist(self, user_id: int, title: str, body: str,
                 ntype: str, data: dict | None, result: tuple,
                 school_id: int | None = None):
        success, msg_id, err = result
        row = PushNotification(
            user_id        = user_id,
            school_id      = school_id,
            title          = title,
            body           = body,
            data_json      = json.dumps(data or {}, ensure_ascii=False),
            ntype          = ntype,
            status         = 'sent' if success else 'failed',
            fcm_message_id = msg_id,
            error          = err,
            sent_at        = datetime.utcnow() if success else None,
        )
        db.session.add(row)
        return row

    def send_to_user(self, user_id: int, title: str, body: str,
                     ntype: str = 'general', data: dict | None = None):
        user = User.query.get(user_id)
        if not user:
            return []
        if user.school_id is None:
            return []

        # Add Flutter routing fields if the caller didn't specify them.
        if not data or 'type' not in data:
            role_name = user.role.name if user.role else ''
            route = '/teacher/notifications' if role_name == 'teacher' else '/parent/notifications'
            data = dict(data or {})
            data.setdefault('type', 'notification')
            data.setdefault('route', route)

        token = user.device_token
        result = self.backend.send_one(token, title, body, data) \
                 if token else (False, None, 'no-device-token')
        row = self._persist(user_id, title, body, ntype, data, result,
                            school_id=user.school_id)
        db.session.commit()

        # Multi-device FCM push to all active device tokens (additional; does not
        # replace the in-app notification row above).
        try:
            from app.services.fcm_service import is_enabled, send_push_to_user
            if is_enabled():
                fcm_data = dict(data or {})
                fcm_data.setdefault('ntype', ntype)  # Flutter needs this to route to the right screen
                send_push_to_user(user_id, title, body, fcm_data)
        except Exception:
            log.exception('[FCM] multi-device push failed for user_id=%s', user_id)

        return [row]

    def send_to_users(self, user_ids: Iterable[int], title: str, body: str,
                      ntype: str = 'general', data: dict | None = None):
        rows = []
        unique_ids = set(user_ids)
        for uid in unique_ids:
            user = User.query.get(uid)
            if not user:
                continue
            if user.school_id is None:
                continue
            token = user.device_token
            result = self.backend.send_one(token, title, body, data) \
                     if token else (False, None, 'no-device-token')
            rows.append(self._persist(uid, title, body, ntype, data, result,
                                      school_id=user.school_id))
        db.session.commit()

        # Multi-device FCM push for each user in the batch.
        try:
            from app.services.fcm_service import is_enabled, send_push_to_user
            if is_enabled():
                fcm_data = dict(data or {})
                fcm_data.setdefault('ntype', ntype)  # Flutter needs this to route to the right screen
                for uid in unique_ids:
                    send_push_to_user(uid, title, body, fcm_data)
        except Exception:
            log.exception('[FCM] multi-device push failed for batch user_ids=%s', unique_ids)

        return rows

    # ── high-level helpers used by routes ─────────────────────────────────────

    def send_to_parents_of_student(self, student_id: int, title: str, body: str,
                                   ntype: str = 'general',
                                   data: dict | None = None):
        """
        Pulls every parent User linked to the given Student via `parent_students`
        and dispatches to their device_token.
        """
        student = Student.query.get(student_id)
        if not student:
            return []

        parent_ids = [row[0] for row in db.session.query(parent_students.c.user_id)
                      .filter(parent_students.c.student_id == student_id).all()]

        log.info('[attendance-notify] student_id=%s name=%s ntype=%s parents=%d',
                 student_id, student.full_name, ntype, len(parent_ids))

        if not parent_ids:
            return []

        payload = dict(data or {})
        payload.setdefault('student_id', str(student_id))
        payload.setdefault('student_name', student.full_name)
        # Flutter routing fields — Flutter reads data.type and data.route
        payload.setdefault('type', 'notification')
        payload.setdefault('route', '/parent/notifications')
        return self.send_to_users(parent_ids, title, body, ntype, payload)

    def broadcast(self, announcement_id: int):
        """Legacy Announcement broadcasts are disabled.

        Parent-facing communication is handled by in-app Notification rows.
        Announcement rows remain historical records only.
        """
        log.warning(
            'Ignoring legacy announcement broadcast request for announcement_id=%s',
            announcement_id,
        )
        return []


# Module-level singleton — import this, don't instantiate.
NotificationService = _NotificationService()

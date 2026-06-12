"""
Mobile API — Common endpoints (available to all authenticated roles)
=====================================================================
GET  /api/mobile/v1/me               current user profile + school
POST /api/mobile/v1/me/device-token  save/update the FCM push token
"""
import logging

from flask import g, request

from app.models import db, MobileDeviceToken
from app.utils.helpers import resolve_photo_url
from .utils import jwt_required, ok, err
from . import mobile_api_bp

log = logging.getLogger('mecha.mobile.common')


@mobile_api_bp.route('/me', methods=['GET'])
@jwt_required()
def me():
    """Return the authenticated user's full profile and school details."""
    user      = g.mobile_user
    role_name = user.role.name if user.role else None

    school_data = None
    if user.school:
        s = user.school
        school_data = {
            'id':            s.id,
            'name':          s.school_name,
            'name_ar':       s.school_name_ar,
            'logo':          resolve_photo_url(s.logo_path),
            'primary_color': s.primary_color,
            'currency':      s.currency_symbol,
            'currency_code': s.currency_code,
            'phone':         s.phone,
            'email':         s.email,
            'address':       s.address,
        }

    children = None
    if role_name == 'parent':
        children = [
            {
                'id':         c.id,
                'student_id': c.student_id,
                'name':       c.full_name,
                'photo':      c.photo,
                'section':    c.section.name if c.section else None,
                'grade':      c.section.grade.name if c.section and c.section.grade else None,
                'status':     c.status,
            }
            for c in user.children
        ]

    return ok(
        user={
            'id':         user.id,
            'name':       user.full_name,
            'username':   user.username,
            'email':      user.email,
            'phone':      user.phone,
            'avatar':     user.avatar,
            'role':       role_name,
            'locale':     user.locale,
            'school_id':  user.school_id,
            'last_login': user.last_login.isoformat() if user.last_login else None,
        },
        school=school_data,
        children=children,
    )


@mobile_api_bp.route('/me/device-token', methods=['POST'])
@jwt_required()
def update_device_token():
    """
    Save or update the FCM push notification token for this device.

    Request body (JSON):
      { "device_token": "<FCM registration token>", "locale": "ar",
        "platform": "android"|"ios"|"web", "device_name": "..." }

    Accepts 'device_token' (Flutter field name) or 'fcm_token' interchangeably.
    """
    user    = g.mobile_user
    payload = request.get_json(silent=True) or {}

    # Accept both Flutter's 'device_token' and the internal 'fcm_token' key.
    token = (payload.get('device_token') or payload.get('fcm_token') or '').strip()
    if not token:
        return err('device_token is required')

    platform    = (payload.get('platform') or 'android').strip()[:20]
    device_name = (payload.get('device_name') or '').strip()[:200] or None

    # Keep the legacy single-token field in sync.
    user.device_token = token
    if payload.get('locale'):
        user.locale = payload['locale'][:10]

    # Upsert into MobileDeviceToken — this is the table send_push_to_user() reads.
    # Without this, FCM pushes are never delivered because the token is never found.
    if user.school_id is not None:
        existing = MobileDeviceToken.query.filter_by(fcm_token=token).first()
        if existing:
            if existing.user_id != user.id:
                log.info('[FCM] device-token reassigned  old_user=%s new_user=%s token=%.16s…',
                         existing.user_id, user.id, token)
                existing.user_id   = user.id
                existing.school_id = user.school_id
            existing.platform    = platform
            existing.device_name = device_name
            existing.touch()
        else:
            db.session.add(MobileDeviceToken(
                user_id     = user.id,
                school_id   = user.school_id,
                fcm_token   = token,
                platform    = platform,
                device_name = device_name,
            ))
    else:
        log.warning('[FCM] device-token saved to User only — user_id=%s has no school_id', user.id)

    db.session.commit()
    log.warning('[FCM] device-token registered  user_id=%s platform=%s token=%.16s…',
                user.id, platform, token)

    return ok(message='device_token_saved')

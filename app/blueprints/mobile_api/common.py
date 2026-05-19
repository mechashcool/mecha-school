"""
Mobile API — Common endpoints (available to all authenticated roles)
=====================================================================
GET  /api/mobile/v1/me               current user profile + school
POST /api/mobile/v1/me/device-token  save/update the FCM push token
"""
from flask import g, request

from app.models import db
from .utils import jwt_required, ok, err
from . import mobile_api_bp


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
            'logo':          s.logo_path,
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
      { "device_token": "<FCM registration token>", "locale": "ar" }
    """
    user    = g.mobile_user
    payload = request.get_json(silent=True) or {}

    token = (payload.get('device_token') or '').strip()
    if not token:
        return err('device_token is required')

    user.device_token = token
    if payload.get('locale'):
        user.locale = payload['locale'][:10]
    db.session.commit()

    return ok(message='device_token_saved')

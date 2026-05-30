"""
Mobile API — Authentication
===========================
POST /api/mobile/v1/auth/login    issue access + refresh tokens
POST /api/mobile/v1/auth/refresh  issue new access token (send refresh token)
POST /api/mobile/v1/auth/logout   client-side only; server-side no-op
"""
import datetime

from flask import g, request

from app.models import db, User, Employee, MobileDeviceToken
from .utils import encode_token, jwt_required, ok, err, photo_url

# Circular import guard — routes are registered by __init__.py after the bp is created
from . import mobile_api_bp


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _school_payload(school) -> dict | None:
    if not school:
        return None
    return {
        'id':            school.id,
        'name':          school.school_name,
        'name_ar':       school.school_name_ar,
        'logo':          school.logo_path,
        'primary_color': school.primary_color,
        'currency':      school.currency_symbol,
        'currency_code': school.currency_code,
        'phone':         school.phone,
        'email':         school.email,
        'address':       school.address,
    }


def _user_payload(user: User) -> dict:
    return {
        'id':        user.id,
        'name':      user.full_name,
        'username':  user.username,
        'email':     user.email,
        'phone':     user.phone,
        'avatar':    user.avatar,
        'role':      user.role.name if user.role else None,
        'locale':    user.locale,
        'school_id': user.school_id,
    }


# ─── Login ────────────────────────────────────────────────────────────────────

@mobile_api_bp.route('/auth/login', methods=['POST'])
def login():
    """
    Request body (JSON):
      { "username": "...", "password": "..." }
      username may be a username or email address.

    Response:
      {
        "ok": true,
        "access_token":  "<JWT — expires in 24 h>",
        "refresh_token": "<JWT — expires in 30 d>",
        "token_type":    "Bearer",
        "expires_in":    86400,
        "user":   { ... },
        "school": { ... },
        "children": [ ... ],   # parent only
        "employee": { ... }    # teacher only
      }
    """
    payload = request.get_json(silent=True) or {}
    identifier = (payload.get('username') or '').strip()
    password   = (payload.get('password') or '').strip()

    if not identifier or not password:
        return err('username and password are required')

    user = User.query.filter(
        (User.username == identifier) | (User.email == identifier)
    ).first()

    if not user or not user.check_password(password):
        return err('invalid_credentials', 401)

    if not user.is_active:
        return err('account_disabled', 401)

    role_name = user.role.name if user.role else None
    if role_name not in ('parent', 'teacher'):
        return err('role_not_supported — mobile API supports parent and teacher roles only', 403)

    # Record last login
    user.last_login = datetime.datetime.utcnow()
    db.session.commit()

    access_token  = encode_token(user, 'access')
    refresh_token = encode_token(user, 'refresh')

    # Role-specific extras
    children = None
    employee = None

    if role_name == 'parent':
        children = [
            {
                'id':         s.id,
                'student_id': s.student_id,
                'name':       s.full_name,
                'photo':      photo_url(s.photo),
                'section':    s.section.name if s.section else None,
                'grade':      s.section.grade.name if s.section and s.section.grade else None,
            }
            for s in user.children
        ]

    if role_name == 'teacher':
        emp = Employee.query.filter_by(user_id=user.id).first()
        if emp:
            employee = {
                'id':          emp.id,
                'employee_id': emp.employee_id,
                'name':        emp.full_name,
                'job_title':   emp.job_title,
                'photo':       photo_url(emp.photo),
            }

    return ok(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type='Bearer',
        expires_in=86400,
        user=_user_payload(user),
        school=_school_payload(user.school),
        children=children,
        employee=employee,
    )


# ─── Register device token ───────────────────────────────────────────────────

@mobile_api_bp.route('/auth/register-device', methods=['POST'])
@jwt_required()
def register_device():
    """
    Register or refresh an FCM device token for the authenticated user.

    Request body (JSON):
      {
        "fcm_token":   "<Firebase Cloud Messaging token>",   required
        "platform":    "android" | "ios" | "web",            optional — default "android"
        "device_name": "Samsung Galaxy S24"                  optional — free text label
      }

    Behaviour:
      • Token already registered to THIS user  → touch last_seen_at, update
        platform/device_name, re-activate.
      • Token registered to a DIFFERENT user   → reassign to current user
        (covers app reinstall / device handover scenarios).
      • Token not seen before                  → create a new row.

    Also writes the token to User.device_token so the existing FCM notification
    service (which reads that field) keeps working without changes.
    """
    user    = g.mobile_user
    payload = request.get_json(silent=True) or {}
    token   = (payload.get('fcm_token') or '').strip()

    if not token:
        return err('fcm_token is required')

    platform    = (payload.get('platform')    or 'android').strip()[:20]
    device_name = (payload.get('device_name') or '').strip()[:200] or None

    existing = MobileDeviceToken.query.filter_by(fcm_token=token).first()

    if existing:
        # Reassign if owned by a different user (device transfer / reinstall)
        if existing.user_id != user.id:
            existing.user_id   = user.id
            existing.school_id = user.school_id
        existing.platform    = platform
        existing.device_name = device_name
        existing.touch()
    else:
        existing = MobileDeviceToken(
            user_id     = user.id,
            school_id   = user.school_id,
            fcm_token   = token,
            platform    = platform,
            device_name = device_name,
        )
        db.session.add(existing)

    # Keep legacy single-token field in sync so existing notification service works
    user.device_token = token

    db.session.commit()

    return ok(
        message='device_registered',
        device={
            'platform':     existing.platform,
            'device_name':  existing.device_name,
            'last_seen_at': existing.last_seen_at.isoformat() if existing.last_seen_at else None,
        },
    )


# ─── Token refresh ────────────────────────────────────────────────────────────

@mobile_api_bp.route('/auth/refresh', methods=['POST'])
@jwt_required('refresh')
def refresh():
    """
    Authorization: Bearer <refresh_token>
    Returns a fresh access token. The refresh token itself is unchanged.
    """
    new_access = encode_token(g.mobile_user, 'access')
    return ok(
        access_token=new_access,
        token_type='Bearer',
        expires_in=86400,
    )


# ─── Logout ───────────────────────────────────────────────────────────────────

@mobile_api_bp.route('/auth/logout', methods=['POST'])
@jwt_required()
def logout():
    """
    Stateless logout. The client must discard both tokens.
    Server-side: no-op. Add a token blacklist table here in the future if needed.
    """
    return ok(message='logged_out')

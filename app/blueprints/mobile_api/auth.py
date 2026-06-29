"""
Mobile API — Authentication
===========================
POST /api/mobile/v1/auth/login    issue access + refresh tokens
POST /api/mobile/v1/auth/refresh  issue new access token (send refresh token)
POST /api/mobile/v1/auth/logout   client-side only; server-side no-op
"""
import datetime

from flask import g, jsonify, request

from app.models import db, User, Employee, MobileDeviceToken
from app.utils.login_throttle import check_lockout, record_failed_attempt, reset_attempts, format_wait_ar
from app.utils.ratelimit import limiter, LOGIN_RATE_LIMIT
from .utils import encode_token, jwt_required, ok, err, photo_url

# Circular import guard — routes are registered by __init__.py after the bp is created
from . import mobile_api_bp


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _lockout_response(wait_seconds: int):
    """Build the standard LOGIN_LOCKED 429 response."""
    retry_minutes = (wait_seconds + 59) // 60
    locked_until = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(seconds=wait_seconds)
    ).isoformat()
    return jsonify({
        'ok':                  False,
        'error':               'LOGIN_LOCKED',
        'message':             (
            f'تجاوزت الحد المسموح لمحاولات تسجيل الدخول. '
            f'يرجى المحاولة {format_wait_ar(wait_seconds)}.'
        ),
        'remaining_seconds':   wait_seconds,
        'retry_after_seconds': wait_seconds,
        'retry_after_minutes': retry_minutes,
        'locked_until':        locked_until,
        'wait_seconds':        wait_seconds,
    }), 429


def _school_payload(school) -> dict | None:
    if not school:
        return None
    return {
        'id':            school.id,
        'name':          school.school_name,
        'name_ar':       school.school_name_ar,
        'logo':          photo_url(school.logo_path),
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
@limiter.limit(LOGIN_RATE_LIMIT)
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
    payload    = request.get_json(silent=True) or {}
    identifier = (payload.get('username') or '').strip()
    password   = (payload.get('password') or '').strip()

    if not identifier or not password:
        return err('username and password are required')

    ip = request.remote_addr or '0.0.0.0'

    # Progressive lockout — evaluated before any DB query so locked-out
    # requests cost nothing and cannot be used to time-attack username existence.
    locked, wait_seconds = check_lockout(ip, identifier)
    if locked:
        return _lockout_response(wait_seconds)

    user = User.query.filter(
        (User.username == identifier) | (User.email == identifier)
    ).first()

    if not user or not user.check_password(password):
        # Record against (IP, identifier) regardless of whether the username
        # exists — same counter, same error, prevents username enumeration.
        record_failed_attempt(ip, identifier)
        # If this attempt crossed a lockout threshold the lock key was just
        # created; return 429 immediately so the Nth attempt itself triggers
        # the lockout response rather than the (N+1)th request.
        locked, wait_seconds = check_lockout(ip, identifier)
        if locked:
            return _lockout_response(wait_seconds)
        return err('invalid_credentials', 401)

    # Credentials are valid — clear the throttle counter before any further
    # business-logic checks so a disabled or wrong-role user is not re-penalised.
    reset_attempts(ip, identifier)

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
@limiter.limit(LOGIN_RATE_LIMIT)
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

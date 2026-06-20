"""
Mobile API — JWT utilities and shared helpers.

Token design:
  access  token — 24 h — used on every protected request
  refresh token — 30 d — used only to get a new access token

Payload keys: sub (user_id), school_id, role, type, iat, exp

ORM tenant scoping: after validating the token we call flask_login.login_user()
so current_user is set for the request. This lets the existing ORM scoping
middleware (app/utils/scoping.py) work transparently — no bypass needed.
"""
import datetime
from functools import wraps

import jwt as pyjwt
from flask import current_app, g, jsonify, request
from flask_login import login_user

from app.models import User, db


# ─── Token helpers ────────────────────────────────────────────────────────────

def _secret() -> str:
    return current_app.config.get('JWT_SECRET_KEY') or current_app.config['SECRET_KEY']


def encode_token(user: User, token_type: str = 'access') -> str:
    """Issue a signed HS256 JWT for the given user."""
    now = datetime.datetime.utcnow()
    ttl = datetime.timedelta(days=30) if token_type == 'refresh' else datetime.timedelta(hours=24)
    payload = {
        'sub':       str(user.id),   # JWT spec requires sub to be a string
        'school_id': user.school_id,
        'role':      user.role.name if user.role else None,
        'type':      token_type,
        'iat':       now,
        'exp':       now + ttl,
    }
    return pyjwt.encode(payload, _secret(), algorithm='HS256')


def decode_token(raw: str) -> dict:
    """Decode and validate a JWT. Raises pyjwt.exceptions.* on failure."""
    return pyjwt.decode(raw, _secret(), algorithms=['HS256'])


# ─── Decorators ───────────────────────────────────────────────────────────────

def jwt_required(token_type: str = 'access'):
    """
    Validate the Bearer token in the Authorization header.
    On success:
      - Sets g.mobile_user  (the User ORM object)
      - Calls login_user(user) so current_user is populated
      - Calls set_mobile_request_scope(user) so ORM school/year filtering
        is applied for the rest of the request (school_id is taken from the
        server-side User row, never from client-supplied token claims)
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            header = request.headers.get('Authorization', '')
            if not header.startswith('Bearer '):
                return jsonify({'ok': False, 'error': 'missing_token'}), 401

            raw_token = header[7:].strip()
            try:
                payload = decode_token(raw_token)
            except pyjwt.ExpiredSignatureError:
                return jsonify({'ok': False, 'error': 'token_expired'}), 401
            except pyjwt.InvalidTokenError:
                return jsonify({'ok': False, 'error': 'invalid_token'}), 401

            if payload.get('type') != token_type:
                return jsonify({'ok': False, 'error': 'wrong_token_type'}), 401

            user = db.session.get(User, int(payload['sub']))
            if not user or not user.is_active:
                return jsonify({'ok': False, 'error': 'user_inactive'}), 401

            login_user(user, remember=False)

            # Set school/year scope from the authenticated User so all subsequent
            # ORM queries in this request receive the correct tenant filters.
            # _set_request_scope() (before_request) skips mobile paths and caches
            # nothing; we set it here after auth so the scope is non-None.
            from app.utils.scoping import set_mobile_request_scope
            set_mobile_request_scope(user)

            g.mobile_user    = user
            g.mobile_payload = payload
            return f(*args, **kwargs)
        return wrapper
    return decorator


def role_required(*roles: str):
    """
    Check that g.mobile_user.role.name is in the allowed roles list.
    Must be stacked AFTER @jwt_required().
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = g.get('mobile_user')
            if not user or not user.role or user.role.name not in roles:
                return jsonify({'ok': False, 'error': 'forbidden'}), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator


# ─── Photo URL helper ─────────────────────────────────────────────────────────

def photo_url(photo: str | None) -> str | None:
    """
    Return an absolute URL for a stored photo value, safe to embed in JSON.

    - Supabase / CDN URLs (http/https) → returned as-is.
    - Legacy relative paths → absolute URL built from the current request host,
      only when the file exists on disk (avoids dead links after Render redeploys).
    - None / empty / missing file → None (mobile client shows local placeholder).
    """
    if not photo:
        return None
    if photo.startswith(('http://', 'https://')):
        return photo
    import os
    try:
        full_path = os.path.join(current_app.root_path, 'static', photo)
        if not os.path.isfile(full_path):
            return None
        return request.host_url.rstrip('/') + '/static/' + photo
    except Exception:
        return None


# ─── Response helpers ─────────────────────────────────────────────────────────

def ok(**kwargs):
    """Return a standard success JSON response."""
    return jsonify({'ok': True, **kwargs})


def err(message: str, status: int = 400):
    """Return a standard error JSON response."""
    return jsonify({'ok': False, 'error': message}), status

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
from flask import current_app, g, jsonify, request, url_for
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

def photo_url(photo: str | None, *, want_video: bool = False) -> str | None:
    """
    Return an absolute URL for a stored photo / media value, safe for JSON.

    - Supabase / CDN URLs (http/https) → returned as-is (external pass-through).
    - Locally-stored uploads ('uploads/...') → absolute URL served by the Flask
      `media` blueprint (/media/uploads/...), only when the file exists on disk.
      The media route is reachable through the reverse proxy regardless of how
      the proxy maps the /static/ location, so newly uploaded files load even
      when nginx's /static/ alias does not point at app/static/uploads/.
    - None / empty / missing file → None (mobile client shows local placeholder).

    ``want_video``: when the value is a private Supabase upload, mint a
    Supabase-native signed CDN URL (which supports HTTP Range/seeking) instead of
    the Flask /media-proxy URL (which streams the whole file with no Range). Set
    it for video media so mobile players can seek; leave it False for images and
    thumbnails. Has no effect on external or public-branding URLs.

    The scheme is taken from PREFERRED_URL_SCHEME (config) — 'https' in
    production, 'http' in local dev — so the URL is HTTPS behind the VPS proxy
    even though Flask itself receives plain HTTP from nginx.
    """
    if not photo:
        return None
    # Stage 2: when private uploads are on, resolve any mappable upload (full
    # Supabase URL OR relative uploads/… path) to a signed URL the app can open
    # without an auth header; public branding stays a public URL. Non-mappable
    # values fall through to the legacy local logic unchanged.
    if current_app.config.get('PRIVATE_UPLOADS_ENABLED'):
        from app.utils.upload_access import supabase_media_url
        signed = supabase_media_url(photo, want_video=want_video)
        if signed is not None:
            return signed
    if photo.startswith(('http://', 'https://')):
        return photo
    import os
    try:
        # Normalize: drop any leading slash and an optional 'static/' prefix so
        # `rel` is always a path relative to the Flask static folder.
        rel = photo.lstrip('/')
        if rel.startswith('static/'):
            rel = rel[len('static/'):]

        full_path = os.path.join(current_app.root_path, 'static', rel)
        if not os.path.isfile(full_path):
            return None

        scheme = current_app.config.get('PREFERRED_URL_SCHEME', 'https')

        # Uploaded media → served by the media blueprint (proxy-independent).
        if rel.startswith('uploads/'):
            return url_for('media.serve', stored=rel, _external=True, _scheme=scheme)
        # Any other static asset (rare in mobile payloads) keeps the /static URL.
        return url_for('static', filename=rel, _external=True, _scheme=scheme)
    except Exception:
        return None


# ─── Pagination helper (P3) ───────────────────────────────────────────────────

def page_args(default_limit: int = 50, max_limit: int = 100):
    """Parse ?limit= and ?offset= safely: bounded and crash-proof.

    Replaces the raw ``int(request.args.get(...))`` pattern, which returned a
    500 on non-numeric input and let a negative limit reach Postgres (SQL
    error → 500). Behaviour for well-formed input is unchanged:
      * valid 0 ≤ limit ≤ max_limit → used as-is (limit=0 still means "empty
        page", preserving the existing contract),
      * limit > max_limit → clamped to max_limit (as before),
      * non-numeric / negative limit → default_limit,
      * non-numeric / negative offset → 0.
    """
    try:
        limit = int(request.args.get('limit', default_limit))
    except (TypeError, ValueError):
        limit = default_limit
    if limit < 0:
        limit = default_limit
    try:
        offset = int(request.args.get('offset', 0))
    except (TypeError, ValueError):
        offset = 0
    return min(limit, max_limit), max(offset, 0)


# ─── Response helpers ─────────────────────────────────────────────────────────

def ok(**kwargs):
    """Return a standard success JSON response."""
    return jsonify({'ok': True, **kwargs})


def ok_etag(**kwargs):
    """Standard success JSON response with HTTP validation (P2).

    Computes a strong ETag over the exact response bytes and answers 304 (no
    body) when the client's If-None-Match matches. Because the payload is
    built AFTER authentication/authorization and the ETag is derived from that
    per-user payload, a 304 can never disclose anything the 200 would not.
    Cache-Control is ``private, no-cache``: shared caches must not store it,
    and clients must revalidate every time — clients that never send
    If-None-Match (the current app) get a normal 200, byte-identical to ok().
    """
    import hashlib

    resp = jsonify({'ok': True, **kwargs})
    etag = hashlib.sha256(resp.get_data()).hexdigest()[:32]
    resp.set_etag(etag)
    resp.headers['Cache-Control'] = 'private, no-cache'
    resp = resp.make_conditional(request)
    if resp.status_code == 304:
        # Werkzeug strips the entity only at WSGI send time; drop it here too
        # so the saved retransmission is guaranteed at every layer.
        resp.set_data(b'')
    return resp


def err(message: str, status: int = 400):
    """Return a standard error JSON response."""
    return jsonify({'ok': False, 'error': message}), status

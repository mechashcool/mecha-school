"""
Media Blueprint  —  /media/uploads/...
======================================
Serves locally-stored uploaded files (student/employee photos, homework and
leave attachments, School Board images/videos, …) directly from the Flask
application.

Why this exists
───────────────
Uploads are written by ``save_uploaded_file()`` to ``app/static/uploads/...``
(relative to ``current_app.root_path``). In some deployments the reverse proxy
(nginx) maps the ``/static/`` location to a different directory than the Flask
package's ``app/static/`` folder, so a freshly uploaded file is present on disk
but is NOT reachable at ``/static/uploads/...`` → the browser/app gets a 404.

This blueprint exposes the same files under ``/media/uploads/...``. That path is
NOT under ``/static/``, so the reverse proxy forwards it to Gunicorn/Flask like
any other dynamic route, and Flask serves the file from the exact directory it
was written to. This removes the dependency on an nginx static alias being
configured for ``app/static/uploads/``.

Security
────────
• GET only, public — identical exposure to the existing public ``/static/``
  serving of the same files (no new data is exposed). UUID filenames are not
  enumerable.
• Only the ``uploads/`` subtree of the static folder is served; any other path
  returns 404.
• ``send_from_directory`` uses ``werkzeug.safe_join`` which blocks path
  traversal (``..``) and absolute paths.
"""
import os

from flask import (Blueprint, current_app, send_from_directory, abort,
                   redirect, url_for, request)
from flask_login import current_user

media_bp = Blueprint('media', __name__)

# Only files under this prefix (relative to app/static/) are served.
_ALLOWED_PREFIX = 'uploads/'


@media_bp.route('/media/<path:stored>')
def serve(stored):
    """Serve a locally-stored upload from ``app/static/<stored>``.

    ``stored`` is the exact relative value persisted by ``save_uploaded_file()``,
    e.g. ``uploads/schools/3/board/media/<uuid>.mp4``. Only the ``uploads/``
    subtree is exposed; everything else returns 404. ``conditional=True`` enables
    ETag / HTTP Range support so video players can seek.

    Security (H-2): this route is unauthenticated. When ``PRIVATE_UPLOADS_ENABLED``
    is on it must NOT stream private local uploads (student/employee photos &
    documents, homework, complaint/leave attachments, board media) straight off
    the local disk — that would bypass ``/files`` and ``/media-proxy``. Instead
    each private value is upgraded to the same signed ``/media-proxy`` flow those
    routes already use, so access requires a valid short-lived HMAC token. Public
    branding/identity assets are non-personal and required before login, so they
    stay directly servable (identical to ``serve_protected``). When the flag is
    off, legacy direct local serving is preserved byte-for-byte.
    """
    if not stored.startswith(_ALLOWED_PREFIX):
        abort(404)

    # ── H-2: close the unauthenticated private-file hole when the feature is on ──
    if current_app.config.get('PRIVATE_UPLOADS_ENABLED'):
        from app.utils.upload_access import (
            object_path_of, is_public_upload, supabase_media_url,
        )
        op = object_path_of(stored) or stored
        if not is_public_upload(op):
            # Route through the signed proxy / public-branding resolver — same
            # protected model as /files/ and /media-proxy/. supabase_media_url
            # returns a signed /media-proxy URL for private local uploads and a
            # public URL for identity objects that live in a public bucket.
            signed = supabase_media_url(op)
            if signed:
                return redirect(signed)
            # A private value that cannot be turned into a signed URL must be
            # denied rather than streamed unauthenticated from local disk.
            abort(404)

    static_root = os.path.join(current_app.root_path, 'static')
    # send_from_directory raises 404 (NotFound) if the file does not exist and
    # blocks traversal via safe_join.
    return send_from_directory(static_root, stored, max_age=604800,
                               conditional=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Protected file serving  —  /files/<path:stored>   (Stage 1: private-by-default)
# ─────────────────────────────────────────────────────────────────────────────

def _serve_local(object_path):
    """Serve a locally-stored upload from app/static/<object_path> with no shared
    caching (private content). safe_join blocks traversal; 404 if missing."""
    static_root = os.path.join(current_app.root_path, 'static')
    return send_from_directory(static_root, object_path, max_age=0,
                               conditional=True)


@media_bp.route('/files/<path:stored>')
def serve_protected(stored):
    """Authenticated / signed access to a locally-stored uploaded file.

    Policy (see app/utils/upload_access.py):
      * PUBLIC branding/identity assets are served without authentication so
        login-page and pre-auth branding never break.
      * PRIVATE files require either a valid short-lived signed token (query
        string) OR an authenticated web session that passes school / role /
        relationship authorisation. Otherwise 401 (redirect to login) or 403.

    This route only serves files Flask holds on the local filesystem. Values
    stored as external Supabase / CDN URLs are never routed here (templates link
    them directly); that path is closed in Stage 2 (private bucket + signed URLs).
    """
    from app.utils.upload_access import (
        object_path_of, is_public_upload, can_access_upload, verify_signed_token,
    )

    op = object_path_of(stored)
    if op is None or not op.startswith(_ALLOWED_PREFIX):
        abort(404)

    # Public branding — never over-block.
    if is_public_upload(op):
        return _serve_local(op)

    # Feature ON → upgrade any residual legacy /files/ link (bookmarks, cached
    # pages, older records) to the signed /media-proxy secure flow, so nothing is
    # served through this unsigned route while private uploads are enabled.
    if current_app.config.get('PRIVATE_UPLOADS_ENABLED'):
        from app.utils.upload_access import supabase_media_url
        signed = supabase_media_url(op)
        if signed:
            return redirect(signed)

    # Private — accept a valid signed token first (credential-less clients).
    if verify_signed_token(op, request.args.get('exp'), request.args.get('sig')):
        return _serve_local(op)

    # Otherwise require an authenticated, authorised web session.
    if not current_user.is_authenticated:
        return redirect(url_for('auth.login', next=request.full_path))
    if not can_access_upload(current_user, op):
        abort(403)
    return _serve_local(op)


@media_bp.route('/media-proxy/<bucket>/<path:object_path>')
def serve_remote(bucket, object_path):
    """Stream a small PRIVATE Supabase object (Stage 2, hybrid signing).

    Authorisation is the short-lived HMAC token in the query string, minted
    server-side (app/utils/upload_access.signed_proxy_url) at the moment a
    web/mobile request that was already authenticated + authorised for the owning
    record rendered the link. The object bytes are fetched with the service key
    and streamed back with private, no-share cache headers. Board media is NOT
    served here — it uses Supabase-native signed URLs.
    """
    from flask import Response
    from app.utils.upload_access import verify_remote_token
    from app.utils.helpers import _supabase_fetch

    # Defense-in-depth (the HMAC token already binds bucket+key, but never trust
    # a URL): only the two known private buckets, and no path traversal / absolute
    # keys. object_path is the key INSIDE the bucket — it must not start with the
    # bucket name or contain '..'.
    allowed_buckets = {
        current_app.config.get('SUPABASE_BUCKET', 'uploads'),
        current_app.config.get('SUPABASE_STORAGE_BUCKET_MEDIA', 'school-media'),
    }
    if bucket not in allowed_buckets:
        abort(404)
    if object_path.startswith('/') or '..' in object_path.split('/'):
        abort(404)

    if not verify_remote_token(bucket, object_path,
                               request.args.get('exp'), request.args.get('sig')):
        abort(403)

    data, ctype = _supabase_fetch(object_path, bucket=bucket)
    if data is None:
        # Local-disk fallback: files not (yet) in Supabase — legacy/local rows or
        # a failed Supabase upload — still stream through this signed, secure
        # route instead of 404ing. Only the uploads bucket maps to the local
        # static/uploads tree; safe_join (via _serve_local) blocks traversal.
        if bucket == current_app.config.get('SUPABASE_BUCKET', 'uploads'):
            local_rel = f"{_ALLOWED_PREFIX}{object_path}"
            candidate = os.path.join(current_app.root_path, 'static',
                                     *local_rel.split('/'))
            if os.path.isfile(candidate):
                return _serve_local(local_rel)
        abort(404)

    resp = Response(data, mimetype=ctype or 'application/octet-stream')
    resp.headers['Cache-Control'] = 'private, max-age=300'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp

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

from flask import Blueprint, current_app, send_from_directory, abort

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
    """
    if not stored.startswith(_ALLOWED_PREFIX):
        abort(404)
    static_root = os.path.join(current_app.root_path, 'static')
    # send_from_directory raises 404 (NotFound) if the file does not exist and
    # blocks traversal via safe_join.
    return send_from_directory(static_root, stored, max_age=604800,
                               conditional=True)

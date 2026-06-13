"""
Al-Muhandis – General utility functions
"""
import os
import uuid
from datetime import datetime
from flask import current_app, url_for
from werkzeug.utils import secure_filename


# NOTE: 'svg' is intentionally excluded — SVG files can embed <script> and are
# an XSS vector when served from the same origin. Use raster formats instead.
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_DOC_EXTENSIONS   = {'pdf', 'doc', 'docx', 'xls', 'xlsx'}
LOGO_IMAGE_EXTENSIONS    = {'png', 'jpg', 'jpeg', 'webp'}
LOGO_MAX_BYTES           = 2 * 1024 * 1024  # 2 MB

ALLOWED_BOARD_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}
ALLOWED_BOARD_VIDEO_EXTENSIONS = {'mp4', 'mov', 'webm'}
BOARD_IMAGE_MAX_BYTES          = 5  * 1024 * 1024   # 5 MB
BOARD_VIDEO_MAX_BYTES          = 50 * 1024 * 1024   # 50 MB

_CONTENT_TYPES = {
    'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
    'gif': 'image/gif', 'webp': 'image/webp',
    'pdf': 'application/pdf', 'doc': 'application/msword',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'mp4': 'video/mp4', 'mov': 'video/quicktime', 'webm': 'video/webm',
}


def allowed_file(filename, extensions=None):
    if extensions is None:
        extensions = current_app.config.get('ALLOWED_EXTENSIONS', set())
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in extensions


def _supabase_upload(file_bytes: bytes, object_path: str, content_type: str,
                     bucket: str | None = None) -> str | None:
    """Upload bytes to Supabase Storage; return the public URL or None on failure."""
    import requests as _req
    url = current_app.config.get('SUPABASE_URL', '').rstrip('/')
    key = current_app.config.get('SUPABASE_SERVICE_KEY', '')
    if bucket is None:
        bucket = current_app.config.get('SUPABASE_BUCKET', 'uploads')
    if not url or not key:
        return None
    try:
        resp = _req.put(
            f"{url}/storage/v1/object/{bucket}/{object_path}",
            data=file_bytes,
            headers={
                'Authorization': f'Bearer {key}',
                'apikey': key,
                'Content-Type': content_type,
                'x-upsert': 'true',
            },
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            current_app.logger.error(
                f"Supabase upload failed {resp.status_code}: {resp.text[:300]}"
            )
            return None
        return f"{url}/storage/v1/object/public/{bucket}/{object_path}"
    except Exception as exc:
        current_app.logger.error(f"Supabase upload error: {exc}")
        return None


def _supabase_delete(object_path: str, bucket: str | None = None) -> bool:
    """Delete an object from Supabase Storage. Returns True on success.

    Never raises and never logs the service key or full credentials.
    """
    import requests as _req
    url = current_app.config.get('SUPABASE_URL', '').rstrip('/')
    key = current_app.config.get('SUPABASE_SERVICE_KEY', '')
    if bucket is None:
        bucket = current_app.config.get('SUPABASE_BUCKET', 'uploads')
    if not url or not key:
        return False
    try:
        resp = _req.delete(
            f"{url}/storage/v1/object/{bucket}/{object_path}",
            headers={'Authorization': f'Bearer {key}', 'apikey': key},
            timeout=30,
        )
        if resp.status_code not in (200, 204):
            current_app.logger.warning(
                f"Supabase delete failed {resp.status_code} for object in bucket "
                f"{bucket}"
            )
            return False
        return True
    except Exception as exc:
        current_app.logger.warning(f"Supabase delete error: {exc}")
        return False


def delete_uploaded_file(stored_value: str | None, bucket: str | None = None) -> bool:
    """Best-effort delete of a previously stored upload.

    Accepts the exact value persisted by ``save_uploaded_file``:
      - A Supabase public URL  →  the object is removed from its bucket.
      - A local relative path ('uploads/...') → the file is removed from disk.

    Returns True on success, False otherwise. Never raises. The original
    filename / path supplied by a client must never be passed here — only the
    server-generated value stored in the database.
    """
    if not stored_value:
        return False
    if stored_value.startswith(('http://', 'https://')):
        # Expected shape: {url}/storage/v1/object/public/{bucket}/{object_path}
        marker = '/storage/v1/object/public/'
        idx = stored_value.find(marker)
        if idx == -1:
            return False
        remainder = stored_value[idx + len(marker):]
        parsed_bucket, _, object_path = remainder.partition('/')
        if not parsed_bucket or not object_path:
            return False
        return _supabase_delete(object_path, bucket=bucket or parsed_bucket)
    # Local relative path under static/.
    try:
        full_path = os.path.normpath(
            os.path.join(current_app.root_path, 'static', stored_value)
        )
        static_root = os.path.normpath(os.path.join(current_app.root_path, 'static'))
        # Guard against path traversal: must stay inside static/.
        if not full_path.startswith(static_root + os.sep):
            return False
        if os.path.isfile(full_path):
            os.remove(full_path)
            return True
        return False
    except Exception as exc:
        current_app.logger.warning(f"Local upload delete error: {exc}")
        return False


def save_uploaded_file(file, subfolder='misc', prefix=None, bucket=None,
                       allowed_exts=None, max_size=None):
    """
    Save an uploaded FileStorage object.

    Returns:
      - A full Supabase public URL when SUPABASE_URL + SUPABASE_SERVICE_KEY are set (production).
      - A relative path string like 'uploads/subfolder/file.ext' for local dev.
      - None on validation failure or upload error.

    Args:
      bucket:       Supabase bucket name override (defaults to SUPABASE_BUCKET config).
      allowed_exts: Set of allowed lowercase extensions; defaults to ALLOWED_IMAGE_EXTENSIONS.
      max_size:     Maximum byte size; returns None if exceeded.
    """
    if not file or file.filename == '':
        return None

    ext = (file.filename.rsplit('.', 1)[1].lower()
           if '.' in file.filename else '')
    check_exts = allowed_exts if allowed_exts is not None else ALLOWED_IMAGE_EXTENSIONS
    if ext not in check_exts:
        return None

    # Read bytes once — used for both size check, Supabase upload, and local save.
    file_bytes = file.read()
    if max_size and len(file_bytes) > max_size:
        return None

    base_name = uuid.uuid4().hex
    if prefix:
        safe_prefix = secure_filename(prefix)
        if safe_prefix:
            base_name = f"{safe_prefix}-{base_name}"
    filename = f"{base_name}.{ext}"
    object_path = f"{subfolder}/{filename}"
    content_type = _CONTENT_TYPES.get(ext, 'application/octet-stream')

    # Production: try Supabase Storage first
    supabase_url = _supabase_upload(file_bytes, object_path, content_type, bucket=bucket)
    if supabase_url is not None:
        return supabase_url

    # Development / fallback: local filesystem
    upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', subfolder)
    os.makedirs(upload_dir, exist_ok=True)
    with open(os.path.join(upload_dir, filename), 'wb') as fh:
        fh.write(file_bytes)
    return f"uploads/{subfolder}/{filename}"


def resolve_photo_url(photo: str | None) -> str | None:
    """
    Return a displayable URL for a stored photo value.

    - Supabase / CDN URLs (http/https) are returned as-is.
    - New relative paths like 'uploads/subfolder/file.ext' are resolved to
      /static/... only when the file still exists on disk.
    - Legacy bare filenames (no slash) are looked up under static/uploads/ for
      backward compatibility with records created before the Supabase migration.
    - None / empty / missing file → returns None so callers can show a placeholder.
    """
    if not photo:
        return None
    if photo.startswith(('http://', 'https://')):
        return photo
    try:
        # Build candidate paths to check, most-specific first.
        candidates = [photo]
        if '/' not in photo:
            # Legacy: bare filename stored without a directory prefix.
            candidates.append(f'uploads/{photo}')
        for candidate in candidates:
            full_path = os.path.join(current_app.root_path, 'static', candidate)
            if os.path.isfile(full_path):
                return url_for('static', filename=candidate)
        return None
    except Exception:
        return None


def generate_student_id(last_id=None):
    """Generate next student ID like STU-00001."""
    next_num = (last_id or 0) + 1
    return f"STU-{next_num:05d}"


def generate_employee_id(last_id=None):
    """Generate next employee ID like EMP-00001."""
    next_num = (last_id or 0) + 1
    return f"EMP-{next_num:05d}"


def generate_receipt_no():
    """Generate unique receipt number."""
    return f"RCP-{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"


def calculate_grade_letter(marks, max_marks=100):
    pct = (float(marks) / float(max_marks)) * 100
    if pct >= 95: return 'A+'
    if pct >= 90: return 'A'
    if pct >= 85: return 'B+'
    if pct >= 80: return 'B'
    if pct >= 75: return 'C+'
    if pct >= 70: return 'C'
    if pct >= 60: return 'D'
    return 'F'

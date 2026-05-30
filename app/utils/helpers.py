"""
Al-Muhandis – General utility functions
"""
import os
import uuid
from datetime import datetime
from flask import current_app, url_for
from werkzeug.utils import secure_filename


ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_DOC_EXTENSIONS   = {'pdf', 'doc', 'docx', 'xls', 'xlsx'}

_CONTENT_TYPES = {
    'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
    'gif': 'image/gif', 'webp': 'image/webp',
    'pdf': 'application/pdf', 'doc': 'application/msword',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
}


def allowed_file(filename, extensions=None):
    if extensions is None:
        extensions = current_app.config.get('ALLOWED_EXTENSIONS', set())
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in extensions


def _supabase_upload(file_bytes: bytes, object_path: str, content_type: str) -> str | None:
    """Upload bytes to Supabase Storage; return the public URL or None on failure."""
    import requests as _req
    url = current_app.config.get('SUPABASE_URL', '').rstrip('/')
    key = current_app.config.get('SUPABASE_SERVICE_KEY', '')
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


def save_uploaded_file(file, subfolder='misc', prefix=None):
    """
    Save an uploaded FileStorage object.

    Returns:
      - A full Supabase public URL when SUPABASE_URL + SUPABASE_SERVICE_KEY are set (production).
      - A relative path string like 'uploads/subfolder/file.ext' for local dev.
      - None on validation failure or upload error.
    """
    if not file or file.filename == '':
        return None
    if not allowed_file(file.filename):
        return None

    ext = file.filename.rsplit('.', 1)[1].lower()
    base_name = uuid.uuid4().hex
    if prefix:
        safe_prefix = secure_filename(prefix)
        if safe_prefix:
            base_name = f"{safe_prefix}-{base_name}"
    filename = f"{base_name}.{ext}"
    object_path = f"{subfolder}/{filename}"
    content_type = _CONTENT_TYPES.get(ext, 'application/octet-stream')

    # Production: try Supabase Storage first
    supabase_url = _supabase_upload(file.read(), object_path, content_type)
    if supabase_url is not None:
        return supabase_url

    # Development / fallback: local filesystem
    # Reset stream in case read() was called (won't help if already consumed, but
    # file.seek(0) works for in-memory FileStorage objects used in tests)
    try:
        file.seek(0)
    except Exception:
        pass
    upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', subfolder)
    os.makedirs(upload_dir, exist_ok=True)
    file.save(os.path.join(upload_dir, filename))
    return f"uploads/{subfolder}/{filename}"


def resolve_photo_url(photo: str | None) -> str | None:
    """
    Return a displayable URL for a stored photo value.

    - Supabase / CDN URLs (http/https) are returned as-is.
    - Legacy relative paths (uploads/...) are resolved to /static/... only if
      the file still exists on disk (guards against broken images from Render's
      ephemeral filesystem after a redeploy).
    - None / empty / missing file → returns None (caller shows placeholder).
    """
    if not photo:
        return None
    if photo.startswith(('http://', 'https://')):
        return photo
    # Legacy local path — only serve if the file is actually present
    try:
        full_path = os.path.join(current_app.root_path, 'static', photo)
        if not os.path.isfile(full_path):
            return None
        return url_for('static', filename=photo)
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

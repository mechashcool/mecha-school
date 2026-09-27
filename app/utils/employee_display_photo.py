"""
Employee DISPLAY photo — an optional, display-only derivative of Employee.photo.

Employee.photo stays the original upload and the ONLY AI Face source; nothing
here reads, rewrites, moves or deletes it. Employee.photo_display is extra
optimisation data for avatars/previews:

  * written only when a NEW / replacement photo is uploaded through Employee
    create/edit, AFTER the original passed validate_employee_photo and was
    stored;
  * NULL for every existing employee (no backfill, no lazy generation —
    viewing an employee never touches Storage);
  * best effort: if generating or storing the derivative fails, the employee
    operation proceeds with photo_display = NULL and display falls back to
    Employee.photo.

Deploy order: migration e5m6p7d8s9p0 (additive, nullable column) must be applied
BEFORE this code is deployed — the mapped column is part of every Employee
INSERT/SELECT. The previous release does not map it, so applying the migration
first is safe while the old code is still running.

Display policy (same as the Student display copy): EXIF orientation applied to
the COPY only, EXIF/XMP/GPS dropped (an RGB ICC profile is kept), longest side
<= 1024 px (never upscaled, LANCZOS), WebP quality 80 / method 4, alpha kept
when the source has transparency. Animated GIF/WEBP use their first frame — the
frame AI Face uses; the stored original keeps its animation.

Memory: JPEG is decoded at a reduced libjpeg scale (draft), the image is
converted/resized BEFORE the orientation transpose (which then runs on the
<=1024 px copy), and an image already in the target mode is resized in place —
no avoidable full-resolution copies.
"""
from __future__ import annotations

import io
import logging
import os

from app.utils.employee_photo import EMPLOYEE_PHOTO_MAX_PIXELS

log = logging.getLogger(__name__)

EMPLOYEE_DISPLAY_MAX_SIDE = 1024
EMPLOYEE_DISPLAY_WEBP_QUALITY = 80
EMPLOYEE_DISPLAY_WEBP_METHOD = 4
EMPLOYEE_DISPLAY_SUBFOLDER = 'employees/display'
_ALLOWED_FORMATS = ('JPEG', 'PNG', 'GIF', 'WEBP')
_ORIENTATION_TAG = 0x0112


# ── Display resolution (no Storage / network access) ──────────────────────────

def _display_usable(value: str | None) -> bool:
    """Cheap, LOCAL-only check that a stored display value can be shown.

    Never makes a network/Storage request (no HEAD, GET or signing probe):
      * empty / whitespace → not usable;
      * a full http(s) URL (normal Supabase upload) → trusted as stored — the
        value is only ever written by this application;
      * a relative value exists only when the Supabase upload failed and
        save_uploaded_file() fell back to local disk (``uploads/...``); it is
        usable only while that file is still on disk. Anything else shaped like
        a path escape, a scheme, a drive letter or a backslash path is refused.
    """
    if not value or not value.strip():
        return False
    v = value.strip()
    if v.startswith(('http://', 'https://')):
        return True
    if '\\' in v or '\x00' in v or ':' in v:
        return False
    rel = v.lstrip('/')
    if rel.startswith('static/'):
        rel = rel[len('static/'):]
    parts = rel.split('/')
    if parts[0] != 'uploads' or len(parts) < 2 or any(p in ('', '.', '..') for p in parts):
        return False
    try:
        from flask import current_app
        return os.path.isfile(os.path.join(current_app.root_path, 'static', *parts))
    except Exception:
        return False


def employee_display_value(employee) -> str | None:
    """Stored value to DISPLAY: photo_display when usable, else Employee.photo.

    A missing/broken display copy therefore never shows worse than having no
    copy at all. Never used by AI Face. No Storage/network access, no
    generation.
    """
    if employee is None:
        return None
    display = getattr(employee, 'photo_display', None)
    if _display_usable(display):
        return display
    return employee.photo


def employee_photo_url(employee) -> str | None:
    """Web URL for an employee's display photo (same resolver as before)."""
    from app.utils.helpers import resolve_photo_url
    return resolve_photo_url(employee_display_value(employee))


# ── Generation (new / replacement uploads only) ──────────────────────────────

def _has_alpha(img) -> bool:
    return (img.mode in ('RGBA', 'LA', 'PA')
            or (img.mode in ('P', 'L', 'RGB') and 'transparency' in img.info))


def _transpose_method(orientation):
    """The transpose Pillow's ImageOps.exif_transpose applies for *orientation*."""
    from PIL import Image
    T = Image.Transpose
    return {2: T.FLIP_LEFT_RIGHT, 3: T.ROTATE_180, 4: T.FLIP_TOP_BOTTOM,
            5: T.TRANSPOSE, 6: T.ROTATE_270, 7: T.TRANSVERSE,
            8: T.ROTATE_90}.get(orientation)


def make_employee_display_photo(raw: bytes) -> bytes:
    """Encode the display copy of an (already validated) photo. Raises on error.

    ``raw`` is never modified; the returned bytes are an independent WebP.
    """
    from PIL import Image

    img = Image.open(io.BytesIO(raw), formats=list(_ALLOWED_FORMATS))
    with img:
        width, height = img.size
        if width < 1 or height < 1 or width * height > EMPLOYEE_PHOTO_MAX_PIXELS:
            raise ValueError('unsupported dimensions')
        orientation = img.getexif().get(_ORIENTATION_TAG, 1)
        icc_profile = (img.info.get('icc_profile')
                       if img.mode in ('RGB', 'RGBA', 'P', 'PA') else None)
        if img.format == 'JPEG':
            img.draft(img.mode, (EMPLOYEE_DISPLAY_MAX_SIDE, EMPLOYEE_DISPLAY_MAX_SIDE))
        img.load()                                    # first frame only
        target = 'RGBA' if _has_alpha(img) else 'RGB'
        out = img if img.mode == target else img.convert(target)
        if max(out.size) > EMPLOYEE_DISPLAY_MAX_SIDE:  # shrink only, never upscale
            out.thumbnail((EMPLOYEE_DISPLAY_MAX_SIDE, EMPLOYEE_DISPLAY_MAX_SIDE),
                          Image.Resampling.LANCZOS)
        method = _transpose_method(orientation)
        if method is not None:
            out = out.transpose(method)               # on the <=1024 px copy
        buf = io.BytesIO()
        extra = {'icc_profile': icc_profile} if icc_profile else {}
        out.save(buf, format='WEBP', quality=EMPLOYEE_DISPLAY_WEBP_QUALITY,
                 method=EMPLOYEE_DISPLAY_WEBP_METHOD, **extra)   # no exif/xmp → stripped
    return buf.getvalue()


def prepare_employee_display_photo(upload) -> bytes | None:
    """Display bytes for a validated upload, or None (logged). Never raises.

    Reads the whole upload from its start and restores the stream position;
    the upload itself is never modified.
    """
    try:
        stream = upload.stream
        pos = stream.tell()
        try:
            stream.seek(0)
            raw = stream.read()
        finally:
            stream.seek(pos)
        return make_employee_display_photo(raw)
    except Exception:
        log.warning('[employee-photo] display derivative generation failed; '
                    'falling back to the original', exc_info=True)
        return None


def save_employee_display_photo(data: bytes | None) -> str | None:
    """Store display bytes as a new employees/display/<uuid>.webp object.

    A fresh object name every time (never overwrites). Returns the stored value
    — same convention as Employee.photo (full Supabase URL, or the local
    ``uploads/...`` fallback) — or None (logged) on any failure. Never raises.
    """
    if not data:
        return None
    try:
        from werkzeug.datastructures import FileStorage
        from app.utils.helpers import save_uploaded_file
        stored = save_uploaded_file(
            FileStorage(io.BytesIO(data), filename='display.webp',
                        content_type='image/webp'),
            subfolder=EMPLOYEE_DISPLAY_SUBFOLDER,
            allowed_exts={'webp'},
        )
    except Exception:
        log.warning('[employee-photo] display derivative storage failed; '
                    'falling back to the original', exc_info=True)
        return None
    if not stored:
        log.warning('[employee-photo] display derivative not stored; '
                    'falling back to the original')
        return None
    return stored

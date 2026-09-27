"""
Student DISPLAY photo — an optional, display-only derivative of Student.photo.

Student.photo stays the original upload and the ONLY AI Face source; nothing
here reads, rewrites, moves or deletes it. Student.photo_display is extra
optimisation data for avatars/previews:

  * written only when a NEW photo is uploaded through Student create/edit;
  * NULL for every existing student (no backfill, no lazy generation — viewing
    a student never touches Storage);
  * best effort: if generating or storing the derivative fails, the student
    operation proceeds with photo_display = NULL and display falls back to
    Student.photo.

Deploy order: migration j1s2d3p4h5o6 (additive, nullable column) must be applied
BEFORE this code is deployed — the mapped column is part of every Student
INSERT/SELECT. The previous release does not map it, so applying the migration
first is safe while the old code is still running.

Display policy: EXIF orientation applied to the COPY only, EXIF/XMP/GPS dropped
(an RGB ICC profile is kept), longest side <= 1024 px (never upscaled, LANCZOS),
WebP quality 80, alpha kept when the source has transparency. Animated GIF/WEBP
(accepted for Student.photo) use their first frame — the frame AI Face uses.
"""
from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)

STUDENT_DISPLAY_MAX_SIDE = 1024
STUDENT_DISPLAY_WEBP_QUALITY = 80
STUDENT_DISPLAY_SUBFOLDER = 'students/display'
_ALLOWED_FORMATS = ('JPEG', 'PNG', 'GIF', 'WEBP')


def student_display_value(student) -> str | None:
    """Stored value to DISPLAY: photo_display when present, else Student.photo.

    Never used by AI Face. Performs no Storage access and no generation.
    """
    if student is None:
        return None
    return student.photo_display or student.photo


def student_photo_url(student) -> str | None:
    """Web URL for a student's display photo (same resolver as before)."""
    from app.utils.helpers import resolve_photo_url
    return resolve_photo_url(student_display_value(student))


def _has_alpha(img) -> bool:
    return (img.mode in ('RGBA', 'LA', 'PA')
            or (img.mode in ('P', 'L', 'RGB') and 'transparency' in img.info))


def make_display_photo(raw: bytes) -> bytes:
    """Encode the display copy of an (already validated) photo. Raises on error."""
    from PIL import Image, ImageOps
    from app.utils.student_photo import STUDENT_PHOTO_MAX_PIXELS

    img = Image.open(io.BytesIO(raw), formats=list(_ALLOWED_FORMATS))
    with img:
        width, height = img.size
        if width < 1 or height < 1 or width * height > STUDENT_PHOTO_MAX_PIXELS:
            raise ValueError('unsupported dimensions')
        icc_profile = (img.info.get('icc_profile')
                       if img.mode in ('RGB', 'RGBA', 'P', 'PA') else None)
        if img.format == 'JPEG':
            img.draft(img.mode, (STUDENT_DISPLAY_MAX_SIDE, STUDENT_DISPLAY_MAX_SIDE))
        img.load()                                    # first frame only
        oriented = ImageOps.exif_transpose(img)       # a new image; source untouched
        out = oriented.convert('RGBA' if _has_alpha(oriented) else 'RGB')
    if max(out.size) > STUDENT_DISPLAY_MAX_SIDE:
        out.thumbnail((STUDENT_DISPLAY_MAX_SIDE, STUDENT_DISPLAY_MAX_SIDE),
                      Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    extra = {'icc_profile': icc_profile} if icc_profile else {}
    out.save(buf, format='WEBP', quality=STUDENT_DISPLAY_WEBP_QUALITY, method=4,
             **extra)                                 # no exif/xmp -> stripped
    return buf.getvalue()


def prepare_display_photo(upload) -> bytes | None:
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
        return make_display_photo(raw)
    except Exception:
        log.warning('[student-photo] display derivative generation failed; '
                    'falling back to the original', exc_info=True)
        return None


def save_display_photo(data: bytes | None) -> str | None:
    """Store display bytes as a new students/display/<uuid>.webp object.

    Returns the stored value, or None (logged) on any failure. Never raises.
    """
    if not data:
        return None
    try:
        from werkzeug.datastructures import FileStorage
        from app.utils.helpers import save_uploaded_file
        stored = save_uploaded_file(
            FileStorage(io.BytesIO(data), filename='display.webp',
                        content_type='image/webp'),
            subfolder=STUDENT_DISPLAY_SUBFOLDER,
            allowed_exts={'webp'},
        )
    except Exception:
        log.warning('[student-photo] display derivative storage failed; '
                    'falling back to the original', exc_info=True)
        return None
    if not stored:
        log.warning('[student-photo] display derivative not stored; '
                    'falling back to the original')
        return None
    return stored

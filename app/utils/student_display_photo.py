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

Display policy (shared with the Employee display copy through
``encode_display_photo``): a centred square crop of DISPLAY_SIDE x DISPLAY_SIDE
(192) px — the box every avatar consumer renders with object-fit / BoxFit.cover
— never upscaled (a smaller source gives a min(w, h) square), LANCZOS, WebP
quality 45 / method 6. EXIF orientation is applied to the COPY only;
EXIF/XMP/GPS are dropped; an embedded RGB ICC profile is converted to sRGB and
then dropped (no profile in the copy). Alpha is kept when the source has
transparency. Animated GIF/WEBP (accepted for Student.photo) use their first
frame — the frame AI Face uses.
"""
from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)

# Shared display policy (students AND employees).
DISPLAY_SIDE = 192
DISPLAY_WEBP_QUALITY = 45
DISPLAY_WEBP_METHOD = 6

STUDENT_DISPLAY_MAX_SIDE = DISPLAY_SIDE
STUDENT_DISPLAY_WEBP_QUALITY = DISPLAY_WEBP_QUALITY
STUDENT_DISPLAY_WEBP_METHOD = DISPLAY_WEBP_METHOD
STUDENT_DISPLAY_SUBFOLDER = 'students/display'
_ALLOWED_FORMATS = ('JPEG', 'PNG', 'GIF', 'WEBP')
_ORIENTATION_TAG = 0x0112


def _display_usable(value: str | None) -> bool:
    """Cheap, LOCAL-only check that a stored display value can be shown.

    Never makes a network/Storage request:
      * empty / whitespace → not usable;
      * a full http(s) URL (normal Supabase upload) → trusted as stored;
      * a relative/local value exists only when the Supabase upload failed and
        save_uploaded_file() fell back to local disk — such a copy was never
        in Supabase, so it is usable only while its file is still on disk.
    """
    if not value or not value.strip():
        return False
    if value.startswith(('http://', 'https://')):
        return True
    import os
    from flask import current_app
    rel = value.strip().lstrip('/')
    if rel.startswith('static/'):
        rel = rel[len('static/'):]
    if '/' not in rel:
        rel = f'uploads/{rel}'                 # legacy bare-filename form
    parts = rel.split('/')
    if '..' in parts or '' in parts:
        return False
    try:
        return os.path.isfile(os.path.join(current_app.root_path, 'static', *parts))
    except Exception:
        return False


def student_display_value(student) -> str | None:
    """Stored value to DISPLAY: photo_display when usable, else Student.photo.

    A missing/broken display copy therefore never shows worse than having no
    copy at all. Never used by AI Face. No Storage/network access, no
    generation.
    """
    if student is None:
        return None
    display = student.photo_display
    if _display_usable(display):
        return display
    return student.photo


def student_photo_url(student) -> str | None:
    """Web URL for a student's display photo (same resolver as before)."""
    from app.utils.helpers import resolve_photo_url
    return resolve_photo_url(student_display_value(student))


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


def _to_srgb(img, icc_profile):
    """*img* (RGB/RGBA) with its pixels converted from *icc_profile* to sRGB.

    The profile is never carried into the copy. Alpha is detached and re-attached
    untouched. An unreadable or non-RGB profile leaves the decoded pixels as they
    are — what a viewer ignoring that profile would show.
    """
    if not icc_profile:
        return img
    from PIL import ImageCms
    try:
        src = ImageCms.ImageCmsProfile(io.BytesIO(icc_profile))
        dst = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB'))
        rgb = img.convert('RGB') if img.mode == 'RGBA' else img
        converted = ImageCms.profileToProfile(rgb, src, dst, outputMode='RGB')
    except Exception:
        return img
    if converted is None:
        return img
    if img.mode == 'RGBA':
        converted.putalpha(img.getchannel('A'))
    return converted


def encode_display_photo(raw: bytes, *, max_pixels: int) -> bytes:
    """THE display encoder for Student AND Employee display copies.

    Centred DISPLAY_SIDE square (never upscaled), WebP DISPLAY_WEBP_QUALITY /
    DISPLAY_WEBP_METHOD, EXIF orientation applied, EXIF/XMP/GPS dropped, RGB ICC
    converted to sRGB and dropped, alpha kept, first frame only. ``raw`` is never
    modified; the result is an independent WebP. Raises on any decode error.

    Memory: JPEG is decoded at a reduced libjpeg scale (draft, still >= 2x the
    target) and the crop/resize runs BEFORE the orientation transpose — a centred
    square crop commutes with every EXIF orientation.
    """
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(raw), formats=list(_ALLOWED_FORMATS))
    with img:
        width, height = img.size
        if width < 1 or height < 1 or width * height > max_pixels:
            raise ValueError('unsupported dimensions')
        orientation = img.getexif().get(_ORIENTATION_TAG, 1)
        icc_profile = (img.info.get('icc_profile')
                       if img.mode in ('RGB', 'RGBA', 'P', 'PA') else None)
        if img.format == 'JPEG':
            img.draft(img.mode, (DISPLAY_SIDE * 2, DISPLAY_SIDE * 2))
        img.load()                                    # first frame only
        target = 'RGBA' if _has_alpha(img) else 'RGB'
        out = img if img.mode == target else img.convert(target)
        side = min(DISPLAY_SIDE, *out.size)           # shrink only, never upscale
        out = ImageOps.fit(out, (side, side), Image.Resampling.LANCZOS,
                           centering=(0.5, 0.5))      # a new image; source untouched
    method = _transpose_method(orientation)
    if method is not None:
        out = out.transpose(method)
    out = _to_srgb(out, icc_profile)
    out.info.clear()                                  # no exif/xmp/icc carried over
    buf = io.BytesIO()
    out.save(buf, format='WEBP', quality=DISPLAY_WEBP_QUALITY,
             method=DISPLAY_WEBP_METHOD)
    return buf.getvalue()


def make_display_photo(raw: bytes) -> bytes:
    """Encode the display copy of an (already validated) photo. Raises on error."""
    from app.utils.student_photo import STUDENT_PHOTO_MAX_PIXELS
    return encode_display_photo(raw, max_pixels=STUDENT_PHOTO_MAX_PIXELS)


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

"""
Stored profile-photo form — the image the AI Face pipeline already sends.

Student.photo / Employee.photo stay the ONLY Face ID source; they are now
stored in exactly the representation app.services.aiface_sync.
prepare_photo_for_device() produces for the device: EXIF orientation applied,
RGB, longest side <= 640 px (aspect ratio kept, never upscaled, LANCZOS),
JPEG quality 85 with optimize=True. The Face ID code itself is unchanged and
still normalises whatever ``photo`` holds before sending it.

Display copies (photo_display) are NOT made from this: callers keep handing the
ORIGINAL upload to the display-photo helpers.
"""
from __future__ import annotations

import io

FACE_PHOTO_MAX_SIDE = 640
FACE_PHOTO_JPEG_QUALITY = 85
# Same decode ceiling as Student.photo / Employee.photo validation.
FACE_PHOTO_MAX_PIXELS = 40_000_000
_ALLOWED_FORMATS = ('JPEG', 'PNG', 'GIF', 'WEBP')


def normalize_face_photo(raw: bytes) -> bytes:
    """640 px / RGB / JPEG q85 optimize bytes for *raw*. Raises on any error.

    Mirrors the Pillow block of prepare_photo_for_device() step for step, so the
    stored photo is what the device would receive. ``raw`` is never modified.
    """
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(raw), formats=list(_ALLOWED_FORMATS))
    width, height = img.size
    if width < 1 or height < 1 or width * height > FACE_PHOTO_MAX_PIXELS:
        raise ValueError('unsupported dimensions')
    img = ImageOps.exif_transpose(img)
    if img.mode != 'RGB':
        img = img.convert('RGB')
    if img.width > FACE_PHOTO_MAX_SIDE or img.height > FACE_PHOTO_MAX_SIDE:
        img.thumbnail((FACE_PHOTO_MAX_SIDE, FACE_PHOTO_MAX_SIDE), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=FACE_PHOTO_JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def normalized_face_upload(upload):
    """A NEW FileStorage holding the normalised JPEG of *upload*, or None.

    Reads the whole upload from its start and restores the stream position, so
    the ORIGINAL upload stays available, unchanged, for the display-photo
    helpers. Never raises (a failure is logged and returns None).
    """
    import logging
    from werkzeug.datastructures import FileStorage
    try:
        stream = upload.stream
        pos = stream.tell()
        try:
            stream.seek(0)
            raw = stream.read()
        finally:
            stream.seek(pos)
        data = normalize_face_photo(raw)
    except Exception:
        logging.getLogger(__name__).warning(
            '[face-photo] normalisation failed; the photo is not stored', exc_info=True)
        return None
    return FileStorage(io.BytesIO(data), filename='photo.jpg', content_type='image/jpeg')

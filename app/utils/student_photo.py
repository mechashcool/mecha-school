"""
Student.photo upload validation — inspection only, the bytes are never changed.

Student.photo is the source image for AI Face device enrollment
(app.services.aiface_sync.prepare_photo_for_device downloads and decodes the
stored original), so this module does NOT resize, re-encode, convert, strip
metadata or otherwise transform the upload. It only decides whether the upload
may be stored, then rewinds the stream so the existing save_uploaded_file()
call stores the exact original bytes, extension and Content-Type as before.

Checks, for an upload whose extension is already allowed:
  1. Pillow must recognise the bytes as JPEG, PNG, GIF or WEBP (the formats of
     the existing allow-list; the filename/MIME are not trusted). A real image
     of an allowed format under another allowed extension is accepted, exactly
     as it is stored and synced today;
  2. width x height <= STUDENT_PHOTO_MAX_PIXELS, read from the header before
     any pixel data is decoded (a tiny file can declare enormous dimensions);
  3. integrity: JPEG is decoded at libjpeg's reduced 1/8 scale (the whole
     compressed stream is read, pixel memory stays small), PNG chunks and CRCs
     are verified without decoding pixels, GIF/WEBP decode their first frame
     (bounded by the pixel guard) since Pillow offers no cheaper check.
Animated GIF/WEBP stay accepted (current behaviour; AI Face uses frame 0).

Uploads with a disallowed extension or no filename are not judged here: the
existing save_uploaded_file() extension check keeps handling them unchanged.
Pillow's global limits are not modified.
"""
from __future__ import annotations

import io

from app.utils.helpers import ALLOWED_IMAGE_EXTENSIONS

STUDENT_PHOTO_MAX_PIXELS = 40_000_000     # same decode ceiling as board images
_ALLOWED_FORMATS = ('JPEG', 'PNG', 'GIF', 'WEBP')

MSG_INVALID = 'تعذّر قراءة صورة الطالب. يرجى رفع صورة صالحة بصيغة jpg أو png أو webp أو gif.'
MSG_TOO_LARGE = 'أبعاد صورة الطالب كبيرة جداً. يرجى رفع صورة بأبعاد أصغر.'


def validate_student_photo(file) -> str | None:
    """Return None when the upload may be stored, else a user-facing message.

    The stream is always restored to its original position, so the caller
    passes the SAME FileStorage to save_uploaded_file() and the stored bytes
    are the uploaded bytes.
    """
    from PIL import Image

    name = (file.filename or '') if file else ''
    ext = name.rsplit('.', 1)[1].lower() if '.' in name else ''
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return None

    stream = file.stream
    pos = stream.tell()
    try:
        raw = stream.read()
    finally:
        stream.seek(pos)

    try:
        img = Image.open(io.BytesIO(raw), formats=list(_ALLOWED_FORMATS))
    except Image.DecompressionBombError:
        return MSG_TOO_LARGE
    except Exception:
        return MSG_INVALID
    try:
        with img:
            width, height = img.size
            if width < 1 or height < 1:
                return MSG_INVALID
            if width * height > STUDENT_PHOTO_MAX_PIXELS:
                return MSG_TOO_LARGE
            if img.format == 'PNG':
                img.verify()
            else:
                if img.format == 'JPEG':
                    img.draft(img.mode, (max(1, width // 8), max(1, height // 8)))
                img.load()
    except Exception:
        return MSG_INVALID
    return None

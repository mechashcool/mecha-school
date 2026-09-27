"""
Employee.photo upload validation — inspection only, the bytes are never changed.

Employee.photo is the source image for AI Face device enrollment
(app.services.aiface_sync.prepare_photo_for_device downloads and decodes the
stored original), so this module does NOT resize, re-encode, convert, strip
metadata or otherwise transform the upload. It only decides whether a NEW or
REPLACEMENT photo may be stored, then rewinds the stream so the existing
save_uploaded_file() call stores the exact original bytes, extension and
Content-Type as before. Existing employees and their stored photos are never
read or touched here.

Same safety model as the deployed Student.photo validator
(app.utils.student_photo), plus the employee-specific checks that used to be
missing or create-only:
  1. the extension must be in the existing allow-list (ALLOWED_IMAGE_EXTENSIONS)
     — refused explicitly here, so an edit can no longer drop it silently;
  2. size <= EMPLOYEE_PHOTO_MAX_BYTES (2 MB), measured from the stream itself
     (never the Content-Length header, MIME type or filename) before the bytes
     are read; an unmeasurable stream is refused (fail closed);
  3. Pillow must recognise the bytes as JPEG, PNG, GIF or WEBP (the formats of
     the allow-list). A real image of an allowed format under another allowed
     extension is accepted, exactly as it is stored and synced today;
  4. width x height <= EMPLOYEE_PHOTO_MAX_PIXELS, read from the header before
     any pixel data is decoded (a tiny file can declare enormous dimensions);
  5. integrity: JPEG is decoded at libjpeg's reduced 1/8 scale (the whole
     compressed stream is read, pixel memory stays small), PNG chunks and CRCs
     are verified without decoding pixels, GIF/WEBP decode their first frame
     (bounded by the pixel guard) since Pillow offers no cheaper check.
Animated GIF/WEBP stay accepted (current behaviour; AI Face uses frame 0).
Pillow's global limits are not modified.
"""
from __future__ import annotations

import io

from app.utils.helpers import ALLOWED_IMAGE_EXTENSIONS

EMPLOYEE_PHOTO_MAX_BYTES = 2 * 1024 * 1024     # 2 MB per new/replacement photo
EMPLOYEE_PHOTO_MAX_PIXELS = 40_000_000         # same ceiling as Student.photo
_ALLOWED_FORMATS = ('JPEG', 'PNG', 'GIF', 'WEBP')

# Generic on purpose — never echo a filename, path, byte count or internal detail.
MSG_UNSUPPORTED = ('صيغة صورة الموظف غير مدعومة. '
                   'الصيغ المسموحة: JPG, JPEG, PNG, WEBP, GIF.')
MSG_TOO_BIG = 'حجم صورة الموظف يجب ألا يتجاوز 2 ميجابايت.'
MSG_INVALID = ('تعذّر قراءة صورة الموظف. يرجى رفع صورة صالحة '
               'بصيغة jpg أو png أو webp أو gif.')
MSG_TOO_LARGE = 'أبعاد صورة الموظف كبيرة جداً. يرجى رفع صورة بأبعاد أصغر.'


def validate_employee_photo(file) -> str | None:
    """Return None when the upload may be stored, else a user-facing message.

    Only called for a submitted photo (a file with a filename). The stream is
    always restored to its original position, so the caller passes the SAME
    FileStorage to save_uploaded_file() and the stored bytes are the uploaded
    bytes.
    """
    from PIL import Image

    name = (file.filename or '') if file else ''
    ext = name.rsplit('.', 1)[1].lower() if '.' in name else ''
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return MSG_UNSUPPORTED

    stream = getattr(file, 'stream', None)
    if stream is None:
        return MSG_TOO_BIG                     # cannot be measured → fail closed
    try:
        pos = stream.tell()
        stream.seek(0, 2)                      # SEEK_END
        size = stream.tell() - pos
        stream.seek(pos)
    except (AttributeError, OSError, ValueError):
        return MSG_TOO_BIG
    if size > EMPLOYEE_PHOTO_MAX_BYTES:
        return MSG_TOO_BIG

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
            if width * height > EMPLOYEE_PHOTO_MAX_PIXELS:
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

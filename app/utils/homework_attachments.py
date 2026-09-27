"""
Homework attachment preparation — image attachments are optimised ONCE, at
upload time; PDFs pass through untouched.

Image attachments (jpg / jpeg / png / webp by filename) are refused above
5 MB (the School Board incoming limit) before any decode, then reuse the
School Board optimiser unchanged (app.utils.board_images): real decode restricted to
JPEG/PNG/WEBP, pixel guard before decoding, animated images refused, EXIF
orientation applied then EXIF/XMP dropped, longest side <= 1600 px (never
upscaled), WebP quality 80, alpha kept. Only the optimised WebP is handed to
the unchanged generic save_uploaded_file(); the original is never stored.

Every other upload (PDF, or an extension the caller does not allow) is
returned as-is, so the caller's existing extension check, bytes, object
extension and Content-Type stay exactly as before.

Used only by homework create/edit (web) and create/update (mobile teacher).
Student/employee photos, registration photos and AI Face never use it.
"""
from __future__ import annotations

import io

from werkzeug.datastructures import FileStorage

from app.utils.board_images import BoardImageError, optimize_board_image

HOMEWORK_IMAGE_EXTS = frozenset({'jpg', 'jpeg', 'png', 'webp'})
# Same incoming limit as School Board images; checked on the raw upload,
# before any decode. PDFs are not subject to it.
HOMEWORK_IMAGE_MAX_BYTES = 5 * 1024 * 1024
MSG_IMAGE_TOO_BIG = 'حجم الصورة أكبر من الحد المسموح (5 MB).'


class HomeworkImageError(ValueError):
    """Image attachment refused; ``str(exc)`` is a user-facing Arabic message."""


def prepare_homework_upload(file: FileStorage) -> FileStorage:
    """Return the FileStorage to pass to save_uploaded_file().

    Raises HomeworkImageError for an image-named upload larger than 5 MB or
    whose bytes are not a valid still JPEG/PNG/WEBP image, before anything
    is decoded or reaches Storage.
    """
    name = file.filename or ''
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext not in HOMEWORK_IMAGE_EXTS:
        return file
    raw = file.read(HOMEWORK_IMAGE_MAX_BYTES + 1)
    if len(raw) > HOMEWORK_IMAGE_MAX_BYTES:
        raise HomeworkImageError(MSG_IMAGE_TOO_BIG)
    try:
        optimized = optimize_board_image(raw)
    except BoardImageError as exc:
        raise HomeworkImageError(str(exc)) from None
    return FileStorage(io.BytesIO(optimized.data), filename='homework.webp',
                       content_type='image/webp')

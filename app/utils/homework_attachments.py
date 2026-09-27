"""
Homework attachment preparation — image attachments are optimised ONCE, at
upload time; PDFs pass through untouched.

Image attachments (jpg / jpeg / png / webp by filename) reuse the School Board
optimiser unchanged (app.utils.board_images): real decode restricted to
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


class HomeworkImageError(ValueError):
    """Image attachment refused; ``str(exc)`` is a user-facing Arabic message."""


def prepare_homework_upload(file: FileStorage) -> FileStorage:
    """Return the FileStorage to pass to save_uploaded_file().

    Raises HomeworkImageError for an image-named upload whose bytes are not a
    valid still JPEG/PNG/WEBP image, before anything reaches Storage.
    """
    name = file.filename or ''
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext not in HOMEWORK_IMAGE_EXTS:
        return file
    try:
        optimized = optimize_board_image(file.read())
    except BoardImageError as exc:
        raise HomeworkImageError(str(exc)) from None
    return FileStorage(io.BytesIO(optimized.data), filename='homework.webp',
                       content_type='image/webp')

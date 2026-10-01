"""
Employee DOCUMENT uploads — one validation/processing path, BEFORE any Storage.

Applies to NEW employee documents only (Add Employee wizard and the employee
Documents page). Existing EmployeeDocument rows and their stored objects — of
ANY type previously accepted, DOC/DOCX included — are never read, converted,
rewritten or deleted here; they keep viewing/downloading through the unchanged
read paths.

New-upload policy:
  * allow-list: pdf, jpg, jpeg, png (DOC/DOCX/GIF/WEBP/anything else refused);
  * <= EMPLOYEE_DOC_MAX_BYTES (5 MB), measured from the stream itself — never
    the filename, MIME type or Content-Length;
  * magic bytes must match the extension (the same signatures as the student
    document policy);
  * PDF → returned unchanged, stored byte-for-byte (never converted);
  * JPG/JPEG/PNG → app.utils.student_documents.optimize_document_image (the
    deployed, generic document-image pipeline): real decode, 40 MP ceiling,
    animated/APNG refused, EXIF orientation, EXIF/GPS/XMP stripped (RGB ICC
    kept), <= 1200 px (never upscaled, LANCZOS), WebP q75 with the proven
    lossless-WebP / metadata-free-PNG fallback — exactly the Student Document
    policy, from the same function. Only the processed bytes are stored; the
    original image is never stored.
  * title / doc_type are validated against their column lengths first.
"""
from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)

EMPLOYEE_DOC_ALLOWED_EXTS = frozenset({'pdf', 'jpg', 'jpeg', 'png'})
# What a NEW document is stored as: PDF bytes as-is, images as the optimised
# WebP (or a metadata-free PNG when that is smaller).
EMPLOYEE_DOC_STORED_EXTS = frozenset({'pdf', 'webp', 'png'})
EMPLOYEE_DOC_MAX_BYTES = 5 * 1024 * 1024          # 5 MB per file
EMPLOYEE_DOC_SUBFOLDER = 'employee_docs'
EMPLOYEE_DOC_TITLE_MAX = 200                       # EmployeeDocument.title
EMPLOYEE_DOC_TYPE_MAX = 80                         # EmployeeDocument.doc_type

_MAGIC = {
    'pdf':  (b'%PDF',),
    'png':  (b'\x89PNG\r\n\x1a\n',),
    'jpg':  (b'\xff\xd8\xff',),
    'jpeg': (b'\xff\xd8\xff',),
}

# Generic on purpose — never echo a filename, path, byte count or internal detail.
MSG_UNSUPPORTED = ('نوع الملف غير مدعوم. الصيغ المسموح بها للمستندات الجديدة: '
                   'PDF أو JPG أو JPEG أو PNG.')
MSG_EMPTY = 'الملف المرفوع فارغ.'
MSG_TOO_BIG = 'حجم الملف أكبر من الحد المسموح (5 ميجابايت).'
MSG_MISMATCH = 'محتوى الملف لا يطابق صيغته. يرجى رفع ملف صالح.'
MSG_UNREADABLE = 'تعذّر قراءة الملف المرفوع. يرجى المحاولة مرة أخرى.'
MSG_FILE_REQUIRED = 'يرجى اختيار ملف المستند.'
MSG_TITLE_REQUIRED = 'يرجى إدخال عنوان المستند.'
MSG_TITLE_TOO_LONG = f'عنوان المستند يجب ألا يتجاوز {EMPLOYEE_DOC_TITLE_MAX} حرف.'
MSG_TYPE_TOO_LONG = f'نوع المستند يجب ألا يتجاوز {EMPLOYEE_DOC_TYPE_MAX} حرفاً.'
MSG_SAVE_FAILED = 'تعذّر حفظ المستند. لم يتم حفظ أي تغيير. يرجى المحاولة مرة أخرى.'


def validate_document_meta(title: str, doc_type: str, *, title_required: bool = True):
    """Arabic message when title/doc_type (already stripped) are invalid, else None."""
    if title_required and not title:
        return MSG_TITLE_REQUIRED
    if len(title) > EMPLOYEE_DOC_TITLE_MAX:
        return MSG_TITLE_TOO_LONG
    if len(doc_type) > EMPLOYEE_DOC_TYPE_MAX:
        return MSG_TYPE_TOO_LONG
    return None


def prepare_employee_document(file_storage):
    """Validate and process ONE new employee document BEFORE anything is stored.

    Returns ``(upload, None)`` — the FileStorage to hand to
    save_employee_document() — or ``(None, message)`` with a ready-to-flash
    Arabic message. Nothing is written anywhere.
    """
    from werkzeug.datastructures import FileStorage
    from app.utils.student_documents import (MSG_INVALID, StudentDocumentImageError,
                                             optimize_document_image)

    name = (file_storage.filename or '') if file_storage else ''
    if not name:
        return None, MSG_FILE_REQUIRED
    ext = name.rsplit('.', 1)[1].lower() if '.' in name else ''
    if ext not in EMPLOYEE_DOC_ALLOWED_EXTS:
        return None, MSG_UNSUPPORTED

    try:
        stream = file_storage.stream
        pos = stream.tell()
        stream.seek(0, 2)                          # SEEK_END
        size = stream.tell() - pos
        stream.seek(pos)
        head = stream.read(16)
        stream.seek(pos)
    except Exception:
        return None, MSG_UNREADABLE
    if size <= 0:
        return None, MSG_EMPTY
    if size > EMPLOYEE_DOC_MAX_BYTES:
        return None, MSG_TOO_BIG
    if not any(head.startswith(sig) for sig in _MAGIC[ext]):
        return None, MSG_MISMATCH

    if ext == 'pdf':
        return file_storage, None                  # byte-for-byte, never converted

    try:
        try:
            raw = stream.read()
        finally:
            stream.seek(pos)
        processed = optimize_document_image(raw)
    except StudentDocumentImageError as exc:
        return None, str(exc)
    except Exception:
        log.warning('[employee_documents] image processing failed', exc_info=True)
        return None, MSG_INVALID
    content_type = 'image/webp' if processed.ext == 'webp' else 'image/png'
    return FileStorage(io.BytesIO(processed.data), filename=f'document.{processed.ext}',
                       content_type=content_type), None


def save_employee_document(upload) -> str | None:
    """Store one PREPARED document as a new employee_docs/<uuid>.<ext> object.

    Returns the stored value (same convention as every other upload) or None on
    any failure (logged). Never raises. The stored-extension allow-list is a
    last gate: only pdf / webp / png can ever be written here.
    """
    try:
        from app.utils.helpers import save_uploaded_file
        return save_uploaded_file(upload, EMPLOYEE_DOC_SUBFOLDER,
                                  allowed_exts=set(EMPLOYEE_DOC_STORED_EXTS))
    except Exception:
        log.warning('[employee_documents] storage failed', exc_info=True)
        return None

"""
Homework attachment preparation.

Two pipelines live here.

1. ``prepare_homework_upload`` — the ORIGINAL single-attachment pipeline, kept
   byte-for-byte for INSTITUTE homework (web institute branch and
   /teacher/institute/homework). Image attachments (jpg / jpeg / png / webp by
   filename) are refused above 5 MB before any decode, then optimised with the
   School Board policy unchanged (app.utils.board_images: <= 1600 px, WebP q80).
   PDFs and other uploads are returned as-is.

2. NORMAL-SCHOOL homework (web create/edit school branch, mobile
   /teacher/homework create/update): up to 3 images OR exactly one PDF.

   * Input: multipart field ``attachments[]`` (several files); the legacy
     single ``attachment`` field is still accepted. Sending real files in both
     fields is refused, never silently merged or dropped.
   * Everything is validated before any Storage write: allowed extensions,
     never images + PDF, at most one PDF, at most 3 images, <= 5 MiB raw per
     image, <= 14 MiB raw for all images together, <= 5 MiB per PDF, a real
     PDF header, then every image is decoded and optimised.
   * Images: homework-specific policy — longest side <= 768 px (never
     upscaled), WebP quality 85, EXIF (incl. GPS) / XMP dropped, orientation
     applied, animated images refused. Only the optimised WebP is stored;
     the original upload never is. Other features keep their own policy.
   * Image #1 (or the PDF) stays on Homework.attachment_path /
     attachment_type; images #2 / #3 are HomeworkImage rows.

Bytes never reach PostgreSQL: only Storage paths are recorded. Student /
employee photos, registration photos and AI Face never use this module.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

from werkzeug.datastructures import FileStorage

from app.utils.board_images import BoardImageError, optimize_board_image

HOMEWORK_IMAGE_EXTS = frozenset({'jpg', 'jpeg', 'png', 'webp'})
# Same incoming limit as School Board images; checked on the raw upload,
# before any decode.
HOMEWORK_IMAGE_MAX_BYTES = 5 * 1024 * 1024
MSG_IMAGE_TOO_BIG = 'حجم الصورة أكبر من الحد المسموح (5 MB).'


class HomeworkImageError(ValueError):
    """Image attachment refused; ``str(exc)`` is a user-facing Arabic message."""


def prepare_homework_upload(file: FileStorage) -> FileStorage:
    """INSTITUTE homework only — return the FileStorage to pass to
    save_uploaded_file().

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


# ═════════════════════════════════════════════════════════════════════════════
#  NORMAL-SCHOOL HOMEWORK: up to 3 images OR one PDF
# ═════════════════════════════════════════════════════════════════════════════

HOMEWORK_MAX_IMAGES = 3
HOMEWORK_TOTAL_IMAGE_MAX_BYTES = 14 * 1024 * 1024
HOMEWORK_PDF_MAX_BYTES = 5 * 1024 * 1024
HOMEWORK_PDF_EXTS = frozenset({'pdf'})
HOMEWORK_IMAGE_MAX_SIDE = 768
HOMEWORK_IMAGE_WEBP_QUALITY = 85

FIELD_MULTI = 'attachments[]'
FIELD_LEGACY = 'attachment'
FIELD_REMOVE = 'remove_attachments[]'
FIELD_CLEAR = 'clear_attachment'
PRIMARY_TOKEN = 'primary'

MODE_IMAGE = 'image'
MODE_PDF = 'pdf'

# The exact string the legacy single-``attachment`` mobile contract returned
# for a disallowed extension; kept for callers that still use that field.
LEGACY_MOBILE_TYPE_ERROR = 'invalid_attachment — allowed: jpg, jpeg, png, webp, pdf'
MSG_TYPE = 'نوع الملف غير مسموح. الأنواع المقبولة: jpg, jpeg, png, webp, pdf.'

_MESSAGES = {
    'invalid_file_type': MSG_TYPE,
    'too_many_images': 'يمكن إرفاق 3 صور كحد أقصى.',
    'mixed_attachment_types': ('لا يمكن الجمع بين الصور وملف PDF في الواجب نفسه. '
                               'يمكن إرفاق حتى 3 صور أو ملف PDF واحد.'),
    'multiple_pdfs': 'يمكن إرفاق ملف PDF واحد فقط.',
    'image_too_large': MSG_IMAGE_TOO_BIG,
    'images_total_too_large': 'الحجم الإجمالي للصور أكبر من الحد المسموح (14 MB).',
    'pdf_too_large': 'حجم ملف PDF أكبر من الحد المسموح (5 MB).',
    'invalid_pdf': 'تعذّر قراءة ملف PDF. يرجى رفع ملف PDF صالح.',
    'conflicting_attachment_fields': 'أُرسلت المرفقات في حقلين مختلفين. يرجى إعادة الإرسال.',
    'invalid_remove_target': 'المرفق المطلوب حذفه غير موجود. يرجى تحديث الصفحة والمحاولة مجدداً.',
    'upload_failed': 'تعذّر حفظ المرفق. يرجى المحاولة مجدداً.',
}


class HomeworkAttachmentError(ValueError):
    """School homework attachment refused.

    ``code`` is the stable API error code, ``message`` the Arabic text shown
    to users, ``status`` the HTTP status for JSON callers.
    """

    def __init__(self, code: str, message: str | None = None, status: int = 400):
        self.code = code
        self.message = message or _MESSAGES.get(code, code)
        self.status = status
        super().__init__(self.message)

    @property
    def legacy_error(self) -> str:
        """``error`` value for a caller that used the legacy ``attachment``
        field: the same strings that contract always returned (Arabic image
        messages, the English extension message)."""
        if self.code == 'invalid_file_type':
            return LEGACY_MOBILE_TYPE_ERROR
        return self.message


def _ext(filename: str) -> str:
    return filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''


@dataclass
class IncomingAttachments:
    """Validated raw uploads of one request (not yet decoded or stored)."""
    mode: str | None = None                      # None | 'image' | 'pdf'
    items: list = field(default_factory=list)    # [(ext, raw bytes)]
    legacy_field: bool = False                   # sent through ``attachment``

    @property
    def count(self) -> int:
        return len(self.items)


def uses_legacy_field(files) -> bool:
    """True when real files arrived ONLY through the legacy ``attachment``
    field — such callers keep receiving the legacy error strings."""
    def real(name):
        return any(f and f.filename for f in files.getlist(name))
    return real(FIELD_LEGACY) and not real(FIELD_MULTI)


def collect_homework_attachments(files) -> IncomingAttachments:
    """Steps 1-9: collect real files, decide NONE / IMAGES / PDF, enforce the
    count and raw size limits. Reads each file at most limit + 1 bytes.
    Nothing is decoded and nothing reaches Storage. Raises
    HomeworkAttachmentError.
    """
    multi = [f for f in files.getlist(FIELD_MULTI) if f and f.filename]
    legacy = [f for f in files.getlist(FIELD_LEGACY) if f and f.filename]
    if multi and legacy:
        raise HomeworkAttachmentError('conflicting_attachment_fields')
    chosen = multi or legacy
    if not chosen:
        return IncomingAttachments()

    exts = [_ext(f.filename) for f in chosen]
    if any(e not in HOMEWORK_IMAGE_EXTS and e not in HOMEWORK_PDF_EXTS for e in exts):
        raise HomeworkAttachmentError('invalid_file_type')
    n_pdf = sum(1 for e in exts if e in HOMEWORK_PDF_EXTS)
    n_img = len(exts) - n_pdf
    if n_pdf and n_img:
        raise HomeworkAttachmentError('mixed_attachment_types')
    if n_pdf > 1:
        raise HomeworkAttachmentError('multiple_pdfs')
    if n_img > HOMEWORK_MAX_IMAGES:
        raise HomeworkAttachmentError('too_many_images')

    items, total = [], 0
    for f, ext in zip(chosen, exts):
        is_pdf = ext in HOMEWORK_PDF_EXTS
        limit = HOMEWORK_PDF_MAX_BYTES if is_pdf else HOMEWORK_IMAGE_MAX_BYTES
        raw = f.read(limit + 1)
        if len(raw) > limit:
            raise HomeworkAttachmentError('pdf_too_large' if is_pdf else 'image_too_large')
        if not is_pdf:
            total += len(raw)
            if total > HOMEWORK_TOTAL_IMAGE_MAX_BYTES:
                raise HomeworkAttachmentError('images_total_too_large')
        items.append((ext, raw))
    return IncomingAttachments(MODE_PDF if n_pdf else MODE_IMAGE, items, bool(legacy))


def optimize_homework_image(raw: bytes) -> bytes:
    """School homework image policy: <= 768 px longest side, never upscaled,
    WebP q85, metadata stripped. Raises HomeworkAttachmentError('invalid_image')
    carrying the specific Arabic reason."""
    try:
        return optimize_board_image(raw, max_side=HOMEWORK_IMAGE_MAX_SIDE,
                                    quality=HOMEWORK_IMAGE_WEBP_QUALITY).data
    except BoardImageError as exc:
        raise HomeworkAttachmentError('invalid_image', str(exc)) from None


@dataclass
class PreparedAttachments:
    """Bytes ready for Storage: optimised WebP images or the PDF untouched."""
    mode: str | None = None
    blobs: list = field(default_factory=list)    # [(data, ext)]


def prepare_homework_attachments(incoming: IncomingAttachments) -> PreparedAttachments:
    """Steps 10-12: verify the PDF header / decode and optimise ALL images.
    A failure on any file means nothing has been stored yet."""
    if incoming.mode == MODE_PDF:
        (_, raw), = incoming.items
        # A PDF header may be preceded by junk, but must start in the first KiB.
        if b'%PDF-' not in raw[:1024]:
            raise HomeworkAttachmentError('invalid_pdf')
        return PreparedAttachments(MODE_PDF, [(raw, 'pdf')])
    if incoming.mode == MODE_IMAGE:
        return PreparedAttachments(
            MODE_IMAGE, [(optimize_homework_image(raw), 'webp') for _, raw in incoming.items])
    return PreparedAttachments()


_CONTENT_TYPES = {'webp': 'image/webp', 'pdf': 'application/pdf'}


def store_homework_attachments(prepared: PreparedAttachments) -> list[str]:
    """Step 13: upload each prepared file ONCE through the existing
    save_uploaded_file() into the existing homework location. If any upload
    fails, the files already stored by this call are removed and
    HomeworkAttachmentError('upload_failed', status 503) is raised."""
    from app.utils.helpers import save_uploaded_file

    stored: list[str] = []
    for data, ext in prepared.blobs:
        path = save_uploaded_file(
            FileStorage(io.BytesIO(data), filename=f'homework.{ext}',
                        content_type=_CONTENT_TYPES[ext]),
            subfolder='homework', allowed_exts={ext})
        if not path:
            discard_unreferenced_uploads(stored)
            raise HomeworkAttachmentError('upload_failed', status=503)
        stored.append(path)
    return stored


def discard_unreferenced_uploads(paths) -> None:
    """Best-effort removal of files THIS request stored, after its DB work was
    rolled back (or never happened). A path still referenced by any homework
    or homework image — e.g. one shared by «جميع الشعب» rows — is kept. If the
    check itself fails the file is kept: an orphan is safer than a deleted
    file somebody still uses."""
    from app.models import db, Homework, HomeworkImage
    from app.utils.helpers import delete_uploaded_file

    opts = {'bypass_tenant_scope': True, 'include_all_years': True}
    for path in dict.fromkeys(p for p in paths if p):
        try:
            referenced = (
                db.session.query(Homework.id).execution_options(**opts)
                .filter(Homework.attachment_path == path).first() is not None
                or db.session.query(HomeworkImage.id).execution_options(**opts)
                .filter(HomeworkImage.file_path == path).first() is not None)
        except Exception:
            continue
        if not referenced:
            delete_uploaded_file(path)


# ── Reading and writing the stored attachment state ──────────────────────────

def load_extra_images(homework_ids, school_id: int) -> dict:
    """{homework_id: [HomeworkImage, ...]} in sort order, for a whole page in
    ONE query (never one query per homework). Explicitly school-filtered."""
    from app.models import HomeworkImage

    ids = sorted({i for i in homework_ids if i})
    if not ids or not school_id:
        return {}
    rows = (HomeworkImage.query
            .filter(HomeworkImage.school_id == school_id,
                    HomeworkImage.homework_id.in_(ids))
            .order_by(HomeworkImage.homework_id, HomeworkImage.sort_order)
            .all())
    out: dict = {}
    for row in rows:
        out.setdefault(row.homework_id, []).append(row)
    return out


def image_paths(hw, extras) -> list[str]:
    """Stored image paths in display order: attachment_path first, then the
    extra rows. Empty for a PDF, another file type or no attachment."""
    if hw.attachment_type != MODE_IMAGE or not hw.attachment_path:
        return []
    return [hw.attachment_path] + [e.file_path for e in (extras or [])]


def images_payload(paths, url_of, primary_url=None) -> list[dict]:
    """Additive API field ``images``: [{"url": ..., "type": "image"}] for the
    stored image ``paths`` (see image_paths), at most 3. ``primary_url`` reuses
    the already-resolved attachment_url instead of resolving image #1 twice."""
    out = []
    for i, path in enumerate(paths):
        url = primary_url if (i == 0 and primary_url) else url_of(path)
        if url:
            out.append({'url': url, 'type': MODE_IMAGE})
    return out


def plan_attachment_edit(hw, extras, incoming: IncomingAttachments,
                         remove_tokens, clear_all: bool) -> list[str] | None:
    """Decide which CURRENT stored paths an EDIT keeps (in order), before
    anything is decoded or stored. Returns None when the request does not
    touch attachments at all — the stored attachment then stays exactly as it
    is (no read of image rows, no write, no re-encode).

    Rules (final state is always NONE, 1-3 images, or one PDF/file):
      * ``remove_attachments[]`` tokens ('primary' or an image id of THIS
        homework) remove those attachments; an unknown token is refused;
      * ``clear_attachment=1`` removes every current attachment;
      * a file sent through the legacy ``attachment`` field replaces every
        current attachment (the old single-attachment contract);
      * new images through ``attachments[]`` are ADDED after the remaining
        images (max 3 in total, refused — never truncated — above that); they
        replace a current PDF / other file;
      * a new PDF replaces every current attachment.
    """
    tokens = {str(t).strip() for t in (remove_tokens or []) if str(t).strip()}
    if incoming.mode is None and not tokens and not clear_all:
        return None

    if hw.attachment_path and hw.attachment_type == MODE_IMAGE:
        current = [(PRIMARY_TOKEN, hw.attachment_path)] + [
            (str(e.id), e.file_path) for e in (extras or [])]
        current_is_images = True
    elif hw.attachment_path:
        current, current_is_images = [(PRIMARY_TOKEN, hw.attachment_path)], False
    else:
        current, current_is_images = [], False

    if tokens - {t for t, _ in current}:
        raise HomeworkAttachmentError('invalid_remove_target')

    drop_all = clear_all or (incoming.legacy_field and incoming.mode is not None)
    remaining = [] if drop_all else [p for t, p in current if t not in tokens]

    if incoming.mode == MODE_PDF:
        return []
    if incoming.mode == MODE_IMAGE:
        keep = remaining if current_is_images else []
        if len(keep) + incoming.count > HOMEWORK_MAX_IMAGES:
            raise HomeworkAttachmentError('too_many_images')
        return keep
    return remaining


def add_new_attachments(hw, mode: str | None, paths: list[str]) -> None:
    """CREATE: record freshly stored paths on a new homework. hw.id must exist
    (flushed) when there is more than one image."""
    from app.models import db, HomeworkImage

    hw.attachment_path = paths[0] if paths else None
    hw.attachment_type = mode if paths else None
    if mode == MODE_IMAGE:
        db.session.add_all([
            HomeworkImage(homework_id=hw.id, school_id=hw.school_id,
                          file_path=path, sort_order=order)
            for order, path in enumerate(paths[1:], start=1)])


def apply_attachment_edit(hw, extras, keep: list[str],
                          new_mode: str | None, new_paths: list[str]) -> None:
    """EDIT: make the homework's attachments exactly keep + new_paths.

    The first remaining image is always (re)written to attachment_path, so a
    removed image #1 promotes the next one. An unchanged kept path is the same
    string — the stored file is never re-encoded, re-uploaded or deleted
    (removed files are left in Storage, as every replaced homework attachment
    always was; they may be shared by «جميع الشعب» rows).
    """
    from app.models import HomeworkImage

    final = list(keep) + list(new_paths)
    if new_mode:
        mode = new_mode
    elif final:
        mode = hw.attachment_type          # removals only: kind is unchanged
    else:
        mode = None
    if extras:
        # Executed immediately (before the INSERT flush), so re-numbered rows
        # never collide with uq_homework_image_homework_sort.
        (HomeworkImage.query
         .filter(HomeworkImage.homework_id == hw.id,
                 HomeworkImage.school_id == hw.school_id)
         .delete(synchronize_session=False))
    add_new_attachments(hw, mode, final)

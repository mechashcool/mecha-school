"""
Mobile API — "أرسل درسي" (institute study requests), parent side
==================================================================
A parent sends 1–5 lesson images plus an optional note, for one of their
ACTIVE children, to the institute ADMINISTRATION. Institutes only.

POST   /parent/children/<id>/study-requests             create (multipart)
GET    /parent/children/<id>/study-requests             history (paginated)
GET    /parent/children/<id>/study-requests/<rid>       detail + images
DELETE /parent/children/<id>/study-requests/<rid>       sender, pending only

Authorization (every route): JWT + role parent → the authenticated user's own
school must be an institute (_user_institute) → the child must be linked to
this parent and belong to the same school (_assert_owns_student) → the child
must be active. Every failure is a JSON response; ownership failures are 404
so another family's / school's ids reveal nothing.

Every linked parent of the child may VIEW its requests; only the sender may
DELETE, and only while the request is still pending.

Database pressure:
  * the list reads ONLY institute_study_requests (stored image_count, no
    image rows, no COUNT, no URLs) with limit+1 pagination — no total COUNT;
  * image rows are read only by the detail route;
  * create = one bounded pending-limit read, one request INSERT, one image
    INSERT batch, ONE commit;
  * image bytes never reach PostgreSQL — Supabase Storage (bucket `uploads`)
    holds them, the table holds metadata only.

History policy: InstituteStudyRequest is year-scoped. Parents see the child's
history across academic years, so list/detail/delete read with
include_all_years=True — and always filter explicitly on school_id AND
student_id (the school tenant criteria stay active as well), so another
school's or another child's rows can never appear.
"""
import io
from datetime import timezone

from flask import current_app, g, jsonify, request
from sqlalchemy import delete as sa_delete
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import NotFound

from app.models import db, InstituteStudyRequest, InstituteStudyRequestImage
from app.utils.helpers import delete_uploaded_file, save_uploaded_file
from app.utils.ratelimit import limiter
from app.utils.scoping import current_academic_year_id

from . import mobile_api_bp
from .parent import _assert_owns_student, _current_year_id, _user_institute
from .utils import err, jwt_required, ok, page_args, photo_url, role_required


# ─── Limits ───────────────────────────────────────────────────────────────────

MAX_IMAGES          = 5
MAX_IMAGE_BYTES     = 5 * 1024 * 1024          # per ORIGINAL image
MAX_NOTE_CHARS      = 1000
MAX_PENDING         = 10                       # per student
CREATE_RATE_LIMIT   = '20 per hour'            # per parent user

_BUCKET    = 'uploads'
_ALLOWED   = {'jpg', 'jpeg', 'png'}
_MAGIC     = {'png': b'\x89PNG\r\n\x1a\n', 'jpg': b'\xff\xd8\xff', 'jpeg': b'\xff\xd8\xff'}
_STORED    = {'webp', 'png'}                   # optimize_document_image output
_MIME      = {'webp': 'image/webp', 'png': 'image/png'}
_ALL_YEARS = {'include_all_years': True}

_STATUS_LABELS = {
    InstituteStudyRequest.STATUS_PENDING: 'جديد',
    InstituteStudyRequest.STATUS_VIEWED:  'تم الاطلاع',
}


def _rate_key():
    """Per-parent limiter key. The limiter wrapper runs AFTER jwt_required, so
    the authenticated user is known; the address is only a defensive fallback."""
    user = g.get('mobile_user')
    if user is not None:
        return f'study-request:{user.id}'
    return request.remote_addr or 'anonymous'


# ─── Guard ────────────────────────────────────────────────────────────────────

def _authorized_child(student_id):
    """(school, student, None) or (None, None, json_error). Institute first, so
    a school-type account never reaches the ownership queries."""
    school = _user_institute()
    if school is None:
        return None, None, err('institute_not_available', 404)
    try:
        student = _assert_owns_student(student_id)
    except NotFound:
        return None, None, err('student_not_found', 404)
    if student.status != 'active':
        return None, None, err('student_not_active', 403)
    return school, student, None


# ─── Serialisers ──────────────────────────────────────────────────────────────

def _iso(dt):
    return dt.replace(tzinfo=timezone.utc).isoformat() if dt else None


def _item(row, student_id, user_id) -> dict:
    """List/detail fields. Never exposes the sender or the reviewer."""
    pending = row.status == InstituteStudyRequest.STATUS_PENDING
    return {
        'id':           row.id,
        'student_id':   student_id,
        'note':         row.note,
        'status':       row.status,
        'status_label': _STATUS_LABELS.get(row.status, row.status),
        'image_count':  row.image_count,
        'admin_note':   row.admin_note,
        'viewed_at':    _iso(row.viewed_at),
        'created_at':   _iso(row.created_at),
        'can_delete':   pending and row.parent_id == user_id,
    }


# Only the columns the list/detail need — no ORM entities, no relationships.
_COLS = (InstituteStudyRequest.id, InstituteStudyRequest.parent_id,
         InstituteStudyRequest.note, InstituteStudyRequest.status,
         InstituteStudyRequest.image_count, InstituteStudyRequest.admin_note,
         InstituteStudyRequest.viewed_at, InstituteStudyRequest.created_at)


def _child_requests(school_id, student_id):
    return (db.session.query(*_COLS)
            .execution_options(**_ALL_YEARS)
            .filter(InstituteStudyRequest.school_id == school_id,
                    InstituteStudyRequest.student_id == student_id))


# ─── Image intake ─────────────────────────────────────────────────────────────

def _image_error(code, index):
    return jsonify({'ok': False, 'error': code, 'index': index}), 400


def _process_images(files):
    """Validate + optimise every image BEFORE anything is uploaded.

    Returns ([(bytes, ext)], None) or (None, json_error). Per image: extension
    allow-list → size cap read from the stream → magic bytes (no decode) →
    optimize_document_image(), the project's single image pipeline, which does
    the one real decode (JPEG/PNG only, pixel ceiling, no animation), strips
    EXIF/GPS and re-encodes ≤1200 px WebP q75. Sequential, and only the small
    optimised output is kept — each original buffer is released at once.
    """
    from app.utils.student_documents import (MSG_TOO_LARGE, StudentDocumentImageError,
                                             optimize_document_image)
    processed = []
    for index, upload in enumerate(files):
        name = upload.filename or ''
        ext = name.rsplit('.', 1)[1].lower() if '.' in name else ''
        if ext not in _ALLOWED:
            return None, _image_error('invalid_image', index)
        raw = upload.stream.read(MAX_IMAGE_BYTES + 1)
        if not raw:
            return None, _image_error('invalid_image', index)
        if len(raw) > MAX_IMAGE_BYTES:
            return None, _image_error('image_too_large', index)
        if not raw.startswith(_MAGIC[ext]):
            return None, _image_error('invalid_image', index)
        try:
            result = optimize_document_image(raw)
        except StudentDocumentImageError as exc:
            code = ('image_dimensions_too_large' if str(exc) == MSG_TOO_LARGE
                    else 'invalid_image')
            return None, _image_error(code, index)
        except Exception:
            current_app.logger.warning('[study-request] image processing failed',
                                       exc_info=True)
            return None, _image_error('invalid_image', index)
        del raw
        processed.append((result.data, result.ext))
    return processed, None


def _delete_files(paths, request_id=None):
    """Best-effort storage delete; never raises, never logs URLs/secrets."""
    failed = 0
    for path in paths:
        try:
            if not delete_uploaded_file(path, bucket=_BUCKET):
                failed += 1
        except Exception:
            failed += 1
    if failed:
        current_app.logger.warning(
            '[study-request] storage delete failed for %s of %s file(s) request_id=%s',
            failed, len(paths), request_id)


# ─── Routes ───────────────────────────────────────────────────────────────────

@mobile_api_bp.route('/parent/children/<int:student_id>/study-requests', methods=['POST'])
@jwt_required()
@role_required('parent')
@limiter.limit(CREATE_RATE_LIMIT, key_func=_rate_key)
def parent_create_study_request(student_id):
    user = g.mobile_user
    school, s, denied = _authorized_child(student_id)
    if denied:
        return denied

    if 'multipart/form-data' not in (request.content_type or ''):
        return err('multipart_required', 415)

    note = (request.form.get('note') or '').strip() or None
    if note is not None and len(note) > MAX_NOTE_CHARS:
        return err('note_too_long')

    files = [f for f in (request.files.getlist('images[]')
                         or request.files.getlist('images')) if f and f.filename]
    if not files:
        return err('images_required')
    if len(files) > MAX_IMAGES:
        return err('too_many_images')

    # Bounded: reads at most MAX_PENDING index entries, never the whole history.
    pending = (db.session.query(InstituteStudyRequest.id)
               .execution_options(**_ALL_YEARS)
               .filter(InstituteStudyRequest.school_id == school.id,
                       InstituteStudyRequest.student_id == s.id,
                       InstituteStudyRequest.status == InstituteStudyRequest.STATUS_PENDING)
               .limit(MAX_PENDING).count())
    if pending >= MAX_PENDING:
        return err('pending_limit_reached', 409)

    year_id = current_academic_year_id() or _current_year_id(school.id)
    if not year_id:
        return err('no_active_academic_year', 409)

    images, bad = _process_images(files)
    if bad:
        return bad

    subfolder = f'schools/{school.id}/institute-study-requests/{s.id}'
    uploaded = []
    for index, (data, ext) in enumerate(images):
        path = save_uploaded_file(
            FileStorage(io.BytesIO(data), filename=f'image.{ext}',
                        content_type=_MIME[ext]),
            subfolder, bucket=_BUCKET, allowed_exts=_STORED, local_fallback=False)
        if not path:
            _delete_files(uploaded)
            return jsonify({'ok': False, 'error': 'image_upload_failed',
                            'index': index}), 503
        uploaded.append(path)

    req = InstituteStudyRequest(
        school_id=school.id, academic_year_id=year_id, student_id=s.id,
        parent_id=user.id, note=note, image_count=len(uploaded))
    try:
        db.session.add(req)
        db.session.flush()                     # request id for the image rows
        db.session.add_all([
            InstituteStudyRequestImage(
                request_id=req.id, school_id=school.id, file_path=path,
                mime=_MIME[ext], size_bytes=len(data), sort_order=order)
            for order, (path, (data, ext)) in enumerate(zip(uploaded, images))])
        # Serialised from the flushed row, before commit expires it — no
        # post-commit refresh SELECT.
        payload = _item(req, s.id, user.id)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception('[study-request] create failed student_id=%s', s.id)
        _delete_files(uploaded)
        return err('server_error', 500)

    return ok(message='study_request_created', request=payload), 201


@mobile_api_bp.route('/parent/children/<int:student_id>/study-requests', methods=['GET'])
@jwt_required()
@role_required('parent')
def parent_list_study_requests(student_id):
    user = g.mobile_user
    school, s, denied = _authorized_child(student_id)
    if denied:
        return denied

    limit, offset = page_args(default_limit=20, max_limit=50)
    rows = (_child_requests(school.id, s.id)
            .order_by(InstituteStudyRequest.created_at.desc(),
                      InstituteStudyRequest.id.desc())
            .offset(offset).limit(limit + 1).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    return ok(student_id=s.id, count=len(rows), limit=limit, offset=offset,
              has_more=has_more, next_offset=offset + limit if has_more else None,
              requests=[_item(r, s.id, user.id) for r in rows])


@mobile_api_bp.route('/parent/children/<int:student_id>/study-requests/<int:request_id>',
                     methods=['GET'])
@jwt_required()
@role_required('parent')
def parent_study_request_detail(student_id, request_id):
    user = g.mobile_user
    school, s, denied = _authorized_child(student_id)
    if denied:
        return denied

    row = (_child_requests(school.id, s.id)
           .filter(InstituteStudyRequest.id == request_id).first())
    if row is None:
        return err('study_request_not_found', 404)

    images = (db.session.query(InstituteStudyRequestImage.id,
                               InstituteStudyRequestImage.file_path,
                               InstituteStudyRequestImage.mime,
                               InstituteStudyRequestImage.size_bytes,
                               InstituteStudyRequestImage.sort_order)
              .filter(InstituteStudyRequestImage.request_id == row.id,
                      InstituteStudyRequestImage.school_id == school.id)
              .order_by(InstituteStudyRequestImage.sort_order,
                        InstituteStudyRequestImage.id)
              .all())
    data = _item(row, s.id, user.id)
    # URLs only here, through the existing mobile mechanism (signed proxy when
    # private uploads are on); file_path itself is never returned.
    data['images'] = [{'id': i.id, 'url': photo_url(i.file_path), 'mime': i.mime,
                       'size_bytes': i.size_bytes, 'sort_order': i.sort_order}
                      for i in images]
    return ok(request=data)


@mobile_api_bp.route('/parent/children/<int:student_id>/study-requests/<int:request_id>',
                     methods=['DELETE'])
@jwt_required()
@role_required('parent')
def parent_delete_study_request(student_id, request_id):
    user = g.mobile_user
    school, s, denied = _authorized_child(student_id)
    if denied:
        return denied

    row = (db.session.query(InstituteStudyRequest.parent_id, InstituteStudyRequest.status)
           .execution_options(**_ALL_YEARS)
           .filter(InstituteStudyRequest.id == request_id,
                   InstituteStudyRequest.school_id == school.id,
                   InstituteStudyRequest.student_id == s.id)
           .first())
    if row is None:
        return err('study_request_not_found', 404)
    if row.parent_id != user.id:
        return err('only_sender_can_delete', 403)
    if row.status != InstituteStudyRequest.STATUS_PENDING:
        return err('cannot_delete_viewed_request', 409)

    try:
        paths = db.session.execute(
            sa_delete(InstituteStudyRequestImage)
            .where(InstituteStudyRequestImage.request_id == request_id,
                   InstituteStudyRequestImage.school_id == school.id)
            .returning(InstituteStudyRequestImage.file_path)
            .execution_options(synchronize_session=False)).scalars().all()
        # The sender / pending conditions are repeated in the DELETE itself, so
        # a concurrent "viewed" update can never be deleted out from under it.
        deleted = db.session.execute(
            sa_delete(InstituteStudyRequest)
            .where(InstituteStudyRequest.id == request_id,
                   InstituteStudyRequest.school_id == school.id,
                   InstituteStudyRequest.student_id == s.id,
                   InstituteStudyRequest.parent_id == user.id,
                   InstituteStudyRequest.status == InstituteStudyRequest.STATUS_PENDING)
            .execution_options(synchronize_session=False)).rowcount
        if deleted != 1:
            db.session.rollback()
            return err('cannot_delete_viewed_request', 409)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception('[study-request] delete failed request_id=%s',
                                     request_id)
        return err('server_error', 500)

    # Only after the rows are gone for good: storage cleanup, best effort.
    _delete_files(paths, request_id)
    return ok(message='study_request_deleted')

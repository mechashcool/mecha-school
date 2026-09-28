"""
Public (external) student-registration blueprint — NO authentication.

A guardian opens a school's secret link (``/register/<token>``), fills the same
student/guardian fields the school configured for its internal Add Student page
(financial, account, device and other internal sections excluded), and submits a
StudentRegistrationRequest. They then track status at ``/register/track/<token>``.

Security posture (this is a hostile public surface):
  * School is resolved SERVER-SIDE from the sha256 of the presented token — never
    from any client-supplied school_id. Every failure mode returns one identical
    generic page (no existence/enumeration leak).
  * CSRF applies (blueprint is NOT exempt; anonymous sessions still carry a token).
  * Strict server-side field allow-list — unexpected / financial / account /
    device / internal fields are rejected.
  * Uploads: approved types only, magic-byte content check, private storage,
    server-generated filenames, per-file size cap. Every file (and every field
    length) is validated/prepared BEFORE the first Storage write. The photo gets
    the Student photo validation and is stored byte-identical (AI Face source);
    document images get the Student Document optimisation; PDFs are unchanged.
    New objects live under a versioned ``v2`` prefix (app/utils/registration_media).
  * Rate limited per (IP + token). Idempotent on (school_id, submission_nonce).
  * Responses are no-store; no credentials are ever shown here.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, current_app)
from sqlalchemy.exc import IntegrityError

from app.models import (db, School, AcademicYear, Grade, Notification,
                        StudentRegistrationRequest, StudentRegistrationRequestDocument)
from app.utils.ratelimit import limiter
from app.utils.student_form_config import (get_student_form_config,
                                           PUBLIC_ALLOWED_FIELDS)
from app.utils.features import get_enabled_features
from app.utils.helpers import save_uploaded_file
from app.utils.registration_media import (REGISTRATION_UPLOAD_MAX_BYTES,
                                          registration_document_subfolder,
                                          registration_photo_subfolder)
from app.utils.registration_tokens import (hash_token, verify_token,
                                           generate_token, normalize_name,
                                           normalize_text)
from app.utils.student_photo import MSG_TOO_LARGE as _MSG_PHOTO_DIMENSIONS
from app.utils.student_photo import validate_student_photo
# Same whitespace normalization used when the standard Iraqi grades are created,
# so display ordering matches stored names regardless of spacing variations.
from app.utils.iraqi_grades import _normalize as _normalize_grade_name
# Per-school educational-stage configuration. Legacy schools (NULL) are never
# filtered — their public form keeps showing exactly the grades it shows today.
from app.utils.school_stages import (InvalidStageConfiguration,
                                     grade_matches_stages, school_stages)
# Reuse the SAME school-scoped residential-area helpers as the internal Add
# Student form (single source of truth for loading + fail-closed validation).
from app.blueprints.students import (_school_residential_areas,
                                     _validate_residential_area_for_school)
# Same Student Document policy (validation + image optimisation) and the same
# guarded "remove only what nothing references" cleanup as the student forms.
from app.blueprints.students import (STUDENT_DOC_STORED_EXTS,
                                     _discard_unreferenced_upload,
                                     prepare_student_document_upload)

registration_bp = Blueprint(
    'registration', __name__,
    template_folder='../../templates/registration',
)

# ── Public form contract ────────────────────────────────────────────────────────
# The ONLY form keys accepted from the public form. Anything else (fee/account/
# device/internal fields, or unknown injected keys) causes the submission to be
# rejected. document_type[] is read via getlist.
# PUBLIC_ALLOWED_FIELDS covers the config-driven student/guardian fields. The
# extra literals are the always-present form plumbing plus the two public-safe,
# config/school-scoped inputs that are NOT part of PUBLIC_ALLOWED_FIELDS:
#   - notes             : the "ملاحظات" section textarea (public-safe section)
#   - residential_area_id: the same school-scoped residential-area selector the
#                          internal Add Student form uses (validated server-side)
_ALLOWED_FORM_KEYS = (PUBLIC_ALLOWED_FIELDS | {
    'csrf_token', 'submission_nonce', 'desired_grade_id', 'full_name',
    'notes', 'residential_area_id', 'document_type[]',
})
_ALLOWED_FILE_KEYS = {'photo', 'document_file[]'}

# Fields that must NEVER be accepted publicly — presence is treated as tampering.
# (residential_area_id is intentionally NOT here: it is an allow-listed, school-
# scoped, server-validated selector — see _ALLOWED_FORM_KEYS above.)
_FORBIDDEN_KEYS = {
    # financial
    'create_fee', 'fee_type_id', 'fee_notes', 'num_installments', 'discount',
    'total_amount', 'amount', 'pay_amount_1', 'pay_method_1',
    # account
    'create_parent_account', 'link_existing_parent_id', 'parent_username',
    'parent_password', 'username', 'password', 'role_id', 'is_active',
    'permissions',
    # device / internal placement / identifiers
    'employee_no_string', 'device_id', 'all_devices', 'section_id',
    'building_id', 'rfid_tag_id', 'student_id', 'status',
}

_PHOTO_EXTS = {'jpg', 'jpeg', 'png'}
_MAX_UPLOAD_BYTES = REGISTRATION_UPLOAD_MAX_BYTES   # 5 MB per file
_MAX_DOCS = 4
_MAX_TEXT_LEN = 255

# The stored (normalized) value must also fit its database column, so a value
# the database would refuse is rejected with a message — before any upload —
# instead of failing the INSERT after the files were already stored.
_LENGTH_CHECKED_FIELDS = ('full_name', 'nationality', 'phone', 'guardian_name',
                          'guardian_phone', 'guardian_email', 'guardian_relation')
_COLUMN_MAX = {key: StudentRegistrationRequest.__table__.c[key].type.length
               for key in _LENGTH_CHECKED_FIELDS}
_DOC_TYPE_MAX = StudentRegistrationRequestDocument.__table__.c.document_type.type.length

# Magic-byte signatures for the approved photo types — actual content, not the
# client-supplied extension / MIME type, decides acceptance.
_MAGIC = {
    'png':  (b'\x89PNG\r\n\x1a\n',),
    'jpg':  (b'\xff\xd8\xff',),
    'jpeg': (b'\xff\xd8\xff',),
}

_MSG_TOO_LONG = 'أحد الحقول يتجاوز الطول المسموح.'
_MSG_PHOTO_TYPE = 'صيغة الصورة غير مدعومة. الصيغ المسموح بها: JPG أو JPEG أو PNG.'
_MSG_PHOTO_SIZE = 'حجم الصورة أكبر من الحد المسموح (5 ميغابايت).'
_MSG_PHOTO_INVALID = 'تعذّر قراءة صورة الطالب. يرجى رفع صورة صالحة بصيغة JPG أو JPEG أو PNG.'
_MSG_DOC_SIZE = 'حجم الملف أكبر من الحد المسموح (5 ميغابايت).'


# ── Rate-limit key: IP + token (so one school never blocks another) ─────────────

def _ip_token_key():
    from flask_limiter.util import get_remote_address
    va = request.view_args or {}
    tok = va.get('token') or va.get('tracking_token') or ''
    return f'{get_remote_address()}:{hashlib.sha256(tok.encode()).hexdigest()[:16]}'


# ── No-store on every public registration response ─────────────────────────────

@registration_bp.after_request
def _no_store(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, private'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


# ── Helpers ─────────────────────────────────────────────────────────────────────

def _generic_unavailable():
    """Identical response for every failure mode — no existence/enumeration leak."""
    return render_template('registration/unavailable.html'), 404


def _resolve_school(token: str | None):
    """Resolve the active school from a presented registration token, or None.

    Constant-time verification; fails closed for inactive schools or a disabled
    feature. School is never taken from any client-supplied id.
    """
    h = hash_token(token or '')
    if not h:
        return None
    school = School.query.filter_by(registration_token_hash=h).first()
    if school is None:
        return None
    if not school.is_active or not school.external_registration_enabled:
        return None
    if not verify_token(token, school.registration_token_hash):
        return None
    return school


def _active_year(school):
    return (AcademicYear.query.execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school.id, is_current=True).first())


# Canonical display order for the public registration grade dropdown.
# Display only — never used to create, rename, or filter grades.
_GRADE_DISPLAY_ORDER: list[str] = [
    'الصف الأول الابتدائي',
    'الصف الثاني الابتدائي',
    'الصف الثالث الابتدائي',
    'الصف الرابع الابتدائي',
    'الصف الخامس الابتدائي',
    'الصف السادس الابتدائي',
    'الصف الأول المتوسط',
    'الصف الثاني المتوسط',
    'الصف الثالث المتوسط',
    'الصف الرابع الإعدادي العلمي',
    'الصف الرابع الإعدادي الأدبي',
    'الصف الخامس الإعدادي العلمي',
    'الصف الخامس الإعدادي الأدبي',
    'الصف السادس الإعدادي العلمي',
    'الصف السادس الإعدادي الأدبي',
]
_GRADE_RANK = {_normalize_grade_name(n): i
               for i, n in enumerate(_GRADE_DISPLAY_ORDER)}


def _grade_sort_key(grade):
    """Recognized grades first in canonical order; unknown names keep their
    incoming (name-ordered) relative position at the end."""
    rank = _GRADE_RANK.get(_normalize_grade_name(grade.name or ''))
    return (0, rank) if rank is not None else (1, 0)


def _active_grades(school, year):
    """Grades this school's public form may offer — the SINGLE source of truth
    for both the rendered dropdown and the server-side POST validation.

    Always scoped to THIS school + THIS active year, so another tenant's grade
    id can never be accepted. When the school has educational stages configured
    (managed mode) the list is additionally narrowed to those stages; a school
    with ``educational_stages`` NULL/blank (legacy) is not filtered at all.

    A stored value that is non-empty but invalid raises
    ``InvalidStageConfiguration`` — it must never fall back to "show every
    grade", which would silently turn filtering off on a public surface.
    """
    grades = (Grade.query.execution_options(bypass_tenant_scope=True,
                                            include_all_years=True)
              .filter_by(school_id=school.id, academic_year_id=year.id)
              .order_by(Grade.name).all())

    stages = school_stages(school)
    if stages:
        grades = [g for g in grades if grade_matches_stages(g, stages)]

    # Display-order only — same rows, same ids, nothing added or removed.
    return sorted(grades, key=_grade_sort_key)


def _content_matches_ext(file_storage, ext: str) -> bool:
    sigs = _MAGIC.get(ext)
    if not sigs:
        return False
    try:
        pos = file_storage.stream.tell()
        head = file_storage.stream.read(16)
        file_storage.stream.seek(pos)
    except Exception:
        return False
    return any(head.startswith(s) for s in sigs)


def _ext_of(filename: str) -> str:
    return filename.rsplit('.', 1)[1].lower() if '.' in (filename or '') else ''


def _stream_size(file_storage) -> int | None:
    """Byte size measured from the stream itself (never the client's header);
    the position is restored. None when the stream cannot be read."""
    try:
        stream = file_storage.stream
        pos = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(pos)
        return size
    except Exception:
        return None


def _prepare_photo(file_storage):
    """Validate the public student photo BEFORE any Storage write.

    Returns ``(upload, None)`` or ``(None, message)``. Inspection only: the SAME
    FileStorage (rewound) is returned, so the stored original keeps its exact
    bytes, extension and metadata — it becomes Student.photo, the AI Face source,
    on approval. Checks: JPG/JPEG/PNG extension, <= 5 MB, magic bytes matching
    the extension, then the Student photo validator (real Pillow decode/verify,
    40 MP ceiling, corrupt/truncated refused).
    """
    ext = _ext_of(file_storage.filename)
    if ext not in _PHOTO_EXTS:
        return None, _MSG_PHOTO_TYPE
    size = _stream_size(file_storage)
    if not size:
        return None, _MSG_PHOTO_INVALID
    if size > _MAX_UPLOAD_BYTES:
        return None, _MSG_PHOTO_SIZE
    if not _content_matches_ext(file_storage, ext):
        return None, _MSG_PHOTO_INVALID
    err = validate_student_photo(file_storage)
    if err:
        return None, (err if err == _MSG_PHOTO_DIMENSIONS else _MSG_PHOTO_INVALID)
    return file_storage, None


def _prepare_document(file_storage):
    """Validate and prepare ONE registration document BEFORE any Storage write,
    with the Student Document policy: PDF/JPG/JPEG/PNG, <= 5 MB, magic bytes;
    a PDF is returned unchanged (stored byte-for-byte), an image is decoded and
    optimised (<= 1600 px, WebP q88, metadata stripped) and only that result is
    stored. Returns ``(upload, None)`` or ``(None, message)``."""
    upload, err = prepare_student_document_upload(file_storage)
    if err:
        return None, err
    # The prepared bytes must fit the per-file cap Storage enforces, so a (rare)
    # larger re-encode is refused here rather than failing mid-upload.
    size = _stream_size(upload)
    if size is None or size > _MAX_UPLOAD_BYTES:
        return None, _MSG_DOC_SIZE
    return upload, None


def _discard_uploads(stored_values):
    """Remove objects THIS request stored when the submission is not recorded.
    Each value is deleted only if no database row references it."""
    for value in stored_values:
        _discard_unreferenced_upload(value)


# ── Routes ──────────────────────────────────────────────────────────────────────

@registration_bp.route('/register/<token>', methods=['GET', 'POST'])
@limiter.limit('30 per hour; 8 per minute', key_func=_ip_token_key,
               methods=['POST'])
def form(token):
    school = _resolve_school(token)
    if school is None:
        return _generic_unavailable()

    year = _active_year(school)
    if year is None:
        # Cannot register without an active academic year — same generic page.
        return _generic_unavailable()

    try:
        grades = _active_grades(school, year)
    except InvalidStageConfiguration:
        # Fail closed: a corrupt stage configuration must not expose grades the
        # school no longer offers. Same generic page as every other failure.
        return _generic_unavailable()

    form_cfg = get_student_form_config(school.id)
    enabled_features = get_enabled_features(school.id)
    # Same school-scoped, active-only residential areas the internal Add Student
    # form uses. Empty list → the selector is not rendered (identical fallback).
    residential_areas = _school_residential_areas(school.id, active_only=True)

    def _render(values, error=None, nonce=None):
        if error:
            flash(error, 'danger')
        return render_template(
            'registration/form.html',
            school=school, grades=grades, form_cfg=form_cfg,
            enabled_features=enabled_features, token=token,
            residential_areas=residential_areas,
            sf=values or {},
            submission_nonce=nonce or secrets.token_urlsafe(16),
        )

    if request.method == 'GET':
        return _render({}, nonce=secrets.token_urlsafe(16))

    # ── POST ────────────────────────────────────────────────────────────────
    # Reject any unexpected / forbidden field (tampering) BEFORE processing.
    submitted_keys = set(request.form.keys())
    if submitted_keys & _FORBIDDEN_KEYS:
        return _render(request.form, 'تعذّر إرسال الطلب. يرجى استخدام النموذج كما هو.')
    if not submitted_keys.issubset(_ALLOWED_FORM_KEYS):
        return _render(request.form, 'تعذّر إرسال الطلب. يرجى استخدام النموذج كما هو.')
    if set(request.files.keys()) - _ALLOWED_FILE_KEYS:
        return _render(request.form, 'تعذّر إرسال الطلب. يرجى استخدام النموذج كما هو.')

    nonce = (request.form.get('submission_nonce') or '').strip()[:64] or None

    # Grade must belong to THIS school's active year AND — in managed mode — to
    # one of its selected stages. Validated server-side against the same list
    # that was rendered: a grade id from another tenant, another year, or an
    # unselected stage matches nothing and is rejected.
    grade_id = request.form.get('desired_grade_id', type=int)
    grade = next((g for g in grades if g.id == grade_id), None)
    if grade is None:
        return _render(request.form, 'يرجى اختيار الصف الدراسي.', nonce=nonce)

    full_name = normalize_name(request.form.get('full_name'))
    if not full_name:
        return _render(request.form, 'الاسم الكامل مطلوب.', nonce=nonce)

    # School-configured required fields (allow-listed subset only).
    cfg_errors = form_cfg.validate_public(request.form)
    if cfg_errors:
        return _render(request.form, cfg_errors[0], nonce=nonce)

    gender = (request.form.get('gender') or '').strip() or None
    if gender and gender not in ('male', 'female'):
        return _render(request.form, 'قيمة الجنس غير صالحة.', nonce=nonce)

    # Length caps (reject rather than silently truncate): the submitted value
    # and the stored (normalized) value, which must fit its database column.
    for key in _LENGTH_CHECKED_FIELDS:
        value = request.form.get(key) or ''
        if (len(value) > _MAX_TEXT_LEN
                or len(normalize_text(value)) > _COLUMN_MAX[key]):
            return _render(request.form, _MSG_TOO_LONG, nonce=nonce)

    dob = None
    dob_str = (request.form.get('date_of_birth') or '').strip()
    if dob_str:
        try:
            dob = datetime.strptime(dob_str, '%Y-%m-%d').date()
        except ValueError:
            return _render(request.form, 'تاريخ الميلاد غير صالح.', nonce=nonce)

    # Residential area (optional) — server-side validated against THIS school's
    # active areas. A foreign-school / inactive / injected id fails closed to None
    # (reuses the same validator as the internal Add Student form).
    residential_area_id_val = None
    _raw_area_id = request.form.get('residential_area_id', type=int)
    if _raw_area_id:
        residential_area_id_val = _validate_residential_area_for_school(
            _raw_area_id, school.id)
        if residential_area_id_val is None:
            return _render(request.form,
                           'يرجى اختيار منطقة سكن صالحة لهذه المدرسة.', nonce=nonce)

    # ── Idempotency: same rendered form (same nonce) must not duplicate ────────
    if nonce:
        existing = (StudentRegistrationRequest.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter_by(school_id=school.id, submission_nonce=nonce)
                    .first())
        if existing is not None:
            # Re-submission of the same form — reuse the original tracking token
            # is impossible (hash-only), so show a safe "already received" page.
            return render_template('registration/received.html', school=school), 200

    # ── Uploads, step 1: validate + prepare EVERY file, no Storage write yet ──
    # A submission that fails any deterministic check stores nothing at all.
    photo_file = request.files.get('photo')
    want_photo = bool(photo_file and photo_file.filename
                      and form_cfg.public_section_visible('student_photo')
                      and 'students.photo_upload' in enabled_features)

    doc_rows = []  # (document_type, FileStorage)
    if (form_cfg.public_section_visible('student_documents')
            and 'students.documents_upload' in enabled_features):
        doc_types = request.form.getlist('document_type[]')
        doc_files = request.files.getlist('document_file[]')
        if len(doc_files) > _MAX_DOCS:
            return _render(request.form, 'عدد المستندات كبير جداً.', nonce=nonce)
        for doc_type, doc_file in zip(doc_types, doc_files):
            if not (doc_file and doc_file.filename):
                continue
            doc_type = normalize_text(doc_type) or 'وثيقة'
            if len(doc_type) > _DOC_TYPE_MAX:
                return _render(request.form, _MSG_TOO_LONG, nonce=nonce)
            doc_rows.append((doc_type, doc_file))

    photo_upload = None
    if want_photo:
        photo_upload, err = _prepare_photo(photo_file)
        if err:
            return _render(request.form, err, nonce=nonce)

    doc_uploads = []  # (document_type, prepared upload) — processed one at a time
    for doc_type, doc_file in doc_rows:
        upload, err = _prepare_document(doc_file)
        if err:
            return _render(request.form, err, nonce=nonce)
        doc_uploads.append((doc_type, upload))

    # ── Uploads, step 2: store (private bucket, server filenames, v2 prefix) ──
    media_bucket = current_app.config.get('SUPABASE_STORAGE_BUCKET_MEDIA')
    stored = []  # every object written by this request
    photo_path = None
    if photo_upload is not None:
        photo_path = save_uploaded_file(
            photo_upload, registration_photo_subfolder(school.id),
            bucket=media_bucket, allowed_exts=_PHOTO_EXTS,
            max_size=_MAX_UPLOAD_BYTES)
        if photo_path is None:
            return _render(request.form, 'تعذّر رفع الصورة.', nonce=nonce)
        stored.append(photo_path)

    doc_saves = []  # (document_type, file_path) collected before DB write
    for doc_type, upload in doc_uploads:
        saved = save_uploaded_file(
            upload, registration_document_subfolder(school.id),
            bucket=media_bucket, allowed_exts=STUDENT_DOC_STORED_EXTS,
            max_size=_MAX_UPLOAD_BYTES)
        if saved is None:
            _discard_uploads(stored)
            return _render(request.form, 'تعذّر رفع أحد الملفات.', nonce=nonce)
        stored.append(saved)
        doc_saves.append((doc_type, saved))

    # ── Create the request (+ documents + staff notification) atomically ──────
    raw_tracking = generate_token()
    req = StudentRegistrationRequest(
        school_id=school.id,
        academic_year_id=year.id,
        desired_grade_id=grade.id,
        full_name=full_name,
        date_of_birth=dob,
        gender=gender,
        nationality=normalize_text(request.form.get('nationality')) or None,
        address=normalize_text(request.form.get('address')) or None,
        phone=normalize_text(request.form.get('phone')) or None,
        notes=normalize_text(request.form.get('notes')) or None,
        residential_area_id=residential_area_id_val,
        student_photo_path=photo_path,
        guardian_name=normalize_name(request.form.get('guardian_name')) or None,
        guardian_phone=normalize_text(request.form.get('guardian_phone')) or None,
        guardian_email=normalize_text(request.form.get('guardian_email')) or None,
        guardian_relation=normalize_text(request.form.get('guardian_relation')) or None,
        status='pending',
        tracking_token_hash=hash_token(raw_tracking),
        submission_nonce=nonce,
        submission_ip=request.remote_addr,
    )
    db.session.add(req)
    try:
        db.session.flush()
    except IntegrityError:
        # Concurrent duplicate on (school_id, submission_nonce) — safe idempotent
        # outcome: the other request already recorded it. This request's copies
        # were never referenced (nothing was committed), so they are removed.
        db.session.rollback()
        _discard_uploads(stored)
        return render_template('registration/received.html', school=school), 200

    for doc_type, file_path in doc_saves:
        db.session.add(StudentRegistrationRequestDocument(
            request_id=req.id, school_id=school.id,
            document_type=doc_type, file_path=file_path))

    # Staff notification — same school only, NO student/guardian PII in payload.
    db.session.add(Notification(
        school_id=school.id,
        title='طلب تسجيل خارجي جديد',
        body='تم استلام طلب تسجيل طالب جديد عبر رابط التسجيل. يرجى مراجعته.',
        ntype='registration_request',
        target_role='school_admin',
    ))

    db.session.commit()
    return redirect(url_for('registration.track', tracking_token=raw_tracking))


@registration_bp.route('/register/track/<tracking_token>')
@limiter.limit('40 per hour; 12 per minute', key_func=_ip_token_key)
def track(tracking_token):
    h = hash_token(tracking_token or '')
    if not h:
        return _generic_unavailable()
    req = (StudentRegistrationRequest.query
           .execution_options(bypass_tenant_scope=True)
           .filter_by(tracking_token_hash=h).first())
    if req is None:
        return _generic_unavailable()
    school = School.query.get(req.school_id)
    # Expose ONLY status + parent-facing rejection reason — never internal notes,
    # ids, credentials, or other requests.
    return render_template('registration/track.html', req=req, school=school)

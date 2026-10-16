"""
"أرسل درسي" — institute ADMINISTRATION review of parent study requests.

GET   /admin/institute-study-requests                 list (light, paginated)
GET   /admin/institute-study-requests/<id>            detail + images
POST  /admin/institute-study-requests/<id>            mark viewed / admin note

Access (every route): logged in → manage_leave_requests → never a teacher
(even if the permission were granted) → a SELECTED school (a Super Admin with
no selected institute is refused, never shown all institutes) → that school is
an institute (403 otherwise, like institute_groups). Every query is pinned to
that school's id; a request id from another school is a 404.

Database pressure:
  * the list is ONE query over requests ⨝ students ⨝ parent user, selecting
    only small columns (the note as an 80-char SQL substring), limit+1 paging
    with no COUNT, plus ONE batched current-group query for the page's
    students. It never touches the image table and builds no image URLs —
    InstituteStudyRequest.image_count is the stored number;
  * the detail page is the only place image rows are read and protected URLs
    are built (upload_url → protected_upload_url, the P1 access rules);
  * saving is one narrow SELECT + at most one UPDATE + one commit, and NO
    write at all when the request is already viewed and the note is unchanged.

Academic years: like the leave-request administration, the inbox spans the
institute's academic years (include_all_years=True) — a request is an
actionable item, not year data — while always filtered on the selected
school_id (the school tenant criteria stay active too).
"""
import logging
from datetime import datetime

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import and_, case, func
from sqlalchemy.orm import aliased

from app.models import (db, InstituteGroupEnrollment, InstituteStudyGroup,
                        InstituteStudyRequest, InstituteStudyRequestImage, Student, User)
from app.utils.decorators import (get_active_year, get_current_school,
                                  historical_guard, permission_required)
from app.utils.scoping import current_academic_year_id
from app.utils.student_removal import student_not_deleted_clause

from . import admin_bp

PERMISSION      = 'manage_leave_requests'
MAX_ADMIN_NOTE  = 1000
NOTE_PREVIEW    = 80
PER_PAGE        = 25
MAX_PER_PAGE    = 50
STATUS_FILTERS  = ('pending', 'viewed', 'all')

STATUS_LABELS = {
    InstituteStudyRequest.STATUS_PENDING: 'جديد',
    InstituteStudyRequest.STATUS_VIEWED:  'تم الاطلاع',
}

_ALL_YEARS = {'include_all_years': True}
R = InstituteStudyRequest


# ─── Guard ────────────────────────────────────────────────────────────────────

def _institute_or_response():
    """(school, None) for an authorised administrator, else (None, response).
    Fail closed: teacher → 403, no selected school → redirect, school-type
    institution → 403."""
    if current_user.role and current_user.role.name == 'teacher':
        abort(403)
    school = get_current_school()
    if school is None:
        flash('يرجى اختيار مؤسسة أولاً.', 'warning')
        return None, redirect(url_for('admin.dashboard'))
    if not school.is_institute:
        abort(403)
    return school, None


def _current_groups(school_id, student_ids) -> dict:
    """{student_id: [group name, …]} for the CURRENT memberships of the given
    students — ONE query for the whole page. Same rule as
    student_current_enrollments(): active enrollment, active group, this
    institute, its active academic year."""
    student_ids = {sid for sid in student_ids if sid}
    year_id = current_academic_year_id()
    if year_id is None:
        year = get_active_year(school_id)
        year_id = year.id if year else None
    if not student_ids or not year_id:
        return {}
    rows = (db.session.query(InstituteGroupEnrollment.student_id, InstituteStudyGroup.name)
            .join(InstituteStudyGroup,
                  InstituteStudyGroup.id == InstituteGroupEnrollment.group_id)
            .filter(InstituteGroupEnrollment.school_id == school_id,
                    InstituteGroupEnrollment.student_id.in_(student_ids),
                    InstituteGroupEnrollment.status
                    == InstituteGroupEnrollment.STATUS_ACTIVE,
                    InstituteStudyGroup.school_id == school_id,
                    InstituteStudyGroup.academic_year_id == year_id,
                    InstituteStudyGroup.is_active.is_(True))
            .execution_options(bypass_tenant_scope=True)
            .order_by(InstituteStudyGroup.name)
            .all())
    groups = {}
    for student_id, name in rows:
        groups.setdefault(student_id, []).append(name)
    return groups


def _int_arg(name, default, lo, hi):
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(value, hi))


def _request_with_people(school_id, request_id):
    """(request, student name, student code, student id, parent name, viewer
    name) for one request of THIS school — one query — or None."""
    viewer = aliased(User)
    return (db.session.query(R, Student.full_name, Student.student_id, Student.id,
                             User.full_name, viewer.full_name)
            .execution_options(**_ALL_YEARS)
            .join(Student, and_(Student.id == R.student_id,
                                Student.school_id == R.school_id))
            .outerjoin(User, User.id == R.parent_id)
            .outerjoin(viewer, viewer.id == R.viewed_by)
            .filter(R.id == request_id, R.school_id == school_id,
                    student_not_deleted_clause())
            .first())


# ─── List ─────────────────────────────────────────────────────────────────────

@admin_bp.route('/institute-study-requests')
@login_required
@permission_required(PERMISSION)
def institute_study_requests_list():
    school, denied = _institute_or_response()
    if denied:
        return denied

    status = request.args.get('status', 'pending')
    if status not in STATUS_FILTERS:
        status = 'pending'                       # never an arbitrary filter
    page = _int_arg('page', 1, 1, 100000)
    per_page = _int_arg('per_page', PER_PAGE, 1, MAX_PER_PAGE)

    q = (db.session.query(R.id, R.status, R.image_count, R.created_at,
                          func.substr(R.note, 1, NOTE_PREVIEW + 1).label('note_head'),
                          Student.id.label('student_pk'),
                          Student.full_name.label('student_name'),
                          Student.student_id.label('student_code'),
                          User.full_name.label('parent_name'))
         .execution_options(**_ALL_YEARS)
         .join(Student, and_(Student.id == R.student_id,
                             Student.school_id == R.school_id))
         .outerjoin(User, User.id == R.parent_id)
         .filter(R.school_id == school.id, student_not_deleted_clause()))
    if status == 'all':
        q = q.order_by(case((R.status == R.STATUS_PENDING, 0), else_=1),
                       R.created_at.desc(), R.id.desc())
    else:
        q = q.filter(R.status == status).order_by(R.created_at.desc(), R.id.desc())
    rows = q.offset((page - 1) * per_page).limit(per_page + 1).all()
    has_next = len(rows) > per_page
    rows = rows[:per_page]

    return render_template(
        'admin/institute_study_requests_list.html',
        rows=rows, groups=_current_groups(school.id, (r.student_pk for r in rows)),
        status=status, page=page, per_page=per_page, has_next=has_next,
        note_preview=NOTE_PREVIEW, status_labels=STATUS_LABELS)


# ─── Detail + save ────────────────────────────────────────────────────────────

@admin_bp.route('/institute-study-requests/<int:request_id>', methods=['GET', 'POST'])
@login_required
@permission_required(PERMISSION)
@historical_guard
def institute_study_request_detail(request_id):
    school, denied = _institute_or_response()
    if denied:
        return denied
    if request.method == 'POST':
        return _save(school, request_id)

    found = _request_with_people(school.id, request_id)
    if found is None:
        abort(404)
    req, student_name, student_code, student_pk, parent_name, viewer_name = found
    images = (db.session.query(InstituteStudyRequestImage.id,
                               InstituteStudyRequestImage.file_path,
                               InstituteStudyRequestImage.sort_order)
              .filter(InstituteStudyRequestImage.request_id == req.id,
                      InstituteStudyRequestImage.school_id == school.id)
              .order_by(InstituteStudyRequestImage.sort_order,
                        InstituteStudyRequestImage.id)
              .all())
    return render_template(
        'admin/institute_study_request_detail.html',
        req=req, images=images, student_name=student_name, student_code=student_code,
        parent_name=parent_name, viewer_name=viewer_name,
        groups=_current_groups(school.id, [student_pk]).get(student_pk, []),
        status_labels=STATUS_LABELS, max_note=MAX_ADMIN_NOTE)


def _save(school, request_id):
    detail_url = url_for('admin.institute_study_request_detail', request_id=request_id)
    note = (request.form.get('admin_note') or '').strip() or None
    if note is not None and len(note) > MAX_ADMIN_NOTE:
        flash(f'ملاحظة الإدارة يجب ألا تتجاوز {MAX_ADMIN_NOTE} حرف.', 'danger')
        return redirect(detail_url)

    # Narrow lookup: only what the decision needs, pinned to this school.
    current = (db.session.query(R.status, R.admin_note)
               .execution_options(**_ALL_YEARS)
               .join(Student, and_(Student.id == R.student_id,
                                   Student.school_id == R.school_id))
               .filter(R.id == request_id, R.school_id == school.id,
                       student_not_deleted_clause())
               .first())
    if current is None:
        abort(404)
    if current.status == R.STATUS_VIEWED and current.admin_note == note:
        flash('تم الحفظ.', 'success')            # nothing changed → no write
        return redirect(detail_url)

    now = datetime.utcnow()
    try:
        # One atomic UPDATE. The first viewer and time are kept (COALESCE);
        # later saves only edit the note. Only status / note / viewer fields
        # change — the student, parent, school and year links the central
        # write guard validates are untouched, and viewed_by is the current,
        # route-authorised administrator of this school (or the Super Admin
        # working inside it).
        updated = (db.session.query(R)
                   .filter(R.id == request_id, R.school_id == school.id)
                   .update({R.status: R.STATUS_VIEWED,
                            R.admin_note: note,
                            R.viewed_by: func.coalesce(R.viewed_by, current_user.id),
                            R.viewed_at: func.coalesce(R.viewed_at, now),
                            R.updated_at: now},
                           synchronize_session=False))
        if updated == 1:
            db.session.commit()
        else:
            db.session.rollback()
    except Exception:
        db.session.rollback()
        logging.getLogger(__name__).exception(
            '[study-request] admin save failed request_id=%s', request_id)
        flash('تعذر حفظ التغييرات. يرجى المحاولة مرة أخرى.', 'danger')
        return redirect(detail_url)
    if updated != 1:                              # deleted by the parent meanwhile
        abort(404)

    flash('تم الحفظ.', 'success')
    return redirect(detail_url)

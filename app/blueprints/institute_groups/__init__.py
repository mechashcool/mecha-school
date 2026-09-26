"""
Mecha-School — Institute Study Groups Blueprint  (institutes only)

Usable ONLY when the current institution has School.is_institute == True.
A school-type institution can never reach any route here: every one of them
runs through _require_institute(), which 403s for a non-institute. This mirrors
the buildings blueprint, which is gated the same way on School.enable_buildings.

Institutes organise teaching as  subject -> study group -> instructor.
Grade, Section, Student.section_id and teacher_subjects are NOT used, read or
modified by anything in this module.

Routes
------
GET       /institute-groups/                        list groups
GET/POST  /institute-groups/new                     create group
GET/POST  /institute-groups/<id>/edit               edit group
POST      /institute-groups/<id>/toggle-active      activate / deactivate
GET       /institute-groups/<id>                    detail + roster
POST      /institute-groups/<id>/enroll             bulk-add existing students
POST      /institute-groups/<id>/enrollments/<eid>/end   end ONE active enrollment
GET       /institute-groups/attendance/report       read-only attendance report
GET       /institute-groups/attendance/report/export-pdf   same report as PDF

Isolation
---------
Every posted id (group, subject, instructor, student, enrollment) is re-loaded
with an explicit school_id equality filter before it is used. On top of that,
the database itself rejects a cross-school link through the composite foreign
keys on both tables, so a route bug cannot produce mixed-school data.
"""
from datetime import datetime, timedelta
from functools import wraps

from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, abort)
from flask_login import login_required, current_user

from app.models import (db, AcademicYear, Employee, Grade,
                        InstituteGroupEnrollment, InstituteStudyGroup,
                        Student, Subject)
from app.utils.audit import log_action
from app.utils.decorators import (permission_required, get_current_school,
                                  get_active_year, historical_guard)
from app.utils.institute_groups import (active_enrollment_count_map,
                                        active_roster, instructor_groups)
from app.services import institute_attendance as att
from app.utils.school_stages import ALL_STAGES, STAGE_LABELS

institute_groups_bp = Blueprint('institute_groups', __name__,
                                template_folder='../../templates/institute_groups')


# ─── Guards and loaders ───────────────────────────────────────────────────────

def _require_institute():
    """Return (school, active_year) for an institute, or (None, None).

    Fail-closed: a school-type institution, or a super admin with no active
    school selected, gets nothing. The caller redirects.
    """
    school = get_current_school()
    if not school:
        flash('يرجى اختيار مؤسسة أولاً.', 'warning')
        return None, None
    if not getattr(school, 'is_institute', False):
        # Not a flash+redirect: for a school-type institution this surface does
        # not exist at all, and saying so would confirm the route name.
        abort(403)
    year = get_active_year(school.id)
    if not year:
        flash('لا يوجد عام دراسي نشط لهذه المؤسسة.', 'warning')
        return school, None
    return school, year


def _is_group_manager() -> bool:
    """True for a user who may ADMINISTER institute groups.

    Exactly the existing permission — no new permission or role is introduced.
    Admin tiers short-circuit through User.has_permission() as everywhere else.
    A teacher is deliberately NOT a manager even if the permission were ever
    granted to their role, mirroring homework._is_admin().
    """
    if current_user.role and current_user.role.name == 'teacher':
        return False
    return current_user.has_permission('manage_institute_groups')


def _is_instructor() -> bool:
    """True for the existing school-wide teacher role."""
    return bool(current_user.role and current_user.role.name == 'teacher')


def group_read_access_required(f):
    """Allow managers (manage_institute_groups) AND teachers (read-only).

    Mirrors homework_access_required: one decorator for the read surface, while
    every mutating route below keeps permission_required('manage_institute_groups')
    untouched. Teachers therefore gain no write capability anywhere.
    """
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.role:
            abort(403)
        if not (_is_group_manager() or _is_instructor()):
            abort(403)
        return f(*args, **kwargs)
    return wrapper


def _get_group_or_404(group_id, school):
    """Load one group by id AND school_id. Never by id alone."""
    group = (InstituteStudyGroup.query
             .execution_options(bypass_tenant_scope=True)
             .filter_by(id=group_id, school_id=school.id)
             .first())
    if group is None:
        abort(404)
    return group


# Bucket key for a grade that carries no stage. Kept as the empty string so it
# round-trips through the form as a normal (falsy) value, matching how
# sections/subjects.html already groups unclassified rows.
_NO_STAGE = ''
_NO_STAGE_LABEL = 'بدون مرحلة'


def _stage_of(grade):
    """Canonical stage token for a grade, or _NO_STAGE when it has none."""
    return (getattr(grade, 'stage', None) or '').strip() or _NO_STAGE


def _institute_subjects(school, year):
    """Subjects of THIS institute in THIS academic year, ordered by name.

    Kept as the single source of the subject set; the taxonomy helper below
    splits it into grade-classified and legacy rows.
    """
    return (Subject.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school.id, academic_year_id=year.id)
            .order_by(Subject.name)
            .all())


def _institute_grades(school, year):
    """Grades of THIS institute in THIS academic year, ordered by name."""
    return (Grade.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school.id, academic_year_id=year.id)
            .order_by(Grade.name)
            .all())


def _form_taxonomy(school, year, current_subject=None):
    """Build the stage -> grade -> subject data the form cascade needs.

    Everything is resolved server-side from rows that already belong to THIS
    institute and THIS academic year, so the browser only ever filters data it
    was authorized to see. No AJAX route is introduced.

    Returns a dict with:
      stages         [{value, label}]   only stages that actually have a grade
                                        carrying at least one subject
      grades         [{id, name, stage}] only grades that have >= 1 subject
      subjects       [{id, name, code, grade_id}] grade-classified subjects only
      legacy_subject {id, name, code} | None
                     the group's CURRENT subject when it predates grade
                     classification (grade_id IS NULL). It stays selectable so
                     an existing group can be saved unchanged, but it is never
                     offered as a new choice.

    Subjects are never merged or de-duplicated by name: two rows named
    الرياضيات under different grades are different records and both appear,
    each under its own grade.
    """
    all_subjects = _institute_subjects(school, year)
    grades_by_id = {g.id: g for g in _institute_grades(school, year)}

    # Grade-classified subjects only. A subject whose grade_id points outside
    # this institute/year is dropped defensively rather than shown.
    classified = [s for s in all_subjects
                  if s.grade_id is not None and s.grade_id in grades_by_id]

    used_grade_ids = {s.grade_id for s in classified}
    used_grades = [grades_by_id[gid] for gid in used_grade_ids]

    # Stage list derived from the grades that actually carry subjects, so a
    # stage with no usable grade is never offered.
    present = {_stage_of(g) for g in used_grades}
    ordered = [s for s in ALL_STAGES if s in present]
    ordered += sorted(s for s in present if s and s not in ALL_STAGES)
    if _NO_STAGE in present:
        ordered.append(_NO_STAGE)

    stages = [{'value': s,
               'label': STAGE_LABELS.get(s, s) if s else _NO_STAGE_LABEL}
              for s in ordered]

    legacy_subject = None
    if current_subject is not None and current_subject.grade_id is None:
        legacy_subject = {'id': current_subject.id,
                          'name': current_subject.name,
                          'code': current_subject.code}

    return {
        'stages': stages,
        'grades': sorted(
            ({'id': g.id, 'name': g.name, 'stage': _stage_of(g)}
             for g in used_grades),
            key=lambda g: g['name']),
        'subjects': [{'id': s.id, 'name': s.name, 'code': s.code,
                      'grade_id': s.grade_id} for s in classified],
        'legacy_subject': legacy_subject,
    }


def _institute_instructors(school):
    """Employees of THIS institute that may be assigned as instructors.

    Reuses the existing Employee entity and the existing active-status
    convention used elsewhere (Employee.status == 'active'); no separate
    instructor table and no new status vocabulary. A linked user account is
    deliberately NOT required — storing the assignment is a staffing fact, and
    no current business rule requires an account for it.
    """
    return (Employee.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school.id, status='active')
            .order_by(Employee.full_name)
            .all())


def _parse_date(raw):
    """Return (date|None, ok). ok is False only when a non-empty value is malformed."""
    raw = (raw or '').strip()
    if not raw:
        return None, True
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date(), True
    except ValueError:
        return None, False


def _validate_group_form(school, year, *, exclude_group_id=None,
                         current_subject_id=None):
    """Validate the posted group form against THIS institute and year.

    Returns (data_dict, errors_list). Every id is verified to belong to the
    authenticated institute; nothing posted is trusted.

    `current_subject_id` is the subject the group ALREADY has (edit only). It is
    the single exception that lets a legacy subject with no grade_id be saved
    unchanged, so existing rows never become uneditable.

    stage and grade are UI/filtering inputs only. They are re-checked here
    against the subject so a forged combination is rejected, but neither is
    stored on the group: subject_id remains the only persisted link.
    """
    errors = []

    name = (request.form.get('name') or '').strip()
    if not name:
        errors.append('اسم المجموعة مطلوب.')
    elif len(name) > 150:
        errors.append('اسم المجموعة طويل جداً (الحد الأقصى 150 حرفاً).')

    is_active = request.form.get('is_active') == '1'

    # ── Stage → grade → subject ────────────────────────────────────────
    # The browser cascade is a convenience, never the authority. Each level is
    # re-resolved here against this institute and this academic year, and the
    # chain subject → grade → stage is re-checked, so a hand-crafted POST that
    # pairs a valid subject with someone else's grade or a mismatched stage is
    # rejected. Nothing is written until every check below has passed.
    posted_grade_id = request.form.get('grade_id', type=int) or None
    posted_stage    = (request.form.get('stage') or '').strip()

    subject_id = request.form.get('subject_id', type=int)
    subject = None
    if not subject_id:
        errors.append('المادة مطلوبة.')
    else:
        subject = (Subject.query
                   .execution_options(bypass_tenant_scope=True)
                   .filter_by(id=subject_id, school_id=school.id,
                              academic_year_id=year.id)
                   .first())
        if not subject:
            # Covers another school, another academic year and a non-existent
            # id alike, without revealing which one it was.
            errors.append('المادة المحددة غير صالحة أو لا تنتمي إلى هذه المؤسسة '
                          'أو إلى العام الدراسي المحدد.')
            subject_id = None

    if subject is not None:
        if subject.grade_id is None:
            # Legacy/unclassified subject. Allowed ONLY when it is the subject
            # this group already has, so existing data keeps working; it is
            # never selectable as a new choice.
            if current_subject_id is None or subject.id != current_subject_id:
                errors.append('المادة المحددة غير مرتبطة بصف دراسي. '
                              'اختر المرحلة ثم الصف ثم المادة.')
                subject_id = None
            elif posted_grade_id:
                errors.append('المادة الحالية غير مرتبطة بصف دراسي، '
                              'ولا يمكن ربطها بصف من هذا النموذج.')
                subject_id = None
        else:
            # The subject's own grade is the authority; a posted grade_id must
            # agree with it rather than replace it.
            grade = (Grade.query
                     .execution_options(bypass_tenant_scope=True)
                     .filter_by(id=subject.grade_id, school_id=school.id,
                                academic_year_id=year.id)
                     .first())
            if not grade:
                errors.append('الصف المرتبط بالمادة غير صالح لهذه المؤسسة '
                              'في العام الدراسي المحدد.')
                subject_id = None
            else:
                if posted_grade_id and posted_grade_id != grade.id:
                    errors.append('المادة المحددة لا تنتمي إلى الصف المختار.')
                    subject_id = None
                if posted_stage and posted_stage != _stage_of(grade):
                    errors.append('الصف المختار لا ينتمي إلى المرحلة المختارة.')
                    subject_id = None

    # Instructor — must be an employee of this institute.
    instructor_id = request.form.get('instructor_id', type=int) or None
    if instructor_id:
        instructor = (Employee.query
                      .execution_options(bypass_tenant_scope=True)
                      .filter_by(id=instructor_id, school_id=school.id)
                      .first())
        if not instructor:
            errors.append('المدرّس المحدد غير صالح أو لا ينتمي إلى هذه المؤسسة.')
            instructor_id = None
    # An ACTIVE group must have an instructor. An inactive (archived) group may
    # be left without one, which is also the state an employee deletion leaves
    # behind via ON DELETE SET NULL (instructor_id).
    if is_active and not instructor_id:
        errors.append('المجموعة المفعّلة يجب أن يكون لها مدرّس.')

    start_date, start_ok = _parse_date(request.form.get('start_date'))
    if not start_ok:
        errors.append('صيغة تاريخ البداية غير صحيحة.')
    end_date, end_ok = _parse_date(request.form.get('end_date'))
    if not end_ok:
        errors.append('صيغة تاريخ النهاية غير صحيحة.')
    if start_date and end_date and end_date < start_date:
        errors.append('تاريخ النهاية يجب ألا يكون قبل تاريخ البداية.')

    # Duplicate identity: school + year + subject + name (mirrors the DB
    # constraint so the operator gets an Arabic message instead of a 500).
    if name and subject_id:
        dup_q = (InstituteStudyGroup.query
                 .execution_options(bypass_tenant_scope=True)
                 .filter_by(school_id=school.id, academic_year_id=year.id,
                            subject_id=subject_id, name=name))
        if exclude_group_id:
            dup_q = dup_q.filter(InstituteStudyGroup.id != exclude_group_id)
        if dup_q.first():
            errors.append('توجد مجموعة بنفس الاسم لنفس المادة في هذا العام الدراسي.')

    return {
        'name': name,
        'subject_id': subject_id,
        'instructor_id': instructor_id,
        'start_date': start_date,
        'end_date': end_date,
        'is_active': is_active,
    }, errors


# ─── List ─────────────────────────────────────────────────────────────────────

@institute_groups_bp.route('/')
@group_read_access_required
def index():
    school, year = _require_institute()
    if not school:
        return redirect(url_for('admin.dashboard'))

    is_manager = _is_group_manager()
    if not year:
        return render_template('institute_groups/index.html',
                               groups=[], active_counts={}, year=None,
                               is_manager=is_manager)

    if is_manager:
        # Unchanged administrator view: every group of the institute, active
        # and inactive, with the existing management controls.
        groups = (InstituteStudyGroup.query
                  .execution_options(bypass_tenant_scope=True)
                  .filter_by(school_id=school.id, academic_year_id=year.id)
                  .order_by(InstituteStudyGroup.is_active.desc(),
                            InstituteStudyGroup.name)
                  .all())
    else:
        # Instructor view — ONLY the ACTIVE groups assigned to this instructor.
        # An unlinked account resolves to [], never to the full list.
        groups = instructor_groups(school, current_user, year)

    active_counts = active_enrollment_count_map(school, [g.id for g in groups])

    return render_template('institute_groups/index.html',
                           groups=groups, active_counts=active_counts, year=year,
                           is_manager=is_manager)


# ─── Create ───────────────────────────────────────────────────────────────────

@institute_groups_bp.route('/new', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_institute_groups')
def new():
    school, year = _require_institute()
    if not school:
        return redirect(url_for('admin.dashboard'))
    if not year:
        return redirect(url_for('institute_groups.index'))

    taxonomy    = _form_taxonomy(school, year)
    instructors = _institute_instructors(school)

    if request.method == 'POST':
        data, errors = _validate_group_form(school, year)
        if errors:
            for e in errors:
                flash(e, 'danger')
            # request.form is passed straight back so the operator's stage,
            # grade and subject choices survive the error.
            return render_template('institute_groups/form.html', group=None,
                                   taxonomy=taxonomy, instructors=instructors,
                                   form=request.form), 400

        group = InstituteStudyGroup(school_id=school.id,
                                    academic_year_id=year.id, **data)
        db.session.add(group)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        log_action('create', 'institute_study_group', group.id,
                   details=f'created study group "{group.name}"')
        flash('تم إنشاء المجموعة الدراسية.', 'success')
        return redirect(url_for('institute_groups.detail', group_id=group.id))

    return render_template('institute_groups/form.html', group=None,
                           taxonomy=taxonomy, instructors=instructors, form=None)


# ─── Edit ─────────────────────────────────────────────────────────────────────

@institute_groups_bp.route('/<int:group_id>/edit', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_institute_groups')
def edit(group_id):
    school, year = _require_institute()
    if not school:
        return redirect(url_for('admin.dashboard'))
    if not year:
        return redirect(url_for('institute_groups.index'))

    group = _get_group_or_404(group_id, school)

    # A group is edited inside ITS OWN academic year, not the newest one. For a
    # historical group the active year would offer a different year's grades and
    # subjects and would reject the group's own subject, making the row
    # uneditable; it would also run the duplicate check against the wrong year.
    # The stored academic_year_id is never rewritten by this form.
    group_year = (AcademicYear.query
                  .execution_options(bypass_tenant_scope=True)
                  .filter_by(id=group.academic_year_id, school_id=school.id)
                  .first()) or year

    current_subject = (Subject.query
                       .execution_options(bypass_tenant_scope=True)
                       .filter_by(id=group.subject_id, school_id=school.id)
                       .first())

    taxonomy    = _form_taxonomy(school, group_year, current_subject=current_subject)
    instructors = _institute_instructors(school)

    if request.method == 'POST':
        data, errors = _validate_group_form(
            school, group_year, exclude_group_id=group.id,
            current_subject_id=group.subject_id)
        if errors:
            for e in errors:
                flash(e, 'danger')
            # Nothing has been assigned to the group yet, so a failed POST
            # leaves it completely untouched. request.form is echoed back so the
            # submitted stage/grade/subject survive.
            return render_template('institute_groups/form.html', group=group,
                                   taxonomy=taxonomy, instructors=instructors,
                                   form=request.form), 400

        for field, value in data.items():
            setattr(group, field, value)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        log_action('update', 'institute_study_group', group.id,
                   details=f'updated study group "{group.name}"')
        flash('تم تحديث المجموعة الدراسية.', 'success')
        return redirect(url_for('institute_groups.detail', group_id=group.id))

    return render_template('institute_groups/form.html', group=group,
                           taxonomy=taxonomy, instructors=instructors, form=None)


# ─── Activate / deactivate ────────────────────────────────────────────────────

@institute_groups_bp.route('/<int:group_id>/toggle-active', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_institute_groups')
def toggle_active(group_id):
    """Flip is_active. Deliberately NOT a delete.

    Deactivating leaves every enrollment exactly as it is — no row is ended and
    none is removed — so the group's history stays intact and it can be
    reactivated with its roster unchanged.
    """
    school, _year = _require_institute()
    if not school:
        return redirect(url_for('admin.dashboard'))

    group = _get_group_or_404(group_id, school)

    # Reactivating requires an instructor, same rule as an active group on save.
    if not group.is_active and not group.instructor_id:
        flash('لا يمكن تفعيل المجموعة بدون مدرّس. عدّل المجموعة وعيّن مدرّساً أولاً.',
              'danger')
        return redirect(url_for('institute_groups.detail', group_id=group.id))

    group.is_active = not group.is_active
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    log_action('update', 'institute_study_group', group.id,
               details=f'{"activated" if group.is_active else "deactivated"} '
                       f'study group "{group.name}"')
    flash('تم تفعيل المجموعة.' if group.is_active else 'تم تعطيل المجموعة.',
          'success')
    return redirect(url_for('institute_groups.detail', group_id=group.id))


# ─── Detail + roster ──────────────────────────────────────────────────────────

@institute_groups_bp.route('/<int:group_id>')
@group_read_access_required
def detail(group_id):
    school, year = _require_institute()
    if not school:
        return redirect(url_for('admin.dashboard'))

    group = _get_group_or_404(group_id, school)
    is_manager = _is_group_manager()

    if not is_manager:
        # INSTRUCTOR — read-only, and only for a group assigned to them.
        # Any other group (another instructor's, an inactive one, or one from
        # another institute) is a plain 404: the same response the caller would
        # get for a non-existent id, so nothing is disclosed either way.
        if group.id not in {g.id for g in instructor_groups(school, current_user, year)}:
            abort(404)

        # Roster = ACTIVE enrollments only. Ended rows are not shown to the
        # instructor and are not touched; they remain stored for the admin view.
        roster = active_roster(school, group.id)
        return render_template('institute_groups/detail.html',
                               group=group, year=year,
                               active_rows=[e for e, _s in roster],
                               ended_rows=[], candidates=[],
                               is_manager=False)

    # Unchanged administrator view below.
    enrollments = (InstituteGroupEnrollment.query
                   .execution_options(bypass_tenant_scope=True)
                   .filter_by(school_id=school.id, group_id=group.id)
                   .order_by(InstituteGroupEnrollment.enrolled_at.desc())
                   .all())
    active_rows = [e for e in enrollments
                   if e.status == InstituteGroupEnrollment.STATUS_ACTIVE]
    ended_rows  = [e for e in enrollments
                   if e.status != InstituteGroupEnrollment.STATUS_ACTIVE]

    # Candidates for bulk enrollment: active students of THIS institute that do
    # not already hold an active enrollment in this group.
    enrolled_ids = {e.student_id for e in active_rows}
    candidates = (Student.query
                  .execution_options(bypass_tenant_scope=True)
                  .filter_by(school_id=school.id, status='active')
                  .order_by(Student.full_name)
                  .all())
    candidates = [s for s in candidates if s.id not in enrolled_ids]

    return render_template('institute_groups/detail.html',
                           group=group, year=year,
                           active_rows=active_rows, ended_rows=ended_rows,
                           candidates=candidates, is_manager=True)


# ─── Bulk enroll ──────────────────────────────────────────────────────────────

@institute_groups_bp.route('/<int:group_id>/enroll', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_institute_groups')
def enroll(group_id):
    """Add several existing students to one group in a single transaction.

    Every posted student id is validated BEFORE any write: it must be an active
    student of this institute. One invalid or cross-school id rejects the whole
    submission and writes nothing. Students who already hold an active
    enrollment are skipped silently rather than duplicated.
    """
    school, _year = _require_institute()
    if not school:
        return redirect(url_for('admin.dashboard'))

    group = _get_group_or_404(group_id, school)
    if not group.is_active:
        flash('لا يمكن إضافة طلاب إلى مجموعة معطّلة. فعّل المجموعة أولاً.', 'danger')
        return redirect(url_for('institute_groups.detail', group_id=group.id))

    raw_ids = request.form.getlist('student_ids')
    posted  = []
    for raw in raw_ids:
        try:
            posted.append(int(raw))
        except (TypeError, ValueError):
            flash('قائمة الطلاب المرسلة غير صالحة.', 'danger')
            return redirect(url_for('institute_groups.detail', group_id=group.id))
    posted = list(dict.fromkeys(posted))  # de-duplicate, keep order

    if not posted:
        flash('لم يتم اختيار أي طالب.', 'warning')
        return redirect(url_for('institute_groups.detail', group_id=group.id))

    # Validate every id against this institute BEFORE writing anything.
    valid_ids = {
        s.id for s in Student.query
        .execution_options(bypass_tenant_scope=True)
        .filter(Student.school_id == school.id,
                Student.status == 'active',
                Student.id.in_(posted))
        .all()
    }
    unknown = [sid for sid in posted if sid not in valid_ids]
    if unknown:
        # Do not reveal whether the id exists in another school.
        flash('بعض الطلاب المحددين غير صالحين لهذه المؤسسة. لم يتم حفظ أي تغيير.',
              'danger')
        return redirect(url_for('institute_groups.detail', group_id=group.id))

    already = {
        e.student_id for e in InstituteGroupEnrollment.query
        .execution_options(bypass_tenant_scope=True)
        .filter(InstituteGroupEnrollment.school_id == school.id,
                InstituteGroupEnrollment.group_id == group.id,
                InstituteGroupEnrollment.status
                == InstituteGroupEnrollment.STATUS_ACTIVE,
                InstituteGroupEnrollment.student_id.in_(posted))
        .all()
    }
    to_add = [sid for sid in posted if sid not in already]

    if not to_add:
        flash('جميع الطلاب المحددين مسجّلون في هذه المجموعة بالفعل.', 'info')
        return redirect(url_for('institute_groups.detail', group_id=group.id))

    now = datetime.utcnow()
    for sid in to_add:
        db.session.add(InstituteGroupEnrollment(
            school_id=school.id, group_id=group.id, student_id=sid,
            enrolled_at=now, ended_at=None,
            status=InstituteGroupEnrollment.STATUS_ACTIVE,
        ))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash('تعذر حفظ الاشتراكات. لم يتم حفظ أي تغيير.', 'danger')
        return redirect(url_for('institute_groups.detail', group_id=group.id))

    log_action('create', 'institute_group_enrollment', group.id,
               details=f'enrolled {len(to_add)} student(s) into study group '
                       f'"{group.name}"')
    flash(f'تم تسجيل {len(to_add)} طالب/طالبة في المجموعة.', 'success')
    return redirect(url_for('institute_groups.detail', group_id=group.id))


# ─── End one enrollment ───────────────────────────────────────────────────────

@institute_groups_bp.route('/<int:group_id>/enrollments/<int:enrollment_id>/end',
                           methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_institute_groups')
def end_enrollment(group_id, enrollment_id):
    """End ONE active enrollment. The row is kept forever, never deleted."""
    school, _year = _require_institute()
    if not school:
        return redirect(url_for('admin.dashboard'))

    group = _get_group_or_404(group_id, school)

    enrollment = (InstituteGroupEnrollment.query
                  .execution_options(bypass_tenant_scope=True)
                  .filter_by(id=enrollment_id, school_id=school.id,
                             group_id=group.id)
                  .first())
    if enrollment is None:
        abort(404)

    if enrollment.status != InstituteGroupEnrollment.STATUS_ACTIVE:
        flash('هذا الاشتراك منتهٍ بالفعل.', 'info')
        return redirect(url_for('institute_groups.detail', group_id=group.id))

    enrollment.status   = InstituteGroupEnrollment.STATUS_ENDED
    enrollment.ended_at = datetime.utcnow()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    log_action('update', 'institute_group_enrollment', enrollment.id,
               details=f'ended enrollment of student {enrollment.student_id} '
                       f'in study group "{group.name}"')
    flash('تم إنهاء اشتراك الطالب مع الاحتفاظ بالسجل التاريخي.', 'success')
    return redirect(url_for('institute_groups.detail', group_id=group.id))


# ═════════════════════════════════════════════════════════════════════════════
#  WEEKLY SCHEDULES AND MANUAL ATTENDANCE
# ═════════════════════════════════════════════════════════════════════════════
#
# Every read below is bounded by _require_institute() + _get_group_or_404(),
# i.e. the institution must be an institute and the group must belong to THIS
# school — a group id from another institute 404s before any query touches it.
#
# Instructors reach these surfaces through _scoped_group(), which additionally
# requires the group to be assigned to their own Employee row. Managers keep
# the existing manage_institute_groups permission. No new permission or role is
# introduced anywhere in this feature.
#
# All writes go through app/services/institute_attendance.py — the one domain
# service the mobile API also calls — so neither surface re-implements the
# rules, and a future card/device source plugs into the same path.


def _scoped_group(group_id, school, year):
    """A group this account may act on, or 404.

    Manager -> any group of this institute. Instructor -> only groups assigned
    to their own Employee row, resolved through the existing Employee.user_id
    link. 404 rather than 403 so another instructor's group is indistinguishable
    from a nonexistent one.
    """
    group = _get_group_or_404(group_id, school)
    if _is_group_manager():
        return group
    allowed = {g.id for g in instructor_groups(school, current_user, year,
                                               active_only=False)}
    if group.id not in allowed:
        abort(404)
    return group


def _parse_date_arg(raw, fallback):
    if not raw:
        return fallback
    try:
        return datetime.strptime(str(raw).strip(), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return fallback


def _parse_time_arg(raw):
    if not raw:
        return None
    for fmt in ('%H:%M', '%H:%M:%S'):
        try:
            return datetime.strptime(str(raw).strip(), fmt).time()
        except ValueError:
            continue
    return None


# ── Weekly schedule management (managers only) ──────────────────────────────

@institute_groups_bp.route('/<int:group_id>/schedule', methods=['GET'])
@permission_required('manage_institute_groups')
def schedule(group_id):
    school, year = _require_institute()
    if not school or not year:
        return redirect(url_for('institute_groups.index'))
    group = _get_group_or_404(group_id, school)
    slots = att.group_slots(school.id, group.id, active_only=False)
    today = att.local_today(school)
    # A two-week preview so the manager can see the rules actually resolving
    # into dates. Computed only — nothing is created by viewing this page.
    preview = att.occurrences_for_range(school, [group], today,
                                        today + timedelta(days=13))
    return render_template('institute_groups/schedule.html',
                           group=group, slots=slots, preview=preview,
                           day_names=att.DAY_NAMES_AR, today=today)


@institute_groups_bp.route('/<int:group_id>/schedule/add', methods=['POST'])
@historical_guard
@permission_required('manage_institute_groups')
def schedule_add(group_id):
    school, year = _require_institute()
    if not school or not year:
        return redirect(url_for('institute_groups.index'))
    group = _get_group_or_404(group_id, school)
    try:
        slot = att.add_slot(school, group,
                            request.form.get('day_of_week'),
                            request.form.get('start_time'),
                            request.form.get('end_time'))
    except att.AttendanceError as exc:
        flash(str(exc), 'danger')
    else:
        log_action('institute_schedule_add',
                   f'مجموعة {group.name}: إضافة موعد '
                   f'{att.day_name(slot.day_of_week)} '
                   f'{slot.start_time.strftime("%H:%M")}')
        flash('تمت إضافة الموعد الأسبوعي.', 'success')
    return redirect(url_for('institute_groups.schedule', group_id=group.id))


@institute_groups_bp.route('/<int:group_id>/schedule/<int:slot_id>/edit',
                           methods=['POST'])
@historical_guard
@permission_required('manage_institute_groups')
def schedule_edit(group_id, slot_id):
    school, year = _require_institute()
    if not school or not year:
        return redirect(url_for('institute_groups.index'))
    group = _get_group_or_404(group_id, school)
    slot = next((x for x in att.group_slots(school.id, group.id,
                                            active_only=False)
                 if x.id == slot_id), None)
    if slot is None:
        abort(404)
    try:
        # Editing a rule changes FUTURE computed occurrences only; a stored
        # session keeps its own date/time snapshot and is never touched.
        att.update_slot(slot, request.form.get('day_of_week'),
                        request.form.get('start_time'),
                        request.form.get('end_time'),
                        is_active=request.form.get('is_active') == '1')
    except att.AttendanceError as exc:
        flash(str(exc), 'danger')
    else:
        log_action('institute_schedule_edit',
                   f'مجموعة {group.name}: تعديل موعد #{slot.id}')
        flash('تم تحديث الموعد. لا يؤثر التعديل على الجلسات المسجّلة سابقاً.',
              'success')
    return redirect(url_for('institute_groups.schedule', group_id=group.id))


@institute_groups_bp.route('/<int:group_id>/schedule/<int:slot_id>/delete',
                           methods=['POST'])
@historical_guard
@permission_required('manage_institute_groups')
def schedule_delete(group_id, slot_id):
    school, year = _require_institute()
    if not school or not year:
        return redirect(url_for('institute_groups.index'))
    group = _get_group_or_404(group_id, school)
    slot = next((x for x in att.group_slots(school.id, group.id,
                                            active_only=False)
                 if x.id == slot_id), None)
    if slot is None:
        abort(404)
    att.delete_slot(slot)
    log_action('institute_schedule_delete',
               f'مجموعة {group.name}: حذف موعد #{slot_id}')
    flash('تم حذف الموعد. السجلات التاريخية للحضور محفوظة ولم تتأثر.',
          'success')
    return redirect(url_for('institute_groups.schedule', group_id=group.id))


# ── Scheduled sessions by date (managers + assigned instructors) ────────────

@institute_groups_bp.route('/attendance', methods=['GET'])
@group_read_access_required
def attendance_sessions():
    school, year = _require_institute()
    if not school or not year:
        return redirect(url_for('institute_groups.index'))

    if _is_group_manager():
        groups = (InstituteStudyGroup.query
                  .execution_options(bypass_tenant_scope=True)
                  .filter_by(school_id=school.id, academic_year_id=year.id)
                  .order_by(InstituteStudyGroup.name).all())
    else:
        groups = instructor_groups(school, current_user, year)

    today = att.local_today(school)
    start = _parse_date_arg(request.args.get('start'), today)
    end = _parse_date_arg(request.args.get('end'), start + timedelta(days=6))
    if end < start:
        end = start

    group_filter = request.args.get('group_id', type=int)
    if group_filter and group_filter not in {g.id for g in groups}:
        group_filter = None          # a forged id simply drops out of scope
    shown = [g for g in groups if not group_filter or g.id == group_filter]

    try:
        occurrences = att.occurrences_for_range(school, shown, start, end)
    except att.AttendanceError as exc:
        flash(str(exc), 'danger')
        occurrences = []

    summary = att.attendance_summary(
        school, [o.session.id for o in occurrences if o.session])

    return render_template('institute_groups/attendance_sessions.html',
                           groups=groups, occurrences=occurrences,
                           summary=summary, start=start, end=end,
                           group_filter=group_filter, today=today,
                           is_manager=_is_group_manager(),
                           day_names=att.DAY_NAMES_AR)


# ── Attendance report (managers + assigned instructors, read-only) ───────────

@institute_groups_bp.route('/attendance/report', methods=['GET'])
@group_read_access_required
def attendance_report():
    school, year = _require_institute()
    if not school or not year:
        return redirect(url_for('institute_groups.index'))

    # The SAME group scope as attendance_sessions: a manager sees every group
    # of this institute and year, an instructor only their own active groups.
    is_manager = _is_group_manager()
    if is_manager:
        groups = (InstituteStudyGroup.query
                  .execution_options(bypass_tenant_scope=True)
                  .filter_by(school_id=school.id, academic_year_id=year.id)
                  .order_by(InstituteStudyGroup.name).all())
    else:
        groups = instructor_groups(school, current_user, year)

    # Date range: the school attendance report's convention (first day of the
    # month -> today), evaluated in the institute's own timezone.
    today = att.local_today(school)
    start = _parse_date_arg(request.args.get('start'), today.replace(day=1))
    end = _parse_date_arg(request.args.get('end'), today)
    if end < start:
        end = start

    # A group outside this account's scope — another institute's, another
    # instructor's, or a nonexistent id — is a plain 404, never a silent
    # widening to "all groups".
    group_filter = request.args.get('group_id', type=int)
    if group_filter and group_filter not in {g.id for g in groups}:
        abort(404)
    shown = [g for g in groups if not group_filter or g.id == group_filter]

    q = (request.args.get('q') or '').strip()[:att.REPORT_MAX_NAME_QUERY]
    return {'is_manager': is_manager, 'groups': groups, 'shown': shown,
            'group_filter': group_filter,
            'selected_group': next((g for g in groups
                                    if g.id == group_filter), None),
            'q': q, 'start': start, 'end': end, 'today': today}


def _report_filter_args(scope):
    """The effective filters as query arguments — what the PDF link carries."""
    args = {'start': scope['start'].strftime('%Y-%m-%d'),
            'end': scope['end'].strftime('%Y-%m-%d')}
    if scope['group_filter']:
        args['group_id'] = scope['group_filter']
    if scope['q']:
        args['q'] = scope['q']
    return args


@institute_groups_bp.route('/attendance/report', methods=['GET'])
@group_read_access_required
def attendance_report():
    school, year = _require_institute()
    if not school or not year:
        return redirect(url_for('institute_groups.index'))
    scope = _attendance_report_scope(school, year)
    groups, group_filter, q = scope['groups'], scope['group_filter'], scope['q']
    start, end, is_manager = scope['start'], scope['end'], scope['is_manager']

    try:
        report = att.attendance_report(school, scope['shown'], start, end,
                                       name_query=q, today=scope['today'])
    except att.AttendanceError as exc:
        flash(str(exc), 'danger')
        report = att.attendance_report(school, [], start, end)

    return render_template('institute_groups/attendance_report.html',
                           groups=groups, group_filter=group_filter,
                           selected_group=scope['selected_group'],
                           export_args=_report_filter_args(scope),
                           q=q, start=start, end=end, report=report,
                           row_limit=REPORT_ROW_LIMIT,
                           statuses=att.InstituteAttendanceRecord.STATUSES,
                           status_labels=att.STATUS_LABELS_AR,
                           unrecorded=att.REPORT_UNRECORDED,
                           unrecorded_label=att.REPORT_UNRECORDED_LABEL_AR,
                           day_names=att.DAY_NAMES_AR,
                           local_dt=att.local_formatter(school),
                           is_manager=is_manager)


# The detail table renders at most this many rows; the summary always counts
# every matching row.
REPORT_ROW_LIMIT = 2000


@institute_groups_bp.route('/attendance/report/export-pdf', methods=['GET'])
@group_read_access_required
def attendance_report_export_pdf():
    """The CURRENT filtered report as a PDF. Same scope, same filters and the
    same attendance_report() result as the page — nothing is recomputed."""
    from flask import make_response
    from app.utils.institute_attendance_pdf import (
        generate_institute_attendance_report_pdf)

    school, year = _require_institute()
    if not school or not year:
        return redirect(url_for('institute_groups.index'))
    scope = _attendance_report_scope(school, year)
    back = url_for('institute_groups.attendance_report',
                   **_report_filter_args(scope))

    try:
        report = att.attendance_report(school, scope['shown'], scope['start'],
                                       scope['end'], name_query=scope['q'],
                                       today=scope['today'])
    except att.AttendanceError as exc:
        flash(str(exc), 'danger')
        return redirect(back)

    group = scope['selected_group']
    if group is None:
        group_label = 'كل المجموعات'
    else:
        group_label = group.name + (f' — {group.subject.name}'
                                    if group.subject else '')
    student_label = ''
    if scope['q']:
        student_label = (report['student_names'][0] if report['students'] == 1
                         else f'«{scope["q"]}» ({report["students"]} طالب)')

    start_s = scope['start'].strftime('%Y-%m-%d')
    end_s = scope['end'].strftime('%Y-%m-%d')
    pdf_bytes = generate_institute_attendance_report_pdf(
        report, school=school, date_from=start_s, date_to=end_s,
        group_label=group_label, student_label=student_label,
        status_labels=att.STATUS_LABELS_AR,
        unrecorded=att.REPORT_UNRECORDED,
        unrecorded_label=att.REPORT_UNRECORDED_LABEL_AR,
        day_names=att.DAY_NAMES_AR, local_dt=att.local_formatter(school))
    if not pdf_bytes:
        flash('تعذّر إنشاء ملف PDF — تأكد من تثبيت مكتبة ReportLab وتوفر الخط العربي.',
              'danger')
        return redirect(back)

    resp = make_response(pdf_bytes)
    resp.headers['Content-Type'] = 'application/pdf'
    resp.headers['Content-Disposition'] = (
        f'attachment; filename="institute_attendance_report_{start_s}_{end_s}.pdf"')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


# ── Take / correct attendance for one occurrence ────────────────────────────

@institute_groups_bp.route('/<int:group_id>/attendance/<date_str>',
                           methods=['GET', 'POST'])
@historical_guard
@group_read_access_required
def attendance_take(group_id, date_str):
    school, year = _require_institute()
    if not school or not year:
        return redirect(url_for('institute_groups.index'))
    group = _scoped_group(group_id, school, year)

    on_date = _parse_date_arg(date_str, None)
    start_time = _parse_time_arg(request.values.get('start'))
    if on_date is None or start_time is None:
        abort(404)

    # The (date, start) tuple must correspond to a REAL occurrence — an active
    # weekly rule or an already materialized session. An arbitrary date cannot
    # be invented through the URL.
    occ = att.find_occurrence(school, group, on_date, start_time)
    if occ is None:
        abort(404)

    # Materializing does NOT record anything: the row is 'not_recorded' until
    # somebody submits. Opening the page can never create an absence.
    session = att.get_or_create_session(school, group, occ)
    roster = att.session_roster(school, session)

    if request.method == 'POST':
        statuses = {}
        for stu, _rec in roster:
            chosen = request.form.get(f'status_{stu.id}', '').strip()
            if chosen:
                statuses[stu.id] = chosen
        source = (att.InstituteAttendanceSession.SOURCE_MANUAL_ADMIN
                  if _is_group_manager()
                  else att.InstituteAttendanceSession.SOURCE_MANUAL_INSTRUCTOR)
        try:
            result = att.submit_attendance(
                school, session, statuses,
                source=source, actor_user_id=current_user.id)
        except att.AttendanceError as exc:
            flash(str(exc), 'danger')
        else:
            log_action('institute_attendance_submit',
                       f'مجموعة {group.name} — {session.session_date}: '
                       f'{result["created"]} جديد، {result["updated"]} تعديل')
            flash(f'تم حفظ الحضور. سجلات جديدة: {result["created"]}، '
                  f'تعديلات: {result["updated"]}.', 'success')
            return redirect(url_for('institute_groups.attendance_take',
                                    group_id=group.id,
                                    date_str=on_date.strftime('%Y-%m-%d'),
                                    start=start_time.strftime('%H:%M')))
        roster = att.session_roster(school, session)

    return render_template('institute_groups/attendance_take.html',
                           group=group, session=session, roster=roster,
                           statuses=att.InstituteAttendanceRecord.STATUSES,
                           status_labels=att.STATUS_LABELS_AR,
                           day_names=att.DAY_NAMES_AR,
                           # recorded_at is stored as naive UTC; convert to the
                           # school's local wall-clock only for display.
                           local_dt=att.local_formatter(school),
                           is_manager=_is_group_manager())

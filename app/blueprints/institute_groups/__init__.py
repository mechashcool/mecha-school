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

Isolation
---------
Every posted id (group, subject, instructor, student, enrollment) is re-loaded
with an explicit school_id equality filter before it is used. On top of that,
the database itself rejects a cross-school link through the composite foreign
keys on both tables, so a route bug cannot produce mixed-school data.
"""
from datetime import datetime

from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, abort)
from flask_login import login_required

from app.models import (db, AcademicYear, Employee, Grade,
                        InstituteGroupEnrollment, InstituteStudyGroup,
                        Student, Subject)
from app.utils.audit import log_action
from app.utils.decorators import (permission_required, get_current_school,
                                  get_active_year, historical_guard)
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
@login_required
@permission_required('manage_institute_groups')
def index():
    school, year = _require_institute()
    if not school:
        return redirect(url_for('admin.dashboard'))
    if not year:
        return render_template('institute_groups/index.html',
                               groups=[], active_counts={}, year=None)

    groups = (InstituteStudyGroup.query
              .execution_options(bypass_tenant_scope=True)
              .filter_by(school_id=school.id, academic_year_id=year.id)
              .order_by(InstituteStudyGroup.is_active.desc(),
                        InstituteStudyGroup.name)
              .all())

    # Active-member count per group, in one query rather than N.
    active_counts = {}
    if groups:
        rows = (db.session.query(InstituteGroupEnrollment.group_id,
                                 db.func.count(InstituteGroupEnrollment.id))
                .filter(InstituteGroupEnrollment.school_id == school.id,
                        InstituteGroupEnrollment.group_id.in_([g.id for g in groups]),
                        InstituteGroupEnrollment.status
                        == InstituteGroupEnrollment.STATUS_ACTIVE)
                .group_by(InstituteGroupEnrollment.group_id)
                .all())
        active_counts = {gid: cnt for gid, cnt in rows}

    return render_template('institute_groups/index.html',
                           groups=groups, active_counts=active_counts, year=year)


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
@login_required
@permission_required('manage_institute_groups')
def detail(group_id):
    school, year = _require_institute()
    if not school:
        return redirect(url_for('admin.dashboard'))

    group = _get_group_or_404(group_id, school)

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
                           candidates=candidates)


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

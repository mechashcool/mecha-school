"""
Evaluations blueprint.

Two faces:
  • /evaluations/            → Teacher Performance Evaluation (computed from
                               students' exam grades; read-only analytics).
                               One row per (teacher, subject, section) — never
                               aggregated to teacher level only, because one
                               teacher may teach several subjects/sections.
  • /evaluations/manual      → the original manual EmployeeEvaluation records
                               (create/edit/delete kept intact).

Teacher attribution comes ONLY from the `teacher_subjects` junction
(employee_id, subject_id, section_id) — never from ExamResult.entered_by,
which records the data-entry user, not the teacher.
"""
from decimal import Decimal

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for)
from flask_login import current_user, login_required
from sqlalchemy import and_, case, distinct, func

from app.models import (db, Employee, EmployeeEvaluation, Exam, ExamResult,
                        Grade, School, Section, Subject, teacher_subjects)
from app.utils.decorators import (get_active_year, get_current_school,
                                   permission_required, get_view_year, historical_guard)


evaluations_bp = Blueprint(
    'evaluations',
    __name__,
    template_folder='../../templates/evaluations',
)


def _is_super_admin():
    return bool(current_user.is_authenticated and current_user.is_super_admin)


def _schools_for_selector():
    if not _is_super_admin():
        return []
    return (School.query
            .filter_by(is_active=True)
            .order_by(School.school_name)
            .all())


def _selected_school():
    """Return the school selected for this evaluation operation."""
    if _is_super_admin():
        school_id = request.values.get('school_id', type=int)
        if school_id:
            return School.query.get_or_404(school_id)
        return get_current_school()
    return get_current_school()


def _scoped(query):
    """Super admin may inspect any school (incl. one that differs from the
    session's switched school), so the ORM auto-scope must be bypassed and
    the explicit school/year filters in each query take over — the same
    convention the manual-evaluation queries below already use."""
    if _is_super_admin():
        return query.execution_options(bypass_tenant_scope=True,
                                       include_all_years=True)
    return query


def _employee_query_for_school(school):
    if not school:
        return None

    query = Employee.query
    if _is_super_admin():
        query = query.execution_options(bypass_tenant_scope=True)

    return (query
            .filter(Employee.school_id == school.id,
                    Employee.status == 'active')
            .order_by(Employee.full_name))


def _employees_for_school(school):
    query = _employee_query_for_school(school)
    return query.all() if query is not None else []


def _load_employee_for_school(employee_id, school):
    query = _employee_query_for_school(school)
    if query is None or not employee_id:
        return None
    return query.filter(Employee.id == employee_id).first()


def _evaluation_query():
    query = EmployeeEvaluation.query
    if _is_super_admin():
        query = query.execution_options(
            bypass_tenant_scope=True,
            include_all_years=True,
        )
    return query


def _redirect_to_manual(school_id=None):
    if _is_super_admin() and school_id:
        return redirect(url_for('evaluations.manual', school_id=school_id))
    return redirect(url_for('evaluations.manual'))


def _score(name, default):
    value = request.form.get(name, default, type=int)
    return max(1, min(10, value or default))


# ─────────────────────────────────────────────────────────────────────────────
#  TEACHER PERFORMANCE  (computed from exam results)
# ─────────────────────────────────────────────────────────────────────────────

#: final score = weighted blend of the three computed signals (all 0–100).
_W_AVG, _W_PASS, _W_ACTIVITY = 0.45, 0.45, 0.10

#: exam_activity_score targets 4+ exams per assignment for a full score:
#: 1 exam = 25, 2 = 50, 3 = 75, 4+ = 100.
_ACTIVITY_TARGET = 4


def _activity_score(exam_count):
    if exam_count >= _ACTIVITY_TARGET:
        return 100.0
    return exam_count / _ACTIVITY_TARGET * 100


def _pass_case():
    """SQL CASE marking a result as passed.

    A result passes when marks >= exam.pass_marks; if pass_marks is NULL the
    fallback is marks >= 50% of max_marks (written as marks*2 >= max_marks to
    avoid division).
    """
    return case(
        (and_(Exam.pass_marks.isnot(None),
              ExamResult.marks >= Exam.pass_marks), 1),
        (and_(Exam.pass_marks.is_(None),
              ExamResult.marks * 2 >= Exam.max_marks), 1),
        else_=0,
    )

_RATING_BANDS = (
    (85, 'ممتاز',        'badge-active'),
    (75, 'جيد جداً',     'badge-approved'),
    (60, 'جيد',          'badge-paid'),
    (0,  'يحتاج متابعة', 'badge-pending'),
)


def _rating(final):
    for threshold, label, css in _RATING_BANDS:
        if final >= threshold:
            return label, css
    return _RATING_BANDS[-1][1], _RATING_BANDS[-1][2]


def _performance_data(school, year):
    """Aggregate exam-grade metrics per (teacher, subject, section).

    Returns (rows, pending, unattributed_exam_count) where:
      rows        — assignments that have at least one exam result (scored)
      pending     — assignments with no usable results (no exams, or exams
                    without entered marks)
      unattributed— number of exams whose (subject, section) pair has no
                    teacher_subjects assignment at all
    """
    ts = teacher_subjects

    agg_q = (
        db.session.query(
            ts.c.employee_id.label('employee_id'),
            ts.c.subject_id.label('subject_id'),
            ts.c.section_id.label('section_id'),
            func.count(distinct(Exam.id)).label('exam_count'),
            func.count(ExamResult.id).label('result_count'),
            func.count(distinct(ExamResult.student_id)).label('student_count'),
            func.coalesce(func.sum(ExamResult.marks), 0).label('marks_sum'),
            # max_marks summed once per RESULT row → correct denominator for avg %
            func.coalesce(
                func.sum(case((ExamResult.id.isnot(None), Exam.max_marks))), 0
            ).label('max_sum'),
            # pass computed against the exam's own pass mark (with 50%-of-max
            # fallback when pass_marks is NULL) — ExamResult.is_pass is
            # nullable, so it is not trusted here
            func.coalesce(func.sum(_pass_case()), 0).label('pass_count'),
        )
        .select_from(ts)
        .join(Employee, Employee.id == ts.c.employee_id)
        .join(Section,  Section.id  == ts.c.section_id)
        .join(Subject,  Subject.id  == ts.c.subject_id)
        # LEFT joins so assignments with zero exams / zero results still appear
        .outerjoin(Exam, and_(Exam.subject_id == ts.c.subject_id,
                              Exam.section_id == ts.c.section_id,
                              Exam.school_id == school.id,
                              Exam.academic_year_id == year.id))
        .outerjoin(ExamResult, ExamResult.exam_id == Exam.id)
        # Section is a year-scoped entity → these two filters pin the whole
        # assignment row to this school + academic year
        .filter(Section.school_id == school.id,
                Section.academic_year_id == year.id,
                Employee.school_id == school.id,
                Employee.status == 'active')
        .group_by(ts.c.employee_id, ts.c.subject_id, ts.c.section_id)
    )
    agg = _scoped(agg_q).all()

    emp_ids = {r.employee_id for r in agg}
    sub_ids = {r.subject_id for r in agg}
    sec_ids = {r.section_id for r in agg}

    employees = {}
    if emp_ids:
        emp_q = Employee.query.filter(Employee.id.in_(emp_ids),
                                      Employee.school_id == school.id)
        employees = {e.id: e for e in _scoped(emp_q).all()}

    subjects = {}
    if sub_ids:
        sub_q = Subject.query.filter(Subject.id.in_(sub_ids),
                                     Subject.school_id == school.id)
        subjects = {s.id: s for s in _scoped(sub_q).all()}

    sections = {}
    if sec_ids:
        sec_q = (db.session.query(Section.id, Section.name,
                                  Grade.name.label('grade_name'))
                 .outerjoin(Grade, Grade.id == Section.grade_id)
                 .filter(Section.id.in_(sec_ids),
                         Section.school_id == school.id))
        sections = {row.id: (row.name, row.grade_name)
                    for row in _scoped(sec_q).all()}

    rows, pending = [], []
    for r in agg:
        emp = employees.get(r.employee_id)
        if emp is None:
            continue  # stale junction row pointing at a removed/inactive employee
        subject = subjects.get(r.subject_id)
        sec_name, grade_name = sections.get(r.section_id, ('—', None))

        item = {
            'employee_id':  r.employee_id,
            'subject_id':   r.subject_id,
            'section_id':   r.section_id,
            'teacher_name': emp.full_name,
            'job_title':    emp.job_title,
            'subject_name': subject.name if subject else '—',
            'grade_name':   grade_name or '—',
            'section_name': sec_name,
            'exam_count':   r.exam_count,
            'result_count': r.result_count,
            'student_count': r.student_count,
        }

        if r.result_count:
            max_sum = float(r.max_sum)
            avg_pct  = round((float(r.marks_sum) / max_sum * 100) if max_sum else 0.0, 1)
            pass_pct = round(r.pass_count / r.result_count * 100, 1)
            activity = round(_activity_score(r.exam_count), 1)
            final    = round(_W_AVG * avg_pct + _W_PASS * pass_pct
                             + _W_ACTIVITY * activity, 1)
            label, css = _rating(final)
            item.update(avg_pct=avg_pct,
                        pass_pct=pass_pct,
                        activity=activity,
                        final=final,
                        rating_label=label,
                        rating_css=css)
            rows.append(item)
        else:
            pending.append(item)

    rows.sort(key=lambda x: x['final'], reverse=True)
    pending.sort(key=lambda x: (x['teacher_name'], x['subject_name']))

    # Exams whose (subject, section) has no teacher assignment at all —
    # surfaced so managers notice incomplete teacher_subjects data.
    unattr_q = (
        db.session.query(func.count(distinct(Exam.id)))
        .outerjoin(ts, and_(ts.c.subject_id == Exam.subject_id,
                            ts.c.section_id == Exam.section_id))
        .filter(Exam.school_id == school.id,
                Exam.academic_year_id == year.id,
                ts.c.employee_id.is_(None))
    )
    unattributed = _scoped(unattr_q).scalar() or 0

    return rows, pending, unattributed


@evaluations_bp.route('/')
@login_required
@permission_required('manage_employees')
def index():
    """Teacher Performance Evaluation — the new landing page."""
    school = _selected_school()
    year = get_view_year(school.id) if school else None

    rows, pending, unattributed = [], [], 0
    if school and year:
        rows, pending, unattributed = _performance_data(school, year)

    return render_template(
        'evaluations/index.html',
        rows=rows,
        pending=pending,
        unattributed=unattributed,
        view_year=year,
        schools=_schools_for_selector(),
        selected_school_id=school.id if school else None,
        is_super_admin=_is_super_admin(),
    )


@evaluations_bp.route('/performance/<int:employee_id>/<int:subject_id>/<int:section_id>')
@login_required
@permission_required('manage_employees')
def performance_detail(employee_id, subject_id, section_id):
    """Per-exam breakdown for one (teacher, subject, section) assignment."""
    school = _selected_school()
    year = get_view_year(school.id) if school else None
    if not school or not year:
        flash('اختر مدرسة ذات عام دراسي نشط أولاً.', 'warning')
        return redirect(url_for('evaluations.index'))

    # The assignment must actually exist in teacher_subjects.
    ts = teacher_subjects
    assignment = _scoped(
        db.session.query(ts.c.employee_id)
        .filter(ts.c.employee_id == employee_id,
                ts.c.subject_id == subject_id,
                ts.c.section_id == section_id)
    ).first()
    if not assignment:
        abort(404)

    # Tenant isolation: every referenced entity must belong to this school/year.
    employee = _load_employee_for_school(employee_id, school)
    section = _scoped(
        Section.query.filter(Section.id == section_id,
                             Section.school_id == school.id,
                             Section.academic_year_id == year.id)
    ).first()
    subject = _scoped(
        Subject.query.filter(Subject.id == subject_id,
                             Subject.school_id == school.id)
    ).first()
    if not employee or not section or not subject:
        abort(404)

    exam_rows = _scoped(
        db.session.query(
            Exam,
            func.count(ExamResult.id).label('result_count'),
            func.coalesce(func.sum(ExamResult.marks), 0).label('marks_sum'),
            func.coalesce(func.sum(_pass_case()), 0).label('pass_count'),
        )
        .outerjoin(ExamResult, ExamResult.exam_id == Exam.id)
        .filter(Exam.subject_id == subject_id,
                Exam.section_id == section_id,
                Exam.school_id == school.id,
                Exam.academic_year_id == year.id)
        .group_by(Exam.id)
        .order_by(Exam.exam_date.desc())
    ).all()

    exams = []
    for exam, result_count, marks_sum, pass_count in exam_rows:
        max_total = float(exam.max_marks or 0) * result_count
        exams.append({
            'exam':         exam,
            'result_count': result_count,
            'avg_pct':      round(float(marks_sum) / max_total * 100, 1) if max_total else None,
            'pass_count':   pass_count,
            'pass_pct':     round(pass_count / result_count * 100, 1) if result_count else None,
        })

    return render_template(
        'evaluations/performance_detail.html',
        employee=employee,
        subject=subject,
        section=section,
        grade=section.grade,
        exams=exams,
        view_year=year,
        selected_school_id=school.id if school else None,
        is_super_admin=_is_super_admin(),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  MANUAL EVALUATIONS  (original EmployeeEvaluation records — kept intact)
# ─────────────────────────────────────────────────────────────────────────────

@evaluations_bp.route('/manual')
@login_required
@permission_required('manage_employees')
def manual():
    page = request.args.get('page', 1, type=int)
    school = _selected_school()
    year = get_view_year(school.id) if school else None

    query = _evaluation_query().join(Employee)
    if school and year:
        query = query.filter(EmployeeEvaluation.school_id == school.id,
                             EmployeeEvaluation.academic_year_id == year.id)
    else:
        query = query.filter(EmployeeEvaluation.id == -1)

    evals = (query
             .order_by(EmployeeEvaluation.created_at.desc())
             .paginate(page=page, per_page=20))
    return render_template(
        'evaluations/manual.html',
        evals=evals,
        schools=_schools_for_selector(),
        selected_school_id=school.id if school else None,
        is_super_admin=_is_super_admin(),
    )


@evaluations_bp.route('/create', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_employees')
def create():
    school = _selected_school()
    employees = _employees_for_school(school)

    if request.method == 'POST':
        employee = _load_employee_for_school(
            request.form.get('employee_id', type=int),
            school,
        )
        if not employee:
            abort(403)

        year = get_active_year(employee.school_id)
        if not year:
            flash('Select a school with an active academic year before creating evaluations.', 'danger')
            return _redirect_to_manual(employee.school_id)

        perf = _score('performance', 5)
        disc = _score('discipline', 5)
        att = _score('attendance_score', 5)
        final = Decimal(str(round((perf + disc + att) / 3, 2)))

        ev = EmployeeEvaluation(
            employee_id=employee.id,
            evaluator_id=current_user.id,
            school_id=employee.school_id,
            academic_year_id=year.id,
            period=request.form.get('period', '').strip(),
            performance=perf,
            discipline=disc,
            attendance_score=att,
            final_score=final,
            notes=request.form.get('notes', '').strip(),
        )
        db.session.add(ev)
        db.session.commit()
        flash('Evaluation saved.', 'success')
        return _redirect_to_manual(employee.school_id)

    return render_template(
        'evaluations/form.html',
        employees=employees,
        record=None,
        schools=_schools_for_selector(),
        selected_school_id=school.id if school else None,
        is_super_admin=_is_super_admin(),
    )


@evaluations_bp.route('/<int:ev_id>/edit', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_employees')
def edit(ev_id):
    ev = _evaluation_query().get_or_404(ev_id)
    year = get_active_year(ev.school_id)
    if not _is_super_admin():
        school = get_current_school()
        if (not school or ev.school_id != school.id or
                not year or ev.academic_year_id != year.id):
            abort(403)

    employees = _employees_for_school(ev.school)
    if request.method == 'POST':
        perf = _score('performance', ev.performance)
        disc = _score('discipline', ev.discipline)
        att = _score('attendance_score', ev.attendance_score)
        ev.performance = perf
        ev.discipline = disc
        ev.attendance_score = att
        ev.final_score = Decimal(str(round((perf + disc + att) / 3, 2)))
        ev.notes = request.form.get('notes', '').strip()
        ev.period = request.form.get('period', ev.period).strip()
        db.session.commit()
        flash('Evaluation updated.', 'success')
        return _redirect_to_manual(ev.school_id)

    return render_template(
        'evaluations/form.html',
        employees=employees,
        record=ev,
        schools=_schools_for_selector(),
        selected_school_id=ev.school_id,
        is_super_admin=_is_super_admin(),
    )


@evaluations_bp.route('/<int:ev_id>/delete', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_employees')
def delete(ev_id):
    ev = _evaluation_query().get_or_404(ev_id)
    if not _is_super_admin():
        school = get_current_school()
        year = get_active_year(school.id) if school else None
        if (not school or ev.school_id != school.id or
                not year or ev.academic_year_id != year.id):
            abort(403)

    school_id = ev.school_id
    db.session.delete(ev)
    db.session.commit()
    flash('Evaluation deleted.', 'success')
    return _redirect_to_manual(school_id)

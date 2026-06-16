"""Al-Muhandis – Grades Blueprint"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, abort, jsonify
from flask_login import login_required, current_user
from datetime import datetime as dt

from app.models import (db, Exam, ExamResult, ExamType, Subject, Section,
                         Student, AcademicYear, teacher_subjects, Employee, Grade)
from app.utils.decorators import (permission_required, get_teacher_section_ids,
                                   get_current_school, get_active_year, get_view_year,
                                   historical_guard)
from app.utils.helpers import calculate_grade_letter

grades_bp = Blueprint('grades', __name__, template_folder='../../templates/grades')


def _is_teacher():
    return (current_user.is_authenticated and
            current_user.role and
            current_user.role.name == 'teacher')


def _teacher_subject_ids():
    """Return the set of subject IDs the current teacher teaches."""
    emp = Employee.query.filter_by(user_id=current_user.id).first()
    if not emp:
        return set()
    return {
        row[0] for row in
        db.session.query(teacher_subjects.c.subject_id)
                  .filter(teacher_subjects.c.employee_id == emp.id)
                  .all()
    }


def _build_exam_query(base_query, search, exam_type_filter, subject_filter, start_date, end_date):
    if search:
        base_query = base_query.filter(Exam.exam_name.ilike(f'%{search}%'))
    if exam_type_filter != 'all':
        base_query = base_query.filter(Exam.exam_type_id == int(exam_type_filter))
    if subject_filter != 'all':
        base_query = base_query.filter(Exam.subject_id == int(subject_filter))
    try:
        if start_date:
            base_query = base_query.filter(Exam.exam_date >= dt.strptime(start_date, '%Y-%m-%d').date())
        if end_date:
            base_query = base_query.filter(Exam.exam_date <= dt.strptime(end_date, '%Y-%m-%d').date())
    except ValueError:
        pass
    return base_query


def _apply_teacher_scope(query):
    """Restrict an Exam query to the teacher's own sections and subjects."""
    section_ids = get_teacher_section_ids(current_user)
    subject_ids = _teacher_subject_ids()
    if section_ids:
        query = query.filter(Exam.section_id.in_(section_ids))
    else:
        query = query.filter(Exam.id == -1)
    if subject_ids:
        query = query.filter(Exam.subject_id.in_(subject_ids))
    return query


def _notify_grade_results(exam, students):
    """FCM push to the linked parents of each graded student (post-commit).

    The notification body never contains the marks — only that a grade was
    recorded — so no sensitive academic data leaves the API. Parents are
    resolved server-side via the student→parent link; school isolation is
    inherited from that relationship. Never raises.
    """
    if not students:
        return
    try:
        from app.services.notifications import NotificationService
        for student in students:
            NotificationService.send_to_parents_of_student(
                student.id,
                'درجة جديدة',
                f'تم رصد درجة جديدة في {exam.exam_name}.',
                ntype='grade',
                data={
                    'type':       'grade',
                    'screen':     'grades',
                    'route':      '/parent/grades',
                    'exam_id':    str(exam.id),
                    'subject_id': str(exam.subject_id),
                    'student_id': str(student.id),
                },
            )
    except Exception:
        # Push is best-effort; a delivery failure must not fail the grade save.
        import logging
        logging.getLogger('mecha.grades').exception(
            '[grades] FCM push failed for exam_id=%s', getattr(exam, 'id', None))


def _notify_new_exam(exam):
    """FCM push announcing a newly scheduled exam to the section's parents.

    The exam is section-scoped, so recipients are the linked parents of the
    active students in that section, resolved server-side. Never raises.
    """
    try:
        from app.services.notifications import NotificationService
        section_students = (Student.query
                            .filter_by(section_id=exam.section_id, status='active')
                            .all())
        for student in section_students:
            NotificationService.send_to_parents_of_student(
                student.id,
                'اختبار جديد',
                f'تم جدولة اختبار جديد: {exam.exam_name}.',
                ntype='exam',
                data={
                    'type':       'exam',
                    'screen':     'exams',
                    'route':      '/parent/exams',
                    'exam_id':    str(exam.id),
                    'subject_id': str(exam.subject_id),
                    'student_id': str(student.id),
                },
            )
    except Exception:
        import logging
        logging.getLogger('mecha.grades').exception(
            '[grades] FCM push failed for new exam_id=%s', getattr(exam, 'id', None))


@grades_bp.route('/')
@login_required
@permission_required('enter_grades')
def index():
    search           = request.args.get('q', '').strip()
    student_search   = request.args.get('student_q', '').strip()
    exam_type_filter = request.args.get('exam_type', 'all')
    subject_filter   = request.args.get('subject_id', 'all')
    start_date       = request.args.get('start_date', '')
    end_date         = request.args.get('end_date', '')

    school = get_current_school()
    year   = get_view_year(school.id) if school else None

    base = Exam.query
    if year:
        base = base.filter(Exam.academic_year_id == year.id)
    if _is_teacher():
        base = _apply_teacher_scope(base)

    exams = _build_exam_query(
        base, search, exam_type_filter, subject_filter, start_date, end_date
    ).order_by(Exam.exam_date.desc()).all()

    results_view = None
    if subject_filter != 'all':
        results_base = ExamResult.query.join(Exam)
        if year:
            results_base = results_base.filter(Exam.academic_year_id == year.id)
        if _is_teacher():
            results_base = _apply_teacher_scope(results_base)
        if student_search:
            results_base = results_base.join(
                Student, ExamResult.student_id == Student.id
            ).filter(
                Student.full_name.ilike(f'%{student_search}%') |
                Student.student_id.ilike(f'%{student_search}%')
            )
        results_view = (
            _build_exam_query(
                results_base,
                search, exam_type_filter, subject_filter, start_date, end_date
            )
            .order_by(Exam.exam_date.desc(), ExamResult.marks.desc())
            .all()
        )

    exam_types = ExamType.query.all()
    if _is_teacher():
        subject_ids = _teacher_subject_ids()
        subjects = Subject.query.filter(Subject.id.in_(subject_ids)).all() if subject_ids else []
    else:
        subjects = Subject.query.all()

    return render_template('grades/index.html',
                           exams=exams,
                           results_view=results_view,
                           exam_types=exam_types,
                           subjects=subjects,
                           search=search,
                           student_search=student_search,
                           exam_type_filter=exam_type_filter,
                           subject_filter=subject_filter,
                           start_date=start_date,
                           end_date=end_date)


@grades_bp.route('/export/excel')
@login_required
@permission_required('enter_grades')
def export_excel():
    from flask import Response
    from app.utils.excel_export import export_exams

    search           = request.args.get('q', '').strip()
    student_search   = request.args.get('student_q', '').strip()
    exam_type_filter = request.args.get('exam_type', 'all')
    subject_filter   = request.args.get('subject_id', 'all')
    start_date       = request.args.get('start_date', '')
    end_date         = request.args.get('end_date', '')

    base = Exam.query
    if _is_teacher():
        base = _apply_teacher_scope(base)

    exams = _build_exam_query(
        base, search, exam_type_filter, subject_filter, start_date, end_date
    ).order_by(Exam.exam_date.desc()).all()

    subject_report = subject_filter != 'all'
    data = export_exams(
        exams,
        subject_report=subject_report,
        student_search=student_search if subject_report else '',
    )
    if not data:
        flash('مكتبة Excel غير متاحة.', 'warning')
        return redirect(url_for('grades.index'))

    filename = 'subject_report.xlsx' if subject_report else 'exams.xlsx'
    return Response(
        data,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


@grades_bp.route('/exams/create', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('enter_grades')
def create_exam():
    exam_types  = ExamType.query.all()
    school      = get_current_school()
    active_year = get_active_year(school.id) if school else None
    if not school or not active_year:
        flash('Select a school with an active academic year before creating exams.', 'danger')
        return redirect(url_for('grades.index'))
    years_q = AcademicYear.query
    if school:
        years_q = years_q.filter_by(school_id=school.id)
    years = years_q.order_by(AcademicYear.start_date.desc()).all()

    # Grade list for cascade (school + year scoped)
    grades_q = (Grade.query
                .filter_by(school_id=school.id, academic_year_id=active_year.id)
                .order_by(Grade.name))

    if _is_teacher():
        section_ids = get_teacher_section_ids(current_user)
        subject_ids = _teacher_subject_ids()
        sections = Section.query.filter(Section.id.in_(section_ids)).order_by(Section.name).all() if section_ids else []
        subjects = Subject.query.filter(Subject.id.in_(subject_ids)).order_by(Subject.name).all() if subject_ids else []
        teacher_grade_ids = list({s.grade_id for s in sections if s.grade_id})
        grades = grades_q.filter(Grade.id.in_(teacher_grade_ids)).all() if teacher_grade_ids else []
    else:
        sections = (Section.query
                    .filter_by(academic_year_id=active_year.id)
                    .order_by(Section.name).all())
        subjects = Subject.query.order_by(Subject.name).all()
        grades   = grades_q.all()

    if request.method == 'POST':
        exam_name  = request.form.get('exam_name', '').strip()
        section_id = request.form.get('section_id', type=int)
        grade_id   = request.form.get('grade_id', type=int)
        subject_id = request.form.get('subject_id', type=int)

        def _form_error(msg):
            flash(msg, 'danger')
            return render_template('grades/exam_form.html',
                                   exam_types=exam_types, subjects=subjects,
                                   sections=sections, grades=grades, years=years)

        if not exam_name:
            return _form_error('يرجى إدخال اسم الاختبار.')

        # ── Resolve target sections ──────────────────────────────────────────
        # Section selected → single exam for it.
        # No section       → one exam per section of the selected grade.
        if section_id:
            section = Section.query.filter_by(id=section_id,
                                              academic_year_id=active_year.id).first()
            if not section:
                return _form_error('الشعبة المختارة غير صالحة.')
            target_sections = [section]
            target_grade_id = section.grade_id
        else:
            if not grade_id:
                return _form_error('يرجى اختيار الصف أو الشعبة.')
            grade = Grade.query.filter_by(id=grade_id, school_id=school.id,
                                          academic_year_id=active_year.id).first()
            if not grade:
                return _form_error('الصف المختار غير صالح.')
            target_sections = (Section.query
                               .filter_by(grade_id=grade.id,
                                          academic_year_id=active_year.id)
                               .order_by(Section.name).all())
            if not target_sections:
                return _form_error('لا توجد شعب مسجلة لهذا الصف — أنشئ شعبة أولاً '
                                   'قبل إنشاء اختبار له.')
            target_grade_id = grade.id

        if _is_teacher():
            allowed_sections = get_teacher_section_ids(current_user)
            allowed_subjects = _teacher_subject_ids()
            if subject_id not in allowed_subjects:
                abort(403)
            # Teachers may only create exams for sections assigned to them.
            target_sections = [s for s in target_sections if s.id in allowed_sections]
            if not target_sections:
                abort(403)
        else:
            subject = Subject.query.filter_by(id=subject_id,
                                              grade_id=target_grade_id).first()
            if not subject:
                return _form_error('المادة المختارة لا تنتمي إلى الصف المختار.')

        exam_type_id = request.form.get('exam_type_id', type=int) or None
        year_id      = request.form.get('academic_year_id', type=int)
        exam_date    = dt.strptime(request.form.get('exam_date'), '%Y-%m-%d').date()
        max_marks    = float(request.form.get('max_marks', 100))
        pass_marks   = float(request.form.get('pass_marks', 50))

        created = []
        for sec in target_sections:
            exam = Exam(
                school_id        = school.id,
                exam_name        = exam_name,
                exam_type_id     = exam_type_id,
                subject_id       = subject_id,
                section_id       = sec.id,
                academic_year_id = year_id,
                exam_date        = exam_date,
                max_marks        = max_marks,
                pass_marks       = pass_marks,
            )
            db.session.add(exam)
            created.append(exam)
        db.session.commit()

        # Push only after the exam rows are committed. Each exam is section-scoped,
        # so parents are resolved per section, server-side.
        for exam in created:
            _notify_new_exam(exam)

        if section_id:
            flash('تم إنشاء الاختبار بنجاح.', 'success')
            return redirect(url_for('grades.enter_results', exam_id=created[0].id))

        flash(f'تم إنشاء الاختبار لجميع الشعب بنجاح ({len(created)} شعبة).', 'success')
        return redirect(url_for('grades.index'))

    return render_template('grades/exam_form.html',
                           exam_types=exam_types, subjects=subjects,
                           sections=sections, grades=grades, years=years)


@grades_bp.route('/exams/<int:exam_id>/results', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('enter_grades')
def enter_results(exam_id):
    exam = Exam.query.get_or_404(exam_id)

    if _is_teacher():
        allowed_sections = get_teacher_section_ids(current_user)
        allowed_subjects = _teacher_subject_ids()
        if exam.section_id not in allowed_sections or exam.subject_id not in allowed_subjects:
            flash('لا يمكنك إدخال درجات لاختبار خارج نطاق صلاحياتك.', 'danger')
            return redirect(url_for('grades.index'))

    students = Student.query.filter_by(section_id=exam.section_id, status='active').all()
    existing = {r.student_id: r for r in ExamResult.query.filter_by(exam_id=exam_id).all()}

    if request.method == 'POST':
        graded_students = []
        for student in students:
            marks_str = request.form.get(f'marks_{student.id}', '')
            if marks_str == '':
                continue
            marks = float(marks_str)
            grade = calculate_grade_letter(marks, float(exam.max_marks))
            is_p  = marks >= float(exam.pass_marks)

            if student.id in existing:
                existing[student.id].marks       = marks
                existing[student.id].grade_letter = grade
                existing[student.id].is_pass      = is_p
                existing[student.id].entered_by   = current_user.id
            else:
                res = ExamResult(
                    exam_id     = exam_id,
                    student_id  = student.id,
                    school_id   = exam.school_id,
                    academic_year_id = exam.academic_year_id,
                    marks       = marks,
                    grade_letter = grade,
                    is_pass     = is_p,
                    entered_by  = current_user.id,
                )
                db.session.add(res)
            graded_students.append(student)

        db.session.commit()

        results = ExamResult.query.filter_by(exam_id=exam_id)\
                                  .order_by(ExamResult.marks.desc()).all()
        for rank, res in enumerate(results, 1):
            res.rank = rank
        db.session.commit()

        # Push only after the grades are committed. Recipients (the linked
        # parents) are resolved server-side per student; the body never
        # contains the actual marks — Flutter fetches the grade via the API.
        _notify_grade_results(exam, graded_students)

        flash('تم حفظ الدرجات وحساب الترتيب.', 'success')
        return redirect(url_for('grades.index'))

    return render_template('grades/results_form.html',
                           exam=exam, students=students, existing=existing)


def _pivot_data(section_id, subject_filter, exam_type_filter, start_date, end_date):
    base = Exam.query.filter(Exam.section_id == section_id)
    exams = _build_exam_query(
        base, '', exam_type_filter, subject_filter, start_date, end_date
    ).order_by(Exam.exam_date.asc(), Exam.id.asc()).all()

    rows = []
    if not exams:
        return exams, rows

    exam_ids    = [e.id for e in exams]
    students    = (Student.query
                   .filter_by(section_id=section_id, status='active')
                   .order_by(Student.full_name.asc())
                   .all())
    student_ids = [s.id for s in students]

    all_results = (ExamResult.query
                   .filter(ExamResult.exam_id.in_(exam_ids),
                           ExamResult.student_id.in_(student_ids))
                   .all())
    lookup = {(r.student_id, r.exam_id): r for r in all_results}

    for student in students:
        cells = [lookup.get((student.id, e.id)) for e in exams]
        taken = [(c, exams[i]) for i, c in enumerate(cells) if c is not None]
        if taken:
            avg = round(
                sum(float(c.marks) / float(e.max_marks) * 100 for c, e in taken)
                / len(taken), 1
            )
        else:
            avg = None
        rows.append({'student': student, 'cells': cells, 'avg': avg, 'taken': len(taken)})

    return exams, rows


@grades_bp.route('/gradebook')
@login_required
@permission_required('enter_grades')
def gradebook():
    section_id       = request.args.get('section_id', type=int)
    subject_filter   = request.args.get('subject_id', 'all')
    exam_type_filter = request.args.get('exam_type', 'all')
    start_date       = request.args.get('start_date', '')
    end_date         = request.args.get('end_date', '')
    stage_filter     = request.args.get('stage', '').strip()
    grade_filter     = request.args.get('grade_id', type=int)

    school     = get_current_school()
    year       = get_view_year(school.id) if school else None
    exam_types = ExamType.query.all()
    stages     = ['ابتدائية', 'متوسطة', 'إعدادية']

    # ── Grades list (drives the grade dropdown) ───────────────────────────────
    grades_q = Grade.query
    if school:
        grades_q = grades_q.filter(Grade.school_id == school.id)
    if year:
        grades_q = grades_q.filter(Grade.academic_year_id == year.id)
    if stage_filter:
        grades_q = grades_q.filter(Grade.stage == stage_filter)
    grades = grades_q.order_by(Grade.name).all()

    # ── Sections / subjects base queries ──────────────────────────────────────
    if _is_teacher():
        allowed_sec_ids = get_teacher_section_ids(current_user)
        allowed_sub_ids = _teacher_subject_ids()
        sections_q = (Section.query.filter(Section.id.in_(allowed_sec_ids))
                      if allowed_sec_ids else Section.query.filter(Section.id.in_([])))
        subjects_q = (Subject.query.filter(Subject.id.in_(allowed_sub_ids))
                      if allowed_sub_ids else Subject.query.filter(Subject.id.in_([])))
        if section_id and section_id not in allowed_sec_ids:
            section_id = None
    else:
        sections_q = Section.query
        subjects_q = Subject.query

    if year:
        sections_q = sections_q.filter(Section.academic_year_id == year.id)

    # ── Cascade: stage → grade → section / subject ────────────────────────────
    if stage_filter:
        stage_grade_ids = [g.id for g in grades]
        sections_q = (sections_q.filter(Section.grade_id.in_(stage_grade_ids))
                      if stage_grade_ids else sections_q.filter(Section.id.in_([])))
    if grade_filter:
        sections_q = sections_q.filter(Section.grade_id == grade_filter)
        subjects_q = subjects_q.filter(Subject.grade_id == grade_filter)

    sections = sections_q.order_by(Section.name).all()
    subjects = subjects_q.order_by(Subject.name).all()

    exams, rows = [], []
    stats = {}
    if section_id:
        exams, rows = _pivot_data(section_id, subject_filter, exam_type_filter,
                                  start_date, end_date)
        with_avg = [r for r in rows if r['avg'] is not None]
        stats = {
            'n_students': len(rows),
            'n_exams':    len(exams),
            'n_passing':  sum(1 for r in with_avg if r['avg'] >= 60),
            'n_failing':  sum(1 for r in with_avg if r['avg'] < 60),
        }

    return render_template('grades/gradebook.html',
                           exams=exams, rows=rows, stats=stats,
                           sections=sections, subjects=subjects,
                           grades=grades, exam_types=exam_types,
                           stages=stages,
                           section_id=section_id,
                           subject_filter=subject_filter,
                           exam_type_filter=exam_type_filter,
                           start_date=start_date,
                           end_date=end_date,
                           stage_filter=stage_filter,
                           grade_filter=grade_filter)


@grades_bp.route('/gradebook/export')
@login_required
@permission_required('enter_grades')
def gradebook_export():
    from flask import Response
    from app.utils.excel_export import export_gradebook

    section_id       = request.args.get('section_id', type=int)
    subject_filter   = request.args.get('subject_id', 'all')
    exam_type_filter = request.args.get('exam_type', 'all')
    start_date       = request.args.get('start_date', '')
    end_date         = request.args.get('end_date', '')

    if not section_id:
        flash('يرجى اختيار الشعبة أولاً.', 'warning')
        return redirect(url_for('grades.gradebook'))

    if _is_teacher() and section_id not in get_teacher_section_ids(current_user):
        abort(403)

    exams, rows = _pivot_data(section_id, subject_filter, exam_type_filter,
                               start_date, end_date)
    data = export_gradebook(exams, rows)
    if not data:
        flash('مكتبة Excel غير متاحة.', 'warning')
        return redirect(url_for('grades.gradebook'))

    return Response(
        data,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename=gradebook.xlsx'}
    )


@grades_bp.route('/report')
@login_required
@permission_required('enter_grades')
def report():
    school = get_current_school()
    year   = get_view_year(school.id) if school else None

    # student_id is the only param that drives the results table.
    # stage / grade_id / section_id are JS-only cascade state echoed back so
    # the dropdowns restore correctly after a page load.
    selected_student_id = request.args.get('student_id', type=int)

    results = []
    selected_student_obj = None

    if selected_student_id and school and year:
        student = Student.query.filter_by(
            id=selected_student_id,
            school_id=school.id,
            academic_year_id=year.id,
            status='active',
        ).first()

        if student:
            # Teacher scope: teacher may only view students in their sections
            if _is_teacher():
                allowed = get_teacher_section_ids(current_user)
                if student.section_id not in (allowed or []):
                    student = None

        if student:
            selected_student_obj = student
            s_results = (ExamResult.query
                         .filter_by(student_id=student.id,
                                    school_id=school.id,
                                    academic_year_id=year.id)
                         .all())
            if s_results:
                avg = sum(float(r.marks) for r in s_results) / len(s_results)
                results.append({'student': student,
                                'avg': round(avg, 2),
                                'count': len(s_results)})

    return render_template('grades/report.html',
                           results=results,
                           selected_student_id=selected_student_id,
                           selected_student_obj=selected_student_obj)


# ── Grades report — cascade + student search JSON APIs ───────────────────────

def _report_allowed_section_ids():
    """Return the set of section IDs visible to the current user, or None = all."""
    if _is_teacher():
        return set(get_teacher_section_ids(current_user) or [])
    return None  # non-teacher: no extra restriction


def _report_student_payload(student):
    section = student.section
    grade   = section.grade if section else None
    return {
        'id':         student.id,
        'student_id': student.student_id,
        'full_name':  student.full_name,
        'grade':      grade.name   if grade   else '',
        'section':    section.name if section else '',
    }


@grades_bp.route('/report/api/stages')
@login_required
@permission_required('enter_grades')
def report_api_stages():
    school = get_current_school()
    year   = get_view_year(school.id) if school else None
    if not school or not year:
        return jsonify([])

    q = (db.session.query(Grade.stage)
         .filter(Grade.school_id == school.id,
                 Grade.academic_year_id == year.id,
                 Grade.stage.isnot(None),
                 Grade.stage != ''))

    allowed = _report_allowed_section_ids()
    if allowed is not None:
        # Restrict to grades that contain at least one allowed section
        allowed_grade_ids = (
            db.session.query(Section.grade_id)
            .filter(Section.id.in_(allowed))
            .subquery()
        )
        q = q.filter(Grade.id.in_(allowed_grade_ids))

    rows = q.distinct().order_by(Grade.stage).all()
    return jsonify([{'value': r[0], 'label': r[0]} for r in rows])


@grades_bp.route('/report/api/grades')
@login_required
@permission_required('enter_grades')
def report_api_grades():
    school = get_current_school()
    year   = get_view_year(school.id) if school else None
    stage  = request.args.get('stage', '').strip()
    if not school or not year or not stage:
        return jsonify([])

    grades_q = (Grade.query
                .filter_by(school_id=school.id, academic_year_id=year.id, stage=stage)
                .order_by(Grade.name))

    allowed = _report_allowed_section_ids()
    if allowed is not None:
        allowed_grade_ids = (
            db.session.query(Section.grade_id)
            .filter(Section.id.in_(allowed))
            .subquery()
        )
        grades_q = grades_q.filter(Grade.id.in_(allowed_grade_ids))

    return jsonify([{'id': g.id, 'name': g.name} for g in grades_q.all()])


@grades_bp.route('/report/api/sections')
@login_required
@permission_required('enter_grades')
def report_api_sections():
    school   = get_current_school()
    year     = get_view_year(school.id) if school else None
    grade_id = request.args.get('grade_id', type=int)
    if not school or not year or not grade_id:
        return jsonify([])

    grade = Grade.query.filter_by(id=grade_id, school_id=school.id,
                                   academic_year_id=year.id).first()
    if not grade:
        return jsonify([])

    sections_q = (Section.query
                  .filter_by(grade_id=grade_id,
                             school_id=school.id,
                             academic_year_id=year.id)
                  .order_by(Section.name))

    allowed = _report_allowed_section_ids()
    if allowed is not None:
        sections_q = sections_q.filter(Section.id.in_(allowed))

    return jsonify([{'id': s.id, 'name': s.name} for s in sections_q.all()])


@grades_bp.route('/report/api/students')
@login_required
@permission_required('enter_grades')
def report_api_students():
    school = get_current_school()
    year   = get_view_year(school.id) if school else None
    if not school or not year:
        return jsonify({'results': []})

    term       = request.args.get('q', '').strip()
    section_id = request.args.get('section_id', type=int)
    grade_id   = request.args.get('grade_id',   type=int)

    # Need at least a search term or a section to return anything
    if not term and not section_id:
        return jsonify({'results': []})

    allowed = _report_allowed_section_ids()

    q = Student.query.filter_by(status='active',
                                 school_id=school.id,
                                 academic_year_id=year.id)

    if section_id:
        # Verify section belongs to this school/year
        sec = Section.query.filter_by(id=section_id,
                                       school_id=school.id,
                                       academic_year_id=year.id).first()
        if not sec:
            return jsonify({'results': []})
        if allowed is not None and section_id not in allowed:
            return jsonify({'results': []})
        q = q.filter(Student.section_id == section_id)
    elif grade_id:
        # Scope to all sections of the grade visible to this user
        secs_q = Section.query.filter_by(grade_id=grade_id,
                                          school_id=school.id,
                                          academic_year_id=year.id)
        if allowed is not None:
            secs_q = secs_q.filter(Section.id.in_(allowed))
        sec_ids = [s.id for s in secs_q.all()]
        if not sec_ids:
            return jsonify({'results': []})
        q = q.filter(Student.section_id.in_(sec_ids))
    elif allowed is not None:
        q = q.filter(Student.section_id.in_(allowed))

    if term:
        q = q.filter(
            Student.full_name.ilike(f'%{term}%') |
            Student.student_id.ilike(f'%{term}%')
        )

    limit = 200 if (section_id and not term) else 20
    students = q.order_by(Student.full_name).limit(limit).all()
    return jsonify({'results': [_report_student_payload(s) for s in students]})

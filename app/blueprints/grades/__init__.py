"""Al-Muhandis – Grades Blueprint"""
from flask import Blueprint, render_template, redirect, url_for, flash, request, abort
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
        subject_id = request.form.get('subject_id', type=int)

        if not exam_name:
            flash('يرجى إدخال اسم الاختبار.', 'danger')
            return render_template('grades/exam_form.html',
                                   exam_types=exam_types, subjects=subjects,
                                   sections=sections, grades=grades, years=years)

        if _is_teacher():
            allowed_sections = get_teacher_section_ids(current_user)
            allowed_subjects = _teacher_subject_ids()
            if section_id not in allowed_sections or subject_id not in allowed_subjects:
                abort(403)
        else:
            section = Section.query.filter_by(id=section_id,
                                              academic_year_id=active_year.id).first()
            if not section:
                flash('الشعبة المختارة غير صالحة.', 'danger')
                return render_template('grades/exam_form.html',
                                       exam_types=exam_types, subjects=subjects,
                                       sections=sections, grades=grades, years=years)
            subject = Subject.query.filter_by(id=subject_id,
                                              grade_id=section.grade_id).first()
            if not subject:
                flash('المادة المختارة لا تنتمي إلى صف الشعبة المختارة.', 'danger')
                return render_template('grades/exam_form.html',
                                       exam_types=exam_types, subjects=subjects,
                                       sections=sections, grades=grades, years=years)

        exam = Exam(
            school_id        = school.id,
            exam_name        = exam_name,
            exam_type_id     = request.form.get('exam_type_id', type=int) or None,
            subject_id       = subject_id,
            section_id       = section_id,
            academic_year_id = request.form.get('academic_year_id', type=int),
            exam_date        = dt.strptime(request.form.get('exam_date'), '%Y-%m-%d').date(),
            max_marks        = float(request.form.get('max_marks', 100)),
            pass_marks       = float(request.form.get('pass_marks', 50)),
        )
        db.session.add(exam)
        db.session.commit()
        flash('تم إنشاء الاختبار. يمكنك الآن إدخال الدرجات.', 'success')
        return redirect(url_for('grades.enter_results', exam_id=exam.id))

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

        db.session.commit()

        results = ExamResult.query.filter_by(exam_id=exam_id)\
                                  .order_by(ExamResult.marks.desc()).all()
        for rank, res in enumerate(results, 1):
            res.rank = rank
        db.session.commit()

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
    section_id    = request.args.get('section_id', type=int)
    student_search = request.args.get('student_q', '').strip()

    if _is_teacher():
        section_ids = get_teacher_section_ids(current_user)
        sections = Section.query.filter(Section.id.in_(section_ids)).all() if section_ids else []
        if section_id and section_id not in section_ids:
            section_id = None
    else:
        sections = Section.query.all()

    results = []
    if section_id:
        students_q = Student.query.filter_by(section_id=section_id, status='active')
        if student_search:
            students_q = students_q.filter(
                Student.full_name.ilike(f'%{student_search}%') |
                Student.student_id.ilike(f'%{student_search}%')
            )
        students = students_q.all()
        for s in students:
            s_results = ExamResult.query.filter_by(student_id=s.id).all()
            if s_results:
                avg = sum(float(r.marks) for r in s_results) / len(s_results)
                results.append({'student': s, 'avg': round(avg, 2),
                                'count': len(s_results)})
        results.sort(key=lambda x: x['avg'], reverse=True)
    return render_template('grades/report.html',
                           results=results, sections=sections,
                           section_id=section_id,
                           student_search=student_search)

"""Mecha-School — Students Blueprint  (Phase 6: multi-tenant + capacity check)"""
from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, abort)
from flask_login import login_required, current_user
from sqlalchemy.orm import joinedload
from app.models import db, Student, Section, Grade, AcademicYear, StudentDocument, parent_students
from app.utils.decorators import (permission_required, get_teacher_section_ids,
                                   get_current_school, get_active_year, get_view_year,
                                   historical_guard)
from app.utils.helpers import save_uploaded_file, generate_student_id

students_bp = Blueprint('students', __name__,
                         template_folder='../../templates/students')


def _is_teacher():
    return (current_user.is_authenticated and
            current_user.role and
            current_user.role.name == 'teacher')


@students_bp.route('/')
@login_required
@permission_required('view_students')
def index():
    page       = request.args.get('page', 1, type=int)
    search     = request.args.get('q', '')
    status     = request.args.get('status', '')
    grade_id   = request.args.get('grade_id', type=int)
    section_id = request.args.get('section_id', type=int)
    school     = get_current_school()
    year       = get_view_year(school.id) if school else None

    query = Student.query

    # School scoping — always filter by current school
    if school:
        query = query.filter_by(school_id=school.id)

    if _is_teacher():
        teacher_sids = get_teacher_section_ids(current_user)
        if teacher_sids:
            query = query.filter(Student.section_id.in_(teacher_sids))
        else:
            query = query.filter(Student.id == -1)

    if search:
        query = query.filter(
            Student.full_name.ilike(f'%{search}%') |
            Student.student_id.ilike(f'%{search}%')
        )
    if status == 'archived':
        query = query.filter_by(status='archived')
    elif status:
        query = query.filter_by(status=status)
    else:
        # Archived students are hidden from the default list; use status=archived to view them
        query = query.filter(Student.status != 'archived')

    if section_id:
        query = query.filter_by(section_id=section_id)
    elif grade_id:
        section_ids_for_grade = [
            s.id for s in Section.query.filter_by(grade_id=grade_id).all()
        ]
        if section_ids_for_grade:
            query = query.filter(Student.section_id.in_(section_ids_for_grade))
        else:
            query = query.filter(Student.id == -1)

    students = (query
                .options(joinedload(Student.section).joinedload(Section.grade))
                .execution_options(include_all_years=True)
                .order_by(Student.created_at.desc())
                .paginate(page=page, per_page=20, error_out=False))

    # Grades and sections for filter dropdowns
    grades_q   = Grade.query
    sections_q = Section.query
    if school and year:
        grades_q   = grades_q.filter_by(school_id=school.id, academic_year_id=year.id)
        sections_q = sections_q.filter_by(school_id=school.id, academic_year_id=year.id)
    grades_list   = grades_q.order_by(Grade.name).all()
    sections_list = (sections_q.filter_by(grade_id=grade_id).order_by(Section.name).all()
                     if grade_id else [])

    # Capacity info for the header banner
    capacity_info = None
    if school and school.capacity and school.capacity > 0:
        current_count = Student.query.filter_by(
            school_id=school.id, status='active').count()
        capacity_info = {
            'capacity': school.capacity,
            'current':  current_count,
            'at_limit': school.is_at_capacity,
        }

    return render_template('students/index.html',
                           students=students, search=search, status=status,
                           capacity_info=capacity_info,
                           active_year=year,
                           grades_list=grades_list,
                           sections_list=sections_list,
                           grade_id=grade_id,
                           section_id=section_id)


@students_bp.route('/create', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('add_student')
def create():
    school = get_current_school()
    year   = get_active_year(school.id) if school else None

    # ── SCHOOL / YEAR GUARDS ────────────────────────────────────────────────
    if not school:
        flash('يجب تحديد مدرسة أولاً. تواصل مع مسؤول النظام.', 'warning')
        return redirect(url_for('admin.dashboard'))
    if not year:
        flash(
            'لا يوجد عام دراسي نشط لهذه المدرسة. '
            'يرجى مراجعة مسؤول النظام لتفعيل عام دراسي قبل إضافة الطلاب.',
            'danger',
        )
        return redirect(url_for('students.index'))

    # ── CAPACITY CHECK ──────────────────────────────────────────────────────
    if school and school.capacity and school.capacity > 0:
        current_count = Student.query.filter_by(
            school_id=school.id, status='active').count()
        if current_count >= school.capacity:
            flash(
                f'لقد تم الوصول إلى الحد الأقصى لعدد الطلاب في هذه المدرسة '
                f'({school.capacity} طالب). لا يمكن إضافة طلاب جدد حتى يتم '
                f'تعديل السعة أو تخفيض العدد الحالي.',
                'danger'
            )
            return redirect(url_for('students.index'))

    # Build sections list scoped to current school
    all_sections = Section.query
    if school and year:
        school_year_grade_ids = [g.id for g in
                                  Grade.query.execution_options(include_all_years=True)
                                  .filter_by(academic_year_id=year.id).all()]
        if school_year_grade_ids:
            all_sections = all_sections.filter(
                Section.grade_id.in_(school_year_grade_ids))

    all_sections = all_sections.all()

    if _is_teacher():
        section_ids = get_teacher_section_ids(current_user)
        sections = [s for s in all_sections if s.id in section_ids]
    else:
        sections = all_sections

    grades_q = Grade.query.execution_options(include_all_years=True)
    if school and year:
        grades_q = grades_q.filter_by(school_id=school.id, academic_year_id=year.id)
    grades = grades_q.order_by(Grade.name).all()

    if request.method == 'POST':
        from datetime import datetime as dt
        last_student = Student.query.order_by(Student.id.desc()).first()
        student_id   = generate_student_id(last_student.id if last_student else 0)

        section_id = request.form.get('section_id', type=int)
        if _is_teacher() and section_id not in get_teacher_section_ids(current_user):
            abort(403)

        photo_path = None
        if 'photo' in request.files:
            photo_path = save_uploaded_file(request.files['photo'], 'students')

        dob_str = request.form.get('date_of_birth')
        dob     = dt.strptime(dob_str, '%Y-%m-%d').date() if dob_str else None

        student = Student(
            student_id        = student_id,
            full_name         = request.form.get('full_name', '').strip(),
            date_of_birth     = dob,
            gender            = request.form.get('gender', ''),
            nationality       = request.form.get('nationality', '').strip(),
            address           = request.form.get('address', '').strip(),
            phone             = request.form.get('phone', '').strip(),
            section_id        = section_id,
            guardian_name     = request.form.get('guardian_name', '').strip(),
            guardian_phone    = request.form.get('guardian_phone', '').strip(),
            guardian_email    = request.form.get('guardian_email', '').strip(),
            guardian_relation = request.form.get('guardian_relation', '').strip(),
            photo             = photo_path,
            notes             = request.form.get('notes', '').strip(),
            # Multi-tenant fields
            school_id         = school.id if school else None,
            academic_year_id  = year.id   if year   else None,
        )
        db.session.add(student)
        db.session.flush()

        doc_types = request.form.getlist('document_type[]')
        doc_files = request.files.getlist('document_file[]')
        for doc_type, doc_file in zip(doc_types, doc_files):
            if doc_file and doc_file.filename:
                saved = save_uploaded_file(
                    doc_file,
                    'students/documents',
                    prefix=f"{student.student_id}_{doc_type or 'document'}"
                )
                if saved:
                    db.session.add(StudentDocument(
                        student_id=student.id,
                        document_type=doc_type.strip() or 'وثيقة',
                        file_path=saved,
                    ))

        db.session.commit()
        flash(f'تم إضافة الطالب {student.full_name} برقم {student.student_id}.', 'success')
        return redirect(url_for('students.index'))

    return render_template('students/form.html', student=None, sections=sections,
                           grades=grades, stages=['ابتدائية', 'متوسطة', 'إعدادية'],
                           selected_grade_id=None, selected_stage=None)


@students_bp.route('/<int:student_id>/edit', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('edit_student')
def edit(student_id):
    student = Student.query.execution_options(include_all_years=True).get_or_404(student_id)
    school  = get_current_school()
    year    = get_active_year(school.id) if school else None

    # Prevent editing a student from another school
    if school and student.school_id and student.school_id != school.id:
        abort(403)

    # Always show sections/grades from the current active year so students
    # can be reassigned across year boundaries (e.g. during year rollover).
    edit_year_id = year.id if year else student.academic_year_id

    all_sections = (Section.query.execution_options(include_all_years=True)
                    .filter_by(academic_year_id=edit_year_id)
                    .all())

    if _is_teacher():
        section_ids = get_teacher_section_ids(current_user)
        if student.section_id not in section_ids:
            flash('لا يمكنك تعديل بيانات طالب خارج شعبتك.', 'danger')
            return redirect(url_for('students.index'))
        sections = [s for s in all_sections if s.id in section_ids]
    else:
        sections = all_sections

    grades = (Grade.query
              .execution_options(include_all_years=True)
              .filter_by(academic_year_id=edit_year_id)
              .order_by(Grade.name).all())

    # selected_* may be None when the student's current section is from a
    # previous year — the form requires a fresh selection in that case.
    selected_section  = next((s for s in sections if s.id == student.section_id), None)
    selected_grade_id = selected_section.grade_id if selected_section else None
    selected_grade    = next((g for g in grades if g.id == selected_grade_id), None)
    selected_stage    = selected_grade.stage if selected_grade else None

    if request.method == 'POST':
        from datetime import datetime as dt
        new_section_id = request.form.get('section_id', type=int)
        if _is_teacher() and new_section_id not in get_teacher_section_ids(current_user):
            abort(403)

        student.full_name         = request.form.get('full_name', student.full_name).strip()
        student.gender            = request.form.get('gender', student.gender)
        student.nationality       = request.form.get('nationality', '').strip()
        student.address           = request.form.get('address', '').strip()
        student.phone             = request.form.get('phone', '').strip()
        student.section_id        = new_section_id
        # Keep academic_year_id in sync with the assigned section's year so
        # the student's record reflects the year they are actively enrolled in.
        if new_section_id and year:
            student.academic_year_id = year.id
        student.guardian_name     = request.form.get('guardian_name', '').strip()
        student.guardian_phone    = request.form.get('guardian_phone', '').strip()
        student.guardian_email    = request.form.get('guardian_email', '').strip()
        student.guardian_relation = request.form.get('guardian_relation', '').strip()
        student.status            = request.form.get('status', student.status)
        student.notes             = request.form.get('notes', '').strip()

        dob_str = request.form.get('date_of_birth')
        if dob_str:
            student.date_of_birth = dt.strptime(dob_str, '%Y-%m-%d').date()

        if 'photo' in request.files and request.files['photo'].filename:
            photo_path = save_uploaded_file(request.files['photo'], 'students')
            if photo_path:
                student.photo = photo_path

        doc_types = request.form.getlist('document_type[]')
        doc_files = request.files.getlist('document_file[]')
        for doc_type, doc_file in zip(doc_types, doc_files):
            if doc_file and doc_file.filename:
                saved = save_uploaded_file(
                    doc_file,
                    'students/documents',
                    prefix=f"{student.student_id}_{doc_type or 'document'}"
                )
                if saved:
                    db.session.add(StudentDocument(
                        student_id=student.id,
                        document_type=doc_type.strip() or 'وثيقة',
                        file_path=saved,
                    ))

        db.session.commit()
        flash('تم تحديث بيانات الطالب بنجاح.', 'success')
        return redirect(url_for('students.view', student_id=student.id))

    return render_template('students/form.html', student=student, sections=sections,
                           grades=grades, stages=['ابتدائية', 'متوسطة', 'إعدادية'],
                           selected_grade_id=selected_grade_id, selected_stage=selected_stage)


@students_bp.route('/<int:student_id>')
@login_required
@permission_required('view_students')
def view(student_id):
    student = (Student.query
               .options(joinedload(Student.section).joinedload(Section.grade))
               .execution_options(include_all_years=True)
               .filter(Student.id == student_id)
               .first_or_404())
    school  = get_current_school()

    if school and student.school_id and student.school_id != school.id:
        abort(403)

    if _is_teacher():
        section_ids = get_teacher_section_ids(current_user)
        if student.section_id not in section_ids:
            flash('لا يمكنك عرض بيانات طالب خارج شعبتك.', 'danger')
            return redirect(url_for('students.index'))

    docs = student.documents.order_by(StudentDocument.uploaded_at.desc()).all()
    return render_template('students/view.html', student=student, docs=docs)


@students_bp.route('/<int:student_id>/archive', methods=['POST'])
@login_required
@historical_guard
@permission_required('delete_student')
def archive(student_id):
    """Set a student's status to archived (or any non-active status).
    This is the safe, data-preserving alternative to hard delete for school managers."""
    student = Student.query.execution_options(include_all_years=True).get_or_404(student_id)
    school  = get_current_school()

    if school and student.school_id and student.school_id != school.id:
        abort(403)
    if _is_teacher():
        flash('المعلمون لا يملكون صلاحية أرشفة الطلاب.', 'danger')
        return redirect(url_for('students.index'))

    new_status = request.form.get('status', 'archived')
    _valid_statuses = {'archived', 'withdrawn', 'transferred', 'graduated', 'active'}
    if new_status not in _valid_statuses:
        new_status = 'archived'

    _labels = {'archived': 'مؤرشف', 'withdrawn': 'مسحوب',
               'transferred': 'منقول', 'graduated': 'متخرج', 'active': 'فعّال'}
    student.status = new_status
    db.session.commit()
    flash(f'تم تغيير حالة الطالب {student.full_name} إلى {_labels[new_status]}.', 'success')
    next_url = request.form.get('next') or url_for('students.index')
    return redirect(next_url)


@students_bp.route('/<int:student_id>/delete', methods=['POST'])
@login_required
@historical_guard
@permission_required('delete_student')
def delete(student_id):
    """Permanent hard delete — super_admin only.
    School managers must use the archive route instead."""
    if not current_user.is_super_admin:
        flash('الحذف النهائي مقتصر على مسؤول النظام الأعلى فقط. '
              'استخدم خيار الأرشفة لإخفاء الطالب من القوائم النشطة.', 'danger')
        return redirect(url_for('students.view', student_id=student_id))

    student = Student.query.execution_options(include_all_years=True).get_or_404(student_id)
    school  = get_current_school()

    if school and student.school_id and student.school_id != school.id:
        abort(403)

    name = student.full_name
    sid  = student.id

    # Remove M2M parent links (no DB-side CASCADE)
    db.session.execute(
        parent_students.delete().where(parent_students.c.student_id == sid)
    )

    # Explicitly delete all child records via raw SQL to bypass:
    # a) lazy='dynamic' cascade unreliability
    # b) ORM year-scope filtering that leaves cross-year rows orphaned
    from sqlalchemy import text
    db.session.execute(
        text("DELETE FROM fee_installments"
             " WHERE fee_record_id IN (SELECT id FROM fee_records WHERE student_id = :sid)"),
        {'sid': sid},
    )
    for tbl in ('student_attendance', 'fee_records', 'exam_results',
                'student_documents', 'student_suspensions'):
        db.session.execute(text(f"DELETE FROM {tbl} WHERE student_id = :sid"), {'sid': sid})

    db.session.delete(student)
    db.session.commit()
    flash(f'تم حذف الطالب {name} وجميع سجلاته بشكل نهائي.', 'success')
    return redirect(url_for('students.index'))


@students_bp.route('/export/excel')
@login_required
@permission_required('view_students')
def export_excel():
    from flask import Response
    from app.utils.excel_export import export_students
    school = get_current_school()
    year   = get_view_year(school.id) if school else None
    status = request.args.get('status', 'active')

    query = Student.query.filter_by(status=status)
    if request.args.get('all_years', '0') == '1':
        query = query.execution_options(include_all_years=True)
    if school:
        query = query.filter_by(school_id=school.id)
    if year and request.args.get('all_years', '0') != '1':
        query = query.filter_by(academic_year_id=year.id)
    if _is_teacher():
        section_ids = get_teacher_section_ids(current_user)
        query = query.filter(Student.section_id.in_(section_ids)) if section_ids \
                else query.filter(Student.id == -1)

    students = query.order_by(Student.full_name).all()
    data = export_students(students)
    if not data:
        flash('مكتبة Excel غير متاحة.', 'warning')
        return redirect(url_for('students.index'))
    return Response(
        data,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename=students_{status}.xlsx'}
    )

"""Middle student record snapshots (السجلات الوسطية)."""
from datetime import datetime, date
import math

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload

from app.models import Section, Student, StudentMiddleRecord, db
from app.utils.decorators import get_current_school, permission_required, any_permission_required
from app.utils.school_stages import ALL_STAGES

SUBJECTS = [
    ('islamic_education', 'التربية الإسلامية'),
    ('arabic_language', 'اللغة العربية'),
    ('english_language', 'اللغة الإنكليزية'),
    ('mathematics', 'الرياضيات'),
    ('physics', 'الفيزياء'),
    ('chemistry', 'الكيمياء'),
    ('biology', 'الأحياء'),
    ('social_studies', 'الاجتماعيات'),
    ('computer', 'الحاسوب'),
    ('moral_education', 'التربية الأخلاقية'),
    ('physical_education', 'التربية الرياضية'),
    ('art_education', 'التربية الفنية'),
]

GRADE_COLUMNS = [
    ('first_term_average', 'معدل الفصل الأول'),
    ('mid_year', 'نصف السنة'),
    ('second_term_average', 'معدل الفصل الثاني'),
    ('annual_effort', 'درجة السعي السنوي'),
    ('final_exam', 'درجة الامتحان النهائي'),
    ('final_grade', 'الدرجة النهائية'),
    ('completion_grade', 'درجة الإكمال'),
    ('final_after_completion', 'الدرجة النهائية بعد الإكمال'),
    ('notes', 'الملاحظات'),
]

student_middle_records_bp = Blueprint(
    'student_middle_records',
    __name__,
    template_folder='../../templates/student_middle_records',
)


def _school_or_404():
    school = get_current_school()
    if not school:
        abort(404)
    return school


def _parse_date(val):
    if not val:
        return None
    try:
        return date.fromisoformat(val.strip())
    except (AttributeError, TypeError, ValueError):
        return None


def _query(school, q='', stage=''):
    query = StudentMiddleRecord.query.filter(StudentMiddleRecord.school_id == school.id)
    if q:
        query = query.filter(
            db.or_(
                StudentMiddleRecord.snap_full_name.ilike(f'%{q}%'),
                StudentMiddleRecord.snap_student_number.ilike(f'%{q}%'),
                StudentMiddleRecord.record_number.ilike(f'%{q}%'),
            )
        )
    if stage:
        query = query.filter(StudentMiddleRecord.snap_stage == stage)
    return query.order_by(StudentMiddleRecord.updated_at.desc(), StudentMiddleRecord.id.desc())


def _build_autofill(student, school, academic_year):
    section = student.section
    grade = section.grade if section else None
    return {
        'record_number': student.student_id or '',
        'page_number': '',
        'father_name': '',
        'grandfather_name': '',
        'great_grandfather_name': '',
        'years_failed': '',
        'snap_full_name': student.full_name or '',
        'snap_student_number': student.student_id or '',
        'snap_stage': grade.stage if grade else '',
        'snap_grade_name': grade.name if grade else '',
        'snap_section_name': section.name if section else '',
        'snap_year_name': academic_year.name if academic_year else '',
        'snap_gender': student.gender or '',
        'snap_date_of_birth': student.date_of_birth.isoformat() if student.date_of_birth else '',
        'snap_phone': student.phone or '',
        'snap_address': student.address or '',
        'snap_status': student.status or 'active',
        'snap_enrollment_date': student.enrollment_date.isoformat() if student.enrollment_date else '',
        'snap_guardian_name': student.guardian_name or '',
        'snap_guardian_phone': student.guardian_phone or '',
        'snap_guardian_relation': student.guardian_relation or '',
        'school_name': school.school_name or '',
        'school_name_ar': school.school_name_ar or '',
        'previous_school': '',
        'admission_date': student.enrollment_date.isoformat() if student.enrollment_date else '',
        'notes': '',
        'total_score': '',
        'first_round_result': '',
        'second_round_result': '',
        'result_notes': '',
    }


def _parse_optional_number(value):
    value = (value or '').strip()
    if not value:
        return None
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError('invalid numeric value') from exc
    if not math.isfinite(number):
        raise ValueError('invalid numeric value')
    return int(number) if number.is_integer() else number


def _parse_subject_grades(form):
    grades = {}
    for subject_key, _subject_label in SUBJECTS:
        subject_grades = {}
        for column_key, _column_label in GRADE_COLUMNS:
            field_name = f'grade__{subject_key}__{column_key}'
            value = form.get(field_name, '')
            subject_grades[column_key] = (
                (value or '').strip()
                if column_key == 'notes'
                else _parse_optional_number(value)
            )
        grades[subject_key] = subject_grades
    return grades


def _apply_record_fields(record, form, school):
    record.subject_grades = _parse_subject_grades(form)
    record.total_score = _parse_optional_number(form.get('total_score'))
    record.first_round_result = (form.get('first_round_result', '') or '').strip()
    record.second_round_result = (form.get('second_round_result', '') or '').strip()
    record.result_notes = (form.get('result_notes', '') or '').strip()
    record.page_number = (form.get('page_number', '') or '').strip()
    record.father_name = (form.get('father_name', '') or '').strip()
    record.grandfather_name = (form.get('grandfather_name', '') or '').strip()
    record.great_grandfather_name = (form.get('great_grandfather_name', '') or '').strip()
    record.years_failed = (form.get('years_failed', '') or '').strip()
    record.record_number = (form.get('record_number', '') or '').strip()
    record.snap_full_name = (form.get('snap_full_name', '') or '').strip()
    record.snap_student_number = (form.get('snap_student_number', '') or '').strip()
    record.snap_stage = (form.get('snap_stage', '') or '').strip()
    record.snap_grade_name = (form.get('snap_grade_name', '') or '').strip()
    record.snap_section_name = (form.get('snap_section_name', '') or '').strip()
    record.snap_gender = (form.get('snap_gender', '') or '').strip()
    record.snap_date_of_birth = _parse_date(form.get('snap_date_of_birth'))
    record.snap_phone = (form.get('snap_phone', '') or '').strip()
    record.snap_address = (form.get('snap_address', '') or '').strip()
    record.snap_status = (form.get('snap_status', 'active') or 'active').strip() or 'active'
    record.snap_enrollment_date = _parse_date(form.get('snap_enrollment_date'))
    record.snap_guardian_name = (form.get('snap_guardian_name', '') or '').strip()
    record.snap_guardian_phone = (form.get('snap_guardian_phone', '') or '').strip()
    record.snap_guardian_relation = (form.get('snap_guardian_relation', '') or '').strip()
    record.school_name = (form.get('school_name', school.school_name if school else '') or '').strip()
    record.school_name_ar = (form.get('school_name_ar', school.school_name_ar if school else '') or '').strip()
    record.previous_school = (form.get('previous_school', '') or '').strip()
    record.admission_date = _parse_date(form.get('admission_date'))
    record.notes = (form.get('notes', '') or '').strip()


@student_middle_records_bp.route('/')
@login_required
@permission_required('view_student_records')
def index():
    school = _school_or_404()
    q = request.args.get('q', '').strip()
    page = request.args.get('page', 1, type=int)
    stage = request.args.get('stage', '').strip()

    records = _query(school, q, stage).paginate(page=page, per_page=25, error_out=False)
    return render_template(
        'student_middle_records/index.html',
        records=records,
        q=q,
        stage=stage,
        school=school,
        stages=ALL_STAGES,
    )


@student_middle_records_bp.route('/new', methods=['GET', 'POST'])
@login_required
@any_permission_required('add_student', 'edit_student')
def new():
    school = _school_or_404()
    academic_year = school.current_year
    if not academic_year or academic_year.school_id != school.id:
        flash('لا توجد سنة دراسية حالية لهذه المدرسة.', 'danger')
        return redirect(url_for('student_middle_records.index'))

    if request.method == 'POST':
        student_id = request.form.get('student_id', type=int)
        if not student_id:
            flash('يرجى اختيار طالب أولاً.', 'danger')
            return redirect(url_for('student_middle_records.new'))

        student = (Student.query
               .options(joinedload(Student.section).joinedload(Section.grade))
               .filter_by(id=student_id, school_id=school.id)
               .first())
        if not student:
            flash('الطالب غير موجود أو لا ينتمي لهذه المدرسة.', 'danger')
            return redirect(url_for('student_middle_records.new'))

        existing = StudentMiddleRecord.query.filter_by(
            school_id=school.id,
            student_id=student_id,
            academic_year_id=academic_year.id,
        ).first()
        if existing:
            flash('يوجد سجل وسيط لهذا الطالب مسبقاً.', 'warning')
            return redirect(url_for('student_middle_records.view', record_id=existing.id))

        record = StudentMiddleRecord(
            school_id=school.id,
            student_id=student_id,
            academic_year_id=academic_year.id,
            snap_year_name=academic_year.name,
            created_by=current_user.id,
        )
        try:
            _apply_record_fields(record, request.form, school)
        except ValueError:
            flash('يرجى إدخال درجات رقمية صحيحة.', 'danger')
            return redirect(url_for('student_middle_records.new', student_id=student_id))
        db.session.add(record)
        db.session.commit()
        flash(f'تم إنشاء السجل الوسطي للطالب {record.snap_full_name} بنجاح.', 'success')
        return redirect(url_for('student_middle_records.view', record_id=record.id))

    q = request.args.get('q', '').strip()
    students = []
    if q:
        students = (
            Student.query
            .options(joinedload(Student.section).joinedload(Section.grade))
            .filter(Student.school_id == school.id)
            .filter(
                db.or_(
                    Student.full_name.ilike(f'%{q}%'),
                    Student.student_id.ilike(f'%{q}%'),
                )
            )
            .order_by(Student.full_name)
            .limit(20)
            .all()
        )

    prefill = {}
    sid = request.args.get('student_id', type=int)
    if sid:
        student = (Student.query
                   .options(joinedload(Student.section).joinedload(Section.grade))
                   .filter_by(id=sid, school_id=school.id)
                   .first())
        if student:
            prefill = _build_autofill(student, school, academic_year)
            prefill['student_id'] = student.id

    return render_template(
        'form.html',
        record=None,
        school=school,
        mode='new',
        students=students,
        q=q,
        prefill=prefill,
        subjects=SUBJECTS,
        grade_columns=GRADE_COLUMNS,
    )


@student_middle_records_bp.route('/<int:record_id>')
@login_required
@permission_required('view_student_records')
def view(record_id):
    school = _school_or_404()
    record = StudentMiddleRecord.query.filter_by(id=record_id, school_id=school.id).first_or_404()
    return render_template('view.html', record=record, school=school,
                           subjects=SUBJECTS, grade_columns=GRADE_COLUMNS)


@student_middle_records_bp.route('/<int:record_id>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('edit_student')
def edit(record_id):
    school = _school_or_404()
    record = StudentMiddleRecord.query.filter_by(id=record_id, school_id=school.id).first_or_404()

    if request.method == 'POST':
        try:
            _apply_record_fields(record, request.form, school)
        except ValueError:
            db.session.rollback()
            flash('يرجى إدخال درجات رقمية صحيحة.', 'danger')
            return redirect(url_for('student_middle_records.edit', record_id=record.id))
        record.updated_at = datetime.utcnow()
        db.session.commit()
        flash('تم تحديث السجل الوسطي بنجاح.', 'success')
        return redirect(url_for('student_middle_records.view', record_id=record.id))

    return render_template('form.html', record=record, school=school, mode='edit',
                           subjects=SUBJECTS, grade_columns=GRADE_COLUMNS)


@student_middle_records_bp.route('/<int:record_id>/print')
@login_required
@permission_required('view_student_records')
def print_record(record_id):
    school = _school_or_404()
    record = StudentMiddleRecord.query.filter_by(id=record_id, school_id=school.id).first_or_404()
    return render_template('print.html', record=record, school=school,
                           subjects=SUBJECTS, grade_columns=GRADE_COLUMNS)

"""Mecha-School — Student Registration Record (سجل قيد الطالب) Blueprint"""
import json
from datetime import datetime, date

from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, abort, jsonify, send_file)
from flask_login import login_required, current_user
from sqlalchemy.orm import joinedload
from io import BytesIO

from app.models import (db, Student, StudentRegistrationRecord, Section, Grade,
                        AcademicYear, School, parent_students, User)
from app.utils.decorators import (permission_required, get_current_school,
                                   admin_required)

student_records_bp = Blueprint(
    'student_records', __name__,
    template_folder='../../templates/student_records',
)


def _school_or_404():
    school = get_current_school()
    if not school:
        abort(404)
    return school


def _build_autofill(student: Student, school: School) -> dict:
    """Return a dict of all auto-fill values for the form."""
    section = student.section
    grade   = section.grade if section else None

    # Linked parent user (first found)
    parent_user = None
    parent_rel  = None
    if hasattr(student, 'parents'):
        row = (
            db.session.execute(
                db.select(User, parent_students.c.relation)
                .join(parent_students, User.id == parent_students.c.user_id)
                .where(parent_students.c.student_id == student.id)
            ).first()
        )
        if row:
            parent_user, parent_rel = row

    return {
        # Student
        'snap_full_name':       student.full_name,
        'snap_student_number':  student.student_id,
        'snap_gender':          student.gender or '',
        'snap_date_of_birth':   student.date_of_birth.isoformat() if student.date_of_birth else '',
        'snap_nationality':     student.nationality or '',
        'snap_address':         student.address or '',
        'snap_phone':           student.phone or '',
        'snap_status':          student.status or 'active',
        'snap_enrollment_date': student.enrollment_date.isoformat() if student.enrollment_date else '',
        # Guardian from student model fields
        'snap_guardian_name':     student.guardian_name or '',
        'snap_guardian_phone':    student.guardian_phone or '',
        'snap_guardian_email':    student.guardian_email or '',
        'snap_guardian_relation': student.guardian_relation or '',
        'snap_guardian_address':  '',
        # Override with linked parent user if available
        **(  # noqa: PIE800
            {
                'snap_guardian_name':     parent_user.full_name,
                'snap_guardian_phone':    parent_user.phone or student.guardian_phone or '',
                'snap_guardian_relation': parent_rel or student.guardian_relation or '',
            }
            if parent_user else {}
        ),
        # Academic placement
        'snap_school_name':    school.school_name,
        'snap_school_name_ar': school.school_name_ar or '',
        'snap_year_name':      student.academic_year.name if student.academic_year else '',
        'snap_grade_name':     grade.name if grade else '',
        'snap_stage':          grade.stage if grade else '',
        'snap_section_name':   section.name if section else '',
        # Admission defaults
        'admission_date': student.enrollment_date.isoformat() if student.enrollment_date else '',
    }


# ─── AJAX: student search ─────────────────────────────────────────────────────

@student_records_bp.route('/search-students')
@login_required
@admin_required
def search_students():
    school = _school_or_404()
    q = request.args.get('q', '').strip()
    if len(q) < 1:
        return jsonify([])

    query = (
        Student.query
        .options(joinedload(Student.section).joinedload(Section.grade))
        .filter(Student.school_id == school.id)
    )
    if q:
        query = query.filter(
            db.or_(
                Student.full_name.ilike(f'%{q}%'),
                Student.student_id.ilike(f'%{q}%'),
            )
        )
    students = query.order_by(Student.full_name).limit(20).all()

    results = []
    for s in students:
        section = s.section
        grade   = section.grade if section else None
        results.append({
            'id':             s.id,
            'full_name':      s.full_name,
            'student_number': s.student_id,
            'grade':          grade.name if grade else '',
            'section':        section.name if section else '',
            'has_record':     s.registration_record is not None,
            'record_id':      s.registration_record.id if s.registration_record else None,
        })
    return jsonify(results)


# ─── AJAX: get full student autofill data ────────────────────────────────────

@student_records_bp.route('/student-data/<int:student_id>')
@login_required
@admin_required
def get_student_data(student_id):
    school  = _school_or_404()
    student = (
        Student.query
        .options(joinedload(Student.section).joinedload(Section.grade),
                 joinedload(Student.academic_year))
        .filter_by(id=student_id, school_id=school.id)
        .first_or_404()
    )
    return jsonify(_build_autofill(student, school))


# ─── INDEX ────────────────────────────────────────────────────────────────────

@student_records_bp.route('/')
@login_required
@permission_required('view_students')
def index():
    school = _school_or_404()
    q      = request.args.get('q', '').strip()
    page   = request.args.get('page', 1, type=int)

    query = (
        StudentRegistrationRecord.query
        .options(joinedload(StudentRegistrationRecord.student)
                 .joinedload(Student.section)
                 .joinedload(Section.grade))
        .filter(StudentRegistrationRecord.school_id == school.id)
    )
    if q:
        query = query.join(Student).filter(
            db.or_(
                Student.full_name.ilike(f'%{q}%'),
                Student.student_id.ilike(f'%{q}%'),
                StudentRegistrationRecord.snap_full_name.ilike(f'%{q}%'),
                StudentRegistrationRecord.snap_student_number.ilike(f'%{q}%'),
            )
        )
    records = query.order_by(StudentRegistrationRecord.updated_at.desc()).paginate(
        page=page, per_page=25, error_out=False
    )
    return render_template('student_records/index.html',
                           records=records, q=q, school=school)


# ─── NEW ──────────────────────────────────────────────────────────────────────

@student_records_bp.route('/new', methods=['GET', 'POST'])
@login_required
@permission_required('add_student')
def new():
    school = _school_or_404()

    if request.method == 'POST':
        sid = request.form.get('student_id', type=int)
        if not sid:
            flash('يرجى اختيار طالب أولاً.', 'danger')
            return redirect(url_for('student_records.new'))

        student = Student.query.filter_by(id=sid, school_id=school.id).first()
        if not student:
            flash('الطالب غير موجود أو لا ينتمي لهذه المدرسة.', 'danger')
            return redirect(url_for('student_records.new'))

        # Duplicate guard
        existing = StudentRegistrationRecord.query.filter_by(
            school_id=school.id, student_id=sid
        ).first()
        if existing:
            flash('يوجد سجل قيد لهذا الطالب مسبقاً.', 'warning')
            return redirect(url_for('student_records.view', record_id=existing.id))

        # Parse academic history rows from form
        history = _parse_history_from_form(request.form)

        record = StudentRegistrationRecord(
            school_id=school.id,
            student_id=sid,
            created_by=current_user.id,
            # Student snapshot
            snap_full_name       = request.form.get('snap_full_name', '').strip(),
            snap_student_number  = request.form.get('snap_student_number', '').strip(),
            snap_gender          = request.form.get('snap_gender', '').strip(),
            snap_date_of_birth   = _parse_date(request.form.get('snap_date_of_birth')),
            snap_nationality     = request.form.get('snap_nationality', '').strip(),
            snap_address         = request.form.get('snap_address', '').strip(),
            snap_phone           = request.form.get('snap_phone', '').strip(),
            snap_status          = request.form.get('snap_status', 'active').strip(),
            snap_enrollment_date = _parse_date(request.form.get('snap_enrollment_date')),
            # Guardian snapshot
            snap_guardian_name     = request.form.get('snap_guardian_name', '').strip(),
            snap_guardian_phone    = request.form.get('snap_guardian_phone', '').strip(),
            snap_guardian_email    = request.form.get('snap_guardian_email', '').strip(),
            snap_guardian_relation = request.form.get('snap_guardian_relation', '').strip(),
            snap_guardian_address  = request.form.get('snap_guardian_address', '').strip(),
            # Placement snapshot
            snap_school_name    = request.form.get('snap_school_name', school.school_name).strip(),
            snap_school_name_ar = request.form.get('snap_school_name_ar', school.school_name_ar or '').strip(),
            snap_year_name      = request.form.get('snap_year_name', '').strip(),
            snap_grade_name     = request.form.get('snap_grade_name', '').strip(),
            snap_stage          = request.form.get('snap_stage', '').strip(),
            snap_section_name   = request.form.get('snap_section_name', '').strip(),
            # Admission
            admission_date  = _parse_date(request.form.get('admission_date')),
            document_number = request.form.get('document_number', '').strip(),
            previous_school = request.form.get('previous_school', '').strip(),
            transfer_reason = request.form.get('transfer_reason', '').strip(),
            admission_notes = request.form.get('admission_notes', '').strip(),
            # Documents
            has_birth_cert       = bool(request.form.get('has_birth_cert')),
            has_id_card          = bool(request.form.get('has_id_card')),
            has_prev_certificate = bool(request.form.get('has_prev_certificate')),
            has_photo            = bool(request.form.get('has_photo')),
            document_notes       = request.form.get('document_notes', '').strip(),
            # Notes & signatures
            general_notes    = request.form.get('general_notes', '').strip(),
            signature_admin  = request.form.get('signature_admin', '').strip(),
            signature_parent = request.form.get('signature_parent', '').strip(),
        )
        record.academic_history = history
        db.session.add(record)
        db.session.commit()
        flash(f'تم إنشاء سجل القيد للطالب {record.snap_full_name} بنجاح.', 'success')
        return redirect(url_for('student_records.view', record_id=record.id))

    # GET — pre-select student if given in query string
    prefill      = {}
    pre_student  = None
    has_record   = False
    existing_id  = None
    sid = request.args.get('student_id', type=int)
    if sid:
        pre_student = Student.query.filter_by(id=sid, school_id=school.id).first()
        if pre_student:
            existing = StudentRegistrationRecord.query.filter_by(
                school_id=school.id, student_id=sid
            ).first()
            if existing:
                has_record  = True
                existing_id = existing.id
            else:
                prefill = _build_autofill(pre_student, school)

    return render_template('student_records/new.html',
                           school=school, prefill=prefill,
                           pre_student=pre_student,
                           has_record=has_record, existing_id=existing_id)


# ─── VIEW ─────────────────────────────────────────────────────────────────────

@student_records_bp.route('/<int:record_id>')
@login_required
@permission_required('view_students')
def view(record_id):
    school = _school_or_404()
    record = StudentRegistrationRecord.query.filter_by(
        id=record_id, school_id=school.id
    ).first_or_404()
    return render_template('student_records/view.html',
                           record=record, school=school)


# ─── EDIT ─────────────────────────────────────────────────────────────────────

@student_records_bp.route('/<int:record_id>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('edit_student')
def edit(record_id):
    school = _school_or_404()
    record = StudentRegistrationRecord.query.filter_by(
        id=record_id, school_id=school.id
    ).first_or_404()

    if request.method == 'POST':
        history = _parse_history_from_form(request.form)

        record.snap_full_name       = request.form.get('snap_full_name', '').strip()
        record.snap_student_number  = request.form.get('snap_student_number', '').strip()
        record.snap_gender          = request.form.get('snap_gender', '').strip()
        record.snap_date_of_birth   = _parse_date(request.form.get('snap_date_of_birth'))
        record.snap_nationality     = request.form.get('snap_nationality', '').strip()
        record.snap_address         = request.form.get('snap_address', '').strip()
        record.snap_phone           = request.form.get('snap_phone', '').strip()
        record.snap_status          = request.form.get('snap_status', 'active').strip()
        record.snap_enrollment_date = _parse_date(request.form.get('snap_enrollment_date'))

        record.snap_guardian_name     = request.form.get('snap_guardian_name', '').strip()
        record.snap_guardian_phone    = request.form.get('snap_guardian_phone', '').strip()
        record.snap_guardian_email    = request.form.get('snap_guardian_email', '').strip()
        record.snap_guardian_relation = request.form.get('snap_guardian_relation', '').strip()
        record.snap_guardian_address  = request.form.get('snap_guardian_address', '').strip()

        record.snap_school_name    = request.form.get('snap_school_name', '').strip()
        record.snap_school_name_ar = request.form.get('snap_school_name_ar', '').strip()
        record.snap_year_name      = request.form.get('snap_year_name', '').strip()
        record.snap_grade_name     = request.form.get('snap_grade_name', '').strip()
        record.snap_stage          = request.form.get('snap_stage', '').strip()
        record.snap_section_name   = request.form.get('snap_section_name', '').strip()

        record.admission_date  = _parse_date(request.form.get('admission_date'))
        record.document_number = request.form.get('document_number', '').strip()
        record.previous_school = request.form.get('previous_school', '').strip()
        record.transfer_reason = request.form.get('transfer_reason', '').strip()
        record.admission_notes = request.form.get('admission_notes', '').strip()

        record.has_birth_cert       = bool(request.form.get('has_birth_cert'))
        record.has_id_card          = bool(request.form.get('has_id_card'))
        record.has_prev_certificate = bool(request.form.get('has_prev_certificate'))
        record.has_photo            = bool(request.form.get('has_photo'))
        record.document_notes       = request.form.get('document_notes', '').strip()

        record.academic_history = history

        record.general_notes    = request.form.get('general_notes', '').strip()
        record.signature_admin  = request.form.get('signature_admin', '').strip()
        record.signature_parent = request.form.get('signature_parent', '').strip()

        record.updated_at = datetime.utcnow()
        db.session.commit()
        flash('تم تحديث سجل القيد بنجاح.', 'success')
        return redirect(url_for('student_records.view', record_id=record.id))

    return render_template('student_records/edit.html',
                           record=record, school=school)


# ─── PDF ──────────────────────────────────────────────────────────────────────

@student_records_bp.route('/<int:record_id>/pdf')
@login_required
@permission_required('view_students')
def pdf(record_id):
    school = _school_or_404()
    record = StudentRegistrationRecord.query.filter_by(
        id=record_id, school_id=school.id
    ).first_or_404()

    from app.utils.pdf_gen import generate_registration_record_pdf
    pdf_bytes = generate_registration_record_pdf(record, school)
    if not pdf_bytes:
        flash('تعذّر إنشاء ملف PDF — تحقق من توفر مكتبة ReportLab والخط العربي.', 'danger')
        return redirect(url_for('student_records.view', record_id=record.id))

    fname = f'سجل_قيد_{record.snap_student_number or record.id}.pdf'
    buf   = BytesIO(pdf_bytes)
    return send_file(buf, mimetype='application/pdf',
                     as_attachment=False,
                     download_name=fname)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_date(val):
    if not val:
        return None
    try:
        return date.fromisoformat(val.strip())
    except (ValueError, AttributeError):
        return None


def _parse_history_from_form(form) -> list:
    """Extract repeating academic history rows from multidict form."""
    years    = form.getlist('history_year')
    grades   = form.getlist('history_grade')
    sections = form.getlist('history_section')
    results  = form.getlist('history_result')
    gpas     = form.getlist('history_gpa')
    rounds   = form.getlist('history_round')
    statuses = form.getlist('history_status')
    notes    = form.getlist('history_notes')

    history = []
    for i in range(len(years)):
        row = {
            'year':    years[i]    if i < len(years)    else '',
            'grade':   grades[i]   if i < len(grades)   else '',
            'section': sections[i] if i < len(sections) else '',
            'result':  results[i]  if i < len(results)  else '',
            'gpa':     gpas[i]     if i < len(gpas)     else '',
            'round':   rounds[i]   if i < len(rounds)   else '',
            'status':  statuses[i] if i < len(statuses) else '',
            'notes':   notes[i]    if i < len(notes)    else '',
        }
        # Skip fully empty rows
        if any(v.strip() for v in row.values()):
            history.append(row)
    return history

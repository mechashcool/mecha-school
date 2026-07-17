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
from app.utils.decorators import (permission_required, any_permission_required,
                                   get_current_school, admin_required)

student_records_bp = Blueprint(
    'student_records', __name__,
    template_folder='../../templates/student_records',
)

# Standard subjects in the official registration record
SUBJECTS = [
    'التربية الإسلامية',
    'اللغة العربية',
    'اللغة الإنكليزية',
    'الرياضيات',
    'العلوم',
    'الاجتماعيات',
    'التربية الفنية والنشيد',
    'التربية الرياضية',
]

# Year labels for accordion display (9 school years)
YEAR_LABELS = [
    'الأول', 'الثاني', 'الثالث', 'الرابع', 'الخامس',
    'السادس', 'السابع', 'الثامن', 'التاسع',
]


def _school_or_404():
    school = get_current_school()
    if not school:
        abort(404)
    return school


def _records_query(school, q):
    """School-scoped registration-records query, optionally filtered by the same
    name/number search used on the index page. Ordered newest-updated first.
    Shared by the index list and the bulk export routes so they stay consistent.
    """
    query = StudentRegistrationRecord.query.filter(
        StudentRegistrationRecord.school_id == school.id
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
    return query.order_by(StudentRegistrationRecord.updated_at.desc())


def _build_autofill(student: Student, school: School) -> dict:
    """Return a dict of all auto-fill values for the form."""
    section = student.section
    grade   = section.grade if section else None

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
        # Guardian
        'snap_guardian_name':     student.guardian_name or '',
        'snap_guardian_phone':    student.guardian_phone or '',
        'snap_guardian_email':    student.guardian_email or '',
        'snap_guardian_relation': student.guardian_relation or '',
        'snap_guardian_address':  '',
        **(
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
        # Admission
        'admission_date': student.enrollment_date.isoformat() if student.enrollment_date else '',
        # Extra fields (mostly blank — entered manually on the official form)
        'general_registry':   '',
        'record_number':      '',
        'father_name':        '',
        'father_house_num':   '',
        'father_mahalla':     '',
        'father_occupation':  '',
        'guardian_house_num': '',
        'guardian_mahalla':   '',
        'civil_registry_num': '',
        'birth_place':        '',
        'religion':           '',
        'departure_date':     '',
        'departure_reason':   '',
    }


def _parse_date(val):
    if not val:
        return None
    try:
        return date.fromisoformat(val.strip())
    except (ValueError, AttributeError):
        return None


def _parse_extra_fields(form) -> dict:
    """Extract the extra official-form fields from POST data."""
    keys = [
        'general_registry', 'record_number',
        'father_name', 'father_house_num', 'father_mahalla',
        'father_occupation', 'guardian_house_num', 'guardian_mahalla',
        'civil_registry_num', 'birth_place', 'religion',
        'departure_date', 'departure_reason',
    ]
    return {k: form.get(k, '').strip() for k in keys}


def _parse_grades_from_form(form) -> dict:
    """Extract the subject×year grade grid from POST form data.
    Always produces exactly 9 entries (one per slot) so the PDF can map
    slot index directly to physical column position.
    Returns: {"years": [...9 items...]} stored in academic_history_json.
    """
    years = []
    for i in range(9):
        row = {
            'class': form.get(f'y{i}_class', '').strip(),
            'year':  form.get(f'y{i}_year',  '').strip(),
        }
        for j in range(len(SUBJECTS)):
            row[f's{j}_n'] = form.get(f'y{i}_s{j}_n', '').strip()
            row[f's{j}_t'] = form.get(f'y{i}_s{j}_t', '').strip()

        row['extra'] = [
            {
                'name': form.get(f'y{i}_ex{k}_name', '').strip(),
                'n':    form.get(f'y{i}_ex{k}_n',    '').strip(),
                't':    form.get(f'y{i}_ex{k}_t',    '').strip(),
            }
            for k in range(3)
        ]
        for fld in ['total_n', 'total_t', 'behavior', 'result',
                    'notes_results', 'final_result', 'final_result_t',
                    'principal_sig', 'col_notes']:
            row[fld] = form.get(f'y{i}_{fld}', '').strip()

        years.append(row)  # Always keep all 9 slots — preserves slot↔PDF-column mapping

    return {'years': years}


def _apply_record_fields(record, form, school):
    """Apply all form fields to a record object (new or existing)."""
    record.snap_full_name       = form.get('snap_full_name', '').strip()
    record.snap_student_number  = form.get('snap_student_number', '').strip()
    record.snap_gender          = form.get('snap_gender', '').strip()
    record.snap_date_of_birth   = _parse_date(form.get('snap_date_of_birth'))
    record.snap_nationality     = form.get('snap_nationality', '').strip()
    record.snap_address         = form.get('snap_address', '').strip()
    record.snap_phone           = form.get('snap_phone', '').strip()
    record.snap_status          = form.get('snap_status', 'active').strip()
    record.snap_enrollment_date = _parse_date(form.get('snap_enrollment_date'))

    record.snap_guardian_name     = form.get('snap_guardian_name', '').strip()
    record.snap_guardian_phone    = form.get('snap_guardian_phone', '').strip()
    record.snap_guardian_email    = form.get('snap_guardian_email', '').strip()
    record.snap_guardian_relation = form.get('snap_guardian_relation', '').strip()
    record.snap_guardian_address  = form.get('snap_guardian_address', '').strip()

    record.snap_school_name    = form.get('snap_school_name',
                                          school.school_name if school else '').strip()
    record.snap_school_name_ar = form.get('snap_school_name_ar',
                                          school.school_name_ar or '' if school else '').strip()
    record.snap_year_name      = form.get('snap_year_name', '').strip()
    record.snap_grade_name     = form.get('snap_grade_name', '').strip()
    record.snap_stage          = form.get('snap_stage', '').strip()
    record.snap_section_name   = form.get('snap_section_name', '').strip()

    record.admission_date  = _parse_date(form.get('admission_date'))
    record.document_number = form.get('document_number', '').strip()
    record.previous_school = form.get('previous_school', '').strip()
    record.transfer_reason = form.get('transfer_reason', '').strip()
    record.admission_notes = form.get('admission_notes', '').strip()

    record.has_birth_cert       = bool(form.get('has_birth_cert'))
    record.has_id_card          = bool(form.get('has_id_card'))
    record.has_prev_certificate = bool(form.get('has_prev_certificate'))
    record.has_photo            = bool(form.get('has_photo'))
    record.document_notes       = form.get('document_notes', '').strip()

    record.general_notes    = form.get('general_notes', '').strip()
    record.signature_admin  = form.get('signature_admin', '').strip()
    record.signature_parent = form.get('signature_parent', '').strip()

    record.academic_history = _parse_grades_from_form(form)
    record.extra_fields     = _parse_extra_fields(form)


# ─── AJAX: student search ─────────────────────────────────────────────────────

@student_records_bp.route('/search-students')
@login_required
@any_permission_required('add_student', 'edit_student')
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
@any_permission_required('add_student', 'edit_student')
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


# ─── AJAX: records live search ───────────────────────────────────────────────

@student_records_bp.route('/search')
@login_required
@permission_required('view_students')
def search():
    school = _school_or_404()
    q = request.args.get('q', '').strip()

    query = StudentRegistrationRecord.query.filter(
        StudentRegistrationRecord.school_id == school.id
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
    rows = query.order_by(StudentRegistrationRecord.updated_at.desc()).limit(50).all()

    def _gs(r):
        if r.snap_grade_name:
            return (f"{r.snap_grade_name} / {r.snap_section_name}"
                    if r.snap_section_name else r.snap_grade_name)
        return '—'

    return jsonify({
        'ok': True,
        'records': [{
            'student_name':   r.snap_full_name or '',
            'student_number': r.snap_student_number or '',
            'grade_section':  _gs(r),
            'academic_year':  r.snap_year_name or '—',
            'created_at':     r.created_at.strftime('%Y-%m-%d') if r.created_at else '',
            'updated_at':     r.updated_at.strftime('%Y-%m-%d') if r.updated_at else '',
            'view_url':    url_for('student_records.view', record_id=r.id),
            'edit_url':    url_for('student_records.edit', record_id=r.id),
            'pdf_url_a3':  url_for('student_records.pdf',  record_id=r.id, paper='a3'),
            'pdf_url_a4':  url_for('student_records.pdf',  record_id=r.id, paper='a4'),
        } for r in rows],
    })


# ─── INDEX ────────────────────────────────────────────────────────────────────

@student_records_bp.route('/')
@login_required
@permission_required('view_students')
def index():
    school = _school_or_404()
    q      = request.args.get('q', '').strip()
    page   = request.args.get('page', 1, type=int)

    records = _records_query(school, q).paginate(
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

        existing = StudentRegistrationRecord.query.filter_by(
            school_id=school.id, student_id=sid
        ).first()
        if existing:
            flash('يوجد سجل قيد لهذا الطالب مسبقاً.', 'warning')
            return redirect(url_for('student_records.view', record_id=existing.id))

        record = StudentRegistrationRecord(
            school_id=school.id,
            student_id=sid,
            created_by=current_user.id,
        )
        _apply_record_fields(record, request.form, school)
        db.session.add(record)
        db.session.commit()
        flash(f'تم إنشاء سجل القيد للطالب {record.snap_full_name} بنجاح.', 'success')
        return redirect(url_for('student_records.view', record_id=record.id))

    # GET
    prefill     = {}
    pre_student = None
    has_record  = False
    existing_id = None
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
                           has_record=has_record, existing_id=existing_id,
                           subjects=SUBJECTS, year_labels=YEAR_LABELS)


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
                           record=record, school=school,
                           subjects=SUBJECTS, year_labels=YEAR_LABELS)


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
        _apply_record_fields(record, request.form, school)
        record.updated_at = datetime.utcnow()
        db.session.commit()
        flash('تم تحديث سجل القيد بنجاح.', 'success')
        return redirect(url_for('student_records.view', record_id=record.id))

    return render_template('student_records/edit.html',
                           record=record, school=school,
                           subjects=SUBJECTS, year_labels=YEAR_LABELS)


# ─── PDF ──────────────────────────────────────────────────────────────────────

@student_records_bp.route('/<int:record_id>/pdf')
@login_required
@permission_required('view_students')
def pdf(record_id):
    school = _school_or_404()
    record = StudentRegistrationRecord.query.filter_by(
        id=record_id, school_id=school.id
    ).first_or_404()

    paper = request.args.get('paper', 'a3').lower()
    if paper not in ('a3', 'a4'):
        paper = 'a3'

    from app.utils.pdf_gen import generate_registration_record_pdf
    pdf_bytes = generate_registration_record_pdf(record, school, paper=paper)
    if not pdf_bytes:
        flash('تعذّر إنشاء ملف PDF — تحقق من توفر مكتبة ReportLab والخط العربي.', 'danger')
        return redirect(url_for('student_records.view', record_id=record.id))

    fname = f'سجل_قيد_{record.snap_student_number or record.id}_{paper.upper()}.pdf'
    buf   = BytesIO(pdf_bytes)
    return send_file(buf, mimetype='application/pdf',
                     as_attachment=False,
                     download_name=fname)


# ─── BULK EXPORT: PDF (all records, one per page) ─────────────────────────────

@student_records_bp.route('/export/pdf')
@login_required
@permission_required('view_students')
def export_pdf():
    school = _school_or_404()
    q      = request.args.get('q', '').strip()

    paper = request.args.get('paper', 'a4').lower()
    if paper not in ('a3', 'a4'):
        paper = 'a4'

    records = _records_query(school, q).all()
    if not records:
        flash('لا توجد سجلات قيد للتصدير.', 'warning')
        return redirect(url_for('student_records.index', q=q or None))

    from app.utils.pdf_gen import generate_registration_records_bulk_pdf
    pdf_bytes = generate_registration_records_bulk_pdf(records, school, paper=paper)
    if not pdf_bytes:
        flash('تعذّر إنشاء ملف PDF — تحقق من توفر مكتبة ReportLab والخط العربي.', 'danger')
        return redirect(url_for('student_records.index', q=q or None))

    fname = f'سجلات_القيد_{paper.upper()}.pdf'
    return send_file(BytesIO(pdf_bytes), mimetype='application/pdf',
                     as_attachment=True, download_name=fname)


# ─── BULK EXPORT: Excel (all records, one row each) ───────────────────────────

@student_records_bp.route('/export/excel')
@login_required
@permission_required('view_students')
def export_excel():
    school = _school_or_404()
    q      = request.args.get('q', '').strip()

    records = _records_query(school, q).all()
    if not records:
        flash('لا توجد سجلات قيد للتصدير.', 'warning')
        return redirect(url_for('student_records.index', q=q or None))

    from app.utils.excel_export import export_registration_records
    xlsx_bytes = export_registration_records(records)
    if not xlsx_bytes:
        flash('تعذّر إنشاء ملف Excel — تحقق من توفر مكتبة openpyxl.', 'danger')
        return redirect(url_for('student_records.index', q=q or None))

    return send_file(
        BytesIO(xlsx_bytes),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True, download_name='سجلات_القيد.xlsx')

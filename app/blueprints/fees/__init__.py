"""Mecha-School – Fees Blueprint"""
import logging
from decimal import Decimal, InvalidOperation

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, session as flask_session, abort
from flask_login import login_required, current_user
from datetime import date, datetime as dt
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.models import (db, FeeRecord, FeeInstallment, FeeType, Student, AcademicYear, SchoolSettings, Revenue, RevenueCategory, Section, Grade, School)
from app.utils.decorators import (permission_required, get_current_school,
                                   get_active_year, get_view_year, historical_guard,
                                   admin_required)
from app.utils.helpers import generate_receipt_no
from app.utils.audit import log_action
from app.utils.buildings import (
    apply_building_scope_to_students, apply_building_scope_to_fees,
    user_can_access_student,
)

fees_bp = Blueprint('fees', __name__, template_folder='../../templates/fees')
_log = logging.getLogger('mecha.fees')


def _notify_fee_parents(student_id, title, body, screen, *, fee_record_id=None,
                        installment_id=None):
    """FCM push to a student's linked parents after a fee/payment change.

    Called only after the DB transaction has committed. Parents are resolved
    server-side from the student→parent link (school isolation inherited); no
    client-supplied identifier is trusted. The body carries no amounts beyond
    what the caller passes. Never raises — push must not fail a payment.
    """
    if not student_id:
        return
    data = {
        'type':   'fee',
        'screen': screen,            # 'fees'
        'route':  '/parent/fees',
        'student_id': str(student_id),
    }
    if fee_record_id is not None:
        data['fee_record_id'] = str(fee_record_id)
    if installment_id is not None:
        data['installment_id'] = str(installment_id)
    try:
        from app.services.notifications import NotificationService
        NotificationService.send_to_parents_of_student(
            student_id, title, body, ntype='fee', data=data)
    except Exception:
        _log.exception('[fees] FCM push failed for student_id=%s', student_id)


def _active_fee_student_query(school, year):
    query = (
        Student.query
        .options(joinedload(Student.section).joinedload(Section.grade))
        .filter_by(status='active')
    )
    if school:
        query = query.filter(Student.school_id == school.id)
    if year:
        query = query.filter(Student.academic_year_id == year.id)
    # Building scope — restricted users only act on their buildings' students.
    query = apply_building_scope_to_students(query, current_user, school)
    return query


def _student_payload(student):
    section = student.section
    grade = section.grade if section else None
    return {
        'id': student.id,
        'student_id': student.student_id,
        'full_name': student.full_name,
        'grade': grade.name if grade else '',
        'section': section.name if section else '',
    }


class FeeValidationError(ValueError):
    """Raised when fee form data fails validation.

    Carries a user-safe Arabic message suitable for flashing back to the form.
    """


def compute_fee_amounts(form):
    """Validate and compute (total_amount, discount, net_amount) from form data.

    Single source of truth for the fee discount rules, shared by the standalone
    `fees.create` route and the student-registration wizard so the calculation
    is never duplicated. Performs no database access and creates no records.
    Raises :class:`FeeValidationError` with an Arabic message on invalid input.
    """
    try:
        total_amount = Decimal(form.get('total_amount', '0') or '0')
    except InvalidOperation:
        raise FeeValidationError('المبلغ الإجمالي غير صالح.')

    discount_type = form.get('discount_type', 'fixed')
    try:
        discount_value = Decimal(form.get('discount_value', '0') or '0')
    except InvalidOperation:
        raise FeeValidationError('قيمة الخصم غير صالحة.')

    if discount_type == 'percentage':
        if discount_value < 0 or discount_value > 100:
            raise FeeValidationError('نسبة الخصم يجب أن تكون بين 0 و100.')
        discount = (total_amount * discount_value / Decimal('100')).quantize(Decimal('0.01'))
    else:
        discount = discount_value
        if discount < 0:
            raise FeeValidationError('الخصم لا يمكن أن يكون سالباً.')
        if discount > total_amount:
            raise FeeValidationError('الخصم لا يمكن أن يتجاوز المبلغ الإجمالي.')

    net_amount = total_amount - discount
    if net_amount < 0:
        raise FeeValidationError('الصافي لا يمكن أن يكون سالباً.')

    return total_amount, discount, net_amount


def persist_fee_record(form, *, school, student_id, fee_type_id, academic_year_id,
                       total_amount, discount, notes=None):
    """Create a FeeRecord and its FeeInstallments from already-computed amounts.

    Shared by `fees.create` and the student-registration wizard. Adds objects to
    the current ``db.session`` and flushes; the CALLER is responsible for the
    final commit and for all authorization (``manage_fees``) and ownership /
    duplicate checks. Installment splitting matches the standalone fee route
    exactly. ``notes`` lets a caller (the wizard) supply a non-colliding field
    name; when ``None`` the standard ``notes`` form field is used.
    """
    record = FeeRecord(
        student_id       = student_id,
        fee_type_id      = fee_type_id,
        academic_year_id = academic_year_id,
        school_id        = school.id if school else None,
        total_amount     = float(total_amount),
        discount         = float(discount),   # always stored as final monetary amount
        notes            = (notes if notes is not None else form.get('notes', '')).strip(),
    )
    db.session.add(record)
    db.session.flush()

    # Build installments from the server-computed net amount.
    num_inst = max(1, min(12, int(form.get('num_installments', 1) or 1)))
    net      = record.net_amount          # uses model property: total_amount - discount
    each     = round(net / num_inst, 2)
    for i in range(1, num_inst + 1):
        due_str = form.get(f'due_date_{i}')
        due     = dt.strptime(due_str, '%Y-%m-%d').date() if due_str else date.today()
        inst = FeeInstallment(
            fee_record_id    = record.id,
            school_id        = record.school_id,
            academic_year_id = record.academic_year_id,
            installment_no   = i,
            amount           = each,
            due_date         = due,
        )
        db.session.add(inst)
    return record


@fees_bp.route('/')
@login_required
@permission_required('manage_fees')
def index():
    page      = request.args.get('page', 1, type=int)
    search    = request.args.get('q', '')
    fee_type_filter = request.args.get('fee_type', 'all')
    payment_status = request.args.get('payment_status', 'all')
    installment_filter = request.args.get('installment', 'all')
    
    # Subquery for total paid
    total_paid_sub = db.session.query(
        FeeInstallment.fee_record_id,
        func.sum(FeeInstallment.received_amount).label('total_paid')
    ).group_by(FeeInstallment.fee_record_id).subquery()
    
    school = get_current_school()
    year   = get_view_year(school.id) if school else None

    query = FeeRecord.query.join(Student).outerjoin(
        total_paid_sub, FeeRecord.id == total_paid_sub.c.fee_record_id
    )

    # School scoping
    if school:
        query = query.filter(FeeRecord.school_id == school.id)

    # Building scope — restricted users only see fees of their buildings' students.
    query = apply_building_scope_to_fees(query, current_user, school)

    if search:
        query = query.filter(Student.full_name.ilike(f'%{search}%') |
                             Student.student_id.ilike(f'%{search}%'))

    if fee_type_filter != 'all':
        query = query.filter(FeeRecord.fee_type_id == int(fee_type_filter))
    
    if installment_filter != 'all':
        query = query.join(FeeInstallment).filter(FeeInstallment.installment_no == int(installment_filter))
    
    # Calculate remaining: net_amount - total_paid
    net_amount_expr = FeeRecord.total_amount - func.coalesce(FeeRecord.discount, 0)
    remaining_expr = net_amount_expr - func.coalesce(total_paid_sub.c.total_paid, 0)
    
    if payment_status == 'paid':
        query = query.filter(remaining_expr <= 0)
    elif payment_status == 'unpaid':
        query = query.filter(remaining_expr > 0)
    
    records   = query.order_by(FeeRecord.created_at.desc())\
                     .paginate(page=page, per_page=20, error_out=False)

    # Batch-fetch installments for the current page to avoid N+1 queries.
    _page_ids = [r.id for r in records.items]
    _page_inst = []
    if _page_ids:
        _page_inst = (
            FeeInstallment.query
            .filter(FeeInstallment.fee_record_id.in_(_page_ids))
            .order_by(FeeInstallment.installment_no)
            .all()
        )
    _inst_map = {}
    for _i in _page_inst:
        _inst_map.setdefault(_i.fee_record_id, []).append(_i)
    fee_entries = [(r, _inst_map.get(r.id, [])) for r in records.items]

    fee_types = FeeType.query.all()
    years_q   = AcademicYear.query
    if school:
        years_q = years_q.filter_by(school_id=school.id)
    years = years_q.order_by(AcademicYear.start_date.desc()).all()
    return render_template('fees/index.html',
                           records=records, fee_entries=fee_entries,
                           fee_types=fee_types,
                           years=years, search=search,
                           fee_type_filter=fee_type_filter,
                           payment_status=payment_status,
                           installment_filter=installment_filter)


@fees_bp.route('/create', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_fees')
def create():
    school    = get_current_school()
    year      = get_active_year(school.id) if school else None
    if not school or not year:
        flash('Select a school with an active academic year before creating fee records.', 'danger')
        return redirect(url_for('fees.index'))
    fee_types = FeeType.query.all()
    years_q   = AcademicYear.query
    if school:
        years_q = years_q.filter_by(school_id=school.id)
    years = years_q.order_by(AcademicYear.start_date.desc()).all()

    if request.method == 'POST':
        selected_student_id = request.form.get('student_id', type=int)
        selected_fee_type_id = request.form.get('fee_type_id', type=int)
        selected_year_id = request.form.get('academic_year_id', type=int) or (year.id if year else None)

        selected_student = (
            _active_fee_student_query(school, year)
            .filter(Student.id == selected_student_id)
            .first()
        ) if selected_student_id else None

        if not selected_student:
            flash('يرجى اختيار طالب من طلاب المدرسة والسنة الدراسية الحالية.', 'danger')
            return render_template('fees/form.html',
                                   fee_types=fee_types, years=years), 400

        # Section ownership validation (only when cascade filters were used, not in locked-student mode).
        submitted_section_id = request.form.get('section_id', type=int)
        if submitted_section_id:
            valid_section = Section.query.filter_by(
                id=submitted_section_id,
                school_id=school.id,
                academic_year_id=year.id,
            ).first()
            if not valid_section:
                flash('الشعبة المحددة غير صالحة أو لا تنتمي لهذه المدرسة والسنة الدراسية.', 'danger')
                return render_template('fees/form.html',
                                       fee_types=fee_types, years=years), 400
            if selected_student.section_id != submitted_section_id:
                flash('الطالب لا ينتمي إلى الشعبة المحددة.', 'danger')
                return render_template('fees/form.html',
                                       fee_types=fee_types, years=years), 400

        if FeeRecord.query.filter_by(student_id=selected_student_id,
                                     fee_type_id=selected_fee_type_id,
                                     academic_year_id=selected_year_id).first():
            flash('A fee record for this student, fee type, and academic year already exists.', 'danger')
            return render_template('fees/form.html',
                                   fee_types=fee_types, years=years,
                                   selected_student=selected_student)

        # ── Discount + record + installments (shared with the student wizard) ─
        try:
            total_amount, discount, _net = compute_fee_amounts(request.form)
            persist_fee_record(
                request.form,
                school=school,
                student_id=selected_student_id,
                fee_type_id=selected_fee_type_id,
                academic_year_id=selected_year_id,
                total_amount=total_amount,
                discount=discount,
            )
        except FeeValidationError as exc:
            flash(str(exc), 'danger')
            return render_template('fees/form.html',
                                   fee_types=fee_types, years=years,
                                   selected_student=selected_student), 400

        db.session.commit()
        # Push after the fee record + installments are committed.
        _notify_fee_parents(
            selected_student.id,
            'سجل رسوم جديد',
            f'تم إضافة سجل رسوم جديد للطالب {selected_student.full_name}.',
            screen='fees',
        )
        flash('تم إنشاء سجل الرسوم بنجاح.', 'success')
        return redirect(url_for('fees.index'))

    # Preselect a student when arriving from the student creation flow.
    # The student_id is validated server-side against school + active year.
    prefill_id = request.args.get('student_id', type=int)
    prefilled_student = None
    if prefill_id:
        prefilled_student = (
            _active_fee_student_query(school, year)
            .filter(Student.id == prefill_id)
            .first()
        )

    return render_template('fees/form.html',
                           fee_types=fee_types, years=years,
                           selected_student=prefilled_student,
                           locked_student=prefilled_student is not None)


@fees_bp.route('/students/search')
@login_required
@permission_required('manage_fees')
def search_students():
    school = get_current_school()
    year = get_active_year(school.id) if school else None
    if not school or not year:
        return jsonify({'results': []})

    term = request.args.get('q', '').strip()
    section_id = request.args.get('section_id', type=int)

    # Require at least 2 chars for open search; allow empty term only when loading
    # all students for a specific section dropdown (section_id must still be validated).
    if len(term) < 2 and not section_id:
        return jsonify({'results': []})

    query = _active_fee_student_query(school, year)

    if section_id:
        # Verify section belongs to this school + active year before applying filter.
        valid_section = Section.query.filter_by(
            id=section_id, school_id=school.id, academic_year_id=year.id
        ).first()
        if not valid_section:
            return jsonify({'results': []})
        query = query.filter(Student.section_id == section_id)

    if term:
        query = query.filter(
            Student.full_name.ilike(f'%{term}%') |
            Student.student_id.ilike(f'%{term}%')
        )

    # When loading all students in a section (no term), allow up to 200;
    # for name/code search, cap at 20 results.
    limit = 200 if (section_id and not term) else 20
    students = query.order_by(Student.full_name).limit(limit).all()
    return jsonify({'results': [_student_payload(student) for student in students]})


@fees_bp.route('/students/search-statement')
@login_required
@permission_required('manage_fees')
def search_students_statement():
    """Student search for the financial statement selector.

    Searches all students of the school regardless of academic year or active
    status, because the statement report spans multiple years and may include
    historical or transferred students. School and building isolation are still
    applied server-side.
    """
    school = get_current_school()
    if not school:
        return jsonify({'results': []})

    term = request.args.get('q', '').strip()
    if len(term) < 2:
        return jsonify({'results': []})

    query = (
        Student.query
        .execution_options(include_all_years=True)
        .options(joinedload(Student.section).joinedload(Section.grade))
        .filter(Student.school_id == school.id)
        .filter(
            Student.full_name.ilike(f'%{term}%') |
            Student.student_id.ilike(f'%{term}%')
        )
    )
    query = apply_building_scope_to_students(query, current_user, school)
    students = query.order_by(Student.full_name).limit(20).all()
    return jsonify({'results': [_student_payload(student) for student in students]})


@fees_bp.route('/api/stages')
@login_required
@permission_required('manage_fees')
def api_stages():
    """Distinct stage values for the current school and active year (for cascading dropdown)."""
    school = get_current_school()
    year = get_active_year(school.id) if school else None
    if not school or not year:
        return jsonify([])
    rows = (
        db.session.query(Grade.stage)
        .filter(
            Grade.school_id == school.id,
            Grade.academic_year_id == year.id,
            Grade.stage.isnot(None),
            Grade.stage != '',
        )
        .distinct()
        .order_by(Grade.stage)
        .all()
    )
    return jsonify([{'value': r[0], 'label': r[0]} for r in rows])


@fees_bp.route('/api/grades')
@login_required
@permission_required('manage_fees')
def api_grades():
    """Grades for a given stage, scoped to the current school and active year."""
    school = get_current_school()
    year = get_active_year(school.id) if school else None
    stage = request.args.get('stage', '').strip()
    if not school or not year or not stage:
        return jsonify([])
    grades = (
        Grade.query
        .filter_by(school_id=school.id, academic_year_id=year.id, stage=stage)
        .order_by(Grade.name)
        .all()
    )
    return jsonify([{'id': g.id, 'name': g.name} for g in grades])


@fees_bp.route('/api/sections')
@login_required
@permission_required('manage_fees')
def api_sections():
    """Sections for a given grade_id, scoped to the current school and active year."""
    school = get_current_school()
    year = get_active_year(school.id) if school else None
    grade_id = request.args.get('grade_id', type=int)
    if not school or not year or not grade_id:
        return jsonify([])
    grade = Grade.query.filter_by(id=grade_id, school_id=school.id,
                                  academic_year_id=year.id).first()
    if not grade:
        return jsonify([])
    sections = (
        Section.query
        .filter_by(grade_id=grade_id, school_id=school.id, academic_year_id=year.id)
        .order_by(Section.name)
        .all()
    )
    return jsonify([{'id': s.id, 'name': s.name} for s in sections])


@fees_bp.route('/api/students-bulk')
@login_required
@permission_required('manage_fees')
def api_students_bulk():
    """Students for bulk fee selection — filter by grade/section or name/code search.

    Scoped to the current school, active year, and building-level access.
    Returns up to 500 students when filtered by section/grade; up to 50 for open search.
    """
    school = get_current_school()
    year = get_active_year(school.id) if school else None
    if not school or not year:
        return jsonify({'results': [], 'total': 0})

    grade_id = request.args.get('grade_id', type=int)
    section_id = request.args.get('section_id', type=int)
    q = request.args.get('q', '').strip()

    if not grade_id and not section_id and len(q) < 2:
        return jsonify({'results': [], 'total': 0})

    query = _active_fee_student_query(school, year)

    if section_id:
        valid_section = Section.query.filter_by(
            id=section_id, school_id=school.id, academic_year_id=year.id
        ).first()
        if not valid_section:
            return jsonify({'results': [], 'total': 0})
        query = query.filter(Student.section_id == section_id)
    elif grade_id:
        valid_grade = Grade.query.filter_by(
            id=grade_id, school_id=school.id, academic_year_id=year.id
        ).first()
        if not valid_grade:
            return jsonify({'results': [], 'total': 0})
        # Resolve grade → section IDs to avoid a cross-join with the eager joinedload.
        section_ids = [
            row.id for row in
            Section.query
            .filter_by(grade_id=grade_id, school_id=school.id, academic_year_id=year.id)
            .with_entities(Section.id)
            .all()
        ]
        if not section_ids:
            return jsonify({'results': [], 'total': 0})
        query = query.filter(Student.section_id.in_(section_ids))

    if q:
        query = query.filter(
            Student.full_name.ilike(f'%{q}%') |
            Student.student_id.ilike(f'%{q}%')
        )

    limit = 500 if (section_id or grade_id) else 50
    students = query.order_by(Student.full_name).limit(limit).all()
    return jsonify({
        'results': [_student_payload(s) for s in students],
        'total': len(students),
    })


@fees_bp.route('/bulk-create', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_fees')
def bulk_create():
    """Bulk fee assignment — create the same fee record for multiple students at once.

    Two-step flow:
      Step 1 (GET / POST action=preview): select students + configure fee → show review.
      Step 2 (POST action=confirm): re-validate → create records atomically → redirect.

    Security: all student IDs are re-validated against the authenticated school + active
    year + building scope on every POST. Duplicates are detected and surfaced to the user
    before any record is created. The entire creation is wrapped in a single DB transaction
    that rolls back completely on any unexpected failure.
    """
    school = get_current_school()
    year = get_active_year(school.id) if school else None
    if not school or not year:
        flash('يرجى اختيار مدرسة ذات سنة دراسية نشطة قبل إنشاء سجلات الرسوم.', 'danger')
        return redirect(url_for('fees.index'))

    fee_types = FeeType.query.all()
    years_q = AcademicYear.query
    if school:
        years_q = years_q.filter_by(school_id=school.id)
    years = years_q.order_by(AcademicYear.start_date.desc()).all()

    if request.method == 'GET':
        return render_template('fees/bulk_form.html', fee_types=fee_types, years=years)

    # ── POST (preview or confirm) ─────────────────────────────────────────────
    action = request.form.get('action', 'preview')

    # ── Parse and deduplicate submitted student IDs ───────────────────────────
    raw_ids = request.form.get('student_ids', '').strip()
    try:
        student_id_list = list(dict.fromkeys(
            int(x) for x in raw_ids.split(',') if x.strip().isdigit()
        ))
    except Exception:
        student_id_list = []

    if not student_id_list:
        flash('يرجى اختيار طالب واحد على الأقل.', 'danger')
        return render_template('fees/bulk_form.html', fee_types=fee_types, years=years), 400

    # ── Validate fee type (school-scoped) ─────────────────────────────────────
    selected_fee_type_id = request.form.get('fee_type_id', type=int)
    selected_year_id = request.form.get('academic_year_id', type=int) or year.id

    fee_type = FeeType.query.filter_by(
        id=selected_fee_type_id, school_id=school.id
    ).first()
    if not fee_type:
        flash('نوع الرسم المحدد غير صالح.', 'danger')
        return render_template('fees/bulk_form.html', fee_types=fee_types, years=years), 400

    # ── Validate amounts ──────────────────────────────────────────────────────
    try:
        total_amount, discount, net_amount = compute_fee_amounts(request.form)
    except FeeValidationError as exc:
        flash(str(exc), 'danger')
        return render_template('fees/bulk_form.html', fee_types=fee_types, years=years), 400

    if total_amount <= 0:
        flash('يجب أن يكون المبلغ الإجمالي أكبر من صفر.', 'danger')
        return render_template('fees/bulk_form.html', fee_types=fee_types, years=years), 400

    # ── Validate installments ─────────────────────────────────────────────────
    num_inst = max(1, min(12, int(request.form.get('num_installments', 1) or 1)))
    installment_dates = []
    for i in range(1, num_inst + 1):
        due_str = request.form.get(f'due_date_{i}')
        try:
            due = dt.strptime(due_str, '%Y-%m-%d').date() if due_str else date.today()
        except ValueError:
            due = date.today()
        installment_dates.append(due)

    # ── Load and validate selected students ───────────────────────────────────
    # School + year + building scope enforced by _active_fee_student_query.
    valid_students = (
        _active_fee_student_query(school, year)
        .filter(Student.id.in_(student_id_list))
        .all()
    )
    valid_ids = {s.id for s in valid_students}
    invalid_count = len(student_id_list) - len(valid_ids)

    if invalid_count:
        flash(
            f'تم استبعاد {invalid_count} طالب غير صالح '
            '(لا ينتمي لهذه المدرسة أو السنة الدراسية أو غير نشط).',
            'warning',
        )

    if not valid_students:
        flash('لا يوجد طلاب صالحون من بين المحددين.', 'danger')
        return render_template('fees/bulk_form.html', fee_types=fee_types, years=years), 400

    # ── Detect duplicates: students who already have this fee for this year ───
    existing_student_ids = set(
        row.student_id for row in
        FeeRecord.query
        .filter(
            FeeRecord.student_id.in_(valid_ids),
            FeeRecord.fee_type_id == selected_fee_type_id,
            FeeRecord.academic_year_id == selected_year_id,
        )
        .with_entities(FeeRecord.student_id)
        .all()
    )

    # Preserve original selection order.
    order_map = {sid: idx for idx, sid in enumerate(student_id_list)}
    duplicate_students = sorted(
        [s for s in valid_students if s.id in existing_student_ids],
        key=lambda s: order_map.get(s.id, 9999),
    )
    eligible_students = sorted(
        [s for s in valid_students if s.id not in existing_student_ids],
        key=lambda s: order_map.get(s.id, 9999),
    )

    # ── Build passthrough fields for the confirm form ─────────────────────────
    # Only trusted server-computed or explicitly validated values are forwarded.
    passthrough = {
        'academic_year_id': str(selected_year_id),
        'fee_type_id':      str(selected_fee_type_id),
        'total_amount':     str(total_amount),
        'discount_type':    request.form.get('discount_type', 'fixed'),
        'discount_value':   str(request.form.get('discount_value', '0') or '0'),
        'num_installments': str(num_inst),
        'notes':            request.form.get('notes', ''),
    }
    for i, d in enumerate(installment_dates, 1):
        passthrough[f'due_date_{i}'] = d.isoformat()

    # ── PREVIEW step ──────────────────────────────────────────────────────────
    if action == 'preview':
        return render_template(
            'fees/bulk_review.html',
            school=school,
            year=year,
            fee_type=fee_type,
            eligible_students=eligible_students,
            duplicate_students=duplicate_students,
            total_amount=float(total_amount),
            discount=float(discount),
            net_amount=float(net_amount),
            num_inst=num_inst,
            installment_dates=installment_dates,
            total_financial_value=float(net_amount) * len(eligible_students),
            passthrough=passthrough,
            eligible_ids=','.join(str(s.id) for s in eligible_students),
        )

    # ── CONFIRM step ──────────────────────────────────────────────────────────
    if action != 'confirm':
        flash('طلب غير صالح.', 'danger')
        return redirect(url_for('fees.bulk_create'))

    if not eligible_students:
        flash(
            'لا يوجد طلاب مؤهلون لإنشاء سجلات رسوم لهم '
            '(جميع المحددين لديهم رسوم مماثلة).',
            'warning',
        )
        return redirect(url_for('fees.index'))

    # Capture student names before commit to avoid post-commit N+1 lazy-loads.
    student_names = {s.id: s.full_name for s in eligible_students}

    created_count = 0
    concurrent_skip = 0
    created_ids = []

    try:
        for student in eligible_students:
            # Guard against race: another request may have created the fee between
            # the preview step and this confirm step.
            if FeeRecord.query.filter_by(
                student_id=student.id,
                fee_type_id=selected_fee_type_id,
                academic_year_id=selected_year_id,
            ).first():
                concurrent_skip += 1
                continue

            persist_fee_record(
                request.form,
                school=school,
                student_id=student.id,
                fee_type_id=selected_fee_type_id,
                academic_year_id=selected_year_id,
                total_amount=total_amount,
                discount=discount,
            )
            created_ids.append(student.id)
            created_count += 1

        # Single atomic commit — if any persist_fee_record flush failed earlier,
        # the exception is caught below and the whole batch rolls back.
        db.session.commit()

    except Exception:
        db.session.rollback()
        _log.exception('[fees] Bulk creation failed school_id=%s fee_type_id=%s',
                       school.id, selected_fee_type_id)
        flash(
            'حدث خطأ أثناء إنشاء سجلات الرسوم. '
            'تم التراجع عن جميع التغييرات. يرجى المحاولة مرة أخرى.',
            'danger',
        )
        return redirect(url_for('fees.bulk_create'))

    # Post-commit: push FCM notifications to parents (best-effort — must not block result).
    for sid in created_ids:
        name = student_names.get(sid, '')
        _notify_fee_parents(
            sid,
            'سجل رسوم جديد',
            f'تم إضافة سجل رسوم جديد للطالب {name}.',
            screen='fees',
        )

    skipped_total = len(duplicate_students) + concurrent_skip
    log_action(
        'bulk_create', 'fee_record', None,
        details=(
            f'bulk_fee created={created_count} skipped={skipped_total} '
            f'school_id={school.id} fee_type_id={selected_fee_type_id} '
            f'year_id={selected_year_id}'
        ),
    )

    parts = []
    if created_count:
        parts.append(f'تم إنشاء {created_count} سجل رسوم بنجاح.')
    if skipped_total:
        parts.append(f'تم تخطي {skipped_total} طالب لوجود رسوم مماثلة.')
    if not parts:
        parts.append('لم يتم إنشاء أي سجل رسوم.')

    flash(' '.join(parts), 'success' if created_count else 'warning')
    return redirect(url_for('fees.index'))


@fees_bp.route('/pay/<int:inst_id>', methods=['POST'])
@login_required
@historical_guard
@permission_required('record_payments')
def pay_installment(inst_id):
    """
    Record a (partial or full) payment against an installment.

    All mutations — installment fields, receipt number, and the matching Revenue
    record — are written in a single atomic transaction. Either everything commits
    or everything rolls back. There is no silent swallowing of Revenue failures.

    Form fields:
      received_amount  — Decimal (required). May be less than inst.amount.
      payment_method   — cash | transfer | cheque | card  (default cash)
      paid_date        — YYYY-MM-DD (optional, defaults to today)
      notes            — free text
    """
    try:
        # include_all_years lets staff pay installments from prior academic years
        # that are still visible on the student fee profile. The ORM school filter
        # still applies; only year filtering is relaxed.
        inst = (
            FeeInstallment.query
            .execution_options(include_all_years=True)
            .options(
                joinedload(FeeInstallment.fee_record)
                .joinedload(FeeRecord.student)
            )
            .filter(FeeInstallment.id == inst_id)
            .first_or_404()
        )

        # Building scope — restricted users cannot pay for students outside their buildings.
        _student = inst.fee_record.student if inst.fee_record else None
        if not user_can_access_student(current_user, get_current_school(), _student):
            return jsonify({'status': 'error',
                            'message': 'ليس لديك صلاحية الوصول إلى بيانات هذه البناية'}), 403

        # Capture the student name NOW, while the eagerly-loaded relationship is
        # still live. After db.session.commit() SQLAlchemy expires all attributes;
        # a subsequent lazy-reload of fee_record goes through the year-scoped ORM
        # filter and may return None for prior-year installments, making
        # inst.fee_record.student.full_name raise AttributeError.
        student_name = _student.full_name if _student else '—'

        raw_amount = request.form.get('received_amount', '').strip()
        if not raw_amount:
            return jsonify({'status': 'error', 'message': 'يرجى إدخال المبلغ المستلم.'}), 400

        try:
            received = Decimal(raw_amount)
        except (InvalidOperation, ValueError):
            return jsonify({'status': 'error', 'message': 'المبلغ غير صالح.'}), 400

        if received <= 0:
            return jsonify({'status': 'error', 'message': 'يجب أن يكون المبلغ المستلم أكبر من صفر.'}), 400

        # ── Resolve the Revenue category before acquiring the row lock ────────
        # This must happen before the FOR UPDATE lock: on first use it issues its
        # own db.session.commit(), which would release any lock acquired before it.
        fee_category = (
            RevenueCategory.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(name='رسوم دراسية', school_id=inst.school_id)
            .first()
        )
        if not fee_category:
            try:
                fee_category = RevenueCategory(name='رسوم دراسية', school_id=inst.school_id)
                db.session.add(fee_category)
                db.session.flush()
                db.session.commit()
            except IntegrityError:
                # Race condition: another concurrent request created the category.
                db.session.rollback()
                fee_category = (
                    RevenueCategory.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter_by(name='رسوم دراسية', school_id=inst.school_id)
                    .first()
                )
                if not fee_category:
                    raise  # should never happen

        # ── Parse the payment date ────────────────────────────────────────────
        paid_str = request.form.get('paid_date')
        if paid_str:
            try:
                paid_date = dt.strptime(paid_str, '%Y-%m-%d').date()
            except ValueError:
                paid_date = date.today()
        else:
            paid_date = date.today()

        # ── Determine academic_year_id for the Revenue record ─────────────────
        # Use the installment's own academic year. Revenue queries in the finances
        # module already use include_all_years=True and filter by Revenue.date
        # (calendar year), so the record will appear correctly regardless.
        # Fall back to the active year only if the installment somehow has no year.
        rev_academic_year_id = inst.academic_year_id
        if not rev_academic_year_id:
            school = get_current_school()
            active_year = get_active_year(school.id) if school else None
            rev_academic_year_id = active_year.id if active_year else None

        # ── Acquire row lock and recheck remaining (race-condition guard) ─────
        # SELECT FOR UPDATE serializes concurrent payment attempts for this
        # installment. populate_existing() refreshes inst's scalar attributes
        # from the latest committed DB state so any payment already committed by
        # a concurrent request is reflected before we compute remaining.
        # The lock is held until the transaction commits below.
        (
            db.session.query(FeeInstallment)
            .execution_options(include_all_years=True)
            .with_for_update()
            .filter(FeeInstallment.id == inst_id)
            .populate_existing()
            .one()
        )

        remaining = Decimal(str(inst.amount)) - Decimal(str(inst.received_amount or 0))
        if remaining <= 0:
            return jsonify({'status': 'error',
                            'message': 'هذا القسط مسدّد بالكامل بالفعل.'}), 400
        if received > remaining:
            received = remaining

        # ── Apply ALL DB mutations in ONE atomic transaction ──────────────────
        # Installment fields, receipt number, and Revenue record are written
        # together. If any part fails the entire operation rolls back — no
        # partial state, no missing Revenue records.
        inst.received_amount = Decimal(str(inst.received_amount or 0)) + received
        inst.payment_method  = request.form.get('payment_method', 'cash')
        inst.collected_by    = current_user.id
        inst.notes           = request.form.get('notes', '').strip() or inst.notes
        inst.paid_date       = paid_date
        inst.recompute_status()

        # Assign a receipt number on the first payment against this installment.
        if Decimal(str(inst.received_amount or 0)) > 0 and not inst.receipt_no:
            inst.receipt_no = generate_receipt_no()

        revenue = Revenue(
            category_id      = fee_category.id,
            school_id        = inst.school_id,
            academic_year_id = rev_academic_year_id,
            amount           = received,
            description      = (
                f'دفعة رسوم للطالب {student_name}'
                f' - قسط #{inst.installment_no}'
                + (f' - {inst.receipt_no}' if inst.receipt_no else '')
            ),
            date             = paid_date,
            recorded_by      = current_user.id,
        )
        db.session.add(revenue)
        db.session.commit()  # single commit — installment + revenue are atomic

        # ── Post-commit: audit log and FCM push ───────────────────────────────
        # These run after the transaction is safe. Neither must ever roll back
        # the committed payment — both have their own error handling.
        log_action('payment', 'fee_installment', inst.id,
                   details=f'received={received} method={inst.payment_method} '
                           f'status={inst.status}')

        status_label = {'paid': 'مكتمل', 'partial': 'دفعة جزئية',
                        'pending': 'قيد الانتظار', 'overdue': 'متأخر'}.get(inst.status, '')
        receipt = inst.receipt_no or '—'

        if _student is not None:
            _notify_fee_parents(
                _student.id,
                'تم تسجيل دفعة',
                f'تم تسجيل دفعة بقيمة {received} لقسط الرسوم رقم {inst.installment_no} '
                f'({status_label}).',
                screen='fees',
                fee_record_id=inst.fee_record_id,
                installment_id=inst.id,
            )

        receipt_url = url_for('fees.generate_receipt', inst_id=inst.id) if inst.receipt_no else None
        return jsonify({
            'status':      'success',
            'message':     f'تم تسجيل دفعة {received} ({status_label}). رقم الإيصال: {receipt}',
            'receipt_url': receipt_url,
        })

    except Exception as e:
        db.session.rollback()
        _log.exception('[fees] Payment processing error for inst_id=%s', inst_id)
        return jsonify({'status': 'error',
                        'message': 'حدث خطأ في معالجة الدفع. يرجى المحاولة مرة أخرى.'}), 500


@fees_bp.route('/installment/<int:inst_id>/receipt')
@login_required
@permission_required('record_payments')
def generate_receipt(inst_id):
    """Generate and serve PDF receipt for a paid installment."""
    from app.utils.pdf_gen import generate_fee_receipt
    from app.models import SchoolSettings
    from flask import send_file, abort
    
    inst = (
        FeeInstallment.query
        .execution_options(include_all_years=True)
        .options(
            joinedload(FeeInstallment.fee_record)
            .joinedload(FeeRecord.student)
        )
        .filter(FeeInstallment.id == inst_id)
        .first_or_404()
    )
    if not inst.receipt_no:
        abort(404, "No receipt available for this installment")

    # Building scope — restricted users cannot view receipts outside their buildings.
    _student = inst.fee_record.student if inst.fee_record else None
    if not user_can_access_student(current_user, get_current_school(), _student):
        abort(403)

    school_settings = get_current_school() or SchoolSettings.get()
    pdf_bytes = generate_fee_receipt(inst, school_settings, print_date=date.today())
    
    if not pdf_bytes:
        abort(500, "PDF generation failed")
    
    from io import BytesIO
    buf = BytesIO(pdf_bytes)
    buf.seek(0)
    
    filename = f"receipt_{inst.receipt_no}.pdf"
    return send_file(buf, as_attachment=False, download_name=filename, mimetype='application/pdf')


@fees_bp.route('/export/excel')
@login_required
@permission_required('manage_fees')
def export_excel():
    from flask import Response
    from app.utils.excel_export import export_fees

    search            = request.args.get('q', '')
    fee_type_filter   = request.args.get('fee_type', 'all')
    payment_status    = request.args.get('payment_status', 'all')
    installment_filter = request.args.get('installment', 'all')

    total_paid_sub = db.session.query(
        FeeInstallment.fee_record_id,
        func.sum(FeeInstallment.received_amount).label('total_paid')
    ).group_by(FeeInstallment.fee_record_id).subquery()

    query = FeeRecord.query.join(Student).outerjoin(
        total_paid_sub, FeeRecord.id == total_paid_sub.c.fee_record_id
    )

    # Building scope — restricted users only export their buildings' fees.
    query = apply_building_scope_to_fees(query, current_user, get_current_school())

    if search:
        query = query.filter(Student.full_name.ilike(f'%{search}%') |
                             Student.student_id.ilike(f'%{search}%'))

    if fee_type_filter != 'all':
        query = query.filter(FeeRecord.fee_type_id == int(fee_type_filter))

    if installment_filter != 'all':
        query = query.join(FeeInstallment).filter(FeeInstallment.installment_no == int(installment_filter))

    net_amount_expr = FeeRecord.total_amount - func.coalesce(FeeRecord.discount, 0)
    remaining_expr = net_amount_expr - func.coalesce(total_paid_sub.c.total_paid, 0)
    if payment_status == 'paid':
        query = query.filter(remaining_expr <= 0)
    elif payment_status == 'unpaid':
        query = query.filter(remaining_expr > 0)

    records = query.order_by(FeeRecord.created_at.desc()).all()
    data = export_fees(records)
    if not data:
        flash('مكتبة Excel غير متاحة.', 'warning')
        return redirect(url_for('fees.index'))

    return Response(
        data,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename=fees.xlsx'}
    )


@fees_bp.route('/fee-types', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_fees')
def fee_types():
    school = get_current_school()
    year = get_active_year(school.id) if school else None
    if request.method == 'POST':
        if not school or not year:
            flash('Select a school with an active academic year before adding fee types.', 'danger')
            return redirect(url_for('fees.fee_types'))
        name = request.form.get('name', '').strip()
        if name:
            if FeeType.query.filter_by(name=name, school_id=school.id,
                                       academic_year_id=year.id).first():
                flash('Fee type already exists for this school/year.', 'danger')
                return redirect(url_for('fees.fee_types'))
            ft = FeeType(name=name,
                         school_id=school.id,
                         academic_year_id=year.id,
                         description=request.form.get('description', '').strip())
            db.session.add(ft)
            db.session.commit()
            flash('تم إضافة نوع الرسوم.', 'success')
        return redirect(url_for('fees.fee_types'))
    types = FeeType.query.all()
    return render_template('fees/fee_types.html', types=types)


@fees_bp.route('/print')
@login_required
@permission_required('manage_fees')
def print_list():
    """Print view of current fee records — same filters as index, no pagination."""
    search             = request.args.get('q', '')
    fee_type_filter    = request.args.get('fee_type', 'all')
    payment_status     = request.args.get('payment_status', 'all')
    installment_filter = request.args.get('installment', 'all')

    total_paid_sub = db.session.query(
        FeeInstallment.fee_record_id,
        func.sum(FeeInstallment.received_amount).label('total_paid')
    ).group_by(FeeInstallment.fee_record_id).subquery()

    school = get_current_school()
    year   = get_view_year(school.id) if school else None

    query = (
        FeeRecord.query
        .join(Student)
        .outerjoin(total_paid_sub, FeeRecord.id == total_paid_sub.c.fee_record_id)
    )

    if school:
        query = query.filter(FeeRecord.school_id == school.id)

    query = apply_building_scope_to_fees(query, current_user, school)

    if search:
        query = query.filter(
            Student.full_name.ilike(f'%{search}%') |
            Student.student_id.ilike(f'%{search}%')
        )

    if fee_type_filter != 'all':
        query = query.filter(FeeRecord.fee_type_id == int(fee_type_filter))

    if installment_filter != 'all':
        query = query.join(FeeInstallment).filter(
            FeeInstallment.installment_no == int(installment_filter))

    net_amount_expr = FeeRecord.total_amount - func.coalesce(FeeRecord.discount, 0)
    remaining_expr  = net_amount_expr - func.coalesce(total_paid_sub.c.total_paid, 0)

    if payment_status == 'paid':
        query = query.filter(remaining_expr <= 0)
    elif payment_status == 'unpaid':
        query = query.filter(remaining_expr > 0)

    records = query.order_by(FeeRecord.created_at.desc()).all()

    _inst_map = {}
    if records:
        _ids = [r.id for r in records]
        _insts = (
            FeeInstallment.query
            .filter(FeeInstallment.fee_record_id.in_(_ids))
            .order_by(FeeInstallment.installment_no)
            .all()
        )
        for _i in _insts:
            _inst_map.setdefault(_i.fee_record_id, []).append(_i)

    fee_entries = [(r, _inst_map.get(r.id, [])) for r in records]

    grand_total = sum(float(r.total_amount) for r in records)
    grand_disc  = sum(float(r.discount or 0) for r in records)
    grand_paid  = sum(
        sum(float(i.received_amount or 0) for i in _inst_map.get(r.id, []))
        for r in records
    )
    grand_rem   = grand_total - grand_disc - grand_paid

    fee_types     = FeeType.query.all()
    fee_type_name = next(
        (ft.name for ft in fee_types if str(ft.id) == fee_type_filter), None)

    logo_url = None
    if school and getattr(school, 'logo_path', None):
        from app.utils.helpers import resolve_photo_url
        logo_url = resolve_photo_url(school.logo_path)

    return render_template(
        'fees/print_list.html',
        fee_entries=fee_entries,
        school=school,
        year=year,
        search=search,
        fee_type_filter=fee_type_filter,
        fee_type_name=fee_type_name,
        payment_status=payment_status,
        installment_filter=installment_filter,
        grand_total=grand_total,
        grand_disc=grand_disc,
        grand_paid=grand_paid,
        grand_rem=grand_rem,
        print_date=date.today(),
        logo_url=logo_url,
    )


@fees_bp.route('/student/<int:student_id>/statement')
@login_required
@permission_required('manage_fees')
def student_statement(student_id):
    """Complete financial statement for a student — all fee types, all academic years."""
    school = get_current_school()

    student = (
        Student.query
        .execution_options(include_all_years=True)
        .options(
            joinedload(Student.section).joinedload(Section.grade)
        )
        .filter(Student.id == student_id)
        .first_or_404()
    )

    # School isolation: student must belong to the authenticated user's school.
    if school and student.school_id and student.school_id != school.id:
        abort(403)

    # Building scope: restricted users cannot view students outside their buildings.
    if not user_can_access_student(current_user, school, student):
        abort(403)

    # Load all fee records across all academic years, scoped to school.
    fee_records = (
        FeeRecord.query
        .execution_options(include_all_years=True)
        .filter_by(student_id=student.id, school_id=student.school_id)
        .options(
            joinedload(FeeRecord.fee_type),
            joinedload(FeeRecord.academic_year),
        )
        .order_by(FeeRecord.academic_year_id.asc(), FeeRecord.created_at.asc())
        .all()
    )

    fee_entries = []
    if fee_records:
        _ids = [r.id for r in fee_records]
        _insts = (
            FeeInstallment.query
            .execution_options(include_all_years=True)
            .filter(
                FeeInstallment.fee_record_id.in_(_ids),
                FeeInstallment.school_id == student.school_id,
            )
            .options(joinedload(FeeInstallment.collector))
            .order_by(FeeInstallment.fee_record_id, FeeInstallment.installment_no)
            .all()
        )
        _inst_map = {}
        for _i in _insts:
            _inst_map.setdefault(_i.fee_record_id, []).append(_i)
        fee_entries = [(r, _inst_map.get(r.id, [])) for r in fee_records]

    grand_total = sum(float(r.total_amount) for r, _ in fee_entries)
    grand_disc  = sum(float(r.discount or 0) for r, _ in fee_entries)
    grand_net   = grand_total - grand_disc
    grand_paid  = sum(
        sum(float(i.received_amount or 0) for i in insts)
        for _, insts in fee_entries
    )
    grand_rem   = grand_net - grand_paid

    logo_url = None
    if school and getattr(school, 'logo_path', None):
        from app.utils.helpers import resolve_photo_url
        logo_url = resolve_photo_url(school.logo_path)

    return render_template(
        'fees/student_statement.html',
        student=student,
        school=school,
        fee_entries=fee_entries,
        grand_total=grand_total,
        grand_disc=grand_disc,
        grand_net=grand_net,
        grand_paid=grand_paid,
        grand_rem=grand_rem,
        print_date=date.today(),
        logo_url=logo_url,
    )


@fees_bp.route('/reminder-settings', methods=['POST'])
@login_required
@admin_required
def reminder_settings():
    # Resolve school_id explicitly — do NOT rely on get_current_school() because
    # it can return a SchoolSettings fallback object for super_admin without an
    # active school in session, which is not a School row and cannot be saved.
    if current_user.is_super_admin:
        school_id = flask_session.get('active_school_id')
    else:
        school_id = current_user.school_id

    _log.warning(
        '[fees-reminder-settings] called user_id=%s is_super_admin=%s school_id=%s form=%s',
        current_user.id, current_user.is_super_admin, school_id, dict(request.form),
    )

    if not school_id:
        flash('الرجاء اختيار مدرسة أولاً.', 'warning')
        return redirect(url_for('fees.index'))

    school = (
        School.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(id=school_id)
        .first()
    )
    if not school:
        flash('المدرسة غير موجودة.', 'warning')
        return redirect(url_for('fees.index'))

    enabled = request.form.get('fee_reminder_enabled') == '1'

    try:
        days_before = max(1, min(int(request.form.get('fee_reminder_days_before', '3').strip()), 30))
    except (ValueError, TypeError):
        days_before = 3

    try:
        per_day = max(1, min(int(request.form.get('fee_reminder_per_day', '1').strip()), 6))
    except (ValueError, TypeError):
        per_day = 1

    school.fee_reminder_enabled     = enabled
    school.fee_reminder_days_before = days_before
    school.fee_reminder_per_day     = per_day

    _log.warning(
        '[fees-reminder-settings] saving school_id=%s enabled=%s days_before=%s per_day=%s',
        school.id, enabled, days_before, per_day,
    )

    db.session.commit()

    _log.warning(
        '[fees-reminder-settings] saved school_id=%s enabled=%s days_before=%s per_day=%s',
        school.id, enabled, days_before, per_day,
    )

    log_action('edit', 'school', school.id, details='fee reminder settings updated')
    flash('تم حفظ إعدادات تذكير الأقساط بنجاح.', 'success')
    return redirect(url_for('fees.index'))

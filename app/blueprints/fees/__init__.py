"""Mecha-School – Fees Blueprint"""
import logging
from decimal import Decimal, InvalidOperation

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, session as flask_session
from flask_login import login_required, current_user
from datetime import date, datetime as dt
from sqlalchemy import func
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


@fees_bp.route('/pay/<int:inst_id>', methods=['POST'])
@login_required
@historical_guard
@permission_required('record_payments')
def pay_installment(inst_id):
    """
    Phase 2 rewrite — supports manual partial-payment entry.

    Form fields:
      received_amount  — Decimal (required). May be less than inst.amount.
      payment_method   — cash | transfer | cheque | card  (default cash)
      paid_date        — YYYY-MM-DD (optional, defaults to today)
      notes            — free text

    Behaviour:
      * Adds `received_amount` (cumulative) to inst.received_amount.
      * Calls inst.recompute_status() to transition
            pending → partial → paid   (and flags overdue automatically).
      * Writes an AuditLog row.
      * Generates a fresh receipt_no only on the transaction that *completes*
        the installment, so partial pays get receipts too but the final one
        is distinguishable.
    """
    try:
        # Use include_all_years so installments from prior academic years (which are
        # legitimately visible on the student profile) can be paid when the current
        # view year is the active year.  The ORM school filter still applies.
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

        raw_amount = request.form.get('received_amount', '').strip()
        if not raw_amount:
            return jsonify({'status': 'error', 'message': 'يرجى إدخال المبلغ المستلم.'}), 400

        try:
            received = Decimal(raw_amount)
        except (InvalidOperation, ValueError):
            return jsonify({'status': 'error', 'message': 'المبلغ غير صالح.'}), 400

        if received <= 0:
            return jsonify({'status': 'error', 'message': 'يجب أن يكون المبلغ المستلم أكبر من صفر.'}), 400

        # Cap at remaining balance so we never record over-pay
        remaining = Decimal(str(inst.amount)) - Decimal(str(inst.received_amount or 0))
        if received > remaining:
            received = remaining

        inst.received_amount = Decimal(str(inst.received_amount or 0)) + received
        inst.payment_method  = request.form.get('payment_method', 'cash')
        inst.collected_by    = current_user.id
        inst.notes           = request.form.get('notes', '').strip() or inst.notes

        paid_str = request.form.get('paid_date')
        if paid_str:
            try:
                inst.paid_date = dt.strptime(paid_str, '%Y-%m-%d').date()
            except ValueError:
                inst.paid_date = date.today()
        else:
            inst.paid_date = date.today()

        inst.recompute_status()

        # Generate receipt number for ANY payment transaction
        if (Decimal(str(inst.received_amount or 0)) > 0) and not inst.receipt_no:
            inst.receipt_no = generate_receipt_no()

        db.session.commit()
        
        # Create revenue record for student fee payment
        try:
            # Find or create "Student Fees" revenue category
            fee_category = (
                RevenueCategory.query.execution_options(bypass_tenant_scope=True)
                .filter_by(name='رسوم دراسية', school_id=inst.school_id)
                .first()
            )
            if not fee_category:
                fee_category = RevenueCategory(
                    name='رسوم دراسية',
                    school_id=inst.school_id,
                )
                db.session.add(fee_category)
                db.session.commit()
            
            # Create revenue record for this payment
            revenue_record = Revenue(
                category_id=fee_category.id,
                school_id=inst.school_id,
                academic_year_id=inst.academic_year_id,
                amount=received,
                description=f'دفعة رسوم للطالب {inst.fee_record.student.full_name} - قسط #{inst.installment_no}',
                date=inst.paid_date,
                recorded_by=current_user.id
            )
            db.session.add(revenue_record)
            db.session.commit()
            
        except Exception as e:
            # Log error but don't fail the payment if revenue recording fails
            print(f"Warning: Failed to create revenue record for payment: {e}")
        
        log_action('payment', 'fee_installment', inst.id,
                   details=f'received={received} method={inst.payment_method} '
                           f'status={inst.status}')

        status_label = {'paid': 'مكتمل', 'partial': 'دفعة جزئية',
                        'pending': 'قيد الانتظار', 'overdue': 'متأخر'}.get(inst.status, '')
        receipt = inst.receipt_no or '—'

        # Push after the payment is committed. Resolve the student from the
        # already-loaded fee_record relationship (server-side, school-scoped).
        _paid_student = inst.fee_record.student if inst.fee_record else None
        if _paid_student is not None:
            _notify_fee_parents(
                _paid_student.id,
                'تم تسجيل دفعة',
                f'تم تسجيل دفعة بقيمة {received} لقسط الرسوم رقم {inst.installment_no} '
                f'({status_label}).',
                screen='fees',
                fee_record_id=inst.fee_record_id,
                installment_id=inst.id,
            )

        # Return JSON with success status and receipt URL
        receipt_url = url_for('fees.generate_receipt', inst_id=inst.id) if inst.receipt_no else None
        return jsonify({
            'status': 'success',
            'message': f'تم تسجيل دفعة {received} ({status_label}). رقم الإيصال: {receipt}',
            'receipt_url': receipt_url
        })

    except Exception as e:
        # Rollback any partial changes
        db.session.rollback()
        print(f"Payment processing error: {e}")
        return jsonify({'status': 'error', 'message': 'حدث خطأ في معالجة الدفع. يرجى المحاولة مرة أخرى.'}), 500


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

    # Checkbox: sends '1' when checked, absent when unchecked
    enabled = request.form.get('fee_reminder_enabled') == '1'

    raw_val = request.form.get('fee_reminder_before_value', '3').strip()
    try:
        parsed_val = int(raw_val)
    except (ValueError, TypeError):
        parsed_val = 3

    fee_unit = request.form.get('fee_reminder_before_unit', 'days')
    if fee_unit not in ('days', 'hours', 'minutes'):
        fee_unit = 'days'

    if fee_unit == 'minutes':
        parsed_val = max(5, min(parsed_val, 1440))
    elif fee_unit == 'hours':
        parsed_val = max(1, min(parsed_val, 72))
    else:
        parsed_val = max(1, min(parsed_val, 30))

    school.fee_reminder_enabled      = enabled
    school.fee_reminder_before_value = parsed_val
    school.fee_reminder_before_unit  = fee_unit

    _log.warning(
        '[fees-reminder-settings] saving school_id=%s enabled=%s value=%s unit=%s',
        school.id, enabled, parsed_val, fee_unit,
    )

    db.session.commit()

    _log.warning(
        '[fees-reminder-settings] saved school_id=%s enabled=%s value=%s unit=%s',
        school.id, enabled, parsed_val, fee_unit,
    )

    log_action('edit', 'school', school.id, details='fee reminder settings updated')
    flash('تم حفظ إعدادات تذكير الأقساط بنجاح.', 'success')
    return redirect(url_for('fees.index'))

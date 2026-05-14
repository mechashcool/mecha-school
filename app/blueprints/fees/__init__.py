"""Mecha-School – Fees Blueprint"""
from decimal import Decimal, InvalidOperation

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from datetime import date, datetime as dt
from sqlalchemy import func

from app.models import (db, FeeRecord, FeeInstallment, FeeType, Student, AcademicYear, SchoolSettings, Revenue, RevenueCategory)
from app.utils.decorators import (permission_required, get_current_school,
                                   get_active_year, get_view_year, historical_guard)
from app.utils.helpers import generate_receipt_no
from app.utils.audit import log_action

fees_bp = Blueprint('fees', __name__, template_folder='../../templates/fees')


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
    fee_types = FeeType.query.all()
    years_q   = AcademicYear.query
    if school:
        years_q = years_q.filter_by(school_id=school.id)
    years = years_q.order_by(AcademicYear.start_date.desc()).all()
    return render_template('fees/index.html',
                           records=records, fee_types=fee_types,
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
    students_q = Student.query.filter_by(status='active')
    if school:
        students_q = students_q.filter_by(school_id=school.id)
    if year:
        students_q = students_q.filter_by(academic_year_id=year.id)
    students  = students_q.order_by(Student.full_name).all()
    fee_types = FeeType.query.all()
    years_q   = AcademicYear.query
    if school:
        years_q = years_q.filter_by(school_id=school.id)
    years = years_q.order_by(AcademicYear.start_date.desc()).all()

    if request.method == 'POST':
        selected_student_id = request.form.get('student_id', type=int)
        selected_fee_type_id = request.form.get('fee_type_id', type=int)
        selected_year_id = request.form.get('academic_year_id', type=int) or (year.id if year else None)
        if FeeRecord.query.filter_by(student_id=selected_student_id,
                                     fee_type_id=selected_fee_type_id,
                                     academic_year_id=selected_year_id).first():
            flash('A fee record for this student, fee type, and academic year already exists.', 'danger')
            return render_template('fees/form.html',
                                   students=students, fee_types=fee_types, years=years)
        record = FeeRecord(
            student_id       = selected_student_id,
            fee_type_id      = selected_fee_type_id,
            academic_year_id = selected_year_id,
            school_id        = school.id if school else None,
            total_amount     = float(request.form.get('total_amount', 0)),
            discount         = float(request.form.get('discount', 0) or 0),
            notes            = request.form.get('notes', '').strip(),
        )
        db.session.add(record)
        db.session.flush()

        # Build installments
        num_inst = max(1, min(12, int(request.form.get('num_installments', 1) or 1)))
        net      = record.net_amount
        each     = round(net / num_inst, 2)
        for i in range(1, num_inst + 1):
            due_str = request.form.get(f'due_date_{i}')
            due     = dt.strptime(due_str, '%Y-%m-%d').date() if due_str else date.today()
            inst = FeeInstallment(
                fee_record_id  = record.id,
                school_id      = record.school_id,
                academic_year_id = record.academic_year_id,
                installment_no = i,
                amount         = each,
                due_date       = due,
            )
            db.session.add(inst)

        db.session.commit()
        flash('تم إنشاء سجل الرسوم بنجاح.', 'success')
        return redirect(url_for('fees.index'))

    return render_template('fees/form.html',
                           students=students, fee_types=fee_types, years=years)


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
        inst = FeeInstallment.query.get_or_404(inst_id)

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
    
    inst = FeeInstallment.query.get_or_404(inst_id)
    if not inst.receipt_no:
        abort(404, "No receipt available for this installment")
    
    school_settings = SchoolSettings.get()
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

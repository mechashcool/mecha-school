"""Super-Admin-only annual platform billing routes."""
from datetime import date, datetime
from uuid import uuid4
from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from sqlalchemy.exc import IntegrityError

from app.models import (db, School, SchoolBilling, AcademicYear,
    AnnualSchoolAgreement, PlatformSchoolInvoice, PlatformInvoiceStudentCoverage,
    PlatformSchoolPayment, PlatformPaymentAllocation)
from app.services.platform_finance import (uncovered_students, school_totals,
    simple_school_summaries, invoice_allocated, payment_allocated,
    refresh_invoice_status)
from app.utils.decorators import super_admin_required
from app.utils.audit import log_action
from app.utils.helpers import generate_receipt_no
from app.utils.arabic_numbers import amount_to_words_iqd


def _year(school_id, year_id):
    return AcademicYear.query.execution_options(bypass_tenant_scope=True).filter_by(
        id=year_id, school_id=school_id).first_or_404()


def _date(value, fallback=None):
    try: return datetime.strptime(value or '', '%Y-%m-%d').date()
    except ValueError: return fallback


def register_finance_routes(bp):
    def back(school_id, year_id=None):
        return redirect(url_for('super_admin.finance_school', school_id=school_id,
                                year_id=year_id))

    @bp.route('/finance/schools/<int:school_id>')
    @login_required
    @super_admin_required
    def finance_school(school_id):
        school = School.query.get_or_404(school_id)
        years = (AcademicYear.query.execution_options(bypass_tenant_scope=True)
                 .filter_by(school_id=school_id).order_by(AcademicYear.start_date.desc()).all())
        requested = request.args.get('year_id', type=int)
        year = _year(school_id, requested) if requested else next(
            (y for y in years if y.is_current), years[0] if years else None)
        agreements, invoices, uncovered = [], [], []
        if year:
            agreements = AnnualSchoolAgreement.query.filter_by(school_id=school_id,
                academic_year_id=year.id).order_by(AnnualSchoolAgreement.revision_number.desc()).all()
            invoices = PlatformSchoolInvoice.query.filter_by(school_id=school_id,
                academic_year_id=year.id).order_by(PlatformSchoolInvoice.created_at.desc()).all()
            uncovered = uncovered_students(school_id, year.id)
        agreement = next((a for a in agreements if a.status == 'active'), None)
        payments = PlatformSchoolPayment.query.filter_by(school_id=school_id).order_by(
            PlatformSchoolPayment.payment_date.desc(), PlatformSchoolPayment.id.desc()).all()
        summary = simple_school_summaries(
            [school], {school.id: year}).get(school.id, {})
        return render_template('super_admin/finance_school.html', school=school,
            years=years, year=year, agreement=agreement, agreements=agreements,
            invoices=invoices, payments=payments,
            open_invoices=[i for i in invoices if i.status not in ('paid', 'void')],
            uncovered=uncovered, totals=school_totals(school_id, year.id if year else None),
            summary=summary,
            legacy=SchoolBilling.query.filter_by(school_id=school_id).order_by(
                SchoolBilling.created_at.desc()).all(), today=date.today(),
            submission_key=uuid4().hex, invoice_allocated=invoice_allocated,
            payment_allocated=payment_allocated)

    @bp.route('/finance/schools/<int:school_id>/agreements', methods=['POST'])
    @login_required
    @super_admin_required
    def finance_set_agreement(school_id):
        School.query.get_or_404(school_id)
        year = _year(school_id, request.form.get('academic_year_id', type=int))
        try: price = int(request.form.get('annual_price_per_student', '0'))
        except ValueError: price = 0
        effective = _date(request.form.get('effective_date'))
        if price <= 0 or not effective:
            flash('السعر والتاريخ الصحيحان مطلوبان.', 'danger'); return back(school_id, year.id)
        current = AnnualSchoolAgreement.query.filter_by(school_id=school_id,
            academic_year_id=year.id, status='active').with_for_update().first()
        revision = current.revision_number + 1 if current else 1
        if current:
            current.status = 'superseded'; db.session.flush()
        row = AnnualSchoolAgreement(school_id=school_id, academic_year_id=year.id,
            revision_number=revision, annual_price_per_student=price,
            effective_date=effective, status='active', created_by=current_user.id,
            approved_by=current_user.id, approved_at=datetime.utcnow(),
            notes=request.form.get('notes', '').strip() or None)
        db.session.add(row); db.session.commit()
        log_action('create', 'annual_school_agreement', row.id,
                   details=f'school={school_id}, year={year.id}, revision={revision}, price={price}')
        flash('تم اعتماد مراجعة السعر مع حفظ التاريخ السابق.', 'success')
        return back(school_id, year.id)

    @bp.route('/finance/schools/<int:school_id>/invoices', methods=['POST'])
    @login_required
    @super_admin_required
    def finance_create_invoice(school_id):
        School.query.get_or_404(school_id)
        year = _year(school_id, request.form.get('academic_year_id', type=int))
        agreement = AnnualSchoolAgreement.query.filter_by(school_id=school_id,
            academic_year_id=year.id, status='active').with_for_update().first()
        ids = {int(v) for v in request.form.getlist('student_ids') if v.isdigit()}
        selected = [s for s in uncovered_students(school_id, year.id) if s.id in ids]
        kind = request.form.get('invoice_type')
        key = request.form.get('submission_key', '').strip() or uuid4().hex
        if PlatformSchoolInvoice.query.filter_by(submission_key=key).first(): return back(school_id, year.id)
        if not agreement or not selected or kind not in ('annual_base', 'additional_students'):
            flash('يلزم اتفاق فعال واختيار طالب واحد على الأقل.', 'danger'); return back(school_id, year.id)
        now = datetime.utcnow(); price = agreement.annual_price_per_student
        row = PlatformSchoolInvoice(school_id=school_id, academic_year_id=year.id,
            agreement_id=agreement.id, invoice_number=f'CSI-{now:%Y%m%d}-{uuid4().hex[:8].upper()}',
            submission_key=key, invoice_type=kind,
            issue_date=_date(request.form.get('issue_date'), date.today()),
            due_date=_date(request.form.get('due_date')), approved_student_count=len(selected),
            unit_price_snapshot=price, total_amount=len(selected) * price, status='unpaid',
            created_by=current_user.id, approved_by=current_user.id, approved_at=now,
            notes=request.form.get('notes', '').strip() or None)
        db.session.add(row)
        try:
            db.session.flush()
            for student in selected:
                db.session.add(PlatformInvoiceStudentCoverage(invoice_id=row.id,
                    school_id=school_id, academic_year_id=year.id, student_id=student.id,
                    student_code_snapshot=student.student_id,
                    student_name_snapshot=student.full_name, unit_price_snapshot=price, status='active'))
            db.session.commit()
        except IntegrityError:
            db.session.rollback(); flash('أحد الطلاب مغطى بالفعل أو تكرر الإرسال.', 'danger')
            return back(school_id, year.id)
        log_action('create', 'platform_school_invoice', row.id,
                   details=f'school={school_id}, year={year.id}, students={len(selected)}, total={row.total_amount}')
        flash('تم إصدار الفاتورة واعتماد تغطية الطلاب.', 'success'); return back(school_id, year.id)

    @bp.route('/finance/invoices/<int:invoice_id>/void', methods=['POST'])
    @login_required
    @super_admin_required
    def finance_void_invoice(invoice_id):
        row = PlatformSchoolInvoice.query.filter_by(id=invoice_id).with_for_update().first_or_404()
        reason = request.form.get('reason', '').strip()
        if not reason or invoice_allocated(row.id):
            flash('يلزم سبب، ولا يمكن إلغاء فاتورة لها تخصيصات.', 'danger')
        elif row.status != 'void':
            row.status = 'void'; row.voided_at = datetime.utcnow(); row.voided_by = current_user.id
            row.void_reason = reason
            PlatformInvoiceStudentCoverage.query.filter_by(invoice_id=row.id, status='active').update(
                {'status': 'void'}, synchronize_session=False)
            db.session.commit(); log_action('void', 'platform_school_invoice', row.id, details=reason)
            flash('تم الإلغاء وتحرير التغطية مع إبقاء السجل.', 'success')
        return back(row.school_id, row.academic_year_id)

    @bp.route('/finance/schools/<int:school_id>/payments', methods=['POST'])
    @login_required
    @super_admin_required
    def finance_record_payment(school_id):
        School.query.get_or_404(school_id)
        try:
            amount = int(request.form.get('amount', '0'))
            count = int(request.form['covered_student_count']) if request.form.get('covered_student_count') else None
        except ValueError: amount, count = 0, None
        paid_on = _date(request.form.get('payment_date'))
        key = request.form.get('submission_key', '').strip() or uuid4().hex
        if PlatformSchoolPayment.query.filter_by(submission_key=key).first(): return back(school_id)
        if amount <= 0 or not paid_on or (count is not None and count < 0):
            flash('بيانات الدفعة غير صالحة.', 'danger'); return back(school_id)
        allocations = []
        for name, raw in request.form.items():
            if name.startswith('allocation_') and raw:
                try: pair = (int(name[11:]), int(raw))
                except ValueError: continue
                if pair[1] > 0: allocations.append(pair)
        if sum(v for _, v in allocations) > amount:
            flash('التخصيصات تتجاوز مبلغ الدفعة.', 'danger'); return back(school_id)
        invoices = {}
        for invoice_id, allocated in allocations:
            inv = PlatformSchoolInvoice.query.filter_by(id=invoice_id,
                school_id=school_id).with_for_update().first()
            if not inv or inv.status == 'void' or allocated > inv.total_amount - invoice_allocated(inv.id):
                db.session.rollback(); flash('تخصيص غير صالح أو يتجاوز رصيد الفاتورة.', 'danger')
                return back(school_id)
            invoices[invoice_id] = inv
        row = PlatformSchoolPayment(school_id=school_id, amount=amount, payment_date=paid_on,
            payment_method=request.form.get('payment_method', 'cash'),
            receipt_number=generate_receipt_no(), submission_key=key,
            transaction_reference=request.form.get('transaction_reference', '').strip() or None,
            received_by=request.form.get('received_by', '').strip() or None,
            recorded_by=current_user.id, covered_student_count=count,
            notes=request.form.get('notes', '').strip() or None, status='active')
        db.session.add(row)
        try:
            db.session.flush()
            for invoice_id, allocated in allocations:
                db.session.add(PlatformPaymentAllocation(payment_id=row.id,
                    invoice_id=invoice_id, allocated_amount=allocated))
            db.session.flush()
            for inv in invoices.values(): refresh_invoice_status(inv)
            db.session.commit()
        except IntegrityError:
            db.session.rollback(); flash('تم رفض دفعة مكررة أو تخصيص غير صالح.', 'danger')
            return back(school_id)
        log_action('create', 'platform_school_payment', row.id,
                   details=f'school={school_id}, amount={amount}, allocated={sum(v for _, v in allocations)}')
        flash('تم تسجيل الدفعة المستقلة وتخصيصاتها.', 'success'); return back(school_id)

    @bp.route('/finance/payments/<int:payment_id>/reverse', methods=['POST'])
    @login_required
    @super_admin_required
    def finance_reverse_payment(payment_id):
        row = PlatformSchoolPayment.query.filter_by(id=payment_id).with_for_update().first_or_404()
        reason = request.form.get('reason', '').strip()
        if row.status == 'active' and reason:
            invoice_ids = [a.invoice_id for a in row.allocations.all()]
            row.status = 'reversed'; row.reversed_at = datetime.utcnow()
            row.reversed_by = current_user.id; row.reversal_reason = reason; db.session.flush()
            for inv in PlatformSchoolInvoice.query.filter(PlatformSchoolInvoice.id.in_(invoice_ids)).all():
                refresh_invoice_status(inv)
            db.session.commit(); log_action('reverse', 'platform_school_payment', row.id, details=reason)
            flash('تم عكس الدفعة مع إبقاء سجلها.', 'success')
        else: flash('سبب العكس مطلوب أو أن الدفعة معكوسة مسبقاً.', 'danger')
        return back(row.school_id)

    @bp.route('/finance/invoices/<int:invoice_id>/print')
    @login_required
    @super_admin_required
    def finance_print_invoice(invoice_id):
        row = PlatformSchoolInvoice.query.get_or_404(invoice_id)
        return render_template('super_admin/finance_print.html', kind='invoice', invoice=row,
                               amount_words=amount_to_words_iqd(row.total_amount))

    @bp.route('/finance/payments/<int:payment_id>/print')
    @login_required
    @super_admin_required
    def finance_print_receipt(payment_id):
        row = PlatformSchoolPayment.query.get_or_404(payment_id)
        return render_template('super_admin/finance_print.html', kind='receipt', payment=row,
                               amount_words=amount_to_words_iqd(row.amount))

    @bp.route('/finance/schools/<int:school_id>/statement')
    @login_required
    @super_admin_required
    def finance_statement(school_id):
        school = School.query.get_or_404(school_id)
        years = (AcademicYear.query.execution_options(bypass_tenant_scope=True)
                 .filter_by(school_id=school_id)
                 .order_by(AcademicYear.start_date.desc()).all())
        requested = request.args.get('year_id', type=int)
        year = _year(school_id, requested) if requested else next(
            (item for item in years if item.is_current), years[0] if years else None)
        summary = simple_school_summaries(
            [school], {school.id: year}).get(school.id, {})
        invoice_query = PlatformSchoolInvoice.query.filter_by(school_id=school_id)
        if year:
            invoice_query = invoice_query.filter_by(academic_year_id=year.id)
        return render_template('super_admin/finance_print.html', kind='statement', school=school,
            year=year, summary=summary,
            invoices=invoice_query.order_by(PlatformSchoolInvoice.issue_date).all(),
            payments=PlatformSchoolPayment.query.filter_by(school_id=school_id).order_by(
                PlatformSchoolPayment.payment_date).all(), totals=school_totals(school_id))

"""
Al-Muhandis – Salary System Blueprint
Handles: Monthly salary processing, allowances, deductions, net salary, pay slips
"""
from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, jsonify, make_response)
from flask_login import login_required, current_user
from sqlalchemy import func, and_
from datetime import date, datetime as dt
from decimal import Decimal

from app.models import db, SalaryRecord, Employee
from app.utils.decorators import (permission_required, get_current_school,
                                   get_active_year, historical_guard)
from app.utils.audit import log_action
from app.services.payroll import post_salary_expense, unpost_salary_expense

salaries_bp = Blueprint('salaries', __name__,
                         template_folder='../../templates/salaries')

ARABIC_MONTHS = [
    '', 'يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو',
    'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر'
]


# ─────────────────────────────────────────────────────────────────────────────
#  LIST
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/')
@login_required
@permission_required('manage_salaries')
def index():
    school = get_current_school()
    school_id = school.id if school else None

    today = date.today()
    month = request.args.get('month', today.month, type=int)
    year  = request.args.get('year',  today.year,  type=int)

    q = SalaryRecord.query.filter_by(month=month, year=year)
    if school_id:
        q = q.filter_by(school_id=school_id)
    records = q.join(Employee).order_by(Employee.full_name).all()

    # Totals
    total_net  = sum(float(r.net_salary)  for r in records)
    total_paid = sum(float(r.net_salary)  for r in records if r.status == 'paid')

    # Employees without salary this month
    paid_emp_ids = {r.employee_id for r in records}
    emp_q = Employee.query.filter_by(status='active')
    if school_id:
        emp_q = emp_q.filter_by(school_id=school_id)
    unpaid_employees = emp_q.filter(Employee.id.notin_(paid_emp_ids)).all()

    return render_template('salaries/index.html',
                           records=records,
                           total_net=total_net,
                           total_paid=total_paid,
                           unpaid_employees=unpaid_employees,
                           month=month, year=year,
                           arabic_months=ARABIC_MONTHS)


# ─────────────────────────────────────────────────────────────────────────────
#  GENERATE ALL SALARIES FOR A MONTH
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/generate', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def generate_month():
    school = get_current_school()
    school_id = school.id if school else None
    active_year = get_active_year(school_id) if school_id else None

    month = request.form.get('month', type=int)
    year  = request.form.get('year',  type=int)

    if not month or not year:
        flash('يرجى تحديد الشهر والسنة.', 'danger')
        return redirect(url_for('salaries.index'))
    if not school_id or not active_year:
        flash('Select a school with an active academic year before generating salaries.', 'danger')
        return redirect(url_for('salaries.index'))

    emp_q = Employee.query.filter_by(status='active')
    if school_id:
        emp_q = emp_q.filter_by(school_id=school_id)
    employees = emp_q.all()

    with db.session.no_autoflush:
        # bypass_tenant_scope=True is required because SalaryRecord is __year_scoped__,
        # so the automatic tenant filter would restrict results to the current academic year.
        # The unique constraint covers (employee_id, month, year) across ALL years,
        # so we must see records from every year to avoid false duplicates.
        existing_ids = {
            row.employee_id
            for row in (
                SalaryRecord.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(month=month, year=year)
                .all()
            )
        }

        new_recs = []
        skipped  = 0
        for emp in employees:
            if emp.id in existing_ids:
                skipped += 1
                continue
            net = float(emp.base_salary)
            new_recs.append(SalaryRecord(
                employee_id      = emp.id,
                school_id        = school_id,
                academic_year_id = active_year.id,
                month            = month,
                year             = year,
                base_salary      = emp.base_salary,
                allowances       = Decimal('0'),
                deductions       = Decimal('0'),
                net_salary       = Decimal(str(net)),
                status           = 'pending',
                created_by       = current_user.id,
            ))

        for rec in new_recs:
            db.session.add(rec)

    db.session.commit()
    created    = len(new_recs)
    month_name = ARABIC_MONTHS[month]

    if created and skipped:
        flash(
            f'تم توليد {created} سجل راتب لشهر {month_name} {year}. '
            f'تم تخطي {skipped} موظف لوجود سجلات سابقة.',
            'success'
        )
    elif created:
        flash(f'تم توليد {created} سجل راتب لشهر {month_name} {year}.', 'success')
    else:
        flash(
            f'لا توجد سجلات جديدة — جميع الموظفين ({skipped}) '
            f'لديهم رواتب مسجلة لشهر {month_name} {year} بالفعل.',
            'info'
        )
    return redirect(url_for('salaries.index', month=month, year=year))


# ─────────────────────────────────────────────────────────────────────────────
#  CREATE / EDIT SINGLE RECORD
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/create', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def create():
    school = get_current_school()
    school_id = school.id if school else None
    active_year = get_active_year(school_id) if school_id else None

    emp_q = Employee.query.filter_by(status='active')
    if school_id:
        emp_q = emp_q.filter_by(school_id=school_id)
    employees = emp_q.order_by(Employee.full_name).all()
    today = date.today()

    if request.method == 'POST':
        emp_id     = request.form.get('employee_id', type=int)
        month      = request.form.get('month', type=int)
        year       = request.form.get('year', type=int)
        allowances = Decimal(request.form.get('allowances', '0') or '0')
        deductions = Decimal(request.form.get('deductions', '0') or '0')
        notes      = request.form.get('notes', '').strip()
        if not school_id or not active_year:
            flash('Select a school with an active academic year before creating salaries.', 'danger')
            return render_template('salaries/form.html',
                                   employees=employees, today=today,
                                   arabic_months=ARABIC_MONTHS, record=None)

        # Check duplicate
        if SalaryRecord.query.filter_by(employee_id=emp_id, month=month, year=year).first():
            flash('يوجد سجل راتب لهذا الموظف في هذا الشهر بالفعل.', 'danger')
            return render_template('salaries/form.html',
                                   employees=employees, today=today,
                                   arabic_months=ARABIC_MONTHS)

        emp = Employee.query.get_or_404(emp_id)
        base   = Decimal(str(emp.base_salary))
        net    = base + allowances - deductions

        rec = SalaryRecord(
            employee_id = emp_id,
            school_id   = school_id,
            academic_year_id = active_year.id if active_year else None,
            month       = month,
            year        = year,
            base_salary = base,
            allowances  = allowances,
            deductions  = deductions,
            net_salary  = net,
            notes       = notes,
            created_by  = current_user.id,
        )
        db.session.add(rec)
        db.session.commit()
        flash('تم إنشاء سجل الراتب.', 'success')
        return redirect(url_for('salaries.index', month=month, year=year))

    return render_template('salaries/form.html',
                           employees=employees, today=today,
                           arabic_months=ARABIC_MONTHS, record=None)


@salaries_bp.route('/<int:rec_id>/edit', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def edit(rec_id):
    record    = SalaryRecord.query.get_or_404(rec_id)
    school = get_current_school()
    school_id = school.id if school else None
    if school_id and record.school_id and record.school_id != school_id:
        from flask import abort
        abort(403)

    emp_q = Employee.query.filter_by(status='active')
    if school_id:
        emp_q = emp_q.filter_by(school_id=school_id)
    employees = emp_q.order_by(Employee.full_name).all()
    today     = date.today()

    if request.method == 'POST':
        allowances = Decimal(request.form.get('allowances', '0') or '0')
        deductions = Decimal(request.form.get('deductions', '0') or '0')
        record.allowances = allowances
        record.deductions = deductions
        record.net_salary = record.base_salary + allowances - deductions
        record.notes      = request.form.get('notes', '').strip()
        db.session.commit()
        flash('تم تحديث سجل الراتب.', 'success')
        return redirect(url_for('salaries.index',
                                month=record.month, year=record.year))

    return render_template('salaries/form.html',
                           record=record, employees=employees,
                           today=today, arabic_months=ARABIC_MONTHS)


# ─────────────────────────────────────────────────────────────────────────────
#  MARK AS PAID
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/<int:rec_id>/pay', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def mark_paid(rec_id):
    """
    Phase 2: payment now also posts a linked row in the Expense ledger
    (category=رواتب, source=payroll) so the P&L report reflects payroll
    without double-counting.
    """
    record = SalaryRecord.query.get_or_404(rec_id)
    if record.status == 'paid':
        flash('تم صرف هذا الراتب سابقًا.', 'warning')
        return redirect(url_for('salaries.index',
                                 month=record.month, year=record.year))

    record.status         = 'paid'
    record.paid_date      = date.today()
    record.payment_method = request.form.get('payment_method', 'cash')

    # Link to Expense ledger
    post_salary_expense(record, user_id=current_user.id)

    db.session.commit()
    log_action('pay', 'salary', record.id,
               details=f'paid net={record.net_salary} to {record.employee.full_name}')
    flash(f'تم تسجيل صرف راتب {record.employee.full_name}.', 'success')
    return redirect(url_for('salaries.index',
                             month=record.month, year=record.year))


@salaries_bp.route('/pay-all', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def pay_all():
    """
    Bulk-pay every pending record for the given month/year and write
    one linked Expense per record. Wrapped in a single commit.
    """
    month = request.form.get('month', type=int)
    year  = request.form.get('year',  type=int)
    method = request.form.get('payment_method', 'cash')

    school = get_current_school()
    school_id = school.id if school else None

    q = SalaryRecord.query.filter_by(month=month, year=year, status='pending')
    if school_id:
        q = q.filter_by(school_id=school_id)
    records = q.all()

    for r in records:
        r.status         = 'paid'
        r.paid_date      = date.today()
        r.payment_method = method
        post_salary_expense(r, user_id=current_user.id)

    db.session.commit()
    log_action('pay_all', 'salary', None,
               details=f'{len(records)} records for {month:02d}/{year}')
    flash(f'تم صرف {len(records)} راتب دفعة واحدة.', 'success')
    return redirect(url_for('salaries.index', month=month, year=year))


@salaries_bp.route('/<int:rec_id>/unpay', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def unpay(rec_id):
    """
    Reverse a payment (drops the linked payroll Expense).
    """
    record = SalaryRecord.query.get_or_404(rec_id)
    if record.status != 'paid':
        flash('السجل غير مدفوع.', 'warning')
        return redirect(url_for('salaries.index',
                                 month=record.month, year=record.year))

    unpost_salary_expense(record, user_id=current_user.id)
    record.status    = 'pending'
    record.paid_date = None
    db.session.commit()
    log_action('unpay', 'salary', record.id,
               details=f'reversed payment for {record.employee.full_name}')
    flash('تم إلغاء صرف هذا الراتب وحذف قيد المصروف المرتبط.', 'info')
    return redirect(url_for('salaries.index',
                             month=record.month, year=record.year))


# ─────────────────────────────────────────────────────────────────────────────
#  DELETE
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/<int:rec_id>/delete', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def delete(rec_id):
    record = SalaryRecord.query.get_or_404(rec_id)
    month, year = record.month, record.year
    db.session.delete(record)
    db.session.commit()
    flash('تم حذف سجل الراتب.', 'success')
    return redirect(url_for('salaries.index', month=month, year=year))


# ─────────────────────────────────────────────────────────────────────────────
#  SALARY SLIP (HTML printable)
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/<int:rec_id>/slip')
@login_required
@permission_required('manage_salaries')
def slip(rec_id):
    record = SalaryRecord.query.get_or_404(rec_id)
    return render_template('salaries/slip.html',
                           record=record,
                           arabic_months=ARABIC_MONTHS)


# ─────────────────────────────────────────────────────────────────────────────
#  EMPLOYEE SALARY HISTORY
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/employee/<int:emp_id>')
@login_required
@permission_required('manage_salaries')
def employee_history(emp_id):
    employee = Employee.query.get_or_404(emp_id)
    school = get_current_school()
    school_id = school.id if school else None
    if school_id and employee.school_id and employee.school_id != school_id:
        from flask import abort
        abort(403)

    records = (SalaryRecord.query
               .filter_by(employee_id=emp_id)
               .order_by(SalaryRecord.year.desc(), SalaryRecord.month.desc())
               .all())
    total_paid = sum(float(r.net_salary) for r in records if r.status == 'paid')
    return render_template('salaries/employee_history.html',
                           employee=employee, records=records,
                           total_paid=total_paid,
                           arabic_months=ARABIC_MONTHS)


# ─────────────────────────────────────────────────────────────────────────────
#  API: get employee base salary for JS auto-fill
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/api/employee/<int:emp_id>/salary')
@login_required
def api_emp_salary(emp_id):
    emp = Employee.query.get_or_404(emp_id)
    return jsonify({'base_salary': float(emp.base_salary),
                    'name': emp.full_name})


# ─────────────────────────────────────────────────────────────────────────────
#  PDF SALARY SLIP (downloadable)
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/<int:rec_id>/slip/pdf')
@login_required
@permission_required('manage_salaries')
def slip_pdf(rec_id):
    from flask import Response
    from app.utils.pdf_gen import generate_salary_pdf
    record = SalaryRecord.query.get_or_404(rec_id)
    pdf_bytes = generate_salary_pdf(record)
    if not pdf_bytes:
        flash('مكتبة PDF غير متاحة — استخدم نسخة الطباعة HTML.', 'warning')
        return redirect(url_for('salaries.slip', rec_id=rec_id))
    fname = f"salary_{record.employee.employee_id}_{record.year}_{record.month:02d}.pdf"
    return Response(
        pdf_bytes,
        mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename={fname}'}
    )


# ─────────────────────────────────────────────────────────────────────────────
#  EXCEL EXPORT — salary month
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/export/excel')
@login_required
@permission_required('manage_salaries')
def export_excel():
    from flask import Response
    from app.utils.excel_export import export_salary_month
    school = get_current_school()
    school_id = school.id if school else None

    month = request.args.get('month', date.today().month, type=int)
    year  = request.args.get('year',  date.today().year,  type=int)

    q = SalaryRecord.query.filter_by(month=month, year=year)
    if school_id:
        q = q.filter_by(school_id=school_id)
    records = q.join(Employee).order_by(Employee.full_name).all()

    data = export_salary_month(records, month, year)
    if not data:
        flash('مكتبة Excel غير متاحة.', 'warning')
        return redirect(url_for('salaries.index', month=month, year=year))
    fname = f"salaries_{year}_{month:02d}.xlsx"
    return Response(
        data,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename={fname}'}
    )

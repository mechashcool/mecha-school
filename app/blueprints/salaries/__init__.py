"""
Mecha-School — Payroll / Salaries Blueprint
===========================================
Professional school payroll workflow:

  • Payroll settings (per school)        /settings
  • Salary components (allowances/deds)  /components
  • Monthly payroll generation           /generate
  • General payroll register             /            (index)
  • Payroll record review + line items   /<id>
  • Draft → Approved → Paid → Cancelled  status workflow
  • Attendance-based deductions          (via app.services.payroll)
  • Employee account statement           /employee/<id>
  • PDF salary slip + Excel register     /<id>/slip ...

Reuses the existing Expense-ledger bridge so paid salaries post a single linked
expense (category = رواتب) without double-counting in financial reports.
"""
from datetime import date, datetime as dt
from decimal import Decimal, InvalidOperation

from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, jsonify, abort, Response)
from flask_login import login_required, current_user

from app.models import (db, SalaryRecord, Employee, PayrollItem,
                        PayrollSettings, SalaryComponent)
from app.utils.decorators import (permission_required, get_current_school,
                                   get_active_year, historical_guard)
from app.utils.audit import log_action
from app.services.payroll import (post_salary_expense, unpost_salary_expense,
                                  get_settings, generate_payroll,
                                  rebuild_auto_items, apply_recurring_components,
                                  apply_attendance_items, employee_statement,
                                  _snapshot)

salaries_bp = Blueprint('salaries', __name__,
                         template_folder='../../templates/salaries')

ARABIC_MONTHS = [
    '', 'يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو',
    'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر'
]

STATUS_LABELS = {
    'draft':     'مسودة',
    'approved':  'معتمد',
    'paid':      'مدفوع',
    'cancelled': 'ملغي',
    'pending':   'مسودة',   # legacy alias
}


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _school_or_redirect():
    """Return (school, school_id) or (None, None)."""
    school = get_current_school()
    return school, (school.id if school else None)


def _parse_amount(raw, default='0') -> Decimal:
    try:
        return Decimal(str(raw if raw not in (None, '') else default))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _owned_record(rec_id) -> SalaryRecord:
    """Fetch a record and enforce school ownership (403 on mismatch)."""
    record = SalaryRecord.query.get_or_404(rec_id)
    _, school_id = _school_or_redirect()
    if school_id and record.school_id and record.school_id != school_id:
        abort(403)
    return record


# ─────────────────────────────────────────────────────────────────────────────
#  GENERAL PAYROLL REGISTER  (مسير الرواتب)
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/')
@login_required
@permission_required('manage_salaries')
def index():
    school, school_id = _school_or_redirect()

    today = date.today()
    month = request.args.get('month', today.month, type=int)
    year  = request.args.get('year',  today.year,  type=int)
    dept_filter   = (request.args.get('department') or '').strip()
    status_filter = (request.args.get('status') or '').strip()

    q = SalaryRecord.query.filter_by(month=month, year=year)
    if school_id:
        q = q.filter_by(school_id=school_id)
    records = q.join(Employee).order_by(Employee.full_name).all()

    if dept_filter:
        records = [r for r in records if (r.department or '') == dept_filter]
    if status_filter:
        records = [r for r in records if (r.status or '') == status_filter
                   or (status_filter == 'draft' and r.status == 'pending')]

    total_net      = sum(float(r.net_salary) for r in records)
    total_paid     = sum(float(r.net_salary) for r in records if r.status == 'paid')
    total_deduct   = sum(float(r.deductions or 0) for r in records)
    total_allow    = sum(float(r.allowances or 0) for r in records)
    draft_count    = sum(1 for r in records if r.status in ('draft', 'pending'))
    approved_count = sum(1 for r in records if r.status == 'approved')

    # Employees with no record this month (active payroll employees only)
    paid_emp_ids = {r.employee_id for r in records}
    emp_q = Employee.query.filter_by(status='active')
    if school_id:
        emp_q = emp_q.filter_by(school_id=school_id)
    active_emps = [e for e in emp_q.all() if e.is_payroll_active]
    missing_employees = [e for e in active_emps if e.id not in paid_emp_ids]

    departments = sorted({(e.department or '').strip()
                          for e in active_emps if (e.department or '').strip()})

    return render_template('salaries/index.html',
                           records=records,
                           total_net=total_net, total_paid=total_paid,
                           total_deduct=total_deduct, total_allow=total_allow,
                           draft_count=draft_count, approved_count=approved_count,
                           missing_employees=missing_employees,
                           departments=departments,
                           dept_filter=dept_filter, status_filter=status_filter,
                           month=month, year=year,
                           status_labels=STATUS_LABELS,
                           arabic_months=ARABIC_MONTHS)


# ─────────────────────────────────────────────────────────────────────────────
#  GENERATE PAYROLL FOR A MONTH
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/generate', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def generate_month():
    school, school_id = _school_or_redirect()
    active_year = get_active_year(school_id) if school_id else None

    month = request.form.get('month', type=int)
    year  = request.form.get('year',  type=int)

    if not month or not year:
        flash('يرجى تحديد الشهر والسنة.', 'danger')
        return redirect(url_for('salaries.index'))
    if not school_id or not active_year:
        flash('اختر مدرسة بعام دراسي فعّال قبل توليد الرواتب.', 'danger')
        return redirect(url_for('salaries.index'))

    emp_q = Employee.query.filter_by(status='active', school_id=school_id)
    employees = [e for e in emp_q.all() if e.is_payroll_active]

    created, skipped = generate_payroll(
        school, active_year, month, year, employees, user_id=current_user.id)
    db.session.commit()

    log_action('generate', 'salary', None,
               details=f'{created} payroll drafts for {month:02d}/{year}')

    month_name = ARABIC_MONTHS[month]
    if created and skipped:
        flash(f'تم توليد {created} مسودة راتب لشهر {month_name} {year}. '
              f'تم تخطي {skipped} موظف لوجود سجلات سابقة.', 'success')
    elif created:
        flash(f'تم توليد {created} مسودة راتب لشهر {month_name} {year}.', 'success')
    else:
        flash(f'لا توجد سجلات جديدة — جميع الموظفين ({skipped}) لديهم رواتب '
              f'مسجلة لشهر {month_name} {year} بالفعل.', 'info')
    return redirect(url_for('salaries.index', month=month, year=year))


# ─────────────────────────────────────────────────────────────────────────────
#  CREATE A SINGLE DRAFT RECORD MANUALLY
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/create', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def create():
    school, school_id = _school_or_redirect()
    active_year = get_active_year(school_id) if school_id else None

    emp_q = Employee.query.filter_by(status='active')
    if school_id:
        emp_q = emp_q.filter_by(school_id=school_id)
    employees = emp_q.order_by(Employee.full_name).all()
    today = date.today()

    if request.method == 'POST':
        if not school_id or not active_year:
            flash('اختر مدرسة بعام دراسي فعّال قبل إنشاء الرواتب.', 'danger')
            return redirect(url_for('salaries.index'))

        emp_id = request.form.get('employee_id', type=int)
        month  = request.form.get('month', type=int)
        year   = request.form.get('year', type=int)

        if SalaryRecord.query.execution_options(bypass_tenant_scope=True)\
                .filter_by(employee_id=emp_id, month=month, year=year).first():
            flash('يوجد سجل راتب لهذا الموظف في هذا الشهر بالفعل.', 'danger')
            return redirect(url_for('salaries.index', month=month, year=year))

        emp = Employee.query.get_or_404(emp_id)
        if school_id and emp.school_id != school_id:
            abort(403)

        record = SalaryRecord(
            employee_id      = emp_id,
            school_id        = school_id,
            academic_year_id = active_year.id,
            month            = month,
            year             = year,
            base_salary      = Decimal(str(emp.base_salary)),
            allowances       = Decimal('0'),
            deductions       = Decimal('0'),
            net_salary       = Decimal(str(emp.base_salary)),
            status           = 'draft',
            created_by       = current_user.id,
        )
        _snapshot(record, emp)
        db.session.add(record)
        db.session.flush()
        settings = get_settings(school_id)
        apply_recurring_components(record)
        apply_attendance_items(record, settings, school)
        record.recompute()
        db.session.commit()
        flash('تم إنشاء مسودة الراتب.', 'success')
        return redirect(url_for('salaries.detail', rec_id=record.id))

    return render_template('salaries/form.html',
                           employees=employees, today=today,
                           arabic_months=ARABIC_MONTHS)


# ─────────────────────────────────────────────────────────────────────────────
#  RECORD DETAIL / REVIEW  + LINE ITEM MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/<int:rec_id>')
@login_required
@permission_required('manage_salaries')
def detail(rec_id):
    record = _owned_record(rec_id)
    _, school_id = _school_or_redirect()
    components = (SalaryComponent.query
                  .filter_by(school_id=record.school_id, is_active=True)
                  .order_by(SalaryComponent.name).all()) if school_id else []
    return render_template('salaries/detail.html',
                           record=record, components=components,
                           status_labels=STATUS_LABELS,
                           arabic_months=ARABIC_MONTHS)


@salaries_bp.route('/<int:rec_id>/item/add', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def add_item(rec_id):
    record = _owned_record(rec_id)
    settings = get_settings(record.school_id)
    if record.is_locked:
        flash('لا يمكن تعديل راتب معتمد أو مدفوع.', 'warning')
        return redirect(url_for('salaries.detail', rec_id=rec_id))
    if not settings.allow_edit_draft:
        flash('تعديل المسودات معطّل في إعدادات الرواتب.', 'warning')
        return redirect(url_for('salaries.detail', rec_id=rec_id))

    item_type = request.form.get('item_type', 'addition')
    if item_type not in ('addition', 'deduction'):
        item_type = 'addition'
    name   = (request.form.get('name') or '').strip()
    amount = _parse_amount(request.form.get('amount'))
    component_id = request.form.get('component_id', type=int)

    if component_id:
        comp = SalaryComponent.query.get(component_id)
        if comp and comp.school_id == record.school_id:
            name = name or comp.name
            item_type = 'addition' if comp.component_type == 'addition' else 'deduction'
            if amount <= 0:
                amount = Decimal(str(comp.default_amount or 0))

    if not name or amount <= 0:
        flash('يرجى إدخال اسم البند ومبلغ صحيح.', 'danger')
        return redirect(url_for('salaries.detail', rec_id=rec_id))

    record.items.append(PayrollItem(
        school_id        = record.school_id,
        academic_year_id = record.academic_year_id,
        component_id     = component_id or None,
        name             = name,
        item_type        = item_type,
        amount           = amount,
        source           = 'one_time',
    ))
    record.recompute()
    db.session.commit()
    log_action('edit', 'salary', record.id, details=f'added item {name} {amount}')
    flash('تمت إضافة البند.', 'success')
    return redirect(url_for('salaries.detail', rec_id=rec_id))


@salaries_bp.route('/<int:rec_id>/item/<int:item_id>/delete', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def delete_item(rec_id, item_id):
    record = _owned_record(rec_id)
    if record.is_locked:
        flash('لا يمكن تعديل راتب معتمد أو مدفوع.', 'warning')
        return redirect(url_for('salaries.detail', rec_id=rec_id))
    item = PayrollItem.query.get_or_404(item_id)
    if item.salary_record_id != record.id:
        abort(404)
    record.items.remove(item)
    db.session.delete(item)
    record.recompute()
    db.session.commit()
    flash('تم حذف البند.', 'info')
    return redirect(url_for('salaries.detail', rec_id=rec_id))


@salaries_bp.route('/<int:rec_id>/recalculate', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def recalculate(rec_id):
    record = _owned_record(rec_id)
    if record.is_locked:
        flash('لا يمكن إعادة حساب راتب معتمد أو مدفوع.', 'warning')
        return redirect(url_for('salaries.detail', rec_id=rec_id))
    school, _ = _school_or_redirect()
    settings = get_settings(record.school_id)
    rebuild_auto_items(record, settings, school)
    db.session.commit()
    flash('تمت إعادة حساب البنود التلقائية (البدلات الثابتة وخصومات الحضور).', 'success')
    return redirect(url_for('salaries.detail', rec_id=rec_id))


@salaries_bp.route('/<int:rec_id>/notes', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def update_notes(rec_id):
    record = _owned_record(rec_id)
    record.notes = (request.form.get('notes') or '').strip()
    db.session.commit()
    return redirect(url_for('salaries.detail', rec_id=rec_id))


# ─────────────────────────────────────────────────────────────────────────────
#  STATUS WORKFLOW : approve / pay / cancel / unpay
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/<int:rec_id>/approve', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def approve(rec_id):
    record = _owned_record(rec_id)
    if record.status not in ('draft', 'pending'):
        flash('يمكن اعتماد المسودات فقط.', 'warning')
        return redirect(url_for('salaries.detail', rec_id=rec_id))
    record.status = 'approved'
    record.approved_by = current_user.id
    record.approved_at = dt.utcnow()
    db.session.commit()
    log_action('approve', 'salary', record.id,
               details=f'approved net={record.net_salary} for {record.employee_name}')
    flash(f'تم اعتماد راتب {record.employee_name}.', 'success')
    return redirect(url_for('salaries.detail', rec_id=rec_id))


@salaries_bp.route('/<int:rec_id>/pay', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def mark_paid(rec_id):
    record = _owned_record(rec_id)
    if record.status == 'paid':
        flash('تم صرف هذا الراتب سابقًا.', 'warning')
        return redirect(url_for('salaries.detail', rec_id=rec_id))
    if record.status not in ('approved',):
        flash('يجب اعتماد الراتب قبل صرفه.', 'warning')
        return redirect(url_for('salaries.detail', rec_id=rec_id))

    record.status         = 'paid'
    record.paid_date      = date.today()
    record.payment_method = request.form.get('payment_method', 'cash')
    post_salary_expense(record, user_id=current_user.id)
    db.session.commit()
    log_action('pay', 'salary', record.id,
               details=f'paid net={record.net_salary} to {record.employee_name}')
    flash(f'تم تسجيل صرف راتب {record.employee_name}.', 'success')
    return redirect(url_for('salaries.detail', rec_id=rec_id))


@salaries_bp.route('/<int:rec_id>/cancel', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def cancel(rec_id):
    record = _owned_record(rec_id)
    if record.status == 'paid':
        flash('لا يمكن إلغاء راتب مدفوع — استخدم إلغاء الصرف أولاً.', 'danger')
        return redirect(url_for('salaries.detail', rec_id=rec_id))
    if record.status == 'cancelled':
        flash('السجل ملغى بالفعل.', 'info')
        return redirect(url_for('salaries.detail', rec_id=rec_id))
    record.status = 'cancelled'
    record.cancelled_at = dt.utcnow()
    db.session.commit()
    log_action('cancel', 'salary', record.id,
               details=f'cancelled payroll for {record.employee_name}')
    flash('تم إلغاء سجل الراتب.', 'info')
    return redirect(url_for('salaries.detail', rec_id=rec_id))


@salaries_bp.route('/<int:rec_id>/unpay', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def unpay(rec_id):
    record = _owned_record(rec_id)
    if record.status != 'paid':
        flash('السجل غير مدفوع.', 'warning')
        return redirect(url_for('salaries.detail', rec_id=rec_id))
    unpost_salary_expense(record, user_id=current_user.id)
    record.status    = 'approved'
    record.paid_date = None
    db.session.commit()
    log_action('unpay', 'salary', record.id,
               details=f'reversed payment for {record.employee_name}')
    flash('تم إلغاء الصرف وحذف قيد المصروف المرتبط (عاد إلى «معتمد»).', 'info')
    return redirect(url_for('salaries.detail', rec_id=rec_id))


# ── Bulk actions ──────────────────────────────────────────────────────────────

@salaries_bp.route('/approve-all', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def approve_all():
    month = request.form.get('month', type=int)
    year  = request.form.get('year',  type=int)
    _, school_id = _school_or_redirect()

    q = SalaryRecord.query.filter(SalaryRecord.month == month,
                                  SalaryRecord.year == year,
                                  SalaryRecord.status.in_(['draft', 'pending']))
    if school_id:
        q = q.filter_by(school_id=school_id)
    records = q.all()
    for r in records:
        r.status = 'approved'
        r.approved_by = current_user.id
        r.approved_at = dt.utcnow()
    db.session.commit()
    log_action('approve_all', 'salary', None,
               details=f'{len(records)} records for {month:02d}/{year}')
    flash(f'تم اعتماد {len(records)} راتب.', 'success')
    return redirect(url_for('salaries.index', month=month, year=year))


@salaries_bp.route('/pay-all', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def pay_all():
    month  = request.form.get('month', type=int)
    year   = request.form.get('year',  type=int)
    method = request.form.get('payment_method', 'cash')
    _, school_id = _school_or_redirect()

    q = SalaryRecord.query.filter_by(month=month, year=year, status='approved')
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
    flash(f'تم صرف {len(records)} راتب معتمد دفعة واحدة.', 'success')
    return redirect(url_for('salaries.index', month=month, year=year))


# ─────────────────────────────────────────────────────────────────────────────
#  DELETE  (draft / cancelled only)
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/<int:rec_id>/delete', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def delete(rec_id):
    record = _owned_record(rec_id)
    if record.status in ('approved', 'paid'):
        flash('لا يمكن حذف راتب معتمد أو مدفوع — يمكنك إلغاؤه بدلاً من ذلك.', 'danger')
        return redirect(url_for('salaries.detail', rec_id=rec_id))
    month, year = record.month, record.year
    db.session.delete(record)
    db.session.commit()
    flash('تم حذف سجل الراتب.', 'success')
    return redirect(url_for('salaries.index', month=month, year=year))


# ─────────────────────────────────────────────────────────────────────────────
#  PAYROLL SETTINGS  (إعدادات الرواتب)
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/settings', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def settings():
    school, school_id = _school_or_redirect()
    if not school_id:
        flash('اختر مدرسة أولاً.', 'danger')
        return redirect(url_for('salaries.index'))

    cfg = get_settings(school_id)

    if request.method == 'POST':
        f = request.form
        cfg.payroll_calculation_day = f.get('payroll_calculation_day', type=int) or 28
        cfg.default_payment_day     = f.get('default_payment_day', type=int) or 1
        cfg.allow_edit_draft        = bool(f.get('allow_edit_draft'))

        cfg.attendance_deduction_enabled = bool(f.get('attendance_deduction_enabled'))
        cfg.absence_method        = f.get('absence_method', 'fixed')
        cfg.absence_fixed_amount  = _parse_amount(f.get('absence_fixed_amount'))
        cfg.monthly_working_days  = f.get('monthly_working_days', type=int) or 26

        cfg.late_deduction_enabled = bool(f.get('late_deduction_enabled'))
        cfg.late_method        = f.get('late_method', 'fixed_each')
        cfg.late_amount        = _parse_amount(f.get('late_amount'))
        cfg.late_allowed_count = f.get('late_allowed_count', type=int) or 0
        cfg.late_group_size    = f.get('late_group_size', type=int) or 3

        cfg.early_leave_deduction_enabled = bool(f.get('early_leave_deduction_enabled'))
        cfg.early_leave_amount = _parse_amount(f.get('early_leave_amount'))

        cfg.unpaid_leave_deduction_enabled = bool(f.get('unpaid_leave_deduction_enabled'))

        db.session.commit()
        log_action('edit', 'payroll_settings', cfg.id, details='updated payroll settings')
        flash('تم حفظ إعدادات الرواتب.', 'success')
        return redirect(url_for('salaries.settings'))

    return render_template('salaries/settings.html', cfg=cfg)


# ─────────────────────────────────────────────────────────────────────────────
#  SALARY COMPONENTS  (بنود الراتب)
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/components')
@login_required
@permission_required('manage_salaries')
def components():
    school, school_id = _school_or_redirect()
    q = SalaryComponent.query
    if school_id:
        q = q.filter_by(school_id=school_id)
    comps = q.order_by(SalaryComponent.component_type,
                       SalaryComponent.name).all()

    emp_q = Employee.query.filter_by(status='active')
    if school_id:
        emp_q = emp_q.filter_by(school_id=school_id)
    employees = emp_q.order_by(Employee.full_name).all()

    return render_template('salaries/components.html',
                           components=comps, employees=employees)


@salaries_bp.route('/components/save', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def component_save():
    school, school_id = _school_or_redirect()
    if not school_id:
        flash('اختر مدرسة أولاً.', 'danger')
        return redirect(url_for('salaries.components'))

    f = request.form
    comp_id = f.get('id', type=int)
    name = (f.get('name') or '').strip()
    if not name:
        flash('يرجى إدخال اسم البند.', 'danger')
        return redirect(url_for('salaries.components'))

    scope = f.get('scope', 'general')
    employee_id = f.get('employee_id', type=int) if scope == 'employee' else None
    if scope == 'employee' and employee_id:
        emp = Employee.query.get(employee_id)
        if not emp or (school_id and emp.school_id != school_id):
            abort(403)

    if comp_id:
        comp = SalaryComponent.query.get_or_404(comp_id)
        if school_id and comp.school_id != school_id:
            abort(403)
    else:
        comp = SalaryComponent(school_id=school_id)
        db.session.add(comp)

    comp.name           = name
    comp.component_type = f.get('component_type', 'addition')
    comp.amount_type    = f.get('amount_type', 'fixed')
    comp.default_amount = _parse_amount(f.get('default_amount'))
    comp.recurrence     = f.get('recurrence', 'recurring')
    comp.scope          = scope
    comp.employee_id    = employee_id
    comp.is_active      = bool(f.get('is_active'))
    comp.notes          = (f.get('notes') or '').strip()
    db.session.commit()
    flash('تم حفظ بند الراتب.', 'success')
    return redirect(url_for('salaries.components'))


@salaries_bp.route('/components/<int:comp_id>/delete', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_salaries')
def component_delete(comp_id):
    comp = SalaryComponent.query.get_or_404(comp_id)
    _, school_id = _school_or_redirect()
    if school_id and comp.school_id != school_id:
        abort(403)
    db.session.delete(comp)
    db.session.commit()
    flash('تم حذف بند الراتب.', 'info')
    return redirect(url_for('salaries.components'))


# ─────────────────────────────────────────────────────────────────────────────
#  EMPLOYEE ACCOUNT STATEMENT  (كشف حساب موظف)
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/employee/<int:emp_id>')
@login_required
@permission_required('manage_salaries')
def employee_history(emp_id):
    employee = Employee.query.get_or_404(emp_id)
    _, school_id = _school_or_redirect()
    if school_id and employee.school_id and employee.school_id != school_id:
        abort(403)

    year = request.args.get('year', type=int)
    statement = employee_statement(employee, year=year)

    records = (SalaryRecord.query
               .execution_options(bypass_tenant_scope=True)
               .filter_by(employee_id=emp_id, school_id=employee.school_id)
               .order_by(SalaryRecord.year.desc(), SalaryRecord.month.desc())
               .all())
    years = sorted({r.year for r in records}, reverse=True)

    return render_template('salaries/employee_history.html',
                           employee=employee, statement=statement,
                           records=records, years=years, year=year,
                           status_labels=STATUS_LABELS,
                           arabic_months=ARABIC_MONTHS)


# ─────────────────────────────────────────────────────────────────────────────
#  SALARY SLIP  (HTML + PDF)
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/<int:rec_id>/slip')
@login_required
@permission_required('manage_salaries')
def slip(rec_id):
    record = _owned_record(rec_id)
    return render_template('salaries/slip.html',
                           record=record, status_labels=STATUS_LABELS,
                           arabic_months=ARABIC_MONTHS)


@salaries_bp.route('/<int:rec_id>/slip/pdf')
@login_required
@permission_required('manage_salaries')
def slip_pdf(rec_id):
    from app.utils.pdf_gen import generate_salary_pdf
    record = _owned_record(rec_id)
    pdf_bytes = generate_salary_pdf(record)
    if not pdf_bytes:
        flash('مكتبة PDF غير متاحة — استخدم نسخة الطباعة HTML.', 'warning')
        return redirect(url_for('salaries.slip', rec_id=rec_id))
    fname = f"salary_{record.employee.employee_id}_{record.year}_{record.month:02d}.pdf"
    return Response(pdf_bytes, mimetype='application/pdf',
                    headers={'Content-Disposition': f'attachment; filename={fname}'})


# ─────────────────────────────────────────────────────────────────────────────
#  EXCEL EXPORT — payroll register
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/export/excel')
@login_required
@permission_required('manage_salaries')
def export_excel():
    from app.utils.excel_export import export_salary_month
    _, school_id = _school_or_redirect()

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
        headers={'Content-Disposition': f'attachment; filename={fname}'})


# ─────────────────────────────────────────────────────────────────────────────
#  API: employee base salary (JS auto-fill)
# ─────────────────────────────────────────────────────────────────────────────

@salaries_bp.route('/api/employee/<int:emp_id>/salary')
@login_required
def api_emp_salary(emp_id):
    emp = Employee.query.get_or_404(emp_id)
    return jsonify({'base_salary': float(emp.base_salary), 'name': emp.full_name})

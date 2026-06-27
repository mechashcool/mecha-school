"""
Mecha-School — Payroll service
==============================
Bridges payroll (SalaryRecord) with the general expense ledger so the
financial report isn't double-counted.

Contract:
    Whenever a SalaryRecord transitions to status='paid' and does NOT yet
    have a linked Expense, `post_salary_expense()` creates an Expense
    row in the auto-created "رواتب" (Salaries) category with source='payroll'
    and writes the new expense.id back to salary_record.expense_id.

All callers MUST wrap this in a db.session.commit() of their own — the
helper only stages objects on the session.
"""
import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.models import (db, ExpenseCategory, Expense, SalaryRecord, PayrollItem,
                        PayrollSettings, SalaryComponent, EmployeeAttendance)
from app.utils.audit import log_action


SYSTEM_SALARY_CATEGORY = 'رواتب'

ZERO = Decimal('0')


def _d(value) -> Decimal:
    """Coerce any numeric/None to Decimal."""
    if value is None or value == '':
        return ZERO
    return Decimal(str(value))


def ensure_salaries_category(school_id=None):
    """
    Return the system Expense category used to post payroll runs.
    Creates it if missing so fresh databases still work.
    """
    if school_id is None:
        from app.utils.scoping import current_school_id
        school_id = current_school_id()
    if not school_id:
        raise ValueError('Cannot create salary expense category without school_id')

    cat = (
        ExpenseCategory.query.execution_options(bypass_tenant_scope=True)
        .filter_by(name=SYSTEM_SALARY_CATEGORY, school_id=school_id)
        .first()
    )
    if cat is None:
        cat = ExpenseCategory(
            name=SYSTEM_SALARY_CATEGORY,
            school_id=school_id,
            is_system=True,
        )
        db.session.add(cat)
        db.session.flush()  # make sure cat.id is available
    return cat


def post_salary_expense(record: SalaryRecord, user_id: int = None):
    """
    Idempotent: if `record.expense_id` is already set, does nothing.
    Otherwise creates a linked Expense for the record's net_salary and
    writes an AuditLog row.
    Returns the Expense object (existing or new).
    """
    if record.expense_id:
        return Expense.query.get(record.expense_id)

    category = ensure_salaries_category(record.school_id)

    employee_name = record.employee.full_name if record.employee else f'موظف #{record.employee_id}'
    description = (
        f'راتب {employee_name} — {record.month:02d}/{record.year}'
    )

    expense = Expense(
        category_id  = category.id,
        school_id    = record.school_id,
        academic_year_id = record.academic_year_id,
        amount       = Decimal(str(record.net_salary)),
        date         = record.paid_date or date.today(),
        description  = description,
        payment_method = record.payment_method or 'cash',
        reference_no = f'SAL-{record.id}',
        source       = 'payroll',
        created_by   = user_id,
    )
    db.session.add(expense)
    db.session.flush()  # need expense.id
    record.expense_id = expense.id

    log_action(
        'create', 'expense', expense.id,
        details=f'payroll → expense linked for salary #{record.id} ({employee_name})'
    )
    return expense


def unpost_salary_expense(record: SalaryRecord, user_id: int = None):
    """
    Used when a payroll payment is reversed.  Deletes the linked Expense
    (only if source='payroll' to avoid wiping a hand-entered row).
    """
    if not record.expense_id:
        return
    expense = Expense.query.get(record.expense_id)
    if expense and expense.source == 'payroll':
        db.session.delete(expense)
        log_action(
            'delete', 'expense', expense.id,
            details=f'payroll expense reversed for salary #{record.id}'
        )
    record.expense_id = None


# ═══════════════════════════════════════════════════════════════════════════════
#  PAYROLL GENERATION, COMPONENTS & ATTENDANCE DEDUCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def get_settings(school_id) -> PayrollSettings:
    """Return (creating if needed) the payroll settings for a school."""
    return PayrollSettings.get_or_create(school_id)


def _month_range(month: int, year: int):
    """First and last calendar day of a given month/year."""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _minutes_between(t_from, t_to) -> int:
    """Whole minutes from t_from to t_to (Time objects); 0 if not positive."""
    if not t_from or not t_to:
        return 0
    a = datetime.combine(date.today(), t_from)
    b = datetime.combine(date.today(), t_to)
    delta = (b - a).total_seconds() / 60.0
    return int(delta) if delta > 0 else 0


def compute_attendance(record: SalaryRecord, settings: PayrollSettings, school,
                       working_days=None) -> dict:
    """
    Calculate attendance-based deduction details for one payroll record's month.

    ``working_days`` may be a pre-computed list of working days for the same
    month/year/school (identical for every employee in a generation run) so the
    holiday/weekend calendar is not recomputed per employee. When None it is
    computed here, exactly as before.

    Uses EmployeeAttendance (NOT student attendance). Mirrors the HR attendance
    report logic (calculate_employee_stats): iterates over every working day in
    the effective range and treats any day with no DB record as a virtual absence.
    The effective range is capped at today so future working days in the current
    month are not counted as absences when payroll is generated mid-month.
    Returns a dict with counts and Decimal deduction amounts.
    """
    from app.utils.employee_attendance_helper import get_working_days

    result = {
        'absence_days': 0, 'late_count': 0, 'early_leave_count': 0,
        'absence_deduction': ZERO, 'late_deduction': ZERO,
        'early_leave_deduction': ZERO,
    }
    if not settings or not settings.attendance_deduction_enabled:
        return result

    date_from, date_to = _month_range(record.month, record.year)
    # Cap at today: working days that haven't occurred yet must not be counted
    # as virtual absences (matches what the HR attendance report shows when the
    # user picks the same date range).
    effective_to = min(date_to, date.today())
    if effective_to < date_from:
        return result  # salary month is entirely in the future

    if working_days is None:
        working_days = get_working_days(date_from, effective_to, school)
    if not working_days:
        return result

    # One bulk query for explicit EmployeeAttendance records within the effective
    # range.  bypass_tenant_scope so it works regardless of the active view year.
    rows = (
        EmployeeAttendance.query
        .execution_options(bypass_tenant_scope=True)
        .filter(
            EmployeeAttendance.school_id == record.school_id,
            EmployeeAttendance.employee_id == record.employee_id,
            EmployeeAttendance.date >= date_from,
            EmployeeAttendance.date <= effective_to,
        )
        .all()
    )
    records_by_date = {r.date: r for r in rows}

    late_threshold = getattr(school, 'att_late_threshold', None)
    departure_time = getattr(school, 'att_departure_time', None)

    absence_days = late_count = early_leave_count = 0
    total_late_minutes = 0

    # Iterate over working days exactly as calculate_employee_stats() does:
    # a missing record on a working day is a virtual absence.
    # Days with status='on_leave' are approved leave — no deduction of any kind.
    for d in working_days:
        rec = records_by_date.get(d)
        if rec is None:
            absence_days += 1  # virtual absence
            continue
        if rec.status == 'on_leave':
            continue  # approved leave — exempt from all attendance deductions
        if rec.status == 'absent':
            absence_days += 1
        elif rec.status == 'late':
            late_count += 1
            total_late_minutes += _minutes_between(late_threshold, rec.check_in)
        # Early leave: checked out before the configured departure time.
        if (departure_time and rec.check_out
                and rec.check_out < departure_time):
            early_leave_count += 1

    # ── Absence deduction ─────────────────────────────────────────────────────
    absence_deduction = ZERO
    if settings.absence_method == 'divider':
        wd = settings.monthly_working_days or len(working_days)
        if wd > 0:
            per_day = _d(record.base_salary) / Decimal(wd)
            absence_deduction = (per_day * absence_days).quantize(Decimal('1'))
    else:  # 'fixed'
        absence_deduction = _d(settings.absence_fixed_amount) * absence_days

    # ── Late deduction ────────────────────────────────────────────────────────
    late_deduction = ZERO
    if settings.late_deduction_enabled:
        allowed = settings.late_allowed_count or 0
        if settings.late_method == 'per_minute':
            late_deduction = _d(settings.late_amount) * total_late_minutes
        else:
            effective = max(0, late_count - allowed)
            if settings.late_method == 'per_group':
                group = settings.late_group_size or 1
                late_deduction = _d(settings.late_amount) * (effective // group)
            else:  # 'fixed_each'
                late_deduction = _d(settings.late_amount) * effective

    # ── Early-leave deduction ──────────────────────────────────────────────────
    early_leave_deduction = ZERO
    if settings.early_leave_deduction_enabled:
        early_leave_deduction = _d(settings.early_leave_amount) * early_leave_count

    result.update(
        absence_days=absence_days,
        late_count=late_count,
        early_leave_count=early_leave_count,
        absence_deduction=absence_deduction,
        late_deduction=late_deduction,
        early_leave_deduction=early_leave_deduction,
    )
    return result


def _clear_generated_items(record: SalaryRecord):
    """Remove auto-generated (recurring/attendance) items, keep manual/one-time."""
    keep = []
    for item in list(record.items):
        if item.source in ('recurring', 'attendance'):
            record.items.remove(item)
            db.session.delete(item)
        else:
            keep.append(item)
    return keep


def apply_recurring_components(record: SalaryRecord, comps=None):
    """Add PayrollItem lines for every active recurring component that applies
    to this record's employee (general scope + employee-specific).

    ``comps`` may be a pre-fetched list of the school's active recurring
    components so a bulk generation run does not re-query them once per
    employee. When None (single-record callers), they are loaded as before.
    """
    if comps is None:
        comps = (
            SalaryComponent.query
            .execution_options(bypass_tenant_scope=True)
            .filter(
                SalaryComponent.school_id == record.school_id,
                SalaryComponent.is_active.is_(True),
                SalaryComponent.recurrence == 'recurring',
            )
            .all()
        )
    for comp in comps:
        if comp.scope == 'employee' and comp.employee_id != record.employee_id:
            continue
        record.items.append(PayrollItem(
            school_id        = record.school_id,
            academic_year_id = record.academic_year_id,
            component_id     = comp.id,
            name             = comp.name,
            item_type        = 'addition' if comp.component_type == 'addition' else 'deduction',
            amount           = _d(comp.default_amount),
            source           = 'recurring',
        ))


def apply_attendance_items(record: SalaryRecord, settings: PayrollSettings, school,
                           working_days=None):
    """Compute attendance deductions and store them as 'attendance' line items
    plus informational counts on the record.

    ``working_days`` is forwarded to compute_attendance() so a bulk run can
    share one pre-computed working-day calendar across all employees.
    """
    stats = compute_attendance(record, settings, school, working_days=working_days)
    record.absence_days      = stats['absence_days']
    record.late_count        = stats['late_count']
    record.early_leave_count = stats['early_leave_count']

    deductions = [
        ('خصم غياب',        stats['absence_deduction']),
        ('خصم تأخير',       stats['late_deduction']),
        ('خصم خروج مبكر',   stats['early_leave_deduction']),
    ]
    for name, amount in deductions:
        if amount and amount > 0:
            record.items.append(PayrollItem(
                school_id        = record.school_id,
                academic_year_id = record.academic_year_id,
                name             = name,
                item_type        = 'deduction',
                amount           = amount,
                source           = 'attendance',
            ))


def rebuild_auto_items(record: SalaryRecord, settings: PayrollSettings, school):
    """Refresh recurring + attendance items for a DRAFT record, then recompute.
    Manual / one-time items are preserved."""
    _clear_generated_items(record)
    apply_recurring_components(record)
    apply_attendance_items(record, settings, school)
    record.recompute()


def _snapshot(record: SalaryRecord, employee):
    record.employee_name_snapshot = employee.full_name
    record.job_title_snapshot     = employee.job_title
    record.department_snapshot    = employee.department


def generate_payroll(school, active_year, month: int, year: int,
                     employees, user_id=None):
    """
    Create DRAFT SalaryRecords for the given month/year for each active employee
    that does not already have a record (in ANY academic year — the unique
    constraint spans all years). Applies recurring components and attendance
    deductions. Returns (created_count, skipped_count).
    """
    settings = get_settings(school.id)

    # Existing employee_ids for this month/year across all years (bypass year
    # scope so the unique-constraint check is accurate).
    existing_ids = {
        row.employee_id
        for row in (
            SalaryRecord.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(month=month, year=year, school_id=school.id)
            .all()
        )
    }

    # ── Hoist per-run work out of the per-employee loop ─────────────────────────
    # The active recurring components and the working-day calendar are identical
    # for every employee in this month/year/school, so compute them once instead
    # of re-querying / recomputing per employee. Behaviour is unchanged.
    recurring_comps = (
        SalaryComponent.query
        .execution_options(bypass_tenant_scope=True)
        .filter(
            SalaryComponent.school_id == school.id,
            SalaryComponent.is_active.is_(True),
            SalaryComponent.recurrence == 'recurring',
        )
        .all()
    )

    shared_working_days = None
    if settings and settings.attendance_deduction_enabled:
        from app.utils.employee_attendance_helper import get_working_days
        _df, _dt = _month_range(month, year)
        _eff_to = min(_dt, date.today())
        shared_working_days = (get_working_days(_df, _eff_to, school)
                               if _eff_to >= _df else [])

    created = skipped = 0
    with db.session.no_autoflush:
        for emp in employees:
            if emp.id in existing_ids:
                skipped += 1
                continue
            record = SalaryRecord(
                employee_id      = emp.id,
                school_id        = school.id,
                academic_year_id = active_year.id,
                month            = month,
                year             = year,
                base_salary      = _d(emp.base_salary),
                allowances       = ZERO,
                deductions       = ZERO,
                net_salary       = _d(emp.base_salary),
                status           = 'draft',
                created_by       = user_id,
            )
            _snapshot(record, emp)
            db.session.add(record)
            db.session.flush()  # need record.id + academic_year_id for items
            apply_recurring_components(record, comps=recurring_comps)
            apply_attendance_items(record, settings, school,
                                   working_days=shared_working_days)
            record.recompute()
            created += 1

    return created, skipped


# ═══════════════════════════════════════════════════════════════════════════════
#  EMPLOYEE ACCOUNT STATEMENT
# ═══════════════════════════════════════════════════════════════════════════════

def employee_statement(employee, year: int = None) -> dict:
    """
    Build a ledger-style account statement for one employee.

    Convention (school-friendly):
      * A salary entitlement (net payable for a month) CREDITS the employee
        (increases what the school owes them).
      * A salary payment DEBITS the employee (reduces what the school owes).
      * Running balance = total owed to employee − total paid.

    Cancelled records are excluded. Returns dict with rows + totals.
    """
    q = (SalaryRecord.query
         .execution_options(bypass_tenant_scope=True)
         .filter(SalaryRecord.employee_id == employee.id,
                 SalaryRecord.school_id == employee.school_id))
    if year:
        q = q.filter(SalaryRecord.year == year)
    records = q.order_by(SalaryRecord.year.asc(), SalaryRecord.month.asc()).all()

    events = []  # (sort_date, type, description, credit, debit, ref, month, year)
    for r in records:
        if r.status == 'cancelled':
            continue
        entitle_date = date(r.year, r.month, 1)
        events.append({
            'date': entitle_date,
            'type': 'استحقاق راتب',
            'description': f'صافي راتب {r.month:02d}/{r.year}',
            'credit': _d(r.net_salary),
            'debit': ZERO,
            'ref': f'SAL-{r.id}',
            'month': r.month, 'year': r.year,
            'status': r.status,
        })
        if r.status == 'paid':
            events.append({
                'date': r.paid_date or entitle_date,
                'type': 'صرف راتب',
                'description': f'دفع راتب {r.month:02d}/{r.year}',
                'credit': ZERO,
                'debit': _d(r.net_salary),
                'ref': f'PAY-{r.id}',
                'month': r.month, 'year': r.year,
                'status': r.status,
            })

    events.sort(key=lambda e: (e['date'], 0 if e['credit'] else 1))

    balance = ZERO
    rows = []
    total_credit = total_debit = ZERO
    for ev in events:
        balance += ev['credit'] - ev['debit']
        total_credit += ev['credit']
        total_debit += ev['debit']
        rows.append({**ev, 'balance': balance})

    return {
        'rows': rows,
        'total_credit': total_credit,   # total owed to employee
        'total_debit': total_debit,     # total paid to employee
        'balance': balance,             # >0 ⇒ school owes employee
    }

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
from datetime import date
from decimal import Decimal

from app.models import db, ExpenseCategory, Expense, SalaryRecord
from app.utils.audit import log_action


SYSTEM_SALARY_CATEGORY = 'رواتب'


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

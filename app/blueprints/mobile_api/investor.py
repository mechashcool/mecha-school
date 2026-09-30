"""
Mobile API — Investor (read-only)
=================================
GET /api/mobile/v1/investor/dashboard   full KPI dashboard for the investor's school
GET /api/mobile/v1/investor/revenues    read-only revenue list
GET /api/mobile/v1/investor/expenses    read-only expense list
GET /api/mobile/v1/investor/employees/attendance
                                        active employees + one day's attendance
GET /api/mobile/v1/investor/employees/<id>/attendance
                                        one employee's paginated attendance history

All endpoints require:
  Authorization: Bearer <access_token>   for an `investor_viewer` account.

Isolation
---------
The investor's school_id is taken from the authenticated server-side User row
(set_mobile_request_scope), so the ORM tenant guard forces school_id scoping on
every query. Each query below also filters explicitly on g.mobile_user.school_id
as a second barrier. There are no write endpoints for this role.
"""
from datetime import date
from datetime import datetime as _dt

from flask import g, request
from sqlalchemy import func, extract

from app.models import db, Revenue, Expense, RevenueCategory, ExpenseCategory
from .utils import jwt_required, role_required, ok, err

from . import mobile_api_bp


def _sid():
    return g.mobile_user.school_id


def _parse_int_arg(name):
    """
    Read an optional integer query arg.
    Returns (value, error_response). value is None when the arg is absent/empty.
    error_response is a Flask response tuple when the supplied value is not a
    valid integer — caller must return it immediately.
    """
    raw = request.args.get(name)
    if raw is None or raw.strip() == '':
        return None, None
    try:
        return int(raw), None
    except ValueError:
        return None, err(f'invalid {name}')


def _parse_date_arg(name):
    """
    Read an optional yyyy-MM-dd query arg.
    Returns (value, error_response), mirroring _parse_int_arg().
    """
    raw = request.args.get(name)
    if not raw:
        return None, None
    try:
        return _dt.strptime(raw, '%Y-%m-%d').date(), None
    except ValueError:
        return None, err(f'invalid {name} — use yyyy-MM-dd')


def _category_options(model, sid):
    """School-scoped category list for filter_options — never paginated."""
    rows = (model.query.filter(model.school_id == sid)
            .order_by(model.name).all())
    return [{'id': c.id, 'name': c.name} for c in rows]


@mobile_api_bp.route('/investor/dashboard', methods=['GET'])
@jwt_required()
@role_required('investor_viewer')
def investor_dashboard():
    """
    Full KPI dashboard reusing the same _build_dashboard_context() helper as
    the web investor dashboard. jwt_required() calls login_user() so
    current_user is populated; get_current_school() resolves to the investor's
    own school (non-super-admin path) and all ORM queries are school-scoped.

    Backward-compatible: old top-level fields (year, total_revenue, total_expense,
    balance, monthly_revenue, monthly_expense) are preserved. New fields are added
    under 'school', 'academic_year', 'kpis', 'charts', 'recent_students', and
    'recent_notifications'.
    """
    from app.blueprints.admin import _build_dashboard_context

    year = request.args.get('year', date.today().year, type=int)
    sid  = _sid()

    # Reuse the exact same context helper the web investor dashboard calls.
    ctx         = _build_dashboard_context()
    stats       = ctx['stats']
    school      = ctx['school']
    active_year = ctx['active_year']

    # Year-based 12-month totals (keyed by year query param).
    # Kept for backward compatibility with existing Flutter fields
    # monthly_revenue / monthly_expense (12-element arrays).
    # These differ from the rolling 6-month series in stats/charts which
    # are what the web dashboard chart displays.
    rev_total = float(
        db.session.query(func.coalesce(func.sum(Revenue.amount), 0))
        .execution_options(include_all_years=True)
        .filter(Revenue.school_id == sid, extract('year', Revenue.date) == year,
                Revenue.refunded_at.is_(None))
        .scalar() or 0
    )
    exp_total = float(
        db.session.query(func.coalesce(func.sum(Expense.amount), 0))
        .execution_options(include_all_years=True)
        .filter(Expense.school_id == sid, extract('year', Expense.date) == year)
        .scalar() or 0
    )

    rev_by_month = {
        int(m): float(t) for m, t in
        db.session.query(extract('month', Revenue.date), func.sum(Revenue.amount))
        .execution_options(include_all_years=True)
        .filter(Revenue.school_id == sid, extract('year', Revenue.date) == year,
                Revenue.refunded_at.is_(None))
        .group_by(extract('month', Revenue.date)).all()
    }
    exp_by_month = {
        int(m): float(t) for m, t in
        db.session.query(extract('month', Expense.date), func.sum(Expense.amount))
        .execution_options(include_all_years=True)
        .filter(Expense.school_id == sid, extract('year', Expense.date) == year)
        .group_by(extract('month', Expense.date)).all()
    }

    monthly_revenue_12 = [rev_by_month.get(m, 0) for m in range(1, 13)]
    monthly_expense_12 = [exp_by_month.get(m, 0) for m in range(1, 13)]
    monthly_net_12     = [r - e for r, e in zip(monthly_revenue_12, monthly_expense_12)]

    def _serialize_student(s):
        grade_name = section_name = None
        try:
            if s.section:
                section_name = s.section.name
                if s.section.grade:
                    grade_name = s.section.grade.name
        except Exception:
            pass
        return {
            'id':             s.id,
            'name':           s.full_name,
            'student_number': s.student_id,
            'grade':          grade_name,
            'section':        section_name,
            'status':         s.status,
        }

    def _serialize_notif(n):
        return {
            'id':         n.id,
            'title':      n.title,
            'body':       n.body,
            'type':       n.ntype,
            'created_at': n.created_at.isoformat() if n.created_at else None,
        }

    return ok(
        # ── Backward-compatible top-level fields ─────────────────────────────────
        year            = year,
        total_revenue   = rev_total,
        total_expense   = exp_total,
        balance         = rev_total - exp_total,
        monthly_revenue = monthly_revenue_12,
        monthly_expense = monthly_expense_12,

        # ── School / year context ────────────────────────────────────────────────
        school = {
            'id':            school.id             if school else None,
            'name':          school.school_name    if school else None,
            'name_ar':       school.school_name_ar if school else None,
            'currency':      school.currency_symbol if school else None,
            'currency_code': school.currency_code   if school else None,
        },
        academic_year = {
            'id':   active_year.id   if active_year else None,
            'name': active_year.name if active_year else None,
        },

        # ── KPI cards ────────────────────────────────────────────────────────────
        kpis = {
            # Student / employee
            'active_students':       stats['total_students'],
            'active_employees':      stats['total_employees'],
            # Attendance today
            'attendance_today':      stats['present_today'],
            'absence_today':         stats['absent_today'],
            # Fees
            'fees_collected_today':  stats['fees_collected_today'],
            'overdue_installments':  stats['overdue_installments'],
            # Current-month finance
            'current_month_revenue': stats['monthly_revenue'],
            'current_month_expense': stats['monthly_expense'],
            'current_month_net':     stats['monthly_balance'],
            # Year-total finance (matches top-level fields)
            'total_revenue':         rev_total,
            'total_expense':         exp_total,
            'balance':               rev_total - exp_total,
            # KPI trend percentages vs prior period (None if no prior data)
            'revenue_change_pct':    stats.get('revenue_change_pct'),
            'expense_change_pct':    stats.get('expense_change_pct'),
            'net_change_pct':        stats.get('net_change_pct'),
            'present_change_pct':    stats.get('present_change_pct'),
            'absent_change_pct':     stats.get('absent_change_pct'),
            'fees_today_change_pct': stats.get('fees_today_change_pct'),
        },

        # ── Charts ───────────────────────────────────────────────────────────────
        charts = {
            # Rolling last-6-month series — matches the web dashboard bar/line chart.
            # monthly_labels: Arabic month names oldest→current, e.g. ["فبراير", ...]
            'monthly_labels':  stats['monthly_labels'],
            'monthly_revenue': stats['monthly_revenue_series'],
            'monthly_expense': stats['monthly_expense_series'],
            'monthly_net':     stats['monthly_net_series'],
            # Attendance donut / progress bar for today
            'attendance': {
                'present': stats['present_today'],
                'absent':  stats['absent_today'],
            },
            # Full 12-month arrays for the selected year (year query param).
            # Useful for a year-picker-based chart in Flutter.
            'yearly_revenue': monthly_revenue_12,
            'yearly_expense': monthly_expense_12,
            'yearly_net':     monthly_net_12,
        },

        # ── Recent lists ─────────────────────────────────────────────────────────
        recent_students      = [_serialize_student(s) for s in ctx['recent_students']],
        recent_notifications = [_serialize_notif(n)   for n in ctx['recent_notifications']],
    )


def _serialize_tx(row):
    return {
        'id':          row.id,
        'amount':      float(row.amount or 0),
        'description': row.description or None,
        'category_id': row.category_id,
        'category':    row.category.name if row.category else None,
        'date':        row.date.isoformat() if row.date else None,
    }


@mobile_api_bp.route('/investor/revenues', methods=['GET'])
@jwt_required()
@role_required('investor_viewer')
def investor_revenues():
    year  = request.args.get('year', date.today().year, type=int)
    month = request.args.get('month', type=int)
    page  = request.args.get('page', 1, type=int)
    sid   = _sid()

    category_id, cat_err = _parse_int_arg('category_id')
    if cat_err:
        return cat_err
    date_from, from_err = _parse_date_arg('date_from')
    if from_err:
        return from_err
    date_to, to_err = _parse_date_arg('date_to')
    if to_err:
        return to_err

    query = (Revenue.query.execution_options(include_all_years=True)
             .filter(Revenue.school_id == sid,
                     Revenue.refunded_at.is_(None)))

    if date_from or date_to:
        # Explicit date range replaces the year/month default so ranges that
        # cross a calendar-year boundary are not clipped.
        if date_from:
            query = query.filter(Revenue.date >= date_from)
        if date_to:
            query = query.filter(Revenue.date <= date_to)
    else:
        query = query.filter(extract('year', Revenue.date) == year)
        if month:
            query = query.filter(extract('month', Revenue.date) == month)

    if category_id is not None:
        query = query.filter(Revenue.category_id == category_id)

    total = float(query.with_entities(func.sum(Revenue.amount)).scalar() or 0)
    pagination = (query.order_by(Revenue.date.desc(), Revenue.id.desc())
                  .paginate(page=page, per_page=20, error_out=False))

    return ok(
        year=year, month=month, total=total,
        page=pagination.page, pages=pagination.pages,
        items=[_serialize_tx(r) for r in pagination.items],
        filter_options={'categories': _category_options(RevenueCategory, sid)},
    )


@mobile_api_bp.route('/investor/expenses', methods=['GET'])
@jwt_required()
@role_required('investor_viewer')
def investor_expenses():
    year  = request.args.get('year', date.today().year, type=int)
    month = request.args.get('month', type=int)
    page  = request.args.get('page', 1, type=int)
    sid   = _sid()

    category_id, cat_err = _parse_int_arg('category_id')
    if cat_err:
        return cat_err
    date_from, from_err = _parse_date_arg('date_from')
    if from_err:
        return from_err
    date_to, to_err = _parse_date_arg('date_to')
    if to_err:
        return to_err

    query = (Expense.query.execution_options(include_all_years=True)
             .filter(Expense.school_id == sid))

    if date_from or date_to:
        # Explicit date range replaces the year/month default so ranges that
        # cross a calendar-year boundary are not clipped.
        if date_from:
            query = query.filter(Expense.date >= date_from)
        if date_to:
            query = query.filter(Expense.date <= date_to)
    else:
        query = query.filter(extract('year', Expense.date) == year)
        if month:
            query = query.filter(extract('month', Expense.date) == month)

    if category_id is not None:
        query = query.filter(Expense.category_id == category_id)

    total = float(query.with_entities(func.sum(Expense.amount)).scalar() or 0)
    pagination = (query.order_by(Expense.date.desc(), Expense.id.desc())
                  .paginate(page=page, per_page=20, error_out=False))

    return ok(
        year=year, month=month, total=total,
        page=pagination.page, pages=pagination.pages,
        items=[_serialize_tx(e) for e in pagination.items],
        filter_options={'categories': _category_options(ExpenseCategory, sid)},
    )


# ─── Employee attendance (read-only) ──────────────────────────────────────────
#
# Data source: EmployeeAttendance — one row per (employee_id, date)
# (uq_employee_date). check_in / check_out are naive school-local wall-clock
# times; AI Face writes the first punch as check_in and the latest later punch
# as check_out, manual entry and approved leave (status 'on_leave') write the
# same row. No row = nothing recorded for that day (no virtual absence here).
#
# Isolation: the school comes only from the authenticated User row. Every query
# filters school_id explicitly on top of the ORM tenant guard. The rows are
# date-keyed, so reads use include_all_years (school criteria stay active, like
# the web daily sheet and HR reports) instead of the view-year filter.

EMP_ATT_DEFAULT_LIMIT = 30
EMP_ATT_MAX_LIMIT = 100
EMP_SEARCH_MAX_LEN = 100


def _investor_school_id():
    """The investor's own school id, or None when the account has no school.

    A school-less account must fail closed: with no school the mobile ORM scope
    applies no tenant filter at all.
    """
    user = g.mobile_user
    return user.school_id if getattr(user, 'is_investor', False) else None


def _strict_page_args():
    """(limit, offset, error_response). limit 1..100 (larger is clamped), offset >= 0."""
    raw_limit = (request.args.get('limit') or '').strip()
    raw_offset = (request.args.get('offset') or '').strip()
    try:
        limit = int(raw_limit) if raw_limit else EMP_ATT_DEFAULT_LIMIT
    except ValueError:
        return None, None, err('invalid_limit')
    try:
        offset = int(raw_offset) if raw_offset else 0
    except ValueError:
        return None, None, err('invalid_offset')
    if limit < 1:
        return None, None, err('invalid_limit')
    if offset < 0:
        return None, None, err('invalid_offset')
    return min(limit, EMP_ATT_MAX_LIMIT), offset, None


def _page(rows, limit, offset):
    """Trim a limit+1 fetch and build the pagination block."""
    has_more = len(rows) > limit
    return rows[:limit], {
        'limit':       limit,
        'offset':      offset,
        'has_more':    has_more,
        'next_offset': offset + limit if has_more else None,
    }


def _employee_payload(emp):
    from app.utils.employee_display_photo import employee_display_value
    from .utils import photo_url
    return {
        'id':          emp.id,
        'employee_id': emp.employee_id,
        'name':        emp.full_name,
        'job_title':   emp.job_title,
        'photo':       photo_url(employee_display_value(emp)),
    }


def _attendance_payload(rec):
    """Same status normalisation and HH:MM time format as /teacher/attendance."""
    from .teacher import _emp_att_status, _fmt_time
    return {
        'id':        rec.id,
        'date':      rec.date.isoformat(),
        'status':    _emp_att_status(rec.status),
        'check_in':  _fmt_time(rec.check_in),
        'check_out': _fmt_time(rec.check_out),
        'source':    rec.source,
    }


def _employee_columns():
    """Only the columns the payload needs — never salary / HR fields."""
    from sqlalchemy.orm import load_only
    from app.models import Employee
    return load_only(Employee.id, Employee.employee_id, Employee.full_name,
                     Employee.job_title, Employee.photo, Employee.photo_display,
                     Employee.school_id, Employee.status)


def _investor_school():
    """(school, error_response) for the authenticated investor."""
    from app.models import School
    sid = _investor_school_id()
    school = db.session.get(School, sid) if sid else None
    if school is None:
        return None, err('forbidden', 403)
    return school, None


def _school_block(school):
    return {
        'id':       school.id,
        'name':     school.school_name,
        'timezone': school.timezone or 'Asia/Baghdad',
    }


@mobile_api_bp.route('/investor/employees/attendance', methods=['GET'])
@jwt_required()
@role_required('investor_viewer')
def investor_employees_attendance():
    """Active employees of the investor's school with their attendance for ONE
    date, in one page. Employees without a row for that date are included with
    ``attendance: null``. One employees LEFT JOIN attendance query per page — no N+1.

    Query: date=YYYY-MM-DD (default: school-local today), limit (default 30,
    max 100), offset, search (name or employee code, max 100 chars).
    """
    from app.models import Employee, EmployeeAttendance
    from app.utils.attendance_helpers import get_local_date, is_holiday_date

    school, school_err = _investor_school()
    if school_err:
        return school_err
    sid = school.id

    att_date, date_err = _parse_date_arg('date')
    if date_err:
        return err('invalid_date')
    if att_date is None:
        att_date = get_local_date(school)

    limit, offset, page_err = _strict_page_args()
    if page_err:
        return page_err

    search = (request.args.get('search') or '').strip()
    if len(search) > EMP_SEARCH_MAX_LEN:
        return err('invalid_search')

    query = (db.session.query(Employee, EmployeeAttendance)
             .execution_options(include_all_years=True)
             .options(_employee_columns())
             .outerjoin(EmployeeAttendance,
                        (EmployeeAttendance.employee_id == Employee.id)
                        & (EmployeeAttendance.school_id == sid)
                        & (EmployeeAttendance.date == att_date))
             .filter(Employee.school_id == sid, Employee.status == 'active'))
    if search:
        query = query.filter(
            Employee.full_name.icontains(search, autoescape=True)
            | Employee.employee_id.icontains(search, autoescape=True))

    rows = (query.order_by(Employee.full_name, Employee.id)
            .limit(limit + 1).offset(offset).all())
    rows, pagination = _page(rows, limit, offset)

    items = []
    for emp, rec in rows:
        item = _employee_payload(emp)
        item['attendance'] = _attendance_payload(rec) if rec is not None else None
        items.append(item)

    return ok(
        school=_school_block(school),
        date=att_date.isoformat(),
        # Employee weekly day off / holiday for this date (same rule as the
        # employee attendance report), so a null attendance can be told apart.
        is_day_off=bool(is_holiday_date(att_date, sid, school=school,
                                        audience='employees')),
        items=items,
        pagination=pagination,
    )


@mobile_api_bp.route('/investor/employees/<int:employee_id>/attendance', methods=['GET'])
@jwt_required()
@role_required('investor_viewer')
def investor_employee_attendance_history(employee_id):
    """One employee's attendance rows, newest first, paginated.

    The employee must belong to the investor's school; otherwise 404 (the
    response never reveals whether the id exists in another school).
    Query: limit (default 30, max 100), offset, start / end (YYYY-MM-DD, inclusive).
    """
    from app.models import Employee, EmployeeAttendance

    school, school_err = _investor_school()
    if school_err:
        return school_err
    sid = school.id

    start, start_err = _parse_date_arg('start')
    if start_err:
        return err('invalid_start')
    end, end_err = _parse_date_arg('end')
    if end_err:
        return err('invalid_end')
    if start and end and start > end:
        return err('invalid_date_range')

    limit, offset, page_err = _strict_page_args()
    if page_err:
        return page_err

    emp = (Employee.query.options(_employee_columns())
           .filter(Employee.id == employee_id, Employee.school_id == sid)
           .first())
    if emp is None:
        return err('employee_not_found', 404)

    query = (EmployeeAttendance.query
             .execution_options(include_all_years=True)
             .filter(EmployeeAttendance.school_id == sid,
                     EmployeeAttendance.employee_id == emp.id))
    if start:
        query = query.filter(EmployeeAttendance.date >= start)
    if end:
        query = query.filter(EmployeeAttendance.date <= end)

    rows = (query.order_by(EmployeeAttendance.date.desc(), EmployeeAttendance.id.desc())
            .limit(limit + 1).offset(offset).all())
    rows, pagination = _page(rows, limit, offset)

    employee = _employee_payload(emp)
    employee['status'] = emp.status
    return ok(
        school=_school_block(school),
        employee=employee,
        items=[_attendance_payload(r) for r in rows],
        pagination=pagination,
    )

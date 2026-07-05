"""
Mobile API — Investor (read-only)
=================================
GET /api/mobile/v1/investor/dashboard   full KPI dashboard for the investor's school
GET /api/mobile/v1/investor/revenues    read-only revenue list
GET /api/mobile/v1/investor/expenses    read-only expense list

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

from flask import g, request
from sqlalchemy import func, extract

from app.models import db, Revenue, Expense
from .utils import jwt_required, role_required, ok

from . import mobile_api_bp


def _sid():
    return g.mobile_user.school_id


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
        .filter(Revenue.school_id == sid, extract('year', Revenue.date) == year)
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
        .filter(Revenue.school_id == sid, extract('year', Revenue.date) == year)
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

    query = (Revenue.query.execution_options(include_all_years=True)
             .filter(Revenue.school_id == sid,
                     extract('year', Revenue.date) == year))
    if month:
        query = query.filter(extract('month', Revenue.date) == month)

    total = float(query.with_entities(func.sum(Revenue.amount)).scalar() or 0)
    pagination = (query.order_by(Revenue.date.desc(), Revenue.id.desc())
                  .paginate(page=page, per_page=20, error_out=False))

    return ok(
        year=year, month=month, total=total,
        page=pagination.page, pages=pagination.pages,
        items=[_serialize_tx(r) for r in pagination.items],
    )


@mobile_api_bp.route('/investor/expenses', methods=['GET'])
@jwt_required()
@role_required('investor_viewer')
def investor_expenses():
    year  = request.args.get('year', date.today().year, type=int)
    month = request.args.get('month', type=int)
    page  = request.args.get('page', 1, type=int)
    sid   = _sid()

    query = (Expense.query.execution_options(include_all_years=True)
             .filter(Expense.school_id == sid,
                     extract('year', Expense.date) == year))
    if month:
        query = query.filter(extract('month', Expense.date) == month)

    total = float(query.with_entities(func.sum(Expense.amount)).scalar() or 0)
    pagination = (query.order_by(Expense.date.desc(), Expense.id.desc())
                  .paginate(page=page, per_page=20, error_out=False))

    return ok(
        year=year, month=month, total=total,
        page=pagination.page, pages=pagination.pages,
        items=[_serialize_tx(e) for e in pagination.items],
    )

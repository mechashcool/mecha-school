"""
Mecha-School — Investor Portal Blueprint  (read-only, school-scoped)
====================================================================
URL prefix: /investor

A minimal, strictly read-only portal for the `investor_viewer` role. An
investor is bound to a single school via `User.school_id` and may only view:

  * Dashboard      — finance summary (revenues, expenses, balance, chart)
  * Revenues       — read-only list
  * Expenses       — read-only list

Isolation
---------
Every query is scoped to `current_user.school_id`. The ORM tenant guard
(app/utils/scoping.py) already forces `school_id = current_user.school_id`
on all reads, so an investor physically cannot load another school's rows.
Each query below *also* filters on `current_user.school_id` explicitly as a
defence-in-depth second barrier.

Authorization
-------------
`investor_required` allows only an authenticated, active `investor_viewer`
with a school binding. All routes are GET-only; the portal exposes no
create/edit/delete actions and the role holds no permissions, so every other
(permission-gated) route in the app already returns 403 for this role.
"""
from functools import wraps

from flask import Blueprint, render_template, redirect, url_for, abort, request
from flask_login import login_required, current_user
from sqlalchemy import func, extract

from app.models import db, Revenue, Expense, RevenueCategory, ExpenseCategory, INVESTOR_ROLE

investor_bp = Blueprint('investor', __name__,
                        template_folder='../../templates/investor')

ARABIC_MONTHS = ['يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو',
                 'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر']


def investor_required(f):
    """Allow only an authenticated, active, school-bound investor_viewer."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login'))
        # is_investor already requires role.name == investor_viewer AND a school_id.
        if not getattr(current_user, 'is_investor', False):
            abort(403)
        return f(*args, **kwargs)
    return wrapped


def _school_id():
    """The investor's own school id — the single authoritative scope for this portal."""
    return current_user.school_id


# ─────────────────────────────────────────────────────────────────────────────
#  DASHBOARD  — finance summary for the investor's school
# ─────────────────────────────────────────────────────────────────────────────

@investor_bp.route('/')
@login_required
@investor_required
def dashboard():
    from datetime import date
    today = date.today()
    year  = request.args.get('year', today.year, type=int)
    sid   = _school_id()

    # Totals for the selected calendar year — school-scoped (ORM + explicit filter).
    rev_total = float(
        db.session.query(func.coalesce(func.sum(Revenue.amount), 0))
        .execution_options(include_all_years=True)
        .filter(Revenue.school_id == sid,
                extract('year', Revenue.date) == year)
        .scalar() or 0
    )
    exp_total = float(
        db.session.query(func.coalesce(func.sum(Expense.amount), 0))
        .execution_options(include_all_years=True)
        .filter(Expense.school_id == sid,
                extract('year', Expense.date) == year)
        .scalar() or 0
    )

    # Monthly breakdown for the chart.
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
    chart_rev = [rev_by_month.get(m, 0) for m in range(1, 13)]
    chart_exp = [exp_by_month.get(m, 0) for m in range(1, 13)]

    # Recent transactions (read-only), scoped to the investor's school.
    recent_rev = (Revenue.query.execution_options(include_all_years=True)
                  .filter(Revenue.school_id == sid, Revenue.amount > 0)
                  .order_by(Revenue.date.desc(), Revenue.id.desc()).limit(5).all())
    recent_exp = (Expense.query.execution_options(include_all_years=True)
                  .filter(Expense.school_id == sid, Expense.amount > 0)
                  .order_by(Expense.date.desc(), Expense.id.desc()).limit(5).all())

    return render_template(
        'investor/dashboard.html',
        year=year,
        total_rev=rev_total,
        total_exp=exp_total,
        balance=rev_total - exp_total,
        chart_rev=chart_rev,
        chart_exp=chart_exp,
        recent_rev=recent_rev,
        recent_exp=recent_exp,
        arabic_months=ARABIC_MONTHS,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  REVENUES  — read-only list
# ─────────────────────────────────────────────────────────────────────────────

@investor_bp.route('/revenues')
@login_required
@investor_required
def revenues():
    from datetime import date
    page   = request.args.get('page', 1, type=int)
    year   = request.args.get('year', date.today().year, type=int)
    month  = request.args.get('month', type=int)
    sid    = _school_id()

    query = (Revenue.query.execution_options(include_all_years=True)
             .filter(Revenue.school_id == sid,
                     extract('year', Revenue.date) == year))
    if month:
        query = query.filter(extract('month', Revenue.date) == month)

    total = float(query.with_entities(func.sum(Revenue.amount)).scalar() or 0)
    items = query.order_by(Revenue.date.desc(), Revenue.id.desc())\
                 .paginate(page=page, per_page=20, error_out=False)

    return render_template('investor/revenues.html',
                           items=items, total=total, year=year, month=month,
                           arabic_months=ARABIC_MONTHS)


# ─────────────────────────────────────────────────────────────────────────────
#  EXPENSES  — read-only list
# ─────────────────────────────────────────────────────────────────────────────

@investor_bp.route('/expenses')
@login_required
@investor_required
def expenses():
    from datetime import date
    page   = request.args.get('page', 1, type=int)
    year   = request.args.get('year', date.today().year, type=int)
    month  = request.args.get('month', type=int)
    sid    = _school_id()

    query = (Expense.query.execution_options(include_all_years=True)
             .filter(Expense.school_id == sid,
                     extract('year', Expense.date) == year))
    if month:
        query = query.filter(extract('month', Expense.date) == month)

    total = float(query.with_entities(func.sum(Expense.amount)).scalar() or 0)
    items = query.order_by(Expense.date.desc(), Expense.id.desc())\
                 .paginate(page=page, per_page=20, error_out=False)

    return render_template('investor/expenses.html',
                           items=items, total=total, year=year, month=month,
                           arabic_months=ARABIC_MONTHS)

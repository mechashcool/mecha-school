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
#  DASHBOARD  — reuses the school-manager dashboard, read-only
# ─────────────────────────────────────────────────────────────────────────────

@investor_bp.route('/')
@login_required
@investor_required
def dashboard():
    """Render the exact school-manager dashboard for the investor.

    Reuses admin._build_dashboard_context() (same stats/charts/summaries, all
    school-scoped via get_current_school() → the investor's own school) and the
    same admin/dashboard.html template. Only two things differ for the investor:
      * base_template → investor/base.html (investor-only sidebar)
      * investor_view=True → template hides "عرض الكل" and restricted links
    No manager permissions are granted; restricted routes stay guarded server-side.
    """
    from app.blueprints.admin import _build_dashboard_context
    ctx = _build_dashboard_context()
    return render_template(
        'admin/dashboard.html',
        base_template='investor/base.html',
        investor_view=True,
        **ctx,
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

"""
Mobile API — Investor (read-only)
=================================
GET /api/mobile/v1/investor/dashboard   finance summary for the investor's school
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
    year = request.args.get('year', date.today().year, type=int)
    sid  = _sid()

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

    return ok(
        year=year,
        total_revenue=rev_total,
        total_expense=exp_total,
        balance=rev_total - exp_total,
        monthly_revenue=[rev_by_month.get(m, 0) for m in range(1, 13)],
        monthly_expense=[exp_by_month.get(m, 0) for m in range(1, 13)],
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

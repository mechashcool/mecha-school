"""
Al-Muhandis – Finances Blueprint
Handles: Revenues, Expenses, Categories, Monthly/Annual summaries
"""
from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, jsonify, abort)
from flask_login import login_required, current_user
from sqlalchemy import func, extract
from sqlalchemy.exc import IntegrityError
from datetime import date, datetime as dt

from app.models import (db, Revenue, RevenueCategory,
                         Expense, ExpenseCategory, AcademicYear)
from app.utils.decorators import (permission_required, any_permission_required,
                                  get_current_school, get_active_year, historical_guard)
from app.utils.scoping import is_historical_view
from app.utils.helpers import save_uploaded_file
from app.utils.audit import log_action

finances_bp = Blueprint('finances', __name__,
                          template_folder='../../templates/finances')

ARABIC_MONTHS = [
    '', 'يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو',
    'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر'
]


def _academic_year_for_date(school, tx_date):
    if not school:
        return None
    return (AcademicYear.query.execution_options(bypass_tenant_scope=True)
            .filter(AcademicYear.school_id == school.id,
                    AcademicYear.start_date <= tx_date,
                    AcademicYear.end_date >= tx_date)
            .order_by(AcademicYear.start_date.desc())
            .first()) or get_active_year(school.id)


def _category_school_id(record=None):
    school = get_current_school()
    if school:
        return school.id
    return getattr(record, 'school_id', None)


def _category_query(model, school_id=None):
    query = model.query
    if school_id:
        query = query.filter_by(school_id=school_id)
    return query.order_by(model.name)


def _category_for_school(model, category_id, school_id):
    if not category_id or not school_id:
        return None
    return (
        model.query.execution_options(bypass_tenant_scope=True)
        .filter_by(id=category_id, school_id=school_id)
        .first()
    )


def _category_record_counts(record_model, categories):
    counts = {}
    for category in categories:
        counts[category.id] = (
            record_model.query.execution_options(include_all_years=True)
            .filter_by(category_id=category.id, school_id=category.school_id)
            .count()
        )
    return counts


# ─────────────────────────────────────────────────────────────────────────────
#  OVERVIEW DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

@finances_bp.route('/')
@login_required
@any_permission_required('manage_revenues', 'manage_expenses')
def index():
    today  = date.today()
    year   = request.args.get('year', today.year, type=int)
    month  = request.args.get('month', type=int)
    school = get_current_school()
    sid    = school.id if school else None

    rev_q = (db.session.query(
        func.coalesce(func.sum(Revenue.amount), 0).label('total')
    ).execution_options(include_all_years=True)
     .filter(extract('year', Revenue.date) == year))
    if sid:
        rev_q = rev_q.filter(Revenue.school_id == sid)

    exp_q = (db.session.query(
        func.coalesce(func.sum(Expense.amount), 0).label('total')
    ).execution_options(include_all_years=True)
     .filter(extract('year', Expense.date) == year))
    if sid:
        exp_q = exp_q.filter(Expense.school_id == sid)

    if month:
        rev_q = rev_q.filter(extract('month', Revenue.date) == month)
        exp_q = exp_q.filter(extract('month', Expense.date) == month)

    total_rev = float(rev_q.scalar() or 0)
    total_exp = float(exp_q.scalar() or 0)

    # Monthly breakdown for chart (full year)
    rev_chart_q = (db.session.query(
        extract('month', Revenue.date).label('m'),
        func.sum(Revenue.amount).label('total')
    ).execution_options(include_all_years=True)
     .filter(extract('year', Revenue.date) == year))
    if sid:
        rev_chart_q = rev_chart_q.filter(Revenue.school_id == sid)
    monthly_rev = {r.m: float(r.total) for r in rev_chart_q.group_by('m').all()}

    exp_chart_q = (db.session.query(
        extract('month', Expense.date).label('m'),
        func.sum(Expense.amount).label('total')
    ).execution_options(include_all_years=True)
     .filter(extract('year', Expense.date) == year))
    if sid:
        exp_chart_q = exp_chart_q.filter(Expense.school_id == sid)
    monthly_exp = {r.m: float(r.total) for r in exp_chart_q.group_by('m').all()}

    chart_rev = [monthly_rev.get(m, 0) for m in range(1, 13)]
    chart_exp = [monthly_exp.get(m, 0) for m in range(1, 13)]

    # Recent transactions — school-scoped across all academic years
    recent_rev_q = Revenue.query.execution_options(include_all_years=True)
    recent_exp_q = Expense.query.execution_options(include_all_years=True)
    if sid:
        recent_rev_q = recent_rev_q.filter_by(school_id=sid)
        recent_exp_q = recent_exp_q.filter_by(school_id=sid)
    recent_rev = recent_rev_q.order_by(Revenue.date.desc()).limit(5).all()
    recent_exp = recent_exp_q.order_by(Expense.date.desc()).limit(5).all()

    return render_template('finances/index.html',
                           total_rev=total_rev, total_exp=total_exp,
                           balance=total_rev - total_exp,
                           chart_rev=chart_rev, chart_exp=chart_exp,
                           recent_rev=recent_rev, recent_exp=recent_exp,
                           year=year, month=month,
                           arabic_months=ARABIC_MONTHS)


# ─────────────────────────────────────────────────────────────────────────────
#  REVENUES
# ─────────────────────────────────────────────────────────────────────────────

@finances_bp.route('/revenues')
@login_required
@permission_required('manage_revenues')
def revenues():
    page   = request.args.get('page', 1, type=int)
    year   = request.args.get('year', date.today().year, type=int)
    month  = request.args.get('month', type=int)
    cat_id = request.args.get('category_id', type=int)

    query = (Revenue.query.execution_options(include_all_years=True)
             .filter(extract('year', Revenue.date) == year))
    if month:
        query = query.filter(extract('month', Revenue.date) == month)
    if cat_id:
        query = query.filter_by(category_id=cat_id)

    revenues   = query.order_by(Revenue.date.desc()).paginate(page=page, per_page=20)
    categories = _category_query(RevenueCategory, _category_school_id()).all()
    total      = query.with_entities(func.sum(Revenue.amount)).scalar() or 0

    return render_template('finances/revenues.html',
                           revenues=revenues, categories=categories,
                           total=float(total), year=year, month=month,
                           cat_id=cat_id, arabic_months=ARABIC_MONTHS,
                           is_historical_year=is_historical_view())


@finances_bp.route('/revenues/create', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_revenues')
def create_revenue():
    school = get_current_school()
    categories = _category_query(
        RevenueCategory,
        school.id if school else None,
    ).all()
    if request.method == 'POST':
        date_str = request.form.get('date')
        tx_date = dt.strptime(date_str, '%Y-%m-%d').date() if date_str else date.today()
        ay = get_active_year(school.id) if school else None
        if not school or not ay:
            flash('لا توجد سنة دراسية نشطة. يرجى التحقق من إعدادات السنة الدراسية.', 'danger')
            return redirect(url_for('finances.revenues'))
        category = _category_for_school(
            RevenueCategory,
            request.form.get('category_id', type=int),
            school.id,
        )
        if not category:
            flash('الفئة المحددة غير متاحة لهذه المدرسة.', 'danger')
            return redirect(url_for('finances.create_revenue'))
        rev = Revenue(
            category_id = category.id,
            school_id   = school.id,
            academic_year_id = ay.id,
            amount      = float(request.form.get('amount', 0)),
            description = request.form.get('description', '').strip(),
            date        = tx_date,
            recorded_by = current_user.id,
        )
        db.session.add(rev)
        db.session.commit()
        log_action('create', 'revenue', rev.id,
                   details=f'amount={rev.amount} cat={rev.category_id}')
        flash('تم تسجيل الإيراد بنجاح.', 'success')
        return redirect(url_for('finances.revenues'))
    return render_template('finances/revenue_form.html',
                           categories=categories, record=None)


@finances_bp.route('/revenues/<int:rev_id>/edit', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_revenues')
def edit_revenue(rev_id):
    rev = (Revenue.query.execution_options(include_all_years=True)
           .filter_by(id=rev_id).first_or_404())
    categories = _category_query(RevenueCategory, _category_school_id(rev)).all()
    if request.method == 'POST':
        date_str = request.form.get('date')
        category = _category_for_school(
            RevenueCategory,
            request.form.get('category_id', type=int),
            rev.school_id,
        )
        if not category:
            flash('الفئة المحددة غير متاحة لهذه المدرسة.', 'danger')
            return redirect(url_for('finances.edit_revenue', rev_id=rev.id))
        rev.category_id = category.id
        rev.amount      = float(request.form.get('amount', rev.amount))
        rev.description = request.form.get('description', '').strip()
        if date_str:
            rev.date = dt.strptime(date_str, '%Y-%m-%d').date()
            ay = _academic_year_for_date(rev.school, rev.date)
            if ay:
                rev.academic_year_id = ay.id
        db.session.commit()
        log_action('edit', 'revenue', rev.id,
                   details=f'amount={rev.amount}')
        flash('تم تحديث الإيراد.', 'success')
        return redirect(url_for('finances.revenues'))
    return render_template('finances/revenue_form.html',
                           categories=categories, record=rev)


@finances_bp.route('/revenues/<int:rev_id>/delete', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_revenues')
def delete_revenue(rev_id):
    rev = (Revenue.query.execution_options(include_all_years=True)
           .filter_by(id=rev_id).first_or_404())
    amount = float(rev.amount)
    db.session.delete(rev)
    db.session.commit()
    log_action('delete', 'revenue', rev_id, details=f'amount={amount}')
    flash('تم حذف الإيراد.', 'success')
    return redirect(url_for('finances.revenues'))


# ─────────────────────────────────────────────────────────────────────────────
#  EXPENSES
# ─────────────────────────────────────────────────────────────────────────────

@finances_bp.route('/expenses')
@login_required
@permission_required('manage_expenses')
def expenses():
    page   = request.args.get('page', 1, type=int)
    year   = request.args.get('year', date.today().year, type=int)
    month  = request.args.get('month', type=int)
    cat_id = request.args.get('category_id', type=int)

    query = (Expense.query.execution_options(include_all_years=True)
             .filter(extract('year', Expense.date) == year))
    if month:
        query = query.filter(extract('month', Expense.date) == month)
    if cat_id:
        query = query.filter_by(category_id=cat_id)

    expenses   = query.order_by(Expense.date.desc()).paginate(page=page, per_page=20)
    categories = _category_query(ExpenseCategory, _category_school_id()).all()
    total      = query.with_entities(func.sum(Expense.amount)).scalar() or 0

    return render_template('finances/expenses.html',
                           expenses=expenses, categories=categories,
                           total=float(total), year=year, month=month,
                           cat_id=cat_id, arabic_months=ARABIC_MONTHS,
                           is_historical_year=is_historical_view())


@finances_bp.route('/expenses/create', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_expenses')
def create_expense():
    school = get_current_school()
    categories = _category_query(
        ExpenseCategory,
        school.id if school else None,
    ).all()
    if request.method == 'POST':
        date_str = request.form.get('date')
        tx_date = dt.strptime(date_str, '%Y-%m-%d').date() if date_str else date.today()
        ay = get_active_year(school.id) if school else None
        if not school or not ay:
            flash('لا توجد سنة دراسية نشطة. يرجى التحقق من إعدادات السنة الدراسية.', 'danger')
            return redirect(url_for('finances.expenses'))
        category = _category_for_school(
            ExpenseCategory,
            request.form.get('category_id', type=int),
            school.id,
        )
        if not category:
            flash('الفئة المحددة غير متاحة لهذه المدرسة.', 'danger')
            return redirect(url_for('finances.create_expense'))
        receipt  = None
        if 'receipt' in request.files and request.files['receipt'].filename:
            receipt = save_uploaded_file(request.files['receipt'], 'receipts')
        exp = Expense(
            category_id = category.id,
            school_id   = school.id,
            academic_year_id = ay.id,
            amount      = float(request.form.get('amount', 0)),
            description = request.form.get('description', '').strip(),
            date        = tx_date,
            payment_method = request.form.get('payment_method', 'cash'),
            reference_no   = request.form.get('reference_no', '').strip() or None,
            source         = 'manual',
            created_by  = current_user.id,
            approved_by = current_user.id,
            receipt     = receipt,
        )
        db.session.add(exp)
        db.session.commit()
        log_action('create', 'expense', exp.id,
                   details=f'amount={exp.amount} cat={exp.category_id}')
        flash('تم تسجيل المصروف بنجاح.', 'success')
        return redirect(url_for('finances.expenses'))
    return render_template('finances/expense_form.html',
                           categories=categories, record=None)


@finances_bp.route('/expenses/<int:exp_id>/edit', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_expenses')
def edit_expense(exp_id):
    exp = (Expense.query.execution_options(include_all_years=True)
           .filter_by(id=exp_id).first_or_404())
    categories = _category_query(ExpenseCategory, _category_school_id(exp)).all()
    if request.method == 'POST':
        # Payroll-sourced expenses are owned by the payroll module.
        # Editing them here would decouple salary↔expense and corrupt reports.
        if exp.source == 'payroll':
            flash('لا يمكن تعديل قيد ناتج من الرواتب. قم بإلغاء صرف الراتب أولاً.', 'danger')
            return redirect(url_for('finances.expenses'))

        date_str = request.form.get('date')
        category = _category_for_school(
            ExpenseCategory,
            request.form.get('category_id', type=int),
            exp.school_id,
        )
        if not category:
            flash('الفئة المحددة غير متاحة لهذه المدرسة.', 'danger')
            return redirect(url_for('finances.edit_expense', exp_id=exp.id))
        exp.category_id = category.id
        exp.amount      = float(request.form.get('amount', exp.amount))
        exp.description = request.form.get('description', '').strip()
        exp.payment_method = request.form.get('payment_method', exp.payment_method)
        exp.reference_no   = request.form.get('reference_no', '').strip() or None
        if date_str:
            exp.date = dt.strptime(date_str, '%Y-%m-%d').date()
            ay = _academic_year_for_date(exp.school, exp.date)
            if ay:
                exp.academic_year_id = ay.id
        db.session.commit()
        log_action('edit', 'expense', exp.id,
                   details=f'amount={exp.amount}')
        flash('تم تحديث المصروف.', 'success')
        return redirect(url_for('finances.expenses'))
    return render_template('finances/expense_form.html',
                           categories=categories, record=exp)


@finances_bp.route('/expenses/<int:exp_id>/delete', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_expenses')
def delete_expense(exp_id):
    exp = (Expense.query.execution_options(include_all_years=True)
           .filter_by(id=exp_id).first_or_404())
    if exp.source == 'payroll':
        flash('لا يمكن حذف قيد ناتج من الرواتب مباشرة.', 'danger')
        return redirect(url_for('finances.expenses'))
    amount = float(exp.amount)
    db.session.delete(exp)
    db.session.commit()
    log_action('delete', 'expense', exp_id, details=f'amount={amount}')
    flash('تم حذف المصروف.', 'success')
    return redirect(url_for('finances.expenses'))


# ─────────────────────────────────────────────────────────────────────────────
#  CATEGORIES
# ─────────────────────────────────────────────────────────────────────────────

@finances_bp.route('/categories', methods=['GET', 'POST'])
@login_required
@historical_guard
@any_permission_required('manage_revenues', 'manage_expenses')
def categories():
    school = get_current_school()
    school_id = school.id if school else None

    if request.method == 'POST':
        ctype = request.form.get('type')
        name  = request.form.get('name', '').strip()
        if not school:
            flash('يرجى اختيار مدرسة قبل إضافة فئات مالية.', 'danger')
        elif not name:
            flash('الاسم مطلوب.', 'danger')
        else:
            if ctype == 'revenue':
                if not RevenueCategory.query.filter_by(name=name, school_id=school.id).first():
                    db.session.add(RevenueCategory(name=name, school_id=school.id))
                    db.session.commit()
                    flash('تمت إضافة فئة الإيراد.', 'success')
                else:
                    flash('هذه الفئة موجودة بالفعل لهذه المدرسة.', 'warning')
            else:
                if not ExpenseCategory.query.filter_by(name=name, school_id=school.id).first():
                    db.session.add(ExpenseCategory(name=name, school_id=school.id))
                    db.session.commit()
                    flash('تمت إضافة فئة المصروف.', 'success')
                else:
                    flash('هذه الفئة موجودة بالفعل لهذه المدرسة.', 'warning')
        return redirect(url_for('finances.categories'))

    rev_cats = _category_query(RevenueCategory, school_id).all()
    exp_cats = _category_query(ExpenseCategory, school_id).all()
    rev_counts = _category_record_counts(Revenue, rev_cats)
    exp_counts = _category_record_counts(Expense, exp_cats)
    return render_template('finances/categories.html',
                           rev_cats=rev_cats, exp_cats=exp_cats,
                           rev_counts=rev_counts, exp_counts=exp_counts,
                           is_historical_year=is_historical_view())


@finances_bp.route('/categories/<string:ctype>/<int:cat_id>/delete', methods=['POST'])
@login_required
@historical_guard
@any_permission_required('manage_revenues', 'manage_expenses')
def delete_category(ctype, cat_id):
    if ctype == 'revenue':
        cat = RevenueCategory.query.get_or_404(cat_id)
        record_count = (
            Revenue.query.execution_options(include_all_years=True)
            .filter_by(category_id=cat.id, school_id=cat.school_id)
            .count()
        )
        entity_label = 'إيراد'
    elif ctype == 'expense':
        cat = ExpenseCategory.query.get_or_404(cat_id)
        record_count = (
            Expense.query.execution_options(include_all_years=True)
            .filter_by(category_id=cat.id, school_id=cat.school_id)
            .count()
        )
        entity_label = 'مصروف'
    else:
        abort(404)

    if record_count > 0:
        flash(
            f'لا يمكن حذف الفئة "{cat.name}" لأنها مرتبطة بـ {record_count} سجل {entity_label}. '
            f'يرجى حذف أو إعادة تصنيف تلك السجلات أولاً.',
            'danger'
        )
        return redirect(url_for('finances.categories'))

    try:
        db.session.delete(cat)
        db.session.commit()
        flash('تم حذف الفئة.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash(
            f'لا يمكن حذف الفئة "{cat.name}" لأنها مرتبطة بسجلات مالية. '
            'يرجى حذف السجلات أو نقلها إلى فئة أخرى أولاً.',
            'danger',
        )
    return redirect(url_for('finances.categories'))

"""
Al-Muhandis – Reports Blueprint  (complete rewrite)
All report routes with real aggregated data.

NOTE: When adding PDF generation routes to reports, ensure they handle Arabic font
loading failures gracefully by using generate_error_pdf() fallback, similar to
generate_schedule_pdf() in schedules blueprint.
"""
from flask import Blueprint, render_template, request
from flask_login import login_required
from sqlalchemy import func, extract
from datetime import date

from app.models import (db, Revenue, Expense, FeeInstallment, FeeRecord,
                         Student, Employee, StudentAttendance, ExamResult,
                         SalaryRecord, Section, Grade, RevenueCategory,
                         ExpenseCategory)
from app.utils.decorators import permission_required, get_current_school, get_active_year

reports_bp = Blueprint('reports', __name__,
                        template_folder='../../templates/reports')

ARABIC_MONTHS = ['', 'يناير', 'فبراير', 'مارس', 'أبريل', 'مايو', 'يونيو',
                 'يوليو', 'أغسطس', 'سبتمبر', 'أكتوبر', 'نوفمبر', 'ديسمبر']


# ── helpers ──────────────────────────────────────────────────────────────────
def _year_monthly(model, date_col, amount_col, year, school_id=None):
    q = (db.session.query(
                extract('month', date_col).label('m'),
                func.coalesce(func.sum(amount_col), 0).label('t'))
            .filter(extract('year', date_col) == year))
    if school_id:
        q = q.filter(model.school_id == school_id)
    rows = q.group_by('m').all()
    return {int(r[0]): float(r[1]) for r in rows}


# ── MAIN DASHBOARD ────────────────────────────────────────────────────────────
@reports_bp.route('/')
@login_required
@permission_required('view_reports')
def index():
    school = get_current_school()
    school_id = school.id if school else None
    active_year = get_active_year(school_id) if school_id else None
    year_id = active_year.id if active_year else None

    today = date.today()
    year  = today.year

    # Students
    def _student_count(status):
        q = Student.query.filter_by(status=status)
        if school_id:
            q = q.filter_by(school_id=school_id)
        return q.count()
    student_stats = {s: _student_count(s)
                     for s in ('active', 'transferred', 'withdrawn', 'graduated')}

    # Employees
    emp_q = Employee.query.filter_by(status='active')
    if school_id:
        emp_q = emp_q.filter_by(school_id=school_id)
    emp_active = emp_q.count()

    # Financial year totals
    rev_q = db.session.query(func.coalesce(func.sum(Revenue.amount), 0))\
                      .filter(extract('year', Revenue.date) == year)
    if school_id:
        rev_q = rev_q.filter(Revenue.school_id == school_id)
    total_rev = float(rev_q.scalar())

    exp_q = db.session.query(func.coalesce(func.sum(Expense.amount), 0))\
                      .filter(extract('year', Expense.date) == year)
    if school_id:
        exp_q = exp_q.filter(Expense.school_id == school_id)
    total_exp = float(exp_q.scalar())

    # Monthly chart data
    m_rev = _year_monthly(Revenue, Revenue.date, Revenue.amount, year, school_id)
    m_exp = _year_monthly(Expense, Expense.date, Expense.amount, year, school_id)
    chart_rev = [m_rev.get(i, 0) for i in range(1, 13)]
    chart_exp = [m_exp.get(i, 0) for i in range(1, 13)]

    # Fees collection
    def _fee_sum(col, filters=None):
        q = db.session.query(func.coalesce(func.sum(col), 0))\
                      .join(FeeRecord, FeeInstallment.fee_record_id == FeeRecord.id)
        if school_id:
            q = q.filter(FeeRecord.school_id == school_id)
        if filters:
            q = q.filter(*filters)
        return float(q.scalar())

    total_fee_all  = _fee_sum(FeeInstallment.amount)
    total_fee_paid = _fee_sum(FeeInstallment.received_amount)
    fee_rate = round(total_fee_paid / total_fee_all * 100 if total_fee_all else 0, 1)

    # Today attendance
    def _att_count(status):
        q = StudentAttendance.query.filter_by(date=today, status=status)
        if school_id:
            q = q.filter_by(school_id=school_id)
        return q.count()
    present_today = _att_count('present')
    absent_today  = _att_count('absent')
    late_today    = _att_count('late')

    # Salary year total (exclude cancelled payroll records)
    sal_q = db.session.query(func.coalesce(func.sum(SalaryRecord.net_salary), 0))\
                      .filter(SalaryRecord.year == year,
                              SalaryRecord.status != 'cancelled')
    if school_id:
        sal_q = sal_q.filter(SalaryRecord.school_id == school_id)
    salary_year = float(sal_q.scalar())

    return render_template('reports/index.html',
                           student_stats=student_stats,
                           emp_active=emp_active,
                           total_rev=total_rev, total_exp=total_exp,
                           balance=total_rev - total_exp,
                           chart_rev=chart_rev, chart_exp=chart_exp,
                           fee_rate=fee_rate,
                           total_fee_paid=total_fee_paid,
                           total_fee_all=total_fee_all,
                           present_today=present_today,
                           absent_today=absent_today,
                           late_today=late_today,
                           salary_year=salary_year,
                           year=year,
                           arabic_months=ARABIC_MONTHS)


# ── FINANCIAL ─────────────────────────────────────────────────────────────────
@reports_bp.route('/financial')
@login_required
@permission_required('view_reports')
def financial():
    school = get_current_school()
    school_id = school.id if school else None

    year  = request.args.get('year', date.today().year, type=int)
    month = request.args.get('month', type=int)

    rev_q = db.session.query(func.coalesce(func.sum(Revenue.amount), 0))\
                      .filter(extract('year', Revenue.date) == year)
    if school_id:
        rev_q = rev_q.filter(Revenue.school_id == school_id)
    exp_q = db.session.query(func.coalesce(func.sum(Expense.amount), 0))\
                      .filter(extract('year', Expense.date) == year)
    if school_id:
        exp_q = exp_q.filter(Expense.school_id == school_id)

    if month:
        rev_q = rev_q.filter(extract('month', Revenue.date) == month)
        exp_q = exp_q.filter(extract('month', Expense.date) == month)

    total_rev = float(rev_q.scalar())
    total_exp = float(exp_q.scalar())

    # Revenue breakdown by category
    rev_cat_q = (db.session.query(
                    RevenueCategory.name,
                    func.coalesce(func.sum(Revenue.amount), 0).label('total'))
                 .join(Revenue, Revenue.category_id == RevenueCategory.id)
                 .filter(extract('year', Revenue.date) == year))
    if school_id:
        rev_cat_q = rev_cat_q.filter(Revenue.school_id == school_id)
    rev_cats = [(name, float(total)) for name, total in
                rev_cat_q.group_by(RevenueCategory.name)
                         .order_by(func.sum(Revenue.amount).desc()).all()]

    # Expense breakdown by category
    exp_cat_q = (db.session.query(
                    ExpenseCategory.name,
                    func.coalesce(func.sum(Expense.amount), 0).label('total'))
                 .join(Expense, Expense.category_id == ExpenseCategory.id)
                 .filter(extract('year', Expense.date) == year))
    if school_id:
        exp_cat_q = exp_cat_q.filter(Expense.school_id == school_id)
    exp_cats = [(name, float(total)) for name, total in
                exp_cat_q.group_by(ExpenseCategory.name)
                         .order_by(func.sum(Expense.amount).desc()).all()]

    # Monthly chart
    m_rev = _year_monthly(Revenue, Revenue.date, Revenue.amount, year, school_id)
    m_exp = _year_monthly(Expense, Expense.date, Expense.amount, year, school_id)
    chart_rev = [m_rev.get(i, 0) for i in range(1, 13)]
    chart_exp = [m_exp.get(i, 0) for i in range(1, 13)]

    return render_template('reports/financial.html',
                           total_rev=total_rev, total_exp=total_exp,
                           balance=total_rev - total_exp,
                           rev_cats=rev_cats, exp_cats=exp_cats,
                           chart_rev=chart_rev, chart_exp=chart_exp,
                           year=year, month=month,
                           arabic_months=ARABIC_MONTHS)


# ── STUDENTS ──────────────────────────────────────────────────────────────────
@reports_bp.route('/students')
@login_required
@permission_required('view_reports')
def students_report():
    school = get_current_school()
    school_id = school.id if school else None
    active_year = get_active_year(school_id) if school_id else None
    year_id = active_year.id if active_year else None

    def _student_count(status):
        q = Student.query.filter_by(status=status)
        if school_id:
            q = q.filter_by(school_id=school_id)
        return q.count()
    stats = {s: _student_count(s)
             for s in ('active', 'transferred', 'withdrawn', 'graduated')}

    # Per-section stats scoped to current school
    sec_q = Section.query.join(Grade).order_by(Grade.name, Section.name)
    if school_id:
        sec_q = sec_q.filter(Grade.school_id == school_id)
    sections = sec_q.all()

    section_data = []
    for sec in sections:
        stud_q = sec.students.filter_by(status='active')
        if year_id:
            stud_q = stud_q.filter_by(academic_year_id=year_id)
        active = stud_q.count()
        section_data.append({
            'section': sec, 'active': active,
            'capacity': sec.capacity,
            'pct': round(active / sec.capacity * 100 if sec.capacity else 0, 1)
        })

    return render_template('reports/students.html',
                           stats=stats, section_data=section_data)


# ── FEES ──────────────────────────────────────────────────────────────────────
@reports_bp.route('/fees')
@login_required
@permission_required('view_reports')
def fees_report():
    """
    Phase 2 rewrite — Fees Report accuracy fix.

    Old bug: the report summed `FeeInstallment.amount WHERE status='paid'`,
    which ignored partial payments entirely and over-counted any installment
    paid early at less than its scheduled amount.

    New behaviour: every monetary total is aggregated from
    `FeeInstallment.received_amount` (actual cash in) versus
    `FeeInstallment.amount` (scheduled). This matches how the
    installment pay modal stores values and makes the Decimal math exact.
    """
    from datetime import date as d
    school = get_current_school()
    school_id = school.id if school else None

    def _inst_sum(col, extra_filters=None):
        q = db.session.query(func.coalesce(func.sum(col), 0))\
                      .join(FeeRecord, FeeInstallment.fee_record_id == FeeRecord.id)
        if school_id:
            q = q.filter(FeeRecord.school_id == school_id)
        if extra_filters:
            q = q.filter(*extra_filters)
        return float(q.scalar())

    total_all     = _inst_sum(FeeInstallment.amount)
    total_paid_actual = _inst_sum(FeeInstallment.received_amount)
    total_pending = _inst_sum(FeeInstallment.amount,
                              [FeeInstallment.status == 'pending'])
    total_partial = _inst_sum(FeeInstallment.amount - FeeInstallment.received_amount,
                              [FeeInstallment.status == 'partial'])

    overdue_base = (FeeInstallment.query
                    .join(FeeRecord, FeeInstallment.fee_record_id == FeeRecord.id))
    if school_id:
        overdue_base = overdue_base.filter(FeeRecord.school_id == school_id)
    overdue_q = (overdue_base
                 .filter(FeeInstallment.status.in_(['pending', 'partial', 'overdue']))
                 .filter(FeeInstallment.due_date < d.today())
                 .filter(FeeInstallment.received_amount < FeeInstallment.amount)
                 .order_by(FeeInstallment.due_date))
    overdue_list  = overdue_q.limit(25).all()
    overdue_total = sum(
        float(i.amount) - float(i.received_amount or 0)
        for i in overdue_q.all()
    )

    rate = round(total_paid_actual / total_all * 100 if total_all else 0, 1)

    return render_template('reports/fees.html',
                           total_all=total_all,
                           total_paid=total_paid_actual,
                           total_partial=total_partial,
                           total_pending=total_pending,
                           overdue_total=overdue_total,
                           overdue_list=overdue_list,
                           rate=rate,
                           today=d.today())


# ── ATTENDANCE ────────────────────────────────────────────────────────────────
@reports_bp.route('/attendance')
@login_required
@permission_required('view_reports')
def attendance_report():
    from datetime import datetime as dt
    school = get_current_school()
    school_id = school.id if school else None

    start_str = request.args.get('start', date.today().replace(day=1).isoformat())
    end_str   = request.args.get('end',   date.today().isoformat())
    start_d   = dt.strptime(start_str, '%Y-%m-%d').date()
    end_d     = dt.strptime(end_str,   '%Y-%m-%d').date()
    section_id = request.args.get('section_id', type=int)

    sec_q = Section.query.join(Grade).order_by(Grade.name)
    if school_id:
        sec_q = sec_q.filter(Grade.school_id == school_id)
    sections = sec_q.all()

    rows = []
    if section_id:
        stud_q = Student.query.filter_by(section_id=section_id, status='active')
        if school_id:
            stud_q = stud_q.filter_by(school_id=school_id)
        students = stud_q.all()
        for s in students:
            att_q = (StudentAttendance.query
                     .filter_by(student_id=s.id)
                     .filter(StudentAttendance.date.between(start_d, end_d)))
            if school_id:
                att_q = att_q.filter_by(school_id=school_id)
            recs = att_q.all()
            p = sum(1 for r in recs if r.status == 'present')
            a = sum(1 for r in recs if r.status == 'absent')
            l = sum(1 for r in recs if r.status == 'late')
            total = p + a + l
            rows.append({'student': s, 'present': p, 'absent': a,
                         'late': l, 'total': total,
                         'rate': round((p + l) / total * 100 if total else 0, 1)})
        rows.sort(key=lambda x: x['rate'], reverse=True)

    return render_template('reports/attendance.html',
                           rows=rows, sections=sections,
                           section_id=section_id,
                           start=start_str, end=end_str)


# ── SALARY ────────────────────────────────────────────────────────────────────
@reports_bp.route('/salaries')
@login_required
@permission_required('view_reports')
def salary_report():
    school = get_current_school()
    school_id = school.id if school else None

    year  = request.args.get('year', date.today().year, type=int)

    sal_q = (db.session.query(
                Employee.full_name, Employee.job_title,
                func.count(SalaryRecord.id).label('months'),
                func.coalesce(func.sum(SalaryRecord.net_salary), 0).label('total'))
             .join(SalaryRecord, SalaryRecord.employee_id == Employee.id)
             .filter(SalaryRecord.year == year,
                     SalaryRecord.status != 'cancelled'))
    if school_id:
        sal_q = sal_q.filter(SalaryRecord.school_id == school_id)
    rows = (sal_q.group_by(Employee.full_name, Employee.job_title)
                 .order_by(func.sum(SalaryRecord.net_salary).desc()).all())

    grand_q = db.session.query(func.coalesce(func.sum(SalaryRecord.net_salary), 0))\
                        .filter(SalaryRecord.year == year,
                                SalaryRecord.status != 'cancelled')
    if school_id:
        grand_q = grand_q.filter(SalaryRecord.school_id == school_id)
    grand_total = float(grand_q.scalar())

    return render_template('reports/salary.html',
                           rows=rows, year=year,
                           grand_total=grand_total)

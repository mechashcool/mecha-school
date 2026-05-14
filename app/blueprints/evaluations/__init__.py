"""
Employee evaluations blueprint.
"""
from decimal import Decimal

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for)
from flask_login import current_user, login_required

from app.models import db, Employee, EmployeeEvaluation, School
from app.utils.decorators import (get_active_year, get_current_school,
                                   permission_required, get_view_year, historical_guard)


evaluations_bp = Blueprint(
    'evaluations',
    __name__,
    template_folder='../../templates/evaluations',
)


def _is_super_admin():
    return bool(current_user.is_authenticated and current_user.is_super_admin)


def _schools_for_selector():
    if not _is_super_admin():
        return []
    return (School.query
            .filter_by(is_active=True)
            .order_by(School.school_name)
            .all())


def _selected_school():
    """Return the school selected for this evaluation operation."""
    if _is_super_admin():
        school_id = request.values.get('school_id', type=int)
        if school_id:
            return School.query.get_or_404(school_id)
        return get_current_school()
    return get_current_school()


def _employee_query_for_school(school):
    if not school:
        return None

    query = Employee.query
    if _is_super_admin():
        query = query.execution_options(bypass_tenant_scope=True)

    return (query
            .filter(Employee.school_id == school.id,
                    Employee.status == 'active')
            .order_by(Employee.full_name))


def _employees_for_school(school):
    query = _employee_query_for_school(school)
    return query.all() if query is not None else []


def _load_employee_for_school(employee_id, school):
    query = _employee_query_for_school(school)
    if query is None or not employee_id:
        return None
    return query.filter(Employee.id == employee_id).first()


def _evaluation_query():
    query = EmployeeEvaluation.query
    if _is_super_admin():
        query = query.execution_options(
            bypass_tenant_scope=True,
            include_all_years=True,
        )
    return query


def _redirect_to_index(school_id=None):
    if _is_super_admin() and school_id:
        return redirect(url_for('evaluations.index', school_id=school_id))
    return redirect(url_for('evaluations.index'))


def _score(name, default):
    value = request.form.get(name, default, type=int)
    return max(1, min(10, value or default))


@evaluations_bp.route('/')
@login_required
@permission_required('manage_employees')
def index():
    page = request.args.get('page', 1, type=int)
    school = _selected_school()
    year = get_view_year(school.id) if school else None

    query = _evaluation_query().join(Employee)
    if school and year:
        query = query.filter(EmployeeEvaluation.school_id == school.id,
                             EmployeeEvaluation.academic_year_id == year.id)
    else:
        query = query.filter(EmployeeEvaluation.id == -1)

    evals = (query
             .order_by(EmployeeEvaluation.created_at.desc())
             .paginate(page=page, per_page=20))
    return render_template(
        'evaluations/index.html',
        evals=evals,
        schools=_schools_for_selector(),
        selected_school_id=school.id if school else None,
        is_super_admin=_is_super_admin(),
    )


@evaluations_bp.route('/create', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_employees')
def create():
    school = _selected_school()
    employees = _employees_for_school(school)

    if request.method == 'POST':
        employee = _load_employee_for_school(
            request.form.get('employee_id', type=int),
            school,
        )
        if not employee:
            abort(403)

        year = get_active_year(employee.school_id)
        if not year:
            flash('Select a school with an active academic year before creating evaluations.', 'danger')
            return _redirect_to_index(employee.school_id)

        perf = _score('performance', 5)
        disc = _score('discipline', 5)
        att = _score('attendance_score', 5)
        final = Decimal(str(round((perf + disc + att) / 3, 2)))

        ev = EmployeeEvaluation(
            employee_id=employee.id,
            evaluator_id=current_user.id,
            school_id=employee.school_id,
            academic_year_id=year.id,
            period=request.form.get('period', '').strip(),
            performance=perf,
            discipline=disc,
            attendance_score=att,
            final_score=final,
            notes=request.form.get('notes', '').strip(),
        )
        db.session.add(ev)
        db.session.commit()
        flash('Evaluation saved.', 'success')
        return _redirect_to_index(employee.school_id)

    return render_template(
        'evaluations/form.html',
        employees=employees,
        record=None,
        schools=_schools_for_selector(),
        selected_school_id=school.id if school else None,
        is_super_admin=_is_super_admin(),
    )


@evaluations_bp.route('/<int:ev_id>/edit', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_employees')
def edit(ev_id):
    ev = _evaluation_query().get_or_404(ev_id)
    year = get_active_year(ev.school_id)
    if not _is_super_admin():
        school = get_current_school()
        if (not school or ev.school_id != school.id or
                not year or ev.academic_year_id != year.id):
            abort(403)

    employees = _employees_for_school(ev.school)
    if request.method == 'POST':
        perf = _score('performance', ev.performance)
        disc = _score('discipline', ev.discipline)
        att = _score('attendance_score', ev.attendance_score)
        ev.performance = perf
        ev.discipline = disc
        ev.attendance_score = att
        ev.final_score = Decimal(str(round((perf + disc + att) / 3, 2)))
        ev.notes = request.form.get('notes', '').strip()
        ev.period = request.form.get('period', ev.period).strip()
        db.session.commit()
        flash('Evaluation updated.', 'success')
        return _redirect_to_index(ev.school_id)

    return render_template(
        'evaluations/form.html',
        employees=employees,
        record=ev,
        schools=_schools_for_selector(),
        selected_school_id=ev.school_id,
        is_super_admin=_is_super_admin(),
    )


@evaluations_bp.route('/<int:ev_id>/delete', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_employees')
def delete(ev_id):
    ev = _evaluation_query().get_or_404(ev_id)
    if not _is_super_admin():
        school = get_current_school()
        year = get_active_year(school.id) if school else None
        if (not school or ev.school_id != school.id or
                not year or ev.academic_year_id != year.id):
            abort(403)

    school_id = ev.school_id
    db.session.delete(ev)
    db.session.commit()
    flash('Evaluation deleted.', 'success')
    return _redirect_to_index(school_id)

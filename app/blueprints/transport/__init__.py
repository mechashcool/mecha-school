"""Mecha-School – Transport Routes Blueprint

Routes for managing school bus/van routes and linking students to them.
Permission: manage_transport
"""
import calendar
from datetime import date, datetime as dt

from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, abort)
from flask_login import login_required, current_user
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.models import (db, TransportRoute, StudentTransport, Student,
                        Section, Grade, Employee, User, Role, DRIVER_ROLE)
from app.utils import code_generator
from app.utils.decorators import (permission_required, get_current_school,
                                   historical_guard)
from app.utils.audit import log_action
from app.utils.device_numbering import (DeviceNumberAllocationError,
                                        DeviceNumberConflictError,
                                        map_new_employee_to_devices)

transport_bp = Blueprint('transport', __name__,
                         template_folder='../../templates/transport')


def _scope_route(route_id):
    """Load a TransportRoute and verify it belongs to the current school."""
    school = get_current_school()
    route = TransportRoute.query.get_or_404(route_id)
    if school and route.school_id != school.id:
        abort(403)
    return school, route


# ─────────────────────────────────────────────────────────────────────────────
#  INDEX — list all routes
# ─────────────────────────────────────────────────────────────────────────────

@transport_bp.route('/')
@login_required
@permission_required('manage_transport')
def index():
    school      = get_current_school()
    status_f    = request.args.get('status', 'all')
    route_id_f  = request.args.get('route_id', type=int)

    # All routes for the dropdown (unfiltered by status/id, scoped to school)
    all_routes_q = TransportRoute.query
    if school:
        all_routes_q = all_routes_q.filter_by(school_id=school.id)
    all_routes = all_routes_q.order_by(TransportRoute.name).all()

    # Displayed routes — apply status and optional route-id filter
    query = TransportRoute.query
    if school:
        query = query.filter_by(school_id=school.id)
    if status_f != 'all':
        query = query.filter_by(status=status_f)
    if route_id_f:
        query = query.filter_by(id=route_id_f)
    routes = query.order_by(TransportRoute.name).all()

    # Active student count per route (one query with GROUP BY)
    if routes:
        route_ids = [r.id for r in routes]
        counts_q = (
            db.session.query(
                StudentTransport.route_id,
                func.count(StudentTransport.id).label('cnt'),
            )
            .filter(StudentTransport.route_id.in_(route_ids))
            .filter_by(status='active')
            .group_by(StudentTransport.route_id)
            .all()
        )
        counts = {row.route_id: row.cnt for row in counts_q}
    else:
        counts = {}

    return render_template('transport/index.html',
                           routes=routes, counts=counts,
                           all_routes=all_routes,
                           status_f=status_f, route_id_f=route_id_f)


# ─────────────────────────────────────────────────────────────────────────────
#  CREATE
# ─────────────────────────────────────────────────────────────────────────────

@transport_bp.route('/create', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_transport')
def create():
    school = get_current_school()
    if not school:
        flash('لم يتم تحديد مدرسة حالية.', 'danger')
        return redirect(url_for('transport.index'))

    if request.method == 'POST':
        fd = request.form
        errors = _validate_form(fd)
        driver = None
        if not errors:
            driver, driver_error = _apply_driver_choice(fd, school)
            if driver_error:
                errors.append(driver_error)
        if not errors:
            route = TransportRoute(
                school_id      = school.id,
                name           = fd['name'].strip(),
                route_number   = fd.get('route_number', '').strip() or None,
                supervisor     = fd.get('supervisor', '').strip() or None,
                vehicle_type   = fd['vehicle_type'].strip(),
                vehicle_number = fd['vehicle_number'].strip(),
                capacity       = int(fd['capacity']),
                status         = fd.get('status', 'active'),
            )
            _set_route_driver(route, driver, fd)
            db.session.add(route)
            db.session.commit()
            log_action('create', 'transport_route', route.id,
                       details=f'name={route.name}')
            _after_driver_commit(route.id, None, driver)
            flash(f'تم إضافة خط النقل "{route.name}" بنجاح.', 'success')
            return redirect(url_for('transport.detail', route_id=route.id))

        for err in errors:
            flash(err, 'danger')
        return _render_form(None, fd, school)

    return _render_form(None, {}, school)


# ─────────────────────────────────────────────────────────────────────────────
#  DETAIL — view route + linked students
# ─────────────────────────────────────────────────────────────────────────────

@transport_bp.route('/<int:route_id>')
@login_required
@permission_required('manage_transport')
def detail(route_id):
    school, route = _scope_route(route_id)

    # Optional residential-area filter for the "ربط طالب بالخط" picker. Reuses
    # the same ResidentialArea model/helper and the same filtering logic as the
    # Students page (Student.residential_area_id == id applied on a query already
    # scoped to the current school).
    residential_area_id = request.args.get('residential_area_id', type=int)

    links = (StudentTransport.query
             .filter_by(route_id=route_id)
             .join(Student, StudentTransport.student_id == Student.id)
             .order_by(StudentTransport.status.desc(), Student.full_name)
             .all())

    linked_ids = [lk.student_id for lk in links]
    avail_q = Student.query.filter_by(status='active')
    if school:
        avail_q = avail_q.filter_by(school_id=school.id)
    if linked_ids:
        avail_q = avail_q.filter(~Student.id.in_(linked_ids))
    # Exclude students who already have an active subscription in any other route
    if school:
        active_in_other = (
            db.session.query(StudentTransport.student_id)
            .filter(
                StudentTransport.school_id == school.id,
                StudentTransport.route_id  != route_id,
                StudentTransport.status    == 'active',
            )
        )
        avail_q = avail_q.filter(~Student.id.in_(active_in_other))
    # Residential-area filter — fail-closed: avail_q is already scoped to this
    # school, so a manipulated foreign area id matches no rows and can never
    # expose or return another school's students.
    if residential_area_id:
        avail_q = avail_q.filter(Student.residential_area_id == residential_area_id)
    available_students = avail_q.order_by(Student.full_name).all()

    # Residential areas for the picker filter dropdown — this school only
    # (reuses the exact helper from the Students blueprint; local import avoids
    # any import-order coupling between blueprints).
    from app.blueprints.students import _school_residential_areas
    residential_areas_list = _school_residential_areas(school.id) if school else []

    active_count    = sum(1 for lk in links if lk.status == 'active')
    available_seats = max(0, route.capacity - active_count)
    is_full         = active_count >= route.capacity

    return render_template('transport/detail.html',
                           route=route, links=links,
                           available_students=available_students,
                           residential_areas_list=residential_areas_list,
                           residential_area_id=residential_area_id,
                           active_count=active_count,
                           available_seats=available_seats,
                           is_full=is_full)


# ─────────────────────────────────────────────────────────────────────────────
#  EDIT
# ─────────────────────────────────────────────────────────────────────────────

@transport_bp.route('/<int:route_id>/edit', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_transport')
def edit(route_id):
    school, route = _scope_route(route_id)

    if request.method == 'POST':
        fd = request.form
        errors = _validate_form(fd)
        if not errors:
            new_cap = int(fd['capacity'])
            active_count = (StudentTransport.query
                            .filter_by(route_id=route_id, status='active')
                            .count())
            if new_cap < active_count:
                errors.append(
                    f'لا يمكن تقليل الطاقة الاستيعابية إلى أقل من عدد الطلبة المشتركين حالياً '
                    f'({active_count} طالب فعّال).'
                )
        driver = None
        old_driver_id = route.driver_employee_id
        if not errors:
            driver, driver_error = _apply_driver_choice(fd, school)
            if driver_error:
                errors.append(driver_error)
        if not errors:
            route.name           = fd['name'].strip()
            route.route_number   = fd.get('route_number', '').strip() or None
            _set_route_driver(route, driver, fd)
            route.supervisor     = fd.get('supervisor', '').strip() or None
            route.vehicle_type   = fd['vehicle_type'].strip()
            route.vehicle_number = fd['vehicle_number'].strip()
            route.capacity       = int(fd['capacity'])
            route.status         = fd.get('status', 'active')
            db.session.commit()
            log_action('edit', 'transport_route', route.id,
                       details=f'name={route.name}')
            _after_driver_commit(route.id, old_driver_id, driver)
            flash('تم تحديث بيانات الخط بنجاح.', 'success')
            return redirect(url_for('transport.detail', route_id=route.id))

        for err in errors:
            flash(err, 'danger')
        return _render_form(route, fd, school)

    # Pre-fill form with existing values
    fd = {
        'name':           route.name,
        'route_number':   route.route_number or '',
        'driver_name':    route.driver_name,
        'driver_phone':   route.driver_phone,
        'supervisor':     route.supervisor or '',
        'vehicle_type':   route.vehicle_type,
        'vehicle_number': route.vehicle_number,
        'capacity':       route.capacity,
        'status':         route.status,
        'driver_mode':    'existing' if route.driver_employee_id else 'manual',
        'driver_employee_id': route.driver_employee_id or '',
    }
    return _render_form(route, fd, school)


# ─────────────────────────────────────────────────────────────────────────────
#  DELETE
# ─────────────────────────────────────────────────────────────────────────────

@transport_bp.route('/<int:route_id>/delete', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_transport')
def delete(route_id):
    school, route = _scope_route(route_id)

    active_count = route.students_links.filter_by(status='active').count()
    if active_count > 0:
        flash(
            f'لا يمكن حذف الخط لأنه يحتوي على {active_count} طالب مشترك فعّال. '
            'يرجى إلغاء اشتراكهم أولاً أو تغيير حالتهم إلى "متوقف".',
            'danger',
        )
        return redirect(url_for('transport.detail', route_id=route_id))

    route_name = route.name
    db.session.delete(route)
    db.session.commit()
    log_action('delete', 'transport_route', route_id,
               details=f'name={route_name}')
    flash(f'تم حذف خط النقل "{route_name}" بنجاح.', 'success')
    return redirect(url_for('transport.index'))


# ─────────────────────────────────────────────────────────────────────────────
#  ADD STUDENT TO ROUTE
# ─────────────────────────────────────────────────────────────────────────────

@transport_bp.route('/<int:route_id>/students/add', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_transport')
def add_student(route_id):
    school, route = _scope_route(route_id)

    student_id     = request.form.get('student_id', type=int)
    sub_status     = request.form.get('status', 'active')
    start_date_str = request.form.get('start_date', '').strip()
    notes          = request.form.get('notes', '').strip()

    if not student_id:
        flash('يرجى اختيار طالب.', 'danger')
        return redirect(url_for('transport.detail', route_id=route_id))

    student = Student.query.get_or_404(student_id)
    if school and student.school_id != school.id:
        abort(403)

    if StudentTransport.query.filter_by(route_id=route_id,
                                        student_id=student_id).first():
        flash(f'الطالب {student.full_name} مضاف مسبقاً لهذا الخط.', 'warning')
        return redirect(url_for('transport.detail', route_id=route_id))

    # Block adding the student if they already have an active subscription elsewhere
    if sub_status == 'active' and school:
        active_elsewhere = (
            StudentTransport.query
            .filter(
                StudentTransport.student_id == student_id,
                StudentTransport.school_id  == school.id,
                StudentTransport.route_id   != route_id,
                StudentTransport.status     == 'active',
            )
            .first()
        )
        if active_elsewhere:
            flash(
                f'لا يمكن إضافة الطالب "{student.full_name}" '
                'لأن لديه اشتراك فعّال في خط نقل آخر. '
                'يرجى إيقاف اشتراكه الحالي أولاً ثم إعادة المحاولة.',
                'danger',
            )
            return redirect(url_for('transport.detail', route_id=route_id))

    # Capacity enforcement: only active subscriptions count against capacity
    if sub_status == 'active':
        active_count = (StudentTransport.query
                        .filter_by(route_id=route_id, status='active')
                        .count())
        if active_count >= route.capacity:
            flash(
                'لا يمكن إضافة الطالب، تم الوصول إلى الطاقة الاستيعابية لهذا الخط.',
                'danger',
            )
            return redirect(url_for('transport.detail', route_id=route_id))

    start_date = None
    if start_date_str:
        try:
            start_date = dt.strptime(start_date_str, '%Y-%m-%d').date()
        except ValueError:
            pass

    link = StudentTransport(
        school_id  = school.id,
        route_id   = route_id,
        student_id = student_id,
        status     = sub_status,
        start_date = start_date,
        notes      = notes or None,
    )
    db.session.add(link)
    db.session.commit()
    log_action('create', 'student_transport', link.id,
               details=f'student={student_id} route={route_id}')
    flash(f'تم ربط الطالب {student.full_name} بالخط بنجاح.', 'success')
    return redirect(url_for('transport.detail', route_id=route_id))


# ─────────────────────────────────────────────────────────────────────────────
#  REMOVE STUDENT FROM ROUTE
# ─────────────────────────────────────────────────────────────────────────────

@transport_bp.route('/students/<int:link_id>/remove', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_transport')
def remove_student(link_id):
    school = get_current_school()
    link = StudentTransport.query.get_or_404(link_id)
    if school and link.school_id != school.id:
        abort(403)

    route_id = link.route_id
    name     = link.student.full_name
    db.session.delete(link)
    db.session.commit()
    log_action('delete', 'student_transport', link_id,
               details=f'student_name={name} route_id={route_id}')
    flash(f'تم إزالة الطالب {name} من الخط.', 'success')
    return redirect(url_for('transport.detail', route_id=route_id))


# ─────────────────────────────────────────────────────────────────────────────
#  TOGGLE STUDENT STATUS (active ↔ inactive without full remove)
# ─────────────────────────────────────────────────────────────────────────────

@transport_bp.route('/students/<int:link_id>/toggle', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_transport')
def toggle_student(link_id):
    school = get_current_school()
    link = StudentTransport.query.get_or_404(link_id)
    if school and link.school_id != school.id:
        abort(403)

    if link.status == 'inactive':
        # Activating — enforce capacity before switching
        tr = TransportRoute.query.get(link.route_id)
        if tr:
            active_count = (StudentTransport.query
                            .filter_by(route_id=link.route_id, status='active')
                            .count())
            if active_count >= tr.capacity:
                flash(
                    'لا يمكن تفعيل اشتراك الطالب، تم الوصول إلى الطاقة الاستيعابية لهذا الخط.',
                    'danger',
                )
                return redirect(url_for('transport.detail', route_id=link.route_id))
    link.status = 'inactive' if link.status == 'active' else 'active'
    db.session.commit()
    label = 'فعّال' if link.status == 'active' else 'متوقف'
    flash(f'تم تغيير حالة اشتراك {link.student.full_name} إلى {label}.', 'info')
    return redirect(url_for('transport.detail', route_id=link.route_id))


# ─────────────────────────────────────────────────────────────────────────────
#  REPORT
# ─────────────────────────────────────────────────────────────────────────────

@transport_bp.route('/report')
@login_required
@permission_required('manage_transport')
def report():
    school = get_current_school()
    today  = date.today()

    month          = request.args.get('month', today.month, type=int)
    year           = request.args.get('year',  today.year,  type=int)
    route_id_f     = request.args.get('route_id', type=int)
    status_f       = request.args.get('status', 'active')

    # Clamp month to valid range
    month = max(1, min(12, month))

    # ── Summary: active students per route ───────────────────────────────────
    routes_q = TransportRoute.query
    if school:
        routes_q = routes_q.filter_by(school_id=school.id)
    all_routes = routes_q.order_by(TransportRoute.name).all()

    route_ids = [r.id for r in all_routes]
    summary_counts = {}
    if route_ids:
        rows = (
            db.session.query(
                StudentTransport.route_id,
                func.count(StudentTransport.id).label('cnt'),
            )
            .filter(StudentTransport.route_id.in_(route_ids))
            .filter_by(status='active')
            .group_by(StudentTransport.route_id)
            .all()
        )
        summary_counts = {row.route_id: row.cnt for row in rows}

    # ── Monthly list ─────────────────────────────────────────────────────────
    # Show subscriptions that started on or before the last day of the month.
    last_day        = calendar.monthrange(year, month)[1]
    end_of_month    = date(year, month, last_day)

    monthly_q = (
        StudentTransport.query
        .join(Student,        StudentTransport.student_id == Student.id)
        .join(TransportRoute, StudentTransport.route_id   == TransportRoute.id)
    )
    if school:
        monthly_q = monthly_q.filter(StudentTransport.school_id == school.id)
    if route_id_f:
        monthly_q = monthly_q.filter(StudentTransport.route_id == route_id_f)
    if status_f and status_f != 'all':
        monthly_q = monthly_q.filter(StudentTransport.status == status_f)

    # Active during this month: start_date is NULL (no date recorded) or ≤ end_of_month
    monthly_q = monthly_q.filter(
        db.or_(
            StudentTransport.start_date.is_(None),
            StudentTransport.start_date <= end_of_month,
        )
    )
    monthly_records = (monthly_q
                       .order_by(TransportRoute.name, Student.full_name)
                       .all())

    return render_template(
        'transport/report.html',
        all_routes      = all_routes,
        route_summary   = [{'route': r,
                             'active': summary_counts.get(r.id, 0)} for r in all_routes],
        monthly_records = monthly_records,
        month           = month,
        year            = year,
        route_id_f      = route_id_f,
        status_f        = status_f,
        month_name      = _month_ar(month),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _validate_form(fd):
    errors = []
    if not fd.get('name', '').strip():
        errors.append('اسم الخط مطلوب.')
    # Free-text driver fields are required only in the manual (legacy) mode; a
    # linked driver Employee supplies them instead (see _set_route_driver).
    if _driver_mode(fd) == 'manual':
        if not fd.get('driver_name', '').strip():
            errors.append('اسم السائق مطلوب.')
        if not fd.get('driver_phone', '').strip():
            errors.append('رقم هاتف السائق مطلوب.')
    if not fd.get('vehicle_type', '').strip():
        errors.append('نوع المركبة مطلوب.')
    if not fd.get('vehicle_number', '').strip():
        errors.append('رقم المركبة مطلوب.')
    cap_str = fd.get('capacity', '').strip()
    if not cap_str:
        errors.append('الطاقة الاستيعابية مطلوبة.')
    else:
        try:
            cap = int(cap_str)
            if cap <= 0:
                errors.append('الطاقة الاستيعابية يجب أن تكون رقماً موجباً أكبر من صفر.')
        except ValueError:
            errors.append('الطاقة الاستيعابية يجب أن تكون رقماً صحيحاً.')
    return errors


# ─────────────────────────────────────────────────────────────────────────────
#  DRIVER (Employee + linked `driver` User) — create / select from route form
# ─────────────────────────────────────────────────────────────────────────────

# Same default classification the employee module already offers for drivers
# (app/utils/employee_classification.py).
DRIVER_JOB_TITLE  = 'سائق'
DRIVER_DEPARTMENT = 'النقل'
_DRIVER_MODES = ('manual', 'existing', 'new')
_MSG_DRIVER_SAVE_FAILED = 'تعذّر حفظ بيانات السائق. لم يتم حفظ أي تغيير. يرجى المحاولة مرة أخرى.'
_MSG_DRIVER_NEEDS_EMPLOYEE_SHIFT = (
    'نظام شفتات الموظفين مفعّل لهذه المدرسة، ولا يمكن إنشاء سائق جديد من هنا بدون '
    'شفت صالح. أضف السائق من صفحة الموظفين (المسمى الوظيفي: سائق) مع اختيار شفت صالح، '
    'ثم اختره هنا كسائق موجود.')


def _driver_mode(fd):
    """'manual' (legacy free text), 'existing' or 'new'. Unknown → 'invalid'."""
    mode = (fd.get('driver_mode') or 'manual').strip()
    return mode if mode in _DRIVER_MODES else 'invalid'


def _driver_options(school):
    """Same-school ACTIVE Employees classified as driver, with account state.

    Two queries total (employees, then their linked accounts) — never one per
    employee. account: 'driver' (active driver account, linkable), 'none' (an
    account will be created) or 'other' (another role / disabled / not this
    school — rejected on save).
    """
    if not school:
        return []
    emps = (Employee.query
            .execution_options(bypass_tenant_scope=True)
            .filter(Employee.school_id == school.id,
                    Employee.status == 'active',
                    Employee.job_title == DRIVER_JOB_TITLE)
            .order_by(Employee.full_name)
            .all())
    user_ids = [e.user_id for e in emps if e.user_id]
    accounts = {}
    if user_ids:
        rows = (db.session.query(User.id, User.is_active, User.school_id, Role.name)
                .join(Role, User.role_id == Role.id)
                .filter(User.id.in_(user_ids), User.school_id == school.id)
                .execution_options(bypass_tenant_scope=True)
                .all())
        accounts = {r[0]: r for r in rows}
    options = []
    for e in emps:
        if not e.user_id:
            state = 'none'
        else:
            acct = accounts.get(e.user_id)
            state = ('driver' if acct and acct[3] == DRIVER_ROLE and acct[1]
                     else 'other')
        options.append({'id': e.id, 'name': e.full_name,
                        'employee_id': e.employee_id, 'phone': e.phone or '',
                        'account': state})
    return options


def _render_form(route, fd, school):
    return render_template('transport/form.html', route=route, fd=fd,
                           driver_options=_driver_options(school))


def _account_creation_error(school):
    """Creating an Employee / login account from this page needs the same
    authority as the employee module — never manage_transport alone."""
    if not current_user.has_permission('manage_employees'):
        return 'إنشاء سائق جديد أو حساب دخول له يتطلب صلاحية إدارة الموظفين.'
    if not current_user.is_super_admin:
        from app.utils.school_config import get_school_config
        if not get_school_config(school.id).action_enabled('employees', 'create'):
            return 'إضافة الموظفين غير مفعلة لهذه المدرسة.'
    return None


def _create_driver_account(employee, school, role):
    """Create ONE `driver` login for `employee` and link it (flush, no commit).

    Reuses the existing credential generators (same as the employee wizard):
    globally-unique short username + generated password, stored only as a
    bcrypt hash via User.set_password. Returns (user, username, password); the
    plaintext is shown once to the creator and never stored or logged.
    """
    username = code_generator.generate_parent_username()
    password = code_generator.generate_parent_password()
    user = User(username=username, full_name=employee.full_name,
                phone=(employee.phone or None), role_id=role.id,
                school_id=school.id, is_active=True)
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    employee.user_id = user.id
    return user, username, password


def _apply_driver_choice(fd, school):
    """Resolve the driver section of the route form.

    Returns (driver, error). driver is None for the manual (legacy) mode, else
    {'employee', 'new_employee_id', 'new_user_id', 'credentials'}. Any rows it
    stages (Employee / User) are flushed into the caller's transaction, which
    commits them together with the route — or rolls everything back here on
    error. The school always comes from the server-side current school; the
    submitted employee id is only a lookup key, re-validated against it.
    """
    mode = _driver_mode(fd)
    if mode == 'invalid':
        return None, 'خيار السائق غير صالح.'
    if mode == 'manual':
        return None, None
    if not school:
        return None, 'يرجى اختيار مدرسة أولاً.'

    try:
        driver, error = _stage_driver(fd, school, mode)
    except IntegrityError:
        db.session.rollback()
        return None, _MSG_DRIVER_SAVE_FAILED
    if error:
        db.session.rollback()
        return None, error
    driver['employee_id'] = driver['employee'].id
    return driver, None


def _stage_driver(fd, school, mode):
    role = None
    if mode == 'existing':
        emp_id = fd.get('driver_employee_id', type=int)
        emp = None
        if emp_id:
            # Row lock: two concurrent saves for an account-less driver must not
            # create two accounts — the second waits and then sees user_id set.
            emp = (Employee.query
                   .execution_options(bypass_tenant_scope=True)
                   .filter_by(id=emp_id, school_id=school.id, status='active',
                              job_title=DRIVER_JOB_TITLE)
                   .with_for_update()
                   .first())
        if emp is None:
            return None, 'السائق المحدد غير موجود أو غير فعّال في هذه المدرسة.'

        if emp.user_id:
            user = db.session.get(User, emp.user_id,
                                  execution_options={'bypass_tenant_scope': True})
            if user is None or user.school_id != school.id:
                return None, 'حساب الدخول المرتبط بهذا السائق لا يعود لهذه المدرسة.'
            if not user.role or user.role.name != DRIVER_ROLE:
                return None, (f'الموظف "{emp.full_name}" مرتبط مسبقاً بحساب دخول بدور آخر. '
                              'لن يتم تغيير دور الحساب ولن يُنشأ حساب ثانٍ له. '
                              'راجع حساب الموظف من صفحة الموظفين أو اختر سائقاً آخر.')
            if not user.is_active:
                return None, ('حساب السائق المرتبط بهذا الموظف معطّل. '
                              'يرجى تفعيله من إدارة المستخدمين أولاً.')
            return {'employee': emp, 'new_employee_id': None,
                    'new_user_id': None, 'credentials': None}, None

        error = _account_creation_error(school)
        if error:
            return None, error
        role = Role.query.filter_by(name=DRIVER_ROLE).first()
        if role is None:
            return None, 'دور "سائق" غير موجود في النظام. يرجى مراجعة مسؤول النظام.'
        user, username, password = _create_driver_account(emp, school, role)
        return {'employee': emp, 'new_employee_id': None,
                'new_user_id': user.id, 'credentials': (username, password)}, None

    # mode == 'new'
    # The quick driver form has no shift selector: in employee shift mode a
    # new ACTIVE Employee would have no valid shift, so refuse (fail closed,
    # nothing is created) and point to the employee flow.
    if getattr(school, 'emp_enable_attendance_shifts', False):
        return None, _MSG_DRIVER_NEEDS_EMPLOYEE_SHIFT
    error = _account_creation_error(school)
    if error:
        return None, error
    full_name = ' '.join((fd.get('new_driver_name') or '').split())
    phone = (fd.get('new_driver_phone') or '').strip()
    if not full_name:
        return None, 'اسم السائق الجديد مطلوب.'
    if len(full_name) > 200:
        return None, 'اسم السائق يجب ألا يتجاوز 200 حرف.'
    if not phone:
        return None, 'رقم هاتف السائق الجديد مطلوب.'
    if len(phone) > 30:
        return None, 'رقم هاتف السائق يجب ألا يتجاوز 30 حرفاً.'
    role = Role.query.filter_by(name=DRIVER_ROLE).first()
    if role is None:
        return None, 'دور "سائق" غير موجود في النظام. يرجى مراجعة مسؤول النظام.'

    emp = Employee(employee_id=code_generator.generate_employee_id(school.id),
                   full_name=full_name, phone=phone,
                   job_title=DRIVER_JOB_TITLE, department=DRIVER_DEPARTMENT,
                   school_id=school.id)
    db.session.add(emp)
    db.session.flush()
    # Attendance-device binding of the new driver Employee (database only);
    # an error return makes _apply_driver_choice roll the whole stage back.
    try:
        map_new_employee_to_devices(emp.id, school.id)
    except DeviceNumberAllocationError as exc:
        return None, (str(exc) if isinstance(exc, DeviceNumberConflictError) else
                      'تعذر إنشاء رقم السائق على جهاز الحضور. لم يتم حفظ أي تغيير.')
    user, username, password = _create_driver_account(emp, school, role)
    return {'employee': emp, 'new_employee_id': emp.id,
            'new_user_id': user.id, 'credentials': (username, password)}, None


def _set_route_driver(route, driver, fd):
    """Link the route to the driver Employee (syncing the legacy text columns so
    existing consumers keep working), or keep the manual/legacy text."""
    if driver:
        emp = driver['employee']
        route.driver_employee_id = emp.id
        route.driver_name  = (emp.full_name or '')[:200]
        route.driver_phone = (emp.phone or '')[:30]
    else:
        route.driver_employee_id = None
        route.driver_name  = fd['driver_name'].strip()
        route.driver_phone = fd['driver_phone'].strip()


def _after_driver_commit(route_id, old_driver_id, driver):
    """Audit driver events and show new credentials once — after the commit.
    Credentials are never written to the audit log."""
    new_driver_id = driver['employee_id'] if driver else None
    if driver and driver['new_employee_id']:
        log_action('create', 'employee', driver['new_employee_id'],
                   details=f'driver employee created from transport route={route_id}')
    if driver and driver['new_user_id']:
        log_action('create', 'user', driver['new_user_id'],
                   details=f'driver account for employee={new_driver_id} '
                           f'from transport route={route_id}')
    if new_driver_id != old_driver_id:
        log_action('link_driver' if new_driver_id else 'unlink_driver',
                   'transport_route', route_id,
                   details=f'driver_employee {old_driver_id} -> {new_driver_id}')
    if driver and driver['credentials']:
        username, password = driver['credentials']
        flash('تم إنشاء حساب دخول للسائق (تطبيق الهاتف). '
              f'اسم المستخدم: {username} — كلمة المرور: {password}. '
              'يرجى حفظ هذه البيانات وتسليمها للسائق.', 'success')


def _month_ar(m):
    names = ['يناير','فبراير','مارس','أبريل','مايو','يونيو',
             'يوليو','أغسطس','سبتمبر','أكتوبر','نوفمبر','ديسمبر']
    return names[m - 1] if 1 <= m <= 12 else ''

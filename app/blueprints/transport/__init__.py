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

from app.models import (db, TransportRoute, StudentTransport, Student,
                        Section, Grade)
from app.utils.decorators import (permission_required, get_current_school,
                                   historical_guard)
from app.utils.audit import log_action

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
        if not errors:
            route = TransportRoute(
                school_id      = school.id,
                name           = fd['name'].strip(),
                route_number   = fd.get('route_number', '').strip() or None,
                driver_name    = fd['driver_name'].strip(),
                driver_phone   = fd['driver_phone'].strip(),
                supervisor     = fd.get('supervisor', '').strip() or None,
                vehicle_type   = fd['vehicle_type'].strip(),
                vehicle_number = fd['vehicle_number'].strip(),
                capacity       = int(fd['capacity']),
                status         = fd.get('status', 'active'),
            )
            db.session.add(route)
            db.session.commit()
            log_action('create', 'transport_route', route.id,
                       details=f'name={route.name}')
            flash(f'تم إضافة خط النقل "{route.name}" بنجاح.', 'success')
            return redirect(url_for('transport.detail', route_id=route.id))

        for err in errors:
            flash(err, 'danger')
        return render_template('transport/form.html', route=None, fd=fd)

    return render_template('transport/form.html', route=None, fd={})


# ─────────────────────────────────────────────────────────────────────────────
#  DETAIL — view route + linked students
# ─────────────────────────────────────────────────────────────────────────────

@transport_bp.route('/<int:route_id>')
@login_required
@permission_required('manage_transport')
def detail(route_id):
    school, route = _scope_route(route_id)

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
    available_students = avail_q.order_by(Student.full_name).all()

    active_count    = sum(1 for lk in links if lk.status == 'active')
    available_seats = max(0, route.capacity - active_count)
    is_full         = active_count >= route.capacity

    return render_template('transport/detail.html',
                           route=route, links=links,
                           available_students=available_students,
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
        if not errors:
            route.name           = fd['name'].strip()
            route.route_number   = fd.get('route_number', '').strip() or None
            route.driver_name    = fd['driver_name'].strip()
            route.driver_phone   = fd['driver_phone'].strip()
            route.supervisor     = fd.get('supervisor', '').strip() or None
            route.vehicle_type   = fd['vehicle_type'].strip()
            route.vehicle_number = fd['vehicle_number'].strip()
            route.capacity       = int(fd['capacity'])
            route.status         = fd.get('status', 'active')
            db.session.commit()
            log_action('edit', 'transport_route', route.id,
                       details=f'name={route.name}')
            flash('تم تحديث بيانات الخط بنجاح.', 'success')
            return redirect(url_for('transport.detail', route_id=route.id))

        for err in errors:
            flash(err, 'danger')
        return render_template('transport/form.html', route=route, fd=fd)

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
    }
    return render_template('transport/form.html', route=route, fd=fd)


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


def _month_ar(m):
    names = ['يناير','فبراير','مارس','أبريل','مايو','يونيو',
             'يوليو','أغسطس','سبتمبر','أكتوبر','نوفمبر','ديسمبر']
    return names[m - 1] if 1 <= m <= 12 else ''

"""
Mecha-School — Schools Blueprint  (Phase 6: Multi-Tenant)

Routes:
  GET  /schools/                  — global overview (all schools, stats)
  GET/POST /schools/create        — add a new school
  GET/POST /schools/<id>/edit     — edit school (name, capacity, settings)
  POST /schools/<id>/delete       — delete school
  POST /schools/<id>/switch       — switch active school (super admin)

  GET  /schools/<id>/years        — manage academic years for a school
  POST /schools/<id>/years/create — add academic year
  POST /schools/<id>/years/<yid>/activate — set as current year
  POST /schools/<id>/years/<yid>/delete   — delete year

All routes are super-admin only (role.name == 'super_admin').
"""
from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, session, jsonify)
from flask_login import login_required, current_user
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from datetime import datetime as dt

from app.models import (db, School, AcademicYear, Student, Employee,
                         User)
from app.utils.decorators import super_admin_required
from app.utils.audit import log_action
from app.utils.school_cleanup import (
    cleanup_school_cascade, format_linked_counts, is_demo_school,
    linked_school_counts,
)

schools_bp = Blueprint('schools', __name__,
                        template_folder='../../templates/schools')


def _school_form_context(school=None):
    return {'school': school}


MANAGER_ROLE_NAMES = frozenset({'school_admin'})


def _is_linked_school_manager(user):
    """School managers are school-bound users with an admin/manager role."""
    if not user or not user.school_id or not user.role:
        return False
    role_name = (user.role.name or '').strip().lower()
    return role_name in MANAGER_ROLE_NAMES


# ─────────────────────────────────────────────────────────────────────────────
#  GLOBAL OVERVIEW
# ─────────────────────────────────────────────────────────────────────────────

@schools_bp.route('/')
@login_required
@super_admin_required
def index():
    """Global dashboard: all schools with student counts vs capacity."""
    schools = School.query.filter_by(is_active=True).order_by(School.id).all()

    school_data = []
    for s in schools:
        current_year = AcademicYear.query.execution_options(bypass_tenant_scope=True)\
            .filter_by(school_id=s.id, is_current=True).first()
        student_count = Student.query.execution_options(
            bypass_tenant_scope=True, include_all_years=True
        ).filter_by(school_id=s.id, status='active').count()
        employee_count = Employee.query.execution_options(
            bypass_tenant_scope=True
        ).filter_by(school_id=s.id, status='active').count()
        capacity_pct = 0
        if s.capacity and s.capacity > 0:
            capacity_pct = min(100, round(student_count / s.capacity * 100))

        school_data.append({
            'school':        s,
            'current_year':  current_year,
            'students':      student_count,
            'employees':     employee_count,
            'capacity_pct':  capacity_pct,
            'at_capacity':   bool(s.capacity and student_count >= s.capacity),
        })

    active_school_id = session.get('active_school_id')
    return render_template('schools/index.html',
                           school_data=school_data,
                           active_school_id=active_school_id)


@schools_bp.route('/<int:school_id>')
@login_required
@super_admin_required
def detail(school_id):
    school = School.query.get_or_404(school_id)
    users = (User.query.execution_options(bypass_tenant_scope=True)
             .filter_by(school_id=school.id)
             .order_by(User.created_at.desc())
             .all())
    manager = next((u for u in users if _is_linked_school_manager(u)), None)

    current_year = AcademicYear.query.execution_options(bypass_tenant_scope=True)\
        .filter_by(school_id=school.id, is_current=True).first()
    student_count = Student.query.execution_options(
        bypass_tenant_scope=True, include_all_years=True
    ).filter_by(school_id=school.id, status='active').count()
    employee_count = Employee.query.execution_options(
        bypass_tenant_scope=True
    ).filter_by(school_id=school.id, status='active').count()

    return render_template('schools/detail.html',
                           school=school,
                           current_year=current_year,
                           users=users,
                           manager=manager,
                           student_count=student_count,
                           employee_count=employee_count)


# ─────────────────────────────────────────────────────────────────────────────
#  SCHOOL SWITCHER  (stores active school in session)
# ─────────────────────────────────────────────────────────────────────────────

@schools_bp.route('/<int:school_id>/switch', methods=['POST'])
@login_required
@super_admin_required
def switch(school_id):
    """Set the session-level active school for the super admin."""
    school = School.query.get_or_404(school_id)
    session['active_school_id'] = school.id
    flash(f'تم التبديل إلى مدرسة: {school.school_name}', 'success')
    next_url = request.form.get('next') or url_for('admin.dashboard')
    return redirect(next_url)


@schools_bp.route('/clear-switch', methods=['POST'])
@login_required
@super_admin_required
def clear_switch():
    """Return super admin to the global view (no school filter)."""
    session.pop('active_school_id', None)
    flash('تم الرجوع إلى العرض الشامل.', 'info')
    return redirect(url_for('schools.index'))


# ─────────────────────────────────────────────────────────────────────────────
#  CREATE SCHOOL
# ─────────────────────────────────────────────────────────────────────────────

@schools_bp.route('/create', methods=['GET', 'POST'])
@login_required
@super_admin_required
def create():
    if request.method == 'POST':
        name = request.form.get('school_name', '').strip()
        errors = []
        if not name:
            errors.append('School name is required.')

        capacity_raw = request.form.get('capacity', '0').strip()
        try:
            capacity = int(capacity_raw) if capacity_raw else 0
        except ValueError:
            capacity = 0

        code = request.form.get('code', '').strip() or None
        if code and School.query.filter_by(code=code).first():
            errors.append('School code is already in use.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('schools/form.html', school=None)

        from decimal import Decimal, InvalidOperation
        try:
            pps = Decimal(request.form.get('price_per_student', '0') or '0')
        except InvalidOperation:
            pps = Decimal('0')

        school = School(
            school_name       = name,
            school_name_ar    = request.form.get('school_name_ar', '').strip() or None,
            code              = code,
            capacity          = capacity,
            primary_color     = request.form.get('primary_color', '#0d6efd').strip(),
            address           = request.form.get('address', '').strip() or None,
            phone             = request.form.get('phone', '').strip() or None,
            email             = request.form.get('email', '').strip() or None,
            currency_code     = request.form.get('currency_code', 'IQD').strip() or 'IQD',
            currency_symbol   = request.form.get('currency_symbol', 'د.ع').strip() or 'د.ع',
            timezone          = request.form.get('timezone', 'Asia/Baghdad').strip(),
            locale            = request.form.get('locale', 'ar').strip() or 'ar',
            governorate       = request.form.get('governorate', '').strip() or None,
            price_per_student = pps,
        )

        try:
            db.session.add(school)
            db.session.flush()

            year_name = request.form.get('year_name', '').strip()
            if year_name:
                start_raw = request.form.get('year_start')
                end_raw = request.form.get('year_end')
                if start_raw and end_raw:
                    ay = AcademicYear(
                        school_id=school.id,
                        name=year_name,
                        start_date=dt.strptime(start_raw, '%Y-%m-%d').date(),
                        end_date=dt.strptime(end_raw, '%Y-%m-%d').date(),
                        is_current=True,
                    )
                    db.session.add(ay)

            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            flash(f'Could not create school: {exc}', 'danger')
            return render_template('schools/form.html', school=None)

        log_action('create', 'school', school.id, details=f'created school "{name}"')
        flash(f'School "{name}" was created successfully.', 'success')
        return redirect(url_for('schools.detail', school_id=school.id))

    return render_template('schools/form.html', school=None)


# ─────────────────────────────────────────────────────────────────────────────
#  EDIT SCHOOL
# ─────────────────────────────────────────────────────────────────────────────

@schools_bp.route('/<int:school_id>/edit', methods=['GET', 'POST'])
@login_required
@super_admin_required
def edit(school_id):
    school = School.query.get_or_404(school_id)

    if request.method == 'POST':
        name = request.form.get('school_name', '').strip()
        if not name:
            flash('اسم المدرسة مطلوب.', 'danger')
            return render_template('schools/form.html', school=school)

        capacity_raw = request.form.get('capacity', '0').strip()
        try:
            new_capacity = int(capacity_raw) if capacity_raw else 0
        except ValueError:
            new_capacity = 0

        # Warn if new capacity is below current student count
        if new_capacity > 0:
            current_count = Student.query.filter_by(
                school_id=school.id, status='active').count()
            if new_capacity < current_count:
                flash(
                    f'تحذير: السعة الجديدة ({new_capacity}) أقل من عدد الطلاب الحاليين '
                    f'({current_count}). سيتم حفظ الإعداد لكن لن يُقبل تسجيل طلاب جدد '
                    f'حتى يقل العدد عن الحد المحدد.',
                    'warning'
                )

        from decimal import Decimal, InvalidOperation
        try:
            pps = Decimal(request.form.get('price_per_student', '0') or '0')
        except InvalidOperation:
            pps = school.price_per_student or Decimal('0')

        old_name = school.school_name
        school.school_name        = name
        school.school_name_ar     = request.form.get('school_name_ar', '').strip() or None
        school.capacity           = new_capacity
        school.primary_color      = request.form.get('primary_color', school.primary_color).strip()
        school.address            = request.form.get('address', '').strip() or None
        school.phone              = request.form.get('phone', '').strip() or None
        school.email              = request.form.get('email', '').strip() or None
        school.currency_code      = request.form.get('currency_code', school.currency_code).strip()
        school.currency_symbol    = request.form.get('currency_symbol', school.currency_symbol).strip()
        school.timezone           = request.form.get('timezone', school.timezone).strip()
        school.locale             = request.form.get('locale', school.locale).strip()
        school.is_active          = bool(request.form.get('is_active'))
        school.governorate        = request.form.get('governorate', '').strip() or None
        school.price_per_student  = pps

        # Attendance time thresholds
        from datetime import time as _time
        def _parse_time(s):
            if not s or not s.strip():
                return None
            try:
                h, m = map(int, s.strip().split(':')[:2])
                return _time(h, m)
            except (ValueError, AttributeError):
                return None

        school.att_start_time        = _parse_time(request.form.get('att_start_time', ''))
        school.att_late_threshold    = _parse_time(request.form.get('att_late_threshold', ''))
        school.att_absence_threshold = _parse_time(request.form.get('att_absence_threshold', ''))

        # Logo upload
        import os
        from werkzeug.utils import secure_filename
        from flask import current_app
        logo_file = request.files.get('logo')
        if logo_file and logo_file.filename:
            import time as _t
            fname = secure_filename(
                f"school_{school.id}_logo_{int(_t.time())}_{logo_file.filename}"
            )
            uploads_dir = os.path.join(current_app.root_path, 'static', 'uploads')
            os.makedirs(uploads_dir, exist_ok=True)
            logo_file.save(os.path.join(uploads_dir, fname))
            school.logo_path = fname

        db.session.commit()
        log_action('edit', 'school', school.id,
                   details=f'updated school "{old_name}" → "{name}", capacity={new_capacity}')
        flash(f'تم تحديث بيانات المدرسة "{name}".', 'success')
        return redirect(url_for('schools.index'))

    return render_template('schools/form.html', school=school)


# ─────────────────────────────────────────────────────────────────────────────
#  DELETE SCHOOL
# ─────────────────────────────────────────────────────────────────────────────

@schools_bp.route('/<int:school_id>/delete', methods=['POST'])
@login_required
@super_admin_required
def delete(school_id):
    school = School.query.get_or_404(school_id)
    name = school.school_name
    linked_counts = linked_school_counts(school.id)

    if linked_counts and not is_demo_school(school):
        flash(
            f'لا يمكن حذف المدرسة "{name}" لأنها مرتبطة ببيانات: '
            f'{format_linked_counts(linked_counts)}. '
            'احذف أو انقل هذه البيانات أولاً. التنظيف التلقائي متاح فقط '
            'للمدارس التجريبية/الاختبارية المولّدة من النظام.',
            'danger'
        )
        return redirect(url_for('schools.index'))

    # Clear session if we deleted the active school
    if session.get('active_school_id') == school_id:
        session.pop('active_school_id', None)

    try:
        if linked_counts:
            deleted_counts = cleanup_school_cascade(school.id)
            db.session.commit()
            details = format_linked_counts(deleted_counts)
            log_action(
                'delete',
                'school',
                school_id,
                details=f'cascade-deleted demo/test school "{name}": {details}',
            )
            flash(
                f'تم تنظيف وحذف المدرسة التجريبية "{name}" وجميع بياناتها '
                f'المرتبطة بأمان. البيانات المحذوفة: {details}.',
                'success',
            )
        else:
            db.session.delete(school)
            db.session.commit()
            log_action('delete', 'school', school_id,
                       details=f'deleted school "{name}"')
            flash(f'تم حذف المدرسة "{name}".', 'success')
    except IntegrityError:
        db.session.rollback()
        refreshed_counts = linked_school_counts(school_id)
        details = format_linked_counts(refreshed_counts) or 'بيانات مرتبطة غير محددة'
        flash(
            f'تعذر حذف المدرسة "{name}" بسبب بيانات مرتبطة: {details}.',
            'danger',
        )
    except Exception as exc:
        db.session.rollback()
        flash(f'تعذر حذف المدرسة "{name}" أثناء عملية التنظيف: {exc}', 'danger')

    return redirect(url_for('schools.index'))


# ─────────────────────────────────────────────────────────────────────────────
#  ACADEMIC YEARS  (per school)
# ─────────────────────────────────────────────────────────────────────────────

@schools_bp.route('/<int:school_id>/years')
@login_required
@super_admin_required
def school_years(school_id):
    school = School.query.get_or_404(school_id)
    years  = AcademicYear.query.filter_by(school_id=school_id)\
                               .order_by(AcademicYear.start_date.desc()).all()
    return render_template('schools/years.html', school=school, years=years)


@schools_bp.route('/<int:school_id>/years/create', methods=['POST'])
@login_required
@super_admin_required
def create_year(school_id):
    school = School.query.get_or_404(school_id)
    name       = request.form.get('name', '').strip()
    start_date = request.form.get('start_date')
    end_date   = request.form.get('end_date')
    is_current = bool(request.form.get('is_current'))

    if not name or not start_date or not end_date:
        flash('جميع حقول العام الدراسي مطلوبة.', 'danger')
        return redirect(url_for('schools.school_years', school_id=school_id))

    existing = (AcademicYear.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=school_id, name=name)
                .first())
    if existing:
        flash(f'يوجد عام دراسي بنفس الاسم "{name}" مسجّل لهذه المدرسة بالفعل.', 'danger')
        return redirect(url_for('schools.school_years', school_id=school_id))

    if is_current:
        AcademicYear.query.filter_by(school_id=school_id).update({'is_current': False})

    try:
        ay = AcademicYear(
            school_id  = school_id,
            name       = name,
            start_date = dt.strptime(start_date, '%Y-%m-%d').date(),
            end_date   = dt.strptime(end_date,   '%Y-%m-%d').date(),
            is_current = is_current,
        )
        db.session.add(ay)
        db.session.commit()
        log_action('create', 'academic_year', ay.id,
                   details=f'school={school_id}, year="{name}"')
        flash(f'تم إنشاء العام الدراسي "{name}".', 'success')
    except ValueError:
        flash('تنسيق التاريخ غير صحيح.', 'danger')

    return redirect(url_for('schools.school_years', school_id=school_id))


@schools_bp.route('/<int:school_id>/years/<int:year_id>/activate', methods=['POST'])
@login_required
@super_admin_required
def activate_year(school_id, year_id):
    year = AcademicYear.query.filter_by(id=year_id, school_id=school_id).first_or_404()
    AcademicYear.query.filter_by(school_id=school_id).update({'is_current': False})
    year.is_current = True
    db.session.commit()
    log_action('edit', 'academic_year', year_id,
               details=f'activated year "{year.name}" for school {school_id}')
    flash(f'تم تفعيل العام الدراسي "{year.name}" كالعام الحالي.', 'success')
    return redirect(url_for('schools.school_years', school_id=school_id))


@schools_bp.route('/<int:school_id>/years/<int:year_id>/delete', methods=['POST'])
@login_required
@super_admin_required
def delete_year(school_id, year_id):
    year = AcademicYear.query.filter_by(id=year_id, school_id=school_id).first_or_404()
    if year.is_current:
        flash('لا يمكن حذف العام الدراسي الحالي.', 'danger')
        return redirect(url_for('schools.school_years', school_id=school_id))

    student_count = Student.query.filter_by(academic_year_id=year_id).count()
    if student_count > 0:
        flash(
            f'لا يمكن حذف هذا العام — مرتبط بـ {student_count} طالب.',
            'danger'
        )
        return redirect(url_for('schools.school_years', school_id=school_id))

    name = year.name
    db.session.delete(year)
    db.session.commit()
    log_action('delete', 'academic_year', year_id, details=f'deleted year "{name}"')
    flash(f'تم حذف العام الدراسي "{name}".', 'success')
    return redirect(url_for('schools.school_years', school_id=school_id))


# ─────────────────────────────────────────────────────────────────────────────
#  CAPACITY STATUS API  (JSON — used by the global overview page)
# ─────────────────────────────────────────────────────────────────────────────

@schools_bp.route('/<int:school_id>/capacity-status')
@login_required
@super_admin_required
def capacity_status(school_id):
    school = School.query.get_or_404(school_id)
    count  = Student.query.execution_options(
        bypass_tenant_scope=True, include_all_years=True
    ).filter_by(school_id=school_id, status='active').count()
    return jsonify({
        'school_id':    school_id,
        'capacity':     school.capacity,
        'current':      count,
        'available':    max(0, (school.capacity or 0) - count) if school.capacity else None,
        'at_capacity':  bool(school.capacity and count >= school.capacity),
    })

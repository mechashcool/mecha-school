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
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from datetime import datetime as dt

from app.models import (db, School, AcademicYear, Student, Employee,
                         User, SchoolStudentFormConfig, SchoolModuleConfig, FeaturePackage)
from app.utils.decorators import super_admin_required
from app.utils.modules import MODULES, PRESETS, get_enabled_modules, save_school_modules
from app.utils.features import FEATURES, FEATURE_PRESETS, get_enabled_features, save_school_features
from app.utils.packages import (
    get_default_package_config, build_config_from_form,
    apply_package_to_school, config_summary,
)
from app.utils.school_config import MODULE_DEFS, CONFIGURABLE_MODULES, save_module_config
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

    # Reuse the super_admin blueprint's helper so investor lookup logic is not
    # duplicated. Local import avoids any blueprint import-order/circular issues.
    from app.blueprints.super_admin import _get_school_investor
    investor = _get_school_investor(school.id)

    return render_template('schools/detail.html',
                           school=school,
                           current_year=current_year,
                           users=users,
                           manager=manager,
                           investor=investor,
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

def _active_packages():
    """Active feature packages for the school-create package selector."""
    return (FeaturePackage.query
            .filter_by(is_active=True)
            .order_by(FeaturePackage.name)
            .all())


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

        # Validate the selected package (if any) server-side before doing any work.
        selected_package = None
        pkg_id = request.form.get('package_id', type=int)
        if pkg_id:
            selected_package = FeaturePackage.query.filter_by(
                id=pkg_id, is_active=True).first()
            if selected_package is None:
                errors.append('الباقة المحددة غير موجودة أو غير مفعّلة.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('schools/form.html', school=None,
                                   modules=MODULES, presets=PRESETS,
                                   packages=_active_packages(),
                                   selected_package_id=pkg_id,
                                   selected_modules=set(request.form.getlist('modules')))

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
            new_ay = None
            if year_name:
                start_raw = request.form.get('year_start')
                end_raw = request.form.get('year_end')
                if start_raw and end_raw:
                    new_ay = AcademicYear(
                        school_id=school.id,
                        name=year_name,
                        start_date=dt.strptime(start_raw, '%Y-%m-%d').date(),
                        end_date=dt.strptime(end_raw, '%Y-%m-%d').date(),
                        is_current=True,
                    )
                    db.session.add(new_ay)
                    db.session.flush()  # assign new_ay.id before grade creation

            # Auto-create standard Iraqi grades and subjects when a year is created with the school.
            if new_ay:
                from app.utils.iraqi_grades import ensure_iraqi_standard_grades
                from app.utils.iraqi_subjects import ensure_standard_subjects
                ensure_iraqi_standard_grades(school.id, new_ay.id)
                db.session.flush()  # assign grade IDs so subjects can reference them
                ensure_standard_subjects(school.id, new_ay.id)

            # Apply configuration. A selected package takes precedence and is
            # applied atomically (modules + features + student form + module
            # configs) so the new school is never left partially configured.
            # When no package is selected, fall back to the manual module
            # checkboxes (empty = "no modules configured yet" → treated as
            # all-enabled by the app, preserving the original behavior).
            if selected_package is not None:
                school.package_id = selected_package.id
                apply_package_to_school(school.id, selected_package)
            else:
                enabled_keys = request.form.getlist('modules')
                if enabled_keys:
                    save_school_modules(school.id, enabled_keys)

            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            flash(f'Could not create school: {exc}', 'danger')
            return render_template('schools/form.html', school=None,
                                   modules=MODULES, presets=PRESETS,
                                   packages=_active_packages(),
                                   selected_package_id=pkg_id,
                                   selected_modules=set(request.form.getlist('modules')))

        if selected_package is not None:
            log_action('create', 'school', school.id,
                       details=(f'created school "{name}" with package '
                                f'"{selected_package.name}" (id={selected_package.id}) applied'))
            flash(f'تم إنشاء المدرسة "{name}" وتطبيق الباقة "{selected_package.name}" بنجاح. '
                  'يمكنك الآن تعديل الوحدات والميزات يدوياً.', 'success')
            return redirect(url_for('schools.school_features', school_id=school.id, tab=1))

        log_action('create', 'school', school.id, details=f'created school "{name}"')
        flash(f'تم إنشاء المدرسة "{name}" بنجاح. يمكنك الآن ضبط الوحدات والميزات التفصيلية.', 'success')
        return redirect(url_for('schools.school_modules', school_id=school.id))

    return render_template('schools/form.html', school=None,
                           modules=MODULES, presets=PRESETS,
                           packages=_active_packages(),
                           selected_package_id=None,
                           selected_modules=set())


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
        school.enable_buildings   = bool(request.form.get('enable_buildings'))
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

        # School code — unique across all schools
        import re as _re
        raw_code = request.form.get('code', '').strip().upper()
        new_code = _re.sub(r'[^A-Z0-9]', '', raw_code)[:20] or None
        if new_code and new_code != (school.code or '').upper():
            if School.query.filter(School.code == new_code, School.id != school.id).first():
                flash('رمز المدرسة مستخدم مسبقاً من مدرسة أخرى. اختر رمزاً مختلفاً.', 'danger')
                return render_template('schools/form.html', school=school)
        school.code = new_code

        # Logo upload — stored in Supabase Storage (school-media bucket) in production,
        # or local static/uploads/ in development.
        from app.utils.helpers import (save_uploaded_file, LOGO_IMAGE_EXTENSIONS,
                                        LOGO_MAX_BYTES, identity_upload_bucket)
        logo_file = request.files.get('logo')
        if logo_file and logo_file.filename:
            result = save_uploaded_file(
                logo_file,
                subfolder=f'schools/{school.id}/identity',
                prefix='logo',
                bucket=identity_upload_bucket(),
                allowed_exts=LOGO_IMAGE_EXTENSIONS,
                max_size=LOGO_MAX_BYTES,
            )
            if result:
                school.logo_path = result
            else:
                flash('فشل رفع الشعار. تأكد من أن الملف صورة صالحة (PNG/JPG/WEBP/SVG) ولا يتجاوز 2 ميغابايت.', 'warning')

        db.session.commit()
        # P2: name/logo/identity may have changed — drop the cached branding
        # block served by the mobile /me endpoint for this school.
        from app.utils.context_cache import invalidate_school_context
        invalidate_school_context(school.id)
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
    # P2: active year changed — drop this school's cached context immediately.
    from app.utils.context_cache import invalidate_school_context
    invalidate_school_context(school_id)
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
#  SCHOOL MODULES  (feature flags per school — super admin only)
# ─────────────────────────────────────────────────────────────────────────────

@schools_bp.route('/<int:school_id>/modules', methods=['GET', 'POST'])
@login_required
@super_admin_required
def school_modules(school_id):
    """View and edit which modules and sub-features are enabled for a school."""
    school = School.query.get_or_404(school_id)

    if request.method == 'POST':
        enabled_module_keys  = request.form.getlist('modules')
        enabled_feature_keys = request.form.getlist('features')
        save_school_modules(school.id, enabled_module_keys)
        save_school_features(school.id, enabled_feature_keys)
        db.session.commit()
        log_action('edit', 'school', school.id,
                   details=(f'updated modules+features for "{school.school_name}": '
                             f'modules={enabled_module_keys}'))
        flash('تم حفظ إعدادات الوحدات والميزات بنجاح.', 'success')
        return redirect(url_for('schools.school_modules', school_id=school_id))

    school_enabled_modules  = get_enabled_modules(school.id)
    school_enabled_features = get_enabled_features(school.id)
    return render_template('schools/modules.html',
                           school=school,
                           modules=MODULES,
                           presets=PRESETS,
                           features=FEATURES,
                           feature_presets=FEATURE_PRESETS,
                           school_enabled_modules=school_enabled_modules,
                           school_enabled_features=school_enabled_features)


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


# ─────────────────────────────────────────────────────────────────────────────
#  STUDENT FORM SETTINGS  (per-school field visibility / required — super admin)
# ─────────────────────────────────────────────────────────────────────────────

@schools_bp.route('/<int:school_id>/student-form-settings', methods=['GET', 'POST'])
@login_required
@super_admin_required
def student_form_settings(school_id):
    from app.utils.student_form_config import (
        ALL_SECTIONS, ALL_FIELDS, SECTION_LABELS, FIELD_LABELS
    )

    school = School.query.get_or_404(school_id)
    config = SchoolStudentFormConfig.query.filter_by(school_id=school_id).first()

    if request.method == 'POST':
        # Collect checkbox values from form
        hidden_sections = [s for s in ALL_SECTIONS
                           if not request.form.get(f'section_{s}')]
        hidden_fields   = [f for f in ALL_FIELDS
                           if not request.form.get(f'field_{f}')]
        required_fields = [f for f in ALL_FIELDS
                           if request.form.get(f'required_{f}')
                           and f not in hidden_fields]

        if config is None:
            config = SchoolStudentFormConfig(school_id=school_id)
            db.session.add(config)

        config.hidden_sections = hidden_sections or None
        config.hidden_fields   = hidden_fields   or None
        config.required_fields = required_fields or None
        config.updated_at      = dt.utcnow()
        db.session.commit()
        log_action('edit', 'school_student_form_config', school_id,
                   details='student form settings updated')
        flash('تم حفظ إعدادات نموذج الطالب بنجاح.', 'success')
        return redirect(url_for('schools.student_form_settings', school_id=school_id))

    # Build current state for the template
    hidden_sections = set(config.hidden_sections or []) if config else set()
    hidden_fields   = set(config.hidden_fields   or []) if config else set()
    required_fields = set(config.required_fields or []) if config else set()

    return render_template(
        'schools/student_form_settings.html',
        school=school,
        all_sections=ALL_SECTIONS,
        all_fields=ALL_FIELDS,
        section_labels=SECTION_LABELS,
        field_labels=FIELD_LABELS,
        hidden_sections=hidden_sections,
        hidden_fields=hidden_fields,
        required_fields=required_fields,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  CENTRAL SCHOOL FEATURE MANAGEMENT  (searchable/filterable school selector)
# ─────────────────────────────────────────────────────────────────────────────

@schools_bp.route('/features-overview')
@login_required
@super_admin_required
def features_overview():
    """Central super-admin page: pick a school (search + filters) then manage
    its modules/features via the existing per-school features page."""
    from app.models import SchoolModule, SchoolFeature

    search = request.args.get('q', '').strip()
    gov    = request.args.get('governorate', '').strip()
    pkg_id = request.args.get('package_id', type=int)
    status = request.args.get('status', '').strip()
    page   = request.args.get('page', 1, type=int)

    q = School.query.execution_options(bypass_tenant_scope=True)
    if search:
        like = f'%{search}%'
        q = q.filter(or_(School.school_name.ilike(like),
                         School.school_name_ar.ilike(like),
                         School.code.ilike(like)))
    if gov:
        if gov == 'غير محدد':
            q = q.filter(School.governorate.is_(None))
        else:
            q = q.filter(School.governorate == gov)
    if pkg_id:
        q = q.filter(School.package_id == pkg_id)
    if status == 'active':
        q = q.filter(School.is_active.is_(True))
    elif status == 'inactive':
        q = q.filter(School.is_active.is_(False))

    q = q.order_by(School.governorate.nullslast(), School.school_name)
    pagination = q.paginate(page=page, per_page=20, error_out=False)
    schools = pagination.items
    school_ids = [s.id for s in schools]

    total_modules  = len(MODULES)
    total_features = len(FEATURES)

    # Grouped counts for the schools on this page only (avoids N+1).
    mod_total, mod_enabled, feat_total, feat_enabled = {}, {}, {}, {}
    if school_ids:
        for sid, cnt in (db.session.query(SchoolModule.school_id, func.count(SchoolModule.id))
                         .filter(SchoolModule.school_id.in_(school_ids))
                         .group_by(SchoolModule.school_id).all()):
            mod_total[sid] = int(cnt or 0)
        for sid, cnt in (db.session.query(SchoolModule.school_id, func.count(SchoolModule.id))
                         .filter(SchoolModule.school_id.in_(school_ids),
                                 SchoolModule.is_enabled.is_(True))
                         .group_by(SchoolModule.school_id).all()):
            mod_enabled[sid] = int(cnt or 0)
        for sid, cnt in (db.session.query(SchoolFeature.school_id, func.count(SchoolFeature.id))
                         .filter(SchoolFeature.school_id.in_(school_ids))
                         .group_by(SchoolFeature.school_id).all()):
            feat_total[sid] = int(cnt or 0)
        for sid, cnt in (db.session.query(SchoolFeature.school_id, func.count(SchoolFeature.id))
                         .filter(SchoolFeature.school_id.in_(school_ids),
                                 SchoolFeature.is_enabled.is_(True))
                         .group_by(SchoolFeature.school_id).all()):
            feat_enabled[sid] = int(cnt or 0)

    school_rows = []
    for s in schools:
        # No config rows = backward-compat "all enabled".
        school_rows.append({
            'school':           s,
            'modules_enabled':  mod_enabled.get(s.id, 0) if mod_total.get(s.id) else total_modules,
            'modules_total':    total_modules,
            'features_enabled': feat_enabled.get(s.id, 0) if feat_total.get(s.id) else total_features,
            'features_total':   total_features,
        })

    gov_rows = (db.session.query(School.governorate)
                .filter(School.governorate.isnot(None))
                .distinct().order_by(School.governorate).all())
    governorates = [r[0] for r in gov_rows]
    packages = FeaturePackage.query.order_by(FeaturePackage.name).all()

    return render_template('schools/features_overview.html',
                           school_rows=school_rows,
                           pagination=pagination,
                           governorates=governorates,
                           packages=packages,
                           q=search, gov=gov,
                           pkg_id=pkg_id, status=status)


# ─────────────────────────────────────────────────────────────────────────────
#  UNIFIED SCHOOL FEATURES PAGE  (modules + features + student form + package)
# ─────────────────────────────────────────────────────────────────────────────

@schools_bp.route('/<int:school_id>/features', methods=['GET', 'POST'])
@login_required
@super_admin_required
def school_features(school_id):
    """Unified per-school configuration page (tabs: package / modules+features / student form)."""
    from app.utils.student_form_config import (
        ALL_SECTIONS, ALL_FIELDS, SECTION_LABELS, FIELD_LABELS
    )

    school = School.query.get_or_404(school_id)
    config_row = SchoolStudentFormConfig.query.filter_by(school_id=school_id).first()

    if request.method == 'POST':
        action = request.form.get('action', '')

        # ── Save modules + features ───────────────────────────────────────────
        if action == 'save_modules':
            enabled_module_keys  = request.form.getlist('modules')
            enabled_feature_keys = request.form.getlist('features')
            save_school_modules(school.id, enabled_module_keys)
            save_school_features(school.id, enabled_feature_keys)
            db.session.commit()
            log_action('edit', 'school', school.id,
                       details=f'updated modules+features: modules={enabled_module_keys}')
            flash('تم حفظ إعدادات الوحدات والميزات بنجاح.', 'success')
            return redirect(url_for('schools.school_features', school_id=school_id, tab=1))

        # ── Save student form config ───────────────────────────────────────────
        elif action == 'save_student_form':
            hidden_sections = [s for s in ALL_SECTIONS
                               if not request.form.get(f'section_{s}')]
            hidden_fields   = [f for f in ALL_FIELDS
                               if not request.form.get(f'field_{f}')]
            required_fields = [f for f in ALL_FIELDS
                               if request.form.get(f'required_{f}')
                               and f not in hidden_fields]

            if config_row is None:
                config_row = SchoolStudentFormConfig(school_id=school_id)
                db.session.add(config_row)

            config_row.hidden_sections = hidden_sections or None
            config_row.hidden_fields   = hidden_fields   or None
            config_row.required_fields = required_fields or None
            config_row.updated_at      = dt.utcnow()
            db.session.commit()
            log_action('edit', 'school_student_form_config', school_id,
                       details='student form settings updated via unified features page')
            flash('تم حفظ إعدادات نموذج الطالب بنجاح.', 'success')
            return redirect(url_for('schools.school_features', school_id=school_id, tab=2))

        # ── Apply package to school ────────────────────────────────────────────
        elif action == 'apply_package':
            pkg_id = request.form.get('package_id', type=int)
            pkg = FeaturePackage.query.get(pkg_id) if pkg_id else None
            if not pkg:
                flash('الباقة المحددة غير موجودة.', 'danger')
                return redirect(url_for('schools.school_features', school_id=school_id, tab=0))

            apply_package_to_school(school.id, pkg)
            school.package_id = pkg.id
            db.session.commit()
            log_action('edit', 'school', school.id,
                       details=f'applied feature package "{pkg.name}" (id={pkg.id})')
            flash(f'تم تطبيق الباقة "{pkg.name}" على المدرسة. الإعدادات نُسخت بنجاح.', 'success')
            return redirect(url_for('schools.school_features', school_id=school_id, tab=0))

        # ── Remove package reference ───────────────────────────────────────────
        elif action == 'remove_package':
            old_name = school.package.name if school.package else ''
            school.package_id = None
            db.session.commit()
            log_action('edit', 'school', school.id,
                       details=f'removed package reference "{old_name}"')
            flash('تمت إزالة ربط الباقة. إعدادات المدرسة الحالية لم تتغير.', 'info')
            return redirect(url_for('schools.school_features', school_id=school_id, tab=0))

        # ── Save module config (employees / employee_attendance / devices …) ───
        elif action == 'save_module_config':
            module_key = request.form.get('module_key', '')
            if module_key not in MODULE_DEFS:
                flash('وحدة غير معروفة.', 'danger')
                return redirect(url_for('schools.school_features', school_id=school_id, tab=3))

            mdef = MODULE_DEFS[module_key]
            hidden_sec = [s for s in mdef.get('sections', {})
                          if not request.form.get(f'section_{s}')]
            hidden_fld = [f for f in mdef.get('fields', {})
                          if not request.form.get(f'field_{f}')]
            req_fld    = [f for f in mdef.get('fields', {})
                          if request.form.get(f'required_{f}') and f not in hidden_fld]
            dis_act    = [a for a in mdef.get('actions', {})
                          if not request.form.get(f'action_{a}')]

            cfg = {
                'hidden_sections':  hidden_sec,
                'hidden_fields':    hidden_fld,
                'required_fields':  req_fld,
                'disabled_actions': dis_act,
            }
            save_module_config(school.id, module_key, cfg)
            db.session.commit()
            log_action('edit', 'school', school.id,
                       details=f'updated module config: {module_key}')
            flash(f'تم حفظ إعدادات وحدة "{mdef["label"]}" بنجاح.', 'success')

            # redirect to the right tab (3-indexed based on module order)
            tab_map = {mk: 3 + i for i, mk in enumerate(MODULE_DEFS)}
            return redirect(url_for('schools.school_features',
                                    school_id=school_id, tab=tab_map.get(module_key, 3)))

    # ── GET ───────────────────────────────────────────────────────────────────
    active_tab = request.args.get('tab', 0, type=int)

    packages = (FeaturePackage.query
                .filter_by(is_active=True)
                .order_by(FeaturePackage.name)
                .all())

    school_enabled_modules  = get_enabled_modules(school.id)
    school_enabled_features = get_enabled_features(school.id)

    hidden_sections = set(config_row.hidden_sections or []) if config_row else set()
    hidden_fields   = set(config_row.hidden_fields   or []) if config_row else set()
    required_fields = set(config_row.required_fields or []) if config_row else set()

    # Load per-module configs for new modules
    existing_module_configs: dict[str, dict] = {}
    for mk in MODULE_DEFS:
        row = (SchoolModuleConfig.query
               .filter_by(school_id=school.id, module_key=mk)
               .first())
        existing_module_configs[mk] = row.config if row else {}

    return render_template(
        'schools/features.html',
        school=school,
        active_tab=active_tab,
        # Package tab
        packages=packages,
        # Modules/features tab
        modules=MODULES,
        presets=PRESETS,
        features=FEATURES,
        feature_presets=FEATURE_PRESETS,
        school_enabled_modules=school_enabled_modules,
        school_enabled_features=school_enabled_features,
        # Student form tab
        all_sections=ALL_SECTIONS,
        all_fields=ALL_FIELDS,
        section_labels=SECTION_LABELS,
        field_labels=FIELD_LABELS,
        hidden_sections=hidden_sections,
        hidden_fields=hidden_fields,
        required_fields=required_fields,
        # Module configs (employees, employee_attendance, etc.)
        module_defs=MODULE_DEFS,
        existing_module_configs=existing_module_configs,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  REDIRECT SHIMS  (keep old URLs working)
# ─────────────────────────────────────────────────────────────────────────────

# school_modules GET now redirects to the unified features page (tab 1)
# The existing school_modules POST still works for any form that targets it directly.
# We override only the GET by wrapping the function after its definition.
# (The route is already registered above — we add a dedicated redirect route here.)

@schools_bp.route('/<int:school_id>/modules/redirect')
@login_required
@super_admin_required
def school_modules_redirect(school_id):
    return redirect(url_for('schools.school_features', school_id=school_id, tab=1))


@schools_bp.route('/<int:school_id>/student-form-settings/redirect')
@login_required
@super_admin_required
def student_form_settings_redirect(school_id):
    return redirect(url_for('schools.school_features', school_id=school_id, tab=2))


# ─────────────────────────────────────────────────────────────────────────────
#  FEATURE PACKAGES CRUD  (super admin only — global, not per-school)
# ─────────────────────────────────────────────────────────────────────────────

@schools_bp.route('/packages')
@login_required
@super_admin_required
def packages_list():
    """List all feature packages."""
    packages = (FeaturePackage.query
                .order_by(FeaturePackage.is_active.desc(), FeaturePackage.name)
                .all())
    return render_template('schools/packages_list.html', packages=packages)


@schools_bp.route('/packages/create', methods=['GET', 'POST'])
@login_required
@super_admin_required
def create_package():
    """Create a new feature package."""
    from app.utils.student_form_config import (
        ALL_SECTIONS as _SF_SECTIONS, ALL_FIELDS as _SF_FIELDS,
        SECTION_LABELS as _SF_SEC_LABELS, FIELD_LABELS as _SF_FLD_LABELS,
    )

    def _re_render(cfg):
        return render_template('schools/package_form.html',
                               package=None,
                               modules=MODULES, features=FEATURES,
                               all_sections=_SF_SECTIONS,
                               all_fields=_SF_FIELDS,
                               section_labels=_SF_SEC_LABELS,
                               field_labels=_SF_FLD_LABELS,
                               module_defs=MODULE_DEFS,
                               config=cfg)

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('اسم الباقة مطلوب.', 'danger')
            return _re_render(build_config_from_form(request.form))

        config = build_config_from_form(request.form)
        pkg = FeaturePackage(
            name        = name,
            description = request.form.get('description', '').strip() or None,
            is_active   = bool(request.form.get('is_active', '1')),
            config      = config,
        )
        db.session.add(pkg)
        db.session.commit()
        log_action('create', 'feature_package', pkg.id,
                   details=f'created feature package "{name}"')
        flash(f'تم إنشاء الباقة "{name}" بنجاح.', 'success')
        return redirect(url_for('schools.packages_list'))

    return _re_render(get_default_package_config())


@schools_bp.route('/packages/<int:pkg_id>/edit', methods=['GET', 'POST'])
@login_required
@super_admin_required
def edit_package(pkg_id):
    """Edit an existing feature package."""
    from app.utils.student_form_config import (
        ALL_SECTIONS as _SF_SECTIONS, ALL_FIELDS as _SF_FIELDS,
        SECTION_LABELS as _SF_SEC_LABELS, FIELD_LABELS as _SF_FLD_LABELS,
    )

    pkg = FeaturePackage.query.get_or_404(pkg_id)

    def _re_render(cfg):
        return render_template('schools/package_form.html',
                               package=pkg,
                               modules=MODULES, features=FEATURES,
                               all_sections=_SF_SECTIONS,
                               all_fields=_SF_FIELDS,
                               section_labels=_SF_SEC_LABELS,
                               field_labels=_SF_FLD_LABELS,
                               module_defs=MODULE_DEFS,
                               config=cfg)

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('اسم الباقة مطلوب.', 'danger')
            return _re_render(build_config_from_form(request.form))

        old_name = pkg.name
        pkg.name        = name
        pkg.description = request.form.get('description', '').strip() or None
        pkg.is_active   = bool(request.form.get('is_active'))
        pkg.config      = build_config_from_form(request.form)
        pkg.updated_at  = dt.utcnow()
        db.session.commit()
        log_action('edit', 'feature_package', pkg.id,
                   details=f'updated feature package "{old_name}" → "{name}"')
        flash(f'تم تحديث الباقة "{name}" بنجاح.', 'success')
        return redirect(url_for('schools.packages_list'))

    return _re_render(pkg.config or get_default_package_config())


@schools_bp.route('/packages/<int:pkg_id>/delete', methods=['POST'])
@login_required
@super_admin_required
def delete_package(pkg_id):
    """Delete a feature package (sets school.package_id to NULL via FK cascade)."""
    pkg = FeaturePackage.query.get_or_404(pkg_id)
    name = pkg.name
    # Null-out school references first to avoid FK errors on non-CASCADE DBs
    School.query.filter_by(package_id=pkg.id).update({'package_id': None})
    db.session.delete(pkg)
    db.session.commit()
    log_action('delete', 'feature_package', pkg_id,
               details=f'deleted feature package "{name}"')
    flash(f'تم حذف الباقة "{name}".', 'success')
    return redirect(url_for('schools.packages_list'))

"""
Mecha-School — Permission decorators and school-scoping helpers
"""
from functools import wraps
from flask import abort, flash, redirect, request, url_for, session
from flask_login import current_user


# ─────────────────────────────────────────────────────────────────────────────
#  PERMISSION DECORATORS
# ─────────────────────────────────────────────────────────────────────────────

def permission_required(perm_name):
    """Abort 403 unless current user has the given permission."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('auth.login'))
            if not current_user.has_permission(perm_name):
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return decorator


def staff_required(f):
    """
    Allow any authenticated staff (all roles except parent/teacher).
    Parents and teachers are redirected to their own portals.
    """
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login'))
        role_name = current_user.role.name if current_user.role else ''
        if role_name == 'teacher':
            return redirect(url_for('teacher.dashboard'))
        if role_name == 'parent':
            return redirect(url_for('parent.dashboard'))
        if role_name == 'investor_viewer':
            # Read-only investor accounts have their own portal; they must never
            # reach the shared staff dashboard or any other staff route.
            return redirect(url_for('investor.dashboard'))
        return f(*args, **kwargs)
    return wrapped


def admin_required(f):
    """
    Allow explicit admin tiers: super_admin and school_admin.
    Non-admin staff are redirected to the shared admin dashboard.
    """
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login'))
        if current_user.is_super_admin or current_user.is_school_admin:
            return f(*args, **kwargs)
        flash('ليس لديك صلاحية الوصول إلى هذه الصفحة.', 'danger')
        if current_user.role and current_user.role.name == 'teacher':
            return redirect(url_for('teacher.dashboard'))
        if current_user.role and current_user.role.name == 'parent':
            return redirect(url_for('parent.dashboard'))
        if current_user.role and current_user.role.name == 'investor_viewer':
            return redirect(url_for('investor.dashboard'))
        return redirect(url_for('admin.dashboard'))
    return wrapped


def super_admin_required(f):
    """
    Only the system-level super admin (role.name == super_admin).
    School-bound admins are redirected to admin.dashboard.
    """
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login'))
        if current_user.is_super_admin:
            return f(*args, **kwargs)
        flash('هذه الصفحة مخصصة لمسؤول النظام فقط.', 'danger')
        if current_user.role and current_user.role.name == 'teacher':
            return redirect(url_for('teacher.dashboard'))
        if current_user.role and current_user.role.name == 'parent':
            return redirect(url_for('parent.dashboard'))
        if current_user.role and current_user.role.name == 'investor_viewer':
            return redirect(url_for('investor.dashboard'))
        return redirect(url_for('admin.dashboard'))
    return wrapped


def historical_guard(f):
    """Block write (non-GET) requests when the user is viewing a historical academic year.
    Apply to every route that mutates data so historical years stay read-only."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        if request.method != 'GET':
            from app.utils.scoping import is_historical_view
            if is_historical_view():
                flash(
                    'لا يمكن تعديل البيانات أثناء عرض سنة دراسية مؤرشفة. '
                    'يمكنك العرض والطباعة والتصدير فقط.',
                    'danger',
                )
                return redirect(request.referrer or url_for('admin.dashboard'))
        return f(*args, **kwargs)
    return wrapped


def any_permission_required(*perm_names):
    """Abort 403 unless user has at least one of the given permissions."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('auth.login'))
            if not any(current_user.has_permission(p) for p in perm_names):
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return decorator


def _wants_json():
    """True when the caller expects a JSON response (AJAX / JSON body / JSON Accept)."""
    if request.is_json:
        return True
    if 'application/json' in request.headers.get('Accept', ''):
        return True
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return True
    return False


def action_required(module_key, action_key):
    """
    Block the route if the given action is disabled for the current user's school.

    - Super admin always passes through.
    - School staff with disabled action:
        • AJAX/JSON requests  → JSON  {'ok': False, 'error': '…'} 403
        • HTML requests       → flash Arabic message + redirect to referrer
    - Fail-open: no config row → action considered enabled.
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('auth.login'))
            if not getattr(current_user, 'is_super_admin', False):
                school_id = getattr(current_user, 'school_id', None)
                from app.utils.school_config import get_school_config
                cfg = get_school_config(school_id)
                if not cfg.action_enabled(module_key, action_key):
                    if _wants_json():
                        from flask import jsonify
                        return jsonify({'ok': False,
                                        'error': 'هذه الميزة غير مفعلة لهذه المدرسة'}), 403
                    flash('هذه الميزة غير مفعلة لهذه المدرسة.', 'warning')
                    return redirect(request.referrer or url_for('admin.dashboard'))
            return f(*args, **kwargs)
        return wrapped
    return decorator


def module_required(module_key):
    """
    Block the route if the given module is disabled for the current user's school.

    Use this when a route belongs to a blueprint that also serves other modules
    (e.g. subjects inside the sections blueprint) so the global BLUEPRINT_MODULE
    guard cannot be used.

    - Super admin always passes through.
    - Fail-open: no SchoolModule row → module considered enabled.
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('auth.login'))
            if not getattr(current_user, 'is_super_admin', False):
                school_id = getattr(current_user, 'school_id', None)
                from app.utils.modules import is_module_enabled
                if not is_module_enabled(school_id, module_key):
                    if _wants_json():
                        from flask import jsonify
                        return jsonify({'ok': False,
                                        'error': 'هذه الميزة غير مفعلة لهذه المدرسة'}), 403
                    flash('هذه الميزة غير مفعلة لهذه المدرسة.', 'warning')
                    return redirect(request.referrer or url_for('admin.dashboard'))
            return f(*args, **kwargs)
        return wrapped
    return decorator


def section_required(module_key, section_key):
    """
    Block the route if the given section is hidden for the current user's school.

    Same AJAX/HTML split as action_required.
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('auth.login'))
            if not getattr(current_user, 'is_super_admin', False):
                school_id = getattr(current_user, 'school_id', None)
                from app.utils.school_config import get_school_config
                cfg = get_school_config(school_id)
                if not cfg.section_visible(module_key, section_key):
                    if _wants_json():
                        from flask import jsonify
                        return jsonify({'ok': False,
                                        'error': 'هذا القسم غير مفعل لهذه المدرسة'}), 403
                    flash('هذا القسم غير مفعل لهذه المدرسة.', 'warning')
                    return redirect(request.referrer or url_for('admin.dashboard'))
            return f(*args, **kwargs)
        return wrapped
    return decorator


# ─────────────────────────────────────────────────────────────────────────────
#  SCHOOL-SCOPING HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_current_school():
    """
    Return the School object that should scope the current request.

    Logic:
      - Super admin (role.name == 'super_admin'): use session['active_school_id'] if set,
        otherwise return None so global views stay truly global.
      - All other users: return their own school (current_user.school).

    Returns None for global super-admin views or fresh installs.
    Memoized per request; the key embeds the super-admin school selector (or
    the user's own school id) so a different selection is never served a
    cached value from another one.
    """
    from app.models import School
    from app.utils.request_cache import request_memo

    if not current_user.is_authenticated:
        return None

    if current_user.is_super_admin:
        # Super admin — may have switched to a specific school
        active_id = session.get('active_school_id')

        def _load_super():
            if active_id:
                school = School.query.get(active_id)
                if school:
                    return school
            # No active switch means "global super-admin", not the first school.
            return None

        return request_memo(('current_school', 'super', active_id), _load_super)

    # Regular user — always their own school
    return request_memo(('current_school', 'user', current_user.school_id),
                        lambda: current_user.school)


def get_active_year(school_id):
    """
    Return the current AcademicYear for the given school, or None.
    Always returns the year with is_current=True — use for write operations.
    Memoized per request, keyed by school_id.
    """
    from app.models import AcademicYear
    from app.utils.request_cache import request_memo

    def _load():
        return AcademicYear.query.execution_options(bypass_tenant_scope=True)\
            .filter_by(school_id=school_id, is_current=True).first()

    return request_memo(('active_year', school_id), _load)


def get_view_year(school_id):
    """
    Return the academic year currently being VIEWED for the given school.
    If the user has selected a historical year in the session, returns that year.
    Falls back to the current active year (same as get_active_year).
    Use this for read/display queries; use get_active_year for write operations.
    Memoized per request, keyed by (school_id, view_year_id) so historical-year
    switching always resolves against the year selected for THIS request.
    """
    from flask import g, has_request_context
    from app.models import AcademicYear
    from app.utils.request_cache import request_memo

    if has_request_context():
        view_yid = getattr(g, 'tenant_scope_view_year_id', None)
        if view_yid:
            def _load():
                return AcademicYear.query\
                    .execution_options(bypass_tenant_scope=True)\
                    .filter_by(id=view_yid, school_id=school_id).first()

            year = request_memo(('view_year', school_id, view_yid), _load)
            if year:
                return year
    return get_active_year(school_id)


def get_teacher_section_ids(user):
    """
    Return the set of Section IDs a teacher is authorised to access.
    Combines homeroom sections and subject-teaching sections.
    """
    from app.models import db, Employee, Section, teacher_subjects as ts_table
    emp = Employee.query.filter_by(user_id=user.id).first()
    if not emp:
        return set()
    subject_section_ids = {
        row[0] for row in
        db.session.query(ts_table.c.section_id)
                  .filter(ts_table.c.employee_id == emp.id)
                  .all()
    }
    homeroom_ids = {s.id for s in Section.query.filter_by(teacher_id=emp.id).all()}
    return subject_section_ids | homeroom_ids

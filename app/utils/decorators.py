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
    """
    from app.models import School

    if not current_user.is_authenticated:
        return None

    if current_user.is_super_admin:
        # Super admin — may have switched to a specific school
        active_id = session.get('active_school_id')
        if active_id:
            school = School.query.get(active_id)
            if school:
                return school
        # No active switch means "global super-admin", not the first school.
        return None

    # Regular user — always their own school
    return current_user.school


def get_active_year(school_id):
    """
    Return the current AcademicYear for the given school, or None.
    Always returns the year with is_current=True — use for write operations.
    """
    from app.models import AcademicYear
    return AcademicYear.query.execution_options(bypass_tenant_scope=True)\
        .filter_by(school_id=school_id, is_current=True).first()


def get_view_year(school_id):
    """
    Return the academic year currently being VIEWED for the given school.
    If the user has selected a historical year in the session, returns that year.
    Falls back to the current active year (same as get_active_year).
    Use this for read/display queries; use get_active_year for write operations.
    """
    from flask import g, has_request_context
    from app.models import AcademicYear

    if has_request_context():
        view_yid = getattr(g, 'tenant_scope_view_year_id', None)
        if view_yid:
            year = AcademicYear.query.execution_options(bypass_tenant_scope=True)\
                .filter_by(id=view_yid, school_id=school_id).first()
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

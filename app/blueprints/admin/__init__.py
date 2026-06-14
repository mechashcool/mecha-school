"""
Mecha-School — Admin Blueprint  (Phase 6: Multi-Tenant)
Handles: Dashboard, User Management, Role/Permission Management,
         Academic Years (now school-scoped), School Settings
"""
from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, jsonify)
from flask_login import login_required, current_user
from sqlalchemy import func
from datetime import date, timedelta, datetime
import json
import logging

from app.models import (db, User, Role, Permission, Employee, Student, Subject,
                         FeeInstallment, StudentAttendance, Revenue, Expense,
                         Notification, AcademicYear, School, Section, Grade,
                         teacher_subjects, parent_students, Complaint, LeaveRequest,
                         EmployeeLeaveRequest,
                         SchoolVideo, SchoolAnnouncement, SchoolContentRead,
                         SchoolBuilding, UserBuildingAccess)
from app.utils.decorators import (admin_required, staff_required,
                                   get_current_school,
                                   get_active_year, get_view_year, super_admin_required)
from app.utils import code_generator

admin_bp = Blueprint('admin', __name__, template_folder='../../templates/admin')

SUPER_ADMIN_ROLE = 'super_admin'
SCHOOL_ADMIN_ROLE = 'school_admin'
LEGACY_ADMIN_ROLE = 'admin'

COMPLAINT_TYPES = {
    'academic': '\u0623\u0643\u0627\u062f\u064a\u0645\u064a\u0629',
    'administrative': '\u0625\u062f\u0627\u0631\u064a\u0629',
    'financial': '\u0645\u0627\u0644\u064a\u0629',
    'transportation': '\u0627\u0644\u0646\u0642\u0644',
    'behavior': '\u0633\u0644\u0648\u0643\u064a\u0629',
    'other': '\u0623\u062e\u0631\u0649',
}

COMPLAINT_STATUS = {
    'new': '\u062c\u062f\u064a\u062f\u0629',
    'under_review': '\u0642\u064a\u062f \u0627\u0644\u0645\u0631\u0627\u062c\u0639\u0629',
    'replied': '\u062a\u0645 \u0627\u0644\u0631\u062f',
    'closed': '\u0645\u063a\u0644\u0642\u0629',
}

LEAVE_TYPES = {
    'sick': '\u0625\u062c\u0627\u0632\u0629 \u0645\u0631\u0636\u064a\u0629',
    'medical': '\u0645\u0648\u0639\u062f \u0637\u0628\u064a',
    'family': '\u0638\u0631\u0641 \u0639\u0627\u0626\u0644\u064a',
    'travel': '\u0633\u0641\u0631',
    'emergency': '\u0637\u0627\u0631\u0626',
    'other': '\u0623\u062e\u0631\u0649',
}

LEAVE_STATUS = {
    'pending': '\u0642\u064a\u062f \u0627\u0644\u0627\u0646\u062a\u0638\u0627\u0631',
    'approved': '\u0645\u0648\u0627\u0641\u0642 \u0639\u0644\u064a\u0647',
    'rejected': '\u0645\u0631\u0641\u0648\u0636',
}

LEAVE_SOURCE_LABELS = {
    'parent':   '\u0648\u0644\u064a \u0627\u0644\u0623\u0645\u0631 / \u062c\u0648\u0627\u0644',
    'employee': '\u0627\u0644\u0645\u0648\u0638\u0641 / \u062c\u0648\u0627\u0644',
    'admin':    '\u0627\u0644\u0625\u062f\u0627\u0631\u0629',
}


def _role_name(role):
    return (role.name or '').strip() if role else ''


def _is_role_assignable_by_current_user(role):
    name = _role_name(role)
    if current_user.is_super_admin:
        return name != LEGACY_ADMIN_ROLE
    if current_user.is_school_admin:
        return name not in {SUPER_ADMIN_ROLE, SCHOOL_ADMIN_ROLE, LEGACY_ADMIN_ROLE}
    return False


def _assignable_roles():
    roles = Role.query.order_by(Role.id).all()
    return [role for role in roles if _is_role_assignable_by_current_user(role)]


def _is_super_admin_account(user):
    return bool(user and user.role and user.role.name == SUPER_ADMIN_ROLE)


def _non_super_visible_roles():
    return Role.query.filter(
        ~Role.name.in_([SUPER_ADMIN_ROLE, SCHOOL_ADMIN_ROLE, LEGACY_ADMIN_ROLE])
    ).order_by(Role.id).all()


def _admin_scope_id():
    if current_user.is_school_admin:
        return current_user.school_id
    school = get_current_school()
    return school.id if school else None


def _notify_parent(parent_id, school_id, title, body, fcm_data=None):
    db.session.add(Notification(
        school_id=school_id,
        title=title,
        body=body,
        ntype='parent_request',
        target_user_id=parent_id,
        created_by=current_user.id,
    ))
    # FCM push — fires immediately; DB commit for the in-app row happens in the caller.
    try:
        from app.services.fcm_service import is_enabled, send_push_to_user
        if is_enabled():
            data = fcm_data or {'type': 'parent_request', 'school_id': str(school_id)}
            send_push_to_user(parent_id, title, body, data)
    except Exception:
        pass


def _valid_email(email):
    return bool(email and '@' in email and '.' in email.rsplit('@', 1)[-1])


# ── Leave request helpers ──────────────────────────────────────────────────────

def _parse_leave_date(value):
    try:
        return datetime.strptime(value or '', '%Y-%m-%d').date()
    except ValueError:
        return None


def _admin_save_leave_attachment(prefix):
    """Save an optional leave attachment uploaded by the admin.

    Accepts images and PDF/Office documents. Returns (path_or_None, error_or_None).
    """
    from app.utils.helpers import (save_uploaded_file,
                                   ALLOWED_IMAGE_EXTENSIONS, ALLOWED_DOC_EXTENSIONS)
    file = request.files.get('attachment')
    if not file or not file.filename:
        return None, None
    allowed = ALLOWED_IMAGE_EXTENSIONS | ALLOWED_DOC_EXTENSIONS
    saved = save_uploaded_file(file, 'leave_requests', prefix=prefix,
                               allowed_exts=allowed)
    if not saved:
        return None, 'المرفق غير صالح أو حجمه كبير جداً.'
    return saved, None


def _notify_student_parents(student, school_id, title, body, fcm_data=None):
    """Notify every parent linked to this student (in-app + FCM)."""
    parent_ids = db.session.query(parent_students.c.user_id).filter(
        parent_students.c.student_id == student.id
    ).all()
    for (pid,) in parent_ids:
        _notify_parent(pid, school_id, title, body, fcm_data)


def _unique_ids(ids):
    seen = set()
    result = []
    for ident in ids:
        if ident not in seen:
            seen.add(ident)
            result.append(ident)
    return result


def _student_options(school, year):
    query = Student.query.filter_by(status='active')
    if school:
        query = query.filter_by(school_id=school.id)
    if year:
        query = query.filter_by(academic_year_id=year.id)
    return query.order_by(Student.full_name).all()


def _section_options(school, year):
    if not (school and year):
        return []
    return (Section.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school.id, academic_year_id=year.id)
            .order_by(Section.name)
            .all())


def _subject_options(school, year):
    if not (school and year):
        return []
    return (Subject.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school.id, academic_year_id=year.id)
            .order_by(Subject.name)
            .all())


# ── Building access helpers (optional per-school buildings feature) ───────────

# Roles that never receive building restrictions (they use other scoping models).
_BUILDING_EXEMPT_ROLES = frozenset({'parent', 'teacher', 'super_admin'})


def _school_buildings_enabled(school_id):
    """True when the given school has the buildings feature turned on."""
    if not school_id:
        return False
    school = (School.query.execution_options(bypass_tenant_scope=True)
              .get(school_id))
    return bool(school and getattr(school, 'enable_buildings', False))


def _active_buildings_for(school_id):
    if not school_id:
        return []
    return (SchoolBuilding.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school_id, is_active=True)
            .order_by(SchoolBuilding.name)
            .all())


def _user_building_ids(user_id, school_id):
    if not user_id or not school_id:
        return set()
    return {
        r.building_id for r in
        UserBuildingAccess.query.execution_options(bypass_tenant_scope=True)
        .filter_by(user_id=user_id, school_id=school_id).all()
    }


def _save_user_building_access(user, school_id):
    """
    Persist building restrictions for a user from the submitted form.

    Returns an error message (str) to abort with, or None on success.
    Does NOT commit — the caller owns the transaction.

    Behaviour:
      * Feature off / no school        → ignored (no rows touched).
      * Exempt role (parent/teacher)   → clears any stale rows, no restriction.
      * mode 'all'                     → clears rows (unrestricted).
      * mode 'restricted' + buildings  → replaces rows with the selected set.
      * mode 'restricted' + none       → validation error.
    """
    if not _school_buildings_enabled(school_id):
        return None

    # Always clear existing rows first, then re-add as needed.
    (UserBuildingAccess.query.execution_options(bypass_tenant_scope=True)
     .filter_by(user_id=user.id).delete(synchronize_session=False))

    role_name = user.role.name if user.role else ''
    if role_name in _BUILDING_EXEMPT_ROLES:
        return None

    mode = request.form.get('building_access_mode', 'all')
    if mode != 'restricted':
        return None  # unrestricted — no rows

    raw_ids = request.form.getlist('building_ids', type=int)
    valid_ids = []
    if raw_ids:
        valid_ids = [
            b.id for b in
            SchoolBuilding.query.execution_options(bypass_tenant_scope=True)
            .filter(SchoolBuilding.school_id == school_id,
                    SchoolBuilding.id.in_(raw_ids),
                    SchoolBuilding.is_active.is_(True))
            .all()
        ]
    if not valid_ids:
        return 'يجب اختيار بناية واحدة على الأقل عند تقييد المستخدم ببنايات محددة.'

    for bid in valid_ids:
        db.session.add(UserBuildingAccess(
            school_id=school_id, user_id=user.id, building_id=bid,
        ))
    return None

# ─────────────────────────────────────────────────────────────────────────────
#  DASHBOARD  (school-scoped)
# ─────────────────────────────────────────────────────────────────────────────

@admin_bp.route('/dashboard')
@login_required
@staff_required
def dashboard():
    today   = date.today()
    school  = get_current_school()
    year    = get_view_year(school.id) if school else None

    school_id = school.id if school else None
    year_id   = year.id   if year   else None

    # Core stats — scoped to the current school
    student_q  = Student.query.filter_by(status='active')
    employee_q = Employee.query.filter_by(status='active')
    if school_id:
        student_q  = student_q.filter_by(school_id=school_id)
        employee_q = employee_q.filter_by(school_id=school_id)

    stats = {
        'total_students':  student_q.count(),
        'total_employees': employee_q.count(),
        'total_users':     User.query.filter_by(is_active=True).count(),
        # Capacity info
        'school_capacity': school.capacity if school else 0,
        'capacity_pct':    0,
    }
    if school and school.capacity and school.capacity > 0:
        stats['capacity_pct'] = min(100, round(
            stats['total_students'] / school.capacity * 100
        ))

    # Fees summary — school-scoped via student FK
    fee_q = db.session.query(func.sum(FeeInstallment.amount))\
        .join(FeeInstallment.fee_record)\
        .filter(FeeInstallment.status == 'paid',
                FeeInstallment.paid_date == today)
    if school_id:
        from app.models import FeeRecord
        fee_q = fee_q.filter(FeeRecord.school_id == school_id)
    paid_today = fee_q.scalar() or 0

    overdue_q = FeeInstallment.query\
        .join(FeeInstallment.fee_record)\
        .filter(FeeInstallment.status == 'pending',
                FeeInstallment.due_date < today)
    if school_id:
        from app.models import FeeRecord
        overdue_q = overdue_q.filter(FeeRecord.school_id == school_id)
    overdue = overdue_q.count()

    stats['fees_collected_today'] = float(paid_today)
    stats['overdue_installments'] = overdue

    # Attendance today — school-scoped
    att_q_base = StudentAttendance.query.filter_by(date=today)
    if school_id:
        att_q_base = att_q_base.filter_by(school_id=school_id)
    stats['present_today'] = att_q_base.filter_by(status='present').count()
    stats['absent_today']  = att_q_base.filter_by(status='absent').count()

    # Revenue vs Expense this month — school-scoped
    first_of_month = today.replace(day=1)
    rev_q = db.session.query(func.sum(Revenue.amount))\
        .filter(Revenue.date >= first_of_month)
    exp_q = db.session.query(func.sum(Expense.amount))\
        .filter(Expense.date >= first_of_month)
    if school_id:
        rev_q = rev_q.filter(Revenue.school_id == school_id)
        exp_q = exp_q.filter(Expense.school_id == school_id)
    monthly_revenue = rev_q.scalar() or 0
    monthly_expense = exp_q.scalar() or 0
    stats['monthly_revenue'] = float(monthly_revenue)
    stats['monthly_expense'] = float(monthly_expense)
    stats['monthly_balance'] = float(monthly_revenue) - float(monthly_expense)

    # Last 5 notifications — school-scoped
    notif_q = Notification.query
    if school_id:
        notif_q = notif_q.filter_by(school_id=school_id)
    recent_notifications = notif_q.order_by(Notification.created_at.desc()).limit(5).all()

    # Recent students
    recent_q = Student.query
    if school_id:
        recent_q = recent_q.filter_by(school_id=school_id)
    if year_id:
        recent_q = recent_q.filter_by(academic_year_id=year_id)
    recent_students = recent_q.order_by(Student.created_at.desc()).limit(5).all()

    return render_template('admin/dashboard.html',
                           stats=stats,
                           recent_notifications=recent_notifications,
                           recent_students=recent_students,
                           today=today,
                           school=school,
                           active_year=year)


# ─────────────────────────────────────────────────────────────────────────────
#  USER MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

@admin_bp.route('/users')
@login_required
@admin_required
def users_list():
    page        = request.args.get('page', 1, type=int)
    search      = request.args.get('q', '')
    role_filter = request.args.get('role_id', 'all')
    school      = get_current_school()

    query = User.query
    if search:
        query = query.filter(
            User.full_name.ilike(f'%{search}%') |
            User.username.ilike(f'%{search}%')
        )
    if role_filter != 'all':
        query = query.filter(User.role_id == int(role_filter))

    # School managers see only non-super-admin users in their own school.
    if current_user.is_school_admin:
        query = (query.join(Role, User.role_id == Role.id)
                 .filter(User.school_id == current_user.school_id,
                         Role.name != SUPER_ADMIN_ROLE))

    users = query.order_by(User.created_at.desc())\
                 .paginate(page=page, per_page=20, error_out=False)
    roles = (Role.query.order_by(Role.id).all()
             if current_user.is_super_admin else _non_super_visible_roles())
    all_schools = (School.query.filter_by(is_active=True).order_by(School.id).all()
                   if current_user.is_super_admin else [])
    return render_template('admin/users_list.html',
                           users=users, roles=roles,
                           search=search, role_filter=role_filter,
                           all_schools=all_schools)


@admin_bp.route('/users/create', methods=['GET', 'POST'])
@login_required
@admin_required
def create_user():
    is_school_manager = current_user.is_school_admin
    school = get_current_school()

    roles = _assignable_roles()
    year = get_active_year(school.id) if school else None

    all_students = _student_options(school, year)

    all_schools = (School.query.filter_by(is_active=True).order_by(School.id).all()
                   if current_user.is_super_admin else [])

    all_sections = _section_options(school, year)
    all_subjects = _subject_options(school, year)

    # Only super-admin can grant per-user extra permissions. School managers
    # create users with role permissions only.
    all_permissions = []
    if current_user.is_super_admin:
        all_permissions = (Permission.query
                           .order_by(Permission.category, Permission.name)
                           .all())

    if request.method == 'POST':
        username  = request.form.get('username', '').strip()
        email     = request.form.get('email', '').strip()
        full_name = request.form.get('full_name', '').strip()
        password  = request.form.get('password', '')
        role_id   = request.form.get('role_id', type=int)

        role_obj = Role.query.get(role_id) if role_id else None

        # Determine school assignment
        if current_user.is_super_admin:
            assigned_school_id = request.form.get('school_id', type=int) or None
        else:
            assigned_school_id = current_user.school_id

        # School managers cannot create admin-role users — reject before any DB writes
        if not role_obj or not _is_role_assignable_by_current_user(role_obj):
            flash('لا يمكنك إسناد دور إداري للمستخدمين. اختر دوراً آخر.', 'danger')
            return redirect(url_for('admin.users_list'))

        if role_obj.name == SUPER_ADMIN_ROLE:
            assigned_school_id = None

        # Auto-generate username when left blank and role supports it
        if (not username and password and assigned_school_id and role_obj
                and role_obj.name in ('teacher', 'parent', 'school_admin')):
            username = code_generator.generate_username(assigned_school_id, role_obj.name)

        errors = []
        if not username: errors.append('اسم المستخدم مطلوب.')
        if User.query.execution_options(bypass_tenant_scope=True).filter_by(username=username).first():
            errors.append('اسم المستخدم مستخدم بالفعل.')
        if email and not _valid_email(email):
            errors.append('Invalid email address.')
        if email and User.query.execution_options(bypass_tenant_scope=True).filter_by(email=email).first():
            errors.append('البريد الإلكتروني مستخدم بالفعل.')
        if not password or len(password) < 6:
            errors.append('كلمة المرور يجب أن تكون 6 أحرف على الأقل.')
        if not role_id: errors.append('الدور مطلوب.')

        if role_obj.name != SUPER_ADMIN_ROLE and not assigned_school_id:
            errors.append('School is required for all non-super-admin users.')

        if assigned_school_id and not School.query.get(assigned_school_id):
            errors.append('Selected school is invalid.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return redirect(url_for('admin.create_user'))

        user = User(username=username, email=email or None, full_name=full_name,
                    role=role_obj, school_id=assigned_school_id)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()

        if current_user.is_super_admin:
            selected_perm_ids = request.form.getlist('permissions', type=int)
            user.extra_permissions = Permission.query.filter(
                Permission.id.in_(selected_perm_ids)
            ).all()

        assigned_year = get_active_year(assigned_school_id) if assigned_school_id else None

        if role_obj and role_obj.name == 'parent':
            student_ids = _unique_ids(request.form.getlist('student_ids', type=int))
            if student_ids:
                # Validate students belong to the same school
                q = (Student.query
                     .execution_options(bypass_tenant_scope=True)
                     .filter(Student.id.in_(student_ids)))
                if assigned_school_id:
                    q = q.filter(Student.school_id == assigned_school_id)
                if assigned_year:
                    q = q.filter(Student.academic_year_id == assigned_year.id)
                user.children = q.all()

        elif role_obj and role_obj.name == 'teacher':
            # Auto-create Employee record for the teacher
            emp = Employee(
                employee_id=code_generator.generate_employee_id(assigned_school_id),
                full_name=user.full_name,
                job_title='معلم',
                school_id=assigned_school_id,
                base_salary=0,
                status='active',
                user_id=user.id,
            )
            db.session.add(emp)
            db.session.flush()

            # Assign homeroom sections (validates they belong to the same school)
            section_ids = _unique_ids(request.form.getlist('teacher_section_ids', type=int))
            if section_ids and assigned_school_id:
                section_filter = [
                    Section.id.in_(section_ids),
                    Section.school_id == assigned_school_id,
                ]
                if assigned_year:
                    section_filter.append(Section.academic_year_id == assigned_year.id)
                (Section.query
                 .execution_options(bypass_tenant_scope=True)
                 .filter(*section_filter)
                 .update({'teacher_id': emp.id}, synchronize_session=False))

            # Assign teaching subjects × sections into teacher_subjects
            subject_ids = _unique_ids(request.form.getlist('teacher_subject_ids', type=int))
            if subject_ids and section_ids and assigned_school_id:
                subject_filter = [
                    Subject.id.in_(subject_ids),
                    Subject.school_id == assigned_school_id,
                ]
                section_filter = [
                    Section.id.in_(section_ids),
                    Section.school_id == assigned_school_id,
                ]
                if assigned_year:
                    subject_filter.append(Subject.academic_year_id == assigned_year.id)
                    section_filter.append(Section.academic_year_id == assigned_year.id)
                valid_subj_ids = [r[0] for r in
                    db.session.query(Subject.id)
                              .execution_options(bypass_tenant_scope=True)
                              .filter(*subject_filter)
                              .all()]
                valid_sec_ids = [r[0] for r in
                    db.session.query(Section.id)
                              .execution_options(bypass_tenant_scope=True)
                              .filter(*section_filter)
                              .all()]
                rows = [
                    {'employee_id': emp.id, 'subject_id': s, 'section_id': c}
                    for s in valid_subj_ids for c in valid_sec_ids
                ]
                if rows:
                    db.session.execute(teacher_subjects.insert(), rows)

        # ── Building access restrictions (optional feature) ─────────────────
        _berr = _save_user_building_access(user, assigned_school_id)
        if _berr:
            db.session.rollback()
            flash(_berr, 'danger')
            return redirect(url_for('admin.create_user'))

        import logging as _logging
        _log = _logging.getLogger(__name__)
        from sqlalchemy.exc import IntegrityError as _IntegrityError
        try:
            db.session.commit()
        except _IntegrityError as _exc:
            db.session.rollback()
            _s = str(_exc).lower()
            _log.error('Admin create_user commit IntegrityError: %s', str(_exc)[:800])
            if 'username' in _s and 'unique' in _s:
                flash('اسم المستخدم مستخدم مسبقاً، يرجى اختيار اسم آخر.', 'danger')
            elif ('uq_employee_school_employee_id' in _s
                  or 'ix_employees_employee_id' in _s
                  or ('employee_id' in _s and 'unique' in _s)):
                flash('تعذر توليد رقم الموظف، يرجى المحاولة مرة أخرى.', 'danger')
            else:
                flash('تعذر حفظ البيانات بسبب تعارض في القيم. يرجى المحاولة مرة أخرى.', 'danger')
            return redirect(url_for('admin.create_user'))
        flash(f'تم إنشاء الحساب بنجاح. اسم المستخدم: {username}', 'success')
        return redirect(url_for('admin.users_list'))

    preselect_school_id = request.args.get('school_id', type=int)
    _access_school_id = school.id if school else None
    _buildings_access_enabled = _school_buildings_enabled(_access_school_id)
    return render_template('admin/user_form.html',
                           roles=roles, user=None,
                           all_permissions=all_permissions,
                           all_students=all_students, all_schools=all_schools,
                           all_sections=all_sections, all_subjects=all_subjects,
                           teacher_section_ids=set(), teacher_subject_ids=set(),
                           is_school_manager=is_school_manager,
                           safe_permissions=[],
                           preselect_school_id=preselect_school_id,
                           buildings_access_enabled=_buildings_access_enabled,
                           buildings_for_access=_active_buildings_for(_access_school_id),
                           user_building_ids=set())


@admin_bp.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_user(user_id):
    from flask import abort
    # Bypass tenant scope so cross-school access raises 403 (not silently 404)
    user = db.session.get(User, user_id, execution_options={'bypass_tenant_scope': True})
    if user is None:
        abort(404)
    is_school_manager = current_user.is_school_admin

    # School managers can only edit lower-privilege users from their own school.
    if is_school_manager and _is_super_admin_account(user):
        abort(403)
    if is_school_manager and user.role and user.role.name == SCHOOL_ADMIN_ROLE:
        abort(403)
    if is_school_manager and user.school_id != current_user.school_id:
        abort(403)
    if is_school_manager and user.id == current_user.id:
        abort(403)

    roles = _assignable_roles()

    # Super-admin: full permission list. School managers cannot edit extra
    # permissions from School User Management.
    if current_user.is_super_admin:
        all_permissions = (Permission.query
                           .order_by(Permission.category, Permission.name).all())
    else:
        all_permissions = []
    safe_permissions = []

    school = get_current_school()
    year = get_active_year(school.id) if school else None
    all_students = _student_options(school, year)
    all_schools = (School.query.filter_by(is_active=True).order_by(School.id).all()
                   if current_user.is_super_admin else [])

    # Sections and subjects for teacher role assignment
    all_sections = _section_options(school, year)
    all_subjects = _subject_options(school, year)

    # Current homeroom section IDs and subject IDs for this teacher (if applicable)
    teacher_section_ids = set()
    teacher_subject_ids = set()
    if user.role and user.role.name == 'teacher':
        emp = (Employee.query
               .execution_options(bypass_tenant_scope=True)
               .filter_by(user_id=user.id).first())
        if emp:
            teacher_section_ids = {
                s.id for s in
                Section.query.execution_options(bypass_tenant_scope=True)
                             .filter_by(teacher_id=emp.id).all()
            }
            teacher_subject_ids = {
                row[0] for row in
                db.session.query(teacher_subjects.c.subject_id)
                          .filter(teacher_subjects.c.employee_id == emp.id)
                          .distinct()
                          .all()
            }

    if request.method == 'POST':
        user.full_name = request.form.get('full_name', user.full_name).strip()

        # Super admin can change the username
        if current_user.is_super_admin:
            new_username = request.form.get('username', '').strip()
            if not new_username:
                flash('اسم المستخدم مطلوب.', 'danger')
                return redirect(url_for('admin.edit_user', user_id=user.id))
            if new_username != user.username:
                clash = (User.query
                         .execution_options(bypass_tenant_scope=True)
                         .filter(User.username == new_username, User.id != user.id)
                         .first())
                if clash:
                    flash('اسم المستخدم مستخدم بالفعل من قِبل مستخدم آخر.', 'danger')
                    return redirect(url_for('admin.edit_user', user_id=user.id))
                user.username = new_username

        new_email = request.form.get('email', '').strip()
        if new_email and not _valid_email(new_email):
            flash('Invalid email address.', 'danger')
            return redirect(url_for('admin.edit_user', user_id=user.id))
        if new_email:
            existing_email = (User.query
                              .execution_options(bypass_tenant_scope=True)
                              .filter(User.email == new_email, User.id != user.id)
                              .first())
            if existing_email:
                flash('Email is already in use.', 'danger')
                return redirect(url_for('admin.edit_user', user_id=user.id))
        user.email = new_email or None
        user.phone     = request.form.get('phone', '').strip()

        new_role = user.role
        new_role_id = request.form.get('role_id', type=int)
        if new_role_id:
            new_role = Role.query.get(new_role_id)
            # School managers cannot promote users to admin roles
            if not new_role or not _is_role_assignable_by_current_user(new_role):
                flash('لا يمكنك إسناد دور إداري للمستخدمين.', 'danger')
                return redirect(url_for('admin.edit_user', user_id=user.id))
            user.role = new_role

        user.is_active = bool(request.form.get('is_active'))

        # Super admin can change school assignment
        if current_user.is_super_admin:
            new_school_id = request.form.get('school_id', type=int) or None
            if new_role and new_role.name == SUPER_ADMIN_ROLE:
                new_school_id = None
            elif not new_school_id:
                flash('School is required for all non-super-admin users.', 'danger')
                return redirect(url_for('admin.edit_user', user_id=user.id))
            if new_school_id and not School.query.get(new_school_id):
                flash('Selected school is invalid.', 'danger')
                return redirect(url_for('admin.edit_user', user_id=user.id))
            user.school_id = new_school_id

        user_year = get_active_year(user.school_id) if user.school_id else None

        new_password = request.form.get('new_password', '')
        if new_password and len(new_password) >= 6:
            user.set_password(new_password)

        # Super admin: full permission control
        if current_user.is_super_admin:
            selected_perm_ids = request.form.getlist('permissions', type=int)
            user.extra_permissions = Permission.query.filter(
                Permission.id.in_(selected_perm_ids)
            ).all()

        if new_role and new_role.name == 'parent':
            student_ids = _unique_ids(request.form.getlist('student_ids', type=int))
            if student_ids:
                q = (Student.query
                     .execution_options(bypass_tenant_scope=True)
                     .filter(Student.id.in_(student_ids)))
                if user.school_id:
                    q = q.filter(Student.school_id == user.school_id)
                if user_year:
                    q = q.filter(Student.academic_year_id == user_year.id)
                user.children = q.all()
            else:
                user.children = []

        elif new_role and new_role.name == 'teacher':
            import uuid as _uuid
            emp = (Employee.query
                   .execution_options(bypass_tenant_scope=True)
                   .filter_by(user_id=user.id).first())
            if not emp:
                emp = Employee(
                    employee_id=code_generator.generate_employee_id(user.school_id),
                    full_name=user.full_name,
                    job_title='معلم',
                    school_id=user.school_id,
                    base_salary=0,
                    status='active',
                    user_id=user.id,
                )
                db.session.add(emp)
                db.session.flush()

            new_section_ids = _unique_ids(request.form.getlist('teacher_section_ids', type=int))
            # Clear previous homeroom assignments for this teacher
            (Section.query
             .execution_options(bypass_tenant_scope=True)
             .filter_by(teacher_id=emp.id)
             .update({'teacher_id': None}, synchronize_session=False))
            if new_section_ids and user.school_id:
                section_filter = [
                    Section.id.in_(new_section_ids),
                    Section.school_id == user.school_id,
                ]
                if user_year:
                    section_filter.append(Section.academic_year_id == user_year.id)
                (Section.query
                 .execution_options(bypass_tenant_scope=True)
                 .filter(*section_filter)
                 .update({'teacher_id': emp.id}, synchronize_session=False))

            # Update subject assignments — clear then rebuild
            db.session.execute(
                teacher_subjects.delete().where(
                    teacher_subjects.c.employee_id == emp.id
                )
            )
            new_subject_ids = _unique_ids(request.form.getlist('teacher_subject_ids', type=int))
            if new_subject_ids and new_section_ids and user.school_id:
                subject_filter = [
                    Subject.id.in_(new_subject_ids),
                    Subject.school_id == user.school_id,
                ]
                section_filter = [
                    Section.id.in_(new_section_ids),
                    Section.school_id == user.school_id,
                ]
                if user_year:
                    subject_filter.append(Subject.academic_year_id == user_year.id)
                    section_filter.append(Section.academic_year_id == user_year.id)
                valid_subj_ids = [r[0] for r in
                    db.session.query(Subject.id)
                              .execution_options(bypass_tenant_scope=True)
                              .filter(*subject_filter)
                              .all()]
                valid_sec_ids = [r[0] for r in
                    db.session.query(Section.id)
                              .execution_options(bypass_tenant_scope=True)
                              .filter(*section_filter)
                              .all()]
                rows = [
                    {'employee_id': emp.id, 'subject_id': s, 'section_id': c}
                    for s in valid_subj_ids for c in valid_sec_ids
                ]
                if rows:
                    db.session.execute(teacher_subjects.insert(), rows)

        # ── Building access restrictions (optional feature) ─────────────────
        _berr = _save_user_building_access(user, user.school_id)
        if _berr:
            db.session.rollback()
            flash(_berr, 'danger')
            return redirect(url_for('admin.edit_user', user_id=user.id))

        db.session.commit()
        flash('تم تحديث بيانات المستخدم بنجاح.', 'success')
        return redirect(url_for('admin.users_list'))

    _buildings_access_enabled = _school_buildings_enabled(user.school_id)
    return render_template('admin/user_form.html',
                           user=user, roles=roles,
                           all_permissions=all_permissions,
                           safe_permissions=safe_permissions,
                           is_school_manager=is_school_manager,
                           all_students=all_students,
                           all_schools=all_schools,
                           all_sections=all_sections,
                           all_subjects=all_subjects,
                           teacher_section_ids=teacher_section_ids,
                           teacher_subject_ids=teacher_subject_ids,
                           buildings_access_enabled=_buildings_access_enabled,
                           buildings_for_access=_active_buildings_for(user.school_id),
                           user_building_ids=_user_building_ids(user.id, user.school_id))


@admin_bp.route('/users/<int:user_id>/toggle', methods=['POST'])
@login_required
@admin_required
def toggle_user(user_id):
    from flask import abort
    user = db.session.get(User, user_id, execution_options={'bypass_tenant_scope': True})
    if user is None:
        abort(404)
    if current_user.is_school_admin and (
            _is_super_admin_account(user)
            or (user.role and user.role.name == SCHOOL_ADMIN_ROLE)
            or user.school_id != current_user.school_id):
        abort(403)
    if _is_super_admin_account(user) and not current_user.is_super_admin:
        abort(403)
    if user.id == current_user.id:
        flash('لا يمكنك تعطيل حسابك الخاص.', 'danger')
    else:
        user.is_active = not user.is_active
        db.session.commit()
        status = 'مفعّل' if user.is_active else 'معطّل'
        flash(f'تم تغيير حالة المستخدم إلى: {status}.', 'success')
    return redirect(url_for('admin.users_list'))


@admin_bp.route('/users/<int:user_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    from flask import abort
    user = db.session.get(User, user_id, execution_options={'bypass_tenant_scope': True})
    if user is None:
        abort(404)
    if current_user.is_school_admin and (
            _is_super_admin_account(user)
            or (user.role and user.role.name == SCHOOL_ADMIN_ROLE)
            or user.school_id != current_user.school_id):
        abort(403)
    if _is_super_admin_account(user) and not current_user.is_super_admin:
        abort(403)
    if user.id == current_user.id:
        flash('لا يمكنك حذف حسابك الخاص.', 'danger')
    elif _is_super_admin_account(user) and not current_user.is_super_admin:
        flash('لا يمكنك حذف مسؤول النظام.', 'danger')
    else:
        db.session.delete(user)
        db.session.commit()
        flash('تم حذف المستخدم.', 'success')
    return redirect(url_for('admin.users_list'))


# ─────────────────────────────────────────────────────────────────────────────
#  ROLE MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

@admin_bp.route('/complaints')
@login_required
@admin_required
def complaints_list():
    school_id = _admin_scope_id()
    query = Complaint.query.execution_options(include_all_years=True)
    if school_id:
        query = query.filter_by(school_id=school_id)
    complaints = query.order_by(Complaint.created_at.desc()).all()
    return render_template('admin/complaints_list.html',
                           complaints=complaints,
                           status_labels=COMPLAINT_STATUS,
                           type_labels=COMPLAINT_TYPES)


@admin_bp.route('/complaints/<int:complaint_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def complaint_detail(complaint_id):
    school_id = _admin_scope_id()
    query = Complaint.query.execution_options(include_all_years=True).filter_by(id=complaint_id)
    if school_id:
        query = query.filter_by(school_id=school_id)
    complaint = query.first_or_404()

    if request.method == 'POST':
        new_status = request.form.get('status', '').strip()
        manager_reply = request.form.get('manager_reply', '').strip()
        if new_status not in COMPLAINT_STATUS:
            flash('\u062d\u0627\u0644\u0629 \u0627\u0644\u0634\u0643\u0648\u0649 \u063a\u064a\u0631 \u0635\u0627\u0644\u062d\u0629.', 'danger')
            return redirect(url_for('admin.complaint_detail', complaint_id=complaint.id))

        changed = (
            new_status != complaint.status
            or manager_reply != (complaint.manager_reply or '')
        )
        complaint.status = new_status
        complaint.manager_reply = manager_reply or None
        complaint.replied_by = current_user.id
        complaint.replied_at = datetime.utcnow()

        if changed:
            _notify_parent(
                complaint.parent_id,
                complaint.school_id,
                '\u062a\u062d\u062f\u064a\u062b \u0627\u0644\u0634\u0643\u0648\u0649',
                f'\u062a\u0645 \u062a\u062d\u062f\u064a\u062b \u0634\u0643\u0648\u0627\u0643 \u0628\u062d\u0627\u0644\u0629: {COMPLAINT_STATUS[new_status]}.',
                fcm_data={
                    'type':        'complaint_reply',
                    'complaint_id': str(complaint.id),
                    'student_id':  str(complaint.student_id),
                    'screen':      'complaints',
                },
            )
        db.session.commit()
        flash('\u062a\u0645 \u062a\u062d\u062f\u064a\u062b \u0627\u0644\u0634\u0643\u0648\u0649 \u0628\u0646\u062c\u0627\u062d.', 'success')
        return redirect(url_for('admin.complaint_detail', complaint_id=complaint.id))

    return render_template('admin/complaint_detail.html',
                           complaint=complaint,
                           status_labels=COMPLAINT_STATUS,
                           type_labels=COMPLAINT_TYPES)


@admin_bp.route('/leave-requests')
@login_required
@admin_required
def leave_requests_list():
    school_id = _admin_scope_id()
    status_filter = request.args.get('status', '').strip()

    query = LeaveRequest.query.execution_options(include_all_years=True)
    if school_id:
        query = query.filter_by(school_id=school_id)
    if status_filter and status_filter in LEAVE_STATUS:
        query = query.filter_by(status=status_filter)

    all_requests = query.order_by(LeaveRequest.created_at.desc()).all()
    stats = {
        'total':    len(all_requests),
        'pending':  sum(1 for r in all_requests if r.status == 'pending'),
        'approved': sum(1 for r in all_requests if r.status == 'approved'),
        'rejected': sum(1 for r in all_requests if r.status == 'rejected'),
    }
    return render_template('admin/leave_requests_list.html',
                           requests=all_requests,
                           stats=stats,
                           status_filter=status_filter,
                           status_labels=LEAVE_STATUS,
                           type_labels=LEAVE_TYPES,
                           source_labels=LEAVE_SOURCE_LABELS)


@admin_bp.route('/leave-requests/<int:request_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def leave_request_detail(request_id):
    school_id = _admin_scope_id()
    query = LeaveRequest.query.execution_options(include_all_years=True).filter_by(id=request_id)
    if school_id:
        query = query.filter_by(school_id=school_id)
    leave_request = query.first_or_404()

    if request.method == 'POST':
        new_status = request.form.get('status', '').strip()
        manager_note = request.form.get('manager_note', '').strip()
        if new_status not in {'approved', 'rejected'}:
            flash('\u064a\u0631\u062c\u0649 \u0627\u062e\u062a\u064a\u0627\u0631 \u0645\u0648\u0627\u0641\u0642\u0629 \u0623\u0648 \u0631\u0641\u0636.', 'danger')
            return redirect(url_for('admin.leave_request_detail', request_id=leave_request.id))

        changed = (
            new_status != leave_request.status
            or manager_note != (leave_request.manager_note or '')
        )
        leave_request.status = new_status
        leave_request.manager_note = manager_note or None
        leave_request.reviewed_by = current_user.id
        leave_request.reviewed_at = datetime.utcnow()

        notification_kwargs = dict(
            title='\u062a\u062d\u062f\u064a\u062b \u0637\u0644\u0628 \u0627\u0644\u0625\u062c\u0627\u0632\u0629',
            body=f'\u062a\u0645 \u062a\u062d\u062f\u064a\u062b \u0637\u0644\u0628 \u0627\u0644\u0625\u062c\u0627\u0632\u0629 \u0628\u062d\u0627\u0644\u0629: {LEAVE_STATUS[new_status]}.',
            fcm_data={
                'type':       'leave_request_update',
                'request_id': str(leave_request.id),
                'student_id': str(leave_request.student_id),
                'screen':     'leave_requests',
            },
        )
        if changed:
            if leave_request.source == 'admin' and leave_request.student:
                _notify_student_parents(
                    leave_request.student,
                    leave_request.school_id,
                    **notification_kwargs,
                )
            else:
                _notify_parent(
                    leave_request.parent_id,
                    leave_request.school_id,
                    **notification_kwargs,
                )
        db.session.commit()
        flash('\u062a\u0645 \u062a\u062d\u062f\u064a\u062b \u0637\u0644\u0628 \u0627\u0644\u0625\u062c\u0627\u0632\u0629 \u0628\u0646\u062c\u0627\u062d.', 'success')
        return redirect(url_for('admin.leave_request_detail', request_id=leave_request.id))

    return render_template('admin/leave_request_detail.html',
                           leave_request=leave_request,
                           status_labels=LEAVE_STATUS,
                           type_labels=LEAVE_TYPES,
                           source_labels=LEAVE_SOURCE_LABELS)


# ── Employee (teacher) leave requests ─────────────────────────────────────────

def _notify_employee_user(user_id, school_id, title, body, fcm_data=None):
    """In-app + FCM notification to a single staff user in the same school."""
    if not user_id:
        return
    db.session.add(Notification(
        school_id=school_id,
        title=title,
        body=body,
        ntype='employee_leave_request',
        target_user_id=user_id,
        created_by=current_user.id,
    ))
    try:
        from app.services.fcm_service import is_enabled, send_push_to_user
        if is_enabled():
            data = fcm_data or {'type': 'employee_leave_request',
                                'school_id': str(school_id)}
            send_push_to_user(user_id, title, body, data)
    except Exception:
        pass


@admin_bp.route('/employee-leave-requests')
@login_required
@admin_required
def employee_leave_requests_list():
    school_id = _admin_scope_id()
    status_filter = request.args.get('status', '').strip()

    query = EmployeeLeaveRequest.query
    if school_id:
        query = query.filter_by(school_id=school_id)
    if status_filter and status_filter in LEAVE_STATUS:
        query = query.filter_by(status=status_filter)

    all_requests = query.order_by(EmployeeLeaveRequest.created_at.desc()).all()
    stats = {
        'total':    len(all_requests),
        'pending':  sum(1 for r in all_requests if r.status == 'pending'),
        'approved': sum(1 for r in all_requests if r.status == 'approved'),
        'rejected': sum(1 for r in all_requests if r.status == 'rejected'),
    }
    return render_template('admin/employee_leave_requests_list.html',
                           requests=all_requests,
                           stats=stats,
                           status_filter=status_filter,
                           status_labels=LEAVE_STATUS,
                           type_labels=LEAVE_TYPES,
                           source_labels=LEAVE_SOURCE_LABELS)


@admin_bp.route('/employee-leave-requests/<int:request_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def employee_leave_request_detail(request_id):
    school_id = _admin_scope_id()
    query = EmployeeLeaveRequest.query.filter_by(id=request_id)
    if school_id:
        query = query.filter_by(school_id=school_id)
    leave_request = query.first_or_404()

    if request.method == 'POST':
        new_status = request.form.get('status', '').strip()
        admin_response = request.form.get('admin_response', '').strip()
        rejection_reason = request.form.get('rejection_reason', '').strip()
        if new_status not in {'approved', 'rejected'}:
            flash('يرجى اختيار موافقة أو رفض.', 'danger')
            return redirect(url_for('admin.employee_leave_request_detail',
                                    request_id=leave_request.id))

        changed = new_status != leave_request.status
        leave_request.status = new_status
        leave_request.admin_response = admin_response or None
        leave_request.rejection_reason = (
            rejection_reason or None) if new_status == 'rejected' else None
        leave_request.reviewed_by = current_user.id
        leave_request.reviewed_at = datetime.utcnow()

        if changed:
            employee = leave_request.employee
            target_user_id = employee.user_id if employee else None
            _notify_employee_user(
                target_user_id,
                leave_request.school_id,
                'تحديث طلب الإجازة',
                f'تم تحديث طلب الإجازة بحالة: {LEAVE_STATUS[new_status]}.',
                fcm_data={
                    'type':       'employee_leave_request_update',
                    'request_id': str(leave_request.id),
                    'screen':     'leave_requests',
                },
            )
        db.session.commit()
        flash('تم تحديث طلب الإجازة بنجاح.', 'success')
        return redirect(url_for('admin.employee_leave_request_detail',
                                request_id=leave_request.id))

    return render_template('admin/employee_leave_request_detail.html',
                           leave_request=leave_request,
                           status_labels=LEAVE_STATUS,
                           type_labels=LEAVE_TYPES,
                           source_labels=LEAVE_SOURCE_LABELS)


# ── Admin-created leave requests ──────────────────────────────────────────────

@admin_bp.route('/leave-requests/create', methods=['GET', 'POST'])
@login_required
@admin_required
def leave_request_create_admin():
    """Admin creates a leave request on behalf of a student."""
    school_id = _admin_scope_id()
    if not school_id:
        flash('لا يمكن تحديد المدرسة.', 'danger')
        return redirect(url_for('admin.leave_requests_list'))

    active_year = get_active_year(school_id)

    grades = []
    stages = []
    grades_json = '[]'
    if active_year:
        grades = (Grade.query
                  .execution_options(include_all_years=True)
                  .filter_by(school_id=school_id, academic_year_id=active_year.id)
                  .order_by(Grade.stage, Grade.name)
                  .all())
        stages = sorted(set(g.stage for g in grades if g.stage))
        grades_json = json.dumps([
            {'id': g.id, 'name': g.name, 'stage': g.stage or ''}
            for g in grades
        ])

    if request.method == 'POST':
        stage_val   = request.form.get('stage', '').strip()
        grade_id    = request.form.get('grade_id', type=int)
        section_id  = request.form.get('section_id', type=int)
        student_id  = request.form.get('student_id', type=int)
        leave_type  = request.form.get('leave_type', '').strip()
        from_date   = _parse_leave_date(request.form.get('from_date'))
        to_date     = _parse_leave_date(request.form.get('to_date'))
        notes       = request.form.get('notes', '').strip() or None
        status_val  = request.form.get('status', 'pending').strip()
        attachment, upload_error = _admin_save_leave_attachment(
            f'admin-leave-{school_id}')

        errors = []
        grade = section = student = None

        if not active_year:
            errors.append('لا يوجد عام دراسي نشط.')

        # Validate grade belongs to school/year and stage matches the posted value
        if grade_id and active_year:
            grade = (Grade.query
                     .execution_options(include_all_years=True)
                     .filter_by(id=grade_id, school_id=school_id,
                                academic_year_id=active_year.id)
                     .first())
            if not grade:
                errors.append('الصف غير موجود أو لا ينتمي لهذه المدرسة.')
            elif stage_val and grade.stage != stage_val:
                errors.append('الصف لا ينتمي للمرحلة المحددة.')
        else:
            errors.append('يرجى اختيار الصف.')

        # Validate section belongs to the selected grade and school/year
        if section_id and grade:
            section = (Section.query
                       .execution_options(include_all_years=True)
                       .filter_by(id=section_id, grade_id=grade.id,
                                  school_id=school_id,
                                  academic_year_id=active_year.id)
                       .first())
            if not section:
                errors.append('الشعبة لا تنتمي للصف المحدد.')
        elif not section_id:
            errors.append('يرجى اختيار الشعبة.')

        # Validate student belongs to section and school
        if student_id:
            student = (Student.query
                       .execution_options(bypass_tenant_scope=True)
                       .filter_by(id=student_id, school_id=school_id)
                       .first())
            if not student:
                errors.append('الطالب غير موجود أو لا ينتمي لهذه المدرسة.')
            elif section_id and student.section_id != section_id:
                errors.append('الطالب لا ينتمي للشعبة المحددة.')
        else:
            errors.append('يرجى اختيار الطالب.')

        if leave_type not in LEAVE_TYPES:
            errors.append('نوع الإجازة غير صالح.')
        if not from_date:
            errors.append('تاريخ البداية مطلوب.')
        if not to_date:
            errors.append('تاريخ النهاية مطلوب.')
        if from_date and to_date and to_date < from_date:
            errors.append('تاريخ النهاية يجب أن يكون بعد تاريخ البداية.')
        if status_val not in LEAVE_STATUS:
            errors.append('حالة الطلب غير صالحة.')
        if upload_error:
            errors.append(upload_error)

        if errors:
            for err in errors:
                flash(err, 'danger')
            return render_template('admin/leave_request_create_admin.html',
                                   stages=stages,
                                   grades_json=grades_json,
                                   type_labels=LEAVE_TYPES,
                                   status_labels=LEAVE_STATUS,
                                   form_data=request.form), 422

        leave_obj = LeaveRequest(
            parent_id=None,
            student_id=student.id,
            school_id=school_id,
            academic_year_id=active_year.id,
            leave_type=leave_type,
            from_date=from_date,
            to_date=to_date,
            notes=notes,
            attachment_path=attachment,
            status=status_val,
            source='admin',
            created_by_user_id=current_user.id,
        )
        if status_val in ('approved', 'rejected'):
            leave_obj.reviewed_by = current_user.id
            leave_obj.reviewed_at = datetime.utcnow()

        db.session.add(leave_obj)
        _notify_student_parents(
            student,
            school_id,
            title='طلب إجازة من الإدارة',
            body=f'أضافت الإدارة طلب إجازة للطالب {student.full_name}.',
            fcm_data={
                'type':       'leave_request_update',
                'student_id': str(student.id),
                'screen':     'leave_requests',
            },
        )
        db.session.commit()
        flash('تم إضافة طلب الإجازة بنجاح.', 'success')
        return redirect(url_for('admin.leave_requests_list'))

    return render_template('admin/leave_request_create_admin.html',
                           stages=stages,
                           grades_json=grades_json,
                           type_labels=LEAVE_TYPES,
                           status_labels=LEAVE_STATUS,
                           form_data={})


@admin_bp.route('/students/<int:student_id>/leave-archive')
@login_required
@admin_required
def student_leave_archive(student_id):
    """Complete leave history for one student — scoped by school."""
    school_id = _admin_scope_id()
    student = (Student.query
               .execution_options(bypass_tenant_scope=True)
               .filter_by(id=student_id, school_id=school_id)
               .first_or_404())

    status_filter    = request.args.get('status', '').strip()
    type_filter      = request.args.get('leave_type', '').strip()
    date_from_filter = _parse_leave_date(request.args.get('date_from'))
    date_to_filter   = _parse_leave_date(request.args.get('date_to'))

    q = (LeaveRequest.query
         .execution_options(bypass_tenant_scope=True, include_all_years=True)
         .filter_by(student_id=student.id, school_id=school_id))

    if status_filter and status_filter in LEAVE_STATUS:
        q = q.filter_by(status=status_filter)
    if type_filter and type_filter in LEAVE_TYPES:
        q = q.filter_by(leave_type=type_filter)
    if date_from_filter:
        q = q.filter(LeaveRequest.from_date >= date_from_filter)
    if date_to_filter:
        q = q.filter(LeaveRequest.to_date <= date_to_filter)

    archive = q.order_by(LeaveRequest.created_at.desc()).all()
    stats = {
        'total':    len(archive),
        'pending':  sum(1 for r in archive if r.status == 'pending'),
        'approved': sum(1 for r in archive if r.status == 'approved'),
        'rejected': sum(1 for r in archive if r.status == 'rejected'),
    }
    return render_template('admin/student_leave_archive.html',
                           student=student,
                           archive=archive,
                           stats=stats,
                           status_filter=status_filter,
                           type_filter=type_filter,
                           date_from_filter=date_from_filter,
                           date_to_filter=date_to_filter,
                           status_labels=LEAVE_STATUS,
                           type_labels=LEAVE_TYPES,
                           source_labels=LEAVE_SOURCE_LABELS)


@admin_bp.route('/employee-leave-requests/create', methods=['GET', 'POST'])
@login_required
@admin_required
def employee_leave_request_create_admin():
    """Admin creates a leave request on behalf of an employee."""
    school_id = _admin_scope_id()
    if not school_id:
        flash('لا يمكن تحديد المدرسة.', 'danger')
        return redirect(url_for('admin.employee_leave_requests_list'))

    active_year = get_active_year(school_id)
    employees = (Employee.query
                 .execution_options(bypass_tenant_scope=True)
                 .filter_by(school_id=school_id, status='active')
                 .order_by(Employee.full_name)
                 .all())

    if request.method == 'POST':
        employee_id = request.form.get('employee_id', type=int)
        leave_type  = request.form.get('leave_type', '').strip()
        from_date   = _parse_leave_date(request.form.get('from_date'))
        to_date     = _parse_leave_date(request.form.get('to_date'))
        reason      = request.form.get('reason', '').strip()
        details     = request.form.get('details', '').strip() or None
        status_val  = request.form.get('status', 'pending').strip()
        attachment, upload_error = _admin_save_leave_attachment(
            f'admin-emp-leave-{school_id}')

        errors = []
        employee = None
        if employee_id:
            employee = (Employee.query
                        .execution_options(bypass_tenant_scope=True)
                        .filter_by(id=employee_id, school_id=school_id)
                        .first())
            if not employee:
                errors.append('الموظف غير موجود أو لا ينتمي لهذه المدرسة.')
        else:
            errors.append('يرجى اختيار الموظف.')

        if leave_type not in LEAVE_TYPES:
            errors.append('نوع الإجازة غير صالح.')
        if not from_date:
            errors.append('تاريخ البداية مطلوب.')
        if not to_date:
            errors.append('تاريخ النهاية مطلوب.')
        if from_date and to_date and to_date < from_date:
            errors.append('تاريخ النهاية يجب أن يكون بعد تاريخ البداية.')
        if not reason:
            errors.append('السبب مطلوب.')
        if status_val not in LEAVE_STATUS:
            errors.append('حالة الطلب غير صالحة.')
        if upload_error:
            errors.append(upload_error)

        if errors:
            for err in errors:
                flash(err, 'danger')
            return render_template('admin/employee_leave_create_admin.html',
                                   employees=employees,
                                   type_labels=LEAVE_TYPES,
                                   status_labels=LEAVE_STATUS,
                                   form_data=request.form), 422

        leave_obj = EmployeeLeaveRequest(
            employee_id=employee.id,
            school_id=school_id,
            academic_year_id=active_year.id if active_year else None,
            leave_type=leave_type,
            from_date=from_date,
            to_date=to_date,
            reason=reason,
            details=details,
            attachment_path=attachment,
            status=status_val,
            source='admin',
            created_by_user_id=current_user.id,
        )
        if status_val in ('approved', 'rejected'):
            leave_obj.reviewed_by = current_user.id
            leave_obj.reviewed_at = datetime.utcnow()

        db.session.add(leave_obj)
        if employee.user_id:
            _notify_employee_user(
                employee.user_id,
                school_id,
                'طلب إجازة من الإدارة',
                'أضافت الإدارة طلب إجازة باسمك.',
                fcm_data={
                    'type':   'employee_leave_request_update',
                    'screen': 'leave_requests',
                },
            )
        db.session.commit()
        flash('تم إضافة طلب الإجازة للموظف بنجاح.', 'success')
        return redirect(url_for('admin.employee_leave_requests_list'))

    return render_template('admin/employee_leave_create_admin.html',
                           employees=employees,
                           type_labels=LEAVE_TYPES,
                           status_labels=LEAVE_STATUS,
                           form_data={})


@admin_bp.route('/employees/<int:employee_id>/leave-archive')
@login_required
@admin_required
def employee_leave_archive(employee_id):
    """Complete leave history for one employee — scoped by school."""
    school_id = _admin_scope_id()
    employee = (Employee.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(id=employee_id, school_id=school_id)
                .first_or_404())

    status_filter    = request.args.get('status', '').strip()
    type_filter      = request.args.get('leave_type', '').strip()
    date_from_filter = _parse_leave_date(request.args.get('date_from'))
    date_to_filter   = _parse_leave_date(request.args.get('date_to'))

    q = (EmployeeLeaveRequest.query
         .execution_options(bypass_tenant_scope=True)
         .filter_by(employee_id=employee.id, school_id=school_id))

    if status_filter and status_filter in LEAVE_STATUS:
        q = q.filter_by(status=status_filter)
    if type_filter and type_filter in LEAVE_TYPES:
        q = q.filter_by(leave_type=type_filter)
    if date_from_filter:
        q = q.filter(EmployeeLeaveRequest.from_date >= date_from_filter)
    if date_to_filter:
        q = q.filter(EmployeeLeaveRequest.to_date <= date_to_filter)

    archive = q.order_by(EmployeeLeaveRequest.created_at.desc()).all()
    stats = {
        'total':    len(archive),
        'pending':  sum(1 for r in archive if r.status == 'pending'),
        'approved': sum(1 for r in archive if r.status == 'approved'),
        'rejected': sum(1 for r in archive if r.status == 'rejected'),
    }
    return render_template('admin/employee_leave_archive.html',
                           employee=employee,
                           archive=archive,
                           stats=stats,
                           status_filter=status_filter,
                           type_filter=type_filter,
                           date_from_filter=date_from_filter,
                           date_to_filter=date_to_filter,
                           status_labels=LEAVE_STATUS,
                           type_labels=LEAVE_TYPES,
                           source_labels=LEAVE_SOURCE_LABELS)


# ── AJAX helpers for cascading student-leave dropdowns ─────────────────────────

@admin_bp.route('/api/leave/sections-for-grade')
@login_required
@admin_required
def api_leave_sections_for_grade():
    """Return sections for a grade (JSON) — scoped by school + active year."""
    school_id = _admin_scope_id()
    grade_id = request.args.get('grade_id', type=int)
    if not school_id or not grade_id:
        return jsonify([])
    active_year = get_active_year(school_id)
    if not active_year:
        return jsonify([])
    grade = (Grade.query
             .execution_options(include_all_years=True)
             .filter_by(id=grade_id, school_id=school_id,
                        academic_year_id=active_year.id)
             .first())
    if not grade:
        return jsonify([])
    sections = (Section.query
                .execution_options(include_all_years=True)
                .filter_by(grade_id=grade.id, school_id=school_id,
                           academic_year_id=active_year.id)
                .order_by(Section.name)
                .all())
    return jsonify([{'id': s.id, 'name': s.name} for s in sections])


@admin_bp.route('/api/leave/students-for-section')
@login_required
@admin_required
def api_leave_students_for_section():
    """Return students in a section (JSON) — scoped by school."""
    school_id = _admin_scope_id()
    section_id = request.args.get('section_id', type=int)
    if not school_id or not section_id:
        return jsonify([])
    section = (Section.query
               .execution_options(include_all_years=True)
               .filter_by(id=section_id, school_id=school_id)
               .first())
    if not section:
        return jsonify([])
    students = (Student.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(section_id=section.id, school_id=school_id)
                .order_by(Student.full_name)
                .all())
    return jsonify([
        {'id': s.id, 'name': s.full_name, 'code': s.student_id}
        for s in students
    ])


@admin_bp.route('/roles')
@login_required
@super_admin_required
def roles_list():
    roles = (Role.query
             .filter(Role.name != LEGACY_ADMIN_ROLE)
             .order_by(Role.id)
             .all())
    return render_template('admin/roles_list.html', roles=roles)


@admin_bp.route('/roles/create', methods=['GET', 'POST'])
@login_required
@super_admin_required
def create_role():
    all_permissions = Permission.query.order_by(Permission.category, Permission.name).all()
    if request.method == 'POST':
        name        = request.form.get('name', '').strip()
        label       = request.form.get('label', '').strip()
        description = request.form.get('description', '').strip()
        perm_ids    = request.form.getlist('permissions', type=int)
        protected_names = {SUPER_ADMIN_ROLE, SCHOOL_ADMIN_ROLE, LEGACY_ADMIN_ROLE}

        if not name or not label:
            flash('الاسم والتسمية مطلوبان.', 'danger')
            return render_template('admin/role_form.html',
                                   all_permissions=all_permissions)

        if name in protected_names:
            flash('This role name is reserved by the system.', 'danger')
            return render_template('admin/role_form.html',
                                   all_permissions=all_permissions)

        if Role.query.filter_by(name=name).first():
            flash('اسم الدور مستخدم بالفعل.', 'danger')
            return render_template('admin/role_form.html',
                                   all_permissions=all_permissions)

        role = Role(name=name, label=label, description=description)
        role.permissions = Permission.query.filter(Permission.id.in_(perm_ids)).all()
        db.session.add(role)
        db.session.commit()
        flash('تم إنشاء الدور بنجاح.', 'success')
        return redirect(url_for('admin.roles_list'))

    return render_template('admin/role_form.html',
                           all_permissions=all_permissions, role=None)


@admin_bp.route('/roles/<int:role_id>/edit', methods=['GET', 'POST'])
@login_required
@super_admin_required
def edit_role(role_id):
    role = Role.query.get_or_404(role_id)
    if role.name in {SUPER_ADMIN_ROLE, SCHOOL_ADMIN_ROLE, LEGACY_ADMIN_ROLE}:
        flash('System roles cannot be edited from role management.', 'warning')
        return redirect(url_for('admin.roles_list'))

    all_permissions = Permission.query.order_by(Permission.category, Permission.name).all()

    if request.method == 'POST':
        role.label       = request.form.get('label', role.label).strip()
        role.description = request.form.get('description', '').strip()
        perm_ids         = request.form.getlist('permissions', type=int)
        role.permissions = Permission.query.filter(Permission.id.in_(perm_ids)).all()
        db.session.commit()
        flash('تم تحديث الدور بنجاح.', 'success')
        return redirect(url_for('admin.roles_list'))

    return render_template('admin/role_form.html',
                           role=role, all_permissions=all_permissions)


# ─────────────────────────────────────────────────────────────────────────────
#  ACADEMIC YEARS  (redirects to per-school management on schools blueprint)
# ─────────────────────────────────────────────────────────────────────────────

@admin_bp.route('/academic-years')
@login_required
@admin_required
def academic_years():
    """
    For super admin → redirect to global schools list where years are managed.
    For school-bound admin → read-only list of their school's years.
    """
    school = get_current_school()
    if current_user.is_super_admin:
        # Super admin: go to global schools overview, years managed there
        return redirect(url_for('schools.index'))

    if not school:
        flash('لا توجد مدرسة محددة. تواصل مع مسؤول النظام.', 'warning')
        return redirect(url_for('admin.dashboard'))

    years = AcademicYear.query.execution_options(bypass_tenant_scope=True)\
                              .filter_by(school_id=school.id)\
                              .order_by(AcademicYear.start_date.desc()).all()
    return render_template('admin/academic_years.html', years=years, school=school)


@admin_bp.route('/switch-year', methods=['POST'])
@login_required
@staff_required
def switch_year():
    """Allow school staff to switch their data view to a different academic year."""
    if current_user.is_super_admin:
        return redirect(url_for('admin.dashboard'))

    school = get_current_school()
    if not school:
        return redirect(url_for('admin.dashboard'))

    year_id  = request.form.get('year_id', type=int)
    next_url = request.form.get('next', url_for('admin.dashboard'))

    from flask import session as _session
    if year_id:
        year = AcademicYear.query.execution_options(bypass_tenant_scope=True)\
            .filter_by(id=year_id, school_id=school.id).first()
        if year:
            _session['view_year_id'] = year_id
            flash(f'تم التبديل إلى العام الدراسي "{year.name}".', 'info')
        else:
            flash('العام الدراسي غير موجود.', 'danger')
    else:
        _session.pop('view_year_id', None)
        flash('تم التبديل إلى العام الدراسي الحالي.', 'info')

    return redirect(next_url)


@admin_bp.route('/academic-years/create', methods=['POST'])
@login_required
@super_admin_required
def create_academic_year():
    from datetime import datetime as dt_cls
    school = get_current_school()
    if not school:
        flash('لا توجد مدرسة محددة.', 'danger')
        return redirect(url_for('admin.academic_years'))

    name       = request.form.get('name', '').strip()
    start_date = request.form.get('start_date')
    end_date   = request.form.get('end_date')
    is_current = bool(request.form.get('is_current'))

    if not name or not start_date or not end_date:
        flash('جميع الحقول مطلوبة.', 'danger')
        return redirect(url_for('admin.academic_years'))

    existing = (AcademicYear.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=school.id, name=name)
                .first())
    if existing:
        flash(f'يوجد عام دراسي بنفس الاسم "{name}" مسجّل لهذه المدرسة بالفعل.', 'danger')
        return redirect(url_for('admin.academic_years'))

    if is_current:
        AcademicYear.query.filter_by(school_id=school.id).update({'is_current': False})

    year = AcademicYear(
        school_id  = school.id,
        name       = name,
        start_date = dt_cls.strptime(start_date, '%Y-%m-%d').date(),
        end_date   = dt_cls.strptime(end_date,   '%Y-%m-%d').date(),
        is_current = is_current,
    )
    db.session.add(year)
    db.session.commit()
    flash('تم إنشاء العام الدراسي بنجاح.', 'success')
    return redirect(url_for('admin.academic_years'))


@admin_bp.route('/academic-years/<int:year_id>/edit', methods=['POST'])
@login_required
@super_admin_required
def edit_academic_year(year_id):
    from datetime import datetime as dt_cls
    from app.utils.audit import log_action

    school = get_current_school()
    year   = AcademicYear.query.get_or_404(year_id)

    # Prevent school-bound admin from editing another school's year
    if school and year.school_id and year.school_id != school.id:
        flash('لا يمكنك تعديل عام دراسي لمدرسة أخرى.', 'danger')
        return redirect(url_for('admin.academic_years'))

    new_name   = request.form.get('name', '').strip()
    start_date = request.form.get('start_date')
    end_date   = request.form.get('end_date')
    is_current = bool(request.form.get('is_current'))

    if not new_name:
        flash('اسم العام الدراسي مطلوب.', 'danger')
        return redirect(url_for('admin.academic_years'))

    old_name = year.name
    year.name = new_name
    if start_date:
        try:
            year.start_date = dt_cls.strptime(start_date, '%Y-%m-%d').date()
        except ValueError:
            pass
    if end_date:
        try:
            year.end_date = dt_cls.strptime(end_date, '%Y-%m-%d').date()
        except ValueError:
            pass

    if is_current and not year.is_current:
        AcademicYear.query.filter_by(school_id=year.school_id).update({'is_current': False})
        year.is_current = True
    elif not is_current and year.is_current:
        year.is_current = False

    db.session.commit()
    log_action('edit', 'academic_year', year.id,
               details=f'renamed "{old_name}" → "{new_name}"')
    flash(f'تم تحديث العام الدراسي "{new_name}".', 'success')
    return redirect(url_for('admin.academic_years'))


@admin_bp.route('/academic-years/<int:year_id>/delete', methods=['POST'])
@login_required
@super_admin_required
def delete_academic_year(year_id):
    from app.utils.audit import log_action

    school = get_current_school()
    year   = AcademicYear.query.get_or_404(year_id)

    if school and year.school_id and year.school_id != school.id:
        flash('لا يمكنك حذف عام دراسي لمدرسة أخرى.', 'danger')
        return redirect(url_for('admin.academic_years'))

    if year.is_current:
        flash('لا يمكن حذف العام الدراسي الحالي.', 'danger')
        return redirect(url_for('admin.academic_years'))

    name = year.name
    db.session.delete(year)
    db.session.commit()
    log_action('delete', 'academic_year', year_id, details=f'deleted "{name}"')
    flash(f'تم حذف العام الدراسي "{name}".', 'success')
    return redirect(url_for('admin.academic_years'))


# ─────────────────────────────────────────────────────────────────────────────
#  SETTINGS PAGE
# ─────────────────────────────────────────────────────────────────────────────

@admin_bp.route('/settings')
@login_required
@super_admin_required
def settings():
    return render_template('admin/settings.html')


# ─────────────────────────────────────────────────────────────────────────────
#  SCHOOL SETTINGS  (now edits the School object, not SchoolSettings)
# ─────────────────────────────────────────────────────────────────────────────

@admin_bp.route('/school-settings', methods=['GET', 'POST'])
@login_required
@admin_required
def school_settings():
    from flask import current_app
    from app.utils.audit import log_action
    from app.utils.helpers import save_uploaded_file, LOGO_IMAGE_EXTENSIONS, LOGO_MAX_BYTES

    school = get_current_school()

    # Super admin without an active school → redirect to global schools management
    if school is None or not isinstance(school, School):
        flash('الرجاء اختيار مدرسة من لوحة التحكم الشاملة أولاً.', 'warning')
        return redirect(url_for('schools.index'))

    if request.method == 'POST':
        school.school_name     = request.form.get('school_name', school.school_name).strip() or school.school_name
        school.school_name_ar  = request.form.get('school_name_ar', '').strip() or None
        school.primary_color   = request.form.get('primary_color', '#0d6efd').strip()
        school.address         = request.form.get('address', '').strip() or None
        school.phone           = request.form.get('phone',   '').strip() or None
        school.email           = request.form.get('email',   '').strip() or None
        school.website         = request.form.get('website', '').strip() or None
        school.currency_code   = request.form.get('currency_code',   'IQD').strip() or 'IQD'
        school.currency_symbol = request.form.get('currency_symbol', 'د.ع').strip() or 'د.ع'
        school.timezone        = request.form.get('timezone', 'Asia/Baghdad').strip()
        school.locale          = request.form.get('locale',  'ar').strip() or 'ar'
        school.receipt_footer  = request.form.get('receipt_footer', '').strip() or None

        logo_file = request.files.get('logo')
        if logo_file and logo_file.filename:
            bucket = current_app.config.get('SUPABASE_STORAGE_BUCKET_MEDIA', 'school-media')
            result = save_uploaded_file(
                logo_file,
                subfolder=f'schools/{school.id}/identity',
                prefix='logo',
                bucket=bucket,
                allowed_exts=LOGO_IMAGE_EXTENSIONS,
                max_size=LOGO_MAX_BYTES,
            )
            if result:
                school.logo_path = result
            else:
                flash('فشل رفع الشعار. تأكد من أن الملف صورة صالحة (PNG/JPG/WEBP/SVG) ولا يتجاوز 2 ميغابايت.', 'warning')

        db.session.commit()
        log_action('edit', 'school', school.id, details='white-label identity updated')
        flash('تم حفظ هوية المدرسة بنجاح.', 'success')
        return redirect(url_for('admin.school_settings'))

    return render_template('admin/school_settings.html', settings=school)


# ─────────────────────────────────────────────────────────────────────────────
#  ATTENDANCE SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

@admin_bp.route('/attendance-settings', methods=['GET', 'POST'])
@login_required
def attendance_settings():
    from app.utils.audit import log_action

    is_admin = current_user.is_super_admin or current_user.is_school_admin
    has_perm = current_user.has_permission('manage_attendance_settings')
    if not is_admin and not has_perm:
        flash('ليس لديك صلاحية الوصول إلى إعدادات الحضور.', 'danger')
        if current_user.role and current_user.role.name == 'teacher':
            return redirect(url_for('teacher.dashboard'))
        if current_user.role and current_user.role.name == 'parent':
            return redirect(url_for('parent.dashboard'))
        return redirect(url_for('attendance.index'))

    school = get_current_school()
    if school is None or not isinstance(school, School):
        from app.models import SchoolSettings
        settings_row = SchoolSettings.get()
        is_school_obj = False
    else:
        settings_row = school
        is_school_obj = True

    if request.method == 'POST':
        from datetime import time as _time

        def _parse_time(s):
            if not s or not s.strip():
                return None
            try:
                h, m = map(int, s.strip().split(':')[:2])
                return _time(h, m)
            except (ValueError, AttributeError):
                return None

        settings_row.att_start_time        = _parse_time(request.form.get('att_start_time', ''))
        settings_row.att_late_threshold    = _parse_time(request.form.get('att_late_threshold', ''))
        settings_row.att_absence_threshold = _parse_time(request.form.get('att_absence_threshold', ''))
        settings_row.att_departure_time    = _parse_time(request.form.get('att_departure_time', ''))

        # Employee absence limit settings (saved on School object only)
        if is_school_obj:
            raw_limit = request.form.get('emp_absence_limit', '').strip()
            settings_row.emp_absence_limit = int(raw_limit) if raw_limit.isdigit() and int(raw_limit) > 0 else None
            settings_row.emp_absence_period = request.form.get('emp_absence_period', 'monthly') or 'monthly'
            settings_row.emp_absence_alert_enabled = bool(request.form.get('emp_absence_alert_enabled'))
            # Attendance shifts feature toggle
            settings_row.enable_attendance_shifts = bool(request.form.get('enable_attendance_shifts'))

        db.session.commit()
        resource_id = school.id if is_school_obj else getattr(settings_row, 'id', None)
        log_action('edit', 'school_settings', resource_id,
                   details='attendance time thresholds updated')
        flash('تم حفظ إعدادات الحضور بنجاح.', 'success')
        return redirect(url_for('admin.attendance_settings'))

    # Load shifts for the shift management section
    active_shifts = []
    inactive_shifts = []
    if is_school_obj and getattr(settings_row, 'enable_attendance_shifts', False):
        from app.models import AttendanceShift
        all_shifts = (AttendanceShift.query
                      .execution_options(bypass_tenant_scope=True)
                      .filter_by(school_id=school.id)
                      .order_by(AttendanceShift.start_time)
                      .all())
        active_shifts   = [s for s in all_shifts if s.is_active]
        inactive_shifts = [s for s in all_shifts if not s.is_active]
    elif is_school_obj:
        # Load all shifts regardless (so the list shows even after toggling off)
        from app.models import AttendanceShift
        all_shifts = (AttendanceShift.query
                      .execution_options(bypass_tenant_scope=True)
                      .filter_by(school_id=school.id)
                      .order_by(AttendanceShift.start_time)
                      .all())
        active_shifts   = [s for s in all_shifts if s.is_active]
        inactive_shifts = [s for s in all_shifts if not s.is_active]

    return render_template('admin/attendance_settings.html',
                           settings=settings_row,
                           is_school_obj=is_school_obj,
                           active_shifts=active_shifts,
                           inactive_shifts=inactive_shifts)


# ─────────────────────────────────────────────────────────────────────────────
#  PUSH NOTIFICATION TEST (admin / super_admin only)
# ─────────────────────────────────────────────────────────────────────────────

@admin_bp.route('/notifications/test-push', methods=['POST'])
@login_required
def test_push_notification():
    """
    Send a test FCM push to a specific user.

    Request JSON:
      { "user_id": 123, "title": "...", "body": "..." }

    Returns:
      { "ok": true, "fcm_enabled": true, "success_count": N, "fail_count": N }
    """
    role = _role_name(current_user.role)
    if role not in (SUPER_ADMIN_ROLE, SCHOOL_ADMIN_ROLE, LEGACY_ADMIN_ROLE):
        return jsonify({'ok': False, 'error': 'forbidden'}), 403

    payload = request.get_json(silent=True) or {}
    user_id = payload.get('user_id')
    title   = (payload.get('title') or 'اختبار الإشعار').strip()
    body    = (payload.get('body')  or 'هذا إشعار تجريبي من Mecha School').strip()

    if not user_id:
        return jsonify({'ok': False, 'error': 'user_id is required'}), 400

    from app.services.fcm_service import is_enabled, send_push_to_user

    if not is_enabled():
        return jsonify({
            'ok':          False,
            'fcm_enabled': False,
            'error':       'FCM is not configured — set FIREBASE_SERVICE_ACCOUNT_JSON or GOOGLE_APPLICATION_CREDENTIALS',
        }), 503

    success, fail = send_push_to_user(int(user_id), title, body, {'type': 'test'})
    return jsonify({
        'ok':           True,
        'fcm_enabled':  True,
        'success_count': success,
        'fail_count':    fail,
    })


# ═════════════════════════════════════════════════════════════════════════════
#  SCHOOL BOARD — Videos and Announcements
# ═════════════════════════════════════════════════════════════════════════════

_sb_log = logging.getLogger(__name__)

BOARD_AUDIENCES = {
    'all':      'الجميع',
    'parents':  'أولياء الأمور فقط',
    'teachers': 'المعلمون فقط',
}

MEDIA_TYPES = {
    'none':  'لا يوجد',
    'image': 'صورة',
}

VIDEO_MEDIA_TYPES = {
    'video': 'فيديو',
    'image': 'صورة',
}


def _parse_dt(value):
    """Parse a datetime-local form field; return datetime or None."""
    if not value:
        return None
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


# ── Debug route (temporary — remove after root cause is confirmed) ─────────────

@admin_bp.route('/debug/school-board')
@login_required
@admin_required
def debug_school_board():
    from flask import session as _sess, current_app, abort
    # Diagnostic-only endpoint: never expose its internal scope dump in
    # production. Available only when the app runs in debug/development.
    if not current_app.debug:
        abort(404)
    try:
        scope_id = _admin_scope_id()
    except Exception as e:
        scope_id = f'ERROR: {e}'

    ann_count = vid_count = 'error'
    try:
        ann_count = SchoolAnnouncement.query.execution_options(
            bypass_tenant_scope=True).count()
        vid_count = SchoolVideo.query.execution_options(
            bypass_tenant_scope=True).count()
    except Exception as e:
        ann_count = vid_count = str(e)

    return jsonify({
        'user_id':          current_user.id,
        'role':             current_user.role.name if current_user.role else None,
        'school_id':        current_user.school_id,
        'is_school_admin':  current_user.is_school_admin,
        'is_super_admin':   current_user.is_super_admin,
        'active_school_id': _sess.get('active_school_id'),
        '_admin_scope_id':  scope_id,
        'announcements_count': ann_count,
        'videos_count':     vid_count,
    })


# ── School Videos ─────────────────────────────────────────────────────────────

@admin_bp.route('/school-board/videos')
@login_required
@admin_required
def school_board_videos():
    _sb_log.info('school_board_videos | user=%s role=%s school_id=%s',
                 current_user.id,
                 current_user.role.name if current_user.role else None,
                 current_user.school_id)
    school_id = _admin_scope_id()
    try:
        query = SchoolVideo.query.execution_options(bypass_tenant_scope=True)
        if school_id:
            query = query.filter_by(school_id=school_id)
        videos = query.order_by(SchoolVideo.created_at.desc()).all()
    except Exception as exc:
        _sb_log.error('school_board_videos query failed: %s', exc, exc_info=True)
        db.session.rollback()
        flash(
            'تعذّر تحميل الفيديوهات. إذا كان هذا أول ظهور للخاصية، '
            'تأكّد من تطبيق آخر migration على قاعدة البيانات (flask db upgrade).',
            'danger',
        )
        videos = []
    return render_template('admin/school_board_videos.html',
                           videos=videos,
                           audience_labels=BOARD_AUDIENCES)


@admin_bp.route('/school-board/videos/create', methods=['GET', 'POST'])
@login_required
@admin_required
def school_board_video_create():
    _sb_log.info('school_board_video_create | user=%s role=%s school_id=%s method=%s',
                 current_user.id,
                 current_user.role.name if current_user.role else None,
                 current_user.school_id, request.method)
    school_id = _admin_scope_id()
    if not school_id:
        flash('لا يمكن إنشاء فيديو بدون مدرسة محددة.', 'danger')
        return redirect(url_for('admin.school_board_videos'))

    if request.method == 'POST':
        from app.utils.helpers import (save_uploaded_file,
                                       ALLOWED_BOARD_IMAGE_EXTENSIONS,
                                       ALLOWED_BOARD_VIDEO_EXTENSIONS,
                                       BOARD_IMAGE_MAX_BYTES, BOARD_VIDEO_MAX_BYTES)

        title         = request.form.get('title', '').strip()
        description   = request.form.get('description', '').strip() or None
        media_type    = request.form.get('media_type', 'video').strip()
        fallback_url  = request.form.get('video_url', '').strip()
        thumbnail_url = request.form.get('thumbnail_url', '').strip() or None
        audience      = request.form.get('audience', 'all').strip()
        is_featured   = 'is_featured' in request.form
        is_active     = 'is_active'   in request.form
        publish_at    = _parse_dt(request.form.get('publish_at', ''))
        expires_at    = _parse_dt(request.form.get('expires_at', ''))

        if media_type not in VIDEO_MEDIA_TYPES:
            media_type = 'video'
        is_image    = media_type == 'image'
        allowed_ext = ALLOWED_BOARD_IMAGE_EXTENSIONS if is_image else ALLOWED_BOARD_VIDEO_EXTENSIONS
        max_bytes   = BOARD_IMAGE_MAX_BYTES           if is_image else BOARD_VIDEO_MAX_BYTES
        type_label  = 'الصورة' if is_image else 'الفيديو'

        final_url = None
        upload_file = request.files.get('media_file')

        if upload_file and upload_file.filename:
            ext = (upload_file.filename.rsplit('.', 1)[-1].lower()
                   if '.' in upload_file.filename else '')
            if ext not in allowed_ext:
                flash(f'نوع الملف غير مدعوم. الأنواع المقبولة: {", ".join(sorted(allowed_ext))}.', 'danger')
                return render_template('admin/school_board_video_form.html',
                                       video=None, audience_labels=BOARD_AUDIENCES,
                                       media_labels=VIDEO_MEDIA_TYPES, form_data=request.form)
            raw = upload_file.read()
            if len(raw) > max_bytes:
                flash(f'حجم {type_label} أكبر من الحد المسموح ({max_bytes // (1024 * 1024)} MB).', 'danger')
                return render_template('admin/school_board_video_form.html',
                                       video=None, audience_labels=BOARD_AUDIENCES,
                                       media_labels=VIDEO_MEDIA_TYPES, form_data=request.form)
            upload_file.stream.seek(0)
            result = save_uploaded_file(
                upload_file,
                subfolder=f'schools/{school_id}/board/media',
                bucket='school-media',
                allowed_exts=allowed_ext,
                max_size=max_bytes,
            )
            if not result:
                flash('فشل رفع الملف إلى التخزين. تحقق من الإعدادات أو حاول مجدداً.', 'danger')
                return render_template('admin/school_board_video_form.html',
                                       video=None, audience_labels=BOARD_AUDIENCES,
                                       media_labels=VIDEO_MEDIA_TYPES, form_data=request.form)
            final_url = result
        elif fallback_url and fallback_url.startswith(('http://', 'https://')):
            final_url = fallback_url

        errors = []
        if not title:
            errors.append('العنوان مطلوب.')
        if not final_url:
            errors.append('يرجى رفع ملف أو إدخال رابط خارجي.')
        if audience not in BOARD_AUDIENCES:
            errors.append('الجمهور المستهدف غير صالح.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('admin/school_board_video_form.html',
                                   video=None, audience_labels=BOARD_AUDIENCES,
                                   media_labels=VIDEO_MEDIA_TYPES, form_data=request.form)

        try:
            db.session.add(SchoolVideo(
                school_id=school_id, title=title, description=description,
                media_type=media_type, video_url=final_url,
                thumbnail_url=thumbnail_url,
                audience=audience, is_featured=is_featured, is_active=is_active,
                publish_at=publish_at, expires_at=expires_at,
                created_by=current_user.id,
            ))
            db.session.commit()
        except Exception as exc:
            _sb_log.error('school_board_video_create commit failed: %s', exc, exc_info=True)
            db.session.rollback()
            flash('حدث خطأ أثناء الحفظ — تأكّد من تطبيق migration على قاعدة البيانات.', 'danger')
            return render_template('admin/school_board_video_form.html',
                                   video=None, audience_labels=BOARD_AUDIENCES,
                                   media_labels=VIDEO_MEDIA_TYPES, form_data=request.form)
        flash('تمت الإضافة بنجاح.', 'success')
        return redirect(url_for('admin.school_board_videos'))

    return render_template('admin/school_board_video_form.html',
                           video=None, audience_labels=BOARD_AUDIENCES,
                           media_labels=VIDEO_MEDIA_TYPES, form_data={})


@admin_bp.route('/school-board/videos/<int:video_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def school_board_video_edit(video_id):
    school_id = _admin_scope_id()
    q = SchoolVideo.query.execution_options(bypass_tenant_scope=True).filter_by(id=video_id)
    if school_id:
        q = q.filter_by(school_id=school_id)
    video = q.first_or_404()

    if request.method == 'POST':
        from app.utils.helpers import (save_uploaded_file,
                                       ALLOWED_BOARD_IMAGE_EXTENSIONS,
                                       ALLOWED_BOARD_VIDEO_EXTENSIONS,
                                       BOARD_IMAGE_MAX_BYTES, BOARD_VIDEO_MAX_BYTES)

        title         = request.form.get('title', '').strip()
        description   = request.form.get('description', '').strip() or None
        media_type    = request.form.get('media_type', video.media_type).strip()
        fallback_url  = request.form.get('video_url', '').strip()
        thumbnail_url = request.form.get('thumbnail_url', '').strip() or None
        audience      = request.form.get('audience', 'all').strip()
        is_featured   = 'is_featured' in request.form
        is_active     = 'is_active'   in request.form
        publish_at    = _parse_dt(request.form.get('publish_at', ''))
        expires_at    = _parse_dt(request.form.get('expires_at', ''))

        if media_type not in VIDEO_MEDIA_TYPES:
            media_type = video.media_type
        is_image    = media_type == 'image'
        allowed_ext = ALLOWED_BOARD_IMAGE_EXTENSIONS if is_image else ALLOWED_BOARD_VIDEO_EXTENSIONS
        max_bytes   = BOARD_IMAGE_MAX_BYTES           if is_image else BOARD_VIDEO_MAX_BYTES
        type_label  = 'الصورة' if is_image else 'الفيديو'

        final_url   = video.video_url  # keep existing unless replaced
        upload_file = request.files.get('media_file')

        if upload_file and upload_file.filename:
            ext = (upload_file.filename.rsplit('.', 1)[-1].lower()
                   if '.' in upload_file.filename else '')
            if ext not in allowed_ext:
                flash(f'نوع الملف غير مدعوم. الأنواع المقبولة: {", ".join(sorted(allowed_ext))}.', 'danger')
                return render_template('admin/school_board_video_form.html',
                                       video=video, audience_labels=BOARD_AUDIENCES,
                                       media_labels=VIDEO_MEDIA_TYPES, form_data=request.form)
            raw = upload_file.read()
            if len(raw) > max_bytes:
                flash(f'حجم {type_label} أكبر من الحد المسموح ({max_bytes // (1024 * 1024)} MB).', 'danger')
                return render_template('admin/school_board_video_form.html',
                                       video=video, audience_labels=BOARD_AUDIENCES,
                                       media_labels=VIDEO_MEDIA_TYPES, form_data=request.form)
            upload_file.stream.seek(0)
            result = save_uploaded_file(
                upload_file,
                subfolder=f'schools/{school_id}/board/media',
                bucket='school-media',
                allowed_exts=allowed_ext,
                max_size=max_bytes,
            )
            if not result:
                flash('فشل رفع الملف إلى التخزين. تحقق من الإعدادات أو حاول مجدداً.', 'danger')
                return render_template('admin/school_board_video_form.html',
                                       video=video, audience_labels=BOARD_AUDIENCES,
                                       media_labels=VIDEO_MEDIA_TYPES, form_data=request.form)
            final_url = result
        elif fallback_url and fallback_url.startswith(('http://', 'https://')):
            final_url = fallback_url

        errors = []
        if not title:
            errors.append('العنوان مطلوب.')
        if not final_url:
            errors.append('يرجى رفع ملف أو إدخال رابط خارجي.')
        if audience not in BOARD_AUDIENCES:
            errors.append('الجمهور المستهدف غير صالح.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('admin/school_board_video_form.html',
                                   video=video, audience_labels=BOARD_AUDIENCES,
                                   media_labels=VIDEO_MEDIA_TYPES, form_data=request.form)

        video.title         = title
        video.description   = description
        video.media_type    = media_type
        video.video_url     = final_url
        video.thumbnail_url = thumbnail_url
        video.audience      = audience
        video.is_featured   = is_featured
        video.is_active     = is_active
        video.publish_at    = publish_at
        video.expires_at    = expires_at
        try:
            db.session.commit()
        except Exception as exc:
            _sb_log.error('school_board_video_edit commit failed: %s', exc, exc_info=True)
            db.session.rollback()
            flash('حدث خطأ أثناء الحفظ.', 'danger')
            return render_template('admin/school_board_video_form.html',
                                   video=video, audience_labels=BOARD_AUDIENCES,
                                   media_labels=VIDEO_MEDIA_TYPES, form_data=request.form)
        flash('تم التحديث بنجاح.', 'success')
        return redirect(url_for('admin.school_board_videos'))

    return render_template('admin/school_board_video_form.html',
                           video=video, audience_labels=BOARD_AUDIENCES,
                           media_labels=VIDEO_MEDIA_TYPES, form_data={})


@admin_bp.route('/school-board/videos/<int:video_id>/toggle', methods=['POST'])
@login_required
@admin_required
def school_board_video_toggle(video_id):
    school_id = _admin_scope_id()
    q = SchoolVideo.query.execution_options(bypass_tenant_scope=True).filter_by(id=video_id)
    if school_id:
        q = q.filter_by(school_id=school_id)
    video = q.first_or_404()
    try:
        video.is_active = not video.is_active
        db.session.commit()
    except Exception as exc:
        _sb_log.error('school_board_video_toggle failed: %s', exc, exc_info=True)
        db.session.rollback()
        flash('حدث خطأ أثناء تغيير الحالة.', 'danger')
        return redirect(url_for('admin.school_board_videos'))
    flash('تم تغيير حالة الفيديو إلى: {}.'.format('نشط' if video.is_active else 'معطّل'), 'success')
    return redirect(url_for('admin.school_board_videos'))


@admin_bp.route('/school-board/videos/<int:video_id>/delete', methods=['POST'])
@login_required
@admin_required
def school_board_video_delete(video_id):
    school_id = _admin_scope_id()
    q = SchoolVideo.query.execution_options(bypass_tenant_scope=True).filter_by(id=video_id)
    if school_id:
        q = q.filter_by(school_id=school_id)
    video = q.first_or_404()
    try:
        db.session.delete(video)
        db.session.commit()
    except Exception as exc:
        _sb_log.error('school_board_video_delete failed: %s', exc, exc_info=True)
        db.session.rollback()
        flash('حدث خطأ أثناء الحذف.', 'danger')
        return redirect(url_for('admin.school_board_videos'))
    flash('تم حذف الفيديو بنجاح.', 'success')
    return redirect(url_for('admin.school_board_videos'))


# ── School Announcements ───────────────────────────────────────────────────────

@admin_bp.route('/school-board/announcements')
@login_required
@admin_required
def school_board_announcements():
    _sb_log.info('school_board_announcements | user=%s role=%s school_id=%s',
                 current_user.id,
                 current_user.role.name if current_user.role else None,
                 current_user.school_id)
    school_id = _admin_scope_id()
    try:
        query = SchoolAnnouncement.query.execution_options(bypass_tenant_scope=True)
        if school_id:
            query = query.filter_by(school_id=school_id)
        announcements = query.order_by(SchoolAnnouncement.created_at.desc()).all()
    except Exception as exc:
        _sb_log.error('school_board_announcements query failed: %s', exc, exc_info=True)
        db.session.rollback()
        flash(
            'تعذّر تحميل الإعلانات. إذا كان هذا أول ظهور للخاصية، '
            'تأكّد من تطبيق آخر migration على قاعدة البيانات (flask db upgrade).',
            'danger',
        )
        announcements = []
    return render_template('admin/school_board_announcements.html',
                           announcements=announcements,
                           audience_labels=BOARD_AUDIENCES)


@admin_bp.route('/school-board/announcements/create', methods=['GET', 'POST'])
@login_required
@admin_required
def school_board_announcement_create():
    _sb_log.info('school_board_announcement_create | user=%s role=%s school_id=%s method=%s',
                 current_user.id,
                 current_user.role.name if current_user.role else None,
                 current_user.school_id, request.method)
    school_id = _admin_scope_id()
    if not school_id:
        flash('لا يمكن إنشاء إعلان بدون مدرسة محددة.', 'danger')
        return redirect(url_for('admin.school_board_announcements'))

    if request.method == 'POST':
        title         = request.form.get('title', '').strip()
        body          = request.form.get('body', '').strip()
        media_url     = request.form.get('media_url', '').strip() or None
        media_type    = request.form.get('media_type', 'none').strip()
        thumbnail_url = request.form.get('thumbnail_url', '').strip() or None
        audience      = request.form.get('audience', 'all').strip()
        is_featured   = 'is_featured' in request.form
        is_active     = 'is_active' in request.form
        publish_at    = _parse_dt(request.form.get('publish_at', ''))
        expires_at    = _parse_dt(request.form.get('expires_at', ''))

        errors = []
        if not title:
            errors.append('العنوان مطلوب.')
        if not body:
            errors.append('نص الإعلان مطلوب.')
        if audience not in BOARD_AUDIENCES:
            errors.append('الجمهور المستهدف غير صالح.')
        if media_type not in MEDIA_TYPES:
            errors.append('نوع الوسائط غير صالح.')
        if media_url and not media_url.startswith(('http://', 'https://')):
            errors.append('رابط الوسائط يجب أن يبدأ بـ http:// أو https://.')
        if thumbnail_url and not thumbnail_url.startswith(('http://', 'https://')):
            errors.append('رابط الصورة المصغرة يجب أن يبدأ بـ http:// أو https://.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('admin/school_board_announcement_form.html',
                                   announcement=None, audience_labels=BOARD_AUDIENCES,
                                   media_types=MEDIA_TYPES, form_data=request.form)

        try:
            db.session.add(SchoolAnnouncement(
                school_id=school_id, title=title, body=body,
                media_url=media_url, media_type=media_type, thumbnail_url=thumbnail_url,
                audience=audience, is_featured=is_featured, is_active=is_active,
                publish_at=publish_at, expires_at=expires_at,
                created_by=current_user.id,
            ))
            db.session.commit()
        except Exception as exc:
            _sb_log.error('school_board_announcement_create commit failed: %s', exc, exc_info=True)
            db.session.rollback()
            flash('حدث خطأ أثناء الحفظ — تأكّد من تطبيق migration على قاعدة البيانات.', 'danger')
            return render_template('admin/school_board_announcement_form.html',
                                   announcement=None, audience_labels=BOARD_AUDIENCES,
                                   media_types=MEDIA_TYPES, form_data=request.form)
        flash('تم إنشاء الإعلان بنجاح.', 'success')
        return redirect(url_for('admin.school_board_announcements'))

    return render_template('admin/school_board_announcement_form.html',
                           announcement=None, audience_labels=BOARD_AUDIENCES,
                           media_types=MEDIA_TYPES, form_data={})


@admin_bp.route('/school-board/announcements/<int:ann_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def school_board_announcement_edit(ann_id):
    school_id = _admin_scope_id()
    q = SchoolAnnouncement.query.execution_options(bypass_tenant_scope=True).filter_by(id=ann_id)
    if school_id:
        q = q.filter_by(school_id=school_id)
    ann = q.first_or_404()

    if request.method == 'POST':
        title         = request.form.get('title', '').strip()
        body          = request.form.get('body', '').strip()
        media_url     = request.form.get('media_url', '').strip() or None
        media_type    = request.form.get('media_type', 'none').strip()
        thumbnail_url = request.form.get('thumbnail_url', '').strip() or None
        audience      = request.form.get('audience', 'all').strip()
        is_featured   = 'is_featured' in request.form
        is_active     = 'is_active' in request.form
        publish_at    = _parse_dt(request.form.get('publish_at', ''))
        expires_at    = _parse_dt(request.form.get('expires_at', ''))

        errors = []
        if not title:
            errors.append('العنوان مطلوب.')
        if not body:
            errors.append('نص الإعلان مطلوب.')
        if audience not in BOARD_AUDIENCES:
            errors.append('الجمهور المستهدف غير صالح.')
        if media_type not in MEDIA_TYPES:
            errors.append('نوع الوسائط غير صالح.')
        if media_url and not media_url.startswith(('http://', 'https://')):
            errors.append('رابط الوسائط يجب أن يبدأ بـ http:// أو https://.')
        if thumbnail_url and not thumbnail_url.startswith(('http://', 'https://')):
            errors.append('رابط الصورة المصغرة يجب أن يبدأ بـ http:// أو https://.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('admin/school_board_announcement_form.html',
                                   announcement=ann, audience_labels=BOARD_AUDIENCES,
                                   media_types=MEDIA_TYPES, form_data=request.form)

        ann.title         = title
        ann.body          = body
        ann.media_url     = media_url
        ann.media_type    = media_type
        ann.thumbnail_url = thumbnail_url
        ann.audience      = audience
        ann.is_featured   = is_featured
        ann.is_active     = is_active
        ann.publish_at    = publish_at
        ann.expires_at    = expires_at
        try:
            db.session.commit()
        except Exception as exc:
            _sb_log.error('school_board_announcement_edit commit failed: %s', exc, exc_info=True)
            db.session.rollback()
            flash('حدث خطأ أثناء الحفظ.', 'danger')
            return render_template('admin/school_board_announcement_form.html',
                                   announcement=ann, audience_labels=BOARD_AUDIENCES,
                                   media_types=MEDIA_TYPES, form_data=request.form)
        flash('تم تحديث الإعلان بنجاح.', 'success')
        return redirect(url_for('admin.school_board_announcements'))

    return render_template('admin/school_board_announcement_form.html',
                           announcement=ann, audience_labels=BOARD_AUDIENCES,
                           media_types=MEDIA_TYPES, form_data={})


@admin_bp.route('/school-board/announcements/<int:ann_id>/toggle', methods=['POST'])
@login_required
@admin_required
def school_board_announcement_toggle(ann_id):
    school_id = _admin_scope_id()
    q = SchoolAnnouncement.query.execution_options(bypass_tenant_scope=True).filter_by(id=ann_id)
    if school_id:
        q = q.filter_by(school_id=school_id)
    ann = q.first_or_404()
    try:
        ann.is_active = not ann.is_active
        db.session.commit()
    except Exception as exc:
        _sb_log.error('school_board_announcement_toggle failed: %s', exc, exc_info=True)
        db.session.rollback()
        flash('حدث خطأ أثناء تغيير الحالة.', 'danger')
        return redirect(url_for('admin.school_board_announcements'))
    flash('تم تغيير حالة الإعلان إلى: {}.'.format('نشط' if ann.is_active else 'معطّل'), 'success')
    return redirect(url_for('admin.school_board_announcements'))


@admin_bp.route('/school-board/announcements/<int:ann_id>/delete', methods=['POST'])
@login_required
@admin_required
def school_board_announcement_delete(ann_id):
    school_id = _admin_scope_id()
    q = SchoolAnnouncement.query.execution_options(bypass_tenant_scope=True).filter_by(id=ann_id)
    if school_id:
        q = q.filter_by(school_id=school_id)
    ann = q.first_or_404()
    try:
        db.session.delete(ann)
        db.session.commit()
    except Exception as exc:
        _sb_log.error('school_board_announcement_delete failed: %s', exc, exc_info=True)
        db.session.rollback()
        flash('حدث خطأ أثناء الحذف.', 'danger')
        return redirect(url_for('admin.school_board_announcements'))
    flash('تم حذف الإعلان بنجاح.', 'success')
    return redirect(url_for('admin.school_board_announcements'))

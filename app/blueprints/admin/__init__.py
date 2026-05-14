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

from app.models import (db, User, Role, Permission, Employee, Student, Subject,
                         FeeInstallment, StudentAttendance, Revenue, Expense,
                         Notification, AcademicYear, School, Section,
                         teacher_subjects)
from app.utils.decorators import (admin_required, staff_required,
                                   get_current_school,
                                   get_active_year, get_view_year, super_admin_required)
from app.utils.helpers import generate_employee_id

admin_bp = Blueprint('admin', __name__, template_folder='../../templates/admin')

SUPER_ADMIN_ROLE = 'super_admin'
SCHOOL_ADMIN_ROLE = 'school_admin'
LEGACY_ADMIN_ROLE = 'admin'


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

    all_students = Student.query.filter_by(status='active')
    if school and is_school_manager:
        all_students = all_students.filter_by(school_id=school.id)
    all_students = all_students.order_by(Student.full_name).all()

    all_schools = (School.query.filter_by(is_active=True).order_by(School.id).all()
                   if current_user.is_super_admin else [])

    year = get_active_year(school.id) if school else None
    all_sections = []
    all_subjects = []
    if school and year:
        all_sections = (Section.query
                        .execution_options(bypass_tenant_scope=True)
                        .filter_by(school_id=school.id, academic_year_id=year.id)
                        .all())
        all_subjects = (Subject.query
                        .execution_options(bypass_tenant_scope=True)
                        .filter_by(school_id=school.id, academic_year_id=year.id)
                        .order_by(Subject.name)
                        .all())

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

        errors = []
        if not username: errors.append('اسم المستخدم مطلوب.')
        if User.query.execution_options(bypass_tenant_scope=True).filter_by(username=username).first():
            errors.append('اسم المستخدم مستخدم بالفعل.')
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

        if role_obj and role_obj.name == 'parent':
            student_ids = request.form.getlist('student_ids', type=int)
            if student_ids:
                # Validate students belong to the same school
                q = Student.query.filter(Student.id.in_(student_ids))
                if assigned_school_id:
                    q = q.filter(Student.school_id == assigned_school_id)
                user.children = q.all()

        elif role_obj and role_obj.name == 'teacher':
            # Auto-create Employee record for the teacher
            last_emp = (Employee.query
                        .execution_options(bypass_tenant_scope=True)
                        .order_by(Employee.id.desc()).first())
            emp = Employee(
                employee_id=generate_employee_id(last_emp.id if last_emp else 0),
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
            section_ids = request.form.getlist('teacher_section_ids', type=int)
            if section_ids and assigned_school_id:
                (Section.query
                 .execution_options(bypass_tenant_scope=True)
                 .filter(Section.id.in_(section_ids),
                         Section.school_id == assigned_school_id)
                 .update({'teacher_id': emp.id}, synchronize_session=False))

            # Assign teaching subjects × sections into teacher_subjects
            subject_ids = request.form.getlist('teacher_subject_ids', type=int)
            if subject_ids and section_ids and assigned_school_id:
                valid_subj_ids = [r[0] for r in
                    db.session.query(Subject.id)
                              .execution_options(bypass_tenant_scope=True)
                              .filter(Subject.id.in_(subject_ids),
                                      Subject.school_id == assigned_school_id)
                              .all()]
                valid_sec_ids = [r[0] for r in
                    db.session.query(Section.id)
                              .execution_options(bypass_tenant_scope=True)
                              .filter(Section.id.in_(section_ids),
                                      Section.school_id == assigned_school_id)
                              .all()]
                rows = [
                    {'employee_id': emp.id, 'subject_id': s, 'section_id': c}
                    for s in valid_subj_ids for c in valid_sec_ids
                ]
                if rows:
                    db.session.execute(teacher_subjects.insert(), rows)

        db.session.commit()
        flash(f'تم إنشاء المستخدم {username} بنجاح.', 'success')
        return redirect(url_for('admin.users_list'))

    preselect_school_id = request.args.get('school_id', type=int)
    return render_template('admin/user_form.html',
                           roles=roles, user=None,
                           all_permissions=all_permissions,
                           all_students=all_students, all_schools=all_schools,
                           all_sections=all_sections, all_subjects=all_subjects,
                           teacher_section_ids=set(), teacher_subject_ids=set(),
                           is_school_manager=is_school_manager,
                           safe_permissions=[],
                           preselect_school_id=preselect_school_id)


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
    all_students = Student.query.filter_by(status='active')
    if school and is_school_manager:
        all_students = all_students.filter_by(school_id=school.id)
    all_students = all_students.order_by(Student.full_name).all()
    all_schools = (School.query.filter_by(is_active=True).order_by(School.id).all()
                   if current_user.is_super_admin else [])

    # Sections and subjects for teacher role assignment
    year = get_active_year(school.id) if school else None
    all_sections = []
    all_subjects = []
    if school and year:
        all_sections = (Section.query
                        .execution_options(bypass_tenant_scope=True)
                        .filter_by(school_id=school.id, academic_year_id=year.id)
                        .all())
        all_subjects = (Subject.query
                        .execution_options(bypass_tenant_scope=True)
                        .filter_by(school_id=school.id, academic_year_id=year.id)
                        .order_by(Subject.name)
                        .all())

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
        new_email = request.form.get('email', '').strip()
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
            student_ids = request.form.getlist('student_ids', type=int)
            if student_ids:
                q = Student.query.filter(Student.id.in_(student_ids))
                if is_school_manager:
                    q = q.filter(Student.school_id == current_user.school_id)
                user.children = q.all()
            else:
                user.children = []

        elif new_role and new_role.name == 'teacher':
            import uuid as _uuid
            emp = (Employee.query
                   .execution_options(bypass_tenant_scope=True)
                   .filter_by(user_id=user.id).first())
            if not emp:
                last_emp = (Employee.query
                            .execution_options(bypass_tenant_scope=True)
                            .order_by(Employee.id.desc()).first())
                emp = Employee(
                    employee_id=generate_employee_id(last_emp.id if last_emp else 0),
                    full_name=user.full_name,
                    job_title='معلم',
                    school_id=user.school_id,
                    base_salary=0,
                    status='active',
                    user_id=user.id,
                )
                db.session.add(emp)
                db.session.flush()

            new_section_ids = request.form.getlist('teacher_section_ids', type=int)
            # Clear previous homeroom assignments for this teacher
            (Section.query
             .execution_options(bypass_tenant_scope=True)
             .filter_by(teacher_id=emp.id)
             .update({'teacher_id': None}, synchronize_session=False))
            if new_section_ids and user.school_id:
                (Section.query
                 .execution_options(bypass_tenant_scope=True)
                 .filter(Section.id.in_(new_section_ids),
                         Section.school_id == user.school_id)
                 .update({'teacher_id': emp.id}, synchronize_session=False))

            # Update subject assignments — clear then rebuild
            db.session.execute(
                teacher_subjects.delete().where(
                    teacher_subjects.c.employee_id == emp.id
                )
            )
            new_subject_ids = request.form.getlist('teacher_subject_ids', type=int)
            if new_subject_ids and new_section_ids and user.school_id:
                valid_subj_ids = [r[0] for r in
                    db.session.query(Subject.id)
                              .execution_options(bypass_tenant_scope=True)
                              .filter(Subject.id.in_(new_subject_ids),
                                      Subject.school_id == user.school_id)
                              .all()]
                valid_sec_ids = [r[0] for r in
                    db.session.query(Section.id)
                              .execution_options(bypass_tenant_scope=True)
                              .filter(Section.id.in_(new_section_ids),
                                      Section.school_id == user.school_id)
                              .all()]
                rows = [
                    {'employee_id': emp.id, 'subject_id': s, 'section_id': c}
                    for s in valid_subj_ids for c in valid_sec_ids
                ]
                if rows:
                    db.session.execute(teacher_subjects.insert(), rows)

        db.session.commit()
        flash('تم تحديث بيانات المستخدم بنجاح.', 'success')
        return redirect(url_for('admin.users_list'))

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
                           teacher_subject_ids=teacher_subject_ids)


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
    import os
    from werkzeug.utils import secure_filename
    from flask import current_app
    from app.utils.audit import log_action

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
            fname = secure_filename(
                f"school_{school.id}_logo_{int(datetime.utcnow().timestamp())}_{logo_file.filename}"
            )
            uploads_dir = os.path.join(current_app.root_path, 'static', 'uploads')
            os.makedirs(uploads_dir, exist_ok=True)
            logo_file.save(os.path.join(uploads_dir, fname))
            school.logo_path = fname

        fav_file = request.files.get('favicon')
        if fav_file and fav_file.filename:
            fname = secure_filename(
                f"school_{school.id}_fav_{int(datetime.utcnow().timestamp())}_{fav_file.filename}"
            )
            uploads_dir = os.path.join(current_app.root_path, 'static', 'uploads')
            os.makedirs(uploads_dir, exist_ok=True)
            fav_file.save(os.path.join(uploads_dir, fname))
            school.favicon_path = fname

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

        db.session.commit()
        resource_id = school.id if is_school_obj else getattr(settings_row, 'id', None)
        log_action('edit', 'school_settings', resource_id,
                   details='attendance time thresholds updated')
        flash('تم حفظ إعدادات الحضور بنجاح.', 'success')
        return redirect(url_for('admin.attendance_settings'))

    return render_template('admin/attendance_settings.html', settings=settings_row)

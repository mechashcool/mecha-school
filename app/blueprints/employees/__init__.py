"""Al-Muhandis – Employees Blueprint  (Phase 6: user account, teacher assignments)"""
import logging

from flask import (Blueprint, render_template, redirect, url_for, flash, request)
from flask_login import login_required, current_user
from datetime import datetime as dt, date

from app.models import (db, Employee, User, Role, teacher_subjects,
                        Subject, Section, Grade, DeviceEmployeeMapping,
                        EmployeeAttendance)
from app.utils.decorators import (permission_required, get_current_school,
                                   historical_guard, get_active_year, action_required)
from app.utils.helpers import save_uploaded_file
from app.utils import code_generator

_log = logging.getLogger(__name__)

employees_bp = Blueprint('employees', __name__,
                          template_folder='../../templates/employees')


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _available_roles():
    """Roles selectable for employee accounts based on current user's permissions."""
    excluded = {'super_admin', 'parent'}
    if not current_user.is_admin_user:
        excluded.add('school_admin')
    return Role.query.filter(Role.name.notin_(excluded)).order_by(Role.name).all()


def _form_context(employee=None):
    """Build the full context dict for the create/edit form."""
    school = get_current_school()
    year   = get_active_year(school.id) if school else None

    subjects  = (Subject.query.filter_by(academic_year_id=year.id)
                 .order_by(Subject.name).all() if year else [])
    grades    = (Grade.query.filter_by(academic_year_id=year.id)
                 .order_by(Grade.name).all() if year else [])
    grade_ids = [g.id for g in grades]
    sections  = (Section.query.filter(Section.grade_id.in_(grade_ids))
                 .order_by(Section.name).all() if grade_ids else [])
    grade_map = {g.id: g for g in grades}

    roles = _available_roles()

    existing_subject_ids    = []
    existing_section_ids    = []
    existing_homeroom_ids   = []
    linked_user             = None
    existing_device_mapping = None

    if employee:
        rows = db.session.execute(
            teacher_subjects.select().where(
                teacher_subjects.c.employee_id == employee.id
            )
        ).fetchall()
        existing_subject_ids = list({r.subject_id for r in rows})
        existing_section_ids = list({r.section_id for r in rows})

        existing_homeroom_ids = [
            s.id for s in Section.query.filter_by(teacher_id=employee.id).all()
        ]

        if employee.user_id:
            linked_user = (User.query
                           .execution_options(bypass_tenant_scope=True)
                           .get(employee.user_id))

        existing_device_mapping = (DeviceEmployeeMapping.query
                                   .filter_by(employee_id=employee.id, is_active=True)
                                   .first())

    return dict(
        employee                = employee,
        subjects                = subjects,
        grades                  = grades,
        grade_map               = grade_map,
        sections                = sections,
        roles                   = roles,
        existing_subject_ids    = existing_subject_ids,
        existing_section_ids    = existing_section_ids,
        existing_homeroom_ids   = existing_homeroom_ids,
        linked_user             = linked_user,
        existing_device_mapping = existing_device_mapping,
    )


def _save_teacher_assignments(emp):
    """
    Replace teacher assignments for this employee.
    Homeroom  → Section.teacher_id  (ORM-scoped to current year).
    Teaching  → teacher_subjects rows: delete-all then re-insert from form.
    """
    homeroom_section_ids = request.form.getlist('homeroom_section_ids', type=int)
    teaching_section_ids = request.form.getlist('teaching_section_ids', type=int)
    subject_ids          = request.form.getlist('subject_ids', type=int)

    Section.query.filter_by(teacher_id=emp.id).update(
        {'teacher_id': None}, synchronize_session=False)
    if homeroom_section_ids:
        Section.query.filter(Section.id.in_(homeroom_section_ids)).update(
            {'teacher_id': emp.id}, synchronize_session=False)

    db.session.execute(
        teacher_subjects.delete().where(
            teacher_subjects.c.employee_id == emp.id
        )
    )
    for section_id in set(teaching_section_ids):
        for subject_id in set(subject_ids):
            db.session.execute(teacher_subjects.insert().values(
                employee_id=emp.id,
                subject_id=subject_id,
                section_id=section_id,
            ))




# ─────────────────────────────────────────────────────────────────────────────
#  Shared POST handler
# ─────────────────────────────────────────────────────────────────────────────

def _handle_employee_post(employee):
    from app.utils.school_config import get_school_config
    school    = get_current_school()
    is_create = employee is None
    emp_cfg   = get_school_config(school.id if school else None)
    # Create uses the multi-step wizard; edit keeps the single form.
    _tmpl = 'employees/create_wizard.html' if is_create else 'employees/form.html'

    full_name = request.form.get('full_name', '').strip()
    email     = request.form.get('email', '').strip() or None

    # job_title: read from form only when visible; preserve existing value when hidden
    if emp_cfg.field_visible('employees', 'job_title'):
        job_title = request.form.get('job_title', '').strip() or None
    else:
        job_title = (employee.job_title if employee else None)

    if not full_name:
        flash('يرجى ملء حقل الاسم الكامل.', 'danger')
        return render_template(_tmpl, error_step='basic', **_form_context(employee))

    if emp_cfg.field_visible('employees', 'job_title') and emp_cfg.field_required('employees', 'job_title') and not job_title:
        flash('المسمى الوظيفي مطلوب.', 'danger')
        return render_template(_tmpl, error_step='basic', **_form_context(employee))

    if email:
        q = Employee.query.filter_by(email=email)
        if employee:
            q = q.filter(Employee.id != employee.id)
        if q.first():
            flash('عذراً، هذا البريد الإلكتروني مسجل مسبقاً.', 'danger')
            return render_template(_tmpl, error_step='basic', **_form_context(employee))

    hire_date = None
    hire_str  = request.form.get('hire_date', '').strip()
    if hire_str:
        try:
            hire_date = dt.strptime(hire_str, '%Y-%m-%d').date()
        except ValueError:
            flash('صيغة تاريخ التعيين غير صحيحة.', 'danger')
            return render_template(_tmpl, error_step='basic', **_form_context(employee))

    dob = None
    dob_str = request.form.get('date_of_birth', '').strip()
    if dob_str:
        try:
            dob = dt.strptime(dob_str, '%Y-%m-%d').date()
        except ValueError:
            pass

    salary_start = None
    sal_start_str = request.form.get('salary_start_date', '').strip()
    if sal_start_str:
        try:
            salary_start = dt.strptime(sal_start_str, '%Y-%m-%d').date()
        except ValueError:
            pass

    photo_path = None
    if 'photo' in request.files and request.files['photo'].filename:
        photo_path = save_uploaded_file(request.files['photo'], 'employees')

    if is_create:
        employee = Employee(
            employee_id   = code_generator.generate_employee_id(school.id),
            full_name     = full_name,
            job_title     = job_title,
            department    = request.form.get('department', '').strip(),
            gender        = request.form.get('gender', ''),
            date_of_birth = dob,
            nationality   = request.form.get('nationality', '').strip(),
            phone         = request.form.get('phone', '').strip(),
            email         = email,
            address       = request.form.get('address', '').strip(),
            base_salary   = float(request.form.get('base_salary', 0) or 0),
            hire_date     = hire_date,
            contract_type = request.form.get('contract_type', '').strip(),
            salary_type   = request.form.get('salary_type', 'monthly') or 'monthly',
            pay_method    = request.form.get('pay_method', '').strip() or None,
            bank_account  = request.form.get('bank_account', '').strip() or None,
            salary_start_date = salary_start,
            payroll_status = request.form.get('payroll_status', 'active') or 'active',
            photo         = photo_path,
            notes         = request.form.get('notes', '').strip(),
            school_id     = school.id if school else None,
        )
        import logging as _logging
        _log = _logging.getLogger(__name__)
        from sqlalchemy.exc import IntegrityError as _IntegrityError
        try:
            db.session.add(employee)
            db.session.flush()
        except _IntegrityError as _exc:
            db.session.rollback()
            _s = str(_exc).lower()
            _log.error('Employee flush IntegrityError: %s', str(_exc)[:800])
            _is_emp_id_conflict = (
                'uq_employee_school_employee_id' in _s
                or 'ix_employees_employee_id' in _s
                or ('employee_id' in _s and 'unique' in _s)
            )
            if _is_emp_id_conflict:
                flash('رقم الموظف مستخدم مسبقاً، يرجى المحاولة مرة أخرى', 'danger')
                return render_template(_tmpl, error_step='basic', **_form_context(None))
            raise
    else:
        employee.full_name     = full_name
        employee.job_title     = job_title if job_title is not None else employee.job_title
        employee.department    = request.form.get('department', '').strip()
        employee.gender        = request.form.get('gender', employee.gender)
        employee.date_of_birth = dob if dob else employee.date_of_birth
        employee.nationality   = request.form.get('nationality', '').strip()
        employee.phone         = request.form.get('phone', '').strip()
        employee.email         = email
        employee.address       = request.form.get('address', '').strip()
        employee.base_salary   = float(
            request.form.get('base_salary', employee.base_salary) or 0)
        employee.status        = request.form.get('status', employee.status)
        employee.contract_type = request.form.get('contract_type', '').strip()
        employee.salary_type   = request.form.get('salary_type', employee.salary_type) or 'monthly'
        employee.pay_method    = request.form.get('pay_method', '').strip() or None
        employee.bank_account  = request.form.get('bank_account', '').strip() or None
        employee.payroll_status = request.form.get('payroll_status', employee.payroll_status) or 'active'
        if salary_start:
            employee.salary_start_date = salary_start
        employee.notes         = request.form.get('notes', '').strip()
        if hire_date:
            employee.hire_date = hire_date
        if photo_path:
            employee.photo = photo_path

    db.session.commit()
    flash_msgs = [('success',
                   f'تم {"إضافة" if is_create else "تحديث"} بيانات الموظف {employee.full_name}.')]

    # ── Wizard documents (create only) ───────────────────────────────────────
    if is_create:
        from app.models import EmployeeDocument
        _ALLOWED_DOC_EXTS = {'pdf', 'jpg', 'jpeg', 'png', 'doc', 'docx'}
        doc_types  = request.form.getlist('doc_type[]')
        doc_files  = request.files.getlist('doc_file[]')
        doc_saved  = 0
        for i, f in enumerate(doc_files):
            if not f or not f.filename:
                continue
            doc_type = doc_types[i].strip() if i < len(doc_types) else ''
            _raw = f.filename.rsplit('/', 1)[-1].rsplit('\\', 1)[-1]
            title = doc_type or _raw.rsplit('.', 1)[0] or 'مستند'
            file_path = save_uploaded_file(
                f, 'employee_docs',
                allowed_exts=_ALLOWED_DOC_EXTS,
            )
            if not file_path:
                flash_msgs.append(('warning',
                    f'تعذّر رفع المستند "{title}" — تأكد من أن صيغة الملف مقبولة.'))
                continue
            db.session.add(EmployeeDocument(
                employee_id=employee.id,
                school_id=school.id if school else None,
                title=title,
                file_path=file_path,
                doc_type=doc_type or None,
            ))
            doc_saved += 1
        if doc_saved:
            db.session.commit()
            flash_msgs.append(('success', f'تم رفع {doc_saved} مستند(ات) بنجاح.'))

    # ── User account ──────────────────────────────────────────────────────────
    create_account = request.form.get('create_account')
    reset_password = request.form.get('reset_password')

    if create_account and not employee.user_id:
        username      = request.form.get('username', '').strip()
        raw_password  = request.form.get('user_password', '').strip()
        role_id       = request.form.get('role_id', type=int)
        user_is_active = bool(request.form.get('user_is_active'))

        # Auto-generate username when password is provided but username is left blank
        if raw_password and not username and role_id and school:
            _role_obj = Role.query.get(role_id)
            if _role_obj:
                username = code_generator.generate_username(school.id, _role_obj.name)

        acct_error = None
        if not username:
            acct_error = 'يرجى إدخال اسم المستخدم أو كلمة المرور لتوليده تلقائياً.'
        elif not raw_password:
            acct_error = 'يرجى إدخال كلمة المرور.'
        elif not role_id:
            acct_error = 'يرجى اختيار الدور الوظيفي للحساب.'
        elif User.query.filter_by(username=username).first():
            acct_error = 'اسم المستخدم مستخدم بالفعل — اختر اسماً آخر.'

        if acct_error:
            flash_msgs.append(('warning', acct_error))
        else:
            user = User(username=username, full_name=employee.full_name,
                        role_id=role_id,
                        school_id=school.id if school else None,
                        is_active=user_is_active, password_hash='')
            user.set_password(raw_password)
            db.session.add(user)
            db.session.flush()
            employee.user_id = user.id
            db.session.commit()
            flash_msgs.append(('success', 'تم إنشاء حساب النظام للموظف.'))

    elif employee.user_id:
        linked_user = (User.query
                       .execution_options(bypass_tenant_scope=True)
                       .get(employee.user_id))
        if linked_user:
            changed    = False
            new_role   = request.form.get('role_id', type=int)
            user_active = request.form.get('user_is_active')

            if new_role and new_role != linked_user.role_id:
                linked_user.role_id = new_role
                changed = True
            if user_active is not None:
                linked_user.is_active = bool(user_active)
                changed = True
            if reset_password:
                new_pw = request.form.get('user_password', '').strip()
                if new_pw:
                    linked_user.set_password(new_pw)
                    changed = True
                    flash_msgs.append(('success', 'تم تغيير كلمة مرور الحساب.'))
            if changed:
                db.session.commit()

    # ── Teacher assignments ───────────────────────────────────────────────────
    if request.form.get('save_teacher_section'):
        try:
            _save_teacher_assignments(employee)
            db.session.commit()
            flash_msgs.append(('success', 'تم ربط الموظف بالمواد والصفوف والشعب.'))
        except Exception:
            db.session.rollback()
            _log.exception('Teacher assignment save failed employee_id=%s', employee.id)
            flash_msgs.append(('warning',
                               'خطأ في حفظ تكليفات التدريسي — يرجى المحاولة مرة أخرى.'))

    for level, msg in flash_msgs:
        flash(msg, level)

    return redirect(url_for('employees.view', emp_id=employee.id))


# ─────────────────────────────────────────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────────────────────────────────────────

@employees_bp.route('/')
@login_required
@permission_required('manage_employees')
def index():
    page   = request.args.get('page', 1, type=int)
    search = request.args.get('q', '')
    school = get_current_school()
    query  = Employee.query
    if school:
        query = query.filter_by(school_id=school.id)
    if search:
        query = query.filter(
            db.or_(
                Employee.full_name.ilike(f'%{search}%'),
                Employee.employee_id.ilike(f'%{search}%'),
            )
        )
    employees = (query.order_by(Employee.created_at.desc())
                 .paginate(page=page, per_page=20, error_out=False))
    return render_template('employees/index.html',
                           employees=employees, search=search)


@employees_bp.route('/create', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_employees')
@action_required('employees', 'create')
def create():
    if request.method == 'POST':
        return _handle_employee_post(None)
    return render_template('employees/create_wizard.html', **_form_context())


@employees_bp.route('/<int:emp_id>')
@login_required
@permission_required('manage_employees')
def view(emp_id):
    employee = Employee.query.get_or_404(emp_id)
    linked_user = None
    if employee.user_id:
        linked_user = (User.query
                       .execution_options(bypass_tenant_scope=True)
                       .get(employee.user_id))
    device_mapping = (DeviceEmployeeMapping.query
                      .filter_by(employee_id=emp_id, is_active=True)
                      .first())
    return render_template('employees/view.html',
                           employee=employee,
                           linked_user=linked_user,
                           device_mapping=device_mapping)


@employees_bp.route('/<int:emp_id>/edit', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_employees')
def edit(emp_id):
    employee = Employee.query.get_or_404(emp_id)
    if request.method == 'POST':
        return _handle_employee_post(employee)
    return render_template('employees/form.html', **_form_context(employee))


@employees_bp.route('/<int:emp_id>/sync-to-device', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_employees')
def sync_to_device(emp_id):
    """Redirect to the device's mappings page where sync is managed."""
    mapping = (DeviceEmployeeMapping.query
               .filter_by(employee_id=emp_id, is_active=True).first())
    if mapping:
        return redirect(url_for('attendance_devices.mappings',
                                device_id=mapping.device_id))
    flash('لا يوجد ربط بجهاز حضور لهذا الموظف — أضفه من صفحة أجهزة الحضور.', 'info')
    return redirect(url_for('employees.view', emp_id=emp_id))


@employees_bp.route('/<int:emp_id>/documents', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_employees')
def documents(emp_id):
    from app.models import EmployeeDocument
    employee = Employee.query.get_or_404(emp_id)
    if request.method == 'POST':
        title     = request.form.get('title', '').strip()
        doc_type  = request.form.get('doc_type', '').strip()
        file_path = None
        if 'file' in request.files and request.files['file'].filename:
            file_path = save_uploaded_file(
                request.files['file'], 'employee_docs',
                allowed_exts={'pdf', 'jpg', 'jpeg', 'png', 'doc', 'docx'},
            )
        if title and file_path:
            doc = EmployeeDocument(
                employee_id=emp_id, title=title,
                file_path=file_path, doc_type=doc_type)
            db.session.add(doc)
            db.session.commit()
            flash('تم رفع المستند.', 'success')
        else:
            flash('يرجى إدخال العنوان واختيار ملف.', 'danger')
        return redirect(url_for('employees.documents', emp_id=emp_id))
    docs = (EmployeeDocument.query
            .filter_by(employee_id=emp_id)
            .order_by(EmployeeDocument.uploaded_at.desc()).all())
    return render_template('employees/documents.html',
                           employee=employee, docs=docs)


@employees_bp.route('/documents/<int:doc_id>/delete', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_employees')
def delete_document(doc_id):
    from app.models import EmployeeDocument
    doc    = EmployeeDocument.query.get_or_404(doc_id)
    emp_id = doc.employee_id
    db.session.delete(doc)
    db.session.commit()
    flash('تم حذف المستند.', 'success')
    return redirect(url_for('employees.documents', emp_id=emp_id))


# ─────────────────────────────────────────────────────────────────────────────
#  Employee Attendance Report  (professional per-employee summary + detail)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_date_arg(arg_name, fallback):
    """Parse a YYYY-MM-DD query-string param; return fallback date on failure."""
    raw = request.args.get(arg_name, '').strip()
    if raw:
        try:
            return dt.strptime(raw, '%Y-%m-%d').date(), raw
        except ValueError:
            pass
    return fallback, fallback.isoformat()


def _attendance_filters():
    """Read shared filter args from query string. Returns a dict."""
    today = date.today()
    date_from, date_from_str = _parse_date_arg('date_from', today.replace(day=1))
    date_to,   date_to_str   = _parse_date_arg('date_to',   today)
    return {
        'date_from':     date_from,
        'date_to':       date_to,
        'date_from_str': date_from_str,
        'date_to_str':   date_to_str,
        'employee_id':   request.args.get('employee_id', type=int),
        'department':    request.args.get('department', '').strip(),
        'status_filter': request.args.get('status', '').strip(),
        'name_search':   request.args.get('q', '').strip(),
    }


def _all_employees(school):
    return (Employee.query
            .filter_by(school_id=school.id, status='active')
            .order_by(Employee.full_name)
            .all())


# ── Main report (per-employee summary) ───────────────────────────────────────

@employees_bp.route('/attendance-report')
@login_required
@permission_required('manage_employees')
@action_required('employee_attendance', 'view_report')
def attendance_report():
    from app.utils.employee_attendance_helper import (
        get_employees_attendance_summary, get_absence_alerts,
        get_working_days,
    )

    school = get_current_school()
    f = _attendance_filters()
    employees = _all_employees(school)
    departments = sorted({e.department for e in employees if e.department})

    # If a single employee is selected via dropdown, keep only that one
    sel_emp_id = f['employee_id']
    emp_list = [e for e in employees if e.id == sel_emp_id] if sel_emp_id else employees

    rows = get_employees_attendance_summary(
        emp_list,
        f['date_from'], f['date_to'], school,
        name_search=f['name_search'],
        department=f['department'],
        status_filter=f['status_filter'],
    )

    alerts = get_absence_alerts(rows, school)
    working_days_count = len(get_working_days(f['date_from'], f['date_to'], school))

    # Aggregate summary totals across all rows
    total_present  = sum(r['present'] for r in rows)
    total_late     = sum(r['late']    for r in rows)
    total_absent   = sum(r['absent']  for r in rows)
    total_checkout = sum(r['checked_out'] for r in rows)

    return render_template(
        'employees/attendance_report.html',
        rows              = rows,
        alerts            = alerts,
        all_employees     = employees,
        departments       = departments,
        working_days_count= working_days_count,
        date_from         = f['date_from_str'],
        date_to           = f['date_to_str'],
        employee_id       = sel_emp_id,
        department        = f['department'],
        status_filter     = f['status_filter'],
        name_search       = f['name_search'],
        total_present     = total_present,
        total_late        = total_late,
        total_absent      = total_absent,
        total_checkout    = total_checkout,
        school            = school,
    )


# ── Per-employee detail (day-by-day breakdown) ────────────────────────────────

@employees_bp.route('/attendance-report/<int:emp_id>')
@login_required
@permission_required('manage_employees')
@action_required('employee_attendance', 'view_detail')
def attendance_report_detail(emp_id):
    from app.utils.employee_attendance_helper import (
        calculate_employee_stats, get_working_days,
    )
    from app.models import EmployeeAttendance

    school = get_current_school()
    emp = Employee.query.filter_by(id=emp_id, school_id=school.id).first_or_404()

    f = _attendance_filters()

    working_days = get_working_days(f['date_from'], f['date_to'], school)

    records = (EmployeeAttendance.query
               .execution_options(bypass_tenant_scope=True)
               .filter(
                   EmployeeAttendance.school_id == school.id,
                   EmployeeAttendance.employee_id == emp_id,
                   EmployeeAttendance.date >= f['date_from'],
                   EmployeeAttendance.date <= f['date_to'],
               ).all())

    records_by_date = {r.date: r for r in records}
    stats = calculate_employee_stats(emp, records_by_date, working_days)

    return render_template(
        'employees/attendance_report_detail.html',
        emp       = emp,
        stats     = stats,
        date_from = f['date_from_str'],
        date_to   = f['date_to_str'],
        school    = school,
    )


# ── Export all employees (Excel) ──────────────────────────────────────────────

@employees_bp.route('/attendance-report/export/excel')
@login_required
@permission_required('manage_employees')
@action_required('employee_attendance', 'export_excel')
def attendance_report_export_excel():
    from flask import Response
    from app.utils.employee_attendance_helper import get_employees_attendance_summary
    from app.utils.excel_export import export_employee_attendance

    school = get_current_school()
    f = _attendance_filters()
    employees = _all_employees(school)
    sel_emp_id = f['employee_id']
    emp_list = [e for e in employees if e.id == sel_emp_id] if sel_emp_id else employees

    rows = get_employees_attendance_summary(
        emp_list, f['date_from'], f['date_to'], school,
        name_search=f['name_search'],
        department=f['department'],
        status_filter=f['status_filter'],
    )

    data = export_employee_attendance(rows, f['date_from_str'], f['date_to_str'])
    if not data:
        flash('مكتبة Excel غير متاحة.', 'warning')
        return redirect(url_for('employees.attendance_report', **request.args))

    filename = f"employee_attendance_{f['date_from_str']}_{f['date_to_str']}.xlsx"
    return Response(
        data,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


# ── Export all employees (PDF) ────────────────────────────────────────────────

@employees_bp.route('/attendance-report/export/pdf')
@login_required
@permission_required('manage_employees')
@action_required('employee_attendance', 'export_pdf')
def attendance_report_export_pdf():
    from flask import Response
    from app.utils.employee_attendance_helper import get_employees_attendance_summary
    from app.utils.pdf_gen import generate_employee_attendance_pdf

    school = get_current_school()
    f = _attendance_filters()
    employees = _all_employees(school)
    sel_emp_id = f['employee_id']
    emp_list = [e for e in employees if e.id == sel_emp_id] if sel_emp_id else employees

    rows = get_employees_attendance_summary(
        emp_list, f['date_from'], f['date_to'], school,
        name_search=f['name_search'],
        department=f['department'],
        status_filter=f['status_filter'],
    )

    data = generate_employee_attendance_pdf(rows, f['date_from_str'], f['date_to_str'], school=school)
    if not data:
        flash('مكتبة PDF غير متاحة.', 'warning')
        return redirect(url_for('employees.attendance_report', **request.args))

    filename = f"employee_attendance_{f['date_from_str']}_{f['date_to_str']}.pdf"
    return Response(
        data,
        mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


# ── Export single employee (Excel) ────────────────────────────────────────────

@employees_bp.route('/attendance-report/<int:emp_id>/export/excel')
@login_required
@permission_required('manage_employees')
@action_required('employee_attendance', 'employee_excel')
def attendance_report_employee_excel(emp_id):
    from flask import Response
    from app.models import EmployeeAttendance
    from app.utils.employee_attendance_helper import calculate_employee_stats, get_working_days
    from app.utils.excel_export import export_single_employee_attendance

    school = get_current_school()
    emp = Employee.query.filter_by(id=emp_id, school_id=school.id).first_or_404()
    f = _attendance_filters()

    working_days = get_working_days(f['date_from'], f['date_to'], school)
    records = (EmployeeAttendance.query
               .execution_options(bypass_tenant_scope=True)
               .filter(EmployeeAttendance.school_id == school.id,
                       EmployeeAttendance.employee_id == emp_id,
                       EmployeeAttendance.date >= f['date_from'],
                       EmployeeAttendance.date <= f['date_to'])
               .all())
    stats = calculate_employee_stats(emp, {r.date: r for r in records}, working_days)

    data = export_single_employee_attendance(stats, f['date_from_str'], f['date_to_str'])
    if not data:
        flash('مكتبة Excel غير متاحة.', 'warning')
        return redirect(url_for('employees.attendance_report_detail', emp_id=emp_id, **request.args))

    filename = f"attendance_{emp.employee_id or emp_id}_{f['date_from_str']}.xlsx"
    return Response(
        data,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


# ── Export single employee (PDF) ──────────────────────────────────────────────

@employees_bp.route('/attendance-report/<int:emp_id>/export/pdf')
@login_required
@permission_required('manage_employees')
@action_required('employee_attendance', 'employee_pdf')
def attendance_report_employee_pdf(emp_id):
    from flask import Response
    from app.models import EmployeeAttendance
    from app.utils.employee_attendance_helper import calculate_employee_stats, get_working_days
    from app.utils.pdf_gen import generate_single_employee_attendance_pdf

    school = get_current_school()
    emp = Employee.query.filter_by(id=emp_id, school_id=school.id).first_or_404()
    f = _attendance_filters()

    working_days = get_working_days(f['date_from'], f['date_to'], school)
    records = (EmployeeAttendance.query
               .execution_options(bypass_tenant_scope=True)
               .filter(EmployeeAttendance.school_id == school.id,
                       EmployeeAttendance.employee_id == emp_id,
                       EmployeeAttendance.date >= f['date_from'],
                       EmployeeAttendance.date <= f['date_to'])
               .all())
    stats = calculate_employee_stats(emp, {r.date: r for r in records}, working_days)

    data = generate_single_employee_attendance_pdf(stats, f['date_from_str'], f['date_to_str'], school=school)
    if not data:
        flash('مكتبة PDF غير متاحة.', 'warning')
        return redirect(url_for('employees.attendance_report_detail', emp_id=emp_id, **request.args))

    filename = f"attendance_{emp.employee_id or emp_id}_{f['date_from_str']}.pdf"
    return Response(
        data,
        mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Employee Manual Attendance
# ─────────────────────────────────────────────────────────────────────────────

@employees_bp.route('/attendance/manual')
@login_required
@permission_required('manage_employees')
def manual_attendance():
    """Manual employee attendance entry page."""
    from app.utils.attendance_helpers import get_local_date

    school = get_current_school()
    today  = get_local_date(school)   # School carries att_* and timezone settings
    employees   = _all_employees(school)
    departments = sorted({e.department for e in employees if e.department})

    return render_template(
        'employees/manual_attendance.html',
        today       = today,
        departments = departments,
        school      = school,
    )


@employees_bp.route('/attendance/manual/list')
@login_required
@permission_required('manage_employees')
def manual_attendance_list():
    """AJAX: return employee list with existing attendance for a date."""
    from flask import jsonify
    from datetime import time as _time
    from app.utils.attendance_helpers import get_local_now

    school     = get_current_school()
    settings   = school
    date_str   = request.args.get('date', '')
    department = request.args.get('department', '').strip()

    local_now = get_local_now(settings)
    try:
        att_date = dt.strptime(date_str, '%Y-%m-%d').date() if date_str else local_now.date()
    except ValueError:
        att_date = local_now.date()

    # Departure-window check (mirrors student attendance logic)
    departure_time = getattr(settings, 'att_departure_time', None)
    if departure_time == _time(0, 0, 0):
        departure_time = None
    now_time    = local_now.time().replace(microsecond=0)
    is_departure = (
        att_date == local_now.date()
        and departure_time is not None
        and now_time >= departure_time
    )

    # Employees — explicitly scoped to current school
    q = (Employee.query
         .filter_by(school_id=school.id, status='active')
         .order_by(Employee.full_name))
    if department:
        q = q.filter(Employee.department == department)
    employees = q.all()

    emp_ids = [e.id for e in employees]

    # Fetch existing attendance for this date
    # bypass_tenant_scope + explicit school_id filter: same pattern as report routes
    existing: dict = {}
    if emp_ids:
        for a in (EmployeeAttendance.query
                  .execution_options(bypass_tenant_scope=True)
                  .filter(
                      EmployeeAttendance.school_id   == school.id,
                      EmployeeAttendance.employee_id.in_(emp_ids),
                      EmployeeAttendance.date        == att_date,
                  ).all()):
            existing[a.employee_id] = a

    result = []
    for emp in employees:
        rec = existing.get(emp.id)
        result.append({
            'id':          emp.id,
            'full_name':   emp.full_name,
            'employee_id': emp.employee_id or '',
            'department':  emp.department  or '',
            'job_title':   emp.job_title   or '',
            'existing': {
                'status':    rec.status,
                'check_in':  rec.check_in.strftime('%H:%M')  if rec.check_in  else '',
                'check_out': rec.check_out.strftime('%H:%M') if rec.check_out else '',
                'source':    rec.source or '',
                'notes':     rec.notes  or '',
            } if rec else None,
        })

    return jsonify({
        'employees':    result,
        'is_departure': is_departure,
        'total':        len(result),
    })


@employees_bp.route('/attendance/manual/save', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_employees')
def manual_attendance_save():
    """Create or update employee attendance records for a selected date."""
    from datetime import time as _time
    from app.utils.attendance_helpers import get_local_now, determine_check_in_status

    school = get_current_school()
    year   = get_active_year(school.id) if school else None

    if not school or not year:
        flash('لا توجد سنة دراسية نشطة.', 'danger')
        return redirect(url_for('employees.manual_attendance'))

    settings  = school
    date_str  = request.form.get('att_date', '').strip()
    att_dept  = request.form.get('att_dept', '').strip()
    local_now = get_local_now(settings)

    try:
        att_date = dt.strptime(date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        att_date = local_now.date()

    now_time = local_now.time().replace(microsecond=0)

    # Employee IDs come from hidden fields injected by JS
    raw_ids = request.form.getlist('emp_ids')
    try:
        emp_ids = [int(x) for x in raw_ids if str(x).isdigit()]
    except (ValueError, TypeError):
        emp_ids = []

    if not emp_ids:
        flash('لم يتم إرسال أي موظفين.', 'warning')
        return redirect(url_for('employees.manual_attendance', date=att_date.isoformat()))

    # Security: validate every submitted ID belongs to this school
    employees = (Employee.query
                 .filter(
                     Employee.id.in_(emp_ids),
                     Employee.school_id == school.id,
                     Employee.status    == 'active',
                 ).all())
    emp_map = {e.id: e for e in employees}
    valid_ids = list(emp_map.keys())

    # Fetch existing records for update (bypass ORM scope, explicit school filter)
    existing: dict = {}
    if valid_ids:
        for a in (EmployeeAttendance.query
                  .execution_options(bypass_tenant_scope=True)
                  .filter(
                      EmployeeAttendance.school_id   == school.id,
                      EmployeeAttendance.employee_id.in_(valid_ids),
                      EmployeeAttendance.date        == att_date,
                  ).all()):
            existing[a.employee_id] = a

    created = updated = 0

    for emp_id in emp_ids:
        if emp_id not in emp_map:
            continue  # Cross-school attempt — silently rejected

        status_choice = request.form.get(f'status_{emp_id}', 'absent').strip()
        if status_choice not in ('present', 'late', 'absent'):
            continue

        check_in_val  = None
        check_out_val = None
        notes_val     = request.form.get(f'notes_{emp_id}', '').strip() or None

        if status_choice in ('present', 'late'):
            ci_str = request.form.get(f'check_in_{emp_id}',  '').strip()
            co_str = request.form.get(f'check_out_{emp_id}', '').strip()

            if ci_str:
                try:
                    check_in_val = dt.strptime(ci_str, '%H:%M').time()
                except ValueError:
                    pass

            # Fall back to server time only for today (not for historical dates)
            if check_in_val is None and att_date == local_now.date():
                check_in_val = now_time

            # Auto-determine late vs present from check-in time
            if check_in_val and status_choice == 'present':
                status_choice = determine_check_in_status(check_in_val, settings)

            if co_str:
                try:
                    check_out_val = dt.strptime(co_str, '%H:%M').time()
                except ValueError:
                    pass

        rec = existing.get(emp_id)
        if rec:
            rec.status      = status_choice
            rec.check_in    = check_in_val
            rec.check_out   = check_out_val
            if notes_val is not None:
                rec.notes = notes_val
            rec.source      = 'manual'
            rec.recorded_by = current_user.id
            updated += 1
        else:
            db.session.add(EmployeeAttendance(
                employee_id      = emp_id,
                school_id        = school.id,
                academic_year_id = year.id,
                date             = att_date,
                status           = status_choice,
                check_in         = check_in_val,
                check_out        = check_out_val,
                notes            = notes_val,
                source           = 'manual',
                recorded_by      = current_user.id,
            ))
            created += 1

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        _log.exception('[emp-manual-att] commit failed date=%s school_id=%s', att_date, school.id)
        flash('حدث خطأ أثناء الحفظ. يرجى المحاولة مرة أخرى.', 'danger')
        return redirect(url_for('employees.manual_attendance', date=att_date.isoformat()))

    parts = []
    if created:
        parts.append(f'تم تسجيل {created} موظف')
    if updated:
        parts.append(f'تحديث {updated} سجل')
    flash(('، '.join(parts) or 'لم تطرأ أي تغييرات') + f' ليوم {att_date.isoformat()}.', 'success')

    redirect_kwargs = {'date': att_date.isoformat()}
    if att_dept:
        redirect_kwargs['department'] = att_dept
    return redirect(url_for('employees.manual_attendance', **redirect_kwargs))

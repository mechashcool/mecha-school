"""Al-Muhandis – Employees Blueprint"""
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required
from app.models import db, Employee
from app.utils.decorators import permission_required, get_current_school, historical_guard
from app.utils.helpers import save_uploaded_file, generate_employee_id

employees_bp = Blueprint('employees', __name__,
                          template_folder='../../templates/employees')


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
            Employee.full_name.ilike(f'%{search}%') |
            Employee.employee_id.ilike(f'%{search}%')
        )
    employees = query.order_by(Employee.created_at.desc())\
                     .paginate(page=page, per_page=20, error_out=False)
    return render_template('employees/index.html',
                           employees=employees, search=search)


@employees_bp.route('/create', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_employees')
def create():
    if request.method == 'POST':
        from datetime import datetime as dt
        
        # Form validation
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip() or None
        
        if not full_name:
            flash('يرجى ملء كافة الحقول المطلوبة.', 'danger')
            return render_template('employees/form.html', employee=None)
        
        # Check for duplicate email
        if email:
            existing = Employee.query.filter_by(email=email).first()
            if existing:
                flash('عذراً، هذا البريد الإلكتروني مسجل مسبقاً.', 'danger')
                return render_template('employees/form.html', employee=None)
        
        last_emp   = Employee.query.order_by(Employee.id.desc()).first()
        emp_id     = generate_employee_id(last_emp.id if last_emp else 0)
        photo_path = None
        if 'photo' in request.files:
            photo_path = save_uploaded_file(request.files['photo'], 'employees')

        hire_str = request.form.get('hire_date')
        hire_date = dt.strptime(hire_str, '%Y-%m-%d').date() if hire_str else None

        school = get_current_school()
        emp = Employee(
            employee_id   = emp_id,
            full_name     = full_name,
            job_title     = request.form.get('job_title', '').strip(),
            department    = request.form.get('department', '').strip(),
            gender        = request.form.get('gender', ''),
            nationality   = request.form.get('nationality', '').strip(),
            phone         = request.form.get('phone', '').strip(),
            email         = email,
            address       = request.form.get('address', '').strip(),
            base_salary   = float(request.form.get('base_salary', 0) or 0),
            hire_date     = hire_date,
            contract_type = request.form.get('contract_type', '').strip(),
            photo         = photo_path,
            notes         = request.form.get('notes', '').strip(),
            school_id     = school.id if school else None,
        )
        try:
            db.session.add(emp)
            db.session.commit()
            flash(f'تم إضافة الموظف {emp.full_name} برقم {emp.employee_id}.', 'success')
            return redirect(url_for('employees.index'))
        except Exception as e:
            db.session.rollback()
            flash('حدث خطأ غير متوقع، يرجى المحاولة مرة أخرى.', 'danger')
            return render_template('employees/form.html', employee=None)
    return render_template('employees/form.html', employee=None)


@employees_bp.route('/<int:emp_id>')
@login_required
@permission_required('manage_employees')
def view(emp_id):
    employee = Employee.query.get_or_404(emp_id)
    return render_template('employees/view.html', employee=employee)


@employees_bp.route('/<int:emp_id>/edit', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_employees')
def edit(emp_id):
    employee = Employee.query.get_or_404(emp_id)
    if request.method == 'POST':
        from datetime import datetime as dt
        
        # Form validation
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip() or None
        
        if not full_name:
            flash('يرجى ملء كافة الحقول المطلوبة.', 'danger')
            return render_template('employees/form.html', employee=employee)
        
        # Check for duplicate email (excluding current employee)
        if email and email != employee.email:
            existing = Employee.query.filter_by(email=email).first()
            if existing:
                flash('عذراً، هذا البريد الإلكتروني مسجل مسبقاً.', 'danger')
                return render_template('employees/form.html', employee=employee)
        
        employee.full_name     = full_name
        employee.job_title     = request.form.get('job_title', employee.job_title).strip()
        employee.department    = request.form.get('department', '').strip()
        employee.gender        = request.form.get('gender', employee.gender)
        employee.nationality   = request.form.get('nationality', '').strip()
        employee.phone         = request.form.get('phone', '').strip()
        employee.email         = email
        employee.address       = request.form.get('address', '').strip()
        employee.base_salary   = float(request.form.get('base_salary', employee.base_salary) or 0)
        employee.status        = request.form.get('status', employee.status)
        employee.contract_type = request.form.get('contract_type', '').strip()
        employee.notes         = request.form.get('notes', '').strip()

        hire_str = request.form.get('hire_date')
        if hire_str:
            try:
                employee.hire_date = dt.strptime(hire_str, '%Y-%m-%d').date()
            except ValueError:
                flash('صيغة تاريخ التوظيف غير صحيحة.', 'danger')
                return render_template('employees/form.html', employee=employee)
        
        try:
            db.session.commit()
            flash('تم تحديث بيانات الموظف.', 'success')
            return redirect(url_for('employees.view', emp_id=emp_id))
        except Exception as e:
            db.session.rollback()
            flash('حدث خطأ غير متوقع، يرجى المحاولة مرة أخرى.', 'danger')
            return render_template('employees/form.html', employee=employee)
    return render_template('employees/form.html', employee=employee)


@employees_bp.route('/<int:emp_id>/documents', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_employees')
def documents(emp_id):
    from app.models import EmployeeDocument
    employee = Employee.query.get_or_404(emp_id)
    if request.method == 'POST':
        title    = request.form.get('title', '').strip()
        doc_type = request.form.get('doc_type', '').strip()
        file_path = None
        if 'file' in request.files and request.files['file'].filename:
            file_path = save_uploaded_file(request.files['file'], 'employee_docs')
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
    docs = EmployeeDocument.query.filter_by(employee_id=emp_id)\
               .order_by(EmployeeDocument.uploaded_at.desc()).all()
    return render_template('employees/documents.html',
                           employee=employee, docs=docs)


@employees_bp.route('/documents/<int:doc_id>/delete', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_employees')
def delete_document(doc_id):
    from app.models import EmployeeDocument
    doc = EmployeeDocument.query.get_or_404(doc_id)
    emp_id = doc.employee_id
    db.session.delete(doc)
    db.session.commit()
    flash('تم حذف المستند.', 'success')
    return redirect(url_for('employees.documents', emp_id=emp_id))

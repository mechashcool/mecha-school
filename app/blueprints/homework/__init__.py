"""
Mecha-School — Homework Blueprint
===================================
Teacher-facing CRUD for homework assignments.

Routes
------
GET  /homework/                → list (current year, this teacher)
GET  /homework/create          → blank form
POST /homework/create          → save new assignment
GET  /homework/<id>/edit       → prefilled form
POST /homework/<id>/edit       → save changes
POST /homework/<id>/delete     → soft-delete (is_active=False) then DB delete

Scope rules
-----------
- Teacher: sees/creates only for their assigned sections and subjects.
- Admin (school_admin / super_admin): sees all homework for the school.
- Homework module must be enabled for the school (enforced by the global
  before_request hook in app/__init__.py via BLUEPRINT_MODULE).
"""
from datetime import date
from functools import wraps

from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, abort, current_app)
from flask_login import login_required, current_user
from sqlalchemy import select

from app.models import (db, Employee, Grade, Homework, Section, Subject,
                        AcademicYear, Notification, teacher_subjects)
from app.utils.decorators import get_current_school, get_active_year
from app.utils.helpers import save_uploaded_file, resolve_photo_url

homework_bp = Blueprint('homework', __name__,
                        template_folder='../../templates/homework')

# ── Allowed attachment extensions for homework ────────────────────────────────
_ATTACH_IMG = {'jpg', 'jpeg', 'png', 'webp'}
_ATTACH_PDF = {'pdf'}
_ATTACH_ALL = _ATTACH_IMG | _ATTACH_PDF


def _ext(filename: str) -> str:
    return filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''


def _attachment_type(filename: str) -> str:
    e = _ext(filename)
    if e in _ATTACH_IMG:
        return 'image'
    if e in _ATTACH_PDF:
        return 'pdf'
    return 'file'


# ── Access guard ──────────────────────────────────────────────────────────────

def homework_access_required(f):
    """Allow teachers AND admins; block everyone else."""
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.role:
            abort(403)
        role_name = current_user.role.name
        if not (current_user.is_admin_user or role_name == 'teacher'):
            flash('هذه الصفحة مخصصة للمعلمين والمسؤولين فقط.', 'warning')
            return redirect(url_for('admin.dashboard'))
        return f(*args, **kwargs)
    return wrapper


# ── Shared helpers ────────────────────────────────────────────────────────────

def _get_employee() -> Employee | None:
    return Employee.query.filter_by(user_id=current_user.id).first()


def _teacher_section_ids(emp: Employee) -> set[int]:
    homeroom = {s.id for s in emp.sections_managed}
    subject_secs = {
        row.section_id
        for row in db.session.execute(
            select(teacher_subjects.c.section_id).where(
                teacher_subjects.c.employee_id == emp.id
            )
        ).fetchall()
    }
    return homeroom | subject_secs


def _teacher_subject_ids(emp: Employee) -> set[int]:
    return {
        row.subject_id
        for row in db.session.execute(
            select(teacher_subjects.c.subject_id).where(
                teacher_subjects.c.employee_id == emp.id
            )
        ).fetchall()
    }


def _teacher_grades(sections: list) -> list:
    """Return Grade objects for the given sections, ordered by name."""
    grade_ids = {s.grade_id for s in sections if s.grade_id}
    if not grade_ids:
        return []
    return Grade.query.filter(Grade.id.in_(grade_ids)).order_by(Grade.name).all()


def _is_admin() -> bool:
    return current_user.is_admin_user


def _get_school_and_year():
    school = get_current_school()
    if not school:
        return None, None
    year = get_active_year(school.id)
    return school, year


def _notify_section_parents(hw: Homework, school_id: int) -> None:
    """Create one in-app notification per parent of every student in hw's section."""
    if not hw.section_id:
        return
    from app.models import Student, parent_students, User
    from sqlalchemy import select as _sel

    subject_name = hw.subject.name if hw.subject else 'مادة'
    title = f'واجب جديد — {subject_name}'
    body  = f'تم إضافة واجب "{hw.title}" في مادة {subject_name}. تاريخ التسليم: {hw.due_date}'

    student_ids = [
        s.id for s in
        Student.query.filter_by(section_id=hw.section_id, status='active').all()
    ]
    if not student_ids:
        return

    parent_user_ids = {
        row.user_id
        for row in db.session.execute(
            _sel(parent_students.c.user_id).where(
                parent_students.c.student_id.in_(student_ids)
            )
        ).fetchall()
    }

    fcm_targets = []
    for uid in parent_user_ids:
        existing = Notification.query.filter_by(
            school_id=school_id,
            target_user_id=uid,
            title=title,
            body=body,
        ).first()
        if not existing:
            db.session.add(Notification(
                school_id=school_id,
                title=title,
                body=body,
                ntype='homework',
                target_user_id=uid,
                created_by=current_user.id,
            ))
            fcm_targets.append(uid)

    # FCM push fires after the session flush in the calling route.
    # send_push_to_user reads already-committed device tokens, so DB commit
    # of the Notification row is not required before sending.
    try:
        from app.services.fcm_service import is_enabled, send_push_to_user
        if is_enabled() and fcm_targets:
            data = {'type': 'homework', 'school_id': str(school_id),
                    'homework_id': str(hw.id)}
            current_app.logger.info(
                '[homework] FCM push → %d parent(s) title=%r', len(fcm_targets), title)
            for uid in fcm_targets:
                send_push_to_user(uid, title, body, data)
    except Exception:
        current_app.logger.exception('[homework] FCM dispatch failed hw_id=%s', hw.id)


# ═════════════════════════════════════════════════════════════════════════════
#  Routes
# ═════════════════════════════════════════════════════════════════════════════

@homework_bp.route('/', methods=['GET'])
@homework_access_required
def index():
    school, year = _get_school_and_year()
    if not school or not year:
        flash('لا يوجد عام دراسي نشط.', 'warning')
        return render_template('homework/index.html', homework_list=[], emp=None,
                               is_admin=_is_admin())

    emp = _get_employee()
    is_admin = _is_admin()

    q = (Homework.query
         .filter_by(school_id=school.id, academic_year_id=year.id, is_active=True)
         .order_by(Homework.publish_date.desc(), Homework.id.desc()))

    if not is_admin and emp:
        # Teachers see only their own homework
        q = q.filter_by(teacher_id=emp.id)
    elif not is_admin and not emp:
        q = q.filter(False)  # no employee record → nothing visible

    homework_list = q.all()
    return render_template('homework/index.html',
                           homework_list=homework_list,
                           emp=emp,
                           is_admin=is_admin,
                           resolve_photo_url=resolve_photo_url)


@homework_bp.route('/create', methods=['GET', 'POST'])
@homework_access_required
def create():
    school, year = _get_school_and_year()
    if not school or not year:
        flash('لا يوجد عام دراسي نشط.', 'warning')
        return redirect(url_for('homework.index'))

    emp = _get_employee()
    is_admin = _is_admin()

    # Build dropdown data scoped to teacher (or all for admin)
    if is_admin:
        sections = Section.query.filter_by(
            school_id=school.id, academic_year_id=year.id).order_by(Section.name).all()
        subjects = Subject.query.filter_by(
            school_id=school.id, academic_year_id=year.id).order_by(Subject.name).all()
    elif emp:
        sec_ids  = _teacher_section_ids(emp)
        subj_ids = _teacher_subject_ids(emp)
        sections = (Section.query.filter(Section.id.in_(sec_ids)).order_by(Section.name).all()
                    if sec_ids else [])
        subjects = (Subject.query.filter(Subject.id.in_(subj_ids)).order_by(Subject.name).all()
                    if subj_ids else [])
    else:
        sections, subjects = [], []

    grades = _teacher_grades(sections) if not is_admin else (
        Grade.query.filter_by(school_id=school.id, academic_year_id=year.id)
                   .order_by(Grade.name).all()
    )

    if not is_admin and not sections:
        flash('لا توجد شعب مرتبطة بحسابك. لا يمكن إنشاء واجب.', 'warning')
        return redirect(url_for('homework.index'))

    if request.method == 'POST':
        title        = request.form.get('title', '').strip()
        subject_id   = request.form.get('subject_id', type=int)
        section_id   = request.form.get('section_id', type=int)
        publish_date = request.form.get('publish_date', '')
        due_date_str = request.form.get('due_date', '')
        description  = request.form.get('description', '').strip()
        file         = request.files.get('attachment')

        # ── Validation ──────────────────────────────────────────────────────
        errors = []
        if not title:
            errors.append('عنوان الواجب مطلوب.')
        if not section_id:
            errors.append('الشعبة مطلوبة.')
        if not publish_date:
            errors.append('تاريخ النشر مطلوب.')
        if not due_date_str:
            errors.append('تاريخ التسليم مطلوب.')

        pub_dt = due_dt = None
        if publish_date:
            try:
                from datetime import datetime as _dt
                pub_dt = _dt.strptime(publish_date, '%Y-%m-%d').date()
            except ValueError:
                errors.append('صيغة تاريخ النشر غير صحيحة.')
        if due_date_str:
            try:
                from datetime import datetime as _dt
                due_dt = _dt.strptime(due_date_str, '%Y-%m-%d').date()
            except ValueError:
                errors.append('صيغة تاريخ التسليم غير صحيحة.')
        if pub_dt and due_dt and due_dt < pub_dt:
            errors.append('تاريخ التسليم يجب ألا يكون قبل تاريخ النشر.')

        # Teacher scope validation
        if not is_admin and emp:
            if section_id and section_id not in _teacher_section_ids(emp):
                errors.append('لا يمكنك تعيين واجب لشعبة غير مرتبطة بك.')
            if subject_id and subject_id not in _teacher_subject_ids(emp):
                errors.append('لا يمكنك تعيين واجب لمادة غير مرتبطة بك.')

        # Attachment validation
        attach_path = attach_type = None
        if file and file.filename:
            ext = _ext(file.filename)
            if ext not in _ATTACH_ALL:
                errors.append('نوع الملف غير مسموح. الأنواع المقبولة: jpg, jpeg, png, webp, pdf.')
            else:
                attach_type = _attachment_type(file.filename)

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('homework/form.html',
                                   sections=sections, subjects=subjects,
                                   grades=grades, hw=None, today=date.today())

        # ── Save attachment ──────────────────────────────────────────────────
        if file and file.filename and not errors:
            current_app.config['ALLOWED_EXTENSIONS'] = _ATTACH_ALL
            attach_path = save_uploaded_file(file, subfolder='homework')

        # ── Persist ─────────────────────────────────────────────────────────
        teacher_id = emp.id if emp else None
        hw = Homework(
            school_id=school.id,
            academic_year_id=year.id,
            teacher_id=teacher_id,
            subject_id=subject_id or None,
            section_id=section_id or None,
            title=title,
            description=description or None,
            publish_date=pub_dt,
            due_date=due_dt,
            attachment_path=attach_path,
            attachment_type=attach_type,
            is_active=True,
        )
        db.session.add(hw)
        db.session.flush()

        # Optional: notify parents
        try:
            _notify_section_parents(hw, school.id)
        except Exception as exc:
            current_app.logger.warning(f'Homework notification failed: {exc}')

        db.session.commit()
        flash('تم إضافة الواجب بنجاح.', 'success')
        return redirect(url_for('homework.index'))

    return render_template('homework/form.html',
                           sections=sections, subjects=subjects, grades=grades,
                           hw=None, today=date.today())


@homework_bp.route('/<int:hw_id>/edit', methods=['GET', 'POST'])
@homework_access_required
def edit(hw_id):
    school, year = _get_school_and_year()
    if not school or not year:
        abort(404)

    hw = Homework.query.filter_by(id=hw_id, school_id=school.id, is_active=True).first_or_404()
    emp = _get_employee()
    is_admin = _is_admin()

    # Teachers can only edit their own homework
    if not is_admin and emp and hw.teacher_id != emp.id:
        abort(403)
    if not is_admin and not emp:
        abort(403)

    if is_admin:
        sections = Section.query.filter_by(
            school_id=school.id, academic_year_id=year.id).order_by(Section.name).all()
        subjects = Subject.query.filter_by(
            school_id=school.id, academic_year_id=year.id).order_by(Subject.name).all()
    elif emp:
        sec_ids  = _teacher_section_ids(emp)
        subj_ids = _teacher_subject_ids(emp)
        sections = (Section.query.filter(Section.id.in_(sec_ids)).order_by(Section.name).all()
                    if sec_ids else [])
        subjects = (Subject.query.filter(Subject.id.in_(subj_ids)).order_by(Subject.name).all()
                    if subj_ids else [])
    else:
        sections, subjects = [], []

    grades = _teacher_grades(sections) if not is_admin else (
        Grade.query.filter_by(school_id=school.id, academic_year_id=year.id)
                   .order_by(Grade.name).all()
    )

    if request.method == 'POST':
        title        = request.form.get('title', '').strip()
        subject_id   = request.form.get('subject_id', type=int)
        section_id   = request.form.get('section_id', type=int)
        publish_date = request.form.get('publish_date', '')
        due_date_str = request.form.get('due_date', '')
        description  = request.form.get('description', '').strip()
        file         = request.files.get('attachment')
        clear_attach = request.form.get('clear_attachment') == '1'

        errors = []
        if not title:
            errors.append('عنوان الواجب مطلوب.')
        if not section_id:
            errors.append('الشعبة مطلوبة.')
        if not publish_date:
            errors.append('تاريخ النشر مطلوب.')
        if not due_date_str:
            errors.append('تاريخ التسليم مطلوب.')

        pub_dt = due_dt = None
        if publish_date:
            try:
                from datetime import datetime as _dt
                pub_dt = _dt.strptime(publish_date, '%Y-%m-%d').date()
            except ValueError:
                errors.append('صيغة تاريخ النشر غير صحيحة.')
        if due_date_str:
            try:
                from datetime import datetime as _dt
                due_dt = _dt.strptime(due_date_str, '%Y-%m-%d').date()
            except ValueError:
                errors.append('صيغة تاريخ التسليم غير صحيحة.')
        if pub_dt and due_dt and due_dt < pub_dt:
            errors.append('تاريخ التسليم يجب ألا يكون قبل تاريخ النشر.')

        if not is_admin and emp:
            if section_id and section_id not in _teacher_section_ids(emp):
                errors.append('لا يمكنك تعيين واجب لشعبة غير مرتبطة بك.')
            if subject_id and subject_id not in _teacher_subject_ids(emp):
                errors.append('لا يمكنك تعيين واجب لمادة غير مرتبطة بك.')

        new_attach_path = new_attach_type = None
        if file and file.filename:
            ext = _ext(file.filename)
            if ext not in _ATTACH_ALL:
                errors.append('نوع الملف غير مسموح. الأنواع المقبولة: jpg, jpeg, png, webp, pdf.')
            else:
                new_attach_type = _attachment_type(file.filename)

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('homework/form.html',
                                   sections=sections, subjects=subjects,
                                   grades=grades, hw=hw, today=date.today())

        if file and file.filename:
            current_app.config['ALLOWED_EXTENSIONS'] = _ATTACH_ALL
            new_attach_path = save_uploaded_file(file, subfolder='homework')

        hw.title       = title
        hw.subject_id  = subject_id or None
        hw.section_id  = section_id or None
        hw.publish_date = pub_dt
        hw.due_date    = due_dt
        hw.description = description or None
        if new_attach_path:
            hw.attachment_path = new_attach_path
            hw.attachment_type = new_attach_type
        elif clear_attach:
            hw.attachment_path = None
            hw.attachment_type = None

        db.session.commit()
        flash('تم تحديث الواجب بنجاح.', 'success')
        return redirect(url_for('homework.index'))

    return render_template('homework/form.html',
                           sections=sections, subjects=subjects, grades=grades,
                           hw=hw, today=date.today())


@homework_bp.route('/<int:hw_id>/delete', methods=['POST'])
@homework_access_required
def delete(hw_id):
    school, year = _get_school_and_year()
    if not school:
        abort(404)

    hw = Homework.query.filter_by(id=hw_id, school_id=school.id, is_active=True).first_or_404()
    emp = _get_employee()
    is_admin = _is_admin()

    if not is_admin and emp and hw.teacher_id != emp.id:
        abort(403)
    if not is_admin and not emp:
        abort(403)

    db.session.delete(hw)
    db.session.commit()
    flash('تم حذف الواجب.', 'success')
    return redirect(url_for('homework.index'))

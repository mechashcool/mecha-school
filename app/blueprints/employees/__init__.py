"""Al-Muhandis – Employees Blueprint  (Phase 6: user account, teacher assignments)"""
import logging

from flask import (Blueprint, render_template, redirect, url_for, flash, request, jsonify)
from flask_login import login_required, current_user
from datetime import datetime as dt, date

from app.models import (db, Employee, User, Role, teacher_subjects,
                        Subject, Section, Grade, DeviceEmployeeMapping,
                        EmployeeAttendance)
from app.utils.decorators import (permission_required, accountant_or_permission,
                                   get_current_school,
                                   historical_guard, get_active_year, action_required)
from app.utils.helpers import save_uploaded_file
from app.utils import code_generator
from app.utils.audit import log_action

_log = logging.getLogger(__name__)

employees_bp = Blueprint('employees', __name__,
                          template_folder='../../templates/employees')


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

# Login roles that may be created/assigned for an employee FROM THE EMPLOYEE PAGE.
# Deliberately limited to non-privileged roles. Privileged roles (school_admin,
# super_admin) must never be creatable or assignable from this page. Every
# server-side handler that reads a submitted role_id re-validates it against this
# allow-list, so a crafted POST cannot bypass the restricted dropdown.
_ACCOUNT_ROLE_NAMES = ('teacher', 'parent')

# Maximum number of documents that may be attached in the "Add New Employee"
# wizard. Enforced BOTH in the create wizard UI (add-row button) and server-side
# in _handle_employee_post so a forged/manually-modified POST cannot exceed it.
MAX_EMPLOYEE_DOCUMENTS = 5

# Per-file / per-field ceilings for the "Add New Employee" wizard. Each one is
# enforced twice: in the browser for immediate feedback, and again server-side
# in _handle_employee_post — which is the authoritative check, since a client
# can disable JavaScript or craft the POST directly. These are PER FILE limits,
# not a request limit: a valid submission may carry one photo plus five
# documents, so the global MAX_CONTENT_LENGTH (16 MB) is deliberately left
# untouched — lowering it would reject legitimate submissions.
MAX_EMPLOYEE_PHOTO_BYTES    = 2 * 1024 * 1024   # 2 MB per employee photo
MAX_EMPLOYEE_DOCUMENT_BYTES = 2 * 1024 * 1024   # 2 MB per employee document
MAX_EMPLOYEE_NOTES_CHARS    = 300               # characters, Python len()

# Arabic validation messages for the limits above (kept generic on purpose —
# they never echo a filename, path, byte count, or internal detail).
_MSG_PHOTO_TOO_BIG = 'حجم صورة الموظف يجب ألا يتجاوز 2 ميجابايت.'
_MSG_DOC_TOO_BIG   = 'حجم كل مستند يجب ألا يتجاوز 2 ميجابايت.'
_MSG_NOTES_TOO_LONG = f'يجب ألا تتجاوز الملاحظات {MAX_EMPLOYEE_NOTES_CHARS} حرف.'
_MSG_PHOTO_REJECTED = ('تعذّر حفظ صورة الموظف — تأكد من أن الصيغة مقبولة '
                       'وأن الحجم لا يتجاوز 2 ميجابايت.')


def _uploaded_size(file_storage):
    """Real byte length of an uploaded file, measured from its own stream.

    Never trusts a client-supplied size, the Content-Length header, the MIME
    type, or the filename: the stream itself is measured. The position is
    restored before returning so the existing ``save_uploaded_file`` helper can
    still read the file normally afterwards.

    Returns the size in bytes, or ``None`` when it cannot be measured — callers
    treat ``None`` as a failure (fail closed) rather than letting an unmeasured
    upload through.
    """
    stream = getattr(file_storage, 'stream', None)
    if stream is None:
        return None
    try:
        pos = stream.tell()
        stream.seek(0, 2)          # SEEK_END
        size = stream.tell()
        stream.seek(pos)           # restore for save_uploaded_file()
        return size
    except (AttributeError, OSError, ValueError):
        return None


def _available_roles():
    """Roles selectable for an employee login account from the employee page.

    Restricted to teacher and parent only (see _ACCOUNT_ROLE_NAMES). This is an
    allow-list, not an exclude-list, so no privileged role can ever leak into the
    selector even if new roles are added to the system later.
    """
    return (Role.query
            .filter(Role.name.in_(_ACCOUNT_ROLE_NAMES))
            .order_by(Role.name)
            .all())


def _is_allowed_account_role(role_id) -> bool:
    """True when role_id maps to a role permitted for employee login accounts.

    Used to re-validate a client-submitted role_id server-side. Never trust the
    posted value: the dropdown is already restricted, but the POST body is not.
    """
    if not role_id:
        return False
    role = Role.query.get(role_id)
    return bool(role and role.name in _ACCOUNT_ROLE_NAMES)


def _ordered_wizard_grades(grades):
    """Sort the school's grades into their normal display order for the wizard.

    Reuses the project's existing canonical grade list (``IRAQI_STANDARD_GRADES``
    in app/utils/iraqi_grades.py, whose order already "defines visual display
    order") instead of the alphabetical-by-name ordering the query returns, so
    الصف الأول comes before الصف الثاني and stages appear in their real
    progression rather than alphabetically.

    Nothing is hard-coded or invented here: only the grades the school actually
    has are returned. Custom grades the canonical list does not know, and stages
    it does not know (e.g. رياض الأطفال, الثانوية, or any school-specific stage),
    are kept and sorted after the known ones by name — never dropped.
    """
    from app.utils.iraqi_grades import IRAQI_STANDARD_GRADES, _normalize

    grade_rank = {_normalize(n): i for i, (n, _s) in enumerate(IRAQI_STANDARD_GRADES)}
    stage_rank = {}
    for _i, (_n, stage) in enumerate(IRAQI_STANDARD_GRADES):
        stage_rank.setdefault(_normalize(stage), len(stage_rank))
    big = len(grade_rank) + 1

    def key(g):
        stage = _normalize(g.stage or '')
        # Unknown stages sort after known ones, alphabetically; grades with no
        # stage at all go last so they never break the stage grouping.
        s_known = stage in stage_rank
        return (
            0 if stage else 1,
            0 if s_known else 1,
            stage_rank.get(stage, 0),
            '' if s_known else stage,
            grade_rank.get(_normalize(g.name or ''), big),
            g.name or '',
        )

    return sorted(grades, key=key)


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

    # Grade → its own sections AND its own subjects, for the create-wizard
    # teacher step. Built from the SAME school+year scoped grade/section/subject
    # lists loaded above, so the client can never be offered anything belonging
    # to another school or year — and every id is still re-validated server-side
    # on POST (_wizard_teacher_selection).
    #
    # EVERY grade configured for the school is included, across every stage, even
    # when it has no sections or no subjects yet: the wizard shows an explicit
    # empty state inside that grade instead of hiding it. Order follows the
    # project's canonical grade order (see _ordered_wizard_grades).
    wizard_ta_grades = [
        {
            'id':       g.id,
            'name':     g.name,
            'stage':    g.stage or '',
            'sections': [{'id': s.id, 'name': s.name}
                         for s in sections if s.grade_id == g.id],
            'subjects': [{'id': sub.id, 'name': sub.name}
                         for sub in subjects if sub.grade_id == g.id],
        }
        for g in _ordered_wizard_grades(grades)
    ]

    roles = _available_roles()

    # ── Employee classification (job title / specialty) ───────────────────────
    # Selectable job titles = defaults + this school's custom titles. The full
    # {title: [specialties…, 'أخرى']} map drives the dependent specialty dropdown
    # client-side. An existing employee whose saved values fall outside those
    # lists (grandfathered free-text) is merged in so edit can preselect them.
    from app.utils import employee_classification as _ec
    school_id  = school.id if school else None
    job_titles = _ec.selectable_job_titles(school_id)
    class_map  = _ec.classification_map(school_id)
    cur_job    = employee.job_title  if employee else None
    cur_dep    = employee.department if employee else None
    if cur_job and cur_job != _ec.OTHER and cur_job not in job_titles:
        job_titles = job_titles + [cur_job]
    if cur_job and cur_job != _ec.OTHER:
        _base = [s for s in class_map.get(cur_job, []) if s != _ec.OTHER]
        if cur_dep and cur_dep not in _base:
            _base.append(cur_dep)
        class_map[cur_job] = _base + [_ec.OTHER]

    # ── Auto-generated employee (teacher) login credentials — create only ─────
    # Mirrors the Add Student flow: a fresh pair is produced on each render of the
    # create wizard and shown read-only. On a re-render after a validation error
    # the submitted values take precedence in the template, so the SAME
    # credentials persist through the wizard until save. Not generated for the
    # edit form (employee is not None), where the linked account already exists.
    gen_employee_username = None
    gen_employee_password = None
    if employee is None:
        gen_employee_username = code_generator.generate_parent_username()
        gen_employee_password = code_generator.generate_parent_password()

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
        max_employee_documents  = MAX_EMPLOYEE_DOCUMENTS,
        max_employee_photo_bytes    = MAX_EMPLOYEE_PHOTO_BYTES,
        max_employee_document_bytes = MAX_EMPLOYEE_DOCUMENT_BYTES,
        max_employee_notes_chars    = MAX_EMPLOYEE_NOTES_CHARS,
        wizard_ta_grades        = wizard_ta_grades,
        gen_employee_username   = gen_employee_username,
        gen_employee_password   = gen_employee_password,
        emp_job_titles          = job_titles,
        emp_classification_map  = class_map,
        emp_class_other         = _ec.OTHER,
        emp_cur_job             = cur_job,
        emp_cur_dep             = cur_dep,
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
#  Create-wizard teacher assignments (multi grade → multi section)
#
#  The "Add New Employee" wizard submits every teaching / homeroom section as an
#  explicit "<grade_id>:<section_id>" pair, so the grade a section was chosen
#  under is part of the request instead of being inferred from the section id.
#  Storage is unchanged and identical to what the School User Management screen
#  writes: homeroom → Section.teacher_id, teaching → teacher_subjects rows
#  (subject × section). No new table, model, or parallel assignment store.
#
#  Only the create wizard uses these helpers; the employee EDIT form keeps using
#  _save_teacher_assignments above, unchanged.
# ─────────────────────────────────────────────────────────────────────────────

# Generic Arabic messages — never echo the submitted ids, model names, or the
# underlying exception back to the browser.
_TA_ERR_READ    = 'تعذّر قراءة تكليفات التدريسي المحددة. يرجى إعادة الاختيار.'
_TA_ERR_SECTION = ('الصفوف أو الشعب المحددة غير صالحة أو لا تعود لهذه المدرسة '
                   'أو للعام الدراسي الحالي. يرجى إعادة الاختيار.')
_TA_ERR_SUBJECT = ('المواد الدراسية المحددة غير صالحة أو لا تعود لهذه المدرسة '
                   'أو للعام الدراسي الحالي. يرجى إعادة الاختيار.')
_TA_ERR_SUBJ_GRADE = 'إحدى المواد الدراسية المختارة لا تنتمي إلى الصفوف المحددة.'
_TA_ERR_NO_YEAR = 'لا يمكن حفظ تكليفات التدريسي بدون عام دراسي فعّال.'


def _parse_grade_section_pairs(field_name):
    """Parse ``"<grade_id>:<section_id>"`` tokens submitted under *field_name*.

    Returns a list of ``(grade_id, section_id)`` int tuples. Any token that is
    not exactly two integers is rejected — a malformed/forged value never
    silently degrades into "section only" (which would let the server infer the
    grade from the section instead of verifying the submitted relationship).
    """
    pairs = []
    for raw in request.form.getlist(field_name):
        raw = (raw or '').strip()
        if not raw:
            continue
        parts = raw.split(':')
        if len(parts) != 2:
            raise ValueError(_TA_ERR_READ)
        try:
            pairs.append((int(parts[0]), int(parts[1])))
        except (TypeError, ValueError):
            raise ValueError(_TA_ERR_READ)
    return pairs


def _wizard_teacher_selection(school, year):
    """Validate the create-wizard teacher selection against the trusted context.

    Every submitted grade, section, and subject is re-checked against THIS
    school's own rows for THIS academic year, and every section must really
    belong to the grade it was submitted under. Ids are never trusted from the
    request: a forged grade/section/subject id from another school (or another
    year, or a section paired with the wrong grade) is rejected before anything
    is written.

    Returns ``(homeroom_section_ids, teaching_section_ids, subject_ids)`` with
    duplicates removed, or raises ``ValueError`` carrying a friendly Arabic
    message for the caller to flash.
    """
    hr_pairs    = _parse_grade_section_pairs('wiz_homeroom[]')
    ts_pairs    = _parse_grade_section_pairs('wiz_teaching[]')
    subject_ids = [i for i in request.form.getlist('subject_ids', type=int) if i]

    if not (hr_pairs or ts_pairs or subject_ids):
        return [], [], []

    if not (school and year):
        raise ValueError(_TA_ERR_NO_YEAR)

    # Allow-lists built from the trusted server-side school/year context. The
    # explicit school_id + academic_year_id filters (with the ORM tenant scope
    # bypassed) mirror the School User Management validation exactly, so the
    # result cannot depend on implicit request-scope state.
    valid_grade_ids = {
        r[0] for r in db.session.query(Grade.id)
        .execution_options(bypass_tenant_scope=True)
        .filter(Grade.school_id == school.id,
                Grade.academic_year_id == year.id).all()
    }
    section_grade = {
        r[0]: r[1] for r in db.session.query(Section.id, Section.grade_id)
        .execution_options(bypass_tenant_scope=True)
        .filter(Section.school_id == school.id,
                Section.academic_year_id == year.id).all()
    }
    # subject_id → its owning grade_id (None for school-wide subjects), scoped to
    # this school and year. Used both to reject foreign subjects and to enforce
    # that a subject really belongs to one of the selected teaching grades.
    subject_grade = {
        r[0]: r[1] for r in db.session.query(Subject.id, Subject.grade_id)
        .execution_options(bypass_tenant_scope=True)
        .filter(Subject.school_id == school.id,
                Subject.academic_year_id == year.id).all()
    }

    def _clean_pairs(pairs):
        """Verify each pair and collapse duplicate grade/section combinations."""
        out, seen = [], set()
        for grade_id, section_id in pairs:
            if (grade_id not in valid_grade_ids
                    or section_grade.get(section_id) != grade_id):
                raise ValueError(_TA_ERR_SECTION)
            if section_id in seen:
                continue
            seen.add(section_id)
            out.append(section_id)
        return out

    homeroom_ids = _clean_pairs(hr_pairs)
    teaching_ids = _clean_pairs(ts_pairs)

    # Grades actually selected under "الشعب التي يدرسها". Homeroom-only grades
    # deliberately do NOT widen the set of acceptable subjects.
    teaching_grade_ids = {section_grade[s_id] for s_id in teaching_ids}

    clean_subjects, seen_subjects = [], set()
    for subject_id in subject_ids:
        if subject_id not in subject_grade:
            # Unknown, other-school, or other-year subject.
            raise ValueError(_TA_ERR_SUBJECT)
        if subject_grade[subject_id] not in teaching_grade_ids:
            # Belongs to no selected teaching grade (or to none at all) — this
            # catches a stale selection left over from a removed grade just as
            # much as a deliberately forged id.
            raise ValueError(_TA_ERR_SUBJ_GRADE)
        if subject_id in seen_subjects:
            continue
        seen_subjects.add(subject_id)
        clean_subjects.append(subject_id)

    return homeroom_ids, teaching_ids, clean_subjects


def _save_wizard_teacher_assignments(emp, school, year):
    """Persist the validated create-wizard teacher assignments for *emp*.

    Uses the existing relationships only:
      * homeroom  → ``Section.teacher_id``
      * teaching  → ``teacher_subjects`` (employee_id, subject_id, section_id)

    Homeroom and teaching stay separate: a taught section never sets
    ``teacher_id``. No commit here — the caller commits once, together with the
    employee, the linked user account, the photo, and the documents.
    """
    homeroom_ids, teaching_ids, subject_ids = _wizard_teacher_selection(school, year)

    if homeroom_ids:
        # Re-assert the school/year filter on the write itself so the UPDATE can
        # only ever touch rows already proven to belong to this school and year.
        (Section.query
         .execution_options(bypass_tenant_scope=True)
         .filter(Section.id.in_(homeroom_ids),
                 Section.school_id == school.id,
                 Section.academic_year_id == year.id)
         .update({'teacher_id': emp.id}, synchronize_session=False))

    rows = [{'employee_id': emp.id, 'subject_id': subject_id, 'section_id': section_id}
            for section_id in teaching_ids for subject_id in subject_ids]
    if rows:
        db.session.execute(teacher_subjects.insert(), rows)

    return homeroom_ids, teaching_ids, subject_ids


# ─────────────────────────────────────────────────────────────────────────────
#  Classification (job title / specialty) resolution
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_classification(employee, emp_cfg, school):
    """Resolve and validate the submitted job title + specialty server-side.

    Returns ``(job_title, department, pending_title, pending_specialty, error)``:
      * job_title / department  — final text to store (may be None / '').
      * pending_title           — a new custom job title to persist for the
                                  school on success, else None.
      * pending_specialty       — ``(job_title, value)`` custom specialty to
                                  persist for the school on success, else None.
      * error                   — an Arabic message string when validation fails
                                  (caller re-renders the form); None on success.

    Never trusts the browser dropdown: every submitted value is validated against
    the school's own default/custom options (or the employee's grandfathered
    value). A title or specialty belonging to another school cannot be accepted —
    it is neither a default nor present in THIS school's custom store, so it is
    rejected.
    """
    from app.utils import employee_classification as ec
    school_id = school.id if school else None

    pending_title = None
    pending_specialty = None

    # ── Job title ─────────────────────────────────────────────────────────────
    if emp_cfg.field_visible('employees', 'job_title'):
        raw = request.form.get('job_title', '').strip()
        preserved_job = employee.job_title if employee else None
        if raw == ec.OTHER:
            custom = ec.normalize_display(request.form.get('job_title_custom', ''))
            if not custom:
                return None, None, None, None, 'يرجى كتابة المسمى الوظيفي.'
            job_title = custom
            if not ec.job_title_exists(school_id, custom):
                pending_title = custom
        elif raw:
            if not ec.is_valid_job_title(school_id, raw, preserved_job):
                return None, None, None, None, 'المسمى الوظيفي المحدد غير صالح.'
            job_title = raw
        else:
            job_title = None
    else:
        # Field hidden for this school — preserve the existing value untouched.
        job_title = employee.job_title if employee else None

    if (emp_cfg.field_visible('employees', 'job_title')
            and emp_cfg.field_required('employees', 'job_title')
            and not job_title):
        return None, None, None, None, 'المسمى الوظيفي مطلوب.'

    # ── Specialty / department ────────────────────────────────────────────────
    if emp_cfg.field_visible('employees', 'department'):
        raw_dep = request.form.get('department', '').strip()
        # Preserved specialty is only honoured when the job title is unchanged.
        preserved_dep = None
        if (employee and employee.department
                and employee.job_title and job_title
                and ec._norm_key(employee.job_title) == ec._norm_key(job_title)):
            preserved_dep = employee.department
        if raw_dep == ec.OTHER:
            custom = ec.normalize_display(request.form.get('department_custom', ''))
            if not custom:
                return None, None, None, None, 'يرجى كتابة القسم / الاختصاص.'
            if not job_title:
                return None, None, None, None, 'يرجى اختيار المسمى الوظيفي أولاً.'
            department = custom
            if not ec.specialty_exists(school_id, job_title, custom):
                pending_specialty = (job_title, custom)
        elif raw_dep:
            if not ec.is_valid_specialty(school_id, job_title or '', raw_dep, preserved_dep):
                return None, None, None, None, 'القسم / الاختصاص غير صالح للمسمى الوظيفي المحدد.'
            department = raw_dep
        else:
            department = ''
    else:
        department = employee.department if employee else ''

    return job_title, department, pending_title, pending_specialty, None


# ─────────────────────────────────────────────────────────────────────────────
#  Shared POST handler
# ─────────────────────────────────────────────────────────────────────────────

def _handle_employee_post(employee):
    from app.utils.school_config import get_school_config
    school    = get_current_school()
    year      = get_active_year(school.id) if school else None
    is_create = employee is None
    emp_cfg   = get_school_config(school.id if school else None)
    # Create uses the multi-step wizard; edit keeps the single form.
    _tmpl = 'employees/create_wizard.html' if is_create else 'employees/form.html'

    full_name = request.form.get('full_name', '').strip()
    email     = request.form.get('email', '').strip() or None

    if not full_name:
        flash('يرجى ملء حقل الاسم الكامل.', 'danger')
        return render_template(_tmpl, error_step='basic', **_form_context(employee))

    # Job title + specialty ("القسم / الاختصاص"): resolved and validated
    # server-side against THIS school's default/custom options. pending_* hold
    # any new custom option to persist for the school AFTER a successful save.
    (job_title, department, _pending_title, _pending_specialty,
     _class_error) = _resolve_classification(employee, emp_cfg, school)
    if _class_error:
        flash(_class_error, 'danger')
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

    notes_value = request.form.get('notes', '').strip()

    # ── Create-wizard limits — enforced BEFORE anything is created ────────────
    # Notes length, photo size, document count, and per-document size are all
    # validated here: before the Employee row is inserted, before the linked User
    # account exists, and before a single byte is written to storage. A request
    # that fails any of these is rejected outright, so it can never leave a
    # partial employee, an orphan account, orphan EmployeeDocument rows, or an
    # orphan uploaded file behind. The wizard enforces the same limits in the
    # browser; these are the independent server-side checks — JavaScript, the
    # Content-Length header, the MIME type, and the filename are all untrusted.
    # Document rows exist only in the create wizard, so this block is create-only
    # and the employee edit form keeps its current behaviour unchanged.
    if is_create:
        if len(notes_value) > MAX_EMPLOYEE_NOTES_CHARS:
            flash(_MSG_NOTES_TOO_LONG, 'danger')
            return render_template(_tmpl, error_step='documents',
                                   **_form_context(employee))

        _photo_file = request.files.get('photo')
        if _photo_file and _photo_file.filename:
            _photo_size = _uploaded_size(_photo_file)
            if _photo_size is None or _photo_size > MAX_EMPLOYEE_PHOTO_BYTES:
                flash(_MSG_PHOTO_TOO_BIG, 'danger')
                return render_template(_tmpl, error_step='documents',
                                       **_form_context(employee))

        _submitted_doc_files = [f for f in request.files.getlist('doc_file[]')
                                if f and f.filename]
        if len(_submitted_doc_files) > MAX_EMPLOYEE_DOCUMENTS:
            flash(f'يمكن إضافة {MAX_EMPLOYEE_DOCUMENTS} مستندات كحد أقصى.', 'danger')
            return render_template(_tmpl, error_step='documents',
                                   **_form_context(employee))
        # Every document is checked before ANY of them is saved, so an oversized
        # file never results in the other documents being partially uploaded.
        for _doc_file in _submitted_doc_files:
            _doc_size = _uploaded_size(_doc_file)
            if _doc_size is None or _doc_size > MAX_EMPLOYEE_DOCUMENT_BYTES:
                flash(_MSG_DOC_TOO_BIG, 'danger')
                return render_template(_tmpl, error_step='documents',
                                       **_form_context(employee))

    photo_path = None
    if 'photo' in request.files and request.files['photo'].filename:
        # max_size is a second, independent gate inside the shared upload helper
        # (it measures the bytes it actually reads). Applied on create only, so
        # the edit flow keeps its existing behaviour untouched.
        photo_path = save_uploaded_file(
            request.files['photo'], 'employees',
            max_size=MAX_EMPLOYEE_PHOTO_BYTES if is_create else None)
        if is_create and not photo_path:
            # Rejected by the helper (extension or size) — stop before creating
            # the employee rather than silently dropping the photo.
            flash(_MSG_PHOTO_REJECTED, 'danger')
            return render_template(_tmpl, error_step='documents',
                                   **_form_context(employee))

    if is_create:
        employee = Employee(
            employee_id   = code_generator.generate_employee_id(school.id),
            full_name     = full_name,
            job_title     = job_title,
            department    = department,
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
            notes         = notes_value,
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
        employee.department    = department
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
        employee.notes         = notes_value
        if hire_date:
            employee.hire_date = hire_date
        if photo_path:
            employee.photo = photo_path

    # ── Mandatory linked teacher account (CREATE flow only) ───────────────────
    # Every new employee automatically receives a login account. It is created in
    # the SAME transaction as the employee (before the single commit below), so
    # the two are atomic: if the account cannot be built the whole request rolls
    # back — no partial employee, no orphan user. The role is ALWAYS the built-in
    # teacher role resolved server-side; no role id is read from the request, so a
    # forged/added role field cannot change it. Credentials reuse the Add Student
    # generator: the read-only values submitted by the wizard are trusted only
    # after re-validating format + global uniqueness, otherwise regenerated here.
    _new_emp_username = None
    _new_emp_password = None
    if is_create:
        teacher_role = Role.query.filter_by(name='teacher').first()
        if not teacher_role:
            db.session.rollback()
            _log.error('Employee create aborted: built-in teacher role missing.')
            flash('تعذّر إنشاء حساب الموظف: دور "تدريسي" غير موجود في النظام. '
                  'يرجى مراجعة مسؤول النظام.', 'danger')
            return render_template(_tmpl, error_step='account', **_form_context(None))

        _new_emp_username = request.form.get('username', '').strip()
        _new_emp_password = request.form.get('user_password', '').strip()
        if not (code_generator.is_valid_parent_username(_new_emp_username)
                and code_generator.parent_username_available(_new_emp_username)):
            _new_emp_username = code_generator.generate_parent_username()
        if not code_generator.is_valid_parent_password(_new_emp_password):
            _new_emp_password = code_generator.generate_parent_password()

        try:
            emp_user = User(
                username=_new_emp_username,
                full_name=employee.full_name,
                role_id=teacher_role.id,
                school_id=school.id if school else None,
                is_active=True,
            )
            emp_user.set_password(_new_emp_password)
            db.session.add(emp_user)
            db.session.flush()
            employee.user_id = emp_user.id
        except Exception:
            db.session.rollback()
            _log.exception('Employee linked-account creation failed (create flow).')
            flash('تعذّر إنشاء حساب الدخول للموظف. لم يتم حفظ الموظف. '
                  'يرجى المحاولة مرة أخرى.', 'danger')
            return render_template(_tmpl, error_step='account', **_form_context(None))

    # Persist any new custom job title / specialty for THIS school only, in the
    # same transaction as the employee so it is saved atomically with the record
    # (and rolled back with it on any failure above). school_id is the trusted
    # server-side context — a custom value can never be attached to another school.
    if school and (_pending_title or _pending_specialty):
        from app.utils import employee_classification as _ec
        if _pending_title:
            _ec.add_custom_job_title(school.id, _pending_title)
        if _pending_specialty:
            _ec.add_custom_specialty(school.id, _pending_specialty[0],
                                     _pending_specialty[1])

    # ── Wizard documents + teacher assignments (CREATE flow only) ─────────────
    # Both are staged in the SAME transaction as the employee row and its linked
    # teacher account, before the single commit below. If either fails the whole
    # request is rolled back: no partial employee, no orphan user account, no
    # half-written assignments. employee.id is already available from the flush.
    _doc_saved     = 0
    _doc_warnings  = []
    _ta_no_subject = False
    if is_create:
        from app.models import EmployeeDocument
        _ALLOWED_DOC_EXTS = {'pdf', 'jpg', 'jpeg', 'png', 'doc', 'docx'}
        doc_types = request.form.getlist('doc_type[]')
        doc_files = request.files.getlist('doc_file[]')
        for i, f in enumerate(doc_files):
            if not f or not f.filename:
                continue
            doc_type = doc_types[i].strip() if i < len(doc_types) else ''
            _raw = f.filename.rsplit('/', 1)[-1].rsplit('\\', 1)[-1]
            title = doc_type or _raw.rsplit('.', 1)[0] or 'مستند'
            file_path = save_uploaded_file(
                f, 'employee_docs',
                allowed_exts=_ALLOWED_DOC_EXTS,
                max_size=MAX_EMPLOYEE_DOCUMENT_BYTES,
            )
            if not file_path:
                _doc_warnings.append(
                    f'تعذّر رفع المستند "{title}" — تأكد من أن صيغة الملف مقبولة.')
                continue
            db.session.add(EmployeeDocument(
                employee_id=employee.id,
                school_id=school.id if school else None,
                title=title,
                file_path=file_path,
                doc_type=doc_type or None,
            ))
            _doc_saved += 1

        try:
            _hr_ids, _ts_ids, _subj_ids = _save_wizard_teacher_assignments(
                employee, school, year)
            _ta_no_subject = bool(_ts_ids and not _subj_ids)
        except ValueError as _ta_err:
            # Forged / stale / cross-school selection — reject the whole create.
            db.session.rollback()
            flash(str(_ta_err), 'danger')
            return render_template(_tmpl, error_step='teacher', **_form_context(None))
        except Exception:
            db.session.rollback()
            _log.exception('Wizard teacher assignment save failed (create flow).')
            flash('تعذّر حفظ تكليفات التدريسي. لم يتم حفظ الموظف. '
                  'يرجى المحاولة مرة أخرى.', 'danger')
            return render_template(_tmpl, error_step='teacher', **_form_context(None))

    db.session.commit()
    flash_msgs = [('success',
                   f'تم {"إضافة" if is_create else "تحديث"} بيانات الموظف {employee.full_name}.')]
    if is_create and _new_emp_username:
        # One-time display to the authorized creator. Plaintext is shown here only
        # (never stored in the DB — the User row holds a bcrypt hash — and never
        # written to logs, audit details, or the URL).
        flash_msgs.append(('success',
            'تم إنشاء حساب دخول للموظف بدور تدريسي. '
            f'اسم المستخدم: {_new_emp_username} — كلمة المرور: {_new_emp_password}. '
            'يرجى حفظ هذه البيانات وتسليمها للموظف.'))

    # ── Wizard documents / teacher assignments outcome (create only) ─────────
    # The rows themselves were already staged and committed atomically above.
    if is_create:
        for _w in _doc_warnings:
            flash_msgs.append(('warning', _w))
        if _doc_saved:
            flash_msgs.append(('success', f'تم رفع {_doc_saved} مستند(ات) بنجاح.'))
        if _ta_no_subject:
            flash_msgs.append(('warning',
                'لم يتم ربط الشعب التي يدرسها لعدم اختيار أي مادة دراسية. '
                'يمكنك إضافة المواد لاحقاً من صفحة تعديل الموظف.'))

    # ── Linked login account — EDIT flow only ─────────────────────────────────
    # The CREATE flow already created the mandatory teacher account atomically
    # above. For edits we keep the existing behaviour unchanged: optionally create
    # a teacher/parent account, or update the already-linked account.
    create_account = request.form.get('create_account')
    reset_password = request.form.get('reset_password')

    if (not is_create) and create_account and not employee.user_id:
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
        elif not _is_allowed_account_role(role_id):
            # Only teacher/parent accounts may be created from the employee page.
            # Reject any other (or forged) role_id server-side.
            acct_error = 'الدور المحدد غير مسموح به لحساب الموظف. الأدوار المتاحة: معلم أو ولي أمر.'
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

    elif (not is_create) and employee.user_id:
        linked_user = (User.query
                       .execution_options(bypass_tenant_scope=True)
                       .get(employee.user_id))
        if linked_user:
            _current_school_id = school.id if school else None
            if _current_school_id and linked_user.school_id != _current_school_id:
                # Linked user belongs to a different school — the tenant write guard
                # (_before_flush) would raise PermissionError and crash the request.
                # Skip all user mutations and inform the operator.
                flash_msgs.append(('warning',
                    'حساب تسجيل الدخول المرتبط بهذا الموظف يعود لمدرسة مختلفة '
                    'ولا يمكن تعديله من هنا. تم حفظ بيانات الموظف فقط.'))
            else:
                changed    = False
                new_role   = request.form.get('role_id', type=int)
                user_active = request.form.get('user_is_active')

                if new_role and new_role != linked_user.role_id:
                    # Only allow switching the linked account to a permitted
                    # (teacher/parent) role. Ignore any other/forged role_id so a
                    # crafted POST cannot promote the account to a privileged role.
                    if _is_allowed_account_role(new_role):
                        linked_user.role_id = new_role
                        changed = True
                    else:
                        flash_msgs.append(('warning',
                            'لم يتم تغيير دور الحساب: الدور المطلوب غير مسموح به من صفحة الموظف.'))
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

    # ── Teacher assignments — EDIT flow only ─────────────────────────────────
    # The create wizard already saved its assignments atomically (above) using
    # the grade→section pairs it submits; this path stays exactly as it was for
    # the employee edit form.
    if (not is_create) and request.form.get('save_teacher_section'):
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
#  Employee-list filter context (job title / specialty)
# ─────────────────────────────────────────────────────────────────────────────

def _employee_filter_context(school):
    """Build filter dropdown data for the employee list, scoped to this school.

    Options combine default titles/specialties, the school's custom options, and
    the distinct values actually used by the school's employees.
    """
    from app.utils import employee_classification as ec
    school_id = school.id if school else None

    db_job_titles: list[str] = []
    db_pairs: list[tuple[str, str]] = []
    if school:
        rows = (db.session.query(Employee.job_title, Employee.department)
                .filter(Employee.school_id == school.id)
                .distinct().all())
        _seen_jt = set()
        for jt, dep in rows:
            if jt and jt not in _seen_jt:
                _seen_jt.add(jt)
                db_job_titles.append(jt)
            if jt or dep:
                db_pairs.append((jt or '', dep or ''))

    fctx = ec.build_filter_context(school_id, db_job_titles, db_pairs)
    return {
        'filter_job_titles':      fctx['job_titles'],
        'filter_spec_map':        fctx['spec_map'],
        'filter_all_specialties': fctx['all_specialties'],
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────────────────────────────────────────

@employees_bp.route('/')
@login_required
@permission_required('manage_employees')
def index():
    page   = request.args.get('page', 1, type=int)
    search = request.args.get('q', '')
    job_filter = request.args.get('job_title', '').strip()
    dep_filter = request.args.get('department', '').strip()
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
    # Server-side classification filters (exact match, scoped to this school).
    if job_filter:
        query = query.filter(Employee.job_title == job_filter)
    if dep_filter:
        query = query.filter(Employee.department == dep_filter)
    employees = (query.order_by(Employee.created_at.desc())
                 .paginate(page=page, per_page=20, error_out=False))
    filter_ctx = _employee_filter_context(school)
    return render_template('employees/index.html',
                           employees=employees, search=search,
                           filter_job=job_filter, filter_dep=dep_filter,
                           **filter_ctx)


@employees_bp.route('/search')
@login_required
@permission_required('manage_employees')
def search():
    """Debounced AJAX live-search endpoint – returns JSON for client-side rendering."""
    school = get_current_school()
    if not school:
        return jsonify({'items': [], 'total': 0, 'page': 1, 'pages': 0,
                        'has_next': False, 'has_prev': False,
                        'next_num': None, 'prev_num': None}), 200

    q      = request.args.get('q', '').strip()
    page   = request.args.get('page', 1, type=int)
    job_filter = request.args.get('job_title', '').strip()
    dep_filter = request.args.get('department', '').strip()

    query = Employee.query.filter_by(school_id=school.id)
    if q:
        like = f'%{q}%'
        query = query.filter(db.or_(
            Employee.full_name.ilike(like),
            Employee.employee_id.ilike(like),
            Employee.job_title.ilike(like),
            Employee.department.ilike(like),
        ))
    # Classification filters — exact match, already scoped to this school above.
    if job_filter:
        query = query.filter(Employee.job_title == job_filter)
    if dep_filter:
        query = query.filter(Employee.department == dep_filter)
    paginated = query.order_by(Employee.created_at.desc()).paginate(
        page=page, per_page=20, error_out=False)

    from app.utils.helpers import resolve_photo_url as _rpu
    items = []
    for e in paginated.items:
        items.append({
            'id':          e.id,
            'employee_id': e.employee_id,
            'full_name':   e.full_name,
            'job_title':   e.job_title or '',
            'department':  e.department or '—',
            'base_salary': e.base_salary,
            'status':      e.status,
            'photo_url':   _rpu(e.photo) or '',
            'view_url':    url_for('employees.view', emp_id=e.id),
            'edit_url':    url_for('employees.edit', emp_id=e.id),
        })

    return jsonify({
        'items':      items,
        'total':      paginated.total,
        'page':       paginated.page,
        'pages':      paginated.pages,
        'has_next':   paginated.has_next,
        'has_prev':   paginated.has_prev,
        'next_num':   paginated.next_num,
        'prev_num':   paginated.prev_num,
    })


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


@employees_bp.route('/<int:emp_id>/unlink-account', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_employees')
def unlink_account(emp_id):
    """Remove the login-account link from an employee — nothing else.

    Safety guarantees (why this is safe even when the linked account belongs to
    another school):
      • Employee.query.get_or_404 is tenant-scoped, so the operator can only ever
        reach an employee in their OWN school. A manager cannot target another
        school's employee.
      • The ONLY write is employee.user_id = None on that in-scope employee row.
        The linked User row is never loaded into the session as dirty, so it is
        never validated, updated, or deleted by this request — its username,
        password, role, school_id, and status are all left exactly as they were.
      • Because we do not mutate the (possibly cross-school) User, the tenant
        write-guard (_before_flush) has nothing cross-school to reject, so the
        request cannot 500 the way an in-place edit of a mismatched account would.
    """
    employee = Employee.query.get_or_404(emp_id)

    old_user_id = employee.user_id
    if not old_user_id:
        flash('لا يوجد حساب دخول مرتبط بهذا الموظف.', 'info')
        return redirect(url_for('employees.view', emp_id=employee.id))

    # Detach the reference only. Do NOT touch the User row in any way.
    employee.user_id = None
    db.session.commit()

    # Audit trail (scoped to the operator's school). log_action never raises and
    # commits its own row separately, so it cannot affect the unlink above.
    log_action('unlink_account', 'employee', employee.id,
               details=f'Unlinked login account (former user_id={old_user_id}); '
                       f'user record left unmodified.')

    flash('تم فك ربط حساب الدخول عن الموظف. لم يتم حذف أو تعديل حساب المستخدم.',
          'success')
    return redirect(url_for('employees.view', emp_id=employee.id))


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
@accountant_or_permission('manage_employees')
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
@accountant_or_permission('manage_employees')
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
@accountant_or_permission('manage_employees')
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
@accountant_or_permission('manage_employees')
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
@accountant_or_permission('manage_employees')
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
@accountant_or_permission('manage_employees')
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
                'status':      rec.status,
                'check_in':    rec.check_in.strftime('%H:%M')  if rec.check_in  else '',
                'check_out':   rec.check_out.strftime('%H:%M') if rec.check_out else '',
                'source':      rec.source or '',
                'notes':       rec.notes  or '',
                'is_on_leave': (rec.status == 'on_leave' and rec.source == 'leave'),
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
        _log.warning('[emp-manual-att] aborted — missing active academic year '
                     '(school_id=%s year=%s user_id=%s). Set a current academic year '
                     'for this school before recording employee attendance.',
                     getattr(school, 'id', None), getattr(year, 'id', None),
                     getattr(current_user, 'id', None))
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
        _log.warning('[emp-manual-att] no employee ids submitted '
                     '(school_id=%s date=%s user_id=%s raw_ids=%r) — nothing to save',
                     school.id, att_date, getattr(current_user, 'id', None), raw_ids)
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

    # Surface any submitted ids that did not resolve to an active employee in
    # this school: cross-school attempt, inactive employee, or stale form id.
    rejected_ids = [i for i in emp_ids if i not in emp_map]
    if rejected_ids:
        _log.warning('[emp-manual-att] %d submitted id(s) rejected — not an active '
                     'employee in school_id=%s (cross-school / inactive / stale): %r',
                     len(rejected_ids), school.id, rejected_ids)
    _log.info('[emp-manual-att] save start school_id=%s date=%s user_id=%s '
              'submitted=%d valid=%d', school.id, att_date,
              getattr(current_user, 'id', None), len(emp_ids), len(valid_ids))

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
    # Notification queue: tuples of (employee_obj, att_record, action).
    # Populated during the loop; flushed after a confirmed commit so that no
    # notification is ever sent if the transaction rolls back.
    _notify_queue = []

    for emp_id in emp_ids:
        if emp_id not in emp_map:
            continue  # Cross-school / inactive — already logged above, rejected

        status_choice = request.form.get(f'status_{emp_id}', 'absent').strip()
        if status_choice not in ('present', 'late', 'absent', 'on_leave'):
            _log.warning('[emp-manual-att] employee_id=%s skipped — invalid status %r '
                         '(school_id=%s date=%s)', emp_id, status_choice, school.id, att_date)
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
        # on_leave and absent: check_in_val and check_out_val remain None

        rec = existing.get(emp_id)
        if rec:
            # Actual manual selection always wins — including overriding on_leave
            # records created by the leave sync. Actual attendance takes priority.
            rec.status      = status_choice
            rec.check_in    = check_in_val
            rec.check_out   = check_out_val
            if notes_val is not None:
                rec.notes = notes_val
            rec.source      = 'manual'
            rec.recorded_by = current_user.id
            updated += 1
            # Status-update notification covers all manual changes (present, late,
            # absent, on_leave) — the employee is informed their record changed.
            _notify_queue.append((emp_map[emp_id], rec, 'status_update'))
        else:
            new_att = EmployeeAttendance(
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
            )
            db.session.add(new_att)
            created += 1
            # Only notify for actual attendance events (not absence / leave creation).
            if status_choice in ('present', 'late'):
                _notify_queue.append((emp_map[emp_id], new_att, 'check_in'))
            if check_out_val is not None:
                _notify_queue.append((emp_map[emp_id], new_att, 'check_out'))

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        _log.exception('[emp-manual-att] commit failed date=%s school_id=%s '
                       'created=%d updated=%d', att_date, school.id, created, updated)
        flash('حدث خطأ أثناء الحفظ. يرجى المحاولة مرة أخرى.', 'danger')
        return redirect(url_for('employees.manual_attendance', date=att_date.isoformat()))

    # Send notifications after confirmed commit.  Each call is individually
    # guarded so a single failure does not prevent the rest from being sent.
    if _notify_queue:
        from app.services.notifications import NotificationService
        for _emp_obj, _att_obj, _action in _notify_queue:
            try:
                NotificationService.send_employee_attendance_notification(
                    _emp_obj, _att_obj, _action, 'manual')
            except Exception:
                _log.exception('[emp-manual-att] notification error employee_id=%s action=%s',
                               _emp_obj.id, _action)

    _log.info('[emp-manual-att] save done school_id=%s date=%s created=%d updated=%d',
              school.id, att_date, created, updated)

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

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
                        AcademicYear, teacher_subjects)
from app.services import homework_publish
from app.utils.decorators import get_current_school, get_active_year
from app.utils.helpers import save_uploaded_file, resolve_photo_url
from app.utils.homework_attachments import (
    FIELD_REMOVE, HomeworkAttachmentError, HomeworkImageError,
    add_new_attachments, apply_attachment_edit, collect_homework_attachments,
    discard_unreferenced_uploads, load_extra_images, plan_attachment_edit,
    prepare_homework_attachments, prepare_homework_upload,
    store_homework_attachments)
from app.utils.institute_groups import (active_groups_for_form,
                                        institute_enabled, instructor_groups)

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
    """Allow teachers, admins, and any role granted manage_homework."""
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.role:
            abort(403)
        role_name = current_user.role.name
        if not (current_user.is_admin_user or role_name == 'teacher'
                or current_user.has_permission('manage_homework')):
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
    if current_user.is_admin_user:
        return True
    # Teachers keep their own (assignment-scoped) branch even if a permission
    # is ever added to their role; any OTHER role granted manage_homework gets
    # the admin-side behaviour — still strictly school-scoped by every query.
    if current_user.role and current_user.role.name == 'teacher':
        return False
    return current_user.has_permission('manage_homework')


def _hw_exists(school_id: int, year_id: int, teacher_id, subject_id,
               section_id, title: str, pub_dt, institute_group_id=None) -> bool:
    """True when an active homework with identical key fields already exists (duplicate guard).

    institute_group_id defaults to None, which is exactly what every school
    homework row carries, so the school duplicate check is unchanged.
    """
    return Homework.query.filter_by(
        school_id=school_id,
        academic_year_id=year_id,
        teacher_id=teacher_id,
        subject_id=subject_id or None,
        section_id=section_id,
        institute_group_id=institute_group_id,
        title=title,
        publish_date=pub_dt,
        is_active=True,
    ).first() is not None


def _belongs_to_school(model, ident: int, school_id: int) -> bool:
    """True when the Section / Subject ``ident`` is this school's, in any
    academic year. bypass_tenant_scope also drops the year filter on purpose
    (a historical homework keeps its own year's section); school_id is
    re-applied explicitly."""
    return (model.query.execution_options(bypass_tenant_scope=True)
            .filter_by(id=ident, school_id=school_id).first()) is not None


def _extra_images_of(hw, school_id: int) -> list:
    """Image #2/#3 rows of one homework — no query unless it has images."""
    if hw is None or hw.attachment_type != 'image':
        return []
    return load_extra_images([hw.id], school_id).get(hw.id, [])


def _get_school_and_year():
    school = get_current_school()
    if not school:
        return None, None
    year = get_active_year(school.id)
    return school, year


# ═════════════════════════════════════════════════════════════════════════════
#  INSTITUTE TARGETING  (School.is_institute only)
# ═════════════════════════════════════════════════════════════════════════════
#
# A school assignment targets a Section; that code path below is untouched.
# An institute assignment targets an InstituteStudyGroup instead, because an
# institute student belongs to several groups and carries no section.
#
# Everything an institute route is allowed to see or write flows through the
# ONE function _eligible_groups(), so there is a single authorization query
# rather than a slightly different condition per route. It reuses the helpers
# already written for the institute-groups blueprint — no second identity, no
# new permission and no new role: an instructor is resolved through the same
# Employee.user_id link every other teacher surface uses, and a manager is
# whoever _is_admin() already recognises.


def _hw_year(school, hw):
    """The academic year a stored homework belongs to.

    Editing must stay inside the homework's OWN year, not whichever year is
    currently active, otherwise a historical assignment would find no eligible
    group and silently become uneditable. Scoped by school_id, so a foreign
    year id can never be resolved.
    """
    if school is None or hw is None or not hw.academic_year_id:
        return None
    return AcademicYear.query.filter_by(id=hw.academic_year_id,
                                        school_id=school.id).first()


def _eligible_groups(school, year, is_admin):
    """ACTIVE study groups this user may target, for THIS institute and year.

    * institute manager  → every active group of the institute in that year
    * institute instructor → only the groups assigned to their Employee row
    * anything else (a school, no year, an unlinked account) → []

    Returning [] rather than a wider set is what makes every caller fail
    closed: a posted group id is accepted only when it is a key of this list.
    """
    if not institute_enabled(school) or year is None:
        return []
    if is_admin:
        return active_groups_for_form(school.id, year.id)
    return instructor_groups(school, current_user, year)


def _resolve_posted_group(school, year, is_admin, raw_group_id):
    """(group, error) for a posted institute_group_id.

    The id is matched against the freshly recomputed eligible set, so ONE check
    covers all of: the institution is an institute, the group belongs to it,
    the group belongs to the right academic year, the group is active, and the
    instructor owns it unless they hold the management permission. A forged,
    foreign, ended or nonexistent id yields the same message and discloses
    nothing about whether it exists elsewhere.
    """
    if not raw_group_id:
        return None, 'المجموعة الدراسية مطلوبة.'
    allowed = {g.id: g for g in _eligible_groups(school, year, is_admin)}
    group = allowed.get(raw_group_id)
    if group is None:
        return None, ('المجموعة الدراسية المحددة غير صالحة أو غير مرتبطة '
                      'بحسابك. لم يتم حفظ أي تغيير.')
    return group, None


def _no_groups_message(is_admin: bool) -> str:
    return ('لا توجد مجموعات دراسية مفعّلة في هذا المعهد للعام الدراسي الحالي. '
            'أضف مجموعة أولاً.') if is_admin else (
        'لا توجد مجموعات دراسية مسندة إلى حسابك. لا يمكن إنشاء واجب. '
        'راجع إدارة المعهد لإسناد مجموعة إليك.')


def _notify_institute_homework_parents(hw: Homework, school_id: int, title: str,
                                       body: str, created_by=None) -> None:
    """Institute homework: in-app rows + ONE queued FCM batch for the linked
    parents of the students holding an ACTIVE enrollment in
    hw.institute_group_id. Never a grade, a section, the whole institute or
    another instructor's students.

    Delegates to the mobile teacher infrastructure's _notify_parents_batched,
    so there is a single batched parent flow for institute homework whether it
    was created on the web (instructor or administration) or from the mobile
    teacher API:

      * audience: _active_audience_student_ids() — the group's active
        enrollments, re-filtered by school_id and status='active';
      * recipients: one row per parent (siblings collapse), only parents whose
        own account belongs to this school and is active — a corrupt link to
        another school's parent is skipped, never notified;
      * dedup: scoped to THIS homework (created_at), one query — an older
        homework with identical text never suppresses this one;
      * FCM: handed to send_push_batch through async_dispatch after the rows
        commit — no Firebase call inside the request.

    The payload carries the group, never a section (an institute row has none);
    the helper adds the parent's own student_id. Never raises.
    """
    import logging
    try:
        from app.blueprints.mobile_api.teacher import (
            _active_audience_student_ids, _notify_parents_batched,
            institute_homework_fcm_data)
        _notify_parents_batched(
            school_id=school_id,
            student_ids=_active_audience_student_ids(
                school_id, None, hw.institute_group_id),
            title=title,
            body=body,
            ntype='homework',
            fcm_data=institute_homework_fcm_data(hw.id, hw.institute_group_id),
            active_parents_only=True,
            created_by=created_by,
            dedup_since=hw.created_at,
            log_tag=f'[homework-notify] institute hw_id={hw.id}',
        )
    except Exception:
        db.session.rollback()
        logging.getLogger('mecha.homework').exception(
            '[homework-notify] institute notify failed homework_id=%s', hw.id)


def _notify_new_homework(hws, school_id: int, marker: dict, created_by=None) -> None:
    """Notify parents about newly COMMITTED homework rows. Never raises.

    `marker` is the homework_publish.pending_marker_kwargs() the rows were
    inserted with, so the insert and the notification path always agree:
      * deferred (marker set): durable fan-out (in-app rows + outbox push
        jobs, one transaction) of every row already published on the
        school-local date; a future row stays pending for the outbox worker's
        sweep; a failure stays pending and the sweep recovers it;
      * otherwise: the original immediate path, unchanged.
    """
    if marker:
        homework_publish.notify_after_commit(
            [hw.id for hw in hws],
            created_by=(created_by if created_by is not None
                        else current_user.id if current_user.is_authenticated
                        else None))
        return
    for hw in hws:
        try:
            _notify_homework_parents(hw, school_id, created_by)
        except Exception as exc:
            current_app.logger.warning(
                '[homework] notify failed hw_id=%s: %s', hw.id, exc)


def _notify_homework_parents(hw: Homework, school_id: int, created_by=None) -> None:
    """Create in-app Notification rows + FCM push for the parents of the
    students this homework ACTUALLY targets.

    School homework  → the active students of hw.section_id.
    Institute homework → _notify_institute_homework_parents() (batched, push
    queued off the request).

    Both delegate to the mobile teacher infrastructure's _notify_parents_batched,
    so web and mobile creation share ONE recipient rule: linked parents whose own
    account is in this school and active (a cross-school or disabled account is
    skipped), one row + one push per parent (siblings collapse, carrying the
    lowest linked student_id), dedup scoped to THIS homework (created_at) so an
    older homework with the same text never suppresses this one. Rows commit
    first; the push is queued after the commit. `created_by` falls back to
    current_user (the mobile teacher API passes its JWT user). Never raises.
    """
    import logging
    _hw_log = logging.getLogger('mecha.homework')

    subject_name = hw.subject.name if hw.subject else 'غير محدد'
    title = 'واجب جديد'
    body  = f'تم إضافة واجب جديد في مادة {subject_name}: {hw.title}'

    _hw_log.info('[homework-notify] homework_id=%s section_id=%s group_id=%s title=%r',
                 hw.id, hw.section_id, hw.institute_group_id, hw.title)

    if created_by is None and current_user.is_authenticated:
        created_by = current_user.id

    if hw.institute_group_id:
        _notify_institute_homework_parents(hw, school_id, title, body, created_by)
        return

    if not hw.section_id:
        # No target at all (a legacy row whose section was deleted). Notify
        # nobody rather than guessing an audience.
        return

    try:
        from app.blueprints.mobile_api.teacher import (
            _active_audience_student_ids, _notify_parents_batched)
        _notify_parents_batched(
            school_id=school_id,
            student_ids=_active_audience_student_ids(school_id, hw.section_id, None),
            title=title,
            body=body,
            ntype='homework',
            # The unchanged school payload; the helper adds each parent's
            # own student_id.
            fcm_data={
                'type':        'homework',
                'ntype':       'homework',
                'route':       '/parent/homework',
                'homework_id': str(hw.id),
                'section_id':  str(hw.section_id),
                'screen':      'homework',
            },
            active_parents_only=True,
            created_by=created_by,
            dedup_since=hw.created_at,
            log_tag=f'[homework-notify] hw_id={hw.id}',
        )
    except Exception:
        db.session.rollback()
        _hw_log.exception('[homework-notify] notify failed homework_id=%s', hw.id)


# ═════════════════════════════════════════════════════════════════════════════
#  Routes
# ═════════════════════════════════════════════════════════════════════════════

HOMEWORK_PAGE_SIZE = 20


def _paginate_index(q):
    """One page of the (already scoped, filtered and ordered) index query.

    LIMIT/OFFSET and the count run in the database. A page past the end falls
    back to the last page rather than showing an empty list.
    """
    page = request.args.get('page', 1, type=int) or 1
    pagination = q.paginate(page=max(page, 1), per_page=HOMEWORK_PAGE_SIZE,
                            error_out=False)
    if not pagination.items and pagination.pages and page > pagination.pages:
        pagination = q.paginate(page=pagination.pages, per_page=HOMEWORK_PAGE_SIZE,
                                error_out=False)
    return pagination


@homework_bp.route('/', methods=['GET'])
@homework_access_required
def index():
    school, year = _get_school_and_year()
    if not school or not year:
        flash('لا يوجد عام دراسي نشط.', 'warning')
        return render_template('homework/index.html', homework_list=[], emp=None,
                               is_admin=_is_admin(),
                               filter_grades=[], filter_sections=[], filter_subjects=[],
                               filter_stages=[], filter_groups=[],
                               is_institute=institute_enabled(school),
                               f_grade_id=None, f_section_id=None,
                               f_subject_id=None, f_stage='', f_group_id=None)

    emp = _get_employee()
    is_admin = _is_admin()
    is_institute = institute_enabled(school)

    # ── Build filter dropdown data (same scope as teacher/admin access) ───
    # An institute filters by study group; Section/Grade/Stage do not apply and
    # are deliberately left empty so the school controls disappear entirely.
    filter_groups = _eligible_groups(school, year, is_admin) if is_institute else []
    if is_institute:
        filter_sections, filter_grades = [], []
        filter_subjects = []
        seen_subject_ids = set()
        for g in filter_groups:
            if g.subject and g.subject.id not in seen_subject_ids:
                seen_subject_ids.add(g.subject.id)
                filter_subjects.append(g.subject)
        filter_subjects.sort(key=lambda x: x.name or '')
    elif is_admin:
        filter_sections = Section.query.filter_by(
            school_id=school.id, academic_year_id=year.id).order_by(Section.name).all()
        filter_subjects = Subject.query.filter_by(
            school_id=school.id, academic_year_id=year.id).order_by(Subject.name).all()
        filter_grades = Grade.query.filter_by(
            school_id=school.id, academic_year_id=year.id).order_by(Grade.name).all()
    elif emp:
        sec_ids  = _teacher_section_ids(emp)
        subj_ids = _teacher_subject_ids(emp)
        filter_sections = (Section.query.filter(Section.id.in_(sec_ids))
                           .order_by(Section.name).all() if sec_ids else [])
        filter_subjects = (Subject.query.filter(Subject.id.in_(subj_ids))
                           .order_by(Subject.name).all() if subj_ids else [])
        filter_grades = _teacher_grades(filter_sections)
    else:
        filter_sections, filter_subjects, filter_grades = [], [], []

    filter_stages = sorted({g.stage for g in filter_grades if g.stage})

    # ── Parse filter params ───────────────────────────────────────────────
    f_stage      = request.args.get('f_stage', '').strip()
    f_grade_id   = request.args.get('f_grade_id', type=int)
    f_section_id = request.args.get('f_section_id', type=int)
    f_subject_id = request.args.get('f_subject_id', type=int)

    # An institute never accepts a section/grade filter, so a probe using those
    # URL params is dropped before it can reach a query.
    f_group_id = request.args.get('f_group_id', type=int) if is_institute else None
    if is_institute:
        f_stage, f_grade_id, f_section_id = '', None, None
        if f_group_id and f_group_id not in {g.id for g in filter_groups}:
            f_group_id = None

    # Validate that filter values actually belong to this school/year
    # (prevents cross-school probing via URL params)
    if f_section_id and not Section.query.filter_by(
            id=f_section_id, school_id=school.id, academic_year_id=year.id).first():
        f_section_id = None
    if f_grade_id and not Grade.query.filter_by(
            id=f_grade_id, school_id=school.id, academic_year_id=year.id).first():
        f_grade_id = None
    if f_subject_id and not Subject.query.filter_by(
            id=f_subject_id, school_id=school.id, academic_year_id=year.id).first():
        f_subject_id = None

    # ── Base query ────────────────────────────────────────────────────────
    q = (Homework.query
         .filter_by(school_id=school.id, academic_year_id=year.id, is_active=True)
         .order_by(Homework.publish_date.desc(), Homework.id.desc()))

    if not is_admin and emp:
        # Teachers see only their own homework
        q = q.filter_by(teacher_id=emp.id)
    elif not is_admin and not emp:
        q = q.filter(False)  # no employee record → nothing visible

    # ── Institute: only ever group-targeted homework, only eligible groups ──
    if is_institute:
        eligible_ids = [g.id for g in filter_groups]
        if not is_admin:
            # An instructor's list is bounded by their own groups as well as by
            # teacher_id above, so an assignment retargeted to a group they lost
            # stops being listed immediately.
            q = (q.filter(Homework.institute_group_id.in_(eligible_ids))
                 if eligible_ids else q.filter(False))
        else:
            q = q.filter(Homework.institute_group_id.isnot(None))
        if f_group_id:
            q = q.filter(Homework.institute_group_id == f_group_id)
        if f_subject_id:
            q = q.filter(Homework.subject_id == f_subject_id)
        pagination = _paginate_index(q)
        return render_template('homework/index.html',
                               homework_list=pagination.items,
                               pagination=pagination,
                               emp=emp,
                               is_admin=is_admin,
                               is_institute=True,
                               resolve_photo_url=resolve_photo_url,
                               filter_grades=[], filter_sections=[],
                               filter_subjects=filter_subjects,
                               filter_stages=[], filter_groups=filter_groups,
                               f_grade_id=None, f_section_id=None,
                               f_subject_id=f_subject_id,
                               f_stage='', f_group_id=f_group_id)

    # ── Apply location filters (section takes precedence over grade) ──────
    if f_section_id:
        q = q.filter(Homework.section_id == f_section_id)
    elif f_grade_id:
        grade_sec_ids = [s.id for s in Section.query.filter_by(
            grade_id=f_grade_id, school_id=school.id, academic_year_id=year.id).all()]
        q = (q.filter(Homework.section_id.in_(grade_sec_ids))
             if grade_sec_ids else q.filter(False))

    # ── Apply subject filter (independent of location filter) ─────────────
    if f_subject_id:
        q = q.filter(Homework.subject_id == f_subject_id)

    pagination = _paginate_index(q)
    # Image #2/#3 of every image homework on this page: ONE query, none when
    # the page has no image homework (legacy rows simply have no extra rows).
    extra_images = load_extra_images(
        [h.id for h in pagination.items if h.attachment_type == 'image'], school.id)
    return render_template('homework/index.html',
                           homework_list=pagination.items,
                           pagination=pagination,
                           extra_images=extra_images,
                           emp=emp,
                           is_admin=is_admin,
                           resolve_photo_url=resolve_photo_url,
                           filter_grades=filter_grades,
                           filter_sections=filter_sections,
                           filter_subjects=filter_subjects,
                           filter_stages=filter_stages,
                           filter_groups=[],
                           is_institute=False,
                           f_grade_id=f_grade_id,
                           f_section_id=f_section_id,
                           f_subject_id=f_subject_id,
                           f_stage=f_stage,
                           f_group_id=None)


@homework_bp.route('/create', methods=['GET', 'POST'])
@homework_access_required
def create():
    school, year = _get_school_and_year()
    if not school or not year:
        flash('لا يوجد عام دراسي نشط.', 'warning')
        return redirect(url_for('homework.index'))

    emp = _get_employee()
    is_admin = _is_admin()
    is_institute = institute_enabled(school)

    # An institute targets a study group. Section / Grade / Subject selectors do
    # not apply: the subject is derived from the group on the server, so no
    # independent subject list is offered and none is accepted.
    groups = _eligible_groups(school, year, is_admin) if is_institute else []
    if is_institute and not groups:
        flash(_no_groups_message(is_admin), 'warning')
        return redirect(url_for('homework.index'))

    # Build dropdown data scoped to teacher (or all for admin)
    if is_institute:
        sections, subjects = [], []
    elif is_admin:
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

    if is_institute:
        grades = []
    else:
        grades = _teacher_grades(sections) if not is_admin else (
            Grade.query.filter_by(school_id=school.id, academic_year_id=year.id)
                       .order_by(Grade.name).all()
        )
    stages = sorted({g.stage for g in grades if g.stage})

    if not is_institute and not is_admin and not sections:
        flash('لا توجد شعب مرتبطة بحسابك. لا يمكن إنشاء واجب.', 'warning')
        return redirect(url_for('homework.index'))

    def _re_render(errors_list):
        for e in errors_list:
            flash(e, 'danger')
        # request.form is echoed back so a validation failure does not wipe the
        # selected group; it is redisplay only — the authoritative check runs
        # again against _eligible_groups() on the next POST.
        return render_template('homework/form.html',
                               sections=sections, subjects=subjects,
                               grades=grades, stages=stages,
                               groups=groups, is_institute=is_institute,
                               posted=request.form,
                               hw=None, today=date.today())

    if request.method == 'POST':
        from datetime import datetime as _dt

        title        = request.form.get('title', '').strip()
        subject_id   = request.form.get('subject_id', type=int)
        grade_id     = request.form.get('grade_id', type=int)
        section_raw  = request.form.get('section_id', '').strip()
        publish_date = request.form.get('publish_date', '')
        due_date_str = request.form.get('due_date', '')
        description  = request.form.get('description', '').strip()
        file         = request.files.get('attachment')

        institute_group_id = request.form.get('institute_group_id', type=int)

        # Batch («جميع الشعب») is a school-only concept.
        batch_mode = (not is_institute) and section_raw == 'all'
        try:
            section_id = int(section_raw) if not batch_mode and section_raw else None
        except ValueError:
            section_id = None

        # ── Validation ──────────────────────────────────────────────────────
        errors = []
        batch_sections: list = []

        if not title:
            errors.append('عنوان الواجب مطلوب.')
        if not publish_date:
            errors.append('تاريخ النشر مطلوب.')
        if not due_date_str:
            errors.append('تاريخ التسليم مطلوب.')

        pub_dt = due_dt = None
        if publish_date:
            try:
                pub_dt = _dt.strptime(publish_date, '%Y-%m-%d').date()
            except ValueError:
                errors.append('صيغة تاريخ النشر غير صحيحة.')
        if due_date_str:
            try:
                due_dt = _dt.strptime(due_date_str, '%Y-%m-%d').date()
            except ValueError:
                errors.append('صيغة تاريخ التسليم غير صحيحة.')
        if pub_dt and due_dt and due_dt < pub_dt:
            errors.append('تاريخ التسليم يجب ألا يكون قبل تاريخ النشر.')

        # ── Target validation ─────────────────────────────────────
        group = None
        if is_institute:
            # ONE check covers: this is an institute, the group belongs to it,
            # to the right academic year, is active, and is assigned to this
            # instructor unless they hold the management permission.
            group, group_err = _resolve_posted_group(school, year, is_admin,
                                                     institute_group_id)
            if group_err:
                errors.append(group_err)
            else:
                # The group's stored subject_id is authoritative. Any posted
                # subject_id / grade_id / section_id / teacher / school /
                # academic-year value is discarded here, so none of them can be
                # forged into the saved row.
                subject_id = group.subject_id
                section_id = None
        elif batch_mode:
            if not grade_id:
                errors.append('الصف مطلوب عند اختيار «جميع الشعب».')
            else:
                grade_obj = Grade.query.filter_by(
                    id=grade_id, school_id=school.id, academic_year_id=year.id
                ).first()
                if not grade_obj:
                    errors.append('الصف المحدد غير صالح أو لا ينتمي إلى هذه المدرسة.')
                else:
                    cand = Section.query.filter_by(
                        grade_id=grade_id, school_id=school.id, academic_year_id=year.id
                    ).order_by(Section.name).all()
                    if not cand:
                        errors.append(
                            f'الصف «{grade_obj.name}» لا يحتوي على شعب. '
                            'لا يمكن إنشاء الواجب لجميع الشعب.'
                        )
                    else:
                        if not is_admin and emp:
                            auth_ids = _teacher_section_ids(emp)
                            cand = [s for s in cand if s.id in auth_ids]
                        if not cand:
                            errors.append('لا توجد شعب مخوّل لك في هذا الصف.')
                        else:
                            batch_sections = cand
        else:
            if not section_id:
                errors.append('الشعبة مطلوبة.')
            else:
                sec_obj = Section.query.filter_by(
                    id=section_id, school_id=school.id, academic_year_id=year.id
                ).first()
                if not sec_obj:
                    errors.append('الشعبة المحددة غير صالحة.')
                elif not is_admin and emp and section_id not in _teacher_section_ids(emp):
                    errors.append('لا يمكنك تعيين واجب لشعبة غير مرتبطة بك.')
                elif grade_id and sec_obj.grade_id != grade_id:
                    errors.append('الشعبة المحددة لا تنتمي إلى الصف المحدد.')

        # ── Subject validation (school only — an institute derives it) ───────
        if subject_id and not is_institute:
            subj_obj = Subject.query.filter_by(
                id=subject_id, school_id=school.id, academic_year_id=year.id
            ).first()
            if not subj_obj:
                errors.append('المادة المحددة غير صالحة.')
            elif not is_admin and emp and subject_id not in _teacher_subject_ids(emp):
                errors.append('لا يمكنك تعيين واجب لمادة غير مرتبطة بك.')
            elif grade_id and subj_obj.grade_id and subj_obj.grade_id != grade_id:
                errors.append('المادة المحددة لا تنتمي إلى الصف المحدد.')

        # ── Attachment validation ────────────────────────────────────────────
        attach_path = attach_type = None
        upload = None
        prepared = None
        if is_institute:
            # Institute: the original single attachment, unchanged. Images are
            # validated from their bytes and optimised to WebP here, before
            # any Storage write; PDFs pass through unchanged.
            if file and file.filename:
                ext = _ext(file.filename)
                if ext not in _ATTACH_ALL:
                    errors.append('نوع الملف غير مسموح. الأنواع المقبولة: jpg, jpeg, png, webp, pdf.')
                else:
                    attach_type = _attachment_type(file.filename)
                    if not errors:
                        try:
                            upload = prepare_homework_upload(file)
                        except HomeworkImageError as exc:
                            errors.append(str(exc))
        else:
            # School: up to 3 images OR one PDF. Counts and raw sizes are
            # checked first; every image is decoded and optimised before the
            # first Storage write, so a bad image #2 stores nothing.
            try:
                incoming = collect_homework_attachments(request.files)
                if not errors:
                    prepared = prepare_homework_attachments(incoming)
            except HomeworkAttachmentError as exc:
                errors.append(exc.message)

        if errors:
            return _re_render(errors)

        # ── Save attachment ──────────────────────────────────────────────────
        # School images are stored ONCE even for «جميع الشعب»; every generated
        # row references the same objects (homework_images.file_path is not
        # unique for exactly this reason).
        stored_paths: list[str] = []
        if upload is not None:
            attach_path = save_uploaded_file(upload, subfolder='homework',
                                             allowed_exts=_ATTACH_ALL)
        elif prepared is not None and prepared.blobs:
            try:
                stored_paths = store_homework_attachments(prepared)
            except HomeworkAttachmentError as exc:
                return _re_render([exc.message])
            attach_path, attach_type = stored_paths[0], prepared.mode

        def _record_extra_images(rows):
            # Image #2/#3 rows need the homework ids; one flush for the batch.
            if len(stored_paths) > 1:
                db.session.flush()
                for row in rows:
                    add_new_attachments(row, prepared.mode, stored_paths)

        teacher_id = emp.id if emp else None
        # Pending (NULL) only in deferred mode; otherwise the column default
        # stamps the rows handled and the immediate path notifies.
        marker = homework_publish.pending_marker_kwargs()

        # ── Persist ─────────────────────────────────────────────────────────
        try:
            if batch_mode:
                created_hws = []
                skipped = 0
                for sec in batch_sections:
                    if _hw_exists(school.id, year.id, teacher_id, subject_id, sec.id, title, pub_dt):
                        skipped += 1
                        continue
                    hw = Homework(
                        school_id=school.id,
                        academic_year_id=year.id,
                        teacher_id=teacher_id,
                        subject_id=subject_id or None,
                        section_id=sec.id,
                        title=title,
                        description=description or None,
                        publish_date=pub_dt,
                        due_date=due_dt,
                        attachment_path=attach_path,
                        attachment_type=attach_type,
                        is_active=True,
                        **marker,
                    )
                    db.session.add(hw)
                    created_hws.append(hw)

                _record_extra_images(created_hws)
                db.session.commit()

                created = len(created_hws)
                if not created:
                    # Every section was a duplicate: nothing references the
                    # files this request stored.
                    discard_unreferenced_uploads(stored_paths)
                if skipped and not created:
                    flash('الواجب موجود بالفعل لجميع الشعب المحددة.', 'warning')
                elif skipped:
                    flash(
                        f'تم إنشاء الواجب لـ {created} شعبة. '
                        f'تم تخطي {skipped} شعبة (واجب مكرر).',
                        'success',
                    )
                else:
                    flash('تم إنشاء الواجب لجميع شعب الصف بنجاح.', 'success')

                _notify_new_homework(created_hws, school.id, marker)

            else:
                target_group_id = group.id if group else None
                if _hw_exists(school.id, year.id, teacher_id, subject_id,
                              section_id, title, pub_dt, target_group_id):
                    discard_unreferenced_uploads(stored_paths)
                    flash('هذا الواجب موجود بالفعل لهذه المجموعة الدراسية.'
                          if is_institute else
                          'هذا الواجب موجود بالفعل لهذه الشعبة.', 'warning')
                    return redirect(url_for('homework.index'))

                # Exactly one target: an institute row carries the group and no
                # section, a school row carries the section and no group.
                hw = Homework(
                    school_id=school.id,
                    academic_year_id=year.id,
                    teacher_id=teacher_id,
                    subject_id=subject_id or None,
                    section_id=None if is_institute else (section_id or None),
                    institute_group_id=target_group_id,
                    title=title,
                    description=description or None,
                    publish_date=pub_dt,
                    due_date=due_dt,
                    attachment_path=attach_path,
                    attachment_type=attach_type,
                    is_active=True,
                    **marker,
                )
                db.session.add(hw)
                _record_extra_images([hw])
                db.session.commit()

                _notify_new_homework([hw], school.id, marker)

                flash('تم إضافة الواجب بنجاح.', 'success')

        except Exception:
            db.session.rollback()
            current_app.logger.exception('[homework] create failed')
            discard_unreferenced_uploads(stored_paths)
            flash('حدث خطأ أثناء حفظ الواجب. يرجى المحاولة مجدداً.', 'danger')
            return render_template('homework/form.html',
                                   sections=sections, subjects=subjects,
                                   grades=grades, stages=stages,
                                   groups=groups, is_institute=is_institute,
                                   posted=request.form,
                                   hw=None, today=date.today())

        return redirect(url_for('homework.index'))

    return render_template('homework/form.html',
                           sections=sections, subjects=subjects, grades=grades,
                           stages=stages, groups=groups,
                           is_institute=is_institute, posted=None,
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

    is_institute = institute_enabled(school)
    # Eligibility is resolved against the homework's OWN academic year, not the
    # currently active one, so a historical assignment stays editable instead of
    # silently losing every candidate group.
    hw_year = _hw_year(school, hw) if is_institute else None
    groups = _eligible_groups(school, hw_year, is_admin) if is_institute else []
    if is_institute:
        # An institute row must already point at a group this user may target,
        # otherwise the record is out of scope. 404, not 403, so an instructor
        # learns nothing about whose assignment it is.
        if not hw.institute_group_id:
            abort(404)
        if hw.institute_group_id not in {g.id for g in groups}:
            abort(404)

    if is_institute:
        sections, subjects = [], []
    elif is_admin:
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

    if is_institute:
        grades = []
    else:
        grades = _teacher_grades(sections) if not is_admin else (
            Grade.query.filter_by(school_id=school.id, academic_year_id=year.id)
                       .order_by(Grade.name).all()
        )
    stages = sorted({g.stage for g in grades if g.stage})

    if request.method == 'POST':
        title        = request.form.get('title', '').strip()
        subject_id   = request.form.get('subject_id', type=int)
        section_id   = request.form.get('section_id', type=int)
        institute_group_id = request.form.get('institute_group_id', type=int)
        publish_date = request.form.get('publish_date', '')
        due_date_str = request.form.get('due_date', '')
        description  = request.form.get('description', '').strip()
        file         = request.files.get('attachment')
        clear_attach = request.form.get('clear_attachment') == '1'

        errors = []
        group = None
        if not title:
            errors.append('عنوان الواجب مطلوب.')
        if is_institute:
            # Re-validated from scratch against the eligible set. A forged id
            # never retargets the stored row: on failure nothing is written.
            group, group_err = _resolve_posted_group(school, hw_year, is_admin,
                                                     institute_group_id)
            if group_err:
                errors.append(group_err)
            else:
                subject_id = group.subject_id   # derived, never posted
                section_id = None
        elif not section_id:
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

        if not is_institute and not is_admin and emp:
            if section_id and section_id not in _teacher_section_ids(emp):
                errors.append('لا يمكنك تعيين واجب لشعبة غير مرتبطة بك.')
            if subject_id and subject_id not in _teacher_subject_ids(emp):
                errors.append('لا يمكنك تعيين واجب لمادة غير مرتبطة بك.')
        if not is_institute:
            # Every role, admins included: a posted section / subject must be
            # this school's (any of its years — the homework may be historical).
            if section_id and not _belongs_to_school(Section, section_id, school.id):
                errors.append('الشعبة المحددة غير صالحة.')
            if subject_id and not _belongs_to_school(Subject, subject_id, school.id):
                errors.append('المادة المحددة غير صالحة.')

        new_attach_path = new_attach_type = None
        upload = None
        keep = None          # school: None = attachments untouched by this edit
        extras: list = []
        prepared = None
        if is_institute:
            # Institute: the original single attachment, unchanged.
            if file and file.filename:
                ext = _ext(file.filename)
                if ext not in _ATTACH_ALL:
                    errors.append('نوع الملف غير مسموح. الأنواع المقبولة: jpg, jpeg, png, webp, pdf.')
                else:
                    new_attach_type = _attachment_type(file.filename)
                    if not errors:
                        try:
                            upload = prepare_homework_upload(file)
                        except HomeworkImageError as exc:
                            errors.append(str(exc))
        else:
            # School: the attachment logic runs ONLY when this request
            # explicitly adds, removes or clears attachments. A title / text /
            # date edit never reads, re-encodes or rewrites the attachment.
            try:
                incoming = collect_homework_attachments(request.files)
                remove_tokens = request.form.getlist(FIELD_REMOVE)
                if incoming.mode or remove_tokens or clear_attach:
                    # Lock the row and plan against its committed state, so two
                    # concurrent edits cannot both pass the 3-image limit.
                    db.session.refresh(hw, with_for_update=True)
                    extras = load_extra_images([hw.id], school.id).get(hw.id, [])
                keep = plan_attachment_edit(hw, extras, incoming, remove_tokens,
                                            clear_attach)
                if keep is not None and not errors:
                    prepared = prepare_homework_attachments(incoming)
            except HomeworkAttachmentError as exc:
                errors.append(exc.message)

        def _form_error(messages):
            for e in messages:
                flash(e, 'danger')
            return render_template('homework/form.html',
                                   sections=sections, subjects=subjects,
                                   grades=grades, stages=stages,
                                   groups=groups, is_institute=is_institute,
                                   posted=request.form,
                                   extra_images=([] if is_institute
                                                 else _extra_images_of(hw, school.id)),
                                   hw=hw, today=date.today())

        if errors:
            return _form_error(errors)

        stored_paths: list[str] = []
        if upload is not None:
            new_attach_path = save_uploaded_file(upload, subfolder='homework',
                                                 allowed_exts=_ATTACH_ALL)
        elif prepared is not None and prepared.blobs:
            try:
                stored_paths = store_homework_attachments(prepared)
            except HomeworkAttachmentError as exc:
                return _form_error([exc.message])

        # school_id, academic_year_id and teacher_id are never reassigned here,
        # so an edit cannot move an assignment to another institution, year or
        # owner. Exactly one target survives the write.
        hw.title       = title
        hw.subject_id  = subject_id or None
        hw.section_id  = None if is_institute else (section_id or None)
        if is_institute:
            hw.institute_group_id = group.id
        hw.publish_date = pub_dt
        hw.due_date    = due_dt
        hw.description = description or None
        if is_institute:
            if new_attach_path:
                hw.attachment_path = new_attach_path
                hw.attachment_type = new_attach_type
            elif clear_attach:
                hw.attachment_path = None
                hw.attachment_type = None
        elif keep is not None:
            apply_attachment_edit(hw, extras, keep,
                                  prepared.mode if prepared else None, stored_paths)

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            if is_institute:
                raise
            current_app.logger.exception('[homework] edit failed hw_id=%s', hw_id)
            discard_unreferenced_uploads(stored_paths)
            flash('حدث خطأ أثناء حفظ الواجب. يرجى المحاولة مجدداً.', 'danger')
            return redirect(url_for('homework.edit', hw_id=hw_id))
        # A still-PENDING homework (deferred mode) whose publish date is now
        # today or earlier is announced now; a future one stays pending. A
        # homework already notified is never announced again by an edit.
        if hw.parents_notified_at is None:
            homework_publish.notify_after_commit([hw.id], created_by=current_user.id)
        flash('تم تحديث الواجب بنجاح.', 'success')
        return redirect(url_for('homework.index'))

    return render_template('homework/form.html',
                           sections=sections, subjects=subjects, grades=grades,
                           stages=stages, groups=groups,
                           is_institute=is_institute, posted=None,
                           extra_images=([] if is_institute
                                         else _extra_images_of(hw, school.id)),
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

    # An institute assignment can only be deleted from inside its own group
    # scope, resolved against the homework's own academic year.
    if institute_enabled(school) and hw.institute_group_id:
        if hw.institute_group_id not in {
                g.id for g in _eligible_groups(school, _hw_year(school, hw),
                                               is_admin)}:
            abort(404)

    db.session.delete(hw)
    db.session.commit()
    flash('تم حذف الواجب.', 'success')
    return redirect(url_for('homework.index'))

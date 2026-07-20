"""Mecha-School — Students Blueprint  (Phase 6: multi-tenant + capacity check)"""
from decimal import Decimal, InvalidOperation

from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, abort, jsonify, session)
from flask_login import login_required, current_user
from sqlalchemy.orm import joinedload
from app.models import (db, Student, Section, Grade, AcademicYear, StudentDocument,
                        parent_students, User, Role, AttendanceDevice, DeviceStudentMapping,
                        FeeRecord, FeeInstallment, FeeType, Revenue, RevenueCategory,
                        ResidentialArea)
from app.utils.decorators import (permission_required, get_teacher_section_ids,
                                   get_current_school, get_active_year, get_view_year,
                                   historical_guard)
from app.utils.helpers import save_uploaded_file, resolve_photo_url
from app.utils import code_generator
from app.utils.features import feature_required, is_feature_enabled
from app.utils.student_form_config import get_student_form_config
from app.utils.buildings import (
    school_buildings_enabled, get_active_buildings,
    apply_building_scope_to_students, user_allowed_building_ids,
    user_can_access_student, validate_building_for_school,
)
from app.utils.audit import log_action

students_bp = Blueprint('students', __name__,
                         template_folder='../../templates/students')


def _is_teacher():
    return (current_user.is_authenticated and
            current_user.role and
            current_user.role.name == 'teacher')


# ─── Residential areas helpers (school-scoped lookup list) ───────────────────
# Same pattern as app/utils/buildings.py: bypass the automatic tenant criteria
# and filter by the trusted server-side school id explicitly, so the queries
# behave identically for school users and for a super admin with an active
# school selected. Never trust a client-provided area id without these checks.

def _school_residential_areas(school_id, active_only=False):
    """All ResidentialArea rows of ONE school, ordered by name."""
    if not school_id:
        return []
    q = (ResidentialArea.query
         .execution_options(bypass_tenant_scope=True)
         .filter_by(school_id=school_id))
    if active_only:
        q = q.filter_by(is_active=True)
    return q.order_by(ResidentialArea.name).all()


def _validate_residential_area_for_school(area_id, school_id):
    """Return area_id if it is an active area of the given school, else None.

    Fail-closed: a manipulated id belonging to another school (or an inactive
    area) resolves to None = "no area".
    """
    if not area_id or not school_id:
        return None
    area = (ResidentialArea.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=area_id, school_id=school_id, is_active=True)
            .first())
    return area.id if area else None


def _get_residential_area_or_404(area_id, school):
    """Load an area scoped by id + school_id — a foreign school's id is a 404."""
    area = (ResidentialArea.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=area_id, school_id=school.id)
            .first())
    if area is None:
        abort(404)
    return area


@students_bp.route('/')
@login_required
@permission_required('view_students')
def index():
    page        = request.args.get('page', 1, type=int)
    search      = request.args.get('q', '')
    status      = request.args.get('status', '')
    stage       = request.args.get('stage', '')
    grade_id    = request.args.get('grade_id', type=int)
    section_id  = request.args.get('section_id', type=int)
    building_id = request.args.get('building_id', type=int)
    residential_area_id = request.args.get('residential_area_id', type=int)
    school      = get_current_school()
    year        = get_view_year(school.id) if school else None

    query = Student.query

    # School scoping — always filter by current school
    if school:
        query = query.filter_by(school_id=school.id)

    # Building scoping — second layer, only when the feature is on AND the user
    # is restricted. No effect on schools/users without buildings.
    buildings_on = school_buildings_enabled(school)
    allowed_building_ids = user_allowed_building_ids(current_user, school)
    query = apply_building_scope_to_students(query, current_user, school)

    if _is_teacher():
        teacher_sids = get_teacher_section_ids(current_user)
        if teacher_sids:
            query = query.filter(Student.section_id.in_(teacher_sids))
        else:
            query = query.filter(Student.id == -1)

    if search:
        query = query.filter(
            Student.full_name.ilike(f'%{search}%') |
            Student.student_id.ilike(f'%{search}%')
        )
    if status == 'archived':
        query = query.filter_by(status='archived')
    elif status:
        query = query.filter_by(status=status)
    else:
        # Archived students are hidden from the default list; use status=archived to view them
        query = query.filter(Student.status != 'archived')

    # Optional building filter (only meaningful when buildings are enabled).
    if buildings_on and building_id:
        # A restricted user may only filter within their allowed buildings.
        if allowed_building_ids is None or building_id in allowed_building_ids:
            query = query.filter(Student.building_id == building_id)

    # Optional residential-area filter. The query is already restricted to the
    # current school, so a manipulated foreign area id matches no rows —
    # students of this school can never be linked to another school's area.
    if residential_area_id:
        query = query.filter(Student.residential_area_id == residential_area_id)

    if section_id:
        query = query.filter_by(section_id=section_id)
    elif grade_id:
        section_ids_for_grade = [
            s.id for s in Section.query.filter_by(grade_id=grade_id).all()
        ]
        if section_ids_for_grade:
            query = query.filter(Student.section_id.in_(section_ids_for_grade))
        else:
            query = query.filter(Student.id == -1)
    elif stage:
        grade_q = Grade.query.filter_by(school_id=school.id, stage=stage)
        if year:
            grade_q = grade_q.filter_by(academic_year_id=year.id)
        stage_grade_ids = [g.id for g in grade_q.all()]
        if stage_grade_ids:
            stage_sec_ids = [s.id for s in Section.query.filter(
                Section.grade_id.in_(stage_grade_ids)).all()]
            if stage_sec_ids:
                query = query.filter(Student.section_id.in_(stage_sec_ids))
            else:
                query = query.filter(Student.id == -1)
        else:
            query = query.filter(Student.id == -1)

    students = (query
                .options(joinedload(Student.section).joinedload(Section.grade))
                .execution_options(include_all_years=True)
                .order_by(Student.created_at.desc())
                .paginate(page=page, per_page=20, error_out=False))

    # Grades and sections for filter dropdowns
    sections_q = Section.query
    if school and year:
        sections_q = sections_q.filter_by(school_id=school.id, academic_year_id=year.id)

    # All grades for the school/year — used for both stages list and grade dropdown
    all_grades_q = Grade.query
    if school and year:
        all_grades_q = all_grades_q.filter_by(school_id=school.id, academic_year_id=year.id)
    elif school:
        all_grades_q = all_grades_q.filter_by(school_id=school.id)
    all_grades = all_grades_q.order_by(Grade.name).all()

    # Distinct non-empty stages for the stage filter dropdown
    stages_list = sorted({g.stage for g in all_grades if g.stage})

    # Grade dropdown is filtered by stage when a stage is selected
    grades_list = [g for g in all_grades if not stage or g.stage == stage]

    sections_list = (sections_q.filter_by(grade_id=grade_id).order_by(Section.name).all()
                     if grade_id else [])

    # Capacity info for the header banner
    capacity_info = None
    if school and school.capacity and school.capacity > 0:
        current_count = Student.query.filter_by(
            school_id=school.id, status='active').count()
        capacity_info = {
            'capacity': school.capacity,
            'current':  current_count,
            'at_limit': school.is_at_capacity,
        }

    # Buildings for the filter dropdown / column (restricted users see only theirs).
    buildings_list = []
    if buildings_on:
        buildings_list = get_active_buildings(school.id) if school else []
        if allowed_building_ids is not None:
            buildings_list = [b for b in buildings_list if b.id in allowed_building_ids]

    # Residential areas for the filter dropdown (this school only; includes
    # deactivated areas so students linked to them can still be filtered).
    residential_areas_list = _school_residential_areas(school.id) if school else []

    return render_template('students/index.html',
                           students=students, search=search, status=status,
                           capacity_info=capacity_info,
                           active_year=year,
                           stages_list=stages_list,
                           stage=stage,
                           grades_list=grades_list,
                           sections_list=sections_list,
                           grade_id=grade_id,
                           section_id=section_id,
                           buildings_enabled=buildings_on,
                           buildings_list=buildings_list,
                           building_id=building_id,
                           residential_areas_list=residential_areas_list,
                           residential_area_id=residential_area_id)


@students_bp.route('/search')
@login_required
@permission_required('view_students')
def search():
    """Debounced AJAX live-search endpoint — returns JSON for client-side rendering."""
    school = get_current_school()
    if not school:
        return jsonify({'items': [], 'total': 0, 'page': 1, 'pages': 0,
                        'has_next': False, 'has_prev': False,
                        'next_num': None, 'prev_num': None}), 200

    q           = request.args.get('q', '').strip()
    page        = request.args.get('page', 1, type=int)
    status      = request.args.get('status', '')
    stage       = request.args.get('stage', '')
    grade_id    = request.args.get('grade_id', type=int)
    section_id  = request.args.get('section_id', type=int)
    building_id = request.args.get('building_id', type=int)
    residential_area_id = request.args.get('residential_area_id', type=int)

    buildings_on         = school_buildings_enabled(school)
    allowed_building_ids = user_allowed_building_ids(current_user, school)

    query = Student.query.filter_by(school_id=school.id)
    query = apply_building_scope_to_students(query, current_user, school)

    if _is_teacher():
        teacher_sids = get_teacher_section_ids(current_user)
        if teacher_sids:
            query = query.filter(Student.section_id.in_(teacher_sids))
        else:
            query = query.filter(Student.id == -1)

    if q:
        query = query.filter(
            Student.full_name.ilike(f'%{q}%') |
            Student.student_id.ilike(f'%{q}%')
        )

    if status == 'archived':
        query = query.filter_by(status='archived')
    elif status:
        query = query.filter_by(status=status)
    else:
        query = query.filter(Student.status != 'archived')

    if buildings_on and building_id:
        if allowed_building_ids is None or building_id in allowed_building_ids:
            query = query.filter(Student.building_id == building_id)

    # Residential-area filter — same fail-closed reasoning as students.index:
    # the query is already school-filtered, so a foreign area id matches nothing.
    if residential_area_id:
        query = query.filter(Student.residential_area_id == residential_area_id)

    if section_id:
        query = query.filter_by(section_id=section_id)
    elif grade_id:
        _sids = [s.id for s in Section.query.filter_by(grade_id=grade_id).all()]
        if _sids:
            query = query.filter(Student.section_id.in_(_sids))
        else:
            query = query.filter(Student.id == -1)
    elif stage:
        year = get_view_year(school.id)
        _gq = Grade.query.filter_by(school_id=school.id, stage=stage)
        if year:
            _gq = _gq.filter_by(academic_year_id=year.id)
        _gids = [g.id for g in _gq.all()]
        if _gids:
            _ssids = [s.id for s in Section.query.filter(
                Section.grade_id.in_(_gids)).all()]
            if _ssids:
                query = query.filter(Student.section_id.in_(_ssids))
            else:
                query = query.filter(Student.id == -1)
        else:
            query = query.filter(Student.id == -1)

    paginated = (query
                 .options(joinedload(Student.section).joinedload(Section.grade))
                 .execution_options(include_all_years=True)
                 .order_by(Student.created_at.desc())
                 .paginate(page=page, per_page=20, error_out=False))

    from app.utils.helpers import resolve_photo_url as _rpu
    items = []
    for s in paginated.items:
        section_label = ''
        if s.section:
            section_label = f'{s.section.grade.name} / {s.section.name}'
        items.append({
            'id':             s.id,
            'student_id':     s.student_id,
            'full_name':      s.full_name,
            'gender':         s.gender,
            'section_label':  section_label,
            'building_name':  (s.building.name if buildings_on and s.building else ''),
            'guardian_name':  s.guardian_name or '',
            'guardian_phone': s.guardian_phone or '',
            'status':         s.status,
            'enrollment_date': (s.enrollment_date.strftime('%Y-%m-%d')
                                if s.enrollment_date else ''),
            'photo_url':      _rpu(s.photo) or '',
            'view_url':       url_for('students.view',    student_id=s.id),
            'edit_url':       url_for('students.edit',    student_id=s.id),
            'archive_url':    url_for('students.archive', student_id=s.id),
        })

    return jsonify({
        'items':        items,
        'total':        paginated.total,
        'page':         paginated.page,
        'pages':        paginated.pages,
        'has_next':     paginated.has_next,
        'has_prev':     paginated.has_prev,
        'next_num':     paginated.next_num,
        'prev_num':     paginated.prev_num,
        'buildings_on': buildings_on,
    })


@students_bp.route('/create', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('add_student')
def create():
    school = get_current_school()
    year   = get_active_year(school.id) if school else None

    # ── SCHOOL / YEAR GUARDS ────────────────────────────────────────────────
    if not school:
        flash('يجب تحديد مدرسة أولاً. تواصل مع مسؤول النظام.', 'warning')
        return redirect(url_for('admin.dashboard'))
    if not year:
        flash(
            'لا يوجد عام دراسي نشط لهذه المدرسة. '
            'يرجى مراجعة مسؤول النظام لتفعيل عام دراسي قبل إضافة الطلاب.',
            'danger',
        )
        return redirect(url_for('students.index'))

    # ── CAPACITY CHECK ──────────────────────────────────────────────────────
    if school and school.capacity and school.capacity > 0:
        current_count = Student.query.filter_by(
            school_id=school.id, status='active').count()
        if current_count >= school.capacity:
            flash(
                f'لقد تم الوصول إلى الحد الأقصى لعدد الطلاب في هذه المدرسة '
                f'({school.capacity} طالب). لا يمكن إضافة طلاب جدد حتى يتم '
                f'تعديل السعة أو تخفيض العدد الحالي.',
                'danger'
            )
            return redirect(url_for('students.index'))

    # Build sections list scoped to current school
    all_sections = Section.query
    if school and year:
        school_year_grade_ids = [g.id for g in
                                  Grade.query.execution_options(include_all_years=True)
                                  .filter_by(academic_year_id=year.id).all()]
        if school_year_grade_ids:
            all_sections = all_sections.filter(
                Section.grade_id.in_(school_year_grade_ids))

    all_sections = all_sections.all()

    if _is_teacher():
        section_ids = get_teacher_section_ids(current_user)
        sections = [s for s in all_sections if s.id in section_ids]
    else:
        sections = all_sections

    grades_q = Grade.query.execution_options(include_all_years=True)
    if school and year:
        grades_q = grades_q.filter_by(school_id=school.id, academic_year_id=year.id)
    grades = grades_q.order_by(Grade.name).all()

    active_devices = []
    if school and is_feature_enabled(school.id, 'attendance_devices.mappings'):
        active_devices = (AttendanceDevice.query
                         .filter_by(school_id=school.id, is_active=True)
                         .order_by(AttendanceDevice.name).all())

    parent_role = Role.query.filter_by(name='parent').first()
    available_parents = []
    if school and parent_role:
        available_parents = (User.query
                             .filter_by(school_id=school.id, role_id=parent_role.id,
                                        is_active=True)
                             .order_by(User.full_name)
                             .all())

    form_cfg = get_student_form_config(school.id)

    # ── Buildings (optional feature) ─────────────────────────────────────────
    buildings_on = school_buildings_enabled(school)
    allowed_building_ids = user_allowed_building_ids(current_user, school)
    buildings_for_form = []
    if buildings_on:
        buildings_for_form = get_active_buildings(school.id)
        if allowed_building_ids is not None:
            buildings_for_form = [b for b in buildings_for_form
                                  if b.id in allowed_building_ids]

    # ── Optional in-wizard fee step ──────────────────────────────────────────
    # The fee step is only available to users who may manage fees; this keeps
    # the manage_fees authorization boundary intact even though fee creation now
    # happens inside the add_student flow. fee_types is auto-scoped to the
    # current school + active year by the tenant ORM filter.
    can_manage_fees = current_user.has_permission('manage_fees')
    fee_types = FeeType.query.all() if can_manage_fees else []

    # ── Residential areas (active areas of the current school only) ─────────
    residential_areas_for_form = _school_residential_areas(school.id,
                                                           active_only=True)

    # ── Auto-generated parent credentials (shown read-only in the wizard) ─────
    # A fresh pair is produced on each page load, so cancelling and re-opening a
    # new student always yields new credentials. On a re-render after a
    # validation error the submitted values take precedence in the template, so
    # the same credentials persist through the wizard until save.
    gen_parent_username = code_generator.generate_parent_username()
    gen_parent_password = code_generator.generate_parent_password()

    if request.method == 'POST':
        # ── Pre-validate parent account and device mapping fields ────────────
        # Parent credentials are auto-generated (see below); the manager only
        # chooses whether to create the account via this toggle. Selecting an
        # existing parent to link takes precedence, so the two paths are mutually
        # exclusive and a duplicate account is never created.
        create_parent_account   = request.form.get('create_parent_account') == '1'
        link_existing_parent_id = request.form.get('link_existing_parent_id', type=int)
        employee_no_string      = request.form.get('employee_no_string', '').strip()
        _dev_id_raw             = request.form.get('device_id', '').strip()
        device_id               = int(_dev_id_raw) if _dev_id_raw.isdigit() else None
        all_devices_flag        = request.form.get('all_devices') == '1'

        def _re_render(msg, error_step=1):
            if msg:
                flash(msg, 'danger')
            return render_template('students/create_wizard.html', student=None, sections=sections,
                                   grades=grades, stages=['ابتدائية', 'متوسطة', 'إعدادية'],
                                   selected_grade_id=None, selected_stage=None,
                                   active_devices=active_devices,
                                   available_parents=available_parents,
                                   linked_parents=[], existing_device_mappings=[],
                                   form_cfg=form_cfg,
                                   buildings_enabled=buildings_on,
                                   buildings_list=buildings_for_form,
                                   selected_building_id=request.form.get('building_id', type=int),
                                   residential_areas_list=residential_areas_for_form,
                                   active_year=year,
                                   can_manage_fees=can_manage_fees,
                                   fee_types=fee_types,
                                   gen_parent_username=gen_parent_username,
                                   gen_parent_password=gen_parent_password,
                                   error_step=error_step)

        # ── Backend enforcement of required fields per school config ─────────
        _cfg_errors = form_cfg.validate(request.form)
        if _cfg_errors:
            for _err in _cfg_errors:
                flash(_err, 'danger')
            return _re_render('')

        # ── Residential area (optional) — validated BEFORE any upload/DB write ─
        # Empty is allowed (no area). A non-empty value must resolve to an active
        # area of THIS school; an invalid, inactive, or foreign-school id is
        # rejected here (submitted values preserved by _re_render) instead of
        # being silently discarded, so the user can correct the selection.
        _raw_area_id = request.form.get('residential_area_id', type=int)
        residential_area_id_val = None
        if _raw_area_id:
            residential_area_id_val = _validate_residential_area_for_school(
                _raw_area_id, school.id)
            if not residential_area_id_val:
                return _re_render('يرجى اختيار منطقة سكن صالحة لهذه المدرسة.')

        # Parent credentials are generated automatically — nothing the manager
        # typed is validated here. The unique username is produced (and its
        # global uniqueness guaranteed) in the account-creation block below, so
        # a duplicate-username error can never be surfaced.

        if employee_no_string:
            if not employee_no_string.isdigit():
                return _re_render('رقم الطالب في جهاز الحضور يجب أن يكون أرقاماً فقط')
            _check_devs = active_devices if all_devices_flag else (
                [d for d in active_devices if d.id == device_id] if device_id else [])
            for _dev in _check_devs:
                _conflict = (DeviceStudentMapping.query
                             .filter_by(device_id=_dev.id, employee_no_string=employee_no_string)
                             .first())
                if _conflict:
                    return _re_render(f'الرقم {employee_no_string} مستخدم مسبقاً في جهاز "{_dev.name}"')

        # ── Pre-validate optional fee BEFORE creating the student / uploads ───
        # Validating here (no DB writes yet) keeps registration atomic and avoids
        # orphaned uploads if the fee data is invalid. Fee creation is gated by
        # manage_fees and only runs when the admin explicitly opted in.
        create_fee        = (can_manage_fees and request.form.get('create_fee') == '1')
        _fee_total        = None
        _fee_discount     = None
        _fee_type_id_val  = None
        if create_fee:
            from app.blueprints.fees import FeeValidationError, compute_fee_amounts
            _fee_type_id_val = request.form.get('fee_type_id', type=int)
            # FeeType.query is auto-scoped to this school + active year by the
            # tenant ORM filter, so a fee type from another school/year is None.
            _fee_type = (FeeType.query.filter_by(id=_fee_type_id_val).first()
                         if _fee_type_id_val else None)
            if not _fee_type:
                return _re_render('يرجى اختيار نوع رسم صالح لهذه المدرسة.', error_step=5)
            try:
                _fee_total, _fee_discount, _ = compute_fee_amounts(request.form)
            except FeeValidationError as _fexc:
                return _re_render(str(_fexc), error_step=5)
            # Pre-validate any payment amounts submitted for wizard installments.
            _pre_num_inst = max(1, min(12, int(request.form.get('num_installments', '1') or '1')))
            for _pi in range(1, _pre_num_inst + 1):
                _pre_pay = request.form.get(f'pay_amount_{_pi}', '').strip()
                if _pre_pay:
                    try:
                        _pre_pv = Decimal(_pre_pay)
                        if _pre_pv <= 0:
                            return _re_render(
                                f'المبلغ المدفوع للقسط {_pi} يجب أن يكون أكبر من صفر.',
                                error_step=5)
                    except InvalidOperation:
                        return _re_render(
                            f'المبلغ المدفوع للقسط {_pi} غير صالح.', error_step=5)

        from datetime import datetime as dt
        student_id = code_generator.generate_student_id(school.id)

        section_id = request.form.get('section_id', type=int)
        if _is_teacher() and section_id not in get_teacher_section_ids(current_user):
            abort(403)

        _school_id_for_feat = school.id if school else None
        photo_path = None
        if 'photo' in request.files and is_feature_enabled(_school_id_for_feat, 'students.photo_upload'):
            photo_path = save_uploaded_file(request.files['photo'], 'students')

        dob_str = request.form.get('date_of_birth', '').strip()
        dob     = dt.strptime(dob_str, '%Y-%m-%d').date() if dob_str else None

        gender_val = request.form.get('gender', '').strip() or None

        # Backend validation for visible fields that are configured as required
        if form_cfg.field_visible('date_of_birth') and form_cfg.field_required('date_of_birth') and not dob:
            return _re_render('تاريخ الميلاد مطلوب')
        if form_cfg.field_visible('gender') and form_cfg.field_required('gender') and not gender_val:
            return _re_render('الجنس مطلوب')

        # ── Building assignment (only when feature enabled) ──────────────────
        building_id_val = None
        if buildings_on:
            _raw_building = request.form.get('building_id', type=int)
            building_id_val = validate_building_for_school(_raw_building, school.id)
            # A restricted user can only assign students to their own buildings.
            if (building_id_val and allowed_building_ids is not None
                    and building_id_val not in allowed_building_ids):
                return _re_render('ليس لديك صلاحية الوصول إلى بيانات هذه البناية')

        # Residential area was validated (and rejected on error) in the
        # pre-validation block above; residential_area_id_val is safe to use.

        student = Student(
            student_id        = student_id,
            full_name         = request.form.get('full_name', '').strip(),
            date_of_birth     = dob,
            gender            = gender_val,
            nationality       = request.form.get('nationality', '').strip(),
            address           = request.form.get('address', '').strip(),
            phone             = request.form.get('phone', '').strip(),
            rfid_tag_id       = request.form.get('rfid_tag_id', '').strip() or None,
            section_id        = section_id,
            guardian_name     = request.form.get('guardian_name', '').strip(),
            guardian_phone    = request.form.get('guardian_phone', '').strip(),
            guardian_email    = request.form.get('guardian_email', '').strip(),
            guardian_relation = request.form.get('guardian_relation', '').strip(),
            photo             = photo_path,
            notes             = request.form.get('notes', '').strip(),
            building_id       = building_id_val,
            residential_area_id = residential_area_id_val,
            # Multi-tenant fields
            school_id         = school.id if school else None,
            academic_year_id  = year.id   if year   else None,
        )
        import logging as _logging
        _log = _logging.getLogger(__name__)
        from sqlalchemy.exc import IntegrityError as _IntegrityError
        try:
            db.session.add(student)
            db.session.flush()
        except _IntegrityError as _exc:
            db.session.rollback()
            _exc_str = str(_exc).lower()
            _log.error('Student flush IntegrityError: %s', str(_exc)[:800])
            # Match only the student_id uniqueness constraint, not RFID or FK violations.
            _is_student_id_conflict = (
                'uq_student_school_student_id' in _exc_str
                or 'ix_students_student_id' in _exc_str
                or ('student_id' in _exc_str and 'unique' in _exc_str)
            )
            if _is_student_id_conflict:
                return _re_render('رقم الطالب مستخدم مسبقاً، يرجى المحاولة مرة أخرى')
            if 'uq_student_school_rfid_tag' in _exc_str:
                return _re_render('رقم RFID مستخدم مسبقاً لطالب آخر في هذه المدرسة')
            return _re_render('تعذر حفظ بيانات الطالب بسبب تعارض في القيم. يرجى المحاولة مرة أخرى')

        if is_feature_enabled(_school_id_for_feat, 'students.documents_upload') and form_cfg.section_visible('student_documents'):
            doc_types = request.form.getlist('document_type[]')
            doc_files = request.files.getlist('document_file[]')
            for doc_type, doc_file in zip(doc_types, doc_files):
                if doc_file and doc_file.filename:
                    saved = save_uploaded_file(
                        doc_file,
                        'students/documents',
                        prefix=f"{student.student_id}_{doc_type or 'document'}"
                    )
                    if saved:
                        db.session.add(StudentDocument(
                            student_id=student.id,
                            document_type=doc_type.strip() or 'وثيقة',
                            file_path=saved,
                        ))

        # ── Create parent user account (auto-generated credentials) ──────────
        # Skipped when the manager linked an existing parent instead — the two
        # are mutually exclusive so no duplicate account is ever created.
        parent_created  = False
        parent_username = ''
        parent_password = ''
        if create_parent_account and not link_existing_parent_id:
            parent_role = Role.query.filter_by(name='parent').first()
            if parent_role:
                # Trust the read-only credentials generated in the wizard, but
                # regenerate server-side if they are missing, malformed, or (very
                # rarely) already taken globally by a concurrent request. This
                # guarantees a unique, policy-compliant username right before the
                # insert without ever surfacing a duplicate-username error.
                parent_username = request.form.get('parent_username', '').strip()
                parent_password = request.form.get('parent_password', '').strip()
                if not (code_generator.is_valid_parent_username(parent_username)
                        and code_generator.parent_username_available(parent_username)):
                    parent_username = code_generator.generate_parent_username()
                if not code_generator.is_valid_parent_password(parent_password):
                    parent_password = code_generator.generate_parent_password()

                _parent_email = None
                if student.guardian_email:
                    if not User.query.filter_by(email=student.guardian_email).first():
                        _parent_email = student.guardian_email
                parent_user = User(
                    username=parent_username,
                    full_name=student.guardian_name or parent_username,
                    email=_parent_email,
                    phone=student.guardian_phone or None,
                    school_id=school.id,
                    role_id=parent_role.id,
                    is_active=True,
                )
                parent_user.set_password(parent_password)
                db.session.add(parent_user)
                db.session.flush()
                db.session.execute(
                    parent_students.insert().values(
                        user_id=parent_user.id,
                        student_id=student.id,
                        relation=student.guardian_relation or 'guardian',
                    )
                )
                parent_created = True

        # ── Create attendance device mappings ────────────────────────────────
        if employee_no_string and employee_no_string.isdigit():
            _map_devs = active_devices if all_devices_flag else (
                [d for d in active_devices if d.id == device_id] if device_id else [])
            for _dev in _map_devs:
                db.session.add(DeviceStudentMapping(
                    school_id=school.id,
                    device_id=_dev.id,
                    employee_no_string=employee_no_string,
                    student_id=student.id,
                    is_active=True,
                ))

        # ── Link to existing parent account ──────────────────────────────────
        # School-scoped lookup — an id from another school resolves to None.
        _linked_parent_name = None
        if link_existing_parent_id and not parent_created:
            _ep = User.query.filter_by(id=link_existing_parent_id, school_id=school.id,
                                       role_id=parent_role.id if parent_role else 0).first()
            if _ep:
                db.session.execute(
                    parent_students.insert().values(
                        user_id=_ep.id,
                        student_id=student.id,
                        relation=student.guardian_relation or 'guardian',
                    )
                )
                _linked_parent_name = _ep.full_name

        # ── Create optional fee record (shared fee logic, manage_fees gated) ──
        _fee_created = False
        _fee_payment_data = []   # plain dicts for post-commit Revenue records
        _paid_inst_count  = 0
        if create_fee:
            from app.blueprints.fees import (persist_fee_record, FeeValidationError,
                                             distribute_fee_payment, _notify_fee_parents)
            try:
                _fee_record = persist_fee_record(
                    request.form,
                    school=school,
                    student_id=student.id,
                    fee_type_id=_fee_type_id_val,
                    academic_year_id=year.id,   # fee shares the student's active year
                    total_amount=_fee_total,
                    discount=_fee_discount,
                    notes=request.form.get('fee_notes', ''),
                )
                _fee_created = True

                # Apply any payments entered per-installment in the wizard.
                # Flush first so the installments have IDs and are queryable.
                db.session.flush()
                _num_inst = max(1, min(12, int(request.form.get('num_installments', 1) or 1)))
                # Fetched ONCE and reused for every entry below: distribute_fee_payment
                # mutates these same in-memory installment objects in place, so a
                # cascade triggered by an earlier installment's payment is already
                # reflected (reduced remaining balance) when a later installment's
                # own entered amount is processed — no installment is ever paid twice.
                _installments = (FeeInstallment.query
                                 .filter_by(fee_record_id=_fee_record.id, school_id=school.id)
                                 .order_by(FeeInstallment.installment_no)
                                 .all())
                for _wi in range(1, _num_inst + 1):
                    _pay_raw = request.form.get(f'pay_amount_{_wi}', '').strip()
                    if not _pay_raw:
                        continue
                    try:
                        _received = Decimal(_pay_raw)
                    except InvalidOperation:
                        continue
                    if _received <= 0:
                        continue
                    _pay_date_str = request.form.get(f'pay_date_{_wi}', '').strip()
                    if _pay_date_str:
                        try:
                            _pay_date_val = dt.strptime(_pay_date_str, '%Y-%m-%d').date()
                        except ValueError:
                            _pay_date_val = dt.today().date()
                    else:
                        _pay_date_val = dt.today().date()
                    # Field order is ascending installment number, and an earlier
                    # field's cascade (still the SAME shared _installments objects)
                    # may already have fully settled installment #_wi by the time we
                    # get here. That amount must not be rejected or discarded — it
                    # is forwarded to the next installment (in the existing
                    # ascending order, starting no earlier than #_wi) that still has
                    # an outstanding balance. If every installment from #_wi onward
                    # is already settled, fall back to #_wi itself so
                    # distribute_fee_payment() raises its normal "already fully
                    # paid" rejection — correct here too, since nothing is left to
                    # apply this amount to.
                    _effective_start_no = next(
                        (i.installment_no for i in _installments
                         if i.installment_no >= _wi
                         and (Decimal(str(i.amount)) - Decimal(str(i.received_amount or 0))) > 0),
                        _wi,
                    )
                    # Delegate to the exact same distribution function used by the
                    # standalone Fees and Installments "record payment" flow
                    # (fees.pay_installment): settle the target installment first,
                    # then cascade any excess into the following unpaid
                    # installments of this same fee record, in order — never a
                    # second, independently-maintained payment implementation.
                    # Raises FeeValidationError (caught below, atomic rollback) if
                    # the entered amount exceeds what's currently outstanding from
                    # that point onward.
                    _allocations = distribute_fee_payment(
                        _installments, _effective_start_no, _received,
                        payment_method=request.form.get(f'pay_method_{_wi}', 'cash'),
                        paid_date=_pay_date_val,
                        notes=request.form.get(f'pay_notes_{_wi}', '').strip(),
                        collected_by=current_user.id,
                    )
                    for _alloc in _allocations:
                        _ai, _aa = _alloc['installment'], _alloc['applied']
                        _fee_payment_data.append({
                            'school_id':        school.id,
                            'academic_year_id': year.id,
                            'amount':           float(_aa),
                            'installment_no':   _ai.installment_no,
                            'paid_date':        _ai.paid_date,
                            'inst_id':          _ai.id,
                            'fee_record_id':    _fee_record.id,
                            'receipt_no':       _ai.receipt_no,
                            'status':           _ai.status,
                            'payment_method':   _ai.payment_method,
                        })
                _paid_inst_count = len(_fee_payment_data)

                # Revenue records staged in the same transaction — every payment must
                # have its matching accounting entry before the single final commit.
                if _fee_payment_data:
                    _fee_cat = (
                        RevenueCategory.query
                        .execution_options(bypass_tenant_scope=True)
                        .filter_by(name='رسوم دراسية', school_id=school.id)
                        .first()
                    )
                    if not _fee_cat:
                        _fee_cat = RevenueCategory(name='رسوم دراسية', school_id=school.id)
                        db.session.add(_fee_cat)
                        db.session.flush()
                    for _pd in _fee_payment_data:
                        db.session.add(Revenue(
                            category_id      = _fee_cat.id,
                            school_id        = _pd['school_id'],
                            academic_year_id = _pd['academic_year_id'],
                            amount           = _pd['amount'],
                            description      = (f'دفعة رسوم للطالب {student.full_name}'
                                                f' - قسط #{_pd["installment_no"]}'),
                            date             = _pd['paid_date'],
                            recorded_by      = current_user.id,
                        ))

            except FeeValidationError as _fee_exc:
                # Keep registration atomic: discard the student and everything
                # staged in this transaction, return to the fee step. Unlike the
                # generic ValueError branch below, FeeValidationError always
                # carries a safe, specific Arabic message (e.g. the maximum
                # amount currently payable on an over-limit installment
                # payment) — surface it as-is instead of a generic fallback.
                db.session.rollback()
                return _re_render(str(_fee_exc), error_step=5)
            except ValueError:
                # Keep registration atomic: discard the student and everything
                # staged in this transaction, return to the fee step.
                db.session.rollback()
                return _re_render(
                    'تعذر حفظ بيانات الرسوم. يرجى مراجعة المبالغ وعدد الأقساط وتواريخ الاستحقاق.',
                    error_step=5)
            except Exception:
                # Unexpected DB or system error (e.g. constraint on Revenue) — roll back
                # the entire operation and let Flask's error handler surface the 500.
                db.session.rollback()
                raise

        # ── Single final commit ──────────────────────────────────────────────
        # Student, fee record, installments, payments, receipt numbers, and
        # Revenue records are all staged in the same session and committed here.
        db.session.commit()

        # ── Post-commit: same audit log + FCM pushes as fees.pay_installment ──
        # Every payment recorded through this wizard step must leave the same
        # audit trail and trigger the same parent/investor notifications as one
        # recorded from the standalone Fees and Installments page. None of this
        # may ever roll back the already-committed student/fee/payment records,
        # so it mirrors pay_installment's own defensive error handling.
        if _fee_payment_data:
            status_labels = {'paid': 'مكتمل', 'partial': 'دفعة جزئية',
                             'pending': 'قيد الانتظار', 'overdue': 'متأخر'}
            for _pd in _fee_payment_data:
                log_action('payment', 'fee_installment', _pd['inst_id'],
                          details=f"received={_pd['amount']} method={_pd['payment_method']} "
                                  f"status={_pd['status']}")
                _notify_fee_parents(
                    student.id,
                    'تم تسجيل دفعة',
                    f"تم تسجيل دفعة بقيمة {_pd['amount']} لقسط الرسوم رقم {_pd['installment_no']} "
                    f"({status_labels.get(_pd['status'], '')}).",
                    screen='fees',
                    fee_record_id=_pd['fee_record_id'],
                    installment_id=_pd['inst_id'],
                )
                try:
                    from app.services.fcm_service import notify_investors
                    notify_investors(
                        school_id = school.id,
                        title     = 'إيراد جديد',
                        body      = f"تم تسجيل إيراد جديد بقيمة {_pd['amount']}",
                        data      = {
                            'type':       'investor_revenue',
                            'route':      '/investor/revenues',
                            'school_id':  str(school.id),
                            'amount':     str(_pd['amount']),
                        },
                    )
                except Exception:
                    _log.exception('[students] investor push failed for inst_id=%s', _pd['inst_id'])

        flash(f'تم إضافة الطالب {student.full_name} برقم {student.student_id}.', 'success')
        if parent_created:
            flash(
                'تم إنشاء حساب ولي الأمر بنجاح. '
                f'اسم المستخدم: {parent_username} — كلمة المرور: {parent_password}. '
                'يرجى حفظ هذه البيانات وتسليمها لولي الأمر.',
                'success')
            # One-time payload for the success page. The plaintext password is
            # held only in the server-side session and is shown once on the
            # immediate success view, then removed — it is never stored in the
            # database and cannot be retrieved after that view.
            session['new_parent_credentials'] = {
                'student_id': student.id,
                'username':   parent_username,
                'password':   parent_password,
            }
        if _linked_parent_name:
            flash(f'تم ربط الطالب بولي الأمر {_linked_parent_name} بنجاح.', 'success')
        if _fee_created:
            flash('تم إنشاء سجل الرسوم للطالب بنجاح.', 'success')
        if _paid_inst_count:
            flash(f'تم تسجيل {_paid_inst_count} دفعة للرسوم بنجاح.', 'success')
        return redirect(url_for('students.create_success', student_id=student.id))

    return render_template('students/create_wizard.html', student=None, sections=sections,
                           grades=grades, stages=['ابتدائية', 'متوسطة', 'إعدادية'],
                           selected_grade_id=None, selected_stage=None,
                           active_devices=active_devices,
                           available_parents=available_parents,
                           linked_parents=[], existing_device_mappings=[],
                           form_cfg=form_cfg,
                           buildings_enabled=buildings_on,
                           buildings_list=buildings_for_form,
                           selected_building_id=None,
                           residential_areas_list=residential_areas_for_form,
                           active_year=year,
                           can_manage_fees=can_manage_fees,
                           fee_types=fee_types,
                           gen_parent_username=gen_parent_username,
                           gen_parent_password=gen_parent_password,
                           error_step=1)


@students_bp.route('/<int:student_id>/create-success')
@login_required
@permission_required('add_student')
def create_success(student_id):
    """Success landing page shown immediately after a new student is created.

    Renders the same review sections as the wizard's Review step (from the saved
    record) so they can be printed. The generated parent password is shown only
    on the immediate post-creation view: it lives in the server-side session for
    a single view and is popped here — never stored in the database.
    """
    from datetime import date
    school = get_current_school()
    student = (Student.query
               .options(joinedload(Student.section).joinedload(Section.grade))
               .execution_options(include_all_years=True)
               .filter_by(id=student_id)
               .first_or_404())
    if school and student.school_id != school.id:
        abort(403)
    # Building scope — a restricted user cannot view students outside their buildings.
    if not user_can_access_student(current_user, school, student):
        flash('ليس لديك صلاحية الوصول إلى بيانات هذه البناية', 'danger')
        return redirect(url_for('students.index'))

    # ── Parent account credentials (one-time password) ───────────────────────
    parent_creds = None
    payload = session.get('new_parent_credentials')
    if payload and payload.get('student_id') == student.id:
        parent_creds = {
            'username':           payload.get('username'),
            'password':           payload.get('password'),
            'password_available': True,
        }
        # Show once, then discard so the plaintext password is not retrievable
        # again on refresh or re-open.
        session.pop('new_parent_credentials', None)
    else:
        # Refreshed/reopened later — surface the linked parent username from the
        # database but never the password.
        _linked = student.parents.order_by(User.id.desc()).first()
        if _linked:
            parent_creds = {
                'username':           _linked.username,
                'password':           None,
                'password_available': False,
            }

    docs = student.documents.order_by(StudentDocument.uploaded_at.desc()).all()
    device_mappings = student.device_mappings.all()

    # ── Fees (same scoping as students.view) — only for fee-authorised users ──
    fee_records_with_inst = []
    if current_user.has_permission('manage_fees'):
        _fee_records = (
            FeeRecord.query
            .execution_options(include_all_years=True)
            .filter_by(student_id=student.id, school_id=student.school_id)
            .options(joinedload(FeeRecord.fee_type),
                     joinedload(FeeRecord.academic_year))
            .order_by(FeeRecord.created_at.desc())
            .all()
        )
        if _fee_records:
            _fee_ids = [fr.id for fr in _fee_records]
            _all_inst = (
                FeeInstallment.query
                .execution_options(include_all_years=True)
                .filter(FeeInstallment.fee_record_id.in_(_fee_ids),
                        FeeInstallment.school_id == student.school_id)
                .order_by(FeeInstallment.installment_no)
                .all()
            )
            _inst_map = {}
            for _inst in _all_inst:
                _inst_map.setdefault(_inst.fee_record_id, []).append(_inst)
            fee_records_with_inst = [(fr, _inst_map.get(fr.id, [])) for fr in _fee_records]

    logo_url = None
    if school and getattr(school, 'logo_path', None):
        logo_url = resolve_photo_url(school.logo_path)

    return render_template('students/create_success.html',
                           student=student,
                           school=school,
                           logo_url=logo_url,
                           print_date=date.today(),
                           parent_creds=parent_creds,
                           docs=docs,
                           device_mappings=device_mappings,
                           fee_records_with_inst=fee_records_with_inst)


@students_bp.route('/<int:student_id>/edit', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('edit_student')
def edit(student_id):
    student = Student.query.execution_options(include_all_years=True).get_or_404(student_id)
    school  = get_current_school()
    year    = get_active_year(school.id) if school else None

    # Prevent editing a student from another school
    if school and student.school_id and student.school_id != school.id:
        abort(403)

    # Building scope — a restricted user cannot edit students outside their buildings.
    if not user_can_access_student(current_user, school, student):
        flash('ليس لديك صلاحية الوصول إلى بيانات هذه البناية', 'danger')
        return redirect(url_for('students.index'))

    buildings_on = school_buildings_enabled(school)
    allowed_building_ids = user_allowed_building_ids(current_user, school)
    buildings_for_form = []
    if buildings_on:
        buildings_for_form = get_active_buildings(school.id) if school else []
        if allowed_building_ids is not None:
            buildings_for_form = [b for b in buildings_for_form
                                  if b.id in allowed_building_ids]
        # Keep the student's current building selectable even if inactive, so the
        # dropdown shows the real current value rather than losing it on save.
        if (student.building_id
                and student.building_id not in [b.id for b in buildings_for_form]):
            cur = next((b for b in get_active_buildings(school.id)
                        if b.id == student.building_id), None)
            if cur is None:
                from app.models import SchoolBuilding
                cur = (SchoolBuilding.query
                       .execution_options(bypass_tenant_scope=True)
                       .filter_by(id=student.building_id, school_id=school.id)
                       .first())
            if cur and (allowed_building_ids is None
                        or cur.id in allowed_building_ids):
                buildings_for_form = buildings_for_form + [cur]

    # ── Residential areas (active areas of this school; keep the student's
    # current area selectable even if deactivated, so the dropdown shows the
    # real current value rather than silently losing it on save) ────────────
    residential_areas_for_form = (_school_residential_areas(school.id, active_only=True)
                                  if school else [])
    if (student.residential_area_id
            and student.residential_area_id not in
            [a.id for a in residential_areas_for_form] and school):
        _cur_area = (ResidentialArea.query
                     .execution_options(bypass_tenant_scope=True)
                     .filter_by(id=student.residential_area_id, school_id=school.id)
                     .first())
        if _cur_area:
            residential_areas_for_form = residential_areas_for_form + [_cur_area]

    # Always show sections/grades from the current active year so students
    # can be reassigned across year boundaries (e.g. during year rollover).
    edit_year_id = year.id if year else student.academic_year_id

    all_sections = (Section.query.execution_options(include_all_years=True)
                    .filter_by(academic_year_id=edit_year_id)
                    .all())

    if _is_teacher():
        section_ids = get_teacher_section_ids(current_user)
        if student.section_id not in section_ids:
            flash('لا يمكنك تعديل بيانات طالب خارج شعبتك.', 'danger')
            return redirect(url_for('students.index'))
        sections = [s for s in all_sections if s.id in section_ids]
    else:
        sections = all_sections

    grades = (Grade.query
              .execution_options(include_all_years=True)
              .filter_by(academic_year_id=edit_year_id)
              .order_by(Grade.name).all())

    # selected_* may be None when the student's current section is from a
    # previous year — the form requires a fresh selection in that case.
    selected_section  = next((s for s in sections if s.id == student.section_id), None)
    selected_grade_id = selected_section.grade_id if selected_section else None
    selected_grade    = next((g for g in grades if g.id == selected_grade_id), None)
    selected_stage    = selected_grade.stage if selected_grade else None

    active_devices = []
    if school and is_feature_enabled(school.id, 'attendance_devices.mappings'):
        active_devices = (AttendanceDevice.query
                         .filter_by(school_id=school.id, is_active=True)
                         .order_by(AttendanceDevice.name).all())
    linked_parents = student.parents.all()
    existing_device_mappings = student.device_mappings.all()

    form_cfg = get_student_form_config(school.id) if school else get_student_form_config(0)

    parent_role = Role.query.filter_by(name='parent').first()
    available_parents = []
    if school and parent_role:
        linked_ids = {p.id for p in linked_parents}
        available_parents = [
            u for u in (User.query
                        .filter_by(school_id=school.id, role_id=parent_role.id,
                                   is_active=True)
                        .order_by(User.full_name)
                        .all())
            if u.id not in linked_ids
        ]

    if request.method == 'POST':
        from datetime import datetime as dt

        # ── Backend enforcement of required fields per school config ─────────
        _cfg_errors = form_cfg.validate(request.form)
        if _cfg_errors:
            for _err in _cfg_errors:
                flash(_err, 'danger')
            return render_template(
                'students/form.html', student=student, sections=sections,
                grades=grades, stages=['ابتدائية', 'متوسطة', 'إعدادية'],
                selected_grade_id=selected_grade_id, selected_stage=selected_stage,
                active_devices=active_devices, available_parents=available_parents,
                linked_parents=linked_parents,
                existing_device_mappings=existing_device_mappings,
                form_cfg=form_cfg,
                buildings_enabled=buildings_on,
                buildings_list=buildings_for_form,
                selected_building_id=student.building_id,
                residential_areas_list=residential_areas_for_form,
            )

        new_section_id = request.form.get('section_id', type=int)
        if _is_teacher() and new_section_id not in get_teacher_section_ids(current_user):
            abort(403)

        # full_name — always updated
        student.full_name = request.form.get('full_name', student.full_name).strip()

        # Only update visible fields; preserve existing data for hidden ones
        if form_cfg.field_visible('gender'):
            student.gender = request.form.get('gender', student.gender)
        if form_cfg.field_visible('nationality'):
            student.nationality = request.form.get('nationality', '').strip()
        if form_cfg.field_visible('address'):
            student.address = request.form.get('address', '').strip()
        if form_cfg.field_visible('phone'):
            student.phone = request.form.get('phone', '').strip()
        if form_cfg.field_visible('date_of_birth'):
            dob_str = request.form.get('date_of_birth')
            if dob_str:
                student.date_of_birth = dt.strptime(dob_str, '%Y-%m-%d').date()

        if form_cfg.section_visible('class_section'):
            student.section_id = new_section_id
            # Keep academic_year_id in sync with the assigned section's year so
            # the student's record reflects the year they are actively enrolled in.
            if new_section_id and year:
                student.academic_year_id = year.id
        else:
            # section hidden — preserve existing assignment
            pass

        if form_cfg.section_visible('guardian_info'):
            if form_cfg.field_visible('guardian_name'):
                student.guardian_name = request.form.get('guardian_name', '').strip()
            if form_cfg.field_visible('guardian_phone'):
                student.guardian_phone = request.form.get('guardian_phone', '').strip()
            if form_cfg.field_visible('guardian_email'):
                student.guardian_email = request.form.get('guardian_email', '').strip()
            if form_cfg.field_visible('guardian_relation'):
                student.guardian_relation = request.form.get('guardian_relation', '').strip()

        student.status = request.form.get('status', student.status)

        # ── Building assignment (only when feature enabled) ──────────────────
        if buildings_on:
            _raw_building = request.form.get('building_id', type=int)
            _new_building = validate_building_for_school(_raw_building, school.id)
            if (_new_building and allowed_building_ids is not None
                    and _new_building not in allowed_building_ids):
                flash('ليس لديك صلاحية الوصول إلى بيانات هذه البناية', 'danger')
                return redirect(url_for('students.edit', student_id=student.id))
            student.building_id = _new_building

        # ── Residential area (optional) — server-side school validation ──────
        if school:
            _raw_area = request.form.get('residential_area_id', type=int)
            if not _raw_area:
                # Intentional empty selection — clear any existing link.
                student.residential_area_id = None
            elif _raw_area == student.residential_area_id:
                # Unchanged — keep it even if the area was deactivated meanwhile
                # (it was validated against this school when originally linked).
                pass
            else:
                _area_id = _validate_residential_area_for_school(_raw_area, school.id)
                if not _area_id:
                    # Invalid / inactive / foreign-school id — reject the change
                    # without saving anything and without clearing the student's
                    # current area. Submitted values are preserved (the dirty
                    # student object carries them into the form) so the user can
                    # correct the selection. No commit happens on this path, so
                    # the in-memory edits are discarded at request teardown.
                    flash('يرجى اختيار منطقة سكن صالحة لهذه المدرسة.', 'danger')
                    return render_template(
                        'students/form.html', student=student, sections=sections,
                        grades=grades, stages=['ابتدائية', 'متوسطة', 'إعدادية'],
                        selected_grade_id=selected_grade_id,
                        selected_stage=selected_stage,
                        active_devices=active_devices,
                        available_parents=available_parents,
                        linked_parents=linked_parents,
                        existing_device_mappings=existing_device_mappings,
                        form_cfg=form_cfg,
                        buildings_enabled=buildings_on,
                        buildings_list=buildings_for_form,
                        selected_building_id=student.building_id,
                        residential_areas_list=residential_areas_for_form,
                    )
                student.residential_area_id = _area_id

        if form_cfg.section_visible('notes'):
            student.notes = request.form.get('notes', '').strip()

        _edit_school_id = school.id if school else None
        if ('photo' in request.files and request.files['photo'].filename
                and is_feature_enabled(_edit_school_id, 'students.photo_upload')):
            photo_path = save_uploaded_file(request.files['photo'], 'students')
            if photo_path:
                student.photo = photo_path

        if is_feature_enabled(_edit_school_id, 'students.documents_upload') and form_cfg.section_visible('student_documents'):
            doc_types = request.form.getlist('document_type[]')
            doc_files = request.files.getlist('document_file[]')
            for doc_type, doc_file in zip(doc_types, doc_files):
                if doc_file and doc_file.filename:
                    saved = save_uploaded_file(
                        doc_file,
                        'students/documents',
                        prefix=f"{student.student_id}_{doc_type or 'document'}"
                    )
                    if saved:
                        db.session.add(StudentDocument(
                            student_id=student.id,
                            document_type=doc_type.strip() or 'وثيقة',
                            file_path=saved,
                        ))

        # ── Attendance device mapping update/create ──────────────────────────
        _emp_no = request.form.get('employee_no_string', '').strip()
        _raw_dev = request.form.get('device_id', '').strip()
        _dev_id  = int(_raw_dev) if _raw_dev.isdigit() else None
        _all_dev = request.form.get('all_devices') == '1'

        if _emp_no:
            if not _emp_no.isdigit():
                flash('رقم الطالب في جهاز الحضور يجب أن يكون أرقاماً فقط', 'warning')
            else:
                _dev_targets = active_devices if _all_dev else (
                    [d for d in active_devices if d.id == _dev_id] if _dev_id else [])
                for _dev in _dev_targets:
                    _existing_map = (DeviceStudentMapping.query
                                     .filter_by(device_id=_dev.id, student_id=student.id)
                                     .first())
                    if _existing_map:
                        _existing_map.employee_no_string = _emp_no
                        _existing_map.is_active = True
                    else:
                        _conflict = (DeviceStudentMapping.query
                                     .filter_by(device_id=_dev.id, employee_no_string=_emp_no)
                                     .first())
                        if _conflict:
                            flash(f'الرقم {_emp_no} مستخدم مسبقاً في جهاز "{_dev.name}"', 'warning')
                        else:
                            db.session.add(DeviceStudentMapping(
                                school_id=school.id,
                                device_id=_dev.id,
                                employee_no_string=_emp_no,
                                student_id=student.id,
                                is_active=True,
                            ))

        # ── Add new parent account (edit form) ──────────────────────────────
        # Guardian profile fields come from the student record (already updated above)
        _np_username = request.form.get('new_parent_username', '').strip()
        _np_password = request.form.get('new_parent_password', '').strip()
        _np_confirm  = request.form.get('new_parent_password_confirm', '').strip()

        _parent_added = False
        if _np_username:
            if not student.guardian_name:
                flash('يرجى إدخال اسم ولي الأمر في قسم "بيانات ولي الأمر" أولاً', 'danger')
            elif not _np_password:
                flash('يجب إدخال كلمة المرور لإنشاء حساب ولي الأمر', 'danger')
            elif _np_password != _np_confirm:
                flash('كلمة المرور غير متطابقة', 'danger')
            elif User.query.filter_by(username=_np_username).first():
                flash('اسم المستخدم مستخدم مسبقاً، الرجاء اختيار اسم مستخدم آخر', 'danger')
            else:
                _np_role = Role.query.filter_by(name='parent').first()
                if _np_role:
                    _np_email_val = None
                    if student.guardian_email and not User.query.filter_by(email=student.guardian_email).first():
                        _np_email_val = student.guardian_email
                    _new_parent = User(
                        username=_np_username,
                        full_name=student.guardian_name,
                        email=_np_email_val,
                        phone=student.guardian_phone or None,
                        school_id=school.id,
                        role_id=_np_role.id,
                        is_active=True,
                    )
                    _new_parent.set_password(_np_password)
                    db.session.add(_new_parent)
                    db.session.flush()
                    db.session.execute(
                        parent_students.insert().values(
                            user_id=_new_parent.id,
                            student_id=student.id,
                            relation=student.guardian_relation or 'guardian',
                        )
                    )
                    _parent_added = True

        # ── Link to existing parent account (edit) ───────────────────────────
        _link_parent_id = request.form.get('link_existing_parent_id', type=int)
        _linked_parent_name = None
        if _link_parent_id and not _parent_added:
            _ep = User.query.filter_by(id=_link_parent_id, school_id=school.id,
                                       role_id=parent_role.id if parent_role else 0).first()
            if _ep:
                _already = db.session.query(parent_students.c.user_id).filter(
                    parent_students.c.user_id == _ep.id,
                    parent_students.c.student_id == student.id,
                ).first()
                if not _already:
                    db.session.execute(
                        parent_students.insert().values(
                            user_id=_ep.id,
                            student_id=student.id,
                            relation=student.guardian_relation or 'guardian',
                        )
                    )
                    _linked_parent_name = _ep.full_name

        db.session.commit()
        flash('تم تحديث بيانات الطالب بنجاح.', 'success')
        if _parent_added:
            flash('تم إنشاء حساب ولي الأمر وربطه بالطالب بنجاح.', 'success')
        if _linked_parent_name:
            flash(f'تم ربط الطالب بولي الأمر {_linked_parent_name} بنجاح.', 'success')
        return redirect(url_for('students.view', student_id=student.id))

    return render_template('students/form.html', student=student, sections=sections,
                           grades=grades, stages=['ابتدائية', 'متوسطة', 'إعدادية'],
                           selected_grade_id=selected_grade_id, selected_stage=selected_stage,
                           active_devices=active_devices,
                           available_parents=available_parents,
                           linked_parents=linked_parents,
                           existing_device_mappings=existing_device_mappings,
                           form_cfg=form_cfg,
                           buildings_enabled=buildings_on,
                           buildings_list=buildings_for_form,
                           selected_building_id=student.building_id,
                           residential_areas_list=residential_areas_for_form)


@students_bp.route('/<int:student_id>')
@login_required
@permission_required('view_students')
def view(student_id):
    student = (Student.query
               .options(joinedload(Student.section).joinedload(Section.grade))
               .execution_options(include_all_years=True)
               .filter(Student.id == student_id)
               .first_or_404())
    school  = get_current_school()

    if school and student.school_id and student.school_id != school.id:
        abort(403)

    # Building scope — restricted users cannot view students outside their buildings.
    if not user_can_access_student(current_user, school, student):
        flash('ليس لديك صلاحية الوصول إلى بيانات هذه البناية', 'danger')
        return redirect(url_for('students.index'))

    if _is_teacher():
        section_ids = get_teacher_section_ids(current_user)
        if student.section_id not in section_ids:
            flash('لا يمكنك عرض بيانات طالب خارج شعبتك.', 'danger')
            return redirect(url_for('students.index'))

    docs = student.documents.order_by(StudentDocument.uploaded_at.desc()).all()

    # Fee records — only loaded for users authorised to manage fees.
    # Scoped by student.id + student.school_id; spans all academic years.
    fee_records_with_inst = []
    if current_user.has_permission('manage_fees'):
        _fee_records = (
            FeeRecord.query
            .execution_options(include_all_years=True)
            .filter_by(student_id=student.id, school_id=student.school_id)
            .options(
                joinedload(FeeRecord.fee_type),
                joinedload(FeeRecord.academic_year),
            )
            .order_by(FeeRecord.created_at.desc())
            .all()
        )
        if _fee_records:
            _fee_ids = [fr.id for fr in _fee_records]
            _all_inst = (
                FeeInstallment.query
                .execution_options(include_all_years=True)
                .filter(
                    FeeInstallment.fee_record_id.in_(_fee_ids),
                    FeeInstallment.school_id == student.school_id,
                )
                .order_by(FeeInstallment.installment_no)
                .all()
            )
            _inst_map = {}
            for _inst in _all_inst:
                _inst_map.setdefault(_inst.fee_record_id, []).append(_inst)
            fee_records_with_inst = [
                (fr, _inst_map.get(fr.id, [])) for fr in _fee_records
            ]

    return render_template('students/view.html', student=student, docs=docs,
                           fee_records_with_inst=fee_records_with_inst)


@students_bp.route('/<int:student_id>/archive', methods=['POST'])
@login_required
@historical_guard
@permission_required('delete_student')
@feature_required('students.delete')
def archive(student_id):
    """Set a student's status to archived (or any non-active status).
    This is the safe, data-preserving alternative to hard delete for school managers."""
    student = Student.query.execution_options(include_all_years=True).get_or_404(student_id)
    school  = get_current_school()

    if school and student.school_id and student.school_id != school.id:
        abort(403)
    if not user_can_access_student(current_user, school, student):
        flash('ليس لديك صلاحية الوصول إلى بيانات هذه البناية', 'danger')
        return redirect(url_for('students.index'))
    if _is_teacher():
        flash('المعلمون لا يملكون صلاحية أرشفة الطلاب.', 'danger')
        return redirect(url_for('students.index'))

    new_status = request.form.get('status', 'archived')
    _valid_statuses = {'archived', 'withdrawn', 'transferred', 'graduated', 'active'}
    if new_status not in _valid_statuses:
        new_status = 'archived'

    _labels = {'archived': 'مؤرشف', 'withdrawn': 'مسحوب',
               'transferred': 'منقول', 'graduated': 'متخرج', 'active': 'فعّال'}
    student.status = new_status
    db.session.commit()
    flash(f'تم تغيير حالة الطالب {student.full_name} إلى {_labels[new_status]}.', 'success')
    next_url = request.form.get('next') or url_for('students.index')
    return redirect(next_url)


@students_bp.route('/<int:student_id>/delete', methods=['POST'])
@login_required
@historical_guard
@permission_required('delete_student')
@feature_required('students.delete')
def delete(student_id):
    """Permanent hard delete — super_admin only.
    School managers must use the archive route instead."""
    if not current_user.is_super_admin:
        flash('الحذف النهائي مقتصر على مسؤول النظام الأعلى فقط. '
              'استخدم خيار الأرشفة لإخفاء الطالب من القوائم النشطة.', 'danger')
        return redirect(url_for('students.view', student_id=student_id))

    student = Student.query.execution_options(include_all_years=True).get_or_404(student_id)
    school  = get_current_school()

    if school and student.school_id and student.school_id != school.id:
        abort(403)

    name = student.full_name
    sid  = student.id

    # Remove M2M parent links (no DB-side CASCADE)
    db.session.execute(
        parent_students.delete().where(parent_students.c.student_id == sid)
    )

    # Explicitly delete all child records via raw SQL to bypass:
    # a) lazy='dynamic' cascade unreliability
    # b) ORM year-scope filtering that leaves cross-year rows orphaned
    from sqlalchemy import text
    db.session.execute(
        text("DELETE FROM fee_installments"
             " WHERE fee_record_id IN (SELECT id FROM fee_records WHERE student_id = :sid)"),
        {'sid': sid},
    )
    for tbl in ('complaints', 'leave_requests', 'student_attendance', 'fee_records',
                'exam_results', 'student_documents', 'student_suspensions'):
        db.session.execute(text(f"DELETE FROM {tbl} WHERE student_id = :sid"), {'sid': sid})

    db.session.delete(student)
    db.session.commit()
    flash(f'تم حذف الطالب {name} وجميع سجلاته بشكل نهائي.', 'success')
    return redirect(url_for('students.index'))


@students_bp.route('/<int:student_id>/unlink-parent', methods=['POST'])
@login_required
@historical_guard
@permission_required('edit_student')
def unlink_parent(student_id):
    """Remove a parent ↔ student link from the parent_students junction."""
    student = Student.query.execution_options(include_all_years=True).get_or_404(student_id)
    school  = get_current_school()
    if school and student.school_id and student.school_id != school.id:
        abort(403)

    parent_id = request.form.get('parent_user_id', type=int)
    if parent_id:
        parent = User.query.filter_by(id=parent_id, school_id=school.id).first()
        if parent:
            db.session.execute(
                parent_students.delete().where(
                    (parent_students.c.user_id    == parent_id) &
                    (parent_students.c.student_id == student_id)
                )
            )
            db.session.commit()
            flash(f'تم إلغاء ربط ولي الأمر {parent.full_name} من هذا الطالب.', 'success')
        else:
            flash('ولي الأمر غير موجود أو لا ينتمي لهذه المدرسة.', 'danger')
    return redirect(url_for('students.edit', student_id=student_id))


@students_bp.route('/export/excel')
@login_required
@permission_required('view_students')
@feature_required('students.export')
def export_excel():
    from flask import Response
    from app.utils.excel_export import export_students
    school = get_current_school()
    year   = get_view_year(school.id) if school else None
    status = request.args.get('status', 'active')

    query = Student.query.filter_by(status=status)
    if request.args.get('all_years', '0') == '1':
        query = query.execution_options(include_all_years=True)
    if school:
        query = query.filter_by(school_id=school.id)
    if year and request.args.get('all_years', '0') != '1':
        query = query.filter_by(academic_year_id=year.id)
    # Building scope — restricted users only export their own buildings' students.
    query = apply_building_scope_to_students(query, current_user, school)
    if _is_teacher():
        section_ids = get_teacher_section_ids(current_user)
        query = query.filter(Student.section_id.in_(section_ids)) if section_ids \
                else query.filter(Student.id == -1)

    students = query.order_by(Student.full_name).all()
    data = export_students(students)
    if not data:
        flash('مكتبة Excel غير متاحة.', 'warning')
        return redirect(url_for('students.index'))
    return Response(
        data,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename=students_{status}.xlsx'}
    )


# ═════════════════════════════════════════════════════════════════════════════
#  RESIDENTIAL AREAS (مناطق السكن) — per-school lookup list management
#  Same isolation pattern as the buildings blueprint: every query is scoped by
#  the trusted server-side school id; ids from other schools resolve to 404.
# ═════════════════════════════════════════════════════════════════════════════

def _require_school_for_areas():
    """Return the current school or None (caller redirects). Super admin must
    have an active school selected — areas are always owned by one school."""
    school = get_current_school()
    if not school:
        flash('يرجى اختيار مدرسة أولاً.', 'warning')
    return school


@students_bp.route('/residential-areas')
@login_required
@permission_required('manage_residential_areas')
def residential_areas():
    school = _require_school_for_areas()
    if not school:
        return redirect(url_for('students.index'))

    areas = _school_residential_areas(school.id)

    # Linked-student counts per area (all statuses, all years) — scoped to this
    # school only so totals can never include another school's students.
    counts = dict(
        db.session.query(Student.residential_area_id, db.func.count(Student.id))
        .execution_options(bypass_tenant_scope=True, include_all_years=True)
        .filter(Student.school_id == school.id,
                Student.residential_area_id.isnot(None))
        .group_by(Student.residential_area_id)
        .all()
    )

    return render_template('students/residential_areas.html',
                           areas=areas, student_counts=counts)


@students_bp.route('/residential-areas/new', methods=['GET', 'POST'])
@login_required
@permission_required('manage_residential_areas')
def residential_area_new():
    school = _require_school_for_areas()
    if not school:
        return redirect(url_for('students.index'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        is_active = request.form.get('is_active', '1') == '1'

        if not name:
            flash('اسم المنطقة مطلوب.', 'danger')
            return render_template('students/residential_area_form.html',
                                   area=None, form_name=name)
        if len(name) > 200:
            flash('اسم المنطقة طويل جداً (الحد الأقصى 200 حرف).', 'danger')
            return render_template('students/residential_area_form.html',
                                   area=None, form_name=name[:200])

        # Prevent duplicate names within the same school.
        existing = (ResidentialArea.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter_by(school_id=school.id, name=name)
                    .first())
        if existing:
            flash('توجد منطقة سكن بنفس الاسم في هذه المدرسة.', 'danger')
            return render_template('students/residential_area_form.html',
                                   area=None, form_name=name)

        area = ResidentialArea(school_id=school.id, name=name, is_active=is_active)
        db.session.add(area)
        db.session.commit()
        log_action('create', 'residential_area', area.id,
                   details=f'created residential area "{name}"')
        flash(f'تم إنشاء منطقة السكن "{name}" بنجاح.', 'success')
        return redirect(url_for('students.residential_areas'))

    return render_template('students/residential_area_form.html', area=None)


@students_bp.route('/residential-areas/<int:area_id>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('manage_residential_areas')
def residential_area_edit(area_id):
    school = _require_school_for_areas()
    if not school:
        return redirect(url_for('students.index'))
    area = _get_residential_area_or_404(area_id, school)

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        is_active = request.form.get('is_active', '1') == '1'

        if not name:
            flash('اسم المنطقة مطلوب.', 'danger')
            return render_template('students/residential_area_form.html', area=area)
        if len(name) > 200:
            flash('اسم المنطقة طويل جداً (الحد الأقصى 200 حرف).', 'danger')
            return render_template('students/residential_area_form.html', area=area)

        duplicate = (ResidentialArea.query
                     .execution_options(bypass_tenant_scope=True)
                     .filter(ResidentialArea.school_id == school.id,
                             ResidentialArea.name == name,
                             ResidentialArea.id != area.id)
                     .first())
        if duplicate:
            flash('توجد منطقة سكن أخرى بنفس الاسم في هذه المدرسة.', 'danger')
            return render_template('students/residential_area_form.html', area=area)

        area.name = name
        area.is_active = is_active
        db.session.commit()
        log_action('edit', 'residential_area', area.id,
                   details=f'updated residential area "{name}"')
        flash('تم تحديث بيانات منطقة السكن بنجاح.', 'success')
        return redirect(url_for('students.residential_areas'))

    return render_template('students/residential_area_form.html', area=area)


@students_bp.route('/residential-areas/<int:area_id>/toggle-active', methods=['POST'])
@login_required
@permission_required('manage_residential_areas')
def residential_area_toggle_active(area_id):
    school = _require_school_for_areas()
    if not school:
        return redirect(url_for('students.index'))
    area = _get_residential_area_or_404(area_id, school)

    area.is_active = not area.is_active
    db.session.commit()
    state = 'تفعيل' if area.is_active else 'تعطيل'
    log_action('edit', 'residential_area', area.id,
               details=f'{state} residential area "{area.name}"')
    flash(f'تم {state} منطقة السكن "{area.name}".', 'success')
    return redirect(url_for('students.residential_areas'))


@students_bp.route('/residential-areas/<int:area_id>/delete', methods=['POST'])
@login_required
@permission_required('manage_residential_areas')
def residential_area_delete(area_id):
    school = _require_school_for_areas()
    if not school:
        return redirect(url_for('students.index'))
    area = _get_residential_area_or_404(area_id, school)

    # Block delete while students (any status, any year) are still linked, to
    # avoid orphaning data. Deactivate or relink students instead.
    linked = (Student.query
              .execution_options(bypass_tenant_scope=True, include_all_years=True)
              .filter_by(school_id=school.id, residential_area_id=area.id)
              .count())
    if linked:
        flash(
            f'لا يمكن حذف منطقة السكن "{area.name}" لوجود {linked} طالب مرتبط بها. '
            'قم بتعطيل المنطقة أو نقل الطلاب إلى منطقة أخرى بدلاً من حذفها.',
            'danger',
        )
        return redirect(url_for('students.residential_areas'))

    name = area.name
    db.session.delete(area)
    db.session.commit()
    log_action('delete', 'residential_area', area_id,
               details=f'deleted residential area "{name}"')
    flash(f'تم حذف منطقة السكن "{name}".', 'success')
    return redirect(url_for('students.residential_areas'))

"""Mecha-School — Students Blueprint  (Phase 6: multi-tenant + capacity check)"""
from decimal import Decimal, InvalidOperation

from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, abort)
from flask_login import login_required, current_user
from sqlalchemy.orm import joinedload
from app.models import (db, Student, Section, Grade, AcademicYear, StudentDocument,
                        parent_students, User, Role, AttendanceDevice, DeviceStudentMapping,
                        FeeRecord, FeeInstallment, FeeType, Revenue, RevenueCategory)
from app.utils.decorators import (permission_required, get_teacher_section_ids,
                                   get_current_school, get_active_year, get_view_year,
                                   historical_guard)
from app.utils.helpers import save_uploaded_file, generate_receipt_no
from app.utils import code_generator
from app.utils.features import feature_required, is_feature_enabled
from app.utils.student_form_config import get_student_form_config
from app.utils.buildings import (
    school_buildings_enabled, get_active_buildings,
    apply_building_scope_to_students, user_allowed_building_ids,
    user_can_access_student, validate_building_for_school,
)

students_bp = Blueprint('students', __name__,
                         template_folder='../../templates/students')


def _is_teacher():
    return (current_user.is_authenticated and
            current_user.role and
            current_user.role.name == 'teacher')


@students_bp.route('/')
@login_required
@permission_required('view_students')
def index():
    page       = request.args.get('page', 1, type=int)
    search     = request.args.get('q', '')
    status     = request.args.get('status', '')
    grade_id   = request.args.get('grade_id', type=int)
    section_id = request.args.get('section_id', type=int)
    building_id = request.args.get('building_id', type=int)
    school     = get_current_school()
    year       = get_view_year(school.id) if school else None

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

    students = (query
                .options(joinedload(Student.section).joinedload(Section.grade))
                .execution_options(include_all_years=True)
                .order_by(Student.created_at.desc())
                .paginate(page=page, per_page=20, error_out=False))

    # Grades and sections for filter dropdowns
    grades_q   = Grade.query
    sections_q = Section.query
    if school and year:
        grades_q   = grades_q.filter_by(school_id=school.id, academic_year_id=year.id)
        sections_q = sections_q.filter_by(school_id=school.id, academic_year_id=year.id)
    grades_list   = grades_q.order_by(Grade.name).all()
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

    return render_template('students/index.html',
                           students=students, search=search, status=status,
                           capacity_info=capacity_info,
                           active_year=year,
                           grades_list=grades_list,
                           sections_list=sections_list,
                           grade_id=grade_id,
                           section_id=section_id,
                           buildings_enabled=buildings_on,
                           buildings_list=buildings_list,
                           building_id=building_id)


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

    if request.method == 'POST':
        # ── Pre-validate parent account and device mapping fields ────────────
        parent_username         = request.form.get('parent_username', '').strip()
        parent_password         = request.form.get('parent_password', '').strip()
        parent_password_confirm = request.form.get('parent_password_confirm', '').strip()
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
                                   active_year=year,
                                   can_manage_fees=can_manage_fees,
                                   fee_types=fee_types,
                                   error_step=error_step)

        # ── Backend enforcement of required fields per school config ─────────
        _cfg_errors = form_cfg.validate(request.form)
        if _cfg_errors:
            for _err in _cfg_errors:
                flash(_err, 'danger')
            return _re_render('')

        # Auto-generate parent username when password is supplied but username is blank
        if parent_password and not parent_username:
            parent_username = code_generator.generate_username(school.id, 'parent')

        if parent_username:
            if not parent_password:
                return _re_render('يجب إدخال كلمة المرور لإنشاء حساب ولي الأمر', error_step=4)
            if parent_password != parent_password_confirm:
                return _re_render('كلمة المرور غير متطابقة', error_step=4)
            if User.query.filter_by(username=parent_username).first():
                return _re_render('اسم المستخدم مستخدم مسبقاً، يرجى اختيار اسم مستخدم آخر', error_step=4)

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

        # ── Create parent user account ───────────────────────────────────────
        parent_created = False
        if parent_username:
            parent_role = Role.query.filter_by(name='parent').first()
            if parent_role:
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
        _link_parent_id = request.form.get('link_existing_parent_id', type=int)
        _linked_parent_name = None
        if _link_parent_id and not parent_created:
            _ep = User.query.filter_by(id=_link_parent_id, school_id=school.id,
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
            from app.blueprints.fees import persist_fee_record, FeeValidationError
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
                    _inst = (FeeInstallment.query
                             .filter_by(fee_record_id=_fee_record.id,
                                        installment_no=_wi,
                                        school_id=school.id)
                             .first())
                    if _inst is None:
                        continue
                    # Cap at installment amount — never record an overpayment.
                    _inst_amount = Decimal(str(_inst.amount))
                    if _received > _inst_amount:
                        _received = _inst_amount
                    _inst.received_amount = float(_received)
                    _inst.payment_method  = request.form.get(f'pay_method_{_wi}', 'cash')
                    _inst.collected_by    = current_user.id
                    _pay_date_str = request.form.get(f'pay_date_{_wi}', '').strip()
                    if _pay_date_str:
                        try:
                            _inst.paid_date = dt.strptime(_pay_date_str, '%Y-%m-%d').date()
                        except ValueError:
                            _inst.paid_date = dt.today().date()
                    else:
                        _inst.paid_date = dt.today().date()
                    _pay_notes = request.form.get(f'pay_notes_{_wi}', '').strip()
                    if _pay_notes:
                        _inst.notes = _pay_notes
                    _inst.recompute_status()
                    if not _inst.receipt_no:
                        _inst.receipt_no = generate_receipt_no()
                    _fee_payment_data.append({
                        'school_id':        school.id,
                        'academic_year_id': year.id,
                        'amount':           float(_received),
                        'installment_no':   _wi,
                        'paid_date':        _inst.paid_date,
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

            except (FeeValidationError, ValueError):
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
        flash(f'تم إضافة الطالب {student.full_name} برقم {student.student_id}.', 'success')
        if parent_created:
            flash(f'تم إنشاء حساب ولي الأمر بنجاح. اسم المستخدم: {parent_username}', 'success')
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
                           active_year=year,
                           can_manage_fees=can_manage_fees,
                           fee_types=fee_types,
                           error_step=1)


@students_bp.route('/<int:student_id>/create-success')
@login_required
@permission_required('add_student')
def create_success(student_id):
    """Success landing page shown immediately after a new student is created."""
    school = get_current_school()
    student = (Student.query
               .execution_options(include_all_years=True)
               .filter_by(id=student_id)
               .first_or_404())
    if school and student.school_id != school.id:
        abort(403)
    return render_template('students/create_success.html', student=student)


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
                           selected_building_id=student.building_id)


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

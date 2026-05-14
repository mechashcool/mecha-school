"""Central school/year scoping for tenant-safe ORM access.

Use execution_options(bypass_tenant_scope=True) only for super-admin system
workflows and migrations/backfills. Use include_all_years=True for historical
queries that are still school-scoped but intentionally span academic years.
"""
from __future__ import annotations

from datetime import date

from flask import g, has_request_context, session
from flask_login import current_user
from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

_REGISTERED = False


def _models():
    from app.models import (
        AcademicYear, Announcement, AuditLog, Device, Employee,
        EmployeeAttendance, EmployeeDocument, EmployeeEvaluation, Exam,
        ExamResult, Expense, ExpenseCategory, FeeInstallment, FeeRecord,
        FeeType, Grade, Notification, PushNotification, Revenue,
        RevenueCategory, SalaryRecord, Schedule,
        Section, Student, StudentAttendance, StudentDocument, StudentSuspension,
        Subject, User,
    )

    school_scoped = (
        AcademicYear, Announcement, AuditLog, Device, Employee,
        EmployeeAttendance, EmployeeDocument, EmployeeEvaluation, Exam,
        ExamResult, Expense, ExpenseCategory, FeeInstallment, FeeRecord,
        FeeType, Grade, Notification, PushNotification, Revenue,
        RevenueCategory, SalaryRecord, Schedule,
        Section, Student, StudentAttendance, StudentDocument, StudentSuspension,
        Subject, User,
    )
    # Student, StudentDocument, StudentSuspension are school-scoped only —
    # they persist across academic years so that a year rollover does not
    # require re-entering master student data.
    year_scoped = (
        EmployeeAttendance, EmployeeEvaluation, Exam, ExamResult, Expense,
        FeeInstallment, FeeRecord, FeeType, Grade, Revenue, SalaryRecord,
        Schedule, Section, StudentAttendance, Subject,
    )
    return school_scoped, year_scoped


def scoped_query(model, *, include_all_years: bool = False, bypass: bool = False):
    """Return a model query with explicit tenant-scope options.

    This is mostly a readability helper. The ORM event still applies school
    scope automatically; this helper makes intentional historical or
    super-admin global queries stand out in route/service code.
    """
    query = model.query
    if include_all_years:
        query = query.execution_options(include_all_years=True)
    if bypass:
        query = query.execution_options(bypass_tenant_scope=True)
    return query


def current_school_id() -> int | None:
    """School id for the current request scope, or None for global super-admin."""
    if not has_request_context():
        return None

    if hasattr(g, 'tenant_scope_school_id'):
        return g.tenant_scope_school_id

    try:
        user = current_user._get_current_object()
    except Exception:
        return None

    if not getattr(user, 'is_authenticated', False):
        return None

    # In the strict model, school_id NULL is reserved for role.name=super_admin.
    # Avoid user.is_super_admin here because it may lazy-load Role while the ORM
    # scoping event is itself processing a lazy load.
    if user.school_id is None:
        if _current_role_name(user) == 'super_admin':
            active_id = session.get('active_school_id')
            return int(active_id) if active_id else None
        return -1

    # A non-super user without a school must not see school-owned data.
    return user.school_id if user.school_id is not None else -1


def current_academic_year_id() -> int | None:
    """Current ACTIVE academic year id — used for write operations."""
    if not has_request_context():
        return None
    if hasattr(g, 'tenant_scope_academic_year_id'):
        return g.tenant_scope_academic_year_id
    return None


def current_view_year_id() -> int | None:
    """Current VIEW academic year id — may be a historical year selected by user.
    Used for read/display queries. Differs from current_academic_year_id() only
    when a school manager has selected a historical year in the session."""
    if not has_request_context():
        return None
    if hasattr(g, 'tenant_scope_view_year_id'):
        return g.tenant_scope_view_year_id
    return None


def is_historical_view() -> bool:
    """True when the current request is viewing a historical (non-current) academic year.
    Used by historical_guard decorator to block write operations."""
    if not has_request_context():
        return False
    active_id = getattr(g, 'tenant_scope_academic_year_id', None)
    view_id = getattr(g, 'tenant_scope_view_year_id', None)
    if active_id is None or view_id is None:
        return False
    return view_id != active_id


def _is_super_admin() -> bool:
    if not has_request_context():
        return False
    try:
        user = current_user._get_current_object()
    except Exception:
        return False
    return bool(getattr(user, 'is_authenticated', False)
                and getattr(user, 'school_id', None) is None
                and _current_role_name(user) == 'super_admin')


def _current_role_name(user=None) -> str | None:
    if not has_request_context():
        return None

    if user is None:
        try:
            user = current_user._get_current_object()
        except Exception:
            return None

    cached = getattr(g, 'tenant_scope_role_name', None)
    if cached is not None:
        return cached

    role = user.__dict__.get('role')
    if role is not None:
        return getattr(role, 'name', None)

    role_id = getattr(user, 'role_id', None)
    if role_id is None:
        return None

    from app.models import db
    return db.session.execute(
        db.text('SELECT name FROM roles WHERE id = :role_id'),
        {'role_id': role_id},
    ).scalar()


def _academic_year_for_date(db_session, school_id: int | None, day: date | None):
    from app.models import AcademicYear

    if not school_id:
        return None
    query = db_session.query(AcademicYear).execution_options(
        bypass_tenant_scope=True
    ).filter(AcademicYear.school_id == school_id)
    if day:
        match = query.filter(AcademicYear.start_date <= day,
                             AcademicYear.end_date >= day).first()
        if match:
            return match
    return query.filter(AcademicYear.is_current == True).first()


def _set_request_scope():
    """Cache request scope once so ORM events do not need to query repeatedly."""
    if not has_request_context():
        return

    try:
        g.tenant_scope_role_name = _current_role_name()
    except Exception:
        g.tenant_scope_role_name = None

    sid = current_school_id()
    g.tenant_scope_school_id = sid
    g.tenant_scope_academic_year_id = None  # always the CURRENT active year (for writes)
    g.tenant_scope_view_year_id = None      # view year for reads (may be historical)

    if sid and sid > 0:
        from app.models import AcademicYear

        active_year_id = (
            AcademicYear.query.execution_options(bypass_tenant_scope=True)
            .with_entities(AcademicYear.id)
            .filter_by(school_id=sid, is_current=True)
            .scalar()
        )
        g.tenant_scope_academic_year_id = active_year_id

        # Check whether the user has selected a historical view year in the session.
        # Super-admin uses school switching instead; skip view-year for them.
        view_year_id = session.get('view_year_id')
        if view_year_id and not _is_super_admin():
            # Validate the session year belongs to the current school.
            valid = (
                AcademicYear.query.execution_options(bypass_tenant_scope=True)
                .with_entities(AcademicYear.id)
                .filter_by(id=view_year_id, school_id=sid)
                .scalar()
            )
            if valid:
                g.tenant_scope_view_year_id = view_year_id
            else:
                try:
                    session.pop('view_year_id', None)
                except RuntimeError:
                    pass
                g.tenant_scope_view_year_id = active_year_id
        else:
            g.tenant_scope_view_year_id = active_year_id


def set_hardware_scope(device):
    """Bind an unauthenticated hardware request to its device's school."""
    if not has_request_context() or not device:
        return

    sid = device.school_id
    g.tenant_scope_school_id = sid
    g.tenant_scope_academic_year_id = None
    g.tenant_scope_view_year_id = None
    if sid:
        from app.models import AcademicYear

        year_id = (
            AcademicYear.query.execution_options(bypass_tenant_scope=True)
            .with_entities(AcademicYear.id)
            .filter_by(school_id=sid, is_current=True)
            .scalar()
        )
        g.tenant_scope_academic_year_id = year_id
        g.tenant_scope_view_year_id = year_id  # hardware always uses the current active year


def _inherit_scope(session_, obj):
    """Populate school/year from strong parent relationships before validation."""
    from app.models import (
        AcademicYear, Employee, EmployeeAttendance, EmployeeDocument,
        EmployeeEvaluation, Exam, ExamResult, Expense, FeeInstallment,
        FeeRecord, Grade, Notification, PushNotification, Revenue, SalaryRecord, Schedule,
        Section, Student, StudentAttendance, StudentDocument, StudentSuspension,
        Subject, User,
    )

    def load(model, ident):
        if ident is None:
            return None
        return session_.get(
            model, ident,
            execution_options={'bypass_tenant_scope': True},
        )

    def copy_scope(parent, *, year=True):
        if not parent:
            return
        if getattr(obj, 'school_id', None) is None and hasattr(parent, 'school_id'):
            obj.school_id = parent.school_id
        if year and getattr(obj, 'academic_year_id', None) is None and hasattr(parent, 'academic_year_id'):
            obj.academic_year_id = parent.academic_year_id

    if isinstance(obj, Grade):
        copy_scope(load(AcademicYear, obj.academic_year_id), year=False)
    elif isinstance(obj, Section):
        grade = obj.grade or load(Grade, obj.grade_id)
        copy_scope(grade)
    elif isinstance(obj, Subject):
        if obj.school_id is None:
            obj.school_id = current_school_id()
        if obj.academic_year_id is None:
            obj.academic_year_id = current_academic_year_id()
    elif isinstance(obj, Student):
        section = obj.section or load(Section, obj.section_id)
        copy_scope(section)
    elif isinstance(obj, StudentDocument):
        student = obj.student or load(Student, obj.student_id)
        if student and getattr(obj, 'school_id', None) is None:
            obj.school_id = student.school_id
        # StudentDocument is school-scoped only; keep academic_year_id as the
        # year it was uploaded (current active year) rather than enrollment year.
        if getattr(obj, 'academic_year_id', None) is None:
            obj.academic_year_id = current_academic_year_id()
    elif isinstance(obj, EmployeeDocument):
        copy_scope(obj.employee or load(Employee, obj.employee_id), year=False)
    elif isinstance(obj, FeeRecord):
        student = obj.student or load(Student, obj.student_id)
        if student and getattr(obj, 'school_id', None) is None:
            obj.school_id = student.school_id
        # Fee records belong to the current active year, not the student's
        # enrollment year.
        if getattr(obj, 'academic_year_id', None) is None:
            obj.academic_year_id = current_academic_year_id()
    elif isinstance(obj, FeeInstallment):
        copy_scope(obj.fee_record or load(FeeRecord, obj.fee_record_id))
    elif isinstance(obj, (Revenue, Expense)):
        if obj.school_id is None:
            obj.school_id = current_school_id()
        if obj.academic_year_id is None:
            ay = _academic_year_for_date(session_, obj.school_id, obj.date)
            obj.academic_year_id = ay.id if ay else current_academic_year_id()
    elif isinstance(obj, SalaryRecord):
        copy_scope(obj.employee or load(Employee, obj.employee_id), year=False)
        if obj.academic_year_id is None:
            salary_date = date(obj.year, obj.month, 1) if obj.year and obj.month else None
            ay = _academic_year_for_date(session_, obj.school_id, salary_date)
            obj.academic_year_id = ay.id if ay else current_academic_year_id()
    elif isinstance(obj, StudentAttendance):
        student = obj.student or load(Student, obj.student_id)
        if student and getattr(obj, 'school_id', None) is None:
            obj.school_id = student.school_id
        # Derive year from the attendance date so records land in the correct
        # year even when the student's enrollment year differs.
        if getattr(obj, 'academic_year_id', None) is None:
            ay = _academic_year_for_date(session_, obj.school_id, obj.date)
            obj.academic_year_id = ay.id if ay else current_academic_year_id()
    elif isinstance(obj, EmployeeAttendance):
        copy_scope(obj.employee or load(Employee, obj.employee_id), year=False)
        if obj.academic_year_id is None:
            ay = _academic_year_for_date(session_, obj.school_id, obj.date)
            obj.academic_year_id = ay.id if ay else current_academic_year_id()
    elif isinstance(obj, Exam):
        copy_scope(obj.section or load(Section, obj.section_id))
    elif isinstance(obj, ExamResult):
        copy_scope(obj.exam or load(Exam, obj.exam_id))
    elif isinstance(obj, EmployeeEvaluation):
        employee = obj.employee or load(Employee, obj.employee_id)
        copy_scope(employee, year=False)
        if obj.academic_year_id is None:
            ay = _academic_year_for_date(
                session_,
                employee.school_id if employee else obj.school_id,
                date.today(),
            )
            obj.academic_year_id = ay.id if ay else current_academic_year_id()
    elif isinstance(obj, Schedule):
        copy_scope(obj.section or load(Section, obj.section_id))
    elif isinstance(obj, StudentSuspension):
        student = obj.student or load(Student, obj.student_id)
        if student and getattr(obj, 'school_id', None) is None:
            obj.school_id = student.school_id
        # Derive year from suspension start date so cross-year suspensions work.
        if getattr(obj, 'academic_year_id', None) is None:
            ay = _academic_year_for_date(session_, obj.school_id, obj.start_date)
            obj.academic_year_id = ay.id if ay else current_academic_year_id()
    elif isinstance(obj, PushNotification):
        copy_scope(obj.user or load(User, obj.user_id), year=False)
    elif isinstance(obj, Notification):
        copy_scope(obj.target_user or load(User, obj.target_user_id), year=False)


def _validate_relationship_scope(session_, obj):
    """Reject rows whose direct relationships cross school/year boundaries."""
    from app.models import (
        AcademicYear, Employee, EmployeeAttendance, EmployeeDocument,
        EmployeeEvaluation, Exam, ExamResult, Expense, FeeInstallment,
        FeeRecord, Grade, Revenue, RevenueCategory, SalaryRecord, Schedule,
        Section, Student, StudentAttendance, StudentDocument, StudentSuspension,
        Subject, ExpenseCategory, Notification, User,
    )

    def load(model, ident):
        if ident is None:
            return None
        return session_.get(
            model, ident,
            execution_options={'bypass_tenant_scope': True},
        )

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    school_id = getattr(obj, 'school_id', None)
    year_id = getattr(obj, 'academic_year_id', None)

    if school_id is not None and year_id is not None:
        year = load(AcademicYear, year_id)
        require(year is not None and year.school_id == school_id,
                f'{obj.__class__.__name__} academic_year_id does not belong to school_id')

    if isinstance(obj, Grade):
        year = load(AcademicYear, obj.academic_year_id)
        require(year and obj.school_id == year.school_id,
                'Grade must belong to the same school as its academic year')
    elif isinstance(obj, Section):
        grade = obj.grade or load(Grade, obj.grade_id)
        require(grade and grade.school_id == obj.school_id
                and grade.academic_year_id == obj.academic_year_id,
                'Section must match its grade school/year')
    elif isinstance(obj, Subject):
        pass
    elif isinstance(obj, Student):
        section = obj.section or load(Section, obj.section_id)
        if section:
            require(section.school_id == obj.school_id
                    and section.academic_year_id == obj.academic_year_id,
                    'Student section must match student school/year')
    elif isinstance(obj, StudentDocument):
        student = obj.student or load(Student, obj.student_id)
        require(student and student.school_id == obj.school_id,
                'StudentDocument must match student school')
    elif isinstance(obj, EmployeeDocument):
        employee = obj.employee or load(Employee, obj.employee_id)
        require(employee and employee.school_id == obj.school_id,
                'EmployeeDocument must match employee school')
    elif isinstance(obj, FeeRecord):
        student = obj.student or load(Student, obj.student_id)
        require(student and student.school_id == obj.school_id,
                'FeeRecord must be for a student in the same school')
    elif isinstance(obj, FeeInstallment):
        record = obj.fee_record or load(FeeRecord, obj.fee_record_id)
        require(record and record.school_id == obj.school_id
                and record.academic_year_id == obj.academic_year_id,
                'FeeInstallment must match fee record school/year')
    elif isinstance(obj, Revenue):
        category = obj.category or load(RevenueCategory, obj.category_id)
        require(category and category.school_id == obj.school_id,
                'Revenue category must match revenue school')
    elif isinstance(obj, Expense):
        category = obj.category or load(ExpenseCategory, obj.category_id)
        require(category and category.school_id == obj.school_id,
                'Expense category must match expense school')
    elif isinstance(obj, SalaryRecord):
        employee = obj.employee or load(Employee, obj.employee_id)
        require(employee and employee.school_id == obj.school_id,
                'SalaryRecord must match employee school')
    elif isinstance(obj, StudentAttendance):
        student = obj.student or load(Student, obj.student_id)
        require(student and student.school_id == obj.school_id,
                'StudentAttendance must be for a student in the same school')
    elif isinstance(obj, EmployeeAttendance):
        employee = obj.employee or load(Employee, obj.employee_id)
        require(employee and employee.school_id == obj.school_id,
                'EmployeeAttendance must match employee school')
    elif isinstance(obj, Exam):
        section = obj.section or load(Section, obj.section_id)
        require(section and section.school_id == obj.school_id
                and section.academic_year_id == obj.academic_year_id,
                'Exam must match section school/year')
        subject = obj.subject or load(Subject, obj.subject_id)
        if subject:
            require(subject.school_id == obj.school_id
                    and subject.academic_year_id == obj.academic_year_id,
                    'Exam subject must match exam school/year')
    elif isinstance(obj, ExamResult):
        exam = obj.exam or load(Exam, obj.exam_id)
        student = obj.student or load(Student, obj.student_id)
        require(exam and exam.school_id == obj.school_id
                and exam.academic_year_id == obj.academic_year_id,
                'ExamResult must match exam school/year')
        require(student and student.school_id == obj.school_id,
                'ExamResult student must be in the same school')
    elif isinstance(obj, EmployeeEvaluation):
        employee = obj.employee or load(Employee, obj.employee_id)
        require(employee and employee.school_id == obj.school_id,
                'EmployeeEvaluation must match employee school')
    elif isinstance(obj, Notification):
        if obj.target_user_id:
            target_user = obj.target_user or load(User, obj.target_user_id)
            require(target_user and target_user.school_id == obj.school_id,
                    'Notification target user must match notification school')
            require(obj.target_role is None,
                    'Notification cannot target both a user and a role')
    elif isinstance(obj, Schedule):
        section = obj.section or load(Section, obj.section_id)
        require(section and section.school_id == obj.school_id
                and section.academic_year_id == obj.academic_year_id,
                'Schedule must match section school/year')
    elif isinstance(obj, StudentSuspension):
        student = obj.student or load(Student, obj.student_id)
        require(student and student.school_id == obj.school_id,
                'StudentSuspension must match student school')


def _before_flush(session_, flush_context, instances):
    if session_.info.get('skip_tenant_validation'):
        return

    sid = current_school_id()
    yid = current_academic_year_id()
    super_admin = _is_super_admin()

    with session_.no_autoflush:
        for obj in list(session_.new) + list(session_.dirty):
            if not getattr(obj, '__school_scoped__', False):
                continue

            _inherit_scope(session_, obj)

            if hasattr(obj, 'school_id') and getattr(obj, 'school_id', None) is None:
                if sid and sid > 0:
                    obj.school_id = sid

            if getattr(obj, '__year_scoped__', False):
                if hasattr(obj, 'academic_year_id') and getattr(obj, 'academic_year_id', None) is None:
                    if yid:
                        obj.academic_year_id = yid

            obj_school_id = getattr(obj, 'school_id', None)
            if sid and sid > 0 and not super_admin and obj_school_id != sid:
                raise PermissionError(
                    f'{obj.__class__.__name__} cannot be written outside current school'
                )

            if (getattr(obj, '__year_scoped__', False)
                    or obj.__class__.__name__ == 'Notification'):
                _validate_relationship_scope(session_, obj)


def register_tenant_guards(app):
    """Register request, query, and write guards once per process."""
    global _REGISTERED

    app.before_request(_set_request_scope)

    if _REGISTERED:
        return

    @event.listens_for(Session, 'do_orm_execute')
    def _add_tenant_criteria(execute_state):
        if not execute_state.is_select:
            return
        if execute_state.execution_options.get('bypass_tenant_scope'):
            return

        sid = current_school_id()
        # Use the VIEW year for reads — may be a historical year selected by user.
        # Write guards in _before_flush always use current_academic_year_id() instead.
        yid = current_view_year_id()

        options = []
        if sid is not None:
            school_scoped, year_scoped = _models()
            for model in school_scoped:
                def school_filter(cls):
                    return cls.school_id == sid

                options.append(
                    with_loader_criteria(
                        model,
                        school_filter,
                        include_aliases=True,
                    )
                )
            if yid is not None and not execute_state.execution_options.get('include_all_years'):
                for model in year_scoped:
                    def year_filter(cls):
                        return cls.academic_year_id == yid

                    options.append(
                        with_loader_criteria(
                            model,
                            year_filter,
                            include_aliases=True,
                        )
                    )

        if options:
            execute_state.statement = execute_state.statement.options(*options)

    event.listen(Session, 'before_flush', _before_flush)
    _REGISTERED = True

"""
Mecha-School — School Calendar Blueprint
=========================================
Admin UI for managing:
  • Weekly days off per school, separately for students and employees
    (effective-dated SchoolWeeklyOffSchedule rows; schools with no rows keep
    using the legacy shared School.weekly_off_days for both audiences)
  • Named holiday / break date ranges (SchoolHoliday model), each applying to
    students, employees, or both (SchoolHoliday.applies_to)

Routes
------
GET  /school-calendar/                    — list holidays + weekly schedules
POST /school-calendar/weekly              — save one audience's weekly days off
POST /school-calendar/weekly/<id>/delete  — delete a FUTURE weekly schedule row
POST /school-calendar/add                 — create a holiday
GET  /school-calendar/<id>/edit           — redirects to the list (edit is a modal)
POST /school-calendar/<id>/edit           — update a holiday
POST /school-calendar/<id>/toggle         — activate / deactivate
POST /school-calendar/<id>/delete         — delete
"""
from datetime import date as date_type, datetime

from flask import (Blueprint, flash, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from app.models import (db, SchoolHoliday, SchoolWeeklyOffSchedule,
                        AcademicYear)
from app.utils.attendance_helpers import (
    get_local_date, parse_weekly_off_days, serialize_weekly_off_days,
)
from app.utils.audit import log_action
from app.utils.decorators import (permission_required,
                                   get_current_school, get_active_year)

school_calendar_bp = Blueprint(
    'school_calendar', __name__,
    template_folder='../../templates/school_calendar',
)

# Weekday labels (Python weekday(): 0=Mon … 6=Sun)
WEEKDAY_LABELS = {
    0: 'الإثنين',
    1: 'الثلاثاء',
    2: 'الأربعاء',
    3: 'الخميس',
    4: 'الجمعة',
    5: 'السبت',
    6: 'الأحد',
}

HOLIDAY_TYPE_LABELS = {
    'official':  'رسمية',
    'summer':    'صيفية',
    'emergency': 'طارئة',
    'custom':    'مخصصة',
}

APPLIES_TO_LABELS = {
    'both':      'الطلاب والموظفون',
    'students':  'الطلاب فقط',
    'employees': 'الموظفون فقط',
}

WEEKLY_AUDIENCE_LABELS = {
    'students':  'أيام العطلة الأسبوعية للطلاب',
    'employees': 'أيام العطلة الأسبوعية للموظفين',
}

# Fixed effective date for the weekly schedules saved from this page (both
# audiences).  Enforced server-side; the form does not send a date.
WEEKLY_EFFECTIVE_FROM = date_type(2026, 9, 28)

WEEKLY_NO_DAY_MSG = 'يجب اختيار يوم عطلة أسبوعية واحد على الأقل.'


def _parse_date(field_name):
    raw = (request.form.get(field_name) or '').strip()
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        return None


def _school_or_abort():
    school = get_current_school()
    if not school:
        flash('يرجى اختيار مدرسة نشطة أولاً.', 'danger')
        return None
    return school


def _can_manage(holiday, school):
    """
    Authorization for managing a single holiday record.

    - A global holiday (school_id IS NULL) may be managed only by a super admin.
      School managers must not modify, toggle, or delete global holidays because
      that would affect every school.
    - A school-specific holiday may be managed only by its owning school.

    This preserves the existing per-school isolation and closes the path that
    previously let a school manager edit a global holiday.
    """
    if holiday.school_id is None:
        return bool(current_user.is_super_admin)
    return holiday.school_id == school.id


# ── Started-holiday protection ───────────────────────────────────────────────
# Once a holiday has started (start_date <= the school's local today) its
# definition is frozen in the normal UI: it may not be deleted, and only
# name / notes may be edited.  Changing dates, type, audience, academic year
# or ownership would silently reinterpret past attendance, employee reports
# and draft payroll.  Activating / deactivating stays allowed (audited) so an
# accidentally created holiday can be stopped.

STARTED_SCOPE_MSG = ('لا يمكن تغيير نطاق عطلة بدأت أو انتهت؛ لأن ذلك قد يؤثر في '
                     'تقارير الحضور ومسودات الرواتب السابقة.')
STARTED_EDIT_MSG = ('لا يمكن تغيير تواريخ أو نوع أو نطاق عطلة بدأت أو انتهت. '
                    'يمكن تعديل الاسم والملاحظات فقط.')
STARTED_DELETE_MSG = ('لا يمكن حذف عطلة بدأت أو انتهت؛ حفاظًا على صحة سجلات '
                      'الحضور وتقارير الرواتب السابقة.')


def _has_started(holiday, school):
    """True when the holiday's stored start_date is on/before school-local today."""
    return holiday.start_date <= get_local_date(school)


def _started_protected_changes(holiday):
    """
    Names of protected fields that a submitted edit would change on a STARTED
    holiday.  A field absent from the form (e.g. a disabled control) means
    "keep"; a present field must equal the stored value.
    """
    form = request.form
    changed = []
    for field in ('start_date', 'end_date'):
        if field in form and _parse_date(field) != getattr(holiday, field):
            changed.append(field)
    if 'holiday_type' in form and (form.get('holiday_type') or '').strip() != holiday.holiday_type:
        changed.append('holiday_type')
    if 'applies_to' in form:
        raw = (form.get('applies_to') or '').strip()
        if raw and raw != holiday.applies_to:          # '' = keep current
            changed.append('applies_to')
    if 'academic_year_id' in form:
        raw = (form.get('academic_year_id') or '').strip()
        try:
            submitted = int(raw) if raw else None
        except ValueError:
            submitted = object()                        # malformed → a change
        if submitted != holiday.academic_year_id:
            changed.append('academic_year_id')
    if 'is_global' in form:
        want_global = bool(form.get('is_global')) and current_user.is_super_admin
        if want_global != (holiday.school_id is None):
            changed.append('ownership')
    return changed


def _holidays_for_school(school_id):
    """
    Return holidays for this school + global holidays, newest first.
    bypass_tenant_scope because SchoolHoliday is not __school_scoped__.
    """
    return (
        SchoolHoliday.query
        .execution_options(bypass_tenant_scope=True)
        .filter(
            db.or_(
                SchoolHoliday.school_id == school_id,
                SchoolHoliday.school_id.is_(None),
            )
        )
        .order_by(SchoolHoliday.start_date.desc())
        .all()
    )


def _school_years(school_id):
    return (
        AcademicYear.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(school_id=school_id)
        .order_by(AcademicYear.start_date.desc())
        .all()
    )


def _parse_year_id(school_id):
    """
    Read academic_year_id from the form.
    Returns (ok, year_id): an empty value is (True, None); a value that is not
    an academic year of THIS school is rejected (False, None) so a forged id can
    never link a holiday to another school's year.
    """
    raw = (request.form.get('academic_year_id') or '').strip()
    if not raw:
        return True, None
    try:
        year_id = int(raw)
    except ValueError:
        return False, None
    exists = (
        AcademicYear.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(id=year_id, school_id=school_id)
        .first()
    )
    return (True, year_id) if exists else (False, None)


def _parse_applies_to(current=None):
    """
    Read the holiday audience from the form.
    Returns (ok, value):
      • field absent / blank → keep `current` (edit) or 'both' (create), so an
        old form or client that does not send the field keeps today's behaviour
      • one of SchoolHoliday.APPLIES_TO_CHOICES → that value
      • anything else → (False, None): the request is rejected, nothing written
    """
    raw = (request.form.get('applies_to') or '').strip()
    if not raw:
        return True, (current or 'both')
    if raw not in SchoolHoliday.APPLIES_TO_CHOICES:
        return False, None
    return True, raw


def _weekly_rows(school_id, audience):
    return (
        SchoolWeeklyOffSchedule.query
        .execution_options(bypass_tenant_scope=True)
        .filter(SchoolWeeklyOffSchedule.school_id == school_id,
                SchoolWeeklyOffSchedule.audience == audience)
        .order_by(SchoolWeeklyOffSchedule.effective_from.asc())
        .all()
    )


def _weekly_context(school, today):
    """Per-audience view model for the two weekly-days-off cards."""
    legacy = sorted(d for d in parse_weekly_off_days(school.weekly_off_days)
                    if d in WEEKDAY_LABELS)
    ctx = {}
    for audience in SchoolWeeklyOffSchedule.AUDIENCES:
        rows = _weekly_rows(school.id, audience)
        in_force = None
        for row in rows:
            if row.effective_from <= today:
                in_force = row
        if in_force is not None:
            current = sorted(d for d in parse_weekly_off_days(in_force.off_days)
                             if d in WEEKDAY_LABELS)
        else:
            current = legacy
        history = [
            {
                'row': row,
                'days': sorted(d for d in parse_weekly_off_days(row.off_days)
                               if d in WEEKDAY_LABELS),
                'is_future': row.effective_from > today,
                'is_in_force': in_force is not None and row.id == in_force.id,
            }
            for row in reversed(rows)
        ]
        ctx[audience] = {
            'label': WEEKLY_AUDIENCE_LABELS[audience],
            'inherited': in_force is None,
            'current': current,
            'history': history,
        }
    ctx['legacy'] = legacy
    return ctx


# ─────────────────────────────────────────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────────────────────────────────────────

@school_calendar_bp.route('/')
@login_required
@permission_required('manage_calendar')
def index():
    school = _school_or_abort()
    if not school:
        return redirect(url_for('admin.dashboard'))

    year = get_active_year(school.id)
    all_years = _school_years(school.id)
    holidays = _holidays_for_school(school.id)
    local_today = get_local_date(school)

    return render_template(
        'school_calendar/index.html',
        school=school,
        year=year,
        all_years=all_years,
        holidays=holidays,
        weekly=_weekly_context(school, local_today),
        weekly_audiences=SchoolWeeklyOffSchedule.AUDIENCES,
        weekday_labels=WEEKDAY_LABELS,
        holiday_type_labels=HOLIDAY_TYPE_LABELS,
        applies_to_labels=APPLIES_TO_LABELS,
        today=date_type.today(),
        local_today=local_today,
    )


@school_calendar_bp.route('/weekly', methods=['POST'])
@login_required
@permission_required('manage_calendar')
def save_weekly():
    """
    Save the weekly days off of ONE audience, effective from the fixed date
    WEEKLY_EFFECTIVE_FROM (2026-09-28).

    • The effective date is decided HERE only: any effective_from submitted by
      the client is ignored.  The row for that date is created or updated;
      other schedule rows (earlier or later) are never deleted or changed.
    • The legacy School.weekly_off_days column is never written here; it stays
      the fallback for dates before the audience's first schedule row.
    • A submission without the weekly_form marker or a valid audience (e.g. an
      old cached form) changes nothing.
    • At least one weekday is required.  Existing empty ('') rows keep working
      for the dates they cover, but this page never creates or saves one.
    """
    school = _school_or_abort()
    if not school:
        return redirect(url_for('admin.dashboard'))

    audience = (request.form.get('audience') or '').strip()
    if (request.form.get('weekly_form') != '1'
            or audience not in SchoolWeeklyOffSchedule.AUDIENCES):
        flash('لم يُحفظ أي تغيير: نموذج أيام العطلة الأسبوعية غير صالح. '
              'يرجى تحديث الصفحة والمحاولة مرة أخرى.', 'danger')
        return redirect(url_for('school_calendar.index'))

    # Only day_0 … day_6 are read — any other key or value is ignored.
    days = [d for d in range(7) if request.form.get(f'day_{d}')]
    if not days:
        flash(WEEKLY_NO_DAY_MSG, 'danger')
        return redirect(url_for('school_calendar.index'))

    effective_from = WEEKLY_EFFECTIVE_FROM      # fixed; client value ignored

    off_days = serialize_weekly_off_days(days)
    label = WEEKLY_AUDIENCE_LABELS[audience]

    rows = _weekly_rows(school.id, audience)
    same_date = next((r for r in rows if r.effective_from == effective_from), None)

    if same_date is not None:
        if same_date.off_days == off_days:
            flash('لا يوجد تغيير في الإعداد.', 'info')
            return redirect(url_for('school_calendar.index'))
        same_date.off_days = off_days
        action = 'edit'
        record = same_date
    else:
        legacy = parse_weekly_off_days(school.weekly_off_days)
        in_force = legacy
        for r in rows:
            if r.effective_from <= effective_from:
                in_force = parse_weekly_off_days(r.off_days)
        # A row equal to what is already in force on effective_from would change
        # nothing (a later row, if any, still takes over on its own date).
        if in_force == frozenset(days):
            flash('لا يوجد تغيير في الإعداد.', 'info')
            return redirect(url_for('school_calendar.index'))
        record = SchoolWeeklyOffSchedule(
            school_id      = school.id,
            audience       = audience,
            off_days       = off_days,
            effective_from = effective_from,
            created_by     = current_user.id,
        )
        db.session.add(record)
        action = 'create'

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash('تعذّر الحفظ بسبب تعديل متزامن. يرجى المحاولة مرة أخرى.', 'danger')
        return redirect(url_for('school_calendar.index'))

    log_action(action, 'weekly_off_schedule', record.id,
               details=(f'audience={audience} off_days="{off_days}" '
                        f'effective_from={effective_from.isoformat()}'))
    flash(f'تم حفظ {label} اعتباراً من {effective_from.isoformat()}.', 'success')
    return redirect(url_for('school_calendar.index'))


@school_calendar_bp.route('/weekly/<int:schedule_id>/delete', methods=['POST'])
@login_required
@permission_required('manage_calendar')
def delete_weekly(schedule_id):
    """Delete a weekly schedule row that has NOT taken effect yet (future only)."""
    school = _school_or_abort()
    if not school:
        return redirect(url_for('admin.dashboard'))

    row = (
        SchoolWeeklyOffSchedule.query
        .execution_options(bypass_tenant_scope=True)
        .filter(SchoolWeeklyOffSchedule.id == schedule_id,
                SchoolWeeklyOffSchedule.school_id == school.id)
        .first()
    )
    if row is None:
        flash('الإعداد المطلوب غير موجود.', 'danger')
        return redirect(url_for('school_calendar.index'))

    if row.effective_from <= get_local_date(school):
        flash('لا يمكن حذف إعداد سارٍ أو سابق — احفظ إعداداً جديداً بتاريخ سريان '
              'اليوم أو لاحقاً بدلاً من ذلك.', 'danger')
        return redirect(url_for('school_calendar.index'))

    details = (f'audience={row.audience} off_days="{row.off_days}" '
               f'effective_from={row.effective_from.isoformat()}')
    db.session.delete(row)
    db.session.commit()
    log_action('delete', 'weekly_off_schedule', schedule_id, details=details)
    flash('تم حذف الإعداد المستقبلي.', 'success')
    return redirect(url_for('school_calendar.index'))


@school_calendar_bp.route('/add', methods=['POST'])
@login_required
@permission_required('manage_calendar')
def add():
    school = _school_or_abort()
    if not school:
        return redirect(url_for('admin.dashboard'))

    name         = (request.form.get('name') or '').strip()
    start_date   = _parse_date('start_date')
    end_date     = _parse_date('end_date')
    holiday_type = (request.form.get('holiday_type') or 'official').strip()
    notes        = (request.form.get('notes') or '').strip() or None
    # Only a super admin may create a global holiday. For a school manager the
    # 'is_global' field is not rendered; any forged value is ignored here so the
    # holiday is always created school-specific.
    is_global    = bool(request.form.get('is_global')) and current_user.is_super_admin

    if not name:
        flash('اسم العطلة مطلوب.', 'danger')
        return redirect(url_for('school_calendar.index'))
    if not start_date or not end_date:
        flash('تاريخ البداية والنهاية مطلوبان.', 'danger')
        return redirect(url_for('school_calendar.index'))
    if end_date < start_date:
        flash('تاريخ النهاية يجب أن يكون بعد أو يساوي تاريخ البداية.', 'danger')
        return redirect(url_for('school_calendar.index'))
    if holiday_type not in SchoolHoliday.HOLIDAY_TYPES:
        holiday_type = 'official'

    ok, applies_to = _parse_applies_to()
    if not ok:
        flash('قيمة "تشمل العطلة" غير صالحة.', 'danger')
        return redirect(url_for('school_calendar.index'))

    ok, year_id = _parse_year_id(school.id)
    if not ok:
        flash('العام الدراسي المحدد غير صالح لهذه المدرسة.', 'danger')
        return redirect(url_for('school_calendar.index'))

    holiday = SchoolHoliday(
        school_id        = None if is_global else school.id,
        academic_year_id = year_id,
        name             = name,
        start_date       = start_date,
        end_date         = end_date,
        holiday_type     = holiday_type,
        applies_to       = applies_to,
        notes            = notes,
        is_active        = True,
        created_by       = current_user.id,
    )
    db.session.add(holiday)
    db.session.commit()
    log_action('create', 'school_holiday', holiday.id,
               details=(f'{start_date.isoformat()}..{end_date.isoformat()} '
                        f'applies_to={applies_to} '
                        f'scope={"global" if is_global else "school"}'))
    flash(f'تمت إضافة العطلة "{name}".', 'success')
    return redirect(url_for('school_calendar.index'))


@school_calendar_bp.route('/<int:holiday_id>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('manage_calendar')
def edit(holiday_id):
    school = _school_or_abort()
    if not school:
        return redirect(url_for('admin.dashboard'))

    # Editing happens in the modal on the list page; there is no standalone
    # edit page (the old GET rendered a template that does not exist).
    if request.method != 'POST':
        return redirect(url_for('school_calendar.index'))

    holiday = (
        SchoolHoliday.query
        .execution_options(bypass_tenant_scope=True)
        .get_or_404(holiday_id)
    )

    # Only the owning school may edit a school-specific holiday; global holidays
    # may be edited only by a super admin (school managers are blocked).
    if not _can_manage(holiday, school):
        flash('لا يمكنك تعديل هذه العطلة.', 'danger')
        return redirect(url_for('school_calendar.index'))

    # ── Started holiday: only name and notes are editable ──────────────────
    # Any submitted change to a protected field rejects the WHOLE request
    # before anything is assigned (no partial name/notes save, no audit entry).
    if _has_started(holiday, school):
        changed = _started_protected_changes(holiday)
        if changed:
            flash(STARTED_SCOPE_MSG if changed == ['applies_to'] else STARTED_EDIT_MSG,
                  'danger')
            return redirect(url_for('school_calendar.index'))
        name = (request.form.get('name') or '').strip()
        if not name:
            flash('اسم العطلة مطلوب.', 'danger')
            return redirect(url_for('school_calendar.index'))
        holiday.name  = name
        holiday.notes = (request.form.get('notes') or '').strip() or None
        db.session.commit()
        log_action('edit', 'school_holiday', holiday.id,
                   details='name/notes only (started holiday)')
        flash(f'تم تعديل العطلة "{name}".', 'success')
        return redirect(url_for('school_calendar.index'))

    name         = (request.form.get('name') or '').strip()
    start_date   = _parse_date('start_date')
    end_date     = _parse_date('end_date')
    holiday_type = (request.form.get('holiday_type') or 'official').strip()
    notes        = (request.form.get('notes') or '').strip() or None
    # Only a super admin may set/keep a holiday global. A forged 'is_global'
    # from a school manager is ignored, keeping the holiday school-specific.
    is_global    = bool(request.form.get('is_global')) and current_user.is_super_admin

    if not name or not start_date or not end_date:
        flash('اسم العطلة وتاريخا البداية والنهاية مطلوبة.', 'danger')
        return redirect(url_for('school_calendar.index'))
    if end_date < start_date:
        flash('تاريخ النهاية يجب أن يكون بعد أو يساوي تاريخ البداية.', 'danger')
        return redirect(url_for('school_calendar.index'))
    if holiday_type not in SchoolHoliday.HOLIDAY_TYPES:
        holiday_type = 'official'

    # An absent field keeps the stored audience — never silently reset to 'both'.
    ok, applies_to = _parse_applies_to(current=holiday.applies_to)
    if not ok:
        flash('قيمة "تشمل العطلة" غير صالحة.', 'danger')
        return redirect(url_for('school_calendar.index'))

    ok, year_id = _parse_year_id(school.id)
    if not ok:
        flash('العام الدراسي المحدد غير صالح لهذه المدرسة.', 'danger')
        return redirect(url_for('school_calendar.index'))

    # Once a holiday has started, its audience is immutable: re-scoping it
    # would silently change past employee working days (reports, draft payroll)
    # and past student days off.  Checked against the stored AND the submitted
    # start date (school-local today) so moving a future holiday into the past
    # in the same request cannot bypass the rule.  The whole request is
    # rejected before any field is assigned — nothing is written or logged.
    if applies_to != holiday.applies_to:
        local_today = get_local_date(school)
        if holiday.start_date <= local_today or start_date <= local_today:
            flash(STARTED_SCOPE_MSG, 'danger')
            return redirect(url_for('school_calendar.index'))

    old_applies_to = holiday.applies_to
    holiday.name             = name
    holiday.start_date       = start_date
    holiday.end_date         = end_date
    holiday.holiday_type     = holiday_type
    holiday.applies_to       = applies_to
    holiday.notes            = notes
    holiday.school_id        = None if is_global else school.id
    holiday.academic_year_id = year_id
    db.session.commit()
    log_action('edit', 'school_holiday', holiday.id,
               details=(f'{start_date.isoformat()}..{end_date.isoformat()} '
                        f'applies_to={old_applies_to}->{applies_to} '
                        f'scope={"global" if is_global else "school"}'))
    flash(f'تم تعديل العطلة "{name}".', 'success')
    return redirect(url_for('school_calendar.index'))


@school_calendar_bp.route('/<int:holiday_id>/toggle', methods=['POST'])
@login_required
@permission_required('manage_calendar')
def toggle(holiday_id):
    school = _school_or_abort()
    if not school:
        return redirect(url_for('admin.dashboard'))

    holiday = (
        SchoolHoliday.query
        .execution_options(bypass_tenant_scope=True)
        .get_or_404(holiday_id)
    )
    if not _can_manage(holiday, school):
        flash('لا يمكنك تعديل هذه العطلة.', 'danger')
        return redirect(url_for('school_calendar.index'))
    # Allowed for started holidays too, so an accidentally created holiday can
    # be stopped; its dates, type, audience, year and ownership stay locked.
    started = _has_started(holiday, school)
    holiday.is_active = not holiday.is_active
    db.session.commit()
    log_action('edit', 'school_holiday', holiday.id,
               details=f'is_active={holiday.is_active} started={started}')
    state = 'مفعّلة' if holiday.is_active else 'معطّلة'
    flash(f'العطلة "{holiday.name}" أصبحت {state}.', 'success')
    return redirect(url_for('school_calendar.index'))


@school_calendar_bp.route('/<int:holiday_id>/delete', methods=['POST'])
@login_required
@permission_required('manage_calendar')
def delete(holiday_id):
    school = _school_or_abort()
    if not school:
        return redirect(url_for('admin.dashboard'))

    holiday = (
        SchoolHoliday.query
        .execution_options(bypass_tenant_scope=True)
        .get_or_404(holiday_id)
    )
    if not _can_manage(holiday, school):
        flash('لا يمكنك حذف هذه العطلة.', 'danger')
        return redirect(url_for('school_calendar.index'))
    if _has_started(holiday, school):
        flash(STARTED_DELETE_MSG, 'danger')
        return redirect(url_for('school_calendar.index'))

    name = holiday.name
    details = (f'{holiday.start_date.isoformat()}..{holiday.end_date.isoformat()} '
               f'applies_to={holiday.applies_to}')
    db.session.delete(holiday)
    db.session.commit()
    log_action('delete', 'school_holiday', holiday_id, details=details)
    flash(f'تم حذف العطلة "{name}".', 'success')
    return redirect(url_for('school_calendar.index'))

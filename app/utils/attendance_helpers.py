"""
Attendance utility helpers — time-based status determination and timezone support.
"""
import pytz
from datetime import datetime
from typing import NamedTuple, Optional


def _get_tz(settings=None):
    """Return the pytz timezone object from school settings (fallback: Asia/Baghdad)."""
    tz_name = None
    if settings and settings.timezone:
        tz_name = settings.timezone
    try:
        return pytz.timezone(tz_name or 'Asia/Baghdad')
    except pytz.exceptions.UnknownTimeZoneError:
        return pytz.timezone('Asia/Baghdad')


def get_local_now(settings=None):
    """
    Return the current datetime in the school's configured timezone, as a
    timezone-naive value (suitable for storing in plain DB Time/DateTime columns).
    """
    if settings is None:
        from app.models import SchoolSettings
        settings = SchoolSettings.get()
    return datetime.now(_get_tz(settings)).replace(tzinfo=None)


def get_local_date(settings=None):
    """Return today's date in the school's configured timezone."""
    return get_local_now(settings).date()


def utc_to_local(utc_dt, settings=None):
    """
    Convert a UTC (or timezone-aware) datetime to the school's local timezone.
    Returns a timezone-naive datetime representing the local wall-clock time.
    """
    if settings is None:
        from app.models import SchoolSettings
        settings = SchoolSettings.get()
    tz = _get_tz(settings)
    if utc_dt.tzinfo is None:
        utc_dt = pytz.utc.localize(utc_dt)
    return utc_dt.astimezone(tz).replace(tzinfo=None)


# ─────────────────────────────────────────────────────────────────────────────
#  Effective attendance settings — ONE calculation path for both audiences
#
#  Students and employees store their timings in separate School columns and
#  resolve their shift differently (section→grade vs Employee.shift_id), but
#  the PRECEDENCE RULES are identical, so they live here once:
#
#    late_threshold  = shift.late_after_time ?? school.<audience late>
#    departure_time  = shift.dismissal_time  ?? school.<audience departure>
#    absence_cutoff  = school.<audience shift cutoff>  when shift mode is ON
#                      school.<audience absence threshold>  when it is OFF
#
#  The absence cutoff NEVER cross-falls-back: not between the two modes of one
#  audience, and not between audiences.  NULL stays NULL and the caller fails
#  closed.  This reproduces the existing student rule exactly (see
#  School.shift_absent_after_time) and applies the same discipline to employees.
#
#  This function performs NO database access.  The caller supplies the
#  already-resolved shift (get_student_shift / get_employee_shift), so reports
#  and payroll can bulk-preload shifts and still share this logic.
# ─────────────────────────────────────────────────────────────────────────────

# audience → (late threshold, absence threshold, departure, shift toggle,
#             shift-mode absence cutoff) column names on School/SchoolSettings.
_AUDIENCE_SETTINGS_FIELDS = {
    'students': (
        'att_late_threshold', 'att_absence_threshold', 'att_departure_time',
        'enable_attendance_shifts', 'shift_absent_after_time', 'att_start_time',
    ),
    'employees': (
        'emp_att_late_threshold', 'emp_att_absence_threshold',
        'emp_att_departure_time', 'emp_enable_attendance_shifts',
        'emp_shift_absent_after_time', 'emp_att_start_time',
    ),
}


class EffectiveAttendanceSettings(NamedTuple):
    """Resolved timings for one audience (and optionally one shift)."""
    audience:       str
    shift_enabled:  bool
    late_threshold: Optional[object]   # datetime.time | None
    absence_cutoff: Optional[object]   # datetime.time | None
    departure_time: Optional[object]   # datetime.time | None
    attendance_start: Optional[object] # datetime.time | None


def _audience_fields(audience):
    try:
        return _AUDIENCE_SETTINGS_FIELDS[audience]
    except KeyError:
        raise ValueError(f'invalid attendance audience: {audience!r}') from None


def get_effective_attendance_settings(school, audience, shift=None):
    """
    Effective attendance timings for `audience` ('students' | 'employees').

    PURE — no queries, no writes.  `shift` is the already-resolved shift object
    for the person (or None); anything exposing `late_after_time` /
    `dismissal_time` works, which is why AttendanceShift and
    EmployeeAttendanceShift use those same attribute names.

    `shift_enabled` reflects the SCHOOL TOGGLE only, not whether a shift was
    passed — it is what selects which absence-cutoff column applies.
    """
    late_f, absence_f, departure_f, toggle_f, shift_cutoff_f, start_f = _audience_fields(audience)

    shift_enabled = bool(getattr(school, toggle_f, False)) if school else False

    late = getattr(shift, 'late_after_time', None) if shift is not None else None
    if late is None and school:
        late = getattr(school, late_f, None)

    departure = getattr(shift, 'dismissal_time', None) if shift is not None else None
    if departure is None and school:
        departure = getattr(school, departure_f, None)

    attendance_start = getattr(shift, 'start_time', None) if shift is not None else None
    if attendance_start is None and school:
        attendance_start = getattr(school, start_f, None)

    # No cross-fallback between the two modes: an unset cutoff stays unset.
    cutoff = None
    if school:
        cutoff = getattr(school, shift_cutoff_f if shift_enabled else absence_f, None)

    return EffectiveAttendanceSettings(
        audience       = audience,
        shift_enabled  = shift_enabled,
        late_threshold = late,
        absence_cutoff = cutoff,
        departure_time = departure,
        attendance_start = attendance_start,
    )


def effective_shift_late_threshold(school, audience, shift):
    """The late boundary attendance logic actually applies to `shift`.

    Thin wrapper over get_effective_attendance_settings so validation can never
    drift from runtime: shift.late_after_time when configured, otherwise the
    audience's school-level threshold. Returns None when lateness is switched
    off for this audience/shift (both sources NULL) — there is then no late
    boundary at all.

    `shift` only needs a ``late_after_time`` attribute, so a not-yet-saved form
    submission can be validated with a lightweight stand-in object.
    """
    return get_effective_attendance_settings(school, audience,
                                             shift=shift).late_threshold


def shift_absence_boundary(school, audience, shift):
    """The earliest time at which `shift` may legitimately be called absent-ready.

        effective late threshold  (shift.late_after_time ?? school-level)
        ELSE shift.start_time     (when lateness is disabled on BOTH sources)

    The start_time fallback matters because a shift with no late threshold at all
    still has a start: without it, a 14:00 shift with lateness disabled would
    accept an 08:00 absence cutoff, declaring those people absent six hours
    before their day begins.

    dismissal_time is never consulted.  Returns None only when the shift exposes
    neither boundary.
    """
    late = effective_shift_late_threshold(school, audience, shift)
    if late is not None:
        return late
    return getattr(shift, 'start_time', None)


def conflicting_shift_late_thresholds(school, audience, shifts, cutoff):
    """Shifts that make `cutoff` an invalid global absence cutoff.

    RULE: the single school-level absence cutoff must be STRICTLY AFTER the
    absence boundary of every ACTIVE shift, so nobody in a later shift can be
    called absent while still inside their valid arrival window.
    Equality is a conflict.

    The boundary is the shift's effective LATE threshold, falling back to its
    start_time when lateness is disabled on both the shift and the school (see
    shift_absence_boundary) — never dismissal_time: the cutoff decides when a
    missing record may become an absence, not when the day ends.

    Returns [(shift, boundary)] for the offending shifts, ordered by boundary.
    Empty list = the cutoff is valid.
      * cutoff None (unconfigured / being cleared) → no conflict, fail closed
        is handled by the consumers.
      * a shift exposing neither a late threshold nor a start_time cannot
        conflict, since there is nothing to order the cutoff against.

    PURE: no queries, no writes. The caller supplies the active shifts.
    """
    if cutoff is None:
        return []
    conflicts = []
    for shift in shifts or ():
        boundary = shift_absence_boundary(school, audience, shift)
        if boundary is not None and boundary >= cutoff:
            conflicts.append((shift, boundary))
    return sorted(conflicts, key=lambda pair: pair[1])


def determine_check_in_status(check_in_time, settings, shift=None, audience='students'):
    """
    Return 'present' or 'late' based on check_in_time vs time thresholds.

    If `shift` is provided, its late_after_time is used.  Otherwise falls back
    to the school-level late threshold for `audience` (students →
    settings.att_late_threshold, the existing behaviour).
    Passing shift=None is fully backwards-compatible with all existing callers,
    and `audience` defaults to 'students' so no existing call site changes.

    INSTITUTE ONLY — optional student lateness
    ──────────────────────────────────────────
    ``School.att_late_threshold`` is nullable, so leaving it empty already
    disables lateness everywhere it is the source (unified mode, and shiftless
    students in shift mode): the check below returns 'present'.

    For an institute running shifts there are two independent off switches, and
    either one alone disables lateness:

      * GLOBAL — the institute has no school-level ``att_late_threshold``.
      * PER SHIFT — this shift's own ``late_after_time`` was left blank or
        cleared (it is nullable for institutes; school forms still require it).

    Neither ever falls back to the other, so a cleared shift cutoff cannot be
    silently revived by a school-level time.  When a cutoff IS configured the
    existing calculation and the existing shift-first priority run unchanged.

    EMPLOYEE attendance passes audience='employees', which swaps the
    school-level source to ``emp_att_late_threshold`` and lets an
    EmployeeAttendanceShift supply ``late_after_time``.  The two audiences never
    read each other's columns.  Nothing here writes.
    """
    late_field = _audience_fields(audience)[0]
    school_threshold = getattr(settings, late_field, None) if settings else None

    if shift is not None and getattr(settings, 'is_institute', False):
        if school_threshold is None:
            return 'present'
        if getattr(shift, 'late_after_time', None) is None:
            return 'present'

    threshold = None
    if shift is not None:
        threshold = getattr(shift, 'late_after_time', None)
    if threshold is None:
        threshold = school_threshold
    if threshold and check_in_time >= threshold:
        return 'late'
    return 'present'


def get_student_shift(student, school):
    """
    Return the AttendanceShift for `student` when shifts are enabled, else None.

    Priority:
      1. section.shift_id  — explicit section-level override
      2. section.grade.shift_id — grade-level fallback (for schools without
         per-section assignments, or sections that inherit the grade shift)
      3. None — student will check in without shift-specific thresholds

    Returns None when:
      - school.enable_attendance_shifts is False/absent
      - the resolved shift is inactive
    """
    if not school or not getattr(school, 'enable_attendance_shifts', False):
        return None
    section_id = getattr(student, 'section_id', None)
    if not section_id:
        return None
    from app.models import Section, Grade, AttendanceShift
    section = (Section.query
               .execution_options(bypass_tenant_scope=True)
               .get(section_id))
    if not section:
        return None
    # 1. Section-level shift
    shift_id = section.shift_id
    # 2. Grade-level fallback
    if not shift_id:
        grade = (Grade.query
                 .execution_options(bypass_tenant_scope=True)
                 .get(section.grade_id))
        if grade:
            shift_id = grade.shift_id
    if not shift_id:
        return None
    shift = (AttendanceShift.query
             .execution_options(bypass_tenant_scope=True)
             .get(shift_id))
    if shift and not shift.is_active:
        return None
    return shift


def get_employee_shift(employee, school):
    """
    Return the EmployeeAttendanceShift for `employee`, else None.

    Deliberately NOT merged with get_student_shift: a student's shift comes
    from section→grade, an employee's from a direct Employee.shift_id. Both
    feed the same get_effective_attendance_settings / determine_check_in_status.

    Returns None when:
      - school.emp_enable_attendance_shifts is False/absent
      - employee.shift_id is NULL
      - the shift no longer exists or is inactive
      - the shift does not belong to the employee's school AND the resolving
        school (cross-school rows can never influence a status, even if one was
        somehow stored)
    """
    if not school or not getattr(school, 'emp_enable_attendance_shifts', False):
        return None
    shift_id = getattr(employee, 'shift_id', None)
    if not shift_id:
        return None
    from app.models import EmployeeAttendanceShift
    shift = (EmployeeAttendanceShift.query
             .execution_options(bypass_tenant_scope=True)
             .get(shift_id))
    if not shift or not shift.is_active:
        return None
    if shift.school_id != getattr(employee, 'school_id', None):
        return None
    if shift.school_id != getattr(school, 'id', None):
        return None
    return shift


def get_employee_shift_map(school, employees):
    """{employee_id: EmployeeAttendanceShift} for a SET of employees — ONE query.

    The bulk form of get_employee_shift, for the manual daily sheet and payroll
    generation, so neither performs a shift query per employee.  Applies exactly
    the same rules: feature toggle off, no assignment, inactive shift or a shift
    from another school all yield no entry (the caller then falls back to the
    school-level employee settings).
    """
    if not school or not getattr(school, 'emp_enable_attendance_shifts', False):
        return {}
    wanted = {getattr(e, 'shift_id', None) for e in employees}
    wanted.discard(None)
    if not wanted:
        return {}

    from app.models import EmployeeAttendanceShift
    rows = (EmployeeAttendanceShift.query
            .execution_options(bypass_tenant_scope=True)
            .filter(EmployeeAttendanceShift.id.in_(wanted),
                    EmployeeAttendanceShift.school_id == school.id,
                    EmployeeAttendanceShift.is_active.is_(True))
            .all())
    by_id = {s.id: s for s in rows}
    return {
        e.id: by_id[e.shift_id]
        for e in employees
        if getattr(e, 'shift_id', None) in by_id
        and getattr(e, 'school_id', None) == school.id
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Holidays & weekly days off — audience aware (students / employees)
#
#  Two independent sources decide whether a date is "off" for an audience:
#    1. Weekly days off, resolved per audience from the effective-dated
#       SchoolWeeklyOffSchedule history, falling back to the legacy shared
#       School.weekly_off_days when the school has no row in force.
#    2. Active SchoolHoliday ranges (school-specific or global) whose
#       applies_to is 'both' or the requested audience.
#  The date is off when EITHER source says so.
#
#  audience=None is the LEGACY mode kept only for backward compatibility: it
#  uses the UNION of both audiences' weekly days and every holiday regardless
#  of applies_to.  For a school that never configured separate schedules or
#  scoped holidays this is exactly the pre-feature behaviour.  Production call
#  sites must pass audience explicitly (enforced by a test); a missed call site
#  can only err towards "day off" — never create wrong student absences /
#  parent notifications or wrong employee deductions.
# ─────────────────────────────────────────────────────────────────────────────

HOLIDAY_AUDIENCES = ('students', 'employees')


def _check_audience(audience):
    if audience is not None and audience not in HOLIDAY_AUDIENCES:
        raise ValueError(f'invalid holiday audience: {audience!r}')


def parse_weekly_off_days(raw):
    """
    Parse a stored weekly-off string ("4,5") into a frozenset of Python weekday
    numbers (Mon=0 … Sun=6).

    Mirrors the legacy parsing exactly: NULL/blank → empty set, whitespace
    tolerated, non-digit tokens skipped, duplicates collapse, out-of-range
    numbers kept (they simply never match a weekday), and a value that cannot
    be converted makes the whole setting count as "no weekly days off".
    """
    if not raw:
        return frozenset()
    try:
        return frozenset(
            int(d.strip())
            for d in str(raw).split(',')
            if d.strip().isdigit()
        )
    except (ValueError, AttributeError):
        return frozenset()


def serialize_weekly_off_days(days):
    """Canonical storage form: sorted, unique, ASCII; '' means no days off."""
    return ','.join(str(d) for d in sorted({int(d) for d in days}))


def load_weekly_off_schedule(school_id, audience):
    """
    Return [(effective_from, frozenset(days))] for one school + audience,
    oldest first.  Explicit school filter + bypass_tenant_scope so it is safe
    from background jobs and never reads another school's rows.
    """
    from app.models import SchoolWeeklyOffSchedule

    _check_audience(audience)
    if not school_id or audience is None:
        return []
    rows = (SchoolWeeklyOffSchedule.query
            .execution_options(bypass_tenant_scope=True)
            .filter(SchoolWeeklyOffSchedule.school_id == school_id,
                    SchoolWeeklyOffSchedule.audience == audience)
            .order_by(SchoolWeeklyOffSchedule.effective_from.asc())
            .all())
    return [(r.effective_from, parse_weekly_off_days(r.off_days)) for r in rows]


def _resolve_from_schedule(schedule, legacy, on_date):
    chosen = legacy
    for effective_from, days in schedule:
        if effective_from <= on_date:
            chosen = days
        else:
            break
    return chosen


def resolve_weekly_off_days(school, audience, on_date, *, school_id=None,
                            schedule=None):
    """
    Weekly days off (frozenset of Mon=0 weekdays) in force on on_date.

    audience 'students' / 'employees' → the row with the greatest
    effective_from <= on_date, else the legacy School.weekly_off_days.
    audience None → union of both audiences (legacy mode, see above).
    schedule → optional pre-loaded load_weekly_off_schedule() result for the
    same school + audience (range computations load it once).
    """
    _check_audience(audience)
    legacy = parse_weekly_off_days(getattr(school, 'weekly_off_days', None))
    sid = school_id or getattr(school, 'id', None)
    if audience is None:
        return (resolve_weekly_off_days(school, 'students', on_date, school_id=sid)
                | resolve_weekly_off_days(school, 'employees', on_date, school_id=sid))
    if schedule is None:
        schedule = load_weekly_off_schedule(sid, audience)
    return _resolve_from_schedule(schedule, legacy, on_date)


def _holiday_query(school_id, audience):
    """Active holidays for this school + global ones, filtered by audience."""
    from app.models import SchoolHoliday, db

    q = (SchoolHoliday.query
         .execution_options(bypass_tenant_scope=True)
         .filter(SchoolHoliday.is_active == True))
    if school_id:
        q = q.filter(
            db.or_(
                SchoolHoliday.school_id == school_id,
                SchoolHoliday.school_id.is_(None),
            )
        )
    else:
        q = q.filter(SchoolHoliday.school_id.is_(None))
    if audience is not None:
        q = q.filter(SchoolHoliday.applies_to.in_(('both', audience)))
    return q


def is_holiday_date(check_date, school_id, school=None, *, audience=None):
    """
    Return True if check_date is a day off for the given audience because:
      1. Its weekday is a weekly day off for that audience (effective-dated
         schedule, else the legacy School.weekly_off_days), or
      2. It is inside an active SchoolHoliday range for this school or a global
         holiday (school_id IS NULL) whose applies_to covers the audience.

    audience: 'students' (automatic student absence, student pages/reports),
              'employees' (employee working days, reports, payroll), or
              None (legacy: union of both — do not use in new code).

    Pass school= to avoid an extra DB hit when the School object is already loaded.
    Uses bypass_tenant_scope so this is safe to call from background tasks and
    from the AI Face WebSocket service (no request context).
    """
    from app.models import School, SchoolHoliday

    _check_audience(audience)

    if school is None and school_id:
        school = (School.query
                  .execution_options(bypass_tenant_scope=True)
                  .get(school_id))

    # ── weekly day-off check (independent of the holiday query) ───────────────
    weekly = resolve_weekly_off_days(school, audience, check_date,
                                     school_id=school_id)
    if check_date.weekday() in weekly:
        return True

    # ── named holiday check ───────────────────────────────────────────────────
    q = _holiday_query(school_id, audience).filter(
        SchoolHoliday.start_date <= check_date,
        SchoolHoliday.end_date   >= check_date,
    )
    return q.first() is not None


def get_off_dates(date_from, date_to, school, *, audience):
    """
    Set of dates in [date_from, date_to] that are off for the audience — the
    same rules as is_holiday_date(), evaluated with one schedule query per
    audience and one holiday query instead of one query per day.
    """
    from datetime import timedelta
    from app.models import SchoolHoliday

    _check_audience(audience)
    if date_to < date_from:
        return set()
    sid = getattr(school, 'id', None)
    legacy = parse_weekly_off_days(getattr(school, 'weekly_off_days', None))
    audiences = HOLIDAY_AUDIENCES if audience is None else (audience,)
    schedules = {a: load_weekly_off_schedule(sid, a) for a in audiences}

    holidays = (_holiday_query(sid, audience)
                .filter(SchoolHoliday.start_date <= date_to,
                        SchoolHoliday.end_date   >= date_from)
                .with_entities(SchoolHoliday.start_date, SchoolHoliday.end_date)
                .all())

    off = set()
    d = date_from
    while d <= date_to:
        weekday = d.weekday()
        if any(weekday in _resolve_from_schedule(schedules[a], legacy, d)
               for a in audiences):
            off.add(d)
        elif any(start <= d <= end for start, end in holidays):
            off.add(d)
        d += timedelta(days=1)
    return off

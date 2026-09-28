"""
Attendance utility helpers — time-based status determination and timezone support.
"""
import pytz
from datetime import datetime


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


def determine_check_in_status(check_in_time, settings, shift=None):
    """
    Return 'present' or 'late' based on check_in_time vs time thresholds.

    If `shift` is provided (AttendanceShift), its late_after_time is used.
    Otherwise falls back to settings.att_late_threshold (existing behaviour).
    Passing shift=None is fully backwards-compatible with all existing callers.

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

    Employee attendance calls this helper WITHOUT a shift (employees have no
    shifts at all), so for staff the school-level threshold remains the single
    switch — already optional, since that column is nullable. Existing schools,
    payroll and stored shift times are unaffected; nothing here writes.
    """
    if shift is not None and getattr(settings, 'is_institute', False):
        if getattr(settings, 'att_late_threshold', None) is None:
            return 'present'
        if getattr(shift, 'late_after_time', None) is None:
            return 'present'

    threshold = None
    if shift is not None:
        threshold = getattr(shift, 'late_after_time', None)
    if threshold is None and settings:
        threshold = getattr(settings, 'att_late_threshold', None)
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

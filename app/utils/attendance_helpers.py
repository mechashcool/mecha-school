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


def is_holiday_date(check_date, school_id, school=None):
    """
    Return True if check_date must be skipped for automatic absence because:
      1. It falls on a weekday listed in school.weekly_off_days  (e.g. "4,5" → Fri+Sat).
      2. It is inside any active SchoolHoliday range for this school or a global holiday
         (school_id IS NULL in school_holidays).

    Pass school= to avoid an extra DB hit when the School object is already loaded.
    Uses bypass_tenant_scope so this is safe to call from background tasks and
    from the AI Face WebSocket service (no request context).
    """
    from app.models import School, SchoolHoliday, db

    if school is None and school_id:
        school = (School.query
                  .execution_options(bypass_tenant_scope=True)
                  .get(school_id))

    # ── weekly day-off check ──────────────────────────────────────────────────
    if school and school.weekly_off_days:
        try:
            off_days = {
                int(d.strip())
                for d in school.weekly_off_days.split(',')
                if d.strip().isdigit()
            }
            if check_date.weekday() in off_days:
                return True
        except (ValueError, AttributeError):
            pass

    # ── named holiday check ───────────────────────────────────────────────────
    q = (SchoolHoliday.query
         .execution_options(bypass_tenant_scope=True)
         .filter(
             SchoolHoliday.is_active == True,
             SchoolHoliday.start_date <= check_date,
             SchoolHoliday.end_date   >= check_date,
         ))
    if school_id:
        q = q.filter(
            db.or_(
                SchoolHoliday.school_id == school_id,
                SchoolHoliday.school_id.is_(None),
            )
        )
    else:
        q = q.filter(SchoolHoliday.school_id.is_(None))

    return q.first() is not None

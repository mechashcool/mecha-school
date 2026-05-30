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


def determine_check_in_status(check_in_time, settings):
    """
    Return 'present' or 'late' based on check_in_time vs SchoolSettings thresholds.
    Falls back to 'present' when no late threshold is configured.
    """
    if settings and settings.att_late_threshold:
        if check_in_time >= settings.att_late_threshold:
            return 'late'
    return 'present'


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

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

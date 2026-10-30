"""
Parent visibility of homework — ONE publication-date rule.

A homework row is visible to a parent once its publish_date is on or before
TODAY IN THE SCHOOL'S TIMEZONE. The server clock is UTC, so date.today() was
three hours behind Baghdad: a homework published "today" at 00:30 Baghdad time
stayed hidden until 03:00. The date is resolved through the existing attendance
helper (pytz, Asia/Baghdad fallback for a missing or unknown timezone).

Everything else that decides visibility (school, academic year, section or
study group, is_active, parent ownership) stays with each caller — this module
owns only the date.
"""
from types import SimpleNamespace

from sqlalchemy import and_, or_

from app.models import db, Homework, School
from app.utils.attendance_helpers import get_local_date, utc_to_local


def school_clock(school):
    """An object with `.timezone`, as the attendance helpers expect.

    `school` may be a loaded School (no query) or a school id (one
    single-column SELECT, pinned to that id)."""
    if hasattr(school, 'timezone'):
        return school
    tz = (db.session.query(School.timezone)
          .execution_options(bypass_tenant_scope=True)
          .filter(School.id == school)
          .scalar())
    return SimpleNamespace(timezone=tz)


def school_local_today(school):
    """Today's date in the school's timezone (School or school id)."""
    return get_local_date(school_clock(school))


def published_clause(local_today):
    """Homework already published on `local_today` — the parent-visible rule."""
    return Homework.publish_date <= local_today


def newly_visible_clause(last_viewed_utc, local_today, clock):
    """Parent-visible homework that was NOT visible at `last_viewed_utc`.

    A row is new when it was created after the last view, or when its
    publish_date came after the local date of the last view (it existed then
    but was still unpublished). `last_viewed_utc` is naive UTC, as stored.
    """
    last_view_date = utc_to_local(last_viewed_utc, clock).date()
    return and_(published_clause(local_today),
                or_(Homework.created_at > last_viewed_utc,
                    Homework.publish_date > last_view_date))

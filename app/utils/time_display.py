"""
Presentation-only conversion of stored UTC timestamps to Baghdad local time.

Timestamps are stored as NAIVE UTC (``datetime.utcnow``).  That convention is
correct and is never changed here: these helpers only build display strings
and never touch the stored value or anything used for ordering/pagination.

Asia/Baghdad comes from the IANA database (``tzdata`` is pinned for hosts
without a system zone database) — no fixed +03:00 arithmetic.

The 12-hour Arabic labels are explicit, never ``%p``: ``%p`` depends on the
server locale, and the CLDR Arabic forms are ص/م rather than صباحًا/مساءً.
"""
from datetime import timezone
from zoneinfo import ZoneInfo

BAGHDAD_TZ = ZoneInfo('Asia/Baghdad')

AM_LABEL = 'صباحًا'
PM_LABEL = 'مساءً'


def to_baghdad(dt):
    """A stored timestamp as an aware Asia/Baghdad datetime (None → None).

    A naive value is the stored UTC convention; an aware value is converted
    from its own offset, so it is never shifted twice.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BAGHDAD_TZ)


def format_baghdad(dt, fmt='%Y-%m-%d %H:%M'):
    """strftime() in Baghdad time; '' for a missing value."""
    if not dt:
        return ''
    return to_baghdad(dt).strftime(fmt)


def format_time_ar12(dt):
    """'08:15 مساءً' — 12-hour Baghdad time with an Arabic period label."""
    if not dt:
        return ''
    local = to_baghdad(dt)
    label = AM_LABEL if local.hour < 12 else PM_LABEL
    return f'{local:%I:%M} {label}'


def format_datetime_ar12(dt):
    """'29/10/2026 - 08:15 مساءً' — Baghdad date plus 12-hour Arabic time."""
    if not dt:
        return ''
    return f'{to_baghdad(dt):%d/%m/%Y} - {format_time_ar12(dt)}'

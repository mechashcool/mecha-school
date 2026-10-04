"""
Employee attendance calculation helpers.

Calculates working days and per-employee stats.
Kept entirely separate from student attendance to avoid any interference.

Employee absences are stored by the existing attendance scheduler after the
configured cutoff. Reports count only persisted absent rows; the separate
``classify_missing_working_day`` rule remains available to the scheduler and
payroll handling that intentionally depends on it.
"""
from __future__ import annotations
import re as _re
from datetime import date, datetime, time, timedelta
from typing import Dict, List, NamedTuple, Optional

# Matches the AiFace dedup tag written by _process_employee_punch:
#   "AI Face YYYY-MM-DD HH:MM:SS"
# Multiple tags are pipe-separated; each segment is checked individually.
_AIFACE_DEDUP_RE = _re.compile(r'^AI Face \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')
AUTO_ABSENCE_SOURCE = 'auto_absence'


def is_final_employee_auto_absence(record) -> bool:
    """True for an automatic absent row whose official status is final that day."""
    return bool(
        record
        and record.status == 'absent'
        and record.source == AUTO_ABSENCE_SOURCE
    )


def _clean_employee_notes(raw: str | None) -> str | None:
    """Strip AiFace dedup tags from the notes field for display purposes.

    The raw DB value must not be modified — it is still used by
    _process_employee_punch to detect duplicate device punches.
    """
    if not raw:
        return None
    parts = [p.strip() for p in raw.split('|')]
    cleaned = [p for p in parts if p and not _AIFACE_DEDUP_RE.match(p)]
    return ' | '.join(cleaned) or None


# ── Working-day calendar ─────────────────────────────────────────────────────

def get_working_days(date_from: date, date_to: date, school) -> List[date]:
    """
    Returns all calendar days in [date_from, date_to] that are not days off for
    EMPLOYEES:
      - Employee weekly days off in force on that date (effective-dated
        SchoolWeeklyOffSchedule, else the legacy school.weekly_off_days)
      - Named SchoolHoliday entries (this school or global) whose applies_to
        is 'both' or 'employees' — student-only holidays stay working days
    Uses the same rules as is_holiday_date(..., audience='employees'); there is
    deliberately NO separate weekly check here, so the shared legacy value can
    never override the employee schedule.
    """
    from app.utils.attendance_helpers import get_off_dates

    off = get_off_dates(date_from, date_to, school, audience='employees')

    working: List[date] = []
    current = date_from
    while current <= date_to:
        if current not in off:
            working.append(current)
        current += timedelta(days=1)
    return working


# ── Absence clock: when may a missing day be called absent? ──────────────────
#
# Employees have no auto-absence job, so "absent" is decided while reading. That
# makes the CLOCK part of the calculation: a working day with no row is only an
# absence once the day's arrival window has actually closed.
#
# General-mode absence uses the school-level employee cutoff. Shift-mode
# callers use each already-resolved EmployeeAttendanceShift cutoff instead.

# A stored 00:00 is treated as "not configured". The column is nullable and the
# UI writes NULL when cleared, but a mis-saved midnight must not silently mean
# "every employee is absent from the very start of the day".
_MIDNIGHT = time(0, 0)


class EmployeeAbsenceClock(NamedTuple):
    """School-local now + the effective employee absence cutoff."""
    local_today:    date
    local_now:      datetime
    absence_cutoff: Optional[time]       # None = not configured → fail closed

    @property
    def cutoff_passed(self) -> bool:
        """True only when a cutoff IS configured and the local time reached it."""
        return (self.absence_cutoff is not None
                and self.local_now.time() >= self.absence_cutoff)


def get_employee_absence_clock(school) -> EmployeeAbsenceClock:
    """Resolve school-local time and the general-mode employee cutoff.

    In employee shift mode the returned cutoff is deliberately None; callers
    must use each already-resolved shift's absent_after_time.
    """
    from app.utils.attendance_helpers import (get_effective_attendance_settings,
                                              get_local_now)

    now = get_local_now(school)
    cutoff = get_effective_attendance_settings(school, 'employees').absence_cutoff
    if cutoff == _MIDNIGHT:
        cutoff = None
    return EmployeeAbsenceClock(local_today=now.date(), local_now=now,
                               absence_cutoff=cutoff)


def classify_missing_working_day(d: date, clock: EmployeeAbsenceClock,
                                 hire_date: Optional[date] = None) -> Optional[str]:
    """Classify a working day that has NO EmployeeAttendance row.

    Returns:
        'absent'       – the day is over (or today's cutoff has passed)
        'not_recorded' – today, and absence cannot legitimately be declared yet
        None           – not an attendance obligation at all; the caller must
                         exclude the day entirely (future, or before hire_date)

    Rules (school-local):
        before hire_date        → None   (the employee was not employed yet)
        future day              → None   (it has not happened)
        past working day        → 'absent'
        today, cutoff passed    → 'absent'
        today, before cutoff    → 'not_recorded'
        today, cutoff NULL      → 'not_recorded'  (never auto-absent)
    """
    if hire_date and d < hire_date:
        return None
    if d > clock.local_today:
        return None
    if d < clock.local_today:
        return 'absent'
    return 'absent' if clock.cutoff_passed else 'not_recorded'


# ── Per-employee statistics ───────────────────────────────────────────────────

def calculate_employee_stats(employee,
                              records_by_date: Dict[date, object],
                              working_days: List[date],
                              school=None,
                              *,
                              clock: Optional[EmployeeAbsenceClock] = None) -> dict:
    """
    Build full attendance statistics for one employee across working_days.

    A day with no record is always ``not_recorded``. Only a persisted attendance
    row with status ``absent`` contributes to the absence count. Future and
    pre-hire days are dropped from the day list entirely.

    `clock` lets a bulk caller resolve the school clock once; when omitted it is
    derived from `school`.

    Returns:
        employee      – the Employee ORM object
        present       – count of on-time days
        late          – count of late days
        absent        – count of persisted records with status='absent'
        on_leave      – count of approved-leave days (status='on_leave')
        not_recorded  – working days with no persisted attendance row
        checked_out   – count of days with a check_out time
        working_days  – working days that are an attendance obligation for THIS
                        employee (excludes future and pre-hire days)
        calendar_working_days – working days in the range for the school
        attended      – present + late (employee showed up)
        rate          – attended / (working_days - on_leave - not_recorded) *
                        100; approved leave is excused and a day that is not yet
                        declarable is not counted against the employee
        daily         – list of per-day dicts (date, status, check_in, ...)
    """
    if clock is None:
        clock = get_employee_absence_clock(school)
    hire_date = getattr(employee, 'hire_date', None)

    daily: list = []
    present = absent = late = checked_out = on_leave = not_recorded = 0

    for d in working_days:
        rec = records_by_date.get(d)
        if rec is None:
            if (hire_date and d < hire_date) or d > clock.local_today:
                continue        # future or pre-employment — not an obligation
            daily.append({
                'date': d,
                'status': 'not_recorded',
                'check_in': None,
                'check_out': None,
                'source': None,
                'device': None,
                'notes': None,
                'is_virtual': True,
            })
            not_recorded += 1
        else:
            status = rec.status
            if status == 'present':
                present += 1
            elif status == 'late':
                late += 1
            elif status == 'absent':
                absent += 1
            elif status == 'on_leave':
                on_leave += 1
            if rec.check_out:
                checked_out += 1
            daily.append({
                'date': d,
                'status': status,
                'check_in': rec.check_in,
                'check_out': rec.check_out,
                'source': rec.source,
                'device': rec.device,
                'notes': _clean_employee_notes(rec.notes),
                'is_virtual': False,
                'record_id': rec.id,
            })

    # `daily` now holds only the days that are an obligation for THIS employee:
    # future and pre-hire days were skipped above. A day that HAS a record is
    # always kept — real data is never hidden.
    total = len(daily)
    attended = present + late
    # Approved leave is excused, and a day that cannot yet be declared absent is
    # not yet a miss — neither belongs in the denominator.
    billable = total - on_leave - not_recorded
    rate = round(attended / billable * 100, 1) if billable > 0 else 0.0

    return {
        'employee': employee,
        'present': present,
        'late': late,
        'absent': absent,
        'on_leave': on_leave,
        'not_recorded': not_recorded,
        'checked_out': checked_out,
        'working_days': total,
        'calendar_working_days': len(working_days),
        'attended': attended,
        'rate': rate,
        'daily': daily,
    }


# ── Bulk summary (all employees in one query) ─────────────────────────────────

def get_employees_attendance_summary(
    employees,
    date_from: date,
    date_to: date,
    school,
    name_search: str = '',
    department: str = '',
    status_filter: str = '',
) -> list:
    """
    Build persisted-attendance summaries for a list of employees.
    Fetches all EmployeeAttendance records in one bulk query, then assembles
    per-employee stats.  Filters are applied in Python after the bulk fetch.

    status_filter values: 'present' | 'late' | 'absent' | 'on_leave' |
                          'not_recorded' | '' (= all)
    The filter keeps rows where the employee has AT LEAST ONE day of that status.

    The range end is clamped to the SCHOOL-LOCAL date, so a future date_to can
    never turn days that have not happened into attendance obligations.  The
    absence clock (and therefore the employee cutoff) is resolved ONCE here and
    shared by every employee — no per-employee settings or shift query.
    """
    from app.models import EmployeeAttendance

    clock = get_employee_absence_clock(school)
    effective_to = min(date_to, clock.local_today)
    if effective_to < date_from:
        return []                      # range is entirely in the future

    working_days = get_working_days(date_from, effective_to, school)

    # Narrow the employee list first (cheap in-memory)
    filtered = list(employees)
    if name_search:
        term = name_search.strip().lower()
        filtered = [e for e in filtered if term in (e.full_name or '').lower()]
    if department:
        filtered = [e for e in filtered if (e.department or '') == department]

    if not filtered:
        return []

    emp_ids = [e.id for e in filtered]

    # One bulk DB query for all employees in the date range (no year scoping here —
    # HR date-range reports should work across year boundaries)
    all_records = (
        EmployeeAttendance.query
        .execution_options(bypass_tenant_scope=True)
        .filter(
            EmployeeAttendance.school_id == school.id,
            EmployeeAttendance.employee_id.in_(emp_ids),
            EmployeeAttendance.date >= date_from,
            EmployeeAttendance.date <= effective_to,
        )
        .all()
    )

    # Index: employee_id → {date → record}
    records_map: Dict[int, Dict[date, object]] = {}
    for rec in all_records:
        records_map.setdefault(rec.employee_id, {})[rec.date] = rec

    results = []
    for emp in filtered:
        stats = calculate_employee_stats(emp, records_map.get(emp.id, {}),
                                         working_days, school, clock=clock)

        # Apply status_filter to summary row
        if status_filter == 'present' and stats['present'] == 0:
            continue
        if status_filter == 'late' and stats['late'] == 0:
            continue
        if status_filter == 'absent' and stats['absent'] == 0:
            continue
        if status_filter == 'on_leave' and stats.get('on_leave', 0) == 0:
            continue
        if status_filter == 'not_recorded' and stats.get('not_recorded', 0) == 0:
            continue

        results.append(stats)

    return results


# ── Absence-limit alerts ──────────────────────────────────────────────────────

PERIOD_LABELS = {
    'monthly': 'شهرياً',
    'yearly': 'سنوياً',
    'range': 'في الفترة المحددة',
}


def get_absence_alerts(summary_rows: list, school) -> list:
    """
    Returns a list of dicts for employees whose absent count exceeds
    school.emp_absence_limit.  Returns [] if limit is not configured.

    Counts row['absent'] ONLY — which by construction already excludes
    not_recorded, on_leave, weekly days off, employee holidays, pre-hire days
    and future days.  An alert can therefore never be raised for a day the
    employee was not yet obliged to attend.
    """
    limit = getattr(school, 'emp_absence_limit', None)
    if not limit:
        return []

    alert_enabled = getattr(school, 'emp_absence_alert_enabled', True)
    if not alert_enabled:
        return []

    period = getattr(school, 'emp_absence_period', 'monthly') or 'monthly'
    period_label = PERIOD_LABELS.get(period, period)

    alerts = []
    for row in summary_rows:
        if row['absent'] > limit:
            alerts.append({
                'employee': row['employee'],
                'absent': row['absent'],
                'limit': limit,
                'period': period,
                'period_label': period_label,
                'over_by': row['absent'] - limit,
            })
    return sorted(alerts, key=lambda a: a['absent'], reverse=True)

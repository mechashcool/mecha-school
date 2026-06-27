"""
Employee attendance calculation helpers.

Calculates working days, virtual absences, and per-employee stats.
Kept entirely separate from student attendance to avoid any interference.
"""
from __future__ import annotations
import re as _re
from datetime import date, timedelta
from typing import Dict, List

# Matches the AiFace dedup tag written by _process_employee_punch:
#   "AI Face YYYY-MM-DD HH:MM:SS"
# Multiple tags are pipe-separated; each segment is checked individually.
_AIFACE_DEDUP_RE = _re.compile(r'^AI Face \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')


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
    Returns all calendar days in [date_from, date_to] that are not:
      - School weekly off days  (school.weekly_off_days, e.g. "4,5" = Fri+Sat)
      - Named SchoolHoliday entries for this school or global holidays
    Delegates to the existing is_holiday_date() helper so logic stays in one place.
    """
    from app.utils.attendance_helpers import is_holiday_date

    weekly_off: set[int] = set()
    if school and school.weekly_off_days:
        try:
            weekly_off = {
                int(d.strip())
                for d in school.weekly_off_days.split(',')
                if d.strip().isdigit()
            }
        except (ValueError, AttributeError):
            pass

    working: List[date] = []
    current = date_from
    while current <= date_to:
        if current.weekday() not in weekly_off:
            if not is_holiday_date(current, school.id if school else None, school=school):
                working.append(current)
        current += timedelta(days=1)
    return working


# ── Per-employee statistics ───────────────────────────────────────────────────

def calculate_employee_stats(employee,
                              records_by_date: Dict[date, object],
                              working_days: List[date]) -> dict:
    """
    Build full attendance statistics for one employee across working_days.
    Days without a record in records_by_date are treated as virtual-absent.

    Returns:
        employee      – the Employee ORM object
        present       – count of on-time days
        late          – count of late days
        absent        – count of absent days (real DB records + virtual)
        on_leave      – count of approved-leave days (status='on_leave')
        checked_out   – count of days with a check_out time
        working_days  – total number of working days in range
        attended      – present + late (employee showed up)
        rate          – attended / (working_days - on_leave) * 100; approved
                        leave days are excluded from the denominator so they
                        do not penalise the attendance percentage
        daily         – list of per-day dicts (date, status, check_in, ...)
    """
    daily: list = []
    present = absent = late = checked_out = on_leave = 0

    for d in working_days:
        rec = records_by_date.get(d)
        if rec is None:
            # No DB record → virtual absence
            daily.append({
                'date': d,
                'status': 'absent',
                'check_in': None,
                'check_out': None,
                'source': None,
                'device': None,
                'notes': None,
                'is_virtual': True,
            })
            absent += 1
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

    total = len(working_days)
    attended = present + late
    # Approved leave days are excused — exclude them from the rate denominator
    billable = total - on_leave
    rate = round(attended / billable * 100, 1) if billable > 0 else 0.0

    return {
        'employee': employee,
        'present': present,
        'late': late,
        'absent': absent,
        'on_leave': on_leave,
        'checked_out': checked_out,
        'working_days': total,
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
    Build attendance summaries (with virtual absences) for a list of employees.
    Fetches all EmployeeAttendance records in one bulk query, then assembles
    per-employee stats.  Filters are applied in Python after the bulk fetch.

    status_filter values: 'present' | 'late' | 'absent' | 'on_leave' | '' (= all)
    The filter keeps rows where the employee has AT LEAST ONE day of that status.
    """
    from app.models import EmployeeAttendance

    working_days = get_working_days(date_from, date_to, school)

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
            EmployeeAttendance.date <= date_to,
        )
        .all()
    )

    # Index: employee_id → {date → record}
    records_map: Dict[int, Dict[date, object]] = {}
    for rec in all_records:
        records_map.setdefault(rec.employee_id, {})[rec.date] = rec

    results = []
    for emp in filtered:
        stats = calculate_employee_stats(emp, records_map.get(emp.id, {}), working_days)

        # Apply status_filter to summary row
        if status_filter == 'present' and stats['present'] == 0:
            continue
        if status_filter == 'late' and stats['late'] == 0:
            continue
        if status_filter == 'absent' and stats['absent'] == 0:
            continue
        if status_filter == 'on_leave' and stats.get('on_leave', 0) == 0:
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

"""
Background auto-attendance scheduler.

Runs every ATTENDANCE_CHECK_INTERVAL seconds (default 300 = 5 min).
For each active school, checks whether the absence cutoff has passed and
calls _run_auto_absent to mark unmarked students absent and notify parents.

Set ATTENDANCE_SCHEDULER_DISABLED=true in the environment to opt out.

Cache policy
────────────
No per-school "already ran today" cache is used.  _run_auto_absent is fully
idempotent — it queries existing records before creating new ones, so calling
it multiple times a day is safe and only creates records for students who still
have no attendance row.

Removing the cache ensures that students added after the first run of the day
are still processed on subsequent scheduler ticks (they have no attendance row
so they are correctly marked absent).  The only extra cost is a lightweight
DB read every tick (~5 min) per school.
"""
import logging
import os
import threading
import time
from datetime import timedelta

_log = logging.getLogger(__name__)
_scheduler_thread: threading.Thread | None = None

# If local time is before this hour (exclusive), the previous day's catch-up runs.
# Covers the window where a 23:59 cutoff tick was missed and the next tick fires
# after midnight (e.g., 00:01–00:59).
_CATCHUP_WINDOW_HOURS = 1


def start_auto_attendance_scheduler(app) -> None:
    """
    Start the background auto-attendance daemon thread.
    Called once from create_app(). Safe to call multiple times.
    """
    if os.environ.get('ATTENDANCE_SCHEDULER_DISABLED', '').lower() == 'true':
        app.logger.info('[attendance] auto-attendance scheduler disabled via env var')
        return
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    interval = max(60, int(os.environ.get('ATTENDANCE_CHECK_INTERVAL', '300')))
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        args=(app, interval),
        daemon=True,
        name='auto-attendance-scheduler',
    )
    _scheduler_thread.start()
    app.logger.info('[attendance] auto-attendance scheduler started (interval=%ds)', interval)


def _scheduler_loop(app, interval: int) -> None:
    """Main loop — runs inside a long-lived app context."""
    with app.app_context():
        while True:
            try:
                _run_check()
            except Exception as exc:
                _log.error('[attendance] scheduler loop error: %s', exc)
                try:
                    from app.models import db
                    db.session.rollback()
                except Exception:
                    pass
            finally:
                # Return the DB connection to the pool between ticks.
                # The long-lived app_context() does not trigger teardown handlers
                # until it exits, so we remove the scoped session manually to
                # avoid holding a connection open during time.sleep().
                try:
                    from app.models import db
                    db.session.remove()
                except Exception:
                    pass
            time.sleep(interval)


def _run_check() -> None:
    """Called every tick. Iterates all active schools and fires per-school logic."""
    from app.models import School

    _log.info('[attendance] scheduled auto absent check — scanning active schools')

    schools = (
        School.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(is_active=True)
        .all()
    )
    _log.info('[attendance] found %d active school(s) to check', len(schools))

    for school in schools:
        try:
            _check_school(school)
        except Exception as exc:
            _log.error('[attendance] scheduler error school_id=%s name=%s: %s',
                       school.id, getattr(school, 'name', '?'), exc)
            try:
                from app.models import db
                db.session.rollback()
            except Exception:
                pass


def _check_school(school) -> None:
    """Per-school check: fire _run_auto_absent if cutoff has passed."""
    from app.utils.attendance_helpers import get_local_now, get_local_date

    school_name = getattr(school, 'name', f'school_{school.id}')
    local_now   = get_local_now(school)
    local_date  = get_local_date(school)

    # Branch: shifts mode vs. standard single-cutoff mode
    if getattr(school, 'enable_attendance_shifts', False):
        _check_school_shifts(school, school_name, local_now, local_date)
        return

    # ── Standard single-cutoff mode (existing behaviour, unchanged) ──────────
    from app.utils.decorators import get_active_year
    from app.blueprints.attendance import _run_auto_absent

    cutoff = getattr(school, 'att_absence_threshold', None)

    if not cutoff:
        _log.info('[attendance] school_id=%s "%s" — no absence cutoff configured, skip',
                  school.id, school_name)
        return

    now_time = local_now.time()
    passed   = now_time >= cutoff

    _log.info(
        '[attendance] school_id=%s "%s" local_now=%s cutoff=%s passed=%s',
        school.id, school_name, local_now.strftime('%Y-%m-%d %H:%M:%S'), cutoff, passed,
    )

    if not passed:
        _log.info('[attendance] school_id=%s — absence time not yet reached, skip', school.id)
        _catchup_previous_day(school, school_name, local_now, local_date)
        return

    year = get_active_year(school.id)
    if not year:
        _log.warning('[attendance] school_id=%s "%s" — no active academic year, skip',
                     school.id, school_name)
        return

    _log.info('[attendance] school_id=%s "%s" year_id=%s — calling _run_auto_absent',
              school.id, school_name, year.id)

    result = _run_auto_absent(school, year, school)

    if result.get('too_early'):
        _log.info('[attendance] school_id=%s — _run_auto_absent says too_early '
                  '(clock skew?), will retry next tick', school.id)
        return

    if result.get('holiday'):
        _log.info('[attendance] school_id=%s "%s" date=%s — holiday, absent skipped',
                  school.id, school_name, local_date)
        return

    count = result.get('count', 0)
    _log.info(
        '[attendance] school_id=%s "%s" date=%s — absent created=%d',
        school.id, school_name, local_date, count,
    )

    _catchup_previous_day(school, school_name, local_now, local_date)


# ─────────────────────────────────────────────────────────────────────────────
#  SHIFTS MODE — per-shift auto-absence
# ─────────────────────────────────────────────────────────────────────────────

def _check_school_shifts(school, school_name: str, local_now, local_date) -> None:
    """Per-school check when enable_attendance_shifts=True. Runs each active shift."""
    from app.models import AttendanceShift
    from app.utils.attendance_helpers import is_holiday_date
    from app.utils.decorators import get_active_year

    now_time = local_now.time()

    if is_holiday_date(local_date, school.id, school):
        _log.info('[attendance-shift] school_id=%s date=%s — holiday, skip all shifts',
                  school.id, local_date)
        _catchup_previous_day_shifts(school, school_name, local_now, local_date)
        return

    year = get_active_year(school.id)
    if not year:
        _log.info('[attendance-shift] school_id=%s — no active academic year, skip',
                  school.id)
        return

    active_shifts = (
        AttendanceShift.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(school_id=school.id, is_active=True)
        .all()
    )

    if not active_shifts:
        _log.info('[attendance-shift] school_id=%s "%s" — no active shifts configured, skip',
                  school.id, school_name)
        return

    for shift in active_shifts:
        cutoff = shift.absent_after_time
        passed = now_time >= cutoff
        _log.info(
            '[attendance-shift] school_id=%s shift_id=%s "%s" cutoff=%s passed=%s',
            school.id, shift.id, shift.name, cutoff, passed,
        )
        if passed:
            _run_auto_absent_for_shift(school, year, shift, local_date)

    _catchup_previous_day_shifts(school, school_name, local_now, local_date)


def _run_auto_absent_for_shift(school, year, shift, target_date) -> dict:
    """
    Mark absent all active students in sections assigned to `shift` who have
    no attendance record for `target_date`.

    Fully idempotent — safe to call multiple times for the same shift/date.
    Does NOT check holidays; caller handles that.
    """
    from app.models import db, Student, StudentAttendance, Section
    from app.blueprints.attendance import _notify_absent_parents

    school_id = school.id

    # Find active sections assigned to this shift
    shift_section_ids = {
        row.id for row in
        Section.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school_id, shift_id=shift.id)
            .with_entities(Section.id)
            .all()
    }

    if not shift_section_ids:
        _log.info('[attendance-shift] shift_id=%s "%s" — no sections assigned, skip',
                  shift.id, shift.name)
        return {'count': 0}

    all_students = (
        Student.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(status='active', school_id=school_id)
        .filter(Student.section_id.in_(shift_section_ids))
        .all()
    )
    student_ids = [s.id for s in all_students]

    _log.info(
        '[attendance-shift] shift_id=%s "%s" date=%s active_students=%d',
        shift.id, shift.name, target_date, len(student_ids),
    )

    if not student_ids:
        return {'count': 0}

    already_ids = {
        row.student_id for row in
        StudentAttendance.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(date=target_date)
            .filter(StudentAttendance.student_id.in_(student_ids))
            .with_entities(StudentAttendance.student_id)
            .all()
    }
    unmarked = [s for s in all_students if s.id not in already_ids]

    _log.info(
        '[attendance-shift] shift_id=%s date=%s existing=%d missing=%d',
        shift.id, target_date, len(already_ids), len(unmarked),
    )

    for student in unmarked:
        db.session.add(StudentAttendance(
            student_id       = student.id,
            school_id        = school_id,
            academic_year_id = year.id if year else None,
            date             = target_date,
            status           = 'absent',
            source           = 'automatic',
            shift_id         = shift.id,
        ))

    if unmarked:
        db.session.commit()
        _log.info(
            '[attendance-shift] shift_id=%s "%s" date=%s absent_created=%d',
            shift.id, shift.name, target_date, len(unmarked),
        )

    notified = 0
    for student in unmarked:
        try:
            _notify_absent_parents(
                student, school_id, target_date.isoformat(),
                source='automatic',
                shift_name=shift.name,
            )
            notified += 1
        except Exception:
            _log.exception('[attendance-shift] notification failed student_id=%s', student.id)

    _log.info(
        '[attendance-shift] shift_id=%s "%s" date=%s absent_created=%d notifications_sent=%d',
        shift.id, shift.name, target_date, len(unmarked), notified,
    )
    return {'count': len(unmarked)}


def _catchup_previous_day_shifts(school, school_name: str, local_now, local_date) -> None:
    """
    Within _CATCHUP_WINDOW_HOURS after midnight: re-run per-shift auto-absence for
    yesterday so a near-midnight absent_after_time is never permanently missed.
    """
    if local_now.hour >= _CATCHUP_WINDOW_HOURS:
        return

    from app.models import AttendanceShift
    from app.utils.attendance_helpers import is_holiday_date
    from app.utils.decorators import get_active_year

    yesterday = local_date - timedelta(days=1)

    try:
        if is_holiday_date(yesterday, school.id, school):
            _log.info('[attendance-shift] catch-up school_id=%s date=%s — holiday, skip',
                      school.id, yesterday)
            return
    except Exception:
        _log.exception('[attendance-shift] catch-up holiday check failed school_id=%s date=%s',
                       school.id, yesterday)
        return

    year = get_active_year(school.id)
    if not year:
        _log.info('[attendance-shift] catch-up school_id=%s date=%s — no_active_year, skip',
                  school.id, yesterday)
        return

    active_shifts = (
        AttendanceShift.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(school_id=school.id, is_active=True)
        .all()
    )

    for shift in active_shifts:
        _log.info('[attendance-shift] catch-up check school_id=%s shift_id=%s "%s" date=%s',
                  school.id, shift.id, shift.name, yesterday)
        try:
            result = _run_auto_absent_for_shift(school, year, shift, yesterday)
            _log.info('[attendance-shift] catch-up absent_created=%d school_id=%s shift_id=%s date=%s',
                      result.get('count', 0), school.id, shift.id, yesterday)
        except Exception:
            _log.exception('[attendance-shift] catch-up failed school_id=%s shift_id=%s date=%s',
                           school.id, shift.id, yesterday)


def _catchup_previous_day(school, school_name: str, local_now, local_date) -> None:
    """
    If the scheduler is running within _CATCHUP_WINDOW_HOURS after midnight,
    process the previous calendar day to recover absences that were missed when
    the server restarted around the cutoff time (e.g., cutoff=23:59, next tick=00:01).

    Fully idempotent: _run_auto_absent skips students who already have a record.
    Only runs once per tick per school; the caller controls when this is invoked.
    """
    if local_now.hour >= _CATCHUP_WINDOW_HOURS:
        return  # not in the midnight catch-up window

    from app.utils.decorators import get_active_year
    from app.blueprints.attendance import _run_auto_absent
    from app.utils.attendance_helpers import is_holiday_date

    yesterday = local_date - timedelta(days=1)

    try:
        if is_holiday_date(yesterday, school.id, school):
            _log.info('[attendance] catch-up school_id=%s date=%s — skipped_reason=holiday',
                      school.id, yesterday)
            return
    except Exception:
        _log.exception('[attendance] catch-up holiday check failed school_id=%s date=%s',
                       school.id, yesterday)
        return

    year = get_active_year(school.id)
    if not year:
        _log.info('[attendance] catch-up school_id=%s date=%s — skipped_reason=no_active_year',
                  school.id, yesterday)
        return

    _log.info('[attendance] catch-up check school_id=%s "%s" date=%s',
              school.id, school_name, yesterday)

    try:
        result = _run_auto_absent(school, year, school, target_date=yesterday)
    except Exception:
        _log.exception('[attendance] catch-up failed school_id=%s date=%s', school.id, yesterday)
        return

    if result.get('holiday'):
        _log.info('[attendance] catch-up school_id=%s date=%s — skipped_reason=holiday',
                  school.id, yesterday)
        return

    count = result.get('count', 0)
    _log.info('[attendance] catch-up absent_created=%d school_id=%s date=%s',
              count, school.id, yesterday)

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
        _log.warning('[attendance] auto-attendance scheduler disabled via ATTENDANCE_SCHEDULER_DISABLED env var')
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
    _log.warning('[attendance] auto-attendance scheduler started (interval=%ds)', interval)


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

    schools = (
        School.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(is_active=True)
        .all()
    )

    if not schools:
        _log.warning('[attendance] tick: no active schools found — nothing to process')
        return

    _log.info('[attendance] tick: checking %d active school(s)', len(schools))

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

    # ── Standard single-cutoff mode ──────────────────────────────────────────
    from app.utils.decorators import get_active_year
    from app.blueprints.attendance import _run_auto_absent

    cutoff = getattr(school, 'att_absence_threshold', None)

    # Python's datetime.time(0, 0, 0) is falsy — treat midnight (00:00) the same
    # as None (unconfigured) so a mis-saved form value doesn't silently skip absence.
    if not cutoff:
        _log.warning(
            '[attendance] school_id=%s "%s" — no absence cutoff configured '
            '(att_absence_threshold is NULL or 00:00), skip',
            school.id, school_name,
        )
        return

    now_time = local_now.time()
    passed   = now_time >= cutoff

    _log.info(
        '[attendance] school_id=%s "%s" local_now=%s cutoff=%s passed=%s',
        school.id, school_name, local_now.strftime('%Y-%m-%d %H:%M:%S'), cutoff, passed,
    )

    if not passed:
        _log.info('[attendance] school_id=%s "%s" — absence cutoff not yet reached '
                  '(now=%s < cutoff=%s), skip',
                  school.id, school_name, now_time, cutoff)
        _catchup_previous_day(school, school_name, local_now, local_date)
        return

    year = get_active_year(school.id)
    if not year:
        _log.warning('[attendance] school_id=%s "%s" — no active academic year, skip',
                     school.id, school_name)
        return

    _log.info('[attendance] school_id=%s "%s" year_id=%s date=%s — running auto-absent',
              school.id, school_name, year.id, local_date)

    # Pass target_date=local_date explicitly so _run_auto_absent skips its own
    # redundant clock check (we already verified cutoff passed above).
    result = _run_auto_absent(school, year, school, target_date=local_date)

    if result.get('holiday'):
        _log.warning('[attendance] school_id=%s "%s" date=%s — holiday, absent skipped',
                     school.id, school_name, local_date)
        return

    count = result.get('count', 0)
    if count > 0:
        _log.warning(
            '[attendance] school_id=%s "%s" date=%s — absent_created=%d',
            school.id, school_name, local_date, count,
        )
    else:
        _log.info(
            '[attendance] school_id=%s "%s" date=%s — absent_created=0 '
            '(all students already have records or no active students)',
            school.id, school_name, local_date,
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
        _log.warning('[attendance-shift] school_id=%s "%s" date=%s — holiday, skip all shifts',
                     school.id, school_name, local_date)
        _catchup_previous_day_shifts(school, school_name, local_now, local_date)
        return

    year = get_active_year(school.id)
    if not year:
        _log.warning('[attendance-shift] school_id=%s "%s" — no active academic year, skip',
                     school.id, school_name)
        return

    active_shifts = (
        AttendanceShift.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(school_id=school.id, is_active=True)
        .all()
    )

    _log.info(
        '[attendance-shift] school_id=%s "%s" shifts_enabled=True active_shifts=%d local_now=%s',
        school.id, school_name, len(active_shifts), local_now.strftime('%H:%M:%S'),
    )

    if not active_shifts:
        _log.warning('[attendance-shift] school_id=%s "%s" — no active shifts configured, skip',
                     school.id, school_name)
        return

    for shift in active_shifts:
        cutoff = shift.absent_after_time
        passed = now_time >= cutoff
        _log.info(
            '[attendance-shift] school_id=%s shift_id=%s "%s" cutoff=%s local_now=%s passed=%s',
            school.id, shift.id, shift.name, cutoff,
            local_now.strftime('%H:%M:%S'), passed,
        )
        if passed:
            try:
                result = _run_auto_absent_for_shift(school, year, shift, local_date)
                count = result.get('count', 0)
                if count > 0:
                    _log.warning(
                        '[attendance-shift] school_id=%s shift_id=%s "%s" date=%s — absent_created=%d',
                        school.id, shift.id, shift.name, local_date, count,
                    )
                else:
                    _log.info(
                        '[attendance-shift] school_id=%s shift_id=%s "%s" date=%s — '
                        'absent_created=0 (all students already have records or no students)',
                        school.id, shift.id, shift.name, local_date,
                    )
            except Exception:
                _log.exception(
                    '[attendance-shift] school_id=%s shift_id=%s "%s" failed — '
                    'continuing with remaining shifts',
                    school.id, shift.id, shift.name,
                )
                try:
                    from app.models import db
                    db.session.rollback()
                except Exception:
                    pass

    # Case 4 — students with no shift fall back to the school default cutoff.
    try:
        fb = _run_auto_absent_shiftless(school, year, school, local_date, now_time=now_time)
        fb_count = fb.get('count', 0)
        if fb_count > 0:
            _log.warning(
                '[attendance-shift-fallback] school_id=%s "%s" date=%s — absent_created=%d '
                '(shiftless students)',
                school.id, school_name, local_date, fb_count,
            )
        else:
            _log.info(
                '[attendance-shift-fallback] school_id=%s "%s" date=%s — absent_created=0 '
                '(no shiftless students, cutoff not reached, or all already have records)',
                school.id, school_name, local_date,
            )
    except Exception:
        _log.exception(
            '[attendance-shift-fallback] school_id=%s shiftless fallback failed',
            school.id,
        )
        try:
            from app.models import db
            db.session.rollback()
        except Exception:
            pass

    _catchup_previous_day_shifts(school, school_name, local_now, local_date)


def _run_auto_absent_for_shift(school, year, shift, target_date) -> dict:
    """
    Mark absent all active students whose EFFECTIVE shift matches `shift` and
    who have no attendance record for `target_date`.

    Effective shift priority:
      1. student.section.shift_id  — explicit section assignment
      2. student.section.grade.shift_id — grade fallback (schools without
         per-section shift assignment)

    Fully idempotent — safe to call multiple times for the same shift/date.
    Does NOT check holidays; caller handles that.
    """
    from app.models import db, Student, StudentAttendance, Section, Grade
    from app.blueprints.attendance import _notify_absent_parents

    school_id = school.id

    _log.info(
        '[attendance-shift] school_id=%s shift_id=%s "%s" date=%s year_id=%s — collecting students',
        school_id, shift.id, shift.name, target_date, year.id if year else None,
    )

    # ── 1. Sections explicitly assigned to this shift ─────────────────────────
    explicit_section_ids = {
        row.id for row in
        Section.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school_id, shift_id=shift.id)
            .with_entities(Section.id)
            .all()
    }
    _log.info(
        '[attendance-shift] shift_id=%s explicit_sections=%d ids=%s',
        shift.id, len(explicit_section_ids), sorted(explicit_section_ids),
    )

    # ── 2. Grades assigned to this shift (for sections that have no shift_id) ─
    grade_shift_ids = {
        row.id for row in
        Grade.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school_id, shift_id=shift.id)
            .with_entities(Grade.id)
            .all()
    }
    # Sections under those grades that have NO explicit shift_id override
    fallback_section_ids: set[int] = set()
    if grade_shift_ids:
        fallback_section_ids = {
            row.id for row in
            Section.query
                .execution_options(bypass_tenant_scope=True)
                .filter(
                    Section.school_id == school_id,
                    Section.grade_id.in_(grade_shift_ids),
                    Section.shift_id.is_(None),
                )
                .with_entities(Section.id)
                .all()
        }
    _log.info(
        '[attendance-shift] shift_id=%s grade_shift_ids=%d fallback_sections=%d ids=%s',
        shift.id, len(grade_shift_ids), len(fallback_section_ids),
        sorted(fallback_section_ids),
    )

    all_shift_section_ids = explicit_section_ids | fallback_section_ids

    if not all_shift_section_ids:
        _log.info(
            '[attendance-shift] shift_id=%s "%s" — no sections (explicit or via grade), skip',
            shift.id, shift.name,
        )
        return {'count': 0}

    # ── 3. Active students in those sections ──────────────────────────────────
    all_students = (
        Student.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(status='active', school_id=school_id)
        .filter(Student.section_id.in_(all_shift_section_ids))
        .all()
    )
    student_ids = [s.id for s in all_students]

    _log.info(
        '[attendance-shift] shift_id=%s "%s" date=%s active_students=%d ids=%s',
        shift.id, shift.name, target_date, len(student_ids), student_ids,
    )

    if not student_ids:
        _log.info(
            '[attendance-shift] skipped shift_id=%s — no active students in assigned sections',
            shift.id,
        )
        return {'count': 0}

    # ── 4. Students who already have any attendance row for target_date ────────
    already_ids = {
        row.student_id for row in
        StudentAttendance.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(date=target_date)
            .filter(StudentAttendance.student_id.in_(student_ids))
            .with_entities(StudentAttendance.student_id)
            .all()
    }

    for sid in already_ids:
        _log.debug(
            '[attendance-shift] skipped student_id=%s reason=already_has_attendance date=%s',
            sid, target_date,
        )

    unmarked = [s for s in all_students if s.id not in already_ids]

    _log.info(
        '[attendance-shift] shift_id=%s date=%s existing_attendance=%d missing_students=%d ids=%s',
        shift.id, target_date, len(already_ids), len(unmarked),
        [s.id for s in unmarked],
    )

    if not unmarked:
        _log.info('[attendance-shift] shift_id=%s date=%s — all students already have records',
                  shift.id, target_date)
        return {'count': 0}

    # ── 5. Create absent records ───────────────────────────────────────────────
    from sqlalchemy.exc import IntegrityError

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

    try:
        db.session.commit()
    except IntegrityError:
        # Another worker (or a concurrent web request) committed attendance for
        # one or more of these students between our query and our commit.
        # The UniqueConstraint on (student_id, date) protected the DB from
        # duplicates. Roll back, log, and skip notifications for this tick —
        # the records already exist, parents were already notified.
        db.session.rollback()
        _log.warning(
            '[attendance-shift] shift_id=%s "%s" date=%s — commit conflict '
            '(concurrent insert from another worker or request). '
            'Records already exist; skipping notifications for this tick.',
            shift.id, shift.name, target_date,
        )
        return {'count': 0}

    _log.info(
        '[attendance-shift] shift_id=%s "%s" date=%s absent_created=%d',
        shift.id, shift.name, target_date, len(unmarked),
    )

    # ── 6. Notify parents ─────────────────────────────────────────────────────
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


def _shift_covered_section_ids(school_id: int) -> set[int]:
    """
    Return the set of section IDs whose EFFECTIVE shift is an ACTIVE shift.

    Mirrors get_student_shift() resolution exactly:
      * section.shift_id points to an active shift, OR
      * section.shift_id IS NULL but the section's grade.shift_id points to an
        active shift (grade-level fallback).

    Sections pointing at an inactive shift are intentionally NOT covered — those
    students fall back to the school default settings, just like get_student_shift
    returns None for an inactive shift.
    """
    from app.models import AttendanceShift, Section, Grade

    active_shift_ids = {
        row.id for row in
        AttendanceShift.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school_id, is_active=True)
            .with_entities(AttendanceShift.id)
            .all()
    }
    if not active_shift_ids:
        return set()

    covered = {
        row.id for row in
        Section.query
            .execution_options(bypass_tenant_scope=True)
            .filter(Section.school_id == school_id,
                    Section.shift_id.in_(active_shift_ids))
            .with_entities(Section.id)
            .all()
    }
    grade_ids = {
        row.id for row in
        Grade.query
            .execution_options(bypass_tenant_scope=True)
            .filter(Grade.school_id == school_id,
                    Grade.shift_id.in_(active_shift_ids))
            .with_entities(Grade.id)
            .all()
    }
    if grade_ids:
        covered |= {
            row.id for row in
            Section.query
                .execution_options(bypass_tenant_scope=True)
                .filter(Section.school_id == school_id,
                        Section.grade_id.in_(grade_ids),
                        Section.shift_id.is_(None))
                .with_entities(Section.id)
                .all()
        }
    return covered


def _run_auto_absent_shiftless(school, year, settings, target_date,
                               now_time=None, force=False) -> dict:
    """
    Case 4 fallback: in SHIFT mode, mark absent the active students who have NO
    effective shift (neither a section shift nor a grade shift), using the school
    DEFAULT att_absence_threshold cutoff — exactly as normal mode would.

    Gated on the default cutoff having passed (unless force=True for past-day
    catch-up). Fully idempotent. Holiday is checked by the caller.
    """
    from app.models import db, Student, StudentAttendance
    from app.blueprints.attendance import _notify_absent_parents

    school_id = school.id
    cutoff = getattr(settings, 'att_absence_threshold', None)

    if not cutoff:
        _log.warning(
            '[attendance-shift-fallback] school_id=%s date=%s — no default cutoff '
            'configured (att_absence_threshold is NULL or 00:00), shiftless students skipped',
            school_id, target_date,
        )
        return {'count': 0}

    passed = True if force else (now_time is not None and now_time >= cutoff)
    _log.info(
        '[attendance-shift-fallback] school_id=%s date=%s default_cutoff=%s '
        'local_now=%s passed=%s force=%s',
        school_id, target_date, cutoff,
        now_time.strftime('%H:%M:%S') if now_time else None, passed, force,
    )
    if not passed:
        return {'count': 0}

    covered_section_ids = _shift_covered_section_ids(school_id)

    # Active students NOT covered by any active shift (incl. students with no section).
    q = (Student.query
         .execution_options(bypass_tenant_scope=True)
         .filter_by(status='active', school_id=school_id))
    if covered_section_ids:
        q = q.filter(db.or_(
            Student.section_id.is_(None),
            Student.section_id.notin_(covered_section_ids),
        ))
    shiftless = q.all()
    ids = [s.id for s in shiftless]

    _log.info(
        '[attendance-shift-fallback] school_id=%s date=%s covered_sections=%d '
        'active_shiftless_students=%d ids=%s',
        school_id, target_date, len(covered_section_ids), len(ids), ids,
    )

    if not ids:
        return {'count': 0}

    already_ids = {
        row.student_id for row in
        StudentAttendance.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(date=target_date)
            .filter(StudentAttendance.student_id.in_(ids))
            .with_entities(StudentAttendance.student_id)
            .all()
    }
    unmarked = [s for s in shiftless if s.id not in already_ids]

    _log.info(
        '[attendance-shift-fallback] school_id=%s date=%s students_with_attendance=%d '
        'missing_students=%d ids=%s',
        school_id, target_date, len(already_ids), len(unmarked),
        [s.id for s in unmarked],
    )

    if not unmarked:
        return {'count': 0}

    from sqlalchemy.exc import IntegrityError

    for student in unmarked:
        db.session.add(StudentAttendance(
            student_id       = student.id,
            school_id        = school_id,
            academic_year_id = year.id if year else None,
            date             = target_date,
            status           = 'absent',
            source           = 'automatic',
            shift_id         = None,
        ))

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        _log.warning(
            '[attendance-shift-fallback] school_id=%s date=%s — commit conflict '
            '(concurrent insert). Records already exist; skipping notifications.',
            school_id, target_date,
        )
        return {'count': 0}

    _log.info(
        '[attendance-shift-fallback] school_id=%s date=%s inserted_absences=%d',
        school_id, target_date, len(unmarked),
    )

    for student in unmarked:
        try:
            _notify_absent_parents(student, school_id, target_date.isoformat(),
                                   source='automatic')
        except Exception:
            _log.exception('[attendance-shift-fallback] notification failed student_id=%s',
                           student.id)

    return {'count': len(unmarked)}


def run_school_shift_auto_absent_now(school, year, settings) -> dict:
    """
    Public, web-triggerable shift-aware auto-absence for TODAY.

    Used by the attendance index page and the manual "mark absent today" button
    so those flows respect shift timings instead of the default cutoff. Mirrors
    the scheduler's per-tick shift logic (without the previous-day catch-up).

    Returns {'holiday': bool, 'count': int} where count is total absences created
    across all shifts plus the shiftless fallback.
    """
    from app.models import AttendanceShift
    from app.utils.attendance_helpers import (
        get_local_now, get_local_date, is_holiday_date,
    )

    local_now  = get_local_now(school)
    local_date = get_local_date(school)
    now_time   = local_now.time()

    if is_holiday_date(local_date, school.id, school):
        _log.info('[attendance-shift] web-trigger school_id=%s date=%s — holiday, skip',
                  school.id, local_date)
        return {'holiday': True, 'count': 0}

    active_shifts = (
        AttendanceShift.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(school_id=school.id, is_active=True)
        .all()
    )
    _log.info(
        '[attendance-shift] web-trigger school_id=%s active_shifts=%d local_now=%s',
        school.id, len(active_shifts), local_now.strftime('%H:%M:%S'),
    )

    total = 0
    for shift in active_shifts:
        passed = now_time >= shift.absent_after_time
        _log.info(
            '[attendance-shift] web-trigger school_id=%s shift_id=%s "%s" '
            'cutoff=%s local_now=%s passed=%s',
            school.id, shift.id, shift.name, shift.absent_after_time,
            now_time.strftime('%H:%M:%S'), passed,
        )
        if passed:
            result = _run_auto_absent_for_shift(school, year, shift, local_date)
            total += result.get('count', 0)

    # Case 4 — students without any shift use the default school settings.
    fallback = _run_auto_absent_shiftless(school, year, school, local_date,
                                          now_time=now_time)
    total += fallback.get('count', 0)

    return {'holiday': False, 'count': total}


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

    # Case 4 catch-up — shiftless students for yesterday (force: cutoff already passed).
    try:
        _run_auto_absent_shiftless(school, year, school, yesterday, force=True)
    except Exception:
        _log.exception('[attendance-shift] catch-up shiftless failed school_id=%s date=%s',
                       school.id, yesterday)


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

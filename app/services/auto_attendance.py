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

_log = logging.getLogger(__name__)
_scheduler_thread: threading.Thread | None = None


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
    from app.utils.decorators import get_active_year
    from app.blueprints.attendance import _run_auto_absent
    from app.utils.attendance_helpers import get_local_now, get_local_date

    school_name = getattr(school, 'name', f'school_{school.id}')
    cutoff = getattr(school, 'att_absence_threshold', None)

    if not cutoff:
        _log.info('[attendance] school_id=%s "%s" — no absence cutoff configured, skip',
                  school.id, school_name)
        return

    local_now  = get_local_now(school)
    local_date = get_local_date(school)
    now_time   = local_now.time()
    passed     = now_time >= cutoff

    _log.info(
        '[attendance] school_id=%s "%s" local_now=%s cutoff=%s passed=%s',
        school.id, school_name, local_now.strftime('%Y-%m-%d %H:%M:%S'), cutoff, passed,
    )

    if not passed:
        _log.info('[attendance] school_id=%s — absence time not yet reached, skip', school.id)
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
        # Should not normally happen since we already checked passed=True above,
        # but _run_auto_absent also checks internally with a fresh clock read.
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

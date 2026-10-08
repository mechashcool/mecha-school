"""
Background transport-trip auto-expiration.

An ACTIVE trip is ended automatically once no location update has been
accepted for INACTIVITY_LIMIT (2 h). Inactivity is measured from the server
receive time of the last accepted fix (TransportTrip.location_updated_at) or,
for a trip that never received one, from TransportTrip.started_at. Both are
naive UTC written by the server (app/blueprints/mobile_api/driver.py); the
device-supplied location_recorded_at is never used.

Checked every CHECK_INTERVAL_SECONDS (600 s), so a trip ends up to ~10 min
after its threshold. Nothing runs between checks.

One statement per cycle
───────────────────────
A single set-based conditional UPDATE … RETURNING across all schools, then
COMMIT. No trip is loaded into the ORM, there is no per-trip loop and no
join. RETURNING carries only (id, school_id) of the rows actually ended, for
the log line — it is part of the same statement.

The UPDATE reads and writes each row's OWN columns only: no school context,
no cross-row reference, so it cannot move or expose data between schools.
(The ORM tenant criteria in app/utils/scoping.py apply to SELECTs only.)

Concurrency (PostgreSQL READ COMMITTED row locks)
─────────────────────────────────────────────────
• Location ping vs. expiry — both are conditional UPDATEs on the same row.
  If the ping commits first, the expiry re-checks its WHERE against the new
  location_updated_at and skips the trip. If the expiry commits first, the
  ping's `status = 'active'` no longer matches → 409 trip_not_active.
• Manual end vs. expiry — the expiry only matches `status = 'active'`, so it
  never overwrites a trip that was ended manually. A manual end arriving
  after expiry takes the endpoint's existing idempotent path (no write).
• Several schedulers (WEB_CONCURRENCY > 1) — each row is ended by exactly one
  statement; the others match nothing. Only the polling is duplicated (one
  statement per process per cycle).

Attendance, notifications and stored coordinates are not touched: only
status and ended_at change.

Opt-out
───────
TRANSPORT_TRIP_EXPIRY_DISABLED=true — skip startup entirely.
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta

_log = logging.getLogger('mecha.transport_trip_expiry')
_scheduler_thread: threading.Thread | None = None

CHECK_INTERVAL_SECONDS = 600
INACTIVITY_LIMIT = timedelta(hours=2)


def start_transport_trip_expiry_scheduler(app) -> None:
    """Called once from create_app(). Safe to call multiple times."""
    if os.environ.get('TRANSPORT_TRIP_EXPIRY_DISABLED', '').lower() == 'true':
        _log.info('[transport-expiry] scheduler disabled (TRANSPORT_TRIP_EXPIRY_DISABLED=true)')
        return

    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return

    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        args=(app, CHECK_INTERVAL_SECONDS),
        daemon=True,
        name='transport-trip-expiry',
    )
    _scheduler_thread.start()
    _log.info('[transport-expiry] scheduler started (interval=%ds, inactivity=%s)',
              CHECK_INTERVAL_SECONDS, INACTIVITY_LIMIT)


def _scheduler_loop(app, interval: int) -> None:
    with app.app_context():
        while True:
            try:
                expire_inactive_trips()
            except Exception as exc:
                _log.error('[transport-expiry] check failed: %s', exc)
                try:
                    from app.models import db
                    db.session.rollback()
                except Exception:
                    pass
            finally:
                # Return the connection to the pool between checks (the
                # long-lived app context never runs teardown handlers).
                try:
                    from app.models import db
                    db.session.remove()
                except Exception:
                    pass
            time.sleep(interval)


def expire_inactive_trips(now: datetime | None = None) -> int:
    """End every active trip inactive since before now - INACTIVITY_LIMIT.

    ONE UPDATE … RETURNING + COMMIT. Idempotent: a second call ends nothing.
    Returns the number of trips ended.
    """
    from sqlalchemy import func, update
    from app.models import db, TransportTrip

    now = now or datetime.utcnow()
    cutoff = now - INACTIVITY_LIMIT
    rows = db.session.execute(
        update(TransportTrip)
        .where(TransportTrip.status == 'active',
               func.coalesce(TransportTrip.location_updated_at,
                             TransportTrip.started_at) <= cutoff)
        .values(status='ended', ended_at=now)
        .returning(TransportTrip.id, TransportTrip.school_id)
        .execution_options(synchronize_session=False)
    ).all()
    db.session.commit()

    if rows:
        _log.warning('[transport-expiry] auto-ended %d inactive trip(s) '
                     '(trip_id:school_id) %s', len(rows),
                     ', '.join(f'{r.id}:{r.school_id}' for r in rows))
    return len(rows)

# -*- coding: utf-8 -*-
"""Independent worker for the durable notification outbox.

Runs as its OWN process — never inside a Gunicorn request worker. That
separation is the point: Firebase latency must not touch an HTTP request or a
device WebSocket, and recycling or restarting the web service must not disturb
delivery.

Run it:

    python -m app.services.outbox_worker run
    python -m app.services.outbox_worker status
    python -m app.services.outbox_worker cleanup --days 30

A systemd template is tracked at deploy/mecha-school-outbox-worker.service.

Claim/send/settle loop
──────────────────────
Database locks are never held across network I/O:

  1. SHORT transaction: claim a bounded batch with FOR UPDATE SKIP LOCKED,
     stamp the lease, COMMIT.
  2. Outside any lock: send each job to Firebase.
  3. SHORT transaction per job: mark sent / retry / dead, COMMIT.

SKIP LOCKED is what makes a second worker safe — it steps over rows another
worker already holds rather than blocking on them. A worker killed between (1)
and (3) leaves its rows in 'processing'; once the lease expires any worker
reclaims them.

Failure handling
────────────────
  * permanent registration failure (the classification added in 5aa8dab)
    deactivates that ONE token and makes the row terminal — never retried;
  * transient failure retries with bounded exponential backoff plus jitter,
    until max attempts, then becomes a VISIBLE 'dead' row;
  * a malformed job is marked dead instead of crashing the loop;
  * a database outage backs off instead of busy-looping.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import socket
import sys
import threading
import time
from datetime import datetime

log = logging.getLogger('mecha.outbox.worker')

# ── Tunables (environment-overridable) ───────────────────────────────────────
DEFAULTS = {
    'batch_size':     int(os.environ.get('OUTBOX_BATCH_SIZE', 20)),
    'poll_seconds':   float(os.environ.get('OUTBOX_POLL_SECONDS', 5)),
    'lease_seconds':  int(os.environ.get('OUTBOX_LEASE_SECONDS', 300)),
    'max_attempts':   int(os.environ.get('OUTBOX_MAX_ATTEMPTS', 5)),
    'error_backoff':  float(os.environ.get('OUTBOX_ERROR_BACKOFF_SECONDS', 30)),
}

_stop = threading.Event()


def _install_signal_handlers() -> None:
    """SIGTERM/SIGINT ask the loop to finish its current batch and exit.

    systemd sends SIGTERM on stop/restart. Setting a flag (rather than dying
    mid-batch) means in-flight rows are settled instead of being left on an
    expired lease for the next worker to rediscover.
    """
    def _handle(signum, _frame):
        log.warning('[outbox] signal %s received — finishing current batch',
                    signum)
        _stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            pass        # not the main thread (e.g. under a test runner)


def worker_identity() -> str:
    return f'{socket.gethostname()}-{os.getpid()}'


# ═════════════════════════════════════════════════════════════════════════════
#  Delivering one claimed job
# ═════════════════════════════════════════════════════════════════════════════

def deliver_one(row, *, max_attempts: int, now=None) -> str:
    """Send ONE claimed job and settle it. Returns 'sent' | 'retry' | 'dead'.

    Runs with no database lock held. Never raises: a job that cannot be
    classified is settled as dead rather than being allowed to stop the loop.
    """
    from app.models import db, MobileDeviceToken
    from app.services import fcm_service
    from app.services import notification_outbox as outbox

    now = now or datetime.utcnow()

    try:
        token_row = db.session.get(
            MobileDeviceToken, row.device_token_id,
            execution_options={'bypass_tenant_scope': True})

        # The registration disappeared or was deactivated between enqueue and
        # now. Nothing to deliver to and nothing to retry: terminal, not an
        # error worth alerting on.
        if token_row is None or not token_row.is_active:
            outbox.mark_dead(row, error='device-token-inactive', now=now)
            db.session.commit()
            return 'dead'

        # Defence in depth: the job and the registration must agree on the
        # tenant. A mismatch means data was rewritten underneath us.
        if token_row.school_id != row.school_id:
            log.error('[outbox] job %s school mismatch — refusing to deliver',
                      row.id)
            outbox.mark_dead(row, error='school-mismatch', now=now)
            db.session.commit()
            return 'dead'

        try:
            data = json.loads(row.data_json) if row.data_json else {}
        except ValueError:
            data = {}

        result = fcm_service.send_to_device_token(
            token_row, row.title, row.body, data)

        if result.ok:
            outbox.mark_sent(row, message_id=result.message_id, now=now)
            _log_delivery_row(row, token_row, 'sent', result)
        elif result.permanent:
            # The token is dead. send_to_device_token already flipped that ONE
            # row to inactive (uncommitted); this commit persists it together
            # with the terminal job state.
            outbox.mark_dead(row, error=result.error or 'permanent', now=now)
            _log_delivery_row(row, token_row, 'failed', result)
        else:
            outbox.mark_retry(row, error=result.error or 'transient',
                              max_attempts=max_attempts, now=now)
            _log_delivery_row(row, token_row, 'failed', result)

        db.session.commit()
        return ('sent' if result.ok
                else 'dead' if row.status == 'dead'
                else 'retry')

    except Exception as exc:
        # Settling failed (or the job was malformed). Roll back, then try once
        # to record a retry so the row is not stranded in 'processing'. If even
        # that fails, the lease expires and another worker reclaims it.
        db.session.rollback()
        log.exception('[outbox] job %s failed to settle (%s)',
                      getattr(row, 'id', '?'), type(exc).__name__)
        try:
            fresh = db.session.get(
                type(row), row.id,
                execution_options={'bypass_tenant_scope': True})
            if fresh is not None:
                outbox.mark_retry(fresh, error=type(exc).__name__,
                                  max_attempts=max_attempts, now=now)
                db.session.commit()
        except Exception:
            db.session.rollback()
        return 'retry'


def _log_delivery_row(row, token_row, status, result) -> None:
    """Keep the existing PushNotification delivery log populated.

    The inline path writes one of these per attempt; the outbox path must not
    silently stop doing so, or the operational history would change shape the
    moment the flag is switched on. Added to the SAME transaction the caller is
    about to commit. Best effort: a logging failure must never fail a delivery.
    """
    try:
        from app.models import db, PushNotification
        db.session.add(PushNotification(
            user_id=row.user_id,
            school_id=row.school_id,
            title=row.title,
            body=row.body,
            data_json=row.data_json,
            ntype=row.ntype or 'attendance',
            status=status,
            fcm_message_id=result.message_id,
            error=result.error,
            sent_at=datetime.utcnow() if status == 'sent' else None,
        ))
    except Exception:
        log.warning('[outbox] could not write PushNotification log for job %s',
                    getattr(row, 'id', '?'))


# ═════════════════════════════════════════════════════════════════════════════
#  The loop
# ═════════════════════════════════════════════════════════════════════════════

def run_once(worker_id: str, *, batch_size: int, lease_seconds: int,
             max_attempts: int) -> dict:
    """One sweep: reclaim stale leases, claim a batch, deliver it.

    Returns a counts dict. Raises only on database-level failure, which the
    caller turns into a backoff.
    """
    from app.services import notification_outbox as outbox

    stats = {'reclaimed': 0, 'claimed': 0, 'sent': 0, 'retry': 0, 'dead': 0,
             'disabled': False}

    # Feature disabled: touch NOTHING. Not a claim, not a lease reclaim, not a
    # status change. A worker left running while the flag is off must be inert,
    # so installing the unit before enabling the feature is safe.
    if not outbox.enabled():
        stats['disabled'] = True
        return stats

    stats['reclaimed'] = outbox.reclaim_stale(
        worker_id, lease_seconds=lease_seconds)

    rows = outbox.claim_batch(worker_id, limit=batch_size)
    stats['claimed'] = len(rows)

    for row in rows:
        outcome = deliver_one(row, max_attempts=max_attempts)
        stats[outcome] = stats.get(outcome, 0) + 1

    return stats


def run(app=None, *, batch_size=None, poll_seconds=None, lease_seconds=None,
        max_attempts=None, max_loops=None) -> dict:
    """Run the worker loop until SIGTERM/SIGINT (or `max_loops` in tests)."""
    from app.services import notification_outbox as outbox

    batch_size = batch_size or DEFAULTS['batch_size']
    poll_seconds = poll_seconds or DEFAULTS['poll_seconds']
    lease_seconds = lease_seconds or DEFAULTS['lease_seconds']
    max_attempts = max_attempts or DEFAULTS['max_attempts']

    app = app or _build_app()
    worker_id = worker_identity()
    totals = {'loops': 0, 'reclaimed': 0, 'claimed': 0,
              'sent': 0, 'retry': 0, 'dead': 0}

    with app.app_context():
        if not outbox.enabled():
            # Not fatal: the worker may legitimately be started before the flag
            # is switched on. It idles instead of exiting so systemd does not
            # flap it in a restart loop.
            log.warning('[outbox] INSTITUTE_ATTENDANCE_OUTBOX_ENABLED is false '
                        '— worker will idle and deliver nothing')

        log.warning('[outbox] worker %s started  batch=%d poll=%ss lease=%ss '
                    'max_attempts=%d', worker_id, batch_size, poll_seconds,
                    lease_seconds, max_attempts)

        while not _stop.is_set():
            totals['loops'] += 1
            try:
                stats = run_once(worker_id, batch_size=batch_size,
                                 lease_seconds=lease_seconds,
                                 max_attempts=max_attempts)
                for key in ('reclaimed', 'claimed', 'sent', 'retry', 'dead'):
                    totals[key] += stats.get(key, 0)
                totals['disabled_ticks'] = totals.get('disabled_ticks', 0) + (
                    1 if stats.get('disabled') else 0)

                if stats['claimed'] or stats['reclaimed']:
                    log.info('[outbox] claimed=%d sent=%d retry=%d dead=%d '
                             'reclaimed=%d', stats['claimed'], stats['sent'],
                             stats['retry'], stats['dead'], stats['reclaimed'])

                # A full batch probably means more is waiting: loop straight
                # away instead of sleeping through a backlog.
                idle = stats['claimed'] < batch_size
            except Exception as exc:
                # Database unreachable or similar. Back off with jitter rather
                # than hammering it in a tight loop.
                log.error('[outbox] sweep failed (%s) — backing off %ss',
                          type(exc).__name__, DEFAULTS['error_backoff'])
                try:
                    from app.models import db
                    db.session.rollback()
                except Exception:
                    pass
                _stop.wait(DEFAULTS['error_backoff']
                           * (0.8 + 0.4 * random.random()))
                idle = True

            if max_loops is not None and totals['loops'] >= max_loops:
                break
            if idle:
                _stop.wait(poll_seconds)

        log.warning('[outbox] worker %s stopped  %s', worker_id, totals)
    return totals


# ═════════════════════════════════════════════════════════════════════════════
#  CLI
# ═════════════════════════════════════════════════════════════════════════════

def _build_app():
    """Construct the application for THIS process only.

    The role is declared before create_app() is imported or called, so the
    background-service gate in app/lifecycle.py refuses to start the
    attendance scheduler, the AI Face WebSocket receiver (port 7788), the
    Hikvision sync loop, the fee-reminder scheduler and the durable-push
    consumer in this process.

    This is the exact defect that was observed in production: started
    temporarily, the worker initialised the full application, the old
    argv-based check saw argv[1] == 'run', and the worker immediately ran an
    attendance scheduler tick across schools.

    The worker still needs the application context for its own database work
    and for FCM delivery — it just must not inherit the web server's
    background services.
    """
    from app.lifecycle import ROLE_OUTBOX_WORKER, set_role
    set_role(ROLE_OUTBOX_WORKER)

    from app import create_app
    return create_app(os.environ.get('FLASK_ENV', 'production'))


def _cmd_status(app=None) -> dict:
    """Read-only backlog report. Touches nothing."""
    from app.services import notification_outbox as outbox
    app = app or _build_app()
    with app.app_context():
        summary = outbox.status_summary()
    counts = summary['counts']
    print('notification_outbox')
    for status in ('pending', 'processing', 'retry', 'sent', 'dead', 'cancelled'):
        print(f'  {status:<11} {counts.get(status, 0)}')
    print(f'  {"backlog":<11} {summary["backlog"]}')
    age = summary['oldest_due_age_seconds']
    print(f'  oldest due  {age if age is not None else "-"}'
          f'{" s" if age is not None else ""}')
    return summary


def _cmd_cleanup(days: int, limit: int, app=None) -> int:
    from app.services import notification_outbox as outbox
    app = app or _build_app()
    with app.app_context():
        removed = outbox.cleanup_terminal(older_than_days=days, limit=limit)
    print(f'removed {removed} sent row(s) older than {days} day(s)')
    return removed


def main(argv=None) -> int:
    # Declared at the very top of the entry point, before any command parsing
    # or application construction, so every subcommand (run/status/cleanup)
    # runs under the outbox-worker role. _build_app() re-declares it, which is
    # an idempotent no-op — belt and braces for callers that import this
    # module and build the app themselves.
    from app.lifecycle import ROLE_OUTBOX_WORKER, set_role
    set_role(ROLE_OUTBOX_WORKER)

    logging.basicConfig(
        level=os.environ.get('OUTBOX_LOG_LEVEL', 'INFO'),
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    parser = argparse.ArgumentParser(prog='outbox_worker')
    sub = parser.add_subparsers(dest='command', required=True)

    run_p = sub.add_parser('run', help='process the outbox until stopped')
    run_p.add_argument('--batch-size', type=int, default=None)
    run_p.add_argument('--poll-seconds', type=float, default=None)
    run_p.add_argument('--lease-seconds', type=int, default=None)
    run_p.add_argument('--max-attempts', type=int, default=None)

    sub.add_parser('status', help='read-only backlog report')

    clean_p = sub.add_parser('cleanup', help='delete old SENT rows only')
    clean_p.add_argument('--days', type=int, default=30)
    clean_p.add_argument('--limit', type=int, default=1000)

    args = parser.parse_args(argv)

    if args.command == 'run':
        _install_signal_handlers()
        run(batch_size=args.batch_size, poll_seconds=args.poll_seconds,
            lease_seconds=args.lease_seconds, max_attempts=args.max_attempts)
    elif args.command == 'status':
        _cmd_status()
    elif args.command == 'cleanup':
        _cmd_cleanup(args.days, args.limit)
    return 0


if __name__ == '__main__':
    sys.exit(main())

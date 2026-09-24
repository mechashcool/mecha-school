# -*- coding: utf-8 -*-
"""Durable transactional outbox for push notifications.

Why this exists
───────────────
Institute attendance used to commit the attendance rows and then, still inside
the HTTP request, call Firebase once per parent device. Three consequences:

  * a crash between the commit and the send lost the notification silently;
  * a slow or hung Firebase held a Gunicorn thread (1 worker × 4 threads);
  * a failed send had nowhere to be retried from.

Here, the notification job is a DATABASE ROW written in the SAME transaction as
the attendance change. Either both exist or neither does. A separate worker
process (app/services/outbox_worker.py) claims rows and talks to Firebase.

Guarantee — stated precisely
────────────────────────────
Durable **at-least-once** processing with **deduplicated enqueueing**.

NOT exactly-once. A crash after Firebase accepts a message but before this
process marks the row sent will re-deliver it. Push notifications are
display-only, so a rare duplicate is the right trade against losing one.

Redis
─────
Not used. PostgreSQL is the source of truth and the worker sweeps it on a
timer. Production Redis has AOF disabled, so it could not back a durable queue
anyway, and a Redis wake-up would only reduce latency — never correctness. It
is a documented later optimisation, deliberately out of this phase.

Delivery granularity
────────────────────
One row per (event, parent user, device token). A parent with two phones gets
two rows, so one phone timing out never re-sends to the phone that succeeded,
and a permanent failure retires exactly one registration.

The token STRING is never stored here — only the FK to mobile_device_tokens.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from app.models import (
    db, InstituteStudyGroup, MobileDeviceToken, Notification,
    NotificationOutbox, Student, parent_students,
)

log = logging.getLogger('mecha.outbox')

OPTS = {'bypass_tenant_scope': True}

# Dedup keys are bounded by the column width (190). Comfortably under it.
_MAX_DEDUP_KEY = 190


# ═════════════════════════════════════════════════════════════════════════════
#  Feature flag
# ═════════════════════════════════════════════════════════════════════════════

def _flag(name: str) -> bool:
    """Read one boolean feature flag, config first then raw environment.

    Flask config is authoritative so tests and per-environment config work; the
    environment is the fallback for non-request contexts such as the worker
    process, which has an app context but may be started before config exists.
    Defaults to FALSE — an unset flag is never "on".
    """
    try:
        from flask import current_app
        if current_app:
            return bool(current_app.config.get(name, False))
    except Exception:
        pass
    import os
    return os.environ.get(name, 'false').strip().lower() == 'true'


def enabled() -> bool:
    """INSTITUTE attendance outbox. True only when explicitly switched on.

    Defaults to FALSE so deploying this code changes nothing: institute
    attendance keeps its existing inline behaviour, startup never touches the
    new table, and the deployment is safe even if the migration has not been
    applied yet.
    """
    return _flag('INSTITUTE_ATTENDANCE_OUTBOX_ENABLED')


def aiface_enabled() -> bool:
    """NORMAL SCHOOL / AI Face attendance outbox. Independent of enabled().

    Deliberately a separate flag with its own default of FALSE: institute and
    school attendance roll out separately, and neither flag may switch the
    other on. They share only the table and the worker.
    """
    return _flag('AIFACE_ATTENDANCE_OUTBOX_ENABLED')


def any_enabled() -> bool:
    """True when ANY producer is switched on — the worker's gate.

    The worker is generic over notification_outbox rows and does not care which
    feature produced one. It must run whenever work can exist, and stay
    completely inert when neither flag is set.
    """
    return enabled() or aiface_enabled()


# ═════════════════════════════════════════════════════════════════════════════
#  Enqueueing — runs INSIDE the caller's transaction
# ═════════════════════════════════════════════════════════════════════════════

def _dedup_key(event_type: str, session_id: int, student_id: int,
               user_id: int, token_id: int, transition_at: datetime) -> str:
    """A key that is stable for duplicates but distinct for real transitions.

    The problem: a static key of (session, student, parent, token) would be
    correct for a retry but WRONG for a legitimate re-transition. Institute
    attendance genuinely re-notifies when a record goes absent → present →
    absent, and a static key would silently swallow the second absence.

    So the key carries the transition instant, truncated to the second:

      * an identical re-submission never reaches here at all, because
        submit_attendance() classifies it as `unchanged` and newly_absent is
        empty — the existing semantics are the primary deduplication;
      * two concurrent identical submissions land in the same second and
        collide on the unique index, so only one job survives;
      * a genuine later absence, seconds or minutes afterwards, produces a
        different key and is correctly delivered.
    """
    stamp = transition_at.replace(microsecond=0).strftime('%Y%m%d%H%M%S')
    key = (f'{event_type}:{session_id}:{student_id}:'
           f'{user_id}:{token_id}:{stamp}')
    return key[:_MAX_DEDUP_KEY]


def stage_absence_deliveries(school, session, student_ids, *, now=None) -> int:
    """Stage in-app rows AND push jobs for newly-absent students.

    Adds to the CURRENT session and does NOT commit: the caller commits once,
    together with the attendance rows, so the whole thing is atomic. Any
    exception propagates so the caller can roll back — attendance must never
    commit without the notification work it promised.

    Returns the number of push jobs staged (0 is legitimate: a parent with no
    registered device still gets the in-app row).
    """
    if school is None or session is None:
        raise ValueError('outbox: school and session are required')
    student_ids = [int(s) for s in (student_ids or [])]
    if not student_ids:
        return 0

    now = now or datetime.utcnow()

    group = db.session.get(InstituteStudyGroup, session.group_id,
                           execution_options=OPTS)
    group_name = group.name if group else ''
    date_str = session.session_date.strftime('%Y-%m-%d')

    # Tenant guard: only students of THIS school, even if a caller passed a
    # forged id. Mirrors the filter submit_attendance already applies.
    students = (Student.query.execution_options(**OPTS)
                .filter(Student.id.in_(student_ids),
                        Student.school_id == school.id)
                .all())

    staged = 0
    for student in students:
        # Wording, ntype and payload keys are IDENTICAL to the inline path, so
        # the mobile router, badge counts and tap routing are unaffected.
        title = 'تنبيه غياب'
        body = (f'تم تسجيل الطالب {student.full_name} غائباً في '
                f'{group_name} بتاريخ {date_str}.')
        data = {
            'type':         'attendance',
            'ntype':        'attendance',
            'action':       'absent',
            'status':       'absent',
            'student_id':   str(student.id),
            'student_name': student.full_name,
            'date':         date_str,
            'screen':       'attendance',
            'institute_group_id':   str(session.group_id),
            'institute_group_name': group_name,
            'session_id':           str(session.id),
        }
        payload = json.dumps(data, ensure_ascii=False)

        parent_ids = [row[0] for row in
                      db.session.query(parent_students.c.user_id)
                      .filter(parent_students.c.student_id == student.id).all()]
        if not parent_ids:
            continue

        for parent_id in parent_ids:
            # The in-app feed row — same shape as the inline path.
            db.session.add(Notification(
                school_id=school.id, title=title, body=body,
                ntype='attendance', target_user_id=parent_id,
                created_by=None))

            # One job per ACTIVE registration of this parent, restricted to
            # this school so a token reassigned elsewhere is never targeted.
            tokens = (MobileDeviceToken.query.execution_options(**OPTS)
                      .filter(MobileDeviceToken.user_id == parent_id,
                              MobileDeviceToken.school_id == school.id,
                              MobileDeviceToken.is_active.is_(True))
                      .all())
            for tok in tokens:
                db.session.add(NotificationOutbox(
                    school_id=school.id,
                    event_type=NotificationOutbox.EVENT_INSTITUTE_ABSENCE,
                    user_id=parent_id,
                    device_token_id=tok.id,
                    title=title, body=body, data_json=payload,
                    ntype='attendance',
                    dedup_key=_dedup_key(
                        NotificationOutbox.EVENT_INSTITUTE_ABSENCE,
                        session.id, student.id, parent_id, tok.id, now),
                    status=NotificationOutbox.STATUS_PENDING,
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                ))
                staged += 1

    return staged


# ═════════════════════════════════════════════════════════════════════════════
#  Normal school / AI Face device scan
# ═════════════════════════════════════════════════════════════════════════════

def _scan_dedup_key(student_id: int, on_date, action: str,
                    user_id: int, token_id: int) -> str:
    """A key that is stable for a replayed device record and distinct per
    genuine transition.

    Unlike the institute key this carries NO timestamp, and it does not need
    one. `student_attendance` is unique on (student_id, date) — `uq_student_date`
    — and the engine allows at most one check_in and one check_out per row, so
    (student, date, action) identifies exactly one logical transition. A device
    that replays the same record, or the same record arriving through both
    sendlog and getnewlog, produces the same key and collides on the UNIQUE
    index instead of enqueueing twice.

    That is defence in depth, not the primary defence: the attendance engine
    already classifies a replay as `duplicate`/`already_checked_in`, so this
    function is normally not reached a second time at all.
    """
    key = (f'{NotificationOutbox.EVENT_SCHOOL_ATTENDANCE_SCAN}:'
           f'{student_id}:{on_date.strftime("%Y%m%d")}:{action}:'
           f'{user_id}:{token_id}')
    return key[:_MAX_DEDUP_KEY]


def stage_scan_deliveries(school, student, *, action, on_date,
                          title: str, body: str, data: dict,
                          now=None) -> int:
    """Stage push jobs for ONE attendance scan transition. Does NOT commit.

    Adds to the CURRENT session so the caller commits the jobs together with
    the StudentAttendance change: either both exist or neither does. Any
    exception propagates so the caller can roll back — attendance must never
    commit without the notification work it promised.

    Returns the number of jobs staged. 0 is legitimate and is NOT an error: a
    parent with no registered device, or a student with no linked parent, has
    nothing to deliver to. That mirrors the inline path, which also sends
    nothing in those cases.

    Deliberately creates no in-app `Notification` row, because the inline AI
    Face path never created one either — a scan is push-only and has never
    appeared in the parent feed. Adding one here would silently change the feed
    and the unread badge the moment the flag is switched on.
    """
    if school is None or student is None:
        raise ValueError('outbox: school and student are required')
    if action not in ('check_in', 'check_out'):
        raise ValueError(f'outbox: unsupported scan action {action!r}')

    # Fail closed on tenant mismatch. The device→student mapping is resolved by
    # device_id alone upstream, so this is the point at which the job's school
    # and the student's school are proven to agree. A mismatch means the
    # mapping data is wrong; refusing here rolls the attendance back rather
    # than delivering one school's scan to another school's parent.
    if student.school_id != school.id:
        raise ValueError(
            f'outbox: student {student.id} does not belong to school '
            f'{school.id} — refusing to stage a cross-school delivery')

    now = now or datetime.utcnow()
    payload = json.dumps(data or {}, ensure_ascii=False)

    parent_ids = [row[0] for row in
                  db.session.query(parent_students.c.user_id)
                  .filter(parent_students.c.student_id == student.id).all()]
    if not parent_ids:
        return 0

    staged = 0
    for parent_id in parent_ids:
        # One job per ACTIVE registration of this parent, restricted to this
        # school so a token reassigned elsewhere is never targeted.
        tokens = (MobileDeviceToken.query.execution_options(**OPTS)
                  .filter(MobileDeviceToken.user_id == parent_id,
                          MobileDeviceToken.school_id == school.id,
                          MobileDeviceToken.is_active.is_(True))
                  .all())
        for tok in tokens:
            db.session.add(NotificationOutbox(
                school_id=school.id,
                event_type=NotificationOutbox.EVENT_SCHOOL_ATTENDANCE_SCAN,
                user_id=parent_id,
                device_token_id=tok.id,
                title=title, body=body, data_json=payload,
                ntype='attendance',
                dedup_key=_scan_dedup_key(student.id, on_date, action,
                                          parent_id, tok.id),
                status=NotificationOutbox.STATUS_PENDING,
                attempts=0,
                next_attempt_at=now,
                created_at=now,
            ))
            staged += 1

    return staged


# ═════════════════════════════════════════════════════════════════════════════
#  Worker-side operations
# ═════════════════════════════════════════════════════════════════════════════

def claim_batch(worker_id: str, *, limit: int = 20, now=None) -> list:
    """Atomically claim up to `limit` due jobs. Commits and returns the rows.

    FOR UPDATE SKIP LOCKED is what makes two workers safe: each transaction
    locks the rows it selects and skips rows another worker already holds, so
    the same job can never be claimed twice. The claim transaction is SHORT and
    holds no lock while talking to Firebase.
    """
    now = now or datetime.utcnow()
    try:
        rows = (NotificationOutbox.query.execution_options(**OPTS)
                .filter(NotificationOutbox.status.in_(
                            NotificationOutbox.DUE_STATUSES),
                        db.or_(NotificationOutbox.next_attempt_at.is_(None),
                               NotificationOutbox.next_attempt_at <= now))
                .order_by(NotificationOutbox.next_attempt_at.asc().nullsfirst(),
                          NotificationOutbox.id.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
                .all())
        for row in rows:
            row.status = NotificationOutbox.STATUS_PROCESSING
            row.locked_by = worker_id[:80]
            row.locked_at = now
        db.session.commit()
        return rows
    except Exception:
        db.session.rollback()
        raise


def reclaim_stale(worker_id: str, *, lease_seconds: int = 300, now=None) -> int:
    """Return rows abandoned by a dead worker to the pending pool.

    A worker that is SIGKILLed leaves its rows in 'processing' with a lease
    timestamp and nothing to finish them. Once the lease is older than
    `lease_seconds` any worker may take them back.
    """
    now = now or datetime.utcnow()
    cutoff = now - timedelta(seconds=lease_seconds)
    try:
        rows = (NotificationOutbox.query.execution_options(**OPTS)
                .filter(NotificationOutbox.status
                        == NotificationOutbox.STATUS_PROCESSING,
                        NotificationOutbox.locked_at.isnot(None),
                        NotificationOutbox.locked_at < cutoff)
                .limit(500)
                .with_for_update(skip_locked=True)
                .all())
        for row in rows:
            row.status = NotificationOutbox.STATUS_PENDING
            row.locked_by = None
            row.locked_at = None
            row.next_attempt_at = now
        count = len(rows)
        db.session.commit()
        if count:
            log.warning('[outbox] reclaimed %d stale lease(s) older than %ss',
                        count, lease_seconds)
        return count
    except Exception:
        db.session.rollback()
        raise


def backoff_delay(attempts: int, *, base: int = 60, cap: int = 3600) -> float:
    """Bounded exponential backoff with jitter, in seconds.

    Jitter matters: without it every job queued during a Firebase outage would
    wake at the same instant and stampede it again on recovery.
    """
    import random
    delay = min(cap, base * (2 ** max(0, attempts - 1)))
    # Cap AFTER jitter, not before: +20% on an already-capped delay would push
    # the result past the ceiling the caller was promised.
    return min(cap, delay * (0.8 + 0.4 * random.random()))


def mark_sent(row, *, message_id=None, now=None) -> None:
    now = now or datetime.utcnow()
    row.status = NotificationOutbox.STATUS_SENT
    row.locked_by = None
    row.locked_at = None
    row.last_error = None
    row.processed_at = now
    row.completed_at = now
    row.next_attempt_at = None
    if message_id:
        row.last_error = None


def mark_retry(row, *, error=None, max_attempts=5, now=None) -> None:
    """Schedule another attempt, or give up visibly once attempts run out."""
    now = now or datetime.utcnow()
    row.attempts = (row.attempts or 0) + 1
    row.processed_at = now
    row.last_error = (error or '')[:200] or None
    row.locked_by = None
    row.locked_at = None
    if row.attempts >= max_attempts:
        # Terminal and VISIBLE — never silently dropped.
        row.status = NotificationOutbox.STATUS_DEAD
        row.completed_at = now
        row.next_attempt_at = None
        log.error('[outbox] job %s DEAD after %d attempts (%s)',
                  row.id, row.attempts, row.last_error)
    else:
        row.status = NotificationOutbox.STATUS_RETRY
        row.next_attempt_at = now + timedelta(
            seconds=backoff_delay(row.attempts))


def mark_dead(row, *, error=None, now=None) -> None:
    """Permanent failure — no further attempts."""
    now = now or datetime.utcnow()
    row.attempts = (row.attempts or 0) + 1
    row.status = NotificationOutbox.STATUS_DEAD
    row.locked_by = None
    row.locked_at = None
    row.last_error = (error or '')[:200] or None
    row.processed_at = now
    row.completed_at = now
    row.next_attempt_at = None


# ═════════════════════════════════════════════════════════════════════════════
#  Observability and retention
# ═════════════════════════════════════════════════════════════════════════════

def status_summary(now=None) -> dict:
    """Counts by status plus the age of the oldest due job. Read-only."""
    now = now or datetime.utcnow()
    counts = {s: 0 for s in NotificationOutbox.STATUSES}
    rows = (db.session.query(NotificationOutbox.status,
                             db.func.count(NotificationOutbox.id))
            .group_by(NotificationOutbox.status).all())
    for status, count in rows:
        counts[status] = count

    oldest = (db.session.query(db.func.min(NotificationOutbox.next_attempt_at))
              .filter(NotificationOutbox.status.in_(
                  NotificationOutbox.DUE_STATUSES),
                  db.or_(NotificationOutbox.next_attempt_at.is_(None),
                         NotificationOutbox.next_attempt_at <= now))
              .scalar())
    return {
        'counts': counts,
        'backlog': counts[NotificationOutbox.STATUS_PENDING]
                   + counts[NotificationOutbox.STATUS_RETRY],
        'oldest_due_at': oldest,
        'oldest_due_age_seconds': (
            int((now - oldest).total_seconds()) if oldest else None),
    }


def cleanup_terminal(older_than_days: int = 30, *, limit: int = 1000,
                     now=None) -> int:
    """Delete only SENT rows older than the retention window.

    Deliberately narrow. pending, retry, processing, dead and cancelled rows
    are never touched: 'dead' is the evidence an operator needs, and deleting
    pending work would be exactly the data loss this table exists to prevent.
    Bounded by `limit` so one run can never become a long lock.
    """
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=max(1, older_than_days))
    ids = [r[0] for r in
           db.session.query(NotificationOutbox.id)
           .filter(NotificationOutbox.status == NotificationOutbox.STATUS_SENT,
                   NotificationOutbox.completed_at.isnot(None),
                   NotificationOutbox.completed_at < cutoff)
           .limit(limit).all()]
    if not ids:
        return 0
    (NotificationOutbox.query.execution_options(**OPTS)
     .filter(NotificationOutbox.id.in_(ids))
     .delete(synchronize_session=False))
    db.session.commit()
    log.info('[outbox] retention removed %d sent row(s) older than %d days',
             len(ids), older_than_days)
    return len(ids)

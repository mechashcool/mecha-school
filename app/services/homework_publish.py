# -*- coding: utf-8 -*-
"""Delayed homework parent notifications — ONE durable fan-out.

The problem this closes: a homework was announced to parents the moment it
was saved, while the parent list hides it until its publish_date (school-local
date). With HOMEWORK_PUBLISH_DEFER_ENABLED a future homework is saved PENDING
(homework.parents_notified_at IS NULL) and nobody hears about it until it
publishes.

The marker
──────────
  NULL      parents not notified yet — the outbox worker's sweep will do it.
  a value   handled: notified, or deliberately skipped (logged).

The column's server default stamps every row the application does not
explicitly mark pending (existing rows, old code, the immediate path with the
flag off), so turning this on can never resend anything historical.

fan_out_homework() — the only writer of notifications here
──────────────────────────────────────────────────────────
ONE transaction:
  1. claim the homework row: SELECT … WHERE parents_notified_at IS NULL
     FOR UPDATE SKIP LOCKED — a concurrent claimer skips it;
  2. re-check eligibility (active, school active, current year, has a target,
     published on the school-local date, not past its due date);
  3. resolve recipients with the SAME rule as the immediate path (active
     students of the section / active group enrollments → this school's
     active linked parents, one per parent, lowest linked student_id);
  4. stage one in-app Notification per parent and one outbox push job per
     active device token (notification_outbox.stage_homework_deliveries);
  5. stamp parents_notified_at; COMMIT.
The marker, the in-app rows and the push jobs commit together or not at all.
Firebase is never called here — the existing outbox worker delivers.

Guarantees: database fan-out at most once per homework (the claim + marker);
push jobs deduplicated by the outbox's UNIQUE dedup_key; FCM delivery
AT-LEAST-ONCE (the outbox's contract — a crash after Firebase accepts but
before the job is marked sent can repeat one push). Never exactly-once.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import and_, null, or_
from sqlalchemy.orm import load_only

from app.models import db, AcademicYear, Homework, School, Subject
from app.services import notification_outbox as outbox
from app.utils.attendance_helpers import get_local_now

log = logging.getLogger('mecha.homework_publish')

OPTS = {'bypass_tenant_scope': True}

DEFAULT_SEND_HOUR = 7
DEFAULT_SWEEP_SECONDS = 60
MIN_SWEEP_SECONDS = 60
SWEEP_BATCH = 20

TITLE = 'واجب جديد'

# fan_out_homework() outcomes
STAGED = 'staged'            # rows + push jobs committed, marker stamped
SKIPPED = 'skipped'          # no longer eligible: marker stamped, nothing sent
NOT_PENDING = 'not_pending'  # already handled, deleted, or claimed elsewhere
NOT_DUE = 'not_due'          # not published yet (or before the send hour)
ERROR = 'error'              # rolled back; still pending, retried later


# ═════════════════════════════════════════════════════════════════════════════
#  Configuration — every value fails safe
# ═════════════════════════════════════════════════════════════════════════════

def defer_enabled() -> bool:
    return outbox.homework_defer_enabled()


def sweep_enabled() -> bool:
    return outbox.homework_sweep_enabled()


def _setting(name: str):
    try:
        from flask import current_app
        if current_app:
            return current_app.config.get(name)
    except Exception:
        pass
    return os.environ.get(name)


def send_hour() -> int:
    """School-local hour from which delayed notifications go out (0-23)."""
    try:
        hour = int(str(_setting('HOMEWORK_PUBLISH_SEND_HOUR')).strip())
    except (TypeError, ValueError):
        return DEFAULT_SEND_HOUR
    return hour if 0 <= hour <= 23 else DEFAULT_SEND_HOUR


def sweep_seconds() -> float:
    """Seconds between sweeps — never below MIN_SWEEP_SECONDS."""
    try:
        seconds = float(str(_setting('HOMEWORK_PUBLISH_SWEEP_SECONDS')).strip())
    except (TypeError, ValueError):
        return float(DEFAULT_SWEEP_SECONDS)
    return max(float(MIN_SWEEP_SECONDS), seconds)


def pending_marker_kwargs() -> dict:
    """Homework(**…) kwargs for a NEW row.

    DEFER on → an explicit SQL NULL (pending). The ORM drops a plain None for
    a column with a server default and the default would stamp the row as
    handled, so null() is required, not None.
    DEFER off → nothing: the server default stamps the row handled, and the
    original immediate notification path runs.
    """
    return {'parents_notified_at': null()} if defer_enabled() else {}


def notification_body(subject_name, title) -> str:
    """The unchanged homework notification text."""
    return f'تم إضافة واجب جديد في مادة {subject_name or "غير محدد"}: {title}'


# ═════════════════════════════════════════════════════════════════════════════
#  Fan-out
# ═════════════════════════════════════════════════════════════════════════════

def _clock(school):
    return SimpleNamespace(timezone=getattr(school, 'timezone', None))


def _current_year_id(school_id: int):
    return (db.session.query(AcademicYear.id)
            .execution_options(**OPTS)
            .filter(AcademicYear.school_id == school_id,
                    AcademicYear.is_current.is_(True))
            .scalar())


def _ineligible_reason(hw, school, local_today) -> str | None:
    if not hw.is_active:
        return 'homework-inactive'
    if school is None or not school.is_active:
        return 'school-inactive'
    if hw.academic_year_id != _current_year_id(hw.school_id):
        return 'not-current-academic-year'
    if not hw.section_id and not hw.institute_group_id:
        return 'no-target'
    if hw.due_date and hw.due_date < local_today:
        return 'expired'
    return None


def fan_out_homework(homework_id: int, *, now=None, enforce_send_hour=False,
                     ignore_schedule=False, created_by=None) -> str:
    """Notify the parents of ONE pending homework, durably. Never raises.

    enforce_send_hour  the sweep: nothing before HOMEWORK_PUBLISH_SEND_HOUR
                       school-local time.
    ignore_schedule    rollback flush only: notify even if not published yet
                       (the pre-fix behaviour).
    created_by         Notification.created_by (the author on the immediate
                       path; None for the worker).
    """
    from app.blueprints.mobile_api.teacher import (
        _active_audience_student_ids, _linked_parent_recipients,
        institute_homework_fcm_data)

    now = now or datetime.utcnow()
    try:
        hw = (Homework.query.execution_options(**OPTS)
              .options(load_only(
                  Homework.id, Homework.school_id, Homework.academic_year_id,
                  Homework.subject_id, Homework.section_id,
                  Homework.institute_group_id, Homework.title,
                  Homework.publish_date, Homework.due_date, Homework.is_active,
                  Homework.parents_notified_at))
              .filter(Homework.id == homework_id,
                      Homework.parents_notified_at.is_(None))
              .with_for_update(skip_locked=True)
              .first())
        if hw is None:
            db.session.rollback()
            return NOT_PENDING

        school = (db.session.query(School.id, School.is_active, School.timezone)
                  .execution_options(**OPTS)
                  .filter(School.id == hw.school_id)
                  .first())
        local_now = get_local_now(_clock(school))
        local_today = local_now.date()
        if not ignore_schedule and (
                hw.publish_date > local_today
                or (enforce_send_hour and local_now.hour < send_hour())):
            db.session.rollback()
            return NOT_DUE

        reason = _ineligible_reason(hw, school, local_today)
        if reason:
            hw.parents_notified_at = now
            db.session.commit()
            log.warning('[homework-publish] hw_id=%s school_id=%s skipped (%s) '
                        '— marked handled, nothing sent', homework_id,
                        hw.school_id, reason)
            return SKIPPED

        school_id = hw.school_id
        tag = f'[homework-publish] hw_id={homework_id}'
        users, parent_to_student = _linked_parent_recipients(
            school_id,
            _active_audience_student_ids(school_id, hw.section_id,
                                         hw.institute_group_id),
            active_parents_only=True, log_tag=tag)

        subject_name = (db.session.query(Subject.name)
                        .execution_options(**OPTS)
                        .filter(Subject.id == hw.subject_id,
                                Subject.school_id == school_id)
                        .scalar()) if hw.subject_id else None
        if hw.institute_group_id:
            payload = institute_homework_fcm_data(hw.id, hw.institute_group_id)
        else:
            payload = {
                'type':        'homework',
                'ntype':       'homework',
                'route':       '/parent/homework',
                'homework_id': str(hw.id),
                'section_id':  str(hw.section_id),
                'screen':      'homework',
            }

        rows, jobs = outbox.stage_homework_deliveries(
            school_id=school_id, homework_id=hw.id, title=TITLE,
            body=notification_body(subject_name, hw.title), payload=payload,
            recipients=[(users[uid], parent_to_student[uid])
                        for uid in sorted(users)],
            created_by=created_by, now=now)
        hw.parents_notified_at = now
        db.session.commit()
        log.warning('%s school_id=%s in_app=%d push_jobs=%d', tag, school_id,
                    rows, jobs)
        return STAGED
    except Exception:
        db.session.rollback()
        log.exception('[homework-publish] fan-out FAILED hw_id=%s — rolled back, '
                      'still pending (retried by the sweep)', homework_id)
        return ERROR


def notify_after_commit(homework_ids, *, created_by=None) -> dict:
    """DEFER mode, after the homework commit (create or edit): fan out every
    id already published on the school-local date. A future one stays pending
    for the sweep; a failure is logged and stays pending, and the sweep
    recovers it from the next run at/after the send hour. Never raises."""
    outcomes: dict = {}
    for hid in homework_ids:
        outcome = fan_out_homework(hid, created_by=created_by)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return outcomes


# ═════════════════════════════════════════════════════════════════════════════
#  Sweep — runs inside the existing outbox worker
# ═════════════════════════════════════════════════════════════════════════════

def _pending_q(*columns):
    return (db.session.query(*columns)
            .execution_options(**OPTS)
            .filter(Homework.parents_notified_at.is_(None),
                    Homework.is_active.is_(True)))


def sweep_due(*, now=None, limit: int = SWEEP_BATCH) -> dict:
    """Notify up to `limit` pending homework that are due now.

    Due = published on the school-local date AND the school-local time is at
    or after the send hour. Anything older is due too, so a missed run (worker
    stopped, VPS offline) is caught up on the next run.

    Cost: ONE query on the partial index when nothing is pending (the normal
    case); otherwise one timezone query, one bounded id query, then one short
    transaction per homework.
    """
    now = now or datetime.utcnow()
    stats = {'candidates': 0, STAGED: 0, SKIPPED: 0, NOT_PENDING: 0,
             NOT_DUE: 0, ERROR: 0}
    # No school-local date can be later than the UTC date + 1.
    horizon = now.date() + timedelta(days=1)
    school_ids = [r[0] for r in
                  _pending_q(Homework.school_id)
                  .filter(Homework.publish_date <= horizon)
                  .distinct().all()]
    if not school_ids:
        db.session.rollback()
        return stats

    hour = send_hour()
    due = []
    for sid, tz in (db.session.query(School.id, School.timezone)
                    .execution_options(**OPTS)
                    .filter(School.id.in_(school_ids)).all()):
        local_now = get_local_now(SimpleNamespace(timezone=tz))
        if local_now.hour >= hour:
            due.append(and_(Homework.school_id == sid,
                            Homework.publish_date <= local_now.date()))
    ids = ([r[0] for r in _pending_q(Homework.id).filter(or_(*due))
            .order_by(Homework.publish_date.asc(), Homework.id.asc())
            .limit(limit).all()] if due else [])
    db.session.rollback()          # end the read; each fan-out is its own tx

    stats['candidates'] = len(ids)
    for hid in ids:
        outcome = fan_out_homework(hid, now=now, enforce_send_hour=True)
        stats[outcome] += 1
    if ids:
        log.warning('[homework-publish] sweep %s', stats)
    return stats


# ═════════════════════════════════════════════════════════════════════════════
#  Operator commands (outbox_worker homework-status / homework-flush)
# ═════════════════════════════════════════════════════════════════════════════

def status_summary(now=None) -> dict:
    """Read-only: pending homework counts. Touches nothing."""
    now = now or datetime.utcnow()
    pending = _pending_q(Homework.id).count()
    past = (_pending_q(Homework.id)
            .filter(Homework.publish_date < now.date()).count())
    oldest = (_pending_q(Homework.publish_date)
              .order_by(Homework.publish_date.asc()).limit(1).scalar())
    db.session.rollback()
    return {'pending': pending, 'pending_publish_date_before_utc_today': past,
            'oldest_pending_publish_date': oldest}


def flush_pending(*, now=None, batch: int = 50) -> dict:
    """ROLLBACK AID: notify every pending homework NOW, published or not —
    the pre-fix behaviour — so nothing is left pending before the feature is
    removed. Eligibility (active, school, year, target, due date) still
    applies. Bounded batches; each homework is its own transaction."""
    now = now or datetime.utcnow()
    totals = {STAGED: 0, SKIPPED: 0, NOT_PENDING: 0, NOT_DUE: 0, ERROR: 0}
    seen: set = set()
    while True:
        q = _pending_q(Homework.id)
        if seen:
            q = q.filter(Homework.id.notin_(seen))
        ids = [r[0] for r in q.order_by(Homework.id.asc()).limit(batch).all()]
        db.session.rollback()
        if not ids:
            return totals
        for hid in ids:
            seen.add(hid)
            totals[fan_out_homework(hid, now=now, ignore_schedule=True)] += 1

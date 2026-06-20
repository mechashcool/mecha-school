"""
Background fee-installment reminder scheduler — recurring daily workflow.

Configuration (per school, stored on the School model):
  fee_reminder_enabled     : bool — master on/off switch
  fee_reminder_days_before : int  — days before due date to begin sending reminders
  fee_reminder_per_day     : int  — how many reminder slots to send each day (1–6)

Reminder window
───────────────
An installment enters its reminder window when:
    installment.due_date <= local_school_today + fee_reminder_days_before

Reminders continue daily — including days after the due date — until the
installment reaches status 'paid' (remaining balance = 0).  A partial payment
reduces the displayed amount but does NOT stop reminders.

Daily time slots
────────────────
  1/day → [09:00]
  2/day → [09:00, 17:00]                       (~8 h apart)
  3/day → [09:00, 13:00, 18:00]
  4/day → [08:00, 11:00, 14:00, 17:00]
  5/day → [08:00, 10:30, 13:00, 15:30, 18:00]
  6/day → [08:00, 10:00, 12:00, 14:00, 16:00, 18:00]

Each slot fires within a 60-minute window after its scheduled time.
The school's configured timezone is used for all time comparisons.

Duplicate prevention
────────────────────
FeeReminderLog has a UNIQUE constraint on
  (installment_id, parent_user_id, reminder_date, slot_index).
An optimistic pre-check avoids unnecessary DB writes; an IntegrityError
catch handles the rare concurrent-scheduler race condition.

Isolation guarantees
────────────────────
• Only parents whose school_id matches the school being processed are
  notified — cross-school delivery is structurally impossible.
• Only active (is_active=True) parent accounts receive notifications.
• A push-delivery failure never rolls back the in-app notification or log.

Opt-out
───────
FEE_REMINDER_SCHEDULER_DISABLED=true  — skip startup entirely.
FEE_REMINDER_CHECK_INTERVAL           — seconds between ticks (default/min 300).
"""
import logging
import os
import threading
import time
from datetime import date as _date, datetime, timedelta

_log = logging.getLogger('mecha.fee_reminder')
_scheduler_thread: threading.Thread | None = None

# Each slot fires within this many minutes after its scheduled time.
_SLOT_WINDOW_MINUTES = 60


# ─── Daily slot tables ────────────────────────────────────────────────────────

_SLOT_TIMES: dict[int, list[tuple[int, int]]] = {
    1: [(9, 0)],
    2: [(9, 0), (17, 0)],
    3: [(9, 0), (13, 0), (18, 0)],
    4: [(8, 0), (11, 0), (14, 0), (17, 0)],
    5: [(8, 0), (10, 30), (13, 0), (15, 30), (18, 0)],
    6: [(8, 0), (10, 0), (12, 0), (14, 0), (16, 0), (18, 0)],
}


def _get_daily_slots(per_day: int) -> list[tuple[int, int]]:
    """Return list of (hour, minute) for each daily reminder slot."""
    per_day = max(1, min(per_day, 6))
    return _SLOT_TIMES[per_day]


def _active_slot_index(slots: list[tuple[int, int]], local_now: datetime) -> int:
    """Return 0-based index of the currently active slot, or -1 if none is firing."""
    today = local_now.date()
    for idx, (h, m) in enumerate(slots):
        slot_dt = local_now.replace(
            year=today.year, month=today.month, day=today.day,
            hour=h, minute=m, second=0, microsecond=0,
        )
        if slot_dt <= local_now < slot_dt + timedelta(minutes=_SLOT_WINDOW_MINUTES):
            return idx
    return -1


# ─── Public entry point ───────────────────────────────────────────────────────

def start_fee_reminder_scheduler(app) -> None:
    """Called once from create_app(). Safe to call multiple times."""
    if os.environ.get('FEE_REMINDER_SCHEDULER_DISABLED', '').lower() == 'true':
        _log.info('[fees-reminder] scheduler disabled (FEE_REMINDER_SCHEDULER_DISABLED=true)')
        return

    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return

    interval = max(300, int(os.environ.get('FEE_REMINDER_CHECK_INTERVAL', '300')))
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        args=(app, interval),
        daemon=True,
        name='fee-reminder-scheduler',
    )
    _scheduler_thread.start()
    _log.info('[fees-reminder] scheduler started (interval=%ds)', interval)


# ─── Internal loop ────────────────────────────────────────────────────────────

def _scheduler_loop(app, interval: int) -> None:
    with app.app_context():
        while True:
            try:
                _run_check()
            except Exception as exc:
                _log.error('[fees-reminder] scheduler loop error: %s', exc)
                try:
                    from app.models import db
                    db.session.rollback()
                except Exception:
                    pass
            finally:
                # Return the connection to the pool between ticks.
                # The long-lived app_context() doesn't fire teardown handlers,
                # so we remove the scoped session manually.
                try:
                    from app.models import db
                    db.session.remove()
                except Exception:
                    pass
            time.sleep(interval)


def _run_check() -> None:
    from app.models import School

    all_schools = (
        School.query
        .execution_options(bypass_tenant_scope=True)
        .all()
    )

    _log.debug('[fees-reminder] tick — %d school(s) to evaluate', len(all_schools))

    total_sent = total_skipped = 0
    for school in all_schools:
        try:
            sent, skipped = _check_school(school)
            total_sent    += sent
            total_skipped += skipped
        except Exception as exc:
            _log.error('[fees-reminder] error school_id=%s: %s', school.id, exc)
            try:
                from app.models import db
                db.session.rollback()
            except Exception:
                pass

    if total_sent:
        _log.info('[fees-reminder] tick complete — sent=%d skipped=%d',
                  total_sent, total_skipped)
    else:
        _log.debug('[fees-reminder] tick complete — sent=0 skipped=%d', total_skipped)


def _check_school(school) -> tuple[int, int]:
    from app.models import (
        db, FeeInstallment, FeeRecord, FeeReminderLog,
        Notification, parent_students, Student, User,
    )
    from app.utils.attendance_helpers import get_local_now
    from sqlalchemy.exc import IntegrityError

    enabled = getattr(school, 'fee_reminder_enabled', False)
    if not enabled:
        return 0, 0

    days_before = max(1, int(getattr(school, 'fee_reminder_days_before', None) or 3))
    per_day     = max(1, min(int(getattr(school, 'fee_reminder_per_day', None) or 1), 6))

    local_now = get_local_now(school)
    today     = local_now.date()
    slots     = _get_daily_slots(per_day)
    slot_idx  = _active_slot_index(slots, local_now)

    if slot_idx < 0:
        # Not within any slot firing window right now.
        return 0, 0

    # Query installments that are inside the reminder window AND still unpaid/partial.
    # due_date <= today + days_before  means the window has opened (or due date has passed).
    cutoff_date = today + timedelta(days=days_before)

    installments = (
        FeeInstallment.query
        .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
        .filter(
            FeeInstallment.school_id == school.id,
            FeeInstallment.due_date  <= cutoff_date,
            FeeInstallment.status.in_(['pending', 'partial', 'overdue']),
        )
        .all()
    )

    _log.debug(
        '[fees-reminder] school_id=%s date=%s slot=%d/%d days_before=%d '
        'installments_in_window=%d',
        school.id, today, slot_idx, len(slots) - 1, days_before, len(installments),
    )

    sent = skipped = 0

    for inst in installments:
        # Double-check balance — status field may lag behind a payment.
        paid      = float(inst.received_amount or 0)
        remaining = float(inst.amount or 0) - paid
        if remaining <= 0:
            skipped += 1
            continue

        fee_record = (
            FeeRecord.query
            .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
            .get(inst.fee_record_id)
        )
        if not fee_record:
            continue

        student = (
            Student.query
            .execution_options(bypass_tenant_scope=True)
            .get(fee_record.student_id)
        )
        # Hard school-isolation guard: student must belong to this school.
        if not student or student.school_id != school.id:
            continue

        # Collect parent user IDs linked to this student.
        parent_ids = [
            row[0] for row in
            db.session.query(parent_students.c.user_id)
            .filter(parent_students.c.student_id == student.id)
            .all()
        ]
        if not parent_ids:
            _log.debug('[fees-reminder] installment_id=%s — no linked parents, skip', inst.id)
            continue

        # Fetch only active parents that belong to this school.
        # This prevents cross-school notification regardless of parent_students content.
        active_parents = (
            User.query
            .execution_options(bypass_tenant_scope=True)
            .filter(
                User.id.in_(parent_ids),
                User.school_id == school.id,
                User.is_active.is_(True),
            )
            .all()
        )
        if not active_parents:
            continue

        is_overdue   = inst.due_date < today
        status_label = 'متأخر السداد' if is_overdue else 'قادم'
        due_str      = inst.due_date.strftime('%Y-%m-%d')
        inst_no      = inst.installment_no or '–'

        title = 'تذكير بموعد القسط'
        body  = (
            f"الطالب: {student.full_name} | قسط رقم {inst_no} | {status_label}\n"
            f"المبلغ: {inst.amount:,.0f} | مدفوع: {paid:,.0f} | "
            f"متبقي: {remaining:,.0f} | استحقاق: {due_str}"
        )
        data = {
            'type':           'fee_reminder',
            'student_id':     str(student.id),
            'installment_id': str(inst.id),
            'due_date':       due_str,
            'screen':         'fees',
        }

        for parent in active_parents:
            # Optimistic pre-check (avoids a DB write on the common already-sent path).
            already = (
                FeeReminderLog.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(
                    installment_id=inst.id,
                    parent_user_id=parent.id,
                    reminder_date=today,
                    slot_index=slot_idx,
                )
                .first()
            )
            if already:
                skipped += 1
                continue

            try:
                db.session.add(Notification(
                    school_id      = school.id,
                    title          = title,
                    body           = body,
                    ntype          = 'fee_reminder',
                    target_user_id = parent.id,
                    created_by     = None,
                ))
                db.session.add(FeeReminderLog(
                    school_id        = school.id,
                    academic_year_id = inst.academic_year_id,
                    student_id       = student.id,
                    installment_id   = inst.id,
                    parent_user_id   = parent.id,
                    reminder_date    = today,
                    slot_index       = slot_idx,
                    due_date         = inst.due_date,
                ))
                db.session.commit()

                # FCM push — failure must not roll back the notification/log.
                try:
                    from app.services.fcm_service import is_enabled, send_push_to_user
                    if is_enabled():
                        send_push_to_user(parent.id, title, body, data)
                except Exception as fcm_exc:
                    _log.error(
                        '[fees-reminder] FCM push failed parent_id=%s installment_id=%s: %s',
                        parent.id, inst.id, fcm_exc,
                    )

                sent += 1
                _log.info(
                    '[fees-reminder] sent school=%s student=%s inst=%s '
                    'parent=%s slot=%d/%d overdue=%s remaining=%.0f',
                    school.id, student.id, inst.id,
                    parent.id, slot_idx, len(slots) - 1,
                    is_overdue, remaining,
                )

            except IntegrityError:
                # Concurrent scheduler process already wrote this slot — safe to skip.
                db.session.rollback()
                skipped += 1

            except Exception as exc:
                _log.error(
                    '[fees-reminder] error parent_id=%s installment_id=%s: %s',
                    parent.id, inst.id, exc,
                )
                db.session.rollback()

    return sent, skipped

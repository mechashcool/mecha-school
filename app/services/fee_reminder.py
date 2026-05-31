"""
Background fee-installment reminder scheduler.

Runs every FEE_REMINDER_CHECK_INTERVAL seconds (default 3600 = 1 hour).
For each active school with fee_reminder_enabled=True, finds unpaid/partial
installments whose due date falls within the configured advance window and
sends push + in-app reminders to every linked parent.

Duplicate prevention
────────────────────
FeeReminderLog has a UNIQUE constraint on
(installment_id, parent_user_id, reminder_value, reminder_unit).
Each (parent × installment × window) is therefore sent exactly once,
even when the scheduler ticks multiple times before the due date.

Opt-out
───────
Set FEE_REMINDER_SCHEDULER_DISABLED=true in the environment to skip startup.
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta

_log = logging.getLogger('mecha.fee_reminder')
_scheduler_thread: threading.Thread | None = None


# ─── Public entry point ───────────────────────────────────────────────────────

def start_fee_reminder_scheduler(app) -> None:
    """Called once from create_app(). Safe to call multiple times."""
    if os.environ.get('FEE_REMINDER_SCHEDULER_DISABLED', '').lower() == 'true':
        app.logger.warning(
            '[fees-reminder] scheduler disabled via FEE_REMINDER_SCHEDULER_DISABLED')
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
    app.logger.warning('[fees-reminder] scheduler started (interval=%ds)', interval)


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
                # Return the DB connection to the pool between ticks.
                # The long-lived app_context() doesn't trigger teardown_appcontext
                # handlers until it exits, so we must remove the scoped session
                # manually to avoid holding a connection during time.sleep().
                try:
                    from app.models import db
                    db.session.remove()
                except Exception:
                    pass
            time.sleep(interval)


def _run_check() -> None:
    from app.models import School

    _log.warning('[fees-reminder] scheduler tick — loading all schools')

    all_schools = (
        School.query
        .execution_options(bypass_tenant_scope=True)
        .all()
    )

    _log.warning('[fees-reminder] total schools in db: %d', len(all_schools))
    for s in all_schools:
        _log.warning(
            '[fees-reminder] school id=%s name=%r is_active=%s fee_reminder_enabled=%s',
            s.id, s.school_name, s.is_active, getattr(s, 'fee_reminder_enabled', None),
        )

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

    _log.warning('[fees-reminder] completed — total sent=%d skipped=%d',
                 total_sent, total_skipped)


def _check_school(school) -> tuple[int, int]:
    from app.models import (
        db, FeeInstallment, FeeRecord, FeeReminderLog,
        Notification, parent_students, Student,
    )
    from app.utils.attendance_helpers import get_local_now

    enabled = getattr(school, 'fee_reminder_enabled', False)
    value   = int(getattr(school, 'fee_reminder_before_value', None) or 3)
    unit    = getattr(school, 'fee_reminder_before_unit',  None) or 'days'

    _log.warning('[fees-reminder] checking school_id=%s enabled=%s value=%s unit=%s',
                 school.id, enabled, value, unit)

    if not enabled:
        return 0, 0

    # Compute which due date we want to alert on
    local_now = get_local_now(school)
    if unit == 'days':
        target_date = (local_now + timedelta(days=value)).date()
    elif unit == 'hours':
        target_date = (local_now + timedelta(hours=value)).date()
    else:  # minutes
        target_date = (local_now + timedelta(minutes=value)).date()

    _log.warning('[fees-reminder] school_id=%s target_window=%s', school.id, target_date)

    installments = (
        FeeInstallment.query
        .execution_options(bypass_tenant_scope=True, bypass_year_scope=True)
        .filter(
            FeeInstallment.school_id == school.id,
            FeeInstallment.due_date  == target_date,
            FeeInstallment.status.in_(['pending', 'partial']),
        )
        .all()
    )

    _log.warning('[fees-reminder] school_id=%s found %d installment(s) due on %s',
                 school.id, len(installments), target_date)

    sent = skipped = 0

    for inst in installments:
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
        if not student:
            continue

        parent_ids = [
            row[0] for row in
            db.session.query(parent_students.c.user_id)
            .filter(parent_students.c.student_id == student.id)
            .all()
        ]

        if not parent_ids:
            _log.warning('[fees-reminder] installment_id=%s student_id=%s — no linked parents, skip',
                         inst.id, student.id)
            continue

        for parent_id in parent_ids:
            # Duplicate guard
            already_sent = (
                FeeReminderLog.query
                .filter_by(
                    installment_id=inst.id,
                    parent_user_id=parent_id,
                    reminder_value=value,
                    reminder_unit=unit,
                )
                .first()
            )
            if already_sent:
                _log.warning(
                    '[fees-reminder] skipped duplicate student_id=%s installment_id=%s parent_id=%s',
                    student.id, inst.id, parent_id)
                skipped += 1
                continue

            # Build notification text
            due_str = inst.due_date.strftime('%Y-%m-%d')
            title   = 'تذكير بموعد القسط'
            if unit == 'days':
                unit_word = 'يوم' if value == 1 else 'أيام'
                body = (
                    f'نود تذكيركم بأن موعد تسديد القسط للطالب {student.full_name} '
                    f'سيكون بعد {value} {unit_word} بتاريخ {due_str}.'
                )
            elif unit == 'hours':
                body = (
                    f'نود تذكيركم بأن موعد تسديد القسط للطالب {student.full_name} '
                    f'سيكون بعد {value} ساعة بتاريخ {due_str}.'
                )
            else:  # minutes
                body = (
                    f'نود تذكيركم بأن موعد تسديد القسط للطالب {student.full_name} '
                    f'سيكون بعد {value} دقيقة بتاريخ {due_str}.'
                )

            data = {
                'type':           'fee_reminder',
                'student_id':     str(student.id),
                'installment_id': str(inst.id),
                'due_date':       due_str,
                'screen':         'fees',
            }

            try:
                db.session.add(Notification(
                    school_id      = school.id,
                    title          = title,
                    body           = body,
                    ntype          = 'fee_reminder',
                    target_user_id = parent_id,
                    created_by     = None,
                ))
                db.session.add(FeeReminderLog(
                    school_id        = school.id,
                    academic_year_id = inst.academic_year_id,
                    student_id       = student.id,
                    installment_id   = inst.id,
                    parent_user_id   = parent_id,
                    reminder_value   = value,
                    reminder_unit    = unit,
                    due_date         = inst.due_date,
                ))
                db.session.commit()

                # FCM push
                try:
                    from app.services.fcm_service import is_enabled, send_push_to_user
                    if is_enabled():
                        send_push_to_user(parent_id, title, body, data)
                except Exception as fcm_exc:
                    _log.error('[fees-reminder] FCM push failed parent_id=%s: %s',
                               parent_id, fcm_exc)

                sent += 1
                _log.warning(
                    '[fees-reminder] sent reminder student_id=%s installment_id=%s parent_user_id=%s',
                    student.id, inst.id, parent_id)

            except Exception as exc:
                _log.error(
                    '[fees-reminder] error sending parent_id=%s installment_id=%s: %s',
                    parent_id, inst.id, exc)
                db.session.rollback()

    _log.warning('[fees-reminder] school_id=%s completed sent=%d skipped=%d',
                 school.id, sent, skipped)
    return sent, skipped

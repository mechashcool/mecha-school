"""Super Admin permanent removal of a student — two-case policy.

A student is physically deleted only when nothing in the school's financial
history needs the row. Otherwise the row is kept as an operational tombstone
(``Student.status = 'deleted'``) so receipts, statements, refund history and
paid totals stay exactly as they were.

Protected financial history (any one is enough):
  * a ``fee_refund_events`` row for the student, or
  * an installment of one of the student's fees with ``received_amount > 0``
    or a ``receipt_no`` (money moved at some point — the ``revenues`` rows of
    that payment are linked to the installment through the receipt number).

CASE A — no protected history: the student and every student-owned child row
are deleted in one transaction. Revenues are never touched.

CASE B — protected history: the student must have no outstanding balance on an
active (non-cancelled) fee, otherwise the operation is refused so receivables
are never removed silently. The row becomes a tombstone: hidden from every
operational list, unlinked from parents / devices / transport / reminders, and
stripped of its section, card, building and residential-area links. Financial
rows (fee_records, fee_installments, fee_refund_events, revenues) and academic
history (attendance, exam results, documents, registration and institute
records) are preserved untouched.

Every statement here is raw SQL keyed by ``student_id`` on purpose: several of
these tables are academic-year scoped in the ORM, so an ORM query would
silently miss other years' rows (the same reason the original delete route
used raw SQL).
"""
from sqlalchemy import or_, text

from app.models import db, Student, parent_students


STUDENT_STATUS_DELETED = 'deleted'

# Statuses a school user may set through the edit form or the archive route.
# 'deleted' is deliberately absent: only the Super Admin removal flow sets it.
EDITABLE_STUDENT_STATUSES = ('active', 'transferred', 'withdrawn', 'graduated', 'archived')

OUTCOME_PHYSICAL_DELETE     = 'physical_delete'
OUTCOME_FINANCIAL_TOMBSTONE = 'financial_tombstone'
OUTCOME_BLOCKED_OUTSTANDING = 'blocked_outstanding_balance'

# Student-owned rows removed by a physical delete (CASE A), in a safe order.
# fee_reminder_logs points at both students and fee_installments, so it goes
# before the installments; installments go before their fee_records.
# The institute tables RESTRICT-reference students(id, school_id) and
# student_middle_records has a no-ondelete FK — all three must be cleared
# explicitly or the student delete fails.
_PHYSICAL_DELETE_TABLES = (
    'complaints', 'leave_requests', 'student_attendance', 'fee_records',
    'exam_results', 'student_documents', 'student_suspensions',
    'student_registration_records', 'student_middle_records',
    'institute_attendance_records', 'institute_group_enrollments',
    'student_transport', 'device_student_mappings',
)

# Operational links removed by a tombstone (CASE B). Nothing financial or
# academic is in this list.
_TOMBSTONE_UNLINK_TABLES = (
    'fee_reminder_logs', 'device_student_mappings', 'student_transport',
)


def student_not_deleted_clause():
    """SQL clause excluding tombstoned students. NULL status (legacy rows) is
    kept, matching how those rows behave today."""
    return or_(Student.status.is_(None), Student.status != STUDENT_STATUS_DELETED)


def is_deleted_student(student) -> bool:
    return student is not None and student.status == STUDENT_STATUS_DELETED


def _is_postgres() -> bool:
    return db.session.get_bind().dialect.name == 'postgresql'


def _lock_student_financial_rows(sid: int) -> None:
    """Serialize against concurrent payments / refunds / cancellations, which
    lock the same installment and fee rows FOR UPDATE. Held until commit."""
    if not _is_postgres():
        return
    params = {'sid': sid}
    db.session.execute(text("SELECT id FROM students WHERE id = :sid FOR UPDATE"), params)
    db.session.execute(
        text("SELECT id FROM fee_records WHERE student_id = :sid FOR UPDATE"), params)
    db.session.execute(
        text("SELECT id FROM fee_installments"
             " WHERE fee_record_id IN (SELECT id FROM fee_records WHERE student_id = :sid)"
             " FOR UPDATE"),
        params)


def has_protected_financial_history(sid: int) -> bool:
    """True when physically deleting the student would destroy financial
    history. Not filtered by school on purpose: any row anywhere that needs
    the student keeps it (fail closed)."""
    params = {'sid': sid}
    refund_events = db.session.execute(
        text("SELECT COUNT(*) FROM fee_refund_events WHERE student_id = :sid"), params,
    ).scalar() or 0
    if refund_events:
        return True
    paid_or_receipted = db.session.execute(
        text("SELECT COUNT(*) FROM fee_installments fi"
             " JOIN fee_records fr ON fr.id = fi.fee_record_id"
             " WHERE fr.student_id = :sid"
             "   AND (fi.received_amount > 0 OR fi.receipt_no IS NOT NULL)"),
        params,
    ).scalar() or 0
    return bool(paid_or_receipted)


def has_outstanding_balance(sid: int) -> bool:
    """True when any ACTIVE (non-cancelled) fee of the student still has an
    unpaid remainder — same remaining rule as the Fees list:
    total_amount - discount - SUM(received_amount) > 0."""
    count = db.session.execute(
        text("SELECT COUNT(*) FROM fee_records fr"
             " LEFT JOIN (SELECT fee_record_id, SUM(received_amount) AS paid"
             "            FROM fee_installments GROUP BY fee_record_id) p"
             "   ON p.fee_record_id = fr.id"
             " WHERE fr.student_id = :sid AND fr.cancelled_at IS NULL"
             "   AND (fr.total_amount - COALESCE(fr.discount, 0)"
             "        - COALESCE(p.paid, 0)) > 0"),
        {'sid': sid},
    ).scalar() or 0
    return bool(count)


def _physical_delete(student) -> None:
    sid = student.id
    params = {'sid': sid}
    db.session.execute(parent_students.delete().where(parent_students.c.student_id == sid))
    db.session.execute(text("DELETE FROM fee_reminder_logs WHERE student_id = :sid"), params)
    db.session.execute(
        text("DELETE FROM fee_installments"
             " WHERE fee_record_id IN (SELECT id FROM fee_records WHERE student_id = :sid)"),
        params)
    # The ORM backrefs of student_registration_records / student_transport /
    # device_student_mappings would otherwise try to NULL a NOT NULL
    # student_id during the flush; removing the rows first avoids that.
    for tbl in _PHYSICAL_DELETE_TABLES:
        db.session.execute(text(f"DELETE FROM {tbl} WHERE student_id = :sid"), params)
    # Admission requests are the school's own audit trail: keep the row, clear
    # only the optional pointer (nullable; the admissions screen handles it).
    db.session.execute(
        text("UPDATE student_registration_requests SET approved_student_id = NULL"
             " WHERE approved_student_id = :sid"),
        params)
    db.session.delete(student)


def _tombstone(student) -> None:
    sid = student.id
    params = {'sid': sid}
    db.session.execute(parent_students.delete().where(parent_students.c.student_id == sid))
    for tbl in _TOMBSTONE_UNLINK_TABLES:
        db.session.execute(text(f"DELETE FROM {tbl} WHERE student_id = :sid"), params)
    student.status = STUDENT_STATUS_DELETED
    student.section_id = None
    student.rfid_tag_id = None
    student.building_id = None
    student.residential_area_id = None


def remove_student_permanently(student) -> str:
    """Apply the two-case policy inside the CURRENT transaction and return the
    outcome. The caller authorizes (Super Admin + selected school), commits on
    success and rolls back on any exception. Nothing is written when the
    outcome is OUTCOME_BLOCKED_OUTSTANDING."""
    sid = student.id
    _lock_student_financial_rows(sid)
    if not has_protected_financial_history(sid):
        _physical_delete(student)
        return OUTCOME_PHYSICAL_DELETE
    if has_outstanding_balance(sid):
        return OUTCOME_BLOCKED_OUTSTANDING
    _tombstone(student)
    return OUTCOME_FINANCIAL_TOMBSTONE

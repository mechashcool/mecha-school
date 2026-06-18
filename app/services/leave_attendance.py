"""
Leave-to-attendance synchronization.

When a LeaveRequest (student) or EmployeeLeaveRequest (employee) transitions
to status='approved', attendance records with status='on_leave' and
source='leave' are written for every date in [from_date, to_date] that has:
  • No existing record, OR
  • An existing absent record with no physical check-in (auto-absent or manual
    absent with no check-in time) — converted to on_leave, with the original
    state encoded in notes for exact restoration on revocation.

Precedence rules (highest → lowest):
  1. Physical check-in or manual present/late (check_in is not None) — always
     preserved and takes priority over approved leave. Actual attendance wins.
  2. Approved leave — fills empty dates AND converts absent-without-check-in
     records to on_leave.
  3. Existing present, late, or on_leave record — never overwritten by sync.
  4. Absent records with no check-in — converted (not preserved) when leave
     covers the same date. Restored exactly on revocation.

When the leave is revoked (status changes away from 'approved'), the sync
restores the original state:
  • Records created from scratch (no prior attendance) → deleted.
  • Records converted from absent → restored to their original absent/source.
  Manual, device, and already-present/late records are never touched.

Date-change support:
  When an approved leave's from_date or to_date is edited, call
  resync_student_leave_dates / resync_employee_leave_dates with the old dates.
  The function revokes attendance for dates no longer covered and applies
  attendance for newly covered dates.

Auto-absent protection: on_leave records in the attendance table prevent the
auto-absent scheduler from duplicating an absent entry (it skips already_ids).
No changes to the scheduler are required.

School isolation: every write and delete filters explicitly by school_id.
Academic-year isolation: student leaves carry a mandatory academic_year_id;
employee leaves resolve the active year automatically when the field is null.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta

_log = logging.getLogger(__name__)

# ─── Marker helpers for converted absent records ────────────────────────────
# When an absent record is converted to on_leave, the original source is
# encoded in notes using this marker so revocation can restore it exactly.
# Format: [leave:{leave_id}|was:absent:{original_source}]
_PREV_MARKER = re.compile(
    r'^\[leave:(\d+)\|was:absent:([\w]+)\](.*)',
    re.DOTALL,
)


def _encode_prev_absent(original_source: str, leave_id: int, original_notes) -> str:
    marker = f'[leave:{leave_id}|was:absent:{original_source}]'
    suffix = original_notes.strip() if original_notes else ''
    return f'{marker} {suffix}'.strip()


def _decode_prev_absent(notes):
    """Return (original_source, cleaned_notes_or_None) or None if no marker."""
    if not notes:
        return None
    m = _PREV_MARKER.match(notes)
    if not m:
        return None
    return m.group(2), (m.group(3).strip() or None)


# ─── Date utility ─────────────────────────────────────────────────────────────

def _date_range(from_date: date, to_date: date):
    """Yield every date in [from_date, to_date] inclusive."""
    d = from_date
    while d <= to_date:
        yield d
        d += timedelta(days=1)


# ─────────────────────────────────────────────────────────────────────────────
#  Student — internal apply/revoke helpers
# ─────────────────────────────────────────────────────────────────────────────

def _apply_student_leave_dates(school_id, student_id, year_id, dates, leave_request):
    """
    Apply leave to a set of dates for a student.

    For each date:
      - No existing record → create on_leave (source='leave').
      - Existing absent with check_in is None → convert to on_leave,
        encoding original source in notes for later restoration.
      - Existing present, late, or on_leave → skip (actual attendance wins).

    Modifies the ORM session but does NOT commit.
    Returns (created, converted) counts.
    """
    from app.models import db, StudentAttendance

    date_list = sorted(dates)
    if not date_list:
        return 0, 0

    existing_rows = (
        StudentAttendance.query
        .execution_options(bypass_tenant_scope=True)
        .filter(
            StudentAttendance.school_id  == school_id,
            StudentAttendance.student_id == student_id,
            StudentAttendance.date.between(date_list[0], date_list[-1]),
        )
        .all()
    )
    by_date = {r.date: r for r in existing_rows}

    created = converted = 0
    for d in date_list:
        rec = by_date.get(d)
        if rec is None:
            db.session.add(StudentAttendance(
                student_id       = student_id,
                school_id        = school_id,
                academic_year_id = year_id,
                date             = d,
                status           = 'on_leave',
                source           = 'leave',
                notes            = f'إجازة مجازة #{leave_request.id}',
            ))
            created += 1
        elif rec.status == 'absent' and rec.check_in is None:
            # Convert absent-without-check-in to on_leave.
            # Encode original source so revocation restores the absent record.
            rec.notes  = _encode_prev_absent(
                rec.source or 'manual', leave_request.id, rec.notes,
            )
            rec.status = 'on_leave'
            rec.source = 'leave'
            converted += 1
        # else: present, late, on_leave — actual attendance, leave untouched.

    return created, converted


def _revoke_student_leave_dates(school_id, student_id, dates):
    """
    Revoke leave attendance records for a set of dates for a student.

    For each leave-managed record (source='leave', status='on_leave') on a date:
      - Has prev-absent marker → restore to absent with original source.
      - No marker → delete (was created from nothing by sync).
    Other records (present, late, manual on_leave) are untouched.

    Modifies the ORM session but does NOT commit.
    Returns (deleted, restored) counts.
    """
    from app.models import db, StudentAttendance

    date_list = sorted(dates)
    if not date_list:
        return 0, 0

    to_handle = (
        StudentAttendance.query
        .execution_options(bypass_tenant_scope=True)
        .filter(
            StudentAttendance.school_id  == school_id,
            StudentAttendance.student_id == student_id,
            StudentAttendance.status     == 'on_leave',
            StudentAttendance.source     == 'leave',
            StudentAttendance.date.in_(date_list),
        )
        .all()
    )

    deleted = restored = 0
    for rec in to_handle:
        prev = _decode_prev_absent(rec.notes)
        if prev:
            original_source, cleaned_notes = prev
            rec.status = 'absent'
            rec.source = original_source
            rec.notes  = cleaned_notes
            restored += 1
        else:
            db.session.delete(rec)
            deleted += 1

    return deleted, restored


# ─────────────────────────────────────────────────────────────────────────────
#  Employee — internal apply/revoke helpers
# ─────────────────────────────────────────────────────────────────────────────

def _apply_employee_leave_dates(school_id, employee_id, year_id, dates, leave_request):
    """
    Apply leave to a set of dates for an employee.
    Same logic as _apply_student_leave_dates.
    Modifies the ORM session but does NOT commit.
    Returns (created, converted) counts.
    """
    from app.models import db, EmployeeAttendance

    date_list = sorted(dates)
    if not date_list:
        return 0, 0

    existing_rows = (
        EmployeeAttendance.query
        .execution_options(bypass_tenant_scope=True)
        .filter(
            EmployeeAttendance.school_id   == school_id,
            EmployeeAttendance.employee_id == employee_id,
            EmployeeAttendance.date.between(date_list[0], date_list[-1]),
        )
        .all()
    )
    by_date = {r.date: r for r in existing_rows}

    created = converted = 0
    for d in date_list:
        rec = by_date.get(d)
        if rec is None:
            db.session.add(EmployeeAttendance(
                employee_id      = employee_id,
                school_id        = school_id,
                academic_year_id = year_id,
                date             = d,
                status           = 'on_leave',
                source           = 'leave',
                notes            = f'إجازة مجازة #{leave_request.id}',
            ))
            created += 1
        elif rec.status == 'absent' and rec.check_in is None:
            rec.notes  = _encode_prev_absent(
                rec.source or 'manual', leave_request.id, rec.notes,
            )
            rec.status = 'on_leave'
            rec.source = 'leave'
            converted += 1

    return created, converted


def _revoke_employee_leave_dates(school_id, employee_id, dates):
    """
    Revoke leave attendance records for a set of dates for an employee.
    Same logic as _revoke_student_leave_dates.
    Modifies the ORM session but does NOT commit.
    Returns (deleted, restored) counts.
    """
    from app.models import db, EmployeeAttendance

    date_list = sorted(dates)
    if not date_list:
        return 0, 0

    to_handle = (
        EmployeeAttendance.query
        .execution_options(bypass_tenant_scope=True)
        .filter(
            EmployeeAttendance.school_id   == school_id,
            EmployeeAttendance.employee_id == employee_id,
            EmployeeAttendance.status      == 'on_leave',
            EmployeeAttendance.source      == 'leave',
            EmployeeAttendance.date.in_(date_list),
        )
        .all()
    )

    deleted = restored = 0
    for rec in to_handle:
        prev = _decode_prev_absent(rec.notes)
        if prev:
            original_source, cleaned_notes = prev
            rec.status = 'absent'
            rec.source = original_source
            rec.notes  = cleaned_notes
            restored += 1
        else:
            db.session.delete(rec)
            deleted += 1

    return deleted, restored


# ─────────────────────────────────────────────────────────────────────────────
#  Public sync functions — called on leave status changes
# ─────────────────────────────────────────────────────────────────────────────

def sync_student_leave(leave_request) -> None:
    """
    Synchronize StudentAttendance with a student leave request.

    Approved  → for each date in [from_date, to_date]:
                  • No record          → create on_leave
                  • Absent, no check-in → convert to on_leave (restorable)
                  • Present/late/on_leave → left untouched
    Otherwise → restore/delete records created or converted by this sync.

    Must be called after the leave_request change has been committed.
    """
    from app.models import db

    school_id  = leave_request.school_id
    student_id = leave_request.student_id
    from_date  = leave_request.from_date
    to_date    = leave_request.to_date
    year_id    = leave_request.academic_year_id

    if not all([school_id, student_id, from_date, to_date, year_id]):
        _log.warning(
            '[leave-att] student leave %s missing required fields — skipped',
            leave_request.id,
        )
        return

    all_dates = set(_date_range(from_date, to_date))

    if leave_request.status == 'approved':
        created, converted = _apply_student_leave_dates(
            school_id, student_id, year_id, all_dates, leave_request,
        )
        if created or converted:
            try:
                db.session.commit()
                _log.info(
                    '[leave-att] student=%s school=%s leave=%s: '
                    'created=%d converted=%d (%s → %s)',
                    student_id, school_id, leave_request.id,
                    created, converted, from_date, to_date,
                )
            except Exception:
                db.session.rollback()
                _log.exception(
                    '[leave-att] student=%s school=%s leave=%s: commit failed',
                    student_id, school_id, leave_request.id,
                )
    else:
        deleted, restored = _revoke_student_leave_dates(school_id, student_id, all_dates)
        if deleted or restored:
            try:
                db.session.commit()
                _log.info(
                    '[leave-att] student=%s school=%s leave=%s: '
                    'deleted=%d restored=%d (leave revoked)',
                    student_id, school_id, leave_request.id, deleted, restored,
                )
            except Exception:
                db.session.rollback()
                _log.exception(
                    '[leave-att] student=%s school=%s leave=%s: delete commit failed',
                    student_id, school_id, leave_request.id,
                )


def sync_employee_leave(leave_request) -> None:
    """
    Synchronize EmployeeAttendance with an employee leave request.

    Same precedence rules as sync_student_leave:
      • No record → create on_leave
      • Absent, no check-in → convert to on_leave (restorable)
      • Present/late/on_leave → left untouched

    EmployeeLeaveRequest.academic_year_id is nullable; when absent the active
    academic year for the school is resolved automatically.
    """
    from app.models import db

    school_id   = leave_request.school_id
    employee_id = leave_request.employee_id
    from_date   = leave_request.from_date
    to_date     = leave_request.to_date
    year_id     = leave_request.academic_year_id

    if not year_id:
        from app.utils.decorators import get_active_year
        ay = get_active_year(school_id)
        year_id = ay.id if ay else None

    if not all([school_id, employee_id, from_date, to_date, year_id]):
        _log.warning(
            '[leave-att] employee leave %s missing required fields — skipped',
            leave_request.id,
        )
        return

    all_dates = set(_date_range(from_date, to_date))

    if leave_request.status == 'approved':
        created, converted = _apply_employee_leave_dates(
            school_id, employee_id, year_id, all_dates, leave_request,
        )
        if created or converted:
            try:
                db.session.commit()
                _log.info(
                    '[leave-att] employee=%s school=%s leave=%s: '
                    'created=%d converted=%d (%s → %s)',
                    employee_id, school_id, leave_request.id,
                    created, converted, from_date, to_date,
                )
            except Exception:
                db.session.rollback()
                _log.exception(
                    '[leave-att] employee=%s school=%s leave=%s: commit failed',
                    employee_id, school_id, leave_request.id,
                )
    else:
        deleted, restored = _revoke_employee_leave_dates(school_id, employee_id, all_dates)
        if deleted or restored:
            try:
                db.session.commit()
                _log.info(
                    '[leave-att] employee=%s school=%s leave=%s: '
                    'deleted=%d restored=%d (leave revoked)',
                    employee_id, school_id, leave_request.id, deleted, restored,
                )
            except Exception:
                db.session.rollback()
                _log.exception(
                    '[leave-att] employee=%s school=%s leave=%s: delete commit failed',
                    employee_id, school_id, leave_request.id,
                )


# ─────────────────────────────────────────────────────────────────────────────
#  Date-change resync — called when an approved leave's dates are edited
# ─────────────────────────────────────────────────────────────────────────────

def resync_student_leave_dates(leave_request, old_from_date, old_to_date) -> None:
    """
    Called when an already-approved student leave's from_date or to_date changes.

    Computes the delta between the old and new date ranges:
      • Dates removed from coverage → revoked (deleted or restored to absent).
      • Dates added to coverage    → applied (created or converted from absent).

    Must be called AFTER leave_request.from_date/to_date has been updated and
    committed to the database.
    """
    from app.models import db

    if leave_request.status != 'approved':
        return

    school_id  = leave_request.school_id
    student_id = leave_request.student_id
    year_id    = leave_request.academic_year_id
    new_from   = leave_request.from_date
    new_to     = leave_request.to_date

    if not all([school_id, student_id, year_id, old_from_date, old_to_date, new_from, new_to]):
        _log.warning('[leave-att] resync_student_leave_dates leave=%s: missing fields', leave_request.id)
        return

    old_dates = set(_date_range(old_from_date, old_to_date))
    new_dates = set(_date_range(new_from, new_to))
    removed   = old_dates - new_dates
    added     = new_dates - old_dates

    needs_commit = False

    if removed:
        deleted, restored = _revoke_student_leave_dates(school_id, student_id, removed)
        if deleted or restored:
            needs_commit = True
            _log.info(
                '[leave-att] resync student=%s school=%s leave=%s: '
                'removed %d dates (del=%d rest=%d)',
                student_id, school_id, leave_request.id, len(removed), deleted, restored,
            )

    if added:
        created, converted = _apply_student_leave_dates(
            school_id, student_id, year_id, added, leave_request,
        )
        if created or converted:
            needs_commit = True
            _log.info(
                '[leave-att] resync student=%s school=%s leave=%s: '
                'added %d dates (cre=%d conv=%d)',
                student_id, school_id, leave_request.id, len(added), created, converted,
            )

    if needs_commit:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            _log.exception(
                '[leave-att] resync_student_leave_dates leave=%s: commit failed',
                leave_request.id,
            )


def resync_employee_leave_dates(leave_request, old_from_date, old_to_date) -> None:
    """
    Called when an already-approved employee leave's from_date or to_date changes.
    Same delta logic as resync_student_leave_dates.
    """
    from app.models import db

    if leave_request.status != 'approved':
        return

    school_id   = leave_request.school_id
    employee_id = leave_request.employee_id
    year_id     = leave_request.academic_year_id

    if not year_id:
        from app.utils.decorators import get_active_year
        ay = get_active_year(school_id)
        year_id = ay.id if ay else None

    new_from = leave_request.from_date
    new_to   = leave_request.to_date

    if not all([school_id, employee_id, year_id, old_from_date, old_to_date, new_from, new_to]):
        _log.warning('[leave-att] resync_employee_leave_dates leave=%s: missing fields', leave_request.id)
        return

    old_dates = set(_date_range(old_from_date, old_to_date))
    new_dates = set(_date_range(new_from, new_to))
    removed   = old_dates - new_dates
    added     = new_dates - old_dates

    needs_commit = False

    if removed:
        deleted, restored = _revoke_employee_leave_dates(school_id, employee_id, removed)
        if deleted or restored:
            needs_commit = True
            _log.info(
                '[leave-att] resync employee=%s school=%s leave=%s: '
                'removed %d dates (del=%d rest=%d)',
                employee_id, school_id, leave_request.id, len(removed), deleted, restored,
            )

    if added:
        created, converted = _apply_employee_leave_dates(
            school_id, employee_id, year_id, added, leave_request,
        )
        if created or converted:
            needs_commit = True
            _log.info(
                '[leave-att] resync employee=%s school=%s leave=%s: '
                'added %d dates (cre=%d conv=%d)',
                employee_id, school_id, leave_request.id, len(added), created, converted,
            )

    if needs_commit:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            _log.exception(
                '[leave-att] resync_employee_leave_dates leave=%s: commit failed',
                leave_request.id,
            )


# ─────────────────────────────────────────────────────────────────────────────
#  Backfill helper — called from the admin backfill route
# ─────────────────────────────────────────────────────────────────────────────

def backfill_all_approved_leaves(school_id=None) -> dict:
    """
    One-time backfill: sync all existing approved leave requests that were
    approved before this feature was deployed.

    school_id=None → backfill every school (super-admin call).
    school_id=N    → backfill only that school (school-scoped admin call).

    Safe to re-run: existing on_leave records are skipped (no duplicate creation).
    Now also converts existing absent-without-check-in records to on_leave.

    Returns a summary dict with counts:
        student_leaves_processed, employee_leaves_processed,
        student_errors, employee_errors
    """
    from app.models import LeaveRequest, EmployeeLeaveRequest

    st_processed = st_errors = 0
    em_processed = em_errors = 0

    # ── Student leaves ────────────────────────────────────────────────────────
    sq = (LeaveRequest.query
          .execution_options(bypass_tenant_scope=True, include_all_years=True)
          .filter(LeaveRequest.status == 'approved'))
    if school_id:
        sq = sq.filter(LeaveRequest.school_id == school_id)

    for lr in sq.all():
        try:
            sync_student_leave(lr)
            st_processed += 1
        except Exception:
            _log.exception(
                '[leave-att-backfill] student leave %s failed', lr.id)
            st_errors += 1

    # ── Employee leaves ───────────────────────────────────────────────────────
    eq = (EmployeeLeaveRequest.query
          .execution_options(bypass_tenant_scope=True)
          .filter(EmployeeLeaveRequest.status == 'approved'))
    if school_id:
        eq = eq.filter(EmployeeLeaveRequest.school_id == school_id)

    for lr in eq.all():
        try:
            sync_employee_leave(lr)
            em_processed += 1
        except Exception:
            _log.exception(
                '[leave-att-backfill] employee leave %s failed', lr.id)
            em_errors += 1

    return {
        'student_leaves_processed': st_processed,
        'employee_leaves_processed': em_processed,
        'student_errors': st_errors,
        'employee_errors': em_errors,
    }

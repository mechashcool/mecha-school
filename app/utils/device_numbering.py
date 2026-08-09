"""Automatic attendance-device user-number allocation for students.

The device user number is stored in the existing field
``DeviceStudentMapping.employee_no_string`` — no second numbering system is
introduced here.

Uniqueness scope
----------------
The DB constraint ``uq_device_student_mapping_device_empno`` is
``(device_id, employee_no_string)``, so the number is unique **per device**.
Numbers are therefore allocated independently for each device; two different
devices may legitimately both use 1, 2, 3 …

For a device whose ``device_scope`` is ``'mixed'`` the same physical user list
also holds employee enrolment numbers (``DeviceEmployeeMapping.enrollment_no``),
which ``add_mapping`` already refuses to collide with in the employee
direction. Allocation reads those numbers too (read-only) so a generated
student number can never collide with an existing employee number on the same
device. Employee mapping behaviour itself is unchanged.

Allocation rule
---------------
``max(existing valid positive number in the device) + 1``, starting at 1 for a
device with no mappings. Gaps left by deleted mappings are never reused, and no
existing mapping is ever renumbered.

Concurrency
-----------
On PostgreSQL a transaction-scoped advisory lock keyed on the device id
serialises "read max → insert" for a single device, so two simultaneous
requests cannot compute the same number. The lock is released automatically
when the caller's transaction commits or rolls back. The unique constraint
remains the final guard: an IntegrityError is surfaced as
``DeviceNumberAllocationError`` instead of a raw DB error.
"""
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

# Arbitrary namespace so this advisory lock cannot collide with another
# feature's advisory lock keys.
_ADVISORY_LOCK_NAMESPACE = 815343


class DeviceNumberAllocationError(Exception):
    """Raised when a device user number could not be allocated safely.

    The session is left in a failed state when this is raised from a flush —
    the caller must roll back and surface a user-facing Arabic message.
    """


def _lock_device_numbering(device_id):
    """Serialise number allocation for one device (PostgreSQL only).

    Held until the caller's transaction ends. On any other backend the unique
    constraint plus the pre-insert checks remain in force.
    """
    from app.models import db
    try:
        dialect = db.session.get_bind().dialect.name
    except Exception:
        return
    if dialect != 'postgresql':
        return
    db.session.execute(
        text('SELECT pg_advisory_xact_lock(CAST(:ns AS int), CAST(:key AS int))'),
        {'ns': _ADVISORY_LOCK_NAMESPACE, 'key': int(device_id)},
    )


def _next_device_number(device):
    """Return the next free positive integer for this device."""
    from app.models import db, DeviceStudentMapping, DeviceEmployeeMapping

    # Scoped by device_id only — that is exactly the uniqueness scope of the
    # constraint being satisfied, and a device belongs to a single school.
    used = [row[0] for row in
            db.session.query(DeviceStudentMapping.employee_no_string)
            .filter(DeviceStudentMapping.device_id == device.id).all()]

    if getattr(device, 'device_scope', 'students') == 'mixed':
        used += [row[0] for row in
                 db.session.query(DeviceEmployeeMapping.enrollment_no)
                 .filter(DeviceEmployeeMapping.device_id == device.id).all()]

    highest = 0
    for value in used:
        raw = (value or '').strip()
        if raw.isdigit():
            number = int(raw)
            if number > highest:
                highest = number
    return highest + 1


def _existing_mapping(device_id, student_id):
    from app.models import db, DeviceStudentMapping
    return (db.session.query(DeviceStudentMapping)
            .filter(DeviceStudentMapping.device_id == device_id,
                    DeviceStudentMapping.student_id == student_id)
            .first())


def ensure_student_device_mapping(device, student_id, school_id):
    """Return ``(mapping, created)`` for this student on this device.

    * If the student is already mapped to the device, the existing mapping is
      returned untouched with ``created=False`` — no second mapping and no new
      number, and its current number is preserved.
    * Otherwise the next free number for the device is allocated and a new
      mapping is staged with ``db.session.flush()`` (NOT committed — the caller
      owns the transaction).

    Raises ``DeviceNumberAllocationError`` if the insert violates the device
    uniqueness constraint; the caller must roll back.
    """
    from app.models import db, DeviceStudentMapping

    existing = _existing_mapping(device.id, student_id)
    if existing is not None:
        return existing, False

    _lock_device_numbering(device.id)

    # Re-check under the lock: a concurrent request may have mapped this
    # student while we were waiting for the lock.
    existing = _existing_mapping(device.id, student_id)
    if existing is not None:
        return existing, False

    mapping = DeviceStudentMapping(
        school_id=school_id,
        device_id=device.id,
        employee_no_string=str(_next_device_number(device)),
        student_id=student_id,
        is_active=True,
    )
    db.session.add(mapping)
    try:
        db.session.flush()
    except IntegrityError as exc:
        raise DeviceNumberAllocationError(str(exc)[:500]) from exc
    return mapping, True

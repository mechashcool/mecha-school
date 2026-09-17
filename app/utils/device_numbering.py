"""Automatic attendance-device user-number allocation for students.

The device user number is stored in the existing field
``DeviceStudentMapping.employee_no_string`` — no second numbering system is
introduced here.

Uniqueness scope
----------------
The DB constraint ``uq_device_student_mapping_device_empno`` is
``(device_id, employee_no_string)``, so the number is unique **per device**.
A student has ONE number across all of their devices
(``ensure_student_device_mappings``): a new student gets a number free on every
target device; a student who already has a number keeps it for new bindings,
and the binding is refused if that number is taken on a target device or if
the student's existing numbers are inconsistent.

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


# The AI Face protocol sends enrollid as an integer; keep explicit numbers
# within a signed 32-bit range.
MAX_DEVICE_NUMBER = 999999999


class DeviceNumberAllocationError(Exception):
    """Raised when a device user number could not be allocated safely.

    The session is left in a failed state when this is raised from a flush —
    the caller must roll back and surface a user-facing Arabic message.
    """


class DeviceNumberConflictError(DeviceNumberAllocationError):
    """The student's existing number cannot be used on a new target device.

    ``str(exc)`` is a user-facing Arabic message. Raised before any mapping is
    staged; the caller still rolls back its transaction.
    """


class DeviceNumberChangeError(Exception):
    """Raised when an explicit student device-number change is rejected.

    ``str(exc)`` is a user-facing Arabic message. Nothing has been modified
    when this is raised before the flush; after a failed flush the caller must
    roll back.
    """


def _normalise_number(value):
    """Canonical form used for comparisons: '007' and '7' are the same user."""
    raw = (value or '').strip()
    return str(int(raw)) if raw.isdigit() else raw


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


def _used_numbers(device, exclude_student_id=None):
    """Numbers already taken on this device, in normalised form.

    Student mappings always count; employee enrolment numbers count when the
    device's user list is shared with staff (``device_scope == 'mixed'``).
    ``exclude_student_id`` drops that student's own mappings.
    """
    from app.models import db, DeviceStudentMapping, DeviceEmployeeMapping

    # Scoped by device_id only — that is exactly the uniqueness scope of the
    # constraint being satisfied, and a device belongs to a single school.
    stu_q = (db.session.query(DeviceStudentMapping.employee_no_string)
             .filter(DeviceStudentMapping.device_id == device.id))
    if exclude_student_id is not None:
        stu_q = stu_q.filter(DeviceStudentMapping.student_id != exclude_student_id)
    used = [row[0] for row in stu_q.all()]

    if getattr(device, 'device_scope', 'students') == 'mixed':
        used += [row[0] for row in
                 db.session.query(DeviceEmployeeMapping.enrollment_no)
                 .filter(DeviceEmployeeMapping.device_id == device.id).all()]

    return {_normalise_number(v) for v in used}


def _next_device_number(device):
    """Return the next free positive integer for this device."""
    highest = 0
    for value in _used_numbers(device):
        if value.isdigit():
            number = int(value)
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

    Single-device form of ``ensure_student_device_mappings``.
    """
    return ensure_student_device_mappings([device], student_id, school_id)[0]


def ensure_student_device_mappings(devices, student_id, school_id):
    """Bind a student to every device in ``devices`` with ONE shared number.

    Returns ``[(mapping, created), ...]`` in the order of ``devices``.

    * A device the student is already mapped to keeps its mapping untouched
      (``created=False``) — no second mapping and no renumbering.
    * For the missing devices:
        1. a student with no binding in this school gets
           ``max(used on any missing device) + 1``, free on all of them;
        2. a student with one consistent number reuses it; if it is taken on
           any missing device ``DeviceNumberConflictError`` is raised;
        3. a student whose bindings carry different numbers is refused with
           ``DeviceNumberConflictError`` until corrected explicitly.
      Nothing is staged when a conflict is raised.
    * New mappings are staged with ``db.session.flush()`` (NOT committed — the
      caller owns the transaction).

    Devices of another school are ignored. Raises
    ``DeviceNumberAllocationError`` if the insert violates the device
    uniqueness constraint; the caller must roll back.
    """
    from app.models import db, DeviceStudentMapping

    targets = []
    seen = set()
    for dev in devices:
        if dev is None or dev.id in seen or dev.school_id != school_id:
            continue
        seen.add(dev.id)
        targets.append(dev)

    existing = {dev.id: _existing_mapping(dev.id, student_id) for dev in targets}
    missing = [dev for dev in targets if existing[dev.id] is None]

    if missing:
        # Lock in a stable order so two requests on overlapping device sets
        # cannot deadlock.
        for dev in sorted(missing, key=lambda d: d.id):
            _lock_device_numbering(dev.id)

        # Re-check under the locks: a concurrent request may have mapped this
        # student while we were waiting.
        for dev in missing:
            existing[dev.id] = _existing_mapping(dev.id, student_id)
        missing = [dev for dev in targets if existing[dev.id] is None]

    if missing:
        used_by_device = {dev.id: _used_numbers(dev) for dev in missing}

        own_numbers = {
            _normalise_number(row[0]) for row in
            db.session.query(DeviceStudentMapping.employee_no_string)
            .filter(DeviceStudentMapping.student_id == student_id,
                    DeviceStudentMapping.school_id == school_id).all()
        }
        number = None
        if len(own_numbers) > 1:
            raise DeviceNumberConflictError(
                'أرقام هذا الطالب غير متطابقة بين الأجهزة. يرجى توحيد رقمه أولاً '
                'من صفحة ربط الطلاب عبر "تعديل رقم الطالب في الجهاز". '
                'لم يتم إنشاء أي ربط.')
        if own_numbers:
            number = own_numbers.pop()
            busy = [dev.name for dev in missing if number in used_by_device[dev.id]]
            if busy:
                raise DeviceNumberConflictError(
                    f'رقم الطالب {number} مستخدم لشخص آخر على الجهاز: '
                    f'{"، ".join(busy)}. لم يتم إنشاء أي ربط؛ يرجى تعديل رقم '
                    f'الطالب إلى رقم متاح أولاً.')
        else:
            highest = 0
            for used in used_by_device.values():
                for value in used:
                    if value.isdigit() and int(value) > highest:
                        highest = int(value)
            number = str(highest + 1)

        for dev in missing:
            mapping = DeviceStudentMapping(
                school_id=school_id,
                device_id=dev.id,
                employee_no_string=number,
                student_id=student_id,
                is_active=True,
            )
            db.session.add(mapping)
            existing[dev.id] = mapping
        try:
            db.session.flush()
        except IntegrityError as exc:
            raise DeviceNumberAllocationError(str(exc)[:500]) from exc

    created_ids = {dev.id for dev in missing}
    return [(existing[dev.id], dev.id in created_ids) for dev in targets]


def change_student_device_number(student_id, school_id, new_number):
    """Set one explicit number on ALL of a student's mappings in this school.

    Only the stored bindings change; the student record, its school serial
    number and attendance history are untouched, and nothing is sent to a
    physical device.

    Every target device is validated before anything is modified. Returns
    ``(mappings, changed)``. Raises ``DeviceNumberChangeError`` with an Arabic
    message on invalid input or a collision; after a failed flush the caller
    must roll back.
    """
    from app.models import db, DeviceStudentMapping

    raw = (new_number or '').strip()
    # isdecimal, not isdigit: '²' passes isdigit but int('²') raises.
    if not raw.isdecimal() or int(raw) <= 0 or int(raw) > MAX_DEVICE_NUMBER:
        raise DeviceNumberChangeError(
            f'رقم الطالب في الجهاز يجب أن يكون عدداً صحيحاً موجباً '
            f'لا يتجاوز {MAX_DEVICE_NUMBER}.')
    number = str(int(raw))

    mappings = (DeviceStudentMapping.query
                .filter(DeviceStudentMapping.student_id == student_id,
                        DeviceStudentMapping.school_id == school_id)
                .order_by(DeviceStudentMapping.device_id)
                .all())
    if not mappings:
        raise DeviceNumberChangeError('لا توجد ربطات أجهزة لهذا الطالب.')

    if all(m.employee_no_string == number for m in mappings):
        return mappings, False

    for m in mappings:
        _lock_device_numbering(m.device_id)

    conflicts = [m.device.name for m in mappings
                 if number in _used_numbers(m.device, exclude_student_id=student_id)]
    if conflicts:
        raise DeviceNumberChangeError(
            f'الرقم {number} مستخدم لشخص آخر على الجهاز: {"، ".join(conflicts)}. '
            f'لم يتم تعديل أي ربط.')

    for m in mappings:
        m.employee_no_string = number
    try:
        db.session.flush()
    except IntegrityError as exc:
        raise DeviceNumberChangeError(
            'تعذر تعديل رقم الطالب بسبب تعارض في الأرقام. لم يتم تعديل أي ربط.') from exc
    return mappings, True

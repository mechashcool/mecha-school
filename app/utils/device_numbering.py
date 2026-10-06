"""Attendance-device user-number allocation for students and employees.

Device user numbers are stored in the existing fields
``DeviceStudentMapping.employee_no_string`` and
``DeviceEmployeeMapping.enrollment_no`` — no second numbering system is
introduced here.

Number ranges (NEW numbers only)
--------------------------------
* students:  ``STUDENT_DEVICE_NUMBER_MIN``..``STUDENT_DEVICE_NUMBER_MAX`` (1-999)
* employees: ``EMPLOYEE_DEVICE_NUMBER_MIN``..``EMPLOYEE_DEVICE_NUMBER_MAX``
  (1000-``MAX_DEVICE_NUMBER``)

The ranges govern automatic allocation and new operator-typed numbers only.
Existing stored numbers are historical data: nothing in this module rewrites,
normalises, renumbers or deletes a stored mapping, and a person's existing
number is reused as-is even when it lies outside the new range.

Collision rule
--------------
A NEW number is refused if its numeric value ('7' == '07' == '007') is used by
ANY student or employee mapping of the same school, in either table and on any
device — legacy mappings may already sit in the opposite range. Reusing a
person's own existing number on a further device is checked against both
tables on the target devices, which is the device's physical user namespace.

Allocation rule
---------------
``max(used numbers inside the range) + 1``, starting at the range minimum. Only
when the top of the range is already taken is the lowest free number of the
range used; if none is free the allocation fails and nothing is staged.

Device targets
--------------
Students are only bound to ``students`` / ``mixed`` devices, employees only to
``employees`` / ``mixed`` devices. Bindings that already exist elsewhere are
left untouched.

Concurrency
-----------
On PostgreSQL every number write (allocation, explicit change, manual employee
mapping, copy) takes ONE transaction-scoped advisory lock keyed on the school,
so student and employee writes of a school serialise against each other. The
lock is released when the caller's transaction commits or rolls back. The
per-device unique constraints remain the final guard: an IntegrityError is
surfaced as ``DeviceNumberAllocationError`` instead of a raw DB error.

Reads here bypass the automatic tenant scope and filter on the explicit
``school_id`` instead: a super administrator creating a teacher for another
school must see THAT school's numbers, not those of the session's school.

Nothing in this module talks to a physical device.
"""
import re

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

# Arbitrary namespace so this advisory lock cannot collide with another
# feature's advisory lock keys (the earlier per-device lock used 815343).
_ADVISORY_LOCK_NAMESPACE = 815344

# The AI Face protocol sends enrollid as an integer; keep explicit numbers
# within a signed 32-bit range.
MAX_DEVICE_NUMBER = 999999999

STUDENT_DEVICE_NUMBER_MIN = 1
STUDENT_DEVICE_NUMBER_MAX = 999
EMPLOYEE_DEVICE_NUMBER_MIN = 1000
EMPLOYEE_DEVICE_NUMBER_MAX = MAX_DEVICE_NUMBER

STUDENT_DEVICE_SCOPES = ('students', 'mixed')
EMPLOYEE_DEVICE_SCOPES = ('employees', 'mixed')

_ASCII_DIGITS = re.compile(r'[0-9]+')
_UNSCOPED = {'bypass_tenant_scope': True}


class DeviceNumberAllocationError(Exception):
    """Raised when a device user number could not be allocated safely.

    The session is left in a failed state when this is raised from a flush —
    the caller must roll back and surface a user-facing Arabic message.
    """


class DeviceNumberConflictError(DeviceNumberAllocationError):
    """The person's number cannot be used, or no number is available.

    ``str(exc)`` is a user-facing Arabic message. Raised before any mapping is
    staged; the caller still rolls back its transaction.
    """


class DeviceNumberRangeExhaustedError(DeviceNumberConflictError):
    """Every number of the person's range is already used in the school."""


class DeviceNumberChangeError(Exception):
    """Raised when an operator-entered device number is rejected.

    ``str(exc)`` is a user-facing Arabic message. Nothing has been modified
    when this is raised before the flush; after a failed flush the caller must
    roll back.
    """


def numeric_device_number(value):
    """Numeric value of a stored/typed number ('007' -> 7), or None.

    Only ASCII digits count: '²' or Arabic-Indic digits are not numbers here.
    """
    raw = (value or '').strip()
    return int(raw) if _ASCII_DIGITS.fullmatch(raw) else None


def _normalise_number(value):
    """Canonical form used for comparisons: '007' and '7' are the same user."""
    number = numeric_device_number(value)
    return str(number) if number is not None else (value or '').strip()


def lock_school_device_numbering(school_id):
    """Serialise every device-number write of one school (PostgreSQL only).

    Held until the caller's transaction ends. On any other backend the unique
    constraints plus the pre-insert checks remain in force.
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
        {'ns': _ADVISORY_LOCK_NAMESPACE, 'key': int(school_id)},
    )


def _school_used_numbers(school_id, exclude_student_id=None, exclude_employee_id=None):
    """Numeric values used by any student OR employee mapping of the school."""
    from app.models import db, DeviceStudentMapping, DeviceEmployeeMapping

    stu_q = (db.session.query(DeviceStudentMapping.employee_no_string)
             .execution_options(**_UNSCOPED)
             .filter(DeviceStudentMapping.school_id == school_id))
    if exclude_student_id is not None:
        stu_q = stu_q.filter(DeviceStudentMapping.student_id != exclude_student_id)
    emp_q = (db.session.query(DeviceEmployeeMapping.enrollment_no)
             .execution_options(**_UNSCOPED)
             .filter(DeviceEmployeeMapping.school_id == school_id))
    if exclude_employee_id is not None:
        emp_q = emp_q.filter(DeviceEmployeeMapping.employee_id != exclude_employee_id)

    used = set()
    for (value,) in stu_q.all() + emp_q.all():
        number = numeric_device_number(value)
        if number is not None:
            used.add(number)
    return used


def _device_used_numbers(device_ids, school_id, exclude_student_id=None,
                         exclude_employee_id=None):
    """``{device_id: {normalised number, ...}}`` from both tables."""
    from app.models import db, DeviceStudentMapping, DeviceEmployeeMapping

    used = {dev_id: set() for dev_id in device_ids}
    if not device_ids:
        return used
    stu_q = (db.session.query(DeviceStudentMapping.device_id,
                              DeviceStudentMapping.employee_no_string)
             .execution_options(**_UNSCOPED)
             .filter(DeviceStudentMapping.school_id == school_id,
                     DeviceStudentMapping.device_id.in_(device_ids)))
    if exclude_student_id is not None:
        stu_q = stu_q.filter(DeviceStudentMapping.student_id != exclude_student_id)
    emp_q = (db.session.query(DeviceEmployeeMapping.device_id,
                              DeviceEmployeeMapping.enrollment_no)
             .execution_options(**_UNSCOPED)
             .filter(DeviceEmployeeMapping.school_id == school_id,
                     DeviceEmployeeMapping.device_id.in_(device_ids)))
    if exclude_employee_id is not None:
        emp_q = emp_q.filter(DeviceEmployeeMapping.employee_id != exclude_employee_id)

    for dev_id, value in stu_q.all() + emp_q.all():
        used[dev_id].add(_normalise_number(value))
    return used


def _next_device_number(used, low, high):
    """Next free number inside ``low..high``, or None when the range is full."""
    in_range = [n for n in used if low <= n <= high]
    candidate = max(in_range) + 1 if in_range else low
    if candidate <= high:
        return candidate
    # The top of the range is taken: fall back to the lowest free number. The
    # loop stops at the first gap, so it never runs past len(used) + 1 steps.
    number = low
    while number <= high:
        if number not in used:
            return number
        number += 1
    return None


class _Kind:
    """Per-entity settings for the shared binding routine."""

    def __init__(self, model_name, person_attr, number_attr, scopes, low, high,
                 label, group_label, inconsistent_msg):
        self.model_name = model_name
        self.person_attr = person_attr
        self.number_attr = number_attr
        self.scopes = scopes
        self.low = low
        self.high = high
        self.label = label
        self.group_label = group_label
        self.inconsistent_msg = inconsistent_msg

    @property
    def model(self):
        import app.models as models
        return getattr(models, self.model_name)


_STUDENT = _Kind(
    'DeviceStudentMapping', 'student_id', 'employee_no_string',
    STUDENT_DEVICE_SCOPES, STUDENT_DEVICE_NUMBER_MIN, STUDENT_DEVICE_NUMBER_MAX,
    'الطالب', 'للطلاب',
    'أرقام هذا الطالب غير متطابقة بين الأجهزة. يرجى توحيد رقمه أولاً '
    'من صفحة ربط الطلاب عبر "تعديل رقم الطالب في الجهاز". '
    'لم يتم إنشاء أي ربط.')

_EMPLOYEE = _Kind(
    'DeviceEmployeeMapping', 'employee_id', 'enrollment_no',
    EMPLOYEE_DEVICE_SCOPES, EMPLOYEE_DEVICE_NUMBER_MIN, EMPLOYEE_DEVICE_NUMBER_MAX,
    'الموظف', 'للموظفين',
    'أرقام هذا الموظف غير متطابقة بين الأجهزة. يرجى توحيد رقمه يدوياً من صفحة '
    'أجهزة الحضور أولاً. لم يتم إنشاء أي ربط.')


def _ensure_device_mappings(kind, devices, person_id, school_id):
    """Bind one person to every eligible device in ``devices`` with ONE number.

    Returns ``[(mapping, created), ...]`` in the order of the eligible devices.

    * Devices of another school, or whose scope does not accept this kind of
      person, are ignored.
    * A device the person is already mapped to keeps its mapping untouched
      (``created=False``) — no second mapping and no renumbering.
    * For the missing devices:
        1. a person with no binding in this school gets the next free number
           of their range, free across BOTH tables of the school;
        2. a person with one consistent number reuses it; if it is taken on
           any missing device ``DeviceNumberConflictError`` is raised;
        3. a person whose bindings carry different numbers is refused with
           ``DeviceNumberConflictError`` until corrected explicitly;
        4. a full range raises ``DeviceNumberRangeExhaustedError``.
      Nothing is staged when one of these is raised.
    * New mappings are staged with ``db.session.flush()`` (NOT committed — the
      caller owns the transaction).

    Raises ``DeviceNumberAllocationError`` if the insert violates a device
    uniqueness constraint; the caller must roll back.
    """
    from app.models import db

    model = kind.model
    person_col = getattr(model, kind.person_attr)
    number_col = getattr(model, kind.number_attr)

    targets = []
    seen = set()
    for dev in devices:
        if (dev is None or dev.id in seen or dev.school_id != school_id
                or getattr(dev, 'device_scope', 'students') not in kind.scopes):
            continue
        seen.add(dev.id)
        targets.append(dev)
    if not targets:
        return []

    def _existing():
        rows = (db.session.query(model)
                .execution_options(**_UNSCOPED)
                .filter(model.school_id == school_id,
                        person_col == person_id,
                        model.device_id.in_([dev.id for dev in targets]))
                .order_by(model.id)
                .all())
        found = {}
        for row in rows:
            found.setdefault(row.device_id, row)
        return found

    existing = _existing()
    missing = [dev for dev in targets if dev.id not in existing]

    if missing:
        lock_school_device_numbering(school_id)
        # Re-check under the lock: a concurrent request may have mapped this
        # person while we were waiting.
        existing = _existing()
        missing = [dev for dev in targets if dev.id not in existing]

    if missing:
        own_numbers = {
            _normalise_number(value) for (value,) in
            db.session.query(number_col)
            .execution_options(**_UNSCOPED)
            .filter(model.school_id == school_id, person_col == person_id)
            .all()
        }
        if len(own_numbers) > 1:
            raise DeviceNumberConflictError(kind.inconsistent_msg)
        if own_numbers:
            number = own_numbers.pop()
            used_by_device = _device_used_numbers([dev.id for dev in missing], school_id)
            busy = [dev.name for dev in missing if number in used_by_device[dev.id]]
            if busy:
                raise DeviceNumberConflictError(
                    f'رقم {kind.label} {number} مستخدم لشخص آخر على الجهاز: '
                    f'{"، ".join(busy)}. لم يتم إنشاء أي ربط؛ يرجى تعديل رقم '
                    f'{kind.label} إلى رقم متاح أولاً.')
        else:
            allocated = _next_device_number(_school_used_numbers(school_id),
                                            kind.low, kind.high)
            if allocated is None:
                raise DeviceNumberRangeExhaustedError(
                    f'لا يوجد رقم متاح على جهاز الحضور {kind.group_label} ضمن النطاق '
                    f'المحدد {kind.low}-{kind.high}. لم يتم حفظ أي تغيير.')
            number = str(allocated)

        for dev in missing:
            mapping = model(school_id=school_id, device_id=dev.id, is_active=True,
                            **{kind.person_attr: person_id, kind.number_attr: number})
            db.session.add(mapping)
            existing[dev.id] = mapping
        try:
            db.session.flush()
        except IntegrityError as exc:
            raise DeviceNumberAllocationError(str(exc)[:500]) from exc

    created_ids = {dev.id for dev in missing}
    return [(existing[dev.id], dev.id in created_ids) for dev in targets]


# ─── Students ────────────────────────────────────────────────────────────────

def ensure_student_device_mapping(device, student_id, school_id):
    """Return ``(mapping, created)`` for this student on this device.

    Single-device form of ``ensure_student_device_mappings``. Raises
    ``DeviceNumberConflictError`` when the device does not accept students.
    """
    result = ensure_student_device_mappings([device], student_id, school_id)
    if not result:
        raise DeviceNumberConflictError('هذا الجهاز لا يقبل ربط الطلاب.')
    return result[0]


def ensure_student_device_mappings(devices, student_id, school_id):
    """Bind a student to every ``students``/``mixed`` device in ``devices``
    with ONE shared number from the student range (1-999).

    See ``_ensure_device_mappings`` for the full contract.
    """
    return _ensure_device_mappings(_STUDENT, devices, student_id, school_id)


def change_student_device_number(student_id, school_id, new_number):
    """Set one explicit number on ALL of a student's mappings in this school.

    Only the stored bindings change; the student record, its school serial
    number and attendance history are untouched, and nothing is sent to a
    physical device. The new number must lie in the student range and be free
    across both tables of the school.

    Every target device is validated before anything is modified. Returns
    ``(mappings, changed)``. Raises ``DeviceNumberChangeError`` with an Arabic
    message on invalid input or a collision; after a failed flush the caller
    must roll back.
    """
    from app.models import db, DeviceStudentMapping

    value = numeric_device_number(new_number)
    if (value is None or value < STUDENT_DEVICE_NUMBER_MIN
            or value > STUDENT_DEVICE_NUMBER_MAX):
        raise DeviceNumberChangeError(
            f'رقم الطالب في الجهاز يجب أن يكون عدداً صحيحاً بين '
            f'{STUDENT_DEVICE_NUMBER_MIN} و{STUDENT_DEVICE_NUMBER_MAX}.')
    number = str(value)

    mappings = (DeviceStudentMapping.query
                .filter(DeviceStudentMapping.student_id == student_id,
                        DeviceStudentMapping.school_id == school_id)
                .order_by(DeviceStudentMapping.device_id)
                .all())
    if not mappings:
        raise DeviceNumberChangeError('لا توجد ربطات أجهزة لهذا الطالب.')

    if all(m.employee_no_string == number for m in mappings):
        return mappings, False

    lock_school_device_numbering(school_id)

    used_by_device = _device_used_numbers([m.device_id for m in mappings], school_id,
                                          exclude_student_id=student_id)
    conflicts = [m.device.name for m in mappings if number in used_by_device[m.device_id]]
    if conflicts:
        raise DeviceNumberChangeError(
            f'الرقم {number} مستخدم لشخص آخر على الجهاز: {"، ".join(conflicts)}. '
            f'لم يتم تعديل أي ربط.')
    if value in _school_used_numbers(school_id, exclude_student_id=student_id):
        raise DeviceNumberChangeError(
            f'الرقم {number} مستخدم لشخص آخر (طالب أو موظف) على جهاز آخر في هذه '
            f'المدرسة. لم يتم تعديل أي ربط.')

    for m in mappings:
        m.employee_no_string = number
    try:
        db.session.flush()
    except IntegrityError as exc:
        raise DeviceNumberChangeError(
            'تعذر تعديل رقم الطالب بسبب تعارض في الأرقام. لم يتم تعديل أي ربط.') from exc
    return mappings, True


def copy_student_mappings(source_dev, target_dev, school_id):
    """Copy the active student mappings of ``source_dev`` to ``target_dev``.

    Each student keeps their existing number — a copy never picks another
    one. A row is not copied when the student is already bound to the target
    (``conflicts``) or when the number is used by another person on the target,
    in either table (``skipped``). Numeric values are written in canonical
    form ('007' -> '7'); the source rows are not modified.

    Returns ``(copied, skipped, conflicts)``; staged with a flush, never
    committed. Raises ``DeviceNumberConflictError`` when the target does not
    accept students, and ``DeviceNumberAllocationError`` on a failed flush.
    """
    from app.models import db, DeviceStudentMapping

    if (target_dev.school_id != school_id or source_dev.school_id != school_id
            or getattr(target_dev, 'device_scope', 'students') not in STUDENT_DEVICE_SCOPES):
        raise DeviceNumberConflictError(
            'لا يمكن نسخ ربطات الطلاب إلى جهاز مخصص للموظفين فقط.')

    lock_school_device_numbering(school_id)

    source_mappings = (DeviceStudentMapping.query
                       .execution_options(**_UNSCOPED)
                       .filter_by(device_id=source_dev.id, school_id=school_id,
                                  is_active=True)
                       .order_by(DeviceStudentMapping.id)
                       .all())
    target_students = {
        row[0] for row in
        db.session.query(DeviceStudentMapping.student_id)
        .execution_options(**_UNSCOPED)
        .filter(DeviceStudentMapping.device_id == target_dev.id,
                DeviceStudentMapping.school_id == school_id)
        .all()
    }
    target_used = _device_used_numbers([target_dev.id], school_id)[target_dev.id]

    copied = skipped = conflicts = 0
    for src in source_mappings:
        if src.student_id in target_students:
            conflicts += 1
            continue
        number = _normalise_number(src.employee_no_string)
        if number in target_used:
            skipped += 1
            continue
        db.session.add(DeviceStudentMapping(
            school_id=school_id, device_id=target_dev.id,
            employee_no_string=number, student_id=src.student_id, is_active=True,
        ))
        target_students.add(src.student_id)
        target_used.add(number)
        copied += 1
    try:
        db.session.flush()
    except IntegrityError as exc:
        raise DeviceNumberAllocationError(str(exc)[:500]) from exc
    return copied, skipped, conflicts


# ─── Employees ───────────────────────────────────────────────────────────────

def ensure_employee_device_mappings(devices, employee_id, school_id):
    """Bind an employee to every ``employees``/``mixed`` device in ``devices``
    with ONE shared number from the employee range (1000-MAX_DEVICE_NUMBER).

    See ``_ensure_device_mappings`` for the full contract.
    """
    return _ensure_device_mappings(_EMPLOYEE, devices, employee_id, school_id)


def employee_target_devices(school_id):
    """Active ``employees``/``mixed`` devices of the school, or [] when the
    attendance-device mappings feature is off for that school."""
    from app.models import AttendanceDevice
    from app.utils.features import is_feature_enabled

    if not school_id or not is_feature_enabled(school_id, 'attendance_devices.mappings'):
        return []
    return (AttendanceDevice.query
            .execution_options(**_UNSCOPED)
            .filter(AttendanceDevice.school_id == school_id,
                    AttendanceDevice.is_active.is_(True),
                    AttendanceDevice.device_scope.in_(EMPLOYEE_DEVICE_SCOPES))
            .order_by(AttendanceDevice.id)
            .all())


def map_new_employee_to_devices(employee_id, school_id):
    """Database-only binding of a NEWLY created employee (flush, no commit).

    Call it only from an Employee creation path, before that path's commit.
    No device available → nothing is created and no number is reserved.
    Raises ``DeviceNumberAllocationError`` (or a subclass); the caller must
    roll back the whole creation.
    """
    devices = employee_target_devices(school_id)
    if not devices:
        return []
    return ensure_employee_device_mappings(devices, employee_id, school_id)


def ensure_employee_device_mapping(device, employee_id, school_id):
    """Return ``(mapping, created)`` for this employee on THIS device only.

    Single-device form of ``ensure_employee_device_mappings``, used by the
    manual "add employee mapping" action. The number is always chosen by the
    server: the employee's one consistent existing number is reused (even a
    legacy one below 1000), otherwise a new employee-range number is
    allocated; inconsistent numbers are refused. An existing mapping on this
    device is returned untouched with ``created=False``. Raises
    ``DeviceNumberConflictError`` when the device does not accept employees.
    """
    result = ensure_employee_device_mappings([device], employee_id, school_id)
    if not result:
        raise DeviceNumberConflictError('هذا الجهاز لا يقبل ربط الموظفين.')
    return result[0]

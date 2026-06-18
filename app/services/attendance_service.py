"""
Shared attendance processing logic.
Extracted here so both the REST API endpoint and the Hikvision sync service
can reuse the same check-in / check-out logic without circular imports.
"""
from app.models import db, Device, Student, StudentAttendance, StudentSuspension
from app.utils.attendance_helpers import get_local_now, determine_check_in_status, get_student_shift


def process_attendance_punch(student, school, punch_dt, source='api', dedup_tag=None):
    """
    Process a raw attendance punch using the school's configured attendance rules.

    student   — Student ORM object (already resolved by the caller)
    school    — School or SchoolSettings object supplying att_late_threshold
                and att_departure_time
    punch_dt  — timezone-naive datetime in school local time
    source    — value written to StudentAttendance.source ('aiface', 'rfid', …)
    dedup_tag — optional string embedded in notes; if already present the punch
                is treated as a duplicate and skipped

    Returns (action, row) where action is one of:
        'check_in'           — new record created
        'check_out'          — check_out updated on the existing record
        'already_checked_in' — record exists but departure window not yet open
        'duplicate'          — dedup_tag already found in existing notes
    """
    today    = punch_dt.date()
    now_time = punch_dt.time().replace(microsecond=0)

    existing = StudentAttendance.query.filter_by(
        student_id=student.id, date=today
    ).first()

    # Exact-punch dedup: same device/enrollid/timestamp already recorded
    if dedup_tag and existing and existing.notes and dedup_tag in existing.notes:
        return 'duplicate', existing

    if existing:
        # Actual attendance overrides an approved-leave record: a device scan
        # is proof the student physically arrived, regardless of leave status.
        if existing.status == 'on_leave' and existing.source == 'leave':
            _shift = get_student_shift(student, school)
            status = determine_check_in_status(now_time, school, shift=_shift)
            existing.status   = status
            existing.check_in = now_time
            existing.source   = source
            if dedup_tag:
                existing.notes = (
                    (existing.notes or '').rstrip() + ' ' + dedup_tag
                ).strip()
            db.session.commit()
            return 'check_in', existing

        # Checkout cutoff: prefer the student's shift dismissal_time when shift
        # mode is on and a shift resolves; otherwise fall back to the school
        # default att_departure_time (normal mode — unchanged behaviour).
        _co_shift = get_student_shift(student, school)
        departure = (_co_shift.dismissal_time
                     if _co_shift and getattr(_co_shift, 'dismissal_time', None)
                     else getattr(school, 'att_departure_time', None))
        if departure and now_time >= departure and existing.check_out is None:
            existing.check_out = now_time
            if dedup_tag:
                existing.notes = ((existing.notes or '') + ' ' + dedup_tag).strip()
            db.session.commit()
            return 'check_out', existing
        return 'already_checked_in', existing

    # New check-in — academic_year_id auto-derived from punch date by scoping system
    from sqlalchemy.exc import IntegrityError
    _shift = get_student_shift(student, school)
    status = determine_check_in_status(now_time, school, shift=_shift)
    row = StudentAttendance(
        student_id = student.id,
        school_id  = student.school_id,
        date       = today,
        status     = status,
        check_in   = now_time,
        source     = source,
        notes      = dedup_tag,
        shift_id   = _shift.id if _shift else None,
    )
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = StudentAttendance.query.filter_by(
            student_id=student.id, date=today
        ).first()
        return 'already_checked_in', existing
    return 'check_in', row


def process_student_scan(student_id_str, device_sn_str=None,
                         local_dt=None, hik_serial_no=None, school=None):
    """
    Core check-in / check-out logic.

    student_id_str  — public code like 'STU-00032'
    device_sn_str   — device serial; used to look up school from the Device
                      (hardware RFID) table unless `school` is provided directly
    local_dt        — pre-converted school-timezone datetime; falls back to
                      get_local_now(school) when None
    hik_serial_no   — Hikvision event serialNo for dedup tagging in notes;
                      None for normal REST calls
    school          — School / SchoolSettings object; when supplied the Device
                      table lookup is skipped (used by the Hikvision service)

    Returns (result_dict, http_status_int).
    """
    if school is None:
        if not device_sn_str:
            return {"ok": False, "error": "device_sn required"}, 400
        device = Device.query.filter_by(device_id=device_sn_str, is_active=True).first()
        if not device:
            return {"ok": False, "error": "invalid device"}, 403
        school = device.school
        # Bind this request to the device's school so the student lookup below is
        # tenant-scoped. Without this the lookup runs globally, allowing a caller
        # with one school's device serial to read/write another school's students.
        from app.utils.scoping import set_hardware_scope
        set_hardware_scope(device)

    student = Student.query.filter_by(student_id=student_id_str).first()
    if not student:
        return {"ok": False, "error": "student not found"}, 404

    if local_dt is None:
        local_dt = get_local_now(school)
    today    = local_dt.date()
    now_time = local_dt.time().replace(microsecond=0)

    suspended = StudentSuspension.query.filter(
        StudentSuspension.student_id == student.id,
        StudentSuspension.start_date <= today,
        StudentSuspension.end_date   >= today,
    ).first()
    if suspended:
        return {"ok": False, "error": "student suspended"}, 403

    def _fmt(t):
        return t.strftime('%H:%M') if t else None

    existing = StudentAttendance.query.filter_by(
        student_id=student.id, date=today
    ).first()

    # Dedup: skip if this exact Hikvision serialNo is already in notes
    if hik_serial_no is not None and existing:
        sn_tag = f'hik:sn={hik_serial_no}'
        if existing.notes and sn_tag in existing.notes:
            return {
                "ok":           True,
                "action":       "skipped",
                "reason":       "duplicate_event",
                "serial_no":    hik_serial_no,
                "student_name": student.full_name,
                "student_id":   student.student_id,
                "date":         today.isoformat(),
                "check_in":     _fmt(existing.check_in),
                "check_out":    _fmt(existing.check_out),
                "status":       existing.status,
            }, 200

    src = 'hikvision' if hik_serial_no is not None else 'api'

    if existing:
        # Actual attendance overrides an approved-leave record: a device scan
        # is proof the student physically arrived, regardless of leave status.
        if existing.status == 'on_leave' and existing.source == 'leave':
            _shift = get_student_shift(student, school)
            status = determine_check_in_status(now_time, school, shift=_shift)
            existing.status   = status
            existing.check_in = now_time
            existing.source   = src
            if hik_serial_no is not None:
                existing.notes = (
                    (existing.notes or '').rstrip() + f' hik:sn={hik_serial_no}'
                ).strip()
            db.session.commit()
            return {
                "ok":           True,
                "action":       "check_in",
                "student_name": student.full_name,
                "student_id":   student.student_id,
                "date":         today.isoformat(),
                "check_in":     now_time.strftime('%H:%M'),
                "check_out":    None,
                "status":       status,
            }, 200

        # Checkout cutoff: prefer shift dismissal_time when in shift mode with a
        # resolved shift; otherwise the school default att_departure_time.
        _co_shift = get_student_shift(student, school)
        departure = (_co_shift.dismissal_time
                     if _co_shift and getattr(_co_shift, 'dismissal_time', None)
                     else getattr(school, 'att_departure_time', None))
        if departure and now_time >= departure:
            if existing.check_out is None:
                existing.check_out = now_time
                if hik_serial_no is not None:
                    existing.notes = (
                        (existing.notes or '') + f' hik:sn={hik_serial_no}'
                    ).strip()
                db.session.commit()
            return {
                "ok":           True,
                "action":       "check_out",
                "student_name": student.full_name,
                "student_id":   student.student_id,
                "date":         today.isoformat(),
                "check_in":     _fmt(existing.check_in),
                "check_out":    _fmt(existing.check_out),
                "status":       existing.status,
            }, 200
        return {
            "ok":           True,
            "action":       "already_checked_in",
            "student_name": student.full_name,
            "student_id":   student.student_id,
            "date":         today.isoformat(),
            "check_in":     _fmt(existing.check_in),
            "check_out":    _fmt(existing.check_out),
            "status":       existing.status,
        }, 200

    # ── Check-in path ──────────────────────────────────────────────────────────
    _shift = get_student_shift(student, school)
    status = determine_check_in_status(now_time, school, shift=_shift)
    attendance = StudentAttendance(
        student_id = student.id,
        school_id  = student.school_id,
        date       = today,
        status     = status,
        check_in   = now_time,
        source     = src,
        notes      = f'hik:sn={hik_serial_no}' if hik_serial_no is not None else None,
        shift_id   = _shift.id if _shift else None,
    )
    db.session.add(attendance)
    db.session.commit()

    return {
        "ok":           True,
        "action":       "check_in",
        "student_name": student.full_name,
        "student_id":   student.student_id,
        "date":         today.isoformat(),
        "check_in":     now_time.strftime('%H:%M'),
        "check_out":    None,
        "status":       status,
    }, 200

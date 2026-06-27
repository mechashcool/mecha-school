"""
AI Face 11 — WebSocket Attendance Receiver Service
====================================================
Protocol  : WebSocket + JSON  (device → server and server → device)
Port      : 7788  (override with env  AIFACE_WS_PORT=<n>)
Enable    : always on by default; set AIFACE_WS_ENABLED=false to disable

Device → server messages
─────────────────────────
  {"cmd":"reg",     "sn":"STC010083281", "modelname":"AiFace",
   "usersize":5000, "facesize":5000, "logsize":500000,
   "firmware":"ai518_f43h_v1.27"}

  {"cmd":"sendlog", "sn":"STC010083281", "count":18, "logindex":12345,
   "record":[
     {"enrollid":1001,"name":"","time":"2026-05-26 21:49:51",
      "mode":3,"inout":0,"event":0}
   ]}

  {"cmd":"senduser", ...}   (shouldn't arrive — we send nosenduser:true)

Server → device messages (outbound commands)
─────────────────────────────────────────────
  {"ret":"reg",     "result":true,  "cloudtime":"...", "nosenduser":true}
  {"ret":"sendlog", "result":true,  "count":N, "logindex":M,
   "cloudtime":"...", "access":1}
  {"cmd":"getnewlog",  "stn":true|false}
  {"cmd":"setuserinfo","enrollid":N,"name":"...","admin":0,...}
  {"cmd":"deleteuser", "enrollid":N,"backupnum":13}

Device → server (responses to server commands)
───────────────────────────────────────────────
  {"ret":"getnewlog",   "count":N, "logindex":M, "record":[...]}
  {"ret":"setuserinfo", "result":true|false}
  {"ret":"deleteuser",  "result":true|false}

  NOTE: some firmware versions echo the command key instead of using ret=,
  e.g. {"cmd":"getnewlog","count":0,...}.  Both forms are handled.

Enrollment ID mapping
─────────────────────
  enrollid on the device → DeviceStudentMapping.employee_no_string
  Add mappings via Admin → Attendance Devices → Mappings in the web UI.

Attendance processing
─────────────────────
  Each normal sendlog/getnewlog record (event=0) is passed to
  process_attendance_punch() which applies the school's att_late_threshold
  and att_departure_time rules — identical to the logic used by RFID endpoints.
  Dedup tag format: "AI Face YYYY-MM-DD HH:MM:SS"
  A punch whose tag is already in notes is silently skipped.

Offline log recovery
─────────────────────
  On every device reconnect (cmd=reg), after acknowledging registration,
  the server automatically sends {"cmd":"getnewlog","stn":true} to pull
  any attendance logs recorded while the server was offline.
  The device returns batches; the server processes each and requests the
  next (stn=false) until count=0.  Same dedup protection applies.
  Initial pull timeout: 15 s per exchange.

Periodic log polling (checkout reliability)
────────────────────────────────────────────
  Many AI Face devices send realtime sendlog only for the first scan of
  the day (check-in) and store subsequent scans (checkout) locally.
  To catch these missed checkout logs without waiting for a reconnect,
  the server polls getnewlog(stn=false) every AIFACE_POLL_INTERVAL seconds
  (default 60) while the device stays connected.  Dedup via notes prevents
  double-processing if a log arrives via both sendlog and the periodic poll.
  Periodic poll timeout: 10 s (short — if device is alive it responds fast).

  Duplicate task prevention: each periodic task is bound to the specific
  connection entry (my_entry).  When the device reconnects and creates a
  new entry, the old task detects the mismatch and exits cleanly.

  Stale detection: after AIFACE_GETNEWLOG_STALE_THRESHOLD consecutive
  getnewlog timeouts (default 3), the device is marked stale and the
  periodic poll stops.  The stale flag is cleared automatically on the
  next cmd=reg (reconnect), so polling always resumes after reconnect.

Pending command queue
─────────────────────
  If an admin issues a setuserinfo or deleteuser while the device is offline,
  the command is held in _pending_commands[sn] (in-memory).  On the next reg,
  after the missed-log pull, queued commands are executed in order.
  NOTE: the queue is lost on server restart — for durable queuing add a DB
  model and migrate accordingly.

Device status API
─────────────────
  get_device_status(sn)    → dict with last_reg_at, last_sendlog_at,
                             last_getnewlog_success_at, getnewlog_timeout_count,
                             getnewlog_stale, poll_task_running, last_disconnect_at
  get_all_device_status()  → {sn: status_dict, ...} for all known devices
"""

import asyncio
import errno as _errno
import json
import logging
import os
import socket as _socket
import threading
from datetime import datetime

log = logging.getLogger("aiface_ws")

_WS_PORT           = int(os.environ.get("AIFACE_WS_PORT", 7788))
_flask_app         = None   # set by start_ai_face_ws_server()
_ws_loop           = None   # asyncio event loop; set by _serve()
_connections: dict = {}     # sn → {"ws": websocket, "pending": {cmd_key: Future}, "poll_task": Task|None}
_pending_commands: dict = {}  # sn → [{"payload": dict, "note": str}]
_device_status: dict = {}   # sn → status dict (see module docstring)

# After this many consecutive getnewlog timeouts the periodic poll stops.
# Resets to 0 on the next cmd=reg (device reconnect).
_GETNEWLOG_STALE_THRESHOLD = int(os.environ.get("AIFACE_GETNEWLOG_STALE_THRESHOLD", "3"))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _cloudtime() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _json_reply(**kw) -> str:
    return json.dumps(kw, ensure_ascii=False)


def _log_task_exception(task: asyncio.Task) -> None:
    """Done-callback: log any unhandled exception from a fire-and-forget task."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        log.error("[aiface] background task '%s' raised: %s",
                  task.get_name(), exc, exc_info=exc)


# ── Public API — connection status & command queueing ─────────────────────────

class DeviceOfflineError(Exception):
    """Raised when a command cannot be delivered because the device is not connected."""


def get_connected_sns() -> list:
    """Return list of device SNs currently connected via WebSocket."""
    return list(_connections.keys())


def is_device_connected(sn: str) -> bool:
    """Return True if the device with the given SN is currently connected."""
    return bool(_connections.get(sn))


def queue_command_for_device(sn: str, payload: dict, note: str = "") -> None:
    """
    Queue a command for a currently-offline device.
    The command is executed automatically after the device's next reg + log-pull.
    """
    if sn not in _pending_commands:
        _pending_commands[sn] = []
    _pending_commands[sn].append({"payload": payload, "note": note})
    log.info("[aiface] queued pending cmd=%s enrollid=%s for offline sn=%s  note=%s",
             payload.get("cmd"), payload.get("enrollid", ""), sn, note)


def get_device_status(sn: str) -> dict:
    """Return a copy of the status dict for the given device SN."""
    st = dict(_device_status.get(sn, {}))
    # Augment with live poll-task state
    entry = _connections.get(sn)
    task  = entry.get("poll_task") if entry else None
    st["poll_task_running"] = bool(task and not task.done())
    st["ws_connected"]      = bool(entry)
    return st


def get_all_device_status() -> dict:
    """Return status dicts for all known devices, keyed by SN."""
    return {sn: get_device_status(sn) for sn in set(list(_device_status) + list(_connections))}


# ── Outbound command API (sync wrapper for Flask handlers) ─────────────────────

async def _send_and_wait(sn: str, payload: dict, timeout: float) -> dict:
    """Async: send JSON command to connected device and await the ret= response."""
    entry = _connections.get(sn)
    if not entry:
        raise DeviceOfflineError(f"Device {sn} is not connected")
    ws = entry["ws"]
    ret_key = payload.get("cmd", "")
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    entry["pending"][ret_key] = fut
    try:
        raw_out = json.dumps(payload, ensure_ascii=False)
        await ws.send(raw_out)
        log.info("[aiface] → sn=%s cmd=%s payload=%s", sn, ret_key, raw_out)
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"Device {sn} did not respond to cmd={ret_key!r} within {timeout}s"
        )
    finally:
        entry["pending"].pop(ret_key, None)


def send_command_to_device(sn: str, payload: dict, timeout: float = 10) -> dict:
    """
    Synchronous wrapper — send a command to an AI Face device and return
    the device's JSON response dict.  Safe to call from Flask request handlers.

    Raises:
        DeviceOfflineError  — device not currently connected via WebSocket
        TimeoutError        — device did not respond within `timeout` seconds
        RuntimeError        — WS server thread not started
    """
    if _ws_loop is None:
        raise RuntimeError("AI Face WS server is not running")
    if not _connections.get(sn):
        raise DeviceOfflineError(f"Device {sn} is not connected")
    future = asyncio.run_coroutine_threadsafe(
        _send_and_wait(sn, payload, timeout), _ws_loop
    )
    return future.result(timeout=timeout + 2)  # +2 for scheduling overhead


# ── Shared attendance record processing ───────────────────────────────────────

def _process_record_list(sn: str, device, school, records: list,
                         source_cmd: str = "sendlog") -> tuple:
    """
    Process a list of raw attendance records from the device.
    Behaviour depends on device.device_scope:
      'students'  — only DeviceStudentMapping → StudentAttendance
      'employees' — only DeviceEmployeeMapping → EmployeeAttendance
      'mixed'     — try student first, then employee fallback
    Returns (processed, skipped, unmatched, errors).
    """
    from app.models import DeviceStudentMapping, Student
    from app.services.attendance_service import process_attendance_punch
    from app.services.notifications import NotificationService

    scope = getattr(device, 'device_scope', 'students')
    processed = skipped = unmatched = errors = 0

    for rec in records:
        enrollid = rec.get("enrollid")
        time_str = rec.get("time", "")
        event    = rec.get("event", 0)
        inout    = rec.get("inout", 0)
        mode     = rec.get("mode", 0)

        log.warning("[aiface-log] raw record enrollid=%s name=%r time=%r sn=%s source=%s",
                    enrollid, rec.get("name"), time_str, sn, source_cmd)

        if rec.get("image"):
            log.debug("  [snapshot] enrollid=%s image %d chars — not stored",
                      enrollid, len(rec["image"]))

        if event != 0:
            log.debug("  Skipping enrollid=%s event=%d (not normal attendance)",
                      enrollid, event)
            skipped += 1
            continue

        try:
            punch_dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            log.warning("  Bad time string %r for enrollid=%s — skipping", time_str, enrollid)
            errors += 1
            continue

        dedup_tag = f"AI Face {time_str}"

        log.warning("[aiface-log] lookup device_id=%d device_sn=%s enrollid=%s scope=%s",
                    device.id, sn, enrollid, scope)

        # ── Employee path ─────────────────────────────────────────────────────
        if scope == 'employees':
            result = _process_employee_punch(
                device, school, sn, enrollid, punch_dt, dedup_tag, source_cmd)
            if result == 'processed':
                processed += 1
            elif result == 'skipped':
                skipped += 1
            elif result == 'error':
                errors += 1
            else:
                unmatched += 1
            continue

        # ── Student path ──────────────────────────────────────────────────────
        mapping = (DeviceStudentMapping.query
                   .execution_options(bypass_tenant_scope=True)
                   .filter_by(device_id=device.id,
                              employee_no_string=str(enrollid),
                              is_active=True)
                   .first())

        # Log student mapping result
        _all_stu = (DeviceStudentMapping.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter_by(device_id=device.id,
                               employee_no_string=str(enrollid))
                    .all())
        log.warning("[aiface-log] student mapping candidates count=%d (any is_active) "
                    "device_id=%d enrollid=%s",
                    len(_all_stu), device.id, enrollid)
        if mapping:
            log.warning("[aiface-log] student mapping found id=%d student_id=%d",
                        mapping.id, mapping.student_id)
        else:
            log.warning("[aiface-log] no student mapping device_id=%d enrollid=%s",
                        device.id, enrollid)

        if not mapping:
            if scope == 'mixed':
                result = _process_employee_punch(
                    device, school, sn, enrollid, punch_dt, dedup_tag, source_cmd)
                if result == 'processed':
                    processed += 1
                elif result == 'skipped':
                    skipped += 1
                elif result == 'error':
                    errors += 1
                else:
                    log.warning(
                        "  [aiface] No mapping for device_id=%d scope=%s enrollid=%s source=%s",
                        device.id, scope, enrollid, source_cmd)
                    unmatched += 1
            else:
                # scope == 'students' (default): an employee punch on a student-only
                # device is never tried against employee mappings. This is the most
                # common reason employee attendance "does not record" from a device.
                log.warning(
                    "  [aiface] No student mapping for device_id=%d scope=%s enrollid=%s "
                    "source=%s — if this enrollid is an EMPLOYEE, set the device scope to "
                    "'employees' or 'mixed' and add a DeviceEmployeeMapping.",
                    device.id, scope, enrollid, source_cmd)
                unmatched += 1
            continue

        student = (Student.query
                   .execution_options(bypass_tenant_scope=True)
                   .filter_by(id=mapping.student_id)
                   .first())
        if not student:
            log.warning("  [aiface] student_id=%d not found for enrollid=%s",
                        mapping.student_id, enrollid)
            unmatched += 1
            continue

        # ── Detailed diagnostic logging ────────────────────────────────────────
        from app.models import StudentAttendance as _SA
        _existing_pre = (_SA.query
                         .execution_options(bypass_tenant_scope=True)
                         .filter_by(student_id=student.id, date=punch_dt.date())
                         .first())
        _departure = getattr(school, 'att_departure_time', None)
        log.info(
            "  [aiface] PUNCH source=%s sn=%s device_id=%d enrollid=%s "
            "record_time=%r punch_dt=%s "
            "student_id=%d student=%s "
            "existing_check_in=%s existing_check_out=%s existing_status=%s "
            "school_departure=%s dedup_tag=%r",
            source_cmd, sn, device.id, enrollid,
            time_str, punch_dt.strftime('%Y-%m-%d %H:%M:%S'),
            student.id, student.full_name,
            getattr(_existing_pre, 'check_in', None),
            getattr(_existing_pre, 'check_out', None),
            getattr(_existing_pre, 'status', None),
            _departure, dedup_tag,
        )

        try:
            action, row = process_attendance_punch(
                student=student, school=school, punch_dt=punch_dt,
                source='aiface', dedup_tag=dedup_tag,
            )
        except Exception:
            log.exception("  [aiface] Attendance engine error for student_id=%d", student.id)
            errors += 1
            continue

        # Log action + detailed reason
        _punch_time = punch_dt.time()
        if action == 'check_in':
            _reason = 'no record today → new check-in created'
        elif action == 'check_out':
            _reason = (f'departure window open (punch {_punch_time} >= departure {_departure})'
                       f' — realtime={source_cmd == "sendlog"}')
        elif action == 'already_checked_in':
            if _existing_pre is None:
                _reason = 'race condition (no existing before call, but record appeared)'
            elif _existing_pre.check_out is not None:
                _reason = f'check_out already set ({_existing_pre.check_out}) — duplicate punch'
            elif not _departure:
                _reason = 'no departure time configured for this school'
            elif _punch_time < _departure:
                _reason = (f'punch {_punch_time} < departure {_departure} '
                           f'— not in departure window yet')
            else:
                _reason = 'already_checked_in (departure window open but check_out was set)'
        elif action == 'duplicate':
            _reason = 'dedup_tag already found in notes — exact same punch seen before'
        else:
            _reason = 'unknown'

        log.info(
            "  [aiface] RESULT source=%s enrollid=%s student_id=%d action=%s reason=%s",
            source_cmd, enrollid, student.id, action, _reason,
        )

        if action in ('check_in', 'check_out'):
            processed += 1
            scan_time = punch_dt.time().replace(microsecond=0)

            if action == 'check_in':
                if row.status == 'present':
                    title = 'حضور الطالب في الوقت المحدد'
                    body  = (f'طالبك {student.full_name} وصل في الوقت المحدد '
                             f'الساعة {scan_time.strftime("%H:%M")}.')
                else:
                    title = 'تأخر الطالب عن موعد الحضور'
                    body  = (f'طالبك {student.full_name} وصل متأخراً '
                             f'الساعة {scan_time.strftime("%H:%M")}.')
            else:
                title = 'خروج الطالب من المدرسة'
                body  = (f'طالبك {student.full_name} غادر المدرسة '
                         f'الساعة {scan_time.strftime("%H:%M")}.')
                if source_cmd == 'getnewlog':
                    log.info(
                        "  [aiface] CHECKOUT via getnewlog (not realtime) "
                        "student_id=%d — device did not send sendlog for this scan",
                        student.id,
                    )

            try:
                from app.services.notifications import NotificationService
                NotificationService.send_to_parents_of_student(
                    student.id, title, body, ntype='attendance',
                    data={'action': action, 'status': row.status,
                          'at': scan_time.strftime('%H:%M'),
                          'date': punch_dt.date().isoformat(),
                          'source': 'aiface', 'device_sn': sn,
                          'screen': 'attendance'},
                )
                log.info("  [aiface] parent notification sent: student_id=%d action=%s",
                         student.id, action)
            except Exception:
                log.exception("  [aiface] Notification error for student_id=%d", student.id)
        else:
            skipped += 1

    return processed, skipped, unmatched, errors


def _process_employee_punch(device, school, sn: str, enrollid, punch_dt,
                             dedup_tag: str, source_cmd: str) -> str:
    """
    Look up DeviceEmployeeMapping and create/update EmployeeAttendance.
    Returns: 'processed' | 'skipped' | 'unmatched' | 'error'
    """
    from app.models import DeviceEmployeeMapping, EmployeeAttendance, Employee, db
    from app.utils.decorators import get_active_year
    from sqlalchemy.exc import IntegrityError

    log.warning("[aiface-log] employee mapping lookup device_id=%d enrollid=%s is_active=True",
                device.id, enrollid)

    emp_mapping = (DeviceEmployeeMapping.query
                   .execution_options(bypass_tenant_scope=True)
                   .filter_by(device_id=device.id,
                              enrollment_no=str(enrollid),
                              is_active=True)
                   .first())

    if not emp_mapping:
        # Dump every employee mapping row for this device so we can diagnose
        # whether enrollment_no format or is_active flag is the mismatch.
        _all = (DeviceEmployeeMapping.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(device_id=device.id)
                .all())
        log.warning("[aiface-log] employee mapping NOT found — "
                    "all rows for device_id=%d: count=%d",
                    device.id, len(_all))
        for _m in _all:
            log.warning("[aiface-log]   row id=%d device_id=%d enrollment_no=%r "
                        "is_active=%s employee_id=%d",
                        _m.id, _m.device_id, _m.enrollment_no,
                        _m.is_active, _m.employee_id)
        log.warning("[aiface-log] no employee mapping found enrollid=%s device_id=%d device_sn=%s",
                    enrollid, device.id, sn)
        return 'unmatched'

    log.warning("[aiface-log] employee mapping found id=%d employee_id=%d enrollment_no=%r",
                emp_mapping.id, emp_mapping.employee_id, emp_mapping.enrollment_no)

    employee = (Employee.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(id=emp_mapping.employee_id)
                .first())
    if not employee:
        log.warning("  [aiface] employee_id=%d not found for enrollid=%s",
                    emp_mapping.employee_id, enrollid)
        return 'unmatched'

    # Cross-school safety: the mapping, employee and device must all belong to the
    # same school. bypass_tenant_scope above disables automatic filtering, so this
    # is verified explicitly to prevent any cross-school attendance write.
    if employee.school_id != school.id:
        log.warning("  [aiface] SCHOOL MISMATCH — employee_id=%d school_id=%d != "
                    "device school_id=%d enrollid=%s sn=%s — rejecting punch",
                    employee.id, employee.school_id, school.id, enrollid, sn)
        return 'unmatched'

    year = get_active_year(school.id)
    if not year:
        log.warning("  [aiface] no active year for school_id=%d — skipping employee "
                    "punch enrollid=%s employee_id=%d. Set a current academic year "
                    "for this school.", school.id, enrollid, employee.id)
        return 'skipped'

    punch_date = punch_dt.date()
    punch_time = punch_dt.time().replace(microsecond=0)

    # Track what was written so we can notify after the commit and outside the
    # exception handler. Notification failure must never trigger a rollback of
    # attendance data that was already committed successfully.
    _notify_action   = None   # 'check_in' | 'check_out'
    _notify_att      = None   # EmployeeAttendance instance for the notification

    try:
        emp_att = (EmployeeAttendance.query
                   .execution_options(bypass_tenant_scope=True)
                   .filter_by(employee_id=employee.id, date=punch_date)
                   .first())

        if emp_att is None:
            emp_att = EmployeeAttendance(
                employee_id      = employee.id,
                school_id        = school.id,
                academic_year_id = year.id,
                date             = punch_date,
                status           = 'present',
                check_in         = punch_time,
                source           = 'aiface',
                device_id        = device.id,
                notes            = dedup_tag,
            )
            db.session.add(emp_att)
            db.session.commit()
            log.info("  [aiface] employee check_in: employee_id=%d (%s) at %s",
                     employee.id, employee.full_name, punch_time)
            _notify_action = 'check_in'
            _notify_att    = emp_att

        elif dedup_tag in (emp_att.notes or ''):
            log.debug("  [aiface] duplicate punch employee_id=%d tag=%s",
                      employee.id, dedup_tag)
            return 'skipped'

        else:
            emp_att.check_out = punch_time
            emp_att.notes     = (emp_att.notes or '') + '|' + dedup_tag
            db.session.commit()
            log.info("  [aiface] employee check_out: employee_id=%d (%s) at %s",
                     employee.id, employee.full_name, punch_time)
            _notify_action = 'check_out'
            _notify_att    = emp_att

    except IntegrityError:
        # Unique (employee_id, date) race: a concurrent punch (realtime sendlog +
        # periodic getnewlog) created the row between our SELECT and INSERT.
        # Roll back and treat as an already-recorded duplicate, not an error, so
        # the rest of the batch keeps processing.
        db.session.rollback()
        log.info("  [aiface] employee attendance duplicate/race (unique constraint) "
                 "employee_id=%d date=%s enrollid=%s — skipped",
                 employee.id, punch_date, enrollid)
        return 'skipped'
    except Exception:
        # Roll back the failed transaction; without this the session stays in a
        # failed state and every subsequent record in the same batch would also
        # fail with PendingRollbackError.
        db.session.rollback()
        log.exception("  [aiface] Employee attendance error employee_id=%d enrollid=%s "
                      "school_id=%d", employee.id, enrollid, school.id)
        return 'error'

    # Send notification after the confirmed commit, outside the exception handler.
    # A notification failure must not affect the attendance result.
    if _notify_action and _notify_att:
        try:
            from app.services.notifications import NotificationService
            NotificationService.send_employee_attendance_notification(
                employee, _notify_att, _notify_action, 'aiface')
        except Exception:
            log.exception("  [aiface] employee attendance notification error "
                          "employee_id=%d action=%s", employee.id, _notify_action)

    return 'processed'


def _handle_getnewlog_records(sn: str, records: list) -> tuple:
    """Process a batch of records received from a getnewlog response. Runs in executor."""
    try:
        with _flask_app.app_context():
            from app.models import AttendanceDevice, School, db

            device = (AttendanceDevice.query
                      .execution_options(bypass_tenant_scope=True)
                      .filter_by(device_sn=sn)
                      .first())
            if not device:
                log.warning("[getnewlog] Unknown device sn=%s — skipping records", sn)
                return 0, 0, 0, 0

            school = (School.query
                      .execution_options(bypass_tenant_scope=True)
                      .filter_by(id=device.school_id)
                      .first())
            if not school:
                log.warning("[getnewlog] No school for device sn=%s", sn)
                return 0, 0, 0, 0

            # Touch heartbeat (records is non-empty when this function is called).
            try:
                device.last_sync_at = datetime.utcnow()
                db.session.commit()
            except Exception:
                db.session.rollback()

            return _process_record_list(sn, device, school, records, source_cmd="getnewlog")

    except Exception:
        log.exception("[getnewlog] Unexpected error processing records for sn=%s", sn)
        return 0, 0, 0, 0


async def _periodic_getnewlog_task(sn: str, my_entry: dict, interval: int = 60) -> None:
    """
    Poll getnewlog every `interval` seconds while the device stays connected
    AND while `my_entry` is still the active entry for this SN.

    The `my_entry` binding prevents a leftover task from a previous connection
    from polling the new connection after a device reconnect.  When the device
    re-registers, _connections[sn] is replaced with a new entry object; this
    task's `_connections.get(sn) is my_entry` check then fails and it exits.

    Uses stn=false so each poll only retrieves logs since the previous getnewlog.
    Dedup protection (via notes) prevents double-processing.
    Timeout: 20 s per poll.

    HEARTBEAT DECOUPLING (critical for the cross-process "online" signal):
      The DB heartbeat (last_sync_at) is committed at the top of EVERY cycle,
      before — and independently of — the getnewlog attempt, for as long as the
      WebSocket connection is alive (entry unchanged).  last_sync_at therefore
      means "the bridge currently has this device connected", which is exactly
      what the cloud web UI (Render, a different process) needs.  Without this,
      a device that stays TCP-connected (cloud icon on) but only pushes realtime
      sendlog — or whose firmware never answers getnewlog — would stop updating
      last_sync_at and be shown offline/stale within the 90 s bridge threshold
      even though it is genuinely connected.

      When the device stops answering getnewlog it is marked stale; the heartbeat
      keeps being committed and getnewlog is retried only every ~5 minutes so the
      task can self-heal without spamming 20 s timeouts each cycle.
    """
    loop = asyncio.get_running_loop()
    _device_status.setdefault(sn, {})["poll_task_running"] = True
    log.info("[aiface] periodic getnewlog task started sn=%s interval=%ds", sn, interval)
    _stale_cycles = 0  # consecutive cycles skipped while stale (for retry backoff)
    try:
        while _connections.get(sn) is my_entry:
            # Fixed cadence — never backed off, so the heartbeat below stays well
            # inside the bridge online threshold even when getnewlog is stale.
            await asyncio.sleep(interval)

            # Exit if our connection was replaced (device reconnected)
            if _connections.get(sn) is not my_entry:
                log.info("[aiface] periodic poll: sn=%s entry replaced — stopping old task", sn)
                break

            # ── Heartbeat FIRST, unconditionally ───────────────────────────────
            # The connection entry is still ours, so the device IS connected to
            # this bridge right now.  Commit last_sync_at every cycle regardless
            # of getnewlog so the cloud UI reports the device online.
            await loop.run_in_executor(None, _db_touch_device, sn)

            _st = _device_status.get(sn, {})
            if _st.get("getnewlog_stale", False):
                # Keep heart-beating (done above); retry getnewlog only every ~5 min.
                _stale_cycles += 1
                _retry_every = max(1, 300 // max(interval, 1))
                if _stale_cycles < _retry_every:
                    log.warning(
                        "[aiface] periodic poll: sn=%s getnewlog stale (%d timeouts) — "
                        "heartbeat committed; getnewlog retry in %d cycle(s)",
                        sn, _st.get("getnewlog_timeout_count", 0),
                        _retry_every - _stale_cycles)
                    continue
                _stale_cycles = 0
                log.info("[aiface] periodic poll: sn=%s retrying getnewlog after stale backoff", sn)

            log.info("[aiface] periodic getnewlog poll sn=%s "
                     "last_success=%s timeout_count=%d",
                     sn,
                     _st.get("last_getnewlog_success_at", "never"),
                     _st.get("getnewlog_timeout_count", 0))
            try:
                result = await _send_and_wait(
                    sn, {"cmd": "getnewlog", "stn": False}, timeout=20
                )
            except DeviceOfflineError:
                log.info("[aiface] periodic poll: sn=%s went offline — stopping task", sn)
                break
            except TimeoutError:
                _st2 = _device_status.setdefault(sn, {})
                _tc = _st2.get("getnewlog_timeout_count", 0) + 1
                _st2["getnewlog_timeout_count"] = _tc
                _is_stale = _tc >= _GETNEWLOG_STALE_THRESHOLD
                _st2["getnewlog_stale"] = _is_stale
                log.warning(
                    "[aiface] periodic poll: getnewlog timeout sn=%s consecutive_timeouts=%d%s "
                    "(heartbeat unaffected — device still connected)",
                    sn, _tc,
                    " — MARKED STALE, getnewlog retried periodically" if _is_stale else " — will retry")
                continue
            except Exception:
                log.exception("[aiface] periodic poll: unexpected error sn=%s", sn)
                continue

            count   = result.get("count", 0)
            records = result.get("record") or []
            _device_status.setdefault(sn, {}).update({
                "last_getnewlog_success_at": datetime.now(),
                "getnewlog_timeout_count": 0,
                "getnewlog_stale": False,
            })
            _stale_cycles = 0
            log.info("[aiface] periodic poll response sn=%s count=%d records=%d",
                     sn, count, len(records))

            if records:
                p, sk, um, er = await loop.run_in_executor(
                    None, _handle_getnewlog_records, sn, records
                )
                log.info(
                    "[aiface] periodic poll processed sn=%s: "
                    "processed=%d skipped=%d unmatched=%d errors=%d",
                    sn, p, sk, um, er,
                )
    except asyncio.CancelledError:
        log.info("[aiface] periodic getnewlog task cancelled sn=%s", sn)
    except Exception:
        log.exception("[aiface] periodic getnewlog task crashed sn=%s", sn)
    finally:
        _device_status.setdefault(sn, {})["poll_task_running"] = False
        log.info("[aiface] periodic getnewlog task stopped sn=%s", sn)


# ── Command handlers (synchronous — run inside Flask app context) ──────────────

def _db_touch_device(sn: str) -> None:
    """
    Update last_sync_at for a device in the DB.
    Called from background threads as a keep-alive heartbeat so the web UI
    (which may run in a different process or on Render) can determine whether
    the local WS bridge is actively connected — without relying on the
    in-memory _connections dict that is invisible across process boundaries.
    """
    if not _flask_app:
        return
    try:
        with _flask_app.app_context():
            from app.models import db, AttendanceDevice
            device = (AttendanceDevice.query
                      .execution_options(bypass_tenant_scope=True)
                      .filter_by(device_sn=sn)
                      .first())
            if device:
                _old_sync = device.last_sync_at
                _new_sync = datetime.utcnow()
                device.last_sync_at = _new_sync
                db.session.commit()
                log.info(
                    '[aiface] heartbeat committed: sn=%s device_id=%d school_id=%d '
                    'last_sync_at: %s → %s (UTC) — commit OK',
                    sn, device.id, device.school_id, _old_sync, _new_sync,
                )
            else:
                log.warning(
                    '[aiface] heartbeat skipped — sn=%s not found in DB '
                    '(device not registered via Admin → Attendance Devices)', sn)
    except Exception:
        log.exception("[aiface] heartbeat DB update failed sn=%s", sn)


def _handle_reg(payload: dict, remote_ip: str = '') -> str:
    sn = payload.get("sn", "")
    log.info("[reg] sn=%s model=%s firmware=%s remote_ip=%s",
             sn, payload.get("modelname"), payload.get("firmware"), remote_ip or "(unknown)")

    try:
        with _flask_app.app_context():
            from app.models import db, AttendanceDevice
            device = (AttendanceDevice.query
                      .execution_options(bypass_tenant_scope=True)
                      .filter_by(device_sn=sn)
                      .first())
            if device:
                device.last_sync_at = datetime.utcnow()
                # Update the stored IP so the web UI shows the current address.
                # For AI Face the connection direction is device→server, so
                # the stored ip_address is display-only; updating it here keeps
                # the device list accurate without any manual editing.
                if remote_ip and device.ip_address != remote_ip:
                    log.info("[reg] device id=%d ip_address updated %s → %s",
                             device.id, device.ip_address, remote_ip)
                    device.ip_address = remote_ip
                db.session.commit()
                log.info(
                    "[reg] device id=%d school_id=%d scope=%s "
                    "last_sync_at set to %s (UTC) — commit OK",
                    device.id, device.school_id, device.device_scope,
                    device.last_sync_at,
                )
            else:
                log.warning(
                    "[reg] UNKNOWN device sn=%s remote_ip=%s — "
                    "no AttendanceDevice row with this serial number. "
                    "Create one via Admin → Attendance Devices before attendance can be saved.",
                    sn, remote_ip)
    except Exception:
        log.exception("[reg] DB error sn=%s", sn)

    return _json_reply(
        ret="reg",
        result=True,
        cloudtime=_cloudtime(),
        nosenduser=True,
    )


def _handle_sendlog(payload: dict) -> str:
    sn       = payload.get("sn", "")
    count    = payload.get("count", 0)
    logindex = payload.get("logindex", 0)
    records  = payload.get("record", [])

    _device_status.setdefault(sn, {})["last_sendlog_at"] = datetime.now()

    log.info("[aiface] raw sendlog sn=%s count=%d logindex=%s records_in_payload=%d",
             sn, count, logindex, len(records))
    for _i, _rec in enumerate(records):
        log.info(
            "  [aiface] sendlog record[%d] keys=%s enrollid=%s mode=%s inout=%s event=%s image_present=%s",
            _i, sorted(_rec.keys()), _rec.get('enrollid'), _rec.get('mode'),
            _rec.get('inout'), _rec.get('event'), bool(_rec.get('image')),
        )

    processed = skipped = unmatched = errors = 0

    try:
        with _flask_app.app_context():
            from app.models import AttendanceDevice, School, db

            device = (AttendanceDevice.query
                      .execution_options(bypass_tenant_scope=True)
                      .filter_by(device_sn=sn)
                      .first())
            if not device:
                log.warning(
                    "[sendlog] UNKNOWN device sn=%s — responding OK to clear "
                    "the device queue. Add device via Admin → Attendance Devices.", sn)
                return _json_reply(ret="sendlog", result=True, count=count,
                                   logindex=logindex, cloudtime=_cloudtime(), access=1)

            school = (School.query
                      .execution_options(bypass_tenant_scope=True)
                      .filter_by(id=device.school_id)
                      .first())
            if not school:
                log.warning("[sendlog] No school row for school_id=%d sn=%s",
                            device.school_id, sn)
                return _json_reply(ret="sendlog", result=True, count=count,
                                   logindex=logindex, cloudtime=_cloudtime(), access=1)

            # Touch last_sync_at now so the web UI can show "online" even when
            # running on Render (different process, cannot see in-memory _connections).
            # This commit is small and runs before attendance processing.
            try:
                device.last_sync_at = datetime.utcnow()
                db.session.commit()
            except Exception:
                db.session.rollback()
                log.warning("[sendlog] heartbeat update failed for device_id=%d", device.id)

            log.info("[sendlog] device id=%d school_id=%d scope=%s count=%d",
                     device.id, device.school_id, device.device_scope, count)

            processed, skipped, unmatched, errors = _process_record_list(
                sn, device, school, records, source_cmd="sendlog"
            )

    except Exception:
        log.exception("[sendlog] Unexpected error sn=%s", sn)

    log.info("[sendlog] result: processed=%d skipped=%d unmatched=%d errors=%d",
             processed, skipped, unmatched, errors)

    return _json_reply(
        ret="sendlog",
        result=True,
        count=count,
        logindex=logindex,
        cloudtime=_cloudtime(),
        access=1,
    )


# ── Post-registration async task ───────────────────────────────────────────────

async def _post_reg_task(sn: str, entry: dict) -> None:
    """
    Fires after device registration is acknowledged.
    1. Pull all missed offline logs via getnewlog (stn=true → stn=false loop).
    2. Execute any commands queued while the device was offline.
    3. Start the periodic getnewlog poll task (bound to this connection's entry).
    """
    loop = asyncio.get_running_loop()

    # ── Pull missed logs via getnewlog ─────────────────────────────────────────
    log.info("[aiface] device registered sn=%s — requesting missed logs getnewlog stn=true", sn)
    stn          = True
    total_pulled = 0
    try:
        while True:
            if _connections.get(sn) is not entry:
                log.warning("[getnewlog] sn=%s entry changed during log pull — stopping", sn)
                break
            try:
                result = await _send_and_wait(sn, {"cmd": "getnewlog", "stn": stn}, timeout=20)
            except DeviceOfflineError:
                log.warning("[getnewlog] sn=%s went offline during log pull", sn)
                break
            except TimeoutError:
                _st = _device_status.setdefault(sn, {})
                _tc = _st.get("getnewlog_timeout_count", 0) + 1
                _st["getnewlog_timeout_count"] = _tc
                log.warning(
                    "[getnewlog] timeout sn=%s stn=%s consecutive_timeouts=%d — "
                    "trying getalllog as firmware fallback",
                    sn, stn, _tc)
                # Some firmware uses getalllog instead of getnewlog
                try:
                    if _connections.get(sn) is entry:
                        _ga = await _send_and_wait(
                            sn, {"cmd": "getalllog", "stn": True}, timeout=20)
                        _ga_count   = _ga.get("count", 0)
                        _ga_records = _ga.get("record") or []
                        _st["last_getnewlog_success_at"] = datetime.now()
                        _st["getnewlog_timeout_count"]   = 0
                        log.info("[aiface] getalllog fallback sn=%s count=%d records=%d",
                                 sn, _ga_count, len(_ga_records))
                        if _ga_records:
                            p, sk, um, er = await loop.run_in_executor(
                                None, _handle_getnewlog_records, sn, _ga_records)
                            total_pulled += p
                            log.info("[aiface] getalllog processed: processed=%d skipped=%d "
                                     "unmatched=%d errors=%d", p, sk, um, er)
                except asyncio.CancelledError:
                    raise
                except Exception as _ga_exc:
                    log.warning("[aiface] getalllog fallback failed sn=%s: %s", sn, _ga_exc)
                break

            count   = result.get("count", 0)
            records = result.get("record") or []
            _device_status.setdefault(sn, {}).update({
                "last_getnewlog_success_at": datetime.now(),
                "getnewlog_timeout_count": 0,
            })
            log.info("[aiface] pulled getnewlog sn=%s stn=%s count=%d records=%d",
                     sn, stn, count, len(records))

            if records:
                p, sk, um, er = await loop.run_in_executor(
                    None, _handle_getnewlog_records, sn, records
                )
                total_pulled += p
                log.info("[aiface] processed pulled log: processed=%d skipped=%d "
                         "unmatched=%d errors=%d", p, sk, um, er)

            if count == 0 or not records:
                log.info("[aiface] getnewlog complete sn=%s total_pulled=%d", sn, total_pulled)
                break

            stn = False  # subsequent requests: stn=false

    except Exception:
        log.exception("[aiface] unexpected error during getnewlog for sn=%s", sn)

    # ── Execute pending commands queued while device was offline ───────────────
    pending = _pending_commands.pop(sn, [])
    if pending:
        log.info("[aiface] executing %d pending commands for sn=%s", len(pending), sn)
        requeue = []
        for item in pending:
            if _connections.get(sn) is not entry:
                log.warning("[aiface] sn=%s entry changed — stopping pending command execution", sn)
                requeue.extend(pending[pending.index(item):])
                break
            cmd_payload = item["payload"]
            note        = item.get("note", "")
            try:
                result = await _send_and_wait(sn, cmd_payload, timeout=15)
                log.info("[aiface] pending cmd=%s enrollid=%s result=%s  note=%s",
                         cmd_payload.get("cmd"), cmd_payload.get("enrollid", ""),
                         result.get("result"), note)
            except DeviceOfflineError:
                log.warning("[aiface] sn=%s offline on pending cmd=%s — re-queuing remainder",
                            sn, cmd_payload.get("cmd"))
                requeue.append(item)
                break
            except Exception as exc:
                log.warning("[aiface] pending cmd=%s failed: %s", cmd_payload.get("cmd"), exc)

        if requeue:
            existing = _pending_commands.get(sn, [])
            _pending_commands[sn] = requeue + existing

    # ── Start periodic log poll bound to THIS connection entry ─────────────────
    # Binding to `entry` ensures the task stops automatically if the device
    # reconnects and replaces _connections[sn] with a new entry object.
    poll_interval = int(os.environ.get("AIFACE_POLL_INTERVAL", "60"))
    task = asyncio.ensure_future(
        _periodic_getnewlog_task(sn, my_entry=entry, interval=poll_interval)
    )
    task.set_name(f"poll-{sn}")
    task.add_done_callback(_log_task_exception)
    entry["poll_task"] = task
    log.info("[aiface] periodic poll task started sn=%s interval=%ds", sn, poll_interval)


# ── WebSocket connection handler ───────────────────────────────────────────────

async def _ws_handler(websocket):
    remote = websocket.remote_address
    log.info("[aiface] WS connection accepted from %s:%s", *remote)

    # Log the HTTP upgrade path (websockets 14+: websocket.request is set after handshake)
    try:
        _req_path = getattr(getattr(websocket, 'request', None), 'path', '?') or '?'
    except Exception:
        _req_path = '?'
    log.info("[aiface] WS handshake complete from %s:%s path=%s", *remote, _req_path)

    sn: str | None     = None
    entry: dict | None = None

    try:
        async for raw_msg in websocket:
            # Diagnostic: always log raw message length and a safe preview
            _pending_keys = list(entry["pending"].keys()) if entry else []
            _msg_len = len(raw_msg) if isinstance(raw_msg, (bytes, str)) else -1
            _preview = (raw_msg if isinstance(raw_msg, str) else raw_msg.decode('utf-8', errors='replace'))[:300]
            log.info("[aiface] ← sn=%s len=%d pending=%s raw_preview=%s",
                     sn or "?", _msg_len, _pending_keys or "none", _preview)

            try:
                payload = json.loads(raw_msg)
            except json.JSONDecodeError:
                log.warning("Non-JSON from %s:%s: %r", *remote, str(raw_msg)[:100])
                continue

            # ── ret= response: device replied to a server command ──────────────
            if "ret" in payload and entry is not None:
                ret_key = payload["ret"]
                fut = entry["pending"].pop(ret_key, None)
                if fut and not fut.done():
                    fut.set_result(payload)
                    log.info("← [ret=%s] resolved pending future sn=%s", ret_key, sn)
                else:
                    log.debug("← [ret=%s] no pending future sn=%s", ret_key, sn)
                continue

            # ── Firmware compatibility: some devices echo cmd= not ret= ─────────
            # (e.g. {"cmd":"getnewlog","count":0,...} instead of {"ret":"getnewlog",...})
            if entry is not None and "ret" not in payload:
                _echo_key = payload.get("cmd", "")
                if _echo_key and _echo_key in entry.get("pending", {}):
                    log.info(
                        "← [cmd=%s as ret] device uses cmd= not ret= sn=%s — resolving future",
                        _echo_key, sn)
                    fut = entry["pending"].pop(_echo_key, None)
                    if fut and not fut.done():
                        fut.set_result(payload)
                    continue

            cmd  = payload.get("cmd", "")
            loop = asyncio.get_running_loop()

            if cmd == "reg":
                sn    = payload.get("sn", "")
                entry = {"ws": websocket, "pending": {}, "poll_task": None}
                _connections[sn] = entry

                # Clear stale status so periodic polling restarts fresh after reconnect
                _prev_stale = _device_status.get(sn, {}).get("getnewlog_stale", False)
                _device_status.setdefault(sn, {}).update({
                    "last_reg_at": datetime.now(),
                    "getnewlog_timeout_count": 0,
                    "getnewlog_stale": False,
                    "poll_task_running": False,
                })
                if _prev_stale:
                    log.info("[aiface] sn=%s reconnected — stale cleared, "
                             "periodic poll will resume after log pull", sn)

                log.info("[aiface] WS device registered sn=%s from %s:%s path=%s",
                         sn, *remote, _req_path)
                reply = await loop.run_in_executor(None, _handle_reg, payload, remote[0])
                log.debug("→ %s", reply)
                await websocket.send(reply)

                # Pull missed logs, execute pending commands, start periodic poll
                task = asyncio.ensure_future(_post_reg_task(sn, entry))
                task.set_name(f"post-reg-{sn}")
                task.add_done_callback(_log_task_exception)
                continue  # reply already sent above

            elif cmd == "sendlog":
                reply = await loop.run_in_executor(None, _handle_sendlog, payload)

            elif cmd == "senduser":
                # nosenduser=true should prevent this, but handle gracefully
                log.info("[senduser] received from sn=%s — acking and discarding",
                         payload.get("sn", "?"))
                reply = _json_reply(ret="senduser", result=True, cloudtime=_cloudtime())

            else:
                log.warning("Unknown cmd=%r from %s:%s", cmd, *remote)
                continue

            log.debug("→ %s", reply)
            await websocket.send(reply)

    except Exception as exc:
        import websockets.exceptions as _wse
        if isinstance(exc, _wse.ConnectionClosed):
            _code   = getattr(exc, 'code', None) or getattr(exc, 'rcvd', None) and getattr(exc.rcvd, 'code', None)
            _reason = getattr(exc, 'reason', '') or (getattr(exc, 'rcvd', None) and getattr(exc.rcvd, 'reason', ''))
            log.info("[aiface] WS disconnected (exception): sn=%s %s:%s close_code=%s reason=%r",
                     sn or "?", *remote, _code, _reason)
        else:
            log.exception("[aiface] WS unexpected error from %s:%s sn=%s", *remote, sn or "?")
    finally:
        # Log close code and reason regardless of how the connection ended
        _close_code   = getattr(websocket, 'close_code', None)
        _close_reason = getattr(websocket, 'close_reason', None)
        log.info("[aiface] WS session ended: sn=%s %s:%s close_code=%s close_reason=%r",
                 sn or "?", *remote, _close_code, _close_reason)

        if sn:
            _device_status.setdefault(sn, {})["last_disconnect_at"] = datetime.now()

        if sn and _connections.get(sn) is entry:
            del _connections[sn]
            log.info("[aiface] WS device unregistered sn=%s", sn)

        if entry:
            # Cancel the periodic poll task bound to THIS entry
            poll_task = entry.get("poll_task")
            if poll_task and not poll_task.done():
                poll_task.cancel()
                log.info("[aiface] cancelled poll task for sn=%s on disconnect", sn)

            # Fail all unresolved pending futures so _send_and_wait callers don't hang
            for fut in list(entry["pending"].values()):
                if not fut.done():
                    fut.set_exception(DeviceOfflineError(
                        f"Device {sn} disconnected while waiting for response"))
            entry["pending"].clear()


# ── Server entry point ─────────────────────────────────────────────────────────

async def _serve():
    global _ws_loop
    import websockets
    _ws_loop = asyncio.get_running_loop()
    # ping_interval / ping_timeout control how quickly stale TCP connections are
    # detected.  20 s interval + 10 s timeout = stale detected within 30 s.
    async with websockets.serve(
        _ws_handler, "0.0.0.0", _WS_PORT,
        ping_interval=20,
        ping_timeout=10,
    ):
        log.info("AI Face WS server listening on 0.0.0.0:%d "
                 "(ping_interval=20s ping_timeout=10s)", _WS_PORT)
        await asyncio.Future()   # run until cancelled


def _run_server_thread():
    try:
        asyncio.run(_serve())
    except OSError as e:
        if e.errno == _errno.EADDRINUSE:
            # In production (Gunicorn multi-worker): a second worker tries to bind
            # the same WS port that the first worker already holds — expected and safe.
            # In development: most likely Flask is running with --port=7788 which
            # conflicts with the AI Face WS server.  Fix: use python run.py (port 5000).
            log.error(
                "[aiface] AI Face WS port %d is already in use — WS server will NOT start.\n"
                "  PRODUCTION (Gunicorn multi-worker): another worker already owns this port — "
                "this worker will skip it (expected).\n"
                "  DEVELOPMENT: Flask web server is likely running on the same port.\n"
                "    WRONG command: flask run --port=%d  or  flask run --host=0.0.0.0 --port=%d\n"
                "    CORRECT command: python run.py        (Flask on port 5000, WS on port 7788)\n"
                "    The AI Face device CANNOT connect until Flask is started on a different port.",
                _WS_PORT, _WS_PORT, _WS_PORT,
            )
        else:
            log.exception("[aiface] AI Face WS server thread crashed (OSError)")
    except Exception:
        log.exception("[aiface] AI Face WS server thread crashed unexpectedly")


def start_ai_face_ws_server(app) -> None:
    """
    Start the AI Face 11 WebSocket receiver in a background daemon thread.
    Called from the application factory after all extensions are initialized.
    Controlled by env var AIFACE_WS_ENABLED (default: true).
    """
    global _flask_app
    import os as _os
    _pid             = _os.getpid()
    _reloader_child  = _os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    _web_port        = _os.environ.get("PORT", "(not set)")
    _ws_enabled      = _os.environ.get("AIFACE_WS_ENABLED", "true").lower() not in ("0", "false", "no")
    _ws_port_src     = _os.environ.get("AIFACE_WS_PORT")  # None means "using default 7788"

    # Always emit a startup diagnostic so Render logs show exactly what was in effect.
    # The pid and reloader_child fields make it easy to identify which process owns
    # the WS server when the Flask debug reloader spawns two processes.
    log.info(
        "AI Face WS startup — pid=%d  reloader_child=%s  "
        "PORT=%s  AIFACE_WS_PORT=%s (effective WS port: %d)  "
        "AIFACE_WS_ENABLED=%s",
        _pid, _reloader_child,
        _web_port,
        _ws_port_src if _ws_port_src is not None else "(not set, default=7788)",
        _WS_PORT,
        "true" if _ws_enabled else "false",
    )

    if not _ws_enabled:
        log.info("AI Face WS server disabled (AIFACE_WS_ENABLED=false)")
        return

    # Safety guard: refuse to bind on the same port Gunicorn uses for HTTP.
    # This catches the case where AIFACE_WS_PORT was accidentally set to $PORT
    # in the Render dashboard (or any other deployment where PORT is exported).
    if _web_port != "(not set)" and str(_WS_PORT) == str(_web_port):
        log.error(
            "AIFACE_WS_PORT (%d) matches the web server PORT (%s) — "
            "AI Face WS server will NOT start to avoid a port conflict with Gunicorn. "
            "Fix: remove AIFACE_WS_PORT from Render env vars to use the default (7788), "
            "or set it explicitly to a port that differs from PORT.",
            _WS_PORT, _web_port,
        )
        return

    # Pre-bind probe: detect port conflict immediately (e.g., Flask running on wrong port)
    # before starting the background thread, so the error is clear and synchronous.
    _probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        _probe.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        _probe.bind(("0.0.0.0", _WS_PORT))
    except OSError as _probe_err:
        _probe.close()
        if _probe_err.errno == _errno.EADDRINUSE:
            log.error(
                "[aiface] PORT CONFLICT: AI Face WS port %d is already occupied before "
                "the WS thread starts. The Flask web server is almost certainly running "
                "on this port (e.g., 'flask run --port=%d').\n"
                "  Fix: stop the current server and restart with:\n"
                "    python run.py        →  Flask on port 5000, WS server on port 7788\n"
                "  Do NOT use: flask run --port=%d\n"
                "  The AI Face device at %s will keep reconnecting until this is fixed.",
                _WS_PORT, _WS_PORT, _WS_PORT,
                os.environ.get("AIFACE_DEVICE_IP", "<device-ip>"),
            )
            return
        log.warning("[aiface] WS port probe failed unexpectedly (%s) — starting WS thread anyway",
                    _probe_err)
    else:
        _probe.close()  # port is free; the WS server thread will bind it properly

    _flask_app = app
    t = threading.Thread(target=_run_server_thread, name="aiface-ws", daemon=True)
    t.start()
    log.info("[aiface] AI Face WS server thread started on port %d", _WS_PORT)

"""
Hikvision attendance device service.

Handles:
  - Connection testing via GET /ISAPI/System/deviceInfo
  - Event fetching via POST /ISAPI/AccessControl/AcsEvent
  - Full attendance sync: raw-log storage + process_student_scan
  - Optional background auto-sync (HIKVISION_AUTO_SYNC=true)
"""
import json
import logging
import os
import threading
import time
from datetime import date, datetime

import pytz
import requests
from requests.auth import HTTPDigestAuth
from sqlalchemy.exc import IntegrityError

from app.models import (db, AttendanceDevice, DeviceEventLog,
                        DeviceStudentMapping, Student)
from app.services.attendance_service import process_student_scan

logger = logging.getLogger(__name__)

# Hikvision access-granted event codes
# minor 75 = card/fingerprint access-granted
# minor 38 = face-recognition access-granted (observed after device re-enrollment)
_VALID_MAJOR  = 5
_VALID_MINORS = {75, 38}


def _is_valid_event(ev: dict):
    """Return (is_valid, reason_if_invalid) for a raw Hikvision event dict."""
    emp_no = str(ev.get('employeeNoString') or '').strip()
    verify = str(ev.get('currentVerifyMode') or '').strip()
    major  = ev.get('major')
    minor  = ev.get('minor')

    if not emp_no:
        return False, 'لا يوجد رقم موظف (حدث باب فقط)'
    if major != _VALID_MAJOR:
        return False, f'major غير مدعوم: {major}'
    if minor not in _VALID_MINORS:
        return False, f'minor غير مدعوم: {minor}'
    if not verify:
        return False, 'لا توجد طريقة تحقق'
    return True, None


# ─────────────────────────────────────────────────────────────────────────────
#  Connection helpers
# ─────────────────────────────────────────────────────────────────────────────

def _auth(device: AttendanceDevice) -> HTTPDigestAuth:
    return HTTPDigestAuth(device.username, device.password)


def _base_url(device: AttendanceDevice) -> str:
    port = device.port or 80
    return f"http://{device.ip_address}:{port}"


def test_connection(device: AttendanceDevice) -> dict:
    """
    Ping the device with GET /ISAPI/System/deviceInfo.
    Returns {"ok": True, "model": ..., "serial_no": ...} or {"ok": False, "error": ...}.
    """
    url = f"{_base_url(device)}/ISAPI/System/deviceInfo"
    try:
        resp = requests.get(url, auth=_auth(device), timeout=8)
        if resp.status_code == 200:
            try:
                data   = resp.json()
                info   = data.get('DeviceInfo', {})
                model  = info.get('model') or info.get('deviceType', 'N/A')
                serial = info.get('serialNumber', 'N/A')
            except ValueError:
                model, serial = 'N/A', 'N/A'
            return {"ok": True, "model": model, "serial_no": serial}
        return {"ok": False,
                "error": f"الجهاز أجاب بكود HTTP {resp.status_code}",
                "body": resp.text[:300]}
    except requests.exceptions.ConnectionError:
        return {"ok": False,
                "error": f"تعذر الاتصال بالجهاز على {device.ip_address}:{device.port}"}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "انتهت مهلة الاتصال بالجهاز"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ─────────────────────────────────────────────────────────────────────────────
#  Event fetching
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_events(device: AttendanceDevice, target_date: date) -> list:
    """Fetch ALL access events for target_date by paginating through Hikvision pages.

    Hikvision returns responseStatusStrg='MORE' when additional pages exist.
    We keep fetching with increasing searchResultPosition until we see 'OK' or
    'NO MATCH', or until a safety cap (2000 events) is reached.
    Page size is kept at 50 — some devices silently cap lower than 200.
    """
    start_str = f"{target_date.isoformat()}T00:00:00+03:00"
    end_str   = f"{target_date.isoformat()}T23:59:59+03:00"
    url       = f"{_base_url(device)}/ISAPI/AccessControl/AcsEvent?format=json"
    search_id = f"mecha-{device.id}-{target_date}"

    all_events: list = []
    position  = 0
    page_size = 50

    while len(all_events) < 2000:
        body = {
            "AcsEventCond": {
                "searchID":             search_id,
                "searchResultPosition": position,
                "maxResults":           page_size,
                "major":                0,
                "minor":                0,
                "startTime":            start_str,
                "endTime":              end_str,
            }
        }
        resp = requests.post(url, json=body, auth=_auth(device), timeout=12)
        resp.raise_for_status()
        acs  = resp.json().get('AcsEvent') or {}
        page = acs.get('InfoList') or []
        all_events.extend(page)

        if acs.get('responseStatusStrg') != 'MORE' or not page:
            break
        position += len(page)

    return all_events


def _parse_event_time(ts_str: str):
    """Parse Hikvision timestamp to naive UTC datetime for DB storage."""
    if not ts_str:
        return None
    try:
        clean = ts_str.replace('Z', '+00:00')
        dt    = datetime.fromisoformat(clean)
        if dt.tzinfo:
            dt = dt.astimezone(pytz.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _to_school_local(ts_str: str, school):
    """Convert device timestamp to naive school-local datetime."""
    from app.utils.attendance_helpers import utc_to_local
    if not ts_str:
        return None
    try:
        clean    = ts_str.replace('Z', '+00:00')
        aware_dt = datetime.fromisoformat(clean)
        if aware_dt.tzinfo is None:
            aware_dt = pytz.utc.localize(aware_dt)
        return utc_to_local(aware_dt, school)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Core sync
# ─────────────────────────────────────────────────────────────────────────────

def _enrich_detail_from_mapping(detail: dict, device: AttendanceDevice,
                                emp_no: str) -> None:
    """Populate detail['student'] and detail['student_code'] from the mapping table."""
    mapping = DeviceStudentMapping.query.filter_by(
        device_id=device.id, employee_no_string=emp_no, is_active=True
    ).first()
    if mapping:
        stu = db.session.get(Student, mapping.student_id)
        if stu:
            detail['student']      = stu.full_name
            detail['student_code'] = stu.student_id


def sync_device(device: AttendanceDevice, target_date: date = None) -> dict:
    """
    Full sync for one device.

    Phase 1 — fetch & classify:
      • Paginate all events for target_date.
      • Mark invalid events (wrong major/minor, no employeeNoString) as 'ignored'.
      • Keep only the highest serialNo per employeeNoString (mark older as 'skipped_older').

    Phase 2 — process latest valid event per employee:
      • Pre-query DeviceEventLog by (device_id, serial_no).
        – status='processed' → count as 'duplicate' (already done), skip.
        – status='raw'/'error' → reuse the existing row and retry.
        – not found → insert new row.
      • Look up DeviceStudentMapping.
      • Call process_student_scan with the device event time converted to school-local.
      • Classify result: 'processed' (new check_in/out), 'already_attended'
        (attendance exists, no change needed), 'duplicate', 'unmatched', 'error'.

    Summary keys returned:
      events_fetched, valid_events, skipped_invalid, skipped_older,
      processed, already_attended, duplicate, unmatched, error,
      newest_serial, newest_valid_serial, events_detail.
    """
    if target_date is None:
        target_date = date.today()

    summary: dict = {
        "ok":                  True,
        "device":              device.name,
        "events_fetched":      0,
        "valid_events":        0,   # passed filter; will enter processing
        "skipped_invalid":     0,   # door / system / alarm events
        "skipped_older":       0,   # not the latest scan for that employee
        "processed":           0,   # new check_in or check_out created
        "already_attended":    0,   # attendance existed; no new record created
        "duplicate":           0,   # serialNo already fully processed before
        "unmatched":           0,   # no mapping for employeeNoString
        "error":               0,
        "newest_serial":       None,
        "newest_valid_serial": None,
        "events_detail":       [],
    }

    try:
        raw_events = _fetch_events(device, target_date)
    except requests.exceptions.ConnectionError:
        return {"ok": False, "error": f"تعذر الاتصال بالجهاز {device.name}"}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": f"انتهت مهلة الاتصال بالجهاز {device.name}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    summary["events_fetched"] = len(raw_events)
    if raw_events:
        summary["newest_serial"] = max(
            (ev.get('serialNo') or 0) for ev in raw_events
        )

    # ── Phase 1: classify; keep latest valid per employee ─────────────────────
    event_details: list  = []
    latest_per_emp: dict = {}  # emp_no -> {'ev': ..., 'detail': ...}

    for ev in raw_events:
        emp_no = str(ev.get('employeeNoString') or '').strip()
        verify = str(ev.get('currentVerifyMode') or '').strip()
        sn     = ev.get('serialNo') or 0

        is_valid, skip_reason = _is_valid_event(ev)

        detail = {
            'serial_no':    sn,
            'employee_no':  emp_no or None,
            'person_name':  (ev.get('name') or '').strip() or None,
            'major':        ev.get('major'),
            'minor':        ev.get('minor'),
            'event_time':   ev.get('time'),
            'verify_mode':  verify or None,
            'status':       'ignored' if not is_valid else 'pending',
            'reason':       skip_reason,
            'action':       None,
            'student':      None,
            'student_code': None,
        }
        event_details.append(detail)

        if not is_valid:
            summary["skipped_invalid"] += 1
            continue

        if (summary["newest_valid_serial"] is None
                or sn > summary["newest_valid_serial"]):
            summary["newest_valid_serial"] = sn

        existing = latest_per_emp.get(emp_no)
        if existing is None or sn > (existing['ev'].get('serialNo') or 0):
            if existing is not None:
                existing['detail']['status'] = 'skipped_older'
                existing['detail']['reason'] = 'حدث أحدث موجود لنفس الموظف'
                summary["skipped_older"] += 1
            latest_per_emp[emp_no] = {'ev': ev, 'detail': detail}
        else:
            detail['status'] = 'skipped_older'
            detail['reason'] = 'حدث أحدث موجود لنفس الموظف'
            summary["skipped_older"] += 1

    summary["valid_events"] = len(latest_per_emp)
    school = device.school

    # ── Phase 2: process latest valid event per employee ─────────────────────
    for emp_no, item in latest_per_emp.items():
        ev     = item['ev']
        detail = item['detail']

        serial_no   = ev.get('serialNo')
        event_ts    = ev.get('time')
        person_name = (ev.get('name') or '').strip()
        verify_mode = ev.get('currentVerifyMode', '')

        # ── Check if this serialNo is already in the DB ────────────────────────
        existing_log = DeviceEventLog.query.filter_by(
            device_id=device.id, serial_no=serial_no
        ).first()

        if existing_log is not None and existing_log.status == 'processed':
            # Fully processed in a prior sync — attendance already created.
            summary["duplicate"] += 1
            detail['status'] = 'duplicate'
            detail['reason'] = 'سبق معالجة هذا الحدث — الحضور مسجل مسبقاً'
            _enrich_detail_from_mapping(detail, device, emp_no)
            continue

        # ── Create new log or reuse existing raw/error row ────────────────────
        if existing_log is not None:
            # Previous attempt failed (status='raw'/'error') — retry.
            log               = existing_log
            log.person_name   = person_name or log.person_name
            log.verify_mode   = verify_mode or log.verify_mode
            log.status        = 'raw'
            log.error_message = None
        else:
            log = DeviceEventLog(
                school_id          = device.school_id,
                device_id          = device.id,
                serial_no          = serial_no,
                employee_no_string = emp_no,
                person_name        = person_name,
                event_time         = _parse_event_time(event_ts),
                major              = ev.get('major'),
                minor              = ev.get('minor'),
                verify_mode        = verify_mode,
                raw_json           = json.dumps(ev, ensure_ascii=False),
                status             = 'raw',
            )
            db.session.add(log)

        try:
            db.session.flush()
        except IntegrityError:
            # Race condition with auto-sync thread inserting concurrently.
            db.session.rollback()
            summary["duplicate"] += 1
            detail['status'] = 'duplicate'
            detail['reason'] = 'حدث تزامن متزامن — أُضيف بواسطة عملية أخرى'
            continue

        # ── Find student mapping ───────────────────────────────────────────────
        mapping = DeviceStudentMapping.query.filter_by(
            device_id          = device.id,
            employee_no_string = emp_no,
            is_active          = True,
        ).first()

        if not mapping:
            log.status        = 'unmatched'
            log.error_message = f'لا يوجد ربط للرقم {emp_no}'
            db.session.commit()
            summary["unmatched"] += 1
            detail['status'] = 'unmatched'
            detail['reason'] = f'لا يوجد ربط لرقم الجهاز {emp_no}'
            continue

        # ── Resolve student ────────────────────────────────────────────────────
        student = db.session.get(Student, mapping.student_id)
        if not student:
            log.status        = 'error'
            log.error_message = 'سجل الطالب غير موجود'
            db.session.commit()
            summary["error"] += 1
            detail['status'] = 'error'
            detail['reason'] = 'سجل الطالب غير موجود'
            continue

        detail['student']      = student.full_name
        detail['student_code'] = student.student_id

        local_dt = _to_school_local(event_ts, school)

        result, _ = process_student_scan(
            student.student_id,
            device.device_sn,
            local_dt      = local_dt,
            hik_serial_no = serial_no,
            school        = school,
        )

        action = result.get('action', '')
        detail['action'] = action

        if action in ('check_in', 'check_out'):
            log.status       = 'processed'
            summary["processed"] += 1
            detail['status'] = 'processed'
            detail['reason'] = ('تم تسجيل الحضور'
                                if action == 'check_in' else 'تم تسجيل الانصراف')
        elif action == 'already_checked_in':
            log.status       = 'processed'
            summary["already_attended"] += 1
            detail['status'] = 'already_attended'
            detail['reason'] = 'الطالب مسجل حضور مسبقاً اليوم'
        elif action == 'skipped':
            log.status       = 'processed'
            summary["duplicate"] += 1
            detail['status'] = 'duplicate'
            detail['reason'] = 'سجل مكرر سبق معالجته'
        elif not result.get('ok'):
            log.status        = 'error'
            log.error_message = result.get('error', '')
            summary["error"] += 1
            detail['status'] = 'error'
            detail['reason'] = result.get('error', '')
        else:
            log.status       = 'processed'
            summary["processed"] += 1
            detail['status'] = 'processed'
            detail['reason'] = 'تمت المعالجة'

        db.session.commit()

    device.last_sync_at = datetime.utcnow()
    db.session.commit()

    # Sort: non-ignored first (serialNo desc), then ignored (serialNo desc).
    valid_first  = [d for d in event_details if d['status'] != 'ignored']
    ignored_last = [d for d in event_details if d['status'] == 'ignored']
    valid_first.sort(key=lambda d: d.get('serial_no') or 0,  reverse=True)
    ignored_last.sort(key=lambda d: d.get('serial_no') or 0, reverse=True)
    summary['events_detail'] = valid_first + ignored_last

    return summary


# ─────────────────────────────────────────────────────────────────────────────
#  Background auto-sync
# ─────────────────────────────────────────────────────────────────────────────

_auto_sync_thread: threading.Thread | None = None


def start_auto_sync(app) -> None:
    """
    Start a daemon thread that polls all active Hikvision devices every
    HIKVISION_SYNC_INTERVAL seconds (default 10).
    Activated only when HIKVISION_AUTO_SYNC=true is set in the environment.
    Safe to call multiple times — only starts one thread.
    """
    if os.environ.get('HIKVISION_AUTO_SYNC', '').lower() != 'true':
        return
    global _auto_sync_thread
    if _auto_sync_thread and _auto_sync_thread.is_alive():
        return
    interval = max(5, int(os.environ.get('HIKVISION_SYNC_INTERVAL', '10')))
    _auto_sync_thread = threading.Thread(
        target=_sync_loop,
        args=(app, interval),
        daemon=True,
        name='hikvision-auto-sync',
    )
    _auto_sync_thread.start()
    app.logger.info(f'Hikvision auto-sync started (interval={interval}s)')


def _sync_loop(app, interval: int) -> None:
    """Runs in a background daemon thread; uses bypass_tenant_scope so it
    can access all schools without a request context."""
    with app.app_context():
        while True:
            try:
                devices = (
                    AttendanceDevice.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter_by(is_active=True)
                    .all()
                )
                for dev in devices:
                    try:
                        sync_device(dev)
                    except Exception as exc:
                        logger.error(f'Auto-sync failed for device "{dev.name}": {exc}')
            except Exception as exc:
                logger.error(f'Auto-sync loop error: {exc}')
                try:
                    db.session.rollback()
                except Exception:
                    pass
            finally:
                # Return connection to pool between ticks — long-lived app_context()
                # doesn't fire teardown handlers, so we remove the session manually.
                try:
                    db.session.remove()
                except Exception:
                    pass
            time.sleep(interval)

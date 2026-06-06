"""
Mecha-School — Hardware (ESP32 / RFID) Blueprint
================================================
Exposes a narrow set of JSON endpoints that ESP32 / Arduino readers hit
to record gate scans.

Authentication:
    Each device is pre-registered in the `devices` table with a unique
    random `api_key`. Clients must send that key via the `X-Device-Key`
    HTTP header. No session cookies, no CSRF.

Endpoints:
    POST  /api/v1/hardware/attendance   — record one RFID scan
    GET   /api/v1/hardware/ping         — health-check for the device
"""
import pytz
from datetime import datetime

from flask import Blueprint, jsonify, request

from app.models import (db, Device, Student, StudentAttendance,
                         Notification, User, SchoolSettings)
from app.services.notifications import NotificationService
from app.utils.audit import log_action
from app.utils.attendance_helpers import (determine_check_in_status,
                                           get_local_now, utc_to_local,
                                           get_student_shift)
from app.utils.scoping import set_hardware_scope

hardware_bp = Blueprint('hardware', __name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Auth helper
# ─────────────────────────────────────────────────────────────────────────────

def _authenticate_device():
    """
    Look up the device by its X-Device-Key header.
    Returns (device, None) on success, or (None, (json, status)) on failure.
    """
    api_key = request.headers.get('X-Device-Key', '').strip()
    if not api_key:
        return None, (jsonify({'ok': False,
                               'error': 'missing X-Device-Key header'}), 401)

    device = Device.query.filter_by(api_key=api_key, is_active=True).first()
    if not device:
        return None, (jsonify({'ok': False,
                               'error': 'unknown or inactive device'}), 403)

    device.last_seen = datetime.utcnow()
    set_hardware_scope(device)
    return device, None


def _parse_device_timestamp(ts_str, settings):
    """
    Parse an ISO-8601 UTC timestamp from the device and convert it to the
    school's local timezone (naive datetime).  Falls back to get_local_now()
    if the timestamp is missing or malformed.
    """
    if not ts_str:
        return get_local_now(settings)
    try:
        ts_clean = ts_str.replace('Z', '+00:00')
        utc_dt   = datetime.fromisoformat(ts_clean)
        if utc_dt.tzinfo is None:
            utc_dt = pytz.utc.localize(utc_dt)
        return utc_to_local(utc_dt, settings)
    except Exception:
        return get_local_now(settings)


# ─────────────────────────────────────────────────────────────────────────────
#  Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@hardware_bp.route('/ping', methods=['GET'])
def ping():
    device, err = _authenticate_device()
    if err:
        return err
    db.session.commit()
    settings = device.school or SchoolSettings.get()
    return jsonify({
        'ok': True,
        'device_id':   device.device_id,
        'location':    device.location,
        'server_time': get_local_now(settings).isoformat(),
    })


@hardware_bp.route('/attendance', methods=['POST'])
def record_attendance():
    """
    ESP32 posts a JSON body:
        {
          "rfid_tag":  "04A1B2C3D4",
          "timestamp": "2026-04-29T07:32:10Z",   # UTC; converted to school timezone
          "scan_type": "check_in" | "check_out"  # optional; inferred if omitted
        }

    Logic:
      * Find student by rfid_tag_id.
      * If no attendance row for today → create with status from time thresholds.
      * If row exists and check_in already set → 409 (duplicate check_in).
      * If row exists without check_in → fill check_in.
      * Otherwise → set check_out.
      * Fire a status-specific push notification to every linked parent.
    """
    device, err = _authenticate_device()
    if err:
        return err

    payload = request.get_json(silent=True) or {}
    tag = (payload.get('rfid_tag') or '').strip()
    if not tag:
        return jsonify({'ok': False, 'error': 'rfid_tag required'}), 400

    student = Student.query.filter_by(rfid_tag_id=tag).first()
    if not student:
        log_action('rfid_unknown', 'device', device.id,
                   details=f'unknown tag={tag}')
        db.session.commit()
        return jsonify({'ok': False, 'error': 'tag not registered'}), 404

    settings  = device.school or SchoolSettings.get()
    local_now = _parse_device_timestamp(payload.get('timestamp'), settings)
    today     = local_now.date()
    scan_time = local_now.time().replace(microsecond=0)
    scan_type = payload.get('scan_type')

    row = StudentAttendance.query.filter_by(
        student_id=student.id, date=today
    ).first()

    action = None

    if row is None:
        # First scan → determine status from time thresholds
        _shift = get_student_shift(student, settings)
        status = determine_check_in_status(scan_time, settings, shift=_shift)
        row = StudentAttendance(
            student_id = student.id,
            school_id  = student.school_id,
            academic_year_id = student.academic_year_id,
            date       = today,
            status     = status,
            check_in   = scan_time,
            source     = 'rfid',
            device_id  = device.id,
            notes      = f'RFID {tag} @ {device.device_id}',
            shift_id   = _shift.id if _shift else None,
        )
        db.session.add(row)
        action = 'check_in'
    else:
        is_check_in_intent = (scan_type == 'check_in') or (
            scan_type is None and row.check_out is None
        )
        if is_check_in_intent:
            if row.check_in is not None:
                # Duplicate check_in — block
                db.session.commit()   # persist last_seen
                return jsonify({
                    'ok':            False,
                    'error':         'already_checked_in',
                    'student':       {'id': student.id, 'name': student.full_name},
                    'checked_in_at': row.check_in.isoformat(),
                    'status':        row.status,
                }), 409
            row.check_in = scan_time
            action = 'check_in'
        else:
            row.check_out = scan_time
            action = 'check_out'
        row.source    = 'rfid'
        row.device_id = device.id

    db.session.commit()
    log_action('rfid_scan', 'student', student.id,
               details=f'{action} via {device.device_id} tag={tag}')

    # Status-specific push notification
    if action == 'check_in':
        if row.status == 'present':
            title = 'حضور الطالب في الوقت المحدد'
            body  = f'طالبك {student.full_name} وصل في الوقت المحدد الساعة {scan_time.strftime("%H:%M")}.'
        else:
            title = 'تأخر الطالب عن موعد الحضور'
            body  = f'طالبك {student.full_name} وصل متأخراً الساعة {scan_time.strftime("%H:%M")}.'
    else:
        title = 'خروج الطالب من المدرسة'
        body  = f'طالبك {student.full_name} غادر المدرسة الساعة {scan_time.strftime("%H:%M")}.'

    NotificationService.send_to_parents_of_student(
        student.id, title, body, ntype='rfid',
        data={'action': action, 'status': row.status,
              'at': scan_time.isoformat(), 'device': device.device_id}
    )

    return jsonify({
        'ok':      True,
        'student': {'id': student.id, 'name': student.full_name},
        'action':  action,
        'status':  row.status,
        'at':      scan_time.isoformat(),
    })


@hardware_bp.route('/hardware-sync', methods=['POST'])
def hardware_sync():
    """
    Unified check-in endpoint for RFID readers AND facial-recognition units.

    Body (JSON):
        {
          "card_id":    "04A1B2C3D4",             # RFID UID
          "face_token": "usr_abc123",             # Face-ID token
          "timestamp":  "2026-04-29T07:30:00Z"   # UTC; converted to school timezone
        }

    First scan  → check_in with status from time thresholds.
    Second scan → check_out (not blocked).
    """
    device, err = _authenticate_device()
    if err:
        return err

    payload    = request.get_json(silent=True) or {}
    card_id    = (payload.get('card_id')    or '').strip()
    face_token = (payload.get('face_token') or '').strip()
    tag        = card_id or face_token
    scan_src   = 'rfid' if card_id else 'face_id'

    if not tag:
        return jsonify({'ok': False,
                        'error': 'card_id or face_token required'}), 400

    student = Student.query.filter_by(rfid_tag_id=tag).first()
    if not student:
        log_action('hardware_sync_unknown', 'device', device.id,
                   details=f'unknown {scan_src} tag={tag}')
        db.session.commit()
        return jsonify({'ok': False, 'error': 'identifier not registered'}), 404

    settings  = device.school or SchoolSettings.get()
    local_now = _parse_device_timestamp(payload.get('timestamp'), settings)
    today     = local_now.date()
    scan_time = local_now.time().replace(microsecond=0)

    row = StudentAttendance.query.filter_by(
        student_id=student.id, date=today
    ).first()

    if row is None:
        # First scan → check_in with smart status
        _shift = get_student_shift(student, settings)
        status = determine_check_in_status(scan_time, settings, shift=_shift)
        row = StudentAttendance(
            student_id = student.id,
            school_id  = student.school_id,
            academic_year_id = student.academic_year_id,
            date       = today,
            status     = status,
            check_in   = scan_time,
            source     = 'rfid',
            device_id  = device.id,
            notes      = f'{scan_src.upper()} {tag} @ {device.device_id}',
            shift_id   = _shift.id if _shift else None,
        )
        db.session.add(row)
        action = 'check_in'
    else:
        # Second scan → check_out
        row.check_out = scan_time
        row.source    = 'rfid'
        row.device_id = device.id
        action = 'check_out'

    db.session.commit()
    log_action('hardware_sync', 'student', student.id,
               details=f'{action} via {scan_src} device={device.device_id}')

    # Status-specific push notification
    if action == 'check_in':
        if row.status == 'present':
            title = 'حضور الطالب في الوقت المحدد'
            body  = f'طالبك {student.full_name} وصل في الوقت المحدد الساعة {scan_time.strftime("%H:%M")}.'
        else:
            title = 'تأخر الطالب عن موعد الحضور'
            body  = f'طالبك {student.full_name} وصل متأخراً الساعة {scan_time.strftime("%H:%M")}.'
    else:
        title = 'خروج الطالب من المدرسة'
        body  = f'طالبك {student.full_name} غادر المدرسة الساعة {scan_time.strftime("%H:%M")}.'

    NotificationService.send_to_parents_of_student(
        student.id, title, body, ntype='rfid',
        data={'action': action, 'status': row.status,
              'at': scan_time.isoformat(), 'device': device.device_id, 'source': scan_src}
    )

    return jsonify({
        'ok':      True,
        'student': {'id': student.id, 'name': student.full_name},
        'action':  action,
        'status':  row.status,
        'at':      scan_time.isoformat(),
        'source':  scan_src,
    })

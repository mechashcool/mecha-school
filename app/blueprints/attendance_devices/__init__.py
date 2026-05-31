"""
Mecha-School — Attendance Devices (أجهزة الحضور)
================================================
School-manager module for managing AI Face readers,
student/employee–device mappings, event sync, and raw log review.

device_scope controls which entity type is managed on each device:
  'students'  — only student mappings; sendlog resolves DeviceStudentMapping
  'employees' — only employee mappings; sendlog resolves DeviceEmployeeMapping
  'mixed'     — both: sendlog tries student first, then employee fallback

Routes
------
GET   /attendance-devices/                              — list devices
GET   /attendance-devices/new                           — add device form
POST  /attendance-devices/new                           — create device
GET   /attendance-devices/<id>/edit                     — edit form
POST  /attendance-devices/<id>/edit                     — update device
POST  /attendance-devices/<id>/delete                   — delete device
POST  /attendance-devices/<id>/toggle                   — enable / disable
POST  /attendance-devices/<id>/test-connection          — AJAX: connection test
POST  /attendance-devices/<id>/sync                     — AJAX: Hikvision attendance sync
GET   /attendance-devices/<id>/logs                     — raw event log viewer
GET   /attendance-devices/<id>/mappings                 — mapping list (scope-aware)
POST  /attendance-devices/<id>/mappings/add             — create mapping (scope-aware)
POST  /attendance-devices/mappings/<mid>/delete         — delete student mapping (DB only)
POST  /attendance-devices/emp-mappings/<mid>/delete     — delete employee mapping (DB only)
POST  /attendance-devices/<id>/mappings/copy-from       — copy student mappings from another device
POST  /attendance-devices/<id>/mappings/copy-to         — copy student mappings to another device
POST  /attendance-devices/<id>/aiface-sync-student      — AJAX: push one student to AI Face
POST  /attendance-devices/<id>/aiface-sync-employee     — AJAX: push one employee to AI Face
POST  /attendance-devices/<id>/aiface-sync-all          — AJAX: push all (scope-aware)
POST  /attendance-devices/<id>/aiface-delete-from-device — AJAX: deleteuser + remove mapping
"""
import os
from datetime import date, datetime

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from app.models import (db, AttendanceDevice, DeviceEventLog,
                        DeviceStudentMapping, DeviceEmployeeMapping,
                        Student, Employee)
from app.services.hikvision import sync_device, test_connection
from app.utils.decorators import (admin_required, get_active_year, get_current_school,
                                   action_required, section_required)

attendance_devices_bp = Blueprint('attendance_devices', __name__)

_VALID_SCOPES = ('students', 'employees', 'mixed')


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _school_or_abort():
    if not current_user.is_authenticated or not current_user.is_admin_user:
        abort(403)
    school = get_current_school()
    if not school or not hasattr(school, 'id'):
        flash('يرجى اختيار مدرسة نشطة أولاً.', 'danger')
        abort(400)
    return school


def _get_device_or_404(device_id: int, school) -> AttendanceDevice:
    return AttendanceDevice.query.filter_by(
        id=device_id, school_id=school.id
    ).first_or_404()


def _form_to_device(dev: AttendanceDevice):
    """Read POST form into device fields; returns error string or None."""
    name         = (request.form.get('name')         or '').strip()
    ip_address   = (request.form.get('ip_address')   or '').strip()
    port_str     = (request.form.get('port')          or '80').strip()
    username     = (request.form.get('username')      or 'admin').strip()
    password     = (request.form.get('password')      or '').strip()
    device_sn    = (request.form.get('device_sn')     or '').strip()
    notes        = (request.form.get('notes')         or '').strip()
    device_scope = (request.form.get('device_scope')  or 'students').strip()

    if not name:
        return 'اسم الجهاز مطلوب'
    if not ip_address:
        return 'عنوان IP مطلوب'
    if not device_sn:
        return 'الرقم التسلسلي للجهاز مطلوب'
    if device_scope not in _VALID_SCOPES:
        return 'نوع الجهاز غير صحيح'

    try:
        port = int(port_str)
        if port < 1 or port > 65535:
            raise ValueError
    except ValueError:
        return 'رقم المنفذ يجب أن يكون بين 1 و 65535'

    # Prevent duplicate device_sn within the same school
    conflict = (AttendanceDevice.query
                .filter(AttendanceDevice.school_id == dev.school_id,
                        AttendanceDevice.device_sn == device_sn)
                .first())
    if conflict and (dev.id is None or conflict.id != dev.id):
        return (f'الرقم التسلسلي "{device_sn}" مستخدم بالفعل '
                f'من قِبل الجهاز "{conflict.name}" في هذه المدرسة')

    dev.name         = name
    dev.ip_address   = ip_address
    dev.port         = port
    dev.username     = username
    if password:
        dev.password = password
    dev.device_sn    = device_sn
    dev.notes        = notes or None
    dev.device_scope = device_scope
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Device CRUD
# ─────────────────────────────────────────────────────────────────────────────

@attendance_devices_bp.route('/', methods=['GET'])
@login_required
@admin_required
def index():
    school  = _school_or_abort()
    devices = (AttendanceDevice.query
               .filter_by(school_id=school.id)
               .order_by(AttendanceDevice.name)
               .all())
    return render_template('attendance_devices/index.html', devices=devices, school=school)


@attendance_devices_bp.route('/aiface-live-status', methods=['GET'])
@login_required
@admin_required
def aiface_live_status():
    """
    JSON endpoint: returns live WebSocket status for all AI Face devices
    belonging to this school.  Polled periodically by the device list page.

    Response shape:
      {
        "sn": {
          "ws_connected": bool,
          "getnewlog_stale": bool,
          "poll_task_running": bool,
          "getnewlog_timeout_count": int,
          "last_reg_at": "ISO" | null,
          "last_sendlog_at": "ISO" | null,
          "last_getnewlog_success_at": "ISO" | null,
          "last_disconnect_at": "ISO" | null
        },
        ...
      }
    """
    from app.services.ai_face_ws import get_all_device_status
    school  = _school_or_abort()
    devices = (AttendanceDevice.query
               .filter_by(school_id=school.id)
               .all())

    def _fmt(v):
        if v is None:
            return None
        if hasattr(v, 'isoformat'):
            return v.strftime('%Y-%m-%d %H:%M:%S')
        return str(v)

    all_status = get_all_device_status()
    result = {}
    for d in devices:
        sn = d.device_sn
        if not sn:
            continue
        st = all_status.get(sn, {})
        result[sn] = {
            "device_id":                 d.id,
            "device_name":               d.name,
            "ws_connected":              st.get("ws_connected", False),
            "getnewlog_stale":           st.get("getnewlog_stale", False),
            "poll_task_running":         st.get("poll_task_running", False),
            "getnewlog_timeout_count":   st.get("getnewlog_timeout_count", 0),
            "last_reg_at":               _fmt(st.get("last_reg_at")),
            "last_sendlog_at":           _fmt(st.get("last_sendlog_at")),
            "last_getnewlog_success_at": _fmt(st.get("last_getnewlog_success_at")),
            "last_disconnect_at":        _fmt(st.get("last_disconnect_at")),
        }
    return jsonify(result)


@attendance_devices_bp.route('/<int:device_id>/aiface-pull-logs', methods=['POST'])
@login_required
@admin_required
def ajax_aiface_pull_logs(device_id):
    """
    AJAX: manually trigger a log pull from an AI Face device.
    Returns raw JSON sent to device, raw JSON received, parsed records, and processing results.

    Request body (JSON):
      cmd     : "getnewlog" | "getalllog"    (default: "getnewlog")
      stn     : true | false                 (default: true)
      process : true | false                 (default: true — create attendance records)
    """
    from app.services.ai_face_ws import send_command_to_device, DeviceOfflineError

    school = _school_or_abort()
    dev    = _get_device_or_404(device_id, school)

    if not dev.device_sn:
        return jsonify({'ok': False, 'error': 'الجهاز لا يملك رقم تسلسلي'}), 400

    body    = request.get_json(silent=True) or {}
    cmd     = body.get('cmd',     'getnewlog')
    stn     = body.get('stn',     True)
    process = body.get('process', True)

    if cmd not in ('getnewlog', 'getalllog'):
        return jsonify({'ok': False, 'error': f'cmd غير مدعوم: {cmd}'}), 400

    payload = {'cmd': cmd, 'stn': stn}

    current_app.logger.info(
        '[aiface] manual pull: device_id=%d sn=%s payload=%s process=%s',
        dev.id, dev.device_sn, payload, process)

    try:
        raw_response = send_command_to_device(dev.device_sn, payload, timeout=25)
    except DeviceOfflineError as exc:
        return jsonify({
            'ok': False, 'offline': True, 'error': str(exc),
            'hint': 'الجهاز غير متصل — تحقق من حالة WebSocket في لوحة AI Face أعلاه',
        }), 503
    except TimeoutError as exc:
        return jsonify({
            'ok': False, 'timeout': True, 'error': str(exc),
            'hint': (f'الجهاز لم يستجب لأمر {cmd} خلال 25 ثانية. '
                     'قد لا يدعم هذا الأمر، أو يحتاج إعادة تشغيل. '
                     'جرب الأمر الآخر (getnewlog ↔ getalllog).'),
        }), 504
    except Exception as exc:
        current_app.logger.exception('[aiface] manual pull error device_id=%d', dev.id)
        return jsonify({'ok': False, 'error': str(exc)}), 500

    count   = raw_response.get('count', 0)
    records = raw_response.get('record') or []

    result = {
        'ok':              True,
        'cmd_sent':        payload,
        'raw_response':    {k: v for k, v in raw_response.items() if k != 'record'},
        'count':           count,
        'records_count':   len(records),
        'records_preview': records[:20],
    }

    if records and process:
        # Call _process_record_list directly so we use the already-validated
        # device object (device_id is certain) and the caller's DB session.
        # _handle_getnewlog_records creates a nested app_context + re-does an
        # SN→device lookup which can resolve to a different row and cause the
        # employee/student mapping filter_by(device_id=...) to miss.
        from app.services.ai_face_ws import _process_record_list
        from app.models import School as _School
        _school_obj = (
            _School.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=dev.school_id)
            .first()
        )
        if _school_obj:
            p, sk, um, er = _process_record_list(
                dev.device_sn, dev, _school_obj, records, source_cmd=cmd)
        else:
            current_app.logger.error('[aiface] no school for device_id=%d school_id=%s',
                                     dev.id, dev.school_id)
            p, sk, um, er = 0, 0, len(records), 0
        result['processing'] = {
            'processed': p, 'skipped': sk, 'unmatched': um, 'errors': er,
        }
        current_app.logger.warning(
            '[aiface] manual pull result: device_id=%d device_sn=%s count=%d '
            'processed=%d skipped=%d unmatched=%d errors=%d',
            dev.id, dev.device_sn, count, p, sk, um, er)
    elif not records:
        result['processing'] = None
        result['hint'] = (
            f'الجهاز أعاد count={count} بدون سجلات. '
            'المؤشر الداخلي للجهاز قد يكون عند النهاية. '
            'جرب: getalllog stn=true للحصول على كل السجلات التاريخية.'
        )

    return jsonify(result)


@attendance_devices_bp.route('/new', methods=['GET', 'POST'])
@login_required
@admin_required
@action_required('attendance_devices', 'add_device')
def new_device():
    school = _school_or_abort()
    if request.method == 'POST':
        dev = AttendanceDevice(school_id=school.id, device_type='hikvision',
                               device_scope='students')
        err = _form_to_device(dev)
        if err:
            flash(err, 'danger')
            return render_template('attendance_devices/form.html', device=None,
                                   school=school, form=request.form)
        if not dev.password:
            flash('كلمة المرور مطلوبة عند إضافة جهاز جديد', 'danger')
            return render_template('attendance_devices/form.html', device=None,
                                   school=school, form=request.form)
        db.session.add(dev)
        db.session.commit()
        flash('تمت إضافة الجهاز بنجاح.', 'success')
        return redirect(url_for('attendance_devices.index'))
    return render_template('attendance_devices/form.html', device=None, school=school, form={})


@attendance_devices_bp.route('/<int:device_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
@action_required('attendance_devices', 'edit_device')
def edit_device(device_id):
    school = _school_or_abort()
    dev    = _get_device_or_404(device_id, school)
    if request.method == 'POST':
        err = _form_to_device(dev)
        if err:
            flash(err, 'danger')
            return render_template('attendance_devices/form.html', device=dev,
                                   school=school, form=request.form)
        db.session.commit()
        flash('تم تحديث بيانات الجهاز.', 'success')
        return redirect(url_for('attendance_devices.index'))
    return render_template('attendance_devices/form.html', device=dev, school=school, form={})


@attendance_devices_bp.route('/<int:device_id>/delete', methods=['POST'])
@login_required
@admin_required
@action_required('attendance_devices', 'delete_device')
def delete_device(device_id):
    school = _school_or_abort()
    dev    = _get_device_or_404(device_id, school)
    db.session.delete(dev)
    db.session.commit()
    flash(f'تم حذف الجهاز "{dev.name}".', 'success')
    return redirect(url_for('attendance_devices.index'))


@attendance_devices_bp.route('/<int:device_id>/toggle', methods=['POST'])
@login_required
@admin_required
def toggle_device(device_id):
    school = _school_or_abort()
    dev    = _get_device_or_404(device_id, school)
    dev.is_active = not dev.is_active
    db.session.commit()
    state = 'تم تفعيل' if dev.is_active else 'تم تعطيل'
    flash(f'{state} الجهاز "{dev.name}".', 'success')
    return redirect(url_for('attendance_devices.index'))


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — connection test & Hikvision sync
# ─────────────────────────────────────────────────────────────────────────────

@attendance_devices_bp.route('/<int:device_id>/test-connection', methods=['POST'])
@login_required
@admin_required
@action_required('attendance_devices', 'test_connection')
def ajax_test_connection(device_id):
    school = _school_or_abort()
    dev    = _get_device_or_404(device_id, school)
    result = test_connection(dev)
    if result.get('ok'):
        return jsonify({'ok': True, 'message': 'تم الاتصال بالجهاز بنجاح',
                        'model': result.get('model'), 'serial_no': result.get('serial_no')})
    return jsonify({'ok': False, 'message': 'تعذر الاتصال بالجهاز',
                    'error': result.get('error')}), 502


@attendance_devices_bp.route('/<int:device_id>/sync', methods=['POST'])
@login_required
@admin_required
@action_required('attendance_devices', 'sync')
def ajax_sync(device_id):
    school = _school_or_abort()
    dev    = _get_device_or_404(device_id, school)

    date_str = (request.get_json(silent=True) or {}).get('date')
    try:
        target_date = (datetime.strptime(date_str, '%Y-%m-%d').date()
                       if date_str else date.today())
    except ValueError:
        return jsonify({'ok': False, 'error': 'تاريخ غير صحيح'}), 400

    result = sync_device(dev, target_date)
    if result.get('ok'):
        return jsonify({'ok': True, 'message': 'تمت مزامنة الحضور بنجاح',
                        'summary': result})
    return jsonify({'ok': False, 'message': 'تعذر الاتصال بالجهاز',
                    'error': result.get('error')}), 502


# ─────────────────────────────────────────────────────────────────────────────
#  Raw event logs
# ─────────────────────────────────────────────────────────────────────────────

@attendance_devices_bp.route('/<int:device_id>/logs', methods=['GET'])
@login_required
@admin_required
@action_required('attendance_devices', 'view_logs')
def logs(device_id):
    school = _school_or_abort()
    dev    = _get_device_or_404(device_id, school)

    status_filter = request.args.get('status', '')
    q = (DeviceEventLog.query
         .filter_by(device_id=dev.id, school_id=school.id)
         .order_by(DeviceEventLog.created_at.desc()))
    if status_filter:
        q = q.filter_by(status=status_filter)
    logs_page = q.limit(200).all()

    return render_template('attendance_devices/logs.html', device=dev, logs=logs_page,
                           status_filter=status_filter, school=school)


# ─────────────────────────────────────────────────────────────────────────────
#  Mappings — scope-aware list + add
# ─────────────────────────────────────────────────────────────────────────────

@attendance_devices_bp.route('/<int:device_id>/mappings', methods=['GET'])
@login_required
@admin_required
def mappings(device_id):
    school = _school_or_abort()
    dev    = _get_device_or_404(device_id, school)
    scope  = getattr(dev, 'device_scope', 'students')

    from app.services.ai_face_ws import is_device_connected
    device_online = is_device_connected(dev.device_sn) if dev.device_sn else False

    student_mappings = []
    employee_mappings = []
    students  = []
    employees = []
    other_devices = (AttendanceDevice.query
                     .filter(AttendanceDevice.school_id == school.id,
                             AttendanceDevice.id != dev.id)
                     .order_by(AttendanceDevice.name)
                     .all())

    if scope in ('students', 'mixed'):
        student_mappings = (DeviceStudentMapping.query
                            .filter_by(device_id=dev.id, school_id=school.id)
                            .order_by(DeviceStudentMapping.employee_no_string)
                            .all())
        students = (Student.query
                    .filter_by(school_id=school.id, status='active')
                    .order_by(Student.full_name)
                    .all())

    if scope in ('employees', 'mixed'):
        employee_mappings = (DeviceEmployeeMapping.query
                             .filter_by(device_id=dev.id, school_id=school.id)
                             .order_by(DeviceEmployeeMapping.enrollment_no)
                             .all())
        employees = (Employee.query
                     .filter_by(school_id=school.id, status='active')
                     .order_by(Employee.full_name)
                     .all())

    return render_template(
        'attendance_devices/mappings.html',
        device=dev,
        scope=scope,
        student_mappings=student_mappings,
        employee_mappings=employee_mappings,
        students=students,
        employees=employees,
        other_devices=other_devices,
        device_online=device_online,
        school=school,
    )


@attendance_devices_bp.route('/<int:device_id>/mappings/add', methods=['POST'])
@login_required
@admin_required
def add_mapping(device_id):
    school = _school_or_abort()
    dev    = _get_device_or_404(device_id, school)
    scope  = getattr(dev, 'device_scope', 'students')

    emp_no = (request.form.get('employee_no_string') or '').strip()
    if not emp_no or not emp_no.isdigit():
        flash('رقم التسجيل يجب أن يكون أرقاماً فقط.', 'danger')
        return redirect(url_for('attendance_devices.mappings', device_id=device_id))

    # ── Students ──────────────────────────────────────────────────────────────
    if scope in ('students', 'mixed'):
        student_id = request.form.get('student_id', type=int)
        if student_id:
            student = Student.query.filter_by(id=student_id, school_id=school.id).first()
            if not student:
                flash('الطالب غير موجود أو لا ينتمي لهذه المدرسة.', 'danger')
                return redirect(url_for('attendance_devices.mappings', device_id=device_id))
            mapping = DeviceStudentMapping(
                school_id=school.id, device_id=dev.id,
                employee_no_string=emp_no, student_id=student.id, is_active=True,
            )
            try:
                db.session.add(mapping)
                db.session.commit()
                flash(f'تم ربط الرقم {emp_no} بالطالب {student.full_name}.', 'success')
            except IntegrityError:
                db.session.rollback()
                flash(f'الرقم {emp_no} مرتبط بطالب آخر في هذا الجهاز.', 'danger')
            return redirect(url_for('attendance_devices.mappings', device_id=device_id))

    # ── Employees ─────────────────────────────────────────────────────────────
    if scope in ('employees', 'mixed'):
        employee_id = request.form.get('employee_id', type=int)
        if not employee_id:
            flash('يرجى اختيار موظف.', 'danger')
            return redirect(url_for('attendance_devices.mappings', device_id=device_id))

        employee = Employee.query.filter_by(id=employee_id, school_id=school.id).first()
        if not employee:
            flash('الموظف غير موجود أو لا ينتمي لهذه المدرسة.', 'danger')
            return redirect(url_for('attendance_devices.mappings', device_id=device_id))

        # For mixed devices also check that enroll_no is not taken by a student mapping
        if scope == 'mixed':
            stu_conflict = DeviceStudentMapping.query.filter_by(
                device_id=dev.id, employee_no_string=emp_no).first()
            if stu_conflict:
                flash(f'الرقم {emp_no} مستخدم بالفعل لطالب على هذا الجهاز.', 'danger')
                return redirect(url_for('attendance_devices.mappings', device_id=device_id))

        mapping = DeviceEmployeeMapping(
            school_id=school.id, device_id=dev.id,
            enrollment_no=emp_no, employee_id=employee.id, is_active=True,
        )
        try:
            db.session.add(mapping)
            db.session.commit()
            flash(f'تم ربط الرقم {emp_no} بالموظف {employee.full_name}.', 'success')
        except IntegrityError:
            db.session.rollback()
            flash(f'الرقم {emp_no} مرتبط بموظف آخر في هذا الجهاز.', 'danger')
        return redirect(url_for('attendance_devices.mappings', device_id=device_id))

    flash('نوع الجهاز غير مدعوم لإضافة ربط.', 'danger')
    return redirect(url_for('attendance_devices.mappings', device_id=device_id))


@attendance_devices_bp.route('/mappings/<int:mapping_id>/delete', methods=['POST'])
@login_required
@admin_required
@section_required('attendance_devices', 'student_mappings')
def delete_mapping(mapping_id):
    school  = _school_or_abort()
    mapping = DeviceStudentMapping.query.filter_by(
        id=mapping_id, school_id=school.id
    ).first_or_404()
    device_id = mapping.device_id
    db.session.delete(mapping)
    db.session.commit()
    flash('تم حذف الربط.', 'success')
    return redirect(url_for('attendance_devices.mappings', device_id=device_id))


@attendance_devices_bp.route('/emp-mappings/<int:mapping_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_emp_mapping(mapping_id):
    school  = _school_or_abort()
    mapping = DeviceEmployeeMapping.query.filter_by(
        id=mapping_id, school_id=school.id
    ).first_or_404()
    device_id = mapping.device_id
    db.session.delete(mapping)
    db.session.commit()
    flash('تم حذف ربط الموظف.', 'success')
    return redirect(url_for('attendance_devices.mappings', device_id=device_id))


# ─────────────────────────────────────────────────────────────────────────────
#  Student mapping copy helpers (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def _do_copy_mappings(source_dev, target_dev, school):
    """Copy active student mappings from source to target (same school)."""
    source_mappings = DeviceStudentMapping.query.filter_by(
        device_id=source_dev.id, is_active=True
    ).all()

    copied = skipped = conflicts = 0
    for src in source_mappings:
        if DeviceStudentMapping.query.filter_by(
            device_id=target_dev.id, employee_no_string=src.employee_no_string,
        ).first():
            skipped += 1
            continue
        if DeviceStudentMapping.query.filter_by(
            device_id=target_dev.id, student_id=src.student_id,
        ).first():
            conflicts += 1
            continue
        db.session.add(DeviceStudentMapping(
            school_id=school.id, device_id=target_dev.id,
            employee_no_string=src.employee_no_string,
            student_id=src.student_id, is_active=True,
        ))
        copied += 1
    return copied, skipped, conflicts


@attendance_devices_bp.route('/<int:device_id>/mappings/copy-from', methods=['POST'])
@login_required
@admin_required
def copy_mappings(device_id):
    school     = _school_or_abort()
    target_dev = _get_device_or_404(device_id, school)
    source_id  = request.form.get('source_device_id', type=int)
    if not source_id:
        flash('يرجى اختيار جهاز المصدر.', 'danger')
        return redirect(url_for('attendance_devices.mappings', device_id=device_id))

    source_dev = AttendanceDevice.query.filter_by(
        id=source_id, school_id=school.id
    ).first_or_404()

    copied, skipped, conflicts = _do_copy_mappings(source_dev, target_dev, school)
    db.session.commit()

    parts = [f'تم نسخ الربط بنجاح من الجهاز "{source_dev.name}".']
    parts.append(f'عدد الروابط المنسوخة: {copied}')
    if skipped:
        parts.append(f'عدد الروابط المتخطاة (رقم موجود): {skipped}')
    if conflicts:
        parts.append(f'عدد التعارضات (طالب مرتبط مسبقاً): {conflicts}')
    flash(' — '.join(parts), 'success' if copied > 0 else 'warning')
    return redirect(url_for('attendance_devices.mappings', device_id=device_id))


@attendance_devices_bp.route('/<int:device_id>/mappings/copy-to', methods=['POST'])
@login_required
@admin_required
def copy_mappings_to(device_id):
    school     = _school_or_abort()
    source_dev = _get_device_or_404(device_id, school)
    target_id  = request.form.get('target_device_id', type=int)
    if not target_id:
        flash('يرجى اختيار الجهاز الهدف.', 'danger')
        return redirect(url_for('attendance_devices.mappings', device_id=device_id))
    if target_id == device_id:
        flash('لا يمكن نسخ الربطات إلى نفس الجهاز.', 'danger')
        return redirect(url_for('attendance_devices.mappings', device_id=device_id))

    target_dev = AttendanceDevice.query.filter_by(
        id=target_id, school_id=school.id
    ).first_or_404()

    copied, skipped, conflicts = _do_copy_mappings(source_dev, target_dev, school)
    db.session.commit()

    parts = [f'تم نسخ الربط بنجاح إلى الجهاز "{target_dev.name}".']
    parts.append(f'عدد الروابط المنسوخة: {copied}')
    if skipped:
        parts.append(f'عدد الروابط المتخطاة (رقم موجود): {skipped}')
    if conflicts:
        parts.append(f'عدد التعارضات (طالب مرتبط مسبقاً): {conflicts}')
    flash(' — '.join(parts), 'success' if copied > 0 else 'warning')
    return redirect(url_for('attendance_devices.mappings', device_id=device_id))


# ─────────────────────────────────────────────────────────────────────────────
#  AI Face sync — student (uses shared aiface_sync service)
# ─────────────────────────────────────────────────────────────────────────────

@attendance_devices_bp.route('/<int:device_id>/status-check', methods=['POST'])
@login_required
@admin_required
def ajax_status_check(device_id):
    """AJAX: return connection status, SN match, and last sync info for this device."""
    from app.services.ai_face_ws import get_connected_sns, is_device_connected

    school = _school_or_abort()
    dev    = _get_device_or_404(device_id, school)

    connected_sns = get_connected_sns()
    is_connected  = is_device_connected(dev.device_sn) if dev.device_sn else False

    if is_connected:
        status = 'متصل'
        badge  = 'success'
    elif connected_sns:
        status = (f'هناك {len(connected_sns)} جهاز متصل لكن SN الجهاز الحالي '
                  f'({dev.device_sn}) غير مطابق — المتصلون: {connected_sns}')
        badge  = 'warning'
    else:
        status = 'لا يوجد أي جهاز متصل حالياً عبر WebSocket'
        badge  = 'danger'

    return jsonify({
        'ok':             True,
        'device_id':      dev.id,
        'device_name':    dev.name,
        'device_sn':      dev.device_sn,
        'device_scope':   getattr(dev, 'device_scope', 'students'),
        'is_connected':   is_connected,
        'connected_sns':  connected_sns,
        'exact_sn_match': dev.device_sn in connected_sns if dev.device_sn else False,
        'last_sync_at':   (dev.last_sync_at.strftime('%Y-%m-%d %H:%M:%S')
                           if dev.last_sync_at else None),
        'status':         status,
        'badge':          badge,
    })


@attendance_devices_bp.route('/<int:device_id>/aiface-sync-student', methods=['POST'])
@login_required
@admin_required
@action_required('attendance_devices', 'sync')
def ajax_aiface_sync_student(device_id):
    """AJAX: push a single student mapping to the AI Face device."""
    from app.services.aiface_sync import sync_person_to_device
    from app.services.ai_face_ws import get_connected_sns

    school = _school_or_abort()
    dev    = _get_device_or_404(device_id, school)

    body       = request.get_json(silent=True) or {}
    mapping_id = body.get('mapping_id')
    if not mapping_id:
        return jsonify({'ok': False, 'error': 'mapping_id مطلوب'}), 400

    mapping = DeviceStudentMapping.query.filter_by(
        id=mapping_id, device_id=dev.id, school_id=school.id
    ).first_or_404()

    student = db.session.get(Student, mapping.student_id)
    if not student:
        return jsonify({'ok': False, 'error': 'الطالب غير موجود'}), 404

    current_app.logger.info(
        '[aiface] sync-student request device_id=%d device_scope=%s device_sn=%s '
        'mapping_id=%d student_id=%d enrollid=%s connected_sns=%s',
        dev.id, getattr(dev, 'device_scope', '?'), dev.device_sn,
        mapping.id, mapping.student_id, mapping.employee_no_string, get_connected_sns())

    result = sync_person_to_device(
        dev,
        enrollid=int(mapping.employee_no_string),
        name=student.full_name,
        photo=student.photo,
        entity_type='student',
    )
    status = 200 if result['ok'] else (503 if result.get('offline') else 502)
    return jsonify(result), status


# ─────────────────────────────────────────────────────────────────────────────
#  AI Face sync — employee
# ─────────────────────────────────────────────────────────────────────────────

@attendance_devices_bp.route('/<int:device_id>/aiface-sync-employee', methods=['POST'])
@login_required
@admin_required
@action_required('attendance_devices', 'sync')
def ajax_aiface_sync_employee(device_id):
    """AJAX: push a single employee mapping to the AI Face device."""
    from app.services.aiface_sync import sync_person_to_device
    from app.services.ai_face_ws import get_connected_sns

    school = _school_or_abort()
    dev    = _get_device_or_404(device_id, school)

    body       = request.get_json(silent=True) or {}
    mapping_id = body.get('mapping_id')
    if not mapping_id:
        return jsonify({'ok': False, 'error': 'mapping_id مطلوب'}), 400

    mapping = DeviceEmployeeMapping.query.filter_by(
        id=mapping_id, device_id=dev.id, school_id=school.id
    ).first_or_404()

    employee = db.session.get(Employee, mapping.employee_id)
    if not employee:
        return jsonify({'ok': False, 'error': 'الموظف غير موجود'}), 404

    current_app.logger.info(
        '[aiface] sync-employee request device_id=%d device_scope=%s device_sn=%s '
        'mapping_id=%d employee_id=%d enrollid=%s connected_sns=%s',
        dev.id, getattr(dev, 'device_scope', '?'), dev.device_sn,
        mapping.id, mapping.employee_id, mapping.enrollment_no, get_connected_sns())

    result = sync_person_to_device(
        dev,
        enrollid=int(mapping.enrollment_no),
        name=employee.full_name,
        photo=employee.photo,
        entity_type='employee',
    )
    status = 200 if result['ok'] else (503 if result.get('offline') else 502)
    return jsonify(result), status


# ─────────────────────────────────────────────────────────────────────────────
#  AI Face sync — all (scope-aware)
# ─────────────────────────────────────────────────────────────────────────────

@attendance_devices_bp.route('/<int:device_id>/aiface-sync-all', methods=['POST'])
@login_required
@admin_required
@action_required('attendance_devices', 'sync')
def ajax_aiface_sync_all(device_id):
    """AJAX: push all active mappings for this device (scope-aware)."""
    from app.services.aiface_sync import sync_person_to_device

    school = _school_or_abort()
    dev    = _get_device_or_404(device_id, school)
    scope  = getattr(dev, 'device_scope', 'students')

    succeeded = []
    failed    = []

    def _run_sync(enrollid_str, name, photo, entity_type):
        res = sync_person_to_device(dev, int(enrollid_str), name, photo, entity_type)
        if res.get('offline'):
            return res  # propagate offline signal up
        if res['ok']:
            succeeded.append(enrollid_str)
        else:
            failed.append({'enrollid': enrollid_str, 'name': name,
                           'error': res.get('message', '')})
        return None

    if scope in ('students', 'mixed'):
        active_mappings = (DeviceStudentMapping.query
                           .filter_by(device_id=dev.id, school_id=school.id, is_active=True)
                           .all())
        for m in active_mappings:
            student = db.session.get(Student, m.student_id)
            if not student:
                continue
            offline_res = _run_sync(m.employee_no_string, student.full_name,
                                    student.photo, 'student')
            if offline_res:
                return jsonify({'ok': False, 'offline': True,
                                'message': offline_res.get('message')}), 503

    if scope in ('employees', 'mixed'):
        active_emp_mappings = (DeviceEmployeeMapping.query
                               .filter_by(device_id=dev.id, school_id=school.id, is_active=True)
                               .all())
        for m in active_emp_mappings:
            employee = db.session.get(Employee, m.employee_id)
            if not employee:
                continue
            offline_res = _run_sync(m.enrollment_no, employee.full_name,
                                    employee.photo, 'employee')
            if offline_res:
                return jsonify({'ok': False, 'offline': True,
                                'message': offline_res.get('message')}), 503

    total = len(succeeded) + len(failed)
    if total == 0:
        return jsonify({'ok': False, 'message': 'لا توجد ربطات نشطة لهذا الجهاز'}), 400

    return jsonify({
        'ok':        len(failed) == 0,
        'succeeded': len(succeeded),
        'failed':    len(failed),
        'errors':    failed,
        'message':   (f'تم إرسال {len(succeeded)} شخص بنجاح'
                      + (f'، فشل {len(failed)}' if failed else '')),
    })


# ─────────────────────────────────────────────────────────────────────────────
#  AI Face delete from device (scope-aware)
# ─────────────────────────────────────────────────────────────────────────────

@attendance_devices_bp.route('/<int:device_id>/aiface-delete-from-device', methods=['POST'])
@login_required
@admin_required
@action_required('attendance_devices', 'delete_device')
def ajax_aiface_delete_from_device(device_id):
    """
    AJAX: send deleteuser to the AI Face device then remove the mapping from DB.
    Body: {mapping_id, mapping_type: "student"|"employee"}
    If offline, queues the deleteuser command and keeps the DB mapping.
    """
    from app.services.ai_face_ws import (send_command_to_device, DeviceOfflineError,
                                          queue_command_for_device)
    school = _school_or_abort()
    dev    = _get_device_or_404(device_id, school)

    body         = request.get_json(silent=True) or {}
    mapping_id   = body.get('mapping_id')
    mapping_type = body.get('mapping_type', 'student')

    if not mapping_id:
        return jsonify({'ok': False, 'error': 'mapping_id مطلوب'}), 400

    # Resolve mapping + entity name
    if mapping_type == 'employee':
        mapping = DeviceEmployeeMapping.query.filter_by(
            id=mapping_id, device_id=dev.id, school_id=school.id
        ).first_or_404()
        enrollid     = int(mapping.enrollment_no)
        entity_name  = (mapping.employee.full_name
                        if mapping.employee else f'enrollid={enrollid}')
    else:
        mapping = DeviceStudentMapping.query.filter_by(
            id=mapping_id, device_id=dev.id, school_id=school.id
        ).first_or_404()
        enrollid     = int(mapping.employee_no_string)
        entity_name  = (mapping.student.full_name
                        if mapping.student else f'enrollid={enrollid}')

    delete_cmd = {'cmd': 'deleteuser', 'enrollid': enrollid, 'backupnum': 13}

    current_app.logger.info(
        '[aiface] delete start: mapping_id=%d mapping_type=%s enrollid=%d sn=%s entity=%s',
        mapping.id, mapping_type, enrollid, dev.device_sn, entity_name)

    try:
        result = send_command_to_device(dev.device_sn, delete_cmd, timeout=10)
        current_app.logger.info(
            '[aiface] deleteuser enrollid=%d response: result=%s full=%s',
            enrollid, result.get('result'), result)
    except DeviceOfflineError:
        queue_command_for_device(
            dev.device_sn, delete_cmd,
            note=f'delete mapping_id={mapping.id} entity={entity_name}',
        )
        current_app.logger.warning(
            '[aiface] device offline — deleteuser queued: enrollid=%d sn=%s',
            enrollid, dev.device_sn)
        return jsonify({
            'ok': False, 'offline': True, 'queued': True,
            'message': ('الجهاز غير متصل — تم وضع أمر الحذف في قائمة الانتظار '
                        'وسيُنفَّذ تلقائياً عند اتصال الجهاز. '
                        'الربط في قاعدة البيانات محفوظ حتى يؤكد الجهاز الحذف.'),
        }), 200
    except TimeoutError as exc:
        current_app.logger.warning('[aiface] deleteuser timeout enrollid=%d: %s', enrollid, exc)
        return jsonify({'ok': False, 'error_type': 'timeout',
                        'message': f'انتهت مهلة الاتصال بالجهاز: {exc}'}), 504
    except Exception as exc:
        current_app.logger.exception('[aiface] unexpected error on deleteuser enrollid=%d', enrollid)
        return jsonify({'ok': False, 'error_type': 'internal_error',
                        'message': str(exc)}), 500

    if result.get('result'):
        mapping_id_log = mapping.id
        db.session.delete(mapping)
        db.session.commit()
        current_app.logger.info(
            '[aiface] deleteuser result=true — mapping removed from DB: '
            'mapping_id=%d enrollid=%d', mapping_id_log, enrollid)
        return jsonify({
            'ok':      True,
            'message': f'تم حذف {entity_name} (رقم {enrollid}) من الجهاز وإزالة الربط بنجاح',
        })

    current_app.logger.warning(
        '[aiface] deleteuser result=false enrollid=%d response=%s', enrollid, result)
    return jsonify({
        'ok': False, 'error_type': 'device_result_false',
        'message': f'الجهاز أعاد result=false للرقم {enrollid} — الربط محفوظ',
        'device_response': result,
    }), 502

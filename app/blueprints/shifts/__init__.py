"""
Attendance Shifts blueprint — CRUD for AttendanceShift records.

All routes are POST-only (or GET for edit form) and redirect back to
admin.attendance_settings. There is NO sidebar entry for this blueprint;
the shifts management UI is embedded inside the attendance settings page.

Routes:
  POST /attendance-shifts/create
  POST /attendance-shifts/<id>/edit
  POST /attendance-shifts/<id>/toggle
  POST /attendance-shifts/<id>/delete
"""
from flask import Blueprint, redirect, url_for, flash, request
from flask_login import login_required, current_user
from datetime import time as _time

from app.models import db, AttendanceShift, Section
from app.utils.decorators import get_current_school

shifts_bp = Blueprint('shifts', __name__)


def _require_admin():
    if not current_user.is_authenticated:
        return False
    if current_user.is_super_admin or current_user.is_school_admin:
        return True
    # Roles granted manage_attendance_settings may manage shifts — the shifts
    # UI lives inside the attendance-settings page that permission unlocks.
    return current_user.has_permission('manage_attendance_settings')


def _parse_time(s: str) -> _time | None:
    if not s or not s.strip():
        return None
    try:
        h, m = map(int, s.strip().split(':')[:2])
        return _time(h, m)
    except (ValueError, AttributeError):
        return None


@shifts_bp.route('/create', methods=['POST'])
@login_required
def create_shift():
    if not _require_admin():
        flash('ليس لديك صلاحية.', 'danger')
        return redirect(url_for('admin.attendance_settings'))

    school = get_current_school()
    if not school:
        flash('لم يتم تحديد المدرسة.', 'danger')
        return redirect(url_for('admin.attendance_settings'))

    name              = request.form.get('name', '').strip()
    start_time        = _parse_time(request.form.get('start_time', ''))
    late_after_time   = _parse_time(request.form.get('late_after_time', ''))
    absent_after_time = _parse_time(request.form.get('absent_after_time', ''))
    dismissal_time    = _parse_time(request.form.get('dismissal_time', ''))

    if not name:
        flash('اسم الشفت مطلوب.', 'danger')
        return redirect(url_for('admin.attendance_settings'))
    if not start_time or not late_after_time or not absent_after_time:
        flash('أوقات البداية والتأخر والغياب التلقائي مطلوبة.', 'danger')
        return redirect(url_for('admin.attendance_settings'))
    if late_after_time <= start_time:
        flash('وقت التأخر يجب أن يكون بعد وقت البداية.', 'warning')
        return redirect(url_for('admin.attendance_settings'))
    if absent_after_time <= start_time:
        flash('وقت الغياب التلقائي يجب أن يكون بعد وقت البداية.', 'warning')
        return redirect(url_for('admin.attendance_settings'))

    existing = (AttendanceShift.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=school.id, name=name)
                .first())
    if existing:
        flash(f'يوجد شفت باسم "{name}" بالفعل.', 'warning')
        return redirect(url_for('admin.attendance_settings'))

    shift = AttendanceShift(
        school_id         = school.id,
        name              = name,
        start_time        = start_time,
        late_after_time   = late_after_time,
        absent_after_time = absent_after_time,
        dismissal_time    = dismissal_time,
        is_active         = True,
    )
    db.session.add(shift)
    db.session.commit()
    flash(f'تم إضافة الشفت "{name}" بنجاح.', 'success')
    return redirect(url_for('admin.attendance_settings'))


@shifts_bp.route('/<int:shift_id>/edit', methods=['POST'])
@login_required
def edit_shift(shift_id):
    if not _require_admin():
        flash('ليس لديك صلاحية.', 'danger')
        return redirect(url_for('admin.attendance_settings'))

    school = get_current_school()
    shift = (AttendanceShift.query
             .execution_options(bypass_tenant_scope=True)
             .get_or_404(shift_id))

    if school and shift.school_id != school.id:
        flash('لا يمكن تعديل هذا الشفت.', 'danger')
        return redirect(url_for('admin.attendance_settings'))

    name              = request.form.get('name', '').strip() or shift.name
    start_time        = _parse_time(request.form.get('start_time', ''))
    late_after_time   = _parse_time(request.form.get('late_after_time', ''))
    absent_after_time = _parse_time(request.form.get('absent_after_time', ''))
    dismissal_time    = _parse_time(request.form.get('dismissal_time', ''))

    if not start_time or not late_after_time or not absent_after_time:
        flash('أوقات البداية والتأخر والغياب التلقائي مطلوبة.', 'danger')
        return redirect(url_for('admin.attendance_settings'))
    if late_after_time <= start_time:
        flash('وقت التأخر يجب أن يكون بعد وقت البداية.', 'warning')
        return redirect(url_for('admin.attendance_settings'))
    if absent_after_time <= start_time:
        flash('وقت الغياب التلقائي يجب أن يكون بعد وقت البداية.', 'warning')
        return redirect(url_for('admin.attendance_settings'))

    # Check duplicate name (exclude self)
    dup = (AttendanceShift.query
           .execution_options(bypass_tenant_scope=True)
           .filter(
               AttendanceShift.school_id == shift.school_id,
               AttendanceShift.name == name,
               AttendanceShift.id != shift_id,
           ).first())
    if dup:
        flash(f'يوجد شفت باسم "{name}" بالفعل.', 'warning')
        return redirect(url_for('admin.attendance_settings'))

    shift.name              = name
    shift.start_time        = start_time
    shift.late_after_time   = late_after_time
    shift.absent_after_time = absent_after_time
    shift.dismissal_time    = dismissal_time
    db.session.commit()
    flash(f'تم تحديث الشفت "{name}" بنجاح.', 'success')
    return redirect(url_for('admin.attendance_settings'))


@shifts_bp.route('/<int:shift_id>/toggle', methods=['POST'])
@login_required
def toggle_shift(shift_id):
    if not _require_admin():
        flash('ليس لديك صلاحية.', 'danger')
        return redirect(url_for('admin.attendance_settings'))

    school = get_current_school()
    shift = (AttendanceShift.query
             .execution_options(bypass_tenant_scope=True)
             .get_or_404(shift_id))

    if school and shift.school_id != school.id:
        flash('لا يمكن تعديل هذا الشفت.', 'danger')
        return redirect(url_for('admin.attendance_settings'))

    shift.is_active = not shift.is_active
    db.session.commit()
    state = 'مفعَّل' if shift.is_active else 'معطَّل'
    flash(f'الشفت "{shift.name}" الآن {state}.', 'info')
    return redirect(url_for('admin.attendance_settings'))


@shifts_bp.route('/<int:shift_id>/delete', methods=['POST'])
@login_required
def delete_shift(shift_id):
    if not _require_admin():
        flash('ليس لديك صلاحية.', 'danger')
        return redirect(url_for('admin.attendance_settings'))

    school = get_current_school()
    shift = (AttendanceShift.query
             .execution_options(bypass_tenant_scope=True)
             .get_or_404(shift_id))

    if school and shift.school_id != school.id:
        flash('لا يمكن حذف هذا الشفت.', 'danger')
        return redirect(url_for('admin.attendance_settings'))

    # Block delete if any section is still assigned to this shift
    in_use = (Section.query
              .execution_options(bypass_tenant_scope=True)
              .filter_by(shift_id=shift_id)
              .count())
    if in_use:
        flash(
            f'لا يمكن حذف الشفت "{shift.name}" لأنه مرتبط بـ {in_use} شعبة. '
            f'قم بإلغاء تعيين الشعب أولاً، أو عطِّل الشفت بدلاً من حذفه.',
            'warning',
        )
        return redirect(url_for('admin.attendance_settings'))

    name = shift.name
    db.session.delete(shift)
    db.session.commit()
    flash(f'تم حذف الشفت "{name}".', 'success')
    return redirect(url_for('admin.attendance_settings'))

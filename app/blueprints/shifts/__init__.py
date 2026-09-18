"""
Attendance Shifts blueprint — CRUD for AttendanceShift records.

All routes are POST-only (or GET for edit form) and redirect back to
admin.attendance_settings. There is NO sidebar entry for this blueprint;
the shifts management UI is embedded inside the attendance settings page.

The automatic-absence cutoff is NOT a per-shift setting: every shift of a
school shares School.shift_absent_after_time, edited through
`update_global_absence` below.  AttendanceShift.absent_after_time is retained
in the database for rollback/audit and is no longer read for behaviour.

Routes:
  POST /attendance-shifts/create
  POST /attendance-shifts/<id>/edit
  POST /attendance-shifts/<id>/toggle
  POST /attendance-shifts/<id>/delete
  POST /attendance-shifts/global-absence
"""
from flask import Blueprint, redirect, url_for, flash, request
from flask_login import login_required, current_user
from datetime import time as _time

from app.models import db, AttendanceShift, School, Section
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
    dismissal_time    = _parse_time(request.form.get('dismissal_time', ''))

    # Lateness is OPTIONAL for institutes only (School.is_institute); a blank
    # value means "no lateness for this shift". Schools keep the existing
    # required-field validation unchanged.
    lateness_optional = bool(getattr(school, 'is_institute', False))

    if not name:
        flash('اسم الشفت مطلوب.', 'danger')
        return redirect(url_for('admin.attendance_settings'))
    if not start_time:
        flash('وقت بداية الدوام مطلوب.', 'danger')
        return redirect(url_for('admin.attendance_settings'))
    if late_after_time is None and not lateness_optional:
        flash('أوقات البداية والتأخر مطلوبة.', 'danger')
        return redirect(url_for('admin.attendance_settings'))
    if late_after_time is not None and late_after_time <= start_time:
        flash('وقت التأخر يجب أن يكون بعد وقت البداية.', 'warning')
        return redirect(url_for('admin.attendance_settings'))

    existing = (AttendanceShift.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=school.id, name=name)
                .first())
    if existing:
        flash(f'يوجد شفت باسم "{name}" بالفعل.', 'warning')
        return redirect(url_for('admin.attendance_settings'))

    # absent_after_time is LEGACY: it is never read for the auto-absence
    # decision (School.shift_absent_after_time is).  Populate it with the
    # school-wide cutoff when one is configured, otherwise the shift's own late
    # threshold as an inert placeholder.  When neither exists (an institute that
    # left lateness blank) it stays NULL — no time is invented and start_time is
    # never substituted.  Nothing reads it either way.
    legacy_absent = getattr(school, 'shift_absent_after_time', None) or late_after_time

    shift = AttendanceShift(
        school_id         = school.id,
        name              = name,
        start_time        = start_time,
        late_after_time   = late_after_time,
        absent_after_time = legacy_absent,
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
    dismissal_time    = _parse_time(request.form.get('dismissal_time', ''))

    # Lateness is OPTIONAL for institutes only; clearing the field switches
    # lateness off for this shift. Schools keep the existing validation.
    lateness_optional = bool(getattr(school, 'is_institute', False))

    if not start_time:
        flash('وقت بداية الدوام مطلوب.', 'danger')
        return redirect(url_for('admin.attendance_settings'))
    if late_after_time is None and not lateness_optional:
        flash('أوقات البداية والتأخر مطلوبة.', 'danger')
        return redirect(url_for('admin.attendance_settings'))
    if late_after_time is not None and late_after_time <= start_time:
        flash('وقت التأخر يجب أن يكون بعد وقت البداية.', 'warning')
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
    # shift.absent_after_time is intentionally NOT touched — the historical
    # per-shift value is preserved for rollback/audit.
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


@shifts_bp.route('/global-absence', methods=['POST'])
@login_required
def update_global_absence():
    """
    Set the school-wide automatic-absence cutoff shared by ALL shifts
    (School.shift_absent_after_time).

    Lives in its own tiny form because #shiftSettingsPane sits outside the main
    attendance-settings form (that pane carries the shift CRUD sub-forms and
    nested forms are invalid HTML).

    The school is resolved from the trusted server-side session context only —
    a school_id in the request body is never read.  Submitting an empty value
    clears the setting back to NULL, which makes shift auto-absence fail closed.
    Unified mode's School.att_absence_threshold is not touched here.

    A non-empty cutoff is REJECTED (nothing is saved, the previous value stays)
    unless it is strictly after the start_time of every active shift.
    """
    from app.utils.audit import log_action

    if not _require_admin():
        flash('ليس لديك صلاحية.', 'danger')
        return redirect(url_for('admin.attendance_settings'))

    school = get_current_school()
    if not school or not isinstance(school, School):
        flash('لم يتم تحديد المدرسة.', 'danger')
        return redirect(url_for('admin.attendance_settings'))

    raw = request.form.get('shift_absent_after_time', '')
    cutoff = _parse_time(raw)

    if raw.strip() and cutoff is None:
        flash('صيغة الوقت غير صحيحة.', 'danger')
        return redirect(url_for('admin.attendance_settings'))

    # BLOCKING validation — the cutoff must be strictly AFTER the start of every
    # active shift.  A cutoff at or before a shift's start_time would mark that
    # shift's students absent before their day begins, sending parent
    # notifications that cannot be unsent.  Validate BEFORE assigning so a
    # rejected submission leaves the stored value completely unchanged.
    if cutoff is not None:
        invalid = (AttendanceShift.query
                   .execution_options(bypass_tenant_scope=True)
                   .filter(AttendanceShift.school_id == school.id,
                           AttendanceShift.is_active.is_(True),
                           AttendanceShift.start_time >= cutoff)
                   .order_by(AttendanceShift.start_time)
                   .all())
        if invalid:
            details = '، '.join(
                f'{sh.name} (يبدأ {sh.start_time.strftime("%H:%M")})'
                for sh in invalid
            )
            flash(
                f'لم يتم الحفظ: وقت الغياب التلقائي ({cutoff.strftime("%H:%M")}) '
                f'يجب أن يكون بعد بداية دوام كل الشفتات المفعَّلة. '
                f'الشفتات التالية تبدأ في نفس الوقت أو بعده: {details}. '
                f'لم يتم تغيير الوقت المحفوظ سابقاً.',
                'danger',
            )
            return redirect(url_for('admin.attendance_settings'))

    school.shift_absent_after_time = cutoff
    db.session.commit()
    log_action('edit', 'school_settings', school.id,
               details='shift global auto-absence time updated')

    if cutoff is None:
        flash('تم إلغاء وقت الغياب التلقائي للشفتات. لن يتم تسجيل الغياب '
              'التلقائي لأي شفت حتى يتم تحديد وقت.', 'warning')
        return redirect(url_for('admin.attendance_settings'))

    flash(f'تم حفظ وقت الغياب التلقائي للشفتات: {cutoff.strftime("%H:%M")}.', 'success')
    return redirect(url_for('admin.attendance_settings'))

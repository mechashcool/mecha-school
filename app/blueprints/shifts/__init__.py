"""
Attendance Shifts blueprint — CRUD for AttendanceShift records.

All routes are POST-only (or GET for edit form) and redirect back to
admin.attendance_settings. There is NO sidebar entry for this blueprint;
the shifts management UI is embedded inside the attendance settings page.

The automatic-absence cutoff is NOT a per-shift setting: every shift of a
school shares School.shift_absent_after_time, edited through
`update_global_absence` below.  AttendanceShift.absent_after_time is retained
in the database for rollback/audit and is no longer read for behaviour.

EMPLOYEE shifts are a SEPARATE model (EmployeeAttendanceShift) with its own
routes at the bottom of this file. They share the validation shape and the
school-resolution pattern but touch no student row: student and employee shift
data, toggles and cutoffs are fully independent.

Routes:
  POST /attendance-shifts/create
  POST /attendance-shifts/<id>/edit
  POST /attendance-shifts/<id>/toggle
  POST /attendance-shifts/<id>/delete
  POST /attendance-shifts/global-absence
  POST /attendance-shifts/employee/create
  POST /attendance-shifts/employee/<id>/edit
  POST /attendance-shifts/employee/<id>/toggle
  POST /attendance-shifts/employee/<id>/delete
  POST /attendance-shifts/employee/global-absence
"""
from flask import Blueprint, redirect, url_for, flash, request
from flask_login import login_required, current_user
from datetime import time as _time

from app.models import (db, AttendanceShift, EmployeeAttendanceShift, Employee,
                        School, Section)
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


# ─────────────────────────────────────────────────────────────────────────────
#  SHIFT MODE ORDERING RULE  (students and employees, both directions)
#
#      shift start  <  shift late threshold  <  GLOBAL ABSENCE CUTOFF
#
#  The one school-level absence cutoff must be STRICTLY AFTER the effective late
#  threshold of every ACTIVE shift, so nobody in a later shift can be marked
#  absent while still inside their valid arrival window. Equality is invalid.
#  dismissal_time is deliberately NOT part of this rule — the cutoff decides when
#  a missing record may become an absence, not when the day ends.
#
#  Both directions are guarded: saving the cutoff, and creating / editing /
#  re-activating a shift once a cutoff exists. Existing stored configuration is
#  never repaired, altered or deactivated — only new saves and activations are
#  blocked.
# ─────────────────────────────────────────────────────────────────────────────

# audience → (model, School cutoff attribute, label used in messages)
_SHIFT_AUDIENCES = {
    'students':  (AttendanceShift, 'shift_absent_after_time', 'الشفتات'),
}


def _active_shifts(school, audience):
    """Active shifts of `school` for one audience. Explicit school filter."""
    model = _SHIFT_AUDIENCES[audience][0]
    return (model.query
            .execution_options(bypass_tenant_scope=True)
            .filter(model.school_id == school.id,
                    model.is_active.is_(True))
            .all())


def _boundary_label(school, audience, shift):
    """'التأخير' or, when lateness is disabled on both sources, 'البداية'.

    Keeps the validation message truthful about WHICH boundary was compared.
    """
    from app.utils.attendance_helpers import effective_shift_late_threshold

    return ('التأخير'
            if effective_shift_late_threshold(school, audience, shift) is not None
            else 'البداية')


def _reject_cutoff_conflicts(school, audience, shifts, cutoff):
    """Arabic error message when `cutoff` is invalid for `shifts`, else None.

    Used when SAVING a global absence cutoff: every active shift is checked
    against its absence boundary (effective late threshold, else start_time).
    """
    from app.utils.attendance_helpers import conflicting_shift_late_thresholds

    conflicts = conflicting_shift_late_thresholds(school, audience, shifts, cutoff)
    if not conflicts:
        return None
    label = _SHIFT_AUDIENCES[audience][2]
    details = '، '.join(
        f'{sh.name} ({_boundary_label(school, audience, sh)} '
        f'{boundary.strftime("%H:%M")})'
        for sh, boundary in conflicts
    )
    return (
        f'لم يتم الحفظ: وقت اعتبار الغياب ({cutoff.strftime("%H:%M")}) يجب أن يكون '
        f'بعد وقت التأخير لجميع {label} الفعالة (أو بعد وقت البداية عند تعطيل '
        f'التأخير). {label} التالية وقتها في نفس الوقت أو بعده: {details}. '
        f'لم يتم تغيير الوقت المحفوظ سابقاً.'
    )


def _reject_shift_against_cutoff(school, audience, late_after_time, start_time,
                                 *, activating=False):
    """Arabic error when ONE shift's absence boundary reaches the stored global
    cutoff, else None.

    The reverse direction: a shift may not be created, edited into, or activated
    into a state where its arrival window extends to or past the configured
    cutoff.  The boundary is the effective late threshold — `late_after_time`
    when set, else the audience's school-level threshold, exactly as at runtime —
    falling back to `start_time` when lateness is disabled on both sources.
    No cutoff configured → nothing to conflict with.
    """
    from types import SimpleNamespace
    from app.utils.attendance_helpers import conflicting_shift_late_thresholds

    cutoff_attr = _SHIFT_AUDIENCES[audience][1]
    cutoff = getattr(school, cutoff_attr, None) if school else None
    if cutoff is None:
        return None

    probe = SimpleNamespace(late_after_time=late_after_time, start_time=start_time)
    conflicts = conflicting_shift_late_thresholds(school, audience, [probe], cutoff)
    if not conflicts:
        return None

    boundary = conflicts[0][1]
    action = 'تفعيل هذا الشفت' if activating else 'حفظ هذا الشفت'
    return (
        f'لم يتم {action}: وقت {_boundary_label(school, audience, probe)} '
        f'({boundary.strftime("%H:%M")}) يساوي أو يتجاوز '
        f'وقت اعتبار الغياب المحفوظ ({cutoff.strftime("%H:%M")}). '
        f'عدِّل وقت اعتبار الغياب إلى وقت لاحق أولاً، ثم أعد المحاولة. '
        f'لم يتم تغيير أي بيانات.'
    )


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

    # Reverse direction of the ordering rule: a new shift may not reach or pass
    # the already-stored global absence cutoff.
    _cutoff_err = _reject_shift_against_cutoff(
        school, 'students', late_after_time, start_time)
    if _cutoff_err:
        flash(_cutoff_err, 'danger')
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

    # Reverse direction: the edited times may not reach or pass the stored global
    # absence cutoff. Checked BEFORE any assignment, so a rejected edit leaves
    # the shift exactly as it was.
    _cutoff_err = _reject_shift_against_cutoff(
        school, 'students', late_after_time, start_time)
    if _cutoff_err:
        flash(_cutoff_err, 'danger')
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

    # Re-ACTIVATION only: an inactive shift that conflicts with the stored global
    # absence cutoff may stay stored, but must not become active. Deactivating is
    # always allowed (it can only remove a conflict).
    if not shift.is_active:
        _cutoff_err = _reject_shift_against_cutoff(
            school, 'students', shift.late_after_time, shift.start_time,
            activating=True)
        if _cutoff_err:
            flash(_cutoff_err, 'danger')
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
    unless it is strictly after the EFFECTIVE LATE THRESHOLD of every active
    shift (see _reject_cutoff_conflicts).
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

    # BLOCKING validation — the cutoff must be strictly AFTER the EFFECTIVE LATE
    # THRESHOLD of every active shift (previously this compared against
    # start_time, which allowed a cutoff to land inside a later shift's lateness
    # window). A cutoff at or before a shift's late threshold would mark that
    # shift's students absent while they could still legitimately arrive, sending
    # parent notifications that cannot be unsent. Validate BEFORE assigning so a
    # rejected submission leaves the stored value completely unchanged.
    if cutoff is not None:
        err = _reject_cutoff_conflicts(school, 'students',
                                      _active_shifts(school, 'students'), cutoff)
        if err:
            flash(err, 'danger')
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


# ═════════════════════════════════════════════════════════════════════════════
#  EMPLOYEE SHIFTS — EmployeeAttendanceShift CRUD
#
#  Separate rows and separate toggle (School.emp_enable_attendance_shifts) from
#  the student shifts above. Nothing in this section reads or writes
#  AttendanceShift, Section,
#  Grade or StudentAttendance.
#
#  The school is always resolved from the trusted server-side session context
#  (get_current_school); a school_id in the request body is never read, and
#  every row is re-checked against it before being modified.
# ═════════════════════════════════════════════════════════════════════════════

def _emp_shift_or_none(shift_id, school):
    """Fetch an employee shift and verify it belongs to the current school.

    bypass_tenant_scope + an explicit school_id comparison, matching the
    student shift routes: a cross-school id is treated exactly like a missing
    one, so the response never reveals that it exists elsewhere.
    """
    shift = (EmployeeAttendanceShift.query
             .execution_options(bypass_tenant_scope=True)
             .get(shift_id))
    if shift is None or not school or shift.school_id != school.id:
        return None
    return shift


def _emp_shift_times_or_error(school):
    """Parse and validate one employee shift's independent time window."""
    name           = request.form.get('name', '').strip()
    start_time     = _parse_time(request.form.get('start_time', ''))
    late_after     = _parse_time(request.form.get('late_after_time', ''))
    absent_after   = _parse_time(request.form.get('absent_after_time', ''))
    dismissal_time = _parse_time(request.form.get('dismissal_time', ''))

    lateness_optional = bool(getattr(school, 'is_institute', False))

    if not start_time:
        return name, None, None, None, None, 'وقت بداية الدوام مطلوب.'
    if late_after is None and not lateness_optional:
        return name, None, None, None, None, 'أوقات البداية والتأخر مطلوبة.'
    if late_after is not None and late_after <= start_time:
        return name, None, None, None, None, 'وقت التأخر يجب أن يكون بعد وقت البداية.'
    if absent_after is None:
        return name, None, None, None, None, 'وقت الغياب التلقائي مطلوب.'
    absence_boundary = late_after if late_after is not None else start_time
    if absent_after <= absence_boundary:
        label = 'وقت التأخر' if late_after is not None else 'وقت بداية الشفت'
        return (name, None, None, None, None,
                f'وقت الغياب التلقائي يجب أن يكون بعد {label}.')

    return name, start_time, late_after, absent_after, dismissal_time, None


@shifts_bp.route('/employee/create', methods=['POST'])
@login_required
def create_employee_shift():
    if not _require_admin():
        flash('ليس لديك صلاحية.', 'danger')
        return redirect(url_for('admin.attendance_settings'))

    school = get_current_school()
    if not school or not isinstance(school, School):
        flash('لم يتم تحديد المدرسة.', 'danger')
        return redirect(url_for('admin.attendance_settings'))

    (name, start_time, late_after, absent_after,
     dismissal_time, err) = _emp_shift_times_or_error(school)
    if not name:
        flash('اسم الشفت مطلوب.', 'danger')
        return redirect(url_for('admin.attendance_settings'))
    if err:
        flash(err, 'danger')
        return redirect(url_for('admin.attendance_settings'))

    existing = (EmployeeAttendanceShift.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=school.id, name=name)
                .first())
    if existing:
        flash(f'يوجد شفت موظفين بنفس الاسم بالفعل: "{name}".', 'warning')
        return redirect(url_for('admin.attendance_settings'))

    db.session.add(EmployeeAttendanceShift(
        school_id       = school.id,
        name            = name,
        start_time      = start_time,
        late_after_time = late_after,
        absent_after_time = absent_after,
        dismissal_time  = dismissal_time,
        is_active       = True,
    ))
    db.session.commit()
    flash(f'تم إضافة شفت الموظفين "{name}" بنجاح.', 'success')
    return redirect(url_for('admin.attendance_settings'))


@shifts_bp.route('/employee/<int:shift_id>/edit', methods=['POST'])
@login_required
def edit_employee_shift(shift_id):
    if not _require_admin():
        flash('ليس لديك صلاحية.', 'danger')
        return redirect(url_for('admin.attendance_settings'))

    school = get_current_school()
    shift  = _emp_shift_or_none(shift_id, school)
    if shift is None:
        flash('لا يمكن تعديل هذا الشفت.', 'danger')
        return redirect(url_for('admin.attendance_settings'))

    (name, start_time, late_after, absent_after,
     dismissal_time, err) = _emp_shift_times_or_error(school)
    name = name or shift.name
    if err:
        flash(err, 'danger')
        return redirect(url_for('admin.attendance_settings'))

    dup = (EmployeeAttendanceShift.query
           .execution_options(bypass_tenant_scope=True)
           .filter(EmployeeAttendanceShift.school_id == shift.school_id,
                   EmployeeAttendanceShift.name == name,
                   EmployeeAttendanceShift.id != shift.id)
           .first())
    if dup:
        flash(f'يوجد شفت موظفين بنفس الاسم بالفعل: "{name}".', 'warning')
        return redirect(url_for('admin.attendance_settings'))

    shift.name            = name
    shift.start_time      = start_time
    shift.late_after_time = late_after
    shift.absent_after_time = absent_after
    shift.dismissal_time  = dismissal_time
    db.session.commit()
    flash(f'تم تحديث شفت الموظفين "{name}" بنجاح.', 'success')
    return redirect(url_for('admin.attendance_settings'))


@shifts_bp.route('/employee/<int:shift_id>/toggle', methods=['POST'])
@login_required
def toggle_employee_shift(shift_id):
    if not _require_admin():
        flash('ليس لديك صلاحية.', 'danger')
        return redirect(url_for('admin.attendance_settings'))

    school = get_current_school()
    shift  = _emp_shift_or_none(shift_id, school)
    if shift is None:
        flash('لا يمكن تعديل هذا الشفت.', 'danger')
        return redirect(url_for('admin.attendance_settings'))

    # In shift mode, deactivation may not strand active employees without a
    # valid assignment.  A single count query is sufficient.
    if (shift.is_active
            and getattr(school, 'emp_enable_attendance_shifts', False)):
        active_users = (Employee.query
                        .execution_options(bypass_tenant_scope=True)
                        .filter(Employee.school_id == shift.school_id,
                                Employee.shift_id == shift.id,
                                Employee.status == 'active')
                        .count())
        if active_users:
            flash('لا يمكن تعطيل هذا الشفت لأنه مرتبط بموظفين نشطين. '
                  'يرجى نقل الموظفين إلى شفت آخر أولاً.', 'danger')
            return redirect(url_for('admin.attendance_settings'))

    # Re-ACTIVATION only: a conflicting inactive shift may stay stored but must
    # not become active. Deactivating is always allowed.
    if not shift.is_active:
        _absence_boundary = (shift.late_after_time
                             if shift.late_after_time is not None
                             else shift.start_time)
        if (shift.absent_after_time is None
                or (_absence_boundary is not None
                    and shift.absent_after_time <= _absence_boundary)):
            flash('لا يمكن تفعيل الشفت قبل تحديد وقت غياب تلقائي صالح له.', 'danger')
            return redirect(url_for('admin.attendance_settings'))

    # An inactive shift is ignored by get_employee_shift. In employee shift mode
    # its assigned employee then fails closed (there is no general-time fallback).
    # The assignment itself remains preserved.
    shift.is_active = not shift.is_active
    db.session.commit()
    state = 'مفعَّل' if shift.is_active else 'معطَّل'
    flash(f'شفت الموظفين "{shift.name}" الآن {state}.', 'info')
    return redirect(url_for('admin.attendance_settings'))


@shifts_bp.route('/employee/<int:shift_id>/delete', methods=['POST'])
@login_required
def delete_employee_shift(shift_id):
    if not _require_admin():
        flash('ليس لديك صلاحية.', 'danger')
        return redirect(url_for('admin.attendance_settings'))

    school = get_current_school()
    shift  = _emp_shift_or_none(shift_id, school)
    if shift is None:
        flash('لا يمكن حذف هذا الشفت.', 'danger')
        return redirect(url_for('admin.attendance_settings'))

    # Block delete while employees are still assigned — mirrors the student
    # route, which refuses to delete a shift that still has sections.
    in_use = (Employee.query
              .execution_options(bypass_tenant_scope=True)
              .filter(Employee.school_id == shift.school_id,
                      Employee.shift_id == shift.id)
              .count())
    if in_use:
        flash(
            f'لا يمكن حذف شفت الموظفين "{shift.name}" لأنه مرتبط بـ {in_use} موظف. '
            f'قم بإلغاء تعيين الموظفين أولاً، أو عطِّل الشفت بدلاً من حذفه.',
            'warning',
        )
        return redirect(url_for('admin.attendance_settings'))

    name = shift.name
    db.session.delete(shift)
    db.session.commit()
    flash(f'تم حذف شفت الموظفين "{name}".', 'success')
    return redirect(url_for('admin.attendance_settings'))


@shifts_bp.route('/employee/global-absence', methods=['POST'])
@login_required
def update_employee_global_absence():
    """Compatibility endpoint; employee shift cutoffs are now per shift."""
    if not _require_admin():
        flash('ليس لديك صلاحية.', 'danger')
        return redirect(url_for('admin.attendance_settings'))
    flash('أصبح وقت الغياب التلقائي يُحدد لكل شفت موظفين بشكل مستقل.', 'warning')
    return redirect(url_for('admin.attendance_settings'))

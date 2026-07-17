"""
Mecha-School — School Calendar Blueprint
=========================================
Admin UI for managing:
  • Weekly day-off settings per school (stored on School.weekly_off_days)
  • Named holiday / break date ranges (SchoolHoliday model)

Routes
------
GET  /school-calendar/               — list holidays + weekly settings form
POST /school-calendar/weekly         — save weekly off-days setting
POST /school-calendar/add            — create a holiday
GET  /school-calendar/<id>/edit      — edit form (rendered inline via modal)
POST /school-calendar/<id>/edit      — update a holiday
POST /school-calendar/<id>/toggle    — activate / deactivate
POST /school-calendar/<id>/delete    — delete
"""
from datetime import date as date_type, datetime

from flask import (Blueprint, flash, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required

from app.models import (db, School, SchoolHoliday, AcademicYear)
from app.utils.decorators import (admin_required, permission_required,
                                   get_current_school, get_active_year)

school_calendar_bp = Blueprint(
    'school_calendar', __name__,
    template_folder='../../templates/school_calendar',
)

# Weekday labels (Python weekday(): 0=Mon … 6=Sun)
WEEKDAY_LABELS = {
    0: 'الإثنين',
    1: 'الثلاثاء',
    2: 'الأربعاء',
    3: 'الخميس',
    4: 'الجمعة',
    5: 'السبت',
    6: 'الأحد',
}

HOLIDAY_TYPE_LABELS = {
    'official':  'رسمية',
    'summer':    'صيفية',
    'emergency': 'طارئة',
    'custom':    'مخصصة',
}


def _parse_date(field_name):
    raw = (request.form.get(field_name) or '').strip()
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        return None


def _school_or_abort():
    school = get_current_school()
    if not school:
        flash('يرجى اختيار مدرسة نشطة أولاً.', 'danger')
        return None
    return school


def _holidays_for_school(school_id):
    """
    Return holidays for this school + global holidays, newest first.
    bypass_tenant_scope because SchoolHoliday is not __school_scoped__.
    """
    return (
        SchoolHoliday.query
        .execution_options(bypass_tenant_scope=True)
        .filter(
            db.or_(
                SchoolHoliday.school_id == school_id,
                SchoolHoliday.school_id.is_(None),
            )
        )
        .order_by(SchoolHoliday.start_date.desc())
        .all()
    )


def _parse_weekly_off_days():
    """Read checkboxes 'day_0' … 'day_6' from POST form, return "4,5" style string or None."""
    selected = [str(d) for d in range(7) if request.form.get(f'day_{d}')]
    return ','.join(selected) if selected else None


# ─────────────────────────────────────────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────────────────────────────────────────

@school_calendar_bp.route('/')
@login_required
@permission_required('manage_calendar')
def index():
    school = _school_or_abort()
    if not school:
        return redirect(url_for('admin.dashboard'))

    year = get_active_year(school.id)
    all_years = (
        AcademicYear.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(school_id=school.id)
        .order_by(AcademicYear.start_date.desc())
        .all()
    )

    holidays = _holidays_for_school(school.id)

    # Parse stored weekly_off_days into a set for easy template use
    off_set = set()
    if school.weekly_off_days:
        try:
            off_set = {int(d.strip()) for d in school.weekly_off_days.split(',') if d.strip().isdigit()}
        except ValueError:
            pass

    return render_template(
        'school_calendar/index.html',
        school=school,
        year=year,
        all_years=all_years,
        holidays=holidays,
        off_set=off_set,
        weekday_labels=WEEKDAY_LABELS,
        holiday_type_labels=HOLIDAY_TYPE_LABELS,
        today=date_type.today(),
    )


@school_calendar_bp.route('/weekly', methods=['POST'])
@login_required
@permission_required('manage_calendar')
def save_weekly():
    school = _school_or_abort()
    if not school:
        return redirect(url_for('admin.dashboard'))

    school_row = (
        School.query
        .execution_options(bypass_tenant_scope=True)
        .get(school.id)
    )
    school_row.weekly_off_days = _parse_weekly_off_days()
    db.session.commit()
    flash('تم حفظ أيام العطلة الأسبوعية.', 'success')
    return redirect(url_for('school_calendar.index'))


@school_calendar_bp.route('/add', methods=['POST'])
@login_required
@permission_required('manage_calendar')
def add():
    school = _school_or_abort()
    if not school:
        return redirect(url_for('admin.dashboard'))

    name         = (request.form.get('name') or '').strip()
    start_date   = _parse_date('start_date')
    end_date     = _parse_date('end_date')
    holiday_type = (request.form.get('holiday_type') or 'official').strip()
    notes        = (request.form.get('notes') or '').strip() or None
    is_global    = bool(request.form.get('is_global'))
    year_id_raw  = request.form.get('academic_year_id') or None

    if not name:
        flash('اسم العطلة مطلوب.', 'danger')
        return redirect(url_for('school_calendar.index'))
    if not start_date or not end_date:
        flash('تاريخ البداية والنهاية مطلوبان.', 'danger')
        return redirect(url_for('school_calendar.index'))
    if end_date < start_date:
        flash('تاريخ النهاية يجب أن يكون بعد أو يساوي تاريخ البداية.', 'danger')
        return redirect(url_for('school_calendar.index'))
    if holiday_type not in SchoolHoliday.HOLIDAY_TYPES:
        holiday_type = 'official'

    year_id = None
    if year_id_raw:
        try:
            year_id = int(year_id_raw)
        except ValueError:
            year_id = None

    holiday = SchoolHoliday(
        school_id        = None if is_global else school.id,
        academic_year_id = year_id,
        name             = name,
        start_date       = start_date,
        end_date         = end_date,
        holiday_type     = holiday_type,
        notes            = notes,
        is_active        = True,
        created_by       = current_user.id,
    )
    db.session.add(holiday)
    db.session.commit()
    flash(f'تمت إضافة العطلة "{name}".', 'success')
    return redirect(url_for('school_calendar.index'))


@school_calendar_bp.route('/<int:holiday_id>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('manage_calendar')
def edit(holiday_id):
    school = _school_or_abort()
    if not school:
        return redirect(url_for('admin.dashboard'))

    holiday = (
        SchoolHoliday.query
        .execution_options(bypass_tenant_scope=True)
        .get_or_404(holiday_id)
    )

    # Only allow editing holidays that belong to this school or are global
    if holiday.school_id is not None and holiday.school_id != school.id:
        flash('لا يمكنك تعديل هذه العطلة.', 'danger')
        return redirect(url_for('school_calendar.index'))

    if request.method == 'POST':
        name         = (request.form.get('name') or '').strip()
        start_date   = _parse_date('start_date')
        end_date     = _parse_date('end_date')
        holiday_type = (request.form.get('holiday_type') or 'official').strip()
        notes        = (request.form.get('notes') or '').strip() or None
        is_global    = bool(request.form.get('is_global'))
        year_id_raw  = request.form.get('academic_year_id') or None

        if not name or not start_date or not end_date:
            flash('اسم العطلة وتاريخا البداية والنهاية مطلوبة.', 'danger')
            return redirect(url_for('school_calendar.index'))
        if end_date < start_date:
            flash('تاريخ النهاية يجب أن يكون بعد أو يساوي تاريخ البداية.', 'danger')
            return redirect(url_for('school_calendar.index'))
        if holiday_type not in SchoolHoliday.HOLIDAY_TYPES:
            holiday_type = 'official'

        year_id = None
        if year_id_raw:
            try:
                year_id = int(year_id_raw)
            except ValueError:
                year_id = None

        holiday.name             = name
        holiday.start_date       = start_date
        holiday.end_date         = end_date
        holiday.holiday_type     = holiday_type
        holiday.notes            = notes
        holiday.school_id        = None if is_global else school.id
        holiday.academic_year_id = year_id
        db.session.commit()
        flash(f'تم تعديل العطلة "{name}".', 'success')
        return redirect(url_for('school_calendar.index'))

    all_years = (
        AcademicYear.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(school_id=school.id)
        .order_by(AcademicYear.start_date.desc())
        .all()
    )
    return render_template(
        'school_calendar/edit_modal.html',
        holiday=holiday,
        school=school,
        all_years=all_years,
        holiday_type_labels=HOLIDAY_TYPE_LABELS,
    )


@school_calendar_bp.route('/<int:holiday_id>/toggle', methods=['POST'])
@login_required
@permission_required('manage_calendar')
def toggle(holiday_id):
    school = _school_or_abort()
    if not school:
        return redirect(url_for('admin.dashboard'))

    holiday = (
        SchoolHoliday.query
        .execution_options(bypass_tenant_scope=True)
        .get_or_404(holiday_id)
    )
    if holiday.school_id is not None and holiday.school_id != school.id:
        flash('لا يمكنك تعديل هذه العطلة.', 'danger')
        return redirect(url_for('school_calendar.index'))

    holiday.is_active = not holiday.is_active
    db.session.commit()
    state = 'مفعّلة' if holiday.is_active else 'معطّلة'
    flash(f'العطلة "{holiday.name}" أصبحت {state}.', 'success')
    return redirect(url_for('school_calendar.index'))


@school_calendar_bp.route('/<int:holiday_id>/delete', methods=['POST'])
@login_required
@permission_required('manage_calendar')
def delete(holiday_id):
    school = _school_or_abort()
    if not school:
        return redirect(url_for('admin.dashboard'))

    holiday = (
        SchoolHoliday.query
        .execution_options(bypass_tenant_scope=True)
        .get_or_404(holiday_id)
    )
    if holiday.school_id is not None and holiday.school_id != school.id:
        flash('لا يمكنك حذف هذه العطلة.', 'danger')
        return redirect(url_for('school_calendar.index'))

    name = holiday.name
    db.session.delete(holiday)
    db.session.commit()
    flash(f'تم حذف العطلة "{name}".', 'success')
    return redirect(url_for('school_calendar.index'))

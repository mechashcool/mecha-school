"""
Al-Muhandis – Schedules Blueprint
Handles: Weekly class timetables per section, exam schedules
"""
from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, jsonify, send_file, abort)
from flask_login import login_required
from app.models import db, Schedule, Section, Subject, Employee, Grade, AcademicYear
from app.utils.decorators import admin_required, get_current_school, historical_guard

schedules_bp = Blueprint('schedules', __name__,
                           template_folder='../../templates/schedules')

DAYS = ['الأحد', 'الاثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت']


@schedules_bp.route('/')
@login_required
@admin_required
def index():
    sections = Section.query.join(Grade).order_by(Grade.name, Section.name).all()
    sec_id   = request.args.get('section_id', type=int)
    if not sec_id and sections:
        sec_id = sections[0].id

    schedule_grid = {}
    if sec_id:
        entries = Schedule.query.filter_by(section_id=sec_id).all()
        for e in entries:
            schedule_grid.setdefault(e.day_of_week, []).append(e)
        for day in schedule_grid:
            schedule_grid[day].sort(key=lambda x: x.start_time)

    subjects  = Subject.query.order_by(Subject.name).all()
    teachers  = Employee.query.filter_by(status='active').order_by(Employee.full_name).all()

    # Display days: Sun-Thu only
    display_days = DAYS[:5]  # Sun-Thu
    display_day_indices = list(range(5))  # 0-4 for Sun-Thu

    return render_template('schedules/index.html',
                           sections=sections, sec_id=sec_id,
                           schedule_grid=schedule_grid,
                           subjects=subjects, teachers=teachers,
                           days=display_days, day_indices=display_day_indices)


@schedules_bp.route('/create', methods=['POST'])
@login_required
@historical_guard
@admin_required
def create():
    from datetime import time as t
    sec_id     = request.form.get('section_id', type=int)
    subject_id = request.form.get('subject_id', type=int)
    teacher_id = request.form.get('teacher_id', type=int) or None
    day        = request.form.get('day_of_week', type=int)
    start_str  = request.form.get('start_time')
    end_str    = request.form.get('end_time')
    room       = request.form.get('room', '').strip()

    if not all([sec_id, subject_id, day is not None, start_str, end_str]):
        flash('يرجى ملء كافة الحقول المطلوبة.', 'danger')
        return redirect(url_for('schedules.index', section_id=sec_id))

    sh, sm = map(int, start_str.split(':'))
    eh, em = map(int, end_str.split(':'))
    start_time = t(sh, sm)
    end_time = t(eh, em)
    section = Section.query.get_or_404(sec_id)

    # Check for duplicate schedule time (same section, day, and overlapping time)
    existing = Schedule.query.filter_by(
        section_id=sec_id,
        day_of_week=day
    ).filter(
        ((Schedule.start_time <= start_time) & (Schedule.end_time > start_time)) |
        ((Schedule.start_time < end_time) & (Schedule.end_time >= end_time)) |
        ((Schedule.start_time >= start_time) & (Schedule.end_time <= end_time))
    ).first()
    
    if existing:
        flash('عذراً، هذا الوقت محجوز مسبقاً في جدول هذه الشعبة.', 'danger')
        return redirect(url_for('schedules.index', section_id=sec_id))

    entry = Schedule(
        school_id   = section.school_id,
        academic_year_id = section.academic_year_id,
        section_id  = sec_id,
        subject_id  = subject_id,
        teacher_id  = teacher_id,
        day_of_week = day,
        start_time  = start_time,
        end_time    = end_time,
        room        = room or None,
    )
    try:
        db.session.add(entry)
        db.session.commit()
        flash('تم إضافة الحصة.', 'success')
        return redirect(url_for('schedules.index', section_id=sec_id))
    except Exception as e:
        db.session.rollback()
        flash('حدث خطأ غير متوقع، يرجى المحاولة مرة أخرى.', 'danger')
        return redirect(url_for('schedules.index', section_id=sec_id))


@schedules_bp.route('/<int:entry_id>/delete', methods=['POST'])
@login_required
@historical_guard
@admin_required
def delete(entry_id):
    entry = Schedule.query.get_or_404(entry_id)
    sec_id = entry.section_id
    db.session.delete(entry)
    db.session.commit()
    flash('تم حذف الحصة.', 'success')
    return redirect(url_for('schedules.index', section_id=sec_id))


@schedules_bp.route('/<int:section_id>/print')
@login_required
@admin_required
def print_pdf(section_id):
    """
    Phase 4 — download a printable PDF of the weekly schedule for a section.
    The PDF uses the white-label header from SchoolSettings so every tenant's
    print-out matches their branding.
    """
    from io import BytesIO
    from app.models import SchoolSettings
    from app.utils.pdf_gen import generate_schedule_pdf

    section = Section.query.get_or_404(section_id)
    entries = Schedule.query.filter_by(section_id=section_id)\
                            .order_by(Schedule.day_of_week, Schedule.start_time).all()

    school = get_current_school() or SchoolSettings.get()

    # Days: Sun-Thu only
    pdf_days = DAYS[:5]  # Sun-Thu

    pdf_bytes = generate_schedule_pdf(section, entries, pdf_days, school=school)
    if pdf_bytes is None:
        flash('مكتبة ReportLab غير مثبّتة — لا يمكن توليد PDF.', 'danger')
        return redirect(url_for('schedules.index', section_id=section_id))

    filename = f"schedule_{section.grade.name}_{section.name}.pdf".replace(' ', '_')
    return send_file(BytesIO(pdf_bytes),
                     mimetype='application/pdf',
                     as_attachment=False,
                     download_name=filename)

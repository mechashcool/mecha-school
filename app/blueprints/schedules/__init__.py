"""
Al-Muhandis – Schedules Blueprint
Handles: Weekly class timetables per section OR per grade, exam schedules

A schedule entry targets EITHER a section (section-based — the original
behaviour) OR a grade (grade-based — for schools that do not use sections).
The same create / view / delete / print logic serves both modes; the only
difference is which column (section_id vs grade_id) the entry is keyed on.
"""
from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, jsonify, send_file, abort)
from flask_login import login_required
from app.models import db, Schedule, Section, Subject, Employee, Grade, AcademicYear
from app.utils.decorators import admin_required, get_current_school, historical_guard

schedules_bp = Blueprint('schedules', __name__,
                           template_folder='../../templates/schedules')

DAYS = ['الأحد', 'الاثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت']

# Educational stages, used for the Stage → Grade → Section → Subject guided flow.
STAGES = ['ابتدائية', 'متوسطة', 'إعدادية']


def _build_grid(entries):
    """Group a flat list of Schedule rows into {day_of_week: [sorted entries]}."""
    grid = {}
    for e in entries:
        grid.setdefault(e.day_of_week, []).append(e)
    for day in grid:
        grid[day].sort(key=lambda x: x.start_time)
    return grid


def _subjects_for_grade(grade):
    """
    Return the subjects that belong to a grade, for the schedule subject dropdown.

    Includes:
      * subjects explicitly assigned to this grade (subject.grade_id == grade.id)
      * stage-level subjects of the grade's stage that are not bound to any grade
        (grade_id IS NULL and stage == grade.stage)
      * general subjects bound to neither a grade nor a stage (both NULL)

    Excludes subjects bound to a DIFFERENT grade or a different stage — this is
    what stops subjects from other grades/stages leaking into the timetable form.

    Returns [] when no grade is given (no grade selected → empty dropdown).
    """
    if not grade:
        return []
    stage_clause = [Subject.stage.is_(None)]
    if grade.stage:
        stage_clause.append(Subject.stage == grade.stage)
    return (Subject.query
            .filter(db.or_(
                Subject.grade_id == grade.id,
                db.and_(Subject.grade_id.is_(None), db.or_(*stage_clause)),
            ))
            .order_by(Subject.name)
            .all())


@schedules_bp.route('/')
@login_required
@admin_required
def index():
    sections = Section.query.join(Grade).order_by(Grade.name, Section.name).all()
    grades   = Grade.query.order_by(Grade.name).all()

    sec_id   = request.args.get('section_id', type=int)
    grade_id = request.args.get('grade_id', type=int)

    # ── Resolve which target we are managing ─────────────────────────────────
    # Flow: Grade (required) → optional Section.
    #   * section_id present → manage that section's schedule (section-based)
    #   * grade_id only      → manage the whole-grade schedule (grade-based)
    #   * nothing            → default to the first grade (whole grade)
    # Section takes precedence so an explicit section selection always wins.
    target_mode = None   # 'section' | 'grade' | None
    if sec_id:
        target_mode = 'section'
        grade_id = None
    elif grade_id:
        target_mode = 'grade'
    elif grades:
        grade_id = grades[0].id
        target_mode = 'grade'

    schedule_grid = {}
    if target_mode == 'section' and sec_id:
        schedule_grid = _build_grid(Schedule.query.filter_by(section_id=sec_id).all())
    elif target_mode == 'grade' and grade_id:
        # Grade-based entries only (section_id IS NULL); section entries never
        # carry grade_id, so this never mixes the two.
        schedule_grid = _build_grid(
            Schedule.query.filter_by(grade_id=grade_id, section_id=None).all()
        )

    # ── Resolve the effective grade and filter subjects to it ────────────────
    # Subject dropdown must only show subjects of the grade being managed:
    #   grade mode   → the selected grade
    #   section mode → the selected section's grade
    #   no target    → no subjects (empty dropdown)
    effective_grade = None
    if target_mode == 'grade' and grade_id:
        effective_grade = next((g for g in grades if g.id == grade_id), None)
    elif target_mode == 'section' and sec_id:
        _sec = next((s for s in sections if s.id == sec_id), None)
        effective_grade = _sec.grade if _sec else None

    subjects  = _subjects_for_grade(effective_grade)
    teachers  = Employee.query.filter_by(status='active').order_by(Employee.full_name).all()

    # Sections of the effective grade — used to populate the OPTIONAL section
    # dropdown (admin may leave it on "بدون شعبة" to manage the whole grade).
    effective_grade_sections = [
        s for s in sections
        if effective_grade and s.grade_id == effective_grade.id
    ]

    # Display days: Sun-Thu only
    display_days = DAYS[:5]  # Sun-Thu
    display_day_indices = list(range(5))  # 0-4 for Sun-Thu

    return render_template('schedules/index.html',
                           sections=sections, grades=grades,
                           sec_id=sec_id, grade_id=grade_id,
                           target_mode=target_mode,
                           effective_grade=effective_grade,
                           effective_grade_id=(effective_grade.id if effective_grade else None),
                           effective_stage=(effective_grade.stage if effective_grade else None),
                           effective_grade_sections=effective_grade_sections,
                           stages=STAGES,
                           schedule_grid=schedule_grid,
                           subjects=subjects, teachers=teachers,
                           days=display_days, day_indices=display_day_indices)


def _redirect_to_target(sec_id, grade_id):
    if sec_id:
        return redirect(url_for('schedules.index', section_id=sec_id))
    if grade_id:
        return redirect(url_for('schedules.index', grade_id=grade_id))
    return redirect(url_for('schedules.index'))


@schedules_bp.route('/create', methods=['POST'])
@login_required
@historical_guard
@admin_required
def create():
    from datetime import time as t
    sec_id     = request.form.get('section_id', type=int)
    grade_id   = request.form.get('grade_id', type=int)
    subject_id = request.form.get('subject_id', type=int)
    teacher_id = request.form.get('teacher_id', type=int) or None
    day        = request.form.get('day_of_week', type=int)
    start_str  = request.form.get('start_time')
    end_str    = request.form.get('end_time')
    room       = request.form.get('room', '').strip()

    # Section takes precedence; exactly one target is used.
    if sec_id:
        grade_id = None
    target_ok = bool(sec_id) or bool(grade_id)

    if not all([target_ok, subject_id, day is not None, start_str, end_str]):
        flash('يرجى ملء كافة الحقول المطلوبة.', 'danger')
        return _redirect_to_target(sec_id, grade_id)

    sh, sm = map(int, start_str.split(':'))
    eh, em = map(int, end_str.split(':'))
    start_time = t(sh, sm)
    end_time = t(eh, em)

    # Resolve the parent (section or grade) for school/year scoping.
    if sec_id:
        parent = Section.query.get_or_404(sec_id)
        overlap_filter = Schedule.query.filter_by(section_id=sec_id, day_of_week=day)
        target_label = 'هذه الشعبة'
        target_grade_id = parent.grade_id
    else:
        parent = Grade.query.get_or_404(grade_id)
        overlap_filter = Schedule.query.filter_by(grade_id=grade_id, section_id=None,
                                                  day_of_week=day)
        target_label = 'هذا الصف'
        target_grade_id = parent.id

    # Guard: the chosen subject must not belong to a DIFFERENT grade. Subjects
    # with no grade (stage-level or general) are allowed. This stops a subject
    # from another grade being saved if it ever reaches the form.
    subject = Subject.query.filter_by(id=subject_id).first()
    if subject is None:
        flash('المادة المختارة غير صالحة.', 'danger')
        return _redirect_to_target(sec_id, grade_id)
    if subject.grade_id and target_grade_id and subject.grade_id != target_grade_id:
        flash('المادة المختارة لا تنتمي إلى هذا الصف. يرجى اختيار مادة من مواد الصف.',
              'danger')
        return _redirect_to_target(sec_id, grade_id)

    # Check for an overlapping time slot in the same section/grade and day.
    existing = overlap_filter.filter(
        ((Schedule.start_time <= start_time) & (Schedule.end_time > start_time)) |
        ((Schedule.start_time < end_time) & (Schedule.end_time >= end_time)) |
        ((Schedule.start_time >= start_time) & (Schedule.end_time <= end_time))
    ).first()

    if existing:
        flash(f'عذراً، هذا الوقت محجوز مسبقاً في جدول {target_label}.', 'danger')
        return _redirect_to_target(sec_id, grade_id)

    entry = Schedule(
        school_id        = parent.school_id,
        academic_year_id = parent.academic_year_id,
        section_id       = sec_id or None,
        grade_id         = grade_id or None,
        subject_id       = subject_id,
        teacher_id       = teacher_id,
        day_of_week      = day,
        start_time       = start_time,
        end_time         = end_time,
        room             = room or None,
    )
    try:
        db.session.add(entry)
        db.session.commit()
        flash('تم إضافة الحصة.', 'success')
    except Exception:
        db.session.rollback()
        flash('حدث خطأ غير متوقع، يرجى المحاولة مرة أخرى.', 'danger')
    return _redirect_to_target(sec_id, grade_id)


@schedules_bp.route('/<int:entry_id>/delete', methods=['POST'])
@login_required
@historical_guard
@admin_required
def delete(entry_id):
    entry = Schedule.query.get_or_404(entry_id)
    sec_id   = entry.section_id
    grade_id = entry.grade_id
    db.session.delete(entry)
    db.session.commit()
    flash('تم حذف الحصة.', 'success')
    return _redirect_to_target(sec_id, grade_id)


def _render_schedule_pdf(target, entries, filename):
    """Shared PDF builder for both section and grade schedules."""
    from io import BytesIO
    from app.models import SchoolSettings
    from app.utils.pdf_gen import generate_schedule_pdf

    school = get_current_school() or SchoolSettings.get()
    pdf_days = DAYS[:5]  # Sun-Thu

    pdf_bytes = generate_schedule_pdf(target, entries, pdf_days, school=school)
    if pdf_bytes is None:
        flash('مكتبة ReportLab غير مثبّتة — لا يمكن توليد PDF.', 'danger')
        return None
    return send_file(BytesIO(pdf_bytes),
                     mimetype='application/pdf',
                     as_attachment=False,
                     download_name=filename)


@schedules_bp.route('/<int:section_id>/print')
@login_required
@admin_required
def print_pdf(section_id):
    """Download a printable PDF of the weekly schedule for a section."""
    section = Section.query.get_or_404(section_id)
    entries = Schedule.query.filter_by(section_id=section_id)\
                            .order_by(Schedule.day_of_week, Schedule.start_time).all()

    filename = f"schedule_{section.grade.name}_{section.name}.pdf".replace(' ', '_')
    result = _render_schedule_pdf(section, entries, filename)
    return result or redirect(url_for('schedules.index', section_id=section_id))


@schedules_bp.route('/grade/<int:grade_id>/print')
@login_required
@admin_required
def print_grade_pdf(grade_id):
    """Download a printable PDF of the weekly schedule for a grade."""
    grade = Grade.query.get_or_404(grade_id)
    entries = Schedule.query.filter_by(grade_id=grade_id, section_id=None)\
                            .order_by(Schedule.day_of_week, Schedule.start_time).all()

    filename = f"schedule_{grade.name}.pdf".replace(' ', '_')
    result = _render_schedule_pdf(grade, entries, filename)
    return result or redirect(url_for('schedules.index', grade_id=grade_id))

"""
Mecha-School — Teacher Portal
==============================
Dashboard for staff with role.name == 'teacher' (or admins who browse it).
Surfaces the teacher's assigned sections, weekly schedule, upcoming exams,
and a live exam-conflict checker.
"""
from datetime import date, timedelta
from functools import wraps

from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, jsonify, abort)
from flask_login import login_required, current_user
from sqlalchemy import func

from app.models import (db, Employee, Section, Schedule, Exam, ExamResult,
                         Subject, ExamType, AcademicYear, teacher_subjects)

teacher_bp = Blueprint('teacher', __name__,
                        template_folder='../../templates/teacher')

DAYS = ['الأحد', 'الاثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت']


def teacher_required(f):
    """Allow teachers and admins; everyone else → admin dashboard."""
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.role:
            abort(403)
        if not (current_user.is_admin_user or current_user.role.name == 'teacher'):
            flash('هذه الصفحة مخصصة للمعلمين فقط.', 'warning')
            return redirect(url_for('admin.dashboard'))
        return f(*args, **kwargs)
    return wrapper


def _get_employee():
    """Return the Employee record linked to the current user, or None."""
    return Employee.query.filter_by(user_id=current_user.id).first()


# ─────────────────────────────────────────────────────────────────────────────
#  Dashboard
# ─────────────────────────────────────────────────────────────────────────────

@teacher_bp.route('/')
@teacher_bp.route('/dashboard')
@teacher_required
def dashboard():
    employee = _get_employee()

    sections       = []
    schedule       = []
    upcoming_exams = []
    conflicts      = []

    if employee:
        # Homeroom sections (Section.teacher_id = emp.id)
        sections = list(employee.sections_managed)

        # Subject-teaching sections (from teacher_subjects junction)
        subj_sec_ids = {
            row[0] for row in
            db.session.query(teacher_subjects.c.section_id)
                      .filter(teacher_subjects.c.employee_id == employee.id)
                      .all()
        }
        # Combine homeroom + subject-teaching section IDs
        homeroom_ids = {s.id for s in sections}
        all_assigned_section_ids = list(homeroom_ids | subj_sec_ids)

        # Subjects assigned to this teacher (scoped to assigned sections)
        assigned_subject_ids = {
            row[0] for row in
            db.session.query(teacher_subjects.c.subject_id)
                      .filter(teacher_subjects.c.employee_id == employee.id)
                      .distinct()
                      .all()
        }

        schedule = (Schedule.query
                    .filter_by(teacher_id=employee.id)
                    .order_by(Schedule.day_of_week, Schedule.start_time)
                    .all())

        section_ids = all_assigned_section_ids
        today = date.today()
        if section_ids:
            upcoming_exams = (Exam.query
                              .filter(Exam.section_id.in_(section_ids),
                                      Exam.exam_date >= today,
                                      Exam.exam_date <= today + timedelta(days=30))
                              .order_by(Exam.exam_date.asc())
                              .all())

            # Detect sections where multiple exams share the same date
            conflict_rows = (db.session.query(
                                 Exam.section_id,
                                 Exam.exam_date,
                                 func.count(Exam.id).label('cnt')
                             )
                             .filter(Exam.section_id.in_(section_ids))
                             .group_by(Exam.section_id, Exam.exam_date)
                             .having(func.count(Exam.id) > 1)
                             .all())

            # Enrich conflicts with exam details
            for row in conflict_rows:
                exams_on_day = Exam.query.filter_by(
                    section_id=row.section_id,
                    exam_date=row.exam_date
                ).all()
                conflicts.append({
                    'section': Section.query.get(row.section_id),
                    'date':    row.exam_date,
                    'exams':   exams_on_day,
                })

        # Scope subjects and sections for template dropdowns to teacher's assignments
        subjects = (Subject.query
                    .filter(Subject.id.in_(assigned_subject_ids))
                    .order_by(Subject.name).all()
                    if assigned_subject_ids else [])
        all_sections = (Section.query
                        .filter(Section.id.in_(all_assigned_section_ids))
                        .all()
                        if all_assigned_section_ids else [])
    else:
        subjects     = []
        all_sections = []

    # Build schedule grid: day → sorted entries
    schedule_grid = {}
    for e in schedule:
        schedule_grid.setdefault(e.day_of_week, []).append(e)

    exam_types = ExamType.query.all()

    return render_template('teacher/dashboard.html',
                           employee=employee,
                           sections=sections,
                           schedule_grid=schedule_grid,
                           upcoming_exams=upcoming_exams,
                           conflicts=conflicts,
                           subjects=subjects,
                           exam_types=exam_types,
                           all_sections=all_sections,
                           days=DAYS)


# ─────────────────────────────────────────────────────────────────────────────
#  AJAX — exam conflict checker
# ─────────────────────────────────────────────────────────────────────────────

@teacher_bp.route('/exam-conflict-check')
@teacher_required
def exam_conflict_check():
    """
    Returns JSON: whether a given (section_id, exam_date) has existing exams.
    Called by JS on the exam creation form to give real-time conflict warnings.

    Query params:
        section_id  int   required
        exam_date   str   YYYY-MM-DD  required
        exam_id     int   optional — exclude this exam when editing
    """
    from datetime import datetime as dt

    section_id = request.args.get('section_id', type=int)
    exam_date  = request.args.get('exam_date', '')
    exam_id    = request.args.get('exam_id', type=int)

    if not section_id or not exam_date:
        return jsonify({'conflict': False})

    try:
        check_date = dt.strptime(exam_date, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'conflict': False})

    q = Exam.query.filter_by(section_id=section_id, exam_date=check_date)
    if exam_id:
        q = q.filter(Exam.id != exam_id)

    existing = q.all()
    if existing:
        return jsonify({
            'conflict': True,
            'exams': [
                {
                    'name':    e.display_name,
                    'subject': e.subject.name if e.subject else '—',
                }
                for e in existing
            ],
        })
    return jsonify({'conflict': False})

"""
Mobile API — Teacher endpoints
================================
All routes require:  Authorization: Bearer <access_token>   (role: teacher)

Endpoint map
────────────
GET  /teacher/profile                  teacher record + quick dashboard stats + subjects
GET  /teacher/subjects                 distinct subjects assigned to this teacher (primary Flutter source)
GET  /teacher/sections                 sections I teach (homeroom + subject) with subjects per section
GET  /teacher/sections/<id>/students   students in one of my sections
GET  /teacher/students/<id>            student profile (only allowed sections)
GET  /teacher/schedule                 my weekly timetable (subject_id, section_id, day_label included)
GET  /teacher/exams                    exams for my sections (subject_id, section_id, title, max_score)
POST /teacher/exams                    create an exam — accepts title + max_score; validates subject assignment
GET  /teacher/exams/<id>               exam detail + entered results (subject_id, section_id, title)
POST /teacher/exams/<id>/results       bulk-upsert grade entries (accepts score/note or marks/notes)
GET  /teacher/notifications            notifications feed (paginated)
GET  /teacher/homework                 homework list (subject_id, section_id, grade_name)
POST /teacher/homework                 create homework — subject_id required

Security rules
──────────────
• Every endpoint calls _get_employee() to resolve the Employee linked to the
  authenticated user. If no Employee row exists the endpoint returns 404.
• _teacher_section_ids() returns the union of homeroom sections and sections
  assigned via the teacher_subjects junction table.
• Students and exams are validated to belong to those sections AND to the
  same school as the employee.
• Exam creation: the supplied section_id must be in the teacher's section set.
• Result entry: the exam's section_id must be in the teacher's section set.
"""
from datetime import date, timedelta, timezone
from datetime import datetime as _dt

from flask import abort, g, request
from sqlalchemy import select

from app.models import (
    db,
    Employee,
    Exam,
    ExamResult,
    Homework,
    Notification,
    NotificationRead,
    Schedule,
    Section,
    Student,
    StudentAttendance,
    Subject,
    teacher_subjects,
)
from app.utils.notification_visibility import notification_visible_to

from . import mobile_api_bp
from .utils import jwt_required, role_required, ok, err, photo_url


# ─── Shared helpers ───────────────────────────────────────────────────────────

def _get_employee() -> Employee | None:
    """Return the Employee row linked to the current mobile user, or None."""
    return Employee.query.filter_by(user_id=g.mobile_user.id).first()


def _teacher_section_ids(emp: Employee) -> set[int]:
    """
    Return the set of Section IDs this teacher can access:
      • homeroom sections  (Section.teacher_id == emp.id)
      • sections via subject assignment  (teacher_subjects junction)
    """
    homeroom = {s.id for s in emp.sections_managed}
    subject_secs = {
        row.section_id
        for row in db.session.execute(
            select(teacher_subjects.c.section_id).where(
                teacher_subjects.c.employee_id == emp.id
            )
        ).fetchall()
    }
    return homeroom | subject_secs


def _assert_section_access(emp: Employee, section_id: int) -> None:
    """Abort 403 if the teacher does not have access to section_id."""
    if section_id not in _teacher_section_ids(emp):
        abort(403)


def _assert_student_access(emp: Employee, student_id: int) -> Student:
    """Return Student if teacher can access it; abort 403/404 otherwise."""
    student = db.session.get(Student, student_id)
    if not student or student.school_id != emp.school_id:
        abort(404)
    if student.section_id is None or student.section_id not in _teacher_section_ids(emp):
        abort(403)
    return student


def _assert_exam_access(emp: Employee, exam_id: int) -> Exam:
    """Return Exam if it belongs to one of the teacher's sections; abort otherwise."""
    exam = db.session.get(Exam, exam_id)
    if not exam or exam.school_id != emp.school_id:
        abort(404)
    if exam.section_id not in _teacher_section_ids(emp):
        abort(403)
    return exam


_DAY_NAMES = {
    0: 'الأحد', 1: 'الاثنين', 2: 'الثلاثاء',
    3: 'الأربعاء', 4: 'الخميس', 5: 'الجمعة', 6: 'السبت',
}
_DAY_NAMES_EN = {
    0: 'sunday', 1: 'monday', 2: 'tuesday',
    3: 'wednesday', 4: 'thursday', 5: 'friday', 6: 'saturday',
}


def _fmt_time(t) -> str | None:
    return t.strftime('%H:%M') if t else None


def _teacher_subjects(emp: Employee) -> list[dict]:
    """Return distinct subjects assigned to this teacher via teacher_subjects junction."""
    rows = db.session.execute(
        select(teacher_subjects.c.subject_id).where(
            teacher_subjects.c.employee_id == emp.id
        ).distinct()
    ).fetchall()
    subject_ids = {r.subject_id for r in rows}
    if not subject_ids:
        return []
    subjects = Subject.query.filter(Subject.id.in_(subject_ids)).order_by(Subject.name).all()
    return [{'id': s.id, 'name': s.name} for s in subjects]


def _section_subjects(emp_id: int, section_id: int) -> list[dict]:
    """Return subjects this teacher teaches in a specific section."""
    rows = db.session.execute(
        select(teacher_subjects.c.subject_id).where(
            teacher_subjects.c.employee_id == emp_id,
            teacher_subjects.c.section_id == section_id,
        ).distinct()
    ).fetchall()
    subject_ids = [r.subject_id for r in rows]
    if not subject_ids:
        return []
    subjects = Subject.query.filter(Subject.id.in_(subject_ids)).order_by(Subject.name).all()
    return [{'id': s.id, 'name': s.name} for s in subjects]


# ─── Profile / dashboard ──────────────────────────────────────────────────────

@mobile_api_bp.route('/teacher/profile', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_profile():
    """Teacher employee record + quick stats (sections, students, upcoming exams)."""
    emp = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)

    section_ids = _teacher_section_ids(emp)
    today       = date.today()

    sections_count = len(section_ids)
    student_count  = (Student.query
                      .filter(Student.section_id.in_(section_ids))
                      .filter_by(status='active')
                      .count()) if section_ids else 0
    upcoming_14d   = (Exam.query
                      .filter(Exam.section_id.in_(section_ids))
                      .filter(Exam.exam_date >= today)
                      .filter(Exam.exam_date <= today + timedelta(days=14))
                      .count()) if section_ids else 0

    return ok(
        employee={
            'id':          emp.id,
            'employee_id': emp.employee_id,
            'name':        emp.full_name,
            'job_title':   emp.job_title,
            'department':  emp.department,
            'phone':       emp.phone,
            'email':       emp.email,
            'photo':       emp.photo,
            'hire_date':   emp.hire_date.isoformat() if emp.hire_date else None,
        },
        stats={
            'sections_count':      sections_count,
            'student_count':       student_count,
            'upcoming_exams_14d':  upcoming_14d,
        },
        subjects=_teacher_subjects(emp),
    )


# ─── Sections I teach ────────────────────────────────────────────────────────

@mobile_api_bp.route('/teacher/sections', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_sections():
    """All sections the teacher is associated with (homeroom + subject teaching)."""
    emp = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)

    section_ids  = _teacher_section_ids(emp)
    homeroom_ids = {s.id for s in emp.sections_managed}
    sections     = (Section.query
                    .filter(Section.id.in_(section_ids))
                    .order_by(Section.id)
                    .all()) if section_ids else []

    return ok(
        sections=[
            {
                'id':            sec.id,
                'name':          sec.name,
                'grade_name':    sec.grade.name  if sec.grade else None,
                'grade':         sec.grade.name  if sec.grade else None,
                'stage':         sec.grade.stage if sec.grade else None,
                'display_name':  f"{sec.grade.name} - شعبة {sec.name}" if sec.grade else sec.name,
                'capacity':      sec.capacity,
                'student_count': Student.query.filter_by(section_id=sec.id, status='active').count(),
                'is_homeroom':   sec.id in homeroom_ids,
                'subjects':      _section_subjects(emp.id, sec.id),
            }
            for sec in sections
        ],
    )


# ─── Subjects I teach ────────────────────────────────────────────────────────

@mobile_api_bp.route('/teacher/subjects', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_subjects_list():
    """
    Distinct subjects assigned to this teacher across all sections.
    Primary endpoint Flutter should use to populate subject pickers (Create Exam, etc.).

    Response:
      { "ok": true, "subjects": [ {"id": 2, "name": "الرياضيات"} ] }
    """
    emp = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)
    return ok(subjects=_teacher_subjects(emp))


# ─── Students in a section ───────────────────────────────────────────────────

@mobile_api_bp.route('/teacher/sections/<int:section_id>/students', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_section_students(section_id):
    """
    Active students in one of the teacher's sections.
    Optional query param: q=<name fragment> for name search.
    """
    emp = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)
    _assert_section_access(emp, section_id)

    section = db.session.get(Section, section_id)
    q_name  = request.args.get('q', '').strip()

    query = Student.query.filter_by(section_id=section_id, status='active')
    if q_name:
        query = query.filter(Student.full_name.ilike(f'%{q_name}%'))
    students = query.order_by(Student.full_name).all()

    return ok(
        section={
            'id':    section.id,
            'name':  section.name,
            'grade': section.grade.name if section.grade else None,
        },
        count=len(students),
        students=[
            {
                'id':         s.id,
                'student_id': s.student_id,
                'name':       s.full_name,
                'gender':     s.gender,
                'photo':      photo_url(s.photo),
                'status':     s.status,
            }
            for s in students
        ],
    )


# ─── Student profile (teacher-scoped) ────────────────────────────────────────

@mobile_api_bp.route('/teacher/students/<int:student_id>', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_student_profile(student_id):
    """
    Profile for a student in one of the teacher's sections.
    Includes last-30-day attendance snapshot and recent exam results for
    exams that belong to the teacher's sections/subjects.
    """
    emp     = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)
    student = _assert_student_access(emp, student_id)

    today       = date.today()
    section_ids = _teacher_section_ids(emp)

    att_rows = (StudentAttendance.query
                .filter_by(student_id=student.id)
                .filter(StudentAttendance.date >= today - timedelta(days=30))
                .order_by(StudentAttendance.date.desc())
                .all())
    att_stats = {
        'present': sum(1 for r in att_rows if r.status == 'present'),
        'absent':  sum(1 for r in att_rows if r.status == 'absent'),
        'late':    sum(1 for r in att_rows if r.status == 'late'),
        'excused': sum(1 for r in att_rows if r.status == 'excused'),
    }

    # Only show results for exams in this teacher's sections
    results = (ExamResult.query
               .execution_options(include_all_years=True)
               .join(ExamResult.exam)
               .filter(ExamResult.student_id == student.id)
               .filter(Exam.section_id.in_(section_ids))
               .order_by(ExamResult.id.desc())
               .limit(10)
               .all())

    return ok(
        student={
            'id':              student.id,
            'student_id':      student.student_id,
            'name':            student.full_name,
            'gender':          student.gender,
            'photo':           photo_url(student.photo),
            'date_of_birth':   student.date_of_birth.isoformat() if student.date_of_birth else None,
            'phone':           student.phone,
            'section':         student.section.name       if student.section else None,
            'grade':           student.section.grade.name if student.section and student.section.grade else None,
            'guardian_name':   student.guardian_name,
            'guardian_phone':  student.guardian_phone,
            'status':          student.status,
        },
        attendance_last30=att_stats,
        recent_results=[
            {
                'exam':      r.exam.display_name if r.exam else None,
                'subject':   r.exam.subject.name if r.exam and r.exam.subject else None,
                'marks':     float(r.marks)        if r.marks is not None else None,
                'max_marks': float(r.exam.max_marks) if r.exam else None,
                'grade':     r.grade_letter,
                'is_pass':   r.is_pass,
                'date':      r.exam.exam_date.isoformat() if r.exam and r.exam.exam_date else None,
            }
            for r in results
        ],
    )


# ─── Teacher schedule ────────────────────────────────────────────────────────

@mobile_api_bp.route('/teacher/schedule', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_schedule():
    """The teacher's weekly timetable (periods where teacher_id == emp.id)."""
    emp = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)

    schedules = (Schedule.query
                 .filter_by(teacher_id=emp.id)
                 .order_by(Schedule.day_of_week, Schedule.start_time)
                 .all())

    return ok(
        schedule=[
            {
                'id':           sch.id,
                'day':          sch.day_of_week,
                'day_en':       _DAY_NAMES_EN.get(sch.day_of_week, ''),
                'day_label':    _DAY_NAMES.get(sch.day_of_week, ''),
                'day_name':     _DAY_NAMES.get(sch.day_of_week, ''),
                'subject_id':   sch.subject_id,
                'subject_name': sch.subject.name       if sch.subject else None,
                'subject':      sch.subject.name       if sch.subject else None,
                'subject_code': sch.subject.code       if sch.subject else None,
                'section_id':   sch.section_id,
                'section_name': sch.section.name       if sch.section else None,
                'section':      sch.section.name       if sch.section else None,
                'grade_name':   sch.section.grade.name if sch.section and sch.section.grade else None,
                'grade':        sch.section.grade.name if sch.section and sch.section.grade else None,
                'start_time':   _fmt_time(sch.start_time),
                'end_time':     _fmt_time(sch.end_time),
                'room':         sch.room,
            }
            for sch in schedules
        ],
    )


# ─── Exams list ───────────────────────────────────────────────────────────────

@mobile_api_bp.route('/teacher/exams', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_exams():
    """
    Exams for all of the teacher's sections.
    Query params:
      upcoming=1  → only future exams
      past=1      → only past exams
      limit       → default 50, max 100
      offset      → default 0
    """
    emp = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)

    section_ids = _teacher_section_ids(emp)
    if not section_ids:
        return ok(count=0, exams=[])

    today = date.today()
    q     = Exam.query.filter(Exam.section_id.in_(section_ids))

    if request.args.get('upcoming'):
        q = q.filter(Exam.exam_date >= today)
    elif request.args.get('past'):
        q = q.filter(Exam.exam_date < today)

    limit  = min(int(request.args.get('limit', 50)), 100)
    offset = max(int(request.args.get('offset', 0)), 0)
    exams  = q.order_by(Exam.exam_date.desc()).offset(offset).limit(limit).all()

    return ok(
        count=len(exams),
        exams=[
            {
                'id':            e.id,
                'title':         e.display_name,
                'name':          e.display_name,
                'subject_id':    e.subject_id,
                'subject_name':  e.subject.name       if e.subject else None,
                'subject':       e.subject.name       if e.subject else None,
                'section_id':    e.section_id,
                'section_name':  e.section.name       if e.section else None,
                'section':       e.section.name       if e.section else None,
                'grade_name':    e.section.grade.name if e.section and e.section.grade else None,
                'grade':         e.section.grade.name if e.section and e.section.grade else None,
                'exam_date':     e.exam_date.isoformat() if e.exam_date else None,
                'max_score':     float(e.max_marks),
                'max_marks':     float(e.max_marks),
                'pass_marks':    float(e.pass_marks),
                'notes':         None,
                'is_upcoming':   e.exam_date >= today if e.exam_date else None,
                'result_count':  ExamResult.query.filter_by(exam_id=e.id).count(),
                'created_at':    e.created_at.isoformat() if e.created_at else None,
            }
            for e in exams
        ],
    )


# ─── Create exam ──────────────────────────────────────────────────────────────

@mobile_api_bp.route('/teacher/exams', methods=['POST'])
@jwt_required()
@role_required('teacher')
def teacher_create_exam():
    """
    Create an exam in one of the teacher's sections.

    Request body (JSON):
      {
        "section_id":   <int>,           required
        "subject_id":   <int>,           required
        "exam_date":    "YYYY-MM-DD",    required
        "max_marks":    100,             optional — default 100
        "pass_marks":   50,              optional — default 50
        "exam_name":    "...",           optional — free-text name
        "exam_type_id": <int>            optional — ExamType foreign key
      }
    """
    emp = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)

    payload      = request.get_json(silent=True) or {}
    # Accept 'title' (Flutter spec) or legacy 'exam_name'
    title        = (payload.get('title') or payload.get('exam_name') or '').strip() or None
    section_id   = payload.get('section_id')
    subject_id   = payload.get('subject_id')
    exam_date_s  = payload.get('exam_date')
    # Accept 'max_score' (Flutter spec) or legacy 'max_marks'
    max_marks    = payload.get('max_score') if payload.get('max_score') is not None else payload.get('max_marks', 100)
    pass_marks   = payload.get('pass_marks', 50)
    exam_type_id = payload.get('exam_type_id')

    # Per-field validation with spec-format errors
    if not title:
        return err('required_field_missing: title')
    if not section_id:
        return err('required_field_missing: section_id')
    if not subject_id:
        return err('required_field_missing: subject_id')
    if not exam_date_s:
        return err('required_field_missing: exam_date')
    try:
        max_marks_val = float(max_marks)
        if max_marks_val <= 0:
            return err('max_score must be greater than 0')
    except (TypeError, ValueError):
        return err('invalid value: max_score')

    # Security: teacher must have access to the section
    if section_id not in _teacher_section_ids(emp):
        return err('forbidden — section not assigned to you', 403)

    # Validate subject is assigned to this teacher
    allowed_subject_ids = {row.subject_id for row in db.session.execute(
        select(teacher_subjects.c.subject_id).where(
            teacher_subjects.c.employee_id == emp.id
        ).distinct()
    ).fetchall()}
    if subject_id not in allowed_subject_ids:
        return err('forbidden — subject not assigned to you', 403)

    subject = db.session.get(Subject, subject_id)
    if not subject or subject.school_id != emp.school_id:
        return err('subject_not_found', 404)

    try:
        exam_date_obj = _dt.strptime(exam_date_s, '%Y-%m-%d').date()
    except ValueError:
        return err('invalid exam_date — use YYYY-MM-DD')

    # Get the current academic year from the school
    user   = g.mobile_user
    school = user.school
    year   = school.current_year if school else None
    if not year:
        return err('no_active_academic_year', 400)

    new_exam = Exam(
        school_id        = emp.school_id,
        academic_year_id = year.id,
        section_id       = section_id,
        subject_id       = subject_id,
        exam_date        = exam_date_obj,
        max_marks        = max_marks_val,
        pass_marks       = float(pass_marks),
        exam_name        = title,
        exam_type_id     = exam_type_id,
    )
    db.session.add(new_exam)
    db.session.commit()

    return ok(
        message='exam_created',
        exam={
            'id':           new_exam.id,
            'title':        new_exam.display_name,
            'name':         new_exam.display_name,
            'subject_id':   new_exam.subject_id,
            'subject_name': new_exam.subject.name if new_exam.subject else None,
            'subject':      new_exam.subject.name if new_exam.subject else None,
            'section_id':   new_exam.section_id,
            'section_name': new_exam.section.name if new_exam.section else None,
            'section':      new_exam.section.name if new_exam.section else None,
            'grade_name':   new_exam.section.grade.name if new_exam.section and new_exam.section.grade else None,
            'exam_date':    new_exam.exam_date.isoformat(),
            'max_score':    float(new_exam.max_marks),
            'max_marks':    float(new_exam.max_marks),
            'pass_marks':   float(new_exam.pass_marks),
            'notes':        None,
            'created_at':   new_exam.created_at.isoformat() if new_exam.created_at else None,
        },
    ), 201


# ─── Exam detail + results ────────────────────────────────────────────────────

@mobile_api_bp.route('/teacher/exams/<int:exam_id>', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_exam_detail(exam_id):
    """
    Exam metadata + entered results + list of students still missing a result.
    """
    emp  = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)
    exam = _assert_exam_access(emp, exam_id)

    section_students = (Student.query
                        .filter_by(section_id=exam.section_id, status='active')
                        .order_by(Student.full_name)
                        .all())
    results = ExamResult.query.filter_by(exam_id=exam.id).all()

    students_map = {s.id: s for s in section_students}
    entered_ids  = {r.student_id for r in results}
    missing      = [s for s in section_students if s.id not in entered_ids]

    return ok(
        exam={
            'id':               exam.id,
            'title':            exam.display_name,
            'name':             exam.display_name,
            'subject_id':       exam.subject_id,
            'subject_name':     exam.subject.name       if exam.subject else None,
            'subject':          exam.subject.name       if exam.subject else None,
            'section_id':       exam.section_id,
            'section_name':     exam.section.name       if exam.section else None,
            'section':          exam.section.name       if exam.section else None,
            'grade_name':       exam.section.grade.name if exam.section and exam.section.grade else None,
            'grade':            exam.section.grade.name if exam.section and exam.section.grade else None,
            'exam_date':        exam.exam_date.isoformat() if exam.exam_date else None,
            'max_score':        float(exam.max_marks),
            'max_marks':        float(exam.max_marks),
            'pass_marks':       float(exam.pass_marks),
            'notes':            None,
            'created_at':       exam.created_at.isoformat() if exam.created_at else None,
            'total_students':   len(section_students),
            'results_entered':  len(results),
            'results_missing':  len(missing),
        },
        results=[
            {
                'student_id':   r.student_id,
                'student_name': students_map[r.student_id].full_name if r.student_id in students_map else '?',
                'marks':        float(r.marks) if r.marks is not None else None,
                'grade':        r.grade_letter,
                'is_pass':      r.is_pass,
                'rank':         r.rank,
                'notes':        r.notes,
            }
            for r in results
        ],
        missing_students=[
            {'id': s.id, 'student_id': s.student_id, 'name': s.full_name}
            for s in missing
        ],
    )


# ─── Bulk upsert exam results (grade entry) ───────────────────────────────────

@mobile_api_bp.route('/teacher/exams/<int:exam_id>/results', methods=['POST'])
@jwt_required()
@role_required('teacher')
def teacher_enter_results(exam_id):
    """
    Bulk create or update exam results (grade entry).

    Request body (JSON):
      {
        "results": [
          {"student_id": 123, "marks": 88.5, "grade_letter": "A", "notes": ""},
          ...
        ]
      }

    Each entry is upserted: created if it does not exist, updated if it does.
    Returns counts of saved entries and any per-student validation errors.
    """
    emp  = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)
    exam = _assert_exam_access(emp, exam_id)

    payload = request.get_json(silent=True) or {}
    entries = payload.get('results', [])
    if not isinstance(entries, list) or not entries:
        return err('results must be a non-empty array')

    # Build allowed student set from the exam's section
    allowed_ids = {
        s.id for s in Student.query.filter_by(section_id=exam.section_id, status='active').all()
    }

    saved  = 0
    errors = []

    for entry in entries:
        sid = entry.get('student_id')
        if sid not in allowed_ids:
            errors.append({'student_id': sid, 'error': 'not_in_section'})
            continue

        # Accept 'score' (Flutter spec) or legacy 'marks'
        raw_marks = entry.get('score') if entry.get('score') is not None else entry.get('marks')
        if raw_marks is None:
            errors.append({'student_id': sid, 'error': 'marks_required'})
            continue
        try:
            marks = float(raw_marks)
        except (TypeError, ValueError):
            errors.append({'student_id': sid, 'error': 'invalid_marks_value'})
            continue
        if marks < 0 or marks > float(exam.max_marks):
            errors.append({'student_id': sid, 'error': f'marks must be between 0 and {exam.max_marks}'})
            continue

        # Accept 'note' (Flutter spec, singular) or legacy 'notes' (plural)
        entry_notes = entry.get('note') if entry.get('note') is not None else entry.get('notes')
        is_pass = marks >= float(exam.pass_marks)

        existing = ExamResult.query.filter_by(exam_id=exam.id, student_id=sid).first()
        if existing:
            existing.marks        = marks
            existing.grade_letter = entry.get('grade_letter') or existing.grade_letter
            existing.is_pass      = is_pass
            existing.notes        = entry_notes if entry_notes is not None else existing.notes
            existing.entered_by   = g.mobile_user.id
        else:
            new_result = ExamResult(
                exam_id          = exam.id,
                student_id       = sid,
                school_id        = exam.school_id,
                academic_year_id = exam.academic_year_id,
                marks            = marks,
                grade_letter     = entry.get('grade_letter'),
                is_pass          = is_pass,
                notes            = entry_notes,
                entered_by       = g.mobile_user.id,
            )
            db.session.add(new_result)
        saved += 1

    db.session.commit()
    return ok(saved=saved, errors=errors)


# ─── Teacher notifications ────────────────────────────────────────────────────

@mobile_api_bp.route('/teacher/notifications', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_notifications():
    """
    Paginated notifications visible to this teacher.
    Query params: limit (default 50, max 100), offset (default 0).
    """
    user   = g.mobile_user
    limit  = min(int(request.args.get('limit', 50)), 100)
    offset = max(int(request.args.get('offset', 0)), 0)

    q     = (Notification.query
             .filter(notification_visible_to(user))
             .order_by(Notification.created_at.desc()))
    total = q.count()
    rows  = q.offset(offset).limit(limit).all()

    read_ids = {
        nr.notification_id
        for nr in NotificationRead.query.filter_by(user_id=user.id).all()
    }

    return ok(
        total=total,
        limit=limit,
        offset=offset,
        notifications=[
            {
                'id':      n.id,
                'title':   n.title,
                'body':    n.body,
                'ntype':   n.ntype,
                'is_read': n.id in read_ids,
                'sent_at': n.created_at.replace(tzinfo=timezone.utc).isoformat() if n.created_at else None,
            }
            for n in rows
        ],
    )


# ─── Teacher homework ─────────────────────────────────────────────────────────

def _hw_attachment_url(hw: Homework) -> str | None:
    if not hw.attachment_path:
        return None
    if hw.attachment_path.startswith(('http://', 'https://')):
        return hw.attachment_path
    return photo_url(hw.attachment_path)


@mobile_api_bp.route('/teacher/homework', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_homework_list():
    """
    List homework created by this teacher for the current academic year.
    Blocked if the school's homework module is disabled (api_access action).

    Query params: limit (default 50, max 100), offset (default 0).
    """
    from app.utils.school_config import get_school_config
    from app.utils.decorators import get_active_year

    user = g.mobile_user
    cfg  = get_school_config(user.school_id)
    if not cfg.action_enabled('homework', 'api_access'):
        return err('الوصول إلى الواجبات غير مفعل لهذه المدرسة.', 403)

    emp = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)

    from app.models import AcademicYear
    year = AcademicYear.query.filter_by(school_id=emp.school_id, is_current=True).first()
    if not year:
        return ok(count=0, homework=[])

    limit  = min(int(request.args.get('limit', 50)), 100)
    offset = max(int(request.args.get('offset', 0)), 0)

    q = (Homework.query
         .filter_by(teacher_id=emp.id, academic_year_id=year.id, is_active=True)
         .order_by(Homework.publish_date.desc(), Homework.id.desc()))

    total = q.count()
    rows  = q.offset(offset).limit(limit).all()

    return ok(
        total=total,
        limit=limit,
        offset=offset,
        homework=[
            {
                'id':              hw.id,
                'title':           hw.title,
                'subject_id':      hw.subject_id,
                'subject_name':    hw.subject.name if hw.subject else None,
                'subject':         hw.subject.name if hw.subject else None,
                'section_id':      hw.section_id,
                'section_name':    hw.section.name if hw.section else None,
                'section':         hw.section.name if hw.section else None,
                'grade_name':      hw.section.grade.name if hw.section and hw.section.grade else None,
                'grade':           hw.section.grade.name if hw.section and hw.section.grade else None,
                'display_name':    f"{hw.section.grade.name} - شعبة {hw.section.name}" if hw.section and hw.section.grade else (hw.section.name if hw.section else None),
                'publish_date':    hw.publish_date.isoformat() if hw.publish_date else None,
                'due_date':        hw.due_date.isoformat() if hw.due_date else None,
                'description':     hw.description,
                'attachment_url':  _hw_attachment_url(hw),
                'attachment_type': hw.attachment_type,
                'created_at':      hw.created_at.isoformat() if hasattr(hw, 'created_at') and hw.created_at else None,
            }
            for hw in rows
        ],
    )


@mobile_api_bp.route('/teacher/homework', methods=['POST'])
@jwt_required()
@role_required('teacher')
def teacher_homework_create():
    """
    Create a new homework assignment from the mobile app.

    Expected JSON body:
        title        str   required
        section_id   int   required
        subject_id   int   optional
        publish_date str   YYYY-MM-DD  required
        due_date     str   YYYY-MM-DD  required
        description  str   optional
    """
    from app.utils.school_config import get_school_config
    from datetime import datetime as _dt

    user = g.mobile_user
    cfg  = get_school_config(user.school_id)
    if not cfg.action_enabled('homework', 'api_access'):
        return err('الوصول إلى الواجبات غير مفعل لهذه المدرسة.', 403)
    if not cfg.action_enabled('homework', 'create'):
        return err('إضافة الواجبات غير مفعلة لهذه المدرسة.', 403)

    emp = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)

    data = request.get_json(silent=True) or {}

    title        = (data.get('title') or '').strip()
    section_id   = data.get('section_id')
    subject_id   = data.get('subject_id')
    publish_date = data.get('publish_date', '')
    due_date_str = data.get('due_date', '')
    description  = (data.get('description') or '').strip() or None

    if not title:
        return err('required_field_missing: title')
    if not section_id:
        return err('required_field_missing: section_id')
    if not subject_id:
        return err('required_field_missing: subject_id')
    if not due_date_str:
        return err('required_field_missing: due_date')

    # publish_date defaults to today if not provided
    if not publish_date:
        from datetime import date as _date
        pub_dt = _date.today()
    else:
        try:
            pub_dt = _dt.strptime(publish_date, '%Y-%m-%d').date()
        except ValueError:
            return err('invalid publish_date — use YYYY-MM-DD')

    try:
        due_dt = _dt.strptime(due_date_str, '%Y-%m-%d').date()
    except ValueError:
        return err('invalid due_date — use YYYY-MM-DD')

    if due_dt < pub_dt:
        return err('due_date must not be before publish_date')

    allowed_sec_ids = _teacher_section_ids(emp)
    if section_id not in allowed_sec_ids:
        return err('forbidden — section not assigned to you', 403)

    allowed_subj_ids = {
        row.subject_id
        for row in db.session.execute(
            select(teacher_subjects.c.subject_id).where(
                teacher_subjects.c.employee_id == emp.id
            )
        ).fetchall()
    }
    if subject_id not in allowed_subj_ids:
        return err('forbidden — subject not assigned to you', 403)

    from app.models import AcademicYear
    year = AcademicYear.query.filter_by(school_id=emp.school_id, is_current=True).first()
    if not year:
        return err('no active academic year', 400)

    hw = Homework(
        school_id=emp.school_id,
        academic_year_id=year.id,
        teacher_id=emp.id,
        subject_id=subject_id or None,
        section_id=section_id,
        title=title,
        description=description,
        publish_date=pub_dt,
        due_date=due_dt,
        is_active=True,
    )
    db.session.add(hw)
    db.session.commit()

    return ok(
        message='تم إضافة الواجب بنجاح.',
        homework={
            'id':           hw.id,
            'title':        hw.title,
            'subject_id':   hw.subject_id,
            'subject_name': hw.subject.name if hw.subject else None,
            'section_id':   hw.section_id,
            'section_name': hw.section.name if hw.section else None,
            'grade_name':   hw.section.grade.name if hw.section and hw.section.grade else None,
            'publish_date': hw.publish_date.isoformat(),
            'due_date':     hw.due_date.isoformat(),
        },
    ), 201

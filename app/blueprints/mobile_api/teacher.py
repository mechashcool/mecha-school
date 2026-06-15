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
GET  /teacher/my-attendance            my own employee-attendance records + summary (current academic year)
GET  /teacher/exams                    exams for my sections (subject_id, section_id, title, max_score)
GET  /teacher/exams/check-conflict     read-only time-overlap check (section_id, exam_date, exam_time, duration_minutes)
GET  /teacher/exams/check-day          read-only same-day exams for a section (section_id, exam_date)
POST /teacher/exams                    create an exam — accepts title + max_score; validates subject assignment
GET  /teacher/exams/<id>               exam detail + entered results (subject_id, section_id, title)
POST /teacher/exams/<id>/results       bulk-upsert grade entries (accepts score/note or marks/notes)
GET  /teacher/notifications            notifications feed (paginated)
GET    /teacher/homework                 homework list (subject_id, section_id, grade_name)
POST   /teacher/homework                 create homework — subject_id required
PUT    /teacher/homework/<id>            update homework — all core fields required
PATCH  /teacher/homework/<id>            same as PUT (both verbs accepted)

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

from flask import abort, g, jsonify, request
from sqlalchemy import select

from app.models import (
    db,
    AcademicYear,
    Employee,
    EmployeeAttendance,
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


def _check_exam_conflict(school_id: int, academic_year_id: int, section_id: int,
                         exam_date, exam_time, duration_minutes: int,
                         exclude_exam_id: int | None = None) -> 'Exam | None':
    """
    Return the first Exam whose time range overlaps the proposed slot, or None.
    Exams with null exam_time or null duration_minutes are skipped (cannot be time-compared).
    Overlap rule: new_start < existing_end AND existing_start < new_end
    """
    q = Exam.query.filter(
        Exam.school_id        == school_id,
        Exam.academic_year_id == academic_year_id,
        Exam.section_id       == section_id,
        Exam.exam_date        == exam_date,
        Exam.exam_time.isnot(None),
        Exam.duration_minutes.isnot(None),
    )
    if exclude_exam_id:
        q = q.filter(Exam.id != exclude_exam_id)

    new_start = exam_time.hour * 60 + exam_time.minute
    new_end   = new_start + duration_minutes

    for e in q.all():
        e_start = e.exam_time.hour * 60 + e.exam_time.minute
        e_end   = e_start + e.duration_minutes
        if new_start < e_end and e_start < new_end:
            return e
    return None


def _conflict_dict(e: Exam) -> dict:
    return {
        'exam_id':          e.id,
        'title':            e.display_name,
        'subject_name':     e.subject.name if e.subject else None,
        'exam_date':        e.exam_date.isoformat() if e.exam_date else None,
        'exam_time':        e.exam_time.strftime('%H:%M') if e.exam_time else None,
        'duration_minutes': e.duration_minutes,
    }


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


def _emp_att_status(raw: str | None) -> str:
    """
    Normalize a stored employee-attendance status for the mobile client.

    Stored values in EmployeeAttendance.status are 'present' | 'late' | 'absent'.
    The value is lower-cased/trimmed defensively and returned. Any value other
    than present/late/absent is passed through unchanged and is NOT counted in
    the summary present/late/absent buckets (still counted in total_days).
    """
    return (raw or '').strip().lower()


def _emp_att_notes(rec: EmployeeAttendance) -> str:
    """
    Safe notes value for the mobile client.

    AiFace device records store an internal dedup marker ("AI Face HH:MM:SS")
    in the notes column — an implementation detail that must not be exposed.
    For aiface-sourced rows we return an empty string; manual notes are returned
    as-is. NULL notes become an empty string.
    """
    if rec.source == 'aiface':
        return ''
    return rec.notes or ''


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

    school = emp.school
    return ok(
        employee={
            'id':          emp.id,
            'employee_id': emp.employee_id,
            'user_id':     emp.user_id,
            'name':        emp.full_name,
            'full_name':   emp.full_name,
            'job_title':   emp.job_title,
            'department':  emp.department,
            'phone':       emp.phone,
            'email':       emp.email,
            'photo':       photo_url(emp.photo),
            'photo_url':   photo_url(emp.photo),
            'hire_date':   emp.hire_date.isoformat() if emp.hire_date else None,
            'status':      emp.status,
            'school_id':   emp.school_id,
            'school_name': school.school_name if school else None,
            'role':        g.mobile_user.role.name if g.mobile_user.role else None,
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
    """
    The teacher's weekly timetable for the current academic year.

    Security:
      • Teacher/employee identity is resolved entirely from the JWT via
        _get_employee() (Employee.user_id link). No teacher_id / employee_id /
        user_id / school_id / academic_year_id is ever read from the client.
      • Schedule.teacher_id is optional (NULL when admin does not assign a teacher
        to a period). The query uses three OR branches: (1) teacher_id == emp.id —
        explicit assignments always included; (2) section_id in teacher's sections
        AND teacher_id IS NULL; (3) grade_id in teacher's grades AND section_id IS
        NULL AND teacher_id IS NULL. Entries explicitly assigned to a DIFFERENT
        employee are never included (branches 2 and 3 require teacher_id IS NULL).
      • The query is explicitly scoped to school_id and academic_year_id so that
        no other school's or year's data can be returned. The ORM global tenant
        scope is inert on mobile (JWT login happens inside the view, after
        before_request has already cached g.tenant_scope_school_id = None) —
        explicit column filters are mandatory.
      • Both section-based (section_id set, grade_id NULL) and grade-based
        (grade_id set, section_id NULL) schedule rows are returned.
    """
    emp = _get_employee()
    if not emp:
        return jsonify({
            'ok':      False,
            'error':   'teacher_employee_profile_not_found',
            'message': 'No employee profile is associated with this account',
        }), 404

    # Resolve the school's current active academic year explicitly.
    # Mobile has no historical view-year selection; if no active year exists,
    # return an empty schedule rather than leaking historical data.
    year = AcademicYear.query.filter_by(school_id=emp.school_id, is_current=True).first()
    if not year:
        return ok(schedule=[])

    # Schedule.teacher_id is optional (NULL when the web UI does not assign a
    # teacher to a period). Filtering by teacher_id == emp.id alone returns an
    # empty result set whenever teacher_id was left NULL in the web.
    #
    # The authoritative teacher-scope is the set of sections/grades this teacher
    # is responsible for, derived entirely server-side from their homeroom and
    # subject assignments — consistent with how all other teacher endpoints work.
    #
    # Isolation:
    #   school_id        → explicit column filter; prevent cross-school leakage
    #   academic_year_id → explicit column filter; current year only
    #   section/grade    → server-side from _teacher_section_ids(); no client input
    section_ids = list(_teacher_section_ids(emp))

    # Three ownership branches (OR):
    #   1. teacher_id == emp.id — entry explicitly assigned to this teacher;
    #      always included regardless of section/grade membership.
    #   2. section_id IN teacher's sections AND teacher_id IS NULL — unassigned
    #      section-based entry; section membership is the scope.
    #   3. grade_id IN teacher's grades AND section_id IS NULL AND teacher_id IS NULL —
    #      unassigned whole-grade entry; grade membership (derived from teacher's
    #      sections) is the scope.
    # Entries with teacher_id pointing to a DIFFERENT employee are excluded because
    # branches 2 and 3 require teacher_id IS NULL.
    or_clauses = [Schedule.teacher_id == emp.id]

    if section_ids:
        or_clauses.append(
            db.and_(
                Schedule.section_id.in_(section_ids),
                Schedule.teacher_id.is_(None),
            )
        )

        # Derive grade_ids so that unassigned whole-grade rows are included.
        # Explicit school_id guard — ORM scope is inert for mobile requests.
        grade_sections = (
            Section.query
            .filter(
                Section.id.in_(section_ids),
                Section.school_id == emp.school_id,
            )
            .all()
        )
        grade_ids = list({s.grade_id for s in grade_sections if s.grade_id})

        if grade_ids:
            or_clauses.append(
                db.and_(
                    Schedule.grade_id.in_(grade_ids),
                    Schedule.section_id.is_(None),
                    Schedule.teacher_id.is_(None),
                )
            )

    schedules = (Schedule.query
                 .filter(
                     Schedule.school_id        == emp.school_id,
                     Schedule.academic_year_id == year.id,
                     db.or_(*or_clauses),
                 )
                 .order_by(Schedule.day_of_week, Schedule.start_time)
                 .all())

    def _grade_id(sch: Schedule) -> int | None:
        # Grade-based entry: grade_id is stored directly on the row.
        if sch.grade_id:
            return sch.grade_id
        # Section-based entry: derive from the linked section's grade.
        if sch.section and sch.section.grade_id:
            return sch.section.grade_id
        return None

    def _grade_name(sch: Schedule) -> str | None:
        # Grade-based entry: use the directly linked grade relationship.
        if sch.grade_id and sch.grade:
            return sch.grade.name
        # Section-based entry: derive from the linked section's grade.
        if sch.section and sch.section.grade:
            return sch.section.grade.name
        return None

    return ok(
        schedule=[
            {
                'id':           sch.id,
                # 'day' returns the English lowercase name per the Flutter spec.
                # 'day_int' exposes the raw 0-6 integer for callers that need it.
                'day':          _DAY_NAMES_EN.get(sch.day_of_week, ''),
                'day_int':      sch.day_of_week,
                'day_en':       _DAY_NAMES_EN.get(sch.day_of_week, ''),
                'day_label':    _DAY_NAMES.get(sch.day_of_week, ''),
                'day_name':     _DAY_NAMES.get(sch.day_of_week, ''),
                'start_time':   _fmt_time(sch.start_time),
                'end_time':     _fmt_time(sch.end_time),
                'grade_id':     _grade_id(sch),
                'grade_name':   _grade_name(sch),
                'grade':        _grade_name(sch),
                'section_id':   sch.section_id,
                'section_name': sch.section.name if sch.section else None,
                'section':      sch.section.name if sch.section else None,
                'subject_id':   sch.subject_id,
                'subject_name': sch.subject.name if sch.subject else None,
                'subject':      sch.subject.name if sch.subject else None,
                'subject_code': sch.subject.code if sch.subject else None,
                'room':         sch.room,
            }
            for sch in schedules
        ],
    )


# ─── My (own) employee attendance ─────────────────────────────────────────────

@mobile_api_bp.route('/teacher/my-attendance', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_my_attendance():
    """
    The authenticated teacher's OWN employee-attendance records for the current
    academic year.

    Security:
      • Teacher/employee identity is resolved entirely from the JWT via
        _get_employee() (Employee.user_id link). No teacher_id / employee_id /
        user_id / school_id is ever read from the query string, route, headers,
        or body — any such client value is ignored.
      • The query is explicitly scoped to the employee's own school_id,
        employee_id, AND the school's current academic_year_id, so it cannot
        return another teacher's or another school's rows even though the ORM
        global tenant scope is inert on mobile (JWT login happens after the
        before_request scope is cached).

    Response (200):
      {
        "ok": true,
        "records": [
          {"id": 1, "date": "2026-06-12", "check_in": "08:00",
           "check_out": "13:30", "status": "present", "notes": ""}
        ],
        "summary": {"total_days": 20, "present_days": 18,
                    "late_days": 2, "absent_days": 0}
      }

    No linked employee profile (404):
      {"ok": false, "error": "teacher_employee_profile_not_found",
       "message": "No employee profile is associated with this account"}
    """
    emp = _get_employee()
    if not emp:
        return jsonify({
            'ok':      False,
            'error':   'teacher_employee_profile_not_found',
            'message': 'No employee profile is associated with this account',
        }), 404

    empty_summary = {'total_days': 0, 'present_days': 0, 'late_days': 0, 'absent_days': 0}

    # Employee attendance is academic-year scoped. Resolve the school's current
    # active year explicitly (mobile has no historical view-year selection).
    # If the school has no active year configured, fail closed with no data.
    year = AcademicYear.query.filter_by(school_id=emp.school_id, is_current=True).first()
    if not year:
        return ok(records=[], summary=dict(empty_summary))

    # Explicit isolation: own school_id + own employee_id + current year.
    records = (EmployeeAttendance.query
               .filter_by(school_id=emp.school_id,
                          employee_id=emp.id,
                          academic_year_id=year.id)
               .order_by(EmployeeAttendance.date.desc())
               .all())

    out = []
    present_days = late_days = absent_days = 0
    for r in records:
        status = _emp_att_status(r.status)
        if status == 'present':
            present_days += 1
        elif status == 'late':
            late_days += 1
        elif status == 'absent':
            absent_days += 1
        out.append({
            'id':        r.id,
            'date':      r.date.isoformat() if r.date else None,
            'check_in':  _fmt_time(r.check_in),
            'check_out': _fmt_time(r.check_out),
            'status':    status,
            'notes':     _emp_att_notes(r),
        })

    # total_days = number of attendance rows for the current academic year.
    # Equals distinct attendance dates because of the (employee_id, date)
    # unique constraint, so duplicate device punches cannot inflate it.
    return ok(
        records=out,
        summary={
            'total_days':   len(records),
            'present_days': present_days,
            'late_days':    late_days,
            'absent_days':  absent_days,
        },
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
                'exam_date':       e.exam_date.isoformat() if e.exam_date else None,
                'exam_time':       e.exam_time.strftime('%H:%M') if e.exam_time else None,
                'duration_minutes': e.duration_minutes,
                'max_score':       float(e.max_marks),
                'max_marks':       float(e.max_marks),
                'pass_marks':      float(e.pass_marks),
                'notes':           None,
                'is_upcoming':     e.exam_date >= today if e.exam_date else None,
                'result_count':    ExamResult.query.filter_by(exam_id=e.id).count(),
                'created_at':      e.created_at.isoformat() if e.created_at else None,
            }
            for e in exams
        ],
    )


# ─── Conflict check (read-only) ───────────────────────────────────────────────

@mobile_api_bp.route('/teacher/exams/check-conflict', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_check_exam_conflict():
    """
    Check whether a proposed exam slot conflicts with existing exams for the same section.
    Read-only — never modifies anything.

    Query params:
      section_id        int       required
      exam_date         YYYY-MM-DD required
      exam_time         HH:MM     required
      duration_minutes  int       required
      subject_id        int       optional (ignored in conflict logic, for caller context)
      exclude_exam_id   int       optional (exclude this exam — useful for edit support)
    """
    emp = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)

    args = request.args

    try:
        section_id = int(args.get('section_id') or 0)
    except (TypeError, ValueError):
        return err('invalid section_id')
    if not section_id:
        return err('required_field_missing: section_id')
    if section_id not in _teacher_section_ids(emp):
        return err('forbidden — section not assigned to you', 403)

    exam_date_s = (args.get('exam_date') or '').strip()
    if not exam_date_s:
        return err('required_field_missing: exam_date')
    try:
        exam_date_obj = _dt.strptime(exam_date_s, '%Y-%m-%d').date()
    except ValueError:
        return err('invalid exam_date — use YYYY-MM-DD')

    exam_time_s = (args.get('exam_time') or '').strip()
    if not exam_time_s:
        return err('required_field_missing: exam_time')
    try:
        exam_time_obj = _dt.strptime(exam_time_s, '%H:%M').time()
    except ValueError:
        return err('invalid exam_time — use HH:MM')

    try:
        dur = int(args.get('duration_minutes') or 0)
        if dur <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return err('duration_minutes must be a positive integer')

    try:
        exclude_id = int(args['exclude_exam_id']) if args.get('exclude_exam_id') else None
    except (TypeError, ValueError):
        return err('invalid exclude_exam_id')

    user   = g.mobile_user
    school = user.school
    year   = school.current_year if school else None
    if not year:
        return err('no_active_academic_year', 400)

    conflict = _check_exam_conflict(
        school_id        = emp.school_id,
        academic_year_id = year.id,
        section_id       = section_id,
        exam_date        = exam_date_obj,
        exam_time        = exam_time_obj,
        duration_minutes = dur,
        exclude_exam_id  = exclude_id,
    )

    if conflict:
        return ok(
            has_conflict = True,
            available    = False,
            message      = 'There is another exam for this section at the same time',
            conflict     = _conflict_dict(conflict),
        )
    return ok(has_conflict=False, available=True)


# ─── Same-day exam lookup (read-only, no time required) ──────────────────────

@mobile_api_bp.route('/teacher/exams/check-day', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_check_exam_day():
    """
    Return all exams already scheduled for a given section on a given date,
    regardless of subject or exam time.

    This is a soft informational check only — it never blocks creation.
    The existing /teacher/exams/check-conflict endpoint (which requires
    exam_time + duration_minutes) is what drives the POST 409 hard block.

    Query params:
      section_id  int        required
      exam_date   YYYY-MM-DD required

    Success response:
      {
        "ok": true,
        "has_exams_same_day": true,
        "same_day_exams": [
          {"exam_id": 15, "title": "...", "subject_name": "...",
           "exam_date": "2026-06-14", "exam_time": "09:00",
           "duration_minutes": 45}
        ]
      }
    Empty:
      {"ok": true, "has_exams_same_day": false, "same_day_exams": []}
    """
    emp = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)

    args = request.args

    try:
        section_id = int(args.get('section_id') or 0)
    except (TypeError, ValueError):
        return err('invalid section_id')
    if not section_id:
        return err('required_field_missing: section_id')
    if section_id not in _teacher_section_ids(emp):
        return err('forbidden — section not assigned to you', 403)

    exam_date_s = (args.get('exam_date') or '').strip()
    if not exam_date_s:
        return err('required_field_missing: exam_date')
    try:
        exam_date_obj = _dt.strptime(exam_date_s, '%Y-%m-%d').date()
    except ValueError:
        return err('invalid exam_date — use YYYY-MM-DD')

    user   = g.mobile_user
    school = user.school
    year   = school.current_year if school else None
    if not year:
        return err('no_active_academic_year', 400)

    # Explicit school + year + section + date scope — never relies on global
    # ORM tenant filter, which is inert for mobile (JWT login happens inside
    # the view, after before_request has already cached the scope).
    exams = (Exam.query
             .filter(
                 Exam.school_id        == emp.school_id,
                 Exam.academic_year_id == year.id,
                 Exam.section_id       == section_id,
                 Exam.exam_date        == exam_date_obj,
             )
             .all())

    # Sort: exams with a known time first (ascending), then timeless, then by id.
    exams.sort(key=lambda e: (
        1 if e.exam_time is None else 0,
        (e.exam_time.hour * 60 + e.exam_time.minute) if e.exam_time else 0,
        e.id,
    ))

    return ok(
        has_exams_same_day=bool(exams),
        same_day_exams=[
            {
                'exam_id':          e.id,
                'title':            e.display_name,
                'subject_name':     e.subject.name if e.subject else None,
                'exam_date':        e.exam_date.isoformat() if e.exam_date else None,
                'exam_time':        e.exam_time.strftime('%H:%M') if e.exam_time else None,
                'duration_minutes': e.duration_minutes,
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
    exam_time_s  = (payload.get('exam_time') or '').strip() or None
    dur_raw      = payload.get('duration_minutes')

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

    # Parse optional exam_time
    exam_time_obj = None
    if exam_time_s:
        try:
            exam_time_obj = _dt.strptime(exam_time_s, '%H:%M').time()
        except ValueError:
            return err('invalid exam_time — use HH:MM')

    # Parse optional duration_minutes
    dur_val = None
    if dur_raw is not None:
        try:
            dur_val = int(dur_raw)
            if dur_val <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return err('duration_minutes must be a positive integer')

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

    # Conflict check — only when both exam_time and duration_minutes are provided
    if exam_time_obj is not None and dur_val is not None:
        conflict = _check_exam_conflict(
            school_id        = emp.school_id,
            academic_year_id = year.id,
            section_id       = section_id,
            exam_date        = exam_date_obj,
            exam_time        = exam_time_obj,
            duration_minutes = dur_val,
        )
        if conflict:
            return jsonify({
                'ok':      False,
                'error':   'exam_time_conflict',
                'message': 'There is another exam for this section at the same time',
                'conflict': _conflict_dict(conflict),
            }), 409

    new_exam = Exam(
        school_id        = emp.school_id,
        academic_year_id = year.id,
        section_id       = section_id,
        subject_id       = subject_id,
        exam_date        = exam_date_obj,
        exam_time        = exam_time_obj,
        duration_minutes = dur_val,
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
            'exam_date':        new_exam.exam_date.isoformat(),
            'exam_time':        new_exam.exam_time.strftime('%H:%M') if new_exam.exam_time else None,
            'duration_minutes': new_exam.duration_minutes,
            'max_score':        float(new_exam.max_marks),
            'max_marks':        float(new_exam.max_marks),
            'pass_marks':       float(new_exam.pass_marks),
            'notes':            None,
            'created_at':       new_exam.created_at.isoformat() if new_exam.created_at else None,
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
            'exam_time':        exam.exam_time.strftime('%H:%M') if exam.exam_time else None,
            'duration_minutes': exam.duration_minutes,
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

    Body: application/json  OR  multipart/form-data.
    Multipart adds an optional 'attachment' file field (jpg/jpeg/png/webp/pdf).

    Fields:
        title        str   required
        section_id   int   required
        subject_id   int   required
        due_date     str   YYYY-MM-DD  required
        publish_date str   YYYY-MM-DD  optional (defaults to today)
        description  str   optional
        attachment   file  optional (multipart only)
    """
    from app.utils.school_config import get_school_config
    from app.utils.helpers import save_uploaded_file
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

    is_multipart = bool(
        request.content_type and 'multipart/form-data' in request.content_type
    )
    if is_multipart:
        data        = request.form
        attachment  = request.files.get('attachment')
    else:
        data        = request.get_json(silent=True) or {}
        attachment  = None

    title        = (data.get('title')       or '').strip()
    section_id   = data.get('section_id')
    subject_id   = data.get('subject_id')
    publish_date = (data.get('publish_date') or '').strip()
    due_date_str = (data.get('due_date')     or '').strip()
    description  = (data.get('description') or '').strip() or None

    try:
        section_id = int(section_id) if section_id is not None else None
    except (ValueError, TypeError):
        section_id = None
    try:
        subject_id = int(subject_id) if subject_id is not None else None
    except (ValueError, TypeError):
        subject_id = None

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

    # Handle optional attachment upload
    att_path = None
    att_type = None
    if attachment and attachment.filename:
        _HOMEWORK_EXTS = {'jpg', 'jpeg', 'png', 'webp', 'pdf'}
        uploaded = save_uploaded_file(
            attachment,
            subfolder='homework',
            allowed_exts=_HOMEWORK_EXTS,
        )
        if uploaded is None:
            return err('invalid_attachment — allowed: jpg, jpeg, png, webp, pdf')
        orig_ext = (
            attachment.filename.rsplit('.', 1)[-1].lower()
            if '.' in attachment.filename else ''
        )
        att_path = uploaded
        att_type = 'pdf' if orig_ext == 'pdf' else 'image'

    hw = Homework(
        school_id=emp.school_id,
        academic_year_id=year.id,
        teacher_id=emp.id,
        subject_id=subject_id,
        section_id=section_id,
        title=title,
        description=description,
        publish_date=pub_dt,
        due_date=due_dt,
        is_active=True,
        attachment_path=att_path,
        attachment_type=att_type,
    )
    db.session.add(hw)
    db.session.commit()

    att_url  = _hw_attachment_url(hw)
    att_name = hw.attachment_path.rstrip('/').rsplit('/', 1)[-1] if hw.attachment_path else None

    return ok(
        message='تم إضافة الواجب بنجاح.',
        homework={
            'id':              hw.id,
            'title':           hw.title,
            'description':     hw.description,
            'subject_id':      hw.subject_id,
            'subject_name':    hw.subject.name if hw.subject else None,
            'section_id':      hw.section_id,
            'section_name':    hw.section.name if hw.section else None,
            'grade_name':      hw.section.grade.name if hw.section and hw.section.grade else None,
            'publish_date':    hw.publish_date.isoformat(),
            'due_date':        hw.due_date.isoformat(),
            'attachment_url':  att_url,
            'attachment_name': att_name,
            'attachment_type': hw.attachment_type,
        },
    ), 201


@mobile_api_bp.route('/teacher/homework/<int:homework_id>', methods=['PUT', 'PATCH', 'DELETE'])
@jwt_required()
@role_required('teacher')
def teacher_homework_update(homework_id):
    """
    PUT/PATCH: Update an existing homework assignment.
    DELETE:    Soft-delete (sets is_active=False). Teacher can only delete
               their own homework within their own school.

    Body (PUT/PATCH): application/json  OR  multipart/form-data.
    Multipart adds an optional 'attachment' file field (jpg/jpeg/png/webp/pdf).
    """
    from app.utils.school_config import get_school_config
    from app.utils.helpers import save_uploaded_file

    user = g.mobile_user
    cfg  = get_school_config(user.school_id)
    if not cfg.action_enabled('homework', 'api_access'):
        return err('الوصول إلى الواجبات غير مفعل لهذه المدرسة.', 403)

    emp = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)

    hw = Homework.query.filter_by(
        id=homework_id,
        school_id=emp.school_id,
        teacher_id=emp.id,
        is_active=True,
    ).first()
    if not hw:
        return err('homework_not_found', 404)

    if request.method == 'DELETE':
        hw.is_active = False
        db.session.commit()
        return ok(message='homework_deleted')

    # ── PUT / PATCH ────────────────────────────────────────────────────────
    is_multipart = bool(
        request.content_type and 'multipart/form-data' in request.content_type
    )
    if is_multipart:
        title        = (request.form.get('title')       or '').strip()
        description  = (request.form.get('description') or '').strip() or None
        due_date_str = (request.form.get('due_date')    or '').strip()
        section_id   = request.form.get('section_id')
        subject_id   = request.form.get('subject_id')
    else:
        data         = request.get_json(silent=True) or {}
        title        = (data.get('title')       or '').strip()
        description  = (data.get('description') or '').strip() or None
        due_date_str = (data.get('due_date')    or '').strip()
        section_id   = data.get('section_id')
        subject_id   = data.get('subject_id')

    try:
        section_id = int(section_id) if section_id is not None else None
    except (ValueError, TypeError):
        section_id = None
    try:
        subject_id = int(subject_id) if subject_id is not None else None
    except (ValueError, TypeError):
        subject_id = None

    if not title:
        return err('required_field_missing: title')
    if not section_id:
        return err('required_field_missing: section_id')
    if not subject_id:
        return err('required_field_missing: subject_id')
    if not due_date_str:
        return err('required_field_missing: due_date')

    try:
        due_dt = _dt.strptime(due_date_str, '%Y-%m-%d').date()
    except ValueError:
        return err('invalid due_date — use YYYY-MM-DD')

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

    # Attachment replacement (multipart only).
    # NOTE: the old file is NOT deleted from Supabase Storage — the project
    # does not yet have a storage-delete helper.
    new_path = hw.attachment_path
    new_type = hw.attachment_type
    if is_multipart:
        attachment_file = request.files.get('attachment')
        if attachment_file and attachment_file.filename:
            _HOMEWORK_EXTS = {'jpg', 'jpeg', 'png', 'webp', 'pdf'}
            uploaded = save_uploaded_file(
                attachment_file,
                subfolder='homework',
                allowed_exts=_HOMEWORK_EXTS,
            )
            if uploaded is None:
                return err('invalid_attachment — allowed: jpg, jpeg, png, webp, pdf')
            orig_ext = (
                attachment_file.filename.rsplit('.', 1)[-1].lower()
                if '.' in attachment_file.filename else ''
            )
            new_path = uploaded
            new_type = 'pdf' if orig_ext == 'pdf' else 'image'

    hw.title           = title
    hw.description     = description
    hw.section_id      = section_id
    hw.subject_id      = subject_id
    hw.due_date        = due_dt
    hw.attachment_path = new_path
    hw.attachment_type = new_type
    db.session.commit()

    att_url  = _hw_attachment_url(hw)
    att_name = hw.attachment_path.rstrip('/').rsplit('/', 1)[-1] if hw.attachment_path else None

    return ok(
        homework={
            'id':              hw.id,
            'title':           hw.title,
            'description':     hw.description,
            'section_id':      hw.section_id,
            'section_name':    hw.section.name if hw.section else None,
            'grade_name':      hw.section.grade.name if hw.section and hw.section.grade else None,
            'subject_id':      hw.subject_id,
            'subject_name':    hw.subject.name if hw.subject else None,
            'due_date':        hw.due_date.isoformat(),
            'attachment_url':  att_url,
            'attachment_name': att_name,
            'attachment_type': hw.attachment_type,
        }
    )

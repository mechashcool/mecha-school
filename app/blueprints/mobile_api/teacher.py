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
from decimal import Decimal, InvalidOperation

from flask import abort, g, jsonify, request
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

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
    User,
    parent_students,
    teacher_subjects,
)
from app.utils.helpers import calculate_grade_letter
from app.utils.notification_visibility import notification_visible_to

from . import mobile_api_bp
from .utils import jwt_required, role_required, ok, ok_etag, err, photo_url


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


def _teacher_exam_filter(emp: Employee):
    """Return an ORM WHERE clause restricting exams to this teacher's access set.

    Homeroom sections (Section.teacher_id == emp.id): all exams in the section.
    Subject-assigned sections (teacher_subjects junction): only exams whose
    (section_id, subject_id) matches the explicit assignment row.
    Returns None when the teacher has no access (no sections, no assignments).
    """
    homeroom_ids = list({s.id for s in emp.sections_managed})
    rows = db.session.execute(
        select(teacher_subjects.c.section_id, teacher_subjects.c.subject_id).where(
            teacher_subjects.c.employee_id == emp.id
        )
    ).fetchall()

    clauses = []
    homeroom_set = set(homeroom_ids)

    if homeroom_ids:
        clauses.append(Exam.section_id.in_(homeroom_ids))

    for row in rows:
        if row.section_id not in homeroom_set:
            clauses.append(
                db.and_(Exam.section_id == row.section_id, Exam.subject_id == row.subject_id)
            )

    return db.or_(*clauses) if clauses else None


def _assert_exam_access(emp: Employee, exam_id: int) -> Exam:
    """Return Exam if teacher is authorized for its (section, subject) pair; abort otherwise.

    Homeroom teachers have access to every exam in their section.
    Subject-assigned teachers must have an explicit (section_id, subject_id) row in
    teacher_subjects.  This prevents a teacher who teaches Math in Section 1 from
    viewing or entering results for English exams in Section 1.
    """
    exam = db.session.get(Exam, exam_id)
    if not exam or exam.school_id != emp.school_id:
        abort(404)
    # Homeroom teacher: full access to all exams in the section.
    homeroom_ids = {s.id for s in emp.sections_managed}
    if exam.section_id in homeroom_ids:
        return exam
    # Subject-assigned section: require an explicit (section_id, subject_id) pair.
    row = db.session.execute(
        select(teacher_subjects.c.employee_id).where(
            db.and_(
                teacher_subjects.c.employee_id == emp.id,
                teacher_subjects.c.section_id  == exam.section_id,
                teacher_subjects.c.subject_id  == exam.subject_id,
            )
        ).limit(1)
    ).fetchone()
    if not row:
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
    exam_filter = _teacher_exam_filter(emp)
    today       = date.today()

    sections_count = len(section_ids)
    student_count  = (Student.query
                      .filter(Student.section_id.in_(section_ids))
                      .filter_by(status='active')
                      .count()) if section_ids else 0
    upcoming_14d   = (
        Exam.query
        .filter(exam_filter)
        .filter(Exam.exam_date >= today)
        .filter(Exam.exam_date <= today + timedelta(days=14))
        .count()
    ) if exam_filter is not None else 0

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
                    .options(joinedload(Section.grade))
                    .filter(Section.id.in_(section_ids))
                    .order_by(Section.id)
                    .all()) if section_ids else []

    # P1: batch what was 3 queries per section (student count, subject-pair
    # lookup, subject rows) into 3 queries total for the whole list. Every
    # batch is bound to this teacher's OWN section ids (server-derived) and,
    # for subjects, to this teacher's OWN teacher_subjects assignment rows —
    # identical scope to the per-section queries replaced.
    student_counts: dict[int, int] = {}
    subs_by_section: dict[int, list[dict]] = {}
    if sections:
        sec_ids = [sec.id for sec in sections]
        student_counts = dict(
            db.session.query(Student.section_id, func.count(Student.id))
            .filter(Student.section_id.in_(sec_ids), Student.status == 'active')
            .group_by(Student.section_id)
            .all()
        )
        pair_rows = db.session.execute(
            select(teacher_subjects.c.section_id, teacher_subjects.c.subject_id)
            .where(
                teacher_subjects.c.employee_id == emp.id,
                teacher_subjects.c.section_id.in_(sec_ids),
            )
            .distinct()
        ).fetchall()
        subj_ids = {r.subject_id for r in pair_rows}
        subj_map = {
            subj.id: subj
            for subj in Subject.query.filter(Subject.id.in_(subj_ids)).all()
        } if subj_ids else {}
        for r in pair_rows:
            subj = subj_map.get(r.subject_id)
            if subj is not None:
                subs_by_section.setdefault(r.section_id, []).append(
                    {'id': subj.id, 'name': subj.name})
        for subj_list in subs_by_section.values():
            subj_list.sort(key=lambda d: d['name'])   # same order as _section_subjects

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
                'student_count': student_counts.get(sec.id, 0),
                'is_homeroom':   sec.id in homeroom_ids,
                'subjects':      subs_by_section.get(sec.id, []),
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
        scope is now active for authenticated mobile requests (set_mobile_request_scope
        is called by jwt_required after token validation); the explicit column
        filters here are defence-in-depth.
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
        # Explicit school_id guard — defence-in-depth. The ORM tenant scope is
        # active for authenticated mobile requests (set_mobile_request_scope);
        # this explicit filter must not be removed.
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

    # P2: ok_etag adds HTTP validation — clients that send If-None-Match get a
    # bodyless 304 when the timetable is unchanged; all others receive the
    # exact same 200 payload as before.
    return ok_etag(
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
        return another teacher's or another school's rows. The explicit filters
        are defence-in-depth alongside the ORM tenant scope that is now active
        for authenticated mobile requests.

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

    exam_filter = _teacher_exam_filter(emp)
    if exam_filter is None:
        return ok(count=0, exams=[])

    today = date.today()
    q     = Exam.query.filter(exam_filter)

    if request.args.get('upcoming'):
        q = q.filter(Exam.exam_date >= today)
    elif request.args.get('past'):
        q = q.filter(Exam.exam_date < today)

    limit  = min(int(request.args.get('limit', 50)), 100)
    offset = max(int(request.args.get('offset', 0)), 0)
    # P1: eager-load the relationships the serializer touches in the same
    # statement (school criteria still applies to every joined entity), and
    # compute all result counts in ONE grouped query instead of one COUNT per
    # exam. The grouped query runs under the same ORM tenant scope as the
    # per-exam counts it replaces, restricted to this page's exam ids — which
    # already passed the teacher's section/subject access filter.
    exams = (q.options(
                 joinedload(Exam.subject),
                 joinedload(Exam.section).joinedload(Section.grade),
                 joinedload(Exam.exam_type),
             )
             .order_by(Exam.exam_date.desc())
             .offset(offset).limit(limit).all())

    exam_ids = [e.id for e in exams]
    result_counts = dict(
        db.session.query(ExamResult.exam_id, func.count(ExamResult.id))
        .filter(ExamResult.exam_id.in_(exam_ids))
        .group_by(ExamResult.exam_id)
        .all()
    ) if exam_ids else {}

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
                'result_count':    result_counts.get(e.id, 0),
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

    # Explicit school + year + section + date scope. The ORM tenant scope is
    # now active for authenticated mobile requests; these explicit filters
    # are defence-in-depth.
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

    # Validate the (section_id, subject_id) pair against the teacher's assignments.
    # Homeroom teachers can create exams for any school-scoped subject in their section.
    # Subject-assigned teachers must have an explicit assignment for the pair.
    _cr_homeroom_ids = {s.id for s in emp.sections_managed}
    _cr_subj_rows = db.session.execute(
        select(teacher_subjects.c.section_id, teacher_subjects.c.subject_id).where(
            teacher_subjects.c.employee_id == emp.id
        )
    ).fetchall()
    _cr_all_sections = _cr_homeroom_ids | {r.section_id for r in _cr_subj_rows}

    if section_id not in _cr_all_sections:
        return err('forbidden — section not assigned to you', 403)
    if section_id not in _cr_homeroom_ids:
        if (section_id, subject_id) not in {(r.section_id, r.subject_id) for r in _cr_subj_rows}:
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

    # In-app Notification + FCM push to parents of active students in this section.
    # P0: queued to the background dispatcher — the fan-out (per-parent rows +
    # FCM HTTPS calls) no longer blocks this request. _notify_new_exam() is
    # documented context-independent (bypass_tenant_scope=True + explicit
    # school_id on every query), so it is safe in a background thread where no
    # ORM tenant scope exists. Only primitives cross the thread boundary; the
    # Exam is re-loaded inside the task with an explicit school_id equality.
    # Best-effort: a notification failure must never fail the API response.
    _exam_id_for_log = new_exam.id  # PK retained after commit — safe to read
    try:
        from app.services import async_dispatch
        async_dispatch.submit(_notify_new_exam_bg, _exam_id_for_log, emp.school_id)
    except Exception:
        import logging as _mlog
        _mlog.getLogger('mecha.mobile').exception(
            '[mobile-exam] _notify_new_exam dispatch error '
            'exam_id=%s school_id=%s section_id=%s',
            _exam_id_for_log,
            getattr(new_exam, 'school_id', None),
            getattr(new_exam, 'section_id', None),
        )

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
    # include_all_years=True ensures results are visible even when the exam's
    # academic_year_id differs from the current active year (e.g. after a year
    # rollover); the exam_id filter already uniquely identifies the correct exam.
    results = (
        ExamResult.query
        .execution_options(include_all_years=True)
        .filter_by(exam_id=exam.id)
        .order_by(ExamResult.marks.desc())
        .all()
    )

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
                'grade_letter': r.grade_letter,   # matches POST response field name
                'grade':        r.grade_letter,   # backward-compat alias
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


# ─── Background notification wrappers (P0) ────────────────────────────────────
#
# Both wrappers run on the async_dispatch background thread pool, where there is
# NO request context and therefore NO implicit ORM tenant scope. They must never
# trust an id alone: the source record is re-loaded with an explicit school_id
# equality filter (bypass_tenant_scope=True + include_all_years=True so the
# lookup is deterministic in the scope-less thread), and the delegated helpers
# are the existing context-independent implementations whose queries all carry
# their own explicit school/ownership filters.

def _notify_new_exam_bg(exam_id: int, school_id: int) -> None:
    """Background wrapper for grades._notify_new_exam(). Never raises."""
    import logging as _mlog
    try:
        from app.blueprints.grades import _notify_new_exam
        exam = (Exam.query
                .execution_options(bypass_tenant_scope=True, include_all_years=True)
                .filter_by(id=exam_id, school_id=school_id)
                .first())
        if exam is None:
            return
        _notify_new_exam(exam)
    except Exception:
        _mlog.getLogger('mecha.mobile').exception(
            '[mobile-exam] background _notify_new_exam failed exam_id=%s school_id=%s',
            exam_id, school_id,
        )


def _notify_homework_bg(homework_id: int, school_id: int) -> None:
    """Background wrapper for homework._notify_section_parents(). Never raises."""
    import logging as _mlog
    try:
        from app.blueprints.homework import _notify_section_parents
        hw = (Homework.query
              .execution_options(bypass_tenant_scope=True, include_all_years=True)
              .filter_by(id=homework_id, school_id=school_id)
              .first())
        if hw is None:
            return
        _notify_section_parents(hw, school_id)
    except Exception:
        _mlog.getLogger('mecha.mobile').exception(
            '[mobile-hw] background _notify_section_parents failed hw_id=%s school_id=%s',
            homework_id, school_id,
        )


# ─── Grade notification helper ───────────────────────────────────────────────

def _notify_grade_results_mobile(
    exam_id: int,
    school_id: int,
    subject_id: int | None,
    exam_name: str,
    students: list,
) -> tuple[int, int]:
    """Create in-app Notification rows + queue FCM pushes for each parent of
    every graded student.

    P0 restructure — the previous version committed one Notification row per
    parent and performed every FCM HTTPS round-trip inline, blocking the
    request thread for the whole fan-out. Now:
      1. Parent links are resolved with the same explicit ownership filters as
         before (parent_students junction + User.school_id equality + is_active).
      2. ALL Notification rows are inserted in ONE commit — in-request,
         DB-local, fast — so the in-app feed is durable before the response.
         This commit runs AFTER the grade commit and can never roll it back.
      3. FCM delivery is handed to the background dispatcher
         (app/services/async_dispatch.py) as primitive tuples only. Background
         threads have NO request context and therefore no implicit ORM tenant
         scope — per-user isolation is enforced inside send_push_to_user()
         (device tokens are resolved by user_id).

    Isolation guarantees (unchanged from the previous version):
    - bypass_tenant_scope=True + explicit school_id equality on the User query;
      a parent from another school can never be targeted.
    - parent_students is queried with Core SELECT (not ORM) to avoid scope issues.
    - Each Notification row carries the parent's own school_id + target_user_id.

    Never raises. Returns (fcm_queued_count, 0) — delivery results are logged
    asynchronously by the FCM batch task, not returned here.
    """
    import logging as _logging
    _log = _logging.getLogger('mecha.mobile.grade_notify')

    try:
        from app.services import async_dispatch
        from app.services.fcm_service import (
            is_enabled as _fcm_enabled,
            send_push_batch,
        )

        # Primitive capture first — never carry ORM objects past a commit or
        # into the background task.
        student_ids = [s.id for s in (students or [])]
        if not student_ids:
            return 0, 0

        title = 'درجة جديدة'
        body  = f'تم رصد درجة جديدة في {exam_name}.'

        notif_rows: list[Notification] = []
        push_items: list[tuple] = []

        for student_id in student_ids:
            # Resolve linked parent user IDs via the junction table.
            # Core SELECT — not affected by ORM with_loader_criteria.
            raw_rows = db.session.execute(
                select(parent_students.c.user_id).where(
                    parent_students.c.student_id == student_id
                )
            ).fetchall()
            raw_parent_ids = [r[0] for r in raw_rows]

            if not raw_parent_ids:
                _log.warning(
                    '[grade-notify] exam_id=%s student_id=%s — no linked parents',
                    exam_id, student_id,
                )
                continue

            # Only active parents who belong to this school.
            # bypass_tenant_scope=True + explicit school_id guard — cross-school
            # notifications are prevented by the school_id equality filter.
            parent_records = (
                User.query
                .execution_options(bypass_tenant_scope=True)
                .with_entities(User.id, User.school_id)
                .filter(
                    User.id.in_(raw_parent_ids),
                    User.school_id == school_id,
                    User.is_active.is_(True),
                )
                .all()
            )

            _log.warning(
                '[grade-notify] exam_id=%s student_id=%s '
                'linked_parents=%d active_in_school=%d',
                exam_id, student_id, len(raw_parent_ids), len(parent_records),
            )
            if not parent_records:
                continue

            fcm_data = {
                'type':       'grade',
                'screen':     'grades',
                'route':      '/parent/grades',
                'exam_id':    str(exam_id),
                'subject_id': str(subject_id or ''),
                'student_id': str(student_id),
                'ntype':      'grade',
            }

            for parent_id, parent_school_id in parent_records:
                notif_rows.append(Notification(
                    school_id      = parent_school_id,
                    title          = title,
                    body           = body,
                    ntype          = 'grade',
                    target_user_id = parent_id,
                    created_by     = None,
                ))
                push_items.append((parent_id, title, body, fcm_data))

        if not notif_rows:
            return 0, 0

        # Single batch commit for the in-app feed rows. The grades commit
        # already happened in the caller — a failure here is logged and never
        # affects it; FCM pushes are still queued (device notification is
        # independent of the in-app row).
        try:
            db.session.add_all(notif_rows)
            db.session.commit()
        except Exception:
            db.session.rollback()
            _log.exception(
                '[grade-notify] Notification batch commit FAILED exam_id=%s '
                'rows=%d — rolled back (grades unaffected)',
                exam_id, len(notif_rows),
            )

        if push_items and _fcm_enabled():
            async_dispatch.submit(send_push_batch, push_items)

        _log.warning(
            '[grade-notify] exam_id=%s school_id=%s notif_rows=%d fcm_queued=%d',
            exam_id, school_id, len(notif_rows), len(push_items),
        )
        return len(push_items), 0

    except Exception:
        import logging as _fallback_log
        _fallback_log.getLogger('mecha.mobile.grade_notify').exception(
            '[grade-notify] UNHANDLED ERROR exam_id=%s school_id=%s',
            exam_id, school_id,
        )
        return 0, 0


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
          {"student_id": 123, "marks": 88.5, "notes": ""},
          ...
        ]
      }

    Fields:
      student_id   int     required — DB primary key of the Student row
      marks        number  required — also accepted as "score" (Flutter alias)
      notes        string  optional — also accepted as "note" (Flutter alias)
      grade_letter string  IGNORED — always calculated server-side

    Security:
      - Teacher identity and school are resolved from the JWT via _get_employee().
        No school_id, teacher_id, section_id, or academic_year_id is trusted
        from the client payload.
      - The exam's section must belong to the teacher's assigned sections.
      - Only active students in the exam's section are accepted.
      - Marks are validated against the exam's max_marks.
      - grade_letter is always calculated server-side (calculate_grade_letter).

    Upsert behaviour:
      - Creates a new ExamResult when no row exists for (exam_id, student_id).
      - Updates the existing row when one already exists.
      - Duplicate-key conflicts are prevented by the pre-fetched existing_map
        which uses include_all_years=True to handle exams from any year.
      - Only entries whose marks or notes actually differ are counted as
        "updated" and trigger parent notifications.

    Notifications:
      - After a successful commit, one in-app Notification row is written and
        one FCM push is sent per linked active parent for every student whose
        result was created or changed.
      - Unchanged entries (same marks + notes) do not trigger notifications.
      - Notification failures never fail the API response.

    Ranks:
      - After every successful commit, ranks are recalculated for all results
        on the same exam (descending marks order, 1-based), matching the web route.

    Response (200):
      {
        "ok": true,
        "saved": 3,
        "created": 2,
        "updated": 1,
        "unchanged": 0,
        "errors": [],
        "results": [
          {
            "student_id": 123, "student_name": "...",
            "marks": 88.5, "grade_letter": "B+", "grade": "B+",
            "is_pass": true, "rank": 1, "notes": null
          }
        ]
      }

    Error responses:
      400  no_employee_profile         no Employee linked to this JWT user
      400  results must be a non-empty array
      400  no valid results to save    all submitted entries failed validation
      403  exam belongs to a different teacher's section
      404  exam not found or not in this school
      500  database_error              commit or rank-update failed; rolled back
    """
    import logging as _log
    _logger = _log.getLogger('mecha.mobile.grades')

    emp = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)
    exam = _assert_exam_access(emp, exam_id)

    # Capture scalar attributes before any later commit expires the ORM object.
    exam_id_val      = exam.id
    exam_school_id   = exam.school_id
    exam_section_id  = exam.section_id
    exam_subject_id  = exam.subject_id
    exam_year_id     = exam.academic_year_id
    exam_name_val    = exam.exam_name or exam.display_name

    try:
        max_marks_dec  = Decimal(str(exam.max_marks))
        pass_marks_dec = Decimal(str(exam.pass_marks))
    except (InvalidOperation, TypeError):
        _logger.error(
            '[mobile-grades] invalid marks config exam_id=%s max=%r pass=%r',
            exam_id_val, exam.max_marks, exam.pass_marks,
        )
        return err('exam_has_invalid_marks_configuration', 500)

    payload = request.get_json(silent=True) or {}
    entries = payload.get('results', [])
    if not isinstance(entries, list) or not entries:
        return err('results must be a non-empty array')

    _logger.warning(
        '[mobile-grades] START exam_id=%s school_id=%s '
        'teacher_user_id=%s emp_id=%s submitted=%d',
        exam_id_val, exam_school_id,
        g.mobile_user.id, emp.id, len(entries),
    )

    # Allowed student set — active students in the exam's section, school-scoped.
    # Student is NOT year-scoped so this query always returns current enrollment
    # regardless of which academic year the exam belongs to.
    section_students = Student.query.filter_by(
        section_id=exam_section_id, status='active'
    ).all()
    allowed_ids   = {s.id for s in section_students}
    student_by_id = {s.id: s for s in section_students}

    _logger.warning(
        '[mobile-grades] exam_id=%s section_id=%s active_students_in_section=%d',
        exam_id_val, exam_section_id, len(allowed_ids),
    )

    # Pre-fetch ALL existing results for this exam.
    # include_all_years=True removes the year-scope filter so that results from
    # exams whose academic_year_id differs from the current active year are still
    # found and updated instead of triggering a duplicate-key IntegrityError.
    existing_map: dict[int, ExamResult] = {
        r.student_id: r
        for r in (
            ExamResult.query
            .execution_options(include_all_years=True)
            .filter_by(exam_id=exam_id_val)
            .all()
        )
    }

    saved     = 0   # total entries that passed validation (created + updated + unchanged)
    created   = 0
    updated   = 0
    unchanged = 0
    errors: list[dict] = []
    graded_student_ids: list[int] = []   # students whose result actually changed

    # Capture user id now — g.mobile_user.id is the PK, never expired.
    entered_by_id = g.mobile_user.id

    for entry in entries:
        raw_sid = entry.get('student_id')

        # Coerce student_id to int — Flutter may serialise integers as strings
        # or as JSON numbers parsed to Dart doubles (both are safe to int()).
        try:
            sid = int(raw_sid) if raw_sid is not None else None
        except (TypeError, ValueError):
            errors.append({
                'student_id': raw_sid,
                'error': 'invalid_student_id_format',
                'reason': f'expected integer, got {type(raw_sid).__name__}',
            })
            _logger.warning(
                '[mobile-grades] REJECT exam_id=%s student_id=%r — invalid type',
                exam_id_val, raw_sid,
            )
            continue

        if sid is None or sid not in allowed_ids:
            errors.append({
                'student_id': raw_sid,
                'error': 'not_in_section',
                'reason': 'student is not active in this exam\'s section or does not exist',
            })
            _logger.warning(
                '[mobile-grades] REJECT exam_id=%s student_id=%r — not_in_section',
                exam_id_val, raw_sid,
            )
            continue

        # Accept 'score' (Flutter spec) or 'marks' (legacy)
        raw_marks = entry.get('score') if entry.get('score') is not None else entry.get('marks')
        if raw_marks is None:
            errors.append({'student_id': sid, 'error': 'marks_required'})
            _logger.warning(
                '[mobile-grades] REJECT exam_id=%s student_id=%s — marks_required',
                exam_id_val, sid,
            )
            continue

        try:
            marks_dec = Decimal(str(raw_marks))
        except (InvalidOperation, TypeError, ValueError):
            errors.append({
                'student_id': sid,
                'error': 'invalid_marks_value',
                'reason': f'cannot convert {raw_marks!r} to a number',
            })
            _logger.warning(
                '[mobile-grades] REJECT exam_id=%s student_id=%s — invalid_marks %r',
                exam_id_val, sid, raw_marks,
            )
            continue

        if marks_dec < Decimal('0') or marks_dec > max_marks_dec:
            errors.append({
                'student_id': sid,
                'error': 'marks_out_of_range',
                'reason': f'must be between 0 and {max_marks_dec}',
            })
            _logger.warning(
                '[mobile-grades] REJECT exam_id=%s student_id=%s — marks_out_of_range %s max=%s',
                exam_id_val, sid, marks_dec, max_marks_dec,
            )
            continue

        # Server-side grade calculation (never trust client-supplied grade_letter).
        grade_letter = calculate_grade_letter(float(marks_dec), float(max_marks_dec))
        is_pass      = marks_dec >= pass_marks_dec

        # Accept 'note' (Flutter spec, singular) or 'notes' (plural)
        entry_notes = entry.get('note') if entry.get('note') is not None else entry.get('notes')

        existing = existing_map.get(sid)
        if existing:
            old_marks = (
                Decimal(str(existing.marks)).quantize(Decimal('0.01'))
                if existing.marks is not None else None
            )
            new_marks = marks_dec.quantize(Decimal('0.01'))
            new_notes = entry_notes if entry_notes is not None else existing.notes
            actually_changed = (old_marks != new_marks) or (existing.notes != new_notes)

            existing.marks        = marks_dec
            existing.grade_letter = grade_letter
            existing.is_pass      = is_pass
            existing.notes        = new_notes
            existing.entered_by   = entered_by_id

            if actually_changed:
                updated += 1
                graded_student_ids.append(sid)
                _logger.warning(
                    '[mobile-grades] UPDATE exam_id=%s student_id=%s '
                    'marks=%s→%s grade=%s',
                    exam_id_val, sid, old_marks, new_marks, grade_letter,
                )
            else:
                unchanged += 1
                _logger.warning(
                    '[mobile-grades] UNCHANGED exam_id=%s student_id=%s marks=%s',
                    exam_id_val, sid, new_marks,
                )
        else:
            new_result = ExamResult(
                exam_id          = exam_id_val,
                student_id       = sid,
                school_id        = exam_school_id,
                academic_year_id = exam_year_id,
                marks            = marks_dec,
                grade_letter     = grade_letter,
                is_pass          = is_pass,
                notes            = entry_notes,
                entered_by       = entered_by_id,
            )
            db.session.add(new_result)
            created += 1
            graded_student_ids.append(sid)
            _logger.warning(
                '[mobile-grades] INSERT exam_id=%s student_id=%s marks=%s grade=%s',
                exam_id_val, sid, marks_dec, grade_letter,
            )

        saved += 1

    _logger.warning(
        '[mobile-grades] PRE-COMMIT exam_id=%s '
        'created=%d updated=%d unchanged=%d rejected=%d',
        exam_id_val, created, updated, unchanged, len(errors),
    )

    # Guard: if nothing valid was submitted, don't commit and return an informative error.
    if created == 0 and updated == 0 and unchanged == 0:
        _logger.warning(
            '[mobile-grades] ABORT exam_id=%s — all %d entries rejected',
            exam_id_val, len(errors),
        )
        return err(
            f'no valid results to save — all {len(errors)} entries were rejected',
            400,
        )

    # Commit only when at least one result was created or updated.
    if created > 0 or updated > 0:
        try:
            db.session.commit()
            _logger.warning(
                '[mobile-grades] COMMIT OK exam_id=%s created=%d updated=%d',
                exam_id_val, created, updated,
            )
        except Exception:
            db.session.rollback()
            _logger.exception(
                '[mobile-grades] COMMIT FAILED exam_id=%s — rolled back',
                exam_id_val,
            )
            return err('database_error — changes were rolled back', 500)

        # Recalculate ranks for all results of this exam (mirrors web route).
        try:
            all_for_rank = (
                ExamResult.query
                .execution_options(include_all_years=True)
                .filter_by(exam_id=exam_id_val)
                .order_by(ExamResult.marks.desc())
                .all()
            )
            for rank_pos, res in enumerate(all_for_rank, 1):
                res.rank = rank_pos
            db.session.commit()
            _logger.warning(
                '[mobile-grades] RANKS UPDATED exam_id=%s total=%d',
                exam_id_val, len(all_for_rank),
            )
        except Exception:
            db.session.rollback()
            _logger.exception(
                '[mobile-grades] RANK UPDATE FAILED exam_id=%s '
                '(non-fatal, results already saved)',
                exam_id_val,
            )

    # Write in-app Notification rows (single commit) and QUEUE the FCM pushes to
    # the background dispatcher — the fan-out no longer blocks this request.
    # Best-effort — a notification failure must never fail the API response.
    fcm_queued = 0
    if graded_student_ids:
        try:
            graded_students = [
                student_by_id[s] for s in graded_student_ids if s in student_by_id
            ]
            fcm_queued, _ = _notify_grade_results_mobile(
                exam_id_val,
                exam_school_id,
                exam_subject_id,
                exam_name_val,
                graded_students,
            )
        except Exception:
            _logger.exception(
                '[mobile-grades] NOTIFICATION DISPATCH FAILED exam_id=%s',
                exam_id_val,
            )

    _logger.warning(
        '[mobile-grades] DONE exam_id=%s '
        'created=%d updated=%d unchanged=%d rejected=%d '
        'notified_students=%d fcm_queued=%d',
        exam_id_val,
        created, updated, unchanged, len(errors),
        len(graded_student_ids), fcm_queued,
    )

    # Return the full current results list so Flutter can refresh immediately
    # without a second GET request.  include_all_years=True ensures visibility
    # for exams from non-current years (same flag used by existing_map above).
    all_results_now = (
        ExamResult.query
        .execution_options(include_all_years=True)
        .filter_by(exam_id=exam_id_val)
        .order_by(ExamResult.marks.desc())
        .all()
    )

    def _result_row(r: ExamResult) -> dict:
        s = student_by_id.get(r.student_id)
        return {
            'student_id':   r.student_id,
            'student_name': s.full_name if s else '?',
            'marks':        float(r.marks) if r.marks is not None else None,
            'grade_letter': r.grade_letter,
            'grade':        r.grade_letter,   # alias for Flutter compatibility
            'is_pass':      r.is_pass,
            'rank':         r.rank,
            'notes':        r.notes,
        }

    return ok(
        saved     = saved,
        created   = created,
        updated   = updated,
        unchanged = unchanged,
        errors    = errors,
        results   = [_result_row(r) for r in all_results_now],
    )


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

    # Apply the teacher's account creation datetime as a cutoff for broadcast
    # notifications (target_user_id IS NULL). This prevents a newly created
    # teacher from inheriting historical role-broadcast or NULL-target
    # notifications from before their account existed. Direct notifications
    # (target_user_id == user.id) are unaffected by the cutoff.
    # Explicit school_id guard is defence-in-depth alongside the ORM scope.
    q     = (Notification.query
             .filter(
                 Notification.school_id == user.school_id,
                 notification_visible_to(user, cutoff_dt=user.created_at),
             )
             .order_by(Notification.created_at.desc()))
    total = q.count()
    rows  = q.offset(offset).limit(limit).all()

    # P1: read receipts for THIS PAGE only (was: every receipt the user ever
    # created — unbounded growth). Scope: this user's receipts, this page's
    # notification ids.
    page_ids = [n.id for n in rows]
    read_ids = {
        r[0]
        for r in NotificationRead.query
        .with_entities(NotificationRead.notification_id)
        .filter(NotificationRead.user_id == user.id,
                NotificationRead.notification_id.in_(page_ids))
        .all()
    } if page_ids else set()

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
    # Always resolve through photo_url so that when PRIVATE_UPLOADS_ENABLED is on
    # a stored full Supabase URL (private bucket) is re-signed to a /media-proxy
    # URL the app can open. Returning it raw would 400 against the private bucket.
    # photo_url still returns http(s) values unchanged when the feature is off.
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

    # Validate the (section_id, subject_id) pair against the teacher's assignments.
    # Homeroom teachers can create homework for any subject in their section.
    # Subject-assigned teachers must have an explicit assignment for the pair.
    _hw_homeroom_ids = {s.id for s in emp.sections_managed}
    _hw_subj_rows = db.session.execute(
        select(teacher_subjects.c.section_id, teacher_subjects.c.subject_id).where(
            teacher_subjects.c.employee_id == emp.id
        )
    ).fetchall()
    _hw_all_sections = _hw_homeroom_ids | {r.section_id for r in _hw_subj_rows}

    if section_id not in _hw_all_sections:
        return err('forbidden — section not assigned to you', 403)
    if section_id not in _hw_homeroom_ids:
        if (section_id, subject_id) not in {(r.section_id, r.subject_id) for r in _hw_subj_rows}:
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

    # FCM + in-app Notification rows to parents of students in this section.
    # P0: queued to the background dispatcher — the per-parent fan-out no longer
    # blocks this request. The Homework row is re-loaded inside the task with an
    # explicit school_id equality; only primitives cross the thread boundary.
    # Best-effort: a notification failure must never fail the API response.
    try:
        from app.services import async_dispatch
        async_dispatch.submit(_notify_homework_bg, hw.id, emp.school_id)
    except Exception:
        import logging as _log
        _log.getLogger('mecha.mobile').exception(
            '[mobile-hw] notification dispatch failed hw_id=%s', hw.id)

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

    # Validate the (section_id, subject_id) pair against the teacher's assignments.
    # Homeroom teachers can update homework for any subject in their section.
    # Subject-assigned teachers must have an explicit assignment for the pair.
    _upd_homeroom_ids = {s.id for s in emp.sections_managed}
    _upd_subj_rows = db.session.execute(
        select(teacher_subjects.c.section_id, teacher_subjects.c.subject_id).where(
            teacher_subjects.c.employee_id == emp.id
        )
    ).fetchall()
    _upd_all_sections = _upd_homeroom_ids | {r.section_id for r in _upd_subj_rows}

    if section_id not in _upd_all_sections:
        return err('forbidden — section not assigned to you', 403)
    if section_id not in _upd_homeroom_ids:
        if (section_id, subject_id) not in {(r.section_id, r.subject_id) for r in _upd_subj_rows}:
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

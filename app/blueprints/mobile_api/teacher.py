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
GET  /teacher/grades                   /teacher/exams window + each exam's results in ONE request
POST /teacher/exams/<id>/results       bulk-upsert grade entries (accepts score/note or marks/notes)
GET  /teacher/notifications            notifications feed (paginated)
GET  /teacher/institute/sessions       my institute group sessions for a date range
GET  /teacher/institute/sessions/open  open ONE occurrence (group_id + date + start)
POST /teacher/institute/sessions/<id>/attendance   submit/correct attendance
GET  /teacher/institute/groups         my institute study groups + weekly slots
GET  /teacher/institute/groups/<id>/students   active roster of one of my groups
GET  /teacher/institute/students/<id>  student profile (only my groups' students)
GET  /teacher/institute/exams          exams of my institute groups (paginated)
POST /teacher/institute/exams          create an exam for one of my groups
GET  /teacher/institute/exams/<id>     exam + editable roster + historical results
POST /teacher/institute/exams/<id>/results   atomic batch grade entry
POST /teacher/institute/homework       create homework for one of my groups
PUT  /teacher/institute/homework/<id>  update my institute homework (PATCH too)
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.models import (
    db,
    AcademicYear,
    Employee,
    EmployeeAttendance,
    Exam,
    ExamResult,
    ExamType,
    Homework,
    InstituteAttendanceRecord,
    InstituteAttendanceSession,
    InstituteInstructorAttendance,
    InstituteGroupEnrollment,
    InstituteStudyGroup,
    Notification,
    NotificationRead,
    Schedule,
    School,
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
from .utils import jwt_required, role_required, ok, ok_etag, err, photo_url, page_args
from app.utils.student_display_photo import student_display_value
from app.utils.employee_display_photo import employee_display_value
from app.services import institute_attendance as inst_att
from app.utils.institute_groups import (active_enrollment_count_map, active_roster,
                                        active_roster_students,
                                        eligible_groups_for_user,
                                        historical_result_rows, instructor_groups,
                                        instructor_can_access_student,
                                        resolve_eligible_group)


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


def _section_subjects(emp: Employee, section_id: int) -> tuple[Section | None, list[Subject]]:
    """The ONE rule for which subjects this teacher may use in ONE section —
    shared by the subject picker and exam creation so they can never disagree.

    Returns (section, subjects), or (None, []) when the section is not this
    teacher's (or not in this school / active year): callers answer with the
    same response whether it is foreign or nonexistent.

      * subject-assigned section → exactly the teacher_subjects rows for
        (this employee, this section);
      * homeroom section (Section.teacher_id == emp.id) → those rows PLUS the
        subjects of the section's own grade (Subject.grade_id == grade_id).
        Subjects with a NULL grade_id are reachable only via an explicit row,
        never through homeroom.

    Three small indexed queries, set-based. Section and Subject run under the
    mobile ORM scope (school + active year) and also pin school_id explicitly.
    """
    section = (Section.query
               .filter(Section.id == section_id,
                       Section.school_id == emp.school_id)
               .first())
    if section is None:
        return None, []

    pair_ids = [r.subject_id for r in db.session.execute(
        select(teacher_subjects.c.subject_id).where(
            teacher_subjects.c.employee_id == emp.id,
            teacher_subjects.c.section_id == section.id,
        ).distinct()
    ).fetchall()]
    is_homeroom = section.teacher_id == emp.id
    if not is_homeroom and not pair_ids:
        return None, []

    allowed = []
    if pair_ids:
        allowed.append(Subject.id.in_(pair_ids))
    if is_homeroom:
        allowed.append(Subject.grade_id == section.grade_id)
    subjects = (Subject.query
                .filter(Subject.school_id == emp.school_id, db.or_(*allowed))
                .order_by(Subject.name)
                .all())
    return section, subjects


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
            'photo':       photo_url(employee_display_value(emp)),   # display copy, else original
            'photo_url':   photo_url(employee_display_value(emp)),
            'hire_date':   emp.hire_date.isoformat() if emp.hire_date else None,
            'status':      emp.status,
            'school_id':   emp.school_id,
            'school_name': school.school_name if school else None,
            'role':        g.mobile_user.role.name if g.mobile_user.role else None,
            'can_record_institute_attendance': bool(
                emp.can_record_institute_attendance),
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

    Optional ?section_id=<id> — only the subjects this teacher may use in THAT
    section (see _section_subjects; the same rule POST /teacher/exams enforces).
    404 when the section is not this teacher's. Without it (or blank) the
    response is unchanged.

    Response:
      { "ok": true, "subjects": [ {"id": 2, "name": "الرياضيات"} ] }
    """
    emp = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)

    raw_section = (request.args.get('section_id') or '').strip()
    if not raw_section:
        return ok(subjects=_teacher_subjects(emp))
    try:
        section_id = int(raw_section)
    except ValueError:
        return err('invalid section_id')

    section, subjects = _section_subjects(emp, section_id)
    if section is None:
        return err('section_not_found', 404)
    return ok(subjects=[{'id': s.id, 'name': s.name} for s in subjects])


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
                'photo':      photo_url(student_display_value(s)),
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
            'photo':           photo_url(student_display_value(student)),
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

    # Institute staff attendance is recorded per lesson in
    # InstituteInstructorAttendance, not in the daily EmployeeAttendance table.
    # Keep the institute administration workflow authoritative and expose only
    # this authenticated employee's rows from this institute and current year.
    if emp.school and emp.school.is_institute:
        institute_records = (
            db.session.query(InstituteInstructorAttendance,
                             InstituteAttendanceSession)
            .join(
                InstituteAttendanceSession,
                (InstituteAttendanceSession.id
                 == InstituteInstructorAttendance.session_id)
                & (InstituteAttendanceSession.school_id == emp.school_id),
            )
            .filter(
                InstituteInstructorAttendance.school_id == emp.school_id,
                InstituteInstructorAttendance.employee_id == emp.id,
                InstituteAttendanceSession.academic_year_id == year.id,
            )
            .order_by(InstituteAttendanceSession.session_date.desc(),
                      InstituteAttendanceSession.start_time.desc(),
                      InstituteInstructorAttendance.id.desc())
            .all()
        )

        out = []
        present_days = late_days = absent_days = 0
        for rec, session in institute_records:
            status = _emp_att_status(rec.status)
            if status == 'present':
                present_days += 1
            elif status == 'late':
                late_days += 1
            elif status == 'absent':
                absent_days += 1
            out.append({
                'id':        rec.id,
                'date':      session.session_date.isoformat(),
                'check_in':  None,
                'check_out': None,
                'status':    status,
                'notes':     rec.notes or '',
            })

        return ok(
            records=out,
            summary={
                'total_days':   len(institute_records),
                'present_days': present_days,
                'late_days':    late_days,
                'absent_days':  absent_days,
            },
        )

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

def _teacher_exam_window(emp: Employee, today: date) -> list[Exam] | None:
    """The teacher's exam page, shared by /teacher/exams and /teacher/grades so
    both always return the same exam population.

    Access comes only from _teacher_exam_filter (homeroom sections + explicit
    (section, subject) assignments) under the ORM school/year scope. Honours
    ?upcoming / ?past and ?limit / ?offset (default 50, max 100). Returns None
    when the teacher has no access at all.

    P1: the relationships the serializers touch are eager-loaded in the same
    statement (school criteria still applies to every joined entity).
    """
    exam_filter = _teacher_exam_filter(emp)
    if exam_filter is None:
        return None

    q = Exam.query.filter(exam_filter)
    if request.args.get('upcoming'):
        q = q.filter(Exam.exam_date >= today)
    elif request.args.get('past'):
        q = q.filter(Exam.exam_date < today)

    limit, offset = page_args(default_limit=50, max_limit=100)
    return (q.options(
                joinedload(Exam.subject),
                joinedload(Exam.section).joinedload(Section.grade),
                joinedload(Exam.exam_type),
            )
            .order_by(Exam.exam_date.desc())
            .offset(offset).limit(limit).all())


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

    today = date.today()
    exams = _teacher_exam_window(emp, today)
    if exams is None:
        return ok(count=0, exams=[])

    # P1: all result counts in ONE grouped query instead of one COUNT per
    # exam. The grouped query runs under the same ORM tenant scope as the
    # per-exam counts it replaces, restricted to this page's exam ids — which
    # already passed the teacher's section/subject access filter.
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
        "pass_marks":   50,              optional — raw points, 0..max_marks;
                                         default max_marks * 0.5
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
    pass_marks   = payload.get('pass_marks')   # None → max_marks * 0.5 below
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
    max_marks_dec = Decimal(str(max_marks_val))
    if not max_marks_dec.is_finite():
        return err('invalid value: max_score')

    # pass_marks is RAW POINTS on the same scale as max_marks (pass/fail is
    # marks >= pass_marks). An explicit value is kept exactly as sent and must
    # satisfy 0 <= pass_marks <= max_marks — same rule and messages as the
    # institute path. When omitted it defaults to half of THIS exam's maximum,
    # so a 10-point exam gets 5, not the 100-point default of 50.
    if pass_marks is None:
        pass_marks_dec = (max_marks_dec * Decimal('0.5')).quantize(Decimal('0.01'))
    else:
        try:
            if isinstance(pass_marks, bool):
                raise ValueError
            pass_marks_dec = Decimal(str(pass_marks))
            if not pass_marks_dec.is_finite():
                raise ValueError
        except (InvalidOperation, ValueError, TypeError):
            return err('max_score and pass_marks must be numbers')
        if pass_marks_dec < 0 or pass_marks_dec > max_marks_dec:
            return err('pass_marks must be between 0 and max_score')

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

    # Validate the (section_id, subject_id) pair with the SAME rule the subject
    # picker uses (_section_subjects): the exact teacher_subjects pair, or — in
    # the teacher's homeroom section — a subject of that section's own grade.
    # Ids must be JSON integers, as the previous set-membership checks required.
    if isinstance(section_id, bool) or not isinstance(section_id, int):
        return err('forbidden — section not assigned to you', 403)
    _cr_section, _cr_allowed = _section_subjects(emp, section_id)
    if _cr_section is None:
        return err('forbidden — section not assigned to you', 403)
    if (isinstance(subject_id, bool) or not isinstance(subject_id, int)
            or subject_id not in {s.id for s in _cr_allowed}):
        return err('forbidden — subject not assigned to you', 403)

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
        pass_marks       = pass_marks_dec,
        exam_name        = title,
        exam_type_id     = exam_type_id,
    )
    db.session.add(new_exam)
    db.session.commit()

    # In-app Notification + FCM push to parents of active students in this section.
    # P0: queued to the background dispatcher — the fan-out no longer blocks
    # this request. _notify_new_exam_bg() uses explicit school_id filters on
    # every query (bypass_tenant_scope=True), so it is safe in a background
    # thread where no ORM tenant scope exists; it batches the rows into one
    # commit and hands FCM to send_push_batch. Only primitives cross the thread
    # boundary; the Exam is re-loaded inside the task by id + school_id.
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


# ─── Grades (all my exams + results in one request) ───────────────────────────

@mobile_api_bp.route('/teacher/grades', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_grades():
    """
    Every exam of the /teacher/exams window with its entered results, so the
    Grades screen needs one request instead of one detail request per exam.
    Same query params and window as /teacher/exams (upcoming, past, limit,
    offset).

    Three bounded data queries regardless of exam/student/result counts:
      A. the exam window (same helper and access filter as /teacher/exams)
      B. ExamResult WHERE exam_id IN (A)            — include_all_years, as in
                                                      /teacher/exams/<id>
      C. active Student WHERE section_id IN (A)     — name resolution only

    Student names follow the detail route exactly: a result is named only from
    the ACTIVE students of that exam's own section, otherwise '?'. Result keys
    are deliberately explicit (student_name, never name/grade) because the
    client merges each result over its exam map.
    """
    emp = _get_employee()
    if not emp:
        return err('employee_profile_not_found', 404)

    exams = _teacher_exam_window(emp, date.today())
    if not exams:
        return ok(items=[])

    exam_ids    = [e.id for e in exams]
    section_ids = {e.section_id for e in exams}

    results_by_exam: dict[int, list] = {}
    for r in (ExamResult.query
              .execution_options(include_all_years=True)
              .with_entities(ExamResult.exam_id, ExamResult.student_id,
                             ExamResult.marks, ExamResult.notes)
              .filter(ExamResult.exam_id.in_(exam_ids))
              .order_by(ExamResult.exam_id, ExamResult.marks.desc(), ExamResult.id)
              .all()):
        results_by_exam.setdefault(r.exam_id, []).append(r)

    names = {
        (s.section_id, s.id): s.full_name
        for s in (Student.query
                  .with_entities(Student.id, Student.section_id, Student.full_name)
                  .filter(Student.section_id.in_(section_ids),
                          Student.status == 'active')
                  .all())
    }

    return ok(items=[
        {
            'exam': {
                'exam_title':   e.display_name,
                'subject_name': e.subject.name if e.subject else None,
                'grade_name':   e.section.grade.name if e.section and e.section.grade else None,
                'section_name': e.section.name if e.section else None,
                'exam_date':    e.exam_date.isoformat() if e.exam_date else None,
                'max_marks':    float(e.max_marks),
            },
            'results': [
                {
                    'student_id':   r.student_id,
                    'student_name': names.get((e.section_id, r.student_id), '?'),
                    'marks':        float(r.marks) if r.marks is not None else None,
                    'note':         r.notes,
                }
                for r in results_by_exam.get(e.id, [])
            ],
        }
        for e in exams
    ])


# ─── Background notification wrappers (P0) ────────────────────────────────────
#
# Both wrappers run on the async_dispatch background thread pool, where there is
# NO request context and therefore NO implicit ORM tenant scope and NO
# current_user. They must never trust an id alone: the source record is
# re-loaded with an explicit school_id equality filter (bypass_tenant_scope=True
# + include_all_years=True so the lookup is deterministic in the scope-less
# thread), and every recipient query below carries its own explicit school
# filter. Only primitives are passed in.
#
# Both delegate to _notify_parents_batched(), the same shape as
# _notify_grade_results_mobile(): a fixed number of set-based recipient queries,
# ONE Notification commit per job, and ONE send_push_batch task for the FCM
# fan-out — never a commit or a Firebase round-trip per parent inside this job.
# The web routes keep their own inline helpers (grades._notify_new_exam,
# homework._notify_homework_parents); this path no longer calls either.

# NOTE (P3): _notify_new_exam_bg and _notify_homework_bg are deliberately NOT
# registered as durable-queue tasks. They create in-app Notification rows, and
# the durable queue's crash-reclaim path is at-least-once — a worker dying
# mid-job could re-run the task and duplicate those rows for parents already
# processed. They stay on the in-process thread pool (at-most-once), where the
# only loss window is a queued task at worker recycle. Pure-push tasks
# (fcm.send_push_batch, chat.send_room_pushes) ARE durable: re-delivering a
# tray notification is harmless, losing it is not.

def _notify_parents_batched(*, school_id: int, student_ids, title: str, body: str,
                            ntype: str, fcm_data: dict | None,
                            active_parents_only: bool, created_by=None,
                            dedup_since=None, log_tag: str) -> None:
    """In-app Notification rows + one queued FCM batch for the linked parents of
    `student_ids`. One row and one push per PARENT: a parent with several
    children in the audience is notified once, carrying the lowest student_id
    of those children.

    Recipient queries (fixed count, independent of audience size):
      1. parent_students links for all students    (Core SELECT, one IN query)
      2. parent Users, explicit school_id equality (one IN query)
      3. existing rows for this event — only when dedup_since is given

    Isolation: a parent whose own account is not in `school_id` is skipped —
    never notified — and the flush-time tenant guard re-checks every row.

    Dedup identifies THIS event: an identical-text row counts as a duplicate
    only when it was created at/after `dedup_since` (the source record's own
    created_at), so an unrelated earlier homework/exam with the same text never
    suppresses this one.

    The rows are committed once; FCM work is handed to send_push_batch via
    async_dispatch (durable Redis queue when enabled) as primitive tuples, so
    no transaction is open across a Firebase call here. A notification failure
    is logged and never touches the already-committed homework/exam.
    """
    import logging as _mlog
    from sqlalchemy.orm import load_only
    from app.services import async_dispatch
    from app.services.fcm_service import is_enabled as _fcm_enabled, send_push_batch

    _log = _mlog.getLogger('mecha.mobile')
    student_ids = sorted({int(s) for s in (student_ids or [])})
    if not student_ids:
        return

    # 1. Links → parent_user_id : lowest linked student_id in this audience.
    parent_to_student: dict[int, int] = {}
    for uid, sid in db.session.execute(
        select(parent_students.c.user_id, parent_students.c.student_id).where(
            parent_students.c.student_id.in_(student_ids)
        )
    ).fetchall():
        if uid not in parent_to_student or sid < parent_to_student[uid]:
            parent_to_student[uid] = sid
    if not parent_to_student:
        _log.info('%s no linked parents school_id=%s', log_tag, school_id)
        return

    # 2. Only parents whose OWN account belongs to this school. load_only keeps
    # the row narrow; the loaded objects are handed to Notification.target_user
    # so the flush guard validates them from the identity map, not per row.
    users_q = (User.query
               .execution_options(bypass_tenant_scope=True)
               .options(load_only(User.id, User.school_id, User.is_active))
               .filter(User.id.in_(list(parent_to_student)),
                       User.school_id == school_id))
    if active_parents_only:
        users_q = users_q.filter(User.is_active.is_(True))
    users = {u.id: u for u in users_q.all()}
    skipped = len(parent_to_student) - len(users)
    if skipped:
        _log.warning('%s school_id=%s: %d linked parent(s) skipped '
                     '(outside this school%s)', log_tag, school_id, skipped,
                     ' or inactive' if active_parents_only else '')
    if not users:
        return

    # 3. Event-scoped dedup (retry safety), one query.
    already: set[int] = set()
    if dedup_since is not None:
        already = {
            row[0] for row in (
                db.session.query(Notification.target_user_id)
                .execution_options(bypass_tenant_scope=True)
                .filter(Notification.school_id == school_id,
                        Notification.ntype == ntype,
                        Notification.title == title,
                        Notification.body == body,
                        Notification.target_user_id.in_(list(users)),
                        Notification.created_at >= dedup_since)
                .all())
        }

    notif_rows: list[Notification] = []
    push_items: list[tuple] = []
    for uid in sorted(users):
        if uid in already:
            continue
        notif_rows.append(Notification(
            school_id=school_id, title=title, body=body, ntype=ntype,
            target_user=users[uid], created_by=created_by))
        if fcm_data is not None:
            push_items.append((uid, title, body,
                               {**fcm_data,
                                'student_id': str(parent_to_student[uid])}))

    if not notif_rows:
        _log.info('%s all notifications already exist school_id=%s',
                  log_tag, school_id)
        return

    try:
        db.session.add_all(notif_rows)
        db.session.commit()
    except Exception:
        # No push without its committed in-app row. The homework/exam itself
        # was committed by the request before this job and is untouched.
        db.session.rollback()
        _log.exception('%s Notification batch commit FAILED school_id=%s rows=%d '
                       '— rolled back, push NOT queued (source record unaffected)',
                       log_tag, school_id, len(notif_rows))
        return

    if push_items:
        if _fcm_enabled():
            async_dispatch.submit(send_push_batch, push_items)
        else:
            _log.info('%s FCM disabled — push skipped school_id=%s',
                      log_tag, school_id)

    _log.warning('%s school_id=%s notif_rows=%d fcm_queued=%d',
                 log_tag, school_id, len(notif_rows), len(push_items))


def _active_audience_student_ids(school_id: int, section_id, group_id) -> list[int]:
    """Active students targeted by a section OR an institute group, school-pinned.

    Same audience rules as the web helpers: a section's active students, or the
    students holding an ACTIVE enrollment in the group, re-filtered by school_id
    and status. Neither target → [] (never a wider audience).
    """
    from app.utils.institute_groups import active_student_ids_in_group
    q = (db.session.query(Student.id)
         .execution_options(bypass_tenant_scope=True)
         .filter(Student.school_id == school_id, Student.status == 'active'))
    if group_id:
        enrolled = active_student_ids_in_group(school_id, group_id)
        if not enrolled:
            return []
        q = q.filter(Student.id.in_(enrolled))
    elif section_id:
        q = q.filter(Student.section_id == section_id)
    else:
        return []
    return [r[0] for r in q.all()]


def _notify_new_exam_bg(exam_id: int, school_id: int) -> None:
    """New-exam notification for the mobile teacher APIs. Never raises.

    Audience and payload match grades._notify_new_exam(): active parents (in
    this school) of the active students of the exam's section or institute
    group; push for both. Siblings collapse to one notification per parent.
    """
    import logging as _mlog
    try:
        exam = (db.session.query(Exam.section_id, Exam.institute_group_id,
                                 Exam.subject_id, Exam.exam_name, Exam.created_at)
                .execution_options(bypass_tenant_scope=True, include_all_years=True)
                .filter(Exam.id == exam_id, Exam.school_id == school_id)
                .first())
        if exam is None or not school_id or (not exam.section_id
                                             and not exam.institute_group_id):
            return
        _notify_parents_batched(
            school_id=school_id,
            student_ids=_active_audience_student_ids(
                school_id, exam.section_id, exam.institute_group_id),
            title='اختبار جديد',
            body=f'تم جدولة اختبار جديد: {exam.exam_name or ""}.',
            ntype='exam',
            fcm_data={
                'type':       'exam',
                'screen':     'exams',
                'route':      '/parent/exams',
                'exam_id':    str(exam_id),
                'subject_id': str(exam.subject_id or ''),
                'ntype':      'exam',
            },
            active_parents_only=True,
            created_by=None,
            dedup_since=exam.created_at,
            log_tag=f'[mobile-exam] exam_id={exam_id}',
        )
    except Exception:
        db.session.rollback()
        _mlog.getLogger('mecha.mobile').exception(
            '[mobile-exam] background notify failed exam_id=%s school_id=%s',
            exam_id, school_id,
        )


def _notify_homework_bg(homework_id: int, school_id: int,
                        created_by: int | None = None) -> None:
    """New-homework notification for the mobile teacher API. Never raises.

    Audience, text and payload match homework._notify_homework_parents(): the
    linked parents (in this school) of the active students of hw.section_id;
    an institute row targets its group's active enrollments and stays in-app
    only (FCM withheld, as on the web). `created_by` is the authoring user's id,
    passed as a primitive — current_user does not exist in this thread.
    """
    import logging as _mlog
    try:
        hw = (db.session.query(Homework.section_id, Homework.institute_group_id,
                               Homework.title, Homework.created_at, Subject.name)
              .execution_options(bypass_tenant_scope=True, include_all_years=True)
              .outerjoin(Subject, (Subject.id == Homework.subject_id)
                         & (Subject.school_id == school_id))
              .filter(Homework.id == homework_id, Homework.school_id == school_id)
              .first())
        if hw is None:
            return
        is_institute_hw = bool(hw.institute_group_id)
        _notify_parents_batched(
            school_id=school_id,
            student_ids=_active_audience_student_ids(
                school_id, hw.section_id, hw.institute_group_id),
            title='واجب جديد',
            body=f'تم إضافة واجب جديد في مادة {hw.name or "غير محدد"}: {hw.title}',
            ntype='homework',
            fcm_data=None if is_institute_hw else {
                'type':        'homework',
                'ntype':       'homework',
                'route':       '/parent/homework',
                'homework_id': str(homework_id),
                'section_id':  str(hw.section_id),
                'screen':      'homework',
            },
            active_parents_only=False,
            created_by=created_by,
            dedup_since=hw.created_at,
            log_tag=f'[mobile-hw] hw_id={homework_id}',
        )
    except Exception:
        db.session.rollback()
        _mlog.getLogger('mecha.mobile').exception(
            '[mobile-hw] background notify failed hw_id=%s school_id=%s',
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
    limit, offset = page_args(default_limit=50, max_limit=100)

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

    limit, offset = page_args(default_limit=50, max_limit=100)

    q = (Homework.query
         .filter_by(teacher_id=emp.id, academic_year_id=year.id, is_active=True)
         .order_by(Homework.publish_date.desc(), Homework.id.desc()))

    total = q.count()
    rows  = q.offset(offset).limit(limit).all()
    # Institute rows only: one batched name lookup; a school page runs none.
    group_names = _group_names(emp.school_id,
                               {hw.institute_group_id for hw in rows})

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
                # Additive, institute homework only: a school item keeps its
                # exact previous key set.
                **({'group_id':   hw.institute_group_id,
                    'group_name': group_names.get(hw.institute_group_id)}
                   if hw.institute_group_id else {}),
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
    from app.utils.homework_attachments import HomeworkImageError, prepare_homework_upload
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
        # Images are validated from their bytes and optimised to WebP before
        # any Storage write; PDFs pass through unchanged.
        try:
            upload = prepare_homework_upload(attachment)
        except HomeworkImageError as exc:
            return err(str(exc))
        uploaded = save_uploaded_file(
            upload,
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
    # explicit school_id equality; only primitives cross the thread boundary
    # (the author's user id included — current_user does not exist there).
    # Best-effort: a notification failure must never fail the API response.
    try:
        from app.services import async_dispatch
        async_dispatch.submit(_notify_homework_bg, hw.id, emp.school_id, user.id)
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
    from app.utils.homework_attachments import HomeworkImageError, prepare_homework_upload

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

    # Single-target guard: this is the SECTION edit path. An institute row
    # (institute_group_id set) must never be given a section here, which would
    # make it target both and reach that section's parents. Institute homework
    # is edited only through PUT /teacher/institute/homework/<id>.
    if hw.institute_group_id is not None:
        return err('institute_homework_use_institute_endpoint', 409)

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
            try:
                upload = prepare_homework_upload(attachment_file)
            except HomeworkImageError as exc:
                return err(str(exc))
            uploaded = save_uploaded_file(
                upload,
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


# ═════════════════════════════════════════════════════════════════════════════
#  INSTITUTE GROUP SESSIONS AND MANUAL ATTENDANCE
# ═════════════════════════════════════════════════════════════════════════════
#
# Authorization is decided ENTIRELY on the server and never trusted from the
# client:
#
#   * the institution must be an institute (School.is_institute);
#   * the Employee is resolved from the JWT subject via the existing
#     Employee.user_id link, exactly like every other teacher endpoint;
#   * a group is reachable only when InstituteStudyGroup.instructor_id is that
#     Employee, inside the same school AND the school's current academic year;
#   * a posted student_id must be currently enrolled in THAT group, or already
#     hold a record for that session (a correction);
#   * school_id is taken from the Employee, never from the request, and a NULL
#     school_id is treated as no access at all — never as global access.
#
# Every write goes through app/services/institute_attendance.py, the same
# domain service the web blueprint uses. Nothing here re-implements the rules,
# and a future card/device source plugs into that one path.
#
# NO AUTOMATIC ABSENCE: opening a session materializes a 'not_recorded' row and
# nothing else. A status exists only because this endpoint received one.


def _institute_school_or_none(emp):
    """The Employee's school, but only when it is an institute.

    Fail-closed on every degenerate case: no employee, no school_id (a NULL
    school_id is NOT global access), a missing school row, or a school-type
    institution.
    """
    if emp is None or not getattr(emp, 'school_id', None):
        return None
    school = School.query.execution_options(bypass_tenant_scope=True).get(
        emp.school_id)
    if school is None or not getattr(school, 'is_institute', False):
        return None
    return school


def _institute_context():
    """(employee, school, year) or (None, None, None) when out of scope."""
    emp = _get_employee()
    school = _institute_school_or_none(emp)
    if school is None:
        return None, None, None
    year = (AcademicYear.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school.id, is_current=True)
            .first())
    if year is None:
        return None, None, None
    return emp, school, year


def _my_institute_groups(school, year, emp):
    """Active groups assigned to THIS instructor. Never a wider fallback."""
    if emp is None:
        return []
    return (InstituteStudyGroup.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=school.id, academic_year_id=year.id,
                       instructor_id=emp.id, is_active=True)
            .order_by(InstituteStudyGroup.name)
            .all())


def _session_dict(occ, counts=None):
    counts = counts or {}
    return {
        'group_id':     occ.group.id,
        'group_name':   occ.group.name,
        'subject_name': occ.group.subject.name if occ.group.subject else None,
        'date':         occ.date.strftime('%Y-%m-%d'),
        'day_of_week':  occ.day_of_week,
        'day_label':    inst_att.day_name(occ.day_of_week),
        'start_time':   occ.start_time.strftime('%H:%M'),
        'end_time':     occ.end_time.strftime('%H:%M'),
        'session_id':   occ.session.id if occ.session else None,
        # 'not_recorded' is a first-class value, never rendered as absence.
        'status':       occ.status,
        'is_recorded':  occ.is_recorded,
        'summary': {
            'present': counts.get('present', 0),
            'absent':  counts.get('absent', 0),
            'late':    counts.get('late', 0),
            'excused': counts.get('excused', 0),
        },
    }


@mobile_api_bp.route('/teacher/institute/sessions', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_institute_sessions():
    """My institute group sessions between ?start and ?end (default: today).

    Read-only and side-effect free: listing a date NEVER materializes a session
    and never creates an attendance row.
    """
    emp, school, year = _institute_context()
    if school is None:
        return err('institute_not_available', 404)

    groups = _my_institute_groups(school, year, emp)
    if not groups:
        return ok(count=0, sessions=[], statuses=list(
            InstituteAttendanceRecord.STATUSES))

    today = inst_att.local_today(school)

    def _parse(raw, fallback):
        if not raw:
            return fallback
        try:
            return _dt.strptime(str(raw).strip(), '%Y-%m-%d').date()
        except (TypeError, ValueError):
            return fallback

    start = _parse(request.args.get('start'), today)
    end = _parse(request.args.get('end'), start)
    if end < start:
        end = start

    group_id = request.args.get('group_id', type=int)
    if group_id:
        groups = [g for g in groups if g.id == group_id]
        if not groups:
            # A forged or unassigned id yields an empty scope, not an error
            # that would confirm the group exists elsewhere.
            return ok(count=0, sessions=[], statuses=list(
                InstituteAttendanceRecord.STATUSES))

    try:
        occurrences = inst_att.occurrences_for_range(school, groups, start, end)
    except inst_att.AttendanceError as exc:
        return err(str(exc), 400)

    summary = inst_att.attendance_summary(
        school, [o.session.id for o in occurrences if o.session])
    payload = [_session_dict(o, summary.get(o.session.id) if o.session else None)
               for o in occurrences]
    return ok(count=len(payload), start=start.strftime('%Y-%m-%d'),
              end=end.strftime('%Y-%m-%d'), sessions=payload,
              statuses=list(InstituteAttendanceRecord.STATUSES))


@mobile_api_bp.route('/teacher/institute/sessions/open', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_institute_session_open():
    """Open ONE occurrence and return its roster.

    Requires group_id + date + start, which must match a REAL occurrence of an
    active weekly rule (or an already materialized session). An arbitrary date
    cannot be invented through this endpoint.

    Materializing the session does not record anything: it is created as
    'not_recorded' with no statuses at all.
    """
    emp, school, year = _institute_context()
    if school is None:
        return err('institute_not_available', 404)
    if not emp.can_record_institute_attendance:
        return err('attendance_not_permitted', 403)

    group_id = request.args.get('group_id', type=int)
    groups = {g.id: g for g in _my_institute_groups(school, year, emp)}
    group = groups.get(group_id)
    if group is None:
        return err('session_not_found', 404)

    try:
        on_date = _dt.strptime(request.args.get('date', ''), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return err('invalid_date — use YYYY-MM-DD', 400)
    start_raw = (request.args.get('start') or '').strip()
    start_time = None
    for fmt in ('%H:%M', '%H:%M:%S'):
        try:
            start_time = _dt.strptime(start_raw, fmt).time()
            break
        except ValueError:
            continue
    if start_time is None:
        return err('invalid_start — use HH:MM', 400)

    occ = inst_att.find_occurrence(school, group, on_date, start_time)
    if occ is None:
        return err('session_not_found', 404)

    session = inst_att.get_or_create_session(school, group, occ)
    roster = inst_att.session_roster(school, session)
    eligible = inst_att.eligible_student_ids(school, group.id)

    students = [{
        'student_id':   stu.id,
        'student_code': stu.student_id,
        'full_name':    stu.full_name,
        # An unmarked student is null, NEVER 'absent'.
        'status':       rec.status if rec else None,
        # Stored as naive UTC; returned as ISO-8601 WITH the school's
        # offset (e.g. 2026-09-23T11:44:00+03:00) so the client needs no
        # conversion of its own.
        'recorded_at':  inst_att.to_local_iso(rec.recorded_at, school)
                        if rec else None,
        # False for a student who has left the group but already holds a
        # record — the app shows them read-only instead of hiding history.
        'editable':     stu.id in eligible,
    } for stu, rec in roster]

    return ok(session={
        'session_id':   session.id,
        'group_id':     group.id,
        'group_name':   group.name,
        'subject_name': group.subject.name if group.subject else None,
        'date':         session.session_date.strftime('%Y-%m-%d'),
        'start_time':   session.start_time.strftime('%H:%M'),
        'end_time':     session.end_time.strftime('%H:%M'),
        'status':       session.status,
        'is_recorded':  session.is_recorded,
        'recorded_at':  inst_att.to_local_iso(session.recorded_at, school),
    }, count=len(students), students=students,
        statuses=list(InstituteAttendanceRecord.STATUSES))


@mobile_api_bp.route('/teacher/institute/sessions/<int:session_id>/attendance',
                     methods=['POST'])
@jwt_required()
@role_required('teacher')
def teacher_institute_submit_attendance(session_id):
    """Submit or correct attendance for one session. Atomic and idempotent.

    Body: {"records": [{"student_id": 1, "status": "present"}, …]}

    Re-sending the same body changes nothing and notifies nobody, so a retry
    after a network failure is safe. A student omitted from the body keeps
    whatever they had — omission is NOT absence.
    """
    emp, school, year = _institute_context()
    if school is None:
        return err('institute_not_available', 404)
    if not emp.can_record_institute_attendance:
        return err('attendance_not_permitted', 403)

    session = (InstituteAttendanceSession.query
               .execution_options(bypass_tenant_scope=True)
               .filter_by(id=session_id, school_id=school.id)
               .first())
    if session is None:
        return err('session_not_found', 404)

    # The session's group must be assigned to THIS instructor right now.
    # Resolved server side; the client cannot widen it with any id it sends.
    if session.group_id not in {g.id for g in
                                _my_institute_groups(school, year, emp)}:
        return err('session_not_found', 404)

    body = request.get_json(silent=True) or {}
    raw_records = body.get('records')
    if not isinstance(raw_records, list) or not raw_records:
        return err('records[] is required', 400)
    if len(raw_records) > 500:
        return err('too_many_records', 400)

    statuses = {}
    for item in raw_records:
        if not isinstance(item, dict):
            return err('invalid record entry', 400)
        sid = item.get('student_id')
        status = item.get('status')
        if sid is None or status is None:
            return err('student_id and status are required', 400)
        statuses[sid] = status

    try:
        result = inst_att.submit_attendance(
            school, session, statuses,
            source=InstituteAttendanceSession.SOURCE_MANUAL_INSTRUCTOR,
            actor_user_id=g.mobile_user.id)
    except inst_att.AttendanceError as exc:
        return err(str(exc), 400)

    return ok(session_id=session.id, status=session.status,
              created=result['created'], updated=result['updated'],
              unchanged=result['unchanged'], notified=result['notified'])


# ═════════════════════════════════════════════════════════════════════════════
#  INSTITUTE READ APIS — my groups, group roster, student profile
# ═════════════════════════════════════════════════════════════════════════════
#
# Same authorization as the session endpoints above: _institute_context()
# (institute only, Employee from the JWT, current academic year) and
# _my_institute_groups() (ACTIVE groups whose instructor_id is THIS Employee).
# A group or student outside that set is a plain 404 — identical for another
# instructor's, another institute's or a nonexistent id — so nothing discloses
# whether it exists elsewhere. No client value widens the scope. Every
# endpoint here is read-only.

_OPTS = {'bypass_tenant_scope': True}


def _group_names(school_id, group_ids) -> dict:
    """{group_id: name} for many groups in ONE query, pinned to this school."""
    group_ids = {gid for gid in (group_ids or ()) if gid}
    if not school_id or not group_ids:
        return {}
    return dict(db.session.query(InstituteStudyGroup.id, InstituteStudyGroup.name)
                .filter(InstituteStudyGroup.school_id == school_id,
                        InstituteStudyGroup.id.in_(group_ids))
                .execution_options(**_OPTS)
                .all())


def _subject_map(school_id, subject_ids) -> dict:
    """{subject_id: name} in ONE query, pinned to this school."""
    subject_ids = {sid for sid in (subject_ids or ()) if sid}
    if not subject_ids:
        return {}
    return dict(db.session.query(Subject.id, Subject.name)
                .filter(Subject.school_id == school_id,
                        Subject.id.in_(subject_ids))
                .execution_options(**_OPTS)
                .all())


def _group_brief(grp, subjects) -> dict:
    return {
        'group_id': grp.id,
        'name':     grp.name,
        'subject':  {'id': grp.subject_id, 'name': subjects.get(grp.subject_id)},
    }


@mobile_api_bp.route('/teacher/institute/groups', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_institute_groups():
    """My ACTIVE study groups in the current academic year, with the active
    student count and the weekly slots. Fixed query count: groups, subjects,
    counts, slots — never one per group."""
    emp, school, year = _institute_context()
    if school is None:
        return err('institute_not_available', 404)

    groups = _my_institute_groups(school, year, emp)
    group_ids = [grp.id for grp in groups]
    subjects = _subject_map(school.id, (grp.subject_id for grp in groups))
    counts = active_enrollment_count_map(school, group_ids)
    slots = (inst_att.slots_by_group(school.id, year.id, group_ids, active_only=True)
             if group_ids else {})

    return ok(
        count=len(groups),
        groups=[
            {
                **_group_brief(grp, subjects),
                'student_count': counts.get(grp.id, 0),
                'start_date':    grp.start_date.isoformat() if grp.start_date else None,
                'end_date':      grp.end_date.isoformat() if grp.end_date else None,
                'slots': [
                    {
                        'day_of_week': slot.day_of_week,
                        'day_label':   inst_att.day_name(slot.day_of_week),
                        'start_time':  _fmt_time(slot.start_time),
                        'end_time':    _fmt_time(slot.end_time),
                    }
                    for slot in slots.get(grp.id, [])
                ],
            }
            for grp in groups
        ],
    )


@mobile_api_bp.route('/teacher/institute/groups/<int:group_id>/students', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_institute_group_students(group_id):
    """Active roster of ONE of my groups (active_roster: ACTIVE enrollments,
    the same population student_count counts). Read-only — unlike
    /sessions/open, nothing is materialized."""
    emp, school, year = _institute_context()
    if school is None:
        return err('institute_not_available', 404)

    group = next((grp for grp in _my_institute_groups(school, year, emp)
                  if grp.id == group_id), None)
    if group is None:
        return err('group_not_found', 404)

    students = [stu for _, stu in active_roster(school, group.id)]
    return ok(
        group=_group_brief(group, _subject_map(school.id, [group.subject_id])),
        count=len(students),
        students=[
            {
                'id':         stu.id,
                'student_id': stu.student_id,
                'name':       stu.full_name,
                'photo':      photo_url(student_display_value(stu)),
                'status':     stu.status,
            }
            for stu in students
        ],
    )


_RECENT_RESULTS_LIMIT = 10


@mobile_api_bp.route('/teacher/institute/students/<int:student_id>', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_institute_student_profile(student_id):
    """Compact profile of a student who holds an ACTIVE enrollment in one of
    my groups (instructor_can_access_student). Only MY groups and results of
    MY groups' exams are returned — never the student's memberships or results
    with other instructors, and never school-section exams."""
    emp, school, year = _institute_context()
    if school is None:
        return err('institute_not_available', 404)
    if not instructor_can_access_student(school, g.mobile_user, year, student_id):
        return err('student_not_found', 404)

    student = (Student.query.execution_options(**_OPTS)
               .filter_by(id=student_id, school_id=school.id).first())
    if student is None:
        return err('student_not_found', 404)

    my_groups = {grp.id: grp for grp in _my_institute_groups(school, year, emp)}
    shared_ids = {
        row[0] for row in
        db.session.query(InstituteGroupEnrollment.group_id)
        .filter(InstituteGroupEnrollment.school_id == school.id,
                InstituteGroupEnrollment.student_id == student.id,
                InstituteGroupEnrollment.status
                == InstituteGroupEnrollment.STATUS_ACTIVE,
                InstituteGroupEnrollment.group_id.in_(list(my_groups)))
        .execution_options(**_OPTS)
        .all()
    } if my_groups else set()
    groups = sorted((my_groups[gid] for gid in shared_ids),
                    key=lambda grp: (grp.name or '', grp.id))
    subjects = _subject_map(school.id, (grp.subject_id for grp in groups))

    results = (db.session.query(ExamResult, Exam)
               .join(Exam, Exam.id == ExamResult.exam_id)
               .options(joinedload(Exam.exam_type))
               .filter(ExamResult.student_id == student.id,
                       ExamResult.school_id == school.id,
                       Exam.school_id == school.id,
                       Exam.institute_group_id.in_(list(my_groups)))
               .execution_options(**_OPTS)
               .order_by(Exam.exam_date.desc(), ExamResult.id.desc())
               .limit(_RECENT_RESULTS_LIMIT)
               .all()) if my_groups else []

    return ok(
        student={
            'id':         student.id,
            'student_id': student.student_id,
            'name':       student.full_name,
            'photo':      photo_url(student_display_value(student)),
            'status':     student.status,
        },
        groups=[_group_brief(grp, subjects) for grp in groups],
        recent_results=[
            {
                'exam_id':    exam.id,
                'exam_name':  exam.display_name,
                'group_id':   exam.institute_group_id,
                'group_name': my_groups[exam.institute_group_id].name,
                'score':      float(res.marks) if res.marks is not None else None,
                'max_score':  float(exam.max_marks),
                'exam_date':  exam.exam_date.isoformat() if exam.exam_date else None,
            }
            for res, exam in results
        ],
    )


# ═════════════════════════════════════════════════════════════════════════════
#  INSTITUTE WRITE APIS — exams, grade entry, homework
# ═════════════════════════════════════════════════════════════════════════════
#
# Group-targeted content only: every row written here carries
# institute_group_id and a NULL section_id, and its subject is copied from the
# group — never taken from the request. The same rules as the web institute
# branches (grades.create_exam / grades.enter_results / homework.create /
# homework.edit), resolved through the same shared helpers:
#
#   * resolve_eligible_group(is_manager=False) — the ACTIVE group of THIS
#     institute and current year whose instructor is the JWT user's Employee;
#   * active_roster_students()  — who may receive a NEW grade;
#   * historical_result_rows()  — stored results shown read-only.
#
# Exams and grade entry additionally require the existing 'enter_grades'
# permission, exactly as the web routes do. A group, exam or homework outside
# the caller's scope is a 404 that discloses nothing about whether it exists.
# Every write is validated completely before anything is added to the session
# and committed ONCE; notifications run only after that commit.

_MAX_RESULT_ENTRIES = 500

import logging as _inst_logging  # noqa: E402
_inst_log = _inst_logging.getLogger('mecha.mobile.institute')


def _text(data, key) -> str:
    """A stripped string field; non-string JSON values are coerced, None -> ''."""
    raw = data.get(key)
    if raw is None:
        return ''
    return (raw if isinstance(raw, str) else str(raw)).strip()


def _can_enter_grades() -> bool:
    return bool(g.mobile_user.has_permission('enter_grades'))


def _json_or_form():
    """(data, attachment) — multipart form with optional file, or JSON."""
    if request.content_type and 'multipart/form-data' in request.content_type:
        return request.form, request.files.get('attachment')
    return (request.get_json(silent=True) or {}), None


def _opt_int(raw):
    """(value, ok): None when absent/blank; ok=False when present but not an int."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, True
    if isinstance(raw, bool):
        return None, False
    try:
        return int(raw), True
    except (TypeError, ValueError):
        return None, False


def _refuse_foreign_target(data, group):
    """Error response when the client tries to steer the target or subject.

    section_id must be absent/null: an institute row never carries a section.
    subject_id, when sent, must equal the group's own subject.
    """
    section_id, ok_sec = _opt_int(data.get('section_id'))
    if not ok_sec or section_id is not None:
        return err('section_id_not_allowed_for_institute')
    subject_id, ok_subj = _opt_int(data.get('subject_id'))
    if not ok_subj or (subject_id is not None and subject_id != group.subject_id):
        return err('subject_mismatch — the subject comes from the group')
    return None


def _institute_exam_scope(school, exam_id):
    """(exam, group) for an institute exam this instructor may OPEN, else
    (None, None). Mirrors grades._institute_exam_group(include_inactive=True):
    the exam's OWN academic year, the caller's own groups, an inactive group
    admitted for reading only."""
    exam = (Exam.query.execution_options(**_OPTS)
            .options(joinedload(Exam.exam_type))
            .filter(Exam.id == exam_id, Exam.school_id == school.id,
                    Exam.institute_group_id.isnot(None))
            .first())
    if exam is None:
        return None, None
    exam_year = (AcademicYear.query.execution_options(**_OPTS)
                 .filter_by(id=exam.academic_year_id, school_id=school.id).first())
    groups = eligible_groups_for_user(school, g.mobile_user, exam_year,
                                      is_manager=False, include_inactive=True)
    group = next((grp for grp in groups if grp.id == exam.institute_group_id), None)
    return (exam, group) if group is not None else (None, None)


def _institute_exam_item(exam, group_name, subjects, today, result_count=None):
    item = {
        'id':               exam.id,
        'name':             exam.display_name,
        'title':            exam.display_name,
        'exam_type_id':     exam.exam_type_id,
        'exam_date':        exam.exam_date.isoformat() if exam.exam_date else None,
        'max_score':        float(exam.max_marks),
        'pass_marks':       float(exam.pass_marks),
        'is_upcoming':      exam.exam_date >= today if exam.exam_date else None,
        'group_id':         exam.institute_group_id,
        'group_name':       group_name,
        'subject':          {'id': exam.subject_id, 'name': subjects.get(exam.subject_id)},
        'section_id':       None,
        'created_at':       exam.created_at.isoformat() if exam.created_at else None,
    }
    if result_count is not None:
        item['result_count'] = result_count
    return item


# ─── Institute exams: list / create ───────────────────────────────────────────

@mobile_api_bp.route('/teacher/institute/exams', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_institute_exams():
    """Exams of MY active groups in the current academic year, newest first.

    ?group_id only narrows my own set — an unassigned id yields an empty page,
    the /sessions convention. ?limit (default 50, max 100) & ?offset.
    """
    emp, school, year = _institute_context()
    if school is None:
        return err('institute_not_available', 404)

    groups = {grp.id: grp for grp in _my_institute_groups(school, year, emp)}
    group_id, ok_gid = _opt_int(request.args.get('group_id'))
    if not ok_gid:
        return err('invalid group_id')
    group_ids = ([group_id] if group_id in groups else []) if group_id else list(groups)
    limit, offset = page_args(default_limit=50, max_limit=100)
    if not group_ids:
        return ok(total=0, limit=limit, offset=offset, exams=[])

    q = (Exam.query.execution_options(**_OPTS)
         .filter(Exam.school_id == school.id,
                 Exam.academic_year_id == year.id,
                 Exam.institute_group_id.in_(group_ids)))
    total = q.count()
    exams = (q.options(joinedload(Exam.exam_type))
             .order_by(Exam.exam_date.desc(), Exam.id.desc())
             .offset(offset).limit(limit).all())
    exam_ids = [e.id for e in exams]
    counts = dict(db.session.query(ExamResult.exam_id, func.count(ExamResult.id))
                  .filter(ExamResult.school_id == school.id,
                          ExamResult.exam_id.in_(exam_ids))
                  .execution_options(**_OPTS)
                  .group_by(ExamResult.exam_id).all()) if exam_ids else {}
    subjects = _subject_map(school.id, (e.subject_id for e in exams))
    today = date.today()
    return ok(total=total, limit=limit, offset=offset, exams=[
        _institute_exam_item(e, groups[e.institute_group_id].name, subjects, today,
                             result_count=counts.get(e.id, 0))
        for e in exams])


@mobile_api_bp.route('/teacher/institute/exams', methods=['POST'])
@jwt_required()
@role_required('teacher')
def teacher_institute_create_exam():
    """Create ONE exam for ONE of my active groups (web grades.create_exam,
    institute branch). Body (JSON):

      group_id      int         required — must be one of my active groups
      name          str         required (alias: title, exam_name)
      exam_date     YYYY-MM-DD  required
      max_score     number      optional, default 100 (alias: max_marks)
      pass_marks    number      optional, default 50; 0 <= pass <= max
      exam_type_id  int         optional, must exist

    subject_id is taken from the group (a different value is rejected) and
    section_id must be absent: section_id is always NULL on the stored row.
    """
    emp, school, year = _institute_context()
    if school is None:
        return err('institute_not_available', 404)
    if not _can_enter_grades():
        return err('forbidden', 403)

    data = request.get_json(silent=True) or {}
    name = _text(data, 'name') or _text(data, 'title') or _text(data, 'exam_name')
    if not name:
        return err('required_field_missing: name')

    group_id, ok_gid = _opt_int(data.get('group_id'))
    if not ok_gid or group_id is None:
        return err('required_field_missing: group_id')
    group, _msg = resolve_eligible_group(school, g.mobile_user, year, group_id,
                                         is_manager=False)
    if group is None:
        return err('group_not_found', 404)
    refused = _refuse_foreign_target(data, group)
    if refused is not None:
        return refused

    try:
        exam_date = _dt.strptime(_text(data, 'exam_date'), '%Y-%m-%d').date()
    except ValueError:
        return err('invalid exam_date — use YYYY-MM-DD')
    raw_max = data.get('max_score') if data.get('max_score') is not None \
        else data.get('max_marks', 100)
    raw_pass = data.get('pass_marks', 50)
    try:
        if isinstance(raw_max, bool) or isinstance(raw_pass, bool):
            raise ValueError
        max_marks, pass_marks = Decimal(str(raw_max)), Decimal(str(raw_pass))
        if not (max_marks.is_finite() and pass_marks.is_finite()):
            raise ValueError
    except (InvalidOperation, ValueError, TypeError):
        return err('max_score and pass_marks must be numbers')
    if max_marks <= 0:
        return err('max_score must be greater than 0')
    if pass_marks < 0 or pass_marks > max_marks:
        return err('pass_marks must be between 0 and max_score')

    exam_type_id, ok_type = _opt_int(data.get('exam_type_id'))
    if not ok_type or (exam_type_id is not None
                       and db.session.get(ExamType, exam_type_id) is None):
        return err('invalid exam_type_id')

    exam = Exam(
        school_id          = school.id,
        academic_year_id   = year.id,           # the year the group was validated in
        exam_name          = name,
        exam_type_id       = exam_type_id,
        subject_id         = group.subject_id,  # derived, never posted
        section_id         = None,              # never both targets
        institute_group_id = group.id,
        exam_date          = exam_date,
        max_marks          = max_marks,
        pass_marks         = pass_marks,
    )
    db.session.add(exam)
    db.session.commit()

    # Same post-commit notification path as the school mobile exam;
    # _notify_new_exam_bg() targets a group's actively enrolled students.
    try:
        from app.services import async_dispatch
        async_dispatch.submit(_notify_new_exam_bg, exam.id, school.id)
    except Exception:
        _inst_log.exception('[mobile-inst-exam] notify dispatch failed exam_id=%s', exam.id)

    return ok(message='exam_created',
              exam=_institute_exam_item(exam, group.name,
                                        _subject_map(school.id, [exam.subject_id]),
                                        date.today(), result_count=0)), 201


# ─── Institute exam detail / grade entry ─────────────────────────────────────

def _result_dict(res):
    if res is None:
        return None
    return {
        'score':   float(res.marks) if res.marks is not None else None,
        'grade':   res.grade_letter,
        'is_pass': res.is_pass,
        'rank':    res.rank,
        'notes':   res.notes,
    }


@mobile_api_bp.route('/teacher/institute/exams/<int:exam_id>', methods=['GET'])
@jwt_required()
@role_required('teacher')
def teacher_institute_exam_detail(exam_id):
    """Exam + the EDITABLE roster (active enrollment, active student, active
    group) with each student's current result, plus stored results of students
    who are no longer eligible, shown read-only (historical_result_rows).
    Read-only request."""
    emp, school, year = _institute_context()
    if school is None:
        return err('institute_not_available', 404)
    exam, group = _institute_exam_scope(school, exam_id)
    if exam is None:
        return err('exam_not_found', 404)

    editable = bool(group.is_active)
    roster = active_roster_students(school.id, group.id) if editable else []
    results = {r.student_id: r for r in
               ExamResult.query.execution_options(**_OPTS)
               .filter_by(exam_id=exam.id, school_id=school.id).all()}
    historical = historical_result_rows(school.id, exam.id, group.id,
                                        exclude_student_ids=[s.id for s in roster])

    return ok(
        exam=_institute_exam_item(exam, group.name,
                                  _subject_map(school.id, [exam.subject_id]),
                                  date.today(), result_count=len(results)),
        group={'group_id': group.id, 'name': group.name, 'is_active': group.is_active},
        editable=editable,
        count=len(roster),
        students=[{
            'id':         s.id,
            'student_id': s.student_id,
            'name':       s.full_name,
            'photo':      photo_url(student_display_value(s)),
            'result':     _result_dict(results.get(s.id)),
            'editable':   editable,
        } for s in roster],
        historical=[{
            'id':         s.id,
            'student_id': s.student_id,
            'name':       s.full_name,
            'label':      label,
            'result':     _result_dict(res),
            'editable':   False,
        } for s, res, label in historical],
    )


@mobile_api_bp.route('/teacher/institute/exams/<int:exam_id>/results', methods=['POST'])
@jwt_required()
@role_required('teacher')
def teacher_institute_submit_results(exam_id):
    """Atomic batch grade entry for one of my institute exams.

    Body: {"results": [{"student_id": 1, "score": 88.5, "notes": "..."}]}
    ('marks' / 'note' accepted as aliases). A null or empty score means "not
    entered" and is skipped, exactly as on the web form.

    EVERY entry is validated before anything is written: a student outside the
    editable roster, a duplicate student, a non-numeric score or one outside
    0..max_score rejects the WHOLE batch and nothing is saved. Results and the
    recomputed ranks are committed together in ONE transaction.
    """
    emp, school, year = _institute_context()
    if school is None:
        return err('institute_not_available', 404)
    if not _can_enter_grades():
        return err('forbidden', 403)
    exam, group = _institute_exam_scope(school, exam_id)
    if exam is None:
        return err('exam_not_found', 404)
    if not group.is_active:
        # A deactivated group is read-only, re-decided from the stored row.
        return err('group_inactive — results are read-only', 409)

    entries = (request.get_json(silent=True) or {}).get('results')
    if not isinstance(entries, list) or not entries:
        return err('results must be a non-empty array')
    if len(entries) > _MAX_RESULT_ENTRIES:
        return err('too_many_results')

    roster = {s.id: s for s in active_roster_students(school.id, group.id)}
    max_marks = Decimal(str(exam.max_marks))
    pass_marks = Decimal(str(exam.pass_marks))

    parsed, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            return err('invalid result entry — nothing was saved')
        sid, ok_sid = _opt_int(entry.get('student_id'))
        if not ok_sid or sid is None or sid not in roster:
            return jsonify({'ok': False, 'error': 'student_not_in_group',
                            'student_id': entry.get('student_id'),
                            'message': 'nothing was saved'}), 400
        if sid in seen:
            return jsonify({'ok': False, 'error': 'duplicate_student',
                            'student_id': sid, 'message': 'nothing was saved'}), 400
        seen.add(sid)
        raw = entry.get('score') if entry.get('score') is not None else entry.get('marks')
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue                     # not entered — existing row untouched
        try:
            if isinstance(raw, bool):
                raise ValueError
            marks = Decimal(str(raw).strip())
            if not marks.is_finite():
                raise ValueError
        except (InvalidOperation, ValueError, TypeError):
            return jsonify({'ok': False, 'error': 'invalid_score', 'student_id': sid,
                            'message': 'nothing was saved'}), 400
        if marks < 0 or marks > max_marks:
            return jsonify({'ok': False, 'error': 'score_out_of_range', 'student_id': sid,
                            'max_score': float(max_marks),
                            'message': 'nothing was saved'}), 400
        notes = entry.get('notes') if entry.get('notes') is not None else entry.get('note')
        if notes is not None:
            notes = str(notes).strip()[:1000] or None
        parsed.append((sid, marks, notes, 'notes' in entry or 'note' in entry))

    if not parsed:
        return err('no scores were entered')

    existing = {r.student_id: r for r in
                ExamResult.query.execution_options(**_OPTS)
                .filter_by(exam_id=exam.id, school_id=school.id).all()}
    user_id = g.mobile_user.id
    created = updated = unchanged = 0
    graded = []
    for sid, marks, notes, notes_sent in parsed:
        grade = calculate_grade_letter(float(marks), float(max_marks))
        is_pass = marks >= pass_marks
        res = existing.get(sid)
        if res is None:
            # school / year are copied from the stored exam, never the request.
            db.session.add(ExamResult(
                exam_id=exam.id, student_id=sid, school_id=exam.school_id,
                academic_year_id=exam.academic_year_id, marks=marks,
                grade_letter=grade, is_pass=is_pass, notes=notes,
                entered_by=user_id))
            created += 1
            graded.append(sid)
            continue
        new_notes = notes if notes_sent else res.notes
        changed = (Decimal(str(res.marks)).quantize(Decimal('0.01'))
                   != marks.quantize(Decimal('0.01'))) or res.notes != new_notes
        res.marks, res.grade_letter, res.is_pass = marks, grade, is_pass
        res.notes, res.entered_by = new_notes, user_id
        if changed:
            updated += 1
            graded.append(sid)
        else:
            unchanged += 1

    # Ranks for the whole exam, in the SAME transaction as the results (the web
    # route commits them separately; here a failure rolls both back).
    db.session.flush()
    for rank, res in enumerate(ExamResult.query.execution_options(**_OPTS)
                               .filter_by(exam_id=exam.id, school_id=school.id)
                               .order_by(ExamResult.marks.desc(), ExamResult.id)
                               .all(), 1):
        res.rank = rank
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return err('results_changed_concurrently — nothing was saved, reload and retry', 409)

    if graded:
        try:
            _notify_grade_results_mobile(exam.id, school.id, exam.subject_id,
                                         exam.exam_name or exam.display_name,
                                         [roster[sid] for sid in graded])
        except Exception:
            _inst_log.exception('[mobile-inst-grades] notify failed exam_id=%s', exam.id)

    results = (ExamResult.query.execution_options(**_OPTS)
               .filter_by(exam_id=exam.id, school_id=school.id)
               .order_by(ExamResult.rank).all())
    return ok(exam_id=exam.id, saved=len(parsed), created=created, updated=updated,
              unchanged=unchanged,
              results=[{'student_id': r.student_id, **_result_dict(r)} for r in results])


# ─── Institute homework: create / update ─────────────────────────────────────

_HOMEWORK_EXTS = {'jpg', 'jpeg', 'png', 'webp', 'pdf'}


def _homework_gate(action=None):
    """The existing mobile homework feature switches, or None when allowed."""
    from app.utils.school_config import get_school_config
    cfg = get_school_config(g.mobile_user.school_id)
    if not cfg.action_enabled('homework', 'api_access'):
        return err('الوصول إلى الواجبات غير مفعل لهذه المدرسة.', 403)
    if action and not cfg.action_enabled('homework', action):
        return err('إضافة الواجبات غير مفعلة لهذه المدرسة.', 403)
    return None


def _store_homework_attachment(attachment):
    """(path, type, error_response) — the existing mobile upload pipeline."""
    from app.utils.helpers import save_uploaded_file
    from app.utils.homework_attachments import HomeworkImageError, prepare_homework_upload
    try:
        upload = prepare_homework_upload(attachment)
    except HomeworkImageError as exc:
        return None, None, err(str(exc))
    stored = save_uploaded_file(upload, subfolder='homework', allowed_exts=_HOMEWORK_EXTS)
    if stored is None:
        return None, None, err('invalid_attachment — allowed: jpg, jpeg, png, webp, pdf')
    ext = attachment.filename.rsplit('.', 1)[-1].lower() if '.' in attachment.filename else ''
    return stored, ('pdf' if ext == 'pdf' else 'image'), None


def _institute_homework_dict(hw, group_name, subjects):
    return {
        'id':              hw.id,
        'title':           hw.title,
        'description':     hw.description,
        'group_id':        hw.institute_group_id,
        'group_name':      group_name,
        'subject_id':      hw.subject_id,
        'subject_name':    subjects.get(hw.subject_id),
        'section_id':      None,
        'publish_date':    hw.publish_date.isoformat() if hw.publish_date else None,
        'due_date':        hw.due_date.isoformat() if hw.due_date else None,
        'attachment_url':  _hw_attachment_url(hw),
        'attachment_name': (hw.attachment_path.rstrip('/').rsplit('/', 1)[-1]
                            if hw.attachment_path else None),
        'attachment_type': hw.attachment_type,
    }


def _parse_date_field(raw, field):
    try:
        return _dt.strptime(str(raw).strip(), '%Y-%m-%d').date(), None
    except (TypeError, ValueError):
        return None, err(f'invalid {field} — use YYYY-MM-DD')


def _notify_institute_homework(hw, school_id):
    """The web institute homework notification (in-app rows for the parents
    of the group's actively enrolled students; FCM stays withheld there).
    Runs after the homework commit; never fails the request."""
    try:
        from app.blueprints.homework import _notify_homework_parents
        _notify_homework_parents(hw, school_id)
    except Exception:
        db.session.rollback()
        _inst_log.exception('[mobile-inst-hw] notify failed hw_id=%s', hw.id)


@mobile_api_bp.route('/teacher/institute/homework', methods=['POST'])
@jwt_required()
@role_required('teacher')
def teacher_institute_create_homework():
    """Create homework for ONE of my active groups (web homework.create,
    institute branch). JSON or multipart (optional 'attachment'):

      group_id      int         required — one of my active groups
      title         str         required
      due_date      YYYY-MM-DD  required, not before publish_date
      publish_date  YYYY-MM-DD  optional, default today
      description   str         optional

    subject comes from the group; section_id must be absent. An identical
    active assignment (same group, title, publish date) is refused (409).
    """
    gate = _homework_gate('create')
    if gate is not None:
        return gate
    emp, school, year = _institute_context()
    if school is None:
        return err('institute_not_available', 404)

    data, attachment = _json_or_form()
    title = _text(data, 'title')
    if not title:
        return err('required_field_missing: title')
    group_id, ok_gid = _opt_int(data.get('group_id'))
    if not ok_gid or group_id is None:
        return err('required_field_missing: group_id')
    group, _msg = resolve_eligible_group(school, g.mobile_user, year, group_id,
                                         is_manager=False)
    if group is None:
        return err('group_not_found', 404)
    refused = _refuse_foreign_target(data, group)
    if refused is not None:
        return refused

    if not _text(data, 'due_date'):
        return err('required_field_missing: due_date')
    due_dt, bad = _parse_date_field(data.get('due_date'), 'due_date')
    if bad:
        return bad
    if _text(data, 'publish_date'):
        pub_dt, bad = _parse_date_field(data.get('publish_date'), 'publish_date')
        if bad:
            return bad
    else:
        pub_dt = date.today()
    if due_dt < pub_dt:
        return err('due_date must not be before publish_date')
    description = _text(data, 'description') or None

    duplicate = (Homework.query.execution_options(**_OPTS)
                 .filter_by(school_id=school.id, academic_year_id=year.id,
                            teacher_id=emp.id, subject_id=group.subject_id,
                            section_id=None, institute_group_id=group.id,
                            title=title, publish_date=pub_dt, is_active=True)
                 .first())
    if duplicate is not None:
        return err('duplicate_homework — this homework already exists for this group', 409)

    att_path = att_type = None
    if attachment is not None and attachment.filename:
        att_path, att_type, bad = _store_homework_attachment(attachment)
        if bad:
            return bad

    hw = Homework(
        school_id=school.id, academic_year_id=year.id, teacher_id=emp.id,
        subject_id=group.subject_id,     # derived, never posted
        section_id=None,                 # never both targets
        institute_group_id=group.id,
        title=title, description=description, publish_date=pub_dt, due_date=due_dt,
        attachment_path=att_path, attachment_type=att_type, is_active=True)
    db.session.add(hw)
    db.session.commit()
    _notify_institute_homework(hw, school.id)

    return ok(message='تم إضافة الواجب بنجاح.',
              homework=_institute_homework_dict(
                  hw, group.name, _subject_map(school.id, [hw.subject_id]))), 201


@mobile_api_bp.route('/teacher/institute/homework/<int:homework_id>',
                     methods=['PUT', 'PATCH'])
@jwt_required()
@role_required('teacher')
def teacher_institute_update_homework(homework_id):
    """Update MY institute homework (web homework.edit, institute branch).

    The row must be active, mine (teacher_id), of this institute, and point at
    a group I may still target in the homework's OWN academic year; otherwise
    404. Body (JSON or multipart, optional 'attachment' replaces the file):

      title         str         required
      due_date      YYYY-MM-DD  required
      publish_date  YYYY-MM-DD  optional — unchanged when absent
      description   str         optional (absent/blank clears it)
      group_id      int         optional — unchanged when absent; a new value
                                must be another of my groups (web allows the
                                move within the eligible set)

    school, year and owner are never reassigned; section_id stays NULL and
    the subject follows the group.
    """
    gate = _homework_gate()
    if gate is not None:
        return gate
    emp, school, year = _institute_context()
    if school is None:
        return err('institute_not_available', 404)

    hw = (Homework.query.execution_options(**_OPTS)
          .filter(Homework.id == homework_id, Homework.school_id == school.id,
                  Homework.teacher_id == emp.id, Homework.is_active.is_(True),
                  Homework.institute_group_id.isnot(None))
          .first())
    hw_year = (AcademicYear.query.execution_options(**_OPTS)
               .filter_by(id=hw.academic_year_id, school_id=school.id).first()
               if hw is not None else None)
    eligible = ({grp.id: grp for grp in instructor_groups(school, g.mobile_user, hw_year)}
                if hw_year is not None else {})
    if hw is None or hw.institute_group_id not in eligible:
        return err('homework_not_found', 404)

    data, attachment = _json_or_form()
    title = _text(data, 'title')
    if not title:
        return err('required_field_missing: title')

    group = eligible[hw.institute_group_id]
    if 'group_id' in data:
        group_id, ok_gid = _opt_int(data.get('group_id'))
        if not ok_gid or group_id is None:
            return err('invalid group_id')
        group = eligible.get(group_id)
        if group is None:
            return err('group_not_found', 404)
    refused = _refuse_foreign_target(data, group)
    if refused is not None:
        return refused

    if not _text(data, 'due_date'):
        return err('required_field_missing: due_date')
    due_dt, bad = _parse_date_field(data.get('due_date'), 'due_date')
    if bad:
        return bad
    pub_dt = hw.publish_date
    if _text(data, 'publish_date'):
        pub_dt, bad = _parse_date_field(data.get('publish_date'), 'publish_date')
        if bad:
            return bad
    if pub_dt and due_dt < pub_dt:
        return err('due_date must not be before publish_date')

    new_path, new_type = hw.attachment_path, hw.attachment_type
    if attachment is not None and attachment.filename:
        new_path, new_type, bad = _store_homework_attachment(attachment)
        if bad:
            return bad

    hw.title              = title
    hw.description        = _text(data, 'description') or None
    hw.institute_group_id = group.id
    hw.subject_id         = group.subject_id
    hw.section_id         = None           # an institute row never gains a section
    hw.publish_date       = pub_dt
    hw.due_date           = due_dt
    hw.attachment_path    = new_path
    hw.attachment_type    = new_type
    db.session.commit()

    return ok(homework=_institute_homework_dict(
        hw, group.name, _subject_map(school.id, [hw.subject_id])))

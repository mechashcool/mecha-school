"""
Mobile API — Parent endpoints
==============================
All routes require:  Authorization: Bearer <access_token>   (role: parent)

Endpoint map
────────────
GET /parent/children                          list of linked children
GET /parent/children/<id>                     child profile + quick stats
GET /parent/children/<id>/attendance          attendance history (queryable range)
GET /parent/children/<id>/fees                fee records + installments
GET /parent/children/<id>/grades              exam results (all years)
GET /parent/children/<id>/exams               exams for child's section (±30/60 d)
GET /parent/children/<id>/schedule            weekly class schedule
GET /parent/notifications                     notifications feed (paginated)

Security rules
──────────────
• _assert_owns_student() checks the parent_students junction to verify the
  authenticated parent is actually linked to the requested student_id.
• All queries are additionally guarded by school_id equality so an ID-swap
  across schools is impossible.
"""
from datetime import date, timedelta
from datetime import datetime as _dt

from flask import abort, g, request
from sqlalchemy import select

from app.models import (
    db,
    FeeRecord,
    Exam,
    ExamResult,
    Notification,
    NotificationRead,
    Schedule,
    Student,
    StudentAttendance,
    parent_students,
)
from app.utils.notification_visibility import notification_visible_to

from . import mobile_api_bp
from .utils import jwt_required, role_required, ok, err


# ─── Ownership guard ──────────────────────────────────────────────────────────

def _assert_owns_student(student_id: int) -> Student:
    """
    Verify the authenticated parent is linked to student_id AND the student
    belongs to the parent's school. Returns the Student or raises 404.
    """
    user = g.mobile_user
    link = db.session.execute(
        select(parent_students.c.student_id).where(
            parent_students.c.user_id    == user.id,
            parent_students.c.student_id == student_id,
        )
    ).first()
    if not link:
        abort(404)

    student = db.session.get(Student, student_id)
    if not student or student.school_id != user.school_id:
        abort(404)
    return student


# ─── Shared serialisers ───────────────────────────────────────────────────────

def _student_brief(s: Student) -> dict:
    return {
        'id':         s.id,
        'student_id': s.student_id,
        'name':       s.full_name,
        'photo':      s.photo,
        'gender':     s.gender,
        'section':    s.section.name       if s.section              else None,
        'grade':      s.section.grade.name if s.section and s.section.grade else None,
        'stage':      s.section.grade.stage if s.section and s.section.grade else None,
        'status':     s.status,
    }


def _fmt_time(t) -> str | None:
    return t.strftime('%H:%M') if t else None


# ─── Children list ────────────────────────────────────────────────────────────

@mobile_api_bp.route('/parent/children', methods=['GET'])
@jwt_required()
@role_required('parent')
def parent_children():
    """Return the list of students linked to the authenticated parent."""
    user     = g.mobile_user
    children = [_student_brief(s) for s in user.children]
    return ok(children=children, count=len(children))


# ─── Child profile ────────────────────────────────────────────────────────────

@mobile_api_bp.route('/parent/children/<int:student_id>', methods=['GET'])
@jwt_required()
@role_required('parent')
def parent_child_profile(student_id):
    """Full child profile with attendance snapshot, latest exam, and fee summary."""
    s     = _assert_owns_student(student_id)
    today = date.today()

    # Attendance stats — last 30 days
    since    = today - timedelta(days=30)
    att_rows = (StudentAttendance.query
                .filter_by(student_id=s.id)
                .filter(StudentAttendance.date >= since)
                .all())
    att_stats = {
        'total':   len(att_rows),
        'present': sum(1 for r in att_rows if r.status == 'present'),
        'absent':  sum(1 for r in att_rows if r.status == 'absent'),
        'late':    sum(1 for r in att_rows if r.status == 'late'),
        'excused': sum(1 for r in att_rows if r.status == 'excused'),
    }

    # Latest exam result
    latest_exam = None
    lr = (ExamResult.query
          .execution_options(include_all_years=True)
          .filter_by(student_id=s.id)
          .order_by(ExamResult.id.desc())
          .first())
    if lr and lr.exam:
        e = lr.exam
        latest_exam = {
            'name':      e.display_name,
            'subject':   e.subject.name if e.subject else None,
            'marks':     float(lr.marks) if lr.marks is not None else None,
            'max_marks': float(e.max_marks),
            'grade':     lr.grade_letter,
            'is_pass':   lr.is_pass,
            'date':      e.exam_date.isoformat() if e.exam_date else None,
        }

    # Fee summary across all academic years
    fee_records  = (FeeRecord.query
                    .execution_options(include_all_years=True)
                    .filter_by(student_id=s.id)
                    .all())
    total_fees = sum(float(r.net_amount) for r in fee_records)
    total_paid = sum(float(r.total_paid)  for r in fee_records)

    profile = _student_brief(s)
    profile.update({
        'date_of_birth':      s.date_of_birth.isoformat() if s.date_of_birth else None,
        'nationality':        s.nationality,
        'address':            s.address,
        'phone':              s.phone,
        'enrollment_date':    s.enrollment_date.isoformat() if s.enrollment_date else None,
        'guardian_name':      s.guardian_name,
        'guardian_phone':     s.guardian_phone,
        'guardian_relation':  s.guardian_relation,
    })

    return ok(
        student=profile,
        attendance_last30=att_stats,
        latest_exam=latest_exam,
        fees_summary={
            'total':     total_fees,
            'paid':      total_paid,
            'remaining': total_fees - total_paid,
        },
    )


# ─── Attendance history ───────────────────────────────────────────────────────

@mobile_api_bp.route('/parent/children/<int:student_id>/attendance', methods=['GET'])
@jwt_required()
@role_required('parent')
def parent_child_attendance(student_id):
    """
    Attendance records for a specific date range.
    Query params: start (YYYY-MM-DD), end (YYYY-MM-DD) — default: last 30 days.
    Range is capped at 365 days.
    """
    s   = _assert_owns_student(student_id)
    end = date.today()
    start = end - timedelta(days=30)

    try:
        if request.args.get('start'):
            start = _dt.strptime(request.args['start'], '%Y-%m-%d').date()
        if request.args.get('end'):
            end = _dt.strptime(request.args['end'], '%Y-%m-%d').date()
    except ValueError:
        return err('invalid date format — use YYYY-MM-DD')

    if (end - start).days > 365:
        start = end - timedelta(days=365)

    rows = (StudentAttendance.query
            .execution_options(include_all_years=True)
            .filter_by(student_id=s.id)
            .filter(StudentAttendance.date.between(start, end))
            .order_by(StudentAttendance.date.desc())
            .all())

    summary = {
        'total':   len(rows),
        'present': sum(1 for r in rows if r.status == 'present'),
        'absent':  sum(1 for r in rows if r.status == 'absent'),
        'late':    sum(1 for r in rows if r.status == 'late'),
        'excused': sum(1 for r in rows if r.status == 'excused'),
    }

    return ok(
        student_id=s.id,
        range={'start': start.isoformat(), 'end': end.isoformat()},
        summary=summary,
        records=[
            {
                'date':      r.date.isoformat(),
                'status':    r.status,
                'check_in':  _fmt_time(r.check_in),
                'check_out': _fmt_time(r.check_out),
                'source':    r.source,
                'notes':     r.notes,
            }
            for r in rows
        ],
    )


# ─── Fee records + installments ───────────────────────────────────────────────

@mobile_api_bp.route('/parent/children/<int:student_id>/fees', methods=['GET'])
@jwt_required()
@role_required('parent')
def parent_child_fees(student_id):
    """All fee records with installment detail across all academic years."""
    s = _assert_owns_student(student_id)

    records = (FeeRecord.query
               .execution_options(include_all_years=True)
               .filter_by(student_id=s.id)
               .order_by(FeeRecord.id.desc())
               .all())

    grand_total = grand_paid = 0.0
    records_out = []
    for rec in records:
        net  = float(rec.net_amount)
        paid = float(rec.total_paid)
        grand_total += net
        grand_paid  += paid

        installments = [
            {
                'id':              i.id,
                'no':              i.installment_no,
                'amount':          float(i.amount),
                'received_amount': float(i.received_amount or 0),
                'remaining':       float(i.amount) - float(i.received_amount or 0),
                'due_date':        i.due_date.isoformat() if i.due_date else None,
                'paid_date':       i.paid_date.isoformat() if i.paid_date else None,
                'status':          i.status,
                'receipt_no':      i.receipt_no,
            }
            for i in rec.installments
        ]

        records_out.append({
            'id':           rec.id,
            'fee_type':     rec.fee_type.name if rec.fee_type else None,
            'year':         rec.academic_year.name if rec.academic_year else None,
            'total':        float(rec.total_amount),
            'discount':     float(rec.discount or 0),
            'net':          net,
            'paid':         paid,
            'remaining':    net - paid,
            'installments': installments,
        })

    return ok(
        student_id=s.id,
        summary={
            'total':     grand_total,
            'paid':      grand_paid,
            'remaining': grand_total - grand_paid,
        },
        records=records_out,
    )


# ─── Grades / exam results ────────────────────────────────────────────────────

@mobile_api_bp.route('/parent/children/<int:student_id>/grades', methods=['GET'])
@jwt_required()
@role_required('parent')
def parent_child_grades(student_id):
    """All exam results across all academic years, newest first."""
    s = _assert_owns_student(student_id)

    results = (ExamResult.query
               .execution_options(include_all_years=True)
               .filter_by(student_id=s.id)
               .order_by(ExamResult.id.desc())
               .all())

    return ok(
        student_id=s.id,
        count=len(results),
        results=[
            {
                'id':          r.id,
                'exam':        r.exam.display_name  if r.exam else None,
                'subject':     r.exam.subject.name  if r.exam and r.exam.subject  else None,
                'section':     r.exam.section.name  if r.exam and r.exam.section  else None,
                'grade_label': (r.exam.section.grade.name
                                if r.exam and r.exam.section and r.exam.section.grade
                                else None),
                'exam_date':   r.exam.exam_date.isoformat() if r.exam and r.exam.exam_date else None,
                'max_marks':   float(r.exam.max_marks)  if r.exam else None,
                'pass_marks':  float(r.exam.pass_marks) if r.exam else None,
                'marks':       float(r.marks) if r.marks is not None else None,
                'grade':       r.grade_letter,
                'is_pass':     r.is_pass,
                'rank':        r.rank,
                'notes':       r.notes,
                'year':        r.academic_year.name if r.academic_year else None,
            }
            for r in results
        ],
    )


# ─── Exams for child's section ────────────────────────────────────────────────

@mobile_api_bp.route('/parent/children/<int:student_id>/exams', methods=['GET'])
@jwt_required()
@role_required('parent')
def parent_child_exams(student_id):
    """
    Exams scheduled for the child's section.
    Default window: 30 days before today → 60 days ahead.
    """
    s = _assert_owns_student(student_id)

    if not s.section_id:
        return ok(student_id=s.id, exams=[])

    today = date.today()
    since = today - timedelta(days=30)
    until = today + timedelta(days=60)

    exams = (Exam.query
             .filter_by(section_id=s.section_id)
             .filter(Exam.exam_date.between(since, until))
             .order_by(Exam.exam_date.asc())
             .all())

    return ok(
        student_id=s.id,
        exams=[
            {
                'id':          e.id,
                'name':        e.display_name,
                'subject':     e.subject.name if e.subject else None,
                'exam_date':   e.exam_date.isoformat() if e.exam_date else None,
                'max_marks':   float(e.max_marks),
                'pass_marks':  float(e.pass_marks),
                'is_upcoming': e.exam_date >= today if e.exam_date else None,
            }
            for e in exams
        ],
    )


# ─── Class schedule ───────────────────────────────────────────────────────────

_DAY_NAMES = {
    0: 'الأحد', 1: 'الاثنين', 2: 'الثلاثاء',
    3: 'الأربعاء', 4: 'الخميس', 5: 'الجمعة', 6: 'السبت',
}


@mobile_api_bp.route('/parent/children/<int:student_id>/schedule', methods=['GET'])
@jwt_required()
@role_required('parent')
def parent_child_schedule(student_id):
    """Weekly class schedule for the child's current section."""
    s = _assert_owns_student(student_id)

    if not s.section_id:
        return ok(student_id=s.id, schedule=[])

    schedules = (Schedule.query
                 .filter_by(section_id=s.section_id)
                 .order_by(Schedule.day_of_week, Schedule.start_time)
                 .all())

    return ok(
        student_id=s.id,
        section=s.section.name if s.section else None,
        grade=s.section.grade.name if s.section and s.section.grade else None,
        schedule=[
            {
                'id':           sch.id,
                'day':          sch.day_of_week,
                'day_name':     _DAY_NAMES.get(sch.day_of_week, ''),
                'subject':      sch.subject.name if sch.subject else None,
                'subject_code': sch.subject.code if sch.subject else None,
                'teacher':      sch.teacher.full_name if sch.teacher else None,
                'start_time':   _fmt_time(sch.start_time),
                'end_time':     _fmt_time(sch.end_time),
                'room':         sch.room,
            }
            for sch in schedules
        ],
    )


# ─── Notifications ────────────────────────────────────────────────────────────

@mobile_api_bp.route('/parent/notifications', methods=['GET'])
@jwt_required()
@role_required('parent')
def parent_notifications():
    """
    Paginated notifications visible to this parent.
    Query params: limit (default 50, max 100), offset (default 0).
    """
    user   = g.mobile_user
    limit  = min(int(request.args.get('limit', 50)),  100)
    offset = max(int(request.args.get('offset', 0)),  0)

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
                'sent_at': n.created_at.isoformat() if n.created_at else None,
            }
            for n in rows
        ],
    )

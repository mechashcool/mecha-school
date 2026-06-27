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
from datetime import date, timedelta, timezone
from datetime import datetime as _dt

from flask import abort, g, request
from sqlalchemy import select

from app.models import (
    db,
    AcademicYear,
    Complaint,
    FeeRecord,
    Exam,
    ExamResult,
    Homework,
    LeaveRequest,
    Notification,
    NotificationRead,
    Schedule,
    Student,
    StudentAttendance,
    StudentTransport,
    parent_students,
)
from app.utils.helpers import save_uploaded_file, delete_uploaded_file
from app.utils.notification_visibility import notification_visible_to

from . import mobile_api_bp
from .utils import jwt_required, role_required, ok, err, photo_url


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
        'photo':      photo_url(s.photo),
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
    _on_leave_30 = sum(1 for r in att_rows if r.status == 'on_leave')
    _billable_30 = len(att_rows) - _on_leave_30
    att_stats = {
        'total':    len(att_rows),
        'present':  sum(1 for r in att_rows if r.status == 'present'),
        'absent':   sum(1 for r in att_rows if r.status == 'absent'),
        'late':     sum(1 for r in att_rows if r.status == 'late'),
        'on_leave': _on_leave_30,
        'excused':  sum(1 for r in att_rows if r.status == 'excused'),
        'att_pct':  round(
            (sum(1 for r in att_rows if r.status in ('present', 'late')) / _billable_30 * 100), 1
        ) if _billable_30 > 0 else 0.0,
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

    _on_leave_rng = sum(1 for r in rows if r.status == 'on_leave')
    _billable_rng = len(rows) - _on_leave_rng
    summary = {
        'total':    len(rows),
        'present':  sum(1 for r in rows if r.status == 'present'),
        'absent':   sum(1 for r in rows if r.status == 'absent'),
        'late':     sum(1 for r in rows if r.status == 'late'),
        'on_leave': _on_leave_rng,
        'excused':  sum(1 for r in rows if r.status == 'excused'),
        'att_pct':  round(
            (sum(1 for r in rows if r.status in ('present', 'late')) / _billable_rng * 100), 1
        ) if _billable_rng > 0 else 0.0,
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
    # Grade fallback: when the school manages timetables by grade (no per-section
    # schedule), return the grade-level schedule for the student's grade.
    if not schedules and s.section and s.section.grade_id:
        schedules = (Schedule.query
                     .filter_by(grade_id=s.section.grade_id, section_id=None)
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

    # Explicit school_id guard: notification_visible_to() filters by user/role
    # but has no school filter. Without this, role-broadcast notifications from
    # other schools could appear in the feed (the ORM scope is now set correctly
    # for mobile, but the explicit guard is defence-in-depth).
    q     = (Notification.query
             .filter(
                 Notification.school_id == user.school_id,
                 notification_visible_to(user),
             )
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


# ─── Homework for child ───────────────────────────────────────────────────────

@mobile_api_bp.route('/parent/children/<int:student_id>/homework', methods=['GET'])
@jwt_required()
@role_required('parent')
def parent_child_homework(student_id):
    """
    Homework assigned to the child's current section.
    Returns active, published homework for the active academic year only.

    Blocked if the school's homework module is disabled (api_access action).
    """
    from app.utils.school_config import get_school_config
    user = g.mobile_user
    cfg  = get_school_config(user.school_id)
    if not cfg.action_enabled('homework', 'api_access'):
        return err('الوصول إلى الواجبات غير مفعل لهذه المدرسة.', 403)

    s = _assert_owns_student(student_id)

    if not s.section_id:
        return ok(student_id=s.id, count=0, homework=[])

    # Resolve the school's active academic year. bypass_tenant_scope=True + explicit
    # school_id guard so this lookup is deterministic regardless of ORM scope state.
    year = (AcademicYear.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(school_id=user.school_id, is_current=True)
            .first())

    today = date.today()
    q = (Homework.query
         .execution_options(bypass_tenant_scope=True)
         .filter_by(section_id=s.section_id, is_active=True, school_id=user.school_id))
    if year:
        q = q.filter_by(academic_year_id=year.id)
    # Only show homework that has been published (publish_date <= today)
    q = q.filter(Homework.publish_date <= today)
    rows = q.order_by(Homework.publish_date.desc(), Homework.id.desc()).all()

    def _hw_url(hw):
        if not hw.attachment_path:
            return None
        path = hw.attachment_path
        if path.startswith(('http://', 'https://')):
            return path
        return photo_url(path)

    def _hw_file_name(path):
        if not path:
            return None
        import os
        from urllib.parse import urlparse
        if path.startswith(('http://', 'https://')):
            return os.path.basename(urlparse(path).path) or None
        return os.path.basename(path) or None

    return ok(
        student_id=s.id,
        section=s.section.name if s.section else None,
        count=len(rows),
        homework=[
            {
                'id':              hw.id,
                'homework_id':     hw.id,
                'title':           hw.title,
                'subject':         hw.subject.name if hw.subject else None,
                'subject_name':    hw.subject.name if hw.subject else None,
                'teacher_name':    hw.teacher.full_name if hw.teacher else None,
                'grade_name':      (hw.section.grade.name
                                    if hw.section and hw.section.grade else None),
                'section_name':    hw.section.name if hw.section else None,
                'assigned_at':     hw.publish_date.isoformat() if hw.publish_date else None,
                'publish_date':    hw.publish_date.isoformat() if hw.publish_date else None,
                'due_date':        hw.due_date.isoformat() if hw.due_date else None,
                'description':     hw.description,
                'status':          'active' if hw.is_active else 'inactive',
                'attachment_url':  _hw_url(hw),
                'attachment_type': hw.attachment_type,
                'file_name':       _hw_file_name(hw.attachment_path),
                'file_size':       None,
                'is_pdf':          hw.attachment_type == 'pdf',
                'submitted_status': 'not_submitted',
            }
            for hw in rows
        ],
    )


# ─── Leave Requests & Complaints — labels ─────────────────────────────────────

_LEAVE_TYPES = {
    'sick':      'إجازة مرضية',
    'medical':   'موعد طبي',
    'family':    'ظرف عائلي',
    'travel':    'سفر',
    'emergency': 'طارئ',
    'other':     'أخرى',
}
_LEAVE_STATUS = {
    'pending':  'قيد الانتظار',
    'approved': 'موافق عليه',
    'rejected': 'مرفوض',
}
_COMPLAINT_TYPES = {
    'academic':       'أكاديمية',
    'administrative': 'إدارية',
    'financial':      'مالية',
    'transportation': 'النقل',
    'behavior':       'سلوكية',
    'other':          'أخرى',
}
_COMPLAINT_STATUS = {
    'new':          'جديدة',
    'under_review': 'قيد المراجعة',
    'replied':      'تم الرد',
    'closed':       'مغلقة',
}

# ─── Student leave-request attachment config ──────────────────────────────────

_ALLOWED_LEAVE_EXTS   = {'pdf', 'jpg', 'jpeg', 'png'}
_MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024  # 15 MB
_LEAVE_BUCKET         = 'uploads'
_EXT_TO_MIME = {
    'pdf':  'application/pdf',
    'jpg':  'image/jpeg',
    'jpeg': 'image/jpeg',
    'png':  'image/png',
}


def _sniff_leave_content(file_bytes: bytes, ext: str) -> bool:
    """Return True when file_bytes actually match the declared extension.

    Checks the PDF magic header for PDFs; uses Pillow to verify image
    format for JPEG/PNG. Rejects renamed or spoofed files (e.g. .exe → .png).
    """
    if ext == 'pdf':
        return file_bytes[:5] == b'%PDF-'
    if ext in {'jpg', 'jpeg', 'png'}:
        try:
            import io
            from PIL import Image
            with Image.open(io.BytesIO(file_bytes)) as img:
                fmt = (img.format or '').lower()
            allowed = {'jpeg'} if ext in {'jpg', 'jpeg'} else {'png'}
            return fmt in allowed
        except Exception:
            return False
    return False


def _validate_leave_attachment(file):
    """Validate an uploaded attachment FileStorage.

    Returns (ext, size_bytes, None) on success or (None, None, error_code)
    on failure. Rewinds the file stream to position 0 before returning so
    the caller can pass the FileStorage directly to save_uploaded_file().
    """
    filename = file.filename or ''
    if '.' not in filename:
        return None, None, 'invalid_attachment_type'
    ext = filename.rsplit('.', 1)[1].lower()
    if ext not in _ALLOWED_LEAVE_EXTS:
        return None, None, 'invalid_attachment_type'

    file_bytes = file.read()
    if not file_bytes:
        return None, None, 'invalid_attachment_type'
    if len(file_bytes) > _MAX_ATTACHMENT_BYTES:
        return None, None, 'attachment_too_large'
    if not _sniff_leave_content(file_bytes, ext):
        return None, None, 'invalid_attachment_type'

    file.seek(0)
    return ext, len(file_bytes), None


def _leave_dict(r) -> dict:
    return {
        'id':               r.id,
        'student_id':       r.student_id,
        'student_name':     r.student.full_name if r.student else None,
        'leave_type':       r.leave_type,
        'leave_type_label': _LEAVE_TYPES.get(r.leave_type, r.leave_type),
        'start_date':       r.from_date.isoformat() if r.from_date else None,
        'end_date':         r.to_date.isoformat() if r.to_date else None,
        'reason':           r.notes,
        'status':           r.status,
        'status_label':     _LEAVE_STATUS.get(r.status, r.status),
        'admin_note':       r.manager_note,
        'source':           r.source or 'parent',
        'created_at':       (r.created_at.replace(tzinfo=timezone.utc).isoformat()
                             if r.created_at else None),
        'attachment': {
            'url':       photo_url(r.attachment_path),
            'file_name': r.attachment_name,
            'mime_type': r.attachment_mime,
            'size':      r.attachment_size,
        } if r.attachment_path else None,
    }


def _complaint_dict(c) -> dict:
    return {
        'id':             c.id,
        'student_id':     c.student_id,
        'student_name':   c.student.full_name if c.student else None,
        'category':       c.complaint_type,
        'category_label': _COMPLAINT_TYPES.get(c.complaint_type, c.complaint_type),
        'title':          c.title,
        'body':           c.details,
        'status':         c.status,
        'status_label':   _COMPLAINT_STATUS.get(c.status, c.status),
        'admin_reply':    c.manager_reply,
        'created_at':     (c.created_at.replace(tzinfo=timezone.utc).isoformat()
                           if c.created_at else None),
        'updated_at':     (c.updated_at.replace(tzinfo=timezone.utc).isoformat()
                           if c.updated_at else None),
    }


def _notify_managers_mobile(user, school_id: int, title: str, body: str) -> None:
    db.session.add(Notification(
        school_id=school_id,
        title=title,
        body=body,
        ntype='parent_request',
        target_role='school_admin',
        created_by=user.id,
    ))


# ─── Leave Request routes ─────────────────────────────────────────────────────

@mobile_api_bp.route('/parent/children/<int:student_id>/leave-requests', methods=['GET'])
@jwt_required()
@role_required('parent')
def parent_child_leave_requests(student_id):
    s    = _assert_owns_student(student_id)
    user = g.mobile_user
    rows = (LeaveRequest.query
            .execution_options(bypass_tenant_scope=True, include_all_years=True)
            .filter_by(student_id=s.id, school_id=user.school_id)
            .order_by(LeaveRequest.created_at.desc())
            .all())
    return ok(count=len(rows), requests=[_leave_dict(r) for r in rows])


@mobile_api_bp.route('/parent/children/<int:student_id>/leave-requests', methods=['POST'])
@jwt_required()
@role_required('parent')
def parent_child_create_leave_request(student_id):
    s    = _assert_owns_student(student_id)
    user = g.mobile_user

    # Support multipart/form-data (new clients, optional attachment) and
    # application/json (existing clients, no attachment) for backward compat.
    ct = request.content_type or ''
    if 'multipart/form-data' in ct:
        form       = request.form
        leave_type = (form.get('leave_type') or '').strip()
        start_str  = (form.get('start_date') or '').strip()
        end_str    = (form.get('end_date')   or '').strip()
        reason     = (form.get('reason')     or '').strip() or None
    else:
        data       = request.get_json(silent=True) or {}
        leave_type = (data.get('leave_type') or '').strip()
        start_str  = (data.get('start_date') or '').strip()
        end_str    = (data.get('end_date')   or '').strip()
        reason     = (data.get('reason')     or '').strip() or None

    if not leave_type:
        return err('required_field_missing: leave_type')
    if leave_type not in _LEAVE_TYPES:
        return err('invalid_leave_type')
    if not start_str:
        return err('required_field_missing: start_date')
    if not end_str:
        return err('required_field_missing: end_date')

    try:
        from_date = _dt.strptime(start_str, '%Y-%m-%d').date()
    except ValueError:
        return err('invalid_date_format: start_date')
    try:
        to_date = _dt.strptime(end_str, '%Y-%m-%d').date()
    except ValueError:
        return err('invalid_date_format: end_date')

    if to_date < from_date:
        return err('end_date_before_start_date')

    if not s.academic_year_id:
        return err('no_active_academic_year_for_student')

    # Optional attachment — only possible in multipart requests.
    attachment_path = None
    attachment_name = None
    attachment_mime = None
    attachment_size = None
    if 'multipart/form-data' in ct:
        file = request.files.get('attachment')
        if file and file.filename:
            ext, size, verr = _validate_leave_attachment(file)
            if verr:
                return err(verr)
            subfolder = f'schools/{s.school_id}/student-leave-requests/{s.id}'
            attachment_path = save_uploaded_file(
                file, subfolder,
                bucket=_LEAVE_BUCKET,
                allowed_exts=_ALLOWED_LEAVE_EXTS,
                max_size=_MAX_ATTACHMENT_BYTES,
            )
            if not attachment_path:
                return err('attachment_upload_failed')
            # Strip any client-supplied path components; store filename for display only.
            attachment_name = (file.filename or '').replace('\\', '/').rsplit('/', 1)[-1][:255]
            attachment_mime = _EXT_TO_MIME.get(ext, 'application/octet-stream')
            attachment_size = size

    leave = LeaveRequest(
        parent_id=user.id,
        student_id=s.id,
        school_id=s.school_id,
        academic_year_id=s.academic_year_id,
        leave_type=leave_type,
        from_date=from_date,
        to_date=to_date,
        notes=reason,
        attachment_path=attachment_path,
        attachment_name=attachment_name,
        attachment_mime=attachment_mime,
        attachment_size=attachment_size,
        status='pending',
    )
    db.session.add(leave)
    _notify_managers_mobile(user, s.school_id,
                            'طلب إجازة جديد',
                            f'تم تقديم طلب إجازة للطالب {s.full_name}.')
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        # Orphan prevention: remove the uploaded object if the DB write failed.
        if attachment_path:
            delete_uploaded_file(attachment_path, bucket=_LEAVE_BUCKET)
        return err('server_error', 500)
    return ok(message='leave_request_created', request=_leave_dict(leave)), 201


@mobile_api_bp.route('/parent/children/<int:student_id>/leave-requests/<int:request_id>',
                     methods=['GET'])
@jwt_required()
@role_required('parent')
def parent_child_leave_request_detail(student_id, request_id):
    s    = _assert_owns_student(student_id)
    user = g.mobile_user
    leave = (LeaveRequest.query
             .execution_options(bypass_tenant_scope=True, include_all_years=True)
             .filter_by(id=request_id, student_id=s.id, school_id=user.school_id)
             .first())
    if not leave:
        return err('leave_request_not_found', 404)
    return ok(request=_leave_dict(leave))


@mobile_api_bp.route('/parent/children/<int:student_id>/leave-requests/<int:request_id>',
                     methods=['DELETE'])
@jwt_required()
@role_required('parent')
def parent_child_delete_leave_request(student_id, request_id):
    s    = _assert_owns_student(student_id)
    user = g.mobile_user
    leave = (LeaveRequest.query
             .execution_options(bypass_tenant_scope=True, include_all_years=True)
             .filter_by(id=request_id, parent_id=user.id,
                        student_id=s.id, school_id=user.school_id)
             .first())
    if not leave:
        return err('leave_request_not_found', 404)
    if leave.status != 'pending':
        return err('cannot_cancel_non_pending_request')
    attachment_path = leave.attachment_path
    db.session.delete(leave)
    db.session.commit()
    # Best-effort storage cleanup after the DB row is confirmed gone.
    if attachment_path:
        delete_uploaded_file(attachment_path, bucket=_LEAVE_BUCKET)
    return ok(message='leave_request_deleted')


# ─── Complaint routes ─────────────────────────────────────────────────────────

@mobile_api_bp.route('/parent/children/<int:student_id>/complaints', methods=['GET'])
@jwt_required()
@role_required('parent')
def parent_child_complaints(student_id):
    s    = _assert_owns_student(student_id)
    user = g.mobile_user
    rows = (Complaint.query
            .execution_options(bypass_tenant_scope=True, include_all_years=True)
            .filter_by(parent_id=user.id, student_id=s.id, school_id=user.school_id)
            .order_by(Complaint.created_at.desc())
            .all())
    return ok(count=len(rows), complaints=[_complaint_dict(c) for c in rows])


@mobile_api_bp.route('/parent/children/<int:student_id>/complaints', methods=['POST'])
@jwt_required()
@role_required('parent')
def parent_child_create_complaint(student_id):
    s    = _assert_owns_student(student_id)
    user = g.mobile_user
    data = request.get_json(silent=True) or {}

    category = (data.get('category') or '').strip()
    title    = (data.get('title')    or '').strip()
    body     = (data.get('body')     or '').strip()

    if not category:
        return err('required_field_missing: category')
    if category not in _COMPLAINT_TYPES:
        return err('invalid_category')
    if not title:
        return err('required_field_missing: title')
    if not body:
        return err('required_field_missing: body')

    if not s.academic_year_id:
        return err('no_active_academic_year_for_student')

    complaint = Complaint(
        parent_id=user.id,
        student_id=s.id,
        school_id=s.school_id,
        academic_year_id=s.academic_year_id,
        title=title,
        complaint_type=category,
        details=body,
        status='new',
    )
    db.session.add(complaint)
    _notify_managers_mobile(user, s.school_id,
                            'شكوى جديدة',
                            f'تم تقديم شكوى جديدة خاصة بالطالب {s.full_name}.')
    db.session.commit()
    return ok(message='complaint_created', complaint=_complaint_dict(complaint)), 201


@mobile_api_bp.route('/parent/children/<int:student_id>/complaints/<int:complaint_id>',
                     methods=['GET'])
@jwt_required()
@role_required('parent')
def parent_child_complaint_detail(student_id, complaint_id):
    s    = _assert_owns_student(student_id)
    user = g.mobile_user
    complaint = (Complaint.query
                 .execution_options(bypass_tenant_scope=True, include_all_years=True)
                 .filter_by(id=complaint_id, parent_id=user.id,
                             student_id=s.id, school_id=user.school_id)
                 .first())
    if not complaint:
        return err('complaint_not_found', 404)
    return ok(complaint=_complaint_dict(complaint))


@mobile_api_bp.route('/parent/children/<int:student_id>/complaints/<int:complaint_id>',
                     methods=['DELETE'])
@jwt_required()
@role_required('parent')
def parent_child_delete_complaint(student_id, complaint_id):
    s    = _assert_owns_student(student_id)
    user = g.mobile_user
    complaint = (Complaint.query
                 .execution_options(bypass_tenant_scope=True, include_all_years=True)
                 .filter_by(id=complaint_id, parent_id=user.id,
                             student_id=s.id, school_id=user.school_id)
                 .first())
    if not complaint:
        return err('complaint_not_found', 404)
    if complaint.status != 'new':
        return err('cannot_delete_non_new_complaint')
    db.session.delete(complaint)
    db.session.commit()
    return ok(message='complaint_deleted')


# ─── Transportation ───────────────────────────────────────────────────────────

@mobile_api_bp.route('/parent/children/<int:student_id>/transportation', methods=['GET'])
@jwt_required()
@role_required('parent')
def parent_child_transportation(student_id):
    """
    Active transport route assigned to the authenticated parent's child.

    Security:
      • _assert_owns_student() verifies the parent_students junction (parent
        owns the child) AND student.school_id == user.school_id before any
        transport data is accessed. A 404 is returned for unowned children so
        the response does not reveal whether another parent's child exists.
      • The StudentTransport query adds explicit school_id, student_id, and
        status='active' filters. The ORM global tenant scope IS active for
        authenticated mobile requests (jwt_required calls
        set_mobile_request_scope() after token validation), so these explicit
        column filters are defence-in-depth — they must not be removed.
      • The loaded route's school_id is re-verified against the authenticated
        user's school as defense-in-depth against any FK-only trust.
      • No internal IDs, license plates, route internals, or sensitive vehicle
        data are exposed; only driver_name, phone, and vehicle_name are returned.
      • StudentTransport is not year-scoped (no academic_year_id column) — the
        transport assignment is a school-level record, consistent with the web
        transport module.

    200 – child has active transport:
      { "ok": true, "transportation": {"driver_name": "...", "phone": "...", "vehicle_name": "..."} }

    200 – no active transport assignment:
      { "ok": true, "transportation": null }
    """
    user    = g.mobile_user
    student = _assert_owns_student(student_id)

    # Find the student's active transport assignment.
    # Explicit school_id + student_id + status prevents cross-school leakage
    # and excludes inactive/cancelled subscriptions.
    link = (StudentTransport.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(
                school_id  = user.school_id,
                student_id = student.id,
                status     = 'active',
            )
            .first())

    if not link:
        return ok(transportation=None)

    route = link.route
    # Defense-in-depth: the route must belong to the parent's school.
    # This guards against a StudentTransport row whose route_id FK resolves
    # to a route from a different school (should be impossible by schema but
    # is checked explicitly to fail closed).
    if not route or route.school_id != user.school_id:
        return ok(transportation=None)

    return ok(
        transportation={
            'driver_name':  route.driver_name,
            'phone':        route.driver_phone,
            # vehicle_type is the make/model string (e.g. "تويوتا هايس").
            # vehicle_number is the licence plate — excluded per spec.
            'vehicle_name': route.vehicle_type,
        },
    )

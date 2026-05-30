"""
Mecha-School — Mobile/Parent JSON API
=====================================
JSON endpoints for the parent mobile app (Flutter) AND the web parent
portal's AJAX widgets. Kept apart from session-auth blueprints so we
can swap in a JWT layer later without disturbing HTML flows.

Auth model (Phase 3):
  * Endpoints under `/api/v1/parent/*` require a logged-in parent user.
  * For the mobile app we rely on Flask-Login session cookies (the app
    already calls /auth/login). A future JWT migration is planned.

All responses are JSON. 401 when anonymous, 403 when not a parent,
404 when the child is not linked to the caller.
"""
import re
from datetime import date, timedelta
from functools import wraps

from flask import Blueprint, jsonify, request, abort
from flask_login import login_required, current_user
from sqlalchemy import func

from app.models import (db, Student, FeeRecord, FeeInstallment,
                         StudentAttendance, ExamResult, Exam,
                         Notification, User, SchoolSettings,
                         parent_students)
from app.utils.notification_visibility import notification_visible_to
from app.services.attendance_service import process_student_scan

api_bp = Blueprint('api', __name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def parent_required(f):
    """Allow only authenticated users whose role.name == 'parent'."""
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({'ok': False, 'error': 'unauthorized'}), 401
        if not current_user.role or current_user.role.name != 'parent':
            return jsonify({'ok': False, 'error': 'parent role required'}), 403
        return f(*args, **kwargs)
    return wrapper


def _assert_parent_of(student_id: int) -> Student:
    """Raise 404 if the current parent is not linked to this student."""
    link = db.session.query(parent_students.c.student_id).filter(
        parent_students.c.user_id == current_user.id,
        parent_students.c.student_id == student_id
    ).first()
    if not link:
        abort(404)
    return Student.query.get_or_404(student_id)


# ─── Device source → Arabic display helpers ───────────────────────────────────

_SOURCE_LABELS = {
    'hikvision': 'جهاز الحضور',
    'rfid':      'جهاز الحضور',
    'face_id':   'بصمة الوجه',
}


def _friendly_source(src):
    return _SOURCE_LABELS.get(src or '', src)


def _friendly_notes(notes, src):
    """Strip internal hik:sn=... dedup tags; replace with Arabic label for device sources."""
    cleaned = re.sub(r'hik:sn=\S+', '', notes or '').strip()
    if not cleaned and src in _SOURCE_LABELS:
        return 'تم التسجيل عبر جهاز الحضور'
    return cleaned or None


# ─────────────────────────────────────────────────────────────────────────────
#  Profile
# ─────────────────────────────────────────────────────────────────────────────

@api_bp.route('/v1/parent/me', methods=['GET'])
@parent_required
def me():
    children = [
        {
            'id':         s.id,
            'student_id': s.student_id,
            'name':       s.full_name,
            'photo':      s.photo,
            'section':    s.section.name if s.section else None,
            'grade':      s.section.grade.name if s.section and s.section.grade else None,
        }
        for s in current_user.children
    ]
    school = SchoolSettings.get()
    return jsonify({
        'ok': True,
        'user': {
            'id':       current_user.id,
            'name':     current_user.full_name,
            'email':    current_user.email,
            'phone':    current_user.phone,
            'locale':   current_user.locale,
        },
        'school': {
            'name':    school.school_name,
            'logo':    school.logo_path,
            'color':   school.primary_color,
            'currency': school.currency_symbol,
        },
        'children': children,
    })


@api_bp.route('/v1/parent/register-device', methods=['POST'])
@parent_required
def register_device():
    """Flutter app calls this after FCM gives it a token."""
    payload = request.get_json(silent=True) or {}
    token = (payload.get('device_token') or '').strip()
    if not token:
        return jsonify({'ok': False, 'error': 'device_token required'}), 400

    current_user.device_token = token
    if payload.get('locale'):
        current_user.locale = payload['locale'][:10]
    db.session.commit()
    return jsonify({'ok': True})


# ─────────────────────────────────────────────────────────────────────────────
#  Per-child data
# ─────────────────────────────────────────────────────────────────────────────

@api_bp.route('/v1/parent/children/<int:student_id>/attendance', methods=['GET'])
@parent_required
def child_attendance(student_id):
    s = _assert_parent_of(student_id)

    # Default window: last 30 days
    end = date.today()
    start = end - timedelta(days=30)
    if request.args.get('start'):
        try:
            from datetime import datetime as dt
            start = dt.strptime(request.args['start'], '%Y-%m-%d').date()
        except ValueError:
            pass
    if request.args.get('end'):
        try:
            from datetime import datetime as dt
            end = dt.strptime(request.args['end'], '%Y-%m-%d').date()
        except ValueError:
            pass

    rows = (StudentAttendance.query
            .filter_by(student_id=s.id)
            .filter(StudentAttendance.date.between(start, end))
            .order_by(StudentAttendance.date.desc()).all())

    summary = {
        'present': sum(1 for r in rows if r.status == 'present'),
        'absent':  sum(1 for r in rows if r.status == 'absent'),
        'late':    sum(1 for r in rows if r.status == 'late'),
        'excused': sum(1 for r in rows if r.status == 'excused'),
    }

    return jsonify({
        'ok': True,
        'student_id': s.id,
        'range': {'start': start.isoformat(), 'end': end.isoformat()},
        'summary': summary,
        'records': [
            {
                'date':     r.date.isoformat(),
                'status':   r.status,
                'check_in':  r.check_in.strftime('%H:%M')  if r.check_in  else None,
                'check_out': r.check_out.strftime('%H:%M') if r.check_out else None,
                'source':   _friendly_source(r.source),
                'notes':    _friendly_notes(r.notes, r.source),
            }
            for r in rows
        ],
    })


@api_bp.route('/v1/parent/children/<int:student_id>/fees', methods=['GET'])
@parent_required
def child_fees(student_id):
    s = _assert_parent_of(student_id)
    records = FeeRecord.query.filter_by(student_id=s.id).all()

    out = []
    grand_total = grand_paid = 0.0
    for rec in records:
        inst_list = []
        for i in rec.installments:
            inst_list.append({
                'id':               i.id,
                'no':               i.installment_no,
                'amount':           float(i.amount),
                'received_amount':  float(i.received_amount or 0),
                'remaining':        float(i.amount) - float(i.received_amount or 0),
                'due_date':         i.due_date.isoformat() if i.due_date else None,
                'paid_date':        i.paid_date.isoformat() if i.paid_date else None,
                'status':           i.status,
                'receipt_no':       i.receipt_no,
            })
        total_paid = float(rec.total_paid)
        total      = float(rec.net_amount)
        grand_total += total
        grand_paid  += total_paid
        out.append({
            'id':          rec.id,
            'fee_type':    rec.fee_type.name if rec.fee_type else None,
            'year':        rec.academic_year.name if rec.academic_year else None,
            'total':       total,
            'paid':        total_paid,
            'remaining':   total - total_paid,
            'installments': inst_list,
        })

    return jsonify({
        'ok': True,
        'student_id': s.id,
        'summary': {
            'total':     grand_total,
            'paid':      grand_paid,
            'remaining': grand_total - grand_paid,
        },
        'records': out,
    })


@api_bp.route('/v1/parent/children/<int:student_id>/grades', methods=['GET'])
@parent_required
def child_grades(student_id):
    s = _assert_parent_of(student_id)
    results = (ExamResult.query
               .filter_by(student_id=s.id)
               .order_by(ExamResult.id.desc()).all())
    return jsonify({
        'ok': True,
        'student_id': s.id,
        'results': [
            {
                'exam':       r.exam.display_name if r.exam else None,
                'subject':    r.exam.subject.name if r.exam and r.exam.subject else None,
                'max_marks':  float(r.exam.max_marks) if r.exam else None,
                'marks':      float(r.marks) if r.marks is not None else None,
                'grade':      r.grade_letter,
                'remarks':    r.notes,
                'date':       r.exam.exam_date.isoformat() if r.exam and r.exam.exam_date else None,
            }
            for r in results
        ],
    })


# ─────────────────────────────────────────────────────────────────────────────
#  Parent notifications feed
# ─────────────────────────────────────────────────────────────────────────────

@api_bp.route('/v1/parent/announcements', methods=['GET'])
@api_bp.route('/v1/parent/notifications', methods=['GET'])
@parent_required
def notifications_feed():
    rows = (Notification.query
            .filter(notification_visible_to(current_user))
            .order_by(Notification.created_at.desc())
            .limit(50).all())
    data = [
        {
            'id':       n.id,
            'title':    n.title,
            'body':     n.body,
            'ntype':    n.ntype,
            'sent_at':  n.created_at.isoformat() if n.created_at else None,
            'created_at': n.created_at.isoformat() if n.created_at else None,
        }
        for n in rows
    ]
    return jsonify({
        'ok': True,
        'notifications': data,
        # Backward-compatible key for older clients using the legacy URL.
        'announcements': data,
    })


# ─────────────────────────────────────────────────────────────────────────────
#  Attendance — device API endpoint
# ─────────────────────────────────────────────────────────────────────────────

@api_bp.route('/v1/attendance/student', methods=['POST'])
def api_record_attendance():
    data = request.get_json(silent=True) or {}
    result, status_code = process_student_scan(
        data.get('student_id'),
        data.get('device_sn'),
    )
    return jsonify(result), status_code
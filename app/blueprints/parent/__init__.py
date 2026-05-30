"""
Mecha-School — Parent Web Portal
================================
Web-facing dashboard for logged-in parent users. Shows their children's
attendance, fees, grades, and notifications.

Uses the same session auth as the rest of the admin ERP — a parent's
User row just has role.name='parent', which gates access via the
`parent_required` decorator below.
"""
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Blueprint, render_template, redirect, url_for,
                   flash, abort, request)
from flask_login import login_required, current_user
from sqlalchemy import func

from app.models import (db, Student, FeeRecord, FeeInstallment,
                         StudentAttendance, ExamResult, Notification,
                         Schedule, parent_students, Complaint, LeaveRequest)
from app.utils.helpers import save_uploaded_file
from app.utils.notification_visibility import notification_visible_to

parent_bp = Blueprint('parent', __name__,
                       template_folder='../../templates/parent')

COMPLAINT_TYPES = {
    'academic': '\u0623\u0643\u0627\u062f\u064a\u0645\u064a\u0629',
    'administrative': '\u0625\u062f\u0627\u0631\u064a\u0629',
    'financial': '\u0645\u0627\u0644\u064a\u0629',
    'transportation': '\u0627\u0644\u0646\u0642\u0644',
    'behavior': '\u0633\u0644\u0648\u0643\u064a\u0629',
    'other': '\u0623\u062e\u0631\u0649',
}

COMPLAINT_STATUS = {
    'new': '\u062c\u062f\u064a\u062f\u0629',
    'under_review': '\u0642\u064a\u062f \u0627\u0644\u0645\u0631\u0627\u062c\u0639\u0629',
    'replied': '\u062a\u0645 \u0627\u0644\u0631\u062f',
    'closed': '\u0645\u063a\u0644\u0642\u0629',
}

LEAVE_TYPES = {
    'sick': '\u0625\u062c\u0627\u0632\u0629 \u0645\u0631\u0636\u064a\u0629',
    'medical': '\u0645\u0648\u0639\u062f \u0637\u0628\u064a',
    'family': '\u0638\u0631\u0641 \u0639\u0627\u0626\u0644\u064a',
    'travel': '\u0633\u0641\u0631',
    'emergency': '\u0637\u0627\u0631\u0626',
    'other': '\u0623\u062e\u0631\u0649',
}

LEAVE_STATUS = {
    'pending': '\u0642\u064a\u062f \u0627\u0644\u0627\u0646\u062a\u0638\u0627\u0631',
    'approved': '\u0645\u0648\u0627\u0641\u0642 \u0639\u0644\u064a\u0647',
    'rejected': '\u0645\u0631\u0641\u0648\u0636',
}


def parent_required(f):
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.role or current_user.role.name != 'parent':
            flash('هذه الصفحة مخصصة لأولياء الأمور فقط.', 'warning')
            return redirect(url_for('admin.dashboard'))
        return f(*args, **kwargs)
    return wrapper


def _owned_student(student_id: int) -> Student:
    link = db.session.query(parent_students.c.student_id).filter(
        parent_students.c.user_id == current_user.id,
        parent_students.c.student_id == student_id
    ).first()
    if not link:
        abort(404)
    return Student.query.get_or_404(student_id)


def _children():
    return sorted(list(current_user.children), key=lambda s: s.full_name or '')


def _parse_form_date(value):
    try:
        return datetime.strptime(value or '', '%Y-%m-%d').date()
    except ValueError:
        return None


def _save_optional_attachment(field_name, subfolder, prefix):
    file = request.files.get(field_name)
    if not file or not file.filename:
        return None, None
    saved = save_uploaded_file(file, subfolder, prefix=prefix)
    if not saved:
        return None, '\u0627\u0644\u0645\u0631\u0641\u0642 \u063a\u064a\u0631 \u0635\u0627\u0644\u062d.'
    return saved, None


def _notify_school_managers(school_id, title, body):
    db.session.add(Notification(
        school_id=school_id,
        title=title,
        body=body,
        ntype='parent_request',
        target_role='school_admin',
        created_by=current_user.id,
    ))


# ─────────────────────────────────────────────────────────────────────────────
#  Landing / overview
# ─────────────────────────────────────────────────────────────────────────────

@parent_bp.route('/')
@parent_bp.route('/dashboard')
@parent_required
def dashboard():
    today = date.today()
    children = list(current_user.children)

    # Per-child summary card data
    summaries = []
    for s in children:
        # 30-day attendance
        since = today - timedelta(days=30)
        atts = (StudentAttendance.query
                .filter_by(student_id=s.id)
                .filter(StudentAttendance.date >= since).all())
        present = sum(1 for r in atts if r.status == 'present')
        total   = len(atts) or 1

        # Fees
        fee_total = fee_paid = 0.0
        for rec in s.fee_records:
            fee_total += float(rec.net_amount)
            fee_paid  += float(rec.total_paid)

        # Most recent exam mark
        latest_result = (ExamResult.query
                         .filter_by(student_id=s.id)
                         .order_by(ExamResult.id.desc()).first())

        summaries.append({
            'student':    s,
            'att_rate':   round(present / total * 100, 1),
            'fee_total':  fee_total,
            'fee_paid':   fee_paid,
            'fee_due':    fee_total - fee_paid,
            'last_exam':  latest_result,
        })

    recent_notifications = (Notification.query
                            .filter(notification_visible_to(current_user))
                            .order_by(Notification.created_at.desc())
                            .limit(5).all())

    return render_template('parent/dashboard.html',
                           summaries=summaries,
                           recent_notifications=recent_notifications,
                           today=today)


# ─────────────────────────────────────────────────────────────────────────────
#  Per-child detail views
# ─────────────────────────────────────────────────────────────────────────────

@parent_bp.route('/child/<int:student_id>')
@parent_required
def child_overview(student_id):
    s     = _owned_student(student_id)
    today = date.today()
    since = today - timedelta(days=60)

    atts = (StudentAttendance.query
            .filter_by(student_id=s.id)
            .filter(StudentAttendance.date >= since)
            .order_by(StudentAttendance.date.desc()).all())

    att_stats = {
        'total':   len(atts),
        'present': sum(1 for r in atts if r.status == 'present'),
        'absent':  sum(1 for r in atts if r.status == 'absent'),
        'late':    sum(1 for r in atts if r.status == 'late'),
    }
    att_stats['pct'] = (
        round(att_stats['present'] / att_stats['total'] * 100, 1)
        if att_stats['total'] else 0
    )

    fee_records = list(s.fee_records)
    results = (ExamResult.query
               .filter_by(student_id=s.id)
               .order_by(ExamResult.id.desc()).limit(30).all())

    schedule = []
    if s.section_id:
        schedule = (Schedule.query
                    .filter_by(section_id=s.section_id)
                    .order_by(Schedule.day_of_week, Schedule.start_time)
                    .all())

    return render_template('parent/child.html',
                           s=s, atts=atts, att_stats=att_stats,
                           fee_records=fee_records, results=results,
                           schedule=schedule, today=today)


@parent_bp.route('/complaints')
@parent_required
def complaints():
    q = (Complaint.query
         .filter_by(parent_id=current_user.id)
         .order_by(Complaint.created_at.desc()))
    return render_template('parent/complaints.html',
                           complaints=q.all(),
                           status_labels=COMPLAINT_STATUS,
                           type_labels=COMPLAINT_TYPES)


@parent_bp.route('/complaints/new', methods=['GET', 'POST'])
@parent_required
def create_complaint():
    children = _children()
    if request.method == 'POST':
        student_id = request.form.get('student_id', type=int)
        student = _owned_student(student_id) if student_id else None
        title = request.form.get('title', '').strip()
        complaint_type = request.form.get('complaint_type', '').strip()
        details = request.form.get('details', '').strip()
        attachment, upload_error = _save_optional_attachment(
            'attachment', 'complaints', f'complaint-{current_user.id}'
        )

        errors = []
        if not student:
            errors.append('\u064a\u0631\u062c\u0649 \u0627\u062e\u062a\u064a\u0627\u0631 \u0627\u0644\u0637\u0627\u0644\u0628.')
        if not title:
            errors.append('\u0639\u0646\u0648\u0627\u0646 \u0627\u0644\u0634\u0643\u0648\u0649 \u0645\u0637\u0644\u0648\u0628.')
        if complaint_type not in COMPLAINT_TYPES:
            errors.append('\u0646\u0648\u0639 \u0627\u0644\u0634\u0643\u0648\u0649 \u063a\u064a\u0631 \u0635\u0627\u0644\u062d.')
        if not details:
            errors.append('\u062a\u0641\u0627\u0635\u064a\u0644 \u0627\u0644\u0634\u0643\u0648\u0649 \u0645\u0637\u0644\u0648\u0628\u0629.')
        if upload_error:
            errors.append(upload_error)
        if student and not student.academic_year_id:
            errors.append('\u0644\u0627 \u064a\u0648\u062c\u062f \u0639\u0627\u0645 \u062f\u0631\u0627\u0633\u064a \u0645\u0631\u062a\u0628\u0637 \u0628\u0627\u0644\u0637\u0627\u0644\u0628.')

        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template('parent/complaint_form.html',
                                   children=children,
                                   type_labels=COMPLAINT_TYPES), 400

        complaint = Complaint(
            parent_id=current_user.id,
            student_id=student.id,
            school_id=student.school_id,
            academic_year_id=student.academic_year_id,
            title=title,
            complaint_type=complaint_type,
            details=details,
            attachment_path=attachment,
            status='new',
        )
        db.session.add(complaint)
        _notify_school_managers(
            student.school_id,
            '\u0634\u0643\u0648\u0649 \u062c\u062f\u064a\u062f\u0629',
            f'\u062a\u0645 \u062a\u0642\u062f\u064a\u0645 \u0634\u0643\u0648\u0649 \u062c\u062f\u064a\u062f\u0629 \u062e\u0627\u0635\u0629 \u0628\u0627\u0644\u0637\u0627\u0644\u0628 {student.full_name}.',
        )
        db.session.commit()
        flash('\u062a\u0645 \u0625\u0631\u0633\u0627\u0644 \u0627\u0644\u0634\u0643\u0648\u0649 \u0628\u0646\u062c\u0627\u062d.', 'success')
        return redirect(url_for('parent.complaints'))

    return render_template('parent/complaint_form.html',
                           children=children,
                           type_labels=COMPLAINT_TYPES)


@parent_bp.route('/complaints/<int:complaint_id>')
@parent_required
def complaint_detail(complaint_id):
    complaint = (Complaint.query
                 .filter_by(id=complaint_id, parent_id=current_user.id)
                 .first_or_404())
    return render_template('parent/complaint_detail.html',
                           complaint=complaint,
                           status_labels=COMPLAINT_STATUS,
                           type_labels=COMPLAINT_TYPES)


@parent_bp.route('/leave-requests')
@parent_required
def leave_requests():
    q = (LeaveRequest.query
         .filter_by(parent_id=current_user.id)
         .order_by(LeaveRequest.created_at.desc()))
    return render_template('parent/leave_requests.html',
                           requests=q.all(),
                           status_labels=LEAVE_STATUS,
                           type_labels=LEAVE_TYPES)


@parent_bp.route('/leave-requests/new', methods=['GET', 'POST'])
@parent_required
def create_leave_request():
    children = _children()
    if request.method == 'POST':
        student_id = request.form.get('student_id', type=int)
        student = _owned_student(student_id) if student_id else None
        leave_type = request.form.get('leave_type', '').strip()
        from_date = _parse_form_date(request.form.get('from_date'))
        to_date = _parse_form_date(request.form.get('to_date'))
        notes = request.form.get('notes', '').strip() or None
        attachment, upload_error = _save_optional_attachment(
            'attachment', 'leave_requests', f'leave-{current_user.id}'
        )

        errors = []
        if not student:
            errors.append('\u064a\u0631\u062c\u0649 \u0627\u062e\u062a\u064a\u0627\u0631 \u0627\u0644\u0637\u0627\u0644\u0628.')
        if leave_type not in LEAVE_TYPES:
            errors.append('\u0646\u0648\u0639 \u0627\u0644\u0625\u062c\u0627\u0632\u0629 \u063a\u064a\u0631 \u0635\u0627\u0644\u062d.')
        if not from_date:
            errors.append('\u062a\u0627\u0631\u064a\u062e \u0627\u0644\u0628\u062f\u0627\u064a\u0629 \u0645\u0637\u0644\u0648\u0628.')
        if not to_date:
            errors.append('\u062a\u0627\u0631\u064a\u062e \u0627\u0644\u0646\u0647\u0627\u064a\u0629 \u0645\u0637\u0644\u0648\u0628.')
        if from_date and to_date and to_date < from_date:
            errors.append('\u062a\u0627\u0631\u064a\u062e \u0627\u0644\u0646\u0647\u0627\u064a\u0629 \u064a\u062c\u0628 \u0623\u0646 \u064a\u0643\u0648\u0646 \u0628\u0639\u062f \u062a\u0627\u0631\u064a\u062e \u0627\u0644\u0628\u062f\u0627\u064a\u0629.')
        if upload_error:
            errors.append(upload_error)
        if student and not student.academic_year_id:
            errors.append('\u0644\u0627 \u064a\u0648\u062c\u062f \u0639\u0627\u0645 \u062f\u0631\u0627\u0633\u064a \u0645\u0631\u062a\u0628\u0637 \u0628\u0627\u0644\u0637\u0627\u0644\u0628.')

        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template('parent/leave_request_form.html',
                                   children=children,
                                   type_labels=LEAVE_TYPES), 400

        leave_request = LeaveRequest(
            parent_id=current_user.id,
            student_id=student.id,
            school_id=student.school_id,
            academic_year_id=student.academic_year_id,
            leave_type=leave_type,
            from_date=from_date,
            to_date=to_date,
            notes=notes,
            attachment_path=attachment,
            status='pending',
        )
        db.session.add(leave_request)
        _notify_school_managers(
            student.school_id,
            '\u0637\u0644\u0628 \u0625\u062c\u0627\u0632\u0629 \u062c\u062f\u064a\u062f',
            f'\u062a\u0645 \u062a\u0642\u062f\u064a\u0645 \u0637\u0644\u0628 \u0625\u062c\u0627\u0632\u0629 \u0644\u0644\u0637\u0627\u0644\u0628 {student.full_name}.',
        )
        db.session.commit()
        flash('\u062a\u0645 \u0625\u0631\u0633\u0627\u0644 \u0637\u0644\u0628 \u0627\u0644\u0625\u062c\u0627\u0632\u0629 \u0628\u0646\u062c\u0627\u062d.', 'success')
        return redirect(url_for('parent.leave_requests'))

    return render_template('parent/leave_request_form.html',
                           children=children,
                           type_labels=LEAVE_TYPES)


@parent_bp.route('/leave-requests/<int:request_id>')
@parent_required
def leave_request_detail(request_id):
    leave_request = (LeaveRequest.query
                     .filter_by(id=request_id, parent_id=current_user.id)
                     .first_or_404())
    return render_template('parent/leave_request_detail.html',
                           leave_request=leave_request,
                           status_labels=LEAVE_STATUS,
                           type_labels=LEAVE_TYPES)


@parent_bp.route('/announcements')
@parent_required
def announcements():
    return redirect(url_for('notifications.index'))

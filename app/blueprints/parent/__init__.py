"""
Mecha-School — Parent Web Portal
================================
Web-facing dashboard for logged-in parent users. Shows their children's
attendance, fees, grades, and notifications.

Uses the same session auth as the rest of the admin ERP — a parent's
User row just has role.name='parent', which gates access via the
`parent_required` decorator below.
"""
from datetime import date, timedelta
from functools import wraps

from flask import (Blueprint, render_template, redirect, url_for,
                   flash, abort, request)
from flask_login import login_required, current_user
from sqlalchemy import func

from app.models import (db, Student, FeeRecord, FeeInstallment,
                         StudentAttendance, ExamResult, Notification,
                         Schedule, parent_students)
from app.utils.notification_visibility import notification_visible_to

parent_bp = Blueprint('parent', __name__,
                       template_folder='../../templates/parent')


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


@parent_bp.route('/announcements')
@parent_required
def announcements():
    return redirect(url_for('notifications.index'))

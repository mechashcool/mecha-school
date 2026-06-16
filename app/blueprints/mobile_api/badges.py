"""
Mobile API — Unified badge counts and module-view acknowledgement
=================================================================
GET  /me/badge-counts                       all unread counts in one response
POST /me/mark-module-viewed/<module>        reset badge for an allowed module

Authentication
──────────────
Both endpoints require a valid access token.  No IDs (school_id, user_id,
parent_id, teacher_id, academic_year_id) are accepted from the client — every
scope dimension is resolved from the server-side JWT.

Security guarantees
───────────────────
• school_id is always taken from user.school_id (JWT payload), never from the
  client.  All queries carry an explicit school_id equality filter.
• Parent counts are further scoped to children explicitly linked to the parent
  (parent_students junction), never to the whole school.
• Teacher counts are scoped to the employee record tied to the authenticated
  user's user_id.
• mark-module-viewed only modifies the authenticated user's own rows
  (WHERE user_id = current user).  IDOR is impossible.
• Unknown module names are rejected with 400 before any DB write.
• All count queries use aggregate COUNT — no N+1 loops.

Badge semantics per module
──────────────────────────
notifications   Notification rows visible to this user with no NotificationRead
messages        ChatMessage rows not read by this user across all their rooms
school_board    SchoolVideo + SchoolAnnouncement rows not yet in SchoolContentRead
grades          ExamResult rows created after last_viewed_at  (current year, parent only)
homework        Homework rows created after last_viewed_at    (current year, parent only)
exams           Exam rows created after last_viewed_at        (current year, parent only)
attendance      StudentAttendance rows created after last_viewed_at (current year, parent only)
fees            FeeInstallment rows updated after last_viewed_at   (current year, parent only)
leave_requests  Leave requests whose status changed since last_viewed_at (all years)
                  parent  → LeaveRequest (status != 'pending')
                  teacher → EmployeeLeaveRequest (status != 'pending')
complaints      Complaint rows whose status changed since last_viewed_at
                  parent only (status != 'new', all years)
"""
from datetime import datetime

from flask import g
from sqlalchemy import func

from app.models import (
    db,
    AcademicYear,
    ChatMessage,
    ChatMessageRead,
    ChatRoom,
    ChatRoomMember,
    Complaint,
    Employee,
    EmployeeLeaveRequest,
    Exam,
    ExamResult,
    FeeInstallment,
    FeeRecord,
    Homework,
    LeaveRequest,
    MobileModuleView,
    Notification,
    NotificationRead,
    SchoolAnnouncement,
    SchoolContentRead,
    SchoolVideo,
    StudentAttendance,
)
from app.utils.notification_visibility import notification_visible_to

from . import mobile_api_bp
from .utils import jwt_required, role_required, ok, err

# ── Constants ─────────────────────────────────────────────────────────────────

ALLOWED_MODULES = frozenset({
    'grades', 'homework', 'exams', 'attendance',
    'fees', 'leave_requests', 'complaints',
})


# ── Private helpers ───────────────────────────────────────────────────────────

def _active_year_id(school_id: int) -> int | None:
    """Return the id of the currently active academic year, or None."""
    return (
        AcademicYear.query
        .execution_options(bypass_tenant_scope=True)
        .with_entities(AcademicYear.id)
        .filter_by(school_id=school_id, is_current=True)
        .scalar()
    )


def _audience_values(role_name: str) -> tuple:
    """Return the SchoolVideo/SchoolAnnouncement audience values visible to role."""
    if role_name == 'parent':
        return ('parents', 'all')
    if role_name == 'teacher':
        return ('teachers', 'all')
    return ()


def _notification_badge(user) -> int:
    """Unread notification count for user — single aggregate query."""
    return (
        db.session.query(func.count(Notification.id))
        .execution_options(bypass_tenant_scope=True)
        .outerjoin(
            NotificationRead,
            (NotificationRead.notification_id == Notification.id) &
            (NotificationRead.user_id == user.id),
        )
        .filter(
            Notification.school_id == user.school_id,
            notification_visible_to(user),
            NotificationRead.id.is_(None),
        )
        .scalar() or 0
    )


def _message_badge(user) -> int:
    """Total unread messages across all chat rooms — single aggregate query."""
    return (
        db.session.query(func.count(ChatMessage.id))
        .join(
            ChatRoom,
            (ChatRoom.id == ChatMessage.room_id) &
            (ChatRoom.school_id == user.school_id) &
            (ChatRoom.is_active == True),
        )
        .join(
            ChatRoomMember,
            (ChatRoomMember.room_id == ChatMessage.room_id) &
            (ChatRoomMember.user_id == user.id) &
            (ChatRoomMember.is_blocked == False),
        )
        .outerjoin(
            ChatMessageRead,
            (ChatMessageRead.message_id == ChatMessage.id) &
            (ChatMessageRead.user_id == user.id),
        )
        .filter(
            ChatMessage.is_deleted == False,
            ChatMessage.sender_user_id != user.id,
            ChatMessageRead.id.is_(None),
        )
        .scalar() or 0
    )


def _school_board_badge(user, audiences: tuple) -> int:
    """Count unread school-board items (videos + announcements) — two aggregate queries."""
    if not audiences:
        return 0

    now = datetime.utcnow()

    videos = (
        db.session.query(func.count(SchoolVideo.id))
        .execution_options(bypass_tenant_scope=True)
        .outerjoin(
            SchoolContentRead,
            (SchoolContentRead.content_id == SchoolVideo.id) &
            (SchoolContentRead.content_type == 'video') &
            (SchoolContentRead.user_id == user.id),
        )
        .filter(
            SchoolVideo.school_id == user.school_id,
            SchoolVideo.is_active == True,
            SchoolVideo.audience.in_(audiences),
            (SchoolVideo.publish_at.is_(None)) | (SchoolVideo.publish_at <= now),
            (SchoolVideo.expires_at.is_(None)) | (SchoolVideo.expires_at > now),
            SchoolContentRead.id.is_(None),
        )
        .scalar() or 0
    )

    anns = (
        db.session.query(func.count(SchoolAnnouncement.id))
        .execution_options(bypass_tenant_scope=True)
        .outerjoin(
            SchoolContentRead,
            (SchoolContentRead.content_id == SchoolAnnouncement.id) &
            (SchoolContentRead.content_type == 'announcement') &
            (SchoolContentRead.user_id == user.id),
        )
        .filter(
            SchoolAnnouncement.school_id == user.school_id,
            SchoolAnnouncement.is_active == True,
            SchoolAnnouncement.audience.in_(audiences),
            (SchoolAnnouncement.publish_at.is_(None)) | (SchoolAnnouncement.publish_at <= now),
            (SchoolAnnouncement.expires_at.is_(None)) | (SchoolAnnouncement.expires_at > now),
            SchoolContentRead.id.is_(None),
        )
        .scalar() or 0
    )

    return videos + anns


# ── Routes ────────────────────────────────────────────────────────────────────

@mobile_api_bp.route('/me/badge-counts', methods=['GET'])
@jwt_required()
@role_required('parent', 'teacher')
def badge_counts():
    """
    Return unread counts for all badge modules in one response.

    All counts are derived from the authenticated user's school and identity.
    No client-supplied IDs are trusted.
    """
    user      = g.mobile_user
    role_name = user.role.name if user.role else None
    school_id = user.school_id

    # ── resolve current academic year once ────────────────────────────────────
    year_id = _active_year_id(school_id)

    # ── fetch all per-user module views in one query ──────────────────────────
    views: dict[str, datetime] = {
        v.module: v.last_viewed_at
        for v in MobileModuleView.query.filter_by(user_id=user.id).all()
    }

    # ── always-available counts (parent + teacher) ────────────────────────────
    audiences    = _audience_values(role_name)
    notif_count  = _notification_badge(user)
    msg_count    = _message_badge(user)
    board_count  = _school_board_badge(user, audiences)

    # ── initialise all module counts to 0 ────────────────────────────────────
    grades_count = homework_count = exams_count = 0
    att_count    = fees_count = leave_count = complaint_count = 0

    # ── parent-specific counts ────────────────────────────────────────────────
    if role_name == 'parent':
        # Resolve linked children scoped to this school.
        child_ids = [c.id for c in user.children if c.school_id == school_id]

        if child_ids and year_id:
            # Unique section IDs across all linked children (for homework/exams).
            section_ids = list({
                c.section_id for c in user.children
                if c.school_id == school_id and c.section_id is not None
            })

            # grades: new ExamResult rows since last view
            last_grades = views.get('grades')
            if last_grades is not None:
                grades_count = (
                    db.session.query(func.count(ExamResult.id))
                    .execution_options(bypass_tenant_scope=True)
                    .filter(
                        ExamResult.student_id.in_(child_ids),
                        ExamResult.school_id == school_id,
                        ExamResult.academic_year_id == year_id,
                        ExamResult.created_at > last_grades,
                    )
                    .scalar() or 0
                )

            # homework: new Homework rows since last view
            last_hw = views.get('homework')
            if last_hw is not None and section_ids:
                homework_count = (
                    db.session.query(func.count(Homework.id))
                    .execution_options(bypass_tenant_scope=True)
                    .filter(
                        Homework.section_id.in_(section_ids),
                        Homework.school_id == school_id,
                        Homework.academic_year_id == year_id,
                        Homework.is_active == True,
                        Homework.created_at > last_hw,
                    )
                    .scalar() or 0
                )

            # exams: new Exam rows since last view
            last_exams = views.get('exams')
            if last_exams is not None and section_ids:
                exams_count = (
                    db.session.query(func.count(Exam.id))
                    .execution_options(bypass_tenant_scope=True)
                    .filter(
                        Exam.section_id.in_(section_ids),
                        Exam.school_id == school_id,
                        Exam.academic_year_id == year_id,
                        Exam.created_at > last_exams,
                    )
                    .scalar() or 0
                )

            # attendance: new StudentAttendance rows since last view
            last_att = views.get('attendance')
            if last_att is not None:
                att_count = (
                    db.session.query(func.count(StudentAttendance.id))
                    .execution_options(bypass_tenant_scope=True)
                    .filter(
                        StudentAttendance.student_id.in_(child_ids),
                        StudentAttendance.school_id == school_id,
                        StudentAttendance.academic_year_id == year_id,
                        StudentAttendance.created_at > last_att,
                    )
                    .scalar() or 0
                )

            # fees: FeeInstallment rows updated since last view
            # (new installments or status changes after payment)
            last_fees = views.get('fees')
            if last_fees is not None:
                fees_count = (
                    db.session.query(func.count(FeeInstallment.id))
                    .execution_options(bypass_tenant_scope=True)
                    .join(FeeRecord,
                          FeeInstallment.fee_record_id == FeeRecord.id)
                    .filter(
                        FeeInstallment.school_id == school_id,
                        FeeInstallment.academic_year_id == year_id,
                        FeeRecord.student_id.in_(child_ids),
                        FeeRecord.school_id == school_id,
                        FeeInstallment.updated_at > last_fees,
                    )
                    .scalar() or 0
                )

        # leave_requests: status changed from 'pending' since last view
        # Scoped across all years so no year_id filter (consistent with list endpoint).
        last_leave = views.get('leave_requests')
        if last_leave is not None:
            leave_count = (
                db.session.query(func.count(LeaveRequest.id))
                .execution_options(bypass_tenant_scope=True)
                .filter(
                    LeaveRequest.parent_id == user.id,
                    LeaveRequest.school_id == school_id,
                    LeaveRequest.status != 'pending',
                    LeaveRequest.updated_at > last_leave,
                )
                .scalar() or 0
            )

        # complaints: status changed from 'new' since last view
        # Same all-years policy as leave requests.
        last_complaint = views.get('complaints')
        if last_complaint is not None:
            complaint_count = (
                db.session.query(func.count(Complaint.id))
                .execution_options(bypass_tenant_scope=True)
                .filter(
                    Complaint.parent_id == user.id,
                    Complaint.school_id == school_id,
                    Complaint.status != 'new',
                    Complaint.updated_at > last_complaint,
                )
                .scalar() or 0
            )

    # ── teacher-specific counts ───────────────────────────────────────────────
    elif role_name == 'teacher':
        last_leave = views.get('leave_requests')
        if last_leave is not None:
            # Resolve employee record for this user — never trust a client ID.
            emp = (
                Employee.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(user_id=user.id, school_id=school_id)
                .first()
            )
            if emp:
                leave_count = (
                    db.session.query(func.count(EmployeeLeaveRequest.id))
                    .execution_options(bypass_tenant_scope=True)
                    .filter(
                        EmployeeLeaveRequest.employee_id == emp.id,
                        EmployeeLeaveRequest.school_id == school_id,
                        EmployeeLeaveRequest.status != 'pending',
                        EmployeeLeaveRequest.updated_at > last_leave,
                    )
                    .scalar() or 0
                )

    return ok(badges={
        'notifications':  notif_count,
        'messages':       msg_count,
        'grades':         grades_count,
        'homework':       homework_count,
        'exams':          exams_count,
        'attendance':     att_count,
        'fees':           fees_count,
        'school_board':   board_count,
        'leave_requests': leave_count,
        'complaints':     complaint_count,
    })


@mobile_api_bp.route('/me/mark-module-viewed/<module>', methods=['POST'])
@jwt_required()
@role_required('parent', 'teacher')
def mark_module_viewed(module):
    """
    Set last_viewed_at = now() for the specified module.

    Only the authenticated user's own row is written — IDOR is impossible
    because user_id is taken from the JWT, not the request body.
    Unknown module names are rejected before any DB access.
    """
    if module not in ALLOWED_MODULES:
        return err('unknown_module', 400)

    user = g.mobile_user
    view = MobileModuleView.query.filter_by(user_id=user.id, module=module).first()
    now  = datetime.utcnow()

    if view:
        view.last_viewed_at = now
    else:
        view = MobileModuleView(
            user_id=user.id,
            school_id=user.school_id,
            module=module,
            last_viewed_at=now,
        )
        db.session.add(view)

    db.session.commit()
    return ok(module=module, last_viewed_at=now.strftime('%Y-%m-%dT%H:%M:%S') + '+00:00')

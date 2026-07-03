"""
Mobile API — Notification read-state endpoints
=================================================
POST /notifications/<notification_id>/read   mark one notification read
POST /notifications/read-all                 mark all visible notifications read

Security guarantees
───────────────────
• school_id is always taken from user.school_id (JWT payload), never from the client.
• Single-read: the notification is looked up by id AND school_id AND visibility
  before the read receipt is written — IDOR is prevented at the query level.
• Bulk read-all: uses a LEFT JOIN subquery to insert only missing rows for
  notifications visible to the authenticated user within their school, so it
  cannot touch other schools' data.
• No client-supplied IDs are trusted.
"""
from flask import g
from sqlalchemy import and_, insert, select
from sqlalchemy.exc import IntegrityError

from app.models import db, Notification, NotificationRead
from app.utils.notification_visibility import notification_visible_to

from . import mobile_api_bp
from .utils import jwt_required, role_required, ok, err


@mobile_api_bp.route('/notifications/<int:notification_id>/read', methods=['POST'])
@jwt_required()
@role_required('parent', 'teacher')
def notification_mark_read(notification_id):
    """
    Mark a single notification as read for the authenticated user.

    The notification is scoped by both school_id and the notification_visible_to
    filter before the read receipt is written.  If the notification does not
    exist, does not belong to this school, or is not visible to this user,
    a 404 is returned — the same response as for an invalid id, so no record
    existence is leaked.
    """
    user = g.mobile_user
    # Apply the same broadcast cutoff as the notification list endpoint so that
    # a teacher cannot mark a pre-account broadcast notification as read
    # (it is not visible in their list and should not be addressable here either).
    role_name = user.role.name if user.role else None
    cutoff    = user.created_at if role_name == 'teacher' else None

    notif = (
        Notification.query
        .execution_options(bypass_tenant_scope=True)
        .filter(
            Notification.id == notification_id,
            Notification.school_id == user.school_id,
            notification_visible_to(user, cutoff_dt=cutoff),
        )
        .first()
    )
    if not notif:
        return err('not_found', 404)

    existing = NotificationRead.query.filter_by(
        notification_id=notif.id,
        user_id=user.id,
    ).first()

    if not existing:
        db.session.add(NotificationRead(
            notification_id=notif.id,
            user_id=user.id,
        ))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()

    return ok(notification_id=notif.id)


@mobile_api_bp.route('/notifications/read-all', methods=['POST'])
@jwt_required()
@role_required('parent', 'teacher')
def notification_read_all():
    """
    Mark every visible notification as read for the authenticated user.

    Uses a single INSERT … SELECT with a LEFT JOIN anti-pattern to bulk-insert
    only the missing read receipts — no Python loop, no N+1 queries.

    The SELECT is scoped to:
      • Notification.school_id == user.school_id
      • notification_visible_to(user)   (direct target or role broadcast)
      • LEFT JOIN NotificationRead WHERE user_id = user.id → only rows with no receipt
    """
    user = g.mobile_user
    # Apply the same broadcast cutoff as the notification list endpoint so that
    # "read all" does not create read receipts for pre-account broadcast
    # notifications that are invisible in the teacher's notification list.
    role_name = user.role.name if user.role else None
    cutoff    = user.created_at if role_name == 'teacher' else None

    # Build a SELECT of (notification_id, user_id) for all unread visible notifications.
    unread_ids_subq = (
        select(Notification.id)
        .outerjoin(
            NotificationRead,
            and_(
                NotificationRead.notification_id == Notification.id,
                NotificationRead.user_id == user.id,
            ),
        )
        .where(
            Notification.school_id == user.school_id,
            notification_visible_to(user, cutoff_dt=cutoff),
            NotificationRead.id.is_(None),
        )
        .execution_options(bypass_tenant_scope=True)
    )

    rows = db.session.execute(unread_ids_subq).scalars().all()
    if not rows:
        return ok(marked=0)

    # Bulk insert read receipts — ignore duplicates (shouldn't happen but be safe).
    try:
        db.session.execute(
            insert(NotificationRead),
            [{'notification_id': nid, 'user_id': user.id} for nid in rows],
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # A concurrent request won the race — that is acceptable; all were marked.

    return ok(marked=len(rows))

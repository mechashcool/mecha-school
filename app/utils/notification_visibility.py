"""Visibility rules for in-app notifications."""
from sqlalchemy import and_, or_

from app.models import Notification


def notification_visible_to(user, cutoff_dt=None):
    """Return the SQLAlchemy filter for notifications addressed to ``user``.

    A row with ``target_user_id`` is always treated as a direct notification,
    even if older data accidentally also has ``target_role`` populated.
    Role broadcasts only apply when no specific user is targeted.

    cutoff_dt: when provided, broadcast notifications (target_user_id IS NULL)
    created before this datetime are excluded.  Direct notifications
    (target_user_id == user.id) are never affected by the cutoff.
    Pass ``user.created_at`` to prevent a new account from inheriting
    pre-existing broadcast history.
    """
    role_name = user.role.name if user.role else None
    audience_filters = [Notification.target_role.is_(None)]
    if role_name:
        audience_filters.append(Notification.target_role == role_name)

    broadcast_conditions = [
        Notification.target_user_id.is_(None),
        or_(*audience_filters),
    ]
    if cutoff_dt is not None:
        broadcast_conditions.append(Notification.created_at >= cutoff_dt)

    return or_(
        Notification.target_user_id == user.id,
        and_(*broadcast_conditions),
    )


def notification_is_addressed_to(notification, user) -> bool:
    """Return True when a loaded notification is part of ``user``'s inbox."""
    if notification.target_user_id is not None:
        return notification.target_user_id == user.id

    role_name = user.role.name if user.role else None
    return notification.target_role is None or notification.target_role == role_name

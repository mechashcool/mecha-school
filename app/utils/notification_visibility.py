"""Visibility rules for in-app notifications."""
from sqlalchemy import and_, or_

from app.models import Notification


def notification_visible_to(user):
    """Return the SQLAlchemy filter for notifications addressed to ``user``.

    A row with ``target_user_id`` is always treated as a direct notification,
    even if older data accidentally also has ``target_role`` populated.
    Role broadcasts only apply when no specific user is targeted.
    """
    role_name = user.role.name if user.role else None
    audience_filters = [Notification.target_role.is_(None)]
    if role_name:
        audience_filters.append(Notification.target_role == role_name)

    return or_(
        Notification.target_user_id == user.id,
        and_(
            Notification.target_user_id.is_(None),
            or_(*audience_filters),
        ),
    )


def notification_is_addressed_to(notification, user) -> bool:
    """Return True when a loaded notification is part of ``user``'s inbox."""
    if notification.target_user_id is not None:
        return notification.target_user_id == user.id

    role_name = user.role.name if user.role else None
    return notification.target_role is None or notification.target_role == role_name

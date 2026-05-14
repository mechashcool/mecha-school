"""
Al-Muhandis – Audit Logging Helper
Call log_action() inside any route to record what happened.
"""
from flask import request
from flask_login import current_user
from app.models import db, AuditLog
from app.utils.scoping import current_school_id


def log_action(action: str, resource: str = None,
               resource_id: int = None, details: str = None):
    """
    Insert an audit log entry.
    Safe to call even if the user is not authenticated (e.g. login events).
    """
    try:
        user_id = current_user.id if current_user.is_authenticated else None
        entry = AuditLog(
            school_id   = current_school_id(),
            user_id     = user_id,
            action      = action,
            resource    = resource,
            resource_id = resource_id,
            details     = details,
            ip_address  = request.remote_addr,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        # Never let audit logging crash the main request
        db.session.rollback()

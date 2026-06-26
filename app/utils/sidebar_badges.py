"""Single source of truth for the sidebar/topbar badge counts.

Both the page-render context processor (``inject_globals`` in ``app/__init__.py``)
and the live-polling endpoint (``live.badges``) call :func:`get_badge_counts` so
the count logic, tenant isolation, and badge semantics never drift between the
two surfaces.

Counts returned (always all four keys, integers):
    * ``unread_notifications``     — in-app notifications addressed to the user
                                     and not yet read.
    * ``pending_complaints``       — complaints awaiting action (admin: whole
                                     school; parent: only their own).
    * ``pending_leave_requests``   — leave requests awaiting action (same scope
                                     rule as complaints).
    * ``unread_chat``              — chat messages in the user's rooms, from
                                     other senders, not yet read.

Caching:
    * ``live=False`` (page render): reuses the established 45 s ``badge_cache``
      keys so the navigation-performance caching is preserved unchanged.
    * ``live=True`` (polling): a short TTL on a *separate* key namespace so the
      poll reflects near-current state (bounded staleness) without disturbing
      the page-render cache or its keys.

Isolation guarantees (identical to the original inline logic):
    * Notification counts use ``notification_visible_to`` + the per-user read
      set, scoped by ``g.tenant_scope_school_id`` (the school the request is
      bound to — for a super admin this is their selected school, or none).
    * Complaint/leave counts are filtered by explicit ``school_id`` and, for a
      parent, by ``parent_id`` — never crossing schools or users.
    * Chat membership and read state are strictly per user.
Every count runs in its own guard: a failing query yields 0 for that count and
rolls back, exactly as the original context processor did.
"""
from __future__ import annotations

from app.models import db
from app.utils import badge_cache

# Live polling tolerates a small amount of staleness in exchange for not
# hammering the database when many tabs/users poll at once. Kept short so a
# single visible tab sees fresh values, and so any residual staleness clears
# quickly; a state-changing action additionally invalidates this user's keys
# (see invalidate_user_badges) for immediate correctness on the acting worker.
LIVE_TTL = 5  # seconds


def _empty_counts() -> dict:
    return {
        'unread_notifications': 0,
        'pending_complaints': 0,
        'pending_leave_requests': 0,
        'pending_employee_leave_requests': 0,
        'unread_chat': 0,
    }


def invalidate_user_badges(user) -> None:
    """Drop the cached badge counts that ``user`` could be affected by.

    Called after a state-changing request so the user who just read / replied /
    resolved / approved / rejected an item sees the badge update immediately
    (on the worker that handled the action) instead of waiting for the TTL.

    Matches both cache namespaces — the page-render keys (45 s) and the live
    poll keys (``live:`` prefix, short TTL) — but only the keys whose value this
    user influences:
      * notifications / chat keys carry the user id in position 1,
      * a parent's complaint/leave keys end with the user id,
      * an admin's complaint/leave keys are ``(base, school_id, 'admin')``.
    Other users' keys and the read-only navigation cache for unrelated users are
    never touched, so this does not add broad recompute traffic.
    """
    from flask import g

    if not getattr(user, 'is_authenticated', False):
        return

    uid = user.id
    is_admin = bool(getattr(user, 'is_admin_user', False))
    sid = getattr(g, 'tenant_scope_school_id', None)
    if sid is None:
        sid = getattr(user, 'school_id', None)

    def _matches(key):
        if not isinstance(key, tuple) or not key or not isinstance(key[0], str):
            return False
        base = key[0][5:] if key[0].startswith('live:') else key[0]
        if base in ('notif', 'chat'):
            return len(key) > 1 and key[1] == uid
        if base in ('complaints', 'leave', 'emp_leave'):
            # Parent keys: (base, sid, 'parent', uid). Admin keys: (base, sid, 'admin').
            if len(key) >= 4 and key[3] == uid:
                return True
            if (is_admin and sid is not None
                    and len(key) >= 3 and key[1] == sid and key[2] == 'admin'):
                return True
        return False

    badge_cache.invalidate(_matches)


def get_badge_counts(*, live: bool = False) -> dict:
    """Return the four badge counts for ``current_user`` in the current scope.

    Safe to call from any authenticated request context. Returns zeros for
    counts that cannot be computed (e.g. no school context) without raising.
    """
    from flask import g
    from flask_login import current_user

    counts = _empty_counts()

    if not getattr(current_user, 'is_authenticated', False):
        return counts

    prefix = 'live:' if live else ''
    ttl = LIVE_TTL if live else badge_cache.DEFAULT_TTL

    role = current_user.role.name if current_user.role else None
    uid = current_user.id

    # ── Unread in-app notifications ─────────────────────────────────────────
    # Keyed by the tenant-scope school id (auto-scopes the Notification query;
    # changes when a super admin switches school), the user (read-state + direct
    # targeting), and role (drives the visibility filter).
    try:
        from app.models import Notification, NotificationRead
        from app.utils.notification_visibility import notification_visible_to

        notif_sid = getattr(g, 'tenant_scope_school_id', None)

        def _load_notifications():
            read_ids = (db.session.query(NotificationRead.notification_id)
                        .filter_by(user_id=uid).subquery())
            return (Notification.query
                    .filter(notification_visible_to(current_user))
                    .filter(Notification.id.notin_(read_ids))
                    .count())

        counts['unread_notifications'] = badge_cache.get_or_set(
            (prefix + 'notif', uid, notif_sid, role),
            _load_notifications, ttl=ttl,
        )
    except Exception:
        db.session.rollback()

    # ── Complaints / leave requests / chat ──────────────────────────────────
    try:
        from app.models import (
            Complaint, LeaveRequest,
            ChatMessage, ChatMessageRead, ChatRoom, ChatRoomMember,
        )
        from app.utils.decorators import get_current_school
        from app.utils.modules import get_enabled_modules

        current_school = get_current_school()
        sid = (current_school.id
               if current_school and hasattr(current_school, 'id')
               else getattr(current_user, 'school_id', None))

        # Complaint + leave counts use bypass_tenant_scope + include_all_years
        # with explicit school filters, so they depend only on (school, [parent
        # user]) — the view year is intentionally NOT part of the key.
        if sid:
            if current_user.is_admin_user:
                from app.models import EmployeeLeaveRequest

                def _load_admin_complaints():
                    return (Complaint.query
                            .execution_options(bypass_tenant_scope=True, include_all_years=True)
                            .filter(Complaint.school_id == sid,
                                    Complaint.status.in_(['new', 'under_review']))
                            .count())

                def _load_admin_leaves():
                    return (LeaveRequest.query
                            .execution_options(bypass_tenant_scope=True, include_all_years=True)
                            .filter(LeaveRequest.school_id == sid,
                                    LeaveRequest.status == 'pending')
                            .count())

                def _load_admin_emp_leaves():
                    return (EmployeeLeaveRequest.query
                            .execution_options(bypass_tenant_scope=True)
                            .filter(EmployeeLeaveRequest.school_id == sid,
                                    EmployeeLeaveRequest.status == 'pending')
                            .count())

                counts['pending_complaints'] = badge_cache.get_or_set(
                    (prefix + 'complaints', sid, 'admin'), _load_admin_complaints, ttl=ttl)
                counts['pending_leave_requests'] = badge_cache.get_or_set(
                    (prefix + 'leave', sid, 'admin'), _load_admin_leaves, ttl=ttl)
                counts['pending_employee_leave_requests'] = badge_cache.get_or_set(
                    (prefix + 'emp_leave', sid, 'admin'), _load_admin_emp_leaves, ttl=ttl)
            elif role == 'parent':
                def _load_parent_complaints():
                    return (Complaint.query
                            .execution_options(bypass_tenant_scope=True, include_all_years=True)
                            .filter(Complaint.school_id == sid,
                                    Complaint.parent_id == uid,
                                    Complaint.status.in_(['new', 'under_review']))
                            .count())

                def _load_parent_leaves():
                    return (LeaveRequest.query
                            .execution_options(bypass_tenant_scope=True, include_all_years=True)
                            .filter(LeaveRequest.school_id == sid,
                                    LeaveRequest.parent_id == uid,
                                    LeaveRequest.status == 'pending')
                            .count())

                counts['pending_complaints'] = badge_cache.get_or_set(
                    (prefix + 'complaints', sid, 'parent', uid), _load_parent_complaints, ttl=ttl)
                counts['pending_leave_requests'] = badge_cache.get_or_set(
                    (prefix + 'leave', sid, 'parent', uid), _load_parent_leaves, ttl=ttl)

        enabled_modules = get_enabled_modules(
            None if current_user.is_super_admin else getattr(current_user, 'school_id', None))

        if 'chat' in enabled_modules:
            def _load_unread_chat():
                # Scope membership through the room's school so a stale or
                # mismatched cross-school ChatRoomMember can never inflate this
                # badge with another school's unread messages.
                member_rooms = (db.session.query(ChatRoomMember.room_id)
                                .join(ChatRoom, ChatRoom.id == ChatRoomMember.room_id)
                                .filter(ChatRoomMember.user_id == uid,
                                        ChatRoom.school_id == sid,
                                        ChatRoom.is_active == True))  # noqa: E712
                read_msgs = (db.session.query(ChatMessageRead.message_id)
                             .filter_by(user_id=uid))
                return (ChatMessage.query
                        .filter(
                            ChatMessage.room_id.in_(member_rooms),
                            ChatMessage.is_deleted == False,  # noqa: E712
                            ChatMessage.sender_user_id != uid,
                            ChatMessage.id.notin_(read_msgs),
                        )
                        .count()) or 0

            # Membership + read state are per user; the room-school join above
            # binds the count to the current school, so (user, school) fully
            # determines it. The user id remains the cache key because a given
            # user belongs to exactly one school.
            counts['unread_chat'] = badge_cache.get_or_set(
                (prefix + 'chat', uid), _load_unread_chat, ttl=ttl)
    except Exception:
        db.session.rollback()

    return counts

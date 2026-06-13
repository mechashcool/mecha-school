"""Mecha-School — Live blueprint.

Lightweight, session-authenticated JSON endpoints that let an open page stay in
sync with server state without a full reload. Currently exposes the sidebar /
topbar badge counts for the logged-in user.

Why a dedicated blueprint:
    * It is intentionally NOT listed in ``BLUEPRINT_MODULE`` so the per-school
      module guard never blocks it — the badge payload spans several modules
      (notifications, complaints, leave, chat) and a disabled *notifications*
      module must not stop the *chat* badge from updating.
    * It uses normal session login + tenant scoping (set by the global
      ``before_request`` hook), so every count is computed inside the caller's
      own school/role/year scope. No cross-user or cross-school data is exposed:
      the endpoint returns only small integer counts for ``current_user``.
"""
from flask import Blueprint, jsonify
from flask_login import login_required

live_bp = Blueprint('live', __name__)


@live_bp.route('/badges')
@login_required
def badges():
    """Return the current badge counts for the logged-in user as JSON.

    Shape: ``{"counts": {"unread_notifications": N, "pending_complaints": N,
    "pending_leave_requests": N, "unread_chat": N}}``. The same helper that
    feeds the server-rendered sidebar computes these, so the values and their
    isolation rules are identical — only the cache TTL differs (short, for
    freshness). ``Cache-Control: no-store`` keeps proxies/browsers from serving
    one user's counts to another.
    """
    from app.utils.sidebar_badges import get_badge_counts

    counts = get_badge_counts(live=True)
    resp = jsonify(counts=counts)
    resp.headers['Cache-Control'] = 'no-store'
    return resp

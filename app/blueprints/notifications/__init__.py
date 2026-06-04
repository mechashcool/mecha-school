"""Mecha-School – Notifications Blueprint"""
import logging

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from app.models import db, Notification, NotificationRead, User, Role
from app.utils.decorators import permission_required, get_current_school, historical_guard
from app.utils.notification_visibility import (
    notification_is_addressed_to, notification_visible_to,
)

_log = logging.getLogger('mecha.notifications_bp')

notifications_bp = Blueprint('notifications', __name__,
                              template_folder='../../templates/notifications')


def _dispatch_fcm_push(title: str, body: str, ntype: str,
                       target_user_id: int | None,
                       target_role: str | None,
                       school_id: int) -> None:
    """
    Fire FCM push for a just-committed Notification row.
    Runs after DB commit so the in-app row is already saved regardless of FCM outcome.
    Handles three target modes:
      - target_user_id set → push to one user
      - target_role set → push to all active users with that role in the school
      - both None → push to all active users in the school
    """
    try:
        from app.services.fcm_service import is_enabled, send_push_to_user
        if not is_enabled():
            _log.warning('[notifications] FCM disabled — skipping push for title=%r', title)
            return

        data = {'type': 'message', 'screen': 'notifications', 'school_id': str(school_id)}

        if target_user_id:
            _log.warning('[notifications] FCM push → user_id=%s title=%r', target_user_id, title)
            send_push_to_user(target_user_id, title, body, data)

        elif target_role:
            role_obj = Role.query.filter_by(name=target_role).first()
            if not role_obj:
                _log.warning('[notifications] FCM broadcast skipped — unknown role=%r', target_role)
                return
            users = (User.query
                     .filter_by(role_id=role_obj.id, school_id=school_id, is_active=True)
                     .all())
            _log.warning('[notifications] FCM broadcast → role=%s users=%d title=%r',
                         target_role, len(users), title)
            for u in users:
                _log.warning('[notifications] dispatching FCM user_id=%s title=%r', u.id, title)
                send_push_to_user(u.id, title, body, data)

        else:
            # Broadcast to all active users in the school
            users = (User.query
                     .filter_by(school_id=school_id, is_active=True)
                     .all())
            _log.warning('[notifications] FCM broadcast → all_school_users=%d title=%r',
                         len(users), title)
            for u in users:
                _log.warning('[notifications] dispatching FCM user_id=%s title=%r', u.id, title)
                send_push_to_user(u.id, title, body, data)

    except Exception:
        _log.exception('[notifications] FCM dispatch failed title=%r', title)


# ntype values that are always system-generated (never admin-issued).
_SYSTEM_NTYPES = {
    'attendance', 'fee_reminder', 'homework', 'rfid', 'aiface',
    'parent_request', 'chat_message', 'school_announcement',
}


@notifications_bp.route('/')
@login_required
def index():
    school = get_current_school()
    school_id = school.id if school else None

    page     = request.args.get('page', 1, type=int)
    tab      = request.args.get('tab', 'all')
    if tab not in ('all', 'admin', 'system'):
        tab = 'all'

    can_manage = current_user.has_permission('manage_notifications')
    q = Notification.query
    if not can_manage:
        q = q.filter(notification_visible_to(current_user))
    q = q.order_by(Notification.created_at.desc())

    if school_id:
        q = q.filter(Notification.school_id == school_id)

    if tab == 'admin':
        q = q.filter(Notification.ntype == 'announcement')
    elif tab == 'system':
        q = q.filter(Notification.ntype != 'announcement')

    notifs = q.paginate(page=page, per_page=20, error_out=False)

    # Mark only the user's own inbox notifications as read. Admin history can
    # include notifications sent to other users and must not consume them.
    for n in notifs.items:
        if not notification_is_addressed_to(n, current_user):
            continue
        exists = NotificationRead.query.filter_by(
            notification_id=n.id, user_id=current_user.id).first()
        if not exists:
            db.session.add(NotificationRead(
                notification_id=n.id, user_id=current_user.id))
    db.session.commit()
    return render_template('notifications/index.html',
                           notifs=notifs, can_manage=can_manage,
                           active_tab=tab)


@notifications_bp.route('/create', methods=['GET', 'POST'])
@login_required
@historical_guard
@permission_required('manage_notifications')
def create():
    school = get_current_school()
    school_id = school.id if school else None
    if not school_id:
        flash('Select a school before creating notifications.', 'danger')
        return redirect(url_for('notifications.index'))

    # Load users for specific-recipient selectors.
    parent_role = Role.query.filter_by(name='parent').first()
    teacher_role = Role.query.filter_by(name='teacher').first()
    parent_users = []
    teacher_users = []
    if parent_role:
        parent_users = (User.query
                        .filter_by(role_id=parent_role.id, school_id=school_id)
                        .order_by(User.full_name)
                        .all())
    if teacher_role:
        teacher_users = (User.query
                         .filter_by(role_id=teacher_role.id, school_id=school_id)
                         .order_by(User.full_name)
                         .all())

    def form_response(status=200):
        return render_template(
            'notifications/form.html',
            parent_users=parent_users,
            teacher_users=teacher_users,
        ), status

    if request.method == 'POST':
        target_mode = request.form.get('target_role', '') or None
        target_role = None
        target_user_id = None

        if target_mode == '_specific_parent':
            target_user_id = request.form.get('target_parent_id', type=int)
            if not target_user_id:
                # Backward-compatible fallback for older forms/tests.
                target_user_id = request.form.get('target_user_id', type=int)
            if not target_user_id:
                flash('يرجى اختيار ولي الأمر المحدد.', 'danger')
                return form_response(400)
            target_user = (User.query
                           .filter_by(id=target_user_id, school_id=school_id)
                           .filter(User.role.has(name='parent'))
                           .first())
            if not target_user:
                flash('ولي الأمر المحدد غير متاح لهذه المدرسة.', 'danger')
                return form_response(403)

        elif target_mode == '_specific_teacher':
            target_user_id = request.form.get('target_teacher_id', type=int)
            if not target_user_id:
                target_user_id = request.form.get('target_user_id', type=int)
            if not target_user_id:
                flash('يرجى اختيار المعلم المحدد.', 'danger')
                return form_response(400)
            target_user = (User.query
                           .filter_by(id=target_user_id, school_id=school_id)
                           .filter(User.role.has(name='teacher'))
                           .first())
            if not target_user:
                flash('المعلم المحدد غير متاح لهذه المدرسة.', 'danger')
                return form_response(403)

        elif target_mode in (None, 'teacher', 'parent'):
            target_role = target_mode

        else:
            flash('الجمهور المستهدف غير صالح.', 'danger')
            return form_response(400)

        if target_role:
            role_exists = Role.query.filter_by(name=target_role).first()
            if not role_exists:
                flash('الجمهور المستهدف غير صالح.', 'danger')
                return form_response(400)

        if not request.form.get('title', '').strip():
            flash('عنوان الإشعار مطلوب.', 'danger')
            return form_response(400)
        if not request.form.get('body', '').strip():
            flash('نص الإشعار مطلوب.', 'danger')
            return form_response(400)

        title = request.form.get('title', '').strip()
        body  = request.form.get('body', '').strip()
        ntype = request.form.get('ntype', 'announcement')

        _log.warning('[notifications] target_mode=%r target_user_id=%s target_role=%s',
                     target_mode, target_user_id, target_role)

        n = Notification(
            school_id      = school_id,
            title          = title,
            body           = body,
            ntype          = ntype,
            target_role    = target_role,
            target_user_id = target_user_id,
            created_by     = current_user.id,
        )
        db.session.add(n)
        db.session.commit()
        _log.warning('[notifications] created notification_id=%s target_user_id=%s '
                     'target_role=%s title=%r', n.id, target_user_id, target_role, title)

        # FCM push — after DB commit so in-app row is saved regardless of push outcome
        _log.warning('[notifications] dispatching FCM target_user_id=%s target_role=%s title=%r',
                     target_user_id, target_role, title)
        _dispatch_fcm_push(
            title=title,
            body=body,
            ntype=ntype,
            target_user_id=target_user_id,
            target_role=target_role,
            school_id=school_id,
        )

        flash('تم إرسال الإشعار بنجاح.', 'success')
        return redirect(url_for('notifications.index'))

    return render_template('notifications/form.html',
                           parent_users=parent_users,
                           teacher_users=teacher_users)


@notifications_bp.route('/delete/<int:nid>', methods=['POST'])
@login_required
@historical_guard
@permission_required('manage_notifications')
def delete(nid):
    n = Notification.query.get_or_404(nid)
    db.session.delete(n)
    db.session.commit()
    flash('تم حذف الإشعار.', 'success')
    return redirect(url_for('notifications.index'))

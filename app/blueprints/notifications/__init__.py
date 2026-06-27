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


_MOBILE_ROLES = frozenset({'parent', 'teacher'})


def _dispatch_fcm_push(title: str, body: str, ntype: str,
                       target_user_id: int | None,
                       target_role: str | None,
                       school_id: int,
                       notification_id: int | None = None,
                       sender_user_id: int | None = None) -> None:
    """
    Fire FCM push for a just-committed Notification row.
    Runs after DB commit so the in-app row is already saved regardless of FCM outcome.
    Handles three target modes:
      - target_user_id set → push to one user
      - target_role set → push to all active users with that role in the school
      - both None → push to all active mobile users (parent + teacher) in the school

    Only 'parent' and 'teacher' roles use the mobile app and register device tokens.
    Admin/manager users are excluded from broadcasts to avoid misleading
    "no active device tokens" log spam and ensure push delivery to actual recipients.
    """
    try:
        from app.models import MobileDeviceToken
        from app.services.fcm_service import is_enabled, send_push_to_user
        if not is_enabled():
            _log.warning('[notifications] FCM disabled — skipping push notification_id=%s title=%r',
                         notification_id, title)
            return

        data = {
            'type':     'message',
            'screen':   'notifications',
            'ntype':    ntype,
            'school_id': str(school_id),
        }

        _log.warning(
            '[notifications] FCM dispatch start '
            'notification_id=%s sender_user_id=%s school_id=%s '
            'target_user_id=%s target_role=%s title=%r',
            notification_id, sender_user_id, school_id,
            target_user_id, target_role, title,
        )

        if target_user_id:
            token_count = MobileDeviceToken.query.filter_by(
                user_id=target_user_id, is_active=True).count()
            _log.warning(
                '[notifications] FCM push → specific user  '
                'notification_id=%s user_id=%s active_tokens=%d title=%r',
                notification_id, target_user_id, token_count, title,
            )
            ok_n, fail_n = send_push_to_user(target_user_id, title, body, data)
            _log.warning(
                '[notifications] FCM result  notification_id=%s user_id=%s sent=%d failed=%d',
                notification_id, target_user_id, ok_n, fail_n,
            )

        elif target_role:
            role_obj = Role.query.filter_by(name=target_role).first()
            if not role_obj:
                _log.warning(
                    '[notifications] FCM broadcast skipped — unknown role=%r '
                    'notification_id=%s', target_role, notification_id,
                )
                return
            users = (User.query
                     .filter_by(role_id=role_obj.id, school_id=school_id, is_active=True)
                     .all())
            user_ids = [u.id for u in users]
            _log.warning(
                '[notifications] FCM broadcast → role=%s  '
                'notification_id=%s school_id=%s resolved_user_ids=%s count=%d title=%r',
                target_role, notification_id, school_id, user_ids, len(users), title,
            )
            total_ok = total_fail = 0
            for u in users:
                token_count = MobileDeviceToken.query.filter_by(
                    user_id=u.id, is_active=True).count()
                _log.warning(
                    '[notifications] dispatching FCM  notification_id=%s user_id=%s '
                    'active_tokens=%d', notification_id, u.id, token_count,
                )
                ok_n, fail_n = send_push_to_user(u.id, title, body, data)
                total_ok += ok_n
                total_fail += fail_n
            _log.warning(
                '[notifications] FCM broadcast result  notification_id=%s role=%s '
                'recipients=%d total_sent=%d total_failed=%d',
                notification_id, target_role, len(users), total_ok, total_fail,
            )

        else:
            # Broadcast to all active mobile users (parents + teachers) in the school.
            # Admin/manager accounts cannot log into the mobile app and therefore never
            # register device tokens — including them only produces misleading "no active
            # device tokens" log noise and never delivers a push.
            mobile_role_ids = [
                r.id for r in
                Role.query.filter(Role.name.in_(_MOBILE_ROLES)).all()
            ]
            users = (User.query
                     .filter(User.school_id == school_id,
                             User.is_active.is_(True),
                             User.role_id.in_(mobile_role_ids))
                     .all())
            user_ids = [u.id for u in users]
            _log.warning(
                '[notifications] FCM broadcast → all mobile users  '
                'notification_id=%s school_id=%s resolved_user_ids=%s count=%d title=%r',
                notification_id, school_id, user_ids, len(users), title,
            )
            total_ok = total_fail = 0
            for u in users:
                token_count = MobileDeviceToken.query.filter_by(
                    user_id=u.id, is_active=True).count()
                _log.warning(
                    '[notifications] dispatching FCM  notification_id=%s user_id=%s '
                    'active_tokens=%d', notification_id, u.id, token_count,
                )
                ok_n, fail_n = send_push_to_user(u.id, title, body, data)
                total_ok += ok_n
                total_fail += fail_n
            _log.warning(
                '[notifications] FCM broadcast result  notification_id=%s '
                'recipients=%d total_sent=%d total_failed=%d',
                notification_id, len(users), total_ok, total_fail,
            )

    except Exception:
        _log.exception('[notifications] FCM dispatch failed notification_id=%s title=%r',
                       notification_id, title)


# ntype values that are always system-generated (never admin-issued).
_SYSTEM_NTYPES = {
    'attendance', 'fee_reminder', 'homework', 'rfid', 'aiface',
    'parent_request', 'chat_message', 'school_announcement',
}

_ATTENDANCE_NTYPES = frozenset({'attendance', 'rfid', 'aiface'})
_FEE_NTYPES        = frozenset({'fee_reminder', 'fee'})
_ADMIN_NTYPES      = frozenset({'announcement'})


@notifications_bp.route('/')
@login_required
def index():
    school = get_current_school()
    school_id = school.id if school else None

    page     = request.args.get('page', 1, type=int)
    tab      = request.args.get('tab', 'all')
    if tab not in ('all', 'admin', 'attendance', 'fees', 'system'):
        tab = 'all'

    can_manage = current_user.has_permission('manage_notifications')
    q = Notification.query
    if not can_manage:
        q = q.filter(notification_visible_to(current_user))
    q = q.order_by(Notification.created_at.desc())

    if school_id:
        q = q.filter(Notification.school_id == school_id)

    if tab == 'admin':
        q = q.filter(Notification.ntype.in_(_ADMIN_NTYPES))
    elif tab == 'attendance':
        q = q.filter(Notification.ntype.in_(_ATTENDANCE_NTYPES))
    elif tab == 'fees':
        q = q.filter(Notification.ntype.in_(_FEE_NTYPES))
    elif tab == 'system':
        q = q.filter(Notification.ntype.notin_(_ATTENDANCE_NTYPES | _FEE_NTYPES | _ADMIN_NTYPES))

    notifs = q.paginate(page=page, per_page=20, error_out=False)

    # Mark ALL unread notifications visible to this user as read — not just the
    # current page. This keeps the sidebar badge in sync: the badge count uses
    # notification_visible_to across all pages; the mark-as-read must match that
    # exact scope so the badge clears on the first page visit.
    # Admin users (can_manage) see all school notifications but their badge only
    # counts those addressed to them; using notification_visible_to here ensures
    # we only touch the notifications that belong to the current user's inbox.
    _read_ids_sub = (
        db.session.query(NotificationRead.notification_id)
        .filter_by(user_id=current_user.id)
        .subquery()
    )
    _unread_q = (
        Notification.query
        .filter(notification_visible_to(current_user))
        .filter(Notification.id.notin_(_read_ids_sub))
    )
    if school_id:
        _unread_q = _unread_q.filter(Notification.school_id == school_id)
    _unread_ids = _unread_q.with_entities(Notification.id).all()
    if _unread_ids:
        db.session.add_all([
            NotificationRead(notification_id=nid, user_id=current_user.id)
            for (nid,) in _unread_ids
        ])
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
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

        _log.warning(
            '[notifications] create  sender_user_id=%s school_id=%s '
            'target_mode=%r target_user_id=%s target_role=%s title=%r',
            current_user.id, school_id, target_mode, target_user_id, target_role, title,
        )

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
        _dispatch_fcm_push(
            title=title,
            body=body,
            ntype=ntype,
            target_user_id=target_user_id,
            target_role=target_role,
            school_id=school_id,
            notification_id=n.id,
            sender_user_id=current_user.id,
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

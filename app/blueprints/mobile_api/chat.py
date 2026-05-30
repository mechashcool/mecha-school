"""
Mobile API — Chat / Messaging endpoints
========================================
All routes require:
  Authorization: Bearer <access_token>
  Chat module enabled for school
  chat.api_access feature enabled

Endpoint map
────────────
GET  /chat/rooms                          list rooms I'm member of
GET  /chat/rooms/<id>                     room detail + my permissions
GET  /chat/rooms/<id>/messages            paginated message history
POST /chat/rooms/<id>/messages            send a text message
POST /chat/rooms/<id>/read                mark all unread messages as read
GET  /chat/contacts                       available contacts (for starting private chats)

Security rules
──────────────
• Every endpoint verifies the authenticated user is a member of the requested room.
• Parents can only see rooms they're a member of (auto-added by admin on room creation).
• Teachers can only see rooms they're a member of.
• Blocked members cannot send messages.
• Announcement-only rooms: only owner/admin role members can send.
• Closed rooms: nobody can send.
• Schedule check: if schedules exist, only send within window.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import g, request
from sqlalchemy import select

from app.models import (
    db, ChatRoom, ChatRoomMember, ChatMessage, ChatMessageRead,
    ChatRoomSchedule, Employee, parent_students, teacher_subjects,
    User, Section, Student,
)
from app.utils.modules import is_module_enabled
from app.utils.features import is_feature_enabled

from . import mobile_api_bp
from .utils import jwt_required, role_required, ok, err, photo_url

_log = logging.getLogger('mecha.chat_api')

# ─── Module guard ─────────────────────────────────────────────────────────────

def _check_chat_access():
    """Return error response tuple if chat module or api_access is disabled."""
    user = g.mobile_user
    school_id = user.school_id
    if not is_module_enabled(school_id, 'chat'):
        return err('هذه الميزة غير مفعلة لهذه المدرسة.', 403)
    if not is_feature_enabled(school_id, 'chat.api_access'):
        return err('الوصول إلى المراسلات من التطبيق غير مفعل.', 403)
    return None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _get_membership(room_id: int, user_id: int) -> ChatRoomMember | None:
    return ChatRoomMember.query.filter_by(room_id=room_id, user_id=user_id).first()


def _can_send_now(room: ChatRoom, school) -> tuple[bool, str]:
    """Check schedule; return (allowed, reason)."""
    schedules = (ChatRoomSchedule.query
                 .filter_by(room_id=room.id, is_enabled=True)
                 .all())
    if not schedules:
        return True, ''
    try:
        from app.utils.attendance_helpers import get_local_now
        local_now = get_local_now(school)
    except Exception:
        local_now = datetime.utcnow()
    dow = (local_now.weekday() + 1) % 7  # Sun=0 scheme
    now_t = local_now.time()
    for sch in schedules:
        if sch.day_of_week == dow:
            if sch.open_time <= now_t <= sch.close_time:
                return True, ''
            return (False,
                    'المراسلات غير متاحة حالياً، يمكنكم الإرسال ضمن أوقات '
                    'التواصل المحددة من المدرسة.')
    return (False,
            'المراسلات غير متاحة حالياً، يمكنكم الإرسال ضمن أوقات التواصل '
            'المحددة من المدرسة.')


def _serialize_message(msg: ChatMessage, current_user_id: int) -> dict:
    return {
        'id':              msg.id,
        'sender_id':       msg.sender_user_id,
        'sender_name':     msg.sender.full_name if msg.sender else None,
        'sender_role':     (msg.sender.role.name
                            if msg.sender and msg.sender.role else None),
        'body':            None if msg.is_deleted else msg.body,
        'message_type':    msg.message_type,
        'attachment_url':  None if msg.is_deleted else msg.attachment_url,
        'created_at':      (msg.created_at.replace(tzinfo=timezone.utc).isoformat()
                            if msg.created_at else None),
        'is_mine':         msg.sender_user_id == current_user_id,
        'is_deleted':      msg.is_deleted,
    }


def _unread_count(room_id: int, user_id: int) -> int:
    read_msg_ids = db.session.scalars(
        select(ChatMessageRead.message_id).where(
            ChatMessageRead.user_id == user_id
        )
    ).all()
    q = (ChatMessage.query
         .filter_by(room_id=room_id, is_deleted=False)
         .filter(ChatMessage.sender_user_id != user_id))
    if read_msg_ids:
        q = q.filter(ChatMessage.id.notin_(read_msg_ids))
    return q.count()


def _last_message_payload(room_id: int) -> dict | None:
    msg = (ChatMessage.query
           .filter_by(room_id=room_id, is_deleted=False)
           .order_by(ChatMessage.created_at.desc())
           .first())
    if not msg:
        return None
    return {
        'body':       msg.body,
        'sender_name': msg.sender.full_name if msg.sender else None,
        'created_at': (msg.created_at.replace(tzinfo=timezone.utc).isoformat()
                       if msg.created_at else None),
    }


def _member_can_send(membership: ChatRoomMember, room: ChatRoom) -> tuple[bool, str]:
    """
    Check membership-level send permissions.
    Returns (can_send, reason).
    """
    if membership.is_blocked:
        return (False,
                'تم تقييدك من إرسال الرسائل في هذه المحادثة. '
                'يرجى مراجعة إدارة المدرسة.')
    if room.is_closed:
        return False, 'هذه المحادثة مغلقة. لا يمكن إرسال رسائل.'
    if not room.allow_replies:
        return False, 'لا تملك صلاحية إرسال رسالة في هذه المحادثة.'
    if room.is_announcement_only and membership.role not in ('owner', 'admin'):
        return False, 'هذه المحادثة للإعلانات فقط. الإرسال مخصص للإدارة والمشرفين.'
    return True, ''


# ─── FCM push for new message ─────────────────────────────────────────────────

def _push_new_message(room: ChatRoom, msg: ChatMessage, sender_name: str) -> None:
    """Send FCM push to all non-blocked, non-sender members of the room."""
    try:
        from app.services.fcm_service import is_enabled, send_push_to_user
        if not is_enabled():
            return
        members = (ChatRoomMember.query
                   .filter_by(room_id=room.id, is_blocked=False)
                   .all())
        ntype  = 'school_announcement' if room.is_announcement_only else 'chat_message'
        title  = (f'رسالة جديدة في {room.name}'
                  if room.type in ('group', 'announcement')
                  else f'رسالة جديدة من {sender_name}')
        body   = (msg.body or '[مرفق]')[:150]
        data   = {
            'type':       ntype,
            'room_id':    str(room.id),
            'message_id': str(msg.id),
            'screen':     'chat',
        }
        for mem in members:
            if mem.user_id == msg.sender_user_id:
                continue
            try:
                send_push_to_user(mem.user_id, title, body, data)
            except Exception as exc:
                _log.warning('[chat_api] FCM push failed user_id=%s: %s', mem.user_id, exc)
    except Exception as exc:
        _log.error('[chat_api] _push_new_message error: %s', exc)


# ─── List rooms ───────────────────────────────────────────────────────────────

@mobile_api_bp.route('/chat/rooms', methods=['GET'])
@jwt_required()
def chat_rooms():
    guard = _check_chat_access()
    if guard:
        return guard

    user = g.mobile_user
    limit  = min(int(request.args.get('limit',  50)), 100)
    offset = max(int(request.args.get('offset',  0)),  0)
    type_f = request.args.get('type', '').strip()

    memberships = (ChatRoomMember.query
                   .filter_by(user_id=user.id)
                   .all())
    room_ids = [m.room_id for m in memberships]

    if not room_ids:
        return ok(rooms=[], total=0, limit=limit, offset=offset)

    q = (ChatRoom.query
         .execution_options(bypass_tenant_scope=True)
         .filter(ChatRoom.id.in_(room_ids),
                 ChatRoom.school_id == user.school_id,
                 ChatRoom.is_active == True)
         .order_by(ChatRoom.updated_at.desc()))

    if type_f:
        q = q.filter(ChatRoom.type == type_f)

    total = q.count()
    rooms = q.offset(offset).limit(limit).all()
    mem_map = {m.room_id: m for m in memberships}

    rooms_out = []
    for room in rooms:
        mem = mem_map.get(room.id)
        can_send_flag = False
        if mem and not mem.is_blocked and not room.is_closed and room.allow_replies:
            if not room.is_announcement_only or mem.role in ('owner', 'admin'):
                can_send_flag = True

        rooms_out.append({
            'id':                  room.id,
            'name':                room.name,
            'type':                room.type,
            'scope':               room.scope,
            'is_closed':           room.is_closed,
            'is_announcement_only': room.is_announcement_only,
            'can_send':            can_send_flag,
            'my_role':             mem.role if mem else 'member',
            'is_blocked':          mem.is_blocked if mem else False,
            'unread_count':        _unread_count(room.id, user.id),
            'last_message':        _last_message_payload(room.id),
        })

    return ok(rooms=rooms_out, total=total, limit=limit, offset=offset)


# ─── Room detail ─────────────────────────────────────────────────────────────

@mobile_api_bp.route('/chat/rooms/<int:room_id>', methods=['GET'])
@jwt_required()
def chat_room_detail(room_id):
    guard = _check_chat_access()
    if guard:
        return guard

    user = g.mobile_user
    mem = _get_membership(room_id, user.id)
    if not mem:
        return err('لست عضواً في هذه المحادثة.', 403)

    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=user.school_id, is_active=True)
            .first())
    if not room:
        return err('المحادثة غير موجودة.', 404)

    can_send, send_reason = _member_can_send(mem, room)
    # Schedule check
    if can_send and room.schedules.filter_by(is_enabled=True).count() > 0:
        can_send, send_reason = _can_send_now(room, user.school)

    members_out = []
    for m in room.members.all():
        if m.user:
            members_out.append({
                'user_id':  m.user_id,
                'name':     m.user.full_name,
                'role':     m.role,
                'is_blocked': m.is_blocked,
            })

    schedules_out = [
        {
            'day':        s.day_of_week,
            'open_time':  s.open_time.strftime('%H:%M'),
            'close_time': s.close_time.strftime('%H:%M'),
            'is_enabled': s.is_enabled,
        }
        for s in room.schedules.filter_by(is_enabled=True).all()
    ]

    return ok(
        room={
            'id':                   room.id,
            'name':                 room.name,
            'type':                 room.type,
            'scope':                room.scope,
            'is_closed':            room.is_closed,
            'is_announcement_only': room.is_announcement_only,
            'allow_replies':        room.allow_replies,
            'created_at':           (room.created_at.replace(tzinfo=timezone.utc).isoformat()
                                     if room.created_at else None),
        },
        my_membership={
            'role':       mem.role,
            'is_blocked': mem.is_blocked,
            'can_send':   can_send,
            'send_reason': send_reason,
        },
        member_count=room.members.count(),
        members=members_out,
        schedules=schedules_out,
        unread_count=_unread_count(room.id, user.id),
    )


# ─── Message history ──────────────────────────────────────────────────────────

@mobile_api_bp.route('/chat/rooms/<int:room_id>/messages', methods=['GET'])
@jwt_required()
def chat_room_messages(room_id):
    guard = _check_chat_access()
    if guard:
        return guard

    user = g.mobile_user
    mem = _get_membership(room_id, user.id)
    if not mem:
        return err('لست عضواً في هذه المحادثة.', 403)

    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=user.school_id, is_active=True)
            .first())
    if not room:
        return err('المحادثة غير موجودة.', 404)

    limit    = min(int(request.args.get('limit', 50)), 100)
    before_id = request.args.get('before')

    q = ChatMessage.query.filter_by(room_id=room.id)
    if before_id:
        try:
            q = q.filter(ChatMessage.id < int(before_id))
        except (ValueError, TypeError):
            pass

    messages = (q.order_by(ChatMessage.created_at.desc())
                 .limit(limit).all())
    messages.reverse()

    return ok(
        room_id=room_id,
        count=len(messages),
        messages=[_serialize_message(m, user.id) for m in messages],
    )


# ─── Send message ────────────────────────────────────────────────────────────

@mobile_api_bp.route('/chat/rooms/<int:room_id>/messages', methods=['POST'])
@jwt_required()
def chat_send_message(room_id):
    guard = _check_chat_access()
    if guard:
        return guard

    if not is_feature_enabled(g.mobile_user.school_id, 'chat.send_message'):
        return err('إرسال الرسائل غير مفعل.', 403)

    user = g.mobile_user
    mem = _get_membership(room_id, user.id)
    if not mem:
        return err('لست عضواً في هذه المحادثة.', 403)

    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=user.school_id, is_active=True)
            .first())
    if not room:
        return err('المحادثة غير موجودة.', 404)

    can_send, reason = _member_can_send(mem, room)
    if not can_send:
        return err(reason, 403)

    # Schedule check
    if room.schedules.filter_by(is_enabled=True).count() > 0:
        can_send, reason = _can_send_now(room, user.school)
        if not can_send:
            return err(reason, 403)

    payload = request.get_json(silent=True) or {}
    body    = (payload.get('body') or '').strip()
    if not body:
        return err('نص الرسالة مطلوب.', 400)

    # Message length cap
    from app.utils.school_config import get_school_config
    cfg     = get_school_config(user.school_id)
    raw_cfg = cfg.as_dict('chat')
    max_len = int((raw_cfg.get('extra') or {}).get('message_max_length') or 2000)
    if len(body) > max_len:
        return err(f'الرسالة طويلة جداً. الحد الأقصى {max_len} حرف.', 400)

    msg = ChatMessage(
        room_id=room.id,
        sender_user_id=user.id,
        body=body,
        message_type='text',
    )
    db.session.add(msg)
    room.updated_at = datetime.utcnow()
    db.session.commit()

    _log.warning('[chat_api] message sent room_id=%s user_id=%s msg_id=%s',
                 room.id, user.id, msg.id)

    _push_new_message(room, msg, user.full_name)

    return ok(
        message='تم إرسال الرسالة بنجاح.',
        data=_serialize_message(msg, user.id),
    ), 201


# ─── Mark room read ───────────────────────────────────────────────────────────

@mobile_api_bp.route('/chat/rooms/<int:room_id>/read', methods=['POST'])
@jwt_required()
def chat_mark_read(room_id):
    guard = _check_chat_access()
    if guard:
        return guard

    user = g.mobile_user
    mem = _get_membership(room_id, user.id)
    if not mem:
        return err('لست عضواً في هذه المحادثة.', 403)

    room = (ChatRoom.query
            .execution_options(bypass_tenant_scope=True)
            .filter_by(id=room_id, school_id=user.school_id)
            .first())
    if not room:
        return err('المحادثة غير موجودة.', 404)

    already_read = {
        r.message_id
        for r in ChatMessageRead.query.filter_by(user_id=user.id).all()
    }
    messages = (ChatMessage.query
                .filter_by(room_id=room.id, is_deleted=False)
                .filter(ChatMessage.sender_user_id != user.id)
                .all())
    marked = 0
    for msg in messages:
        if msg.id not in already_read:
            db.session.add(ChatMessageRead(message_id=msg.id, user_id=user.id))
            marked += 1
    if marked:
        db.session.commit()

    return ok(marked=marked)


# ─── Available contacts ───────────────────────────────────────────────────────

@mobile_api_bp.route('/chat/contacts', methods=['GET'])
@jwt_required()
def chat_contacts():
    guard = _check_chat_access()
    if guard:
        return guard

    user      = g.mobile_user
    role_name = user.role.name if user.role else None
    school_id = user.school_id

    contacts = []

    if role_name == 'parent':
        # Teachers related to parent's children
        child_ids = [c.id for c in user.children]
        if child_ids:
            # Sections of the children
            section_ids = {
                s.section_id
                for s in Student.query
                .execution_options(bypass_tenant_scope=True)
                .filter(Student.id.in_(child_ids), Student.section_id.isnot(None))
                .all()
            }
            if section_ids:
                # Homeroom teachers
                sections = (Section.query
                            .execution_options(bypass_tenant_scope=True)
                            .filter(Section.id.in_(section_ids))
                            .all())
                emp_ids = {s.teacher_id for s in sections if s.teacher_id}
                # Subject teachers
                subj_rows = (db.session.query(teacher_subjects.c.employee_id)
                             .filter(teacher_subjects.c.section_id.in_(section_ids))
                             .all())
                for (eid,) in subj_rows:
                    emp_ids.add(eid)
                for eid in emp_ids:
                    emp = db.session.get(Employee, eid)
                    if emp and emp.user_id and emp.school_id == school_id:
                        u = db.session.get(User, emp.user_id)
                        if u and u.is_active:
                            contacts.append({
                                'user_id':  u.id,
                                'name':     u.full_name,
                                'role':     'teacher',
                                'job_title': emp.job_title,
                                'photo':    photo_url(emp.photo),
                            })

        # School admins
        admins = (User.query
                  .execution_options(bypass_tenant_scope=True)
                  .filter_by(school_id=school_id, is_active=True)
                  .join(User.role)
                  .filter(db.text("roles.name IN ('admin', 'school_manager')"))
                  .all())
        for a in admins:
            if a.id != user.id:
                contacts.append({
                    'user_id': a.id,
                    'name':    a.full_name,
                    'role':    'admin',
                    'job_title': None,
                    'photo':   photo_url(a.avatar),
                })

    elif role_name == 'teacher':
        emp = Employee.query.filter_by(user_id=user.id, school_id=school_id).first()
        if emp:
            # Parents of students in my sections
            from app.blueprints.mobile_api.teacher import _teacher_section_ids
            section_ids = _teacher_section_ids(emp)
            if section_ids:
                student_ids = [
                    s.id for s in
                    Student.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter(Student.section_id.in_(section_ids), Student.status == 'active')
                    .all()
                ]
                if student_ids:
                    parent_rows = (db.session.query(parent_students.c.user_id)
                                   .filter(parent_students.c.student_id.in_(student_ids))
                                   .all())
                    parent_ids = {row[0] for row in parent_rows}
                    for pid in parent_ids:
                        p = db.session.get(User, pid)
                        if p and p.is_active and p.school_id == school_id:
                            contacts.append({
                                'user_id':  p.id,
                                'name':     p.full_name,
                                'role':     'parent',
                                'job_title': None,
                                'photo':    photo_url(p.avatar),
                            })

        # School admins
        admins = (User.query
                  .execution_options(bypass_tenant_scope=True)
                  .filter_by(school_id=school_id, is_active=True)
                  .join(User.role)
                  .filter(db.text("roles.name IN ('admin', 'school_manager')"))
                  .all())
        for a in admins:
            if a.id != user.id:
                contacts.append({
                    'user_id':  a.id,
                    'name':     a.full_name,
                    'role':     'admin',
                    'job_title': None,
                    'photo':    photo_url(a.avatar),
                })

    # Deduplicate by user_id
    seen = set()
    deduped = []
    for c in contacts:
        if c['user_id'] not in seen:
            seen.add(c['user_id'])
            deduped.append(c)

    return ok(contacts=deduped, count=len(deduped))

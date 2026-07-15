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
POST /chat/rooms/<id>/mute                mute push notifications for this room
POST /chat/rooms/<id>/unmute              unmute push notifications for this room
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
from sqlalchemy import and_, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.models import (
    db, ChatRoom, ChatRoomMember, ChatMessage, ChatMessageRead,
    ChatRoomSchedule, Employee, parent_students, teacher_subjects,
    User, Section, Student,
)
from app.utils.modules import is_module_enabled
from app.utils.features import is_feature_enabled

from . import mobile_api_bp
from .utils import jwt_required, role_required, ok, err, photo_url

# Mobile chat is restricted to parent and teacher roles, matching the login
# restriction.  The individual endpoints also call _check_chat_access() for
# module-level feature gating, but role enforcement must happen first.
_CHAT_ROLES = ('parent', 'teacher')

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


_UNSET = object()   # sentinel: "no prefetched schedule supplied" (None is meaningful)


def _local_now_for(school, room_id=None):
    """School-local now, falling back to Asia/Baghdad — shared by the per-room
    check and the batched room-list path so both use identical day/time logic."""
    try:
        from app.utils.attendance_helpers import get_local_now
        return get_local_now(school)
    except Exception:
        # Fall back to Iraq local time rather than raw UTC.
        # UTC gives the wrong weekday between 21:00–24:00 UTC (midnight–03:00 Iraq).
        import pytz as _pytz
        _log.warning(
            '[chat_api] _local_now_for: get_local_now failed for room_id=%s — '
            'falling back to Asia/Baghdad', room_id,
        )
        return datetime.now(_pytz.timezone('Asia/Baghdad')).replace(tzinfo=None)


def _can_send_now(room: ChatRoom, school, today_sch=_UNSET) -> tuple[bool, str]:
    """
    Per-day independent schedule check.

    Each day is evaluated in isolation:
    - No row for today, or today's row is disabled  → allowed (no restriction for today).
    - Today's row is enabled and current time is within open/close → allowed.
    - Today's row is enabled but current time is outside the range  → blocked.

    Other days' enabled/disabled state never affects today's result.

    ``today_sch``: optional prefetched ChatRoomSchedule row for (room, today) —
    the room-list endpoint loads all rooms' rows in one query (P1) and passes
    each room's row (or None) here so the decision logic stays in one place.
    """
    local_now = _local_now_for(school, room_id=room.id)
    dow = (local_now.weekday() + 1) % 7  # Sun=0 scheme
    now_t = local_now.time()

    if today_sch is _UNSET:
        # Look up ONLY today's row — other days are irrelevant
        today_sch = (ChatRoomSchedule.query
                     .filter_by(room_id=room.id, day_of_week=dow)
                     .first())

    if today_sch is None or not today_sch.is_enabled:
        # No schedule configured for today, or today is disabled → no restriction
        return True, ''

    if today_sch.open_time <= now_t <= today_sch.close_time:
        return True, ''

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
    """Unread messages in one room for one user — single aggregate anti-join.

    P1: the previous version loaded EVERY ChatMessageRead id the user had ever
    written (unbounded growth over account lifetime) and ran a giant NOT IN.
    Semantics are identical: messages in this room, not deleted, not sent by
    this user, with no read receipt BY THIS USER. Isolation: both room_id and
    user_id are bound in the query — another user's receipts can never satisfy
    the join, and other rooms' messages can never be counted.
    """
    return (
        db.session.query(func.count(ChatMessage.id))
        .outerjoin(
            ChatMessageRead,
            (ChatMessageRead.message_id == ChatMessage.id) &
            (ChatMessageRead.user_id == user_id),
        )
        .filter(
            ChatMessage.room_id == room_id,
            ChatMessage.is_deleted == False,
            ChatMessage.sender_user_id != user_id,
            ChatMessageRead.id.is_(None),
        )
        .scalar() or 0
    )


def _unread_counts_for_rooms(room_ids: list[int], user_id: int) -> dict[int, int]:
    """Unread counts for a page of rooms in ONE grouped query (P1).

    Same anti-join semantics as _unread_count, grouped by room. Keys carry the
    caller's room ids only (already membership-verified) and the user_id bound
    into the join — per-user, per-room isolation is structural.
    """
    if not room_ids:
        return {}
    rows = (
        db.session.query(ChatMessage.room_id, func.count(ChatMessage.id))
        .outerjoin(
            ChatMessageRead,
            (ChatMessageRead.message_id == ChatMessage.id) &
            (ChatMessageRead.user_id == user_id),
        )
        .filter(
            ChatMessage.room_id.in_(room_ids),
            ChatMessage.is_deleted == False,
            ChatMessage.sender_user_id != user_id,
            ChatMessageRead.id.is_(None),
        )
        .group_by(ChatMessage.room_id)
        .all()
    )
    return {room_id: count for room_id, count in rows}


def _last_message_payload(room_id: int) -> dict | None:
    msg = (ChatMessage.query
           .options(joinedload(ChatMessage.sender))
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


def _last_messages_for_rooms(room_ids: list[int]) -> dict[int, dict]:
    """Last-message payloads for a page of rooms in TWO queries (P1).

    Picks max(id) per room among non-deleted messages (ids are insert-ordered,
    matching the created_at ordering used by _last_message_payload — chat
    messages are never backdated), then loads those rows with their senders in
    one joined query. Only the caller's membership-verified room ids are ever
    queried.
    """
    if not room_ids:
        return {}
    last_ids = [
        r[0] for r in (
            db.session.query(func.max(ChatMessage.id))
            .filter(ChatMessage.room_id.in_(room_ids),
                    ChatMessage.is_deleted == False)
            .group_by(ChatMessage.room_id)
            .all()
        )
    ]
    if not last_ids:
        return {}
    msgs = (ChatMessage.query
            .options(joinedload(ChatMessage.sender))
            .filter(ChatMessage.id.in_(last_ids))
            .all())
    return {
        m.room_id: {
            'body':        m.body,
            'sender_name': m.sender.full_name if m.sender else None,
            'created_at':  (m.created_at.replace(tzinfo=timezone.utc).isoformat()
                            if m.created_at else None),
        }
        for m in msgs
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

def _send_room_pushes(room_id: int, sender_user_id: int,
                      title: str, body: str, data: dict) -> None:
    """Background task: push to every non-blocked, non-muted member of a room.

    Runs on the async_dispatch thread pool — NO request context and NO implicit
    ORM tenant scope. Isolation is explicit: members are selected by room_id
    with is_blocked=False / is_muted=False, the sender is excluded, and the
    room's school ownership was already verified by the API route BEFORE the
    message committed (this task is only ever queued after that check). Device
    delivery is per-user isolated inside send_push_to_user(). Never raises.
    """
    from app.services.fcm_service import send_push_to_user
    try:
        member_ids = [
            m.user_id
            for m in (ChatRoomMember.query
                      .filter_by(room_id=room_id, is_blocked=False, is_muted=False)
                      .with_entities(ChatRoomMember.user_id)
                      .all())
        ]
        for uid in member_ids:
            if uid == sender_user_id:
                continue
            try:
                send_push_to_user(uid, title, body, data)
            except Exception as exc:
                _log.warning('[chat_api] FCM push failed user_id=%s: %s', uid, exc)
    except Exception as exc:
        _log.error('[chat_api] _send_room_pushes error room_id=%s: %s', room_id, exc)


def _push_new_message(room: ChatRoom, msg: ChatMessage, sender_name: str) -> None:
    """Queue FCM pushes to all non-blocked, non-muted, non-sender room members.

    P0: the member query and every FCM HTTPS round-trip now run on the
    background dispatcher, so sending a message is no longer blocked by the
    fan-out size. Only primitives cross the thread boundary — the ORM room and
    message objects never leave this request. Called only AFTER the message
    commit, so a push failure can never affect the stored message.
    """
    try:
        from app.services import async_dispatch
        from app.services.fcm_service import is_enabled
        if not is_enabled():
            return
        ntype  = 'school_announcement' if room.is_announcement_only else 'chat_message'
        title  = (f'رسالة جديدة في {room.name}'
                  if room.type in ('group', 'announcement')
                  else f'رسالة جديدة من {sender_name}')
        body   = (msg.body or '[مرفق]')[:150]
        data   = {
            'type':        'message',    # Flutter routes on data['type']
            'route':       '/chat',      # Flutter navigates to this screen
            'ntype':       ntype,
            'room_id':     str(room.id),
            'message_id':  str(msg.id),
            'room_type':   room.type,    # helps Flutter choose private vs group view
            'sender_name': sender_name,  # display name for Flutter notification tap
        }
        async_dispatch.submit(
            _send_room_pushes, room.id, msg.sender_user_id, title, body, data,
        )
    except Exception as exc:
        _log.error('[chat_api] _push_new_message error: %s', exc)


# ─── List rooms ───────────────────────────────────────────────────────────────

@mobile_api_bp.route('/chat/rooms', methods=['GET'])
@jwt_required()
@role_required(*_CHAT_ROLES)
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

    # P1: batch the per-room lookups for this page — one grouped unread query,
    # two queries for all last messages, and one query for today's schedules —
    # instead of 3+ queries per room. All batches are keyed strictly by this
    # page's membership-verified room ids and the authenticated user id.
    page_room_ids = [room.id for room in rooms]
    unread_map = _unread_counts_for_rooms(page_room_ids, user.id)
    last_map   = _last_messages_for_rooms(page_room_ids)
    today_dow  = (_local_now_for(user.school).weekday() + 1) % 7  # Sun=0 scheme
    sched_map  = {
        s.room_id: s
        for s in ChatRoomSchedule.query
        .filter(ChatRoomSchedule.room_id.in_(page_room_ids),
                ChatRoomSchedule.day_of_week == today_dow)
        .all()
    } if page_room_ids else {}

    rooms_out = []
    for room in rooms:
        mem = mem_map.get(room.id)
        can_send_flag = False
        if mem and not mem.is_blocked and not room.is_closed and room.allow_replies:
            if not room.is_announcement_only or mem.role in ('owner', 'admin'):
                sched_ok, _ = _can_send_now(room, user.school,
                                            today_sch=sched_map.get(room.id))
                can_send_flag = sched_ok

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
            'is_muted':            mem.is_muted if mem else False,
            'unread_count':        unread_map.get(room.id, 0),
            'last_message':        last_map.get(room.id),
        })

    return ok(rooms=rooms_out, total=total, limit=limit, offset=offset)


# ─── Room detail ─────────────────────────────────────────────────────────────

@mobile_api_bp.route('/chat/rooms/<int:room_id>', methods=['GET'])
@jwt_required()
@role_required(*_CHAT_ROLES)
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
    # Per-day schedule check — always evaluate today's row regardless of other days
    if can_send:
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
            'role':        mem.role,
            'is_blocked':  mem.is_blocked,
            'is_muted':    mem.is_muted,
            'can_send':    can_send,
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
@role_required(*_CHAT_ROLES)
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
@role_required(*_CHAT_ROLES)
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

    # Per-day schedule check — always evaluate today's row regardless of other days
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
@role_required(*_CHAT_ROLES)
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

    # P1: one anti-join SELECT for the missing receipt ids + one bulk INSERT —
    # replaces loading the user's entire read history plus every room message.
    # Scope: this room's messages only, receipts for this user only.
    unread_ids = db.session.execute(
        select(ChatMessage.id)
        .outerjoin(
            ChatMessageRead,
            and_(
                ChatMessageRead.message_id == ChatMessage.id,
                ChatMessageRead.user_id == user.id,
            ),
        )
        .where(
            ChatMessage.room_id == room.id,
            ChatMessage.is_deleted == False,
            ChatMessage.sender_user_id != user.id,
            ChatMessageRead.id.is_(None),
        )
    ).scalars().all()

    if unread_ids:
        try:
            db.session.execute(
                insert(ChatMessageRead),
                [{'message_id': mid, 'user_id': user.id} for mid in unread_ids],
            )
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            # A concurrent mark-read won the race — receipts exist; acceptable.

    return ok(marked=len(unread_ids))


# ─── Mute / unmute room ──────────────────────────────────────────────────────

@mobile_api_bp.route('/chat/rooms/<int:room_id>/mute', methods=['POST'])
@jwt_required()
@role_required(*_CHAT_ROLES)
def chat_mute_room(room_id):
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

    mem.is_muted = True
    db.session.commit()
    _log.info('[chat_api] muted room_id=%s user_id=%s', room_id, user.id)
    return ok(message='تم كتم إشعارات المحادثة.', is_muted=True)


@mobile_api_bp.route('/chat/rooms/<int:room_id>/unmute', methods=['POST'])
@jwt_required()
@role_required(*_CHAT_ROLES)
def chat_unmute_room(room_id):
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

    mem.is_muted = False
    db.session.commit()
    _log.info('[chat_api] unmuted room_id=%s user_id=%s', room_id, user.id)
    return ok(message='تم تفعيل إشعارات المحادثة.', is_muted=False)


# ─── Available contacts ───────────────────────────────────────────────────────

@mobile_api_bp.route('/chat/contacts', methods=['GET'])
@jwt_required()
@role_required(*_CHAT_ROLES)
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
                # P1: two batched queries instead of 2 lookups per employee.
                # Same guards as before, now as explicit SQL filters:
                # Employee.school_id == school AND the linked User is active
                # and in the SAME school — an Employee/User school mismatch
                # must never surface a teacher from another school as a contact.
                emp_rows = (
                    Employee.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter(
                        Employee.id.in_(emp_ids),
                        Employee.school_id == school_id,
                        Employee.user_id.isnot(None),
                    )
                    .all()
                ) if emp_ids else []
                teacher_users = {
                    u.id: u
                    for u in User.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter(
                        User.id.in_([e.user_id for e in emp_rows]),
                        User.school_id == school_id,
                        User.is_active.is_(True),
                    )
                    .all()
                } if emp_rows else {}
                for emp_row in emp_rows:
                    u = teacher_users.get(emp_row.user_id)
                    if u:
                        contacts.append({
                            'user_id':  u.id,
                            'name':     u.full_name,
                            'role':     'teacher',
                            'job_title': emp_row.job_title,
                            'photo':    photo_url(emp_row.photo),
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
                    # P1: one batched query instead of one lookup per parent.
                    # Same guards as before as explicit filters: active users
                    # in the teacher's OWN school only.
                    parents_batch = (
                        User.query
                        .execution_options(bypass_tenant_scope=True)
                        .filter(
                            User.id.in_(parent_ids),
                            User.school_id == school_id,
                            User.is_active.is_(True),
                        )
                        .all()
                    ) if parent_ids else []
                    for p in parents_batch:
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

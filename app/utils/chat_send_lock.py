"""
Per-room serialization of chat message inserts.

Every path that creates a ChatMessage calls lock_room_for_message_insert()
BEFORE the message is added to the session. The chat_rooms row stays locked
(SELECT ... FOR NO KEY UPDATE) until the sending transaction commits or rolls
back,
so within one room a message id is only ever allocated while no other sender
holds an uncommitted message. Per room, id order therefore equals commit
order: once a reader can see message id H, every lower id of that room that
will ever commit is already visible. That is the property an exact
``id > after_id`` poll cursor needs.

Sends to the same room already serialized on this row at commit time (every
send path updates room.updated_at, which takes this same NO KEY UPDATE lock);
the lock is only taken earlier, before the id is allocated. Different rooms
never contend. NO KEY UPDATE rather than plain FOR UPDATE: it conflicts with
other senders exactly the same way, but not with the KEY SHARE lock that
foreign-key inserts take on the room (members, schedules, messages), so it
blocks nothing that the commit-time UPDATE did not already block.
"""
from __future__ import annotations

from sqlalchemy import select

from app.models import db, ChatRoom


class ChatRoomLockError(Exception):
    """The room row no longer exists for the verified (room_id, school_id)."""


def lock_room_for_message_insert(room_id: int, school_id: int) -> None:
    """Lock the room row for the rest of the current transaction.

    Callers pass the ids of a room they have ALREADY authorized (membership,
    school, send permission). The lock re-binds both ids explicitly, so it can
    never lock — or confirm the existence of — another school's room. It adds
    no new access rule of its own: if the row is gone, the send must abort.
    Must be called before the ChatMessage is added to the session, and with
    nothing else pending, so the lock is the transaction's first write-intent.
    """
    locked = db.session.execute(
        select(ChatRoom.id)
        .where(ChatRoom.id == room_id, ChatRoom.school_id == school_id)
        .with_for_update(key_share=True)          # FOR NO KEY UPDATE
        .execution_options(bypass_tenant_scope=True)
    ).scalar_one_or_none()
    if locked is None:
        raise ChatRoomLockError(room_id)

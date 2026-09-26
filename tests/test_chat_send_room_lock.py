"""
Chat message sends are serialized per room (lock before id allocation).

Send paths covered (the only three places that create a ChatMessage):
  web_admin : POST /chat/rooms/<id>                        (school admin)
  web_user  : POST /chat/my-rooms/<id>                     (room member)
  mobile    : POST /api/mobile/v1/chat/rooms/<id>/messages (parent/teacher)

Proves, on the real routes against the isolated test DB:
  1. two concurrent sends to the same room serialize: while one send holds an
     allocated-but-uncommitted message, a second send (any path) waits on the
     room row lock and allocates no message id until the first commits;
  2. per room, commit order equals id order — the earlier committed message
     never has the higher id — including under a mixed 12-sender burst watched
     by a concurrent reader (every snapshot the reader takes is an id-prefix
     of the room's final messages, which is what an ``id > after_id`` cursor
     needs);
  3. existing send behaviour is unchanged: status codes, response JSON,
     sender read receipt (web only), room.updated_at, push hand-off, and every
     denial (blocked, closed, non-member, cross-school, unauthenticated).
"""
import threading
import time
import unittest
from unittest import mock
from uuid import uuid4

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app import create_app
from app.models import (
    db, Role, School, User, AuditLog,
    ChatRoom, ChatRoomMember, ChatMessage, ChatMessageRead,
)
from app.blueprints.mobile_api.utils import encode_token
from app.utils.chat_send_lock import ChatRoomLockError, lock_room_for_message_insert

PASSWORD = 'Test1234!'
WEB_MSG_KEYS = {'id', 'body', 'sender_name', 'created_at', 'is_self'}
MOBILE_MSG_KEYS = {'id', 'sender_id', 'sender_name', 'sender_role', 'body', 'message_type',
                   'attachment_url', 'created_at', 'is_mine', 'is_deleted'}
WAIT = 15            # seconds; generous upper bound for any single wait


def _uid():
    return uuid4().hex[:10]


class ChatSendRoomLockTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        with cls.app.app_context():
            cls.admin_role = Role.query.filter_by(name='school_admin').first()
            cls.parent_role = Role.query.filter_by(name='parent').first()
            assert cls.admin_role and cls.parent_role
            cls.engine = db.engine

    def setUp(self):
        self.sfx = _uid()
        # Push hand-off is asserted, never executed (no daemon threads, no FCM).
        self.web_push = mock.patch('app.blueprints.chat._push_chat_message').start()
        self.mobile_push = mock.patch(
            'app.blueprints.mobile_api.chat._push_new_message').start()
        self.addCleanup(mock.patch.stopall)
        with self.app.app_context():
            a = School(school_name=f'Lock School A {self.sfx}', code=f'LA{self.sfx[:8]}',
                       capacity=0, is_active=True)
            b = School(school_name=f'Lock School B {self.sfx}', code=f'LB{self.sfx[:8]}',
                       capacity=0, is_active=True)
            db.session.add_all([a, b])
            db.session.flush()

            def user(name, role, school):
                u = User(username=f'{name}_{self.sfx}', email=f'{name}_{self.sfx}@test.test',
                         full_name=f'{name} {self.sfx}', role_id=role.id,
                         school_id=school.id, is_active=True)
                u.set_password(PASSWORD)
                db.session.add(u)
                return u

            admin_a = user('ladmin_a', self.admin_role, a)
            admin_b = user('ladmin_b', self.admin_role, b)
            p1 = user('lparent_1', self.parent_role, a)
            p2 = user('lparent_2', self.parent_role, a)
            blocked = user('lparent_blk', self.parent_role, a)
            outsider = user('lparent_x', self.parent_role, a)
            foreign = user('lparent_b', self.parent_role, b)
            db.session.flush()

            room_a = ChatRoom(school_id=a.id, name=f'Lock Room {self.sfx}', type='group',
                              scope='custom', created_by_user_id=admin_a.id)
            room_b = ChatRoom(school_id=b.id, name=f'Lock Room B {self.sfx}', type='group',
                              scope='custom', created_by_user_id=admin_b.id)
            db.session.add_all([room_a, room_b])
            db.session.flush()
            for u in (p1, p2):
                db.session.add(ChatRoomMember(room_id=room_a.id, user_id=u.id, role='member'))
            db.session.add(ChatRoomMember(room_id=room_a.id, user_id=blocked.id,
                                          role='member', is_blocked=True))
            # Data inconsistency on purpose: a school-B parent holding a
            # membership row in a school-A room must still be refused.
            db.session.add(ChatRoomMember(room_id=room_a.id, user_id=foreign.id, role='member'))
            db.session.commit()

            self.ids = dict(school_a=a.id, school_b=b.id, admin_a=admin_a.id,
                            admin_b=admin_b.id, p1=p1.id, p2=p2.id, blocked=blocked.id,
                            outsider=outsider.id, foreign=foreign.id,
                            room_a=room_a.id, room_b=room_b.id)
            self.names = dict(admin_a=admin_a.username, admin_b=admin_b.username,
                              p1=p1.username, p2=p2.username, blocked=blocked.username,
                              outsider=outsider.username, foreign=foreign.username)
            self.tokens = {k: encode_token(db.session.get(User, self.ids[k]))
                           for k in ('p1', 'p2', 'blocked', 'outsider', 'foreign')}

    def tearDown(self):
        with self.app.app_context():
            sids = [self.ids['school_a'], self.ids['school_b']]
            room_ids = [r.id for r in ChatRoom.query.execution_options(bypass_tenant_scope=True)
                        .filter(ChatRoom.school_id.in_(sids)).all()]
            if room_ids:
                msg_ids = db.select(ChatMessage.id).where(ChatMessage.room_id.in_(room_ids))
                ChatMessageRead.query.filter(ChatMessageRead.message_id.in_(msg_ids)).delete(
                    synchronize_session=False)
                ChatMessage.query.filter(ChatMessage.room_id.in_(room_ids)).delete(
                    synchronize_session=False)
                ChatRoomMember.query.filter(ChatRoomMember.room_id.in_(room_ids)).delete(
                    synchronize_session=False)
            ChatRoom.query.execution_options(bypass_tenant_scope=True).filter(
                ChatRoom.school_id.in_(sids)).delete(synchronize_session=False)
            uids = [u.id for u in User.query.execution_options(bypass_tenant_scope=True)
                    .filter(User.school_id.in_(sids)).all()]
            if uids:
                AuditLog.query.execution_options(bypass_tenant_scope=True).filter(
                    AuditLog.user_id.in_(uids)).delete(synchronize_session=False)
            User.query.execution_options(bypass_tenant_scope=True).filter(
                User.school_id.in_(sids)).delete(synchronize_session=False)
            School.query.filter(School.id.in_(sids)).delete(synchronize_session=False)
            db.session.commit()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _web_client(self, key):
        client = self.app.test_client()
        resp = client.post('/auth/login', data={'username': self.names[key],
                                                'password': PASSWORD})
        self.assertIn(resp.status_code, (200, 302))
        return client

    def _send(self, path, body, key=None, client=None, room_key='room_a'):
        """One real send through ``path``; returns the Flask response."""
        rid = self.ids[room_key]
        if path == 'mobile':
            client = client or self.app.test_client()
            return client.post(f'/api/mobile/v1/chat/rooms/{rid}/messages',
                               json={'body': body},
                               headers={'Authorization': f'Bearer {self.tokens[key]}'})
        url = f'/chat/rooms/{rid}' if path == 'web_admin' else f'/chat/my-rooms/{rid}'
        return client.post(url, data={'body': body},
                           headers={'X-Requested-With': 'XMLHttpRequest'})

    def _sender(self, path):
        """(key, client) able to send on ``path`` in room_a."""
        if path == 'web_admin':
            return 'admin_a', self._web_client('admin_a')
        if path == 'web_user':
            return 'p2', self._web_client('p2')
        return 'p1', self.app.test_client()

    @staticmethod
    def _msg_id(path, resp):
        data = resp.get_json()
        return data['data']['id'] if path == 'mobile' else data['message']['id']

    def _room_messages(self, room_key='room_a'):
        with self.app.app_context():
            return db.session.query(ChatMessage).filter(
                ChatMessage.room_id == self.ids[room_key]).order_by(ChatMessage.id).all()

    def _room_updated_at(self):
        with self.app.app_context():
            return db.session.get(ChatRoom, self.ids['room_a']).updated_at

    def _seq_last_value(self, conn):
        return conn.execute(text(
            "SELECT pg_sequence_last_value("
            "pg_get_serial_sequence('chat_messages', 'id')::regclass)")).scalar()

    def _wait_for_room_lock_waiter(self, conn, thread):
        """True once a backend is blocked on the chat_rooms send lock; False if
        ``thread`` finished first (i.e. the send was NOT serialized)."""
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            n = conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                "AND query ILIKE '%chat_rooms%' AND query ILIKE '%FOR NO KEY UPDATE%'")).scalar()
            if n:
                return True
            if not thread.is_alive():
                return False
            time.sleep(0.02)
        return False

    # ── 1 + 2: two concurrent real sends serialize, commit order == id order ─

    def _pause_after_first_message_flush(self, thread_name):
        """Pause ``thread_name`` right after its ChatMessage INSERT is flushed
        (id allocated, transaction still open, room lock held); record the
        commit order of every transaction that inserted a ChatMessage."""
        flushed, release = threading.Event(), threading.Event()
        commits, local = [], threading.local()

        def after_flush(session, _ctx):
            if any(isinstance(o, ChatMessage) for o in session.new):
                local.inserted = True
                if threading.current_thread().name == thread_name and not flushed.is_set():
                    flushed.set()
                    release.wait(WAIT)

        def after_commit(_session):
            if getattr(local, 'inserted', False):
                local.inserted = False
                commits.append(threading.current_thread().name)

        event.listen(Session, 'after_flush', after_flush)
        event.listen(Session, 'after_commit', after_commit)

        def remove():
            release.set()
            event.remove(Session, 'after_flush', after_flush)
            event.remove(Session, 'after_commit', after_commit)
        self.addCleanup(remove)
        return flushed, release, commits

    def _assert_pair_serializes(self, first, second):
        k1, c1 = self._sender(first)
        k2, c2 = self._sender(second)
        flushed, release, commits = self._pause_after_first_message_flush('send-A')
        out = {}

        def run(name, path, key, client):
            out[name] = self._send(path, f'{name} via {path}', key=key, client=client)

        ta = threading.Thread(target=run, name='send-A', args=('A', first, k1, c1))
        tb = threading.Thread(target=run, name='send-B', args=('B', second, k2, c2))
        with self.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as mon:
            try:
                ta.start()
                self.assertTrue(flushed.wait(WAIT), 'send A never flushed its message')
                seq_a = self._seq_last_value(mon)            # A's id is allocated
                tb.start()
                self.assertTrue(self._wait_for_room_lock_waiter(mon, tb),
                                f'{second} send was not blocked by the room lock held '
                                f'by the uncommitted {first} send')
                self.assertEqual(self._seq_last_value(mon), seq_a,
                                 'send B allocated a message id while A was uncommitted')
                self.assertEqual(len(self._room_messages()), 0, 'nothing committed yet')
            finally:
                release.set()
                ta.join(WAIT)
                tb.join(WAIT)
        self.assertFalse(ta.is_alive() or tb.is_alive())
        ok_status = {'web_admin': 200, 'web_user': 200, 'mobile': 201}
        self.assertEqual(out['A'].status_code, ok_status[first], out['A'].get_data(True))
        self.assertEqual(out['B'].status_code, ok_status[second], out['B'].get_data(True))
        id_a, id_b = self._msg_id(first, out['A']), self._msg_id(second, out['B'])
        self.assertEqual(commits, ['send-A', 'send-B'], 'commit order')
        self.assertLess(id_a, id_b, 'the earlier committed message must have the lower id')
        self.assertEqual([m.id for m in self._room_messages()], [id_a, id_b])

    def test_concurrent_web_admin_then_mobile_serialize(self):
        self._assert_pair_serializes('web_admin', 'mobile')

    def test_concurrent_mobile_then_web_user_serialize(self):
        self._assert_pair_serializes('mobile', 'web_user')

    def test_concurrent_web_user_then_web_admin_serialize(self):
        self._assert_pair_serializes('web_user', 'web_admin')

    def test_send_waits_for_external_room_lock_before_allocating_id(self):
        """A foreign transaction holding the room row blocks each send path
        BEFORE any id is taken; its own lower id commits first."""
        for path in ('web_admin', 'web_user', 'mobile'):
            with self.subTest(path=path):
                key, client = self._sender(path)
                out = {}
                t = threading.Thread(target=lambda: out.setdefault(
                    'r', self._send(path, f'after holder {path}', key=key, client=client)))
                with self.engine.connect() as holder, \
                        self.engine.connect().execution_options(
                            isolation_level='AUTOCOMMIT') as mon:
                    tx = holder.begin()
                    try:
                        holder.execute(text('SELECT id FROM chat_rooms WHERE id = :r FOR NO KEY UPDATE'),
                                       {'r': self.ids['room_a']})
                        before = self._seq_last_value(mon)
                        t.start()
                        self.assertTrue(self._wait_for_room_lock_waiter(mon, t),
                                        f'{path} send did not wait for the room lock')
                        self.assertEqual(self._seq_last_value(mon), before,
                                         f'{path} allocated an id before holding the room lock')
                        holder_id = holder.execute(text(
                            "INSERT INTO chat_messages (room_id, sender_user_id, body, "
                            "message_type, is_deleted, created_at, updated_at) "
                            "VALUES (:r, :s, 'holder', 'text', false, now(), now()) "
                            "RETURNING id"), {'r': self.ids['room_a'],
                                              's': self.ids['p2']}).scalar()
                        tx.commit()
                    finally:
                        if tx.is_active:
                            tx.rollback()
                        t.join(WAIT)
                self.assertFalse(t.is_alive())
                self.assertIn(out['r'].status_code, (200, 201), out['r'].get_data(True))
                self.assertGreater(self._msg_id(path, out['r']), holder_id)

    def test_mixed_burst_every_reader_snapshot_is_an_id_prefix(self):
        """12 concurrent sends over all three paths while a reader polls: no
        snapshot ever sees a message whose lower-id sibling commits later."""
        senders = []
        for path in ('web_admin', 'web_user', 'mobile'):
            for _ in range(4):
                senders.append((path, *self._sender(path)))
        barrier = threading.Barrier(len(senders))
        results, stop, snapshots = [], threading.Event(), []

        def send(i, path, key, client):
            barrier.wait(WAIT)
            resp = self._send(path, f'burst {i} {path}', key=key, client=client)
            results.append((path, resp.status_code, self._msg_id(path, resp)
                            if resp.status_code in (200, 201) else None))

        def watch():
            with self.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as c:
                while not stop.is_set():
                    snapshots.append(frozenset(c.execute(text(
                        'SELECT id FROM chat_messages WHERE room_id = :r'),
                        {'r': self.ids['room_a']}).scalars()))

        watcher = threading.Thread(target=watch)
        watcher.start()
        threads = [threading.Thread(target=send, args=(i, *s)) for i, s in enumerate(senders)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(WAIT * 2)
        stop.set()
        watcher.join(WAIT)

        self.assertEqual(len(results), 12)
        self.assertTrue(all(code in (200, 201) for _, code, _ in results), results)
        final = sorted(m.id for m in self._room_messages())
        self.assertEqual(final, sorted(mid for _, _, mid in results))
        self.assertGreater(len(snapshots), 1)
        for snap in snapshots:
            if snap:
                expected_prefix = {i for i in final if i <= max(snap)}
                self.assertEqual(snap, expected_prefix,
                                 'a reader saw a higher id before a lower id committed')

    # ── 3: existing behaviour unchanged ──────────────────────────────────────

    def test_web_admin_send_unchanged(self):
        before = self._room_updated_at()
        resp = self._send('web_admin', 'hello admin', client=self._web_client('admin_a'))
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['ok'])
        self.assertEqual(set(data['message']), WEB_MSG_KEYS)
        self.assertEqual(data['message']['body'], 'hello admin')
        self.assertTrue(data['message']['is_self'])
        [msg] = self._room_messages()
        self.assertEqual((msg.id, msg.sender_user_id, msg.message_type),
                         (data['message']['id'], self.ids['admin_a'], 'text'))
        with self.app.app_context():                 # sender's own receipt, as before
            self.assertEqual(ChatMessageRead.query.filter_by(
                message_id=msg.id, user_id=self.ids['admin_a']).count(), 1)
        self.assertGreater(self._room_updated_at(), before)
        self.web_push.assert_called_once()
        push = self.web_push.call_args.args[1]
        self.assertEqual((push['room_id'], push['msg_id'], push['sender_user_id']),
                         (self.ids['room_a'], msg.id, self.ids['admin_a']))

    def test_web_user_send_unchanged(self):
        before = self._room_updated_at()
        resp = self._send('web_user', 'hello member', client=self._web_client('p2'))
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['ok'])
        self.assertEqual(set(data['message']), WEB_MSG_KEYS)
        [msg] = self._room_messages()
        self.assertEqual(msg.sender_user_id, self.ids['p2'])
        with self.app.app_context():
            self.assertEqual(ChatMessageRead.query.filter_by(
                message_id=msg.id, user_id=self.ids['p2']).count(), 1)
        self.assertGreater(self._room_updated_at(), before)
        self.web_push.assert_called_once()
        self.assertEqual(self.web_push.call_args.args[1]['msg_id'], msg.id)

    def test_mobile_send_unchanged(self):
        before = self._room_updated_at()
        resp = self._send('mobile', 'hello mobile', key='p1')
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertEqual(data['ok'], True)
        self.assertEqual(data['message'], 'تم إرسال الرسالة بنجاح.')
        self.assertEqual(set(data['data']), MOBILE_MSG_KEYS)
        self.assertEqual((data['data']['body'], data['data']['is_mine'],
                          data['data']['is_deleted']), ('hello mobile', True, False))
        [msg] = self._room_messages()
        self.assertEqual((msg.id, msg.sender_user_id), (data['data']['id'], self.ids['p1']))
        with self.app.app_context():                 # mobile never wrote a sender receipt
            self.assertEqual(ChatMessageRead.query.filter_by(message_id=msg.id).count(), 0)
        self.assertGreater(self._room_updated_at(), before)
        self.mobile_push.assert_called_once()
        self.assertEqual(self.mobile_push.call_args.args[1].id, msg.id)

    def test_denials_unchanged_and_create_nothing(self):
        # mobile: blocked member, non-member, cross-school membership, no token
        blocked = self._send('mobile', 'x', key='blocked')
        self.assertEqual(blocked.status_code, 403)
        self.assertIn('تم تقييدك', blocked.get_json()['error'])
        self.assertEqual(self._send('mobile', 'x', key='outsider').status_code, 403)
        foreign = self._send('mobile', 'x', key='foreign')
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.get_json()['error'], 'المحادثة غير موجودة.')
        self.assertEqual(self.app.test_client().post(
            f"/api/mobile/v1/chat/rooms/{self.ids['room_a']}/messages",
            json={'body': 'x'}).status_code, 401)
        # web user: blocked member, non-member
        resp = self._send('web_user', 'x', client=self._web_client('blocked'))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('تم تقييدك', resp.get_json()['error'])
        self.assertEqual(self._send('web_user', 'x', client=self._web_client('outsider'))
                         .status_code, 403)
        # web admin: other school's admin cannot reach room_a
        self.assertEqual(self._send('web_admin', 'x', client=self._web_client('admin_b'))
                         .status_code, 404)
        # closed room: every path refuses
        with self.app.app_context():
            db.session.get(ChatRoom, self.ids['room_a']).is_closed = True
            db.session.commit()
        self.assertEqual(self._send('web_admin', 'x', client=self._web_client('admin_a'))
                         .status_code, 400)
        self.assertEqual(self._send('web_user', 'x', client=self._web_client('p2'))
                         .status_code, 400)
        self.assertEqual(self._send('mobile', 'x', key='p1').status_code, 403)

        self.assertEqual(self._room_messages(), [])
        self.web_push.assert_not_called()
        self.mobile_push.assert_not_called()

    def test_send_lock_does_not_block_foreign_key_inserts_on_the_room(self):
        """The send lock conflicts only with other senders: adding a member to
        the room (FK -> KEY SHARE on chat_rooms) proceeds while it is held."""
        with self.engine.connect() as holder, self.engine.connect() as other:
            tx = holder.begin()
            try:
                holder.execute(text('SELECT id FROM chat_rooms WHERE id = :r FOR NO KEY UPDATE'),
                               {'r': self.ids['room_a']})
                with other.begin():
                    other.execute(text("SET LOCAL lock_timeout = '2s'"))
                    other.execute(text(
                        "INSERT INTO chat_room_members (room_id, user_id, role, is_muted, "
                        "is_blocked) VALUES (:r, :u, 'member', false, false)"),
                        {'r': self.ids['room_a'], 'u': self.ids['outsider']})
            finally:
                tx.rollback()
        with self.app.app_context():
            self.assertIsNotNone(ChatRoomMember.query.filter_by(
                room_id=self.ids['room_a'], user_id=self.ids['outsider']).first())

    def test_lock_helper_is_bound_to_the_verified_school(self):
        with self.app.app_context():
            lock_room_for_message_insert(self.ids['room_a'], self.ids['school_a'])
            db.session.rollback()
            with self.assertRaises(ChatRoomLockError):   # right room, wrong school
                lock_room_for_message_insert(self.ids['room_a'], self.ids['school_b'])
            db.session.rollback()
            with self.assertRaises(ChatRoomLockError):   # room that does not exist
                lock_room_for_message_insert(0, self.ids['school_a'])
            db.session.rollback()


if __name__ == '__main__':
    unittest.main()

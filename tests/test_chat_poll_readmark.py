"""
Web-chat room poll: read receipts, isolation and bounded query shape.

GET /chat/rooms/<id>/poll      (school admin, manage_chat)
GET /chat/my-rooms/<id>/poll   (room member: parent / teacher)

Covers:
  - empty room, history with no new messages, one and several new messages
  - read receipts: created exactly once, never duplicated by repeated polls,
    independent per member, never created for the reader's own messages or
    for soft-deleted messages
  - non-member / cross-school denial (unchanged)
  - response JSON shape and ordering (unchanged)
  - room open and "load older" still mark the whole room read
  - the commit-order look-back: an unread message just below the cursor is
    still marked read by the poll
  - query shape: a poll with no new messages fetches the same number of rows
    from chat tables for a 10-message room and a 10,000-message room
  - pages that are not an open room never reference a room poll URL
"""
import unittest
from uuid import uuid4

from sqlalchemy import event

from app import create_app
from app.blueprints.chat import _POLL_READ_LOOKBACK_IDS
from app.models import (
    db, Role, School, User, AuditLog,
    ChatRoom, ChatRoomMember, ChatMessage, ChatMessageRead,
)

PASSWORD = 'Test1234!'
POLL_KEYS = {'id', 'body', 'sender_name', 'created_at', 'is_self'}


def _uid():
    return uuid4().hex[:10]


class ChatPollReadMarkTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        with cls.app.app_context():
            cls.admin_role = Role.query.filter_by(name='school_admin').first()
            cls.parent_role = Role.query.filter_by(name='parent').first()
            assert cls.admin_role and cls.parent_role

    def setUp(self):
        self.sfx = _uid()
        self.client = self.app.test_client()
        with self.app.app_context():
            a = School(school_name=f'Poll School A {self.sfx}', code=f'PA{self.sfx[:8]}',
                       capacity=0, is_active=True)
            b = School(school_name=f'Poll School B {self.sfx}', code=f'PB{self.sfx[:8]}',
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

            admin_a = user('padmin_a', self.admin_role, a)
            admin_b = user('padmin_b', self.admin_role, b)
            p1 = user('pparent_1', self.parent_role, a)
            p2 = user('pparent_2', self.parent_role, a)
            outsider = user('pparent_x', self.parent_role, a)
            db.session.flush()

            def room(name, school, creator):
                r = ChatRoom(school_id=school.id, name=f'{name} {self.sfx}', type='group',
                             scope='custom', created_by_user_id=creator.id)
                db.session.add(r)
                db.session.flush()
                return r

            room_a = room('Room A', a, admin_a)
            room_empty = room('Empty', a, admin_a)
            room_big = room('Big', a, admin_a)
            for r in (room_a, room_empty, room_big):
                for u in (p1, p2):
                    db.session.add(ChatRoomMember(room_id=r.id, user_id=u.id, role='member'))
            db.session.commit()

            self.ids = dict(
                school_a=a.id, school_b=b.id, admin_a=admin_a.id, admin_b=admin_b.id,
                p1=p1.id, p2=p2.id, outsider=outsider.id,
                room_a=room_a.id, room_empty=room_empty.id, room_big=room_big.id,
            )
            self.names = dict(admin_a=admin_a.username, admin_b=admin_b.username,
                              p1=p1.username, p2=p2.username, outsider=outsider.username)

    def tearDown(self):
        with self.app.app_context():
            sids = [self.ids['school_a'], self.ids['school_b']]
            room_ids = [r.id for r in ChatRoom.query.execution_options(bypass_tenant_scope=True)
                        .filter(ChatRoom.school_id.in_(sids)).all()]
            if room_ids:
                msg_ids = db.select(ChatMessage.id).where(
                    ChatMessage.room_id.in_(room_ids))
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

    def _login(self, key):
        self.client.get('/auth/logout')
        resp = self.client.post('/auth/login', data={'username': self.names[key],
                                                     'password': PASSWORD})
        self.assertIn(resp.status_code, (200, 302))

    def _add_messages(self, room_key, sender_key, n, deleted=False):
        with self.app.app_context():
            rows = [dict(room_id=self.ids[room_key], sender_user_id=self.ids[sender_key],
                         body=f'm{i}', message_type='text', is_deleted=deleted)
                    for i in range(n)]
            db.session.execute(ChatMessage.__table__.insert(), rows)
            db.session.commit()
            return [r[0] for r in db.session.query(ChatMessage.id)
                    .filter(ChatMessage.room_id == self.ids[room_key])
                    .order_by(ChatMessage.id.desc()).limit(n).all()][::-1]

    def _reads(self, room_key, user_key):
        with self.app.app_context():
            return (db.session.query(ChatMessageRead.message_id)
                    .join(ChatMessage, ChatMessage.id == ChatMessageRead.message_id)
                    .filter(ChatMessage.room_id == self.ids[room_key],
                            ChatMessageRead.user_id == self.ids[user_key])
                    .count())

    def _user_poll(self, room_key, after_id):
        return self.client.get(f'/chat/my-rooms/{self.ids[room_key]}/poll?after_id={after_id}')

    def _admin_poll(self, room_key, after_id):
        return self.client.get(f'/chat/rooms/{self.ids[room_key]}/poll?after_id={after_id}')

    def _max_id(self, room_key):
        with self.app.app_context():
            return db.session.query(db.func.max(ChatMessage.id)).filter(
                ChatMessage.room_id == self.ids[room_key]).scalar() or 0

    # ── behaviour ─────────────────────────────────────────────────────────────

    def test_empty_room_poll(self):
        self._login('p1')
        self.assertEqual(self.client.get(f"/chat/my-rooms/{self.ids['room_empty']}").status_code, 200)
        resp = self._user_poll('room_empty', 0)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {'messages': []})
        self.assertEqual(self._reads('room_empty', 'p1'), 0)

    def test_history_then_no_new_messages(self):
        self._add_messages('room_a', 'p2', 30)
        self._login('p1')
        self.client.get(f"/chat/my-rooms/{self.ids['room_a']}")      # room open marks all
        self.assertEqual(self._reads('room_a', 'p1'), 30)
        cursor = self._max_id('room_a')
        for _ in range(3):
            resp = self._user_poll('room_a', cursor)
            self.assertEqual(resp.get_json(), {'messages': []})
        self.assertEqual(self._reads('room_a', 'p1'), 30)            # no duplicates

    def test_one_new_message_read_once(self):
        self._add_messages('room_a', 'p2', 5)
        self._login('p1')
        self.client.get(f"/chat/my-rooms/{self.ids['room_a']}")
        cursor = self._max_id('room_a')
        [new_id] = self._add_messages('room_a', 'p2', 1)
        resp = self._user_poll('room_a', cursor)
        msgs = resp.get_json()['messages']
        self.assertEqual([m['id'] for m in msgs], [new_id])
        self.assertEqual(set(msgs[0]), POLL_KEYS)
        self.assertFalse(msgs[0]['is_self'])
        self.assertEqual(self._reads('room_a', 'p1'), 6)
        self._user_poll('room_a', new_id)
        self._user_poll('room_a', cursor)                           # replayed cursor
        self.assertEqual(self._reads('room_a', 'p1'), 6)

    def test_several_new_messages_ordered(self):
        self._login('p1')
        self.client.get(f"/chat/my-rooms/{self.ids['room_a']}")
        new_ids = self._add_messages('room_a', 'p2', 7)
        msgs = self._user_poll('room_a', 0).get_json()['messages']
        self.assertEqual([m['id'] for m in msgs], sorted(new_ids))
        self.assertEqual(self._reads('room_a', 'p1'), 7)

    def test_own_and_deleted_messages_get_no_receipt(self):
        self._login('p1')
        self.client.get(f"/chat/my-rooms/{self.ids['room_a']}")
        self._add_messages('room_a', 'p1', 3)                          # own
        self._add_messages('room_a', 'p2', 2, deleted=True)           # soft-deleted
        self._user_poll('room_a', 0)
        self.assertEqual(self._reads('room_a', 'p1'), 0)

    def test_members_have_independent_read_state(self):
        self._add_messages('room_a', 'admin_a', 4)
        self._login('p1')
        self._user_poll('room_a', 0)
        self.assertEqual(self._reads('room_a', 'p1'), 4)
        self.assertEqual(self._reads('room_a', 'p2'), 0)
        self._login('p2')
        self._user_poll('room_a', 0)
        self.assertEqual(self._reads('room_a', 'p2'), 4)
        self.assertEqual(self._reads('room_a', 'p1'), 4)

    def test_lookback_marks_late_committed_message(self):
        """A message below the cursor that is still unread (the commit-order
        race) is marked read exactly as the old full-room scan did."""
        ids = self._add_messages('room_a', 'p2', 10)
        self._login('p1')
        self.client.get(f"/chat/my-rooms/{self.ids['room_a']}")
        with self.app.app_context():                # simulate the late commit
            ChatMessageRead.query.filter_by(message_id=ids[3], user_id=self.ids['p1']).delete()
            db.session.commit()
        self.assertEqual(self._reads('room_a', 'p1'), 9)
        self._user_poll('room_a', ids[-1])
        self.assertEqual(self._reads('room_a', 'p1'), 10)
        self.assertLess(ids[-1] - ids[3], _POLL_READ_LOOKBACK_IDS)

    def test_room_open_and_load_older_still_mark_whole_room(self):
        ids = self._add_messages('room_big', 'p2', 300)
        self._login('admin_a')
        self.assertEqual(self.client.get(f"/chat/rooms/{self.ids['room_big']}").status_code, 200)
        self.assertEqual(self._reads('room_big', 'admin_a'), 300)
        with self.app.app_context():
            ChatMessageRead.query.filter_by(message_id=ids[0], user_id=self.ids['admin_a']).delete()
            db.session.commit()
        resp = self.client.get(f"/chat/rooms/{self.ids['room_big']}/messages/older"
                               f"?before_id={ids[200]}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()['messages']), 100)
        self.assertEqual(self._reads('room_big', 'admin_a'), 300)

    def test_initial_history_renders(self):
        ids = self._add_messages('room_a', 'p2', 12)
        self._login('p1')
        html = self.client.get(f"/chat/my-rooms/{self.ids['room_a']}").get_data(as_text=True)
        for mid in ids:
            self.assertIn(f'data-msg-id="{mid}"', html)
        self.assertIn(f"/chat/my-rooms/{self.ids['room_a']}/poll", html)

    def test_admin_poll_same_school(self):
        self._add_messages('room_a', 'p1', 3)
        self._login('admin_a')
        resp = self._admin_poll('room_a', 0)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()['messages']), 3)
        self.assertEqual(self._reads('room_a', 'admin_a'), 3)

    def test_non_member_denied(self):
        self._add_messages('room_a', 'p2', 3)
        self._login('outsider')
        self.assertEqual(self._user_poll('room_a', 0).status_code, 403)
        self.assertEqual(self._reads('room_a', 'outsider'), 0)

    def test_cross_school_admin_denied(self):
        self._add_messages('room_a', 'p2', 3)
        self._login('admin_b')
        self.assertEqual(self._admin_poll('room_a', 0).status_code, 404)
        self.assertEqual(self._user_poll('room_a', 0).status_code, 404)
        self.assertEqual(self._reads('room_a', 'admin_b'), 0)

    def test_unauthenticated_poll_redirects(self):
        self.client.get('/auth/logout')
        resp = self._user_poll('room_a', 0)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/auth/login', resp.headers['Location'])

    # ── query shape ───────────────────────────────────────────────────────────

    def _chat_rows_fetched_during(self, fn):
        """Rows returned by SELECTs on chat_messages / chat_message_reads."""
        stats = {'rows': 0, 'selects': 0, 'sql': []}
        with self.app.app_context():
            engine = db.engine

        def after(conn, cursor, statement, params, context, executemany):
            s = statement.lower()
            if s.lstrip().startswith('select') and ('chat_messages' in s or 'chat_message_reads' in s):
                stats['selects'] += 1
                stats['sql'].append(statement)
                if cursor.description is not None and cursor.rowcount and cursor.rowcount > 0:
                    stats['rows'] += cursor.rowcount

        with self.app.app_context():
            event.listen(engine, 'after_cursor_execute', after)
        try:
            fn()
        finally:
            with self.app.app_context():
                event.remove(engine, 'after_cursor_execute', after)
        return stats

    def test_no_new_message_poll_is_independent_of_history(self):
        self._add_messages('room_a', 'p2', 10)
        self._add_messages('room_big', 'p2', 10_000)
        self._login('p1')
        self.client.get(f"/chat/my-rooms/{self.ids['room_a']}")
        self.client.get(f"/chat/my-rooms/{self.ids['room_big']}")
        self.assertEqual(self._reads('room_big', 'p1'), 10_000)

        small_cursor, big_cursor = self._max_id('room_a'), self._max_id('room_big')
        small = self._chat_rows_fetched_during(lambda: self._user_poll('room_a', small_cursor))
        big = self._chat_rows_fetched_during(lambda: self._user_poll('room_big', big_cursor))

        self.assertEqual(small['rows'], 0, small['sql'])
        self.assertEqual(big['rows'], 0, big['sql'])
        self.assertEqual(small['selects'], big['selects'])
        readmark = [s for s in big['sql'] if 'chat_message_reads' in s.lower()]
        self.assertEqual(len(readmark), 1, big['sql'])
        self.assertIn('chat_messages.id >', readmark[0])
        self.assertIn('LEFT OUTER JOIN chat_message_reads', readmark[0])

    def test_one_new_message_in_large_room_scales_with_new_data(self):
        self._add_messages('room_big', 'p2', 10_000)
        self._login('p1')
        self.client.get(f"/chat/my-rooms/{self.ids['room_big']}")
        cursor = self._max_id('room_big')
        [new_id] = self._add_messages('room_big', 'p2', 1)
        stats = self._chat_rows_fetched_during(lambda: self._user_poll('room_big', cursor))
        # 1 unread id from the anti-join + 1 message row for the response.
        self.assertEqual(stats['rows'], 2, stats['sql'])
        self.assertEqual(self._reads('room_big', 'p1'), 10_001)

    # ── pages that are not an open room never poll a room ────────────────────

    def test_non_room_pages_reference_no_room_poll(self):
        self._login('admin_a')
        for path in ('/chat/', '/admin/dashboard', '/students/'):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            html = resp.get_data(as_text=True)
            self.assertNotIn('/poll', html, path)
            self.assertNotIn('POLL_URL', html, path)
            self.assertIn('/live/badges', html, path)     # global badge poll kept
        self._login('p1')
        resp = self.client.get('/chat/my-rooms')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('/poll', resp.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()

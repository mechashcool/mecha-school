"""
Mobile chat messages endpoint: optional ``after_id`` forward cursor.

GET /api/mobile/v1/chat/rooms/<id>/messages

Without ``after_id`` the endpoint is unchanged: newest ``limit`` messages by
created_at, returned oldest-first; ``before`` filters id < before (malformed
``before`` ignored); limit default 50, max 100, malformed/negative -> 50.

With ``after_id`` (plain non-negative integer): only this room's messages with
id > after_id, ascending id, the FIRST ``limit`` rows — repeated calls drain a
backlog with no gap and no duplicate. Malformed/negative -> 400
``invalid after_id``; combined with ``before`` -> 400. Same envelope, same
message schema, deleted rows keep their representation, no read receipts.

Also proves, with real concurrent sends over all three send paths, that an
``after_id`` poller never observes a message id H while a lower id of the same
room is still uncommitted (the room-lock ordering this cursor relies on).
"""
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
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

PASSWORD = 'Test1234!'
ENVELOPE_KEYS = {'ok', 'room_id', 'count', 'messages'}
MSG_KEYS = {'id', 'sender_id', 'sender_name', 'sender_role', 'body', 'message_type',
            'attachment_url', 'created_at', 'is_mine', 'is_deleted'}
NOT_MEMBER = 'لست عضواً في هذه المحادثة.'
NOT_FOUND = 'المحادثة غير موجودة.'
BAD_CURSOR = 'invalid after_id'
MIXED = 'invalid pagination: use before or after_id, not both'
WAIT = 15


def _uid():
    return uuid4().hex[:10]


class MobileChatAfterIdTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        with cls.app.app_context():
            cls.roles = {n: Role.query.filter_by(name=n).first()
                         for n in ('school_admin', 'parent', 'teacher')}
            assert all(cls.roles.values()), cls.roles
            cls.engine = db.engine

    def setUp(self):
        self.sfx = _uid()
        self.client = self.app.test_client()
        self._clock = datetime(2026, 1, 1, 8, 0, 0)
        with self.app.app_context():
            a = School(school_name=f'Cursor School A {self.sfx}', code=f'CA{self.sfx[:8]}',
                       capacity=0, is_active=True)
            b = School(school_name=f'Cursor School B {self.sfx}', code=f'CB{self.sfx[:8]}',
                       capacity=0, is_active=True)
            db.session.add_all([a, b])
            db.session.flush()

            def user(name, role, school):
                u = User(username=f'{name}_{self.sfx}', email=f'{name}_{self.sfx}@test.test',
                         full_name=f'{name} {self.sfx}', role_id=self.roles[role].id,
                         school_id=school.id, is_active=True)
                u.set_password(PASSWORD)
                db.session.add(u)
                return u

            users = dict(
                admin_a=user('cadmin_a', 'school_admin', a),
                p1=user('cparent_1', 'parent', a),
                p2=user('cparent_2', 'parent', a),
                t1=user('cteacher_1', 'teacher', a),
                outsider=user('cparent_x', 'parent', a),
                foreign=user('cparent_b', 'parent', b),
                p_b=user('cparent_b2', 'parent', b),
            )
            db.session.flush()

            def room(name, school, creator):
                r = ChatRoom(school_id=school.id, name=f'{name} {self.sfx}', type='group',
                             scope='custom', created_by_user_id=creator.id)
                db.session.add(r)
                db.session.flush()
                return r

            room_a = room('Cursor Room', a, users['admin_a'])
            room_other = room('Cursor Other', a, users['admin_a'])
            room_b = room('Cursor Room B', b, users['p_b'])
            for k in ('p1', 'p2', 't1'):
                db.session.add(ChatRoomMember(room_id=room_a.id, user_id=users[k].id))
                db.session.add(ChatRoomMember(room_id=room_other.id, user_id=users[k].id))
            db.session.add(ChatRoomMember(room_id=room_b.id, user_id=users['p_b'].id))
            # Inconsistent data on purpose: a school-B parent holding a
            # membership row in a school-A room must still get 404.
            db.session.add(ChatRoomMember(room_id=room_a.id, user_id=users['foreign'].id))
            db.session.commit()

            self.ids = {k: u.id for k, u in users.items()}
            self.ids.update(school_a=a.id, school_b=b.id, room_a=room_a.id,
                            room_other=room_other.id, room_b=room_b.id)
            self.names = {k: u.username for k, u in users.items()}
            self.tokens = {k: encode_token(users[k]) for k in users}

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

    def _add(self, n, room_key='room_a', sender='p2', deleted=False, reverse_time=False):
        """Insert n messages (ids ascending). created_at ascends with id unless
        ``reverse_time`` — then it descends, to separate id order from time."""
        rows = []
        for i in range(n):
            self._clock += timedelta(seconds=1)
            rows.append(dict(room_id=self.ids[room_key], sender_user_id=self.ids[sender],
                             body=f'{room_key} m{i}', message_type='text',
                             is_deleted=deleted, created_at=self._clock,
                             updated_at=self._clock))
        if reverse_time:
            times = [r['created_at'] for r in rows][::-1]
            for r, t in zip(rows, times):
                r['created_at'] = r['updated_at'] = t
        with self.app.app_context():
            ids = []
            for r in rows:                       # one statement per row: ids ascend
                ids.append(db.session.execute(
                    ChatMessage.__table__.insert().returning(ChatMessage.__table__.c.id),
                    r).scalar())
            db.session.commit()
            return ids

    def _get(self, key='p1', room_key='room_a', **params):
        qs = '&'.join(f'{k}={v}' for k, v in params.items())
        url = f"/api/mobile/v1/chat/rooms/{self.ids[room_key]}/messages" + (f'?{qs}' if qs else '')
        headers = {'Authorization': f'Bearer {self.tokens[key]}'} if key else {}
        # Fresh client per call: a device holds one user's token and no other
        # user's session cookie (login_user() in jwt_required sets one).
        return self.app.test_client().get(url, headers=headers)

    def _ids(self, resp):
        self.assertEqual(resp.status_code, 200, resp.get_data(True))
        return [m['id'] for m in resp.get_json()['messages']]

    def _expected_item(self, msg_id, viewer):
        with self.app.app_context():
            m = db.session.get(ChatMessage, msg_id)
            return {
                'id': m.id, 'sender_id': m.sender_user_id,
                'sender_name': m.sender.full_name if m.sender else None,
                'sender_role': m.sender.role.name if m.sender and m.sender.role else None,
                'body': None if m.is_deleted else m.body, 'message_type': m.message_type,
                'attachment_url': None if m.is_deleted else m.attachment_url,
                'created_at': m.created_at.replace(tzinfo=timezone.utc).isoformat(),
                'is_mine': m.sender_user_id == self.ids[viewer], 'is_deleted': m.is_deleted,
            }

    def _room_read_count(self, room_key='room_a'):
        with self.app.app_context():
            return (ChatMessageRead.query
                    .join(ChatMessage, ChatMessage.id == ChatMessageRead.message_id)
                    .filter(ChatMessage.room_id == self.ids[room_key]).count())

    def _drain(self, cursor, key='p1', limit=None, max_pages=50):
        pages, seen = [], []
        for _ in range(max_pages):
            params = {'after_id': cursor}
            if limit is not None:
                params['limit'] = limit
            page = self._ids(self._get(key, **params))
            pages.append(page)
            seen.extend(page)
            if page:
                cursor = page[-1]
            if len(page) < (limit if limit is not None else 50):
                return pages, seen
        self.fail('backlog never drained')

    # ── 1, 18: no after_id — unchanged ───────────────────────────────────────

    def test_01_no_after_id_newest_page_unchanged(self):
        ids = self._add(40) + self._add(3, deleted=True) + self._add(17, sender='p1')
        resp = self._get('p1')
        data = resp.get_json()
        self.assertEqual(set(data), ENVELOPE_KEYS)
        self.assertEqual((data['ok'], data['room_id'], data['count']),
                         (True, self.ids['room_a'], 50))
        self.assertEqual(data['messages'], [self._expected_item(i, 'p1') for i in ids[-50:]])
        # before: id < before, newest page of that range; malformed before ignored
        self.assertEqual(self._ids(self._get('p1', before=ids[30])), ids[:30])
        self.assertEqual(self._ids(self._get('p1', before='abc')), ids[-50:])
        self.assertEqual(self._ids(self._get('p1', before='')), ids[-50:])

    def test_01b_no_after_id_still_orders_by_created_at(self):
        ids = self._add(5, reverse_time=True)            # newest created_at = lowest id
        self.assertEqual(self._ids(self._get('p1')), ids[::-1])

    def test_18_message_schema_identical_on_both_paths(self):
        ids = self._add(3) + self._add(1, deleted=True) + self._add(1, sender='p1')
        plain = self._get('p1').get_json()
        cursor = self._get('p1', after_id=0).get_json()
        self.assertEqual(set(cursor), ENVELOPE_KEYS)
        for item in plain['messages'] + cursor['messages']:
            self.assertEqual(set(item), MSG_KEYS)
        self.assertEqual(cursor['messages'], plain['messages'])   # same rows, same bytes
        self.assertEqual([m['id'] for m in cursor['messages']], ids)

    # ── 2, 3: only newer ids, ascending by id ────────────────────────────────

    def test_02_returns_only_ids_greater_than_cursor(self):
        ids = self._add(20)
        self.assertEqual(self._ids(self._get('p1', after_id=ids[9])), ids[10:])
        self.assertEqual(self._ids(self._get('p1', after_id=0)), ids)

    def test_03_after_id_orders_by_numeric_id_not_created_at(self):
        ids = self._add(12, reverse_time=True)
        got = self._ids(self._get('p1', after_id=ids[1]))
        self.assertEqual(got, ids[2:])
        self.assertEqual(got, sorted(got))
        self.assertEqual(len(set(got)), len(got))

    # ── 4: limit rules are the existing ones ─────────────────────────────────

    def test_04_limit_default_max_and_malformed(self):
        ids = self._add(130)
        self.assertEqual(self._ids(self._get('p1', after_id=0)), ids[:50])           # default
        self.assertEqual(self._ids(self._get('p1', after_id=0, limit=10)), ids[:10])
        self.assertEqual(self._ids(self._get('p1', after_id=0, limit=500)), ids[:100])  # max
        self.assertEqual(self._ids(self._get('p1', after_id=0, limit='abc')), ids[:50])
        self.assertEqual(self._ids(self._get('p1', after_id=0, limit=-5)), ids[:50])
        self.assertEqual(self._ids(self._get('p1', after_id=0, limit=0)), [])

    # ── 5: backlog larger than a page drains with no gap / duplicate ─────────

    def test_05_backlog_drains_without_gaps_or_duplicates(self):
        start = self._add(5)
        new = self._add(120)
        pages, seen = self._drain(start[-1])
        self.assertEqual([len(p) for p in pages], [50, 50, 20])
        self.assertEqual(pages[0], new[:50])
        self.assertEqual(seen, new)

    def test_05b_exact_multiple_of_limit_ends_with_empty_page(self):
        new = self._add(100)
        pages, seen = self._drain(0)
        self.assertEqual([len(p) for p in pages], [50, 50, 0])
        self.assertEqual(seen, new)

    # ── 6: id gaps (other rooms interleaved) do not matter ───────────────────

    def test_06_non_contiguous_ids(self):
        mine = []
        for _ in range(8):
            mine += self._add(3)
            self._add(4, room_key='room_other')
        self.assertNotEqual(mine, list(range(mine[0], mine[0] + len(mine))))  # real gaps
        pages, seen = self._drain(0, limit=5)
        self.assertEqual(seen, mine)
        gap_cursor = mine[2] + 1                      # an id of the interleaved room
        self.assertNotIn(gap_cursor, mine)
        self.assertEqual(self._ids(self._get('p1', after_id=gap_cursor)), mine[3:])

    # ── 7, 8: cursor at / beyond the latest id ───────────────────────────────

    def test_07_cursor_equal_to_latest_returns_nothing(self):
        ids = self._add(5)
        resp = self._get('p1', after_id=ids[-1])
        self.assertEqual(resp.get_json(), {'ok': True, 'room_id': self.ids['room_a'],
                                           'count': 0, 'messages': []})

    def test_08_cursor_beyond_latest_returns_nothing(self):
        ids = self._add(5)
        self.assertEqual(self._ids(self._get('p1', after_id=ids[-1] + 1000)), [])
        self.assertEqual(self._ids(self._get('p1', after_id=2 ** 63 - 1)), [])

    # ── 9, 10: deleted rows ──────────────────────────────────────────────────

    def test_09_deleted_newer_message_keeps_representation(self):
        [old] = self._add(1)
        [gone] = self._add(1, deleted=True)
        [item] = self._get('p1', after_id=old).get_json()['messages']
        self.assertEqual(item, self._expected_item(gone, 'p1'))
        self.assertEqual((item['is_deleted'], item['body'], item['attachment_url']),
                         (True, None, None))

    def test_10_message_deleted_below_cursor_is_not_redelivered(self):
        """Expected limitation: a soft-delete of an already-received message
        does not move it above the cursor. A full request without after_id
        (unchanged behaviour) still shows it as deleted."""
        ids = self._add(5)
        cursor = ids[-1]
        with self.app.app_context():
            db.session.get(ChatMessage, ids[2]).is_deleted = True
            db.session.commit()
        self.assertEqual(self._ids(self._get('p1', after_id=cursor)), [])
        full = {m['id']: m for m in self._get('p1').get_json()['messages']}
        self.assertTrue(full[ids[2]]['is_deleted'])

    # ── 11, 12: rejected parameters ──────────────────────────────────────────

    def test_11_before_with_after_id_rejected(self):
        ids = self._add(5)
        for before in (ids[3], 'abc'):
            resp = self._get('p1', before=before, after_id=ids[0])
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(resp.get_json(), {'ok': False, 'error': MIXED})

    def test_12_invalid_after_id_rejected(self):
        ids = self._add(3)
        for bad in ('', 'abc', '-1', '1.5', '%201', '%2B1', '1_0', '%D9%A3', '0x1',
                    str(2 ** 63)):
            with self.subTest(after_id=bad):
                resp = self._get('p1', after_id=bad)
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.get_json(), {'ok': False, 'error': BAD_CURSOR})
        self.assertEqual(self._ids(self._get('p1', after_id='0')), ids)
        self.assertEqual(self._ids(self._get('p1', after_id=f'00{ids[0]}')), ids[1:])

    # ── 13, 14, 15, 16: access control unchanged ─────────────────────────────

    def test_13_non_member_denied_before_any_cursor_handling(self):
        self._add(3)
        for params in ({}, {'after_id': 0}, {'after_id': 'bad'}):
            resp = self._get('outsider', **params)
            self.assertEqual(resp.status_code, 403)
            self.assertEqual(resp.get_json(), {'ok': False, 'error': NOT_MEMBER})

    def test_14_cross_school_isolation_unchanged(self):
        self._add(3, room_key='room_b', sender='p_b')
        for params in ({}, {'after_id': 0}):
            r = self._get('p1', room_key='room_b', **params)        # other school's room
            self.assertEqual((r.status_code, r.get_json()['error']), (403, NOT_MEMBER))
            r = self._get('foreign', **params)                       # stray cross-school row
            self.assertEqual((r.status_code, r.get_json()['error']), (404, NOT_FOUND))
        # a cursor can never reach another room's rows
        self.assertEqual(self._ids(self._get('p1', after_id=0)), [])

    def test_15_parent_and_teacher_both_supported_admin_role_refused(self):
        ids = self._add(4)
        self.assertEqual(self._ids(self._get('p1', after_id=ids[1])), ids[2:])
        self.assertEqual(self._ids(self._get('t1', after_id=ids[1])), ids[2:])
        self.assertEqual(self._ids(self._get('t1')), ids)
        resp = self._get('admin_a', after_id=0)
        self.assertEqual((resp.status_code, resp.get_json()), (403, {'ok': False,
                                                                     'error': 'forbidden'}))

    def test_16_unauthenticated_unchanged(self):
        for params in ({}, {'after_id': 0}):
            resp = self._get(None, **params)
            self.assertEqual((resp.status_code, resp.get_json()),
                             (401, {'ok': False, 'error': 'missing_token'}))
        resp = self.client.get(
            f"/api/mobile/v1/chat/rooms/{self.ids['room_a']}/messages?after_id=0",
            headers={'Authorization': 'Bearer not-a-token'})
        self.assertEqual((resp.status_code, resp.get_json()['error']), (401, 'invalid_token'))

    # ── 17: GET never writes read receipts ───────────────────────────────────

    def test_17_after_id_get_creates_no_read_receipts(self):
        ids = self._add(10)
        detail = f"/api/mobile/v1/chat/rooms/{self.ids['room_a']}"
        auth = {'Authorization': f"Bearer {self.tokens['p1']}"}
        self.assertEqual(self.client.get(detail, headers=auth).get_json()['unread_count'], 10)
        self._drain(0)
        self._get('p1', after_id=ids[4])
        self._get('p1')
        self.assertEqual(self._room_read_count(), 0)
        self.assertEqual(self.client.get(detail, headers=auth).get_json()['unread_count'], 10)

    # ── ordering safety: concurrent sends + after_id polling ─────────────────

    def _send(self, path, body, client, key=None):
        rid = self.ids['room_a']
        if path == 'mobile':
            return client.post(f'/api/mobile/v1/chat/rooms/{rid}/messages', json={'body': body},
                               headers={'Authorization': f'Bearer {self.tokens[key]}'})
        url = f'/chat/rooms/{rid}' if path == 'web_admin' else f'/chat/my-rooms/{rid}'
        return client.post(url, data={'body': body},
                           headers={'X-Requested-With': 'XMLHttpRequest'})

    def _sender(self, path):
        if path == 'mobile':
            return 'p1', self.app.test_client()
        key = 'admin_a' if path == 'web_admin' else 'p2'
        client = self.app.test_client()
        client.post('/auth/login', data={'username': self.names[key], 'password': PASSWORD})
        return key, client

    def test_ordering_poller_never_skips_a_late_committing_lower_id(self):
        """Send A (web) holds an allocated, uncommitted id; send B (mobile)
        must wait on the room lock, so an after_id poll in between sees
        nothing — never B's higher id ahead of A's lower one."""
        mock.patch('app.blueprints.chat._push_chat_message').start()
        mock.patch('app.blueprints.mobile_api.chat._push_new_message').start()
        self.addCleanup(mock.patch.stopall)
        [base] = self._add(1)
        (ka, ca), (kb, cb) = self._sender('web_admin'), self._sender('mobile')
        flushed, release = threading.Event(), threading.Event()

        def after_flush(session, _ctx):
            if (threading.current_thread().name == 'send-A' and not flushed.is_set()
                    and any(isinstance(o, ChatMessage) for o in session.new)):
                flushed.set()
                release.wait(WAIT)
        event.listen(Session, 'after_flush', after_flush)
        self.addCleanup(event.remove, Session, 'after_flush', after_flush)
        self.addCleanup(release.set)

        out = {}
        ta = threading.Thread(name='send-A', target=lambda: out.setdefault(
            'A', self._send('web_admin', 'A', ca, ka)))
        tb = threading.Thread(name='send-B', target=lambda: out.setdefault(
            'B', self._send('mobile', 'B', cb, kb)))
        with self.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as mon:
            try:
                ta.start()
                self.assertTrue(flushed.wait(WAIT), 'send A never flushed')
                tb.start()
                deadline, blocked = time.monotonic() + WAIT, False
                while time.monotonic() < deadline and tb.is_alive() and not blocked:
                    blocked = bool(mon.execute(text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                        "current_database() AND wait_event_type = 'Lock' AND query "
                        "ILIKE '%chat_rooms%FOR NO KEY UPDATE%'")).scalar())
                    time.sleep(0.02)
                self.assertTrue(blocked, 'send B was not held behind send A')
                self.assertEqual(self._ids(self._get('p2', after_id=base)), [],
                                 'poller saw a message while a lower id was uncommitted')
            finally:
                release.set()
                ta.join(WAIT)
                tb.join(WAIT)
        id_a, id_b = out['A'].get_json()['message']['id'], out['B'].get_json()['data']['id']
        self.assertLess(id_a, id_b)
        self.assertEqual(self._ids(self._get('p2', after_id=base)), [id_a, id_b])

    def test_ordering_concurrent_burst_with_live_after_id_poller(self):
        """9 concurrent sends over all three paths while a client polls with
        after_id: the poller's final sequence is every message exactly once,
        strictly ascending — nothing skipped behind its advancing cursor."""
        mock.patch('app.blueprints.chat._push_chat_message').start()
        mock.patch('app.blueprints.mobile_api.chat._push_new_message').start()
        self.addCleanup(mock.patch.stopall)
        senders = [(p, *self._sender(p)) for p in ('web_admin', 'web_user', 'mobile')
                   for _ in range(3)]
        barrier, stop = threading.Barrier(len(senders)), threading.Event()
        seen, codes, poll_errors = [], [], []
        poll_client = self.app.test_client()
        auth = {'Authorization': f"Bearer {self.tokens['t1']}"}
        url = f"/api/mobile/v1/chat/rooms/{self.ids['room_a']}/messages"

        def poll_once(cursor):
            resp = poll_client.get(f'{url}?after_id={cursor}&limit=2', headers=auth)
            if resp.status_code != 200:
                poll_errors.append(resp.status_code)
                return []
            return [m['id'] for m in resp.get_json()['messages']]

        def poller():
            cursor = 0
            while not stop.is_set():
                page = poll_once(cursor)
                seen.extend(page)
                cursor = page[-1] if page else cursor
            while True:                                   # final drain
                page = poll_once(cursor)
                seen.extend(page)
                if not page:
                    return
                cursor = page[-1]

        def send(i, path, key, client):
            barrier.wait(WAIT)
            codes.append(self._send(path, f'burst {i}', client, key).status_code)

        pt = threading.Thread(target=poller)
        pt.start()
        threads = [threading.Thread(target=send, args=(i, *s)) for i, s in enumerate(senders)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(WAIT * 2)
        stop.set()
        pt.join(WAIT * 2)

        self.assertEqual(poll_errors, [])
        self.assertEqual(sorted(codes), [200] * 6 + [201] * 3)
        with self.app.app_context():
            final = [m.id for m in ChatMessage.query.filter_by(room_id=self.ids['room_a'])
                     .order_by(ChatMessage.id).all()]
        self.assertEqual(len(final), 9)
        self.assertEqual(seen, final)          # each once, ascending, none skipped


if __name__ == '__main__':
    unittest.main()

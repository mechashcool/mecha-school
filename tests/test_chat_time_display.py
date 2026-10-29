"""
Web chat time display — stored naive UTC shown as Asia/Baghdad, 12-hour Arabic.

Storage is UTC (datetime.utcnow) and stays untouched. Every web path must show
the SAME string for the same message:

  server-rendered room page  (|ar_datetime12)
  AJAX send response         (_format_message_json)
  poll JSON                  (_poll_messages_json)
  SSE stream                 (_sse_generator)
  "load older" JSON          (room_older_messages)

The mobile API keeps its timezone-aware UTC ISO-8601 (+00:00) — unchanged.
Ordering stays on the raw columns, never on the display string.

Fixture (school A): admin A, parent P1 (member), room R with three legacy rows
inserted chronologically whose display strings sort the other way round:
  L1  2026-10-29 17:00 UTC → 29/10/2026 - 08:00 مساءً
  L2  2026-10-31 20:00 UTC → 31/10/2026 - 11:00 مساءً
  L3  2026-10-31 21:30 UTC → 01/11/2026 - 12:30 صباحًا   (crosses midnight)

Requires an isolated, approved test database (tests/conftest.py guard).
"""
import unittest
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (db, AuditLog, ChatMessage, ChatMessageRead, ChatRoom,
                        ChatRoomMember, Role, School, User)
from app.utils.time_display import (format_baghdad, format_datetime_ar12,
                                    format_time_ar12, to_baghdad)

PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True}

L1_UTC = datetime(2026, 10, 29, 17, 0)
L2_UTC = datetime(2026, 10, 31, 20, 0)
L3_UTC = datetime(2026, 10, 31, 21, 30)


class TimeFormatterTest(unittest.TestCase):
    """Pure formatter behaviour — the stored value is naive UTC."""

    def test_01_utc_1700_is_baghdad_0800_pm(self):
        self.assertEqual(to_baghdad(L1_UTC).strftime('%Y-%m-%d %H:%M'), '2026-10-29 20:00')
        self.assertEqual(format_time_ar12(L1_UTC), '08:00 مساءً')
        self.assertEqual(format_datetime_ar12(L1_UTC), '29/10/2026 - 08:00 مساءً')

    def test_02_morning_noon_midnight(self):
        self.assertEqual(format_time_ar12(datetime(2026, 10, 29, 2, 15)), '05:15 صباحًا')
        self.assertEqual(format_time_ar12(datetime(2026, 10, 29, 9, 30)), '12:30 مساءً')
        self.assertEqual(format_time_ar12(datetime(2026, 10, 28, 21, 15)), '12:15 صباحًا')
        self.assertEqual(format_time_ar12(datetime(2026, 10, 29, 17, 5)), '08:05 مساءً')

    def test_03_crossing_midnight_moves_the_date(self):
        self.assertEqual(format_datetime_ar12(L3_UTC), '01/11/2026 - 12:30 صباحًا')
        self.assertEqual(format_datetime_ar12(datetime(2026, 12, 31, 22, 0)),
                         '01/01/2027 - 01:00 صباحًا')

    def test_04_zoneinfo_and_no_double_conversion(self):
        local = to_baghdad(L1_UTC)
        self.assertEqual(local.tzinfo, ZoneInfo('Asia/Baghdad'))
        # Already-aware values are converted from their own offset, once.
        aware_utc = L1_UTC.replace(tzinfo=timezone.utc)
        aware_bgw = aware_utc.astimezone(ZoneInfo('Asia/Baghdad'))
        for value in (aware_utc, aware_bgw):
            self.assertEqual(format_time_ar12(value), '08:00 مساءً')

    def test_05_input_not_mutated_and_empty_values(self):
        original = datetime(2026, 10, 29, 17, 0)
        format_datetime_ar12(original)
        self.assertIsNone(original.tzinfo)
        self.assertEqual(original, datetime(2026, 10, 29, 17, 0))
        for fn in (format_time_ar12, format_datetime_ar12, format_baghdad):
            self.assertEqual(fn(None), '')
        self.assertIsNone(to_baghdad(None))

    def test_06_to_baghdad_filter_backward_compatible(self):
        app = create_app('testing')
        filt = app.jinja_env.filters['to_baghdad']
        self.assertEqual(filt(L1_UTC), '2026-10-29 20:00')
        self.assertEqual(filt(L1_UTC, '%H:%M'), '20:00')
        self.assertEqual(filt(None), '')
        self.assertIs(app.jinja_env.filters['ar_time12'], format_time_ar12)
        self.assertIs(app.jinja_env.filters['ar_datetime12'], format_datetime_ar12)


class ChatTimeDisplayTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['RATELIMIT_ENABLED'] = False
        with cls.app.app_context():
            cls.role_ids = {n: Role.query.filter_by(name=n).first().id
                            for n in ('school_admin', 'parent')}

    def setUp(self):
        self.sfx = uuid4().hex[:10]
        with self.app.app_context():
            school = School(school_name=f'CTD {self.sfx}', code=f'CT{self.sfx[:8]}',
                            capacity=0, is_active=True)
            db.session.add(school)
            db.session.flush()

            def user(name, role):
                u = User(username=f'{name}_{self.sfx}', email=f'{name}_{self.sfx}@t.test',
                         full_name=f'{name} {self.sfx}', role_id=self.role_ids[role],
                         school_id=school.id, is_active=True)
                u.set_password(PASSWORD)
                db.session.add(u)
                return u

            admin = user('ctdadmin', 'school_admin')
            parent = user('ctdparent', 'parent')
            db.session.flush()
            room = ChatRoom(school_id=school.id, name=f'CTD Room {self.sfx}', type='group',
                            scope='custom', created_by_user_id=admin.id)
            db.session.add(room)
            db.session.flush()
            db.session.add(ChatRoomMember(room_id=room.id, user_id=parent.id, role='member'))
            # Legacy rows, one insert each so ids follow chronological order.
            msg_ids = {}
            for label, ts in (('L1', L1_UTC), ('L2', L2_UTC), ('L3', L3_UTC)):
                msg_ids[label] = db.session.execute(
                    ChatMessage.__table__.insert().returning(ChatMessage.id),
                    dict(room_id=room.id, sender_user_id=admin.id, body=f'{label} {self.sfx}',
                         message_type='text', is_deleted=False,
                         created_at=ts, updated_at=ts)).scalar_one()
            db.session.commit()
            self.ids = dict(school=school.id, admin=admin.id, parent=parent.id,
                            room=room.id, **msg_ids)
            self.names = dict(admin=admin.username, parent=parent.username)

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            sid = self.ids['school']
            msg_ids = db.select(ChatMessage.id).where(ChatMessage.room_id == self.ids['room'])
            ChatMessageRead.query.filter(ChatMessageRead.message_id.in_(msg_ids)).delete(
                synchronize_session=False)
            ChatMessage.query.filter_by(room_id=self.ids['room']).delete(
                synchronize_session=False)
            ChatRoomMember.query.filter_by(room_id=self.ids['room']).delete(
                synchronize_session=False)
            ChatRoom.query.execution_options(**OPTS).filter_by(school_id=sid).delete(
                synchronize_session=False)
            uids = [self.ids['admin'], self.ids['parent']]
            AuditLog.query.execution_options(**OPTS).filter(
                AuditLog.user_id.in_(uids)).delete(synchronize_session=False)
            User.query.execution_options(**OPTS).filter(User.id.in_(uids)).delete(
                synchronize_session=False)
            School.query.filter_by(id=sid).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _client(self, key):
        client = self.app.test_client()
        resp = client.post('/auth/login', data={'username': self.names[key],
                                                'password': PASSWORD})
        self.assertIn(resp.status_code, (200, 302))
        return client

    def _rows(self):
        """{id: stored created_at} — read straight from the DB."""
        with self.app.app_context():
            return dict(db.session.query(ChatMessage.id, ChatMessage.created_at)
                        .filter(ChatMessage.room_id == self.ids['room']).all())

    def _expected(self):
        return {mid: format_datetime_ar12(ts) for mid, ts in self._rows().items()}

    def _send(self, client, url):
        resp = client.post(url, data={'body': f'new {self.sfx}'},
                           headers={'X-Requested-With': 'XMLHttpRequest'})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:300])
        return resp.get_json()['message']

    def _sse_messages(self, client, url):
        """Read the first SSE batch (all messages after_id=0), then disconnect."""
        resp = client.get(url, buffered=False)
        self.assertEqual(resp.status_code, 200)
        import json
        out = {}
        try:
            for chunk in resp.response:
                text = chunk.decode() if isinstance(chunk, bytes) else chunk
                if text.startswith('event: message'):
                    data = json.loads(text.split('data: ', 1)[1])
                    out[data['id']] = data['created_at']
                else:
                    break                # first ping / non-message ends the batch
        finally:
            resp.close()
        return out

    # ── tests ─────────────────────────────────────────────────────────────────

    def test_01_admin_room_every_path_shows_the_same_time(self):
        client = self._client('admin')
        rid = self.ids['room']
        sent = self._send(client, f'/chat/rooms/{rid}')
        expected = self._expected()
        new_id = sent['id']
        self.assertEqual(sent['created_at'], expected[new_id])     # live send

        page = client.get(f'/chat/rooms/{rid}').get_data(as_text=True)
        for mid, text in expected.items():                         # page load
            self.assertIn(text, page)
        self.assertIn('29/10/2026 - 08:00 مساءً', page)

        poll = client.get(f'/chat/rooms/{rid}/poll?after_id=0').get_json()['messages']
        self.assertEqual({m['id']: m['created_at'] for m in poll}, expected)

        older = client.get(f'/chat/rooms/{rid}/messages/older?before_id={new_id}').get_json()
        self.assertEqual({m['id']: m['created_at'] for m in older['messages']},
                         {k: v for k, v in expected.items() if k != new_id})

        sse = self._sse_messages(client, f'/chat/rooms/{rid}/events?after_id=0')
        self.assertEqual(sse, expected)

    def test_02_user_room_page_send_and_poll_match(self):
        client = self._client('parent')
        rid = self.ids['room']
        sent = self._send(client, f'/chat/my-rooms/{rid}')
        expected = self._expected()
        self.assertEqual(sent['created_at'], expected[sent['id']])

        page = client.get(f'/chat/my-rooms/{rid}').get_data(as_text=True)
        for text in expected.values():
            self.assertIn(text, page)

        poll = client.get(f'/chat/my-rooms/{rid}/poll?after_id=0').get_json()['messages']
        self.assertEqual({m['id']: m['created_at'] for m in poll}, expected)

        # Conversation list: last message as 12-hour Arabic time only.
        listing = client.get('/chat/my-rooms').get_data(as_text=True)
        latest = max(self._rows().values())
        self.assertIn(format_time_ar12(latest), listing)

    def test_03_old_and_new_rows_share_the_formatter(self):
        client = self._client('admin')
        sent = self._send(client, f'/chat/rooms/{self.ids["room"]}')
        rows = self._rows()
        self.assertEqual(sent['created_at'], format_datetime_ar12(rows[sent['id']]))
        self.assertEqual(format_datetime_ar12(rows[self.ids['L1']]),
                         '29/10/2026 - 08:00 مساءً')

    def test_04_ordering_follows_raw_timestamps_and_storage_is_unchanged(self):
        # Display strings sort 01/11 < 29/10 < 31/10; chronology is L1 < L2 < L3.
        client = self._client('admin')
        rid = self.ids['room']
        page = client.get(f'/chat/rooms/{rid}').get_data(as_text=True)
        pos = [page.index(f'{k} {self.sfx}') for k in ('L1', 'L2', 'L3')]
        self.assertEqual(pos, sorted(pos))

        poll = client.get(f'/chat/rooms/{rid}/poll?after_id=0').get_json()['messages']
        self.assertEqual([m['id'] for m in poll],
                         [self.ids['L1'], self.ids['L2'], self.ids['L3']])
        older = client.get(f'/chat/rooms/{rid}/messages/older'
                           f'?before_id={self.ids["L3"]}&limit=1').get_json()
        self.assertEqual([m['id'] for m in older['messages']], [self.ids['L2']])
        self.assertTrue(older['has_more'])

        # Nothing was rewritten: stored values are the original naive UTC.
        rows = self._rows()
        self.assertEqual(rows[self.ids['L1']], L1_UTC)
        self.assertEqual(rows[self.ids['L3']], L3_UTC)
        self.assertIsNone(rows[self.ids['L1']].tzinfo)

    def test_05_mobile_api_still_returns_utc_offset(self):
        with self.app.app_context():
            token = encode_token(db.session.get(User, self.ids['parent'],
                                                execution_options=OPTS))
        resp = self.app.test_client().get(
            f'/api/mobile/v1/chat/rooms/{self.ids["room"]}/messages',
            headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:300])
        got = {m['id']: m['created_at'] for m in resp.get_json()['messages']}
        self.assertEqual(got[self.ids['L1']], '2026-10-29T17:00:00+00:00')
        self.assertEqual(got[self.ids['L3']], '2026-10-31T21:30:00+00:00')


if __name__ == '__main__':
    unittest.main()

"""
Tests for school admin/manager chat send permissions.

Covers:
  - Admin can send in a normal group room (auto-added as member)
  - Admin can send in announcement-only group (admin override)
  - Admin can send in no-replies group (admin override)
  - Admin cannot send in another school's room (403)
  - Admin is auto-added as ChatRoomMember on first send
  - Closed room blocks even admin
  - Parent/teacher restrictions still apply (announcement_only, no_replies)
  - direct_chat creates a private room between admin and target user
  - direct_chat reuses an existing private room
  - direct_chat rejects cross-school target (404)
  - FCM not tested here (requires live FCM; see integration tests)
"""
import unittest
from uuid import uuid4

from app import create_app
from app.models import (
    db, Role, School, User, AuditLog,
    ChatRoom, ChatRoomMember, ChatMessage,
)


def _uid():
    return uuid4().hex[:10]


class ChatAdminTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('development')
        with cls.app.app_context():
            cls.school_admin_role = Role.query.filter_by(name='school_admin').first()
            cls.parent_role       = Role.query.filter_by(name='parent').first()
            cls.teacher_role      = Role.query.filter_by(name='teacher').first()
            assert cls.school_admin_role, 'school_admin role must exist'
            assert cls.parent_role,       'parent role must exist'

    def setUp(self):
        self.sfx = _uid()
        self.client = self.app.test_client()

        with self.app.app_context():
            school_a = School(
                school_name=f'Chat Admin School A {self.sfx}',
                code=f'CHA{self.sfx[:7]}',
                capacity=0, is_active=True,
            )
            school_b = School(
                school_name=f'Chat Admin School B {self.sfx}',
                code=f'CHB{self.sfx[:7]}',
                capacity=0, is_active=True,
            )
            db.session.add_all([school_a, school_b])
            db.session.flush()

            admin_a = User(
                username=f'cadmin_a_{self.sfx}',
                email=f'cadmin_a_{self.sfx}@test.test',
                full_name=f'Chat Admin A {self.sfx}',
                role_id=self.school_admin_role.id,
                school_id=school_a.id,
                is_active=True,
            )
            parent_a = User(
                username=f'cparent_a_{self.sfx}',
                email=f'cparent_a_{self.sfx}@test.test',
                full_name=f'Chat Parent A {self.sfx}',
                role_id=self.parent_role.id,
                school_id=school_a.id,
                is_active=True,
            )
            for u in [admin_a, parent_a]:
                u.set_password('Test1234!')
                db.session.add(u)
            db.session.flush()

            # Normal group room (school A)
            room_normal = ChatRoom(
                school_id=school_a.id,
                name=f'Normal Room {self.sfx}',
                type='group', scope='custom',
                created_by_user_id=admin_a.id,
                is_announcement_only=False,
                allow_replies=True,
            )
            # Announcement-only room (school A)
            room_ann = ChatRoom(
                school_id=school_a.id,
                name=f'Ann Room {self.sfx}',
                type='announcement', scope='school',
                created_by_user_id=admin_a.id,
                is_announcement_only=True,
                allow_replies=True,
            )
            # No-replies room (school A)
            room_noreply = ChatRoom(
                school_id=school_a.id,
                name=f'NoReply Room {self.sfx}',
                type='group', scope='custom',
                created_by_user_id=admin_a.id,
                is_announcement_only=False,
                allow_replies=False,
            )
            # Closed room (school A)
            room_closed = ChatRoom(
                school_id=school_a.id,
                name=f'Closed Room {self.sfx}',
                type='group', scope='custom',
                created_by_user_id=admin_a.id,
                is_announcement_only=False,
                allow_replies=True,
                is_closed=True,
            )
            # Room in school B (different school)
            room_b = ChatRoom(
                school_id=school_b.id,
                name=f'School B Room {self.sfx}',
                type='group', scope='school',
                created_by_user_id=None,
                is_announcement_only=False,
                allow_replies=True,
            )
            for r in [room_normal, room_ann, room_noreply, room_closed, room_b]:
                db.session.add(r)
            db.session.flush()

            # Add parent_a as a member of ann and noreply rooms
            db.session.add(ChatRoomMember(
                room_id=room_ann.id, user_id=parent_a.id, role='member'))
            db.session.add(ChatRoomMember(
                room_id=room_noreply.id, user_id=parent_a.id, role='member'))

            db.session.commit()

            self.ids = {
                'school_a_id':    school_a.id,
                'school_b_id':    school_b.id,
                'admin_a_id':     admin_a.id,
                'admin_a_user':   admin_a.username,
                'parent_a_id':    parent_a.id,
                'parent_a_user':  parent_a.username,
                'room_normal_id': room_normal.id,
                'room_ann_id':    room_ann.id,
                'room_noreply_id':room_noreply.id,
                'room_closed_id': room_closed.id,
                'room_b_id':      room_b.id,
            }

    def tearDown(self):
        with self.app.app_context():
            sid_a = self.ids['school_a_id']
            sid_b = self.ids['school_b_id']
            # ChatMessage has no school_id — delete via room_id
            room_ids = [
                r.id for r in
                ChatRoom.query.execution_options(bypass_tenant_scope=True)
                .filter(ChatRoom.school_id.in_([sid_a, sid_b])).all()
            ]
            if room_ids:
                ChatMessage.query.filter(
                    ChatMessage.room_id.in_(room_ids)
                ).delete(synchronize_session=False)
                ChatRoomMember.query.filter(
                    ChatRoomMember.room_id.in_(room_ids)
                ).delete(synchronize_session=False)
            ChatRoom.query.execution_options(bypass_tenant_scope=True).filter(
                ChatRoom.school_id.in_([sid_a, sid_b])
            ).delete(synchronize_session=False)
            user_ids = [
                u.id for u in
                User.query.filter(User.school_id.in_([sid_a, sid_b])).all()
            ]
            if user_ids:
                AuditLog.query.execution_options(bypass_tenant_scope=True).filter(
                    AuditLog.user_id.in_(user_ids)
                ).delete(synchronize_session=False)
            User.query.filter(User.school_id.in_([sid_a, sid_b])).delete(
                synchronize_session=False)
            School.query.filter(School.id.in_([sid_a, sid_b])).delete(
                synchronize_session=False)
            db.session.commit()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _login(self, username):
        return self.client.post('/auth/login', data={
            'username': username, 'password': 'Test1234!',
        }, follow_redirects=True)

    def _logout(self):
        self.client.get('/auth/logout', follow_redirects=True)

    def _send_via_room_detail(self, room_id, body='Hello admin'):
        """POST to /chat/rooms/<id> (admin send form)."""
        return self.client.post(
            f'/chat/rooms/{room_id}',
            data={'body': body},
            follow_redirects=False,
        )

    def _send_via_user_room(self, room_id, body='Hello user'):
        """POST to /chat/my-rooms/<id> (user send form)."""
        return self.client.post(
            f'/chat/my-rooms/{room_id}',
            data={'body': body},
            follow_redirects=False,
        )

    def _member_exists(self, room_id, user_id):
        with self.app.app_context():
            return ChatRoomMember.query.filter_by(
                room_id=room_id, user_id=user_id
            ).first() is not None

    def _message_count(self, room_id):
        with self.app.app_context():
            return ChatMessage.query.filter_by(
                room_id=room_id, is_deleted=False
            ).count()

    # ── Test: admin can send in a normal group ────────────────────────────────

    def test_admin_send_normal_group(self):
        """Admin sends in a normal group room via /chat/rooms/<id>."""
        self._login(self.ids['admin_a_user'])
        before = self._message_count(self.ids['room_normal_id'])

        resp = self._send_via_room_detail(self.ids['room_normal_id'], 'Admin normal msg')
        self.assertEqual(resp.status_code, 302)  # redirect after success

        after = self._message_count(self.ids['room_normal_id'])
        self.assertEqual(after, before + 1)
        self._logout()

    def test_admin_auto_added_as_member(self):
        """Admin is auto-added to ChatRoomMember when accessing a room."""
        self._login(self.ids['admin_a_user'])
        # Admin was not manually added — send creates membership.
        self._send_via_room_detail(self.ids['room_normal_id'], 'auto-member test')
        self.assertTrue(
            self._member_exists(self.ids['room_normal_id'], self.ids['admin_a_id']),
            'Admin must be auto-added as ChatRoomMember',
        )
        self._logout()

    # ── Test: admin can send in announcement-only room ───────────────────────

    def test_admin_send_announcement_only(self):
        """Admin bypasses announcement-only restriction."""
        self._login(self.ids['admin_a_user'])
        before = self._message_count(self.ids['room_ann_id'])

        resp = self._send_via_room_detail(self.ids['room_ann_id'], 'Official announcement')
        self.assertEqual(resp.status_code, 302)

        after = self._message_count(self.ids['room_ann_id'])
        self.assertEqual(after, before + 1)
        self._logout()

    # ── Test: admin can send in no-replies room ───────────────────────────────

    def test_admin_send_no_replies(self):
        """Admin bypasses allow_replies=False restriction."""
        self._login(self.ids['admin_a_user'])
        before = self._message_count(self.ids['room_noreply_id'])

        resp = self._send_via_room_detail(self.ids['room_noreply_id'], 'Admin override msg')
        self.assertEqual(resp.status_code, 302)

        after = self._message_count(self.ids['room_noreply_id'])
        self.assertEqual(after, before + 1)
        self._logout()

    # ── Test: closed room blocks even admin ───────────────────────────────────

    def test_admin_blocked_by_closed_room(self):
        """Admin cannot send into a closed room."""
        self._login(self.ids['admin_a_user'])
        before = self._message_count(self.ids['room_closed_id'])

        resp = self._send_via_room_detail(self.ids['room_closed_id'], 'Closed attempt')
        # Should redirect (flash warning) — not create a message.
        self.assertEqual(resp.status_code, 302)

        after = self._message_count(self.ids['room_closed_id'])
        self.assertEqual(after, before, 'No message must be created in a closed room')
        self._logout()

    # ── Test: admin cannot access another school's room ───────────────────────

    def test_admin_cannot_access_other_school_room(self):
        """Admin from school A cannot access room in school B."""
        self._login(self.ids['admin_a_user'])
        resp = self.client.get(
            f'/chat/rooms/{self.ids["room_b_id"]}',
            follow_redirects=False,
        )
        self.assertIn(resp.status_code, (403, 404))
        self._logout()

    # ── Test: parent blocked by announcement-only (no admin override) ─────────

    def test_parent_blocked_announcement_only(self):
        """Regular parent cannot send in announcement-only room."""
        self._login(self.ids['parent_a_user'])
        before = self._message_count(self.ids['room_ann_id'])

        resp = self._send_via_user_room(self.ids['room_ann_id'], 'Parent trying ann room')
        self.assertEqual(resp.status_code, 302)

        after = self._message_count(self.ids['room_ann_id'])
        self.assertEqual(after, before, 'Parent must not send in announcement-only room')
        self._logout()

    # ── Test: parent blocked by no_replies ────────────────────────────────────

    def test_parent_blocked_no_replies(self):
        """Regular parent cannot send in no-replies room."""
        self._login(self.ids['parent_a_user'])
        before = self._message_count(self.ids['room_noreply_id'])

        resp = self._send_via_user_room(self.ids['room_noreply_id'], 'Parent no-reply')
        self.assertEqual(resp.status_code, 302)

        after = self._message_count(self.ids['room_noreply_id'])
        self.assertEqual(after, before, 'Parent must not send in no-replies room')
        self._logout()

    # ── Test: direct_chat creates private room ────────────────────────────────

    def test_direct_chat_creates_room(self):
        """direct_chat creates a new private room and redirects to it."""
        self._login(self.ids['admin_a_user'])
        resp = self.client.get(
            f'/chat/direct/{self.ids["parent_a_id"]}',
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        location = resp.headers.get('Location', '')
        self.assertIn('/chat/rooms/', location)

        with self.app.app_context():
            admin_mems  = {m.room_id for m in ChatRoomMember.query.filter_by(
                user_id=self.ids['admin_a_id']).all()}
            parent_mems = {m.room_id for m in ChatRoomMember.query.filter_by(
                user_id=self.ids['parent_a_id']).all()}
            shared = admin_mems & parent_mems
            self.assertTrue(len(shared) >= 1, 'A shared private room must exist')
        self._logout()

    def test_direct_chat_reuses_existing_room(self):
        """direct_chat called twice returns the same room (no duplicates)."""
        self._login(self.ids['admin_a_user'])

        self.client.get(
            f'/chat/direct/{self.ids["parent_a_id"]}',
            follow_redirects=False,
        )
        r2 = self.client.get(
            f'/chat/direct/{self.ids["parent_a_id"]}',
            follow_redirects=False,
        )
        loc1 = r2.headers.get('Location', '')

        with self.app.app_context():
            admin_mems  = {m.room_id for m in ChatRoomMember.query.filter_by(
                user_id=self.ids['admin_a_id']).all()}
            parent_mems = {m.room_id for m in ChatRoomMember.query.filter_by(
                user_id=self.ids['parent_a_id']).all()}
            private_shared = [
                r for r in (admin_mems & parent_mems)
                if ChatRoom.query.execution_options(bypass_tenant_scope=True)
                .filter_by(id=r, type='private').first()
            ]
            self.assertEqual(len(private_shared), 1,
                             'Exactly one private room must exist — no duplicates')
        self._logout()

    def test_direct_chat_cross_school_rejected(self):
        """direct_chat with a user from another school returns 404."""
        self._login(self.ids['admin_a_user'])

        # Create a user in school B.
        with self.app.app_context():
            user_b = User(
                username=f'cross_{self.sfx}',
                email=f'cross_{self.sfx}@test.test',
                full_name=f'Cross User {self.sfx}',
                role_id=self.parent_role.id,
                school_id=self.ids['school_b_id'],
                is_active=True,
            )
            user_b.set_password('Test1234!')
            db.session.add(user_b)
            db.session.commit()
            user_b_id = user_b.id

        resp = self.client.get(
            f'/chat/direct/{user_b_id}',
            follow_redirects=False,
        )
        self.assertIn(resp.status_code, (403, 404))
        self._logout()

    # ── Test: direct_new user-picker page ────────────────────────────────

    def test_direct_new_shows_same_school_users(self):
        """GET /chat/direct lists only same-school users."""
        self._login(self.ids['admin_a_user'])
        resp = self.client.get('/chat/direct', follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        # parent_a from school A must appear
        self.assertIn(f'Chat Parent A {self.sfx}', html)
        self._logout()

    def test_direct_new_excludes_other_school_users(self):
        """GET /chat/direct must not expose users from other schools."""
        with self.app.app_context():
            user_b = User(
                username=f'picker_b_{self.sfx}',
                email=f'picker_b_{self.sfx}@test.test',
                full_name=f'Picker School B {self.sfx}',
                role_id=self.parent_role.id,
                school_id=self.ids['school_b_id'],
                is_active=True,
            )
            user_b.set_password('Test1234!')
            db.session.add(user_b)
            db.session.commit()

        self._login(self.ids['admin_a_user'])
        resp = self.client.get('/chat/direct', follow_redirects=False)
        html = resp.data.decode()
        self.assertNotIn(f'Picker School B {self.sfx}', html)
        self._logout()

    def test_direct_new_search_filter(self):
        """GET /chat/direct?q=<name> filters by name."""
        self._login(self.ids['admin_a_user'])
        # Search for a unique fragment of parent_a's name
        resp = self.client.get(
            f'/chat/direct?q=Chat+Parent+A+{self.sfx}',
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn(f'Chat Parent A {self.sfx}', html)
        self._logout()

    def test_direct_new_role_filter_parent(self):
        """GET /chat/direct?role=parent returns only parents."""
        self._login(self.ids['admin_a_user'])
        resp = self.client.get('/chat/direct?role=parent', follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn(f'Chat Parent A {self.sfx}', html)
        # admin_a himself must not appear (excluded as current_user)
        self.assertNotIn(f'Chat Admin A {self.sfx}', html)
        self._logout()

    def test_direct_new_excludes_self(self):
        """GET /chat/direct must not include the current admin in the list."""
        self._login(self.ids['admin_a_user'])
        resp = self.client.get('/chat/direct', follow_redirects=False)
        html = resp.data.decode()
        # admin_a should not appear as a selectable user (they are the logged-in user)
        # We verify by checking the direct link is not present for their own ID
        self.assertNotIn(
            f'/chat/direct/{self.ids["admin_a_id"]}',
            html,
        )
        self._logout()

    def test_direct_new_private_room_visible_on_index(self):
        """After creating a private room via direct_chat, index shows it in private section."""
        self._login(self.ids['admin_a_user'])
        # Create a private room
        self.client.get(
            f'/chat/direct/{self.ids["parent_a_id"]}',
            follow_redirects=False,
        )
        # Index page should show the room under المحادثات الخاصة
        resp = self.client.get('/chat/', follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn('محادثة مع', html)
        self.assertIn(f'Chat Parent A {self.sfx}', html)
        self._logout()

    # ── Test: AJAX send in normal room ────────────────────────────────────

    def test_ajax_send_normal_group(self):
        """Admin sends via AJAX and gets JSON response."""
        self._login(self.ids['admin_a_user'])
        before = self._message_count(self.ids['room_normal_id'])

        resp = self.client.post(
            f'/chat/rooms/{self.ids["room_normal_id"]}',
            data={'body': 'AJAX test message'},
            headers={'X-Requested-With': 'XMLHttpRequest'},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['ok'])
        self.assertIn('message', data)
        self.assertEqual(data['message']['body'], 'AJAX test message')
        self.assertTrue(data['message']['is_self'])

        after = self._message_count(self.ids['room_normal_id'])
        self.assertEqual(after, before + 1)
        self._logout()

    def test_ajax_closed_room_error(self):
        """AJAX send to closed room returns error JSON."""
        self._login(self.ids['admin_a_user'])

        resp = self.client.post(
            f'/chat/rooms/{self.ids["room_closed_id"]}',
            data={'body': 'Should not send'},
            headers={'X-Requested-With': 'XMLHttpRequest'},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data['ok'])
        self.assertIn('error', data)
        self._logout()


    # ── Test: add_member page ─────────────────────────────────────────────

    def test_add_member_page_loads_for_group(self):
        """Admin can open the add-member picker for a group room."""
        self._login(self.ids['admin_a_user'])
        resp = self.client.get(
            f'/chat/rooms/{self.ids["room_normal_id"]}/members/add',
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn('إضافة عضو', html)
        self._logout()

    def test_add_member_page_blocked_for_private_room(self):
        """add_member returns 403 for private rooms."""
        self._login(self.ids['admin_a_user'])

        # Create a private room first.
        with self.app.app_context():
            priv = ChatRoom(
                school_id=self.ids['school_a_id'],
                name=f'Private {self.sfx}',
                type='private', scope='custom',
                created_by_user_id=self.ids['admin_a_id'],
            )
            db.session.add(priv)
            db.session.flush()
            db.session.add(ChatRoomMember(
                room_id=priv.id, user_id=self.ids['admin_a_id'], role='owner'))
            db.session.add(ChatRoomMember(
                room_id=priv.id, user_id=self.ids['parent_a_id'], role='member'))
            db.session.commit()
            priv_id = priv.id

        resp = self.client.get(
            f'/chat/rooms/{priv_id}/members/add',
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 403)
        self._logout()

    def test_add_member_adds_same_school_user(self):
        """POST to add_member adds a same-school user to the room."""
        self._login(self.ids['admin_a_user'])

        before_count = self._member_count(self.ids['room_normal_id'])

        resp = self.client.post(
            f'/chat/rooms/{self.ids["room_normal_id"]}/members/add',
            data={'user_id': self.ids['parent_a_id']},
            follow_redirects=False,
        )
        # Should redirect back to room detail on success.
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f'/chat/rooms/{self.ids["room_normal_id"]}',
                      resp.headers.get('Location', ''))

        after_count = self._member_count(self.ids['room_normal_id'])
        self.assertEqual(after_count, before_count + 1)
        self._logout()

    def test_add_member_duplicate_rejected(self):
        """Adding the same user twice does not create a duplicate membership."""
        self._login(self.ids['admin_a_user'])

        # Add parent once.
        self.client.post(
            f'/chat/rooms/{self.ids["room_normal_id"]}/members/add',
            data={'user_id': self.ids['parent_a_id']},
            follow_redirects=False,
        )
        count_after_first = self._member_count(self.ids['room_normal_id'])

        # Add same parent again.
        self.client.post(
            f'/chat/rooms/{self.ids["room_normal_id"]}/members/add',
            data={'user_id': self.ids['parent_a_id']},
            follow_redirects=False,
        )
        count_after_second = self._member_count(self.ids['room_normal_id'])

        self.assertEqual(count_after_first, count_after_second,
                         'Duplicate membership must not be created')
        self._logout()

    def test_add_member_cross_school_rejected(self):
        """Cannot add a user from another school to a room."""
        with self.app.app_context():
            user_b = User(
                username=f'add_b_{self.sfx}',
                email=f'add_b_{self.sfx}@test.test',
                full_name=f'Add School B {self.sfx}',
                role_id=self.parent_role.id,
                school_id=self.ids['school_b_id'],
                is_active=True,
            )
            user_b.set_password('Test1234!')
            db.session.add(user_b)
            db.session.commit()
            user_b_id = user_b.id

        self._login(self.ids['admin_a_user'])
        before_count = self._member_count(self.ids['room_normal_id'])

        self.client.post(
            f'/chat/rooms/{self.ids["room_normal_id"]}/members/add',
            data={'user_id': user_b_id},
            follow_redirects=False,
        )
        after_count = self._member_count(self.ids['room_normal_id'])
        self.assertEqual(before_count, after_count,
                         'Cross-school user must not be added')
        self._logout()

    def test_add_member_blocked_for_parent(self):
        """A parent cannot access the add-member page (admin_required)."""
        self._login(self.ids['parent_a_user'])
        resp = self.client.get(
            f'/chat/rooms/{self.ids["room_normal_id"]}/members/add',
            follow_redirects=False,
        )
        self.assertIn(resp.status_code, (302, 403))
        self._logout()

    def test_add_member_picker_excludes_existing_members(self):
        """The add-member picker must not list users already in the room."""
        self._login(self.ids['admin_a_user'])

        # Manually add parent_a to room_normal first.
        with self.app.app_context():
            if not ChatRoomMember.query.filter_by(
                room_id=self.ids['room_normal_id'],
                user_id=self.ids['parent_a_id'],
            ).first():
                db.session.add(ChatRoomMember(
                    room_id=self.ids['room_normal_id'],
                    user_id=self.ids['parent_a_id'],
                    role='member',
                ))
                db.session.commit()

        resp = self.client.get(
            f'/chat/rooms/{self.ids["room_normal_id"]}/members/add',
            follow_redirects=False,
        )
        html = resp.data.decode()
        # parent_a is already a member — their add button must NOT appear
        # (their name might appear elsewhere but the form with their user_id must be absent)
        self.assertNotIn(
            f'name="user_id" value="{self.ids["parent_a_id"]}"',
            html,
        )
        self._logout()

    # ── Test: create_room custom scope drops forged cross-school members ──────

    def test_create_room_custom_rejects_cross_school_member(self):
        """create_room (custom scope) must drop forged cross-school member_ids.

        The custom member picker only lists same-school users, but the POST
        handler must not trust the submitted ids.  The _sync_members chokepoint
        validates every candidate against the room's school, so a forged id from
        another school can never become a member.
        """
        with self.app.app_context():
            user_b = User(
                username=f'inj_b_{self.sfx}',
                email=f'inj_b_{self.sfx}@test.test',
                full_name=f'Inject School B {self.sfx}',
                role_id=self.parent_role.id,
                school_id=self.ids['school_b_id'],
                is_active=True,
            )
            user_b.set_password('Test1234!')
            db.session.add(user_b)
            db.session.commit()
            user_b_id = user_b.id

        self._login(self.ids['admin_a_user'])
        resp = self.client.post(
            '/chat/rooms/create',
            data={
                'name':  f'Custom Room {self.sfx}',
                'type':  'group',
                'scope': 'custom',
                'member_ids': [str(self.ids['parent_a_id']), str(user_b_id)],
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302)
        location = resp.headers.get('Location', '')
        self.assertIn('/chat/rooms/', location)
        new_room_id = int(location.rstrip('/').split('/')[-1])

        with self.app.app_context():
            self.assertIsNotNone(
                ChatRoomMember.query.filter_by(
                    room_id=new_room_id, user_id=self.ids['parent_a_id']).first(),
                'Same-school member must be added',
            )
            self.assertIsNone(
                ChatRoomMember.query.filter_by(
                    room_id=new_room_id, user_id=user_b_id).first(),
                'Cross-school member must NOT be added via forged member_ids',
            )
        self._logout()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _member_count(self, room_id):
        with self.app.app_context():
            return ChatRoomMember.query.filter_by(room_id=room_id).count()


if __name__ == '__main__':
    unittest.main()

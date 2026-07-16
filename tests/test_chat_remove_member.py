"""
Tests for the group-chat "Remove from Group" (kick) action.

Covers:
  - Admin removes a normal member: membership row deleted, messages and
    read receipts intact, other rooms' memberships untouched
  - Removed member immediately loses access to the room (user_room 403,
    room absent from /chat/my-rooms)
  - Removed member can be re-added later via add_member
  - Removing a room-level moderator ('admin' role) deletes the role with
    the membership row
  - Room owner cannot be removed
  - Acting admin cannot remove themselves
  - Admin from school A cannot remove a member of a school-B room (404)
  - A parent (non-admin) cannot access the endpoint even directly
  - Private rooms are rejected (403)
  - Removing a non-member returns 404
"""
import unittest
from uuid import uuid4

from app import create_app
from app.models import (
    db, Role, School, User, AuditLog,
    ChatRoom, ChatRoomMember, ChatMessage, ChatMessageRead,
)


def _uid():
    return uuid4().hex[:10]


class ChatRemoveMemberTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('development')
        with cls.app.app_context():
            cls.school_admin_role = Role.query.filter_by(name='school_admin').first()
            cls.parent_role       = Role.query.filter_by(name='parent').first()
            assert cls.school_admin_role, 'school_admin role must exist'
            assert cls.parent_role,       'parent role must exist'

    def setUp(self):
        self.sfx = _uid()
        self.client = self.app.test_client()

        with self.app.app_context():
            school_a = School(
                school_name=f'Kick School A {self.sfx}',
                code=f'KKA{self.sfx[:7]}',
                capacity=0, is_active=True,
            )
            school_b = School(
                school_name=f'Kick School B {self.sfx}',
                code=f'KKB{self.sfx[:7]}',
                capacity=0, is_active=True,
            )
            db.session.add_all([school_a, school_b])
            db.session.flush()

            admin_a = User(
                username=f'kadmin_a_{self.sfx}',
                email=f'kadmin_a_{self.sfx}@test.test',
                full_name=f'Kick Admin A {self.sfx}',
                role_id=self.school_admin_role.id,
                school_id=school_a.id,
                is_active=True,
            )
            parent_a = User(
                username=f'kparent_a_{self.sfx}',
                email=f'kparent_a_{self.sfx}@test.test',
                full_name=f'Kick Parent A {self.sfx}',
                role_id=self.parent_role.id,
                school_id=school_a.id,
                is_active=True,
            )
            moderator_a = User(
                username=f'kmod_a_{self.sfx}',
                email=f'kmod_a_{self.sfx}@test.test',
                full_name=f'Kick Moderator A {self.sfx}',
                role_id=self.parent_role.id,
                school_id=school_a.id,
                is_active=True,
            )
            parent_b = User(
                username=f'kparent_b_{self.sfx}',
                email=f'kparent_b_{self.sfx}@test.test',
                full_name=f'Kick Parent B {self.sfx}',
                role_id=self.parent_role.id,
                school_id=school_b.id,
                is_active=True,
            )
            for u in [admin_a, parent_a, moderator_a, parent_b]:
                u.set_password('Test1234!')
                db.session.add(u)
            db.session.flush()

            # Group room (school A) — admin_a owner, parent_a member,
            # moderator_a room-level admin.
            room_a = ChatRoom(
                school_id=school_a.id,
                name=f'Kick Room A {self.sfx}',
                type='group', scope='custom',
                created_by_user_id=admin_a.id,
            )
            # Second group room (school A) — parent_a also member here; this
            # membership must survive removal from room_a.
            room_a2 = ChatRoom(
                school_id=school_a.id,
                name=f'Kick Room A2 {self.sfx}',
                type='group', scope='custom',
                created_by_user_id=admin_a.id,
            )
            # Private room (school A)
            room_priv = ChatRoom(
                school_id=school_a.id,
                name=f'Kick Private {self.sfx}',
                type='private', scope='custom',
                created_by_user_id=admin_a.id,
            )
            # Group room in school B
            room_b = ChatRoom(
                school_id=school_b.id,
                name=f'Kick Room B {self.sfx}',
                type='group', scope='custom',
                created_by_user_id=None,
            )
            db.session.add_all([room_a, room_a2, room_priv, room_b])
            db.session.flush()

            db.session.add_all([
                ChatRoomMember(room_id=room_a.id,  user_id=admin_a.id,     role='owner'),
                ChatRoomMember(room_id=room_a.id,  user_id=parent_a.id,    role='member'),
                ChatRoomMember(room_id=room_a.id,  user_id=moderator_a.id, role='admin'),
                ChatRoomMember(room_id=room_a2.id, user_id=parent_a.id,    role='member'),
                ChatRoomMember(room_id=room_priv.id, user_id=admin_a.id,   role='owner'),
                ChatRoomMember(room_id=room_priv.id, user_id=parent_a.id,  role='member'),
                ChatRoomMember(room_id=room_b.id,  user_id=parent_b.id,    role='member'),
            ])
            db.session.flush()

            # Historical message from parent_a in room_a + a read receipt —
            # both must survive the member's removal.
            msg = ChatMessage(
                room_id=room_a.id,
                sender_user_id=parent_a.id,
                body='historical message',
                message_type='text',
            )
            db.session.add(msg)
            db.session.flush()
            db.session.add(ChatMessageRead(message_id=msg.id, user_id=admin_a.id))
            db.session.commit()

            self.ids = {
                'school_a_id':    school_a.id,
                'school_b_id':    school_b.id,
                'admin_a_id':     admin_a.id,
                'admin_a_user':   admin_a.username,
                'parent_a_id':    parent_a.id,
                'parent_a_user':  parent_a.username,
                'moderator_a_id': moderator_a.id,
                'parent_b_id':    parent_b.id,
                'room_a_id':      room_a.id,
                'room_a2_id':     room_a2.id,
                'room_priv_id':   room_priv.id,
                'room_b_id':      room_b.id,
                'msg_id':         msg.id,
            }

    def tearDown(self):
        with self.app.app_context():
            sid_a = self.ids['school_a_id']
            sid_b = self.ids['school_b_id']
            room_ids = [
                r.id for r in
                ChatRoom.query.execution_options(bypass_tenant_scope=True)
                .filter(ChatRoom.school_id.in_([sid_a, sid_b])).all()
            ]
            if room_ids:
                msg_ids_sq = db.session.query(ChatMessage.id).filter(
                    ChatMessage.room_id.in_(room_ids))
                ChatMessageRead.query.filter(
                    ChatMessageRead.message_id.in_(msg_ids_sq)
                ).delete(synchronize_session=False)
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

    def _remove(self, room_id, user_id, data=None):
        return self.client.post(
            f'/chat/rooms/{room_id}/members/{user_id}/remove',
            data=data or {},
            follow_redirects=False,
        )

    def _member_exists(self, room_id, user_id):
        with self.app.app_context():
            return ChatRoomMember.query.filter_by(
                room_id=room_id, user_id=user_id
            ).first() is not None

    # ── Successful removal ────────────────────────────────────────────────────

    def test_admin_removes_member(self):
        """Membership deleted; messages, receipts, and other rooms intact."""
        self._login(self.ids['admin_a_user'])
        resp = self._remove(self.ids['room_a_id'], self.ids['parent_a_id'])
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f'/chat/rooms/{self.ids["room_a_id"]}',
                      resp.headers.get('Location', ''))

        self.assertFalse(
            self._member_exists(self.ids['room_a_id'], self.ids['parent_a_id']),
            'Membership row must be deleted')

        with self.app.app_context():
            # Historical message survives with correct sender.
            msg = ChatMessage.query.get(self.ids['msg_id'])
            self.assertIsNotNone(msg, 'Historical message must remain')
            self.assertEqual(msg.sender_user_id, self.ids['parent_a_id'])
            self.assertFalse(msg.is_deleted)
            # Read receipt survives.
            self.assertIsNotNone(
                ChatMessageRead.query.filter_by(
                    message_id=self.ids['msg_id'],
                    user_id=self.ids['admin_a_id']).first(),
                'Read receipts must remain')
            # User account untouched.
            u = User.query.execution_options(bypass_tenant_scope=True).get(
                self.ids['parent_a_id'])
            self.assertIsNotNone(u)
            self.assertTrue(u.is_active)

        # Membership in the other room is unaffected.
        self.assertTrue(
            self._member_exists(self.ids['room_a2_id'], self.ids['parent_a_id']),
            'Other rooms\' memberships must not be affected')
        # Other members of the same room unaffected.
        self.assertTrue(
            self._member_exists(self.ids['room_a_id'], self.ids['moderator_a_id']))
        self._logout()

    def test_removed_member_loses_access(self):
        """After removal the member gets 403 on the room and it vanishes
        from their room list."""
        self._login(self.ids['admin_a_user'])
        self._remove(self.ids['room_a_id'], self.ids['parent_a_id'])
        self._logout()

        self._login(self.ids['parent_a_user'])
        resp = self.client.get(
            f'/chat/my-rooms/{self.ids["room_a_id"]}', follow_redirects=False)
        self.assertEqual(resp.status_code, 403)

        resp = self.client.get('/chat/my-rooms', follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertNotIn(f'Kick Room A {self.sfx}', html)
        # Still sees the room they remain a member of.
        self.assertIn(f'Kick Room A2 {self.sfx}', html)
        self._logout()

    def test_removed_member_can_be_readded(self):
        """The removed user can be added again via add_member."""
        self._login(self.ids['admin_a_user'])
        self._remove(self.ids['room_a_id'], self.ids['parent_a_id'])
        self.assertFalse(
            self._member_exists(self.ids['room_a_id'], self.ids['parent_a_id']))

        self.client.post(
            f'/chat/rooms/{self.ids["room_a_id"]}/members/add',
            data={'user_id': self.ids['parent_a_id']},
            follow_redirects=False,
        )
        self.assertTrue(
            self._member_exists(self.ids['room_a_id'], self.ids['parent_a_id']),
            'Removed member must be re-addable')
        self._logout()

    def test_remove_moderator_deletes_role(self):
        """Removing a room-level 'admin' member removes the moderator
        privilege together with the membership row."""
        self._login(self.ids['admin_a_user'])
        resp = self._remove(self.ids['room_a_id'], self.ids['moderator_a_id'])
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(
            self._member_exists(self.ids['room_a_id'], self.ids['moderator_a_id']))
        self._logout()

    def test_remove_redirects_to_edit_page_when_requested(self):
        """next=edit redirects back to the edit page (allow-listed)."""
        self._login(self.ids['admin_a_user'])
        resp = self._remove(self.ids['room_a_id'], self.ids['parent_a_id'],
                            data={'next': 'edit'})
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f'/chat/rooms/{self.ids["room_a_id"]}/edit',
                      resp.headers.get('Location', ''))
        self._logout()

    # ── Guards ────────────────────────────────────────────────────────────────

    def test_owner_cannot_be_removed(self):
        """The room owner's membership must survive a removal attempt."""
        self._login(self.ids['admin_a_user'])
        resp = self._remove(self.ids['room_a_id'], self.ids['admin_a_id'])
        # Redirect with warning flash — no deletion (self + owner guard).
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            self._member_exists(self.ids['room_a_id'], self.ids['admin_a_id']),
            'Owner membership must not be deleted')
        self._logout()

    def test_owner_cannot_be_removed_by_other_admin(self):
        """A second school admin also cannot remove the owner."""
        with self.app.app_context():
            admin2 = User(
                username=f'kadmin2_{self.sfx}',
                email=f'kadmin2_{self.sfx}@test.test',
                full_name=f'Kick Admin2 {self.sfx}',
                role_id=self.school_admin_role.id,
                school_id=self.ids['school_a_id'],
                is_active=True,
            )
            admin2.set_password('Test1234!')
            db.session.add(admin2)
            db.session.commit()
            admin2_user = admin2.username

        self._login(admin2_user)
        resp = self._remove(self.ids['room_a_id'], self.ids['admin_a_id'])
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            self._member_exists(self.ids['room_a_id'], self.ids['admin_a_id']),
            'Owner membership must not be deletable by any admin')
        self._logout()

    def test_private_room_rejected(self):
        """Removal is for groups only — private rooms return 403."""
        self._login(self.ids['admin_a_user'])
        resp = self._remove(self.ids['room_priv_id'], self.ids['parent_a_id'])
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(
            self._member_exists(self.ids['room_priv_id'], self.ids['parent_a_id']))
        self._logout()

    def test_remove_nonmember_404(self):
        """Removing a user who is not a member of the room returns 404."""
        self._login(self.ids['admin_a_user'])
        # moderator_a is not a member of room_a2
        resp = self._remove(self.ids['room_a2_id'], self.ids['moderator_a_id'])
        self.assertEqual(resp.status_code, 404)
        self._logout()

    # ── Isolation / authorization ─────────────────────────────────────────────

    def test_cross_school_admin_rejected(self):
        """Admin from school A cannot remove a member of a school-B room."""
        self._login(self.ids['admin_a_user'])
        resp = self._remove(self.ids['room_b_id'], self.ids['parent_b_id'])
        self.assertIn(resp.status_code, (403, 404))
        self.assertTrue(
            self._member_exists(self.ids['room_b_id'], self.ids['parent_b_id']),
            'Cross-school membership must not be deleted')
        self._logout()

    def test_parent_cannot_access_endpoint(self):
        """A normal member (parent) cannot kick others even with a direct
        POST to the endpoint."""
        self._login(self.ids['parent_a_user'])
        resp = self._remove(self.ids['room_a_id'], self.ids['moderator_a_id'])
        # admin_required redirects non-admins away.
        self.assertIn(resp.status_code, (302, 403))
        if resp.status_code == 302:
            self.assertNotIn('/chat/rooms/', resp.headers.get('Location', ''))
        self.assertTrue(
            self._member_exists(self.ids['room_a_id'], self.ids['moderator_a_id']),
            'Non-admin must not be able to remove members')
        self._logout()

    def test_unauthenticated_rejected(self):
        """Anonymous POST is redirected to login; membership intact."""
        resp = self._remove(self.ids['room_a_id'], self.ids['parent_a_id'])
        self.assertIn(resp.status_code, (302, 401))
        self.assertTrue(
            self._member_exists(self.ids['room_a_id'], self.ids['parent_a_id']))

    def test_remove_button_visible_only_for_eligible_members(self):
        """Room page shows the kick form for members/moderators but not for
        the owner or the acting admin themselves."""
        self._login(self.ids['admin_a_user'])
        resp = self.client.get(
            f'/chat/rooms/{self.ids["room_a_id"]}', follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        base = f'/chat/rooms/{self.ids["room_a_id"]}/members'
        self.assertIn(f'{base}/{self.ids["parent_a_id"]}/remove', html)
        self.assertIn(f'{base}/{self.ids["moderator_a_id"]}/remove', html)
        self.assertNotIn(f'{base}/{self.ids["admin_a_id"]}/remove', html,
                         'Owner/self must have no remove button')
        self._logout()


if __name__ == '__main__':
    unittest.main()

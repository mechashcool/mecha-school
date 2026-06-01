"""
Tests for School Board mobile API endpoints.

Covers:
  - Parent and teacher can fetch videos/announcements for their school
  - Audience filtering: parents-only → parent only, teachers-only → teacher only, all → both
  - School isolation: user cannot read another school's content
  - Inactive content not returned
  - Expired content not returned
  - Future publish_at content not returned
  - Mark-as-read works per user (idempotent)
  - /school/board returns combined data
  - Empty response is clean
  - No auth → 401
"""
import unittest
from datetime import datetime, timedelta
from uuid import uuid4

from app import create_app
from app.models import (
    db, Role, School, User,
    SchoolVideo, SchoolAnnouncement, SchoolContentRead,
)


class SchoolBoardTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('development')

    def setUp(self):
        self.sfx = uuid4().hex[:10]
        self.ids = {}

        with self.app.app_context():
            parent_role  = Role.query.filter_by(name='parent').first()
            teacher_role = Role.query.filter_by(name='teacher').first()
            self.assertIsNotNone(parent_role,  'parent role must exist')
            self.assertIsNotNone(teacher_role, 'teacher role must exist')

            school_a = School(
                school_name=f'Board School A {self.sfx}',
                code=f'BSA{self.sfx[:7]}',
                capacity=0, is_active=True,
            )
            school_b = School(
                school_name=f'Board School B {self.sfx}',
                code=f'BSB{self.sfx[:7]}',
                capacity=0, is_active=True,
            )
            db.session.add_all([school_a, school_b])
            db.session.flush()

            parent_a = User(
                username=f'board_pa_{self.sfx}',
                email=f'board_pa_{self.sfx}@test.test',
                full_name=f'Board Parent A {self.sfx}',
                role_id=parent_role.id,
                school_id=school_a.id,
                is_active=True,
            )
            parent_b = User(
                username=f'board_pb_{self.sfx}',
                email=f'board_pb_{self.sfx}@test.test',
                full_name=f'Board Parent B {self.sfx}',
                role_id=parent_role.id,
                school_id=school_b.id,
                is_active=True,
            )
            teacher_a = User(
                username=f'board_ta_{self.sfx}',
                email=f'board_ta_{self.sfx}@test.test',
                full_name=f'Board Teacher A {self.sfx}',
                role_id=teacher_role.id,
                school_id=school_a.id,
                is_active=True,
            )
            for u in [parent_a, parent_b, teacher_a]:
                u.set_password('Password123')
            db.session.add_all([parent_a, parent_b, teacher_a])
            db.session.flush()

            db.session.commit()
            self.ids = {
                'school_a_id': school_a.id,
                'school_b_id': school_b.id,
                'parent_a_id': parent_a.id,
                'parent_b_id': parent_b.id,
                'teacher_a_id': teacher_a.id,
            }

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            ids = self.ids
            school_ids = [ids.get('school_a_id'), ids.get('school_b_id')]

            for model in [SchoolContentRead, SchoolVideo, SchoolAnnouncement]:
                (model.query
                 .execution_options(bypass_tenant_scope=True)
                 .filter(model.school_id.in_(school_ids))
                 .delete(synchronize_session=False))
            db.session.flush()

            for uid in [ids.get('parent_a_id'), ids.get('parent_b_id'),
                        ids.get('teacher_a_id')]:
                u = db.session.get(User, uid,
                                   execution_options={'bypass_tenant_scope': True})
                if u:
                    db.session.delete(u)
            db.session.flush()

            for sid in school_ids:
                s = db.session.get(School, sid,
                                   execution_options={'bypass_tenant_scope': True})
                if s:
                    db.session.delete(s)
            db.session.commit()
            db.session.remove()

    # ── JWT helpers ───────────────────────────────────────────────────────────

    def _token_for(self, user_id):
        with self.app.app_context():
            from app.blueprints.mobile_api.utils import encode_token
            user = db.session.get(
                User, user_id,
                execution_options={'bypass_tenant_scope': True},
            )
            return encode_token(user)

    def _api(self, method, path, token, **kwargs):
        client = self.app.test_client()
        fn = getattr(client, method)
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        return fn(
            f'/api/mobile/v1{path}',
            headers=headers,
            content_type='application/json',
            **kwargs,
        )

    # ── fixture helpers ───────────────────────────────────────────────────────

    def _make_video(self, school_id, audience='all', is_active=True,
                    is_featured=False, publish_at=None, expires_at=None, title=None):
        with self.app.app_context():
            v = SchoolVideo(
                school_id=school_id,
                title=title or f'Test Video {uuid4().hex[:6]}',
                video_url='https://example.com/video.mp4',
                audience=audience,
                is_active=is_active,
                is_featured=is_featured,
                publish_at=publish_at,
                expires_at=expires_at,
            )
            db.session.add(v)
            db.session.commit()
            return v.id

    def _make_announcement(self, school_id, audience='all', is_active=True,
                           is_featured=False, publish_at=None, expires_at=None, title=None):
        with self.app.app_context():
            a = SchoolAnnouncement(
                school_id=school_id,
                title=title or f'Test Announcement {uuid4().hex[:6]}',
                body='Test body content',
                media_type='none',
                audience=audience,
                is_active=is_active,
                is_featured=is_featured,
                publish_at=publish_at,
                expires_at=expires_at,
            )
            db.session.add(a)
            db.session.commit()
            return a.id

    # ── tests ─────────────────────────────────────────────────────────────────

    def test_parent_can_fetch_videos(self):
        ids   = self.ids
        vid   = self._make_video(ids['school_a_id'], audience='all')
        token = self._token_for(ids['parent_a_id'])

        resp = self._api('get', '/school/videos', token)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['ok'])
        self.assertIn('videos', data)
        self.assertIn('total', data)
        video_ids = [v['id'] for v in data['videos']]
        self.assertIn(vid, video_ids)

    def test_teacher_can_fetch_videos(self):
        ids   = self.ids
        vid   = self._make_video(ids['school_a_id'], audience='all')
        token = self._token_for(ids['teacher_a_id'])

        resp = self._api('get', '/school/videos', token)
        self.assertEqual(resp.status_code, 200)
        video_ids = [v['id'] for v in resp.get_json()['videos']]
        self.assertIn(vid, video_ids)

    def test_parent_can_fetch_announcements(self):
        ids   = self.ids
        ann   = self._make_announcement(ids['school_a_id'], audience='all')
        token = self._token_for(ids['parent_a_id'])

        resp = self._api('get', '/school/announcements', token)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['ok'])
        ann_ids = [a['id'] for a in data['announcements']]
        self.assertIn(ann, ann_ids)

    def test_teacher_can_fetch_announcements(self):
        ids   = self.ids
        ann   = self._make_announcement(ids['school_a_id'], audience='all')
        token = self._token_for(ids['teacher_a_id'])

        resp = self._api('get', '/school/announcements', token)
        self.assertEqual(resp.status_code, 200)
        ann_ids = [a['id'] for a in resp.get_json()['announcements']]
        self.assertIn(ann, ann_ids)

    def test_audience_parents_visible_to_parent_only(self):
        ids   = self.ids
        vid   = self._make_video(ids['school_a_id'], audience='parents')
        token_parent  = self._token_for(ids['parent_a_id'])
        token_teacher = self._token_for(ids['teacher_a_id'])

        resp_parent  = self._api('get', '/school/videos', token_parent)
        resp_teacher = self._api('get', '/school/videos', token_teacher)

        parent_ids  = [v['id'] for v in resp_parent.get_json()['videos']]
        teacher_ids = [v['id'] for v in resp_teacher.get_json()['videos']]

        self.assertIn(vid, parent_ids,   'parents-only video must appear to parent')
        self.assertNotIn(vid, teacher_ids, 'parents-only video must not appear to teacher')

    def test_audience_teachers_visible_to_teacher_only(self):
        ids   = self.ids
        vid   = self._make_video(ids['school_a_id'], audience='teachers')
        token_parent  = self._token_for(ids['parent_a_id'])
        token_teacher = self._token_for(ids['teacher_a_id'])

        parent_ids  = [v['id'] for v in
                       self._api('get', '/school/videos', token_parent).get_json()['videos']]
        teacher_ids = [v['id'] for v in
                       self._api('get', '/school/videos', token_teacher).get_json()['videos']]

        self.assertNotIn(vid, parent_ids,  'teachers-only video must not appear to parent')
        self.assertIn(vid, teacher_ids,    'teachers-only video must appear to teacher')

    def test_school_isolation_parent_cannot_see_other_school_content(self):
        ids   = self.ids
        vid_b = self._make_video(ids['school_b_id'], audience='all')
        token = self._token_for(ids['parent_a_id'])

        resp = self._api('get', '/school/videos', token)
        ids_returned = [v['id'] for v in resp.get_json()['videos']]
        self.assertNotIn(vid_b, ids_returned, 'parent_a must not see school_b video')

        resp2 = self._api('get', f'/school/videos/{vid_b}', token)
        self.assertEqual(resp2.status_code, 404)

    def test_inactive_content_not_returned(self):
        ids   = self.ids
        vid   = self._make_video(ids['school_a_id'], is_active=False)
        token = self._token_for(ids['parent_a_id'])

        ids_returned = [v['id'] for v in
                        self._api('get', '/school/videos', token).get_json()['videos']]
        self.assertNotIn(vid, ids_returned)

    def test_expired_content_not_returned(self):
        ids   = self.ids
        past  = datetime.utcnow() - timedelta(hours=1)
        vid   = self._make_video(ids['school_a_id'], expires_at=past)
        token = self._token_for(ids['parent_a_id'])

        ids_returned = [v['id'] for v in
                        self._api('get', '/school/videos', token).get_json()['videos']]
        self.assertNotIn(vid, ids_returned)

    def test_future_publish_at_content_not_returned(self):
        ids   = self.ids
        future = datetime.utcnow() + timedelta(hours=2)
        vid    = self._make_video(ids['school_a_id'], publish_at=future)
        token  = self._token_for(ids['parent_a_id'])

        ids_returned = [v['id'] for v in
                        self._api('get', '/school/videos', token).get_json()['videos']]
        self.assertNotIn(vid, ids_returned)

    def test_mark_read_works_per_user_and_is_idempotent(self):
        ids   = self.ids
        vid   = self._make_video(ids['school_a_id'])
        token = self._token_for(ids['parent_a_id'])

        # not read initially
        resp = self._api('get', '/school/videos', token)
        videos = {v['id']: v for v in resp.get_json()['videos']}
        self.assertFalse(videos[vid]['is_read'])

        # mark read
        r = self._api('post', f'/school/videos/{vid}/read', token)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['message'], 'video_marked_read')

        # now is_read = True
        resp2 = self._api('get', '/school/videos', token)
        videos2 = {v['id']: v for v in resp2.get_json()['videos']}
        self.assertTrue(videos2[vid]['is_read'])

        # second mark is idempotent
        r2 = self._api('post', f'/school/videos/{vid}/read', token)
        self.assertEqual(r2.status_code, 200)

        # teacher marking does not affect parent's read state
        token_t = self._token_for(ids['teacher_a_id'])
        self._api('post', f'/school/videos/{vid}/read', token_t)

        # parent's read state still True, teacher's should also be true now
        resp3 = self._api('get', '/school/videos', token)
        videos3 = {v['id']: v for v in resp3.get_json()['videos']}
        self.assertTrue(videos3[vid]['is_read'])

    def test_school_board_combined_endpoint(self):
        ids   = self.ids
        vid   = self._make_video(ids['school_a_id'], is_featured=True)
        ann   = self._make_announcement(ids['school_a_id'], is_featured=True)
        token = self._token_for(ids['parent_a_id'])

        resp = self._api('get', '/school/board', token)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['ok'])
        self.assertIn('featured_video',        data)
        self.assertIn('featured_announcement', data)
        self.assertIn('videos',                data)
        self.assertIn('announcements',          data)
        self.assertIsNotNone(data['featured_video'])
        self.assertIsNotNone(data['featured_announcement'])
        self.assertEqual(data['featured_video']['id'],        vid)
        self.assertEqual(data['featured_announcement']['id'], ann)

    def test_empty_response_is_clean(self):
        """School with no content returns zero counts and empty arrays."""
        ids   = self.ids
        # school_b has no content — use parent_b
        token = self._token_for(ids['parent_b_id'])

        v_resp = self._api('get', '/school/videos', token)
        self.assertEqual(v_resp.status_code, 200)
        vdata = v_resp.get_json()
        self.assertEqual(vdata['total'], 0)
        self.assertEqual(vdata['videos'], [])

        a_resp = self._api('get', '/school/announcements', token)
        self.assertEqual(a_resp.status_code, 200)
        adata = a_resp.get_json()
        self.assertEqual(adata['total'], 0)
        self.assertEqual(adata['announcements'], [])

    def test_no_auth_returns_401(self):
        resp = self._api('get', '/school/videos', token=None)
        self.assertEqual(resp.status_code, 401)

        resp2 = self._api('get', '/school/announcements', token=None)
        self.assertEqual(resp2.status_code, 401)

        resp3 = self._api('get', '/school/board', token=None)
        self.assertEqual(resp3.status_code, 401)

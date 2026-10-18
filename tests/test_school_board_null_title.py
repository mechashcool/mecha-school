"""
School Board media: a NULL title (SchoolVideo.title is nullable) renders as an
empty value in the admin pages and can never round-trip into the literal
string "None".

Proves:
  * admin list with title=NULL shows an empty title cell, never "None";
  * edit form with title=NULL shows an empty title input;
  * re-submitting the edit form exactly as rendered stores "" — not "None";
  * a real title ("اعلان مدرسي") is rendered and kept unchanged;
  * create without a title still stores "" (unchanged behaviour);
  * the mobile serializer still returns the raw title (null / "" / text) with
    the same field set — no API contract change, no invented fallback.

Storage and pushes are replaced by mocks: no network, no files.
"""
import io
import re
import unittest
from unittest import mock
from uuid import uuid4

from PIL import Image

from app import create_app
from app.models import (
    db, Role, School, User, AuditLog,
    SchoolVideo, SchoolAnnouncement, SchoolContentRead,
)
from app.blueprints.mobile_api.utils import encode_token

PASSWORD = 'Test1234!'
REAL_TITLE = 'اعلان مدرسي'
FAKE_STORAGE_URL = 'https://storage.test/school-media/obj'

VIDEO_KEYS = {'id', 'title', 'description', 'media_type', 'media_url', 'video_url',
              'thumbnail_url', 'audience', 'is_featured', 'is_published', 'is_read',
              'school_id', 'publish_at', 'expires_at', 'created_at'}

TITLE_INPUT_RE = re.compile(r'<input type="text" name="title" class="form-control"\s+'
                            r'value="([^"]*)">')
TITLE_CELL_RE = re.compile(r'<div class="fw-600">(.*?)</div>', re.S)


def _png():
    buf = io.BytesIO()
    Image.new('RGB', (64, 48), (30, 120, 200)).save(buf, 'PNG')
    return buf.getvalue()


class SchoolBoardNullTitleTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        with cls.app.app_context():
            cls.roles = {n: Role.query.filter_by(name=n).first()
                         for n in ('school_admin', 'parent')}
            assert all(cls.roles.values()), cls.roles

    def setUp(self):
        self.sfx = uuid4().hex[:10]
        mock.patch('app.utils.helpers._supabase_upload',
                   return_value=FAKE_STORAGE_URL).start()
        self.push = mock.patch('app.blueprints.admin._push_school_board').start()
        self.addCleanup(mock.patch.stopall)
        with self.app.app_context():
            school = School(school_name=f'NullTitle School {self.sfx}',
                            code=f'NT{self.sfx[:8]}', capacity=0, is_active=True)
            db.session.add(school)
            db.session.flush()

            def user(name, role):
                u = User(username=f'{name}_{self.sfx}', email=f'{name}_{self.sfx}@test.test',
                         full_name=f'{name} {self.sfx}', role_id=self.roles[role].id,
                         school_id=school.id, is_active=True)
                u.set_password(PASSWORD)
                db.session.add(u)
                return u

            admin = user('ntadmin', 'school_admin')
            parent = user('ntparent', 'parent')
            db.session.flush()

            def row(title, media_type, url):
                v = SchoolVideo(school_id=school.id, title=title, media_type=media_type,
                                video_url=url, audience='all', is_active=True,
                                created_by=admin.id)
                db.session.add(v)
                return v

            null_image = row(None, 'image', 'https://cdn.example.test/null.png')
            null_video = row(None, 'video', 'https://cdn.example.test/null.mp4')
            empty_image = row('', 'image', 'https://cdn.example.test/empty.png')
            titled_image = row(REAL_TITLE, 'image', 'https://cdn.example.test/titled.png')
            db.session.commit()
            self.ids = dict(school=school.id, admin=admin.id, parent=parent.id,
                            null_image=null_image.id, null_video=null_video.id,
                            empty_image=empty_image.id, titled_image=titled_image.id)
            self.admin_name = admin.username
            self.token = encode_token(parent)

    def tearDown(self):
        with self.app.app_context():
            sid = self.ids['school']
            for model in (SchoolContentRead, SchoolVideo, SchoolAnnouncement):
                model.query.execution_options(bypass_tenant_scope=True).filter(
                    model.school_id == sid).delete(synchronize_session=False)
            uids = [self.ids['admin'], self.ids['parent']]
            AuditLog.query.execution_options(bypass_tenant_scope=True).filter(
                AuditLog.user_id.in_(uids)).delete(synchronize_session=False)
            User.query.execution_options(bypass_tenant_scope=True).filter(
                User.id.in_(uids)).delete(synchronize_session=False)
            School.query.filter(School.id == sid).delete(synchronize_session=False)
            db.session.commit()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _admin(self):
        client = self.app.test_client()
        resp = client.post('/auth/login', data={'username': self.admin_name,
                                                'password': PASSWORD})
        self.assertIn(resp.status_code, (200, 302))
        return client

    def _title(self, key):
        with self.app.app_context():
            return db.session.get(SchoolVideo, self.ids[key],
                                  execution_options={'bypass_tenant_scope': True}).title

    def _edit_form_title(self, client, key):
        resp = client.get(f'/admin/school-board/videos/{self.ids[key]}/edit')
        self.assertEqual(resp.status_code, 200)
        m = TITLE_INPUT_RE.search(resp.get_data(as_text=True))
        self.assertIsNotNone(m, 'title input not found')
        return m.group(1)

    def _post_edit(self, client, key, title, media_type='image'):
        return client.post(f'/admin/school-board/videos/{self.ids[key]}/edit',
                           data={'title': title, 'description': '', 'audience': 'all',
                                 'media_type': media_type, 'is_active': 'on'},
                           content_type='multipart/form-data')

    def _mobile_videos(self):
        resp = self.app.test_client().get(
            '/api/mobile/v1/school/videos',
            headers={'Authorization': f'Bearer {self.token}'})
        self.assertEqual(resp.status_code, 200)
        return {v['id']: v for v in resp.get_json()['videos']}

    # ── 1. admin list ─────────────────────────────────────────────────────────

    def test_admin_list_null_title_renders_empty_not_none(self):
        html = self._admin().get('/admin/school-board/videos').get_data(as_text=True)
        cells = [c.strip() for c in TITLE_CELL_RE.findall(html)]
        self.assertEqual(len(cells), 4, cells)
        self.assertNotIn('None', cells)
        self.assertEqual(cells.count(''), 3)          # 2 NULL + 1 empty-string row
        self.assertIn(REAL_TITLE, cells)
        self.assertNotIn('>None<', html)

    # ── 2. edit form ──────────────────────────────────────────────────────────

    def test_edit_form_null_title_input_is_empty(self):
        client = self._admin()
        for key in ('null_image', 'null_video'):
            with self.subTest(row=key):
                self.assertEqual(self._edit_form_title(client, key), '')
                html = client.get(f'/admin/school-board/videos/{self.ids[key]}/edit'
                                  ).get_data(as_text=True)
                self.assertNotIn('alt="None"', html)
                self.assertNotIn('value="None"', html)

    # ── 3. NULL round-trip ────────────────────────────────────────────────────

    def test_resubmitting_null_title_form_stores_empty_never_none(self):
        client = self._admin()
        for key, media_type in (('null_image', 'image'), ('null_video', 'video')):
            with self.subTest(row=key):
                rendered = self._edit_form_title(client, key)
                resp = self._post_edit(client, key, rendered, media_type)
                self.assertEqual(resp.status_code, 302)
                self.assertEqual(self._title(key), '')
                self.assertNotEqual(self._title(key), 'None')

    # ── 4. real title unchanged ───────────────────────────────────────────────

    def test_real_title_rendered_and_kept_unchanged(self):
        client = self._admin()
        self.assertEqual(self._edit_form_title(client, 'titled_image'), REAL_TITLE)
        resp = self._post_edit(client, 'titled_image',
                               self._edit_form_title(client, 'titled_image'))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._title('titled_image'), REAL_TITLE)

    def test_create_without_title_still_stores_empty_string(self):
        client = self._admin()
        resp = client.post('/admin/school-board/videos/create',
                           data={'title': '   ', 'audience': 'all', 'media_type': 'image',
                                 'is_active': 'on',
                                 'media_file': (io.BytesIO(_png()), 'p.png', 'image/png')},
                           content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 302)
        with self.app.app_context():
            new = (SchoolVideo.query.execution_options(bypass_tenant_scope=True)
                   .filter_by(school_id=self.ids['school'])
                   .order_by(SchoolVideo.id.desc()).first())
            self.assertNotIn(new.id, self.ids.values())
            self.assertEqual(new.title, '')

    # ── 5 + 6. mobile serializer / contract unchanged ─────────────────────────

    def test_mobile_returns_raw_title_and_same_contract(self):
        videos = self._mobile_videos()
        self.assertIsNone(videos[self.ids['null_image']]['title'])
        self.assertIsNone(videos[self.ids['null_video']]['title'])
        self.assertEqual(videos[self.ids['empty_image']]['title'], '')
        self.assertEqual(videos[self.ids['titled_image']]['title'], REAL_TITLE)
        for v in videos.values():
            self.assertEqual(set(v), VIDEO_KEYS)
            self.assertNotIn('اعلاني', str(v['title']))
            self.assertNotIn('إعلاني', str(v['title']))


if __name__ == '__main__':
    unittest.main()

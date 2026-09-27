"""
School Board: new VIDEO content is disabled; everything else is unchanged.

Write paths (the only two that can create video content):
  POST /admin/school-board/videos/create        (manage_school_board)
  POST /admin/school-board/videos/<id>/edit     (manage_school_board)

Proves:
  * mp4 / mov / webm uploads, a missing/invalid media_type (defaults to video),
    an external video link, and a video renamed as .jpg/.png are all refused
    with "رفع الفيديو غير متاح حالياً." — before save_uploaded_file, before any
    Storage call, before any DB row, before any push;
  * on edit, a new video file, a new video link, or turning an image post into
    a video is refused; editing an existing video's details still works;
  * image posts (create + edit) still work (now optimised to WebP),
    announcements and the generic upload helper for documents are unchanged;
  * existing videos: admin list, toggle, delete, and the mobile list/detail/
    featured responses are unchanged (exact serialization);
  * unauthenticated, wrong-permission and cross-school callers get the same
    result as before, never the new message.

Storage and pushes are replaced by recording mocks: no network, no files.
"""
import io
import unittest
from datetime import datetime
from unittest import mock
from uuid import uuid4

from PIL import Image

from app import create_app
from app.models import (
    db, Role, School, User, AuditLog,
    SchoolVideo, SchoolAnnouncement, SchoolContentRead,
)
from app.blueprints.mobile_api.utils import encode_token
from app.utils import helpers
from app.utils.board_images import MSG_INVALID

PASSWORD = 'Test1234!'
DISABLED = 'رفع الفيديو غير متاح حالياً.'
FAKE_STORAGE_URL = 'https://storage.test/school-media/obj'

MP4 = b'\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom' + b'\x00' * 64
MOV = b'\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  ' + b'\x00' * 64
WEBM = b'\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01' + b'\x00' * 64


def _real_image(fmt):
    # Board images are now decoded and re-encoded, so they must be real.
    buf = io.BytesIO()
    Image.new('RGB', (64, 48), (200, 30, 30)).save(buf, fmt)
    return buf.getvalue()


PNG = _real_image('PNG')
JPEG = _real_image('JPEG')
HEIC = b'\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic' + b'\x00' * 64

VIDEO_KEYS = {'id', 'title', 'description', 'media_type', 'media_url', 'video_url',
              'thumbnail_url', 'audience', 'is_featured', 'is_published', 'is_read',
              'school_id', 'publish_at', 'expires_at', 'created_at'}


def _uid():
    return uuid4().hex[:10]


class BoardVideoUploadDisabledTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        with cls.app.app_context():
            cls.roles = {n: Role.query.filter_by(name=n).first()
                         for n in ('school_admin', 'parent')}
            assert all(cls.roles.values()), cls.roles

    def setUp(self):
        self.sfx = _uid()
        self.save_spy = mock.patch('app.utils.helpers.save_uploaded_file',
                                   wraps=helpers.save_uploaded_file).start()
        self.storage = mock.patch('app.utils.helpers._supabase_upload',
                                  return_value=FAKE_STORAGE_URL).start()
        self.push = mock.patch('app.blueprints.admin._push_school_board').start()
        self.addCleanup(mock.patch.stopall)
        with self.app.app_context():
            a = School(school_name=f'Vid School A {self.sfx}', code=f'VA{self.sfx[:8]}',
                       capacity=0, is_active=True)
            b = School(school_name=f'Vid School B {self.sfx}', code=f'VB{self.sfx[:8]}',
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

            users = dict(admin_a=user('vadmin_a', 'school_admin', a),
                         admin_b=user('vadmin_b', 'school_admin', b),
                         parent_a=user('vparent_a', 'parent', a),
                         parent_b=user('vparent_b', 'parent', b))
            db.session.flush()
            # Pre-existing board rows, as already stored in production.
            video = SchoolVideo(
                school_id=a.id, title='Existing demo video', description='demo',
                media_type='video', video_url='https://cdn.example.test/demo.mp4',
                thumbnail_url='https://cdn.example.test/demo.jpg', audience='all',
                is_featured=True, is_active=True, created_by=users['admin_a'].id,
                created_at=datetime(2026, 5, 1, 9, 30, 0))
            image = SchoolVideo(
                school_id=a.id, title='Existing image', media_type='image',
                video_url='https://cdn.example.test/pic.png', audience='parents',
                is_active=True, created_by=users['admin_a'].id,
                created_at=datetime(2026, 5, 2, 9, 30, 0))
            db.session.add_all([video, image])
            db.session.commit()
            self.ids = {k: u.id for k, u in users.items()}
            self.ids.update(school_a=a.id, school_b=b.id, video=video.id, image=image.id)
            self.names = {k: u.username for k, u in users.items()}
            self.tokens = {k: encode_token(users[k]) for k in ('parent_a', 'parent_b')}

    def tearDown(self):
        with self.app.app_context():
            sids = [self.ids['school_a'], self.ids['school_b']]
            SchoolContentRead.query.execution_options(bypass_tenant_scope=True).filter(
                SchoolContentRead.school_id.in_(sids)).delete(synchronize_session=False)
            for model in (SchoolVideo, SchoolAnnouncement):
                model.query.execution_options(bypass_tenant_scope=True).filter(
                    model.school_id.in_(sids)).delete(synchronize_session=False)
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

    def _client(self, key=None):
        client = self.app.test_client()
        if key:
            resp = client.post('/auth/login', data={'username': self.names[key],
                                                    'password': PASSWORD})
            self.assertIn(resp.status_code, (200, 302))
        return client

    @staticmethod
    def _form(media_type='video', file=None, **extra):
        data = {'title': 'New post', 'description': 'd', 'audience': 'all',
                'is_active': 'on', **extra}
        if media_type is not None:
            data['media_type'] = media_type
        if file is not None:
            name, content, mimetype = file
            data['media_file'] = (io.BytesIO(content), name, mimetype)
        return data

    def _create(self, client, **kw):
        return client.post('/admin/school-board/videos/create', data=self._form(**kw),
                           content_type='multipart/form-data')

    def _edit(self, client, board_id, **kw):
        return client.post(f'/admin/school-board/videos/{board_id}/edit',
                           data=self._form(**kw), content_type='multipart/form-data')

    def _rows(self, school_key='school_a'):
        with self.app.app_context():
            return {(v.id, v.title, v.media_type, v.video_url, v.is_active)
                    for v in SchoolVideo.query.execution_options(bypass_tenant_scope=True)
                    .filter_by(school_id=self.ids[school_key]).all()}

    def _assert_refused(self, resp, rows_before):
        self.assertEqual(resp.status_code, 200)
        self.assertIn(DISABLED, resp.get_data(as_text=True))
        self.assertEqual(self._rows(), rows_before, 'board rows changed')
        self.save_spy.assert_not_called()
        self.storage.assert_not_called()
        self.push.assert_not_called()

    def _mobile(self, path, key='parent_a'):
        return self.app.test_client().get(
            f'/api/mobile/v1{path}', headers={'Authorization': f'Bearer {self.tokens[key]}'})

    def _expected_existing_video(self, is_read):
        with self.app.app_context():
            v = db.session.get(SchoolVideo, self.ids['video'])
            return {
                'id': v.id, 'title': 'Existing demo video', 'description': 'demo',
                'media_type': 'video', 'media_url': 'https://cdn.example.test/demo.mp4',
                'video_url': 'https://cdn.example.test/demo.mp4',
                'thumbnail_url': 'https://cdn.example.test/demo.jpg', 'audience': 'all',
                'is_featured': True, 'is_published': True, 'is_read': is_read,
                'school_id': self.ids['school_a'], 'publish_at': None, 'expires_at': None,
                'created_at': '2026-05-01T09:30:00+00:00',
            }

    # ── 1-6: every video creation attempt is refused before any write ────────

    def test_create_video_uploads_refused_before_storage_or_db(self):
        client = self._client('admin_a')
        before = self._rows()
        for name, content, mimetype in (('clip.mp4', MP4, 'video/mp4'),
                                        ('clip.webm', WEBM, 'video/webm'),
                                        ('clip.mov', MOV, 'video/quicktime')):
            with self.subTest(file=name):
                self._assert_refused(
                    self._create(client, file=(name, content, mimetype)), before)

    def test_create_video_without_or_with_invalid_media_type_refused(self):
        client = self._client('admin_a')
        before = self._rows()
        self._assert_refused(self._create(client, media_type=None,
                                          file=('clip.mp4', MP4, 'video/mp4')), before)
        self._assert_refused(self._create(client, media_type='movie',
                                          file=('clip.mp4', MP4, 'video/mp4')), before)

    def test_create_video_external_link_refused(self):
        client = self._client('admin_a')
        before = self._rows()
        self._assert_refused(self._create(client, video_url='https://youtu.be/abc'), before)

    def test_video_renamed_as_image_refused(self):
        client = self._client('admin_a')
        before = self._rows()
        for name, content, mimetype in (('photo.jpg', MP4, 'image/jpeg'),
                                        ('photo.png', WEBM, 'image/png'),
                                        ('photo.webp', MOV, 'image/webp'),
                                        ('photo.jpg', JPEG, 'video/mp4')):
            with self.subTest(file=name, mimetype=mimetype):
                self._assert_refused(
                    self._create(client, media_type='image', file=(name, content, mimetype)),
                    before)

    # ── edit: no new video content; existing video details still editable ───

    def test_edit_cannot_introduce_new_video_content(self):
        client = self._client('admin_a')
        before = self._rows()
        self._assert_refused(self._edit(client, self.ids['video'],
                                        file=('new.mp4', MP4, 'video/mp4')), before)
        self._assert_refused(self._edit(client, self.ids['video'],
                                        video_url='https://youtu.be/other'), before)
        self._assert_refused(self._edit(client, self.ids['image'],
                                        video_url='https://youtu.be/new'), before)
        self._assert_refused(self._edit(client, self.ids['image'], media_type='image',
                                        file=('p.jpg', MP4, 'image/jpeg')), before)

    def test_edit_existing_video_details_still_allowed(self):
        client = self._client('admin_a')
        resp = self._edit(client, self.ids['video'], title='Renamed demo',
                          video_url='https://cdn.example.test/demo.mp4')   # same link
        self.assertEqual(resp.status_code, 302)
        with self.app.app_context():
            v = db.session.get(SchoolVideo, self.ids['video'])
            self.assertEqual((v.title, v.media_type, v.video_url),
                             ('Renamed demo', 'video', 'https://cdn.example.test/demo.mp4'))
        self.storage.assert_not_called()

    # ── 7-9: existing videos stay readable, unchanged ───────────────────────

    def test_mobile_video_list_detail_featured_unchanged(self):
        resp = self._mobile('/school/videos?limit=20&offset=0')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual((data['ok'], data['total'], data['limit'], data['offset']),
                         (True, 2, 20, 0))
        by_id = {v['id']: v for v in data['videos']}
        self.assertEqual(set(by_id), {self.ids['video'], self.ids['image']})
        for item in data['videos']:
            self.assertEqual(set(item), VIDEO_KEYS)
        self.assertEqual(by_id[self.ids['video']], self._expected_existing_video(False))

        featured = self._mobile('/school/videos/featured').get_json()
        self.assertEqual(featured['video'], self._expected_existing_video(False))

        detail = self._mobile(f"/school/videos/{self.ids['video']}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.get_json()['video'], self._expected_existing_video(True))
        board = self._mobile('/school/board')
        self.assertEqual(board.status_code, 200)

    def test_admin_list_toggle_delete_existing_video_unchanged(self):
        client = self._client('admin_a')
        page = client.get('/admin/school-board/videos')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Existing demo video', page.get_data(as_text=True))
        self.assertEqual(client.post(
            f"/admin/school-board/videos/{self.ids['video']}/toggle").status_code, 302)
        with self.app.app_context():
            self.assertFalse(db.session.get(SchoolVideo, self.ids['video']).is_active)
        self.assertEqual(client.post(
            f"/admin/school-board/videos/{self.ids['video']}/delete").status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(SchoolVideo, self.ids['video']))
        self.storage.assert_not_called()

    # ── 10-12: images, announcements, documents unchanged ────────────────────

    def test_image_post_create_and_edit_still_work(self):
        client = self._client('admin_a')
        resp = self._create(client, media_type='image', title='Pic post',
                            file=('pic.png', PNG, 'image/png'))
        self.assertEqual(resp.status_code, 302)
        self.storage.assert_called_once()
        _, object_path, content_type = self.storage.call_args.args
        self.assertTrue(object_path.startswith(f"schools/{self.ids['school_a']}/board/media/"))
        # stored as the optimised WebP (see test_board_image_optimization.py)
        self.assertTrue(object_path.endswith('.webp'))
        self.assertEqual((content_type, self.storage.call_args.kwargs), ('image/webp',
                                                                         {'bucket': 'school-media'}))
        with self.app.app_context():
            row = SchoolVideo.query.execution_options(bypass_tenant_scope=True).filter_by(
                school_id=self.ids['school_a'], title='Pic post').one()
            self.assertEqual((row.media_type, row.video_url), ('image', FAKE_STORAGE_URL))
        self.push.assert_called_once()
        # external links are no longer accepted; a HEIF-branded .jpg is still
        # not treated as a video, but it is not a decodable jpg/png/webp either
        linked = self._create(client, media_type='image', title='Linked pic',
                              video_url='https://cdn.example.test/x.png')
        self.assertIn('الروابط الخارجية غير متاحة', linked.get_data(as_text=True))
        heif = self._create(client, media_type='image', title='Heif pic',
                            file=('h.jpg', HEIC, 'image/jpeg')).get_data(as_text=True)
        self.assertNotIn(DISABLED, heif)
        self.assertIn(MSG_INVALID, heif)
        with self.app.app_context():
            self.assertEqual(SchoolVideo.query.execution_options(bypass_tenant_scope=True)
                             .filter(SchoolVideo.title.in_(['Linked pic', 'Heif pic'])).count(), 0)
        # edit an image post with a new real image
        self.assertEqual(self._edit(client, self.ids['image'], media_type='image',
                                    file=('new.jpg', JPEG, 'image/jpeg')).status_code, 302)
        with self.app.app_context():
            self.assertEqual(db.session.get(SchoolVideo, self.ids['image']).video_url,
                             FAKE_STORAGE_URL)

    def test_image_type_limits_unchanged(self):
        client = self._client('admin_a')
        before = self._rows()
        resp = self._create(client, media_type='image', file=('doc.pdf', b'%PDF-1.4', 'application/pdf'))
        self.assertIn('نوع الملف غير مدعوم', resp.get_data(as_text=True))
        big = self._create(client, media_type='image',
                           file=('big.png', PNG + b'\x00' * (5 * 1024 * 1024), 'image/png'))
        self.assertIn('أكبر من الحد المسموح', big.get_data(as_text=True))
        self.assertEqual(self._rows(), before)
        self.storage.assert_not_called()

    def test_announcement_creation_unchanged(self):
        client = self._client('admin_a')
        resp = client.post('/admin/school-board/announcements/create', data={
            'title': 'Ann', 'body': 'Body', 'audience': 'all', 'media_type': 'image',
            'media_url': 'https://cdn.example.test/a.png', 'is_active': 'on'})
        self.assertEqual(resp.status_code, 302)
        with self.app.app_context():
            ann = SchoolAnnouncement.query.execution_options(bypass_tenant_scope=True) \
                .filter_by(school_id=self.ids['school_a'], title='Ann').one()
            self.assertEqual(ann.media_type, 'image')
        # announcements never accepted video, and still do not
        bad = client.post('/admin/school-board/announcements/create', data={
            'title': 'Ann2', 'body': 'Body', 'audience': 'all', 'media_type': 'video',
            'media_url': 'https://cdn.example.test/a.mp4'})
        self.assertIn('نوع الوسائط غير صالح', bad.get_data(as_text=True))

    def test_generic_document_upload_helper_unchanged(self):
        with self.app.test_request_context():
            from werkzeug.datastructures import FileStorage
            pdf = FileStorage(io.BytesIO(b'%PDF-1.4 x'), filename='leave.pdf',
                              content_type='application/pdf')
            url = helpers.save_uploaded_file(
                pdf, 'leave_requests',
                allowed_exts=helpers.ALLOWED_IMAGE_EXTENSIONS | helpers.ALLOWED_DOC_EXTENSIONS)
        self.assertEqual(url, FAKE_STORAGE_URL)
        self.assertEqual(self.storage.call_args.args[2], 'application/pdf')

    # ── 13-14: authorization and isolation unchanged ─────────────────────────

    def test_unauthorized_callers_get_existing_result_not_video_message(self):
        before = self._rows()
        anon = self._create(self._client(), file=('clip.mp4', MP4, 'video/mp4'))
        self.assertEqual(anon.status_code, 302)
        self.assertIn('/auth/login', anon.headers['Location'])
        parent = self._client('parent_a')
        for resp in (self._create(parent, file=('clip.mp4', MP4, 'video/mp4')),
                     self._edit(parent, self.ids['video'], file=('c.mp4', MP4, 'video/mp4'))):
            self.assertNotIn(DISABLED, resp.get_data(as_text=True))
            self.assertIn(resp.status_code, (302, 403))
        self.assertEqual(self._rows(), before)
        self.save_spy.assert_not_called()
        self.storage.assert_not_called()

    def test_cross_school_unchanged(self):
        client_b = self._client('admin_b')
        before = self._rows()
        for resp in (self._edit(client_b, self.ids['video'], file=('c.mp4', MP4, 'video/mp4')),
                     self._edit(client_b, self.ids['video'], title='hijack'),
                     client_b.post(f"/admin/school-board/videos/{self.ids['video']}/delete")):
            self.assertEqual(resp.status_code, 404)
        self.assertNotIn('Existing demo video',
                         client_b.get('/admin/school-board/videos').get_data(as_text=True))
        self.assertEqual(self._rows(), before)
        other = self._mobile('/school/videos?limit=20&offset=0', key='parent_b').get_json()
        self.assertEqual((other['total'], other['videos']), (0, []))
        self.assertEqual(self._mobile(f"/school/videos/{self.ids['video']}",
                                      key='parent_b').status_code, 404)
        self.storage.assert_not_called()

    # ── web form: video option only for existing video posts ─────────────────

    def test_form_offers_video_only_for_existing_video_posts(self):
        client = self._client('admin_a')
        new = client.get('/admin/school-board/videos/create').get_data(as_text=True)
        self.assertNotIn('id="type_video"', new)
        self.assertIn('id="type_image"', new)
        edit_video = client.get(f"/admin/school-board/videos/{self.ids['video']}/edit")
        self.assertIn('id="type_video"', edit_video.get_data(as_text=True))
        edit_image = client.get(f"/admin/school-board/videos/{self.ids['image']}/edit")
        self.assertNotIn('id="type_video"', edit_image.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()

"""
School Board images: optimised once at upload; no new external links.

Helper level (app.utils.board_images.optimize_board_image), real images made
with Pillow: validation from actual bytes, EXIF orientation, metadata strip,
transparency, <=1600 px longest side without upscaling, WebP output, pixel
and animation guards.

Route level (POST /admin/school-board/videos/create and /<id>/edit):
only the optimised WebP reaches Storage (content type image/webp, .webp
object); invalid / oversized / non-image / disguised-video uploads never reach
Storage or the DB; new external links are refused while legacy external rows
stay readable and keep their URLs on metadata edits; edits without a new file
never reprocess; mobile JSON field names unchanged; auth and cross-school
behaviour unchanged; the generic upload helper still stores bytes untouched.

Storage and pushes are recording mocks: no network, no files written.
Run with ``-s`` to see the compression examples table.
"""
import io
import unittest
from datetime import datetime
from unittest import mock
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from werkzeug.datastructures import FileStorage

from app import create_app
from app.models import db, Role, School, User, AuditLog, SchoolVideo, SchoolContentRead
from app.blueprints.mobile_api.utils import encode_token
from app.utils import helpers
from app.utils.board_images import (
    BoardImageError, MSG_ANIMATED, MSG_INVALID, MSG_TOO_LARGE, optimize_board_image,
)

PASSWORD = 'Test1234!'
FAKE_STORAGE_URL = 'https://storage.test/school-media/opt.webp'
EXTERNAL_MSG = 'الروابط الخارجية غير متاحة. يرجى رفع صورة.'
VIDEO_MSG = 'رفع الفيديو غير متاح حالياً.'
SIZE_MSG = 'أكبر من الحد المسموح'
MP4 = b'\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom' + b'\x00' * 64
VIDEO_KEYS = {'id', 'title', 'description', 'media_type', 'media_url', 'video_url',
              'thumbnail_url', 'audience', 'is_featured', 'is_published', 'is_read',
              'school_id', 'publish_at', 'expires_at', 'created_at'}


# ── synthetic images (no production files) ───────────────────────────────────

def _enc(img, fmt, **kw):
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def _photo(w, h):
    """Photo-like: gradients, shapes, blur and grain."""
    r = Image.linear_gradient('L').resize((w, h))
    g = Image.radial_gradient('L').resize((w, h))
    img = Image.merge('RGB', (r, g, r.transpose(Image.Transpose.FLIP_LEFT_RIGHT)))
    d = ImageDraw.Draw(img)
    for i in range(40):
        x, y = (i * 997) % w, (i * 613) % h
        d.ellipse((x, y, x + w // 8, y + h // 8),
                  fill=((i * 50) % 255, (i * 90) % 255, (i * 20) % 255))
    img = img.filter(ImageFilter.GaussianBlur(3))
    return Image.blend(img, Image.effect_noise((w, h), 40).convert('RGB'), 0.18)


def _textured(w, h):
    """Detail-heavy content that does not average away when resized."""
    n = Image.effect_noise((w, h), 30).filter(ImageFilter.GaussianBlur(1))
    return Image.merge('RGB', (n, n.transpose(Image.Transpose.FLIP_TOP_BOTTOM),
                               n.transpose(Image.Transpose.FLIP_LEFT_RIGHT)))


def _font(size):
    try:
        return ImageFont.truetype('arial.ttf', size)
    except OSError:
        return ImageFont.load_default()


def _poster(w, h):
    """School-poster-like: flat colours and many lines of Arabic/Latin text."""
    img = Image.new('RGB', (w, h), (250, 246, 235))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, w, h // 9), fill=(20, 70, 140))
    d.text((w // 20, h // 30), 'مدرسة المهندس — إعلان هام', font=_font(h // 38), fill='white')
    for i in range(28):
        d.text((w // 20, h // 7 + i * h // 34),
               f'السطر {i + 1}: موعد الاجتماع يوم الخميس Meeting line {i + 1}',
               font=_font(h // 72), fill=(30, 30, 30))
    d.ellipse((w - w // 3, h - w // 3, w - w // 20, h - w // 20), fill=(220, 60, 60))
    return img


def _transparent(w, h):
    img = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((w // 9, h // 12, w - w // 9, h - h // 12), fill=(40, 160, 90, 255))
    d.rectangle((w // 3, h // 3, w - w // 3, h - h // 3), fill=(255, 255, 255, 128))
    return img


def _jpeg_with_exif(w, h, orientation=1):
    img = Image.new('RGB', (w, h), (0, 0, 255))
    ImageDraw.Draw(img).rectangle((0, 0, w // 10, h // 10), fill=(255, 0, 0))  # top-left mark
    exif = Image.Exif()
    exif[0x0112] = orientation          # Orientation
    exif[0x010F] = 'SecretCam'          # Make
    exif[0x0110] = 'Model X'            # Model
    exif[0x0131] = 'EditorApp 1.0'      # Software
    return _enc(img, 'JPEG', quality=90, exif=exif.tobytes())


def _decode(data):
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


# ─────────────────────────────────────────────────────────────────────────────
#  Helper level
# ─────────────────────────────────────────────────────────────────────────────

class BoardImageHelperTest(unittest.TestCase):

    def _ok(self, raw):
        out = optimize_board_image(raw)
        img = _decode(out.data)
        self.assertEqual(img.format, 'WEBP')                       # 9: decodable WebP
        self.assertEqual(img.size, (out.width, out.height))
        return out, img

    def test_01_normal_jpeg_accepted(self):
        out, img = self._ok(_enc(_photo(1200, 900), 'JPEG', quality=90))
        self.assertEqual((img.size, out.source_format), ((1200, 900), 'JPEG'))

    def test_02_large_jpeg_longest_side_1600(self):
        raw = _enc(_photo(4000, 3000), 'JPEG', quality=92)
        self.assertLess(len(raw), 5 * 1024 * 1024)
        _, img = self._ok(raw)
        self.assertEqual(img.size, (1600, 1200))                   # aspect preserved
        _, tall = self._ok(_enc(_poster(1500, 3000), 'JPEG', quality=90))
        self.assertEqual(tall.size, (800, 1600))

    def test_03_png_accepted_and_optimised(self):
        raw = _enc(_poster(2480, 3508), 'PNG')
        out, img = self._ok(raw)
        self.assertEqual((max(img.size), out.source_format), (1600, 'PNG'))
        self.assertLess(len(out.data), len(raw))

    def test_04_webp_accepted_and_optimised(self):
        raw = _enc(_photo(3000, 2000), 'WEBP', quality=95)
        out, img = self._ok(raw)
        self.assertEqual((img.size, out.source_format), ((1600, 1067), 'WEBP'))
        self.assertLess(len(out.data), len(raw))

    def test_05_exif_orientation_applied(self):
        # stored landscape 2000x1000, orientation 6 = display rotated 90° clockwise
        _, img = self._ok(_jpeg_with_exif(2000, 1000, orientation=6))
        self.assertEqual(img.size, (800, 1600))
        rgb = img.convert('RGB')
        r, g, b = rgb.getpixel((img.width - 20, 20))               # mark now top-right
        self.assertTrue(r > 200 and b < 60, (r, g, b))
        r, g, b = rgb.getpixel((20, 20))
        self.assertTrue(b > 200 and r < 60, (r, g, b))

    def test_06_metadata_stripped(self):
        _, img = self._ok(_jpeg_with_exif(1200, 800, orientation=1))
        self.assertNotIn('exif', img.info)
        self.assertNotIn('xmp', img.info)
        self.assertEqual(len(img.getexif()), 0)

    def test_07_transparency_preserved(self):
        pal = Image.new('P', (900, 600), 0)
        pal.putpalette([0, 0, 0, 40, 160, 90] + [0] * 762)   # index 0 = transparent
        ImageDraw.Draw(pal).ellipse((100, 50, 800, 550), fill=1)
        for label, raw in (('rgba', _enc(_transparent(1800, 1200), 'PNG')),
                           ('palette', _enc(pal, 'PNG', transparency=0))):
            with self.subTest(label):
                _, img = self._ok(raw)
                self.assertEqual(img.mode, 'RGBA')
                self.assertLess(img.getpixel((2, 2))[3], 10)                    # clear corner
                self.assertGreater(img.getpixel((img.width // 2, img.height // 5))[3], 245)

    def test_08_small_source_not_upscaled(self):
        _, img = self._ok(_enc(_photo(800, 600), 'PNG'))
        self.assertEqual(img.size, (800, 600))
        _, img = self._ok(_enc(_photo(1600, 900), 'JPEG'))
        self.assertEqual(img.size, (1600, 900))

    def test_opaque_source_has_no_alpha(self):
        _, img = self._ok(_enc(_photo(900, 600), 'PNG'))
        self.assertEqual(img.mode, 'RGB')

    def test_12_multi_megabyte_photo_substantially_smaller(self):
        raw = _enc(_photo(4000, 3000), 'JPEG', quality=92)
        self.assertGreater(len(raw), 2 * 1024 * 1024)
        out = optimize_board_image(raw)
        self.assertLess(len(out.data), len(raw) // 4)

    def test_flat_graphic_never_needlessly_larger(self):
        raw = _enc(Image.new('RGB', (600, 400), (10, 120, 200)), 'PNG')   # tiny flat PNG
        out = optimize_board_image(raw)
        _decode(out.data)
        self.assertLessEqual(len(out.data), max(len(raw), 1024))

    def test_14_15_corrupt_and_non_images_refused(self):
        good = _enc(_photo(1200, 900), 'JPEG', quality=90)
        for label, raw in (('truncated jpeg', good[: len(good) // 2]),
                           ('header then junk', good[:20] + b'\x13\x37' * 4000),
                           ('text', b'hello, this is not an image' * 50),
                           ('pdf', b'%PDF-1.4\n' + b'0' * 500),
                           ('gif', _enc(_photo(200, 100), 'GIF')),
                           ('bmp', _enc(_photo(200, 100), 'BMP')),
                           ('mp4', MP4),
                           ('empty', b'')):
            with self.subTest(label):
                with self.assertRaises(BoardImageError) as ctx:
                    optimize_board_image(raw)
                self.assertEqual(str(ctx.exception), MSG_INVALID)

    def test_pixel_bomb_refused_before_decode(self):
        raw = _enc(Image.new('L', (9000, 5000)), 'PNG')            # 45 MP, tiny file
        self.assertLess(len(raw), 1024 * 1024)
        with self.assertRaises(BoardImageError) as ctx:
            optimize_board_image(raw)
        self.assertEqual(str(ctx.exception), MSG_TOO_LARGE)

    def test_animated_refused(self):
        a, b = _photo(200, 100), _photo(200, 100).rotate(180)
        for fmt in ('WEBP', 'PNG'):
            with self.subTest(fmt):
                raw = _enc(a, fmt, save_all=True, append_images=[b], duration=100)
                with self.assertRaises(BoardImageError) as ctx:
                    optimize_board_image(raw)
                self.assertEqual(str(ctx.exception), MSG_ANIMATED)

    def test_13_compression_examples_report(self):
        cases = [
            ('photo-like JPEG q92', _enc(_photo(4000, 3000), 'JPEG', quality=92)),
            ('detail-heavy JPEG q92', _enc(_textured(1600, 1200), 'JPEG', quality=92)),
            ('poster PNG (A4 300dpi)', _enc(_poster(2480, 3508), 'PNG')),
            ('poster JPEG q95', _enc(_poster(2480, 3508), 'JPEG', quality=95)),
            ('transparent PNG', _enc(_transparent(1800, 1200), 'PNG')),
        ]
        print('\n\ncase | input | output | reduction')
        for label, raw in cases:
            src = Image.open(io.BytesIO(raw))
            out = optimize_board_image(raw)
            pct = 100 - 100 * len(out.data) / len(raw)
            print(f'{label} | {src.width}x{src.height} {src.format} {len(raw):,} B | '
                  f'{out.width}x{out.height} WEBP{" lossless" if out.lossless else ""} '
                  f'{len(out.data):,} B | {pct:.1f}%')
            self.assertLessEqual(max(out.width, out.height), 1600)


# ─────────────────────────────────────────────────────────────────────────────
#  Route level
# ─────────────────────────────────────────────────────────────────────────────

class BoardImageRouteTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        with cls.app.app_context():
            cls.roles = {n: Role.query.filter_by(name=n).first()
                         for n in ('school_admin', 'parent')}
            assert all(cls.roles.values()), cls.roles

    def setUp(self):
        self.sfx = uuid4().hex[:10]
        self.save_spy = mock.patch('app.utils.helpers.save_uploaded_file',
                                   wraps=helpers.save_uploaded_file).start()
        self.storage = mock.patch('app.utils.helpers._supabase_upload',
                                  return_value=FAKE_STORAGE_URL).start()
        # create=True keeps this suite runnable against code without the
        # optimiser (negative control), where every upload must then differ.
        self.optimize = mock.patch('app.blueprints.admin.optimize_board_image',
                                   wraps=optimize_board_image, create=True).start()
        self.push = mock.patch('app.blueprints.admin._push_school_board').start()
        self.addCleanup(mock.patch.stopall)
        with self.app.app_context():
            a = School(school_name=f'Img School A {self.sfx}', code=f'IA{self.sfx[:8]}',
                       capacity=0, is_active=True)
            b = School(school_name=f'Img School B {self.sfx}', code=f'IB{self.sfx[:8]}',
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

            users = dict(admin_a=user('iadmin_a', 'school_admin', a),
                         admin_b=user('iadmin_b', 'school_admin', b),
                         parent_a=user('iparent_a', 'parent', a))
            db.session.flush()
            legacy = SchoolVideo(       # legacy external-link image row
                school_id=a.id, title='Legacy poster', media_type='image',
                video_url='https://legacy.example.test/poster.jpg',
                thumbnail_url='https://legacy.example.test/poster-thumb.jpg',
                audience='all', is_active=True, created_by=users['admin_a'].id,
                created_at=datetime(2026, 4, 1, 8, 0, 0))
            stored = SchoolVideo(       # image previously uploaded to Storage
                school_id=a.id, title='Stored image', media_type='image',
                video_url='https://storage.test/school-media/old.png', audience='all',
                is_active=True, created_by=users['admin_a'].id,
                created_at=datetime(2026, 4, 2, 8, 0, 0))
            db.session.add_all([legacy, stored])
            db.session.commit()
            self.ids = {k: u.id for k, u in users.items()}
            self.ids.update(school_a=a.id, school_b=b.id, legacy=legacy.id, stored=stored.id)
            self.names = {k: u.username for k, u in users.items()}
            self.token = encode_token(users['parent_a'])

    def tearDown(self):
        with self.app.app_context():
            sids = [self.ids['school_a'], self.ids['school_b']]
            SchoolContentRead.query.execution_options(bypass_tenant_scope=True).filter(
                SchoolContentRead.school_id.in_(sids)).delete(synchronize_session=False)
            SchoolVideo.query.execution_options(bypass_tenant_scope=True).filter(
                SchoolVideo.school_id.in_(sids)).delete(synchronize_session=False)
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
    def _form(file=None, media_type='image', **extra):
        data = {'title': 'Poster', 'description': 'd', 'audience': 'all', 'is_active': 'on',
                'media_type': media_type, **extra}
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

    def _snapshot(self):
        with self.app.app_context():
            return sorted((v.id, v.title, v.media_type, v.video_url, v.thumbnail_url,
                           v.audience, v.is_active)
                          for v in SchoolVideo.query.execution_options(bypass_tenant_scope=True)
                          .filter(SchoolVideo.school_id.in_([self.ids['school_a'],
                                                             self.ids['school_b']])).all())

    def _row(self, board_id):
        with self.app.app_context():
            return db.session.get(SchoolVideo, board_id)

    def _stored(self):
        """(bytes, object_path, content_type) of the single Storage upload."""
        self.storage.assert_called_once()
        data, path, ctype = self.storage.call_args.args
        self.assertEqual(self.storage.call_args.kwargs, {'bucket': 'school-media'})
        return data, path, ctype

    def _assert_refused(self, resp, before, message):
        self.assertEqual(resp.status_code, 200)
        self.assertIn(message, resp.get_data(as_text=True))
        self.assertEqual(self._snapshot(), before, 'DB changed on a refused upload')
        self.save_spy.assert_not_called()
        self.storage.assert_not_called()
        self.push.assert_not_called()

    def _mobile(self, path):
        return self.app.test_client().get(f'/api/mobile/v1{path}',
                                          headers={'Authorization': f'Bearer {self.token}'})

    # ── accepted uploads: only the optimised WebP is stored ──────────────────

    def test_01_02_10_11_create_stores_only_optimised_webp(self):
        raw = _enc(_photo(4000, 3000), 'JPEG', quality=92)
        resp = self._create(self._client('admin_a'), file=('big.jpg', raw, 'image/jpeg'))
        self.assertEqual(resp.status_code, 302)
        data, path, ctype = self._stored()
        self.assertEqual(ctype, 'image/webp')
        self.assertTrue(path.startswith(f"schools/{self.ids['school_a']}/board/media/"))
        self.assertTrue(path.endswith('.webp'))
        img = _decode(data)
        self.assertEqual((img.format, img.size), ('WEBP', (1600, 1200)))
        self.assertLess(len(data), len(raw) // 4)
        self.assertNotEqual(data, raw)                              # original never stored
        with self.app.app_context():
            row = SchoolVideo.query.execution_options(bypass_tenant_scope=True).filter_by(
                school_id=self.ids['school_a'], title='Poster').one()
            self.assertEqual((row.media_type, row.video_url, row.thumbnail_url),
                             ('image', FAKE_STORAGE_URL, None))
        self.push.assert_called_once()

    def test_03_04_png_and_webp_uploads(self):
        client = self._client('admin_a')
        for name, raw, mime in (('p.png', _enc(_poster(1240, 1754), 'PNG'), 'image/png'),
                                ('w.webp', _enc(_photo(2000, 1500), 'WEBP'), 'image/webp')):
            with self.subTest(name):
                self.storage.reset_mock()
                self.assertEqual(self._create(client, file=(name, raw, mime),
                                              title=name).status_code, 302)
                data, path, ctype = self._stored()
                self.assertEqual((ctype, path[-5:], _decode(data).format),
                                 ('image/webp', '.webp', 'WEBP'))

    def test_storage_failure_creates_no_row(self):
        before = self._snapshot()
        with mock.patch('app.utils.helpers.save_uploaded_file', return_value=None):
            resp = self._create(self._client('admin_a'),
                                file=('ok.jpg', _enc(_photo(800, 600), 'JPEG'), 'image/jpeg'))
        self.assertIn('فشل رفع الملف إلى التخزين', resp.get_data(as_text=True))
        self.assertEqual(self._snapshot(), before)

    # ── 13-18: refused before Storage and before any DB change ───────────────

    def test_13_over_5mb_refused_before_processing(self):
        raw = _enc(_photo(800, 600), 'JPEG') + b'\x00' * (5 * 1024 * 1024)
        before = self._snapshot()
        self._assert_refused(self._create(self._client('admin_a'),
                                          file=('big.jpg', raw, 'image/jpeg')), before, SIZE_MSG)
        self.optimize.assert_not_called()

    def test_14_15_16_invalid_uploads_refused(self):
        client = self._client('admin_a')
        before = self._snapshot()
        good = _enc(_photo(1200, 900), 'JPEG', quality=90)
        for name, raw, mime, message in (
                ('corrupt.jpg', good[: len(good) // 2], 'image/jpeg', MSG_INVALID),
                ('notes.jpg', b'plain text, not an image' * 40, 'image/jpeg', MSG_INVALID),
                ('doc.png', b'%PDF-1.4\n' + b'0' * 400, 'image/png', MSG_INVALID),
                ('anim.webp', _enc(_photo(200, 100), 'WEBP', save_all=True,
                                   append_images=[_photo(200, 100).rotate(90)]),
                 'image/webp', MSG_ANIMATED),
                ('bomb.png', _enc(Image.new('L', (9000, 5000)), 'PNG'), 'image/png',
                 MSG_TOO_LARGE),
                ('clip.jpg', MP4, 'image/jpeg', VIDEO_MSG)):
            with self.subTest(name):
                self._assert_refused(self._create(client, file=(name, raw, mime)),
                                     before, message)

    # ── 19-20: new external links refused ────────────────────────────────────

    def test_19_20_new_external_links_refused(self):
        client = self._client('admin_a')
        before = self._snapshot()
        good = ('ok.jpg', _enc(_photo(800, 600), 'JPEG'), 'image/jpeg')
        for kw in ({'video_url': 'https://cdn.example.test/p.png'},
                   {'video_url': 'https://cdn.example.test/p.png', 'file': good},
                   {'thumbnail_url': 'https://cdn.example.test/t.png', 'file': good}):
            with self.subTest(kw=sorted(kw)):
                self._assert_refused(self._create(client, **kw), before, EXTERNAL_MSG)
        # the form no longer offers the field at all
        page = client.get('/admin/school-board/videos/create').get_data(as_text=True)
        self.assertNotIn('name="video_url"', page)
        self.assertIn('name="media_file"', page)
        # still refused as video first when the post is a video link
        self._assert_refused(self._create(client, media_type='video',
                                          video_url='https://youtu.be/x'), before, VIDEO_MSG)

    # ── 21-23: legacy external-link rows ─────────────────────────────────────

    def test_21_legacy_external_row_readable(self):
        data = self._mobile('/school/videos?limit=20&offset=0').get_json()
        item = {v['id']: v for v in data['videos']}[self.ids['legacy']]
        self.assertEqual((item['media_url'], item['video_url'], item['thumbnail_url']),
                         ('https://legacy.example.test/poster.jpg',
                          'https://legacy.example.test/poster.jpg',
                          'https://legacy.example.test/poster-thumb.jpg'))

    def test_22_legacy_metadata_edit_keeps_urls(self):
        resp = self._edit(self._client('admin_a'), self.ids['legacy'], title='Legacy renamed',
                          audience='parents')
        self.assertEqual(resp.status_code, 302)
        row = self._row(self.ids['legacy'])
        self.assertEqual((row.title, row.audience, row.video_url, row.thumbnail_url),
                         ('Legacy renamed', 'parents',
                          'https://legacy.example.test/poster.jpg',
                          'https://legacy.example.test/poster-thumb.jpg'))
        self.storage.assert_not_called()
        self.optimize.assert_not_called()
        # re-posting the same URLs is a no-op, not a change
        self.assertEqual(self._edit(self._client('admin_a'), self.ids['legacy'],
                                    video_url='https://legacy.example.test/poster.jpg',
                                    thumbnail_url='https://legacy.example.test/poster-thumb.jpg'
                                    ).status_code, 302)

    def test_23_legacy_url_cannot_be_changed(self):
        client = self._client('admin_a')
        before = self._snapshot()
        for kw in ({'video_url': 'https://other.example.test/new.jpg'},
                   {'thumbnail_url': 'https://other.example.test/t.jpg'}):
            with self.subTest(kw=sorted(kw)):
                self._assert_refused(self._edit(client, self.ids['legacy'], **kw),
                                     before, EXTERNAL_MSG)
        self._assert_refused(self._edit(client, self.ids['stored'],
                                        video_url='https://other.example.test/x.jpg'),
                             before, EXTERNAL_MSG)

    def test_legacy_row_replaced_by_uploaded_image(self):
        resp = self._edit(self._client('admin_a'), self.ids['legacy'],
                          file=('new.png', _enc(_poster(1240, 1754), 'PNG'), 'image/png'))
        self.assertEqual(resp.status_code, 302)
        row = self._row(self.ids['legacy'])
        self.assertEqual((row.video_url, row.thumbnail_url), (FAKE_STORAGE_URL, None))

    # ── 24-25: existing image edits ──────────────────────────────────────────

    def test_24_edit_without_file_never_reprocesses(self):
        resp = self._edit(self._client('admin_a'), self.ids['stored'], title='Renamed')
        self.assertEqual(resp.status_code, 302)
        row = self._row(self.ids['stored'])
        self.assertEqual((row.title, row.video_url),
                         ('Renamed', 'https://storage.test/school-media/old.png'))
        self.optimize.assert_not_called()
        self.save_spy.assert_not_called()
        self.storage.assert_not_called()

    def test_25_replacement_stores_new_optimised_image(self):
        raw = _enc(_photo(3000, 2000), 'JPEG', quality=92)
        resp = self._edit(self._client('admin_a'), self.ids['stored'],
                          file=('new.jpg', raw, 'image/jpeg'))
        self.assertEqual(resp.status_code, 302)
        data, path, ctype = self._stored()
        self.assertEqual((ctype, path[-5:], _decode(data).size), ('image/webp', '.webp',
                                                                   (1600, 1067)))
        self.assertEqual(self._row(self.ids['stored']).video_url, FAKE_STORAGE_URL)

    def test_invalid_replacement_leaves_row_untouched(self):
        before = self._snapshot()
        self._assert_refused(self._edit(self._client('admin_a'), self.ids['stored'],
                                        file=('bad.jpg', b'garbage' * 100, 'image/jpeg')),
                             before, MSG_INVALID)

    # ── 26: video block still effective ──────────────────────────────────────

    def test_26_video_block_unchanged(self):
        client = self._client('admin_a')
        before = self._snapshot()
        self._assert_refused(self._create(client, media_type='video',
                                          file=('c.mp4', MP4, 'video/mp4')), before, VIDEO_MSG)
        self._assert_refused(self._create(client, file=('c.png', MP4, 'image/png')),
                             before, VIDEO_MSG)
        self.optimize.assert_not_called()

    # ── 27: mobile JSON unchanged ────────────────────────────────────────────

    def test_27_mobile_board_fields_unchanged(self):
        self._create(self._client('admin_a'), title='Fresh',
                     file=('f.jpg', _enc(_photo(900, 600), 'JPEG'), 'image/jpeg'))
        videos = self._mobile('/school/videos?limit=20&offset=0')
        self.assertEqual(videos.status_code, 200)
        items = videos.get_json()['videos']
        for item in items:
            self.assertEqual(set(item), VIDEO_KEYS)
        fresh = next(v for v in items if v['title'] == 'Fresh')
        self.assertEqual((fresh['media_type'], fresh['media_url'], fresh['video_url']),
                         ('image', FAKE_STORAGE_URL, FAKE_STORAGE_URL))
        self.assertEqual(self._mobile('/school/board').status_code, 200)

    # ── 28-29: isolation and authorization unchanged ─────────────────────────

    def test_28_cross_school_unchanged(self):
        before = self._snapshot()
        client_b = self._client('admin_b')
        good = ('ok.jpg', _enc(_photo(800, 600), 'JPEG'), 'image/jpeg')
        self.assertEqual(self._edit(client_b, self.ids['stored'], file=good).status_code, 404)
        self.assertEqual(self._edit(client_b, self.ids['legacy'], title='x').status_code, 404)
        self.assertEqual(self._snapshot(), before)
        self.optimize.assert_not_called()
        self.storage.assert_not_called()

    def test_29_authorization_unchanged(self):
        before = self._snapshot()
        good = ('ok.jpg', _enc(_photo(800, 600), 'JPEG'), 'image/jpeg')
        anon = self._create(self._client(), file=good)
        self.assertEqual(anon.status_code, 302)
        self.assertIn('/auth/login', anon.headers['Location'])
        parent = self._create(self._client('parent_a'), file=good)
        self.assertIn(parent.status_code, (302, 403))
        self.assertEqual(self._snapshot(), before)
        self.optimize.assert_not_called()
        self.storage.assert_not_called()

    # ── 30: generic helper behaviour untouched ───────────────────────────────

    def test_30_generic_upload_helper_stores_bytes_unchanged(self):
        raw = _enc(_photo(2400, 1800), 'JPEG', quality=90)
        with self.app.test_request_context():
            url = helpers.save_uploaded_file(
                FileStorage(io.BytesIO(raw), filename='student.jpg', content_type='image/jpeg'),
                'students')
        self.assertEqual(url, FAKE_STORAGE_URL)
        data, path, ctype = self.storage.call_args.args
        self.assertEqual((data, ctype, path[-4:]), (raw, 'image/jpeg', '.jpg'))
        self.optimize.assert_not_called()


if __name__ == '__main__':
    unittest.main()

"""
Student.photo upload hardening — validation only, bytes never changed.

Helper level (app.utils.student_photo.validate_student_photo) and route level
(POST /students/create, POST /students/<id>/edit):

  * valid JPEG / PNG / WebP / GIF (incl. animated GIF, as today) are accepted
    and the bytes handed to Storage are the uploaded bytes (SHA-256 equal),
    with the same extension and Content-Type as before;
  * non-images renamed .jpg, corrupt images, video renamed .jpg and extreme
    pixel counts are refused with no Storage call and no DB change;
  * a metadata-only edit never validates or stores anything;
  * the stored original is still readable by the unchanged AI Face path;
  * another school's student cannot receive a photo.

Storage is a recording mock; AI Face's Storage fetch is mocked to return the
recorded bytes. No network, no files written.
"""
import hashlib
import io
import struct
import unittest
import zlib
from datetime import date
from unittest import mock
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFilter

from app import create_app
from app.models import (db, AcademicYear, AuditLog, Grade, Role, School, Section,
                        Student, User)
from app.utils import helpers
from app.utils.student_photo import (MSG_INVALID, MSG_TOO_LARGE,
                                     STUDENT_PHOTO_MAX_PIXELS, validate_student_photo)

PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True}
FAKE_URL = 'https://storage.test/storage/v1/object/public/uploads/students/stored-object'
MP4 = b'\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom' + b'\x00' * 64
PDF = b'%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n' + b'0' * 800 + b'\n%%EOF\n'


# ── synthetic images (no production files) ───────────────────────────────────

def _enc(img, fmt, **kw):
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def _photo(w, h):
    r = Image.linear_gradient('L').resize((w, h))
    g = Image.radial_gradient('L').resize((w, h))
    img = Image.merge('RGB', (r, g, r.transpose(Image.Transpose.FLIP_LEFT_RIGHT)))
    ImageDraw.Draw(img).ellipse((w // 4, h // 4, w // 2, h // 2), fill=(200, 60, 60))
    return img.filter(ImageFilter.GaussianBlur(2))


def _phone_jpeg():
    """Phone-sized JPEG with EXIF orientation + camera tags (kept verbatim)."""
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x010F] = 'SecretCam'
    return _enc(_photo(4032, 3024), 'JPEG', quality=90, exif=exif.tobytes())


def _png_header_only(w, h):
    """A PNG declaring w x h pixels with a tiny body (a decompression-bomb shape)."""
    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF))
    ihdr = struct.pack('>IIBBBBB', w, h, 8, 0, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', zlib.compress(b'\x00' * 64)) + chunk(b'IEND', b''))


def _animated_gif():
    a, b = _photo(200, 150), _photo(200, 150).rotate(180)
    return _enc(a.convert('P'), 'GIF', save_all=True,
                append_images=[b.convert('P')], duration=100, loop=0)


def _valid_uploads():
    return [
        ('phone jpeg', 'photo.jpg', _phone_jpeg()),
        ('jpeg ext', 'photo.jpeg', _enc(_photo(800, 600), 'JPEG', quality=85)),
        ('png', 'photo.png', _enc(_photo(800, 600), 'PNG')),
        ('webp', 'photo.webp', _enc(_photo(1200, 900), 'WEBP', quality=90)),
        ('gif', 'photo.gif', _enc(_photo(300, 200).convert('P'), 'GIF')),
        ('animated gif', 'anim.gif', _animated_gif()),
    ]


def _invalid_uploads():
    good = _enc(_photo(1200, 900), 'JPEG', quality=90)
    png = _enc(_photo(600, 400), 'PNG')
    return [
        ('text renamed jpg', 'x.jpg', b'hello, this is not an image' * 50, MSG_INVALID),
        ('pdf renamed jpg', 'x.jpg', PDF, MSG_INVALID),
        ('truncated jpeg', 'x.jpg', good[: len(good) // 2], MSG_INVALID),
        ('jpeg header then junk', 'x.jpg', good[:600] + b'\x13\x37' * 3000, MSG_INVALID),
        ('truncated png', 'x.png', png[: len(png) // 2], MSG_INVALID),
        ('video renamed jpg', 'x.jpg', MP4, MSG_INVALID),
        ('bmp renamed jpg', 'x.jpg', _enc(_photo(100, 80), 'BMP'), MSG_INVALID),
        ('empty file', 'x.jpg', b'', MSG_INVALID),
        ('45 MP png', 'x.png', _enc(Image.new('L', (9000, 5000)), 'PNG'), MSG_TOO_LARGE),
        ('900 MP header', 'x.png', _png_header_only(30000, 30000), MSG_TOO_LARGE),
    ]


class _FS:
    """Minimal FileStorage stand-in (filename + stream), like werkzeug's."""
    def __init__(self, raw, name):
        self.filename, self.stream = name, io.BytesIO(raw)


# ─────────────────────────────────────────────────────────────────────────────
#  Helper level
# ─────────────────────────────────────────────────────────────────────────────

class StudentPhotoValidatorTest(unittest.TestCase):

    def test_valid_images_accepted_and_stream_rewound(self):
        for label, name, raw in _valid_uploads():
            with self.subTest(label):
                f = _FS(raw, name)
                self.assertIsNone(validate_student_photo(f))
                self.assertEqual(f.stream.tell(), 0)
                self.assertEqual(f.stream.read(), raw)            # untouched

    def test_invalid_images_refused_and_stream_rewound(self):
        for label, name, raw, msg in _invalid_uploads():
            with self.subTest(label):
                f = _FS(raw, name)
                self.assertEqual(validate_student_photo(f), msg)
                self.assertEqual(f.stream.tell(), 0)

    def test_pixel_ceiling_and_pillow_globals_untouched(self):
        self.assertEqual(STUDENT_PHOTO_MAX_PIXELS, 40_000_000)
        before = Image.MAX_IMAGE_PIXELS
        validate_student_photo(_FS(_png_header_only(30000, 30000), 'x.png'))
        self.assertEqual(Image.MAX_IMAGE_PIXELS, before)

    def test_disallowed_or_missing_extension_left_to_existing_check(self):
        for name in ('photo.bmp', 'photo.svg', 'noext', ''):
            with self.subTest(name):
                self.assertIsNone(validate_student_photo(_FS(b'anything', name)))

    def test_real_image_under_other_allowed_extension_still_accepted(self):
        # Stored and synced today (AI Face sniffs content); not a new refusal.
        self.assertIsNone(validate_student_photo(_FS(_enc(_photo(300, 200), 'PNG'), 'p.jpg')))


# ─────────────────────────────────────────────────────────────────────────────
#  Route level
# ─────────────────────────────────────────────────────────────────────────────

class StudentPhotoRouteTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['RATELIMIT_ENABLED'] = False

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.storage = mock.patch('app.utils.helpers._supabase_upload',
                                  return_value=FAKE_URL).start()
        # create=True keeps this suite runnable against code without the
        # validator (negative control).
        self.validate = mock.patch('app.blueprints.students.validate_student_photo',
                                   wraps=validate_student_photo, create=True).start()
        self.addCleanup(mock.patch.stopall)
        self.ids = {}
        with self.app.app_context():
            role = Role.query.filter_by(name='school_admin').first()
            for key in ('a', 'b'):
                s = School(school_name=f'Photo {key} {self.sfx}', code=f'PH{key}{self.sfx}'[:20],
                           capacity=0, is_active=True)
                db.session.add(s)
                db.session.flush()
                y = AcademicYear(school_id=s.id, name=f'Y{key}{self.sfx}', is_current=True,
                                 start_date=date(2026, 8, 1), end_date=date(2027, 6, 30))
                db.session.add(y)
                db.session.flush()
                g = Grade(name=f'G{key}', school_id=s.id, academic_year_id=y.id)
                db.session.add(g)
                db.session.flush()
                sec = Section(name=f'{key}1', grade_id=g.id, school_id=s.id,
                              academic_year_id=y.id)
                db.session.add(sec)
                db.session.flush()
                u = User(username=f'ph{key}_{self.sfx}', email=f'ph{key}_{self.sfx}@t.test',
                         full_name=f'admin {key}', role_id=role.id, school_id=s.id,
                         is_active=True)
                u.set_password(PASSWORD)
                st = Student(student_id=f'PH{key}-{self.sfx}', full_name=f'Existing {key}',
                             school_id=s.id, academic_year_id=y.id, section_id=sec.id,
                             status='active',
                             photo=f'https://storage.test/storage/v1/object/public/'
                                   f'uploads/students/old-{key}.jpg')
                db.session.add_all([u, st])
                db.session.flush()
                self.ids.update({f'school_{key}': s.id, f'sec_{key}': sec.id,
                                 f'student_{key}': st.id, f'admin_{key}': u.username})
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for key in ('a', 'b'):
                sid = self.ids[f'school_{key}']
                uids = [u.id for u in User.query.execution_options(**OPTS)
                        .filter_by(school_id=sid).all()]
                if uids:
                    AuditLog.query.execution_options(**OPTS).filter(
                        AuditLog.user_id.in_(uids)).delete(synchronize_session=False)
                for model in (AuditLog, Student, Section, Grade, User, AcademicYear):
                    model.query.execution_options(**OPTS).filter_by(
                        school_id=sid).delete(synchronize_session=False)
                School.query.filter_by(id=sid).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _client(self, key='a'):
        client = self.app.test_client()
        resp = client.post('/auth/login', data={'username': self.ids[f'admin_{key}'],
                                                'password': PASSWORD})
        self.assertEqual(resp.status_code, 302)
        return client

    def _create(self, client, name, raw, full_name):
        return client.post('/students/create', content_type='multipart/form-data', data={
            'full_name': full_name, 'section_id': str(self.ids['sec_a']),
            'gender': 'male', 'date_of_birth': '2015-01-01',
            'photo': (io.BytesIO(raw), name, 'image/jpeg')})

    def _edit(self, client, student_key, full_name, photo=None):
        data = {'full_name': full_name, 'section_id': str(self.ids[f'sec_{student_key[-1]}']),
                'status': 'active'}
        if photo is not None:
            name, raw = photo
            data['photo'] = (io.BytesIO(raw), name, 'image/jpeg')
        return client.post(f"/students/{self.ids[student_key]}/edit",
                           content_type='multipart/form-data', data=data)

    def _students(self):
        with self.app.app_context():
            return sorted((s.id, s.full_name, s.photo) for s in
                          Student.query.execution_options(**OPTS).filter(
                              Student.school_id.in_([self.ids['school_a'],
                                                     self.ids['school_b']])).all())

    def _student(self, key):
        with self.app.app_context():
            return db.session.get(Student, self.ids[key], execution_options=OPTS)

    def _assert_stored_verbatim(self, name, raw):
        # The first write is the original, byte-identical. A new upload may add
        # ONE separate display-only copy (students/display/*.webp); the original
        # itself is never replaced by it.
        calls = self.storage.call_args_list
        self.assertIn(len(calls), (1, 2), calls)
        data, path, ctype = calls[0].args
        self.assertEqual(hashlib.sha256(data).hexdigest(), hashlib.sha256(raw).hexdigest())
        ext = name.rsplit('.', 1)[1]
        self.assertTrue(path.startswith('students/') and path.endswith(f'.{ext}'), path)
        self.assertEqual(ctype, helpers._CONTENT_TYPES[ext])       # unchanged mapping
        self.assertEqual(calls[0].kwargs, {'bucket': None})
        for extra in calls[1:]:
            self.assertTrue(extra.args[1].startswith('students/display/')
                            and extra.args[1].endswith('.webp'), extra.args[1])
        return data

    # ── 1-7: valid uploads stored byte-identical ─────────────────────────────

    def test_create_accepts_valid_images_byte_identical(self):
        client = self._client()
        for label, name, raw in _valid_uploads():
            with self.subTest(label):
                self.storage.reset_mock()
                full_name = f'New {label} {self.sfx}'
                resp = self._create(client, name, raw, full_name)
                self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True)[-600:])
                self.assertIn('create', resp.headers['Location'])
                self._assert_stored_verbatim(name, raw)
                with self.app.app_context():
                    (st,) = Student.query.execution_options(**OPTS).filter_by(
                        full_name=full_name).all()
                    self.assertEqual(st.photo, FAKE_URL)

    # ── 8-14: invalid uploads refused before Storage / DB ────────────────────

    def test_create_refuses_invalid_images(self):
        client = self._client()
        for label, name, raw, msg in _invalid_uploads():
            with self.subTest(label):
                before = self._students()
                resp = self._create(client, name, raw, f'Bad {label} {self.sfx}')
                self.assertEqual(resp.status_code, 200)
                html = resp.get_data(as_text=True)
                self.assertIn(msg, html)
                self.assertNotIn('Traceback', html)
                self.assertEqual(self._students(), before, 'a student row was written')
                self.storage.assert_not_called()

    def test_edit_refuses_invalid_images_row_untouched(self):
        client = self._client()
        for label, name, raw, msg in _invalid_uploads():
            with self.subTest(label):
                before = self._students()
                resp = self._edit(client, 'student_a', 'Renamed', photo=(name, raw))
                self.assertEqual(resp.status_code, 302)
                self.assertIn(f"/students/{self.ids['student_a']}/edit", resp.headers['Location'])
                page = client.get(resp.headers['Location']).get_data(as_text=True)
                self.assertIn(msg, page)
                self.assertEqual(self._students(), before, 'student changed on refusal')
                self.storage.assert_not_called()

    # ── 15, 16: metadata-only edit and replacement ───────────────────────────

    def test_metadata_only_edit_touches_no_photo(self):
        old = self._student('student_a').photo
        resp = self._edit(self._client(), 'student_a', 'Meta only')
        self.assertEqual(resp.status_code, 302)
        st = self._student('student_a')
        self.assertEqual((st.full_name, st.photo), ('Meta only', old))
        self.validate.assert_not_called()
        self.storage.assert_not_called()

    def test_replacement_stores_original_bytes(self):
        raw = _phone_jpeg()
        resp = self._edit(self._client(), 'student_a', 'Replaced', photo=('new.jpg', raw))
        self.assertEqual(resp.status_code, 302)
        self._assert_stored_verbatim('new.jpg', raw)
        st = self._student('student_a')
        self.assertEqual((st.full_name, st.photo), ('Replaced', FAKE_URL))
        self.validate.assert_called_once()

    # ── 17: AI Face reads the stored original through its unchanged path ─────

    def test_aiface_prepares_the_stored_original(self):
        from app.services.aiface_sync import prepare_photo_for_device
        for label, name, raw in (('phone jpeg', 'p.jpg', _phone_jpeg()),
                                 ('animated gif', 'a.gif', _animated_gif())):
            with self.subTest(label):
                self.storage.reset_mock()
                resp = self._edit(self._client(), 'student_a', f'AI {label}', photo=(name, raw))
                self.assertEqual(resp.status_code, 302)
                stored = self._assert_stored_verbatim(name, raw)
                photo = self._student('student_a').photo
                with self.app.app_context(), mock.patch(
                        'app.utils.helpers._supabase_fetch',
                        return_value=(stored, helpers._CONTENT_TYPES[name.rsplit('.', 1)[1]])) as fetch:
                    jpeg, info = prepare_photo_for_device(photo, label='t')
                fetch.assert_called_once_with('students/stored-object', bucket='uploads')
                self.assertIsNotNone(jpeg, info)
                self.assertEqual(info['source'], 'supabase')
                out = Image.open(io.BytesIO(jpeg))
                self.assertEqual(out.format, 'JPEG')
                self.assertLessEqual(max(out.size), 640)

    # ── 18: cross-school ─────────────────────────────────────────────────────

    def test_other_school_student_cannot_receive_a_photo(self):
        before = self._students()
        resp = self._edit(self._client('a'), 'student_b', 'Hijack',
                          photo=('p.jpg', _enc(_photo(400, 300), 'JPEG')))
        self.assertIn(resp.status_code, (403, 404))
        self.assertEqual(self._students(), before)
        self.storage.assert_not_called()

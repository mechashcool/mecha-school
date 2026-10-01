"""
Employee display-photo derivative (employees.photo_display) — targeted tests.

Employee.photo stays the byte-identical original and the only AI Face source.
A NEW / replacement upload additionally stores a display-only WebP copy
(192 x 192 centred square, q45/m6, EXIF/GPS/XMP stripped, ICC → sRGB) under
employees/display/ — the SAME shared encoder as the Student copy; any failure
leaves photo_display NULL and never fails the employee operation. Every display
consumer (web list/search/detail/edit/attendance report, mobile login/profile,
chat contacts) prefers the copy and falls back to Employee.photo. Existing
employees (photo_display NULL) render exactly as before; nothing reads,
rewrites or backfills them.

Storage is a recording mock; fetch/sign/delete must stay unused. No network,
no production data, no files written outside a temp folder.
"""
import hashlib
import io
import pathlib
import re
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFilter
from sqlalchemy import inspect as sa_inspect, text

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (db, AcademicYear, AttendanceDevice, AuditLog,
                        DeviceEmployeeMapping, Employee, EmployeeDocument, Grade, Role,
                        School, Section, Student, User, parent_students)
from app.utils import employee_display_photo as edp
from app.utils import helpers
from app.utils.employee_display_photo import (EMPLOYEE_DISPLAY_MAX_SIDE,
                                              EMPLOYEE_DISPLAY_SUBFOLDER,
                                              EMPLOYEE_DISPLAY_WEBP_METHOD,
                                              EMPLOYEE_DISPLAY_WEBP_QUALITY,
                                              make_employee_display_photo)
from app.utils.employee_photo import (EMPLOYEE_PHOTO_MAX_BYTES, MSG_INVALID,
                                      MSG_TOO_BIG, MSG_TOO_LARGE)

PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True}
BASE = 'https://storage.test/storage/v1/object/public/uploads/'
ROOT = pathlib.Path(__file__).resolve().parent.parent


def _url_for_path(data, path, ctype, bucket=None):
    return f'{BASE}{path}'


# ── synthetic employee-style photos (no production files) ────────────────────

def _enc(img, fmt, **kw):
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def _portrait(w, h):
    img = Image.new('RGB', (w, h), (205, 190, 170))
    d = ImageDraw.Draw(img)
    d.ellipse((w // 4, h // 6, 3 * w // 4, h // 2), fill=(225, 185, 150))      # face
    d.rectangle((w // 6, h // 2, 5 * w // 6, h), fill=(30, 60, 120))            # clothing
    d.rectangle((0, 0, w // 10, h // 10), fill=(250, 20, 20))                    # red marker, top-left
    noise = Image.effect_noise((w, h), 25).convert('RGB')
    return Image.blend(img.filter(ImageFilter.GaussianBlur(2)), noise, 0.12)


def _phone_jpeg(w=3024, h=4032, orientation=1, quality=80):
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = 'SecretCam'
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = 'N', (33.0, 18.0, 0.0), 'E', (44.0, 22.0, 0.0)
    xmp = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf='
           b'"http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description '
           b'SecretXmp="1"/></rdf:RDF></x:xmpmeta>')
    return _enc(_portrait(w, h), 'JPEG', quality=quality, exif=exif.tobytes(), xmp=xmp)


def _transparent_png(w=1400, h=1400):
    img = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((w // 8, h // 8, 7 * w // 8, 7 * h // 8), fill=(40, 90, 160, 255))
    return _enc(img, 'PNG')


def _two_frame(fmt, first=(220, 30, 30), second=(30, 30, 220), size=(300, 400)):
    a, b = Image.new('RGB', size, first), Image.new('RGB', size, second)
    if fmt == 'GIF':
        a, b = a.convert('P'), b.convert('P')
    return _enc(a, fmt, save_all=True, append_images=[b], duration=100, loop=0)


def _decode(data):
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def _mean_diff(a, b):
    a, b = a.convert('RGB'), b.convert('RGB').resize(a.size)
    pa, pb = a.tobytes(), b.tobytes()
    return sum(abs(x - y) for x, y in zip(pa, pb)) / len(pa)


# ─────────────────────────────────────────────────────────────────────────────
#  Derivative policy (helper level) — 10-14
# ─────────────────────────────────────────────────────────────────────────────

class EmployeeDisplayPolicyTest(unittest.TestCase):

    def test_policy_constants(self):
        self.assertEqual((EMPLOYEE_DISPLAY_MAX_SIDE, EMPLOYEE_DISPLAY_WEBP_QUALITY,
                          EMPLOYEE_DISPLAY_WEBP_METHOD, EMPLOYEE_DISPLAY_SUBFOLDER),
                         (192, 45, 6, 'employees/display'))

    def test_10_11_square_192_and_no_upscale(self):
        for (w, h) in ((4032, 3024), (3024, 4032), (2000, 900)):
            out = _decode(make_employee_display_photo(_enc(_portrait(w, h), 'JPEG')))
            self.assertEqual((out.format, out.size), ('WEBP', (192, 192)), (w, h))
        for (w, h, side) in ((600, 800, 192), (1024, 700, 192), (40, 30, 30), (150, 120, 120)):
            out = _decode(make_employee_display_photo(_enc(_portrait(w, h), 'PNG')))
            self.assertEqual(out.size, (side, side))                        # never upscaled

    def test_12_webp_quality_45_method_6(self):
        from PIL import ImageOps
        raw = _enc(_portrait(1600, 1200), 'JPEG', quality=92)
        ref = Image.open(io.BytesIO(raw))
        ref.draft('RGB', (384, 384))
        ref = ImageOps.fit(ref.convert('RGB'), (192, 192), Image.Resampling.LANCZOS,
                           centering=(0.5, 0.5))
        expect = {(q, m): _enc(ref, 'WEBP', quality=q, method=m)
                  for (q, m) in ((45, 6), (50, 6), (40, 6), (45, 4), (80, 4))}
        got = make_employee_display_photo(raw)
        self.assertEqual(got, expect[(45, 6)])
        for other in ((50, 6), (40, 6), (45, 4), (80, 4)):
            self.assertNotEqual(got, expect[other], other)

    def test_13_exif_orientation_applied_like_pillow(self):
        from PIL import ImageOps
        for orientation in range(1, 9):
            with self.subTest(orientation=orientation):
                raw = _phone_jpeg(1600, 1200, orientation=orientation)
                out = _decode(make_employee_display_photo(raw))
                ref = ImageOps.fit(ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert('RGB'),
                                   (192, 192), Image.Resampling.LANCZOS)
                self.assertEqual(out.size, (192, 192))
                self.assertLess(_mean_diff(out, ref), 6.0)                  # same picture/orientation

    def test_14_15_metadata_stripped_original_untouched(self):
        raw = _phone_jpeg(3000, 2250, orientation=6)
        before = hashlib.sha256(raw).hexdigest()
        data = make_employee_display_photo(raw)
        out = _decode(data)
        self.assertEqual(len(out.getexif()), 0)
        for key in ('exif', 'xmp', 'XML:com.adobe.xmp'):
            self.assertNotIn(key, out.info)
        for marker in (b'SecretCam', b'SecretXmp', b'Exif', b'EXIF', b'XMP ', b'xmpmeta'):
            self.assertNotIn(marker, data)
        # 15: the original bytes / metadata are what they were
        self.assertEqual(hashlib.sha256(raw).hexdigest(), before)
        src = Image.open(io.BytesIO(raw))
        self.assertEqual(src.getexif()[0x010F], 'SecretCam')
        self.assertEqual(dict(src.getexif().get_ifd(0x8825))[1], 'N')
        self.assertIn(b'SecretXmp', raw)

    def test_transparency_and_icc(self):
        alpha = _decode(make_employee_display_photo(_transparent_png()))
        self.assertEqual((alpha.mode, alpha.size), ('RGBA', (192, 192)))
        self.assertLess(alpha.getpixel((2, 2))[3], 10)
        opaque = _decode(make_employee_display_photo(_enc(_portrait(800, 600), 'PNG')))
        self.assertEqual(opaque.mode, 'RGB')
        from PIL import ImageCms
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        raw = _enc(_portrait(800, 600), 'JPEG', icc_profile=icc)
        data = make_employee_display_photo(raw)
        self.assertNotIn('icc_profile', _decode(data).info)                 # ICC converted + dropped
        self.assertNotIn(b'ICCP', data)
        self.assertEqual(Image.open(io.BytesIO(raw)).info.get('icc_profile'), icc)  # original keeps it

    def test_animated_first_frame_only(self):
        for fmt in ('GIF', 'WEBP'):
            with self.subTest(fmt):
                out = _decode(make_employee_display_photo(_two_frame(fmt)))
                self.assertEqual((out.format, getattr(out, 'n_frames', 1)), ('WEBP', 1))
                r, g, b = out.convert('RGB').getpixel((96, 96))
                self.assertGreater(r, 150)                                  # red = frame 0
                self.assertLess(b, 100)

    def test_prepare_never_raises_and_rewinds(self):
        class _FS:
            def __init__(self, raw):
                self.stream = io.BytesIO(raw)
        good = _FS(_enc(_portrait(300, 400), 'JPEG'))
        good.stream.seek(0, 2)                           # as left by save_uploaded_file
        end = good.stream.tell()
        self.assertIsNotNone(edp.prepare_employee_display_photo(good))
        self.assertEqual(good.stream.tell(), end)
        self.assertIsNone(edp.prepare_employee_display_photo(_FS(b'not an image')))
        self.assertIsNone(edp.prepare_employee_display_photo(None))
        self.assertIsNone(edp.save_employee_display_photo(None))
        self.assertIsNone(edp.save_employee_display_photo(b''))


# ─────────────────────────────────────────────────────────────────────────────
#  Fallback resolver — 21-25 (local checks only, no I/O)
# ─────────────────────────────────────────────────────────────────────────────

class EmployeeDisplayFallbackTest(unittest.TestCase):

    ORIG = BASE + 'employees/orig-fallback.jpg'

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    def setUp(self):
        boom = AssertionError('network/Storage used to decide the display fallback')
        for target in ('app.utils.helpers._supabase_fetch', 'app.utils.helpers._supabase_sign',
                       'app.utils.helpers._supabase_upload', 'requests.sessions.Session.request'):
            mock.patch(target, side_effect=boom).start()
        self.addCleanup(mock.patch.stopall)

    def _resolve(self, display, root=None):
        emp = SimpleNamespace(photo=self.ORIG, photo_display=display)
        out = {}
        for flag in (False, True):
            with mock.patch.dict(self.app.config, {'PRIVATE_UPLOADS_ENABLED': flag}), \
                    self.app.test_request_context('/'), \
                    mock.patch.object(self.app, 'root_path', root or self.app.root_path):
                out[flag] = (edp.employee_display_value(emp), edp.employee_photo_url(emp))
        return out

    def _assert_original(self, display, root=None):
        for flag, (value, url) in self._resolve(display, root).items():
            self.assertEqual(value, self.ORIG, (display, flag))
            self.assertIn('orig-fallback.jpg', url, (display, flag))

    def test_21_22_null_empty_whitespace(self):
        for display in (None, '', '   ', '\n'):
            with self.subTest(display=display):
                self._assert_original(display)
        self.assertIsNone(edp.employee_display_value(None))

    def test_23_24_missing_or_unsafe_local_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A real file OUTSIDE static/uploads that traversal values point at.
            (pathlib.Path(tmp) / 'secret.webp').write_bytes(b'x')
            (pathlib.Path(tmp) / 'static').mkdir()
            gone = f'gone-{uuid4().hex}.webp'
            for display in (f'uploads/employees/display/{gone}',
                            f'/static/uploads/employees/display/{gone}',
                            gone, '../secret.webp', 'uploads/../../secret.webp',
                            'uploads/employees/display/../../../secret.webp',
                            'uploads\\..\\..\\secret.webp', 'C:/secret.webp',
                            'file:///etc/passwd', 'javascript:alert(1)', 'uploads/',
                            'static/secret.webp', 'uploads/./x.webp'):
                with self.subTest(display=display):
                    self._assert_original(display, root=tmp)

    def test_existing_local_display_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            rel = f'uploads/employees/display/kept-{uuid4().hex}.webp'
            path = pathlib.Path(tmp, 'static', *rel.split('/'))
            path.parent.mkdir(parents=True)
            path.write_bytes(b'webp')
            for flag, (value, _url) in self._resolve(rel, root=tmp).items():
                self.assertEqual(value, rel, flag)

    def test_25_remote_display_used_without_network(self):
        display = BASE + 'employees/display/remote-ok.webp'
        for flag, (value, url) in self._resolve(display).items():
            self.assertEqual(value, display, flag)
            self.assertIn('remote-ok.webp', url, flag)


# ─────────────────────────────────────────────────────────────────────────────
#  Routes, serializers, AI Face, ownership, failure safety
# ─────────────────────────────────────────────────────────────────────────────

class EmployeeDisplayPhotoRouteTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['RATELIMIT_ENABLED'] = False
        with cls.app.app_context():
            cls.role_ids = {n: Role.query.filter_by(name=n).first().id
                            for n in ('school_admin', 'parent', 'teacher')}

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.storage = mock.patch('app.utils.helpers._supabase_upload',
                                  side_effect=_url_for_path).start()
        self.fetch = mock.patch('app.utils.helpers._supabase_fetch',
                                return_value=(None, None)).start()
        self.delete = mock.patch('app.utils.helpers._supabase_delete').start()
        # create=True keeps this suite runnable against code without the
        # derivative wiring (negative control).
        self.prepare = mock.patch('app.blueprints.employees.prepare_employee_display_photo',
                                  wraps=edp.prepare_employee_display_photo, create=True).start()
        self.addCleanup(mock.patch.stopall)
        self.ids = {}
        with self.app.app_context():
            for key in ('a', 'b'):
                self._school(key)
            db.session.commit()

    def _school(self, key):
        s = self.sfx
        school = School(school_name=f'EDisp {key} {s}', code=f'ED{key}{s}'[:20],
                        capacity=0, is_active=True)
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'Y{key}{s}', is_current=True,
                            start_date=date(2026, 8, 1), end_date=date(2027, 6, 30))
        db.session.add(year)
        db.session.flush()
        grade = Grade(name=f'G{key}', school_id=school.id, academic_year_id=year.id)
        db.session.add(grade)
        db.session.flush()
        sec = Section(name=f'{key}1', grade_id=grade.id, school_id=school.id,
                      academic_year_id=year.id)
        db.session.add(sec)
        db.session.flush()

        def user(label, role):
            u = User(username=f'ed{label}{key}_{s}', email=f'ed{label}{key}_{s}@t.test',
                     full_name=f'{label} {key}', role_id=self.role_ids[role],
                     school_id=school.id, is_active=True)
            u.set_password(PASSWORD)
            db.session.add(u)
            db.session.flush()
            return u

        admin, parent = user('adm', 'school_admin'), user('par', 'parent')
        teacher, other_teacher = user('t', 'teacher'), user('o', 'teacher')
        # derived: has a display copy; legacy: old employee, photo_display NULL
        derived = Employee(school_id=school.id, employee_id=f'ED{key}{s}', full_name=f'Derived {key}',
                           base_salary=0, status='active', user_id=teacher.id,
                           photo=f'{BASE}employees/orig-{key}-{s}.jpg',
                           photo_display=f'{BASE}employees/display/disp-{key}-{s}.webp')
        legacy = Employee(school_id=school.id, employee_id=f'EL{key}{s}', full_name=f'Legacy {key}',
                          base_salary=0, status='active', user_id=other_teacher.id,
                          photo=f'{BASE}employees/old-{key}-{s}.jpg')
        db.session.add_all([derived, legacy])
        db.session.flush()
        db.session.execute(Section.__table__.update().where(Section.id == sec.id)
                           .values(teacher_id=derived.id))
        child = Student(student_id=f'EC{key}-{s}', full_name=f'Child {key}', school_id=school.id,
                        academic_year_id=year.id, section_id=sec.id, status='active')
        db.session.add(child)
        db.session.flush()
        db.session.execute(parent_students.insert().values(
            user_id=parent.id, student_id=child.id, relation='guardian'))
        self.ids.update({f'school_{key}': school.id, f'derived_{key}': derived.id,
                         f'legacy_{key}': legacy.id, f'admin_{key}': admin.username,
                         f'admin_id_{key}': admin.id, f'parent_id_{key}': parent.id,
                         f'teacher_{key}': teacher.username, f'teacher_id_{key}': teacher.id,
                         f'other_teacher_id_{key}': other_teacher.id})

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
                    db.session.execute(parent_students.delete().where(
                        parent_students.c.user_id.in_(uids)))
                db.session.execute(Section.__table__.update()
                                   .where(Section.school_id == sid).values(teacher_id=None))
                for model in (DeviceEmployeeMapping, AttendanceDevice, AuditLog, EmployeeDocument,
                              Student, Section, Grade, Employee, User, AcademicYear):
                    model.query.execution_options(**OPTS).filter_by(
                        school_id=sid).delete(synchronize_session=False)
                School.query.filter_by(id=sid).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _web(self, key):
        client = self.app.test_client()
        resp = client.post('/auth/login', data={'username': self.ids[key], 'password': PASSWORD})
        self.assertEqual(resp.status_code, 302)
        return client

    def _jwt(self, user_id):
        with self.app.app_context():
            return {'Authorization': 'Bearer ' + encode_token(
                db.session.get(User, user_id, execution_options=OPTS))}

    def _row(self, key):
        with self.app.app_context():
            e = db.session.get(Employee, self.ids[key], execution_options=OPTS)
            return e.photo, e.photo_display

    def _all_rows(self):
        with self.app.app_context():
            return sorted((e.id, e.full_name, e.photo, e.photo_display, e.updated_at)
                          for e in Employee.query.execution_options(**OPTS).filter(
                              Employee.school_id.in_([self.ids['school_a'],
                                                      self.ids['school_b']])).all())

    def _create(self, client, raw, name='photo.jpg', full_name=None, extra=None):
        full_name = full_name or f'New {uuid4().hex[:6]}'
        data = {'full_name': full_name, 'gender': 'male',
                'photo': (io.BytesIO(raw), name, 'image/jpeg')}
        data.update(extra or {})
        resp = client.post('/employees/create', content_type='multipart/form-data', data=data)
        with self.app.app_context():
            e = Employee.query.execution_options(**OPTS).filter_by(full_name=full_name).first()
            return resp, (e.photo, e.photo_display) if e else None

    def _edit(self, client, key, full_name, photo=None):
        data = {'full_name': full_name, 'status': 'active', 'gender': 'male'}
        if photo is not None:
            data['photo'] = (io.BytesIO(photo[1]), photo[0], 'image/jpeg')
        return client.post(f"/employees/{self.ids[key]}/edit",
                           content_type='multipart/form-data', data=data)

    def _uploads(self):
        """[(sha256, object_path, content_type)] of every Storage write."""
        return [(hashlib.sha256(c.args[0]).hexdigest(), c.args[1], c.args[2])
                for c in self.storage.call_args_list]

    def _assert_pair(self, raw, name, stored):
        """Original stored verbatim + one display WebP; row points at both."""
        ups = self._uploads()
        self.assertEqual(len(ups), 2, ups)
        (osha, opath, octype), (dsha, dpath, dctype) = ups
        ext = name.rsplit('.', 1)[1]
        self.assertEqual(osha, hashlib.sha256(raw).hexdigest())            # original verbatim
        self.assertRegex(opath, r'^employees/[0-9a-f]{32}\.' + ext + '$')
        self.assertEqual(octype, helpers._CONTENT_TYPES[ext])
        self.assertRegex(dpath, r'^employees/display/[0-9a-f]{32}\.webp$')
        self.assertEqual(dctype, 'image/webp')
        self.assertEqual(stored, (BASE + opath, BASE + dpath))
        disp = _decode(self.storage.call_args_list[1].args[0])
        self.assertEqual(disp.format, 'WEBP')
        self.assertEqual(disp.size, (192, 192))
        return disp

    # ── 1-4: create stores the original verbatim + a display copy ────────────

    def test_01_04_create_stores_original_and_display(self):
        client = self._web('admin_a')
        cases = [('jpeg', 'photo.jpg', _phone_jpeg(orientation=6)),
                 ('png', 'photo.png', _enc(_portrait(900, 1200), 'PNG')),
                 ('webp', 'photo.webp', _enc(_portrait(1200, 1600), 'WEBP', quality=90)),
                 ('gif', 'photo.gif', _enc(_portrait(300, 400).convert('P'), 'GIF')),
                 ('animated gif', 'anim.gif', _two_frame('GIF')),
                 ('animated webp', 'anim.webp', _two_frame('WEBP'))]
        for label, name, raw in cases:
            with self.subTest(label):
                self.storage.reset_mock()
                self.assertLessEqual(len(raw), EMPLOYEE_PHOTO_MAX_BYTES)
                resp, stored = self._create(client, raw, name)
                self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True)[-600:])
                disp = self._assert_pair(raw, name, stored)
                if label.startswith('animated'):
                    self.assertEqual(getattr(disp, 'n_frames', 1), 1)
                    self.assertGreater(disp.convert('RGB').getpixel((96, 96))[0], 150)
                    stored_orig = self.storage.call_args_list[0].args[0]
                    self.assertEqual(Image.open(io.BytesIO(stored_orig)).n_frames, 2)
                if label == 'jpeg':
                    self.assertEqual(disp.size, (192, 192))                  # 3024x4032 + orientation 6
                    self.assertIn(b'SecretCam', self.storage.call_args_list[0].args[0])
        self.fetch.assert_not_called()
        self.delete.assert_not_called()

    # ── 5-9: existing hardening preserved ─────────────────────────────────────

    def test_05_exact_limit_accepted(self):
        base = _enc(_portrait(400, 300), 'JPEG')
        at = base + b'\x00' * (EMPLOYEE_PHOTO_MAX_BYTES - len(base))
        resp, stored = self._create(self._web('admin_a'), at)
        self.assertEqual(resp.status_code, 302)
        self._assert_pair(at, 'photo.jpg', stored)

    def test_06_09_invalid_rejected_before_storage(self):
        import random
        big = _enc(Image.frombytes('RGB', (1000, 1000), random.Random(3).randbytes(3_000_000)), 'PNG')
        good = _enc(_portrait(900, 700), 'JPEG')
        cases = [('over 2 MB', 'x.png', big, MSG_TOO_BIG),
                 ('fake jpg', 'x.jpg', b'plain text, not a picture' * 40, MSG_INVALID),
                 ('corrupt jpeg', 'x.jpg', good[:600] + b'\x13\x37' * 3000, MSG_INVALID),
                 ('45 MP', 'x.png', _enc(Image.new('L', (9000, 5000)), 'PNG'), MSG_TOO_LARGE)]
        client = self._web('admin_a')
        for label, name, raw, msg in cases:
            with self.subTest(label):
                before = self._all_rows()
                resp, stored = self._create(client, raw, name)
                self.assertEqual(resp.status_code, 200)
                self.assertIn(msg, resp.get_data(as_text=True))
                self.assertIsNone(stored)
                self.assertEqual(self._all_rows(), before)
                resp = self._edit(client, 'derived_a', 'Nope', photo=(name, raw))
                self.assertIn(msg, resp.get_data(as_text=True))
                self.assertEqual(self._all_rows(), before)
        self.storage.assert_not_called()
        self.prepare.assert_not_called()

    # ── 16, 17: derivative failures never fail the employee operation ─────────

    def test_16_generation_failure_original_kept_display_null(self):
        raw = _enc(_portrait(900, 1200), 'JPEG')
        with mock.patch('app.utils.employee_display_photo.make_employee_display_photo',
                        side_effect=RuntimeError('encoder down')):
            resp, stored = self._create(self._web('admin_a'), raw)
        self.assertEqual(resp.status_code, 302)
        (osha, opath, _), = self._uploads()
        self.assertEqual(osha, hashlib.sha256(raw).hexdigest())
        self.assertEqual(stored, (BASE + opath, None))

    def test_17_display_storage_failure_original_kept_display_null(self):
        real = helpers.save_uploaded_file
        raw = _enc(_portrait(900, 1200), 'JPEG')
        for label, failure in (('returns None', None), ('raises', OSError('bucket down'))):
            with self.subTest(label):
                self.storage.reset_mock()

                def fake(file, subfolder='misc', *a, **kw):
                    if subfolder == EMPLOYEE_DISPLAY_SUBFOLDER:
                        if failure is not None:
                            raise failure
                        return None
                    return real(file, subfolder, *a, **kw)
                with mock.patch('app.utils.helpers.save_uploaded_file', side_effect=fake):
                    resp, stored = self._create(self._web('admin_a'), raw)
                self.assertEqual(resp.status_code, 302)
                (osha, opath, _), = self._uploads()
                self.assertEqual(osha, hashlib.sha256(raw).hexdigest())
                self.assertEqual(stored, (BASE + opath, None))

    # ── 18-20: replacement invariant + metadata-only edit ─────────────────────

    def test_18_replacement_gets_new_pair(self):
        old = self._row('derived_a')
        raw = _phone_jpeg(2000, 1500)
        resp = self._edit(self._web('admin_a'), 'derived_a', 'Replaced', photo=('n.jpg', raw))
        self.assertEqual(resp.status_code, 302)
        new = self._row('derived_a')
        self._assert_pair(raw, 'n.jpg', new)
        self.assertNotEqual(new[0], old[0])
        self.assertNotEqual(new[1], old[1])
        self.fetch.assert_not_called()                                      # old objects not read
        self.delete.assert_not_called()                                     # nor deleted

    def test_original_supabase_failure_no_local_fallback(self):
        from app.blueprints.employees import _MSG_PHOTO_REJECTED, _MSG_PHOTO_REPLACE_FAILED
        self.storage.side_effect = lambda *a, **k: None                     # Supabase down
        local = pathlib.Path(self.app.root_path, 'static', 'uploads', 'employees')
        before = sorted(local.rglob('*')) if local.exists() else []
        client = self._web('admin_a')
        # create: refused — no employee without its photo
        resp, row = self._create(client, _phone_jpeg(1200, 1600), full_name=f'NoStore {self.sfx}')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(_MSG_PHOTO_REJECTED, resp.get_data(as_text=True))
        self.assertIsNone(row)
        # edit: refused, current photo pair and name kept
        old = self._row('derived_a')
        with self.app.app_context():
            old_name = db.session.get(Employee, self.ids['derived_a'],
                                      execution_options=OPTS).full_name
        resp = self._edit(client, 'derived_a', 'Renamed NoStore',
                          photo=('n.jpg', _phone_jpeg(1200, 1600)))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(_MSG_PHOTO_REPLACE_FAILED, resp.get_data(as_text=True))
        self.assertEqual(self._row('derived_a'), old)
        with self.app.app_context():
            self.assertEqual(db.session.get(Employee, self.ids['derived_a'],
                                            execution_options=OPTS).full_name, old_name)
        self.assertEqual(self.storage.call_count, 2)                        # no display attempt
        self.assertEqual(sorted(local.rglob('*')) if local.exists() else [], before)

    def test_19_replacement_derivative_failure_clears_old_display(self):
        raw = _enc(_portrait(900, 1200), 'JPEG')
        for label, target in (('generation', 'make_employee_display_photo'),
                              ('storage', 'save_uploaded_file')):
            with self.subTest(label):
                with self.app.app_context():                                # restore an old pair
                    e = db.session.get(Employee, self.ids['derived_a'], execution_options=OPTS)
                    e.photo_display = f'{BASE}employees/display/stale-{self.sfx}.webp'
                    db.session.commit()
                self.storage.reset_mock()
                path = ('app.utils.employee_display_photo.' + target if label == 'generation'
                        else 'app.utils.helpers.save_uploaded_file')
                real = helpers.save_uploaded_file
                effect = (RuntimeError('x') if label == 'generation' else
                          (lambda f, sub='misc', *a, **kw: None if sub == EMPLOYEE_DISPLAY_SUBFOLDER
                           else real(f, sub, *a, **kw)))
                with mock.patch(path, side_effect=effect):
                    resp = self._edit(self._web('admin_a'), 'derived_a', 'R', photo=('n.jpg', raw))
                self.assertEqual(resp.status_code, 302)
                (osha, opath, _), = self._uploads()
                self.assertEqual(self._row('derived_a'), (BASE + opath, None))  # stale copy gone

    def test_20_metadata_only_edit_touches_neither_field(self):
        before = self._row('derived_a')
        resp = self._edit(self._web('admin_a'), 'derived_a', 'Meta only')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._row('derived_a'), before)
        self.storage.assert_not_called()
        self.prepare.assert_not_called()
        self.fetch.assert_not_called()

    # ── 21, 26-30: web consumers prefer the copy, legacy falls back ───────────

    def _names(self, key):
        orig, disp = self._row(key)
        return orig.rsplit('/', 1)[-1], (disp.rsplit('/', 1)[-1] if disp else None)

    def test_21_26_30_web_prefers_display_and_falls_back(self):
        d_orig, d_disp = self._names('derived_a')
        l_orig, l_disp = self._names('legacy_a')
        self.assertIsNone(l_disp)
        client = self._web('admin_a')
        html = client.get('/employees/').get_data(as_text=True)
        self.assertIn(d_disp, html)
        self.assertNotIn(d_orig, html)
        self.assertIn(l_orig, html)                                        # 21 fallback
        for name in (d_disp, l_orig):                                      # 26 list rows lazy
            self.assertRegex(html, r'<img src="[^"]*' + re.escape(name)
                             + r'[^"]*" loading="lazy" decoding="async"')
        self.assertIn('loading="lazy" decoding="async"', html.split('function avatarHtml', 1)[1])
        items = {i['id']: i['photo_url'] for i in
                 client.get('/employees/search').get_json()['items']}        # 27
        self.assertIn(d_disp, items[self.ids['derived_a']])
        self.assertIn(l_orig, items[self.ids['legacy_a']])
        for page in (f"/employees/{self.ids['derived_a']}",                   # 28
                     f"/employees/{self.ids['derived_a']}/edit",              # 29
                     f"/employees/attendance-report/{self.ids['derived_a']}"):  # 30
            with self.subTest(page):
                resp = client.get(page)
                self.assertEqual(resp.status_code, 200)
                body = resp.get_data(as_text=True)
                self.assertIn(d_disp, body)
                self.assertNotIn(d_orig, body)
        for page in (f"/employees/{self.ids['legacy_a']}",
                     f"/employees/attendance-report/{self.ids['legacy_a']}"):
            self.assertIn(l_orig, client.get(page).get_data(as_text=True), page)

    def test_30_attendance_report_uses_resolver_not_raw_value(self):
        # A relative value is resolved (like every other employee avatar) and a
        # missing local file shows the placeholder, never a broken raw src.
        with self.app.app_context():
            e = db.session.get(Employee, self.ids['legacy_a'], execution_options=OPTS)
            e.photo = f'uploads/employees/gone-{self.sfx}.jpg'
            db.session.commit()
        body = self._web('admin_a').get(
            f"/employees/attendance-report/{self.ids['legacy_a']}").get_data(as_text=True)
        self.assertNotIn(f'gone-{self.sfx}.jpg', body)

    # ── 31-34: mobile + chat keep field names, prefer the copy ────────────────

    def test_31_34_mobile_and_chat_prefer_display(self):
        d_orig, d_disp = self._names('derived_a')
        login = self.app.test_client().post(
            '/api/mobile/v1/auth/login',
            json={'username': self.ids['teacher_a'], 'password': PASSWORD})
        self.assertEqual(login.status_code, 200, login.get_data(as_text=True)[:300])
        emp = login.get_json()['employee']
        self.assertEqual(set(emp), {'id', 'employee_id', 'name', 'job_title', 'photo'})   # 34
        self.assertIn(d_disp, emp['photo'])                                               # 31
        self.assertNotIn(d_orig, emp['photo'])

        c = self.app.test_client()
        prof = c.get('/api/mobile/v1/teacher/profile',
                     headers=self._jwt(self.ids['teacher_id_a'])).get_json()['employee']
        for field in ('photo', 'photo_url'):                                              # 32
            self.assertIn(d_disp, prof[field])
            self.assertNotIn(d_orig, prof[field])

        # 33: gate switches only (module/feature flags) — the contact query,
        # school filter and authorization run unchanged.
        with mock.patch('app.blueprints.mobile_api.chat.is_module_enabled', return_value=True), \
                mock.patch('app.blueprints.mobile_api.chat.is_feature_enabled', return_value=True):
            resp = c.get('/api/mobile/v1/chat/contacts',
                         headers=self._jwt(self.ids['parent_id_a']))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:300])
        body = resp.get_json()
        contacts = body.get('contacts', body.get('data', body))
        teachers = [x for x in contacts if x.get('role') == 'teacher']
        self.assertEqual(len(teachers), 1)
        self.assertEqual(set(teachers[0]), {'user_id', 'name', 'role', 'job_title', 'photo'})
        self.assertIn(d_disp, teachers[0]['photo'])
        self.assertNotIn(d_orig, teachers[0]['photo'])
        self.assertNotIn(f'Derived b', str(contacts))                       # other school absent

    # ── 35-37: AI Face reads Employee.photo only ──────────────────────────────

    def test_35_37_aiface_uses_original_only(self):
        orig, disp = self._row('derived_a')
        self.assertIsNotNone(disp)
        with self.app.app_context():
            dev = AttendanceDevice(school_id=self.ids['school_a'], name='cam',
                                   device_scope='employees', ip_address='127.0.0.1',
                                   password='x', device_sn=f'SN-{self.sfx}')
            db.session.add(dev)
            db.session.flush()
            m = DeviceEmployeeMapping(school_id=self.ids['school_a'], device_id=dev.id,
                                      employee_id=self.ids['derived_a'], enrollment_no='7',
                                      is_active=True)
            db.session.add(m)
            db.session.commit()
            dev_id, m_id = dev.id, m.id
        client = self._web('admin_a')
        with mock.patch('app.services.aiface_sync.sync_person_to_device',
                        return_value={'ok': True}) as sync:
            resp = client.post(f'/attendance-devices/{dev_id}/aiface-sync-employee',
                               json={'mapping_id': m_id})                           # 35
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:300])
            self.assertEqual(sync.call_args.kwargs['photo'], orig)
            sync.reset_mock()
            resp = client.post(f'/attendance-devices/{dev_id}/aiface-sync-all', json={})  # 36
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:300])
            (call,) = sync.call_args_list
            self.assertEqual(call.args[3], orig)
        for c in sync.mock_calls:                                                         # 37
            self.assertNotIn(disp, repr(c))
        for rel in ('app/services/aiface_sync.py', 'app/blueprints/attendance_devices/__init__.py',
                    'app/services/hikvision.py', 'app/services/ai_face_ws.py'):
            text_ = (ROOT / rel).read_text(encoding='utf-8')
            self.assertNotIn('photo_display', text_, rel)
            self.assertNotIn('employee_display', text_, rel)

    # ── 38, 39: tenant isolation + local-file ownership ───────────────────────

    def test_38_cross_school_unchanged(self):
        before = self._all_rows()
        client = self._web('admin_a')
        resp = self._edit(client, 'derived_b', 'Hijack', photo=('p.jpg', _enc(_portrait(300, 400), 'JPEG')))
        self.assertIn(resp.status_code, (403, 404))
        self.assertIn(client.get(f"/employees/{self.ids['derived_b']}").status_code, (403, 404))
        _, b_disp = self._names('derived_b')
        self.assertNotIn(b_disp, client.get('/employees/').get_data(as_text=True))
        self.assertNotIn(b_disp, str(client.get('/employees/search').get_json()))
        self.assertEqual(self._all_rows(), before)
        self.storage.assert_not_called()

    def test_39_local_display_ownership_owner_school_only(self):
        from app.utils.upload_access import can_access_upload, resolve_upload_owner
        rel = f'uploads/employees/display/own-{self.sfx}.webp'
        with self.app.app_context():
            e = db.session.get(Employee, self.ids['derived_a'], execution_options=OPTS)
            e.photo_display = rel
            db.session.commit()
            owner = resolve_upload_owner(rel)
            self.assertEqual((owner['school_id'], owner['employee_id'], owner['kind']),
                             (self.ids['school_a'], self.ids['derived_a'], 'employee_photo'))

            def allowed(uid):
                return can_access_upload(db.session.get(User, uid, execution_options=OPTS), rel)
            self.assertTrue(allowed(self.ids['admin_id_a']))          # same-school staff
            self.assertTrue(allowed(self.ids['teacher_id_a']))        # the employee's own account
            self.assertFalse(allowed(self.ids['other_teacher_id_a'])) # another teacher
            self.assertFalse(allowed(self.ids['parent_id_a']))        # parent: not a child file
            self.assertFalse(allowed(self.ids['admin_id_b']))         # other school
            self.assertFalse(allowed(self.ids['teacher_id_b']))
            self.assertIsNone(resolve_upload_owner(
                f'uploads/employees/display/unknown-{self.sfx}.webp'))  # unknown → deny

    # ── 40-42: old rows never processed, schema additive ──────────────────────

    def test_40_old_rows_never_processed(self):
        before = self._all_rows()
        client = self._web('admin_a')
        for page in ('/employees/', '/employees/search', f"/employees/{self.ids['legacy_a']}",
                     f"/employees/{self.ids['legacy_a']}/edit",
                     f"/employees/attendance-report/{self.ids['legacy_a']}"):
            self.assertEqual(client.get(page).status_code, 200, page)
        resp, _ = self._create(client, _enc(_portrait(600, 800), 'JPEG'))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._all_rows()[:len(before)], before)            # no old row changed
        self.assertIsNone(self._row('legacy_a')[1])
        self.fetch.assert_not_called()
        self.delete.assert_not_called()
        self.prepare.assert_called_once()                                   # the new upload only
        # generation is wired ONLY into the create/edit upload handler
        users = [str(p.relative_to(ROOT)).replace('\\', '/') for p in (ROOT / 'app').rglob('*.py')
                 if 'prepare_employee_display_photo' in p.read_text(encoding='utf-8')
                 or 'save_employee_display_photo' in p.read_text(encoding='utf-8')]
        self.assertEqual(sorted(users), ['app/blueprints/employees/__init__.py',
                                         'app/utils/employee_display_photo.py'])

    def test_41_42_schema_additive_existing_null(self):
        with self.app.app_context():
            col = {c['name']: c for c in sa_inspect(db.engine).get_columns('employees')}['photo_display']
            self.assertTrue(col['nullable'])
            self.assertIsNone(col['default'])
            self.assertEqual(col['type'].length, 255)
            self.assertTrue(Employee.__table__.c.photo_display.nullable)
            self.assertIsNone(Employee.__table__.c.photo_display.default)
            self.assertIsNone(Employee.__table__.c.photo_display.server_default)
            value = db.session.execute(text('SELECT photo_display FROM employees WHERE id = :i'),
                                       {'i': self.ids['legacy_a']}).scalar()
            self.assertIsNone(value)
        mig = (ROOT / 'migrations/versions/e5m6p7d8s9p0_add_employee_photo_display.py').read_text(
            encoding='utf-8')
        code = mig.split('"""', 2)[2]
        for forbidden in ('UPDATE', 'INSERT', 'server_default', 'default=', 'nullable=False',
                          'execute(sa.text', 'bulk_'):
            self.assertNotIn(forbidden, code, forbidden)

    # ── 45: employee documents unchanged ──────────────────────────────────────

    def test_45_employee_documents_unchanged(self):
        pdf = b'%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n'
        raw = _enc(_portrait(600, 800), 'JPEG')
        resp, stored = self._create(self._web('admin_a'), raw, extra={
            'doc_type[]': ['هوية'], 'doc_file[]': [(io.BytesIO(pdf), 'id.pdf', 'application/pdf')]})
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True)[-600:])
        paths = [p for _, p, _ in self._uploads()]
        self.assertEqual(len(paths), 3, paths)
        self.assertTrue(paths[0].startswith('employees/') and paths[0].endswith('.jpg'))
        self.assertTrue(paths[1].startswith('employees/display/'))
        self.assertTrue(paths[2].startswith('employee_docs/') and paths[2].endswith('.pdf'))
        self.assertEqual(hashlib.sha256(self.storage.call_args_list[2].args[0]).digest(),
                         hashlib.sha256(pdf).digest())                      # doc stored as-is
        with self.app.app_context():
            doc = EmployeeDocument.query.execution_options(**OPTS).filter_by(
                school_id=self.ids['school_a']).one()
            self.assertEqual(doc.file_path, BASE + paths[2])


if __name__ == '__main__':
    unittest.main()

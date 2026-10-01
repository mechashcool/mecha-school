"""
Student display-photo derivative (students.photo_display) — targeted tests.

Student.photo stays the byte-identical original and the only AI Face source.
A NEW upload through Student create/edit also stores a display-only WebP copy
(192 x 192 centred square, q45/m6, EXIF/GPS stripped) under students/display/. Every display
consumer (web list/search/attendance/detail/edit/create-success, mobile parent
and teacher) prefers the copy and falls back to Student.photo; the legacy raw
/api/v1/parent/me stays unchanged. Existing students (photo_display NULL) render
exactly as before and are never processed. Derivative failure never affects the
original or the student operation. Requires migration j1s2d3p4h5o6 on the test
database.

Storage is a recording mock (no network); nothing is written to Supabase.
Run with ``-s`` to see the local size table.
"""
import hashlib
import io
import pathlib
import re
import unittest
from datetime import date
from unittest import mock
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFilter
from sqlalchemy import inspect as sa_inspect

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (db, AcademicYear, AttendanceDevice, AuditLog, DeviceStudentMapping,
                        Employee, Grade, Role, School, Section, Student, User,
                        parent_students)
from app.utils import student_display_photo as sdp
from app.utils.student_display_photo import (STUDENT_DISPLAY_MAX_SIDE,
                                             STUDENT_DISPLAY_WEBP_QUALITY, make_display_photo)
from app.utils.student_photo import MSG_INVALID
from app.utils.face_photo import normalize_face_photo

PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True}
BASE = 'https://storage.test/storage/v1/object/public/uploads/'


def _url_for_path(data, path, ctype, bucket=None):
    return f'{BASE}{path}'


# ── synthetic student-style photos (no production files) ────────────────────

def _enc(img, fmt, **kw):
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def _portrait(w, h):
    img = Image.new('RGB', (w, h), (205, 190, 170))
    d = ImageDraw.Draw(img)
    d.ellipse((w // 4, h // 6, 3 * w // 4, h // 2), fill=(225, 185, 150))      # face
    d.rectangle((w // 6, h // 2, 5 * w // 6, h), fill=(30, 60, 120))            # uniform
    noise = Image.effect_noise((w, h), 25).convert('RGB')
    return Image.blend(img.filter(ImageFilter.GaussianBlur(2)), noise, 0.12)


def _phone_jpeg(w=3024, h=4032, orientation=1):
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = 'SecretCam'
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = 'N', (33.0, 18.0, 0.0), 'E', (44.0, 22.0, 0.0)
    return _enc(_portrait(w, h), 'JPEG', quality=90, exif=exif.tobytes())


def _transparent_png(w=1400, h=1400):
    img = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((w // 8, h // 8, 7 * w // 8, 7 * h // 8), fill=(40, 90, 160, 255))
    return _enc(img, 'PNG')


def _decode(data):
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


# ─────────────────────────────────────────────────────────────────────────────
#  Derivative policy (helper level)
# ─────────────────────────────────────────────────────────────────────────────

def _mean_diff(a, b):
    a, b = a.convert('RGB'), b.convert('RGB').resize(a.size)
    pa, pb = a.tobytes(), b.tobytes()
    return sum(abs(x - y) for x, y in zip(pa, pb)) / len(pa)


def _quadrants(w, h):
    """Four distinct quadrants meeting at the centre: every one of the 8 EXIF
    orientations gives a different centred crop."""
    img = Image.new('RGB', (w, h))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, w // 2, h // 2), fill=(220, 30, 30))
    d.rectangle((w // 2, 0, w, h // 2), fill=(30, 200, 30))
    d.rectangle((0, h // 2, w // 2, h), fill=(30, 30, 220))
    d.rectangle((w // 2, h // 2, w, h), fill=(230, 230, 40))
    return img


def _bands(w, h):
    """Green centre with red/blue bands on the long-axis ends, outside the
    centred square: a centred crop shows green only."""
    img = Image.new('RGB', (w, h), (30, 200, 30))
    d = ImageDraw.Draw(img)
    band = (max(w, h) - min(w, h)) // 2 - 50
    if w >= h:
        d.rectangle((0, 0, band, h), fill=(220, 30, 30))
        d.rectangle((w - band, 0, w, h), fill=(30, 30, 220))
    else:
        d.rectangle((0, 0, w, band), fill=(220, 30, 30))
        d.rectangle((0, h - band, w, h), fill=(30, 30, 220))
    return img


def _swapped_primaries_icc():
    """A valid non-sRGB RGB profile: sRGB with its red/blue colorants swapped,
    so red pixels tagged with it are blue once converted to sRGB."""
    import struct
    from PIL import ImageCms
    p = bytearray(ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
    entries = {bytes(p[132 + 12 * i:136 + 12 * i]): 132 + 12 * i
               for i in range(struct.unpack('>I', p[128:132])[0])}
    r, b = entries[b'rXYZ'], entries[b'bXYZ']
    p[r + 4:r + 12], p[b + 4:b + 12] = p[b + 4:b + 12], p[r + 4:r + 12]
    return bytes(p)


class DisplayPolicyTest(unittest.TestCase):
    """Shared 192 x 192 WebP q45/m6 policy (encode_display_photo)."""

    def test_policy_constants(self):
        self.assertEqual((STUDENT_DISPLAY_MAX_SIDE, STUDENT_DISPLAY_WEBP_QUALITY,
                          sdp.STUDENT_DISPLAY_WEBP_METHOD, sdp.STUDENT_DISPLAY_SUBFOLDER),
                         (192, 45, 6, 'students/display'))
        self.assertEqual((sdp.DISPLAY_SIDE, sdp.DISPLAY_WEBP_QUALITY, sdp.DISPLAY_WEBP_METHOD),
                         (192, 45, 6))

    def test_exact_192_square_webp_portrait_and_landscape(self):
        for (w, h, fmt) in ((3024, 4032, 'JPEG'), (4032, 3024, 'JPEG'), (900, 1600, 'PNG'),
                            (1600, 900, 'PNG'), (1200, 1200, 'WEBP'), (192, 500, 'PNG')):
            with self.subTest(w=w, h=h, fmt=fmt):
                out = _decode(make_display_photo(_enc(_portrait(w, h), fmt)))
                self.assertEqual((out.format, out.size), ('WEBP', (192, 192)))

    def test_never_upscaled(self):
        for (w, h, side) in ((40, 30, 30), (150, 120, 120), (100, 400, 100), (191, 191, 191)):
            with self.subTest(w=w, h=h):
                out = _decode(make_display_photo(_enc(_portrait(w, h), 'PNG')))
                self.assertEqual(out.size, (side, side))         # min(w, h) square, no upscale

    def test_centered_crop(self):
        from PIL import ImageOps
        for (w, h, fmt) in ((1600, 900, 'PNG'), (900, 1600, 'PNG'), (2400, 1350, 'JPEG')):
            with self.subTest(w=w, h=h):
                src = _bands(w, h)
                out = _decode(make_display_photo(_enc(src, fmt, **({'quality': 95} if fmt == 'JPEG' else {}))))
                ref = ImageOps.fit(src, (192, 192), Image.Resampling.LANCZOS, centering=(0.5, 0.5))
                self.assertLess(_mean_diff(out, ref), 6.0)
                for xy in ((1, 1), (190, 1), (1, 190), (190, 190), (96, 96)):
                    r, g, b = out.convert('RGB').getpixel(xy)
                    self.assertGreater(g, 150, xy)               # centre only: no edge bands
                    self.assertLess(max(r, b), 90, xy)

    def test_webp_quality_45_method_6_exactly(self):
        from PIL import ImageOps
        src = _portrait(1600, 1200)
        raw = _enc(src, 'PNG')
        ref = ImageOps.fit(src, (192, 192), Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        expect = {(q, m): _enc(ref, 'WEBP', quality=q, method=m)
                  for (q, m) in ((45, 6), (40, 6), (50, 6), (45, 4), (80, 4))}
        got = make_display_photo(raw)
        self.assertEqual(got, expect[(45, 6)])
        for other in ((40, 6), (50, 6), (45, 4), (80, 4)):
            self.assertNotEqual(got, expect[other], other)

    def test_exif_orientations_1_to_8(self):
        from PIL import ImageOps
        unoriented = None
        for orientation in range(1, 9):
            with self.subTest(orientation=orientation):
                exif = Image.Exif()
                exif[0x0112] = orientation
                raw = _enc(_quadrants(1600, 1200), 'JPEG', quality=95, exif=exif.tobytes())
                out = _decode(make_display_photo(raw))
                self.assertEqual(out.size, (192, 192))
                ref = ImageOps.fit(ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert('RGB'),
                                   (192, 192), Image.Resampling.LANCZOS)
                self.assertLess(_mean_diff(out, ref), 6.0)        # same picture, same orientation
                if orientation == 1:
                    unoriented = out
                else:
                    self.assertGreater(_mean_diff(out, unoriented), 25.0)   # orientation applied

    def test_exif_xmp_gps_removed_original_untouched(self):
        xmp = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf='
               b'"http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description '
               b'SecretXmp="1"/></rdf:RDF></x:xmpmeta>')
        exif = Image.Exif()
        exif[0x0112], exif[0x010F] = 6, 'SecretCam'
        gps = exif.get_ifd(0x8825)
        gps[1], gps[2] = 'N', (33.0, 18.0, 0.0)
        raw = _enc(_portrait(3000, 2250), 'JPEG', quality=90, exif=exif.tobytes(), xmp=xmp)
        before = hashlib.sha256(raw).hexdigest()
        data = make_display_photo(raw)
        out = _decode(data)
        self.assertEqual(len(out.getexif()), 0)
        for key in ('exif', 'xmp', 'XML:com.adobe.xmp', 'icc_profile'):
            self.assertNotIn(key, out.info)
        for marker in (b'SecretCam', b'SecretXmp', b'Exif', b'EXIF', b'XMP ', b'xmpmeta', b'ICCP'):
            self.assertNotIn(marker, data)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), before)          # original bytes
        src = Image.open(io.BytesIO(raw))
        self.assertEqual(src.getexif()[0x010F], 'SecretCam')
        self.assertEqual(dict(src.getexif().get_ifd(0x8825))[1], 'N')
        self.assertIn(b'SecretXmp', raw)

    def test_icc_converted_to_srgb_and_dropped(self):
        from PIL import ImageCms
        red = Image.new('RGB', (400, 300), (230, 20, 20))
        swapped = _swapped_primaries_icc()
        for fmt in ('PNG', 'JPEG'):
            with self.subTest(fmt=fmt):
                raw = _enc(red, fmt, icc_profile=swapped)
                before = hashlib.sha256(raw).hexdigest()
                data = make_display_photo(raw)
                out = _decode(data)
                self.assertNotIn('icc_profile', out.info)
                self.assertNotIn(b'ICCP', data)
                r, g, b = out.convert('RGB').getpixel((96, 96))
                self.assertGreater(b, 180)                       # converted: red → blue in sRGB
                self.assertLess(r, 60)
                self.assertEqual(hashlib.sha256(raw).hexdigest(), before)
                self.assertEqual(Image.open(io.BytesIO(raw)).info.get('icc_profile'), swapped)
        srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        plain = _decode(make_display_photo(_enc(red, 'PNG')))
        tagged = _decode(make_display_photo(_enc(red, 'PNG', icc_profile=srgb)))
        self.assertNotIn('icc_profile', tagged.info)
        self.assertLess(_mean_diff(plain, tagged), 2.0)          # sRGB source: same colours
        broken = _decode(make_display_photo(_enc(red, 'PNG', icc_profile=b'not a profile')))
        self.assertEqual((broken.size, broken.info.get('icc_profile')), ((192, 192), None))
        self.assertLess(_mean_diff(plain, broken), 2.0)          # unusable profile ignored

    def test_transparency_preserved(self):
        alpha = _decode(make_display_photo(_transparent_png()))
        self.assertEqual((alpha.mode, alpha.size), ('RGBA', (192, 192)))
        self.assertLess(alpha.getpixel((2, 2))[3], 10)                  # transparent corner
        self.assertGreater(alpha.getpixel((96, 96))[3], 245)            # opaque centre
        icc = _decode(make_display_photo(_enc(Image.open(io.BytesIO(_transparent_png())),
                                              'PNG', icc_profile=_swapped_primaries_icc())))
        self.assertEqual(icc.mode, 'RGBA')
        self.assertLess(icc.getpixel((2, 2))[3], 10)                    # alpha kept through ICC
        opaque = _decode(make_display_photo(_enc(_portrait(800, 600), 'PNG')))
        self.assertEqual(opaque.mode, 'RGB')

    def test_animated_gif_uses_first_frame(self):
        a, b = _portrait(300, 400), _portrait(300, 400).rotate(180)
        gif = _enc(a.convert('P'), 'GIF', save_all=True, append_images=[b.convert('P')],
                   duration=100)
        out = _decode(make_display_photo(gif))
        self.assertEqual((out.format, out.size, getattr(out, 'n_frames', 1)),
                         ('WEBP', (192, 192), 1))
        first = Image.new('RGB', (300, 400), (220, 30, 30)).convert('P')
        second = Image.new('RGB', (300, 400), (30, 30, 220)).convert('P')
        out = _decode(make_display_photo(_enc(first, 'GIF', save_all=True,
                                              append_images=[second], duration=100)))
        r, g, b = out.convert('RGB').getpixel((96, 96))
        self.assertGreater(r, 150)                                      # red = frame 0
        self.assertLess(b, 100)

    def test_student_and_employee_share_one_encoder(self):
        from app.utils.employee_display_photo import make_employee_display_photo
        for raw in (_phone_jpeg(1600, 1200, orientation=6), _transparent_png(600, 600),
                    _enc(_portrait(900, 1600), 'WEBP')):
            self.assertEqual(make_display_photo(raw), make_employee_display_photo(raw))

    def test_size_examples_report(self):
        cases = [('phone portrait JPEG q90', _phone_jpeg()),
                 ('small portrait JPEG q90 (600x800)', _enc(_portrait(600, 800), 'JPEG', quality=90)),
                 ('transparent PNG', _transparent_png())]
        print('\n\ncase | input | original stored | display | display vs input')
        for label, raw in cases:
            src = Image.open(io.BytesIO(raw))
            disp = make_display_photo(raw)
            out = _decode(disp)
            print(f'{label} | {src.format} {src.width}x{src.height} {len(raw):,} B | '
                  f'same {len(raw):,} B | WEBP {out.width}x{out.height} {len(disp):,} B | '
                  f'{100 - 100 * len(disp) / len(raw):.1f}%')
            self.assertEqual(out.size, (192, 192))


# ─────────────────────────────────────────────────────────────────────────────
#  Fallback hardening: a broken display copy is never worse than none
# ─────────────────────────────────────────────────────────────────────────────

class DisplayFallbackTest(unittest.TestCase):
    """student_display_value / student_photo_url: local checks only, no I/O."""

    ORIG = BASE + 'students/orig-fallback.jpg'

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    def setUp(self):
        # Any Storage or HTTP call while deciding the fallback fails the test.
        boom = AssertionError('network/Storage used to decide the display fallback')
        for target in ('app.utils.helpers._supabase_fetch', 'app.utils.helpers._supabase_sign',
                       'app.utils.helpers._supabase_upload', 'requests.sessions.Session.request'):
            mock.patch(target, side_effect=boom).start()
        self.addCleanup(mock.patch.stopall)

    def _resolve(self, display):
        from types import SimpleNamespace
        st = SimpleNamespace(photo=self.ORIG, photo_display=display)
        out = {}
        for flag in (False, True):
            with mock.patch.dict(self.app.config, {'PRIVATE_UPLOADS_ENABLED': flag}), \
                    self.app.test_request_context('/'):
                out[flag] = (sdp.student_display_value(st), sdp.student_photo_url(st))
        return out

    def _assert_original(self, display):
        for flag, (value, url) in self._resolve(display).items():
            self.assertEqual(value, self.ORIG, (display, flag))
            self.assertIn('orig-fallback.jpg', url, (display, flag))

    def test_null_or_empty_display_falls_back_to_original(self):
        for display in (None, '', '   '):
            with self.subTest(display=display):
                self._assert_original(display)

    def test_missing_local_display_falls_back_to_original(self):
        gone = f'gone-{uuid4().hex}.webp'
        for display in (f'uploads/students/display/{gone}', f'/static/uploads/students/display/{gone}',
                        gone, '../../config/settings.py', 'uploads/../../run.py'):
            with self.subTest(display=display):
                self._assert_original(display)

    def test_existing_local_display_is_used(self):
        name = f'kept-{uuid4().hex}.webp'
        rel = f'uploads/students/display/{name}'
        path = pathlib.Path(self.app.root_path, 'static', *rel.split('/'))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(make_display_photo(_enc(_portrait(300, 400), 'JPEG')))
        try:
            for flag, (value, url) in self._resolve(rel).items():
                self.assertEqual(value, rel, flag)
                self.assertIn(name, url, flag)
        finally:
            path.unlink()

    def test_remote_display_used_without_network(self):
        display = BASE + 'students/display/remote-ok.webp'
        for flag, (value, url) in self._resolve(display).items():
            self.assertEqual(value, display, flag)
            self.assertIn('remote-ok.webp', url, flag)


# ─────────────────────────────────────────────────────────────────────────────
#  Routes, serializers, AI Face, failure safety
# ─────────────────────────────────────────────────────────────────────────────

class StudentDisplayPhotoTest(unittest.TestCase):

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
        self.prepare = mock.patch('app.blueprints.students.prepare_display_photo',
                                  wraps=sdp.prepare_display_photo, create=True).start()
        self.addCleanup(mock.patch.stopall)
        self.ids = {}
        with self.app.app_context():
            for key in ('a', 'b'):
                self._school(key)
            db.session.commit()

    def _school(self, key):
        s = self.sfx
        school = School(school_name=f'Disp {key} {s}', code=f'DP{key}{s}'[:20],
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
            u = User(username=f'dp{label}{key}_{s}', email=f'dp{label}{key}_{s}@t.test',
                     full_name=f'{label} {key}', role_id=self.role_ids[role],
                     school_id=school.id, is_active=True)
            u.set_password(PASSWORD)
            db.session.add(u)
            db.session.flush()
            return u

        admin, parent, teacher = user('adm', 'school_admin'), user('par', 'parent'), user('t', 'teacher')
        emp = Employee(school_id=school.id, employee_id=f'DP{key}{s}', full_name=f'T {key}',
                       base_salary=0, status='active', user_id=teacher.id)
        db.session.add(emp)
        db.session.flush()
        db.session.execute(Section.__table__.update().where(Section.id == sec.id)
                           .values(teacher_id=emp.id))
        legacy = Student(student_id=f'L{key}-{s}', full_name=f'Legacy {key}', school_id=school.id,
                         academic_year_id=year.id, section_id=sec.id, status='active',
                         photo=f'{BASE}students/old-{key}-{s}.jpg')         # photo_display NULL
        derived = Student(student_id=f'D{key}-{s}', full_name=f'Derived {key}',
                          school_id=school.id, academic_year_id=year.id, section_id=sec.id,
                          status='active', photo=f'{BASE}students/orig-{key}-{s}.jpg')
        derived.photo_display = f'{BASE}students/display/disp-{key}-{s}.webp'
        db.session.add_all([legacy, derived])
        db.session.flush()
        for st in (legacy, derived):
            db.session.execute(parent_students.insert().values(
                user_id=parent.id, student_id=st.id, relation='guardian'))
        self.ids.update({f'school_{key}': school.id, f'sec_{key}': sec.id,
                         f'legacy_{key}': legacy.id, f'derived_{key}': derived.id,
                         f'admin_{key}': admin.username, f'parent_{key}': parent.username,
                         f'parent_id_{key}': parent.id, f'teacher_id_{key}': teacher.id})

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
                for model in (DeviceStudentMapping, AttendanceDevice, AuditLog, Student,
                              Section, Grade, Employee, User, AcademicYear):
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
            st = db.session.get(Student, self.ids[key], execution_options=OPTS)
            return st.photo, st.photo_display

    def _create(self, client, raw, name='photo.jpg', full_name=None):
        full_name = full_name or f'New {uuid4().hex[:6]}'
        resp = client.post('/students/create', content_type='multipart/form-data', data={
            'full_name': full_name, 'section_id': str(self.ids['sec_a']),
            'gender': 'male', 'date_of_birth': '2015-01-01',
            'photo': (io.BytesIO(raw), name, 'image/jpeg')})
        with self.app.app_context():
            st = Student.query.execution_options(**OPTS).filter_by(full_name=full_name).first()
            return resp, (st.photo, st.photo_display) if st else None

    def _edit(self, client, key, full_name, photo=None):
        data = {'full_name': full_name, 'section_id': str(self.ids['sec_a']), 'status': 'active'}
        if photo is not None:
            data['photo'] = (io.BytesIO(photo[1]), photo[0], 'image/jpeg')
        return client.post(f"/students/{self.ids[key]}/edit",
                           content_type='multipart/form-data', data=data)

    def _uploads(self):
        """[(sha256, object_path, content_type)] of every Storage write."""
        return [(hashlib.sha256(c.args[0]).hexdigest(), c.args[1], c.args[2])
                for c in self.storage.call_args_list]

    def _paths(self, key):
        orig, disp = self._row(key)
        return (orig.rsplit('/', 1)[-1] if orig else None,
                disp.rsplit('/', 1)[-1] if disp else None)

    # ── 1. migration / model ─────────────────────────────────────────────────

    def test_01_column_nullable_no_default_existing_rows_null(self):
        with self.app.app_context():
            col = {c['name']: c for c in sa_inspect(db.engine).get_columns('students')}['photo_display']
            self.assertTrue(col['nullable'])
            self.assertIsNone(col['default'])
            self.assertTrue(Student.__table__.c.photo_display.nullable)
            self.assertIsNone(Student.__table__.c.photo_display.default)
            value = db.session.execute(db.text('SELECT photo_display FROM students WHERE id = :i'),
                                       {'i': self.ids['legacy_a']}).scalar()
            self.assertIsNone(value)

    # ── 2-7. new student ─────────────────────────────────────────────────────

    def test_02_to_07_create_stores_original_and_display(self):
        raw = _phone_jpeg()
        resp, (orig, disp) = self._create(self._web('admin_a'), raw)
        self.assertEqual(resp.status_code, 302)
        (o_sha, o_path, o_ct), (d_sha, d_path, d_ct) = self._uploads()
        self.assertEqual(o_sha, hashlib.sha256(normalize_face_photo(raw)).hexdigest())  # 2: Face ID form
        self.assertTrue(o_path.startswith('students/') and o_path.endswith('.jpg'))
        self.assertEqual(o_ct, 'image/jpeg')
        self.assertTrue(d_path.startswith('students/display/') and d_path.endswith('.webp'))
        self.assertEqual(d_ct, 'image/webp')                                # 3
        self.assertEqual((orig, disp), (BASE + o_path, BASE + d_path))
        data = self.storage.call_args_list[1].args[0]
        img = _decode(data)
        self.assertEqual(img.format, 'WEBP')
        self.assertEqual(img.size, (192, 192))                              # 4
        self.assertEqual(data, make_display_photo(raw))                     # 5: from the ORIGINAL
        self.assertNotEqual(data, make_display_photo(normalize_face_photo(raw)))
        self.assertEqual(len(img.getexif()), 0)                             # 6
        for marker in (b'SecretCam', b'Exif', b'EXIF'):
            self.assertNotIn(marker, data)
        stored_photo = self.storage.call_args_list[0].args[0]               # 7: photo = 640 JPEG q85
        st_img = Image.open(io.BytesIO(stored_photo))
        self.assertEqual((st_img.format, st_img.mode, st_img.size), ('JPEG', 'RGB', (480, 640)))
        self.assertNotIn(b'SecretCam', stored_photo)

    # ── 8, 9. AI Face ────────────────────────────────────────────────────────

    def test_08_09_aiface_reads_only_the_original(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        for rel in ('app/services/aiface_sync.py', 'app/blueprints/attendance_devices/__init__.py',
                    'app/services/admission_approval.py', 'app/blueprints/api/__init__.py'):
            self.assertNotIn('photo_display', (root / rel).read_text(encoding='utf-8'), rel)
        # the real sync route passes Student.photo, not the display copy
        with self.app.app_context():
            dev = AttendanceDevice(school_id=self.ids['school_a'], name='cam', device_scope='students',
                                   ip_address='127.0.0.1', password='x', device_sn=f'SN-{self.sfx}')
            db.session.add(dev)
            db.session.flush()
            m = DeviceStudentMapping(school_id=self.ids['school_a'], device_id=dev.id,
                                     employee_no_string='7', student_id=self.ids['derived_a'],
                                     is_active=True)
            db.session.add(m)
            db.session.commit()
            dev_id, m_id = dev.id, m.id
        orig, disp = self._row('derived_a')
        with mock.patch('app.services.aiface_sync.sync_person_to_device',
                        return_value={'ok': True}) as sync:
            resp = self._web('admin_a').post(f'/attendance-devices/{dev_id}/aiface-sync-student',
                                             json={'mapping_id': m_id})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:300])
        self.assertEqual(sync.call_args.kwargs['photo'], orig)
        self.assertNotEqual(sync.call_args.kwargs['photo'], disp)
        # the device preparation fetches the ORIGINAL object of a new upload
        from app.services.aiface_sync import prepare_photo_for_device
        raw = _phone_jpeg()
        _, (new_orig, new_disp) = self._create(self._web('admin_a'), raw)
        self.assertTrue(new_disp)
        with self.app.app_context():
            self.fetch.return_value = (raw, 'image/jpeg')
            jpeg, info = prepare_photo_for_device(new_orig, label='t')
        self.fetch.assert_called_once_with(new_orig[len(BASE):], bucket='uploads')
        self.assertNotIn('display', self.fetch.call_args.args[0])
        self.assertLessEqual(max(Image.open(io.BytesIO(jpeg)).size), 640)

    # ── 10-12. web display consumers ─────────────────────────────────────────

    def test_10_11_12_web_prefers_display_and_falls_back(self):
        d_orig, d_disp = self._paths('derived_a')
        l_orig, _ = self._paths('legacy_a')
        client = self._web('admin_a')
        html = client.get('/students/').get_data(as_text=True)
        self.assertIn(d_disp, html)
        self.assertNotIn(d_orig, html)
        self.assertIn(l_orig, html)                                        # fallback
        for name in (d_disp, l_orig):                                      # row <img> is lazy
            self.assertRegex(html, r'<img src="[^"]*' + re.escape(name)
                             + r'[^"]*" loading="lazy" decoding="async"')
        items = {i['id']: i['photo_url'] for i in
                 client.get('/students/search').get_json()['items']}
        self.assertIn(d_disp, items[self.ids['derived_a']])
        self.assertIn(l_orig, items[self.ids['legacy_a']])
        att = {s['id']: s['photo_url'] for s in client.get(
            f"/attendance/manual/students?section_id={self.ids['sec_a']}").get_json()['students']}
        self.assertIn(d_disp, att[self.ids['derived_a']])
        self.assertIn(l_orig, att[self.ids['legacy_a']])
        for page in (f"/students/{self.ids['derived_a']}", f"/students/{self.ids['derived_a']}/edit"):
            body = client.get(page).get_data(as_text=True)
            self.assertIn(d_disp, body, page)
            self.assertNotIn(d_orig, body, page)
        self.assertIn(l_orig, client.get(f"/students/{self.ids['legacy_a']}").get_data(as_text=True))

    # ── 13-15. mobile + legacy ───────────────────────────────────────────────

    def test_13_14_15_mobile_prefers_display_legacy_raw(self):
        d_orig, d_disp = self._paths('derived_a')
        l_orig, _ = self._paths('legacy_a')
        def check(photo_by_id):
            self.assertIn(d_disp, photo_by_id[self.ids['derived_a']])
            self.assertNotIn(d_orig, photo_by_id[self.ids['derived_a']])
            self.assertIn(l_orig, photo_by_id[self.ids['legacy_a']])

        login = self.app.test_client().post(
            '/api/mobile/v1/auth/login',
            json={'username': self.ids['parent_a'], 'password': PASSWORD})
        self.assertEqual(login.status_code, 200)
        check({x['id']: x['photo'] for x in login.get_json()['children']})
        c = self.app.test_client()
        ph = self._jwt(self.ids['parent_id_a'])

        check({x['id']: x['photo'] for x in c.get('/api/mobile/v1/parent/children',
                                                  headers=ph).get_json()['children']})
        check({x['id']: x['photo'] for x in c.get('/api/mobile/v1/me',
                                                  headers=ph).get_json()['children']})
        prof = c.get(f"/api/mobile/v1/parent/children/{self.ids['derived_a']}", headers=ph).get_json()
        self.assertIn(d_disp, prof['photo'] if 'photo' in prof else prof['student']['photo'])
        th = self._jwt(self.ids['teacher_id_a'])
        roster = c.get(f"/api/mobile/v1/teacher/sections/{self.ids['sec_a']}/students",
                       headers=th).get_json()
        check({x['id']: x['photo'] for x in roster['students']})
        tp = c.get(f"/api/mobile/v1/teacher/students/{self.ids['derived_a']}", headers=th).get_json()
        tp = tp.get('student', tp)
        self.assertIn(d_disp, tp['photo'])
        # 15: legacy web-session API still returns the raw original value
        legacy = self._web('parent_a').get('/api/v1/parent/me').get_json()
        raw_by_id = {x['id']: x['photo'] for x in legacy['children']}
        orig, _ = self._row('derived_a')
        self.assertEqual(raw_by_id[self.ids['derived_a']], orig)

    def test_missing_local_display_copy_shows_original_web_and_mobile(self):
        gone = f'gone-{self.sfx}.webp'
        with self.app.app_context():
            st = db.session.get(Student, self.ids['derived_a'], execution_options=OPTS)
            st.photo_display = f'uploads/students/display/{gone}'   # file absent on disk
            db.session.commit()
        d_orig, _ = self._paths('derived_a')
        client = self._web('admin_a')
        for page in ('/students/', f"/students/{self.ids['derived_a']}"):
            body = client.get(page).get_data(as_text=True)
            self.assertIn(d_orig, body, page)
            self.assertNotIn(gone, body, page)
        children = self.app.test_client().get(
            '/api/mobile/v1/parent/children',
            headers=self._jwt(self.ids['parent_id_a'])).get_json()['children']
        photo = {c['id']: c['photo'] for c in children}[self.ids['derived_a']]
        self.assertIn(d_orig, photo)
        self.assertNotIn(gone, photo)
        self.fetch.assert_not_called()
        self.storage.assert_not_called()

    # ── 16, 17. edit ─────────────────────────────────────────────────────────

    def test_16_metadata_edit_touches_neither_field(self):
        before = self._row('derived_a')
        self.assertEqual(self._edit(self._web('admin_a'), 'derived_a', 'Meta').status_code, 302)
        self.assertEqual(self._row('derived_a'), before)
        self.storage.assert_not_called()
        self.prepare.assert_not_called()

    def test_17_replacement_updates_both_to_new_pair(self):
        old = self._row('derived_a')
        raw = _phone_jpeg(1200, 1600)
        self.assertEqual(self._edit(self._web('admin_a'), 'derived_a', 'Repl',
                                    photo=('new.jpg', raw)).status_code, 302)
        orig, disp = self._row('derived_a')
        (o_sha, o_path, _), (_, d_path, _) = self._uploads()
        self.assertEqual(o_sha, hashlib.sha256(normalize_face_photo(raw)).hexdigest())
        self.assertEqual(self.storage.call_args_list[1].args[0], make_display_photo(raw))
        self.assertEqual((orig, disp), (BASE + o_path, BASE + d_path))
        self.assertNotIn(orig, old)
        self.assertNotIn(disp, old)
        self.delete.assert_not_called()                                     # nothing deleted

    # ── 18, 19. failure safety ───────────────────────────────────────────────

    def test_18_generation_failure_keeps_original(self):
        raw = _phone_jpeg(1200, 1600)
        with mock.patch('app.utils.student_display_photo.make_display_photo',
                        side_effect=RuntimeError('boom')):
            resp, (orig, disp) = self._create(self._web('admin_a'), raw)
            self.assertEqual(resp.status_code, 302)
            self.assertEqual((len(self._uploads()), disp), (1, None))
            self.assertEqual(self._uploads()[0][0], hashlib.sha256(normalize_face_photo(raw)).hexdigest())
            self.assertTrue(orig)
            # replacement with failing derivative clears the OLD display copy
            self.storage.reset_mock()
            self._edit(self._web('admin_a'), 'derived_a', 'R', photo=('n.jpg', raw))
        orig, disp = self._row('derived_a')
        self.assertIsNone(disp)
        self.assertEqual(orig, BASE + self._uploads()[0][1])

    def test_19_storage_failure_keeps_original(self):
        from app.utils import helpers
        real = helpers.save_uploaded_file

        def failing_display_save(file, subfolder='misc', **kw):
            if subfolder == 'students/display':
                raise OSError('storage down')
            return real(file, subfolder, **kw)

        raw = _phone_jpeg(1200, 1600)
        for label, fake in (('raises', failing_display_save),
                            ('returns None', lambda f, subfolder='misc', **kw:
                             None if subfolder == 'students/display' else real(f, subfolder, **kw))):
            with self.subTest(label), mock.patch('app.utils.helpers.save_uploaded_file', fake):
                self.storage.reset_mock()
                resp, (orig, disp) = self._create(self._web('admin_a'), raw)
                self.assertEqual(resp.status_code, 302)
                self.assertIsNone(disp)
                self.assertEqual(orig, BASE + self._uploads()[0][1])
                self.assertEqual(self._uploads()[0][0], hashlib.sha256(normalize_face_photo(raw)).hexdigest())

    def test_original_failure_behaviour_unchanged_no_derivative(self):
        # the original is refused by the existing extension check -> no display copy
        resp, row = self._create(self._web('admin_a'), b'BM' + b'\x00' * 100, name='p.bmp')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(row, (None, None))
        self.storage.assert_not_called()
        self.prepare.assert_not_called()

    def test_original_supabase_failure_aborts_no_local_fallback(self):
        from app.blueprints.students import (_MSG_PHOTO_STORE_FAILED_CREATE,
                                             _MSG_PHOTO_STORE_FAILED_EDIT)
        self.storage.side_effect = lambda *a, **k: None                     # Supabase down
        local = pathlib.Path(self.app.root_path, 'static', 'uploads', 'students')
        before = sorted(local.rglob('*')) if local.exists() else []
        client = self._web('admin_a')
        # create: refused before anything is created — no student without its photo
        resp, row = self._create(client, _phone_jpeg(600, 800), full_name=f'NoStore {self.sfx}')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(_MSG_PHOTO_STORE_FAILED_CREATE, resp.get_data(as_text=True))
        self.assertIsNone(row)
        # edit: refused, nothing saved — current photo pair and other fields kept
        old = self._row('derived_a')
        with self.app.app_context():
            old_name = db.session.get(Student, self.ids['derived_a'],
                                      execution_options=OPTS).full_name
        resp = self._edit(client, 'derived_a', 'Renamed NoStore',
                          photo=('n.jpg', _phone_jpeg(600, 800)))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(_MSG_PHOTO_STORE_FAILED_EDIT,
                      client.get(resp.headers['Location']).get_data(as_text=True))
        self.assertEqual(self._row('derived_a'), old)
        with self.app.app_context():
            self.assertEqual(db.session.get(Student, self.ids['derived_a'],
                                            execution_options=OPTS).full_name, old_name)
        # one Supabase attempt per request, no display copy, no local file
        self.assertEqual(self.storage.call_count, 2)
        self.prepare.assert_not_called()
        self.assertEqual(sorted(local.rglob('*')) if local.exists() else [], before)

    def test_normalization_failure_creates_nothing(self):
        from app.blueprints.students import _MSG_PHOTO_STORE_FAILED_CREATE
        with mock.patch('app.blueprints.students.normalized_face_upload', return_value=None):
            resp, row = self._create(self._web('admin_a'), _phone_jpeg(600, 800),
                                     full_name=f'NoNorm {self.sfx}')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(_MSG_PHOTO_STORE_FAILED_CREATE, resp.get_data(as_text=True))
        self.assertIsNone(row)                                              # no student row
        self.storage.assert_not_called()                                    # nothing stored
        self.prepare.assert_not_called()

    # ── 20. existing students are never processed on view ────────────────────

    def test_20_viewing_legacy_student_touches_no_storage(self):
        before = self._row('legacy_a')
        client = self._web('admin_a')
        for path in ('/students/', '/students/search', f"/students/{self.ids['legacy_a']}",
                     f"/students/{self.ids['legacy_a']}/edit",
                     f"/attendance/manual/students?section_id={self.ids['sec_a']}"):
            self.assertEqual(client.get(path).status_code, 200, path)
        c = self.app.test_client()
        c.get('/api/mobile/v1/parent/children', headers=self._jwt(self.ids['parent_id_a']))
        c.get(f"/api/mobile/v1/teacher/sections/{self.ids['sec_a']}/students",
              headers=self._jwt(self.ids['teacher_id_a']))
        self.assertEqual(self._row('legacy_a'), before)
        self.assertIsNone(before[1])
        self.storage.assert_not_called()
        self.fetch.assert_not_called()
        self.prepare.assert_not_called()

    # ── 21. cross-school ─────────────────────────────────────────────────────

    def test_21_cross_school_unchanged(self):
        before = self._row('derived_b')
        resp = self._edit(self._web('admin_a'), 'derived_b', 'Hijack', photo=('p.jpg', _phone_jpeg(600, 800)))
        self.assertIn(resp.status_code, (403, 404))
        self.assertEqual(self._row('derived_b'), before)
        self.storage.assert_not_called()
        resp = self.app.test_client().get(f"/api/mobile/v1/parent/children/{self.ids['derived_b']}",
                                          headers=self._jwt(self.ids['parent_id_a']))
        self.assertIn(resp.status_code, (403, 404))
        html = self._web('admin_a').get('/students/').get_data(as_text=True)
        self.assertNotIn(self._paths('derived_b')[1], html)

    # ── 22. hardening still active ───────────────────────────────────────────

    def test_22_invalid_photo_still_refused(self):
        for name, raw in (('x.jpg', b'not an image' * 40), ('x.jpg', _phone_jpeg(600, 800)[:500])):
            resp, row = self._create(self._web('admin_a'), raw, name=name)
            self.assertEqual(resp.status_code, 200)
            self.assertIn(MSG_INVALID, resp.get_data(as_text=True))
            self.assertIsNone(row)
        self.storage.assert_not_called()

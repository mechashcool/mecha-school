"""
Public (external) registration media hardening — targeted tests.

NEW public-registration uploads:
  * photo: JPG/JPEG/PNG, <= 5 MB, Student photo validation (real decode, 40 MP)
    BEFORE Storage, stored byte-identical under registration/<sid>/photos/v2/;
  * documents: Student Document policy (images -> <=1200 px WebP q75, metadata
    stripped; PDF byte-identical) under registration/<sid>/documents/v2/;
  * every file and every field length is validated before the first write;
  * approval keeps Student.photo = the registration original (AI Face source),
    reuses document objects, and — after commit, v2 photos only — adds the
    Student display copy (best effort).
Legacy registration media is never downloaded, processed or rewritten.

Storage is an in-memory recording fake (no network, nothing reaches Supabase).
Run with ``-s`` to see the synthetic compression table.
"""
import hashlib
import io
import pathlib
import re
import time
import unittest
from datetime import date
from unittest import mock
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFilter
from werkzeug.datastructures import FileStorage

from app import create_app
from app.models import (db, AcademicYear, AuditLog, Employee, Grade, Notification, Role,
                        School, SchoolBuilding, Section, Student, StudentDocument,
                        StudentRegistrationRequest, StudentRegistrationRequestDocument,
                        User, UserBuildingAccess, parent_students)
from app.utils.registration_media import is_v2_registration_photo
from app.utils.registration_tokens import generate_token, hash_token
from app.utils.student_display_photo import make_display_photo
from app.utils.student_documents import (MSG_ANIMATED, MSG_INVALID as DOC_MSG_INVALID,
                                         MSG_TOO_LARGE as DOC_MSG_TOO_LARGE,
                                         optimize_document_image)
from app.utils.student_photo import MSG_TOO_LARGE as PHOTO_MSG_TOO_LARGE
from app.utils.student_photo import validate_student_photo

PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True}
PUBLIC = 'https://storage.test/storage/v1/object/public/'
MB = 1024 * 1024
ROOT = pathlib.Path(__file__).resolve().parent.parent

MSG_TOO_LONG = 'أحد الحقول يتجاوز الطول المسموح.'
MSG_PHOTO_TYPE = 'صيغة الصورة غير مدعومة. الصيغ المسموح بها: JPG أو JPEG أو PNG.'
MSG_PHOTO_SIZE = 'حجم الصورة أكبر من الحد المسموح (5 ميغابايت).'
MSG_PHOTO_INVALID = 'تعذّر قراءة صورة الطالب. يرجى رفع صورة صالحة بصيغة JPG أو JPEG أو PNG.'
MSG_DOC_TYPE = 'نوع الملف غير مدعوم. الصيغ المسموح بها: PDF أو JPG أو JPEG أو PNG.'
MSG_DOC_SIZE = 'حجم الملف أكبر من الحد المسموح (5 ميغابايت).'
MSG_DOC_MAGIC = 'محتوى الملف لا يطابق صيغته. يرجى رفع ملف صالح.'
MSG_TOO_MANY = 'عدد المستندات كبير جداً.'


# ── synthetic media (no production files) ───────────────────────────────────

def _enc(img, fmt, **kw):
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def _scene(w, h):
    img = Image.new('RGB', (w, h), (205, 190, 170))
    d = ImageDraw.Draw(img)
    d.ellipse((w // 4, h // 6, 3 * w // 4, h // 2), fill=(225, 185, 150))
    d.rectangle((w // 6, h // 2, 5 * w // 6, h), fill=(30, 60, 120))
    noise = Image.effect_noise((w, h), 25).convert('RGB')
    return Image.blend(img.filter(ImageFilter.GaussianBlur(2)), noise, 0.12)


def _exif(orientation=1):
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = 'SecretCam'
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = 'N', (33.0, 18.0, 0.0), 'E', (44.0, 22.0, 0.0)
    return exif.tobytes()


def _jpeg(w=1200, h=1600, orientation=1, quality=90):
    return _enc(_scene(w, h), 'JPEG', quality=quality, exif=_exif(orientation))


def _png(w=900, h=700):
    return _enc(_scene(w, h), 'PNG')


def _flat_jpeg(w, h):                        # huge dimensions, tiny file
    return _enc(Image.new('RGB', (w, h), (240, 240, 240)), 'JPEG', quality=50)


def _apng():
    a, b = _scene(300, 300), _scene(300, 300).rotate(90)
    return _enc(a, 'PNG', save_all=True, append_images=[b], duration=100)


def _xmp_jpeg(w=2400, h=1800):
    raw = _jpeg(w, h)
    xmp = (b'http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta xmlns:x="adobe:ns:meta/">'
           b'SecretXMP</x:xmpmeta>')
    seg = b'\xff\xe1' + (len(xmp) + 2).to_bytes(2, 'big') + xmp
    return raw[:2] + seg + raw[2:]


PDF = (b'%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n'
       b'2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n')


def _decode(data):
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def _sha(data):
    return hashlib.sha256(data).hexdigest()


class FakeStorage:
    """Records every Storage write/fetch/delete; serves what it stored."""

    def __init__(self):
        self.objects, self.writes, self.fetches, self.deletes = {}, [], [], []
        self.fail_bucket = None

    def upload(self, data, path, ctype, bucket=None):
        bucket = bucket or 'uploads'
        if bucket == self.fail_bucket:
            return None
        self.objects[(bucket, path)] = data
        self.writes.append((bucket, path, ctype, data))
        return f'{PUBLIC}{bucket}/{path}'

    def fetch(self, path, bucket=None):
        self.fetches.append((bucket, path))
        data = self.objects.get((bucket or 'uploads', path))
        return (data, 'application/octet-stream') if data is not None else (None, None)

    def delete(self, path, bucket=None):
        self.deletes.append((bucket, path))
        self.objects.pop((bucket or 'uploads', path), None)
        return True


# ─────────────────────────────────────────────────────────────────────────────
#  Shared DB fixture
# ─────────────────────────────────────────────────────────────────────────────

class _RegistrationBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['RATELIMIT_ENABLED'] = False
        cls.media = cls.app.config.get('SUPABASE_STORAGE_BUCKET_MEDIA') or 'school-media'
        with cls.app.app_context():
            cls.role_ids = {n: Role.query.filter_by(name=n).first().id
                            for n in ('school_admin', 'parent', 'teacher')}

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.fs = FakeStorage()
        mock.patch('app.utils.helpers._supabase_upload', side_effect=self.fs.upload).start()
        mock.patch('app.utils.helpers._supabase_fetch', side_effect=self.fs.fetch).start()
        mock.patch('app.utils.helpers._supabase_delete', side_effect=self.fs.delete).start()
        mock.patch('app.utils.helpers._supabase_sign', return_value=None).start()
        self.addCleanup(mock.patch.stopall)
        self.local_files = []
        self.ip = 0
        self.ids = {}
        with self.app.app_context():
            for key in ('a', 'b'):
                self._school(key)
            db.session.commit()

    def _school(self, key):
        s = self.sfx
        token = generate_token()
        school = School(school_name=f'Reg {key} {s}', code=f'RG{key}{s}'[:20], capacity=0,
                        is_active=True, external_registration_enabled=True,
                        registration_token_hash=hash_token(token))
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
        sec2 = Section(name=f'{key}2', grade_id=grade.id, school_id=school.id,
                       academic_year_id=year.id)
        db.session.add_all([sec, sec2])
        db.session.flush()

        def user(label, role):
            u = User(username=f'rg{label}{key}_{s}', email=f'rg{label}{key}_{s}@t.test',
                     full_name=f'{label} {key}', role_id=self.role_ids[role],
                     school_id=school.id, is_active=True)
            u.set_password(PASSWORD)
            db.session.add(u)
            db.session.flush()
            return u

        admin, staff = user('adm', 'school_admin'), user('stf', 'school_admin')
        parent, other_parent = user('par', 'parent'), user('opar', 'parent')
        teacher, other_teacher = user('t', 'teacher'), user('ot', 'teacher')
        for i, t in enumerate((teacher, other_teacher)):
            emp = Employee(school_id=school.id, employee_id=f'RG{key}{i}{s}',
                           full_name=f'T{i} {key}', base_salary=0, status='active',
                           user_id=t.id)
            db.session.add(emp)
            db.session.flush()
            db.session.execute(Section.__table__.update()
                               .where(Section.id == (sec.id if i == 0 else sec2.id))
                               .values(teacher_id=emp.id))
        self.ids.update({
            f'token_{key}': token, f'school_{key}': school.id, f'year_{key}': year.id,
            f'grade_{key}': grade.id, f'sec_{key}': sec.id,
            f'admin_{key}': admin.username, f'staff_{key}': staff.username,
            f'staff_id_{key}': staff.id, f'parent_id_{key}': parent.id,
            f'other_parent_id_{key}': other_parent.id, f'teacher_id_{key}': teacher.id,
            f'other_teacher_id_{key}': other_teacher.id})

    def tearDown(self):
        for path in self.local_files:
            path.unlink(missing_ok=True)
        with self.app.app_context():
            db.session.rollback()
            for key in ('a', 'b'):
                sid = self.ids[f'school_{key}']
                uids = [u.id for u in User.query.execution_options(**OPTS)
                        .filter_by(school_id=sid).all()]
                sids = [st.id for st in Student.query.execution_options(**OPTS)
                        .filter_by(school_id=sid).all()]
                if sids:
                    db.session.execute(parent_students.delete().where(
                        parent_students.c.student_id.in_(sids)))
                if uids:
                    AuditLog.query.execution_options(**OPTS).filter(
                        AuditLog.user_id.in_(uids)).delete(synchronize_session=False)
                    db.session.execute(parent_students.delete().where(
                        parent_students.c.user_id.in_(uids)))
                db.session.execute(Section.__table__.update()
                                   .where(Section.school_id == sid).values(teacher_id=None))
                for model in (StudentRegistrationRequestDocument, StudentRegistrationRequest,
                              StudentDocument, Notification, AuditLog, Student,
                              UserBuildingAccess, SchoolBuilding, Section, Grade, Employee,
                              User, AcademicYear):
                    model.query.execution_options(**OPTS).filter_by(
                        school_id=sid).delete(synchronize_session=False)
                School.query.filter_by(id=sid).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _web(self, username_key):
        client = self.app.test_client()
        resp = client.post('/auth/login', data={'username': self.ids[username_key],
                                                'password': PASSWORD})
        self.assertEqual(resp.status_code, 302)
        return client

    def _post(self, key='a', photo=None, docs=(), token=None, client=None, **fields):
        """POST the public form. photo=(name, bytes); docs=[(type, name, bytes)]."""
        self.ip += 1
        nonce = uuid4().hex
        data = {'full_name': f'Applicant {uuid4().hex[:6]}', 'gender': 'male',
                'desired_grade_id': str(self.ids[f'grade_{key}']), 'submission_nonce': nonce}
        data.update(fields)
        if photo is not None:
            data['photo'] = (io.BytesIO(photo[1]), photo[0], 'application/octet-stream')
        if docs:
            data['document_type[]'] = [t for t, _, _ in docs]
            data['document_file[]'] = [(io.BytesIO(b), n, 'application/octet-stream')
                                       for _, n, b in docs]
        resp = (client or self.app.test_client()).post(
            f"/register/{token or self.ids[f'token_{key}']}", data=data,
            content_type='multipart/form-data',
            environ_base={'REMOTE_ADDR': f'10.9.{self.ip // 250}.{self.ip % 250 + 1}'})
        return resp, nonce

    def _request(self, nonce, key='a'):
        with self.app.app_context():
            req = (StudentRegistrationRequest.query.execution_options(**OPTS)
                   .filter_by(school_id=self.ids[f'school_{key}'], submission_nonce=nonce)
                   .first())
            if req is None:
                return None
            docs = [(d.document_type, d.file_path) for d in
                    StudentRegistrationRequestDocument.query.execution_options(**OPTS)
                    .filter_by(request_id=req.id).order_by(StudentRegistrationRequestDocument.id)]
            return {'id': req.id, 'photo': req.student_photo_path, 'docs': docs,
                    'status': req.status, 'student_id': req.approved_student_id}

    def _assert_refused(self, resp, nonce, message=None, key='a'):
        self.assertEqual(resp.status_code, 200)
        if message:
            self.assertIn(message, resp.get_data(as_text=True))
        self.assertIsNone(self._request(nonce, key))
        self.assertEqual(self.fs.writes, [], 'a refused submission must write nothing')

    def _accepted(self, resp, nonce, key='a'):
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True)[:400])
        self.assertIn('/register/track/', resp.headers['Location'])
        req = self._request(nonce, key)
        self.assertIsNotNone(req)
        return req

    def _is_v2(self, value, school_id):
        with self.app.app_context():
            return is_v2_registration_photo(value, school_id)

    def _key(self, value):
        self.assertTrue(value.startswith(PUBLIC), value)
        bucket, _, key = value[len(PUBLIC):].partition('/')
        return bucket, key

    def _approve(self, req_id, key='a', client=None, link_parent=True):
        data = {'section_id': str(self.ids[f'sec_{key}'])}
        if link_parent:
            data.update(parent_choice='link', link_parent_id=str(self.ids[f'parent_id_{key}']))
        return (client or self._web(f'admin_{key}')).post(
            f'/admissions/{req_id}/approve', data=data)

    def _student(self, student_id):
        with self.app.app_context():
            st = db.session.get(Student, student_id, execution_options=OPTS)
            docs = sorted(d.file_path for d in StudentDocument.query.execution_options(
                **OPTS, include_all_years=True).filter_by(student_id=student_id))
            return {'photo': st.photo, 'photo_display': st.photo_display, 'docs': docs}


# ─────────────────────────────────────────────────────────────────────────────
#  Photo intake (1-16)
# ─────────────────────────────────────────────────────────────────────────────

class RegistrationPhotoTest(_RegistrationBase):

    def _photo_ok(self, name, raw):
        resp, nonce = self._post(photo=(name, raw))
        req = self._accepted(resp, nonce)
        self.assertEqual(len(self.fs.writes), 1)
        bucket, key = self._key(req['photo'])
        return req, bucket, key

    def test_01_02_03_14_16_valid_photos_stored_byte_identical_under_v2(self):
        sid = self.ids['school_a']
        for name, raw, ctype in (('p.jpg', _jpeg(), 'image/jpeg'),
                                 ('p.JPEG', _jpeg(800, 600), 'image/jpeg'),
                                 ('p.png', _png(), 'image/png')):
            with self.subTest(name=name):
                self.fs.writes.clear()
                req, bucket, key = self._photo_ok(name, raw)
                self.assertEqual(bucket, self.media)                        # private media bucket
                self.assertRegex(key, rf'^registration/{sid}/photos/v2/[0-9a-f]{{32}}\.'
                                      + name.rsplit('.', 1)[1].lower() + '$')
                w_bucket, w_path, w_ct, w_data = self.fs.writes[0]
                self.assertEqual(_sha(w_data), _sha(raw))                   # 14: byte-identical
                self.assertEqual(w_ct, ctype)
                self.assertTrue(self._is_v2(req['photo'], sid))
        # no display derivative is made during the anonymous submission
        self.assertFalse(any('students/display' in w[1] for w in self.fs.writes))

    def test_15_original_exif_gps_kept(self):
        raw = _jpeg(orientation=6)
        _, _, _ = self._photo_ok('p.jpg', raw)
        stored = self.fs.writes[0][3]
        self.assertIn(b'SecretCam', stored)
        exif = Image.open(io.BytesIO(stored)).getexif()
        self.assertEqual(exif.get(0x0112), 6)
        self.assertEqual(dict(exif.get_ifd(0x8825))[1], 'N')

    def test_04_05_06_other_formats_refused(self):
        gif = _enc(_scene(200, 200).convert('P'), 'GIF')
        webp = _enc(_scene(200, 200), 'WEBP')
        for name, raw in (('p.gif', gif), ('p.webp', webp), ('p.heic', b'\x00\x00\x00\x18ftypheic' * 50),
                          ('photo', _jpeg(300, 300))):
            with self.subTest(name=name):
                resp, nonce = self._post(photo=(name, raw))
                self._assert_refused(resp, nonce, MSG_PHOTO_TYPE)

    def test_07_08_09_invalid_content_refused_before_storage(self):
        good = _jpeg(800, 600)
        cases = (('corrupt.jpg', b'\xff\xd8\xff\xe0' + bytes(range(256)) * 40),
                 ('fake.jpg', b'this is not an image' * 50),
                 ('renamed.jpg', _png(300, 300)),                           # PNG named .jpg
                 ('renamed.png', good),                                     # JPEG named .png
                 ('truncated.jpg', good[:len(good) // 2]),
                 ('truncated.png', _png()[:3000]),
                 ('empty.jpg', b''))
        for name, raw in cases:
            with self.subTest(name=name):
                resp, nonce = self._post(photo=(name, raw))
                self._assert_refused(resp, nonce, MSG_PHOTO_INVALID)

    def test_10_over_5mb_refused(self):
        resp, nonce = self._post(photo=('big.jpg', b'\xff\xd8\xff\xe0' + b'\x00' * (5 * MB)))
        self._assert_refused(resp, nonce, MSG_PHOTO_SIZE)
        exact = _jpeg(600, 600)
        exact = exact[:-2] + b'\x00' * (5 * MB - len(exact)) + exact[-2:]   # exactly 5 MB
        self.assertEqual(len(exact), 5 * MB)
        resp, nonce = self._post(photo=('exact.jpg', exact))
        self.assertNotIn(MSG_PHOTO_SIZE, resp.get_data(as_text=True))

    def test_11_12_pixel_ceiling(self):
        _, _, key = self._photo_ok('edge.jpg', _flat_jpeg(8000, 5000))      # exactly 40 MP
        self.assertIn('/photos/v2/', key)
        self.fs.writes.clear()
        resp, nonce = self._post(photo=('over.jpg', _flat_jpeg(8001, 5000)))
        self._assert_refused(resp, nonce, PHOTO_MSG_TOO_LARGE)

    def test_13_animated_png_follows_student_photo_policy(self):
        # The Student photo validator accepts animated input (AI Face uses frame
        # 0); the public registration photo applies the very same rule.
        raw = _apng()
        self.assertIsNone(validate_student_photo(FileStorage(io.BytesIO(raw), filename='a.png')))
        _, _, key = self._photo_ok('anim.png', raw)
        self.assertEqual(_sha(self.fs.writes[0][3]), _sha(raw))


# ─────────────────────────────────────────────────────────────────────────────
#  Document intake (17-38)
# ─────────────────────────────────────────────────────────────────────────────

class RegistrationDocumentTest(_RegistrationBase):

    def _docs_ok(self, docs):
        resp, nonce = self._post(docs=docs)
        req = self._accepted(resp, nonce)
        return req

    def test_17_18_19_32_37_images_optimised_with_student_policy(self):
        sid = self.ids['school_a']
        raws = [('الهوية الوطنية', 'id.jpg', _jpeg(3000, 2000)),
                ('بطاقة السكن', 'card.JPEG', _jpeg(2200, 1400)),
                ('التقرير الطبي', 'scan.png', _png(2000, 1500))]
        req = self._docs_ok(raws)
        self.assertEqual(len(req['docs']), 3)
        for (doc_type, name, raw), (stored_type, value), write in zip(raws, req['docs'],
                                                                     self.fs.writes):
            with self.subTest(name=name):
                bucket, key = self._key(value)
                expected = optimize_document_image(raw)
                self.assertEqual(stored_type, doc_type)
                self.assertEqual(bucket, self.media)
                self.assertRegex(key, rf'^registration/{sid}/documents/v2/[0-9a-f]{{32}}\.'
                                      + expected.ext + '$')                  # 37
                self.assertEqual(write[3], expected.data)                   # 32: same 1200/q75 policy
                self.assertLess(len(write[3]), len(raw))
                self.assertLessEqual(max(_decode(write[3]).size), 1200)
        self.assertEqual(len(self.fs.writes), 3)                            # original never stored

    def test_20_38_pdf_byte_identical(self):
        req = self._docs_ok([('الوثيقة الدراسية', 'cert.pdf', PDF)])
        (_, value), = req['docs']
        bucket, key = self._key(value)
        self.assertTrue(key.endswith('.pdf') and '/documents/v2/' in key)
        self.assertEqual(_sha(self.fs.objects[(bucket, key)]), _sha(PDF))   # 38
        self.assertEqual(self.fs.writes[0][2], 'application/pdf')

    def test_21_to_28_refused_before_storage(self):
        cases = (('x.doc', b'\xd0\xcf\x11\xe0' * 100, MSG_DOC_TYPE),
                 ('x.docx', b'PK\x03\x04' * 100, MSG_DOC_TYPE),
                 ('x.gif', _enc(_scene(100, 100).convert('P'), 'GIF'), MSG_DOC_TYPE),
                 ('x.webp', _enc(_scene(100, 100), 'WEBP'), MSG_DOC_TYPE),
                 ('corrupt.jpg', b'\xff\xd8\xff\xe0' + bytes(range(256)) * 40, DOC_MSG_INVALID),
                 ('fake.png', b'not a png at all' * 30, MSG_DOC_MAGIC),
                 ('invalid.pdf', b'<html>not a pdf</html>' * 20, MSG_DOC_MAGIC),
                 ('big.pdf', b'%PDF-1.4\n' + b'0' * (5 * MB), MSG_DOC_SIZE),
                 ('truncated.jpg', _jpeg(1200, 900)[:4000], DOC_MSG_INVALID))
        for name, raw, message in cases:
            with self.subTest(name=name):
                resp, nonce = self._post(docs=[('وثيقة', name, raw)])
                self._assert_refused(resp, nonce, message)

    def test_29_more_than_four_refused(self):
        docs = [('وثيقة', f'd{i}.pdf', PDF) for i in range(5)]
        resp, nonce = self._post(docs=docs)
        self._assert_refused(resp, nonce, MSG_TOO_MANY)
        req = self._docs_ok(docs[:4])
        self.assertEqual(len(req['docs']), 4)

    def test_30_31_33_34_resize_orientation_metadata(self):
        req = self._docs_ok([('a', 'big.jpg', _xmp_jpeg(4032, 3024)),
                             ('b', 'small.jpg', _jpeg(800, 600)),
                             ('c', 'rotated.jpg', _jpeg(1200, 800, orientation=6))])
        big, small, rotated = (_decode(w[3]) for w in self.fs.writes)
        self.assertEqual(big.size, (1200, 900))                            # 30
        self.assertEqual(small.size, (800, 600))                           # 31: never upscaled
        self.assertEqual(rotated.size, (800, 1200))                        # 33: EXIF applied
        for w in self.fs.writes:                                           # 34
            img = _decode(w[3])
            self.assertEqual(len(img.getexif()), 0)
            self.assertNotIn('xmp', img.info)
            for marker in (b'SecretCam', b'SecretXMP', b'Exif\x00'):
                self.assertNotIn(marker, w[3])
        self.assertEqual(len(req['docs']), 3)

    def test_35_36_pixel_ceiling_and_animation(self):
        resp, nonce = self._post(docs=[('a', 'huge.jpg', _flat_jpeg(8001, 5000))])
        self._assert_refused(resp, nonce, DOC_MSG_TOO_LARGE)
        resp, nonce = self._post(docs=[('a', 'anim.png', _apng())])
        self._assert_refused(resp, nonce, MSG_ANIMATED)
        req = self._docs_ok([('a', 'edge.jpg', _flat_jpeg(8000, 5000))])   # exactly 40 MP
        self.assertLessEqual(max(_decode(self.fs.writes[0][3]).size), 1200)
        self.assertEqual(len(req['docs']), 1)


# ─────────────────────────────────────────────────────────────────────────────
#  Everything validated before the first Storage write (39-43) + cleanup
# ─────────────────────────────────────────────────────────────────────────────

class RegistrationAllBeforeStorageTest(_RegistrationBase):

    def test_39_invalid_fourth_document_stores_nothing(self):
        docs = [('a', 'a.jpg', _jpeg(900, 700)), ('b', 'b.pdf', PDF),
                ('c', 'c.png', _png(600, 400)), ('d', 'd.jpg', b'fake' * 100)]
        resp, nonce = self._post(photo=('p.jpg', _jpeg()), docs=docs)
        self._assert_refused(resp, nonce, MSG_DOC_MAGIC)

    def test_40_too_many_documents_stores_nothing(self):
        resp, nonce = self._post(photo=('p.jpg', _jpeg()),
                                 docs=[('d', f'{i}.pdf', PDF) for i in range(5)])
        self._assert_refused(resp, nonce, MSG_TOO_MANY)

    def test_41_42_43_overlong_values_store_nothing(self):
        with self.app.app_context():
            cols = StudentRegistrationRequest.__table__.c
            limits = {k: cols[k].type.length for k in (
                'full_name', 'nationality', 'phone', 'guardian_name', 'guardian_phone',
                'guardian_email', 'guardian_relation')}
            doc_type_max = StudentRegistrationRequestDocument.__table__.c.document_type.type.length
        self.assertEqual((limits['full_name'], limits['nationality'], limits['phone'],
                          limits['guardian_phone'], limits['guardian_email'],
                          limits['guardian_relation'], doc_type_max),
                         (200, 80, 30, 30, 180, 50, 100))
        media = dict(photo=('p.jpg', _jpeg(800, 600)), docs=[('وثيقة', 'd.pdf', PDF)])
        for field, limit in limits.items():
            with self.subTest(field=field):
                resp, nonce = self._post(**media, **{field: 'ب' * (limit + 1)})
                self._assert_refused(resp, nonce, MSG_TOO_LONG)
        resp, nonce = self._post(photo=media['photo'], docs=[('ت' * (doc_type_max + 1), 'd.pdf', PDF)])
        self._assert_refused(resp, nonce, MSG_TOO_LONG)
        # exactly at the column limits is accepted (nothing truncated)
        resp, nonce = self._post(photo=media['photo'], docs=[('ت' * doc_type_max, 'd.pdf', PDF)],
                                 **{f: 'ب' * n for f, n in limits.items()})
        req = self._accepted(resp, nonce)
        self.assertEqual(req['docs'][0][0], 'ت' * doc_type_max)

    def test_upload_failure_midway_removes_this_requests_objects(self):
        from app.utils import helpers
        real = helpers.save_uploaded_file
        calls = []

        def flaky(file, subfolder='misc', **kw):
            calls.append(subfolder)
            return None if len(calls) == 3 else real(file, subfolder, **kw)

        with mock.patch('app.blueprints.registration.save_uploaded_file', side_effect=flaky):
            resp, nonce = self._post(photo=('p.jpg', _jpeg(800, 600)),
                                     docs=[('a', 'a.pdf', PDF), ('b', 'b.pdf', PDF)])
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self._request(nonce))
        self.assertEqual(len(self.fs.writes), 2)
        self.assertEqual(sorted(self.fs.deletes), sorted((b, p) for b, p, _, _ in self.fs.writes))
        self.assertEqual(self.fs.objects, {})


# ─────────────────────────────────────────────────────────────────────────────
#  Approval (44-53) and AI Face (49)
# ─────────────────────────────────────────────────────────────────────────────

class RegistrationApprovalTest(_RegistrationBase):

    def _submit(self, photo_raw=None, docs=()):
        resp, nonce = self._post(photo=('p.jpg', photo_raw) if photo_raw else None, docs=docs)
        return self._accepted(resp, nonce)

    def test_44_to_47_50_v2_approval_reuses_originals_and_adds_display(self):
        raw = _jpeg(3024, 4032)
        img_doc = _jpeg(2400, 1800)
        req = self._submit(raw, docs=[('الهوية الوطنية', 'id.jpg', img_doc), ('شهادة', 'c.pdf', PDF)])
        writes_before = len(self.fs.writes)
        resp = self._approve(req['id'])
        self.assertEqual(resp.status_code, 302)
        after = self._request(self._nonce_of(req['id']))
        self.assertEqual(after['status'], 'approved')
        st = self._student(after['student_id'])
        self.assertEqual(st['photo'], req['photo'])                        # 44: the original
        self.assertEqual(after['photo'], req['photo'])                     # request unchanged
        # 45-47: exactly ONE new object — the display copy, current Student policy
        new = self.fs.writes[writes_before:]
        self.assertEqual(len(new), 1)
        d_bucket, d_path, d_ct, d_data = new[0]
        self.assertEqual((d_bucket, d_ct), ('uploads', 'image/webp'))
        self.assertRegex(d_path, r'^students/display/[0-9a-f]{32}\.webp$')
        self.assertEqual(st['photo_display'], f'{PUBLIC}uploads/{d_path}')
        self.assertEqual(d_data, make_display_photo(raw))                  # 47: 192/q45 policy
        self.assertEqual(_decode(d_data).size, (192, 192))                 # 46
        self.assertNotIn(b'SecretCam', d_data)
        # the fetched object was the registration original, byte-identical
        self.assertEqual(self.fs.fetches, [self._key(req['photo'])])
        self.assertEqual(_sha(self.fs.objects[self._key(req['photo'])]), _sha(raw))
        # 50: documents reuse the very same stored objects, nothing re-uploaded
        self.assertEqual(st['docs'], sorted(v for _, v in req['docs']))
        self.assertFalse(any('/documents/' in w[1] for w in new))

    def _nonce_of(self, req_id):
        with self.app.app_context():
            return db.session.get(StudentRegistrationRequest, req_id,
                                  execution_options=OPTS).submission_nonce

    def test_48_derivative_failures_never_fail_approval(self):
        for label in ('encode', 'fetch'):
            with self.subTest(label):
                self.fs.fetches.clear()
                req = self._submit(_jpeg(1200, 1600))
                writes_before = len(self.fs.writes)
                patches = ([mock.patch('app.utils.student_display_photo.make_display_photo',
                                       side_effect=RuntimeError('boom'))] if label == 'encode'
                           else [mock.patch.object(self.fs, 'objects', {})])
                with patches[0]:
                    resp = self._approve(req['id'])
                self.assertEqual(resp.status_code, 302)
                after = self._request(self._nonce_of(req['id']))
                self.assertEqual(after['status'], 'approved')
                st = self._student(after['student_id'])
                self.assertEqual(st['photo'], req['photo'])
                self.assertIsNone(st['photo_display'])
                self.assertEqual(len(self.fs.writes), writes_before)

    def test_idempotent_reapproval_makes_no_second_copy(self):
        req = self._submit(_jpeg(800, 1000))
        client = self._web('admin_a')
        self._approve(req['id'], client=client)
        writes = len(self.fs.writes)
        display = self._student(self._request(self._nonce_of(req['id']))['student_id'])['photo_display']
        self.assertTrue(display)
        self._approve(req['id'], client=client)
        self.assertEqual(len(self.fs.writes), writes)
        self.assertEqual(self._student(
            self._request(self._nonce_of(req['id']))['student_id'])['photo_display'], display)

    def test_49_aiface_source_is_student_photo_original(self):
        raw = _jpeg(1500, 2000)
        req = self._submit(raw)
        self._approve(req['id'])
        st = self._student(self._request(self._nonce_of(req['id']))['student_id'])
        self.assertTrue(st['photo_display'])
        for rel in ('app/services/aiface_sync.py', 'app/blueprints/attendance_devices/__init__.py',
                    'app/services/admission_approval.py'):
            self.assertNotIn('photo_display', (ROOT / rel).read_text(encoding='utf-8'), rel)
        from app.services.aiface_sync import prepare_photo_for_device
        self.fs.fetches.clear()
        with self.app.app_context():
            jpeg, info = prepare_photo_for_device(st['photo'], label='t')
        self.assertEqual(self.fs.fetches, [self._key(req['photo'])])
        self.assertIn('/photos/v2/', self.fs.fetches[0][1])
        self.assertLessEqual(max(Image.open(io.BytesIO(jpeg)).size), 640)

    # ── legacy media (51-53) ──────────────────────────────────────────────────

    def _legacy_request(self, status='pending'):
        sid = self.ids['school_a']
        photo = f'{PUBLIC}{self.media}/registration/{sid}/photos/{uuid4().hex}.jpg'
        doc = f'{PUBLIC}{self.media}/registration/{sid}/documents/{uuid4().hex}.jpg'
        with self.app.app_context():
            req = StudentRegistrationRequest(
                school_id=sid, academic_year_id=self.ids['year_a'],
                desired_grade_id=self.ids['grade_a'], full_name=f'Legacy {self.sfx}',
                student_photo_path=photo, status=status,
                tracking_token_hash=hash_token(generate_token()), submission_nonce=uuid4().hex)
            db.session.add(req)
            db.session.flush()
            db.session.add(StudentRegistrationRequestDocument(
                request_id=req.id, school_id=sid, document_type='الهوية الوطنية', file_path=doc))
            db.session.commit()
            return req.id, photo, doc

    def test_51_52_legacy_pending_approved_exactly_as_before(self):
        req_id, photo, doc = self._legacy_request()
        self.assertFalse(self._is_v2(photo, self.ids['school_a']))
        resp = self._approve(req_id)
        self.assertEqual(resp.status_code, 302)
        with self.app.app_context():
            req = db.session.get(StudentRegistrationRequest, req_id, execution_options=OPTS)
            self.assertEqual((req.status, req.student_photo_path), ('approved', photo))
            student_id = req.approved_student_id
        st = self._student(student_id)
        self.assertEqual((st['photo'], st['photo_display'], st['docs']), (photo, None, [doc]))
        self.assertEqual((self.fs.writes, self.fs.fetches, self.fs.deletes), ([], [], []))

    def test_53_old_approved_registration_untouched(self):
        req_id, photo, doc = self._legacy_request(status='approved')
        with self.app.app_context():
            st = Student(student_id=f'OLD-{self.sfx}', full_name='Old', school_id=self.ids['school_a'],
                         academic_year_id=self.ids['year_a'], status='active', photo=photo)
            db.session.add(st)
            db.session.flush()
            db.session.add(StudentDocument(student_id=st.id, school_id=self.ids['school_a'],
                                           academic_year_id=self.ids['year_a'],
                                           document_type='الهوية الوطنية', file_path=doc))
            db.session.execute(StudentRegistrationRequest.__table__.update()
                               .where(StudentRegistrationRequest.id == req_id)
                               .values(approved_student_id=st.id))
            db.session.commit()
            old_student = st.id

        def snapshot():
            with self.app.app_context():
                r = db.session.get(StudentRegistrationRequest, req_id, execution_options=OPTS)
                return (r.status, r.student_photo_path, r.updated_at, self._request(r.submission_nonce)['docs'],
                        self._student(old_student))
        before = snapshot()
        admin = self._web('admin_a')
        for path in ('/admissions/?status=all', f'/admissions/{req_id}', f'/students/{old_student}'):
            self.assertEqual(admin.get(path).status_code, 200, path)
        self._approve(req_id, client=admin)                                # idempotent: already
        new = self._submit(_jpeg(800, 1000))
        self._approve(new['id'], client=admin)
        self.assertEqual(snapshot(), before)
        self.assertNotIn(self._key(photo), self.fs.fetches)
        self.assertEqual(self.fs.deletes, [])


# ─────────────────────────────────────────────────────────────────────────────
#  Ownership / authorization (54-60)
# ─────────────────────────────────────────────────────────────────────────────

class RegistrationAuthorizationTest(_RegistrationBase):

    def _approved_photo(self):
        resp, nonce = self._post(photo=('p.jpg', _jpeg(800, 1000)))
        req = self._accepted(resp, nonce)
        self._approve(req['id'])
        return req['photo'], self._request(nonce)['student_id']

    def _can(self, user_id, value):
        from flask_login import login_user
        from app.utils.upload_access import can_access_upload
        with self.app.test_request_context('/'):
            user = db.session.get(User, user_id, execution_options=OPTS)
            login_user(user)
            return can_access_upload(user, value)

    def _user_id(self, username_key):
        with self.app.app_context():
            return User.query.execution_options(**OPTS).filter_by(
                username=self.ids[username_key]).first().id

    def test_54_cross_school_approval_denied(self):
        resp, nonce = self._post(key='b', photo=('p.jpg', _jpeg(600, 800)))
        req = self._accepted(resp, nonce, key='b')
        writes = len(self.fs.writes)
        admin_a = self._web('admin_a')
        self.assertEqual(admin_a.post(f"/admissions/{req['id']}/approve",
                                      data={'section_id': str(self.ids['sec_a'])}).status_code, 404)
        self.assertEqual(admin_a.get(f"/admissions/{req['id']}").status_code, 404)
        self.assertEqual(self._request(nonce, key='b')['status'], 'pending')
        self.assertEqual((len(self.fs.writes), self.fs.fetches), (writes, []))

    def test_55_shared_original_resolves_to_student_after_approval(self):
        from app.utils.upload_access import resolve_upload_owner
        resp, nonce = self._post(photo=('p.jpg', _jpeg(600, 800)))
        req = self._accepted(resp, nonce)
        with self.app.app_context():
            pending = resolve_upload_owner(req['photo'])
        self.assertEqual((pending['kind'], pending['student_id'], pending['school_id']),
                         ('registration_photo', None, self.ids['school_a']))
        self._approve(req['id'])
        student_id = self._request(nonce)['student_id']
        with self.app.app_context():
            owner = resolve_upload_owner(req['photo'])
        self.assertEqual((owner['kind'], owner['student_id'], owner['school_id']),
                         ('student_photo', student_id, self.ids['school_a']))

    def test_56_57_parent_and_teacher_scope(self):
        photo, _ = self._approved_photo()
        self.assertTrue(self._can(self.ids['parent_id_a'], photo))          # linked parent
        self.assertFalse(self._can(self.ids['other_parent_id_a'], photo))   # same school, not linked
        self.assertFalse(self._can(self.ids['parent_id_b'], photo))         # other school
        self.assertTrue(self._can(self.ids['teacher_id_a'], photo))         # assigned section
        self.assertFalse(self._can(self.ids['other_teacher_id_a'], photo))  # unassigned section
        self.assertFalse(self._can(self.ids['teacher_id_b'], photo))        # other school
        self.assertFalse(self._can(self._user_id('admin_b'), photo))        # other school staff

    def test_58_staff_building_restriction_applies_after_approval(self):
        photo, student_id = self._approved_photo()
        sid = self.ids['school_a']
        with self.app.app_context():
            b1 = SchoolBuilding(school_id=sid, name=f'B1 {self.sfx}')
            b2 = SchoolBuilding(school_id=sid, name=f'B2 {self.sfx}')
            db.session.add_all([b1, b2])
            db.session.flush()
            db.session.execute(Student.__table__.update().where(Student.id == student_id)
                               .values(building_id=b1.id))
            db.session.add(UserBuildingAccess(school_id=sid, user_id=self.ids['staff_id_a'],
                                              building_id=b2.id))
            db.session.execute(School.__table__.update().where(School.id == sid)
                               .values(enable_buildings=True))
            db.session.commit()
        self.assertFalse(self._can(self.ids['staff_id_a'], photo))          # other building
        self.assertTrue(self._can(self._user_id('admin_a'), photo))         # unrestricted staff

    def test_59_local_fallback_media_route_stays_fail_closed(self):
        # Supabase unavailable -> the v2 photo lands on local disk (uploads/...).
        self.fs.fail_bucket = self.media
        resp, nonce = self._post(photo=('p.jpg', _jpeg(600, 800)))
        req = self._accepted(resp, nonce)
        value = req['photo']
        self.assertRegex(value, r'^uploads/registration/\d+/photos/v2/[0-9a-f]{32}\.jpg$')
        self.local_files.append(pathlib.Path(self.app.root_path, 'static', *value.split('/')))
        self._approve(req['id'])
        st = self._student(self._request(nonce)['student_id'])
        self.assertEqual(st['photo'], value)
        self.assertTrue(st['photo_display'])                               # local original read
        with mock.patch.dict(self.app.config, {'PRIVATE_UPLOADS_ENABLED': True}):
            url = f'/media/{value}'
            self.assertEqual(self.app.test_client().get(url).status_code, 404)   # anonymous
            self.assertEqual(self._web('admin_b').get(url).status_code, 404)     # other school
            ok = self._web('admin_a').get(url)
            self.assertEqual(ok.status_code, 302)
            self.assertIn('/media-proxy/', ok.headers['Location'])


# ─────────────────────────────────────────────────────────────────────────────
#  Public route guarantees (61-63)
# ─────────────────────────────────────────────────────────────────────────────

class RegistrationPublicRouteTest(_RegistrationBase):

    def test_61_csrf_enforced_on_public_post(self):
        with mock.patch.dict(self.app.config, {'WTF_CSRF_ENABLED': True}):
            resp, nonce = self._post(photo=('p.jpg', _jpeg(600, 800)))      # no token
            self.assertNotIn('/register/track/', resp.headers.get('Location', ''))
            self.assertIsNone(self._request(nonce))
            self.assertEqual(self.fs.writes, [])
            client = self.app.test_client()
            page = client.get(f"/register/{self.ids['token_a']}").get_data(as_text=True)
            token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
            resp, nonce = self._post(photo=('p.jpg', _jpeg(600, 800)), client=client,
                                     csrf_token=token)
            self._accepted(resp, nonce)

    def test_62_token_scoping_unchanged(self):
        resp, nonce = self._post(key='a', desired_grade_id=str(self.ids['grade_b']),
                                 photo=('p.jpg', _jpeg(600, 800)))
        self._assert_refused(resp, nonce, 'يرجى اختيار الصف الدراسي.')
        resp, nonce = self._post(photo=('p.jpg', _jpeg(600, 800)), token='not-a-real-token')
        self.assertEqual(resp.status_code, 404)
        resp, nonce = self._post(photo=('p.jpg', _jpeg(600, 800)), school_id=str(self.ids['school_b']))
        self._assert_refused(resp, nonce)
        self.assertEqual(self.fs.writes, [])

    def test_63_rate_limits_unchanged(self):
        src = (ROOT / 'app/blueprints/registration/__init__.py').read_text(encoding='utf-8')
        self.assertIn("@registration_bp.route('/register/<token>', methods=['GET', 'POST'])\n"
                      "@limiter.limit('30 per hour; 8 per minute', key_func=_ip_token_key,\n"
                      "               methods=['POST'])\ndef form(token):", src)
        self.assertIn("@limiter.limit('40 per hour; 12 per minute', key_func=_ip_token_key)\n"
                      "def track(tracking_token):", src)

    def test_public_form_hints_match_backend(self):
        html = self.app.test_client().get(f"/register/{self.ids['token_a']}").get_data(as_text=True)
        self.assertIn('accept=".jpg,.jpeg,.png"', html)
        self.assertIn('accept=".pdf,.jpg,.jpeg,.png"', html)
        self.assertNotIn('.docx', html)
        self.assertNotIn('image/*', html)
        self.assertIn('الحد الأقصى 5 ميغابايت', html)
        self.assertIn('حتى 4 مستندات', html)
        # the internal Add Student wizard keeps its own inputs unchanged
        wizard = self._web('admin_a').get('/students/create').get_data(as_text=True)
        self.assertIn('accept="image/*"', wizard)
        self.assertIn('accept=".pdf,.jpg,.jpeg,.png,.doc,.docx"', wizard)


# ─────────────────────────────────────────────────────────────────────────────
#  Synthetic compression / timing report (Phase 23)
# ─────────────────────────────────────────────────────────────────────────────

class RegistrationCompressionReport(unittest.TestCase):

    @staticmethod
    def _text_page(w, h, lines=60, fmt='PNG'):
        img = Image.new('RGB', (w, h), (252, 252, 250))
        d = ImageDraw.Draw(img)
        for i in range(lines):
            y = 60 + i * (h - 120) // lines
            d.text((80, y), f'Line {i:02d}  Student record 2026-09-28  No. {1000 + i}  ABCDEFGHIJ',
                   fill=(20, 20, 20))
        return img

    def test_report(self):
        id_photo = _scene(4032, 3024)
        ImageDraw.Draw(id_photo).rectangle((900, 700, 3100, 2300), fill=(235, 235, 225))
        a4 = self._text_page(2480, 3508, lines=45)
        a4_photo = Image.blend(a4, Image.effect_noise(a4.size, 18).convert('RGB'), 0.08)
        cases = [('phone photo of ID (JPEG q92)', _enc(id_photo, 'JPEG', quality=92)),
                 ('A4 document photo (JPEG q90)', _enc(a4_photo, 'JPEG', quality=90)),
                 ('text-heavy scan (PNG)', _enc(self._text_page(2480, 3508, lines=90), 'PNG')),
                 ('small PNG', _enc(self._text_page(400, 300, lines=6), 'PNG'))]
        print('\n\nDOCUMENTS: case | input | output | reduction | time')
        for label, raw in cases:
            src = Image.open(io.BytesIO(raw))
            t0 = time.perf_counter()
            out = optimize_document_image(raw)
            ms = (time.perf_counter() - t0) * 1000
            print(f'{label} | {src.format} {src.width}x{src.height} {len(raw):,} B | '
                  f'{out.ext.upper()} {out.width}x{out.height} {len(out.data):,} B | '
                  f'{100 - 100 * len(out.data) / len(raw):.1f}% | {ms:.0f} ms')
            self.assertLessEqual(max(out.width, out.height), 1200)
        print('\nPHOTO display copy (original stored unchanged): input | display | reduction')
        for label, raw in (('phone portrait JPEG q90', _jpeg(3024, 4032)),
                           ('PNG portrait', _enc(_scene(1200, 1600), 'PNG'))):
            src = Image.open(io.BytesIO(raw))
            disp = make_display_photo(raw)
            out = _decode(disp)
            print(f'{label} | {src.format} {src.width}x{src.height} {len(raw):,} B | '
                  f'WEBP {out.width}x{out.height} {len(disp):,} B | '
                  f'{100 - 100 * len(disp) / len(raw):.1f}%')


if __name__ == '__main__':
    unittest.main()

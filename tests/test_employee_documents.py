"""
Employee document uploads — validation before Storage + image optimisation.

NEW uploads (Add Employee wizard, employee Documents page): pdf/jpg/jpeg/png
only, 5 MB, magic bytes, real image decode (40 MP, no animation), images stored
ONLY as the optimised <=1600 px WebP q88 (EXIF/GPS/XMP stripped), PDFs stored
byte-for-byte. Everything — title/type length included — is validated before
the first Storage write; one bad wizard document rejects the whole create.

OLD documents of every previously accepted type (DOC/DOCX included) are never
read, rewritten or deleted and still open through the unchanged read paths.
Storage is a recording mock. No network, no production data.
"""
import hashlib
import io
import re
import struct
import unittest
import zlib
from datetime import date
from unittest import mock
from urllib.parse import urlparse
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFilter

from app import create_app
from app.models import db, AcademicYear, AuditLog, Employee, EmployeeDocument, Role, School, User
from app.utils import employee_documents as ed
from app.utils import helpers
from app.utils.student_documents import (MSG_ANIMATED, MSG_INVALID, MSG_TOO_LARGE,
                                         STUDENT_DOC_MAX_PIXELS)

PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True}
SUPA = 'https://storage.test'
BASE = f'{SUPA}/storage/v1/object/public/uploads/'
MB5 = 5 * 1024 * 1024
DOCX = b'PK\x03\x04' + b'\x14\x00\x06\x00' + b'\x00' * 200          # zip header
DOC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\x00' * 200           # OLE2 header


def _url_for_path(data, path, ctype, bucket=None):
    return f'{BASE}{path}'


# ── synthetic document-style content (no production files) ──────────────────

def _enc(img, fmt, **kw):
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def _page(w, h, noise=True):
    img = Image.new('RGB', (w, h), (245, 243, 236))
    d = ImageDraw.Draw(img)
    for y in range(h // 12, h - h // 12, max(8, h // 60)):
        d.line((w // 12, y, w - w // 12, y), fill=(40, 40, 40), width=max(1, h // 700))
    d.rectangle((w // 12, h // 20, w // 3, h // 9), fill=(20, 60, 140))
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    if not noise:                               # clean scan: a PNG stays well under 5 MB
        return img
    return Image.blend(img, Image.effect_noise((w, h), 18).convert('RGB'), 0.05)


def _jpeg(w=2400, h=1800, orientation=None, quality=85):
    kw = {'quality': quality}
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        exif[0x010F] = 'SecretCam'
        exif.get_ifd(0x8825)[1] = 'N'
        kw['exif'] = exif.tobytes()
        kw['xmp'] = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><SecretXmp/></x:xmpmeta>'
    return _enc(_page(w, h), 'JPEG', **kw)


def _pdf(size=None):
    body = (b'%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n'
            b'2 0 obj << /Type /Pages /Kids [] /Count 0 >> endobj\ntrailer << /Root 1 0 R >>\n')
    tail = b'%%EOF\n'
    if size is None:
        return body + tail
    return body + b'%' + b'0' * (size - len(body) - len(tail) - 1) + tail


def _png_header_only(w, h):
    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 0, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00' * 64)) + chunk(b'IEND', b''))


def _apng():
    a, b = Image.new('RGB', (200, 150), 'red'), Image.new('RGB', (200, 150), 'blue')
    return _enc(a, 'PNG', save_all=True, append_images=[b], duration=100)


class _FS:
    def __init__(self, raw, name):
        self.filename, self.stream = name, io.BytesIO(raw)


def _invalid_cases():
    good = _jpeg(1200, 900)
    return [
        ('doc', 'x.doc', DOC, ed.MSG_UNSUPPORTED),
        ('docx', 'x.docx', DOCX, ed.MSG_UNSUPPORTED),
        ('gif', 'x.gif', _enc(Image.new('P', (40, 40)), 'GIF'), ed.MSG_UNSUPPORTED),
        ('webp input', 'x.webp', _enc(_page(400, 300), 'WEBP'), ed.MSG_UNSUPPORTED),
        ('no extension', 'scan', good, ed.MSG_UNSUPPORTED),
        ('fake jpg', 'x.jpg', b'this is plain text, not a picture' * 30, ed.MSG_MISMATCH),
        ('fake png', 'x.png', b'\x89PNG but not really' * 30, ed.MSG_MISMATCH),
        ('docx renamed pdf', 'x.pdf', DOCX, ed.MSG_MISMATCH),
        ('invalid pdf', 'x.pdf', b'<html>not a pdf</html>' * 20, ed.MSG_MISMATCH),
        ('corrupt jpeg', 'x.jpg', good[:600] + b'\x13\x37' * 3000, MSG_INVALID),
        ('truncated jpeg', 'x.jpg', good[: len(good) // 2], MSG_INVALID),
        ('empty', 'x.pdf', b'', ed.MSG_EMPTY),
        ('image > 5 MB', 'x.jpg', good + b'\x00' * (MB5 + 1 - len(good)), ed.MSG_TOO_BIG),
        ('pdf > 5 MB', 'x.pdf', _pdf(MB5 + 1), ed.MSG_TOO_BIG),
        ('45 MP png', 'x.png', _enc(Image.new('L', (9000, 5000)), 'PNG'), MSG_TOO_LARGE),
        ('900 MP header', 'x.png', _png_header_only(30000, 30000), MSG_TOO_LARGE),
        ('apng', 'x.png', _apng(), MSG_ANIMATED),
    ]


def _decode(data):
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


# ─────────────────────────────────────────────────────────────────────────────
#  Helper level — policy
# ─────────────────────────────────────────────────────────────────────────────

class EmployeeDocumentPolicyTest(unittest.TestCase):

    def test_policy_constants(self):
        self.assertEqual(ed.EMPLOYEE_DOC_ALLOWED_EXTS, {'pdf', 'jpg', 'jpeg', 'png'})
        self.assertEqual(ed.EMPLOYEE_DOC_STORED_EXTS, {'pdf', 'webp', 'png'})
        self.assertEqual(ed.EMPLOYEE_DOC_MAX_BYTES, MB5)
        self.assertEqual((ed.EMPLOYEE_DOC_TITLE_MAX, ed.EMPLOYEE_DOC_TYPE_MAX), (200, 80))
        self.assertEqual((ed.EMPLOYEE_DOC_IMAGE_MAX_SIDE, ed.EMPLOYEE_DOC_WEBP_QUALITY,
                          STUDENT_DOC_MAX_PIXELS), (1600, 88, 40_000_000))   # unchanged

    def test_6_15_22_23_invalid_uploads_refused(self):
        for label, name, raw, msg in _invalid_cases():
            with self.subTest(label):
                up, err = ed.prepare_employee_document(_FS(raw, name))
                self.assertIsNone(up)
                self.assertEqual(err, msg)

    def test_3_pdf_returned_unchanged(self):
        raw = _pdf()
        fs = _FS(raw, 'Scan.PDF')
        up, err = ed.prepare_employee_document(fs)
        self.assertIsNone(err)
        self.assertIs(up, fs)
        self.assertEqual(fs.stream.tell(), 0)
        self.assertEqual(fs.stream.read(), raw)

    def test_16_exact_5mb_boundary(self):
        at_pdf = _pdf(MB5)
        self.assertEqual(len(at_pdf), MB5)
        self.assertIsNone(ed.prepare_employee_document(_FS(at_pdf, 'a.pdf'))[1])
        self.assertEqual(ed.prepare_employee_document(_FS(at_pdf + b'\n', 'a.pdf'))[1],
                         ed.MSG_TOO_BIG)
        base = _jpeg(1200, 900)
        at_img = base + b'\x00' * (MB5 - len(base))          # trailing bytes after EOI
        up, err = ed.prepare_employee_document(_FS(at_img, 'a.jpg'))
        self.assertIsNone(err)
        self.assertEqual(up.filename.rsplit('.', 1)[1], 'webp')

    def test_1_17_18_images_optimised_never_upscaled(self):
        for (w, h, fmt, name) in ((3000, 2250, 'JPEG', 'a.jpg'), (2200, 3100, 'PNG', 'a.png'),
                                  (900, 700, 'JPEG', 'b.jpeg'), (1600, 1200, 'JPEG', 'c.jpg')):
            with self.subTest((w, h, fmt)):
                raw = _enc(_page(w, h, noise=fmt == 'JPEG'), fmt,
                           **({'quality': 88} if fmt == 'JPEG' else {}))
                self.assertLessEqual(len(raw), MB5)
                up, err = ed.prepare_employee_document(_FS(raw, name))
                self.assertIsNone(err)
                out = _decode(up.stream.getvalue())
                self.assertIn(out.format, ('WEBP', 'PNG'))
                self.assertEqual(up.filename, f'document.{out.format.lower()}')
                self.assertEqual(max(out.size), min(1600, max(w, h)))   # <=1600, never upscaled
                self.assertAlmostEqual(out.width / out.height, w / h, delta=0.01)

    def test_19_webp_q88_policy(self):
        raw = _jpeg(2400, 1800, quality=92)
        ref = Image.open(io.BytesIO(raw))
        ref.draft(ref.mode, (1600, 1600))
        ref.load()
        ref = ref.convert('RGB')
        ref.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        expect = {q: _enc(ref, 'WEBP', quality=q, method=4) for q in (88, 80, 95)}
        up, _ = ed.prepare_employee_document(_FS(raw, 'q.jpg'))
        got = up.stream.getvalue()
        self.assertEqual(got, expect[88])
        self.assertNotEqual(got, expect[80])
        self.assertNotEqual(got, expect[95])

    def test_20_21_orientation_applied_metadata_stripped(self):
        raw = _jpeg(3000, 2000, orientation=6)
        self.assertIn(b'SecretCam', raw)
        up, err = ed.prepare_employee_document(_FS(raw, 'o.jpg'))
        self.assertIsNone(err)
        data = up.stream.getvalue()
        out = _decode(data)
        self.assertEqual(out.size, (1067, 1600))                   # landscape → portrait
        self.assertEqual(len(out.getexif()), 0)
        for marker in (b'SecretCam', b'SecretXmp', b'Exif', b'xmpmeta'):
            self.assertNotIn(marker, data)

    def test_24_26_meta_validation(self):
        v = ed.validate_document_meta
        self.assertEqual(v('', ''), ed.MSG_TITLE_REQUIRED)
        self.assertEqual(v('x' * 201, ''), ed.MSG_TITLE_TOO_LONG)
        self.assertEqual(v('ok', 'x' * 81), ed.MSG_TYPE_TOO_LONG)
        self.assertIsNone(v('x' * 200, 'y' * 80))
        self.assertIsNone(v('', 'y' * 80, title_required=False))


# ─────────────────────────────────────────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────────────────────────────────────────

class EmployeeDocumentRouteTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config.update(RATELIMIT_ENABLED=False)
        with cls.app.app_context():
            cls.role_ids = {n: Role.query.filter_by(name=n).first().id
                            for n in ('school_admin', 'teacher')}

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.storage = mock.patch('app.utils.helpers._supabase_upload',
                                  side_effect=_url_for_path).start()
        self.fetch = mock.patch('app.utils.helpers._supabase_fetch',
                                return_value=(b'OLD-BYTES', 'application/octet-stream')).start()
        self.sign = mock.patch('app.utils.helpers._supabase_sign', return_value=None).start()
        self.delete = mock.patch('app.utils.helpers._supabase_delete').start()
        self.addCleanup(mock.patch.stopall)
        self.ids, self.old = {}, {}
        with self.app.app_context():
            for key in ('a', 'b'):
                self._school(key)
            db.session.commit()

    def _school(self, key):
        s = self.sfx
        school = School(school_name=f'EDoc {key} {s}', code=f'EO{key}{s}'[:20],
                        capacity=0, is_active=True)
        db.session.add(school)
        db.session.flush()
        db.session.add(AcademicYear(school_id=school.id, name=f'Y{key}{s}', is_current=True,
                                    start_date=date(2026, 8, 1), end_date=date(2027, 6, 30)))
        admin = User(username=f'eo{key}_{s}', email=f'eo{key}_{s}@t.test', full_name=f'adm {key}',
                     role_id=self.role_ids['school_admin'], school_id=school.id, is_active=True)
        admin.set_password(PASSWORD)
        emp = Employee(school_id=school.id, employee_id=f'EO{key}{s}', full_name=f'Emp {key}',
                       base_salary=0, status='active', photo=f'{BASE}employees/old-{s}.jpg')
        db.session.add_all([admin, emp])
        db.session.flush()
        old = {}
        for ext in ('doc', 'docx', 'jpg', 'pdf'):                 # every previously accepted kind
            k = f'employee_docs/old-{ext}-{s}-{uuid4().hex}.{ext}'
            d = EmployeeDocument(employee_id=emp.id, school_id=school.id, title=f'old {ext}',
                                 file_path=BASE + k, doc_type='قديم')
            db.session.add(d)
            db.session.flush()
            old[ext] = (d.id, k)
        self.old[key] = old
        self.ids.update({f'school_{key}': school.id, f'emp_{key}': emp.id,
                         f'admin_{key}': admin.username})

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
                for model in (AuditLog, EmployeeDocument, Employee, User, AcademicYear):
                    model.query.execution_options(**OPTS).filter_by(
                        school_id=sid).delete(synchronize_session=False)
                School.query.filter_by(id=sid).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _web(self, key='a'):
        client = self.app.test_client()
        resp = client.post('/auth/login', data={'username': self.ids[f'admin_{key}'],
                                                'password': PASSWORD})
        self.assertEqual(resp.status_code, 302)
        return client

    def _docs(self, key='a'):
        with self.app.app_context():
            return sorted((d.id, d.employee_id, d.title, d.file_path, d.doc_type)
                          for d in EmployeeDocument.query.execution_options(**OPTS)
                          .filter_by(school_id=self.ids[f'school_{key}']).all())

    def _employees(self):
        with self.app.app_context():
            return sorted((e.id, e.full_name) for e in Employee.query.execution_options(**OPTS)
                          .filter(Employee.school_id.in_([self.ids['school_a'],
                                                          self.ids['school_b']])).all())

    def _users(self):
        with self.app.app_context():
            return User.query.execution_options(**OPTS).filter(User.school_id.in_(
                [self.ids['school_a'], self.ids['school_b']])).count()

    def _create(self, docs, full_name=None, photo=None):
        full_name = full_name or f'New {uuid4().hex[:6]}'
        data = {'full_name': full_name, 'gender': 'male',
                'doc_type[]': [t for t, _, _ in docs],
                'doc_file[]': [(io.BytesIO(raw), name, 'application/octet-stream')
                               for _, name, raw in docs]}
        if photo is not None:
            data['photo'] = (io.BytesIO(photo), 'p.jpg', 'image/jpeg')
        resp = self._web().post('/employees/create', content_type='multipart/form-data',
                                data=data)
        with self.app.app_context():
            e = Employee.query.execution_options(**OPTS).filter_by(full_name=full_name).first()
            return resp, e.id if e else None

    def _post_doc(self, name, raw, title='شهادة', doc_type='شهادة خبرة', key='a', emp='a'):
        data = {'title': title, 'doc_type': doc_type}
        if name is not None:
            data['file'] = (io.BytesIO(raw), name, 'application/octet-stream')
        return self._web(key).post(f"/employees/{self.ids[f'emp_{emp}']}/documents",
                                   content_type='multipart/form-data', data=data,
                                   follow_redirects=True)

    def _doc_writes(self):
        return [c for c in self.storage.call_args_list if c.args[1].startswith('employee_docs/')]

    # ── 1-3: create stores optimised images / PDF as-is ──────────────────────

    def test_01_03_create_documents(self):
        jpeg, png, pdf = _jpeg(3000, 2250), _enc(_page(1000, 1400), 'PNG'), _pdf()
        resp, emp_id = self._create([('الهوية', 'id.jpg', jpeg), ('شهادة', 'cert.png', png),
                                     ('عقد', 'contract.pdf', pdf)])
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True)[-600:])
        writes = self._doc_writes()
        self.assertEqual(len(writes), 3)
        (d1, p1, c1), (d2, p2, c2), (d3, p3, c3) = [w.args for w in writes]
        self.assertRegex(p1, r'^employee_docs/[0-9a-f]{32}\.webp$')
        self.assertEqual(c1, 'image/webp')
        self.assertLessEqual(max(_decode(d1).size), 1600)
        self.assertRegex(p2, r'^employee_docs/[0-9a-f]{32}\.(webp|png)$')
        self.assertIn(c2, ('image/webp', 'image/png'))
        self.assertNotEqual(d2, png)                               # never the original
        self.assertRegex(p3, r'^employee_docs/[0-9a-f]{32}\.pdf$')
        self.assertEqual((hashlib.sha256(d3).digest(), c3),
                         (hashlib.sha256(pdf).digest(), 'application/pdf'))
        with self.app.app_context():
            rows = EmployeeDocument.query.execution_options(**OPTS).filter_by(
                employee_id=emp_id).order_by(EmployeeDocument.id).all()
            self.assertEqual([(r.title, r.doc_type, r.file_path) for r in rows],
                             [('الهوية', 'الهوية', BASE + p1), ('شهادة', 'شهادة', BASE + p2),
                              ('عقد', 'عقد', BASE + p3)])
            self.assertEqual({r.school_id for r in rows}, {self.ids['school_a']})

    # ── 27, 28 + validation before Storage on create ─────────────────────────

    def test_27_one_invalid_document_rejects_whole_create(self):
        before = (self._employees(), self._users(), self._docs())
        for label, name, raw, msg in _invalid_cases():
            with self.subTest(label):
                self.storage.reset_mock()
                resp, emp_id = self._create([('أ', 'ok.pdf', _pdf()), ('ب', name, raw),
                                             ('ج', 'ok.jpg', _jpeg(800, 600))],
                                            photo=_jpeg(600, 800))
                self.assertEqual(resp.status_code, 200)
                html = resp.get_data(as_text=True)
                self.assertIn(f'المستند رقم 2: {msg}', html)
                self.assertIsNone(emp_id)
                self.storage.assert_not_called()                   # not even the photo
        self.assertEqual((self._employees(), self._users(), self._docs()), before)

    def test_28_more_than_five_documents_rejected(self):
        resp, emp_id = self._create([(f't{i}', f'd{i}.pdf', _pdf()) for i in range(6)])
        self.assertEqual(resp.status_code, 200)
        self.assertIn('يمكن إضافة 5 مستندات كحد أقصى', resp.get_data(as_text=True))
        self.assertIsNone(emp_id)
        self.storage.assert_not_called()

    def test_create_doc_type_too_long_rejected_and_long_filename_title_capped(self):
        resp, emp_id = self._create([('x' * 81, 'a.pdf', _pdf())])
        self.assertEqual(resp.status_code, 200)
        self.assertIn(ed.MSG_TYPE_TOO_LONG, resp.get_data(as_text=True))
        self.assertIsNone(emp_id)
        self.storage.assert_not_called()
        long_stem = 'م' * 250                                       # was a 500 before
        resp, emp_id = self._create([('', f'{long_stem}.pdf', _pdf())])
        self.assertEqual(resp.status_code, 302)
        with self.app.app_context():
            (row,) = EmployeeDocument.query.execution_options(**OPTS).filter_by(
                employee_id=emp_id).all()
            self.assertEqual(row.title, 'م' * 200)
            self.assertIsNone(row.doc_type)

    def test_create_document_storage_failure_rejects_create(self):
        real = helpers.save_uploaded_file

        def fake(file, subfolder='misc', *a, **kw):
            return None if subfolder == 'employee_docs' else real(file, subfolder, *a, **kw)
        before = (self._employees(), self._docs())
        with mock.patch('app.utils.helpers.save_uploaded_file', side_effect=fake):
            resp, emp_id = self._create([('أ', 'a.pdf', _pdf())])
        self.assertEqual(resp.status_code, 200)
        self.assertIn(ed.MSG_SAVE_FAILED, resp.get_data(as_text=True))
        self.assertIsNone(emp_id)
        self.assertEqual((self._employees(), self._docs()), before)

    def test_35_photo_logic_unchanged_alongside_documents(self):
        photo = _jpeg(600, 800)
        resp, emp_id = self._create([('أ', 'a.pdf', _pdf())], photo=photo)
        self.assertEqual(resp.status_code, 302)
        paths = [c.args[1] for c in self.storage.call_args_list]
        self.assertRegex(paths[0], r'^employees/[0-9a-f]{32}\.jpg$')
        self.assertEqual(self.storage.call_args_list[0].args[0], photo)   # original verbatim
        self.assertRegex(paths[1], r'^employees/display/[0-9a-f]{32}\.webp$')
        self.assertRegex(paths[2], r'^employee_docs/[0-9a-f]{32}\.pdf$')

    # ── 4-15, 24-26: Documents page ───────────────────────────────────────────

    def test_04_05_documents_page_image_and_pdf(self):
        before = self._docs()
        resp = self._post_doc('id.jpeg', _jpeg(3200, 2400))
        self.assertIn('تم رفع المستند.', resp.get_data(as_text=True))
        pdf = _pdf()
        resp = self._post_doc('c.pdf', pdf, title='عقد', doc_type='')
        self.assertIn('تم رفع المستند.', resp.get_data(as_text=True))
        (img_w, pdf_w) = [c.args for c in self._doc_writes()]
        self.assertRegex(img_w[1], r'^employee_docs/[0-9a-f]{32}\.webp$')
        self.assertEqual(max(_decode(img_w[0]).size), 1600)
        self.assertEqual(hashlib.sha256(pdf_w[0]).digest(), hashlib.sha256(pdf).digest())
        new = [r for r in self._docs() if r not in before]
        self.assertEqual([(r[2], r[3], r[4]) for r in new],
                         [('شهادة', BASE + img_w[1], 'شهادة خبرة'), ('عقد', BASE + pdf_w[1], '')])
        self.assertEqual(before, [r for r in self._docs() if r in before])   # old rows intact

    def test_06_15_22_23_documents_page_refusals_before_storage(self):
        before = self._docs()
        for label, name, raw, msg in _invalid_cases():
            with self.subTest(label):
                resp = self._post_doc(name, raw)
                html = resp.get_data(as_text=True)
                self.assertIn(msg, html)
                self.assertNotIn('تم رفع المستند.', html)
                self.assertNotIn('يرجى إدخال العنوان واختيار ملف', html)   # old generic msg
        self.storage.assert_not_called()
        self.assertEqual(self._docs(), before)

    def test_24_26_title_type_file_checked_before_storage(self):
        before = self._docs()
        cases = [('whitespace title', {'title': '   '}, ed.MSG_TITLE_REQUIRED),
                 ('title > 200', {'title': 'ع' * 201}, ed.MSG_TITLE_TOO_LONG),
                 ('doc_type > 80', {'doc_type': 'ن' * 81}, ed.MSG_TYPE_TOO_LONG)]
        for label, kw, msg in cases:
            with self.subTest(label):
                html = self._post_doc('ok.pdf', _pdf(), **kw).get_data(as_text=True)
                self.assertIn(msg, html)
        self.assertIn(ed.MSG_FILE_REQUIRED,
                      self._post_doc(None, None).get_data(as_text=True))
        self.storage.assert_not_called()                           # no orphan objects
        self.assertEqual(self._docs(), before)
        html = self._post_doc('ok.pdf', _pdf(), title='ع' * 200,
                              doc_type='ن' * 80).get_data(as_text=True)
        self.assertIn('تم رفع المستند.', html)                     # exact limits accepted

    def test_documents_page_storage_failure_no_row(self):
        before = self._docs()
        with mock.patch('app.utils.helpers.save_uploaded_file', side_effect=OSError('down')):
            html = self._post_doc('ok.pdf', _pdf()).get_data(as_text=True)
        self.assertIn(ed.MSG_SAVE_FAILED, html)
        self.assertEqual(self._docs(), before)

    # ── 29-32: old documents untouched and still viewable ─────────────────────

    def test_29_32_old_documents_untouched_and_viewable(self):
        before = self._docs()
        client = self._web()
        self._post_doc('new.pdf', _pdf())                          # a new upload meanwhile
        page = client.get(f"/employees/{self.ids['emp_a']}/documents").get_data(as_text=True)
        for ext, (_, key) in self.old['a'].items():
            self.assertIn(key, page, ext)                          # every old doc still listed
        with mock.patch.dict(self.app.config, {'PRIVATE_UPLOADS_ENABLED': True,
                                               'SUPABASE_URL': SUPA,
                                               'SUPABASE_BUCKET': 'uploads'}):
            page = client.get(f"/employees/{self.ids['emp_a']}/documents").get_data(as_text=True)
            for ext, (_, key) in self.old['a'].items():
                with self.subTest(ext):
                    link = re.search(r'href="([^"]*' + re.escape(key) + r'[^"]*)"', page).group(1)
                    self.assertIn('/media-proxy/uploads/', link)     # signed link rendered
                    u = urlparse(link.replace('&amp;', '&'))
                    resp = self.app.test_client().get(f'{u.path}?{u.query}')
                    self.assertEqual((resp.status_code, resp.data), (200, b'OLD-BYTES'))
                    legacy = client.get(f'/files/uploads/{key}')     # hotfix path, authorised
                    self.assertEqual(legacy.status_code, 302)
        self.assertEqual([r for r in self._docs() if r in before], before)
        self.delete.assert_not_called()
        self.assertTrue(all(c.args[0].startswith('employee_docs/old-')
                            for c in self.fetch.call_args_list))   # only explicit views read

    # ── delete unchanged, 33 cross-school, 34 hotfix ──────────────────────────

    def test_delete_unchanged_row_only(self):
        doc_id, _ = self.old['a']['docx']
        resp = self._web().post(f'/employees/documents/{doc_id}/delete')
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn(doc_id, [r[0] for r in self._docs()])
        self.delete.assert_not_called()                            # Storage object left as before

    def test_33_cross_school_unchanged(self):
        before_a, before_b = self._docs('a'), self._docs('b')
        client = self._web('b')
        emp_a = self.ids['emp_a']
        self.assertEqual(client.get(f'/employees/{emp_a}/documents').status_code, 404)
        resp = client.post(f'/employees/{emp_a}/documents', content_type='multipart/form-data',
                           data={'title': 'x', 'file': (io.BytesIO(_pdf()), 'a.pdf', 'x')})
        self.assertEqual(resp.status_code, 404)
        doc_id, _ = self.old['a']['pdf']
        self.assertEqual(client.post(f'/employees/documents/{doc_id}/delete').status_code, 404)
        self.assertEqual((self._docs('a'), self._docs('b')), (before_a, before_b))
        self.storage.assert_not_called()

    def test_34_private_upload_hotfix_still_denies(self):
        _, key = self.old['a']['docx']
        with mock.patch.dict(self.app.config, {'PRIVATE_UPLOADS_ENABLED': True,
                                               'SUPABASE_URL': SUPA}):
            anon = self.app.test_client()
            other = self._web('b')
            for route in ('/media/uploads/', '/files/uploads/'):
                self.assertEqual(anon.get(route + key).status_code, 404)
                self.assertEqual(other.get(route + key).status_code, 404)


if __name__ == '__main__':
    unittest.main()

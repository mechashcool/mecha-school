"""
Student document image optimisation — Add Student, Edit → add, Replace.

Helper level (app.utils.student_documents.optimize_document_image): real
decode, pixel ceiling, animation refusal, EXIF orientation then strip,
<=1600 px without upscaling, WebP q88, alpha kept, smallest safe encoding for
flat graphics, fine text stays legible.

Route level: every path validates and processes BEFORE Storage; image
documents are stored only as the optimised copy; PDFs are stored byte-for-byte
(and are no longer silently dropped on Add Student); invalid files never reach
Storage or the DB; replacement keeps its soft-delete/history semantics; a
storage failure never leaves a broken reference; existing documents,
Student.photo, Student.photo_display and AI Face are untouched; cross-school
access stays refused.

Storage is a recording mock (no network). Run with ``-s`` for the size table.
"""
import hashlib
import io
import pathlib
import time
import unittest
from datetime import date
from unittest import mock
from uuid import uuid4

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageStat

from app import create_app
from app.models import (db, AcademicYear, AuditLog, Grade, Role, School, Section, Student,
                        StudentDocument, User)
from app.utils import student_documents as sd
from app.utils.student_documents import (MSG_ANIMATED, MSG_INVALID, MSG_TOO_LARGE,
                                         STUDENT_DOC_IMAGE_MAX_SIDE, STUDENT_DOC_MAX_PIXELS,
                                         STUDENT_DOC_WEBP_QUALITY, StudentDocumentImageError,
                                         optimize_document_image)

PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True}
BASE = 'https://storage.test/storage/v1/object/public/uploads/'
PDF = b'%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n' + bytes(range(256)) * 40 + b'\n%%EOF\n'
MAGIC_MISMATCH = 'محتوى الملف لا يطابق صيغته'
TYPE_REFUSED = 'نوع الملف غير مدعوم'


def _url_for_path(data, path, ctype, bucket=None):
    return f'{BASE}{path}'


# ── synthetic document images (no production files) ─────────────────────────

def _enc(img, fmt, **kw):
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def _decode(data):
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def _font(size):
    try:
        return ImageFont.truetype('arial.ttf', size)
    except OSError:
        return ImageFont.load_default()


def _document(w, h, *, photographed):
    """An ID-card / certificate style page with fine print."""
    img = Image.new('RGB', (w, h), (252, 252, 248))
    d = ImageDraw.Draw(img)
    m = w // 14
    d.rectangle((m // 2, m // 2, w - m // 2, h - m // 2), outline=(20, 40, 90), width=6)
    d.text((m, h // 25), 'جمهورية العراق — وزارة التربية — شهادة', font=_font(h // 38), fill='black')
    body, small = _font(h // 70), _font(h // 115)
    y = h // 8
    for i in range(1, 22):
        d.text((m, y), f'الحقل {i}: الاسم الكامل / Full name line {i} — 1234-5678-{i:04d}',
               font=body, fill=(15, 15, 15))
        d.text((m, y + h // 55), f'Fine print {i}: issued 2026-09-{i % 28 + 1:02d}, ref DOC/{i * 97}',
               font=small, fill=(40, 40, 40))
        y += h // 30
    if photographed:
        light = Image.radial_gradient('L').resize((w, h)).point(lambda v: 255 - v // 4)
        tint = Image.merge('RGB', (light, light, light.point(lambda v: v * 93 // 100)))
        img = Image.composite(img, tint, img.convert('L').point(lambda v: 255 if v < 128 else 0))
        img = Image.blend(img.filter(ImageFilter.GaussianBlur(0.7)),
                          Image.effect_noise((w, h), 25).convert('RGB'), 0.05)
    return img


def _jpeg_with_exif(w, h, orientation=1):
    img = _document(w, h, photographed=False)
    ImageDraw.Draw(img).rectangle((0, 0, w // 10, h // 10), fill=(255, 0, 0))   # top-left mark
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = 'SecretCam'
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = 'N', (33.0, 18.0, 0.0)
    return _enc(img, 'JPEG', quality=90, exif=exif.tobytes())


def _stamp_png(w=900, h=900):
    img = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((w // 10, h // 10, 9 * w // 10, 9 * h // 10), outline=(30, 60, 160, 255), width=18)
    d.text((w // 4, h // 2 - 20), 'OFFICIAL', font=_font(h // 9), fill=(30, 60, 160, 255))
    return _enc(img, 'PNG')


def _png_header_only(w, h):
    import struct
    import zlib

    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 0, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00' * 64)) + chunk(b'IEND', b''))


def _bad_documents():
    good = _enc(_document(1200, 1600, photographed=False), 'JPEG', quality=90)
    a, b = _document(300, 400, photographed=False), _document(400, 300, photographed=False).resize((300, 400))
    return [
        ('corrupt jpeg', 'id.jpg', good[: len(good) // 2], MSG_INVALID),
        ('jpeg header then junk', 'id.jpg', good[:600] + b'\x13\x37' * 3000, MSG_INVALID),
        ('text renamed jpg', 'id.jpg', b'hello, not an image' * 40, MAGIC_MISMATCH),
        ('exe renamed png', 'id.png', b'MZ\x90\x00' + b'\x00' * 200, MAGIC_MISMATCH),
        ('invalid pdf', 'report.pdf', b'not really a pdf' * 20, MAGIC_MISMATCH),
        ('animated png', 'id.png', _enc(a, 'PNG', save_all=True, append_images=[b]), MSG_ANIMATED),
        ('45 MP png', 'scan.png', _enc(Image.new('L', (9000, 5000), 255), 'PNG'), MSG_TOO_LARGE),
        ('900 MP header', 'scan.png', _png_header_only(30000, 30000), MSG_TOO_LARGE),
        ('docx', 'form.docx', b'PK\x03\x04' + b'\x00' * 100, TYPE_REFUSED),
        ('gif', 'id.gif', _enc(_document(200, 300, photographed=False), 'GIF'), TYPE_REFUSED),
    ]


# ─────────────────────────────────────────────────────────────────────────────
#  Helper level
# ─────────────────────────────────────────────────────────────────────────────

class DocumentImagePolicyTest(unittest.TestCase):

    def test_policy_constants(self):
        self.assertEqual((STUDENT_DOC_IMAGE_MAX_SIDE, STUDENT_DOC_WEBP_QUALITY,
                          STUDENT_DOC_MAX_PIXELS), (1600, 88, 40_000_000))

    def test_11_12_resize_and_never_upscale(self):
        out = optimize_document_image(_enc(_document(3024, 4032, photographed=True), 'JPEG', quality=90))
        self.assertEqual((out.ext, (out.width, out.height)), ('webp', (1200, 1600)))
        self.assertEqual(_decode(out.data).size, (1200, 1600))
        small = optimize_document_image(_enc(_document(900, 1200, photographed=True), 'JPEG', quality=90))
        self.assertEqual((small.width, small.height), (900, 1200))

    def test_13_quality_88_policy(self):
        raw = _enc(_document(2400, 3200, photographed=True), 'JPEG', quality=92)
        ref = Image.open(io.BytesIO(raw))
        ref.draft(ref.mode, (1600, 1600))
        ref = ref.convert('RGB')
        ref.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        expected = _enc(ref, 'WEBP', quality=88, method=4)
        self.assertEqual(optimize_document_image(raw).data, expected)
        self.assertNotEqual(expected, _enc(ref, 'WEBP', quality=80, method=4))

    def test_14_15_metadata_stripped_orientation_applied(self):
        raw = _jpeg_with_exif(2000, 1500, orientation=6)       # displays rotated 90° cw
        self.assertIn(b'SecretCam', raw)
        out = optimize_document_image(raw)
        img = _decode(out.data)
        self.assertEqual(img.size, (1200, 1600))
        self.assertEqual(len(img.getexif()), 0)
        self.assertNotIn('xmp', img.info)
        for marker in (b'SecretCam', b'Exif', b'EXIF'):
            self.assertNotIn(marker, out.data)
        r, _, b = img.convert('RGB').getpixel((img.width - 20, 20))      # mark now top-right
        self.assertTrue(r > 200 and b < 80, (r, b))

    def test_transparency_kept_and_flat_png_never_needlessly_larger(self):
        raw = _stamp_png()
        out = optimize_document_image(raw)
        img = _decode(out.data)
        self.assertEqual(img.mode, 'RGBA')
        self.assertLess(img.getpixel((2, 2))[3], 10)
        self.assertLessEqual(len(out.data), len(raw))
        flat = _enc(Image.new('RGB', (600, 400), (255, 255, 255)), 'PNG')
        self.assertLessEqual(len(optimize_document_image(flat).data), len(flat))

    def test_7_10_invalid_images_refused(self):
        for label, name, raw, msg in _bad_documents():
            if not name.endswith(('.jpg', '.png')) or msg == MAGIC_MISMATCH:
                continue                      # extension / magic handled by the route validator
            with self.subTest(label):
                with self.assertRaises(StudentDocumentImageError) as ctx:
                    optimize_document_image(raw)
                self.assertEqual(str(ctx.exception), msg)

    def test_16_fine_text_stays_legible(self):
        for label, src, fmt in (('scan PNG', _document(2480, 3508, photographed=False), 'PNG'),
                                ('photo JPEG', _document(3024, 4032, photographed=True), 'JPEG')):
            with self.subTest(label):
                raw = _enc(src, fmt, **({'quality': 90} if fmt == 'JPEG' else {}))
                out = _decode(optimize_document_image(raw).data)
                ref = _decode(raw)
                ref.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                self.assertEqual(out.size, ref.size)
                mae = ImageStat.Stat(ImageChops.difference(out.convert('L'),
                                                           ref.convert('L'))).mean[0]
                self.assertLess(mae, 2.5, f'{label}: mean abs error {mae:.2f}')

    def test_size_and_timing_report(self):
        cases = [
            ('A phone photo of a document JPEG q90', _enc(_document(3024, 4032, photographed=True), 'JPEG', quality=90)),
            ('B high-res scan PNG (A4 300 dpi)', _enc(_document(2480, 3508, photographed=False), 'PNG')),
            ('C text-heavy scan JPEG q95', _enc(_document(2480, 3508, photographed=False), 'JPEG', quality=95)),
            ('D transparent stamp PNG', _stamp_png()),
            ('E small document photo JPEG q85', _enc(_document(900, 1200, photographed=True), 'JPEG', quality=85)),
        ]
        print('\n\ncase | input | output | reduction | time')
        for label, raw in cases:
            src = Image.open(io.BytesIO(raw))
            t0 = time.perf_counter()
            out = optimize_document_image(raw)
            ms = (time.perf_counter() - t0) * 1000
            print(f'{label} | {src.format} {src.width}x{src.height} {len(raw):,} B | '
                  f'{out.ext.upper()} {out.width}x{out.height} {len(out.data):,} B | '
                  f'{100 - 100 * len(out.data) / len(raw):.1f}% | {ms:.0f} ms')
            self.assertLessEqual(max(out.width, out.height), 1600)


# ─────────────────────────────────────────────────────────────────────────────
#  Route level
# ─────────────────────────────────────────────────────────────────────────────

class StudentDocumentRouteTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['RATELIMIT_ENABLED'] = False
        with cls.app.app_context():
            cls.admin_role = Role.query.filter_by(name='school_admin').first().id

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.storage = mock.patch('app.utils.helpers._supabase_upload',
                                  side_effect=_url_for_path).start()
        self.fetch = mock.patch('app.utils.helpers._supabase_fetch',
                                return_value=(None, None)).start()
        self.delete = mock.patch('app.utils.helpers._supabase_delete').start()
        self.optimize = mock.patch('app.utils.student_documents.optimize_document_image',
                                   wraps=optimize_document_image).start()
        self.addCleanup(mock.patch.stopall)
        self.ids = {}
        with self.app.app_context():
            for key in ('a', 'b'):
                s = School(school_name=f'Doc {key} {self.sfx}', code=f'SD{key}{self.sfx}'[:20],
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
                sec = Section(name=f'{key}1', grade_id=g.id, school_id=s.id, academic_year_id=y.id)
                db.session.add(sec)
                db.session.flush()
                u = User(username=f'sd{key}_{self.sfx}', email=f'sd{key}_{self.sfx}@t.test',
                         full_name=f'admin {key}', role_id=self.admin_role, school_id=s.id,
                         is_active=True)
                u.set_password(PASSWORD)
                st = Student(student_id=f'SD{key}-{self.sfx}', full_name=f'Doc student {key}',
                             school_id=s.id, academic_year_id=y.id, section_id=sec.id,
                             status='active', photo=f'{BASE}students/orig-{key}.jpg')
                st.photo_display = f'{BASE}students/display/disp-{key}.webp'
                db.session.add_all([u, st])
                db.session.flush()
                old = StudentDocument(student_id=st.id, school_id=s.id, academic_year_id=y.id,
                                      document_type='الهوية الوطنية',
                                      file_path=f'{BASE}students/documents/old-{key}-{self.sfx}.png')
                db.session.add(old)
                db.session.flush()
                self.ids.update({f'school_{key}': s.id, f'sec_{key}': sec.id,
                                 f'student_{key}': st.id, f'admin_{key}': u.username,
                                 f'olddoc_{key}': old.id})
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for key in ('a', 'b'):
                sid = self.ids[f'school_{key}']
                uids = [u.id for u in User.query.execution_options(**OPTS).filter_by(school_id=sid)]
                if uids:
                    AuditLog.query.execution_options(**OPTS).filter(
                        AuditLog.user_id.in_(uids)).delete(synchronize_session=False)
                for model in (StudentDocument, AuditLog, Student, Section, Grade, User, AcademicYear):
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

    def _create(self, client, docs, full_name=None, photo=None):
        full_name = full_name or f'New {uuid4().hex[:6]}'
        data = {'full_name': full_name, 'section_id': str(self.ids['sec_a']),
                'gender': 'male', 'date_of_birth': '2015-01-01',
                'document_type[]': [t for t, _, _ in docs],
                'document_file[]': [(io.BytesIO(raw), name) for _, name, raw in docs]}
        if photo is not None:
            data['photo'] = (io.BytesIO(photo), 'p.jpg', 'image/jpeg')
        resp = client.post('/students/create', content_type='multipart/form-data', data=data)
        with self.app.app_context():
            st = Student.query.execution_options(**OPTS).filter_by(full_name=full_name).first()
            return resp, (st.id if st else None)

    def _edit(self, client, docs, full_name='Edited'):
        data = {'full_name': full_name, 'section_id': str(self.ids['sec_a']), 'status': 'active'}
        if docs:
            data['document_type[]'] = [t for t, _, _ in docs]
            data['document_file[]'] = [(io.BytesIO(raw), name) for _, name, raw in docs]
        return client.post(f"/students/{self.ids['student_a']}/edit",
                           content_type='multipart/form-data', data=data)

    def _replace(self, client, doc_id, name, raw, student='student_a'):
        return client.post(f"/students/{self.ids[student]}/documents/{doc_id}/replace",
                           content_type='multipart/form-data',
                           data={'document_file': (io.BytesIO(raw), name)})

    def _docs(self, student_id):
        with self.app.app_context():
            return sorted((d.id, d.document_type, d.file_path, d.deleted_at is not None,
                           d.replaced_by_id)
                          for d in StudentDocument.query.execution_options(**OPTS)
                          .filter_by(student_id=student_id))

    def _all_docs(self):
        return {k: self._docs(self.ids[k]) for k in ('student_a', 'student_b')}

    def _stored(self, index=0):
        c = self.storage.call_args_list[index]
        return c.args[0], c.args[1], c.args[2]

    def _assert_optimised_doc(self, index=0):
        data, path, ctype = self._stored(index)
        self.assertTrue(path.startswith('students/documents/'), path)
        self.assertIn(ctype, ('image/webp', 'image/png'))
        self.assertTrue(path.endswith('.webp' if ctype == 'image/webp' else '.png'), path)
        img = _decode(data)
        self.assertLessEqual(max(img.size), 1600)
        self.assertEqual(len(img.getexif()), 0)
        return data, path

    # ── 1-3: Add Student ─────────────────────────────────────────────────────

    def test_01_02_03_create_jpeg_png_pdf(self):
        jpeg = _jpeg_with_exif(3024, 4032)
        png = _enc(_document(2480, 3508, photographed=False), 'PNG')
        resp, sid = self._create(self._web(), [('الهوية الوطنية', 'id.jpg', jpeg),
                                               ('بطاقة السكن', 'res.png', png),
                                               ('التقرير الطبي', 'report.pdf', PDF)])
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True)[-400:])
        self.assertEqual(self.storage.call_count, 3)
        d0, p0 = self._assert_optimised_doc(0)                      # 1: JPEG -> WebP
        self.assertEqual(self._stored(0)[2], 'image/webp')
        self.assertNotEqual(d0, jpeg)
        self.assertNotIn(b'SecretCam', d0)
        d1, p1 = self._assert_optimised_doc(1)                      # 2: PNG optimised
        self.assertLess(len(d1), len(png))
        pdf_data, pdf_path, pdf_ct = self._stored(2)                # 3: PDF byte-identical
        self.assertEqual(hashlib.sha256(pdf_data).digest(), hashlib.sha256(PDF).digest())
        self.assertEqual((pdf_ct, pdf_path.endswith('.pdf')), ('application/pdf', True))
        rows = {r[1]: r[2] for r in self._docs(sid)}
        self.assertEqual(rows, {'الهوية الوطنية': BASE + p0, 'بطاقة السكن': BASE + p1,
                                'التقرير الطبي': BASE + pdf_path})

    def test_create_refuses_invalid_documents_before_storage(self):
        client = self._web()
        for label, name, raw, msg in _bad_documents():
            with self.subTest(label):
                full_name = f'Bad {label} {self.sfx}'
                resp, sid = self._create(client, [('الهوية الوطنية', name, raw)], full_name=full_name)
                self.assertEqual(resp.status_code, 200)
                self.assertIn(msg, resp.get_data(as_text=True))
                self.assertIsNone(sid, 'student created despite an invalid document')
        self.storage.assert_not_called()

    # ── 4, 17: Edit ──────────────────────────────────────────────────────────

    def test_04_edit_adds_optimised_image(self):
        before = self._docs(self.ids['student_a'])
        resp = self._edit(self._web(), [('التقرير الطبي', 'med.jpg',
                                         _enc(_document(3024, 4032, photographed=True), 'JPEG', quality=90))])
        self.assertEqual(resp.status_code, 302)
        _, path = self._assert_optimised_doc(0)
        after = self._docs(self.ids['student_a'])
        self.assertEqual(len(after), len(before) + 1)
        self.assertIn(BASE + path, [r[2] for r in after])
        for row in before:
            self.assertIn(row, after)                                    # 18: old untouched

    def test_edit_refuses_invalid_documents_before_storage(self):
        client = self._web()
        for label, name, raw, msg in _bad_documents():
            with self.subTest(label):
                before = self._all_docs()
                resp = self._edit(client, [('الهوية الوطنية', name, raw)], full_name='Nope')
                self.assertEqual(resp.status_code, 302)
                self.assertIn(msg, client.get(resp.headers['Location']).get_data(as_text=True))
                self.assertEqual(self._all_docs(), before)
        self.storage.assert_not_called()

    def test_17_metadata_only_edit_processes_nothing(self):
        before = self._all_docs()
        self.assertEqual(self._edit(self._web(), None, full_name='Meta').status_code, 302)
        self.assertEqual(self._all_docs(), before)
        self.optimize.assert_not_called()
        self.storage.assert_not_called()

    # ── 5, 6, 21: Replace ────────────────────────────────────────────────────

    def test_05_21_replace_image_optimised_history_kept(self):
        old_id = self.ids['olddoc_a']
        (old_row,) = [r for r in self._docs(self.ids['student_a']) if r[0] == old_id]
        raw = _jpeg_with_exif(3024, 4032)
        resp = self._replace(self._web(), old_id, 'new-id.jpg', raw)
        self.assertEqual(resp.status_code, 302)
        data, path = self._assert_optimised_doc(0)
        self.assertEqual(self._stored(0)[2], 'image/webp')
        self.assertNotEqual(data, raw)                                   # original never stored
        self.assertNotIn(b'SecretCam', data)
        rows = {r[0]: r for r in self._docs(self.ids['student_a'])}
        new = [r for r in rows.values() if r[2] == BASE + path]
        self.assertEqual(len(new), 1)
        new_id = new[0][0]
        self.assertEqual(new[0][1], 'الهوية الوطنية')
        self.assertFalse(new[0][3])                                      # new row active
        old_after = rows[old_id]
        self.assertEqual(old_after[2], old_row[2])                       # old path unchanged
        self.assertTrue(old_after[3])                                    # soft-deleted
        self.assertEqual(old_after[4], new_id)                           # replaced_by_id
        self.delete.assert_not_called()                                  # old object kept

    def test_06_replace_pdf_byte_identical(self):
        resp = self._replace(self._web(), self.ids['olddoc_a'], 'new.pdf', PDF)
        self.assertEqual(resp.status_code, 302)
        data, path, ctype = self._stored(0)
        self.assertEqual((data, ctype, path.endswith('.pdf')), (PDF, 'application/pdf', True))
        self.optimize.assert_not_called()

    def test_replace_refuses_invalid_documents_before_storage(self):
        client = self._web()
        for label, name, raw, msg in _bad_documents():
            with self.subTest(label):
                before = self._all_docs()
                resp = self._replace(client, self.ids['olddoc_a'], name, raw)
                self.assertEqual(resp.status_code, 302)
                self.assertIn(msg, client.get(resp.headers['Location']).get_data(as_text=True))
                self.assertEqual(self._all_docs(), before)
        self.storage.assert_not_called()

    # ── 19, 20: failure safety ───────────────────────────────────────────────

    def test_19_processing_failure_stores_nothing(self):
        raw = _enc(_document(1200, 1600, photographed=False), 'JPEG', quality=90)
        client = self._web()
        with mock.patch('app.utils.student_documents.optimize_document_image',
                        side_effect=RuntimeError('boom')):
            before = self._all_docs()
            resp, sid = self._create(client, [('الهوية الوطنية', 'id.jpg', raw)])
            self.assertEqual((resp.status_code, sid), (200, None))
            self.assertIn(MSG_INVALID, resp.get_data(as_text=True))
            self._edit(client, [('الهوية الوطنية', 'id.jpg', raw)])
            self._replace(client, self.ids['olddoc_a'], 'id.jpg', raw)
            self.assertEqual(self._all_docs(), before)
        self.storage.assert_not_called()

    def test_20_storage_failure_leaves_no_broken_reference(self):
        from app.utils import helpers
        real = helpers.save_uploaded_file

        def fail_documents(file, subfolder='misc', **kw):
            if subfolder == 'students/documents':
                return None
            return real(file, subfolder, **kw)

        raw = _enc(_document(1200, 1600, photographed=False), 'JPEG', quality=90)
        client = self._web()
        with mock.patch('app.blueprints.students.save_uploaded_file', side_effect=fail_documents):
            resp, sid = self._create(client, [('الهوية الوطنية', 'id.jpg', raw)])
            self.assertEqual(resp.status_code, 302)                      # student kept
            self.assertIsNotNone(sid)
            self.assertEqual(self._docs(sid), [])                        # no row
            self.assertIn('تعذّر رفع المستمسكات', client.get(resp.headers['Location'])
                          .get_data(as_text=True))                       # not silent
            before = self._all_docs()
            self._edit(client, [('الهوية الوطنية', 'id.jpg', raw)])
            self._replace(client, self.ids['olddoc_a'], 'id.jpg', raw)
            self.assertEqual(self._all_docs(), before)                   # old still active

    # ── 22-25: isolation, photos, AI Face ────────────────────────────────────

    def test_22_cross_school_replace_refused(self):
        before = self._all_docs()
        resp = self._replace(self._web('a'), self.ids['olddoc_b'], 'x.png', _stamp_png(),
                             student='student_b')
        self.assertIn(resp.status_code, (403, 404))
        resp = self._replace(self._web('a'), self.ids['olddoc_b'], 'x.png', _stamp_png())
        self.assertEqual(resp.status_code, 404)                          # doc of another student
        self.assertEqual(self._all_docs(), before)
        self.storage.assert_not_called()
        self.optimize.assert_not_called()

    def test_23_24_student_photo_and_display_unaffected(self):
        with self.app.app_context():
            st = db.session.get(Student, self.ids['student_a'], execution_options=OPTS)
            before = (st.photo, st.photo_display)
        self._edit(self._web(), [('التقرير الطبي', 'm.jpg',
                                  _enc(_document(1200, 1600, photographed=True), 'JPEG'))])
        with self.app.app_context():
            st = db.session.get(Student, self.ids['student_a'], execution_options=OPTS)
            self.assertEqual((st.photo, st.photo_display), before)
        # create with a photo AND a document: photo bytes untouched, display still made
        photo = _jpeg_with_exif(1200, 1600)
        self.storage.reset_mock()
        resp, sid = self._create(self._web(), [('الهوية الوطنية', 'id.jpg', photo)], photo=photo)
        self.assertEqual(resp.status_code, 302)
        paths = [c.args[1] for c in self.storage.call_args_list]
        (orig_call,) = [c for c in self.storage.call_args_list
                        if c.args[1].startswith('students/') and '/' not in c.args[1][9:]]
        self.assertEqual(orig_call.args[0], photo)                       # original byte-identical
        self.assertTrue(any(p.startswith('students/display/') for p in paths))
        self.assertTrue(any(p.startswith('students/documents/') for p in paths))

    def test_25_aiface_and_other_features_do_not_use_document_processing(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        # The public registration intake reuses this policy on purpose (NEW v2
        # registration documents, see tests/test_registration_media.py), so it is
        # no longer in this list; approval still never processes documents.
        for rel in ('app/services/aiface_sync.py', 'app/blueprints/attendance_devices/__init__.py',
                    'app/services/admission_approval.py',
                    'app/blueprints/employees/__init__.py', 'app/utils/helpers.py',
                    'app/utils/student_photo.py', 'app/utils/student_display_photo.py'):
            src = (root / rel).read_text(encoding='utf-8')
            for name in ('app.utils.student_documents', 'optimize_document_image',
                         'prepare_student_document_upload'):
                self.assertNotIn(name, src, f'{rel} uses {name}')

"""
Homework image attachments: optimised once at upload; PDFs untouched.

Helper level (app.utils.homework_attachments.prepare_homework_upload):
image attachments reuse the School Board optimiser byte-for-byte (same policy:
real decode, pixel/animation guards, EXIF orientation then strip, <=1600 px
longest side without upscaling, WebP q80, alpha kept); PDF / other uploads are
returned untouched.

Route level — web POST /homework/create, /homework/<id>/edit and mobile
POST /api/mobile/v1/teacher/homework, PUT /teacher/homework/<id>:
only the optimised WebP reaches Storage (image/webp, .webp object); PDFs are
stored byte-identical as before; invalid images never reach Storage, the DB or
the notification path; edits without a new file never reprocess; mobile JSON
field names unchanged; authorization and cross-school behaviour unchanged.

Storage and notifications are recording mocks: no network, no files written.
Run with ``-s`` to see the size / timing table.
"""
import io
import pathlib
import time
import unittest
from datetime import date, timedelta
from unittest import mock
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from werkzeug.datastructures import FileStorage

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (db, AcademicYear, AuditLog, Employee, Grade, Homework,
                        Notification, Role, School, Section, Student, Subject,
                        User, parent_students, teacher_subjects)
from app.utils import board_images, helpers
from app.utils.board_images import MSG_ANIMATED, MSG_INVALID, MSG_TOO_LARGE, optimize_board_image
from app.utils.homework_attachments import HomeworkImageError, prepare_homework_upload

PASSWORD = 'Test1234!'
FAKE_URL = 'https://storage.test/uploads/homework/stored-object'
OPTS = {'bypass_tenant_scope': True}
MP4 = b'\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom' + b'\x00' * 64
PDF = (b'%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n'
       + bytes(range(256)) * 8 + b'\n%%EOF\n')
TYPE_ERR = 'نوع الملف غير مسموح'

MOBILE_CREATE_KEYS = {'id', 'title', 'description', 'subject_id', 'subject_name',
                      'section_id', 'section_name', 'grade_name', 'publish_date',
                      'due_date', 'attachment_url', 'attachment_name', 'attachment_type'}
MOBILE_UPDATE_KEYS = MOBILE_CREATE_KEYS - {'publish_date'}
PARENT_ITEM_KEYS = {'id', 'homework_id', 'title', 'subject', 'subject_name',
                    'teacher_name', 'grade_name', 'section_name', 'assigned_at',
                    'publish_date', 'due_date', 'description', 'status',
                    'attachment_url', 'attachment_type', 'file_name', 'file_size',
                    'is_pdf', 'submitted_status'}


# ── synthetic images (no production files) ───────────────────────────────────

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


def _worksheet(w, h, *, photographed):
    """A homework page: heading, numbered questions, equations, a table.

    photographed=True adds uneven lighting, paper tint and sensor grain, like
    a phone photo of a printed sheet; False is a clean scan / export.
    """
    img = Image.new('RGB', (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    m = w // 14
    d.text((m, h // 30), 'الواجب المنزلي — الرياضيات — الصف الخامس', font=_font(h // 40), fill='black')
    body, small = _font(h // 70), _font(h // 110)
    y = h // 9
    for i in range(1, 19):
        d.text((m, y), f'س{i}: احسب ناتج  {i * 7} × {i + 3} + {i * 11} ÷ 11  ثم اكتب الخطوات.',
                font=body, fill=(20, 20, 20))
        d.text((m, y + h // 60), f'Q{i}: Solve x^2 + {i}x - {i * 2} = 0 and check your answer.',
                font=small, fill=(40, 40, 40))
        y += h // 30
    top = y + h // 40
    for r in range(6):                                   # answer table
        d.line((m, top + r * h // 40, w - m, top + r * h // 40), fill='black', width=2)
    for c in range(5):
        x = m + c * (w - 2 * m) // 4
        d.line((x, top, x, top + 5 * h // 40), fill='black', width=2)
    if not photographed:
        return img
    light = Image.radial_gradient('L').resize((w, h)).point(lambda v: 255 - v // 4)
    tint = Image.merge('RGB', (light, light, light.point(lambda v: v * 92 // 100)))
    img = Image.composite(img, tint, img.convert('L').point(lambda v: 255 if v < 128 else 0))
    img = img.filter(ImageFilter.GaussianBlur(0.8))
    return Image.blend(img, Image.effect_noise((w, h), 25).convert('RGB'), 0.06)


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
    exif[0x0112] = orientation
    exif[0x010F] = 'SecretCam'
    exif[0x0110] = 'Model X'
    xmp = b'<x:xmpmeta xmlns:x="adobe:ns:meta/">secret</x:xmpmeta>'
    return _enc(img, 'JPEG', quality=90, exif=exif.tobytes(), xmp=xmp)


def _fs(raw, name, mimetype='application/octet-stream'):
    return FileStorage(io.BytesIO(raw), filename=name, content_type=mimetype)


def _jpeg(w=1200, h=900):
    return _enc(_photo(w, h), 'JPEG', quality=90)


def _bad_uploads():
    good = _jpeg()
    a, b = _photo(200, 100), _photo(200, 100).rotate(180)
    return [
        ('corrupt jpg', 'hw.jpg', good[: len(good) // 2], MSG_INVALID),
        ('text renamed jpg', 'hw.jpg', b'hello, this is not an image' * 50, MSG_INVALID),
        ('video renamed jpg', 'hw.jpg', MP4, MSG_INVALID),
        ('pdf renamed png', 'hw.png', PDF, MSG_INVALID),
        ('animated webp', 'hw.webp',
         _enc(a, 'WEBP', save_all=True, append_images=[b], duration=100), MSG_ANIMATED),
        ('animated png', 'hw.png',
         _enc(a, 'PNG', save_all=True, append_images=[b], duration=100), MSG_ANIMATED),
        ('45 MP pixel bomb', 'hw.png', _enc(Image.new('L', (9000, 5000)), 'PNG'), MSG_TOO_LARGE),
    ]


# ─────────────────────────────────────────────────────────────────────────────
#  Helper level
# ─────────────────────────────────────────────────────────────────────────────

class HomeworkImageHelperTest(unittest.TestCase):

    def _ok(self, raw, name):
        out = prepare_homework_upload(_fs(raw, name, 'image/jpeg'))
        self.assertEqual((out.filename, out.mimetype), ('homework.webp', 'image/webp'))
        data = out.read()
        img = _decode(data)
        self.assertEqual(img.format, 'WEBP')                          # 9
        return data, img

    def test_07_large_image_longest_side_1600(self):
        _, img = self._ok(_enc(_photo(4000, 3000), 'JPEG', quality=92), 'big.jpg')
        self.assertEqual(img.size, (1600, 1200))
        _, tall = self._ok(_enc(_worksheet(1500, 3000, photographed=False), 'PNG'), 'p.png')
        self.assertEqual(tall.size, (800, 1600))

    def test_08_small_image_not_upscaled(self):
        _, img = self._ok(_enc(_photo(800, 600), 'PNG'), 's.png')
        self.assertEqual(img.size, (800, 600))

    def test_12_policy_is_the_board_policy_byte_for_byte(self):
        self.assertEqual((board_images.BOARD_IMAGE_MAX_SIDE,
                          board_images.BOARD_IMAGE_WEBP_QUALITY), (1600, 80))
        for name, raw in (('a.jpg', _enc(_photo(3000, 2000), 'JPEG', quality=92)),
                          ('b.png', _enc(_worksheet(1240, 1754, photographed=False), 'PNG')),
                          ('c.webp', _enc(_photo(2000, 1500), 'WEBP', quality=95))):
            with self.subTest(name):
                data, _ = self._ok(raw, name)
                self.assertEqual(data, optimize_board_image(raw).data)

    def test_13_exif_orientation_applied(self):
        # stored landscape 2000x1000, orientation 6 = display rotated 90° clockwise
        _, img = self._ok(_jpeg_with_exif(2000, 1000, orientation=6), 'o.jpg')
        self.assertEqual(img.size, (800, 1600))
        rgb = img.convert('RGB')
        r, _, b = rgb.getpixel((img.width - 20, 20))                # mark now top-right
        self.assertTrue(r > 200 and b < 60, (r, b))

    def test_14_exif_xmp_stripped(self):
        raw = _jpeg_with_exif(1200, 800)
        src = Image.open(io.BytesIO(raw))
        self.assertIn('exif', src.info)                             # precondition
        self.assertIn(b'SecretCam', raw)
        data, img = self._ok(raw, 'm.jpg')
        self.assertNotIn('exif', img.info)
        self.assertNotIn('xmp', img.info)
        self.assertEqual(len(img.getexif()), 0)
        for marker in (b'SecretCam', b'xmpmeta', b'Exif', b'EXIF', b'XMP '):
            self.assertNotIn(marker, data)

    def test_15_transparency_preserved(self):
        _, img = self._ok(_enc(_transparent(1800, 1200), 'PNG'), 't.png')
        self.assertEqual(img.mode, 'RGBA')
        self.assertLess(img.getpixel((2, 2))[3], 10)
        self.assertGreater(img.getpixel((img.width // 2, img.height // 5))[3], 245)

    def test_16_to_20_invalid_images_refused(self):
        for label, name, raw, msg in _bad_uploads():
            with self.subTest(label):
                with self.assertRaises(HomeworkImageError) as ctx:
                    prepare_homework_upload(_fs(raw, name, 'image/jpeg'))
                self.assertEqual(str(ctx.exception), msg)

    def test_22_pdf_and_other_uploads_returned_untouched(self):
        with mock.patch('app.utils.homework_attachments.optimize_board_image') as opt:
            for name in ('sheet.pdf', 'SHEET.PDF', 'clip.gif', 'noext'):
                with self.subTest(name):
                    f = _fs(PDF, name, 'application/pdf')
                    self.assertIs(prepare_homework_upload(f), f)
                    self.assertEqual(f.stream.tell(), 0)            # not even read
            opt.assert_not_called()

    def test_size_and_timing_report(self):
        cases = [
            ('A phone photo JPEG q92', 'a.jpg', _enc(_photo(4032, 3024), 'JPEG', quality=92)),
            ('B photographed page JPEG q90', 'b.jpg',
             _enc(_worksheet(3024, 4032, photographed=True), 'JPEG', quality=90)),
            ('C scanned page PNG (A4 300dpi)', 'c.png',
             _enc(_worksheet(2480, 3508, photographed=False), 'PNG')),
            ('D transparent PNG', 'd.png', _enc(_transparent(1800, 1200), 'PNG')),
            ('E small screenshot PNG', 'e.png',
             _enc(_worksheet(1080, 1440, photographed=False), 'PNG')),
        ]
        print('\n\ncase | input | output | reduction | time')
        for label, name, raw in cases:
            src = Image.open(io.BytesIO(raw))
            t0 = time.perf_counter()
            data = prepare_homework_upload(_fs(raw, name)).read()
            ms = (time.perf_counter() - t0) * 1000
            out = _decode(data)
            pct = 100 - 100 * len(data) / len(raw)
            print(f'{label} | {src.format} {src.width}x{src.height} {len(raw):,} B | '
                  f'{out.format} {out.width}x{out.height} {len(data):,} B | '
                  f'{pct:.1f}% | {ms:.0f} ms')
            self.assertLessEqual(max(out.size), 1600)
            if not label.startswith('D'):
                # Photos and pages shrink. A small flat transparent graphic can
                # come out larger as WebP (board policy, unchanged): reported.
                self.assertLess(len(data), len(raw))

    def test_text_page_stays_legible(self):
        """WebP q80 adds little error on top of the 1600 px resize itself."""
        from PIL import ImageChops, ImageStat
        for label, src in (('scan', _worksheet(2480, 3508, photographed=False)),
                           ('photo', _worksheet(3024, 4032, photographed=True))):
            with self.subTest(label):
                raw = _enc(src, 'PNG')
                out = _decode(prepare_homework_upload(_fs(raw, 'p.png')).read())
                ref = src.copy()
                ref.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                self.assertEqual(out.size, ref.size)
                diff = ImageChops.difference(out.convert('L'), ref.convert('L'))
                mae = ImageStat.Stat(diff).mean[0]
                self.assertLess(mae, 3.0, f'{label}: mean abs error {mae:.2f}')


# ─────────────────────────────────────────────────────────────────────────────
#  Route level (web + mobile teacher + mobile parent read)
# ─────────────────────────────────────────────────────────────────────────────

class HomeworkImageRouteTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        with cls.app.app_context():
            cls.role_ids = {n: Role.query.filter_by(name=n).first().id
                            for n in ('teacher', 'school_admin', 'parent')}

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.storage = mock.patch('app.utils.helpers._supabase_upload',
                                  return_value=FAKE_URL).start()
        self.web_save = mock.patch('app.blueprints.homework.save_uploaded_file',
                                   wraps=helpers.save_uploaded_file).start()
        self.optimize = mock.patch('app.utils.homework_attachments.optimize_board_image',
                                   wraps=optimize_board_image).start()
        self.web_notify = mock.patch('app.blueprints.homework._notify_homework_parents').start()
        self.dispatch = mock.patch('app.services.async_dispatch.submit').start()
        self.addCleanup(mock.patch.stopall)
        with self.app.app_context():
            self.ids = {}
            for key in ('a', 'b'):
                self._school(key)
            db.session.commit()

    def _school(self, key):
        s = self.sfx
        school = School(school_name=f'HW Img {key} {s}', code=f'HW{key}{s}'[:20],
                        capacity=0, is_active=True)
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'Y {key} {s}', is_current=True,
                            start_date=date(2026, 8, 1), end_date=date(2027, 6, 30))
        db.session.add(year)
        db.session.flush()
        grade = Grade(school_id=school.id, academic_year_id=year.id, name=f'G{key}{s[:4]}')
        db.session.add(grade)
        db.session.flush()
        sec1 = Section(school_id=school.id, academic_year_id=year.id, grade_id=grade.id,
                       name=f'A{s[:4]}', capacity=30)
        sec2 = Section(school_id=school.id, academic_year_id=year.id, grade_id=grade.id,
                       name=f'B{s[:4]}', capacity=30)
        subj = Subject(school_id=school.id, academic_year_id=year.id, name=f'Math {key}',
                       code=f'M{key}{s[:6]}')
        db.session.add_all([sec1, sec2, subj])
        db.session.flush()

        def user(label, role):
            u = User(username=f'hw{label}{key}_{s}', email=f'hw{label}{key}_{s}@example.test',
                     full_name=f'{label} {key} {s}', role_id=self.role_ids[role],
                     school_id=school.id, is_active=True)
            u.set_password(PASSWORD)
            db.session.add(u)
            db.session.flush()
            return u

        teacher, teacher2 = user('t', 'teacher'), user('t2', 'teacher')
        admin, parent = user('adm', 'school_admin'), user('par', 'parent')
        emp = Employee(school_id=school.id, employee_id=f'E{key}{s}', full_name=f'T {key} {s}',
                       base_salary=0, status='active', user_id=teacher.id)
        emp2 = Employee(school_id=school.id, employee_id=f'F{key}{s}', full_name=f'T2 {key} {s}',
                        base_salary=0, status='active', user_id=teacher2.id)
        db.session.add_all([emp, emp2])
        db.session.flush()
        db.session.execute(Section.__table__.update().where(Section.id == sec1.id)
                           .values(teacher_id=emp.id))
        db.session.execute(Section.__table__.update().where(Section.id == sec2.id)
                           .values(teacher_id=emp2.id))
        db.session.execute(teacher_subjects.insert().values(
            employee_id=emp.id, subject_id=subj.id, section_id=sec1.id))
        db.session.execute(teacher_subjects.insert().values(
            employee_id=emp2.id, subject_id=subj.id, section_id=sec2.id))
        child = Student(student_id=f'S-{uuid4().hex[:10]}', full_name=f'Child {key}',
                        school_id=school.id, academic_year_id=year.id,
                        section_id=sec1.id, status='active')
        db.session.add(child)
        db.session.flush()
        db.session.execute(parent_students.insert().values(
            user_id=parent.id, student_id=child.id, relation='guardian'))
        hw = Homework(school_id=school.id, academic_year_id=year.id, teacher_id=emp.id,
                      subject_id=subj.id, section_id=sec1.id, title=f'Existing {key}',
                      publish_date=date.today() - timedelta(days=1),
                      due_date=date.today() + timedelta(days=5), is_active=True,
                      attachment_path=f'https://storage.test/uploads/homework/old-{key}.jpg',
                      attachment_type='image')
        db.session.add(hw)
        db.session.flush()
        self.ids.update({f'school_{key}': school.id, f'grade_{key}': grade.id,
                         f'sec_{key}': sec1.id, f'sec2_{key}': sec2.id,
                         f'subj_{key}': subj.id, f'hw_{key}': hw.id,
                         f'child_{key}': child.id})
        self.ids.update({f'{k}_{key}': u.id for k, u in (('teacher', teacher), ('teacher2', teacher2),
                                                         ('admin', admin), ('parent', parent))})

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
                for model in (AuditLog, Notification, Homework):
                    model.query.execution_options(**OPTS).filter_by(
                        school_id=sid).delete(synchronize_session=False)
                db.session.execute(parent_students.delete().where(
                    parent_students.c.user_id.in_(uids or [0])))
                db.session.execute(teacher_subjects.delete().where(
                    teacher_subjects.c.employee_id.in_(
                        db.session.query(Employee.id).filter(Employee.school_id == sid))))
                db.session.execute(Section.__table__.update()
                                   .where(Section.school_id == sid).values(teacher_id=None))
                for model in (Student, Section, Subject, Grade, Employee, User, AcademicYear):
                    model.query.execution_options(**OPTS).filter_by(
                        school_id=sid).delete(synchronize_session=False)
                School.query.filter_by(id=sid).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _web(self, key):
        client = self.app.test_client()
        with self.app.app_context():
            name = db.session.get(User, self.ids[key], execution_options=OPTS).username
        resp = client.post('/auth/login', data={'username': name, 'password': PASSWORD})
        self.assertIn(resp.status_code, (200, 302))
        return client

    def _jwt(self, key):
        with self.app.app_context():
            return {'Authorization': 'Bearer ' + encode_token(
                db.session.get(User, self.ids[key], execution_options=OPTS))}

    def _web_form(self, school='a', file=None, **over):
        data = {'title': f'HW {uuid4().hex[:6]}', 'description': 'd',
                'subject_id': str(self.ids[f'subj_{school}']),
                'grade_id': str(self.ids[f'grade_{school}']),
                'section_id': str(self.ids[f'sec_{school}']),
                'publish_date': date.today().isoformat(),
                'due_date': (date.today() + timedelta(days=3)).isoformat(), **over}
        if file is not None:
            name, raw = file
            data['attachment'] = (io.BytesIO(raw), name, 'image/jpeg')
        return data

    def _web_create(self, client, **kw):
        return client.post('/homework/create', data=self._web_form(**kw),
                           content_type='multipart/form-data')

    def _web_edit(self, client, hw_id, **kw):
        return client.post(f'/homework/{hw_id}/edit', data=self._web_form(**kw),
                           content_type='multipart/form-data')

    def _mobile_form(self, school='a', file=None, **over):
        data = {'title': f'MHW {uuid4().hex[:6]}', 'description': 'd',
                'section_id': str(self.ids[f'sec_{school}']),
                'subject_id': str(self.ids[f'subj_{school}']),
                'due_date': (date.today() + timedelta(days=3)).isoformat(), **over}
        if file is not None:
            name, raw = file
            data['attachment'] = (io.BytesIO(raw), name, 'image/jpeg')
        return data

    def _mobile_create(self, who='teacher_a', **kw):
        return self.app.test_client().post('/api/mobile/v1/teacher/homework',
                                           data=self._mobile_form(**kw),
                                           headers=self._jwt(who),
                                           content_type='multipart/form-data')

    def _mobile_update(self, hw_id, who='teacher_a', **kw):
        return self.app.test_client().put(f'/api/mobile/v1/teacher/homework/{hw_id}',
                                          data=self._mobile_form(**kw),
                                          headers=self._jwt(who),
                                          content_type='multipart/form-data')

    def _snapshot(self):
        with self.app.app_context():
            return sorted(
                (h.id, h.school_id, h.title, h.description, h.section_id, h.subject_id,
                 h.due_date, h.is_active, h.attachment_path, h.attachment_type)
                for h in Homework.query.execution_options(**OPTS).filter(
                    Homework.school_id.in_([self.ids['school_a'], self.ids['school_b']])).all())

    def _hw(self, hw_id):
        with self.app.app_context():
            return db.session.get(Homework, hw_id, execution_options=OPTS)

    def _hw_by_title(self, title):
        with self.app.app_context():
            return Homework.query.execution_options(**OPTS).filter_by(title=title).all()

    def _stored(self):
        """(bytes, object_path, content_type) of the single Storage upload."""
        self.storage.assert_called_once()
        data, path, ctype = self.storage.call_args.args
        self.assertEqual(self.storage.call_args.kwargs, {'bucket': None})
        return data, path, ctype

    def _assert_webp_stored(self, raw, size):
        data, path, ctype = self._stored()
        self.assertEqual(ctype, 'image/webp')                       # 10
        self.assertTrue(path.startswith('homework/') and path.endswith('.webp'), path)  # 11
        img = _decode(data)
        self.assertEqual((img.format, img.size), ('WEBP', size))    # 9
        self.assertNotEqual(data, raw)                              # original never stored
        self.assertEqual(data, optimize_board_image(raw).data)      # 12
        return data

    def _assert_nothing_happened(self, before):
        self.assertEqual(self._snapshot(), before, 'DB changed on a refused upload')
        self.storage.assert_not_called()
        self.web_notify.assert_not_called()
        self.dispatch.assert_not_called()

    # ── 1-3, 7, 8: web create ────────────────────────────────────────────────

    def test_01_07_web_create_jpeg_stored_as_1600_webp(self):
        raw = _enc(_photo(4000, 3000), 'JPEG', quality=92)
        form = self._web_form(file=('photo.jpg', raw))
        resp = self._web(
            'teacher_a').post('/homework/create', data=form, content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True)[:400])
        data = self._assert_webp_stored(raw, (1600, 1200))
        self.assertLess(len(data), len(raw) // 4)
        (row,) = self._hw_by_title(form['title'])
        self.assertEqual((row.attachment_path, row.attachment_type), (FAKE_URL, 'image'))
        self.web_notify.assert_called_once()

    def test_02_03_08_web_create_png_and_webp(self):
        for name, raw, size in (
                ('page.png', _enc(_worksheet(2480, 3508, photographed=False), 'PNG'), (1131, 1600)),
                ('pic.webp', _enc(_photo(3000, 2000), 'WEBP', quality=95), (1600, 1067)),
                ('small.png', _enc(_photo(800, 600), 'PNG'), (800, 600))):
            with self.subTest(name):
                self.storage.reset_mock()
                form = self._web_form(file=(name, raw))
                resp = self._web('teacher_a').post('/homework/create', data=form,
                                                   content_type='multipart/form-data')
                self.assertEqual(resp.status_code, 302)
                self._assert_webp_stored(raw, size)
                (row,) = self._hw_by_title(form['title'])
                self.assertEqual(row.attachment_type, 'image')

    def test_web_batch_create_optimises_once_for_all_sections(self):
        raw = _jpeg(2400, 1800)
        form = self._web_form(file=('p.jpg', raw), section_id='all')
        resp = self._web('admin_a').post('/homework/create', data=form,
                                         content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 302)
        self._assert_webp_stored(raw, (1600, 1200))
        self.optimize.assert_called_once()
        rows = self._hw_by_title(form['title'])
        self.assertEqual(len(rows), 2)
        self.assertEqual({(r.attachment_path, r.attachment_type) for r in rows},
                         {(FAKE_URL, 'image')})

    # ── 6: web edit replacement ──────────────────────────────────────────────

    def test_06_web_edit_replaces_with_optimised_webp(self):
        raw = _enc(_photo(3000, 4000), 'JPEG', quality=92)
        resp = self._web_edit(self._web('teacher_a'), self.ids['hw_a'], title='Edited',
                              file=('new.jpg', raw))
        self.assertEqual(resp.status_code, 302)
        self._assert_webp_stored(raw, (1200, 1600))
        row = self._hw(self.ids['hw_a'])
        self.assertEqual((row.title, row.attachment_path, row.attachment_type),
                         ('Edited', FAKE_URL, 'image'))

    # ── 4, 5: mobile teacher ─────────────────────────────────────────────────

    def test_04_24_mobile_create_jpeg_and_fields_unchanged(self):
        raw = _enc(_photo(4000, 3000), 'JPEG', quality=92)
        resp = self._mobile_create(file=('photo.jpg', raw))
        self.assertEqual(resp.status_code, 201, resp.get_json())
        body = resp.get_json()
        self.assertEqual(set(body), {'ok', 'message', 'homework'})
        self.assertEqual(set(body['homework']), MOBILE_CREATE_KEYS)
        self.assertEqual(body['homework']['attachment_type'], 'image')
        self.assertTrue(body['homework']['attachment_url'])
        self._assert_webp_stored(raw, (1600, 1200))
        row = self._hw(body['homework']['id'])
        self.assertEqual((row.attachment_path, row.attachment_type), (FAKE_URL, 'image'))
        self.dispatch.assert_called_once()

    def test_05_24_mobile_update_replaces_image_and_fields_unchanged(self):
        raw = _enc(_worksheet(2480, 3508, photographed=False), 'PNG')
        resp = self._mobile_update(self.ids['hw_a'], title='M edited', file=('p.png', raw))
        self.assertEqual(resp.status_code, 200, resp.get_json())
        body = resp.get_json()
        self.assertEqual(set(body), {'ok', 'homework'})
        self.assertEqual(set(body['homework']), MOBILE_UPDATE_KEYS)
        self._assert_webp_stored(raw, (1131, 1600))
        row = self._hw(self.ids['hw_a'])
        self.assertEqual((row.title, row.attachment_path, row.attachment_type),
                         ('M edited', FAKE_URL, 'image'))

    def test_24_parent_homework_fields_unchanged(self):
        self._mobile_update(self.ids['hw_a'], title='Parent sees', file=('p.jpg', _jpeg()))
        resp = self.app.test_client().get(
            f"/api/mobile/v1/parent/children/{self.ids['child_a']}/homework",
            headers=self._jwt('parent_a'))
        self.assertEqual(resp.status_code, 200, resp.get_json())
        items = resp.get_json()['homework']
        (item,) = [i for i in items if i['id'] == self.ids['hw_a']]
        self.assertEqual(set(item), PARENT_ITEM_KEYS)
        self.assertEqual((item['attachment_type'], item['is_pdf']), ('image', False))
        self.assertTrue(item['attachment_url'])

    # ── 16-21: invalid images refused before Storage / DB / notifications ────

    def test_16_to_21_web_create_refuses_invalid_images(self):
        client = self._web('teacher_a')
        for label, name, raw, msg in _bad_uploads():
            with self.subTest(label):
                before = self._snapshot()
                resp = self._web_create(client, file=(name, raw))
                self.assertEqual(resp.status_code, 200)
                self.assertIn(msg, resp.get_data(as_text=True))
                self._assert_nothing_happened(before)
                self.web_save.assert_not_called()

    def test_16_to_21_web_edit_refuses_invalid_images(self):
        client = self._web('teacher_a')
        for label, name, raw, msg in _bad_uploads():
            with self.subTest(label):
                before = self._snapshot()
                resp = self._web_edit(client, self.ids['hw_a'], title='Nope', file=(name, raw))
                self.assertEqual(resp.status_code, 200)
                self.assertIn(msg, resp.get_data(as_text=True))
                self._assert_nothing_happened(before)            # row untouched

    def test_16_to_21_mobile_refuses_invalid_images(self):
        for label, name, raw, msg in _bad_uploads():
            with self.subTest(label):
                before = self._snapshot()
                resp = self._mobile_create(file=(name, raw))
                self.assertEqual((resp.status_code, resp.get_json()),
                                 (400, {'ok': False, 'error': msg}))
                resp = self._mobile_update(self.ids['hw_a'], title='Nope', file=(name, raw))
                self.assertEqual((resp.status_code, resp.get_json()),
                                 (400, {'ok': False, 'error': msg}))
                self._assert_nothing_happened(before)

    def test_disallowed_extension_errors_unchanged(self):
        before = self._snapshot()
        gif = _enc(_photo(200, 100), 'GIF')
        resp = self._web_create(self._web('teacher_a'), file=('a.gif', gif))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(TYPE_ERR, resp.get_data(as_text=True))
        resp = self._mobile_create(file=('a.gif', gif))
        self.assertEqual((resp.status_code, resp.get_json()['error']),
                         (400, 'invalid_attachment — allowed: jpg, jpeg, png, webp, pdf'))
        self._assert_nothing_happened(before)
        self.optimize.assert_not_called()

    # ── 22: PDF pass-through ─────────────────────────────────────────────────

    def test_22_pdf_stored_byte_identical_everywhere(self):
        client = self._web('teacher_a')
        paths = [
            lambda: self._web_create(client, file=('sheet.pdf', PDF)),
            lambda: self._web_edit(client, self.ids['hw_a'], file=('sheet.pdf', PDF)),
            lambda: self._mobile_create(file=('sheet.pdf', PDF)),
            lambda: self._mobile_update(self.ids['hw_a'], file=('sheet.pdf', PDF)),
        ]
        for i, call in enumerate(paths):
            with self.subTest(i):
                self.storage.reset_mock()
                resp = call()
                self.assertIn(resp.status_code, (200, 201, 302))
                data, path, ctype = self._stored()
                self.assertEqual(data, PDF)
                self.assertEqual(ctype, 'application/pdf')
                self.assertTrue(path.startswith('homework/') and path.endswith('.pdf'), path)
        self.optimize.assert_not_called()
        row = self._hw(self.ids['hw_a'])
        self.assertEqual((row.attachment_path, row.attachment_type), (FAKE_URL, 'pdf'))
        resp = self.app.test_client().get(
            f"/api/mobile/v1/parent/children/{self.ids['child_a']}/homework",
            headers=self._jwt('parent_a'))
        (item,) = [i for i in resp.get_json()['homework'] if i['id'] == self.ids['hw_a']]
        self.assertEqual((item['attachment_type'], item['is_pdf']), ('pdf', True))

    # ── 23: edits without a new file never reprocess ─────────────────────────

    def test_23_edit_without_file_keeps_attachment(self):
        old = self._hw(self.ids['hw_a']).attachment_path
        resp = self._web_edit(self._web('teacher_a'), self.ids['hw_a'], title='Web meta')
        self.assertEqual(resp.status_code, 302)
        resp = self._mobile_update(self.ids['hw_a'], title='Multipart meta')
        self.assertEqual(resp.status_code, 200)
        resp = self.app.test_client().put(
            f"/api/mobile/v1/teacher/homework/{self.ids['hw_a']}",
            json={k: v for k, v in self._mobile_form(title='Json meta').items()},
            headers=self._jwt('teacher_a'))
        self.assertEqual(resp.status_code, 200)
        row = self._hw(self.ids['hw_a'])
        self.assertEqual((row.title, row.attachment_path, row.attachment_type),
                         ('Json meta', old, 'image'))
        self.storage.assert_not_called()
        self.optimize.assert_not_called()

    # ── 25, 26: authorization and cross-school unchanged ────────────────────

    def test_25_authorization_unchanged(self):
        before = self._snapshot()
        img = ('p.jpg', _jpeg())
        # unauthenticated
        resp = self.app.test_client().post('/homework/create', data=self._web_form(file=img),
                                           content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/auth/login', resp.headers['Location'])
        resp = self.app.test_client().post('/api/mobile/v1/teacher/homework',
                                           data=self._mobile_form(file=img),
                                           content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 401)
        # wrong role
        resp = self._web_create(self._web('parent_a'), file=img)
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('/homework', resp.headers['Location'])
        self.assertEqual(self._mobile_create(who='parent_a', file=img).status_code, 403)
        # same school, not the owner
        resp = self._web_edit(self._web('teacher2_a'), self.ids['hw_a'], file=img)
        self.assertEqual(resp.status_code, 403)
        resp = self._mobile_update(self.ids['hw_a'], who='teacher2_a', file=img)
        self.assertEqual(resp.status_code, 404)
        # a teacher cannot target a section they are not assigned to
        resp = self._web_create(self._web('teacher_a'), file=img,
                                section_id=str(self.ids['sec2_a']))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._mobile_create(file=img, section_id=str(self.ids['sec2_a']))
                         .status_code, 403)
        self._assert_nothing_happened(before)
        self.optimize.assert_not_called()

    def test_26_cross_school_unchanged(self):
        before = self._snapshot()
        img = ('p.jpg', _jpeg())
        resp = self._web_edit(self._web('teacher_a'), self.ids['hw_b'], file=img)
        self.assertEqual(resp.status_code, 404)
        resp = self._web_edit(self._web('admin_a'), self.ids['hw_b'], file=img)
        self.assertEqual(resp.status_code, 404)
        resp = self._mobile_update(self.ids['hw_b'], file=img)
        self.assertEqual(resp.status_code, 404)
        resp = self._web_create(self._web('admin_a'), file=img, section_id=str(self.ids['sec_b']),
                                grade_id=str(self.ids['grade_a']))
        self.assertEqual(resp.status_code, 200)
        resp = self._mobile_create(file=img, section_id=str(self.ids['sec_b']),
                                   subject_id=str(self.ids['subj_b']))
        self.assertEqual(resp.status_code, 403)
        resp = self.app.test_client().get(
            f"/api/mobile/v1/parent/children/{self.ids['child_b']}/homework",
            headers=self._jwt('parent_a'))
        self.assertIn(resp.status_code, (403, 404))
        self._assert_nothing_happened(before)
        self.optimize.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
#  28: biometric / profile image paths never use the homework optimiser
# ─────────────────────────────────────────────────────────────────────────────

class BiometricPathsUntouchedTest(unittest.TestCase):

    ROOT = pathlib.Path(__file__).resolve().parent.parent

    def test_28_student_employee_registration_aiface_do_not_use_it(self):
        for rel in ('app/blueprints/students/__init__.py',
                    'app/blueprints/employees/__init__.py',
                    'app/blueprints/registration/__init__.py',
                    'app/services/admission_approval.py',
                    'app/services/aiface_sync.py',
                    'app/blueprints/attendance_devices/__init__.py',
                    'app/utils/helpers.py'):
            with self.subTest(rel):
                src = (self.ROOT / rel).read_text(encoding='utf-8')
                self.assertNotIn('homework_attachments', src)
                self.assertNotIn('prepare_homework_upload', src)
                self.assertNotIn('board_images', src)

    def test_28_generic_upload_helper_still_stores_bytes_untouched(self):
        app = create_app('testing')
        raw = _jpeg_with_exif(1200, 800)
        with app.app_context(), mock.patch('app.utils.helpers._supabase_upload',
                                           return_value=FAKE_URL) as up:
            self.assertEqual(helpers.save_uploaded_file(_fs(raw, 'x.jpg'), 'students'), FAKE_URL)
        data, path, ctype = up.call_args.args
        self.assertEqual((data, ctype), (raw, 'image/jpeg'))
        self.assertTrue(path.startswith('students/') and path.endswith('.jpg'))

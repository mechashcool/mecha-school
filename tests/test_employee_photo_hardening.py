"""
Employee.photo upload hardening — validation only, bytes never changed.

Helper level (app.utils.employee_photo.validate_employee_photo) and route level
(POST /employees/create, POST /employees/<id>/edit):

  * valid JPEG / PNG / WebP / GIF (incl. animated) <= 2 MB are accepted and the
    bytes handed to Storage are the uploaded bytes (SHA-256 equal, EXIF kept),
    with the same extension, Content-Type and bucket as before;
  * > 2 MB, unsupported extensions, fake/corrupt/truncated images and extreme
    pixel counts are refused with a clear message, no Storage call and no DB
    change — on create AND on edit (the edit no longer drops them silently);
  * a Storage failure on edit keeps the current photo and reports an error;
  * a metadata-only edit and viewing an employee never validate, store, fetch
    or delete anything;
  * AI Face still reads Employee.photo through its unchanged path;
  * another school's employee cannot receive a photo.

Storage is a recording mock; fetch/delete are mocks that must stay unused
(AI Face's own fetch is mocked to return the recorded bytes). No network.
"""
import hashlib
import io
import pathlib
import random
import struct
import unittest
import zlib
from datetime import date
from unittest import mock
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFilter

from app import create_app
from app.models import (db, AcademicYear, AttendanceDevice, AuditLog,
                        DeviceEmployeeMapping, Employee, Role, School, User)
from app.utils import helpers
from app.utils import student_photo
from app.utils.employee_photo import (EMPLOYEE_PHOTO_MAX_BYTES,
                                      EMPLOYEE_PHOTO_MAX_PIXELS, MSG_INVALID,
                                      MSG_TOO_BIG, MSG_TOO_LARGE, MSG_UNSUPPORTED,
                                      validate_employee_photo)

PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True}
FAKE_URL = 'https://storage.test/storage/v1/object/public/uploads/employees/stored-object'
OLD = 'https://storage.test/storage/v1/object/public/uploads/employees/old-{}.jpg'
MP4 = b'\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom' + b'\x00' * 64
PDF = b'%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n' + b'0' * 800 + b'\n%%EOF\n'
MSG_REPLACE_FAILED = 'تعذّر حفظ صورة الموظف الجديدة'


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
    """Phone-sized JPEG (< 2 MB) with EXIF orientation + camera tags + GPS."""
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x010F] = 'SecretCam'
    exif.get_ifd(0x8825)[1] = 'N'
    raw = _enc(_photo(3000, 2250), 'JPEG', quality=80, exif=exif.tobytes())
    assert len(raw) <= EMPLOYEE_PHOTO_MAX_BYTES, len(raw)
    return raw


def _noise_png(w, h):
    """A real, valid PNG whose size is dominated by incompressible noise."""
    return _enc(Image.frombytes('RGB', (w, h), random.Random(7).randbytes(w * h * 3)), 'PNG')


def _png_header_only(w, h):
    """A PNG declaring w x h pixels with a tiny body (a decompression-bomb shape)."""
    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF))
    ihdr = struct.pack('>IIBBBBB', w, h, 8, 0, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', zlib.compress(b'\x00' * 64)) + chunk(b'IEND', b''))


def _animated(fmt):
    a, b = _photo(200, 150), _photo(200, 150).rotate(180)
    if fmt == 'GIF':
        a, b = a.convert('P'), b.convert('P')
    return _enc(a, fmt, save_all=True, append_images=[b], duration=100, loop=0)


def _valid_uploads():
    return [
        ('phone jpeg', 'photo.jpg', _phone_jpeg()),
        ('jpeg ext', 'photo.jpeg', _enc(_photo(800, 600), 'JPEG', quality=85)),
        ('png', 'photo.png', _enc(_photo(800, 600), 'PNG')),
        ('webp', 'photo.webp', _enc(_photo(1200, 900), 'WEBP', quality=90)),
        ('gif', 'photo.gif', _enc(_photo(300, 200).convert('P'), 'GIF')),
        ('animated gif', 'anim.gif', _animated('GIF')),
        ('animated webp', 'anim.webp', _animated('WEBP')),
    ]


def _invalid_uploads():
    good = _enc(_photo(1200, 900), 'JPEG', quality=90)
    png = _enc(_photo(600, 400), 'PNG')
    big = _noise_png(1000, 1000)
    assert len(big) > EMPLOYEE_PHOTO_MAX_BYTES, len(big)
    return [
        ('over 2 MB png', 'x.png', big, MSG_TOO_BIG),
        ('text renamed jpg', 'x.jpg', b'hello, this is not an image' * 50, MSG_INVALID),
        ('text renamed png', 'x.png', b'\x89PNG but not really' * 40, MSG_INVALID),
        ('pdf renamed jpg', 'x.jpg', PDF, MSG_INVALID),
        ('video renamed jpg', 'x.jpg', MP4, MSG_INVALID),
        ('bmp renamed jpg', 'x.jpg', _enc(_photo(100, 80), 'BMP'), MSG_INVALID),
        ('truncated jpeg', 'x.jpg', good[: len(good) // 2], MSG_INVALID),
        ('corrupt jpeg', 'x.jpg', good[:600] + b'\x13\x37' * 3000, MSG_INVALID),
        ('truncated png', 'x.png', png[: len(png) // 2], MSG_INVALID),
        ('empty file', 'x.jpg', b'', MSG_INVALID),
        ('45 MP png', 'x.png', _enc(Image.new('L', (9000, 5000)), 'PNG'), MSG_TOO_LARGE),
        ('900 MP header', 'x.png', _png_header_only(30000, 30000), MSG_TOO_LARGE),
        ('bmp extension', 'x.bmp', _enc(_photo(100, 80), 'BMP'), MSG_UNSUPPORTED),
        ('svg extension', 'x.svg', b'<svg xmlns="http://www.w3.org/2000/svg"/>', MSG_UNSUPPORTED),
        ('no extension', 'photo', good, MSG_UNSUPPORTED),
    ]


class _FS:
    """Minimal FileStorage stand-in (filename + stream), like werkzeug's."""
    def __init__(self, raw, name):
        self.filename, self.stream = name, io.BytesIO(raw)


# ─────────────────────────────────────────────────────────────────────────────
#  Helper level
# ─────────────────────────────────────────────────────────────────────────────

class EmployeePhotoValidatorTest(unittest.TestCase):

    def test_valid_images_accepted_and_stream_rewound(self):
        for label, name, raw in _valid_uploads():
            with self.subTest(label):
                f = _FS(raw, name)
                self.assertIsNone(validate_employee_photo(f))
                self.assertEqual(f.stream.tell(), 0)
                self.assertEqual(f.stream.read(), raw)            # untouched

    def test_invalid_images_refused_and_stream_rewound(self):
        for label, name, raw, msg in _invalid_uploads():
            with self.subTest(label):
                f = _FS(raw, name)
                self.assertEqual(validate_employee_photo(f), msg)
                self.assertEqual(f.stream.tell(), 0)

    def test_limits_and_pillow_globals_untouched(self):
        self.assertEqual(EMPLOYEE_PHOTO_MAX_BYTES, 2 * 1024 * 1024)
        self.assertEqual(EMPLOYEE_PHOTO_MAX_PIXELS, 40_000_000)
        self.assertEqual(EMPLOYEE_PHOTO_MAX_PIXELS, student_photo.STUDENT_PHOTO_MAX_PIXELS)
        before = Image.MAX_IMAGE_PIXELS
        validate_employee_photo(_FS(_png_header_only(30000, 30000), 'x.png'))
        self.assertEqual(Image.MAX_IMAGE_PIXELS, before)

    def test_exactly_2mb_boundary(self):
        base = _enc(_photo(400, 300), 'JPEG')
        at = base + b'\x00' * (EMPLOYEE_PHOTO_MAX_BYTES - len(base))    # trailing bytes after EOI
        self.assertIsNone(validate_employee_photo(_FS(at, 'a.jpg')))
        self.assertEqual(validate_employee_photo(_FS(at + b'\x00', 'a.jpg')), MSG_TOO_BIG)

    def test_real_image_under_other_allowed_extension_still_accepted(self):
        # Stored and synced today (AI Face sniffs content); not a new refusal.
        self.assertIsNone(validate_employee_photo(_FS(_enc(_photo(300, 200), 'PNG'), 'p.jpg')))


# ─────────────────────────────────────────────────────────────────────────────
#  Route level
# ─────────────────────────────────────────────────────────────────────────────

class EmployeePhotoRouteTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['RATELIMIT_ENABLED'] = False

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.storage = mock.patch('app.utils.helpers._supabase_upload',
                                  return_value=FAKE_URL).start()
        # Nothing in these flows may read or delete a stored object.
        self.fetch = mock.patch('app.utils.helpers._supabase_fetch',
                                return_value=(None, None)).start()
        self.delete = mock.patch('app.utils.helpers._supabase_delete',
                                 return_value=False).start()
        # create=True keeps this suite runnable against code without the
        # validator (negative control).
        self.validate = mock.patch('app.blueprints.employees.validate_employee_photo',
                                   wraps=validate_employee_photo, create=True).start()
        self.addCleanup(mock.patch.stopall)
        self.ids = {}
        with self.app.app_context():
            role = Role.query.filter_by(name='school_admin').first()
            for key in ('a', 'b'):
                s = School(school_name=f'EPhoto {key} {self.sfx}', code=f'EP{key}{self.sfx}'[:20],
                           capacity=0, is_active=True)
                db.session.add(s)
                db.session.flush()
                y = AcademicYear(school_id=s.id, name=f'Y{key}{self.sfx}', is_current=True,
                                 start_date=date(2026, 8, 1), end_date=date(2027, 6, 30))
                u = User(username=f'ep{key}_{self.sfx}', email=f'ep{key}_{self.sfx}@t.test',
                         full_name=f'admin {key}', role_id=role.id, school_id=s.id,
                         is_active=True)
                u.set_password(PASSWORD)
                emp = Employee(employee_id=f'EP{key}-{self.sfx}', full_name=f'Existing {key}',
                               job_title=None, department='', school_id=s.id, status='active',
                               base_salary=0, photo=OLD.format(key))
                db.session.add_all([y, u, emp])
                db.session.flush()
                self.ids.update({f'school_{key}': s.id, f'emp_{key}': emp.id,
                                 f'admin_{key}': u.username})
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
                for model in (DeviceEmployeeMapping, AttendanceDevice, AuditLog, Employee,
                              User, AcademicYear):
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
        return client.post('/employees/create', content_type='multipart/form-data', data={
            'full_name': full_name, 'gender': 'male',
            'photo': (io.BytesIO(raw), name, 'image/jpeg')})

    def _edit(self, client, emp_key, full_name, photo=None, empty_file=False):
        data = {'full_name': full_name, 'status': 'active', 'gender': 'male'}
        if photo is not None:
            name, raw = photo
            data['photo'] = (io.BytesIO(raw), name, 'image/jpeg')
        elif empty_file:                          # browser: no file chosen
            data['photo'] = (io.BytesIO(b''), '', 'application/octet-stream')
        return client.post(f"/employees/{self.ids[emp_key]}/edit",
                           content_type='multipart/form-data', data=data)

    def _rows(self):
        with self.app.app_context():
            return sorted((e.id, e.full_name, e.photo, e.updated_at) for e in
                          Employee.query.execution_options(**OPTS).filter(
                              Employee.school_id.in_([self.ids['school_a'],
                                                      self.ids['school_b']])).all())

    def _emp(self, key):
        with self.app.app_context():
            return db.session.get(Employee, self.ids[key], execution_options=OPTS)

    def _assert_stored_verbatim(self, name, raw):
        # The first write is the original, byte-identical. A new upload may add
        # ONE separate display-only copy (employees/display/*.webp); the
        # original itself is never replaced by it.
        calls = self.storage.call_args_list
        self.assertIn(len(calls), (1, 2), calls)
        call = calls[0]
        for extra in calls[1:]:
            self.assertRegex(extra.args[1], r'^employees/display/[0-9a-f]{32}\.webp$')
        data, path, ctype = call.args
        self.assertEqual(hashlib.sha256(data).hexdigest(), hashlib.sha256(raw).hexdigest())
        ext = name.rsplit('.', 1)[1]
        self.assertTrue(path.startswith('employees/') and path.endswith(f'.{ext}'), path)
        self.assertNotIn('/', path[len('employees/'):])
        self.assertEqual(ctype, helpers._CONTENT_TYPES[ext])       # unchanged mapping
        self.assertEqual(call.kwargs, {'bucket': None})           # default bucket as before
        return data

    def _assert_no_object_io(self):
        self.fetch.assert_not_called()
        self.delete.assert_not_called()

    # ── 1-3, 12: valid uploads on create, stored byte-identical ──────────────

    def test_create_accepts_valid_images_byte_identical(self):
        client = self._client()
        for label, name, raw in _valid_uploads():
            with self.subTest(label):
                self.storage.reset_mock()
                full_name = f'New {label} {self.sfx}'
                resp = self._create(client, name, raw, full_name)
                self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True)[-800:])
                self._assert_stored_verbatim(name, raw)
                with self.app.app_context():
                    (e,) = Employee.query.execution_options(**OPTS).filter_by(
                        full_name=full_name).all()
                    self.assertEqual(e.photo, FAKE_URL)
                    self.assertEqual(e.school_id, self.ids['school_a'])
        self._assert_no_object_io()

    # ── 13: EXIF / metadata preserved verbatim ────────────────────────────────

    def test_exif_and_metadata_kept_on_replacement(self):
        raw = _phone_jpeg()
        resp = self._edit(self._client(), 'emp_a', 'Replaced', photo=('new.jpg', raw))
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True)[-800:])
        stored = self._assert_stored_verbatim('new.jpg', raw)
        img = Image.open(io.BytesIO(stored))
        self.assertEqual(img.size, (3000, 2250))                  # not resized
        exif = img.getexif()
        self.assertEqual(exif[0x0112], 6)                         # orientation kept
        self.assertEqual(exif[0x010F], 'SecretCam')               # camera tag kept
        self.assertEqual(dict(exif.get_ifd(0x8825))[1], 'N')      # GPS kept
        e = self._emp('emp_a')
        self.assertEqual((e.full_name, e.photo), ('Replaced', FAKE_URL))
        self.validate.assert_called_once()
        self._assert_no_object_io()                               # old object not touched

    # ── 4, 7-11: invalid uploads on create ────────────────────────────────────

    def test_create_refuses_invalid_images_before_storage(self):
        client = self._client()
        for label, name, raw, msg in _invalid_uploads():
            with self.subTest(label):
                before = self._rows()
                resp = self._create(client, name, raw, f'Bad {label} {self.sfx}')
                self.assertEqual(resp.status_code, 200)
                html = resp.get_data(as_text=True)
                self.assertIn(msg, html)
                self.assertNotIn('Traceback', html)
                self.assertEqual(self._rows(), before, 'an employee row was written')
                self.storage.assert_not_called()
        with self.app.app_context():                              # no orphan account either
            self.assertEqual(User.query.execution_options(**OPTS).filter_by(
                school_id=self.ids['school_a']).count(), 1)
        self._assert_no_object_io()

    # ── 5, 6, 7-11: invalid replacement on edit — clear error, row unchanged ──

    def test_edit_refuses_invalid_images_row_untouched(self):
        client = self._client()
        for label, name, raw, msg in _invalid_uploads():
            with self.subTest(label):
                before = self._rows()
                resp = self._edit(client, 'emp_a', 'Renamed', photo=(name, raw))
                self.assertEqual(resp.status_code, 200)
                html = resp.get_data(as_text=True)
                self.assertIn(msg, html)
                self.assertNotIn('تم تحديث بيانات الموظف', html)   # no false success
                self.assertEqual(self._rows(), before, 'employee changed on refusal')
                self.assertEqual(self._emp('emp_a').photo, OLD.format('a'))
                self.storage.assert_not_called()
        self._assert_no_object_io()

    # ── 14: metadata-only edit ────────────────────────────────────────────────

    def test_metadata_only_edit_touches_no_photo(self):
        for label, kw in (('no file field', {}), ('empty file field', {'empty_file': True})):
            with self.subTest(label):
                resp = self._edit(self._client(), 'emp_a', f'Meta {label}', **kw)
                self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True)[-800:])
                e = self._emp('emp_a')
                self.assertEqual((e.full_name, e.photo), (f'Meta {label}', OLD.format('a')))
        self.validate.assert_not_called()
        self.storage.assert_not_called()
        self._assert_no_object_io()

    # ── 15: storage failure on edit keeps the current photo ───────────────────

    def test_edit_storage_failure_keeps_old_photo(self):
        raw = _enc(_photo(400, 300), 'JPEG')
        for label, patch_kw in (('helper returns None', {'return_value': None}),
                                ('helper raises', {'side_effect': OSError('disk full')})):
            with self.subTest(label), mock.patch('app.blueprints.employees.save_uploaded_file',
                                                 **patch_kw) as save:
                before = self._rows()
                resp = self._edit(self._client(), 'emp_a', 'Should not save',
                                  photo=('p.jpg', raw))
                self.assertEqual(resp.status_code, 200)
                html = resp.get_data(as_text=True)
                self.assertIn(MSG_REPLACE_FAILED, html)
                self.assertNotIn('Traceback', html)
                save.assert_called_once()
                self.assertEqual(self._rows(), before)
                self.assertEqual(self._emp('emp_a').photo, OLD.format('a'))

    def test_create_storage_failure_semantics_preserved(self):
        before = self._rows()
        with mock.patch('app.blueprints.employees.save_uploaded_file', return_value=None):
            resp = self._create(self._client(), 'p.jpg', _enc(_photo(400, 300), 'JPEG'),
                                f'NoStore {self.sfx}')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('تعذّر حفظ صورة الموظف', resp.get_data(as_text=True))
        self.assertEqual(self._rows(), before)

    # ── 16, 17: AI Face reads Employee.photo through its unchanged path ───────

    def test_aiface_route_passes_employee_photo(self):
        with self.app.app_context():
            dev = AttendanceDevice(school_id=self.ids['school_a'], name='cam',
                                   device_scope='employees', ip_address='127.0.0.1',
                                   password='x', device_sn=f'SN-{self.sfx}')
            db.session.add(dev)
            db.session.flush()
            m = DeviceEmployeeMapping(school_id=self.ids['school_a'], device_id=dev.id,
                                      employee_id=self.ids['emp_a'], enrollment_no='7',
                                      is_active=True)
            db.session.add(m)
            db.session.commit()
            dev_id, m_id = dev.id, m.id
        with mock.patch('app.services.aiface_sync.sync_person_to_device',
                        return_value={'ok': True}) as sync:
            resp = self._client().post(f'/attendance-devices/{dev_id}/aiface-sync-employee',
                                       json={'mapping_id': m_id})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:300])
        self.assertEqual(sync.call_args.kwargs['photo'], self._emp('emp_a').photo)
        self.assertEqual(sync.call_args.kwargs['entity_type'], 'employee')

    def test_aiface_prepares_the_stored_original(self):
        from app.services.aiface_sync import prepare_photo_for_device
        for label, name, raw in (('phone jpeg', 'p.jpg', _phone_jpeg()),
                                 ('animated gif', 'a.gif', _animated('GIF')),
                                 ('animated webp', 'a.webp', _animated('WEBP'))):
            with self.subTest(label):
                self.storage.reset_mock()
                resp = self._edit(self._client(), 'emp_a', f'AI {label}', photo=(name, raw))
                self.assertEqual(resp.status_code, 302)
                stored = self._assert_stored_verbatim(name, raw)
                photo = self._emp('emp_a').photo
                with self.app.app_context(), mock.patch(
                        'app.utils.helpers._supabase_fetch',
                        return_value=(stored, helpers._CONTENT_TYPES[name.rsplit('.', 1)[1]])) as f:
                    jpeg, info = prepare_photo_for_device(photo, label='t')
                f.assert_called_once_with('employees/stored-object', bucket='uploads')
                self.assertIsNotNone(jpeg, info)
                self.assertEqual(info['source'], 'supabase')
                out = Image.open(io.BytesIO(jpeg))
                self.assertEqual(out.format, 'JPEG')
                self.assertLessEqual(max(out.size), 640)

    def test_aiface_and_device_code_unchanged_in_shape(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        sync = (root / 'app/services/aiface_sync.py').read_text(encoding='utf-8')
        devices = (root / 'app/blueprints/attendance_devices/__init__.py').read_text(encoding='utf-8')
        self.assertIn('img.thumbnail((640, 640), Image.LANCZOS)', sync)
        self.assertIn("img.save(buf, format='JPEG', quality=85, optimize=True)", sync)
        self.assertIn('photo=employee.photo,', devices)
        self.assertIn('photo=employee.photo if employee else None', devices)
        for text in (sync, devices):
            self.assertNotIn('employee_photo', text)
            self.assertNotIn('photo_display', text)

    # ── 18: cross-school ──────────────────────────────────────────────────────

    def test_other_school_employee_cannot_receive_a_photo(self):
        before = self._rows()
        resp = self._edit(self._client('a'), 'emp_b', 'Hijack',
                          photo=('p.jpg', _enc(_photo(400, 300), 'JPEG')))
        self.assertIn(resp.status_code, (403, 404))
        self.assertEqual(self._rows(), before)
        self.storage.assert_not_called()
        self.validate.assert_not_called()

    # ── 19: existing employee with an old photo is untouched ─────────────────

    def test_viewing_existing_employee_touches_nothing(self):
        before = self._rows()
        client = self._client()
        for path in (f"/employees/{self.ids['emp_a']}", f"/employees/{self.ids['emp_a']}/edit",
                     '/employees/', '/employees/search?q=Existing'):
            with self.subTest(path):
                self.assertEqual(client.get(path).status_code, 200)
        self.assertEqual(self._rows(), before)
        self.assertEqual(self._emp('emp_a').photo, OLD.format('a'))
        self.assertEqual(self._emp('emp_b').photo, OLD.format('b'))
        self.validate.assert_not_called()
        self.storage.assert_not_called()
        self._assert_no_object_io()

    def test_edit_form_shows_limit_hint(self):
        html = self._client().get(f"/employees/{self.ids['emp_a']}/edit").get_data(as_text=True)
        self.assertIn('الحد الأقصى لحجم الصورة 2 ميجابايت', html)
        self.assertIn('JPG, JPEG, PNG, WEBP, GIF', html)


if __name__ == '__main__':
    unittest.main()

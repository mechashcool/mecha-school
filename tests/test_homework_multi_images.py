"""
School homework: up to 3 images OR one PDF (P1 — backend + web, no Flutter).

Focused acceptance tests. Numbers in test names refer to the P1 checklist:

  old data / compatibility   1-8      new create            9-21
  optimisation               22-28    legacy input          29-31
  «جميع الشعب» batch          32-34    API                   35-41
  security                   42-43    institute unchanged   44-46
  delete                     47       edit                  48-52

The migration itself (upgrade / downgrade / upgrade with seeded legacy rows)
is verified separately on the isolated test clone; here 1-2 assert the
resulting state: legacy rows carry no image rows and are never rewritten.

Storage, Storage deletes and notifications are recording mocks: no network,
no file written to the working tree.
"""
import io
import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from unittest import mock
from uuid import uuid4

from PIL import Image, ImageDraw
from sqlalchemy import event

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (db, AcademicYear, AuditLog, Employee, Grade, Homework,
                        HomeworkImage, InstituteStudyGroup, Notification, Role,
                        School, Section, Student, Subject, User, parent_students,
                        teacher_subjects)
from app.utils import helpers
from app.utils.board_images import optimize_board_image
from app.utils.homework_attachments import (
    HOMEWORK_IMAGE_MAX_BYTES, HOMEWORK_MAX_IMAGES, HOMEWORK_PDF_MAX_BYTES,
    HOMEWORK_TOTAL_IMAGE_MAX_BYTES, LEGACY_MOBILE_TYPE_ERROR, MSG_IMAGE_TOO_BIG,
    prepare_homework_upload)
from app.utils.upload_access import can_access_upload, resolve_upload_owner

PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True, 'include_all_years': True}
STORE = 'https://storage.test/storage/v1/object/public/uploads/'
LEGACY_IMG = STORE + 'homework/legacy-a.jpg'
LEGACY_PDF = STORE + 'homework/legacy-b.pdf'
PDF = b'%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n' + bytes(range(256)) * 8 + b'\n%%EOF\n'

LEGACY_PARENT_KEYS = {'id', 'homework_id', 'title', 'subject', 'subject_name',
                      'teacher_name', 'grade_name', 'section_name', 'assigned_at',
                      'publish_date', 'due_date', 'description', 'status',
                      'attachment_url', 'attachment_type', 'file_name', 'file_size',
                      'is_pdf', 'submitted_status'}

MSG = {
    'too_many_images': 'يمكن إرفاق 3 صور كحد أقصى.',
    'multiple_pdfs': 'يمكن إرفاق ملف PDF واحد فقط.',
    'pdf_too_large': 'حجم ملف PDF أكبر من الحد المسموح (5 MB).',
    'images_total_too_large': 'الحجم الإجمالي للصور أكبر من الحد المسموح (14 MB).',
    'invalid_pdf': 'تعذّر قراءة ملف PDF. يرجى رفع ملف PDF صالح.',
}


# ── synthetic files ──────────────────────────────────────────────────────────

def _enc(img, fmt, **kw):
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def _photo(w, h, seed=0):
    img = Image.linear_gradient('L').resize((w, h)).convert('RGB')
    d = ImageDraw.Draw(img)
    for i in range(12):
        x, y = ((i + seed) * 997) % w, ((i + seed) * 613) % h
        d.rectangle((x, y, x + w // 6, y + h // 6),
                    fill=((i * 50 + seed) % 255, (i * 90) % 255, (i * 20) % 255))
    return img


def _jpeg(w=1200, h=900, seed=0):
    return _enc(_photo(w, h, seed), 'JPEG', quality=90)


def _padded_jpeg(total, seed=0):
    raw = _jpeg(1600, 1200, seed)
    assert len(raw) < total
    return raw + b'\x00' * (total - len(raw))


def _jpeg_with_exif_gps(w, h):
    exif = Image.Exif()
    exif[0x010F] = 'SecretCam'
    exif[0x0112] = 1
    gps = exif.get_ifd(0x8825)
    gps.update({1: 'N', 2: (30.0, 15.0, 7.0), 3: 'E', 4: (47.0, 48.0, 9.0)})
    return _enc(_photo(w, h), 'JPEG', quality=90, exif=exif.tobytes())


def _decode(data):
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


class HomeworkMultiImageTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        with cls.app.app_context():
            cls.role_ids = {n: Role.query.filter_by(name=n).first().id
                            for n in ('teacher', 'school_admin', 'parent')}

    # ── fixtures ────────────────────────────────────────────────────────────

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.uploads = []                      # [(data, object_path, ctype)]
        self.storage = mock.patch('app.utils.helpers._supabase_upload',
                                  side_effect=self._fake_upload).start()
        self.deleted = mock.patch('app.utils.helpers._supabase_delete',
                                  return_value=True).start()
        self.web_notify = mock.patch('app.blueprints.homework._notify_homework_parents').start()
        self.dispatch = mock.patch('app.services.async_dispatch.submit').start()
        self.addCleanup(mock.patch.stopall)
        self.ids = {}
        with self.app.app_context():
            for key in ('a', 'b'):
                self._school(key)
            self._institute()
            db.session.commit()

    def _fake_upload(self, data, object_path, content_type, bucket=None):
        self.uploads.append((data, object_path, content_type))
        return STORE + object_path

    def _user(self, school_id, label, role):
        u = User(username=f'mi{label}_{self.sfx}', email=f'mi{label}_{self.sfx}@example.test',
                 full_name=f'{label} {self.sfx}', role_id=self.role_ids[role],
                 school_id=school_id, is_active=True)
        u.set_password(PASSWORD)
        db.session.add(u)
        db.session.flush()
        return u

    def _school(self, key):
        s = self.sfx
        school = School(school_name=f'MI {key} {s}', code=f'MI{key}{s}'[:20],
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
                       code=f'MI{key}{s[:6]}')
        db.session.add_all([sec1, sec2, subj])
        db.session.flush()
        teacher, teacher2 = self._user(school.id, f't{key}', 'teacher'), self._user(school.id, f't2{key}', 'teacher')
        admin, parent = self._user(school.id, f'adm{key}', 'school_admin'), self._user(school.id, f'par{key}', 'parent')
        emp = Employee(school_id=school.id, employee_id=f'E{key}{s}', full_name=f'T {key}',
                       base_salary=0, status='active', user_id=teacher.id)
        emp2 = Employee(school_id=school.id, employee_id=f'F{key}{s}', full_name=f'T2 {key}',
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

        def legacy(title, path, kind):
            # Written exactly the way every pre-feature row looks.
            hw = Homework(school_id=school.id, academic_year_id=year.id, teacher_id=emp.id,
                          subject_id=subj.id, section_id=sec1.id, title=title,
                          publish_date=date.today() - timedelta(days=1),
                          due_date=date.today() + timedelta(days=5), is_active=True,
                          attachment_path=path, attachment_type=kind)
            db.session.add(hw)
            db.session.flush()
            return hw.id

        self.ids.update({
            f'school_{key}': school.id, f'year_{key}': year.id, f'grade_{key}': grade.id,
            f'sec_{key}': sec1.id, f'sec2_{key}': sec2.id, f'subj_{key}': subj.id,
            f'emp_{key}': emp.id, f'child_{key}': child.id,
            f'teacher_{key}': teacher.id, f'teacher2_{key}': teacher2.id,
            f'admin_{key}': admin.id, f'parent_{key}': parent.id,
            f'legacy_img_{key}': legacy(f'Legacy img {key}', LEGACY_IMG.replace('legacy-a', f'legacy-a-{key}'), 'image'),
            f'legacy_pdf_{key}': legacy(f'Legacy pdf {key}', LEGACY_PDF.replace('legacy-b', f'legacy-b-{key}'), 'pdf'),
            f'legacy_none_{key}': legacy(f'Legacy none {key}', None, None),
        })

    def _institute(self):
        s = self.sfx
        inst = School(school_name=f'MI inst {s}', code=f'MII{s}'[:20], capacity=0,
                      is_active=True, institution_type=School.INSTITUTION_INSTITUTE)
        db.session.add(inst)
        db.session.flush()
        year = AcademicYear(school_id=inst.id, name=f'IY {s}', is_current=True,
                            start_date=date(2026, 8, 1), end_date=date(2027, 6, 30))
        db.session.add(year)
        db.session.flush()
        subj = Subject(school_id=inst.id, academic_year_id=year.id, name='Inst subj',
                       code=f'MIS{s[:6]}')
        db.session.add(subj)
        db.session.flush()
        teacher = self._user(inst.id, 'ti', 'teacher')
        emp = Employee(school_id=inst.id, employee_id=f'EI{s}', full_name='Inst T',
                       base_salary=0, status='active', user_id=teacher.id)
        db.session.add(emp)
        db.session.flush()
        group = InstituteStudyGroup(school_id=inst.id, academic_year_id=year.id,
                                    subject_id=subj.id, instructor_id=emp.id,
                                    name=f'Grp {s}', is_active=True)
        db.session.add(group)
        db.session.flush()
        self.ids.update({'school_i': inst.id, 'teacher_i': teacher.id, 'group_i': group.id})

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for key in ('a', 'b', 'i'):
                sid = self.ids[f'school_{key}']
                uids = [u.id for u in User.query.execution_options(**OPTS)
                        .filter_by(school_id=sid).all()]
                if uids:
                    AuditLog.query.execution_options(**OPTS).filter(
                        AuditLog.user_id.in_(uids)).delete(synchronize_session=False)
                for model in (AuditLog, Notification, HomeworkImage, Homework,
                              InstituteStudyGroup):
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

    # ── request helpers ──────────────────────────────────────────────────────

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

    @staticmethod
    def _files(files, field='attachments[]'):
        if not files:
            return {}
        return {field: [(io.BytesIO(raw), name, 'application/octet-stream')
                        for name, raw in files]}

    def _web_form(self, school='a', files=(), field='attachments[]', **over):
        data = {'title': f'HW {uuid4().hex[:6]}', 'description': 'd',
                'subject_id': str(self.ids[f'subj_{school}']),
                'grade_id': str(self.ids[f'grade_{school}']),
                'section_id': str(self.ids[f'sec_{school}']),
                'publish_date': date.today().isoformat(),
                'due_date': (date.today() + timedelta(days=3)).isoformat(), **over}
        data.update(self._files(files, field))
        return data

    def _web_create(self, who='teacher_a', **kw):
        form = self._web_form(**kw)
        resp = self._web(who).post('/homework/create', data=form,
                                   content_type='multipart/form-data')
        return resp, form['title']

    def _web_edit(self, hw_id, who='teacher_a', **kw):
        return self._web(who).post(f'/homework/{hw_id}/edit', data=self._web_form(**kw),
                                   content_type='multipart/form-data')

    def _mobile_form(self, school='a', files=(), field='attachments[]', **over):
        data = {'title': f'MHW {uuid4().hex[:6]}', 'description': 'd',
                'section_id': str(self.ids[f'sec_{school}']),
                'subject_id': str(self.ids[f'subj_{school}']),
                'due_date': (date.today() + timedelta(days=3)).isoformat(), **over}
        data.update(self._files(files, field))
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

    def _parent_items(self, key='parent_a', child='child_a'):
        resp = self.app.test_client().get(
            f"/api/mobile/v1/parent/children/{self.ids[child]}/homework",
            headers=self._jwt(key))
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return {i['id']: i for i in resp.get_json()['homework']}

    # ── state helpers ────────────────────────────────────────────────────────

    def _state(self, hw_id):
        """(attachment_path, attachment_type, [extra paths in sort order])."""
        with self.app.app_context():
            hw = db.session.get(Homework, hw_id, execution_options=OPTS)
            extras = (HomeworkImage.query.execution_options(**OPTS)
                      .filter_by(homework_id=hw_id).order_by(HomeworkImage.sort_order).all())
            self.assertEqual([e.sort_order for e in extras], list(range(1, len(extras) + 1)))
            return hw.attachment_path, hw.attachment_type, [e.file_path for e in extras]

    def _by_title(self, title):
        with self.app.app_context():
            return [h.id for h in Homework.query.execution_options(**OPTS)
                    .filter_by(title=title).order_by(Homework.id).all()]

    def _snapshot(self):
        with self.app.app_context():
            sids = [self.ids['school_a'], self.ids['school_b'], self.ids['school_i']]
            hws = sorted((h.id, h.title, h.attachment_path, h.attachment_type, h.updated_at)
                         for h in Homework.query.execution_options(**OPTS)
                         .filter(Homework.school_id.in_(sids)).all())
            imgs = sorted((i.homework_id, i.file_path, i.sort_order)
                          for i in HomeworkImage.query.execution_options(**OPTS)
                          .filter(HomeworkImage.school_id.in_(sids)).all())
            return hws, imgs

    def _stored_paths(self):
        return [STORE + path for _, path, _ in self.uploads]

    def _assert_refused(self, before, resp, *, web_msg=None, code=None, status=400):
        if web_msg is not None:
            self.assertEqual(resp.status_code, 200)
            self.assertIn(web_msg, resp.get_data(as_text=True))
        if code is not None:
            body = resp.get_json()
            self.assertEqual((resp.status_code, body['ok'], body['error']),
                             (status, False, code), body)
            self.assertTrue(body.get('message'))
        self.assertEqual(self._snapshot(), before, 'DB changed on a refused request')
        self.assertEqual(self.uploads, [], 'Storage written on a refused request')
        self.web_notify.assert_not_called()
        self.dispatch.assert_not_called()

    def _make_images_hw(self, n, who='teacher_a'):
        """A NEW school homework with n images (web create); returns its id."""
        resp, title = self._web_create(who=who, school=who[-1],
                                       files=[(f'p{i}.jpg', _jpeg(seed=i)) for i in range(n)])
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True)[:300])
        (hw_id,) = self._by_title(title)
        self.uploads.clear()
        self.web_notify.reset_mock()
        return hw_id

    @contextmanager
    def _sql(self):
        stmts = []
        with self.app.app_context():
            engine = db.engine

        def before(conn, cursor, statement, params, context, executemany):
            stmts.append(statement)
        event.listen(engine, 'before_cursor_execute', before)
        try:
            yield stmts
        finally:
            event.remove(engine, 'before_cursor_execute', before)

    # ═════════════════════════════════════════════════════════════════════════
    #  1-8  OLD DATA / BACKWARD COMPATIBILITY
    # ═════════════════════════════════════════════════════════════════════════

    def test_01_02_legacy_rows_have_no_image_rows_and_original_values(self):
        for key in ('legacy_img_a', 'legacy_pdf_a', 'legacy_none_a'):
            path, kind, extras = self._state(self.ids[key])
            self.assertEqual(extras, [])
        self.assertEqual(self._state(self.ids['legacy_img_a'])[:2],
                         (LEGACY_IMG.replace('legacy-a', 'legacy-a-a'), 'image'))
        self.assertEqual(self._state(self.ids['legacy_pdf_a'])[:2],
                         (LEGACY_PDF.replace('legacy-b', 'legacy-b-a'), 'pdf'))

    def test_03_04_old_image_and_pdf_render_exactly_as_before(self):
        html = self._web('teacher_a').get('/homework/').get_data(as_text=True)
        img = LEGACY_IMG.replace('legacy-a', 'legacy-a-a')
        pdf = LEGACY_PDF.replace('legacy-b', 'legacy-b-a')
        # The pre-feature single-image markup, byte-for-byte attributes.
        self.assertIn(f'<a href="{img}" target="_blank">\n            <img src="{img}" alt="مرفق" '
                      'class="img-fluid rounded"', html)
        self.assertIn('style="max-height:120px;object-fit:cover;width:100%"', html)
        self.assertIn(f'<a href="{pdf}" target="_blank" class="btn btn-sm btn-outline-danger w-100">',
                      html)
        self.assertNotIn('height:80px', html)          # no gallery for legacy rows

    def test_05_06_metadata_only_edits_never_touch_the_attachment(self):
        hw_id = self.ids['legacy_img_a']
        before = self._state(hw_id)
        empty_placeholder = {'attachments[]': (io.BytesIO(b''), '')}   # browser empty input
        with mock.patch('app.utils.homework_attachments.optimize_board_image') as opt:
            resp = self._web(
                'teacher_a').post(f'/homework/{hw_id}/edit',
                                  data={**self._web_form(title='Web meta'), **empty_placeholder},
                                  content_type='multipart/form-data')
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(self._mobile_update(hw_id, title='Multipart meta').status_code, 200)
            resp = self.app.test_client().put(
                f'/api/mobile/v1/teacher/homework/{hw_id}',
                json=self._mobile_form(title='Json meta'), headers=self._jwt('teacher_a'))
            self.assertEqual(resp.status_code, 200)
            opt.assert_not_called()
        self.assertEqual(self._state(hw_id), before)
        self.assertEqual(self.uploads, [])
        self.deleted.assert_not_called()
        with self.app.app_context():
            self.assertEqual(db.session.get(Homework, hw_id, execution_options=OPTS).title,
                             'Json meta')

    def test_07_08_legacy_api_keys_and_attachment_url_identical(self):
        items = self._parent_items()
        img = items[self.ids['legacy_img_a']]
        pdf = items[self.ids['legacy_pdf_a']]
        none = items[self.ids['legacy_none_a']]
        for item in (img, pdf, none):
            self.assertEqual(set(item), LEGACY_PARENT_KEYS | {'images'})
        legacy_url = LEGACY_IMG.replace('legacy-a', 'legacy-a-a')
        self.assertEqual((img['attachment_url'], img['attachment_type'], img['is_pdf'],
                          img['file_name']),
                         (legacy_url, 'image', False, 'legacy-a-a.jpg'))
        self.assertEqual(img['images'], [{'url': legacy_url, 'type': 'image'}])      # 36
        self.assertEqual((pdf['attachment_type'], pdf['is_pdf'], pdf['images']),
                         ('pdf', True, []))                                        # 37
        self.assertEqual((none['attachment_url'], none['images']), (None, []))     # 38

    # ═════════════════════════════════════════════════════════════════════════
    #  9-21  NEW CREATE
    # ═════════════════════════════════════════════════════════════════════════

    def test_09_no_attachment(self):
        resp, title = self._web_create()
        self.assertEqual(resp.status_code, 302)
        (hw_id,) = self._by_title(title)
        self.assertEqual(self._state(hw_id), (None, None, []))
        resp = self._mobile_create()
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(self._state(resp.get_json()['homework']['id']), (None, None, []))
        self.assertEqual(self.uploads, [])
        self.web_notify.assert_called_once()                   # 33: one notification
        self.dispatch.assert_called_once()

    def test_10_11_12_one_two_three_images_web_and_mobile(self):
        for n in (1, 2, 3):
            for path in ('web', 'mobile'):
                with self.subTest(images=n, path=path):
                    self.uploads.clear()
                    self.web_notify.reset_mock()
                    self.dispatch.reset_mock()
                    files = [(f'p{i}.jpg', _jpeg(seed=i)) for i in range(n)]
                    if path == 'web':
                        resp, title = self._web_create(files=files)
                        self.assertEqual(resp.status_code, 302)
                        (hw_id,) = self._by_title(title)
                        self.web_notify.assert_called_once()           # 33
                    else:
                        resp = self._mobile_create(files=files)
                        self.assertEqual(resp.status_code, 201, resp.get_json())
                        hw_id = resp.get_json()['homework']['id']
                        self.dispatch.assert_called_once()             # 33
                    self.assertEqual(len(self.uploads), n)
                    stored = self._stored_paths()
                    self.assertEqual(self._state(hw_id), (stored[0], 'image', stored[1:]))
                    for data, obj, ctype in self.uploads:
                        self.assertEqual(ctype, 'image/webp')
                        self.assertTrue(obj.startswith('homework/') and obj.endswith('.webp'))

    def test_13_four_images_rejected_everywhere(self):
        files = [(f'p{i}.jpg', _jpeg(400, 300, seed=i)) for i in range(4)]
        with mock.patch('app.utils.homework_attachments.optimize_board_image') as opt:
            before = self._snapshot()
            resp, _ = self._web_create(files=files)
            self._assert_refused(before, resp, web_msg=MSG['too_many_images'])
            self._assert_refused(before, self._mobile_create(files=files), code='too_many_images')
            self._assert_refused(before, self._mobile_update(self.ids['legacy_none_a'], files=files),
                                 code='too_many_images')
            opt.assert_not_called()                             # refused before decoding

    def test_14_15_pdf_up_to_5mib_accepted_larger_refused(self):
        at_limit = PDF + b'0' * (HOMEWORK_PDF_MAX_BYTES - len(PDF))
        resp, title = self._web_create(files=[('sheet.pdf', at_limit)])
        self.assertEqual(resp.status_code, 302)
        (hw_id,) = self._by_title(title)
        stored = self._stored_paths()
        self.assertEqual(self._state(hw_id), (stored[0], 'pdf', []))
        self.assertEqual(self.uploads[0][0], at_limit)          # stored byte-identical
        self.assertEqual(self.uploads[0][2], 'application/pdf')

        self.uploads.clear()
        self.web_notify.reset_mock()
        over = at_limit + b'0'
        before = self._snapshot()
        resp, _ = self._web_create(files=[('big.pdf', over)])
        self._assert_refused(before, resp, web_msg=MSG['pdf_too_large'])
        self._assert_refused(before, self._mobile_create(files=[('big.pdf', over)]),
                             code='pdf_too_large')
        self._assert_refused(before, self._mobile_create(files=[('fake.pdf', b'not a pdf')]),
                             code='invalid_pdf')

    def test_16_17_mixed_types_and_two_pdfs_refused(self):
        before = self._snapshot()
        for files, code in (([('a.jpg', _jpeg()), ('b.pdf', PDF)], 'mixed_attachment_types'),
                            ([('a.pdf', PDF), ('b.pdf', PDF)], 'multiple_pdfs')):
            with self.subTest(code):
                resp, _ = self._web_create(files=files)
                self.assertEqual(resp.status_code, 200)
                self._assert_refused(before, self._mobile_create(files=files), code=code)
        resp, _ = self._web_create(files=[('a.pdf', PDF), ('b.pdf', PDF)])
        self.assertIn(MSG['multiple_pdfs'], resp.get_data(as_text=True))

    def test_18_19_image_and_total_size_limits(self):
        before = self._snapshot()
        over = _padded_jpeg(HOMEWORK_IMAGE_MAX_BYTES + 1)
        resp, _ = self._web_create(files=[('big.jpg', over)])
        self._assert_refused(before, resp, web_msg=MSG_IMAGE_TOO_BIG)
        self._assert_refused(before, self._mobile_create(files=[('big.jpg', over)]),
                             code='image_too_large')
        # Three images, each under 5 MiB, together over 14 MiB.
        each = HOMEWORK_TOTAL_IMAGE_MAX_BYTES // 3 + 1024
        self.assertLessEqual(each, HOMEWORK_IMAGE_MAX_BYTES)
        three = [(f'p{i}.jpg', _padded_jpeg(each, seed=i)) for i in range(3)]
        with mock.patch('app.utils.homework_attachments.optimize_board_image') as opt:
            resp, _ = self._web_create(files=three)
            self._assert_refused(before, resp, web_msg=MSG['images_total_too_large'])
            self._assert_refused(before, self._mobile_create(files=three),
                                 code='images_total_too_large')
            opt.assert_not_called()

    def test_20_invalid_second_image_leaves_no_partial_state(self):
        good = _jpeg()
        files = [('a.jpg', good), ('b.jpg', good[: len(good) // 3]), ('c.jpg', good)]
        before = self._snapshot()
        resp, _ = self._web_create(files=files)
        self._assert_refused(before, resp, web_msg='تعذّر قراءة الصورة')
        self._assert_refused(before, self._mobile_create(files=files), code='invalid_image')

    def test_21_storage_failure_on_second_upload_cleans_the_first(self):
        real_save = helpers.save_uploaded_file
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            return None if len(calls) == 2 else real_save(*args, **kwargs)

        before = self._snapshot()
        with mock.patch('app.utils.helpers.save_uploaded_file', side_effect=flaky):
            resp = self._mobile_create(files=[('a.jpg', _jpeg(seed=1)), ('b.jpg', _jpeg(seed=2)),
                                              ('c.jpg', _jpeg(seed=3))])
        body = resp.get_json()
        self.assertEqual((resp.status_code, body['error']), (503, 'upload_failed'))
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(len(self.uploads), 1)
        self.deleted.assert_called_once_with(self.uploads[0][1], bucket='uploads')

    def test_21b_db_failure_after_upload_removes_this_requests_files(self):
        before = self._snapshot()
        with mock.patch('app.blueprints.homework.add_new_attachments',
                        side_effect=RuntimeError('boom')):
            resp, _ = self._web_create(files=[('a.jpg', _jpeg(seed=1)), ('b.jpg', _jpeg(seed=2))])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(len(self.uploads), 2)
        self.assertEqual(sorted(c.args[0] for c in self.deleted.call_args_list),
                         sorted(obj for _, obj, _ in self.uploads))

    # ═════════════════════════════════════════════════════════════════════════
    #  22-28  OPTIMISATION
    # ═════════════════════════════════════════════════════════════════════════

    def test_22_to_27_new_images_are_768_webp_q85_without_metadata(self):
        big = _jpeg_with_exif_gps(4000, 3000)
        src = Image.open(io.BytesIO(big))
        self.assertTrue(src.getexif().get_ifd(0x8825))                 # GPS present in upload
        self.assertIn(b'SecretCam', big)
        tall = _enc(_photo(1500, 3000), 'PNG')
        small = _enc(_photo(500, 400), 'PNG')
        resp = self._mobile_create(files=[('big.jpg', big), ('tall.png', tall), ('s.png', small)])
        self.assertEqual(resp.status_code, 201, resp.get_json())
        sizes = []
        for (data, _, ctype), raw in zip(self.uploads, (big, tall, small)):
            img = _decode(data)
            self.assertEqual((img.format, ctype), ('WEBP', 'image/webp'))     # 22
            self.assertLessEqual(max(img.size), 768)                          # 23
            self.assertEqual(len(img.getexif()), 0)                           # 25, 26
            for marker in (b'Exif', b'SecretCam'):
                self.assertNotIn(marker, data)
            self.assertNotEqual(data, raw)                                    # 27
            self.assertEqual(data, optimize_board_image(raw, max_side=768, quality=85).data)
            sizes.append(img.size)
        self.assertEqual(sizes, [(768, 576), (384, 768), (500, 400)])          # 24: no upscale

    def test_28_other_features_keep_their_optimiser_policy(self):
        raw = _jpeg(3000, 2000)
        default = optimize_board_image(raw)
        self.assertEqual(default.data,
                         optimize_board_image(raw, max_side=1600, quality=80).data)
        self.assertEqual((default.width, default.height), (1600, 1067))
        # Institute homework still goes through the original 1600 / q80 helper.
        from werkzeug.datastructures import FileStorage
        out = prepare_homework_upload(FileStorage(io.BytesIO(raw), filename='i.jpg'))
        self.assertEqual(out.read(), default.data)

    # ═════════════════════════════════════════════════════════════════════════
    #  29-31  LEGACY INPUT
    # ═════════════════════════════════════════════════════════════════════════

    def test_29_to_31_legacy_attachment_field_still_works(self):
        resp, title = self._web_create(files=[('one.jpg', _jpeg())], field='attachment')
        self.assertEqual(resp.status_code, 302)
        (hw_id,) = self._by_title(title)
        self.assertEqual(self._state(hw_id), (self._stored_paths()[0], 'image', []))   # 30

        self.uploads.clear()
        resp = self._mobile_create(files=[('one.png', _enc(_photo(900, 600), 'PNG'))],
                                   field='attachment')
        self.assertEqual(resp.status_code, 201, resp.get_json())
        item = resp.get_json()['homework']
        stored = self._stored_paths()[0]
        self.assertEqual(self._state(item['id']), (stored, 'image', []))
        self.assertEqual(item['attachment_url'], stored)                              # 31
        self.assertEqual(item['images'], [{'url': stored, 'type': 'image'}])

    def test_legacy_field_keeps_legacy_error_strings(self):
        before = self._snapshot()
        gif = _enc(_photo(200, 100), 'GIF')
        cases = ((('a.gif', gif), LEGACY_MOBILE_TYPE_ERROR),
                 (('big.jpg', _padded_jpeg(HOMEWORK_IMAGE_MAX_BYTES + 1)), MSG_IMAGE_TOO_BIG))
        for file, legacy_error in cases:
            with self.subTest(legacy_error):
                resp = self._mobile_create(files=[file], field='attachment')
                self.assertEqual((resp.status_code, resp.get_json()),
                                 (400, {'ok': False, 'error': legacy_error}))
                resp = self._mobile_create(files=[file])                  # new field → code
                self.assertEqual(resp.get_json()['error'],
                                 'invalid_file_type' if file[0] == 'a.gif' else 'image_too_large')
        self.assertEqual(self._snapshot(), before)

    def test_files_in_both_fields_are_refused_not_dropped(self):
        before = self._snapshot()
        data = self._mobile_form(files=[('a.jpg', _jpeg())])
        data['attachment'] = (io.BytesIO(_jpeg(seed=3)), 'b.jpg', 'image/jpeg')
        resp = self.app.test_client().post('/api/mobile/v1/teacher/homework', data=data,
                                           headers=self._jwt('teacher_a'),
                                           content_type='multipart/form-data')
        self._assert_refused(before, resp, code='conflicting_attachment_fields')

    # ═════════════════════════════════════════════════════════════════════════
    #  32-34  «جميع الشعب» BATCH
    # ═════════════════════════════════════════════════════════════════════════

    def test_32_to_34_batch_three_images_uploaded_once_and_shared(self):
        files = [(f'p{i}.jpg', _jpeg(seed=i)) for i in range(3)]
        resp, title = self._web_create(who='admin_a', files=files, section_id='all')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(self.uploads), 3)                               # 32: not 3 × N
        stored = self._stored_paths()
        rows = self._by_title(title)
        self.assertEqual(len(rows), 2)                                       # two sections
        for hw_id in rows:
            self.assertEqual(self._state(hw_id), (stored[0], 'image', stored[1:]))  # 33
        with self.app.app_context():
            for path in stored[1:]:                                          # 34: repeated path
                self.assertEqual(HomeworkImage.query.execution_options(**OPTS)
                                 .filter_by(file_path=path).count(), 2)
        self.assertEqual(self.web_notify.call_count, 2)                      # one per homework

    # ═════════════════════════════════════════════════════════════════════════
    #  35-41  API
    # ═════════════════════════════════════════════════════════════════════════

    def test_35_39_parent_and_teacher_api_images_in_order(self):
        resp = self._mobile_create(files=[(f'p{i}.jpg', _jpeg(seed=i)) for i in range(3)])
        self.assertEqual(resp.status_code, 201)
        created = resp.get_json()['homework']
        stored = self._stored_paths()
        expected = [{'url': u, 'type': 'image'} for u in stored]
        self.assertEqual(created['images'], expected)
        self.assertEqual(created['attachment_url'], stored[0])
        item = self._parent_items()[created['id']]
        self.assertEqual(item['images'], expected)                           # 35
        self.assertEqual(item['attachment_url'], stored[0])
        resp = self.app.test_client().get('/api/mobile/v1/teacher/homework',
                                          headers=self._jwt('teacher_a'))
        listed = {h['id']: h for h in resp.get_json()['homework']}
        self.assertEqual(listed[created['id']]['images'], expected)          # 39
        self.assertEqual(listed[self.ids['legacy_pdf_a']]['images'], [])
        resp = self._mobile_update(created['id'], title='still 3')
        self.assertEqual(resp.get_json()['homework']['images'], expected)

    def test_40_41_lists_load_images_in_one_batched_query(self):
        for _ in range(3):
            self._make_images_hw(3)
        calls = (
            ('parent', lambda: self._parent_items()),
            ('teacher list', lambda: self.app.test_client().get(
                '/api/mobile/v1/teacher/homework', headers=self._jwt('teacher_a'))),
        )
        for label, call in calls:
            with self.subTest(label), self._sql() as stmts:
                call()
                image_selects = [s for s in stmts if 'homework_images' in s]
                self.assertEqual(len(image_selects), 1, image_selects)
        client = self._web('teacher_a')
        with self._sql() as stmts:
            html = client.get('/homework/').get_data(as_text=True)
        self.assertEqual(len([s for s in stmts if 'homework_images' in s]), 1)
        self.assertEqual(html.count('height:80px'), 9)                       # 3 × 3 thumbnails

    # ═════════════════════════════════════════════════════════════════════════
    #  42-43  SECURITY
    # ═════════════════════════════════════════════════════════════════════════

    def test_42_cross_school_images_unreachable(self):
        hw_b = self._make_images_hw(3, who='teacher_b')
        hw_a = self._make_images_hw(2)
        with self.app.app_context():
            b_img = HomeworkImage.query.execution_options(**OPTS).filter_by(homework_id=hw_b).first()
            b_img_id, b_path = b_img.id, b_img.file_path
        before = self._snapshot()
        # Another school's homework cannot be edited at all.
        self.assertEqual(self._web_edit(hw_b, **{'remove_attachments[]': ['primary']}).status_code, 404)
        self.assertEqual(self._web_edit(hw_b, who='admin_a',
                                        **{'remove_attachments[]': ['primary']}).status_code, 404)
        self.assertEqual(self._mobile_update(hw_b, **{'remove_attachments[]': 'primary'})
                         .status_code, 404)
        # Another school's image id is not a valid remove target on my homework.
        resp = self._mobile_update(hw_a, **{'remove_attachments[]': str(b_img_id)})
        self.assertEqual((resp.status_code, resp.get_json()['error']),
                         (400, 'invalid_remove_target'))
        resp = self._web_edit(hw_a, **{'remove_attachments[]': [str(b_img_id)]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._snapshot(), before)
        # Parent of school A cannot read school B's child.
        resp = self.app.test_client().get(
            f"/api/mobile/v1/parent/children/{self.ids['child_b']}/homework",
            headers=self._jwt('parent_a'))
        self.assertIn(resp.status_code, (403, 404))
        # File access: school A users never reach school B's extra image.
        with self.app.app_context():
            for key, allowed in (('teacher_a', False), ('admin_a', False),
                                 ('teacher_b', True), ('admin_b', True)):
                user = db.session.get(User, self.ids[key], execution_options=OPTS)
                self.assertEqual(can_access_upload(user, b_path), allowed, key)
        # The ORM guard and the composite FK both refuse a cross-school image row.
        with self.app.app_context():
            db.session.add(HomeworkImage(homework_id=hw_b, school_id=self.ids['school_a'],
                                         file_path='x', sort_order=1))
            with self.assertRaises(ValueError):
                db.session.flush()
            db.session.rollback()

    def test_43_local_fallback_extra_image_resolves_like_its_homework(self):
        hw_a = self._make_images_hw(1)
        local = f'uploads/homework/{uuid4().hex}.webp'
        with self.app.app_context():
            db.session.add(HomeworkImage(homework_id=hw_a, school_id=self.ids['school_a'],
                                         file_path=local, sort_order=1))
            db.session.commit()
            primary = db.session.get(Homework, hw_a, execution_options=OPTS).attachment_path
            owner = resolve_upload_owner(local)
            self.assertEqual(owner, {**resolve_upload_owner(primary)})
            self.assertEqual((owner['kind'], owner['school_id'], owner['section_id']),
                             ('homework', self.ids['school_a'], self.ids['sec_a']))
            # Same decision as image #1 for every role (access not widened).
            for key in ('teacher_a', 'teacher2_a', 'admin_a', 'parent_a', 'teacher_b', 'admin_b'):
                user = db.session.get(User, self.ids[key], execution_options=OPTS)
                self.assertEqual(can_access_upload(user, local),
                                 can_access_upload(user, primary), key)
            self.assertIsNone(resolve_upload_owner(f'uploads/homework/{uuid4().hex}.webp'))

    # ═════════════════════════════════════════════════════════════════════════
    #  44-46  INSTITUTE UNCHANGED
    # ═════════════════════════════════════════════════════════════════════════

    def test_44_to_46_institute_homework_is_single_attachment_as_before(self):
        def form(**files):
            return {'title': f'IHW {uuid4().hex[:6]}', 'institute_group_id': str(self.ids['group_i']),
                    'publish_date': date.today().isoformat(),
                    'due_date': (date.today() + timedelta(days=3)).isoformat(), **files}

        client = self._web('teacher_i')
        raw = _jpeg(4000, 3000)
        data = form(attachment=(io.BytesIO(raw), 'i.jpg', 'image/jpeg'))
        self.assertEqual(client.post('/homework/create', data=data,
                                     content_type='multipart/form-data').status_code, 302)
        (hw_id,) = self._by_title(data['title'])
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(_decode(self.uploads[0][0]).size, (1600, 1200))   # original policy
        self.assertEqual(self._state(hw_id), (self._stored_paths()[0], 'image', []))   # 45

        # attachments[] was never part of the institute contract: still ignored.
        self.uploads.clear()
        data = form(**{'attachments[]': [(io.BytesIO(_jpeg(seed=i)), f'{i}.jpg') for i in range(3)]})
        self.assertEqual(client.post('/homework/create', data=data,
                                     content_type='multipart/form-data').status_code, 302)
        (hw_id,) = self._by_title(data['title'])
        self.assertEqual(self._state(hw_id), (None, None, []))
        self.assertEqual(self.uploads, [])

        # Mobile institute endpoint: single attachment, a > 5 MiB PDF still accepted.
        big_pdf = PDF + b'0' * (6 * 1024 * 1024)
        resp = self.app.test_client().post(
            '/api/mobile/v1/teacher/institute/homework',
            data={'group_id': str(self.ids['group_i']), 'title': 'Inst mobile',
                  'due_date': (date.today() + timedelta(days=2)).isoformat(),
                  'attachment': (io.BytesIO(big_pdf), 'big.pdf', 'application/pdf')},
            headers=self._jwt('teacher_i'), content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 201, resp.get_json())
        self.assertNotIn('images', resp.get_json()['homework'])
        self.assertEqual(self.uploads[-1][0], big_pdf)
        resp = self.app.test_client().get('/api/mobile/v1/teacher/homework',
                                          headers=self._jwt('teacher_i'))
        for item in resp.get_json()['homework']:
            self.assertNotIn('images', item)                                 # 46
        with self.app.app_context():
            self.assertEqual(HomeworkImage.query.execution_options(**OPTS)
                             .filter_by(school_id=self.ids['school_i']).count(), 0)

    # ═════════════════════════════════════════════════════════════════════════
    #  47  DELETE
    # ═════════════════════════════════════════════════════════════════════════

    def test_47_delete_cascades_image_rows(self):
        hw_id = self._make_images_hw(3)
        self.assertEqual(len(self._state(hw_id)[2]), 2)
        resp = self._web('teacher_a').post(f'/homework/{hw_id}/delete')
        self.assertEqual(resp.status_code, 302)
        with self.app.app_context():
            self.assertIsNone(db.session.get(Homework, hw_id, execution_options=OPTS))
            self.assertEqual(HomeworkImage.query.execution_options(**OPTS)
                             .filter_by(homework_id=hw_id).count(), 0)

    # ═════════════════════════════════════════════════════════════════════════
    #  48-52  EDIT
    # ═════════════════════════════════════════════════════════════════════════

    def test_48_removing_image_one_promotes_the_next(self):
        hw_id = self._make_images_hw(3)
        a, _, (b, c) = self._state(hw_id)
        resp = self._web_edit(hw_id, **{'remove_attachments[]': ['primary']})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._state(hw_id), (b, 'image', [c]))
        self.assertEqual(self.uploads, [])
        self.deleted.assert_not_called()          # removed files are never deleted

    def test_48b_legacy_image_plus_new_images_keeps_it_first_then_promotes(self):
        hw_id = self.ids['legacy_img_a']
        legacy, _, _ = self._state(hw_id)
        resp = self._web_edit(hw_id, files=[('x.jpg', _jpeg(seed=4)), ('y.jpg', _jpeg(seed=5))])
        self.assertEqual(resp.status_code, 302)
        b, c = self._stored_paths()
        self.assertEqual(self._state(hw_id), (legacy, 'image', [b, c]))      # A untouched, first
        with self.app.app_context():
            c_id = HomeworkImage.query.execution_options(**OPTS).filter_by(
                homework_id=hw_id, file_path=c).one().id
        resp = self._mobile_update(hw_id, **{'remove_attachments[]': ['primary', str(c_id)]})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(self._state(hw_id), (b, 'image', []))
        self.assertEqual(resp.get_json()['homework']['images'], [{'url': b, 'type': 'image'}])

    def test_49_removing_everything_leaves_no_attachment(self):
        hw_id = self._make_images_hw(3)
        with self.app.app_context():
            ids = [str(i.id) for i in HomeworkImage.query.execution_options(**OPTS)
                   .filter_by(homework_id=hw_id).all()]
        resp = self._web_edit(hw_id, **{'remove_attachments[]': ['primary', *ids]})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._state(hw_id), (None, None, []))
        hw2 = self._make_images_hw(2)
        resp = self._mobile_update(hw2, clear_attachment='1')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._state(hw2), (None, None, []))

    def test_50_51_images_to_pdf_and_pdf_to_images(self):
        hw_id = self._make_images_hw(3)
        resp = self._web_edit(hw_id, files=[('s.pdf', PDF)])
        self.assertEqual(resp.status_code, 302)
        (pdf_path,) = self._stored_paths()
        self.assertEqual(self._state(hw_id), (pdf_path, 'pdf', []))          # 50

        self.uploads.clear()
        legacy_pdf = self.ids['legacy_pdf_a']
        resp = self._mobile_update(legacy_pdf, files=[('a.jpg', _jpeg(seed=7)),
                                                      ('b.jpg', _jpeg(seed=8))])
        self.assertEqual(resp.status_code, 200, resp.get_json())
        a, b = self._stored_paths()
        self.assertEqual(self._state(legacy_pdf), (a, 'image', [b]))         # 51

    def test_52_final_four_images_refused_without_truncation(self):
        hw_id = self._make_images_hw(2)
        before = self._snapshot()
        files = [('c.jpg', _jpeg(seed=3)), ('d.jpg', _jpeg(seed=4))]
        resp = self._web_edit(hw_id, files=files)
        self._assert_refused(before, resp, web_msg=MSG['too_many_images'])
        self._assert_refused(before, self._mobile_update(hw_id, files=files),
                             code='too_many_images')
        # Removing one first makes room: final = 3, accepted.
        resp = self._mobile_update(hw_id, files=files, **{'remove_attachments[]': 'primary'})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(len(resp.get_json()['homework']['images']), HOMEWORK_MAX_IMAGES)

    def test_forms_school_multi_input_institute_unchanged(self):
        html = self._web('teacher_a').get('/homework/create').get_data(as_text=True)
        self.assertIn('name="attachments[]"', html)
        self.assertIn(' multiple>', html)
        self.assertIn('يمكن إرفاق حتى 3 صور أو ملف PDF واحد بحد أقصى 5 MB.', html)
        self.assertIn('يتم ضغط الصور تلقائياً مع الحفاظ على وضوحها.', html)
        card = html[html.index('المرفقات (اختياري)'):html.index('يتم ضغط الصور تلقائياً')]
        for technical in ('768', 'WebP', 'quality', '85'):      # no technical policy shown
            self.assertNotIn(technical, card)
        self.assertNotIn('name="attachment"', html)

        hw_id = self._make_images_hw(3)
        html = self._web('teacher_a').get(f'/homework/{hw_id}/edit').get_data(as_text=True)
        self.assertEqual(html.count('name="remove_attachments[]"'), 3)
        self.assertIn('value="primary"', html)

        html = self._web('teacher_i').get('/homework/create').get_data(as_text=True)
        self.assertIn('<input type="file" name="attachment" class="form-control"\n'
                      '                 accept=".jpg,.jpeg,.png,.webp,.pdf">', html)
        self.assertNotIn('attachments[]', html)

    def test_legacy_attachment_field_on_update_replaces_everything(self):
        hw_id = self._make_images_hw(3)
        resp = self._mobile_update(hw_id, files=[('one.jpg', _jpeg(seed=9))], field='attachment')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        (only,) = self._stored_paths()
        self.assertEqual(self._state(hw_id), (only, 'image', []))

    def test_teacher_cannot_touch_another_teachers_images(self):
        hw_id = self._make_images_hw(3)
        before = self._snapshot()
        self.assertEqual(self._web_edit(hw_id, who='teacher2_a',
                                        **{'remove_attachments[]': ['primary']}).status_code, 403)
        self.assertEqual(self._mobile_update(hw_id, who='teacher2_a',
                                             **{'remove_attachments[]': 'primary'}).status_code, 404)
        self.assertEqual(self._snapshot(), before)

    def test_admin_edit_refuses_another_schools_section(self):
        hw_id = self.ids['legacy_none_a']
        before = self._snapshot()
        resp = self._web_edit(hw_id, who='admin_a', section_id=str(self.ids['sec_b']))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('الشعبة المحددة غير صالحة.', resp.get_data(as_text=True))
        resp = self._web_edit(hw_id, who='admin_a', subject_id=str(self.ids['subj_b']))
        self.assertIn('المادة المحددة غير صالحة.', resp.get_data(as_text=True))
        self.assertEqual(self._snapshot(), before)


if __name__ == '__main__':
    unittest.main()

"""
Private upload access-control hotfix — /media/uploads/<key> and /files/uploads/<key>.

With PRIVATE_UPLOADS_ENABLED=true both routes used to mint a fresh signed
/media-proxy link for ANY caller before checking who was asking. A link is now
minted only after: authenticated session → resolve_upload_owner() →
can_access_upload() (the existing rules, unchanged). Everyone else — anonymous,
unauthorised, other school, unknown key — gets 404 and NO token is created.

Covers employee documents, student documents, employee photo + display copy,
student photo + display copy, in both stored shapes (full Supabase URL and
relative uploads/...). The /media-proxy HMAC check and the flag-off behaviour
are pinned unchanged. Storage is never contacted (fetch/sign stubbed).
"""
import unittest
from datetime import date
from unittest import mock
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from app import create_app
from app.models import (db, AcademicYear, AuditLog, Employee, EmployeeDocument, Grade,
                        Role, School, Section, Student, StudentDocument, User,
                        parent_students)
from app.utils import upload_access

PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True}
SUPA = 'https://storage.test'
PUBLIC = f'{SUPA}/storage/v1/object/public/uploads/'
ROUTES = ('/media/uploads/', '/files/uploads/')
# Kinds resolve_upload_owner() maps today (Student.photo_display is not one).
RESOLVABLE = ('edoc', 'ephoto', 'edisp', 'sdoc', 'sphoto')


class PrivateUploadAuthTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config.update(RATELIMIT_ENABLED=False, PRIVATE_UPLOADS_ENABLED=True,
                              SUPABASE_URL=SUPA, SUPABASE_SERVICE_KEY='',
                              SUPABASE_BUCKET='uploads',
                              SUPABASE_STORAGE_BUCKET_MEDIA='school-media')
        with cls.app.app_context():
            cls.role_ids = {n: Role.query.filter_by(name=n).first().id
                            for n in ('school_admin', 'parent', 'teacher')}

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        # Every fresh link goes through make_remote_token — record each mint.
        self.mint = mock.patch.object(upload_access, 'make_remote_token',
                                      wraps=upload_access.make_remote_token).start()
        self.fetch = mock.patch('app.utils.helpers._supabase_fetch',
                                return_value=(b'DATA', 'application/pdf')).start()
        self.sign = mock.patch('app.utils.helpers._supabase_sign', return_value=None).start()
        self.addCleanup(mock.patch.stopall)
        self.ids, self.keys = {}, {}
        with self.app.app_context():
            for key in ('a', 'b'):
                self._school(key)
            db.session.commit()

    def _k(self, label):
        return f'{label}-{self.sfx}-{uuid4().hex}'

    def _school(self, key):
        s = self.sfx
        school = School(school_name=f'PUA {key} {s}', code=f'PU{key}{s}'[:20],
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
            u = User(username=f'pu{label}{key}_{s}', email=f'pu{label}{key}_{s}@t.test',
                     full_name=f'{label} {key}', role_id=self.role_ids[role],
                     school_id=school.id, is_active=True)
            u.set_password(PASSWORD)
            db.session.add(u)
            db.session.flush()
            return u

        admin, parent = user('adm', 'school_admin'), user('par', 'parent')
        teacher, other = user('t', 'teacher'), user('o', 'teacher')
        k = {name: self._k(name) for name in
             ('edoc', 'ephoto', 'edisp', 'sdoc', 'sphoto', 'sdisp')}
        k = {'edoc': f"employee_docs/{k['edoc']}.pdf",
             'ephoto': f"employees/{k['ephoto']}.jpg",
             'edisp': f"employees/display/{k['edisp']}.webp",
             'sdoc': f"students/documents/{k['sdoc']}.webp",
             'sphoto': f"students/{k['sphoto']}.jpg",
             'sdisp': f"students/display/{k['sdisp']}.webp"}
        emp = Employee(school_id=school.id, employee_id=f'PU{key}{s}', full_name=f'T {key}',
                       base_salary=0, status='active', user_id=teacher.id,
                       photo=PUBLIC + k['ephoto'],                 # Supabase-URL shape
                       photo_display=f"uploads/{k['edisp']}")      # relative shape
        other_emp = Employee(school_id=school.id, employee_id=f'PO{key}{s}',
                             full_name=f'O {key}', base_salary=0, status='active',
                             user_id=other.id)
        db.session.add_all([emp, other_emp])
        db.session.flush()
        db.session.execute(Section.__table__.update().where(Section.id == sec.id)
                           .values(teacher_id=emp.id))             # homeroom of the child
        st = Student(student_id=f'PS{key}-{s}', full_name=f'Child {key}', school_id=school.id,
                     academic_year_id=year.id, section_id=sec.id, status='active',
                     photo=f"uploads/{k['sphoto']}",               # relative shape
                     photo_display=PUBLIC + k['sdisp'])            # Supabase-URL shape
        db.session.add(st)
        db.session.flush()
        db.session.execute(parent_students.insert().values(
            user_id=parent.id, student_id=st.id, relation='guardian'))
        db.session.add_all([
            EmployeeDocument(employee_id=emp.id, school_id=school.id, title='ID',
                             file_path=PUBLIC + k['edoc']),
            StudentDocument(student_id=st.id, school_id=school.id, academic_year_id=year.id,
                            document_type='ID', file_path=PUBLIC + k['sdoc'])])
        db.session.flush()
        self.keys[key] = k
        self.ids.update({f'school_{key}': school.id, f'admin_{key}': admin.username,
                         f'parent_{key}': parent.username, f'teacher_{key}': teacher.username,
                         f'other_{key}': other.username})

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
                for model in (AuditLog, EmployeeDocument, StudentDocument, Student, Section,
                              Grade, Employee, User, AcademicYear):
                    model.query.execution_options(**OPTS, include_all_years=True).filter_by(
                        school_id=sid).delete(synchronize_session=False)
                School.query.filter_by(id=sid).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _client(self, who=None):
        client = self.app.test_client()
        if who:
            resp = client.post('/auth/login', data={'username': self.ids[who],
                                                     'password': PASSWORD})
            self.assertEqual(resp.status_code, 302)
        return client

    def _assert_denied(self, client, key, label=''):
        for route in ROUTES:
            self.mint.reset_mock()
            resp = client.get(route + key)
            self.assertEqual(resp.status_code, 404, f'{label} {route}{key}')
            self.assertNotIn('/media-proxy/', resp.headers.get('Location', ''))
            self.mint.assert_not_called()                     # no link minted

    def _assert_allowed(self, client, key, label=''):
        for route in ROUTES:
            self.mint.reset_mock()
            resp = client.get(route + key)
            self.assertEqual(resp.status_code, 302, f'{label} {route}{key}')
            loc = urlparse(resp.headers['Location'])
            self.assertTrue(loc.path.endswith(f'/media-proxy/uploads/{key}'), loc.path)
            q = {n: v[0] for n, v in parse_qs(loc.query).items()}
            with self.app.app_context():
                self.assertTrue(upload_access.verify_remote_token('uploads', key,
                                                                  q['exp'], q['sig']))
            self.mint.assert_called_once()

    # ── 1. unauthenticated ────────────────────────────────────────────────────

    def test_01_unauthenticated_denied_for_every_kind(self):
        client = self._client()
        for label, key in self.keys['a'].items():
            self._assert_denied(client, key, label)
        self.fetch.assert_not_called()

    # ── 2, 5, 6. unauthorised / cross-school / unknown ────────────────────────

    def test_02_authenticated_but_unauthorised(self):
        k = self.keys['a']
        other = self._client('other_a')             # same school, another teacher
        for label in ('edoc', 'ephoto', 'edisp', 'sdoc', 'sphoto', 'sdisp'):
            self._assert_denied(other, k[label], f'other teacher {label}')
        parent = self._client('parent_a')            # parent: never employee files
        for label in ('edoc', 'ephoto', 'edisp'):
            self._assert_denied(parent, k[label], f'parent {label}')

    def test_05_cross_school_denied(self):
        for who in ('admin_b', 'teacher_b', 'parent_b'):
            client = self._client(who)
            for label, key in self.keys['a'].items():
                self._assert_denied(client, key, f'{who} {label}')

    def test_06_unknown_key_denied_even_for_admin(self):
        admin = self._client('admin_a')
        for key in (f'employee_docs/{uuid4().hex}.pdf', f'students/{uuid4().hex}.jpg',
                    f'schools/1/board/media/{uuid4().hex}.mp4', f'{uuid4().hex}.png'):
            self._assert_denied(admin, key, 'unknown')

    # ── 3, 4, 10-13. authorised access preserved ──────────────────────────────

    def test_03_10_13_same_school_admin_allowed(self):
        admin = self._client('admin_a')
        for label in RESOLVABLE:
            self._assert_allowed(admin, self.keys['a'][label], f'admin {label}')

    def test_04_teacher_own_and_assigned_scope(self):
        k = self.keys['a']
        teacher = self._client('teacher_a')
        for label in ('edoc', 'ephoto', 'edisp',            # own employee files
                      'sdoc', 'sphoto'):                    # homeroom student files
            self._assert_allowed(teacher, k[label], f'teacher {label}')

    def test_12_13_parent_own_child_only(self):
        k = self.keys['a']
        parent = self._client('parent_a')
        for label in ('sdoc', 'sphoto'):
            self._assert_allowed(parent, k[label], f'parent {label}')

    def test_13_student_display_copy_stays_fail_closed(self):
        # resolve_upload_owner() has never mapped Student.photo_display (the
        # UI mints signed /media-proxy links for it directly and never links
        # /media or /files). Its owner is therefore unknown on these routes, so
        # — exactly like before for /files — nobody can mint a link through them.
        key = self.keys['a']['sdisp']
        for who in (None, 'admin_a', 'teacher_a', 'parent_a', 'admin_b'):
            self._assert_denied(self._client(who), key, f'{who} sdisp')

    def test_11_soft_deleted_student_document_denied(self):
        from datetime import datetime
        with self.app.app_context():
            doc = StudentDocument.query.execution_options(**OPTS, include_all_years=True).filter_by(
                school_id=self.ids['school_a']).one()
            doc.deleted_at = datetime.utcnow()
            db.session.commit()
        self._assert_denied(self._client('admin_a'), self.keys['a']['sdoc'], 'soft-deleted')

    # ── 7, 8. /media-proxy HMAC unchanged ─────────────────────────────────────

    def test_07_forged_or_expired_proxy_signature_rejected(self):
        key = self.keys['a']['edoc']
        with self.app.app_context():
            exp, sig = upload_access.make_remote_token('uploads', key, ttl=900)
        client = self._client()
        for q in (f'exp={exp}&sig=forged', f'exp=1&sig={sig}', 'exp=&sig=', ''):
            resp = client.get(f'/media-proxy/uploads/{key}?{q}')
            self.assertEqual(resp.status_code, 403, q)
        wrong = self.keys['a']['ephoto']                    # token bound to another key
        self.assertEqual(client.get(f'/media-proxy/uploads/{wrong}?exp={exp}&sig={sig}')
                         .status_code, 403)
        self.fetch.assert_not_called()

    def test_08_already_issued_valid_proxy_link_unchanged(self):
        key = self.keys['a']['edoc']
        with self.app.app_context(), self.app.test_request_context('/'):
            url = upload_access.signed_proxy_url('uploads', key)
        loc = urlparse(url)
        resp = self._client().get(f'{loc.path}?{loc.query}')   # anonymous, as before
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b'DATA')
        self.fetch.assert_called_once_with(key, bucket='uploads')

    # ── 9. flag off: legacy behaviour exactly as before ───────────────────────

    def test_09_flag_off_unchanged(self):
        key = self.keys['a']['edoc']
        with mock.patch.dict(self.app.config, {'PRIVATE_UPLOADS_ENABLED': False}):
            anon = self._client()
            media = anon.get('/media/uploads/' + key)
            self.assertEqual(media.status_code, 404)             # local send (file absent)
            self.assertNotIn('Location', media.headers)
            files = anon.get('/files/uploads/' + key)
            self.assertEqual(files.status_code, 302)             # login redirect, as before
            self.assertIn('/auth/login', files.headers['Location'])
            other = self._client('admin_b').get('/files/uploads/' + key)
            self.assertEqual(other.status_code, 403)             # existing 403 semantics
        self.mint.assert_not_called()


if __name__ == '__main__':
    unittest.main()

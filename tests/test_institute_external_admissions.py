"""
Institute external admissions — focused guarantees.

Public side: an institute's /register/<token> form asks for NO grade (no
replacement field, no study groups) and its requests are stored with
desired_grade_id NULL; a posted grade is discarded. A school's form still shows
and requires its grade exactly as before.

Staff side for an institute:

  * the "طلبات التسجيل الخارجي" sidebar item is shown again for institutes,
    while the other school-only items stay hidden;
  * the admissions detail page offers the institute's active study groups of
    the active year instead of the stage/grade/section cascade;
  * approval is sectionless (a forged section_id is ignored), validates every
    group id against THIS institute + active year, enrolls the student in the
    same transaction, and writes nothing at all when any id is invalid;
  * a normal school keeps its section placement exactly as before.

Storage is an in-memory fake (nothing reaches Supabase).
"""
import io
import unittest
from datetime import date
from unittest import mock
from uuid import uuid4

from PIL import Image

from app import create_app
from app.models import (db, AcademicYear, AuditLog, Employee, Grade,
                        InstituteGroupEnrollment, InstituteStudyGroup, Notification,
                        Permission, Role, School, Section, Student, StudentDocument,
                        StudentRegistrationRequest, StudentRegistrationRequestDocument,
                        Subject, User, parent_students, user_permissions)
from app.utils.registration_tokens import generate_token, hash_token

PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True}
PUBLIC = 'https://storage.test/storage/v1/object/public/'
ADMISSIONS_LABEL = 'طلبات التسجيل الخارجي'
GRADE_LABEL = 'الصف الدراسي المطلوب'

PDF = (b'%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n'
       b'2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n')


def _jpeg(w=600, h=800):
    buf = io.BytesIO()
    Image.new('RGB', (w, h), (180, 150, 120)).save(buf, 'JPEG', quality=85)
    return buf.getvalue()


class FakeStorage:
    def __init__(self):
        self.objects, self.writes = {}, []

    def upload(self, data, path, ctype, bucket=None):
        bucket = bucket or 'uploads'
        self.objects[(bucket, path)] = data
        self.writes.append((bucket, path))
        return f'{PUBLIC}{bucket}/{path}'

    def fetch(self, path, bucket=None):
        data = self.objects.get((bucket or 'uploads', path))
        return (data, 'application/octet-stream') if data is not None else (None, None)

    def delete(self, path, bucket=None):
        self.objects.pop((bucket or 'uploads', path), None)
        return True


class InstituteExternalAdmissionsTest(unittest.TestCase):

    ORGS = ('ia', 'ib', 'sc')   # institute A, institute B, normal school

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['RATELIMIT_ENABLED'] = False
        with cls.app.app_context():
            cls.role_ids = {}
            for name in ('school_admin', 'hr'):
                role = Role.query.filter_by(name=name).first()
                assert role is not None, f'seed role {name!r} before running'
                cls.role_ids[name] = role.id

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.fs = FakeStorage()
        mock.patch('app.utils.helpers._supabase_upload', side_effect=self.fs.upload).start()
        mock.patch('app.utils.helpers._supabase_fetch', side_effect=self.fs.fetch).start()
        mock.patch('app.utils.helpers._supabase_delete', side_effect=self.fs.delete).start()
        mock.patch('app.utils.helpers._supabase_sign', return_value=None).start()
        self.addCleanup(mock.patch.stopall)
        self.ip = 0
        self.ids = {}
        with self.app.app_context():
            self._org('ia', institute=True)
            self._org('ib', institute=True)
            self._org('sc', institute=False)
            db.session.commit()

    def _org(self, key, *, institute):
        s = self.sfx
        token = generate_token()
        school = School(school_name=f'IEA {key} {s}', code=f'IE{key}{s}'[:20], capacity=0,
                        is_active=True, external_registration_enabled=True,
                        registration_token_hash=hash_token(token),
                        institution_type=School.INSTITUTION_INSTITUTE if institute else None)
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'Y{key}{s}', is_current=True,
                            start_date=date(2026, 8, 1), end_date=date(2027, 6, 30))
        old_year = AcademicYear(school_id=school.id, name=f'OY{key}{s}', is_current=False,
                                start_date=date(2025, 8, 1), end_date=date(2026, 6, 30))
        db.session.add_all([year, old_year])
        db.session.flush()
        grade = Grade(name=f'G{key}', school_id=school.id, academic_year_id=year.id,
                      stage='متوسطة')
        db.session.add(grade)
        db.session.flush()
        # Institutes are provisioned with grades + section "أ" like schools, so a
        # real section exists that a forged request could try to attach.
        sec = Section(name=f'{key}1', grade_id=grade.id, school_id=school.id,
                      academic_year_id=year.id)
        db.session.add(sec)
        db.session.flush()
        emp = Employee(school_id=school.id, employee_id=f'IE{key}{s}', full_name=f'Inst {key}',
                       base_salary=0, status='active')
        subj = Subject(school_id=school.id, academic_year_id=year.id,
                       name=f'Subj {key}', code=f'SJ{key}{s}'[:20])
        db.session.add_all([emp, subj])
        db.session.flush()

        def group(name, *, active=True, year_id=year.id):
            g = InstituteStudyGroup(school_id=school.id, academic_year_id=year_id,
                                    subject_id=subj.id, instructor_id=emp.id,
                                    name=f'{name} {key}', is_active=active)
            db.session.add(g)
            db.session.flush()
            return g.id

        groups = {}
        if institute:
            groups = {'g1': group('Alpha'), 'g2': group('Beta'),
                      'inactive': group('Dormant', active=False),
                      'old_year': group('Past', year_id=old_year.id)}

        def user(label, role):
            u = User(username=f'iea{label}{key}_{s}', email=f'iea{label}{key}_{s}@t.test',
                     full_name=f'{label} {key}', role_id=self.role_ids[role],
                     school_id=school.id, is_active=True)
            u.set_password(PASSWORD)
            db.session.add(u)
            db.session.flush()
            return u

        admin = user('adm', 'school_admin')
        hr = user('hr', 'hr')
        # view_students WITHOUT add_student: a role holding neither, plus the one
        # extra permission (the accountant role is confined to finance pages).
        viewer = user('vw', 'hr')
        viewer.extra_permissions.append(self.view_students_perm())
        db.session.flush()
        self.ids[key] = {'token': token, 'school': school.id, 'year': year.id,
                         'old_year': old_year.id, 'grade': grade.id, 'sec': sec.id,
                         'groups': groups, 'admin': admin.username,
                         'viewer': viewer.username, 'hr': hr.username}

    @staticmethod
    def view_students_perm():
        perm = Permission.query.filter_by(name='view_students').first()
        assert perm is not None, 'seed permission view_students before running'
        return perm

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for key in self.ORGS:
                sid = self.ids[key]['school']
                InstituteGroupEnrollment.query.execution_options(**OPTS).filter_by(
                    school_id=sid).delete(synchronize_session=False)
                InstituteStudyGroup.query.execution_options(**OPTS).filter_by(
                    school_id=sid).delete(synchronize_session=False)
                uids = [u.id for u in User.query.execution_options(**OPTS)
                        .filter_by(school_id=sid).all()]
                stids = [st.id for st in Student.query.execution_options(**OPTS)
                         .filter_by(school_id=sid).all()]
                if stids:
                    db.session.execute(parent_students.delete().where(
                        parent_students.c.student_id.in_(stids)))
                if uids:
                    db.session.execute(user_permissions.delete().where(
                        user_permissions.c.user_id.in_(uids)))
                    AuditLog.query.execution_options(**OPTS).filter(
                        AuditLog.user_id.in_(uids)).delete(synchronize_session=False)
                    db.session.execute(parent_students.delete().where(
                        parent_students.c.user_id.in_(uids)))
                for model in (StudentRegistrationRequestDocument, StudentRegistrationRequest,
                              StudentDocument, Notification, AuditLog, Student, Subject,
                              Section, Grade, Employee, User):
                    model.query.execution_options(**OPTS).filter_by(
                        school_id=sid).delete(synchronize_session=False)
                AcademicYear.query.execution_options(**OPTS).filter_by(
                    school_id=sid).delete(synchronize_session=False)
                School.query.filter_by(id=sid).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _web(self, key, who='admin'):
        client = self.app.test_client()
        resp = client.post('/auth/login', data={'username': self.ids[key][who],
                                                'password': PASSWORD})
        self.assertEqual(resp.status_code, 302)
        return client

    DEFAULT = object()

    def _post_public(self, key, grade=DEFAULT, photo=None, docs=()):
        """POST the public form. By default a school sends its own grade and an
        institute sends none (its form has no grade field); pass an id to send
        one explicitly, or None to omit it."""
        self.ip += 1
        nonce = uuid4().hex
        data = {'full_name': f'Applicant {uuid4().hex[:6]}', 'gender': 'male',
                'submission_nonce': nonce}
        if grade is self.DEFAULT:
            grade = self.ids[key]['grade'] if key == 'sc' else None
        if grade is not None:
            data['desired_grade_id'] = str(grade)
        if photo is not None:
            data['photo'] = (io.BytesIO(photo), 'p.jpg', 'application/octet-stream')
        if docs:
            data['document_type[]'] = [t for t, _, _ in docs]
            data['document_file[]'] = [(io.BytesIO(b), n, 'application/octet-stream')
                                       for _, n, b in docs]
        resp = self.app.test_client().post(
            f"/register/{self.ids[key]['token']}", data=data,
            content_type='multipart/form-data',
            environ_base={'REMOTE_ADDR': f'10.8.{self.ip // 250}.{self.ip % 250 + 1}'})
        return resp, nonce

    def _find_request(self, key, nonce):
        with self.app.app_context():
            return (StudentRegistrationRequest.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids[key]['school'], submission_nonce=nonce)
                    .first())

    def _submit(self, key, grade=DEFAULT, photo=None, docs=()):
        """POST the public form and assert it was accepted; return the request id."""
        resp, nonce = self._post_public(key, grade=grade, photo=photo, docs=docs)
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True)[:400])
        self.assertIn('/register/track/', resp.headers['Location'])
        with self.app.app_context():
            req = (StudentRegistrationRequest.query.execution_options(**OPTS)
                   .filter_by(school_id=self.ids[key]['school'], submission_nonce=nonce)
                   .one())
            return req.id

    def _approve(self, key, req_id, *, groups=(), section_id=None, client=None, who='admin'):
        data = {'parent_choice': 'new'}
        if groups:
            data['institute_group_ids'] = [str(g) for g in groups]
        if section_id is not None:
            data['section_id'] = str(section_id)
        return (client or self._web(key, who)).post(f'/admissions/{req_id}/approve',
                                                    data=data)

    def _state(self, key, req_id):
        with self.app.app_context():
            req = db.session.get(StudentRegistrationRequest, req_id, execution_options=OPTS)
            sid = self.ids[key]['school']
            students = Student.query.execution_options(**OPTS).filter_by(school_id=sid).all()
            enrollments = (InstituteGroupEnrollment.query.execution_options(**OPTS)
                           .filter_by(school_id=sid).all())
            student = (db.session.get(Student, req.approved_student_id, execution_options=OPTS)
                       if req.approved_student_id else None)
            return {
                'status': req.status,
                'student': None if student is None else {
                    'id': student.id, 'school_id': student.school_id,
                    'section_id': student.section_id, 'photo': student.photo},
                'n_students': len(students),
                'enrollments': sorted((e.group_id, e.student_id, e.status)
                                      for e in enrollments),
                'photo_path': req.student_photo_path,
                'desired_grade_id': req.desired_grade_id,
            }

    def _assert_nothing_written(self, key, req_id):
        st = self._state(key, req_id)
        self.assertEqual(st['status'], 'pending')
        self.assertIsNone(st['student'])
        self.assertEqual(st['n_students'], 0, 'no student may remain from a failed approval')
        self.assertEqual(st['enrollments'], [], 'no partial enrollments may remain')

    def _href(self, endpoint):
        with self.app.test_request_context():
            from flask import url_for
            return f'href="{url_for(endpoint)}"'

    # ── 1-3. Sidebar ──────────────────────────────────────────────────────────

    def test_01_02_institute_sidebar_shows_admissions_but_keeps_school_items_hidden(self):
        html = self._web('ia').get('/admissions/').get_data(as_text=True)
        self.assertIn(ADMISSIONS_LABEL, html)
        self.assertIn(self._href('admissions.index'), html)
        for endpoint in ('student_records.index', 'attendance.index',
                         'admin.attendance_settings', 'school_calendar.index'):
            self.assertNotIn(self._href(endpoint), html, endpoint)

    def test_03_school_sidebar_unchanged(self):
        html = self._web('sc').get('/admissions/').get_data(as_text=True)
        self.assertIn(ADMISSIONS_LABEL, html)
        self.assertIn(self._href('admissions.index'), html)
        self.assertIn(self._href('student_records.index'), html)

    # ── 4-5. Detail page ──────────────────────────────────────────────────────

    def test_04_05_institute_detail_shows_own_active_groups_not_sections(self):
        req_id = self._submit('ia')
        resp = self._web('ia').get(f'/admissions/{req_id}')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        g = self.ids['ia']['groups']
        other = self.ids['ib']['groups']
        self.assertIn('name="institute_group_ids"', html)
        self.assertIn(f'value="{g["g1"]}"', html)
        self.assertIn(f'value="{g["g2"]}"', html)
        self.assertIn('Alpha ia', html)
        for hidden in ('Dormant ia', 'Past ia', 'Alpha ib', 'Beta ib'):
            self.assertNotIn(hidden, html)
        self.assertNotIn(f'id="grp{other["g1"]}"', html)
        self.assertNotIn('name="section_id"', html)
        self.assertNotIn('id="fSection"', html)
        self.assertNotIn('initClassCascade(', html)

    def test_13a_school_detail_keeps_stage_grade_section_cascade(self):
        req_id = self._submit('sc')
        html = self._web('sc').get(f'/admissions/{req_id}').get_data(as_text=True)
        self.assertIn('name="section_id"', html)
        self.assertIn('id="fStage"', html)
        self.assertIn('initClassCascade(', html)
        self.assertNotIn('name="institute_group_ids"', html)

    # ── 7-9. Valid institute approval ─────────────────────────────────────────

    def test_07_08_09_multi_group_approval_is_sectionless_even_with_forged_section(self):
        g = self.ids['ia']['groups']
        req_id = self._submit('ia')
        resp = self._approve('ia', req_id, groups=[g['g1'], g['g2']],
                             section_id=self.ids['ia']['sec'])
        self.assertEqual(resp.status_code, 200)       # one-time credential panel
        st = self._state('ia', req_id)
        self.assertEqual(st['status'], 'approved')
        self.assertEqual(st['student']['school_id'], self.ids['ia']['school'])
        self.assertIsNone(st['student']['section_id'])
        sid = st['student']['id']
        self.assertEqual(st['enrollments'],
                         sorted([(g['g1'], sid, 'active'), (g['g2'], sid, 'active')]))

    def test_12_duplicate_group_ids_create_one_enrollment_each(self):
        g = self.ids['ia']['groups']
        req_id = self._submit('ia')
        self._approve('ia', req_id, groups=[g['g1'], g['g1'], g['g2'], g['g1']])
        st = self._state('ia', req_id)
        sid = st['student']['id']
        self.assertEqual(st['enrollments'],
                         sorted([(g['g1'], sid, 'active'), (g['g2'], sid, 'active')]))

    def test_zero_groups_allowed_like_internal_add_student(self):
        req_id = self._submit('ia')
        self._approve('ia', req_id)
        st = self._state('ia', req_id)
        self.assertEqual(st['status'], 'approved')
        self.assertIsNone(st['student']['section_id'])
        self.assertEqual(st['enrollments'], [])

    def test_idempotent_reapproval_adds_no_enrollments(self):
        g = self.ids['ia']['groups']
        req_id = self._submit('ia')
        client = self._web('ia')
        self._approve('ia', req_id, groups=[g['g1']], client=client)
        first = self._state('ia', req_id)
        resp = self._approve('ia', req_id, groups=[g['g1'], g['g2']], client=client)
        self.assertEqual(resp.status_code, 302)
        again = self._state('ia', req_id)
        self.assertEqual(again['enrollments'], first['enrollments'])
        self.assertEqual(again['n_students'], 1)

    # ── 6, 10, 11. Invalid ids fail the WHOLE approval ────────────────────────

    def test_06_10_11_any_invalid_group_id_writes_nothing(self):
        g = self.ids['ia']['groups']
        cases = {
            'inactive':      [g['g1'], g['inactive']],
            'other_year':    [g['g1'], g['old_year']],
            'cross_school':  [g['g1'], self.ids['ib']['groups']['g1']],
            'nonexistent':   [g['g2'], 987654321],
        }
        client = self._web('ia')
        for label, ids in cases.items():
            with self.subTest(label):
                req_id = self._submit('ia')
                resp = self._approve('ia', req_id, groups=ids, client=client)
                self.assertEqual(resp.status_code, 302)
                self.assertIn(f'/admissions/{req_id}', resp.headers['Location'])
                self._assert_nothing_written('ia', req_id)

    def test_non_integer_group_id_writes_nothing(self):
        req_id = self._submit('ia')
        client = self._web('ia')
        resp = client.post(f'/admissions/{req_id}/approve',
                           data={'parent_choice': 'new',
                                 'institute_group_ids': [str(self.ids['ia']['groups']['g1']),
                                                         'abc']})
        self.assertEqual(resp.status_code, 302)
        self._assert_nothing_written('ia', req_id)

    def test_cross_school_request_is_404_for_other_institute(self):
        req_id = self._submit('ib')
        resp = self._approve('ia', req_id, groups=[self.ids['ia']['groups']['g1']])
        self.assertEqual(resp.status_code, 404)
        self._assert_nothing_written('ib', req_id)
        self.assertEqual(self._state('ia', req_id)['enrollments'], [])

    # ── 13. Normal school unchanged ───────────────────────────────────────────

    def test_13_school_approval_keeps_section_and_ignores_group_ids(self):
        req_id = self._submit('sc')
        resp = self._approve('sc', req_id, section_id=self.ids['sc']['sec'],
                             groups=[self.ids['ia']['groups']['g1']])
        self.assertEqual(resp.status_code, 200)
        st = self._state('sc', req_id)
        self.assertEqual(st['status'], 'approved')
        self.assertEqual(st['student']['section_id'], self.ids['sc']['sec'])
        self.assertEqual(st['enrollments'], [])
        self.assertEqual(self._state('ia', req_id)['enrollments'], [])

    def test_13b_school_foreign_section_still_rejected(self):
        req_id = self._submit('sc')
        self._approve('sc', req_id, section_id=self.ids['ia']['sec'])
        self._assert_nothing_written('sc', req_id)

    # ── 14-15. Public form + media unchanged ──────────────────────────────────

    def test_14_15_public_form_and_media_approval_for_institute(self):
        get = self.app.test_client().get(f"/register/{self.ids['ia']['token']}")
        self.assertEqual(get.status_code, 200)
        html = get.get_data(as_text=True)
        self.assertNotIn('desired_grade_id', html)
        self.assertNotIn(GRADE_LABEL, html)
        self.assertNotIn('institute_group_ids', html)
        self.assertNotIn('Alpha ia', html)
        self.assertNotIn('Subj ia', html)

        req_id = self._submit('ia', photo=_jpeg(), docs=[('شهادة', 'c.pdf', PDF)])
        before = self._state('ia', req_id)
        self.assertIn('/registration/', before['photo_path'])
        self.assertIn('/v2/', before['photo_path'])
        g = self.ids['ia']['groups']
        self._approve('ia', req_id, groups=[g['g1']])
        st = self._state('ia', req_id)
        self.assertEqual(st['status'], 'approved')
        self.assertEqual(st['student']['photo'], before['photo_path'])
        with self.app.app_context():
            req_docs = [d.file_path for d in StudentRegistrationRequestDocument.query
                        .execution_options(**OPTS).filter_by(request_id=req_id)]
            stu_docs = [d.file_path for d in StudentDocument.query
                        .execution_options(**OPTS, include_all_years=True)
                        .filter_by(student_id=st['student']['id'])]
        self.assertEqual(sorted(stu_docs), sorted(req_docs))
        self.assertEqual(len(stu_docs), 1)
        self.assertIsNone(st['desired_grade_id'])
        self.assertIsNone(st['student']['section_id'])

    # ── Public form: no grade for institutes, unchanged for schools ───────────

    def test_g1_g2_g7_institute_public_form_has_no_grade_or_groups(self):
        html = self.app.test_client().get(
            f"/register/{self.ids['ia']['token']}").get_data(as_text=True)
        self.assertNotIn(GRADE_LABEL, html)
        self.assertNotIn('desired_grade_id', html)
        self.assertNotIn(f'>G{"ia"}<', html)
        for leak in ('institute_group_ids', 'Alpha ia', 'Beta ia', 'Subj ia', 'Inst ia'):
            self.assertNotIn(leak, html)
        self.assertIn('name="full_name"', html)          # rest of the form intact

    def test_g3_g4_institute_submits_without_grade_and_stores_null(self):
        req_id = self._submit('ia')
        self.assertIsNone(self._state('ia', req_id)['desired_grade_id'])

    def test_institute_forged_grade_is_discarded_not_stored(self):
        # A stale/forged grade id — own or another school's — is never stored.
        for grade in (self.ids['ia']['grade'], self.ids['sc']['grade']):
            with self.subTest(grade=grade):
                req_id = self._submit('ia', grade=grade)
                self.assertIsNone(self._state('ia', req_id)['desired_grade_id'])

    def test_g5_school_public_form_still_shows_required_grade(self):
        html = self.app.test_client().get(
            f"/register/{self.ids['sc']['token']}").get_data(as_text=True)
        self.assertIn(GRADE_LABEL, html)
        self.assertIn('<select name="desired_grade_id" class="form-select" required>', html)
        self.assertIn(f'value="{self.ids["sc"]["grade"]}"', html)

    def test_g6_school_submission_without_grade_still_rejected(self):
        writes = len(self.fs.writes)
        resp, nonce = self._post_public('sc', grade=None, photo=_jpeg())
        self.assertEqual(resp.status_code, 200)
        self.assertIn('يرجى اختيار الصف الدراسي.', resp.get_data(as_text=True))
        self.assertIsNone(self._find_request('sc', nonce))
        self.assertEqual(len(self.fs.writes), writes, 'a refused submission stores nothing')

    def test_school_submission_with_grade_stores_it(self):
        req_id = self._submit('sc')
        self.assertEqual(self._state('sc', req_id)['desired_grade_id'], self.ids['sc']['grade'])

    def test_g11_school_cannot_use_another_schools_grade(self):
        resp, nonce = self._post_public('sc', grade=self.ids['ia']['grade'])
        self.assertEqual(resp.status_code, 200)
        self.assertIn('يرجى اختيار الصف الدراسي.', resp.get_data(as_text=True))
        self.assertIsNone(self._find_request('sc', nonce))

    def test_g8_g9_no_grade_request_detail_and_group_approval(self):
        req_id = self._submit('ia')
        client = self._web('ia')
        detail = client.get(f'/admissions/{req_id}')
        self.assertEqual(detail.status_code, 200)
        self.assertIn('name="institute_group_ids"', detail.get_data(as_text=True))
        g = self.ids['ia']['groups']
        self._approve('ia', req_id, groups=[g['g1'], g['g2']], client=client)
        st = self._state('ia', req_id)
        self.assertEqual(st['status'], 'approved')
        self.assertIsNone(st['student']['section_id'])
        self.assertIsNone(st['desired_grade_id'])
        sid = st['student']['id']
        self.assertEqual(st['enrollments'],
                         sorted([(g['g1'], sid, 'active'), (g['g2'], sid, 'active')]))

    # ── 16. Permissions unchanged ─────────────────────────────────────────────

    def test_16_permissions_enforced(self):
        req_id = self._submit('ia')
        with self.app.app_context():
            viewer = User.query.execution_options(**OPTS).filter_by(
                username=self.ids['ia']['viewer']).one()
            hr = User.query.execution_options(**OPTS).filter_by(
                username=self.ids['ia']['hr']).one()
            self.assertTrue(viewer.has_permission('view_students'))
            self.assertFalse(viewer.has_permission('add_student'))
            self.assertFalse(hr.has_permission('view_students'))
            self.assertFalse(hr.has_permission('add_student'))

        hr_client = self._web('ia', 'hr')
        self.assertEqual(hr_client.get('/admissions/').status_code, 403)
        self.assertEqual(hr_client.get(f'/admissions/{req_id}').status_code, 403)

        viewer_client = self._web('ia', 'viewer')
        queue = viewer_client.get('/admissions/')
        self.assertEqual(queue.status_code, 200)
        self.assertIn(ADMISSIONS_LABEL, queue.get_data(as_text=True))
        resp = self._approve('ia', req_id, groups=[self.ids['ia']['groups']['g1']],
                             client=viewer_client)
        self.assertEqual(resp.status_code, 403)
        self._assert_nothing_written('ia', req_id)


if __name__ == '__main__':
    unittest.main()

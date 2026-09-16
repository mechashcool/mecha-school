"""
Focused tests for the per-school educational-stages feature.

Covered:
  * new-school creation provisions ONLY the selected stages (grades + one
    section "أ" per grade + that stage's standard subjects)
  * creating a school with no stage is rejected outright
  * legacy schools (educational_stages IS NULL) are preserved untouched
  * editing an unrelated field of a legacy school changes no structural row
  * adding a stage creates only the missing rows
  * re-saving the same selection creates no duplicates
  * removing a stage that has linked data is rejected with the Arabic message
  * a rejected stage change rolls back the WHOLE edit (no partial update)
  * subjects are filtered to the selected stages' grades
  * the external registration form shows only the allowed grades
  * a manually submitted out-of-stage / cross-school grade id is rejected

Run against the isolated LOCAL Postgres named by TEST_DATABASE_URL:
    .venv\\Scripts\\python.exe -m pytest tests/test_school_educational_stages.py -v
"""
import unittest
from datetime import date
from uuid import uuid4

from app import create_app
from app.models import (db, Role, School, User, AcademicYear, Grade, Section,
                        Subject, Student, Notification, ChatRoom, Homework,
                        StudentRegistrationRequest,
                        StudentRegistrationRequestDocument, parent_students)
from app.utils.iraqi_grades import IRAQI_STANDARD_GRADES
from app.utils.registration_tokens import generate_token, hash_token, encrypt_token
from app.utils.school_stages import (STAGE_PRIMARY, STAGE_INTERMEDIATE,
                                     STAGE_PREPARATORY, ERR_STAGE_LINKED,
                                     ERR_NO_STAGE, ERR_STORED_INVALID,
                                     ERR_AMBIGUOUS, DEFAULT_SECTION_NAME,
                                     InvalidStageConfiguration, school_stages)
from app.utils.iraqi_subjects import STANDARD_SUBJECTS_BY_GRADE


def _uid():
    return uuid4().hex[:10]


def _names_for(stage):
    return {n for n, s in IRAQI_STANDARD_GRADES if s == stage}


PRIMARY_NAMES = _names_for(STAGE_PRIMARY)
INTERMEDIATE_NAMES = _names_for(STAGE_INTERMEDIATE)
PREPARATORY_NAMES = _names_for(STAGE_PREPARATORY)


class SchoolStagesTestBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['WTF_CSRF_ENABLED'] = False
        cls.app.config['RATELIMIT_ENABLED'] = False

    def setUp(self):
        self.sfx = _uid()
        self.ctx = self.app.test_request_context()
        self.ctx.push()
        self.client = self.app.test_client()
        self._created_school_ids = []

        self.super_role = Role.query.filter_by(name='super_admin').first()
        self.admin_role = Role.query.filter_by(name='school_admin').first()
        self.assertIsNotNone(self.super_role, 'Seed roles before running these tests')

        self.super_user = User(
            username=f'sa_{self.sfx}', full_name='Super Admin',
            email=f'sa_{self.sfx}@t.com', role_id=self.super_role.id,
            school_id=None, is_active=True)
        self.super_user.set_password('pw12345')
        db.session.add(self.super_user)
        db.session.commit()
        self.super_user_id = self.super_user.id
        self.super_username = self.super_user.username
        self._login(self.super_username)

    def tearDown(self):
        try:
            db.session.rollback()
            for sid in self._created_school_ids:
                self._purge_school(sid)
            User.query.execution_options(bypass_tenant_scope=True)\
                .filter_by(id=self.super_user_id).delete()
            db.session.commit()
        except Exception:
            db.session.rollback()
        self.ctx.pop()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _login(self, username, password='pw12345'):
        """Log in through the real /auth/login route.

        A hand-built session cookie is not reliable here: the outer
        ``test_request_context`` from setUp shares its app context with the test
        client, and the server-issued cookie from a genuine login is what keeps
        Flask-Login resolving the user on every later request.
        """
        resp = self._c().post('/auth/login',
                                data={'username': username, 'password': password},
                                follow_redirects=False)
        assert resp.status_code == 302, 'test login failed'
        return resp

    def _c(self):
        """Test client with the shared ``g`` reset first.

        Flask reuses an already-pushed app context for the same app, so the
        outer ``test_request_context`` from setUp shares ``g`` with every
        request the test client makes. Any ORM query run in the test body goes
        through app/utils/scoping.py, which resolves ``current_user`` and the
        tenant scope and caches both on ``g`` — pinning an anonymous,
        unscoped state for every later request. Clearing ``g`` immediately
        before each request is what makes the session cookie take effect.
        """
        from flask import g
        for key in list(vars(g)):
            g.pop(key, None)
        return self.client

    def _reload(self):
        """End the outer session's read transaction so rows committed by the
        test client's request are visible to the assertions below."""
        db.session.rollback()

    def _set_active_school(self, school_id):
        """Super-admin school switch — what the setup buttons resolve from.

        Logs in again through the real route first. A hand-written session is
        not durable here: Flask-Login's basic session protection drops
        ``_user_id`` when the session carries no server-issued ``_id``, so
        injecting the id by hand produces a silent login redirect. Logging in
        for real, then adding ``active_school_id`` on top, is deterministic.
        """
        self._login(self.super_username)
        with self.app.app_context():
            with self.client.session_transaction() as sess:
                sess['active_school_id'] = school_id

    def _track(self, school_id):
        if school_id and school_id not in self._created_school_ids:
            self._created_school_ids.append(school_id)
        return school_id

    def _purge_school(self, sid):
        from app.models import AuditLog, SchoolModuleConfig
        opts = dict(bypass_tenant_scope=True)
        StudentRegistrationRequestDocument.query.execution_options(**opts)\
            .filter_by(school_id=sid).delete()
        StudentRegistrationRequest.query.execution_options(**opts)\
            .filter_by(school_id=sid).delete()
        sids = [r[0] for r in db.session.execute(
            db.select(Student.id).where(Student.school_id == sid)).all()]
        if sids:
            db.session.execute(parent_students.delete().where(
                parent_students.c.student_id.in_(sids)))
        Student.query.execution_options(**opts).filter_by(school_id=sid).delete()
        Notification.query.execution_options(**opts).filter_by(school_id=sid).delete()
        AuditLog.query.execution_options(**opts).filter_by(school_id=sid).delete()
        from app.models import Employee, teacher_subjects
        emp_ids = [r[0] for r in db.session.execute(
            db.select(Employee.id).where(Employee.school_id == sid)).all()]
        if emp_ids:
            db.session.execute(teacher_subjects.delete().where(
                teacher_subjects.c.employee_id.in_(emp_ids)))
        Section.query.execution_options(**opts).filter_by(school_id=sid)            .update({'teacher_id': None}, synchronize_session=False)
        Employee.query.execution_options(**opts).filter_by(school_id=sid).delete()
        db.session.flush()
        for room in (ChatRoom.query.execution_options(**opts)
                     .filter_by(school_id=sid).all()):
            db.session.delete(room)
        db.session.flush()
        Homework.query.execution_options(**opts).filter_by(school_id=sid).delete()
        Subject.query.execution_options(**opts).filter_by(school_id=sid).delete()
        Section.query.execution_options(**opts).filter_by(school_id=sid).delete()
        Grade.query.execution_options(**opts).filter_by(school_id=sid).delete()
        SchoolModuleConfig.query.filter_by(school_id=sid).delete()
        User.query.execution_options(**opts).filter_by(school_id=sid).delete()
        AcademicYear.query.execution_options(**opts).filter_by(school_id=sid).delete()
        School.query.filter_by(id=sid).delete()

    def _create_school_via_route(self, stages, tag=None, with_year=True, **extra):
        tag = tag or _uid()
        data = {
            'school_name': f'S{tag}',
            'code': f'C{tag[:8]}'.upper(),
            'capacity': '0',
        }
        if with_year:
            data.update({'year_name': f'Y{tag}',
                         'year_start': '2025-08-01',
                         'year_end': '2026-06-30'})
        if stages:
            data['educational_stages'] = list(stages)
        data.update(extra)
        resp = self._c().post('/schools/create', data=data,
                                follow_redirects=False)
        school = School.query.filter_by(school_name=f'S{tag}').first()
        if school:
            self._track(school.id)
        return resp, school

    def _make_legacy_school(self, tag=None, grade_names=None):
        """A school exactly as it exists in production today: stages NULL."""
        tag = tag or _uid()
        s = School(school_name=f'L{tag}', code=f'L{tag[:8]}'.upper(),
                   is_active=True)
        db.session.add(s)
        db.session.flush()
        self._track(s.id)
        y = AcademicYear(school_id=s.id, name=f'Y{tag}',
                         start_date=date(2025, 8, 1), end_date=date(2026, 6, 30),
                         is_current=True)
        db.session.add(y)
        db.session.flush()
        if grade_names is None:
            grade_names = ['صف مخصص', 'الصف الأول الابتدائي']
        for name in grade_names:
            stage = next((st for n, st in IRAQI_STANDARD_GRADES if n == name), None)
            g = Grade(name=name, stage=stage, school_id=s.id, academic_year_id=y.id)
            db.session.add(g)
            db.session.flush()
            db.session.add(Section(name='أ', grade_id=g.id, school_id=s.id,
                                   academic_year_id=y.id))
        db.session.commit()
        return s, y

    def _grades(self, school_id):
        return (Grade.query.execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=school_id).all())

    def _grade_names(self, school_id):
        return {g.name for g in self._grades(school_id)}

    def _sections(self, school_id):
        return (Section.query.execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=school_id).all())

    def _subjects(self, school_id):
        return (Subject.query.execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=school_id).all())

    def _edit(self, school, stages=None, **overrides):
        data = {
            'school_name': school.school_name,
            'code': school.code or '',
            'capacity': str(school.capacity or 0),
            'is_active': 'on' if school.is_active else '',
        }
        if stages:
            data['educational_stages'] = list(stages)
        data.update(overrides)
        return self._c().post(f'/schools/{school.id}/edit', data=data,
                                follow_redirects=False)


# ═════════════════════════════════════════════════════════════════════════════
#  1. NEW SCHOOL CREATION
# ═════════════════════════════════════════════════════════════════════════════

class NewSchoolCreationTest(SchoolStagesTestBase):

    def test_creates_only_selected_stage_grades_with_section_alef(self):
        resp, school = self._create_school_via_route([STAGE_PRIMARY])
        self.assertEqual(resp.status_code, 302)
        self.assertIsNotNone(school)
        self.assertEqual(school.educational_stages, STAGE_PRIMARY)

        names = self._grade_names(school.id)
        self.assertEqual(names, PRIMARY_NAMES,
                         'only the selected stage grades may be created')
        self.assertFalse(names & INTERMEDIATE_NAMES)
        self.assertFalse(names & PREPARATORY_NAMES)

        sections = self._sections(school.id)
        self.assertEqual(len(sections), len(PRIMARY_NAMES))
        self.assertEqual({s.name for s in sections}, {DEFAULT_SECTION_NAME})
        self.assertEqual(len({s.grade_id for s in sections}), len(PRIMARY_NAMES),
                         'exactly one section per grade')

    def test_multiple_stages_create_their_union_only(self):
        _, school = self._create_school_via_route(
            [STAGE_PRIMARY, STAGE_INTERMEDIATE])
        names = self._grade_names(school.id)
        self.assertEqual(names, PRIMARY_NAMES | INTERMEDIATE_NAMES)
        self.assertFalse(names & PREPARATORY_NAMES)

    def test_subjects_are_filtered_to_selected_stage_grades(self):
        _, school = self._create_school_via_route([STAGE_INTERMEDIATE])
        subjects = self._subjects(school.id)
        self.assertTrue(subjects, 'standard subjects must be provisioned')

        grade_by_id = {g.id: g for g in self._grades(school.id)}
        for sub in subjects:
            self.assertIsNotNone(sub.grade_id)
            self.assertIn(grade_by_id[sub.grade_id].name, INTERMEDIATE_NAMES)

        expected = sum(len(STANDARD_SUBJECTS_BY_GRADE[n])
                       for n in INTERMEDIATE_NAMES)
        self.assertEqual(len(subjects), expected,
                         'exactly the standard subjects of the selected grades')

    def test_creation_requires_an_initial_academic_year(self):
        """Stage provisioning is scoped to a year, so a new school without one
        would be created in managed mode with no structure at all."""
        tag = _uid()
        resp = self._c().post('/schools/create', data={
            'school_name': f'S{tag}', 'code': f'C{tag[:8]}'.upper(),
            'capacity': '0',
            'educational_stages': [STAGE_PRIMARY],
        })
        self.assertEqual(resp.status_code, 200, 'form must be re-rendered')
        self.assertIsNone(School.query.filter_by(school_name=f'S{tag}').first(),
                          'no school row may be created without a year')

    def test_creation_rejects_an_invalid_year_range(self):
        tag = _uid()
        resp = self._c().post('/schools/create', data={
            'school_name': f'S{tag}', 'code': f'C{tag[:8]}'.upper(),
            'capacity': '0', 'educational_stages': [STAGE_PRIMARY],
            'year_name': f'Y{tag}',
            'year_start': '2026-06-30', 'year_end': '2025-08-01',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(School.query.filter_by(school_name=f'S{tag}').first())

    def test_creation_provisions_everything_in_one_transaction(self):
        """The year, its grades, one section أ each and their default subjects
        all land together."""
        _, school = self._create_school_via_route([STAGE_PRIMARY])
        self.assertIsNotNone(school)
        sid = school.id

        year = (AcademicYear.query.execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=sid, is_current=True).first())
        self.assertIsNotNone(year, 'the initial year must exist')

        grades = self._grades(sid)
        self.assertEqual({g.name for g in grades}, PRIMARY_NAMES)
        self.assertTrue(all(g.academic_year_id == year.id for g in grades))

        sections = self._sections(sid)
        self.assertEqual(len(sections), len(PRIMARY_NAMES))
        self.assertEqual({s.name for s in sections}, {DEFAULT_SECTION_NAME})
        self.assertEqual(len({s.grade_id for s in sections}), len(PRIMARY_NAMES))

        subjects = self._subjects(sid)
        expected = sum(len(STANDARD_SUBJECTS_BY_GRADE[n]) for n in PRIMARY_NAMES)
        self.assertEqual(len(subjects), expected)

    def test_creation_without_any_stage_is_rejected(self):
        tag = _uid()
        resp = self._c().post('/schools/create', data={
            'school_name': f'S{tag}', 'code': f'C{tag[:8]}'.upper(),
            'capacity': '0', 'year_name': f'Y{tag}',
            'year_start': '2025-08-01', 'year_end': '2026-06-30',
        })
        self.assertEqual(resp.status_code, 200, 'form must be re-rendered')
        self.assertIn(ERR_NO_STAGE, resp.get_data(as_text=True))
        self.assertIsNone(School.query.filter_by(school_name=f'S{tag}').first(),
                          'no school row may be created')

    def test_invalid_stage_value_is_rejected(self):
        tag = _uid()
        resp = self._c().post('/schools/create', data={
            'school_name': f'S{tag}', 'code': f'C{tag[:8]}'.upper(),
            'capacity': '0', 'educational_stages': ['رياض الأطفال'],
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(School.query.filter_by(school_name=f'S{tag}').first())

    def test_creation_is_idempotent_no_duplicate_rows(self):
        _, school = self._create_school_via_route([STAGE_PRIMARY])
        grades = self._grades(school.id)
        self.assertEqual(len(grades), len({g.name for g in grades}),
                         'no duplicate grade names')
        sections = self._sections(school.id)
        self.assertEqual(len(sections),
                         len({(s.grade_id, s.name) for s in sections}))
        subjects = self._subjects(school.id)
        self.assertEqual(len(subjects),
                         len({(s.grade_id, s.name) for s in subjects}))


# ═════════════════════════════════════════════════════════════════════════════
#  2. LEGACY SCHOOL PRESERVATION
# ═════════════════════════════════════════════════════════════════════════════

class LegacySchoolTest(SchoolStagesTestBase):

    def test_legacy_school_keeps_null_stages(self):
        school, _ = self._make_legacy_school()
        self.assertIsNone(school.educational_stages)

    def test_unrelated_edit_does_not_touch_structure_or_stages(self):
        school, year = self._make_legacy_school()
        # A subject so the "nothing changed" assertions are not vacuous.
        grade = self._grades(school.id)[0]
        db.session.add(Subject(name='مادة قديمة', school_id=school.id,
                               academic_year_id=year.id, grade_id=grade.id))
        db.session.commit()

        before_grades = self._grade_names(school.id)
        before_sections = {(s.grade_id, s.name) for s in self._sections(school.id)}
        before_subjects = {(s.grade_id, s.name) for s in self._subjects(school.id)}

        resp = self._edit(school, stages=None,
                          school_name=f'{school.school_name}-renamed',
                          phone='07700000000')
        self.assertEqual(resp.status_code, 302)

        db.session.expire_all()
        fresh = School.query.get(school.id)
        self.assertTrue(fresh.school_name.endswith('-renamed'),
                        'the unrelated field change itself must be saved')
        self.assertIsNone(fresh.educational_stages,
                          'an unrelated edit must never opt a school into managed mode')
        self.assertEqual(self._grade_names(school.id), before_grades)
        self.assertEqual({(s.grade_id, s.name) for s in self._sections(school.id)},
                         before_sections)
        self.assertEqual({(s.grade_id, s.name) for s in self._subjects(school.id)},
                         before_subjects)

    def test_legacy_external_registration_is_not_filtered(self):
        school, year = self._make_legacy_school(
            grade_names=['صف مخصص', 'الصف الأول المتوسط'])
        raw = generate_token()
        school.external_registration_enabled = True
        school.registration_token_hash = hash_token(raw)
        school.registration_token_encrypted = encrypt_token(raw)
        db.session.commit()

        resp = self._c().get(f'/register/{raw}')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('صف مخصص', body)
        self.assertIn('الصف الأول المتوسط', body)


# ═════════════════════════════════════════════════════════════════════════════
#  3. EDITING STAGES
# ═════════════════════════════════════════════════════════════════════════════

class StageEditTest(SchoolStagesTestBase):

    def test_adding_a_stage_creates_only_missing_rows(self):
        _, school = self._create_school_via_route([STAGE_PRIMARY])
        before_ids = {g.id for g in self._grades(school.id)}
        before_sec_ids = {s.id for s in self._sections(school.id)}

        resp = self._edit(school, stages=[STAGE_PRIMARY, STAGE_INTERMEDIATE])
        self.assertEqual(resp.status_code, 302)

        db.session.expire_all()
        fresh = School.query.get(school.id)
        self.assertEqual(fresh.educational_stages,
                         f'{STAGE_PRIMARY},{STAGE_INTERMEDIATE}')
        names = self._grade_names(school.id)
        self.assertEqual(names, PRIMARY_NAMES | INTERMEDIATE_NAMES)
        self.assertTrue(before_ids <= {g.id for g in self._grades(school.id)},
                        'existing grade rows must be reused, not recreated')
        self.assertTrue(before_sec_ids <= {s.id for s in self._sections(school.id)})

        sections = self._sections(school.id)
        self.assertEqual(len(sections), len(names))
        self.assertEqual({s.name for s in sections}, {DEFAULT_SECTION_NAME})

    def test_resaving_same_selection_creates_no_duplicates(self):
        _, school = self._create_school_via_route([STAGE_PRIMARY])
        before = (len(self._grades(school.id)), len(self._sections(school.id)),
                  len(self._subjects(school.id)))

        for _ in range(2):
            resp = self._edit(school, stages=[STAGE_PRIMARY])
            self.assertEqual(resp.status_code, 302)

        db.session.expire_all()
        after = (len(self._grades(school.id)), len(self._sections(school.id)),
                 len(self._subjects(school.id)))
        self.assertEqual(before, after, 're-saving must be a no-op')

    def test_removing_empty_stage_drops_only_its_structure(self):
        _, school = self._create_school_via_route(
            [STAGE_PRIMARY, STAGE_INTERMEDIATE])
        self.assertEqual(self._grade_names(school.id),
                         PRIMARY_NAMES | INTERMEDIATE_NAMES)

        resp = self._edit(school, stages=[STAGE_PRIMARY])
        self.assertEqual(resp.status_code, 302)

        db.session.expire_all()
        fresh = School.query.get(school.id)
        self.assertEqual(fresh.educational_stages, STAGE_PRIMARY)
        self.assertEqual(self._grade_names(school.id), PRIMARY_NAMES)
        self.assertEqual(len(self._sections(school.id)), len(PRIMARY_NAMES))
        grade_ids = {g.id for g in self._grades(school.id)}
        self.assertTrue(all(s.grade_id in grade_ids
                            for s in self._subjects(school.id)))

    def test_cannot_drop_to_zero_stages_once_managed(self):
        _, school = self._create_school_via_route([STAGE_PRIMARY])
        resp = self._edit(school, stages=None)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(ERR_NO_STAGE, resp.get_data(as_text=True))

        db.session.expire_all()
        fresh = School.query.get(school.id)
        self.assertEqual(fresh.educational_stages, STAGE_PRIMARY)
        self.assertEqual(self._grade_names(school.id), PRIMARY_NAMES)

    def test_removing_stage_with_students_is_rejected_and_rolls_back(self):
        _, school = self._create_school_via_route(
            [STAGE_PRIMARY, STAGE_INTERMEDIATE])
        year = AcademicYear.query.execution_options(bypass_tenant_scope=True)\
            .filter_by(school_id=school.id, is_current=True).first()
        inter_grade = next(g for g in self._grades(school.id)
                           if g.name in INTERMEDIATE_NAMES)
        section = next(s for s in self._sections(school.id)
                       if s.grade_id == inter_grade.id)
        student = Student(
            school_id=school.id, academic_year_id=year.id,
            section_id=section.id, full_name='طالب مرتبط',
            student_id=f'ST{self.sfx}', status='active')
        db.session.add(student)
        db.session.commit()

        before_grades = self._grade_names(school.id)
        before_sections = {s.id for s in self._sections(school.id)}
        before_subjects = {s.id for s in self._subjects(school.id)}
        original_name = school.school_name

        resp = self._edit(school, stages=[STAGE_PRIMARY],
                          school_name=f'{original_name}-should-not-persist')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(ERR_STAGE_LINKED, resp.get_data(as_text=True))

        db.session.expire_all()
        fresh = School.query.get(school.id)
        # configuration preserved
        self.assertEqual(fresh.educational_stages,
                         f'{STAGE_PRIMARY},{STAGE_INTERMEDIATE}')
        # no partial update: the unrelated name change was rolled back too
        self.assertEqual(fresh.school_name, original_name)
        # every record preserved
        self.assertEqual(self._grade_names(school.id), before_grades)
        self.assertEqual({s.id for s in self._sections(school.id)}, before_sections)
        self.assertEqual({s.id for s in self._subjects(school.id)}, before_subjects)
        self.assertIsNotNone(
            Student.query.execution_options(bypass_tenant_scope=True)
            .filter_by(id=student.id).first(), 'student data must never be deleted')

    def test_legacy_school_with_linked_data_cannot_silently_drop_stages(self):
        """A legacy school opting into managed mode is fail-closed: a stage its
        data already uses cannot be dropped by the selection."""
        school, year = self._make_legacy_school(
            grade_names=['الصف الأول الابتدائي', 'الصف الأول المتوسط'])
        inter_grade = next(g for g in self._grades(school.id)
                           if g.name == 'الصف الأول المتوسط')
        section = next(s for s in self._sections(school.id)
                       if s.grade_id == inter_grade.id)
        db.session.add(Student(
            school_id=school.id, academic_year_id=year.id,
            section_id=section.id, full_name='طالب قديم',
            student_id=f'SL{self.sfx}', status='active'))
        db.session.commit()

        resp = self._edit(school, stages=[STAGE_PRIMARY])
        self.assertEqual(resp.status_code, 200)
        self.assertIn(ERR_STAGE_LINKED, resp.get_data(as_text=True))

        db.session.expire_all()
        fresh = School.query.get(school.id)
        self.assertIsNone(fresh.educational_stages, 'stays legacy on rejection')
        self.assertEqual(self._grade_names(school.id),
                         {'الصف الأول الابتدائي', 'الصف الأول المتوسط'})


# ═════════════════════════════════════════════════════════════════════════════
#  4. EXTERNAL REGISTRATION
# ═════════════════════════════════════════════════════════════════════════════

class ExternalRegistrationStageTest(SchoolStagesTestBase):

    def _enable_link(self, school):
        raw = generate_token()
        school.external_registration_enabled = True
        school.registration_token_hash = hash_token(raw)
        school.registration_token_encrypted = encrypt_token(raw)
        db.session.commit()
        return raw

    def setUp(self):
        super().setUp()
        _, self.school_a = self._create_school_via_route([STAGE_PRIMARY])
        _, self.school_b = self._create_school_via_route([STAGE_INTERMEDIATE])
        self.token_a = self._enable_link(self.school_a)
        self.school_a_id = self.school_a.id

    def _submit(self, token, grade_id):
        return self._c().post(f'/register/{token}', data={
            'submission_nonce': _uid(),
            'desired_grade_id': str(grade_id),
            'full_name': 'الطالب التجريبي',
            'guardian_name': 'ولي الأمر',
            'guardian_phone': '07701234567',
        }, content_type='multipart/form-data')

    def _requests_for(self, school_id):
        return (StudentRegistrationRequest.query
                .execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=school_id).all())

    def test_form_shows_only_allowed_stage_grades(self):
        resp = self._c().get(f'/register/{self.token_a}')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        for name in PRIMARY_NAMES:
            self.assertIn(name, body)
        for name in INTERMEDIATE_NAMES | PREPARATORY_NAMES:
            self.assertNotIn(name, body)

    def test_allowed_grade_is_accepted(self):
        grade = next(g for g in self._grades(self.school_a_id)
                     if g.name in PRIMARY_NAMES)
        resp = self._submit(self.token_a, grade.id)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(self._requests_for(self.school_a_id)), 1)

    def test_out_of_stage_grade_is_rejected(self):
        """A grade of THIS school but outside its selected stages — injected
        manually — must not be accepted."""
        year = AcademicYear.query.execution_options(bypass_tenant_scope=True)\
            .filter_by(school_id=self.school_a_id, is_current=True).first()
        rogue = Grade(name='الصف الأول المتوسط', stage=STAGE_INTERMEDIATE,
                      school_id=self.school_a_id, academic_year_id=year.id)
        db.session.add(rogue)
        db.session.commit()

        resp = self._submit(self.token_a, rogue.id)
        self.assertEqual(resp.status_code, 200, 'form re-rendered with an error')
        self.assertEqual(self._requests_for(self.school_a_id), [])

    def test_cross_school_grade_is_rejected(self):
        foreign = next(g for g in self._grades(self.school_b.id)
                       if g.name in INTERMEDIATE_NAMES)
        resp = self._submit(self.token_a, foreign.id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._requests_for(self.school_a_id), [])
        self.assertEqual(self._requests_for(self.school_b.id), [])

    def test_section_id_is_still_a_forbidden_public_field(self):
        grade = next(g for g in self._grades(self.school_a_id)
                     if g.name in PRIMARY_NAMES)
        section = next(s for s in self._sections(self.school_a_id)
                       if s.grade_id == grade.id)
        resp = self._c().post(f'/register/{self.token_a}', data={
            'submission_nonce': _uid(),
            'desired_grade_id': str(grade.id),
            'section_id': str(section.id),
            'full_name': 'الطالب التجريبي',
            'guardian_name': 'ولي الأمر',
            'guardian_phone': '07701234567',
        }, content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._requests_for(self.school_a_id), [])


class AcademicYearAndRolloverTest(SchoolStagesTestBase):
    """A new year must stay EMPTY for every school so super_admin.rollover_year
    (which refuses to run once the target year has grades) keeps working."""

    def _create_year(self, school_id, name):
        return self._c().post(f'/schools/{school_id}/years/create', data={
            'name': name, 'start_date': '2026-08-01', 'end_date': '2027-06-30',
        }, follow_redirects=False)

    def _year_id(self, school_id, name):
        row = (AcademicYear.query.execution_options(bypass_tenant_scope=True)
               .filter_by(school_id=school_id, name=name).first())
        return row.id if row else None

    def test_managed_school_new_year_is_empty(self):
        _, school = self._create_school_via_route([STAGE_PRIMARY])
        sid, name = school.id, f'NY{self.sfx}'
        self.assertEqual(self._create_year(sid, name).status_code, 302)
        self._reload()
        yid = self._year_id(sid, name)
        self.assertIsNotNone(yid)
        self.assertEqual([g for g in self._grades(sid)
                          if g.academic_year_id == yid], [],
                         'create_year must not auto-provision any structure')

    def test_legacy_school_new_year_is_empty(self):
        school, _ = self._make_legacy_school()
        sid, name = school.id, f'NY{self.sfx}'
        self.assertEqual(self._create_year(sid, name).status_code, 302)
        self._reload()
        yid = self._year_id(sid, name)
        self.assertIsNotNone(yid)
        self.assertEqual([g for g in self._grades(sid)
                          if g.academic_year_id == yid], [])

    def test_new_empty_year_can_still_be_rolled_over(self):
        """The regression this revert exists for: rollover_year aborts when the
        target year already has grades."""
        _, school = self._create_school_via_route([STAGE_PRIMARY])
        sid, name = school.id, f'NY{self.sfx}'
        self.assertEqual(self._create_year(sid, name).status_code, 302)
        self._reload()
        yid = self._year_id(sid, name)

        resp = self._c().post(
            f'/admin/super/schools/{sid}/years/{yid}/rollover',
            follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self._reload()

        cloned = {g.name for g in self._grades(sid) if g.academic_year_id == yid}
        self.assertEqual(cloned, PRIMARY_NAMES,
                         'rollover must clone the previous year structure')
        cloned_sections = [s for s in self._sections(sid)
                           if s.academic_year_id == yid]
        self.assertEqual(len(cloned_sections), len(PRIMARY_NAMES))


class UnchangedSelectionIsInertTest(SchoolStagesTestBase):
    """An edit that does not change the stage selection must touch nothing."""

    def _manual_extras(self, school_id, year_id):
        """Rows an operator added by hand, including deliberately out-of-stage
        ones."""
        rogue_grade = Grade(name='الصف الأول المتوسط', stage=STAGE_INTERMEDIATE,
                            school_id=school_id, academic_year_id=year_id)
        db.session.add(rogue_grade)
        db.session.flush()
        rogue_section = Section(name='ب', grade_id=rogue_grade.id,
                                school_id=school_id, academic_year_id=year_id)
        rogue_subject = Subject(name='مادة يدوية', school_id=school_id,
                                academic_year_id=year_id, grade_id=rogue_grade.id)
        db.session.add_all([rogue_section, rogue_subject])
        db.session.flush()
        return rogue_grade, rogue_section, rogue_subject

    def test_unrelated_edit_preserves_manual_rows_and_deletions(self):
        _, school = self._create_school_via_route([STAGE_PRIMARY])
        sid = school.id
        year = (AcademicYear.query.execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=sid, is_current=True).first())

        rogue_grade, rogue_section, rogue_subject = self._manual_extras(sid, year.id)
        rogue_ids = (rogue_grade.id, rogue_section.id, rogue_subject.id)

        # Deliberately delete one provisioned default of a SELECTED stage.
        victim = next(g for g in self._grades(sid)
                      if g.name == 'الصف السادس الابتدائي')
        for row in [s for s in self._subjects(sid) if s.grade_id == victim.id]:
            db.session.delete(row)
        for row in [s for s in self._sections(sid) if s.grade_id == victim.id]:
            db.session.delete(row)
        db.session.delete(victim)
        db.session.commit()

        before_grades   = {g.id for g in self._grades(sid)}
        before_sections = {s.id for s in self._sections(sid)}
        before_subjects = {s.id for s in self._subjects(sid)}

        # Same stage selection, unrelated fields changed.
        resp = self._edit(school, stages=[STAGE_PRIMARY],
                          school_name=f'{school.school_name}-renamed',
                          phone='07701112233')
        self.assertEqual(resp.status_code, 302)
        self._reload()

        fresh = School.query.get(sid)
        self.assertTrue(fresh.school_name.endswith('-renamed'))
        self.assertEqual(fresh.educational_stages, STAGE_PRIMARY)

        self.assertEqual({g.id for g in self._grades(sid)}, before_grades,
                         'no grade created or deleted by an unchanged-stage edit')
        self.assertEqual({s.id for s in self._sections(sid)}, before_sections)
        self.assertEqual({s.id for s in self._subjects(sid)}, before_subjects)

        # Explicit: the manual out-of-stage rows survived ...
        self.assertIn(rogue_ids[0], before_grades)
        self.assertIn(rogue_ids[1], before_sections)
        self.assertIn(rogue_ids[2], before_subjects)
        # ... and the deliberately deleted default was NOT recreated.
        self.assertNotIn('الصف السادس الابتدائي',
                         {g.name for g in self._grades(sid)})

    def test_resaving_same_selection_creates_no_duplicates(self):
        _, school = self._create_school_via_route([STAGE_PRIMARY])
        sid = school.id
        before = (len(self._grades(sid)), len(self._sections(sid)),
                  len(self._subjects(sid)))
        for _ in range(2):
            self.assertEqual(self._edit(school, stages=[STAGE_PRIMARY]).status_code, 302)
        self._reload()
        after = (len(self._grades(sid)), len(self._sections(sid)),
                 len(self._subjects(sid)))
        self.assertEqual(before, after)


class RemovalGuardCoverageTest(SchoolStagesTestBase):
    """Every dependency shape must reject the WHOLE edit and roll it back."""

    def _managed_school(self, stages):
        _, school = self._create_school_via_route(stages)
        year = (AcademicYear.query.execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=school.id, is_current=True).first())
        return school, year

    def _assert_rejected_and_intact(self, school, stages, expected_msg=ERR_STAGE_LINKED):
        sid = school.id
        before_stages   = School.query.get(sid).educational_stages
        before_grades   = {g.id for g in self._grades(sid)}
        before_sections = {s.id for s in self._sections(sid)}
        before_subjects = {s.id for s in self._subjects(sid)}
        original_name   = school.school_name

        resp = self._edit(school, stages=stages,
                          school_name=f'{original_name}-should-not-persist',
                          phone='07709998877')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(expected_msg, resp.get_data(as_text=True))

        self._reload()
        db.session.expire_all()
        fresh = School.query.get(sid)
        self.assertEqual(fresh.educational_stages, before_stages)
        self.assertEqual(fresh.school_name, original_name,
                         'no partial update: the unrelated fields rolled back too')
        self.assertEqual({g.id for g in self._grades(sid)}, before_grades)
        self.assertEqual({s.id for s in self._sections(sid)}, before_sections)
        self.assertEqual({s.id for s in self._subjects(sid)}, before_subjects)

    def test_stage_scoped_chat_room_blocks_removal_with_no_grade_rows(self):
        """ChatRoom.stage is a bare string with no FK. Delete every grade of the
        stage first, so ONLY the stage string is left to block the removal."""
        school, year = self._managed_school([STAGE_PRIMARY, STAGE_INTERMEDIATE])
        sid = school.id

        inter_ids = [g.id for g in self._grades(sid)
                     if g.name in INTERMEDIATE_NAMES]
        for row in [s for s in self._subjects(sid) if s.grade_id in inter_ids]:
            db.session.delete(row)
        for row in [s for s in self._sections(sid) if s.grade_id in inter_ids]:
            db.session.delete(row)
        for row in [g for g in self._grades(sid) if g.id in inter_ids]:
            db.session.delete(row)
        db.session.flush()
        self.assertEqual([g for g in self._grades(sid)
                          if g.name in INTERMEDIATE_NAMES], [],
                         'precondition: no intermediate grade rows remain')

        db.session.add(ChatRoom(school_id=sid, academic_year_id=year.id,
                                name='غرفة المرحلة المتوسطة', type='group',
                                scope='stage', stage=STAGE_INTERMEDIATE))
        db.session.commit()

        self._assert_rejected_and_intact(school, [STAGE_PRIMARY])

    def test_stage_only_subject_with_homework_blocks_removal(self):
        """Subject with grade_id NULL, classified only by Subject.stage."""
        school, year = self._managed_school([STAGE_PRIMARY, STAGE_INTERMEDIATE])
        sid = school.id

        orphan = Subject(name='مادة بلا صف', school_id=sid,
                         academic_year_id=year.id, grade_id=None,
                         stage=STAGE_INTERMEDIATE)
        db.session.add(orphan)
        db.session.flush()
        db.session.add(Homework(
            school_id=sid, academic_year_id=year.id, subject_id=orphan.id,
            title='واجب مرتبط', publish_date=date(2025, 9, 1),
            due_date=date(2025, 9, 8)))
        db.session.commit()

        self._assert_rejected_and_intact(school, [STAGE_PRIMARY])

    def test_stage_only_subject_without_dependents_is_removed(self):
        """The same shape, but unused: it is a deletion candidate, proving the
        guard set and the deletion set classify subjects identically."""
        school, year = self._managed_school([STAGE_PRIMARY, STAGE_INTERMEDIATE])
        sid = school.id
        orphan = Subject(name='مادة بلا صف', school_id=sid,
                         academic_year_id=year.id, grade_id=None,
                         stage=STAGE_INTERMEDIATE)
        db.session.add(orphan)
        db.session.commit()
        orphan_id = orphan.id

        self.assertEqual(self._edit(school, stages=[STAGE_PRIMARY]).status_code, 302)
        self._reload()
        self.assertNotIn(orphan_id, {s.id for s in self._subjects(sid)})
        self.assertEqual(self._grade_names(sid), PRIMARY_NAMES)

    def test_ambiguous_grade_classification_is_rejected(self):
        """A grade whose name says one stage and whose stage column says another
        must never be silently reclassified or swept into a deletion."""
        school, year = self._managed_school([STAGE_PRIMARY, STAGE_INTERMEDIATE])
        sid = school.id
        clash = next(g for g in self._grades(sid)
                     if g.name == 'الصف الثالث الابتدائي')
        clash.stage = STAGE_INTERMEDIATE      # primary by name, intermediate by column
        db.session.commit()

        self._assert_rejected_and_intact(school, [STAGE_PRIMARY],
                                         expected_msg=ERR_AMBIGUOUS)

    def test_teacher_assignment_still_blocks_removal(self):
        from app.models import teacher_subjects, Employee
        school, year = self._managed_school([STAGE_PRIMARY, STAGE_INTERMEDIATE])
        sid = school.id
        emp = Employee(school_id=sid, employee_id=f'E{self.sfx}',
                       full_name='مدرس', job_title='مدرس')
        db.session.add(emp)
        db.session.flush()
        grade = next(g for g in self._grades(sid) if g.name in INTERMEDIATE_NAMES)
        section = next(s for s in self._sections(sid) if s.grade_id == grade.id)
        subject = next(s for s in self._subjects(sid) if s.grade_id == grade.id)
        db.session.execute(teacher_subjects.insert().values(
            employee_id=emp.id, subject_id=subject.id, section_id=section.id))
        db.session.commit()

        self._assert_rejected_and_intact(school, [STAGE_PRIMARY])


class HistoricalYearTest(SchoolStagesTestBase):
    """Dependencies are checked across ALL years; deletion touches the CURRENT
    year only, so historical structure survives."""

    def _with_history(self, stages):
        _, school = self._create_school_via_route(stages)
        sid = school.id
        current = (AcademicYear.query.execution_options(bypass_tenant_scope=True)
                   .filter_by(school_id=sid, is_current=True).first())
        old = AcademicYear(school_id=sid, name=f'OLD{self.sfx}',
                           start_date=date(2024, 8, 1), end_date=date(2025, 6, 30),
                           is_current=False)
        db.session.add(old)
        db.session.flush()
        hist_grade = Grade(name='الصف الأول المتوسط', stage=STAGE_INTERMEDIATE,
                           school_id=sid, academic_year_id=old.id)
        db.session.add(hist_grade)
        db.session.flush()
        hist_section = Section(name='أ', grade_id=hist_grade.id,
                               school_id=sid, academic_year_id=old.id)
        hist_subject = Subject(name='مادة قديمة', school_id=sid,
                               academic_year_id=old.id, grade_id=hist_grade.id)
        db.session.add_all([hist_section, hist_subject])
        db.session.flush()
        return school, current, old, hist_grade, hist_section, hist_subject

    def test_historical_dependency_blocks_removal(self):
        (school, current, old, hist_grade,
         hist_section, hist_subject) = self._with_history(
            [STAGE_PRIMARY, STAGE_INTERMEDIATE])
        sid = school.id
        db.session.add(Student(school_id=sid, academic_year_id=old.id,
                               section_id=hist_section.id,
                               full_name='طالب تاريخي',
                               student_id=f'HS{self.sfx}', status='active'))
        db.session.commit()

        before_grades = {g.id for g in self._grades(sid)}
        resp = self._edit(school, stages=[STAGE_PRIMARY])
        self.assertEqual(resp.status_code, 200)
        self.assertIn(ERR_STAGE_LINKED, resp.get_data(as_text=True))
        self._reload()
        db.session.expire_all()
        self.assertEqual(School.query.get(sid).educational_stages,
                         f'{STAGE_PRIMARY},{STAGE_INTERMEDIATE}')
        self.assertEqual({g.id for g in self._grades(sid)}, before_grades)

    def test_historical_structure_survives_a_clean_removal(self):
        (school, current, old, hist_grade,
         hist_section, hist_subject) = self._with_history(
            [STAGE_PRIMARY, STAGE_INTERMEDIATE])
        sid = school.id
        current_id = current.id
        db.session.commit()
        hist_ids = (hist_grade.id, hist_section.id, hist_subject.id)

        self.assertEqual(self._edit(school, stages=[STAGE_PRIMARY]).status_code, 302)
        self._reload()

        grade_ids   = {g.id for g in self._grades(sid)}
        section_ids = {s.id for s in self._sections(sid)}
        subject_ids = {s.id for s in self._subjects(sid)}
        self.assertIn(hist_ids[0], grade_ids, 'historical grade preserved')
        self.assertIn(hist_ids[1], section_ids, 'historical section preserved')
        self.assertIn(hist_ids[2], subject_ids, 'historical subject preserved')

        current_names = {g.name for g in self._grades(sid)
                         if g.academic_year_id == current_id}
        self.assertEqual(current_names, PRIMARY_NAMES,
                         'current-year intermediate structure removed')


class LegacyDetectionCoversNonGradeStagesTest(SchoolStagesTestBase):
    """A legacy school entering managed mode must not be able to drop a stage
    that its data represents WITHOUT any grade row."""

    def _legacy_with(self, build):
        school, year = self._make_legacy_school(
            grade_names=['\u0627\u0644\u0635\u0641 \u0627\u0644\u0623\u0648\u0644 \u0627\u0644\u0627\u0628\u062a\u062f\u0627\u0626\u064a'])
        build(school.id, year.id)
        db.session.commit()
        return school

    def test_stage_only_subject_blocks_entering_managed_mode(self):
        def build(sid, yid):
            db.session.add(Subject(name='\u0645\u0627\u062f\u0629 \u0628\u0644\u0627 \u0635\u0641', school_id=sid,
                                   academic_year_id=yid, grade_id=None,
                                   stage=STAGE_INTERMEDIATE))
            db.session.flush()
            sub = (Subject.query.execution_options(bypass_tenant_scope=True)
                   .filter_by(school_id=sid, grade_id=None).first())
            db.session.add(Homework(
                school_id=sid, academic_year_id=yid, subject_id=sub.id,
                title='\u0648\u0627\u062c\u0628', publish_date=date(2025, 9, 1),
                due_date=date(2025, 9, 8)))

        school = self._legacy_with(build)
        sid = school.id
        resp = self._edit(school, stages=[STAGE_PRIMARY])
        self.assertEqual(resp.status_code, 200)
        self.assertIn(ERR_STAGE_LINKED, resp.get_data(as_text=True))
        self._reload()
        db.session.expire_all()
        self.assertIsNone(School.query.get(sid).educational_stages,
                          'stays legacy on rejection')

    def test_stage_chat_room_blocks_entering_managed_mode(self):
        def build(sid, yid):
            db.session.add(ChatRoom(school_id=sid, academic_year_id=yid,
                                    name='\u063a\u0631\u0641\u0629 \u0627\u0644\u0645\u0631\u062d\u0644\u0629', type='group',
                                    scope='stage', stage=STAGE_INTERMEDIATE))

        school = self._legacy_with(build)
        sid = school.id
        resp = self._edit(school, stages=[STAGE_PRIMARY])
        self.assertEqual(resp.status_code, 200)
        self.assertIn(ERR_STAGE_LINKED, resp.get_data(as_text=True))
        self._reload()
        db.session.expire_all()
        self.assertIsNone(School.query.get(sid).educational_stages,
                          'stays legacy on rejection')


class ManualCrudUnrestrictedTest(SchoolStagesTestBase):
    """Stages govern AUTOMATIC defaults only. Manual CRUD keeps its freedom."""

    def test_manual_out_of_stage_grade_and_section_still_allowed(self):
        _, school = self._create_school_via_route([STAGE_PRIMARY])
        sid = school.id
        year = (AcademicYear.query.execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=sid, is_current=True).first())
        self._set_active_school(sid)

        resp = self._c().post('/sections/grades/create', data={
            'name': 'الصف الثالث المتوسط', 'stage': STAGE_INTERMEDIATE,
            'academic_year_id': str(year.id)}, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self._reload()
        created = next((g for g in self._grades(sid)
                        if g.name == 'الصف الثالث المتوسط'), None)
        self.assertIsNotNone(created, 'manual grade creation must stay available')

        resp = self._c().post(f'/sections/grades/{created.id}/sections/create',
                              data={'name': 'ج', 'capacity': '30'},
                              follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self._reload()
        self.assertIn('ج', {s.name for s in self._sections(sid)
                            if s.grade_id == created.id})


class AutomaticSetupIsStageAwareTest(SchoolStagesTestBase):
    """The existing setup buttons are AUTOMATIC defaults, so they follow the
    configured stages - and are unchanged for legacy schools."""

    def _setup_grades(self, school_id, year_id):
        self._set_active_school(school_id)
        return self._c().post('/sections/grades/setup-iraqi',
                              data={'academic_year_id': str(year_id)},
                              follow_redirects=False)

    def _setup_subjects(self, school_id):
        self._set_active_school(school_id)
        return self._c().post('/sections/subjects/setup-standard',
                              data={}, follow_redirects=False)

    def test_managed_school_setup_is_restricted_to_selected_stages(self):
        _, school = self._create_school_via_route([STAGE_PRIMARY])
        sid = school.id
        year = (AcademicYear.query.execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=sid, is_current=True).first())

        # Delete one default so the button has real work to do — otherwise the
        # assertion below would hold even if the request never ran.
        victim = next(g for g in self._grades(sid)
                      if g.name == 'الصف الخامس الابتدائي')
        for row in [x for x in self._subjects(sid) if x.grade_id == victim.id]:
            db.session.delete(row)
        for row in [x for x in self._sections(sid) if x.grade_id == victim.id]:
            db.session.delete(row)
        db.session.delete(victim)
        db.session.commit()
        self.assertNotIn('الصف الخامس الابتدائي', self._grade_names(sid))

        self.assertEqual(self._setup_grades(sid, year.id).status_code, 302)
        self._reload()
        self.assertIn('الصف الخامس الابتدائي', self._grade_names(sid),
                      'the setup button must actually have run')
        self.assertEqual(self._grade_names(sid), PRIMARY_NAMES,
                         'setup must not add unselected stages')

        self.assertEqual(self._setup_subjects(sid).status_code, 302)
        self._reload()
        grade_by_id = {g.id: g for g in self._grades(sid)}
        for sub in self._subjects(sid):
            self.assertIn(grade_by_id[sub.grade_id].name, PRIMARY_NAMES)

    def test_legacy_school_setup_is_unfiltered_exactly_as_before(self):
        school, year = self._make_legacy_school(grade_names=[])
        db.session.commit()
        sid = school.id

        self.assertEqual(self._setup_grades(sid, year.id).status_code, 302)
        self._reload()
        self.assertEqual(self._grade_names(sid),
                         {n for n, _ in IRAQI_STANDARD_GRADES},
                         'legacy schools keep the original 15-grade setup')

        self.assertEqual(self._setup_subjects(sid).status_code, 302)
        self._reload()
        expected = sum(len(v) for v in STANDARD_SUBJECTS_BY_GRADE.values())
        self.assertEqual(len(self._subjects(sid)), expected)
        self.assertIsNone(School.query.get(sid).educational_stages,
                          'setup must never opt a legacy school into managed mode')


class PreparatoryTracksTest(SchoolStagesTestBase):
    """Preparatory keeps all six canonical grades: scientific + literary, 4-6."""

    def test_six_preparatory_grades_subjects_and_external_filtering(self):
        _, school = self._create_school_via_route([STAGE_PREPARATORY])
        sid = school.id
        self.assertEqual(len(PREPARATORY_NAMES), 6)
        self.assertEqual(self._grade_names(sid), PREPARATORY_NAMES)
        for track in ('العلمي', 'الأدبي'):
            self.assertEqual(
                len([n for n in self._grade_names(sid) if track in n]), 3,
                f'three {track} years must exist')

        sections = self._sections(sid)
        self.assertEqual(len(sections), 6)
        self.assertEqual({s.name for s in sections}, {DEFAULT_SECTION_NAME})

        expected = sum(len(STANDARD_SUBJECTS_BY_GRADE[n]) for n in PREPARATORY_NAMES)
        self.assertEqual(len(self._subjects(sid)), expected)

        raw = generate_token()
        school.external_registration_enabled = True
        school.registration_token_hash = hash_token(raw)
        school.registration_token_encrypted = encrypt_token(raw)
        db.session.commit()
        body = self._c().get(f'/register/{raw}').get_data(as_text=True)
        for name in PREPARATORY_NAMES:
            self.assertIn(name, body)
        for name in PRIMARY_NAMES | INTERMEDIATE_NAMES:
            self.assertNotIn(name, body)


class InvalidStoredConfigurationTest(SchoolStagesTestBase):
    """A non-empty but unparseable stored value is never treated as legacy."""

    def _corrupt(self, school_id, value):
        db.session.execute(
            db.text('UPDATE schools SET educational_stages = :v WHERE id = :i'),
            {'v': value, 'i': school_id})
        db.session.commit()

    def test_helper_raises_instead_of_falling_back_to_legacy(self):
        _, school = self._create_school_via_route([STAGE_PRIMARY])
        sid = school.id
        self._corrupt(sid, 'روضة')
        db.session.expire_all()
        with self.assertRaises(InvalidStageConfiguration):
            school_stages(School.query.get(sid))

    def test_public_registration_fails_closed(self):
        _, school = self._create_school_via_route([STAGE_PRIMARY])
        sid = school.id
        raw = generate_token()
        school.external_registration_enabled = True
        school.registration_token_hash = hash_token(raw)
        school.registration_token_encrypted = encrypt_token(raw)
        db.session.commit()
        self.assertEqual(self._c().get(f'/register/{raw}').status_code, 200)

        self._corrupt(sid, 'روضة')
        resp = self._c().get(f'/register/{raw}')
        self.assertEqual(resp.status_code, 404,
                         'a corrupt config must not fall back to showing every grade')

    def test_repair_stores_selection_and_deletes_nothing(self):
        _, school = self._create_school_via_route([STAGE_PRIMARY, STAGE_INTERMEDIATE])
        sid = school.id
        self._corrupt(sid, 'روضة')
        db.session.expire_all()
        before_grades = {g.id for g in self._grades(sid)}

        fresh = School.query.get(sid)
        resp = self._edit(fresh, stages=[STAGE_PRIMARY])
        self.assertEqual(resp.status_code, 302)
        self._reload()
        db.session.expire_all()
        self.assertEqual(School.query.get(sid).educational_stages, STAGE_PRIMARY)
        self.assertEqual({g.id for g in self._grades(sid)}, before_grades,
                         'repairing a corrupt value must delete nothing')

    def test_setup_button_refuses_a_corrupt_configuration(self):
        """Invoke the route directly (same pattern as tests/test_school_manager_create.py)
        so the assertion is about the guard, not about test-client session juggling."""
        from flask import get_flashed_messages, session as fsession
        from flask_login import login_user, logout_user
        from app.blueprints.sections import setup_iraqi_grades

        _, school = self._create_school_via_route([STAGE_PRIMARY])
        sid = school.id
        year = (AcademicYear.query.execution_options(bypass_tenant_scope=True)
                .filter_by(school_id=sid, is_current=True).first())
        self._corrupt(sid, '\u0631\u0648\u0636\u0629')
        before = self._grade_names(sid)

        with self.app.test_request_context(
                '/sections/grades/setup-iraqi', method='POST',
                data={'academic_year_id': str(year.id)}):
            su = db.session.get(User, self.super_user_id,
                                execution_options={'bypass_tenant_scope': True})
            login_user(su)
            fsession['active_school_id'] = sid
            setup_iraqi_grades()
            flashes = get_flashed_messages()
            logout_user()

        self.assertIn(ERR_STORED_INVALID, flashes)
        self._reload()
        self.assertEqual(self._grade_names(sid), before,
                         'must not fall back to the unfiltered 15-grade setup')


class SeederBackwardCompatibilityTest(SchoolStagesTestBase):
    """The shared seeders keep their pre-feature behaviour when no filter is
    passed — the internal "تهيئة الصفوف العراقية" buttons must be unaffected."""

    def test_unfiltered_seeders_still_create_every_standard_grade(self):
        from app.utils.iraqi_grades import ensure_iraqi_standard_grades
        from app.utils.iraqi_subjects import ensure_standard_subjects

        school, year = self._make_legacy_school(grade_names=[])
        ensure_iraqi_standard_grades(school.id, year.id)
        db.session.flush()
        ensure_standard_subjects(school.id, year.id)
        db.session.commit()

        self.assertEqual(self._grade_names(school.id),
                         {n for n, _ in IRAQI_STANDARD_GRADES})
        expected = sum(len(v) for v in STANDARD_SUBJECTS_BY_GRADE.values())
        self.assertEqual(len(self._subjects(school.id)), expected)
        # No sections: the legacy seeders never created any.
        self.assertEqual(self._sections(school.id), [])


if __name__ == '__main__':
    unittest.main()

"""Focused checks for the per-school student archiving permission."""
import unittest
from datetime import date
from pathlib import Path
from uuid import uuid4

from app import create_app
from app.models import db, AcademicYear, AuditLog, Role, School, Student, User
from app.utils.school_cleanup import cleanup_school_cascade


OPTS = {'bypass_tenant_scope': True, 'include_all_years': True}


class StudentArchivingControlTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config.update(RATELIMIT_ENABLED=False, WTF_CSRF_ENABLED=False)
        with cls.app.app_context():
            cls.school_admin_role = Role.query.filter_by(name='school_admin').first().id
            cls.super_admin_role = Role.query.filter_by(name='super_admin').first().id

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        with self.app.app_context():
            self._add_school('a')
            self._add_school('b')
            super_user = User(
                username=f'archsup_{self.sfx}', email=f'archsup_{self.sfx}@test.local',
                full_name='Archive Super', role_id=self.super_admin_role,
                school_id=None, is_active=True,
            )
            super_user.set_password('Password123!')
            db.session.add(super_user)
            db.session.flush()
            self.ids['super'] = super_user.id
            db.session.commit()

    def _add_school(self, key):
        school = School(school_name=f'Archive {key} {self.sfx}',
                        code=f'AR{key}{self.sfx}'[:20], is_active=True)
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'AY-{key}-{self.sfx}',
                            start_date=date(2026, 9, 1), end_date=date(2027, 6, 30),
                            is_current=True)
        db.session.add(year)
        admin = User(username=f'arch{key}_{self.sfx}',
                     email=f'arch{key}_{self.sfx}@test.local',
                     full_name=f'Archive Admin {key}', role_id=self.school_admin_role,
                     school_id=school.id, is_active=True)
        admin.set_password('Password123!')
        db.session.add(admin)
        db.session.flush()
        student = Student(student_id=f'ST-{key}-{self.sfx}', full_name=f'Student {key}',
                          school_id=school.id, academic_year_id=year.id, status='active')
        db.session.add(student)
        db.session.flush()
        self.ids.update({f'school_{key}': school.id, f'admin_{key}': admin.id,
                         f'student_{key}': student.id})

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            AuditLog.query.execution_options(**OPTS).filter(
                AuditLog.user_id == self.ids['super']).delete(synchronize_session=False)
            for key in ('a', 'b'):
                cleanup_school_cascade(self.ids[f'school_{key}'])
            User.query.execution_options(**OPTS).filter_by(
                id=self.ids['super']).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    def _login(self, user_id, school_id=None):
        with self.client.session_transaction() as session:
            session['_user_id'] = str(user_id)
            session['_fresh'] = True
            if school_id is not None:
                session['active_school_id'] = school_id

    def _student_status(self, key='a'):
        with self.app.app_context():
            student = Student.query.execution_options(**OPTS).filter_by(
                id=self.ids[f'student_{key}']).one()
            return student.status, student.full_name

    def test_enabled_school_can_archive_student(self):
        self.client = self.app.test_client()
        self._login(self.ids['admin_a'], self.ids['school_a'])
        response = self.client.post(
            f'/students/{self.ids["student_a"]}/archive',
            data={'status': 'archived'}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._student_status()[0], 'archived')

    def test_disabled_school_blocks_direct_and_edit_without_partial_save(self):
        with self.app.app_context():
            school = db.session.get(School, self.ids['school_a'])
            school.archiving_enabled = False
            db.session.commit()
        self.client = self.app.test_client()
        self._login(self.ids['admin_a'], self.ids['school_a'])

        self.client.post(f'/students/{self.ids["student_a"]}/archive',
                         data={'status': 'archived'})
        self.assertEqual(self._student_status()[0], 'active')

        self.client.post(f'/students/{self.ids["student_a"]}/edit',
                         data={'status': 'archived', 'full_name': 'Must Not Save'})
        self.assertEqual(self._student_status(), ('active', 'Student a'))

    def test_restoration_remains_allowed_when_disabled(self):
        with self.app.app_context():
            school = db.session.get(School, self.ids['school_a'])
            school.archiving_enabled = False
            student = Student.query.execution_options(**OPTS).filter_by(
                id=self.ids['student_a']).one()
            student.status = 'archived'
            db.session.commit()
        self.client = self.app.test_client()
        self._login(self.ids['admin_a'], self.ids['school_a'])
        self.client.post(f'/students/{self.ids["student_a"]}/archive',
                         data={'status': 'active'})
        self.assertEqual(self._student_status()[0], 'active')

    def test_only_super_admin_updates_one_school_and_defaults_are_enabled(self):
        self.client = self.app.test_client()
        self._login(self.ids['admin_a'], self.ids['school_a'])
        self.client.post(
            f'/admin/super/schools/{self.ids["school_a"]}/student-archiving',
            data={'action': 'disable'})
        with self.app.app_context():
            self.assertTrue(db.session.get(School, self.ids['school_a']).archiving_enabled)

        self.client = self.app.test_client()
        self._login(self.ids['super'])
        self.client.post(
            f'/admin/super/schools/{self.ids["school_a"]}/student-archiving',
            data={'action': 'disable'})
        with self.app.app_context():
            self.assertFalse(db.session.get(School, self.ids['school_a']).archiving_enabled)
            self.assertTrue(db.session.get(School, self.ids['school_b']).archiving_enabled)

        self.client.post(
            f'/admin/super/schools/{self.ids["school_a"]}/student-archiving',
            data={'action': 'enable'})
        with self.app.app_context():
            self.assertTrue(db.session.get(School, self.ids['school_a']).archiving_enabled)

        migration = Path('migrations/versions/w4x6y8z0a2b3_add_school_student_archiving_control.py').read_text()
        self.assertIn('nullable=False', migration)
        self.assertIn('server_default=sa.true()', migration)


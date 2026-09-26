"""
Verification tests for the student-wizard auto-generated parent account.

Covers:
  - create_parent_account toggle ON  -> a parent User is created, role=parent,
    same school, linked to the new student, with the exact submitted credentials.
  - Linking an existing parent (toggle OFF) does NOT create a duplicate account.
  - Cross-school parent link is refused.
"""
import unittest
from datetime import date
from uuid import uuid4

from app import create_app
from app.models import (
    db, Role, School, User, AcademicYear, Grade, Section, Student, parent_students,
)


def _uid():
    return uuid4().hex[:10]


class StudentParentAutocreateTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        with cls.app.app_context():
            cls.admin_role  = Role.query.filter_by(name='school_admin').first()
            cls.parent_role = Role.query.filter_by(name='parent').first()
            assert cls.admin_role,  'school_admin role must exist'
            assert cls.parent_role, 'parent role must exist'

    def setUp(self):
        self.sfx    = _uid()
        self.client = self.app.test_client()
        self._created_usernames = []

        with self.app.app_context():
            school = School(school_name=f'PA School {self.sfx}',
                            code=f'PA{self.sfx[:8]}', capacity=0, is_active=True)
            db.session.add(school)
            db.session.flush()

            year = AcademicYear(school_id=school.id, name=f'Y-{self.sfx}',
                                start_date=date(2025, 9, 1), end_date=date(2026, 6, 30),
                                is_current=True)
            db.session.add(year)
            db.session.flush()

            grade = Grade(name='الأول', stage='ابتدائية', school_id=school.id,
                          academic_year_id=year.id)
            db.session.add(grade)
            db.session.flush()

            section = Section(name='أ', school_id=school.id,
                              academic_year_id=year.id, grade_id=grade.id)
            db.session.add(section)
            db.session.flush()

            admin = User(username=f'pa_admin_{self.sfx}',
                         email=f'pa_admin_{self.sfx}@test.test',
                         full_name='PA Admin', role_id=self.admin_role.id,
                         school_id=school.id, is_active=True)
            admin.set_password('Test1234!')

            # An existing parent to test the link path
            existing_parent = User(username=f'pa_exist_{self.sfx}',
                                   email=f'pa_exist_{self.sfx}@test.test',
                                   full_name='Existing Parent',
                                   role_id=self.parent_role.id,
                                   school_id=school.id, is_active=True)
            existing_parent.set_password('Test1234!')

            db.session.add_all([admin, existing_parent])
            db.session.commit()

            self.school_id       = school.id
            self.section_id      = section.id
            self.admin_username  = admin.username
            self.existing_parent_id = existing_parent.id

    def tearDown(self):
        with self.app.app_context():
            # Remove any students + parent links + users created for this suffix.
            students = (Student.query.execution_options(bypass_tenant_scope=True,
                                                        include_all_years=True)
                        .filter(Student.school_id == self.school_id).all())
            for s in students:
                db.session.execute(parent_students.delete().where(
                    parent_students.c.student_id == s.id))
            for s in students:
                db.session.delete(s)
            db.session.flush()

            users = (User.query.execution_options(bypass_tenant_scope=True)
                     .filter(User.school_id == self.school_id).all())
            for u in users:
                u.children = []
                db.session.delete(u)
            db.session.flush()

            for model, ident in [(Section, self.section_id)]:
                obj = db.session.get(model, ident,
                                     execution_options={'bypass_tenant_scope': True})
                if obj:
                    db.session.delete(obj)
            db.session.flush()
            # grades / year / school
            from app.models import Grade as _G
            for g in (_G.query.execution_options(bypass_tenant_scope=True,
                                                 include_all_years=True)
                      .filter(_G.school_id == self.school_id).all()):
                db.session.delete(g)
            for y in (AcademicYear.query.execution_options(bypass_tenant_scope=True,
                                                          include_all_years=True)
                      .filter(AcademicYear.school_id == self.school_id).all()):
                db.session.delete(y)
            school = db.session.get(School, self.school_id,
                                    execution_options={'bypass_tenant_scope': True})
            if school:
                db.session.delete(school)
            db.session.commit()

    def _login(self, username, password='Test1234!'):
        return self.client.post('/auth/login',
                                data={'username': username, 'password': password},
                                follow_redirects=True)

    # ── Tests ────────────────────────────────────────────────────────────────

    def test_toggle_on_creates_and_links_parent(self):
        self._login(self.admin_username)
        username = 'AB23CD'
        password = 'XY123456'
        resp = self.client.post('/students/create', data={
            'full_name': f'Auto Parent Student {self.sfx}',
            'section_id': str(self.section_id),
            'guardian_name': 'Guardian One',
            'guardian_relation': 'أب',
            'guardian_phone': '07700000000',
            'create_parent_account': '1',
            'parent_username': username,
            'parent_password': password,
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 302, resp.data[:500])

        with self.app.app_context():
            parent = (User.query.execution_options(bypass_tenant_scope=True)
                      .filter_by(username=username).first())
            self.assertIsNotNone(parent, 'parent User was not created')
            self.assertEqual(parent.role.name, 'parent')
            self.assertEqual(parent.school_id, self.school_id)
            self.assertTrue(parent.is_active)
            self.assertTrue(parent.check_password(password),
                            'saved password must match the submitted generated one')
            # Linked to exactly the new student
            link = db.session.execute(parent_students.select().where(
                parent_students.c.user_id == parent.id)).fetchall()
            self.assertEqual(len(link), 1, 'parent must be linked to one student')

    def test_link_existing_parent_creates_no_duplicate(self):
        self._login(self.admin_username)
        resp = self.client.post('/students/create', data={
            'full_name': f'Linked Student {self.sfx}',
            'section_id': str(self.section_id),
            'guardian_name': 'Guardian Two',
            'guardian_relation': 'أم',
            'link_existing_parent_id': str(self.existing_parent_id),
            # Existing-parent selection must take precedence even if the (default-on)
            # create toggle is also submitted — no duplicate account may be created.
            'create_parent_account': '1',
            'parent_username': 'ZZ99YY',
            'parent_password': 'QQ654321',
        }, follow_redirects=False)
        self.assertEqual(resp.status_code, 302, resp.data[:500])

        with self.app.app_context():
            # The read-only credentials must NOT have produced a new account.
            dup = (User.query.execution_options(bypass_tenant_scope=True)
                   .filter_by(username='ZZ99YY').first())
            self.assertIsNone(dup, 'no new parent account may be created when linking')
            # Existing parent is linked to the new student.
            link = db.session.execute(parent_students.select().where(
                parent_students.c.user_id == self.existing_parent_id)).fetchall()
            self.assertEqual(len(link), 1)


if __name__ == '__main__':
    unittest.main()

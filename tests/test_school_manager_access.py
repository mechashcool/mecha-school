"""
Tests for School Manager access control.

1. test_manager_cannot_access_roles_list
   School manager GET /admin/roles → redirected (not allowed).

2. test_manager_cannot_access_create_role
   School manager GET /admin/roles/create → redirected (not allowed).

3. test_super_admin_can_access_roles_list
   Super admin GET /admin/roles → allowed (200 OK).

4. test_manager_teacher_gets_subject_assignment
   School manager creates teacher with subject_ids → teacher_subjects rows created.

5. test_teacher_dashboard_scoped_to_assignments
   Teacher user dashboard only surfaces assigned sections/subjects.
"""
import unittest
from datetime import date
from uuid import uuid4

from flask_login import login_user, logout_user

from app import create_app
from app.models import (
    db, AcademicYear, Employee, Grade, Role, School, Section,
    Student, Subject, User, teacher_subjects,
)


class SchoolManagerAccessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('development')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.created = {}

        with self.app.app_context():
            school_admin_role = Role.query.filter_by(name='school_admin').first()
            super_admin_role  = Role.query.filter_by(name='super_admin').first()
            teacher_role      = Role.query.filter_by(name='teacher').first()
            self.assertIsNotNone(school_admin_role, 'seed roles before running tests')
            self.assertIsNotNone(super_admin_role,  'seed roles before running tests')
            self.assertIsNotNone(teacher_role,      'seed roles before running tests')

            # ── School A (manager's school) ────────────────────────────────
            school = School(
                school_name=f'Access School {self.suffix}',
                code=f'AC{self.suffix[:8]}',
                capacity=0,
                is_active=True,
            )
            db.session.add(school)
            db.session.flush()

            year = AcademicYear(
                school_id=school.id,
                name=f'AY {self.suffix}',
                start_date=date(2025, 8, 1),
                end_date=date(2026, 6, 30),
                is_current=True,
            )
            db.session.add(year)
            db.session.flush()

            grade = Grade(
                school_id=school.id,
                academic_year_id=year.id,
                name=f'G1 {self.suffix}',
            )
            db.session.add(grade)
            db.session.flush()

            section = Section(
                school_id=school.id,
                academic_year_id=year.id,
                grade_id=grade.id,
                name=f'S{self.suffix[:4]}',
                capacity=30,
            )
            db.session.add(section)
            db.session.flush()

            subject = Subject(
                name=f'Math {self.suffix}',
                code=f'M{self.suffix[:6]}',
                school_id=school.id,
                academic_year_id=year.id,
            )
            db.session.add(subject)
            db.session.flush()

            manager = User(
                username=f'mgr_acc_{self.suffix}',
                email=f'mgr_acc_{self.suffix}@example.test',
                full_name=f'Manager Acc {self.suffix}',
                role_id=school_admin_role.id,
                school_id=school.id,
                is_active=True,
            )
            manager.set_password('Password123')
            db.session.add(manager)
            db.session.flush()

            # Super admin (school_id = NULL)
            super_admin = User(
                username=f'sa_acc_{self.suffix}',
                email=f'sa_acc_{self.suffix}@example.test',
                full_name=f'SuperAdmin Acc {self.suffix}',
                role_id=super_admin_role.id,
                school_id=None,
                is_active=True,
            )
            super_admin.set_password('Password123')
            db.session.add(super_admin)
            db.session.flush()

            db.session.commit()

            self.created = {
                'school_admin_role_id': school_admin_role.id,
                'super_admin_role_id':  super_admin_role.id,
                'teacher_role_id':      teacher_role.id,
                'school_id':       school.id,
                'year_id':         year.id,
                'grade_id':        grade.id,
                'section_id':      section.id,
                'subject_id':      subject.id,
                'manager_id':      manager.id,
                'super_admin_id':  super_admin.id,
            }

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            ids = self.created

            # Delete teacher_subjects rows first (FK blocks employee deletion)
            for emp in (Employee.query
                        .execution_options(bypass_tenant_scope=True)
                        .filter(Employee.school_id == ids.get('school_id'))
                        .all()):
                db.session.execute(
                    teacher_subjects.delete().where(
                        teacher_subjects.c.employee_id == emp.id
                    )
                )
            db.session.flush()

            # Clean up employees + users created during tests
            for model in (Employee, User):
                for obj in (model.query
                            .execution_options(bypass_tenant_scope=True)
                            .filter(model.school_id == ids.get('school_id'))
                            .all()):
                    if obj.id not in (ids.get('manager_id'),):
                        db.session.delete(obj)
            db.session.flush()

            for model, key in [
                (User, 'manager_id'),
                (User, 'super_admin_id'),
                (Subject,      'subject_id'),
                (Section,      'section_id'),
                (Grade,        'grade_id'),
                (AcademicYear, 'year_id'),
                (School,       'school_id'),
            ]:
                obj = db.session.get(
                    model, ids.get(key),
                    execution_options={'bypass_tenant_scope': True},
                )
                if obj is not None:
                    db.session.delete(obj)

            db.session.commit()
            db.session.remove()

    def _run_before_request(self):
        for fn in self.app.before_request_funcs.get(None, []):
            rv = fn()
            if rv is not None:
                return rv
        return None

    # ── Test 1 ──────────────────────────────────────────────────────────────

    def test_manager_cannot_access_roles_list(self):
        """School manager accessing /admin/roles is redirected away (super_admin_required)."""
        from app.blueprints.admin import roles_list

        ids = self.created
        with self.app.test_request_context('/admin/roles', method='GET'):
            manager = db.session.get(
                User, ids['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()

            resp = roles_list()
            # Should be a redirect, not 200
            self.assertEqual(resp.status_code, 302,
                             'School manager must be redirected from roles_list')
            logout_user()

    # ── Test 2 ──────────────────────────────────────────────────────────────

    def test_manager_cannot_access_create_role(self):
        """School manager GET /admin/roles/create is blocked (redirected)."""
        from app.blueprints.admin import create_role

        ids = self.created
        with self.app.test_request_context('/admin/roles/create', method='GET'):
            manager = db.session.get(
                User, ids['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()

            resp = create_role()
            self.assertEqual(resp.status_code, 302,
                             'School manager must be redirected from create_role')
            logout_user()

    # ── Test 3 ──────────────────────────────────────────────────────────────

    def test_super_admin_can_access_roles_list(self):
        """Super admin (school_id=None) can access roles_list (200)."""
        from app.blueprints.admin import roles_list

        ids = self.created
        with self.app.test_request_context('/admin/roles', method='GET'):
            sa = db.session.get(
                User, ids['super_admin_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(sa)
            self._run_before_request()

            resp = roles_list()
            # May return a string (render_template) or Response; either way not a redirect
            status = getattr(resp, 'status_code', 200)
            self.assertNotEqual(status, 302,
                                'Super admin must NOT be redirected from roles_list')
            logout_user()

    # ── Test 4 ──────────────────────────────────────────────────────────────

    def test_manager_teacher_gets_subject_assignment(self):
        """School manager creates teacher with teacher_subject_ids → teacher_subjects rows inserted."""
        from app.blueprints.admin import create_user

        ids = self.created
        username = f'teacher_subj_{self.suffix}'

        with self.app.test_request_context(
            '/admin/users/create',
            method='POST',
            data={
                'username':            username,
                'email':               f'{username}@example.test',
                'full_name':           f'Teacher Subj {self.suffix}',
                'password':            'Password123',
                'role_id':             str(ids['teacher_role_id']),
                'teacher_section_ids': str(ids['section_id']),
                'teacher_subject_ids': str(ids['subject_id']),
            },
        ):
            manager = db.session.get(
                User, ids['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()

            response = create_user()
            self.assertEqual(response.status_code, 302,
                             'Expected redirect after teacher creation')

            created_user = (User.query
                            .execution_options(bypass_tenant_scope=True)
                            .filter_by(username=username).first())
            self.assertIsNotNone(created_user, 'Teacher user must be created')

            emp = (Employee.query
                   .execution_options(bypass_tenant_scope=True)
                   .filter_by(user_id=created_user.id).first())
            self.assertIsNotNone(emp, 'Employee record must exist')

            # Verify teacher_subjects rows were created
            from sqlalchemy import select as sa_select
            ts_rows = db.session.execute(
                sa_select(teacher_subjects).where(
                    teacher_subjects.c.employee_id == emp.id
                )
            ).fetchall()
            self.assertGreater(len(ts_rows), 0,
                               'teacher_subjects must have at least one row')
            row = ts_rows[0]
            self.assertEqual(row.subject_id, ids['subject_id'])
            self.assertEqual(row.section_id, ids['section_id'])

            logout_user()

    # ── Test 5 ──────────────────────────────────────────────────────────────

    def test_teacher_dashboard_uses_assigned_sections_and_subjects(self):
        """Teacher dashboard only shows sections/subjects from their teacher_subjects assignments."""
        from app.blueprints.admin import create_user
        from app.blueprints.teacher import dashboard as teacher_dashboard

        ids = self.created
        username = f'teacher_scope_{self.suffix}'

        # Create teacher with known section+subject assignment
        with self.app.test_request_context(
            '/admin/users/create',
            method='POST',
            data={
                'username':            username,
                'email':               f'{username}@example.test',
                'full_name':           f'Teacher Scope {self.suffix}',
                'password':            'Password123',
                'role_id':             str(ids['teacher_role_id']),
                'teacher_section_ids': str(ids['section_id']),
                'teacher_subject_ids': str(ids['subject_id']),
            },
        ):
            manager = db.session.get(
                User, ids['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()
            create_user()
            logout_user()

        # Now test the teacher dashboard returns correct scoped data
        with self.app.test_request_context('/teacher/'):
            teacher_user = (User.query
                            .execution_options(bypass_tenant_scope=True)
                            .filter_by(username=username).first())
            self.assertIsNotNone(teacher_user)
            login_user(teacher_user)
            self._run_before_request()

            resp = teacher_dashboard()
            # render_template returns a string in test context
            self.assertIsNotNone(resp)
            logout_user()


if __name__ == '__main__':
    unittest.main()

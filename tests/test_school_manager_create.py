"""
Tests for school-manager linkage and cross-school isolation.

Workflow (after Phase 6 refactor):
  1. School is created via the schools blueprint (school-only form).
  2. Manager user is created via the admin user-create route with school_id set.
  3. Tenant isolation is enforced at the ORM level via school_id scoping.

These tests verify:
  - A user with school_id is bound to that school and is NOT a super_admin.
  - Extra permissions assigned to the manager are active.
  - The manager cannot read data from a different school.
"""
import unittest
from datetime import date
from uuid import uuid4

from flask_login import login_user, logout_user

from app import create_app
from app.models import (
    db, AcademicYear, Employee, Permission, Role, School, User,
)


class SchoolManagerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('development')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.created = {}

        with self.app.app_context():
            school_admin_role = Role.query.filter_by(name='school_admin').first()
            super_admin_role  = Role.query.filter_by(name='super_admin').first()
            self.assertIsNotNone(school_admin_role, 'seed roles before running manager tests')
            self.assertIsNotNone(super_admin_role,  'seed roles before running manager tests')
            extra_perm = Permission.query.filter_by(name='manage_salaries').first()
            self.assertIsNotNone(extra_perm, 'seed permissions before running manager tests')

            # School the manager belongs to
            manager_school = School(
                school_name=f'Manager School {self.suffix}',
                code=f'MS{self.suffix[:8]}',
                capacity=0,
                is_active=True,
            )
            db.session.add(manager_school)
            db.session.flush()

            manager_year = AcademicYear(
                school_id=manager_school.id,
                name=f'Year {self.suffix}',
                start_date=date(2025, 8, 1),
                end_date=date(2026, 6, 30),
                is_current=True,
            )
            db.session.add(manager_year)
            db.session.flush()

            # Manager user — linked to manager_school with extra permissions
            manager = User(
                username=f'manager_{self.suffix}',
                email=f'manager_{self.suffix}@example.test',
                full_name='Test Manager',
                role_id=school_admin_role.id,
                school_id=manager_school.id,
                is_active=True,
            )
            manager.set_password('Password123')
            db.session.add(manager)
            db.session.flush()
            manager.extra_permissions = [extra_perm]

            super_admin = User(
                username=f'super_{self.suffix}',
                email=f'super_{self.suffix}@example.test',
                full_name='Test Super Admin',
                role_id=super_admin_role.id,
                school_id=None,
                is_active=True,
            )
            super_admin.set_password('Password123')
            db.session.add(super_admin)
            db.session.flush()

            # A separate school with an employee (for cross-school isolation test)
            other_school = School(
                school_name=f'Other School {self.suffix}',
                code=f'OS{self.suffix[:8]}',
                capacity=0,
                is_active=True,
            )
            db.session.add(other_school)
            db.session.flush()

            other_employee = Employee(
                employee_id=f'OE{self.suffix[:8]}',
                full_name=f'Other Employee {self.suffix}',
                job_title='Teacher',
                school_id=other_school.id,
                base_salary=0,
                status='active',
            )
            db.session.add(other_employee)
            db.session.commit()

            self.created = {
                'school_admin_role_id': school_admin_role.id,
                'super_admin_role_id':  super_admin_role.id,
                'extra_permission_id':  extra_perm.id,
                'manager_school_id':   manager_school.id,
                'manager_year_id':     manager_year.id,
                'manager_id':          manager.id,
                'super_admin_id':      super_admin.id,
                'other_school_id':     other_school.id,
                'other_employee_id':   other_employee.id,
            }

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            ids = self.created

            for user in (User.query
                         .execution_options(bypass_tenant_scope=True)
                         .filter(User.email.like(f'%{self.suffix}@example.test'))
                         .all()):
                user.children = []
                db.session.delete(user)
            db.session.flush()

            for model, key in [
                (Employee, 'other_employee_id'),
                (User,     'manager_id'),
                (User,     'super_admin_id'),
                (AcademicYear, 'manager_year_id'),
                (School,   'manager_school_id'),
                (School,   'other_school_id'),
                (School,   'school_only_id'),
            ]:
                ident = ids.get(key)
                if ident is None:
                    continue
                obj = db.session.get(
                    model, ident,
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

    def test_manager_linked_to_school_has_correct_school_id(self):
        """User created with school_id is bound to that school, is NOT super_admin."""
        with self.app.app_context():
            manager = db.session.get(
                User, self.created['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            self.assertEqual(manager.school_id, self.created['manager_school_id'])
            self.assertFalse(manager.is_super_admin)
            self.assertTrue(manager.check_password('Password123'))
            self.assertIn(
                self.created['extra_permission_id'],
                {p.id for p in manager.extra_permissions},
            )

    def test_manager_cannot_see_other_school_data(self):
        """School manager cannot read employees belonging to a different school."""
        with self.app.test_request_context('/'):
            manager = db.session.get(
                User, self.created['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()

            visible = Employee.query.filter_by(
                id=self.created['other_employee_id']
            ).first()
            self.assertIsNone(visible)
            logout_user()

    def test_manager_permissions_apply_within_school(self):
        """Extra permissions granted to a manager are active (has_permission returns True)."""
        with self.app.app_context():
            manager = db.session.get(
                User, self.created['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            self.assertTrue(manager.has_permission('manage_salaries'))
            self.assertFalse(manager.is_super_admin)

    def test_school_create_form_does_not_create_manager_user(self):
        """School creation remains school-only, even if old manager fields are posted."""
        from app.blueprints.schools import create as create_school

        username = f'old_flow_manager_{self.suffix}'
        code = f'NF{self.suffix[:8]}'

        with self.app.test_request_context(
            '/schools/create',
            method='POST',
            data={
                'school_name': f'New Flow School {self.suffix}',
                'code': code,
                'capacity': '0',
                'manager_username': username,
                'manager_email': f'{username}@example.test',
                'manager_password': 'Password123',
            },
        ):
            super_admin = db.session.get(
                User, self.created['super_admin_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(super_admin)
            self._run_before_request()

            response = create_school()
            self.assertEqual(response.status_code, 302)
            logout_user()

        with self.app.app_context():
            school = School.query.filter_by(code=code).first()
            manager = (User.query
                       .execution_options(bypass_tenant_scope=True)
                       .filter_by(username=username).first())
            self.assertIsNotNone(school)
            self.assertIsNone(manager)
            if school:
                self.created['school_only_id'] = school.id

    def test_super_admin_creates_linked_manager_from_users_page(self):
        """Super admin POST /admin/users/create links an admin user to the selected school."""
        from app.blueprints.admin import create_user

        username = f'linked_manager_{self.suffix}'
        ids = self.created

        with self.app.test_request_context(
            '/admin/users/create',
            method='POST',
            data={
                'username': username,
                'email': f'{username}@example.test',
                'full_name': 'Linked Manager',
                'password': 'Password123',
                'role_id': str(ids['school_admin_role_id']),
                'school_id': str(ids['manager_school_id']),
                'permissions': [str(ids['extra_permission_id'])],
            },
        ):
            super_admin = db.session.get(
                User, ids['super_admin_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(super_admin)
            self._run_before_request()

            response = create_user()
            self.assertEqual(response.status_code, 302)
            logout_user()

        with self.app.app_context():
            manager = (User.query
                       .execution_options(bypass_tenant_scope=True)
                       .filter_by(username=username).first())
            self.assertIsNotNone(manager)
            self.assertEqual(manager.school_id, ids['manager_school_id'])
            self.assertFalse(manager.is_super_admin)
            self.assertIn(
                ids['extra_permission_id'],
                {p.id for p in manager.extra_permissions},
            )

    def test_school_detail_detects_linked_manager(self):
        """School detail page shows the manager by user.school_id + admin/manager role."""
        from app.blueprints.schools import detail as school_detail

        with self.app.test_request_context(
            f'/schools/{self.created["manager_school_id"]}',
            method='GET',
        ):
            super_admin = db.session.get(
                User, self.created['super_admin_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(super_admin)
            self._run_before_request()

            response = school_detail(self.created['manager_school_id'])
            self.assertIn('Test Manager', response)
            logout_user()


if __name__ == '__main__':
    unittest.main()

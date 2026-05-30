"""
Tests for school-manager scoped user management.

1. test_manager_can_create_teacher_in_own_school
   School manager creates a User with the teacher role → user gets school_id
   matching the manager's school and an Employee record is auto-created.

2. test_manager_cannot_create_admin_role_user
   School manager POSTs a user creation request with an admin role →
   returns 302 redirect (flash error), NOT a new user.

3. test_manager_cannot_see_other_school_users
   School manager cannot list or fetch users belonging to a different school.

4. test_manager_cannot_edit_other_school_user
   School manager calling edit_user() on a user from a different school → 403.

5. test_manager_can_create_parent_linked_to_own_school_student
   School manager creates a parent user linked to a student from their school
   → parent.children contains that student.

6. test_manager_cannot_link_parent_to_other_school_student
   Student IDs from a different school are silently excluded when creating a
   parent user via school manager.

7. test_teacher_gets_section_assignment
   When school manager creates a teacher and provides teacher_section_ids,
   the linked Employee record's homeroom sections are set correctly.
"""
import unittest
from datetime import date
from uuid import uuid4

from flask_login import login_user, logout_user

from app import create_app
from app.models import (
    db, AcademicYear, Employee, Grade, Permission, Role, School, Section,
    Student, Subject, User, teacher_subjects,
)


class SchoolManagerUsersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('development')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.created = {}

        with self.app.app_context():
            school_admin_role = Role.query.filter_by(name='school_admin').first()
            super_admin_role  = Role.query.filter_by(name='super_admin').first()
            self.assertIsNotNone(school_admin_role, 'seed roles before running tests')
            self.assertIsNotNone(super_admin_role,  'seed roles before running tests')
            teacher_role = Role.query.filter_by(name='teacher').first()
            self.assertIsNotNone(teacher_role, 'seed roles before running tests')
            parent_role = Role.query.filter_by(name='parent').first()
            self.assertIsNotNone(parent_role, 'seed roles before running tests')
            sample_permission = Permission.query.filter_by(name='view_reports').first()
            self.assertIsNotNone(sample_permission, 'seed permissions before running tests')

            # ── School A (manager's school) ─────────────────────────────────
            school_a = School(
                school_name=f'School A {self.suffix}',
                code=f'SA{self.suffix[:8]}',
                capacity=0,
                is_active=True,
            )
            db.session.add(school_a)
            db.session.flush()

            year_a = AcademicYear(
                school_id=school_a.id,
                name=f'YA {self.suffix}',
                start_date=date(2025, 8, 1),
                end_date=date(2026, 6, 30),
                is_current=True,
            )
            db.session.add(year_a)
            db.session.flush()

            grade_a = Grade(
                school_id=school_a.id,
                academic_year_id=year_a.id,
                name=f'G1A {self.suffix}',
            )
            db.session.add(grade_a)
            db.session.flush()

            section_a = Section(
                school_id=school_a.id,
                academic_year_id=year_a.id,
                grade_id=grade_a.id,
                name=f'AA{self.suffix[:4]}',
                capacity=30,
            )
            db.session.add(section_a)
            db.session.flush()

            subject_a = Subject(
                school_id=school_a.id,
                academic_year_id=year_a.id,
                grade_id=grade_a.id,
                name=f'Arabic Math {self.suffix}',
                code=f'AM{self.suffix[:6]}',
            )
            db.session.add(subject_a)
            db.session.flush()

            student_a = Student(
                student_id=f'SA-{self.suffix}',
                full_name=f'Student A {self.suffix}',
                date_of_birth=date(2015, 1, 1),
                gender='male',
                school_id=school_a.id,
                academic_year_id=year_a.id,
                section_id=section_a.id,
                status='active',
            )
            db.session.add(student_a)
            db.session.flush()

            # School manager for school_a
            manager = User(
                username=f'mgr_{self.suffix}',
                email=f'mgr_{self.suffix}@example.test',
                full_name=f'Manager {self.suffix}',
                role_id=school_admin_role.id,
                school_id=school_a.id,
                is_active=True,
            )
            manager.set_password('Password123')
            db.session.add(manager)
            db.session.flush()

            # ── School B (other school) ──────────────────────────────────────
            school_b = School(
                school_name=f'School B {self.suffix}',
                code=f'SB{self.suffix[:8]}',
                capacity=0,
                is_active=True,
            )
            db.session.add(school_b)
            db.session.flush()

            year_b = AcademicYear(
                school_id=school_b.id,
                name=f'YB {self.suffix}',
                start_date=date(2025, 8, 1),
                end_date=date(2026, 6, 30),
                is_current=True,
            )
            db.session.add(year_b)
            db.session.flush()

            grade_b = Grade(
                school_id=school_b.id,
                academic_year_id=year_b.id,
                name=f'G1B {self.suffix}',
            )
            db.session.add(grade_b)
            db.session.flush()

            section_b = Section(
                school_id=school_b.id,
                academic_year_id=year_b.id,
                grade_id=grade_b.id,
                name=f'BA{self.suffix[:4]}',
                capacity=30,
            )
            db.session.add(section_b)
            db.session.flush()

            subject_b = Subject(
                school_id=school_b.id,
                academic_year_id=year_b.id,
                grade_id=grade_b.id,
                name=f'Other Subject {self.suffix}',
                code=f'OS{self.suffix[:6]}',
            )
            db.session.add(subject_b)
            db.session.flush()

            student_b = Student(
                student_id=f'SB-{self.suffix}',
                full_name=f'Student B {self.suffix}',
                date_of_birth=date(2015, 2, 1),
                gender='female',
                school_id=school_b.id,
                academic_year_id=year_b.id,
                section_id=section_b.id,
                status='active',
            )
            db.session.add(student_b)
            db.session.flush()

            user_b = User(
                username=f'user_b_{self.suffix}',
                email=f'user_b_{self.suffix}@example.test',
                full_name=f'User B {self.suffix}',
                role_id=teacher_role.id,
                school_id=school_b.id,
                is_active=True,
            )
            user_b.set_password('Password123')
            db.session.add(user_b)

            db.session.commit()

            self.created = {
                'school_admin_role_id': school_admin_role.id,
                'super_admin_role_id':  super_admin_role.id,
                'teacher_role_id':      teacher_role.id,
                'parent_role_id':       parent_role.id,
                'sample_permission_id': sample_permission.id,
                'school_a_id':     school_a.id,
                'year_a_id':       year_a.id,
                'grade_a_id':      grade_a.id,
                'section_a_id':    section_a.id,
                'subject_a_id':    subject_a.id,
                'student_a_id':    student_a.id,
                'student_a_pk':    student_a.id,
                'manager_id':      manager.id,
                'school_b_id':     school_b.id,
                'year_b_id':       year_b.id,
                'grade_b_id':      grade_b.id,
                'section_b_id':    section_b.id,
                'subject_b_id':    subject_b.id,
                'student_b_id':    student_b.id,
                'user_b_id':       user_b.id,
            }

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            ids = self.created

            # Clean up any users/employees created during tests
            for emp in (Employee.query
                        .execution_options(bypass_tenant_scope=True)
                        .filter(Employee.school_id.in_([
                            ids.get('school_a_id'), ids.get('school_b_id')
                        ]))
                        .all()):
                db.session.execute(
                    teacher_subjects.delete().where(
                        teacher_subjects.c.employee_id == emp.id
                    )
                )
            db.session.flush()

            for model, bypass in [
                (Employee, True),
                (User,     True),
            ]:
                q = (model.query
                     .execution_options(bypass_tenant_scope=True)
                     .filter(model.school_id.in_([
                         ids.get('school_a_id'), ids.get('school_b_id')
                     ])))
                for obj in q.all():
                    if obj.id not in (ids.get('manager_id'), ids.get('user_b_id')):
                        db.session.delete(obj)
            db.session.flush()

            # Delete the fixture users
            for model, key in [
                (User,    'manager_id'),
                (User,    'user_b_id'),
            ]:
                obj = db.session.get(
                    model, ids.get(key),
                    execution_options={'bypass_tenant_scope': True},
                )
                if obj:
                    if hasattr(obj, 'children'):
                        obj.children = []
                    db.session.delete(obj)

            for model, key in [
                (Student,      'student_a_id'),
                (Student,      'student_b_id'),
                (Subject,      'subject_a_id'),
                (Subject,      'subject_b_id'),
                (Section,      'section_a_id'),
                (Section,      'section_b_id'),
                (Grade,        'grade_a_id'),
                (Grade,        'grade_b_id'),
                (AcademicYear, 'year_a_id'),
                (AcademicYear, 'year_b_id'),
                (School,       'school_a_id'),
                (School,       'school_b_id'),
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

    def test_manager_can_create_teacher_in_own_school(self):
        """School manager POST /users/create with teacher role creates user in own school."""
        from app.blueprints.admin import create_user

        ids = self.created
        username = f'teacher_{self.suffix}'
        email    = f'teacher_{self.suffix}@example.test'

        with self.app.test_request_context(
            '/admin/users/create',
            method='POST',
            data={
                'username':  username,
                'email':     email,
                'full_name': f'Teacher {self.suffix}',
                'password':  'Password123',
                'role_id':   str(ids['teacher_role_id']),
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
                             'Expected redirect after successful user creation')

            # Verify the user was created with correct school_id
            created_user = (User.query
                            .execution_options(bypass_tenant_scope=True)
                            .filter_by(username=username).first())
            self.assertIsNotNone(created_user, 'User should have been created')
            self.assertEqual(created_user.school_id, ids['school_a_id'],
                             'Created user must belong to the manager\'s school')

            # Verify Employee record was auto-created
            emp = (Employee.query
                   .execution_options(bypass_tenant_scope=True)
                   .filter_by(user_id=created_user.id).first())
            self.assertIsNotNone(emp, 'Employee record should be auto-created for teacher')
            self.assertEqual(emp.school_id, ids['school_a_id'])

            logout_user()

    def test_manager_create_form_has_no_extra_permissions_section(self):
        """School manager create form must not expose per-user permission checkboxes."""
        from app.blueprints.admin import create_user

        with self.app.test_request_context('/admin/users/create', method='GET'):
            manager = db.session.get(
                User, self.created['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()

            html = create_user()
            self.assertNotIn('صلاحيات إضافية', html)
            self.assertNotIn('name="permissions"', html)
            logout_user()

    def test_manager_cannot_assign_extra_permissions_on_create(self):
        """Forged permission IDs from a school manager are ignored on user creation."""
        from app.blueprints.admin import create_user

        ids = self.created
        username = f'perm_forge_{self.suffix}'

        with self.app.test_request_context(
            '/admin/users/create',
            method='POST',
            data={
                'username': username,
                'email': f'{username}@example.test',
                'full_name': f'Permission Forge {self.suffix}',
                'password': 'Password123',
                'role_id': str(ids['teacher_role_id']),
                'permissions': [str(ids['sample_permission_id'])],
            },
        ):
            manager = db.session.get(
                User, ids['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()

            response = create_user()
            self.assertEqual(response.status_code, 302)

            created_user = (User.query
                            .execution_options(bypass_tenant_scope=True)
                            .filter_by(username=username)
                            .one())
            self.assertEqual(created_user.extra_permissions, [])
            logout_user()

    def test_manager_can_create_user_without_email(self):
        """Email is optional for school-manager-created users."""
        from app.blueprints.admin import create_user

        ids = self.created
        username = f'no_email_{self.suffix}'

        with self.app.test_request_context(
            '/admin/users/create',
            method='POST',
            data={
                'username': username,
                'email': '',
                'full_name': f'No Email {self.suffix}',
                'password': 'Password123',
                'role_id': str(ids['teacher_role_id']),
            },
        ):
            manager = db.session.get(
                User, ids['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()

            response = create_user()
            self.assertEqual(response.status_code, 302)

            created_user = (User.query
                            .execution_options(bypass_tenant_scope=True)
                            .filter_by(username=username)
                            .one())
            self.assertIsNone(created_user.email)
            self.assertEqual(created_user.school_id, ids['school_a_id'])
            logout_user()

    def test_duplicate_email_still_rejected_when_provided(self):
        """Provided email values still participate in duplicate validation."""
        from app.blueprints.admin import create_user

        ids = self.created
        username = f'dupe_email_{self.suffix}'

        with self.app.test_request_context(
            '/admin/users/create',
            method='POST',
            data={
                'username': username,
                'email': f'user_b_{self.suffix}@example.test',
                'full_name': f'Dupe Email {self.suffix}',
                'password': 'Password123',
                'role_id': str(ids['teacher_role_id']),
            },
        ):
            manager = db.session.get(
                User, ids['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()

            response = create_user()
            self.assertEqual(response.status_code, 302)

            created_user = (User.query
                            .execution_options(bypass_tenant_scope=True)
                            .filter_by(username=username)
                            .first())
            self.assertIsNone(created_user)
            db.session.rollback()
            logout_user()

    def test_manager_create_form_scopes_searchable_student_and_subject_options(self):
        """Searchable parent/teacher selectors show only current-school options."""
        from app.blueprints.admin import create_user

        ids = self.created
        with self.app.test_request_context('/admin/users/create', method='GET'):
            manager = db.session.get(
                User, ids['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()

            html = create_user()
            self.assertIn('data-filter-target="studentOptions"', html)
            self.assertIn('data-filter-target="subjectOptions"', html)
            self.assertIn(f'Student A {self.suffix}', html)
            self.assertIn(f'Arabic Math {self.suffix}', html)
            self.assertNotIn(f'Student B {self.suffix}', html)
            self.assertNotIn(f'Other Subject {self.suffix}', html)
            logout_user()

    # ── Test 2 ──────────────────────────────────────────────────────────────

    def test_manager_cannot_create_admin_role_user(self):
        """School manager cannot create a user with an admin role — redirects with error."""
        from app.blueprints.admin import create_user

        ids = self.created
        username = f'bad_admin_{self.suffix}'

        with self.app.test_request_context(
            '/admin/users/create',
            method='POST',
            data={
                'username':  username,
                'email':     f'{username}@example.test',
                'full_name': 'Bad Admin',
                'password':  'Password123',
                'role_id':   str(ids['super_admin_role_id']),  # super_admin role — forbidden
            },
        ):
            manager = db.session.get(
                User, ids['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()

            response = create_user()
            # Should redirect back with an error, not create the user
            self.assertEqual(response.status_code, 302)

            bad_user = (User.query
                        .execution_options(bypass_tenant_scope=True)
                        .filter_by(username=username).first())
            self.assertIsNone(bad_user,
                              'Admin-role user must NOT be created by school manager')
            db.session.rollback()
            logout_user()

    # ── Test 3 ──────────────────────────────────────────────────────────────

    def test_manager_cannot_see_other_school_users(self):
        """School manager query cannot return users from school B."""
        with self.app.test_request_context('/'):
            manager = db.session.get(
                User, self.created['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()

            # Attempt to fetch a user from another school
            other_user = User.query.filter_by(id=self.created['user_b_id']).first()
            self.assertIsNone(other_user,
                              'School manager must not see users from another school')
            logout_user()

    # ── Test 4 ──────────────────────────────────────────────────────────────

    def test_manager_cannot_edit_other_school_user(self):
        """edit_user() raises 403 when school manager targets another school's user."""
        from app.blueprints.admin import edit_user
        from werkzeug.exceptions import Forbidden

        ids = self.created
        with self.app.test_request_context(
            f'/admin/users/{ids["user_b_id"]}/edit',
            method='GET',
        ):
            manager = db.session.get(
                User, ids['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()

            with self.assertRaises(Forbidden):
                edit_user(user_id=ids['user_b_id'])
            logout_user()

    def test_manager_cannot_edit_own_account(self):
        """School manager cannot edit their own account via School User Management."""
        from app.blueprints.admin import edit_user
        from werkzeug.exceptions import Forbidden

        ids = self.created
        with self.app.test_request_context(
            f'/admin/users/{ids["manager_id"]}/edit',
            method='POST',
            data={
                'full_name': 'Changed Manager Name',
                'email': f'changed_{self.suffix}@example.test',
                'role_id': str(ids['teacher_role_id']),
                'permissions': [str(ids['sample_permission_id'])],
            },
        ):
            manager = db.session.get(
                User, ids['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            original_name = manager.full_name
            original_email = manager.email
            original_role_id = manager.role_id
            login_user(manager)
            self._run_before_request()

            with self.assertRaises(Forbidden):
                edit_user(user_id=ids['manager_id'])

            db.session.rollback()
            manager = db.session.get(
                User, ids['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            self.assertEqual(manager.full_name, original_name)
            self.assertEqual(manager.email, original_email)
            self.assertEqual(manager.role_id, original_role_id)
            self.assertEqual(manager.extra_permissions, [])
            logout_user()

    def test_manager_users_list_hides_self_edit_link(self):
        """School manager user list does not show an edit action for their own row."""
        from app.blueprints.admin import users_list

        ids = self.created
        with self.app.test_request_context('/admin/users'):
            manager = db.session.get(
                User, ids['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()

            html = users_list()
            self.assertNotIn(f'/admin/users/{ids["manager_id"]}/edit', html)
            self.assertIn('حسابك', html)
            logout_user()

    # ── Test 5 ──────────────────────────────────────────────────────────────

    def test_manager_can_create_parent_linked_to_own_school_student(self):
        """School manager creates parent user linked to student from their school."""
        from app.blueprints.admin import create_user

        ids = self.created
        username = f'parent_{self.suffix}'

        with self.app.test_request_context(
            '/admin/users/create',
            method='POST',
            data={
                'username':    username,
                'email':       f'{username}@example.test',
                'full_name':   f'Parent {self.suffix}',
                'password':    'Password123',
                'role_id':     str(ids['parent_role_id']),
                'student_ids': str(ids['student_a_pk']),
            },
        ):
            manager = db.session.get(
                User, ids['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()

            response = create_user()
            self.assertEqual(response.status_code, 302)

            created_parent = (User.query
                              .execution_options(bypass_tenant_scope=True)
                              .filter_by(username=username).first())
            self.assertIsNotNone(created_parent)
            child_ids = {c.id for c in created_parent.children}
            self.assertIn(ids['student_a_pk'], child_ids,
                          'Parent should be linked to their own school student')
            logout_user()

    # ── Test 6 ──────────────────────────────────────────────────────────────

    def test_manager_cannot_link_parent_to_other_school_student(self):
        """Students from another school are excluded when school manager creates a parent."""
        from app.blueprints.admin import create_user

        ids = self.created
        username = f'parent2_{self.suffix}'

        with self.app.test_request_context(
            '/admin/users/create',
            method='POST',
            data={
                'username':    username,
                'email':       f'{username}@example.test',
                'full_name':   f'Parent2 {self.suffix}',
                'password':    'Password123',
                'role_id':     str(ids['parent_role_id']),
                'student_ids': str(ids['student_b_id']),  # from school_b — forbidden
            },
        ):
            manager = db.session.get(
                User, ids['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()

            response = create_user()
            self.assertEqual(response.status_code, 302)

            created_parent = (User.query
                              .execution_options(bypass_tenant_scope=True)
                              .filter_by(username=username).first())
            self.assertIsNotNone(created_parent)
            child_ids = {c.id for c in created_parent.children}
            self.assertNotIn(ids['student_b_id'], child_ids,
                             'Parent must NOT be linked to a student from another school')
            logout_user()

    # ── Test 7 ──────────────────────────────────────────────────────────────

    def test_teacher_gets_section_assignment(self):
        """When teacher_section_ids is provided, the Employee's homeroom is set."""
        from app.blueprints.admin import create_user

        ids = self.created
        username = f'teacher2_{self.suffix}'

        with self.app.test_request_context(
            '/admin/users/create',
            method='POST',
            data={
                'username':            username,
                'email':               f'{username}@example.test',
                'full_name':           f'Teacher2 {self.suffix}',
                'password':            'Password123',
                'role_id':             str(ids['teacher_role_id']),
                'teacher_section_ids': str(ids['section_a_id']),
            },
        ):
            manager = db.session.get(
                User, ids['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()

            response = create_user()
            self.assertEqual(response.status_code, 302)

            created_user = (User.query
                            .execution_options(bypass_tenant_scope=True)
                            .filter_by(username=username).first())
            self.assertIsNotNone(created_user)

            emp = (Employee.query
                   .execution_options(bypass_tenant_scope=True)
                   .filter_by(user_id=created_user.id).first())
            self.assertIsNotNone(emp, 'Employee record must exist')

            section = db.session.get(
                Section, ids['section_a_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            self.assertEqual(section.teacher_id, emp.id,
                             'Section teacher_id should point to the new Employee')
            logout_user()

    def test_manager_can_create_teacher_linked_to_subject(self):
        """Selected teacher subject IDs create scoped teacher_subjects links."""
        from app.blueprints.admin import create_user
        from sqlalchemy import select as sa_select

        ids = self.created
        username = f'teacher_subject_{self.suffix}'

        with self.app.test_request_context(
            '/admin/users/create',
            method='POST',
            data={
                'username': username,
                'email': '',
                'full_name': f'Teacher Subject {self.suffix}',
                'password': 'Password123',
                'role_id': str(ids['teacher_role_id']),
                'teacher_section_ids': str(ids['section_a_id']),
                'teacher_subject_ids': [
                    str(ids['subject_a_id']),
                    str(ids['subject_a_id']),
                    str(ids['subject_b_id']),
                ],
            },
        ):
            manager = db.session.get(
                User, ids['manager_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()

            response = create_user()
            self.assertEqual(response.status_code, 302)

            created_user = (User.query
                            .execution_options(bypass_tenant_scope=True)
                            .filter_by(username=username).first())
            self.assertIsNotNone(created_user)

            emp = (Employee.query
                   .execution_options(bypass_tenant_scope=True)
                   .filter_by(user_id=created_user.id).one())
            rows = db.session.execute(
                sa_select(teacher_subjects).where(
                    teacher_subjects.c.employee_id == emp.id
                )
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].subject_id, ids['subject_a_id'])
            self.assertEqual(rows[0].section_id, ids['section_a_id'])
            logout_user()


if __name__ == '__main__':
    unittest.main()

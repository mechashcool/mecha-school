"""
Tests for academic-year guards and parent isolation:

1. test_student_create_redirects_when_no_active_year
   Student create (GET) returns a 302 redirect with a flash message when the
   current school has no active academic year — NOT a 500 IntegrityError.

2. test_student_create_post_redirects_when_no_active_year
   Same as above but for the POST path (form submission).

3. test_duplicate_year_name_does_not_crash
   Attempting to create an academic year whose name already exists for that
   school returns a 302 redirect — NOT a 500 IntegrityError.

4. test_parent_sees_only_own_school_students
   A parent user whose school_id = A cannot see students belonging to school B,
   even when querying with include_all_years=True.
"""
import unittest
from datetime import date
from uuid import uuid4

from flask_login import login_user, logout_user

from app import create_app
from app.models import (
    db, AcademicYear, Grade, Role, School, Section, Student, User,
)


class AcademicYearGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('development')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.created = {}

        with self.app.app_context():
            school_admin_role = Role.query.filter_by(name='school_admin').first()
            self.assertIsNotNone(school_admin_role, 'seed roles before running guard tests')
            parent_role = Role.query.filter_by(name='parent').first()
            self.assertIsNotNone(parent_role, 'seed roles before running guard tests')

            # ── School with NO active year ──────────────────────────────────
            no_year_school = School(
                school_name=f'NoYear School {self.suffix}',
                code=f'NY{self.suffix[:8]}',
                capacity=0,
                is_active=True,
            )
            db.session.add(no_year_school)
            db.session.flush()

            manager_no_year = User(
                username=f'mgr_ny_{self.suffix}',
                email=f'mgr_ny_{self.suffix}@example.test',
                full_name=f'Manager NoYear {self.suffix}',
                role_id=school_admin_role.id,
                school_id=no_year_school.id,
                is_active=True,
            )
            manager_no_year.set_password('Password123')
            db.session.add(manager_no_year)
            db.session.flush()

            # ── School WITH an active year (for duplicate-name + parent tests) ──
            year_school = School(
                school_name=f'Year School {self.suffix}',
                code=f'YS{self.suffix[:8]}',
                capacity=0,
                is_active=True,
            )
            db.session.add(year_school)
            db.session.flush()

            year_a = AcademicYear(
                school_id=year_school.id,
                name=f'Year {self.suffix}',
                start_date=date(2025, 8, 1),
                end_date=date(2026, 6, 30),
                is_current=True,
            )
            db.session.add(year_a)
            db.session.flush()

            grade = Grade(
                school_id=year_school.id,
                academic_year_id=year_a.id,
                name=f'G1 {self.suffix}',
            )
            db.session.add(grade)
            db.session.flush()

            section = Section(
                school_id=year_school.id,
                academic_year_id=year_a.id,
                grade_id=grade.id,
                name=f'A{self.suffix[:4]}',
                capacity=30,
            )
            db.session.add(section)
            db.session.flush()

            student_a = Student(
                student_id=f'SA-{self.suffix}',
                full_name=f'Student A {self.suffix}',
                date_of_birth=date(2015, 1, 1),
                gender='male',
                school_id=year_school.id,
                academic_year_id=year_a.id,
                section_id=section.id,
                status='active',
            )
            db.session.add(student_a)
            db.session.flush()

            # ── Second school for parent-isolation test ─────────────────────
            other_school = School(
                school_name=f'Other Year School {self.suffix}',
                code=f'OY{self.suffix[:8]}',
                capacity=0,
                is_active=True,
            )
            db.session.add(other_school)
            db.session.flush()

            other_year = AcademicYear(
                school_id=other_school.id,
                name=f'Other Year {self.suffix}',
                start_date=date(2025, 8, 1),
                end_date=date(2026, 6, 30),
                is_current=True,
            )
            db.session.add(other_year)
            db.session.flush()

            other_grade = Grade(
                school_id=other_school.id,
                academic_year_id=other_year.id,
                name=f'OG1 {self.suffix}',
            )
            db.session.add(other_grade)
            db.session.flush()

            other_section = Section(
                school_id=other_school.id,
                academic_year_id=other_year.id,
                grade_id=other_grade.id,
                name=f'OA{self.suffix[:4]}',
                capacity=30,
            )
            db.session.add(other_section)
            db.session.flush()

            student_b = Student(
                student_id=f'SB-{self.suffix}',
                full_name=f'Student B {self.suffix}',
                date_of_birth=date(2015, 2, 1),
                gender='female',
                school_id=other_school.id,
                academic_year_id=other_year.id,
                section_id=other_section.id,
                status='active',
            )
            db.session.add(student_b)
            db.session.flush()

            # Parent in year_school linked ONLY to student_a
            parent = User(
                username=f'parent_{self.suffix}',
                email=f'parent_{self.suffix}@example.test',
                full_name=f'Parent {self.suffix}',
                role_id=parent_role.id,
                school_id=year_school.id,
                is_active=True,
            )
            parent.set_password('Password123')
            db.session.add(parent)
            db.session.flush()
            parent.children = [student_a]

            db.session.commit()

            self.created = {
                'no_year_school_id':    no_year_school.id,
                'manager_no_year_id':   manager_no_year.id,
                'year_school_id':       year_school.id,
                'year_a_id':            year_a.id,
                'year_name':            year_a.name,
                'grade_id':             grade.id,
                'section_id':           section.id,
                'student_a_id':         student_a.id,
                'student_a_code':       student_a.student_id,
                'other_school_id':      other_school.id,
                'other_year_id':        other_year.id,
                'other_grade_id':       other_grade.id,
                'other_section_id':     other_section.id,
                'student_b_id':         student_b.id,
                'student_b_code':       student_b.student_id,
                'parent_id':            parent.id,
            }

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            ids = self.created

            # Clear parent-student association before deleting either side
            parent = db.session.get(
                User, ids.get('parent_id'),
                execution_options={'bypass_tenant_scope': True},
            )
            if parent:
                parent.children = []
                db.session.flush()

            for model, key in [
                (User,         'parent_id'),
                (User,         'manager_no_year_id'),
                (Student,      'student_a_id'),
                (Student,      'student_b_id'),
                (Section,      'section_id'),
                (Grade,        'grade_id'),
                (Section,      'other_section_id'),
                (Grade,        'other_grade_id'),
                (AcademicYear, 'year_a_id'),
                (AcademicYear, 'other_year_id'),
                (School,       'no_year_school_id'),
                (School,       'year_school_id'),
                (School,       'other_school_id'),
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

    def test_student_create_redirects_when_no_active_year(self):
        """GET /students/create returns 302 (not 500) when no active year exists."""
        from app.blueprints.students import create as student_create

        with self.app.test_request_context('/students/create', method='GET'):
            manager = db.session.get(
                User, self.created['manager_no_year_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()

            response = student_create()
            self.assertEqual(response.status_code, 302,
                             'Expected redirect when no active year, got non-redirect response')
            logout_user()

    # ── Test 2 ──────────────────────────────────────────────────────────────

    def test_student_create_post_redirects_when_no_active_year(self):
        """POST /students/create returns 302 (not IntegrityError) when no active year."""
        from app.blueprints.students import create as student_create

        with self.app.test_request_context(
            '/students/create',
            method='POST',
            data={
                'full_name':     'Ghost Student',
                'gender':        'male',
                'date_of_birth': '2015-01-01',
            },
        ):
            manager = db.session.get(
                User, self.created['manager_no_year_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(manager)
            self._run_before_request()

            response = student_create()
            self.assertEqual(response.status_code, 302,
                             'Expected redirect on POST with no active year')
            # Ensure nothing was committed
            db.session.rollback()
            logout_user()

    # ── Test 3 ──────────────────────────────────────────────────────────────

    def test_duplicate_year_name_does_not_crash(self):
        """Creating a duplicate academic year name returns 302, not IntegrityError."""
        from app.blueprints.schools import create_year

        school_id = self.created['year_school_id']
        dup_name  = self.created['year_name']

        with self.app.test_request_context(
            f'/schools/{school_id}/years/create',
            method='POST',
            data={
                'name':       dup_name,
                'start_date': '2027-08-01',
                'end_date':   '2028-06-30',
                'is_current': '',
            },
        ):
            admin = (User.query
                     .execution_options(bypass_tenant_scope=True)
                     .filter_by(username='admin')
                     .first())
            self.assertIsNotNone(admin, 'admin user must exist')
            login_user(admin)
            self._run_before_request()

            try:
                response = create_year(school_id=school_id)
                self.assertEqual(response.status_code, 302,
                                 'Expected redirect on duplicate year name')
            except Exception as exc:
                self.fail(f'Duplicate year name raised an exception: {exc}')
            finally:
                db.session.rollback()
                logout_user()

    # ── Test 4 ──────────────────────────────────────────────────────────────

    def test_parent_sees_only_own_school_students(self):
        """Parent user cannot see students from a different school via ORM queries."""
        with self.app.test_request_context('/'):
            parent = db.session.get(
                User, self.created['parent_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(parent)
            self._run_before_request()

            visible_codes = {
                s.student_id
                for s in Student.query.execution_options(include_all_years=True).all()
            }
            self.assertIn(
                self.created['student_a_code'], visible_codes,
                'Parent should see their own school student',
            )
            self.assertNotIn(
                self.created['student_b_code'], visible_codes,
                'Parent must NOT see a student from a different school',
            )
            logout_user()


if __name__ == '__main__':
    unittest.main()

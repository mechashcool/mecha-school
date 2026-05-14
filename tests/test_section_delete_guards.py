import unittest
from datetime import date
from uuid import uuid4

from flask_login import login_user, logout_user

from app import create_app
from app.models import db, AcademicYear, Grade, Role, School, Section, Student, User


class SectionDeleteGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('development')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.created = {}

        with self.app.app_context():
            admin_role = Role.query.filter_by(name='super_admin').first()
            self.assertIsNotNone(admin_role, 'seed roles before running section tests')

            school = School(
                school_name=f'Delete Guard School {self.suffix}',
                code=f'DG{self.suffix[:8]}',
                capacity=0,
                is_active=True,
            )
            db.session.add(school)
            db.session.flush()

            year = AcademicYear(
                school_id=school.id,
                name=f'Delete Guard Year {self.suffix}',
                start_date=date(2025, 8, 1),
                end_date=date(2026, 6, 30),
                is_current=True,
            )
            db.session.add(year)
            db.session.flush()

            grade = Grade(
                school_id=school.id,
                academic_year_id=year.id,
                name=f'Delete Guard Grade {self.suffix}',
            )
            db.session.add(grade)
            db.session.flush()

            empty_section = Section(
                school_id=school.id,
                academic_year_id=year.id,
                grade_id=grade.id,
                name=f'E{self.suffix[:4]}',
                capacity=30,
            )
            student_section = Section(
                school_id=school.id,
                academic_year_id=year.id,
                grade_id=grade.id,
                name=f'S{self.suffix[:4]}',
                capacity=30,
            )
            db.session.add_all([empty_section, student_section])
            db.session.flush()

            student = Student(
                student_id=f'DG-{self.suffix}',
                full_name=f'Delete Guard Student {self.suffix}',
                date_of_birth=date(2015, 1, 1),
                gender='male',
                school_id=school.id,
                academic_year_id=year.id,
                section_id=student_section.id,
                status='active',
            )
            db.session.add(student)
            db.session.flush()

            super_admin = User(
                username=f'section_sa_{self.suffix}',
                email=f'section_sa_{self.suffix}@example.test',
                full_name=f'Section Super Admin {self.suffix}',
                role_id=admin_role.id,
                school_id=None,
                is_active=True,
            )
            super_admin.set_password('Password123')
            db.session.add(super_admin)
            db.session.commit()

            self.created = {
                'school_id': school.id,
                'year_id': year.id,
                'grade_id': grade.id,
                'empty_section_id': empty_section.id,
                'student_section_id': student_section.id,
                'student_id': student.id,
                'super_admin_id': super_admin.id,
            }

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            ids = self.created

            for model, key in [
                (Student, 'student_id'),
                (Section, 'empty_section_id'),
                (Section, 'student_section_id'),
                (Grade, 'grade_id'),
                (AcademicYear, 'year_id'),
                (User, 'super_admin_id'),
                (School, 'school_id'),
            ]:
                obj = db.session.get(
                    model,
                    ids.get(key),
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

    def _login_super_admin(self):
        user = db.session.get(
            User,
            self.created['super_admin_id'],
            execution_options={'bypass_tenant_scope': True},
        )
        login_user(user)
        self._run_before_request()

    def test_delete_grade_with_sections_is_blocked(self):
        from app.blueprints.sections import delete_grade

        with self.app.test_request_context(
            f'/sections/grades/{self.created["grade_id"]}/delete',
            method='POST',
        ):
            self._login_super_admin()
            response = delete_grade(self.created['grade_id'])
            self.assertEqual(response.status_code, 302)
            logout_user()

        with self.app.app_context():
            grade = db.session.get(
                Grade,
                self.created['grade_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            section = db.session.get(
                Section,
                self.created['empty_section_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            self.assertIsNotNone(grade)
            self.assertIsNotNone(section)
            self.assertEqual(section.grade_id, self.created['grade_id'])

    def test_delete_empty_section_succeeds(self):
        from app.blueprints.sections import delete_section

        with self.app.test_request_context(
            f'/sections/sections/{self.created["empty_section_id"]}/delete',
            method='POST',
        ):
            self._login_super_admin()
            response = delete_section(self.created['empty_section_id'])
            self.assertEqual(response.status_code, 302)
            logout_user()

        with self.app.app_context():
            section = db.session.get(
                Section,
                self.created['empty_section_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            grade = db.session.get(
                Grade,
                self.created['grade_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            self.assertIsNone(section)
            self.assertIsNotNone(grade)

    def test_delete_section_with_student_is_blocked(self):
        from app.blueprints.sections import delete_section

        with self.app.test_request_context(
            f'/sections/sections/{self.created["student_section_id"]}/delete',
            method='POST',
        ):
            self._login_super_admin()
            response = delete_section(self.created['student_section_id'])
            self.assertEqual(response.status_code, 302)
            logout_user()

        with self.app.app_context():
            section = db.session.get(
                Section,
                self.created['student_section_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            student = db.session.get(
                Student,
                self.created['student_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            self.assertIsNotNone(section)
            self.assertEqual(student.section_id, self.created['student_section_id'])


if __name__ == '__main__':
    unittest.main()

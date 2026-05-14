import unittest
from datetime import date
from uuid import uuid4

from flask_login import login_user, logout_user

from app import create_app
from app.models import (
    db, AcademicYear, Grade, Role, School, Section, Student, Subject, User,
)


class TenantIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('development')

    def setUp(self):
        self.created = {}
        self.suffix = uuid4().hex[:10]

        with self.app.app_context():
            role = Role.query.filter_by(name='admin').first()
            self.assertIsNotNone(role, 'seed roles before running isolation tests')

            base_school = School.query.order_by(School.id).first()
            self.assertIsNotNone(base_school, 'at least one school is required')
            base_year = AcademicYear.query.execution_options(
                bypass_tenant_scope=True
            ).filter_by(school_id=base_school.id, is_current=True).first()
            self.assertIsNotNone(base_year, 'base school needs a current year')

            school_b = School(
                school_name=f'Isolation School {self.suffix}',
                code=f'ISO{self.suffix[:6]}',
                capacity=0,
                is_active=True,
            )
            db.session.add(school_b)
            db.session.flush()

            year_b_current = AcademicYear(
                school_id=school_b.id,
                name=f'Current {self.suffix}',
                start_date=date(2025, 8, 1),
                end_date=date(2026, 6, 30),
                is_current=True,
            )
            year_b_old = AcademicYear(
                school_id=school_b.id,
                name=f'Old {self.suffix}',
                start_date=date(2024, 8, 1),
                end_date=date(2025, 6, 30),
                is_current=False,
            )
            db.session.add_all([year_b_current, year_b_old])
            db.session.flush()

            grade_current = Grade(
                school_id=school_b.id,
                academic_year_id=year_b_current.id,
                name=f'G Current {self.suffix}',
            )
            grade_old = Grade(
                school_id=school_b.id,
                academic_year_id=year_b_old.id,
                name=f'G Old {self.suffix}',
            )
            db.session.add_all([grade_current, grade_old])
            db.session.flush()

            section_current = Section(
                school_id=school_b.id,
                academic_year_id=year_b_current.id,
                grade_id=grade_current.id,
                name=f'A{self.suffix[:4]}',
                capacity=30,
            )
            section_old = Section(
                school_id=school_b.id,
                academic_year_id=year_b_old.id,
                grade_id=grade_old.id,
                name=f'B{self.suffix[:4]}',
                capacity=30,
            )
            db.session.add_all([section_current, section_old])
            db.session.flush()

            student_current = Student(
                student_id=f'ISO-C-{self.suffix}',
                full_name='Isolation Current Student',
                date_of_birth=date(2015, 1, 1),
                gender='male',
                school_id=school_b.id,
                academic_year_id=year_b_current.id,
                section_id=section_current.id,
                status='active',
            )
            student_old = Student(
                student_id=f'ISO-O-{self.suffix}',
                full_name='Isolation Old Student',
                date_of_birth=date(2014, 1, 1),
                gender='male',
                school_id=school_b.id,
                academic_year_id=year_b_old.id,
                section_id=section_old.id,
                status='active',
            )
            db.session.add_all([student_current, student_old])

            user_a = User(
                username=f'iso_a_{self.suffix}',
                email=f'iso_a_{self.suffix}@example.test',
                full_name='Isolation User A',
                role_id=role.id,
                school_id=base_school.id,
            )
            user_a.set_password('Password123')
            user_b = User(
                username=f'iso_b_{self.suffix}',
                email=f'iso_b_{self.suffix}@example.test',
                full_name='Isolation User B',
                role_id=role.id,
                school_id=school_b.id,
            )
            user_b.set_password('Password123')
            db.session.add_all([user_a, user_b])
            db.session.commit()

            self.created = {
                'school_b_id': school_b.id,
                'year_current_id': year_b_current.id,
                'year_old_id': year_b_old.id,
                'grade_current_id': grade_current.id,
                'grade_old_id': grade_old.id,
                'section_current_id': section_current.id,
                'section_old_id': section_old.id,
                'student_current_id': student_current.id,
                'student_old_id': student_old.id,
                'student_current_code': student_current.student_id,
                'student_old_code': student_old.student_id,
                'user_a_id': user_a.id,
                'user_b_id': user_b.id,
            }

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            ids = self.created
            for model, key in [
                (Student, 'student_current_id'),
                (Student, 'student_old_id'),
                (Section, 'section_current_id'),
                (Section, 'section_old_id'),
                (Grade, 'grade_current_id'),
                (Grade, 'grade_old_id'),
                (AcademicYear, 'year_current_id'),
                (AcademicYear, 'year_old_id'),
                (User, 'user_a_id'),
                (User, 'user_b_id'),
                (School, 'school_b_id'),
            ]:
                obj = db.session.get(
                    model,
                    ids.get(key),
                    execution_options={'bypass_tenant_scope': True},
                )
                if obj is not None:
                    db.session.delete(obj)
            db.session.commit()

    def _run_before_request(self):
        for fn in self.app.before_request_funcs.get(None, []):
            rv = fn()
            if rv is not None:
                return rv
        return None

    def test_school_user_cannot_read_other_school(self):
        with self.app.test_request_context('/'):
            user_a = db.session.get(
                User,
                self.created['user_a_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(user_a)
            self._run_before_request()

            leaked = Student.query.execution_options(include_all_years=True).filter_by(
                student_id=self.created['student_current_code']
            ).first()
            self.assertIsNone(leaked)
            logout_user()

    def test_current_year_default_and_historical_opt_in(self):
        with self.app.test_request_context('/'):
            user_b = db.session.get(
                User,
                self.created['user_b_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(user_b)
            self._run_before_request()

            visible_codes = {s.student_id for s in Student.query.all()}
            self.assertIn(self.created['student_current_code'], visible_codes)
            self.assertNotIn(self.created['student_old_code'], visible_codes)

            historical = Student.query.execution_options(include_all_years=True).filter_by(
                student_id=self.created['student_old_code']
            ).first()
            self.assertIsNotNone(historical)
            logout_user()

    def test_super_admin_global_can_read_all_schools(self):
        with self.app.test_request_context('/'):
            admin = User.query.execution_options(bypass_tenant_scope=True).filter_by(
                username='admin'
            ).first()
            self.assertIsNotNone(admin)
            login_user(admin)
            self._run_before_request()

            row = Student.query.execution_options(include_all_years=True).filter_by(
                student_id=self.created['student_current_code']
            ).first()
            self.assertIsNotNone(row)
            logout_user()

    def test_cross_school_write_is_rejected(self):
        with self.app.test_request_context('/'):
            user_a = db.session.get(
                User,
                self.created['user_a_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(user_a)
            self._run_before_request()

            bad_subject = Subject(
                name=f'Bad Subject {self.suffix}',
                code=f'BAD{self.suffix[:6]}',
                school_id=self.created['school_b_id'],
                academic_year_id=self.created['year_current_id'],
            )
            db.session.add(bad_subject)
            with self.assertRaises(PermissionError):
                db.session.commit()
            db.session.rollback()
            logout_user()


if __name__ == '__main__':
    unittest.main()

import unittest
from datetime import date
from unittest.mock import patch
from uuid import uuid4

from flask_login import login_user, logout_user

from app import create_app
from app.blueprints.grades import index as grades_index
from app.models import (
    db, AcademicYear, Exam, ExamResult, Grade, Role, School, Section,
    Student, Subject, User,
)


class GradeStudentFilterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('development')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.created = {}

        with self.app.app_context():
            role = Role.query.filter_by(name='school_admin').first()
            self.assertIsNotNone(role, 'seed roles before grade filter tests')

            school = School(
                school_name=f'Grade Filter School {self.suffix}',
                code=f'GF{self.suffix[:8]}',
                capacity=0,
                is_active=True,
            )
            db.session.add(school)
            db.session.flush()

            year = AcademicYear(
                school_id=school.id,
                name=f'Grade Filter Year {self.suffix}',
                start_date=date(2025, 8, 1),
                end_date=date(2026, 6, 30),
                is_current=True,
            )
            db.session.add(year)
            db.session.flush()

            grade = Grade(
                school_id=school.id,
                academic_year_id=year.id,
                name=f'Grade {self.suffix}',
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
            subject = Subject(
                school_id=school.id,
                academic_year_id=year.id,
                name=f'Math {self.suffix}',
                code=f'MATH{self.suffix[:5]}',
            )
            db.session.add_all([section, subject])
            db.session.flush()

            student_a = Student(
                student_id=f'LAY-{self.suffix}',
                full_name=f'Layla Filter {self.suffix}',
                date_of_birth=date(2014, 1, 1),
                gender='female',
                school_id=school.id,
                academic_year_id=year.id,
                section_id=section.id,
                status='active',
            )
            student_b = Student(
                student_id=f'OMA-{self.suffix}',
                full_name=f'Omar Filter {self.suffix}',
                date_of_birth=date(2014, 1, 1),
                gender='male',
                school_id=school.id,
                academic_year_id=year.id,
                section_id=section.id,
                status='active',
            )
            db.session.add_all([student_a, student_b])
            db.session.flush()

            exam = Exam(
                school_id=school.id,
                academic_year_id=year.id,
                exam_name=f'Midterm Filter {self.suffix}',
                subject_id=subject.id,
                section_id=section.id,
                exam_date=date(2026, 1, 15),
                max_marks=100,
                pass_marks=50,
            )
            db.session.add(exam)
            db.session.flush()

            result_a = ExamResult(
                school_id=school.id,
                academic_year_id=year.id,
                exam_id=exam.id,
                student_id=student_a.id,
                marks=91,
                grade_letter='A',
                is_pass=True,
            )
            result_b = ExamResult(
                school_id=school.id,
                academic_year_id=year.id,
                exam_id=exam.id,
                student_id=student_b.id,
                marks=82,
                grade_letter='B',
                is_pass=True,
            )
            user = User(
                username=f'grade_filter_{self.suffix}',
                email=f'grade_filter_{self.suffix}@example.test',
                full_name='Grade Filter Admin',
                role_id=role.id,
                school_id=school.id,
                is_active=True,
            )
            user.set_password('Password123')
            db.session.add_all([result_a, result_b, user])
            db.session.commit()

            self.created = {
                'school_id': school.id,
                'year_id': year.id,
                'grade_id': grade.id,
                'section_id': section.id,
                'subject_id': subject.id,
                'student_a_id': student_a.id,
                'student_b_id': student_b.id,
                'exam_id': exam.id,
                'result_a_id': result_a.id,
                'result_b_id': result_b.id,
                'user_id': user.id,
            }

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            ids = self.created
            for model, key in [
                (ExamResult, 'result_a_id'),
                (ExamResult, 'result_b_id'),
                (Exam, 'exam_id'),
                (Student, 'student_a_id'),
                (Student, 'student_b_id'),
                (Subject, 'subject_id'),
                (Section, 'section_id'),
                (Grade, 'grade_id'),
                (AcademicYear, 'year_id'),
                (User, 'user_id'),
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

    def _login_user(self):
        user = db.session.get(
            User,
            self.created['user_id'],
            execution_options={'bypass_tenant_scope': True},
        )
        login_user(user)
        self._run_before_request()

    def _captured_index_context(self, query_string):
        with self.app.test_request_context(f'/grades/{query_string}'):
            self._login_user()
            captured = {}

            def fake_render(_template, **context):
                captured.update(context)
                return 'ok'

            with patch('app.blueprints.grades.render_template',
                       side_effect=fake_render):
                self.assertEqual(grades_index(), 'ok')
            logout_user()
            return captured

    def test_student_name_filter_limits_subject_report(self):
        context = self._captured_index_context(
            f'?subject_id={self.created["subject_id"]}&student_q=Layla'
        )

        self.assertEqual(len(context['results_view']), 1)
        self.assertEqual(
            context['results_view'][0].student_id,
            self.created['student_a_id'],
        )
        self.assertEqual(context['student_search'], 'Layla')

    def test_student_filter_does_not_affect_default_exam_list(self):
        context = self._captured_index_context('?student_q=NoSuchStudent')

        self.assertIsNone(context['results_view'])
        self.assertIn(self.created['exam_id'], {exam.id for exam in context['exams']})
        self.assertEqual(context['student_search'], 'NoSuchStudent')


if __name__ == '__main__':
    unittest.main()

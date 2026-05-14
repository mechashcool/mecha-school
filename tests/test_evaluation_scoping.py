import unittest
from datetime import date
from unittest.mock import patch
from uuid import uuid4

from flask_login import login_user, logout_user
from werkzeug.exceptions import Forbidden

from app import create_app
from app.blueprints.evaluations import create as create_evaluation
from app.blueprints.evaluations import index as evaluations_index
from app.models import (
    db, AcademicYear, Employee, EmployeeEvaluation, Role, School, User,
)


class EvaluationScopingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('development')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.created = {}

        with self.app.app_context():
            role = Role.query.filter_by(name='school_admin').first()
            self.assertIsNotNone(role, 'seed roles before running evaluation tests')

            base_school = School.query.order_by(School.id).first()
            self.assertIsNotNone(base_school, 'at least one school is required')
            base_year = AcademicYear.query.execution_options(
                bypass_tenant_scope=True
            ).filter_by(school_id=base_school.id, is_current=True).first()
            self.assertIsNotNone(base_year, 'base school needs a current year')

            other_school = School(
                school_name=f'Evaluation School {self.suffix}',
                code=f'EV{self.suffix[:8]}',
                capacity=0,
                is_active=True,
            )
            db.session.add(other_school)
            db.session.flush()

            other_year = AcademicYear(
                school_id=other_school.id,
                name=f'Evaluation Year {self.suffix}',
                start_date=date(2025, 8, 1),
                end_date=date(2026, 6, 30),
                is_current=True,
            )
            db.session.add(other_year)
            db.session.flush()

            user = User(
                username=f'eval_user_{self.suffix}',
                email=f'eval_user_{self.suffix}@example.test',
                full_name='Evaluation School User',
                role_id=role.id,
                school_id=base_school.id,
            )
            user.set_password('Password123')
            db.session.add(user)
            db.session.flush()

            employee_a = Employee(
                employee_id=f'EA{self.suffix[:8]}',
                full_name=f'Evaluation Teacher A {self.suffix}',
                job_title='Teacher',
                school_id=base_school.id,
                base_salary=0,
                status='active',
            )
            employee_b = Employee(
                employee_id=f'EB{self.suffix[:8]}',
                full_name=f'Evaluation Teacher B {self.suffix}',
                job_title='Teacher',
                school_id=other_school.id,
                base_salary=0,
                status='active',
            )
            db.session.add_all([employee_a, employee_b])
            db.session.flush()

            eval_a = EmployeeEvaluation(
                employee_id=employee_a.id,
                evaluator_id=user.id,
                school_id=base_school.id,
                academic_year_id=base_year.id,
                period=f'Baseline A {self.suffix}',
                performance=8,
                discipline=8,
                attendance_score=8,
                final_score=8,
            )
            eval_b = EmployeeEvaluation(
                employee_id=employee_b.id,
                evaluator_id=user.id,
                school_id=other_school.id,
                academic_year_id=other_year.id,
                period=f'Baseline B {self.suffix}',
                performance=7,
                discipline=7,
                attendance_score=7,
                final_score=7,
            )
            db.session.add_all([eval_a, eval_b])
            db.session.commit()

            self.created = {
                'base_school_id': base_school.id,
                'base_year_id': base_year.id,
                'other_school_id': other_school.id,
                'other_year_id': other_year.id,
                'user_id': user.id,
                'employee_a_id': employee_a.id,
                'employee_b_id': employee_b.id,
                'employee_a_name': employee_a.full_name,
                'employee_b_name': employee_b.full_name,
            }

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            ids = self.created

            for employee_key in ('employee_a_id', 'employee_b_id'):
                emp_id = ids.get(employee_key)
                if emp_id:
                    EmployeeEvaluation.query.execution_options(
                        bypass_tenant_scope=True,
                        include_all_years=True,
                    ).filter_by(employee_id=emp_id).delete()

            for model, key in [
                (Employee, 'employee_a_id'),
                (Employee, 'employee_b_id'),
                (User, 'user_id'),
                (AcademicYear, 'other_year_id'),
                (School, 'other_school_id'),
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

    def _post_data(self, employee_id, school_id=None, period=None):
        data = {
            'employee_id': str(employee_id),
            'period': period or f'Route Eval {self.suffix}',
            'performance': '9',
            'discipline': '8',
            'attendance_score': '7',
        }
        if school_id:
            data['school_id'] = str(school_id)
        return data

    def test_normal_user_creates_evaluation_in_own_school(self):
        with self.app.test_request_context(
            '/evaluations/create',
            method='POST',
            data=self._post_data(self.created['employee_a_id']),
        ):
            user = db.session.get(
                User,
                self.created['user_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(user)
            self._run_before_request()

            response = create_evaluation()
            self.assertEqual(response.status_code, 302)

            ev = EmployeeEvaluation.query.execution_options(
                bypass_tenant_scope=True,
                include_all_years=True,
            ).filter_by(
                employee_id=self.created['employee_a_id'],
                period=f'Route Eval {self.suffix}',
            ).one()
            self.assertEqual(ev.school_id, self.created['base_school_id'])
            self.assertEqual(ev.academic_year_id, self.created['base_year_id'])
            logout_user()

    def test_normal_user_cannot_create_evaluation_for_other_school(self):
        with self.app.test_request_context(
            '/evaluations/create',
            method='POST',
            data=self._post_data(self.created['employee_b_id']),
        ):
            user = db.session.get(
                User,
                self.created['user_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(user)
            self._run_before_request()

            with self.assertRaises(Forbidden):
                create_evaluation()

            count = EmployeeEvaluation.query.execution_options(
                bypass_tenant_scope=True,
                include_all_years=True,
            ).filter_by(
                employee_id=self.created['employee_b_id'],
                period=f'Route Eval {self.suffix}',
            ).count()
            self.assertEqual(count, 0)
            logout_user()

    def test_super_admin_creates_evaluation_after_selecting_school(self):
        with self.app.test_request_context(
            '/evaluations/create',
            method='POST',
            data=self._post_data(
                self.created['employee_b_id'],
                school_id=self.created['other_school_id'],
            ),
        ):
            admin = User.query.execution_options(bypass_tenant_scope=True)\
                .filter_by(username='admin').first()
            self.assertIsNotNone(admin)
            login_user(admin)
            self._run_before_request()

            response = create_evaluation()
            self.assertEqual(response.status_code, 302)

            ev = EmployeeEvaluation.query.execution_options(
                bypass_tenant_scope=True,
                include_all_years=True,
            ).filter_by(
                employee_id=self.created['employee_b_id'],
                period=f'Route Eval {self.suffix}',
            ).one()
            self.assertEqual(ev.school_id, self.created['other_school_id'])
            self.assertEqual(ev.academic_year_id, self.created['other_year_id'])
            logout_user()

    def test_normal_user_index_does_not_show_other_school_evaluations(self):
        with self.app.test_request_context('/evaluations/'):
            user = db.session.get(
                User,
                self.created['user_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            login_user(user)
            self._run_before_request()

            captured = {}

            def fake_render(_template, **context):
                captured.update(context)
                return 'ok'

            with patch('app.blueprints.evaluations.render_template',
                       side_effect=fake_render):
                self.assertEqual(evaluations_index(), 'ok')

            names = {ev.employee.full_name for ev in captured['evals'].items}
            self.assertIn(self.created['employee_a_name'], names)
            self.assertNotIn(self.created['employee_b_name'], names)
            logout_user()


if __name__ == '__main__':
    unittest.main()

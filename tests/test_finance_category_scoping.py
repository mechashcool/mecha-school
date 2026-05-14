import unittest
from datetime import date
from unittest.mock import patch
from uuid import uuid4

from flask_login import login_user, logout_user
from werkzeug.exceptions import NotFound

from app import create_app
from app.blueprints.finances import categories as finance_categories
from app.blueprints.finances import delete_category
from app.models import (
    db, AcademicYear, Expense, ExpenseCategory, Revenue, RevenueCategory,
    Role, School, User,
)


class FinanceCategoryScopingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('development')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.created = {}

        with self.app.app_context():
            role = Role.query.filter_by(name='school_admin').first()
            self.assertIsNotNone(role, 'seed roles before running finance tests')

            base_school = School.query.order_by(School.id).first()
            self.assertIsNotNone(base_school, 'at least one school is required')
            base_year = AcademicYear.query.execution_options(
                bypass_tenant_scope=True
            ).filter_by(school_id=base_school.id, is_current=True).first()
            self.assertIsNotNone(base_year, 'base school needs a current year')

            other_school = School(
                school_name=f'Finance School {self.suffix}',
                code=f'FIN{self.suffix[:7]}',
                capacity=0,
                is_active=True,
            )
            db.session.add(other_school)
            db.session.flush()

            other_year = AcademicYear(
                school_id=other_school.id,
                name=f'Finance Year {self.suffix}',
                start_date=date(2025, 8, 1),
                end_date=date(2026, 6, 30),
                is_current=True,
            )
            db.session.add(other_year)
            db.session.flush()

            user = User(
                username=f'fin_user_{self.suffix}',
                email=f'fin_user_{self.suffix}@example.test',
                full_name='Finance School User',
                role_id=role.id,
                school_id=base_school.id,
            )
            user.set_password('Password123')
            db.session.add(user)
            db.session.flush()

            rev_cat_a = RevenueCategory(
                name=f'Shared Revenue {self.suffix}',
                school_id=base_school.id,
            )
            rev_cat_b = RevenueCategory(
                name=f'Shared Revenue {self.suffix}',
                school_id=other_school.id,
            )
            exp_cat_a = ExpenseCategory(
                name=f'Shared Expense {self.suffix}',
                school_id=base_school.id,
            )
            exp_cat_b = ExpenseCategory(
                name=f'Shared Expense {self.suffix}',
                school_id=other_school.id,
            )
            empty_rev_cat_a = RevenueCategory(
                name=f'Empty Revenue {self.suffix}',
                school_id=base_school.id,
            )
            db.session.add_all([
                rev_cat_a, rev_cat_b, exp_cat_a, exp_cat_b, empty_rev_cat_a,
            ])
            db.session.flush()

            revenue = Revenue(
                category_id=rev_cat_a.id,
                school_id=base_school.id,
                academic_year_id=base_year.id,
                amount=100,
                description=f'Finance revenue {self.suffix}',
                date=date.today(),
                recorded_by=user.id,
            )
            expense = Expense(
                category_id=exp_cat_a.id,
                school_id=base_school.id,
                academic_year_id=base_year.id,
                amount=40,
                description=f'Finance expense {self.suffix}',
                date=date.today(),
                created_by=user.id,
                approved_by=user.id,
            )
            db.session.add_all([revenue, expense])
            db.session.commit()

            self.created = {
                'base_school_id': base_school.id,
                'other_school_id': other_school.id,
                'other_year_id': other_year.id,
                'user_id': user.id,
                'rev_cat_a_id': rev_cat_a.id,
                'rev_cat_b_id': rev_cat_b.id,
                'exp_cat_a_id': exp_cat_a.id,
                'exp_cat_b_id': exp_cat_b.id,
                'empty_rev_cat_a_id': empty_rev_cat_a.id,
                'revenue_id': revenue.id,
                'expense_id': expense.id,
            }

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            ids = self.created

            for model, key in [
                (Revenue, 'revenue_id'),
                (Expense, 'expense_id'),
            ]:
                obj = db.session.get(
                    model,
                    ids.get(key),
                    execution_options={'bypass_tenant_scope': True},
                )
                if obj is not None:
                    db.session.delete(obj)

            for model in (RevenueCategory, ExpenseCategory):
                for category in model.query.execution_options(
                    bypass_tenant_scope=True
                ).filter(model.name.like(f'%{self.suffix}%')).all():
                    db.session.delete(category)

            for model, key in [
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

    def _login_school_user(self):
        user = db.session.get(
            User,
            self.created['user_id'],
            execution_options={'bypass_tenant_scope': True},
        )
        login_user(user)
        self._run_before_request()
        return user

    def test_category_page_only_lists_current_school_categories(self):
        with self.app.test_request_context('/finances/categories'):
            self._login_school_user()
            captured = {}

            def fake_render(_template, **context):
                captured.update(context)
                return 'ok'

            with patch('app.blueprints.finances.render_template',
                       side_effect=fake_render):
                self.assertEqual(finance_categories(), 'ok')

            self.assertIn(self.created['rev_cat_a_id'],
                          {cat.id for cat in captured['rev_cats']})
            self.assertNotIn(self.created['rev_cat_b_id'],
                             {cat.id for cat in captured['rev_cats']})
            self.assertIn(self.created['exp_cat_a_id'],
                          {cat.id for cat in captured['exp_cats']})
            self.assertNotIn(self.created['exp_cat_b_id'],
                             {cat.id for cat in captured['exp_cats']})
            logout_user()

    def test_creating_category_assigns_current_school(self):
        name = f'Route Revenue {self.suffix}'
        with self.app.test_request_context(
            '/finances/categories',
            method='POST',
            data={'type': 'revenue', 'name': name},
        ):
            self._login_school_user()

            response = finance_categories()
            self.assertEqual(response.status_code, 302)

            category = RevenueCategory.query.execution_options(
                bypass_tenant_scope=True
            ).filter_by(name=name).one()
            self.assertEqual(category.school_id, self.created['base_school_id'])
            logout_user()

    def test_delete_used_category_shows_friendly_redirect(self):
        with self.app.test_request_context(
            f'/finances/categories/revenue/{self.created["rev_cat_a_id"]}/delete',
            method='POST',
        ):
            self._login_school_user()

            response = delete_category('revenue', self.created['rev_cat_a_id'])
            self.assertEqual(response.status_code, 302)

            category = db.session.get(
                RevenueCategory,
                self.created['rev_cat_a_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            self.assertIsNotNone(category)
            logout_user()

    def test_delete_empty_category_in_own_school_succeeds(self):
        with self.app.test_request_context(
            f'/finances/categories/revenue/{self.created["empty_rev_cat_a_id"]}/delete',
            method='POST',
        ):
            self._login_school_user()

            response = delete_category('revenue',
                                       self.created['empty_rev_cat_a_id'])
            self.assertEqual(response.status_code, 302)

            category = db.session.get(
                RevenueCategory,
                self.created['empty_rev_cat_a_id'],
                execution_options={'bypass_tenant_scope': True},
            )
            self.assertIsNone(category)
            logout_user()

    def test_school_user_cannot_delete_other_school_category(self):
        with self.app.test_request_context(
            f'/finances/categories/revenue/{self.created["rev_cat_b_id"]}/delete',
            method='POST',
        ):
            self._login_school_user()

            with self.assertRaises(NotFound):
                delete_category('revenue', self.created['rev_cat_b_id'])
            logout_user()


if __name__ == '__main__':
    unittest.main()

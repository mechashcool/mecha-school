"""
Tests for the school-scoped, read-only investor_viewer account.

Covers:
  1. Super admin can create an investor account tied to a specific school.
  2. School manager CANNOT assign the investor role via the generic user form.
  3. investor_viewer is not assignable by anyone through _is_role_assignable_by_current_user.
  4. School manager CANNOT edit or delete an investor via the generic admin screens (403).
  5. Investor is redirected away from the shared staff dashboard (staff_required).
  6. Investor is blocked (403) from a permission-gated finance route.
  7. School isolation: under investor-A scope, only School A finance rows are visible.
  8. Mobile API login accepts investor_viewer and returns its role for routing.
"""
import unittest
from datetime import date
from uuid import uuid4

from flask_login import login_user, logout_user
from werkzeug.exceptions import Forbidden

from app import create_app
from app.models import (
    db, AcademicYear, Role, School, User,
    Revenue, RevenueCategory, Expense, ExpenseCategory,
)


def _ensure_role(name, label):
    role = Role.query.filter_by(name=name).first()
    if role is None:
        role = Role(name=name, label=label, is_admin=False)
        db.session.add(role)
        db.session.flush()
    return role


class InvestorAccountTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.created = {}

        with self.app.app_context():
            super_role    = Role.query.filter_by(name='super_admin').first()
            manager_role  = Role.query.filter_by(name='school_admin').first()
            investor_role = _ensure_role('investor_viewer', 'مستثمر - عرض فقط')
            self.assertIsNotNone(super_role,   'seed roles before running tests')
            self.assertIsNotNone(manager_role, 'seed roles before running tests')

            def make_school(tag):
                s = School(school_name=f'Inv {tag} {self.suffix}',
                           code=f'IV{tag}{self.suffix[:6]}', capacity=0, is_active=True)
                db.session.add(s)
                db.session.flush()
                y = AcademicYear(school_id=s.id, name=f'AY {self.suffix}',
                                 start_date=date(2025, 8, 1), end_date=date(2026, 6, 30),
                                 is_current=True)
                db.session.add(y)
                db.session.flush()
                rc = RevenueCategory(name=f'Rev {tag}', school_id=s.id)
                ec = ExpenseCategory(name=f'Exp {tag}', school_id=s.id)
                db.session.add_all([rc, ec])
                db.session.flush()
                return s, y, rc, ec

            school_a, year_a, rc_a, ec_a = make_school('A')
            school_b, year_b, rc_b, ec_b = make_school('B')

            # Finance rows in each school (dated 2025 for a deterministic filter).
            db.session.add(Revenue(category_id=rc_a.id, school_id=school_a.id,
                                   academic_year_id=year_a.id, amount=1000,
                                   date=date(2025, 3, 1)))
            db.session.add(Expense(category_id=ec_a.id, school_id=school_a.id,
                                   academic_year_id=year_a.id, amount=400,
                                   date=date(2025, 3, 2)))
            db.session.add(Revenue(category_id=rc_b.id, school_id=school_b.id,
                                   academic_year_id=year_b.id, amount=9999,
                                   date=date(2025, 3, 1)))

            manager = User(username=f'mgr_inv_{self.suffix}',
                           email=f'mgr_inv_{self.suffix}@ex.test',
                           full_name='Mgr', role_id=manager_role.id,
                           school_id=school_a.id, is_active=True)
            manager.set_password('Password123')

            super_admin = User(username=f'sa_inv_{self.suffix}',
                               email=f'sa_inv_{self.suffix}@ex.test',
                               full_name='SA', role_id=super_role.id,
                               school_id=None, is_active=True)
            super_admin.set_password('Password123')

            investor = User(username=f'inv_{self.suffix}',
                            email=f'inv_{self.suffix}@ex.test',
                            full_name='Investor', role_id=investor_role.id,
                            school_id=school_a.id, is_active=True)
            investor.set_password('Password123')

            db.session.add_all([manager, super_admin, investor])
            db.session.commit()

            self.created = {
                'investor_role_id': investor_role.id,
                'school_a': school_a.id, 'school_b': school_b.id,
                'manager_id': manager.id, 'super_admin_id': super_admin.id,
                'investor_id': investor.id,
                'investor_username': investor.username,
            }

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            ids = self.created
            for sid_key in ('school_a', 'school_b'):
                sid = ids.get(sid_key)
                for model in (Revenue, Expense, RevenueCategory, ExpenseCategory, User,
                              AcademicYear):
                    for obj in (model.query.execution_options(bypass_tenant_scope=True)
                                .filter(model.school_id == sid).all()):
                        db.session.delete(obj)
                db.session.flush()
            # Users created for other schools / any leftover investor accounts
            for uid_key in ('manager_id', 'super_admin_id', 'investor_id'):
                obj = db.session.get(User, ids.get(uid_key),
                                     execution_options={'bypass_tenant_scope': True})
                if obj is not None:
                    db.session.delete(obj)
            db.session.flush()
            for sid_key in ('school_a', 'school_b'):
                s = db.session.get(School, ids.get(sid_key),
                                   execution_options={'bypass_tenant_scope': True})
                if s is not None:
                    db.session.delete(s)
            db.session.commit()
            db.session.remove()

    def _run_before_request(self):
        for fn in self.app.before_request_funcs.get(None, []):
            rv = fn()
            if rv is not None:
                return rv
        return None

    def _login(self, user_id):
        user = db.session.get(User, user_id,
                              execution_options={'bypass_tenant_scope': True})
        login_user(user)
        self._run_before_request()
        return user

    # ── 1. Super admin creates an investor tied to a school ───────────────────
    def test_super_admin_creates_investor(self):
        from app.blueprints.super_admin import create_investor
        ids = self.created
        uname = f'newinv_{self.suffix}'
        with self.app.test_request_context(
            f"/admin/super/schools/{ids['school_b']}/investor/create",
            method='POST',
            data={'full_name': 'New Investor', 'username': uname,
                  'password': 'Password123'},
        ):
            self._login(ids['super_admin_id'])
            # school_b already has no investor in setUp
            resp = create_investor(school_id=ids['school_b'])
            self.assertEqual(resp.status_code, 302)
            created = (User.query.execution_options(bypass_tenant_scope=True)
                       .filter_by(username=uname).first())
            self.assertIsNotNone(created, 'investor account must be created')
            self.assertEqual(created.role.name, 'investor_viewer')
            self.assertEqual(created.school_id, ids['school_b'],
                             'investor must be bound to the managed school')
            logout_user()

    # ── 2. School manager cannot assign investor role via generic user form ───
    def test_manager_cannot_create_investor_via_user_form(self):
        from app.blueprints.admin import create_user
        ids = self.created
        uname = f'mgrinv_{self.suffix}'
        with self.app.test_request_context(
            '/admin/users/create', method='POST',
            data={'username': uname, 'full_name': 'X', 'password': 'Password123',
                  'role_id': str(ids['investor_role_id'])},
        ):
            self._login(ids['manager_id'])
            resp = create_user()
            self.assertEqual(resp.status_code, 302)
            leaked = (User.query.execution_options(bypass_tenant_scope=True)
                      .filter_by(username=uname).first())
            self.assertIsNone(leaked, 'manager must NOT be able to create an investor')
            logout_user()

    # ── 3. investor_viewer is not assignable by anyone ────────────────────────
    def test_investor_role_not_assignable(self):
        from app.blueprints.admin import _is_role_assignable_by_current_user
        ids = self.created
        investor_role = db.session.get(Role, ids['investor_role_id'])
        with self.app.test_request_context('/admin/users/create'):
            self._login(ids['super_admin_id'])
            self.assertFalse(_is_role_assignable_by_current_user(investor_role),
                             'super admin must not assign investor via generic form')
            logout_user()
        with self.app.test_request_context('/admin/users/create'):
            self._login(ids['manager_id'])
            self.assertFalse(_is_role_assignable_by_current_user(investor_role),
                             'manager must not assign investor via generic form')
            logout_user()

    # ── 4. Manager cannot edit/delete investor via generic admin screens ──────
    def test_manager_cannot_edit_or_delete_investor(self):
        from app.blueprints.admin import edit_user, delete_user
        ids = self.created
        with self.app.test_request_context(
                f"/admin/users/{ids['investor_id']}/edit", method='GET'):
            self._login(ids['manager_id'])
            with self.assertRaises(Forbidden):
                edit_user(ids['investor_id'])
            logout_user()
        with self.app.test_request_context(
                f"/admin/users/{ids['investor_id']}/delete", method='POST'):
            self._login(ids['manager_id'])
            with self.assertRaises(Forbidden):
                delete_user(ids['investor_id'])
            logout_user()

    # ── 5. Investor redirected away from the shared staff dashboard ───────────
    def test_investor_blocked_from_staff_dashboard(self):
        from app.blueprints.admin import dashboard
        ids = self.created
        with self.app.test_request_context('/admin/dashboard'):
            self._login(ids['investor_id'])
            resp = dashboard()
            self.assertEqual(resp.status_code, 302)
            self.assertIn('/investor', resp.headers.get('Location', ''))
            logout_user()

    # ── 6. Investor blocked (403) from permission-gated finance route ─────────
    def test_investor_blocked_from_finances(self):
        from app.blueprints.finances import index as finances_index
        ids = self.created
        with self.app.test_request_context('/finances/'):
            self._login(ids['investor_id'])
            with self.assertRaises(Forbidden):
                finances_index()
            logout_user()

    # ── 7. School isolation under investor-A scope ────────────────────────────
    def test_investor_scope_is_school_isolated(self):
        ids = self.created
        with self.app.test_request_context('/investor/?year=2025'):
            self._login(ids['investor_id'])
            # ORM tenant guard restricts every read to School A only.
            visible = Revenue.query.all()
            self.assertTrue(all(r.school_id == ids['school_a'] for r in visible),
                            'investor must only see own-school revenue rows')
            # School B's revenue must be invisible even by direct id lookup.
            leaked = Revenue.query.filter(Revenue.school_id == ids['school_b']).all()
            self.assertEqual(leaked, [], 'no cross-school revenue exposure')
            # The dashboard view itself renders without error.
            from app.blueprints.investor import dashboard as inv_dash
            resp = inv_dash()
            self.assertIsNotNone(resp)
            logout_user()

    # ── 8. Mobile API accepts investor login and returns its role ─────────────
    def test_mobile_login_supports_investor(self):
        ids = self.created
        client = self.app.test_client()
        resp = client.post('/api/mobile/v1/auth/login', json={
            'username': ids['investor_username'], 'password': 'Password123',
        })
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertTrue(body.get('ok'))
        self.assertEqual(body['user']['role'], 'investor_viewer')
        self.assertEqual(body['user']['school_id'], ids['school_a'])

    # ── JWT helpers for the mobile revenues/expenses filter tests ──────────────
    def _token_for(self, user_id):
        with self.app.app_context():
            from app.blueprints.mobile_api.utils import encode_token
            user = db.session.get(User, user_id,
                                  execution_options={'bypass_tenant_scope': True})
            return encode_token(user)

    def _api(self, path, token):
        client = self.app.test_client()
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        return client.get(f'/api/mobile/v1{path}', headers=headers)

    def _seed_extra_finance_rows(self):
        """A second category + row per school so filters have something to
        discriminate on beyond the single row created in setUp()."""
        ids = self.created
        with self.app.app_context():
            year_a = (AcademicYear.query.execution_options(bypass_tenant_scope=True)
                      .filter_by(school_id=ids['school_a']).first())
            rc_extra = RevenueCategory(name=f'Rev Extra {self.suffix}',
                                       school_id=ids['school_a'])
            ec_extra = ExpenseCategory(name=f'Exp Extra {self.suffix}',
                                       school_id=ids['school_a'])
            db.session.add_all([rc_extra, ec_extra])
            db.session.flush()
            rev_extra = Revenue(category_id=rc_extra.id, school_id=ids['school_a'],
                                academic_year_id=year_a.id, amount=250,
                                date=date(2025, 6, 15))
            exp_extra = Expense(category_id=ec_extra.id, school_id=ids['school_a'],
                                academic_year_id=year_a.id, amount=75,
                                date=date(2025, 6, 20))
            db.session.add_all([rev_extra, exp_extra])
            db.session.commit()
            return {'rc_extra_id': rc_extra.id, 'ec_extra_id': ec_extra.id}

    # ── 9. Revenues: category_id / date_from / date_to filters ────────────────
    def test_mobile_revenues_filters(self):
        ids = self.created
        extra = self._seed_extra_finance_rows()
        token = self._token_for(ids['investor_id'])

        # category_id filter — only the extra row (rc_extra) must come back.
        resp = self._api(f"/investor/revenues?year=2025&category_id={extra['rc_extra_id']}", token)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual([r['id'] for r in body['items']],
                         [r['id'] for r in body['items'] if r['category_id'] == extra['rc_extra_id']])
        self.assertTrue(len(body['items']) >= 1)
        self.assertIn('filter_options', body)
        cat_names = {c['name'] for c in body['filter_options']['categories']}
        self.assertIn(f"Rev Extra {self.suffix}", cat_names)

        # date range filter — the March row (2025-03-01) must be excluded.
        resp = self._api('/investor/revenues?date_from=2025-06-01&date_to=2025-06-30', token)
        body = resp.get_json()
        self.assertTrue(all(r['date'] >= '2025-06-01' and r['date'] <= '2025-06-30'
                            for r in body['items']))

        # combined filters + a range with no matches -> 200, items: [].
        resp = self._api(
            f"/investor/revenues?category_id={extra['rc_extra_id']}"
            "&date_from=2099-01-01&date_to=2099-01-31", token)
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body['items'], [])
        self.assertIn('filter_options', body)

        # invalid category_id -> clean 400 JSON, not a 500/HTML error.
        resp = self._api('/investor/revenues?category_id=notanumber', token)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()['ok'])

        # invalid date_from -> clean 400 JSON.
        resp = self._api('/investor/revenues?date_from=07-01-2026', token)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()['ok'])

    # ── 10. Expenses: category_id / date_from / date_to filters ───────────────
    def test_mobile_expenses_filters(self):
        ids = self.created
        extra = self._seed_extra_finance_rows()
        token = self._token_for(ids['investor_id'])

        resp = self._api(f"/investor/expenses?year=2025&category_id={extra['ec_extra_id']}", token)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertTrue(all(e['category_id'] == extra['ec_extra_id'] for e in body['items']))
        self.assertIn('filter_options', body)

        resp = self._api('/investor/expenses?date_from=2025-06-01&date_to=2025-06-30', token)
        body = resp.get_json()
        self.assertTrue(all('2025-06-01' <= e['date'] <= '2025-06-30' for e in body['items']))

        resp = self._api('/investor/expenses?category_id=abc', token)
        self.assertEqual(resp.status_code, 400)

        resp = self._api('/investor/expenses?date_to=not-a-date', token)
        self.assertEqual(resp.status_code, 400)

    # ── 11. Cross-school category_id does not leak School B rows ──────────────
    def test_mobile_revenues_cross_school_category_id_is_isolated(self):
        ids = self.created
        with self.app.app_context():
            ec_b = (ExpenseCategory.query.execution_options(bypass_tenant_scope=True)
                    .filter_by(school_id=ids['school_b']).first())
            rc_b = (RevenueCategory.query.execution_options(bypass_tenant_scope=True)
                    .filter_by(school_id=ids['school_b']).first())
            rc_b_id = rc_b.id
        token = self._token_for(ids['investor_id'])
        resp = self._api(f"/investor/revenues?year=2025&category_id={rc_b_id}", token)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        # School A investor scoped to school A; School B's category id must not
        # surface School B's revenue row even though the id itself is valid.
        self.assertEqual(body['items'], [])


if __name__ == '__main__':
    unittest.main()

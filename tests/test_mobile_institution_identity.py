"""
Mobile institution identity — school.institution_type / school.is_institute.

The mobile ``school`` block (POST /auth/login and GET /me) carries two ADDITIVE
keys so the app can route on ``role + school.is_institute``:

  * institution_type — exactly 'school' or 'institute'
  * is_institute     — a JSON boolean

Both come from the authenticated user's server-side School row through
School.is_institute (NULL / '' / 'school' / unknown legacy value -> school).
Nothing else changes: every pre-existing key and value type, the refresh
response and the JWT claims stay exactly as they were.

Requires an isolated, approved test database (tests/conftest.py guard).
"""
import unittest
from datetime import date
from uuid import uuid4

import jwt as pyjwt

from app import create_app
from app.blueprints.mobile_api.utils import decode_token, encode_token
from app.models import (db, AcademicYear, AuditLog, Employee, Role, School,
                        Student, User, parent_students)
from app.utils import ttl_cache

PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True}
LOGIN = '/api/mobile/v1/auth/login'
ME = '/api/mobile/v1/me'
REFRESH = '/api/mobile/v1/auth/refresh'

# Pre-existing contracts. The identity keys are the ONLY additions.
LEGACY_SCHOOL_KEYS = {'id', 'name', 'name_ar', 'logo', 'primary_color',
                      'currency', 'currency_code', 'phone', 'email', 'address'}
IDENTITY_KEYS = {'institution_type', 'is_institute'}
LOGIN_TOP_KEYS = {'ok', 'access_token', 'refresh_token', 'token_type',
                  'expires_in', 'user', 'school', 'children', 'employee'}
LOGIN_USER_KEYS = {'id', 'name', 'username', 'email', 'phone', 'avatar',
                   'role', 'locale', 'school_id'}
ME_TOP_KEYS = {'ok', 'user', 'school', 'children'}
REFRESH_KEYS = {'ok', 'access_token', 'token_type', 'expires_in'}
JWT_CLAIMS = {'sub', 'school_id', 'role', 'type', 'iat', 'exp'}


class MobileInstitutionIdentityTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['RATELIMIT_ENABLED'] = False
        with cls.app.app_context():
            cls.role_ids = {n: Role.query.filter_by(name=n).first().id
                            for n in ('super_admin', 'school_admin', 'parent', 'teacher')}

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        # Every test starts cold; tests that exercise the cache enable it.
        self.app.config['BACKEND_CACHE_ENABLED'] = False
        ttl_cache.clear()
        with self.app.app_context():
            self._school('s', None)            # legacy school: institution_type NULL
            self._school('i', 'institute')     # explicit institute
            sa = User(username=f'mii_sa_{self.sfx}', email=f'mii_sa_{self.sfx}@t.test',
                      full_name='SA', role_id=self.role_ids['super_admin'],
                      school_id=None, is_active=True)
            sa.set_password(PASSWORD)
            db.session.add(sa)
            db.session.flush()
            self.ids['super_admin'] = sa.id
            db.session.commit()

    def _school(self, key, institution_type):
        s = self.sfx
        school = School(school_name=f'MII {key} {s}', school_name_ar=f'م {key}',
                        code=f'MI{key}{s}'[:20], capacity=0, is_active=True,
                        phone='0770000000', email=f'mii{key}{s}@t.test',
                        address='Baghdad', institution_type=institution_type)
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'Y{key}{s}', is_current=True,
                            start_date=date(2026, 8, 1), end_date=date(2027, 6, 30))
        db.session.add(year)
        db.session.flush()

        def user(label, role):
            u = User(username=f'mii{label}{key}_{s}', email=f'mii{label}{key}_{s}@t.test',
                     full_name=f'{label} {key}', role_id=self.role_ids[role],
                     school_id=school.id, is_active=True)
            u.set_password(PASSWORD)
            db.session.add(u)
            db.session.flush()
            return u

        parent, teacher, admin = user('par', 'parent'), user('t', 'teacher'), user('adm', 'school_admin')
        db.session.add(Employee(school_id=school.id, employee_id=f'MI{key}{s}',
                                full_name=f'T {key}', base_salary=0, status='active',
                                user_id=teacher.id))
        # Sectionless student — the shape an institute student has.
        student = Student(student_id=f'MI{key}-{s}', full_name=f'Stu {key}',
                          school_id=school.id, academic_year_id=year.id,
                          section_id=None, status='active')
        db.session.add(student)
        db.session.flush()
        db.session.execute(parent_students.insert().values(
            user_id=parent.id, student_id=student.id, relation='guardian'))
        self.ids.update({f'school_{key}': school.id,
                         f'parent_{key}': parent.username, f'parent_id_{key}': parent.id,
                         f'teacher_{key}': teacher.username, f'teacher_id_{key}': teacher.id,
                         f'admin_{key}': admin.username})

    def tearDown(self):
        ttl_cache.clear()
        with self.app.app_context():
            db.session.rollback()
            AuditLog.query.execution_options(**OPTS).filter_by(
                user_id=self.ids['super_admin']).delete(synchronize_session=False)
            for key in ('s', 'i'):
                sid = self.ids[f'school_{key}']
                uids = [u.id for u in User.query.execution_options(**OPTS)
                        .filter_by(school_id=sid).all()]
                if uids:
                    AuditLog.query.execution_options(**OPTS).filter(
                        AuditLog.user_id.in_(uids)).delete(synchronize_session=False)
                    db.session.execute(parent_students.delete().where(
                        parent_students.c.user_id.in_(uids)))
                for model in (AuditLog, Student, Employee, User, AcademicYear):
                    model.query.execution_options(**OPTS).filter_by(
                        school_id=sid).delete(synchronize_session=False)
                School.query.filter_by(id=sid).delete(synchronize_session=False)
            User.query.execution_options(**OPTS).filter_by(
                id=self.ids['super_admin']).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _login(self, username, **extra):
        return self.app.test_client().post(
            LOGIN, json={'username': username, 'password': PASSWORD, **extra})

    def _ok_login(self, username):
        resp = self._login(username)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()

    def _me(self, token, **params):
        return self.app.test_client().get(
            ME, query_string=params, headers={'Authorization': f'Bearer {token}'})

    def _set_type(self, key, value):
        with self.app.app_context():
            School.query.filter_by(id=self.ids[f'school_{key}']).update(
                {'institution_type': value}, synchronize_session=False)
            db.session.commit()

    def _assert_identity(self, school_block, institution_type, is_institute):
        self.assertEqual(school_block['institution_type'], institution_type)
        self.assertIs(school_block['is_institute'], is_institute)   # real JSON bool

    # ── 1-4. login per role x institution ────────────────────────────────────

    def test_01_school_parent_login(self):
        body = self._ok_login(self.ids['parent_s'])
        self.assertEqual(body['user']['role'], 'parent')
        self.assertEqual(body['school']['id'], self.ids['school_s'])
        self._assert_identity(body['school'], 'school', False)

    def test_02_institute_parent_login(self):
        body = self._ok_login(self.ids['parent_i'])
        self.assertEqual(body['user']['role'], 'parent')
        self.assertEqual(body['school']['id'], self.ids['school_i'])
        self._assert_identity(body['school'], 'institute', True)
        # The sectionless child still appears exactly as before.
        self.assertEqual(len(body['children']), 1)
        self.assertIsNone(body['children'][0]['section'])

    def test_03_school_teacher_login(self):
        body = self._ok_login(self.ids['teacher_s'])
        self.assertEqual(body['user']['role'], 'teacher')
        self.assertIsNotNone(body['employee'])
        self._assert_identity(body['school'], 'school', False)

    def test_04_institute_teacher_login(self):
        body = self._ok_login(self.ids['teacher_i'])
        self.assertEqual(body['user']['role'], 'teacher')   # no new role
        self.assertIsNotNone(body['employee'])
        self._assert_identity(body['school'], 'institute', True)

    # ── 5. /me agrees with login ─────────────────────────────────────────────

    def test_05_me_matches_login(self):
        for account in ('parent_s', 'parent_i', 'teacher_s', 'teacher_i'):
            with self.subTest(account=account):
                body = self._ok_login(self.ids[account])
                me = self._me(body['access_token'])
                self.assertEqual(me.status_code, 200)
                me_school = me.get_json()['school']
                for k in IDENTITY_KEYS:
                    self.assertEqual(me_school[k], body['school'][k])
                # The whole shared block agrees, not just the new keys.
                self.assertEqual(me_school, body['school'])

    # ── 6-7. normalization follows School.is_institute ───────────────────────

    def test_06_07_normalization(self):
        cases = [(None, 'school', False), ('', 'school', False),
                 ('school', 'school', False), ('college', 'school', False),
                 ('institute', 'institute', True),
                 # School.is_institute strips and lower-cases.
                 (' Institute ', 'institute', True)]
        for stored, expected_type, expected_flag in cases:
            with self.subTest(stored=stored):
                self._set_type('s', stored)
                body = self._ok_login(self.ids['parent_s'])
                self._assert_identity(body['school'], expected_type, expected_flag)
                me = self._me(body['access_token']).get_json()
                self._assert_identity(me['school'], expected_type, expected_flag)

    # ── 8-9. existing keys and value types unchanged ─────────────────────────

    def test_08_09_existing_keys_and_types(self):
        for account in ('parent_s', 'teacher_i'):
            with self.subTest(account=account):
                body = self._ok_login(self.ids[account])
                self.assertEqual(set(body), LOGIN_TOP_KEYS)
                self.assertEqual(set(body['user']), LOGIN_USER_KEYS)
                self.assertEqual(set(body['school']), LEGACY_SCHOOL_KEYS | IDENTITY_KEYS)
                sch = body['school']
                self.assertIsInstance(sch['id'], int)
                for k in ('name', 'name_ar', 'primary_color', 'currency',
                          'currency_code', 'phone', 'email', 'address'):
                    self.assertIsInstance(sch[k], str, k)
                self.assertIsNone(sch['logo'])
                self.assertEqual(body['token_type'], 'Bearer')
                self.assertEqual(body['expires_in'], 86400)
                self.assertIsInstance(body['user']['school_id'], int)

                me = self._me(body['access_token']).get_json()
                self.assertEqual(set(me), ME_TOP_KEYS)
                self.assertEqual(set(me['user']), LOGIN_USER_KEYS | {'last_login'})
                self.assertEqual(set(me['school']), LEGACY_SCHOOL_KEYS | IDENTITY_KEYS)

    # ── 10-11. refresh response and JWT claims unchanged ─────────────────────

    def test_10_11_refresh_and_jwt_unchanged(self):
        body = self._ok_login(self.ids['parent_i'])
        with self.app.app_context():
            for tok, typ in ((body['access_token'], 'access'),
                             (body['refresh_token'], 'refresh')):
                claims = decode_token(tok)
                self.assertEqual(set(claims), JWT_CLAIMS)
                self.assertEqual(claims['type'], typ)
                self.assertNotIn('institution_type', claims)
                self.assertNotIn('is_institute', claims)

        client = self.app.test_client()
        resp = client.post(REFRESH, headers={'Authorization': f"Bearer {body['refresh_token']}"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.get_json()), REFRESH_KEYS)
        with self.app.app_context():
            self.assertEqual(set(decode_token(resp.get_json()['access_token'])), JWT_CLAIMS)
        # An access token is still refused at the refresh endpoint.
        wrong = client.post(REFRESH, headers={'Authorization': f"Bearer {body['access_token']}"})
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(wrong.get_json()['error'], 'wrong_token_type')

    # ── 12. inactive / unauthorized accounts behave exactly as before ────────

    def test_12_inactive_and_unauthorized_unchanged(self):
        bad = self.app.test_client().post(
            LOGIN, json={'username': self.ids['parent_i'], 'password': 'wrong-pass'})
        self.assertEqual(bad.status_code, 401)
        self.assertEqual(bad.get_json(), {'ok': False, 'error': 'invalid_credentials'})

        admin = self._login(self.ids['admin_i'])
        self.assertEqual(admin.status_code, 403)
        self.assertTrue(admin.get_json()['error'].startswith('role_not_supported'))
        self.assertNotIn('school', admin.get_json())

        token = self._ok_login(self.ids['parent_i'])['access_token']
        with self.app.app_context():
            User.query.filter_by(id=self.ids['parent_id_i']).update(
                {'is_active': False}, synchronize_session=False)
            db.session.commit()
        disabled = self._login(self.ids['parent_i'])
        self.assertEqual(disabled.status_code, 401)
        self.assertEqual(disabled.get_json(), {'ok': False, 'error': 'account_disabled'})
        me = self._me(token)
        self.assertEqual(me.status_code, 401)
        self.assertEqual(me.get_json(), {'ok': False, 'error': 'user_inactive'})

        self.assertEqual(self.app.test_client().get(ME).status_code, 401)

    # ── 13. cache participates and the school-edit route invalidates it ──────

    def test_13_cache_invalidated_by_school_edit_route(self):
        self.app.config['BACKEND_CACHE_ENABLED'] = True
        ttl_cache.clear()
        token = self._ok_login(self.ids['parent_s'])['access_token']
        self._assert_identity(self._me(token).get_json()['school'], 'school', False)

        # A raw DB change (no invalidation) is NOT seen: /me is served from cache.
        self._set_type('s', 'institute')
        self._assert_identity(self._me(token).get_json()['school'], 'school', False)
        self._set_type('s', None)

        # The real Super Admin edit route (the only writer of institution_type
        # after creation) commits and invalidates in the same request.
        web = self.app.test_client()
        with self.app.app_context():
            sa = db.session.get(User, self.ids['super_admin'], execution_options=OPTS)
            sa_username = sa.username
            school = db.session.get(School, self.ids['school_s'], execution_options=OPTS)
            form = {'school_name': school.school_name, 'school_name_ar': school.school_name_ar,
                    'code': school.code, 'capacity': '0', 'price_per_student': '0',
                    'phone': school.phone, 'email': school.email, 'address': school.address,
                    'is_active': 'on', 'institution_type': 'institute'}
        login = web.post('/auth/login', data={'username': sa_username, 'password': PASSWORD})
        self.assertEqual(login.status_code, 302)
        resp = web.post(f"/schools/{self.ids['school_s']}/edit", data=form)
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True)[:500])
        with self.app.app_context():
            self.assertEqual(db.session.get(School, self.ids['school_s'],
                                            execution_options=OPTS).institution_type,
                             'institute')
        self._assert_identity(self._me(token).get_json()['school'], 'institute', True)

    # ── 14. server-side authority and tenant isolation ───────────────────────

    def test_14_server_authority_and_tenant_isolation(self):
        # Client-supplied identity is ignored on login and on /me.
        body = self._login(self.ids['parent_s'], institution_type='institute',
                           is_institute=True, school_id=self.ids['school_i']).get_json()
        self.assertEqual(body['school']['id'], self.ids['school_s'])
        self._assert_identity(body['school'], 'school', False)
        me = self._me(body['access_token'], institution_type='institute',
                      is_institute='true', school_id=self.ids['school_i']).get_json()
        self.assertEqual(me['school']['id'], self.ids['school_s'])
        self._assert_identity(me['school'], 'school', False)

        # A correctly signed token whose school_id claim names ANOTHER school
        # still resolves the user's own School row, never the claim.
        with self.app.app_context():
            user = db.session.get(User, self.ids['parent_id_s'], execution_options=OPTS)
            claims = decode_token(encode_token(user))
            claims['school_id'] = self.ids['school_i']
            forged = pyjwt.encode(claims, self.app.config.get('JWT_SECRET_KEY')
                                  or self.app.config['SECRET_KEY'], algorithm='HS256')
        me = self._me(forged).get_json()
        self.assertEqual(me['school']['id'], self.ids['school_s'])
        self._assert_identity(me['school'], 'school', False)

        # With the cache on, each school keeps its own entry.
        self.app.config['BACKEND_CACHE_ENABLED'] = True
        ttl_cache.clear()
        tok_s = self._ok_login(self.ids['parent_s'])['access_token']
        tok_i = self._ok_login(self.ids['teacher_i'])['access_token']
        for _ in range(2):
            s_block = self._me(tok_s).get_json()['school']
            i_block = self._me(tok_i).get_json()['school']
            self.assertEqual(s_block['id'], self.ids['school_s'])
            self.assertEqual(i_block['id'], self.ids['school_i'])
            self._assert_identity(s_block, 'school', False)
            self._assert_identity(i_block, 'institute', True)


if __name__ == '__main__':
    unittest.main()

"""
Investor employee attendance — HTTP-level tests.

  GET /api/mobile/v1/investor/employees/attendance
  GET /api/mobile/v1/investor/employees/<id>/attendance

Read-only. The investor sees only its own school's employees and their
EmployeeAttendance rows; nothing a client sends can widen that scope.

Fixture (D = 2026-09-29, a Tuesday; school A has Friday as employee day off):
  school A — current year YA (2026-08-01..), older year YA0
    ea1  Ali     active   D present 07:42-14:10 (aiface), D-1 late 08:15 (manual,
                          no check-out), D-2 on_leave (leave), 2026-05-10 present (YA0)
    ea2  Basim   active   D check-in 08:05 only
    ea3  Zaid    active   no attendance row at all
    ex   Xavier  INACTIVE D present — never in the list
    p00..p44     active   45 employees with a D row (pagination / query count)
  school B — eb1 "Ali B" active, D present
  users: IA investor(A), IB investor(B), IN investor without school,
         PA parent(A), TA teacher(A)

Requires an isolated, approved test database (tests/conftest.py guard).
"""
import unittest
from datetime import date, time, timedelta
from uuid import uuid4

from sqlalchemy import event

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (db, AcademicYear, Employee, EmployeeAttendance, Role,
                        School, User)

OPTS = {'bypass_tenant_scope': True}
API = '/api/mobile/v1/investor/employees'
D = date(2026, 9, 29)          # Tuesday
FRIDAY = date(2026, 10, 2)     # school A employee weekly day off

EMP_KEYS = {'id', 'employee_id', 'name', 'job_title', 'photo', 'attendance'}
ATT_KEYS = {'id', 'date', 'status', 'check_in', 'check_out', 'source'}
PAGE_KEYS = {'limit', 'offset', 'has_more', 'next_offset'}
N_PAGED = 45


class InvestorEmployeeAttendanceTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['RATELIMIT_ENABLED'] = False
        with cls.app.app_context():
            cls.role_ids = {n: Role.query.filter_by(name=n).first().id
                            for n in ('parent', 'teacher', 'investor_viewer')}

    # ── fixture ───────────────────────────────────────────────────────────────

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        self.school_ids = []
        self.user_ids = []
        with self.app.app_context():
            self._school_a()
            self._school_b()
            self._user(None, 'in', 'investor_viewer')
            db.session.commit()

    def _add(self, obj):
        db.session.add(obj)
        db.session.flush()
        return obj

    def _school(self, key):
        school = self._add(School(school_name=f'IEA {key} {self.sfx}',
                                  code=f'IE{key}{self.sfx}'[:20], capacity=0,
                                  is_active=True, weekly_off_days='4'))
        self.school_ids.append(school.id)
        return school

    def _year(self, school, key, current):
        start = date(2026, 8, 1) if current else date(2025, 8, 1)
        return self._add(AcademicYear(school_id=school.id, name=f'Y{key}{self.sfx}',
                                      is_current=current, start_date=start,
                                      end_date=start.replace(year=start.year + 1)
                                      - timedelta(days=1)))

    def _user(self, school, label, role):
        u = User(username=f'iea{label}_{self.sfx}', email=f'iea{label}_{self.sfx}@t.test',
                 full_name=label, role_id=self.role_ids[role],
                 school_id=school.id if school else None, is_active=True)
        u.set_password('Test1234!')
        self._add(u)
        self.ids[label] = u.id
        self.user_ids.append(u.id)
        return u

    def _emp(self, school, label, name, status='active', job='Teacher'):
        emp = self._add(Employee(school_id=school.id, employee_id=f'{label}-{self.sfx}',
                                 full_name=f'{name} {self.sfx}', job_title=job,
                                 base_salary=1234567, phone='07700000000',
                                 email=f'iea{label}_{self.sfx}@emp.test',
                                 status=status))
        self.ids[label] = emp.id
        return emp

    def _att(self, emp, year, day, status, cin=None, cout=None, source='manual',
             key=None):
        rec = self._add(EmployeeAttendance(
            employee_id=emp.id, school_id=emp.school_id, academic_year_id=year.id,
            date=day, status=status, check_in=cin, check_out=cout, source=source,
            notes='AI Face 2026-09-29 07:42:00' if source == 'aiface' else 'private'))
        if key:
            self.ids[key] = rec.id
        return rec

    def _school_a(self):
        school = self._school('a')
        ya0 = self._year(school, 'a0', current=False)
        ya = self._year(school, 'a', current=True)
        self.ids['school_a'] = school.id
        ea1 = self._emp(school, 'ea1', 'Ali')
        ea2 = self._emp(school, 'ea2', 'Basim', job=None)
        self._emp(school, 'ea3', 'Zaid')
        ex = self._emp(school, 'ex', 'Xavier', status='inactive')
        self._att(ea1, ya, D, 'present', time(7, 42), time(14, 10), 'aiface', 'a1_d')
        self._att(ea1, ya, D - timedelta(days=1), 'late', time(8, 15), key='a1_d1')
        self._att(ea1, ya, D - timedelta(days=2), 'on_leave', source='leave', key='a1_d2')
        self._att(ea1, ya0, date(2026, 5, 10), 'present', time(7, 30), time(13, 0),
                  key='a1_old')
        self._att(ea2, ya, D, 'present', time(8, 5), key='a2_d')
        self._att(ex, ya, D, 'present', time(7, 0), time(12, 0))
        for i in range(N_PAGED):
            p = self._emp(school, f'p{i:02d}', f'Pag {i:02d}')
            self._att(p, ya, D, 'present', time(7, 0))
        self._user(school, 'ia', 'investor_viewer')
        self._user(school, 'pa', 'parent')
        self._user(school, 'ta', 'teacher')

    def _school_b(self):
        school = self._school('b')
        yb = self._year(school, 'b', current=True)
        self.ids['school_b'] = school.id
        eb1 = self._emp(school, 'eb1', 'Ali B')
        self._att(eb1, yb, D, 'present', time(9, 0), time(15, 0), key='b1_d')
        self._user(school, 'ib', 'investor_viewer')

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for sid in self.school_ids:
                def q(model):
                    return model.query.execution_options(**OPTS).filter_by(school_id=sid)
                for model in (EmployeeAttendance, Employee):
                    q(model).delete(synchronize_session=False)
            User.query.execution_options(**OPTS).filter(
                User.id.in_(self.user_ids)).delete(synchronize_session=False)
            for sid in self.school_ids:
                AcademicYear.query.execution_options(**OPTS).filter_by(
                    school_id=sid).delete(synchronize_session=False)
                School.query.filter_by(id=sid).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _token(self, user_key):
        with self.app.app_context():
            return encode_token(db.session.get(User, self.ids[user_key],
                                               execution_options=OPTS))

    def _get(self, user_key, path, **params):
        return self.app.test_client().get(
            path, query_string=params,
            headers={'Authorization': f'Bearer {self._token(user_key)}'})

    def _ok(self, user_key, path, **params):
        resp = self._get(user_key, path, **params)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:300])
        body = resp.get_json()
        self.assertTrue(body['ok'])
        return body

    def _list(self, user_key='ia', **params):
        params.setdefault('date', D.isoformat())
        return self._ok(user_key, f'{API}/attendance', **params)

    def _all_items(self, user_key='ia', **params):
        items, offset = [], 0
        while True:
            body = self._list(user_key, limit=100, offset=offset, **params)
            items += body['items']
            if not body['pagination']['has_more']:
                return items
            offset = body['pagination']['next_offset']

    def _hist(self, emp_key, user_key='ia', **params):
        return self._ok(user_key, f"{API}/{self.ids[emp_key]}/attendance", **params)

    def _n(self, name):
        return f'{name} {self.sfx}'

    def _statements(self, user_key, path, **params):
        """Run one request; return (status, [statement texts]). Text only."""
        token = self._token(user_key)
        with self.app.app_context():
            engine = db.engine
        seen = []

        def capture(conn, cursor, statement, *args):
            seen.append(statement)

        event.listen(engine, 'before_cursor_execute', capture)
        try:
            resp = self.app.test_client().get(
                path, query_string=params,
                headers={'Authorization': f'Bearer {token}'})
        finally:
            event.remove(engine, 'before_cursor_execute', capture)
        return resp.status_code, seen

    # ── 1. authentication / role ─────────────────────────────────────────────

    def test_01_unauthenticated_and_wrong_roles_rejected(self):
        client = self.app.test_client()
        hist = f"{API}/{self.ids['ea1']}/attendance"
        for path in (f'{API}/attendance', hist):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 401)
                self.assertEqual(client.get(path, headers={
                    'Authorization': 'Bearer not-a-token'}).status_code, 401)
                for role_user in ('pa', 'ta'):
                    resp = self._get(role_user, path)
                    self.assertEqual(resp.status_code, 403)
                    self.assertEqual(resp.get_json(), {'ok': False, 'error': 'forbidden'})
                # An investor account without a school fails closed.
                resp = self._get('in', path)
                self.assertEqual(resp.status_code, 403)
                self.assertEqual(resp.get_json(), {'ok': False, 'error': 'forbidden'})

    # ── 2. list content ──────────────────────────────────────────────────────

    def test_02_list_contract_and_attendance_states(self):
        body = self._list(limit=100)
        self.assertEqual(set(body), {'ok', 'school', 'date', 'is_day_off', 'items',
                                     'pagination'})
        self.assertEqual(body['school'], {'id': self.ids['school_a'],
                                          'name': f'IEA a {self.sfx}',
                                          'timezone': 'Asia/Baghdad'})
        self.assertEqual(body['date'], D.isoformat())
        self.assertFalse(body['is_day_off'])
        by_name = {i['name']: i for i in body['items']}
        for item in body['items']:
            self.assertEqual(set(item), EMP_KEYS)

        ali = by_name[self._n('Ali')]
        self.assertEqual(ali['id'], self.ids['ea1'])
        self.assertEqual(ali['employee_id'], f'ea1-{self.sfx}')
        self.assertEqual(ali['job_title'], 'Teacher')
        self.assertIsNone(ali['photo'])
        # check-in + check-out
        self.assertEqual(ali['attendance'], {
            'id': self.ids['a1_d'], 'date': D.isoformat(), 'status': 'present',
            'check_in': '07:42', 'check_out': '14:10', 'source': 'aiface'})
        # check-in only
        basim = by_name[self._n('Basim')]
        self.assertIsNone(basim['job_title'])
        self.assertEqual(basim['attendance'], {
            'id': self.ids['a2_d'], 'date': D.isoformat(), 'status': 'present',
            'check_in': '08:05', 'check_out': None, 'source': 'manual'})
        # no attendance row → still listed, attendance null
        self.assertIsNone(by_name[self._n('Zaid')]['attendance'])
        # inactive employee excluded
        self.assertNotIn(self._n('Xavier'), by_name)
        # ordered by name; 3 named + 45 paged
        names = [i['name'] for i in body['items']]
        self.assertEqual(names, sorted(names))
        self.assertEqual(len(names), 3 + N_PAGED)

    def test_03_date_param_default_and_day_off(self):
        # Another date: Ali late without check-out; Basim/Zaid have no row.
        body = self._list(date=(D - timedelta(days=1)).isoformat(), limit=100)
        by_name = {i['name']: i for i in body['items']}
        self.assertEqual(by_name[self._n('Ali')]['attendance']['status'], 'late')
        self.assertIsNone(by_name[self._n('Ali')]['attendance']['check_out'])
        self.assertIsNone(by_name[self._n('Basim')]['attendance'])
        on_leave = self._list(date=(D - timedelta(days=2)).isoformat(), limit=100)
        self.assertEqual({i['name']: i for i in on_leave['items']}
                         [self._n('Ali')]['attendance']['status'], 'on_leave')
        # A date in the older academic year is still visible (date-keyed rows).
        old = self._list(date='2026-05-10', limit=100)
        self.assertEqual({i['name']: i for i in old['items']}
                         [self._n('Ali')]['attendance']['id'], self.ids['a1_old'])
        # Employee weekly day off.
        fri = self._list(date=FRIDAY.isoformat(), limit=1)
        self.assertTrue(fri['is_day_off'])
        self.assertIsNone(fri['items'][0]['attendance'])
        # Default date = school-local today (a well-formed ISO date).
        resp = self._ok('ia', f'{API}/attendance', limit=1)
        date.fromisoformat(resp['date'])

    # ── 3. isolation ─────────────────────────────────────────────────────────

    def test_04_school_isolation_and_forged_params(self):
        a_items = self._all_items()
        a_ids = {i['id'] for i in a_items}
        self.assertNotIn(self.ids['eb1'], a_ids)
        b = self._list('ib', limit=100)
        self.assertEqual([(i['id'], i['attendance']['id']) for i in b['items']],
                         [(self.ids['eb1'], self.ids['b1_d'])])
        self.assertEqual(b['school']['id'], self.ids['school_b'])
        # Client-supplied scope never widens access, for either investor.
        forged = dict(school_id=self.ids['school_a'], employee_id=self.ids['ea1'],
                      academic_year_id=1, user_id=self.ids['ia'])
        self.assertEqual(self._list('ib', limit=100, **forged), b)
        forged_b = dict(school_id=self.ids['school_b'], employee_id=self.ids['eb1'])
        self.assertEqual({i['id'] for i in self._all_items(**forged_b)}, a_ids)
        # Search never reaches another school ("Ali" matches Ali and Ali B).
        found = self._list(search='Ali', limit=100)
        self.assertEqual([i['id'] for i in found['items']], [self.ids['ea1']])
        found_b = self._list('ib', search='Ali', limit=100)
        self.assertEqual([i['id'] for i in found_b['items']], [self.ids['eb1']])

    def test_05_history_isolation_by_employee_id(self):
        # Another school's employee id → 404, identical to a nonexistent id.
        cross = self._get('ia', f"{API}/{self.ids['eb1']}/attendance")
        missing = self._get('ia', f'{API}/999999999/attendance')
        for resp in (cross, missing):
            self.assertEqual(resp.status_code, 404)
            self.assertEqual(resp.get_json(), {'ok': False, 'error': 'employee_not_found'})
        self.assertEqual(self._get('ib', f"{API}/{self.ids['ea1']}/attendance")
                         .status_code, 404)
        forged = self._get('ia', f"{API}/{self.ids['eb1']}/attendance",
                           school_id=self.ids['school_b'])
        self.assertEqual(forged.status_code, 404)
        # Own employee works for the owning investor.
        self.assertEqual(self._hist('eb1', 'ib')['employee']['id'], self.ids['eb1'])

    # ── 4. history ───────────────────────────────────────────────────────────

    def test_06_history_newest_first_and_contract(self):
        body = self._hist('ea1')
        self.assertEqual(set(body), {'ok', 'school', 'employee', 'items', 'pagination'})
        self.assertEqual(body['employee'], {
            'id': self.ids['ea1'], 'employee_id': f'ea1-{self.sfx}',
            'name': self._n('Ali'), 'job_title': 'Teacher', 'photo': None,
            'status': 'active'})
        self.assertEqual([i['id'] for i in body['items']],
                         [self.ids[k] for k in ('a1_d', 'a1_d1', 'a1_d2', 'a1_old')])
        for item in body['items']:
            self.assertEqual(set(item), ATT_KEYS)
        self.assertEqual(body['items'][1], {
            'id': self.ids['a1_d1'], 'date': (D - timedelta(days=1)).isoformat(),
            'status': 'late', 'check_in': '08:15', 'check_out': None,
            'source': 'manual'})
        self.assertEqual(body['items'][2]['status'], 'on_leave')
        self.assertEqual(body['pagination'], {'limit': 30, 'offset': 0,
                                              'has_more': False, 'next_offset': None})
        # Employee without attendance: empty history, still 200.
        self.assertEqual(self._hist('ea3')['items'], [])
        # Inactive employee of the same school: history readable, status shown.
        self.assertEqual(self._hist('ex')['employee']['status'], 'inactive')

    def test_07_history_pagination_and_range(self):
        p1 = self._hist('ea1', limit=2)
        self.assertEqual([i['id'] for i in p1['items']],
                         [self.ids['a1_d'], self.ids['a1_d1']])
        self.assertEqual(p1['pagination'], {'limit': 2, 'offset': 0,
                                            'has_more': True, 'next_offset': 2})
        p2 = self._hist('ea1', limit=2, offset=2)
        self.assertEqual([i['id'] for i in p2['items']],
                         [self.ids['a1_d2'], self.ids['a1_old']])
        self.assertEqual(p2['pagination'], {'limit': 2, 'offset': 2,
                                            'has_more': False, 'next_offset': None})
        self.assertEqual(self._hist('ea1', offset=10)['items'], [])
        ranged = self._hist('ea1', start=(D - timedelta(days=2)).isoformat(),
                            end=(D - timedelta(days=1)).isoformat())
        self.assertEqual([i['id'] for i in ranged['items']],
                         [self.ids['a1_d1'], self.ids['a1_d2']])
        self.assertEqual([i['id'] for i in self._hist('ea1', end='2026-06-01')['items']],
                         [self.ids['a1_old']])

    def test_08_list_pagination(self):
        p1 = self._list(limit=20)
        self.assertEqual(len(p1['items']), 20)
        self.assertEqual(p1['pagination'], {'limit': 20, 'offset': 0,
                                            'has_more': True, 'next_offset': 20})
        seen = [i['id'] for i in p1['items']]
        p3 = self._list(limit=20, offset=40)
        self.assertEqual(len(p3['items']), 3 + N_PAGED - 40)
        self.assertFalse(p3['pagination']['has_more'])
        self.assertIsNone(p3['pagination']['next_offset'])
        all_ids = [i['id'] for i in self._all_items()]
        self.assertEqual(len(all_ids), len(set(all_ids)))     # no dup across pages
        self.assertEqual(all_ids[:20], seen)
        # Default limit 30; oversized limit clamped to 100.
        self.assertEqual(self._list()['pagination']['limit'], 30)
        self.assertEqual(self._list(limit=5000)['pagination']['limit'], 100)
        # Search by employee code.
        code = self._list(search=f'ea2-{self.sfx}')
        self.assertEqual([i['id'] for i in code['items']], [self.ids['ea2']])
        # LIKE wildcards are literal.
        self.assertEqual(self._list(search='%')['items'], [])

    # ── 5. input validation ──────────────────────────────────────────────────

    def test_09_invalid_input_rejected(self):
        hist = f"{API}/{self.ids['ea1']}/attendance"
        cases = (
            (f'{API}/attendance', {'date': '2026-13-01'}, 'invalid_date'),
            (f'{API}/attendance', {'date': 'today'}, 'invalid_date'),
            (f'{API}/attendance', {'limit': 'x'}, 'invalid_limit'),
            (f'{API}/attendance', {'limit': 0}, 'invalid_limit'),
            (f'{API}/attendance', {'limit': -5}, 'invalid_limit'),
            (f'{API}/attendance', {'offset': -1}, 'invalid_offset'),
            (f'{API}/attendance', {'offset': 'x'}, 'invalid_offset'),
            (f'{API}/attendance', {'search': 'a' * 101}, 'invalid_search'),
            (hist, {'start': '2026-02-30'}, 'invalid_start'),
            (hist, {'end': 'x'}, 'invalid_end'),
            (hist, {'start': '2026-09-02', 'end': '2026-09-01'}, 'invalid_date_range'),
            (hist, {'limit': 'x'}, 'invalid_limit'),
        )
        for path, params, error in cases:
            with self.subTest(params=params):
                resp = self._get('ia', path, **params)
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.get_json(), {'ok': False, 'error': error})

    def test_10_data_minimisation(self):
        raw = (self._get('ia', f'{API}/attendance', date=D.isoformat(), limit=100)
               .get_data(as_text=True)
               + self._get('ia', f"{API}/{self.ids['ea1']}/attendance")
               .get_data(as_text=True))
        for secret in ('1234567', '07700000000', '@emp.test', 'AI Face', 'private',
                       'base_salary', 'notes'):
            self.assertNotIn(secret, raw)

    # ── 6. performance / query shape ─────────────────────────────────────────

    def test_11_list_query_count_is_constant(self):
        # Warm the per-school active-year cache: a cold first request adds one
        # academic_years lookup unrelated to page size.
        self._list(limit=1)
        s5, st5 =self._statements('ia', f'{API}/attendance', date=D.isoformat(), limit=5)
        s50, st50 = self._statements('ia', f'{API}/attendance', date=D.isoformat(),
                                     limit=50)
        self.assertEqual((s5, s50), (200, 200))
        att5 = [s for s in st5 if 'employee_attendance' in s]
        att50 = [s for s in st50 if 'employee_attendance' in s]
        # One employees LEFT OUTER JOIN employee_attendance, regardless of size.
        self.assertEqual(len(att50), 1)
        self.assertEqual(len(att5), 1)
        self.assertIn('LEFT OUTER JOIN employee_attendance', att50[0])
        self.assertIn('employees.school_id', att50[0])
        self.assertIn('employee_attendance.school_id', att50[0])
        self.assertEqual(len(st50), len(st5))
        print(f'\n[query-count] list limit=5: {len(st5)} statements, '
              f'limit=50: {len(st50)} statements')

    def test_12_history_query_count(self):
        status, stmts = self._statements('ia', f"{API}/{self.ids['ea1']}/attendance")
        self.assertEqual(status, 200)
        att = [s for s in stmts if 'FROM employee_attendance' in s]
        self.assertEqual(len(att), 1)
        self.assertIn('employee_attendance.school_id', att[0])
        print(f'\n[query-count] history: {len(stmts)} statements')

    # ── 7. read-only ─────────────────────────────────────────────────────────

    def test_13_endpoints_are_read_only(self):
        with self.app.app_context():
            before = EmployeeAttendance.query.execution_options(**OPTS).filter(
                EmployeeAttendance.school_id.in_(self.school_ids)).count()
        client = self.app.test_client()
        hdr = {'Authorization': f"Bearer {self._token('ia')}"}
        for path in (f'{API}/attendance', f"{API}/{self.ids['ea1']}/attendance"):
            for method in (client.post, client.put, client.delete):
                self.assertEqual(method(path, headers=hdr).status_code, 405)
        self._list(limit=100)
        self._hist('ea1')
        with self.app.app_context():
            after = EmployeeAttendance.query.execution_options(**OPTS).filter(
                EmployeeAttendance.school_id.in_(self.school_ids)).count()
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()

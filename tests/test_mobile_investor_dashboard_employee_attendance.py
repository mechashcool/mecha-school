"""
Investor dashboard — employee attendance summary (HTTP-level tests).

  GET /api/mobile/v1/investor/dashboard  →  employee_attendance_today

Read-only, school-local "today" (get_local_date is patched to a fixed date).
The existing dashboard fields (student attendance KPIs, finance) are untouched;
only the new block and the fail-closed investor school check are exercised.

Fixture (D = 2026-09-29, a Tuesday; Friday is the employee weekly day off):
  school A  a_p1..a_p3 present, a_l late, a_ab absent, a_nr no row on D
            (present on D-1), a_ol on_leave (leave), a_in INACTIVE present
            → total 7, expected 6, attended 4, absent 1+1, 66.7 %
  school B  b1 present, b2 late on D; both on_leave on D-1
  school C  institute — one employee present on D
  users: IA/IB/IC investors of A/B/C, IN investor without school,
         PA parent(A), TA teacher(A)

Requires an isolated, approved test database (tests/conftest.py guard).
"""
import unittest
from datetime import date, time, timedelta
from unittest import mock
from uuid import uuid4

from sqlalchemy import event

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (db, AcademicYear, Employee, EmployeeAttendance, Role,
                        School, User)

OPTS = {'bypass_tenant_scope': True}
URL = '/api/mobile/v1/investor/dashboard'
D = date(2026, 9, 29)          # Tuesday
FRIDAY = date(2026, 10, 2)     # employee weekly day off
LOCAL_DATE = 'app.utils.attendance_helpers.get_local_date'

SUMMARY_KEYS = {'date', 'is_day_off', 'total_active', 'expected_to_attend',
                'attended', 'present', 'late', 'absent', 'absent_recorded',
                'not_recorded', 'on_leave', 'attendance_percentage'}
# Response keys before this change + the one new block.
DASHBOARD_KEYS = {'ok', 'year', 'total_revenue', 'total_expense', 'balance',
                  'monthly_revenue', 'monthly_expense', 'school', 'academic_year',
                  'kpis', 'charts', 'recent_students', 'recent_notifications',
                  'employee_attendance_today'}
KPI_KEYS = {'active_students', 'active_employees', 'attendance_today',
            'absence_today', 'fees_collected_today', 'overdue_installments',
            'current_month_revenue', 'current_month_expense', 'current_month_net',
            'total_revenue', 'total_expense', 'balance', 'revenue_change_pct',
            'expense_change_pct', 'net_change_pct', 'present_change_pct',
            'absent_change_pct', 'fees_today_change_pct'}


class InvestorDashboardEmployeeAttendanceTest(unittest.TestCase):

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
            self._school_c()
            self._user(None, 'in', 'investor_viewer')
            db.session.commit()

    def _add(self, obj):
        db.session.add(obj)
        db.session.flush()
        return obj

    def _school(self, key, **extra):
        school = self._add(School(school_name=f'IDE {key} {self.sfx}',
                                  code=f'ID{key}{self.sfx}'[:20], capacity=0,
                                  is_active=True, weekly_off_days='4', **extra))
        self.school_ids.append(school.id)
        self.ids[f'school_{key}'] = school.id
        year = self._add(AcademicYear(school_id=school.id, name=f'Y{key}{self.sfx}',
                                      is_current=True, start_date=date(2026, 8, 1),
                                      end_date=date(2027, 7, 31)))
        return school, year

    def _user(self, school, label, role):
        u = User(username=f'ide{label}_{self.sfx}', email=f'ide{label}_{self.sfx}@t.test',
                 full_name=label, role_id=self.role_ids[role],
                 school_id=school.id if school else None, is_active=True)
        u.set_password('Test1234!')
        self._add(u)
        self.ids[label] = u.id
        self.user_ids.append(u.id)
        return u

    def _emp(self, school, label, status='active'):
        emp = self._add(Employee(school_id=school.id, employee_id=f'{label}-{self.sfx}',
                                 full_name=f'{label} {self.sfx}', base_salary=0,
                                 status=status))
        self.ids[label] = emp.id
        return emp

    def _att(self, emp, year, day, status, cin=None, source='manual'):
        return self._add(EmployeeAttendance(
            employee_id=emp.id, school_id=emp.school_id, academic_year_id=year.id,
            date=day, status=status, check_in=cin, source=source))

    def _school_a(self):
        school, ya = self._school('a')
        for i in (1, 2, 3):
            self._att(self._emp(school, f'a_p{i}'), ya, D, 'present', time(7, 30),
                      'aiface' if i == 1 else 'manual')
        self._att(self._emp(school, 'a_l'), ya, D, 'late', time(8, 20))
        self._att(self._emp(school, 'a_ab'), ya, D, 'absent')
        nr = self._emp(school, 'a_nr')
        self._att(nr, ya, D - timedelta(days=1), 'present', time(7, 0))
        self._att(self._emp(school, 'a_ol'), ya, D, 'on_leave', source='leave')
        self._att(self._emp(school, 'a_in', status='inactive'), ya, D, 'present',
                  time(7, 0))
        self._user(school, 'ia', 'investor_viewer')
        self._user(school, 'pa', 'parent')
        self._user(school, 'ta', 'teacher')

    def _school_b(self):
        school, yb = self._school('b')
        b1, b2 = self._emp(school, 'b1'), self._emp(school, 'b2')
        self._att(b1, yb, D, 'present', time(7, 0))
        self._att(b2, yb, D, 'late', time(9, 0))
        for emp in (b1, b2):
            self._att(emp, yb, D - timedelta(days=1), 'on_leave', source='leave')
        self._user(school, 'ib', 'investor_viewer')

    def _school_c(self):
        school, yc = self._school('c', institution_type='institute')
        self._att(self._emp(school, 'c1'), yc, D, 'present', time(7, 0))
        self._user(school, 'ic', 'investor_viewer')

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for sid in self.school_ids:
                for model in (EmployeeAttendance, Employee):
                    model.query.execution_options(**OPTS).filter_by(
                        school_id=sid).delete(synchronize_session=False)
            User.query.execution_options(**OPTS).filter(
                User.id.in_(self.user_ids)).delete(synchronize_session=False)
            for sid in self.school_ids:
                AcademicYear.query.execution_options(**OPTS).filter_by(
                    school_id=sid).delete(synchronize_session=False)
                School.query.filter_by(id=sid).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _headers(self, user_key):
        with self.app.app_context():
            user = db.session.get(User, self.ids[user_key], execution_options=OPTS)
            return {'Authorization': f'Bearer {encode_token(user)}'}

    def _get(self, user_key, on_date=D, **params):
        headers = self._headers(user_key)
        with mock.patch(LOCAL_DATE, return_value=on_date):
            return self.app.test_client().get(URL, query_string=params,
                                              headers=headers)

    def _dash(self, user_key='ia', on_date=D, **params):
        resp = self._get(user_key, on_date, **params)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:300])
        body = resp.get_json()
        self.assertTrue(body['ok'])
        return body

    def _summary(self, user_key='ia', on_date=D, **params):
        return self._dash(user_key, on_date, **params)['employee_attendance_today']

    # ── 1. calculation ───────────────────────────────────────────────────────

    def test_01_working_day_summary(self):
        body = self._dash()
        self.assertEqual(set(body), DASHBOARD_KEYS)
        self.assertEqual(body['employee_attendance_today'], {
            'date': D.isoformat(), 'is_day_off': False,
            'total_active': 7, 'expected_to_attend': 6,
            'attended': 4, 'present': 3, 'late': 1,
            'absent': 2, 'absent_recorded': 1, 'not_recorded': 1,
            'on_leave': 1,
            'attendance_percentage': 66.7,          # 4 / 6 → 66.666…
        })
        # Existing KPI contract unchanged; active_employees keeps its meaning.
        self.assertEqual(set(body['kpis']), KPI_KEYS)
        self.assertEqual(body['kpis']['active_employees'], 7)
        self.assertEqual(set(body['charts']['attendance']), {'present', 'absent'})
        # Student attendance KPIs do not pick up employee rows.
        self.assertEqual(body['kpis']['attendance_today'], 0)
        self.assertEqual(body['kpis']['absence_today'], 0)

    def test_02_day_off_and_zero_denominator(self):
        fri = self._summary(on_date=FRIDAY)
        self.assertEqual(fri, {
            'date': FRIDAY.isoformat(), 'is_day_off': True, 'total_active': 7,
            'expected_to_attend': 0, 'attended': 0, 'present': 0, 'late': 0,
            'absent': 0, 'absent_recorded': 0, 'not_recorded': 0, 'on_leave': 0,
            'attendance_percentage': None})
        # Working day where every active employee is on leave: no division by 0.
        all_leave = self._summary('ib', on_date=D - timedelta(days=1))
        self.assertFalse(all_leave['is_day_off'])
        self.assertEqual((all_leave['total_active'], all_leave['on_leave'],
                          all_leave['expected_to_attend']), (2, 2, 0))
        self.assertIsNone(all_leave['attendance_percentage'])
        self.assertEqual(set(all_leave), SUMMARY_KEYS)

    def test_03_institute_returns_null(self):
        body = self._dash('ic')
        self.assertEqual(set(body), DASHBOARD_KEYS)
        self.assertIsNone(body['employee_attendance_today'])
        self.assertEqual(body['school']['id'], self.ids['school_c'])

    # ── 2. isolation / authorization ─────────────────────────────────────────

    def test_04_school_isolation_and_forged_params(self):
        a = self._summary()
        b = self._summary('ib')
        self.assertEqual(b, {
            'date': D.isoformat(), 'is_day_off': False,
            'total_active': 2, 'expected_to_attend': 2,
            'attended': 2, 'present': 1, 'late': 1,
            'absent': 0, 'absent_recorded': 0, 'not_recorded': 0,
            'on_leave': 0, 'attendance_percentage': 100.0})
        # Client-supplied scope never changes the school used.
        forged = dict(school_id=self.ids['school_b'], academic_year_id=1,
                      employee_id=self.ids['b1'], user_id=self.ids['ib'])
        self.assertEqual(self._summary(**forged), a)
        self.assertEqual(self._summary('ib', school_id=self.ids['school_a']), b)

    def test_05_fail_closed_and_roles(self):
        client = self.app.test_client()
        self.assertEqual(client.get(URL).status_code, 401)
        for user_key in ('in', 'pa', 'ta'):
            with self.subTest(user=user_key):
                resp = self._get(user_key)
                self.assertEqual(resp.status_code, 403)
                self.assertEqual(resp.get_json(), {'ok': False, 'error': 'forbidden'})

    # ── 3. query shape / read-only ───────────────────────────────────────────

    def test_06_single_grouped_query_and_read_only(self):
        self._dash()     # warm the per-school active-year cache
        with self.app.app_context():
            engine = db.engine
            before = EmployeeAttendance.query.execution_options(**OPTS).filter(
                EmployeeAttendance.school_id.in_(self.school_ids)).count()
        seen = []

        def capture(conn, cursor, statement, *args):
            seen.append(statement)

        headers = self._headers('ia')
        event.listen(engine, 'before_cursor_execute', capture)
        try:
            with mock.patch(LOCAL_DATE, return_value=D):
                resp = self.app.test_client().get(URL, headers=headers)
        finally:
            event.remove(engine, 'before_cursor_execute', capture)
        self.assertEqual(resp.status_code, 200)
        att = [s for s in seen if 'employee_attendance' in s]
        self.assertEqual(len(att), 1)
        stmt = att[0]
        self.assertTrue(stmt.lstrip().upper().startswith('SELECT'))
        self.assertIn('LEFT OUTER JOIN employee_attendance', stmt)
        self.assertIn('employees.school_id', stmt)
        self.assertIn('employee_attendance.school_id', stmt)
        self.assertIn('GROUP BY', stmt)
        with self.app.app_context():
            after = EmployeeAttendance.query.execution_options(**OPTS).filter(
                EmployeeAttendance.school_id.in_(self.school_ids)).count()
        self.assertEqual(before, after)
        print(f'\n[query-count] dashboard: {len(seen)} statements, '
              f'{len(att)} touching employee_attendance')


if __name__ == '__main__':
    unittest.main()

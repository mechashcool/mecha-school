"""
Transport live tracking — Phase 1: driver identity + trip lifecycle.

  Web    /transport/create, /transport/<id>/edit   driver section (manual /
                                                    existing / new driver)
  Mobile POST /api/mobile/v1/auth/login             driver role accepted
         GET  /api/mobile/v1/driver/routes
         POST /api/mobile/v1/driver/routes/<id>/trips/start
         POST /api/mobile/v1/driver/trips/<id>/end

Fixture (per test, unique suffix):
  school A — admin AA (school_admin), TO (custom role: manage_transport only)
    e_free   driver Employee, no account
    e_tch    driver Employee linked to a TEACHER account
    e_drv    driver Employee + active driver account DA  (mobile tests)
    e_drv2   driver Employee + active driver account DA2 (not assigned)
    routes   ra1, ra2 (active, → e_drv), ra_off (inactive, → e_drv),
             ra_leg (legacy: text driver only, no employee)
    parent PA → student st1 subscribed to ra_leg
  school B — admin AB, e_b driver Employee + driver account DB, route rb1 → e_b

Requires an isolated, approved test database (tests/conftest.py guard).
"""
import re
import unittest
from datetime import date
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (db, AcademicYear, AuditLog, Employee, Permission, Role,
                        School, Student, StudentTransport, TransportRoute,
                        TransportTrip, User, parent_students)

OPTS = {'bypass_tenant_scope': True}
PASSWORD = 'Test1234!'
API = '/api/mobile/v1'
DRIVER = 'سائق'
ROUTE_KEYS = {'id', 'name', 'vehicle_name', 'active_trip'}


class TransportDriverPhase1Test(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['RATELIMIT_ENABLED'] = False
        with cls.app.app_context():
            cls.role_ids = {n: Role.query.filter_by(name=n).first().id
                            for n in ('school_admin', 'teacher', 'parent', 'driver')}

    # ── fixture ───────────────────────────────────────────────────────────────

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        with self.app.app_context():
            perm = Permission.query.filter_by(name='manage_transport').first()
            role = Role(name=f'tonly_{self.sfx}', label='transport only', is_admin=False)
            role.permissions = [perm]
            db.session.add(role)
            db.session.flush()
            self.ids['tonly_role'] = role.id
            self._school_a()
            self._school_b()
            db.session.commit()

    def _add(self, obj):
        db.session.add(obj)
        db.session.flush()
        return obj

    def _school(self, key):
        school = self._add(School(school_name=f'TDrv {key} {self.sfx}',
                                  code=f'TD{key}{self.sfx}'[:20], capacity=0,
                                  is_active=True))
        year = self._add(AcademicYear(school_id=school.id, name=f'Y{key}{self.sfx}',
                                      is_current=True, start_date=date(2026, 8, 1),
                                      end_date=date(2027, 6, 30)))
        self.ids[f'school_{key}'] = school.id
        self.ids[f'year_{key}'] = year.id
        return school

    def _user(self, school, label, role_id):
        u = User(username=f'td{label}_{self.sfx}', email=f'td{label}_{self.sfx}@t.test',
                 full_name=f'{label} {self.sfx}', role_id=role_id,
                 school_id=school.id, is_active=True)
        u.set_password(PASSWORD)
        self._add(u)
        self.ids[label] = u.id
        return u

    def _emp(self, school, label, user=None, job=DRIVER):
        e = self._add(Employee(school_id=school.id, employee_id=f'{label}-{self.sfx}',
                               full_name=f'Drv {label} {self.sfx}', job_title=job,
                               phone=f'0770{label[-3:]:0>7}'[:30], base_salary=0,
                               status='active', user_id=user.id if user else None))
        self.ids[label] = e.id
        return e

    def _route(self, school, label, emp=None, status='active'):
        r = self._add(TransportRoute(
            school_id=school.id, name=f'R {label} {self.sfx}',
            driver_name=emp.full_name if emp else f'Legacy {label}',
            driver_phone=emp.phone if emp else '07801112233',
            vehicle_type='Coaster', vehicle_number=f'V-{label}', capacity=20,
            status=status, driver_employee_id=emp.id if emp else None))
        self.ids[label] = r.id
        return r

    def _school_a(self):
        s = self._school('a')
        self._user(s, 'aa', self.role_ids['school_admin'])
        self._user(s, 'to', self.ids['tonly_role'])
        self._emp(s, 'e_free')
        self._emp(s, 'e_tch', self._user(s, 'tch', self.role_ids['teacher']))
        e_drv = self._emp(s, 'e_drv', self._user(s, 'da', self.role_ids['driver']))
        self._emp(s, 'e_drv2', self._user(s, 'da2', self.role_ids['driver']))
        self._route(s, 'ra1', e_drv)
        self._route(s, 'ra2', e_drv)
        self._route(s, 'ra_off', e_drv, status='inactive')
        leg = self._route(s, 'ra_leg')
        st = self._add(Student(student_id=f'ST-{self.sfx}', full_name=f'Stu {self.sfx}',
                               school_id=s.id, academic_year_id=self.ids['year_a'],
                               status='active'))
        self.ids['st1'] = st.id
        pa = self._user(s, 'pa', self.role_ids['parent'])
        db.session.execute(parent_students.insert().values(user_id=pa.id, student_id=st.id))
        self._add(StudentTransport(school_id=s.id, route_id=leg.id, student_id=st.id,
                                   status='active'))

    def _school_b(self):
        s = self._school('b')
        self._user(s, 'ab', self.role_ids['school_admin'])
        e_b = self._emp(s, 'e_b', self._user(s, 'db', self.role_ids['driver']))
        self._route(s, 'rb1', e_b)

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            sids = [self.ids['school_a'], self.ids['school_b']]
            uids = [u.id for u in User.query.execution_options(**OPTS)
                    .filter(User.school_id.in_(sids)).all()]
            TransportTrip.query.execution_options(**OPTS).filter(
                TransportTrip.school_id.in_(sids)).delete(synchronize_session=False)
            StudentTransport.query.execution_options(**OPTS).filter(
                StudentTransport.school_id.in_(sids)).delete(synchronize_session=False)
            TransportRoute.query.execution_options(**OPTS).filter(
                TransportRoute.school_id.in_(sids)).delete(synchronize_session=False)
            if uids:
                db.session.execute(parent_students.delete().where(
                    parent_students.c.user_id.in_(uids)))
                AuditLog.query.execution_options(**OPTS).filter(
                    AuditLog.user_id.in_(uids)).delete(synchronize_session=False)
            for model in (AuditLog, Student, Employee, User, AcademicYear):
                model.query.execution_options(**OPTS).filter(
                    model.school_id.in_(sids)).delete(synchronize_session=False)
            School.query.filter(School.id.in_(sids)).delete(synchronize_session=False)
            role = db.session.get(Role, self.ids['tonly_role'])
            role.permissions = []
            db.session.delete(role)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _web(self, label='aa'):
        client = self.app.test_client()
        resp = client.post('/auth/login', data={'username': f'td{label}_{self.sfx}',
                                                'password': PASSWORD})
        self.assertEqual(resp.status_code, 302)
        return client

    def _form(self, name, **driver):
        data = {'name': f'{name} {self.sfx}', 'route_number': '7',
                'vehicle_type': 'Bus', 'vehicle_number': 'P-1', 'capacity': '15',
                'status': 'active'}
        data.update(driver)
        return data

    def _flashes(self, client):
        """Pop (consume) the pending flash messages, like a page render would."""
        with client.session_transaction() as sess:
            return [m for _, m in sess.pop('_flashes', [])]

    def _route_row(self, name):
        with self.app.app_context():
            r = (TransportRoute.query.execution_options(**OPTS)
                 .filter_by(name=f'{name} {self.sfx}').first())
            return None if r is None else (r.id, r.school_id, r.driver_employee_id,
                                           r.driver_name, r.driver_phone)

    def _emp_row(self, emp_id):
        with self.app.app_context():
            e = db.session.get(Employee, emp_id, execution_options=OPTS)
            return (e.school_id, e.user_id, e.job_title, e.department, e.status,
                    e.full_name, e.phone, e.employee_id)

    def _user_row(self, user_id):
        with self.app.app_context():
            u = db.session.get(User, user_id, execution_options=OPTS)
            return (u.school_id, u.role.name, u.is_active, u.username)

    def _token(self, label):
        with self.app.app_context():
            return encode_token(db.session.get(User, self.ids[label], execution_options=OPTS))

    def _api(self, method, label, path, **kw):
        client = self.app.test_client()
        return getattr(client, method)(
            f'{API}{path}', headers={'Authorization': f'Bearer {self._token(label)}'}, **kw)

    def _count_queries(self, label, path):
        seen = []

        def capture(conn, cursor, statement, *a):
            seen.append(statement)

        token = self._token(label)
        with self.app.app_context():
            engine = db.engine
        event.listen(engine, 'before_cursor_execute', capture)
        try:
            resp = self.app.test_client().get(
                f'{API}{path}', headers={'Authorization': f'Bearer {token}'})
        finally:
            event.remove(engine, 'before_cursor_execute', capture)
        self.assertEqual(resp.status_code, 200)
        return len(seen)

    def _audit(self, action, resource):
        with self.app.app_context():
            return AuditLog.query.execution_options(**OPTS).filter(
                AuditLog.school_id == self.ids['school_a'],
                AuditLog.action == action, AuditLog.resource == resource).all()

    # ── 1. new driver created from the Transport UI ──────────────────────────

    def test_01_new_driver_created_and_linked(self):
        client = self._web()
        resp = client.post('/transport/create', data=self._form(
            'New', driver_mode='new', new_driver_name='  Kareem   Ali ',
            new_driver_phone='07701234567',
            school_id=str(self.ids['school_b'])))               # forged — ignored
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True)[:400])
        rid, sid, emp_id, dname, dphone = self._route_row('New')
        self.assertEqual(sid, self.ids['school_a'])
        e_sid, uid, job, dept, status, fname, phone, code = self._emp_row(emp_id)
        self.assertEqual((e_sid, job, dept, status, fname, phone),
                         (self.ids['school_a'], DRIVER, 'النقل', 'active',
                          'Kareem Ali', '07701234567'))
        self.assertRegex(code, r'-EMP-\d{6}$')
        self.assertEqual((dname, dphone), ('Kareem Ali', '07701234567'))   # synced
        u_sid, role, active, username = self._user_row(uid)
        self.assertEqual((u_sid, role, active), (self.ids['school_a'], 'driver', True))

        # Credentials shown once; password works and is stored only as a hash.
        msg = next(m for m in self._flashes(client) if username in m)
        password = re.search(r'كلمة المرور: (\S+)\.', msg).group(1)
        with self.app.app_context():
            u = db.session.get(User, uid, execution_options=OPTS)
            self.assertNotIn(password, u.password_hash)
            self.assertTrue(u.check_password(password))
        login = self.app.test_client().post(f'{API}/auth/login', json={
            'username': username, 'password': password})
        self.assertEqual(login.status_code, 200)
        body = login.get_json()
        self.assertEqual(body['user']['role'], 'driver')
        self.assertEqual(body['employee']['id'], emp_id)

        # Audit: employee + account + link recorded, never the password.
        self.assertTrue(self._audit('create', 'employee'))
        self.assertTrue(self._audit('create', 'user'))
        self.assertTrue(self._audit('link_driver', 'transport_route'))
        with self.app.app_context():
            details = [a.details or '' for a in AuditLog.query.execution_options(**OPTS)
                       .filter(AuditLog.school_id == self.ids['school_a']).all()]
        self.assertFalse(any(password in d for d in details))

        # manage_transport alone cannot create employees / accounts.
        to = self._web('to')
        resp = to.post('/transport/create', data=self._form(
            'Denied', driver_mode='new', new_driver_name='Nope Driver',
            new_driver_phone='0770'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('صلاحية إدارة الموظفين', resp.get_data(as_text=True))
        self.assertIsNone(self._route_row('Denied'))
        with self.app.app_context():
            self.assertEqual(Employee.query.execution_options(**OPTS)
                             .filter_by(full_name='Nope Driver').count(), 0)

    # ── 2. one driver, several routes, ONE account ───────────────────────────

    def test_02_existing_driver_reused_across_routes(self):
        client = self._web()
        emp_id = self.ids['e_free']
        client.post('/transport/create', data=self._form(
            'X1', driver_mode='existing', driver_employee_id=str(emp_id)))
        uid = self._emp_row(emp_id)[1]
        self.assertIsNotNone(uid)
        self.assertEqual(self._user_row(uid)[1], 'driver')
        self.assertTrue(any('اسم المستخدم' in m for m in self._flashes(client)))

        client.post('/transport/create', data=self._form(
            'X2', driver_mode='existing', driver_employee_id=str(emp_id)))
        self.assertFalse(any('اسم المستخدم' in m for m in self._flashes(client)))
        self.assertEqual(self._emp_row(emp_id)[1], uid)          # same account
        self.assertEqual(self._route_row('X1')[2], emp_id)
        self.assertEqual(self._route_row('X2')[2], emp_id)
        with self.app.app_context():
            n = (User.query.execution_options(**OPTS)
                 .filter_by(school_id=self.ids['school_a'],
                            role_id=self.role_ids['driver'],
                            full_name=f'Drv e_free {self.sfx}').count())
        self.assertEqual(n, 1)

        # The picker lists only this school's active drivers.
        page = client.get('/transport/create').get_data(as_text=True)
        self.assertIn(f'Drv e_free {self.sfx}', page)
        self.assertNotIn(f'Drv e_b {self.sfx}', page)

    # ── 3. cross-school links fail closed ────────────────────────────────────

    def test_03_cross_school_driver_rejected(self):
        client = self._web()
        resp = client.post('/transport/create', data=self._form(
            'Cross', driver_mode='existing', driver_employee_id=str(self.ids['e_b'])))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self._route_row('Cross'))
        resp = client.post(f"/transport/{self.ids['ra_leg']}/edit", data=self._form(
            'LegEdit', driver_mode='existing', driver_employee_id=str(self.ids['e_b'])))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self._route_row('LegEdit'))
        # Another school's route is not editable at all.
        resp = client.post(f"/transport/{self.ids['rb1']}/edit", data=self._form(
            'Steal', driver_mode='existing', driver_employee_id=str(self.ids['e_drv'])))
        self.assertEqual(resp.status_code, 403)
        # Any other code path is stopped by the flush guard.
        with self.app.app_context():
            r = db.session.get(TransportRoute, self.ids['ra_leg'], execution_options=OPTS)
            r.driver_employee_id = self.ids['e_b']
            with self.assertRaises(ValueError):
                db.session.flush()
            db.session.rollback()
            with self.assertRaises(ValueError):
                db.session.add(TransportTrip(school_id=self.ids['school_a'],
                                             route_id=self.ids['rb1'],
                                             driver_employee_id=self.ids['e_drv']))
                db.session.flush()
            db.session.rollback()

    # ── 4. another role is never re-roled or duplicated ──────────────────────

    def test_04_non_driver_account_not_rerolled(self):
        client = self._web()
        before = self._user_row(self.ids['tch'])
        resp = client.post('/transport/create', data=self._form(
            'Tch', driver_mode='existing', driver_employee_id=str(self.ids['e_tch'])))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('بدور آخر', resp.get_data(as_text=True))
        self.assertIsNone(self._route_row('Tch'))
        self.assertEqual(self._user_row(self.ids['tch']), before)        # still teacher
        self.assertEqual(self._emp_row(self.ids['e_tch'])[1], self.ids['tch'])
        with self.app.app_context():
            self.assertEqual(User.query.execution_options(**OPTS).filter_by(
                full_name=f'Drv e_tch {self.sfx}').count(), 0)

    # ── 5. driver login + assigned routes only, bounded queries ──────────────

    def test_05_driver_login_and_routes(self):
        login = self.app.test_client().post(f'{API}/auth/login', json={
            'username': f'tdda_{self.sfx}', 'password': PASSWORD})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.get_json()['user']['role'], 'driver')

        resp = self._api('get', 'da', '/driver/routes')
        self.assertEqual(resp.status_code, 200)
        routes = resp.get_json()['routes']
        self.assertEqual({r['id'] for r in routes}, {self.ids['ra1'], self.ids['ra2']})
        for r in routes:
            self.assertEqual(set(r), ROUTE_KEYS)
            self.assertIsNone(r['active_trip'])
        self.assertEqual(self._api('get', 'da2', '/driver/routes').get_json()['routes'], [])
        self.assertEqual({r['id'] for r in self._api('get', 'db', '/driver/routes')
                          .get_json()['routes']}, {self.ids['rb1']})

        # Role gate.
        self.assertEqual(self.app.test_client().get(f'{API}/driver/routes').status_code, 401)
        for label in ('pa', 'tch'):
            self.assertEqual(self._api('get', label, '/driver/routes').status_code, 403)

        # Query count does not grow with the number of routes.
        q_two = self._count_queries('da', '/driver/routes')
        with self.app.app_context():
            s = db.session.get(School, self.ids['school_a'])
            e = db.session.get(Employee, self.ids['e_drv'], execution_options=OPTS)
            for i in range(8):
                self._route(s, f'extra{i}', e)
            db.session.commit()
        q_ten = self._count_queries('da', '/driver/routes')
        self.assertEqual(q_two, q_ten)
        self.assertLessEqual(q_ten, 6)
        self.assertEqual(len(self._api('get', 'da', '/driver/routes').get_json()['routes']), 10)

        # A driver web session is refused.
        web = self.app.test_client()
        web.post('/auth/login', data={'username': f'tdda_{self.sfx}', 'password': PASSWORD})
        resp = web.get('/auth/profile')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/auth/login', resp.headers['Location'])
        resp = web.get('/auth/profile')                        # session is gone
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/auth/login', resp.headers['Location'])

    # ── 6. start trip ────────────────────────────────────────────────────────

    def test_06_start_trip(self):
        ra1 = self.ids['ra1']
        resp = self._api('post', 'da', f'/driver/routes/{ra1}/trips/start',
                         json={'school_id': self.ids['school_b'],
                               'driver_employee_id': self.ids['e_b']})   # ignored
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        trip = resp.get_json()['trip']
        self.assertEqual((trip['route_id'], trip['status']), (ra1, 'active'))
        self.assertTrue(trip['started_at'].endswith('+00:00'))

        again = self._api('post', 'da', f'/driver/routes/{ra1}/trips/start')
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.get_json()['trip']['id'], trip['id'])
        with self.app.app_context():
            rows = TransportTrip.query.execution_options(**OPTS).filter_by(route_id=ra1).all()
            self.assertEqual([(t.school_id, t.driver_employee_id, t.status) for t in rows],
                             [(self.ids['school_a'], self.ids['e_drv'], 'active')])
            # The database refuses a second active trip on the route.
            db.session.add(TransportTrip(school_id=self.ids['school_a'], route_id=ra1,
                                         driver_employee_id=self.ids['e_drv']))
            with self.assertRaises(IntegrityError):
                db.session.flush()
            db.session.rollback()

        listed = {r['id']: r for r in self._api('get', 'da', '/driver/routes')
                  .get_json()['routes']}
        self.assertEqual(listed[ra1]['active_trip']['id'], trip['id'])
        self.assertIsNone(listed[self.ids['ra2']]['active_trip'])

        for label, rid, code, error in (
                ('da', self.ids['ra_off'], 409, 'route_inactive'),
                ('da', self.ids['ra_leg'], 404, 'route_not_found'),   # not assigned
                ('da', self.ids['rb1'], 404, 'route_not_found'),      # other school
                ('da2', ra1, 404, 'route_not_found'),                 # other driver
                ('da', 999999999, 404, 'route_not_found')):
            resp = self._api('post', label, f'/driver/routes/{rid}/trips/start')
            self.assertEqual((resp.status_code, resp.get_json()['error']), (code, error))
        self.assertTrue(self._audit('trip_start', 'transport_trip'))

    # ── 7. end trip ──────────────────────────────────────────────────────────

    def test_07_end_trip(self):
        ra1 = self.ids['ra1']
        trip_id = self._api('post', 'da', f'/driver/routes/{ra1}/trips/start') \
            .get_json()['trip']['id']
        for label in ('da2', 'db', 'tch'):
            resp = self._api('post', label, f'/driver/trips/{trip_id}/end')
            self.assertIn(resp.status_code, (403, 404))
        with self.app.app_context():
            self.assertEqual(db.session.get(TransportTrip, trip_id,
                                            execution_options=OPTS).status, 'active')

        resp = self._api('post', 'da', f'/driver/trips/{trip_id}/end')
        self.assertEqual(resp.status_code, 200)
        ended = resp.get_json()['trip']
        self.assertEqual((ended['id'], ended['status']), (trip_id, 'ended'))
        self.assertIsNotNone(ended['ended_at'])
        again = self._api('post', 'da', f'/driver/trips/{trip_id}/end').get_json()['trip']
        self.assertEqual(again, ended)                              # idempotent
        self.assertEqual(len(self._audit('trip_end', 'transport_trip')), 1)

        new = self._api('post', 'da', f'/driver/routes/{ra1}/trips/start')
        self.assertEqual(new.status_code, 201)
        self.assertNotEqual(new.get_json()['trip']['id'], trip_id)

        # Deactivated driver Employee loses access (fail closed).
        with self.app.app_context():
            db.session.get(Employee, self.ids['e_drv'], execution_options=OPTS).status = 'inactive'
            db.session.commit()
        resp = self._api('get', 'da', '/driver/routes')
        self.assertEqual((resp.status_code, resp.get_json()['error']),
                         (403, 'driver_not_linked'))

    # ── 8. legacy routes + parent contract unchanged ─────────────────────────

    def test_08_legacy_route_and_parent_contract(self):
        st = self.ids['st1']
        resp = self._api('get', 'pa', f'/parent/children/{st}/transportation')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {'ok': True, 'transportation': {
            'driver_name': 'Legacy ra_leg', 'phone': '07801112233',
            'vehicle_name': 'Coaster'}})

        client = self._web()
        page = client.get(f"/transport/{self.ids['ra_leg']}/edit").get_data(as_text=True)
        self.assertIn('value="manual" checked', page)
        resp = client.post(f"/transport/{self.ids['ra_leg']}/edit", data=self._form(
            'Leg', driver_mode='manual', driver_name='Legacy New', driver_phone='0780'))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._route_row('Leg')[2:], (None, 'Legacy New', '0780'))
        # Omitting driver_mode entirely keeps the old (manual) behaviour.
        resp = client.post(f"/transport/{self.ids['ra_leg']}/edit", data=self._form(
            'Leg2', driver_name='Legacy Old', driver_phone='0781'))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._route_row('Leg2')[2:], (None, 'Legacy Old', '0781'))

        # Linking a real driver keeps the parent contract, with synced values.
        client.post(f"/transport/{self.ids['ra_leg']}/edit", data=self._form(
            'Leg3', driver_mode='existing', driver_employee_id=str(self.ids['e_drv'])))
        e = self._emp_row(self.ids['e_drv'])
        self.assertEqual(self._route_row('Leg3')[2:], (self.ids['e_drv'], e[5], e[6]))
        resp = self._api('get', 'pa', f'/parent/children/{st}/transportation')
        self.assertEqual(resp.get_json()['transportation'],
                         {'driver_name': e[5], 'phone': e[6], 'vehicle_name': 'Bus'})


if __name__ == '__main__':
    unittest.main()

"""
Transport live tracking — Phase 2: latest GPS location on the active trip.

  POST /api/mobile/v1/driver/trips/<trip_id>/location                 (driver)
  GET  /api/mobile/v1/parent/children/<student_id>/transportation/live (parent)

Fixture (per test, unique suffix):
  school A
    e_drv  + driver DA   → routes ra1, ra2 (active)
    e_drv2 + driver DA2  → route  ra3 (active)
    parent PA → st1 (active link ra1), st3 (active links ra1 AND ra2 —
                ambiguous), st4 (no transport)
    parent PB → st2 (active link ra3)
  school B
    e_b + driver DB → route rb1;  parent PX (no link to school A children)

Requires an isolated, approved test database (tests/conftest.py guard).
"""
import unittest
from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import event

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (db, AcademicYear, AuditLog, Employee, Role, School,
                        Student, StudentTransport, TransportRoute,
                        TransportTrip, User, parent_students)

OPTS = {'bypass_tenant_scope': True}
PASSWORD = 'Test1234!'
API = '/api/mobile/v1'
LOC_KEYS = {'latitude', 'longitude', 'accuracy', 'updated_at', 'age_seconds'}
WRITE_VERBS = ('INSERT', 'UPDATE', 'DELETE')


class TransportLiveLocationTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['RATELIMIT_ENABLED'] = False
        with cls.app.app_context():
            cls.role_ids = {n: Role.query.filter_by(name=n).first().id
                            for n in ('parent', 'driver')}

    # ── fixture ───────────────────────────────────────────────────────────────

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        with self.app.app_context():
            a = self._school('a')
            e_drv = self._emp(a, 'e_drv', self._user(a, 'da', 'driver'))
            e_drv2 = self._emp(a, 'e_drv2', self._user(a, 'da2', 'driver'))
            ra1 = self._route(a, 'ra1', e_drv)
            ra2 = self._route(a, 'ra2', e_drv)
            ra3 = self._route(a, 'ra3', e_drv2)
            pa = self._user(a, 'pa', 'parent')
            pb = self._user(a, 'pb', 'parent')
            self._student(a, 'st1', pa, ra1)
            self._student(a, 'st3', pa, ra1, ra2)
            self._student(a, 'st4', pa)
            self._student(a, 'st2', pb, ra3)
            b = self._school('b')
            self._route(b, 'rb1', self._emp(b, 'e_b', self._user(b, 'db', 'driver')))
            self._user(b, 'px', 'parent')
            db.session.commit()

    def _add(self, obj):
        db.session.add(obj)
        db.session.flush()
        return obj

    def _school(self, key):
        school = self._add(School(school_name=f'TLive {key} {self.sfx}',
                                  code=f'TL{key}{self.sfx}'[:20], capacity=0,
                                  is_active=True))
        year = self._add(AcademicYear(school_id=school.id, name=f'Y{key}{self.sfx}',
                                      is_current=True, start_date=date(2026, 8, 1),
                                      end_date=date(2027, 6, 30)))
        self.ids[f'school_{key}'] = school.id
        self.ids[f'year_{key}'] = year.id
        return school

    def _user(self, school, label, role):
        u = User(username=f'tl{label}_{self.sfx}', email=f'tl{label}_{self.sfx}@t.test',
                 full_name=f'{label} {self.sfx}', role_id=self.role_ids[role],
                 school_id=school.id, is_active=True)
        u.set_password(PASSWORD)
        self._add(u)
        self.ids[label] = u.id
        return u

    def _emp(self, school, label, user):
        e = self._add(Employee(school_id=school.id, employee_id=f'{label}-{self.sfx}',
                               full_name=f'Drv {label}', job_title='سائق',
                               phone='07700000000', base_salary=0, status='active',
                               user_id=user.id))
        self.ids[label] = e.id
        return e

    def _route(self, school, label, emp):
        r = self._add(TransportRoute(
            school_id=school.id, name=f'R {label} {self.sfx}', driver_name=emp.full_name,
            driver_phone='07801112233', vehicle_type=f'Bus {label}',
            vehicle_number=f'V-{label}', capacity=30, status='active',
            driver_employee_id=emp.id))
        self.ids[label] = r.id
        return r

    def _student(self, school, label, parent, *routes):
        st = self._add(Student(student_id=f'{label}-{self.sfx}', full_name=f'Stu {label}',
                               school_id=school.id,
                               academic_year_id=self.ids[f'year_{"a" if school.id == self.ids["school_a"] else "b"}'],
                               status='active'))
        db.session.execute(parent_students.insert().values(user_id=parent.id,
                                                           student_id=st.id))
        for r in routes:
            self._add(StudentTransport(school_id=school.id, route_id=r.id,
                                       student_id=st.id, status='active'))
        self.ids[label] = st.id

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            sids = [self.ids['school_a'], self.ids['school_b']]
            uids = [u.id for u in User.query.execution_options(**OPTS)
                    .filter(User.school_id.in_(sids)).all()]
            for model in (TransportTrip, StudentTransport, TransportRoute):
                model.query.execution_options(**OPTS).filter(
                    model.school_id.in_(sids)).delete(synchronize_session=False)
            db.session.execute(parent_students.delete().where(
                parent_students.c.user_id.in_(uids)))
            AuditLog.query.execution_options(**OPTS).filter(
                AuditLog.user_id.in_(uids)).delete(synchronize_session=False)
            for model in (AuditLog, Student, Employee, User, AcademicYear):
                model.query.execution_options(**OPTS).filter(
                    model.school_id.in_(sids)).delete(synchronize_session=False)
            School.query.filter(School.id.in_(sids)).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _token(self, label):
        """Minted once per test and cached, so that token creation never runs
        inside an instrumented request (the counts measure the endpoint only)."""
        cache = self.__dict__.setdefault('_tokens', {})
        if label not in cache:
            with self.app.app_context():
                cache[label] = encode_token(
                    db.session.get(User, self.ids[label], execution_options=OPTS))
        return cache[label]

    def _call(self, method, label, path, **kw):
        headers = {'Authorization': f'Bearer {self._token(label)}'} if label else {}
        return getattr(self.app.test_client(), method)(f'{API}{path}', headers=headers, **kw)

    def _start(self, label='da', route='ra1'):
        resp = self._call('post', label, f'/driver/routes/{self.ids[route]}/trips/start')
        self.assertIn(resp.status_code, (200, 201), resp.get_data(as_text=True))
        return resp.get_json()['trip']['id']

    def _loc(self, trip_id, label='da', **body):
        return self._call('post', label, f'/driver/trips/{trip_id}/location', json=body)

    def _live(self, label='pa', student='st1', **params):
        return self._call('get', label,
                          f"/parent/children/{self.ids[student]}/transportation/live",
                          query_string=params)

    def _trip(self, trip_id):
        with self.app.app_context():
            t = db.session.get(TransportTrip, trip_id, execution_options=OPTS)
            return (t.status, t.latitude, t.longitude, t.location_accuracy,
                    t.location_recorded_at, t.location_updated_at)

    def _set(self, model, key, **values):
        with self.app.app_context():
            obj = db.session.get(model, self.ids[key], execution_options=OPTS)
            for k, v in values.items():
                setattr(obj, k, v)
            db.session.commit()

    def _statements(self, fn):
        """Run fn() and return (response, [SQL statements]) — instrumentation only."""
        seen = []

        def capture(conn, cursor, statement, *a):
            seen.append(' '.join(statement.split()))

        with self.app.app_context():
            engine = db.engine
        event.listen(engine, 'before_cursor_execute', capture)
        try:
            resp = fn()
        finally:
            event.remove(engine, 'before_cursor_execute', capture)
        return resp, seen

    def _trip_count(self):
        with self.app.app_context():
            return TransportTrip.query.execution_options(**OPTS).filter_by(
                school_id=self.ids['school_a']).count()

    # ── 1–2. GPS update overwrites the SAME trip row; no history, no audit ───

    def test_01_gps_update_overwrites_same_row(self):
        trip_id = self._start()
        self._token('da')
        n_trips = self._trip_count()
        resp, stmts = self._statements(lambda: self._loc(
            trip_id, latitude=33.3123, longitude=44.3921, accuracy=8.5,
            recorded_at='2026-10-01T18:30:00Z',
            school_id=self.ids['school_b'], route_id=self.ids['rb1'],
            driver_id=self.ids['e_b'], employee_id=self.ids['e_b']))   # all ignored
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertEqual(set(body), {'ok', 'location_updated_at'})
        self.assertTrue(body['location_updated_at'].endswith('+00:00'))
        status, lat, lng, acc, recorded, updated = self._trip(trip_id)
        self.assertEqual((status, lat, lng, acc), ('active', 33.3123, 44.3921, 8.5))
        self.assertEqual(recorded, datetime(2026, 10, 1, 18, 30))
        self.assertIsNotNone(updated)

        writes = [s for s in stmts if s.split(' ', 1)[0].upper() in WRITE_VERBS]
        self.assertEqual(len(writes), 1, writes)
        self.assertTrue(writes[0].upper().startswith('UPDATE TRANSPORT_TRIPS SET'))
        # The ownership EXISTS checks are correlated to the updated row.
        self.assertNotIn('FROM transport_routes, transport_trips', writes[0])
        self.assertNotIn('FROM employees, transport_trips', writes[0])
        print(f'\n[query-count] GPS update: {len(stmts)} statements '
              f'({len(writes)} write) -> {[s[:70] for s in stmts]}')
        # JWT auth (user, role, role_schools) + the single conditional UPDATE.
        self.assertLessEqual(len(stmts), 4, stmts)

        # Second ping: same row overwritten; bad recorded_at is dropped, not fatal.
        resp = self._loc(trip_id, latitude=-12.5, longitude=-77.25,
                         recorded_at='not-a-date')
        self.assertEqual(resp.status_code, 200)
        status, lat, lng, acc, recorded, updated2 = self._trip(trip_id)
        self.assertEqual((lat, lng, acc, recorded), (-12.5, -77.25, None, None))
        self.assertGreaterEqual(updated2, updated)
        self.assertEqual(self._trip_count(), n_trips)                 # no new row
        with self.app.app_context():
            audits = AuditLog.query.execution_options(**OPTS).filter_by(
                resource='transport_trip', resource_id=trip_id).all()
        self.assertEqual([a.action for a in audits], ['trip_start'])  # no GPS audit

    # ── 3. invalid payloads rejected before any write ────────────────────────

    def test_02_invalid_coordinates_rejected(self):
        trip_id = self._start()
        cases = (
            ({'longitude': 44.0}, 'invalid_latitude'),
            ({'latitude': 33.0}, 'invalid_longitude'),
            ({'latitude': 90.0001, 'longitude': 0}, 'invalid_latitude'),
            ({'latitude': 0, 'longitude': -180.5}, 'invalid_longitude'),
            ({'latitude': '33.1', 'longitude': 44.0}, 'invalid_latitude'),
            ({'latitude': True, 'longitude': 44.0}, 'invalid_latitude'),
            ({'latitude': float('nan'), 'longitude': 44.0}, 'invalid_latitude'),
            ({'latitude': 33.0, 'longitude': float('inf')}, 'invalid_longitude'),
            ({'latitude': 33.0, 'longitude': 44.0, 'accuracy': -1}, 'invalid_accuracy'),
            ({'latitude': 33.0, 'longitude': 44.0, 'accuracy': 'x'}, 'invalid_accuracy'),
        )
        for body, error in cases:
            resp = self._loc(trip_id, **body)
            self.assertEqual((resp.status_code, resp.get_json()['error']), (400, error), body)
        resp = self._call('post', 'da', f'/driver/trips/{trip_id}/location',
                          json=[33.0, 44.0])
        self.assertEqual((resp.status_code, resp.get_json()['error']), (400, 'invalid_payload'))
        resp = self._call('post', 'da', f'/driver/trips/{trip_id}/location',
                          data='lat=1', content_type='text/plain')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._trip(trip_id)[1:], (None, None, None, None, None))
        # Boundaries are valid.
        self.assertEqual(self._loc(trip_id, latitude=-90, longitude=180,
                                   accuracy=0).status_code, 200)

    # ── 4–5. other driver / other school / ended trip / route state ──────────

    def test_03_update_ownership_and_state(self):
        trip_id = self._start()
        good = {'latitude': 1.0, 'longitude': 2.0}
        for label, code, error in (('da2', 404, 'trip_not_found'),     # same school
                                   ('db', 404, 'trip_not_found'),      # other school
                                   ('pa', 403, 'forbidden')):          # wrong role
            resp = self._loc(trip_id, label, **good)
            self.assertEqual((resp.status_code, resp.get_json()['error']), (code, error))
        self.assertEqual(self._loc(trip_id, None, **good).status_code, 401)
        self.assertEqual(self._loc(999999999, **good).status_code, 404)
        self.assertEqual(self._trip(trip_id)[1], None)                 # untouched

        self._set(TransportRoute, 'ra1', status='inactive')
        resp = self._loc(trip_id, **good)
        self.assertEqual((resp.status_code, resp.get_json()['error']), (409, 'route_inactive'))
        self._set(TransportRoute, 'ra1', status='active',
                  driver_employee_id=self.ids['e_drv2'])
        resp = self._loc(trip_id, **good)
        self.assertEqual((resp.status_code, resp.get_json()['error']),
                         (409, 'route_unassigned'))
        # The newly assigned driver still cannot write the old driver's trip.
        self.assertEqual(self._loc(trip_id, 'da2', **good).status_code, 404)
        self._set(TransportRoute, 'ra1', driver_employee_id=self.ids['e_drv'])
        self.assertEqual(self._trip(trip_id)[1], None)

        self._set(Employee, 'e_drv', status='inactive')
        resp = self._loc(trip_id, **good)
        self.assertEqual((resp.status_code, resp.get_json()['error']), (403, 'driver_not_linked'))
        self._set(Employee, 'e_drv', status='active')

        self.assertEqual(self._call('post', 'da', f'/driver/trips/{trip_id}/end').status_code, 200)
        resp = self._loc(trip_id, **good)
        self.assertEqual((resp.status_code, resp.get_json()['error']), (409, 'trip_not_active'))
        self.assertEqual(self._trip(trip_id)[:2], ('ended', None))

    # ── 6–10, 12. parent live view ───────────────────────────────────────────

    def test_04_parent_live_location(self):
        old_before = self._call('get', 'pa',
                                f"/parent/children/{self.ids['st1']}/transportation").get_json()
        self.assertEqual(old_before, {'ok': True, 'transportation': {
            'driver_name': 'Drv e_drv', 'phone': '07801112233', 'vehicle_name': 'Bus ra1'}})

        # No active trip / no transport at all.
        self.assertEqual(self._live().get_json(), {'ok': True, 'active': False, 'location': None})
        self.assertEqual(self._live(student='st4').get_json(),
                         {'ok': True, 'active': False, 'location': None})

        # Active trip before the first GPS fix.
        trip_id = self._start()
        self.assertEqual(self._live().get_json(), {'ok': True, 'active': True, 'location': None})

        self._loc(trip_id, latitude=33.3123, longitude=44.3921, accuracy=8.5)
        body = self._live().get_json()
        self.assertEqual((body['ok'], body['active']), (True, True))
        loc = body['location']
        self.assertEqual(set(body), {'ok', 'active', 'location'})
        self.assertEqual(set(loc), LOC_KEYS)
        self.assertEqual((loc['latitude'], loc['longitude'], loc['accuracy']),
                         (33.3123, 44.3921, 8.5))
        self.assertTrue(loc['updated_at'].endswith('+00:00'))
        self.assertTrue(0 <= loc['age_seconds'] <= 60)

        # Forged identifiers in the query string change nothing.
        rb1_trip = self._start('db', 'rb1')
        self._loc(rb1_trip, 'db', latitude=-1.0, longitude=-2.0)
        forged = self._live(school_id=self.ids['school_b'], route_id=self.ids['rb1'],
                            trip_id=rb1_trip).get_json()
        forged['location'].pop('age_seconds')
        expected = dict(body, location=dict(loc))
        expected['location'].pop('age_seconds')
        self.assertEqual(forged, expected)

        # Another parent's child / another school's parent → 404, nothing leaked.
        for label, student in (('pb', 'st1'), ('pa', 'st2'), ('px', 'st1')):
            resp = self._live(label, student)
            self.assertEqual(resp.status_code, 404)
            self.assertNotIn('latitude', resp.get_data(as_text=True))
        self.assertEqual(self._call('get', 'da', f"/parent/children/{self.ids['st1']}"
                                                  '/transportation/live').status_code, 403)

        # Read-only + bounded statements, unaffected by other routes/trips.
        resp, stmts = self._statements(lambda: self._live())
        self.assertEqual(resp.status_code, 200)
        self.assertFalse([s for s in stmts if s.split(' ', 1)[0].upper() in WRITE_VERBS])
        with self.app.app_context():
            a = db.session.get(School, self.ids['school_a'])
            e2 = db.session.get(Employee, self.ids['e_drv2'], execution_options=OPTS)
            for i in range(6):
                self._route(a, f'noise{i}', e2)
            db.session.commit()
        self._start('da2', 'ra3')
        self._loc(self._start('da2', 'noise0'), 'da2', latitude=5.0, longitude=6.0)
        resp2, stmts2 = self._statements(lambda: self._live())
        self.assertEqual(resp2.get_json()['location']['latitude'], 33.3123)
        print(f'\n[query-count] parent live: {len(stmts)} statements (1 route) / '
              f'{len(stmts2)} statements (+6 irrelevant routes, 2 extra active trips) '
              f'-> {[s[:70] for s in stmts]}')
        self.assertEqual(len(stmts2), len(stmts))
        # JWT auth (user, role, role_schools) + parent_students + student + ONE join.
        self.assertLessEqual(len(stmts), 6, stmts)

        # Old endpoint: byte-identical contract while a live trip exists.
        self.assertEqual(self._call('get', 'pa', f"/parent/children/{self.ids['st1']}"
                                                  '/transportation').get_json(), old_before)

        # A driver unassigned mid-trip stops being shown; ended trip → inactive.
        self._set(TransportRoute, 'ra1', driver_employee_id=self.ids['e_drv2'])
        self.assertEqual(self._live().get_json()['active'], False)
        self._set(TransportRoute, 'ra1', driver_employee_id=self.ids['e_drv'])
        self._call('post', 'da', f'/driver/trips/{trip_id}/end')
        self.assertEqual(self._live().get_json(), {'ok': True, 'active': False, 'location': None})

    # ── 11. ambiguous assignment fails safely ────────────────────────────────

    def test_05_ambiguous_transport_assignment(self):
        trip_id = self._start()
        self._loc(trip_id, latitude=10.0, longitude=20.0)
        resp = self._live(student='st3')
        self.assertEqual((resp.status_code, resp.get_json()['error']),
                         (409, 'ambiguous_transport_assignment'))
        self.assertNotIn('latitude', resp.get_data(as_text=True))
        # Only ACTIVE routes count: with ra2 inactive the child is unambiguous.
        self._set(TransportRoute, 'ra2', status='inactive')
        body = self._live(student='st3').get_json()
        self.assertEqual((body['active'], body['location']['latitude']), (True, 10.0))


if __name__ == '__main__':
    unittest.main()

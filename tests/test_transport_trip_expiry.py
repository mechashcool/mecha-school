"""
Transport trip auto-expiration (app/services/transport_trip_expiry.py).

An active trip is ended when COALESCE(location_updated_at, started_at) is at
least 2 h old. One UPDATE … RETURNING per check, every 600 s.

Fixture (per test, unique suffix):
  school A  driver DA (e_drv) → routes ra1..ra4;  parent PA → st1 (on ra1)
  school B  driver DB (e_b)   → routes rb1, rb2

Requires an isolated, approved test database (tests/conftest.py guard).
"""
import os
import threading
import time
import unittest
from datetime import date, datetime, timedelta
from unittest import mock
from uuid import uuid4

from sqlalchemy import event, text

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (db, AcademicYear, AuditLog, Employee, Role, School,
                        Student, StudentTransport, TransportRoute,
                        TransportTrip, User, parent_students)
from app.services import transport_trip_expiry as expiry

OPTS = {'bypass_tenant_scope': True}
API = '/api/mobile/v1'
H = timedelta(hours=1)
M = timedelta(minutes=1)


class TransportTripExpiryTest(unittest.TestCase):

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
            for key in ('ra1', 'ra2', 'ra3', 'ra4'):
                self._route(a, key, e_drv)
            self._student(a, 'st1', self._user(a, 'pa', 'parent'), 'ra1')
            b = self._school('b')
            e_b = self._emp(b, 'e_b', self._user(b, 'db', 'driver'))
            self._route(b, 'rb1', e_b)
            self._route(b, 'rb2', e_b)
            db.session.commit()

    def _add(self, obj):
        db.session.add(obj)
        db.session.flush()
        return obj

    def _school(self, key):
        school = self._add(School(school_name=f'TExp {key} {self.sfx}',
                                  code=f'TX{key}{self.sfx}'[:20], capacity=0,
                                  is_active=True))
        year = self._add(AcademicYear(school_id=school.id, name=f'Y{key}{self.sfx}',
                                      is_current=True, start_date=date(2026, 8, 1),
                                      end_date=date(2027, 6, 30)))
        self.ids[f'school_{key}'] = school.id
        self.ids[f'year_{key}'] = year.id
        return school

    def _user(self, school, label, role):
        u = User(username=f'tx{label}_{self.sfx}', email=f'tx{label}_{self.sfx}@t.test',
                 full_name=f'{label} {self.sfx}', role_id=self.role_ids[role],
                 school_id=school.id, is_active=True)
        u.set_password('Test1234!')
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

    def _student(self, school, label, parent, route):
        st = self._add(Student(student_id=f'{label}-{self.sfx}', full_name=f'Stu {label}',
                               school_id=school.id, academic_year_id=self.ids['year_a'],
                               status='active'))
        db.session.execute(parent_students.insert().values(user_id=parent.id,
                                                           student_id=st.id))
        self._add(StudentTransport(school_id=school.id, route_id=self.ids[route],
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

    def _trip(self, route, driver, started_at, location_updated_at=None, **extra):
        with self.app.app_context():
            t = TransportTrip(school_id=self.ids[f'school_{route[1]}'],
                              route_id=self.ids[route], driver_employee_id=self.ids[driver],
                              status=extra.pop('status', 'active'), started_at=started_at,
                              location_updated_at=location_updated_at, **extra)
            db.session.add(t)
            db.session.commit()
            return t.id

    def _row(self, trip_id):
        with self.app.app_context():
            t = db.session.get(TransportTrip, trip_id, execution_options=OPTS)
            return t.status, t.ended_at, t.latitude, t.longitude, t.location_updated_at

    def _set_trip(self, trip_id, **values):
        with self.app.app_context():
            t = db.session.get(TransportTrip, trip_id, execution_options=OPTS)
            for k, v in values.items():
                setattr(t, k, v)
            db.session.commit()

    def _expire(self, now=None):
        """Run one check and return (ended_count, [SQL statements])."""
        seen = []

        def capture(conn, cursor, statement, *a):
            seen.append(' '.join(statement.split()))

        with self.app.app_context():
            engine = db.engine
            event.listen(engine, 'before_cursor_execute', capture)
            try:
                count = expiry.expire_inactive_trips(now)
            finally:
                event.remove(engine, 'before_cursor_execute', capture)
                db.session.remove()
        return count, seen

    def _token(self, label):
        cache = self.__dict__.setdefault('_tokens', {})
        if label not in cache:
            with self.app.app_context():
                cache[label] = encode_token(
                    db.session.get(User, self.ids[label], execution_options=OPTS))
        return cache[label]

    def _call(self, method, label, path, **kw):
        headers = {'Authorization': f'Bearer {self._token(label)}'}
        return getattr(self.app.test_client(), method)(f'{API}{path}', headers=headers, **kw)

    def _audit_count(self, action, trip_id):
        with self.app.app_context():
            return (AuditLog.query.execution_options(**OPTS)
                    .filter_by(action=action, resource='transport_trip',
                               resource_id=trip_id).count())

    def _concurrent(self, trip_id, sql, params):
        """Hold an uncommitted UPDATE on the trip row in connection A, run the
        expiry check in another thread (it must block on the row lock), then
        commit A. Returns the check's ended count."""
        result = {}

        def worker():
            with self.app.app_context():
                try:
                    result['count'] = expiry.expire_inactive_trips()
                finally:
                    db.session.remove()

        with self.app.app_context():
            conn = db.engine.connect()
            trans = conn.begin()
            try:
                conn.execute(text(sql), {'id': trip_id, **params})
                t = threading.Thread(target=worker, daemon=True)
                t.start()
                time.sleep(1.0)
                self.assertTrue(t.is_alive(), 'expiry did not wait for the row lock')
            finally:
                trans.commit()
                conn.close()
        t.join(10)
        self.assertFalse(t.is_alive(), 'expiry still blocked after commit')
        return result['count']

    # ── tests ─────────────────────────────────────────────────────────────────

    def test_01_threshold_rules_one_statement_idempotent(self):
        now = datetime.utcnow().replace(microsecond=0)
        recent = self._trip('ra1', 'e_drv', now - 5 * H, now - 30 * M)       # location wins over start
        stale = self._trip('ra2', 'e_drv', now - 5 * H, now - 2 * H - M,
                           latitude=33.3, longitude=44.4)
        noloc_old = self._trip('ra3', 'e_drv', now - 2 * H - M)              # start time used
        noloc_new = self._trip('ra4', 'e_drv', now - 1 * H)
        b_recent = self._trip('rb1', 'e_b', now - 3 * H, now - 10 * M)
        boundary = self._trip('rb2', 'e_b', now - 3 * H, now - 2 * H)        # exactly 2 h → ends
        ended_at = now - 4 * H
        already = self._trip('ra1', 'e_drv', now - 6 * H, status='ended', ended_at=ended_at)

        count, stmts = self._expire(now)

        self.assertEqual(count, 3)
        self.assertEqual(len(stmts), 1, stmts)
        self.assertTrue(stmts[0].startswith('UPDATE transport_trips SET'), stmts[0])
        self.assertIn('RETURNING', stmts[0])
        for tid in (recent, noloc_new, b_recent):
            self.assertEqual(self._row(tid)[:2], ('active', None))
        for tid in (stale, noloc_old, boundary):
            self.assertEqual(self._row(tid)[:2], ('ended', now))
        # Stored location untouched (existing retention behaviour).
        self.assertEqual(self._row(stale)[2:], (33.3, 44.4, now - 2 * H - M))
        # An already-ended trip is never rewritten.
        self.assertEqual(self._row(already)[:2], ('ended', ended_at))

        # Second check: nothing left to end, still one statement.
        count, stmts = self._expire(now)
        self.assertEqual((count, len(stmts)), (0, 1))

    def test_02_api_contract_before_and_after_expiry(self):
        start = self._call('post', 'da', f'/driver/routes/{self.ids["ra1"]}/trips/start')
        self.assertEqual(start.status_code, 201)
        trip_id = start.get_json()['trip']['id']
        loc = self._call('post', 'da', f'/driver/trips/{trip_id}/location',
                         json={'latitude': 33.31, 'longitude': 44.36})
        self.assertEqual(loc.status_code, 200)

        # Recent location → stays active and visible to the parent.
        self.assertEqual(self._expire()[0], 0)
        live = self._call('get', 'pa', f'/parent/children/{self.ids["st1"]}/transportation/live')
        self.assertEqual(live.status_code, 200)
        self.assertTrue(live.get_json()['active'])
        self.assertIsNotNone(live.get_json()['location'])

        # 2 h + without a location → ended by the next check.
        self._set_trip(trip_id, location_updated_at=datetime.utcnow() - 2 * H - M)
        self.assertEqual(self._expire()[0], 1)
        ended_at = self._row(trip_id)[1]

        loc = self._call('post', 'da', f'/driver/trips/{trip_id}/location',
                         json={'latitude': 33.32, 'longitude': 44.37})
        self.assertEqual(loc.status_code, 409)
        self.assertEqual(loc.get_json(), {'ok': False, 'error': 'trip_not_active'})

        live = self._call('get', 'pa', f'/parent/children/{self.ids["st1"]}/transportation/live')
        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.get_json(), {'ok': True, 'active': False, 'location': None})

        routes = self._call('get', 'da', '/driver/routes').get_json()['routes']
        self.assertIsNone(next(r for r in routes if r['id'] == self.ids['ra1'])['active_trip'])

        # Manual end after auto-expiry: existing idempotent path, no write, no audit.
        end = self._call('post', 'da', f'/driver/trips/{trip_id}/end')
        self.assertEqual(end.status_code, 200)
        body = end.get_json()['trip']
        self.assertEqual((body['id'], body['status']), (trip_id, 'ended'))
        self.assertEqual(self._row(trip_id)[1], ended_at)
        self.assertEqual(self._audit_count('trip_end', trip_id), 0)

        # The route can be started again.
        again = self._call('post', 'da', f'/driver/routes/{self.ids["ra1"]}/trips/start')
        self.assertEqual(again.status_code, 201)
        self.assertNotEqual(again.get_json()['trip']['id'], trip_id)

    def test_03_concurrent_location_update_wins(self):
        now = datetime.utcnow()
        trip_id = self._trip('ra1', 'e_drv', now - 3 * H, now - 2 * H - M)
        count = self._concurrent(
            trip_id,
            'UPDATE transport_trips SET location_updated_at = :t WHERE id = :id',
            {'t': datetime.utcnow()})
        self.assertEqual(count, 0)
        self.assertEqual(self._row(trip_id)[0], 'active')

    def test_04_concurrent_manual_end_is_not_overwritten(self):
        now = datetime.utcnow().replace(microsecond=0)
        trip_id = self._trip('ra1', 'e_drv', now - 3 * H)
        manual_at = now - 5 * M
        count = self._concurrent(
            trip_id,
            "UPDATE transport_trips SET status = 'ended', ended_at = :t WHERE id = :id",
            {'t': manual_at})
        self.assertEqual(count, 0)
        self.assertEqual(self._row(trip_id)[:2], ('ended', manual_at))

    def test_06_manual_end_loses_race_to_expiry(self):
        """Real endpoint: its read sees 'active', then the expiry commits before
        its write. The manual end must not overwrite ended_at nor audit."""
        start = self._call('post', 'da', f'/driver/routes/{self.ids["ra1"]}/trips/start')
        trip_id = start.get_json()['trip']['id']
        expired_at = datetime.utcnow().replace(microsecond=0) - 3 * M
        self._token('da')
        result = {}

        def manual_end():
            result['resp'] = self._call('post', 'da', f'/driver/trips/{trip_id}/end')

        with self.app.app_context():
            conn = db.engine.connect()
            trans = conn.begin()
            try:
                # Uncommitted expiry of this trip — holds the row lock.
                conn.execute(text("UPDATE transport_trips SET status = 'ended', "
                                  "ended_at = :t WHERE id = :id AND status = 'active'"),
                             {'t': expired_at, 'id': trip_id})
                t = threading.Thread(target=manual_end, daemon=True)
                t.start()
                time.sleep(1.0)
                self.assertTrue(t.is_alive(), 'manual end did not wait for the row lock')
            finally:
                trans.commit()
                conn.close()
        t.join(10)
        self.assertFalse(t.is_alive(), 'manual end still blocked after commit')

        resp = result['resp']
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()['trip']
        self.assertEqual((body['id'], body['status']), (trip_id, 'ended'))
        self.assertEqual(body['ended_at'], expired_at.isoformat() + '+00:00')
        self.assertEqual(self._row(trip_id)[:2], ('ended', expired_at))
        self.assertEqual(self._audit_count('trip_end', trip_id), 0)

    def test_05_scheduler_gate_and_interval(self):
        self.assertEqual(expiry.CHECK_INTERVAL_SECONDS, 600)
        self.assertEqual(expiry.INACTIVITY_LIMIT, timedelta(hours=2))
        # The testing app never starts it (lifecycle gate).
        self.assertFalse(any(t.name == 'transport-trip-expiry' and t.is_alive()
                             for t in threading.enumerate()))
        try:
            with mock.patch.object(expiry.threading, 'Thread') as thread_cls:
                with mock.patch.dict(os.environ, {'TRANSPORT_TRIP_EXPIRY_DISABLED': 'true'}):
                    expiry.start_transport_trip_expiry_scheduler(self.app)
                thread_cls.assert_not_called()

                with mock.patch.dict(os.environ, {'TRANSPORT_TRIP_EXPIRY_DISABLED': ''}):
                    expiry.start_transport_trip_expiry_scheduler(self.app)
                thread_cls.assert_called_once()
                self.assertEqual(thread_cls.call_args.kwargs['args'], (self.app, 600))
                self.assertTrue(thread_cls.call_args.kwargs['daemon'])
        finally:
            expiry._scheduler_thread = None


if __name__ == '__main__':
    unittest.main()

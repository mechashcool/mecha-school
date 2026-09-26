"""Baseline coverage for existing attendance behaviour — pre-B2.

Attendance is the pilot resource for change capture, and before B1.1 it had NO
direct test coverage at all. These tests describe what the system does TODAY so
that adding a capture hook in B2 can be shown not to have altered it.

They are deliberately descriptive, not aspirational: where current behaviour is
surprising it is pinned WITH a comment explaining why, not "fixed". Two such
cases are recorded here:

  * the mobile parent endpoint reads attendance with ``include_all_years=True``
    and filters by DATE, so it deliberately spans academic years;
  * ``StudentAttendance`` IS year-scoped (unlike ``Student``), so an ORM query
    without that opt-in is confined to the active year.

Nothing here enables a sync flag, and nothing asserts a change-journal row —
capture does not exist yet.
"""
import json
import unittest
from datetime import date, time, timedelta
from uuid import uuid4

from sqlalchemy import text

from app import create_app
from app.models import (
    db, AcademicYear, Grade, Notification, Role, School, Section, Student,
    StudentAttendance, User, parent_students,
)


class AttendanceBaselineTest(unittest.TestCase):
    """Two schools, each with a student, a linked parent, and an admin."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.ids = {}
        with self.app.app_context():
            admin_role = Role.query.filter_by(name='admin').first()
            parent_role = Role.query.filter_by(name='parent').first()
            self.assertIsNotNone(admin_role, 'seed roles before running')
            self.assertIsNotNone(parent_role, 'seed roles before running')

            for tag in ('a', 'b'):
                school = School(school_name=f'Att {tag} {self.suffix}',
                                code=f'AT{tag.upper()}{self.suffix[:6]}',
                                capacity=0, is_active=True)
                db.session.add(school)
                db.session.flush()

                year = AcademicYear(school_id=school.id,
                                    name=f'Y {tag} {self.suffix}',
                                    start_date=date(2025, 8, 1),
                                    end_date=date(2026, 6, 30),
                                    is_current=True)
                prev = AcademicYear(school_id=school.id,
                                    name=f'Yprev {tag} {self.suffix}',
                                    start_date=date(2024, 8, 1),
                                    end_date=date(2025, 6, 30),
                                    is_current=False)
                db.session.add_all([year, prev])
                db.session.flush()

                grade = Grade(school_id=school.id, academic_year_id=year.id,
                              name=f'G{tag}{self.suffix[:4]}')
                db.session.add(grade)
                db.session.flush()

                section = Section(school_id=school.id, academic_year_id=year.id,
                                  grade_id=grade.id,
                                  name=f'S{tag}{self.suffix[:4]}', capacity=30)
                db.session.add(section)
                db.session.flush()

                student = Student(student_id=f'ATT-{tag.upper()}-{self.suffix}',
                                  full_name=f'Student {tag} {self.suffix}',
                                  date_of_birth=date(2015, 1, 1), gender='male',
                                  school_id=school.id, academic_year_id=year.id,
                                  section_id=section.id, status='active')
                db.session.add(student)
                db.session.flush()

                parent = User(username=f'att_p_{tag}_{self.suffix}',
                              email=f'att_p_{tag}_{self.suffix}@example.test',
                              full_name=f'Parent {tag}', role_id=parent_role.id,
                              school_id=school.id, is_active=True)
                parent.set_password('Password123')
                admin = User(username=f'att_ad_{tag}_{self.suffix}',
                             email=f'att_ad_{tag}_{self.suffix}@example.test',
                             full_name=f'Admin {tag}', role_id=admin_role.id,
                             school_id=school.id, is_active=True)
                admin.set_password('Password123')
                db.session.add_all([parent, admin])
                db.session.flush()

                db.session.execute(parent_students.insert().values(
                    user_id=parent.id, student_id=student.id))

                self.ids.update({
                    f'school_{tag}': school.id, f'year_{tag}': year.id,
                    f'prev_year_{tag}': prev.id, f'grade_{tag}': grade.id,
                    f'section_{tag}': section.id, f'student_{tag}': student.id,
                    f'parent_{tag}': parent.id, f'admin_{tag}': admin.id,
                })
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            opts = {'bypass_tenant_scope': True}
            sids = [self.ids['school_a'], self.ids['school_b']]
            db.session.execute(text(
                'DELETE FROM student_attendance WHERE school_id = ANY(:s)'),
                {'s': sids})
            db.session.execute(text(
                'DELETE FROM notifications WHERE school_id = ANY(:s)'),
                {'s': sids})
            db.session.execute(text(
                'DELETE FROM parent_students WHERE student_id = ANY(:s)'),
                {'s': [self.ids['student_a'], self.ids['student_b']]})
            for model, keys in ((Student, ['student_a', 'student_b']),
                                (User, ['parent_a', 'parent_b',
                                        'admin_a', 'admin_b']),
                                (Section, ['section_a', 'section_b']),
                                (Grade, ['grade_a', 'grade_b']),
                                (AcademicYear, ['year_a', 'year_b',
                                                'prev_year_a', 'prev_year_b']),
                                (School, ['school_a', 'school_b'])):
                for key in keys:
                    row = db.session.get(model, self.ids[key],
                                         execution_options=opts)
                    if row is not None:
                        db.session.delete(row)
                db.session.flush()
            db.session.commit()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _token(self, user_id):
        from app.blueprints.mobile_api.utils import encode_token
        with self.app.app_context():
            user = db.session.get(User, user_id,
                                  execution_options={'bypass_tenant_scope': True})
            return encode_token(user)

    def _get(self, path, user_id):
        token = self._token(user_id)
        client = self.app.test_client()
        return client.get(path, headers={'Authorization': f'Bearer {token}'})

    def _add_attendance(self, tag, on_date, status='present', source='manual',
                        year_key=None):
        with self.app.app_context():
            row = StudentAttendance(
                school_id=self.ids[f'school_{tag}'],
                academic_year_id=self.ids[year_key or f'year_{tag}'],
                student_id=self.ids[f'student_{tag}'],
                date=on_date, status=status, source=source)
            db.session.add(row)
            db.session.commit()
            return row.id

    # ── 1. Authorized mobile reads ───────────────────────────────────────────

    def test_parent_reads_own_child_attendance(self):
        self._add_attendance('a', date.today(), status='present')
        resp = self._get(
            f"/api/mobile/v1/parent/children/{self.ids['student_a']}/attendance",
            self.ids['parent_a'])
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['student_id'], self.ids['student_a'])
        self.assertEqual(body['summary']['total'], 1)
        self.assertEqual(body['summary']['present'], 1)
        self.assertEqual(len(body['records']), 1)

    def test_response_shape_is_stable(self):
        """Pin the exact envelope an existing mobile build parses."""
        self._add_attendance('a', date.today(), status='late')
        body = self._get(
            f"/api/mobile/v1/parent/children/{self.ids['student_a']}/attendance",
            self.ids['parent_a']).get_json()

        self.assertEqual(set(body), {'ok', 'student_id', 'range', 'summary',
                                     'records'})
        self.assertEqual(set(body['range']), {'start', 'end'})
        self.assertEqual(set(body['summary']),
                         {'total', 'present', 'absent', 'late', 'on_leave',
                          'excused', 'att_pct'})
        self.assertEqual(set(body['records'][0]),
                         {'date', 'status', 'check_in', 'check_out', 'source',
                          'notes'})

    def test_attendance_percentage_excludes_on_leave(self):
        """on_leave is not billable — pinned because it is easy to regress."""
        today = date.today()
        self._add_attendance('a', today, status='present')
        self._add_attendance('a', today - timedelta(days=1), status='on_leave')
        summary = self._get(
            f"/api/mobile/v1/parent/children/{self.ids['student_a']}/attendance",
            self.ids['parent_a']).get_json()['summary']
        self.assertEqual(summary['total'], 2)
        self.assertEqual(summary['on_leave'], 1)
        self.assertEqual(summary['att_pct'], 100.0,
                         'one present + one on_leave must be 100%, not 50%')

    # ── 2. Authorization / isolation on the read path ────────────────────────

    def test_parent_cannot_read_another_parents_child_same_school(self):
        """Being a parent in the school is not enough — the link is required."""
        with self.app.app_context():
            role = Role.query.filter_by(name='parent').first()
            other = User(username=f'att_p_x_{self.suffix}',
                         email=f'att_p_x_{self.suffix}@example.test',
                         full_name='Unlinked Parent', role_id=role.id,
                         school_id=self.ids['school_a'], is_active=True)
            other.set_password('Password123')
            db.session.add(other)
            db.session.commit()
            other_id = other.id
        try:
            self._add_attendance('a', date.today())
            resp = self._get(
                f"/api/mobile/v1/parent/children/{self.ids['student_a']}/attendance",
                other_id)
            self.assertEqual(resp.status_code, 404,
                             'an unlinked parent must not read the record')
            self.assertNotIn(str(self.ids['student_a']),
                             resp.get_data(as_text=True))
        finally:
            with self.app.app_context():
                row = db.session.get(User, other_id,
                                     execution_options={'bypass_tenant_scope': True})
                if row:
                    db.session.delete(row)
                    db.session.commit()

    def test_parent_cannot_read_across_schools(self):
        self._add_attendance('b', date.today())
        resp = self._get(
            f"/api/mobile/v1/parent/children/{self.ids['student_b']}/attendance",
            self.ids['parent_a'])
        self.assertEqual(resp.status_code, 404)
        blob = resp.get_data(as_text=True)
        self.assertNotIn(str(self.ids['student_b']), blob)
        self.assertNotIn(str(self.ids['school_b']), blob)

    def test_unauthenticated_request_is_rejected(self):
        resp = self.app.test_client().get(
            f"/api/mobile/v1/parent/children/{self.ids['student_a']}/attendance")
        self.assertEqual(resp.status_code, 401)

    def test_wrong_role_is_rejected(self):
        """An admin token must not satisfy a parent-only endpoint."""
        resp = self._get(
            f"/api/mobile/v1/parent/children/{self.ids['student_a']}/attendance",
            self.ids['admin_a'])
        self.assertIn(resp.status_code, (401, 403))

    def test_inactive_account_is_rejected_even_with_a_valid_token(self):
        """jwt_required re-loads the user row per request."""
        token = self._token(self.ids['parent_a'])
        with self.app.app_context():
            user = db.session.get(User, self.ids['parent_a'],
                                  execution_options={'bypass_tenant_scope': True})
            user.is_active = False
            db.session.commit()
        resp = self.app.test_client().get(
            f"/api/mobile/v1/parent/children/{self.ids['student_a']}/attendance",
            headers={'Authorization': f'Bearer {token}'})
        self.assertIn(resp.status_code, (401, 403))

    def test_invalid_date_format_is_rejected_safely(self):
        resp = self._get(
            f"/api/mobile/v1/parent/children/{self.ids['student_a']}"
            f"/attendance?start=not-a-date", self.ids['parent_a'])
        body = resp.get_json()
        self.assertFalse(body['ok'])
        self.assertEqual(set(body), {'ok', 'error'})
        self.assertNotIn('Traceback', resp.get_data(as_text=True))

    # ── 3. Academic-year behaviour ───────────────────────────────────────────

    def test_student_attendance_is_year_scoped_in_the_orm(self):
        """Unlike Student, StudentAttendance IS year-scoped."""
        today = date.today()
        self._add_attendance('a', today, year_key='year_a')
        self._add_attendance('a', today - timedelta(days=400),
                             year_key='prev_year_a')
        with self.app.app_context():
            scoped = (StudentAttendance.query
                      .execution_options(bypass_tenant_scope=True)
                      .filter_by(student_id=self.ids['student_a'],
                                 academic_year_id=self.ids['year_a'])
                      .count())
            all_years = (StudentAttendance.query
                         .execution_options(bypass_tenant_scope=True)
                         .filter_by(student_id=self.ids['student_a'])
                         .count())
        self.assertEqual(scoped, 1)
        self.assertEqual(all_years, 2)

    def test_mobile_parent_endpoint_spans_years_by_date_on_purpose(self):
        """Documented current behaviour: include_all_years + a date filter.

        This is the "explicit, authorized historical behaviour" the endpoint
        already implements — it is not a leak: _assert_owns_student still binds
        the read to a linked child in the parent's own school.
        """
        today = date.today()
        self._add_attendance('a', today, year_key='year_a')
        self._add_attendance('a', today - timedelta(days=200),
                             year_key='prev_year_a')
        body = self._get(
            f"/api/mobile/v1/parent/children/{self.ids['student_a']}"
            f"/attendance?start={(today - timedelta(days=365)).isoformat()}"
            f"&end={today.isoformat()}", self.ids['parent_a']).get_json()
        self.assertEqual(body['summary']['total'], 2,
                         'both years are returned when the date range covers them')

    def test_range_is_capped_at_365_days(self):
        today = date.today()
        body = self._get(
            f"/api/mobile/v1/parent/children/{self.ids['student_a']}"
            f"/attendance?start=2000-01-01&end={today.isoformat()}",
            self.ids['parent_a']).get_json()
        start = date.fromisoformat(body['range']['start'])
        self.assertEqual((today - start).days, 365)

    # ── 4. Write paths ───────────────────────────────────────────────────────

    def test_service_punch_creates_a_record(self):
        """app/services/attendance_service.py — the device/API punch writer."""
        from app.services.attendance_service import process_attendance_punch
        from datetime import datetime as _dt
        with self.app.app_context():
            student = db.session.get(Student, self.ids['student_a'],
                                     execution_options={'bypass_tenant_scope': True})
            school = db.session.get(School, self.ids['school_a'],
                                    execution_options={'bypass_tenant_scope': True})
            punch = _dt.combine(date.today(), time(7, 30))
            process_attendance_punch(student, school, punch, source='api')
            db.session.commit()

            rows = (StudentAttendance.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter_by(student_id=self.ids['student_a']).all())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].school_id, self.ids['school_a'])
            self.assertIsNotNone(rows[0].academic_year_id,
                                 'writer must stamp the academic year')

    def test_update_path_changes_status_in_place(self):
        row_id = self._add_attendance('a', date.today(), status='absent')
        with self.app.app_context():
            row = db.session.get(StudentAttendance, row_id,
                                 execution_options={'bypass_tenant_scope': True})
            row.status = 'present'
            row.check_in = time(7, 45)
            db.session.commit()

            fresh = db.session.get(StudentAttendance, row_id,
                                   execution_options={'bypass_tenant_scope': True})
            self.assertEqual(fresh.status, 'present')
            self.assertEqual(fresh.check_in, time(7, 45))

    def test_orm_delete_path_removes_the_record(self):
        """The shape used by leave revocation (session.delete)."""
        row_id = self._add_attendance('a', date.today())
        with self.app.app_context():
            row = db.session.get(StudentAttendance, row_id,
                                 execution_options={'bypass_tenant_scope': True})
            db.session.delete(row)
            db.session.commit()
            self.assertIsNone(db.session.get(
                StudentAttendance, row_id,
                execution_options={'bypass_tenant_scope': True}))

    def test_bulk_sql_delete_path_removes_records(self):
        """The shape used by holiday cleanup and student hard-delete (raw SQL).

        This path bypasses the ORM entirely, which is exactly why B2 cannot
        rely on an after_flush listener alone.
        """
        today = date.today()
        self._add_attendance('a', today)
        self._add_attendance('a', today - timedelta(days=1))
        with self.app.app_context():
            db.session.execute(
                text('DELETE FROM student_attendance WHERE student_id = :sid'),
                {'sid': self.ids['student_a']})
            db.session.commit()
            remaining = (StudentAttendance.query
                         .execution_options(bypass_tenant_scope=True)
                         .filter_by(student_id=self.ids['student_a']).count())
        self.assertEqual(remaining, 0)

    def test_cross_school_attendance_write_is_rejected(self):
        """A record must not link a student to another school's id."""
        with self.app.app_context():
            bad = StudentAttendance(
                school_id=self.ids['school_b'],
                academic_year_id=self.ids['year_b'],
                student_id=self.ids['student_a'],      # student is in school A
                date=date.today(), status='present', source='manual')
            db.session.add(bad)
            with self.assertRaises(Exception):
                db.session.commit()
            db.session.rollback()

    def test_rolled_back_attendance_write_leaves_no_row(self):
        with self.app.app_context():
            before = (StudentAttendance.query
                      .execution_options(bypass_tenant_scope=True)
                      .filter_by(student_id=self.ids['student_a']).count())
            row = StudentAttendance(
                school_id=self.ids['school_a'],
                academic_year_id=self.ids['year_a'],
                student_id=self.ids['student_a'], date=date.today(),
                status='absent', source='manual')
            db.session.add(row)
            db.session.flush()
            db.session.rollback()

            after = (StudentAttendance.query
                     .execution_options(bypass_tenant_scope=True)
                     .filter_by(student_id=self.ids['student_a']).count())
        self.assertEqual(before, after)

    # ── 5. Notification behaviour (must stay independent of sync) ────────────

    def test_absence_notification_targets_only_linked_parents(self):
        from app.blueprints.attendance import _notify_absent_parents
        with self.app.app_context():
            student = db.session.get(Student, self.ids['student_a'],
                                     execution_options={'bypass_tenant_scope': True})
            _notify_absent_parents(student, self.ids['school_a'],
                                   date.today().isoformat(), source='manual')
            db.session.commit()

            notes = (Notification.query
                     .execution_options(bypass_tenant_scope=True)
                     .filter_by(school_id=self.ids['school_a']).all())
            self.assertGreaterEqual(len(notes), 1,
                                    'an in-app row must be created, not only a push')
            recipients = {n.target_user_id for n in notes}
            self.assertIn(self.ids['parent_a'], recipients)
            self.assertNotIn(self.ids['parent_b'], recipients,
                             'another school\'s parent must never be targeted')

            # It must be a TARGETED notification, not a role-wide broadcast:
            # target_role would deliver the absence to every parent in the
            # school, disclosing one family's record to all the others.
            for note in notes:
                self.assertIsNone(
                    note.target_role,
                    'an absence must never become a parent-role broadcast')
                self.assertIsNotNone(note.target_user_id,
                                     'the recipient must be explicit')
                self.assertEqual(note.ntype, 'attendance')

            other_school = (Notification.query
                            .execution_options(bypass_tenant_scope=True)
                            .filter_by(school_id=self.ids['school_b']).count())
            self.assertEqual(other_school, 0)

    def test_notification_is_created_even_though_fcm_is_disabled(self):
        """Delivery must not depend on the push channel being available."""
        import app.services.fcm_service as fcm
        self.assertFalse(fcm.is_enabled(), 'tests must run with FCM off')

        from app.blueprints.attendance import _notify_absent_parents
        with self.app.app_context():
            student = db.session.get(Student, self.ids['student_a'],
                                     execution_options={'bypass_tenant_scope': True})
            _notify_absent_parents(student, self.ids['school_a'],
                                   date.today().isoformat())
            db.session.commit()
            count = (Notification.query
                     .execution_options(bypass_tenant_scope=True)
                     .filter_by(school_id=self.ids['school_a']).count())
        self.assertGreaterEqual(count, 1)

    def test_absence_notification_needs_no_sync_machinery(self):
        """Pins the independence requirement: no flag, no journal involved."""
        self.assertFalse(self.app.config['SYNC_JOURNAL_ENABLED'])
        self.assertFalse(self.app.config['SYNC_SIGNAL_ENABLED'])

        from app.blueprints.attendance import _notify_absent_parents
        with self.app.app_context():
            before = db.session.execute(
                text('SELECT count(*) FROM change_journal')).scalar()
            student = db.session.get(Student, self.ids['student_a'],
                                     execution_options={'bypass_tenant_scope': True})
            _notify_absent_parents(student, self.ids['school_a'],
                                   date.today().isoformat())
            db.session.commit()
            after = db.session.execute(
                text('SELECT count(*) FROM change_journal')).scalar()
            notes = (Notification.query
                     .execution_options(bypass_tenant_scope=True)
                     .filter_by(school_id=self.ids['school_a']).count())
        self.assertGreaterEqual(notes, 1, 'the notification still happened')
        self.assertEqual(before, after, 'and wrote nothing to the journal')

    def test_attendance_write_creates_no_journal_row_while_disabled(self):
        with self.app.app_context():
            before = db.session.execute(
                text('SELECT count(*) FROM change_journal')).scalar()
        self._add_attendance('a', date.today(), status='absent')
        with self.app.app_context():
            after = db.session.execute(
                text('SELECT count(*) FROM change_journal')).scalar()
        self.assertEqual(before, after)

    def test_sync_principal_state_stays_empty(self):
        """B1.1 adds the table but no writer — it must be completely inert."""
        with self.app.app_context():
            count = db.session.execute(
                text('SELECT count(*) FROM sync_principal_state')).scalar()
        self.assertEqual(count, 0, 'no row may be created for existing users')

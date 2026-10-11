"""
Timetable create: time-range validation and the overlap ("time reserved") check.

  POST /schedules/create

  * malformed / inverted / zero-length times are rejected before any write
  * standard half-open overlap: back-to-back lessons are allowed
  * a legacy inverted row (end <= start) cannot reserve the whole morning
  * the conflict message names the blocking lesson
  * teacher_id must belong to the schedule's school
  * after a failure the add form keeps the submitted day / start / end

Fixture (per test, unique suffix):
  School A: year, grade, sections S1 + S2, subject, employee TA, school_admin AA
  School B: year, employee TB

Requires an isolated, approved test database (tests/conftest.py guard).
"""
import unittest
from datetime import date, time
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from app import create_app
from app.models import (db, AcademicYear, AuditLog, Employee, Grade, Role,
                        Schedule, School, Section, Subject, User)

OPTS = {'bypass_tenant_scope': True}
PASSWORD = 'Test1234!'


class ScheduleTimeConflictTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['RATELIMIT_ENABLED'] = False
        with cls.app.app_context():
            cls.admin_role_id = Role.query.filter_by(name='school_admin').first().id

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        with self.app.app_context():
            a = self._school('a')
            b = self._school('b')
            ya = self.ids['year_a']

            grade = Grade(school_id=a.id, academic_year_id=ya, name=f'G {self.sfx}')
            db.session.add(grade)
            db.session.flush()
            self.ids['grade'] = grade.id
            for label in ('s1', 's2'):
                sec = Section(school_id=a.id, academic_year_id=ya, grade_id=grade.id,
                              name=f'{label}{self.sfx[:4]}', capacity=30)
                db.session.add(sec)
                db.session.flush()
                self.ids[label] = sec.id
            subj = Subject(school_id=a.id, academic_year_id=ya, grade_id=grade.id,
                           name=f'رياضيات {self.sfx}')
            db.session.add(subj)
            db.session.flush()
            self.ids['subject'] = subj.id

            self._emp(a, 'ta')
            self._emp(b, 'tb')
            u = User(username=f'stcaa_{self.sfx}', full_name=f'AA {self.sfx}',
                     role_id=self.admin_role_id, school_id=a.id, is_active=True)
            u.set_password(PASSWORD)
            db.session.add(u)
            db.session.commit()

    def _school(self, label):
        s = School(school_name=f'STC {label} {self.sfx}', code=f'STC{label}{self.sfx}',
                   capacity=0, is_active=True)
        db.session.add(s)
        db.session.flush()
        y = AcademicYear(school_id=s.id, name=f'Y{label}{self.sfx}', is_current=True,
                         start_date=date(2026, 8, 1), end_date=date(2027, 6, 30))
        db.session.add(y)
        db.session.flush()
        self.ids[f'school_{label}'] = s.id
        self.ids[f'year_{label}'] = y.id
        return s

    def _emp(self, school, label):
        e = Employee(school_id=school.id, employee_id=f'{label}-{self.sfx}',
                     full_name=f'Emp {label} {self.sfx}', job_title='معلم',
                     phone='07700000001', base_salary=100, status='active')
        db.session.add(e)
        db.session.flush()
        self.ids[label] = e.id

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            sids = [self.ids['school_a'], self.ids['school_b']]
            uids = [u.id for u in User.query.execution_options(**OPTS)
                    .filter(User.school_id.in_(sids)).all()]
            AuditLog.query.execution_options(**OPTS).filter(
                AuditLog.user_id.in_(uids)).delete(synchronize_session=False)
            for model in (AuditLog, Schedule, Subject, Section, Grade, Employee,
                          User, AcademicYear):
                model.query.execution_options(**OPTS).filter(
                    model.school_id.in_(sids)).delete(synchronize_session=False)
            School.query.filter(School.id.in_(sids)).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _web(self):
        client = self.app.test_client()
        resp = client.post('/auth/login', data={'username': f'stcaa_{self.sfx}',
                                                'password': PASSWORD})
        self.assertEqual(resp.status_code, 302)
        return client

    def _post(self, client, start, end, section='s1', day='1', teacher=None):
        data = {'section_id': str(self.ids[section]),
                'subject_id': str(self.ids['subject']),
                'day_of_week': day, 'start_time': start, 'end_time': end}
        if teacher:
            data['teacher_id'] = str(self.ids[teacher])
        resp = client.post('/schedules/create', data=data)
        self.assertEqual(resp.status_code, 302)
        with client.session_transaction() as sess:
            flashes = sess.pop('_flashes', [])
        return resp, [(cat, msg) for cat, msg in flashes]

    def _rows(self, section='s1'):
        with self.app.app_context():
            return sorted(
                (r.start_time.strftime('%H:%M'), r.end_time.strftime('%H:%M'))
                for r in Schedule.query.execution_options(**OPTS)
                .filter_by(section_id=self.ids[section]).all())

    def _insert_row(self, start, end, section='s1', day=1):
        """Direct insert, bypassing the route — simulates legacy data."""
        with self.app.app_context():
            db.session.add(Schedule(
                school_id=self.ids['school_a'], academic_year_id=self.ids['year_a'],
                section_id=self.ids[section], grade_id=None,
                subject_id=self.ids['subject'], day_of_week=day,
                start_time=start, end_time=end))
            db.session.commit()

    def assertFlash(self, flashes, text, category='danger'):
        self.assertTrue(any(c == category and text in m for c, m in flashes),
                        f'{text!r} not in {flashes!r}')

    # ── 1. inverted / zero-length ────────────────────────────────────────────

    def test_inverted_and_zero_length_ranges_rejected(self):
        client = self._web()
        for start, end in (('09:00', '08:00'), ('12:30', '09:00'), ('08:00', '08:00')):
            _, flashes = self._post(client, start, end)
            self.assertFlash(flashes, 'وقت نهاية الحصة يجب أن يكون بعد وقت البداية.')
        self.assertEqual(self._rows(), [])

    # ── 2. malformed ─────────────────────────────────────────────────────────

    def test_malformed_time_rejected_cleanly(self):
        client = self._web()
        for start, end in (('abc', '09:00'), ('25:00', '26:00'),
                           ('08:00', '08:60'), ('8', '9'), ('08:00:00:00', '09:00')):
            _, flashes = self._post(client, start, end)
            self.assertFlash(flashes, 'صيغة وقت الحصة غير صحيحة')
        self.assertEqual(self._rows(), [])

    # ── 3. back-to-back ──────────────────────────────────────────────────────

    def test_back_to_back_lessons_allowed(self):
        client = self._web()
        for start, end in (('08:00', '08:45'), ('08:45', '09:30'), ('07:15', '08:00')):
            _, flashes = self._post(client, start, end)
            self.assertFlash(flashes, 'تم إضافة الحصة.', 'success')
        self.assertEqual(self._rows(),
                         [('07:15', '08:00'), ('08:00', '08:45'), ('08:45', '09:30')])

    # ── 4. real overlap ──────────────────────────────────────────────────────

    def test_real_overlap_rejected_and_message_names_blocking_lesson(self):
        client = self._web()
        self._post(client, '08:00', '08:45')
        for start, end in (('08:30', '09:15'), ('07:30', '08:15'),
                           ('08:10', '08:20'), ('07:00', '10:00'), ('08:00', '08:45')):
            resp, flashes = self._post(client, start, end)
            self.assertFlash(flashes, f'هذا الوقت يتعارض مع حصة رياضيات {self.sfx} '
                                      'من 08:00 إلى 08:45')
            # Form keeps the submitted values after the conflict.
            qs = parse_qs(urlparse(resp.headers['Location']).query)
            self.assertEqual((qs['day_of_week'], qs['start_time'], qs['end_time']),
                             (['1'], [start], [end]))
        self.assertEqual(self._rows(), [('08:00', '08:45')])

        # Scope unchanged: another section and another day stay free.
        _, flashes = self._post(client, '08:00', '08:45', section='s2')
        self.assertFlash(flashes, 'تم إضافة الحصة.', 'success')
        _, flashes = self._post(client, '08:00', '08:45', day='2')
        self.assertFlash(flashes, 'تم إضافة الحصة.', 'success')

    # ── 5. legacy inverted row ───────────────────────────────────────────────

    def test_existing_inverted_row_does_not_block_the_morning(self):
        self._insert_row(time(12, 30), time(1, 0))          # stored 12:30 → 01:00
        client = self._web()
        for start, end in (('08:00', '08:45'), ('08:45', '09:30'),
                           ('09:30', '10:15'), ('11:00', '12:30')):
            _, flashes = self._post(client, start, end)
            self.assertFlash(flashes, 'تم إضافة الحصة.', 'success')
        self.assertEqual(len(self._rows()), 5)

    # ── 6. foreign-school teacher ────────────────────────────────────────────

    def test_teacher_from_another_school_rejected(self):
        client = self._web()
        _, flashes = self._post(client, '08:00', '08:45', teacher='tb')
        self.assertFlash(flashes, 'المعلم المختار غير صالح.')
        self.assertEqual(self._rows(), [])

        _, flashes = self._post(client, '08:00', '08:45', teacher='ta')
        self.assertFlash(flashes, 'تم إضافة الحصة.', 'success')
        with self.app.app_context():
            row = (Schedule.query.execution_options(**OPTS)
                   .filter_by(section_id=self.ids['s1']).one())
            self.assertEqual(row.teacher_id, self.ids['ta'])

    # ── 7. form values echoed back (validated only) ──────────────────────────

    def test_form_renders_preserved_values_and_ignores_bad_args(self):
        client = self._web()
        page = client.get(f'/schedules/?section_id={self.ids["s1"]}'
                          '&day_of_week=3&start_time=12:30&end_time=13:15')
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn('value="12:30"', html)
        self.assertIn('value="13:15"', html)
        self.assertRegex(html, r'<option value="3"\s+selected>')

        page = client.get(f'/schedules/?section_id={self.ids["s1"]}'
                          '&day_of_week=9&start_time="><script>&end_time=x')
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn('value="08:00"', html)
        self.assertIn('value="09:00"', html)
        self.assertNotIn('"><script>', html)
        self.assertRegex(html, r'<option value="0"\s+selected>')


if __name__ == '__main__':
    unittest.main()

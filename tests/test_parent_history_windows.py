"""
Parent mobile history windows — attendance and exams.

The mobile app shows up to one year of history and walks back one 30-day
window at a time:

    first:  start = today - 29, end = today
    next:   end = previous start - 1 day, start = max(end - 29, today - 364)

Attendance uses the existing ?start=&end= contract (backend unchanged).
Exams gain optional ?start=&end=: without them the endpoint behaves exactly as
before; with them an exam is returned only if it is in the student's CURRENT
section (active year) or the student has an ExamResult for it.
"""
import unittest
from datetime import date, timedelta
from uuid import uuid4

from app import create_app
from app.models import (
    db, AcademicYear, Exam, ExamResult, Grade, Role, School, Section, Student,
    StudentAttendance, Subject, User,
)

EXAM_ITEM_KEYS = {'id', 'name', 'subject', 'exam_date', 'max_marks',
                  'pass_marks', 'is_upcoming'}


def _history_windows(today, first_end):
    """Mirror of the client cursor: 30-day windows back to today - 364."""
    boundary = today - timedelta(days=364)
    end = first_end
    start = max(end - timedelta(days=29), boundary)
    windows = [(start, end)]
    while start > boundary:
        end = start - timedelta(days=1)
        start = max(end - timedelta(days=29), boundary)
        windows.append((start, end))
    return windows


class ParentHistoryWindowsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('development')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.today = T = date.today()
        self.created = {}
        sfx = self.suffix

        with self.app.app_context():
            parent_role = Role.query.filter_by(name='parent').first()
            manager_role = Role.query.filter_by(name='school_admin').first()
            self.assertIsNotNone(parent_role)
            self.assertIsNotNone(manager_role)

            school_a = School(school_name=f'History A {sfx}', code=f'HA{sfx[:8]}',
                              capacity=0, is_active=True)
            school_b = School(school_name=f'History B {sfx}', code=f'HB{sfx[:8]}',
                              capacity=0, is_active=True)
            db.session.add_all([school_a, school_b])
            db.session.flush()

            prev_year = AcademicYear(school_id=school_a.id, name=f'Prev {sfx}',
                                     start_date=T - timedelta(days=400),
                                     end_date=T - timedelta(days=120),
                                     is_current=False)
            cur_year = AcademicYear(school_id=school_a.id, name=f'Cur {sfx}',
                                    start_date=T - timedelta(days=119),
                                    end_date=T + timedelta(days=200),
                                    is_current=True)
            year_b = AcademicYear(school_id=school_b.id, name=f'B {sfx}',
                                  start_date=T - timedelta(days=119),
                                  end_date=T + timedelta(days=200),
                                  is_current=True)
            db.session.add_all([prev_year, cur_year, year_b])
            db.session.flush()

            grade_prev = Grade(school_id=school_a.id, academic_year_id=prev_year.id, name=f'GP {sfx}')
            grade_cur = Grade(school_id=school_a.id, academic_year_id=cur_year.id, name=f'GC {sfx}')
            grade_b = Grade(school_id=school_b.id, academic_year_id=year_b.id, name=f'GB {sfx}')
            db.session.add_all([grade_prev, grade_cur, grade_b])
            db.session.flush()

            sec_prev = Section(school_id=school_a.id, academic_year_id=prev_year.id,
                               grade_id=grade_prev.id, name=f'P{sfx[:4]}', capacity=30)
            sec_cur = Section(school_id=school_a.id, academic_year_id=cur_year.id,
                              grade_id=grade_cur.id, name=f'C{sfx[:4]}', capacity=30)
            sec_other = Section(school_id=school_a.id, academic_year_id=cur_year.id,
                                grade_id=grade_cur.id, name=f'O{sfx[:4]}', capacity=30)
            sec_b = Section(school_id=school_b.id, academic_year_id=year_b.id,
                            grade_id=grade_b.id, name=f'B{sfx[:4]}', capacity=30)
            db.session.add_all([sec_prev, sec_cur, sec_other, sec_b])
            db.session.flush()

            subj_prev = Subject(school_id=school_a.id, academic_year_id=prev_year.id,
                                name=f'Old Math {sfx}')
            subj_cur = Subject(school_id=school_a.id, academic_year_id=cur_year.id,
                               name=f'Math {sfx}')
            subj_b = Subject(school_id=school_b.id, academic_year_id=year_b.id,
                             name=f'B Math {sfx}')
            db.session.add_all([subj_prev, subj_cur, subj_b])
            db.session.flush()

            def student(code, school, year, section):
                return Student(student_id=f'{code}-{sfx}', full_name=f'{code} {sfx}',
                               date_of_birth=date(2015, 1, 1), gender='male',
                               school_id=school.id, academic_year_id=year.id,
                               section_id=section.id, status='active')

            # Own child (A1) and a classmate with another parent (A2), both in
            # the current section; B1 is in another school.
            stu_a1 = student('HA1', school_a, cur_year, sec_cur)
            stu_a2 = student('HA2', school_a, cur_year, sec_cur)
            stu_b1 = student('HB1', school_b, year_b, sec_b)
            db.session.add_all([stu_a1, stu_a2, stu_b1])
            db.session.flush()

            def user(name, role, school):
                u = User(username=f'{name}_{sfx}', email=f'{name}_{sfx}@example.test',
                         full_name=f'{name} {sfx}', role_id=role.id,
                         school_id=school.id, is_active=True)
                u.set_password('Password123')
                return u

            parent_a1 = user('hist_parent_a1', parent_role, school_a)
            parent_a2 = user('hist_parent_a2', parent_role, school_a)
            parent_b1 = user('hist_parent_b1', parent_role, school_b)
            manager_a = user('hist_manager_a', manager_role, school_a)
            db.session.add_all([parent_a1, parent_a2, parent_b1, manager_a])
            db.session.flush()
            parent_a1.children = [stu_a1]
            parent_a2.children = [stu_a2]
            parent_b1.children = [stu_b1]

            def exam(name, school, year, section, subject, days_ago):
                e = Exam(school_id=school.id, academic_year_id=year.id,
                         section_id=section.id, subject_id=subject.id,
                         exam_name=f'{name} {sfx}',
                         exam_date=T - timedelta(days=days_ago),
                         max_marks=100, pass_marks=50)
                db.session.add(e)
                return e

            ex = {
                # current section, active year
                'cur_future':   exam('cur_future', school_a, cur_year, sec_cur, subj_cur, -5),
                'cur_recent':   exam('cur_recent', school_a, cur_year, sec_cur, subj_cur, 10),
                'cur_old':      exam('cur_old', school_a, cur_year, sec_cur, subj_cur, 45),
                'cur_dup':      exam('cur_dup', school_a, cur_year, sec_cur, subj_cur, 50),
                # another current section — no association with A1
                'other_sec':    exam('other_sec', school_a, cur_year, sec_other, subj_cur, 45),
                'other_sec_a2': exam('other_sec_a2', school_a, cur_year, sec_other, subj_cur, 40),
                # previous year, previous section
                'prev_own':     exam('prev_own', school_a, prev_year, sec_prev, subj_prev, 200),
                'prev_none':    exam('prev_none', school_a, prev_year, sec_prev, subj_prev, 205),
                'prev_a2':      exam('prev_a2', school_a, prev_year, sec_prev, subj_prev, 210),
                # beyond the one-year horizon
                'prev_too_old': exam('prev_too_old', school_a, prev_year, sec_prev, subj_prev, 380),
                # other school
                'school_b':     exam('school_b', school_b, year_b, sec_b, subj_b, 45),
            }
            db.session.flush()

            def result(e, stu, year, school):
                db.session.add(ExamResult(exam_id=e.id, student_id=stu.id,
                                          school_id=school.id,
                                          academic_year_id=year.id, marks=70))

            result(ex['cur_dup'], stu_a1, cur_year, school_a)       # both paths match
            result(ex['other_sec_a2'], stu_a2, cur_year, school_a)  # classmate's result only
            result(ex['prev_own'], stu_a1, prev_year, school_a)     # proves A1's old exam
            result(ex['prev_a2'], stu_a2, prev_year, school_a)      # classmate's old result
            result(ex['prev_too_old'], stu_a1, prev_year, school_a)
            result(ex['school_b'], stu_b1, year_b, school_b)

            def att(stu, school, year, days_ago, status):
                db.session.add(StudentAttendance(
                    student_id=stu.id, school_id=school.id, academic_year_id=year.id,
                    date=T - timedelta(days=days_ago), status=status, source='manual'))

            # A1: 60..119 days ago is a deliberate gap (an empty window).
            self.a1_attendance = {
                5: 'present', 20: 'absent', 35: 'late', 40: 'on_leave',
                150: 'present', 300: 'excused',
            }
            for days_ago, status in self.a1_attendance.items():
                year = cur_year if days_ago < 120 else prev_year
                att(stu_a1, school_a, year, days_ago, status)
            att(stu_a1, school_a, prev_year, 380, 'present')   # beyond one year
            for days_ago in (5, 35, 150):
                att(stu_a2, school_a, cur_year if days_ago < 120 else prev_year,
                    days_ago, 'absent')
                att(stu_b1, school_b, year_b, days_ago, 'absent')

            db.session.commit()
            self.exam_ids = {k: e.id for k, e in ex.items()}
            self.subj_prev_name = subj_prev.name
            self.created = {
                'schools': [school_a.id, school_b.id],
                'years': [prev_year.id, cur_year.id, year_b.id],
                'grades': [grade_prev.id, grade_cur.id, grade_b.id],
                'sections': [sec_prev.id, sec_cur.id, sec_other.id, sec_b.id],
                'students': [stu_a1.id, stu_a2.id, stu_b1.id],
                'users': [parent_a1.id, parent_a2.id, parent_b1.id, manager_a.id],
                'parents': [parent_a1.id, parent_a2.id, parent_b1.id],
                'stu_a1': stu_a1.id,
                'stu_b1': stu_b1.id,
                'parent_a1': parent_a1.id,
                'parent_a2': parent_a2.id,
                'parent_b1': parent_b1.id,
                'manager_a': manager_a.id,
            }

    def tearDown(self):
        ids = self.created
        if not ids:
            return
        with self.app.app_context():
            db.session.rollback()
            opts = {'bypass_tenant_scope': True, 'include_all_years': True}
            for pid in ids['parents']:
                p = db.session.get(User, pid, execution_options={'bypass_tenant_scope': True})
                if p:
                    p.children = []
            db.session.flush()
            for model in (ExamResult, Exam, StudentAttendance, Subject):
                (model.query.execution_options(**opts)
                 .filter(model.school_id.in_(ids['schools']))
                 .delete(synchronize_session=False))
            db.session.flush()
            for model, key in ((User, 'users'), (Student, 'students'),
                               (Section, 'sections'), (Grade, 'grades'),
                               (AcademicYear, 'years'), (School, 'schools')):
                for pk in ids[key]:
                    obj = db.session.get(model, pk, execution_options={'bypass_tenant_scope': True})
                    if obj is not None:
                        db.session.delete(obj)
                db.session.flush()
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _token(self, user_id):
        with self.app.app_context():
            from app.blueprints.mobile_api.utils import encode_token
            u = db.session.get(User, user_id, execution_options={'bypass_tenant_scope': True})
            return encode_token(u)

    def _get(self, path, user_id=None, token=None, **params):
        headers = {}
        if user_id is not None:
            token = self._token(user_id)
        if token is not None:
            headers['Authorization'] = f'Bearer {token}'
        return self.app.test_client().get(f'/api/mobile/v1{path}',
                                          query_string=params, headers=headers)

    def _days(self, n):
        return (self.today - timedelta(days=n)).isoformat()

    def _exams(self, start=None, end=None, user_id=None, student=None):
        params = {}
        if start is not None:
            params['start'] = start
        if end is not None:
            params['end'] = end
        return self._get(f'/parent/children/{student or self.created["stu_a1"]}/exams',
                         user_id=user_id or self.created['parent_a1'], **params)

    def _exam_ids(self, resp):
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return [e['id'] for e in resp.get_json()['exams']]

    def _attendance(self, start, end, user_id=None, student=None):
        return self._get(f'/parent/children/{student or self.created["stu_a1"]}/attendance',
                         user_id=user_id or self.created['parent_a1'],
                         start=start.isoformat(), end=end.isoformat())

    # ── attendance (existing contract, backend unchanged) ─────────────────────

    def test_attendance_first_and_second_windows_are_adjacent(self):
        T = self.today
        first = self._attendance(T - timedelta(days=29), T)
        self.assertEqual(first.status_code, 200)
        body = first.get_json()
        self.assertEqual(body['range'], {'start': self._days(29), 'end': T.isoformat()})
        self.assertEqual([r['date'] for r in body['records']], [self._days(5), self._days(20)])

        second_end = T - timedelta(days=30)
        second = self._attendance(second_end - timedelta(days=29), second_end).get_json()
        self.assertEqual(second['range'], {'start': self._days(59), 'end': self._days(30)})
        self.assertEqual([r['date'] for r in second['records']], [self._days(35), self._days(40)])
        # no gap / no overlap: second window ends the day before the first starts
        self.assertEqual(date.fromisoformat(second['range']['end']) + timedelta(days=1),
                         date.fromisoformat(body['range']['start']))

    def test_attendance_missing_days_are_not_absences(self):
        T = self.today
        body = self._attendance(T - timedelta(days=29), T).get_json()
        s = body['summary']
        self.assertEqual(s['total'], 2)          # 30-day window, only 2 rows
        self.assertEqual(s['present'], 1)
        self.assertEqual(s['absent'], 1)
        self.assertEqual(s['late'], 0)
        empty = self._attendance(T - timedelta(days=89), T - timedelta(days=60)).get_json()
        self.assertEqual(empty['records'], [])
        self.assertEqual(empty['summary']['absent'], 0)
        self.assertEqual(empty['summary']['total'], 0)

    def test_attendance_one_year_walk_covers_history_and_stops(self):
        T = self.today
        windows = _history_windows(T, T)
        self.assertLessEqual(len(windows), 13)
        self.assertEqual(windows[-1][0], T - timedelta(days=364))
        seen, statuses = [], {}
        for i, (start, end) in enumerate(windows):
            if i:
                self.assertEqual(end + timedelta(days=1), windows[i - 1][0])
            self.assertLessEqual((end - start).days + 1, 30)
            body = self._attendance(start, end).get_json()
            self.assertEqual(body['range'], {'start': start.isoformat(), 'end': end.isoformat()})
            for r in body['records']:
                seen.append(r['date'])
                statuses[r['date']] = r['status']
        self.assertEqual(len(seen), len(set(seen)))             # no duplicates
        expected = {self._days(d): s for d, s in self.a1_attendance.items()}
        self.assertEqual(statuses, expected)                    # prev-year rows included,
        self.assertNotIn(self._days(380), statuses)             # nothing beyond one year

    def test_attendance_isolation(self):
        T, ids = self.today, self.created
        start, end = T - timedelta(days=29), T
        self.assertEqual(self._attendance(start, end, user_id=ids['parent_a2']).status_code, 404)
        self.assertEqual(self._attendance(start, end, user_id=ids['parent_b1']).status_code, 404)
        self.assertEqual(self._attendance(start, end, user_id=ids['parent_a1'],
                                          student=ids['stu_b1']).status_code, 404)
        self.assertEqual(self._attendance(start, end, user_id=ids['manager_a']).status_code, 403)
        unauth = self._get(f'/parent/children/{ids["stu_a1"]}/attendance',
                           start=start.isoformat(), end=end.isoformat())
        self.assertEqual(unauth.status_code, 401)

    # ── exams: default behaviour unchanged ────────────────────────────────────

    def test_exams_default_behaviour_unchanged(self):
        resp = self._exams()
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(set(body), {'ok', 'student_id', 'exams'})
        ids = self._exam_ids(resp)
        # -30/+60 window, current section only, ascending by date
        self.assertEqual(ids, [self.exam_ids['cur_recent'], self.exam_ids['cur_future']])
        for item in body['exams']:
            self.assertEqual(set(item), EXAM_ITEM_KEYS)
            self.assertIsInstance(item['id'], int)
            self.assertIsInstance(item['name'], str)
            self.assertIsInstance(item['subject'], str)
            self.assertIsInstance(item['exam_date'], str)
            self.assertIsInstance(item['max_marks'], float)
            self.assertIsInstance(item['pass_marks'], float)
            self.assertIsInstance(item['is_upcoming'], bool)
        upcoming = {e['id']: e['is_upcoming'] for e in body['exams']}
        self.assertTrue(upcoming[self.exam_ids['cur_future']])
        self.assertFalse(upcoming[self.exam_ids['cur_recent']])

    # ── exams: explicit history window ────────────────────────────────────────

    def test_exams_recent_window_uses_current_section(self):
        ids = self._exam_ids(self._exams(self._days(29), self._days(0)))
        self.assertEqual(ids, [self.exam_ids['cur_recent']])

    def test_exams_window_attribution_and_dedup(self):
        resp = self._exams(self._days(60), self._days(31))
        ids = self._exam_ids(resp)
        # current-section exams in range; cur_dup matches both paths → once
        self.assertEqual(ids, [self.exam_ids['cur_dup'], self.exam_ids['cur_old']])
        self.assertEqual(len(ids), len(set(ids)))
        for leaked in ('other_sec', 'other_sec_a2', 'school_b'):
            self.assertNotIn(self.exam_ids[leaked], ids)
        for item in resp.get_json()['exams']:
            self.assertEqual(set(item), EXAM_ITEM_KEYS)
            self.assertFalse(item['is_upcoming'])

    def test_exams_previous_year_result_proven_only(self):
        resp = self._exams(self._days(215), self._days(186))
        ids = self._exam_ids(resp)
        self.assertEqual(ids, [self.exam_ids['prev_own']])      # not prev_none / prev_a2
        item = resp.get_json()['exams'][0]
        self.assertEqual(item['subject'], self.subj_prev_name)  # previous-year subject kept
        self.assertFalse(item['is_upcoming'])
        self.assertEqual(set(item), EXAM_ITEM_KEYS)

    def test_exams_one_year_walk_no_duplicates(self):
        T = self.today
        windows = _history_windows(T, T - timedelta(days=31))   # after the default view
        self.assertLessEqual(len(windows), 12)
        self.assertEqual(windows[-1][0], T - timedelta(days=364))
        seen = self._exam_ids(self._exams())
        for start, end in windows:
            seen += self._exam_ids(self._exams(start.isoformat(), end.isoformat()))
        self.assertEqual(len(seen), len(set(seen)))
        expected = {self.exam_ids[k] for k in
                    ('cur_future', 'cur_recent', 'cur_old', 'cur_dup', 'prev_own')}
        self.assertEqual(set(seen), expected)

    def test_exams_invalid_ranges_return_400(self):
        T = self.today
        cases = [
            {'start': 'not-a-date', 'end': self._days(0)},
            {'start': self._days(10), 'end': '2026-13-40'},
            {'start': self._days(0), 'end': self._days(5)},       # start after end
            {'start': self._days(30), 'end': self._days(0)},      # 31 days
            {'start': self._days(366), 'end': self._days(340)},   # beyond one year
            {'start': self._days(0), 'end': (T + timedelta(days=10)).isoformat()},
            {'start': self._days(10)},                            # end missing
            {'end': self._days(10)},                              # start missing
        ]
        for params in cases:
            with self.subTest(params=params):
                resp = self._exams(**params)
                self.assertEqual(resp.status_code, 400)
                self.assertFalse(resp.get_json()['ok'])
        # boundaries accepted: exactly 30 days, and today - 365 (one day grace)
        self.assertEqual(self._exams(self._days(29), self._days(0)).status_code, 200)
        self.assertEqual(self._exams(self._days(365), self._days(336)).status_code, 200)

    def test_exams_isolation(self):
        ids = self.created
        start, end = self._days(60), self._days(31)
        self.assertEqual(self._exams(start, end, user_id=ids['parent_a2']).status_code, 404)
        self.assertEqual(self._exams(start, end, user_id=ids['parent_b1']).status_code, 404)
        self.assertEqual(self._exams(start, end, student=ids['stu_b1']).status_code, 404)
        self.assertEqual(self._exams(start, end, user_id=ids['manager_a']).status_code, 403)
        unauth = self._get(f'/parent/children/{ids["stu_a1"]}/exams', start=start, end=end)
        self.assertEqual(unauth.status_code, 401)
        # the other school's own parent still sees only their child's proven exam
        b_ids = self._exam_ids(self._exams(start, end, user_id=ids['parent_b1'],
                                           student=ids['stu_b1']))
        self.assertEqual(b_ids, [self.exam_ids['school_b']])


if __name__ == '__main__':
    unittest.main()

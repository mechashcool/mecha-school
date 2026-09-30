"""
Teacher Grades aggregate — HTTP-level tests.

  GET /api/mobile/v1/teacher/grades   (new: exam window + results, one request)

It must return exactly the /teacher/exams exam window, with each exam's results
matching /teacher/exams/<id>, under the same teacher / school / year scope — in
a bounded number of SQL statements.

Fixture (school A, current year YA; YA0 is an older, non-current year):
  T1 (Employee E1)
    S1   homeroom of E1      students s1a, s1b active; s1x INACTIVE
    S2   E1 assigned MATH    students s2a, s2b active
    S3   no access           student  s3a
    SO   YA0 section, homeroom of E1 AND assigned — must stay invisible
  Exams (YA unless noted)
    X1  S1 MATH  results s1a 90, s1b 90 (tie), s1x 40 (inactive → '?'),
                 s2a 70 (not in S1 → '?')
    X2  S1 SCI   homeroom sees every subject; exam_name NULL → default title
    X3  S2 MATH  assigned pair
    X4  S2 SCI   NOT visible (unassigned subject in an assigned section)
    X5  S3 MATH  NOT visible
    X6  S1 MATH  future date (upcoming)
    X7  S1 SCI   future date (upcoming), later than X6
    XO  SO (YA0) NOT visible
  T2   teacher in school A without any section
  TN   teacher-role user without an Employee row
  PA   parent in school A
  school B — teacher TB, homeroom SB, exam XB with student sb1

Requires an isolated, approved test database (tests/conftest.py guard).
"""
import time
import unittest
from datetime import date, timedelta
from uuid import uuid4

from sqlalchemy import event

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (db, AcademicYear, Employee, Exam, ExamResult, Grade, Role,
                        School, Section, Student, Subject, User, parent_students,
                        teacher_subjects)

OPTS = {'bypass_tenant_scope': True}
API = '/api/mobile/v1/teacher'

EXAM_KEYS = {'exam_title', 'subject_name', 'grade_name', 'section_name',
             'exam_date', 'max_marks'}
RESULT_KEYS = {'student_id', 'student_name', 'marks', 'note'}
# Shared mobile auth: users + roles (loaded twice) = 3; route: employees,
# sections, teacher_subjects, exams, exam_results, students = 6. So 9 on a
# normal request, 10 on an active-year cache miss — independent of exam count.
MAX_STATEMENTS = 10


class TeacherGradesTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['RATELIMIT_ENABLED'] = False
        with cls.app.app_context():
            cls.role_ids = {n: Role.query.filter_by(name=n).first().id
                            for n in ('parent', 'teacher')}

    # ── fixture ───────────────────────────────────────────────────────────────

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        self.school_ids = []
        self.today = date.today()
        with self.app.app_context():
            self._school_a()
            self._school_b()
            db.session.commit()

    def _add(self, obj):
        db.session.add(obj)
        db.session.flush()
        return obj

    def _school(self, key):
        school = self._add(School(school_name=f'TGR {key} {self.sfx}',
                                  code=f'TG{key}{self.sfx}'[:20], capacity=0,
                                  is_active=True))
        self.school_ids.append(school.id)
        return school

    def _year(self, school, key, current):
        start = date(2026, 8, 1) if current else date(2025, 8, 1)
        return self._add(AcademicYear(school_id=school.id, name=f'Y{key}{self.sfx}',
                                      is_current=current, start_date=start,
                                      end_date=start.replace(year=start.year + 1)
                                      - timedelta(days=1)))

    def _user(self, school, label, role):
        u = User(username=f'tgr{label}_{self.sfx}', email=f'tgr{label}_{self.sfx}@t.test',
                 full_name=label, role_id=self.role_ids[role], school_id=school.id,
                 is_active=True)
        u.set_password('Test1234!')
        self._add(u)
        self.ids[label] = u.id
        return u

    def _teacher(self, school, label):
        u = self._user(school, label, 'teacher')
        emp = self._add(Employee(school_id=school.id, employee_id=f'{label}{self.sfx}',
                                 full_name=f'Emp {label}', base_salary=0, status='active',
                                 user_id=u.id))
        self.ids[f'emp_{label}'] = emp.id
        return emp

    def _student(self, school, year, label, section, status='active'):
        st = self._add(Student(student_id=f'{label}-{self.sfx}', full_name=f'Stu {label}',
                               school_id=school.id, academic_year_id=year.id,
                               section_id=section.id, status=status))
        self.ids[label] = st.id
        return st

    def _exam(self, school, year, subj, sec, label, days, name=True):
        exam = self._add(Exam(school_id=school.id, academic_year_id=year.id,
                              subject_id=subj.id, section_id=sec.id,
                              exam_name=f'{label} {self.sfx}' if name else None,
                              exam_date=self.today + timedelta(days=days),
                              max_marks=100, pass_marks=50))
        self.ids[label] = exam.id
        return exam

    def _result(self, exam, stu, marks, notes=None):
        db.session.add(ExamResult(exam_id=exam.id, student_id=stu.id,
                                  school_id=exam.school_id,
                                  academic_year_id=exam.academic_year_id,
                                  marks=marks, notes=notes))

    def _school_a(self):
        school = self._school('a')
        ya0 = self._year(school, 'a0', current=False)
        ya = self._year(school, 'a', current=True)
        self.ids.update(school_a=school.id, year_a=ya.id)
        grade = self._add(Grade(name=f'G5 {self.sfx}', school_id=school.id,
                                academic_year_id=ya.id))
        math = self._add(Subject(name=f'Math {self.sfx}', school_id=school.id,
                                 academic_year_id=ya.id))
        sci = self._add(Subject(name=f'Sci {self.sfx}', school_id=school.id,
                                academic_year_id=ya.id))
        e1 = self._teacher(school, 't1')
        self._teacher(school, 't2')
        self._user(school, 'tn', 'teacher')           # no Employee row
        self._user(school, 'pa', 'parent')
        s1 = self._add(Section(name='A', grade_id=grade.id, school_id=school.id,
                               academic_year_id=ya.id, teacher_id=e1.id))
        s2 = self._add(Section(name='B', grade_id=grade.id, school_id=school.id,
                               academic_year_id=ya.id))
        s3 = self._add(Section(name='C', grade_id=grade.id, school_id=school.id,
                               academic_year_id=ya.id))
        # Older year: homeroom AND an explicit assignment — still invisible.
        grade0 = self._add(Grade(name=f'G5 {self.sfx}', school_id=school.id,
                                 academic_year_id=ya0.id))
        math0 = self._add(Subject(name=f'Math0 {self.sfx}', school_id=school.id,
                                  academic_year_id=ya0.id))
        so = self._add(Section(name='O', grade_id=grade0.id, school_id=school.id,
                               academic_year_id=ya0.id, teacher_id=e1.id))
        db.session.execute(teacher_subjects.insert(), [
            {'employee_id': e1.id, 'subject_id': math.id, 'section_id': s2.id},
            {'employee_id': e1.id, 'subject_id': math0.id, 'section_id': so.id},
        ])
        self.ids.update(sec_s1=s1.id, sec_s2=s2.id, sec_s3=s3.id, math=math.id,
                        grade_name=grade.name)

        s1a = self._student(school, ya, 's1a', s1)
        s1b = self._student(school, ya, 's1b', s1)
        s1x = self._student(school, ya, 's1x', s1, status='inactive')
        s2a = self._student(school, ya, 's2a', s2)
        s2b = self._student(school, ya, 's2b', s2)
        s3a = self._student(school, ya, 's3a', s3)

        x1 = self._exam(school, ya, math, s1, 'X1', -10)
        x2 = self._exam(school, ya, sci, s1, 'X2', -8, name=False)
        x3 = self._exam(school, ya, math, s2, 'X3', -6)
        x4 = self._exam(school, ya, sci, s2, 'X4', -5)
        x5 = self._exam(school, ya, math, s3, 'X5', -4)
        self._exam(school, ya, math, s1, 'X6', 5)
        self._exam(school, ya, sci, s1, 'X7', 6)
        xo = self._exam(school, ya0, math0, so, 'XO', -300)

        self._result(x1, s1a, 90, 'good')
        self._result(x1, s1b, 90)
        self._result(x1, s1x, 40)
        self._result(x1, s2a, 70)
        self._result(x2, s1a, 55.5, 'retake')
        self._result(x3, s2a, 81)
        self._result(x3, s2b, 64)
        self._result(x4, s2a, 99)
        self._result(x5, s3a, 98)
        self._result(xo, s1a, 97)

    def _school_b(self):
        school = self._school('b')
        yb = self._year(school, 'b', current=True)
        grade = self._add(Grade(name=f'GB {self.sfx}', school_id=school.id,
                                academic_year_id=yb.id))
        subj = self._add(Subject(name=f'SubjB {self.sfx}', school_id=school.id,
                                 academic_year_id=yb.id))
        eb = self._teacher(school, 'tb')
        sb = self._add(Section(name='A', grade_id=grade.id, school_id=school.id,
                               academic_year_id=yb.id, teacher_id=eb.id))
        sb1 = self._student(school, yb, 'sb1', sb)
        xb = self._exam(school, yb, subj, sb, 'XB', -3)
        self._result(xb, sb1, 77, 'b-note')

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for sid in self.school_ids:
                def q(model):
                    return model.query.execution_options(**OPTS).filter_by(school_id=sid)
                for model in (ExamResult, Exam):
                    q(model).delete(synchronize_session=False)
                emp_ids = [e.id for e in q(Employee).all()]
                if emp_ids:
                    db.session.execute(teacher_subjects.delete().where(
                        teacher_subjects.c.employee_id.in_(emp_ids)))
                uids = [u.id for u in q(User).all()]
                if uids:
                    db.session.execute(parent_students.delete().where(
                        parent_students.c.user_id.in_(uids)))
                db.session.execute(Section.__table__.update()
                                   .where(Section.school_id == sid).values(teacher_id=None))
                for model in (Student, Section, Grade, Subject, Employee, User, AcademicYear):
                    q(model).delete(synchronize_session=False)
                School.query.filter_by(id=sid).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _school_a_objs(self):
        return (db.session.get(School, self.ids['school_a']),
                db.session.get(AcademicYear, self.ids['year_a'], execution_options=OPTS))

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
        return resp.get_json()

    def _titles(self, body):
        return [i['exam']['exam_title'] for i in body['items']]

    def _t(self, label):
        return f'{label} {self.sfx}'

    def _window_keys_list(self, body):
        return [(e['title'], e['exam_date'], e['subject_name'], e['section_name'],
                 e['grade_name'], e['max_score']) for e in body['exams']]

    def _window_keys_grades(self, body):
        return [(i['exam']['exam_title'], i['exam']['exam_date'],
                 i['exam']['subject_name'], i['exam']['section_name'],
                 i['exam']['grade_name'], i['exam']['max_marks']) for i in body['items']]

    def _statements(self, user_key, path, **params):
        """Run one request; return (status, body, [statement texts], seconds).

        Only statement TEXT is captured — never bound parameters or rows.
        """
        token = self._token(user_key)
        with self.app.app_context():
            engine = db.engine
        seen = []

        def capture(conn, cursor, statement, *args):
            seen.append(statement)

        client = self.app.test_client()
        event.listen(engine, 'before_cursor_execute', capture)
        try:
            t0 = time.perf_counter()
            resp = client.get(path, query_string=params,
                              headers={'Authorization': f'Bearer {token}'})
            elapsed = time.perf_counter() - t0
        finally:
            event.remove(engine, 'before_cursor_execute', capture)
        return resp.status_code, resp.get_json(), seen, elapsed

    # ── 1. authentication / role ─────────────────────────────────────────────

    def test_01_auth_role_and_missing_employee(self):
        client = self.app.test_client()
        self.assertEqual(client.get(f'{API}/grades').status_code, 401)
        self.assertEqual(client.get(f'{API}/grades', headers={
            'Authorization': 'Bearer not-a-token'}).status_code, 401)
        self.assertEqual(self._get('pa', f'{API}/grades').status_code, 403)
        for path in (f'{API}/grades', f'{API}/exams'):
            with self.subTest(path=path):
                resp = self._get('tn', path)
                self.assertEqual(resp.status_code, 404)
                self.assertEqual(resp.get_json(),
                                 {'ok': False, 'error': 'employee_profile_not_found'})
        # A teacher with no section at all: empty, exactly like /teacher/exams.
        self.assertEqual(self._ok('t2', f'{API}/grades'), {'ok': True, 'items': []})
        self.assertEqual(self._ok('t2', f'{API}/exams'),
                         {'ok': True, 'count': 0, 'exams': []})

    # ── 2. teacher scope + academic year ─────────────────────────────────────

    def test_02_scope_homeroom_assignment_and_year(self):
        body = self._ok('t1', f'{API}/grades')
        self.assertEqual(set(body), {'ok', 'items'})
        # Same order as /teacher/exams: exam_date DESC.
        self.assertEqual(self._titles(body),
                         [self._t('X7'), self._t('X6'), self._t('X3'), 'اختبار',
                          self._t('X1')])
        titles = set(self._titles(body))
        for hidden in ('X4', 'X5', 'XO', 'XB'):      # unassigned subject/section,
            self.assertNotIn(self._t(hidden), titles)  # old year, other school

    # ── 3. tenant isolation ──────────────────────────────────────────────────

    def test_03_school_isolation_and_forged_params(self):
        body = self._ok('tb', f'{API}/grades')
        self.assertEqual(self._titles(body), [self._t('XB')])
        self.assertEqual([(r['student_id'], r['student_name']) for r in
                          body['items'][0]['results']],
                         [(self.ids['sb1'], 'Stu sb1')])
        # Client-supplied identity never widens the scope, for either teacher.
        forged = dict(school_id=self.school_ids[0], employee_id=self.ids['emp_t1'],
                      academic_year_id=self.ids['year_a'], section_id=self.ids['sec_s1'],
                      teacher_id=self.ids['emp_t1'], user_id=self.ids['t1'])
        self.assertEqual(self._ok('tb', f'{API}/grades', **forged), body)
        a_ids = {self.ids[k] for k in ('s1a', 's1b', 's1x', 's2a', 's2b', 's3a')}
        seen_b = {r['student_id'] for i in body['items'] for r in i['results']}
        self.assertFalse(seen_b & a_ids)

        t1 = self._ok('t1', f'{API}/grades')
        forged_b = dict(school_id=self.school_ids[1], employee_id=self.ids['emp_tb'])
        self.assertEqual(self._ok('t1', f'{API}/grades', **forged_b), t1)
        seen_a = {r['student_id'] for i in t1['items'] for r in i['results']}
        self.assertNotIn(self.ids['sb1'], seen_a)
        self.assertNotIn(self._t('XB'), self._titles(t1))

    def test_04_batched_queries_carry_tenant_criteria(self):
        status, _, stmts, _ = self._statements('t1', f'{API}/grades')
        self.assertEqual(status, 200)
        res = [s for s in stmts if 'FROM exam_results' in s]
        stu = [s for s in stmts if 'FROM students' in s]
        self.assertEqual((len(res), len(stu)), (1, 1))
        self.assertIn('exam_results.school_id', res[0])
        self.assertIn('students.school_id', stu[0])
        self.assertIn('students.section_id IN', stu[0])

    # ── 4. response correctness vs the existing detail route ─────────────────

    def test_05_results_match_detail_route(self):
        grades = self._ok('t1', f'{API}/grades')
        listing = self._ok('t1', f'{API}/exams')
        self.assertEqual(len(grades['items']), len(listing['exams']))
        for item, ex in zip(grades['items'], listing['exams']):
            with self.subTest(exam=ex['id']):
                detail = self._ok('t1', f"{API}/exams/{ex['id']}")
                self.assertEqual(set(item), {'exam', 'results'})
                self.assertEqual(set(item['exam']), EXAM_KEYS)
                d = detail['exam']
                self.assertEqual(item['exam'], {
                    'exam_title': d['title'], 'subject_name': d['subject_name'],
                    'grade_name': d['grade_name'], 'section_name': d['section_name'],
                    'exam_date': d['exam_date'], 'max_marks': d['max_marks']})
                date.fromisoformat(item['exam']['exam_date'])   # YYYY-MM-DD
                for r in item['results']:
                    self.assertEqual(set(r), RESULT_KEYS)
                marks = [r['marks'] for r in item['results']]
                self.assertEqual(marks, sorted(marks, reverse=True))   # marks DESC
                mine = sorted((r['student_id'], r['student_name'], r['marks'], r['note'])
                              for r in item['results'])
                theirs = sorted((r['student_id'], r['student_name'], r['marks'], r['notes'])
                                for r in detail['results'])
                self.assertEqual(mine, theirs)

    def test_06_student_name_resolution_is_section_bound(self):
        body = self._ok('t1', f'{API}/grades')
        x1 = next(i for i in body['items'] if i['exam']['exam_title'] == self._t('X1'))
        self.assertEqual(x1['exam'], {
            'exam_title': self._t('X1'), 'subject_name': f'Math {self.sfx}',
            'grade_name': self.ids['grade_name'], 'section_name': 'A',
            'exam_date': (self.today - timedelta(days=10)).isoformat(),
            'max_marks': 100.0})
        got = {r['student_id']: (r['student_name'], r['marks'], r['note'])
               for r in x1['results']}
        self.assertEqual(got, {
            self.ids['s1a']: ('Stu s1a', 90.0, 'good'),
            self.ids['s1b']: ('Stu s1b', 90.0, None),
            self.ids['s1x']: ('?', 40.0, None),       # inactive
            self.ids['s2a']: ('?', 70.0, None),       # active, but not in S1
        })
        self.assertEqual([r['student_id'] for r in x1['results']][-2:],
                         [self.ids['s2a'], self.ids['s1x']])
        # An exam without results is still listed, with an empty result list.
        x6 = next(i for i in body['items'] if i['exam']['exam_title'] == self._t('X6'))
        self.assertEqual(x6['results'], [])

    # ── 5. window parity with /teacher/exams ─────────────────────────────────

    def test_07_window_matches_teacher_exams(self):
        cases = ({}, {'limit': 2}, {'limit': 2, 'offset': 1}, {'offset': 4},
                 {'offset': 50}, {'limit': 0}, {'limit': -3}, {'limit': 'x'},
                 {'upcoming': 1}, {'past': 1}, {'past': 1, 'limit': 1, 'offset': 1})
        for params in cases:
            with self.subTest(params=params):
                listing = self._ok('t1', f'{API}/exams', **params)
                grades = self._ok('t1', f'{API}/grades', **params)
                self.assertEqual(self._window_keys_grades(grades),
                                 self._window_keys_list(listing))
        self.assertEqual(self._titles(self._ok('t1', f'{API}/grades', upcoming=1)),
                         [self._t('X7'), self._t('X6')])

    # ── 6. read-only ─────────────────────────────────────────────────────────

    def test_09_read_only(self):
        verbs = []
        for key in ('t1', 'tb', 't2', 'tn', 'pa'):
            _, _, stmts, _ = self._statements(key, f'{API}/grades')
            verbs += [s.lstrip().split(None, 1)[0].upper() for s in stmts]
        self.assertTrue(verbs)
        self.assertEqual([v for v in verbs if v in ('INSERT', 'UPDATE', 'DELETE')], [])

    # ── 7. performance: bounded statement count ──────────────────────────────

    def _perf_fixture(self, n_exams, n_students, label):
        with self.app.app_context():
            sch, yr = self._school_a_objs()
            grade = db.session.get(Grade, db.session.get(
                Section, self.ids['sec_s1'], execution_options=OPTS).grade_id,
                execution_options=OPTS)
            math = db.session.get(Subject, self.ids['math'], execution_options=OPTS)
            emp = self._teacher(sch, label)
            sec = self._add(Section(name=label.upper(), grade_id=grade.id, school_id=sch.id,
                                    academic_year_id=yr.id, teacher_id=emp.id))
            studs = [self._student(sch, yr, f'{label}{i:03d}', sec)
                     for i in range(n_students)]
            for e in range(n_exams):
                exam = self._exam(sch, yr, math, sec, f'{label}{e:03d}', -e)
                for i, st in enumerate(studs):
                    self._result(exam, st, (i * 7 + e) % 101, 'n' if i % 5 == 0 else None)
            db.session.commit()

    def _measure(self, key, n_exams, n_students):
        cold = self._statements(key, f'{API}/grades')     # active-year cache may miss
        warm = self._statements(key, f'{API}/grades')
        self.assertEqual((cold[0], warm[0]), (200, 200))
        self.assertEqual(len(warm[1]['items']), n_exams)
        self.assertEqual(sum(len(i['results']) for i in warm[1]['items']),
                         n_exams * n_students)
        # The same screen through the old path: list + one detail per exam.
        _, listing, stmts, secs = self._statements(key, f'{API}/exams')
        old_stmts, old_secs = len(stmts), secs
        for ex in listing['exams']:
            status, _, stmts, secs = self._statements(key, f"{API}/exams/{ex['id']}")
            self.assertEqual(status, 200)
            old_stmts += len(stmts)
            old_secs += secs
        print(f'\n[grades-perf] exams={n_exams} students={n_students} '
              f'new: statements cold={len(cold[2])} warm={len(warm[2])} '
              f'warm_ms={warm[3] * 1000:.1f} | old: requests={1 + len(listing["exams"])} '
              f'statements={old_stmts} total_ms={old_secs * 1000:.1f}')
        return len(cold[2]), len(warm[2])

    def test_10_statement_count_is_constant_small_sample(self):
        """1 exam vs 4 exams (3 students each): the count must not grow per exam."""
        self._perf_fixture(1, 3, 'tp')
        cold1, warm1 = self._measure('tp', 1, 3)
        self._perf_fixture(4, 3, 'tq')
        cold4, warm4 = self._measure('tq', 4, 3)
        self.assertLessEqual(max(cold1, cold4), MAX_STATEMENTS)
        self.assertEqual(warm1, warm4)
        self.assertLessEqual(warm4, MAX_STATEMENTS - 1)


if __name__ == '__main__':
    unittest.main()

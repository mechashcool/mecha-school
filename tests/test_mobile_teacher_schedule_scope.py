"""
Teacher mobile timetable scope — HTTP-level tests.

  GET /api/mobile/v1/teacher/schedule

An unassigned lesson (Schedule.teacher_id IS NULL) belongs to a teacher only
through an EXACT teacher_subjects pair: same subject + same section, or — for a
whole-grade lesson — same subject in a section of that grade. Homeroom alone,
or another subject in the same section/grade, must not expose it. Lessons with
teacher_id == the teacher are always included; another teacher's never are.

Fixture (school A, current year YA; YA0 is an older, non-current year):
  Grades G5, G6 (YA).  Subjects MATH, ENG, SCI (YA).
  Sections  A (G5)  homeroom of E1, E1 assigned ENG
            B (G5)  E1 assigned MATH
            C (G5)  no E1 pair
            H (G5)  homeroom of E1 only, no pair
            D (G6)  no E1 pair
  Teachers  T1/E1 (above), T2/E2 (no homeroom, no pair)
  Lessons (YA)
    L1  A   MATH  teacher E1    included   (explicit, no pair needed)
    L2  A   SCI   teacher E2    excluded for E1 (another teacher, even in homeroom)
    L3  B   MATH  NULL          included   (exact pair)
    L4  B   SCI   NULL          excluded   (other subject, same section)
    L5  C   MATH  NULL          excluded   (same subject, other section)
    L6  H   MATH  NULL          excluded   (homeroom only)
    L7  A   SCI   NULL          excluded   (homeroom + other-subject pair)
    W1  G5  MATH  NULL          included   (MATH pair in B ∈ G5)
    W2  G5  SCI   NULL          excluded   (only ENG/MATH in G5)
    W3  G6  MATH  NULL          excluded   (pair is in G5, not G6)
    W4  G5  MATH  teacher E2    excluded for E1
    L8  D   ENG   NULL          excluded   (ENG pair is in G5, not D/G6)
    LX  B   ENG   teacher EB    forged cross-school row — excluded for everyone
  Old year YA0: section SO (homeroom E1) + pair (MATH0, SO) with an explicit
    E1 lesson and an exact-pair NULL lesson — both excluded.
  School B: TB/EB, homeroom SB + pair (SUBJB, SB); LB1 explicit EB,
    LB2 NULL exact pair — visible only to TB.

Requires an isolated, approved test database (tests/conftest.py guard).
"""
import unittest
from datetime import date, time, timedelta
from uuid import uuid4

from sqlalchemy import event

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (db, AcademicYear, Employee, Grade, Role, Schedule, School,
                        Section, Subject, User, teacher_subjects)

OPTS = {'bypass_tenant_scope': True}
URL = '/api/mobile/v1/teacher/schedule'

ITEM_KEYS = {'id', 'day', 'day_int', 'day_en', 'day_label', 'day_name',
             'start_time', 'end_time', 'grade_id', 'grade_name', 'grade',
             'section_id', 'section_name', 'section', 'subject_id',
             'subject_name', 'subject', 'subject_code', 'room'}


class TeacherScheduleScopeTest(unittest.TestCase):

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
        self.lessons = {}           # schedule id → fixture label
        self.school_ids = []
        self._slot = 0
        with self.app.app_context():
            self._school_a()
            self._school_b()
            db.session.commit()

    def _add(self, obj):
        db.session.add(obj)
        db.session.flush()
        return obj

    def _school(self, key):
        school = self._add(School(school_name=f'TSC {key} {self.sfx}',
                                  code=f'TS{key}{self.sfx}'[:20], capacity=0,
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
        u = User(username=f'tsc{label}_{self.sfx}', email=f'tsc{label}_{self.sfx}@t.test',
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

    def _subject(self, school, year, label):
        return self._add(Subject(name=f'{label} {self.sfx}', school_id=school.id,
                                 academic_year_id=year.id))

    def _lesson(self, label, school, year, subject, section=None, grade=None,
                teacher=None):
        # Every lesson gets its own slot so no uniqueness/overlap can interfere.
        self._slot += 1
        lesson = self._add(Schedule(
            school_id=school.id, academic_year_id=year.id,
            section_id=section.id if section else None,
            grade_id=grade.id if grade else None,
            subject_id=subject.id, teacher_id=teacher.id if teacher else None,
            day_of_week=self._slot % 5, start_time=time(7 + self._slot // 5, 0),
            end_time=time(7 + self._slot // 5, 45), room=f'R{self._slot}'))
        self.ids[label] = lesson.id
        self.lessons[lesson.id] = label
        return lesson

    def _pairs(self, emp, *pairs):
        db.session.execute(teacher_subjects.insert(), [
            {'employee_id': emp.id, 'subject_id': subj.id, 'section_id': sec.id}
            for subj, sec in pairs])

    def _school_a(self):
        school = self._school('a')
        ya0 = self._year(school, 'a0', current=False)
        ya = self._year(school, 'a', current=True)
        g5 = self._add(Grade(name=f'G5 {self.sfx}', school_id=school.id,
                             academic_year_id=ya.id))
        g6 = self._add(Grade(name=f'G6 {self.sfx}', school_id=school.id,
                             academic_year_id=ya.id))
        math = self._subject(school, ya, 'Math')
        eng = self._subject(school, ya, 'Eng')
        sci = self._subject(school, ya, 'Sci')
        e1 = self._teacher(school, 't1')
        e2 = self._teacher(school, 't2')
        self._user(school, 'tn', 'teacher')           # no Employee row
        self._user(school, 'pa', 'parent')

        def section(name, grade, homeroom=None):
            return self._add(Section(name=name, grade_id=grade.id, school_id=school.id,
                                     academic_year_id=ya.id,
                                     teacher_id=homeroom.id if homeroom else None))
        sa = section('A', g5, e1)
        sb = section('B', g5)
        sc = section('C', g5)
        sh = section('H', g5, e1)
        sd = section('D', g6)
        self._pairs(e1, (eng, sa), (math, sb))
        self.ids.update(math=math.id, sec_b=sb.id, grade_5=g5.id)

        self._lesson('L1', school, ya, math, section=sa, teacher=e1)
        self._lesson('L2', school, ya, sci, section=sa, teacher=e2)
        self._lesson('L3', school, ya, math, section=sb)
        self._lesson('L4', school, ya, sci, section=sb)
        self._lesson('L5', school, ya, math, section=sc)
        self._lesson('L6', school, ya, math, section=sh)
        self._lesson('L7', school, ya, sci, section=sa)
        self._lesson('W1', school, ya, math, grade=g5)
        self._lesson('W2', school, ya, sci, grade=g5)
        self._lesson('W3', school, ya, math, grade=g6)
        self._lesson('W4', school, ya, math, grade=g5, teacher=e2)
        self._lesson('L8', school, ya, eng, section=sd)   # unrelated, NULL
        self.ids['sec_d'] = sd.id

        # Older year: homeroom, an exact pair, an explicit lesson — all invisible.
        g0 = self._add(Grade(name=f'G5 {self.sfx}', school_id=school.id,
                             academic_year_id=ya0.id))
        math0 = self._subject(school, ya0, 'Math0')
        so = self._add(Section(name='O', grade_id=g0.id, school_id=school.id,
                               academic_year_id=ya0.id, teacher_id=e1.id))
        self._pairs(e1, (math0, so))
        self._lesson('O1', school, ya0, math0, section=so, teacher=e1)
        self._lesson('O2', school, ya0, math0, section=so)
        self._lesson('O3', school, ya0, math0, grade=g0)
        self._school_a_refs = (school, ya, eng, sb)

    def _school_b(self):
        school = self._school('b')
        yb = self._year(school, 'b', current=True)
        grade = self._add(Grade(name=f'GB {self.sfx}', school_id=school.id,
                                academic_year_id=yb.id))
        subj = self._subject(school, yb, 'SubjB')
        eb = self._teacher(school, 'tb')
        sb = self._add(Section(name='A', grade_id=grade.id, school_id=school.id,
                               academic_year_id=yb.id, teacher_id=eb.id))
        self._pairs(eb, (subj, sb))
        self._lesson('LB1', school, yb, subj, section=sb, teacher=eb)
        self._lesson('LB2', school, yb, subj, section=sb)
        # A forged school-A row pointing at school B's employee (the web create
        # route refuses this; the read path must not trust it either).
        school_a, ya, eng, sec_b = self._school_a_refs
        self._lesson('LX', school_a, ya, eng, section=sec_b, teacher=eb)

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for sid in self.school_ids:
                def q(model):
                    return model.query.execution_options(**OPTS).filter_by(school_id=sid)
                q(Schedule).delete(synchronize_session=False)
                emp_ids = [e.id for e in q(Employee).all()]
                if emp_ids:
                    db.session.execute(teacher_subjects.delete().where(
                        teacher_subjects.c.employee_id.in_(emp_ids)))
                db.session.execute(Section.__table__.update()
                                   .where(Section.school_id == sid).values(teacher_id=None))
                for model in (Section, Grade, Subject, Employee, User, AcademicYear):
                    q(model).delete(synchronize_session=False)
                School.query.filter_by(id=sid).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _token(self, user_key):
        with self.app.app_context():
            return encode_token(db.session.get(User, self.ids[user_key],
                                               execution_options=OPTS))

    def _get(self, user_key):
        return self.app.test_client().get(
            URL, headers={'Authorization': f'Bearer {self._token(user_key)}'})

    def _schedule(self, user_key):
        resp = self._get(user_key)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:300])
        body = resp.get_json()
        self.assertTrue(body['ok'])
        return body['schedule']

    def _labels(self, user_key):
        return sorted(self.lessons.get(i['id'], f"?{i['id']}")
                      for i in self._schedule(user_key))

    # ── tests ─────────────────────────────────────────────────────────────────

    def test_01_auth_role_and_missing_employee(self):
        self.assertEqual(self.app.test_client().get(URL).status_code, 401)
        self.assertEqual(self._get('pa').status_code, 403)
        self.assertEqual(self._get('tn').status_code, 404)

    def test_02_exact_scope_for_teacher(self):
        # Covers required cases 1, 3, 4, 5, 6, 7, 8 (+ homeroom with another
        # subject's pair, and same subject in another grade).
        self.assertEqual(self._labels('t1'), ['L1', 'L3', 'W1'])

    def test_03_other_teachers_explicit_lessons_excluded(self):
        # Case 2: L2 (in E1's homeroom) and W4 belong to E2.
        labels = self._labels('t1')
        self.assertNotIn('L2', labels)
        self.assertNotIn('W4', labels)
        # E2 has no homeroom and no pair: explicit lessons only (case 11).
        self.assertEqual(self._labels('t2'), ['L2', 'W4'])

    def test_04_explicit_lesson_needs_no_pair(self):
        # Case 1/11: E1 has no (MATH, A) pair, yet the explicit lesson stays.
        with self.app.app_context():
            n = db.session.execute(teacher_subjects.select().where(
                teacher_subjects.c.employee_id == self.ids['emp_t1'],
                teacher_subjects.c.subject_id == self.ids['math'],
                teacher_subjects.c.section_id != self.ids['sec_b'])).fetchall()
        self.assertEqual(n, [])
        self.assertIn('L1', self._labels('t1'))

    def test_05_cross_school_isolation(self):
        # Case 9: nothing of school B reaches school A's teachers and vice versa;
        # the forged school-A row naming EB reaches nobody.
        for key in ('t1', 't2'):
            labels = self._labels(key)
            for foreign in ('LB1', 'LB2', 'LX'):
                self.assertNotIn(foreign, labels)
        self.assertEqual(self._labels('tb'), ['LB1', 'LB2'])

    def test_06_academic_year_isolation(self):
        # Case 10: old-year explicit, exact-pair and whole-grade lessons hidden.
        labels = self._labels('t1')
        for old in ('O1', 'O2', 'O3'):
            self.assertNotIn(old, labels)

    def test_07_payload_shape_unchanged(self):
        items = {i['id']: i for i in self._schedule('t1')}
        for item in items.values():
            self.assertEqual(set(item), ITEM_KEYS)
        w1 = items[self.ids['W1']]
        self.assertIsNone(w1['section_id'])
        self.assertEqual(w1['grade_id'], self.ids['grade_5'])
        self.assertEqual(w1['subject_id'], self.ids['math'])
        l3 = items[self.ids['L3']]
        self.assertEqual(l3['section_id'], self.ids['sec_b'])
        self.assertEqual(l3['grade_id'], self.ids['grade_5'])

    def test_08_teacher_without_any_pair_or_explicit_lesson(self):
        # Removing E2's explicit lessons leaves an empty timetable, not a fallback.
        with self.app.app_context():
            (Schedule.query.execution_options(**OPTS)
             .filter_by(teacher_id=self.ids['emp_t2'])
             .update({'teacher_id': None}, synchronize_session=False))
            db.session.commit()
        self.assertEqual(self._schedule('t2'), [])

    def test_09_scope_is_one_pair_query(self):
        # The pair lookup is a single statement, and there is exactly one
        # schedules SELECT — no per-lesson or per-section scope queries.
        token = self._token('t1')
        with self.app.app_context():
            engine = db.engine
        seen = []

        def capture(conn, cursor, statement, *args):
            seen.append(statement)

        event.listen(engine, 'before_cursor_execute', capture)
        try:
            resp = self.app.test_client().get(
                URL, headers={'Authorization': f'Bearer {token}'})
        finally:
            event.remove(engine, 'before_cursor_execute', capture)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sum('teacher_subjects' in s for s in seen), 1)
        self.assertEqual(sum('FROM schedules' in s for s in seen), 1)


if __name__ == '__main__':
    unittest.main()

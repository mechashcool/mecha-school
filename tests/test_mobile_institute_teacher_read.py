"""
Institute teacher mobile READ APIs — HTTP-level tests.

  GET /api/mobile/v1/teacher/institute/groups                    (new)
  GET /api/mobile/v1/teacher/institute/groups/<id>/students      (new)
  GET /api/mobile/v1/teacher/institute/students/<id>             (new)
  GET /api/mobile/v1/teacher/homework   (additive group_id / group_name)

Fixture:
  institute A — teacher TA (Employee EA) and teacher TB (Employee EB)
      GA1  EA, active   s1 + s2 active, s3 ENDED       slots Sun, Tue
      GA2  EA, active   (no students)
      GAX  EA, INACTIVE s2 active
      GB1  EB, active   s1 + s4 active
  institute B — teacher TX with group GX and student sx
  school C    — normal school teacher TC with section homework

Requires an isolated, approved test database (tests/conftest.py guard).
"""
import unittest
from datetime import date, datetime, time, timedelta
from uuid import uuid4

from sqlalchemy import event

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (db, AcademicYear, Employee, Exam, ExamResult, Grade, Homework,
                        InstituteGroupEnrollment, InstituteGroupSchedule,
                        InstituteStudyGroup, Role, School, Section, Student, Subject,
                        User, parent_students)

OPTS = {'bypass_tenant_scope': True}
API = '/api/mobile/v1/teacher'

SCHOOL_HW_KEYS = {'id', 'title', 'subject_id', 'subject_name', 'subject', 'section_id',
                  'section_name', 'section', 'grade_name', 'grade', 'display_name',
                  'publish_date', 'due_date', 'description', 'attachment_url',
                  'attachment_type', 'created_at'}
STUDENT_KEYS = {'id', 'student_id', 'name', 'photo', 'status'}


class InstituteTeacherReadTest(unittest.TestCase):

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
        with self.app.app_context():
            self._institute_a()
            self._institute_b()
            self._school_c()
            db.session.commit()

    def _school(self, key, institution_type):
        school = School(school_name=f'ITR {key} {self.sfx}', code=f'IT{key}{self.sfx}'[:20],
                        capacity=0, is_active=True, institution_type=institution_type)
        db.session.add(school)
        db.session.flush()
        self.school_ids.append(school.id)
        year = AcademicYear(school_id=school.id, name=f'Y{key}{self.sfx}', is_current=True,
                            start_date=date(2026, 8, 1), end_date=date(2027, 7, 31))
        db.session.add(year)
        db.session.flush()
        subj = Subject(name=f'Subj {key} {self.sfx}', school_id=school.id,
                       academic_year_id=year.id)
        db.session.add(subj)
        db.session.flush()
        return school, year, subj

    def _user(self, school, label, role):
        u = User(username=f'itr{label}_{self.sfx}', email=f'itr{label}_{self.sfx}@t.test',
                 full_name=label, role_id=self.role_ids[role], school_id=school.id,
                 is_active=True)
        u.set_password('Test1234!')
        db.session.add(u)
        db.session.flush()
        self.ids[label] = u.id
        return u

    def _teacher(self, school, label):
        u = self._user(school, label, 'teacher')
        emp = Employee(school_id=school.id, employee_id=f'{label}{self.sfx}',
                       full_name=f'Emp {label}', base_salary=0, status='active',
                       user_id=u.id)
        db.session.add(emp)
        db.session.flush()
        return emp

    def _student(self, school, year, label, section_id=None):
        st = Student(student_id=f'{label}-{self.sfx}', full_name=f'Stu {label}',
                     school_id=school.id, academic_year_id=year.id,
                     section_id=section_id, status='active')
        db.session.add(st)
        db.session.flush()
        self.ids[label] = st.id
        return st

    def _group(self, school, year, subj, emp, name, days=(), active=True):
        grp = InstituteStudyGroup(school_id=school.id, academic_year_id=year.id,
                                  subject_id=subj.id, instructor_id=emp.id, name=name,
                                  is_active=active)
        db.session.add(grp)
        db.session.flush()
        for dow in days:
            db.session.add(InstituteGroupSchedule(
                school_id=school.id, academic_year_id=year.id, group_id=grp.id,
                day_of_week=dow, start_time=time(16, 0), end_time=time(18, 0)))
        self.ids[name] = grp.id
        return grp

    def _enroll(self, school, grp, stu, ended=False):
        db.session.add(InstituteGroupEnrollment(
            school_id=school.id, group_id=grp.id, student_id=stu.id,
            enrolled_at=datetime(2026, 8, 2),
            ended_at=datetime(2026, 9, 1) if ended else None,
            status='ended' if ended else 'active'))

    def _exam_result(self, school, year, subj, grp, stu, marks, name):
        exam = Exam(school_id=school.id, academic_year_id=year.id, subject_id=subj.id,
                    exam_name=name, exam_date=date.today() - timedelta(days=3),
                    max_marks=100, pass_marks=50, institute_group_id=grp.id)
        db.session.add(exam)
        db.session.flush()
        db.session.add(ExamResult(exam_id=exam.id, student_id=stu.id, school_id=school.id,
                                  academic_year_id=year.id, marks=marks))
        self.ids[name] = exam.id

    def _institute_a(self):
        school, year, subj = self._school('a', 'institute')
        ea, eb = self._teacher(school, 'ta'), self._teacher(school, 'tb')
        self._user(school, 'pa', 'parent')
        s1, s2, s3, s4 = (self._student(school, year, n) for n in ('s1', 's2', 's3', 's4'))
        ga1 = self._group(school, year, subj, ea, 'GA1', days=(0, 2))
        self._group(school, year, subj, ea, 'GA2')
        gax = self._group(school, year, subj, ea, 'GAX', days=(1,), active=False)
        gb1 = self._group(school, year, subj, eb, 'GB1', days=(3,))
        self._enroll(school, ga1, s1)
        self._enroll(school, ga1, s2)
        self._enroll(school, ga1, s3, ended=True)
        self._enroll(school, gax, s2)
        self._enroll(school, gb1, s1)
        self._enroll(school, gb1, s4)
        self._exam_result(school, year, subj, ga1, s1, 80, 'EXA')
        self._exam_result(school, year, subj, gb1, s1, 55, 'EXB')
        hw = Homework(school_id=school.id, academic_year_id=year.id, teacher_id=ea.id,
                      subject_id=subj.id, institute_group_id=ga1.id, title='HWA',
                      publish_date=date.today(), due_date=date.today() + timedelta(days=7),
                      is_active=True)
        db.session.add(hw)
        db.session.flush()
        self.ids.update(school_a=school.id, subj_a=subj.id, hwa=hw.id)

    def _institute_b(self):
        school, year, subj = self._school('b', 'institute')
        ex = self._teacher(school, 'tx')
        sx = self._student(school, year, 'sx')
        gx = self._group(school, year, subj, ex, 'GX', days=(0,))
        self._enroll(school, gx, sx)

    def _school_c(self):
        school, year, subj = self._school('c', None)
        grade = Grade(name=f'G {self.sfx}', school_id=school.id, academic_year_id=year.id)
        db.session.add(grade)
        db.session.flush()
        ec = self._teacher(school, 'tc')
        sec = Section(name='A', grade_id=grade.id, school_id=school.id,
                      academic_year_id=year.id, teacher_id=ec.id)
        db.session.add(sec)
        db.session.flush()
        hw = Homework(school_id=school.id, academic_year_id=year.id, teacher_id=ec.id,
                      subject_id=subj.id, section_id=sec.id, title='HWC',
                      publish_date=date.today(), due_date=date.today() + timedelta(days=7),
                      is_active=True)
        db.session.add(hw)
        db.session.flush()
        self.ids.update(hwc=hw.id, sec_c=sec.id, grade_c=f'G {self.sfx}')

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for sid in self.school_ids:
                def q(model):
                    return model.query.execution_options(**OPTS).filter_by(school_id=sid)
                for model in (ExamResult, Exam, Homework, InstituteGroupSchedule,
                              InstituteGroupEnrollment, InstituteStudyGroup):
                    q(model).delete(synchronize_session=False)
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

    def _get(self, user_key, path, **params):
        with self.app.app_context():
            token = encode_token(db.session.get(User, self.ids[user_key],
                                                execution_options=OPTS))
        return self.app.test_client().get(
            path, query_string=params, headers={'Authorization': f'Bearer {token}'})

    def _ok(self, user_key, path, **params):
        resp = self._get(user_key, path, **params)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:400])
        return resp.get_json()

    def _not_found(self, user_key, path):
        resp = self._get(user_key, path)
        self.assertEqual(resp.status_code, 404, path)
        return resp.get_json()

    # ── 1-2. my groups ───────────────────────────────────────────────────────

    def test_01_02_own_active_groups_with_subject_count_slots(self):
        body = self._ok('ta', f'{API}/institute/groups')
        # Not GAX (inactive), GB1 (another instructor), GX (another institute).
        self.assertEqual([g['group_id'] for g in body['groups']],
                         [self.ids['GA1'], self.ids['GA2']])
        self.assertEqual(body['count'], 2)
        ga1 = body['groups'][0]
        self.assertEqual(ga1['subject'], {'id': self.ids['subj_a'],
                                          'name': f'Subj a {self.sfx}'})
        self.assertEqual(ga1['student_count'], 2)            # s3's ended enrollment excluded
        self.assertEqual(ga1['slots'], [
            {'day_of_week': 0, 'day_label': 'الأحد', 'start_time': '16:00', 'end_time': '18:00'},
            {'day_of_week': 2, 'day_label': 'الثلاثاء', 'start_time': '16:00', 'end_time': '18:00'}])
        self.assertEqual((ga1['start_date'], ga1['end_date']), (None, None))
        self.assertEqual(body['groups'][1]['student_count'], 0)
        # Client-supplied identity never widens the scope.
        forged = self._ok('ta', f'{API}/institute/groups',
                          instructor_id=999999, employee_id=999999,
                          school_id=self.school_ids[1])
        self.assertEqual(forged, body)
        tb = self._ok('tb', f'{API}/institute/groups')
        self.assertEqual([g['group_id'] for g in tb['groups']], [self.ids['GB1']])

    # ── 3-5. roster ──────────────────────────────────────────────────────────

    def test_03_own_group_roster(self):
        body = self._ok('ta', f"{API}/institute/groups/{self.ids['GA1']}/students")
        self.assertEqual(body['group']['group_id'], self.ids['GA1'])
        self.assertEqual(body['group']['subject']['id'], self.ids['subj_a'])
        self.assertEqual({s['id'] for s in body['students']},
                         {self.ids['s1'], self.ids['s2']})      # not s3 (ended)
        self.assertEqual(body['count'], 2)
        for s in body['students']:
            self.assertEqual(set(s), STUDENT_KEYS)
            self.assertEqual(s['status'], 'active')

    def test_04_05_other_instructor_school_or_inactive_group_is_404(self):
        for gid in (self.ids['GB1'], self.ids['GX'], self.ids['GAX'], 999999999):
            with self.subTest(group=gid):
                body = self._not_found('ta', f'{API}/institute/groups/{gid}/students')
                self.assertEqual(body, {'ok': False, 'error': 'group_not_found'})

    # ── 6-7. student profile ─────────────────────────────────────────────────

    def test_06_07_profile_only_own_groups_and_results(self):
        body = self._ok('ta', f"{API}/institute/students/{self.ids['s1']}")
        self.assertEqual(set(body['student']), STUDENT_KEYS)
        self.assertEqual(body['student']['id'], self.ids['s1'])
        # s1 is also in TB's GB1 — never revealed to TA.
        self.assertEqual([g['group_id'] for g in body['groups']], [self.ids['GA1']])
        self.assertEqual([r['exam_id'] for r in body['recent_results']], [self.ids['EXA']])
        r = body['recent_results'][0]
        self.assertEqual((r['exam_name'], r['group_id'], r['group_name'], r['score'],
                          r['max_score']), ('EXA', self.ids['GA1'], 'GA1', 80.0, 100.0))
        # TB sees the mirror image.
        tb = self._ok('tb', f"{API}/institute/students/{self.ids['s1']}")
        self.assertEqual([g['group_id'] for g in tb['groups']], [self.ids['GB1']])
        self.assertEqual([r['exam_id'] for r in tb['recent_results']], [self.ids['EXB']])
        # s2 has no results.
        self.assertEqual(self._ok('ta', f"{API}/institute/students/{self.ids['s2']}")
                         ['recent_results'], [])

        # Not in any of TA's groups: TB-only student, ended member, other
        # institute's student, nonexistent id.
        for sid in (self.ids['s4'], self.ids['s3'], self.ids['sx'], 999999999):
            with self.subTest(student=sid):
                self.assertEqual(self._not_found('ta', f'{API}/institute/students/{sid}'),
                                 {'ok': False, 'error': 'student_not_found'})

    # ── 8-9. homework list ───────────────────────────────────────────────────

    def test_08_09_homework_list_group_fields_and_school_unchanged(self):
        body = self._ok('ta', f'{API}/homework')
        self.assertEqual([h['id'] for h in body['homework']], [self.ids['hwa']])
        item = body['homework'][0]
        self.assertEqual(set(item), SCHOOL_HW_KEYS | {'group_id', 'group_name'})
        self.assertEqual((item['group_id'], item['group_name']), (self.ids['GA1'], 'GA1'))
        self.assertIsNone(item['section_id'])
        self.assertEqual((body['total'], body['limit'], body['offset']), (1, 50, 0))

        school = self._ok('tc', f'{API}/homework')
        self.assertEqual(set(school), {'ok', 'total', 'limit', 'offset', 'homework'})
        self.assertEqual([h['id'] for h in school['homework']], [self.ids['hwc']])
        item = school['homework'][0]
        self.assertEqual(set(item), SCHOOL_HW_KEYS)            # no group keys at all
        self.assertEqual((item['section_id'], item['section'], item['grade'],
                          item['display_name']),
                         (self.ids['sec_c'], 'A', self.ids['grade_c'],
                          f"{self.ids['grade_c']} - شعبة A"))

    # ── 10-11. wrong institution / role / no token ───────────────────────────

    def test_10_11_school_teacher_parent_and_anonymous(self):
        paths = (f'{API}/institute/groups',
                 f"{API}/institute/groups/{self.ids['GA1']}/students",
                 f"{API}/institute/students/{self.ids['s1']}")
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self._not_found('tc', path),
                                 {'ok': False, 'error': 'institute_not_available'})
                self.assertEqual(self._get('pa', path).status_code, 403)
                self.assertEqual(self.app.test_client().get(path).status_code, 401)

    # ── 12. read-only ────────────────────────────────────────────────────────

    def test_12_new_routes_perform_no_writes(self):
        with self.app.app_context():
            engine = db.engine
        seen = []

        def capture(conn, cursor, statement, *args):
            seen.append(statement.lstrip().split(None, 1)[0].upper())

        event.listen(engine, 'before_cursor_execute', capture)
        try:
            self._ok('ta', f'{API}/institute/groups')
            self._ok('ta', f"{API}/institute/groups/{self.ids['GA1']}/students")
            self._ok('ta', f"{API}/institute/students/{self.ids['s1']}")
            self._ok('ta', f'{API}/homework')
            self._not_found('ta', f"{API}/institute/groups/{self.ids['GB1']}/students")
        finally:
            event.remove(engine, 'before_cursor_execute', capture)
        self.assertTrue(seen)
        self.assertEqual([s for s in seen if s in ('INSERT', 'UPDATE', 'DELETE')], [])


if __name__ == '__main__':
    unittest.main()

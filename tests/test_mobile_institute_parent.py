"""
Institute parent mobile APIs — HTTP-level tests.

  GET /api/mobile/v1/parent/children/<id>/institute/groups      (new)
  GET /api/mobile/v1/parent/children/<id>/institute/attendance  (new)
  GET /api/mobile/v1/parent/children/<id>/exams                 (institute branch)
  GET /api/mobile/v1/parent/children/<id>/homework              (institute branch)

Fixture: institute A (the child under test), institute B (cross-tenant) and a
normal school C whose parent contract must stay exactly as it was.

Institute A groups, relative to the child `stu`:
  G1  current year, active, ACTIVE enrollment (joined long ago), daily 08:00
  G5  current year, active, ACTIVE enrollment joined on d-6,     daily 10:00
  G3  current year, active, ENDED enrollment (left on d-8),      daily 12:00
  G2  current year, active, stu NOT enrolled (sibling student is), daily 14:00
  G4  current year, INACTIVE group, active enrollment,           daily 15:00
  G0  PREVIOUS year group, active enrollment,                    daily 16:00

Requires an isolated, approved test database (tests/conftest.py guard).
"""
import unittest
from datetime import date, datetime, time, timedelta
from uuid import uuid4

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (db, AcademicYear, Employee, Exam, ExamResult, Grade, Homework,
                        InstituteAttendanceRecord, InstituteAttendanceSession,
                        InstituteGroupEnrollment, InstituteGroupSchedule,
                        InstituteStudyGroup, Role, Schedule, School, Section,
                        Student, StudentAttendance, Subject, User, parent_students)
from app.services import institute_attendance as inst_att

OPTS = {'bypass_tenant_scope': True}
BASE = '/api/mobile/v1/parent/children'

SCHOOL_EXAM_KEYS = {'id', 'name', 'subject', 'exam_date', 'max_marks',
                    'pass_marks', 'is_upcoming'}
HW_KEYS = {'id', 'homework_id', 'title', 'subject', 'subject_name', 'teacher_name',
           'grade_name', 'section_name', 'assigned_at', 'publish_date', 'due_date',
           'description', 'status', 'attachment_url', 'attachment_type', 'file_name',
           'file_size', 'is_pdf', 'submitted_status'}
GROUP_KEYS = {'group_id', 'group_name'}


def _utc_midnight(d):
    return datetime.combine(d, time(0, 0))


class InstituteParentMobileTest(unittest.TestCase):

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
        s = self.sfx
        school = School(school_name=f'IPM {key} {s}', code=f'IP{key}{s}'[:20],
                        capacity=0, is_active=True, institution_type=institution_type)
        db.session.add(school)
        db.session.flush()
        self.school_ids.append(school.id)
        year = AcademicYear(school_id=school.id, name=f'Y1{key}{s}', is_current=True,
                            start_date=date(2026, 8, 1), end_date=date(2027, 7, 31))
        db.session.add(year)
        db.session.flush()
        return school, year

    def _user(self, school, label, role='parent'):
        u = User(username=f'ipm{label}_{self.sfx}', email=f'ipm{label}_{self.sfx}@t.test',
                 full_name=f'{label}', role_id=self.role_ids[role],
                 school_id=school.id, is_active=True)
        u.set_password('Test1234!')
        db.session.add(u)
        db.session.flush()
        return u

    def _student(self, school, year, label, parent, section_id=None):
        st = Student(student_id=f'{label}-{self.sfx}', full_name=f'Stu {label}',
                     school_id=school.id, academic_year_id=year.id,
                     section_id=section_id, status='active')
        db.session.add(st)
        db.session.flush()
        db.session.execute(parent_students.insert().values(
            user_id=parent.id, student_id=st.id, relation='guardian'))
        return st

    def _group(self, school, year, subject, emp, name, start, active=True):
        g = InstituteStudyGroup(school_id=school.id, academic_year_id=year.id,
                                subject_id=subject.id, instructor_id=emp.id,
                                name=name, is_active=active)
        db.session.add(g)
        db.session.flush()
        for dow in range(7):
            db.session.add(InstituteGroupSchedule(
                school_id=school.id, academic_year_id=year.id, group_id=g.id,
                day_of_week=dow, start_time=start,
                end_time=time(start.hour + 1, 0)))
        return g

    def _enroll(self, school, group, student, joined, ended=None):
        db.session.add(InstituteGroupEnrollment(
            school_id=school.id, group_id=group.id, student_id=student.id,
            enrolled_at=_utc_midnight(joined), ended_at=_utc_midnight(ended) if ended else None,
            status='ended' if ended else 'active'))

    def _session(self, school, group, on_date, start, records):
        sess = InstituteAttendanceSession(
            school_id=school.id, academic_year_id=group.academic_year_id,
            group_id=group.id, session_date=on_date, start_time=start,
            end_time=time(start.hour + 1, 0), status='recorded',
            source='manual_admin', recorded_at=datetime.utcnow())
        db.session.add(sess)
        db.session.flush()
        for student, status in records:
            db.session.add(InstituteAttendanceRecord(
                school_id=school.id, session_id=sess.id, student_id=student.id,
                status=status, source='manual_admin', recorded_at=datetime.utcnow()))
        return sess

    def _exam(self, school, year, subject, on_date, *, group=None, section=None, name=None):
        e = Exam(school_id=school.id, academic_year_id=year.id, subject_id=subject.id,
                 exam_name=name, exam_date=on_date, max_marks=100, pass_marks=50,
                 institute_group_id=group.id if group else None,
                 section_id=section.id if section else None)
        db.session.add(e)
        db.session.flush()
        return e

    def _hw(self, school, year, subject, emp, title, publish, *, group=None,
            section=None, active=True):
        hw = Homework(school_id=school.id, academic_year_id=year.id, teacher_id=emp.id,
                      subject_id=subject.id, title=title, publish_date=publish,
                      due_date=publish + timedelta(days=7), is_active=active,
                      institute_group_id=group.id if group else None,
                      section_id=section.id if section else None)
        db.session.add(hw)
        db.session.flush()
        return hw

    def _institute_a(self):
        school, year = self._school('a', 'institute')
        d = self.d = inst_att.local_today(school)
        self.today = date.today()
        prev = AcademicYear(school_id=school.id, name=f'Y0a{self.sfx}', is_current=False,
                            start_date=date(2025, 8, 1), end_date=date(2026, 7, 31))
        db.session.add(prev)
        db.session.flush()
        subj = Subject(name=f'Math {self.sfx}', school_id=school.id, academic_year_id=year.id)
        subj0 = Subject(name=f'Math0 {self.sfx}', school_id=school.id, academic_year_id=prev.id)
        db.session.add_all([subj, subj0])
        db.session.flush()
        emp = Employee(school_id=school.id, employee_id=f'IPA{self.sfx}',
                       full_name='Instructor A', base_salary=0, status='active')
        db.session.add(emp)
        db.session.flush()
        parent, parent2 = self._user(school, 'pa'), self._user(school, 'pa2')
        stu = self._student(school, year, 'a1', parent)
        other = self._student(school, year, 'a2', parent2)

        g1 = self._group(school, year, subj, emp, 'A G1', time(8, 0))
        g5 = self._group(school, year, subj, emp, 'A G5', time(10, 0))
        g3 = self._group(school, year, subj, emp, 'A G3', time(12, 0))
        g2 = self._group(school, year, subj, emp, 'A G2', time(14, 0))
        g4 = self._group(school, year, subj, emp, 'A G4', time(15, 0), active=False)
        g0 = self._group(school, prev, subj0, emp, 'A G0', time(16, 0))

        long_ago = d - timedelta(days=60)
        self._enroll(school, g1, stu, long_ago)
        self._enroll(school, g1, other, long_ago)
        self._enroll(school, g5, stu, d - timedelta(days=6))
        self._enroll(school, g3, stu, long_ago, ended=d - timedelta(days=8))
        self._enroll(school, g2, other, long_ago)
        self._enroll(school, g4, stu, long_ago)
        self._enroll(school, g0, stu, long_ago)

        # Attendance (window W = d-10 .. d-3)
        self._session(school, g1, d - timedelta(days=5), time(8, 0),
                      [(stu, 'absent'), (other, 'present')])
        self._session(school, g1, d - timedelta(days=4), time(8, 0), [(other, 'present')])
        self._session(school, g5, d - timedelta(days=5), time(10, 0), [(stu, 'present')])
        self._session(school, g3, d - timedelta(days=9), time(12, 0), [(stu, 'late')])
        self._session(school, g2, d - timedelta(days=5), time(14, 0), [(other, 'absent')])
        self._session(school, g4, d - timedelta(days=7), time(15, 0), [(stu, 'excused')])
        # An intentionally recorded FUTURE lesson.
        self._session(school, g1, d + timedelta(days=2), time(8, 0), [(stu, 'present')])

        # Exams
        t = self.today
        ex1 = self._exam(school, year, subj, t + timedelta(days=5), group=g1, name='EX1')
        ex2 = self._exam(school, year, subj, t + timedelta(days=5), group=g2, name='EX2')
        ex3 = self._exam(school, year, subj, t - timedelta(days=20), group=g3, name='EX3')
        ex4 = self._exam(school, year, subj, t - timedelta(days=15), group=g2, name='EX4')
        ex5 = self._exam(school, year, subj, t - timedelta(days=20), group=g5, name='EX5')
        ex0 = self._exam(school, prev, subj0, t + timedelta(days=3), group=g0, name='EX0')
        db.session.add(ExamResult(exam_id=ex4.id, student_id=stu.id, school_id=school.id,
                                  academic_year_id=year.id, marks=70))

        # Homework
        hw1 = self._hw(school, year, subj, emp, 'HW1', t - timedelta(days=1), group=g1)
        hw2 = self._hw(school, year, subj, emp, 'HW2', t - timedelta(days=1), group=g2)
        hw3 = self._hw(school, year, subj, emp, 'HW3', t - timedelta(days=1), group=g1,
                       active=False)
        hw4 = self._hw(school, year, subj, emp, 'HW4', t + timedelta(days=2), group=g1)
        hw5 = self._hw(school, year, subj, emp, 'HW5', t - timedelta(days=2), group=g3)
        hw6 = self._hw(school, prev, subj0, emp, 'HW6', t - timedelta(days=2), group=g0)
        hw7 = self._hw(school, year, subj, emp, 'HW7', t - timedelta(days=3), group=g5)

        self.ids.update(dict(
            school_a=school.id, year_a=year.id, subj_a=subj.id, stu=stu.id, other=other.id,
            parent_a=parent.id, parent_a2=parent2.id,
            g1=g1.id, g2=g2.id, g3=g3.id, g4=g4.id, g5=g5.id, g0=g0.id,
            ex1=ex1.id, ex2=ex2.id, ex3=ex3.id, ex4=ex4.id, ex5=ex5.id, ex0=ex0.id,
            hw1=hw1.id, hw2=hw2.id, hw3=hw3.id, hw4=hw4.id, hw5=hw5.id, hw6=hw6.id,
            hw7=hw7.id))

    def _institute_b(self):
        school, year = self._school('b', 'institute')
        subj = Subject(name=f'Phys {self.sfx}', school_id=school.id, academic_year_id=year.id)
        db.session.add(subj)
        db.session.flush()
        emp = Employee(school_id=school.id, employee_id=f'IPB{self.sfx}',
                       full_name='Instructor B', base_salary=0, status='active')
        db.session.add(emp)
        db.session.flush()
        parent = self._user(school, 'pb')
        stu_b = self._student(school, year, 'b1', parent)
        gb = self._group(school, year, subj, emp, 'B G1', time(8, 0))
        self._enroll(school, gb, stu_b, self.d - timedelta(days=60))
        self._session(school, gb, self.d - timedelta(days=5), time(8, 0), [(stu_b, 'absent')])
        exb = self._exam(school, year, subj, self.today + timedelta(days=5), group=gb, name='EXB')
        hwb = self._hw(school, year, subj, emp, 'HWB', self.today - timedelta(days=1), group=gb)
        self.ids.update(school_b=school.id, stu_b=stu_b.id, parent_b=parent.id,
                        gb=gb.id, exb=exb.id, hwb=hwb.id)

    def _school_c(self):
        school, year = self._school('c', None)
        grade = Grade(name=f'G1 {self.sfx}', school_id=school.id, academic_year_id=year.id)
        db.session.add(grade)
        db.session.flush()
        sec = Section(name='A', grade_id=grade.id, school_id=school.id,
                      academic_year_id=year.id)
        db.session.add(sec)
        db.session.flush()
        subj = Subject(name=f'Arabic {self.sfx}', school_id=school.id,
                       academic_year_id=year.id)
        db.session.add(subj)
        db.session.flush()
        emp = Employee(school_id=school.id, employee_id=f'IPC{self.sfx}',
                       full_name='Teacher C', base_salary=0, status='active')
        db.session.add(emp)
        db.session.flush()
        parent = self._user(school, 'pc')
        stu_c = self._student(school, year, 'c1', parent, section_id=sec.id)
        t = self.today
        exc = self._exam(school, year, subj, t + timedelta(days=4), section=sec, name='EXC')
        exc_old = self._exam(school, year, subj, t - timedelta(days=10), section=sec,
                             name='EXCOLD')
        db.session.add(ExamResult(exam_id=exc_old.id, student_id=stu_c.id,
                                  school_id=school.id, academic_year_id=year.id, marks=88))
        hwc = self._hw(school, year, subj, emp, 'HWC', t - timedelta(days=1), section=sec)
        db.session.add(Schedule(school_id=school.id, academic_year_id=year.id,
                                section_id=sec.id, subject_id=subj.id, teacher_id=emp.id,
                                day_of_week=0, start_time=time(8, 0), end_time=time(8, 45)))
        db.session.add(StudentAttendance(student_id=stu_c.id, school_id=school.id,
                                         academic_year_id=year.id,
                                         date=t - timedelta(days=1), status='present'))
        self.ids.update(school_c=school.id, stu_c=stu_c.id, parent_c=parent.id,
                        sec_c=sec.id, exc=exc.id, exc_old=exc_old.id, hwc=hwc.id)

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for sid in self.school_ids:
                def q(model):
                    return model.query.execution_options(**OPTS).filter_by(school_id=sid)
                q(InstituteAttendanceRecord).delete(synchronize_session=False)
                q(InstituteAttendanceSession).delete(synchronize_session=False)
                q(InstituteGroupSchedule).delete(synchronize_session=False)
                q(ExamResult).delete(synchronize_session=False)
                for model in (Exam, Homework, Schedule, StudentAttendance,
                              InstituteGroupEnrollment):
                    q(model).delete(synchronize_session=False)
                q(InstituteStudyGroup).delete(synchronize_session=False)
                uids = [u.id for u in q(User).all()]
                if uids:
                    db.session.execute(parent_students.delete().where(
                        parent_students.c.user_id.in_(uids)))
                for model in (Student, Subject, Employee, User, Section, Grade,
                              AcademicYear):
                    q(model).delete(synchronize_session=False)
                School.query.filter_by(id=sid).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get(self, parent_key, path, **params):
        with self.app.app_context():
            user = db.session.get(User, self.ids[parent_key], execution_options=OPTS)
            token = encode_token(user)
        return self.app.test_client().get(
            path, query_string=params, headers={'Authorization': f'Bearer {token}'})

    def _ok(self, parent_key, path, **params):
        resp = self._get(parent_key, path, **params)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:400])
        return resp.get_json()

    def _iso(self, days):
        return (self.d + timedelta(days=days)).isoformat()

    def _counts(self):
        with self.app.app_context():
            sid = self.ids['school_a']
            return (InstituteAttendanceSession.query.execution_options(**OPTS)
                    .filter_by(school_id=sid).count(),
                    InstituteAttendanceRecord.query.execution_options(**OPTS)
                    .filter_by(school_id=sid).count())

    # ── groups / timetable ────────────────────────────────────────────────────

    def test_groups_current_memberships_only_with_slots(self):
        body = self._ok('parent_a', f"{BASE}/{self.ids['stu']}/institute/groups")
        self.assertEqual(body['student_id'], self.ids['stu'])
        # Ended (G3), not-enrolled (G2), inactive (G4), previous-year (G0) and
        # the other institute's group never appear.
        self.assertEqual([g['group_id'] for g in body['groups']],
                         [self.ids['g1'], self.ids['g5']])
        g1 = body['groups'][0]
        self.assertEqual(g1['name'], 'A G1')
        self.assertEqual(g1['subject'], {'id': self.ids['subj_a'],
                                         'name': f'Math {self.sfx}'})
        self.assertEqual(g1['instructor_name'], 'Instructor A')
        self.assertIn('+03:00', g1['enrolled_at'])
        self.assertIsNone(g1['start_date'])
        self.assertEqual(len(g1['slots']), 7)
        self.assertEqual(g1['slots'][0], {'day_of_week': 0, 'day_label': 'الأحد',
                                          'start_time': '08:00', 'end_time': '09:00'})
        # A forged group id is ignored — the set is derived server-side.
        forged = self._ok('parent_a', f"{BASE}/{self.ids['stu']}/institute/groups",
                          group_id=self.ids['g2'])
        self.assertEqual(forged['groups'], body['groups'])

    # ── attendance ────────────────────────────────────────────────────────────

    def test_attendance_lessons_statuses_and_unrecorded(self):
        before = self._counts()
        body = self._ok('parent_a', f"{BASE}/{self.ids['stu']}/institute/attendance",
                        start=self._iso(-10), end=self._iso(-3))
        self.assertEqual(self._counts(), before)          # GET writes nothing

        self.assertEqual(body['range'], {'start': self._iso(-10), 'end': self._iso(-3)})
        by_group = {}
        for r in body['records']:
            by_group.setdefault(r['group_id'], []).append(r)
        # G1: 8 daily lessons; G5: joined d-6 -> d-6..d-3; G3: left d-8 -> d-10, d-9;
        # G4 (inactive): only its recorded lesson. G2 / G0 / institute B: nothing.
        self.assertEqual({k: len(v) for k, v in by_group.items()},
                         {self.ids['g1']: 8, self.ids['g5']: 4,
                          self.ids['g3']: 2, self.ids['g4']: 1})
        self.assertEqual(body['summary'], {'present': 1, 'absent': 1, 'late': 1,
                                           'excused': 1, 'unrecorded': 11})
        self.assertEqual(body['count'], 15)

        def row(gid, days):
            return next(r for r in by_group[gid] if r['date'] == self._iso(days))

        self.assertEqual(row(self.ids['g1'], -5)['status'], 'absent')
        # Recorded lesson where THIS student has no status: null, not absent;
        # the sibling's 'present' record never leaks in.
        r = row(self.ids['g1'], -4)
        self.assertIsNone(r['status'])
        self.assertTrue(r['lesson_recorded'])
        self.assertIsNotNone(r['session_id'])
        # Never-opened lesson: no session, still not absent.
        r = row(self.ids['g1'], -6)
        self.assertEqual((r['status'], r['session_id'], r['lesson_recorded'],
                          r['recorded_at']), (None, None, False, None))
        # Two groups on the same day stay two rows.
        same_day = [x for x in body['records'] if x['date'] == self._iso(-5)]
        self.assertEqual({(x['group_id'], x['status']) for x in same_day},
                         {(self.ids['g1'], 'absent'), (self.ids['g5'], 'present')})
        self.assertEqual(row(self.ids['g3'], -9)['status'], 'late')
        self.assertEqual(row(self.ids['g4'], -7)['status'], 'excused')
        g1_row = row(self.ids['g1'], -5)
        self.assertEqual((g1_row['group_name'], g1_row['subject_name'],
                          g1_row['start_time'], g1_row['end_time']),
                         ('A G1', f'Math {self.sfx}', '08:00', '09:00'))
        self.assertIn('+03:00', g1_row['recorded_at'])
        # Newest first.
        dates = [x['date'] for x in body['records']]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_attendance_future_only_recorded_lessons(self):
        body = self._ok('parent_a', f"{BASE}/{self.ids['stu']}/institute/attendance",
                        start=self._iso(1), end=self._iso(6))
        self.assertEqual([(r['group_id'], r['date'], r['status']) for r in body['records']],
                         [(self.ids['g1'], self._iso(2), 'present')])

    def test_attendance_window_validation_and_default(self):
        path = f"{BASE}/{self.ids['stu']}/institute/attendance"
        body = self._ok('parent_a', path)
        self.assertEqual(body['range'], {'start': self._iso(-29), 'end': self._iso(0)})
        for params in ({'start': self._iso(-3)}, {'end': self._iso(-3)},
                       {'start': 'x', 'end': self._iso(-3)},
                       {'start': self._iso(-3), 'end': self._iso(-4)},
                       {'start': self._iso(-31), 'end': self._iso(0)}):
            with self.subTest(params=params):
                self.assertEqual(self._get('parent_a', path, **params).status_code, 400)
        self._ok('parent_a', path, start=self._iso(-30), end=self._iso(0))   # 31 days

    # ── exams ─────────────────────────────────────────────────────────────────

    def test_exams_default_current_groups(self):
        body = self._ok('parent_a', f"{BASE}/{self.ids['stu']}/exams")
        # Not EX5: G5 is a current group, but its exam predates the student
        # joining (same rule as the history window). Not EX2 (unrelated
        # group), EX3 (ended membership), EX0 (previous year), EXB (other
        # institute).
        self.assertEqual([e['id'] for e in body['exams']], [self.ids['ex1']])
        item = body['exams'][0]
        self.assertEqual(set(item), SCHOOL_EXAM_KEYS | GROUP_KEYS)
        self.assertEqual((item['group_id'], item['group_name'], item['name'],
                          item['is_upcoming']), (self.ids['g1'], 'A G1', 'EX1', True))
        forged = self._ok('parent_a', f"{BASE}/{self.ids['stu']}/exams",
                          group_id=self.ids['g2'])
        self.assertEqual(forged, body)

    def test_exams_history_membership_and_result_proof(self):
        t = self.today
        body = self._ok('parent_a', f"{BASE}/{self.ids['stu']}/exams",
                        start=(t - timedelta(days=25)).isoformat(),
                        end=(t - timedelta(days=1)).isoformat())
        # EX3: ended membership covered the exam date. EX4: unrelated group but
        # proven by the student's own ExamResult. EX5: joined after the exam.
        self.assertEqual({e['id'] for e in body['exams']},
                         {self.ids['ex3'], self.ids['ex4']})
        names = {e['id']: e['group_name'] for e in body['exams']}
        self.assertEqual(names[self.ids['ex4']], 'A G2')
        # Same window validation as the school path.
        bad = self._get('parent_a', f"{BASE}/{self.ids['stu']}/exams",
                        start=(t - timedelta(days=40)).isoformat(), end=t.isoformat())
        self.assertEqual(bad.status_code, 400)

    # ── homework ──────────────────────────────────────────────────────────────

    def test_homework_current_groups_only(self):
        body = self._ok('parent_a', f"{BASE}/{self.ids['stu']}/homework")
        # HW1 (G1) and HW7 (G5), newest publish first. Not: HW2 unrelated group,
        # HW3 inactive, HW4 unpublished, HW5 ended membership, HW6 previous
        # year, HWB other institute.
        self.assertEqual([h['id'] for h in body['homework']],
                         [self.ids['hw1'], self.ids['hw7']])
        self.assertEqual(body['count'], 2)
        self.assertIsNone(body['section'])
        item = body['homework'][0]
        self.assertEqual(set(item), HW_KEYS | GROUP_KEYS)
        self.assertEqual((item['group_id'], item['group_name'], item['teacher_name']),
                         (self.ids['g1'], 'A G1', 'Instructor A'))

        page = self._ok('parent_a', f"{BASE}/{self.ids['stu']}/homework", limit=1, offset=1)
        self.assertEqual((page['total'], page['limit'], page['offset'], page['count']),
                         (2, 1, 1, 1))
        self.assertEqual(page['homework'][0]['id'], self.ids['hw7'])

    # ── ownership / tenant isolation ─────────────────────────────────────────

    def test_ownership_and_cross_school_all_endpoints(self):
        suffixes = ('/institute/groups', '/institute/attendance', '/exams', '/homework')
        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                # Another parent in the same institute.
                self.assertEqual(self._get('parent_a2', f"{BASE}/{self.ids['stu']}{suffix}")
                                 .status_code, 404)
                # A parent of another institute.
                self.assertEqual(self._get('parent_b', f"{BASE}/{self.ids['stu']}{suffix}")
                                 .status_code, 404)
                # Nonexistent id.
                self.assertEqual(self._get('parent_a', f"{BASE}/999999999{suffix}")
                                 .status_code, 404)

        # Institute B's parent sees only institute B data.
        b = self._ok('parent_b', f"{BASE}/{self.ids['stu_b']}/institute/attendance",
                     start=self._iso(-10), end=self._iso(-3))
        self.assertEqual({r['group_id'] for r in b['records']}, {self.ids['gb']})
        self.assertEqual(b['summary']['absent'], 1)
        self.assertEqual([e['id'] for e in
                          self._ok('parent_b', f"{BASE}/{self.ids['stu_b']}/exams")['exams']],
                         [self.ids['exb']])
        self.assertEqual([h['id'] for h in
                          self._ok('parent_b', f"{BASE}/{self.ids['stu_b']}/homework")
                          ['homework']], [self.ids['hwb']])

    def test_new_endpoints_refuse_school_and_non_parent(self):
        for suffix in ('/institute/groups', '/institute/attendance'):
            with self.subTest(suffix=suffix):
                resp = self._get('parent_c', f"{BASE}/{self.ids['stu_c']}{suffix}")
                self.assertEqual(resp.status_code, 404)
                self.assertEqual(resp.get_json()['error'], 'institute_not_available')
        with self.app.app_context():
            t = self._user(db.session.get(School, self.ids['school_a']), 'ta', 'teacher')
            db.session.commit()
            self.ids['teacher_a'] = t.id
        self.assertEqual(self._get('teacher_a',
                                   f"{BASE}/{self.ids['stu']}/institute/groups").status_code,
                         403)
        self.assertEqual(self.app.test_client()
                         .get(f"{BASE}/{self.ids['stu']}/institute/groups").status_code, 401)

    # ── normal school contract (must be identical before and after) ──────────

    def test_school_parent_contract_unchanged(self):
        sid = self.ids['stu_c']
        t = self.today

        exams = self._ok('parent_c', f'{BASE}/{sid}/exams')
        self.assertEqual(set(exams), {'ok', 'student_id', 'exams'})
        self.assertEqual([e['id'] for e in exams['exams']],
                         [self.ids['exc_old'], self.ids['exc']])
        for e in exams['exams']:
            self.assertEqual(set(e), SCHOOL_EXAM_KEYS)
        hist = self._ok('parent_c', f'{BASE}/{sid}/exams',
                        start=(t - timedelta(days=20)).isoformat(),
                        end=(t - timedelta(days=1)).isoformat())
        self.assertEqual([e['id'] for e in hist['exams']], [self.ids['exc_old']])
        self.assertEqual(set(hist['exams'][0]), SCHOOL_EXAM_KEYS)
        for params, msg in (({'start': t.isoformat()}, 'start and end must be supplied together'),
                            ({'start': 'x', 'end': 'y'}, 'invalid date format — use YYYY-MM-DD')):
            resp = self._get('parent_c', f'{BASE}/{sid}/exams', **params)
            self.assertEqual((resp.status_code, resp.get_json()),
                             (400, {'ok': False, 'error': msg}))

        hw = self._ok('parent_c', f'{BASE}/{sid}/homework')
        self.assertEqual(set(hw), {'ok', 'student_id', 'section', 'count', 'homework'})
        self.assertEqual(hw['section'], 'A')
        self.assertEqual([h['id'] for h in hw['homework']], [self.ids['hwc']])
        item = hw['homework'][0]
        self.assertEqual(set(item), HW_KEYS)
        self.assertEqual((item['section_name'], item['grade_name'], item['teacher_name'],
                          item['status'], item['submitted_status'], item['is_pdf']),
                         ('A', f'G1 {self.sfx}', 'Teacher C', 'active', 'not_submitted', False))
        page = self._ok('parent_c', f'{BASE}/{sid}/homework', limit=5)
        self.assertEqual(set(page), {'ok', 'student_id', 'section', 'count', 'homework',
                                     'total', 'limit', 'offset'})

        sched = self._ok('parent_c', f'{BASE}/{sid}/schedule')
        self.assertEqual(len(sched['schedule']), 1)
        self.assertEqual(sched['section'], 'A')
        att = self._ok('parent_c', f'{BASE}/{sid}/attendance')
        self.assertEqual(att['summary']['present'], 1)
        grades = self._ok('parent_c', f'{BASE}/{sid}/grades')
        self.assertEqual([r['exam'] for r in grades['results']], ['EXCOLD'])
        self.assertEqual(grades['results'][0]['section'], 'A')
        fees = self._ok('parent_c', f'{BASE}/{sid}/fees')
        self.assertEqual(set(fees), {'ok', 'student_id', 'summary', 'records'})


if __name__ == '__main__':
    unittest.main()

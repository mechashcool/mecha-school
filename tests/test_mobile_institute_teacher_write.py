"""
Institute teacher mobile WRITE APIs — HTTP-level tests.

  GET/POST  /api/mobile/v1/teacher/institute/exams
  GET       /api/mobile/v1/teacher/institute/exams/<id>
  POST      /api/mobile/v1/teacher/institute/exams/<id>/results
  POST      /api/mobile/v1/teacher/institute/homework
  PUT/PATCH /api/mobile/v1/teacher/institute/homework/<id>
  + single-target guard on the legacy PUT /api/mobile/v1/teacher/homework/<id>

Fixture:
  institute A — TA (Employee EA) instructs GA1 (s1, s2 active; s0 ENDED),
                GA2 (active, empty) and GAX (INACTIVE); TA is also homeroom
                of a legacy section SA. TB (EB) instructs GB1 (s3).
  institute B — TX instructs GX.
  school C    — normal school teacher TC, homeroom of section SC.

Requires an isolated, approved test database (tests/conftest.py guard).
"""
import unittest
from datetime import date, datetime, timedelta
from uuid import uuid4

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (db, AcademicYear, Employee, Exam, ExamResult, Grade, Homework,
                        InstituteGroupEnrollment, InstituteStudyGroup, Notification,
                        PushNotification, Role, School, Section, Student, Subject,
                        User, parent_students)

OPTS = {'bypass_tenant_scope': True}
API = '/api/mobile/v1/teacher'


class InstituteTeacherWriteTest(unittest.TestCase):

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
            self._institute_a()
            self._institute_b()
            self._school_c()
            db.session.commit()

    def _school(self, key, institution_type):
        school = School(school_name=f'ITW {key} {self.sfx}', code=f'IW{key}{self.sfx}'[:20],
                        capacity=0, is_active=True, institution_type=institution_type)
        db.session.add(school)
        db.session.flush()
        self.school_ids.append(school.id)
        year = AcademicYear(school_id=school.id, name=f'Y{key}{self.sfx}', is_current=True,
                            start_date=date(2026, 8, 1), end_date=date(2027, 7, 31))
        db.session.add(year)
        db.session.flush()
        return school, year

    def _subject(self, school, year, label):
        subj = Subject(name=f'{label} {self.sfx}', school_id=school.id,
                       academic_year_id=year.id)
        db.session.add(subj)
        db.session.flush()
        self.ids[label] = subj.id
        return subj

    def _user(self, school, label, role):
        u = User(username=f'itw{label}_{self.sfx}', email=f'itw{label}_{self.sfx}@t.test',
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
        self.ids[f'emp_{label}'] = emp.id
        return emp

    def _student(self, school, year, label):
        st = Student(student_id=f'{label}-{self.sfx}', full_name=f'Stu {label}',
                     school_id=school.id, academic_year_id=year.id, status='active')
        db.session.add(st)
        db.session.flush()
        self.ids[label] = st.id
        return st

    def _group(self, school, year, subj, emp, name, active=True):
        grp = InstituteStudyGroup(school_id=school.id, academic_year_id=year.id,
                                  subject_id=subj.id, instructor_id=emp.id, name=name,
                                  is_active=active)
        db.session.add(grp)
        db.session.flush()
        self.ids[name] = grp.id
        return grp

    def _enroll(self, school, grp, stu, ended=False):
        db.session.add(InstituteGroupEnrollment(
            school_id=school.id, group_id=grp.id, student_id=stu.id,
            enrolled_at=datetime(2026, 8, 2),
            ended_at=datetime(2026, 9, 1) if ended else None,
            status='ended' if ended else 'active'))

    def _exam(self, school, year, grp, name):
        exam = Exam(school_id=school.id, academic_year_id=year.id,
                    subject_id=grp.subject_id, exam_name=name,
                    exam_date=self.today - timedelta(days=2), max_marks=100,
                    pass_marks=50, institute_group_id=grp.id)
        db.session.add(exam)
        db.session.flush()
        self.ids[name] = exam.id
        return exam

    def _hw(self, school, year, emp, subj_id, title, *, group=None, section=None):
        hw = Homework(school_id=school.id, academic_year_id=year.id, teacher_id=emp.id,
                      subject_id=subj_id, title=title, publish_date=self.today,
                      due_date=self.today + timedelta(days=7), is_active=True,
                      institute_group_id=group.id if group else None,
                      section_id=section.id if section else None)
        db.session.add(hw)
        db.session.flush()
        self.ids[title] = hw.id
        return hw

    def _institute_a(self):
        school, year = self._school('a', 'institute')
        self.ids.update(school_a=school.id, year_a=year.id)
        s_math, s_phys = self._subject(school, year, 'MATH'), self._subject(school, year, 'PHYS')
        ea, eb = self._teacher(school, 'ta'), self._teacher(school, 'tb')
        parent = self._user(school, 'pa', 'parent')
        s0, s1, s2, s3 = (self._student(school, year, n) for n in ('s0', 's1', 's2', 's3'))
        db.session.execute(parent_students.insert().values(
            user_id=parent.id, student_id=s1.id, relation='guardian'))
        ga1 = self._group(school, year, s_math, ea, 'GA1')
        ga2 = self._group(school, year, s_phys, ea, 'GA2')
        gax = self._group(school, year, s_math, ea, 'GAX', active=False)
        gb1 = self._group(school, year, s_phys, eb, 'GB1')
        for stu in (s1, s2):
            self._enroll(school, ga1, stu)
        self._enroll(school, ga1, s0, ended=True)
        self._enroll(school, gax, s2)
        self._enroll(school, gb1, s3)
        exa0 = self._exam(school, year, ga1, 'EXA0')
        db.session.add(ExamResult(exam_id=exa0.id, student_id=s0.id, school_id=school.id,
                                  academic_year_id=year.id, marks=40))
        self._exam(school, year, gb1, 'EXB')
        self._exam(school, year, gax, 'EXI')
        self._hw(school, year, eb, gb1.subject_id, 'HWB', group=gb1)
        self._hw(school, year, ea, ga1.subject_id, 'HWA', group=ga1)
        # A legacy section TA is homeroom of — the dual-target escape route.
        grade = Grade(name=f'GA {self.sfx}', school_id=school.id, academic_year_id=year.id)
        db.session.add(grade)
        db.session.flush()
        sec = Section(name='SA', grade_id=grade.id, school_id=school.id,
                      academic_year_id=year.id, teacher_id=ea.id)
        db.session.add(sec)
        db.session.flush()
        self.ids['sec_a'] = sec.id

    def _institute_b(self):
        school, year = self._school('b', 'institute')
        subj = self._subject(school, year, 'BIO')
        ex = self._teacher(school, 'tx')
        gx = self._group(school, year, subj, ex, 'GX')
        self._exam(school, year, gx, 'EXX')
        self._hw(school, year, ex, subj.id, 'HWX', group=gx)

    def _school_c(self):
        school, year = self._school('c', None)
        subj = self._subject(school, year, 'ARAB')
        grade = Grade(name=f'GC {self.sfx}', school_id=school.id, academic_year_id=year.id)
        db.session.add(grade)
        db.session.flush()
        ec = self._teacher(school, 'tc')
        sec = Section(name='SC', grade_id=grade.id, school_id=school.id,
                      academic_year_id=year.id, teacher_id=ec.id)
        db.session.add(sec)
        db.session.flush()
        self.ids['sec_c'] = sec.id
        self._hw(school, year, ec, subj.id, 'HWC', section=sec)

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for sid in self.school_ids:
                def q(model):
                    return model.query.execution_options(**OPTS).filter_by(school_id=sid)
                for model in (Notification, PushNotification, ExamResult, Exam, Homework,
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

    def _call(self, user_key, method, path, **kw):
        with self.app.app_context():
            token = encode_token(db.session.get(User, self.ids[user_key],
                                                execution_options=OPTS))
        return self.app.test_client().open(
            path, method=method, headers={'Authorization': f'Bearer {token}'}, **kw)

    def _row(self, model, key):
        with self.app.app_context():
            return db.session.get(model, self.ids[key], execution_options=OPTS)

    def _count(self, model, **filters):
        with self.app.app_context():
            return model.query.execution_options(**OPTS).filter_by(**filters).count()

    def _exam_body(self, **over):
        body = {'group_id': self.ids['GA1'], 'name': 'Quiz 1',
                'exam_date': self.today.isoformat(), 'max_score': 20, 'pass_marks': 10}
        body.update(over)
        return body

    # ── exams ─────────────────────────────────────────────────────────────────

    def test_01_03_exam_create_own_group_group_target_and_subject(self):
        resp = self._call('ta', 'POST', f'{API}/institute/exams', json=self._exam_body())
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        item = resp.get_json()['exam']
        self.assertEqual((item['group_id'], item['group_name'], item['name'],
                          item['max_score'], item['pass_marks'], item['section_id']),
                         (self.ids['GA1'], 'GA1', 'Quiz 1', 20.0, 10.0, None))
        self.assertEqual(item['subject']['id'], self.ids['MATH'])
        with self.app.app_context():
            exam = db.session.get(Exam, item['id'], execution_options=OPTS)
            self.assertIsNone(exam.section_id)
            self.assertEqual(exam.institute_group_id, self.ids['GA1'])
            self.assertEqual(exam.subject_id, self.ids['MATH'])      # from the group
            self.assertEqual(exam.school_id, self.ids['school_a'])
        # Sending the group's own subject is fine; a different one is refused.
        self.assertEqual(self._call('ta', 'POST', f'{API}/institute/exams', json=self._exam_body(
            name='Quiz 2', subject_id=self.ids['MATH'])).status_code, 201)

    def test_04_exam_create_rejections_write_nothing(self):
        before = self._count(Exam, school_id=self.ids['school_a'])
        cases = [
            (self._exam_body(group_id=self.ids['GB1']), 404),     # another instructor
            (self._exam_body(group_id=self.ids['GX']), 404),      # another institute
            (self._exam_body(group_id=self.ids['GAX']), 404),     # inactive group
            (self._exam_body(group_id=999999999), 404),
            (self._exam_body(subject_id=self.ids['PHYS']), 400),  # subject mismatch
            (self._exam_body(section_id=self.ids['sec_a']), 400), # section escape
            (self._exam_body(name='  '), 400),
            (self._exam_body(exam_date='2026-13-01'), 400),
            (self._exam_body(max_score=0), 400),
            (self._exam_body(pass_marks=25), 400),                # > max
            (self._exam_body(max_score='abc'), 400),
            (self._exam_body(school_id=self.ids['GX'], instructor_id=1), 201),  # ignored
        ]
        for body, code in cases:
            with self.subTest(body=body):
                self.assertEqual(self._call('ta', 'POST', f'{API}/institute/exams',
                                            json=body).status_code, code)
        self.assertEqual(self._count(Exam, school_id=self.ids['school_a']), before + 1)

    def test_05_06_exam_list_and_detail_scope(self):
        listed = self._call('ta', 'GET', f'{API}/institute/exams').get_json()
        # EXB (TB), EXI (inactive group) and EXX (institute B) are not listed.
        self.assertEqual([e['id'] for e in listed['exams']], [self.ids['EXA0']])
        self.assertEqual((listed['total'], listed['limit'], listed['offset']), (1, 50, 0))
        self.assertEqual(listed['exams'][0]['result_count'], 1)
        narrowed = self._call('ta', 'GET', f'{API}/institute/exams',
                              query_string={'group_id': self.ids['GB1']}).get_json()
        self.assertEqual(narrowed['exams'], [])

        body = self._call('ta', 'GET', f"{API}/institute/exams/{self.ids['EXA0']}").get_json()
        self.assertTrue(body['editable'])
        self.assertEqual({s['id'] for s in body['students']}, {self.ids['s1'], self.ids['s2']})
        self.assertTrue(all(s['result'] is None and s['editable'] for s in body['students']))
        # s0 left the group but has a stored result: read-only history.
        self.assertEqual([(h['id'], h['editable'], h['result']['score'])
                          for h in body['historical']], [(self.ids['s0'], False, 40.0)])

        for key in ('EXB', 'EXX'):
            with self.subTest(exam=key):
                resp = self._call('ta', 'GET', f"{API}/institute/exams/{self.ids[key]}")
                self.assertEqual(resp.status_code, 404)
        # Inactive group: readable, not editable.
        inactive = self._call('ta', 'GET', f"{API}/institute/exams/{self.ids['EXI']}").get_json()
        self.assertEqual((inactive['editable'], inactive['students']), (False, []))

    # ── results ───────────────────────────────────────────────────────────────

    def test_07_valid_batch_saves_atomically_with_ranks(self):
        url = f"{API}/institute/exams/{self.ids['EXA0']}/results"
        resp = self._call('ta', 'POST', url, json={'results': [
            {'student_id': self.ids['s1'], 'score': 90, 'notes': 'good'},
            {'student_id': self.ids['s2'], 'marks': '55.5'}]})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertEqual((body['saved'], body['created'], body['updated']), (2, 2, 0))
        ranks = {r['student_id']: (r['score'], r['rank'], r['is_pass']) for r in body['results']}
        self.assertEqual(ranks, {self.ids['s1']: (90.0, 1, True),
                                 self.ids['s2']: (55.5, 2, True),
                                 self.ids['s0']: (40.0, 3, None)})   # fixture row, untouched
        # Re-submitting the same values changes nothing.
        again = self._call('ta', 'POST', url, json={'results': [
            {'student_id': self.ids['s1'], 'score': 90}]}).get_json()
        self.assertEqual((again['created'], again['updated'], again['unchanged']), (0, 0, 1))

    def test_08_10_invalid_batches_write_nothing(self):
        url = f"{API}/institute/exams/{self.ids['EXA0']}/results"
        good = {'student_id': self.ids['s1'], 'score': 80}
        cases = [
            ({'student_id': self.ids['s3'], 'score': 50}, 'student_not_in_group'),  # TB's
            ({'student_id': self.ids['s0'], 'score': 50}, 'student_not_in_group'),  # ended
            ({'student_id': 999999999, 'score': 50}, 'student_not_in_group'),
            ({'student_id': self.ids['s2'], 'score': 101}, 'score_out_of_range'),
            ({'student_id': self.ids['s2'], 'score': -1}, 'score_out_of_range'),
            ({'student_id': self.ids['s2'], 'score': 'x'}, 'invalid_score'),
            ({'student_id': self.ids['s1'], 'score': 70}, 'duplicate_student'),
        ]
        for bad, error in cases:
            with self.subTest(error=error, bad=bad):
                resp = self._call('ta', 'POST', url, json={'results': [good, bad]})
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.get_json()['error'], error)
                self.assertEqual(self._count(ExamResult, exam_id=self.ids['EXA0']), 1)
        # Another instructor's / institute's / inactive group's exam.
        for key, code in (('EXB', 404), ('EXX', 404), ('EXI', 409)):
            with self.subTest(exam=key):
                resp = self._call('ta', 'POST',
                                  f"{API}/institute/exams/{self.ids[key]}/results",
                                  json={'results': [good]})
                self.assertEqual(resp.status_code, code)
        self.assertEqual(self._count(ExamResult, school_id=self.ids['school_a']), 1)

    # ── homework ──────────────────────────────────────────────────────────────

    def test_11_13_homework_create(self):
        body = {'group_id': self.ids['GA1'], 'title': 'Read ch.1',
                'due_date': (self.today + timedelta(days=3)).isoformat()}
        resp = self._call('ta', 'POST', f'{API}/institute/homework', json=body)
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        hw_item = resp.get_json()['homework']
        self.assertEqual((hw_item['group_id'], hw_item['group_name'], hw_item['section_id'],
                          hw_item['subject_id'], hw_item['publish_date']),
                         (self.ids['GA1'], 'GA1', None, self.ids['MATH'],
                          self.today.isoformat()))
        with self.app.app_context():
            hw = db.session.get(Homework, hw_item['id'], execution_options=OPTS)
            self.assertEqual((hw.section_id, hw.institute_group_id, hw.subject_id,
                              hw.teacher_id),
                             (None, self.ids['GA1'], self.ids['MATH'], self.ids['emp_ta']))
        # The web institute notification ran: s1's parent got an in-app row.
        self.assertEqual(self._count(Notification, school_id=self.ids['school_a'],
                                     target_user_id=self.ids['pa'], ntype='homework'), 1)

        before = self._count(Homework, school_id=self.ids['school_a'])
        for over, code in (({'group_id': self.ids['GB1']}, 404),
                           ({'group_id': self.ids['GX']}, 404),
                           ({'group_id': self.ids['GAX']}, 404),
                           ({'section_id': self.ids['sec_a']}, 400),
                           ({'subject_id': self.ids['PHYS']}, 400),
                           ({'due_date': (self.today - timedelta(days=1)).isoformat()}, 400),
                           ({}, 409)):                                  # duplicate
            with self.subTest(over=over):
                resp = self._call('ta', 'POST', f'{API}/institute/homework',
                                  json={**body, **over})
                self.assertEqual(resp.status_code, code, resp.get_data(as_text=True))
        self.assertEqual(self._count(Homework, school_id=self.ids['school_a']), before)

    def test_14_16_homework_update(self):
        url = f"{API}/institute/homework/{self.ids['HWA']}"
        due = (self.today + timedelta(days=5)).isoformat()
        resp = self._call('ta', 'PUT', url, json={'title': 'HWA v2', 'due_date': due,
                                                  'group_id': self.ids['GA2']})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual((resp.get_json()['homework']['group_id'],
                          resp.get_json()['homework']['subject_id']),
                         (self.ids['GA2'], self.ids['PHYS']))  # subject follows the group
        hw = self._row(Homework, 'HWA')
        self.assertEqual((hw.title, hw.institute_group_id, hw.section_id, hw.subject_id),
                         ('HWA v2', self.ids['GA2'], None, self.ids['PHYS']))

        for body, code in (({'group_id': self.ids['GB1']}, 404),    # not my group
                           ({'section_id': self.ids['sec_a']}, 400),# section escape
                           ({'subject_id': self.ids['MATH']}, 400)):# mismatch with GA2
            with self.subTest(body=body):
                resp = self._call('ta', 'PATCH', url,
                                  json={'title': 'X', 'due_date': due, **body})
                self.assertEqual(resp.status_code, code)
        hw = self._row(Homework, 'HWA')
        self.assertEqual((hw.title, hw.institute_group_id, hw.section_id),
                         ('HWA v2', self.ids['GA2'], None))
        # Another instructor's / institute's homework, or a school row.
        for key in ('HWB', 'HWX'):
            with self.subTest(hw=key):
                self.assertEqual(self._call('ta', 'PUT', f"{API}/institute/homework/{self.ids[key]}",
                                            json={'title': 'X', 'due_date': due}).status_code, 404)
        self.assertEqual(self._call('tc', 'PUT', f"{API}/institute/homework/{self.ids['HWC']}",
                                    json={'title': 'X', 'due_date': due}).status_code, 404)

    # ── school regression + legacy single-target guard ───────────────────────

    def test_17_school_exam_create_contract(self):
        resp = self._call('tc', 'POST', f'{API}/exams', json={
            'title': 'School quiz', 'section_id': self.ids['sec_c'],
            'subject_id': self.ids['ARAB'], 'exam_date': self.today.isoformat(),
            'max_score': 30})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        item = resp.get_json()['exam']
        self.assertEqual((item['section_id'], item['subject_id'], item['max_score']),
                         (self.ids['sec_c'], self.ids['ARAB'], 30.0))
        with self.app.app_context():
            exam = db.session.get(Exam, item['id'], execution_options=OPTS)
            self.assertEqual((exam.section_id, exam.institute_group_id),
                             (self.ids['sec_c'], None))

    def test_18_legacy_put_cannot_create_dual_target(self):
        due = (self.today + timedelta(days=4)).isoformat()
        # TA is homeroom of section SA, so the section checks would have passed.
        resp = self._call('ta', 'PUT', f"{API}/homework/{self.ids['HWA']}", json={
            'title': 'hijack', 'section_id': self.ids['sec_a'],
            'subject_id': self.ids['MATH'], 'due_date': due})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()['error'], 'institute_homework_use_institute_endpoint')
        hw = self._row(Homework, 'HWA')
        self.assertEqual((hw.title, hw.section_id, hw.institute_group_id),
                         ('HWA', None, self.ids['GA1']))
        # A school homework update still works exactly as before.
        resp = self._call('tc', 'PUT', f"{API}/homework/{self.ids['HWC']}", json={
            'title': 'HWC v2', 'section_id': self.ids['sec_c'],
            'subject_id': self.ids['ARAB'], 'due_date': due})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(set(resp.get_json()['homework']),
                         {'id', 'title', 'description', 'section_id', 'section_name',
                          'grade_name', 'subject_id', 'subject_name', 'due_date',
                          'attachment_url', 'attachment_name', 'attachment_type'})
        hw = self._row(Homework, 'HWC')
        self.assertEqual((hw.title, hw.section_id, hw.institute_group_id),
                         ('HWC v2', self.ids['sec_c'], None))

    # ── auth ──────────────────────────────────────────────────────────────────

    def test_19_21_parent_school_teacher_anonymous(self):
        routes = [('GET', f'{API}/institute/exams'),
                  ('POST', f'{API}/institute/exams'),
                  ('GET', f"{API}/institute/exams/{self.ids['EXA0']}"),
                  ('POST', f"{API}/institute/exams/{self.ids['EXA0']}/results"),
                  ('POST', f'{API}/institute/homework'),
                  ('PUT', f"{API}/institute/homework/{self.ids['HWA']}")]
        for method, path in routes:
            with self.subTest(method=method, path=path):
                self.assertEqual(self._call('pa', method, path, json={}).status_code, 403)
                resp = self._call('tc', method, path, json={})
                self.assertEqual((resp.status_code, resp.get_json()),
                                 (404, {'ok': False, 'error': 'institute_not_available'}))
                self.assertEqual(self.app.test_client().open(path, method=method,
                                                             json={}).status_code, 401)


if __name__ == '__main__':
    unittest.main()

"""Clearing a stored score on the grade-entry screen means "not entered yet".

grades.enter_results used to skip an emptied box, so the old ExamResult row
(marks, grade_letter, is_pass, rank) survived and kept showing as a grade.
An emptied box of a student that already has a result now removes that row,
which is exactly the state of a student whose grade was never entered.
ExamResult.marks is NOT NULL, so removal — not NULL-ing — is the model's own
representation of "ungraded"; every reader (results page, gradebook, parent
web/mobile, badges, evaluations) already treats a missing row that way.

Covers:
  1. A valid score is stored with its derived fields.
  2. Editing a stored score to another valid score updates it.
  3. Clearing a stored score removes the result (ungraded).
  4. A cleared score shows no pass/fail on the results page.
  5. …and no letter grade (تقدير).
  6. Parent mobile and parent web no longer list it as an entered grade.
  7. Blank is never stored as 0.
  8. Other students of the same exam are unchanged; ranks are recomputed.
  9. The gradebook report counts the student as ungraded, not passed/failed.
  +  A student whose field was not submitted at all is left untouched.
  +  A cleared student is not notified as "new grade".
  +  Cross-school exam and unassigned teacher cannot clear anything.
  +  Institute: roster clear works; historical rows cannot be cleared; the
     clear is part of the atomic all-or-nothing save.
"""
import unittest
from datetime import date, datetime
from unittest.mock import patch
from uuid import uuid4

from flask_login import login_user, logout_user
from werkzeug.exceptions import HTTPException

from app import create_app
from app.models import (db, AcademicYear, Employee, Exam, ExamResult, Grade,
                        InstituteGroupEnrollment, InstituteStudyGroup,
                        Notification, PushNotification, Role, School, Section,
                        Student, Subject, User, parent_students,
                        teacher_subjects)

OPTS = {'bypass_tenant_scope': True}


class GradeClearScoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    # ── Fixtures ─────────────────────────────────────────────────────────────

    def _user(self, school_id, label, role_id):
        u = User(username=f'{label}_{self.suffix}',
                 email=f'{label}_{self.suffix}@example.test',
                 full_name=f'{label} {self.suffix}',
                 role_id=role_id, school_id=school_id, is_active=True)
        u.set_password('Password123')
        db.session.add(u)
        db.session.flush()
        return u

    def _employee(self, school_id, label, user_id):
        e = Employee(school_id=school_id, employee_id=f'E{label}{self.suffix}'[:38],
                     full_name=f'{label} {self.suffix}', base_salary=0,
                     status='active', user_id=user_id)
        db.session.add(e)
        db.session.flush()
        return e

    def _student(self, school_id, year_id, name, section_id=None):
        s = Student(student_id=f'S-{uuid4().hex[:10]}', full_name=name,
                    school_id=school_id, academic_year_id=year_id,
                    section_id=section_id, status='active')
        db.session.add(s)
        db.session.flush()
        return s

    def _school(self, label, *, institute=False):
        school = School(
            school_name=f'{label} {self.suffix}',
            code=f'{label.upper()}{self.suffix}'[:20], capacity=0, is_active=True,
            institution_type=(School.INSTITUTION_INSTITUTE if institute else None))
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'{label} Y {self.suffix}',
                            start_date=date(2025, 8, 1), end_date=date(2026, 6, 30),
                            is_current=True)
        db.session.add(year)
        db.session.flush()
        subject = Subject(school_id=school.id, academic_year_id=year.id,
                          name=f'مادة {label}', code=f'S{label[:2]}{self.suffix[:6]}')
        db.session.add(subject)
        db.session.flush()
        return school, year, subject

    def _section(self, school, year, subject):
        grade = Grade(school_id=school.id, academic_year_id=year.id,
                      name=f'G{uuid4().hex[:6]}')
        db.session.add(grade)
        db.session.flush()
        subject.grade_id = grade.id
        sec = Section(school_id=school.id, academic_year_id=year.id,
                      grade_id=grade.id, name=f'A{uuid4().hex[:4]}', capacity=30)
        db.session.add(sec)
        db.session.flush()
        return sec

    def _exam(self, school, year, subject, *, section_id=None, group_id=None):
        e = Exam(school_id=school.id, academic_year_id=year.id,
                 subject_id=subject.id, section_id=section_id,
                 institute_group_id=group_id, exam_name=f'Exam {uuid4().hex[:6]}',
                 exam_date=date.today(), max_marks=100, pass_marks=50)
        db.session.add(e)
        db.session.flush()
        return e

    def _result(self, exam, student_id, marks, letter, is_pass):
        r = ExamResult(exam_id=exam.id, student_id=student_id,
                       school_id=exam.school_id,
                       academic_year_id=exam.academic_year_id,
                       marks=marks, grade_letter=letter, is_pass=is_pass)
        db.session.add(r)
        db.session.flush()
        return r

    def setUp(self):
        self.suffix = uuid4().hex[:8]
        with self.app.app_context():
            teacher_role = Role.query.filter_by(name='teacher').first().id
            parent_role = Role.query.filter_by(name='parent').first().id

            # ── School A ─────────────────────────────────────────────────────
            sch, year, subj = self._school('GcA')
            sec = self._section(sch, year, subj)
            ut = self._user(sch.id, 'gct', teacher_role)
            et = self._employee(sch.id, 'T', ut.id)
            db.session.execute(Section.__table__.update()
                               .where(Section.id == sec.id).values(teacher_id=et.id))
            db.session.execute(teacher_subjects.insert().values(
                employee_id=et.id, subject_id=subj.id, section_id=sec.id))
            u_other = self._user(sch.id, 'gco', teacher_role)   # unassigned
            self._employee(sch.id, 'O', u_other.id)

            s1 = self._student(sch.id, year.id, 'First Child', sec.id)
            s2 = self._student(sch.id, year.id, 'Second Child', sec.id)
            s3 = self._student(sch.id, year.id, 'Third Child', sec.id)
            parent = self._user(sch.id, 'gcp', parent_role)
            db.session.execute(parent_students.insert().values(
                user_id=parent.id, student_id=s1.id, relation='guardian'))

            exam = self._exam(sch, year, subj, section_id=sec.id)

            # ── School B (cross-tenant target) ──────────────────────────────
            bsch, byear, bsubj = self._school('GcB')
            bsec = self._section(bsch, byear, bsubj)
            bstu = self._student(bsch.id, byear.id, 'Other School Child', bsec.id)
            bexam = self._exam(bsch, byear, bsubj, section_id=bsec.id)
            bres = self._result(bexam, bstu.id, 70, 'C', True)

            # ── Institute (atomic institute branch) ─────────────────────────
            inst, iyear, isubj = self._school('GcI', institute=True)
            ui = self._user(inst.id, 'gci', teacher_role)
            ei = self._employee(inst.id, 'I', ui.id)
            grp = InstituteStudyGroup(school_id=inst.id, academic_year_id=iyear.id,
                                      subject_id=isubj.id, instructor_id=ei.id,
                                      name='I-Group', is_active=True)
            db.session.add(grp)
            db.session.flush()
            i1 = self._student(inst.id, iyear.id, 'Inst One')
            i2 = self._student(inst.id, iyear.id, 'Inst Two')
            i_hist = self._student(inst.id, iyear.id, 'Inst Ended')
            for st, ended in ((i1, False), (i2, False), (i_hist, True)):
                db.session.add(InstituteGroupEnrollment(
                    school_id=inst.id, group_id=grp.id, student_id=st.id,
                    status=(InstituteGroupEnrollment.STATUS_ENDED if ended
                            else InstituteGroupEnrollment.STATUS_ACTIVE),
                    ended_at=(datetime.utcnow() if ended else None)))
            db.session.flush()
            iexam = self._exam(inst, iyear, isubj, group_id=grp.id)

            db.session.commit()
            self.ids = {
                'sch': sch.id, 'sec': sec.id, 'ut': ut.id, 'u_other': u_other.id,
                's1': s1.id, 's2': s2.id, 's3': s3.id, 'parent': parent.id,
                's1_code': s1.student_id, 'exam': exam.id,
                'bsch': bsch.id, 'bexam': bexam.id, 'bres': bres.id,
                'inst': inst.id, 'ui': ui.id, 'i1': i1.id, 'i2': i2.id,
                'i_hist': i_hist.id, 'iexam': iexam.id,
            }

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for sid in (self.ids['sch'], self.ids['bsch'], self.ids['inst']):
                for model in (PushNotification, Notification, ExamResult, Exam,
                              InstituteGroupEnrollment, InstituteStudyGroup):
                    for row in (model.query.execution_options(**OPTS)
                                .filter_by(school_id=sid).all()):
                        db.session.delete(row)
                    db.session.flush()
                db.session.execute(parent_students.delete().where(
                    parent_students.c.student_id.in_(
                        db.session.query(Student.id)
                        .filter(Student.school_id == sid))))
                db.session.execute(teacher_subjects.delete().where(
                    teacher_subjects.c.employee_id.in_(
                        db.session.query(Employee.id)
                        .filter(Employee.school_id == sid))))
                db.session.execute(Section.__table__.update()
                                   .where(Section.school_id == sid)
                                   .values(teacher_id=None))
                db.session.flush()
                for model in (Student, Section, Subject, Grade, Employee, User):
                    for row in (model.query.execution_options(**OPTS)
                                .filter_by(school_id=sid).all()):
                        db.session.delete(row)
                    db.session.flush()
                for row in (AcademicYear.query.execution_options(**OPTS)
                            .filter_by(school_id=sid).all()):
                    db.session.delete(row)
                db.session.flush()
                s = db.session.get(School, sid, execution_options=OPTS)
                if s is not None:
                    db.session.delete(s)
                db.session.flush()
            db.session.commit()
            db.session.remove()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _login(self, user_key):
        login_user(db.session.get(User, self.ids[user_key], execution_options=OPTS))
        for fn in self.app.before_request_funcs.get(None, []):
            if fn() is not None:
                break

    def _post(self, user_key, exam_key, data):
        from app.blueprints.grades import enter_results
        exam_id = self.ids[exam_key]
        with self.app.test_request_context(f'/grades/exams/{exam_id}/results',
                                           method='POST', data=data):
            self._login(user_key)
            try:
                return enter_results(exam_id)
            except HTTPException as exc:
                return exc
            finally:
                logout_user()

    def _page(self, user_key, exam_key):
        from app.blueprints.grades import enter_results
        exam_id = self.ids[exam_key]
        with self.app.test_request_context(f'/grades/exams/{exam_id}/results'):
            self._login(user_key)
            try:
                return enter_results(exam_id)
            finally:
                logout_user()

    def _rows(self, exam_key):
        with self.app.app_context():
            return {r.student_id: (float(r.marks), r.grade_letter, r.is_pass, r.rank)
                    for r in ExamResult.query.execution_options(**OPTS)
                    .filter_by(exam_id=self.ids[exam_key]).all()}

    def _m(self, key):
        return f'marks_{self.ids[key]}'

    def _seed_three(self):
        self._post('ut', 'exam', {self._m('s1'): '80', self._m('s2'): '40',
                                  self._m('s3'): '95'})

    # ── 1 & 2. Real scores are unchanged behaviour ──────────────────────────

    def test_enter_then_edit_valid_score(self):
        self._post('ut', 'exam', {self._m('s1'): '80', self._m('s2'): '',
                                  self._m('s3'): ''})
        rows = self._rows('exam')
        self.assertEqual(set(rows), {self.ids['s1']})
        self.assertEqual(rows[self.ids['s1']][:3], (80.0, 'B', True))

        self._post('ut', 'exam', {self._m('s1'): '30'})
        self.assertEqual(self._rows('exam')[self.ids['s1']][:3], (30.0, 'F', False))

    # ── 3, 7, 8. Clearing removes only that result; blank is never 0 ────────

    def test_clear_existing_score_makes_student_ungraded(self):
        self._seed_three()
        before = self._rows('exam')
        self.assertEqual(len(before), 3)

        self._post('ut', 'exam', {self._m('s1'): '', self._m('s2'): '40',
                                  self._m('s3'): '95'})
        rows = self._rows('exam')
        self.assertNotIn(self.ids['s1'], rows, 'cleared score must leave no result')
        self.assertFalse(any(m == 0.0 for m, *_ in rows.values()),
                         'blank must never be stored as 0')
        self.assertEqual(rows[self.ids['s2']][:3], before[self.ids['s2']][:3])
        self.assertEqual(rows[self.ids['s3']][:3], before[self.ids['s3']][:3])
        # Ranks are recomputed over the remaining graded students only.
        self.assertEqual(rows[self.ids['s3']][3], 1)
        self.assertEqual(rows[self.ids['s2']][3], 2)

    def test_whitespace_only_counts_as_cleared(self):
        self._seed_three()
        self._post('ut', 'exam', {self._m('s1'): '   ', self._m('s2'): '40',
                                  self._m('s3'): '95'})
        self.assertNotIn(self.ids['s1'], self._rows('exam'))

    def test_missing_field_is_left_untouched(self):
        self._seed_three()
        # s3 has no input in the submission at all (e.g. joined the section
        # after the page was loaded): its stored result must survive.
        self._post('ut', 'exam', {self._m('s1'): '', self._m('s2'): '40'})
        rows = self._rows('exam')
        self.assertNotIn(self.ids['s1'], rows)
        self.assertEqual(rows[self.ids['s3']][:3], (95.0, 'A+', True))

    def test_zero_is_still_a_real_score(self):
        self._post('ut', 'exam', {self._m('s1'): '0'})
        self.assertEqual(self._rows('exam')[self.ids['s1']][:3], (0.0, 'F', False))

    # ── 4 & 5. The results page shows no pass/fail and no تقدير ─────────────

    def test_results_page_shows_cleared_student_as_ungraded(self):
        self._seed_three()
        self._post('ut', 'exam', {self._m('s1'): '', self._m('s2'): '40',
                                  self._m('s3'): '95'})
        html = self._page('ut', 'exam')
        start = html.index(f'<code>{self.ids["s1_code"]}</code>')
        row = html[start:html.index('</tr>', start)]
        self.assertNotIn('ناجح', row)
        self.assertNotIn('راسب', row)
        self.assertIn('value=""', row, 'the box must render empty, not 0')
        self.assertIn('—', row)
        for letter in ('A+', '>A<', 'B+', '>B<', 'C+', '>C<', '>D<', '>F<'):
            self.assertNotIn(letter, row.replace(' ', '').replace('\n', ''))

    # ── 6. Parent mobile + web no longer see a grade ────────────────────────

    def test_parent_mobile_and_web_see_no_grade_after_clear(self):
        self._seed_three()
        self._post('ut', 'exam', {self._m('s1'): '', self._m('s2'): '40',
                                  self._m('s3'): '95'})

        from app.blueprints.mobile_api.utils import encode_token
        with self.app.app_context():
            token = encode_token(db.session.get(User, self.ids['parent'],
                                                execution_options=OPTS))
        resp = self.app.test_client().get(
            f'/api/mobile/v1/parent/children/{self.ids["s1"]}/grades',
            headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:300])
        body = resp.get_json()
        self.assertEqual(body['count'], 0)
        self.assertEqual(body['results'], [])

        # Parent web child page reads the same rows.
        from app.blueprints.parent import child_overview
        with self.app.test_request_context(f'/parent/child/{self.ids["s1"]}'):
            self._login('parent')
            try:
                with patch('app.blueprints.parent.render_template',
                           side_effect=lambda *a, **k: k) as _rt:
                    ctx = child_overview(self.ids['s1'])
            finally:
                logout_user()
        self.assertEqual(list(ctx['results']), [])

    # ── 9. Reports treat the student as ungraded ────────────────────────────

    def test_gradebook_report_counts_cleared_student_as_ungraded(self):
        self._seed_three()
        self._post('ut', 'exam', {self._m('s1'): '', self._m('s2'): '40',
                                  self._m('s3'): '95'})
        from app.blueprints.grades import _pivot_data
        with self.app.test_request_context('/grades/gradebook'):
            self._login('ut')
            try:
                exams, rows = _pivot_data(self.ids['sec'], 'all', 'all', '', '')
            finally:
                logout_user()
        by_student = {r['student'].id: r for r in rows}
        self.assertEqual(by_student[self.ids['s1']]['taken'], 0)
        self.assertIsNone(by_student[self.ids['s1']]['avg'])
        self.assertEqual(by_student[self.ids['s1']]['cells'], [None])
        self.assertEqual(by_student[self.ids['s2']]['taken'], 1)
        # Pass / fail totals for the exam exclude the cleared student.
        rows_now = self._rows('exam')
        self.assertEqual(sum(1 for *_, p, _r in rows_now.values() if p), 1)
        self.assertEqual(sum(1 for *_, p, _r in rows_now.values() if p is False), 1)

    # ── Notifications: a cleared student is not told "new grade" ────────────

    def test_clear_does_not_notify_cleared_student(self):
        self._seed_three()
        notified = []
        with patch('app.blueprints.grades._notify_grade_results',
                   side_effect=lambda exam, students:
                   notified.extend(s.id for s in students)):
            self._post('ut', 'exam', {self._m('s1'): '', self._m('s2'): '41'})
        self.assertNotIn(self.ids['s1'], notified)
        self.assertIn(self.ids['s2'], notified)

    # ── Isolation: nobody outside scope can clear a result ──────────────────

    def test_cross_school_exam_cannot_be_cleared(self):
        resp = self._post('ut', 'bexam', {'marks_999999': ''})
        self.assertIn(getattr(resp, 'code', None), (403, 404))
        with self.app.app_context():
            r = db.session.get(ExamResult, self.ids['bres'], execution_options=OPTS)
            self.assertIsNotNone(r, 'another school\'s result must survive')
            self.assertEqual(float(r.marks), 70.0)

    def test_unassigned_teacher_cannot_clear(self):
        self._seed_three()
        self._post('u_other', 'exam', {self._m('s1'): '', self._m('s2'): '',
                                       self._m('s3'): ''})
        self.assertEqual(len(self._rows('exam')), 3)

    # ── Institute branch: atomic, roster-only clear ─────────────────────────

    def test_institute_clear_is_roster_only_and_atomic(self):
        with self.app.app_context():
            iexam = db.session.get(Exam, self.ids['iexam'], execution_options=OPTS)
            self._result(iexam, self.ids['i1'], 80, 'B', True)
            self._result(iexam, self.ids['i2'], 60, 'D', True)
            self._result(iexam, self.ids['i_hist'], 55, 'F', True)
            db.session.commit()

        # A clear smuggled with an invalid score: nothing is written.
        self._post('ui', 'iexam', {self._m('i1'): '', self._m('i2'): '500'})
        self.assertEqual(len(self._rows('iexam')), 3, 'atomic: no partial clear')

        # A blank for a historical (ended) student is ignored, never a delete.
        self._post('ui', 'iexam', {self._m('i1'): '', self._m('i2'): '61',
                                   self._m('i_hist'): ''})
        rows = self._rows('iexam')
        self.assertNotIn(self.ids['i1'], rows)
        self.assertEqual(rows[self.ids['i2']][0], 61.0)
        self.assertEqual(rows[self.ids['i_hist']][0], 55.0,
                         'a historical result must never be cleared')


if __name__ == '__main__':
    unittest.main()

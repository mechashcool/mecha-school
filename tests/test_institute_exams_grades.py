"""Institute-group-targeted exams and grade entry — focused guarantees only.

Covers exactly the eighteen acceptance points for this phase:

  1.  A normal school exam still uses grade/section/subject.
  2.  An institute instructor can create an exam for their own active group.
  3.  The subject is derived from the group; a forged subject is ignored.
  4.  Another instructor's group is rejected with no exam row written.
  5.  Cross-school and cross-year group ids are rejected.
  6.  An institute manager reaches any group of the institute (existing perm).
  7.  The grade roster holds only actively enrolled students of that group.
  8.  Ended / inactive enrollments cannot receive a new grade.
  9.  A forged unrelated student id cannot create or modify a grade.
  10. Duplicate joins never duplicate a student or a result.
  11. Score limits and the zero / absent / not-entered semantics hold.
  12. Bulk grade entry is atomic on an invalid or unauthorised row.
  13. An instructor cannot view, grade or reach another instructor's exam.
  14. An exam target cannot change once results exist.
  15. Ending an enrollment never deletes a stored historical grade.
  16. Historical school exams and results stay readable and unchanged.
  17. Parent web visibility is scoped through the linked student only.
  18. The exam notification reaches only the target group.

The project has NO exam-edit, exam-delete and no exam-publish route — the only
mutation routes are grades.create_exam and grades.enter_results — so points 13
and 14 are asserted against those two plus the shared dependency guard, and no
new route was invented to satisfy them.

A later correction added the institute-only history filters. Those cases are
grouped at the end of this file under HISTORICAL ACCESS and prove:

  A.  The default exam list shows active-group exams only.
  B.  An inactive-group exam is absent from the default view…
  C.  …present under `historical`…
  D.  …and present exactly once under `all`.
  E.  A forged exam-status value falls back to `current`.
  F.  A manager sees a historical exam; G. so does its own instructor.
  H.  Another instructor cannot, through the list or a direct URL.
  I.  Cross-school and cross-year accounts cannot.
  J.  The default results view lists currently eligible students only.
  K.  An ended enrollment with a stored result is visible historically…
  L.  …as is an inactive student with a stored result…
  M.  …while an ended/inactive student with NO result is never shown.
  N.  `all` is the distinct union, with no duplicated student.
  O.  Historical rows carry no input element.
  P.  A forged POST for a historical row is rejected atomically.
  Q.  An inactive group refuses every create and update.
  R.  Its results page defaults to the historical view.
  S.  An active eligible student stays editable exactly as before.

Out of scope here: homework, attendance, schedules, mobile API, Flutter.
"""
import unittest
from datetime import date, datetime, timedelta
from uuid import uuid4

from flask_login import login_user, logout_user
from werkzeug.exceptions import NotFound

from app import create_app
from app.models import (db, AcademicYear, AuditLog, Employee, Exam, ExamResult,
                        Grade, InstituteGroupEnrollment, InstituteStudyGroup,
                        Notification, PushNotification, Role, School, Section,
                        Student, Subject, User, parent_students,
                        teacher_subjects)

OPTS = {'bypass_tenant_scope': True}


class InstituteExamGradeTest(unittest.TestCase):
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

    def _employee(self, school_id, label, user_id=None):
        e = Employee(school_id=school_id, employee_id=f'E{label}{self.suffix}'[:38],
                     full_name=f'{label} {self.suffix}', base_salary=0,
                     status='active', user_id=user_id)
        db.session.add(e)
        db.session.flush()
        return e

    def _student(self, school_id, year_id, name, section_id=None, status='active'):
        s = Student(student_id=f'S-{uuid4().hex[:10]}', full_name=name,
                    school_id=school_id, academic_year_id=year_id,
                    section_id=section_id, status=status)
        db.session.add(s)
        db.session.flush()
        return s

    def _link_parent(self, user_id, student_id):
        db.session.execute(parent_students.insert().values(
            user_id=user_id, student_id=student_id, relation='guardian'))
        db.session.flush()

    def _group(self, school_id, year_id, subject_id, name, instructor_id,
               *, active=True):
        g = InstituteStudyGroup(school_id=school_id, academic_year_id=year_id,
                                subject_id=subject_id, instructor_id=instructor_id,
                                name=name, is_active=active)
        db.session.add(g)
        db.session.flush()
        return g

    def _enroll(self, school_id, group_id, student_id, *, ended=False):
        row = InstituteGroupEnrollment(
            school_id=school_id, group_id=group_id, student_id=student_id,
            status=(InstituteGroupEnrollment.STATUS_ENDED if ended
                    else InstituteGroupEnrollment.STATUS_ACTIVE),
            ended_at=(datetime.utcnow() if ended else None))
        db.session.add(row)
        db.session.flush()
        return row

    def _institution(self, label, *, institute):
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
                          name=f'مادة {label}',
                          code=f'S{label[:2]}{self.suffix[:6]}')
        db.session.add(subject)
        db.session.flush()
        return school, year, subject

    def setUp(self):
        self.suffix = uuid4().hex[:8]
        with self.app.app_context():
            self.teacher_role_id = Role.query.filter_by(name='teacher').first().id
            self.admin_role_id = Role.query.filter_by(name='school_admin').first().id
            self.parent_role_id = Role.query.filter_by(name='parent').first().id

            # ── Institute A ──────────────────────────────────────────────────
            inst, iyear, isubj = self._institution('ExA', institute=True)
            other_subj = Subject(school_id=inst.id, academic_year_id=iyear.id,
                                 name='مادة أخرى', code=f'X{self.suffix[:8]}')
            db.session.add(other_subj)
            old_year = AcademicYear(school_id=inst.id, name=f'Old {self.suffix}',
                                    start_date=date(2024, 8, 1),
                                    end_date=date(2025, 6, 30), is_current=False)
            db.session.add(old_year)
            db.session.flush()
            # A group must carry a subject of its OWN year; the exam validator
            # refuses a cross-year subject, so the old year gets its own.
            old_subj = Subject(school_id=inst.id, academic_year_id=old_year.id,
                               name='مادة سابقة', code=f'O{self.suffix[:8]}')
            db.session.add(old_subj)
            db.session.flush()

            ua = self._user(inst.id, 'exa', self.teacher_role_id)
            ub = self._user(inst.id, 'exb', self.teacher_role_id)
            uadmin = self._user(inst.id, 'exadm', self.admin_role_id)
            ea = self._employee(inst.id, 'A', ua.id)
            eb = self._employee(inst.id, 'B', ub.id)

            ga = self._group(inst.id, iyear.id, isubj.id, 'A-Group', ea.id)
            gb = self._group(inst.id, iyear.id, isubj.id, 'B-Group', eb.id)
            g_off = self._group(inst.id, iyear.id, isubj.id, 'A-Off', ea.id,
                                active=False)
            g_old = self._group(inst.id, old_year.id, old_subj.id, 'A-Old', ea.id)

            s_in = self._student(inst.id, iyear.id, 'Enrolled Student')
            s_two = self._student(inst.id, iyear.id, 'In Two Groups')
            s_out = self._student(inst.id, iyear.id, 'Outside Student')
            s_ended = self._student(inst.id, iyear.id, 'Ended Student')
            s_b = self._student(inst.id, iyear.id, 'B Group Student')
            s_inactive = self._student(inst.id, iyear.id, 'Inactive Student',
                                       status='inactive')
            self._enroll(inst.id, ga.id, s_in.id)
            self._enroll(inst.id, ga.id, s_two.id)
            self._enroll(inst.id, gb.id, s_two.id)      # same student, 2 groups
            self._enroll(inst.id, ga.id, s_ended.id, ended=True)
            self._enroll(inst.id, ga.id, s_inactive.id)  # active row, dead student
            self._enroll(inst.id, gb.id, s_b.id)

            p_in = self._user(inst.id, 'exapin', self.parent_role_id)
            p_out = self._user(inst.id, 'exapout', self.parent_role_id)
            p_b = self._user(inst.id, 'exapb', self.parent_role_id)
            self._link_parent(p_in.id, s_in.id)
            self._link_parent(p_out.id, s_out.id)
            self._link_parent(p_b.id, s_b.id)

            # ── Institute B (cross-tenant) ──────────────────────────────────
            oinst, oyear, osubj = self._institution('ExO', institute=True)
            uo = self._user(oinst.id, 'exo', self.teacher_role_id)
            eo = self._employee(oinst.id, 'O', uo.id)
            go = self._group(oinst.id, oyear.id, osubj.id, 'O-Group', eo.id)

            # ── Ordinary school (regression baseline) ───────────────────────
            sch, syear, ssubj = self._institution('ExS', institute=False)
            grade = Grade(school_id=sch.id, academic_year_id=syear.id,
                          name=f'G{self.suffix[:4]}')
            db.session.add(grade)
            db.session.flush()
            ssubj.grade_id = grade.id     # the school subject guard needs this
            sec = Section(school_id=sch.id, academic_year_id=syear.id,
                          grade_id=grade.id, name=f'A{self.suffix[:4]}', capacity=30)
            db.session.add(sec)
            db.session.flush()
            us = self._user(sch.id, 'exsch', self.teacher_role_id)
            es = self._employee(sch.id, 'S', us.id)
            db.session.execute(Section.__table__.update()
                               .where(Section.id == sec.id).values(teacher_id=es.id))
            db.session.execute(teacher_subjects.insert().values(
                employee_id=es.id, subject_id=ssubj.id, section_id=sec.id))
            sch_student = self._student(sch.id, syear.id, 'School Child', sec.id)

            # A pre-existing (historical) school exam + result, written the way
            # every row in production already looks: a section, no group.
            legacy_exam = Exam(school_id=sch.id, academic_year_id=syear.id,
                               subject_id=ssubj.id, section_id=sec.id,
                               exam_name='Legacy Exam',
                               exam_date=date.today() - timedelta(days=10),
                               max_marks=100, pass_marks=50)
            db.session.add(legacy_exam)
            db.session.flush()
            legacy_result = ExamResult(exam_id=legacy_exam.id,
                                       student_id=sch_student.id,
                                       school_id=sch.id, academic_year_id=syear.id,
                                       marks=77, grade_letter='C+', is_pass=True)
            db.session.add(legacy_result)
            db.session.flush()

            db.session.commit()

            self.ids = {
                'inst': inst.id, 'iyear': iyear.id, 'isubj': isubj.id,
                'other_subj': other_subj.id, 'old_year': old_year.id,
                'ua': ua.id, 'ub': ub.id, 'uadmin': uadmin.id,
                'ea': ea.id, 'eb': eb.id,
                'ga': ga.id, 'gb': gb.id, 'g_off': g_off.id, 'g_old': g_old.id,
                's_in': s_in.id, 's_two': s_two.id, 's_out': s_out.id,
                's_ended': s_ended.id, 's_b': s_b.id, 's_inactive': s_inactive.id,
                'p_in': p_in.id, 'p_out': p_out.id, 'p_b': p_b.id,
                'oinst': oinst.id, 'go': go.id, 'uo': uo.id,
                'sch': sch.id, 'syear': syear.id, 'ssubj': ssubj.id,
                'grade': grade.id, 'sec': sec.id, 'us': us.id, 'es': es.id,
                'sch_student': sch_student.id,
                'legacy_exam': legacy_exam.id, 'legacy_result': legacy_result.id,
            }

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for sid in (self.ids['inst'], self.ids['oinst'], self.ids['sch']):
                for model in (AuditLog, PushNotification, Notification,
                              ExamResult, Exam,
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
                sch = db.session.get(School, sid, execution_options=OPTS)
                if sch is not None:
                    db.session.delete(sch)
                db.session.flush()
            db.session.commit()
            db.session.remove()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _run_before_request(self):
        for fn in self.app.before_request_funcs.get(None, []):
            if fn() is not None:
                break

    def _login(self, user_key):
        user = db.session.get(User, self.ids[user_key], execution_options=OPTS)
        login_user(user)
        self._run_before_request()

    def _exams(self, school_id):
        return (Exam.query.execution_options(**OPTS)
                .filter_by(school_id=school_id).all())

    def _exam_form(self, **over):
        data = {
            'exam_name': 'Group Exam',
            'exam_date': date.today().strftime('%Y-%m-%d'),
            'max_marks': '100',
            'pass_marks': '50',
        }
        data.update(over)
        return data

    def _post_create(self, user_key, data):
        from app.blueprints.grades import create_exam
        with self.app.test_request_context('/grades/exams/create',
                                           method='POST', data=data):
            self._login(user_key)
            try:
                return create_exam()
            finally:
                logout_user()

    def _post_results(self, user_key, exam_id, data):
        from app.blueprints.grades import enter_results
        with self.app.test_request_context(f'/grades/exams/{exam_id}/results',
                                           method='POST', data=data):
            self._login(user_key)
            try:
                return enter_results(exam_id)
            finally:
                logout_user()

    def _get_results_page(self, user_key, exam_id):
        from app.blueprints.grades import enter_results
        with self.app.test_request_context(f'/grades/exams/{exam_id}/results'):
            self._login(user_key)
            try:
                return enter_results(exam_id)
            finally:
                logout_user()

    def _make_institute_exam(self, group_key='ga', name='Scoped Exam'):
        with self.app.app_context():
            g = db.session.get(InstituteStudyGroup, self.ids[group_key],
                               execution_options=OPTS)
            e = Exam(school_id=self.ids['inst'], academic_year_id=g.academic_year_id,
                     subject_id=g.subject_id, section_id=None,
                     institute_group_id=g.id, exam_name=name,
                     exam_date=date.today(), max_marks=100, pass_marks=50)
            db.session.add(e)
            db.session.commit()
            return e.id

    # ── 1 & 16. School behaviour and history are untouched ──────────────────

    def test_school_exam_still_uses_section_and_subject(self):
        from app.blueprints.grades import index
        data = self._exam_form(exam_name='School Exam',
                               section_id=str(self.ids['sec']),
                               grade_id=str(self.ids['grade']),
                               subject_id=str(self.ids['ssubj']),
                               academic_year_id=str(self.ids['syear']))
        self._post_create('us', data)

        with self.app.app_context():
            rows = [e for e in self._exams(self.ids['sch'])
                    if e.exam_name == 'School Exam']
            self.assertEqual(len(rows), 1, 'the school create path must still work')
            self.assertEqual(rows[0].section_id, self.ids['sec'])
            self.assertIsNone(rows[0].institute_group_id,
                              'a school exam carries no institute group')
            self.assertEqual(rows[0].subject_id, self.ids['ssubj'])

        with self.app.test_request_context('/grades/'):
            self._login('us')
            html = index()
            logout_user()
        self.assertIn('School Exam', html)
        self.assertIn('الصف / الشعبة', html,
                      'the school section column must remain')
        self.assertNotIn('المجموعة الدراسية', html,
                         'a school never sees the study-group control')

    def test_legacy_school_exam_and_result_unchanged(self):
        with self.app.app_context():
            e = db.session.get(Exam, self.ids['legacy_exam'],
                               execution_options=OPTS)
            r = db.session.get(ExamResult, self.ids['legacy_result'],
                               execution_options=OPTS)
            self.assertEqual(e.section_id, self.ids['sec'])
            self.assertIsNone(e.institute_group_id, 'no backfill may touch a row')
            self.assertEqual(float(r.marks), 77.0)

        from app.blueprints.grades import index
        with self.app.test_request_context('/grades/'):
            self._login('us')
            html = index()
            logout_user()
        self.assertIn('Legacy Exam', html, 'historical exams stay readable')

    # ── 2 & 3. Institute create; subject derived, not posted ────────────────

    def test_instructor_creates_for_own_group_with_derived_subject(self):
        self._post_create('ua', self._exam_form(
            institute_group_id=str(self.ids['ga']),
            # Forged values that must all be ignored:
            subject_id=str(self.ids['other_subj']),
            section_id=str(self.ids['sec']),
            grade_id='999999',
            academic_year_id=str(self.ids['old_year'])))

        with self.app.app_context():
            rows = [e for e in self._exams(self.ids['inst'])
                    if e.exam_name == 'Group Exam']
            self.assertEqual(len(rows), 1)
            e = rows[0]
            self.assertEqual(e.institute_group_id, self.ids['ga'])
            self.assertIsNone(e.section_id, 'never both targets')
            self.assertEqual(e.subject_id, self.ids['isubj'],
                             'the subject must come from the group, not the POST')
            self.assertNotEqual(e.subject_id, self.ids['other_subj'])
            self.assertEqual(e.school_id, self.ids['inst'])
            self.assertEqual(e.academic_year_id, self.ids['iyear'],
                             'a posted academic_year_id must be ignored')

    # ── 4 & 5. Foreign, inactive and cross-year groups are refused ──────────

    def test_ineligible_groups_are_rejected_and_write_nothing(self):
        for key in ('gb', 'g_off', 'g_old', 'go'):
            self._post_create('ua', self._exam_form(
                exam_name=f'Bad {key}', institute_group_id=str(self.ids[key])))
            with self.app.app_context():
                for sid in (self.ids['inst'], self.ids['oinst']):
                    names = [e.exam_name for e in self._exams(sid)]
                    self.assertNotIn(f'Bad {key}', names,
                                     f'{key} must not produce an exam row')

    def test_missing_group_is_rejected(self):
        self._post_create('ua', self._exam_form(exam_name='No Group'))
        with self.app.app_context():
            self.assertNotIn('No Group',
                             [e.exam_name for e in self._exams(self.ids['inst'])])

    # ── 6. Manager follows the existing permission ──────────────────────────

    def test_manager_may_target_any_group_of_the_institute(self):
        self._post_create('uadmin', self._exam_form(
            exam_name='Admin Exam', institute_group_id=str(self.ids['gb'])))
        with self.app.app_context():
            rows = [e for e in self._exams(self.ids['inst'])
                    if e.exam_name == 'Admin Exam']
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].institute_group_id, self.ids['gb'])

        # …but still not another institute's group.
        self._post_create('uadmin', self._exam_form(
            exam_name='Admin Cross', institute_group_id=str(self.ids['go'])))
        with self.app.app_context():
            for sid in (self.ids['inst'], self.ids['oinst']):
                self.assertNotIn('Admin Cross',
                                 [e.exam_name for e in self._exams(sid)])

    # ── 7 & 10. Roster is exactly the active members, each once ─────────────

    def test_roster_holds_only_active_enrolled_students_once(self):
        exam_id = self._make_institute_exam('ga')
        html = self._get_results_page('ua', exam_id)

        self.assertIn('Enrolled Student', html)
        self.assertIn('In Two Groups', html)
        self.assertNotIn('Ended Student', html,
                         'an ended enrollment is not on the roster')
        self.assertNotIn('Outside Student', html,
                         'a student of no group must not appear')
        self.assertNotIn('B Group Student', html,
                         "another group's student must not appear")
        self.assertNotIn('Inactive Student', html,
                         'a deactivated student receives no new grade')
        # A student enrolled in two of the institute's groups appears ONCE.
        self.assertEqual(html.count('In Two Groups'), 1,
                         'duplicate joins must not duplicate a student')

    # ── 8 & 9 & 12. Ineligible or forged ids reject the whole submission ────

    def test_forged_or_ineligible_student_blocks_the_whole_save(self):
        exam_id = self._make_institute_exam('ga')
        for bad_key in ('s_ended', 's_out', 's_b', 's_inactive'):
            self._post_results('ua', exam_id, {
                f'marks_{self.ids["s_in"]}': '80',      # a valid row…
                f'marks_{self.ids[bad_key]}': '90',     # …plus an invalid one
            })
            with self.app.app_context():
                rows = (ExamResult.query.execution_options(**OPTS)
                        .filter_by(exam_id=exam_id).all())
                self.assertEqual(rows, [],
                                 f'{bad_key} must abort the entire save — '
                                 'no partial commit')

    def test_cross_school_student_id_is_refused(self):
        exam_id = self._make_institute_exam('ga')
        with self.app.app_context():
            foreign = self._student(self.ids['oinst'],
                                    db.session.get(
                                        InstituteStudyGroup, self.ids['go'],
                                        execution_options=OPTS).academic_year_id,
                                    'Foreign Student')
            db.session.commit()
            foreign_id = foreign.id

        self._post_results('ua', exam_id, {f'marks_{foreign_id}': '55'})
        with self.app.app_context():
            self.assertEqual(
                ExamResult.query.execution_options(**OPTS)
                .filter_by(exam_id=exam_id).count(), 0)

    # ── 11. Score limits and zero / absent / not-entered semantics ──────────

    def test_score_limits_and_zero_versus_not_entered(self):
        exam_id = self._make_institute_exam('ga')

        # Out of range and non-numeric are both refused, atomically.
        for bad in ('101', '-1', 'abc'):
            self._post_results('ua', exam_id, {
                f'marks_{self.ids["s_in"]}': bad,
                f'marks_{self.ids["s_two"]}': '60',
            })
            with self.app.app_context():
                self.assertEqual(
                    ExamResult.query.execution_options(**OPTS)
                    .filter_by(exam_id=exam_id).count(), 0,
                    f'{bad!r} must abort the whole save')

        # 0 is a real mark; an empty field is "not entered" and writes no row.
        self._post_results('ua', exam_id, {
            f'marks_{self.ids["s_in"]}': '0',
            f'marks_{self.ids["s_two"]}': '',
        })
        with self.app.app_context():
            rows = {r.student_id: r for r in
                    ExamResult.query.execution_options(**OPTS)
                    .filter_by(exam_id=exam_id).all()}
            self.assertIn(self.ids['s_in'], rows, '0 must be stored as a mark')
            self.assertEqual(float(rows[self.ids['s_in']].marks), 0.0)
            self.assertFalse(rows[self.ids['s_in']].is_pass)
            self.assertNotIn(self.ids['s_two'], rows,
                             'an empty field must not create a row')
            # The boundary value is accepted.
            self.assertEqual(len(rows), 1)

        self._post_results('ua', exam_id, {f'marks_{self.ids["s_two"]}': '100'})
        with self.app.app_context():
            r = (ExamResult.query.execution_options(**OPTS)
                 .filter_by(exam_id=exam_id, student_id=self.ids['s_two']).first())
            self.assertIsNotNone(r, 'max_marks exactly is a valid score')
            self.assertEqual(float(r.marks), 100.0)

    # ── 13. Direct-URL access to another instructor's exam ──────────────────

    def test_instructor_cannot_reach_another_instructors_exam(self):
        """Another instructor's group and another YEAR stay fully inaccessible.

        g_off is deliberately NOT in this list: it is instructor A's OWN
        inactive group, which the history correction makes readable to them —
        see test_own_inactive_group_is_readable_but_never_writable.
        """
        from app.blueprints.grades import index, enter_results
        for key in ('gb', 'g_old'):
            exam_id = self._make_institute_exam(key, f'Other {key}')
            for status in (None, 'historical', 'all'):
                with self.app.test_request_context(
                        f'/grades/exams/{exam_id}/results'
                        + (f'?status={status}' if status else '')):
                    self._login('ua')
                    with self.assertRaises(NotFound, msg=f'{key} must 404'):
                        enter_results(exam_id)
                    logout_user()
                # …and it must not leak through any list view either.
                with self.app.test_request_context(
                        '/grades/' + (f'?status={status}' if status else '')):
                    self._login('ua')
                    html = index()
                    logout_user()
                self.assertNotIn(f'Other {key}', html,
                                 f'{key} must not appear under status={status}')

    def test_own_inactive_group_is_readable_but_never_writable(self):
        """The intended behaviour change: an instructor keeps read access to
        their OWN group after it is deactivated, and only read access."""
        from app.blueprints.grades import index
        exam_id = self._make_institute_exam('g_off', 'Own Inactive')

        # Absent from the default current view…
        with self.app.test_request_context('/grades/'):
            self._login('ua')
            html = index()
            logout_user()
        self.assertNotIn('Own Inactive', html,
                         'an inactive group is not part of the current view')

        # …present under historical and all…
        for status in ('historical', 'all'):
            with self.app.test_request_context(f'/grades/?status={status}'):
                self._login('ua')
                html = index()
                logout_user()
            self.assertIn('Own Inactive', html,
                          f'the owning instructor must see it under {status}')

        # …its results page opens read-only…
        page = self._results_html('ua', exam_id)
        self.assertIn('المجموعة الدراسية غير فعّالة', page)
        self.assertNotIn('حفظ الدرجات', page)

        # …and it accepts no write whatsoever.
        self._post_results('ua', exam_id, {f'marks_{self.ids["s_in"]}': '77'})
        with self.app.app_context():
            self.assertEqual(
                ExamResult.query.execution_options(**OPTS)
                .filter_by(exam_id=exam_id).count(), 0,
                'an inactive group must never accept a grade')

    def test_cross_school_exam_is_not_reachable(self):
        from app.blueprints.grades import enter_results
        with self.app.app_context():
            g = db.session.get(InstituteStudyGroup, self.ids['go'],
                               execution_options=OPTS)
            e = Exam(school_id=self.ids['oinst'], academic_year_id=g.academic_year_id,
                     subject_id=g.subject_id, section_id=None,
                     institute_group_id=g.id, exam_name='Foreign Exam',
                     exam_date=date.today(), max_marks=100, pass_marks=50)
            db.session.add(e)
            db.session.commit()
            foreign_exam_id = e.id

        with self.app.test_request_context(
                f'/grades/exams/{foreign_exam_id}/results'):
            self._login('ua')
            with self.assertRaises(Exception):
                enter_results(foreign_exam_id)
            logout_user()

    # ── 14. The target is locked once results exist ─────────────────────────

    def test_target_is_locked_once_results_exist(self):
        from app.blueprints.grades import _exam_has_results
        exam_id = self._make_institute_exam('ga')

        with self.app.test_request_context('/grades/'):
            self._login('ua')
            self.assertFalse(_exam_has_results(exam_id),
                             'a fresh exam has no dependent data')
            logout_user()

        self._post_results('ua', exam_id, {f'marks_{self.ids["s_in"]}': '70'})

        with self.app.test_request_context('/grades/'):
            self._login('ua')
            self.assertTrue(_exam_has_results(exam_id),
                            'the dependency guard must fire once a result exists')
            logout_user()

        # Entering more grades never changes the stored target or subject.
        with self.app.app_context():
            e = db.session.get(Exam, exam_id, execution_options=OPTS)
            self.assertEqual(e.institute_group_id, self.ids['ga'])
            self.assertIsNone(e.section_id)
            self.assertEqual(e.subject_id, self.ids['isubj'])

    # ── 15. Ending an enrollment preserves the stored grade ─────────────────

    def test_ending_enrollment_keeps_the_historical_result(self):
        exam_id = self._make_institute_exam('ga')
        self._post_results('ua', exam_id, {f'marks_{self.ids["s_in"]}': '88'})

        with self.app.app_context():
            row = (InstituteGroupEnrollment.query.execution_options(**OPTS)
                   .filter_by(school_id=self.ids['inst'], group_id=self.ids['ga'],
                              student_id=self.ids['s_in'], status='active').first())
            row.status = InstituteGroupEnrollment.STATUS_ENDED
            row.ended_at = datetime.utcnow()
            db.session.commit()

        with self.app.app_context():
            r = (ExamResult.query.execution_options(**OPTS)
                 .filter_by(exam_id=exam_id, student_id=self.ids['s_in']).first())
            self.assertIsNotNone(r, 'an ended membership never deletes a grade')
            self.assertEqual(float(r.marks), 88.0)
            self.assertEqual(r.exam_id, exam_id,
                             'the exam relationship is preserved')

        # …but no NEW grade may be entered for them any more.
        html = self._get_results_page('ua', exam_id)
        self.assertNotIn('Enrolled Student', html,
                         'an ended member is off the entry roster')
        self._post_results('ua', exam_id, {f'marks_{self.ids["s_in"]}': '10'})
        with self.app.app_context():
            r = (ExamResult.query.execution_options(**OPTS)
                 .filter_by(exam_id=exam_id, student_id=self.ids['s_in']).first())
            self.assertEqual(float(r.marks), 88.0,
                             'the stored mark must not be overwritten')

    # ── 17. Parent web visibility ───────────────────────────────────────────

    def test_parent_sees_only_their_own_childs_institute_result(self):
        from app.blueprints.parent import child_overview
        exam_id = self._make_institute_exam('ga')
        self._post_results('ua', exam_id, {
            f'marks_{self.ids["s_in"]}': '91',
            f'marks_{self.ids["s_two"]}': '42',
        })

        with self.app.test_request_context(f'/parent/child/{self.ids["s_in"]}'):
            self._login('p_in')
            html = child_overview(self.ids['s_in'])
            logout_user()
        # Numeric(6, 2) renders as 91.00 / 42.00, so a stray '42' elsewhere in
        # the page (an id, a colour, a date) cannot fake a match.
        self.assertIn('91.00', html, "the linked child's own result is visible")
        self.assertNotIn('42.00', html,
                         "another student's mark must never appear")
        self.assertIn('Scoped Exam', html,
                      'the institute exam itself is visible to the parent')

        # A parent cannot open an unrelated child at all.
        with self.app.test_request_context(f'/parent/child/{self.ids["s_two"]}'):
            self._login('p_in')
            with self.assertRaises(Exception):
                child_overview(self.ids['s_two'])
            logout_user()

    # ── 18. Exam notification reaches only the target group ─────────────────

    def test_exam_notification_reaches_only_the_group(self):
        self._post_create('ua', self._exam_form(
            exam_name='Notify Exam', institute_group_id=str(self.ids['ga'])))

        with self.app.app_context():
            rows = (Notification.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids['inst'], ntype='exam').all())
            notified = {r.target_user_id for r in rows}
            self.assertIn(self.ids['p_in'], notified,
                          'the parent of an actively enrolled student is notified')
            self.assertNotIn(self.ids['p_out'], notified,
                             'a student outside the group is not reached')
            self.assertNotIn(self.ids['p_b'], notified,
                             "another instructor's group is not reached")
            self.assertEqual(
                Notification.query.execution_options(**OPTS)
                .filter_by(school_id=self.ids['oinst']).count(), 0,
                'nothing crosses the tenant boundary')
            self.assertEqual(
                Notification.query.execution_options(**OPTS)
                .filter_by(school_id=self.ids['sch']).count(), 0)

    # ── The database refuses a two-target or no-target row ──────────────────

    def test_orm_guard_rejects_an_exam_with_both_or_neither_target(self):
        """The relationship validator refuses a bad target before any SQL runs."""
        for section_id, group_id, label in (
                (self.ids['sec'], self.ids['ga'], 'both targets'),
                (None, None, 'no target')):
            with self.app.app_context():
                e = Exam(school_id=self.ids['inst'],
                         academic_year_id=self.ids['iyear'],
                         subject_id=self.ids['isubj'],
                         section_id=section_id, institute_group_id=group_id,
                         exam_name=f'Bad {label}', exam_date=date.today(),
                         max_marks=100, pass_marks=50)
                db.session.add(e)
                with self.assertRaises(ValueError,
                                       msg=f'{label} must be refused'):
                    db.session.commit()
                db.session.rollback()

    def test_database_check_rejects_both_or_neither_target(self):
        """…and the database refuses it too, even if the ORM were bypassed."""
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError
        stmt = text(
            'INSERT INTO exams (school_id, academic_year_id, subject_id, '
            'section_id, institute_group_id, exam_name, exam_date, '
            'max_marks, pass_marks) VALUES (:sc, :yr, :su, :se, :gr, :nm, '
            ':dt, 100, 50)')
        for section_id, group_id, label in (
                (self.ids['sec'], self.ids['ga'], 'both targets'),
                (None, None, 'no target')):
            with self.app.app_context():
                with self.assertRaises(IntegrityError,
                                       msg=f'{label} must violate the CHECK'):
                    db.session.execute(stmt, {
                        'sc': self.ids['inst'], 'yr': self.ids['iyear'],
                        'su': self.ids['isubj'], 'se': section_id,
                        'gr': group_id, 'nm': f'Raw {label}',
                        'dt': date.today()})
                    db.session.flush()
                db.session.rollback()


    # ═══════════════════════════════════════════════════════════════════
    #  HISTORICAL ACCESS — the institute-only status filters
    # ═══════════════════════════════════════════════════════════════════

    def _index_html(self, user_key, **args):
        from app.blueprints.grades import index
        qs = '&'.join(f'{k}={v}' for k, v in args.items() if v is not None)
        with self.app.test_request_context('/grades/' + ('?' + qs if qs else '')):
            self._login(user_key)
            try:
                return index()
            finally:
                logout_user()

    def _results_html(self, user_key, exam_id, **args):
        from app.blueprints.grades import enter_results
        qs = '&'.join(f'{k}={v}' for k, v in args.items() if v is not None)
        url = f'/grades/exams/{exam_id}/results' + ('?' + qs if qs else '')
        with self.app.test_request_context(url):
            self._login(user_key)
            try:
                return enter_results(exam_id)
            finally:
                logout_user()

    def _deactivate_group(self, group_key):
        with self.app.app_context():
            g = db.session.get(InstituteStudyGroup, self.ids[group_key],
                               execution_options=OPTS)
            g.is_active = False
            db.session.commit()

    def _end_enrollment(self, group_key, student_key):
        with self.app.app_context():
            row = (InstituteGroupEnrollment.query.execution_options(**OPTS)
                   .filter_by(school_id=self.ids['inst'],
                              group_id=self.ids[group_key],
                              student_id=self.ids[student_key],
                              status='active').first())
            row.status = InstituteGroupEnrollment.STATUS_ENDED
            row.ended_at = datetime.utcnow()
            db.session.commit()

    def _store_result(self, exam_id, student_key, marks):
        """Write a result directly, bypassing the roster rules."""
        with self.app.app_context():
            e = db.session.get(Exam, exam_id, execution_options=OPTS)
            r = ExamResult(exam_id=exam_id, student_id=self.ids[student_key],
                           school_id=e.school_id,
                           academic_year_id=e.academic_year_id,
                           marks=marks, grade_letter='C', is_pass=True)
            db.session.add(r)
            db.session.commit()
            return r.id

    # ── A, B, C, D, E. The exam-list status filter ─────────────────────────

    def test_exam_list_status_filter(self):
        live_id = self._make_institute_exam('ga', 'Live Exam')
        hist_id = self._make_institute_exam('ga', 'Hist Exam')
        # A second group of the SAME instructor, then deactivated, so the
        # historical row is theirs and the live one is unaffected.
        with self.app.app_context():
            g2 = self._group(self.ids['inst'], self.ids['iyear'],
                             self.ids['isubj'], 'A-Second', self.ids['ea'])
            db.session.commit()
            g2_id = g2.id
            e = db.session.get(Exam, hist_id, execution_options=OPTS)
            e.institute_group_id = g2_id
            db.session.commit()
            g2 = db.session.get(InstituteStudyGroup, g2_id,
                                execution_options=OPTS)
            g2.is_active = False
            db.session.commit()

        # A + B — default shows the active-group exam only.
        html = self._index_html('ua')
        self.assertIn('Live Exam', html)
        self.assertNotIn('Hist Exam', html,
                         'an inactive-group exam must not be in the default view')

        # C — historical shows it, badged, and hides the current one.
        html = self._index_html('ua', status='historical')
        self.assertIn('Hist Exam', html)
        self.assertNotIn('Live Exam', html)
        self.assertIn('سجل تاريخي', html, 'a historical row must be badged')

        # D — all shows both, each exactly once.
        html = self._index_html('ua', status='all')
        self.assertIn('Live Exam', html)
        self.assertIn('Hist Exam', html)
        self.assertEqual(html.count('>Live Exam<'), 1, 'no duplicate rows')
        self.assertEqual(html.count('>Hist Exam<'), 1, 'no duplicate rows')

        # E — a forged status falls back to current.
        for bad in ('deleted', 'CURRENT', '../all', '1'):
            html = self._index_html('ua', status=bad)
            self.assertIn('Live Exam', html)
            self.assertNotIn('Hist Exam', html,
                             f'status={bad!r} must fall back to current')

    # ── F & G. Manager and the owning instructor both see history ──────────

    def test_manager_and_owning_instructor_see_historical_exam(self):
        exam_id = self._make_institute_exam('ga', 'Owned Hist')
        self._deactivate_group('ga')

        for key in ('uadmin', 'ua'):
            html = self._index_html(key, status='historical')
            self.assertIn('Owned Hist', html,
                          f'{key} must see the historical exam')
            # …and may open its results page read-only.
            page = self._results_html(key, exam_id)
            self.assertIn('المجموعة الدراسية غير فعّالة', page)

    # ── H & I. Nobody else does ────────────────────────────────────────────

    def test_other_instructor_and_foreign_users_cannot_see_history(self):
        from app.blueprints.grades import enter_results
        exam_id = self._make_institute_exam('ga', 'Private Hist')
        self._deactivate_group('ga')

        for key in ('ub', 'uo'):
            for status in ('current', 'historical', 'all'):
                html = self._index_html(key, status=status)
                self.assertNotIn('Private Hist', html,
                                 f'{key} must not see it under status={status}')
            with self.app.test_request_context(
                    f'/grades/exams/{exam_id}/results'):
                self._login(key)
                with self.assertRaises(Exception,
                                       msg=f'{key} must be refused by URL'):
                    enter_results(exam_id)
                logout_user()

    def test_cross_year_group_history_stays_out_of_scope(self):
        """A past-year group is not 'historical' — it is simply another year."""
        exam_id = self._make_institute_exam('g_old', 'Old Year Exam')
        for status in ('current', 'historical', 'all'):
            html = self._index_html('ua', status=status)
            self.assertNotIn('Old Year Exam', html,
                             'the year context must never be bypassed')
        self.assertIsNotNone(exam_id)

    # ── J, K, L, M, N. The results status filter ───────────────────────────

    def test_results_status_filter_and_distinct_union(self):
        exam_id = self._make_institute_exam('ga', 'Roster Exam')
        # s_in stays current. s_two gets a result then leaves.
        # s_inactive has an active enrollment but a dead student record.
        self._post_results('ua', exam_id, {
            f'marks_{self.ids["s_in"]}': '80',
            f'marks_{self.ids["s_two"]}': '60',
        })
        self._end_enrollment('ga', 's_two')
        self._store_result(exam_id, 's_inactive', 55)

        # J — default lists currently eligible students only.
        html = self._results_html('ua', exam_id)
        self.assertIn('Enrolled Student', html)
        self.assertNotIn('In Two Groups', html,
                         'an ended enrollment leaves the current view')
        self.assertNotIn('Inactive Student', html)
        self.assertNotIn('Ended Student', html,
                         'no stored result means nothing to show')

        # K + L — historical lists exactly the stored-result leavers.
        html = self._results_html('ua', exam_id, status='historical')
        self.assertIn('In Two Groups', html, 'ended enrollment + result')
        self.assertIn('Inactive Student', html, 'inactive student + result')
        self.assertIn('اشتراك منتهٍ', html)
        self.assertIn('طالب غير فعّال', html)
        self.assertNotIn('Enrolled Student', html,
                         'a currently eligible student is not historical')
        # M — an ended student with NO result never appears.
        self.assertNotIn('Ended Student', html)

        # N — all is the distinct union.
        html = self._results_html('ua', exam_id, status='all')
        for name in ('Enrolled Student', 'In Two Groups', 'Inactive Student'):
            self.assertIn(name, html)
        self.assertEqual(html.count('In Two Groups'), 1,
                         'the union must not duplicate a student')
        self.assertEqual(html.count('Enrolled Student'), 1)
        self.assertNotIn('Ended Student', html)

    # ── O & S. Read-only rendering vs. an editable current row ─────────────

    def test_historical_rows_are_read_only_and_current_rows_are_not(self):
        exam_id = self._make_institute_exam('ga', 'ReadOnly Exam')
        self._post_results('ua', exam_id, {
            f'marks_{self.ids["s_in"]}': '80',
            f'marks_{self.ids["s_two"]}': '60',
        })
        self._end_enrollment('ga', 's_two')

        html = self._results_html('ua', exam_id, status='all')
        # S — the current student still has an input and the save button.
        self.assertIn(f'name="marks_{self.ids["s_in"]}"', html,
                      'a currently eligible student stays editable')
        self.assertIn('حفظ الدرجات', html)
        # O — the historical student has no input element at all.
        self.assertNotIn(f'name="marks_{self.ids["s_two"]}"', html,
                         'a historical row must carry no input')

        # A historical-only view offers no save action.
        html = self._results_html('ua', exam_id, status='historical')
        self.assertNotIn('حفظ الدرجات', html,
                         'nothing editable means no save action')
        self.assertIn('للاطلاع فقط', html)

    # ── P. A forged POST for a historical row is rejected atomically ───────

    def test_forged_historical_row_post_is_rejected_atomically(self):
        exam_id = self._make_institute_exam('ga', 'Forge Exam')
        self._post_results('ua', exam_id, {
            f'marks_{self.ids["s_in"]}': '80',
            f'marks_{self.ids["s_two"]}': '60',
        })
        self._end_enrollment('ga', 's_two')

        self._post_results('ua', exam_id, {
            'status': 'all',
            f'marks_{self.ids["s_in"]}': '99',   # a legitimate change…
            f'marks_{self.ids["s_two"]}': '10',  # …smuggled with a dead row
        })
        with self.app.app_context():
            rows = {r.student_id: float(r.marks) for r in
                    ExamResult.query.execution_options(**OPTS)
                    .filter_by(exam_id=exam_id).all()}
            self.assertEqual(rows[self.ids['s_two']], 60.0,
                             'a historical mark must never be overwritten')
            self.assertEqual(rows[self.ids['s_in']], 80.0,
                             'the valid row must not commit either — atomic')

    # ── Q & R. An inactive group is fully read-only ────────────────────────

    def test_inactive_group_refuses_all_writes_and_defaults_to_history(self):
        exam_id = self._make_institute_exam('ga', 'Frozen Exam')
        self._post_results('ua', exam_id, {f'marks_{self.ids["s_in"]}': '70'})
        self._deactivate_group('ga')

        # R — the page opens on the historical view with a clear notice.
        html = self._results_html('ua', exam_id)
        self.assertIn('المجموعة الدراسية غير فعّالة', html)
        self.assertIn('Enrolled Student', html,
                      'the stored result is still shown')
        self.assertNotIn(f'name="marks_{self.ids["s_in"]}"', html,
                         'no input may be offered for a frozen group')
        self.assertNotIn('حفظ الدرجات', html)

        # Q — an update is refused…
        self._post_results('ua', exam_id, {f'marks_{self.ids["s_in"]}': '5'})
        # …and so is a brand-new result, even forcing status=current.
        self._post_results('ua', exam_id, {
            'status': 'current', f'marks_{self.ids["s_two"]}': '44'})
        with self.app.app_context():
            rows = {r.student_id: float(r.marks) for r in
                    ExamResult.query.execution_options(**OPTS)
                    .filter_by(exam_id=exam_id).all()}
            self.assertEqual(rows, {self.ids['s_in']: 70.0},
                             'an inactive group writes nothing at all')

    def test_inactive_group_exam_without_results_shows_empty_history(self):
        exam_id = self._make_institute_exam('ga', 'Empty Hist')
        self._deactivate_group('ga')
        html = self._index_html('ua', status='historical')
        self.assertIn('Empty Hist', html,
                      'an exam with no results is still listed historically')
        page = self._results_html('ua', exam_id)
        self.assertIn('لا توجد سجلات تاريخية لهذا الاختبار', page)

    # ── Creation scope stays active-only ───────────────────────────────────

    def test_history_filter_never_widens_creation(self):
        self._deactivate_group('ga')
        for status in ('historical', 'all', 'current'):
            self._post_create('ua', self._exam_form(
                exam_name=f'Forged {status}', status=status,
                institute_group_id=str(self.ids['ga'])))
        with self.app.app_context():
            names = [e.exam_name for e in self._exams(self.ids['inst'])]
            for status in ('historical', 'all', 'current'):
                self.assertNotIn(f'Forged {status}', names,
                                 'an inactive group can never be created for')

    # ── 21. Normal schools see none of this ────────────────────────────────

    def test_school_index_has_no_institute_history_filter(self):
        from app.blueprints.grades import index
        for status in (None, 'historical', 'all'):
            with self.app.test_request_context(
                    '/grades/' + (f'?status={status}' if status else '')):
                self._login('us')
                html = index()
                logout_user()
            self.assertIn('Legacy Exam', html,
                          'the school list is unaffected by the status param')
            self.assertNotIn('السجلات التاريخية', html,
                             'a school must not see the institute filter')
            self.assertNotIn('سجل تاريخي', html)

    def test_school_grade_entry_has_no_history_filter(self):
        html = self._get_results_page('us', self.ids['legacy_exam'])
        self.assertIn('School Child', html)
        self.assertIn('حفظ الدرجات', html, 'school saving is unchanged')
        self.assertNotIn('السجلات التاريخية', html)
        self.assertNotIn('المجموعة الدراسية غير فعّالة', html)


if __name__ == '__main__':
    unittest.main()

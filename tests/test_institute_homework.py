"""Institute-group-targeted homework — focused guarantees only.

Covers exactly the twelve acceptance points for this phase:

  1.  School homework creation and visibility still use the section behaviour.
  2.  An institute teacher can create homework for their own active group.
  3.  The subject is derived from the group and cannot be forged.
  4.  Another instructor's group is rejected with no row written.
  5.  Cross-school and cross-year groups are rejected.
  6.  Parents of actively enrolled students are notified.
  7.  Students outside the group are not.
  8.  A linked parent is reached only through an eligible student.
  9.  Institute manager access follows the existing permission.
  10. Edit and delete cannot escape group scope.
  11. A homework-triggered notification reaches only the target group.
  12. Historical school homework stays readable and unchanged.

The web application has no student- or parent-facing homework page (parent
homework lives only in the mobile API, which is out of scope for this phase),
so points 6-8 and 11 are asserted where institute homework actually becomes
visible to a student's family on the web: the in-app Notification rows the
homework create flow raises.

Out of scope here: exams, schedules, attendance, mobile API, Flutter.
"""
import unittest
from datetime import date, datetime, timedelta
from uuid import uuid4

from flask_login import login_user, logout_user
from werkzeug.exceptions import NotFound

from app import create_app
from app.models import (db, AcademicYear, AuditLog, Employee, Grade, Homework,
                        InstituteGroupEnrollment, InstituteStudyGroup,
                        Notification, Role, School, Section, Student, Subject,
                        User, parent_students, teacher_subjects)

OPTS = {'bypass_tenant_scope': True}


class InstituteHomeworkTest(unittest.TestCase):
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

    def _student(self, school_id, year_id, name, section_id=None):
        s = Student(student_id=f'S-{uuid4().hex[:10]}', full_name=name,
                    school_id=school_id, academic_year_id=year_id,
                    section_id=section_id, status='active')
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
            inst, iyear, isubj = self._institution('InsA', institute=True)
            # A second subject proves the derived subject is the GROUP's one.
            other_subj = Subject(school_id=inst.id, academic_year_id=iyear.id,
                                 name='مادة أخرى',
                                 code=f'X{self.suffix[:8]}')
            db.session.add(other_subj)
            # A past year, to prove a cross-year group is refused.
            old_year = AcademicYear(school_id=inst.id, name=f'Old {self.suffix}',
                                    start_date=date(2024, 8, 1),
                                    end_date=date(2025, 6, 30), is_current=False)
            db.session.add(old_year)
            db.session.flush()

            ua = self._user(inst.id, 'ihwa', self.teacher_role_id)
            ub = self._user(inst.id, 'ihwb', self.teacher_role_id)
            uadmin = self._user(inst.id, 'ihwadm', self.admin_role_id)
            ea = self._employee(inst.id, 'A', ua.id)
            eb = self._employee(inst.id, 'B', ub.id)

            ga = self._group(inst.id, iyear.id, isubj.id, 'A-Group', ea.id)
            gb = self._group(inst.id, iyear.id, isubj.id, 'B-Group', eb.id)
            g_off = self._group(inst.id, iyear.id, isubj.id, 'A-Off', ea.id,
                                active=False)
            g_old = self._group(inst.id, old_year.id, isubj.id, 'A-Old', ea.id)

            s_in = self._student(inst.id, iyear.id, 'Enrolled Student')
            s_out = self._student(inst.id, iyear.id, 'Outside Student')
            s_ended = self._student(inst.id, iyear.id, 'Ended Student')
            s_b = self._student(inst.id, iyear.id, 'B Group Student')
            self._enroll(inst.id, ga.id, s_in.id)
            self._enroll(inst.id, ga.id, s_ended.id, ended=True)
            self._enroll(inst.id, gb.id, s_b.id)

            # p_in  → only the enrolled child          (must be notified)
            # p_out → only a child outside the group   (must NOT be notified)
            # p_mix → one enrolled + one outside child (exactly one notification)
            p_in = self._user(inst.id, 'ihwpin', self.parent_role_id)
            p_out = self._user(inst.id, 'ihwpout', self.parent_role_id)
            p_mix = self._user(inst.id, 'ihwpmix', self.parent_role_id)
            p_b = self._user(inst.id, 'ihwpb', self.parent_role_id)
            self._link_parent(p_in.id, s_in.id)
            self._link_parent(p_out.id, s_out.id)
            self._link_parent(p_mix.id, s_in.id)
            self._link_parent(p_mix.id, s_out.id)
            self._link_parent(p_b.id, s_b.id)

            # ── Institute B (cross-tenant) ──────────────────────────────────
            oinst, oyear, osubj = self._institution('InsO', institute=True)
            uo = self._user(oinst.id, 'ihwo', self.teacher_role_id)
            eo = self._employee(oinst.id, 'O', uo.id)
            go = self._group(oinst.id, oyear.id, osubj.id, 'O-Group', eo.id)

            # ── Ordinary school (regression baseline) ───────────────────────
            sch, syear, ssubj = self._institution('PlnS', institute=False)
            grade = Grade(school_id=sch.id, academic_year_id=syear.id,
                          name=f'G{self.suffix[:4]}')
            db.session.add(grade)
            db.session.flush()
            sec = Section(school_id=sch.id, academic_year_id=syear.id,
                          grade_id=grade.id, name=f'A{self.suffix[:4]}', capacity=30)
            db.session.add(sec)
            db.session.flush()
            us = self._user(sch.id, 'ihwsch', self.teacher_role_id)
            es = self._employee(sch.id, 'S', us.id)
            db.session.execute(Section.__table__.update()
                               .where(Section.id == sec.id).values(teacher_id=es.id))
            # A realistic school teacher: homeroom of sec AND assigned to ssubj
            # there, which is what the existing subject guard requires.
            db.session.execute(teacher_subjects.insert().values(
                employee_id=es.id, subject_id=ssubj.id, section_id=sec.id))
            sch_student = self._student(sch.id, syear.id, 'School Child', sec.id)
            p_sch = self._user(sch.id, 'ihwpsch', self.parent_role_id)
            self._link_parent(p_sch.id, sch_student.id)

            # A pre-existing (historical) school assignment, written the way
            # every row in production already looks: a section, no group.
            legacy = Homework(school_id=sch.id, academic_year_id=syear.id,
                              teacher_id=es.id, subject_id=ssubj.id,
                              section_id=sec.id, title='Legacy HW',
                              publish_date=date.today() - timedelta(days=5),
                              due_date=date.today() + timedelta(days=5),
                              is_active=True)
            db.session.add(legacy)
            db.session.flush()

            db.session.commit()

            self.ids = {
                'inst': inst.id, 'iyear': iyear.id, 'isubj': isubj.id,
                'other_subj': other_subj.id, 'old_year': old_year.id,
                'ua': ua.id, 'ub': ub.id, 'uadmin': uadmin.id,
                'ea': ea.id, 'eb': eb.id,
                'ga': ga.id, 'gb': gb.id, 'g_off': g_off.id, 'g_old': g_old.id,
                's_in': s_in.id, 's_out': s_out.id, 's_ended': s_ended.id,
                's_b': s_b.id,
                'p_in': p_in.id, 'p_out': p_out.id, 'p_mix': p_mix.id,
                'p_b': p_b.id,
                'oinst': oinst.id, 'go': go.id, 'uo': uo.id,
                'sch': sch.id, 'syear': syear.id, 'ssubj': ssubj.id,
                'grade': grade.id, 'sec': sec.id, 'us': us.id, 'es': es.id,
                'sch_student': sch_student.id, 'p_sch': p_sch.id,
                'legacy': legacy.id,
            }

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for sid in (self.ids['inst'], self.ids['oinst'], self.ids['sch']):
                for model in (AuditLog, Notification, Homework,
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

    def _hw_rows(self, school_id):
        return (Homework.query.execution_options(**OPTS)
                .filter_by(school_id=school_id).all())

    def _post_create(self, user_key, data):
        """POST /homework/create as user_key; returns the response."""
        from app.blueprints.homework import create
        with self.app.test_request_context('/homework/create', method='POST',
                                           data=data):
            self._login(user_key)
            try:
                return create()
            finally:
                logout_user()

    def _notified_user_ids(self, school_id, title_fragment):
        rows = (Notification.query.execution_options(**OPTS)
                .filter_by(school_id=school_id, ntype='homework').all())
        return {r.target_user_id for r in rows if title_fragment in r.body}

    def _base_form(self, **over):
        data = {
            'title': 'Group HW',
            'publish_date': date.today().strftime('%Y-%m-%d'),
            'due_date': (date.today() + timedelta(days=3)).strftime('%Y-%m-%d'),
            'description': '',
        }
        data.update(over)
        return data

    # ── 1. School homework is untouched ──────────────────────────────────────

    def test_school_homework_still_uses_sections(self):
        from app.blueprints.homework import index
        data = self._base_form(title='School HW', section_id=str(self.ids['sec']),
                               grade_id=str(self.ids['grade']),
                               subject_id=str(self.ids['ssubj']))
        self._post_create('us', data)

        with self.app.app_context():
            rows = [h for h in self._hw_rows(self.ids['sch'])
                    if h.title == 'School HW']
            self.assertEqual(len(rows), 1, 'the school create path must still work')
            hw = rows[0]
            self.assertEqual(hw.section_id, self.ids['sec'],
                             'a school assignment still targets its section')
            self.assertIsNone(hw.institute_group_id,
                              'a school assignment carries no institute group')
            self.assertEqual(hw.subject_id, self.ids['ssubj'])

        # The teacher's own list still shows it, with the section controls.
        with self.app.test_request_context('/homework/'):
            self._login('us')
            html = index()
            logout_user()
        self.assertIn('School HW', html)
        self.assertIn('الشعبة', html,
                      'the school section filter label must remain')
        self.assertNotIn('المجموعة الدراسية', html,
                         'a school never sees the study-group control')

    # ── 2 & 3. Institute create; subject derived, not posted ─────────────────

    def test_instructor_creates_for_own_group_with_derived_subject(self):
        data = self._base_form(institute_group_id=str(self.ids['ga']),
                               # Forged values that must all be ignored:
                               subject_id=str(self.ids['other_subj']),
                               section_id=str(self.ids['sec']),
                               grade_id='999999')
        self._post_create('ua', data)

        with self.app.app_context():
            rows = [h for h in self._hw_rows(self.ids['inst'])
                    if h.title == 'Group HW']
            self.assertEqual(len(rows), 1, 'the institute create path must work')
            hw = rows[0]
            self.assertEqual(hw.institute_group_id, self.ids['ga'])
            self.assertIsNone(hw.section_id,
                              'an institute assignment never carries a section')
            self.assertEqual(hw.subject_id, self.ids['isubj'],
                             "the subject must come from the group, not the POST")
            self.assertNotEqual(hw.subject_id, self.ids['other_subj'])
            self.assertEqual(hw.school_id, self.ids['inst'])
            self.assertEqual(hw.academic_year_id, self.ids['iyear'])
            self.assertEqual(hw.teacher_id, self.ids['ea'])

    # ── 4 & 5. Foreign, inactive and cross-year groups are refused ───────────

    def test_ineligible_groups_are_rejected_and_write_nothing(self):
        for key in ('gb', 'g_off', 'g_old', 'go'):
            data = self._base_form(title=f'Bad {key}',
                                   institute_group_id=str(self.ids[key]))
            self._post_create('ua', data)
            with self.app.app_context():
                for sid in (self.ids['inst'], self.ids['oinst']):
                    titles = [h.title for h in self._hw_rows(sid)]
                    self.assertNotIn(f'Bad {key}', titles,
                                     f'{key} must not produce a homework row')

    def test_missing_group_is_rejected(self):
        self._post_create('ua', self._base_form(title='No Group'))
        with self.app.app_context():
            titles = [h.title for h in self._hw_rows(self.ids['inst'])]
            self.assertNotIn('No Group', titles)

    # ── 6, 7, 8 & 11. Notification reaches exactly the group's families ──────

    def test_notification_reaches_only_the_target_group(self):
        self._post_create('ua', self._base_form(
            title='Notify HW', institute_group_id=str(self.ids['ga'])))

        with self.app.app_context():
            notified = self._notified_user_ids(self.ids['inst'], 'Notify HW')

            self.assertIn(self.ids['p_in'], notified,
                          'the parent of an actively enrolled student is notified')
            self.assertIn(self.ids['p_mix'], notified,
                          'a parent with one eligible child is notified')
            self.assertNotIn(self.ids['p_out'], notified,
                             'a student outside the group must not be reached')
            self.assertNotIn(self.ids['p_b'], notified,
                             "another instructor's group must not be reached")

            # Exactly one row per parent — p_mix is not notified twice.
            rows = [r for r in Notification.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids['inst'], ntype='homework').all()
                    if 'Notify HW' in r.body]
            self.assertEqual(len(rows), 2,
                             'one notification per eligible parent, no duplicates')

            # Nothing crossed the tenant boundary.
            self.assertEqual(
                Notification.query.execution_options(**OPTS)
                .filter_by(school_id=self.ids['oinst']).count(), 0)
            self.assertEqual(
                Notification.query.execution_options(**OPTS)
                .filter_by(school_id=self.ids['sch']).count(), 0)

    def test_ended_enrollment_receives_nothing(self):
        with self.app.app_context():
            ended_student = db.session.get(Student, self.ids['s_ended'],
                                           execution_options=OPTS)
            p_ended = self._user(self.ids['inst'], 'ihwpend', self.parent_role_id)
            self._link_parent(p_ended.id, ended_student.id)
            db.session.commit()
            p_ended_id = p_ended.id

        self._post_create('ua', self._base_form(
            title='Ended HW', institute_group_id=str(self.ids['ga'])))

        with self.app.app_context():
            notified = self._notified_user_ids(self.ids['inst'], 'Ended HW')
            self.assertNotIn(p_ended_id, notified,
                             'an ended membership stops receiving assignments')
            self.assertIn(self.ids['p_in'], notified)

    # ── 9. Institute manager follows the existing permission ────────────────

    def test_manager_may_target_any_group_of_the_institute(self):
        self._post_create('uadmin', self._base_form(
            title='Admin HW', institute_group_id=str(self.ids['gb'])))
        with self.app.app_context():
            rows = [h for h in self._hw_rows(self.ids['inst'])
                    if h.title == 'Admin HW']
            self.assertEqual(len(rows), 1,
                             'a manager may target any group of their institute')
            self.assertEqual(rows[0].institute_group_id, self.ids['gb'])

        # …but still not another institute's group.
        self._post_create('uadmin', self._base_form(
            title='Admin Cross', institute_group_id=str(self.ids['go'])))
        with self.app.app_context():
            for sid in (self.ids['inst'], self.ids['oinst']):
                self.assertNotIn('Admin Cross',
                                 [h.title for h in self._hw_rows(sid)])

    # ── 10. Edit and delete cannot escape group scope ───────────────────────

    def _make_institute_hw(self, group_key='ga', title='Scoped HW'):
        with self.app.app_context():
            group = db.session.get(InstituteStudyGroup, self.ids[group_key],
                                   execution_options=OPTS)
            hw = Homework(school_id=self.ids['inst'],
                          academic_year_id=group.academic_year_id,
                          teacher_id=group.instructor_id,
                          subject_id=group.subject_id, section_id=None,
                          institute_group_id=group.id, title=title,
                          publish_date=date.today(),
                          due_date=date.today() + timedelta(days=2),
                          is_active=True)
            db.session.add(hw)
            db.session.commit()
            return hw.id

    def test_instructor_cannot_open_or_delete_another_groups_homework(self):
        from app.blueprints.homework import edit, delete
        hw_id = self._make_institute_hw('gb', 'B Scoped HW')

        with self.app.test_request_context(f'/homework/{hw_id}/edit'):
            self._login('ua')
            with self.assertRaises(Exception) as ctx:
                edit(hw_id)
            logout_user()
        self.assertIn(ctx.exception.__class__.__name__, ('NotFound', 'Forbidden'))

        with self.app.test_request_context(f'/homework/{hw_id}/delete',
                                           method='POST'):
            self._login('ua')
            with self.assertRaises(Exception):
                delete(hw_id)
            logout_user()

        with self.app.app_context():
            still = db.session.get(Homework, hw_id, execution_options=OPTS)
            self.assertIsNotNone(still, 'the row must survive a refused delete')

    def test_edit_cannot_retarget_to_an_ineligible_group(self):
        from app.blueprints.homework import edit
        hw_id = self._make_institute_hw('ga', 'Retarget HW')

        data = self._base_form(title='Retarget HW',
                               institute_group_id=str(self.ids['gb']),
                               subject_id=str(self.ids['other_subj']))
        with self.app.test_request_context(f'/homework/{hw_id}/edit',
                                           method='POST', data=data):
            self._login('ua')
            edit(hw_id)
            logout_user()

        with self.app.app_context():
            hw = db.session.get(Homework, hw_id, execution_options=OPTS)
            self.assertEqual(hw.institute_group_id, self.ids['ga'],
                             'a forged group id must not retarget the row')
            self.assertEqual(hw.subject_id, self.ids['isubj'])

    def test_edit_within_scope_keeps_ownership_and_derives_subject(self):
        from app.blueprints.homework import edit
        hw_id = self._make_institute_hw('ga', 'Editable HW')
        data = self._base_form(title='Edited Title',
                               institute_group_id=str(self.ids['ga']),
                               subject_id=str(self.ids['other_subj']),
                               section_id=str(self.ids['sec']))
        with self.app.test_request_context(f'/homework/{hw_id}/edit',
                                           method='POST', data=data):
            self._login('ua')
            edit(hw_id)
            logout_user()

        with self.app.app_context():
            hw = db.session.get(Homework, hw_id, execution_options=OPTS)
            self.assertEqual(hw.title, 'Edited Title')
            self.assertEqual(hw.institute_group_id, self.ids['ga'])
            self.assertIsNone(hw.section_id)
            self.assertEqual(hw.subject_id, self.ids['isubj'])
            self.assertEqual(hw.school_id, self.ids['inst'])
            self.assertEqual(hw.academic_year_id, self.ids['iyear'])

    def test_group_guard_fires_even_when_the_teacher_owns_the_row(self):
        """The pre-existing teacher_id check alone is not enough.

        A row whose teacher_id IS this instructor but whose group belongs to
        another instructor must still be refused, otherwise the institute scope
        could be escaped by an assignment that changed hands.
        """
        from app.blueprints.homework import edit, delete
        with self.app.app_context():
            gb = db.session.get(InstituteStudyGroup, self.ids['gb'],
                                execution_options=OPTS)
            hw = Homework(school_id=self.ids['inst'],
                          academic_year_id=gb.academic_year_id,
                          teacher_id=self.ids['ea'],      # owned by instructor A
                          subject_id=gb.subject_id, section_id=None,
                          institute_group_id=gb.id,       # …but B's group
                          title='Mismatched HW', publish_date=date.today(),
                          due_date=date.today() + timedelta(days=2),
                          is_active=True)
            db.session.add(hw)
            db.session.commit()
            hw_id = hw.id

        with self.app.test_request_context(f'/homework/{hw_id}/edit'):
            self._login('ua')
            with self.assertRaises(NotFound):
                edit(hw_id)
            logout_user()

        with self.app.test_request_context(f'/homework/{hw_id}/delete',
                                           method='POST'):
            self._login('ua')
            with self.assertRaises(NotFound):
                delete(hw_id)
            logout_user()

        with self.app.app_context():
            self.assertIsNotNone(
                db.session.get(Homework, hw_id, execution_options=OPTS),
                'nothing may be deleted outside the group scope')

    # ── Instructor listing is bounded by their own groups ───────────────────

    def test_instructor_list_shows_only_their_own_group_homework(self):
        from app.blueprints.homework import index
        self._make_institute_hw('ga', 'Mine HW')
        self._make_institute_hw('gb', 'Theirs HW')

        with self.app.test_request_context('/homework/'):
            self._login('ua')
            html = index()
            logout_user()
        self.assertIn('Mine HW', html)
        self.assertNotIn('Theirs HW', html,
                         "another instructor's assignment must not be listed")
        self.assertIn('المجموعة الدراسية', html,
                      'the institute list is filtered by study group')

    # ── 12. Historical school homework is readable and unchanged ────────────

    def test_legacy_school_homework_unchanged(self):
        from app.blueprints.homework import index
        with self.app.app_context():
            hw = db.session.get(Homework, self.ids['legacy'],
                                execution_options=OPTS)
            self.assertEqual(hw.section_id, self.ids['sec'])
            self.assertIsNone(hw.institute_group_id,
                              'no backfill may touch an existing row')

        with self.app.test_request_context('/homework/'):
            self._login('us')
            html = index()
            logout_user()
        self.assertIn('Legacy HW', html, 'historical homework stays readable')

    # ── School teachers are still section-scoped on create ──────────────────

    def test_school_teacher_still_blocked_outside_their_section(self):
        with self.app.app_context():
            sec2 = Section(school_id=self.ids['sch'],
                           academic_year_id=self.ids['syear'],
                           grade_id=self.ids['grade'],
                           name=f'B{self.suffix[:4]}', capacity=30)
            db.session.add(sec2)
            db.session.commit()
            sec2_id = sec2.id

        self._post_create('us', self._base_form(
            title='Foreign Section HW', section_id=str(sec2_id),
            grade_id=str(self.ids['grade'])))

        with self.app.app_context():
            titles = [h.title for h in self._hw_rows(self.ids['sch'])]
            self.assertNotIn('Foreign Section HW', titles,
                             'section scoping for school teachers is unchanged')


if __name__ == '__main__':
    unittest.main()

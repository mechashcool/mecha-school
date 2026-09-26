# -*- coding: utf-8 -*-
""""الصفوف الرئيسية / مشرف الصف" retired from the employee forms — focused checks.

  1. School and institute create wizards no longer render the block.
  2. "الصفوف والشعب التي يدرسها" still renders.
  3. The edit form no longer renders the block.
  4. Saving an employee through the edit form leaves an existing homeroom
     assignment (Section.teacher_id) untouched, while teaching sections are
     still saved exactly as before.

Fixture (a school and an institute) inherited from the institute attendance
tests; a grade, two sections and a subject are added to the school.
"""
import unittest

from flask_login import logout_user

from app.models import Grade, Section, Subject, db, teacher_subjects

from tests.test_institute_attendance import InstituteAttendanceTest, OPTS

_Fixture = InstituteAttendanceTest
del InstituteAttendanceTest

REMOVED = ('الصفوف الرئيسية', 'مشرف الصف', 'homeroom_section_ids',
           'wiz_homeroom', 'data-ta-picker="homeroom"', 'taHomeroomWrap')


class EmployeeHomeroomRetiredTest(_Fixture):

    def setUp(self):
        super().setUp()
        with self.app.app_context():
            ids = self.ids
            grade = Grade(name='الأول', school_id=ids['sch'],
                          academic_year_id=ids['syear'])
            db.session.add(grade)
            db.session.flush()
            sec1 = Section(name='أ', grade_id=grade.id, school_id=ids['sch'],
                           academic_year_id=ids['syear'])
            sec2 = Section(name='ب', grade_id=grade.id, school_id=ids['sch'],
                           academic_year_id=ids['syear'])
            subj = Subject(name='رياضيات', code=f'HR{self.suffix[:6]}',
                           school_id=ids['sch'], academic_year_id=ids['syear'],
                           grade_id=grade.id)
            db.session.add_all([sec1, sec2, subj])
            db.session.flush()
            emp = self._employee(ids['sch'], 'HR')
            sec1.teacher_id = emp.id          # historical homeroom assignment
            sadm = self._user(ids['sch'], 'hradm', self.admin_role_id)
            db.session.commit()
            ids.update(grade=grade.id, sec1=sec1.id, sec2=sec2.id,
                       subj=subj.id, emp=emp.id, sadm=sadm.id)

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            ids = self.ids
            db.session.execute(teacher_subjects.delete().where(
                teacher_subjects.c.employee_id == ids['emp']))
            for model in (Section, Subject):
                (model.query.execution_options(**OPTS)
                 .filter_by(school_id=ids['sch']).delete(synchronize_session=False))
            (Grade.query.execution_options(**OPTS)
             .filter_by(id=ids['grade']).delete(synchronize_session=False))
            db.session.commit()
        super().tearDown()

    def _call(self, view, user_key, path, method='GET', data=None, **kw):
        from app.blueprints import employees as bp
        with self.app.test_request_context(path, method=method, data=data):
            self._login(user_key)
            try:
                return getattr(bp, view)(**kw)
            finally:
                logout_user()

    def _assert_removed(self, html):
        for token in REMOVED:
            self.assertNotIn(token, html, token)

    # ── 1-2. Create wizards ─────────────────────────────────────────────────

    def test_create_wizards_have_no_homeroom_block(self):
        school_html = self._call('create', 'sadm', '/employees/create')
        self._assert_removed(school_html)
        self.assertIn('الصفوف والشعب التي يُدرّسها', school_html)
        self.assertIn('data-ta-picker="teaching"', school_html)

        institute_html = self._call('create', 'uadmin', '/employees/create')
        self._assert_removed(institute_html)

    # ── Create write path ignores forged homeroom input ─────────────────────

    def test_create_ignores_forged_homeroom(self):
        """_save_wizard_teacher_assignments is the create flow's only
        assignment write; forged wiz_homeroom[] (valid AND malformed) is ignored
        while teaching sections are saved as before."""
        from app.blueprints.employees import _save_wizard_teacher_assignments
        from app.models import AcademicYear, Employee, School
        ids = self.ids
        pair = f'{ids["grade"]}:{ids["sec2"]}'
        with self.app.test_request_context('/employees/create', method='POST', data={
                'wiz_homeroom[]': [pair, 'junk'],
                'wiz_teaching[]': [pair], 'subject_ids': [ids['subj']]}):
            self._login('sadm')
            try:
                new_emp = self._employee(ids['sch'], 'NEW')
                result = _save_wizard_teacher_assignments(
                    new_emp,
                    db.session.get(School, ids['sch'], execution_options=OPTS),
                    db.session.get(AcademicYear, ids['syear'], execution_options=OPTS))
                db.session.commit()
                new_id = new_emp.id
            finally:
                logout_user()

        self.assertEqual(result, ([ids['sec2']], [ids['subj']]))
        with self.app.app_context():
            sec2 = db.session.get(Section, ids['sec2'], execution_options=OPTS)
            self.assertIsNone(sec2.teacher_id, 'forged homeroom must be ignored')
            rows = db.session.execute(teacher_subjects.select().where(
                teacher_subjects.c.employee_id == new_id)).fetchall()
            self.assertEqual({(r.subject_id, r.section_id) for r in rows},
                             {(ids['subj'], ids['sec2'])})
            db.session.execute(teacher_subjects.delete().where(
                teacher_subjects.c.employee_id == new_id))
            db.session.commit()

    # ── 3-4. Edit form + save keeps the old homeroom ────────────────────────

    def test_edit_hides_block_and_save_keeps_homeroom(self):
        ids = self.ids
        html = self._call('edit', 'sadm', f'/employees/{ids["emp"]}/edit',
                          emp_id=ids['emp'])
        self._assert_removed(html)
        self.assertIn('name="teaching_section_ids"', html)
        self.assertIn('الشعب التي يُدرّسها', html)

        # Forged retired field: would move the homeroom from sec1 to sec2.
        self._call('edit', 'sadm', f'/employees/{ids["emp"]}/edit', method='POST',
                   emp_id=ids['emp'],
                   data={'full_name': 'HR Teacher', 'save_teacher_section': '1',
                         'homeroom_section_ids': [ids['sec2']],
                         'teaching_section_ids': [ids['sec2']],
                         'subject_ids': [ids['subj']]})

        with self.app.app_context():
            sec1 = db.session.get(Section, ids['sec1'], execution_options=OPTS)
            sec2 = db.session.get(Section, ids['sec2'], execution_options=OPTS)
            self.assertEqual(sec1.teacher_id, ids['emp'],
                             'historical homeroom must survive the save')
            self.assertIsNone(sec2.teacher_id, 'forged homeroom must be ignored')
            rows = db.session.execute(teacher_subjects.select().where(
                teacher_subjects.c.employee_id == ids['emp'])).fetchall()
            self.assertEqual({(r.subject_id, r.section_id) for r in rows},
                             {(ids['subj'], ids['sec2'])},
                             'teaching sections still saved as before')


for _inherited in list(vars(_Fixture)):
    if _inherited.startswith('test_'):
        setattr(EmployeeHomeroomRetiredTest, _inherited, None)

del _Fixture


if __name__ == '__main__':
    unittest.main()

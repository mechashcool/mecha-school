# -*- coding: utf-8 -*-
"""Phase 1 teacher-assignment hardening — focused regression tests.

  1. An unrelated Edit Employee save (same flat selections) leaves the exact
     teacher_subjects pairs untouched — no DELETE/INSERT at all.
  2. A real current-year change reconciles only active-year rows; the
     previous academic year's rows survive.
  3. A foreign-school (or previous-year) section id rejects the save; no row
     changes.
  4. A foreign-school subject id rejects the save; no row changes.
  5. admin.edit_user changing only the password keeps teacher_subjects.
  6. admin.edit_user changing homeroom updates Section.teacher_id and keeps
     teacher_subjects.
  7. No active year: a non-empty selection is rejected, an empty one is a
     no-op.

Fixture (a school, an institute and a foreign institute) inherited from the
institute attendance tests, as test_employee_homeroom_retired does.
"""
import unittest
from datetime import date

from flask import get_flashed_messages
from flask_login import logout_user
from sqlalchemy import event

from app.models import (AcademicYear, Grade, Section, Subject, User, db,
                        teacher_subjects)

from tests.test_institute_attendance import InstituteAttendanceTest, OPTS

_Fixture = InstituteAttendanceTest
del InstituteAttendanceTest


class TeacherAssignmentHardeningTest(_Fixture):

    def setUp(self):
        super().setUp()
        with self.app.app_context():
            ids = self.ids
            sfx = self.suffix[:6]

            # Current (active) year of the ordinary school.
            grade = Grade(name='الأول', school_id=ids['sch'],
                          academic_year_id=ids['syear'])
            db.session.add(grade)
            db.session.flush()
            sec_a = Section(name='أ', grade_id=grade.id, school_id=ids['sch'],
                            academic_year_id=ids['syear'])
            sec_b = Section(name='ب', grade_id=grade.id, school_id=ids['sch'],
                            academic_year_id=ids['syear'])
            math = Subject(name='رياضيات', code=f'TM{sfx}', school_id=ids['sch'],
                           academic_year_id=ids['syear'], grade_id=grade.id)
            sci = Subject(name='علوم', code=f'TS{sfx}', school_id=ids['sch'],
                          academic_year_id=ids['syear'], grade_id=grade.id)

            # Previous academic year of the SAME school.
            prev = AcademicYear(school_id=ids['sch'], name=f'Prev {self.suffix}',
                                start_date=date(2024, 8, 1),
                                end_date=date(2025, 6, 30), is_current=False)
            db.session.add_all([sec_a, sec_b, math, sci, prev])
            db.session.flush()
            pgrade = Grade(name='الأول', school_id=ids['sch'],
                           academic_year_id=prev.id)
            db.session.add(pgrade)
            db.session.flush()
            psec = Section(name='أ', grade_id=pgrade.id, school_id=ids['sch'],
                           academic_year_id=prev.id)
            psubj = Subject(name='رياضيات', code=f'TP{sfx}', school_id=ids['sch'],
                            academic_year_id=prev.id, grade_id=pgrade.id)

            # Another school (the foreign institute).
            fgrade = Grade(name='الأول', school_id=ids['oinst'],
                           academic_year_id=self._oyear_id())
            db.session.add_all([psec, psubj, fgrade])
            db.session.flush()
            fsec = Section(name='أ', grade_id=fgrade.id, school_id=ids['oinst'],
                           academic_year_id=fgrade.academic_year_id)
            fsubj = Subject(name='رياضيات', code=f'TF{sfx}', school_id=ids['oinst'],
                            academic_year_id=fgrade.academic_year_id,
                            grade_id=fgrade.id)
            db.session.add_all([fsec, fsubj])
            db.session.flush()

            tuser = self._user(ids['sch'], 'tah', self.teacher_role_id)
            emp = self._employee(ids['sch'], 'TAH', tuser.id)
            sadm = self._user(ids['sch'], 'tahadm', self.admin_role_id)

            # Exact (NOT cross-product) pairs, plus one previous-year row.
            db.session.execute(teacher_subjects.insert(), [
                {'employee_id': emp.id, 'section_id': sec_a.id, 'subject_id': math.id},
                {'employee_id': emp.id, 'section_id': sec_b.id, 'subject_id': sci.id},
                {'employee_id': emp.id, 'section_id': psec.id, 'subject_id': psubj.id},
            ])
            db.session.commit()
            ids.update(grade=grade.id, sec_a=sec_a.id, sec_b=sec_b.id,
                       math=math.id, sci=sci.id, prev=prev.id, pgrade=pgrade.id,
                       psec=psec.id, psubj=psubj.id, fgrade=fgrade.id,
                       fsec=fsec.id, fsubj=fsubj.id, tuser=tuser.id,
                       emp=emp.id, sadm=sadm.id)

    def _oyear_id(self):
        return (AcademicYear.query.execution_options(**OPTS)
                .filter_by(school_id=self.ids['oinst']).first().id)

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            ids = self.ids
            db.session.execute(teacher_subjects.delete().where(
                teacher_subjects.c.employee_id == ids['emp']))
            # The ordinary school's sections/subjects are all ours; in the
            # foreign institute only the two rows this file added (its own
            # subject is still referenced by the base fixture's study group).
            for model in (Section, Subject):
                (model.query.execution_options(**OPTS)
                 .filter_by(school_id=ids['sch']).delete(synchronize_session=False))
            (Section.query.execution_options(**OPTS)
             .filter_by(id=ids['fsec']).delete(synchronize_session=False))
            (Subject.query.execution_options(**OPTS)
             .filter_by(id=ids['fsubj']).delete(synchronize_session=False))
            (Grade.query.execution_options(**OPTS)
             .filter(Grade.id.in_([ids['grade'], ids['pgrade'], ids['fgrade']]))
             .delete(synchronize_session=False))
            # Test 7 may leave the year inactive if it fails half-way.
            (AcademicYear.query.execution_options(**OPTS)
             .filter_by(id=ids['syear']).update({'is_current': True}))
            db.session.commit()
        super().tearDown()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _rows(self):
        with self.app.app_context():
            return {(r.section_id, r.subject_id) for r in db.session.execute(
                teacher_subjects.select().where(
                    teacher_subjects.c.employee_id == self.ids['emp'])).fetchall()}

    def _baseline(self):
        ids = self.ids
        return {(ids['sec_a'], ids['math']), (ids['sec_b'], ids['sci']),
                (ids['psec'], ids['psubj'])}

    def _post(self, module, view, path, data, **kw):
        """Run one decorated view in a POST request as the school admin.
        Returns (response, flashes, teacher_subjects write statements)."""
        import importlib
        bp = importlib.import_module(f'app.blueprints.{module}')
        writes = []

        def _spy(conn, cursor, statement, params, context, executemany):
            head = statement.lstrip().split(None, 1)[0].upper()
            if head in ('DELETE', 'INSERT') and 'teacher_subjects' in statement:
                writes.append(head)

        with self.app.test_request_context(path, method='POST', data=data):
            engine = db.engine
            event.listen(engine, 'before_cursor_execute', _spy)
            try:
                self._login('sadm')
                resp = getattr(bp, view)(**kw)
                flashes = get_flashed_messages(with_categories=True)
            finally:
                event.remove(engine, 'before_cursor_execute', _spy)
                logout_user()
        return resp, flashes, writes

    def _edit_employee(self, sections, subjects):
        ids = self.ids
        return self._post('employees', 'edit', f'/employees/{ids["emp"]}/edit', {
            'full_name': f'Changed Name {self.suffix}',
            'phone': '07700000000',
            'save_teacher_section': '1',
            'teaching_section_ids': [str(s) for s in sections],
            'subject_ids': [str(s) for s in subjects],
        }, emp_id=ids['emp'])

    def _edit_user(self, extra):
        ids = self.ids
        with self.app.app_context():
            u = db.session.get(User, ids['tuser'], execution_options=OPTS)
            data = {'username': u.username, 'email': u.email or '',
                    'full_name': u.full_name, 'role_id': str(self.teacher_role_id),
                    'is_active': '1'}
        data.update(extra)
        return self._post('admin', 'edit_user', f'/admin/users/{ids["tuser"]}/edit',
                          data, user_id=ids['tuser'])

    # ── 1. Unrelated employee edit ──────────────────────────────────────────

    def test_1_unrelated_edit_preserves_exact_pairs(self):
        ids = self.ids
        resp, flashes, writes = self._edit_employee(
            [ids['sec_a'], ids['sec_b']], [ids['math'], ids['sci']])

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(writes, [], 'no teacher_subjects DELETE/INSERT on an unchanged selection')
        self.assertEqual(self._rows(), self._baseline(),
                         'exact pairs must not be expanded to a cross product')
        with self.app.app_context():
            from app.models import Employee
            emp = db.session.get(Employee, ids['emp'], execution_options=OPTS)
            self.assertEqual(emp.full_name, f'Changed Name {self.suffix}',
                             'the unrelated field itself is still saved')
        self.assertFalse([m for c, m in flashes if c == 'danger'])

    # ── 2. Previous academic year preserved ────────────────────────────────

    def test_2_current_year_change_keeps_previous_year_rows(self):
        ids = self.ids
        resp, flashes, writes = self._edit_employee([ids['sec_a']], [ids['sci']])

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(sorted(writes), ['DELETE', 'INSERT'],
                         'one scoped DELETE and one bulk INSERT')
        self.assertEqual(self._rows(), {(ids['sec_a'], ids['sci']),
                                        (ids['psec'], ids['psubj'])})
        self.assertIn(('success', 'تم ربط الموظف بالمواد والصفوف والشعب.'), flashes)

    # ── 3. Foreign / previous-year section rejected ────────────────────────

    def test_3_foreign_or_stale_section_rejected(self):
        from app.blueprints.employees import _TA_ERR_SECTION
        ids = self.ids
        for label, bad in (('other school', ids['fsec']),
                           ('previous year', ids['psec'])):
            with self.subTest(label):
                resp, flashes, writes = self._edit_employee(
                    [ids['sec_a'], bad], [ids['math']])
                self.assertEqual(resp.status_code, 302)
                self.assertEqual(writes, [])
                self.assertEqual(self._rows(), self._baseline())
                self.assertIn(('danger', _TA_ERR_SECTION), flashes)

    # ── 4. Foreign subject rejected ────────────────────────────────────────

    def test_4_foreign_subject_rejected(self):
        from app.blueprints.employees import _TA_ERR_SUBJECT
        ids = self.ids
        resp, flashes, writes = self._edit_employee(
            [ids['sec_a']], [ids['math'], ids['fsubj']])
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(writes, [])
        self.assertEqual(self._rows(), self._baseline())
        self.assertIn(('danger', _TA_ERR_SUBJECT), flashes)

    # ── 5. Admin password-only edit ────────────────────────────────────────

    def test_5_admin_password_edit_keeps_assignments(self):
        # No homeroom sections posted: the old code deleted every row here.
        resp, flashes, writes = self._edit_user(
            {'new_password': 'NewPassword123',
             'teacher_subject_ids': [str(self.ids['math'])]})
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/admin/users', resp.location)
        self.assertNotIn('/edit', resp.location, flashes)
        self.assertEqual(writes, [])
        self.assertEqual(self._rows(), self._baseline())
        with self.app.app_context():
            u = db.session.get(User, self.ids['tuser'], execution_options=OPTS)
            self.assertTrue(u.check_password('NewPassword123'))

    # ── 6. Admin homeroom edit ─────────────────────────────────────────────

    def test_6_admin_homeroom_edit_keeps_assignments(self):
        ids = self.ids
        resp, flashes, writes = self._edit_user(
            {'teacher_section_ids': [str(ids['sec_b'])],
             'teacher_subject_ids': [str(ids['math'])]})
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('/edit', resp.location, flashes)
        self.assertEqual(writes, [])
        self.assertEqual(self._rows(), self._baseline())
        with self.app.app_context():
            sec_b = db.session.get(Section, ids['sec_b'], execution_options=OPTS)
            self.assertEqual(sec_b.teacher_id, ids['emp'], 'homeroom still updates')

    # ── 7. No active year ──────────────────────────────────────────────────

    def test_7_no_active_year(self):
        from app.blueprints.employees import _TA_ERR_NO_YEAR, _save_teacher_assignments
        from app.models import Employee
        ids = self.ids
        with self.app.app_context():
            (AcademicYear.query.execution_options(**OPTS)
             .filter_by(id=ids['syear']).update({'is_current': False}))
            db.session.commit()

        for posted, expect_error in (
                ({'teaching_section_ids': [str(ids['sec_a'])],
                  'subject_ids': [str(ids['math'])]}, True),
                ({}, False)):
            with self.subTest(posted=bool(posted)):
                with self.app.test_request_context('/x', method='POST', data=posted):
                    emp = db.session.get(Employee, ids['emp'], execution_options=OPTS)
                    if expect_error:
                        with self.assertRaises(ValueError) as cm:
                            _save_teacher_assignments(emp)
                        self.assertEqual(str(cm.exception), _TA_ERR_NO_YEAR)
                    else:
                        self.assertFalse(_save_teacher_assignments(emp))
                    db.session.rollback()
                self.assertEqual(self._rows(), self._baseline())


for _inherited in list(vars(_Fixture)):
    if _inherited.startswith('test_'):
        setattr(TeacherAssignmentHardeningTest, _inherited, None)

del _Fixture


if __name__ == '__main__':
    unittest.main()

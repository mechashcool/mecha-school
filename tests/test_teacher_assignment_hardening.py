# -*- coding: utf-8 -*-
"""Exact teacher section → subject assignments (Phase 2) — focused tests.

teacher_subjects must hold exactly the (section, subject) pairs the operator
ticked: never a product of a section list and a subject list.

Employee Management (the only writer):
  1. different subjects per section — no cross pairs
  2. several grades save exactly; a grade-mismatched pair is rejected
  3. exact edit round-trip: stored pairs pre-checked, unchanged save = 0 writes
  4. a current-year change leaves previous-year rows untouched
  5. foreign section / foreign subject / malformed token rejected before writes

School User Management:
  6. admin create sets homeroom only and writes no teacher_subjects
  7. admin edit (password + homeroom) leaves the exact pairs untouched

Fixture (a school, an institute and a foreign institute) inherited from the
institute attendance tests, as test_employee_homeroom_retired does.
"""
import re
import unittest
from datetime import date

from flask import get_flashed_messages
from flask_login import logout_user
from sqlalchemy import event

from app.models import (AcademicYear, Employee, Grade, Section, Subject, User,
                        db, teacher_subjects)

from tests.test_institute_attendance import InstituteAttendanceTest, OPTS

_Fixture = InstituteAttendanceTest
del InstituteAttendanceTest


class TeacherAssignmentPairsTest(_Fixture):

    def setUp(self):
        super().setUp()
        with self.app.app_context():
            ids = self.ids
            sfx = self.suffix[:6]
            sch, syear = ids['sch'], ids['syear']

            def subject(name, code, year_id, grade_id, school_id=sch):
                return Subject(name=name, code=f'{code}{sfx}', school_id=school_id,
                               academic_year_id=year_id, grade_id=grade_id)

            # Active year: grade 1 (sections A, B, C) and grade 2 (section A).
            g1 = Grade(name='الأول', school_id=sch, academic_year_id=syear)
            g2 = Grade(name='الثاني', school_id=sch, academic_year_id=syear)
            db.session.add_all([g1, g2])
            db.session.flush()
            sec_a = Section(name='أ', grade_id=g1.id, school_id=sch, academic_year_id=syear)
            sec_b = Section(name='ب', grade_id=g1.id, school_id=sch, academic_year_id=syear)
            sec_c = Section(name='ج', grade_id=g1.id, school_id=sch, academic_year_id=syear)
            sec_a2 = Section(name='أ', grade_id=g2.id, school_id=sch, academic_year_id=syear)
            math = subject('رياضيات', 'PM', syear, g1.id)
            sci = subject('علوم', 'PS', syear, g1.id)
            eng = subject('انكليزي', 'PE', syear, g1.id)
            math2 = subject('رياضيات ٢', 'P2', syear, g2.id)
            gen = subject('تربية فنية', 'PG', syear, None)       # no grade

            # Previous academic year of the same school.
            prev = AcademicYear(school_id=sch, name=f'Prev {self.suffix}',
                                start_date=date(2024, 8, 1),
                                end_date=date(2025, 6, 30), is_current=False)
            db.session.add_all([sec_a, sec_b, sec_c, sec_a2,
                                math, sci, eng, math2, gen, prev])
            db.session.flush()
            pgrade = Grade(name='الأول', school_id=sch, academic_year_id=prev.id)
            db.session.add(pgrade)
            db.session.flush()
            psec = Section(name='أ', grade_id=pgrade.id, school_id=sch,
                           academic_year_id=prev.id)
            psubj = subject('رياضيات', 'PP', prev.id, pgrade.id)

            # Another school (the foreign institute).
            oyear = (AcademicYear.query.execution_options(**OPTS)
                     .filter_by(school_id=ids['oinst']).first().id)
            fgrade = Grade(name='الأول', school_id=ids['oinst'], academic_year_id=oyear)
            db.session.add_all([psec, psubj, fgrade])
            db.session.flush()
            fsec = Section(name='أ', grade_id=fgrade.id, school_id=ids['oinst'],
                           academic_year_id=oyear)
            fsubj = subject('رياضيات', 'PF', oyear, fgrade.id, school_id=ids['oinst'])
            db.session.add_all([fsec, fsubj])
            db.session.flush()

            tuser = self._user(sch, 'tap', self.teacher_role_id)
            emp = self._employee(sch, 'TAP', tuser.id)
            sadm = self._user(sch, 'tapadm', self.admin_role_id)
            # One previous-year row, present in every test.
            db.session.execute(teacher_subjects.insert().values(
                employee_id=emp.id, section_id=psec.id, subject_id=psubj.id))
            db.session.commit()
            ids.update(g1=g1.id, g2=g2.id, A=sec_a.id, B=sec_b.id, C=sec_c.id,
                       A2=sec_a2.id, math=math.id, sci=sci.id, eng=eng.id,
                       math2=math2.id, gen=gen.id, prev=prev.id, pgrade=pgrade.id,
                       psec=psec.id, psubj=psubj.id, fgrade=fgrade.id,
                       fsec=fsec.id, fsubj=fsubj.id, tuser=tuser.id,
                       emp=emp.id, sadm=sadm.id)

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            ids = self.ids
            emp_ids = [e.id for e in Employee.query.execution_options(**OPTS)
                       .filter_by(school_id=ids['sch']).all()]
            if emp_ids:
                db.session.execute(teacher_subjects.delete().where(
                    teacher_subjects.c.employee_id.in_(emp_ids)))
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
             .filter(Grade.id.in_([ids['g1'], ids['g2'], ids['pgrade'], ids['fgrade']]))
             .delete(synchronize_session=False))
            (AcademicYear.query.execution_options(**OPTS)
             .filter_by(id=ids['syear']).update({'is_current': True}))
            db.session.commit()
        super().tearDown()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _p(self, *names):
        """('A', 'math') → {(sec_id, subj_id)} using the fixture ids."""
        return {(self.ids[s], self.ids[j]) for s, j in names}

    def _keys(self, pairs):
        return [f'{s}:{j}' for s, j in sorted(pairs)]

    def _rows(self, emp_key='emp'):
        with self.app.app_context():
            return {(r.section_id, r.subject_id) for r in db.session.execute(
                teacher_subjects.select().where(
                    teacher_subjects.c.employee_id == self.ids[emp_key])).fetchall()}

    def _prev_row(self):
        return {(self.ids['psec'], self.ids['psubj'])}

    def _seed(self, pairs):
        """Replace the active-year rows with exactly ``pairs`` (prev row kept)."""
        with self.app.app_context():
            db.session.execute(teacher_subjects.delete().where(
                teacher_subjects.c.employee_id == self.ids['emp'],
                teacher_subjects.c.section_id != self.ids['psec']))
            if pairs:
                db.session.execute(teacher_subjects.insert(), [
                    {'employee_id': self.ids['emp'], 'section_id': s, 'subject_id': j}
                    for s, j in pairs])
            db.session.commit()

    def _run(self, module, view, path, method='POST', data=None, **kw):
        """Run one decorated view as the school admin.
        Returns (response, flashes, teacher_subjects DELETE/INSERT statements)."""
        import importlib
        bp = importlib.import_module(f'app.blueprints.{module}')
        writes = []

        def _spy(conn, cursor, statement, params, context, executemany):
            head = statement.lstrip().split(None, 1)[0].upper()
            if head in ('DELETE', 'INSERT') and 'teacher_subjects' in statement:
                writes.append(head)

        with self.app.test_request_context(path, method=method, data=data):
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

    def _save(self, pairs=None, tokens=None):
        """POST the Edit Employee form with exact pair checkboxes."""
        ids = self.ids
        values = tokens if tokens is not None else self._keys(pairs or set())
        return self._run('employees', 'edit', f'/employees/{ids["emp"]}/edit', data={
            'full_name': f'Teacher {self.suffix}', 'phone': '07700000000',
            'save_teacher_section': '1', 'ta_pair[]': values,
        }, emp_id=ids['emp'])

    def _edit_html(self):
        ids = self.ids
        html, _f, _w = self._run('employees', 'edit', f'/employees/{ids["emp"]}/edit',
                                 method='GET', emp_id=ids['emp'])
        return html

    @staticmethod
    def _is_checked(html, key):
        m = re.search(r'value="%s"[^>]*>' % re.escape(key), html)
        assert m, f'no checkbox for {key}'
        return bool(re.search(r'\bchecked\b', m.group(0)))

    def _assert_saved(self, expected):
        resp, flashes, writes = self._save(expected)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse([m for c, m in flashes if c == 'danger'], flashes)
        self.assertEqual(self._rows(), expected | self._prev_row())
        return writes

    def _assert_rejected(self, message, *, pairs=None, tokens=None):
        before = self._rows()
        resp, flashes, writes = self._save(pairs, tokens)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(writes, [], 'nothing may be written on a rejected save')
        self.assertEqual(self._rows(), before)
        self.assertIn(('danger', message), flashes)

    # ── 1. Different subjects per section — no cross pairs ─────────────────

    def test_01_different_subjects_per_section_no_cross_pairs(self):
        self._assert_saved(self._p(('A', 'math'), ('B', 'sci')))
        rows = self._rows()
        self.assertNotIn((self.ids['A'], self.ids['sci']), rows)
        self.assertNotIn((self.ids['B'], self.ids['math']), rows)

    # ── 2. Several grades; a grade-mismatched pair is rejected ──────────────

    def test_02_multi_grade_and_grade_mismatch(self):
        from app.blueprints.employees import _TA_ERR_SUBJ_GRADE
        saved = self._p(('A', 'math'), ('A2', 'math2'))
        self._assert_saved(saved)
        # Valid pair alongside a mismatched one: nothing is saved.
        self._assert_rejected(_TA_ERR_SUBJ_GRADE,
                              pairs=self._p(('B', 'sci'), ('A', 'math2')))
        self.assertEqual(self._rows(), saved | self._prev_row())

    # ── 3. Exact edit round-trip ────────────────────────────────────────────

    def test_03_exact_round_trip_zero_writes(self):
        ids = self.ids
        stored = self._p(('A', 'math'), ('B', 'sci'))
        self._seed(stored)
        html = self._edit_html()
        self.assertTrue(self._is_checked(html, f'{ids["A"]}:{ids["math"]}'))
        self.assertTrue(self._is_checked(html, f'{ids["B"]}:{ids["sci"]}'))
        self.assertFalse(self._is_checked(html, f'{ids["A"]}:{ids["sci"]}'))
        self.assertFalse(self._is_checked(html, f'{ids["B"]}:{ids["math"]}'))
        writes = self._assert_saved(stored)
        self.assertEqual(writes, [], 'unchanged exact pairs → zero DELETE/INSERT')

    # ── 4. Previous year preserved on a current-year change ─────────────────

    def test_04_previous_year_preserved(self):
        self._seed(self._p(('A', 'math'), ('B', 'sci')))
        writes = self._assert_saved(self._p(('A', 'math'), ('B', 'eng')))
        self.assertEqual(sorted(writes), ['DELETE', 'INSERT'])
        self.assertTrue(self._prev_row() <= self._rows())

    # ── 5. Invalid ids rejected before any write ────────────────────────────

    def test_05_invalid_ids_rejected(self):
        from app.blueprints.employees import (_TA_ERR_READ, _TA_ERR_SECTION,
                                              _TA_ERR_SUBJECT)
        ids = self.ids
        self._seed(self._p(('A', 'math')))
        valid = f'{ids["A"]}:{ids["math"]}'
        for label, token, message in (
                ('foreign section', f'{ids["fsec"]}:{ids["math"]}', _TA_ERR_SECTION),
                ('foreign subject', f'{ids["A"]}:{ids["fsubj"]}', _TA_ERR_SUBJECT),
                ('malformed', f'{ids["A"]}:x', _TA_ERR_READ)):
            with self.subTest(label):
                self._assert_rejected(message, tokens=[valid, token])

    # ── 6-7. School User Management never writes teacher_subjects ──────────

    def test_06_admin_create_sets_homeroom_only(self):
        ids = self.ids
        username = f'tapnew_{self.suffix}'
        resp, flashes, writes = self._run('admin', 'create_user', '/admin/users/create', data={
            'username': username, 'email': f'{username}@example.test',
            'full_name': f'New Teacher {self.suffix}', 'password': 'Password123',
            'role_id': str(self.teacher_role_id),
            'teacher_section_ids': [str(ids['A'])],
            'teacher_subject_ids': [str(ids['math'])],     # legacy field: ignored
        })
        self.assertEqual(resp.status_code, 302, flashes)
        self.assertEqual(writes, [])
        with self.app.app_context():
            user = (User.query.execution_options(**OPTS)
                    .filter_by(username=username).one())
            emp = (Employee.query.execution_options(**OPTS)
                   .filter_by(user_id=user.id).one())
            rows = db.session.execute(teacher_subjects.select().where(
                teacher_subjects.c.employee_id == emp.id)).fetchall()
            self.assertEqual(rows, [], 'no teaching assignment from User Management')
            sec_a = db.session.get(Section, ids['A'], execution_options=OPTS)
            self.assertEqual(sec_a.teacher_id, emp.id, 'homeroom still assigned')

    def test_07_admin_edit_preserves_pairs(self):
        ids = self.ids
        stored = self._p(('A', 'math'), ('B', 'sci'))
        self._seed(stored)
        with self.app.app_context():
            u = db.session.get(User, ids['tuser'], execution_options=OPTS)
            data = {'username': u.username, 'email': u.email or '',
                    'full_name': u.full_name, 'role_id': str(self.teacher_role_id),
                    'is_active': '1', 'new_password': 'NewPassword123',
                    'teacher_section_ids': [str(ids['C'])],
                    'teacher_subject_ids': [str(ids['eng'])]}
        resp, flashes, writes = self._run('admin', 'edit_user',
                                          f'/admin/users/{ids["tuser"]}/edit',
                                          data=data, user_id=ids['tuser'])
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('/edit', resp.location, flashes)
        self.assertEqual(writes, [])
        self.assertEqual(self._rows(), stored | self._prev_row())
        with self.app.app_context():
            sec_c = db.session.get(Section, ids['C'], execution_options=OPTS)
            self.assertEqual(sec_c.teacher_id, ids['emp'], 'homeroom still updates')


for _inherited in list(vars(_Fixture)):
    if _inherited.startswith('test_'):
        setattr(TeacherAssignmentPairsTest, _inherited, None)

del _Fixture


if __name__ == '__main__':
    unittest.main()

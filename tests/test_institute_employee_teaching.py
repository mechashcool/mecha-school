# -*- coding: utf-8 -*-
"""Institute employee teaching settings — focused checks only.

  * Institute create/edit pages show study groups + derived subjects, never the
    school grades/sections UI or the retired homeroom fields.
  * Groups persist through InstituteStudyGroup.instructor_id only: add (moving
    a group from another instructor), remove an INACTIVE group, refuse removal
    from an ACTIVE group, reject a foreign institute's group.
  * Quick-create: the pre-check writes nothing and is permission-protected;
    the group is created at save through the canonical validator, and nothing
    survives if the employee save is rolled back.
  * A school page keeps its grades/sections UI and no institute section.

Fixture inherited from the institute attendance tests.
"""
import json
import re
import unittest
from datetime import time

from flask_login import logout_user

from app.models import (Grade, InstituteStudyGroup, Subject, db,
                        teacher_subjects)
from app.utils.school_stages import STAGE_INTERMEDIATE, STAGE_PREPARATORY

from tests.test_institute_attendance import InstituteAttendanceTest, OPTS

_Fixture = InstituteAttendanceTest
del InstituteAttendanceTest

HOMEROOM = ('الصفوف الرئيسية', 'مشرف الصف', 'homeroom_section_ids', 'wiz_homeroom')


class InstituteEmployeeTeachingTest(_Fixture):

    def setUp(self):
        super().setUp()
        with self.app.app_context():
            ids = self.ids
            g2 = Grade(name='الثاني المتوسط', stage=STAGE_INTERMEDIATE,
                       school_id=ids['inst'], academic_year_id=ids['iyear'])
            g6 = Grade(name='السادس العلمي', stage=STAGE_PREPARATORY,
                       school_id=ids['inst'], academic_year_id=ids['iyear'])
            db.session.add_all([g2, g6])
            db.session.flush()
            math = Subject(name='الرياضيات', code=f'M{self.suffix[:6]}',
                           school_id=ids['inst'], academic_year_id=ids['iyear'],
                           grade_id=g2.id)
            phys = Subject(name='الفيزياء', code=f'P{self.suffix[:6]}',
                           school_id=ids['inst'], academic_year_id=ids['iyear'],
                           grade_id=g6.id)
            db.session.add_all([math, phys])
            db.session.flush()
            # An INACTIVE group already taught by instructor A.
            gi = self._group(ids['inst'], ids['iyear'], ids['isubj'],
                             'I-Group', ids['ea'], active=False)
            db.session.commit()
            ids.update(g2=g2.id, g6=g6.id, math=math.id, phys=phys.id, gi=gi.id)

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            ids = self.ids
            # Groups first (they reference subjects), then subjects, then grades.
            for g in (InstituteStudyGroup.query.execution_options(**OPTS)
                      .filter_by(school_id=ids['inst']).all()):
                if g.subject_id in (ids['math'], ids['phys']):
                    db.session.delete(g)
            db.session.flush()
            (Subject.query.execution_options(**OPTS)
             .filter(Subject.id.in_([ids['math'], ids['phys']]))
             .delete(synchronize_session=False))
            (Grade.query.execution_options(**OPTS)
             .filter(Grade.id.in_([ids['g2'], ids['g6']]))
             .delete(synchronize_session=False))
            db.session.commit()
        super().tearDown()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _view(self, bp_name, view, user_key, path, method='GET', data=None, **kw):
        import importlib
        bp = importlib.import_module(f'app.blueprints.{bp_name}')
        with self.app.test_request_context(path, method=method, data=data):
            self._login(user_key)
            try:
                return getattr(bp, view)(**kw)
            finally:
                logout_user()

    def _edit(self, user_key, emp_key, **form):
        data = {'full_name': f'Emp {emp_key}', 'save_institute_groups': '1'}
        data.update(form)
        return self._view('employees', 'edit', user_key,
                          f'/employees/{self.ids[emp_key]}/edit', method='POST',
                          data=data, emp_id=self.ids[emp_key])

    def _instructors(self):
        with self.app.app_context():
            return {g.id: g.instructor_id for g in
                    InstituteStudyGroup.query.execution_options(**OPTS)
                    .filter(InstituteStudyGroup.school_id.in_(
                        [self.ids['inst'], self.ids['oinst']])).all()}

    def _new_group(self, name, subject_key, grade_key, stage):
        return json.dumps({'name': name, 'stage': stage,
                           'grade_id': self.ids[grade_key],
                           'subject_id': self.ids[subject_key]})

    # ── 1-3, 14. Pages ──────────────────────────────────────────────────────

    def test_institute_pages_show_groups_not_sections(self):
        ids = self.ids
        create = self._view('employees', 'create', 'uadmin', '/employees/create')
        edit = self._view('employees', 'edit', 'uadmin',
                          f'/employees/{ids["ea"]}/edit', emp_id=ids['ea'])
        for html in (create, edit):
            # Visible markup only: shared wizard JS may carry school labels in
            # code paths that never run without school section inputs.
            visible = re.sub(r'<script>.*?</script>', '', html, flags=re.S)
            self.assertIn('المجموعات الدراسية التي يدرّسها', html)
            self.assertIn('المواد الدراسية التي يدرّسها', html)
            self.assertIn('إنشاء مجموعة جديدة', html)
            self.assertIn('A-Group', html)
            self.assertNotIn('O-Group', html, 'foreign institute group')
            self.assertNotIn('الصفوف والشعب التي يُدرّسها', visible)
            self.assertNotIn('data-ta-picker="teaching"', html)
            self.assertNotIn('name="save_teacher_section"', html)
            for token in HOMEROOM:
                self.assertNotIn(token, html)
        # Edit preselects the groups this instructor teaches (A and inactive I).
        current = json.loads(edit.split('const INIT_SELECTED = ')[1].split(';')[0])
        self.assertEqual(sorted(current), sorted([ids['ga'], ids['gi']]))

        # School page: unchanged grades/sections UI, no institute section.
        with self.app.app_context():
            ids['sadm'] = self._user(ids['sch'], 'etsadm', self.admin_role_id).id
            db.session.commit()
        school = self._view('employees', 'create', 'sadm', '/employees/create')
        self.assertNotIn('المجموعات الدراسية التي يدرّسها', school)
        self.assertNotIn('instGroupPicker', school)

    # ── 5-7. Assign / move / remove / foreign ───────────────────────────────

    def test_group_assignment_rules(self):
        ids = self.ids
        # Add B (currently instructor B's) -> moves to A. A and I kept.
        self._edit('uadmin', 'ea', **{'inst_group_ids[]': [ids['ga'], ids['gb'], ids['gi']]})
        inst = self._instructors()
        self.assertEqual((inst[ids['ga']], inst[ids['gb']], inst[ids['gi']]),
                         (ids['ea'], ids['ea'], ids['ea']))

        # Removing him from ACTIVE group A is refused — nothing changes.
        self._edit('uadmin', 'ea', **{'inst_group_ids[]': [ids['gb'], ids['gi']]})
        self.assertEqual(self._instructors()[ids['ga']], ids['ea'])

        # Removing the INACTIVE group I is allowed.
        self._edit('uadmin', 'ea', **{'inst_group_ids[]': [ids['ga'], ids['gb']]})
        self.assertIsNone(self._instructors()[ids['gi']])

        # A foreign institute's group rejects the whole change.
        before = self._instructors()
        self._edit('uadmin', 'ea', **{'inst_group_ids[]': [ids['ga'], ids['gb'], ids['go']]})
        self.assertEqual(self._instructors(), before)

        # Institute path never writes school assignments.
        with self.app.app_context():
            self.assertEqual(db.session.execute(teacher_subjects.select().where(
                teacher_subjects.c.employee_id == ids['ea'])).fetchall(), [])

    # ── 12-13. Quick-create ─────────────────────────────────────────────────

    def test_quick_create_precheck_save_and_permission(self):
        from werkzeug.exceptions import Forbidden
        ids = self.ids
        count = lambda: len(self._instructors())

        before = count()
        ok = self._view('institute_groups', 'quick_validate', 'uadmin',
                        '/institute-groups/quick-validate', method='POST',
                        data={'name': 'Math-New', 'stage': STAGE_INTERMEDIATE,
                              'grade_id': ids['g2'], 'subject_id': ids['math']})
        self.assertTrue(ok.get_json()['ok'])
        bad = self._view('institute_groups', 'quick_validate', 'uadmin',
                         '/institute-groups/quick-validate', method='POST',
                         data={'name': 'Bad', 'stage': STAGE_INTERMEDIATE,
                               'grade_id': ids['g2'], 'subject_id': ids['phys']})
        self.assertEqual(bad[1], 400)
        self.assertIn('المادة المحددة لا تنتمي إلى الصف المختار.', bad[0].get_json()['errors'])
        self.assertEqual(count(), before, 'the pre-check writes nothing')

        # A teacher (no manage_institute_groups) cannot use the endpoint...
        denied = None
        try:
            denied = self._view('institute_groups', 'quick_validate', 'ua',
                                '/institute-groups/quick-validate', method='POST',
                                data={'name': 'X', 'subject_id': ids['math']})
        except Forbidden:
            denied = 'forbidden'
        self.assertFalse(hasattr(denied, 'get_json')
                         and (denied.get_json(silent=True) or {}).get('ok'))
        # ...nor create a group through the save path.
        from app.blueprints.institute_groups import stage_employee_groups
        from app.models import AcademicYear, Employee, School, User
        from werkzeug.datastructures import MultiDict
        with self.app.test_request_context('/employees/x/edit', method='POST'):
            self._login('ua')
            try:
                with self.assertRaises(ValueError):
                    stage_employee_groups(
                        db.session.get(School, ids['inst'], execution_options=OPTS),
                        db.session.get(AcademicYear, ids['iyear'], execution_options=OPTS),
                        db.session.get(Employee, ids['ea'], execution_options=OPTS),
                        MultiDict({'inst_group_ids[]': str(ids['ga']),
                                   'inst_new_group[]': self._new_group(
                                       'Sneaky', 'math', 'g2', STAGE_INTERMEDIATE)}))
            finally:
                logout_user()
                with self.app.app_context():
                    db.session.rollback()
        self.assertEqual(count(), before)

        # Manager saves the employee with a pending group -> created by the
        # canonical rules, active, instructed by this employee, this year.
        self._edit('uadmin', 'ea', **{
            'inst_group_ids[]': [ids['ga'], ids['gi']],
            'inst_new_group[]': [self._new_group('Math-New', 'math', 'g2',
                                                 STAGE_INTERMEDIATE)]})
        with self.app.app_context():
            g = (InstituteStudyGroup.query.execution_options(**OPTS)
                 .filter_by(school_id=ids['inst'], name='Math-New').one())
            self.assertEqual((g.instructor_id, g.subject_id, g.academic_year_id,
                              g.is_active), (ids['ea'], ids['math'], ids['iyear'], True))

        # An invalid pending group (foreign subject pairing) creates nothing.
        self._edit('uadmin', 'ea', **{
            'inst_group_ids[]': [ids['ga'], ids['gi']],
            'inst_new_group[]': [self._new_group('Bad', 'phys', 'g2',
                                                 STAGE_INTERMEDIATE)]})
        self.assertEqual(count(), before + 1)

    # ── manage_employees WITHOUT manage_institute_groups: read-only ─────────

    def test_employees_only_user_is_read_only(self):
        from app.models import Permission, User
        ids = self.ids
        with self.app.app_context():
            user = db.session.get(User, ids['ub'], execution_options=OPTS)
            user.extra_permissions.append(
                Permission.query.filter_by(name='manage_employees').first())
            db.session.commit()
            self.assertTrue(user.has_permission('manage_employees'))
            self.assertFalse(user.has_permission('manage_institute_groups'))

        html = self._view('employees', 'edit', 'ub',
                          f'/employees/{ids["ea"]}/edit', emp_id=ids['ea'])
        self.assertIn('المجموعات الدراسية التي يدرّسها', html)
        self.assertIn('A-Group', html)                 # his current group, shown
        self.assertIn('للعرض فقط', html)
        for control in ('id="instGroupPicker"', 'id="instNewGroupBtn"',
                        'id="instGroupModal"', 'name="save_institute_groups"',
                        'B-Group'):
            self.assertNotIn(control, html)

        # Forged POST: move B, drop A, create a group — nothing may change.
        before = self._instructors()
        self._edit('ub', 'ea', **{
            'inst_group_ids[]': [ids['gb'], ids['gi']],
            'inst_new_group[]': [self._new_group('Forged', 'math', 'g2',
                                                 STAGE_INTERMEDIATE)]})
        self.assertEqual(self._instructors(), before)

    def test_employees_only_user_saves_without_touching_groups(self):
        """Ordinary create/edit work for manage_employees-only; any forged group
        field (with or without the marker) changes and creates nothing."""
        from app.models import Employee, Permission, User
        ids = self.ids
        with self.app.app_context():
            user = db.session.get(User, ids['ub'], execution_options=OPTS)
            user.extra_permissions.append(
                Permission.query.filter_by(name='manage_employees').first())
            db.session.commit()
        before = self._instructors()
        group_count = lambda: len(self._instructors())

        # 1. Ordinary edit — exactly what the read-only page posts (no group field).
        self._view('employees', 'edit', 'ub', f'/employees/{ids["ea"]}/edit',
                   method='POST', data={'full_name': 'Renamed Instructor'},
                   emp_id=ids['ea'])
        with self.app.app_context():
            self.assertEqual(db.session.get(Employee, ids['ea'],
                                            execution_options=OPTS).full_name,
                             'Renamed Instructor')
        self.assertEqual(self._instructors(), before)

        # 2. Ordinary create — no group field.
        self._view('employees', 'create', 'ub', '/employees/create',
                   method='POST', data={'full_name': 'Plain New Employee'})
        with self.app.app_context():
            self.assertEqual(Employee.query.execution_options(**OPTS).filter_by(
                school_id=ids['inst'], full_name='Plain New Employee').count(), 1)
        self.assertEqual(self._instructors(), before)

        # 3. Forged payloads, each field alone and together, edit and create.
        forged = self._new_group('Forged', 'math', 'g2', STAGE_INTERMEDIATE)
        for payload in ({'inst_group_ids[]': [ids['gb'], ids['gi']]},
                        {'inst_new_group[]': [forged]},
                        {'save_institute_groups': '1'},
                        {'save_institute_groups': '1',
                         'inst_group_ids[]': [ids['gb']], 'inst_new_group[]': [forged]}):
            with self.subTest(payload=sorted(payload)):
                self._view('employees', 'edit', 'ub', f'/employees/{ids["ea"]}/edit',
                           method='POST', emp_id=ids['ea'],
                           data={'full_name': 'Renamed Instructor', **payload})
                self._view('employees', 'create', 'ub', '/employees/create',
                           method='POST',
                           data={'full_name': 'Forged Create', **payload})
                self.assertEqual(self._instructors(), before)
                self.assertEqual(group_count(), len(before))
        with self.app.app_context():
            # A create carrying a group mutation is rejected as a whole.
            self.assertEqual(Employee.query.execution_options(**OPTS).filter_by(
                school_id=ids['inst'], full_name='Forged Create').count(), 0)

    def test_create_transaction_rollback_leaves_nothing(self):
        """The create flow stages groups in the employee transaction; if that
        transaction is rolled back, no group and no assignment survive."""
        from app.blueprints.institute_groups import stage_employee_groups
        from app.models import AcademicYear, School
        from werkzeug.datastructures import MultiDict
        ids = self.ids
        before = self._instructors()
        with self.app.test_request_context('/employees/create', method='POST'):
            self._login('uadmin')
            try:
                emp = self._employee(ids['inst'], 'TX')        # flushed, uncommitted
                created = stage_employee_groups(
                    db.session.get(School, ids['inst'], execution_options=OPTS),
                    db.session.get(AcademicYear, ids['iyear'], execution_options=OPTS),
                    emp, MultiDict([('inst_group_ids[]', str(ids['gb'])),
                                    ('inst_new_group[]', self._new_group(
                                        'Phys-New', 'phys', 'g6', STAGE_PREPARATORY))]))
                self.assertEqual(len(created), 1)
                db.session.rollback()                          # later step failed
            finally:
                logout_user()
        self.assertEqual(self._instructors(), before)


for _inherited in list(vars(_Fixture)):
    if _inherited.startswith('test_'):
        setattr(InstituteEmployeeTeachingTest, _inherited, None)

del _Fixture


if __name__ == '__main__':
    unittest.main()

# -*- coding: utf-8 -*-
"""Employee attendance settings + employee shifts — Phase 1 (configuration).

Phase 1 adds SEPARATE employee attendance configuration and employee shifts,
and introduces ONE shared effective-settings resolver used by both audiences.
It deliberately changes NO attendance number: no absence fix, no Face ID
change, no payroll change.  These tests pin that contract.

Two classes:

  EmployeeAttendanceSettingsUnitTest
      Pure, no database, no app context.  get_effective_attendance_settings is
      specified as query-free, so it is testable with plain stub objects — which
      is also the regression guard that it never grows a query.

  EmployeeAttendanceSettingsDbTest
      Needs the isolated local PostgreSQL test database and the
      h8e9m1p2s3t4 migration.  Skipped (not failed) when either is absent, so
      the unit contract above still runs anywhere.

Pinned behaviour:
  * employee and student timings live in different columns and never read each
    other's values, in either direction;
  * shift late/dismissal override the school value; an absent shift, or an
    inactive one, falls back to the school-level employee settings;
  * the absence cutoff switches on the audience's OWN shift toggle and never
    cross-falls-back — not between modes, not between audiences;
  * determine_check_in_status is reused by both audiences and its default is
    student-compatible, so no existing call site changes;
  * the student half of the resolver reads exactly the legacy columns;
  * the migration copies start/late/departure forward but NOT the absence
    threshold;
  * employee shift CRUD and assignment are school-scoped.
"""
import unittest
from datetime import time
from types import SimpleNamespace

from app.utils.attendance_helpers import (
    determine_check_in_status,
    get_effective_attendance_settings,
    get_employee_shift,
)


def _school(**overrides):
    """A stub settings row carrying both audiences' columns.

    Defaults are deliberately DIFFERENT per audience so any accidental
    cross-audience read shows up as a wrong value rather than a passing test.
    """
    values = dict(
        id=1,
        is_institute=False,
        # student
        att_late_threshold=time(7, 30),
        att_absence_threshold=time(9, 0),
        att_departure_time=time(13, 0),
        enable_attendance_shifts=False,
        shift_absent_after_time=None,
        # employee
        emp_att_late_threshold=time(8, 15),
        emp_att_absence_threshold=time(10, 30),
        emp_att_departure_time=time(15, 45),
        emp_enable_attendance_shifts=False,
        emp_shift_absent_after_time=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _shift(late=None, dismissal=None, start=time(7, 0), active=True, school_id=1):
    """A stub shift. AttendanceShift and EmployeeAttendanceShift expose the same
    attribute names on purpose, so one stub stands in for either."""
    return SimpleNamespace(id=99, school_id=school_id, name='S',
                           start_time=start, late_after_time=late,
                           dismissal_time=dismissal, is_active=active)


class EmployeeAttendanceSettingsUnitTest(unittest.TestCase):

    # ── Audience separation ──────────────────────────────────────────────────

    def test_employee_resolver_reads_only_employee_columns(self):
        eff = get_effective_attendance_settings(_school(), 'employees')
        self.assertEqual(eff.late_threshold, time(8, 15))
        self.assertEqual(eff.absence_cutoff, time(10, 30))
        self.assertEqual(eff.departure_time, time(15, 45))
        self.assertFalse(eff.shift_enabled)
        self.assertEqual(eff.audience, 'employees')

    def test_student_resolver_reads_only_student_columns(self):
        eff = get_effective_attendance_settings(_school(), 'students')
        self.assertEqual(eff.late_threshold, time(7, 30))
        self.assertEqual(eff.absence_cutoff, time(9, 0))
        self.assertEqual(eff.departure_time, time(13, 0))
        self.assertFalse(eff.shift_enabled)

    def test_changing_employee_values_does_not_change_student_values(self):
        """(2) Editing the employee timings leaves every student value intact."""
        school = _school(emp_att_late_threshold=time(11, 11),
                         emp_att_departure_time=time(22, 22),
                         emp_att_absence_threshold=time(23, 23))
        students = get_effective_attendance_settings(school, 'students')
        self.assertEqual(students.late_threshold, time(7, 30))
        self.assertEqual(students.absence_cutoff, time(9, 0))
        self.assertEqual(students.departure_time, time(13, 0))

    def test_changing_student_values_does_not_change_employee_values(self):
        school = _school(att_late_threshold=time(1, 1),
                         att_departure_time=time(2, 2),
                         att_absence_threshold=time(3, 3))
        employees = get_effective_attendance_settings(school, 'employees')
        self.assertEqual(employees.late_threshold, time(8, 15))
        self.assertEqual(employees.absence_cutoff, time(10, 30))
        self.assertEqual(employees.departure_time, time(15, 45))

    def test_employee_cutoff_never_falls_back_to_student_cutoff(self):
        """An unconfigured employee cutoff stays NULL — it must not borrow the
        student one, which would silently invent an employee absence policy."""
        school = _school(emp_att_absence_threshold=None,
                         att_absence_threshold=time(9, 0))
        self.assertIsNone(
            get_effective_attendance_settings(school, 'employees').absence_cutoff)

    # ── Shift override / fallback ────────────────────────────────────────────

    def test_no_shift_uses_school_employee_settings(self):
        """(5) Employee with no shift → school-level employee settings."""
        eff = get_effective_attendance_settings(
            _school(emp_enable_attendance_shifts=True), 'employees', shift=None)
        self.assertEqual(eff.late_threshold, time(8, 15))
        self.assertEqual(eff.departure_time, time(15, 45))

    def test_active_shift_overrides_late_and_departure(self):
        """(6) An assigned shift's own times win over the school values."""
        eff = get_effective_attendance_settings(
            _school(emp_enable_attendance_shifts=True), 'employees',
            shift=_shift(late=time(9, 5), dismissal=time(17, 30)))
        self.assertEqual(eff.late_threshold, time(9, 5))
        self.assertEqual(eff.departure_time, time(17, 30))

    def test_shift_with_partial_times_falls_back_per_field(self):
        """A shift that sets only one of the two times falls back for the other
        — the same per-field precedence the student engine already uses."""
        eff = get_effective_attendance_settings(
            _school(emp_enable_attendance_shifts=True), 'employees',
            shift=_shift(late=time(9, 5), dismissal=None))
        self.assertEqual(eff.late_threshold, time(9, 5))
        self.assertEqual(eff.departure_time, time(15, 45))

    def test_employee_shift_mode_selects_the_shift_cutoff_column(self):
        school = _school(emp_enable_attendance_shifts=True,
                         emp_shift_absent_after_time=time(10, 0),
                         emp_att_absence_threshold=time(10, 30))
        eff = get_effective_attendance_settings(school, 'employees')
        self.assertTrue(eff.shift_enabled)
        self.assertEqual(eff.absence_cutoff, time(10, 0))

    def test_employee_shift_cutoff_null_stays_null_fail_closed(self):
        """Shift mode on + no shift cutoff → NULL. It must NOT fall back to the
        unified emp_att_absence_threshold (mirrors the student rule)."""
        school = _school(emp_enable_attendance_shifts=True,
                         emp_shift_absent_after_time=None,
                         emp_att_absence_threshold=time(10, 30))
        self.assertIsNone(
            get_effective_attendance_settings(school, 'employees').absence_cutoff)

    def test_toggles_are_independent(self):
        """The employee toggle must not be influenced by the student toggle."""
        school = _school(enable_attendance_shifts=True,
                         emp_enable_attendance_shifts=False)
        self.assertTrue(get_effective_attendance_settings(school, 'students').shift_enabled)
        self.assertFalse(get_effective_attendance_settings(school, 'employees').shift_enabled)

    # ── Student rules preserved exactly ──────────────────────────────────────

    def test_student_shift_cutoff_semantics_unchanged(self):
        """(8) Shift mode → shift_absent_after_time; unified →
        att_absence_threshold; no cross-fallback; fail closed on NULL."""
        unified = _school(enable_attendance_shifts=False,
                          att_absence_threshold=time(9, 0),
                          shift_absent_after_time=time(8, 0))
        self.assertEqual(
            get_effective_attendance_settings(unified, 'students').absence_cutoff,
            time(9, 0))

        shift_mode = _school(enable_attendance_shifts=True,
                             att_absence_threshold=time(9, 0),
                             shift_absent_after_time=time(8, 0))
        self.assertEqual(
            get_effective_attendance_settings(shift_mode, 'students').absence_cutoff,
            time(8, 0))

        fail_closed = _school(enable_attendance_shifts=True,
                              att_absence_threshold=time(9, 0),
                              shift_absent_after_time=None)
        self.assertIsNone(
            get_effective_attendance_settings(fail_closed, 'students').absence_cutoff)

    def test_student_field_map_is_the_legacy_column_set(self):
        """(9) Guard against a rename: the student half must point at exactly
        the columns the existing student engine reads."""
        from app.utils.attendance_helpers import _AUDIENCE_SETTINGS_FIELDS
        self.assertEqual(
            _AUDIENCE_SETTINGS_FIELDS['students'],
            ('att_late_threshold', 'att_absence_threshold', 'att_departure_time',
             'enable_attendance_shifts', 'shift_absent_after_time'))
        self.assertEqual(
            _AUDIENCE_SETTINGS_FIELDS['employees'],
            ('emp_att_late_threshold', 'emp_att_absence_threshold',
             'emp_att_departure_time', 'emp_enable_attendance_shifts',
             'emp_shift_absent_after_time'))

    def test_unknown_audience_is_rejected(self):
        with self.assertRaises(ValueError):
            get_effective_attendance_settings(_school(), 'teachers')

    def test_resolver_tolerates_no_school(self):
        eff = get_effective_attendance_settings(None, 'employees')
        self.assertIsNone(eff.late_threshold)
        self.assertIsNone(eff.absence_cutoff)
        self.assertIsNone(eff.departure_time)
        self.assertFalse(eff.shift_enabled)

    # ── determine_check_in_status reuse ──────────────────────────────────────

    def test_check_in_status_default_audience_is_student_compatible(self):
        """Every existing call site passes no audience and must be unaffected."""
        school = _school()
        self.assertEqual(determine_check_in_status(time(7, 0), school), 'present')
        self.assertEqual(determine_check_in_status(time(7, 30), school), 'late')
        self.assertEqual(determine_check_in_status(time(8, 0), school), 'late')
        # 08:00 is late for students (07:30) but NOT for employees (08:15):
        # proof the two audiences read different columns through one function.
        self.assertEqual(
            determine_check_in_status(time(8, 0), school, audience='employees'),
            'present')

    def test_check_in_status_employee_threshold(self):
        school = _school()
        self.assertEqual(
            determine_check_in_status(time(8, 15), school, audience='employees'), 'late')
        self.assertEqual(
            determine_check_in_status(time(8, 14), school, audience='employees'), 'present')

    def test_check_in_status_employee_shift_overrides_threshold(self):
        school = _school()
        shift = _shift(late=time(9, 0))
        self.assertEqual(
            determine_check_in_status(time(8, 30), school, shift=shift,
                                      audience='employees'), 'present')
        self.assertEqual(
            determine_check_in_status(time(9, 0), school, shift=shift,
                                      audience='employees'), 'late')

    def test_check_in_status_null_employee_threshold_disables_lateness(self):
        school = _school(emp_att_late_threshold=None)
        self.assertEqual(
            determine_check_in_status(time(23, 0), school, audience='employees'),
            'present')

    def test_check_in_status_institute_double_switch_per_audience(self):
        """The institute two-off-switch rule now keys off the audience's own
        school-level threshold, not always the student one."""
        inst = _school(is_institute=True, emp_att_late_threshold=None)
        self.assertEqual(
            determine_check_in_status(time(23, 0), inst, shift=_shift(late=time(9, 0)),
                                      audience='employees'), 'present')
        # Student side with its threshold still set behaves as before.
        self.assertEqual(
            determine_check_in_status(time(23, 0), inst, shift=_shift(late=time(9, 0)),
                                      audience='students'), 'late')

    def test_check_in_status_tolerates_no_settings(self):
        self.assertEqual(determine_check_in_status(time(23, 0), None), 'present')

    # ── get_employee_shift guards reachable without a database ───────────────

    def test_employee_shift_is_none_when_feature_disabled(self):
        """The toggle is checked BEFORE any query, so this needs no database —
        and that ordering is itself the thing being pinned."""
        school = _school(emp_enable_attendance_shifts=False)
        employee = SimpleNamespace(id=1, school_id=1, shift_id=99)
        self.assertIsNone(get_employee_shift(employee, school))

    def test_employee_shift_is_none_without_assignment(self):
        school = _school(emp_enable_attendance_shifts=True)
        employee = SimpleNamespace(id=1, school_id=1, shift_id=None)
        self.assertIsNone(get_employee_shift(employee, school))
        self.assertIsNone(get_employee_shift(SimpleNamespace(id=1, school_id=1), school))

    def test_employee_shift_is_none_without_school(self):
        employee = SimpleNamespace(id=1, school_id=1, shift_id=99)
        self.assertIsNone(get_employee_shift(employee, None))

    # ── Migration contract ───────────────────────────────────────────────────

    def _migration_module(self):
        import importlib.util
        from pathlib import Path
        path = (Path(__file__).resolve().parents[1] / 'migrations' / 'versions'
                / 'h8e9m1p2s3t4_employee_attendance_settings_shifts.py')
        spec = importlib.util.spec_from_file_location('_emp_att_mig', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_migration_copies_start_late_departure_forward(self):
        """(10) The three values employee code already reads are copied, so
        behaviour is unchanged on deploy."""
        mapping = dict(self._migration_module()._COPY_FORWARD)
        self.assertEqual(mapping['att_start_time'], 'emp_att_start_time')
        self.assertEqual(mapping['att_late_threshold'], 'emp_att_late_threshold')
        self.assertEqual(mapping['att_departure_time'], 'emp_att_departure_time')

    def test_migration_does_not_copy_absence_threshold(self):
        """(11) emp_att_absence_threshold must stay NULL: copying the student
        cutoff would introduce an employee absence policy that was never
        configured and would change absence counts + payroll deductions."""
        module = self._migration_module()
        mapping = dict(module._COPY_FORWARD)
        self.assertNotIn('att_absence_threshold', mapping)
        self.assertNotIn('emp_att_absence_threshold', mapping.values())
        # It is still created as a column, just never populated.
        self.assertIn('emp_att_absence_threshold', module._TIME_COLUMNS)
        self.assertEqual(module._SETTINGS_TABLES, ('schools', 'school_settings'))
        self.assertEqual(module.down_revision, 'g7p5l1v3l0c2')

    def test_employee_shift_model_shape(self):
        """Attribute names must match AttendanceShift so one calculator serves
        both, and the dead student column must not be replicated."""
        from app.models import AttendanceShift, EmployeeAttendanceShift
        for attr in ('school_id', 'name', 'start_time', 'late_after_time',
                     'dismissal_time', 'is_active'):
            self.assertTrue(hasattr(EmployeeAttendanceShift, attr), attr)
            self.assertTrue(hasattr(AttendanceShift, attr), attr)
        self.assertFalse(hasattr(EmployeeAttendanceShift, 'absent_after_time'))
        self.assertEqual(EmployeeAttendanceShift.__tablename__,
                         'employee_attendance_shifts')
        self.assertTrue(EmployeeAttendanceShift.__school_scoped__)
        # The student shift table is untouched by this phase.
        self.assertEqual(AttendanceShift.__tablename__, 'attendance_shifts')


# ═════════════════════════════════════════════════════════════════════════════
#  Database-backed: settings save, CRUD scoping, cross-school assignment
# ═════════════════════════════════════════════════════════════════════════════

def _db_ready():
    """(app, reason) — the app when the isolated test DB has the new schema."""
    try:
        from app import create_app
        from app.models import db
        app = create_app('testing')
        with app.app_context():
            import sqlalchemy as sa
            with db.engine.connect() as conn:
                if not conn.execute(sa.text(
                        "select to_regclass('employee_attendance_shifts') is not null"
                )).scalar():
                    return None, ('migration h8e9m1p2s3t4 not applied to the test '
                                  'database (employee_attendance_shifts missing)')
        return app, None
    except Exception as exc:                                  # pragma: no cover
        return None, f'test database unreachable: {type(exc).__name__}: {exc}'


_APP, _SKIP_REASON = _db_ready()


@unittest.skipIf(_APP is None, _SKIP_REASON or 'no test database')
class EmployeeAttendanceSettingsDbTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _APP

    def setUp(self):
        from uuid import uuid4
        from app.models import db, EmployeeAttendanceShift, Employee, School
        self.suffix = uuid4().hex[:10]
        self.ids = {}
        with self.app.app_context():
            for tag in ('a', 'b'):
                school = School(
                    school_name=f'EmpAtt {tag} {self.suffix}',
                    code=f'EA{tag.upper()}{self.suffix[:6]}',
                    capacity=0, is_active=True, timezone='Asia/Baghdad',
                    att_late_threshold=__import__('datetime').time(7, 30),
                    att_departure_time=__import__('datetime').time(13, 0),
                    emp_att_late_threshold=__import__('datetime').time(8, 15),
                    emp_att_departure_time=__import__('datetime').time(15, 45),
                    emp_enable_attendance_shifts=True)
                db.session.add(school)
                db.session.flush()
                shift = EmployeeAttendanceShift(
                    school_id=school.id, name=f'Shift {tag} {self.suffix}',
                    start_time=__import__('datetime').time(7, 0),
                    late_after_time=__import__('datetime').time(9, 5),
                    # Canonical rule: a valid shift needs an absence cutoff
                    # after its late time (is_valid_employee_shift).
                    absent_after_time=__import__('datetime').time(10, 0),
                    dismissal_time=__import__('datetime').time(17, 30),
                    is_active=True)
                db.session.add(shift)
                db.session.flush()
                employee = Employee(
                    employee_id=f'EA-{tag.upper()}-{self.suffix}',
                    full_name=f'Employee {tag} {self.suffix}',
                    school_id=school.id, base_salary=1000, status='active')
                db.session.add(employee)
                db.session.flush()
                self.ids[f'school_{tag}'] = school.id
                self.ids[f'shift_{tag}'] = shift.id
                self.ids[f'employee_{tag}'] = employee.id
            db.session.commit()

    def tearDown(self):
        from app.models import db, EmployeeAttendanceShift, Employee, School
        OPTS = {'bypass_tenant_scope': True}
        with self.app.app_context():
            for tag in ('a', 'b'):
                (Employee.query.execution_options(**OPTS)
                 .filter_by(id=self.ids[f'employee_{tag}'])
                 .delete(synchronize_session=False))
                (EmployeeAttendanceShift.query.execution_options(**OPTS)
                 .filter_by(id=self.ids[f'shift_{tag}'])
                 .delete(synchronize_session=False))
                (School.query.execution_options(**OPTS)
                 .filter_by(id=self.ids[f'school_{tag}'])
                 .delete(synchronize_session=False))
            db.session.commit()

    def test_employee_shift_rows_are_school_scoped(self):
        """(3) Each school sees only its own employee shifts."""
        from app.models import EmployeeAttendanceShift
        OPTS = {'bypass_tenant_scope': True}
        with self.app.app_context():
            for tag in ('a', 'b'):
                rows = (EmployeeAttendanceShift.query.execution_options(**OPTS)
                        .filter_by(school_id=self.ids[f'school_{tag}']).all())
                self.assertEqual([r.id for r in rows], [self.ids[f'shift_{tag}']])

    def test_cross_school_shift_is_never_resolved(self):
        """(4) A shift belonging to School B must never influence an employee of
        School A, even if an id from B were somehow stored."""
        from app.models import db, Employee, School
        OPTS = {'bypass_tenant_scope': True}
        with self.app.app_context():
            emp = (Employee.query.execution_options(**OPTS)
                   .get(self.ids['employee_a']))
            school_a = School.query.execution_options(**OPTS).get(self.ids['school_a'])
            emp.shift_id = self.ids['shift_b']          # cross-school assignment
            db.session.flush()
            self.assertIsNone(get_employee_shift(emp, school_a))
            # ...and the effective settings therefore stay School A's own.
            eff = get_effective_attendance_settings(
                school_a, 'employees', shift=get_employee_shift(emp, school_a))
            self.assertEqual(eff.late_threshold, school_a.emp_att_late_threshold)
            db.session.rollback()

    def test_own_school_active_shift_resolves_and_overrides(self):
        """(6) end-to-end: assignment → lookup → resolver."""
        from app.models import db, Employee, School
        OPTS = {'bypass_tenant_scope': True}
        with self.app.app_context():
            emp = Employee.query.execution_options(**OPTS).get(self.ids['employee_a'])
            school_a = School.query.execution_options(**OPTS).get(self.ids['school_a'])
            emp.shift_id = self.ids['shift_a']
            db.session.flush()
            shift = get_employee_shift(emp, school_a)
            self.assertIsNotNone(shift)
            eff = get_effective_attendance_settings(school_a, 'employees', shift=shift)
            self.assertEqual(eff.late_threshold, shift.late_after_time)
            self.assertEqual(eff.departure_time, shift.dismissal_time)
            db.session.rollback()

    def test_inactive_shift_falls_back_to_school_employee_settings(self):
        """(7) Deactivating a shift must not change the assignment, only the
        resolution: the employee falls back to the school employee values."""
        from app.models import db, Employee, EmployeeAttendanceShift, School
        OPTS = {'bypass_tenant_scope': True}
        with self.app.app_context():
            emp = Employee.query.execution_options(**OPTS).get(self.ids['employee_a'])
            school_a = School.query.execution_options(**OPTS).get(self.ids['school_a'])
            shift = (EmployeeAttendanceShift.query.execution_options(**OPTS)
                     .get(self.ids['shift_a']))
            emp.shift_id = shift.id
            shift.is_active = False
            db.session.flush()
            self.assertIsNone(get_employee_shift(emp, school_a))
            self.assertEqual(emp.shift_id, shift.id)      # assignment preserved
            eff = get_effective_attendance_settings(
                school_a, 'employees', shift=get_employee_shift(emp, school_a))
            self.assertEqual(eff.late_threshold, school_a.emp_att_late_threshold)
            self.assertEqual(eff.departure_time, school_a.emp_att_departure_time)
            db.session.rollback()

    def test_employee_settings_save_independently_of_student_settings(self):
        """(1)(2) Writing the employee columns leaves the student ones intact."""
        from datetime import time as _t
        from app.models import db, School
        OPTS = {'bypass_tenant_scope': True}
        with self.app.app_context():
            school = School.query.execution_options(**OPTS).get(self.ids['school_a'])
            before = (school.att_late_threshold, school.att_departure_time,
                      school.att_absence_threshold, school.enable_attendance_shifts,
                      school.shift_absent_after_time)
            school.emp_att_start_time        = _t(8, 0)
            school.emp_att_late_threshold    = _t(8, 30)
            school.emp_att_absence_threshold = _t(11, 0)
            school.emp_att_departure_time    = _t(16, 0)
            school.emp_shift_absent_after_time = _t(10, 0)
            db.session.commit()

            db.session.expire_all()
            school = School.query.execution_options(**OPTS).get(self.ids['school_a'])
            self.assertEqual(school.emp_att_late_threshold, _t(8, 30))
            self.assertEqual(school.emp_att_absence_threshold, _t(11, 0))
            self.assertEqual(
                (school.att_late_threshold, school.att_departure_time,
                 school.att_absence_threshold, school.enable_attendance_shifts,
                 school.shift_absent_after_time),
                before)

    def test_new_school_defaults_are_off_and_unassigned(self):
        """A fresh school must not acquire an employee absence policy, and no
        employee is auto-assigned to a shift."""
        from app.models import Employee, School
        OPTS = {'bypass_tenant_scope': True}
        with self.app.app_context():
            emp = Employee.query.execution_options(**OPTS).get(self.ids['employee_b'])
            self.assertIsNone(emp.shift_id)
            school = School.query.execution_options(**OPTS).get(self.ids['school_b'])
            self.assertIsNone(school.emp_att_absence_threshold)
            self.assertIsNone(school.emp_shift_absent_after_time)


if __name__ == '__main__':                                    # pragma: no cover
    unittest.main()

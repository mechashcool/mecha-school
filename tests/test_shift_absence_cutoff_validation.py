# -*- coding: utf-8 -*-
"""Shift-mode ordering rule — global absence cutoff vs shift late thresholds.

    shift start  <  shift late threshold  <  GLOBAL ABSENCE CUTOFF

The single school-level absence cutoff must be STRICTLY AFTER the EFFECTIVE late
threshold of every ACTIVE shift, so nobody in a later shift can be marked absent
while still inside their valid arrival window. Equality is invalid.
dismissal_time is deliberately NOT part of the rule.

Both audiences, both directions:
  * saving the cutoff           → every active shift is checked
  * create / edit / re-activate → the one shift is checked against the cutoff

No database: the rule lives in two pure helpers
(conflicting_shift_late_thresholds / effective_shift_late_threshold), and the
route wiring is pinned structurally.
"""
import unittest
from datetime import time
from pathlib import Path
from types import SimpleNamespace

from app.utils.attendance_helpers import (
    conflicting_shift_late_thresholds,
    effective_shift_late_threshold,
    shift_absence_boundary,
)

ROOT = Path(__file__).resolve().parents[1]


def _school(**overrides):
    values = dict(
        id=1, is_institute=False,
        # student
        att_late_threshold=time(7, 30), att_absence_threshold=time(9, 0),
        att_departure_time=time(13, 0), enable_attendance_shifts=True,
        shift_absent_after_time=None,
        # employee
        emp_att_late_threshold=time(8, 15), emp_att_absence_threshold=time(10, 30),
        emp_att_departure_time=time(15, 45), emp_enable_attendance_shifts=True,
        emp_shift_absent_after_time=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _shift(name, start, late, dismissal=None, active=True):
    return SimpleNamespace(id=abs(hash(name)) % 1000, school_id=1, name=name,
                           start_time=start, late_after_time=late,
                           dismissal_time=dismissal, is_active=active)


# The worked example from the spec.
MORNING = _shift('صباحي', time(7, 0), time(7, 30), dismissal=time(13, 0))
AFTERNOON = _shift('مسائي', time(13, 30), time(14, 0), dismissal=time(18, 0))
BOTH = [MORNING, AFTERNOON]


class CutoffSaveRuleTest(unittest.TestCase):
    """(1)(2)(3)(8)(9)(10)(11)(16) Saving the global cutoff."""

    def _conf(self, audience, cutoff, shifts=BOTH, school=None):
        return conflicting_shift_late_thresholds(
            school or _school(), audience, shifts, cutoff)

    # ── students ─────────────────────────────────────────────────────────────

    def test_student_cutoff_after_latest_late_threshold_is_allowed(self):
        self.assertEqual(self._conf('students', time(14, 1)), [])
        self.assertEqual(self._conf('students', time(16, 0)), [])

    def test_student_cutoff_equal_to_a_late_threshold_is_rejected(self):
        conflicts = self._conf('students', time(14, 0))
        self.assertEqual([s.name for s, _ in conflicts], ['مسائي'])

    def test_student_cutoff_before_a_late_threshold_is_rejected(self):
        conflicts = self._conf('students', time(8, 0))
        self.assertEqual([s.name for s, _ in conflicts], ['مسائي'])
        # Before BOTH thresholds → both reported, ordered by threshold.
        conflicts = self._conf('students', time(7, 0))
        self.assertEqual([s.name for s, _ in conflicts], ['صباحي', 'مسائي'])

    def test_dismissal_time_after_the_cutoff_does_not_reject(self):
        """(8)(16) 14:01 is valid even though dismissal is 18:00."""
        self.assertEqual(self._conf('students', time(14, 1)), [])
        self.assertEqual(self._conf('employees', time(14, 1)), [])
        # A late dismissal alone can never create a conflict.
        late_dismissal = [_shift('ليلي', time(7, 0), time(7, 30),
                                 dismissal=time(23, 30))]
        self.assertEqual(self._conf('students', time(8, 0), late_dismissal), [])

    # ── employees ────────────────────────────────────────────────────────────

    def test_employee_cutoff_after_latest_late_threshold_is_allowed(self):
        self.assertEqual(self._conf('employees', time(14, 1)), [])

    def test_employee_cutoff_equal_to_a_late_threshold_is_rejected(self):
        conflicts = self._conf('employees', time(14, 0))
        self.assertEqual([s.name for s, _ in conflicts], ['مسائي'])

    def test_employee_cutoff_before_a_late_threshold_is_rejected(self):
        self.assertEqual([s.name for s, _ in self._conf('employees', time(13, 59))],
                         ['مسائي'])

    # ── shared semantics ─────────────────────────────────────────────────────

    def test_only_active_shifts_are_passed_in_and_inactive_ones_are_ignored(self):
        """The route supplies active shifts only; an inactive conflicting shift
        may stay stored without blocking the cutoff."""
        self.assertEqual(self._conf('students', time(8, 0), [MORNING]), [])

    def test_cleared_cutoff_never_conflicts(self):
        self.assertEqual(self._conf('students', None), [])
        self.assertEqual(self._conf('employees', None), [])

    def test_no_shifts_never_conflicts(self):
        self.assertEqual(self._conf('students', time(1, 0), []), [])
        self.assertEqual(self._conf('students', time(1, 0), None), [])


class EffectiveLateFallbackTest(unittest.TestCase):
    """The NULL late_after_time fallback matches the runtime resolver exactly."""

    def test_null_shift_late_falls_back_to_the_audience_school_threshold(self):
        school = _school()
        blank = _shift('بدون تأخير', time(7, 0), None)
        self.assertEqual(effective_shift_late_threshold(school, 'students', blank),
                         time(7, 30))      # att_late_threshold
        self.assertEqual(effective_shift_late_threshold(school, 'employees', blank),
                         time(8, 15))      # emp_att_late_threshold

    def test_fallback_threshold_is_what_gets_validated(self):
        """A shift with no own late time is still validated — against the
        school-level value, not skipped and not against start_time."""
        school = _school()
        blank = _shift('بدون تأخير', time(7, 0), None)
        # employees fall back to 08:15, so an 08:00 cutoff conflicts...
        self.assertEqual(
            [s.name for s, _ in conflicting_shift_late_thresholds(
                school, 'employees', [blank], time(8, 0))], ['بدون تأخير'])
        # ...while 08:16 is fine.
        self.assertEqual(
            conflicting_shift_late_thresholds(school, 'employees', [blank],
                                              time(8, 16)), [])

    def test_lateness_fully_disabled_falls_back_to_start_time(self):
        """Both sources NULL = no late threshold, so the shift's START TIME is
        the validation boundary. Without that fallback a 07:00 shift with
        lateness disabled would accept a 01:00 absence cutoff, declaring those
        people absent six hours before their day begins."""
        school = _school(att_late_threshold=None, emp_att_late_threshold=None)
        blank = _shift('بدون تأخير', time(7, 0), None)

        for audience in ('students', 'employees'):
            # No late threshold exists...
            self.assertIsNone(
                effective_shift_late_threshold(school, audience, blank), audience)
            # ...so the boundary is the start time.
            self.assertEqual(
                shift_absence_boundary(school, audience, blank), time(7, 0), audience)

            # cutoff BEFORE start → rejected, and the reported boundary is 07:00
            conflicts = conflicting_shift_late_thresholds(
                school, audience, [blank], time(1, 0))
            self.assertEqual([(s.name, b) for s, b in conflicts],
                             [('بدون تأخير', time(7, 0))], audience)

            # cutoff EQUAL to start → rejected (equality is invalid)
            self.assertEqual(
                [s.name for s, _ in conflicting_shift_late_thresholds(
                    school, audience, [blank], time(7, 0))],
                ['بدون تأخير'], audience)

            # cutoff strictly AFTER start → allowed
            self.assertEqual(
                conflicting_shift_late_thresholds(school, audience, [blank],
                                                  time(7, 1)), [], audience)

    def test_audiences_never_read_each_others_fallback(self):
        school = _school(att_late_threshold=time(7, 30),
                         emp_att_late_threshold=time(8, 15))
        blank = _shift('بدون تأخير', time(7, 0), None)
        self.assertNotEqual(
            effective_shift_late_threshold(school, 'students', blank),
            effective_shift_late_threshold(school, 'employees', blank))


class ReverseDirectionRuleTest(unittest.TestCase):
    """(4)(5)(6)(7)(12)(13)(14)(15) Create / edit / re-activate one shift.

    Exercises the same helper the routes call via _reject_shift_against_cutoff:
    one probe shift carrying the submitted late_after_time.
    """

    def _probe(self, school, audience, late_after):
        cutoff = getattr(school, 'shift_absent_after_time' if audience == 'students'
                         else 'emp_shift_absent_after_time')
        return conflicting_shift_late_thresholds(
            school, audience, [SimpleNamespace(late_after_time=late_after)], cutoff)

    def test_student_create_before_cutoff_allowed_equal_or_after_rejected(self):
        school = _school(shift_absent_after_time=time(14, 30))
        self.assertEqual(self._probe(school, 'students', time(14, 29)), [])  # allowed
        self.assertTrue(self._probe(school, 'students', time(14, 30)))       # equal
        self.assertTrue(self._probe(school, 'students', time(15, 0)))        # after

    def test_employee_create_before_cutoff_allowed_equal_or_after_rejected(self):
        school = _school(emp_shift_absent_after_time=time(14, 30))
        self.assertEqual(self._probe(school, 'employees', time(14, 29)), [])
        self.assertTrue(self._probe(school, 'employees', time(14, 30)))
        self.assertTrue(self._probe(school, 'employees', time(15, 0)))

    def test_edit_into_conflict_is_rejected_for_both_audiences(self):
        """An existing valid shift edited so its lateness passes the cutoff."""
        s = _school(shift_absent_after_time=time(10, 0),
                    emp_shift_absent_after_time=time(10, 0))
        self.assertEqual(self._probe(s, 'students', time(9, 0)), [])
        self.assertTrue(self._probe(s, 'students', time(10, 30)))
        self.assertTrue(self._probe(s, 'employees', time(10, 30)))

    def test_no_stored_cutoff_means_no_restriction(self):
        s = _school(shift_absent_after_time=None, emp_shift_absent_after_time=None)
        self.assertEqual(self._probe(s, 'students', time(23, 0)), [])
        self.assertEqual(self._probe(s, 'employees', time(23, 0)), [])

    def test_reactivation_uses_the_shifts_stored_late_time(self):
        """Re-activating passes shift.late_after_time through the same rule."""
        s = _school(shift_absent_after_time=time(10, 0))
        conflicting = _shift('متأخر', time(11, 0), time(11, 30), active=False)
        self.assertTrue(self._probe(s, 'students', conflicting.late_after_time))
        ok = _shift('مبكر', time(7, 0), time(7, 30), active=False)
        self.assertEqual(self._probe(s, 'students', ok.late_after_time), [])


class RouteWiringTest(unittest.TestCase):
    """Structural pins: the guards exist, on both sides, in the right places."""

    def setUp(self):
        self.src = (ROOT / 'app/blueprints/shifts/__init__.py').read_text(
            encoding='utf-8')

    def _func(self, name):
        start = self.src.index(f'def {name}(')
        nxt = self.src.find('\ndef ', start + 1)
        at = self.src.find('\n@', start + 1)
        end = min(x for x in (nxt, at, len(self.src)) if x > 0)
        return self.src[start:end]

    def test_old_start_time_rule_is_gone_for_both_audiences(self):
        self.assertNotIn('AttendanceShift.start_time >= cutoff', self.src)
        self.assertNotIn('EmployeeAttendanceShift.start_time >= cutoff', self.src)

    def test_cutoff_save_routes_validate_against_active_shifts(self):
        for fn, audience in (('update_global_absence', 'students'),
                             ('update_employee_global_absence', 'employees')):
            body = self._func(fn)
            self.assertIn('_reject_cutoff_conflicts', body, fn)
            self.assertIn(f"'{audience}'", body, fn)
            self.assertIn('_active_shifts(school', body, fn)

    def test_create_edit_toggle_guards_exist(self):
        for fn in ('create_shift', 'edit_shift', 'toggle_shift',
                   'toggle_employee_shift', '_emp_shift_times_or_error'):
            self.assertIn('_reject_shift_against_cutoff', self._func(fn), fn)

    def test_employee_create_and_edit_share_one_guard(self):
        """Both employee write routes go through _emp_shift_times_or_error."""
        for fn in ('create_employee_shift', 'edit_employee_shift'):
            self.assertIn('_emp_shift_times_or_error(school)', self._func(fn), fn)

    def test_toggle_guards_only_block_activation(self):
        for fn in ('toggle_shift', 'toggle_employee_shift'):
            body = self._func(fn)
            guard = body.index('_reject_shift_against_cutoff')
            self.assertIn('if not shift.is_active:', body[:guard], fn)
            self.assertIn('activating=True', body, fn)
            # The flip must happen AFTER the guard.
            self.assertLess(guard, body.index('shift.is_active = not shift.is_active'), fn)

    @staticmethod
    def _code_only(src):
        """Strip docstrings and comments — prose explaining that dismissal_time
        is NOT used must not be mistaken for a use of it."""
        import ast
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef, ast.Module)):
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    node.body.pop(0)
        return ast.unparse(tree)

    def test_dismissal_time_is_not_used_by_any_validation(self):
        helpers = (ROOT / 'app/utils/attendance_helpers.py').read_text(encoding='utf-8')
        rule = helpers[helpers.index('def conflicting_shift_late_thresholds('):
                       helpers.index('def determine_check_in_status(')]
        self.assertNotIn('dismissal_time', self._code_only(rule))
        for fn in ('_reject_cutoff_conflicts', '_reject_shift_against_cutoff'):
            self.assertNotIn('dismissal_time', self._code_only(self._func(fn)), fn)
        # And the rule genuinely reads the late threshold instead.
        self.assertIn('effective_shift_late_threshold', self._code_only(rule))

    def test_no_per_shift_absence_time_was_introduced(self):
        """absent_after_time stays the dead legacy student column it was."""
        self.assertNotIn('EmployeeAttendanceShift.absent_after_time', self.src)
        models = (ROOT / 'app/models/__init__.py').read_text(encoding='utf-8')
        emp = models[models.index('class EmployeeAttendanceShift('):
                     models.index('#  0b. FEATURE PACKAGES')]
        self.assertNotIn('absent_after_time = db.Column', emp)

    def test_calculation_paths_untouched_by_this_change(self):
        import subprocess
        out = subprocess.run(
            ['git', 'diff', '--name-only', 'HEAD', '--',
             'app/services/auto_attendance.py',
             'app/blueprints/attendance/__init__.py',
             'app/services/attendance_service.py',
             'app/services/ai_face_ws.py',
             'app/services/payroll.py',
             'app/utils/employee_attendance_helper.py',
             'app/models/__init__.py'],
            cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), '',
                         'a calculation/model file changed in a validation-only task')


if __name__ == '__main__':                                    # pragma: no cover
    unittest.main()

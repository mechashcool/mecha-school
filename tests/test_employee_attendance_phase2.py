# -*- coding: utf-8 -*-
"""Employee attendance Phase 2 — one logic path, correct absence timing.

Phase 2 wires manual attendance, Face ID, the HR report and payroll to the
Phase 1 employee settings/shift resolver, and fixes the absence temporal rule.

Everything here runs WITHOUT a database: the absence classifier and
calculate_employee_stats accept an injected clock, and the resolver is pure, so
the whole rule set is testable with stub objects. The two paths that genuinely
need a session (the Face ID row transition, payroll row loading) are pinned by
structural assertions over the source instead.

Pinned behaviour:
  * past working day with no row            → absent
  * today before the employee cutoff        → not_recorded
  * today at/after the employee cutoff      → absent
  * today with a NULL (or 00:00) cutoff     → not_recorded, never absent
  * future day                              → excluded entirely
  * day before hire_date                    → excluded entirely
  * manual and Face ID derive the SAME present/late from the same inputs
  * payroll reads employee (not student) thresholds and shares the classifier
  * the student resolver path is untouched
"""
import ast
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

from app.utils.attendance_helpers import (
    determine_check_in_status,
    get_effective_attendance_settings,
)
from app.utils.employee_attendance_helper import (
    EmployeeAbsenceClock,
    calculate_employee_stats,
    classify_missing_working_day,
)

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 2)
YESTERDAY = TODAY - timedelta(days=1)
TOMORROW = TODAY + timedelta(days=1)


def _school(**overrides):
    """Stub settings row. Student and employee values differ on purpose, so any
    cross-audience read shows up as a wrong value."""
    values = dict(
        id=1, is_institute=False,
        att_late_threshold=time(7, 30), att_absence_threshold=time(9, 0),
        att_departure_time=time(13, 0), enable_attendance_shifts=False,
        shift_absent_after_time=None,
        emp_att_late_threshold=time(8, 15), emp_att_absence_threshold=time(10, 30),
        emp_att_departure_time=time(15, 45), emp_enable_attendance_shifts=False,
        emp_shift_absent_after_time=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _shift(late=None, dismissal=None):
    return SimpleNamespace(id=7, school_id=1, name='S', start_time=time(7, 0),
                           late_after_time=late, dismissal_time=dismissal,
                           is_active=True)


def _clock(now_time=time(12, 0), cutoff=time(10, 30), today=TODAY):
    return EmployeeAbsenceClock(
        local_today=today,
        local_now=datetime.combine(today, now_time),
        absence_cutoff=cutoff,
    )


def _rec(status, check_in=None, check_out=None):
    return SimpleNamespace(id=1, status=status, check_in=check_in,
                           check_out=check_out, source='manual', device=None,
                           notes=None)


def _employee(hire_date=None):
    return SimpleNamespace(id=1, school_id=1, full_name='E', shift_id=None,
                           hire_date=hire_date)


class AbsenceTimingTest(unittest.TestCase):
    """(5)(6)(7)(8)(9)(10) The temporal rule."""

    def test_past_working_day_without_record_is_absent(self):
        self.assertEqual(
            classify_missing_working_day(YESTERDAY, _clock()), 'absent')
        self.assertEqual(
            classify_missing_working_day(TODAY - timedelta(days=40), _clock()), 'absent')

    def test_today_before_cutoff_is_not_recorded(self):
        c = _clock(now_time=time(7, 0), cutoff=time(10, 30))
        self.assertEqual(classify_missing_working_day(TODAY, c), 'not_recorded')

    def test_today_at_and_after_cutoff_is_absent(self):
        self.assertEqual(
            classify_missing_working_day(TODAY, _clock(time(10, 30), time(10, 30))),
            'absent')
        self.assertEqual(
            classify_missing_working_day(TODAY, _clock(time(23, 59), time(10, 30))),
            'absent')

    def test_today_with_null_cutoff_is_never_absent(self):
        for now in (time(0, 1), time(12, 0), time(23, 59)):
            self.assertEqual(
                classify_missing_working_day(TODAY, _clock(now, None)),
                'not_recorded', f'now={now}')

    def test_future_day_is_excluded(self):
        self.assertIsNone(classify_missing_working_day(TOMORROW, _clock()))
        self.assertIsNone(
            classify_missing_working_day(TODAY + timedelta(days=90), _clock()))

    def test_day_before_hire_date_is_excluded(self):
        hire = TODAY - timedelta(days=5)
        self.assertIsNone(
            classify_missing_working_day(hire - timedelta(days=1), _clock(), hire))
        # On and after the hire date the normal rule resumes.
        self.assertEqual(classify_missing_working_day(hire, _clock(), hire), 'absent')

    def test_null_hire_date_preserves_historical_behaviour(self):
        self.assertEqual(
            classify_missing_working_day(YESTERDAY, _clock(), None), 'absent')

    def test_cutoff_passed_property(self):
        self.assertFalse(_clock(time(9, 0), time(10, 30)).cutoff_passed)
        self.assertTrue(_clock(time(10, 30), time(10, 30)).cutoff_passed)
        self.assertFalse(_clock(time(23, 0), None).cutoff_passed)


class EmployeeStatsTest(unittest.TestCase):
    """(8)(9)(10)(11) calculate_employee_stats with an injected clock."""

    def _days(self, *offsets):
        return [TODAY + timedelta(days=o) for o in offsets]

    def test_mixed_range_counts_each_bucket_once(self):
        days = self._days(-3, -2, -1, 0)
        records = {
            days[0]: _rec('present', time(7, 0), time(15, 0)),
            days[1]: _rec('late', time(9, 0)),
            days[2]: _rec('on_leave'),
        }
        stats = calculate_employee_stats(
            _employee(), records, days, clock=_clock(time(7, 0), time(10, 30)))
        self.assertEqual(stats['present'], 1)
        self.assertEqual(stats['late'], 1)
        self.assertEqual(stats['on_leave'], 1)
        self.assertEqual(stats['absent'], 0)
        self.assertEqual(stats['not_recorded'], 1)      # today, before cutoff
        self.assertEqual(stats['checked_out'], 1)
        self.assertEqual(stats['working_days'], 4)

    def test_today_moves_from_not_recorded_to_absent_at_cutoff(self):
        days = self._days(0)
        before = calculate_employee_stats(_employee(), {}, days,
                                         clock=_clock(time(9, 0), time(10, 30)))
        after = calculate_employee_stats(_employee(), {}, days,
                                         clock=_clock(time(11, 0), time(10, 30)))
        self.assertEqual((before['not_recorded'], before['absent']), (1, 0))
        self.assertEqual((after['not_recorded'], after['absent']), (0, 1))

    def test_future_days_are_dropped_from_the_day_list(self):
        days = self._days(-1, 0, 1, 2)
        stats = calculate_employee_stats(_employee(), {}, days,
                                         clock=_clock(time(9, 0), time(10, 30)))
        self.assertEqual(stats['working_days'], 2)       # yesterday + today only
        self.assertEqual(stats['calendar_working_days'], 4)
        self.assertEqual([d['date'] for d in stats['daily']], days[:2])
        self.assertEqual(stats['absent'], 1)             # yesterday only

    def test_pre_hire_days_are_dropped(self):
        days = self._days(-4, -3, -2, -1)
        hire = TODAY - timedelta(days=2)
        stats = calculate_employee_stats(_employee(hire), {}, days, clock=_clock())
        self.assertEqual(stats['working_days'], 2)
        self.assertEqual(stats['absent'], 2)
        self.assertTrue(all(d['date'] >= hire for d in stats['daily']))

    def test_stored_absent_still_counts_as_absent(self):
        days = self._days(-1)
        stats = calculate_employee_stats(_employee(), {days[0]: _rec('absent')},
                                         days, clock=_clock())
        self.assertEqual(stats['absent'], 1)
        self.assertEqual(stats['not_recorded'], 0)
        self.assertFalse(stats['daily'][0]['is_virtual'])

    def test_not_recorded_and_leave_leave_the_rate_denominator(self):
        days = self._days(-2, -1, 0)
        records = {days[0]: _rec('present', time(7, 0)), days[1]: _rec('on_leave')}
        stats = calculate_employee_stats(
            _employee(), records, days, clock=_clock(time(7, 0), time(10, 30)))
        # 1 present / (3 days - 1 leave - 1 not_recorded) = 100%, not 33%.
        self.assertEqual(stats['rate'], 100.0)

    def test_days_outside_working_days_are_never_classified(self):
        """Weekly days off and employee holidays are excluded upstream by
        get_working_days; this function only ever sees the days it is given, so
        it can never invent an absence on a non-working day."""
        stats = calculate_employee_stats(_employee(), {}, [], clock=_clock())
        self.assertEqual(stats['daily'], [])
        self.assertEqual((stats['absent'], stats['not_recorded']), (0, 0))
        self.assertEqual(stats['rate'], 0.0)


class ManualAndFaceIdAgreeTest(unittest.TestCase):
    """(1)(2)(3)(18) Both paths use the same expression and must agree."""

    def _manual(self, check_in, school, shift=None):
        return determine_check_in_status(check_in, school, shift=shift,
                                        audience='employees')

    def _faceid(self, punch, school, shift=None):
        return determine_check_in_status(punch, school, shift=shift,
                                        audience='employees')

    def test_manual_present_before_threshold_late_after(self):
        s = _school()
        self.assertEqual(self._manual(time(8, 0), s), 'present')
        self.assertEqual(self._manual(time(8, 15), s), 'late')
        self.assertEqual(self._manual(time(9, 30), s), 'late')

    def test_manual_uses_employee_not_student_threshold(self):
        s = _school(att_late_threshold=time(7, 30), emp_att_late_threshold=time(8, 15))
        # 08:00 is past the student threshold but not the employee one.
        self.assertEqual(self._manual(time(8, 0), s), 'present')

    def test_shift_overrides_school_employee_late_and_departure(self):
        s = _school(emp_enable_attendance_shifts=True)
        sh = _shift(late=time(9, 5), dismissal=time(17, 30))
        self.assertEqual(self._manual(time(8, 30), s, sh), 'present')
        self.assertEqual(self._manual(time(9, 5), s, sh), 'late')
        eff = get_effective_attendance_settings(s, 'employees', shift=sh)
        self.assertEqual(eff.late_threshold, time(9, 5))
        self.assertEqual(eff.departure_time, time(17, 30))

    def test_manual_and_faceid_agree_across_inputs(self):
        for school in (_school(),
                       _school(emp_att_late_threshold=None),
                       _school(emp_enable_attendance_shifts=True)):
            for shift in (None, _shift(late=time(9, 5)), _shift(late=None)):
                for t in (time(6, 0), time(8, 14), time(8, 15), time(9, 5), time(18, 0)):
                    self.assertEqual(
                        self._manual(t, school, shift),
                        self._faceid(t, school, shift),
                        f'disagreement at {t} shift={shift and shift.late_after_time}')


class SourceContractTest(unittest.TestCase):
    """Structural pins for the two paths that need a live session."""

    def _src(self, rel):
        return (ROOT / rel).read_text(encoding='utf-8')

    def test_faceid_no_longer_hardcodes_present(self):
        """(3) The unconditional status='present' literal must be gone."""
        src = self._src('app/services/ai_face_ws.py')
        self.assertNotIn("status           = 'present',", src)
        self.assertIn("audience='employees'", src)
        self.assertIn('punch_status', src)

    def test_faceid_treats_a_row_without_check_in_as_a_check_in(self):
        """(4) An existing row with check_in IS NULL (e.g. approved leave) must
        become a real check-in, and the branch must come BEFORE the check_out
        branch so the normal second punch is unaffected."""
        src = self._src('app/services/ai_face_ws.py')
        self.assertIn('elif emp_att.check_in is None:', src)
        guard = src.index('elif emp_att.check_in is None:')
        checkout = src.index('emp_att.check_out = punch_time')
        self.assertLess(guard, checkout, 'check-in recovery must precede check-out')
        branch = src[guard:checkout]
        self.assertIn('emp_att.check_in  = punch_time', branch)
        self.assertIn('emp_att.status    = punch_status', branch)

    def test_payroll_uses_employee_thresholds_not_student_ones(self):
        """(13)(16)(17) compute_attendance must read the employee values via the
        shared resolver, and no longer the student att_* columns."""
        src = self._src('app/services/payroll.py')
        func = src[src.index('def compute_attendance('):src.index('def apply_recurring_components(')]
        self.assertNotIn("getattr(school, 'att_late_threshold', None)", func)
        self.assertNotIn("getattr(school, 'att_departure_time', None)", func)
        self.assertIn('get_effective_attendance_settings(school, \'employees\'', func)
        self.assertIn('late_threshold = effective.late_threshold', func)
        self.assertIn('departure_time = effective.departure_time', func)

    def test_payroll_shares_the_absence_classifier(self):
        """(14) Payroll must not re-implement the temporal rule."""
        src = self._src('app/services/payroll.py')
        func = src[src.index('def compute_attendance('):src.index('def apply_recurring_components(')]
        self.assertIn('classify_missing_working_day(d, clock, hire_date)', func)
        self.assertIn("== 'absent'", func)
        self.assertNotIn('date.today()', func)

    def test_no_employee_auto_absence_job_was_added(self):
        """(20) No scheduler, no automatic absent INSERT."""
        helper = self._src('app/utils/employee_attendance_helper.py')
        self.assertNotIn('EmployeeAttendance(', helper)
        for rel in ('app/services/auto_attendance.py',):
            self.assertNotIn('EmployeeAttendance', self._src(rel))

    def test_employee_paths_use_school_local_date(self):
        """(9)(10) No server date.today() left in the employee absence paths."""
        helper = self._src('app/utils/employee_attendance_helper.py')
        self.assertNotIn('date.today()', helper)
        self.assertIn('get_local_now', helper)

    def test_helper_compiles_and_exports_the_shared_api(self):
        mod = ast.parse(self._src('app/utils/employee_attendance_helper.py'))
        names = {n.name for n in mod.body if isinstance(n, ast.FunctionDef)}
        for fn in ('classify_missing_working_day', 'get_employee_absence_clock',
                   'calculate_employee_stats', 'get_employees_attendance_summary',
                   'get_working_days', 'get_absence_alerts'):
            self.assertIn(fn, names)


class StudentUnchangedTest(unittest.TestCase):
    """(15)(19) The student side must behave exactly as before."""

    def test_student_resolver_and_default_audience_unchanged(self):
        s = _school()
        eff = get_effective_attendance_settings(s, 'students')
        self.assertEqual(eff.late_threshold, time(7, 30))
        self.assertEqual(eff.absence_cutoff, time(9, 0))
        self.assertEqual(eff.departure_time, time(13, 0))
        # No audience kwarg = student behaviour, for every existing call site.
        self.assertEqual(determine_check_in_status(time(7, 29), s), 'present')
        self.assertEqual(determine_check_in_status(time(7, 30), s), 'late')

    def test_student_cutoff_semantics_still_fail_closed(self):
        shift_mode = _school(enable_attendance_shifts=True,
                             shift_absent_after_time=None,
                             att_absence_threshold=time(9, 0))
        self.assertIsNone(
            get_effective_attendance_settings(shift_mode, 'students').absence_cutoff)

    def test_student_engine_files_untouched_by_phase_2(self):
        import subprocess
        out = subprocess.run(
            ['git', 'diff', '--name-only', 'HEAD', '--',
             'app/services/auto_attendance.py',
             'app/blueprints/attendance/__init__.py',
             'app/services/attendance_service.py',
             'app/services/notifications.py',
             'app/blueprints/sections/__init__.py'],
            cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), '', 'student engine file changed')


if __name__ == '__main__':                                    # pragma: no cover
    unittest.main()

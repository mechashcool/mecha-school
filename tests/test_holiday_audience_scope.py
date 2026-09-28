# -*- coding: utf-8 -*-
"""Audience-scoped calendar holidays + effective-dated weekly days off.

Runs against the isolated local PostgreSQL test database only.

School A: legacy weekly_off_days '4,5' (Fri+Sat), one student, one employee.
School B: legacy weekly_off_days '4'   (Fri),     one student, one employee.

Pinned behaviour:
  * a school with no new configuration behaves exactly as before, for both
    audiences, including through get_working_days();
  * applies_to = both / students / employees on holidays, overlap, multi-day,
    inactive, global, and per-school independence on the same date;
  * effective-dated weekly schedules per audience, legacy fallback before the
    first row, explicit empty schedule, several rows, no reinterpretation of
    dates before an effective date;
  * student automatic absence / catch-up / shift web-trigger skip ONLY on
    student days off; employee-only days still mark students absent;
  * employee working days and payroll deductions follow ONLY employee days off;
  * School Calendar routes: validation, preserve-on-omit, no backdating,
    explicit-empty confirmation, future-only deletion, audit logging,
    permissions, cross-school isolation, stored-XSS regression;
  * no legacy value or attendance row is rewritten by any of the above.
"""
import unittest
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import text

from app import create_app
from app.models import (
    db, AcademicYear, AuditLog, Employee, Grade, PayrollSettings, Role,
    SalaryRecord, School, SchoolHoliday, SchoolWeeklyOffSchedule, Section,
    Student, StudentAttendance, User, parent_students,
)
from app.utils.attendance_helpers import (
    get_local_date, get_off_dates, is_holiday_date, resolve_weekly_off_days,
)
from app.utils.employee_attendance_helper import get_working_days

OPTS = {'bypass_tenant_scope': True}
TODAY = date.today()
# A Monday eight weeks ago: every date used below is in the past, so it is a
# stable "historical" range for helpers, auto-absence and payroll.
BASE = TODAY - timedelta(days=TODAY.weekday()) - timedelta(weeks=8)
MON, TUE, WED, THU, FRI, SAT, SUN = (BASE + timedelta(days=i) for i in range(7))


def _d(offset):
    return BASE + timedelta(days=offset)


class HolidayAudienceScopeTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    # ── Fixture ──────────────────────────────────────────────────────────────

    def setUp(self):
        self.app.config['AUTO_ABSENCE_OUTBOX_ENABLED'] = False
        self.suffix = uuid4().hex[:10]
        self.ids = {'global_holidays': []}
        with self.app.app_context():
            roles = {n: Role.query.filter_by(name=n).first()
                     for n in ('school_admin', 'parent', 'super_admin')}
            for n, r in roles.items():
                self.assertIsNotNone(r, f'seed role {n} before running')

            for tag, weekly in (('a', '4,5'), ('b', '4')):
                school = School(
                    school_name=f'Hol {tag} {self.suffix}',
                    code=f'HL{tag.upper()}{self.suffix[:6]}',
                    capacity=0, is_active=True, timezone='Asia/Baghdad',
                    att_late_threshold=time(7, 30),
                    att_absence_threshold=time(0, 1),
                    att_departure_time=time(13, 0),
                    weekly_off_days=weekly,
                    enable_attendance_shifts=False)
                db.session.add(school)
                db.session.flush()
                year = AcademicYear(school_id=school.id, name=f'Y {tag} {self.suffix}',
                                    start_date=TODAY - timedelta(days=400),
                                    end_date=TODAY + timedelta(days=200),
                                    is_current=True)
                db.session.add(year)
                db.session.flush()
                grade = Grade(school_id=school.id, academic_year_id=year.id,
                              name=f'G{tag}{self.suffix[:4]}')
                db.session.add(grade)
                db.session.flush()
                section = Section(school_id=school.id, academic_year_id=year.id,
                                  grade_id=grade.id, name=f'S{tag}{self.suffix[:4]}',
                                  capacity=30)
                db.session.add(section)
                db.session.flush()
                student = Student(student_id=f'HL-{tag.upper()}-{self.suffix}',
                                  full_name=f'Student {tag} {self.suffix}',
                                  date_of_birth=date(2015, 1, 1), gender='male',
                                  school_id=school.id, academic_year_id=year.id,
                                  section_id=section.id, status='active')
                db.session.add(student)
                db.session.flush()
                admin = User(username=f'hl_ad_{tag}_{self.suffix}',
                             email=f'hl_ad_{tag}_{self.suffix}@example.test',
                             full_name=f'Admin {tag}',
                             role_id=roles['school_admin'].id,
                             school_id=school.id, is_active=True)
                admin.set_password('Password123')
                parent = User(username=f'hl_p_{tag}_{self.suffix}',
                              email=f'hl_p_{tag}_{self.suffix}@example.test',
                              full_name=f'Parent {tag}', role_id=roles['parent'].id,
                              school_id=school.id, is_active=True)
                parent.set_password('Password123')
                db.session.add_all([admin, parent])
                db.session.flush()
                db.session.execute(parent_students.insert().values(
                    user_id=parent.id, student_id=student.id))
                employee = Employee(employee_id=f'HL-E-{tag}-{self.suffix}',
                                    full_name=f'Employee {tag}',
                                    school_id=school.id, base_salary=1000,
                                    status='active')
                db.session.add(employee)
                db.session.flush()
                self.ids.update({
                    f'school_{tag}': school.id, f'year_{tag}': year.id,
                    f'grade_{tag}': grade.id, f'section_{tag}': section.id,
                    f'student_{tag}': student.id, f'admin_{tag}': admin.id,
                    f'parent_{tag}': parent.id, f'employee_{tag}': employee.id,
                })

            superu = User(username=f'hl_su_{self.suffix}',
                          email=f'hl_su_{self.suffix}@example.test',
                          full_name='Super', role_id=roles['super_admin'].id,
                          school_id=None, is_active=True)
            superu.set_password('Password123')
            db.session.add(superu)
            db.session.flush()
            self.ids['super'] = superu.id
            db.session.commit()

        self.client = self.app.test_client()

    def tearDown(self):
        self.app.config.pop('AUTO_ABSENCE_OUTBOX_ENABLED', None)
        with self.app.app_context():
            sids = [self.ids['school_a'], self.ids['school_b']]
            uids = [self.ids[k] for k in ('admin_a', 'admin_b', 'parent_a',
                                          'parent_b', 'super')]
            stids = [self.ids['student_a'], self.ids['student_b']]
            for sql, params in (
                ('DELETE FROM audit_logs WHERE school_id = ANY(:s) OR user_id = ANY(:u)',
                 {'s': sids, 'u': uids}),
                ('DELETE FROM school_weekly_off_schedules WHERE school_id = ANY(:s)',
                 {'s': sids}),
                ('DELETE FROM school_holidays WHERE school_id = ANY(:s) OR id = ANY(:g)',
                 {'s': sids, 'g': self.ids['global_holidays']}),
                ('DELETE FROM notification_outbox WHERE school_id = ANY(:s)', {'s': sids}),
                ('DELETE FROM push_notifications WHERE school_id = ANY(:s)', {'s': sids}),
                ('DELETE FROM notifications WHERE school_id = ANY(:s)', {'s': sids}),
                ('DELETE FROM student_attendance WHERE school_id = ANY(:s)', {'s': sids}),
                ('DELETE FROM employee_attendance WHERE school_id = ANY(:s)', {'s': sids}),
                ('DELETE FROM payroll_items WHERE school_id = ANY(:s)', {'s': sids}),
                ('DELETE FROM salary_records WHERE school_id = ANY(:s)', {'s': sids}),
                ('DELETE FROM payroll_settings WHERE school_id = ANY(:s)', {'s': sids}),
                ('DELETE FROM parent_students WHERE student_id = ANY(:s)', {'s': stids}),
                ('DELETE FROM employees WHERE school_id = ANY(:s)', {'s': sids}),
            ):
                db.session.execute(text(sql), params)
            for model, keys in ((Student, ['student_a', 'student_b']),
                                (User, ['admin_a', 'admin_b', 'parent_a',
                                        'parent_b', 'super']),
                                (Section, ['section_a', 'section_b']),
                                (Grade, ['grade_a', 'grade_b']),
                                (AcademicYear, ['year_a', 'year_b']),
                                (School, ['school_a', 'school_b'])):
                for key in keys:
                    row = db.session.get(model, self.ids[key], execution_options=OPTS)
                    if row is not None:
                        db.session.delete(row)
                db.session.flush()
            db.session.commit()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _school(self, tag):
        return db.session.get(School, self.ids[f'school_{tag}'], execution_options=OPTS)

    def _year(self, tag):
        return db.session.get(AcademicYear, self.ids[f'year_{tag}'],
                              execution_options=OPTS)

    def _holiday(self, tag, start, end=None, applies_to='both', active=True,
                 name=None):
        holiday = SchoolHoliday(
            school_id=None if tag is None else self.ids[f'school_{tag}'],
            name=name or f'H {applies_to} {start}', start_date=start,
            end_date=end or start, holiday_type='official',
            applies_to=applies_to, is_active=active)
        db.session.add(holiday)
        db.session.commit()
        if tag is None:
            self.ids['global_holidays'].append(holiday.id)
        return holiday.id

    def _schedule(self, tag, audience, off_days, effective_from):
        row = SchoolWeeklyOffSchedule(school_id=self.ids[f'school_{tag}'],
                                      audience=audience, off_days=off_days,
                                      effective_from=effective_from)
        db.session.add(row)
        db.session.commit()
        return row.id

    def _off(self, tag, day, audience):
        school = self._school(tag)
        return is_holiday_date(day, school.id, school, audience=audience)

    def _login(self, user_id, active_school=None):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True
            if active_school is not None:
                sess['active_school_id'] = active_school

    def _rows(self, model, **filters):
        return model.query.execution_options(bypass_tenant_scope=True).filter_by(
            **filters).all()

    def _student_att(self, tag, on_date):
        return self._rows(StudentAttendance, student_id=self.ids[f'student_{tag}'],
                          date=on_date)

    # ── 1. Legacy behaviour is untouched ─────────────────────────────────────

    def test_legacy_school_same_result_for_every_audience(self):
        with self.app.app_context():
            school = self._school('a')
            for i in range(14):
                day = _d(i)
                expected = day.weekday() in (4, 5)
                for audience in (None, 'students', 'employees'):
                    self.assertEqual(self._off('a', day, audience), expected,
                                     (day, audience))
            self.assertEqual(
                get_working_days(_d(0), _d(13), school),
                [_d(i) for i in range(14) if _d(i).weekday() not in (4, 5)])

    def test_existing_holiday_without_scope_applies_to_both(self):
        with self.app.app_context():
            db.session.execute(text(
                "INSERT INTO school_holidays (school_id, name, start_date, end_date, "
                "holiday_type, is_active) VALUES (:s, 'legacy', :d, :d, 'official', true)"),
                {'s': self.ids['school_a'], 'd': TUE})
            db.session.commit()
            row = self._rows(SchoolHoliday, school_id=self.ids['school_a'])[0]
            self.assertEqual(row.applies_to, 'both')
            for audience in (None, 'students', 'employees'):
                self.assertTrue(self._off('a', TUE, audience), audience)
            self.assertNotIn(TUE, get_working_days(MON, SUN, self._school('a')))

    # ── 2. Holiday scopes ────────────────────────────────────────────────────

    def test_holiday_scopes_overlap_multiday_inactive(self):
        with self.app.app_context():
            self._holiday('a', MON, applies_to='both')
            self._holiday('a', TUE, applies_to='students')
            self._holiday('a', WED, applies_to='employees')
            self._holiday('a', THU, applies_to='employees', active=False)
            self._holiday('a', _d(7), _d(9), applies_to='students')   # Mon–Wed, inclusive
            self._holiday('a', _d(10), applies_to='students')          # overlap on Thu
            self._holiday('a', _d(10), applies_to='employees')
            expect = {
                MON: (True, True), TUE: (True, False), WED: (False, True),
                THU: (False, False), _d(7): (True, False), _d(8): (True, False),
                _d(9): (True, False), _d(10): (True, True),
            }
            for day, (stu, emp) in expect.items():
                self.assertEqual(self._off('a', day, 'students'), stu, ('students', day))
                self.assertEqual(self._off('a', day, 'employees'), emp, ('employees', day))
                self.assertEqual(self._off('a', day, None), stu or emp, ('legacy', day))

    def test_same_date_different_scope_per_school_and_global(self):
        with self.app.app_context():
            self._holiday('a', TUE, applies_to='students')
            self._holiday('b', TUE, applies_to='employees')
            self._holiday(None, _d(14), applies_to='employees')     # global, Monday
            self.assertTrue(self._off('a', TUE, 'students'))
            self.assertFalse(self._off('a', TUE, 'employees'))
            self.assertFalse(self._off('b', TUE, 'students'))
            self.assertTrue(self._off('b', TUE, 'employees'))
            for tag in ('a', 'b'):
                self.assertFalse(self._off(tag, _d(14), 'students'), tag)
                self.assertTrue(self._off(tag, _d(14), 'employees'), tag)

    def test_range_helper_matches_single_day_helper(self):
        with self.app.app_context():
            self._holiday('a', TUE, applies_to='students')
            self._holiday('a', _d(8), _d(16), applies_to='employees')
            self._holiday(None, _d(20), applies_to='both')
            self._schedule('a', 'employees', '4', _d(7))
            self._schedule('a', 'students', '', _d(21))
            school = self._school('a')
            for audience in (None, 'students', 'employees'):
                ranged = get_off_dates(_d(0), _d(34), school, audience=audience)
                single = {_d(i) for i in range(35)
                          if is_holiday_date(_d(i), school.id, school, audience=audience)}
                self.assertEqual(ranged, single, audience)

    # ── 3. Effective-dated weekly schedules ─────────────────────────────────

    def test_student_fri_sat_employee_fri(self):
        with self.app.app_context():
            school = self._school('a')                      # legacy '4,5'
            before = get_working_days(_d(0), _d(6), school)
            self._schedule('a', 'employees', '4', _d(7))
            self.assertEqual(get_working_days(_d(0), _d(6), self._school('a')),
                             before, 'dates before effective_from reinterpreted')
            self.assertTrue(self._off('a', SAT, 'employees'))          # before: legacy
            self.assertTrue(self._off('a', _d(12), 'students'))        # Sat after
            self.assertFalse(self._off('a', _d(12), 'employees'))      # Sat after
            self.assertTrue(self._off('a', _d(11), 'employees'))       # Fri after
            working = get_working_days(_d(7), _d(13), self._school('a'))
            self.assertIn(_d(12), working)
            self.assertNotIn(_d(11), working)
            self.assertEqual(self._school('a').weekly_off_days, '4,5')

    def test_employee_fri_sat_student_fri(self):
        with self.app.app_context():
            self._schedule('b', 'employees', '4,5', _d(0))      # legacy '4'
            self.assertFalse(self._off('b', SAT, 'students'))
            self.assertTrue(self._off('b', SAT, 'employees'))
            self.assertTrue(self._off('b', FRI, 'students'))
            self.assertNotIn(SAT, get_working_days(MON, SUN, self._school('b')))

    def test_explicit_empty_and_multiple_rows(self):
        with self.app.app_context():
            self._schedule('a', 'students', '', _d(7))
            self._schedule('a', 'employees', '4', _d(7))
            self._schedule('a', 'employees', '5', _d(14))
            self.assertTrue(self._off('a', FRI, 'students'))            # legacy
            self.assertFalse(self._off('a', _d(11), 'students'))        # '' = none
            self.assertFalse(self._off('a', _d(12), 'students'))
            self.assertTrue(self._off('a', _d(11), 'employees'))
            self.assertFalse(self._off('a', _d(12), 'employees'))
            self.assertFalse(self._off('a', _d(18), 'employees'))       # Fri, row '5'
            self.assertTrue(self._off('a', _d(19), 'employees'))        # Sat, row '5'
            school = self._school('a')
            self.assertEqual(resolve_weekly_off_days(school, None, _d(18)),
                             frozenset({5}))                            # union

    def test_weekly_and_holiday_overlap(self):
        with self.app.app_context():
            self._schedule('a', 'employees', '4', _d(0))
            self._holiday('a', SAT, applies_to='students')    # student weekly day anyway
            self._holiday('a', FRI, applies_to='students')    # employee weekly day anyway
            self.assertTrue(self._off('a', SAT, 'students'))
            self.assertFalse(self._off('a', SAT, 'employees'))
            self.assertTrue(self._off('a', FRI, 'employees'))

    # ── 4. Student automatic absence ────────────────────────────────────────

    def test_auto_absence_skips_only_student_days_off(self):
        from app.blueprints.attendance import _run_auto_absent
        with self.app.app_context():
            self._holiday('a', _d(14), applies_to='students')
            self._holiday('a', _d(15), applies_to='employees')
            self._schedule('b', 'students', '0', _d(0))       # Mondays off for students
            with patch('app.blueprints.attendance._notify_absent_parents') as notify:
                res = _run_auto_absent(self._school('a'), self._year('a'),
                                       self._school('a'), target_date=_d(14))
                self.assertTrue(res['holiday'])
                self.assertEqual(self._student_att('a', _d(14)), [])
                res = _run_auto_absent(self._school('a'), self._year('a'),
                                       self._school('a'), target_date=_d(15))
                self.assertFalse(res['holiday'])
                self.assertEqual(res['count'], 1)
                self.assertEqual([r.status for r in self._student_att('a', _d(15))],
                                 ['absent'])
                self.assertEqual(notify.call_count, 1)
                res = _run_auto_absent(self._school('b'), self._year('b'),
                                       self._school('b'), target_date=_d(14))
                self.assertTrue(res['holiday'])
                self.assertEqual(self._student_att('b', _d(14)), [])
                self.assertEqual(notify.call_count, 1)
            self.assertIn(_d(14), get_working_days(_d(14), _d(14), self._school('b')))

    def test_catch_up_uses_student_audience(self):
        from app.services.auto_attendance import _catchup_previous_day
        with self.app.app_context():
            self._holiday('a', _d(14), applies_to='students')
            self._holiday('b', _d(14), applies_to='employees')
            midnight = datetime.combine(_d(15), time(0, 5))
            with patch('app.blueprints.attendance._notify_absent_parents') as notify:
                for tag in ('a', 'b'):
                    school = self._school(tag)
                    _catchup_previous_day(school, school.school_name, midnight, _d(15))
                self.assertEqual(self._student_att('a', _d(14)), [])
                self.assertEqual([r.status for r in self._student_att('b', _d(14))],
                                 ['absent'])
                self.assertEqual(notify.call_count, 1)

    def test_shift_web_trigger_holiday_flag_is_student_scoped(self):
        from app.services.auto_attendance import run_school_shift_auto_absent_now
        with self.app.app_context():
            self._holiday('a', _d(14), applies_to='students')
            self._holiday('b', _d(14), applies_to='employees')
            with patch('app.utils.attendance_helpers.get_local_date',
                       return_value=_d(14)), \
                 patch('app.blueprints.attendance._notify_absent_parents'):
                res_a = run_school_shift_auto_absent_now(
                    self._school('a'), self._year('a'), self._school('a'))
                res_b = run_school_shift_auto_absent_now(
                    self._school('b'), self._year('b'), self._school('b'))
            self.assertTrue(res_a['holiday'])
            self.assertFalse(res_b['holiday'])

    def test_student_daily_detail_rows_use_student_days_off(self):
        """Screen + PDF + Excel single-student detail share _daily_detail_rows."""
        from app.blueprints.attendance import _daily_detail_rows
        with self.app.app_context():
            self._holiday('a', TUE, applies_to='students')
            self._holiday('a', WED, applies_to='employees')
            self._schedule('a', 'students', '0,4,5', _d(0))      # + Mondays for students
            rows = _daily_detail_rows([], MON, SUN, self._school('a'), TODAY)
            self.assertEqual([r['date'] for r in rows], [WED, THU, SUN])
            self.assertTrue(all(r['has_record'] is False for r in rows))

    # ── 5. Employee working days and payroll ────────────────────────────────

    def test_payroll_deductions_follow_employee_days_only(self):
        from app.services.payroll import compute_attendance
        with self.app.app_context():
            month_start = (TODAY.replace(day=1) - timedelta(days=45)).replace(day=1)
            month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            self._schedule('a', 'employees', '4', month_start)       # Fri only
            weekdays = [month_start + timedelta(days=i)
                        for i in range((month_end - month_start).days + 1)]
            mondays = [d for d in weekdays if d.weekday() == 0]
            self._holiday('a', mondays[0], applies_to='students')    # still a working day
            self._holiday('a', mondays[1], applies_to='employees')   # not a working day
            expected = [d for d in weekdays
                        if d.weekday() != 4 and d != mondays[1]]
            school = self._school('a')
            self.assertEqual(get_working_days(month_start, month_end, school), expected)
            settings = PayrollSettings(school_id=school.id,
                                       attendance_deduction_enabled=True,
                                       absence_method='fixed',
                                       absence_fixed_amount=Decimal('10'),
                                       monthly_working_days=26)
            record = SalaryRecord(employee_id=self.ids['employee_a'],
                                  school_id=school.id,
                                  academic_year_id=self.ids['year_a'],
                                  month=month_start.month, year=month_start.year,
                                  base_salary=Decimal('1000'),
                                  net_salary=Decimal('1000'))
            stats = compute_attendance(record, settings, school)
            db.session.expunge_all()
            self.assertEqual(stats['absence_days'], len(expected))
            self.assertEqual(stats['absence_deduction'], Decimal('10') * len(expected))
            self.assertEqual(self._rows(SalaryRecord, school_id=school.id), [])

    def test_payroll_recalculate_draft_follows_schedule_locked_unchanged(self):
        from app.models import PayrollItem
        with self.app.app_context():
            month_start = (TODAY.replace(day=1) - timedelta(days=45)).replace(day=1)
            month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            prev_start = (month_start - timedelta(days=1)).replace(day=1)
            self._schedule('a', 'employees', '4', month_start)       # Fri only
            settings = PayrollSettings.get_or_create(self.ids['school_a'])
            settings.attendance_deduction_enabled = True
            settings.absence_method = 'fixed'
            settings.absence_fixed_amount = Decimal('10')
            common = dict(employee_id=self.ids['employee_a'],
                          school_id=self.ids['school_a'],
                          academic_year_id=self.ids['year_a'],
                          base_salary=Decimal('1000'))
            draft = SalaryRecord(month=month_start.month, year=month_start.year,
                                 net_salary=Decimal('1000'), status='draft', **common)
            locked = SalaryRecord(month=prev_start.month, year=prev_start.year,
                                  net_salary=Decimal('990'), deductions=Decimal('10'),
                                  absence_days=1, status='approved', **common)
            db.session.add_all([draft, locked])
            db.session.flush()
            db.session.add(PayrollItem(salary_record_id=locked.id,
                                       school_id=self.ids['school_a'],
                                       academic_year_id=self.ids['year_a'],
                                       name='خصم غياب', item_type='deduction',
                                       amount=Decimal('10'), source='attendance'))
            db.session.commit()
            draft_id, locked_id = draft.id, locked.id
            working = [month_start + timedelta(days=i)
                       for i in range((month_end - month_start).days + 1)
                       if (month_start + timedelta(days=i)).weekday() != 4]

        self._login(self.ids['admin_a'])
        for rec_id in (draft_id, locked_id):
            resp = self._post(f'/salaries/{rec_id}/recalculate', {})
            self.assertEqual(resp.status_code, 302, rec_id)

        with self.app.app_context():
            draft = db.session.get(SalaryRecord, draft_id, execution_options=OPTS)
            self.assertEqual(draft.absence_days, len(working))      # Saturdays counted
            self.assertEqual(draft.deductions, Decimal('10') * len(working))
            locked = db.session.get(SalaryRecord, locked_id, execution_options=OPTS)
            self.assertEqual((locked.status, locked.absence_days, locked.deductions,
                              locked.net_salary),
                             ('approved', 1, Decimal('10'), Decimal('990')))
            self.assertEqual([(i.name, i.amount) for i in locked.items],
                             [('خصم غياب', Decimal('10'))])

    # ── 6. School Calendar routes ────────────────────────────────────────────

    def _post(self, url, data):
        return self.client.post(url, data=data, follow_redirects=False)

    def test_add_and_edit_holiday_applies_to(self):
        self._login(self.ids['admin_a'])
        with self.app.app_context():
            future = get_local_date(self._school('a')) + timedelta(days=10)
        # Future dates: a started holiday's scope is immutable (tested below).
        base = {'name': 'Scope', 'start_date': future.isoformat(),
                'end_date': future.isoformat(), 'holiday_type': 'official'}
        self.assertEqual(self._post('/school-calendar/add', base).status_code, 302)
        self._post('/school-calendar/add', dict(base, name='Stu', applies_to='students'))
        self._post('/school-calendar/add', dict(base, name='Bad', applies_to='teachers'))
        with self.app.app_context():
            rows = {h.name: h for h in self._rows(SchoolHoliday,
                                                  school_id=self.ids['school_a'])}
            self.assertEqual(rows['Scope'].applies_to, 'both')
            self.assertEqual(rows['Stu'].applies_to, 'students')
            self.assertNotIn('Bad', rows)
            hid = rows['Stu'].id
        url = f'/school-calendar/{hid}/edit'
        self._post(url, dict(base, name='Stu2'))                         # field omitted
        self._post(url, dict(base, name='Stu3', applies_to=''))          # "keep current"
        with self.app.app_context():
            h = db.session.get(SchoolHoliday, hid, execution_options=OPTS)
            self.assertEqual((h.name, h.applies_to), ('Stu3', 'students'))
        self._post(url, dict(base, name='Stu4', applies_to='nobody'))
        with self.app.app_context():
            h = db.session.get(SchoolHoliday, hid, execution_options=OPTS)
            self.assertEqual((h.name, h.applies_to), ('Stu3', 'students'))
        self._post(url, dict(base, name='Emp', applies_to='employees'))
        with self.app.app_context():
            h = db.session.get(SchoolHoliday, hid, execution_options=OPTS)
            self.assertEqual((h.name, h.applies_to), ('Emp', 'employees'))
            logs = AuditLog.query.execution_options(bypass_tenant_scope=True).filter_by(
                school_id=self.ids['school_a'], resource='school_holiday').all()
            self.assertGreaterEqual(len(logs), 4)
        self.assertEqual(self.client.get(url).status_code, 302)          # no GET page

    LOCK_MSG = ('لا يمكن تغيير نطاق عطلة بدأت أو انتهت؛ لأن ذلك قد يؤثر في '
                'تقارير الحضور ومسودات الرواتب السابقة.')

    EDIT_LOCK_MSG = ('لا يمكن تغيير تواريخ أو نوع أو نطاق عطلة بدأت أو انتهت. '
                     'يمكن تعديل الاسم والملاحظات فقط.')
    DELETE_LOCK_MSG = ('لا يمكن حذف عطلة بدأت أو انتهت؛ حفاظًا على صحة سجلات '
                       'الحضور وتقارير الرواتب السابقة.')

    def _started_pair(self):
        """(past holiday id, today holiday id) for school A."""
        today = get_local_date(self._school('a'))
        return (self._holiday('a', _d(1), _d(2), name='Past'),
                self._holiday('a', today, today, name='Today'))

    def test_started_holiday_toggle_allowed_delete_rejected(self):
        with self.app.app_context():
            hids = self._started_pair()
            before = {h: self._holiday_row(h) for h in hids}
            logs_before = self._holiday_edit_logs()
        self._login(self.ids['admin_a'])
        for hid in hids:
            resp = self.client.post(f'/school-calendar/{hid}/delete',
                                    follow_redirects=True)
            self.assertIn(self.DELETE_LOCK_MSG, resp.get_data(as_text=True))
            self._post(f'/school-calendar/{hid}/toggle', {})               # deactivate
        with self.app.app_context():
            for hid in hids:
                row = self._holiday_row(hid)
                self.assertFalse(row[8])                                   # is_active
                # Everything except is_active / updated_at is unchanged.
                self.assertEqual(row[:8], before[hid][:8])
            self.assertEqual(self._holiday_edit_logs(), logs_before + 2)   # toggles audited
        for hid in hids:
            self._post(f'/school-calendar/{hid}/toggle', {})               # reactivate
        self._login(self.ids['parent_a'])
        for hid in hids:
            self.assertEqual(self._post(f'/school-calendar/{hid}/delete', {}).status_code, 403)
            self.assertEqual(self._post(f'/school-calendar/{hid}/toggle', {}).status_code, 403)
        with self.app.app_context():
            for hid in hids:
                row = self._holiday_row(hid)
                self.assertTrue(row[8])                                    # still exists, active
                self.assertEqual(row[:8], before[hid][:8])
            self.assertEqual(self._holiday_edit_logs(), logs_before + 4)

    def test_started_holiday_name_and_notes_only(self):
        with self.app.app_context():
            hids = self._started_pair()
            before = {h: self._holiday_row(h) for h in hids}
            logs_before = self._holiday_edit_logs()
        self._login(self.ids['admin_a'])
        for hid in hids:
            # Disabled controls are not submitted: only name + notes arrive.
            self._post(f'/school-calendar/{hid}/edit',
                       {'name': f'Renamed {hid}', 'notes': 'new note'})
        with self.app.app_context():
            for hid in hids:
                row = self._holiday_row(hid)
                self.assertEqual((row[2], row[7]), (f'Renamed {hid}', 'new note'))
                self.assertEqual(row[:2] + row[3:7] + row[8:9],
                                 before[hid][:2] + before[hid][3:7] + before[hid][8:9])
            self.assertEqual(self._holiday_edit_logs(), logs_before + 2)

    def test_started_holiday_forged_protected_field_rejects_whole_request(self):
        with self.app.app_context():
            past, today_hid = self._started_pair()
            before = {h: self._holiday_row(h) for h in (past, today_hid)}
            logs_before = self._holiday_edit_logs()
        forged = [
            {'start_date': _d(0).isoformat()},
            {'end_date': _d(3).isoformat()},
            {'holiday_type': 'emergency'},
            {'academic_year_id': str(self.ids['year_a'])},
            {'applies_to': 'employees', 'holiday_type': 'summer'},
        ]
        self._login(self.ids['admin_a'])
        for hid in (past, today_hid):
            for extra in forged:
                resp = self.client.post(
                    f'/school-calendar/{hid}/edit',
                    data=dict({'name': 'Hijack', 'notes': 'x'}, **extra),
                    follow_redirects=True)
                self.assertIn(self.EDIT_LOCK_MSG, resp.get_data(as_text=True),
                              (hid, extra))
        # Ownership change (only a super admin could express it).
        self._login(self.ids['super'], active_school=self.ids['school_a'])
        resp = self.client.post(f'/school-calendar/{past}/edit',
                                data={'name': 'Hijack', 'is_global': 'on'},
                                follow_redirects=True)
        self.assertIn(self.EDIT_LOCK_MSG, resp.get_data(as_text=True))
        with self.app.app_context():
            for hid in (past, today_hid):
                self.assertEqual(self._holiday_row(hid), before[hid])
            self.assertEqual(self._holiday_edit_logs(), logs_before)

    def test_future_holiday_edit_toggle_delete_still_work(self):
        with self.app.app_context():
            fut = get_local_date(self._school('a')) + timedelta(days=5)
            hid = self._holiday('a', fut, fut, name='Future')
            logs_before = self._holiday_edit_logs()
        # Cross-school admin is still blocked by ownership, not by the new rule.
        self._login(self.ids['admin_b'])
        self._post(f'/school-calendar/{hid}/toggle', {})
        self._post(f'/school-calendar/{hid}/delete', {})
        with self.app.app_context():
            self.assertTrue(db.session.get(SchoolHoliday, hid,
                                           execution_options=OPTS).is_active)
        self._login(self.ids['admin_a'])
        self._post(f'/school-calendar/{hid}/edit',
                   self._edit_form(fut, fut + timedelta(days=1), applies_to='students'))
        self._post(f'/school-calendar/{hid}/toggle', {})
        with self.app.app_context():
            h = db.session.get(SchoolHoliday, hid, execution_options=OPTS)
            self.assertEqual((h.end_date, h.holiday_type, h.applies_to, h.is_active),
                             (fut + timedelta(days=1), 'summer', 'students', False))
        self._post(f'/school-calendar/{hid}/delete', {})
        with self.app.app_context():
            self.assertIsNone(db.session.get(SchoolHoliday, hid, execution_options=OPTS))
            self.assertEqual(self._holiday_edit_logs(), logs_before + 3)

    def test_calendar_page_freezes_started_holiday_controls(self):
        with self.app.app_context():
            past, today_hid = self._started_pair()
            fut = get_local_date(self._school('a')) + timedelta(days=5)
            future = self._holiday('a', fut, fut, name='Future')
        self._login(self.ids['admin_a'])
        html = self.client.get('/school-calendar/').get_data(as_text=True)
        for hid in (past, today_hid):
            self.assertIn(f'/school-calendar/{hid}/toggle', html)       # toggle restored
            self.assertNotIn(f'/school-calendar/{hid}/delete', html)
            self.assertRegex(html, rf'data-holiday-id="{hid}"[^>]*data-started="1"')
        self.assertIn(f'/school-calendar/{future}/toggle', html)
        self.assertIn(f'/school-calendar/{future}/delete', html)
        self.assertRegex(html, rf'data-holiday-id="{future}"[^>]*data-started="0"')
        self.assertIn('يمكن تعديل الاسم والملاحظات، وتفعيل العطلة أو تعطيلها', html)
        for field in ('editStartDate', 'editEndDate', 'editType', 'editYear',
                      'editAppliesTo', 'editGlobal'):
            self.assertIn(f"'{field}'", html)

    def _holiday_row(self, hid):
        h = db.session.get(SchoolHoliday, hid, execution_options=OPTS)
        return (h.school_id, h.academic_year_id, h.name, h.start_date, h.end_date,
                h.holiday_type, h.applies_to, h.notes, h.is_active, h.updated_at)

    def _holiday_edit_logs(self):
        return AuditLog.query.execution_options(bypass_tenant_scope=True).filter_by(
            school_id=self.ids['school_a'], resource='school_holiday').count()

    def _edit_form(self, start, end, **extra):
        return dict({'name': 'Edited', 'start_date': start.isoformat(),
                     'end_date': end.isoformat(), 'holiday_type': 'summer',
                     'notes': 'changed'}, **extra)

    def test_started_holiday_scope_change_rejected_atomically(self):
        with self.app.app_context():
            hid = self._holiday('a', _d(1), _d(2), applies_to='students', name='Past')
            before = self._holiday_row(hid)
            logs_before = self._holiday_edit_logs()
        self._login(self.ids['admin_a'])
        resp = self.client.post(f'/school-calendar/{hid}/edit',
                                data=self._edit_form(_d(3), _d(4),
                                                     applies_to='employees'),
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        # Several protected fields changed at once → the general started-holiday message.
        self.assertIn(self.EDIT_LOCK_MSG, resp.get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(self._holiday_row(hid), before)
            self.assertEqual(self._holiday_edit_logs(), logs_before)
        # Permission unchanged on the edited route.
        self._login(self.ids['parent_a'])
        resp = self._post(f'/school-calendar/{hid}/edit',
                          self._edit_form(_d(1), _d(2), applies_to='employees'))
        self.assertEqual(resp.status_code, 403)
        with self.app.app_context():
            self.assertEqual(self._holiday_row(hid), before)

    def test_holiday_starting_today_scope_is_immutable(self):
        with self.app.app_context():
            today = get_local_date(self._school('a'))
            hid = self._holiday('a', today, today + timedelta(days=2),
                                applies_to='both', name='Today')
            before = self._holiday_row(hid)
        self._login(self.ids['admin_a'])
        # Only the audience differs → the specific scope-lock message.
        resp = self.client.post(f'/school-calendar/{hid}/edit',
                                data=self._edit_form(today, today + timedelta(days=2),
                                                     holiday_type='official',
                                                     applies_to='students'),
                                follow_redirects=True)
        self.assertIn(self.LOCK_MSG, resp.get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(self._holiday_row(hid), before)

    def test_future_holiday_scope_can_change_but_not_moved_into_past(self):
        with self.app.app_context():
            today = get_local_date(self._school('a'))
            fut = today + timedelta(days=5)
            hid = self._holiday('a', fut, fut, applies_to='both', name='Future')
            hid2 = self._holiday('a', fut, fut, applies_to='both', name='Future2')
            before2 = self._holiday_row(hid2)
        self._login(self.ids['admin_a'])
        self._post(f'/school-calendar/{hid}/edit',
                   self._edit_form(fut, fut, applies_to='employees'))
        resp = self.client.post(f'/school-calendar/{hid2}/edit',
                                data=self._edit_form(today, fut, applies_to='students'),
                                follow_redirects=True)
        self.assertIn(self.LOCK_MSG, resp.get_data(as_text=True))
        with self.app.app_context():
            h = db.session.get(SchoolHoliday, hid, execution_options=OPTS)
            self.assertEqual((h.name, h.applies_to, h.holiday_type),
                             ('Edited', 'employees', 'summer'))
            self.assertEqual(self._holiday_row(hid2), before2)

    def test_started_holiday_edit_without_scope_preserves_it(self):
        with self.app.app_context():
            hid = self._holiday('a', _d(1), _d(2), applies_to='employees', name='Past')
            hid2 = self._holiday('a', _d(5), _d(5), applies_to='students', name='Past2')
        self._login(self.ids['admin_a'])
        # Protected fields resubmitted with their stored values are not changes.
        self._post(f'/school-calendar/{hid}/edit',
                   self._edit_form(_d(1), _d(2), holiday_type='official'))
        self._post(f'/school-calendar/{hid2}/edit',
                   self._edit_form(_d(5), _d(5), holiday_type='official',
                                   applies_to='students'))  # unchanged value
        with self.app.app_context():
            h = db.session.get(SchoolHoliday, hid, execution_options=OPTS)
            self.assertEqual((h.name, h.notes, h.applies_to),
                             ('Edited', 'changed', 'employees'))
            h2 = db.session.get(SchoolHoliday, hid2, execution_options=OPTS)
            self.assertEqual((h2.name, h2.applies_to), ('Edited', 'students'))

    def test_holiday_year_and_cross_school_isolation(self):
        with self.app.app_context():
            hid_b = self._holiday('b', WED, applies_to='students', name='B-only')
        self._login(self.ids['admin_a'])
        form = {'name': 'Hack', 'start_date': TUE.isoformat(),
                'end_date': TUE.isoformat(), 'applies_to': 'employees'}
        self._post('/school-calendar/add',
                   dict(form, academic_year_id=str(self.ids['year_b'])))
        self._post(f'/school-calendar/{hid_b}/edit', form)
        self._post(f'/school-calendar/{hid_b}/toggle', {})
        self._post(f'/school-calendar/{hid_b}/delete', {})
        with self.app.app_context():
            self.assertEqual(self._rows(SchoolHoliday, school_id=self.ids['school_a']), [])
            h = db.session.get(SchoolHoliday, hid_b, execution_options=OPTS)
            self.assertIsNotNone(h)
            self.assertEqual((h.name, h.applies_to, h.is_active),
                             ('B-only', 'students', True))

    def test_weekly_schedule_save_rules(self):
        from app.blueprints.school_calendar import WEEKLY_EFFECTIVE_FROM, WEEKLY_NO_DAY_MSG
        fixed = date(2026, 9, 28)
        self.assertEqual(WEEKLY_EFFECTIVE_FROM, fixed)
        hist = date(2026, 9, 1)
        with self.app.app_context():
            # Pre-existing historical rows, incl. an empty one, must survive.
            hist_emp = self._schedule('a', 'employees', '5', hist)
            hist_stu = self._schedule('a', 'students', '', hist)
        self._login(self.ids['admin_a'])
        base = {'weekly_form': '1', 'audience': 'employees', 'day_4': '1'}
        self._post('/school-calendar/weekly', {'day_4': '1'})                   # old form
        self._post('/school-calendar/weekly', dict(base, audience='teachers'))
        for empty in ({'weekly_form': '1', 'audience': 'students'},
                      {'weekly_form': '1', 'audience': 'students',
                       'confirm_no_days': '1'}):                               # old checkbox
            resp = self.client.post('/school-calendar/weekly', data=empty,
                                    follow_redirects=True)
            self.assertIn(WEEKLY_NO_DAY_MSG, resp.get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(
                {r.id for r in self._rows(SchoolWeeklyOffSchedule,
                                          school_id=self.ids['school_a'])},
                {hist_emp, hist_stu})
        # Forged effective dates (past, far future, garbage), forged day_9 and
        # forged school_id are all ignored: the row is always effective 2026-09-28.
        self._post('/school-calendar/weekly',
                   dict(base, effective_from='2020-01-01', day_9='1',
                        school_id=str(self.ids['school_b'])))
        self._post('/school-calendar/weekly',
                   {'weekly_form': '1', 'audience': 'students', 'day_5': '1',
                    'effective_from': '2099-12-31'})
        # Same fixed-date row is updated; an identical re-save is a no-op.
        self._post('/school-calendar/weekly',
                   dict(base, day_5='1', effective_from='not-a-date'))
        self._post('/school-calendar/weekly', dict(base, day_5='1'))
        with self.app.app_context():
            rows = sorted(self._rows(SchoolWeeklyOffSchedule,
                                     school_id=self.ids['school_a']),
                          key=lambda r: (r.audience, r.effective_from))
            self.assertEqual([(r.audience, r.off_days, r.effective_from) for r in rows],
                             [('employees', '5', hist), ('employees', '4,5', fixed),
                              ('students', '', hist), ('students', '5', fixed)])
            self.assertEqual(self._rows(SchoolWeeklyOffSchedule,
                                        school_id=self.ids['school_b']), [])
            self.assertEqual(self._school('a').weekly_off_days, '4,5')
            logs = AuditLog.query.execution_options(bypass_tenant_scope=True).filter_by(
                school_id=self.ids['school_a'], resource='weekly_off_schedule').all()
            self.assertEqual(len(logs), 3)

    def test_weekly_schedule_delete_future_only_and_isolated(self):
        with self.app.app_context():
            today = get_local_date(self._school('a'))
            current_id = self._schedule('a', 'employees', '4', today)
            future_id = self._schedule('a', 'employees', '5', today + timedelta(days=5))
            other_id = self._schedule('b', 'employees', '5', today + timedelta(days=5))
        self._login(self.ids['admin_a'])
        for sid in (current_id, other_id, future_id):
            self._post(f'/school-calendar/weekly/{sid}/delete', {})
        with self.app.app_context():
            remaining = {r.id for r in SchoolWeeklyOffSchedule.query.execution_options(
                bypass_tenant_scope=True).filter(
                SchoolWeeklyOffSchedule.id.in_([current_id, future_id, other_id]))}
            self.assertEqual(remaining, {current_id, other_id})

    def test_permissions_and_super_admin_scope(self):
        self._login(self.ids['parent_a'])
        with self.app.app_context():
            today = get_local_date(self._school('a'))
        form = {'weekly_form': '1', 'audience': 'students',
                'effective_from': today.isoformat(), 'day_6': '1'}
        self.assertEqual(self._post('/school-calendar/weekly', form).status_code, 403)
        self.assertEqual(self._post('/school-calendar/add',
                                    {'name': 'x', 'start_date': TUE.isoformat(),
                                     'end_date': TUE.isoformat()}).status_code, 403)
        self._login(self.ids['super'], active_school=self.ids['school_b'])
        self._post('/school-calendar/weekly', form)
        with self.app.app_context():
            self.assertEqual(self._rows(SchoolWeeklyOffSchedule,
                                        school_id=self.ids['school_a']), [])
            self.assertEqual([r.off_days for r in self._rows(
                SchoolWeeklyOffSchedule, school_id=self.ids['school_b'])], ['6'])
            self.assertEqual(self._rows(SchoolHoliday, school_id=self.ids['school_a']), [])

    def test_unrelated_settings_save_keeps_schedules(self):
        with self.app.app_context():
            self._schedule('a', 'students', '4,5', _d(0))
        self._login(self.ids['admin_a'])
        self._post('/admin/attendance-settings',
                   {'att_start_time': '07:00', 'att_late_threshold': '07:30'})
        with self.app.app_context():
            self.assertEqual([r.off_days for r in self._rows(
                SchoolWeeklyOffSchedule, school_id=self.ids['school_a'])], ['4,5'])
            self.assertEqual(self._school('a').weekly_off_days, '4,5')

    def test_calendar_page_renders_both_schedules_and_escapes_names(self):
        with self.app.app_context():
            # Future holiday: its delete action (the former XSS sink) is rendered.
            future = get_local_date(self._school('a')) + timedelta(days=3)
            self._holiday('a', future, name='x\');alert(1);//"<b>', applies_to='students')
        self._login(self.ids['admin_a'])
        resp = self.client.get('/school-calendar/')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('أيام العطلة الأسبوعية للطلاب', html)
        self.assertIn('أيام العطلة الأسبوعية للموظفين', html)
        self.assertIn('الطلاب فقط', html)
        for removed in ('موروث من الإعداد المشترك',
                        'لا توجد أيام عطلة أسبوعية لهذه الفئة (مطلوب عند عدم اختيار أي يوم)',
                        'confirm_no_days', 'name="effective_from"', 'تاريخ السريان',
                        'يسري التغيير من تاريخ السريان'):
            self.assertNotIn(removed, html)
        self.assertIn(
            'ملاحظة: العطلة التي تشمل الطلاب (أو يوم عطلة أسبوعية للطلاب) لا يُسجَّل فيها '
            'غياب تلقائي للطلاب. العطلة التي تشمل الموظفين (أو يوم عطلة أسبوعية للموظفين) '
            'لا تُحتسب يوم عمل للموظفين في التقارير والرواتب.', html)
        self.assertNotIn('onsubmit=', html)
        self.assertNotIn('"<b>', html)
        self.assertIn('data-confirm="حذف العطلة «x&#39;);alert(1);//&#34;&lt;b&gt;»؟"',
                      html)

    # ── 7. Holiday absence cleanup is student-scoped ─────────────────────────

    def test_cleanup_route_refuses_employee_only_days(self):
        with self.app.app_context():
            self._holiday('a', _d(14), applies_to='employees')
            self._holiday('a', _d(15), applies_to='students')
            for day in (_d(14), _d(15)):
                db.session.add(StudentAttendance(
                    student_id=self.ids['student_a'], school_id=self.ids['school_a'],
                    academic_year_id=self.ids['year_a'], date=day,
                    status='absent', source='automatic'))
            db.session.commit()
        self._login(self.ids['admin_a'])
        for day in (_d(14), _d(15)):
            self._post('/attendance/cleanup-holiday-absences', {'date': day.isoformat()})
        with self.app.app_context():
            self.assertEqual(len(self._student_att('a', _d(14))), 1)
            self.assertEqual(self._student_att('a', _d(15)), [])


if __name__ == '__main__':
    unittest.main()

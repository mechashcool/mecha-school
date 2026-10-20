# -*- coding: utf-8 -*-
"""Employee automatic absence — egress narrowing must be behaviour-identical.

`_run_employee_auto_absent` now loads only Employee.id/school_id/hire_date/
shift_id (load_only) and only EmployeeAttendance.employee_id (with_entities)
instead of the full rows on every scheduler tick.

What is pinned here, against a VERBATIM frozen copy of the pre-change function
(`_baseline_run_employee_auto_absent` below, from main @ 7c13046):

  * identical return values, identical employee_attendance rows (every column
    except id/created_at), identical log records;
  * identical SQL statement sequence (same count, same verbs, same tables) —
    so the narrowing introduced no lazy load, no N+1 and no per-insert SELECT
    from the before_flush scope guard;
  * the employees SELECT carries 4 columns and the employee_attendance SELECT
    carries 1;
  * every business rule: cutoff (passed / exact / not yet / NULL / 00:00),
    employee holidays vs student-only holidays, weekly days off (legacy column
    and effective-dated schedule), active year, hire date (past / NULL /
    future), status, existing rows of every kind, shift mode (valid / not yet
    due / inactive / NULL cutoff / another school's shift / no shift);
  * school isolation, idempotency, a concurrent device write between the
    existence check and the insert, and two concurrent scheduler runs;
  * server→client protocol bytes for one steady-state tick, measured through
    a loopback relay (printed; asserted to shrink).

Runs against the isolated local PostgreSQL test database only.
"""
import logging
import re
import socket
import threading
import unittest
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import event, text

from app import create_app
from app.models import (
    db, AcademicYear, Employee, EmployeeAttendance, EmployeeAttendanceShift,
    School, SchoolHoliday, SchoolWeeklyOffSchedule,
)
from app.services.auto_attendance import _run_employee_auto_absent

OPTS = {'bypass_tenant_scope': True}
D = date(2031, 3, 4)                         # fixed Tuesday, far from real data
AT = lambda h, m=0: datetime.combine(D, time(h, m))   # noqa: E731
LOGGER = 'app.services.auto_attendance'
_log = logging.getLogger(LOGGER)


# ═════════════════════════════════════════════════════════════════════════════
#  Frozen baseline — verbatim copy of the function BEFORE the change.
#  Do not edit: it is the reference the optimised version is compared with.
# ═════════════════════════════════════════════════════════════════════════════

def _baseline_run_employee_auto_absent(school) -> dict:
    """Persist one final employee absent row per eligible missing workday."""
    from sqlalchemy import or_
    from sqlalchemy.exc import IntegrityError

    from app.models import Employee, EmployeeAttendance, db
    from app.utils.decorators import get_active_year
    from app.utils.employee_attendance_helper import (
        AUTO_ABSENCE_SOURCE,
        classify_missing_working_day,
        get_employee_absence_clock,
        get_working_days,
    )

    clock = get_employee_absence_clock(school)
    shift_mode = bool(getattr(school, 'emp_enable_attendance_shifts', False))
    if not shift_mode and not clock.cutoff_passed:
        return {'count': 0, 'reason': 'cutoff_not_passed'}

    target_date = clock.local_today
    if not get_working_days(target_date, target_date, school):
        return {'count': 0, 'reason': 'non_working_day'}

    year = get_active_year(school.id)
    if not year:
        return {'count': 0, 'reason': 'no_active_year'}

    employees = (Employee.query
                 .execution_options(bypass_tenant_scope=True)
                 .filter(
                     Employee.school_id == school.id,
                     Employee.status == 'active',
                     or_(Employee.hire_date.is_(None),
                         Employee.hire_date <= target_date),
                 )
                 .all())
    if not employees:
        return {'count': 0, 'reason': 'no_eligible_employees'}

    shift_map = {}
    valid_shift_employee_ids = None
    if shift_mode:
        from app.utils.attendance_helpers import get_employee_shift_map
        shift_map = get_employee_shift_map(school, employees)
        valid_shift_employee_ids = set(shift_map)
        for employee in employees:
            if employee.id not in valid_shift_employee_ids:
                _log.warning(
                    '[employee-auto-absence] skipped invalid shift assignment; '
                    'employee_id=%s school_id=%s shift_id=%s',
                    employee.id, school.id, getattr(employee, 'shift_id', None))
        for employee_id, shift in shift_map.items():
            if getattr(shift, 'absent_after_time', None) is None:
                _log.warning(
                    '[employee-auto-absence] skipped NULL shift absence cutoff; '
                    'employee_id=%s school_id=%s shift_id=%s',
                    employee_id, school.id, shift.id)

    employee_ids = [employee.id for employee in employees]
    existing_ids = {
        row.employee_id
        for row in (EmployeeAttendance.query
                    .execution_options(bypass_tenant_scope=True)
                    .filter(
                        EmployeeAttendance.school_id == school.id,
                        EmployeeAttendance.employee_id.in_(employee_ids),
                        EmployeeAttendance.date == target_date,
                    )
                    .all())
    }

    pending = [
        employee for employee in employees
        if employee.id not in existing_ids
        and (valid_shift_employee_ids is None
             or employee.id in valid_shift_employee_ids)
        and (
            (not shift_mode and classify_missing_working_day(
                target_date, clock, employee.hire_date) == 'absent')
            or (shift_mode
                and employee.id in shift_map
                and getattr(shift_map[employee.id], 'absent_after_time', None) is not None
                and clock.local_now.time()
                    >= shift_map[employee.id].absent_after_time)
        )
    ]

    created = raced = 0
    for employee in pending:
        try:
            # The unique (employee_id, date) constraint is the concurrency
            # authority. A per-row savepoint lets one racing check-in coexist
            # with successful absences for the rest of this school's staff.
            with db.session.begin_nested():
                db.session.add(EmployeeAttendance(
                    employee_id=employee.id,
                    school_id=school.id,
                    academic_year_id=year.id,
                    date=target_date,
                    status='absent',
                    recorded_by=None,
                    notes='غياب آلي بعد وقت إغلاق الحضور',
                    source=AUTO_ABSENCE_SOURCE,
                ))
                db.session.flush()
            created += 1
        except IntegrityError:
            raced += 1

    if created:
        db.session.commit()

    return {'count': created, 'raced': raced, 'date': target_date}


# ═════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═════════════════════════════════════════════════════════════════════════════

class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.items = []

    def emit(self, record):
        self.items.append((record.levelname, record.getMessage()))


@contextmanager
def _clock(at):
    with patch('app.utils.attendance_helpers.get_local_now', return_value=at):
        yield


def _shape(statement):
    """(verb, table) — the identity of a statement without its column list."""
    s = ' '.join(statement.split())
    verb = s.split(' ', 1)[0].upper()
    m = (re.search(r'\bINSERT INTO (\w+)', s)
         or re.search(r'\bFROM (\w+)', s)
         or re.search(r'\bUPDATE (\w+)', s))
    return verb, (m.group(1) if m else '')


def _select_list(statement):
    s = ' '.join(statement.split())
    return s[len('SELECT '):s.index(' FROM ')]


class _Relay:
    """Loopback TCP relay that counts bytes in each direction."""

    def __init__(self, upstream_port):
        self.upstream_port = upstream_port
        self.down = 0                       # server -> client (egress side)
        self.up = 0
        self._lock = threading.Lock()
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            server = socket.create_connection(('127.0.0.1', self.upstream_port))
            threading.Thread(target=self._pump, args=(client, server, 'up'),
                             daemon=True).start()
            threading.Thread(target=self._pump, args=(server, client, 'down'),
                             daemon=True).start()

    def _pump(self, src, dst, direction):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                with self._lock:
                    setattr(self, direction, getattr(self, direction) + len(data))
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def reset(self):
        with self._lock:
            self.down = self.up = 0

    def close(self):
        self.sock.close()


# ═════════════════════════════════════════════════════════════════════════════
#  Tests
# ═════════════════════════════════════════════════════════════════════════════

class EmployeeAutoAbsenceEgressTest(unittest.TestCase):
    """School A (general mode, cutoff 09:00), school B (general mode — exists
    so isolation can leak to it), school S (employee SHIFT mode)."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        with cls.app.app_context():
            clash = db.session.execute(text(
                'SELECT count(*) FROM school_holidays WHERE school_id IS NULL '
                'AND is_active AND start_date <= :d AND end_date >= :d'),
                {'d': D}).scalar()
        if clash:
            raise unittest.SkipTest('a global holiday covers the fixture date')

    # ── Fixture ──────────────────────────────────────────────────────────────

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {'schools': [], 'years': [], 'emps': {}, 'shifts': {}}
        with self.app.app_context():
            for tag in ('a', 'b', 's'):
                school = School(
                    school_name=f'EAA {tag} {self.sfx}',
                    code=f'EA{tag.upper()}{self.sfx[:6]}',
                    capacity=0, is_active=True, timezone='Asia/Baghdad',
                    weekly_off_days=None,
                    emp_att_start_time=time(7, 0),
                    emp_att_late_threshold=time(7, 30),
                    emp_att_absence_threshold=time(9, 0),
                    emp_att_departure_time=time(14, 0),
                    emp_enable_attendance_shifts=(tag == 's'))
                db.session.add(school)
                db.session.flush()
                year = AcademicYear(school_id=school.id, name=f'Y {tag} {self.sfx}',
                                    start_date=D - timedelta(days=150),
                                    end_date=D + timedelta(days=150),
                                    is_current=True)
                db.session.add(year)
                db.session.flush()
                self.ids[f'school_{tag}'] = school.id
                self.ids[f'year_{tag}'] = year.id
                self.ids['schools'].append(school.id)
                self.ids['years'].append(year.id)

            sid = self.ids
            shifts = {
                'pass':     ('s', time(9, 30), True),
                'later':    ('s', time(11, 0), True),
                'inactive': ('s', time(9, 30), False),
                'nullcut':  ('s', None, True),
                'foreign':  ('a', time(9, 30), True),
            }
            for key, (tag, cutoff, active) in shifts.items():
                sh = EmployeeAttendanceShift(
                    school_id=sid[f'school_{tag}'], name=f'{key} {self.sfx}',
                    start_time=time(7, 0), late_after_time=time(7, 30),
                    absent_after_time=cutoff, dismissal_time=time(14, 0),
                    is_active=active)
                db.session.add(sh)
                db.session.flush()
                self.ids['shifts'][key] = sh.id

            old = D - timedelta(days=400)
            people = {
                # key: (school, status, hire_date, shift key)
                'a_present':     ('a', 'active', old, None),
                'a_missing1':    ('a', 'active', old, None),
                'a_missing2':    ('a', 'active', old, None),
                'a_leave_row':   ('a', 'active', old, None),
                'a_prev_auto':   ('a', 'active', old, None),
                'a_future':      ('a', 'active', D + timedelta(days=1), None),
                'a_hired_today': ('a', 'active', D, None),
                'a_null_hire':   ('a', 'active', 'NULL', None),
                'a_terminated':  ('a', 'terminated', old, None),
                'a_status_leave': ('a', 'on_leave', old, None),
                'a_foreign_row': ('a', 'active', old, None),
                'b_missing':     ('b', 'active', old, None),
                's_pass_missing': ('s', 'active', old, 'pass'),
                's_pass_present': ('s', 'active', old, 'pass'),
                's_later':       ('s', 'active', old, 'later'),
                's_inactive':    ('s', 'active', old, 'inactive'),
                's_nullcut':     ('s', 'active', old, 'nullcut'),
                's_foreign':     ('s', 'active', old, 'foreign'),
                's_no_shift':    ('s', 'active', old, None),
            }
            for n, (key, (tag, status, hire, shift)) in enumerate(people.items()):
                emp = Employee(
                    employee_id=f'EAA-{self.sfx}-{n}',
                    full_name=f'موظف اختبار {key}',
                    job_title='مدرس', department='الإدارة',
                    phone='07700000000', address='بغداد - عنوان تجريبي',
                    photo=f'https://example.invalid/p/{self.sfx}/{n}.jpg',
                    bank_account='IQ00TEST0000000000',
                    notes='سجل اختبار', base_salary=0,
                    school_id=sid[f'school_{tag}'], status=status,
                    hire_date=None if hire == 'NULL' else hire,
                    shift_id=self.ids['shifts'][shift] if shift else None)
                db.session.add(emp)
                db.session.flush()
                self.ids['emps'][key] = emp.id
            db.session.execute(text('UPDATE employees SET hire_date = NULL WHERE id = :i'),
                               {'i': self.ids['emps']['a_null_hire']})

            e = self.ids['emps']
            for key, status, source in (('a_present', 'present', 'aiface'),
                                        ('a_leave_row', 'on_leave', 'leave'),
                                        ('a_prev_auto', 'absent', 'auto_absence'),
                                        ('s_pass_present', 'late', 'manual')):
                tag = key[0]
                db.session.add(EmployeeAttendance(
                    employee_id=e[key], school_id=sid[f'school_{tag}'],
                    academic_year_id=sid[f'year_{tag}'], date=D,
                    status=status, source=source,
                    check_in=time(7, 10) if status in ('present', 'late') else None))
            db.session.flush()
            # Inconsistent legacy row: A's employee recorded under school B. The
            # scope guard forbids this via the ORM, so write it directly.
            db.session.execute(text(
                'INSERT INTO employee_attendance '
                '(employee_id, school_id, academic_year_id, date, status, source) '
                "VALUES (:e, :s, :y, :d, 'present', 'manual')"),
                {'e': e['a_foreign_row'], 's': sid['school_b'],
                 'y': sid['year_b'], 'd': D})
            db.session.commit()
            self.assertIsNone(db.session.execute(
                text('SELECT hire_date FROM employees WHERE id = :i'),
                {'i': e['a_null_hire']}).scalar())

    def tearDown(self):
        with self.app.app_context():
            s = self.ids['schools']
            for sql in (
                'DELETE FROM employee_attendance WHERE school_id = ANY(:s) '
                'OR employee_id = ANY(:e)',
                'DELETE FROM employees WHERE school_id = ANY(:s)',
                'DELETE FROM employee_attendance_shifts WHERE school_id = ANY(:s)',
                'DELETE FROM school_holidays WHERE school_id = ANY(:s)',
                'DELETE FROM school_weekly_off_schedules WHERE school_id = ANY(:s)',
                'DELETE FROM academic_years WHERE school_id = ANY(:s)',
            ):
                db.session.execute(text(sql), {'s': s, 'e': list(self.ids['emps'].values())})
            for pk in s:
                row = db.session.get(School, pk, execution_options=OPTS)
                if row is not None:
                    db.session.delete(row)
            db.session.commit()

    # ── Runner / comparison ─────────────────────────────────────────────────

    def _att_ids(self):
        with self.app.app_context():
            return {r[0] for r in db.session.execute(text(
                'SELECT id FROM employee_attendance WHERE employee_id = ANY(:e)'),
                {'e': list(self.ids['emps'].values())})}

    def _rows(self):
        with self.app.app_context():
            return db.session.execute(text(
                'SELECT employee_id, school_id, academic_year_id, date, status, '
                'source, notes, recorded_by, check_in, check_out, device_id '
                'FROM employee_attendance WHERE employee_id = ANY(:e) '
                'ORDER BY employee_id, school_id'),
                {'e': list(self.ids['emps'].values())}).all()

    def _reset_to(self, keep):
        with self.app.app_context():
            db.session.execute(text(
                'DELETE FROM employee_attendance WHERE employee_id = ANY(:e) '
                'AND NOT (id = ANY(:k))'),
                {'e': list(self.ids['emps'].values()), 'k': list(keep)})
            db.session.commit()

    def _run(self, fn, tag, at, *, on_existing_select=None):
        logger = logging.getLogger(LOGGER)
        handler = _Records()
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        stmts = []
        fired = []
        try:
            with self.app.app_context():
                engine = db.engine
                school = db.session.get(School, self.ids[f'school_{tag}'],
                                        execution_options=OPTS)
                _ = school.emp_att_absence_threshold     # loaded before capture

                def before(conn, cursor, statement, params, context, many):
                    stmts.append(statement)

                def after(conn, cursor, statement, params, context, many):
                    if (on_existing_select and not fired
                            and _shape(statement) == ('SELECT', 'employee_attendance')):
                        fired.append(True)
                        on_existing_select(engine)

                event.listen(engine, 'before_cursor_execute', before)
                event.listen(engine, 'after_cursor_execute', after)
                try:
                    with _clock(at):
                        result = fn(school)
                finally:
                    event.remove(engine, 'before_cursor_execute', before)
                    event.remove(engine, 'after_cursor_execute', after)
                    db.session.remove()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        return result, handler.items, stmts

    def _parity(self, tag, at, **kw):
        """Run baseline then optimised from the same state; assert identity."""
        keep = self._att_ids()
        base = self._run(_baseline_run_employee_auto_absent, tag, at, **kw)
        base_rows = self._rows()
        self._reset_to(keep)
        new = self._run(_run_employee_auto_absent, tag, at, **kw)
        new_rows = self._rows()

        self.assertEqual(base[0], new[0], 'return value differs')
        self.assertEqual(base_rows, new_rows, 'employee_attendance rows differ')
        self.assertEqual(base[1], new[1], 'log records differ')
        self.assertEqual([_shape(s) for s in base[2]],
                         [_shape(s) for s in new[2]],
                         'SQL statement sequence differs')
        for s in new[2]:
            shape = _shape(s)
            if shape == ('SELECT', 'employees'):
                cols = [c.strip() for c in _select_list(s).split(',')]
                self.assertEqual(len(cols), 4, cols)
            if shape == ('SELECT', 'employee_attendance'):
                cols = [c.strip() for c in _select_list(s).split(',')]
                self.assertEqual(len(cols), 1, cols)
                self.assertTrue(cols[0].startswith('employee_attendance.employee_id'),
                                cols)
        return new, new_rows

    def _created_for(self, rows, tag=None):
        ids = {v: k for k, v in self.ids['emps'].items()}
        return {ids[r.employee_id] for r in rows
                if r.source == 'auto_absence' and r.status == 'absent'
                and (tag is None or r.school_id == self.ids[f'school_{tag}'])}

    # ── General mode ────────────────────────────────────────────────────────

    def test_after_cutoff_creates_exactly_the_missing_and_is_identical(self):
        (result, logs, stmts), rows = self._parity('a', AT(10))
        self.assertEqual(result, {'count': 4, 'raced': 1, 'date': D})
        self.assertEqual(self._created_for(rows),
                         {'a_prev_auto', 'a_missing1', 'a_missing2',
                          'a_hired_today', 'a_null_hire'})
        new_rows = [r for r in rows if r.notes == 'غياب آلي بعد وقت إغلاق الحضور']
        self.assertTrue(all(r.school_id == self.ids['school_a']
                            and r.academic_year_id == self.ids['year_a']
                            and r.recorded_by is None and r.check_in is None
                            for r in new_rows))
        # No extra SELECT on employees during the four inserts (identity map).
        self.assertEqual(sum(_shape(s) == ('SELECT', 'employees') for s in stmts), 1)
        # Untouched: present / leave / previous auto / foreign-school row.
        by = {(r.employee_id, r.school_id): r for r in rows}
        e = self.ids['emps']
        self.assertEqual(by[(e['a_present'], self.ids['school_a'])].status, 'present')
        self.assertEqual(by[(e['a_leave_row'], self.ids['school_a'])].status, 'on_leave')
        self.assertNotIn((e['a_foreign_row'], self.ids['school_a']), by)

    def test_exactly_at_cutoff_creates(self):
        (result, _, _), _ = self._parity('a', AT(9, 0))
        self.assertEqual(result['count'], 4)

    def test_before_cutoff_touches_nothing(self):
        (result, _, stmts), _ = self._parity('a', AT(8, 59))
        self.assertEqual(result, {'count': 0, 'reason': 'cutoff_not_passed'})
        self.assertEqual(stmts, [])

    def _set_school(self, tag, **values):
        with self.app.app_context():
            school = db.session.get(School, self.ids[f'school_{tag}'],
                                    execution_options=OPTS)
            for k, v in values.items():
                setattr(school, k, v)
            db.session.commit()

    def test_null_and_midnight_cutoff_fail_closed(self):
        for cutoff in (None, time(0, 0)):
            self._set_school('a', emp_att_absence_threshold=cutoff)
            (result, _, stmts), _ = self._parity('a', AT(23, 50))
            self.assertEqual(result, {'count': 0, 'reason': 'cutoff_not_passed'})
            self.assertEqual(stmts, [])

    def test_employee_holiday_is_off_but_student_only_holiday_is_working(self):
        with self.app.app_context():
            h = SchoolHoliday(school_id=self.ids['school_a'], name='h',
                              start_date=D, end_date=D, applies_to='students',
                              is_active=True)
            db.session.add(h)
            db.session.commit()
        (result, _, _), _ = self._parity('a', AT(10))
        self.assertEqual(result['count'], 4)

        self.setUp_rows_again()
        with self.app.app_context():
            db.session.add(SchoolHoliday(
                school_id=self.ids['school_a'], name='h2', start_date=D,
                end_date=D, applies_to='employees', is_active=True))
            db.session.commit()
        (result, _, stmts), _ = self._parity('a', AT(10))
        self.assertEqual(result, {'count': 0, 'reason': 'non_working_day'})
        self.assertFalse(any(_shape(s)[1] == 'employees' for s in stmts))

    def setUp_rows_again(self):
        """Re-create the fixture attendance rows after a full reset."""
        self.tearDown()
        self.setUp()

    def test_weekly_off_legacy_column_and_effective_schedule(self):
        self._set_school('a', weekly_off_days=str(D.weekday()))
        (result, _, _), _ = self._parity('a', AT(10))
        self.assertEqual(result, {'count': 0, 'reason': 'non_working_day'})

        self._set_school('a', weekly_off_days=None)
        with self.app.app_context():
            for audience, days in (('students', str(D.weekday())), ('employees', '')):
                db.session.add(SchoolWeeklyOffSchedule(
                    school_id=self.ids['school_a'], audience=audience,
                    off_days=days, effective_from=D - timedelta(days=30)))
            db.session.commit()
        (result, _, _), _ = self._parity('a', AT(10))
        self.assertEqual(result['count'], 4)          # student-only day off

        self.setUp_rows_again()
        with self.app.app_context():
            db.session.add(SchoolWeeklyOffSchedule(
                school_id=self.ids['school_a'], audience='employees',
                off_days=str(D.weekday()), effective_from=D - timedelta(days=30)))
            db.session.commit()
        (result, _, _), _ = self._parity('a', AT(10))
        self.assertEqual(result, {'count': 0, 'reason': 'non_working_day'})

    def test_no_current_year(self):
        with self.app.app_context():
            db.session.execute(text('UPDATE academic_years SET is_current = false '
                                    'WHERE id = :i'), {'i': self.ids['year_a']})
            db.session.commit()
        (result, _, _), _ = self._parity('a', AT(10))
        self.assertEqual(result, {'count': 0, 'reason': 'no_active_year'})

    def test_no_eligible_employees(self):
        with self.app.app_context():
            db.session.execute(text("UPDATE employees SET status = 'terminated' "
                                    'WHERE school_id = :s'), {'s': self.ids['school_b']})
            db.session.commit()
        (result, _, _), _ = self._parity('b', AT(10))
        self.assertEqual(result, {'count': 0, 'reason': 'no_eligible_employees'})

    # ── Isolation / idempotency ─────────────────────────────────────────────

    def test_school_isolation(self):
        (_, _, stmts), rows = self._parity('a', AT(10))
        e = self.ids['emps']
        others = [r for r in rows
                  if r.employee_id in (e['b_missing'], e['s_pass_missing'])]
        self.assertEqual(others, [])
        for s in stmts:
            if _shape(s)[0] == 'SELECT' and _shape(s)[1] in ('employees',
                                                               'employee_attendance'):
                self.assertIn('school_id', s)

    def test_second_tick_is_idempotent(self):
        first = self._run(_run_employee_auto_absent, 'a', AT(10))
        rows_after_first = self._rows()
        second, _ = self._parity('a', AT(10, 5))
        self.assertEqual(first[0]['count'], 4)
        self.assertEqual(second[0], {'count': 0, 'raced': 1, 'date': D})
        self.assertEqual(self._rows(), rows_after_first)

    # ── Shift mode ──────────────────────────────────────────────────────────

    def test_shift_mode_identical_including_invalid_shift_warnings(self):
        (result, logs, _), rows = self._parity('s', AT(10))
        self.assertEqual(result, {'count': 1, 'raced': 0, 'date': D})
        self.assertEqual(self._created_for(rows, 's'), {'s_pass_missing'})
        warned = {m for lvl, m in logs if 'invalid shift assignment' in m}
        e = self.ids['emps']
        for key in ('s_inactive', 's_nullcut', 's_foreign', 's_no_shift'):
            self.assertTrue(any(f'employee_id={e[key]} ' in m for m in warned), key)

    def test_shift_mode_before_any_shift_cutoff(self):
        (result, _, stmts), _ = self._parity('s', AT(7, 0))
        self.assertEqual(result, {'count': 0, 'raced': 0, 'date': D})
        self.assertIn(('SELECT', 'employee_attendance_shifts'),
                      [_shape(s) for s in stmts])

    def test_shift_mode_later_shift_after_its_cutoff(self):
        (result, _, _), rows = self._parity('s', AT(11, 0))
        self.assertEqual(self._created_for(rows, 's'), {'s_pass_missing', 's_later'})

    # ── Concurrency ─────────────────────────────────────────────────────────

    def test_device_write_between_check_and_insert_is_raced_not_duplicated(self):
        target = self.ids['emps']['a_missing1']
        sid, yid = self.ids['school_a'], self.ids['year_a']

        def device_punch(engine):
            with engine.connect() as conn:
                conn.execute(text(
                    'INSERT INTO employee_attendance '
                    '(employee_id, school_id, academic_year_id, date, status, '
                    "source, check_in) VALUES (:e, :s, :y, :d, 'present', "
                    "'aiface', '09:58')"), {'e': target, 's': sid, 'y': yid, 'd': D})
                conn.commit()

        (result, _, _), rows = self._parity('a', AT(10), on_existing_select=device_punch)
        self.assertEqual(result, {'count': 3, 'raced': 2, 'date': D})
        mine = [r for r in rows if r.employee_id == target]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].status, 'present')

    def test_two_concurrent_scheduler_runs_never_duplicate(self):
        barrier = threading.Barrier(2)
        results, errors = [], []

        def worker():
            try:
                with self.app.app_context():
                    school = db.session.get(School, self.ids['school_a'],
                                            execution_options=OPTS)
                    _ = school.timezone
                    barrier.wait(timeout=10)
                    results.append(_run_employee_auto_absent(school))
                    db.session.remove()
            except Exception as exc:                 # pragma: no cover
                errors.append(exc)

        with _clock(AT(10)):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(sum(r['count'] for r in results), 4)
        rows = self._rows()
        keys = [(r.employee_id, r.date) for r in rows]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(self._created_for(rows),
                         {'a_prev_auto', 'a_missing1', 'a_missing2',
                          'a_hired_today', 'a_null_hire'})

    # ── Network measurement (steady state) ──────────────────────────────────

    def test_steady_state_protocol_bytes_shrink(self):
        """One steady-state tick (every employee already has a row today):
        server→client bytes through a loopback relay, baseline vs optimised."""
        import os
        import config.settings as settings
        from sqlalchemy.engine import make_url

        # Steady state for school A: let the optimised run fill the gaps first.
        self._run(_run_employee_auto_absent, 'a', AT(10))

        url = make_url(os.environ['TEST_DATABASE_URL'])
        relay = _Relay(url.port)
        original = settings.TestingConfig.SQLALCHEMY_DATABASE_URI
        settings.TestingConfig.SQLALCHEMY_DATABASE_URI = url.set(
            host='127.0.0.1', port=relay.port).render_as_string(hide_password=False)
        try:
            app2 = create_app('testing')
        finally:
            settings.TestingConfig.SQLALCHEMY_DATABASE_URI = original

        measured = {}
        try:
            for name, fn in (('baseline', _baseline_run_employee_auto_absent),
                             ('optimised', _run_employee_auto_absent)):
                samples = []
                for _ in range(3):
                    with app2.app_context():
                        db.session.execute(text('SELECT 1'))          # warm conn
                        school = db.session.get(School, self.ids['school_a'],
                                                execution_options=OPTS)
                        _ = school.emp_att_absence_threshold
                        relay.reset()
                        with _clock(AT(10, 5)):
                            result = fn(school)
                        samples.append((relay.down, relay.up))
                        db.session.remove()
                    self.assertEqual(result, {'count': 0, 'raced': 1, 'date': D})
                measured[name] = min(samples)
        finally:
            with app2.app_context():
                db.engine.dispose()
            relay.close()

        with self.app.app_context():
            n_emp = db.session.execute(text(
                "SELECT count(*) FROM employees WHERE school_id = :s AND status = 'active'"),
                {'s': self.ids['school_a']}).scalar()
        print(f'\n[egress-measure] school A active employees={n_emp}  '
              f'baseline down={measured["baseline"][0]}B up={measured["baseline"][1]}B  '
              f'optimised down={measured["optimised"][0]}B up={measured["optimised"][1]}B')
        self.assertLess(measured['optimised'][0], measured['baseline'][0])


if __name__ == '__main__':                                    # pragma: no cover
    unittest.main()

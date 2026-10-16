# -*- coding: utf-8 -*-
"""Institute teacher attendance per lesson — focused guarantees only.

/employees/attendance/manual branches for School.is_institute into
Date -> Group -> Lesson -> scheduled teacher, stored in
institute_instructor_attendance and NEVER in employee_attendance.

The institute's DAILY employee attendance (employee, date) is a separate
page, /employees/attendance/daily, sharing the school's daily list/save and
report, stored in employee_attendance and NEVER in
institute_instructor_attendance.

Fixture (inherited, Sunday 2025-09-07):
  Institute A  A-Group (teacher A): Sun 10-12, 13-14*, 16-18, 19-20; Mon 17-19
               B-Group (teacher B): Sun 16-18
               C-Group (teacher A): Sun 13-14            (* via C-Group)
               teacher C: an employee with no lessons at all
  Institute O  O-Group (teacher O): Sun 16-18
  School S     ordinary school with one employee (regression baseline)
"""
import re
import unittest
from datetime import date, time

from flask import get_flashed_messages
from flask_login import logout_user
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import Forbidden, NotFound

from app.models import (db, Employee, EmployeeAttendance,
                        EmployeeAttendanceShift,
                        InstituteAttendanceRecord, InstituteAttendanceSession,
                        InstituteInstructorAttendance, InstituteStudyGroup,
                        School, SchoolHoliday, SchoolWeeklyOffSchedule)
from app.services import institute_attendance as att

from tests.test_institute_attendance import InstituteAttendanceTest, OPTS

_Fixture = InstituteAttendanceTest
del InstituteAttendanceTest

SUNDAY = '2025-09-07'
MONDAY = '2025-09-08'
FRIDAY = '2025-09-12'


class InstituteInstructorAttendanceTest(_Fixture):

    def setUp(self):
        super().setUp()
        with self.app.app_context():
            ids = self.ids
            self._slot(ids['inst'], ids['iyear'], ids['ga'], 0, time(10, 0), time(12, 0))
            self._slot(ids['inst'], ids['iyear'], ids['ga'], 0, time(19, 0), time(20, 0))
            gc = self._group(ids['inst'], ids['iyear'], ids['isubj'], 'C-Group', ids['ea'])
            self._slot(ids['inst'], ids['iyear'], gc.id, 0, time(13, 0), time(14, 0))
            ec = self._employee(ids['inst'], 'NoLessons')
            uoadm = self._user(ids['oinst'], 'attoadm', self.admin_role_id)
            eo = (Employee.query.execution_options(**OPTS)
                  .filter_by(school_id=ids['oinst']).first())
            # Non-teaching institute staff: no user, no group, no lesson.
            acc = self._employee(ids['inst'], 'Acc')
            acc.job_title, acc.department = 'محاسب', 'الحسابات والمالية'
            # Ordinary school: an admin and one employee.
            usadm = self._user(ids['sch'], 'attsadm', self.admin_role_id)
            es = self._employee(ids['sch'], 'S')
            # Employee.hire_date defaults to TODAY; the daily sheet ignores
            # pre-hire dates, so the 2025 fixture dates need an earlier hire.
            for emp in (Employee.query.execution_options(**OPTS)
                        .filter(Employee.school_id.in_(
                            [ids['inst'], ids['oinst'], ids['sch']])).all()):
                emp.hire_date = date(2025, 8, 1)
            db.session.commit()
            ids.update({'gc': gc.id, 'ec': ec.id, 'eo': eo.id, 'acc': acc.id,
                        'uoadm': uoadm.id, 'usadm': usadm.id, 'es': es.id})

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            schools = [self.ids['inst'], self.ids['oinst'], self.ids['sch']]
            for emp in (Employee.query.execution_options(**OPTS)
                        .filter(Employee.school_id.in_(schools),
                                Employee.shift_id.isnot(None)).all()):
                emp.shift_id = None
            db.session.flush()
            for model in (InstituteInstructorAttendance, EmployeeAttendance,
                          EmployeeAttendanceShift, SchoolHoliday,
                          SchoolWeeklyOffSchedule):
                for row in (model.query.execution_options(**OPTS)
                            .filter(model.school_id.in_(schools)).all()):
                    db.session.delete(row)
            db.session.commit()
        super().tearDown()

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _page(self, user_key, **args):
        from app.blueprints.employees import manual_attendance
        with self.app.test_request_context('/employees/attendance/manual',
                                           query_string=args):
            self._login(user_key)
            try:
                return manual_attendance()
            finally:
                logout_user()

    def _post(self, view, path, user_key, data):
        with self.app.test_request_context(path, method='POST', data=data):
            self._login(user_key)
            try:
                resp = view()
                return resp, get_flashed_messages(with_categories=True)
            finally:
                logout_user()

    def _save(self, user_key, *lessons, on=SUNDAY, extra=None):
        """lessons: (group_key_or_id, 'HH:MM', employee_key_or_id, status)."""
        from app.blueprints.employees import manual_lesson_attendance_save
        data = {'att_date': on, 'lesson_keys': []}
        for group, start, emp, status in lessons:
            gid = self.ids.get(group, group) if isinstance(group, str) else group
            eid = self.ids.get(emp, emp) if isinstance(emp, str) else emp
            key = f'{gid}_{str(start).replace(":", "")}'
            data['lesson_keys'].append(key)
            data.update({f'group_{key}': str(gid), f'start_{key}': start,
                         f'employee_{key}': str(eid), f'status_{key}': status})
        data.update(extra or {})
        return self._post(manual_lesson_attendance_save,
                          '/employees/attendance/manual/lessons/save',
                          user_key, data)

    @staticmethod
    def _rows(html):
        """[(group_id, start, employee_id_or_'')] of the rendered lesson rows."""
        return [(int(g), s, e) for g, s, e in re.findall(
            r'data-group="(\d+)" data-start="([\d:]+)"\s*data-employee="(\d*)"',
            html)]

    def _instr_rows(self):
        with self.app.app_context():
            return [(r.id, r.session_id, r.employee_id, r.status, r.role)
                    for r in InstituteInstructorAttendance.query
                    .execution_options(**OPTS)
                    .filter(InstituteInstructorAttendance.school_id.in_(
                        [self.ids['inst'], self.ids['oinst']]))
                    .order_by(InstituteInstructorAttendance.id).all()]

    def _counts(self):
        with self.app.app_context():
            schools = [self.ids['inst'], self.ids['oinst'], self.ids['sch']]
            return {
                'sessions': InstituteAttendanceSession.query.execution_options(**OPTS)
                .filter(InstituteAttendanceSession.school_id.in_(schools)).count(),
                'instr': InstituteInstructorAttendance.query.execution_options(**OPTS)
                .filter(InstituteInstructorAttendance.school_id.in_(schools)).count(),
                'employee_attendance': EmployeeAttendance.query.execution_options(**OPTS)
                .filter(EmployeeAttendance.school_id.in_(schools)).count(),
            }

    def _session(self, group_key, start):
        with self.app.app_context():
            return (InstituteAttendanceSession.query.execution_options(**OPTS)
                    .filter_by(group_id=self.ids[group_key],
                               session_date=date(2025, 9, 7), start_time=start)
                    .first())

    def _name(self, emp_key):
        with self.app.app_context():
            return db.session.get(Employee, self.ids[emp_key],
                                  execution_options=OPTS).full_name

    @staticmethod
    def _errors(flashes):
        return [m for c, m in flashes if c == 'danger']

    # ── Normal school unchanged ─────────────────────────────────────────────

    def test_normal_school_keeps_daily_sheet(self):
        from app.blueprints.employees import (manual_attendance_list,
                                              manual_attendance_save)
        html = self._page('usadm')
        self.assertIn('EMP_LIST_URL', html, 'school keeps the daily sheet')
        self.assertNotIn('حضور المدرسين في الحصص', html)
        self.assertNotIn('lessonAttForm', html)

        with self.app.test_request_context('/employees/attendance/manual/list',
                                           query_string={'date': SUNDAY}):
            self._login('usadm')
            payload = manual_attendance_list().get_json()
            logout_user()
        self.assertIn(self.ids['es'], [e['id'] for e in payload['employees']])

        _resp, flashes = self._post(
            manual_attendance_save, '/employees/attendance/manual/save', 'usadm',
            {'att_date': SUNDAY, 'emp_ids': [str(self.ids['es'])],
             f'status_{self.ids["es"]}': 'absent'})
        self.assertFalse(self._errors(flashes), flashes)
        with self.app.app_context():
            rows = (EmployeeAttendance.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids['sch']).all())
            self.assertEqual([(r.employee_id, r.status, r.date) for r in rows],
                             [(self.ids['es'], 'absent', date(2025, 9, 7))])

        # The institute-only save route does not exist for a school.
        from app.blueprints.employees import manual_lesson_attendance_save
        with self.assertRaises(NotFound):
            self._post(manual_lesson_attendance_save,
                       '/employees/attendance/manual/lessons/save', 'usadm',
                       {'att_date': SUNDAY})

    # ── Navigation: "حضور المدرسين" for institutes only ──────────────────────

    def test_sidebar_teacher_attendance_item_institute_only(self):
        item = (r'<a href="/employees/attendance/manual"\s+class="active">\s*'
                r'<i class="bi bi-person-check"></i> حضور المدرسين\s*</a>')
        self.assertRegex(self._page('uadmin', date=SUNDAY), item)
        school = self._page('usadm')
        self.assertNotRegex(school, r'</i> حضور المدرسين\s*</a>')
        self.assertIn('</i> حضور الموظفين', school, 'school HR items unchanged')

    # ── Institute page: date / group / lesson filtering ─────────────────────

    def test_institute_page_lists_only_scheduled_teachers(self):
        html = self._page('uadmin', date=SUNDAY)
        self.assertIn('حضور المدرسين في الحصص', html)
        self.assertNotIn('EMP_LIST_URL', html)
        ga, gb, gc = self.ids['ga'], self.ids['gb'], self.ids['gc']
        ea, eb = str(self.ids['ea']), str(self.ids['eb'])
        self.assertEqual(self._rows(html), [
            (ga, '10:00', ea), (gc, '13:00', ea),
            (ga, '16:00', ea), (gb, '16:00', eb), (ga, '19:00', ea)])
        self.assertNotIn(self._name('ec'), html, 'no lesson -> not listed')
        self.assertNotIn('O-Group', html)
        for label in ('التاريخ', 'المجموعة', 'الحصة', 'جميع المجموعات',
                      'جميع الحصص', 'حاضر', 'غائب', 'متأخر', 'بعذر'):
            self.assertIn(label, html)

    def test_date_filtering(self):
        monday = self._page('uadmin', date=MONDAY)
        self.assertEqual(self._rows(monday),
                         [(self.ids['ga'], '17:00', str(self.ids['ea']))])
        friday = self._page('uadmin', date=FRIDAY)
        self.assertEqual(self._rows(friday), [])
        self.assertIn('لا توجد حصص مجدولة لهذا التاريخ.', friday)

    def test_group_filtering(self):
        html = self._page('uadmin', date=SUNDAY, group_id=self.ids['gb'])
        self.assertEqual(self._rows(html),
                         [(self.ids['gb'], '16:00', str(self.ids['eb']))])
        self.assertIn(self._name('eb'), html)
        # Another institute's group id drops out of scope (full list, no O).
        forged = self._page('uadmin', date=SUNDAY, group_id=self.ids['go'])
        self.assertEqual(len(self._rows(forged)), 5)
        self.assertNotIn('O-Group', forged)
        empty = self._page('uadmin', date=MONDAY, group_id=self.ids['gb'])
        self.assertIn('لا توجد حصص مجدولة لهذه المجموعة في التاريخ المحدد.', empty)

    def test_lesson_filtering(self):
        html = self._page('uadmin', date=SUNDAY, lesson='16:00-18:00')
        self.assertEqual(self._rows(html), [
            (self.ids['ga'], '16:00', str(self.ids['ea'])),
            (self.ids['gb'], '16:00', str(self.ids['eb']))])
        one = self._page('uadmin', date=SUNDAY, group_id=self.ids['gb'],
                         lesson='16:00-18:00')
        self.assertEqual(len(self._rows(one)), 1)
        bad = self._page('uadmin', date=SUNDAY, lesson='16:00-17:00')
        self.assertEqual(len(self._rows(bad)), 5, 'unknown lesson is ignored')

    # ── Writes: scheduled teacher, independence, idempotency ────────────────

    def test_teacher_with_multiple_lessons_gets_independent_rows(self):
        _r, flashes = self._save('uadmin',
                                 ('ga', '10:00', 'ea', 'present'),
                                 ('gc', '13:00', 'ea', 'excused'),
                                 ('ga', '16:00', 'ea', 'absent'),
                                 ('ga', '19:00', 'ea', 'late'))
        self.assertFalse(self._errors(flashes), flashes)
        rows = self._instr_rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({r[1] for r in rows}), 4, 'one session per lesson')
        self.assertEqual({r[2] for r in rows}, {self.ids['ea']})
        self.assertEqual({r[4] for r in rows}, {'scheduled'})
        by_start = {}
        with self.app.app_context():
            for _id, sid, _e, status, _role in rows:
                sess = db.session.get(InstituteAttendanceSession, sid,
                                      execution_options=OPTS)
                by_start[sess.start_time.strftime('%H:%M')] = status
        self.assertEqual(by_start, {'10:00': 'present', '13:00': 'excused',
                                    '16:00': 'absent', '19:00': 'late'})
        self.assertEqual(self._counts()['employee_attendance'], 0)

    def test_repeated_save_updates_same_row(self):
        self._save('uadmin', ('ga', '16:00', 'ea', 'present'))
        first = self._instr_rows()
        sessions = self._counts()['sessions']
        _r, flashes = self._save('uadmin', ('ga', '16:00', 'ea', 'present'))
        self.assertFalse(self._errors(flashes), flashes)
        self.assertEqual(self._instr_rows(), first, 'identical retry: no change')
        self._save('uadmin', ('ga', '16:00', 'ea', 'late'))
        second = self._instr_rows()
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0][0], first[0][0], 'same row updated')
        self.assertEqual(second[0][3], 'late')
        self.assertEqual(self._counts()['sessions'], sessions, 'no duplicate session')
        html = self._page('uadmin', date=SUNDAY, group_id=self.ids['ga'],
                          lesson='16:00-18:00')
        self.assertRegex(html, r'value="late"\s+checked')

    def test_unmarked_lessons_write_nothing(self):
        before = self._counts()
        _r, flashes = self._save('uadmin', ('ga', '16:00', 'ea', ''))
        self.assertTrue(self._errors(flashes))
        self.assertEqual(self._counts(), before)

    # ── Isolation / forged input ────────────────────────────────────────────

    def test_teacher_from_other_institute_or_wrong_teacher_rejected(self):
        before = self._counts()
        for emp in ('eo', 'eb', 'ec', 'es', 999999999):
            _r, flashes = self._save('uadmin', ('ga', '16:00', emp, 'present'))
            self.assertTrue(self._errors(flashes), emp)
        self.assertEqual(self._counts(), before, 'nothing written, no session')

    def test_group_or_lesson_from_other_institute_rejected(self):
        before = self._counts()
        _r, flashes = self._save('uadmin', ('go', '16:00', 'eo', 'present'))
        self.assertIn('الحصة المحددة غير صالحة', ' '.join(self._errors(flashes)))
        # Institute O's own admin cannot reach institute A's lesson either.
        _r, flashes = self._save('uoadm', ('ga', '16:00', 'ea', 'present'))
        self.assertTrue(self._errors(flashes))
        self.assertEqual(self._counts(), before)

    def test_forged_values_rejected_atomically(self):
        before = self._counts()
        cases = [
            ('abc', '16:00', 'ea', 'present'),          # non-numeric group
            ('ga', '25:00', 'ea', 'present'),           # impossible time
            ('ga', '15:00', 'ea', 'present'),           # unscheduled time
            ('ga', '16:00', 'ea', 'teleported'),        # unknown status
            (999999999, '16:00', 'ea', 'present'),      # nonexistent group
            ('ga', '16:00', 'abc', 'present'),          # non-numeric employee
        ]
        for case in cases:
            # A valid lesson first: the bad one must still reject everything.
            _r, flashes = self._save('uadmin', ('gb', '16:00', 'eb', 'present'),
                                     case)
            self.assertTrue(self._errors(flashes), case)
        # A lesson not scheduled on that DATE (Monday slot posted for Sunday).
        _r, flashes = self._save('uadmin', ('ga', '17:00', 'ea', 'present'))
        self.assertTrue(self._errors(flashes))
        # A malformed date.
        _r, flashes = self._save('uadmin', ('ga', '16:00', 'ea', 'present'),
                                 on='2025-13-40')
        self.assertTrue(self._errors(flashes))
        self.assertEqual(self._counts(), before)

    def test_database_enforces_tenant_and_uniqueness(self):
        self._save('uadmin', ('ga', '16:00', 'ea', 'present'))
        session = self._session('ga', time(16, 0))
        with self.app.app_context():
            # Employee of institute O against institute A's session.
            db.session.add(InstituteInstructorAttendance(
                school_id=self.ids['inst'], session_id=session.id,
                employee_id=self.ids['eo'], status='present',
                source='manual_admin'))
            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()
            # Duplicate (session, employee).
            db.session.add(InstituteInstructorAttendance(
                school_id=self.ids['inst'], session_id=session.id,
                employee_id=self.ids['ea'], status='absent',
                source='manual_admin'))
            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()
            # Unknown role is refused by the CHECK constraint.
            db.session.add(InstituteInstructorAttendance(
                school_id=self.ids['inst'], session_id=session.id,
                employee_id=self.ids['eb'], status='present', role='guest',
                source='manual_admin'))
            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    # ── History ─────────────────────────────────────────────────────────────

    def test_group_reassignment_does_not_rewrite_history(self):
        from app.blueprints.institute_groups import attendance_take  # noqa: F401
        self._save('uadmin', ('ga', '16:00', 'ea', 'present'))
        # 10:00 opened by STUDENT attendance before the change: its session
        # snapshots teacher A.
        with self.app.app_context():
            school = db.session.get(School, self.ids['inst'], execution_options=OPTS)
            group = db.session.get(InstituteStudyGroup, self.ids['ga'],
                                   execution_options=OPTS)
            att.get_or_create_session(
                school, group, att.find_occurrence(school, group,
                                                   date(2025, 9, 7), time(10, 0)))
            group.instructor_id = self.ids['eb']
            db.session.commit()

        html = self._page('uadmin', date=SUNDAY, group_id=self.ids['ga'])
        ea, eb = str(self.ids['ea']), str(self.ids['eb'])
        self.assertEqual(self._rows(html), [
            (self.ids['ga'], '10:00', ea),     # session snapshot
            (self.ids['ga'], '16:00', ea),     # recorded row
            (self.ids['ga'], '19:00', eb)])    # never opened -> current teacher
        self.assertEqual(self._instr_rows()[0][2], self.ids['ea'])

        # The recorded lesson cannot be re-attributed to the new teacher…
        _r, flashes = self._save('uadmin', ('ga', '16:00', 'eb', 'absent'))
        self.assertTrue(self._errors(flashes))
        # …but can still be corrected for its historical teacher.
        _r, flashes = self._save('uadmin', ('ga', '16:00', 'ea', 'late'))
        self.assertFalse(self._errors(flashes), flashes)
        rows = self._instr_rows()
        self.assertEqual([(r[2], r[3]) for r in rows], [(self.ids['ea'], 'late')])

    # ── Read-only viewing ───────────────────────────────────────────────────

    def test_viewing_and_filtering_write_nothing(self):
        before = self._counts()
        self._page('uadmin', date=SUNDAY)
        self._page('uadmin', date=MONDAY, group_id=self.ids['ga'])
        self._page('uadmin', date=SUNDAY, lesson='16:00-18:00')
        self._page('uadmin', date=SUNDAY, group_id=self.ids['go'], lesson='x')
        self.assertEqual(self._counts(), before)

    # ── Authorization ───────────────────────────────────────────────────────

    def test_role_without_manage_employees_is_refused(self):
        with self.assertRaises(Forbidden):
            self._page('ua', date=SUNDAY)
        before = self._counts()
        with self.assertRaises(Forbidden):
            self._save('ua', ('ga', '16:00', 'ea', 'present'))
        self.assertEqual(self._counts(), before)

    # ═════════════════════════════════════════════════════════════════════
    #  Daily employee attendance (employee, date) — institutes
    # ═════════════════════════════════════════════════════════════════════

    def _daily_page(self, user_key, **args):
        from app.blueprints.employees import daily_attendance
        with self.app.test_request_context('/employees/attendance/daily',
                                           query_string=args):
            self._login(user_key)
            try:
                return daily_attendance()
            finally:
                logout_user()

    def _daily_list(self, user_key, on=SUNDAY):
        from app.blueprints.employees import manual_attendance_list
        with self.app.test_request_context('/employees/attendance/manual/list',
                                           query_string={'date': on}):
            self._login(user_key)
            try:
                return manual_attendance_list().get_json()
            finally:
                logout_user()

    def _daily_save(self, user_key, statuses, on=SUNDAY, extra=None):
        """statuses: {employee_key_or_id: daily status}."""
        from app.blueprints.employees import manual_attendance_save
        data = {'att_date': on, 'emp_ids': []}
        for emp, status in statuses.items():
            eid = self.ids.get(emp, emp) if isinstance(emp, str) else emp
            data['emp_ids'].append(str(eid))
            data[f'status_{eid}'] = status
        data.update(extra or {})
        return self._post(manual_attendance_save,
                          '/employees/attendance/manual/save', user_key, data)

    def _report(self, user_key, view_name='attendance_report', *args, **query):
        from app.blueprints import employees as emp_bp
        with self.app.test_request_context('/employees/attendance-report',
                                           query_string=query):
            self._login(user_key)
            try:
                return getattr(emp_bp, view_name)(*args)
            finally:
                logout_user()

    def _ea(self, school_key='inst'):
        """{employee_id: (row id, status, source, check_in, year id, date)}."""
        with self.app.app_context():
            return {r.employee_id: (r.id, r.status, r.source, r.check_in,
                                    r.academic_year_id, r.date)
                    for r in EmployeeAttendance.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids[school_key]).all()}

    def _add_ea(self, emp_key, status, source, check_in=None):
        with self.app.app_context():
            db.session.add(EmployeeAttendance(
                employee_id=self.ids[emp_key], school_id=self.ids['inst'],
                academic_year_id=self.ids['iyear'], date=date(2025, 9, 7),
                status=status, source=source, check_in=check_in))
            db.session.commit()

    def _set(self, model, key, **values):
        with self.app.app_context():
            obj = db.session.get(model, self.ids[key], execution_options=OPTS)
            for name, value in values.items():
                setattr(obj, name, value)
            db.session.commit()

    def test_institute_has_two_separate_attendance_pages(self):
        lesson = self._page('uadmin', date=SUNDAY)
        self.assertIn('حضور المدرسين في الحصص', lesson)
        self.assertIn('lessonAttForm', lesson)
        self.assertNotIn('EMP_LIST_URL', lesson)

        daily = self._daily_page('uadmin', date=SUNDAY)
        self.assertIn('تسجيل حضور الموظفين يدوياً', daily)
        # The school's own daily list and save are reused, not duplicated.
        self.assertIn('EMP_LIST_URL = "/employees/attendance/manual/list"', daily)
        self.assertIn('action="/employees/attendance/manual/save"', daily)
        self.assertIn('الحسابات والمالية', daily, 'department filter option')
        self.assertNotIn('lessonAttForm', daily)
        self.assertNotIn('حضور المدرسين في الحصص', daily)

    def test_daily_list_holds_every_active_employee_of_this_institute(self):
        payload = self._daily_list('uadmin')
        listed = {e['id']: e for e in payload['employees']}
        # Teachers AND non-teaching staff, lessons or not.
        for key in ('ea', 'eb', 'ec', 'acc'):
            self.assertIn(self.ids[key], listed, key)
        for key in ('eo', 'es'):
            self.assertNotIn(self.ids[key], listed, f'{key}: other tenant')
        self.assertEqual(listed[self.ids['acc']]['job_title'], 'محاسب')
        self.assertIsNone(listed[self.ids['acc']]['existing'])
        with self.app.app_context():
            self.assertEqual(InstituteStudyGroup.query.execution_options(**OPTS)
                             .filter_by(instructor_id=self.ids['acc']).count(), 0,
                             'the non-teaching employee has no group at all')
        # Inactive employees drop out; another institute sees only its own.
        self._set(Employee, 'ec', status='inactive')
        self.assertNotIn(self.ids['ec'],
                         [e['id'] for e in self._daily_list('uadmin')['employees']])
        self.assertEqual([e['id'] for e in self._daily_list('uoadm')['employees']],
                         [self.ids['eo']])

    def test_daily_save_writes_employee_attendance_only(self):
        before = self._counts()
        resp, flashes = self._daily_save(
            'uadmin', {'acc': 'present', 'ea': 'present', 'eb': 'absent'})
        self.assertFalse(self._errors(flashes), flashes)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.location.endswith(
            '/employees/attendance/daily?date=2025-09-07'), resp.location)
        rows = self._ea()
        self.assertEqual({k: v[1:3] for k, v in rows.items()}, {
            self.ids['acc']: ('present', 'manual'),
            self.ids['ea']: ('present', 'manual'),
            self.ids['eb']: ('absent', 'manual')})
        self.assertEqual({(v[4], v[5]) for v in rows.values()},
                         {(self.ids['iyear'], date(2025, 9, 7))})
        after = self._counts()
        self.assertEqual((after['instr'], after['sessions']),
                         (before['instr'], before['sessions']),
                         'no lesson row and no lesson session')

        # A correction updates the same (employee, date) row.
        self._daily_save('uadmin', {'acc': 'absent'})
        again = self._ea()
        self.assertEqual(again[self.ids['acc']][:3],
                         (rows[self.ids['acc']][0], 'absent', 'manual'))
        self.assertEqual(self._counts()['employee_attendance'], 3)

    def test_lesson_and_daily_attendance_stay_independent(self):
        self._daily_save('uadmin', {'ea': 'present'})
        _r, flashes = self._save('uadmin', ('ga', '16:00', 'ea', 'absent'))
        self.assertFalse(self._errors(flashes), flashes)
        self.assertEqual([(r[2], r[3]) for r in self._instr_rows()],
                         [(self.ids['ea'], 'absent')])
        self.assertEqual({k: v[1] for k, v in self._ea().items()},
                         {self.ids['ea']: 'present'},
                         'the lesson status never touches the daily row')

    def test_daily_save_rejects_foreign_and_inactive_employees(self):
        self._set(Employee, 'ec', status='inactive')
        before = self._counts()
        self._daily_save('uadmin', {'eo': 'present', 'es': 'present',
                                    'ec': 'present', 999999999: 'present'})
        self.assertEqual(self._counts(), before)
        # A valid id beside a foreign one: only the valid one is written.
        self._daily_save('uadmin', {'acc': 'present', 'eo': 'present'})
        self.assertEqual(list(self._ea()), [self.ids['acc']])
        self.assertEqual(self._ea('oinst'), {})
        # Institute O's admin cannot touch institute A's employee.
        self._daily_save('uoadm', {'acc': 'absent'})
        self.assertEqual(self._ea()[self.ids['acc']][1], 'present')
        self.assertEqual(self._ea('oinst'), {})

    def test_daily_attendance_requires_manage_employees(self):
        from app.blueprints.employees import manual_attendance_list
        with self.assertRaises(Forbidden):
            self._daily_page('ua', date=SUNDAY)
        with self.app.test_request_context('/employees/attendance/manual/list'):
            self._login('ua')
            try:
                with self.assertRaises(Forbidden):
                    manual_attendance_list()
            finally:
                logout_user()
        before = self._counts()
        with self.assertRaises(Forbidden):
            self._daily_save('ua', {'ea': 'present'})
        self.assertEqual(self._counts(), before)

    def test_employee_shift_rules_apply_to_institute_employees(self):
        with self.app.app_context():
            school = db.session.get(School, self.ids['inst'], execution_options=OPTS)
            school.emp_enable_attendance_shifts = True
            shift = EmployeeAttendanceShift(
                school_id=school.id, name=f'دوام {self.suffix}',
                start_time=time(0, 0, 1), late_after_time=None,
                absent_after_time=time(23, 59, 59), is_active=True)
            db.session.add(shift)
            db.session.flush()
            db.session.get(Employee, self.ids['acc'],
                           execution_options=OPTS).shift_id = shift.id
            db.session.commit()
        _r, flashes = self._daily_save('uadmin', {'acc': 'present', 'ec': 'present'})
        rows = self._ea()
        self.assertEqual(rows[self.ids['acc']][1], 'present')
        self.assertNotIn(self.ids['ec'], rows, 'shift mode: no shift -> skipped')
        self.assertTrue(any(c == 'warning' and 'شفت' in m for c, m in flashes),
                        flashes)

    def test_employee_holiday_and_weekly_off_rules_unchanged(self):
        with self.app.app_context():
            hol = SchoolHoliday(school_id=self.ids['inst'], name='عطلة الموظفين',
                                start_date=date(2025, 9, 7),
                                end_date=date(2025, 9, 7),
                                applies_to='employees', is_active=True)
            db.session.add(hol)
            db.session.add(SchoolWeeklyOffSchedule(
                school_id=self.ids['inst'], audience='employees',
                off_days='0', effective_from=date(2025, 1, 1)))   # Monday
            db.session.commit()
            hol_id = hol.id
        # Employee holiday: ordinary statuses are not recorded; leave still is.
        self._daily_save('uadmin', {'acc': 'present', 'eb': 'on_leave'})
        self.assertEqual({k: v[1] for k, v in self._ea().items()},
                         {self.ids['eb']: 'on_leave'})
        # Employee weekly day off (Monday): nothing recorded.
        self._daily_save('uadmin', {'acc': 'present'}, on=MONDAY)
        self.assertNotIn(self.ids['acc'], self._ea())
        # A students-only holiday is a normal employee working day.
        with self.app.app_context():
            db.session.get(SchoolHoliday, hol_id).applies_to = 'students'
            db.session.commit()
        self._daily_save('uadmin', {'acc': 'present'})
        self.assertEqual(self._ea()[self.ids['acc']][1], 'present')

    def test_existing_automatic_device_and_leave_rows_are_shown(self):
        self._add_ea('acc', 'absent', 'auto_absence')
        self._add_ea('ea', 'present', 'aiface', check_in=time(8, 5))
        self._add_ea('eb', 'on_leave', 'leave')
        listed = {e['id']: e['existing']
                  for e in self._daily_list('uadmin')['employees']}
        self.assertEqual((listed[self.ids['acc']]['status'],
                          listed[self.ids['acc']]['source']),
                         ('absent', 'auto_absence'))
        self.assertEqual((listed[self.ids['ea']]['status'],
                          listed[self.ids['ea']]['source'],
                          listed[self.ids['ea']]['check_in']),
                         ('present', 'aiface', '08:05'))
        self.assertTrue(listed[self.ids['eb']]['is_on_leave'])
        self.assertIsNone(listed[self.ids['ec']])

    def test_final_automatic_absence_is_preserved(self):
        self._add_ea('acc', 'absent', 'auto_absence')
        row_id = self._ea()[self.ids['acc']][0]
        self._daily_save('uadmin', {'acc': 'present'})
        self._daily_save('uadmin', {'acc': 'present'},
                         extra={'checkout_employee_id': str(self.ids['acc'])})
        self.assertEqual(self._ea()[self.ids['acc']][:3],
                         (row_id, 'absent', 'auto_absence'))

    def test_employee_attendance_report_reused_for_institute(self):
        self._add_ea('acc', 'present', 'manual', check_in=time(8, 0))
        self._add_ea('ea', 'absent', 'auto_absence')
        html = self._report('uadmin', date_from=SUNDAY, date_to=SUNDAY)
        self.assertIn('تقرير حضور الموظفين', html)
        for key in ('acc', 'ea'):
            self.assertIn(self._name(key), html, key)
        self.assertNotIn(self._name('eo'), html)
        # Its "تسجيل يدوي" button opens the DAILY sheet, not the lesson page.
        self.assertRegex(html, r'href="/employees/attendance/daily" '
                               r'class="btn btn-primary btn-sm"')
        self.assertNotRegex(html, r'href="/employees/attendance/manual" '
                                  r'class="btn btn-primary btn-sm"')
        detail = self._report('uadmin', 'attendance_report_detail',
                              self.ids['acc'], date_from=SUNDAY, date_to=SUNDAY)
        self.assertIn('08:00', detail)
        with self.assertRaises(NotFound):
            self._report('uadmin', 'attendance_report_detail', self.ids['eo'])

    def test_institute_sidebar_shows_both_attendance_items(self):
        teacher = (r'<a href="/employees/attendance/manual"\s+class="(active)?">'
                   r'\s*<i class="bi bi-person-check"></i> حضور المدرسين\s*</a>')
        daily = (r'<a href="/employees/attendance/daily"\s+class="(active)?">'
                 r'\s*<i class="bi bi-calendar-check"></i> حضور الموظفين\s*</a>')
        employees_active = r'<a href="/employees/"\s+class="active"'

        lesson_page = self._page('uadmin', date=SUNDAY)
        self.assertEqual(re.search(teacher, lesson_page).group(1), 'active')
        self.assertIsNone(re.search(daily, lesson_page).group(1))

        for page in (self._daily_page('uadmin', date=SUNDAY),
                     self._report('uadmin', date_from=SUNDAY, date_to=SUNDAY)):
            self.assertIsNone(re.search(teacher, page).group(1))
            self.assertEqual(re.search(daily, page).group(1), 'active')
            self.assertNotRegex(page, employees_active)

    def test_normal_school_daily_flow_unchanged(self):
        school_page = self._page('usadm')
        self.assertNotIn('/employees/attendance/daily', school_page)
        self.assertRegex(school_page, r'<a href="/employees/attendance-report"'
                                      r'\s+class="">\s*<i class="bi bi-calendar-check">'
                                      r'</i> حضور الموظفين')
        # The institute-only URL sends a school to its original daily sheet.
        resp = self._daily_page('usadm', date=SUNDAY)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.location.endswith(
            '/employees/attendance/manual?date=2025-09-07'), resp.location)
        # Saving returns to the original page, writing the same daily row.
        resp, flashes = self._daily_save('usadm', {'es': 'present'})
        self.assertFalse(self._errors(flashes), flashes)
        self.assertTrue(resp.location.endswith(
            '/employees/attendance/manual?date=2025-09-07'), resp.location)
        self.assertEqual(self._ea('sch')[self.ids['es']][1:3], ('present', 'manual'))
        report = self._report('usadm', date_from=SUNDAY, date_to=SUNDAY)
        self.assertRegex(report, r'href="/employees/attendance/manual" '
                                 r'class="btn btn-primary btn-sm"')

    def test_no_migration_for_daily_institute_attendance(self):
        """employee_attendance already models (institute employee, date)."""
        import os
        d = 'migrations/versions'
        touching = sorted(
            fn for fn in os.listdir(d) if fn.endswith('.py')
            and "'employee_attendance'" in open(os.path.join(d, fn),
                                                encoding='utf-8').read())
        self.assertEqual(touching, [
            'a7b8c9d0e1f2_strict_tenant_year_isolation.py',
            'f5c78bef4cc4_initial_setup_for_postgres.py',
            'f6a7b8c9d0e1_multitenant_schools_and_year_scoping.py',
            'r2s3t4u5v6w7_add_device_employee_mapping.py'])
        self.assertEqual(
            {c.name for c in EmployeeAttendance.__table__.columns},
            {'id', 'employee_id', 'school_id', 'academic_year_id', 'date',
             'status', 'check_in', 'check_out', 'recorded_by', 'notes',
             'source', 'device_id', 'created_at'})

    # ── Student attendance unaffected ───────────────────────────────────────

    def test_student_attendance_unaffected(self):
        self._save('uadmin', ('ga', '16:00', 'ea', 'absent'))
        session = self._session('ga', time(16, 0))
        self.assertEqual(session.status, 'not_recorded',
                         'teacher status does not record the students')
        self.assertIsNone(session.recorded_at)
        with self.app.app_context():
            self.assertEqual(InstituteAttendanceRecord.query
                             .execution_options(**OPTS)
                             .filter_by(session_id=session.id).count(), 0)
            school = db.session.get(School, self.ids['inst'], execution_options=OPTS)
            group = db.session.get(InstituteStudyGroup, self.ids['ga'],
                                   execution_options=OPTS)
            occ = att.find_occurrence(school, group, date(2025, 9, 7), time(16, 0))
            same = att.get_or_create_session(school, group, occ)
            self.assertEqual(same.id, session.id, 'students reuse the session')
            att.submit_attendance(school, same, {self.ids['s_in']: 'present'},
                                  source='manual_admin',
                                  actor_user_id=self.ids['uadmin'], notify=False)
        rows = self._instr_rows()
        self.assertEqual([(r[2], r[3]) for r in rows], [(self.ids['ea'], 'absent')])
        self.assertEqual(self._session('ga', time(16, 0)).status, 'recorded')


for _inherited in list(vars(_Fixture)):
    if _inherited.startswith('test_'):
        setattr(InstituteInstructorAttendanceTest, _inherited, None)

del _Fixture


if __name__ == '__main__':
    unittest.main()

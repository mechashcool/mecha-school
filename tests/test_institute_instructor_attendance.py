# -*- coding: utf-8 -*-
"""Institute teacher attendance per lesson — focused guarantees only.

/employees/attendance/manual branches for School.is_institute into
Date -> Group -> Lesson -> scheduled teacher, stored in
institute_instructor_attendance and NEVER in employee_attendance.

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
                        InstituteAttendanceRecord, InstituteAttendanceSession,
                        InstituteInstructorAttendance, InstituteStudyGroup,
                        School)
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
            # Ordinary school: an admin and one employee.
            usadm = self._user(ids['sch'], 'attsadm', self.admin_role_id)
            es = self._employee(ids['sch'], 'S')
            db.session.commit()
            ids.update({'gc': gc.id, 'ec': ec.id, 'eo': eo.id,
                        'uoadm': uoadm.id, 'usadm': usadm.id, 'es': es.id})

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            schools = [self.ids['inst'], self.ids['oinst'], self.ids['sch']]
            for model in (InstituteInstructorAttendance, EmployeeAttendance):
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

    # ── Legacy daily sheet unusable for an institute ────────────────────────

    def test_legacy_daily_employee_sheet_refused_for_institute(self):
        from app.blueprints.employees import (manual_attendance_list,
                                              manual_attendance_save)
        with self.app.test_request_context('/employees/attendance/manual/list',
                                           query_string={'date': SUNDAY}):
            self._login('uadmin')
            resp, status = manual_attendance_list()
            logout_user()
        self.assertEqual(status, 404)

        resp, flashes = self._post(
            manual_attendance_save, '/employees/attendance/manual/save', 'uadmin',
            {'att_date': SUNDAY, 'emp_ids': [str(self.ids['ea'])],
             f'status_{self.ids["ea"]}': 'present'})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.location.endswith('/employees/attendance/manual'))
        self.assertIn('warning', [c for c, _m in flashes])
        self.assertEqual(self._counts()['employee_attendance'], 0)

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

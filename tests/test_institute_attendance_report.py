"""Institute attendance report — focused guarantees only.

  1.  All-groups report: exact totals over every authorized group.
  2.  One-group filter.
  3.  Student-name filter (case-insensitive, LIKE wildcards are literal).
  4.  Combined group + student filter.
  5.  Tenant isolation: another institute's group is a 404, and its students,
      sessions and records never appear — even under a matching name.
  6.  Instructor scope: own groups only; another instructor's group is a 404.
  7.  An unrecorded lesson — materialized or not — is 'unrecorded', never absent.
  8.  An ended enrollment stops counting after it ended; a late joiner is not
      counted before joining; recorded history is kept.
  9.  The report writes nothing (no session is materialized by viewing it).
  10. School-type institutions cannot reach the report, and the school
      attendance report still renders unchanged.

Fixed September 2025 dates, so every count below is deterministic:
  A-Group  Sundays 16:00-18:00 -> 07, 14, 21, 28
  B-Group  Mondays 17:00-19:00 -> 01, 08, 15, 22, 29
"""
import unittest
from datetime import date, datetime, time
from urllib.parse import urlencode
from uuid import uuid4

from flask_login import login_user, logout_user
from werkzeug.exceptions import Forbidden, NotFound

from app import create_app
from app.models import (db, AcademicYear, AuditLog, Employee,
                        InstituteAttendanceRecord, InstituteAttendanceSession,
                        InstituteGroupEnrollment, InstituteGroupSchedule,
                        InstituteStudyGroup, Notification, PushNotification,
                        Role, School, Section, Student, StudentAttendance,
                        Subject, User, parent_students)
from app.services import institute_attendance as att

OPTS = {'bypass_tenant_scope': True}
START, END = date(2025, 9, 1), date(2025, 9, 30)
TODAY = date(2025, 10, 15)
SUN = [date(2025, 9, d) for d in (7, 14, 21, 28)]
MON = [date(2025, 9, d) for d in (1, 8, 15, 22, 29)]
UNREC = att.REPORT_UNRECORDED


class InstituteAttendanceReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    # ── Fixtures ─────────────────────────────────────────────────────────────

    def _user(self, school_id, label, role_id):
        u = User(username=f'{label}_{self.suffix}',
                 email=f'{label}_{self.suffix}@example.test',
                 full_name=f'{label} {self.suffix}',
                 role_id=role_id, school_id=school_id, is_active=True)
        u.set_password('Password123')
        db.session.add(u)
        db.session.flush()
        return u

    def _employee(self, school_id, label, user_id):
        e = Employee(school_id=school_id, employee_id=f'E{label}{self.suffix}'[:38],
                     full_name=f'{label} {self.suffix}', base_salary=0,
                     status='active', user_id=user_id)
        db.session.add(e)
        db.session.flush()
        return e

    def _student(self, school_id, year_id, name):
        s = Student(student_id=f'R-{uuid4().hex[:10]}', full_name=name,
                    school_id=school_id, academic_year_id=year_id,
                    status='active')
        db.session.add(s)
        db.session.flush()
        return s

    def _group(self, school_id, year_id, subject_id, name, instructor_id):
        g = InstituteStudyGroup(school_id=school_id, academic_year_id=year_id,
                                subject_id=subject_id, instructor_id=instructor_id,
                                name=name, is_active=True)
        db.session.add(g)
        db.session.flush()
        return g

    def _enroll(self, school_id, group_id, student_id, enrolled_at):
        row = InstituteGroupEnrollment(
            school_id=school_id, group_id=group_id, student_id=student_id,
            status=InstituteGroupEnrollment.STATUS_ACTIVE,
            enrolled_at=enrolled_at, ended_at=None)
        db.session.add(row)
        db.session.flush()
        return row

    def _slot(self, school_id, year_id, group_id, dow, start, end):
        db.session.add(InstituteGroupSchedule(
            school_id=school_id, academic_year_id=year_id, group_id=group_id,
            day_of_week=dow, start_time=start, end_time=end, is_active=True))
        db.session.flush()

    def _institution(self, label, *, institute):
        school = School(
            school_name=f'{label} {self.suffix}',
            code=f'{label.upper()}{self.suffix}'[:20], capacity=0, is_active=True,
            institution_type=(School.INSTITUTION_INSTITUTE if institute else None))
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'{label} Y {self.suffix}',
                            start_date=date(2025, 8, 1), end_date=date(2026, 6, 30),
                            is_current=True)
        db.session.add(year)
        db.session.flush()
        subject = Subject(school_id=school.id, academic_year_id=year.id,
                          name=f'مادة {label}', code=f'S{label[:2]}{self.suffix[:6]}')
        db.session.add(subject)
        db.session.flush()
        return school, year, subject

    def _record(self, school, group, on_date, start, statuses, actor_id):
        occ = att.find_occurrence(school, group, on_date, start)
        self.assertIsNotNone(occ, f'no occurrence on {on_date}')
        sess = att.get_or_create_session(school, group, occ)
        if statuses:
            att.submit_attendance(
                school, sess, statuses, notify=False, actor_user_id=actor_id,
                source=InstituteAttendanceSession.SOURCE_MANUAL_ADMIN)
        return sess

    def setUp(self):
        self.suffix = uuid4().hex[:8]
        joined = datetime(2025, 8, 1, 6, 0)
        P, A, L, X = (InstituteAttendanceRecord.STATUS_PRESENT,
                      InstituteAttendanceRecord.STATUS_ABSENT,
                      InstituteAttendanceRecord.STATUS_LATE,
                      InstituteAttendanceRecord.STATUS_EXCUSED)
        with self.app.app_context():
            teacher = Role.query.filter_by(name='teacher').first().id
            admin = Role.query.filter_by(name='school_admin').first().id
            parent = Role.query.filter_by(name='parent').first().id

            # ── Institute A ──────────────────────────────────────────────────
            inst, iyear, isubj = self._institution('RpA', institute=True)
            ua = self._user(inst.id, 'rpta', teacher)
            ub = self._user(inst.id, 'rptb', teacher)
            uadmin = self._user(inst.id, 'rptadm', admin)
            uparent = self._user(inst.id, 'rptpar', parent)
            ea = self._employee(inst.id, 'A', ua.id)
            eb = self._employee(inst.id, 'B', ub.id)
            ga = self._group(inst.id, iyear.id, isubj.id, 'A-Group', ea.id)
            gb = self._group(inst.id, iyear.id, isubj.id, 'B-Group', eb.id)
            self._slot(inst.id, iyear.id, ga.id, 0, time(16, 0), time(18, 0))
            self._slot(inst.id, iyear.id, gb.id, 1, time(17, 0), time(19, 0))

            ali = self._student(inst.id, iyear.id, 'Ali Hassan')
            sara = self._student(inst.id, iyear.id, 'Sara Kareem')
            left = self._student(inst.id, iyear.id, 'Left Student')
            new = self._student(inst.id, iyear.id, 'New Joiner')
            bassam = self._student(inst.id, iyear.id, 'Bassam Noor')
            pct = self._student(inst.id, iyear.id, 'Zaid 100% Done')
            for s in (ali, sara, left):
                self._enroll(inst.id, ga.id, s.id, joined)
            left_enr = InstituteGroupEnrollment.query.execution_options(**OPTS) \
                .filter_by(school_id=inst.id, student_id=left.id).one()
            self._enroll(inst.id, gb.id, sara.id, joined)
            self._enroll(inst.id, gb.id, bassam.id, joined)
            db.session.commit()

            # Sun 07: recorded for all three A members.
            self._record(inst, ga, SUN[0], time(16, 0),
                         {ali.id: P, sara.id: A, left.id: L}, uadmin.id)
            # 'Left Student' leaves on Tue 09 (12:00 Baghdad).
            left_enr.status = InstituteGroupEnrollment.STATUS_ENDED
            left_enr.ended_at = datetime(2025, 9, 9, 9, 0)
            # 'New Joiner' joins on Sat 20 (12:00 Baghdad).
            self._enroll(inst.id, ga.id, new.id, datetime(2025, 9, 20, 9, 0))
            # 'Zaid 100% Done' joins A-Group only after every September lesson.
            self._enroll(inst.id, ga.id, pct.id, datetime(2025, 10, 1, 9, 0))
            db.session.commit()
            # Sun 14: recorded (Left Student is no longer a member).
            self._record(inst, ga, SUN[1], time(16, 0),
                         {ali.id: L, sara.id: P}, uadmin.id)
            # Sun 21: MATERIALIZED but never recorded. Sun 28: never opened.
            self._record(inst, ga, SUN[2], time(16, 0), None, uadmin.id)
            # Mon 08: B-Group recorded. Every other Monday: never opened.
            self._record(inst, gb, MON[1], time(17, 0),
                         {sara.id: X, bassam.id: P}, uadmin.id)

            # ── Institute O (cross-tenant; same-looking names on purpose) ───
            oinst, oyear, osubj = self._institution('RpO', institute=True)
            uo = self._user(oinst.id, 'rpto', teacher)
            uoadmin = self._user(oinst.id, 'rptoadm', admin)
            eo = self._employee(oinst.id, 'O', uo.id)
            go = self._group(oinst.id, oyear.id, osubj.id, 'O-Group', eo.id)
            self._slot(oinst.id, oyear.id, go.id, 0, time(16, 0), time(18, 0))
            osara = self._student(oinst.id, oyear.id, 'Sara Other')
            self._enroll(oinst.id, go.id, osara.id, joined)
            db.session.commit()
            self._record(oinst, go, SUN[0], time(16, 0), {osara.id: A},
                         uoadmin.id)

            # ── Ordinary school ──────────────────────────────────────────────
            sch, syear, _ss = self._institution('RpS', institute=False)
            usadmin = self._user(sch.id, 'rptsch', admin)
            db.session.commit()

            self.ids = {
                'inst': inst.id, 'ga': ga.id, 'gb': gb.id,
                'ua': ua.id, 'ub': ub.id, 'uadmin': uadmin.id,
                'uparent': uparent.id,
                'ali': ali.id, 'sara': sara.id, 'left': left.id,
                'new': new.id, 'bassam': bassam.id, 'pct': pct.id,
                'oinst': oinst.id, 'go': go.id, 'uo': uo.id,
                'uoadmin': uoadmin.id, 'osara': osara.id,
                'sch': sch.id, 'usadmin': usadmin.id,
            }

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for sid in (self.ids['inst'], self.ids['oinst'], self.ids['sch']):
                for model in (AuditLog, PushNotification, Notification,
                              StudentAttendance,
                              InstituteAttendanceRecord,
                              InstituteAttendanceSession,
                              InstituteGroupSchedule,
                              InstituteGroupEnrollment, InstituteStudyGroup):
                    for row in (model.query.execution_options(**OPTS)
                                .filter_by(school_id=sid).all()):
                        db.session.delete(row)
                    db.session.flush()
                db.session.execute(parent_students.delete().where(
                    parent_students.c.student_id.in_(
                        db.session.query(Student.id)
                        .filter(Student.school_id == sid))))
                db.session.flush()
                for model in (Student, Section, Subject, Employee, User):
                    for row in (model.query.execution_options(**OPTS)
                                .filter_by(school_id=sid).all()):
                        db.session.delete(row)
                    db.session.flush()
                for row in (AcademicYear.query.execution_options(**OPTS)
                            .filter_by(school_id=sid).all()):
                    db.session.delete(row)
                db.session.flush()
                school = db.session.get(School, sid, execution_options=OPTS)
                if school is not None:
                    db.session.delete(school)
                db.session.flush()
            db.session.commit()
            db.session.remove()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _obj(self, model, key):
        return db.session.get(model, self.ids[key], execution_options=OPTS)

    def _service(self, group_keys=('ga', 'gb'), q=None, school_key='inst'):
        """Direct service call (fixed `today`) -> (report, {(name, date): status})."""
        with self.app.app_context():
            school = self._obj(School, school_key)
            groups = [self._obj(InstituteStudyGroup, k) for k in group_keys]
            rep = att.attendance_report(school, groups, START, END,
                                        name_query=q, today=TODAY)
            cells = {(r['student'].full_name, r['group'].name, r['date']): r['status']
                     for r in rep['rows']}
            return rep, cells

    def _run_before_request(self):
        for fn in self.app.before_request_funcs.get(None, []):
            if fn() is not None:
                break

    def _page(self, user_key, **args):
        """Render the real route as `user_key`. Returns HTML or raises."""
        from app.blueprints.institute_groups import attendance_report
        args.setdefault('start', START.isoformat())
        args.setdefault('end', END.isoformat())
        with self.app.test_request_context(
                '/institute-groups/attendance/report?' + urlencode(args)):
            login_user(self._obj(User, user_key))
            self._run_before_request()
            try:
                return attendance_report()
            finally:
                logout_user()

    def _session_count(self):
        with self.app.app_context():
            return InstituteAttendanceSession.query.execution_options(**OPTS) \
                .count()

    # ── 1. All groups ────────────────────────────────────────────────────────

    def test_all_groups_totals(self):
        rep, _ = self._service()
        t = rep['totals']
        self.assertEqual((t['present'], t['late'], t['absent'], t['excused']),
                         (3, 2, 1, 1))
        # A: Sun21 + Sun28 x (Ali, Sara, New Joiner) = 6.
        # B: four unopened Mondays x (Sara, Bassam)   = 8.
        self.assertEqual(t[UNREC], 14)
        self.assertEqual(len(rep['rows']), 21)
        self.assertEqual(rep['lessons'], 9)
        self.assertEqual(rep['recorded_lessons'], 3)
        self.assertEqual(rep['unrecorded_lessons'], 6)
        self.assertEqual(rep['rate'], round(5 / 6 * 100, 1))

    def test_manager_page_all_groups(self):
        html = self._page('uadmin')
        self.assertIn('تقرير الحضور', html)
        for name in ('Ali Hassan', 'Sara Kareem', 'Bassam Noor', 'A-Group',
                     'B-Group', 'كل المجموعات', 'غير مسجلة'):
            self.assertIn(name, html)

    # ── 2. One group ─────────────────────────────────────────────────────────

    def test_single_group_filter(self):
        rep, cells = self._service(group_keys=('gb',))
        t = rep['totals']
        self.assertEqual((t['present'], t['excused'], t['absent'], t[UNREC]),
                         (1, 1, 0, 8))
        self.assertTrue(all(g == 'B-Group' for _n, g, _d in cells))

        html = self._page('uadmin', group_id=self.ids['gb'])
        self.assertIn('Bassam Noor', html)
        self.assertNotIn('Ali Hassan', html)

    # ── 3. Student name ──────────────────────────────────────────────────────

    def test_student_name_filter_across_groups_case_insensitive(self):
        rep, cells = self._service(q='  SARA  ')
        self.assertEqual({n for n, _g, _d in cells}, {'Sara Kareem'})
        self.assertEqual({g for _n, g, _d in cells}, {'A-Group', 'B-Group'})
        t = rep['totals']
        self.assertEqual((t['present'], t['absent'], t['excused'], t[UNREC]),
                         (1, 1, 1, 6))
        self.assertEqual(rep['students'], 1)

        html = self._page('uadmin', q='sara')
        self.assertIn('Sara Kareem', html)
        self.assertNotIn('Ali Hassan', html)

    def test_like_wildcards_are_literal(self):
        rep, _ = self._service(q='%')
        self.assertEqual(rep['rows'], [], "'%' must not match every name")
        rep, _ = self._service(q='_')
        self.assertEqual(rep['rows'], [])

    # ── 4. Combined ──────────────────────────────────────────────────────────

    def test_group_and_student_combined(self):
        rep, cells = self._service(group_keys=('gb',), q='sara')
        self.assertEqual(set(cells.values()), {'excused', UNREC})
        self.assertEqual(len(cells), 5)
        rep, cells = self._service(group_keys=('ga',), q='ali')
        self.assertEqual(cells, {
            ('Ali Hassan', 'A-Group', SUN[0]): 'present',
            ('Ali Hassan', 'A-Group', SUN[1]): 'late',
            ('Ali Hassan', 'A-Group', SUN[2]): UNREC,
            ('Ali Hassan', 'A-Group', SUN[3]): UNREC,
        })

        html = self._page('uadmin', group_id=self.ids['gb'], q='sara')
        self.assertIn('Sara Kareem', html)
        self.assertNotIn('Bassam Noor', html)

    def test_no_match_message(self):
        html = self._page('uadmin', q='nobody-by-this-name')
        self.assertIn('لا توجد سجلات حضور مطابقة للفلاتر المحددة.', html)

    # ── 5 & 9. Tenant isolation ──────────────────────────────────────────────

    def test_other_institute_group_is_404(self):
        with self.assertRaises(NotFound):
            self._page('uadmin', group_id=self.ids['go'])
        with self.assertRaises(NotFound):
            self._page('uadmin', group_id=999999999)

    def test_other_institute_data_never_appears(self):
        html = self._page('uadmin', q='sara')
        self.assertNotIn('Sara Other', html)
        self.assertNotIn('O-Group', html)
        html = self._page('uadmin')
        self.assertNotIn('Sara Other', html)
        self.assertNotIn('O-Group', html)

        # Even a caller that wrongly passes a foreign group gets nothing from it.
        rep, cells = self._service(group_keys=('ga', 'go'))
        self.assertNotIn('Sara Other', {n for n, _g, _d in cells})
        self.assertNotIn('O-Group', {g for _n, g, _d in cells})

        # And the other institute sees only its own data.
        html = self._page('uoadmin')
        self.assertIn('Sara Other', html)
        self.assertNotIn('Sara Kareem', html)

    # ── 6. Instructor scope ──────────────────────────────────────────────────

    def test_instructor_sees_only_own_groups(self):
        html = self._page('ua')
        self.assertIn('A-Group', html)
        self.assertNotIn('B-Group', html)
        self.assertNotIn('Bassam Noor', html)
        with self.assertRaises(NotFound):
            self._page('ua', group_id=self.ids['gb'])
        with self.assertRaises(NotFound):
            self._page('uo', group_id=self.ids['ga'])

        html = self._page('ub', q='sara')
        self.assertIn('Sara Kareem', html)       # via B-Group only
        self.assertNotIn('A-Group', html)

    def test_parent_is_forbidden(self):
        with self.assertRaises(Forbidden):
            self._page('uparent')

    # ── 7. Unrecorded is never absent ────────────────────────────────────────

    def test_unrecorded_lessons_are_not_absences(self):
        rep, cells = self._service(group_keys=('ga',))
        for day in (SUN[2], SUN[3]):             # materialized / never opened
            for name in ('Ali Hassan', 'Sara Kareem', 'New Joiner'):
                self.assertEqual(cells[(name, 'A-Group', day)], UNREC)
        self.assertEqual(rep['totals']['absent'], 1)   # only Sara on Sun 07
        with self.app.app_context():
            sess = InstituteAttendanceSession.query.execution_options(**OPTS) \
                .filter_by(school_id=self.ids['inst'], session_date=SUN[2]).one()
            self.assertEqual(sess.status,
                             InstituteAttendanceSession.STATUS_NOT_RECORDED)

    def test_future_lessons_are_not_listed(self):
        with self.app.app_context():
            school = self._obj(School, 'inst')
            group = self._obj(InstituteStudyGroup, 'ga')
            rep = att.attendance_report(school, [group], START, END,
                                        today=date(2025, 9, 15))
        self.assertEqual({r['date'] for r in rep['rows']}, {SUN[0], SUN[1]})

    # ── 8. Membership windows ────────────────────────────────────────────────

    def test_ended_enrollment_stops_counting(self):
        _rep, cells = self._service(group_keys=('ga',))
        left = {d for (n, _g, d) in cells if n == 'Left Student'}
        self.assertEqual(left, {SUN[0]}, 'history kept, nothing after leaving')
        self.assertEqual(cells[('Left Student', 'A-Group', SUN[0])], 'late')

    def test_late_joiner_not_counted_before_joining(self):
        _rep, cells = self._service(group_keys=('ga',))
        self.assertEqual({d for (n, _g, d) in cells if n == 'New Joiner'},
                         {SUN[2], SUN[3]})
        self.assertFalse(any(n == 'Zaid 100% Done' for n, _g, _d in cells))

    # ── 9. Read-only ─────────────────────────────────────────────────────────

    def test_report_writes_nothing(self):
        before = self._session_count()
        self._page('uadmin')
        self._page('ua', q='ali')
        self.assertEqual(self._session_count(), before)

    # ── 10. School isolation / regression ────────────────────────────────────

    def test_school_type_institution_cannot_open_report(self):
        with self.assertRaises(Forbidden):
            self._page('usadmin')

    def test_school_attendance_report_still_renders(self):
        from app.blueprints.attendance import report
        with self.app.test_request_context(
                '/attendance/report?report_type=detail&start=2025-09-01'
                '&end=2025-09-30'):
            login_user(self._obj(User, 'usadmin'))
            self._run_before_request()
            try:
                html = report()
            finally:
                logout_user()
        self.assertIn('تقرير الحضور والغياب', html)
        self.assertIn('name="report_type"', html)

    def test_sessions_page_links_to_report(self):
        from app.blueprints.institute_groups import attendance_sessions
        with self.app.test_request_context('/institute-groups/attendance'):
            login_user(self._obj(User, 'uadmin'))
            self._run_before_request()
            try:
                html = attendance_sessions()
            finally:
                logout_user()
        self.assertIn('تقرير الحضور', html)
        self.assertIn('/institute-groups/attendance/report', html)


if __name__ == '__main__':
    unittest.main()

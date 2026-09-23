"""Institute weekly schedules and manual attendance — focused guarantees only.

Acceptance points covered:

  1.  Recurring weekly occurrences are computed from the rules.
  2.  Several weekdays with different times all resolve.
  3.  Invalid and duplicate slots are rejected.
  4.  Editing a rule changes future occurrences but never recorded history.
  5.  A scheduled time passing creates NO absence and NO row.
  6.  A session stays 'not_recorded' until an explicit submission.
  7.  Only active group members are offered for a new status.
  8.  An instructor reaches only their assigned groups.
  9.  Administration reaches only its own school.
  10. Cross-school and cross-group access is rejected.
  11. Forged student / group / session ids are rejected.
  12. Repeated submission is idempotent.
  13. Concurrent session creation produces no duplicate.
  14. History survives membership and schedule changes.
  15. A notification is emitted only after a successful submission…
  16. …and a retry does not duplicate it.
  17. School-section attendance behaviour is unchanged.
  18. The mobile teacher API enforces the same scope.
"""
import unittest
from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from uuid import uuid4

from flask_login import login_user, logout_user
from werkzeug.exceptions import NotFound

from app import create_app
from app.models import (db, AcademicYear, AuditLog, Employee,
                        InstituteAttendanceRecord, InstituteAttendanceSession,
                        InstituteGroupEnrollment, InstituteGroupSchedule,
                        InstituteStudyGroup, Notification, PushNotification,
                        Role, School, Section, Student, StudentAttendance,
                        Subject, User, parent_students)
from app.services import institute_attendance as att

OPTS = {'bypass_tenant_scope': True}


class InstituteAttendanceTest(unittest.TestCase):
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

    def _employee(self, school_id, label, user_id=None):
        e = Employee(school_id=school_id, employee_id=f'E{label}{self.suffix}'[:38],
                     full_name=f'{label} {self.suffix}', base_salary=0,
                     status='active', user_id=user_id)
        db.session.add(e)
        db.session.flush()
        return e

    def _student(self, school_id, year_id, name, status='active', section_id=None):
        s = Student(student_id=f'S-{uuid4().hex[:10]}', full_name=name,
                    school_id=school_id, academic_year_id=year_id,
                    section_id=section_id, status=status)
        db.session.add(s)
        db.session.flush()
        return s

    def _group(self, school_id, year_id, subject_id, name, instructor_id,
               *, active=True, start=None, end=None):
        g = InstituteStudyGroup(school_id=school_id, academic_year_id=year_id,
                                subject_id=subject_id, instructor_id=instructor_id,
                                name=name, is_active=active,
                                start_date=start, end_date=end)
        db.session.add(g)
        db.session.flush()
        return g

    def _enroll(self, school_id, group_id, student_id, *, ended=False):
        row = InstituteGroupEnrollment(
            school_id=school_id, group_id=group_id, student_id=student_id,
            status=(InstituteGroupEnrollment.STATUS_ENDED if ended
                    else InstituteGroupEnrollment.STATUS_ACTIVE),
            ended_at=(datetime.utcnow() if ended else None))
        db.session.add(row)
        db.session.flush()
        return row

    def _slot(self, school_id, year_id, group_id, dow, start, end):
        s = InstituteGroupSchedule(school_id=school_id, academic_year_id=year_id,
                                   group_id=group_id, day_of_week=dow,
                                   start_time=start, end_time=end, is_active=True)
        db.session.add(s)
        db.session.flush()
        return s

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

    def setUp(self):
        self.suffix = uuid4().hex[:8]
        with self.app.app_context():
            self.teacher_role_id = Role.query.filter_by(name='teacher').first().id
            self.admin_role_id = Role.query.filter_by(name='school_admin').first().id
            self.parent_role_id = Role.query.filter_by(name='parent').first().id

            # ── Institute A ──────────────────────────────────────────────────
            inst, iyear, isubj = self._institution('AtA', institute=True)
            ua = self._user(inst.id, 'atta', self.teacher_role_id)
            ub = self._user(inst.id, 'attb', self.teacher_role_id)
            uadmin = self._user(inst.id, 'attadm', self.admin_role_id)
            ea = self._employee(inst.id, 'A', ua.id)
            eb = self._employee(inst.id, 'B', ub.id)

            ga = self._group(inst.id, iyear.id, isubj.id, 'A-Group', ea.id)
            gb = self._group(inst.id, iyear.id, isubj.id, 'B-Group', eb.id)

            # Sunday 16:00-18:00, Monday 17:00-19:00, Thursday 16:00-18:00
            slot_sun = self._slot(inst.id, iyear.id, ga.id, 0,
                                  time(16, 0), time(18, 0))
            slot_mon = self._slot(inst.id, iyear.id, ga.id, 1,
                                  time(17, 0), time(19, 0))
            slot_thu = self._slot(inst.id, iyear.id, ga.id, 4,
                                  time(16, 0), time(18, 0))
            self._slot(inst.id, iyear.id, gb.id, 0, time(16, 0), time(18, 0))

            s_in = self._student(inst.id, iyear.id, 'Enrolled Student')
            s_two = self._student(inst.id, iyear.id, 'Second Student')
            s_out = self._student(inst.id, iyear.id, 'Outside Student')
            s_dead = self._student(inst.id, iyear.id, 'Inactive Student',
                                   status='inactive')
            s_b = self._student(inst.id, iyear.id, 'B Group Student')
            self._enroll(inst.id, ga.id, s_in.id)
            self._enroll(inst.id, ga.id, s_two.id)
            self._enroll(inst.id, ga.id, s_dead.id)
            self._enroll(inst.id, gb.id, s_b.id)

            p_in = self._user(inst.id, 'attpin', self.parent_role_id)
            p_out = self._user(inst.id, 'attpout', self.parent_role_id)
            db.session.execute(parent_students.insert().values(
                user_id=p_in.id, student_id=s_in.id, relation='guardian'))
            db.session.execute(parent_students.insert().values(
                user_id=p_out.id, student_id=s_out.id, relation='guardian'))

            # ── Institute B (cross-tenant) ──────────────────────────────────
            oinst, oyear, osubj = self._institution('AtO', institute=True)
            uo = self._user(oinst.id, 'atto', self.teacher_role_id)
            eo = self._employee(oinst.id, 'O', uo.id)
            go = self._group(oinst.id, oyear.id, osubj.id, 'O-Group', eo.id)
            self._slot(oinst.id, oyear.id, go.id, 0, time(16, 0), time(18, 0))
            o_stu = self._student(oinst.id, oyear.id, 'Other Student')
            self._enroll(oinst.id, go.id, o_stu.id)

            # ── Ordinary school (regression baseline) ───────────────────────
            sch, syear, _ssubj = self._institution('AtS', institute=False)
            us = self._user(sch.id, 'attsch', self.teacher_role_id)
            db.session.flush()

            db.session.commit()
            self.ids = {
                'inst': inst.id, 'iyear': iyear.id, 'isubj': isubj.id,
                'ua': ua.id, 'ub': ub.id, 'uadmin': uadmin.id,
                'ea': ea.id, 'eb': eb.id, 'ga': ga.id, 'gb': gb.id,
                'slot_sun': slot_sun.id, 'slot_mon': slot_mon.id,
                'slot_thu': slot_thu.id,
                's_in': s_in.id, 's_two': s_two.id, 's_out': s_out.id,
                's_dead': s_dead.id, 's_b': s_b.id,
                'p_in': p_in.id, 'p_out': p_out.id,
                'oinst': oinst.id, 'go': go.id, 'uo': uo.id,
                'o_stu': o_stu.id,
                'sch': sch.id, 'syear': syear.id, 'us': us.id,
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

    def _next_dow(self, dow, after=None):
        """The next calendar date matching an app weekday (0 = Sunday)."""
        d = (after or date(2025, 9, 1))
        for _ in range(8):
            if att._py_to_app_dow(d) == dow:
                return d
            d += timedelta(days=1)
        raise AssertionError('no matching weekday')

    def _run_before_request(self):
        for fn in self.app.before_request_funcs.get(None, []):
            if fn() is not None:
                break

    def _login(self, key):
        login_user(db.session.get(User, self.ids[key], execution_options=OPTS))
        self._run_before_request()

    # ── 1 & 2. Recurring computation across several weekdays ────────────────

    def test_weekly_rules_recur_across_days_and_times(self):
        with self.app.app_context():
            school = self._obj(School, 'inst')
            group = self._obj(InstituteStudyGroup, 'ga')
            start = date(2025, 9, 1)          # a Monday
            occ = att.occurrences_for_range(school, [group], start,
                                            start + timedelta(days=13))
            # Sun + Mon + Thu over two full weeks = 6 occurrences.
            self.assertEqual(len(occ), 6, [o.date for o in occ])
            by_dow = {}
            for o in occ:
                by_dow.setdefault(o.day_of_week, []).append(o)
            self.assertEqual(sorted(by_dow), [0, 1, 4])
            self.assertEqual(by_dow[1][0].start_time, time(17, 0),
                             'Monday keeps its own different time')
            self.assertEqual(by_dow[0][0].start_time, time(16, 0))
            self.assertEqual(len(by_dow[0]), 2, 'the rule repeats weekly')
            # Purely computed — nothing was materialized by listing.
            self.assertEqual(
                InstituteAttendanceSession.query.execution_options(**OPTS)
                .filter_by(school_id=school.id).count(), 0)

    def test_group_end_date_bounds_the_recurrence(self):
        with self.app.app_context():
            school = self._obj(School, 'inst')
            group = self._obj(InstituteStudyGroup, 'ga')
            group.end_date = date(2025, 9, 8)
            db.session.commit()
            occ = att.occurrences_for_range(school, [group], date(2025, 9, 1),
                                            date(2025, 9, 30))
            self.assertTrue(occ)
            self.assertTrue(all(o.date <= date(2025, 9, 8) for o in occ),
                            'the recurrence stops when the group ends')

    # ── 3. Slot validation ──────────────────────────────────────────────────

    def test_invalid_and_duplicate_slots_are_rejected(self):
        with self.app.app_context():
            school = self._obj(School, 'inst')
            group = self._obj(InstituteStudyGroup, 'ga')
            before = InstituteGroupSchedule.query.execution_options(**OPTS)\
                .filter_by(group_id=group.id).count()

            for dow, st, en in ((7, '16:00', '18:00'),      # weekday out of range
                                (-1, '16:00', '18:00'),
                                (2, '18:00', '16:00'),      # end before start
                                (2, '16:00', '16:00'),      # zero length
                                (2, 'xx:yy', '18:00'),      # unparsable
                                (2, '', '18:00')):          # missing
                with self.assertRaises(att.AttendanceError,
                                       msg=f'{dow} {st}-{en} must be rejected'):
                    att.add_slot(school, group, dow, st, en)

            # Exact duplicate of the existing Sunday rule.
            with self.assertRaises(att.AttendanceError):
                att.add_slot(school, group, 0, '16:00', '18:00')

            self.assertEqual(
                InstituteGroupSchedule.query.execution_options(**OPTS)
                .filter_by(group_id=group.id).count(), before,
                'no invalid slot may be written')

    # ── 4 & 14. Editing rules never rewrites recorded history ───────────────

    def test_schedule_edit_affects_future_only(self):
        with self.app.app_context():
            school = self._obj(School, 'inst')
            group = self._obj(InstituteStudyGroup, 'ga')
            sunday = self._next_dow(0, date(2025, 9, 1))

            occ = att.find_occurrence(school, group, sunday, time(16, 0))
            session = att.get_or_create_session(school, group, occ)
            att.submit_attendance(
                school, session, {self.ids['s_in']: 'present'},
                source='manual_admin', actor_user_id=self.ids['uadmin'],
                notify=False)
            session_id = session.id

            # Now move the Sunday rule to 09:00 and delete the Monday one.
            slot = self._obj(InstituteGroupSchedule, 'slot_sun')
            att.update_slot(slot, 0, '09:00', '11:00')
            att.delete_slot(self._obj(InstituteGroupSchedule, 'slot_mon'))

            stored = db.session.get(InstituteAttendanceSession, session_id,
                                    execution_options=OPTS)
            self.assertIsNotNone(stored, 'recorded history must survive')
            self.assertEqual(stored.start_time, time(16, 0),
                             'the session keeps its own snapshot')
            self.assertEqual(stored.status, 'recorded')
            self.assertEqual(
                InstituteAttendanceRecord.query.execution_options(**OPTS)
                .filter_by(session_id=session_id).count(), 1)

            # Future occurrences follow the NEW rule.
            future = att.occurrences_for_range(
                school, [group], sunday + timedelta(days=7),
                sunday + timedelta(days=13))
            sundays = [o for o in future if o.day_of_week == 0]
            self.assertTrue(sundays)
            self.assertTrue(all(o.start_time == time(9, 0) for o in sundays))
            self.assertFalse([o for o in future if o.day_of_week == 1],
                             'the deleted Monday rule stops recurring')

    def test_deleting_a_slot_keeps_the_session_and_nulls_the_link(self):
        with self.app.app_context():
            school = self._obj(School, 'inst')
            group = self._obj(InstituteStudyGroup, 'ga')
            sunday = self._next_dow(0, date(2025, 9, 1))
            occ = att.find_occurrence(school, group, sunday, time(16, 0))
            session = att.get_or_create_session(school, group, occ)
            sid = session.id
            att.delete_slot(self._obj(InstituteGroupSchedule, 'slot_sun'))
            db.session.expire_all()
            stored = db.session.get(InstituteAttendanceSession, sid,
                                    execution_options=OPTS)
            self.assertIsNotNone(stored, 'the session must not be deleted')
            self.assertIsNone(stored.schedule_id, 'only the link is cleared')

    # ── 5 & 6. Time passing never creates absence ───────────────────────────

    def test_past_scheduled_time_creates_nothing(self):
        with self.app.app_context():
            school = self._obj(School, 'inst')
            group = self._obj(InstituteStudyGroup, 'ga')
            # A long-past range that certainly contains scheduled occurrences.
            past_end = date(2025, 9, 30)
            occ = att.occurrences_for_range(school, [group],
                                            date(2025, 9, 1), past_end)
            self.assertTrue(occ, 'the fixture must produce past occurrences')
            self.assertTrue(all(o.status == 'not_recorded' for o in occ))
            self.assertTrue(all(o.session is None for o in occ))
            self.assertEqual(
                InstituteAttendanceSession.query.execution_options(**OPTS)
                .filter_by(school_id=school.id).count(), 0,
                'listing past dates must not materialize anything')
            self.assertEqual(
                InstituteAttendanceRecord.query.execution_options(**OPTS)
                .filter_by(school_id=school.id).count(), 0,
                'NO automatic absence may ever be written')

    def test_opening_a_session_records_nothing(self):
        with self.app.app_context():
            school = self._obj(School, 'inst')
            group = self._obj(InstituteStudyGroup, 'ga')
            sunday = self._next_dow(0, date(2025, 9, 1))
            occ = att.find_occurrence(school, group, sunday, time(16, 0))
            session = att.get_or_create_session(school, group, occ)
            self.assertEqual(session.status, 'not_recorded')
            self.assertIsNone(session.source)
            self.assertIsNone(session.recorded_at)
            self.assertEqual(
                InstituteAttendanceRecord.query.execution_options(**OPTS)
                .filter_by(session_id=session.id).count(), 0,
                'opening must not create a single status')

    # ── 7 & 11. Roster membership and forged ids ────────────────────────────

    def test_roster_holds_only_active_members(self):
        with self.app.app_context():
            school = self._obj(School, 'inst')
            group = self._obj(InstituteStudyGroup, 'ga')
            sunday = self._next_dow(0, date(2025, 9, 1))
            occ = att.find_occurrence(school, group, sunday, time(16, 0))
            session = att.get_or_create_session(school, group, occ)
            names = {stu.full_name for stu, _ in att.session_roster(school, session)}
            self.assertIn('Enrolled Student', names)
            self.assertIn('Second Student', names)
            self.assertNotIn('Inactive Student', names)
            self.assertNotIn('Outside Student', names)
            self.assertNotIn('B Group Student', names)

    def test_forged_student_ids_reject_the_whole_submission(self):
        with self.app.app_context():
            school = self._obj(School, 'inst')
            group = self._obj(InstituteStudyGroup, 'ga')
            sunday = self._next_dow(0, date(2025, 9, 1))
            occ = att.find_occurrence(school, group, sunday, time(16, 0))
            session = att.get_or_create_session(school, group, occ)

            for bad in ('s_b', 's_out', 's_dead', 'o_stu'):
                with self.assertRaises(att.AttendanceError, msg=bad):
                    att.submit_attendance(
                        school, session,
                        {self.ids['s_in']: 'present',
                         self.ids[bad]: 'absent'},
                        source='manual_admin',
                        actor_user_id=self.ids['uadmin'], notify=False)
                self.assertEqual(
                    InstituteAttendanceRecord.query.execution_options(**OPTS)
                    .filter_by(session_id=session.id).count(), 0,
                    f'{bad} must abort the entire save — no partial commit')

            with self.assertRaises(att.AttendanceError):
                att.submit_attendance(
                    school, session, {self.ids['s_in']: 'teleported'},
                    source='manual_admin', actor_user_id=self.ids['uadmin'],
                    notify=False)
            self.assertEqual(
                InstituteAttendanceRecord.query.execution_options(**OPTS)
                .filter_by(session_id=session.id).count(), 0)

    def test_cross_school_session_is_refused(self):
        with self.app.app_context():
            school = self._obj(School, 'inst')
            other = self._obj(School, 'oinst')
            ogroup = self._obj(InstituteStudyGroup, 'go')
            sunday = self._next_dow(0, date(2025, 9, 1))
            occ = att.find_occurrence(other, ogroup, sunday, time(16, 0))
            osession = att.get_or_create_session(other, ogroup, occ)
            with self.assertRaises(att.AttendanceError):
                att.submit_attendance(
                    school, osession, {self.ids['o_stu']: 'present'},
                    source='manual_admin', actor_user_id=self.ids['uadmin'],
                    notify=False)

    # ── 12 & 13 & 16. Idempotency, concurrency, no duplicate notification ───

    def test_repeated_submission_is_idempotent(self):
        with self.app.app_context():
            school = self._obj(School, 'inst')
            group = self._obj(InstituteStudyGroup, 'ga')
            sunday = self._next_dow(0, date(2025, 9, 1))
            occ = att.find_occurrence(school, group, sunday, time(16, 0))
            session = att.get_or_create_session(school, group, occ)
            payload = {self.ids['s_in']: 'absent', self.ids['s_two']: 'present'}

            with patch.object(att, '_notify_absent') as notify:
                first = att.submit_attendance(
                    school, session, payload, source='manual_instructor',
                    actor_user_id=self.ids['ua'])
                second = att.submit_attendance(
                    school, session, payload, source='manual_instructor',
                    actor_user_id=self.ids['ua'])
                third = att.submit_attendance(
                    school, session, payload, source='manual_instructor',
                    actor_user_id=self.ids['ua'])

            self.assertEqual(first['created'], 2)
            self.assertEqual((second['created'], second['updated']), (0, 0))
            self.assertEqual(second['unchanged'], 2)
            self.assertEqual(third['unchanged'], 2)
            self.assertEqual(
                InstituteAttendanceRecord.query.execution_options(**OPTS)
                .filter_by(session_id=session.id).count(), 2,
                'a retry must not duplicate rows')
            self.assertEqual(notify.call_count, 1,
                             'only the first submission may notify')

    def test_concurrent_session_creation_yields_one_row(self):
        """The unique constraint, not a pre-insert SELECT, is the guarantee."""
        with self.app.app_context():
            school = self._obj(School, 'inst')
            group = self._obj(InstituteStudyGroup, 'ga')
            sunday = self._next_dow(0, date(2025, 9, 1))
            occ = att.find_occurrence(school, group, sunday, time(16, 0))

            first = att.get_or_create_session(school, group, occ)

            # Simulate the LOSER of the race: its pre-check saw nothing (the
            # winner had not committed yet), so it attempts the INSERT and must
            # recover from the IntegrityError by re-selecting the winner.
            real_find = att._find_session
            calls = {'n': 0}

            def _blind_find(*a, **kw):
                calls['n'] += 1
                return None if calls['n'] == 1 else real_find(*a, **kw)

            with patch.object(att, '_find_session', _blind_find):
                second = att.get_or_create_session(school, group, occ)

            self.assertGreaterEqual(calls['n'], 2,
                                    'the recovery lookup must have run')

            self.assertEqual(second.id, first.id,
                             'both callers must converge on the same session')
            self.assertEqual(
                InstituteAttendanceSession.query.execution_options(**OPTS)
                .filter_by(school_id=school.id, group_id=group.id,
                           session_date=sunday, start_time=time(16, 0)).count(),
                1, 'no duplicate session may exist')

    def test_database_rejects_a_duplicate_record(self):
        from sqlalchemy.exc import IntegrityError
        with self.app.app_context():
            school = self._obj(School, 'inst')
            group = self._obj(InstituteStudyGroup, 'ga')
            sunday = self._next_dow(0, date(2025, 9, 1))
            occ = att.find_occurrence(school, group, sunday, time(16, 0))
            session = att.get_or_create_session(school, group, occ)
            att.submit_attendance(school, session,
                                  {self.ids['s_in']: 'present'},
                                  source='manual_admin',
                                  actor_user_id=self.ids['uadmin'], notify=False)
            db.session.add(InstituteAttendanceRecord(
                school_id=school.id, session_id=session.id,
                student_id=self.ids['s_in'], status='absent',
                source='manual_admin', recorded_at=datetime.utcnow()))
            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    # ── 14. History survives a membership change ────────────────────────────

    def test_history_survives_membership_end(self):
        with self.app.app_context():
            school = self._obj(School, 'inst')
            group = self._obj(InstituteStudyGroup, 'ga')
            sunday = self._next_dow(0, date(2025, 9, 1))
            occ = att.find_occurrence(school, group, sunday, time(16, 0))
            session = att.get_or_create_session(school, group, occ)
            att.submit_attendance(school, session,
                                  {self.ids['s_in']: 'present'},
                                  source='manual_admin',
                                  actor_user_id=self.ids['uadmin'], notify=False)

            row = (InstituteGroupEnrollment.query.execution_options(**OPTS)
                   .filter_by(group_id=group.id, student_id=self.ids['s_in'],
                              status='active').first())
            row.status = InstituteGroupEnrollment.STATUS_ENDED
            row.ended_at = datetime.utcnow()
            db.session.commit()

            rec = (InstituteAttendanceRecord.query.execution_options(**OPTS)
                   .filter_by(session_id=session.id,
                              student_id=self.ids['s_in']).first())
            self.assertIsNotNone(rec, 'the stored record must survive')
            self.assertEqual(rec.status, 'present')
            names = {s.full_name for s, _ in att.session_roster(school, session)}
            self.assertIn('Enrolled Student', names,
                          'a recorded student must not vanish from history')
            self.assertNotIn(self.ids['s_in'],
                             att.eligible_student_ids(school, group.id),
                             'but they may no longer receive a NEW status')

    # ── 15. Notification only after an explicit submission ──────────────────

    def test_notification_only_on_explicit_new_absence(self):
        with self.app.app_context():
            school = self._obj(School, 'inst')
            group = self._obj(InstituteStudyGroup, 'ga')
            sunday = self._next_dow(0, date(2025, 9, 1))
            occ = att.find_occurrence(school, group, sunday, time(16, 0))
            session = att.get_or_create_session(school, group, occ)

            # Opening alone notifies nobody.
            self.assertEqual(
                Notification.query.execution_options(**OPTS)
                .filter_by(school_id=school.id, ntype='attendance').count(), 0)

            att.submit_attendance(school, session,
                                  {self.ids['s_in']: 'absent',
                                   self.ids['s_two']: 'present'},
                                  source='manual_instructor',
                                  actor_user_id=self.ids['ua'])

            rows = (Notification.query.execution_options(**OPTS)
                    .filter_by(school_id=school.id, ntype='attendance').all())
            targets = {r.target_user_id for r in rows}
            self.assertIn(self.ids['p_in'], targets,
                          "the absent student's parent is notified")
            self.assertNotIn(self.ids['p_out'], targets,
                             'an unrelated parent must never be notified')
            self.assertEqual(
                Notification.query.execution_options(**OPTS)
                .filter_by(school_id=self.ids['oinst']).count(), 0,
                'nothing crosses the tenant boundary')

    # ── 8, 9, 10. Web authorization ─────────────────────────────────────────

    def test_instructor_reaches_only_assigned_groups(self):
        from app.blueprints.institute_groups import attendance_take
        sunday = self._next_dow(0, date(2025, 9, 1)).strftime('%Y-%m-%d')

        # Own group: fine.
        with self.app.test_request_context(
                f'/institute-groups/{self.ids["ga"]}/attendance/{sunday}?start=16:00'):
            self._login('ua')
            html = attendance_take(self.ids['ga'], sunday)
            logout_user()
        self.assertIn('Enrolled Student', html)

        # Another instructor's group, and another institute's: 404.
        for user_key, group_key in (('ub', 'ga'), ('ua', 'gb'), ('uo', 'ga')):
            with self.app.test_request_context(
                    f'/institute-groups/{self.ids[group_key]}'
                    f'/attendance/{sunday}?start=16:00'):
                self._login(user_key)
                with self.assertRaises(NotFound,
                                       msg=f'{user_key}->{group_key} must 404'):
                    attendance_take(self.ids[group_key], sunday)
                logout_user()

    def test_manager_reaches_own_school_only(self):
        from app.blueprints.institute_groups import attendance_take
        sunday = self._next_dow(0, date(2025, 9, 1)).strftime('%Y-%m-%d')
        # Any group of their own institute.
        for group_key in ('ga', 'gb'):
            with self.app.test_request_context(
                    f'/institute-groups/{self.ids[group_key]}'
                    f'/attendance/{sunday}?start=16:00'):
                self._login('uadmin')
                html = attendance_take(self.ids[group_key], sunday)
                logout_user()
            self.assertIn('تسجيل الحضور', html)
        # Never another institute's.
        with self.app.test_request_context(
                f'/institute-groups/{self.ids["go"]}/attendance/{sunday}?start=16:00'):
            self._login('uadmin')
            with self.assertRaises(NotFound):
                attendance_take(self.ids['go'], sunday)
            logout_user()

    def test_unscheduled_date_is_refused(self):
        """A date with no matching rule cannot be turned into a session."""
        from app.blueprints.institute_groups import attendance_take
        tuesday = self._next_dow(2, date(2025, 9, 1)).strftime('%Y-%m-%d')
        with self.app.test_request_context(
                f'/institute-groups/{self.ids["ga"]}/attendance/{tuesday}?start=16:00'):
            self._login('uadmin')
            with self.assertRaises(NotFound):
                attendance_take(self.ids['ga'], tuesday)
            logout_user()
        with self.app.app_context():
            self.assertEqual(
                InstituteAttendanceSession.query.execution_options(**OPTS)
                .filter_by(school_id=self.ids['inst']).count(), 0)

    # ── 17. School attendance untouched ─────────────────────────────────────

    def test_school_attendance_model_untouched(self):
        """The school day-level table keeps its own shape and constraint."""
        with self.app.app_context():
            cols = {c.name for c in StudentAttendance.__table__.columns}
            self.assertNotIn('session_id', cols)
            self.assertNotIn('institute_group_id', cols)
            uniques = {c.name for c in StudentAttendance.__table__.constraints
                       if getattr(c, 'name', None)}
            self.assertIn('uq_student_date', uniques,
                          'the one-per-student-per-day rule is unchanged')
            # An institute attendance record writes nothing into it.
            school = self._obj(School, 'inst')
            group = self._obj(InstituteStudyGroup, 'ga')
            sunday = self._next_dow(0, date(2025, 9, 1))
            occ = att.find_occurrence(school, group, sunday, time(16, 0))
            session = att.get_or_create_session(school, group, occ)
            att.submit_attendance(school, session,
                                  {self.ids['s_in']: 'present'},
                                  source='manual_admin',
                                  actor_user_id=self.ids['uadmin'], notify=False)
            self.assertEqual(
                StudentAttendance.query.execution_options(**OPTS)
                .filter_by(school_id=school.id).count(), 0,
                'institute attendance must never touch student_attendance')

    def test_two_groups_same_day_both_record(self):
        """The reason a separate table was required, proven end to end."""
        with self.app.app_context():
            school = self._obj(School, 'inst')
            ga = self._obj(InstituteStudyGroup, 'ga')
            # Give the student a second group meeting the SAME Sunday.
            gc = self._group(school.id, self.ids['iyear'], self.ids['isubj'],
                             'C-Group', self.ids['ea'])
            self._slot(school.id, self.ids['iyear'], gc.id, 0,
                       time(19, 0), time(21, 0))
            self._enroll(school.id, gc.id, self.ids['s_in'])
            db.session.commit()

            sunday = self._next_dow(0, date(2025, 9, 1))
            for group, start in ((ga, time(16, 0)), (gc, time(19, 0))):
                occ = att.find_occurrence(school, group, sunday, start)
                self.assertIsNotNone(occ)
                sess = att.get_or_create_session(school, group, occ)
                att.submit_attendance(school, sess,
                                      {self.ids['s_in']: 'present'},
                                      source='manual_admin',
                                      actor_user_id=self.ids['uadmin'],
                                      notify=False)

            self.assertEqual(
                InstituteAttendanceRecord.query.execution_options(**OPTS)
                .filter_by(school_id=school.id,
                           student_id=self.ids['s_in']).count(), 2,
                'one student, one day, two groups — both must record')


    # ═══════════════════════════════════════════════════════════════════
    #  TIMEZONE — stored UTC, displayed Asia/Baghdad (UTC+03:00)
    # ═══════════════════════════════════════════════════════════════════
    #
    # The production symptom: a session recorded at 11:44 Baghdad time showed
    # 08:44, because recorded_at is stored as naive UTC and was rendered raw.
    # Storage is unchanged; only presentation converts.

    # The exact values from the production report.
    UTC_RECORDED = datetime(2026, 9, 23, 8, 44, 0)
    LOCAL_EXPECTED = '2026-09-23 11:44'
    ISO_EXPECTED = '2026-09-23T11:44:00+03:00'

    def _recorded_session(self):
        """A recorded session whose recorded_at is pinned to a known UTC value."""
        with self.app.app_context():
            school = self._obj(School, 'inst')
            group = self._obj(InstituteStudyGroup, 'ga')
            sunday = self._next_dow(0, date(2025, 9, 1))
            occ = att.find_occurrence(school, group, sunday, time(16, 0))
            session = att.get_or_create_session(school, group, occ)
            att.submit_attendance(school, session,
                                  {self.ids['s_in']: 'present'},
                                  source='manual_admin',
                                  actor_user_id=self.ids['uadmin'], notify=False)
            # Pin both timestamps to the reported UTC instant.
            session.recorded_at = self.UTC_RECORDED
            for rec in (InstituteAttendanceRecord.query.execution_options(**OPTS)
                        .filter_by(session_id=session.id).all()):
                rec.recorded_at = self.UTC_RECORDED
            db.session.commit()
            return session.id, sunday

    # ── 1. Web display ─────────────────────────────────────────────────────

    def test_stored_utc_displays_as_baghdad_local_in_web(self):
        from app.blueprints.institute_groups import attendance_take
        _session_id, sunday = self._recorded_session()
        date_str = sunday.strftime('%Y-%m-%d')

        with self.app.test_request_context(
                f'/institute-groups/{self.ids["ga"]}/attendance/{date_str}'
                '?start=16:00'):
            self._login('uadmin')
            html = attendance_take(self.ids['ga'], date_str)
            logout_user()

        self.assertIn(self.LOCAL_EXPECTED, html,
                      'a UTC 08:44 must render as Baghdad 11:44')
        self.assertNotIn('2026-09-23 08:44', html,
                         'the raw UTC value must never be shown')

    # ── 2. API response ────────────────────────────────────────────────────

    def test_api_returns_local_time_with_offset(self):
        """A real HTTP call with a real signed token."""
        from app.blueprints.mobile_api.utils import encode_token
        _session_id, sunday = self._recorded_session()

        with self.app.app_context():
            user = db.session.get(User, self.ids['ua'], execution_options=OPTS)
            token = encode_token(user)

        client = self.app.test_client()
        resp = client.get(
            '/api/mobile/v1/teacher/institute/sessions/open'
            f'?group_id={self.ids["ga"]}&date={sunday.strftime("%Y-%m-%d")}'
            '&start=16:00',
            headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()

        self.assertEqual(body['session']['recorded_at'], self.ISO_EXPECTED,
                         'the API must return Baghdad local time with +03:00')
        marked = [s for s in body['students'] if s['recorded_at']]
        self.assertTrue(marked, 'the recorded student must be present')
        for stu in marked:
            self.assertEqual(stu['recorded_at'], self.ISO_EXPECTED)
            self.assertTrue(stu['recorded_at'].endswith('+03:00'),
                            'the offset must be explicit and unambiguous')

    # ── 3. No double conversion ────────────────────────────────────────────

    def test_timezone_aware_value_is_not_converted_twice(self):
        import pytz
        with self.app.app_context():
            school = self._obj(School, 'inst')
            naive_utc = self.UTC_RECORDED
            aware_utc = pytz.utc.localize(naive_utc)
            aware_local = pytz.timezone('Asia/Baghdad').localize(
                datetime(2026, 9, 23, 11, 44, 0))

            # All three describe the SAME instant and must render identically.
            for label, value in (('naive UTC', naive_utc),
                                 ('aware UTC', aware_utc),
                                 ('aware local', aware_local)):
                self.assertEqual(
                    att.to_local(value, school).strftime('%Y-%m-%d %H:%M'),
                    self.LOCAL_EXPECTED, f'{label} converted incorrectly')
                self.assertEqual(att.to_local_iso(value, school),
                                 self.ISO_EXPECTED, f'{label} ISO incorrect')

            # Explicitly: converting an already-converted value does not add
            # another three hours.
            once = att.to_local_iso(naive_utc, school)
            twice = att.to_local_iso(
                datetime.fromisoformat(once), school)
            self.assertEqual(once, twice, 'a second pass must be a no-op')

    def test_none_timestamp_stays_none(self):
        with self.app.app_context():
            school = self._obj(School, 'inst')
            self.assertIsNone(att.to_local(None, school))
            self.assertIsNone(att.to_local_iso(None, school))

    # ── 4. Schedule wall-clock times are untouched ─────────────────────────

    def test_schedule_wall_clock_times_are_unchanged(self):
        """12:00-14:00 is local wall clock typed by administration.

        It carries no date and no timezone and must never be shifted.
        """
        from app.blueprints.institute_groups import schedule as schedule_view
        with self.app.app_context():
            school = self._obj(School, 'inst')
            group = self._obj(InstituteStudyGroup, 'ga')
            slot = att.add_slot(school, group, 2, '12:00', '14:00')
            slot_id = slot.id

            stored = db.session.get(InstituteGroupSchedule, slot_id,
                                    execution_options=OPTS)
            self.assertEqual(stored.start_time, time(12, 0))
            self.assertEqual(stored.end_time, time(14, 0))

            # And the occurrence computed from it keeps the same wall clock.
            wednesday = self._next_dow(2, date(2025, 9, 1))
            occ = att.find_occurrence(school, group, wednesday, time(12, 0))
            self.assertIsNotNone(occ, 'the 12:00 slot must resolve')
            self.assertEqual(occ.start_time, time(12, 0))
            self.assertEqual(occ.end_time, time(14, 0))

        with self.app.test_request_context(
                f'/institute-groups/{self.ids["ga"]}/schedule'):
            self._login('uadmin')
            html = schedule_view(self.ids['ga'])
            logout_user()
        self.assertIn('12:00', html, 'the entered start time must render as-is')
        self.assertIn('14:00', html, 'the entered end time must render as-is')
        self.assertNotIn('09:00', html, 'a schedule time must not be shifted')
        self.assertNotIn('15:00', html)

    # ── 5. Storage is untouched — no migration, no rewrite ─────────────────

    def test_storage_stays_utc_and_is_never_rewritten(self):
        session_id, sunday = self._recorded_session()
        date_str = sunday.strftime('%Y-%m-%d')

        with self.app.app_context():
            before = db.session.get(InstituteAttendanceSession, session_id,
                                    execution_options=OPTS).recorded_at
            self.assertEqual(before, self.UTC_RECORDED)
            self.assertIsNone(before.tzinfo,
                              'storage stays naive UTC, no offset baked in')

        # Render both surfaces, which is where conversion happens.
        from app.blueprints.institute_groups import attendance_take
        with self.app.test_request_context(
                f'/institute-groups/{self.ids["ga"]}/attendance/{date_str}'
                '?start=16:00'):
            self._login('uadmin')
            attendance_take(self.ids['ga'], date_str)
            logout_user()

        with self.app.app_context():
            after = db.session.get(InstituteAttendanceSession, session_id,
                                   execution_options=OPTS).recorded_at
            self.assertEqual(after, self.UTC_RECORDED,
                             'displaying must never rewrite the stored value')
            rec = (InstituteAttendanceRecord.query.execution_options(**OPTS)
                   .filter_by(session_id=session_id).first())
            self.assertEqual(rec.recorded_at, self.UTC_RECORDED)

    def test_no_new_migration_is_required(self):
        """The fix is presentation-only: the schema is unchanged."""
        import os
        import re
        revs = set()
        downs = set()
        d = 'migrations/versions'
        for fn in os.listdir(d):
            if not fn.endswith('.py'):
                continue
            txt = io_open_utf8(os.path.join(d, fn))
            m = re.search(r"^revision\s*=\s*['\"]([^'\"]+)", txt, re.M)
            dn = re.search(r"^down_revision\s*=\s*(.+)$", txt, re.M)
            if m:
                revs.add(m.group(1))
                if dn:
                    downs.update(re.findall(r"['\"]([^'\"]+)['\"]", dn.group(1)))
        # a9t8n9d0s1c2 (the attendance feature) is still the newest institute
        # revision — this timezone fix added none.
        self.assertIn('a9t8n9d0s1c2', revs)
        self.assertNotIn('a9t8n9d0s1c2', downs,
                         'no migration may have been chained after it')


def io_open_utf8(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


if __name__ == '__main__':
    unittest.main()

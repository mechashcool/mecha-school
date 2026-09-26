# -*- coding: utf-8 -*-
"""Institute group attendance honours the existing "إيقاف الطالب" rule.

The rule is the one school manual attendance applies: a StudentSuspension
whose start_date <= lesson date <= end_date. A suspended student is skipped —
no attendance row, no in-app notification, no outbox job — while the rest of
the submission is processed normally.

Fixture (institute, group A, s_in with a parent and two device tokens, s_two,
the outbox flag) is inherited from the institute outbox tests.
"""
import unittest
from datetime import timedelta
from unittest.mock import patch

from app.models import (db, InstituteAttendanceRecord, Notification,
                        StudentSuspension)
from app.services import institute_attendance as att

from tests.test_institute_attendance_outbox import InstituteOutboxTest, OPTS

# Same collection guard as the outbox module: keep the fixture, not its tests.
_Fixture = InstituteOutboxTest
del InstituteOutboxTest


class InstituteAttendanceSuspensionTest(_Fixture):

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            (StudentSuspension.query.execution_options(**OPTS)
             .filter_by(school_id=self.ids['inst'])
             .delete(synchronize_session=False))
            db.session.commit()
        super().tearDown()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _suspend(self, student_key, on_date):
        row = StudentSuspension(
            student_id=self.ids[student_key], school_id=self.ids['inst'],
            academic_year_id=self.ids['iyear'],
            start_date=on_date - timedelta(days=1),
            end_date=on_date + timedelta(days=1), reason='test')
        db.session.add(row)
        db.session.commit()
        return row.id

    def _records(self, session_id):
        return {r.student_id: r for r in
                InstituteAttendanceRecord.query.execution_options(**OPTS)
                .filter_by(session_id=session_id).all()}

    def _parent_notifications(self):
        return (Notification.query.execution_options(**OPTS)
                .filter_by(school_id=self.ids['inst'],
                           target_user_id=self.ids['p_in']).count())

    # ── Stopped student alone: every status refused, nothing written ────────

    def test_stopped_student_gets_no_record_notification_or_job(self):
        self._enable()
        with self.app.app_context():
            school, session = self._session()
            self._suspend('s_in', session.session_date)
            for status in InstituteAttendanceRecord.STATUSES:
                with self.assertRaises(att.AttendanceError):
                    att.submit_attendance(
                        school, session, {self.ids['s_in']: status},
                        source='manual_admin',
                        actor_user_id=self.ids['uadmin'])
            sid = session.id

        with self.app.app_context():
            self.assertEqual(self._records(sid), {})
            self.assertEqual(self._parent_notifications(), 0)
            self.assertEqual(self._jobs(), [])

    # ── Mixed submission: stopped skipped, active processed normally ────────

    def test_mixed_submission_skips_only_the_stopped_student(self):
        self._enable()
        with self.app.app_context():
            school, session = self._session()
            self._suspend('s_two', session.session_date)
            result = att.submit_attendance(
                school, session,
                {self.ids['s_in']: 'absent', self.ids['s_two']: 'absent'},
                source='manual_admin', actor_user_id=self.ids['uadmin'])
            sid = session.id

        self.assertEqual((result['created'], result['notified'],
                          result['skipped_suspended']), (1, 1, 1))
        with self.app.app_context():
            recs = self._records(sid)
            self.assertEqual(set(recs), {self.ids['s_in']})
            self.assertEqual(recs[self.ids['s_in']].status, 'absent')
            # The active absent student keeps the existing durable behaviour.
            self.assertEqual(self._parent_notifications(), 1)
            self.assertEqual(len(self._jobs()), 2)

    def test_mixed_submission_legacy_path_never_notifies_stopped(self):
        with self.app.app_context():
            school, session = self._session()
            self._suspend('s_in', session.session_date)
            with patch.object(att, '_notify_absent') as notify:
                result = att.submit_attendance(
                    school, session,
                    {self.ids['s_in']: 'absent', self.ids['s_two']: 'present'},
                    source='manual_admin', actor_user_id=self.ids['uadmin'])
            sid = session.id

        notify.assert_not_called()
        self.assertEqual((result['created'], result['skipped_suspended']), (1, 1))
        with self.app.app_context():
            self.assertEqual(set(self._records(sid)), {self.ids['s_two']})

    # ── History recorded before the stop is untouched ───────────────────────

    def test_record_saved_before_stop_is_not_modified(self):
        with self.app.app_context():
            school, session = self._session()
            att.submit_attendance(
                school, session, {self.ids['s_in']: 'present'},
                source='manual_admin', actor_user_id=self.ids['uadmin'])
            before = self._records(session.id)[self.ids['s_in']]
            snapshot = (before.status, before.recorded_at, before.source)

            self._suspend('s_in', session.session_date)
            with self.assertRaises(att.AttendanceError):
                att.submit_attendance(
                    school, session, {self.ids['s_in']: 'absent'},
                    source='manual_admin', actor_user_id=self.ids['uadmin'])
            sid = session.id

        with self.app.app_context():
            after = self._records(sid)[self.ids['s_in']]
            self.assertEqual((after.status, after.recorded_at, after.source),
                             snapshot)
            self.assertEqual(self._parent_notifications(), 0)

    # ── Reactivation: deleting the suspension restores normal attendance ────

    def test_reactivated_student_records_again(self):
        with self.app.app_context():
            school, session = self._session()
            susp_id = self._suspend('s_two', session.session_date)
            with self.assertRaises(att.AttendanceError):
                att.submit_attendance(
                    school, session, {self.ids['s_two']: 'present'},
                    source='manual_admin', actor_user_id=self.ids['uadmin'])

            # Same operation as attendance.delete_suspension.
            db.session.delete(db.session.get(StudentSuspension, susp_id,
                                             execution_options=OPTS))
            db.session.commit()

            result = att.submit_attendance(
                school, session, {self.ids['s_two']: 'present'},
                source='manual_admin', actor_user_id=self.ids['uadmin'])
            sid = session.id

        self.assertEqual((result['created'], result['skipped_suspended']), (1, 0))
        with self.app.app_context():
            self.assertEqual(self._records(sid)[self.ids['s_two']].status,
                             'present')


for _inherited in list(vars(_Fixture)):
    if _inherited.startswith('test_'):
        setattr(InstituteAttendanceSuspensionTest, _inherited, None)

del _Fixture


if __name__ == '__main__':
    unittest.main()

# -*- coding: utf-8 -*-
"""Durable transactional outbox for NORMAL SCHOOL / AI Face attendance.

What is being guaranteed, stated precisely:

  * ATOMICITY — the StudentAttendance change and the push-delivery jobs are one
    transaction. Both, or neither. A staging failure leaves no attendance row.
  * DURABLE AT-LEAST-ONCE with DEDUPLICATED ENQUEUEING — not exactly-once, for
    the same reason as the institute path: a crash after Firebase accepts a
    message but before the row is marked sent re-delivers it.
  * NO INLINE FIREBASE when AIFACE_ATTENDANCE_OUTBOX_ENABLED is on. Ever, and
    with no fallback: enqueue and inline-send together would double-deliver.
  * FLAG OFF IS BYTE-FOR-BYTE THE OLD PATH — the inline notification still
    fires and not a single outbox row is written.

Deliberately NOT asserted: an in-app ``Notification`` feed row. The inline AI
Face path has never created one — a device scan is push-only and has never
appeared in the parent feed (only the absence flows create feed rows). Adding
one here would change the feed and the unread badge the moment the flag is
switched on, so these tests pin the ABSENCE of feed rows on both paths.

Runs against the isolated local PostgreSQL test database. No real Firebase, no
production Redis, no production server, no real tokens.
"""
import unittest
from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import text

from app import create_app
from app.models import (
    db, AcademicYear, AttendanceDevice, DeviceStudentMapping, Grade,
    MobileDeviceToken, Notification, NotificationOutbox, PushNotification,
    Role, School, Section, Student, StudentAttendance, User, parent_students,
)
from app.services import ai_face_ws
from app.services import notification_outbox as outbox
from app.services import outbox_worker

OPTS = {'bypass_tenant_scope': True}


def _fake_token(tag):
    """A fabricated registration token. Never a production value."""
    return f'tkn-aiface-{tag}-' + ('x' * 60)


class _Sent:
    """Stand-in for fcm_service.TokenSendResult."""

    def __init__(self, ok=True, permanent=False, error=None, message_id='m-1'):
        self.ok = ok
        self.permanent = permanent
        self.error = error
        self.message_id = message_id if ok else None
        self.deactivated = False

    @property
    def transient(self):
        return (not self.ok) and (not self.permanent)


class AiFaceOutboxTest(unittest.TestCase):
    """Two schools, each with a device, a student, a parent and two tokens.

    School B exists only so every isolation assertion has somewhere to leak to.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    # ── Fixture ──────────────────────────────────────────────────────────────

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.ids = {}
        with self.app.app_context():
            parent_role = Role.query.filter_by(name='parent').first()
            self.assertIsNotNone(parent_role, 'seed roles before running')

            for tag in ('a', 'b'):
                school = School(
                    school_name=f'AiFace {tag} {self.suffix}',
                    code=f'AF{tag.upper()}{self.suffix[:6]}',
                    capacity=0, is_active=True,
                    # 07:30 late threshold, 13:00 departure: a 07:00 punch is
                    # 'present', a 08:00 punch is 'late', a 13:30 punch closes
                    # the day. Every time in these tests is chosen against
                    # these two values.
                    att_late_threshold=time(7, 30),
                    att_departure_time=time(13, 0))
                db.session.add(school)
                db.session.flush()

                year = AcademicYear(school_id=school.id,
                                    name=f'Y {tag} {self.suffix}',
                                    start_date=date(2025, 8, 1),
                                    end_date=date(2026, 6, 30),
                                    is_current=True)
                db.session.add(year)
                db.session.flush()

                grade = Grade(school_id=school.id, academic_year_id=year.id,
                              name=f'G{tag}{self.suffix[:4]}')
                db.session.add(grade)
                db.session.flush()

                section = Section(school_id=school.id, academic_year_id=year.id,
                                  grade_id=grade.id,
                                  name=f'S{tag}{self.suffix[:4]}', capacity=30)
                db.session.add(section)
                db.session.flush()

                student = Student(student_id=f'AF-{tag.upper()}-{self.suffix}',
                                  full_name=f'Pupil {tag} {self.suffix}',
                                  date_of_birth=date(2015, 1, 1), gender='male',
                                  school_id=school.id, academic_year_id=year.id,
                                  section_id=section.id, status='active')
                db.session.add(student)
                db.session.flush()

                parent = User(username=f'af_p_{tag}_{self.suffix}',
                              email=f'af_p_{tag}_{self.suffix}@example.test',
                              full_name=f'Parent {tag}', role_id=parent_role.id,
                              school_id=school.id, is_active=True)
                parent.set_password('Password123')
                db.session.add(parent)
                db.session.flush()
                db.session.execute(parent_students.insert().values(
                    user_id=parent.id, student_id=student.id))

                device = AttendanceDevice(
                    school_id=school.id, name=f'Dev {tag}',
                    device_type='aiface', device_scope='students',
                    ip_address='127.0.0.1', port=80, username='admin',
                    password='x', device_sn=f'SN-{tag.upper()}-{self.suffix}',
                    is_active=True)
                db.session.add(device)
                db.session.flush()

                # BOTH schools use enrollid '77'. A device may only ever resolve
                # its own school's mapping; this is what makes the cross-school
                # test meaningful rather than vacuous.
                mapping = DeviceStudentMapping(
                    school_id=school.id, device_id=device.id,
                    employee_no_string='77', student_id=student.id,
                    is_active=True)
                db.session.add(mapping)

                self.ids.update({
                    f'school_{tag}': school.id, f'year_{tag}': year.id,
                    f'grade_{tag}': grade.id, f'section_{tag}': section.id,
                    f'student_{tag}': student.id, f'parent_{tag}': parent.id,
                    f'device_{tag}': device.id, f'sn_{tag}': device.device_sn,
                })

            # Parent A has TWO registered phones; parent B has one.
            for label in ('a1', 'a2'):
                db.session.add(MobileDeviceToken(
                    user_id=self.ids['parent_a'], school_id=self.ids['school_a'],
                    fcm_token=_fake_token(f'{label}{self.suffix}'),
                    platform='android', device_name=f'Phone {label}',
                    is_active=True))
            db.session.add(MobileDeviceToken(
                user_id=self.ids['parent_b'], school_id=self.ids['school_b'],
                fcm_token=_fake_token(f'b1{self.suffix}'),
                platform='ios', device_name='Phone b1', is_active=True))
            db.session.commit()

        self.app.config['AIFACE_ATTENDANCE_OUTBOX_ENABLED'] = True
        # Institute must be unaffected by everything in this module.
        self.app.config['INSTITUTE_ATTENDANCE_OUTBOX_ENABLED'] = False

    def tearDown(self):
        self.app.config.pop('AIFACE_ATTENDANCE_OUTBOX_ENABLED', None)
        self.app.config.pop('INSTITUTE_ATTENDANCE_OUTBOX_ENABLED', None)
        with self.app.app_context():
            sids = [self.ids['school_a'], self.ids['school_b']]
            for table in ('notification_outbox', 'push_notifications',
                          'notifications', 'student_attendance',
                          'device_student_mappings', 'mobile_device_tokens',
                          'attendance_devices'):
                db.session.execute(
                    text(f'DELETE FROM {table} WHERE school_id = ANY(:s)'),
                    {'s': sids})
            db.session.execute(
                text('DELETE FROM parent_students WHERE student_id = ANY(:s)'),
                {'s': [self.ids['student_a'], self.ids['student_b']]})
            for model, keys in ((Student, ['student_a', 'student_b']),
                                (User, ['parent_a', 'parent_b']),
                                (Section, ['section_a', 'section_b']),
                                (Grade, ['grade_a', 'grade_b']),
                                (AcademicYear, ['year_a', 'year_b']),
                                (School, ['school_a', 'school_b'])):
                for key in keys:
                    row = db.session.get(model, self.ids[key],
                                         execution_options=OPTS)
                    if row is not None:
                        db.session.delete(row)
                db.session.flush()
            db.session.commit()

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _record(enrollid, punch_dt):
        """One raw device record, in the device's own wire format."""
        return {'enrollid': str(enrollid),
                'time': punch_dt.strftime('%Y-%m-%d %H:%M:%S'),
                'event': 0, 'inout': 0, 'mode': 0}

    def _deliver(self, tag, punch_dt, *, source_cmd='sendlog', enrollid=77):
        """Drive one device record through the real record processor."""
        with self.app.app_context():
            device = db.session.get(AttendanceDevice, self.ids[f'device_{tag}'],
                                    execution_options=OPTS)
            school = db.session.get(School, self.ids[f'school_{tag}'],
                                    execution_options=OPTS)
            return ai_face_ws._process_record_list(
                self.ids[f'sn_{tag}'], device, school,
                [self._record(enrollid, punch_dt)], source_cmd=source_cmd)

    def _jobs(self, tag='a'):
        with self.app.app_context():
            return (NotificationOutbox.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids[f'school_{tag}'])
                    .order_by(NotificationOutbox.id.asc()).all())

    def _attendance(self, tag='a'):
        with self.app.app_context():
            return (StudentAttendance.query.execution_options(**OPTS)
                    .filter_by(student_id=self.ids[f'student_{tag}']).all())

    def _feed_rows(self, tag='a'):
        with self.app.app_context():
            return (Notification.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids[f'school_{tag}']).all())

    @staticmethod
    def _today_at(hour, minute):
        return datetime.combine(date.today(), time(hour, minute))


# ═════════════════════════════════════════════════════════════════════════════
#  1. Feature flag
# ═════════════════════════════════════════════════════════════════════════════

class FlagBehaviourTest(AiFaceOutboxTest):

    def test_flag_defaults_to_false(self):
        """A deployment that sets nothing must keep the legacy path."""
        fresh = create_app('testing')
        self.assertFalse(fresh.config['AIFACE_ATTENDANCE_OUTBOX_ENABLED'])
        with fresh.app_context():
            self.assertFalse(outbox.aiface_enabled())

    def test_the_two_flags_are_independent(self):
        with self.app.app_context():
            self.app.config['AIFACE_ATTENDANCE_OUTBOX_ENABLED'] = True
            self.app.config['INSTITUTE_ATTENDANCE_OUTBOX_ENABLED'] = False
            self.assertTrue(outbox.aiface_enabled())
            self.assertFalse(outbox.enabled())

            self.app.config['AIFACE_ATTENDANCE_OUTBOX_ENABLED'] = False
            self.app.config['INSTITUTE_ATTENDANCE_OUTBOX_ENABLED'] = True
            self.assertFalse(outbox.aiface_enabled())
            self.assertTrue(outbox.enabled())

    def test_flag_off_uses_the_legacy_inline_path_and_writes_no_job(self):
        self.app.config['AIFACE_ATTENDANCE_OUTBOX_ENABLED'] = False
        with patch('app.services.notifications._NotificationService'
                   '.send_to_parents_of_student') as inline:
            processed, _, _, errors = self._deliver('a', self._today_at(7, 0))
        self.assertEqual((processed, errors), (1, 0))
        self.assertEqual(inline.call_count, 1)
        self.assertEqual(len(self._jobs('a')), 0)
        self.assertEqual(len(self._attendance('a')), 1)

    def test_flag_on_enqueues_and_never_sends_inline(self):
        with patch('app.services.notifications._NotificationService'
                   '.send_to_parents_of_student') as inline, \
             patch('app.services.fcm_service.send_push_to_user') as multi, \
             patch('app.services.fcm_service.send_to_device_token') as direct:
            processed, _, _, errors = self._deliver('a', self._today_at(7, 0))
        self.assertEqual((processed, errors), (1, 0))
        # NOT ONE Firebase call of any shape from the device thread.
        self.assertEqual(inline.call_count, 0)
        self.assertEqual(multi.call_count, 0)
        self.assertEqual(direct.call_count, 0)
        self.assertEqual(len(self._jobs('a')), 2)   # parent A has two phones

    def test_flag_on_writes_no_push_notification_log_from_the_device_thread(self):
        """The delivery log belongs to the worker now, not to the request."""
        self._deliver('a', self._today_at(7, 0))
        with self.app.app_context():
            rows = (PushNotification.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids['school_a']).all())
        self.assertEqual(rows, [])


# ═════════════════════════════════════════════════════════════════════════════
#  2. Atomicity
# ═════════════════════════════════════════════════════════════════════════════

class AtomicityTest(AiFaceOutboxTest):

    def test_attendance_and_jobs_commit_together(self):
        self._deliver('a', self._today_at(7, 0))
        rows = self._attendance('a')
        jobs = self._jobs('a')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, 'present')
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(j.status == NotificationOutbox.STATUS_PENDING
                            for j in jobs))

    def test_staging_failure_rolls_the_attendance_back(self):
        """The half-committed state this design exists to prevent."""
        boom = RuntimeError('staging exploded')
        with patch.object(outbox, 'stage_scan_deliveries', side_effect=boom):
            processed, _, _, errors = self._deliver('a', self._today_at(7, 0))
        self.assertEqual(processed, 0)
        self.assertEqual(errors, 1)
        # NO attendance row, NO job. Not one, not a partial one.
        self.assertEqual(self._attendance('a'), [])
        self.assertEqual(self._jobs('a'), [])

    def test_staging_failure_on_check_out_leaves_the_check_in_intact(self):
        """A rollback must undo the transition, not the earlier day's work."""
        self._deliver('a', self._today_at(7, 0))
        self.assertEqual(len(self._jobs('a')), 2)

        with patch.object(outbox, 'stage_scan_deliveries',
                          side_effect=RuntimeError('boom')):
            processed, _, _, errors = self._deliver('a', self._today_at(13, 30))
        self.assertEqual((processed, errors), (0, 1))

        rows = self._attendance('a')
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0].check_in)
        self.assertIsNone(rows[0].check_out)          # rolled back
        self.assertEqual(len(self._jobs('a')), 2)     # nothing extra staged

    def test_a_device_record_that_is_not_a_transition_stages_nothing(self):
        """already_checked_in must not reach the staging function at all."""
        self._deliver('a', self._today_at(7, 0))
        with patch.object(outbox, 'stage_scan_deliveries') as stage:
            processed, skipped, _, _ = self._deliver('a', self._today_at(9, 0))
        self.assertEqual(stage.call_count, 0)
        self.assertEqual((processed, skipped), (0, 1))


# ═════════════════════════════════════════════════════════════════════════════
#  3. Deduplication
# ═════════════════════════════════════════════════════════════════════════════

class DedupTest(AiFaceOutboxTest):

    def test_first_event_creates_the_expected_jobs(self):
        self._deliver('a', self._today_at(7, 0))
        jobs = self._jobs('a')
        self.assertEqual(len(jobs), 2)
        self.assertEqual({j.event_type for j in jobs},
                         {NotificationOutbox.EVENT_SCHOOL_ATTENDANCE_SCAN})
        self.assertEqual(len({j.dedup_key for j in jobs}), 2)

    def test_exact_replay_creates_nothing(self):
        punch = self._today_at(7, 0)
        self._deliver('a', punch)
        processed, skipped, _, errors = self._deliver('a', punch)
        self.assertEqual((processed, errors), (0, 0))
        self.assertEqual(skipped, 1)                 # classified 'duplicate'
        self.assertEqual(len(self._jobs('a')), 2)
        self.assertEqual(len(self._attendance('a')), 1)

    def test_the_same_record_through_the_other_protocol_path_creates_nothing(self):
        """sendlog then getnewlog for one scan is ordinary device behaviour."""
        punch = self._today_at(7, 0)
        self._deliver('a', punch, source_cmd='sendlog')
        self._deliver('a', punch, source_cmd='getnewlog')
        self.assertEqual(len(self._jobs('a')), 2)
        self.assertEqual(len(self._attendance('a')), 1)

    def test_repeated_polling_of_the_same_record_creates_nothing(self):
        punch = self._today_at(7, 0)
        self._deliver('a', punch, source_cmd='sendlog')
        for _ in range(4):
            self._deliver('a', punch, source_cmd='getnewlog')
        self.assertEqual(len(self._jobs('a')), 2)

    def test_replay_after_the_job_has_already_been_sent_creates_nothing(self):
        punch = self._today_at(7, 0)
        self._deliver('a', punch)
        with self.app.app_context():
            for job in (NotificationOutbox.query.execution_options(**OPTS)
                        .filter_by(school_id=self.ids['school_a']).all()):
                outbox.mark_sent(job)
            db.session.commit()

        self._deliver('a', punch)
        jobs = self._jobs('a')
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(j.status == NotificationOutbox.STATUS_SENT
                            for j in jobs))

    def test_check_out_is_a_second_transition_with_its_own_key(self):
        """Dedup must not swallow a genuinely different transition."""
        self._deliver('a', self._today_at(7, 0))
        self._deliver('a', self._today_at(13, 30))
        jobs = self._jobs('a')
        self.assertEqual(len(jobs), 4)
        self.assertEqual(len({j.dedup_key for j in jobs}), 4)
        actions = {j.dedup_key.split(':')[3] for j in jobs}
        self.assertEqual(actions, {'check_in', 'check_out'})

    def test_the_dedup_key_survives_a_concurrent_duplicate(self):
        """Two identical enqueues collide on the UNIQUE index, not in memory."""
        from sqlalchemy.exc import IntegrityError
        self._deliver('a', self._today_at(7, 0))
        with self.app.app_context():
            first = (NotificationOutbox.query.execution_options(**OPTS)
                     .filter_by(school_id=self.ids['school_a']).first())
            db.session.add(NotificationOutbox(
                school_id=first.school_id, event_type=first.event_type,
                user_id=first.user_id, device_token_id=first.device_token_id,
                title=first.title, body=first.body, data_json=first.data_json,
                ntype=first.ntype, dedup_key=first.dedup_key,
                status=NotificationOutbox.STATUS_PENDING, attempts=0))
            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()


# ═════════════════════════════════════════════════════════════════════════════
#  4. Parent token fan-out
# ═════════════════════════════════════════════════════════════════════════════

class TokenFanOutTest(AiFaceOutboxTest):

    def _set_tokens(self, count):
        with self.app.app_context():
            rows = (MobileDeviceToken.query.execution_options(**OPTS)
                    .filter_by(user_id=self.ids['parent_a'])
                    .order_by(MobileDeviceToken.id.asc()).all())
            for i, row in enumerate(rows):
                row.is_active = i < count
            db.session.commit()

    def test_zero_active_tokens_stages_nothing_and_still_saves_attendance(self):
        self._set_tokens(0)
        processed, _, _, errors = self._deliver('a', self._today_at(7, 0))
        self.assertEqual((processed, errors), (1, 0))
        self.assertEqual(len(self._attendance('a')), 1)
        self.assertEqual(len(self._jobs('a')), 0)

    def test_one_active_token_stages_one_job(self):
        self._set_tokens(1)
        self._deliver('a', self._today_at(7, 0))
        self.assertEqual(len(self._jobs('a')), 1)

    def test_two_active_tokens_stage_one_job_each(self):
        self._set_tokens(2)
        self._deliver('a', self._today_at(7, 0))
        jobs = self._jobs('a')
        self.assertEqual(len(jobs), 2)
        self.assertEqual(len({j.device_token_id for j in jobs}), 2)
        self.assertEqual({j.user_id for j in jobs}, {self.ids['parent_a']})

    def test_an_inactive_token_is_never_targeted(self):
        self._set_tokens(1)
        self._deliver('a', self._today_at(7, 0))
        with self.app.app_context():
            inactive = (MobileDeviceToken.query.execution_options(**OPTS)
                        .filter_by(user_id=self.ids['parent_a'],
                                   is_active=False).all())
            self.assertEqual(len(inactive), 1)
            targeted = {j.device_token_id for j in self._jobs('a')}
        self.assertNotIn(inactive[0].id, targeted)

    def test_a_student_with_no_linked_parent_stages_nothing(self):
        with self.app.app_context():
            db.session.execute(text(
                'DELETE FROM parent_students WHERE student_id = :s'),
                {'s': self.ids['student_a']})
            db.session.commit()
        processed, _, _, errors = self._deliver('a', self._today_at(7, 0))
        self.assertEqual((processed, errors), (1, 0))
        self.assertEqual(len(self._jobs('a')), 0)


# ═════════════════════════════════════════════════════════════════════════════
#  5. Isolation
# ═════════════════════════════════════════════════════════════════════════════

class IsolationTest(AiFaceOutboxTest):

    def test_a_device_cannot_write_another_schools_student(self):
        """Both schools map enrollid 77; device A must resolve only student A."""
        self._deliver('a', self._today_at(7, 0))
        self.assertEqual(len(self._attendance('a')), 1)
        self.assertEqual(self._attendance('b'), [])
        self.assertEqual(self._jobs('b'), [])

    def test_every_job_matches_its_own_school_on_all_three_axes(self):
        self._deliver('a', self._today_at(7, 0))
        self._deliver('b', self._today_at(7, 0))
        with self.app.app_context():
            for tag in ('a', 'b'):
                sid = self.ids[f'school_{tag}']
                jobs = (NotificationOutbox.query.execution_options(**OPTS)
                        .filter_by(school_id=sid).all())
                self.assertTrue(jobs)
                for job in jobs:
                    user = db.session.get(User, job.user_id,
                                          execution_options=OPTS)
                    token = db.session.get(MobileDeviceToken,
                                           job.device_token_id,
                                           execution_options=OPTS)
                    self.assertEqual(job.school_id, sid)
                    self.assertEqual(user.school_id, sid)
                    self.assertEqual(token.school_id, sid)

    def test_staging_refuses_a_cross_school_student(self):
        """Fail closed: a mismatched mapping rolls back, it does not deliver."""
        with self.app.app_context():
            school_a = db.session.get(School, self.ids['school_a'],
                                      execution_options=OPTS)
            student_b = db.session.get(Student, self.ids['student_b'],
                                       execution_options=OPTS)
            with self.assertRaises(ValueError) as caught:
                outbox.stage_scan_deliveries(
                    school_a, student_b, action='check_in',
                    on_date=date.today(), title='t', body='b', data={})
            self.assertIn('cross-school', str(caught.exception))
            db.session.rollback()
        self.assertEqual(self._jobs('a'), [])
        self.assertEqual(self._jobs('b'), [])

    # ── Source-path guard: device school vs mapped student school ────────────

    def _map_device_a_to_student_b(self, enrollid='88'):
        """A corrupt mapping: device A (school A) → student B (school B).

        The schema only has single-column FKs here, so such a row is
        representable; the record processor itself must refuse it.
        """
        with self.app.app_context():
            db.session.add(DeviceStudentMapping(
                school_id=self.ids['school_a'], device_id=self.ids['device_a'],
                employee_no_string=enrollid, student_id=self.ids['student_b'],
                is_active=True))
            db.session.commit()

    def _deliver_mismatch(self, *, times=1):
        """Drive the cross-school record, counting every downstream effect."""
        self._map_device_a_to_student_b()
        results = []
        with patch('app.services.attendance_service.process_attendance_punch') as engine, \
             patch.object(outbox, 'stage_scan_deliveries') as stage, \
             patch('app.services.notifications._NotificationService'
                   '.send_to_parents_of_student') as inline, \
             patch('app.services.fcm_service.send_push_to_user') as multi, \
             patch('app.services.fcm_service.send_to_device_token') as direct:
            for _ in range(times):
                results.append(self._deliver('a', self._today_at(7, 0),
                                             enrollid=88))
        return results, (engine, stage, inline, multi, direct)

    def _assert_nothing_happened(self, mocks):
        for m in mocks:
            self.assertEqual(m.call_count, 0)
        for tag in ('a', 'b'):
            self.assertEqual(self._attendance(tag), [])
            self.assertEqual(self._jobs(tag), [])
            self.assertEqual(self._feed_rows(tag), [])
        with self.app.app_context():
            pushes = (PushNotification.query.execution_options(**OPTS)
                      .filter(PushNotification.school_id.in_(
                          [self.ids['school_a'], self.ids['school_b']])).all())
        self.assertEqual(pushes, [])

    def test_same_school_mapping_still_records_attendance_normally(self):
        processed, skipped, unmatched, errors = self._deliver(
            'a', self._today_at(7, 0))
        self.assertEqual((processed, skipped, unmatched, errors), (1, 0, 0, 0))
        rows = self._attendance('a')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, 'present')
        self.assertEqual(len(self._jobs('a')), 2)

    def test_cross_school_mapping_is_rejected_at_the_source_flag_on(self):
        results, mocks = self._deliver_mismatch()
        self.assertEqual(results, [(0, 0, 1, 0)])
        self._assert_nothing_happened(mocks)

    def test_cross_school_mapping_is_rejected_at_the_source_flag_off(self):
        """The legacy inline path must not notify school B's parents either."""
        self.app.config['AIFACE_ATTENDANCE_OUTBOX_ENABLED'] = False
        results, mocks = self._deliver_mismatch()
        self.assertEqual(results, [(0, 0, 1, 0)])
        self._assert_nothing_happened(mocks)

    def test_repeated_cross_school_records_remain_harmless(self):
        results, mocks = self._deliver_mismatch(times=3)
        self.assertEqual(results, [(0, 0, 1, 0)] * 3)
        self._assert_nothing_happened(mocks)

    def test_a_mismatch_does_not_disturb_the_same_school_mapping(self):
        """Device A's valid enrollid 77 keeps working next to the bad row."""
        self._map_device_a_to_student_b()
        self.assertEqual(self._deliver('a', self._today_at(7, 0), enrollid=88),
                         (0, 0, 1, 0))
        self.assertEqual(self._deliver('a', self._today_at(7, 0)),
                         (1, 0, 0, 0))
        self.assertEqual(len(self._attendance('a')), 1)
        self.assertEqual(self._attendance('b'), [])
        self.assertEqual(self._jobs('b'), [])

    def test_a_parents_token_in_another_school_is_never_targeted(self):
        """A token row reassigned to another school must not receive this scan."""
        with self.app.app_context():
            tok = (MobileDeviceToken.query.execution_options(**OPTS)
                   .filter_by(user_id=self.ids['parent_a'])
                   .order_by(MobileDeviceToken.id.asc()).first())
            tok.school_id = self.ids['school_b']
            db.session.commit()
            moved = tok.id
        self._deliver('a', self._today_at(7, 0))
        jobs = self._jobs('a')
        self.assertEqual(len(jobs), 1)
        self.assertNotIn(moved, {j.device_token_id for j in jobs})

    def test_no_token_string_is_ever_stored_on_a_job(self):
        self._deliver('a', self._today_at(7, 0))
        with self.app.app_context():
            tokens = [t.fcm_token for t in
                      MobileDeviceToken.query.execution_options(**OPTS).all()]
        for job in self._jobs('a'):
            blob = f'{job.title}{job.body}{job.data_json}{job.dedup_key}'
            for value in tokens:
                self.assertNotIn(value, blob)


# ═════════════════════════════════════════════════════════════════════════════
#  6. Worker compatibility — the SAME worker, unchanged
# ═════════════════════════════════════════════════════════════════════════════

class WorkerCompatibilityTest(AiFaceOutboxTest):

    def test_the_existing_worker_claims_and_sends_an_aiface_job(self):
        self._deliver('a', self._today_at(7, 0))
        with self.app.app_context(), \
             patch('app.services.fcm_service.send_to_device_token',
                   return_value=_Sent(ok=True)) as fake:
            stats = outbox_worker.run_once('w-test', batch_size=10,
                                           lease_seconds=300, max_attempts=5)
        self.assertEqual(stats['claimed'], 2)
        self.assertEqual(stats['sent'], 2)
        self.assertEqual(fake.call_count, 2)
        jobs = self._jobs('a')
        self.assertTrue(all(j.status == NotificationOutbox.STATUS_SENT
                            for j in jobs))
        self.assertTrue(all(j.completed_at is not None for j in jobs))

    def test_the_worker_writes_the_delivery_log_for_an_aiface_job(self):
        self._deliver('a', self._today_at(7, 0))
        with self.app.app_context(), \
             patch('app.services.fcm_service.send_to_device_token',
                   return_value=_Sent(ok=True)):
            outbox_worker.run_once('w-test', batch_size=10,
                                   lease_seconds=300, max_attempts=5)
        with self.app.app_context():
            rows = (PushNotification.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids['school_a']).all())
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.status == 'sent' for r in rows))

    def test_a_transient_failure_retries_without_a_worker_restart(self):
        self._deliver('a', self._today_at(7, 0))
        with self.app.app_context(), \
             patch('app.services.fcm_service.send_to_device_token',
                   return_value=_Sent(ok=False, error='unavailable')):
            stats = outbox_worker.run_once('w-test', batch_size=10,
                                           lease_seconds=300, max_attempts=5)
        self.assertEqual(stats['retry'], 2)
        jobs = self._jobs('a')
        self.assertTrue(all(j.status == NotificationOutbox.STATUS_RETRY
                            for j in jobs))
        self.assertTrue(all(j.next_attempt_at is not None for j in jobs))
        self.assertTrue(all(j.attempts == 1 for j in jobs))

    def test_a_stale_lease_is_reclaimed(self):
        self._deliver('a', self._today_at(7, 0))
        with self.app.app_context():
            claimed = outbox.claim_batch('dead-worker', limit=10)
            self.assertEqual(len(claimed), 2)
            for row in claimed:
                row.locked_at = datetime.utcnow() - timedelta(seconds=900)
            db.session.commit()
            reclaimed = outbox.reclaim_stale('live-worker', lease_seconds=300)
        self.assertEqual(reclaimed, 2)
        self.assertTrue(all(j.status == NotificationOutbox.STATUS_PENDING
                            for j in self._jobs('a')))

    def test_the_worker_is_inert_while_both_flags_are_off(self):
        self._deliver('a', self._today_at(7, 0))
        self.app.config['AIFACE_ATTENDANCE_OUTBOX_ENABLED'] = False
        self.app.config['INSTITUTE_ATTENDANCE_OUTBOX_ENABLED'] = False
        with self.app.app_context(), \
             patch('app.services.fcm_service.send_to_device_token') as fake:
            stats = outbox_worker.run_once('w-test', batch_size=10,
                                           lease_seconds=300, max_attempts=5)
        self.assertTrue(stats['disabled'])
        self.assertEqual((stats['claimed'], fake.call_count), (0, 0))
        self.assertTrue(all(j.status == NotificationOutbox.STATUS_PENDING
                            for j in self._jobs('a')))

    def test_the_worker_runs_for_aiface_alone(self):
        """AI Face must not need the institute flag to get its jobs delivered."""
        self._deliver('a', self._today_at(7, 0))
        self.app.config['INSTITUTE_ATTENDANCE_OUTBOX_ENABLED'] = False
        with self.app.app_context():
            self.assertTrue(outbox.any_enabled())
            with patch('app.services.fcm_service.send_to_device_token',
                       return_value=_Sent(ok=True)):
                stats = outbox_worker.run_once('w-test', batch_size=10,
                                               lease_seconds=300,
                                               max_attempts=5)
        self.assertEqual(stats['sent'], 2)


# ═════════════════════════════════════════════════════════════════════════════
#  7. Preserved AI Face behaviour (structural regression)
# ═════════════════════════════════════════════════════════════════════════════

class PreservedBehaviourTest(AiFaceOutboxTest):

    def test_check_in_status_rules_are_untouched(self):
        """07:00 is present, and the late threshold still classifies 08:00."""
        self._deliver('a', self._today_at(7, 0))
        self.assertEqual(self._attendance('a')[0].status, 'present')

        self._deliver('b', self._today_at(8, 0))
        self.assertEqual(self._attendance('b')[0].status, 'late')

    def test_the_dedup_tag_is_still_written_into_notes(self):
        punch = self._today_at(7, 0)
        self._deliver('a', punch)
        notes = self._attendance('a')[0].notes or ''
        self.assertIn(f'AI Face {punch.strftime("%Y-%m-%d %H:%M:%S")}', notes)

    def test_check_out_still_requires_the_departure_window(self):
        self._deliver('a', self._today_at(7, 0))
        self._deliver('a', self._today_at(12, 0))          # before departure
        self.assertIsNone(self._attendance('a')[0].check_out)
        self._deliver('a', self._today_at(13, 30))         # after departure
        self.assertIsNotNone(self._attendance('a')[0].check_out)

    def test_a_non_attendance_event_is_still_skipped(self):
        with self.app.app_context():
            device = db.session.get(AttendanceDevice, self.ids['device_a'],
                                    execution_options=OPTS)
            school = db.session.get(School, self.ids['school_a'],
                                    execution_options=OPTS)
            rec = self._record(77, self._today_at(7, 0))
            rec['event'] = 3
            result = ai_face_ws._process_record_list(
                self.ids['sn_a'], device, school, [rec])
        self.assertEqual(result, (0, 1, 0, 0))
        self.assertEqual(self._attendance('a'), [])
        self.assertEqual(self._jobs('a'), [])

    def test_an_unmatched_enrollid_is_still_unmatched(self):
        result = self._deliver('a', self._today_at(7, 0), enrollid=999)
        self.assertEqual(result, (0, 0, 1, 0))
        self.assertEqual(self._jobs('a'), [])

    def test_a_scan_still_creates_no_parent_feed_row_on_either_path(self):
        """Pinned deliberately: an AI Face scan has never been a feed entry."""
        self._deliver('a', self._today_at(7, 0))
        self.assertEqual(self._feed_rows('a'), [])

        self.app.config['AIFACE_ATTENDANCE_OUTBOX_ENABLED'] = False
        self._deliver('b', self._today_at(7, 0))
        self.assertEqual(self._feed_rows('b'), [])

    def test_the_message_wording_is_identical_on_both_paths(self):
        """The outbox stores exactly what the inline path would have sent."""
        with patch('app.services.notifications._NotificationService'
                   '.send_to_parents_of_student') as inline:
            self.app.config['AIFACE_ATTENDANCE_OUTBOX_ENABLED'] = False
            self._deliver('b', self._today_at(7, 0))
        legacy_args = inline.call_args

        self.app.config['AIFACE_ATTENDANCE_OUTBOX_ENABLED'] = True
        self._deliver('a', self._today_at(7, 0))
        job = self._jobs('a')[0]

        self.assertEqual(job.title, legacy_args.args[1])
        # Only the pupil's name differs between the two fixture schools.
        self.assertEqual(job.body.replace(f'Pupil a {self.suffix}', 'NAME'),
                         legacy_args.args[2].replace(
                             f'Pupil b {self.suffix}', 'NAME'))
        self.assertEqual(job.ntype, 'attendance')

    def test_the_stored_payload_carries_the_full_mobile_contract(self):
        import json
        self._deliver('a', self._today_at(7, 0))
        data = json.loads(self._jobs('a')[0].data_json)
        self.assertEqual(data['action'], 'check_in')
        self.assertEqual(data['status'], 'present')
        self.assertEqual(data['source'], 'aiface')
        self.assertEqual(data['screen'], 'attendance')
        self.assertEqual(data['ntype'], 'attendance')
        self.assertEqual(data['type'], 'notification')
        self.assertEqual(data['route'], '/parent/notifications')
        self.assertEqual(data['device_sn'], self.ids['sn_a'])
        self.assertEqual(data['student_id'], str(self.ids['student_a']))
        self.assertEqual(data['date'], date.today().isoformat())

    def test_process_attendance_punch_is_unchanged_without_a_hook(self):
        """The shared engine keeps its old behaviour for every other caller."""
        from app.services.attendance_service import process_attendance_punch
        with self.app.app_context():
            student = db.session.get(Student, self.ids['student_a'],
                                     execution_options=OPTS)
            school = db.session.get(School, self.ids['school_a'],
                                    execution_options=OPTS)
            action, row = process_attendance_punch(
                student, school, self._today_at(7, 0), source='api')
            self.assertEqual(action, 'check_in')
            self.assertIsNotNone(row.id)
            again, _ = process_attendance_punch(
                student, school, self._today_at(9, 0), source='api')
            self.assertEqual(again, 'already_checked_in')
        self.assertEqual(self._jobs('a'), [])


if __name__ == '__main__':
    unittest.main()

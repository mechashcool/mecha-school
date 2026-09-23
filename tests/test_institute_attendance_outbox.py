# -*- coding: utf-8 -*-
"""Durable transactional outbox for institute attendance notifications.

What is being guaranteed, stated precisely:

  * ATOMICITY — attendance rows, the parent in-app Notification rows and the
    push-delivery jobs are one transaction. All of them, or none.
  * DURABLE AT-LEAST-ONCE with DEDUPLICATED ENQUEUEING — not exactly-once. A
    crash after Firebase accepts a message but before the row is marked sent
    re-delivers it. Push notifications are display-only, so a rare duplicate is
    the right trade against silently losing one.
  * NO INLINE FIREBASE when the flag is on. Ever.

The fixture (institute, groups, slots, students, parents, a second institute
and an ordinary school) is inherited from InstituteAttendanceTest so the outbox
is exercised against exactly the same data shape as the existing behaviour.

Runs against the isolated local PostgreSQL test database — NOT SQLite, because
FOR UPDATE SKIP LOCKED and the concurrency guarantees cannot be tested on
SQLite. No real Firebase, no production Redis, no production server.
"""
import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app.models import (db, MobileDeviceToken, Notification,
                        NotificationOutbox, PushNotification, Student,
                        InstituteAttendanceRecord, InstituteAttendanceSession,
                        parent_students)
from app.services import institute_attendance as att
from app.services import notification_outbox as outbox
from app.services import outbox_worker

from tests.test_institute_attendance import InstituteAttendanceTest, OPTS

# Importing the parent TestCase brings its NAME into this module, and both
# unittest and pytest would collect it here as well — running the whole
# institute attendance suite a second time. Alias it privately and remove the
# public name so only THIS module's tests are collected.
_ParentFixture = InstituteAttendanceTest
del InstituteAttendanceTest


# A fabricated token. Never a production value, and long enough to prove the
# 16-character truncation in logs actually truncates.
def _fake_token(tag):
    return f'tkn-{tag}-' + ('x' * 60)


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


class InstituteOutboxTest(_ParentFixture):
    """Adds device tokens + the outbox flag on top of the inherited fixture."""

    # ── Fixture extensions ───────────────────────────────────────────────────

    def setUp(self):
        super().setUp()
        with self.app.app_context():
            # p_in is the parent of s_in — the student every absence test marks.
            tok_a = MobileDeviceToken(
                user_id=self.ids['p_in'], school_id=self.ids['inst'],
                fcm_token=_fake_token(f'a{self.suffix}'), platform='android',
                device_name='Phone A', is_active=True)
            tok_b = MobileDeviceToken(
                user_id=self.ids['p_in'], school_id=self.ids['inst'],
                fcm_token=_fake_token(f'b{self.suffix}'), platform='ios',
                device_name='Phone B', is_active=True)
            db.session.add_all([tok_a, tok_b])
            db.session.commit()
            self.ids['tok_a'] = tok_a.id
            self.ids['tok_b'] = tok_b.id

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for sid in (self.ids['inst'], self.ids['oinst'], self.ids['sch']):
                (NotificationOutbox.query.execution_options(**OPTS)
                 .filter_by(school_id=sid).delete(synchronize_session=False))
                (MobileDeviceToken.query.execution_options(**OPTS)
                 .filter_by(school_id=sid).delete(synchronize_session=False))
            db.session.commit()
        super().tearDown()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _enable(self):
        self.app.config['INSTITUTE_ATTENDANCE_OUTBOX_ENABLED'] = True
        self.addCleanup(
            self.app.config.__setitem__,
            'INSTITUTE_ATTENDANCE_OUTBOX_ENABLED', False)

    def _session(self):
        """A materialised session for group A on the next Sunday 16:00."""
        from datetime import date, time
        from app.models import School, InstituteStudyGroup
        school = db.session.get(School, self.ids['inst'], execution_options=OPTS)
        group = db.session.get(InstituteStudyGroup, self.ids['ga'],
                               execution_options=OPTS)
        sunday = self._next_dow(0, date(2025, 9, 1))
        occ = att.find_occurrence(school, group, sunday, time(16, 0))
        return school, att.get_or_create_session(school, group, occ)

    def _submit(self, statuses, **kw):
        school, session = self._session()
        return school, session, att.submit_attendance(
            school, session, statuses, source='manual_admin',
            actor_user_id=self.ids['uadmin'], **kw)

    def _jobs(self):
        return (NotificationOutbox.query.execution_options(**OPTS)
                .filter_by(school_id=self.ids['inst']).all())

    # ═════════════════════════════════════════════════════════════════════════
    #  1-2. Atomicity
    # ═════════════════════════════════════════════════════════════════════════

    def test_attendance_notification_and_jobs_commit_atomically(self):
        self._enable()
        with self.app.app_context():
            school, session, result = self._submit({self.ids['s_in']: 'absent'})
            sid = session.id

        with self.app.app_context():
            recs = (InstituteAttendanceRecord.query.execution_options(**OPTS)
                    .filter_by(session_id=sid).all())
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0].status, 'absent')

            notifs = (Notification.query.execution_options(**OPTS)
                      .filter_by(school_id=self.ids['inst'],
                                 target_user_id=self.ids['p_in']).all())
            self.assertEqual(len(notifs), 1, 'one in-app row per parent')

            jobs = self._jobs()
            self.assertEqual(len(jobs), 2, 'one job per ACTIVE device token')
            self.assertEqual({j.device_token_id for j in jobs},
                             {self.ids['tok_a'], self.ids['tok_b']})
            for job in jobs:
                self.assertEqual(job.status, NotificationOutbox.STATUS_PENDING)
                self.assertEqual(job.attempts, 0)
                self.assertEqual(job.school_id, self.ids['inst'])
                self.assertEqual(job.event_type,
                                 NotificationOutbox.EVENT_INSTITUTE_ABSENCE)

    def test_outbox_failure_rolls_back_the_attendance_change(self):
        """The whole point: attendance must never outlive its notifications."""
        self._enable()
        with self.app.app_context():
            school, session = self._session()
            sid = session.id

            with patch.object(outbox, 'stage_absence_deliveries',
                              side_effect=RuntimeError('outbox exploded')):
                with self.assertRaises(att.AttendanceError):
                    att.submit_attendance(
                        school, session, {self.ids['s_in']: 'absent'},
                        source='manual_admin',
                        actor_user_id=self.ids['uadmin'])

        with self.app.app_context():
            recs = (InstituteAttendanceRecord.query.execution_options(**OPTS)
                    .filter_by(session_id=sid).all())
            self.assertEqual(recs, [], 'no attendance row may survive')
            self.assertEqual(self._jobs(), [], 'no job may survive')
            fresh = db.session.get(InstituteAttendanceSession, sid,
                                   execution_options=OPTS)
            self.assertEqual(fresh.status,
                             InstituteAttendanceSession.STATUS_NOT_RECORDED,
                             'the session must not be marked recorded')

    # ═════════════════════════════════════════════════════════════════════════
    #  3-4. Deduplicated enqueueing
    # ═════════════════════════════════════════════════════════════════════════

    def test_identical_retry_creates_no_duplicate(self):
        self._enable()
        with self.app.app_context():
            self._submit({self.ids['s_in']: 'absent'})
        with self.app.app_context():
            first = len(self._jobs())
            first_notifs = Notification.query.execution_options(**OPTS)\
                .filter_by(target_user_id=self.ids['p_in']).count()

        with self.app.app_context():
            school, session, result = self._submit({self.ids['s_in']: 'absent'})
            self.assertEqual(result['unchanged'], 1)
            self.assertEqual(result['notified'], 0,
                             'an identical resubmission notifies nobody')

        with self.app.app_context():
            self.assertEqual(len(self._jobs()), first)
            self.assertEqual(
                Notification.query.execution_options(**OPTS)
                .filter_by(target_user_id=self.ids['p_in']).count(),
                first_notifs)

    def test_concurrent_identical_submissions_do_not_duplicate(self):
        """Two threads, same second, same transition.

        One wins. The loser is stopped either by the attendance unique
        constraint or by the outbox dedup key — both are correct, and neither
        may leave a duplicate job behind.
        """
        self._enable()
        barrier = threading.Barrier(2)
        errors = []

        def submit():
            with self.app.app_context():
                try:
                    school, session = self._session()
                    barrier.wait(timeout=10)
                    att.submit_attendance(
                        school, session, {self.ids['s_in']: 'absent'},
                        source='manual_admin',
                        actor_user_id=self.ids['uadmin'])
                except Exception as exc:          # expected for the loser
                    errors.append(type(exc).__name__)
                finally:
                    db.session.remove()

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        with self.app.app_context():
            jobs = self._jobs()
            keys = [j.dedup_key for j in jobs]
            self.assertEqual(len(keys), len(set(keys)),
                             'dedup keys must be unique')
            self.assertLessEqual(len(jobs), 2,
                                 'at most one job per device token')
            recs = (InstituteAttendanceRecord.query.execution_options(**OPTS)
                    .filter_by(student_id=self.ids['s_in']).all())
            self.assertEqual(len(recs), 1, 'exactly one attendance record')

    def test_a_genuine_later_absence_is_not_suppressed(self):
        """absent → present → absent must notify twice.

        A static dedup key would silently swallow the second absence. This is
        the existing documented behaviour and the outbox must preserve it.
        """
        self._enable()
        with self.app.app_context():
            self._submit({self.ids['s_in']: 'absent'})
        with self.app.app_context():
            first = len(self._jobs())
            self.assertEqual(first, 2)

        with self.app.app_context():
            self._submit({self.ids['s_in']: 'present'})
        with self.app.app_context():
            self.assertEqual(len(self._jobs()), first,
                             'present must not enqueue')

        # A later, genuinely new absence. The dedup key carries the transition
        # instant truncated to the second, so the wait guarantees a distinct
        # key — which is exactly the property under test.
        import time as _time
        _time.sleep(1.1)
        with self.app.app_context():
            school, session = self._session()
            att.submit_attendance(
                school, session, {self.ids['s_in']: 'absent'},
                source='manual_admin', actor_user_id=self.ids['uadmin'])
        with self.app.app_context():
            self.assertGreater(
                len(self._jobs()), first,
                'a real re-transition to absent must enqueue again')

    # ═════════════════════════════════════════════════════════════════════════
    #  5-6. Only newly-absent enqueues
    # ═════════════════════════════════════════════════════════════════════════

    def test_only_newly_absent_enqueues(self):
        self._enable()
        with self.app.app_context():
            self._submit({self.ids['s_in']: 'absent',
                          self.ids['s_two']: 'present'})
        with self.app.app_context():
            jobs = self._jobs()
            self.assertEqual(len(jobs), 2, 'only s_in has tokens and is absent')
            payloads = {j.body for j in jobs}
            for body in payloads:
                self.assertIn('غائبا', body.replace('ً', ''))

    def test_present_late_excused_never_enqueue(self):
        self._enable()
        for status in ('present', 'late', 'excused'):
            with self.subTest(status=status):
                with self.app.app_context():
                    (NotificationOutbox.query.execution_options(**OPTS)
                     .filter_by(school_id=self.ids['inst'])
                     .delete(synchronize_session=False))
                    db.session.commit()
                with self.app.app_context():
                    self._submit({self.ids['s_in']: status})
                with self.app.app_context():
                    self.assertEqual(self._jobs(), [],
                                     f'{status} must never enqueue a push')

    def test_opening_a_session_enqueues_nothing(self):
        self._enable()
        with self.app.app_context():
            self._session()          # materialises only
        with self.app.app_context():
            self.assertEqual(self._jobs(), [])

    # ═════════════════════════════════════════════════════════════════════════
    #  7-9. Feature flag and no inline Firebase
    # ═════════════════════════════════════════════════════════════════════════

    def test_flag_false_preserves_existing_behaviour(self):
        self.app.config['INSTITUTE_ATTENDANCE_OUTBOX_ENABLED'] = False
        with self.app.app_context():
            with patch.object(att, '_notify_absent') as legacy:
                self._submit({self.ids['s_in']: 'absent'})
                legacy.assert_called_once()
        with self.app.app_context():
            self.assertEqual(self._jobs(), [],
                             'the outbox table must not be written at all')

    def test_flag_true_performs_no_inline_firebase_call(self):
        self._enable()
        with self.app.app_context():
            with patch('app.services.fcm_service.send_push_to_user') as spu, \
                 patch('app.services.fcm_service._send_one') as one, \
                 patch.object(att, '_notify_absent') as legacy:
                self._submit({self.ids['s_in']: 'absent'})
                spu.assert_not_called()
                one.assert_not_called()
                legacy.assert_not_called()

    def test_request_does_not_wait_for_a_blocked_sender(self):
        """A deliberately hung Firebase must not be on the request path."""
        self._enable()
        blocked = threading.Event()

        def hang(*a, **kw):
            blocked.set()
            time_to_wait = threading.Event()
            time_to_wait.wait(30)       # would stall the request if reached
            return _Sent()

        with self.app.app_context():
            with patch('app.services.fcm_service.send_to_device_token',
                       side_effect=hang):
                started = datetime.utcnow()
                self._submit({self.ids['s_in']: 'absent'})
                elapsed = (datetime.utcnow() - started).total_seconds()

        self.assertFalse(blocked.is_set(),
                         'the request must never reach the sender')
        self.assertLess(elapsed, 10,
                        'the request must return without Firebase')

    # ═════════════════════════════════════════════════════════════════════════
    #  10-14. Worker delivery
    # ═════════════════════════════════════════════════════════════════════════

    def _claim_and_deliver(self, results, worker='w-1', max_attempts=5):
        """Claim everything due and deliver with a scripted fake sender."""
        calls = []

        def fake_send(token_row, title, body, data=None):
            calls.append(token_row.id)
            outcome = results(token_row) if callable(results) else results
            if outcome.permanent and token_row.is_active:
                token_row.is_active = False
                outcome.deactivated = True
            return outcome

        with patch('app.services.fcm_service.send_to_device_token',
                   side_effect=fake_send):
            stats = outbox_worker.run_once(
                worker, batch_size=50, lease_seconds=300,
                max_attempts=max_attempts)
        return stats, calls

    def test_successful_delivery_marks_only_that_row_sent(self):
        self._enable()
        with self.app.app_context():
            self._submit({self.ids['s_in']: 'absent'})

        with self.app.app_context():
            stats, calls = self._claim_and_deliver(lambda t: _Sent(ok=True))
            self.assertEqual(stats['claimed'], 2)
            self.assertEqual(stats['sent'], 2)

        with self.app.app_context():
            for job in self._jobs():
                self.assertEqual(job.status, NotificationOutbox.STATUS_SENT)
                self.assertIsNotNone(job.completed_at)
                self.assertIsNone(job.locked_by)
            logs = (PushNotification.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids['inst']).all())
            self.assertEqual(len(logs), 2,
                             'the delivery log must still be written')
            self.assertTrue(all(l.status == 'sent' for l in logs))

    def test_partial_multi_device_success_does_not_resend(self):
        """Phone A succeeds, phone B times out. A must not be sent twice."""
        self._enable()
        with self.app.app_context():
            self._submit({self.ids['s_in']: 'absent'})

        def scripted(token_row):
            if token_row.id == self.ids['tok_a']:
                return _Sent(ok=True)
            return _Sent(ok=False, error='Deadline exceeded')

        with self.app.app_context():
            stats, first_calls = self._claim_and_deliver(scripted)
            self.assertEqual(stats['sent'], 1)
            self.assertEqual(stats['retry'], 1)

        with self.app.app_context():
            jobs = {j.device_token_id: j for j in self._jobs()}
            self.assertEqual(jobs[self.ids['tok_a']].status, 'sent')
            self.assertEqual(jobs[self.ids['tok_b']].status, 'retry')
            self.assertEqual(jobs[self.ids['tok_b']].attempts, 1)
            self.assertIsNotNone(jobs[self.ids['tok_b']].next_attempt_at)
            # Make B due immediately and sweep again.
            jobs[self.ids['tok_b']].next_attempt_at = datetime.utcnow()
            db.session.commit()

        with self.app.app_context():
            stats2, second_calls = self._claim_and_deliver(lambda t: _Sent(True))
            self.assertEqual(second_calls, [self.ids['tok_b']],
                             'ONLY the failed token may be retried')
            self.assertEqual(stats2['sent'], 1)

    def test_transient_failures_retry_and_keep_the_token_active(self):
        self._enable()
        for error in ('Deadline exceeded', '429 quota exceeded',
                      '503 Service Unavailable'):
            with self.subTest(error=error):
                with self.app.app_context():
                    (NotificationOutbox.query.execution_options(**OPTS)
                     .filter_by(school_id=self.ids['inst'])
                     .delete(synchronize_session=False))
                    for t in (self.ids['tok_a'], self.ids['tok_b']):
                        db.session.get(MobileDeviceToken, t,
                                       execution_options=OPTS).is_active = True
                    db.session.commit()
                    school, session = self._session()
                    outbox.stage_absence_deliveries(
                        school, session, [self.ids['s_in']])
                    db.session.commit()

                with self.app.app_context():
                    self._claim_and_deliver(
                        lambda t: _Sent(ok=False, error=error))

                with self.app.app_context():
                    for job in self._jobs():
                        self.assertEqual(job.status,
                                         NotificationOutbox.STATUS_RETRY)
                        self.assertGreater(job.next_attempt_at,
                                           datetime.utcnow())
                    for t in (self.ids['tok_a'], self.ids['tok_b']):
                        self.assertTrue(
                            db.session.get(MobileDeviceToken, t,
                                           execution_options=OPTS).is_active,
                            'a transient failure must never kill a token')

    def test_permanent_failure_kills_only_that_token_and_is_terminal(self):
        self._enable()
        with self.app.app_context():
            self._submit({self.ids['s_in']: 'absent'})

        def scripted(token_row):
            if token_row.id == self.ids['tok_a']:
                return _Sent(ok=False, permanent=True, error='NotRegistered')
            return _Sent(ok=True)

        with self.app.app_context():
            stats, _ = self._claim_and_deliver(scripted)
            self.assertEqual(stats['dead'], 1)
            self.assertEqual(stats['sent'], 1)

        with self.app.app_context():
            jobs = {j.device_token_id: j for j in self._jobs()}
            self.assertEqual(jobs[self.ids['tok_a']].status,
                             NotificationOutbox.STATUS_DEAD)
            self.assertIsNotNone(jobs[self.ids['tok_a']].completed_at)
            self.assertEqual(jobs[self.ids['tok_b']].status, 'sent')

            dead_tok = db.session.get(MobileDeviceToken, self.ids['tok_a'],
                                      execution_options=OPTS)
            live_tok = db.session.get(MobileDeviceToken, self.ids['tok_b'],
                                      execution_options=OPTS)
            self.assertFalse(dead_tok.is_active, 'the dead token is retired')
            self.assertTrue(live_tok.is_active,
                            'the healthy token of the SAME user survives')

    def test_retry_exhaustion_produces_a_visible_dead_row(self):
        self._enable()
        with self.app.app_context():
            self._submit({self.ids['s_in']: 'absent'})

        for _ in range(3):
            with self.app.app_context():
                for job in self._jobs():
                    if not job.is_terminal:
                        job.status = NotificationOutbox.STATUS_PENDING
                        job.next_attempt_at = datetime.utcnow()
                        job.locked_by = None
                db.session.commit()
                self._claim_and_deliver(
                    lambda t: _Sent(ok=False, error='UNAVAILABLE'),
                    max_attempts=3)

        with self.app.app_context():
            jobs = self._jobs()
            self.assertTrue(jobs)
            for job in jobs:
                self.assertEqual(job.status, NotificationOutbox.STATUS_DEAD,
                                 'exhausted retries must be visibly dead')
                self.assertEqual(job.attempts, 3)
                self.assertIsNotNone(job.last_error)

    # ═════════════════════════════════════════════════════════════════════════
    #  15-16. Worker concurrency and lease recovery
    # ═════════════════════════════════════════════════════════════════════════

    def test_two_workers_cannot_claim_the_same_row(self):
        """FOR UPDATE SKIP LOCKED — PostgreSQL only, never SQLite."""
        self._enable()
        with self.app.app_context():
            self._submit({self.ids['s_in']: 'absent'})

        claimed = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def claim(worker):
            with self.app.app_context():
                try:
                    barrier.wait(timeout=10)
                    rows = outbox.claim_batch(worker, limit=50)
                    with lock:
                        claimed.extend((worker, r.id) for r in rows)
                finally:
                    db.session.remove()

        threads = [threading.Thread(target=claim, args=(f'w-{i}',))
                   for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        ids = [row_id for _w, row_id in claimed]
        self.assertEqual(sorted(ids), sorted(set(ids)),
                         'no row may be claimed by two workers')
        self.assertEqual(len(ids), 2, 'both jobs claimed exactly once')

    def test_stale_lease_is_reclaimed(self):
        self._enable()
        with self.app.app_context():
            self._submit({self.ids['s_in']: 'absent'})

        with self.app.app_context():
            rows = outbox.claim_batch('dead-worker', limit=50)
            self.assertEqual(len(rows), 2)
            # Simulate a worker that was SIGKILLed six minutes ago.
            for row in rows:
                row.locked_at = datetime.utcnow() - timedelta(minutes=6)
            db.session.commit()

        with self.app.app_context():
            # A fresh claim finds nothing: the rows are still 'processing'.
            self.assertEqual(outbox.claim_batch('w-2', limit=50), [])
            reclaimed = outbox.reclaim_stale('w-2', lease_seconds=300)
            self.assertEqual(reclaimed, 2)

        with self.app.app_context():
            for job in self._jobs():
                self.assertEqual(job.status, NotificationOutbox.STATUS_PENDING)
                self.assertIsNone(job.locked_by)
            self.assertEqual(len(outbox.claim_batch('w-2', limit=50)), 2)

    # ═════════════════════════════════════════════════════════════════════════
    #  17-19. Redis, isolation, cleanup
    # ═════════════════════════════════════════════════════════════════════════

    def test_redis_outage_cannot_lose_a_committed_job(self):
        """Redis is not on this path at all — that is the guarantee."""
        self._enable()
        with self.app.app_context():
            with patch('app.services.redis_client.get_redis',
                       return_value=None) as gr:
                self._submit({self.ids['s_in']: 'absent'})
                gr.assert_not_called()

        with self.app.app_context():
            jobs = self._jobs()
            self.assertEqual(len(jobs), 2,
                             'the job is durable in PostgreSQL regardless')
            self.assertTrue(all(j.status == 'pending' for j in jobs))

    def test_cross_tenant_student_is_never_enqueued(self):
        """A forged student id from another institute must be dropped."""
        self._enable()
        with self.app.app_context():
            school, session = self._session()
            staged = outbox.stage_absence_deliveries(
                school, session, [self.ids['o_stu']])
            db.session.commit()
            self.assertEqual(staged, 0, 'another school\'s student is ignored')
            self.assertEqual(self._jobs(), [])

    def test_worker_refuses_a_school_mismatched_job(self):
        self._enable()
        with self.app.app_context():
            self._submit({self.ids['s_in']: 'absent'})
        with self.app.app_context():
            job = self._jobs()[0]
            # Read the id out as a plain int: the ORM instance detaches when
            # this app context closes.
            tampered_token_id = job.device_token_id
            tampered_job_id = job.id
            job.school_id = self.ids['oinst']       # tamper
            db.session.commit()

        with self.app.app_context():
            stats, calls = self._claim_and_deliver(lambda t: _Sent(True))
            self.assertNotIn(tampered_token_id, calls,
                             'a mismatched job must never be delivered')

        with self.app.app_context():
            tampered = db.session.get(NotificationOutbox, tampered_job_id,
                                      execution_options=OPTS)
            self.assertEqual(tampered.status, NotificationOutbox.STATUS_DEAD)
            self.assertEqual(tampered.last_error, 'school-mismatch')

    def test_school_cleanup_still_succeeds_with_pending_jobs(self):
        """Outbox rows must never block deleting a school."""
        from app.utils.school_cleanup import cleanup_school_cascade
        self._enable()
        with self.app.app_context():
            self._submit({self.ids['s_in']: 'absent'})
            self.assertEqual(len(self._jobs()), 2)

        with self.app.app_context():
            cleanup_school_cascade(self.ids['inst'])
            db.session.commit()

        with self.app.app_context():
            remaining = (NotificationOutbox.query.execution_options(**OPTS)
                         .filter_by(school_id=self.ids['inst']).count())
            self.assertEqual(remaining, 0,
                             'CASCADE must remove the jobs with the school')

    # ═════════════════════════════════════════════════════════════════════════
    #  Retention and observability
    # ═════════════════════════════════════════════════════════════════════════

    def test_retention_removes_only_old_sent_rows(self):
        self._enable()
        with self.app.app_context():
            self._submit({self.ids['s_in']: 'absent'})

        with self.app.app_context():
            jobs = self._jobs()
            old = datetime.utcnow() - timedelta(days=60)
            jobs[0].status = NotificationOutbox.STATUS_SENT
            jobs[0].completed_at = old
            jobs[1].status = NotificationOutbox.STATUS_DEAD
            jobs[1].completed_at = old
            db.session.commit()

            removed = outbox.cleanup_terminal(older_than_days=30)
            self.assertEqual(removed, 1, 'only the SENT row may be removed')

        with self.app.app_context():
            left = self._jobs()
            self.assertEqual(len(left), 1)
            self.assertEqual(left[0].status, NotificationOutbox.STATUS_DEAD,
                             'dead rows are evidence and must be kept')

    def test_retention_never_touches_unfinished_work(self):
        self._enable()
        with self.app.app_context():
            self._submit({self.ids['s_in']: 'absent'})
        with self.app.app_context():
            for job in self._jobs():
                job.completed_at = datetime.utcnow() - timedelta(days=90)
            db.session.commit()
            self.assertEqual(outbox.cleanup_terminal(older_than_days=1), 0)
            self.assertEqual(len(self._jobs()), 2,
                             'pending work is never deleted by retention')

    def test_status_summary_reports_backlog_and_age(self):
        self._enable()
        with self.app.app_context():
            self._submit({self.ids['s_in']: 'absent'})
        with self.app.app_context():
            summary = outbox.status_summary()
            self.assertEqual(summary['counts']['pending'], 2)
            self.assertEqual(summary['backlog'], 2)
            self.assertIsNotNone(summary['oldest_due_age_seconds'])
            self.assertGreaterEqual(summary['oldest_due_age_seconds'], 0)

    def test_backoff_is_bounded_and_jittered(self):
        delays = [outbox.backoff_delay(1) for _ in range(20)]
        self.assertTrue(all(0 < d <= 3600 for d in delays))
        self.assertGreater(len(set(round(d, 3) for d in delays)), 1,
                           'jitter must vary the delay')
        self.assertLessEqual(outbox.backoff_delay(50), 3600,
                             'backoff must be capped')

    def test_no_full_token_is_stored_in_the_outbox(self):
        self._enable()
        with self.app.app_context():
            token = db.session.get(MobileDeviceToken, self.ids['tok_a'],
                                   execution_options=OPTS).fcm_token
            self._submit({self.ids['s_in']: 'absent'})
        with self.app.app_context():
            for job in self._jobs():
                blob = ' '.join(str(x) for x in
                                (job.title, job.body, job.data_json,
                                 job.last_error))
                self.assertNotIn(token, blob,
                                 'the outbox stores a token ID, never a token')


# The fixture is inherited, but its TEST methods are not: they already run in
# their own module, and re-running them here would double the suite without
# adding coverage. Two things are needed, because pytest and unittest collect
# differently:
#   1. blank the inherited test methods on the SUBCLASS (non-callable
#      attributes are skipped by both collectors);
#   2. drop the parent's module-level name, because both collectors also pick
#      up any TestCase subclass bound in the module namespace.
# The class object itself survives as InstituteOutboxTest.__bases__[0], so the
# fixture still works.
for _inherited in list(vars(_ParentFixture)):
    if _inherited.startswith('test_'):
        setattr(InstituteOutboxTest, _inherited, None)

del _ParentFixture


if __name__ == '__main__':
    unittest.main()

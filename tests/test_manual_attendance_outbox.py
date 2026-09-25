# -*- coding: utf-8 -*-
"""Durable transactional outbox for MANUAL school student attendance.

Covers exactly one route: POST /attendance/take/<section_id>. Automatic absence
(_run_auto_absent, mark_absent_today, the GET /attendance/ side effect and the
scheduler) is deliberately out of scope and must be untouched.

What is being guaranteed, stated precisely:

  * FLAG OFF IS THE OLD PATH — the inline notification helpers still fire and
    not a single outbox row is written.
  * ATOMICITY — with MANUAL_ATTENDANCE_OUTBOX_ENABLED on, the StudentAttendance
    change, the absence feed rows and the push jobs are one transaction.
  * NO FIREBASE IN THE REQUEST — with the flag on, no push helper of any shape
    is called from the HTTP request.
  * PAYLOAD PARITY — every stored job carries exactly the title, body and data
    the inline path hands to send_push_to_user(), compared for EQUALITY
    against the real legacy code, not against a hand-written copy.
  * DEDUPLICATED ENQUEUEING and a controlled response to a concurrent
    duplicate submission (no uncontrolled HTTP 500).

Runs against the isolated local PostgreSQL test database. No real Firebase, no
production Redis, no production server, no real tokens.
"""
import json
import unittest
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import event, text

from app import create_app
from app.models import (
    db, AcademicYear, Grade, MobileDeviceToken, Notification,
    NotificationOutbox, PushNotification, Role, School, Section, Student,
    StudentAttendance, User, parent_students,
)
from app.services import notification_outbox as outbox
from app.services import outbox_worker

OPTS = {'bypass_tenant_scope': True}
TODAY = date.today()
BATCH = 10


def _fake_token(tag):
    """A fabricated registration token. Never a production value."""
    return f'tkn-manual-{tag}-' + ('x' * 60)


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


class ManualOutboxTest(unittest.TestCase):
    """School A: a one-student 'solo' section and a ten-student 'batch' section.
    School B: one section, one student. School B exists only so every
    isolation assertion has somewhere to leak to.

    Parents: every student has exactly one linked parent. The solo parent has
    TWO registered phones; every other parent has one.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    # ── Fixture ──────────────────────────────────────────────────────────────

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.ids = {'students': {}, 'parents': {}, 'users': []}
        with self.app.app_context():
            roles = {name: Role.query.filter_by(name=name).first()
                     for name in ('parent', 'school_admin', 'teacher')}
            for name in ('parent', 'school_admin'):
                self.assertIsNotNone(roles[name], 'seed roles before running')

            for tag, sections in (('a', ('solo', 'batch')), ('b', ('solo',))):
                school = School(
                    school_name=f'Manual {tag} {self.suffix}',
                    code=f'MO{tag.upper()}{self.suffix[:6]}',
                    capacity=0, is_active=True,
                    # 07:30 late threshold, 13:00 departure. The request clock
                    # is patched, so every status below is deterministic.
                    att_late_threshold=time(7, 30),
                    att_departure_time=time(13, 0),
                    weekly_off_days=None)
                db.session.add(school)
                db.session.flush()
                year = AcademicYear(school_id=school.id,
                                    name=f'Y {tag} {self.suffix}',
                                    start_date=TODAY - timedelta(days=200),
                                    end_date=TODAY + timedelta(days=200),
                                    is_current=True)
                db.session.add(year)
                db.session.flush()
                grade = Grade(school_id=school.id, academic_year_id=year.id,
                              name=f'G{tag}{self.suffix[:4]}')
                db.session.add(grade)
                db.session.flush()
                self.ids.update({f'school_{tag}': school.id,
                                 f'year_{tag}': year.id,
                                 f'grade_{tag}': grade.id})

                for sec in sections:
                    section = Section(school_id=school.id,
                                      academic_year_id=year.id,
                                      grade_id=grade.id,
                                      name=f'{sec}{tag}{self.suffix[:4]}',
                                      capacity=30)
                    db.session.add(section)
                    db.session.flush()
                    self.ids[f'section_{tag}_{sec}'] = section.id
                    count = BATCH if sec == 'batch' else 1
                    for i in range(count):
                        key = f'{tag}_{sec}_{i}'
                        student = Student(
                            student_id=f'MO-{key}-{self.suffix}',
                            full_name=f'Pupil {key} {self.suffix}',
                            date_of_birth=date(2015, 1, 1), gender='male',
                            school_id=school.id, academic_year_id=year.id,
                            section_id=section.id, status='active')
                        parent = User(
                            username=f'mo_p_{key}_{self.suffix}',
                            email=f'mo_p_{key}_{self.suffix}@example.test',
                            full_name=f'Parent {key}',
                            role_id=roles['parent'].id,
                            school_id=school.id, is_active=True)
                        parent.set_password('Password123')
                        db.session.add_all([student, parent])
                        db.session.flush()
                        db.session.execute(parent_students.insert().values(
                            user_id=parent.id, student_id=student.id))
                        self.ids['students'][key] = student.id
                        self.ids['parents'][key] = parent.id
                        self.ids['users'].append(parent.id)
                        phones = 2 if key == 'a_solo_0' else 1
                        for n in range(phones):
                            db.session.add(MobileDeviceToken(
                                user_id=parent.id, school_id=school.id,
                                fcm_token=_fake_token(f'{key}{n}{self.suffix}'),
                                platform='android', device_name=f'Phone {n}',
                                is_active=True))

                admin = User(username=f'mo_ad_{tag}_{self.suffix}',
                             email=f'mo_ad_{tag}_{self.suffix}@example.test',
                             full_name=f'Admin {tag}',
                             role_id=roles['school_admin'].id,
                             school_id=school.id, is_active=True)
                admin.set_password('Password123')
                db.session.add(admin)
                db.session.flush()
                self.ids[f'admin_{tag}'] = admin.id
                self.ids['users'].append(admin.id)

            if roles['teacher'] is not None:
                # A teacher in school A with NO Employee row → no assigned
                # section. Must never be able to record anything.
                teacher = User(username=f'mo_t_{self.suffix}',
                               email=f'mo_t_{self.suffix}@example.test',
                               full_name='Teacher', role_id=roles['teacher'].id,
                               school_id=self.ids['school_a'], is_active=True)
                teacher.set_password('Password123')
                db.session.add(teacher)
                db.session.flush()
                self.ids['teacher'] = teacher.id
                self.ids['users'].append(teacher.id)
            db.session.commit()

        self.app.config['MANUAL_ATTENDANCE_OUTBOX_ENABLED'] = True
        # The other two producers must be unaffected by everything here.
        self.app.config['AIFACE_ATTENDANCE_OUTBOX_ENABLED'] = False
        self.app.config['INSTITUTE_ATTENDANCE_OUTBOX_ENABLED'] = False
        self.client = self.app.test_client()

    def tearDown(self):
        for name in ('MANUAL_ATTENDANCE_OUTBOX_ENABLED',
                     'AIFACE_ATTENDANCE_OUTBOX_ENABLED',
                     'INSTITUTE_ATTENDANCE_OUTBOX_ENABLED'):
            self.app.config.pop(name, None)
        with self.app.app_context():
            sids = [self.ids['school_a'], self.ids['school_b']]
            for table in ('notification_outbox', 'push_notifications',
                          'notifications', 'student_attendance',
                          'mobile_device_tokens', 'audit_logs'):
                db.session.execute(
                    text(f'DELETE FROM {table} WHERE school_id = ANY(:s)'),
                    {'s': sids})
            db.session.execute(
                text('DELETE FROM parent_students WHERE student_id = ANY(:s)'),
                {'s': list(self.ids['students'].values())})
            sections = [v for k, v in self.ids.items()
                        if k.startswith('section_')]
            for model, ids in ((Student, list(self.ids['students'].values())),
                               (User, self.ids['users']),
                               (Section, sections),
                               (Grade, [self.ids['grade_a'], self.ids['grade_b']]),
                               (AcademicYear, [self.ids['year_a'],
                                               self.ids['year_b']]),
                               (School, sids)):
                for pk in ids:
                    row = db.session.get(model, pk, execution_options=OPTS)
                    if row is not None:
                        db.session.delete(row)
                db.session.flush()
            db.session.commit()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _login(self, user_id):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True

    def _sid(self, key):
        return self.ids['students'][key]

    def _post(self, section, statuses=None, *, at=time(7, 0), on=TODAY,
              checkout=(), user='admin_a', extra=None):
        """Drive the real route. `section` is e.g. 'a_solo'; `statuses` maps
        student key → status. The request clock is fixed at `at` on `on`."""
        self._login(self.ids[user])
        form = {'_from': 'manual'}
        for key, status in (statuses or {}).items():
            form[f'status_{self._sid(key)}'] = status
        for key in checkout:
            form[f'checkout_{self._sid(key)}'] = '1'
        form.update(extra or {})
        with patch('app.blueprints.attendance.get_local_now',
                   return_value=datetime.combine(on, at)):
            return self.client.post(
                f'/attendance/take/{self.ids["section_" + section]}'
                f'?date={on.isoformat()}',
                data=form, follow_redirects=False)

    def _batch_statuses(self, status='present'):
        return {f'a_batch_{i}': status for i in range(BATCH)}

    def _jobs(self, tag='a'):
        with self.app.app_context():
            return (NotificationOutbox.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids[f'school_{tag}'])
                    .order_by(NotificationOutbox.id.asc()).all())

    def _attendance(self, key):
        with self.app.app_context():
            return (StudentAttendance.query.execution_options(**OPTS)
                    .filter_by(student_id=self._sid(key)).all())

    def _feed_rows(self, tag='a'):
        with self.app.app_context():
            return (Notification.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids[f'school_{tag}'])
                    .order_by(Notification.id.asc()).all())

    def _push_log(self, tag='a'):
        with self.app.app_context():
            return (PushNotification.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids[f'school_{tag}']).all())

    def _flashes(self):
        with self.client.session_transaction() as sess:
            return list(sess.get('_flashes', []))

    def _wipe_student_day(self, key):
        """Remove every trace of one student's day so it can be replayed."""
        with self.app.app_context():
            sid = self._sid(key)
            db.session.execute(text(
                'DELETE FROM student_attendance WHERE student_id = :s'),
                {'s': sid})
            for table in ('notifications', 'push_notifications',
                          'notification_outbox'):
                db.session.execute(
                    text(f'DELETE FROM {table} WHERE school_id = :sc'),
                    {'sc': self.ids['school_a']})
            db.session.commit()

    @contextmanager
    def _no_network_push(self):
        """Every push entry point, each failing loudly if the request uses it."""
        boom = AssertionError('Firebase must not be called from the request')
        with patch('app.services.fcm_service.is_enabled', return_value=True), \
             patch('app.services.fcm_service._send_one',
                   side_effect=boom) as send_one, \
             patch('app.services.fcm_service.send_push_to_user',
                   side_effect=boom) as multi, \
             patch('app.services.fcm_service.send_to_device_token',
                   side_effect=boom) as direct, \
             patch('app.services.notifications._NotificationService'
                   '.send_to_parents_of_student', side_effect=boom) as svc, \
             patch('app.blueprints.attendance._notify_absent_parents',
                   side_effect=boom) as absent:
            mocks = {'_send_one': send_one, 'send_push_to_user': multi,
                     'send_to_device_token': direct,
                     'send_to_parents_of_student': svc,
                     '_notify_absent_parents': absent}
            yield mocks
        for name, mock in mocks.items():
            self.assertEqual(mock.call_count, 0, f'{name} was called')

    @contextmanager
    def _capture_legacy_pushes(self):
        """Run the REAL inline path, capturing what it hands to Firebase."""
        captured = []

        def _grab(user_id, title, body, data=None, *_a, **_k):
            captured.append((user_id, title, body, dict(data or {})))
            return 1, 0

        with patch('app.services.fcm_service.is_enabled', return_value=True), \
             patch('app.services.fcm_service.send_push_to_user',
                   side_effect=_grab):
            yield captured

    @staticmethod
    def _job_messages(jobs):
        """(user_id, title, body, data) per job, as the worker will send it."""
        return [(j.user_id, j.title, j.body, json.loads(j.data_json or '{}'))
                for j in jobs]

    @staticmethod
    def _sorted(items):
        return sorted(items, key=lambda m: (m[0], m[1],
                                            json.dumps(m[3], sort_keys=True)))


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 1 — Feature flag
# ═════════════════════════════════════════════════════════════════════════════

class FlagBehaviourTest(ManualOutboxTest):

    def test_flag_defaults_to_false(self):
        """A deployment that sets nothing must keep the legacy path."""
        fresh = create_app('testing')
        self.assertFalse(fresh.config['MANUAL_ATTENDANCE_OUTBOX_ENABLED'])
        self.assertFalse(fresh.config['AIFACE_ATTENDANCE_OUTBOX_ENABLED'])
        self.assertFalse(fresh.config['INSTITUTE_ATTENDANCE_OUTBOX_ENABLED'])
        with fresh.app_context():
            self.assertFalse(outbox.manual_enabled())
            self.assertFalse(outbox.any_enabled())

    def test_the_three_flags_are_independent(self):
        names = ('MANUAL_ATTENDANCE_OUTBOX_ENABLED',
                 'AIFACE_ATTENDANCE_OUTBOX_ENABLED',
                 'INSTITUTE_ATTENDANCE_OUTBOX_ENABLED')
        with self.app.app_context():
            for on in names:
                for name in names:
                    self.app.config[name] = (name == on)
                self.assertEqual(
                    (outbox.manual_enabled(), outbox.aiface_enabled(),
                     outbox.enabled()),
                    tuple(n == on for n in names), on)
                self.assertTrue(outbox.any_enabled(), on)


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 2 — Flag OFF keeps the inline path
# ═════════════════════════════════════════════════════════════════════════════

class FlagOffTest(ManualOutboxTest):

    def setUp(self):
        super().setUp()
        self.app.config['MANUAL_ATTENDANCE_OUTBOX_ENABLED'] = False

    def test_flag_off_uses_the_inline_helpers_and_writes_no_job(self):
        with patch('app.services.notifications._NotificationService'
                   '.send_to_parents_of_student') as inline, \
             patch('app.blueprints.attendance._notify_absent_parents') as absent:
            resp = self._post('a_batch', {**self._batch_statuses('present'),
                                          'a_batch_9': 'absent'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(inline.call_count, BATCH - 1)
        self.assertEqual(absent.call_count, 1)
        self.assertEqual(self._jobs('a'), [])

    def test_flag_off_legacy_side_effects_are_unchanged(self):
        """Real legacy path: feed row for the absence, delivery log rows, and
        send_push_to_user called once per parent, from the request."""
        with self._capture_legacy_pushes() as pushes:
            resp = self._post('a_solo', {'a_solo_0': 'absent'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(pushes), 1)
        self.assertEqual(len(self._feed_rows('a')), 1)
        self.assertEqual(self._jobs('a'), [])
        self.assertEqual(self._attendance('a_solo_0')[0].status, 'absent')


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 3 / 12 — Flag ON, single student, no network in the request
# ═════════════════════════════════════════════════════════════════════════════

class SingleStudentTest(ManualOutboxTest):

    def test_present_is_saved_and_staged_without_any_firebase_call(self):
        with self._no_network_push():
            resp = self._post('a_solo', {'a_solo_0': 'present'})
        self.assertEqual(resp.status_code, 302)
        rows = self._attendance('a_solo_0')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, 'present')
        self.assertEqual(rows[0].check_in, time(7, 0))
        jobs = self._jobs('a')
        self.assertEqual(len(jobs), 2)                # solo parent has 2 phones
        self.assertEqual({j.event_type for j in jobs},
                         {NotificationOutbox.EVENT_SCHOOL_ATTENDANCE_MANUAL})
        self.assertTrue(all(j.status == NotificationOutbox.STATUS_PENDING
                            for j in jobs))
        self.assertEqual({j.user_id for j in jobs},
                         {self.ids['parents']['a_solo_0']})
        # The delivery log belongs to the worker now, not to the request.
        self.assertEqual(self._push_log('a'), [])
        # A check-in has never produced a parent feed row.
        self.assertEqual(self._feed_rows('a'), [])

    def test_request_succeeds_even_though_every_push_path_would_raise(self):
        """TEST 12: Firebase is booby-trapped; the save must still succeed."""
        with self._no_network_push():
            resp = self._post('a_batch', {**self._batch_statuses('present'),
                                          'a_batch_0': 'late',
                                          'a_batch_1': 'absent'})
        self.assertEqual(resp.status_code, 302)
        self.assertIn(('success',
                       f'تم حفظ الحضور ليوم {TODAY.strftime("%Y-%m-%d")}.'),
                      self._flashes())
        self.assertEqual(len(self._jobs('a')), BATCH)


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 4 / 13 — Batch, commits and cost
# ═════════════════════════════════════════════════════════════════════════════

class BatchTest(ManualOutboxTest):

    STATUSES = {**{f'a_batch_{i}': 'present' for i in range(5)},
                **{f'a_batch_{i}': 'late' for i in range(5, 8)},
                **{f'a_batch_{i}': 'absent' for i in range(8, 10)}}

    def test_batch_rows_and_jobs_are_exact(self):
        with self._no_network_push():
            resp = self._post('a_batch', self.STATUSES)
        self.assertEqual(resp.status_code, 302)
        for key, status in self.STATUSES.items():
            rows = self._attendance(key)
            self.assertEqual(len(rows), 1, key)
            self.assertEqual(rows[0].status, status, key)
        jobs = self._jobs('a')
        # One parent per student, one active phone per batch parent.
        self.assertEqual(len(jobs), BATCH)
        self.assertEqual(len({j.dedup_key for j in jobs}), BATCH)
        self.assertEqual(len({j.device_token_id for j in jobs}), BATCH)
        self.assertEqual({j.user_id for j in jobs},
                         {self.ids['parents'][k] for k in self.STATUSES})
        actions = sorted(j.dedup_key.split(':')[3] for j in jobs)
        self.assertEqual(actions, ['absent'] * 2 + ['check_in'] * 8)
        # Feed rows exactly for the two absences, one per linked parent.
        feed = self._feed_rows('a')
        self.assertEqual(sorted(r.target_user_id for r in feed),
                         sorted(self.ids['parents'][f'a_batch_{i}']
                                for i in (8, 9)))

    def _measure(self, flag_on):
        """Commits, SQL statements and Firebase calls for ONE batch request."""
        self.app.config['MANUAL_ATTENDANCE_OUTBOX_ENABLED'] = flag_on
        counts = {'commits': 0, 'statements': 0}

        def _commit(_conn):
            counts['commits'] += 1

        def _stmt(*_a, **_k):
            counts['statements'] += 1

        with self.app.app_context():
            engine = db.engine
        event.listen(engine, 'commit', _commit)
        event.listen(engine, 'before_cursor_execute', _stmt)
        try:
            with self._capture_legacy_pushes() as pushes:
                resp = self._post('a_batch', self.STATUSES)
        finally:
            event.remove(engine, 'commit', _commit)
            event.remove(engine, 'before_cursor_execute', _stmt)
        self.assertEqual(resp.status_code, 302)
        counts['firebase_calls'] = len(pushes)
        counts['outbox_rows'] = len(self._jobs('a'))
        return counts

    def test_cost_of_one_ten_student_batch(self):
        legacy = self._measure(flag_on=False)
        for i in range(BATCH):
            self._wipe_student_day(f'a_batch_{i}')
        durable = self._measure(flag_on=True)
        print(f'\n[manual-outbox cost] 10-student batch  '
              f'flag_off={legacy}  flag_on={durable}')

        self.assertEqual(durable['firebase_calls'], 0)
        self.assertEqual(durable['outbox_rows'], BATCH)
        self.assertEqual(legacy['firebase_calls'], BATCH)
        self.assertEqual(legacy['outbox_rows'], 0)
        # The inline path commits once more per notified student; the durable
        # path commits attendance + jobs together in the one main commit.
        self.assertEqual(legacy['commits'] - durable['commits'], BATCH)


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 5 / 6 — Content parity with the REAL inline path
# ═════════════════════════════════════════════════════════════════════════════

class PayloadParityTest(ManualOutboxTest):
    """Each transition is run twice for the SAME student on the SAME day at the
    SAME clock time: once through the real legacy code (flag off, pushes
    captured), once through the outbox. Everything must be equal."""

    def _parity(self, statuses, *, at=time(7, 0), checkout=(),
                before=None, action=None):
        def _run(flag_on):
            self.app.config['MANUAL_ATTENDANCE_OUTBOX_ENABLED'] = False
            if before is not None:
                with self._capture_legacy_pushes():
                    self.assertEqual(before().status_code, 302)
            self.app.config['MANUAL_ATTENDANCE_OUTBOX_ENABLED'] = flag_on
            with self._capture_legacy_pushes() as pushes:
                resp = self._post('a_solo', statuses, at=at, checkout=checkout)
            self.assertEqual(resp.status_code, 302)
            feed = [(r.school_id, r.title, r.body, r.ntype, r.target_user_id,
                     r.created_by) for r in self._feed_rows('a')]
            return pushes, feed

        legacy_pushes, legacy_feed = _run(False)
        self.assertTrue(legacy_pushes, 'the legacy path sent nothing')
        self._wipe_student_day('a_solo_0')
        outbox_pushes, outbox_feed = _run(True)
        self.assertEqual(outbox_pushes, [])

        jobs = [j for j in self._jobs('a')
                if action is None or j.dedup_key.split(':')[3] == action]
        per_parent = {m[0]: m for m in self._job_messages(jobs)}
        self.assertEqual(self._sorted(per_parent.values()),
                         self._sorted(legacy_pushes))
        # Every phone of the parent carries the identical message.
        self.assertEqual(len({(m[1], m[2], json.dumps(m[3], sort_keys=True))
                              for m in self._job_messages(jobs)}), 1)
        self.assertEqual(outbox_feed, legacy_feed)
        return jobs, legacy_pushes, outbox_feed

    def test_present_is_identical(self):
        jobs, legacy, feed = self._parity({'a_solo_0': 'present'})
        data = legacy[0][3]
        self.assertEqual(legacy[0][1], 'حضور الطالب في الوقت المحدد')
        self.assertEqual(data['type'], 'notification')
        self.assertEqual(data['route'], '/parent/notifications')
        self.assertEqual(data['ntype'], 'attendance')
        self.assertEqual(data['status'], 'present')
        self.assertEqual(feed, [])

    def test_late_selected_explicitly_is_identical(self):
        _, legacy, feed = self._parity({'a_solo_0': 'late'})
        self.assertEqual(legacy[0][1], 'تأخر الطالب عن موعد الحضور')
        self.assertEqual(legacy[0][3]['status'], 'late')
        self.assertEqual(feed, [])

    def test_present_after_the_late_threshold_is_identical(self):
        _, legacy, _ = self._parity({'a_solo_0': 'present'}, at=time(8, 0))
        self.assertEqual(legacy[0][3]['status'], 'late')

    def test_absent_is_identical_including_the_feed_row(self):
        _, legacy, feed = self._parity({'a_solo_0': 'absent'})
        self.assertEqual(legacy[0][1], 'تنبيه غياب')
        self.assertEqual(legacy[0][3]['type'], 'attendance')
        self.assertNotIn('route', legacy[0][3])
        self.assertEqual(len(feed), 1)
        self.assertEqual(feed[0][4], self.ids['parents']['a_solo_0'])

    def test_check_out_is_identical(self):
        def _check_in():
            return self._post('a_solo', {'a_solo_0': 'present'})

        jobs, legacy, feed = self._parity(
            {'a_solo_0': 'present'}, at=time(13, 30), checkout=('a_solo_0',),
            before=_check_in, action='check_out')
        self.assertEqual(legacy[0][1], 'انصراف الطالب من المدرسة')
        self.assertEqual(legacy[0][3]['action'], 'check_out')
        self.assertNotIn('status', legacy[0][3])
        self.assertEqual(feed, [])
        self.assertEqual(self._attendance('a_solo_0')[0].check_out,
                         time(13, 30))

    def test_on_leave_notifies_nobody_on_either_path(self):
        for flag in (False, True):
            self.app.config['MANUAL_ATTENDANCE_OUTBOX_ENABLED'] = flag
            with self._capture_legacy_pushes() as pushes:
                self._post('a_solo', {'a_solo_0': 'on_leave'})
            self.assertEqual(pushes, [])
            self.assertEqual(self._jobs('a'), [])
            self.assertEqual(self._feed_rows('a'), [])
            self._wipe_student_day('a_solo_0')

    def test_on_leave_overridden_to_absent_is_staged(self):
        """The existing on_leave → absent override path stays notified."""
        self.app.config['MANUAL_ATTENDANCE_OUTBOX_ENABLED'] = False
        self._post('a_solo', {'a_solo_0': 'on_leave'})
        self.app.config['MANUAL_ATTENDANCE_OUTBOX_ENABLED'] = True
        with self._no_network_push():
            self._post('a_solo', {'a_solo_0': 'absent'})
        self.assertEqual(self._attendance('a_solo_0')[0].status, 'absent')
        self.assertEqual(len(self._jobs('a')), 2)
        self.assertEqual(len(self._feed_rows('a')), 1)


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 7 — Atomicity: staging failure rolls the attendance back
# ═════════════════════════════════════════════════════════════════════════════

class AtomicityTest(ManualOutboxTest):

    def test_staging_failure_rolls_the_attendance_back(self):
        with patch.object(outbox, 'stage_manual_attendance_deliveries',
                          side_effect=RuntimeError('staging exploded')), \
             self._no_network_push():
            resp = self._post('a_batch', {**self._batch_statuses('present'),
                                          'a_batch_9': 'absent'})
        self.assertEqual(resp.status_code, 302)
        for i in range(BATCH):
            self.assertEqual(self._attendance(f'a_batch_{i}'), [])
        self.assertEqual(self._jobs('a'), [])
        self.assertEqual(self._feed_rows('a'), [])
        self.assertTrue(any(cat == 'danger' for cat, _ in self._flashes()))

    def test_failure_on_the_last_student_leaves_nothing_behind(self):
        """A partial stage (earlier students already added) must not commit."""
        real = outbox.stage_manual_attendance_deliveries
        calls = {'n': 0}

        def _fail_last(*a, **k):
            calls['n'] += 1
            if calls['n'] == BATCH:
                raise RuntimeError('last one fails')
            return real(*a, **k)

        with patch.object(outbox, 'stage_manual_attendance_deliveries',
                          side_effect=_fail_last):
            self._post('a_batch', self._batch_statuses('absent'))
        self.assertEqual(calls['n'], BATCH)
        for i in range(BATCH):
            self.assertEqual(self._attendance(f'a_batch_{i}'), [])
        self.assertEqual(self._jobs('a'), [])
        self.assertEqual(self._feed_rows('a'), [])

    def test_check_out_staging_failure_leaves_the_check_in_intact(self):
        self._post('a_solo', {'a_solo_0': 'present'})
        self.assertEqual(len(self._jobs('a')), 2)
        with patch.object(outbox, 'stage_manual_attendance_deliveries',
                          side_effect=RuntimeError('boom')):
            self._post('a_solo', {}, at=time(13, 30), checkout=('a_solo_0',))
        rows = self._attendance('a_solo_0')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].check_in, time(7, 0))
        self.assertIsNone(rows[0].check_out)          # rolled back
        self.assertEqual(len(self._jobs('a')), 2)     # nothing extra staged


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 8 / 9 — Resubmission, dedup, concurrent duplicate
# ═════════════════════════════════════════════════════════════════════════════

class DuplicateTest(ManualOutboxTest):

    def test_resubmitting_the_same_attendance_stages_nothing_new(self):
        statuses = {**self._batch_statuses('present'), 'a_batch_9': 'absent'}
        self._post('a_batch', statuses)
        first = [j.dedup_key for j in self._jobs('a')]
        with self._no_network_push():
            resp = self._post('a_batch', statuses)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual([j.dedup_key for j in self._jobs('a')], first)
        self.assertEqual(len(self._feed_rows('a')), 1)
        for i in range(BATCH):
            self.assertEqual(len(self._attendance(f'a_batch_{i}')), 1)

    def test_repeated_check_out_stages_once(self):
        self._post('a_solo', {'a_solo_0': 'present'})
        self._post('a_solo', {}, at=time(13, 30), checkout=('a_solo_0',))
        self._post('a_solo', {}, at=time(13, 45), checkout=('a_solo_0',))
        actions = sorted(j.dedup_key.split(':')[3] for j in self._jobs('a'))
        self.assertEqual(actions, ['check_in'] * 2 + ['check_out'] * 2)
        self.assertEqual(self._attendance('a_solo_0')[0].check_out,
                         time(13, 30))

    def test_the_dedup_key_rejects_a_duplicate_transition(self):
        """Two identical enqueues collide on the UNIQUE index, not in memory."""
        from sqlalchemy.exc import IntegrityError
        self._post('a_solo', {'a_solo_0': 'present'})
        with self.app.app_context():
            school = db.session.get(School, self.ids['school_a'],
                                    execution_options=OPTS)
            student = db.session.get(Student, self._sid('a_solo_0'),
                                     execution_options=OPTS)
            row = self._attendance('a_solo_0')[0]
            outbox.stage_manual_attendance_deliveries(
                school, student, action='check_in', on_date=TODAY,
                attendance_id=row.id, title='t', body='b', data={})
            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()
        self.assertEqual(len(self._jobs('a')), 2)

    def test_a_re_recorded_row_gets_a_new_key(self):
        """A deleted and re-recorded day must stay saveable (row id in key)."""
        self._post('a_solo', {'a_solo_0': 'absent'})
        with self.app.app_context():
            db.session.execute(text(
                'DELETE FROM student_attendance WHERE student_id = :s'),
                {'s': self._sid('a_solo_0')})
            db.session.commit()
        resp = self._post('a_solo', {'a_solo_0': 'absent'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(self._attendance('a_solo_0')), 1)
        self.assertEqual(len(self._jobs('a')), 4)

    def _concurrent_insert(self):
        """Simulate the winning concurrent request: while this request is
        building its rows, another transaction commits the same student-day."""
        app = self.app
        student_id = self._sid('a_solo_0')
        school_id = self.ids['school_a']
        year_id = self.ids['year_a']

        def _winner(student, school):
            with app.app_context():
                with db.engine.begin() as conn:
                    conn.execute(text(
                        'INSERT INTO student_attendance (student_id, school_id, '
                        'academic_year_id, date, status, source, created_at) '
                        "VALUES (:st, :sc, :y, :d, 'absent', 'manual', now())"),
                        {'st': student_id, 'sc': school_id, 'y': year_id,
                         'd': TODAY})
            return None

        return patch('app.blueprints.attendance.get_student_shift',
                     side_effect=_winner)

    def _assert_controlled_conflict(self, resp):
        self.assertEqual(resp.status_code, 302)       # not an uncontrolled 500
        self.assertTrue(any(cat == 'warning' for cat, _ in self._flashes()))
        rows = self._attendance('a_solo_0')
        self.assertEqual(len(rows), 1)                # the winner's row only
        self.assertEqual(rows[0].status, 'absent')
        self.assertIsNone(rows[0].recorded_by)
        self.assertEqual(self._jobs('a'), [])
        self.assertEqual(self._feed_rows('a'), [])

    def test_concurrent_duplicate_is_handled_with_the_flag_on(self):
        with self._concurrent_insert(), self._no_network_push():
            resp = self._post('a_solo', {'a_solo_0': 'present'})
        self._assert_controlled_conflict(resp)

    def test_concurrent_duplicate_is_handled_with_the_flag_off(self):
        self.app.config['MANUAL_ATTENDANCE_OUTBOX_ENABLED'] = False
        with self._concurrent_insert(), self._capture_legacy_pushes() as pushes:
            resp = self._post('a_solo', {'a_solo_0': 'present'})
        self._assert_controlled_conflict(resp)
        self.assertEqual(pushes, [])                  # the loser notifies nobody


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 10 — Tenant isolation and authorization (unchanged)
# ═════════════════════════════════════════════════════════════════════════════

class IsolationTest(ManualOutboxTest):

    def test_school_a_admin_cannot_record_school_b_attendance(self):
        with self._no_network_push():
            resp = self._post('b_solo', {'b_solo_0': 'present'})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self._attendance('b_solo_0'), [])
        self.assertEqual(self._jobs('a'), [])
        self.assertEqual(self._jobs('b'), [])

    def test_unauthenticated_request_writes_nothing(self):
        with patch('app.blueprints.attendance.get_local_now',
                   return_value=datetime.combine(TODAY, time(7, 0))):
            resp = self.client.post(
                f'/attendance/take/{self.ids["section_a_solo"]}',
                data={f'status_{self._sid("a_solo_0")}': 'present'})
        self.assertIn(resp.status_code, (302, 401))
        self.assertEqual(self._attendance('a_solo_0'), [])
        self.assertEqual(self._jobs('a'), [])

    def test_unassigned_teacher_writes_nothing(self):
        if 'teacher' not in self.ids:
            self.skipTest('teacher role not seeded')
        resp = self._post('a_solo', {'a_solo_0': 'present'}, user='teacher')
        self.assertIn(resp.status_code, (302, 403))
        self.assertEqual(self._attendance('a_solo_0'), [])
        self.assertEqual(self._jobs('a'), [])

    def test_every_job_matches_its_own_school_on_all_three_axes(self):
        self._post('a_solo', {'a_solo_0': 'present'})
        self._post('b_solo', {'b_solo_0': 'absent'}, user='admin_b')
        with self.app.app_context():
            for tag in ('a', 'b'):
                sid = self.ids[f'school_{tag}']
                jobs = self._jobs(tag)
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
            for row in self._feed_rows('b'):
                self.assertEqual(row.target_user_id,
                                 self.ids['parents']['b_solo_0'])

    def test_staging_refuses_a_cross_school_student(self):
        with self.app.app_context():
            school_a = db.session.get(School, self.ids['school_a'],
                                      execution_options=OPTS)
            student_b = db.session.get(Student, self._sid('b_solo_0'),
                                       execution_options=OPTS)
            with self.assertRaises(ValueError) as caught:
                outbox.stage_manual_attendance_deliveries(
                    school_a, student_b, action='absent', on_date=TODAY,
                    attendance_id=1, title='t', body='b', data={})
            self.assertIn('cross-school', str(caught.exception))
            db.session.rollback()
        self.assertEqual(self._jobs('a'), [])
        self.assertEqual(self._jobs('b'), [])
        self.assertEqual(self._feed_rows('a'), [])

    def test_staging_rejects_an_unknown_action(self):
        with self.app.app_context():
            school = db.session.get(School, self.ids['school_a'],
                                    execution_options=OPTS)
            student = db.session.get(Student, self._sid('a_solo_0'),
                                     execution_options=OPTS)
            with self.assertRaises(ValueError):
                outbox.stage_manual_attendance_deliveries(
                    school, student, action='on_leave', on_date=TODAY,
                    attendance_id=1, title='t', body='b', data={})
            db.session.rollback()

    def test_a_parents_token_in_another_school_is_never_targeted(self):
        with self.app.app_context():
            tok = (MobileDeviceToken.query.execution_options(**OPTS)
                   .filter_by(user_id=self.ids['parents']['a_solo_0'])
                   .order_by(MobileDeviceToken.id.asc()).first())
            tok.school_id = self.ids['school_b']
            db.session.commit()
            moved = tok.id
        self._post('a_solo', {'a_solo_0': 'present'})
        jobs = self._jobs('a')
        self.assertEqual(len(jobs), 1)
        self.assertNotIn(moved, {j.device_token_id for j in jobs})
        self.assertEqual(self._jobs('b'), [])

    def test_an_inactive_token_is_never_targeted(self):
        with self.app.app_context():
            tok = (MobileDeviceToken.query.execution_options(**OPTS)
                   .filter_by(user_id=self.ids['parents']['a_solo_0'])
                   .order_by(MobileDeviceToken.id.asc()).first())
            tok.is_active = False
            db.session.commit()
            inactive = tok.id
        self._post('a_solo', {'a_solo_0': 'present'})
        jobs = self._jobs('a')
        self.assertEqual(len(jobs), 1)
        self.assertNotIn(inactive, {j.device_token_id for j in jobs})

    def test_a_student_with_no_linked_parent_still_saves(self):
        with self.app.app_context():
            db.session.execute(text(
                'DELETE FROM parent_students WHERE student_id = :s'),
                {'s': self._sid('a_solo_0')})
            db.session.commit()
        resp = self._post('a_solo', {'a_solo_0': 'absent'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(self._attendance('a_solo_0')), 1)
        self.assertEqual(self._jobs('a'), [])
        self.assertEqual(self._feed_rows('a'), [])

    def test_no_token_string_is_ever_stored_on_a_job(self):
        self._post('a_solo', {'a_solo_0': 'absent'})
        with self.app.app_context():
            tokens = [t.fcm_token for t in
                      MobileDeviceToken.query.execution_options(**OPTS).all()]
        for job in self._jobs('a'):
            blob = f'{job.title}{job.body}{job.data_json}{job.dedup_key}'
            for value in tokens:
                self.assertNotIn(value, blob)


# ═════════════════════════════════════════════════════════════════════════════
#  Cross-school parent links (corrupt data) — Gate 1 hardening
# ═════════════════════════════════════════════════════════════════════════════

class CrossSchoolParentTest(ManualOutboxTest):
    """A parent_students row linking school A's pupil to school B's parent is
    corrupt data. It must never deliver across schools, and it must never make
    an otherwise valid manual attendance fail."""

    def _link_foreign_parent(self, *, only=False):
        with self.app.app_context():
            if only:
                db.session.execute(text(
                    'DELETE FROM parent_students WHERE student_id = :s'),
                    {'s': self._sid('a_solo_0')})
            db.session.execute(parent_students.insert().values(
                user_id=self.ids['parents']['b_solo_0'],
                student_id=self._sid('a_solo_0')))
            db.session.commit()

    def _foreign_untouched(self):
        foreign = self.ids['parents']['b_solo_0']
        self.assertNotIn(foreign, {j.user_id for j in self._jobs('a')})
        self.assertNotIn(foreign, {r.target_user_id for r in self._feed_rows('a')})
        self.assertEqual(self._jobs('b'), [])
        self.assertEqual(self._feed_rows('b'), [])

    def test_a_valid_same_school_parent(self):
        with self._no_network_push():
            resp = self._post('a_solo', {'a_solo_0': 'absent'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(self._jobs('a')), 2)
        self.assertEqual([r.target_user_id for r in self._feed_rows('a')],
                         [self.ids['parents']['a_solo_0']])

    def test_b_e_mixed_valid_and_cross_school_parent_absent(self):
        self._link_foreign_parent()
        with self._no_network_push():
            resp = self._post('a_solo', {'a_solo_0': 'absent'})
        self.assertEqual(resp.status_code, 302)                 # not a 500
        self.assertEqual(self._attendance('a_solo_0')[0].status, 'absent')
        self.assertEqual({j.user_id for j in self._jobs('a')},
                         {self.ids['parents']['a_solo_0']})
        self.assertEqual(len(self._jobs('a')), 2)
        self.assertEqual([r.target_user_id for r in self._feed_rows('a')],
                         [self.ids['parents']['a_solo_0']])
        self.assertFalse(any(cat == 'danger' for cat, _ in self._flashes()))
        self._foreign_untouched()

    def test_b_e_mixed_parents_check_in_and_check_out(self):
        self._link_foreign_parent()
        with self._no_network_push():
            self._post('a_solo', {'a_solo_0': 'present'})
            self._post('a_solo', {}, at=time(13, 30), checkout=('a_solo_0',))
        actions = sorted(j.dedup_key.split(':')[3] for j in self._jobs('a'))
        self.assertEqual(actions, ['check_in'] * 2 + ['check_out'] * 2)
        self._foreign_untouched()

    def test_foreign_parent_with_a_stale_token_tagged_with_this_school(self):
        """The token filter alone would let this through: the token row says
        school A, but its owner belongs to school B."""
        self._link_foreign_parent()
        with self.app.app_context():
            db.session.add(MobileDeviceToken(
                user_id=self.ids['parents']['b_solo_0'],
                school_id=self.ids['school_a'],
                fcm_token=_fake_token(f'stale{self.suffix}'),
                platform='android', device_name='Stale', is_active=True))
            db.session.commit()
        with self._no_network_push():
            self._post('a_solo', {'a_solo_0': 'present'})
        self.assertEqual(len(self._jobs('a')), 2)
        self._foreign_untouched()

    def test_c_same_school_parent_with_cross_school_token(self):
        with self.app.app_context():
            tok = (MobileDeviceToken.query.execution_options(**OPTS)
                   .filter_by(user_id=self.ids['parents']['a_solo_0'])
                   .order_by(MobileDeviceToken.id.asc()).first())
            tok.school_id = self.ids['school_b']
            db.session.commit()
            moved = tok.id
        with self._no_network_push():
            self._post('a_solo', {'a_solo_0': 'absent'})
        jobs = self._jobs('a')
        self.assertEqual(len(jobs), 1)
        self.assertNotIn(moved, {j.device_token_id for j in jobs})
        self.assertEqual(len(self._feed_rows('a')), 1)
        self.assertEqual(self._jobs('b'), [])

    def test_d_inactive_token_absent(self):
        with self.app.app_context():
            tok = (MobileDeviceToken.query.execution_options(**OPTS)
                   .filter_by(user_id=self.ids['parents']['a_solo_0'])
                   .order_by(MobileDeviceToken.id.asc()).first())
            tok.is_active = False
            db.session.commit()
            inactive = tok.id
        with self._no_network_push():
            self._post('a_solo', {'a_solo_0': 'absent'})
        self.assertEqual(len(self._jobs('a')), 1)
        self.assertNotIn(inactive, {j.device_token_id for j in self._jobs('a')})
        self.assertEqual(len(self._feed_rows('a')), 1)          # feed row still

    def test_f_only_an_invalid_cross_school_parent(self):
        self._link_foreign_parent(only=True)
        with self._no_network_push():
            resp = self._post('a_solo', {'a_solo_0': 'absent'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._attendance('a_solo_0')[0].status, 'absent')
        self.assertEqual(self._jobs('a'), [])
        self.assertEqual(self._feed_rows('a'), [])
        self.assertTrue(any(cat == 'success' for cat, _ in self._flashes()))
        self._foreign_untouched()

    def test_flag_off_legacy_is_unchanged_for_a_corrupt_link(self):
        """Legacy: the attendance saves; the guard rejects that student's feed
        rows, so nobody linked to that student gets a feed row or a push."""
        self.app.config['MANUAL_ATTENDANCE_OUTBOX_ENABLED'] = False
        self._link_foreign_parent()
        with self._capture_legacy_pushes() as pushes:
            resp = self._post('a_solo', {'a_solo_0': 'absent'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._attendance('a_solo_0')[0].status, 'absent')
        self.assertEqual(self._feed_rows('a'), [])
        self.assertEqual(pushes, [])
        self.assertEqual(self._jobs('a'), [])


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 11 — The existing worker, unchanged
# ═════════════════════════════════════════════════════════════════════════════

class WorkerCompatibilityTest(ManualOutboxTest):

    def _run(self, result, max_attempts=5):
        with self.app.app_context(), \
             patch('app.services.fcm_service.send_to_device_token',
                   return_value=result) as fake:
            stats = outbox_worker.run_once('w-test', batch_size=50,
                                           lease_seconds=300,
                                           max_attempts=max_attempts)
        return stats, fake

    def _make_due(self):
        with self.app.app_context():
            for job in (NotificationOutbox.query.execution_options(**OPTS)
                        .filter_by(school_id=self.ids['school_a']).all()):
                job.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
            db.session.commit()

    def test_the_worker_claims_and_sends_a_manual_job(self):
        self._post('a_solo', {'a_solo_0': 'absent'})
        stats, fake = self._run(_Sent(ok=True))
        self.assertEqual((stats['claimed'], stats['sent']), (2, 2))
        self.assertEqual(fake.call_count, 2)
        jobs = self._jobs('a')
        self.assertTrue(all(j.status == NotificationOutbox.STATUS_SENT
                            and j.completed_at is not None for j in jobs))
        # The delivery log is written by the worker, one row per delivery.
        log = self._push_log('a')
        self.assertEqual(len(log), 2)
        self.assertTrue(all(r.status == 'sent' for r in log))
        # The worker sends exactly the stored message.
        args = fake.call_args_list[0].args
        job = jobs[0]
        self.assertEqual((args[1], args[2], args[3]),
                         (job.title, job.body, json.loads(job.data_json)))

    def test_a_transient_failure_retries(self):
        self._post('a_solo', {'a_solo_0': 'present'})
        stats, _ = self._run(_Sent(ok=False, error='unavailable'))
        self.assertEqual(stats['retry'], 2)
        jobs = self._jobs('a')
        self.assertTrue(all(j.status == NotificationOutbox.STATUS_RETRY
                            and j.attempts == 1
                            and j.next_attempt_at is not None for j in jobs))
        # Attendance is untouched by a delivery failure.
        self.assertEqual(self._attendance('a_solo_0')[0].status, 'present')

    def test_repeated_transient_failure_ends_dead(self):
        self._post('a_solo', {'a_solo_0': 'present'})
        self._run(_Sent(ok=False, error='unavailable'), max_attempts=2)
        self._make_due()
        stats, _ = self._run(_Sent(ok=False, error='unavailable'),
                             max_attempts=2)
        self.assertEqual(stats['dead'], 2)
        self.assertTrue(all(j.status == NotificationOutbox.STATUS_DEAD
                            and j.attempts == 2 for j in self._jobs('a')))

    def test_a_permanent_failure_is_dead_immediately(self):
        self._post('a_solo', {'a_solo_0': 'present'})
        stats, _ = self._run(_Sent(ok=False, permanent=True,
                                   error='registration-token-not-registered'))
        self.assertEqual(stats['dead'], 2)
        self.assertTrue(all(j.status == NotificationOutbox.STATUS_DEAD
                            for j in self._jobs('a')))
        self.assertEqual(len(self._attendance('a_solo_0')), 1)

    def test_a_stale_lease_is_reclaimed(self):
        self._post('a_solo', {'a_solo_0': 'present'})
        with self.app.app_context():
            claimed = outbox.claim_batch('dead-worker', limit=50)
            mine = [r for r in claimed if r.school_id == self.ids['school_a']]
            self.assertEqual(len(mine), 2)
            for row in claimed:
                row.locked_at = datetime.utcnow() - timedelta(seconds=900)
            db.session.commit()
            outbox.reclaim_stale('live-worker', lease_seconds=300)
        self.assertTrue(all(j.status == NotificationOutbox.STATUS_PENDING
                            for j in self._jobs('a')))

    def test_the_worker_runs_for_the_manual_flag_alone(self):
        self._post('a_solo', {'a_solo_0': 'present'})
        with self.app.app_context():
            self.assertTrue(outbox.any_enabled())
        stats, _ = self._run(_Sent(ok=True))
        self.assertFalse(stats['disabled'])
        self.assertEqual(stats['sent'], 2)

    def test_the_worker_is_inert_while_all_flags_are_off(self):
        self._post('a_solo', {'a_solo_0': 'present'})
        self.app.config['MANUAL_ATTENDANCE_OUTBOX_ENABLED'] = False
        stats, fake = self._run(_Sent(ok=True))
        self.assertTrue(stats['disabled'])
        self.assertEqual((stats['claimed'], fake.call_count), (0, 0))
        self.assertTrue(all(j.status == NotificationOutbox.STATUS_PENDING
                            for j in self._jobs('a')))


if __name__ == '__main__':
    unittest.main()

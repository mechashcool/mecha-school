# -*- coding: utf-8 -*-
"""Durable transactional outbox for AUTOMATIC school absence (Phase 2).

Producers covered (every automatic-absence writer):
  * _run_auto_absent                — GET /attendance/, POST mark-absent-today,
                                      scheduler normal mode + midnight catch-up
  * _run_auto_absent_for_shift      — scheduler / web trigger, shift mode
  * _run_auto_absent_shiftless      — shift-mode fallback for shiftless students

Guarantees pinned here:
  * FLAG OFF IS THE OLD PATH — _notify_absent_parents runs inline, no job.
  * The absence DECISION is untouched: present / late / on_leave / already
    absent students are never rewritten, and only unmarked students become
    absent.
  * ATOMICITY per existing commit unit — absence rows, feed rows and push jobs
    commit together; a staging failure leaves none of them.
  * NO FIREBASE from the triggering request or scheduler thread.
  * PAYLOAD PARITY against the REAL legacy path (captured, not hand-written).
  * CONCURRENT TRIGGERS never produce duplicate rows, feed rows or jobs.

Runs against the isolated local PostgreSQL test database only.
"""
import json
import threading
import time as _clock
import unittest
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import event, text

from app import create_app
from app.models import (
    db, AcademicYear, AttendanceShift, Grade, MobileDeviceToken, Notification,
    NotificationOutbox, PushNotification, Role, School, Section, Student,
    StudentAttendance, User, parent_students,
)
from app.services import notification_outbox as outbox
from app.services import outbox_worker

OPTS = {'bypass_tenant_scope': True}
TODAY = date.today()
NOW = datetime.combine(TODAY, time(10, 0))      # after every 09:00 cutoff
A_STUDENTS = 10
FLAGS = ('AUTO_ABSENCE_OUTBOX_ENABLED', 'MANUAL_ATTENDANCE_OUTBOX_ENABLED',
         'AIFACE_ATTENDANCE_OUTBOX_ENABLED', 'INSTITUTE_ATTENDANCE_OUTBOX_ENABLED')


def _fake_token(tag):
    """A fabricated registration token. Never a production value."""
    return f'tkn-auto-{tag}-' + ('x' * 60)


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


@contextmanager
def _fixed_clock(at=NOW):
    """Every local-time read in the absence paths returns `at`."""
    with patch('app.utils.attendance_helpers.get_local_now', return_value=at), \
         patch('app.blueprints.attendance.get_local_now', return_value=at):
        yield


class AutoAbsenceOutboxTest(unittest.TestCase):
    """School A (normal mode): one section, ten students a_0..a_9.
    School B (normal mode): one student — exists so isolation can leak to it.
    School S (SHIFT mode): one active shift with a 2-student section
    (s_shift_0/1) and a shiftless section with one student (s_free_0).

    Every student has exactly one linked parent. Parent of a_0 has TWO phones;
    every other parent has one.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    # ── Fixture ──────────────────────────────────────────────────────────────

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.ids = {'students': {}, 'parents': {}, 'users': [], 'sections': [],
                    'grades': [], 'years': [], 'schools': []}
        with self.app.app_context():
            roles = {n: Role.query.filter_by(name=n).first()
                     for n in ('parent', 'school_admin')}
            for n, r in roles.items():
                self.assertIsNotNone(r, f'seed role {n} before running')

            layout = {'a': {'main': A_STUDENTS}, 'b': {'main': 1},
                      's': {'shift': 2, 'free': 1}}
            for tag, sections in layout.items():
                school = School(
                    school_name=f'Auto {tag} {self.suffix}',
                    code=f'AU{tag.upper()}{self.suffix[:6]}',
                    capacity=0, is_active=True, timezone='Asia/Baghdad',
                    att_late_threshold=time(7, 30),
                    att_absence_threshold=time(9, 0),
                    att_departure_time=time(13, 0),
                    weekly_off_days=None,
                    enable_attendance_shifts=(tag == 's'),
                    shift_absent_after_time=time(9, 0) if tag == 's' else None)
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
                                 f'year_{tag}': year.id})
                self.ids['schools'].append(school.id)
                self.ids['years'].append(year.id)
                self.ids['grades'].append(grade.id)

                shift = None
                if tag == 's':
                    shift = AttendanceShift(
                        school_id=school.id, name=f'Morning {self.suffix[:4]}',
                        start_time=time(7, 0), late_after_time=time(7, 30),
                        is_active=True)
                    db.session.add(shift)
                    db.session.flush()
                    self.ids['shift_s'] = shift.id

                for sec, count in sections.items():
                    section = Section(
                        school_id=school.id, academic_year_id=year.id,
                        grade_id=grade.id, name=f'{sec}{tag}{self.suffix[:4]}',
                        capacity=40,
                        shift_id=shift.id if sec == 'shift' else None)
                    db.session.add(section)
                    db.session.flush()
                    self.ids['sections'].append(section.id)
                    self.ids[f'section_{tag}_{sec}'] = section.id
                    for i in range(count):
                        key = f'{tag}_{i}' if sec == 'main' else f'{tag}_{sec}_{i}'
                        student = Student(
                            student_id=f'AU-{key}-{self.suffix}',
                            full_name=f'Pupil {key} {self.suffix}',
                            date_of_birth=date(2015, 1, 1), gender='male',
                            school_id=school.id, academic_year_id=year.id,
                            section_id=section.id, status='active')
                        parent = User(
                            username=f'au_p_{key}_{self.suffix}',
                            email=f'au_p_{key}_{self.suffix}@example.test',
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
                        for n in range(2 if key == 'a_0' else 1):
                            db.session.add(MobileDeviceToken(
                                user_id=parent.id, school_id=school.id,
                                fcm_token=_fake_token(f'{key}{n}{self.suffix}'),
                                platform='android', device_name=f'Phone {n}',
                                is_active=True))

                admin = User(username=f'au_ad_{tag}_{self.suffix}',
                             email=f'au_ad_{tag}_{self.suffix}@example.test',
                             full_name=f'Admin {tag}',
                             role_id=roles['school_admin'].id,
                             school_id=school.id, is_active=True)
                admin.set_password('Password123')
                db.session.add(admin)
                db.session.flush()
                self.ids[f'admin_{tag}'] = admin.id
                self.ids['users'].append(admin.id)
            db.session.commit()

            from app.utils.attendance_helpers import is_holiday_date
            for tag in ('a', 'b', 's'):
                self.assertFalse(
                    is_holiday_date(TODAY, self.ids[f'school_{tag}'],
                                    self._school(tag)),
                    'TODAY is a (global) holiday in this database')

        for name in FLAGS:
            self.app.config[name] = False
        self.app.config['AUTO_ABSENCE_OUTBOX_ENABLED'] = True
        self.client = self.app.test_client()

    def tearDown(self):
        for name in FLAGS:
            self.app.config.pop(name, None)
        with self.app.app_context():
            sids = self.ids['schools']
            for table in ('notification_outbox', 'push_notifications',
                          'notifications', 'student_attendance',
                          'mobile_device_tokens', 'audit_logs'):
                db.session.execute(
                    text(f'DELETE FROM {table} WHERE school_id = ANY(:s)'),
                    {'s': sids})
            db.session.execute(
                text('DELETE FROM parent_students WHERE student_id = ANY(:s)'),
                {'s': list(self.ids['students'].values())})
            db.session.execute(
                text('UPDATE sections SET shift_id = NULL WHERE school_id = ANY(:s)'),
                {'s': sids})
            db.session.execute(
                text('UPDATE grades SET shift_id = NULL WHERE school_id = ANY(:s)'),
                {'s': sids})
            db.session.execute(
                text('DELETE FROM attendance_shifts WHERE school_id = ANY(:s)'),
                {'s': sids})
            for model, pks in ((Student, list(self.ids['students'].values())),
                               (User, self.ids['users']),
                               (Section, self.ids['sections']),
                               (Grade, self.ids['grades']),
                               (AcademicYear, self.ids['years']),
                               (School, sids)):
                for pk in pks:
                    row = db.session.get(model, pk, execution_options=OPTS)
                    if row is not None:
                        db.session.delete(row)
                db.session.flush()
            db.session.commit()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _school(self, tag):
        return db.session.get(School, self.ids[f'school_{tag}'],
                              execution_options=OPTS)

    def _year(self, tag):
        return db.session.get(AcademicYear, self.ids[f'year_{tag}'],
                              execution_options=OPTS)

    def _sid(self, key):
        return self.ids['students'][key]

    def _keys(self, tag):
        return sorted(k for k in self.ids['students'] if k.startswith(f'{tag}_'))

    def _login(self, user_id):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True

    def _flashes(self):
        with self.client.session_transaction() as sess:
            return list(sess.get('_flashes', []))

    # entry points --------------------------------------------------------

    def _run_normal(self, tag='a', target_date=TODAY, clock=True):
        """_run_auto_absent exactly as the scheduler calls it."""
        from app.blueprints.attendance import _run_auto_absent
        with self.app.app_context(), (_fixed_clock() if clock else nullcontext()):
            school = self._school(tag)
            return _run_auto_absent(school, self._year(tag), school,
                                    target_date=target_date)

    def _run_scheduler(self, tag, clock=True):
        """The per-school scheduler entry point (normal or shift mode)."""
        from app.services.auto_attendance import _check_school
        with self.app.app_context(), (_fixed_clock() if clock else nullcontext()):
            _check_school(self._school(tag))

    def _get_index(self, tag='a'):
        self._login(self.ids[f'admin_{tag}'])
        with _fixed_clock():
            return self.client.get('/attendance/')

    def _post_mark_absent(self, tag='a'):
        self._login(self.ids[f'admin_{tag}'])
        with _fixed_clock():
            return self.client.post('/attendance/mark-absent-today')

    # state ---------------------------------------------------------------

    def _rows(self, key, on=TODAY):
        with self.app.app_context():
            return (StudentAttendance.query.execution_options(**OPTS)
                    .filter_by(student_id=self._sid(key), date=on).all())

    def _jobs(self, tag='a'):
        with self.app.app_context():
            return (NotificationOutbox.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids[f'school_{tag}'])
                    .order_by(NotificationOutbox.id.asc()).all())

    def _feed(self, tag='a'):
        with self.app.app_context():
            return (Notification.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids[f'school_{tag}'])
                    .order_by(Notification.id.asc()).all())

    def _feed_tuples(self, tag='a'):
        return sorted((r.school_id, r.title, r.body, r.ntype, r.target_user_id,
                       r.created_by) for r in self._feed(tag))

    def _seed_row(self, key, status, *, check_in=None, source=None, tag='a'):
        with self.app.app_context():
            db.session.add(StudentAttendance(
                student_id=self._sid(key), school_id=self.ids[f'school_{tag}'],
                academic_year_id=self.ids[f'year_{tag}'], date=TODAY,
                status=status, check_in=check_in, source=source))
            db.session.commit()

    def _wipe_day(self, tag):
        with self.app.app_context():
            sid = self.ids[f'school_{tag}']
            for table in ('student_attendance', 'notifications',
                          'push_notifications', 'notification_outbox'):
                db.session.execute(
                    text(f'DELETE FROM {table} WHERE school_id = :s'), {'s': sid})
            db.session.commit()

    def _expected_jobs(self, keys):
        return sum(2 if k == 'a_0' else 1 for k in keys)

    def _assert_consistent(self, tag, *, absent_keys):
        """Exactly one row per student; one feed row per parent and one job per
        token for EXACTLY the students that became absent — no duplicates."""
        for key in self._keys(tag):
            self.assertLessEqual(len(self._rows(key)), 1, key)
        jobs = self._jobs(tag)
        self.assertEqual(len(jobs), self._expected_jobs(absent_keys))
        self.assertEqual(len({j.dedup_key for j in jobs}), len(jobs))
        self.assertEqual(sorted(r.target_user_id for r in self._feed(tag)),
                         sorted(self.ids['parents'][k] for k in absent_keys))

    # mocks ---------------------------------------------------------------

    @contextmanager
    def _no_network_push(self):
        """Every push entry point, each failing loudly if the run uses it."""
        boom = AssertionError('Firebase must not be called by auto-absence')
        with patch('app.services.fcm_service.is_enabled', return_value=True), \
             patch('app.services.fcm_service._send_one',
                   side_effect=boom) as send_one, \
             patch('app.services.fcm_service.send_push_to_user',
                   side_effect=boom) as multi, \
             patch('app.services.fcm_service.send_to_device_token',
                   side_effect=boom) as direct, \
             patch('app.blueprints.attendance._notify_absent_parents',
                   side_effect=boom) as inline:
            mocks = {'_send_one': send_one, 'send_push_to_user': multi,
                     'send_to_device_token': direct,
                     '_notify_absent_parents': inline}
            yield mocks
        for name, mock in mocks.items():
            self.assertEqual(mock.call_count, 0, f'{name} was called')

    @contextmanager
    def _capture_legacy_pushes(self):
        """Run the REAL inline path, capturing what it hands to Firebase."""
        captured = []
        lock = threading.Lock()

        def _grab(user_id, title, body, data=None, *_a, **_k):
            with lock:
                captured.append((user_id, title, body, dict(data or {})))
            return 1, 0

        with patch('app.services.fcm_service.is_enabled', return_value=True), \
             patch('app.services.fcm_service.send_push_to_user',
                   side_effect=_grab):
            yield captured

    @staticmethod
    def _sorted(items):
        return sorted(items, key=lambda m: (m[0], m[1],
                                            json.dumps(m[3], sort_keys=True)))

    def _job_messages_per_parent(self, jobs):
        per = {}
        for j in jobs:
            per.setdefault(j.user_id, set()).add(
                (j.user_id, j.title, j.body,
                 json.dumps(json.loads(j.data_json), sort_keys=True)))
        for uid, msgs in per.items():
            self.assertEqual(len(msgs), 1, f'parent {uid} phones differ')
        return [(uid, t, b, json.loads(d))
                for (uid, t, b, d) in (next(iter(m)) for m in per.values())]


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 1 / 2 — Flags
# ═════════════════════════════════════════════════════════════════════════════

class FlagTest(AutoAbsenceOutboxTest):

    def test_flag_defaults_to_false(self):
        fresh = create_app('testing')
        for name in FLAGS:
            self.assertFalse(fresh.config[name], name)
        with fresh.app_context():
            self.assertFalse(outbox.auto_absence_enabled())
            self.assertFalse(outbox.any_enabled())

    def test_the_four_flags_are_independent(self):
        readers = {'AUTO_ABSENCE_OUTBOX_ENABLED': outbox.auto_absence_enabled,
                   'MANUAL_ATTENDANCE_OUTBOX_ENABLED': outbox.manual_enabled,
                   'AIFACE_ATTENDANCE_OUTBOX_ENABLED': outbox.aiface_enabled,
                   'INSTITUTE_ATTENDANCE_OUTBOX_ENABLED': outbox.enabled}
        with self.app.app_context():
            for on in FLAGS:
                for name in FLAGS:
                    self.app.config[name] = (name == on)
                for name, reader in readers.items():
                    self.assertEqual(reader(), name == on, (on, name))
                self.assertTrue(outbox.any_enabled(), on)


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 3 — Flag OFF keeps the inline path
# ═════════════════════════════════════════════════════════════════════════════

class FlagOffTest(AutoAbsenceOutboxTest):

    def setUp(self):
        super().setUp()
        self.app.config['AUTO_ABSENCE_OUTBOX_ENABLED'] = False

    def test_flag_off_uses_the_inline_helper_and_writes_no_job(self):
        with patch('app.blueprints.attendance._notify_absent_parents') as inline:
            result = self._run_normal('a')
        self.assertEqual(result['count'], A_STUDENTS)
        self.assertNotIn('outbox_failed', result)
        self.assertEqual(inline.call_count, A_STUDENTS)
        self.assertEqual(self._jobs('a'), [])

    def test_flag_off_real_legacy_side_effects(self):
        with self._capture_legacy_pushes() as pushes:
            self._run_normal('a')
        self.assertEqual(len(pushes), A_STUDENTS)          # one per parent
        self.assertEqual(len(self._feed('a')), A_STUDENTS)
        self.assertEqual(self._jobs('a'), [])

    def test_flag_off_shift_mode_uses_the_inline_helper(self):
        with patch('app.blueprints.attendance._notify_absent_parents') as inline:
            self._run_scheduler('s')
        self.assertEqual(inline.call_count, 3)
        self.assertEqual(self._jobs('s'), [])


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 4 / 5 — Flag ON: one and ten absences
# ═════════════════════════════════════════════════════════════════════════════

class FlagOnTest(AutoAbsenceOutboxTest):

    def test_one_automatic_absence(self):
        for key in self._keys('a')[1:]:                    # only a_0 unmarked
            self._seed_row(key, 'present', check_in=time(7, 0))
        with self._no_network_push():
            result = self._run_normal('a')
        self.assertEqual(result['count'], 1)
        rows = self._rows('a_0')
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].status, rows[0].source), ('absent', 'automatic'))
        jobs = self._jobs('a')
        self.assertEqual(len(jobs), 2)                     # a_0 parent: 2 phones
        self.assertEqual({j.event_type for j in jobs},
                         {NotificationOutbox.EVENT_SCHOOL_ATTENDANCE_AUTO_ABSENCE})
        self.assertTrue(all(j.status == NotificationOutbox.STATUS_PENDING
                            for j in jobs))
        self.assertEqual([r.target_user_id for r in self._feed('a')],
                         [self.ids['parents']['a_0']])
        # The delivery log belongs to the worker now.
        with self.app.app_context():
            self.assertEqual(PushNotification.query.execution_options(**OPTS)
                             .filter_by(school_id=self.ids['school_a']).count(), 0)

    def test_ten_automatic_absences(self):
        with self._no_network_push():
            result = self._run_normal('a')
        self.assertEqual(result['count'], A_STUDENTS)
        self._assert_consistent('a', absent_keys=self._keys('a'))
        for key in self._keys('a'):
            self.assertEqual(self._rows(key)[0].status, 'absent')
        jobs = self._jobs('a')
        rows_by_id = {self._rows(k)[0].id for k in self._keys('a')}
        self.assertEqual({int(j.dedup_key.split(':')[4]) for j in jobs}, rows_by_id)


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 7 / 8 / 9 / 10 — The absence decision is unchanged
# ═════════════════════════════════════════════════════════════════════════════

class DecisionPreservedTest(AutoAbsenceOutboxTest):

    def _seed_mixed(self):
        self._seed_row('a_1', 'present', check_in=time(7, 0), source='manual')
        self._seed_row('a_2', 'late', check_in=time(8, 0), source='manual')
        self._seed_row('a_3', 'on_leave', source='leave')
        self._seed_row('a_4', 'absent', source='automatic')
        return [k for k in self._keys('a') if k not in ('a_1', 'a_2', 'a_3', 'a_4')]

    def test_existing_records_are_never_rewritten_on_either_path(self):
        for flag in (False, True):
            self.app.config['AUTO_ABSENCE_OUTBOX_ENABLED'] = flag
            unmarked = self._seed_mixed()
            with self._capture_legacy_pushes() as pushes:
                result = self._run_normal('a')
            self.assertEqual(result['count'], len(unmarked), flag)
            expected = {'a_1': ('present', time(7, 0)), 'a_2': ('late', time(8, 0)),
                        'a_3': ('on_leave', None), 'a_4': ('absent', None)}
            for key, (status, check_in) in expected.items():
                rows = self._rows(key)
                self.assertEqual(len(rows), 1, (flag, key))
                self.assertEqual((rows[0].status, rows[0].check_in),
                                 (status, check_in), (flag, key))
            notified = {self.ids['parents'][k] for k in unmarked}
            if flag:
                self.assertEqual(pushes, [])
                self.assertEqual({j.user_id for j in self._jobs('a')}, notified)
            else:
                self.assertEqual({p[0] for p in pushes}, notified)
            self.assertEqual({r.target_user_id for r in self._feed('a')}, notified)
            self._wipe_day('a')

    def test_already_absent_is_not_duplicated_on_rerun(self):
        with self._no_network_push():
            self._run_normal('a')
            first_jobs = [j.dedup_key for j in self._jobs('a')]
            again = self._run_normal('a')
            self._run_scheduler('a')
        self.assertEqual(again['count'], 0)
        self.assertEqual([j.dedup_key for j in self._jobs('a')], first_jobs)
        self._assert_consistent('a', absent_keys=self._keys('a'))

    def test_before_cutoff_nothing_happens(self):
        early = datetime.combine(TODAY, time(8, 0))
        from app.services.auto_attendance import _check_school
        with self.app.app_context(), _fixed_clock(early), self._no_network_push():
            _check_school(self._school('a'))
        for key in self._keys('a'):
            self.assertEqual(self._rows(key), [])
        self.assertEqual(self._jobs('a'), [])


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 12 / 13 / 14 — Every entry point uses the durable path
# ═════════════════════════════════════════════════════════════════════════════

class EntryPointTest(AutoAbsenceOutboxTest):

    def test_get_attendance_index_stages_without_firebase(self):
        with self._no_network_push():
            resp = self._get_index('a')
        self.assertEqual(resp.status_code, 200)
        self._assert_consistent('a', absent_keys=self._keys('a'))
        self.assertEqual(self._rows('a_0')[0].recorded_by, self.ids['admin_a'])

    def test_get_index_shift_mode_stages_without_firebase(self):
        with self._no_network_push():
            resp = self._get_index('s')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self._jobs('s')), 3)

    def test_repeated_get_requests_do_not_duplicate(self):
        with self._no_network_push():
            for _ in range(3):
                self.assertEqual(self._get_index('a').status_code, 200)
        self._assert_consistent('a', absent_keys=self._keys('a'))

    def test_mark_absent_today_stages_without_firebase(self):
        with self._no_network_push():
            resp = self._post_mark_absent('a')
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(any(cat == 'success' and f' {A_STUDENTS} ' in msg
                            for cat, msg in self._flashes()))
        self._assert_consistent('a', absent_keys=self._keys('a'))

    def test_mark_absent_today_shift_mode(self):
        with self._no_network_push():
            resp = self._post_mark_absent('s')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(self._jobs('s')), 3)

    def test_scheduler_normal_mode_stages_without_firebase(self):
        with self._no_network_push():
            self._run_scheduler('a')
        self._assert_consistent('a', absent_keys=self._keys('a'))

    def test_scheduler_shift_mode_covers_shift_and_shiftless(self):
        with self._no_network_push():
            self._run_scheduler('s')
        jobs = self._jobs('s')
        self.assertEqual(len(jobs), 3)
        rows = {k: self._rows(k)[0] for k in self._keys('s')}
        self.assertEqual(rows['s_shift_0'].shift_id, self.ids['shift_s'])
        self.assertEqual(rows['s_free_0'].shift_id, None)
        by_user = {j.user_id: json.loads(j.data_json) for j in jobs}
        for key in ('s_shift_0', 's_shift_1'):
            self.assertIn('shift_name', by_user[self.ids['parents'][key]])
        self.assertNotIn('shift_name', by_user[self.ids['parents']['s_free_0']])

    def test_scheduler_does_not_need_the_worker(self):
        """The run returns with jobs pending; nothing waits for delivery."""
        with self._no_network_push():
            started = _clock.monotonic()
            self._run_scheduler('a')
            elapsed = _clock.monotonic() - started
        self.assertTrue(all(j.status == NotificationOutbox.STATUS_PENDING
                            for j in self._jobs('a')))
        self.assertLess(elapsed, 30)


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 11 — Concurrent triggers
# ═════════════════════════════════════════════════════════════════════════════

class ConcurrencyTest(AutoAbsenceOutboxTest):
    """Real overlapping runs. A barrier holds every run at the point where it
    has ALREADY decided who is unmarked and is about to insert — the worst
    case, in which both runs try to insert the same students."""

    def _overlap(self, *runners, flag=True):
        self.app.config['AUTO_ABSENCE_OUTBOX_ENABLED'] = flag
        barrier = threading.Barrier(len(runners), timeout=15)
        real = outbox.auto_absence_enabled
        errors, results = [], []

        def _held():
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return real()

        def _run(fn):
            try:
                results.append(fn())
            except Exception as exc:              # pragma: no cover - reported
                errors.append(exc)

        # ONE process-wide clock patch around every thread: mock.patch is
        # global, so per-thread enter/exit would race on restore.
        with patch.object(outbox, 'auto_absence_enabled', side_effect=_held), \
             _fixed_clock(), self._capture_legacy_pushes() as pushes:
            threads = [threading.Thread(target=_run, args=(fn,)) for fn in runners]
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        self.assertEqual(errors, [])
        return results, pushes

    def _client_call(self, method, url, user):
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(user)
            sess['_fresh'] = True

        def _call():
            return getattr(client, method)(url).status_code
        return _call

    def _normal(self):
        return self._run_normal(clock=False)

    def _sched(self, tag):
        return lambda: self._run_scheduler(tag, clock=False)

    def test_two_simultaneous_run_auto_absent_calls(self):
        results, pushes = self._overlap(self._normal, self._normal)
        self.assertEqual(sorted(r['count'] for r in results), [0, A_STUDENTS])
        self.assertEqual(pushes, [])
        self._assert_consistent('a', absent_keys=self._keys('a'))

    def test_scheduler_and_get_attendance_overlap(self):
        get = self._client_call('get', '/attendance/', self.ids['admin_a'])
        results, pushes = self._overlap(self._sched('a'), get)
        self.assertIn(200, results)                      # no uncontrolled 500
        self.assertEqual(pushes, [])
        self._assert_consistent('a', absent_keys=self._keys('a'))

    def test_mark_absent_today_and_scheduler_overlap(self):
        post = self._client_call('post', '/attendance/mark-absent-today',
                                 self.ids['admin_a'])
        results, pushes = self._overlap(post, self._sched('a'))
        self.assertIn(302, results)
        self.assertEqual(pushes, [])
        self._assert_consistent('a', absent_keys=self._keys('a'))

    def test_shift_mode_scheduler_and_get_overlap(self):
        get = self._client_call('get', '/attendance/', self.ids['admin_s'])
        results, pushes = self._overlap(self._sched('s'), get)
        self.assertIn(200, results)
        self.assertEqual(pushes, [])
        self.assertEqual(len(self._jobs('s')), 3)
        self.assertEqual(len(self._feed('s')), 3)

    def test_legacy_overlap_still_behaves_as_before(self):
        """Flag OFF: the same overlap notifies each parent exactly once."""
        results, pushes = self._overlap(self._normal, self._normal, flag=False)
        self.assertEqual(sorted(r['count'] for r in results), [0, A_STUDENTS])
        self.assertEqual(len(pushes), A_STUDENTS)
        self.assertEqual(len(self._feed('a')), A_STUDENTS)
        self.assertEqual(self._jobs('a'), [])

    def test_a_winner_committing_mid_run_leaves_the_loser_clean(self):
        """Deterministic interleaving: another transaction inserts a_0 AFTER
        this run read the existing rows and commits while this run inserts."""
        inserted, release = threading.Event(), threading.Event()

        def _winner():
            with self.app.app_context():
                with db.engine.connect() as conn:
                    trans = conn.begin()
                    conn.execute(text(
                        'INSERT INTO student_attendance (student_id, school_id, '
                        'academic_year_id, date, status, source, created_at) '
                        "VALUES (:st, :sc, :y, :d, 'present', 'manual', now())"),
                        {'st': self._sid('a_0'), 'sc': self.ids['school_a'],
                         'y': self.ids['year_a'], 'd': TODAY})
                    inserted.set()
                    release.wait(15)
                    trans.commit()

        real = outbox.auto_absence_enabled

        def _read_then_release():
            value = real()
            threading.Timer(1.0, release.set).start()
            return value

        winner = threading.Thread(target=_winner)
        winner.start()
        self.assertTrue(inserted.wait(15))
        with patch.object(outbox, 'auto_absence_enabled',
                          side_effect=_read_then_release), self._no_network_push():
            result = self._run_normal('a')
        winner.join(20)
        self.assertEqual(result['count'], 0)             # conflict, nothing of ours
        self.assertNotIn('outbox_failed', result)
        self.assertEqual(self._rows('a_0')[0].status, 'present')
        self.assertEqual(self._jobs('a'), [])
        self.assertEqual(self._feed('a'), [])
        # Next tick marks the rest exactly once.
        with self._no_network_push():
            self.assertEqual(self._run_normal('a')['count'], A_STUDENTS - 1)
        self._assert_consistent('a', absent_keys=self._keys('a')[1:])


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 15 — Tenant isolation
# ═════════════════════════════════════════════════════════════════════════════

class IsolationTest(AutoAbsenceOutboxTest):

    def test_school_a_run_never_touches_school_b(self):
        with self._no_network_push():
            self._run_normal('a')
            self._get_index('a')
            self._post_mark_absent('a')
            self._run_scheduler('a')
        self.assertEqual(self._rows('b_0'), [])
        self.assertEqual(self._jobs('b'), [])
        self.assertEqual(self._feed('b'), [])

    def test_every_job_matches_its_own_school_on_all_three_axes(self):
        with self._no_network_push():
            self._run_normal('a')
            self._run_normal('b')
            self._run_scheduler('s')
        with self.app.app_context():
            for tag in ('a', 'b', 's'):
                sid = self.ids[f'school_{tag}']
                jobs = self._jobs(tag)
                self.assertTrue(jobs, tag)
                for job in jobs:
                    user = db.session.get(User, job.user_id, execution_options=OPTS)
                    token = db.session.get(MobileDeviceToken, job.device_token_id,
                                           execution_options=OPTS)
                    self.assertEqual((job.school_id, user.school_id, token.school_id),
                                     (sid, sid, sid))

    def test_staging_refuses_a_cross_school_student(self):
        with self.app.app_context():
            school_a = self._school('a')
            student_b = db.session.get(Student, self._sid('b_0'),
                                       execution_options=OPTS)
            with self.assertRaises(ValueError) as caught:
                outbox.stage_auto_absence_deliveries(
                    school_a, [(student_b, 1, 't', 'b', {})], on_date=TODAY)
            self.assertIn('cross-school', str(caught.exception))
            db.session.rollback()
        self.assertEqual(self._jobs('a') + self._jobs('b'), [])

    def test_a_parents_token_in_another_school_is_never_targeted(self):
        with self.app.app_context():
            tok = (MobileDeviceToken.query.execution_options(**OPTS)
                   .filter_by(user_id=self.ids['parents']['a_0'])
                   .order_by(MobileDeviceToken.id.asc()).first())
            tok.school_id = self.ids['school_b']
            db.session.commit()
            moved = tok.id
        with self._no_network_push():
            self._run_normal('a')
        self.assertNotIn(moved, {j.device_token_id for j in self._jobs('a')})
        self.assertEqual(len(self._jobs('a')), A_STUDENTS)
        self.assertEqual(self._jobs('b'), [])

    def test_corrupt_cross_school_parent_link_matches_legacy(self):
        """A school-B parent wrongly linked to school-A student a_3: the tenant
        guard rejects that student's feed rows on BOTH paths, so a_3's parents
        get nothing, a_3 is still absent, everyone else is notified, and
        school B receives nothing."""
        with self.app.app_context():
            db.session.execute(parent_students.insert().values(
                user_id=self.ids['parents']['b_0'], student_id=self._sid('a_3')))
            db.session.commit()
        outcomes = {}
        for flag in (False, True):
            self.app.config['AUTO_ABSENCE_OUTBOX_ENABLED'] = flag
            with self._capture_legacy_pushes() as pushes:
                result = self._run_normal('a')
            self.assertEqual(result['count'], A_STUDENTS, flag)
            self.assertEqual(self._rows('a_3')[0].status, 'absent')
            feed = {r.target_user_id for r in self._feed('a')}
            notified = feed | {p[0] for p in pushes} | {j.user_id for j in self._jobs('a')}
            outcomes[flag] = (feed, notified)
            self.assertNotIn(self.ids['parents']['a_3'], notified)
            self.assertNotIn(self.ids['parents']['b_0'], notified)
            self.assertEqual(self._feed('b'), [])
            self.assertEqual(self._jobs('b'), [])
            self._wipe_day('a')
        self.assertEqual(outcomes[False], outcomes[True])
        with self.app.app_context():
            db.session.execute(text(
                'DELETE FROM parent_students WHERE student_id = :s AND user_id = :u'),
                {'s': self._sid('a_3'), 'u': self.ids['parents']['b_0']})
            db.session.commit()

    def test_inactive_tokens_and_unlinked_students(self):
        with self.app.app_context():
            (MobileDeviceToken.query.execution_options(**OPTS)
             .filter_by(user_id=self.ids['parents']['a_1'])
             .update({'is_active': False}))
            db.session.execute(text(
                'DELETE FROM parent_students WHERE student_id = :s'),
                {'s': self._sid('a_2')})
            db.session.commit()
        with self._no_network_push():
            result = self._run_normal('a')
        self.assertEqual(result['count'], A_STUDENTS)       # all still absent
        users = [j.user_id for j in self._jobs('a')]
        self.assertNotIn(self.ids['parents']['a_1'], users)  # no active phone
        self.assertNotIn(self.ids['parents']['a_2'], users)  # not linked
        feed = {r.target_user_id for r in self._feed('a')}
        self.assertIn(self.ids['parents']['a_1'], feed)      # feed row still
        self.assertNotIn(self.ids['parents']['a_2'], feed)


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 16 — Staging failure
# ═════════════════════════════════════════════════════════════════════════════

class StagingFailureTest(AutoAbsenceOutboxTest):

    def _boom(self):
        return patch.object(outbox, 'stage_auto_absence_deliveries',
                            side_effect=RuntimeError('staging exploded'))

    def _assert_nothing_saved(self, tag):
        for key in self._keys(tag):
            self.assertEqual(self._rows(key), [], key)
        self.assertEqual(self._jobs(tag), [])
        self.assertEqual(self._feed(tag), [])

    def test_staging_failure_rolls_the_whole_unit_back(self):
        with self._boom(), self._no_network_push():
            result = self._run_normal('a')
        self.assertEqual(result['count'], 0)
        self.assertTrue(result['outbox_failed'])
        self._assert_nothing_saved('a')

    def test_the_next_run_recovers(self):
        with self._boom():
            self._run_scheduler('a')
        with self._no_network_push():
            self._run_scheduler('a')
        self._assert_consistent('a', absent_keys=self._keys('a'))

    def test_get_index_still_renders_after_a_staging_failure(self):
        with self._boom():
            resp = self._get_index('a')
        self.assertEqual(resp.status_code, 200)
        self._assert_nothing_saved('a')

    def test_mark_absent_today_reports_the_failure(self):
        with self._boom():
            resp = self._post_mark_absent('a')
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(any(cat == 'danger' for cat, _ in self._flashes()))
        self._assert_nothing_saved('a')

    def test_shift_mode_failure_is_per_unit_and_reported(self):
        with self._boom():
            resp = self._post_mark_absent('s')
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(any(cat == 'danger' for cat, _ in self._flashes()))
        self._assert_nothing_saved('s')

    def test_scheduler_continues_after_a_staging_failure(self):
        from app.services.auto_attendance import _check_school
        with self._boom(), self.app.app_context(), _fixed_clock():
            _check_school(self._school('s'))     # must not raise
        self._assert_nothing_saved('s')


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 18 — Payload parity with the REAL legacy path
# ═════════════════════════════════════════════════════════════════════════════

class PayloadParityTest(AutoAbsenceOutboxTest):
    """Each producer runs twice for the SAME students on the SAME day: once
    through the real legacy code (flag off, pushes captured), once through the
    outbox. Push messages and feed rows must be EQUAL."""

    def _parity(self, tag, run):
        self.app.config['AUTO_ABSENCE_OUTBOX_ENABLED'] = False
        with self._capture_legacy_pushes() as legacy:
            run()
        legacy_feed = self._feed_tuples(tag)
        self.assertTrue(legacy)
        self._wipe_day(tag)

        self.app.config['AUTO_ABSENCE_OUTBOX_ENABLED'] = True
        with self._capture_legacy_pushes() as durable_pushes:
            run()
        self.assertEqual(durable_pushes, [])
        messages = self._job_messages_per_parent(self._jobs(tag))
        self.assertEqual(self._sorted(messages), self._sorted(legacy))
        self.assertEqual(self._feed_tuples(tag), legacy_feed)
        return legacy

    def test_normal_mode_scheduler_parity(self):
        legacy = self._parity('a', lambda: self._run_scheduler('a'))
        data = legacy[0][3]
        self.assertEqual(legacy[0][1], 'تنبيه غياب')
        self.assertEqual((data['type'], data['ntype'], data['action'],
                          data['status'], data['screen']),
                         ('attendance', 'attendance', 'absent', 'absent',
                          'attendance'))
        self.assertEqual(data['date'], TODAY.isoformat())
        self.assertNotIn('route', data)
        self.assertNotIn('shift_name', data)

    def test_get_index_parity(self):
        self._parity('a', lambda: self._get_index('a'))

    def test_mark_absent_today_parity(self):
        self._parity('a', lambda: self._post_mark_absent('a'))

    def test_shift_and_shiftless_parity(self):
        legacy = self._parity('s', lambda: self._run_scheduler('s'))
        by_user = {m[0]: m[3] for m in legacy}
        self.assertIn('shift_name',
                      by_user[self.ids['parents']['s_shift_0']])
        self.assertNotIn('shift_name',
                         by_user[self.ids['parents']['s_free_0']])


# ═════════════════════════════════════════════════════════════════════════════
#  Dedup key
# ═════════════════════════════════════════════════════════════════════════════

class DedupTest(AutoAbsenceOutboxTest):

    def test_the_dedup_key_rejects_a_duplicate(self):
        from sqlalchemy.exc import IntegrityError
        with self._no_network_push():
            self._run_normal('a')
        with self.app.app_context():
            student = db.session.get(Student, self._sid('a_1'),
                                     execution_options=OPTS)
            row = self._rows('a_1')[0]
            outbox.stage_auto_absence_deliveries(
                self._school('a'), [(student, row.id, 't', 'b', {})],
                on_date=TODAY)
            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()
        self._assert_consistent('a', absent_keys=self._keys('a'))

    def test_key_shape(self):
        with self._no_network_push():
            self._run_normal('a')
        for job in self._jobs('a'):
            parts = job.dedup_key.split(':')
            self.assertEqual(parts[0], 'school_attendance_auto_absence')
            self.assertEqual(parts[2], TODAY.strftime('%Y%m%d'))
            self.assertEqual(parts[3], 'absent')
            self.assertEqual(int(parts[5]), job.user_id)
            self.assertEqual(int(parts[6]), job.device_token_id)


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 17 — The existing worker, unchanged
# ═════════════════════════════════════════════════════════════════════════════

class WorkerTest(AutoAbsenceOutboxTest):

    def _deliver(self, result, max_attempts=5):
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

    def test_worker_sends_with_only_the_auto_absence_flag(self):
        with self._no_network_push():
            self._run_normal('a')
        stats, fake = self._deliver(_Sent(ok=True))
        self.assertFalse(stats['disabled'])
        self.assertEqual(stats['sent'], self._expected_jobs(self._keys('a')))
        jobs = self._jobs('a')
        self.assertTrue(all(j.status == NotificationOutbox.STATUS_SENT for j in jobs))
        sent = {(c.args[1], c.args[2], json.dumps(c.args[3], sort_keys=True))
                for c in fake.call_args_list}
        stored = {(j.title, j.body, json.dumps(json.loads(j.data_json),
                                              sort_keys=True)) for j in jobs}
        self.assertEqual(sent, stored)

    def test_transient_then_dead(self):
        with self._no_network_push():
            self._run_normal('a')
        n = self._expected_jobs(self._keys('a'))
        stats, _ = self._deliver(_Sent(ok=False, error='unavailable'), max_attempts=2)
        self.assertEqual(stats['retry'], n)
        self._make_due()
        stats, _ = self._deliver(_Sent(ok=False, error='unavailable'), max_attempts=2)
        self.assertEqual(stats['dead'], n)
        # Attendance is untouched by delivery failure.
        self.assertTrue(all(self._rows(k)[0].status == 'absent'
                            for k in self._keys('a')))

    def test_permanent_failure_is_dead_immediately(self):
        with self._no_network_push():
            self._run_normal('a')
        stats, _ = self._deliver(_Sent(ok=False, permanent=True,
                                       error='registration-token-not-registered'))
        self.assertEqual(stats['dead'], self._expected_jobs(self._keys('a')))

    def test_stale_lease_is_reclaimed(self):
        with self._no_network_push():
            self._run_normal('a')
        with self.app.app_context():
            claimed = outbox.claim_batch('dead-worker', limit=100)
            for row in claimed:
                row.locked_at = datetime.utcnow() - timedelta(seconds=900)
            db.session.commit()
            outbox.reclaim_stale('live-worker', lease_seconds=300)
        self.assertTrue(all(j.status == NotificationOutbox.STATUS_PENDING
                            for j in self._jobs('a')))

    def test_worker_is_inert_with_every_flag_off(self):
        with self._no_network_push():
            self._run_normal('a')
        self.app.config['AUTO_ABSENCE_OUTBOX_ENABLED'] = False
        stats, fake = self._deliver(_Sent(ok=True))
        self.assertTrue(stats['disabled'])
        self.assertEqual(fake.call_count, 0)


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 6 — Large realistic school from the preserved synthetic dataset
# ═════════════════════════════════════════════════════════════════════════════

class LargeSchoolTest(unittest.TestCase):
    """One real synthetic school (~1,000 students) from the cloned dataset.

    Temporary state (tokens, one day's absences, feed rows, jobs, delivery
    log) is created on a date proven empty beforehand and removed afterwards,
    so the synthetic dataset is left exactly as found.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    def setUp(self):
        from app.utils.attendance_helpers import is_holiday_date
        from app.utils.decorators import get_active_year
        with self.app.app_context():
            row = db.session.execute(text(
                "SELECT s.id, count(st.id) FROM schools s "
                "JOIN students st ON st.school_id = s.id AND st.status = 'active' "
                "JOIN parent_students ps ON ps.student_id = st.id "
                "WHERE s.is_active GROUP BY s.id ORDER BY count(st.id) DESC LIMIT 1"
            )).first()
            if row is None or row[1] < 500:
                self.skipTest('no large synthetic school in this database')
            self.school_id, self.students = row
            school = db.session.get(School, self.school_id, execution_options=OPTS)
            if getattr(school, 'enable_attendance_shifts', False) or school.is_institute:
                self.skipTest('largest school is not a normal-mode school')
            self.year_id = get_active_year(self.school_id).id
            day = TODAY + timedelta(days=30)
            for _ in range(60):
                empty = db.session.execute(text(
                    'SELECT count(*) FROM student_attendance '
                    'WHERE school_id = :s AND date = :d'),
                    {'s': self.school_id, 'd': day}).scalar() == 0
                if empty and not is_holiday_date(day, self.school_id, school):
                    break
                day += timedelta(days=1)
            self.day = day
            self.baseline = {t: db.session.execute(text(
                f'SELECT count(*) FROM {t} WHERE school_id = :s'),
                {'s': self.school_id}).scalar()
                for t in ('notifications', 'push_notifications',
                          'notification_outbox', 'mobile_device_tokens')}
            self.max_push = db.session.execute(text(
                'SELECT coalesce(max(id), 0) FROM push_notifications')).scalar()
            parents = [r[0] for r in db.session.execute(text(
                'SELECT DISTINCT ps.user_id FROM parent_students ps '
                "JOIN students st ON st.id = ps.student_id "
                "WHERE st.school_id = :s AND st.status = 'active' ORDER BY 1"),
                {'s': self.school_id})]
            tag = uuid4().hex[:8]
            # Realistic fan-out: every parent one phone, every 4th parent two.
            for i, uid in enumerate(parents):
                for n in range(2 if i % 4 == 0 else 1):
                    db.session.add(MobileDeviceToken(
                        user_id=uid, school_id=self.school_id,
                        fcm_token=f'tkn-large-{tag}-{uid}-{n}-' + 'x' * 40,
                        platform='android', device_name=f'Load {n}',
                        is_active=True))
            db.session.commit()
            self.token_ids = [r[0] for r in db.session.execute(text(
                'SELECT id FROM mobile_device_tokens WHERE fcm_token LIKE :p'),
                {'p': f'tkn-large-{tag}-%'})]
            self.parents = len(parents)
        for name in FLAGS:
            self.app.config[name] = False

    def tearDown(self):
        for name in FLAGS:
            self.app.config.pop(name, None)
        self._wipe()
        with self.app.app_context():
            db.session.execute(text(
                'DELETE FROM mobile_device_tokens WHERE id = ANY(:ids)'),
                {'ids': self.token_ids})
            db.session.commit()
            after = {t: db.session.execute(text(
                f'SELECT count(*) FROM {t} WHERE school_id = :s'),
                {'s': self.school_id}).scalar() for t in self.baseline}
        self.assertEqual(after, self.baseline, 'synthetic dataset not restored')

    def _wipe(self):
        with self.app.app_context():
            db.session.execute(text(
                "DELETE FROM student_attendance WHERE school_id = :s AND date = :d "
                "AND source = 'automatic'"), {'s': self.school_id, 'd': self.day})
            db.session.execute(text(
                'DELETE FROM notification_outbox WHERE school_id = :s'),
                {'s': self.school_id})
            db.session.execute(text(
                'DELETE FROM push_notifications WHERE school_id = :s AND id > :m'),
                {'s': self.school_id, 'm': self.max_push})
            if self.baseline['notifications'] == 0:
                db.session.execute(text(
                    'DELETE FROM notifications WHERE school_id = :s'),
                    {'s': self.school_id})
            db.session.commit()

    def _measure(self, flag_on):
        from app.blueprints.attendance import _run_auto_absent
        self.app.config['AUTO_ABSENCE_OUTBOX_ENABLED'] = flag_on
        m = {'commits': 0, 'statements': 0, 'firebase_calls': 0}
        open_tx, tx = {}, []

        def _begin(conn):
            open_tx[id(conn)] = _clock.perf_counter()

        def _end(conn):
            started = open_tx.pop(id(conn), None)
            if started is not None:
                tx.append(_clock.perf_counter() - started)

        def _commit(conn):
            m['commits'] += 1
            _end(conn)

        kinds = {}

        def _stmt(_conn, _cursor, statement, *_a, **_k):
            m['statements'] += 1
            head = ' '.join(statement.split()[:3])[:48]
            kinds[head] = kinds.get(head, 0) + 1

        def _push(*_a, **_k):
            m['firebase_calls'] += 1
            return 1, 0

        with self.app.app_context():
            engine = db.engine
            listeners = (('begin', _begin), ('commit', _commit),
                         ('rollback', _end), ('before_cursor_execute', _stmt))
            for name, fn in listeners:
                event.listen(engine, name, fn)
            try:
                with patch('app.services.fcm_service.is_enabled', return_value=True), \
                     patch('app.services.fcm_service.send_push_to_user',
                           side_effect=_push):
                    school = db.session.get(School, self.school_id,
                                            execution_options=OPTS)
                    year = db.session.get(AcademicYear, self.year_id,
                                          execution_options=OPTS)
                    started = _clock.perf_counter()
                    result = _run_auto_absent(school, year, school,
                                              target_date=self.day)
                    m['wall_seconds'] = round(_clock.perf_counter() - started, 3)
            finally:
                for name, fn in listeners:
                    event.remove(engine, name, fn)
            m['newly_absent'] = result['count']
            m['outbox_jobs'] = db.session.execute(text(
                'SELECT count(*) FROM notification_outbox WHERE school_id = :s'),
                {'s': self.school_id}).scalar()
            m['feed_rows'] = db.session.execute(text(
                'SELECT count(*) FROM notifications WHERE school_id = :s'),
                {'s': self.school_id}).scalar() - self.baseline['notifications']
            # One job per (new absence × linked parent × active same-school token).
            m['expected_jobs'] = db.session.execute(text(
                'SELECT count(*) FROM student_attendance sa '
                'JOIN parent_students ps ON ps.student_id = sa.student_id '
                'JOIN mobile_device_tokens t ON t.user_id = ps.user_id '
                ' AND t.school_id = :s AND t.is_active '
                "WHERE sa.school_id = :s AND sa.date = :d AND sa.source = 'automatic'"),
                {'s': self.school_id, 'd': self.day}).scalar()
        m['longest_transaction_seconds'] = round(max(tx), 3) if tx else None
        m['top_statements'] = sorted(kinds.items(), key=lambda kv: -kv[1])[:6]
        return m

    def test_school_wide_fan_out_off_vs_on(self):
        legacy = self._measure(flag_on=False)
        self._wipe()
        durable = self._measure(flag_on=True)
        print(f'\n[auto-absence fan-out] school_id={self.school_id} '
              f'students_evaluated={self.students} parents={self.parents} '
              f'tokens={len(self.token_ids)} date={self.day}\n'
              f'  flag_off={legacy}\n  flag_on ={durable}')

        self.assertEqual(legacy['newly_absent'], durable['newly_absent'])
        self.assertGreater(durable['newly_absent'], 0)
        self.assertEqual(durable['firebase_calls'], 0)
        self.assertEqual(durable['outbox_jobs'], durable['expected_jobs'])
        self.assertEqual(legacy['outbox_jobs'], 0)
        self.assertEqual(durable['feed_rows'], legacy['feed_rows'])
        self.assertGreaterEqual(legacy['firebase_calls'], self.parents)
        self.assertEqual(durable['commits'], 1)


if __name__ == '__main__':
    unittest.main()

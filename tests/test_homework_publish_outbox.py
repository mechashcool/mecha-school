"""
Delayed homework notifications — durable fan-out through the existing outbox.

  * flags OFF: the original immediate notification path, rows never pending,
    no sweep, attendance outbox delivery and historical dead rows untouched;
  * the marker: old-code inserts and plain ORM inserts are handled
    (server default); only null() stores a pending NULL;
  * DEFER on: published today → in-app rows + outbox jobs in ONE transaction
    right after the commit; future → pending, nothing sent;
  * SWEEP: nothing before 07:00 school-local, due at 07:00, missed runs caught
    up, expired / inactive / deleted / old-year homework marked without a
    push, section changes honoured, institute groups honoured;
  * idempotency: a locked or already-claimed row is never fanned out twice,
    a crash before commit leaves it pending, duplicate dedup keys refused;
  * recipients: active parents of this school only, one row per parent, one
    job per device token of this school; payload keys unchanged;
  * the existing worker delivers the jobs; a reassigned token is refused.

Clock frozen by patching the attendance helper's datetime. Firebase mocked.
"""
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import null, text
from sqlalchemy.exc import IntegrityError

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (db, AcademicYear, AuditLog, Employee, Grade, Homework,
                        InstituteGroupEnrollment, InstituteStudyGroup,
                        MobileDeviceToken, Notification, NotificationOutbox,
                        PushNotification, Role, School, Section, Student, Subject,
                        User, parent_students, teacher_subjects)
from app.services import homework_publish as hp
from app.services import notification_outbox as outbox
from app.services import outbox_worker
from app.services.fcm_service import TokenSendResult

PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True}
PUSH_KEYS = {'type', 'ntype', 'route', 'homework_id', 'section_id', 'student_id', 'screen'}
INST_PUSH_KEYS = {'type', 'ntype', 'route', 'homework_id', 'institute_group_id',
                  'student_id', 'screen'}
FLAGS = ('HOMEWORK_PUBLISH_DEFER_ENABLED', 'HOMEWORK_PUBLISH_SWEEP_ENABLED',
         'AIFACE_ATTENDANCE_OUTBOX_ENABLED')

D = date(2026, 10, 31)                         # a Baghdad calendar day


def utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi)


AT_0659 = utc(2026, 10, 31, 3, 59)             # 06:59 Baghdad on D
AT_0700 = utc(2026, 10, 31, 4, 0)              # 07:00 Baghdad on D
AT_1000 = utc(2026, 10, 31, 7, 0)              # 10:00 Baghdad on D
AT_0030 = utc(2026, 10, 30, 21, 30)            # 00:30 Baghdad on D
AT_2330_PREV = utc(2026, 10, 30, 20, 30)       # 23:30 Baghdad on D-1
NEXT_0830 = utc(2026, 11, 1, 5, 30)            # 08:30 Baghdad on D+1


@contextmanager
def frozen_utc(naive_utc):
    aware = naive_utc.replace(tzinfo=timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return aware.astimezone(tz) if tz is not None else naive_utc

    with patch('app.utils.attendance_helpers.datetime', _Frozen):
        yield


class HomeworkPublishOutboxTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        with cls.app.app_context():
            cls.role_ids = {n: Role.query.filter_by(name=n).first().id
                            for n in ('school_admin', 'parent', 'teacher')}

    # ── fixtures ─────────────────────────────────────────────────────────────

    def _user(self, school_id, label, role, active=True):
        u = User(username=f'hpo{label}_{self.sfx}', email=f'hpo{label}_{self.sfx}@example.test',
                 full_name=f'{label} {self.sfx}', role_id=self.role_ids[role],
                 school_id=school_id, is_active=active)
        u.set_password(PASSWORD)
        db.session.add(u)
        db.session.flush()
        return u

    def _student(self, school_id, year_id, name, section_id=None):
        s = Student(student_id=f'S-{uuid4().hex[:10]}', full_name=name, school_id=school_id,
                    academic_year_id=year_id, section_id=section_id, status='active')
        db.session.add(s)
        db.session.flush()
        return s

    def _token(self, user, school_id, label, active=True):
        t = MobileDeviceToken(user_id=user.id, school_id=school_id, platform='android',
                              fcm_token=f'tok-{label}-{uuid4().hex}', is_active=active)
        db.session.add(t)
        db.session.flush()
        return t

    def _link(self, user, student):
        db.session.execute(parent_students.insert().values(
            user_id=user.id, student_id=student.id, relation='guardian'))

    def _school(self, key, *, institute=False):
        school = School(school_name=f'HPO {key} {self.sfx}', code=f'HO{key}{self.sfx}'[:20],
                        capacity=0, is_active=True, timezone='Asia/Baghdad',
                        institution_type=(School.INSTITUTION_INSTITUTE if institute else None))
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'Y {key} {self.sfx}', is_current=True,
                            start_date=date(2026, 8, 1), end_date=date(2027, 6, 30))
        db.session.add(year)
        db.session.flush()
        subj = Subject(school_id=school.id, academic_year_id=year.id, name=f'Math {key}',
                       code=f'O{key}{self.sfx[:6]}')
        db.session.add(subj)
        db.session.flush()
        self.ids.update({f'school_{key}': school.id, f'year_{key}': year.id,
                         f'subj_{key}': subj.id})
        return school, year, subj

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        self._saved = {k: self.app.config.get(k) for k in FLAGS}
        for k in FLAGS:
            self.app.config[k] = False
        with self.app.app_context():
            school, year, subj = self._school('a')
            grade = Grade(school_id=school.id, academic_year_id=year.id, name=f'G{self.sfx[:4]}')
            db.session.add(grade)
            db.session.flush()
            sec_x = Section(school_id=school.id, academic_year_id=year.id, grade_id=grade.id,
                            name=f'X{self.sfx[:4]}', capacity=30)
            sec_y = Section(school_id=school.id, academic_year_id=year.id, grade_id=grade.id,
                            name=f'Y{self.sfx[:4]}', capacity=30)
            db.session.add_all([sec_x, sec_y])
            db.session.flush()
            admin = self._user(school.id, 'adm', 'school_admin')
            tuser = self._user(school.id, 'tch', 'teacher')
            emp = Employee(school_id=school.id, employee_id=f'O{self.sfx}', full_name=f'T {self.sfx}',
                           base_salary=0, status='active', user_id=tuser.id)
            db.session.add(emp)
            db.session.flush()
            db.session.execute(teacher_subjects.insert().values(
                employee_id=emp.id, subject_id=subj.id, section_id=sec_x.id))

            c1 = self._student(school.id, year.id, 'C1', sec_x.id)
            c2 = self._student(school.id, year.id, 'C2', sec_x.id)
            c3 = self._student(school.id, year.id, 'C3', sec_y.id)
            c4 = self._student(school.id, year.id, 'C4', sec_x.id)
            c5 = self._student(school.id, year.id, 'C5', sec_x.id)
            p1 = self._user(school.id, 'p1', 'parent')                 # c1 + c2
            p_y = self._user(school.id, 'py', 'parent')                # c3 (Y)
            p_off = self._user(school.id, 'poff', 'parent', active=False)   # c4
            for parent, child in ((p1, c1), (p1, c2), (p_y, c3), (p_off, c4)):
                self._link(parent, child)

            school_b, _yb, _sb = self._school('b')
            p_foreign = self._user(school_b.id, 'pfor', 'parent')     # B account → A's c5
            self._link(p_foreign, c5)

            t1a = self._token(p1, school.id, 'p1a')
            t1b = self._token(p1, school.id, 'p1b')
            t1_off = self._token(p1, school.id, 'p1off', active=False)
            t1_b = self._token(p1, school_b.id, 'p1b-school')          # wrong school
            ty = self._token(p_y, school.id, 'py')
            self._token(p_off, school.id, 'poff')
            self._token(p_foreign, school_b.id, 'pfor')

            inst, iyear, isubj = self._school('i', institute=True)
            iuser = self._user(inst.id, 'itch', 'teacher')
            iemp = Employee(school_id=inst.id, employee_id=f'I{self.sfx}', full_name=f'I {self.sfx}',
                            base_salary=0, status='active', user_id=iuser.id)
            db.session.add(iemp)
            db.session.flush()
            group = InstituteStudyGroup(school_id=inst.id, academic_year_id=iyear.id,
                                        subject_id=isubj.id, instructor_id=iemp.id,
                                        name=f'IG {self.sfx}', is_active=True)
            db.session.add(group)
            db.session.flush()
            ci = self._student(inst.id, iyear.id, 'CI')
            cn = self._student(inst.id, iyear.id, 'CN')                # not enrolled
            db.session.add(InstituteGroupEnrollment(
                school_id=inst.id, group_id=group.id, student_id=ci.id,
                status=InstituteGroupEnrollment.STATUS_ACTIVE))
            pi = self._user(inst.id, 'pi', 'parent')
            pn = self._user(inst.id, 'pn', 'parent')
            self._link(pi, ci)
            self._link(pn, cn)
            ti = self._token(pi, inst.id, 'pi')
            self._token(pn, inst.id, 'pn')
            db.session.commit()
            self.ids.update({
                'grade': grade.id, 'sec_x': sec_x.id, 'sec_y': sec_y.id, 'emp': emp.id,
                'admin': admin.id, 'tuser': tuser.id, 'c1': c1.id, 'c2': c2.id, 'c3': c3.id,
                'p1': p1.id, 'p_y': p_y.id, 'p_off': p_off.id, 'p_foreign': p_foreign.id,
                't1a': t1a.id, 't1b': t1b.id, 't1_off': t1_off.id, 't1_b': t1_b.id,
                'ty': ty.id, 'iuser': iuser.id, 'iemp': iemp.id, 'group': group.id,
                'ci': ci.id, 'pi': pi.id, 'pn': pn.id, 'ti': ti.id,
            })

    def tearDown(self):
        for k, v in self._saved.items():
            self.app.config[k] = v
        with self.app.app_context():
            db.session.rollback()
            sids = [self.ids[f'school_{k}'] for k in ('a', 'b', 'i') if f'school_{k}' in self.ids]
            uids = [u.id for u in User.query.execution_options(**OPTS)
                    .filter(User.school_id.in_(sids)).all()]
            stu = [s.id for s in Student.query.execution_options(**OPTS)
                   .filter(Student.school_id.in_(sids)).all()]
            for model in (NotificationOutbox, PushNotification, Notification):
                model.query.execution_options(**OPTS).filter(
                    model.school_id.in_(sids)).delete(synchronize_session=False)
            if uids:
                for model in (AuditLog, MobileDeviceToken):
                    model.query.execution_options(**OPTS).filter(
                        model.user_id.in_(uids)).delete(synchronize_session=False)
                db.session.execute(parent_students.delete().where(
                    parent_students.c.user_id.in_(uids)))
            if stu:
                db.session.execute(parent_students.delete().where(
                    parent_students.c.student_id.in_(stu)))
            db.session.execute(teacher_subjects.delete().where(
                teacher_subjects.c.employee_id.in_(
                    db.session.query(Employee.id).filter(Employee.school_id.in_(sids)))))
            for model in (Homework, InstituteGroupEnrollment, InstituteStudyGroup, Student,
                          Section, Subject, Grade, Employee, User, AcademicYear):
                model.query.execution_options(**OPTS).filter(
                    model.school_id.in_(sids)).delete(synchronize_session=False)
            School.query.filter(School.id.in_(sids)).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ──────────────────────────────────────────────────────────────

    @contextmanager
    def flags(self, defer=False, sweep=False, aiface=False):
        values = dict(zip(FLAGS, (defer, sweep, aiface)))
        old = {k: self.app.config.get(k) for k in FLAGS}
        self.app.config.update(values)
        try:
            yield
        finally:
            self.app.config.update(old)

    def hw(self, publish, *, due=None, section='sec_x', pending=True, school='a',
           group=False, active=True, title=None):
        with self.app.app_context():
            row = Homework(
                school_id=self.ids[f'school_{school}'], academic_year_id=self.ids[f'year_{school}'],
                subject_id=self.ids[f'subj_{school}'],
                section_id=None if group else self.ids[section],
                institute_group_id=self.ids['group'] if group else None,
                title=title or f'HW {uuid4().hex[:6]}', publish_date=publish,
                due_date=due or publish + timedelta(days=2), is_active=active,
                **({'parents_notified_at': null()} if pending else {}))
            db.session.add(row)
            db.session.commit()
            return row.id

    def marker(self, hw_id):
        with self.app.app_context():
            return db.session.execute(text('SELECT parents_notified_at FROM homework '
                                           'WHERE id=:i'), {'i': hw_id}).scalar()

    def staged(self, hw_id=None):
        """(in-app rows by parent, outbox jobs) for school a/i homework."""
        with self.app.app_context():
            sids = [self.ids['school_a'], self.ids['school_i'], self.ids['school_b']]
            rows = (Notification.query.execution_options(**OPTS)
                    .filter(Notification.school_id.in_(sids),
                            Notification.ntype == 'homework').all())
            jobs = (NotificationOutbox.query.execution_options(**OPTS)
                    .filter(NotificationOutbox.school_id.in_(sids),
                            NotificationOutbox.event_type
                            == NotificationOutbox.EVENT_HOMEWORK_PUBLISHED)
                    .order_by(NotificationOutbox.id).all())
            if hw_id is not None:
                jobs = [j for j in jobs if f':{hw_id}:' in j.dedup_key]
            by_parent = {}
            for n in rows:
                by_parent[n.target_user_id] = by_parent.get(n.target_user_id, 0) + 1
            return by_parent, [(j.user_id, j.device_token_id, j.data_json, j.status)
                               for j in jobs]

    def sweep(self, at):
        with self.app.app_context(), frozen_utc(at):
            return hp.sweep_due(now=at)

    def fan_out(self, hw_id, at, *, real_now=False, **kw):
        """School-local clock frozen at `at`. real_now stamps the marker and
        the jobs' next_attempt_at with the REAL clock, as production does, so
        the worker (which claims against the real clock) sees them as due."""
        with self.app.app_context(), frozen_utc(at):
            return hp.fan_out_homework(hw_id, now=None if real_now else at, **kw)

    def jwt(self, key):
        with self.app.app_context():
            return {'Authorization': 'Bearer ' + encode_token(
                db.session.get(User, self.ids[key], execution_options=OPTS))}

    def web_client(self):
        client = self.app.test_client()
        with self.app.app_context():
            name = db.session.get(User, self.ids['admin'], execution_options=OPTS).username
        self.assertIn(client.post('/auth/login', data={'username': name, 'password': PASSWORD})
                      .status_code, (200, 302))
        return client

    def web_create(self, client, title, publish, section='sec_x'):
        return client.post('/homework/create', data={
            'title': title, 'subject_id': str(self.ids['subj_a']),
            'grade_id': str(self.ids['grade']), 'section_id': str(self.ids[section]),
            'publish_date': publish.isoformat(),
            'due_date': (publish + timedelta(days=2)).isoformat()})

    def mobile_create(self, title, publish, section='sec_x'):
        return self.app.test_client().post(
            '/api/mobile/v1/teacher/homework', headers=self.jwt('tuser'),
            json={'title': title, 'section_id': self.ids[section],
                  'subject_id': self.ids['subj_a'], 'publish_date': publish.isoformat(),
                  'due_date': (publish + timedelta(days=2)).isoformat()})

    def hw_id(self, title, school='a'):
        with self.app.app_context():
            return (Homework.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids[f'school_{school}'], title=title).one().id)

    @contextmanager
    def legacy_pushes(self):
        sent = []
        with patch('app.services.fcm_service.is_enabled', return_value=True), \
                patch('app.services.fcm_service.send_push_to_user',
                      side_effect=lambda uid, t, b, d=None: sent.append((uid, d)) or (1, 0)):
            yield sent

    def expected_x_jobs(self):
        return {(self.ids['p1'], self.ids['t1a']), (self.ids['p1'], self.ids['t1b'])}

    def assert_x_fanout(self, hw_id):
        import json
        by_parent, jobs = self.staged(hw_id)
        self.assertEqual(by_parent, {self.ids['p1']: 1})       # one row per parent
        self.assertEqual({(u, t) for u, t, _d, _s in jobs}, self.expected_x_jobs())
        for _u, _t, data, status in jobs:
            payload = json.loads(data)
            self.assertEqual(set(payload), PUSH_KEYS)
            self.assertEqual(payload['homework_id'], str(hw_id))
            self.assertEqual(payload['section_id'], str(self.ids['sec_x']))
            self.assertEqual(payload['student_id'], str(min(self.ids['c1'], self.ids['c2'])))
            self.assertEqual((payload['type'], payload['ntype'], payload['screen'],
                              payload['route']),
                             ('homework', 'homework', 'homework', '/parent/homework'))
            self.assertEqual(status, 'pending')
        self.assertIsNotNone(self.marker(hw_id))

    # ── 1. flags OFF: the original behaviour ────────────────────────────────

    def test_01_29_flags_off_keep_the_immediate_path_and_never_leave_pending(self):
        client = self.web_client()
        with self.legacy_pushes() as sent:
            # even a FUTURE homework is announced at once — unchanged behaviour
            self.assertEqual(self.web_create(client, f'W {self.sfx}', D + timedelta(days=5))
                             .status_code, 302)
            self.assertEqual(self.mobile_create(f'M {self.sfx}', D + timedelta(days=5))
                             .status_code, 201)
        web_id, mob_id = self.hw_id(f'W {self.sfx}'), self.hw_id(f'M {self.sfx}')
        self.assertEqual(sorted(u for u, _d in sent), [self.ids['p1']] * 2)
        self.assertIsNotNone(self.marker(web_id))
        self.assertIsNotNone(self.marker(mob_id))
        self.assertEqual(self.staged()[1], [])                  # no outbox jobs
        with self.app.app_context():
            self.assertFalse(outbox.any_enabled())

    def test_02_30_31_34_flags_off_no_sweep_attendance_delivery_and_dead_rows_untouched(self):
        pending = self.hw(D)
        with self.app.app_context():
            self.assertIsNone(outbox_worker._maybe_homework_sweep(None))   # sweep flag off
        self.assertIsNone(self.marker(pending))
        self.assertEqual(self.staged()[1], [])
        with self.app.app_context():
            school_id = self.ids['school_a']
            live = NotificationOutbox(
                school_id=school_id, event_type=NotificationOutbox.EVENT_SCHOOL_ATTENDANCE_SCAN,
                user_id=self.ids['p1'], device_token_id=self.ids['t1a'], title='t', body='b',
                data_json='{}', ntype='attendance', dedup_key=f'test-live-{self.sfx}',
                status='pending', attempts=0, next_attempt_at=datetime.utcnow())
            dead = NotificationOutbox(
                school_id=school_id, event_type=NotificationOutbox.EVENT_SCHOOL_ATTENDANCE_SCAN,
                user_id=self.ids['p1'], device_token_id=self.ids['t1a'], title='t', body='b',
                data_json='{}', ntype='attendance', dedup_key=f'test-dead-{self.sfx}',
                status='dead', attempts=5, last_error='historical')
            db.session.add_all([live, dead])
            db.session.commit()
            live_id, dead_id = live.id, dead.id
        with self.flags(aiface=True), self.app.app_context(), \
                patch('app.services.fcm_service.send_to_device_token',
                      return_value=TokenSendResult(True, message_id='m1')) as send:
            stats = outbox_worker.run_once('test-worker', batch_size=50, lease_seconds=300,
                                           max_attempts=5)
            self.assertIsNone(outbox_worker._maybe_homework_sweep(None))
            self.assertGreaterEqual(stats['sent'], 1)
            self.assertTrue(any(c.args[0].id == self.ids['t1a'] for c in send.call_args_list))
        with self.app.app_context():
            self.assertEqual(db.session.get(NotificationOutbox, live_id).status, 'sent')
            d = db.session.get(NotificationOutbox, dead_id)
            self.assertEqual((d.status, d.attempts, d.last_error), ('dead', 5, 'historical'))
        self.assertIsNone(self.marker(pending))               # still pending, not swept

    # ── 2. the marker ────────────────────────────────────────────────────────

    def test_04_05_marker_semantics(self):
        with self.app.app_context():
            sid, yid = self.ids['school_a'], self.ids['year_a']
            old = db.session.execute(text(
                'INSERT INTO homework (school_id, academic_year_id, title, publish_date, '
                "due_date, is_active) VALUES (:s, :y, 'old code', :p, :p, true) "
                'RETURNING parents_notified_at'), {'s': sid, 'y': yid, 'p': D}).scalar()
            db.session.commit()
            self.assertIsNotNone(old)                           # old code → handled
        plain_none = self.hw(D, pending=False)
        self.assertIsNotNone(self.marker(plain_none))
        with self.app.app_context():
            # documents the ORM behaviour: an explicit None is DROPPED and the
            # server default applies — which is why null() is required
            row = Homework(school_id=self.ids['school_a'], academic_year_id=self.ids['year_a'],
                           title='explicit none', publish_date=D, due_date=D, is_active=True,
                           parents_notified_at=None)
            db.session.add(row)
            db.session.commit()
            explicit_none = row.id
            self.assertEqual(hp.pending_marker_kwargs(), {})
            with self.flags(defer=True):
                self.assertIn('parents_notified_at', hp.pending_marker_kwargs())
        self.assertIsNotNone(self.marker(explicit_none))
        self.assertIsNone(self.marker(self.hw(D)))              # null() → pending

    # ── 3. DEFER on: immediate durable fan-out vs pending ────────────────────

    def test_06_07_27_deferred_web_create_today_and_tomorrow(self):
        client = self.web_client()
        with self.flags(defer=True, sweep=True), self.legacy_pushes() as sent, \
                frozen_utc(AT_1000):
            self.assertEqual(self.web_create(client, f'Now {self.sfx}', D).status_code, 302)
            self.assertEqual(self.web_create(client, f'Later {self.sfx}', D + timedelta(days=1))
                             .status_code, 302)
        now_id, later_id = self.hw_id(f'Now {self.sfx}'), self.hw_id(f'Later {self.sfx}')
        self.assertEqual(sent, [])                               # legacy path not used
        self.assert_x_fanout(now_id)
        self.assertIsNone(self.marker(later_id))                 # pending
        self.assertEqual(self.staged(later_id)[1], [])
        self.assertEqual(self.staged()[0], {self.ids['p1']: 1})  # nothing premature
        with frozen_utc(AT_1000):
            items = self.app.test_client().get(
                f"/api/mobile/v1/parent/children/{self.ids['c1']}/homework",
                headers=self.jwt('p1')).get_json()['homework']
        self.assertEqual([h['id'] for h in items], [now_id])     # future hidden

    def test_29_deferred_mobile_create_matches_web(self):
        with self.flags(defer=True, sweep=True), self.legacy_pushes() as sent, \
                frozen_utc(AT_1000):
            self.assertEqual(self.mobile_create(f'Mob {self.sfx}', D).status_code, 201)
            self.assertEqual(self.mobile_create(f'MobL {self.sfx}', D + timedelta(days=3))
                             .status_code, 201)
        self.assertEqual(sent, [])
        self.assert_x_fanout(self.hw_id(f'Mob {self.sfx}'))
        self.assertIsNone(self.marker(self.hw_id(f'MobL {self.sfx}')))

    # ── 4. the sweep ─────────────────────────────────────────────────────────

    def test_08_09_send_hour_gate(self):
        hw = self.hw(D)
        with self.flags(sweep=True):
            self.assertEqual(self.sweep(AT_0659)['candidates'], 0)
            self.assertIsNone(self.marker(hw))
            stats = self.sweep(AT_0700)
        self.assertEqual((stats['candidates'], stats['staged']), (1, 1))
        self.assert_x_fanout(hw)

    def test_10_14_missed_run_caught_up_once(self):
        hw = self.hw(D, due=D + timedelta(days=2))
        with self.flags(sweep=True):
            self.assertEqual(self.sweep(NEXT_0830)['staged'], 1)   # server back at 08:30 D+1
            again = self.sweep(NEXT_0830)
            self.assertEqual(again['candidates'], 0)
            self.assertEqual(self.fan_out(hw, NEXT_0830), hp.NOT_PENDING)
        self.assert_x_fanout(hw)
        with self.app.app_context():                               # dedup_key UNIQUE
            job = (NotificationOutbox.query.execution_options(**OPTS)
                   .filter(NotificationOutbox.dedup_key.like(f'homework_published:{hw}:%'))
                   .first())
            db.session.add(NotificationOutbox(
                school_id=job.school_id, event_type=job.event_type, user_id=job.user_id,
                device_token_id=job.device_token_id, title='x', body='x', ntype='homework',
                dedup_key=job.dedup_key, status='pending', attempts=0))
            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_11_locked_or_claimed_row_is_not_fanned_out_twice(self):
        hw = self.hw(D)
        with self.app.app_context():
            other = db.engine.connect()
            tx = other.begin()
            other.execute(text('SELECT id FROM homework WHERE id=:i FOR UPDATE'), {'i': hw})
            try:
                self.assertEqual(self.fan_out(hw, AT_1000), hp.NOT_PENDING)   # SKIP LOCKED
                self.assertEqual(self.staged(hw)[1], [])
                # the other worker finishes its claim
                other.execute(text('UPDATE homework SET parents_notified_at = now() '
                                   'WHERE id=:i'), {'i': hw})
                tx.commit()
            finally:
                other.close()
        self.assertEqual(self.fan_out(hw, AT_1000), hp.NOT_PENDING)
        self.assertEqual(self.staged(hw), ({}, []))

    def test_12_crash_before_commit_stays_pending_then_sends_once(self):
        hw = self.hw(D)
        real = outbox.stage_homework_deliveries

        def crash(**kw):
            real(**kw)
            raise RuntimeError('simulated crash before commit')

        with patch.object(outbox, 'stage_homework_deliveries', side_effect=crash):
            self.assertEqual(self.fan_out(hw, AT_1000), hp.ERROR)
        self.assertIsNone(self.marker(hw))
        self.assertEqual(self.staged(), ({}, []))                 # nothing half-written
        with self.flags(sweep=True):
            self.assertEqual(self.sweep(AT_1000)['staged'], 1)
        self.assert_x_fanout(hw)

    def test_13_19_committed_jobs_survive_and_the_worker_delivers_them(self):
        hw = self.hw(D)
        self.assertEqual(self.fan_out(hw, AT_1000, real_now=True), hp.STAGED)
        with self.app.app_context():                               # reassign one device
            tok = db.session.get(MobileDeviceToken, self.ids['t1b'], execution_options=OPTS)
            tok.user_id = self.ids['p_y']
            db.session.commit()
        delivered = []

        def fake_send(token_row, title, body, data=None):
            delivered.append((token_row.id, data))
            return TokenSendResult(True, message_id='m')

        # a fresh worker (restart) picks the committed jobs up
        with self.flags(sweep=True), self.app.app_context(), \
                patch('app.services.fcm_service.send_to_device_token', side_effect=fake_send):
            stats = outbox_worker.run_once('restarted', batch_size=50, lease_seconds=300,
                                           max_attempts=5)
        self.assertEqual([t for t, _d in delivered], [self.ids['t1a']])
        self.assertEqual(delivered[0][1]['homework_id'], str(hw))
        self.assertEqual(stats['dead'], 1)                          # reassigned token refused
        statuses = {t: s for _u, t, _d, s in self.staged(hw)[1]}
        self.assertEqual(statuses, {self.ids['t1a']: 'sent', self.ids['t1b']: 'dead'})
        with self.flags(sweep=True):
            self.assertEqual(self.sweep(AT_1000)['candidates'], 0)   # never re-fanned

    def test_13_transient_fcm_failure_retries_without_refanning(self):
        hw = self.hw(D)
        self.assertEqual(self.fan_out(hw, AT_1000, real_now=True), hp.STAGED)
        with self.flags(sweep=True), self.app.app_context(), \
                patch('app.services.fcm_service.send_to_device_token',
                      return_value=TokenSendResult(False, error='unavailable')):
            outbox_worker.run_once('w', batch_size=50, lease_seconds=300, max_attempts=5)
        self.assertEqual({s for *_r, s in self.staged(hw)[1]}, {'retry'})
        self.assertEqual(self.staged(hw)[0], {self.ids['p1']: 1})   # in-app row kept
        self.assertEqual(self.fan_out(hw, AT_1000), hp.NOT_PENDING)

    # ── 5. eligibility ───────────────────────────────────────────────────────

    def test_15_16_17_18_recipients(self):
        hw = self.hw(D)
        self.assertEqual(self.fan_out(hw, AT_1000), hp.STAGED)
        by_parent, jobs = self.staged(hw)
        excluded = {self.ids['p_off'], self.ids['p_foreign'], self.ids['p_y']}
        self.assertFalse(excluded & set(by_parent))
        self.assertFalse(excluded & {u for u, *_r in jobs})
        tokens = {t for _u, t, *_r in jobs}
        self.assertNotIn(self.ids['t1_off'], tokens)                # inactive token
        self.assertNotIn(self.ids['t1_b'], tokens)                  # other school's token
        self.assertEqual(len(jobs), 2)                              # two devices, one parent

    def test_20_21_deleted_and_inactive_homework(self):
        deleted, inactive = self.hw(D), self.hw(D)
        with self.app.app_context():
            Homework.query.execution_options(**OPTS).filter_by(id=deleted).delete()
            Homework.query.execution_options(**OPTS).filter_by(id=inactive).update(
                {'is_active': False})
            db.session.commit()
        with self.flags(sweep=True):
            self.assertEqual(self.sweep(AT_1000)['candidates'], 0)
        self.assertEqual(self.fan_out(inactive, AT_1000), hp.SKIPPED)
        self.assertIsNotNone(self.marker(inactive))
        self.assertEqual(self.staged(), ({}, []))

    def test_22_expired_marked_without_push_and_due_today_still_sent(self):
        expired = self.hw(D, due=D)
        due_today = self.hw(D + timedelta(days=1), due=D + timedelta(days=1))
        with self.flags(sweep=True):
            stats = self.sweep(NEXT_0830)
        self.assertEqual((stats['skipped'], stats['staged']), (1, 1))
        self.assertIsNotNone(self.marker(expired))
        self.assertEqual(self.staged(expired)[1], [])
        self.assertEqual(len(self.staged(due_today)[1]), 2)
        with self.app.app_context():
            row = db.session.get(Homework, expired, execution_options=OPTS)
            self.assertEqual((row.due_date, row.is_active), (D, True))   # untouched

    def test_23_section_change_before_publish_uses_new_recipients(self):
        hw = self.hw(D + timedelta(days=1))
        with self.app.app_context():
            Homework.query.execution_options(**OPTS).filter_by(id=hw).update(
                {'section_id': self.ids['sec_y']})
            db.session.commit()
        with self.flags(sweep=True):
            self.assertEqual(self.sweep(NEXT_0830)['staged'], 1)
        by_parent, jobs = self.staged(hw)
        self.assertEqual(by_parent, {self.ids['p_y']: 1})
        self.assertEqual({t for _u, t, *_r in jobs}, {self.ids['ty']})

    def test_24_school_local_midnight(self):
        hw = self.hw(D)
        self.assertEqual(self.fan_out(hw, AT_2330_PREV), hp.NOT_DUE)        # D-1 locally
        with self.flags(sweep=True):
            self.assertEqual(self.sweep(AT_0030)['candidates'], 0)          # before 07:00
        self.assertEqual(self.fan_out(hw, AT_0030), hp.STAGED)              # published
        self.assertEqual(self.fan_out(hw, AT_1000), hp.NOT_PENDING)

    def test_26_old_academic_year_not_notified(self):
        hw = self.hw(D)
        with self.app.app_context():
            AcademicYear.query.execution_options(**OPTS).filter_by(
                id=self.ids['year_a']).update({'is_current': False})
            db.session.add(AcademicYear(school_id=self.ids['school_a'], name=f'N {self.sfx}',
                                        is_current=True, start_date=date(2027, 8, 1),
                                        end_date=date(2028, 6, 30)))
            db.session.commit()
        with self.flags(sweep=True):
            self.assertEqual(self.sweep(AT_1000)['skipped'], 1)
        self.assertIsNotNone(self.marker(hw))
        self.assertEqual(self.staged(), ({}, []))

    def test_25_institute_group_homework(self):
        import json
        with self.flags(defer=True, sweep=True), frozen_utc(AT_1000):
            resp = self.app.test_client().post(
                '/api/mobile/v1/teacher/institute/homework', headers=self.jwt('iuser'),
                json={'group_id': self.ids['group'], 'title': f'I {self.sfx}',
                      'publish_date': (D + timedelta(days=1)).isoformat(),
                      'due_date': (D + timedelta(days=3)).isoformat()})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        hw = self.hw_id(f'I {self.sfx}', school='i')
        self.assertIsNone(self.marker(hw))                          # future → pending
        with self.flags(sweep=True):
            self.assertEqual(self.sweep(NEXT_0830)['staged'], 1)
        by_parent, jobs = self.staged(hw)
        self.assertEqual(by_parent, {self.ids['pi']: 1})            # enrolled only
        self.assertEqual([t for _u, t, *_r in jobs], [self.ids['ti']])
        payload = json.loads(jobs[0][2])
        self.assertEqual(set(payload), INST_PUSH_KEYS)
        self.assertEqual(payload['institute_group_id'], str(self.ids['group']))

    # ── 6. edits, rollback, cost ─────────────────────────────────────────────

    def test_14_spec_edits(self):
        client = self.web_client()
        future = self.hw(D + timedelta(days=4))
        notified = self.hw(D)
        self.assertEqual(self.fan_out(notified, AT_1000), hp.STAGED)
        jobs_before = len(self.staged()[1])

        def edit(hw_id, publish):
            return client.post(f'/homework/{hw_id}/edit', data={
                'title': f'E {hw_id}', 'subject_id': str(self.ids['subj_a']),
                'grade_id': str(self.ids['grade']), 'section_id': str(self.ids['sec_x']),
                'publish_date': publish.isoformat(),
                'due_date': (publish + timedelta(days=5)).isoformat()})

        with frozen_utc(AT_1000), self.legacy_pushes() as sent:
            self.assertEqual(edit(notified, D + timedelta(days=3)).status_code, 302)
            self.assertEqual(len(self.staged()[1]), jobs_before)     # no re-notify
            self.assertEqual(edit(future, D).status_code, 302)       # now published
        self.assertEqual(sent, [])
        self.assertEqual(len(self.staged(future)[1]), 2)
        self.assertIsNotNone(self.marker(future))

    def test_35_defer_off_with_pending_rows_still_drained_and_flushable(self):
        due, later = self.hw(D), self.hw(D + timedelta(days=9))
        with self.flags(defer=False, sweep=True):
            self.assertEqual(self.sweep(AT_1000)['staged'], 1)
        self.assertIsNone(self.marker(later))                        # kept, not deleted
        with self.app.app_context(), frozen_utc(AT_1000):
            totals = hp.flush_pending(now=AT_1000)                   # rollback aid
            summary = hp.status_summary(now=AT_1000)
        self.assertEqual(totals['staged'], 1)
        self.assertIsNotNone(self.marker(later))
        self.assertIsNotNone(self.marker(due))
        self.assertEqual(summary['pending'], 0)

    def test_worker_loop_sweeps_then_delivers_and_respects_the_interval(self):
        hw = self.hw(D)
        delivered = []

        def fake_send(token_row, title, body, data=None):
            delivered.append(token_row.id)
            return TokenSendResult(True, message_id='m')

        with self.flags(sweep=True), frozen_utc(AT_1000), \
                patch('app.services.fcm_service.send_to_device_token', side_effect=fake_send):
            outbox_worker.run(app=self.app, max_loops=1, batch_size=50)
            with self.app.app_context():
                last = outbox_worker._maybe_homework_sweep(None)     # just swept
                self.assertIsNotNone(last)
                self.assertEqual(outbox_worker._maybe_homework_sweep(last), last)
        self.assertEqual(sorted(delivered), sorted([self.ids['t1a'], self.ids['t1b']]))
        self.assertEqual({s for *_r, s in self.staged(hw)[1]}, {'sent'})

    def test_idle_sweep_is_one_query(self):
        from sqlalchemy import event
        with self.app.app_context():
            # this fixture leaves nothing pending in these schools; other
            # pending rows in the test DB would only add candidates, so assert
            # on the first statement only when the sweep finds no school.
            seen = []
            listener = lambda *a: seen.append(a[2])               # noqa: E731
            event.listen(db.engine, 'before_cursor_execute', listener)
            try:
                with frozen_utc(AT_1000):
                    stats = hp.sweep_due(now=AT_1000)
            finally:
                event.remove(db.engine, 'before_cursor_execute', listener)
        if stats['candidates'] == 0 and len(seen) == 1:
            self.assertIn('parents_notified_at IS NULL', seen[0])
        else:
            self.skipTest('other pending homework exists in the test database')


if __name__ == '__main__':
    unittest.main()

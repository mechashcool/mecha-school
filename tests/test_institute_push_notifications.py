"""Institute homework push + "أرسل درسي" viewed push — focused guarantees.

Homework (institute):
  * web instructor, web administration and mobile instructor creation all end
    in ONE batched parent flow (_notify_parents_batched): in-app rows + ONE
    queued send_push_batch, never a synchronous per-parent FCM call;
  * recipients = linked, active, same-school parents of the group's active
    enrollments, once each; a corrupt cross-school link is skipped;
  * payload carries homework_id / institute_group_id / student_id, no
    section_id; dedup is scoped to the homework event, not to its text;
  * normal-school homework keeps its synchronous section flow unchanged.

Study request viewed:
  * only the FIRST pending → viewed transition notifies, only the submitting
    parent, in the SAME transaction as the update (rollback = nothing);
  * in-app row always; outbox rows only with STUDY_REQUEST_OUTBOX_ENABLED, one
    per ACTIVE token of that parent in THIS school;
  * later saves (unchanged or note edits), lost races, other schools and
    ineligible parents stage nothing; the admin note never reaches the push.

No Firebase call is made: async dispatch and FCM entry points are mocked, and
the outbox is only written, never delivered.
"""
import json
import threading
import types
import unittest
from datetime import date, datetime, timedelta
from unittest import mock
from uuid import uuid4

from flask_login import login_user, logout_user
from sqlalchemy import event
from sqlalchemy.orm import Session

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (
    db, AcademicYear, Employee, Grade, Homework, InstituteGroupEnrollment,
    InstituteStudyGroup, InstituteStudyRequest, MobileDeviceToken, Notification,
    NotificationOutbox, Role, School, Section, Student, Subject, User,
    parent_students, teacher_subjects,
)
from app.services import fcm_service
from app.services import notification_outbox as outbox
from app.utils.school_cleanup import cleanup_school_cascade

OPTS = {'bypass_tenant_scope': True, 'include_all_years': True}
PASSWORD = 'Password123!'
LIST = '/admin/institute-study-requests'
MOBILE_HW = '/api/mobile/v1/teacher/institute/homework'
SAVE_LOOKUP = 'app.blueprints.admin.institute_study_requests._save_lookup'


class InstitutePushNotificationsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config.update(RATELIMIT_ENABLED=False, WTF_CSRF_ENABLED=False,
                              PRIVATE_UPLOADS_ENABLED=False)
        with cls.app.app_context():
            cls.roles = {r.name: r.id for r in Role.query.all()}

    # ── fixture ───────────────────────────────────────────────────────────────

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        self.app.config['STUDY_REQUEST_OUTBOX_ENABLED'] = False
        with self.app.app_context():
            a = self._school('a', institute=True)
            b = self._school('b', institute=True)
            c = self._school('c', institute=False)

            # Institute A: one instructor, one administrator (two, for races).
            for tag in ('adm', 'adm2'):
                self._user(tag, 'school_admin', a)
            ta = self._user('ta', 'teacher', a)
            emp = self._employee(a, 'ta', ta)
            subj = self._subject(a, 'sa')
            ga = InstituteStudyGroup(school_id=a, academic_year_id=self.ids['year_a'],
                                     subject_id=subj, instructor_id=emp,
                                     name=f'GA {self.sfx}', is_active=True)
            db.session.add(ga)
            db.session.flush()
            self.ids['ga'] = ga.id

            for key in ('s1', 's2', 's_out'):
                self._student(key, a)
            for key in ('s1', 's2'):
                db.session.add(InstituteGroupEnrollment(
                    school_id=a, group_id=ga.id, student_id=self.ids[key],
                    status=InstituteGroupEnrollment.STATUS_ACTIVE))

            # p1 → s1, p2 → s2, pmix → s1+s2 (one notification), p_out → only
            # a non-member, p_inactive → s1 but disabled, p_foreign → s1 but
            # its own account is in institute B (corrupt link).
            for tag, kids, school, active in (
                    ('p1', ('s1',), a, True), ('p2', ('s2',), a, True),
                    ('pmix', ('s1', 's2'), a, True), ('p_out', ('s_out',), a, True),
                    ('p_inactive', ('s1',), a, False), ('p_foreign', ('s1',), b, True)):
                self._user(tag, 'parent', school, active=active)
                for kid in kids:
                    self._link(tag, kid)

            # Device tokens of p1: two active here, one inactive, and one active
            # but registered in another institute (must never be targeted).
            for tag, school, active in (('t1', a, True), ('t2', a, True),
                                        ('t3', a, False), ('t4', b, True)):
                tok = MobileDeviceToken(user_id=self.ids['p1'], school_id=school,
                                        fcm_token=f'tok-{tag}-{uuid4().hex}',
                                        platform='android', is_active=active)
                db.session.add(tok)
                db.session.flush()
                self.ids[tag] = tok.id

            # Institute B: its own administrator, child, parent and request.
            self._user('adm_b', 'school_admin', b)
            self._student('sb', b)
            self._user('pb', 'parent', b)
            self._link('pb', 'sb')

            # Normal school C: section homework baseline.
            grade = Grade(school_id=c, academic_year_id=self.ids['year_c'],
                          name=f'G{self.sfx[:4]}')
            db.session.add(grade)
            db.session.flush()
            sec = Section(school_id=c, academic_year_id=self.ids['year_c'],
                          grade_id=grade.id, name=f'A{self.sfx[:4]}', capacity=30)
            db.session.add(sec)
            db.session.flush()
            tc = self._user('tc', 'teacher', c)
            emp_c = self._employee(c, 'tc', tc)
            subj_c = self._subject(c, 'sc')
            db.session.execute(Section.__table__.update()
                               .where(Section.id == sec.id).values(teacher_id=emp_c))
            db.session.execute(teacher_subjects.insert().values(
                employee_id=emp_c, subject_id=subj_c, section_id=sec.id))
            self._student('s_sch', c, section_id=sec.id)
            self._user('p_sch', 'parent', c)
            self._link('p_sch', 's_sch')
            self.ids.update(grade_c=grade.id, sec_c=sec.id)
            db.session.commit()

    def _school(self, key, *, institute):
        school = School(school_name=f'IPN {key} {self.sfx}', code=f'IPN{key}{self.sfx}'[:20],
                        capacity=0, is_active=True,
                        institution_type='institute' if institute else 'school')
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'Y{key}{self.sfx}', is_current=True,
                            start_date=date.today() - timedelta(days=60),
                            end_date=date.today() + timedelta(days=300))
        db.session.add(year)
        db.session.flush()
        self.ids.update({f'school_{key}': school.id, f'year_{key}': year.id})
        return school.id

    def _year_of(self, school_id):
        key = next(k[len('school_'):] for k, v in self.ids.items()
                   if k.startswith('school_') and v == school_id)
        return self.ids[f'year_{key}']

    def _user(self, tag, role, school_id, active=True):
        u = User(username=f'ipn{tag}_{self.sfx}', email=f'ipn{tag}_{self.sfx}@t.test',
                 full_name=f'ipn {tag}', role_id=self.roles[role],
                 school_id=school_id, is_active=active)
        u.set_password(PASSWORD)
        db.session.add(u)
        db.session.flush()
        self.ids[tag] = u.id
        return u.id

    def _employee(self, school_id, tag, user_id):
        e = Employee(school_id=school_id, employee_id=f'E{tag}{self.sfx}'[:38],
                     full_name=f'Emp {tag}', base_salary=0, status='active',
                     user_id=user_id)
        db.session.add(e)
        db.session.flush()
        self.ids[f'emp_{tag}'] = e.id
        return e.id

    def _subject(self, school_id, tag):
        s = Subject(school_id=school_id, academic_year_id=self._year_of(school_id),
                    name=f'مادة {tag}', code=f'S{tag}{self.sfx[:6]}')
        db.session.add(s)
        db.session.flush()
        self.ids[f'subj_{tag}'] = s.id
        return s.id

    def _student(self, key, school_id, section_id=None):
        st = Student(student_id=f'IP{uuid4().hex[:10]}', full_name=f'Child {key}',
                     school_id=school_id, academic_year_id=self._year_of(school_id),
                     section_id=section_id, status='active')
        db.session.add(st)
        db.session.flush()
        self.ids[key] = st.id

    def _link(self, parent_tag, student_key):
        db.session.execute(parent_students.insert().values(
            user_id=self.ids[parent_tag], student_id=self.ids[student_key]))

    def tearDown(self):
        self.app.config['STUDY_REQUEST_OUTBOX_ENABLED'] = False
        with self.app.app_context():
            db.session.rollback()
            for key in ('a', 'b', 'c'):
                sid = self.ids[f'school_{key}']
                for model in (NotificationOutbox, Homework):
                    model.query.execution_options(**OPTS).filter_by(
                        school_id=sid).delete(synchronize_session=False)
                cleanup_school_cascade(sid)
            db.session.commit()
            db.session.remove()

    # ── homework helpers ──────────────────────────────────────────────────────

    def _push_mocks(self):
        """Mock every FCM entry point; FCM reports enabled so push work is
        actually produced (and captured) instead of being skipped."""
        submit = mock.patch('app.services.async_dispatch.submit', return_value=True)
        sync = mock.patch.object(fcm_service, 'send_push_to_user', return_value=(1, 0))
        enabled = mock.patch.object(fcm_service, 'is_enabled', return_value=True)
        mocks = types.SimpleNamespace(submit=submit.start(), sync=sync.start())
        enabled.start()
        for p in (submit, sync, enabled):
            self.addCleanup(p.stop)
        return mocks

    @staticmethod
    def _batches(submit_mock):
        return [c.args[1] for c in submit_mock.call_args_list
                if c.args and c.args[0] is fcm_service.send_push_batch]

    def _web_create(self, user_key, form):
        from app.blueprints.homework import create
        with self.app.test_request_context('/homework/create', method='POST', data=form):
            login_user(db.session.get(User, self.ids[user_key], execution_options=OPTS))
            for fn in self.app.before_request_funcs.get(None, []):
                if fn() is not None:
                    break
            try:
                return create()
            finally:
                logout_user()

    def _form(self, title, **over):
        data = {'title': title,
                'publish_date': date.today().strftime('%Y-%m-%d'),
                'due_date': (date.today() + timedelta(days=3)).strftime('%Y-%m-%d'),
                'description': '', 'institute_group_id': str(self.ids['ga'])}
        data.update(over)
        return data

    def _hw_id(self, school_key, title):
        with self.app.app_context():
            return (Homework.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids[f'school_{school_key}'], title=title)
                    .one().id)

    def _hw_notified(self, title):
        with self.app.app_context():
            rows = (Notification.query.execution_options(**OPTS)
                    .filter(Notification.ntype == 'homework',
                            Notification.body.contains(title)).all())
            return [(r.school_id, r.target_user_id, r.created_by) for r in rows]

    def _expected_institute_parents(self):
        return {self.ids['p1'], self.ids['p2'], self.ids['pmix']}

    def _assert_institute_push(self, mocks, title):
        hw_id = self._hw_id('a', title)
        batches = self._batches(mocks.submit)
        self.assertEqual(len(batches), 1, 'exactly ONE queued push batch per homework')
        items = batches[0]
        users = [item[0] for item in items]
        self.assertEqual(sorted(users), sorted(self._expected_institute_parents()))
        self.assertEqual(len(users), len(set(users)), 'one push per parent')
        for uid, push_title, body, data in items:
            self.assertEqual(push_title, 'واجب جديد')
            self.assertIn(title, body)
            self.assertEqual(data['type'], 'homework')
            self.assertEqual(data['homework_id'], str(hw_id))
            self.assertEqual(data['institute_group_id'], str(self.ids['ga']))
            self.assertNotIn('section_id', data)
            self.assertTrue(all(isinstance(v, str) for v in data.values()))
        by_user = {item[0]: item[3]['student_id'] for item in items}
        self.assertEqual(by_user[self.ids['p1']], str(self.ids['s1']))
        self.assertEqual(by_user[self.ids['p2']], str(self.ids['s2']))
        self.assertEqual(by_user[self.ids['pmix']],
                         str(min(self.ids['s1'], self.ids['s2'])))
        mocks.sync.assert_not_called()          # no synchronous per-parent send
        return hw_id

    # ── 1-2, 4-8, 10: web instructor / administration ────────────────────────

    def test_01_teacher_web_institute_homework_pushes_once_per_parent(self):
        mocks = self._push_mocks()
        self._web_create('ta', self._form('HW teacher web'))
        self._assert_institute_push(mocks, 'HW teacher web')
        rows = self._hw_notified('HW teacher web')
        self.assertEqual(sorted(r[1] for r in rows), sorted(self._expected_institute_parents()))
        self.assertTrue(all(r[0] == self.ids['school_a'] for r in rows))
        self.assertTrue(all(r[2] == self.ids['ta'] for r in rows))
        notified = {r[1] for r in rows}
        for outsider in ('p_out', 'p_inactive', 'p_foreign', 'pb', 'p_sch'):
            self.assertNotIn(self.ids[outsider], notified, outsider)

    def test_02_admin_web_institute_homework_pushes(self):
        mocks = self._push_mocks()
        self._web_create('adm', self._form('HW admin web'))
        self._assert_institute_push(mocks, 'HW admin web')
        rows = self._hw_notified('HW admin web')
        self.assertEqual(len(rows), 3, 'pmix (two children) gets ONE row')

    def test_06_foreign_school_parent_never_reached(self):
        mocks = self._push_mocks()
        self._web_create('ta', self._form('HW foreign'))
        pushed = {item[0] for b in self._batches(mocks.submit) for item in b}
        self.assertNotIn(self.ids['p_foreign'], pushed)
        with self.app.app_context():
            self.assertEqual(Notification.query.execution_options(**OPTS).filter_by(
                target_user_id=self.ids['p_foreign']).count(), 0)
            self.assertEqual(Notification.query.execution_options(**OPTS).filter_by(
                school_id=self.ids['school_b']).count(), 0)

    # ── 3: mobile instructor ─────────────────────────────────────────────────

    def test_03_teacher_mobile_institute_homework_pushes_once(self):
        mocks = self._push_mocks()
        with self.app.app_context():
            token = encode_token(db.session.get(User, self.ids['ta'], execution_options=OPTS))
        resp = self.app.test_client().post(
            MOBILE_HW, headers={'Authorization': f'Bearer {token}'},
            json={'group_id': self.ids['ga'], 'title': 'HW mobile',
                  'due_date': (date.today() + timedelta(days=2)).isoformat()})
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        self._assert_institute_push(mocks, 'HW mobile')
        rows = self._hw_notified('HW mobile')
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r[2] == self.ids['ta'] for r in rows))

    def test_03b_background_helper_uses_institute_payload(self):
        """_notify_homework_bg no longer withholds FCM for an institute row and
        carries the same institute payload (no section_id)."""
        from app.blueprints.mobile_api.teacher import _notify_homework_bg
        mocks = self._push_mocks()
        with self.app.app_context():
            hw = Homework(school_id=self.ids['school_a'], academic_year_id=self.ids['year_a'],
                          teacher_id=self.ids['emp_ta'], subject_id=self.ids['subj_sa'],
                          institute_group_id=self.ids['ga'], title='HW bg',
                          publish_date=date.today(), due_date=date.today(), is_active=True)
            db.session.add(hw)
            db.session.commit()
            hw_id = hw.id
            _notify_homework_bg(hw_id, self.ids['school_a'], self.ids['ta'])
        items = self._batches(mocks.submit)[0]
        self.assertEqual({i[0] for i in items}, self._expected_institute_parents())
        for *_, data in items:
            self.assertEqual(data['institute_group_id'], str(self.ids['ga']))
            self.assertNotIn('section_id', data)

    # ── 9: dedup is per event, not per text ──────────────────────────────────

    def test_09_old_identical_text_does_not_suppress_new_homework(self):
        mocks = self._push_mocks()
        with self.app.app_context():
            db.session.add(Notification(
                school_id=self.ids['school_a'], title='واجب جديد',
                body='تم إضافة واجب جديد في مادة مادة sa: HW repeat', ntype='homework',
                target_user_id=self.ids['p1'],
                created_at=datetime.utcnow() - timedelta(days=30)))
            db.session.commit()
        self._web_create('ta', self._form('HW repeat'))
        self._assert_institute_push(mocks, 'HW repeat')
        rows = [r for r in self._hw_notified('HW repeat') if r[1] == self.ids['p1']]
        self.assertEqual(len(rows), 2, 'the old row did not suppress the new homework')

    # ── 11: normal school unchanged ──────────────────────────────────────────

    def test_11_normal_school_homework_unchanged(self):
        mocks = self._push_mocks()
        self._web_create('tc', {
            'title': 'HW school', 'publish_date': date.today().strftime('%Y-%m-%d'),
            'due_date': (date.today() + timedelta(days=3)).strftime('%Y-%m-%d'),
            'description': '', 'section_id': str(self.ids['sec_c']),
            'grade_id': str(self.ids['grade_c']), 'subject_id': str(self.ids['subj_sc'])})
        hw_id = self._hw_id('c', 'HW school')
        self.assertEqual(self._batches(mocks.submit), [], 'school path is not batched')
        mocks.sync.assert_called_once()
        uid, title, body, data = mocks.sync.call_args.args
        self.assertEqual(uid, self.ids['p_sch'])
        self.assertEqual(data, {'type': 'homework', 'ntype': 'homework',
                                'route': '/parent/homework', 'homework_id': str(hw_id),
                                'section_id': str(self.ids['sec_c']),
                                'student_id': str(self.ids['s_sch']), 'screen': 'homework'})
        self.assertEqual([r[1] for r in self._hw_notified('HW school')], [self.ids['p_sch']])

    # ── study request helpers ─────────────────────────────────────────────────

    def _login(self, key):
        client = self.app.test_client()
        with self.app.app_context():
            name = db.session.get(User, self.ids[key], execution_options=OPTS).username
        resp = client.post('/auth/login', data={'username': name, 'password': PASSWORD})
        self.assertEqual(resp.status_code, 302)
        return client

    def _request(self, key='a', parent='p1', student='s1', status='pending', note=None):
        with self.app.app_context():
            req = InstituteStudyRequest(
                school_id=self.ids[f'school_{key}'], academic_year_id=self.ids[f'year_{key}'],
                student_id=self.ids[student], parent_id=self.ids[parent],
                status=status, admin_note=note, note='درس الرياضيات', image_count=0)
            db.session.add(req)
            db.session.commit()
            return req.id

    def _req(self, rid):
        with self.app.app_context():
            return db.session.get(InstituteStudyRequest, rid, execution_options=OPTS)

    def _sr_notifs(self):
        with self.app.app_context():
            return [(n.school_id, n.target_user_id, n.title, n.body, n.created_by)
                    for n in Notification.query.execution_options(**OPTS)
                    .filter_by(ntype='study_request').all()]

    def _outbox_rows(self):
        with self.app.app_context():
            return (NotificationOutbox.query.execution_options(**OPTS)
                    .filter_by(event_type=NotificationOutbox.EVENT_INSTITUTE_STUDY_REQUEST_VIEWED)
                    .order_by(NotificationOutbox.device_token_id).all())

    def _capture_sql(self):
        statements = []
        with self.app.app_context():
            engine = db.engine

        def _cap(conn, cursor, statement, *a):
            statements.append(statement)

        def _commit(conn):
            statements.append('<<COMMIT>>')
        event.listen(engine, 'before_cursor_execute', _cap)
        event.listen(engine, 'commit', _commit)
        self.addCleanup(event.remove, engine, 'before_cursor_execute', _cap)
        self.addCleanup(event.remove, engine, 'commit', _commit)
        return statements

    # ── 12-18, 25-26: first view ─────────────────────────────────────────────

    def test_12_17_18_first_view_one_notification_and_outbox_in_one_commit(self):
        self.app.config['STUDY_REQUEST_OUTBOX_ENABLED'] = True
        rid = self._request()
        client = self._login('adm')
        statements = self._capture_sql()
        self.assertEqual(client.post(f'{LIST}/{rid}', data={'admin_note': ''}).status_code, 302)

        row = self._req(rid)
        self.assertEqual((row.status, row.viewed_by), ('viewed', self.ids['adm']))
        self.assertEqual(self._sr_notifs(), [(
            self.ids['school_a'], self.ids['p1'], 'تم الاطلاع على درسك',
            'تم اطلاع الإدارة على الدرس الذي أرسلته.', self.ids['adm'])])
        jobs = self._outbox_rows()
        self.assertEqual([j.device_token_id for j in jobs], [self.ids['t1'], self.ids['t2']])
        for job in jobs:
            self.assertEqual((job.user_id, job.school_id, job.status, job.ntype),
                             (self.ids['p1'], self.ids['school_a'], 'pending', 'study_request'))
            self.assertEqual(job.dedup_key, f'institute_study_request_viewed:{rid}:'
                                            f'{self.ids["p1"]}:{job.device_token_id}')
            self.assertEqual(json.loads(job.data_json), {
                'type': 'study_request', 'ntype': 'study_request', 'action': 'viewed',
                'screen': 'study_requests', 'student_id': str(self.ids['s1']),
                'request_id': str(rid), 'has_admin_note': '0'})

        # 18. UPDATE + Notification INSERT + outbox INSERT share ONE commit.
        segments, cur = [], []
        for st in statements:
            if st == '<<COMMIT>>':
                segments.append(cur)
                cur = []
            else:
                cur.append(st.lstrip().upper())
        tx = [seg for seg in segments
              if any(s.startswith('UPDATE INSTITUTE_STUDY_REQUESTS') for s in seg)]
        self.assertEqual(len(tx), 1)
        self.assertTrue(any(s.startswith('INSERT INTO NOTIFICATIONS') for s in tx[0]))
        self.assertTrue(any(s.startswith('INSERT INTO NOTIFICATION_OUTBOX') for s in tx[0]))
        # Pressure: ONE token query; the parent link is checked inside the
        # request lookup, never as a separate parent_students query.
        flat = [s.upper() for s in statements if s != '<<COMMIT>>']
        self.assertEqual(sum(1 for s in flat if 'FROM MOBILE_DEVICE_TOKENS' in s), 1)
        self.assertFalse(any('PARENT_STUDENTS' in s and 'INSTITUTE_STUDY_REQUESTS' not in s
                             for s in flat))

    def test_13_only_the_submitting_parent(self):
        # pmix is ALSO linked to s1 — it must not be notified.
        self.app.config['STUDY_REQUEST_OUTBOX_ENABLED'] = True
        rid = self._request()
        self._login('adm').post(f'{LIST}/{rid}', data={})
        self.assertEqual([n[1] for n in self._sr_notifs()], [self.ids['p1']])
        self.assertEqual({j.user_id for j in self._outbox_rows()}, {self.ids['p1']})

    def test_15_no_active_token_still_writes_in_app_row(self):
        self.app.config['STUDY_REQUEST_OUTBOX_ENABLED'] = True
        rid = self._request(parent='p2', student='s2')
        self._login('adm').post(f'{LIST}/{rid}', data={})
        self.assertEqual([n[1] for n in self._sr_notifs()], [self.ids['p2']])
        self.assertEqual(self._outbox_rows(), [])

    def test_16_flag_off_in_app_row_only(self):
        self.assertFalse(self.app.config['STUDY_REQUEST_OUTBOX_ENABLED'])
        rid = self._request()
        self._login('adm').post(f'{LIST}/{rid}', data={})
        self.assertEqual(self._req(rid).status, 'viewed')
        self.assertEqual(len(self._sr_notifs()), 1)
        self.assertEqual(self._outbox_rows(), [])

    def test_17b_flag_feeds_worker_gate(self):
        with self.app.app_context():
            for flag in ('INSTITUTE_ATTENDANCE_OUTBOX_ENABLED', 'AIFACE_ATTENDANCE_OUTBOX_ENABLED',
                         'MANUAL_ATTENDANCE_OUTBOX_ENABLED', 'AUTO_ABSENCE_OUTBOX_ENABLED'):
                self.app.config[flag] = False
            self.assertFalse(outbox.any_enabled())
            self.app.config['STUDY_REQUEST_OUTBOX_ENABLED'] = True
            self.assertTrue(outbox.study_request_enabled())
            self.assertTrue(outbox.any_enabled())

    def test_25_26_note_flagged_but_never_sent(self):
        self.app.config['STUDY_REQUEST_OUTBOX_ENABLED'] = True
        rid = self._request()
        secret = 'ملاحظة خاصة بالطالب 98765'
        self._login('adm').post(f'{LIST}/{rid}', data={'admin_note': secret})
        self.assertEqual(self._req(rid).admin_note, secret)
        (notif,) = self._sr_notifs()
        self.assertEqual(notif[3], 'تم اطلاع الإدارة على الدرس الذي أرسلته. '
                                   'توجد ملاحظة من الإدارة.')
        jobs = self._outbox_rows()
        self.assertEqual(len(jobs), 2)
        for job in jobs:
            self.assertEqual(json.loads(job.data_json)['has_admin_note'], '1')
            for text in (job.title, job.body, job.data_json):
                self.assertNotIn(secret, text)
                self.assertNotIn('98765', text)
        self.assertNotIn(secret, notif[2] + notif[3])

    # ── 19: commit failure ───────────────────────────────────────────────────

    def test_19_commit_failure_rolls_everything_back(self):
        self.app.config['STUDY_REQUEST_OUTBOX_ENABLED'] = True
        rid = self._request()
        client = self._login('adm')

        # The rows may already be flushed (the token query autoflushes), so mark
        # the transaction on flush and fail its COMMIT — the real failure mode.
        def _is_viewed_row(o):
            return isinstance(o, Notification) and o.ntype == 'study_request'

        def _mark(session, _ctx):
            if any(_is_viewed_row(o) for o in session.new):
                session.info['ipn_fail_commit'] = True

        def _fail(session):
            if (session.info.pop('ipn_fail_commit', False)
                    or any(_is_viewed_row(o) for o in session.new)):
                raise RuntimeError('simulated commit failure')
        event.listen(Session, 'after_flush', _mark)
        event.listen(Session, 'before_commit', _fail)
        self.addCleanup(event.remove, Session, 'after_flush', _mark)
        self.addCleanup(event.remove, Session, 'before_commit', _fail)

        resp = client.post(f'{LIST}/{rid}', data={'admin_note': 'x'})
        self.assertEqual(resp.status_code, 302)
        row = self._req(rid)
        self.assertEqual((row.status, row.admin_note, row.viewed_by), ('pending', None, None))
        self.assertEqual(self._sr_notifs(), [])
        self.assertEqual(self._outbox_rows(), [])

    # ── 20-21: later saves ───────────────────────────────────────────────────

    def test_20_21_viewed_saves_never_notify(self):
        self.app.config['STUDY_REQUEST_OUTBOX_ENABLED'] = True
        rid = self._request()
        client = self._login('adm')
        client.post(f'{LIST}/{rid}', data={'admin_note': 'أولى'})
        first = self._req(rid)
        self.assertEqual((len(self._sr_notifs()), len(self._outbox_rows())), (1, 2))

        # 20. unchanged → no UPDATE, nothing staged.
        statements = self._capture_sql()
        client.post(f'{LIST}/{rid}', data={'admin_note': ' أولى '})
        self.assertFalse(any(s.lstrip().upper().startswith('UPDATE INSTITUTE_STUDY_REQUESTS')
                             for s in statements if s != '<<COMMIT>>'))
        self.assertEqual(self._req(rid).updated_at, first.updated_at)
        # 21. note edit → note only, nothing staged.
        self._login('adm2').post(f'{LIST}/{rid}', data={'admin_note': 'ثانية'})
        row = self._req(rid)
        self.assertEqual((row.admin_note, row.viewed_by, row.viewed_at),
                         ('ثانية', self.ids['adm'], first.viewed_at))
        self.assertEqual((len(self._sr_notifs()), len(self._outbox_rows())), (1, 2))

    # ── 22: concurrent first saves ───────────────────────────────────────────

    def test_22a_stale_pending_read_loses_and_keeps_winner_note(self):
        from app.blueprints.admin import institute_study_requests as mod
        self.app.config['STUDY_REQUEST_OUTBOX_ENABLED'] = True
        rid = self._request()
        self._login('adm').post(f'{LIST}/{rid}', data={'admin_note': 'من الأول'})
        real = mod._save_lookup

        def stale(school_id, request_id):
            row = real(school_id, request_id)
            return types.SimpleNamespace(status='pending', admin_note=None,
                                         student_id=row.student_id, parent=row.parent,
                                         parent_linked=row.parent_linked)
        with mock.patch(SAVE_LOOKUP, side_effect=stale):
            # Lost the race with no note of its own → the winner's note stays.
            self.assertEqual(self._login('adm2').post(
                f'{LIST}/{rid}', data={'admin_note': ''}).status_code, 302)
            self.assertEqual(self._req(rid).admin_note, 'من الأول')
            # Lost the race with an entered note → that note is saved.
            self._login('adm2').post(f'{LIST}/{rid}', data={'admin_note': 'من الثاني'})
        row = self._req(rid)
        self.assertEqual((row.admin_note, row.viewed_by), ('من الثاني', self.ids['adm']))
        self.assertEqual(len(self._sr_notifs()), 1)
        self.assertEqual(len(self._outbox_rows()), 2)

    def test_22b_two_real_concurrent_first_saves_one_winner(self):
        from app.blueprints.admin import institute_study_requests as mod
        self.app.config['STUDY_REQUEST_OUTBOX_ENABLED'] = True
        rid = self._request()
        clients = [self._login('adm'), self._login('adm2')]
        real = mod._save_lookup
        barrier = threading.Barrier(2, timeout=15)

        def synced(school_id, request_id):
            row = real(school_id, request_id)
            barrier.wait()                  # both have read status=pending
            return row

        codes, errors = [], []

        def run(client, note):
            try:
                codes.append(client.post(f'{LIST}/{rid}', data={'admin_note': note}).status_code)
            except Exception as exc:        # pragma: no cover — surfaced below
                errors.append(exc)
        with mock.patch(SAVE_LOOKUP, side_effect=synced):
            threads = [threading.Thread(target=run, args=(c, n))
                       for c, n in zip(clients, ('A', 'B'))]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(codes, [302, 302])
        self.assertEqual(self._req(rid).status, 'viewed')
        self.assertEqual(len(self._sr_notifs()), 1, 'exactly one transition winner')
        jobs = self._outbox_rows()
        self.assertEqual(sorted(j.device_token_id for j in jobs),
                         [self.ids['t1'], self.ids['t2']])

    # ── 23-24: isolation and eligibility ─────────────────────────────────────

    def test_23_other_school_request_404_no_writes(self):
        self.app.config['STUDY_REQUEST_OUTBOX_ENABLED'] = True
        rid = self._request(key='b', parent='pb', student='sb')
        statements = self._capture_sql()
        self.assertEqual(self._login('adm').post(
            f'{LIST}/{rid}', data={'admin_note': 'x'}).status_code, 404)
        self.assertFalse(any(s.lstrip().upper().startswith('UPDATE INSTITUTE_STUDY_REQUESTS')
                             for s in statements if s != '<<COMMIT>>'))
        row = self._req(rid)
        self.assertEqual((row.status, row.admin_note), ('pending', None))
        self.assertEqual((self._sr_notifs(), self._outbox_rows()), ([], []))

    def test_24_ineligible_parent_viewed_without_push(self):
        self.app.config['STUDY_REQUEST_OUTBOX_ENABLED'] = True
        unlinked = self._request()
        inactive = self._request(parent='p2', student='s2')
        with self.app.app_context():
            db.session.execute(parent_students.delete().where(
                (parent_students.c.user_id == self.ids['p1'])
                & (parent_students.c.student_id == self.ids['s1'])))
            db.session.get(User, self.ids['p2'], execution_options=OPTS).is_active = False
            db.session.commit()
        client = self._login('adm')
        for rid in (unlinked, inactive):
            self.assertEqual(client.post(f'{LIST}/{rid}', data={'admin_note': 'n'}).status_code, 302)
            self.assertEqual(self._req(rid).status, 'viewed')
        self.assertEqual((self._sr_notifs(), self._outbox_rows()), ([], []))


if __name__ == '__main__':
    unittest.main()

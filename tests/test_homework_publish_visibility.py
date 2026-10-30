"""
Homework publication visibility and notification recipients.

  * parent homework list (school + institute): visible once publish_date <=
    the SCHOOL's local today (Asia/Baghdad), never the UTC server date —
    including the Baghdad midnight boundary;
  * badge: counts only parent-visible homework, and counts a scheduled
    homework once its publish date arrives;
  * web and mobile teacher creation notify the SAME recipients with the SAME
    push payload: active parents of this school only, one per parent, after
    the homework commit, and an older homework with identical text never
    suppresses a new one;
  * unchanged contracts: response keys, ordering, isolation, teacher guard.

The clock is frozen by patching the attendance helper's datetime (the single
place the school-local date is computed). FCM is mocked; no network.
"""
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import text

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (db, AcademicYear, AuditLog, Employee, Grade, Homework,
                        InstituteGroupEnrollment, InstituteStudyGroup,
                        MobileModuleView, Notification, Role, School, Section,
                        Student, Subject, User, parent_students, teacher_subjects)
from app.utils.homework_visibility import school_local_today

PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True}
LEGACY_KEYS = {'ok', 'student_id', 'section', 'count', 'homework'}
PARENT_ITEM_KEYS = {'id', 'homework_id', 'title', 'subject', 'subject_name',
                    'teacher_name', 'grade_name', 'section_name', 'assigned_at',
                    'publish_date', 'due_date', 'description', 'status',
                    'attachment_url', 'attachment_type', 'file_name', 'file_size',
                    'is_pdf', 'submitted_status'}
PUSH_KEYS = {'type', 'ntype', 'route', 'homework_id', 'section_id',
             'student_id', 'screen'}

D = date(2026, 10, 31)                                # a Baghdad calendar day
NOON_D = datetime(2026, 10, 31, 9, 0)                 # 12:00 Baghdad on D
AFTER_MIDNIGHT = datetime(2026, 10, 30, 21, 30)       # 00:30 Baghdad on D
BEFORE_MIDNIGHT = datetime(2026, 10, 30, 20, 30)      # 23:30 Baghdad on D-1


@contextmanager
def frozen_utc(naive_utc):
    """Freeze the school-local clock at a naive-UTC instant."""
    aware = naive_utc.replace(tzinfo=timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return aware.astimezone(tz) if tz is not None else naive_utc

    with patch('app.utils.attendance_helpers.datetime', _Frozen):
        yield


class HomeworkPublishVisibilityTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['BACKEND_CACHE_ENABLED'] = False     # badge computed per call
        with cls.app.app_context():
            cls.role_ids = {n: Role.query.filter_by(name=n).first().id
                            for n in ('school_admin', 'parent', 'teacher')}

    # ── fixtures ─────────────────────────────────────────────────────────────

    def _user(self, school_id, label, role, active=True):
        u = User(username=f'hpv{label}_{self.sfx}', email=f'hpv{label}_{self.sfx}@example.test',
                 full_name=f'{label} {self.sfx}', role_id=self.role_ids[role],
                 school_id=school_id, is_active=active)
        u.set_password(PASSWORD)
        db.session.add(u)
        db.session.flush()
        return u

    def _student(self, school_id, year_id, name, section_id=None):
        s = Student(student_id=f'S-{uuid4().hex[:10]}', full_name=name,
                    school_id=school_id, academic_year_id=year_id,
                    section_id=section_id, status='active')
        db.session.add(s)
        db.session.flush()
        return s

    def _link(self, user, student):
        db.session.execute(parent_students.insert().values(
            user_id=user.id, student_id=student.id, relation='guardian'))

    def _school(self, key, *, institute=False):
        school = School(school_name=f'HPV {key} {self.sfx}', code=f'HV{key}{self.sfx}'[:20],
                        capacity=0, is_active=True, timezone='Asia/Baghdad',
                        institution_type=(School.INSTITUTION_INSTITUTE if institute else None))
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'Y {key} {self.sfx}', is_current=True,
                            start_date=date(2026, 8, 1), end_date=date(2027, 6, 30))
        db.session.add(year)
        db.session.flush()
        subj = Subject(school_id=school.id, academic_year_id=year.id, name=f'Math {key}',
                       code=f'V{key}{self.sfx[:6]}')
        db.session.add(subj)
        db.session.flush()
        self.ids[f'school_{key}'] = school.id
        self.ids[f'year_{key}'] = year.id
        self.ids[f'subj_{key}'] = subj.id
        return school, year, subj

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        with self.app.app_context():
            # ── School A ─────────────────────────────────────────────────────
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
            emp = Employee(school_id=school.id, employee_id=f'V{self.sfx}', full_name=f'T {self.sfx}',
                           base_salary=0, status='active', user_id=tuser.id)
            db.session.add(emp)
            db.session.flush()
            # Subject teacher of (X, subj) only — Y is NOT assigned.
            db.session.execute(teacher_subjects.insert().values(
                employee_id=emp.id, subject_id=subj.id, section_id=sec_x.id))

            c1 = self._student(school.id, year.id, 'C1', sec_x.id)
            c2 = self._student(school.id, year.id, 'C2', sec_x.id)
            c3 = self._student(school.id, year.id, 'C3', sec_y.id)
            c4 = self._student(school.id, year.id, 'C4', sec_x.id)
            c5 = self._student(school.id, year.id, 'C5', sec_x.id)

            p1 = self._user(school.id, 'p1', 'parent')            # c1 + c2 (both X)
            p_mix = self._user(school.id, 'pmix', 'parent')       # c1 (X) + c3 (Y)
            p_y = self._user(school.id, 'py', 'parent')           # c3 only (Y)
            p_off = self._user(school.id, 'poff', 'parent', active=False)   # c4 (X)
            for parent, child in ((p1, c1), (p1, c2), (p_mix, c1), (p_mix, c3),
                                  (p_y, c3), (p_off, c4)):
                self._link(parent, child)

            # ── School B (cross-tenant) ──────────────────────────────────────
            school_b, year_b, subj_b = self._school('b')
            grade_b = Grade(school_id=school_b.id, academic_year_id=year_b.id,
                            name=f'H{self.sfx[:4]}')
            db.session.add(grade_b)
            db.session.flush()
            sec_b = Section(school_id=school_b.id, academic_year_id=year_b.id,
                            grade_id=grade_b.id, name=f'B{self.sfx[:4]}', capacity=30)
            db.session.add(sec_b)
            db.session.flush()
            cb = self._student(school_b.id, year_b.id, 'CB', sec_b.id)
            pb = self._user(school_b.id, 'pb', 'parent')
            self._link(pb, cb)
            # A corrupt cross-school link: B's parent account linked to A's c5.
            p_foreign = self._user(school_b.id, 'pfor', 'parent')
            self._link(p_foreign, c5)
            db.session.add(Homework(
                school_id=school_b.id, academic_year_id=year_b.id, subject_id=subj_b.id,
                section_id=sec_b.id, title=f'B-only {self.sfx}', publish_date=D - timedelta(days=1),
                due_date=D + timedelta(days=3), is_active=True))

            # ── Institute I ──────────────────────────────────────────────────
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
            db.session.add(InstituteGroupEnrollment(
                school_id=inst.id, group_id=group.id, student_id=ci.id,
                status=InstituteGroupEnrollment.STATUS_ACTIVE))
            pi = self._user(inst.id, 'pi', 'parent')
            self._link(pi, ci)

            db.session.commit()
            self.ids.update({
                'grade': grade.id, 'sec_x': sec_x.id, 'sec_y': sec_y.id, 'emp': emp.id,
                'admin': admin.id, 'tuser': tuser.id,
                'c1': c1.id, 'c2': c2.id, 'c3': c3.id, 'c4': c4.id, 'c5': c5.id,
                'p1': p1.id, 'p_mix': p_mix.id, 'p_y': p_y.id, 'p_off': p_off.id,
                'cb': cb.id, 'pb': pb.id, 'p_foreign': p_foreign.id,
                'iemp': iemp.id, 'group': group.id, 'ci': ci.id, 'pi': pi.id,
            })

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            self.purge([self.ids[f'school_{k}'] for k in ('a', 'b', 'i')
                        if f'school_{k}' in self.ids])
            db.session.remove()

    @staticmethod
    def purge(school_ids):
        """Delete every fixture row of these schools. Links are removed for ALL
        of them first: the fixture deliberately links a school-B parent to a
        school-A child."""
        uids = [u.id for u in User.query.execution_options(**OPTS)
                .filter(User.school_id.in_(school_ids)).all()]
        sids = [s.id for s in Student.query.execution_options(**OPTS)
                .filter(Student.school_id.in_(school_ids)).all()]
        if uids:
            for model in (AuditLog, MobileModuleView):
                model.query.execution_options(**OPTS).filter(
                    model.user_id.in_(uids)).delete(synchronize_session=False)
            Notification.query.execution_options(**OPTS).filter(
                Notification.target_user_id.in_(uids)).delete(synchronize_session=False)
            db.session.execute(parent_students.delete().where(
                parent_students.c.user_id.in_(uids)))
        if sids:
            db.session.execute(parent_students.delete().where(
                parent_students.c.student_id.in_(sids)))
        db.session.execute(teacher_subjects.delete().where(
            teacher_subjects.c.employee_id.in_(
                db.session.query(Employee.id).filter(Employee.school_id.in_(school_ids)))))
        for model in (Notification, Homework, InstituteGroupEnrollment,
                      InstituteStudyGroup, Student, Section, Subject, Grade,
                      Employee, User, AcademicYear):
            model.query.execution_options(**OPTS).filter(
                model.school_id.in_(school_ids)).delete(synchronize_session=False)
        School.query.filter(School.id.in_(school_ids)).delete(synchronize_session=False)
        db.session.commit()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _hw(self, publish, *, section='sec_x', title=None, created_at=None, school='a',
            group=None):
        with self.app.app_context():
            hw = Homework(
                school_id=self.ids[f'school_{school}'], academic_year_id=self.ids[f'year_{school}'],
                subject_id=self.ids[f'subj_{school}'],
                section_id=None if group else self.ids[section],
                institute_group_id=self.ids['group'] if group else None,
                title=title or f'HW {uuid4().hex[:6]}', publish_date=publish,
                due_date=publish + timedelta(days=2), is_active=True,
                created_at=created_at or datetime.utcnow())
            db.session.add(hw)
            db.session.commit()
            return hw.id

    def _jwt(self, key):
        with self.app.app_context():
            return {'Authorization': 'Bearer ' + encode_token(
                db.session.get(User, self.ids[key], execution_options=OPTS))}

    def _list(self, parent='p1', child='c1'):
        resp = self.app.test_client().get(
            f"/api/mobile/v1/parent/children/{self.ids[child]}/homework",
            headers=self._jwt(parent))
        return resp

    def _visible_ids(self, parent='p1', child='c1'):
        resp = self._list(parent, child)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return [h['id'] for h in resp.get_json()['homework']]

    def _badge(self, parent='p1'):
        resp = self.app.test_client().get('/api/mobile/v1/me/badge-counts',
                                          headers=self._jwt(parent))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()['badges']['homework']

    def _viewed(self, at, parent='p1'):
        with self.app.app_context():
            db.session.add(MobileModuleView(user_id=self.ids[parent],
                                            school_id=self.ids['school_a'],
                                            module='homework', last_viewed_at=at))
            db.session.commit()

    def _web_client(self):
        client = self.app.test_client()
        with self.app.app_context():
            name = db.session.get(User, self.ids['admin'], execution_options=OPTS).username
        resp = client.post('/auth/login', data={'username': name, 'password': PASSWORD})
        self.assertIn(resp.status_code, (200, 302))
        return client

    @contextmanager
    def _pushes(self):
        """Mock FCM. Each push records (user_id, data) plus whether the homework
        and the recipient's in-app row were COMMITTED when the push was handed
        over — checked on a separate connection, which sees committed data only."""
        sent = []
        engine = None
        with self.app.app_context():
            engine = db.engine

        def _record(user_id, title, body, data=None):
            with engine.connect() as conn:
                hw_committed = conn.execute(
                    text('SELECT count(*) FROM homework WHERE id = :i'),
                    {'i': int(data['homework_id'])}).scalar()
                row_committed = conn.execute(
                    text("SELECT count(*) FROM notifications WHERE target_user_id = :u "
                         "AND ntype = 'homework'"), {'u': user_id}).scalar()
            sent.append({'user_id': user_id, 'title': title, 'body': body, 'data': data,
                         'hw_committed': hw_committed == 1,
                         'row_committed': row_committed >= 1})
            return 1, 0

        with patch('app.services.fcm_service.is_enabled', return_value=True), \
                patch('app.services.fcm_service.send_push_to_user', side_effect=_record):
            yield sent

    def _rows(self, hw_title=None):
        """{target_user_id: count} of homework Notification rows in schools a/b."""
        with self.app.app_context():
            q = (Notification.query.execution_options(**OPTS)
                 .filter(Notification.ntype == 'homework',
                         Notification.school_id.in_([self.ids['school_a'],
                                                     self.ids['school_b']])))
            if hw_title:
                q = q.filter(Notification.body.contains(hw_title))
            out = {}
            for n in q.all():
                out[n.target_user_id] = out.get(n.target_user_id, 0) + 1
            return out

    def _web_create(self, client, title, section='sec_x'):
        return client.post('/homework/create', data={
            'title': title, 'subject_id': str(self.ids['subj_a']),
            'grade_id': str(self.ids['grade']), 'section_id': str(self.ids[section]),
            'publish_date': date.today().isoformat(),
            'due_date': (date.today() + timedelta(days=2)).isoformat(),
            'description': ''})

    def _mobile_create(self, title, section='sec_x'):
        return self.app.test_client().post(
            '/api/mobile/v1/teacher/homework', headers=self._jwt('tuser'),
            json={'title': title, 'section_id': self.ids[section],
                  'subject_id': self.ids['subj_a'],
                  'publish_date': date.today().isoformat(),
                  'due_date': (date.today() + timedelta(days=2)).isoformat()})

    def _hw_id(self, title):
        with self.app.app_context():
            return (Homework.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids['school_a'], title=title).one().id)

    # ── 1. school-local visibility (parent list) ─────────────────────────────

    def test_01_02_published_today_and_yesterday_are_visible(self):
        today_id, past_id = self._hw(D), self._hw(D - timedelta(days=1))
        with frozen_utc(NOON_D):
            self.assertEqual(set(self._visible_ids()), {today_id, past_id})

    def test_03_04_future_hidden_until_its_local_publish_date(self):
        future_id = self._hw(D + timedelta(days=1))
        with frozen_utc(NOON_D):
            self.assertNotIn(future_id, self._visible_ids())
        with frozen_utc(NOON_D + timedelta(days=1)):
            self.assertIn(future_id, self._visible_ids())

    def test_07_baghdad_midnight_boundary(self):
        hw_id = self._hw(D)
        # 21:30 UTC on D-1 is already 00:30 on D in Baghdad: visible, although
        # the UTC date (the old date.today() rule) is still D-1.
        with frozen_utc(AFTER_MIDNIGHT):
            self.assertIn(hw_id, self._visible_ids())
        # 20:30 UTC on D-1 is 23:30 on D-1 in Baghdad: still hidden.
        with frozen_utc(BEFORE_MIDNIGHT):
            self.assertNotIn(hw_id, self._visible_ids())

    def test_school_timezone_is_read_and_falls_back_to_baghdad(self):
        with self.app.app_context():
            sid = self.ids['school_a']
            with frozen_utc(AFTER_MIDNIGHT):
                self.assertEqual(school_local_today(sid), D)
                for tz, expected in (('UTC', D - timedelta(days=1)),
                                     (None, D), ('Not/AZone', D)):
                    with self.subTest(tz=tz):
                        db.session.execute(School.__table__.update()
                                           .where(School.id == sid).values(timezone=tz))
                        self.assertEqual(school_local_today(sid), expected)
            db.session.rollback()

    def test_institute_list_uses_the_same_rule(self):
        today_id = self._hw(D, school='i', group=True)
        future_id = self._hw(D + timedelta(days=1), school='i', group=True)
        with frozen_utc(AFTER_MIDNIGHT):
            items = self._list('pi', 'ci').get_json()['homework']
        self.assertEqual([h['id'] for h in items], [today_id])
        self.assertNotIn(future_id, [h['id'] for h in items])
        # institute item contract unchanged: school keys + group_id/group_name
        self.assertEqual(set(items[0]), PARENT_ITEM_KEYS | {'group_id', 'group_name'})

    # ── 2. contract + isolation ──────────────────────────────────────────────

    def test_20_23_response_keys_and_ordering_unchanged(self):
        a = self._hw(D - timedelta(days=2))
        b = self._hw(D)
        c = self._hw(D)
        with frozen_utc(NOON_D):
            resp = self._list()
        body = resp.get_json()
        self.assertEqual(set(body), LEGACY_KEYS)
        for item in body['homework']:
            self.assertEqual(set(item) - {'images'}, PARENT_ITEM_KEYS)
        # publish_date desc, id desc
        self.assertEqual([h['id'] for h in body['homework']], [c, b, a])

    def test_24_isolation_unchanged(self):
        x_id = self._hw(D)
        with frozen_utc(NOON_D):
            # another school's homework is never listed
            with self.app.app_context():
                b_ids = {h.id for h in Homework.query.execution_options(**OPTS)
                         .filter_by(school_id=self.ids['school_b']).all()}
            self.assertFalse(b_ids & set(self._visible_ids()))
            # a parent cannot read an unlinked child, nor another school's child
            self.assertEqual(self._list('p_y', 'c1').status_code, 404)
            self.assertEqual(self._list('pb', 'c1').status_code, 404)
            # a child in another section does not see section X homework
            self.assertNotIn(x_id, self._visible_ids('p_y', 'c3'))

    # ── 3. badge ─────────────────────────────────────────────────────────────

    def test_05_badge_excludes_unpublished_homework(self):
        last_view = datetime(2026, 10, 30, 12, 0)          # 15:00 Baghdad, D-1
        self._viewed(last_view)
        self._hw(D - timedelta(days=1), created_at=last_view + timedelta(hours=1))   # new
        self._hw(D + timedelta(days=1), created_at=last_view + timedelta(hours=1))   # future
        self._hw(D - timedelta(days=2), created_at=last_view - timedelta(days=1))    # seen
        with frozen_utc(NOON_D):
            # p1 has two children in X: the homework is still counted once.
            self.assertEqual(self._badge(), 1)

    def test_06_badge_counts_homework_once_it_publishes(self):
        last_view = datetime(2026, 10, 30, 12, 0)          # local date D-1
        # Created BEFORE the last view, scheduled for D: unseen at last view.
        self._hw(D, created_at=last_view - timedelta(days=1))
        self._viewed(last_view)
        with frozen_utc(datetime(2026, 10, 30, 13, 0)):    # still D-1 locally
            self.assertEqual(self._badge(), 0)
        with frozen_utc(NOON_D):                           # D: now published
            self.assertEqual(self._badge(), 1)

    def test_badge_last_view_is_compared_in_local_time(self):
        hw_created = datetime(2026, 10, 30, 10, 0)
        self._hw(D, created_at=hw_created)
        # 21:10 UTC on D-1 = 00:10 on D in Baghdad: the parent viewed the list
        # AFTER the homework was already published, so it is not new.
        self._viewed(datetime(2026, 10, 30, 21, 10))
        with frozen_utc(NOON_D):
            self.assertEqual(self._badge(), 0)

    def test_badge_midnight_boundary(self):
        self._viewed(datetime(2026, 10, 30, 12, 0))
        self._hw(D, created_at=datetime(2026, 10, 30, 13, 0))
        with frozen_utc(BEFORE_MIDNIGHT):
            self.assertEqual(self._badge(), 0)
        with frozen_utc(AFTER_MIDNIGHT):
            self.assertEqual(self._badge(), 1)

    # ── 4. notification recipients and payload ───────────────────────────────

    def _assert_school_x_delivery(self, sent, hw_id):
        # p1 (c1+c2) and p_mix (c1+c3): one push each. p_off is inactive, p_y's
        # child is in Y, p_foreign's account belongs to another school.
        self.assertEqual(sorted(p['user_id'] for p in sent),
                         sorted([self.ids['p1'], self.ids['p_mix']]))
        by_user = {p['user_id']: p for p in sent}
        for p in sent:
            self.assertEqual(set(p['data']), PUSH_KEYS)
            self.assertEqual(p['data']['type'], 'homework')
            self.assertEqual(p['data']['ntype'], 'homework')
            self.assertEqual(p['data']['screen'], 'homework')
            self.assertEqual(p['data']['route'], '/parent/homework')
            self.assertEqual(p['data']['homework_id'], str(hw_id))
            self.assertEqual(p['data']['section_id'], str(self.ids['sec_x']))
            self.assertEqual(p['title'], 'واجب جديد')
            self.assertTrue(p['hw_committed'], 'push handed over before the homework commit')
            self.assertTrue(p['row_committed'], 'push handed over before its in-app row')
        self.assertEqual(by_user[self.ids['p1']]['data']['student_id'],
                         str(min(self.ids['c1'], self.ids['c2'])))
        self.assertEqual(by_user[self.ids['p_mix']]['data']['student_id'], str(self.ids['c1']))

    def test_web_create_notifies_active_same_school_parents_after_commit(self):
        title = f'Web {self.sfx}'
        client = self._web_client()
        with self._pushes() as sent:
            resp = self._web_create(client, title)
        self.assertEqual(resp.status_code, 302)
        self._assert_school_x_delivery(sent, self._hw_id(title))
        self.assertEqual(self._rows(title), {self.ids['p1']: 1, self.ids['p_mix']: 1})

    def test_mobile_create_matches_web_recipients_and_payload(self):
        title = f'Mob {self.sfx}'
        with self._pushes() as sent:
            resp = self._mobile_create(title)
        self.assertEqual(resp.status_code, 201, resp.get_data(as_text=True))
        self._assert_school_x_delivery(sent, self._hw_id(title))
        self.assertEqual(self._rows(title), {self.ids['p1']: 1, self.ids['p_mix']: 1})

    def test_16_identical_text_on_a_later_homework_is_not_suppressed(self):
        title = f'Same {self.sfx}'
        client = self._web_client()
        with self._pushes() as sent:
            self.assertEqual(self._web_create(client, title).status_code, 302)
            # same subject and title, a second homework the next day
            with self.app.app_context():
                db.session.execute(Homework.__table__.update()
                                   .where(Homework.title == title)
                                   .values(publish_date=date.today() - timedelta(days=1)))
                db.session.commit()
            self.assertEqual(self._web_create(client, title).status_code, 302)
        self.assertEqual(self._rows(title), {self.ids['p1']: 2, self.ids['p_mix']: 2})
        self.assertEqual(len(sent), 4)
        self.assertEqual(len({p['data']['homework_id'] for p in sent}), 2)

    def test_17_batch_all_sections_one_notification_per_parent(self):
        title = f'Batch {self.sfx}'
        client = self._web_client()
        with self._pushes() as sent:
            resp = client.post('/homework/create', data={
                'title': title, 'subject_id': str(self.ids['subj_a']),
                'grade_id': str(self.ids['grade']), 'section_id': 'all',
                'publish_date': date.today().isoformat(),
                'due_date': (date.today() + timedelta(days=2)).isoformat()})
        self.assertEqual(resp.status_code, 302)
        # p_mix has a child in X and one in Y: still ONE notification.
        self.assertEqual(self._rows(title), {self.ids['p1']: 1, self.ids['p_mix']: 1,
                                             self.ids['p_y']: 1})
        self.assertEqual(sorted(p['user_id'] for p in sent),
                         sorted([self.ids['p1'], self.ids['p_mix'], self.ids['p_y']]))

    def test_18_19_foreign_and_inactive_parents_never_notified(self):
        client = self._web_client()
        with self._pushes() as sent:
            self._web_create(client, f'W {self.sfx}')
            self._mobile_create(f'M {self.sfx}')
        excluded = {self.ids['p_off'], self.ids['p_foreign'], self.ids['pb']}
        self.assertFalse(excluded & {p['user_id'] for p in sent})
        self.assertFalse(excluded & set(self._rows()))

    def test_24_teacher_section_guard_unchanged(self):
        with self._pushes() as sent:
            resp = self._mobile_create(f'Forbidden {self.sfx}', section='sec_y')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(sent, [])
        self.assertEqual(self._rows(), {})


if __name__ == '__main__':
    unittest.main()

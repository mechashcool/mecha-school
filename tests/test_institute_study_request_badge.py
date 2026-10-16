"""Institute study requests ("أرسل درسي") — static sidebar pending badge.

The badge is computed in inject_globals with ONE bounded id query (no COUNT,
no image table, no Student/User join — only a PK EXISTS that hides tombstoned
students, like the inbox) and only for a selected institute, the
manage_leave_requests permission, and never a teacher. No polling.
"""
import re
import unittest
from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import event

from app import STUDY_REQUEST_BADGE_CAP, create_app
from app.models import (
    db, AcademicYear, AuditLog, InstituteStudyRequest, Permission, Role, School,
    Student, User, parent_students,
)
from app.utils.school_cleanup import cleanup_school_cascade

OPTS = {'bypass_tenant_scope': True, 'include_all_years': True}
PASSWORD = 'Password123!'
PAGE = '/auth/profile'                      # any page that renders the sidebar
LIST = '/admin/institute-study-requests'
REQ_TABLE = 'institute_study_requests'
IMG_TABLE = 'institute_study_request_images'
LINK = re.compile(r'<a href="/admin/institute-study-requests"[^>]*>(.*?)</a>', re.S)
BADGE = re.compile(r'<span class="nav-badge">\s*(.*?)\s*</span>', re.S)


class StudyRequestBadgeTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config.update(RATELIMIT_ENABLED=False, WTF_CSRF_ENABLED=False,
                              PRIVATE_UPLOADS_ENABLED=False)
        with cls.app.app_context():
            cls.roles = {r.name: r.id for r in Role.query.all()}
            cls.perm_id = Permission.query.filter_by(name='manage_leave_requests').one().id

    # ── fixture ───────────────────────────────────────────────────────────────

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        with self.app.app_context():
            perm = db.session.get(Permission, self.perm_id)
            with_perm = Role(name=f'isb_leave_{self.sfx}', label='isb leave')
            with_perm.permissions.append(perm)
            no_perm = Role(name=f'isb_noperm_{self.sfx}', label='isb noperm')
            db.session.add_all([with_perm, no_perm])
            db.session.flush()
            self.ids.update(role_leave=with_perm.id, role_noperm=no_perm.id)
            self._school('a', institute=True)
            self._school('b', institute=True)
            self._school('c', institute=False)
            old = AcademicYear(school_id=self.ids['school_a'], name=f'old{self.sfx}',
                               is_current=False,
                               start_date=date(2025, 8, 1), end_date=date(2026, 7, 31))
            db.session.add(old)
            db.session.flush()
            self.ids['old_year_a'] = old.id
            self.ids['super'] = self._user('sup', 'super_admin', None).id
            teacher = db.session.get(User, self.ids['teacher_a'])
            teacher.extra_permissions.append(perm)      # granted, still denied
            db.session.commit()

    def _user(self, tag, role, school_id, role_id=None):
        u = User(username=f'isb{tag}_{self.sfx}', email=f'isb{tag}_{self.sfx}@t.test',
                 full_name=f'isb {tag}', role_id=role_id or self.roles[role],
                 school_id=school_id, is_active=True)
        u.set_password(PASSWORD)
        db.session.add(u)
        db.session.flush()
        return u

    def _school(self, key, *, institute):
        s = self.sfx
        school = School(school_name=f'ISB {key} {s}', code=f'ISB{key}{s}'[:20],
                        capacity=0, is_active=True,
                        institution_type='institute' if institute else 'school')
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'Y{key}{s}', is_current=True,
                            start_date=date(2026, 8, 1), end_date=date(2027, 7, 31))
        db.session.add(year)
        db.session.flush()
        sid = school.id
        self.ids.update({f'school_{key}': sid, f'year_{key}': year.id})
        for tag, role, role_id in (('admin', 'school_admin', None),
                                   ('parent', 'parent', None),
                                   ('teacher', 'teacher', None),
                                   ('staff_leave', None, self.ids['role_leave']),
                                   ('staff_noperm', None, self.ids['role_noperm'])):
            self.ids[f'{tag}_{key}'] = self._user(f"{tag.replace('_', '')}{key}", role, sid, role_id).id
        st = Student(student_id=f'IB{uuid4().hex[:10]}', full_name=f'Child {key} {s}',
                     school_id=sid, academic_year_id=year.id, status='active')
        db.session.add(st)
        db.session.flush()
        db.session.execute(parent_students.insert().values(
            user_id=self.ids[f'parent_{key}'], student_id=st.id))
        self.ids[f'child_{key}'] = st.id

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for key in ('a', 'b', 'c'):
                cleanup_school_cascade(self.ids[f'school_{key}'])
            # Super-admin logins write school-less audit rows.
            AuditLog.query.execution_options(**OPTS).filter(
                AuditLog.user_id == self.ids['super']).delete(synchronize_session=False)
            User.query.execution_options(**OPTS).filter_by(
                id=self.ids['super']).delete(synchronize_session=False)
            for rid in (self.ids['role_leave'], self.ids['role_noperm']):
                role = db.session.get(Role, rid)
                if role is not None:
                    db.session.delete(role)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _login(self, key, school=None):
        client = self.app.test_client()
        with self.app.app_context():
            name = db.session.get(User, self.ids[key], execution_options=OPTS).username
        resp = client.post('/auth/login', data={'username': name, 'password': PASSWORD})
        self.assertEqual(resp.status_code, 302)
        if school:
            with client.session_transaction() as sess:
                sess['active_school_id'] = self.ids[f'school_{school}']
        return client

    def _insert(self, key='a', count=1, status='pending', year=None, student=None):
        with self.app.app_context():
            rows = [InstituteStudyRequest(
                school_id=self.ids[f'school_{key}'],
                academic_year_id=year or self.ids[f'year_{key}'],
                student_id=student or self.ids[f'child_{key}'],
                parent_id=self.ids[f'parent_{key}'],
                status=status, image_count=0, created_at=datetime.utcnow())
                for _ in range(count)]
            db.session.add_all(rows)
            db.session.commit()
            return [r.id for r in rows]

    def _capture_sql(self):
        statements = []
        with self.app.app_context():
            engine = db.engine

        def _cap(conn, cursor, statement, parameters, *a):
            statements.append((statement, parameters))
        event.listen(engine, 'before_cursor_execute', _cap)
        self.addCleanup(event.remove, engine, 'before_cursor_execute', _cap)
        return statements

    def _render(self, client):
        """(link present, badge text or None, badge statements) for one render."""
        statements = self._capture_sql()
        resp = client.get(PAGE)
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        link = LINK.search(html)
        badge = BADGE.search(link.group(1)) if link else None
        badge_sql = [(st, p) for st, p in statements if f'FROM {REQ_TABLE}' in st]
        return bool(link), (badge.group(1) if badge else None), badge_sql

    def _badge(self, key='admin_a', school=None):
        present, badge, badge_sql = self._render(self._login(key, school))
        self.assertTrue(present)
        self.assertEqual(len(badge_sql), 1)             # exactly one badge query
        return badge

    # ── 1-4: value ───────────────────────────────────────────────────────────

    def test_01_zero_pending_no_badge(self):
        self._insert(status='viewed')
        self.assertIsNone(self._badge())

    def test_02_one_pending(self):
        self._insert()
        self.assertEqual(self._badge(), '1')

    def test_03_multiple_pending_and_permitted_staff(self):
        self._insert(count=7)
        self.assertEqual(self._badge('admin_a'), '7')
        self.assertEqual(self._badge('staff_leave_a'), '7')

    def test_04_viewed_not_counted(self):
        self._insert(count=2)
        self._insert(count=3, status='viewed')
        self.assertEqual(self._badge(), '2')

    # ── 5-7: isolation, years, cap ───────────────────────────────────────────

    def test_05_other_school_not_counted(self):
        self._insert(key='b', count=4)
        self._insert(count=1)
        self.assertEqual(self._badge('admin_a'), '1')
        self.assertEqual(self._badge('admin_b'), '4')
        # Super admin: the item is not in the super-admin nav, so no query —
        # with no selected school or with a selected institute.
        for school in (None, 'b'):
            with self.subTest(super_admin_school=school):
                present, badge, badge_sql = self._render(self._login('super', school))
                self.assertFalse(present)
                self.assertEqual(badge_sql, [])

    def test_06_old_academic_year_included(self):
        self._insert(year=self.ids['old_year_a'])
        self._insert()
        self.assertEqual(self._badge(), '2')

    def test_07_cap_shows_99_plus_and_reads_at_most_cap_rows(self):
        self._insert(count=99)
        self.assertEqual(self._badge(), '99')
        self._insert(count=6)                       # 105 pending
        present, badge, badge_sql = self._render(self._login('admin_a'))
        self.assertEqual(badge, '99+')
        (statement, params), = badge_sql
        self.assertIn('LIMIT', statement.upper())
        values = params.values() if isinstance(params, dict) else params
        self.assertIn(STUDY_REQUEST_BADGE_CAP, list(values))
        self.assertEqual(STUDY_REQUEST_BADGE_CAP, 100)

    # ── 8-10: the query never runs for ineligible users ──────────────────────

    def test_08_09_10_no_query_for_school_teacher_unauthorized(self):
        self._insert(count=3)
        for key in ('admin_c', 'teacher_a', 'staff_noperm_a'):
            with self.subTest(user=key):
                present, badge, badge_sql = self._render(self._login(key))
                self.assertFalse(present)
                self.assertIsNone(badge)
                self.assertEqual(badge_sql, [])

    # ── 11: query shape ──────────────────────────────────────────────────────

    def test_11_query_shape_no_images_no_joins_no_count(self):
        self._insert(count=2)
        present, badge, badge_sql = self._render(self._login('admin_a'))
        self.assertEqual(badge, '2')
        (statement, _), = badge_sql
        lowered = statement.lower()
        self.assertNotIn(IMG_TABLE, lowered)
        self.assertNotIn('join', lowered)
        self.assertNotIn('count(', lowered)
        self.assertNotIn('users', lowered)
        self.assertNotIn('note', lowered)
        # students is reached only through the tombstone EXISTS, never selected.
        self.assertIn('exists', lowered)
        selected = re.split(r'\s+from\s+', lowered, maxsplit=1)[0]
        self.assertRegex(' '.join(selected.split()),
                         rf'^select {REQ_TABLE}\.id( as \w+)?$')     # id only

    def test_11b_tombstoned_student_not_counted(self):
        with self.app.app_context():
            gone = Student(student_id=f'IB{uuid4().hex[:10]}', full_name='gone',
                           school_id=self.ids['school_a'],
                           academic_year_id=self.ids['year_a'], status='active')
            db.session.add(gone)
            db.session.commit()
            gone_id = gone.id
        self._insert(student=gone_id, count=2)
        self._insert()
        with self.app.app_context():
            db.session.get(Student, gone_id, execution_options=OPTS).status = 'deleted'
            db.session.commit()
        self.assertEqual(self._badge(), '1')          # matches the inbox

    # ── 12: viewed flow ──────────────────────────────────────────────────────

    def test_12_marking_viewed_reduces_next_render(self):
        first, _ = self._insert(count=2)
        client = self._login('admin_a')
        self.assertEqual(self._render(client)[1], '2')
        resp = client.post(f'{LIST}/{first}', data={'admin_note': ''})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._render(client)[1], '1')


if __name__ == '__main__':
    unittest.main()

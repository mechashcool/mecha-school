"""Institute study requests ("أرسل درسي") — P3 administration web flow.

GET  /admin/institute-study-requests            list
GET  /admin/institute-study-requests/<id>       detail
POST /admin/institute-study-requests/<id>       mark viewed / admin note

No storage is touched: image rows hold metadata strings only.
"""
import glob
import os
import re
import unittest
from datetime import date, datetime, timedelta
from unittest import mock
from uuid import uuid4

from sqlalchemy import event

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (
    db, AcademicYear, AuditLog, InstituteStudyRequest, InstituteStudyRequestImage, Notification,
    Permission, Role, School, Student, User, parent_students,
)
from app.utils.school_cleanup import cleanup_school_cascade
from app.utils.upload_access import can_access_upload

OPTS = {'bypass_tenant_scope': True, 'include_all_years': True}
PASSWORD = 'Password123!'
LIST = '/admin/institute-study-requests'
IMG_TABLE = 'institute_study_request_images'
REQ_TABLE = 'institute_study_requests'
ROW_LINK = re.compile(r'/admin/institute-study-requests/(\d+)"')


class StudyRequestAdminTest(unittest.TestCase):

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
            with_perm = Role(name=f'isa_leave_{self.sfx}', label='isa leave')
            with_perm.permissions.append(perm)
            no_perm = Role(name=f'isa_noperm_{self.sfx}', label='isa noperm')
            db.session.add_all([with_perm, no_perm])
            db.session.flush()
            self.ids.update(role_leave=with_perm.id, role_noperm=no_perm.id)
            self._school('a', institute=True)
            self._school('b', institute=True)
            self._school('c', institute=False)
            self.ids['super'] = self._user('sup', 'super_admin', None).id
            teacher = db.session.get(User, self.ids['teacher_a'])
            teacher.extra_permissions.append(perm)      # granted, still denied
            db.session.commit()

    def _user(self, tag, role, school_id, role_id=None):
        u = User(username=f'isa{tag}_{self.sfx}', email=f'isa{tag}_{self.sfx}@t.test',
                 full_name=f'isa {tag}', role_id=role_id or self.roles[role],
                 school_id=school_id, is_active=True)
        u.set_password(PASSWORD)
        db.session.add(u)
        db.session.flush()
        return u

    def _school(self, key, *, institute):
        s = self.sfx
        school = School(school_name=f'ISA {key} {s}', code=f'ISA{key}{s}'[:20],
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
        st = Student(student_id=f'IA{uuid4().hex[:10]}', full_name=f'Child {key} {s}',
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

    def _insert(self, key='a', n=0, status='pending', when=None, note=None):
        with self.app.app_context():
            req = InstituteStudyRequest(
                school_id=self.ids[f'school_{key}'], academic_year_id=self.ids[f'year_{key}'],
                student_id=self.ids[f'child_{key}'], parent_id=self.ids[f'parent_{key}'],
                status=status, image_count=n, note=note,
                created_at=when or datetime.utcnow())
            db.session.add(req)
            db.session.flush()
            for i in range(n):          # inserted in reverse display order
                db.session.add(InstituteStudyRequestImage(
                    request_id=req.id, school_id=req.school_id, mime='image/webp',
                    sort_order=n - 1 - i,
                    file_path=f'https://isr.invalid/storage/v1/object/public/uploads/'
                              f'x/{uuid4().hex}.webp'))
            db.session.commit()
            return req.id

    def _row(self, rid):
        with self.app.app_context():
            return db.session.get(InstituteStudyRequest, rid, execution_options=OPTS)

    def _ids_on_page(self, resp):
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:300])
        return [int(x) for x in ROW_LINK.findall(resp.get_data(as_text=True))]

    def _capture_sql(self):
        statements = []
        with self.app.app_context():
            engine = db.engine

        def _cap(conn, cursor, statement, *a):
            statements.append(statement)
        event.listen(engine, 'before_cursor_execute', _cap)
        self.addCleanup(event.remove, engine, 'before_cursor_execute', _cap)
        return statements

    # ── 1-7: access ──────────────────────────────────────────────────────────

    def test_01_normal_school_forbidden(self):
        client = self._login('admin_c')
        self.assertEqual(client.get(LIST).status_code, 403)

    def test_02_teacher_denied_even_with_permission(self):
        rid = self._insert(n=1)
        client = self._login('teacher_a')
        self.assertEqual(client.get(LIST).status_code, 403)
        self.assertEqual(client.get(f'{LIST}/{rid}').status_code, 403)
        self.assertEqual(client.post(f'{LIST}/{rid}', data={'admin_note': 'x'}).status_code, 403)
        self.assertEqual(self._row(rid).status, 'pending')

    def test_03_admin_and_permitted_staff_allowed(self):
        rid = self._insert()
        for key in ('admin_a', 'staff_leave_a'):
            with self.subTest(user=key):
                self.assertIn(rid, self._ids_on_page(self._login(key).get(LIST)))

    def test_04_staff_without_permission_denied(self):
        client = self._login('staff_noperm_a')
        self.assertEqual(client.get(LIST).status_code, 403)

    def test_05_06_super_admin_needs_selected_institute(self):
        rid = self._insert()
        resp = self._login('super').get(LIST)
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn(str(rid), resp.get_data(as_text=True))
        self.assertIn(rid, self._ids_on_page(self._login('super', school='a').get(LIST)))
        self.assertEqual(self._login('super', school='c').get(LIST).status_code, 403)

    def test_07_cross_school_id_404(self):
        other = self._insert(key='b', n=1)
        client = self._login('admin_a')
        self.assertEqual(client.get(f'{LIST}/{other}').status_code, 404)
        self.assertEqual(client.post(f'{LIST}/{other}', data={'admin_note': 'x'}).status_code, 404)
        self.assertEqual(self._row(other).status, 'pending')
        self.assertNotIn(other, self._ids_on_page(client.get(LIST, query_string={'status': 'all'})))

    # ── 8-14: list ───────────────────────────────────────────────────────────

    def test_08_09_10_status_filters(self):
        pending = self._insert()
        viewed = self._insert(status='viewed')
        client = self._login('admin_a')
        self.assertEqual(self._ids_on_page(client.get(LIST)), [pending])
        self.assertEqual(self._ids_on_page(client.get(LIST, query_string={'status': 'viewed'})),
                         [viewed])
        self.assertEqual(self._ids_on_page(client.get(LIST, query_string={'status': 'all'})),
                         [pending, viewed])                          # pending first
        for bad in ("viewed' OR 1=1 --", 'approved', ''):
            with self.subTest(status=bad):
                self.assertEqual(
                    self._ids_on_page(client.get(LIST, query_string={'status': bad})), [pending])

    def test_11_12_13_list_light_no_images_no_urls_constant_queries(self):
        for _ in range(3):
            self._insert(n=3, note='ن' * 900)
        client = self._login('admin_a')
        client.get(LIST)                                   # warm per-worker caches
        url_fn = mock.Mock(return_value='/x')
        with mock.patch.dict(self.app.jinja_env.globals, {'upload_url': url_fn}):
            statements = self._capture_sql()
            resp = client.get(LIST)
            few = list(statements)
            for _ in range(10):
                self._insert(n=3)
            statements.clear()
            resp2 = client.get(LIST)
            many = list(statements)
        url_fn.assert_not_called()
        self.assertEqual(len(self._ids_on_page(resp)), 3)
        self.assertEqual(len(self._ids_on_page(resp2)), 13)
        for sql in (few, many):
            self.assertFalse(any(IMG_TABLE in st for st in sql))
            # The list query + the bounded sidebar badge query (LIMIT, ids only).
            self.assertEqual(sum(f'FROM {REQ_TABLE}' in st and 'LIMIT' in st for st in sql), 2)
            self.assertEqual(sum(f'FROM {REQ_TABLE}' in st and 'JOIN' in st for st in sql), 1)
            self.assertFalse(any('count(' in st.lower() and REQ_TABLE in st for st in sql))
        self.assertEqual(len(few), len(many))              # not N+1
        self.assertNotIn('ن' * 81, resp.get_data(as_text=True))   # note preview only

    def test_14_pagination_without_count(self):
        base = datetime(2026, 9, 1, 8, 0)
        ids = [self._insert(when=base + timedelta(minutes=i)) for i in range(5)]
        newest = list(reversed(ids))
        client = self._login('admin_a')
        statements = self._capture_sql()
        p1 = client.get(LIST, query_string={'per_page': 2})
        self.assertEqual(self._ids_on_page(p1), newest[:2])
        self.assertIn('page=2', p1.get_data(as_text=True))
        p3 = client.get(LIST, query_string={'per_page': 2, 'page': 3})
        self.assertEqual(self._ids_on_page(p3), newest[4:])
        self.assertNotIn('page=4', p3.get_data(as_text=True))
        self.assertFalse(any('count(' in st.lower() and REQ_TABLE in st for st in statements))
        capped = client.get(LIST, query_string={'per_page': 500})
        self.assertEqual(len(self._ids_on_page(capped)), 5)

    # ── 15-17: detail ────────────────────────────────────────────────────────

    def test_15_16_detail_only_its_images_in_order(self):
        rid = self._insert(n=3)
        other = self._insert(n=2)
        with self.app.app_context():
            ordered = [p for (p,) in db.session.query(InstituteStudyRequestImage.file_path)
                       .filter_by(request_id=rid)
                       .order_by(InstituteStudyRequestImage.sort_order,
                                 InstituteStudyRequestImage.id)]
            foreign = [p for (p,) in db.session.query(InstituteStudyRequestImage.file_path)
                       .filter_by(request_id=other)]
        html = self._login('admin_a').get(f'{LIST}/{rid}').get_data(as_text=True)
        positions = [html.index(p) for p in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertFalse(any(p in html for p in foreign))
        # Private uploads ON → protected proxy URLs only, never the raw object URL.
        self.app.config['PRIVATE_UPLOADS_ENABLED'] = True
        try:
            html = self._login('admin_a').get(f'{LIST}/{rid}').get_data(as_text=True)
        finally:
            self.app.config['PRIVATE_UPLOADS_ENABLED'] = False
        self.assertEqual(html.count('/media-proxy/'), 6)              # link + img × 3
        self.assertNotIn('isr.invalid', html)

    def test_17_teacher_cannot_reach_attachment(self):
        rid = self._insert(n=1)
        with self.app.app_context():
            path = db.session.query(InstituteStudyRequestImage.file_path).filter_by(
                request_id=rid).scalar()
            teacher = db.session.get(User, self.ids['teacher_a'], execution_options=OPTS)
            noperm = db.session.get(User, self.ids['staff_noperm_a'], execution_options=OPTS)
            admin = db.session.get(User, self.ids['admin_a'], execution_options=OPTS)
            self.assertFalse(can_access_upload(teacher, path))
            self.assertFalse(can_access_upload(noperm, path))
            self.assertTrue(can_access_upload(admin, path))
        self.assertEqual(self._login('teacher_a').get(f'{LIST}/{rid}').status_code, 403)

    # ── 18-25: mark viewed / admin note ──────────────────────────────────────

    def test_18_21_mark_viewed_with_note(self):
        rid = self._insert(n=1)
        before = datetime.utcnow() - timedelta(seconds=5)
        statements = self._capture_sql()
        resp = self._login('staff_leave_a').post(
            f'{LIST}/{rid}', data={'admin_note': '  سيتم شرح هذا الموضوع في المحاضرة القادمة  '})
        self.assertEqual(resp.status_code, 302)
        row = self._row(rid)
        self.assertEqual(row.status, 'viewed')
        self.assertEqual(row.viewed_by, self.ids['staff_leave_a'])
        self.assertTrue(before <= row.viewed_at <= datetime.utcnow() + timedelta(seconds=5))
        self.assertEqual(row.admin_note, 'سيتم شرح هذا الموضوع في المحاضرة القادمة')
        updates = [st for st in statements if st.lstrip().upper().startswith('UPDATE')
                   and REQ_TABLE in st]
        self.assertEqual(len(updates), 1)

    def test_22_note_over_1000_rejected(self):
        rid = self._insert()
        resp = self._login('admin_a').post(f'{LIST}/{rid}', data={'admin_note': 'ن' * 1001})
        self.assertEqual(resp.status_code, 302)
        row = self._row(rid)
        self.assertEqual(row.status, 'pending')
        self.assertIsNone(row.admin_note)

    def test_23_empty_note_becomes_null(self):
        rid = self._insert()
        self._login('admin_a').post(f'{LIST}/{rid}', data={'admin_note': '   '})
        row = self._row(rid)
        self.assertEqual(row.status, 'viewed')
        self.assertIsNone(row.admin_note)

    def test_24_25_unchanged_viewed_no_write_and_later_edit(self):
        rid = self._insert()
        admin = self._login('admin_a')
        admin.post(f'{LIST}/{rid}', data={'admin_note': 'أولى'})
        first = self._row(rid)
        statements = self._capture_sql()
        resp = admin.post(f'{LIST}/{rid}', data={'admin_note': ' أولى '})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(any(st.lstrip().upper().startswith('UPDATE') and REQ_TABLE in st
                             for st in statements))
        self.assertEqual(self._row(rid).updated_at, first.updated_at)
        # A later edit (another administrator) changes only the note; the first
        # viewer and time are kept.
        self._login('staff_leave_a').post(f'{LIST}/{rid}', data={'admin_note': 'ثانية'})
        row = self._row(rid)
        self.assertEqual(row.admin_note, 'ثانية')
        self.assertEqual(row.viewed_by, self.ids['admin_a'])
        self.assertEqual(row.viewed_at, first.viewed_at)

    # ── 26-28: parent visibility, no notifications, no migration ─────────────

    def test_26_27_parent_sees_result_and_no_notification(self):
        rid = self._insert(n=1)
        with self.app.app_context():
            notes_before = (Notification.query.execution_options(**OPTS)
                            .filter_by(school_id=self.ids['school_a']).count())
        self._login('admin_a').post(f'{LIST}/{rid}', data={'admin_note': 'تمت المراجعة'})
        with self.app.app_context():
            parent = db.session.get(User, self.ids['parent_a'], execution_options=OPTS)
            token = encode_token(parent)
            notes_after = (Notification.query.execution_options(**OPTS)
                           .filter_by(school_id=self.ids['school_a']).count())
        resp = self.app.test_client().get(
            f"/api/mobile/v1/parent/children/{self.ids['child_a']}/study-requests/{rid}",
            headers={'Authorization': f'Bearer {token}'})
        body = resp.get_json()['request']
        self.assertEqual(body['status'], 'viewed')
        self.assertEqual(body['admin_note'], 'تمت المراجعة')
        self.assertIsNotNone(body['viewed_at'])
        self.assertFalse(body['can_delete'])
        self.assertEqual(notes_after, notes_before)

    def test_28_no_new_migration(self):
        versions = os.path.join(os.path.dirname(__file__), '..', 'migrations', 'versions')
        revs, downs = set(), set()
        for path in glob.glob(os.path.join(versions, '*.py')):
            src = open(path, encoding='utf-8').read()
            rev = re.search(r"^revision\s*=\s*['\"]([^'\"]+)", src, re.M)
            down = re.search(r"^down_revision\s*=\s*(.+)$", src, re.M)
            if rev:
                revs.add(rev.group(1))
            if down:
                downs.update(re.findall(r"['\"]([^'\"]+)['\"]", down.group(1)))
        self.assertEqual(revs - downs, {'s6t7r8q9i1a2'})

    def test_29_sidebar_link_only_for_institute_administration(self):
        link = 'href="/admin/institute-study-requests"'
        self.assertIn(link, self._login('admin_a').get(LIST).get_data(as_text=True))
        self.assertNotIn(link, self._login('admin_c').get('/admin/', follow_redirects=True).get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()

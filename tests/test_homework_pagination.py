"""
Homework list pagination — web index (20 per page) and the OPTIONAL
limit/offset pagination of GET /api/mobile/v1/parent/children/<id>/homework.

  * web: page 1 / 2 / 3 slices, ordering (publish_date desc, id desc) kept
    across pages, filters carried into page links, out-of-range page falls
    back to the last page, LIMIT/OFFSET issued in SQL;
  * web template: every homework image has loading="lazy" decoding="async";
  * mobile legacy request (no limit/offset): response schema and full row set
    exactly as before, no pagination metadata;
  * mobile paginated request: requested slice, same ordering, total/limit/
    offset metadata, max 50, LIMIT/OFFSET issued in SQL;
  * isolation: another school's homework never listed; a parent cannot page
    through an unlinked child's homework.

Storage is never touched (rows are created directly); no network.
"""
import re
import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from uuid import uuid4

from sqlalchemy import event

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (db, AcademicYear, AuditLog, Employee, Grade, Homework,
                        Role, School, Section, Student, Subject, User, parent_students)

PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True}
N_HW = 45
LEGACY_KEYS = {'ok', 'student_id', 'section', 'count', 'homework'}
PARENT_ITEM_KEYS = {'id', 'homework_id', 'title', 'subject', 'subject_name',
                    'teacher_name', 'grade_name', 'section_name', 'assigned_at',
                    'publish_date', 'due_date', 'description', 'status',
                    'attachment_url', 'attachment_type', 'file_name', 'file_size',
                    'is_pdf', 'submitted_status'}
TITLE_RE = re.compile(r'<h6 class="card-title[^>]*>([^<]+)</h6>')
IMG_RE = re.compile(r'<img [^>]*alt="مرفق"[^>]*>')


class HomeworkPaginationTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        with cls.app.app_context():
            cls.role_ids = {n: Role.query.filter_by(name=n).first().id
                            for n in ('school_admin', 'parent', 'teacher')}

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        with self.app.app_context():
            for key in ('a', 'b'):
                self._school(key)
            db.session.commit()

    def _school(self, key):
        s = self.sfx
        school = School(school_name=f'HW Pg {key} {s}', code=f'HP{key}{s}'[:20],
                        capacity=0, is_active=True)
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'Y {key} {s}', is_current=True,
                            start_date=date(2026, 8, 1), end_date=date(2027, 6, 30))
        db.session.add(year)
        db.session.flush()
        grade = Grade(school_id=school.id, academic_year_id=year.id, name=f'G{key}{s[:4]}')
        db.session.add(grade)
        db.session.flush()
        sec = Section(school_id=school.id, academic_year_id=year.id, grade_id=grade.id,
                      name=f'A{s[:4]}', capacity=30)
        subj = Subject(school_id=school.id, academic_year_id=year.id, name=f'Math {key}',
                       code=f'P{key}{s[:6]}')
        db.session.add_all([sec, subj])
        db.session.flush()

        def user(label, role):
            u = User(username=f'hp{label}{key}_{s}', email=f'hp{label}{key}_{s}@example.test',
                     full_name=f'{label} {key} {s}', role_id=self.role_ids[role],
                     school_id=school.id, is_active=True)
            u.set_password(PASSWORD)
            db.session.add(u)
            db.session.flush()
            return u

        admin, parent, teacher = user('adm', 'school_admin'), user('par', 'parent'), user('t', 'teacher')
        emp = Employee(school_id=school.id, employee_id=f'P{key}{s}', full_name=f'T {key} {s}',
                       base_salary=0, status='active', user_id=teacher.id)
        db.session.add(emp)
        db.session.flush()
        child = Student(student_id=f'S-{uuid4().hex[:10]}', full_name=f'Child {key}',
                        school_id=school.id, academic_year_id=year.id,
                        section_id=sec.id, status='active')
        db.session.add(child)
        db.session.flush()
        db.session.execute(parent_students.insert().values(
            user_id=parent.id, student_id=child.id, relation='guardian'))
        n = N_HW if key == 'a' else 3
        for i in range(n):
            image = i % 4 == 0
            db.session.add(Homework(
                school_id=school.id, academic_year_id=year.id, teacher_id=emp.id,
                subject_id=subj.id, section_id=sec.id, title=f'HW-{key}-{s}-{i:03d}',
                # three rows per date: the id tie-break is exercised too
                publish_date=date.today() - timedelta(days=i // 3),
                due_date=date.today() + timedelta(days=7), is_active=True,
                attachment_path=(f'https://storage.test/uploads/homework/{s}-{i}.webp'
                                 if image else None),
                attachment_type='image' if image else None))
        db.session.flush()
        self.ids.update({f'school_{key}': school.id, f'subj_{key}': subj.id,
                         f'child_{key}': child.id, f'admin_{key}': admin.id,
                         f'parent_{key}': parent.id})

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            for key in ('a', 'b'):
                sid = self.ids[f'school_{key}']
                uids = [u.id for u in User.query.execution_options(**OPTS)
                        .filter_by(school_id=sid).all()]
                if uids:
                    AuditLog.query.execution_options(**OPTS).filter(
                        AuditLog.user_id.in_(uids)).delete(synchronize_session=False)
                    db.session.execute(parent_students.delete().where(
                        parent_students.c.user_id.in_(uids)))
                for model in (AuditLog, Homework, Student, Section, Subject, Grade,
                              Employee, User, AcademicYear):
                    model.query.execution_options(**OPTS).filter_by(
                        school_id=sid).delete(synchronize_session=False)
                School.query.filter_by(id=sid).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _expected(self, key='a'):
        """Titles in the endpoint's documented order: publish_date desc, id desc."""
        with self.app.app_context():
            rows = (Homework.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids[f'school_{key}'])
                    .order_by(Homework.publish_date.desc(), Homework.id.desc()).all())
            return [(h.id, h.title) for h in rows]

    def _web(self, key='admin_a'):
        client = self.app.test_client()
        with self.app.app_context():
            name = db.session.get(User, self.ids[key], execution_options=OPTS).username
        resp = client.post('/auth/login', data={'username': name, 'password': PASSWORD})
        self.assertIn(resp.status_code, (200, 302))
        return client

    def _jwt(self, key='parent_a'):
        with self.app.app_context():
            return {'Authorization': 'Bearer ' + encode_token(
                db.session.get(User, self.ids[key], execution_options=OPTS))}

    def _parent_list(self, query='', key='parent_a', child='child_a'):
        return self.app.test_client().get(
            f"/api/mobile/v1/parent/children/{self.ids[child]}/homework{query}",
            headers=self._jwt(key))

    @contextmanager
    def _sql(self):
        """Record every SQL statement sent to the database."""
        seen = []
        with self.app.app_context():
            engine = db.engine

        def _rec(conn, cursor, statement, *args):
            seen.append(statement)
        event.listen(engine, 'before_cursor_execute', _rec)
        try:
            yield seen
        finally:
            event.remove(engine, 'before_cursor_execute', _rec)

    def _homework_selects(self, seen):
        """The homework ROW fetches (the separate count(*) query excluded)."""
        return [s for s in seen if 'FROM homework' in s and 'homework.title' in s
                and 'count(*)' not in s]

    # ── 1. web pagination ────────────────────────────────────────────────────

    def test_web_pages_of_20_in_order_and_in_sql(self):
        client = self._web()
        titles = []
        with self._sql() as seen:
            for page, size in ((1, 20), (2, 20), (3, 5)):
                html = client.get(f'/homework/?page={page}').get_data(as_text=True)
                got = TITLE_RE.findall(html)
                self.assertEqual(len(got), size, f'page {page}')
                titles += got
        self.assertEqual(titles, [t for _, t in self._expected()])     # order kept, no gap/dup
        selects = self._homework_selects(seen)
        self.assertEqual(len(selects), 3)
        for stmt in selects:
            self.assertIn('LIMIT', stmt)
        self.assertIn('OFFSET', selects[1])

    def test_web_page_links_keep_filters_and_bounds(self):
        client = self._web()
        html = client.get(f"/homework/?f_subject_id={self.ids['subj_a']}").get_data(as_text=True)
        self.assertEqual(len(TITLE_RE.findall(html)), 20)
        self.assertIn(f"f_subject_id={self.ids['subj_a']}", html.split('صفحات الواجبات')[1])
        self.assertIn('page=2', html)
        last = TITLE_RE.findall(client.get('/homework/?page=3').get_data(as_text=True))
        for bad in ('999', '0', '-4', 'abc'):
            with self.subTest(page=bad):
                resp = client.get(f'/homework/?page={bad}')
                self.assertEqual(resp.status_code, 200)
                got = TITLE_RE.findall(resp.get_data(as_text=True))
                self.assertEqual(got, last if bad == '999' else
                                 [t for _, t in self._expected()][:20])

    # ── 2. lazy images ───────────────────────────────────────────────────────

    def test_web_images_lazy_loaded(self):
        page1_ids = [i for i, _ in self._expected()[:20]]
        with self.app.app_context():
            n_images = (Homework.query.execution_options(**OPTS)
                        .filter(Homework.id.in_(page1_ids),
                                Homework.attachment_type == 'image').count())
        self.assertGreater(n_images, 0)
        html = self._web().get('/homework/?page=1').get_data(as_text=True)
        imgs = IMG_RE.findall(html)
        self.assertEqual(len(imgs), n_images)              # only this page's images
        for tag in imgs:
            self.assertIn('loading="lazy"', tag)
            self.assertIn('decoding="async"', tag)

    # ── 3. mobile legacy request unchanged ───────────────────────────────────

    def test_mobile_legacy_request_unchanged(self):
        resp = self._parent_list()
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(set(body), LEGACY_KEYS)                    # no pagination fields
        self.assertEqual(body['count'], N_HW)
        self.assertEqual([(h['id'], h['title']) for h in body['homework']], self._expected())
        for item in body['homework']:
            self.assertEqual(set(item), PARENT_ITEM_KEYS)

    # ── 4. mobile paginated request ──────────────────────────────────────────

    def test_mobile_paginated_slices_in_order_and_in_sql(self):
        expected = self._expected()
        cases = [('?limit=10&offset=10', 10, 10, expected[10:20]),
                 ('?limit=10', 10, 0, expected[:10]),
                 ('?offset=40', 20, 40, expected[40:]),            # default limit 20
                 ('?limit=999', 50, 0, expected),                  # clamped to 50
                 ('?limit=abc&offset=-3', 20, 0, expected[:20]),   # bad input → defaults
                 ('?limit=10&offset=100', 10, 100, [])]
        for query, limit, offset, rows in cases:
            with self.subTest(query):
                with self._sql() as seen:
                    body = self._parent_list(query).get_json()
                self.assertEqual(set(body), LEGACY_KEYS | {'total', 'limit', 'offset'})
                self.assertEqual((body['total'], body['limit'], body['offset'], body['count']),
                                 (N_HW, limit, offset, len(rows)))
                self.assertEqual([(h['id'], h['title']) for h in body['homework']], rows)
                (stmt,) = self._homework_selects(seen)
                self.assertIn('LIMIT', stmt)

    # ── 5. isolation ─────────────────────────────────────────────────────────

    def test_isolation_unchanged(self):
        other = {t for _, t in self._expected('b')}
        client = self._web()
        for page in (1, 2, 3):
            got = set(TITLE_RE.findall(client.get(f'/homework/?page={page}').get_data(as_text=True)))
            self.assertFalse(got & other, 'another school leaked into the web list')
        for query in ('', '?limit=10', '?limit=50&offset=0'):
            with self.subTest(query):
                resp = self._parent_list(query, child='child_b')
                self.assertIn(resp.status_code, (403, 404))
                self.assertNotIn('homework', resp.get_json() or {})
        body = self._parent_list('?limit=50').get_json()
        self.assertFalse({h['title'] for h in body['homework']} & other)

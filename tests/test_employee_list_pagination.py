"""
Employees list (/employees/) pagination.

Before the fix the footer showed "عرض 20 من 30" but the <ul> was empty on the
server-rendered page (links were only drawn by the live-search JS), so rows
21+ were unreachable. Proves:
  * page 1 = 20 rows, page 2 = remaining 10, no duplicates, union = all 30,
    stable even when created_at ties (id tie-breaker);
  * page 1 has "التالي" and no "السابق"; page 2 has "السابق" and no "التالي";
  * q / job_title / department survive the page links;
  * another school's employees never appear, on any page;
  * invalid page values follow Flask-SQLAlchemy error_out=False (non-int / <1
    → page 1, past the end → empty page, never another school's rows);
  * <= 20 employees → no page navigation; the AJAX /employees/search JSON
    pages the same way and now carries per_page.
"""
import html
import re
import unittest
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from app import create_app
from app.models import db, AcademicYear, AuditLog, Employee, Role, School, User

PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True}
JOB, DEP = 'معلم', 'رياضيات'

WRAP_RE = re.compile(r'id="emp-pagination-wrapper"\s+class="([^"]*)"')
UL_RE = re.compile(r'<ul class="[^"]*" id="emp-pagination">(.*?)</ul>', re.S)
HREF_RE = re.compile(r'<a class="page-link" href="([^"]+)">([^<]+)</a>')
INFO_RE = re.compile(r'id="emp-page-info">\s*(.*?)\s*</small>', re.S)


class EmployeeListPaginationTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['RATELIMIT_ENABLED'] = False
        with cls.app.app_context():
            cls.admin_role = Role.query.filter_by(name='school_admin').first().id

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        with self.app.app_context():
            self._school('a', 30)
            self._school('b', 5)
            db.session.commit()

    def _school(self, key, count):
        s = self.sfx
        school = School(school_name=f'EPag {key} {s}', code=f'EP{key}{s}'[:20],
                        capacity=0, is_active=True)
        db.session.add(school)
        db.session.flush()
        db.session.add(AcademicYear(school_id=school.id, name=f'Y{key}{s}', is_current=True,
                                    start_date=date(2026, 8, 1), end_date=date(2027, 6, 30)))
        admin = User(username=f'epadm{key}_{s}', email=f'epadm{key}_{s}@t.test',
                     full_name=f'adm {key}', role_id=self.admin_role,
                     school_id=school.id, is_active=True)
        admin.set_password(PASSWORD)
        db.session.add(admin)
        base = datetime(2026, 9, 1, 8, 0, 0)
        for i in range(count):
            # First 25 of school A: teachers of maths (filter target); the rest
            # accountants. Half share one created_at to exercise the tie-breaker.
            teacher = i < 25
            db.session.add(Employee(
                school_id=school.id, employee_id=f'P{key.upper()}{s}-{i:02d}',
                full_name=f'Emp {key} {i:02d}', base_salary=0, status='active',
                job_title=JOB if teacher else 'محاسب', department=DEP if teacher else 'مالية',
                created_at=base if i % 2 else base + timedelta(minutes=i)))
        db.session.flush()
        self.ids[f'school_{key}'] = school.id
        self.ids[f'admin_{key}'] = admin.username

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
                for model in (AuditLog, Employee, User, AcademicYear):
                    model.query.execution_options(**OPTS).filter_by(
                        school_id=sid).delete(synchronize_session=False)
                School.query.filter_by(id=sid).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _web(self, key='a'):
        client = self.app.test_client()
        resp = client.post('/auth/login', data={'username': self.ids[f'admin_{key}'],
                                                'password': PASSWORD})
        self.assertEqual(resp.status_code, 302)
        return client

    def _get(self, client, url):
        resp = client.get(url)
        self.assertEqual(resp.status_code, 200, url)
        return resp.get_data(as_text=True)

    def _rows(self, page_html):
        return re.findall(rf'<code class="text-primary-custom fw-600">(P[AB]{self.sfx}-\d\d)</code>',
                          page_html)

    @staticmethod
    def _links(page_html):
        """{label: href} for the server-rendered pagination links."""
        ul = UL_RE.search(page_html)
        if not ul:
            return {}
        return {label.strip(): html.unescape(href) for href, label in HREF_RE.findall(ul.group(1))}

    @staticmethod
    def _wrapper_hidden(page_html):
        return 'd-none' in WRAP_RE.search(page_html).group(1).split()

    @staticmethod
    def _info(page_html):
        return ' '.join(INFO_RE.search(page_html).group(1).split())

    # ── 1-4: page sizes, no duplicates, full coverage ─────────────────────────

    def test_pages_split_20_and_10_without_duplicates(self):
        client = self._web()
        p1 = self._rows(self._get(client, '/employees/'))
        p2 = self._rows(self._get(client, '/employees/?page=2'))
        self.assertEqual(len(p1), 20)
        self.assertEqual(len(p2), 10)
        self.assertFalse(set(p1) & set(p2))
        self.assertEqual(set(p1) | set(p2), {f'PA{self.sfx}-{i:02d}' for i in range(30)})
        # Stable across repeated requests (created_at ties broken by id).
        self.assertEqual(self._rows(self._get(client, '/employees/')), p1)

    # ── 5-7: Previous / Next links ────────────────────────────────────────────

    def test_next_and_previous_links(self):
        client = self._web()
        page1 = self._get(client, '/employees/')
        links1 = self._links(page1)
        self.assertIn('التالي', links1)
        self.assertNotIn('السابق', links1)
        self.assertEqual(parse_qs(urlsplit(links1['التالي']).query)['page'], ['2'])
        self.assertEqual(self._info(page1), 'عرض 1–20 من 30')

        page2 = self._get(client, links1['التالي'])          # follow "Next"
        self.assertEqual(len(self._rows(page2)), 10)
        links2 = self._links(page2)
        self.assertIn('السابق', links2)
        self.assertNotIn('التالي', links2)
        self.assertNotIn('page-item disabled"><a', page2)
        self.assertEqual(self._info(page2), 'عرض 21–30 من 30')

        back = self._get(client, links2['السابق'])            # follow "Previous"
        self.assertEqual(self._rows(back), self._rows(page1))

    # ── 8: filters survive pagination ─────────────────────────────────────────

    def test_filters_preserved_in_page_links(self):
        client = self._web()
        page1 = self._get(client, f'/employees/?q=PA{self.sfx}&job_title={JOB}&department={DEP}')
        self.assertEqual(len(self._rows(page1)), 20)
        nxt = self._links(page1)['التالي']
        qs = parse_qs(urlsplit(nxt).query)
        self.assertEqual(qs, {'page': ['2'], 'q': [f'PA{self.sfx}'],
                              'job_title': [JOB], 'department': [DEP]})
        page2 = self._get(client, nxt)
        rows2 = self._rows(page2)
        self.assertEqual(len(rows2), 5)                       # 25 matching → 20 + 5
        self.assertTrue(all(int(r[-2:]) < 25 for r in rows2))
        prev_qs = parse_qs(urlsplit(self._links(page2)['السابق']).query)
        self.assertEqual(prev_qs['job_title'], [JOB])
        self.assertEqual(prev_qs['department'], [DEP])

    # ── 9: tenant isolation on every page ─────────────────────────────────────

    def test_other_school_never_listed(self):
        client = self._web('a')
        for url in ('/employees/', '/employees/?page=2', '/employees/?page=3',
                    f'/employees/?q=PB{self.sfx}'):
            with self.subTest(url=url):
                page = self._get(client, url)
                self.assertNotIn(f'PB{self.sfx}', page.replace(f'q=PB{self.sfx}', '')
                                 .replace(f'value="PB{self.sfx}"', ''))
                self.assertTrue(all(r.startswith('PA') for r in self._rows(page)))
        for page_no in (1, 2):
            data = client.get(f'/employees/search?page={page_no}').get_json()
            self.assertTrue(all(i['employee_id'].startswith(f'PA{self.sfx}')
                                for i in data['items']))

    # ── 10: invalid page values ───────────────────────────────────────────────

    def test_invalid_page_values_are_safe(self):
        client = self._web()
        first = self._rows(self._get(client, '/employees/'))
        for bad in ('abc', '0', '-3', ''):
            with self.subTest(page=bad):
                self.assertEqual(self._rows(self._get(client, f'/employees/?page={bad}')), first)
        past = self._get(client, '/employees/?page=99')
        self.assertEqual(self._rows(past), [])
        self.assertEqual(self._info(past), 'عرض 0 من 30')
        self.assertIn('1', self._links(past))                 # a way back to page 1

    # ── 11: <= 20 employees → no navigation ───────────────────────────────────

    def test_single_page_has_no_navigation(self):
        page = self._get(self._web('b'), '/employees/')
        self.assertEqual(len(self._rows(page)), 5)
        self.assertTrue(self._wrapper_hidden(page))
        self.assertEqual(self._links(page), {})

    # ── 12: controls visible in the RTL layout ────────────────────────────────

    def test_controls_render_visibly_in_rtl_footer(self):
        page = self._get(self._web(), '/employees/')
        self.assertRegex(page, r'<html[^>]*dir="rtl"')
        self.assertFalse(self._wrapper_hidden(page))
        classes = WRAP_RE.search(page).group(1).split()
        self.assertIn('card-footer', classes)
        self.assertIn('flex-wrap', classes)                   # wraps on narrow widths
        self.assertEqual(list(self._links(page)), ['1', '2', 'التالي'])
        self.assertIn('<li class="page-item active">', page)

    # ── AJAX search endpoint pages identically ────────────────────────────────

    def test_search_json_pages_and_per_page(self):
        client = self._web()
        d1 = client.get('/employees/search?page=1').get_json()
        d2 = client.get('/employees/search?page=2').get_json()
        self.assertEqual((d1['total'], d1['pages'], d1['per_page']), (30, 2, 20))
        self.assertEqual((len(d1['items']), len(d2['items'])), (20, 10))
        self.assertFalse({i['id'] for i in d1['items']} & {i['id'] for i in d2['items']})
        self.assertTrue(d1['has_next'] and not d1['has_prev'])
        self.assertTrue(d2['has_prev'] and not d2['has_next'])


if __name__ == '__main__':
    unittest.main()

"""
Student name search tolerates spacing differences in Arabic compound names
("عبدالله" ↔ "عبد الله") on BOTH the server-rendered list (GET /students/) and
the AJAX live search (GET /students/search). Search-only: stored names are
never changed.

Proves:
  * stored "عبدالله محمد خلف" is found by "عبدالله محمد خلف" and "عبد الله محمد خلف";
  * stored "عبد الله محمد خلف" is found by "عبدالله محمد خلف";
  * general compounds (عبدالرحمن / عبد الكريم …), not only عبدالله;
  * leading / trailing / repeated / non-breaking whitespace in the query;
  * partial searches ("محمد خلف", "عبدالله", "خلف") and student-code search;
  * another school's same-name student never leaks;
  * status / stage / grade filters still apply; pagination stays correct;
  * stored full_name values are untouched.
"""
import re
import unittest
from datetime import date, datetime, timedelta
from uuid import uuid4

from app import create_app
from app.models import (db, AcademicYear, AuditLog, Grade, Role, School, Section,
                        Student, User)

PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True}

JOINED = 'عبدالله محمد خلف'
SPACED = 'عبد الله محمد خلف'


class StudentNameSearchSpacingTest(unittest.TestCase):

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
            self._school('a')
            self._school('b')
            db.session.commit()
            self.stored = self._stored_names()

    def _school(self, key):
        s = self.sfx
        school = School(school_name=f'NSrch {key} {s}', code=f'NS{key}{s}'[:20],
                        capacity=0, is_active=True)
        db.session.add(school)
        db.session.flush()
        year = AcademicYear(school_id=school.id, name=f'Y{key}{s}', is_current=True,
                            start_date=date(2026, 8, 1), end_date=date(2027, 6, 30))
        db.session.add(year)
        db.session.flush()
        grades, sections = {}, {}
        for g, stage in (('g1', 'ابتدائي'), ('g2', 'متوسط')):
            grade = Grade(name=f'{g}{key}{s}', stage=stage, school_id=school.id,
                          academic_year_id=year.id)
            db.session.add(grade)
            db.session.flush()
            sec = Section(name=f'{g}-1', grade_id=grade.id, school_id=school.id,
                          academic_year_id=year.id)
            db.session.add(sec)
            db.session.flush()
            grades[g], sections[g] = grade.id, sec.id
        admin = User(username=f'nsadm{key}_{s}', email=f'nsadm{key}_{s}@t.test',
                     full_name=f'adm {key}', role_id=self.admin_role,
                     school_id=school.id, is_active=True)
        admin.set_password(PASSWORD)
        db.session.add(admin)

        base = datetime(2026, 9, 1, 8, 0, 0)
        n = [0]

        def student(name, code, grade='g1', status='active'):
            n[0] += 1
            db.session.add(Student(
                student_id=code, full_name=name, school_id=school.id,
                academic_year_id=year.id, section_id=sections[grade], status=status,
                created_at=base + timedelta(minutes=n[0])))

        U = key.upper()
        if key == 'a':
            student(JOINED, f'NA{s}-J')                       # stored joined, grade 1
            student(SPACED, f'NA{s}-S', grade='g2')           # stored spaced, grade 2
            student('عبدالرحمن علي حسن', f'NA{s}-R')
            student(JOINED, f'NA{s}-X', status='archived')    # hidden by default status
            for i in range(25):                               # pagination set
                student(f'عبد الكريم سالم {i:02d}', f'NA{s}-K{i:02d}')
        else:
            student(JOINED, f'N{U}{s}-J')                     # same name, other school
            student(SPACED, f'N{U}{s}-S')
        self.ids[f'school_{key}'] = school.id
        self.ids[f'admin_{key}'] = admin.username
        self.ids.update({f'{g}_{key}': gid for g, gid in grades.items()})

    def _stored_names(self):
        return sorted((st.student_id, st.full_name) for st in
                      Student.query.execution_options(include_all_years=True, **OPTS)
                      .filter(Student.school_id.in_([self.ids['school_a'],
                                                     self.ids['school_b']])).all())

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            # Search must never rewrite stored names.
            self.assertEqual(self._stored_names(), self.stored)
            for key in ('a', 'b'):
                sid = self.ids[f'school_{key}']
                uids = [u.id for u in User.query.execution_options(**OPTS)
                        .filter_by(school_id=sid).all()]
                if uids:
                    AuditLog.query.execution_options(**OPTS).filter(
                        AuditLog.user_id.in_(uids)).delete(synchronize_session=False)
                for model in (AuditLog, Student, Section, Grade, User, AcademicYear):
                    model.query.execution_options(include_all_years=True, **OPTS).filter_by(
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

    def _codes(self, client, q, **params):
        """Student codes found by BOTH paths; asserts they agree."""
        args = {'q': q, **params}
        resp = client.get('/students/', query_string=args)
        self.assertEqual(resp.status_code, 200)
        page_codes = re.findall(r'<code class="text-primary-custom fw-600">(N[AB]\w+-\w+)</code>',
                                resp.get_data(as_text=True))
        data = client.get('/students/search', query_string=args).get_json()
        ajax_codes = [i['student_id'] for i in data['items']]
        self.assertEqual(page_codes, ajax_codes, f'web vs AJAX differ for {q!r} {params}')
        return set(page_codes)

    def _c(self, *suffixes):
        return {f'NA{self.sfx}-{x}' for x in suffixes}

    # ── 1-3: joined ↔ spaced ──────────────────────────────────────────────────

    def test_joined_and_spaced_queries_find_both_spellings(self):
        client = self._web()
        for q in (JOINED, SPACED):
            with self.subTest(q=q):
                self.assertEqual(self._codes(client, q), self._c('J', 'S'))

    def test_general_compound_names(self):
        client = self._web()
        self.assertEqual(self._codes(client, 'عبد الرحمن علي'), self._c('R'))
        self.assertEqual(self._codes(client, 'عبدالرحمن'), self._c('R'))
        self.assertEqual(len(self._codes(client, 'عبدالكريم سالم', page=1)), 20)

    # ── 4: whitespace normalisation ───────────────────────────────────────────

    def test_extra_whitespace_in_query(self):
        client = self._web()
        for q in ('  عبد   الله   محمد خلف  ', '\tعبدالله محمد  خلف\n',
                  ' عبدالله ', 'عبد الله'):
            with self.subTest(q=q):
                self.assertTrue(self._c('J', 'S') <= self._codes(client, q))

    def test_whitespace_only_query_is_no_filter(self):
        client = self._web()
        self.assertEqual(len(self._codes(client, '   ')), 20)

    # ── 5-7: partial + code search preserved ──────────────────────────────────

    def test_partial_name_searches(self):
        client = self._web()
        self.assertEqual(self._codes(client, 'محمد خلف'), self._c('J', 'S'))
        self.assertEqual(self._codes(client, 'خلف'), self._c('J', 'S'))
        self.assertEqual(self._codes(client, 'عبدالله'), self._c('J', 'S'))
        self.assertEqual(self._codes(client, 'عبد الله'), self._c('J', 'S'))
        self.assertEqual(self._codes(client, 'محمد'), self._c('J', 'S'))
        self.assertEqual(self._codes(client, 'لا يوجد اسم كهذا'), set())

    def test_student_code_search_unchanged(self):
        client = self._web()
        self.assertEqual(self._codes(client, f'NA{self.sfx}-R'), self._c('R'))
        self.assertEqual(self._codes(client, f'na{self.sfx}-r'), self._c('R'))   # ilike
        self.assertEqual(self._codes(client, f'NA{self.sfx}-K0', page=1),
                         self._c(*[f'K0{i}' for i in range(10)]))
        # Code search is not space-collapsed: a space inside the code finds nothing.
        self.assertEqual(self._codes(client, f'NA{self.sfx} -R'), set())

    # ── 8: tenant isolation ───────────────────────────────────────────────────

    def test_other_school_same_name_never_leaks(self):
        for key in ('a', 'b'):
            client = self._web(key)
            for q in (JOINED, SPACED, 'خلف'):
                with self.subTest(school=key, q=q):
                    codes = self._codes(client, q)
                    self.assertTrue(codes)
                    self.assertTrue(all(c.startswith(f'N{key.upper()}{self.sfx}') for c in codes),
                                    codes)

    # ── 9: status / stage / grade filters ─────────────────────────────────────

    def test_status_stage_grade_filters_still_apply(self):
        client = self._web()
        self.assertEqual(self._codes(client, SPACED, status='archived'), self._c('X'))
        self.assertEqual(self._codes(client, SPACED, grade_id=self.ids['g1_a']), self._c('J'))
        self.assertEqual(self._codes(client, JOINED, grade_id=self.ids['g2_a']), self._c('S'))
        self.assertEqual(self._codes(client, JOINED, stage='متوسط'), self._c('S'))
        self.assertEqual(self._codes(client, SPACED, stage='ابتدائي'), self._c('J'))

    # ── 10: pagination ────────────────────────────────────────────────────────

    def test_pagination_with_normalised_search(self):
        client = self._web()
        p1 = self._codes(client, 'عبدالكريم سالم', page=1)
        p2 = self._codes(client, 'عبدالكريم سالم', page=2)
        self.assertEqual((len(p1), len(p2)), (20, 5))
        self.assertFalse(p1 & p2)
        self.assertEqual(p1 | p2, self._c(*[f'K{i:02d}' for i in range(25)]))
        data = client.get('/students/search', query_string={'q': 'عبدالكريم  سالم'}).get_json()
        self.assertEqual((data['total'], data['pages']), (25, 2))


if __name__ == '__main__':
    unittest.main()

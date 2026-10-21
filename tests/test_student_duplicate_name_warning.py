"""
Add-Student wizard duplicate-name WARNING (awareness only — never blocks).

GET /students/check-duplicate-name?name=... returns existing students of the
authenticated user's school whose normalised full name equals the input, and
students.create re-runs the same lookup server-side after saving.

Proves:
  * whitespace / Latin-case / أ-إ-آ-ا variants match (also in the list
    search); partial, different and ة/ه or ى/ي variants do not;
  * every stored academic year is searched; active/archived returned with
    their status, the deleted tombstone never;
  * at most 10 matches (+ "more"); minimal fields only;
  * another school's student is never returned, even with a tampered school_id;
  * a building-restricted user only sees matches inside their buildings;
  * login + add_student/view_students are required;
  * a second same-name student can still be created, and the server-side
    re-check flashes a warning naming the existing record;
  * the wizard wires the debounced check; the public form does not get it.
"""
import re
import unittest
from datetime import date, datetime, timedelta
from uuid import uuid4

from app import create_app
from app.models import (db, AcademicYear, AuditLog, Grade, Role, School, SchoolBuilding,
                        Section, Student, User, UserBuildingAccess)

PASSWORD = 'Test1234!'
OPTS = {'bypass_tenant_scope': True}
URL = '/students/check-duplicate-name'
NAME = 'احمد محمد علي'
MANY = 'سارة علي حسن'
FIELDS = {'id', 'full_name', 'student_code', 'status', 'status_label', 'grade',
          'section', 'enrollment_date', 'view_url'}


class StudentDuplicateNameWarningTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config['RATELIMIT_ENABLED'] = False
        with cls.app.app_context():
            cls.roles = {n: Role.query.filter_by(name=n).first().id
                         for n in ('school_admin', 'parent')}

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        with self.app.app_context():
            self._school('a')
            self._school('b')
            db.session.commit()

    def _user(self, school, label, role):
        s = self.sfx
        u = User(username=f'dn{label}_{s}', email=f'dn{label}_{s}@t.test',
                 full_name=label, role_id=self.roles[role], school_id=school.id,
                 is_active=True)
        u.set_password(PASSWORD)
        db.session.add(u)
        db.session.flush()
        self.ids[label] = u.username
        return u

    def _school(self, key):
        s = self.sfx
        school = School(school_name=f'DupN {key} {s}', code=f'DN{key}{s}'[:20],
                        capacity=0, is_active=True, enable_buildings=(key == 'a'))
        db.session.add(school)
        db.session.flush()
        old = AcademicYear(school_id=school.id, name=f'Old{key}{s}', is_current=False,
                           start_date=date(2025, 8, 1), end_date=date(2026, 6, 30))
        cur = AcademicYear(school_id=school.id, name=f'Cur{key}{s}', is_current=True,
                           start_date=date(2026, 8, 1), end_date=date(2027, 6, 30))
        db.session.add_all([old, cur])
        db.session.flush()
        sections = {}
        for label, year in (('cur', cur), ('old', old)):
            g = Grade(name=f'G{label}{key}{s}', stage='ابتدائية', school_id=school.id,
                      academic_year_id=year.id)
            db.session.add(g)
            db.session.flush()
            sec = Section(name=f'S{label}', grade_id=g.id, school_id=school.id,
                          academic_year_id=year.id)
            db.session.add(sec)
            db.session.flush()
            sections[label] = sec
        self._user(school, f'adm{key}', 'school_admin')
        base = datetime(2026, 9, 1, 8, 0, 0)
        n = [0]

        def student(label, name, status='active', sec='cur', **extra):
            n[0] += 1
            st = Student(student_id=f'D{key.upper()}{s}-{label}', full_name=name,
                         school_id=school.id, section_id=sections[sec].id,
                         academic_year_id=sections[sec].academic_year_id, status=status,
                         enrollment_date=date(2025, 9, 1),
                         created_at=base + timedelta(minutes=n[0]), **extra)
            db.session.add(st)
            return st

        self.ids[f'school_{key}'] = school.id
        self.ids[f'sec_{key}'] = sections['cur'].id
        if key == 'b':
            student('B1', NAME)
            db.session.flush()
            return

        b1 = SchoolBuilding(school_id=school.id, name=f'B1 {s}')
        b2 = SchoolBuilding(school_id=school.id, name=f'B2 {s}')
        db.session.add_all([b1, b2])
        db.session.flush()
        student('ACT', NAME, building_id=b1.id)                        # active, current year
        student('ARC', NAME, 'archived', sec='old', building_id=b2.id)  # archived, older year
        student('DEL', NAME, 'deleted')                                 # tombstone
        student('LAT', 'ahmed ALI')
        student('IBR', 'ابراهيم علي')             # bare alef stored
        student('ADM', 'آدم حسن')                 # madda stored
        student('FTM', 'فاطمة علي')               # taa marbuta stored
        for i in range(12):
            student(f'M{i:02d}', MANY)
        restricted = self._user(school, 'rstr', 'school_admin')
        db.session.add(UserBuildingAccess(school_id=school.id, building_id=b1.id,
                                          user_id=restricted.id))
        self._user(school, 'par', 'parent')
        db.session.flush()
        self.ids['old_place'] = (sections['old'].grade.name, sections['old'].name)

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
                for model in (UserBuildingAccess, Student, SchoolBuilding, Section,
                              Grade, User, AcademicYear):
                    model.query.execution_options(include_all_years=True, **OPTS).filter_by(
                        school_id=sid).delete(synchronize_session=False)
                School.query.filter_by(id=sid).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _web(self, who='adma'):
        client = self.app.test_client()
        resp = client.post('/auth/login', data={'username': self.ids[who],
                                                'password': PASSWORD})
        self.assertEqual(resp.status_code, 302)
        return client

    def _check(self, client, name, **extra):
        resp = client.get(URL, query_string={'name': name, **extra})
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def _codes(self, client, name, **extra):
        data = self._check(client, name, **extra)
        self.assertEqual(data['count'], len(data['matches']))
        return {m['student_code'].rsplit('-', 1)[1] if m['student_code'].startswith(
                    f'DA{self.sfx}-') else m['student_code'] for m in data['matches']}

    # ── 1, 2, 5, 6, 7: matching rules ─────────────────────────────────────────

    def test_same_name_returns_active_and_archived_not_deleted(self):
        data = self._check(self._web(), NAME)
        by = {m['student_code'].rsplit('-', 1)[1]: m for m in data['matches']}
        self.assertEqual(set(by), {'ACT', 'ARC'})               # DEL excluded
        self.assertEqual((by['ACT']['status'], by['ACT']['status_label']), ('active', 'فعّال'))
        self.assertEqual((by['ARC']['status'], by['ARC']['status_label']), ('archived', 'مؤرشف'))
        self.assertFalse(data['more'])
        for m in data['matches']:
            self.assertEqual(set(m), FIELDS)                    # minimal fields only

    def test_whitespace_and_latin_case_variants_match(self):
        client = self._web()
        for variant in ('  احمد   محمد علي ', 'احمد\tمحمد علي', 'احمد محمد علي'):
            with self.subTest(variant=variant):
                self.assertEqual(self._codes(client, variant), {'ACT', 'ARC'})
        self.assertEqual(self._codes(client, 'AHMED   ali'), {'LAT'})

    # ── 4: older stored academic year ─────────────────────────────────────────

    def test_older_academic_year_student_matches_with_its_class(self):
        data = self._check(self._web(), NAME)
        arc = next(m for m in data['matches'] if m['student_code'].endswith('-ARC'))
        self.assertEqual((arc['grade'], arc['section']), self.ids['old_place'])

    def test_alef_variants_match_in_both_directions(self):
        client = self._web()
        cases = {'أحمد محمد علي': {'ACT', 'ARC'},      # أ typed, ا stored
                 'إحمد محمد علي': {'ACT', 'ARC'},
                 'إبراهيم علي':   {'IBR'},             # إ typed, ا stored
                 'أبراهيم   علي': {'IBR'},
                 'ادم حسن':       {'ADM'},             # ا typed, آ stored
                 'آدم حسن':       {'ADM'},
                 'أدم حسن':       {'ADM'}}
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self._codes(client, name), expected)

    def test_list_search_folds_alef_and_keeps_spacing_rule(self):
        client = self._web()
        codes = lambda q: {i['student_id'].rsplit('-', 1)[1] for i in  # noqa: E731
                           client.get('/students/search', query_string={'q': q})
                           .get_json()['items']}
        self.assertEqual(codes('إبراهيم'), {'IBR'})
        self.assertEqual(codes('ادم'), {'ADM'})
        self.assertEqual(codes('أحمد محمد'), {'ACT'})   # partial; archived hidden by default
        self.assertEqual(codes('احمدمحمد'), {'ACT'})    # joined ↔ spaced unchanged

    # ── 8: no fuzzy / aggressive matching ─────────────────────────────────────

    def test_different_partial_short_and_other_letters_do_not_match(self):
        client = self._web()
        for name in ('احمد محمد حسن', 'احمد محمد', 'محمد علي', 'ابراهيم على',
                     'فاطمه علي', 'ادهم حسن', 'abc', '', 'ا' * 201):
            with self.subTest(name=name):
                self.assertEqual(self._check(client, name)['matches'], [])
        self.assertEqual(self._codes(client, 'فاطمة علي'), {'FTM'})

    # ── 9: limit ──────────────────────────────────────────────────────────────

    def test_many_same_name_students_capped_at_ten_with_more_flag(self):
        data = self._check(self._web(), MANY)
        self.assertEqual((data['count'], data['more']), (10, True))
        # newest first
        self.assertEqual([m['student_code'].rsplit('-', 1)[1] for m in data['matches']],
                         [f'M{i:02d}' for i in range(11, 1, -1)])

    # ── 3: tenant isolation ───────────────────────────────────────────────────

    def test_other_school_never_matches_even_with_tampered_school_id(self):
        a = self._web('adma')
        for extra in ({}, {'school_id': self.ids['school_b']}):
            with self.subTest(extra=extra):
                codes = [m['student_code'] for m in self._check(a, NAME, **extra)['matches']]
                self.assertTrue(codes)
                self.assertTrue(all(c.startswith(f'DA{self.sfx}') for c in codes), codes)
        b = self._web('admb')
        codes = [m['student_code'] for m in
                 self._check(b, NAME, school_id=self.ids['school_a'])['matches']]
        self.assertEqual(codes, [f'DB{self.sfx}-B1'])

    # ── 12: building scope ────────────────────────────────────────────────────

    def test_building_restricted_user_sees_only_permitted_buildings(self):
        self.assertEqual(self._codes(self._web('rstr'), NAME), {'ACT'})

    # ── 11: authentication / permission ───────────────────────────────────────

    def test_requires_login_and_student_permissions(self):
        anon = self.app.test_client().get(URL, query_string={'name': NAME})
        self.assertEqual(anon.status_code, 302)
        self.assertIn('/auth/login', anon.headers['Location'])
        self.assertEqual(self._web('par').get(URL, query_string={'name': NAME}).status_code, 403)

    # ── 10: never blocks + server-side re-check ───────────────────────────────

    def _create(self, client, name):
        return client.post('/students/create', data={
            'full_name': name, 'section_id': str(self.ids['sec_a']),
            'gender': 'male', 'date_of_birth': '2015-02-03',
            'guardian_name': 'ولي', 'guardian_phone': '07711111111',
            'guardian_relation': 'father'}, follow_redirects=True)

    def _saved(self, name):
        with self.app.app_context():
            return (Student.query.execution_options(include_all_years=True, **OPTS)
                    .filter_by(school_id=self.ids['school_a'], full_name=name).count())

    def test_same_name_student_can_still_be_created_with_server_warning(self):
        client = self._web()
        typed = '  احمد   محمد علي '
        stored = typed.strip()              # create() only strips; stored as typed
        self.assertEqual(self._saved(stored), 0)
        for attempt in (1, 2):
            with self.subTest(attempt=attempt):
                body = self._create(client, typed).get_data(as_text=True)
                self.assertIn('تم إضافة الطالب', body)
                self.assertIn('يوجد طالب مسجل مسبقاً بالاسم نفسه', body)
                self.assertIn(f'DA{self.sfx}-ACT', body)
                self.assertNotIn(f'DA{self.sfx}-DEL', body)
                self.assertNotIn(f'DB{self.sfx}-B1', body)
        self.assertEqual(self._saved(stored), 2)
        self.assertEqual(self._saved(NAME), 3)          # existing rows untouched

    def test_unique_name_creates_without_warning(self):
        body = self._create(self._web(), f'اسم فريد جديد {self.sfx}').get_data(as_text=True)
        self.assertIn('تم إضافة الطالب', body)
        self.assertNotIn('يوجد طالب مسجل مسبقاً بالاسم نفسه', body)

    # ── 13: client wiring ─────────────────────────────────────────────────────

    def test_wizard_wires_debounced_warning(self):
        body = self._web().get('/students/create').get_data(as_text=True)
        self.assertRegex(body, r'name="full_name"[^>]*data-dup-check-url="' + re.escape(URL) + '"')
        self.assertIn('id="dupNameWarning"', body)
        js = body[body.index('Duplicate-name warning'):]
        js = js[:js.index('})();')]
        self.assertIn('DEBOUNCE_MS = 500', js)
        self.assertIn("addEventListener('blur', check)", js)
        self.assertIn('setTimeout(check, DEBOUNCE_MS)', js)
        self.assertIn('esc(m.full_name)', js)                   # rendered escaped
        self.assertNotIn('school_id', js)
        self.assertNotIn('disabled', js)                       # never blocks Next/Save

    def test_public_identity_fields_have_no_duplicate_check(self):
        with self.app.test_request_context():
            from flask import render_template_string
            from app.utils.student_form_config import get_student_form_config
            html = render_template_string(
                "{% import 'students/_field_macros.html' as f %}"
                "{{ f.student_identity_fields(cfg, {}) }}",
                cfg=get_student_form_config(self.ids['school_a']))
        self.assertIn('name="full_name"', html)
        self.assertNotIn('data-dup-check-url', html)
        self.assertNotIn('dupNameWarning', html)


if __name__ == '__main__':
    unittest.main()

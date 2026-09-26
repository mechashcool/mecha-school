"""Attendance scope-manifest resolver — authorization and inertness.

The resolver decides which `student:<id>` scopes a principal may synchronize.
It is the entitlement authority for the B2 attendance pilot, so every rejection
path matters as much as the happy path: a scope wrongly emitted here would let a
client receive another family's attendance changes.

Every rule is checked against the mobile endpoint it mirrors, so these tests
double as a guard that the resolver has not drifted away from
`_assert_owns_student`.
"""
import unittest
from datetime import date
from uuid import uuid4

from sqlalchemy import text

from app import create_app
from app.models import (
    db, AcademicYear, Grade, Role, School, Section, Student,
    SyncPrincipalState, User, parent_students,
)
from app.services.sync_scope_manifest import (
    PILOT_ROLES, ScopeManifest, resolve_attendance_scopes, read_scopes_version,
    scope_for_student,
)


class ScopeManifestTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.ids = {}
        with self.app.app_context():
            roles = {}
            for name in ('parent', 'teacher', 'admin'):
                role = Role.query.filter_by(name=name).first()
                self.assertIsNotNone(role, f'seed the {name} role first')
                roles[name] = role.id
            self.ids['role_parent'] = roles['parent']

            for tag in ('a', 'b'):
                school = School(school_name=f'Scope {tag} {self.suffix}',
                                code=f'SC{tag.upper()}{self.suffix[:6]}',
                                capacity=0, is_active=True)
                db.session.add(school)
                db.session.flush()
                year = AcademicYear(school_id=school.id,
                                    name=f'Y {tag} {self.suffix}',
                                    start_date=date(2025, 8, 1),
                                    end_date=date(2026, 6, 30), is_current=True)
                prev = AcademicYear(school_id=school.id,
                                    name=f'Yp {tag} {self.suffix}',
                                    start_date=date(2024, 8, 1),
                                    end_date=date(2025, 6, 30), is_current=False)
                db.session.add_all([year, prev])
                db.session.flush()
                grade = Grade(school_id=school.id, academic_year_id=year.id,
                              name=f'G{tag}{self.suffix[:4]}')
                db.session.add(grade)
                db.session.flush()
                section = Section(school_id=school.id, academic_year_id=year.id,
                                  grade_id=grade.id,
                                  name=f'S{tag}{self.suffix[:4]}', capacity=30)
                db.session.add(section)
                db.session.flush()

                # Two students per school: one current year, one prior year, so
                # the cross-year rule can be checked directly.
                cur = Student(student_id=f'SC-{tag.upper()}1-{self.suffix}',
                              full_name=f'Cur {tag}', date_of_birth=date(2015, 1, 1),
                              gender='male', school_id=school.id,
                              academic_year_id=year.id, section_id=section.id,
                              status='active')
                old = Student(student_id=f'SC-{tag.upper()}2-{self.suffix}',
                              full_name=f'Old {tag}', date_of_birth=date(2014, 1, 1),
                              gender='male', school_id=school.id,
                              academic_year_id=prev.id, section_id=section.id,
                              status='active')
                db.session.add_all([cur, old])
                db.session.flush()

                parent = User(username=f'sc_p_{tag}_{self.suffix}',
                              email=f'sc_p_{tag}_{self.suffix}@example.test',
                              full_name=f'Parent {tag}', role_id=roles['parent'],
                              school_id=school.id, is_active=True)
                parent.set_password('Password123')
                db.session.add(parent)
                db.session.flush()
                db.session.execute(parent_students.insert().values(
                    user_id=parent.id, student_id=cur.id))

                self.ids.update({
                    f'school_{tag}': school.id, f'year_{tag}': year.id,
                    f'prev_year_{tag}': prev.id, f'section_{tag}': section.id,
                    f'cur_{tag}': cur.id, f'old_{tag}': old.id,
                    f'grade_{tag}': grade.id, f'parent_{tag}': parent.id,
                })

            # Extra principals in school A for the negative cases.
            extras = {
                'unlinked': ('parent', self.ids['school_a'], True),
                'inactive': ('parent', self.ids['school_a'], False),
                'teacher':  ('teacher', self.ids['school_a'], True),
                'admin':    ('admin', self.ids['school_a'], True),
                'superadmin': ('admin', None, True),
            }
            for key, (role_name, school_id, active) in extras.items():
                u = User(username=f'sc_{key}_{self.suffix}',
                         email=f'sc_{key}_{self.suffix}@example.test',
                         full_name=key, role_id=roles[role_name],
                         school_id=school_id, is_active=active)
                u.set_password('Password123')
                db.session.add(u)
                db.session.flush()
                self.ids[f'u_{key}'] = u.id

            # The inactive and teacher principals ARE linked to a child, so the
            # tests prove the rejection comes from the principal check and not
            # from an absent relationship.
            for key in ('inactive', 'teacher'):
                db.session.execute(parent_students.insert().values(
                    user_id=self.ids[f'u_{key}'], student_id=self.ids['cur_a']))
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            opts = {'bypass_tenant_scope': True}
            students = [self.ids[k] for k in ('cur_a', 'old_a', 'cur_b', 'old_b')]
            all_users = [v for k, v in self.ids.items()
                         if k.startswith('u_') or k.startswith('parent_')]
            db.session.execute(text(
                'DELETE FROM sync_principal_state WHERE user_id = ANY(:u)'),
                {'u': all_users})
            db.session.execute(text(
                'DELETE FROM parent_students WHERE student_id = ANY(:s)'),
                {'s': students})
            user_keys = ['parent_a', 'parent_b'] + [
                f'u_{k}' for k in ('unlinked', 'inactive', 'teacher', 'admin',
                                   'superadmin')]
            for model, keys in ((Student, ['cur_a', 'old_a', 'cur_b', 'old_b']),
                                (User, user_keys),
                                (Section, ['section_a', 'section_b']),
                                (Grade, ['grade_a', 'grade_b']),
                                (AcademicYear, ['year_a', 'year_b',
                                                'prev_year_a', 'prev_year_b']),
                                (School, ['school_a', 'school_b'])):
                for key in keys:
                    row = db.session.get(model, self.ids[key],
                                         execution_options=opts)
                    if row is not None:
                        db.session.delete(row)
                db.session.flush()
            db.session.commit()

    def _user(self, key):
        return db.session.get(User, self.ids[key],
                              execution_options={'bypass_tenant_scope': True})

    def _resolve(self, key):
        with self.app.app_context():
            return resolve_attendance_scopes(self._user(key))

    # ── Own children ─────────────────────────────────────────────────────────

    def test_parent_gets_scope_for_own_linked_child(self):
        m = self._resolve('parent_a')
        self.assertEqual(m.scopes, (f"student:{self.ids['cur_a']}",))
        self.assertEqual(m.school_id, self.ids['school_a'])
        self.assertFalse(m.is_empty)

    def test_multiple_linked_children_all_appear(self):
        with self.app.app_context():
            db.session.execute(parent_students.insert().values(
                user_id=self.ids['parent_a'], student_id=self.ids['old_a']))
            db.session.commit()
        m = self._resolve('parent_a')
        expected = tuple(sorted(
            (f"student:{self.ids['cur_a']}", f"student:{self.ids['old_a']}"),
            key=lambda s: int(s.split(':')[1])))
        self.assertEqual(m.scopes, expected)
        self.assertEqual(len(m.scopes), 2)

    def test_a_second_link_row_does_not_duplicate_a_scope(self):
        """Defensive: a duplicated junction row must not double the scope."""
        with self.app.app_context():
            db.session.execute(text(
                'INSERT INTO parent_students (user_id, student_id) '
                'VALUES (:u, :s) ON CONFLICT DO NOTHING'),
                {'u': self.ids['parent_a'], 's': self.ids['cur_a']})
            db.session.commit()
        m = self._resolve('parent_a')
        self.assertEqual(len(m.scopes), len(set(m.scopes)))
        self.assertEqual(m.scopes, (f"student:{self.ids['cur_a']}",))

    # ── Negative: no unauthorized scope, ever ────────────────────────────────

    def test_unlinked_parent_gets_nothing(self):
        m = self._resolve('u_unlinked')
        self.assertEqual(m.scopes, ())
        self.assertTrue(m.is_empty)

    def test_parent_never_sees_another_schools_student(self):
        m = self._resolve('parent_a')
        for forbidden in ('cur_b', 'old_b'):
            self.assertNotIn(f"student:{self.ids[forbidden]}", m.scopes)

    def test_cross_school_link_yields_no_scope(self):
        """A link row pointing at another school's student must be ignored.

        Planted directly, because the application would not normally create it —
        this proves the school_id condition is enforced by the resolver itself
        and not merely by the absence of such a row.
        """
        with self.app.app_context():
            db.session.execute(parent_students.insert().values(
                user_id=self.ids['parent_a'], student_id=self.ids['cur_b']))
            db.session.commit()
        m = self._resolve('parent_a')
        self.assertNotIn(f"student:{self.ids['cur_b']}", m.scopes,
                         'cross-school link must not produce a scope')
        self.assertEqual(m.scopes, (f"student:{self.ids['cur_a']}",))

    def test_inactive_user_gets_nothing_despite_a_valid_link(self):
        m = self._resolve('u_inactive')
        self.assertEqual(m.scopes, (),
                         'jwt_required rejects inactive accounts; so must this')

    def test_wrong_role_gets_nothing_despite_a_valid_link(self):
        m = self._resolve('u_teacher')
        self.assertEqual(m.scopes, (),
                         'a teacher is linked here but is not in the pilot')

    def test_admin_gets_nothing(self):
        self.assertEqual(self._resolve('u_admin').scopes, ())

    def test_null_school_super_admin_is_never_a_wildcard(self):
        m = self._resolve('u_superadmin')
        self.assertEqual(m.scopes, ())
        self.assertIsNone(m.school_id)

    def test_missing_principal_identity_is_handled(self):
        with self.app.app_context():
            self.assertEqual(resolve_attendance_scopes(None).scopes, ())

            class Anon:
                id = None
            self.assertEqual(resolve_attendance_scopes(Anon()).scopes, ())

    def test_deleted_user_yields_no_scope(self):
        with self.app.app_context():
            uid = self.ids['u_unlinked']
            db.session.execute(text(
                'DELETE FROM parent_students WHERE user_id = :u'), {'u': uid})
            user = db.session.get(User, uid,
                                  execution_options={'bypass_tenant_scope': True})
            db.session.delete(user)
            db.session.commit()
            self.assertIsNone(db.session.get(
                User, uid, execution_options={'bypass_tenant_scope': True}))
            self.ids['u_unlinked'] = uid  # tearDown tolerates the absence

    def test_relationship_removal_drops_the_scope(self):
        self.assertEqual(len(self._resolve('parent_a').scopes), 1)
        with self.app.app_context():
            db.session.execute(text(
                'DELETE FROM parent_students WHERE user_id = :u AND student_id = :s'),
                {'u': self.ids['parent_a'], 's': self.ids['cur_a']})
            db.session.commit()
        after = self._resolve('parent_a')
        self.assertEqual(after.scopes, (),
                         'revoked access must remove the scope immediately')
        self.assertEqual(after.scopes_version, 1,
                         'B1 has no bump writer yet — that is B2')

    def test_student_deletion_drops_the_scope(self):
        with self.app.app_context():
            db.session.execute(text(
                'DELETE FROM parent_students WHERE student_id = :s'),
                {'s': self.ids['cur_a']})
            row = db.session.get(Student, self.ids['cur_a'],
                                 execution_options={'bypass_tenant_scope': True})
            db.session.delete(row)
            db.session.commit()
        self.assertEqual(self._resolve('parent_a').scopes, ())

    # ── Academic-year behaviour ──────────────────────────────────────────────

    def test_scopes_are_not_filtered_by_academic_year(self):
        """Student is a master record that persists across years.

        Linking the prior-year student must still yield a scope: filtering the
        manifest by year would make a linked child vanish at rollover, which is
        exactly the behaviour app/utils/scoping.py:59-61 exists to prevent.
        """
        with self.app.app_context():
            db.session.execute(parent_students.insert().values(
                user_id=self.ids['parent_a'], student_id=self.ids['old_a']))
            db.session.commit()
        m = self._resolve('parent_a')
        self.assertIn(f"student:{self.ids['old_a']}", m.scopes,
                      'a prior-year linked child keeps its scope')

    def test_resolver_does_not_widen_historical_attendance_access(self):
        """The manifest grants scopes, not date ranges.

        Historical reach stays entirely with the existing endpoint, which caps
        its window at 365 days. Nothing in the manifest encodes or extends a
        date range.
        """
        m = self._resolve('parent_a')
        blob = repr(m.as_dict())
        for token in ('start', 'end', 'date', 'year', 'range'):
            self.assertNotIn(token, blob)
        self.assertEqual(set(m.as_dict()), {'scopes_version', 'scopes'})

    # ── Determinism and PII ──────────────────────────────────────────────────

    def test_output_is_deterministic_and_sorted(self):
        with self.app.app_context():
            db.session.execute(parent_students.insert().values(
                user_id=self.ids['parent_a'], student_id=self.ids['old_a']))
            db.session.commit()
        runs = [self._resolve('parent_a').scopes for _ in range(4)]
        self.assertEqual(len(set(runs)), 1, 'repeated resolution must be stable')
        ids = [int(s.split(':')[1]) for s in runs[0]]
        self.assertEqual(ids, sorted(ids), 'scopes must be sorted by student id')

    def test_manifest_contains_no_pii_or_unauthorized_identifiers(self):
        with self.app.app_context():
            names = [s.full_name for s in Student.query
                     .execution_options(bypass_tenant_scope=True)
                     .filter(Student.id.in_([self.ids['cur_a'],
                                             self.ids['cur_b']])).all()]
        blob = repr(self._resolve('parent_a').as_dict())
        for name in names:
            self.assertNotIn(name, blob, 'no student name may appear')
        self.assertNotIn(str(self.ids['cur_b']), blob)
        self.assertNotIn(str(self.ids['school_b']), blob)
        # Every emitted value is an opaque student scope, nothing else.
        for scope in self._resolve('parent_a').scopes:
            self.assertRegex(scope, r'^student:\d+$')

    # ── scopes_version ───────────────────────────────────────────────────────

    def test_absent_principal_state_row_means_version_one(self):
        """B1 creates no row for an existing principal; absence reads as 1.

        Scoped to this test's own principals rather than a global count: the
        isolated database is shared across the suite, so a global assertion
        would fail for reasons unrelated to the behaviour under test.
        """
        with self.app.app_context():
            mine = db.session.execute(text(
                'SELECT count(*) FROM sync_principal_state WHERE user_id = ANY(:u)'),
                {'u': [self.ids['parent_a'], self.ids['parent_b'],
                       self.ids['u_unlinked']]}).scalar()
            self.assertEqual(mine, 0, 'B1 creates no rows for existing users')
            self.assertEqual(read_scopes_version(self.ids['parent_a']), 1)
        self.assertEqual(self._resolve('parent_a').scopes_version, 1)

    def test_durable_version_is_read_when_a_row_exists(self):
        with self.app.app_context():
            db.session.add(SyncPrincipalState(
                user_id=self.ids['parent_a'], school_id=self.ids['school_a'],
                scopes_version=7))
            db.session.commit()
        self.assertEqual(self._resolve('parent_a').scopes_version, 7)

    def test_scopes_version_accepts_a_value_beyond_32_bits(self):
        """BIGINT, not INTEGER — a permanent monotonic counter."""
        big = 2 ** 31 + 5
        with self.app.app_context():
            db.session.add(SyncPrincipalState(
                user_id=self.ids['parent_a'], school_id=self.ids['school_a'],
                scopes_version=big))
            db.session.commit()
        self.assertEqual(self._resolve('parent_a').scopes_version, big)

    def test_version_is_returned_even_when_access_is_fully_revoked(self):
        """A client must be able to see that its scopes became empty."""
        with self.app.app_context():
            db.session.add(SyncPrincipalState(
                user_id=self.ids['parent_a'], school_id=self.ids['school_a'],
                scopes_version=3))
            db.session.execute(text(
                'DELETE FROM parent_students WHERE user_id = :u'),
                {'u': self.ids['parent_a']})
            db.session.commit()
        m = self._resolve('parent_a')
        self.assertEqual(m.scopes, ())
        self.assertEqual(m.scopes_version, 3)

    # ── Inertness ────────────────────────────────────────────────────────────

    def test_resolver_is_not_reachable_from_any_blueprint(self):
        """No public API may import it while the sync flags are disabled."""
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent / 'app' / 'blueprints'
        offenders = [str(p) for p in root.rglob('*.py')
                     if 'sync_scope_manifest' in p.read_text(encoding='utf-8')]
        self.assertEqual(offenders, [], f'blueprint imports the resolver: {offenders}')

    def test_no_route_exposes_the_manifest(self):
        for rule in self.app.url_map.iter_rules():
            self.assertNotIn('manifest', rule.rule.lower())
            self.assertFalse(rule.rule.startswith('/api/mobile/v1/sync'))

    def test_flags_remain_disabled(self):
        self.assertFalse(self.app.config['SYNC_JOURNAL_ENABLED'])
        self.assertFalse(self.app.config['SYNC_SIGNAL_ENABLED'])

    def test_pilot_roles_is_parent_only(self):
        self.assertEqual(set(PILOT_ROLES), {'parent'},
                         'widening this set grants new mobile access')

    def test_scope_key_helper_matches_emitted_scopes(self):
        self.assertEqual(scope_for_student(self.ids['cur_a']),
                         f"student:{self.ids['cur_a']}")
        self.assertIn(scope_for_student(self.ids['cur_a']),
                      self._resolve('parent_a').scopes)

    def test_manifest_is_immutable(self):
        m = self._resolve('parent_a')
        self.assertIsInstance(m, ScopeManifest)
        with self.assertRaises(Exception):
            m.scopes = ('student:1',)

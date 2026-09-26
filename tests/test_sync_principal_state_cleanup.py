"""School teardown must remove sync_principal_state — and still work at all.

`SyncPrincipalState` was added to `SCHOOL_DELETE_ORDER`. That list is ordered by
foreign-key dependency, so a wrong position would break school deletion for
every school, not just ones with sync rows. These tests pin both halves: the new
row is removed, and the existing cascade still completes.

They also pin the two cases the FK cascade does NOT cover, which matter for B2:
a super-admin row (school_id NULL) survives a school delete, and the soft-delete
"delete user" path keeps the users row alive so no cascade fires at all.
"""
import unittest
from datetime import date
from uuid import uuid4

from sqlalchemy import text

from app import create_app
from app.models import (
    db, AcademicYear, Role, School, Student, SyncPrincipalState, User,
    parent_students,
)
from app.utils.school_cleanup import SCHOOL_DELETE_ORDER, cleanup_school_cascade


class SyncPrincipalStateCleanupTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        with self.app.app_context():
            role = Role.query.filter_by(name='parent').first()
            self.assertIsNotNone(role, 'seed roles first')
            school = School(school_name=f'Cleanup {self.suffix}',
                            code=f'CL{self.suffix[:6]}', capacity=0,
                            is_active=True)
            db.session.add(school)
            db.session.flush()
            year = AcademicYear(school_id=school.id, name=f'Y {self.suffix}',
                                start_date=date(2025, 8, 1),
                                end_date=date(2026, 6, 30), is_current=True)
            db.session.add(year)
            db.session.flush()
            student = Student(student_id=f'CL-{self.suffix}',
                              full_name='Cleanup Student',
                              date_of_birth=date(2015, 1, 1), gender='male',
                              school_id=school.id, academic_year_id=year.id,
                              status='active')
            parent = User(username=f'cl_p_{self.suffix}',
                          email=f'cl_p_{self.suffix}@example.test',
                          full_name='Cleanup Parent', role_id=role.id,
                          school_id=school.id, is_active=True)
            parent.set_password('Password123')
            db.session.add_all([student, parent])
            db.session.flush()
            db.session.execute(parent_students.insert().values(
                user_id=parent.id, student_id=student.id))
            db.session.add(SyncPrincipalState(
                user_id=parent.id, school_id=school.id, scopes_version=4))
            db.session.commit()
            self.school_id, self.parent_id = school.id, parent.id

    def tearDown(self):
        with self.app.app_context():
            opts = {'bypass_tenant_scope': True}
            db.session.execute(text(
                'DELETE FROM sync_principal_state WHERE user_id = :u'),
                {'u': self.parent_id})
            school = db.session.get(School, self.school_id,
                                    execution_options=opts)
            if school is not None:
                try:
                    cleanup_school_cascade(self.school_id)
                    db.session.commit()
                except Exception:
                    db.session.rollback()

    def test_sync_principal_state_is_listed_before_user(self):
        """Ordering matters: the row references users.id."""
        models = [m for m, _label in SCHOOL_DELETE_ORDER]
        self.assertIn(SyncPrincipalState, models,
                      'SyncPrincipalState must be explicit in the delete order')
        self.assertLess(models.index(SyncPrincipalState), models.index(User),
                        'it must be deleted before the users it references')

    def test_school_cleanup_removes_the_principal_state_row(self):
        with self.app.app_context():
            before = db.session.execute(text(
                'SELECT count(*) FROM sync_principal_state WHERE user_id = :u'),
                {'u': self.parent_id}).scalar()
            self.assertEqual(before, 1)

            cleanup_school_cascade(self.school_id)
            db.session.commit()

            after = db.session.execute(text(
                'SELECT count(*) FROM sync_principal_state WHERE user_id = :u'),
                {'u': self.parent_id}).scalar()
        self.assertEqual(after, 0)

    def test_school_cleanup_still_completes_and_removes_the_school(self):
        """The existing behaviour must be unchanged, not merely un-crashed."""
        with self.app.app_context():
            deleted = cleanup_school_cascade(self.school_id)
            db.session.commit()

            self.assertIsNone(db.session.get(
                School, self.school_id,
                execution_options={'bypass_tenant_scope': True}))
            self.assertIsNone(db.session.get(
                User, self.parent_id,
                execution_options={'bypass_tenant_scope': True}))
            # The report still lists the pre-existing entries plus the new one.
            self.assertIn('المدرسة', deleted)
            self.assertIn('المستخدمون', deleted)
            self.assertIn('حالة مزامنة المستخدمين', deleted)

    def test_a_super_admin_row_is_not_deleted_with_a_school(self):
        """school_id NULL means "no tenant" — it must not be swept up."""
        with self.app.app_context():
            role = Role.query.filter_by(name='admin').first()
            sa = User(username=f'cl_sa_{self.suffix}',
                      email=f'cl_sa_{self.suffix}@example.test',
                      full_name='Super', role_id=role.id, school_id=None,
                      is_active=True)
            sa.set_password('Password123')
            db.session.add(sa)
            db.session.flush()
            db.session.add(SyncPrincipalState(user_id=sa.id, school_id=None,
                                              scopes_version=2))
            db.session.commit()
            sa_id = sa.id
        try:
            with self.app.app_context():
                cleanup_school_cascade(self.school_id)
                db.session.commit()
                survived = db.session.execute(text(
                    'SELECT count(*) FROM sync_principal_state WHERE user_id = :u'),
                    {'u': sa_id}).scalar()
            self.assertEqual(survived, 1,
                             'a super-admin principal is not owned by a school')
        finally:
            with self.app.app_context():
                db.session.execute(text(
                    'DELETE FROM sync_principal_state WHERE user_id = :u'),
                    {'u': sa_id})
                row = db.session.get(
                    User, sa_id, execution_options={'bypass_tenant_scope': True})
                if row is not None:
                    db.session.delete(row)
                db.session.commit()

    def test_soft_delete_of_a_user_does_not_trigger_the_fk_cascade(self):
        """B2 must bump the version itself — the cascade will not fire.

        `admin.delete_user` is a SOFT delete: it sets is_active=False and
        renames the account to `~deleted~<id>`, keeping the users row so
        historical foreign keys stay intact. So ON DELETE CASCADE never runs and
        the principal-state row survives. The resolver still returns no scopes
        because it rejects inactive users, but a B2 writer must still bump the
        version so a client already holding a cursor is told to drop its cache.
        """
        with self.app.app_context():
            user = db.session.get(
                User, self.parent_id,
                execution_options={'bypass_tenant_scope': True})
            user.is_active = False
            user.username = f'~deleted~{user.id}'
            user.password_hash = '!'
            db.session.commit()

            survived = db.session.execute(text(
                'SELECT count(*) FROM sync_principal_state WHERE user_id = :u'),
                {'u': self.parent_id}).scalar()
            self.assertEqual(survived, 1,
                             'soft delete keeps the row — no cascade fires')

            from app.services.sync_scope_manifest import resolve_attendance_scopes
            refreshed = db.session.get(
                User, self.parent_id,
                execution_options={'bypass_tenant_scope': True})
            manifest = resolve_attendance_scopes(refreshed)
        self.assertEqual(manifest.scopes, (),
                         'an inactive principal must resolve to no scopes')

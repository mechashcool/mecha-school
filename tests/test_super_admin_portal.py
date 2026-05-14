"""
Tests for the Super Admin portal.

1. test_super_admin_can_access_dashboard
   super_admin GET /admin/super/ → 200 OK.

2. test_school_manager_cannot_access_dashboard
   school_admin GET /admin/super/ → redirected (denied).

3. test_teacher_cannot_access_dashboard
   teacher GET /admin/super/ → redirected (denied).

4. test_parent_cannot_access_dashboard
   parent GET /admin/super/ → redirected (denied).

5. test_super_admin_can_access_governorate_detail
   super_admin GET /admin/super/governorates/<gov> → 200 OK.

6. test_school_manager_cannot_access_governorate_detail
   school_admin GET /admin/super/governorates/<gov> → redirected.

7. test_super_admin_can_access_school_detail
   super_admin GET /admin/super/schools/<id> → 200 OK.

8. test_school_manager_cannot_access_school_detail
   school_admin GET /admin/super/schools/<id> → redirected.

9. test_super_admin_can_access_billing_overview
   super_admin GET /admin/super/billing → 200 OK.

10. test_school_manager_cannot_access_billing_overview
    school_admin GET /admin/super/billing → redirected.

11. test_add_billing_record
    super_admin POST /admin/super/schools/<id>/billing/add → SchoolBilling created.

12. test_record_payment_updates_status
    super_admin POST /admin/super/billing/<id>/pay → amount_paid updated + status recomputed.

13. test_toggle_school_deactivates
    super_admin POST /admin/super/schools/<id>/toggle → school.is_active flips.

14. test_governorate_field_saved_on_school_create_edit
    New school gets governorate; governorate changes on edit.
"""
import unittest
from datetime import date
from decimal import Decimal
from uuid import uuid4

from flask_login import login_user

from app import create_app
from app.models import (
    db, Role, School, User, SchoolBilling, AcademicYear, AuditLog,
)


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _uid():
    return uuid4().hex[:10]


def _make_school(suffix, governorate='Baghdad'):
    s = School(
        school_name=f'TestSchool {suffix}',
        code=f'TS{suffix[:8]}',
        capacity=0,
        is_active=True,
        governorate=governorate,
        price_per_student=Decimal('10000'),
    )
    db.session.add(s)
    db.session.flush()
    AcademicYear(
        school_id=s.id,
        name=f'AY {suffix}',
        start_date=date(2025, 8, 1),
        end_date=date(2026, 6, 30),
        is_current=True,
    )
    db.session.flush()
    return s


def _make_user(suffix, role, school=None):
    u = User(
        username=f'user_{suffix}',
        email=f'user_{suffix}@test.com',
        full_name=f'User {suffix}',
        role=role,
        school=school,
        is_active=True,
    )
    u.set_password('pass')
    db.session.add(u)
    db.session.flush()
    return u


# ─────────────────────────────────────────────────────────────────────────────
#  Test suite
# ─────────────────────────────────────────────────────────────────────────────

class SuperAdminPortalTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('development')

    def setUp(self):
        self.suffix = _uid()
        self.ctx = self.app.test_request_context()
        self.ctx.push()
        self.client = self.app.test_client()

        with self.app.app_context():
            self.super_role   = Role.query.filter_by(name='super_admin').first()
            self.manager_role = Role.query.filter_by(name='school_admin').first()
            self.teacher_role = Role.query.filter_by(name='teacher').first()
            self.parent_role  = Role.query.filter_by(name='parent').first()

            for r in [self.super_role, self.manager_role, self.teacher_role]:
                self.assertIsNotNone(r, 'Run seed before tests')

            self.school = _make_school(self.suffix)
            self.super_user   = _make_user(f'sup_{self.suffix}', self.super_role,  school=None)
            self.manager_user = _make_user(f'mgr_{self.suffix}', self.manager_role, school=self.school)
            self.teacher_user = _make_user(f'tch_{self.suffix}', self.teacher_role, school=self.school)
            self.parent_user  = None
            if self.parent_role:
                self.parent_user = _make_user(f'par_{self.suffix}', self.parent_role, school=self.school)
            db.session.commit()
            # Store plain ints so objects can be safely detached
            self.school_id  = self.school.id
            self.super_id   = self.super_user.id
            self.manager_id = self.manager_user.id
            self.teacher_id = self.teacher_user.id
            self.parent_id  = self.parent_user.id if self.parent_user else None

    def tearDown(self):
        with self.app.app_context():
            SchoolBilling.query.filter_by(school_id=self.school_id).delete()
            # Audit logs reference users — delete them before users
            AuditLog.query.execution_options(bypass_tenant_scope=True)\
                .filter_by(school_id=self.school_id).delete()
            AuditLog.query.execution_options(bypass_tenant_scope=True)\
                .filter(AuditLog.user_id == self.super_id).delete()
            AcademicYear.query.execution_options(bypass_tenant_scope=True)\
                .filter_by(school_id=self.school_id).delete()
            User.query.execution_options(bypass_tenant_scope=True)\
                .filter(User.school_id == self.school_id).delete()
            User.query.execution_options(bypass_tenant_scope=True)\
                .filter_by(id=self.super_id).delete()
            School.query.filter_by(id=self.school_id).delete()
            db.session.commit()
        self.ctx.pop()

    # ── Utility ──────────────────────────────────────────────────────────────

    def _login(self, user_id):
        with self.app.app_context():
            user = User.query.execution_options(bypass_tenant_scope=True).get(user_id)
            with self.client.session_transaction() as sess:
                sess['_user_id'] = str(user.id)
                sess['_fresh'] = True

    # ── 1. super_admin can access dashboard ───────────────────────────────

    def test_super_admin_can_access_dashboard(self):
        self._login(self.super_id)
        resp = self.client.get('/admin/super/', follow_redirects=False)
        self.assertEqual(resp.status_code, 200,
                         'super_admin must get 200 on /admin/super/')

    # ── 2. school_admin cannot access dashboard ───────────────────────────

    def test_school_manager_cannot_access_dashboard(self):
        self._login(self.manager_id)
        resp = self.client.get('/admin/super/', follow_redirects=False)
        self.assertIn(resp.status_code, (301, 302),
                      'school_admin must be redirected away from /admin/super/')

    # ── 3. teacher cannot access dashboard ───────────────────────────────

    def test_teacher_cannot_access_dashboard(self):
        self._login(self.teacher_id)
        resp = self.client.get('/admin/super/', follow_redirects=False)
        self.assertIn(resp.status_code, (301, 302))

    # ── 4. parent cannot access dashboard ───────────────────────────────

    def test_parent_cannot_access_dashboard(self):
        if not self.parent_id:
            self.skipTest('parent role not seeded')
        self._login(self.parent_id)
        resp = self.client.get('/admin/super/', follow_redirects=False)
        self.assertIn(resp.status_code, (301, 302))

    # ── 5. super_admin can access governorate detail ──────────────────────

    def test_super_admin_can_access_governorate_detail(self):
        self._login(self.super_id)
        resp = self.client.get('/admin/super/governorates/Baghdad',
                               follow_redirects=False)
        self.assertEqual(resp.status_code, 200)

    # ── 6. school_manager cannot access governorate detail ────────────────

    def test_school_manager_cannot_access_governorate_detail(self):
        self._login(self.manager_id)
        resp = self.client.get('/admin/super/governorates/Baghdad',
                               follow_redirects=False)
        self.assertIn(resp.status_code, (301, 302))

    # ── 7. super_admin can access school detail ───────────────────────────

    def test_super_admin_can_access_school_detail(self):
        self._login(self.super_id)
        resp = self.client.get(f'/admin/super/schools/{self.school_id}',
                               follow_redirects=False)
        self.assertEqual(resp.status_code, 200)

    # ── 8. school_manager cannot access school detail ─────────────────────

    def test_school_manager_cannot_access_school_detail(self):
        self._login(self.manager_id)
        resp = self.client.get(f'/admin/super/schools/{self.school_id}',
                               follow_redirects=False)
        self.assertIn(resp.status_code, (301, 302))

    # ── 9. super_admin can access billing overview ────────────────────────

    def test_super_admin_can_access_billing_overview(self):
        self._login(self.super_id)
        resp = self.client.get('/admin/super/billing', follow_redirects=False)
        self.assertEqual(resp.status_code, 200)

    # ── 10. school_manager cannot access billing overview ─────────────────

    def test_school_manager_cannot_access_billing_overview(self):
        self._login(self.manager_id)
        resp = self.client.get('/admin/super/billing', follow_redirects=False)
        self.assertIn(resp.status_code, (301, 302))

    # ── 11. add billing record ────────────────────────────────────────────

    def test_add_billing_record(self):
        self._login(self.super_id)
        resp = self.client.post(
            f'/admin/super/schools/{self.school_id}/billing/add',
            data={
                'amount_due':   '50000',
                'billing_type': 'subscription',
                'due_date':     '2026-06-01',
                'description':  'اشتراك سنوي',
                'notes':        '',
            },
            follow_redirects=False,
        )
        self.assertIn(resp.status_code, (200, 301, 302))

        with self.app.app_context():
            rec = SchoolBilling.query.filter_by(school_id=self.school_id).first()
            self.assertIsNotNone(rec, 'Billing record should be created')
            self.assertEqual(rec.amount_due, Decimal('50000'))
            self.assertEqual(rec.status, 'unpaid')

    # ── 12. record payment updates status ─────────────────────────────────

    def test_record_payment_updates_status(self):
        with self.app.app_context():
            rec = SchoolBilling(
                school_id=self.school_id,
                amount_due=Decimal('100000'),
                amount_paid=Decimal('0'),
                billing_type='subscription',
                status='unpaid',
                created_by=self.super_id,
            )
            db.session.add(rec)
            db.session.commit()
            billing_id = rec.id

        self._login(self.super_id)
        resp = self.client.post(
            f'/admin/super/billing/{billing_id}/pay',
            data={
                'payment_amount': '100000',
                'payment_date':   '2026-05-06',
                'notes':          '',
            },
            follow_redirects=False,
        )
        self.assertIn(resp.status_code, (200, 301, 302))

        with self.app.app_context():
            rec = SchoolBilling.query.get(billing_id)
            self.assertEqual(rec.amount_paid, Decimal('100000'))
            self.assertEqual(rec.status, 'paid')

    # ── 13. toggle_school flips is_active ─────────────────────────────────

    def test_toggle_school_deactivates(self):
        with self.app.app_context():
            s = School.query.get(self.school_id)
            self.assertTrue(s.is_active)

        self._login(self.super_id)
        resp = self.client.post(
            f'/admin/super/schools/{self.school_id}/toggle',
            follow_redirects=False,
        )
        self.assertIn(resp.status_code, (200, 301, 302))

        with self.app.app_context():
            s = School.query.get(self.school_id)
            self.assertFalse(s.is_active, 'School should be deactivated after toggle')

        # Toggle back so tearDown cleanup works correctly
        self._login(self.super_id)
        self.client.post(
            f'/admin/super/schools/{self.school_id}/toggle',
            follow_redirects=False,
        )

    # ── 14. governorate stored on create / edit ───────────────────────────

    def test_governorate_field_saved(self):
        with self.app.app_context():
            s = School.query.get(self.school_id)
            self.assertEqual(s.governorate, 'Baghdad',
                             'governorate must be stored on the School record')

            s.governorate = 'Basra'
            db.session.commit()
            db.session.expire(s)
            s2 = School.query.get(self.school_id)
            self.assertEqual(s2.governorate, 'Basra')


if __name__ == '__main__':
    unittest.main()

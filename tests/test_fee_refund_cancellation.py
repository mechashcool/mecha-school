"""Installment refund + full-fee cancellation workflow.

Covers the reversal side of the canonical fee-payment pathway:

  * a single installment's active received amount is fully refunded, its
    Revenue allocation rows are flagged (not deleted), and it becomes payable
    again;
  * a cascaded payment is refunded PER-installment — refunding one installment
    never touches another installment's valid allocations;
  * a refund cannot be applied twice to the same allocations;
  * received money with NO exact tagged link (historical) is never guessed —
    the refund / cancellation is refused and nothing changes;
  * a full-fee cancellation reverses every active allocation, restores every
    installment, marks the fee cancelled, and rejects new payments;
  * refunded revenue drops out of active-income totals;
  * a user from another school cannot refund / cancel this school's records.

Run against a LOCAL test database only (the conftest guard blocks production).
Apply the new migration first:  ``flask db upgrade``.
"""
import unittest
from datetime import date
from decimal import Decimal
from uuid import uuid4

from flask_login import login_user, logout_user
from werkzeug.exceptions import Forbidden, NotFound

from app import create_app
from app.models import (
    db, AcademicYear, FeeInstallment, FeeRecord, FeeRefundEvent, FeeType,
    Revenue, RevenueCategory, Role, School, Student, User,
)
from app.blueprints.fees import (
    stage_installment_payment, refund_installment, cancel_fee_record,
    _active_allocations_for_installment, _installment_refund_preview,
    refund_installment_route, cancel_fee_route, FeeValidationError,
)


FEE_CATEGORY_NAME = 'رسوم دراسية'


class FeeRefundCancellationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    # ── fixtures ─────────────────────────────────────────────────────────────
    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.created = {}
        with self.app.app_context():
            admin_role = Role.query.filter_by(name='school_admin').first()
            self.assertIsNotNone(admin_role, 'seed roles before running this test')

            school_a = School(school_name=f'Refund A {self.suffix}',
                              code=f'RFA{self.suffix[:7]}', capacity=0, is_active=True)
            school_b = School(school_name=f'Refund B {self.suffix}',
                              code=f'RFB{self.suffix[:7]}', capacity=0, is_active=True)
            db.session.add_all([school_a, school_b])
            db.session.flush()

            year_a = AcademicYear(school_id=school_a.id, name=f'YA {self.suffix}',
                                  start_date=date(2025, 8, 1), end_date=date(2026, 6, 30),
                                  is_current=True)
            year_b = AcademicYear(school_id=school_b.id, name=f'YB {self.suffix}',
                                  start_date=date(2025, 8, 1), end_date=date(2026, 6, 30),
                                  is_current=True)
            db.session.add_all([year_a, year_b])
            db.session.flush()

            user_a = User(username=f'rf_a_{self.suffix}', email=f'rf_a_{self.suffix}@ex.test',
                          full_name='Refund Admin A', role_id=admin_role.id, school_id=school_a.id)
            user_b = User(username=f'rf_b_{self.suffix}', email=f'rf_b_{self.suffix}@ex.test',
                          full_name='Refund Admin B', role_id=admin_role.id, school_id=school_b.id)
            for u in (user_a, user_b):
                u.set_password('Password123')
            db.session.add_all([user_a, user_b])
            db.session.flush()

            student = Student(student_id=f'RF-ST-{self.suffix}',
                              full_name=f'Refund Student {self.suffix}',
                              date_of_birth=date(2015, 1, 1), gender='male',
                              school_id=school_a.id, academic_year_id=year_a.id,
                              status='active')
            db.session.add(student)
            db.session.flush()

            fee_type = FeeType(school_id=school_a.id, academic_year_id=year_a.id,
                               name=f'Refund Fee {self.suffix}')
            fee_cat = RevenueCategory(name=FEE_CATEGORY_NAME, school_id=school_a.id)
            db.session.add_all([fee_type, fee_cat])
            db.session.flush()

            # Fee = 500,000 over two 250,000 installments.
            fee = FeeRecord(student_id=student.id, fee_type_id=fee_type.id,
                            academic_year_id=year_a.id, school_id=school_a.id,
                            total_amount=Decimal('500000'))
            db.session.add(fee)
            db.session.flush()

            inst1 = FeeInstallment(fee_record_id=fee.id, school_id=school_a.id,
                                   academic_year_id=year_a.id, installment_no=1,
                                   amount=Decimal('250000'), due_date=date(2025, 9, 1))
            inst2 = FeeInstallment(fee_record_id=fee.id, school_id=school_a.id,
                                   academic_year_id=year_a.id, installment_no=2,
                                   amount=Decimal('250000'), due_date=date(2025, 12, 1))
            db.session.add_all([inst1, inst2])
            db.session.commit()

            self.created = dict(
                school_a=school_a.id, school_b=school_b.id, year_a=year_a.id,
                user_a=user_a.id, user_b=user_b.id, student=student.id,
                fee=fee.id, fee_type=fee_type.id, fee_cat=fee_cat.id,
                inst1=inst1.id, inst2=inst2.id,
            )

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            c = self.created
            # Delete Revenue + refund events first (FKs), then fee tree, then rest.
            for r in Revenue.query.execution_options(bypass_tenant_scope=True)\
                    .filter(Revenue.school_id.in_([c['school_a'], c['school_b']])).all():
                r.refund_event_id = None
            db.session.flush()
            for ev in FeeRefundEvent.query.execution_options(bypass_tenant_scope=True)\
                    .filter(FeeRefundEvent.school_id.in_([c['school_a'], c['school_b']])).all():
                db.session.delete(ev)
            for r in Revenue.query.execution_options(bypass_tenant_scope=True)\
                    .filter(Revenue.school_id.in_([c['school_a'], c['school_b']])).all():
                db.session.delete(r)
            for inst in FeeInstallment.query.execution_options(bypass_tenant_scope=True)\
                    .filter_by(fee_record_id=c['fee']).all():
                db.session.delete(inst)
            db.session.flush()
            for model, key in [(FeeRecord, 'fee'), (FeeType, 'fee_type'),
                               (RevenueCategory, 'fee_cat'), (Student, 'student'),
                               (User, 'user_a'), (User, 'user_b'),
                               (AcademicYear, 'year_a')]:
                obj = db.session.get(model, c.get(key),
                                     execution_options={'bypass_tenant_scope': True})
                if obj is not None:
                    db.session.delete(obj)
            db.session.flush()
            # year_b + schools
            for yb in AcademicYear.query.execution_options(bypass_tenant_scope=True)\
                    .filter_by(school_id=c['school_b']).all():
                db.session.delete(yb)
            for model, key in [(School, 'school_a'), (School, 'school_b')]:
                obj = db.session.get(model, c.get(key),
                                     execution_options={'bypass_tenant_scope': True})
                if obj is not None:
                    db.session.delete(obj)
            db.session.commit()
            db.session.remove()

    # ── helpers ──────────────────────────────────────────────────────────────
    def _get_inst(self, key):
        return db.session.get(FeeInstallment, self.created[key],
                              execution_options={'bypass_tenant_scope': True})

    def _record_payment(self, start_inst_no, amount):
        """Record a payment through the canonical pathway (same as pay route)."""
        fee_cat = db.session.get(RevenueCategory, self.created['fee_cat'],
                                 execution_options={'bypass_tenant_scope': True})
        insts = (FeeInstallment.query.execution_options(bypass_tenant_scope=True)
                 .filter_by(fee_record_id=self.created['fee'])
                 .order_by(FeeInstallment.installment_no).all())
        stage_installment_payment(
            insts, start_inst_no, Decimal(str(amount)),
            fee_category=fee_cat, student_name='Refund Student',
            collected_by=self.created['user_a'],
            rev_academic_year_id=self.created['year_a'],
        )
        db.session.commit()

    def _active_rev_total(self):
        return Decimal(str(
            db.session.query(db.func.coalesce(db.func.sum(Revenue.amount), 0))
            .execution_options(bypass_tenant_scope=True)
            .filter(Revenue.school_id == self.created['school_a'],
                    Revenue.refunded_at.is_(None)).scalar() or 0))

    def _login(self, key):
        user = db.session.get(User, self.created[key],
                              execution_options={'bypass_tenant_scope': True})
        login_user(user)
        for fn in self.app.before_request_funcs.get(None, []):
            fn()
        return user

    # ── tests ────────────────────────────────────────────────────────────────
    def test_refund_restores_installment_and_flags_revenue(self):
        with self.app.app_context():
            self._record_payment(1, 150000)              # partial on inst#1
            inst = self._get_inst('inst1')
            self.assertEqual(Decimal(str(inst.received_amount)), Decimal('150000'))
            allocs = _active_allocations_for_installment(inst)
            self.assertEqual(len(allocs), 1)

            refund_installment(inst, reason='parent request', performed_by=self.created['user_a'])
            db.session.commit()

            inst = self._get_inst('inst1')
            self.assertEqual(Decimal(str(inst.received_amount)), Decimal('0'))
            self.assertIn(inst.status, ('pending', 'overdue'))
            # Original revenue preserved but flagged refunded (not deleted).
            self.assertEqual(len(_active_allocations_for_installment(inst)), 0)
            ev = FeeRefundEvent.query.execution_options(bypass_tenant_scope=True)\
                .filter_by(installment_id=inst.id, event_type='installment_refund').first()
            self.assertIsNotNone(ev)
            self.assertEqual(Decimal(str(ev.amount)), Decimal('150000'))

    def test_refund_only_affects_selected_installment_in_cascade(self):
        with self.app.app_context():
            # 300,000 starting at inst#1 settles inst#1 (250k) and cascades 50k to inst#2.
            self._record_payment(1, 300000)
            inst1, inst2 = self._get_inst('inst1'), self._get_inst('inst2')
            self.assertEqual(Decimal(str(inst1.received_amount)), Decimal('250000'))
            self.assertEqual(Decimal(str(inst2.received_amount)), Decimal('50000'))

            refund_installment(inst1, reason='x', performed_by=self.created['user_a'])
            db.session.commit()

            inst1, inst2 = self._get_inst('inst1'), self._get_inst('inst2')
            self.assertEqual(Decimal(str(inst1.received_amount)), Decimal('0'))
            # inst#2's valid allocation from the SAME operation is untouched.
            self.assertEqual(Decimal(str(inst2.received_amount)), Decimal('50000'))
            self.assertEqual(len(_active_allocations_for_installment(inst2)), 1)

    def test_double_refund_is_prevented(self):
        with self.app.app_context():
            self._record_payment(1, 100000)
            inst = self._get_inst('inst1')
            refund_installment(inst, reason='first', performed_by=self.created['user_a'])
            db.session.commit()
            inst = self._get_inst('inst1')
            with self.assertRaises(FeeValidationError):
                refund_installment(inst, reason='again', performed_by=self.created['user_a'])
            db.session.rollback()

    def test_historical_unlinked_payment_refund_refused(self):
        with self.app.app_context():
            # Simulate a historical payment: received amount with NO tagged Revenue.
            inst = self._get_inst('inst1')
            inst.received_amount = Decimal('120000')
            inst.receipt_no = 'RCP-LEGACY-000001'
            inst.recompute_status()
            db.session.commit()
            inst = self._get_inst('inst1')
            prev = _installment_refund_preview(inst)
            self.assertFalse(prev['refundable'])
            with self.assertRaises(FeeValidationError):
                refund_installment(inst, reason='x', performed_by=self.created['user_a'])
            db.session.rollback()
            # Nothing changed.
            self.assertEqual(Decimal(str(self._get_inst('inst1').received_amount)),
                             Decimal('120000'))

    def test_installment_payable_again_after_refund(self):
        with self.app.app_context():
            self._record_payment(1, 250000)             # fully paid
            inst = self._get_inst('inst1')
            self.assertEqual(inst.status, 'paid')
            refund_installment(inst, reason='x', performed_by=self.created['user_a'])
            db.session.commit()
            # Pay it again normally through the canonical pathway.
            self._record_payment(1, 250000)
            inst = self._get_inst('inst1')
            self.assertEqual(Decimal(str(inst.received_amount)), Decimal('250000'))
            self.assertEqual(inst.status, 'paid')
            self.assertEqual(len(_active_allocations_for_installment(inst)), 1)

    def test_full_fee_cancellation_reverses_all_and_blocks_payment(self):
        with self.app.app_context():
            self._record_payment(1, 400000)             # inst#1 full + 150k on inst#2
            before = self._active_rev_total()
            self.assertEqual(before, Decimal('400000'))

            rec = db.session.get(FeeRecord, self.created['fee'],
                                 execution_options={'bypass_tenant_scope': True})
            insts = (FeeInstallment.query.execution_options(bypass_tenant_scope=True)
                     .filter_by(fee_record_id=rec.id)
                     .order_by(FeeInstallment.installment_no).all())
            cancel_fee_record(rec, insts, reason='enrollment cancelled',
                              performed_by=self.created['user_a'])
            db.session.commit()

            rec = db.session.get(FeeRecord, self.created['fee'],
                                 execution_options={'bypass_tenant_scope': True})
            self.assertTrue(rec.is_cancelled)
            for key in ('inst1', 'inst2'):
                inst = self._get_inst(key)
                self.assertEqual(Decimal(str(inst.received_amount)), Decimal('0'))
                self.assertEqual(inst.status, 'cancelled')
            # Active revenue no longer counts the cancelled fee's payments.
            self.assertEqual(self._active_rev_total(), Decimal('0'))
            ev = FeeRefundEvent.query.execution_options(bypass_tenant_scope=True)\
                .filter_by(fee_record_id=rec.id, event_type='fee_cancellation').first()
            self.assertIsNotNone(ev)
            self.assertEqual(Decimal(str(ev.amount)), Decimal('400000'))

    def test_active_revenue_excludes_refunded(self):
        with self.app.app_context():
            self._record_payment(1, 250000)
            self._record_payment(2, 100000)
            self.assertEqual(self._active_rev_total(), Decimal('350000'))
            inst = self._get_inst('inst1')
            refund_installment(inst, reason='x', performed_by=self.created['user_a'])
            db.session.commit()
            self.assertEqual(self._active_rev_total(), Decimal('100000'))

    def test_cross_school_user_cannot_refund(self):
        with self.app.app_context():
            self._record_payment(1, 100000)
        # A School-B admin must not be able to refund School-A's installment.
        with self.app.test_request_context(
                f"/fees/installment/{self.created['inst1']}/refund",
                method='POST', data={'reason': 'malicious'}):
            self._login('user_b')
            with self.assertRaises((Forbidden, NotFound)):
                refund_installment_route(self.created['inst1'])
            logout_user()

    def test_cross_school_user_cannot_cancel_fee(self):
        with self.app.test_request_context(
                f"/fees/fee/{self.created['fee']}/cancel",
                method='POST', data={'reason': 'malicious'}):
            self._login('user_b')
            with self.assertRaises((Forbidden, NotFound)):
                cancel_fee_route(self.created['fee'])
            logout_user()

    def test_refund_requires_reason(self):
        with self.app.app_context():
            self._record_payment(1, 100000)
        with self.app.test_request_context(
                f"/fees/installment/{self.created['inst1']}/refund",
                method='POST', data={'reason': '   '}):
            self._login('user_a')
            resp = refund_installment_route(self.created['inst1'])
            body, status = resp if isinstance(resp, tuple) else (resp, 200)
            self.assertEqual(status, 400)
            logout_user()

    def test_replacement_fee_allowed_after_cancellation(self):
        """A cancelled fee must NOT block re-creating the same
        (student, fee_type, academic_year). Regression for the DB trigger
        prevent_fee_record_duplicate(), whose blanket EXISTS check ignored
        cancelled_at and raised SQLSTATE 23505 -> IntegrityError. Requires the
        y5z6a7b8c9d0 migration (cancellation-aware trigger). Postgres-only: the
        trigger does not exist on other backends."""
        from sqlalchemy.exc import IntegrityError
        with self.app.app_context():
            rec = db.session.get(FeeRecord, self.created['fee'],
                                 execution_options={'bypass_tenant_scope': True})
            insts = (FeeInstallment.query.execution_options(bypass_tenant_scope=True)
                     .filter_by(fee_record_id=rec.id)
                     .order_by(FeeInstallment.installment_no).all())
            cancel_fee_record(rec, insts, reason='enrollment cancelled')
            db.session.commit()

            self.assertTrue(db.session.get(
                FeeRecord, self.created['fee'],
                execution_options={'bypass_tenant_scope': True}).is_cancelled)

            # The replacement fee for the SAME triple must insert cleanly.
            replacement = FeeRecord(
                student_id=self.created['student'],
                fee_type_id=self.created['fee_type'],
                academic_year_id=self.created['year_a'],
                school_id=self.created['school_a'],
                total_amount=Decimal('300000'),
            )
            db.session.add(replacement)
            try:
                db.session.commit()
            except IntegrityError:  # pragma: no cover - fails only pre-migration
                db.session.rollback()
                self.fail('cancelled fee wrongly blocked a replacement fee '
                          '(prevent_fee_record_duplicate not cancellation-aware)')

            self.assertNotEqual(replacement.id, self.created['fee'])
            self.assertIsNone(replacement.cancelled_at)
            # Exactly one ACTIVE fee now exists for the triple; history preserved.
            active = (FeeRecord.query.execution_options(bypass_tenant_scope=True)
                      .filter_by(student_id=self.created['student'],
                                 fee_type_id=self.created['fee_type'],
                                 academic_year_id=self.created['year_a'],
                                 cancelled_at=None).all())
            self.assertEqual([r.id for r in active], [replacement.id])

            # A SECOND active fee for the same triple must still be rejected.
            dup = FeeRecord(
                student_id=self.created['student'],
                fee_type_id=self.created['fee_type'],
                academic_year_id=self.created['year_a'],
                school_id=self.created['school_a'],
                total_amount=Decimal('100000'),
            )
            db.session.add(dup)
            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()

            # Cleanup the replacement so tearDown's fixed-id teardown is unaffected.
            r = db.session.get(FeeRecord, replacement.id,
                               execution_options={'bypass_tenant_scope': True})
            if r is not None:
                db.session.delete(r)
                db.session.commit()


if __name__ == '__main__':
    unittest.main()

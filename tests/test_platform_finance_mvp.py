"""Five lightweight architecture checks; no production/shared DB access."""
from pathlib import Path
import unittest

from app import create_app
from app.models import (AnnualSchoolAgreement, PlatformSchoolInvoice,
                        PlatformInvoiceStudentCoverage, PlatformSchoolPayment,
                        PlatformPaymentAllocation)

ROOT = Path(__file__).resolve().parents[1]


class PlatformFinanceMvpTest(unittest.TestCase):
    def test_five_isolated_models_and_money_constraints(self):
        self.assertEqual(AnnualSchoolAgreement.__tablename__, 'annual_school_agreements')
        self.assertEqual(PlatformSchoolInvoice.__tablename__, 'platform_school_invoices')
        self.assertEqual(PlatformInvoiceStudentCoverage.__tablename__, 'platform_invoice_student_coverage')
        self.assertEqual(PlatformSchoolPayment.__tablename__, 'platform_school_payments')
        self.assertEqual(PlatformPaymentAllocation.__tablename__, 'platform_payment_allocations')
        self.assertIn('BIGINT', str(PlatformSchoolPayment.__table__.c.amount.type).upper())

    def test_coverage_and_idempotency_database_guards(self):
        coverage_indexes = {i.name: i for i in PlatformInvoiceStudentCoverage.__table__.indexes}
        self.assertTrue(coverage_indexes['uq_platform_effective_student_coverage'].unique)
        self.assertTrue(PlatformSchoolInvoice.__table__.c.submission_key.unique)
        self.assertTrue(PlatformSchoolPayment.__table__.c.submission_key.unique)

    def test_migration_is_additive_and_on_current_local_head(self):
        source = (ROOT / 'migrations/versions/x5y7z9a1b3c4_add_platform_school_finance.py').read_text('utf-8')
        self.assertIn("down_revision = 'w4x6y8z0a2b3'", source)
        self.assertNotIn("drop_table('school_billing')", source)
        self.assertNotIn("alter_column('school_billing'", source)

    def test_all_finance_routes_are_super_admin_protected(self):
        source = (ROOT / 'app/blueprints/super_admin/finance_routes.py').read_text('utf-8')
        route_count = source.count('@bp.route(')
        self.assertGreaterEqual(route_count, 9)
        self.assertEqual(route_count, source.count('@super_admin_required'))
        self.assertIn("school_id=school_id).with_for_update()", source)

    def test_pages_and_print_views_compile(self):
        app = create_app('testing')
        for template in ('super_admin/finance_school.html',
                         'super_admin/finance_print.html',
                         'super_admin/billing_overview.html'):
            app.jinja_env.get_template(template)
        rules = {r.endpoint for r in app.url_map.iter_rules()}
        self.assertIn('super_admin.finance_print_receipt', rules)
        self.assertIn('super_admin.finance_statement', rules)


if __name__ == '__main__':
    unittest.main()

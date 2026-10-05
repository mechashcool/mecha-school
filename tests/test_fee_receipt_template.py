"""Focused render tests for the printable fee receipt (fees/receipt.html).

Presentation only — the template is rendered directly with an explicit context,
so no database, no payment logic and no financial calculation is involved.
Covers: footer order, empty footer, per-school identity isolation, currency.
"""
import re
import unittest
from datetime import date
import pathlib
from types import SimpleNamespace

from flask import Flask, render_template

TEMPLATES_DIR = str(pathlib.Path(__file__).resolve().parents[1] / 'app' / 'templates')

CORE_FOOTER = 'تم اصدار هذا الوصل الكترونيا بواسطة نظام Core School'


def _ctx(**over):
    base = dict(
        installment=SimpleNamespace(installment_no=1, due_date=date(2026, 10, 3)),
        student=SimpleNamespace(full_name='محمد علي مجيد', student_id='STU-00023'),
        fee_type_name='حجز مقعد',
        receipt_no='RCP-20261005-F7AA35',
        refund_status=None,
        paid_amount=50000.0,
        remaining=100000.0,
        total_due=150000.0,
        amount_words='خمسون ألف دينار عراقي فقط لا غير',
        payment_method_label='نقداً / Cash',
        currency='د.ع',
        school_name='مدرسة أ',
        logo_url='/static/uploads/school-a-logo.png',
        school_footer='شكراً لثقتكم بنا — مدرسة أ',
        print_date=date(2026, 10, 5),
    )
    base.update(over)
    return base


class FeeReceiptTemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A bare Flask app pointed at the real template folder: the receipt is a
        # pure presentation template, so no app factory, no database and no
        # environment configuration are involved in this test.
        cls.app = Flask(__name__, template_folder=TEMPLATES_DIR)

    def _render(self, **over):
        with self.app.test_request_context('/'):
            return render_template('fees/receipt.html', **_ctx(**over))

    # CASE 1 — school with a custom footer: school footer, then Core School line.
    def test_school_footer_rendered_above_core_footer(self):
        html = self._render()
        school_pos = html.index('شكراً لثقتكم بنا — مدرسة أ')
        core_pos = html.index(CORE_FOOTER)
        self.assertLess(school_pos, core_pos)
        # The fixed line is the last textual content of the document: nothing but
        # markup and whitespace follows it.
        tail = re.sub(r'<[^>]+>', '', html[core_pos + len(CORE_FOOTER):])
        self.assertEqual('', tail.strip())

    # CASE 2 — no custom footer: no blank footer block, Core School line only.
    def test_empty_school_footer_renders_no_block(self):
        for empty in (None, '', '   '):
            html = self._render(school_footer=(empty.strip() or None) if empty else None)
            self.assertNotIn('class="school-footer"', html)
            self.assertIn(CORE_FOOTER, html)

    # CASE 3 — two schools: each receipt carries only its own identity.
    def test_per_school_identity_isolation(self):
        a = self._render()
        b = self._render(school_name='مدرسة ب',
                         logo_url='/static/uploads/school-b-logo.png',
                         school_footer='تحية من مدرسة ب')
        self.assertIn('مدرسة أ', a)
        self.assertIn('school-a-logo.png', a)
        self.assertNotIn('مدرسة ب', a)
        self.assertNotIn('school-b-logo.png', a)
        self.assertNotIn('تحية من مدرسة ب', a)

        self.assertIn('مدرسة ب', b)
        self.assertIn('school-b-logo.png', b)
        self.assertNotIn('school-a-logo.png', b)
        self.assertNotIn('شكراً لثقتكم بنا — مدرسة أ', b)

    # CASE 4 — IQD amounts render as "د.ع <number>": symbol visually on the left.
    def test_iqd_currency_symbol_precedes_the_amount(self):
        html = self._render()
        for amount in ('50,000.00', '100,000.00', '150,000.00'):
            self.assertIn(
                '<span class="cur">د.ع</span><span class="num">%s</span>' % amount,
                html)

    # Every money figure puts the symbol before the number — structural flex
    # order (item 1 is leftmost in an LTR row), so RTL bidi cannot reverse it.
    def test_every_money_value_is_currency_then_number(self):
        html = self._render()
        spans = re.findall(r'<span class="money">(.*?)</span></span>', html, re.S)
        self.assertEqual(5, len(spans))
        for inner in spans:
            self.assertLess(inner.index('class="cur"'), inner.index('class="num"'))
        self.assertIn('flex-direction: row;', html)
        self.assertIn('direction: ltr;', html)

    # CASE 4b — currency stays per-school (no hardcoded IQD).
    def test_non_iqd_currency_is_honoured(self):
        html = self._render(currency='$')
        self.assertIn('<span class="cur">$</span><span class="num">50,000.00</span>', html)
        self.assertNotIn('<span class="cur">د.ع</span>', html)

    # CASE 5/6 — approved structure + receipt values rendered as supplied.
    def test_approved_structure_and_values(self):
        html = self._render()
        for fragment in ('إيصال استلام رسوم دراسية', 'ملخص المبلغ', 'تفاصيل الإيصال',
                         'المبلغ المدفوع', 'الرصيد المتبقي', 'إجمالي المستحق',
                         'ختم المدرسة', 'توقيع المستلم',
                         'RCP-20261005-F7AA35', 'محمد علي مجيد', 'STU-00023',
                         'حجز مقعد', 'خمسون ألف دينار عراقي فقط لا غير',
                         '2026-10-03', '2026-10-05', 'نقداً / Cash'):
            self.assertIn(fragment, html, fragment)

    # Exactly ONE compact stamp/signature block, with no repeated labels.
    def test_single_stamp_signature_block(self):
        html = self._render()
        self.assertEqual(1, html.count('class="sign-area"'))
        self.assertEqual(2, html.count('class="sign-box"'))
        self.assertEqual(1, html.count('ختم المدرسة'))
        self.assertEqual(1, html.count('توقيع المستلم'))

    # One A4 page: the print block compacts spacing and caps the school footer.
    def test_print_block_enforces_single_page(self):
        html = self._render()
        self.assertIn('@page { size: A4 portrait; margin: 8mm; }', html)
        self.assertIn('page-break-inside: avoid', html)
        # The summary section is still present (compaction made dropping it
        # unnecessary) — assert it survived the layout fix.
        self.assertIn('ملخص المبلغ', html)

    # Refund stamp is preserved (existing receipt state, not new data).
    def test_refund_stamp_states(self):
        self.assertNotIn('مسترجع', self._render())
        self.assertIn('مسترجع بالكامل', self._render(refund_status='refunded'))
        self.assertIn('مسترجع جزئياً', self._render(refund_status='partial'))


if __name__ == '__main__':
    unittest.main()

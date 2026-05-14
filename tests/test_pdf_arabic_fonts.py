import unittest
from datetime import date, time
from types import SimpleNamespace

from app import create_app
from app.utils.pdf_gen import generate_fee_receipt, generate_schedule_pdf


class ArabicPdfFontTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('development')

    def _school(self):
        return SimpleNamespace(
            school_name='Mecha School',
            school_name_ar='\u0645\u062f\u0631\u0633\u0629 \u0627\u0644\u0645\u0647\u0646\u062f\u0633',
            logo_path=None,
            phone='07700000000',
            website='',
            receipt_footer='\u0634\u0643\u0631\u0627 \u0644\u062b\u0642\u062a\u0643\u0645 \u0628\u0646\u0627',
            currency_symbol='\u062f.\u0639',
        )

    def test_schedule_pdf_embeds_arabic_font(self):
        section = SimpleNamespace(
            name='\u0623',
            grade=SimpleNamespace(name='\u0627\u0644\u0623\u0648\u0644'),
        )
        entries = [
            SimpleNamespace(
                day_of_week=0,
                start_time=time(8, 0),
                end_time=time(8, 45),
                subject=SimpleNamespace(name='\u0627\u0644\u0631\u064a\u0627\u0636\u064a\u0627\u062a'),
                teacher=SimpleNamespace(full_name='\u0623\u062d\u0645\u062f \u0639\u0644\u064a'),
                room='\u0642\u0627\u0639\u0629 1',
            )
        ]
        days = [
            '\u0627\u0644\u0623\u062d\u062f',
            '\u0627\u0644\u0627\u062b\u0646\u064a\u0646',
            '\u0627\u0644\u062b\u0644\u0627\u062b\u0627\u0621',
            '\u0627\u0644\u0623\u0631\u0628\u0639\u0627\u0621',
            '\u0627\u0644\u062e\u0645\u064a\u0633',
        ]

        with self.app.app_context():
            pdf = generate_schedule_pdf(section, entries, days, self._school())

        self.assertIsNotNone(pdf)
        self.assertTrue(pdf.startswith(b'%PDF'))
        self.assertIn(b'Amiri', pdf)

    def test_receipt_pdf_embeds_arabic_font(self):
        student = SimpleNamespace(
            full_name='\u0633\u0627\u0631\u0629 \u0645\u062d\u0645\u062f',
            student_id='ST-1',
        )
        fee_record = SimpleNamespace(
            student=student,
            fee_type=SimpleNamespace(name='\u0623\u0642\u0633\u0627\u0637 \u062f\u0631\u0627\u0633\u064a\u0629'),
            installments=[],
            net_amount=100000,
        )
        installment = SimpleNamespace(
            fee_record=fee_record,
            receipt_no='R-1',
            installment_no=1,
            received_amount=25000,
            paid_date=date(2026, 5, 3),
            payment_method='cash',
        )
        fee_record.installments = [installment]

        with self.app.app_context():
            pdf = generate_fee_receipt(installment, self._school())

        self.assertIsNotNone(pdf)
        self.assertTrue(pdf.startswith(b'%PDF'))
        self.assertIn(b'Amiri', pdf)


if __name__ == '__main__':
    unittest.main()

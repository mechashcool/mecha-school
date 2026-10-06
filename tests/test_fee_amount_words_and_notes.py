# -*- coding: utf-8 -*-
"""Fees: payment amount in words (modal preview) + visible fee / payment notes.

Pinned:
  * the payment modal's live preview uses ONE shared client helper
    (templates/shared/_amount_words_iqd.html) whose output is byte-identical to
    the receipt's authoritative Python helper (amount_to_words_iqd + ' فقط لا غير')
    — checked by running the real script in Node over a wide value range;
  * the preview converts the ENTERED payment, shows nothing for empty / invalid /
    negative / out-of-range input, and never changes the submitted amount;
  * the fee note (FeeRecord.notes) is shown in the installments detail, escaped,
    only when present;
  * each payment transaction keeps its own note (Revenue.notes) — two partial
    payments show two notes; a payment without a note renders nothing; the
    installment's notes column is no longer overwritten;
  * a legacy note already stored on FeeInstallment.notes is still shown;
  * another school's admin cannot see any of it; balances are unchanged.
"""
import json
import re
import shutil
import subprocess
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock
from uuid import uuid4

from werkzeug.datastructures import MultiDict

from app import create_app
from app.models import (db, AcademicYear, AuditLog, FeeInstallment, FeeRecord,
                        FeeType, Revenue, RevenueCategory, Role, School, Student,
                        User)
from app.utils.arabic_numbers import amount_to_words_iqd

ROOT = Path(__file__).resolve().parents[1]
SHARED_JS = ROOT / 'app/templates/shared/_amount_words_iqd.html'
FEES_TABLE = ROOT / 'app/templates/fees/_fees_table.html'
OPTS = {'bypass_tenant_scope': True, 'include_all_years': True}
PASSWORD = 'Password123!'
NODE = shutil.which('node')


def _receipt_words(n):
    """Exactly what fees.generate_receipt prints for an amount."""
    words = amount_to_words_iqd(int(n))
    return (words + ' فقط لا غير') if words else '—'


def _run_js(calls):
    """Evaluate CoreAmountWords on `calls` [(fn, arg)] with the real script."""
    script = re.search(r'<script>([\s\S]*)</script>',
                       SHARED_JS.read_text(encoding='utf-8')).group(1)
    program = ('var window = {};\n' + script +
               '\nvar calls = ' + json.dumps(calls, ensure_ascii=False) + ';\n'
               'process.stdout.write(JSON.stringify(calls.map(function (c) {'
               ' return window.CoreAmountWords[c[0]](c[1]); })));')
    out = subprocess.run([NODE, '-e', program], capture_output=True, timeout=60,
                         encoding='utf-8')
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ─────────────────────────────────────────────────────────────────────────────
#  1. Amount in words — exact parity with the receipt helper
# ─────────────────────────────────────────────────────────────────────────────

@unittest.skipUnless(NODE, 'node is required to execute the client helper')
class AmountWordsParityTest(unittest.TestCase):

    def test_js_matches_python_receipt_helper_exactly(self):
        values = list(range(0, 2101))
        values += [2999, 3000, 3001, 10000, 10500, 11000, 11001, 99999, 100000,
                   100001, 101000, 102000, 110000, 125750, 200000, 250000, 300000,
                   500000, 500001, 750000, 999999, 1000000, 1000001, 1001000,
                   1500000, 2000000, 2000001, 2500000, 3000000, 10000000,
                   11000000, 12345678, 100000000, 999999999, 1000000000,
                   1000000001, 2000000000, 2500000000, 3000000000, 9999999999]
        values += [((i * 7919) % 9_999_999_999) for i in range(1, 400)]
        results = _run_js([['iqd', v] for v in values])
        for v, got in zip(values, results):
            self.assertEqual(got, amount_to_words_iqd(v), v)

    def test_receipt_text_matches_receipt_wording(self):
        raws = ['500000', '500000.00', '500000.75', '1', '2', '7', '1000',
                '2000', '1250000', '0.5', '9999999999.99', '0']
        results = _run_js([['receiptText', r] for r in raws])
        for raw, got in zip(raws, results):
            self.assertEqual(got, _receipt_words(Decimal(raw)), raw)
        self.assertEqual(results[0], 'خمسمائة ألف دينار عراقي فقط لا غير')

    def test_preview_updates_with_the_entered_value(self):
        """500000 → words; change the amount → different words; a partial
        payment is worded as entered, never as the installment total."""
        a, b, partial = _run_js([['receiptText', '500000'],
                                 ['receiptText', '750000'],
                                 ['receiptText', '125000']])
        self.assertNotEqual(a, b)
        self.assertEqual(b, _receipt_words(750000))
        self.assertEqual(partial, _receipt_words(125000))

    def test_empty_invalid_negative_and_overflow_show_nothing(self):
        raws = ['', '   ', 'abc', '-5', '-0.5', 'NaN', 'Infinity', '1e20',
                '10000000000', None]
        self.assertEqual(_run_js([['receiptText', r] for r in raws]), [''] * len(raws))


class PayModalWiringTest(unittest.TestCase):
    """The modal reads the ENTERED amount and only displays words."""

    def test_modal_preview_wiring(self):
        src = FEES_TABLE.read_text(encoding='utf-8')
        self.assertIn("{% include 'shared/_amount_words_iqd.html' %}", src)
        self.assertIn('id="rAmountWords"', src)
        self.assertIn("window.CoreAmountWords.receiptText(document.getElementById('rAmount').value)", src)
        self.assertIn("_rAmountInput.addEventListener('input', window.updatePayAmountWords)", src)
        # Reset on open and refreshed after "المبلغ المتبقي" fills the field.
        self.assertRegex(src, r"getElementById\('rAmount'\)\.value = '';\s*\n\s*window\.updatePayAmountWords\(\);")
        self.assertRegex(src, r"_lastRemaining\.toFixed\(2\);\s*\n\s*window\.updatePayAmountWords\(\);")
        # Display-only: the preview element is not a form field.
        block = re.search(r'<div id="rAmountWords"[^>]*>', src).group(0)
        self.assertNotIn('name=', block)

    def test_receipt_still_uses_the_python_helper(self):
        src = (ROOT / 'app/blueprints/fees/__init__.py').read_text(encoding='utf-8')
        self.assertIn('_words = amount_to_words_iqd(int(_actual_paid))', src)
        self.assertIn("_amount_words = (_words + ' فقط لا غير') if _words else '—'", src)


# ─────────────────────────────────────────────────────────────────────────────
#  2. Notes — DB-backed
# ─────────────────────────────────────────────────────────────────────────────

class FeeNotesDbTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app.config.update(RATELIMIT_ENABLED=False, WTF_CSRF_ENABLED=False)
        with cls.app.app_context():
            cls.admin_role_id = Role.query.filter_by(name='school_admin').first().id

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {}
        with self.app.app_context():
            for key in ('a', 'b'):
                s = School(school_name=f'FNotes {key} {self.sfx}', code=f'FN{key}{self.sfx}'[:20],
                           capacity=0, is_active=True)
                db.session.add(s)
                db.session.flush()
                y = AcademicYear(school_id=s.id, name=f'Y{key}{self.sfx}', is_current=True,
                                 start_date=date(2026, 8, 1), end_date=date(2027, 7, 31))
                u = User(username=f'fn{key}_{self.sfx}', email=f'fn{key}_{self.sfx}@t.test',
                         full_name=f'adm {key}', role_id=self.admin_role_id,
                         school_id=s.id, is_active=True)
                u.set_password(PASSWORD)
                cat = RevenueCategory(name='رسوم دراسية', school_id=s.id)
                db.session.add_all([y, u, cat])
                db.session.flush()
                st = Student(student_id=f'ST{uuid4().hex[:10]}', full_name=f'Stu {key} {self.sfx}',
                             school_id=s.id, academic_year_id=y.id)
                ft = FeeType(name=f'Tuition {key} {self.sfx}', school_id=s.id,
                             academic_year_id=y.id)
                db.session.add_all([st, ft])
                db.session.flush()
                self.ids.update({f'school_{key}': s.id, f'year_{key}': y.id,
                                 f'student_{key}': st.id, f'ft_{key}': ft.id})
            # School A: one fee WITH a creation note (multi-line + markup), one
            # fee WITHOUT a note; school B: a fee with a secret note.
            self.ids['fee_noted'] = self._fee('a', [600000, 400000],
                                              notes='خصم الأخوة\nيدفع نقداً <b>x</b>')
            self.ids['fee_plain'] = self._fee('a', [300000])
            self.ids['fee_b'] = self._fee('b', [100000], notes=f'SECRET-B-{self.sfx}')
            db.session.commit()

    def _fee(self, key, amounts, notes=None):
        rec = FeeRecord(student_id=self.ids[f'student_{key}'], fee_type_id=self.ids[f'ft_{key}'],
                        academic_year_id=self.ids[f'year_{key}'],
                        school_id=self.ids[f'school_{key}'], total_amount=sum(amounts),
                        discount=0, notes=notes)
        db.session.add(rec)
        db.session.flush()
        for no, amt in enumerate(amounts, 1):
            db.session.add(FeeInstallment(
                fee_record_id=rec.id, school_id=rec.school_id,
                academic_year_id=rec.academic_year_id, installment_no=no,
                amount=amt, due_date=date(2026, 9, 1)))
        db.session.flush()
        return rec.id

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
                for model in (Revenue, FeeInstallment, FeeRecord, FeeType, RevenueCategory,
                              Student, AuditLog, User, AcademicYear):
                    model.query.execution_options(**OPTS).filter_by(
                        school_id=sid).delete(synchronize_session=False)
                School.query.filter_by(id=sid).delete(synchronize_session=False)
            db.session.commit()
            db.session.remove()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _client(self, key='a'):
        client = self.app.test_client()
        resp = client.post('/auth/login', data={'username': f'fn{key}_{self.sfx}',
                                                'password': PASSWORD})
        self.assertEqual(resp.status_code, 302)
        return client

    def _insts(self, fee_key):
        with self.app.app_context():
            return [(i.id, i.installment_no, Decimal(str(i.amount)),
                     Decimal(str(i.received_amount or 0)), i.status, i.notes, i.receipt_no)
                    for i in FeeInstallment.query.execution_options(**OPTS)
                    .filter_by(fee_record_id=self.ids[fee_key])
                    .order_by(FeeInstallment.installment_no).all()]

    def _pay(self, client, inst_id, amount, note=None, paid='2026-09-10'):
        data = {'received_amount': str(amount), 'payment_method': 'cash', 'paid_date': paid}
        if note is not None:
            data['notes'] = note
        with mock.patch('app.blueprints.fees.finalize_payment_notifications'):
            resp = client.post(f'/fees/pay/{inst_id}', data=data)
        self.assertEqual(resp.status_code, 200, resp.data[:300])
        body = resp.get_json()
        self.assertEqual(body['status'], 'success', body)
        return body

    def _revenues(self, school_key='a'):
        with self.app.app_context():
            return [(Decimal(str(r.amount)), r.notes, r.description)
                    for r in Revenue.query.execution_options(**OPTS)
                    .filter_by(school_id=self.ids[f'school_{school_key}'])
                    .order_by(Revenue.id).all()]

    def _fees_html(self, key='a'):
        resp = self._client(key).get('/fees/')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def _inst_block(self, html, fee_key):
        # The installments panel contains nested per-installment payment tables,
        # so it is delimited by its explicit end marker, not the first </table>.
        fid = self.ids[fee_key]
        m = re.search(rf'<tr id="inst_{fid}"[\s\S]*?<!-- /inst_{fid} -->', html)
        self.assertIsNotNone(m, fee_key)
        return m.group(0)

    def _fee_row(self, html, fee_key):
        """The fee's own summary row (immediately before its installments panel)."""
        end = html.index(f'<tr id="inst_{self.ids[fee_key]}"')
        return html[html.rindex('<tr>', 0, end):end]

    def _tx_rows(self, html, inst_id):
        """[(date, row_html)] of the nested payment-transaction table of one
        installment; None when the installment is not expandable."""
        m = re.search(rf'<tr id="instTx_{inst_id}"[\s\S]*?</table>', html)
        if m is None:
            return None
        return re.findall(r'<tr>\s*<td>(\d{4}-\d{2}-\d{2})</td>([\s\S]*?)</tr>', m.group(0))

    # ── fee note ──────────────────────────────────────────────────────────────

    def test_01_fee_note_persisted_by_the_shared_creation_path(self):
        from app.blueprints.fees import persist_fee_record
        with self.app.test_request_context():
            school = db.session.get(School, self.ids['school_a'])
            form = MultiDict({'notes': '  ملاحظة الإنشاء  ', 'num_installments': '1',
                              'due_date_1': '2026-09-01'})
            rec = persist_fee_record(form, school=school, student_id=self.ids['student_a'],
                                     fee_type_id=self.ids['ft_a'],
                                     academic_year_id=self.ids['year_a'],
                                     total_amount=1000, discount=0)
            self.assertEqual(rec.notes, 'ملاحظة الإنشاء')
            db.session.rollback()

    def test_02_fee_note_shown_inline_escaped_and_absent_when_empty(self):
        html = self._fees_html()
        noted = self._fee_row(html, 'fee_noted')
        self.assertIn('ملاحظات الرسم:', noted)
        self.assertIn('خصم الأخوة\nيدفع نقداً &lt;b&gt;x&lt;/b&gt;', noted)
        self.assertNotIn('<b>x</b>', html)
        self.assertNotIn('ملاحظات الرسم', self._fee_row(html, 'fee_plain'))
        # No standalone note box inside the installments panel any more.
        self.assertNotIn('خصم الأخوة', self._inst_block(html, 'fee_noted'))

    # ── payment notes ─────────────────────────────────────────────────────────

    def test_03_two_partial_payments_keep_their_own_notes(self):
        client = self._client()
        inst1 = self._insts('fee_noted')[0][0]
        self._pay(client, inst1, 100000, note='الدفعة الأولى — نقداً من الأب', paid='2026-09-10')
        self._pay(client, inst1, 150000, note='الدفعة الثانية <i>تحويل</i>', paid='2026-09-20')
        revs = self._revenues()
        self.assertEqual([(a, n) for a, n, _ in revs],
                         [(Decimal('100000.00'), 'الدفعة الأولى — نقداً من الأب'),
                          (Decimal('150000.00'), 'الدفعة الثانية <i>تحويل</i>')])
        for _, _, desc in revs:                       # description stays machine-only
            self.assertNotIn('الدفعة', desc)
            self.assertRegex(desc, r' - RCP-\d{8}-\w+ \[TXN:RCP-\d{8}-\w+\]$')
        # Installment notes column is no longer overwritten.
        self.assertIsNone(self._insts('fee_noted')[0][5])
        html = self._fees_html()
        block = self._inst_block(html, 'fee_noted')
        self.assertNotIn('pay-note', block)                 # no loose note rows
        self.assertIn('تفاصيل الدفعات (2)', block)
        rows = self._tx_rows(html, inst1)
        self.assertEqual([d for d, _ in rows], ['2026-09-10', '2026-09-20'])
        first, second = rows[0][1], rows[1][1]
        self.assertIn('100,000', first)
        self.assertIn('الدفعة الأولى — نقداً من الأب', first)
        self.assertIn('150,000', second)
        self.assertIn('الدفعة الثانية &lt;i&gt;تحويل&lt;/i&gt;', second)
        self.assertNotIn('<i>تحويل</i>', html)
        op_refs = re.findall(r'\[TXN:([^\]]+)\]', ' '.join(d for _, _, d in revs))
        # Each row carries its own receipt reference + an exact-op print action.
        self.assertIn(op_refs[0], first); self.assertIn(f'data-op="{op_refs[0]}"', first)
        self.assertIn(op_refs[1], second); self.assertIn(f'data-op="{op_refs[1]}"', second)
        self.assertNotIn(op_refs[1], first)

    def test_04_payment_without_note_shows_empty_note_cell(self):
        client = self._client()
        inst = self._insts('fee_plain')[0][0]
        self._pay(client, inst, 50000)
        self._pay(client, inst, 25000, note='   ')
        self.assertEqual([n for _, n, _ in self._revenues()], [None, None])
        html = self._fees_html()
        self.assertNotIn('pay-note', self._inst_block(html, 'fee_plain'))
        rows = self._tx_rows(html, inst)
        self.assertEqual(len(rows), 2)
        for _, row in rows:
            self.assertIn('<td class="inst-tx-note">', row)
            self.assertNotIn('fee-note-text', row)

    def test_05_cascaded_payment_is_one_transaction_with_one_note(self):
        client = self._client()
        i1, i2 = [r[0] for r in self._insts('fee_noted')]
        self._pay(client, i1, 700000, note='دفعة تغطي قسطين')
        revs = self._revenues()
        self.assertEqual([(a, n) for a, n, _ in revs],
                         [(Decimal('600000.00'), 'دفعة تغطي قسطين'),
                          (Decimal('100000.00'), 'دفعة تغطي قسطين')])
        self.assertEqual(len({re.search(r'\[TXN:([^\]]+)\]', d).group(1)
                              for _, _, d in revs}), 1)
        html = self._fees_html()
        block = self._inst_block(html, 'fee_noted')
        self.assertEqual(block.count('دفعة تغطي قسطين'), 2)

    def test_06_balances_and_statuses_unchanged(self):
        client = self._client()
        inst1, inst2 = [r[0] for r in self._insts('fee_noted')]
        self._pay(client, inst1, 100000, note='a')
        self._pay(client, inst1, 600000, note='b')       # settles 1, cascades 100000
        rows = self._insts('fee_noted')
        self.assertEqual([(r[3], r[4]) for r in rows],
                         [(Decimal('600000.00'), 'paid'), (Decimal('100000.00'), 'partial')])
        self.assertEqual(sum(a for a, _, _ in self._revenues()), Decimal('700000.00'))
        # Receipt wording for the cumulative installment reprint is unchanged.
        resp = client.get(f'/fees/receipt/{inst1}')
        if resp.status_code == 200:
            self.assertIn(_receipt_words(600000), resp.get_data(as_text=True))

    def test_07_legacy_installment_note_still_visible(self):
        with self.app.app_context():
            inst = (FeeInstallment.query.execution_options(**OPTS)
                    .filter_by(fee_record_id=self.ids['fee_plain']).first())
            inst.notes = 'ملاحظة قديمة'
            db.session.commit()
        block = self._inst_block(self._fees_html(), 'fee_plain')
        self.assertIn('ملاحظة دفع سابقة', block)
        self.assertIn('ملاحظة قديمة', block)

    def test_08_student_profile_shows_the_same_notes(self):
        client = self._client()
        inst1 = self._insts('fee_noted')[0][0]
        self._pay(client, inst1, 100000, note='من صفحة الطالب')
        html = client.get(f"/students/{self.ids['student_a']}").get_data(as_text=True)
        self.assertIn('ملاحظات الرسم:', html)
        self.assertIn('من صفحة الطالب', html)

    # ── isolation ─────────────────────────────────────────────────────────────

    def test_09_other_school_cannot_see_notes(self):
        client_a = self._client('a')
        inst1 = self._insts('fee_noted')[0][0]
        self._pay(client_a, inst1, 100000, note=f'PAY-A-{self.sfx}')
        html_b = self._fees_html('b')
        self.assertIn(f'SECRET-B-{self.sfx}', html_b)           # its own note
        self.assertNotIn(f'PAY-A-{self.sfx}', html_b)
        self.assertNotIn('خصم الأخوة', html_b)
        html_a = self._fees_html('a')
        self.assertNotIn(f'SECRET-B-{self.sfx}', html_a)
        resp = self._client('b').get(f"/students/{self.ids['student_a']}")
        self.assertNotIn(f'PAY-A-{self.sfx}', resp.get_data(as_text=True))
        # The bulk helper never binds another school's revenue to an installment.
        from app.blueprints.fees import payment_notes_by_installment
        with self.app.test_request_context():
            insts = (FeeInstallment.query.execution_options(**OPTS)
                     .filter(FeeInstallment.fee_record_id.in_(
                         [self.ids['fee_noted'], self.ids['fee_b']])).all())
            notes = payment_notes_by_installment(insts)
            self.assertEqual([p['note'] for ps in notes.values() for p in ps],
                             [f'PAY-A-{self.sfx}'])

    def test_10_bulk_note_lookup_is_one_query(self):
        from sqlalchemy import event
        from app.blueprints.fees import payment_notes_by_installment
        client = self._client()
        for inst_id, *_ in self._insts('fee_noted'):
            self._pay(client, inst_id, 1000, note='n')
        with self.app.test_request_context():
            insts = (FeeInstallment.query.execution_options(**OPTS)
                     .filter_by(school_id=self.ids['school_a']).all())
            count = {'n': 0}

            def _cnt(conn, cursor, statement, *a):
                if statement.lstrip().upper().startswith('SELECT'):
                    count['n'] += 1
            event.listen(db.engine, 'before_cursor_execute', _cnt)
            try:
                result = payment_notes_by_installment(insts)
            finally:
                event.remove(db.engine, 'before_cursor_execute', _cnt)
            self.assertEqual(count['n'], 1)
            self.assertEqual(sum(len(v) for v in result.values()), 2)

    # ── nested payment history ────────────────────────────────────────────────

    def test_11_history_lists_every_payment_in_one_query_and_school_scoped(self):
        from sqlalchemy import event
        from app.blueprints.fees import payment_history_by_installment
        client = self._client()
        inst1 = self._insts('fee_noted')[0][0]
        self._pay(client, inst1, 1000)                       # no note
        self._pay(client, inst1, 2000, note='n2')
        with self.app.test_request_context():
            insts = (FeeInstallment.query.execution_options(**OPTS)
                     .filter(FeeInstallment.fee_record_id.in_(
                         [self.ids['fee_noted'], self.ids['fee_b']])).all())
            count = {'n': 0}

            def _cnt(conn, cursor, statement, *a):
                if statement.lstrip().upper().startswith('SELECT'):
                    count['n'] += 1
            event.listen(db.engine, 'before_cursor_execute', _cnt)
            try:
                hist = payment_history_by_installment(insts)
            finally:
                event.remove(db.engine, 'before_cursor_execute', _cnt)
            self.assertEqual(count['n'], 1)
            self.assertEqual(list(hist), [inst1])            # nothing from school B
            self.assertEqual([(p['amount'], p['note'], p['refunded']) for p in hist[inst1]],
                             [(Decimal('1000.00'), '', False), (Decimal('2000.00'), 'n2', False)])
            self.assertTrue(all(p['op_ref'] for p in hist[inst1]))

    def test_12_single_full_payment_row_stays_clean(self):
        client = self._client()
        inst = self._insts('fee_plain')[0][0]
        self._pay(client, inst, 300000)
        html = self._fees_html()
        self.assertIsNone(self._tx_rows(html, inst))
        self.assertNotIn('تفاصيل الدفعات', self._inst_block(html, 'fee_plain'))

    def test_13_multi_payment_completed_installment_expands(self):
        client = self._client()
        inst = self._insts('fee_plain')[0][0]
        self._pay(client, inst, 100000)
        self._pay(client, inst, 200000)
        self.assertEqual(self._insts('fee_plain')[0][4], 'paid')
        html = self._fees_html()
        rows = self._tx_rows(html, inst)
        self.assertEqual(len(rows), 2)
        self.assertIn('100,000', rows[0][1]); self.assertIn('200,000', rows[1][1])
        op_refs = re.findall(r'\[TXN:([^\]]+)\]', ' '.join(d for _, _, d in self._revenues()))
        self.assertEqual(re.findall(r'data-op="([^"]+)"', ''.join(r for _, r in rows)), op_refs)


if __name__ == '__main__':
    unittest.main()

# -*- coding: utf-8 -*-
"""Static / pure-logic guarantees for audience-scoped holidays and weekly days off.

No database access. Pins:
  * every production call of the holiday helpers passes an explicit audience
    (a missed call site cannot silently apply the wrong audience);
  * get_working_days() no longer reads the legacy weekly value directly (the
    old redundant check could override the employee schedule);
  * parse_weekly_off_days() is byte-for-byte the legacy parsing;
  * effective-dated resolution semantics;
  * the School Calendar template has no inline JS built from user data
    (stored-XSS regression);
  * the two migrations form a single, additive chain.
"""
import ast
import re
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'app'


def _legacy_parse(raw):
    """Verbatim copy of the pre-feature parsing in is_holiday_date()."""
    if not raw:
        return set()
    try:
        return {int(d.strip()) for d in raw.split(',') if d.strip().isdigit()}
    except (ValueError, AttributeError):
        return set()


def _calls(func_names):
    """Yield (path, lineno, ast.Call) for every call of the given names in app/."""
    for path in APP.rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else None)
            if name in func_names:
                yield path, node.lineno, node


def _audience_value(call, positional_index=None):
    for kw in call.keywords:
        if kw.arg == 'audience':
            return kw.value
    if positional_index is not None and len(call.args) > positional_index:
        return call.args[positional_index]
    return None


class CallSiteGuardTest(unittest.TestCase):

    def test_every_production_call_passes_an_explicit_audience(self):
        missing = []
        for path, line, call in _calls({'is_holiday_date', 'get_off_dates'}):
            if _audience_value(call) is None:
                missing.append(f'{path.relative_to(ROOT)}:{line}')
        for path, line, call in _calls({'resolve_weekly_off_days'}):
            if _audience_value(call, positional_index=1) is None:
                missing.append(f'{path.relative_to(ROOT)}:{line}')
        self.assertEqual(missing, [], 'holiday helper called without audience=')

    def test_literal_audiences_are_valid(self):
        bad = []
        for path, line, call in _calls({'is_holiday_date', 'get_off_dates',
                                        'resolve_weekly_off_days'}):
            value = _audience_value(call, positional_index=None)
            if isinstance(value, ast.Constant) and value.value not in (
                    'students', 'employees'):
                bad.append(f'{path.relative_to(ROOT)}:{line} {value.value!r}')
        self.assertEqual(bad, [])

    def test_student_and_employee_call_sites_use_their_audience(self):
        expected = {
            'app/blueprints/attendance/__init__.py': ('students', 4),
            'app/services/auto_attendance.py': ('students', 4),
            'app/utils/seeder.py': ('students', 1),
            'app/utils/employee_attendance_helper.py': ('employees', 1),
        }
        for rel, (audience, count) in expected.items():
            found = [
                _audience_value(call).value
                for path, _line, call in _calls({'is_holiday_date', 'get_off_dates'})
                if path == ROOT / rel
            ]
            self.assertEqual(found, [audience] * count, rel)

    def test_get_working_days_has_no_direct_weekly_check(self):
        path = APP / 'utils' / 'employee_attendance_helper.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == 'get_working_days')
        attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        self.assertNotIn('weekly_off_days', attrs)
        self.assertNotIn('weekday', attrs)


class WeeklyParsingTest(unittest.TestCase):

    CASES = (None, '', '4,5', '4, 5', ' 4 ,5 ', '5,4,4', '4,,5', '4,x,5', '9',
             '4,9', '-1,4', '0,1,2,3,4,5,6', 'abc', ',', '٤,٥', '²,4')

    def test_parse_matches_legacy_exactly(self):
        from app.utils.attendance_helpers import parse_weekly_off_days
        for raw in self.CASES:
            self.assertEqual(set(parse_weekly_off_days(raw)), _legacy_parse(raw),
                             repr(raw))

    def test_serialize_is_canonical(self):
        from app.utils.attendance_helpers import serialize_weekly_off_days
        self.assertEqual(serialize_weekly_off_days([5, 4, 4]), '4,5')
        self.assertEqual(serialize_weekly_off_days([]), '')
        self.assertEqual(serialize_weekly_off_days(range(7)), '0,1,2,3,4,5,6')
        pattern = re.compile(r'^([0-6](,[0-6]){0,6})?$')   # DB CHECK constraint
        for days in ([], [4], [4, 5], list(range(7))):
            self.assertRegex(serialize_weekly_off_days(days), pattern)

    def test_resolution_uses_latest_row_on_or_before_date(self):
        from app.utils.attendance_helpers import _resolve_from_schedule
        legacy = frozenset({4, 5})
        schedule = [(date(2026, 3, 1), frozenset({4})),
                    (date(2026, 4, 1), frozenset())]
        self.assertEqual(_resolve_from_schedule([], legacy, date(2026, 5, 1)), legacy)
        self.assertEqual(_resolve_from_schedule(schedule, legacy, date(2026, 2, 28)), legacy)
        self.assertEqual(_resolve_from_schedule(schedule, legacy, date(2026, 3, 1)),
                         frozenset({4}))
        self.assertEqual(_resolve_from_schedule(schedule, legacy, date(2026, 3, 31)),
                         frozenset({4}))
        self.assertEqual(_resolve_from_schedule(schedule, legacy, date(2026, 4, 1)),
                         frozenset())

    def test_invalid_audience_is_rejected_before_any_query(self):
        from app.utils.attendance_helpers import is_holiday_date, get_off_dates
        with self.assertRaises(ValueError):
            is_holiday_date(date(2026, 3, 2), None, None, audience='teachers')
        with self.assertRaises(ValueError):
            get_off_dates(date(2026, 3, 2), date(2026, 3, 3), None, audience='all')


class TemplateXssRegressionTest(unittest.TestCase):

    def test_calendar_template_has_no_inline_handler_with_template_data(self):
        html = (APP / 'templates' / 'school_calendar' / 'index.html').read_text(
            encoding='utf-8')
        inline = re.findall(r'\son[a-z]+\s*=\s*"[^"]*\{\{', html)
        self.assertEqual(inline, [], 'inline event handler built from template data')
        self.assertIn('data-confirm="حذف العطلة «{{ h.name }}»؟"', html)

    def test_calendar_template_offers_all_three_audiences(self):
        from app.blueprints.school_calendar import APPLIES_TO_LABELS, WEEKLY_AUDIENCE_LABELS
        self.assertEqual(APPLIES_TO_LABELS, {
            'both': 'الطلاب والموظفون',
            'students': 'الطلاب فقط',
            'employees': 'الموظفون فقط',
        })
        self.assertEqual(WEEKLY_AUDIENCE_LABELS, {
            'students': 'أيام العطلة الأسبوعية للطلاب',
            'employees': 'أيام العطلة الأسبوعية للموظفين',
        })

    def test_employee_report_hint_links_to_the_calendar(self):
        html = (APP / 'templates' / 'employees' / 'attendance_report.html').read_text(
            encoding='utf-8')
        self.assertIn("url_for('school_calendar.index')", html)
        self.assertNotIn('school.weekly_off_days', html)


class MigrationChainTest(unittest.TestCase):

    def _revisions(self):
        revs, downs, sources = {}, set(), {}
        for f in (ROOT / 'migrations' / 'versions').glob('*.py'):
            s = f.read_text(encoding='utf-8')
            r = re.search(r"^revision\s*=\s*['\"]([^'\"]+)", s, re.M)
            d = re.search(r"^down_revision\s*=\s*(.+)$", s, re.M)
            if not r:
                continue
            revs[r.group(1)] = d.group(1) if d else None
            sources[r.group(1)] = s
            if d:
                downs.update(re.findall(r"['\"]([^'\"]+)['\"]", d.group(1)))
        return revs, downs, sources

    def test_single_head_and_chain(self):
        revs, downs, sources = self._revisions()
        heads = sorted(r for r in revs if r not in downs)
        self.assertEqual(heads, ['w2k3o4f5f6s7'])
        self.assertIn("'e5m6p7d8s9p0'", revs['a1u2d3h4o5l6'])
        self.assertIn("'a1u2d3h4o5l6'", revs['w2k3o4f5f6s7'])

    def test_migrations_never_rewrite_existing_rows(self):
        _revs, _downs, sources = self._revisions()
        for rev in ('a1u2d3h4o5l6', 'w2k3o4f5f6s7'):
            src = sources[rev]
            for stmt in ('UPDATE ', 'INSERT ', 'DELETE ', 'bulk_insert'):
                self.assertNotIn(stmt, src.split('def upgrade')[1], (rev, stmt))

    def test_model_contract(self):
        from app.models import SchoolHoliday, SchoolWeeklyOffSchedule
        col = SchoolHoliday.__table__.c.applies_to
        self.assertFalse(col.nullable)
        self.assertEqual(col.server_default.arg, 'both')
        self.assertEqual(SchoolHoliday.APPLIES_TO_CHOICES,
                         ('both', 'students', 'employees'))
        self.assertTrue(SchoolWeeklyOffSchedule.__school_scoped__)
        self.assertFalse(SchoolWeeklyOffSchedule.__table__.c.school_id.nullable)
        from app.utils.scoping import _models
        school_scoped, year_scoped = _models()
        self.assertIn(SchoolWeeklyOffSchedule, school_scoped)
        self.assertNotIn(SchoolWeeklyOffSchedule, year_scoped)


if __name__ == '__main__':
    unittest.main()

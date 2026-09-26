# -*- coding: utf-8 -*-
"""Institute "جلسات الحضور" lesson (الحصة) filter — focused checks only.

  1. The lesson list holds exactly the chosen date's lessons, in scope.
  2. A lesson narrows every group to that start–end slot.
  3. Group + lesson narrows to that one lesson and keeps its take link.
  4. A malformed or unknown lesson is ignored, never widened or errored.
  5. Instructor and cross-institute scope still apply with a lesson.
  6. Group-specific empty state.
  7. A school-type institution still cannot reach the page.
  8. Viewing with a lesson filter writes nothing.

Fixture inherited from the queue tests (Sunday 2025-09-07, 17:00):
  A-Group: 10-12, 16-18, 19-20   B-Group: 16-18   C-Group: 19:30-21
  O-Group (other institute): 16-18
"""
import re
import unittest
from datetime import time
from unittest.mock import patch

from flask_login import logout_user
from werkzeug.exceptions import Forbidden

from app.services import institute_attendance as att

from tests.test_institute_attendance_queue import (InstituteAttendanceQueueTest,
                                                   NOW)

_Fixture = InstituteAttendanceQueueTest
del InstituteAttendanceQueueTest


class InstituteLessonFilterTest(_Fixture):

    @staticmethod
    def _lesson_options(html):
        block = html.split('id="fLesson"')[1].split('</select>')[0]
        return re.findall(r'<option value="([^"]*)"', block)

    @staticmethod
    def _times(html):
        return re.findall(r'(\d\d:\d\d) – \d\d:\d\d\s*</span>', html)

    # ── 1. Options = that day's lessons only ───────────────────────────────

    def test_lesson_options_follow_date_and_group(self):
        html = self._page('uadmin')
        self.assertEqual(self._lesson_options(html),
                         ['', '10:00-12:00', '16:00-18:00', '19:00-20:00',
                          '19:30-21:00'])
        self.assertIn('جميع الحصص', html)
        self.assertIn('جميع المجموعات', html)

        group_a = self._page('uadmin', group_id=self.ids['ga'])
        self.assertEqual(self._lesson_options(group_a),
                         ['', '10:00-12:00', '16:00-18:00', '19:00-20:00'])

        monday = self._page('uadmin', date='2025-09-08')
        self.assertEqual(self._lesson_options(monday), ['', '17:00-19:00'])

    # ── 2. Lesson across all groups ─────────────────────────────────────────

    def test_lesson_narrows_every_group(self):
        html = self._page('uadmin', lesson='16:00-18:00')
        self.assertEqual(sorted(self._cards(html)), ['A-Group', 'B-Group'])
        self.assertEqual(set(self._states(html)), {'current'})
        self.assertEqual(self._times(html), ['16:00', '16:00'])
        self.assertIn('<option value="16:00-18:00" selected>', html)

    # ── 3. Group + lesson -> one lesson with its own take link ──────────────

    def test_group_and_lesson_select_one_lesson(self):
        html = self._page('uadmin', group_id=self.ids['ga'], lesson='19:00-20:00')
        self.assertEqual(self._cards(html), ['A-Group'])
        self.assertEqual(self._states(html), ['upcoming'])
        self.assertIn(f'/{self.ids["ga"]}/attendance/2025-09-07?start=19:00', html)
        self.assertNotIn('?start=16:00', html)

    # ── 4. Malformed / unknown lesson is ignored ────────────────────────────

    def test_bad_lesson_values_are_ignored(self):
        full = self._states(self._page('uadmin'))
        for bad in ('garbage', '16:00', '25:00-26:00', '16:00-17:00',
                    "16:00-18:00' OR 1=1", '-'):
            html = self._page('uadmin', lesson=bad)
            self.assertEqual(self._states(html), full, bad)
            self.assertNotIn('selected>', html.split('id="fLesson"')[1]
                             .split('</select>')[0], bad)

    # ── 5. Scope still applies ──────────────────────────────────────────────

    def test_instructor_and_tenant_scope_with_lesson(self):
        # Instructor of A only: B also meets 16-18 but must stay hidden.
        html = self._page('ua', lesson='16:00-18:00')
        self.assertEqual(self._cards(html), ['A-Group'])
        self.assertNotIn('B-Group', html)
        # Forged other-institute group id drops out; its lesson never shows.
        forged = self._page('uadmin', group_id=self.ids['go'],
                            lesson='16:00-18:00')
        self.assertNotIn('O-Group', forged)
        self.assertEqual(sorted(self._cards(forged)), ['A-Group', 'B-Group'])
        # Another instructor's group id is also dropped for an instructor.
        other = self._page('ua', group_id=self.ids['gb'], lesson='16:00-18:00')
        self.assertEqual(self._cards(other), ['A-Group'])

    # ── 6. Group-specific empty state ───────────────────────────────────────

    def test_group_empty_state(self):
        html = self._page('uadmin', group_id=self.ids['gb'], date='2025-09-08')
        self.assertIn('لا توجد حصص مجدولة لهذه المجموعة في التاريخ المحدد.', html)
        self.assertEqual(self._lesson_options(html), [''])

    # ── 7. School-type institution unchanged: still 403 ─────────────────────

    def test_school_type_still_forbidden(self):
        from app.blueprints.institute_groups import attendance_sessions
        with patch.object(att, 'local_now', return_value=NOW):
            with self.app.test_request_context(
                    '/institute-groups/attendance',
                    query_string={'lesson': '16:00-18:00'}):
                self._login('us')
                try:
                    with self.assertRaises(Forbidden):
                        attendance_sessions()
                finally:
                    logout_user()

    # ── 8. Nothing written ──────────────────────────────────────────────────

    def test_lesson_filter_writes_nothing(self):
        before = self._row_counts()
        self._page('uadmin', lesson='16:00-18:00')
        self._page('uadmin', group_id=self.ids['ga'], lesson='10:00-12:00')
        self._page('ua', lesson='bad')
        self.assertEqual(self._row_counts(), before)

    # ── Parser unit check ───────────────────────────────────────────────────

    def test_parse_lesson_arg(self):
        from app.blueprints.institute_groups import _parse_lesson_arg
        self.assertEqual(_parse_lesson_arg('16:00-18:00'),
                         (time(16, 0), time(18, 0)))
        for bad in (None, '', '16:00', '16:00-', 'x-y', '24:00-25:00'):
            self.assertIsNone(_parse_lesson_arg(bad), bad)


for _inherited in list(vars(_Fixture)):
    if _inherited.startswith('test_'):
        setattr(InstituteLessonFilterTest, _inherited, None)
# The queue fixture also nulls its own parents' tests; those stay None here.

del _Fixture


if __name__ == '__main__':
    unittest.main()

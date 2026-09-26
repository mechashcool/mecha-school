# -*- coding: utf-8 -*-
"""Institute "جلسات الحضور" daily queue — focused checks only.

  1. No date -> TODAY only (institute local time).
  2. A group with several periods today is ONE card with one row per period.
  3. Groups with a current/due lesson come before groups with only upcoming
     ones; lessons inside a group are ordered by start time.
  4. A recorded lesson keeps "مراجعة وتعديل" and moves to "مسجلة اليوم".
  5. Another date shows that date's lessons, without today-only states.
  6. Instructor / tenant scope is unchanged.
  7. Viewing and filtering write nothing.

Fixture inherited from the institute attendance tests.
"""
import re
import unittest
from datetime import datetime, time
from unittest.mock import patch

from flask_login import logout_user

from app.models import (db, InstituteAttendanceRecord, InstituteAttendanceSession,
                        InstituteStudyGroup, School)
from app.services import institute_attendance as att

from tests.test_institute_attendance import InstituteAttendanceTest, OPTS

_Fixture = InstituteAttendanceTest
del InstituteAttendanceTest

# Sunday 2025-09-07, 17:00 institute local time.
NOW = datetime(2025, 9, 7, 17, 0)


class InstituteAttendanceQueueTest(_Fixture):

    def setUp(self):
        super().setUp()
        with self.app.app_context():
            ids = self.ids
            # Group A on Sunday: 10-12 (due), 16-18 (current), 19-20 (upcoming).
            self._slot(ids['inst'], ids['iyear'], ids['ga'], 0, time(10, 0), time(12, 0))
            self._slot(ids['inst'], ids['iyear'], ids['ga'], 0, time(19, 0), time(20, 0))
            # Group C: only an upcoming Sunday lesson.
            gc = self._group(ids['inst'], ids['iyear'], ids['isubj'], 'C-Group', ids['eb'])
            self._slot(ids['inst'], ids['iyear'], gc.id, 0, time(19, 30), time(21, 0))
            db.session.commit()
            ids['gc'] = gc.id

    def _page(self, user_key, **args):
        from app.blueprints.institute_groups import attendance_sessions
        with patch.object(att, 'local_now', return_value=NOW):
            with self.app.test_request_context('/institute-groups/attendance',
                                               query_string=args):
                self._login(user_key)
                try:
                    return attendance_sessions()
                finally:
                    logout_user()

    @staticmethod
    def _cards(html):
        """Group names in page order (card headers only, not the filter)."""
        return re.findall(r'<span class="fw-600">([^<]+)</span>', html)

    @staticmethod
    def _states(html):
        return re.findall(r'data-state="(\w+)"', html)

    def _row_counts(self):
        with self.app.app_context():
            return (InstituteAttendanceSession.query.execution_options(**OPTS).count(),
                    InstituteAttendanceRecord.query.execution_options(**OPTS).count())

    # ── 1-3. Today only, group once, urgency order ──────────────────────────

    def test_today_groups_once_most_urgent_first(self):
        html = self._page('uadmin')
        self.assertIn('حصص اليوم', html)
        self.assertIn('2025-09-07', html)
        self.assertNotIn('17:00 – 19:00', html, 'Monday lesson must not show')

        cards = self._cards(html)
        self.assertEqual(cards.count('A-Group'), 1, 'group rendered once')
        # A (due + current) and B (current) before C (upcoming only).
        self.assertLess(cards.index('A-Group'), cards.index('C-Group'))
        self.assertLess(cards.index('B-Group'), cards.index('C-Group'))

        # A's three periods, by start time: due, current, upcoming; then B, C.
        a_block = html.split('>A-Group<')[1].split('</ul>')[0]
        self.assertEqual(self._states(a_block), ['due', 'current', 'upcoming'])
        self.assertIn('مستحقة للتسجيل', a_block)
        self.assertIn('جارية الآن', a_block)
        self.assertIn('قادمة اليوم', a_block)
        # Each period keeps its own attendance_take identity.
        for start in ('10:00', '16:00', '19:00'):
            self.assertIn(f'/{self.ids["ga"]}/attendance/2025-09-07?start={start}',
                          a_block)

    # ── 4. Recorded lesson -> "مراجعة وتعديل", in the recorded section ───────

    def test_recorded_lesson_keeps_review_action(self):
        with self.app.app_context():
            school = db.session.get(School, self.ids['inst'], execution_options=OPTS)
            group = db.session.get(InstituteStudyGroup, self.ids['gb'],
                                   execution_options=OPTS)
            occ = att.find_occurrence(school, group, NOW.date(), time(16, 0))
            session = att.get_or_create_session(school, group, occ)
            att.submit_attendance(school, session, {self.ids['s_b']: 'present'},
                                  source='manual_admin',
                                  actor_user_id=self.ids['uadmin'])

        html = self._page('uadmin')
        pending, recorded = html.split('id="recordedSection"')
        self.assertNotIn('>B-Group<', pending)
        self.assertIn('>B-Group<', recorded)
        self.assertIn('تم التسجيل', recorded)
        self.assertIn('مراجعة وتعديل', recorded)
        self.assertIn('مسجلة اليوم (1)', recorded)
        self.assertIn('>A-Group<', pending)

    # ── 5. Another date ─────────────────────────────────────────────────────

    def test_other_date_shows_that_days_lessons(self):
        html = self._page('uadmin', date='2025-09-08')          # Monday
        self.assertEqual(self._cards(html), ['A-Group'])
        self.assertEqual(self._states(html), ['unrecorded'])
        self.assertIn('17:00 – 19:00', html)
        self.assertNotIn('حصص اليوم', html)
        for today_only in ('جارية الآن', 'قادمة اليوم', 'مستحقة للتسجيل'):
            self.assertNotIn(today_only, html)
        empty = self._page('uadmin', date='2025-09-12')         # Friday
        self.assertIn('لا توجد حصص مجدولة في هذا اليوم.', empty)

    # ── 6. Instructor / tenant scope unchanged ──────────────────────────────

    def test_instructor_scope_unchanged(self):
        html = self._page('ua')                  # instructor of group A only
        self.assertEqual(self._cards(html), ['A-Group'])
        forged = self._page('ua', group_id=self.ids['go'])      # other institute
        self.assertEqual(self._cards(forged), ['A-Group'])
        self.assertNotIn('O-Group', forged)
        manager = self._page('uadmin', group_id=self.ids['go'])
        self.assertNotIn('O-Group', manager)

    # ── 7. Viewing writes nothing ───────────────────────────────────────────

    def test_viewing_and_filtering_write_nothing(self):
        before = self._row_counts()
        self._page('uadmin')
        self._page('uadmin', date='2025-09-08')
        self._page('uadmin', group_id=self.ids['ga'])
        self._page('ua')
        self.assertEqual(self._row_counts(), before)


for _inherited in list(vars(_Fixture)):
    if _inherited.startswith('test_'):
        setattr(InstituteAttendanceQueueTest, _inherited, None)

del _Fixture


if __name__ == '__main__':
    unittest.main()

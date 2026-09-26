# -*- coding: utf-8 -*-
""""إيقاف الطلاب" page — institute study-group filter, focused checks only.

  1. An institute sees the group filter + name search, not stage/grade/section.
  2. Selecting a group narrows the student lookup to its ACTIVE members.
  3. A foreign institute's group id is a 404 and exposes nobody.
  4. A school keeps the stage/grade/section cascade, and group_id is ignored.

Fixture (institute with groups A/B, a second institute, an ordinary school)
is inherited from the institute attendance tests.
"""
import unittest

from flask_login import logout_user
from werkzeug.exceptions import NotFound

from app.models import InstituteGroupEnrollment, db

from tests.test_institute_attendance import InstituteAttendanceTest

# Keep the fixture, not its tests (same guard as the outbox module).
_Fixture = InstituteAttendanceTest
del InstituteAttendanceTest


class InstituteSuspensionFilterTest(_Fixture):

    def _page(self, user_key):
        from app.blueprints.attendance import suspensions
        with self.app.test_request_context('/attendance/suspensions'):
            self._login(user_key)
            try:
                return suspensions()
            finally:
                logout_user()

    def _search(self, user_key, **args):
        from app.blueprints.attendance import suspension_search_students
        with self.app.test_request_context('/attendance/suspensions/students/search',
                                           query_string=args):
            self._login(user_key)
            try:
                resp = suspension_search_students()
                return {r['id'] for r in resp.get_json()['results']}
            finally:
                logout_user()

    def _school_admin(self):
        with self.app.app_context():
            uid = self._user(self.ids['sch'], 'sussch', self.admin_role_id).id
            db.session.commit()
            return uid

    # ── 1. Institute page ───────────────────────────────────────────────────

    def test_institute_page_has_group_filter_not_cascade(self):
        html = self._page('uadmin')
        self.assertIn('id="groupFilter"', html)
        self.assertIn('كل المجموعات', html)
        self.assertIn('A-Group', html)
        self.assertIn('B-Group', html)
        self.assertNotIn('O-Group', html, 'another institute\'s group')
        self.assertIn('id="studentSearch"', html)
        for gone in ('id="stageFilter"', 'id="gradeFilter"',
                     'id="sectionFilter"', '>المرحلة<', '>الشعبة<'):
            self.assertNotIn(gone, html)

    # ── 2. Group narrows to ACTIVE members ──────────────────────────────────

    def test_group_filter_limits_to_active_members(self):
        with self.app.app_context():
            # s_out once belonged to group A; the membership has ended.
            self._enroll(self.ids['inst'], self.ids['ga'], self.ids['s_out'],
                         ended=True)
            db.session.commit()

        ids = self.ids
        self.assertEqual(self._search('uadmin', group_id=ids['ga']),
                         {ids['s_in'], ids['s_two']},
                         'ended enrollment and inactive student excluded')
        self.assertEqual(self._search('uadmin', group_id=ids['gb']),
                         {ids['s_b']})
        self.assertEqual(self._search('uadmin', q='Student',
                                      group_id=ids['gb']), {ids['s_b']})
        # No group: the whole institute, never another one.
        everyone = self._search('uadmin', q='Student')
        self.assertTrue({ids['s_in'], ids['s_b'], ids['s_out']} <= everyone)
        self.assertNotIn(ids['o_stu'], everyone)

    # ── 3. Forged foreign group ─────────────────────────────────────────────

    def test_foreign_group_is_404(self):
        with self.assertRaises(NotFound):
            self._search('uadmin', group_id=self.ids['go'])
        with self.assertRaises(NotFound):
            self._search('uadmin', q='Student', group_id=self.ids['go'])

    # ── 4. School unchanged ─────────────────────────────────────────────────

    def test_school_page_and_search_unchanged(self):
        self.ids['usadm'] = self._school_admin()
        html = self._page('usadm')
        for kept in ('id="stageFilter"', 'id="gradeFilter"',
                     'id="sectionFilter"', '>المرحلة<', '>الصف<', '>الشعبة<',
                     'id="studentSearch"', 'loadStages();'):
            self.assertIn(kept, html)
        self.assertNotIn('id="groupFilter"', html)
        # group_id means nothing to a school: same (empty) answer as before.
        self.assertEqual(self._search('usadm', group_id=self.ids['ga']), set())


for _inherited in list(vars(_Fixture)):
    if _inherited.startswith('test_'):
        setattr(InstituteSuspensionFilterTest, _inherited, None)

del _Fixture


if __name__ == '__main__':
    unittest.main()

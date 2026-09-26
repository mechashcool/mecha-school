# -*- coding: utf-8 -*-
"""Group-scoped institute suspensions — focused checks only.

  * all-groups scope AND a legacy row (no scope) block every group;
  * a selected-groups scope blocks only the named groups — others record and
    notify normally, and a record saved before the stop is left untouched;
  * the create route accepts only the student's own active groups of this
    institute/year (a foreign or unrelated group id writes nothing), and the
    existing delete route removes the scope with the suspension;
  * a school suspension is created exactly as before, with no scope row.

Fixture inherited from the institute suspension / outbox tests.
"""
import unittest
from datetime import time, timedelta

from flask_login import logout_user

from app.models import (db, InstituteStudyGroup, InstituteSuspensionGroup,
                        InstituteSuspensionScope, School, StudentSuspension)
from app.services import institute_attendance as att

from tests.test_institute_attendance_suspension import (
    InstituteAttendanceSuspensionTest, OPTS)

_Fixture = InstituteAttendanceSuspensionTest
del InstituteAttendanceSuspensionTest


class InstituteSuspensionScopeTest(_Fixture):

    def setUp(self):
        super().setUp()
        with self.app.app_context():
            # Group C (Sunday 16:00, like A and B); s_in joins B and C too.
            gc = self._group(self.ids['inst'], self.ids['iyear'],
                             self.ids['isubj'], 'C-Group', self.ids['eb'])
            self._slot(self.ids['inst'], self.ids['iyear'], gc.id, 0,
                       time(16, 0), time(18, 0))
            self._enroll(self.ids['inst'], self.ids['gb'], self.ids['s_in'])
            self._enroll(self.ids['inst'], gc.id, self.ids['s_in'])
            db.session.commit()
            self.ids['gc'] = gc.id

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()
            (StudentSuspension.query.execution_options(**OPTS)
             .filter_by(school_id=self.ids['sch'])
             .delete(synchronize_session=False))
            db.session.commit()
        super().tearDown()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _group_session(self, group_key):
        school = db.session.get(School, self.ids['inst'], execution_options=OPTS)
        group = db.session.get(InstituteStudyGroup, self.ids[group_key],
                               execution_options=OPTS)
        sunday = self._next_dow(0)
        occ = att.find_occurrence(school, group, sunday, time(16, 0))
        return school, att.get_or_create_session(school, group, occ)

    def _scope(self, susp_id, group_keys):
        """Attach a scope: None -> all groups, list -> those groups only."""
        db.session.add(InstituteSuspensionScope(
            suspension_id=susp_id, school_id=self.ids['inst'],
            applies_to_all_groups=group_keys is None,
            groups=[InstituteSuspensionGroup(school_id=self.ids['inst'],
                                             group_id=self.ids[k])
                    for k in (group_keys or [])]))
        db.session.commit()

    def _mark(self, group_key, status='absent'):
        """Submit s_in in one group. Returns the result, or None if refused."""
        school, session = self._group_session(group_key)
        try:
            return att.submit_attendance(
                school, session, {self.ids['s_in']: status},
                source='manual_admin', actor_user_id=self.ids['uadmin'])
        except att.AttendanceError:
            return None

    def _post_create(self, user_key, student_key, **form):
        from app.blueprints.attendance import create_suspension
        sunday = self._next_dow(0)
        data = {'student_id': self.ids[student_key],
                'start_date': (sunday - timedelta(days=1)).isoformat(),
                'end_date': (sunday + timedelta(days=1)).isoformat()}
        data.update(form)
        with self.app.test_request_context('/attendance/suspensions/create',
                                           method='POST', data=data):
            self._login(user_key)
            try:
                create_suspension()
            finally:
                logout_user()

    def _scopes(self, school_key='inst'):
        out = {}
        for susp in (StudentSuspension.query.execution_options(**OPTS)
                     .filter_by(school_id=self.ids[school_key]).all()):
            scope = (InstituteSuspensionScope.query.execution_options(**OPTS)
                     .filter_by(suspension_id=susp.id).first())
            out[susp.id] = (None if scope is None else
                            (scope.applies_to_all_groups,
                             sorted(g.group_id for g in scope.groups)))
        return out

    # ── 1 & 4. All groups and legacy both block every group ────────────────

    def test_all_groups_and_legacy_scope_block_every_group(self):
        for scope in ('legacy', 'all'):
            with self.subTest(scope=scope):
                with self.app.app_context():
                    susp_id = self._suspend('s_in', self._next_dow(0))
                    if scope == 'all':
                        self._scope(susp_id, None)
                    for g in ('ga', 'gb', 'gc'):
                        self.assertIsNone(self._mark(g), g)
                    (StudentSuspension.query.execution_options(**OPTS)
                     .filter_by(id=susp_id).delete())
                    db.session.commit()
        with self.app.app_context():
            self.assertEqual(self._parent_notifications(), 0)

    # ── 2 & 6. One selected group; others normal; history untouched ────────

    def test_selected_group_blocks_only_that_group(self):
        self._enable()
        with self.app.app_context():
            # Recorded BEFORE the stop exists.
            before = self._mark('ga', 'present')
            self.assertEqual(before['created'], 1)
            _s, sess_a = self._group_session('ga')
            rec = self._records(sess_a.id)[self.ids['s_in']]
            snapshot = (rec.status, rec.recorded_at)

            self._scope(self._suspend('s_in', self._next_dow(0)), ['ga'])

            self.assertIsNone(self._mark('ga', 'absent'), 'A is blocked')
            rec = self._records(sess_a.id)[self.ids['s_in']]
            self.assertEqual((rec.status, rec.recorded_at), snapshot)
            self.assertEqual(self._jobs(), [])

            result = self._mark('gb', 'absent')
            self.assertEqual((result['created'], result['notified'],
                              result['skipped_suspended']), (1, 1, 0))
            self.assertEqual(self._parent_notifications(), 1)
            self.assertEqual(len(self._jobs()), 2, 'B notifies normally')

    # ── 3. Two selected groups; the third stays normal ──────────────────────

    def test_two_selected_groups_leave_third_normal(self):
        with self.app.app_context():
            self._scope(self._suspend('s_in', self._next_dow(0)), ['ga', 'gb'])
            self.assertIsNone(self._mark('ga', 'present'))
            self.assertIsNone(self._mark('gb', 'present'))
            self.assertEqual(self._mark('gc', 'present')['created'], 1)

    # ── 5. Create route validation, delete cascade, school unchanged ────────

    def test_create_validates_groups_and_delete_removes_scope(self):
        ids = self.ids
        # Rejected, nothing written: another institute's group, a group the
        # student is not in, an empty selection, an unknown scope value.
        self._post_create('uadmin', 's_in', scope='groups', group_ids=[ids['go']])
        self._post_create('uadmin', 's_two', scope='groups', group_ids=[ids['gb']])
        self._post_create('uadmin', 's_in', scope='groups',
                          group_ids=[ids['ga'], ids['go']])
        self._post_create('uadmin', 's_in', scope='groups')
        self._post_create('uadmin', 's_in', scope='everything')
        with self.app.app_context():
            self.assertEqual(self._scopes(), {}, 'no suspension written')
            self.assertEqual(InstituteSuspensionScope.query
                             .execution_options(**OPTS)
                             .filter_by(school_id=ids['inst']).count(), 0,
                             'no scope written')

        # Accepted: two selected groups, and all groups.
        self._post_create('uadmin', 's_in', scope='groups',
                          group_ids=[ids['ga'], ids['gc']])
        self._post_create('uadmin', 's_two', scope='all')
        with self.app.app_context():
            scopes = sorted(self._scopes().values(), key=str)
            self.assertEqual(scopes, sorted(
                [(False, sorted([ids['ga'], ids['gc']])), (True, [])], key=str))
            selected = next(sid for sid, v in self._scopes().items()
                            if v[0] is False)

        # The existing delete route removes the scope and its group rows.
        from app.blueprints.attendance import delete_suspension
        with self.app.test_request_context(
                f'/attendance/suspensions/{selected}/delete', method='POST'):
            self._login('uadmin')
            try:
                delete_suspension(selected)
            finally:
                logout_user()
        with self.app.app_context():
            self.assertEqual(InstituteSuspensionScope.query.execution_options(**OPTS)
                             .filter_by(suspension_id=selected).count(), 0)
            self.assertEqual(InstituteSuspensionGroup.query.execution_options(**OPTS)
                             .filter_by(school_id=ids['inst'])
                             .filter(InstituteSuspensionGroup.group_id == ids['gc'])
                             .count(), 0)

        # 7. School: created exactly as before — no scope row, and scope
        # fields are ignored.
        with self.app.app_context():
            ids['usadm'] = self._user(ids['sch'], 'scpadm', self.admin_role_id).id
            ids['s_sch'] = self._student(ids['sch'], ids['syear'], 'School Kid').id
            db.session.commit()
        self._post_create('usadm', 's_sch', scope='groups', group_ids=[ids['ga']])
        with self.app.app_context():
            self.assertEqual(list(self._scopes('sch').values()), [None])


for _inherited in list(vars(_Fixture)):
    if _inherited.startswith('test_'):
        setattr(InstituteSuspensionScopeTest, _inherited, None)

del _Fixture


if __name__ == '__main__':
    unittest.main()

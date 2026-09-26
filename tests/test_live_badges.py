"""Tests for the live badge-sync endpoint (/live/badges).

Verifies that the JSON counts the polling client receives are:
  * computed in the caller's own school/role scope (no cross-school leakage),
  * parent-scoped to the parent's own requests,
  * reflect database state changes once the short live cache is past (simulated
    here by clearing the in-process badge cache, exactly what the TTL does in
    production), so a resolved/read item no longer shows a stale badge,
  * gated behind authentication.
"""
import unittest
from datetime import date
from uuid import uuid4

from flask_login import login_user, logout_user

from app import create_app
from app.models import (
    db, AcademicYear, Complaint, Grade, LeaveRequest, Notification,
    NotificationRead, Role, School, Section, Student, User,
)
from app.utils import badge_cache


class LiveBadgesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.created = {}

        with self.app.app_context():
            parent_role = Role.query.filter_by(name='parent').first()
            manager_role = Role.query.filter_by(name='school_admin').first()
            self.assertIsNotNone(parent_role)
            self.assertIsNotNone(manager_role)

            school_a = School(school_name=f'LB A {self.suffix}', code=f'LBA{self.suffix[:7]}',
                              capacity=0, is_active=True)
            school_b = School(school_name=f'LB B {self.suffix}', code=f'LBB{self.suffix[:7]}',
                              capacity=0, is_active=True)
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

            grade_a = Grade(school_id=school_a.id, academic_year_id=year_a.id, name=f'GA {self.suffix}')
            grade_b = Grade(school_id=school_b.id, academic_year_id=year_b.id, name=f'GB {self.suffix}')
            db.session.add_all([grade_a, grade_b])
            db.session.flush()

            section_a = Section(school_id=school_a.id, academic_year_id=year_a.id,
                                grade_id=grade_a.id, name=f'A{self.suffix[:4]}', capacity=30)
            section_b = Section(school_id=school_b.id, academic_year_id=year_b.id,
                                grade_id=grade_b.id, name=f'B{self.suffix[:4]}', capacity=30)
            db.session.add_all([section_a, section_b])
            db.session.flush()

            student_a = Student(student_id=f'LBA-S-{self.suffix}', full_name=f'Stu A {self.suffix}',
                                date_of_birth=date(2015, 1, 1), gender='male', school_id=school_a.id,
                                academic_year_id=year_a.id, section_id=section_a.id, status='active')
            student_a2 = Student(student_id=f'LBA2-S-{self.suffix}', full_name=f'Stu A2 {self.suffix}',
                                 date_of_birth=date(2015, 3, 1), gender='female', school_id=school_a.id,
                                 academic_year_id=year_a.id, section_id=section_a.id, status='active')
            student_b = Student(student_id=f'LBB-S-{self.suffix}', full_name=f'Stu B {self.suffix}',
                                date_of_birth=date(2015, 2, 1), gender='female', school_id=school_b.id,
                                academic_year_id=year_b.id, section_id=section_b.id, status='active')
            db.session.add_all([student_a, student_a2, student_b])
            db.session.flush()

            parent_a = User(username=f'lb_parent_a_{self.suffix}', email=f'lb_pa_{self.suffix}@ex.test',
                            full_name=f'Parent A {self.suffix}', role_id=parent_role.id,
                            school_id=school_a.id, is_active=True)
            parent_a2 = User(username=f'lb_parent_a2_{self.suffix}', email=f'lb_pa2_{self.suffix}@ex.test',
                             full_name=f'Parent A2 {self.suffix}', role_id=parent_role.id,
                             school_id=school_a.id, is_active=True)
            manager_a = User(username=f'lb_mgr_a_{self.suffix}', email=f'lb_ma_{self.suffix}@ex.test',
                             full_name=f'Manager A {self.suffix}', role_id=manager_role.id,
                             school_id=school_a.id, is_active=True)
            manager_b = User(username=f'lb_mgr_b_{self.suffix}', email=f'lb_mb_{self.suffix}@ex.test',
                             full_name=f'Manager B {self.suffix}', role_id=manager_role.id,
                             school_id=school_b.id, is_active=True)
            for u in [parent_a, parent_a2, manager_a, manager_b]:
                u.set_password('Password123')
            db.session.add_all([parent_a, parent_a2, manager_a, manager_b])
            db.session.flush()
            parent_a.children = [student_a]
            parent_a2.children = [student_a2]

            db.session.commit()
            self.created = {
                'school_a_id': school_a.id, 'school_b_id': school_b.id,
                'year_a_id': year_a.id, 'year_b_id': year_b.id,
                'grade_a_id': grade_a.id, 'grade_b_id': grade_b.id,
                'section_a_id': section_a.id, 'section_b_id': section_b.id,
                'student_a_id': student_a.id, 'student_a2_id': student_a2.id,
                'student_b_id': student_b.id,
                'parent_a_id': parent_a.id, 'parent_a2_id': parent_a2.id,
                'manager_a_id': manager_a.id, 'manager_b_id': manager_b.id,
            }
        badge_cache.clear()

    def tearDown(self):
        badge_cache.clear()
        with self.app.app_context():
            db.session.rollback()
            ids = self.created
            for key in ['parent_a_id', 'parent_a2_id']:
                p = db.session.get(User, ids.get(key), execution_options={'bypass_tenant_scope': True})
                if p:
                    p.children = []
            db.session.flush()

            # NotificationRead rows cascade-delete with their Notification (FK
            # ondelete=CASCADE), so deleting notifications clears them too.
            for model in [Notification, Complaint, LeaveRequest]:
                (model.query
                 .execution_options(bypass_tenant_scope=True, include_all_years=True)
                 .filter(model.school_id.in_([ids.get('school_a_id'), ids.get('school_b_id')]))
                 .delete(synchronize_session=False))
            db.session.flush()

            for model, keys in [
                (User, ['parent_a_id', 'parent_a2_id', 'manager_a_id', 'manager_b_id']),
                (Student, ['student_a_id', 'student_a2_id', 'student_b_id']),
                (Section, ['section_a_id', 'section_b_id']),
                (Grade, ['grade_a_id', 'grade_b_id']),
                (AcademicYear, ['year_a_id', 'year_b_id']),
                (School, ['school_a_id', 'school_b_id']),
            ]:
                for key in keys:
                    obj = db.session.get(model, ids.get(key),
                                         execution_options={'bypass_tenant_scope': True})
                    if obj is not None:
                        db.session.delete(obj)
            db.session.commit()
            db.session.remove()

    def _run_before_request(self):
        for fn in self.app.before_request_funcs.get(None, []):
            rv = fn()
            if rv is not None:
                return rv
        return None

    def _poll(self, user_id):
        """Call the live.badges view as ``user_id`` and return its counts dict.

        Clears the badge cache first so the result reflects current DB state —
        the production short TTL produces the same fresh read once it expires.
        """
        from app.blueprints.live import badges
        badge_cache.clear()
        with self.app.test_request_context('/live/badges'):
            user = db.session.get(User, user_id, execution_options={'bypass_tenant_scope': True})
            login_user(user)
            self._run_before_request()
            resp = badges()
            logout_user()
        return resp.get_json()['counts']

    def _add_complaint(self, parent_id, student_id, school_id, year_id,
                       status='new', force=False):
        """Insert a complaint.

        ``force=True`` plants a row the application would normally REFUSE to
        create, by setting the same ``skip_tenant_validation`` flag that
        ``app/utils/school_cleanup.py`` uses. It is the test-only way to get a
        hostile row into the database so the READ path can be proven to reject
        it. The write guard itself is never modified — see
        ``test_cross_school_complaint_is_rejected_by_the_write_guard``.
        """
        with self.app.app_context():
            c = Complaint(parent_id=parent_id, student_id=student_id, school_id=school_id,
                          academic_year_id=year_id, title=f'C {self.suffix}',
                          complaint_type='academic', details='x', status=status)
            db.session.add(c)
            if force:
                previous = db.session.info.get('skip_tenant_validation')
                db.session.info['skip_tenant_validation'] = True
                try:
                    db.session.commit()
                finally:
                    if previous is None:
                        db.session.info.pop('skip_tenant_validation', None)
                    else:
                        db.session.info['skip_tenant_validation'] = previous
            else:
                db.session.commit()
            return c.id

    def _poll_raw(self, user_id):
        """Full badge payload, for asserting nothing extra leaks into it."""
        from app.blueprints.live import badges
        badge_cache.clear()
        with self.app.test_request_context('/live/badges'):
            user = db.session.get(User, user_id,
                                  execution_options={'bypass_tenant_scope': True})
            login_user(user)
            self._run_before_request()
            resp = badges()
            logout_user()
        return resp.get_json()

    # ── Tests ───────────────────────────────────────────────────────────────

    def test_cross_school_complaint_is_rejected_by_the_write_guard(self):
        """The normal path must refuse a complaint that spans two schools.

        This is the control that made the old version of
        ``test_admin_counts_scoped_to_own_school`` impossible to set up: the
        fixture tried to create a complaint whose parent was in school A and
        whose student/school were B, and ``_validate_relationship_scope``
        correctly refused it. Pinned here so the count test below can bypass
        the guard for its own fixture without anyone concluding the guard is
        optional.
        """
        ids = self.created
        with self.assertRaises(ValueError) as ctx:
            self._add_complaint(ids['parent_a2_id'], ids['student_b_id'],
                                ids['school_b_id'], ids['year_b_id'])
        self.assertIn('Parent request must match parent school', str(ctx.exception))
        with self.app.app_context():
            db.session.rollback()

    def test_admin_counts_scoped_to_own_school(self):
        """A hostile cross-school row must not be counted by the other school.

        School A gets a legitimate pending complaint. School B gets a row that
        the write guard would normally reject: ``school_id`` = B, but
        ``parent_id`` = a parent belonging to A. It is planted with
        ``skip_tenant_validation`` so the READ path can be tested against data
        that a bug, a bad migration, or a future code path could produce.

        Manager A must count only its own complaint — the hostile row must not
        reach A through its parent, and none of school B's identifiers may
        appear in A's payload.
        """
        ids = self.created
        complaint_a = self._add_complaint(
            ids['parent_a_id'], ids['student_a_id'],
            ids['school_a_id'], ids['year_a_id'])

        hostile = self._add_complaint(
            ids['parent_a2_id'], ids['student_b_id'],
            ids['school_b_id'], ids['year_b_id'], force=True)

        with self.app.app_context():
            planted = db.session.get(
                Complaint, hostile,
                execution_options={'bypass_tenant_scope': True})
            self.assertIsNotNone(planted, 'the hostile row must really exist')
            self.assertEqual(planted.school_id, ids['school_b_id'])
            self.assertEqual(planted.parent_id, ids['parent_a2_id'],
                             'parent belongs to school A — this is the attack')

        payload_a = self._poll_raw(ids['manager_a_id'])
        counts_a = payload_a['counts']
        counts_b = self._poll(ids['manager_b_id'])

        self.assertEqual(counts_a['pending_complaints'], 1,
                         'Manager A must count only school A complaints, even '
                         'though the school B row references a school A parent')
        self.assertEqual(counts_b['pending_complaints'], 1,
                         'Manager B must count its own school B row')

        # No school B identifier may appear anywhere in school A's response.
        import json
        blob = json.dumps(payload_a)
        for leaked in (hostile, ids['school_b_id'], ids['student_b_id'],
                       ids['year_b_id']):
            self.assertNotIn(
                str(leaked), blob,
                f'school B identifier {leaked} leaked into school A payload')
        self.assertIn(str(counts_a['pending_complaints']), blob)
        del complaint_a

    def test_parent_counts_scoped_to_own_requests(self):
        ids = self.created
        # Two parents in the SAME school, each with a pending complaint.
        self._add_complaint(ids['parent_a_id'], ids['student_a_id'],
                            ids['school_a_id'], ids['year_a_id'])
        self._add_complaint(ids['parent_a2_id'], ids['student_a2_id'],
                            ids['school_a_id'], ids['year_a_id'])

        counts = self._poll(ids['parent_a_id'])
        self.assertEqual(counts['pending_complaints'], 1,
                         "A parent must only see their own complaints, not the school's")

    def test_count_drops_after_complaint_resolved(self):
        ids = self.created
        cid = self._add_complaint(ids['parent_a_id'], ids['student_a_id'],
                                 ids['school_a_id'], ids['year_a_id'])

        before = self._poll(ids['manager_a_id'])
        self.assertEqual(before['pending_complaints'], 1)

        # Manager resolves it (status leaves the new/under_review set).
        with self.app.app_context():
            c = db.session.get(Complaint, cid, execution_options={'bypass_tenant_scope': True})
            c.status = 'replied'
            db.session.commit()

        after = self._poll(ids['manager_a_id'])
        self.assertEqual(after['pending_complaints'], 0,
                         'Resolved complaint must not leave a stale badge once the cache refreshes')

    def test_leave_request_count(self):
        ids = self.created
        with self.app.app_context():
            lr = LeaveRequest(parent_id=ids['parent_a_id'], student_id=ids['student_a_id'],
                              school_id=ids['school_a_id'], academic_year_id=ids['year_a_id'],
                              leave_type='sick', from_date=date(2026, 1, 10),
                              to_date=date(2026, 1, 12), status='pending')
            db.session.add(lr)
            db.session.commit()

        counts = self._poll(ids['manager_a_id'])
        self.assertEqual(counts['pending_leave_requests'], 1)
        # School B manager sees none.
        self.assertEqual(self._poll(ids['manager_b_id'])['pending_leave_requests'], 0)

    def test_notification_unread_then_read(self):
        ids = self.created
        with self.app.app_context():
            n = Notification(school_id=ids['school_a_id'], title='T', body='B',
                             ntype='announcement', target_user_id=ids['parent_a_id'],
                             created_by=ids['manager_a_id'])
            db.session.add(n)
            db.session.commit()
            nid = n.id

        self.assertEqual(self._poll(ids['parent_a_id'])['unread_notifications'], 1)

        # Mark read → badge clears on next refresh.
        with self.app.app_context():
            db.session.add(NotificationRead(notification_id=nid, user_id=ids['parent_a_id']))
            db.session.commit()

        self.assertEqual(self._poll(ids['parent_a_id'])['unread_notifications'], 0)

    def test_other_parent_does_not_see_notification(self):
        ids = self.created
        with self.app.app_context():
            n = Notification(school_id=ids['school_a_id'], title='T', body='B',
                             ntype='announcement', target_user_id=ids['parent_a_id'],
                             created_by=ids['manager_a_id'])
            db.session.add(n)
            db.session.commit()
        # A direct notification to parent_a must not appear for parent_a2.
        self.assertEqual(self._poll(ids['parent_a2_id'])['unread_notifications'], 0)

    def test_endpoint_requires_login(self):
        resp = self.app.test_client().get('/live/badges')
        self.assertIn(resp.status_code, (302, 401),
                      'Unauthenticated badge poll must be rejected/redirected')


if __name__ == '__main__':
    unittest.main()

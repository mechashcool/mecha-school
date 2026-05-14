import unittest
from unittest.mock import patch
from uuid import uuid4

from flask_login import login_user, logout_user

from app import create_app
from app.blueprints.broadcast import compose as legacy_broadcast_compose
from app.blueprints.notifications import create as create_notification
from app.blueprints.notifications import index as notifications_index
from app.blueprints.parent import announcements as parent_announcements
from app.blueprints.parent import dashboard as parent_dashboard
from app.models import (
    db, Announcement, Notification, NotificationRead, Role, School, User,
)


class NotificationTargetingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app('development')

    def setUp(self):
        self.suffix = uuid4().hex[:10]
        self.created = {}

        with self.app.app_context():
            admin_role = Role.query.filter_by(name='school_admin').first()
            parent_role = Role.query.filter_by(name='parent').first()
            teacher_role = Role.query.filter_by(name='teacher').first()
            self.assertIsNotNone(admin_role, 'seed roles before notification tests')
            self.assertIsNotNone(parent_role, 'seed parent role before notification tests')
            self.assertIsNotNone(teacher_role, 'seed teacher role before notification tests')

            school = School(
                school_name=f'Notify School {self.suffix}',
                code=f'NT{self.suffix[:8]}',
                capacity=0,
                is_active=True,
            )
            other_school = School(
                school_name=f'Notify Other {self.suffix}',
                code=f'NO{self.suffix[:8]}',
                capacity=0,
                is_active=True,
            )
            db.session.add_all([school, other_school])
            db.session.flush()

            sender = User(
                username=f'notify_admin_{self.suffix}',
                email=f'notify_admin_{self.suffix}@example.test',
                full_name='Notification Admin',
                role_id=admin_role.id,
                school_id=school.id,
            )
            parent_a = User(
                username=f'notify_parent_a_{self.suffix}',
                email=f'notify_parent_a_{self.suffix}@example.test',
                full_name='Notification Parent A',
                role_id=parent_role.id,
                school_id=school.id,
            )
            parent_b = User(
                username=f'notify_parent_b_{self.suffix}',
                email=f'notify_parent_b_{self.suffix}@example.test',
                full_name='Notification Parent B',
                role_id=parent_role.id,
                school_id=school.id,
            )
            other_parent = User(
                username=f'notify_parent_other_{self.suffix}',
                email=f'notify_parent_other_{self.suffix}@example.test',
                full_name='Notification Other Parent',
                role_id=parent_role.id,
                school_id=other_school.id,
            )
            teacher_a = User(
                username=f'notify_teacher_a_{self.suffix}',
                email=f'notify_teacher_a_{self.suffix}@example.test',
                full_name='Notification Teacher A',
                role_id=teacher_role.id,
                school_id=school.id,
            )
            teacher_b = User(
                username=f'notify_teacher_b_{self.suffix}',
                email=f'notify_teacher_b_{self.suffix}@example.test',
                full_name='Notification Teacher B',
                role_id=teacher_role.id,
                school_id=school.id,
            )
            for user in (sender, parent_a, parent_b, other_parent,
                         teacher_a, teacher_b):
                user.set_password('Password123')
            db.session.add_all([
                sender, parent_a, parent_b, other_parent, teacher_a, teacher_b,
            ])
            db.session.flush()

            self.created = {
                'school_id': school.id,
                'other_school_id': other_school.id,
                'sender_id': sender.id,
                'parent_a_id': parent_a.id,
                'parent_b_id': parent_b.id,
                'other_parent_id': other_parent.id,
                'teacher_a_id': teacher_a.id,
                'teacher_b_id': teacher_b.id,
            }
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.rollback()

            notifications = Notification.query.execution_options(
                bypass_tenant_scope=True
            ).filter(Notification.title.like(f'%{self.suffix}%')).all()
            for notification in notifications:
                NotificationRead.query.filter_by(
                    notification_id=notification.id
                ).delete()
                db.session.delete(notification)

            for announcement in Announcement.query.execution_options(
                bypass_tenant_scope=True
            ).filter(Announcement.title.like(f'%{self.suffix}%')).all():
                db.session.delete(announcement)

            ids = self.created
            for model, key in [
                (User, 'sender_id'),
                (User, 'parent_a_id'),
                (User, 'parent_b_id'),
                (User, 'other_parent_id'),
                (User, 'teacher_a_id'),
                (User, 'teacher_b_id'),
                (School, 'school_id'),
                (School, 'other_school_id'),
            ]:
                obj = db.session.get(
                    model,
                    ids.get(key),
                    execution_options={'bypass_tenant_scope': True},
                )
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

    def _login(self, user_id):
        user = db.session.get(
            User,
            user_id,
            execution_options={'bypass_tenant_scope': True},
        )
        login_user(user)
        self._run_before_request()
        return user

    def _visible_titles_for(self, user_id):
        with self.app.test_request_context('/notifications/'):
            self._login(user_id)
            captured = {}

            def fake_render(_template, **context):
                captured.update(context)
                return 'ok'

            with patch('app.blueprints.notifications.render_template',
                       side_effect=fake_render):
                self.assertEqual(notifications_index(), 'ok')

            titles = {n.title for n in captured['notifs'].items}
            logout_user()
            return titles

    def test_specific_parent_create_only_targets_selected_parent(self):
        title = f'Targeted Parent {self.suffix}'
        with self.app.test_request_context(
            '/notifications/create',
            method='POST',
            data={
                'title': title,
                'body': 'Only parent A should see this',
                'ntype': 'announcement',
                'target_role': '_specific_parent',
                'target_user_id': str(self.created['parent_a_id']),
            },
        ):
            self._login(self.created['sender_id'])

            response = create_notification()
            self.assertEqual(response.status_code, 302)

            notification = Notification.query.execution_options(
                bypass_tenant_scope=True
            ).filter_by(title=title).one()
            self.assertEqual(notification.target_user_id,
                             self.created['parent_a_id'])
            self.assertIsNone(notification.target_role)
            logout_user()

        self.assertIn(title, self._visible_titles_for(self.created['sender_id']))
        self.assertIn(title, self._visible_titles_for(self.created['parent_a_id']))
        self.assertNotIn(title, self._visible_titles_for(self.created['parent_b_id']))

    def test_specific_teacher_create_only_targets_selected_teacher(self):
        title = f'Targeted Teacher {self.suffix}'
        with self.app.test_request_context(
            '/notifications/create',
            method='POST',
            data={
                'title': title,
                'body': 'Only teacher A should see this',
                'ntype': 'announcement',
                'target_role': '_specific_teacher',
                'target_teacher_id': str(self.created['teacher_a_id']),
            },
        ):
            self._login(self.created['sender_id'])

            response = create_notification()
            self.assertEqual(response.status_code, 302)

            notification = Notification.query.execution_options(
                bypass_tenant_scope=True
            ).filter_by(title=title).one()
            self.assertEqual(notification.target_user_id,
                             self.created['teacher_a_id'])
            self.assertIsNone(notification.target_role)
            logout_user()

        self.assertIn(title, self._visible_titles_for(self.created['sender_id']))
        self.assertIn(title, self._visible_titles_for(self.created['teacher_a_id']))
        self.assertNotIn(title, self._visible_titles_for(self.created['teacher_b_id']))

    def test_admin_history_includes_all_audience_types(self):
        titles = {
            'all': f'History All {self.suffix}',
            'teachers': f'History Teachers {self.suffix}',
            'parent': f'History Parent {self.suffix}',
            'specific_parent': f'History Specific Parent {self.suffix}',
            'specific_teacher': f'History Specific Teacher {self.suffix}',
        }
        with self.app.app_context():
            db.session.add_all([
                Notification(
                    school_id=self.created['school_id'],
                    title=titles['all'],
                    body='All users',
                    ntype='announcement',
                    target_role=None,
                    target_user_id=None,
                    created_by=self.created['sender_id'],
                ),
                Notification(
                    school_id=self.created['school_id'],
                    title=titles['teachers'],
                    body='All teachers',
                    ntype='announcement',
                    target_role='teacher',
                    target_user_id=None,
                    created_by=self.created['sender_id'],
                ),
                Notification(
                    school_id=self.created['school_id'],
                    title=titles['parent'],
                    body='All parents',
                    ntype='announcement',
                    target_role='parent',
                    target_user_id=None,
                    created_by=self.created['sender_id'],
                ),
                Notification(
                    school_id=self.created['school_id'],
                    title=titles['specific_parent'],
                    body='One parent',
                    ntype='announcement',
                    target_user_id=self.created['parent_a_id'],
                    created_by=self.created['sender_id'],
                ),
                Notification(
                    school_id=self.created['school_id'],
                    title=titles['specific_teacher'],
                    body='One teacher',
                    ntype='announcement',
                    target_user_id=self.created['teacher_a_id'],
                    created_by=self.created['sender_id'],
                ),
            ])
            db.session.commit()

        history_titles = self._visible_titles_for(self.created['sender_id'])
        for title in titles.values():
            self.assertIn(title, history_titles)

    def test_create_form_target_options_are_limited(self):
        with self.app.test_request_context('/notifications/create'):
            self._login(self.created['sender_id'])

            html = create_notification()
            for label in (
                '>الكل<', '>التدريسيين<', '>تدريسي محدد<',
                '>أولياء الأمور<', '>ولي أمر محدد<',
            ):
                self.assertIn(label, html)
            for old_option in ('accountant', 'hr', 'reception'):
                self.assertNotIn(f'value="{old_option}"', html)
            logout_user()

    def test_legacy_targeted_row_with_parent_role_does_not_broadcast(self):
        title = f'Legacy Targeted Parent {self.suffix}'
        with self.app.app_context():
            notification = Notification(
                school_id=self.created['school_id'],
                title=title,
                body='Legacy row with both target_user_id and target_role',
                ntype='announcement',
                target_role='parent',
                target_user_id=self.created['parent_a_id'],
                created_by=self.created['sender_id'],
            )
            db.session.add(notification)
            db.session.info['skip_tenant_validation'] = True
            try:
                db.session.commit()
            finally:
                db.session.info.pop('skip_tenant_validation', None)

        self.assertIn(title, self._visible_titles_for(self.created['parent_a_id']))
        self.assertNotIn(title, self._visible_titles_for(self.created['parent_b_id']))

    def test_parent_role_broadcast_still_reaches_all_parents_in_school(self):
        title = f'Parent Broadcast {self.suffix}'
        with self.app.app_context():
            notification = Notification(
                school_id=self.created['school_id'],
                title=title,
                body='All parents in this school should see this',
                ntype='announcement',
                target_role='parent',
                target_user_id=None,
                created_by=self.created['sender_id'],
            )
            db.session.add(notification)
            db.session.commit()

        self.assertIn(title, self._visible_titles_for(self.created['parent_a_id']))
        self.assertIn(title, self._visible_titles_for(self.created['parent_b_id']))

    def test_cannot_target_parent_from_another_school(self):
        title = f'Cross School Target {self.suffix}'
        with self.app.test_request_context(
            '/notifications/create',
            method='POST',
            data={
                'title': title,
                'body': 'Should not be created',
                'ntype': 'announcement',
                'target_role': '_specific_parent',
                'target_user_id': str(self.created['other_parent_id']),
            },
        ):
            self._login(self.created['sender_id'])

            response, status = create_notification()
            self.assertEqual(status, 403)

            count = Notification.query.execution_options(
                bypass_tenant_scope=True
            ).filter_by(title=title).count()
            self.assertEqual(count, 0)
            logout_user()

    def test_parent_dashboard_uses_notifications_not_announcements(self):
        notification_title = f'Dashboard Notification {self.suffix}'
        other_title = f'Dashboard Other Parent {self.suffix}'
        old_announcement_title = f'Dashboard Announcement {self.suffix}'

        with self.app.app_context():
            db.session.add_all([
                Notification(
                    school_id=self.created['school_id'],
                    title=notification_title,
                    body='Visible direct notification',
                    ntype='announcement',
                    target_user_id=self.created['parent_a_id'],
                    created_by=self.created['sender_id'],
                ),
                Notification(
                    school_id=self.created['school_id'],
                    title=other_title,
                    body='Only parent B should see this',
                    ntype='announcement',
                    target_user_id=self.created['parent_b_id'],
                    created_by=self.created['sender_id'],
                ),
                Announcement(
                    school_id=self.created['school_id'],
                    title=old_announcement_title,
                    body='Legacy announcement should not feed parent dashboard',
                    audience='all_parents',
                    status='sent',
                    created_by=self.created['sender_id'],
                ),
            ])
            db.session.commit()

        with self.app.test_request_context('/parent/dashboard'):
            self._login(self.created['parent_a_id'])
            captured = {}

            def fake_render(_template, **context):
                captured.update(context)
                return 'ok'

            with patch('app.blueprints.parent.render_template',
                       side_effect=fake_render):
                self.assertEqual(parent_dashboard(), 'ok')

            titles = {n.title for n in captured['recent_notifications']}
            self.assertIn(notification_title, titles)
            self.assertNotIn(other_title, titles)
            self.assertNotIn(old_announcement_title, titles)
            self.assertNotIn('recent_announcements', captured)
            logout_user()

    def test_parent_announcements_url_redirects_to_notifications(self):
        with self.app.test_request_context('/parent/announcements'):
            self._login(self.created['parent_a_id'])

            response = parent_announcements()
            self.assertEqual(response.status_code, 302)
            self.assertIn('/notifications/', response.location)
            logout_user()

    def test_legacy_broadcast_composer_redirects_to_notifications(self):
        with self.app.test_request_context('/broadcast/new'):
            self._login(self.created['sender_id'])

            response = legacy_broadcast_compose()
            self.assertEqual(response.status_code, 302)
            self.assertIn('/notifications/create', response.location)
            logout_user()


if __name__ == '__main__':
    unittest.main()

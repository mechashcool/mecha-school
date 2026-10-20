# -*- coding: utf-8 -*-
"""FCM token unregister on logout + outbox recipient-ownership guard.

What is being guaranteed:

  * DELETE /me/device-token and POST /auth/logout {fcm_token} deactivate
    exactly ONE registration: the authenticated user's row for exactly the
    submitted token. Never another user's row, never the caller's other phones.
  * The response never reveals whether a submitted token existed, was already
    inactive or belongs to someone else.
  * Every supported mobile role (parent, teacher, driver, investor_viewer) can
    unregister. Unauthenticated calls are refused.
  * Logout without a token is unchanged.
  * When a token moves to a new account, no previous account keeps it in the
    legacy User.device_token field — also across schools.
  * The outbox worker never delivers a job to a token that now belongs to a
    different user (device-token-reassigned); the school-mismatch guard and the
    normal send path are unchanged.

Runs against the isolated local PostgreSQL test database. No real Firebase:
the final send is patched, and the test guard blocks Firebase regardless.
"""
import json
import unittest
from datetime import datetime
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import text

from app import create_app
from app.blueprints.mobile_api.utils import encode_token
from app.models import (
    db, MobileDeviceToken, NotificationOutbox, Role, School, User,
)
from app.services import outbox_worker

OPTS = {'bypass_tenant_scope': True}
API = '/api/mobile/v1'
ROLES = ('parent', 'teacher', 'driver', 'investor_viewer')


def _fake_token(tag):
    """A fabricated registration token. Never a production value."""
    return f'tkn-unreg-{tag}-{uuid4().hex}-' + ('x' * 40)


class _Sent:
    """Stand-in for fcm_service.TokenSendResult."""

    def __init__(self, ok=True):
        self.ok = ok
        self.permanent = False
        self.error = None if ok else 'transient'
        self.message_id = 'm-1' if ok else None
        self.deactivated = False

    @property
    def transient(self):
        return not self.ok


class FcmTokenUnregisterTest(unittest.TestCase):
    """School A: one user per mobile role plus a second parent (same-school
    account switch). School B: one parent (cross-school account switch)."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    # ── Fixture ──────────────────────────────────────────────────────────────

    def setUp(self):
        self.client = self.app.test_client()
        self.suffix = uuid4().hex[:10]
        self.ids = {'users': []}
        with self.app.app_context():
            roles = {name: Role.query.filter_by(name=name).first()
                     for name in ROLES}
            for name, role in roles.items():
                self.assertIsNotNone(role, f'seed role {name!r} first')

            for tag in ('a', 'b'):
                school = School(school_name=f'Unreg {tag} {self.suffix}',
                                code=f'UR{tag.upper()}{self.suffix[:6]}',
                                capacity=0, is_active=True)
                db.session.add(school)
                db.session.flush()
                self.ids[f'school_{tag}'] = school.id

            accounts = [('parent', 'a', 'parent'), ('parent2', 'a', 'parent'),
                        ('teacher', 'a', 'teacher'), ('driver', 'a', 'driver'),
                        ('investor', 'a', 'investor_viewer'),
                        ('parent_b', 'b', 'parent')]
            for key, tag, role in accounts:
                user = User(username=f'ur_{key}_{self.suffix}',
                            password_hash='!', full_name=f'Unreg {key}',
                            role_id=roles[role].id,
                            school_id=self.ids[f'school_{tag}'],
                            is_active=True)
                db.session.add(user)
                db.session.flush()
                self.ids[key] = user.id
                self.ids['users'].append(user.id)
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            sids = [self.ids['school_a'], self.ids['school_b']]
            for table in ('notification_outbox', 'push_notifications',
                          'mobile_device_tokens'):
                db.session.execute(
                    text(f'DELETE FROM {table} WHERE school_id = ANY(:s)'),
                    {'s': sids})
            for model, ids in ((User, self.ids['users']), (School, sids)):
                for pk in ids:
                    row = db.session.get(model, pk, execution_options=OPTS)
                    if row is not None:
                        db.session.delete(row)
                db.session.flush()
            db.session.commit()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _headers(self, key):
        # A fresh client per authenticated call: the mobile app keeps no cookie
        # jar, so the previous account's Flask session cookie (set by
        # login_user in jwt_required) must not ride along to the next account.
        self.client = self.app.test_client()
        with self.app.app_context():
            user = db.session.get(User, self.ids[key], execution_options=OPTS)
            return {'Authorization': f'Bearer {encode_token(user)}'}

    def _register_row(self, key, token, active=True):
        """A MobileDeviceToken row owned by ``key``; returns its id."""
        with self.app.app_context():
            user = db.session.get(User, self.ids[key], execution_options=OPTS)
            row = MobileDeviceToken(user_id=user.id, school_id=user.school_id,
                                    fcm_token=token, platform='android',
                                    is_active=active)
            db.session.add(row)
            db.session.commit()
            return row.id

    def _set_legacy(self, key, token):
        with self.app.app_context():
            user = db.session.get(User, self.ids[key], execution_options=OPTS)
            user.device_token = token
            db.session.commit()

    def _legacy(self, key):
        with self.app.app_context():
            return db.session.get(User, self.ids[key],
                                  execution_options=OPTS).device_token

    def _row(self, token):
        with self.app.app_context():
            rows = (MobileDeviceToken.query.execution_options(**OPTS)
                    .filter_by(fcm_token=token).all())
            self.assertLessEqual(len(rows), 1, 'fcm_token must stay unique')
            if not rows:
                return None
            r = rows[0]
            return {'id': r.id, 'user_id': r.user_id,
                    'school_id': r.school_id, 'is_active': r.is_active}

    def _delete(self, key, body):
        return self.client.delete(f'{API}/me/device-token', json=body,
                                  headers=self._headers(key))

    def _queue_job(self, owner_key, token_row_id):
        """A pending outbox job for ``owner_key`` targeting that token row."""
        with self.app.app_context():
            owner = db.session.get(User, self.ids[owner_key],
                                   execution_options=OPTS)
            now = datetime.utcnow()
            job = NotificationOutbox(
                school_id=owner.school_id,
                event_type=NotificationOutbox.EVENT_SCHOOL_ATTENDANCE_MANUAL,
                user_id=owner.id, device_token_id=token_row_id,
                title='t', body='b', data_json=json.dumps({'type': 'x'}),
                ntype='attendance', dedup_key=f'unreg-{uuid4().hex}',
                status=NotificationOutbox.STATUS_PENDING, attempts=0,
                next_attempt_at=now, created_at=now)
            db.session.add(job)
            db.session.commit()
            return job.id

    def _deliver(self, job_id, send_result=None):
        """Run the real worker on one job; returns (outcome, status,
        last_error, send mock)."""
        with self.app.app_context():
            job = db.session.get(NotificationOutbox, job_id,
                                 execution_options=OPTS)
            sent_to = []

            def fake_send(token_row, *args, **kwargs):
                sent_to.append(token_row.id)
                return send_result or _Sent(True)

            with patch('app.services.fcm_service.send_to_device_token',
                       side_effect=fake_send) as send:
                outcome = outbox_worker.deliver_one(job, max_attempts=5)
            fresh = db.session.get(NotificationOutbox, job_id,
                                   execution_options=OPTS)
            send.sent_to = sent_to
            return outcome, fresh.status, fresh.last_error, send

    # ── 1-3. DELETE /me/device-token: exact (user, token) scope ─────────────

    def test_delete_deactivates_own_token(self):
        token = _fake_token('own')
        self._register_row('parent', token)
        resp = self._delete('parent', {'fcm_token': token})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {'ok': True})
        self.assertFalse(self._row(token)['is_active'])

    def test_delete_one_device_keeps_the_other_device_active(self):
        t1, t2 = _fake_token('dev1'), _fake_token('dev2')
        self._register_row('parent', t1)
        self._register_row('parent', t2)
        resp = self._delete('parent', {'fcm_token': t1})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self._row(t1)['is_active'])
        self.assertTrue(self._row(t2)['is_active'],
                        'logout on one phone must not silence the other')

    def test_delete_of_another_users_token_is_a_noop_with_same_response(self):
        same_school, other_school = _fake_token('p2'), _fake_token('pb')
        self._register_row('parent2', same_school)
        self._register_row('parent_b', other_school)
        own = _fake_token('mine')
        self._register_row('parent', own)

        expected = self._delete('parent', {'fcm_token': own}).get_json()
        for token in (same_school, other_school, _fake_token('never-seen')):
            resp = self._delete('parent', {'fcm_token': token})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.get_json(), expected,
                             'response must not reveal existence/ownership')
        self.assertTrue(self._row(same_school)['is_active'])
        self.assertEqual(self._row(same_school)['user_id'], self.ids['parent2'])
        self.assertTrue(self._row(other_school)['is_active'])
        self.assertEqual(self._row(other_school)['user_id'],
                         self.ids['parent_b'])
        self.assertIsNone(self._row(_fake_token('never-seen')))

    def test_delete_is_idempotent_and_accepts_device_token_key(self):
        token = _fake_token('idem')
        self._register_row('parent', token)
        for _ in range(2):
            resp = self._delete('parent', {'device_token': token})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.get_json(), {'ok': True})
        self.assertFalse(self._row(token)['is_active'])

    def test_delete_without_token_changes_nothing(self):
        token = _fake_token('keep')
        self._register_row('parent', token)
        for body in ({}, {'fcm_token': '   '}, {'fcm_token': 123},
                     {'fcm_token': 'y' * 600}):
            resp = self._delete('parent', body)
            self.assertEqual(resp.status_code, 400)
        self.assertTrue(self._row(token)['is_active'],
                        'no token supplied must never mean "all tokens"')

    # ── 4. Authentication ───────────────────────────────────────────────────

    def test_delete_unauthenticated_is_refused(self):
        token = _fake_token('anon')
        self._register_row('parent', token)
        for headers in ({}, {'Authorization': 'Bearer not-a-jwt'}):
            resp = self.client.delete(f'{API}/me/device-token',
                                      json={'fcm_token': token},
                                      headers=headers)
            self.assertEqual(resp.status_code, 401)
        self.assertTrue(self._row(token)['is_active'])

    def test_refresh_token_cannot_unregister(self):
        token = _fake_token('refresh')
        self._register_row('parent', token)
        with self.app.app_context():
            user = db.session.get(User, self.ids['parent'],
                                  execution_options=OPTS)
            refresh = encode_token(user, 'refresh')
        resp = self.client.delete(f'{API}/me/device-token',
                                  json={'fcm_token': token},
                                  headers={'Authorization': f'Bearer {refresh}'})
        self.assertEqual(resp.status_code, 401)
        self.assertTrue(self._row(token)['is_active'])

    # ── 5-8. Every supported mobile role ────────────────────────────────────

    def test_every_mobile_role_can_unregister(self):
        for key in ('parent', 'teacher', 'driver', 'investor'):
            with self.subTest(role=key):
                token = _fake_token(key)
                self._register_row(key, token)
                resp = self._delete(key, {'fcm_token': token})
                self.assertEqual(resp.status_code, 200)
                self.assertFalse(self._row(token)['is_active'])

    # ── 9-10. POST /auth/logout ─────────────────────────────────────────────

    def test_logout_with_token_unregisters_only_that_token(self):
        token, other_phone = _fake_token('logout'), _fake_token('other')
        self._register_row('parent', token)
        self._register_row('parent', other_phone)
        foreign = _fake_token('foreign')
        self._register_row('parent2', foreign)

        resp = self.client.post(f'{API}/auth/logout',
                                json={'fcm_token': token},
                                headers=self._headers('parent'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {'ok': True, 'message': 'logged_out'})
        self.assertFalse(self._row(token)['is_active'])
        self.assertTrue(self._row(other_phone)['is_active'])

        resp = self.client.post(f'{API}/auth/logout',
                                json={'device_token': foreign},
                                headers=self._headers('parent'))
        self.assertEqual(resp.get_json(), {'ok': True, 'message': 'logged_out'})
        self.assertTrue(self._row(foreign)['is_active'],
                        'logout must never unregister another user')

    def test_logout_without_token_is_unchanged(self):
        token = _fake_token('stay')
        self._register_row('parent', token)
        self._set_legacy('parent', token)
        for kwargs in ({}, {'json': {}}):
            resp = self.client.post(f'{API}/auth/logout',
                                    headers=self._headers('parent'), **kwargs)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.get_json(),
                             {'ok': True, 'message': 'logged_out'})
        self.assertTrue(self._row(token)['is_active'])
        self.assertEqual(self._legacy('parent'), token)

        resp = self.client.post(f'{API}/auth/logout')
        self.assertEqual(resp.status_code, 401)

    # ── 11-12. Legacy User.device_token on unregister ───────────────────────

    def test_unregister_clears_matching_legacy_field(self):
        token = _fake_token('legacy')
        self._register_row('parent', token)
        self._set_legacy('parent', token)
        self._delete('parent', {'fcm_token': token})
        self.assertIsNone(self._legacy('parent'))

    def test_unregister_keeps_a_different_legacy_field(self):
        token, newer = _fake_token('old'), _fake_token('newer')
        self._register_row('parent', token)
        self._set_legacy('parent', newer)
        self._delete('parent', {'fcm_token': token})
        self.assertEqual(self._legacy('parent'), newer)

    # ── 13-15. Outbox worker: recipient ownership at send time ──────────────

    def test_outbox_job_is_not_delivered_after_same_school_reassignment(self):
        token = _fake_token('ob-same')
        row_id = self._register_row('parent', token)
        job_id = self._queue_job('parent', row_id)

        # parent2 logs in on the same installation (real registration path).
        resp = self.client.post(f'{API}/me/device-token',
                                json={'device_token': token},
                                headers=self._headers('parent2'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._row(token)['user_id'], self.ids['parent2'])

        outcome, status, error, send = self._deliver(job_id)
        send.assert_not_called()
        self.assertEqual(outcome, 'dead')
        self.assertEqual(status, NotificationOutbox.STATUS_DEAD)
        self.assertEqual(error, 'device-token-reassigned')

    def test_outbox_cross_school_reassignment_keeps_school_mismatch(self):
        token = _fake_token('ob-cross')
        row_id = self._register_row('parent', token)
        job_id = self._queue_job('parent', row_id)

        resp = self.client.post(f'{API}/me/device-token',
                                json={'device_token': token},
                                headers=self._headers('parent_b'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._row(token)['school_id'], self.ids['school_b'])

        outcome, status, error, send = self._deliver(job_id)
        send.assert_not_called()
        self.assertEqual(status, NotificationOutbox.STATUS_DEAD)
        self.assertEqual(error, 'school-mismatch')

    def test_outbox_normal_job_is_still_sent(self):
        token = _fake_token('ob-ok')
        row_id = self._register_row('parent', token)
        job_id = self._queue_job('parent', row_id)

        outcome, status, error, send = self._deliver(job_id)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.sent_to, [row_id])
        self.assertEqual(outcome, 'sent')
        self.assertEqual(status, NotificationOutbox.STATUS_SENT)

    def test_outbox_job_after_unregister_is_not_delivered(self):
        token = _fake_token('ob-unreg')
        row_id = self._register_row('parent', token)
        job_id = self._queue_job('parent', row_id)
        self._delete('parent', {'fcm_token': token})

        outcome, status, error, send = self._deliver(job_id)
        send.assert_not_called()
        self.assertEqual(error, 'device-token-inactive')

    # ── 16-17. Registration reassignment ────────────────────────────────────

    def test_reassignment_moves_row_and_clears_previous_legacy_field(self):
        cases = (('/me/device-token', 'device_token', 'parent2'),
                 ('/auth/register-device', 'fcm_token', 'parent2'),
                 ('/me/device-token', 'device_token', 'parent_b'),
                 ('/auth/register-device', 'fcm_token', 'parent_b'))
        for path, field, new_owner in cases:
            with self.subTest(path=path, new_owner=new_owner):
                token = _fake_token('move')
                row_id = self._register_row('parent', token)
                self._set_legacy('parent', token)
                untouched = _fake_token('untouched')
                self._set_legacy('teacher', untouched)

                resp = self.client.post(f'{API}{path}', json={field: token},
                                        headers=self._headers(new_owner))
                self.assertEqual(resp.status_code, 200)

                row = self._row(token)
                self.assertEqual(row['id'], row_id, 'no duplicate row')
                self.assertEqual(row['user_id'], self.ids[new_owner])
                self.assertTrue(row['is_active'])
                self.assertIsNone(self._legacy('parent'),
                                  'previous owner keeps no legacy copy')
                self.assertEqual(self._legacy(new_owner), token)
                self.assertEqual(self._legacy('teacher'), untouched)

    def test_reregistering_an_inactive_token_reactivates_it(self):
        for path, field in (('/me/device-token', 'device_token'),
                            ('/auth/register-device', 'fcm_token')):
            with self.subTest(path=path):
                token = _fake_token('again')
                row_id = self._register_row('parent', token)
                self._delete('parent', {'fcm_token': token})
                self.assertFalse(self._row(token)['is_active'])

                resp = self.client.post(f'{API}{path}', json={field: token},
                                        headers=self._headers('parent'))
                self.assertEqual(resp.status_code, 200)
                row = self._row(token)
                self.assertEqual(row['id'], row_id)
                self.assertTrue(row['is_active'])
                self.assertEqual(row['user_id'], self.ids['parent'])


if __name__ == '__main__':
    unittest.main()

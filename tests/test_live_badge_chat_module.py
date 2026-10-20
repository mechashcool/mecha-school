# -*- coding: utf-8 -*-
"""Live badge poll — the chat-module check must be behaviour-identical.

On ``live=True`` the chat badge gate now asks PostgreSQL one single-row
aggregate (``_live_chat_module_enabled``) instead of loading every
school_modules row of the school through ``get_enabled_modules``.

What is pinned here, against a VERBATIM frozen copy of the pre-change function
(``_baseline_get_badge_counts`` below, from main @ db11c12):

  * identical /live/badges status, JSON and Cache-Control for every module
    configuration: no rows, rows without 'chat', chat on, chat off, every
    module off, near-miss keys ('Chat', 'chat '), and for school admin,
    teacher, parent and super admin;
  * identical SQL sequence except the one school_modules statement, which
    returns 1 row instead of all of the school's rows; same pool checkouts;
  * a super-admin toggle between requests (two app instances) is seen by the
    very next poll in both versions;
  * a failing school_modules query is swallowed and rolled back identically;
  * flags already memoized in the request are reused with zero queries;
  * school isolation of the new query;
  * page render (live=False) is untouched;
  * client↔server bytes per poll through the loopback relay (printed).

Runs against the isolated local PostgreSQL test database only.
"""
import os
import re
import statistics
import unittest
from unittest.mock import patch
from uuid import uuid4

from flask_login import login_user
from sqlalchemy import event, text

from app import create_app
from app.models import (db, ChatMessage, ChatRoom, ChatRoomMember, Role, School,
                        User)
from app.utils import badge_cache, sidebar_badges, ttl_cache
from app.utils.modules import MODULES, get_module_flags, save_school_modules
from app.utils.sidebar_badges import LIVE_TTL, _empty_counts  # noqa: F401 (baseline)
from tests.test_employee_auto_absence_egress import _Relay

OPTS = {'bypass_tenant_scope': True}


# ═════════════════════════════════════════════════════════════════════════════
#  Frozen baseline — verbatim copy of get_badge_counts BEFORE the change.
#  Do not edit: it is the reference the optimised version is compared with.
# ═════════════════════════════════════════════════════════════════════════════

def _baseline_get_badge_counts(*, live: bool = False) -> dict:
    """Return the four badge counts for ``current_user`` in the current scope.

    Safe to call from any authenticated request context. Returns zeros for
    counts that cannot be computed (e.g. no school context) without raising.
    """
    from flask import g
    from flask_login import current_user

    counts = _empty_counts()

    if not getattr(current_user, 'is_authenticated', False):
        return counts

    prefix = 'live:' if live else ''
    ttl = LIVE_TTL if live else badge_cache.DEFAULT_TTL

    role = current_user.role.name if current_user.role else None
    uid = current_user.id

    # ── Unread in-app notifications ─────────────────────────────────────────
    # Keyed by the tenant-scope school id (auto-scopes the Notification query;
    # changes when a super admin switches school), the user (read-state + direct
    # targeting), and role (drives the visibility filter).
    try:
        from app.models import Notification, NotificationRead
        from app.utils.notification_visibility import notification_visible_to

        notif_sid = getattr(g, 'tenant_scope_school_id', None)

        def _load_notifications():
            read_ids = (db.session.query(NotificationRead.notification_id)
                        .filter_by(user_id=uid).subquery())
            q = (Notification.query
                 .filter(notification_visible_to(current_user))
                 .filter(Notification.id.notin_(read_ids)))
            # Explicit school filter (defence-in-depth). The ORM auto-scope
            # already constrains Notification by g.tenant_scope_school_id, but
            # binding the count to notif_sid here guarantees a role-broadcast
            # from another school can never inflate this badge even if the ORM
            # scope is ever bypassed or changes. For a global super admin
            # (notif_sid is None) the behaviour is unchanged.
            if notif_sid is not None:
                q = q.filter(Notification.school_id == notif_sid)
            return q.count()

        counts['unread_notifications'] = badge_cache.get_or_set(
            (prefix + 'notif', uid, notif_sid, role),
            _load_notifications, ttl=ttl,
        )
    except Exception:
        db.session.rollback()

    # ── Complaints / leave requests / chat ──────────────────────────────────
    try:
        from app.models import (
            Complaint, LeaveRequest,
            ChatMessage, ChatMessageRead, ChatRoom, ChatRoomMember,
        )
        from app.utils.decorators import get_current_school
        from app.utils.modules import get_enabled_modules

        if (live and not current_user.is_super_admin
                and hasattr(g, 'tenant_scope_school_id')):
            # Live poll, school users only: reuse the school id _set_request_scope
            # already resolved for this request instead of loading the full
            # School row (and its selectin role cascade) via get_current_school()
            # just to read .id. A school-less user (-1) falls back to
            # current_user.school_id, exactly as the branch below does. Super
            # admins keep the original path so a stale/deleted active_school_id
            # still resolves to no current school.
            scope_sid = g.tenant_scope_school_id
            sid = (scope_sid if scope_sid and scope_sid > 0
                   else getattr(current_user, 'school_id', None))
        else:
            current_school = get_current_school()
            sid = (current_school.id
                   if current_school and hasattr(current_school, 'id')
                   else getattr(current_user, 'school_id', None))

        # Complaint + leave counts use bypass_tenant_scope + include_all_years
        # with explicit school filters, so they depend only on (school, [parent
        # user]) — the view year is intentionally NOT part of the key.
        if sid:
            if current_user.is_admin_user:
                from app.models import EmployeeLeaveRequest

                def _load_admin_complaints():
                    return (Complaint.query
                            .execution_options(bypass_tenant_scope=True, include_all_years=True)
                            .filter(Complaint.school_id == sid,
                                    Complaint.status.in_(['new', 'under_review']))
                            .count())

                def _load_admin_leaves():
                    return (LeaveRequest.query
                            .execution_options(bypass_tenant_scope=True, include_all_years=True)
                            .filter(LeaveRequest.school_id == sid,
                                    LeaveRequest.status == 'pending')
                            .count())

                def _load_admin_emp_leaves():
                    return (EmployeeLeaveRequest.query
                            .execution_options(bypass_tenant_scope=True)
                            .filter(EmployeeLeaveRequest.school_id == sid,
                                    EmployeeLeaveRequest.status == 'pending')
                            .count())

                counts['pending_complaints'] = badge_cache.get_or_set(
                    (prefix + 'complaints', sid, 'admin'), _load_admin_complaints, ttl=ttl)
                counts['pending_leave_requests'] = badge_cache.get_or_set(
                    (prefix + 'leave', sid, 'admin'), _load_admin_leaves, ttl=ttl)
                counts['pending_employee_leave_requests'] = badge_cache.get_or_set(
                    (prefix + 'emp_leave', sid, 'admin'), _load_admin_emp_leaves, ttl=ttl)
            elif role == 'parent':
                def _load_parent_complaints():
                    return (Complaint.query
                            .execution_options(bypass_tenant_scope=True, include_all_years=True)
                            .filter(Complaint.school_id == sid,
                                    Complaint.parent_id == uid,
                                    Complaint.status.in_(['new', 'under_review']))
                            .count())

                def _load_parent_leaves():
                    return (LeaveRequest.query
                            .execution_options(bypass_tenant_scope=True, include_all_years=True)
                            .filter(LeaveRequest.school_id == sid,
                                    LeaveRequest.parent_id == uid,
                                    LeaveRequest.status == 'pending')
                            .count())

                counts['pending_complaints'] = badge_cache.get_or_set(
                    (prefix + 'complaints', sid, 'parent', uid), _load_parent_complaints, ttl=ttl)
                counts['pending_leave_requests'] = badge_cache.get_or_set(
                    (prefix + 'leave', sid, 'parent', uid), _load_parent_leaves, ttl=ttl)

        enabled_modules = get_enabled_modules(
            None if current_user.is_super_admin else getattr(current_user, 'school_id', None))

        if 'chat' in enabled_modules:
            def _load_unread_chat():
                # Scope membership through the room's school so a stale or
                # mismatched cross-school ChatRoomMember can never inflate this
                # badge with another school's unread messages.
                member_rooms = (db.session.query(ChatRoomMember.room_id)
                                .join(ChatRoom, ChatRoom.id == ChatRoomMember.room_id)
                                .filter(ChatRoomMember.user_id == uid,
                                        ChatRoom.school_id == sid,
                                        ChatRoom.is_active == True))  # noqa: E712
                read_msgs = (db.session.query(ChatMessageRead.message_id)
                             .filter_by(user_id=uid))
                return (ChatMessage.query
                        .filter(
                            ChatMessage.room_id.in_(member_rooms),
                            ChatMessage.is_deleted == False,  # noqa: E712
                            ChatMessage.sender_user_id != uid,
                            ChatMessage.id.notin_(read_msgs),
                        )
                        .count()) or 0

            # Membership + read state are per user; the room-school join above
            # binds the count to the current school, so (user, school) fully
            # determines it. The user id remains the cache key because a given
            # user belongs to exactly one school.
            counts['unread_chat'] = badge_cache.get_or_set(
                (prefix + 'chat', uid), _load_unread_chat, ttl=ttl)
    except Exception:
        db.session.rollback()

    return counts


# ═════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═════════════════════════════════════════════════════════════════════════════

ALL = list(MODULES)
CONFIGS = {
    # tag: {module_key: is_enabled} rows to create (None = no rows at all)
    'none':     None,
    'nochat':   {k: True for k in ALL if k != 'chat'},
    'on':       {k: True for k in ALL},
    'off':      {k: (k != 'chat') for k in ALL},
    'alloff':   {k: False for k in ALL},
    'nearmiss': {'Chat': True, 'chat ': True, 'students': True},
}
# 'chat' in get_enabled_modules(school) for each configuration.
EXPECT_CHAT = {'none': True, 'nochat': False, 'on': True, 'off': False,
               'alloff': False, 'nearmiss': False}
ROLES = ('school_admin', 'teacher', 'parent')


def _is_modules_stmt(statement):
    return re.search(r'\bFROM school_modules\b', statement) is not None


def _select_list(statement):
    s = ' '.join(statement.split())
    return s[len('SELECT '):s.index(' FROM ')]


class _Capture:
    """SQL statements (+ rowcount) and pool checkouts on one engine."""

    def __init__(self, engine, fail_modules=False):
        self.engine, self.fail_modules = engine, fail_modules
        self.stmts, self.checkouts = [], 0

    def _before(self, conn, cursor, statement, params, context, many):
        self.stmts.append([statement, params, None])
        if self.fail_modules and _is_modules_stmt(statement):
            raise RuntimeError('injected school_modules failure')

    def _after(self, conn, cursor, statement, params, context, many):
        self.stmts[-1][2] = cursor.rowcount

    def _checkout(self, dbapi_conn, record, proxy):
        self.checkouts += 1

    def __enter__(self):
        event.listen(self.engine, 'before_cursor_execute', self._before)
        event.listen(self.engine, 'after_cursor_execute', self._after)
        event.listen(self.engine, 'checkout', self._checkout)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, 'before_cursor_execute', self._before)
        event.remove(self.engine, 'after_cursor_execute', self._after)
        event.remove(self.engine, 'checkout', self._checkout)
        return False


# ═════════════════════════════════════════════════════════════════════════════
#  Tests
# ═════════════════════════════════════════════════════════════════════════════

class LiveBadgeChatModuleTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')
        cls.app2 = create_app('testing')          # a second "worker"
        with cls.app.app_context():
            cls.roles = {n: Role.query.filter_by(name=n).first().id
                         for n in ROLES + ('super_admin',)}

    # ── Fixture ──────────────────────────────────────────────────────────────

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.sids, self.uids, self.users = {}, [], {}
        with self.app.app_context():
            for tag, rows in CONFIGS.items():
                school = School(school_name=f'LB {tag} {self.sfx}',
                                code=f'LB{tag[:3].upper()}{self.sfx[:5]}',
                                capacity=0, is_active=True, timezone='Asia/Baghdad')
                db.session.add(school)
                db.session.flush()
                self.sids[tag] = school.id
                self._set_rows(tag, rows)
                members = {}
                for role in ROLES + ('sender',):
                    u = User(username=f'lb_{tag}_{role}_{self.sfx}',
                             email=f'lb_{tag}_{role}_{self.sfx}@example.test',
                             full_name=f'LB {tag} {role}',
                             role_id=self.roles['school_admin' if role == 'sender' else role],
                             school_id=school.id, is_active=True)
                    u.set_password('Password123')
                    db.session.add(u)
                    db.session.flush()
                    members[role] = u.id
                    self.uids.append(u.id)
                self.users[tag] = members
                room = ChatRoom(name=f'LB room {tag}', school_id=school.id)
                db.session.add(room)
                db.session.flush()
                for uid in members.values():
                    db.session.add(ChatRoomMember(room_id=room.id, user_id=uid))
                db.session.add(ChatMessage(room_id=room.id, body='hello',
                                           sender_user_id=members['sender']))
            su = User(username=f'lb_su_{self.sfx}', email=f'lb_su_{self.sfx}@example.test',
                      full_name='LB super', role_id=self.roles['super_admin'],
                      school_id=None, is_active=True)
            su.set_password('Password123')
            db.session.add(su)
            db.session.flush()
            self.super_id = su.id
            self.uids.append(su.id)
            db.session.commit()
        badge_cache.clear()

    def tearDown(self):
        badge_cache.clear()
        with self.app.app_context():
            s = list(self.sids.values())
            for sql in (
                'DELETE FROM chat_messages WHERE room_id IN '
                '(SELECT id FROM chat_rooms WHERE school_id = ANY(:s))',
                'DELETE FROM chat_room_members WHERE room_id IN '
                '(SELECT id FROM chat_rooms WHERE school_id = ANY(:s))',
                'DELETE FROM chat_rooms WHERE school_id = ANY(:s)',
                'DELETE FROM school_modules WHERE school_id = ANY(:s)',
                'DELETE FROM audit_logs WHERE school_id = ANY(:s) OR user_id = ANY(:u)',
                'DELETE FROM users WHERE id = ANY(:u)',
                'DELETE FROM schools WHERE id = ANY(:s)',
            ):
                db.session.execute(text(sql), {'s': s, 'u': self.uids})
            db.session.commit()

    def _set_rows(self, tag, rows):
        """Replace the school's school_modules rows (inside an app context)."""
        sid = self.sids[tag]
        db.session.execute(text('DELETE FROM school_modules WHERE school_id = :s'),
                           {'s': sid})
        for key, on in (rows or {}).items():
            db.session.execute(text(
                'INSERT INTO school_modules (school_id, module_key, is_enabled, '
                'created_at, updated_at) VALUES (:s, :k, :e, now(), now())'),
                {'s': sid, 'k': key, 'e': on})

    # ── Runner / comparison ─────────────────────────────────────────────────

    def _poll(self, user_id, impl, *, app=None, clear=True, fail_modules=False):
        app = app or self.app
        if clear:
            # Both per-process caches start cold, so baseline and optimised
            # see the same state (the active-year id lives in ttl_cache).
            badge_cache.clear()
            ttl_cache.clear()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True
        with app.app_context():
            engine = db.engine
        patches = ([patch.object(sidebar_badges, 'get_badge_counts',
                                 _baseline_get_badge_counts)]
                   if impl == 'baseline' else [])
        for p in patches:
            p.start()
        try:
            with _Capture(engine, fail_modules) as cap:
                resp = client.get('/live/badges')
        finally:
            for p in patches:
                p.stop()
        return {'status': resp.status_code, 'json': resp.get_json(),
                'cache_control': resp.headers.get('Cache-Control'),
                'stmts': cap.stmts, 'checkouts': cap.checkouts}

    def _parity(self, user_id, **kw):
        base = self._poll(user_id, 'baseline', **kw)
        new = self._poll(user_id, 'optimised', **kw)
        self.assertEqual(base['status'], new['status'])
        self.assertEqual(base['json'], new['json'])
        self.assertEqual(base['cache_control'], new['cache_control'])
        self.assertEqual(base['checkouts'], new['checkouts'], 'pool checkouts')
        b_txt = [' '.join(s.split())[:160] for s, _, _ in base['stmts']]
        n_txt = [' '.join(s.split())[:160] for s, _, _ in new['stmts']]
        self.assertEqual(len(b_txt), len(n_txt),
                         {'only_baseline': [t for t in b_txt if t not in n_txt],
                          'only_optimised': [t for t in n_txt if t not in b_txt]})
        for (bs, bp, br), (ns, np_, nr) in zip(base['stmts'], new['stmts']):
            if _is_modules_stmt(bs):
                self.assertTrue(_is_modules_stmt(ns), ns)
                # Same school binding; the aggregate adds only its own
                # literals ('chat', 0).
                self.assertEqual(bp, {'school_id_1': bp['school_id_1']})
                self.assertEqual(np_, {'school_id_1': bp['school_id_1'],
                                       'module_key_1': 'chat', 'count_1': 0})
                continue
            self.assertEqual(bs, ns)
            self.assertEqual(bp, np_)
            self.assertEqual(br, nr)
        return base, new

    @staticmethod
    def _modules(result):
        return [(s, p, r) for s, p, r in result['stmts'] if _is_modules_stmt(s)]

    # ── 1-6: every configuration, every role ────────────────────────────────

    def test_every_configuration_and_role_is_identical(self):
        for tag, rows in CONFIGS.items():
            for role in ROLES:
                with self.subTest(tag=tag, role=role):
                    base, new = self._parity(self.users[tag][role])
                    self.assertEqual(base['status'], 200)
                    self.assertEqual(new['json']['counts']['unread_chat'],
                                     1 if EXPECT_CHAT[tag] else 0)
                    (bs, bp, br), = self._modules(base)
                    (ns, np_, nr), = self._modules(new)
                    self.assertEqual(br, len(rows or {}))      # baseline: all rows
                    self.assertEqual(nr, 1)                     # optimised: one row
                    self.assertEqual(
                        [c.split(' AS ')[0] for c in _select_list(bs).split(', ')],
                        ['school_modules.module_key', 'school_modules.is_enabled'])
                    self.assertTrue(_select_list(ns).startswith(
                        'coalesce(bool_or(school_modules.module_key = '), ns)
                    self.assertEqual(list(np_.values()).count(self.sids[tag]), 1)

    def test_super_admin_identical_and_no_module_query(self):
        base, new = self._parity(self.super_id)
        self.assertEqual(self._modules(base), [])
        self.assertEqual(self._modules(new), [])

    # ── 7: super-admin toggle between requests, across two app instances ────

    def test_toggle_between_requests_is_seen_immediately(self):
        uid = self.users['on']['school_admin']
        sid = self.sids['on']
        seq = {}
        for impl in ('baseline', 'optimised'):
            badge_cache.clear()
            ttl_cache.clear()
            with self.app.app_context():
                save_school_modules(sid, ALL)
                db.session.commit()
            out = [self._poll(uid, impl, clear=False)['json']]
            with self.app.app_context():                 # the real write helper
                save_school_modules(sid, [k for k in ALL if k != 'chat'])
                db.session.commit()
            out.append(self._poll(uid, impl, app=self.app2, clear=False)['json'])
            with self.app2.app_context():
                save_school_modules(sid, ALL)
                db.session.commit()
            out.append(self._poll(uid, impl, clear=False)['json'])
            seq[impl] = out
        self.assertEqual(seq['baseline'], seq['optimised'])
        self.assertEqual([j['counts']['unread_chat'] for j in seq['optimised']], [1, 0, 1])

    # ── 8: failure / rollback ───────────────────────────────────────────────

    def test_failing_module_query_is_swallowed_identically(self):
        uid = self.users['on']['school_admin']
        base, new = self._parity(uid, fail_modules=True)
        self.assertEqual(new['status'], 200)
        self.assertEqual(new['json']['counts']['unread_chat'], 0)
        # Nothing is left broken: the next poll works normally in both.
        base2, new2 = self._parity(uid)
        self.assertEqual(new2['json']['counts']['unread_chat'], 1)

    # ── 9: flags already memoized in this request ───────────────────────────

    def _in_request(self, user_id, fn, *, preload):
        badge_cache.clear()
        ttl_cache.clear()
        with self.app.test_request_context('/live/badges'):
            user = db.session.get(User, user_id, execution_options=OPTS)
            login_user(user)
            self.assertIsNone(self.app.preprocess_request())
            if preload:
                get_module_flags(user.school_id)
            with _Capture(db.engine) as cap:
                counts = fn(live=True)
            db.session.remove()
        return counts, cap.stmts

    def test_memoized_flags_are_reused_without_any_query(self):
        for tag in ('on', 'off', 'none', 'nochat'):
            with self.subTest(tag=tag):
                uid = self.users[tag]['school_admin']
                b_counts, b_stmts = self._in_request(uid, _baseline_get_badge_counts,
                                                     preload=True)
                n_counts, n_stmts = self._in_request(uid, sidebar_badges.get_badge_counts,
                                                     preload=True)
                self.assertEqual(b_counts, n_counts)
                self.assertEqual([s for s, _, _ in b_stmts], [s for s, _, _ in n_stmts])
                self.assertFalse(any(_is_modules_stmt(s) for s, _, _ in n_stmts))

    # ── Isolation ───────────────────────────────────────────────────────────

    def test_each_school_sees_only_its_own_configuration(self):
        on = self._poll(self.users['on']['school_admin'], 'optimised')
        off = self._poll(self.users['off']['school_admin'], 'optimised')
        self.assertEqual(on['json']['counts']['unread_chat'], 1)
        self.assertEqual(off['json']['counts']['unread_chat'], 0)
        for result, tag in ((on, 'on'), (off, 'off')):
            (_, params, _), = self._modules(result)
            self.assertIn(self.sids[tag], params.values())
            others = set(self.sids.values()) - {self.sids[tag]}
            self.assertFalse(others & set(params.values()))

    # ── live=False (page render) is untouched ───────────────────────────────

    def test_page_render_path_is_unchanged(self):
        for tag in ('on', 'nochat', 'none'):
            with self.subTest(tag=tag):
                uid = self.users[tag]['school_admin']
                runs = {}
                for name, fn in (('baseline', _baseline_get_badge_counts),
                                 ('optimised', sidebar_badges.get_badge_counts)):
                    badge_cache.clear()
                    ttl_cache.clear()
                    with self.app.test_request_context('/'):
                        login_user(db.session.get(User, uid, execution_options=OPTS))
                        self.app.preprocess_request()
                        with _Capture(db.engine) as cap:
                            counts = fn(live=False)
                        db.session.remove()
                    runs[name] = (counts, [(s, p) for s, p, _ in cap.stmts])
                self.assertEqual(runs['baseline'], runs['optimised'])

    # ── Network measurement ─────────────────────────────────────────────────

    def test_protocol_bytes_per_poll(self):
        """Client↔server bytes for one /live/badges poll (school admin, 18
        module rows) through the loopback relay, baseline vs optimised,
        interleaved; pool_pre_ping=True like production. Cold = badge cache
        empty (every count recomputed); warm = counts served from badge_cache."""
        import config.settings as settings
        from sqlalchemy.engine import make_url

        runs = int(os.environ.get('LB_BYTE_RUNS', '15'))
        url = make_url(os.environ['TEST_DATABASE_URL'])
        relay = _Relay(url.port)
        orig_uri = settings.TestingConfig.SQLALCHEMY_DATABASE_URI
        had_opts = 'SQLALCHEMY_ENGINE_OPTIONS' in vars(settings.TestingConfig)
        settings.TestingConfig.SQLALCHEMY_DATABASE_URI = url.set(
            host='127.0.0.1', port=relay.port).render_as_string(hide_password=False)
        settings.TestingConfig.SQLALCHEMY_ENGINE_OPTIONS = {'pool_pre_ping': True}
        try:
            app_r = create_app('testing')
        finally:
            settings.TestingConfig.SQLALCHEMY_DATABASE_URI = orig_uri
            if not had_opts:
                del settings.TestingConfig.SQLALCHEMY_ENGINE_OPTIONS
        uid = self.users['on']['school_admin']
        samples = {(m, i): [] for m in ('cold', 'warm') for i in ('baseline', 'optimised')}
        try:
            self._poll(uid, 'optimised', app=app_r)          # warm the pool
            for mode in ('cold', 'warm'):
                for _ in range(runs):
                    for impl in ('baseline', 'optimised'):
                        if mode == 'warm':
                            self._poll(uid, impl, app=app_r)    # fill badge_cache
                        relay.reset()
                        r = self._poll(uid, impl, app=app_r, clear=(mode == 'cold'))
                        self.assertEqual(r['json']['counts']['unread_chat'], 1)
                        samples[(mode, impl)].append((relay.down, relay.up))
        finally:
            with app_r.app_context():
                db.engine.dispose()
            relay.close()
        for mode in ('cold', 'warm'):
            line = [f'\n[badge-bytes] mode={mode} runs={runs} pool_pre_ping=True']
            for impl in ('baseline', 'optimised'):
                down = [d for d, _ in samples[(mode, impl)]]
                up = [u for _, u in samples[(mode, impl)]]
                line.append(f'  {impl}: down min/median/max='
                            f'{min(down)}/{statistics.median(down)}/{max(down)}B '
                            f'up min/median/max={min(up)}/{statistics.median(up)}/{max(up)}B')
            print(''.join(line))
            self.assertLess(statistics.median(d for d, _ in samples[(mode, 'optimised')]),
                            statistics.median(d for d, _ in samples[(mode, 'baseline')]))


if __name__ == '__main__':                                    # pragma: no cover
    unittest.main()

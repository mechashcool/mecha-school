# -*- coding: utf-8 -*-
"""AI Face heartbeat — egress narrowing must be behaviour-identical.

`_db_touch_device` now loads only AttendanceDevice.id/school_id/last_sync_at
(load_only) and captures id/school_id before the commit, so the log line after
the commit no longer reloads the expired row with a second full SELECT.

What is pinned here, against a VERBATIM frozen copy of the pre-change function
(`_baseline_db_touch_device` below, from main @ 475b796):

  * identical attendance_devices rows after the heartbeat (every column), with
    controlled last_sync_at / updated_at timestamps;
  * identical UPDATE statement text and parameters, one commit, same boundary;
  * baseline = full 16-column SELECT + UPDATE + post-commit 16-column reload
    (2 pool checkouts); optimised = 3-column SELECT + UPDATE (1 checkout);
  * identical log records for the normal, unknown-device and failure paths;
  * a failed UPDATE rolls back, writes nothing, and the next heartbeat works;
  * school isolation (another school's device row is untouched) and
    concurrent heartbeats for one device;
  * client↔server protocol bytes per heartbeat through the loopback relay
    (printed; server→client asserted to shrink).

Runs against the isolated local PostgreSQL test database only.
"""
import logging
import os
import re
import statistics
import sys
import threading
import unittest
from datetime import datetime
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import event, text

from app import create_app
from app.models import db, AttendanceDevice, School
from app.services import ai_face_ws
from tests.test_employee_auto_absence_egress import _Relay

LOGGER = 'aiface_ws'
log = logging.getLogger(LOGGER)
_flask_app = None                     # set per call — used by the frozen baseline
_THIS = sys.modules[__name__]

FIXED_SYNC = datetime(2031, 3, 4, 7, 15, 30, 123456)
FIXED_UPD = datetime(2031, 3, 4, 7, 15, 30, 654321)
DEVICE_COLUMNS = 16


# ═════════════════════════════════════════════════════════════════════════════
#  Frozen baseline — verbatim copy of the function BEFORE the change.
#  Do not edit: it is the reference the optimised version is compared with.
# ═════════════════════════════════════════════════════════════════════════════

def _baseline_db_touch_device(sn: str) -> None:
    """
    Update last_sync_at for a device in the DB.
    Called from background threads as a keep-alive heartbeat so the web UI
    (which may run in a different process or on Render) can determine whether
    the local WS bridge is actively connected — without relying on the
    in-memory _connections dict that is invisible across process boundaries.
    """
    if not _flask_app:
        return
    try:
        with _flask_app.app_context():
            from app.models import db, AttendanceDevice
            device = (AttendanceDevice.query
                      .execution_options(bypass_tenant_scope=True)
                      .filter_by(device_sn=sn)
                      .first())
            if device:
                _old_sync = device.last_sync_at
                _new_sync = datetime.utcnow()
                device.last_sync_at = _new_sync
                db.session.commit()
                log.info(
                    '[aiface] heartbeat committed: sn=%s device_id=%d school_id=%d '
                    'last_sync_at: %s → %s (UTC) — commit OK',
                    sn, device.id, device.school_id, _old_sync, _new_sync,
                )
            else:
                log.warning(
                    '[aiface] heartbeat skipped — sn=%s not found in DB '
                    '(device not registered via Admin → Attendance Devices)', sn)
    except Exception:
        log.exception("[aiface] heartbeat DB update failed sn=%s", sn)


# ═════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═════════════════════════════════════════════════════════════════════════════

class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.items = []
        self._lock_ = threading.Lock()

    def emit(self, record):
        with self._lock_:
            self.items.append((record.levelname, record.getMessage(),
                               record.exc_info is not None))


class _FixedDateTime(datetime):
    @classmethod
    def utcnow(cls):
        return FIXED_SYNC


def _shape(statement):
    s = ' '.join(statement.split())
    verb = s.split(' ', 1)[0].upper()
    m = re.search(r'\bFROM (\w+)', s) or re.search(r'\bUPDATE (\w+)', s)
    return verb, (m.group(1) if m else '')


def _select_cols(statement):
    s = ' '.join(statement.split())
    return [c.strip() for c in s[len('SELECT '):s.index(' FROM ')].split(',')]


IMPLEMENTATIONS = (('baseline', _baseline_db_touch_device),
                   ('optimised', ai_face_ws._db_touch_device))


# ═════════════════════════════════════════════════════════════════════════════
#  Tests
# ═════════════════════════════════════════════════════════════════════════════

class AiFaceHeartbeatEgressTest(unittest.TestCase):
    """School A owns devices A1 and A2, school B owns device B1."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app('testing')

    # ── Fixture ──────────────────────────────────────────────────────────────

    def setUp(self):
        self.sfx = uuid4().hex[:8]
        self.ids = {'schools': [], 'devices': {}}
        self.sns = {}
        with self.app.app_context():
            schools = {}
            for tag in ('a', 'b'):
                school = School(school_name=f'HB {tag} {self.sfx}',
                                code=f'HB{tag.upper()}{self.sfx[:6]}',
                                capacity=0, is_active=True, timezone='Asia/Baghdad')
                db.session.add(school)
                db.session.flush()
                schools[tag] = school.id
                self.ids['schools'].append(school.id)
            for key, tag in (('a1', 'a'), ('a2', 'a'), ('b1', 'b')):
                sn = f'HB{key.upper()}{self.sfx}'
                dev = AttendanceDevice(
                    school_id=schools[tag], name=f'جهاز {key} {self.sfx}',
                    device_type='aiface', device_scope='students',
                    ip_address='10.0.0.7', port=7788, username='admin',
                    password='not-a-real-secret', device_sn=sn, is_active=True,
                    last_sync_at=datetime(2031, 3, 4, 7, 0, 0),
                    notes='heartbeat egress fixture')
                db.session.add(dev)
                db.session.flush()
                self.ids['devices'][key] = dev.id
                self.sns[key] = sn
            self.ids['school_a'], self.ids['school_b'] = schools['a'], schools['b']
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.execute(text('DELETE FROM attendance_devices WHERE id = ANY(:d)'),
                               {'d': list(self.ids['devices'].values())})
            db.session.execute(text('DELETE FROM schools WHERE id = ANY(:s)'),
                               {'s': self.ids['schools']})
            db.session.commit()

    # ── Runner / comparison ─────────────────────────────────────────────────

    def _snapshot(self):
        """Every column of every fixture device, keyed by fixture key."""
        with self.app.app_context():
            rows = db.session.execute(text(
                'SELECT * FROM attendance_devices WHERE id = ANY(:d) ORDER BY id'),
                {'d': list(self.ids['devices'].values())}).mappings().all()
            db.session.remove()
        by_id = {r['id']: dict(r) for r in rows}
        return {k: by_id[i] for k, i in self.ids['devices'].items()}

    def _restore(self, snap):
        with self.app.app_context():
            for row in snap.values():
                db.session.execute(text(
                    'UPDATE attendance_devices SET last_sync_at = :l, updated_at = :u '
                    'WHERE id = :i'),
                    {'l': row['last_sync_at'], 'u': row['updated_at'], 'i': row['id']})
            db.session.commit()
        self.assertEqual(self._snapshot(), snap, 'fixture restore failed')

    def _run(self, fn, sn, *, fixed=True, fail_update=False, app=None):
        """Run one heartbeat; return (log records, [(stmt, params)], checkouts,
        commits)."""
        app = app or self.app
        logger = logging.getLogger(LOGGER)
        handler = _Records()
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        stmts, checkouts, commits = [], [], []

        with app.app_context():
            engine = db.engine

        def before(conn, cursor, statement, params, context, many):
            stmts.append((statement, params))
            if fail_update and statement.lstrip().upper().startswith(
                    'UPDATE ATTENDANCE_DEVICES'):
                raise RuntimeError('injected UPDATE failure')

        def on_checkout(dbapi_conn, record, proxy):
            checkouts.append(1)

        def on_commit(conn):
            commits.append(1)

        event.listen(engine, 'before_cursor_execute', before)
        event.listen(engine, 'checkout', on_checkout)
        event.listen(engine, 'commit', on_commit)
        # SQLAlchemy 2.0 executes the onupdate callable from the column's
        # memoized (arg, is_scalar, is_callable, is_sentinel) tuple.
        upd_col = AttendanceDevice.__table__.c.updated_at
        upd_desc = upd_col._onupdate_description_tuple
        patches = [patch.object(ai_face_ws, '_flask_app', app),
                   patch.object(_THIS, '_flask_app', app)]
        if fixed:
            patches += [patch.object(ai_face_ws, 'datetime', _FixedDateTime),
                        patch.object(_THIS, 'datetime', _FixedDateTime),
                        patch.object(upd_col, '_onupdate_description_tuple',
                                     (lambda ctx: FIXED_UPD,) + tuple(upd_desc[1:]))]
        try:
            for p in patches:
                p.start()
            fn(sn)
        finally:
            for p in reversed(patches):
                p.stop()
            event.remove(engine, 'before_cursor_execute', before)
            event.remove(engine, 'checkout', on_checkout)
            event.remove(engine, 'commit', on_commit)
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        return handler.items, stmts, len(checkouts), len(commits)

    def _parity(self, key='a1', sn=None, **kw):
        """Run baseline then optimised from the same state; assert identity."""
        sn = sn or self.sns[key]
        start = self._snapshot()
        base = self._run(_baseline_db_touch_device, sn, **kw)
        base_rows = self._snapshot()
        self._restore(start)
        new = self._run(ai_face_ws._db_touch_device, sn, **kw)
        new_rows = self._snapshot()

        self.assertEqual(base_rows, new_rows, 'attendance_devices rows differ')
        self.assertEqual(base[0], new[0], 'log records differ')
        self.assertEqual(base[3], new[3], 'commit count differs')
        base_writes = [s for s in base[1] if _shape(s[0])[0] != 'SELECT']
        new_writes = [s for s in new[1] if _shape(s[0])[0] != 'SELECT']
        self.assertEqual(base_writes, new_writes, 'write SQL / parameters differ')
        return start, base, new, new_rows

    # ── 1-7: normal heartbeat ───────────────────────────────────────────────

    def test_normal_heartbeat_rows_logs_sql_and_checkouts(self):
        start, base, new, rows = self._parity('a1')

        # 1/2 — exactly last_sync_at and updated_at changed, to controlled values.
        for col, before in start['a1'].items():
            if col == 'last_sync_at':
                self.assertEqual(rows['a1'][col], FIXED_SYNC)
            elif col == 'updated_at':
                self.assertEqual(rows['a1'][col], FIXED_UPD)
            else:
                self.assertEqual(rows['a1'][col], before, col)
        self.assertEqual(rows['a2'], start['a2'])
        self.assertEqual(rows['b1'], start['b1'])

        # 3 — the single UPDATE: identical text and parameters (asserted in
        # _parity); pin its exact shape here.
        (upd_sql, upd_params), = [s for s in new[1] if _shape(s[0])[0] == 'UPDATE']
        self.assertEqual(' '.join(upd_sql.split()),
                         'UPDATE attendance_devices SET last_sync_at=%(last_sync_at)s, '
                         'updated_at=%(updated_at)s WHERE attendance_devices.id = '
                         '%(attendance_devices_id)s')
        self.assertEqual(upd_params, {'last_sync_at': FIXED_SYNC, 'updated_at': FIXED_UPD,
                                      'attendance_devices_id': self.ids['devices']['a1']})

        # 4/5 — statement sequence.
        self.assertEqual([_shape(s) for s, _ in base[1]],
                         [('SELECT', 'attendance_devices'),
                          ('UPDATE', 'attendance_devices'),
                          ('SELECT', 'attendance_devices')])
        self.assertEqual([_shape(s) for s, _ in new[1]],
                         [('SELECT', 'attendance_devices'),
                          ('UPDATE', 'attendance_devices')])
        b_sel, b_reload = base[1][0][0], base[1][2][0]
        self.assertEqual(len(_select_cols(b_sel)), DEVICE_COLUMNS)
        self.assertEqual(len(_select_cols(b_reload)), DEVICE_COLUMNS)
        self.assertIn('attendance_devices.id = ', b_reload)        # reload by PK
        n_sel = new[1][0][0]
        self.assertEqual(_select_cols(n_sel),
                         ['attendance_devices.id AS attendance_devices_id',
                          'attendance_devices.school_id AS attendance_devices_school_id',
                          'attendance_devices.last_sync_at AS attendance_devices_last_sync_at'])
        # Same lookup: identical WHERE / LIMIT and parameters.
        tail = lambda s: ' '.join(s.split())[' '.join(s.split()).index(' FROM '):]  # noqa: E731
        self.assertEqual(tail(b_sel), tail(n_sel))
        self.assertEqual(base[1][0][1], new[1][0][1])

        # 6 — no lazy load / N+1 / extra checkout; one commit in both.
        self.assertEqual(base[2], 2, 'baseline checkouts')
        self.assertEqual(new[2], 1, 'optimised checkouts')
        self.assertEqual((base[3], new[3]), (1, 1))

        # 7 — the normal log line, value for value.
        self.assertEqual(new[0], [(
            'INFO',
            f"[aiface] heartbeat committed: sn={self.sns['a1']} "
            f"device_id={self.ids['devices']['a1']} school_id={self.ids['school_a']} "
            f"last_sync_at: 2031-03-04 07:00:00 → {FIXED_SYNC} (UTC) — commit OK",
            False)])

    def test_uncontrolled_clock_advances_both_timestamps_in_both(self):
        """Real utcnow / real onupdate: both columns move forward identically."""
        start = self._snapshot()
        for name, fn in IMPLEMENTATIONS:
            with self.subTest(name):
                self._restore(start)
                t0 = datetime.utcnow()
                logs, stmts, _, commits = self._run(fn, self.sns['a1'], fixed=False)
                t1 = datetime.utcnow()
                row = self._snapshot()['a1']
                self.assertTrue(t0 <= row['last_sync_at'] <= t1)
                self.assertTrue(t0 <= row['updated_at'] <= t1)
                self.assertEqual(commits, 1)
                self.assertEqual(logs[0][0], 'INFO')
                self.assertIn(f"→ {row['last_sync_at']} (UTC)", logs[0][1])

    # ── 8: unknown device ───────────────────────────────────────────────────

    def test_unknown_device_same_warning_and_no_write(self):
        sn = f'NOPE{self.sfx}'
        start, base, new, rows = self._parity(sn=sn)
        self.assertEqual(rows, start)
        expected = [('WARNING',
                     f'[aiface] heartbeat skipped — sn={sn} not found in DB '
                     '(device not registered via Admin → Attendance Devices)', False)]
        self.assertEqual(base[0], expected)
        self.assertEqual(new[0], expected)
        self.assertEqual([_shape(s) for s, _ in base[1]], [('SELECT', 'attendance_devices')])
        self.assertEqual([_shape(s) for s, _ in new[1]], [('SELECT', 'attendance_devices')])
        self.assertEqual((base[3], new[3]), (0, 0))

    # ── 9: commit failure ───────────────────────────────────────────────────

    def test_failed_update_rolls_back_logs_the_same_and_recovers(self):
        start, base, new, rows = self._parity('a1', fail_update=True)
        self.assertEqual(rows, start, 'a failed heartbeat must write nothing')
        expected = [('ERROR', f"[aiface] heartbeat DB update failed sn={self.sns['a1']}",
                     True)]
        self.assertEqual(base[0], expected)
        self.assertEqual(new[0], expected)
        self.assertEqual((base[3], new[3]), (0, 0))
        # The session is not left poisoned: the next heartbeat succeeds.
        logs, _, _, commits = self._run(ai_face_ws._db_touch_device, self.sns['a1'])
        self.assertEqual(commits, 1)
        self.assertEqual(logs[0][0], 'INFO')
        self.assertEqual(self._snapshot()['a1']['last_sync_at'], FIXED_SYNC)

    # ── 10: school isolation ────────────────────────────────────────────────

    def test_heartbeat_never_writes_another_schools_device(self):
        for key, other_school in (('a1', 'b1'), ('b1', 'a1')):
            with self.subTest(key):
                start, base, new, rows = self._parity(key)
                self.assertEqual(rows[other_school], start[other_school])
                self.assertEqual(rows['a2'], start['a2'])
                self.assertEqual(rows[key]['school_id'], start[key]['school_id'])
                tag = 'school_a' if key == 'a1' else 'school_b'
                self.assertIn(f"school_id={self.ids[tag]} ", new[0][0][1])
                self._restore(start)

    # ── 11: concurrent heartbeats ───────────────────────────────────────────

    def test_concurrent_heartbeats_for_one_device(self):
        start = self._snapshot()
        workers = 8
        for name, fn in IMPLEMENTATIONS:
            with self.subTest(name):
                self._restore(start)
                logger = logging.getLogger(LOGGER)
                handler = _Records()
                old_level = logger.level
                logger.addHandler(handler)
                logger.setLevel(logging.DEBUG)
                barrier = threading.Barrier(workers)
                escaped = []

                def worker():
                    try:
                        barrier.wait()
                        fn(self.sns['a1'])
                    except Exception as exc:          # pragma: no cover
                        escaped.append(exc)

                with patch.object(ai_face_ws, '_flask_app', self.app), \
                        patch.object(_THIS, '_flask_app', self.app):
                    threads = [threading.Thread(target=worker) for _ in range(workers)]
                    for t in threads:
                        t.start()
                    for t in threads:
                        t.join(60)
                logger.removeHandler(handler)
                logger.setLevel(old_level)

                self.assertEqual(escaped, [])
                self.assertEqual([lvl for lvl, _, _ in handler.items], ['INFO'] * workers)
                written = {m.split(' → ')[1].split(' (UTC)')[0] for _, m, _ in handler.items}
                row = self._snapshot()
                self.assertIn(str(row['a1']['last_sync_at']), written)
                self.assertEqual(row['a2'], start['a2'])
                self.assertEqual(row['b1'], start['b1'])
                for col in start['a1']:
                    if col not in ('last_sync_at', 'updated_at'):
                        self.assertEqual(row['a1'][col], start['a1'][col], col)

    # ── Network measurement ─────────────────────────────────────────────────

    def test_protocol_bytes_per_heartbeat(self):
        """Client↔server bytes for one heartbeat through the loopback relay,
        baseline vs optimised, interleaved, with and without pool_pre_ping
        (production sets pool_pre_ping=True; TestingConfig does not)."""
        import config.settings as settings
        from sqlalchemy.engine import make_url

        runs = int(os.environ.get('HB_BYTE_RUNS', '15'))
        url = make_url(os.environ['TEST_DATABASE_URL'])
        relay = _Relay(url.port)
        report = {}
        try:
            for ping in (True, False):
                orig_uri = settings.TestingConfig.SQLALCHEMY_DATABASE_URI
                orig_opts = getattr(settings.TestingConfig, 'SQLALCHEMY_ENGINE_OPTIONS', None)
                settings.TestingConfig.SQLALCHEMY_DATABASE_URI = url.set(
                    host='127.0.0.1', port=relay.port).render_as_string(hide_password=False)
                settings.TestingConfig.SQLALCHEMY_ENGINE_OPTIONS = {'pool_pre_ping': ping}
                try:
                    app2 = create_app('testing')
                finally:
                    settings.TestingConfig.SQLALCHEMY_DATABASE_URI = orig_uri
                    if orig_opts is None:
                        del settings.TestingConfig.SQLALCHEMY_ENGINE_OPTIONS
                    else:
                        settings.TestingConfig.SQLALCHEMY_ENGINE_OPTIONS = orig_opts
                samples = {'baseline': [], 'optimised': []}
                try:
                    with app2.app_context():
                        db.session.execute(text('SELECT 1'))       # warm the pool
                        db.session.remove()
                    for _ in range(runs):
                        for name, fn in IMPLEMENTATIONS:          # interleaved
                            relay.reset()
                            logs, _, _, commits = self._run(fn, self.sns['a1'],
                                                            fixed=False, app=app2)
                            samples[name].append((relay.down, relay.up))
                            self.assertEqual(commits, 1)
                            self.assertEqual(logs[0][0], 'INFO')
                finally:
                    with app2.app_context():
                        db.engine.dispose()
                report[ping] = samples
        finally:
            relay.close()

        for ping, samples in report.items():
            line = [f'\n[heartbeat-bytes] pool_pre_ping={ping} runs={runs}']
            for name in ('baseline', 'optimised'):
                down = [d for d, _ in samples[name]]
                up = [u for _, u in samples[name]]
                line.append(f'  {name}: down min/median/max='
                            f'{min(down)}/{statistics.median(down)}/{max(down)}B '
                            f'up min/median/max={min(up)}/{statistics.median(up)}/{max(up)}B')
            print(''.join(line))
            self.assertLess(max(d for d, _ in samples['optimised']),
                            min(d for d, _ in samples['baseline']))


if __name__ == '__main__':                                    # pragma: no cover
    unittest.main()

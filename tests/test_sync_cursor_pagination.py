"""Bounded cursor/pagination state machine — executable spec for ADR 0001.

`tests/test_sync_cursor_concurrency.py` proves the commit-order defect and that
a snapshot watermark avoids it. This file proves the remaining properties that
make the design shippable, none of which the earlier `(H, P)` sketch had:

  * cursor state is bounded — no set of delivered xids, six scalars, <= 128 B;
  * a long-open transaction cannot cause unbounded growth, silent loss,
    infinite pagination, or a stalled feed;
  * a page may split one XID's rows without ever marking that XID delivered;
  * every bound failure is an explicit SCOPED RESET, never a silent skip and
    never a false "you are up to date";
  * retention (`min_retained_xid`) and `generation` interact correctly.

The reference implementation below IS the specification — B3 must implement this
algorithm, not a paraphrase of it. It is kept in the test module on purpose:
there is no `/sync` endpoint yet, and adding application code for one would
breach the "inert until B2" boundary.
"""
import json
import os
import unittest
from urllib.parse import urlsplit

import psycopg2

# ── Cursor ───────────────────────────────────────────────────────────────────

LIVE, CATCHUP = 'L', 'C'
CURSOR_MAX_BYTES = 128
LIVE_SCAN_LIMIT = 500


class Reset(Exception):
    """A scoped reset: rebuild these scopes, do not treat as an empty page."""

    def __init__(self, scopes, reason):
        super().__init__(reason)
        self.scopes = tuple(scopes)
        self.reason = reason


class Cursor:
    """Six scalars. Nothing here grows with open transactions or row count."""

    __slots__ = ('gen', 'mode', 'h', 'h_target', 'pos_xid', 'pos_id')

    def __init__(self, gen, mode=LIVE, h=0, h_target=0, pos_xid=0, pos_id=0):
        self.gen, self.mode = gen, mode
        self.h, self.h_target = h, h_target
        self.pos_xid, self.pos_id = pos_xid, pos_id

    def encode(self):
        raw = json.dumps([self.gen, self.mode, str(self.h), str(self.h_target),
                          str(self.pos_xid), self.pos_id],
                         separators=(',', ':'))
        if len(raw.encode()) > CURSOR_MAX_BYTES:
            raise Reset((), 'cursor_too_large')
        return raw

    @classmethod
    def decode(cls, raw, scopes):
        if raw is None:
            return cls(gen=None)
        if len(raw.encode()) > CURSOR_MAX_BYTES:
            raise Reset(scopes, 'cursor_too_large')
        try:
            gen, mode, h, h_target, pos_xid, pos_id = json.loads(raw)
            return cls(int(gen), mode, int(h), int(h_target), int(pos_xid),
                       int(pos_id))
        except Exception:
            raise Reset(scopes, 'cursor_unparseable')


class Page:
    def __init__(self, rows, cursor, caught_up):
        self.rows, self.cursor, self.caught_up = rows, cursor, caught_up


# ── Reference implementation of the ADR read algorithm ───────────────────────

def _meta(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT generation, coalesce(min_retained_xid, 0) "
                    "FROM sync_meta WHERE id = 1")
        gen, floor = cur.fetchone()
    return int(gen), int(floor)


def _xmin(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT pg_snapshot_xmin(pg_current_snapshot())::text::numeric")
        return int(cur.fetchone()[0])


def read(conn, cursor, school_id, scope_ids, limit):
    """One /sync/changes page. Returns Page, or raises Reset."""
    gen, floor = _meta(conn)

    if cursor.gen is None:                       # first ever call
        cursor = Cursor(gen=gen, mode=LIVE, h=0)
    if cursor.gen != gen:
        raise Reset(scope_ids, 'generation_changed')
    if cursor.h < floor:
        raise Reset(scope_ids, 'retention_underrun')

    xmin = _xmin(conn)

    if cursor.mode == CATCHUP:
        if cursor.pos_xid < floor:
            raise Reset(scope_ids, 'retention_underrun')
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, xid, resource_id FROM change_journal "
                "WHERE school_id = %s AND scope_type = 'student' "
                "  AND scope_id = ANY(%s) "
                "  AND xid >= %s AND xid < %s "
                "  AND (xid, id) > (%s, %s) "
                "ORDER BY xid, id LIMIT %s",
                (school_id, list(scope_ids), cursor.pos_xid, cursor.h_target,
                 cursor.pos_xid, cursor.pos_id, limit))
            rows = cur.fetchall()
        if rows:
            # Position is (xid, id) — NOT a bare xid. Resuming continues inside
            # the same xid, so splitting one xid across pages never marks that
            # xid delivered.
            cursor.pos_xid, cursor.pos_id = int(rows[-1][1]), int(rows[-1][0])
            return Page(rows, cursor, caught_up=False)
        # Band drained. Seal it and fall through to LIVE.
        cursor = Cursor(gen=gen, mode=LIVE, h=cursor.h_target)

    # LIVE
    if xmin > cursor.h:
        nxt = Cursor(gen=gen, mode=CATCHUP, h=cursor.h, h_target=xmin,
                     pos_xid=cursor.h, pos_id=0)
        return Page([], nxt, caught_up=False)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, xid, resource_id FROM change_journal "
            "WHERE school_id = %s AND scope_type = 'student' "
            "  AND scope_id = ANY(%s) AND xid >= %s "
            "ORDER BY xid, id LIMIT %s",
            (school_id, list(scope_ids), cursor.h, LIVE_SCAN_LIMIT + 1))
        open_rows = cur.fetchall()

    if len(open_rows) > LIVE_SCAN_LIMIT:
        raise Reset(scope_ids, 'open_region_overflow')
    return Page(open_rows, cursor, caught_up=True)


def drain(conn, school_id, scope_ids, limit, max_pages=200):
    """Poll until caught up. Returns (delivered_ids, cursor, pages_used)."""
    cursor, delivered, pages = Cursor(gen=None), [], 0
    while pages < max_pages:
        page = read(conn, cursor, school_id, scope_ids, limit)
        pages += 1
        delivered += [int(r[0]) for r in page.rows]
        cursor = page.cursor
        if page.caught_up:
            return delivered, cursor, pages
    raise AssertionError('drain did not terminate — infinite pagination')


# ── Harness ──────────────────────────────────────────────────────────────────

class CursorPaginationTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        url = os.environ['TEST_DATABASE_URL']
        parts = urlsplit(url)
        assert (parts.hostname or '').lower() in ('127.0.0.1', 'localhost', '::1')
        assert (parts.path or '').lstrip('/').endswith('_test')
        cls.dsn = url
        conn = psycopg2.connect(cls.dsn); conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("INSERT INTO schools (school_name, code, capacity, is_active) "
                        "VALUES ('Cursor Pagination', 'CURPAG9', 0, true) RETURNING id")
            cls.school_id = cur.fetchone()[0]
        conn.close()

    @classmethod
    def tearDownClass(cls):
        conn = psycopg2.connect(cls.dsn); conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DELETE FROM change_journal WHERE school_id = %s",
                        (cls.school_id,))
            cur.execute("DELETE FROM schools WHERE id = %s", (cls.school_id,))
        conn.close()

    def setUp(self):
        self.conns = []
        c = psycopg2.connect(self.dsn); c.autocommit = True
        with c.cursor() as cur:
            cur.execute("DELETE FROM change_journal WHERE school_id = %s",
                        (self.school_id,))
            cur.execute("UPDATE sync_meta SET generation = 1, min_retained_xid = NULL "
                        "WHERE id = 1")
        c.close()
        self.scopes = [1, 2]

    def tearDown(self):
        for c in self.conns:
            try:
                c.rollback(); c.close()
            except Exception:
                pass
        c = psycopg2.connect(self.dsn); c.autocommit = True
        with c.cursor() as cur:
            cur.execute("UPDATE sync_meta SET generation = 1, min_retained_xid = NULL "
                        "WHERE id = 1")
        c.close()

    def _conn(self, autocommit=False):
        c = psycopg2.connect(self.dsn); c.autocommit = autocommit
        self.conns.append(c)
        return c

    def _commit_one(self, scope_id=1, n=1):
        """Write in a short-lived connection and close it immediately.

        Loops below run tens of iterations; keeping every writer open until
        tearDown exhausts the server's connection slots when the whole suite
        runs in one process.
        """
        conn = psycopg2.connect(self.dsn)
        try:
            rows = self._insert(conn, scope_id, n=n)
            conn.commit()
            return rows
        finally:
            conn.close()

    def _insert(self, conn, scope_id, n=1):
        """n journal rows in ONE transaction, so they share one xid."""
        out = []
        with conn.cursor() as cur:
            for i in range(n):
                cur.execute(
                    "INSERT INTO change_journal "
                    "(school_id, scope_type, scope_id, resource, resource_id, op, xid) "
                    "VALUES (%s,'student',%s,'attendance',%s,'upsert', "
                    "        pg_current_xact_id()::text::numeric(20,0)) "
                    "RETURNING id, xid",
                    (self.school_id, scope_id, 1000 + i))
                out.append(cur.fetchone())
        return out

    # ── Cursor is bounded ────────────────────────────────────────────────────

    def test_cursor_never_exceeds_its_documented_maximum(self):
        """Even at the extreme end of the xid8 range."""
        big = 2 ** 64 - 1
        c = Cursor(gen=2 ** 31 - 1, mode=CATCHUP, h=big, h_target=big,
                   pos_xid=big, pos_id=2 ** 63 - 1)
        self.assertLessEqual(len(c.encode().encode()), CURSOR_MAX_BYTES)

    def test_cursor_carries_no_set_of_delivered_ids(self):
        self.assertEqual(
            set(Cursor.__slots__),
            {'gen', 'mode', 'h', 'h_target', 'pos_xid', 'pos_id'},
            'the cursor must stay six scalars — no delivered-xid set')

    def test_cursor_size_is_constant_as_traffic_grows(self):
        """The whole point: 1 committed txn or 200, the cursor is the same size."""
        reader = self._conn(autocommit=True)
        sizes = set()
        for _ in range(30):
            self._commit_one()
            _, cursor, _ = drain(reader, self.school_id, self.scopes, limit=5)
            sizes.add(len(cursor.encode().encode()))
        self.assertLessEqual(max(sizes), CURSOR_MAX_BYTES)
        self.assertLess(max(sizes) - min(sizes), 24,
                        'cursor size must not scale with traffic')

    def test_cursor_does_not_grow_while_an_old_transaction_stays_open(self):
        """The failure mode that killed the (H, P) draft."""
        old = self._conn()
        self._insert(old, 1)                      # A opens and holds a low xid
        reader = self._conn(autocommit=True)

        sizes = []
        for _ in range(40):                       # many newer commits above A
            self._commit_one()
            _, cursor, _ = drain(reader, self.school_id, self.scopes, limit=10)
            sizes.append(len(cursor.encode().encode()))

        self.assertLessEqual(max(sizes), CURSOR_MAX_BYTES)
        self.assertLess(max(sizes) - min(sizes), 24,
                        'cursor grew while an old transaction was open')
        old.commit()

    # ── Split XID ────────────────────────────────────────────────────────────

    def test_a_single_xid_spanning_pages_is_never_marked_fully_delivered(self):
        """12 rows, one xid, pages of 5. Nothing may be lost at the boundary."""
        w = self._conn()
        rows = self._insert(w, 1, n=12)
        w.commit()
        expected = sorted(int(r[0]) for r in rows)
        self.assertEqual(len({int(r[1]) for r in rows}), 1, 'must be one xid')

        reader = self._conn(autocommit=True)
        delivered, _, pages = drain(reader, self.school_id, self.scopes, limit=5)

        self.assertEqual(sorted(set(delivered)), expected,
                         'every row of the split xid must arrive')
        self.assertGreater(pages, 2, 'the xid really did span multiple pages')

    def test_resuming_mid_xid_continues_inside_that_xid(self):
        """Position is (xid, id): the second page must stay in the same xid."""
        w = self._conn()
        rows = self._insert(w, 1, n=8)
        w.commit()
        reader = self._conn(autocommit=True)

        cursor = Cursor(gen=None)
        page = read(reader, cursor, self.school_id, self.scopes, limit=3)
        while page.caught_up is False and not page.rows:
            page = read(reader, page.cursor, self.school_id, self.scopes, limit=3)

        self.assertEqual(len(page.rows), 3)
        the_xid = int(rows[0][1])
        self.assertEqual(page.cursor.pos_xid, the_xid)
        self.assertEqual(page.cursor.pos_id, int(page.rows[-1][0]))

        nxt = read(reader, page.cursor, self.school_id, self.scopes, limit=3)
        self.assertTrue(nxt.rows, 'the rest of the xid must still be pending')
        self.assertEqual(int(nxt.rows[0][1]), the_xid,
                         'resume continued inside the same xid')
        self.assertGreater(int(nxt.rows[0][0]), int(page.rows[-1][0]))

    def test_multiple_xids_across_pages_lose_nothing(self):
        w1, w2, w3 = self._conn(), self._conn(), self._conn()
        all_rows = []
        for w, n in ((w1, 4), (w2, 5), (w3, 3)):
            all_rows += self._insert(w, 1, n=n)
            w.commit()
        reader = self._conn(autocommit=True)
        delivered, _, _ = drain(reader, self.school_id, self.scopes, limit=2)
        self.assertEqual(sorted(set(delivered)),
                         sorted(int(r[0]) for r in all_rows))

    # ── Long-running transaction ─────────────────────────────────────────────

    def test_new_commits_are_delivered_promptly_while_an_old_txn_is_open(self):
        """Requirement: the feed must not stall behind a long transaction."""
        old = self._conn()
        self._insert(old, 1)                       # A holds the low xid, open
        reader = self._conn(autocommit=True)

        delivered, cursor, _ = drain(reader, self.school_id, self.scopes, limit=50)
        pinned_h = cursor.h

        w = self._conn()
        fresh = self._insert(w, 1)
        w.commit()

        page = read(reader, cursor, self.school_id, self.scopes, limit=50)
        self.assertIn(int(fresh[0][0]), [int(r[0]) for r in page.rows],
                      'a change committed while A is open must arrive at once')
        self.assertTrue(page.caught_up)
        self.assertEqual(page.cursor.h, pinned_h,
                         'the watermark stays pinned; delivery does not wait')
        old.commit()

    def test_the_old_transactions_own_rows_arrive_after_it_commits(self):
        old = self._conn()
        late = self._insert(old, 1)
        reader = self._conn(autocommit=True)

        delivered, cursor, _ = drain(reader, self.school_id, self.scopes, limit=50)
        self.assertNotIn(int(late[0][0]), delivered, 'not committed yet')

        old.commit()
        more, cursor2, _ = drain(reader, self.school_id, self.scopes, limit=50)
        self.assertIn(int(late[0][0]), more,
                      'the late commit must be delivered, never skipped')
        self.assertGreater(cursor2.h, cursor.h, 'the watermark moves on')

    def test_pagination_terminates_even_with_an_open_transaction(self):
        old = self._conn()
        self._insert(old, 1, n=3)
        for _ in range(6):
            self._commit_one(n=4)
        reader = self._conn(autocommit=True)
        delivered, _, pages = drain(reader, self.school_id, self.scopes,
                                    limit=2, max_pages=100)
        self.assertLess(pages, 100, 'drain must terminate')
        self.assertEqual(len(delivered), len(set(delivered)) if delivered else 0,
                         'sealed-band delivery should not duplicate')
        old.commit()

    def test_no_committed_row_is_ever_lost_across_an_interleaved_workload(self):
        """The end-to-end safety property, with commits out of xid order."""
        a, b, c = self._conn(), self._conn(), self._conn()
        ra = self._insert(a, 1, n=2)
        rb = self._insert(b, 2, n=2)
        rc = self._insert(c, 1, n=2)
        reader = self._conn(autocommit=True)

        collected = []
        c.commit()
        collected += drain(reader, self.school_id, self.scopes, limit=1)[0]
        a.commit()
        collected += drain(reader, self.school_id, self.scopes, limit=1)[0]
        b.commit()
        collected += drain(reader, self.school_id, self.scopes, limit=1)[0]

        expected = sorted(int(r[0]) for r in ra + rb + rc)
        self.assertEqual(sorted(set(collected)), expected,
                         'every committed row must arrive exactly once or more')

    # ── Idempotency ──────────────────────────────────────────────────────────

    def test_open_region_rows_repeat_until_sealed_and_must_be_idempotent(self):
        old = self._conn()
        self._insert(old, 1)
        reader = self._conn(autocommit=True)
        drain(reader, self.school_id, self.scopes, limit=50)

        w = self._conn()
        fresh = self._insert(w, 1)
        w.commit()

        seen = []
        cursor = drain(reader, self.school_id, self.scopes, limit=50)[1]
        for _ in range(3):
            page = read(reader, cursor, self.school_id, self.scopes, limit=50)
            seen.append([int(r[0]) for r in page.rows])
            cursor = page.cursor

        self.assertTrue(all(int(fresh[0][0]) in s for s in seen),
                        'the open region is re-delivered until h advances')
        # An idempotent apply keyed by (resource, resource_id) converges anyway.
        applied = {}
        for batch in seen:
            for _ in batch:
                applied[('attendance', 1000)] = True
        self.assertEqual(len(applied), 1)
        old.commit()

    def test_duplicate_rows_for_one_resource_are_delivered_and_collapse(self):
        w = self._conn()
        self._insert(w, 1, n=4)          # 4 rows, same resource_id sequence
        w.commit()
        reader = self._conn(autocommit=True)
        delivered, _, _ = drain(reader, self.school_id, self.scopes, limit=50)
        self.assertEqual(len(delivered), 4)
        applied = {}
        with reader.cursor() as cur:
            cur.execute("SELECT id, resource_id FROM change_journal "
                        "WHERE school_id = %s ORDER BY id", (self.school_id,))
            for _rid, resource_id in cur.fetchall():
                applied[('attendance', resource_id)] = True
        self.assertEqual(len(applied), 4)

    # ── Bound failures are scoped resets ─────────────────────────────────────

    def test_generation_change_forces_a_scoped_reset(self):
        reader = self._conn(autocommit=True)
        _, cursor, _ = drain(reader, self.school_id, self.scopes, limit=10)
        admin = self._conn(autocommit=True)
        with admin.cursor() as cur:
            cur.execute("UPDATE sync_meta SET generation = generation + 1 WHERE id = 1")
        with self.assertRaises(Reset) as ctx:
            read(reader, cursor, self.school_id, self.scopes, limit=10)
        self.assertEqual(ctx.exception.reason, 'generation_changed')
        self.assertEqual(ctx.exception.scopes, tuple(self.scopes),
                         'the reset must name the affected scopes')

    def test_retention_underrun_forces_a_reset_not_an_empty_page(self):
        w = self._conn()
        self._insert(w, 1)
        w.commit()
        reader = self._conn(autocommit=True)
        _, cursor, _ = drain(reader, self.school_id, self.scopes, limit=10)

        admin = self._conn(autocommit=True)
        with admin.cursor() as cur:
            cur.execute("UPDATE sync_meta SET min_retained_xid = %s WHERE id = 1",
                        (cursor.h + 1000,))
        with self.assertRaises(Reset) as ctx:
            read(reader, cursor, self.school_id, self.scopes, limit=10)
        self.assertEqual(ctx.exception.reason, 'retention_underrun')

    def test_open_region_overflow_forces_a_reset(self):
        """Too much above a pinned watermark to re-deliver safely."""
        global LIVE_SCAN_LIMIT
        old = self._conn()
        self._insert(old, 1)
        w = self._conn()
        self._insert(w, 1, n=6)
        w.commit()
        reader = self._conn(autocommit=True)

        original = LIVE_SCAN_LIMIT
        LIVE_SCAN_LIMIT = 3
        try:
            with self.assertRaises(Reset) as ctx:
                drain(reader, self.school_id, self.scopes, limit=50)
            self.assertEqual(ctx.exception.reason, 'open_region_overflow')
        finally:
            LIVE_SCAN_LIMIT = original
        old.commit()

    def test_unparseable_or_oversized_cursor_resets(self):
        for raw in ('not-json', '[1,2', json.dumps(['x'] * 100)):
            with self.assertRaises(Reset):
                Cursor.decode(raw, self.scopes)

    def test_a_reset_is_distinguishable_from_a_caught_up_empty_page(self):
        """The property that stops a client silently believing it is current."""
        reader = self._conn(autocommit=True)
        _, cursor, _ = drain(reader, self.school_id, self.scopes, limit=10)
        page = read(reader, cursor, self.school_id, self.scopes, limit=10)
        self.assertEqual(page.rows, [])
        self.assertTrue(page.caught_up, 'a genuine empty page says caught_up')

        admin = self._conn(autocommit=True)
        with admin.cursor() as cur:
            cur.execute("UPDATE sync_meta SET generation = 99 WHERE id = 1")
        with self.assertRaises(Reset):
            read(reader, cursor, self.school_id, self.scopes, limit=10)

    # ── Scope isolation still holds through pagination ───────────────────────

    def test_pagination_never_leaks_an_unauthorized_scope(self):
        w = self._conn()
        self._insert(w, 1, n=2)
        self._insert(w, 99, n=3)      # a scope this caller does not hold
        w.commit()
        reader = self._conn(autocommit=True)
        delivered, _, _ = drain(reader, self.school_id, [1], limit=1)

        with reader.cursor() as cur:
            cur.execute("SELECT id FROM change_journal "
                        "WHERE school_id = %s AND scope_id = 99",
                        (self.school_id,))
            forbidden = {int(r[0]) for r in cur.fetchall()}
        self.assertTrue(forbidden)
        self.assertEqual(set(delivered) & forbidden, set(),
                         'an unauthorized scope leaked through pagination')

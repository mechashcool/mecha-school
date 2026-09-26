"""Pre-B2 — a sync cursor may not be built from XID or serial-ID order.

Executable proof for docs/adr/0001-sync-cursor-commit-order.md.

An XID is allocated at a transaction's FIRST WRITE, and a BIGSERIAL value at
INSERT time. Neither is a commit-order sequence, so a transaction can hold a
lower value and still commit later. A cursor that reads visible rows ordered by
`xid` (or `id`) and advances to the largest value seen therefore skips that late
commit permanently, with no error anywhere.

These tests drive real concurrent connections against the isolated instance and
demonstrate both halves: the naive cursor loses a committed attendance change,
and the snapshot-watermark cursor does not.

Nothing here touches application code: B2 is not implemented. The tests
exercise the `change_journal` table directly, which is exactly the surface B2
will build on.
"""
import os
import unittest
from urllib.parse import urlsplit

import psycopg2
import psycopg2.extras


# ── Cursor implementations under test ────────────────────────────────────────

def snapshot_xmin(conn):
    """Lowest still-running xid: everything below it has reached a final state."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_snapshot_xmin(pg_current_snapshot())::text::numeric")
        return cur.fetchone()[0]


def read_naive(conn, school_id, cursor_xid):
    """THE BROKEN ONE: visible rows above the cursor, advance to the max seen."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, xid FROM change_journal "
            "WHERE school_id = %s AND xid > %s ORDER BY xid, id",
            (school_id, cursor_xid),
        )
        rows = cur.fetchall()
    next_cursor = max((r[1] for r in rows), default=cursor_xid)
    return [r[0] for r in rows], next_cursor


def read_naive_by_id(conn, school_id, cursor_id):
    """Same defect, using the BIGSERIAL primary key instead of the xid."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM change_journal "
            "WHERE school_id = %s AND id > %s ORDER BY id",
            (school_id, cursor_id),
        )
        rows = cur.fetchall()
    next_cursor = max((r[0] for r in rows), default=cursor_id)
    return [r[0] for r in rows], next_cursor


def read_watermark(conn, school_id, high_watermark, pending, limit=1000):
    """ADR 0001: advance H only to the snapshot's xmin, carry a pending set.

    Returns (delivered_ids, H', P').
    """
    # (1) Take the watermark BEFORE reading, so it can never describe a state
    #     newer than the rows this read observed.
    xmin = snapshot_xmin(conn)

    # (2) Only committed rows are visible under READ COMMITTED, so an in-flight
    #     or rolled-back transaction contributes nothing.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, xid FROM change_journal "
            "WHERE school_id = %s AND xid >= %s "
            "  AND NOT (xid = ANY(%s::numeric[])) "
            "ORDER BY xid, id LIMIT %s",
            (school_id, high_watermark, [str(p) for p in sorted(pending)], limit),
        )
        rows = cur.fetchall()

    delivered = [r[0] for r in rows]
    truncated = len(rows) == limit

    # (3) Advance the watermark only to xmin — never to a value read from rows.
    if truncated:
        new_h = high_watermark                       # do not advance mid-page
    else:
        new_h = max(high_watermark, xmin)

    new_p = {r[1] for r in rows if r[1] >= new_h}
    new_p |= {p for p in pending if p >= new_h}
    return delivered, new_h, new_p


# ── Test harness ─────────────────────────────────────────────────────────────

class SyncCursorConcurrencyTest(unittest.TestCase):
    """Real concurrent connections. No app, no ORM — raw transaction control."""

    @classmethod
    def setUpClass(cls):
        url = os.environ['TEST_DATABASE_URL']
        parts = urlsplit(url)
        assert (parts.hostname or '').lower() in ('127.0.0.1', 'localhost', '::1')
        assert (parts.path or '').lstrip('/').endswith('_test')
        cls.dsn = url

        # A committed school row to satisfy change_journal.school_id.
        conn = psycopg2.connect(cls.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schools (school_name, code, capacity, is_active) "
                "VALUES ('Cursor Concurrency', 'CURSOR9', 0, true) RETURNING id"
            )
            cls.school_id = cur.fetchone()[0]
        conn.close()

    @classmethod
    def tearDownClass(cls):
        conn = psycopg2.connect(cls.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DELETE FROM change_journal WHERE school_id = %s",
                        (cls.school_id,))
            cur.execute("DELETE FROM schools WHERE id = %s", (cls.school_id,))
        conn.close()

    def setUp(self):
        self.conns = []
        conn = psycopg2.connect(self.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DELETE FROM change_journal WHERE school_id = %s",
                        (self.school_id,))
        conn.close()

    def tearDown(self):
        for conn in self.conns:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass

    def _connect(self, autocommit=False):
        conn = psycopg2.connect(self.dsn)
        conn.autocommit = autocommit
        self.conns.append(conn)
        return conn

    def _insert(self, conn, resource_id, op='upsert'):
        """Write one journal row, taking this transaction's real xid.

        pg_current_xact_id() ALLOCATES the xid on first write — that allocation
        is the behaviour under test.
        """
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO change_journal "
                "(school_id, scope_type, scope_id, resource, resource_id, op, xid) "
                "VALUES (%s, 'student', %s, 'attendance', %s, %s, "
                "        pg_current_xact_id()::text::numeric(20,0)) "
                "RETURNING id, xid",
                (self.school_id, resource_id, resource_id, op),
            )
            return cur.fetchone()

    # ── The core proof ───────────────────────────────────────────────────────

    def test_naive_xid_cursor_permanently_skips_a_late_commit(self):
        """A commits second but holds the LOWER xid; the naive cursor loses it."""
        conn_a = self._connect()
        conn_b = self._connect()
        reader = self._connect(autocommit=True)

        # (1) A writes and STAYS OPEN.
        id_a, xid_a = self._insert(conn_a, resource_id=1)

        # (2) B writes and COMMITS first.
        id_b, xid_b = self._insert(conn_b, resource_id=2)
        conn_b.commit()

        self.assertGreater(xid_b, xid_a, 'B must hold the later xid')
        self.assertGreater(id_b, id_a, 'B must hold the later serial id')

        # (3) Reader runs while A is still open: only B is visible.
        seen, naive_cursor = read_naive(reader, self.school_id, 0)
        self.assertEqual(seen, [id_b])
        self.assertEqual(naive_cursor, xid_b)

        # (4) A commits. Its row is now permanently visible...
        conn_a.commit()
        with reader.cursor() as cur:
            cur.execute("SELECT count(*) FROM change_journal WHERE school_id = %s",
                        (self.school_id,))
            self.assertEqual(cur.fetchone()[0], 2, 'both rows are committed')

        # (5) ...but the naive cursor has already moved past it. THE DEFECT.
        seen_after, _ = read_naive(reader, self.school_id, naive_cursor)
        self.assertNotIn(
            id_a, seen_after,
            'expected the naive cursor to SKIP the late commit — if this now '
            'passes, re-derive the ADR before trusting it')
        self.assertEqual(seen_after, [], 'the change is lost with no error')

    def test_naive_serial_id_cursor_skips_it_too(self):
        """BIGSERIAL is allocated at INSERT, so ORDER BY id has the same hole."""
        conn_a = self._connect()
        conn_b = self._connect()
        reader = self._connect(autocommit=True)

        id_a, _ = self._insert(conn_a, resource_id=1)
        id_b, _ = self._insert(conn_b, resource_id=2)
        conn_b.commit()

        seen, cursor_id = read_naive_by_id(reader, self.school_id, 0)
        self.assertEqual(seen, [id_b])
        self.assertEqual(cursor_id, id_b)

        conn_a.commit()
        seen_after, _ = read_naive_by_id(reader, self.school_id, cursor_id)
        self.assertNotIn(id_a, seen_after, 'serial-id cursor skips it as well')

    def test_watermark_cursor_delivers_the_late_commit(self):
        """Same timeline, ADR 0001 cursor: nothing is skipped."""
        conn_a = self._connect()
        conn_b = self._connect()
        reader = self._connect(autocommit=True)

        id_a, xid_a = self._insert(conn_a, resource_id=1)
        id_b, xid_b = self._insert(conn_b, resource_id=2)
        conn_b.commit()

        # Read while A is still open.
        delivered1, h1, p1 = read_watermark(reader, self.school_id, 0, set())
        self.assertEqual(delivered1, [id_b], 'only B is committed yet')

        # The watermark must NOT have passed the still-open transaction A.
        self.assertLessEqual(
            h1, xid_a,
            'H advanced past an in-flight transaction — that is the bug')

        conn_a.commit()

        # Second read picks up A.
        delivered2, h2, p2 = read_watermark(reader, self.school_id, h1, p1)
        self.assertIn(id_a, delivered2, 'the late commit must be delivered')

        # And the watermark now moves past both, since nothing is in flight.
        self.assertGreater(h2, max(xid_a, xid_b))

        # Union of both reads covers every committed row exactly once.
        self.assertEqual(sorted(delivered1 + delivered2), sorted([id_a, id_b]))

    def test_watermark_cursor_converges_and_stops_repeating(self):
        """Once nothing is in flight, a further read returns nothing new."""
        conn_a = self._connect()
        reader = self._connect(autocommit=True)

        id_a, _ = self._insert(conn_a, resource_id=1)
        conn_a.commit()

        d1, h1, p1 = read_watermark(reader, self.school_id, 0, set())
        self.assertEqual(d1, [id_a])

        d2, h2, p2 = read_watermark(reader, self.school_id, h1, p1)
        self.assertEqual(d2, [], 'a caught-up client must receive an empty page')
        self.assertGreaterEqual(h2, h1)

    # ── Rolled-back work is never delivered ──────────────────────────────────

    def test_rolled_back_transaction_never_appears(self):
        conn_a = self._connect()
        reader = self._connect(autocommit=True)

        id_a, xid_a = self._insert(conn_a, resource_id=99)
        conn_a.rollback()

        delivered, h, p = read_watermark(reader, self.school_id, 0, set())
        self.assertEqual(delivered, [], 'an aborted write must never be sent')

        # Still absent after the watermark sweeps past its xid.
        delivered2, _, _ = read_watermark(reader, self.school_id, h, p)
        self.assertEqual(delivered2, [])
        with reader.cursor() as cur:
            cur.execute("SELECT count(*) FROM change_journal WHERE school_id = %s",
                        (self.school_id,))
            self.assertEqual(cur.fetchone()[0], 0)

    def test_rollback_of_a_late_transaction_does_not_block_the_watermark(self):
        """A holds the watermark back, then aborts; the reader must recover."""
        conn_a = self._connect()
        conn_b = self._connect()
        reader = self._connect(autocommit=True)

        _, xid_a = self._insert(conn_a, resource_id=1)
        id_b, _ = self._insert(conn_b, resource_id=2)
        conn_b.commit()

        d1, h1, p1 = read_watermark(reader, self.school_id, 0, set())
        self.assertEqual(d1, [id_b])
        self.assertLessEqual(h1, xid_a)

        conn_a.rollback()

        d2, h2, p2 = read_watermark(reader, self.school_id, h1, p1)
        self.assertEqual(d2, [], 'the aborted row must not surface')
        self.assertGreater(h2, xid_a, 'the watermark must move on after abort')

    # ── Duplicate delivery must be safe ──────────────────────────────────────

    def test_duplicate_delivery_is_possible_and_must_be_idempotent(self):
        """Collapsing the pending set re-delivers rows; applying must be a no-op.

        This is the trade the ADR makes deliberately: bounded cursor state at
        the cost of duplicates. Duplicates are safe; gaps are not.
        """
        conn_a = self._connect()
        reader = self._connect(autocommit=True)

        id_a, xid_a = self._insert(conn_a, resource_id=7)
        conn_a.commit()

        d1, h1, p1 = read_watermark(reader, self.school_id, 0, set())
        self.assertEqual(d1, [id_a])

        # Simulate the |P| cap collapsing the set (step 5 of the algorithm):
        # H falls back to min(P), P empties, so the row is read again.
        if p1:
            collapsed_h, collapsed_p = min(p1), set()
            d2, _, _ = read_watermark(reader, self.school_id, collapsed_h,
                                      collapsed_p)
            self.assertIn(id_a, d2, 'the row is legitimately re-delivered')

        # A client applying by (resource, resource_id) converges either way.
        applied = {}
        for _ in range(3):
            for row_id in [id_a, id_a]:
                applied[('attendance', 7)] = row_id
        self.assertEqual(len(applied), 1, 'idempotent apply keeps one record')

    def test_interleaved_writers_lose_nothing_across_repeated_reads(self):
        """Three overlapping transactions committing out of xid order."""
        conn_a = self._connect()
        conn_b = self._connect()
        conn_c = self._connect()
        reader = self._connect(autocommit=True)

        id_a, _ = self._insert(conn_a, resource_id=1)   # oldest xid
        id_b, _ = self._insert(conn_b, resource_id=2)
        id_c, _ = self._insert(conn_c, resource_id=3)   # newest xid

        conn_c.commit()                                  # commits first
        delivered, h, p = read_watermark(reader, self.school_id, 0, set())
        collected = list(delivered)

        conn_a.commit()                                  # then the oldest xid
        d, h, p = read_watermark(reader, self.school_id, h, p)
        collected += d

        conn_b.commit()
        d, h, p = read_watermark(reader, self.school_id, h, p)
        collected += d

        self.assertEqual(sorted(set(collected)), sorted([id_a, id_b, id_c]),
                         'every committed row must arrive exactly once')
        self.assertEqual(len(collected), len(set(collected)),
                         'and none should be duplicated in this timeline')


class TransactionIdApiTest(unittest.TestCase):
    """Pin the PostgreSQL 17/18 transaction-id API and the required cast."""

    @classmethod
    def setUpClass(cls):
        cls.conn = psycopg2.connect(os.environ['TEST_DATABASE_URL'])
        cls.conn.autocommit = True

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_pg_current_xact_id_returns_xid8(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_typeof(pg_current_xact_id())::text")
            self.assertEqual(cur.fetchone()[0], 'xid8')

    def test_there_is_no_direct_xid8_to_numeric_cast(self):
        """Documents WHY the code casts through text — not a style choice."""
        with self.conn.cursor() as cur:
            with self.assertRaises(psycopg2.errors.CannotCoerce):
                cur.execute("SELECT pg_current_xact_id()::numeric")

    def test_text_cast_round_trips_into_numeric_20(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_current_xact_id()::text::numeric(20,0)")
            value = cur.fetchone()[0]
        self.assertGreater(value, 0)

    def test_numeric_20_holds_the_full_unsigned_64_bit_range(self):
        """xid8 reaches 2**64-1, which overflows BIGINT — hence NUMERIC(20,0)."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT (2::numeric ^ 64 - 1)::numeric(20,0)")
            self.assertEqual(cur.fetchone()[0], 18446744073709551615)
            with self.assertRaises(psycopg2.errors.NumericValueOutOfRange):
                cur.execute("SELECT 18446744073709551615::bigint")

    def test_utc_default_is_not_server_local_wall_clock(self):
        """The corrected default must be UTC regardless of session TimeZone."""
        with self.conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'Asia/Baghdad'")
            cur.execute("SELECT (now() AT TIME ZONE 'utc') - now()::timestamp")
            offset = cur.fetchone()[0]
            self.assertNotEqual(offset.total_seconds(), 0,
                                'Asia/Baghdad is +03, so UTC must differ')
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute("SELECT (now() AT TIME ZONE 'utc') - now()::timestamp")
            self.assertEqual(cur.fetchone()[0].total_seconds(), 0)

# ADR 0001 — The sync cursor must not order by XID or by serial ID

**Status:** accepted (pre-B2)
**Applies to:** `change_journal`, and any future outbox-style feed in this project
**Executable proof:** `tests/test_sync_cursor_concurrency.py` (commit-order
skip) and `tests/test_sync_cursor_pagination.py` (bounded state machine)
**Verified against:** PostgreSQL 18.3 (isolated instance, 127.0.0.1:55432).
Production is PostgreSQL 17.6 (`server_version_num = 170006`, Supabase),
verified read-only by the operator. Every function used here
(`pg_current_xact_id`, `pg_current_snapshot`, `pg_snapshot_xmin`,
`pg_snapshot_xip`) has existed since PostgreSQL 13.

## Context

A client synchronizes by presenting a cursor and receiving every change it has
not yet seen. The obvious implementation is:

```sql
SELECT * FROM change_journal WHERE xid > :cursor ORDER BY xid, id LIMIT :n;
-- then: cursor := max(xid seen)
```

This is wrong, and so is the `ORDER BY id` variant.

## The defect

**A transaction ID is allocated at a transaction's first write, not at its
commit.** `BIGSERIAL` values are likewise drawn at INSERT time. Neither column
is a commit-order sequence, so a transaction can hold a *lower* xid/id and
still become visible *later* than one holding a higher value.

Timeline that breaks the naive cursor:

| # | Transaction A | Transaction B | Reader |
|---|---|---|---|
| 1 | `BEGIN`; INSERT → gets `xid=100`, `id=500` | | |
| 2 | *(still open)* | `BEGIN`; INSERT → `xid=101`, `id=501` | |
| 3 | *(still open)* | `COMMIT` | |
| 4 | *(still open)* | | reads: sees **only** row 501. Sets cursor `= 101` |
| 5 | `COMMIT` | | |
| 6 | | | reads `WHERE xid > 101` → **row 500 is never returned** |

Row 500 was committed, is permanently visible, and is permanently skipped. For
attendance this means a real record silently never reaches the device, and no
error is raised anywhere — the client believes it is up to date.

Larger `LIMIT`s, retries, and longer polling intervals do not fix it. The window
is exactly "how long a writing transaction stays open", which under load is
precisely when it matters most.

## Decision

An earlier draft of this ADR used a pair `(H, P)`: a watermark plus a *set* of
already-delivered xids above it. That set is unbounded — while one old
transaction stays open, `H` is pinned and `P` grows with every transaction that
commits above it — so it is superseded by the state machine below, which holds
only **six scalars** and no set at all.

The journal is split into two regions by the database's own snapshot:

```sql
SELECT pg_snapshot_xmin(pg_current_snapshot());   -- "xmin"
```

`xmin` is the lowest still-running transaction id, so:

* **Sealed region — `xid < H`.** Every transaction below `H` has reached a final
  state. No row can ever be added here. It is therefore safe to paginate with an
  ordinary keyset cursor.
* **Open region — `xid >= H`.** Transactions here may still be in flight, so
  rows can still appear *below* a position already passed. It is never
  paginated and no position inside it is ever recorded.

### Cursor

Six scalars, nothing else:

| Field | Meaning |
|---|---|
| `gen` | `sync_meta.generation` at issue time |
| `mode` | `LIVE` or `CATCHUP` |
| `h` | sealed watermark: everything below is final **and** delivered |
| `h_target` | end of the band being drained (CATCHUP only) |
| `pos_xid`, `pos_id` | keyset position inside the sealed band (CATCHUP only) |

**Maximum encoded size: 128 bytes.** `h`, `h_target` and `pos_xid` are at most
20 digits (xid8 is unsigned 64-bit), `pos_id` at most 19, `gen` at most 10,
`mode` one character. There is no field whose size depends on how many
transactions are open, how long one has been open, or how many rows exist. A
cursor that does not parse, or that exceeds the cap, is a **scoped reset**, not
an error and not an empty page.

### Read algorithm

```
read(cursor, scopes, limit):

  if cursor.gen != sync_meta.generation:            return RESET(scopes)
  if cursor.h   <  sync_meta.min_retained_xid:      return RESET(scopes)

  xmin := pg_snapshot_xmin(pg_current_snapshot())

  # ---- CATCHUP: drain a sealed band; it cannot gain rows ----
  if cursor.mode == CATCHUP:
      if cursor.pos_xid < sync_meta.min_retained_xid:  return RESET(scopes)

      rows := SELECT xid, id, ...
              WHERE  xid >= cursor.pos_xid AND xid < cursor.h_target
                AND  (xid, id) > (cursor.pos_xid, cursor.pos_id)
                AND  <authorized scope predicates>
              ORDER BY xid, id
              LIMIT limit

      if rows is empty:                       # band fully drained
          cursor := LIVE(h = cursor.h_target)
          # fall through to the LIVE branch so the caller still gets fresh rows
      else:
          cursor.pos_xid, cursor.pos_id := last(rows).xid, last(rows).id
          return PAGE(rows, cursor, caught_up = false)

  # ---- LIVE: sealed band drained; serve the open region ----
  if xmin > cursor.h:                         # a new band has sealed
      return PAGE([], CATCHUP(h = cursor.h, h_target = xmin,
                              pos = (cursor.h, 0)), caught_up = false)

  open_rows := SELECT ... WHERE xid >= cursor.h
                  AND <authorized scope predicates>
                ORDER BY xid, id
                LIMIT LIVE_SCAN_LIMIT + 1

  if len(open_rows) > LIVE_SCAN_LIMIT:        # bound exceeded
      return RESET(scopes)

  return PAGE(open_rows, cursor, caught_up = true)
```

### Why each hazard is closed

**No committed change is skipped.** `h` only ever advances to a value of `xmin`
observed *before* the rows were read. A still-open transaction A holds
`xmin <= xid_A`, so `xid_A >= h` always — A's rows sit in the open region, are
served by the LIVE branch immediately once A commits, and are swept into a
sealed band on the first read after `xmin` passes `xid_A`.

**`P` cannot grow without bound** — there is no `P`. While A is open, `h` is
pinned and the cursor simply does not change; it does not accumulate anything.

**Pagination cannot mis-mark a split XID.** The position is the pair
`(pos_xid, pos_id)`, never a bare xid. Splitting one xid's rows across pages is
therefore *safe and allowed*: resuming from `(xid, id)` continues inside the
same xid rather than skipping the remainder. Crucially this is only done in the
**sealed** region, where that xid's row set is already complete and immutable.
The open region is never paginated, so a partially-delivered xid there is never
recorded as delivered at all. A single xid with more rows than `limit` makes
normal forward progress, page by page, with no special case.

**Promptness while an old transaction is open.** The LIVE branch serves the open
region on every poll, so an attendance change that commits while A is still open
is delivered on the very next request — it does not wait for A. The cost is that
the open region is re-sent until `h` advances, which is exactly why idempotent
apply is mandatory.

**Bounded work and memory.** The sealed drain is `LIMIT limit` per page; the
open scan is `LIMIT LIVE_SCAN_LIMIT + 1`. Server memory per request and client
cursor size are both constant. `LIVE_SCAN_LIMIT` is the one knob: it caps how
much can accumulate above a pinned `h` before the server stops guessing.

**Infinite pagination is impossible.** In CATCHUP, `(pos_xid, pos_id)` strictly
increases within a band with a fixed upper bound `h_target`, and the band is
sealed so it cannot grow — the drain always terminates. In LIVE, no position
advances, so there is no loop to spin.

**Duplicates are expected, and must be safe.** The open region is re-delivered
on each poll until `h` advances, and a dropped response re-delivers a page.
Every change must apply as an idempotent upsert keyed by
`(resource, resource_id)`, and `delete` must be idempotent too. This is a
requirement on the client, not an optimization.

**Every bound failure is a scoped reset.** Generation mismatch, retention
underrun, unparseable/oversized cursor, and open-region overflow all return
`RESET(scopes)` — an explicit instruction to rebuild the named scopes from the
authoritative read endpoints. It is never a silent skip, and never an empty page
that a client would read as "you are up to date".

**Retention is compatible.** Pruning only ever removes rows below
`min_retained_xid`. Both `h` and `pos_xid` are checked against it before any
read, so a cursor that has fallen behind the retention floor is reset rather
than served an incomplete band. Because a sealed band is drained in ascending
`(xid, id)` order and `min_retained_xid` only advances, a drain in progress can
be invalidated mid-way — which the `pos_xid` check catches on the next page.

## Rejected alternatives

| Alternative | Why rejected |
|---|---|
| `ORDER BY xid` / `ORDER BY id`, advance to max seen | The defect above. Explicitly rejected. |
| `ORDER BY created_at`, advance to max | Same defect (the timestamp is taken before commit), plus clock and DST hazards. |
| A commit-time sequence assigned by a trigger | A trigger still fires before commit; it does not observe commit order either. |
| Serializable isolation | Does not make xids commit-ordered, and would add retry load to every writer. |
| `pg_logical_slot_get_changes` / logical replication | Genuine commit order, but needs a replication slot and elevated privileges we do not have on managed Supabase, and an unconsumed slot can pin WAL and take the database down. Out of proportion here. |
| Wait until no transaction is older than the cursor | Unbounded stall; a single long transaction halts all clients. |

## Notes for B2

* Populate `xid` explicitly:
  `pg_current_xact_id()::text::numeric(20, 0)`.
  There is **no** direct `xid8 -> numeric` cast — `SELECT
  pg_current_xact_id()::numeric` fails with *"cannot cast type xid8 to
  numeric"* (verified on the isolated instance). `NUMERIC(20,0)` is required
  because xid8 covers the full unsigned 64-bit range, which overflows `BIGINT`.
* Do **not** use `txid_current()`; it is the deprecated 32-bit-wraparound API
  and production is PostgreSQL 17.6.
* The journal row must be written **inside the same transaction** as the
  business write, so that a rolled-back business change cannot leave a journal
  entry behind.
* Retention pruning must never remove a row still needed by a live cursor:
  prune only below `min_retained_xid`, and a client presenting a cursor below
  that value must be told to rebuild — never handed an empty page.
* Entitlement is *not* part of this mechanism. Scope changes travel via
  `sync_principal_state.scopes_version`; journal rows are cascade-deleted with
  their school and pruned by retention, so they can never be the authority on
  what a principal may see.

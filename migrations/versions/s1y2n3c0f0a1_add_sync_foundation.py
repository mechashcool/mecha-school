"""Mobile synchronization foundation (Part B1): change_journal + sync_meta
+ sync_principal_state.

Purely ADDITIVE and INERT:
  * creates three new tables and their indexes,
  * seeds the sync_meta singleton row (generation=1, capture_enabled=false),
  * touches NO existing table, column, index, constraint, trigger, or data.

Nothing in the application reads or writes these tables yet. Capture hooks,
/sync/* endpoints, cursor logic, and the signal service are later parts and are
gated behind SYNC_JOURNAL_ENABLED / SYNC_SIGNAL_ENABLED, which both default to
false.

`change_journal.xid` is intentionally created with NO server default. Part B2
will populate it explicitly from ``pg_current_xact_id()``. Production is
PostgreSQL 17.6 (server_version_num 170006, Supabase), verified read-only by
the operator, so that function is available; the deprecated ``txid_current()``
is not used. NOTE (verified on the isolated instance): there is NO direct
``xid8 -> numeric`` cast — ``SELECT pg_current_xact_id()::numeric`` fails with
"cannot cast type xid8 to numeric". The conversion must go through text:
``pg_current_xact_id()::text::numeric(20, 0)``. NUMERIC(20,0) is kept because
xid8 spans the full unsigned 64-bit range, which overflows BIGINT.

An XID is allocated at a transaction's first write, NOT at commit, so it is not
a commit-order sequence. The same is true of the BIGSERIAL `id`. Reading rows
ordered by either column and advancing a cursor to the largest value seen will
permanently skip a late commit from an older still-open transaction. See
docs/adr/0001-sync-cursor-commit-order.md and
tests/test_sync_cursor_concurrency.py, which demonstrate the skip and the
snapshot-based watermark that avoids it.

Revision ID: s1y2n3c0f0a1
Revises: c3d9f5b2a7e1
Create Date: 2026-09-04
"""
from alembic import op
import sqlalchemy as sa


revision = 's1y2n3c0f0a1'
down_revision = 'c3d9f5b2a7e1'
branch_labels = None
depends_on = None

# Naive UTC, matching the project convention (every existing timestamp column
# is `timestamp without time zone` holding a datetime.utcnow() value).
#
# A bare now() would be WRONG here: now() is timestamptz, and storing it into a
# naive column silently keeps the SERVER'S LOCAL wall-clock. On the isolated
# instance (TimeZone = Asia/Baghdad) that is +03, and two sessions with
# different TimeZone settings would write different values for the same instant
# — which would corrupt age-based retention pruning. `AT TIME ZONE 'utc'`
# converts to UTC first and yields a naive timestamp.
UTC_NOW = sa.text("(now() AT TIME ZONE 'utc')")


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if 'change_journal' not in existing:
        op.create_table(
            'change_journal',
            sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column('school_id', sa.Integer(), nullable=False),
            sa.Column('academic_year_id', sa.Integer(), nullable=True),
            sa.Column('scope_type', sa.String(length=24), nullable=False),
            sa.Column('scope_id', sa.Integer(), nullable=False),
            sa.Column('resource', sa.String(length=32), nullable=False),
            sa.Column('resource_id', sa.Integer(), nullable=True),
            sa.Column('op', sa.String(length=16), nullable=False),
            sa.Column('xid', sa.Numeric(precision=20, scale=0), nullable=False),
            sa.Column('created_at', sa.DateTime(), server_default=UTC_NOW,
                      nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_change_journal'),
            sa.ForeignKeyConstraint(['school_id'], ['schools.id'],
                                    name='fk_change_journal_school_id',
                                    ondelete='CASCADE'),
            sa.CheckConstraint(
                "op IN ('upsert', 'delete', 'reset', 'scopes_changed')",
                name='ck_change_journal_op',
            ),
        )
        # Primary read path (Part B2): one school, the caller's authorized
        # scopes, ordered by commit position. Kept as a single composite index
        # so a client never scans a school's whole history.
        op.create_index('ix_change_journal_scope', 'change_journal',
                        ['school_id', 'scope_type', 'scope_id', 'xid', 'id'])
        # Retention floor lookups and pruning by commit position.
        op.create_index('ix_change_journal_xid', 'change_journal', ['xid'])
        # Age-based pruning.
        op.create_index('ix_change_journal_created_at', 'change_journal',
                        ['created_at'])

    if 'sync_meta' not in existing:
        op.create_table(
            'sync_meta',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('generation', sa.Integer(), server_default='1',
                      nullable=False),
            sa.Column('capture_enabled', sa.Boolean(),
                      server_default=sa.false(), nullable=False),
            sa.Column('min_retained_xid', sa.Numeric(precision=20, scale=0),
                      nullable=True),
            sa.Column('updated_at', sa.DateTime(), server_default=UTC_NOW,
                      nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_sync_meta'),
            sa.CheckConstraint('id = 1', name='ck_sync_meta_singleton'),
        )
        # Seed the singleton in its safe, disabled state. ON CONFLICT keeps the
        # migration idempotent if it is ever re-run against a partial upgrade.
        op.execute(
            "INSERT INTO sync_meta (id, generation, capture_enabled) "
            "VALUES (1, 1, false) ON CONFLICT (id) DO NOTHING"
        )

    if 'sync_principal_state' not in existing:
        # Durable per-principal scope version.
        #
        # WHY A TABLE AND NOT THE JOURNAL: journal rows are cascade-deleted with
        # their school and are pruned by retention, so a scopes_version derived
        # from op='scopes_changed' rows would silently reset. Entitlement state
        # must outlive the change feed.
        #
        # The principal is `users.id`. Every authenticated caller — parent,
        # teacher, admin, school manager, super admin — is a row in `users`, and
        # the mobile JWT path re-loads that row per request, so there is no
        # second principal model to mirror.
        #
        # school_id is NULLABLE on purpose: it mirrors `users.school_id`, which
        # is NULL for super admins (see the User docstring: "school_id = NULL ->
        # super-admin"). Making it NOT NULL would invent a tenant for a
        # principal the real model says has none.
        op.create_table(
            'sync_principal_state',
            sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('school_id', sa.Integer(), nullable=True),
            # BIGINT, not INTEGER: this is a permanent monotonic counter that is
            # bumped on every scope change for the life of the principal. INTEGER
            # would impose an avoidable 2^31 ceiling on a value that only ever
            # grows and can never be reset without forcing a client rebuild.
            sa.Column('scopes_version', sa.BigInteger(), server_default='1',
                      nullable=False),
            sa.Column('updated_at', sa.DateTime(), server_default=UTC_NOW,
                      nullable=False),
            sa.PrimaryKeyConstraint('id', name='pk_sync_principal_state'),
            # Definitive account deletion removes the principal's sync state.
            # school_cleanup.SCHOOL_DELETE_ORDER deletes User rows before the
            # School row, so this CASCADE fires on school teardown too, and the
            # FK can never block an existing delete path.
            sa.ForeignKeyConstraint(['user_id'], ['users.id'],
                                    name='fk_sync_principal_state_user_id',
                                    ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['school_id'], ['schools.id'],
                                    name='fk_sync_principal_state_school_id',
                                    ondelete='CASCADE'),
            sa.UniqueConstraint('user_id',
                                name='uq_sync_principal_state_user_id'),
            sa.CheckConstraint('scopes_version >= 1',
                               name='ck_sync_principal_state_version'),
        )
        # Tenant-safe lookup: a read path must never scan across schools.
        op.create_index('ix_sync_principal_state_school', 'sync_principal_state',
                        ['school_id', 'user_id'])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    # Drop in reverse creation order. Indexes and constraints are removed with
    # their table, so no existing object is touched by this downgrade.
    if 'sync_principal_state' in existing:
        op.drop_index('ix_sync_principal_state_school',
                      table_name='sync_principal_state')
        op.drop_table('sync_principal_state')

    if 'sync_meta' in existing:
        op.drop_table('sync_meta')

    if 'change_journal' in existing:
        op.drop_index('ix_change_journal_created_at', table_name='change_journal')
        op.drop_index('ix_change_journal_xid', table_name='change_journal')
        op.drop_index('ix_change_journal_scope', table_name='change_journal')
        op.drop_table('change_journal')

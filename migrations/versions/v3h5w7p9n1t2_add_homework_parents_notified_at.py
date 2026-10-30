"""add homework.parents_notified_at (delayed homework notification marker)

ONE new nullable column plus ONE partial index:

  homework.parents_notified_at  TIMESTAMP WITHOUT TIME ZONE
      DEFAULT timezone('utc', now())        (naive UTC, the project convention)
  ix_homework_notify_pending ON homework (publish_date, id)
      WHERE parents_notified_at IS NULL

Meaning: NULL = the parents of this homework have NOT been notified yet and
the outbox worker's sweep should notify them once the homework publishes.
Any non-NULL value = handled (notified, or deliberately skipped).

Why the DEFAULT instead of a backfill UPDATE
────────────────────────────────────────────
timezone('utc', now()) is non-volatile, so on PostgreSQL 11+ ADD COLUMN stores
it once in the catalog ("missing value"): no table rewrite, no row scan, no
UPDATE. Consequences:
  * every EXISTING row reads non-NULL → never pending → never resent;
  * an INSERT from OLD application code (which does not know the column)
    gets the default → never pending; old code notifies inline as before;
  * only NEW code with HOMEWORK_PUBLISH_DEFER_ENABLED explicitly writes NULL.

Locking and retry
─────────────────
Step 1 (transactional): ADD COLUMN IF NOT EXISTS under lock_timeout 5s — a
catalog-only change needing a brief ACCESS EXCLUSIVE lock; on timeout nothing
is applied and the migration can simply be re-run.

Step 2 (autocommit, NOT part of step 1's transaction): CREATE INDEX
CONCURRENTLY — writes are never blocked. The index is almost empty (every
existing row is non-NULL). An interrupted CONCURRENTLY build leaves an INVALID
index; a re-run detects it (pg_index.indisvalid), drops it CONCURRENTLY and
rebuilds it. A VALID existing index is left untouched.

Partial-application recovery: if step 2 fails, step 1 is already committed and
alembic_version is NOT advanced. Re-running `flask db upgrade` is safe: step 1
is IF NOT EXISTS and step 2 repairs/creates the index.

Requires PostgreSQL 11+ (fast default) and a session-mode connection for
CONCURRENTLY (Supabase pooler port 5432 is session mode; transaction mode
would refuse it).

Downgrade: drop the index CONCURRENTLY, then the column (catalog-only).
Downgrade ONLY when no homework is pending (parents_notified_at IS NULL),
otherwise those homework would never be notified.

Revision ID: v3h5w7p9n1t2
Revises: t7h8w9i0m1g2
"""
from alembic import op
import sqlalchemy as sa


revision = 'v3h5w7p9n1t2'
down_revision = 't7h8w9i0m1g2'
branch_labels = None
depends_on = None

INDEX = 'ix_homework_notify_pending'


def _index_state(bind):
    """None = absent, True = valid, False = INVALID (interrupted build)."""
    return bind.execute(sa.text(
        'SELECT i.indisvalid FROM pg_index i '
        'JOIN pg_class c ON c.oid = i.indexrelid '
        'WHERE c.relname = :name'), {'name': INDEX}).scalar()


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        op.add_column('homework', sa.Column(
            'parents_notified_at', sa.DateTime(), nullable=True,
            server_default=sa.text('CURRENT_TIMESTAMP')))
        op.create_index(INDEX, 'homework', ['publish_date', 'id'])
        return

    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE homework ADD COLUMN IF NOT EXISTS parents_notified_at "
               "TIMESTAMP WITHOUT TIME ZONE DEFAULT timezone('utc', now())")

    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        try:
            state = _index_state(op.get_bind())
            if state is False:
                op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {INDEX}')
            if state is not True:
                op.execute(f'CREATE INDEX CONCURRENTLY {INDEX} '
                           'ON homework (publish_date, id) '
                           'WHERE parents_notified_at IS NULL')
        finally:
            op.execute('RESET lock_timeout')


def downgrade():
    bind = op.get_bind()
    # Refuse rather than silently drop notifications that are still owed:
    # without the column, pending homework would never be announced. Drain
    # first (outbox_worker homework-status / homework-flush --yes).
    pending = bind.execute(sa.text(
        'SELECT count(*) FROM homework WHERE parents_notified_at IS NULL')).scalar()
    if pending:
        raise RuntimeError(
            f'refusing to downgrade: {pending} homework row(s) still await parent '
            'notification (parents_notified_at IS NULL). Drain them first.')
    if bind.dialect.name != 'postgresql':
        op.drop_index(INDEX, table_name='homework')
        op.drop_column('homework', 'parents_notified_at')
        return

    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        try:
            op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS {INDEX}')
        finally:
            op.execute('RESET lock_timeout')

    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute('ALTER TABLE homework DROP COLUMN IF EXISTS parents_notified_at')

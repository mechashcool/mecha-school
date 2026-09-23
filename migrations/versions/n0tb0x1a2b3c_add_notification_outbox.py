"""add durable notification outbox

One new table: notification_outbox. It holds a push-delivery JOB — one row per
(notification event, parent user, device token) — written inside the SAME
transaction as the business change that caused it, so attendance and the
notifications it promised commit together or not at all.

PURELY ADDITIVE. One new table and its indexes. No existing table, column,
constraint, index or row is read, altered, backfilled or deleted. Because the
table is created empty, nothing here takes a lock on populated production data:
there is no ALTER on push_notifications, notifications, mobile_device_tokens,
student_attendance or any institute table.

WHY NOT REUSE push_notifications: that table is the delivery LOG — a row is
written AFTER an attempt, with status 'sent'/'failed'. It carries no attempt
counter, no due time and no worker lease, so turning it into a queue would mean
ALTERing a large, actively written table and adding indexes to populated data.
A new empty table avoids all of that and keeps the delivery log's meaning
unchanged.

WHY PER DEVICE TOKEN: a parent with two phones must not have a successful
delivery repeated because the other phone timed out, and a permanent
registration failure must retire exactly one token. Each row therefore carries
its own status, attempts and backoff. The token STRING is deliberately not
stored — only the FK — so a deactivated registration leaves no copy behind.

ON DELETE CASCADE on all three foreign keys. This is a queue of transient work,
not history: when a school, a user or a device registration goes away, its
undelivered jobs are meaningless. It also guarantees school cleanup can never
fail because of an outbox row — app/utils/school_cleanup.py needs no entry for
this table and is not modified by this phase.

FEATURE-FLAGGED OFF. INSTITUTE_ATTENDANCE_OUTBOX_ENABLED defaults to false, so
the application neither reads nor writes this table until it is switched on.
The code is therefore safe to deploy BEFORE this migration is applied.

Verified on an isolated local PostgreSQL instance: upgrade -> downgrade ->
upgrade. Not applied to production.

Revision ID: n0tb0x1a2b3c
Revises: a9t8n9d0s1c2
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'n0tb0x1a2b3c'
# a9t8n9d0s1c2 was verified COMMITTED (present in `git ls-files`) and the single
# head of the TRACKED migration graph at the time of writing — 78 tracked
# revisions, one head. The repository also carries an UNRELATED and still
# untracked sync-foundation revision (s1y2n3c0f0a1) whose down_revision points
# at c3d9f5b2a7e1; this migration does not merge, rebase, reference or depend on
# it. Apply with `flask db upgrade n0tb0x1a2b3c`, never `upgrade head`, while
# that second head exists in the working tree.
down_revision = 'a9t8n9d0s1c2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'notification_outbox',
        sa.Column('id',              sa.BigInteger(), nullable=False),
        sa.Column('school_id',       sa.Integer(), nullable=False),
        sa.Column('event_type',      sa.String(length=60), nullable=False),

        # Delivery target: the recipient and the exact registration.
        sa.Column('user_id',         sa.Integer(), nullable=False),
        sa.Column('device_token_id', sa.Integer(), nullable=False),

        # Immutable snapshot of what to send, rendered at enqueue time.
        sa.Column('title',     sa.String(length=200), nullable=False),
        sa.Column('body',      sa.Text(), nullable=False),
        sa.Column('data_json', sa.Text(), nullable=True),
        sa.Column('ntype',     sa.String(length=50), nullable=False,
                  server_default='attendance'),

        # Deduplicated enqueueing.
        sa.Column('dedup_key', sa.String(length=190), nullable=False),

        sa.Column('status',   sa.String(length=20), nullable=False,
                  server_default='pending'),
        sa.Column('attempts', sa.SmallInteger(), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),

        # Worker lease — who holds this row and since when.
        sa.Column('locked_by', sa.String(length=80), nullable=True),
        sa.Column('locked_at', sa.DateTime(), nullable=True),

        # Short, safe classification. Never a token, credential or payload.
        sa.Column('last_error', sa.String(length=200), nullable=True),

        sa.Column('created_at',   sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('processed_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),

        sa.PrimaryKeyConstraint('id', name='pk_notification_outbox'),

        # CASCADE: undelivered work for a removed school/user/registration is
        # meaningless, and this guarantees school cleanup is never blocked.
        sa.ForeignKeyConstraint(['school_id'], ['schools.id'],
                                name='fk_notification_outbox_school',
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'],
                                name='fk_notification_outbox_user',
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['device_token_id'], ['mobile_device_tokens.id'],
                                name='fk_notification_outbox_token',
                                ondelete='CASCADE'),

        sa.UniqueConstraint('dedup_key', name='uq_notification_outbox_dedup'),
        sa.CheckConstraint(
            "status IN ('pending','processing','retry','sent','dead','cancelled')",
            name='ck_notification_outbox_status'),
        sa.CheckConstraint('attempts >= 0',
                           name='ck_notification_outbox_attempts'),
    )

    # Claiming due work — the worker's hot path.
    op.create_index('ix_notification_outbox_due', 'notification_outbox',
                    ['status', 'next_attempt_at'])
    # Reclaiming leases abandoned by a crashed worker.
    op.create_index('ix_notification_outbox_lease', 'notification_outbox',
                    ['status', 'locked_at'])
    # Per-tenant operational inspection.
    op.create_index('ix_notification_outbox_school_status', 'notification_outbox',
                    ['school_id', 'status'])
    # Retention sweeps over terminal rows.
    op.create_index('ix_notification_outbox_completed', 'notification_outbox',
                    ['status', 'completed_at'])
    # Single-column lookups matching the ORM's index=True columns.
    op.create_index('ix_notification_outbox_school_id', 'notification_outbox',
                    ['school_id'])
    op.create_index('ix_notification_outbox_user_id', 'notification_outbox',
                    ['user_id'])
    op.create_index('ix_notification_outbox_device_token_id',
                    'notification_outbox', ['device_token_id'])


def downgrade():
    # Drops only what upgrade() created. Nothing else is touched, so a rollback
    # returns the schema to exactly its previous state.
    for name in ('ix_notification_outbox_device_token_id',
                 'ix_notification_outbox_user_id',
                 'ix_notification_outbox_school_id',
                 'ix_notification_outbox_completed',
                 'ix_notification_outbox_school_status',
                 'ix_notification_outbox_lease',
                 'ix_notification_outbox_due'):
        op.drop_index(name, table_name='notification_outbox')
    op.drop_table('notification_outbox')

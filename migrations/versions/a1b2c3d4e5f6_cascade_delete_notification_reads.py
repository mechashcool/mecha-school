"""cascade delete notification_reads and announcement_targets

Revision ID: a1b2c3d4e5f6
Revises: 5f53bad1ea5b
Create Date: 2026-04-28 14:00:00.000000

Adds ON DELETE CASCADE to:
  - notification_reads.notification_id  → notifications.id
  - announcement_targets.announcement_id → announcements.id

This ensures that deleting a Notification or Announcement automatically
removes linked read-receipts and target rows without raising IntegrityError.
"""
from alembic import op
import sqlalchemy as sa


revision = 'a1b2c3d4e5f6'
down_revision = '5f53bad1ea5b'
branch_labels = None
depends_on = None


def upgrade():
    # ── notification_reads ────────────────────────────────────────────────────
    with op.batch_alter_table('notification_reads', schema=None) as batch_op:
        # Drop the existing FK (PostgreSQL auto-names it)
        batch_op.drop_constraint(
            'notification_reads_notification_id_fkey',
            type_='foreignkey'
        )
        batch_op.create_foreign_key(
            'notification_reads_notification_id_fkey',
            'notifications',
            ['notification_id'], ['id'],
            ondelete='CASCADE'
        )

    # ── announcement_targets ──────────────────────────────────────────────────
    with op.batch_alter_table('announcement_targets', schema=None) as batch_op:
        batch_op.drop_constraint(
            'announcement_targets_announcement_id_fkey',
            type_='foreignkey'
        )
        batch_op.create_foreign_key(
            'announcement_targets_announcement_id_fkey',
            'announcements',
            ['announcement_id'], ['id'],
            ondelete='CASCADE'
        )


def downgrade():
    # ── announcement_targets ──────────────────────────────────────────────────
    with op.batch_alter_table('announcement_targets', schema=None) as batch_op:
        batch_op.drop_constraint(
            'announcement_targets_announcement_id_fkey',
            type_='foreignkey'
        )
        batch_op.create_foreign_key(
            'announcement_targets_announcement_id_fkey',
            'announcements',
            ['announcement_id'], ['id']
        )

    # ── notification_reads ────────────────────────────────────────────────────
    with op.batch_alter_table('notification_reads', schema=None) as batch_op:
        batch_op.drop_constraint(
            'notification_reads_notification_id_fkey',
            type_='foreignkey'
        )
        batch_op.create_foreign_key(
            'notification_reads_notification_id_fkey',
            'notifications',
            ['notification_id'], ['id']
        )

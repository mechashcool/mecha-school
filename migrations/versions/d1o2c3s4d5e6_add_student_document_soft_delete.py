"""add soft delete to student documents

Purely additive and default-safe. Scope is deliberately limited to the
``student_documents`` table — this is NOT a system-wide soft-delete pattern.

  * student_documents.deleted_at          — NULL = active (every existing row)
  * student_documents.deleted_by_user_id  — who deleted/replaced it (users.id)
  * student_documents.replaced_by_id      — replacement history: the superseded
                                            row points at the new active row

No existing row is read, backfilled or modified: all three columns are
nullable, so every pre-existing document is active the moment this runs. No
file path is touched and no stored object is deleted — a deleted or replaced
document keeps its row and its original ``file_path`` forever, which is what
guarantees the object stays referenced and the document stays restorable.

Both new foreign keys use ON DELETE SET NULL so they add no delete-ordering
dependency to student deletion (Student.documents cascade) or to the school
purge in app/utils/school_cleanup.py — in particular the self-reference on
replaced_by_id cannot block a bulk delete of a school's documents.

The index (student_id, deleted_at) serves the hot "active documents of this
student" query that every student page now issues.

Revision ID: d1o2c3s4d5e6
Revises: j1s2h3l4t5n6
Create Date: 2026-09-21
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd1o2c3s4d5e6'
down_revision = 'j1s2h3l4t5n6'
branch_labels = None
depends_on = None

_TABLE = 'student_documents'
_INDEX = 'ix_student_documents_student_active'
_FK_USER = 'fk_student_documents_deleted_by_user_id'
_FK_SELF = 'fk_student_documents_replaced_by_id'


def _existing(bind):
    """Column / index names already present, so a re-run is a no-op."""
    inspector = sa.inspect(bind)
    columns = {c['name'] for c in inspector.get_columns(_TABLE)}
    indexes = {i['name'] for i in inspector.get_indexes(_TABLE)}
    return columns, indexes


def upgrade():
    bind = op.get_bind()
    columns, indexes = _existing(bind)

    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        if 'deleted_at' not in columns:
            batch_op.add_column(sa.Column('deleted_at', sa.DateTime(),
                                          nullable=True))
        if 'deleted_by_user_id' not in columns:
            batch_op.add_column(sa.Column('deleted_by_user_id', sa.Integer(),
                                          nullable=True))
            batch_op.create_foreign_key(
                _FK_USER, 'users', ['deleted_by_user_id'], ['id'],
                ondelete='SET NULL',
            )
        if 'replaced_by_id' not in columns:
            batch_op.add_column(sa.Column('replaced_by_id', sa.Integer(),
                                          nullable=True))
            batch_op.create_foreign_key(
                _FK_SELF, _TABLE, ['replaced_by_id'], ['id'],
                ondelete='SET NULL',
            )
        if _INDEX not in indexes:
            batch_op.create_index(_INDEX, ['student_id', 'deleted_at'],
                                  unique=False)


def downgrade():
    bind = op.get_bind()
    columns, indexes = _existing(bind)

    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        if _INDEX in indexes:
            batch_op.drop_index(_INDEX)
        if 'replaced_by_id' in columns:
            batch_op.drop_constraint(_FK_SELF, type_='foreignkey')
            batch_op.drop_column('replaced_by_id')
        if 'deleted_by_user_id' in columns:
            batch_op.drop_constraint(_FK_USER, type_='foreignkey')
            batch_op.drop_column('deleted_by_user_id')
        if 'deleted_at' in columns:
            batch_op.drop_column('deleted_at')

"""supervisor_approval_audit_retention

Revision ID: 0039_supervisor_approval_audit_retention
Revises: 0038_supervisor_human_approvals
Create Date: 2026-10-26 22:15:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0039_supervisor_approval_audit_retention'
down_revision = '0038_supervisor_human_approvals'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0. Ensure alembic_version table supports revision identifiers > 32 characters
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("ALTER TABLE IF EXISTS alembic_version ALTER COLUMN version_num TYPE VARCHAR(255)"))

    # 1. Add approval_reference_id column as nullable first to allow backfilling
    with op.batch_alter_table('supervisor_approval_audit_events') as batch_op:
        batch_op.add_column(sa.Column('approval_reference_id', sa.Uuid(), nullable=True))

    # 2. Data migration: copy approval_id into approval_reference_id to preserve existing rows
    op.execute(
        sa.text(
            "UPDATE supervisor_approval_audit_events "
            "SET approval_reference_id = approval_id "
            "WHERE approval_reference_id IS NULL"
        )
    )

    # 3. Alter approval_reference_id to NOT NULL, alter approval_id to nullable,
    #    and replace CASCADE foreign key with SET NULL
    with op.batch_alter_table('supervisor_approval_audit_events') as batch_op:
        batch_op.alter_column('approval_reference_id', nullable=False, existing_type=sa.Uuid())
        batch_op.drop_constraint('fk_supervisor_audit_approval', type_='foreignkey')
        batch_op.alter_column('approval_id', nullable=True, existing_type=sa.Uuid())
        batch_op.create_foreign_key(
            'fk_supervisor_audit_approval',
            'supervisor_human_approvals',
            ['approval_id'],
            ['id'],
            ondelete='SET NULL',
        )
        batch_op.create_index(
            'ix_supervisor_audit_approval_ref_created',
            ['approval_reference_id', 'created_at'],
        )


def downgrade() -> None:
    # 1. For safe downgrade, remove records where approval_id is NULL (parent approval was deleted)
    op.execute(
        sa.text(
            "DELETE FROM supervisor_approval_audit_events WHERE approval_id IS NULL"
        )
    )

    # 2. Restore CASCADE foreign key, make approval_id NOT NULL, and drop approval_reference_id
    with op.batch_alter_table('supervisor_approval_audit_events') as batch_op:
        batch_op.drop_index('ix_supervisor_audit_approval_ref_created')
        batch_op.drop_constraint('fk_supervisor_audit_approval', type_='foreignkey')
        batch_op.alter_column('approval_id', nullable=False, existing_type=sa.Uuid())
        batch_op.create_foreign_key(
            'fk_supervisor_audit_approval',
            'supervisor_human_approvals',
            ['approval_id'],
            ['id'],
            ondelete='CASCADE',
        )
        batch_op.drop_column('approval_reference_id')

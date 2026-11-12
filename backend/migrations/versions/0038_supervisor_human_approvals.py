"""supervisor_human_approvals

Revision ID: 0038_supervisor_human_approvals
Revises: 0037_lead_title_dept
Create Date: 2026-10-24 16:15:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0038_supervisor_human_approvals'
down_revision = '0037_lead_title_dept'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. supervisor_human_approvals table
    # ------------------------------------------------------------------
    op.create_table(
        'supervisor_human_approvals',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('action_id', sa.String(length=180), nullable=False),
        sa.Column('lead_id', sa.Uuid(), nullable=False),
        sa.Column('opportunity_id', sa.Uuid(), nullable=False),
        sa.Column('contact_id', sa.Uuid(), nullable=True),
        sa.Column('recommended_action', sa.String(length=60), nullable=False),
        sa.Column('action_tier', sa.String(length=30), nullable=False),
        sa.Column('channel', sa.String(length=30), nullable=True),
        sa.Column('context_fingerprint', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='PENDING'),
        sa.Column('requested_by_user_id', sa.String(length=120), nullable=False),
        sa.Column('approved_by_user_id', sa.String(length=120), nullable=True),
        sa.Column('rejected_by_user_id', sa.String(length=120), nullable=True),
        sa.Column('revoked_by_user_id', sa.String(length=120), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('decision_reason', sa.String(length=500), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('execution_claimed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('execution_claimed_by', sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(['lead_id'], ['leads.id'], name='fk_supervisor_approvals_lead', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name='pk_supervisor_human_approvals'),
        sa.CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'REVOKED', 'EXPIRED')",
            name='ck_supervisor_approvals_status',
        ),
        sa.CheckConstraint(
            "action_tier IN ('INTERNAL_OPERATION', 'EXTERNAL_OUTREACH')",
            name='ck_supervisor_approvals_action_tier',
        ),
        sa.CheckConstraint(
            "channel IS NULL OR channel IN ('EMAIL', 'PHONE', 'DIRECT', 'MULTI_CHANNEL')",
            name='ck_supervisor_approvals_channel',
        ),
    )

    op.create_index(
        'uq_supervisor_approvals_active_action',
        'supervisor_human_approvals',
        ['action_id', 'context_fingerprint'],
        unique=True,
        postgresql_where=sa.text("status = 'APPROVED'"),
        sqlite_where=sa.text("status = 'APPROVED'"),
    )
    op.create_index(
        'ix_supervisor_approvals_lead_created',
        'supervisor_human_approvals',
        ['lead_id', 'created_at'],
    )
    op.create_index(
        'ix_supervisor_approvals_status_expiry',
        'supervisor_human_approvals',
        ['status', 'expires_at'],
    )
    op.create_index(
        'ix_supervisor_approvals_action_id',
        'supervisor_human_approvals',
        ['action_id'],
    )

    # ------------------------------------------------------------------
    # 2. supervisor_approval_audit_events table
    # ------------------------------------------------------------------
    op.create_table(
        'supervisor_approval_audit_events',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('approval_id', sa.Uuid(), nullable=False),
        sa.Column('action_id', sa.String(length=180), nullable=False),
        sa.Column('lead_id', sa.Uuid(), nullable=False),
        sa.Column('actor_id', sa.String(length=120), nullable=False),
        sa.Column('actor_role', sa.String(length=60), nullable=False),
        sa.Column('event_type', sa.String(length=40), nullable=False),
        sa.Column('from_status', sa.String(length=20), nullable=True),
        sa.Column('to_status', sa.String(length=20), nullable=False),
        sa.Column('context_fingerprint', sa.String(length=64), nullable=False),
        sa.Column('reason', sa.String(length=500), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['approval_id'],
            ['supervisor_human_approvals.id'],
            name='fk_supervisor_audit_approval',
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name='pk_supervisor_approval_audit_events'),
    )

    op.create_index(
        'ix_supervisor_audit_approval_created',
        'supervisor_approval_audit_events',
        ['approval_id', 'created_at'],
    )
    op.create_index(
        'ix_supervisor_audit_action_created',
        'supervisor_approval_audit_events',
        ['action_id', 'created_at'],
    )
    op.create_index(
        'ix_supervisor_audit_lead_created',
        'supervisor_approval_audit_events',
        ['lead_id', 'created_at'],
    )


def downgrade() -> None:
    # Drop audit events indexes and table
    op.drop_index('ix_supervisor_audit_lead_created', table_name='supervisor_approval_audit_events')
    op.drop_index('ix_supervisor_audit_action_created', table_name='supervisor_approval_audit_events')
    op.drop_index('ix_supervisor_audit_approval_created', table_name='supervisor_approval_audit_events')
    op.drop_table('supervisor_approval_audit_events')

    # Drop approvals indexes and table
    op.drop_index('ix_supervisor_approvals_action_id', table_name='supervisor_human_approvals')
    op.drop_index('ix_supervisor_approvals_status_expiry', table_name='supervisor_human_approvals')
    op.drop_index('ix_supervisor_approvals_lead_created', table_name='supervisor_human_approvals')
    op.drop_index('uq_supervisor_approvals_active_action', table_name='supervisor_human_approvals')
    op.drop_table('supervisor_human_approvals')

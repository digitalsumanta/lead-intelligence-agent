"""Persist human approvals and single-attempt email execution audits."""

from alembic import op
import sqlalchemy as sa


revision = "0024_ai_outreach_executions"
down_revision = "0023_ai_outreach_preparations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_outreach_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("preparation_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("approved_by_user_id", sa.String(120), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("recipient", sa.String(320), nullable=False),
        sa.Column("context_fingerprint", sa.String(64), nullable=False),
        sa.Column("preparation_version", sa.String(100), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("approval_version", sa.String(100), nullable=False),
        sa.Column("policy_evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("policy_decision", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), server_default="APPROVED", nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("channel = 'EMAIL'", name="ck_ai_outreach_approvals_email_only"),
        sa.CheckConstraint("status IN ('APPROVED','REJECTED','STALE','CANCELLED')", name="ck_ai_outreach_approvals_status"),
        sa.CheckConstraint("policy_decision IN ('ALLOW','REQUIRE_HUMAN_APPROVAL')", name="ck_ai_outreach_approvals_policy_decision"),
        sa.ForeignKeyConstraint(["preparation_id", "lead_id"], ["ai_outreach_preparations.id", "ai_outreach_preparations.lead_id"], name="fk_ai_outreach_approvals_preparation_lead", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["policy_evaluation_id"], ["agent_policy_evaluations.id"], name="fk_ai_outreach_approvals_policy"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_outreach_approvals_lead_created", "ai_outreach_approvals", ["lead_id", "created_at"])
    op.create_index("uq_ai_outreach_approvals_id_lead", "ai_outreach_approvals", ["id", "lead_id"], unique=True)
    op.create_index(
        "uq_ai_outreach_approvals_active_preparation", "ai_outreach_approvals", ["preparation_id"], unique=True,
        postgresql_where=sa.text("status = 'APPROVED'"), sqlite_where=sa.text("status = 'APPROVED'"),
    )

    op.create_table(
        "ai_outreach_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("preparation_id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("recipient", sa.String(320), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("context_fingerprint", sa.String(64), nullable=False),
        sa.Column("execution_version", sa.String(100), nullable=False),
        sa.Column("policy_evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("policy_decision", sa.String(30), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("provider_message_id", sa.String(512), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("failure_code", sa.String(80), nullable=True),
        sa.Column("failure_reason", sa.String(500), nullable=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("channel = 'EMAIL'", name="ck_ai_outreach_executions_email_only"),
        sa.CheckConstraint("status IN ('EXECUTING','SENT','FAILED','UNCERTAIN','STALE','BLOCKED')", name="ck_ai_outreach_executions_status"),
        sa.CheckConstraint("policy_decision IN ('ALLOW','DENY','REQUIRE_HUMAN_APPROVAL')", name="ck_ai_outreach_executions_policy_decision"),
        sa.ForeignKeyConstraint(["approval_id", "lead_id"], ["ai_outreach_approvals.id", "ai_outreach_approvals.lead_id"], name="fk_ai_outreach_executions_approval_lead", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["preparation_id", "lead_id"], ["ai_outreach_preparations.id", "ai_outreach_preparations.lead_id"], name="fk_ai_outreach_executions_preparation_lead", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["policy_evaluation_id"], ["agent_policy_evaluations.id"], name="fk_ai_outreach_executions_policy"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("approval_id", name="uq_ai_outreach_executions_approval"),
        sa.UniqueConstraint("idempotency_key", name="uq_ai_outreach_executions_idempotency"),
    )
    op.create_index("ix_ai_outreach_executions_lead_created", "ai_outreach_executions", ["lead_id", "created_at"])
    op.create_index("uq_ai_outreach_executions_id_lead", "ai_outreach_executions", ["id", "lead_id"], unique=True)


def downgrade():
    op.drop_index("uq_ai_outreach_executions_id_lead", table_name="ai_outreach_executions")
    op.drop_index("ix_ai_outreach_executions_lead_created", table_name="ai_outreach_executions")
    op.drop_table("ai_outreach_executions")
    op.drop_index("uq_ai_outreach_approvals_active_preparation", table_name="ai_outreach_approvals")
    op.drop_index("uq_ai_outreach_approvals_id_lead", table_name="ai_outreach_approvals")
    op.drop_index("ix_ai_outreach_approvals_lead_created", table_name="ai_outreach_approvals")
    op.drop_table("ai_outreach_approvals")

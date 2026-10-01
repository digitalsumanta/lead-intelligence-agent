"""Persist policy-gated, non-sending AI outreach preparations."""

from alembic import op
import sqlalchemy as sa


revision = "0023_ai_outreach_preparations"
down_revision = "0022_autonomous_task_executions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_outreach_preparations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("source_action_id", sa.String(180), nullable=False),
        sa.Column("action_type", sa.String(60), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("purpose", sa.String(80), nullable=False),
        sa.Column("subject", sa.String(200), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("call_script", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("review_required", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("policy_decision", sa.String(30), nullable=True),
        sa.Column("policy_evaluation_id", sa.Uuid(), nullable=True),
        sa.Column("policy_version", sa.String(80), nullable=False),
        sa.Column("context_version", sa.String(100), nullable=False),
        sa.Column("context_fingerprint", sa.String(64), nullable=False),
        sa.Column("preparation_version", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(80), nullable=True),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("opportunity_refs", sa.JSON(), nullable=False),
        sa.Column("service_refs", sa.JSON(), nullable=False),
        sa.Column("validation_result", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source_type IN ('AI_SALES_ACTION_PLAN','AI_SALES_SUPERVISOR')", name="ck_ai_outreach_preparation_source"),
        sa.CheckConstraint("action_type IN ('FOLLOW_UP','CALL_PROSPECT','REVIEW_SEO_OPPORTUNITY','REVIEW_OUTREACH','PREPARE_PROPOSAL','INVESTIGATE_MISSING_INFORMATION','REVIEW_COMMERCIAL_CONTEXT')", name="ck_ai_outreach_preparation_action"),
        sa.CheckConstraint("channel IN ('EMAIL','WHATSAPP','CALL_SCRIPT')", name="ck_ai_outreach_preparation_channel"),
        sa.CheckConstraint("status IN ('READY_FOR_HUMAN_REVIEW','REQUIRES_HUMAN_APPROVAL','REJECTED','STALE_CONTEXT','INVALID_REQUEST','POLICY_DENIED')", name="ck_ai_outreach_preparation_status"),
        sa.CheckConstraint("policy_decision IS NULL OR policy_decision IN ('ALLOW','DENY','REQUIRE_HUMAN_APPROVAL')", name="ck_ai_outreach_preparation_policy"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], name="fk_ai_outreach_preparations_lead", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["policy_evaluation_id"], ["agent_policy_evaluations.id"], name="fk_ai_outreach_preparations_policy"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_outreach_preparations_lead_created", "ai_outreach_preparations", ["lead_id", "created_at"])
    op.create_index("ix_ai_outreach_preparations_source", "ai_outreach_preparations", ["source_type", "source_id"])
    op.create_index("uq_ai_outreach_preparations_id_lead", "ai_outreach_preparations", ["id", "lead_id"], unique=True)
    op.create_index(
        "uq_ai_outreach_preparations_ready_key", "ai_outreach_preparations", ["idempotency_key"], unique=True,
        postgresql_where=sa.text("status IN ('READY_FOR_HUMAN_REVIEW','REQUIRES_HUMAN_APPROVAL')"),
        sqlite_where=sa.text("status IN ('READY_FOR_HUMAN_REVIEW','REQUIRES_HUMAN_APPROVAL')"),
    )


def downgrade():
    op.drop_index("uq_ai_outreach_preparations_ready_key", table_name="ai_outreach_preparations")
    op.drop_index("uq_ai_outreach_preparations_id_lead", table_name="ai_outreach_preparations")
    op.drop_index("ix_ai_outreach_preparations_source", table_name="ai_outreach_preparations")
    op.drop_index("ix_ai_outreach_preparations_lead_created", table_name="ai_outreach_preparations")
    op.drop_table("ai_outreach_preparations")

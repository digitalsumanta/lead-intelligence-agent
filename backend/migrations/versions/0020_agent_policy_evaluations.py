"""Persist deterministic agent policy evaluations and channel opt-outs."""

from alembic import op
import sqlalchemy as sa


revision = "0020_agent_policy_evaluations"
down_revision = "0019_ai_proposal_intelligence"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("leads", sa.Column("email_opted_out", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("leads", sa.Column("whatsapp_opted_out", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("leads", sa.Column("phone_opted_out", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.create_table(
        "agent_policy_evaluations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("action_type", sa.String(length=60), nullable=False),
        sa.Column("requested_channel", sa.String(length=20), nullable=True),
        sa.Column("decision", sa.String(length=30), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("opportunity_refs", sa.JSON(), nullable=False),
        sa.Column("service_refs", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.String(length=80), nullable=False),
        sa.Column("autonomy_level", sa.String(length=30), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("requested_by", sa.String(length=80), nullable=False),
        sa.Column("rule_results", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action_type IN ('FOLLOW_UP','CALL_PROSPECT','REVIEW_SEO_OPPORTUNITY','REVIEW_OUTREACH','PREPARE_PROPOSAL','INVESTIGATE_MISSING_INFORMATION','REVIEW_COMMERCIAL_CONTEXT')", name="ck_agent_policy_action"),
        sa.CheckConstraint("decision IN ('ALLOW','DENY','REQUIRE_HUMAN_APPROVAL')", name="ck_agent_policy_decision"),
        sa.CheckConstraint("autonomy_level IN ('HUMAN_ONLY','ASSISTED','SUPERVISED','AUTONOMOUS')", name="ck_agent_policy_autonomy"),
        sa.CheckConstraint("requested_channel IS NULL OR requested_channel IN ('EMAIL','WHATSAPP','PHONE')", name="ck_agent_policy_channel"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], name="fk_agent_policy_evaluations_lead", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_policy_evaluations_lead_created", "agent_policy_evaluations", ["lead_id", "created_at"])
    op.create_index("ix_agent_policy_evaluations_request_fingerprint", "agent_policy_evaluations", ["request_fingerprint"])


def downgrade():
    op.drop_index("ix_agent_policy_evaluations_request_fingerprint", table_name="agent_policy_evaluations")
    op.drop_index("ix_agent_policy_evaluations_lead_created", table_name="agent_policy_evaluations")
    op.drop_table("agent_policy_evaluations")
    op.drop_column("leads", "phone_opted_out")
    op.drop_column("leads", "whatsapp_opted_out")
    op.drop_column("leads", "email_opted_out")

"""Persist append-only AI Sales Supervisor run history."""

from alembic import op
import sqlalchemy as sa


revision = "0021_ai_sales_supervisor"
down_revision = "0020_agent_policy_evaluations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_sales_supervisor_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("artifact_version", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("context_version", sa.String(length=100), nullable=False),
        sa.Column("context_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("usage_json", sa.JSON(), nullable=False),
        sa.Column("scope_metadata", sa.JSON(), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("scope IN ('PIPELINE','LEAD')", name="ck_ai_sales_supervisor_scope"),
        sa.CheckConstraint("(scope = 'PIPELINE' AND lead_id IS NULL) OR (scope = 'LEAD' AND lead_id IS NOT NULL)", name="ck_ai_sales_supervisor_scope_lead"),
        sa.CheckConstraint("status IN ('COMPLETED','FAILED')", name="ck_ai_sales_supervisor_status"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], name="fk_ai_sales_supervisor_lead", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_sales_supervisor_scope_created", "ai_sales_supervisor_runs", ["scope", "created_at"])
    op.create_index("ix_ai_sales_supervisor_lead_created", "ai_sales_supervisor_runs", ["lead_id", "created_at"])
    op.create_index("ix_ai_sales_supervisor_fingerprint", "ai_sales_supervisor_runs", ["context_fingerprint"])


def downgrade():
    op.drop_index("ix_ai_sales_supervisor_fingerprint", table_name="ai_sales_supervisor_runs")
    op.drop_index("ix_ai_sales_supervisor_lead_created", table_name="ai_sales_supervisor_runs")
    op.drop_index("ix_ai_sales_supervisor_scope_created", table_name="ai_sales_supervisor_runs")
    op.drop_table("ai_sales_supervisor_runs")

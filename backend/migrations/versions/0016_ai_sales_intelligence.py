"""Persist append-only, versioned AI sales intelligence artifacts."""

from alembic import op
import sqlalchemy as sa


revision = "0016_ai_sales_intelligence"
down_revision = "0015_seo_intelligence"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_sales_intelligence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("context_version", sa.String(length=100), nullable=False),
        sa.Column("contract_version", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("generated", sa.JSON(), nullable=True),
        sa.Column("evidence_catalog", sa.JSON(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("usage_metadata", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('validated','failed')", name="ck_ai_sales_intelligence_status"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], name="fk_ai_sales_intelligence_lead", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_sales_intelligence_lead_created", "ai_sales_intelligence", ["lead_id", "created_at"])
    op.create_index("ix_ai_sales_intelligence_lead_fingerprint", "ai_sales_intelligence", ["lead_id", "source_fingerprint"])


def downgrade():
    op.drop_index("ix_ai_sales_intelligence_lead_fingerprint", table_name="ai_sales_intelligence")
    op.drop_index("ix_ai_sales_intelligence_lead_created", table_name="ai_sales_intelligence")
    op.drop_table("ai_sales_intelligence")

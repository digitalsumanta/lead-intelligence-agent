"""Persist grounded outreach drafts and human review state."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010_outreach_preparation"
down_revision = "0009_sales_workflow"
branch_labels = None
depends_on = None

outreach_status = postgresql.ENUM(
    "DRAFT", "READY_FOR_REVIEW", "APPROVED", "REJECTED", "STALE",
    name="outreachstatus", create_type=False,
)


def upgrade():
    bind = op.get_bind()
    outreach_status.create(bind, checkfirst=False)
    op.create_table(
        "outreach_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("enrichment_run_id", sa.Uuid(), nullable=False),
        sa.Column("status", outreach_status, nullable=False),
        sa.Column("sales_context_version", sa.String(100), nullable=False),
        sa.Column("sales_context", sa.JSON(), nullable=False),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column("contract_version", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("content", sa.JSON(), nullable=True),
        sa.Column("validation_result", sa.JSON(), nullable=False),
        sa.Column("generation_error", sa.String(500), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.String(120), nullable=True),
        sa.Column("rejection_reason", sa.String(1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enrichment_run_id", "lead_id"], ["enrichment_runs.id", "enrichment_runs.lead_id"],
            name="fk_outreach_artifacts_run_lead", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outreach_artifacts_lead_created", "outreach_artifacts", ["lead_id", "created_at"])
    op.create_index("ix_outreach_artifacts_enrichment_run", "outreach_artifacts", ["enrichment_run_id"])
    op.create_index("ix_outreach_artifacts_status", "outreach_artifacts", ["status"])


def downgrade():
    op.drop_index("ix_outreach_artifacts_status", table_name="outreach_artifacts")
    op.drop_index("ix_outreach_artifacts_enrichment_run", table_name="outreach_artifacts")
    op.drop_index("ix_outreach_artifacts_lead_created", table_name="outreach_artifacts")
    op.drop_table("outreach_artifacts")
    outreach_status.drop(op.get_bind(), checkfirst=False)

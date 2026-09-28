"""Persist validated, run-scoped AI personalization."""
from alembic import op
import sqlalchemy as sa

revision = "0008_personalization"
down_revision = "0007_deterministic_intelligence"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("uq_enrichment_runs_id_lead", "enrichment_runs", ["id", "lead_id"], unique=True)
    op.create_table(
        "personalizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("enrichment_run_id", sa.Uuid(), nullable=False),
        sa.Column("contract_version", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("validation_result", sa.JSON(), nullable=False),
        sa.Column("generated", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enrichment_run_id", "lead_id"], ["enrichment_runs.id", "enrichment_runs.lead_id"],
            name="fk_personalizations_run_lead", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_personalizations_run_contract", "personalizations", ["enrichment_run_id", "contract_version"], unique=True)
    op.create_table(
        "personalization_evidence_links",
        sa.Column("personalization_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["personalization_id"], ["personalizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["evidence_id"], ["evidence.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("personalization_id", "evidence_id"),
    )
    op.create_index("ix_personalization_evidence_evidence", "personalization_evidence_links", ["evidence_id"])


def downgrade():
    op.drop_index("ix_personalization_evidence_evidence", table_name="personalization_evidence_links")
    op.drop_table("personalization_evidence_links")
    op.drop_index("uq_personalizations_run_contract", table_name="personalizations")
    op.drop_table("personalizations")
    op.drop_index("uq_enrichment_runs_id_lead", table_name="enrichment_runs")

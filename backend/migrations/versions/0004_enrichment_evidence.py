"""Persist enrichment runs and source evidence with historical provenance."""
from alembic import op
import sqlalchemy as sa


revision = "0004_enrichment_evidence"
down_revision = "0003_worker_heartbeat"
branch_labels = None
depends_on = None


uid = sa.Uuid(as_uuid=True)
ts = lambda: sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "enrichment_runs",
        sa.Column("id", uid, primary_key=True),
        sa.Column("lead_id", uid, sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("processing_job_id", uid, sa.ForeignKey("lead_processing_jobs.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("requested_stages", sa.JSON(), nullable=False),
        sa.Column("completed_stages", sa.JSON(), nullable=False),
        sa.Column("started_at", ts()),
        sa.Column("completed_at", ts()),
        sa.Column("pipeline_version", sa.String(100), nullable=False),
        sa.Column("failure_summary", sa.JSON()),
        sa.Column("created_at", ts(), nullable=False),
        sa.Column("updated_at", ts(), nullable=False),
    )
    op.create_index("ix_enrichment_runs_lead_created", "enrichment_runs", ["lead_id", "created_at"])
    op.create_index("ix_enrichment_runs_processing_job", "enrichment_runs", ["processing_job_id"])

    op.create_table(
        "evidence",
        sa.Column("id", uid, primary_key=True),
        sa.Column("lead_id", uid, sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("enrichment_run_id", uid, sa.ForeignKey("enrichment_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("evidence_type", sa.String(160), nullable=False),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("provider_name", sa.String(100), nullable=False),
        sa.Column("provider_kind", sa.String(40), nullable=False),
        sa.Column("provider_implementation", sa.String(100)),
        sa.Column("source_url", sa.String(2048)),
        sa.Column("external_provider_reference", sa.String(512)),
        sa.Column("observed_value", sa.JSON(), nullable=False),
        sa.Column("observed_at", ts(), nullable=False),
        sa.Column("fetched_at", ts(), nullable=False),
        sa.Column("expires_at", ts()),
        sa.Column("confidence", sa.Float()),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", ts(), nullable=False),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_evidence_confidence_range",
        ),
    )
    op.create_index("ix_evidence_lead", "evidence", ["lead_id"])
    op.create_index("ix_evidence_enrichment_run", "evidence", ["enrichment_run_id"])
    op.create_index("ix_evidence_type", "evidence", ["evidence_type"])
    op.create_index("ix_evidence_provider_source", "evidence", ["provider_kind", "provider_name", "source_type"])
    op.create_index("ix_evidence_observed_at", "evidence", ["observed_at"])
    op.create_index("ix_evidence_fetched_at", "evidence", ["fetched_at"])
    op.create_index("ix_evidence_provider_reference", "evidence", ["provider_name", "external_provider_reference"])


def downgrade():
    op.drop_index("ix_evidence_provider_reference", table_name="evidence")
    op.drop_index("ix_evidence_fetched_at", table_name="evidence")
    op.drop_index("ix_evidence_observed_at", table_name="evidence")
    op.drop_index("ix_evidence_provider_source", table_name="evidence")
    op.drop_index("ix_evidence_type", table_name="evidence")
    op.drop_index("ix_evidence_enrichment_run", table_name="evidence")
    op.drop_index("ix_evidence_lead", table_name="evidence")
    op.drop_table("evidence")
    op.drop_index("ix_enrichment_runs_processing_job", table_name="enrichment_runs")
    op.drop_index("ix_enrichment_runs_lead_created", table_name="enrichment_runs")
    op.drop_table("enrichment_runs")

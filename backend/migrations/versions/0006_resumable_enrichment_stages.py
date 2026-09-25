"""Persist resumable enrichment stage leases and evidence idempotency."""
from alembic import op
import sqlalchemy as sa


revision = "0006_resumable_enrichment_stages"
down_revision = "0005_provider_execution_controls"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("enrichment_runs", sa.Column("stage_states", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.add_column("enrichment_runs", sa.Column("active_stage", sa.String(40)))
    op.add_column("enrichment_runs", sa.Column("stage_lease_token", sa.String(36)))
    op.add_column("enrichment_runs", sa.Column("stage_lease_expires_at", sa.DateTime(timezone=True)))
    op.create_index(
        "uq_enrichment_run_active_job",
        "enrichment_runs",
        ["processing_job_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
        sqlite_where=sa.text("status IN ('queued','running')"),
    )
    op.create_index("uq_evidence_run_content_hash", "evidence", ["enrichment_run_id", "content_hash"], unique=True)


def downgrade():
    op.drop_index("uq_evidence_run_content_hash", table_name="evidence")
    op.drop_index("uq_enrichment_run_active_job", table_name="enrichment_runs")
    op.drop_column("enrichment_runs", "stage_lease_expires_at")
    op.drop_column("enrichment_runs", "stage_lease_token")
    op.drop_column("enrichment_runs", "active_stage")
    op.drop_column("enrichment_runs", "stage_states")

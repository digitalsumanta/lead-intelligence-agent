"""Add independent, versioned SEO audit runs and normalized findings."""

from alembic import op
import sqlalchemy as sa


revision = "0015_seo_intelligence"
down_revision = "0014_outreach_outcomes"
branch_labels = None
depends_on = None


def upgrade():
    uid = sa.Uuid(as_uuid=True)
    timestamp = sa.DateTime(timezone=True)

    op.create_table(
        "seo_audit_runs",
        sa.Column("id", uid, nullable=False),
        sa.Column("lead_id", uid, nullable=False),
        sa.Column("status", sa.String(20), server_default="QUEUED", nullable=False),
        sa.Column("audit_version", sa.String(100), nullable=False),
        sa.Column("root_url", sa.String(2048), nullable=False),
        sa.Column("normalized_root_url", sa.String(2048), nullable=False),
        sa.Column("pages_crawled", sa.Integer(), server_default="0", nullable=False),
        sa.Column("pages_discovered", sa.Integer(), server_default="0", nullable=False),
        sa.Column("findings_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(120)),
        sa.Column("error_message", sa.String(1000)),
        sa.Column("started_at", timestamp),
        sa.Column("completed_at", timestamp),
        sa.Column("lease_expires_at", timestamp),
        sa.Column("created_at", timestamp, nullable=False),
        sa.CheckConstraint("status IN ('QUEUED','RUNNING','COMPLETED','FAILED')", name="ck_seo_audit_runs_status"),
        sa.CheckConstraint("pages_crawled >= 0 AND pages_discovered >= 0 AND findings_count >= 0", name="ck_seo_audit_runs_counts_nonnegative"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_seo_audit_runs_lead_created", "seo_audit_runs", ["lead_id", "created_at"])
    op.create_index("ix_seo_audit_runs_status_created", "seo_audit_runs", ["status", "created_at"])
    op.create_index("uq_seo_audit_runs_id_lead", "seo_audit_runs", ["id", "lead_id"], unique=True)
    op.create_index(
        "uq_seo_audit_runs_active_lead", "seo_audit_runs", ["lead_id"], unique=True,
        postgresql_where=sa.text("status IN ('QUEUED','RUNNING')"),
        sqlite_where=sa.text("status IN ('QUEUED','RUNNING')"),
    )

    op.create_table(
        "seo_observations",
        sa.Column("id", uid, nullable=False),
        sa.Column("seo_run_id", uid, nullable=False),
        sa.Column("observation_key", sa.String(64), nullable=False),
        sa.Column("observation_type", sa.String(120), nullable=False),
        sa.Column("page_url", sa.String(2048)),
        sa.Column("observed_value", sa.JSON(), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.ForeignKeyConstraint(["seo_run_id"], ["seo_audit_runs.id"], name="fk_seo_observations_run", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_seo_observations_run_key", "seo_observations", ["seo_run_id", "observation_key"], unique=True)
    op.create_index("uq_seo_observations_id_run", "seo_observations", ["id", "seo_run_id"], unique=True)
    op.create_index("ix_seo_observations_run_type", "seo_observations", ["seo_run_id", "observation_type"])

    op.create_table(
        "seo_findings",
        sa.Column("id", uid, nullable=False),
        sa.Column("seo_run_id", uid, nullable=False),
        sa.Column("observation_id", uid),
        sa.Column("page_url", sa.String(2048)),
        sa.Column("finding_code", sa.String(120), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("title", sa.String(250), nullable=False),
        sa.Column("description", sa.String(1000), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.CheckConstraint("severity IN ('INFO','LOW','MEDIUM','HIGH')", name="ck_seo_findings_severity"),
        sa.CheckConstraint("category IN ('TECHNICAL','ON_PAGE','CRAWLABILITY','INDEXABILITY','PERFORMANCE','STRUCTURED_DATA','LINKS','IMAGES')", name="ck_seo_findings_category"),
        sa.ForeignKeyConstraint(["seo_run_id"], ["seo_audit_runs.id"], name="fk_seo_findings_run", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["observation_id", "seo_run_id"], ["seo_observations.id", "seo_observations.seo_run_id"],
            name="fk_seo_findings_observation_run", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_seo_findings_run_severity", "seo_findings", ["seo_run_id", "severity"])
    op.create_index("ix_seo_findings_run_category", "seo_findings", ["seo_run_id", "category"])
    op.create_index("ix_seo_findings_run_code", "seo_findings", ["seo_run_id", "finding_code"])


def downgrade():
    op.drop_index("ix_seo_findings_run_code", table_name="seo_findings")
    op.drop_index("ix_seo_findings_run_category", table_name="seo_findings")
    op.drop_index("ix_seo_findings_run_severity", table_name="seo_findings")
    op.drop_table("seo_findings")
    op.drop_index("ix_seo_observations_run_type", table_name="seo_observations")
    op.drop_index("uq_seo_observations_id_run", table_name="seo_observations")
    op.drop_index("uq_seo_observations_run_key", table_name="seo_observations")
    op.drop_table("seo_observations")
    op.drop_index("uq_seo_audit_runs_active_lead", table_name="seo_audit_runs")
    op.drop_index("uq_seo_audit_runs_id_lead", table_name="seo_audit_runs")
    op.drop_index("ix_seo_audit_runs_status_created", table_name="seo_audit_runs")
    op.drop_index("ix_seo_audit_runs_lead_created", table_name="seo_audit_runs")
    op.drop_table("seo_audit_runs")

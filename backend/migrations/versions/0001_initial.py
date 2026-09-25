"""Create the initial, explicit lead intelligence schema."""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

lead_status = sa.Enum("IMPORTED", "VALIDATING", "QUEUED", "PROCESSING", "COMPLETED", "FAILED", "RETRYING", name="leadstatus")
job_status = sa.Enum("QUEUED", "PROCESSING", "COMPLETED", "FAILED", "RETRYING", name="jobstatus")
uid = sa.Uuid(as_uuid=True)
ts = lambda: sa.DateTime(timezone=True)

def upgrade():
    op.create_table("leads",
        sa.Column("id", uid, primary_key=True), sa.Column("company_name", sa.String(250), nullable=False),
        sa.Column("website", sa.String(1000)), sa.Column("phone", sa.String(80)), sa.Column("email", sa.String(320)),
        sa.Column("address", sa.String(500)), sa.Column("city", sa.String(120)), sa.Column("state", sa.String(120)),
        sa.Column("country", sa.String(120)), sa.Column("industry", sa.String(160)), sa.Column("status", lead_status, nullable=False),
        sa.Column("lead_score", sa.Integer()), sa.Column("score_factors", sa.JSON()), sa.Column("ai_summary", sa.Text()),
        sa.Column("pain_points", sa.JSON()), sa.Column("keyword_opportunities", sa.JSON()), sa.Column("competitors", sa.JSON()),
        sa.Column("recommended_services", sa.JSON()), sa.Column("decision_maker", sa.String(250)), sa.Column("linkedin_url", sa.String(1000)),
        sa.Column("website_analysis", sa.JSON()), sa.Column("ai_analysis", sa.JSON()), sa.Column("sales_opening", sa.Text()),
        sa.Column("audit_video_script", sa.Text()), sa.Column("proposed_package", sa.Text()), sa.Column("confidence_score", sa.Float()),
        sa.Column("created_at", ts(), nullable=False), sa.Column("updated_at", ts(), nullable=False))
    op.create_index("ix_leads_email", "leads", ["email"])
    op.create_index("ix_leads_company_city", "leads", ["company_name", "city"])
    op.create_index("ix_leads_status_created", "leads", ["status", "created_at"])
    op.create_table("lead_import_jobs", sa.Column("id", uid, primary_key=True), sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("total_rows", sa.Integer(), nullable=False), sa.Column("accepted_rows", sa.Integer(), nullable=False),
        sa.Column("duplicate_rows", sa.Integer(), nullable=False), sa.Column("invalid_rows", sa.Integer(), nullable=False),
        sa.Column("invalid_details", sa.JSON(), nullable=False), sa.Column("created_at", ts(), nullable=False))
    op.create_table("lead_batches", sa.Column("id", uid, primary_key=True), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("requested_count", sa.Integer(), nullable=False), sa.Column("created_at", ts(), nullable=False))
    op.create_table("lead_processing_jobs", sa.Column("id", uid, primary_key=True),
        sa.Column("lead_id", uid, sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("batch_id", uid, sa.ForeignKey("lead_batches.id")), sa.Column("status", job_status, nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False), sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text()), sa.Column("started_at", ts()), sa.Column("completed_at", ts()),
        sa.Column("next_retry_at", ts()), sa.Column("checkpoint", sa.String(40), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False), sa.Column("model", sa.String(160), nullable=False),
        sa.Column("created_at", ts(), nullable=False), sa.Column("updated_at", ts(), nullable=False))
    op.create_index("ix_jobs_status_retry", "lead_processing_jobs", ["status", "next_retry_at"])
    op.create_index("ix_jobs_lead_created", "lead_processing_jobs", ["lead_id", "created_at"])
    op.create_table("lead_events", sa.Column("id", uid, primary_key=True),
        sa.Column("lead_id", uid, sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", uid, sa.ForeignKey("lead_processing_jobs.id", ondelete="SET NULL")),
        sa.Column("event_type", sa.String(80), nullable=False), sa.Column("details", sa.JSON(), nullable=False), sa.Column("created_at", ts(), nullable=False))
    op.create_table("provider_usage", sa.Column("id", uid, primary_key=True), sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("model", sa.String(160), nullable=False), sa.Column("job_id", uid, sa.ForeignKey("lead_processing_jobs.id")),
        sa.Column("success", sa.Boolean(), nullable=False), sa.Column("duration_ms", sa.Integer()), sa.Column("created_at", ts(), nullable=False))
    op.create_table("api_keys", sa.Column("id", uid, primary_key=True), sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("key", sa.Text(), nullable=False), sa.Column("status", sa.String(30), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False), sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("last_used_at", ts()), sa.Column("last_error", sa.Text()), sa.Column("cooldown_until", ts()))
    op.create_table("agent_runs", sa.Column("id", uid, primary_key=True),
        sa.Column("job_id", uid, sa.ForeignKey("lead_processing_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False), sa.Column("started_at", ts(), nullable=False),
        sa.Column("completed_at", ts()), sa.Column("metadata_json", sa.JSON(), nullable=False))

def downgrade():
    for table in ("agent_runs", "api_keys", "provider_usage", "lead_events", "lead_processing_jobs", "lead_batches", "lead_import_jobs"):
        op.drop_table(table)
    op.drop_index("ix_leads_status_created", table_name="leads")
    op.drop_index("ix_leads_company_city", table_name="leads")
    op.drop_index("ix_leads_email", table_name="leads")
    op.drop_table("leads")

"""Prevent multiple active processing jobs for one lead."""
from alembic import op
import sqlalchemy as sa

revision = "0002_active_job_guard"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

def upgrade():
    op.create_index("uq_active_job_lead", "lead_processing_jobs", ["lead_id"], unique=True,
                    postgresql_where=sa.text("status IN ('QUEUED','PROCESSING','RETRYING')"),
                    sqlite_where=sa.text("status IN ('QUEUED','PROCESSING','RETRYING')"))

def downgrade():
    op.drop_index("uq_active_job_lead", table_name="lead_processing_jobs")

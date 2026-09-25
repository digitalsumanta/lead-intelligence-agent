"""Track worker liveness for readiness checks."""
from alembic import op
import sqlalchemy as sa

revision = "0003_worker_heartbeat"
down_revision = "0002_active_job_guard"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("worker_heartbeats",
        sa.Column("worker_id", sa.String(200), primary_key=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False))

def downgrade():
    op.drop_table("worker_heartbeats")

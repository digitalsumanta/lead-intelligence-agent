"""Persist normalized status reported by the configured email provider."""

from alembic import op
import sqlalchemy as sa


revision = "0025_email_provider_status"
down_revision = "0024_ai_outreach_executions"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "ai_outreach_executions",
        sa.Column("provider_status", sa.String(80), nullable=True),
    )


def downgrade():
    op.drop_column("ai_outreach_executions", "provider_status")

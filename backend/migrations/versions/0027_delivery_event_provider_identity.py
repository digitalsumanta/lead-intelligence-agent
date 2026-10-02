"""Add provider identity and cross-execution event idempotency."""

from alembic import op
import sqlalchemy as sa


revision = "0027_delivery_provider_identity"
down_revision = "0026_email_delivery_events"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("email_delivery_events", sa.Column("provider", sa.String(80), nullable=True))
    op.execute(
        "UPDATE email_delivery_events AS event "
        "SET provider = lower(execution.provider) "
        "FROM ai_outreach_executions AS execution "
        "WHERE event.execution_id = execution.id"
    )
    op.alter_column("email_delivery_events", "provider", existing_type=sa.String(80), nullable=False)
    op.create_index(
        "uq_email_delivery_events_provider_event",
        "email_delivery_events", ["provider", "provider_event_id"], unique=True,
        postgresql_where=sa.text("provider IS NOT NULL AND provider_event_id IS NOT NULL"),
        sqlite_where=sa.text("provider IS NOT NULL AND provider_event_id IS NOT NULL"),
    )


def downgrade():
    op.drop_index("uq_email_delivery_events_provider_event", table_name="email_delivery_events")
    op.drop_column("email_delivery_events", "provider")

"""Add append-only, provider-neutral email delivery event history."""

from alembic import op
import sqlalchemy as sa


revision = "0026_email_delivery_events"
down_revision = "0025_email_provider_status"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "email_delivery_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column("recipient_email", sa.String(320), nullable=False),
        sa.Column("provider_message_id", sa.String(512), nullable=True),
        sa.Column("provider_event_id", sa.String(512), nullable=True),
        sa.Column("provider_status", sa.String(80), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('SENT','DELIVERED','BOUNCED','FAILED','COMPLAINED','UNSUBSCRIBED')",
            name="ck_email_delivery_events_type",
        ),
        sa.CheckConstraint("source IN ('PROVIDER','INTERNAL')", name="ck_email_delivery_events_source"),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["ai_outreach_executions.id"],
            name="fk_email_delivery_events_execution", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_email_delivery_events_execution_occurred",
        "email_delivery_events", ["execution_id", "occurred_at", "id"],
    )
    op.create_index("ix_email_delivery_events_type", "email_delivery_events", ["event_type"])
    op.create_index(
        "uq_email_delivery_events_provider_id",
        "email_delivery_events", ["execution_id", "source", "provider_event_id"], unique=True,
        postgresql_where=sa.text("provider_event_id IS NOT NULL"),
        sqlite_where=sa.text("provider_event_id IS NOT NULL"),
    )


def downgrade():
    op.drop_index("uq_email_delivery_events_provider_id", table_name="email_delivery_events")
    op.drop_index("ix_email_delivery_events_type", table_name="email_delivery_events")
    op.drop_index("ix_email_delivery_events_execution_occurred", table_name="email_delivery_events")
    op.drop_table("email_delivery_events")

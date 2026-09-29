"""Add auditable, human-triggered outreach executions and provider events."""
from alembic import op
import sqlalchemy as sa

revision = "0013_outreach_operations"
down_revision = "0012_revenue_conversion"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "outreach_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("outreach_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("retry_of_execution_id", sa.Uuid(), nullable=True),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("manual_method", sa.String(20), nullable=True),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempt_number", sa.Integer(), server_default="1", nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("recipient", sa.String(320), nullable=True),
        sa.Column("subject", sa.String(500), nullable=True),
        sa.Column("content_snapshot", sa.JSON(), nullable=False),
        sa.Column("provider_message_id", sa.String(512), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("channel IN ('EMAIL','WHATSAPP','MANUAL')", name="ck_outreach_executions_channel"),
        sa.CheckConstraint("status IN ('QUEUED','SENDING','SENT','DELIVERED','FAILED','CANCELLED')", name="ck_outreach_executions_status"),
        sa.CheckConstraint("attempt_number >= 1", name="ck_outreach_executions_attempt_number"),
        sa.CheckConstraint("manual_method IS NULL OR (channel = 'MANUAL' AND manual_method IN ('EMAIL','WHATSAPP'))", name="ck_outreach_executions_manual_method"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["outreach_artifact_id", "lead_id"], ["outreach_artifacts.id", "outreach_artifacts.lead_id"], name="fk_outreach_executions_artifact_lead", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["retry_of_execution_id", "lead_id", "outreach_artifact_id"], ["outreach_executions.id", "outreach_executions.lead_id", "outreach_executions.outreach_artifact_id"], name="fk_outreach_executions_retry_scope"),
        sa.UniqueConstraint("id", "lead_id", name="uq_outreach_executions_id_lead"),
        sa.UniqueConstraint("id", "lead_id", "outreach_artifact_id", name="uq_outreach_executions_id_lead_artifact"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outreach_executions_lead_created", "outreach_executions", ["lead_id", "created_at"])
    op.create_index("ix_outreach_executions_artifact_created", "outreach_executions", ["outreach_artifact_id", "created_at"])
    op.create_index("ix_outreach_executions_status", "outreach_executions", ["status"])
    op.create_index("ix_outreach_executions_channel", "outreach_executions", ["channel"])
    op.create_index("uq_outreach_executions_artifact_key", "outreach_executions", ["outreach_artifact_id", "idempotency_key"], unique=True)
    op.create_index("uq_outreach_executions_active_artifact", "outreach_executions", ["outreach_artifact_id"], unique=True, postgresql_where=sa.text("status IN ('QUEUED','SENDING','SENT','DELIVERED')"), sqlite_where=sa.text("status IN ('QUEUED','SENDING','SENT','DELIVERED')"))
    op.create_index("uq_outreach_executions_provider_message", "outreach_executions", ["provider", "provider_message_id"], unique=True, postgresql_where=sa.text("provider_message_id IS NOT NULL"), sqlite_where=sa.text("provider_message_id IS NOT NULL"))

    op.add_column("sales_activities", sa.Column("outreach_execution_id", sa.Uuid(), nullable=True))
    op.create_index("uq_sales_activities_execution", "sales_activities", ["outreach_execution_id"], unique=True)
    op.create_foreign_key("fk_sales_activities_execution_lead", "sales_activities", "outreach_executions", ["outreach_execution_id", "lead_id"], ["id", "lead_id"], ondelete="CASCADE")

    op.create_table(
        "outreach_provider_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("provider_event_id", sa.String(512), nullable=False),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("event_type IN ('DELIVERED','READ','REPLIED','BOUNCED','FAILED')", name="ck_outreach_provider_events_type"),
        sa.ForeignKeyConstraint(["execution_id"], ["outreach_executions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outreach_provider_events_execution", "outreach_provider_events", ["execution_id", "occurred_at"])
    op.create_index("uq_outreach_provider_events_provider_event", "outreach_provider_events", ["provider", "provider_event_id"], unique=True)


def downgrade():
    op.drop_index("uq_outreach_provider_events_provider_event", table_name="outreach_provider_events")
    op.drop_index("ix_outreach_provider_events_execution", table_name="outreach_provider_events")
    op.drop_table("outreach_provider_events")
    op.drop_constraint("fk_sales_activities_execution_lead", "sales_activities", type_="foreignkey")
    op.drop_index("uq_sales_activities_execution", table_name="sales_activities")
    op.drop_column("sales_activities", "outreach_execution_id")
    op.drop_index("uq_outreach_executions_provider_message", table_name="outreach_executions")
    op.drop_index("uq_outreach_executions_active_artifact", table_name="outreach_executions")
    op.drop_index("uq_outreach_executions_artifact_key", table_name="outreach_executions")
    op.drop_index("ix_outreach_executions_channel", table_name="outreach_executions")
    op.drop_index("ix_outreach_executions_status", table_name="outreach_executions")
    op.drop_index("ix_outreach_executions_artifact_created", table_name="outreach_executions")
    op.drop_index("ix_outreach_executions_lead_created", table_name="outreach_executions")
    op.drop_table("outreach_executions")

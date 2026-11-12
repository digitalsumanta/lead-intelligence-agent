"""0041_supervisor_dispatch_delivery_events.py

Revision ID: 0041_supervisor_dispatch_delivery_events
Revises: 0040_supervisor_approval_dispatches
Create Date: 2026-10-28 14:00:00.000000

Phase 12J - D2 - Gate D-C4: Delivery Telemetry Ledger Schema
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0041_supervisor_dispatch_delivery_events'
down_revision = '0040_supervisor_approval_dispatches'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "supervisor_dispatch_delivery_events",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "dispatch_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("supervisor_approval_dispatches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("dispatch_reference_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("lead_reference_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("provider_name", sa.String(60), nullable=False),
        sa.Column("provider_message_id", sa.String(512), nullable=False),
        sa.Column("provider_event_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("raw_event_type", sa.String(80), nullable=False),
        sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("recipient_masked", sa.String(255), nullable=True),
        sa.Column("bounce_type", sa.String(30), nullable=True),
        sa.Column("bounce_code", sa.String(40), nullable=True),
        sa.Column("bounce_reason", sa.String(500), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("processed_for_reconciliation", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "event_type IN ('ACCEPTED', 'DELIVERED', 'BOUNCED', 'DROPPED', 'COMPLAINT', 'REJECTED', 'OTHER')",
            name="ck_delivery_event_type",
        ),
    )
    op.create_index(
        "uq_delivery_event_provider_event_id",
        "supervisor_dispatch_delivery_events",
        ["provider_name", "provider_event_id"],
        unique=True,
    )
    op.create_index(
        "ix_delivery_event_provider_message_id",
        "supervisor_dispatch_delivery_events",
        ["provider_message_id", "created_at"],
    )
    op.create_index(
        "ix_delivery_event_dispatch_ref_created",
        "supervisor_dispatch_delivery_events",
        ["dispatch_reference_id", "created_at"],
    )
    op.create_index(
        "ix_delivery_event_unprocessed",
        "supervisor_dispatch_delivery_events",
        ["processed_for_reconciliation", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("supervisor_dispatch_delivery_events")

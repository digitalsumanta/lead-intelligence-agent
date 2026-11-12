"""0040_supervisor_approval_dispatches.py

Revision ID: 0040_supervisor_approval_dispatches
Revises: 0039_supervisor_approval_audit_retention
Create Date: 2026-10-28 11:25:00.000000

Phase 12J - D2 - Gate D-B2: Transactional Dispatch Outbox Schema
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0040_supervisor_approval_dispatches'
down_revision = '0039_supervisor_approval_audit_retention'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "supervisor_approval_dispatches",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("approval_id", sa.Uuid(as_uuid=True), sa.ForeignKey("supervisor_human_approvals.id", ondelete="SET NULL"), nullable=True),
        sa.Column("approval_reference_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("lead_id", sa.Uuid(as_uuid=True), sa.ForeignKey("leads.id", ondelete="SET NULL"), nullable=True),
        sa.Column("lead_reference_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("action_id", sa.String(180), nullable=False),
        sa.Column("channel", sa.String(30), nullable=False),
        sa.Column("recipient", sa.String(255), nullable=False),
        sa.Column("context_fingerprint", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="PENDING"),
        sa.Column("dispatch_worker_id", sa.String(120), nullable=True),
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("provider_name", sa.String(60), nullable=False),
        sa.Column("provider_message_id", sa.String(512), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("error_message", sa.String(1000), nullable=True),
        sa.Column("reconciliation_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('PENDING', 'IN_PROGRESS', 'SENT', 'FAILED', 'RETRYABLE_FAILURE', 'OUTCOME_UNKNOWN', 'ABORTED', 'RECONCILED')",
            name="ck_dispatch_status",
        ),
        sa.CheckConstraint(
            "channel IN ('EMAIL', 'WHATSAPP', 'PHONE', 'WEBHOOK')",
            name="ck_dispatch_channel",
        ),
    )
    op.create_index("uq_dispatch_idempotency_key", "supervisor_approval_dispatches", ["idempotency_key"], unique=True)
    op.create_index("ix_dispatch_status_next_retry", "supervisor_approval_dispatches", ["status", "next_retry_at"])
    op.create_index("ix_dispatch_approval_ref_created", "supervisor_approval_dispatches", ["approval_reference_id", "created_at"])
    op.create_index("ix_dispatch_lead_ref_created", "supervisor_approval_dispatches", ["lead_reference_id", "created_at"])
    op.create_index("ix_dispatch_lead_created", "supervisor_approval_dispatches", ["lead_id", "created_at"])
    op.create_index("ix_dispatch_stale_in_progress", "supervisor_approval_dispatches", ["status", "last_attempt_at"])


def downgrade() -> None:
    op.drop_table("supervisor_approval_dispatches")

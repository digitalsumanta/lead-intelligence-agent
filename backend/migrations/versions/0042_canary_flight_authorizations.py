"""0042_canary_flight_authorizations.py

Revision ID: 0042_canary_flight_authorizations
Revises: 0041_supervisor_dispatch_delivery_events
Create Date: 2026-10-28 17:00:00.000000

Phase 12J - D2 - Gate D-D3: Canary Flight Two-Person Authorization Ledger Schema
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0042_canary_flight_authorizations'
down_revision = '0041_supervisor_dispatch_delivery_events'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "canary_flight_authorizations",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("flight_name", sa.String(120), nullable=False),
        sa.Column("environment", sa.String(30), nullable=False, server_default="staging"),
        sa.Column("provider_name", sa.String(60), nullable=False, server_default="RESEND"),
        sa.Column("channel", sa.String(30), nullable=False, server_default="EMAIL"),
        sa.Column("sender_identity", sa.String(255), nullable=True),
        sa.Column("allowed_domains_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("max_messages", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("dispatched_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_concurrency", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("rate_limit_per_second", sa.Float(), nullable=False, server_default="0.2"),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scope_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="PENDING_APPROVAL"),
        sa.Column("execution_authorized", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("signer_1_user_id", sa.String(120), nullable=True),
        sa.Column("signer_1_role", sa.String(60), nullable=True),
        sa.Column("signer_1_signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signer_2_user_id", sa.String(120), nullable=True),
        sa.Column("signer_2_role", sa.String(60), nullable=True),
        sa.Column("signer_2_signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.String(500), nullable=True),
        sa.Column("revoked_by_user_id", sa.String(120), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revocation_reason", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('PENDING_APPROVAL', 'AUTHORIZED', 'REJECTED', 'EXPIRED', 'REVOKED', 'EXHAUSTED')",
            name="ck_canary_flight_status",
        ),
    )
    op.create_index(
        "ix_canary_flight_status_expires",
        "canary_flight_authorizations",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_canary_flight_fingerprint",
        "canary_flight_authorizations",
        ["scope_fingerprint"],
    )


def downgrade() -> None:
    op.drop_table("canary_flight_authorizations")

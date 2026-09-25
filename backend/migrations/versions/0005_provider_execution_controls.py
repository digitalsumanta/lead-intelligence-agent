"""Add API key leases and provider request outcome telemetry."""
from alembic import op
import sqlalchemy as sa


revision = "0005_provider_execution_controls"
down_revision = "0004_enrichment_evidence"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("api_keys", sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("api_keys", sa.Column("operation_scope", sa.String(120)))
    op.add_column("api_keys", sa.Column("last_failure_at", sa.DateTime(timezone=True)))
    op.add_column("api_keys", sa.Column("quota_exhausted_until", sa.DateTime(timezone=True)))
    op.add_column("api_keys", sa.Column("lease_token", sa.String(36)))
    op.add_column("api_keys", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_api_keys_provider_operation_available",
        "api_keys",
        ["provider", "operation_scope", "enabled", "status", "cooldown_until", "quota_exhausted_until", "lease_expires_at"],
    )

    op.add_column("provider_usage", sa.Column("operation", sa.String(120)))
    op.add_column("provider_usage", sa.Column("key_fingerprint", sa.String(64)))
    op.add_column("provider_usage", sa.Column("outcome", sa.String(30)))
    op.add_column("provider_usage", sa.Column("http_status", sa.Integer()))
    op.add_column("provider_usage", sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("provider_usage", sa.Column("error_category", sa.String(40)))
    op.add_column("provider_usage", sa.Column("rate_limit_limit", sa.Integer()))
    op.add_column("provider_usage", sa.Column("rate_limit_remaining", sa.Integer()))
    op.add_column("provider_usage", sa.Column("rate_limit_reset_at", sa.DateTime(timezone=True)))
    op.add_column("provider_usage", sa.Column("quota_units", sa.Float()))
    op.add_column("provider_usage", sa.Column("cost_units", sa.Float()))
    op.add_column("provider_usage", sa.Column("started_at", sa.DateTime(timezone=True)))
    op.add_column("provider_usage", sa.Column("completed_at", sa.DateTime(timezone=True)))
    op.create_index("ix_provider_usage_provider_operation_created", "provider_usage", ["provider", "operation", "created_at"])

    op.create_table(
        "provider_operation_state",
        sa.Column("provider", sa.String(40), primary_key=True),
        sa.Column("operation", sa.String(120), primary_key=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True)),
        sa.Column("quota_exhausted_until", sa.DateTime(timezone=True)),
        sa.Column("last_failure_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("provider_operation_state")
    op.drop_index("ix_provider_usage_provider_operation_created", table_name="provider_usage")
    for column in (
        "completed_at", "started_at", "cost_units", "quota_units", "rate_limit_reset_at",
        "rate_limit_remaining", "rate_limit_limit", "error_category", "retry_count",
        "http_status", "outcome", "key_fingerprint", "operation",
    ):
        op.drop_column("provider_usage", column)
    op.drop_index("ix_api_keys_provider_operation_available", table_name="api_keys")
    for column in ("lease_expires_at", "lease_token", "quota_exhausted_until", "last_failure_at", "operation_scope", "enabled"):
        op.drop_column("api_keys", column)

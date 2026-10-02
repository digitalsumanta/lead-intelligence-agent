"""Preserve provider timestamp provenance for delivery reconciliation."""

from alembic import op
import sqlalchemy as sa


revision = "0028_delivery_reconcile"
down_revision = "0027_delivery_provider_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "email_delivery_events",
        sa.Column("provider_occurred_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("email_delivery_events", "provider_occurred_at")

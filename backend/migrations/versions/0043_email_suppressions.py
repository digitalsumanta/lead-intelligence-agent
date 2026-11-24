"""0043_email_suppressions.py

Revision ID: 0043_email_suppressions
Revises: 0042_canary_flight_authorizations
Create Date: 2026-11-24 12:00:00.000000

Email compliance: global suppression (do-not-email) list.
"""
from alembic import op
import sqlalchemy as sa


revision = "0043_email_suppressions"
down_revision = "0042_canary_flight_authorizations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_suppressions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("email_hash", sa.String(64), nullable=False),
        sa.Column("email_masked", sa.String(255), nullable=True),
        sa.Column("reason", sa.String(30), nullable=False),
        sa.Column("source", sa.String(60), nullable=False),
        sa.Column("details", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "reason IN ('UNSUBSCRIBED', 'HARD_BOUNCE', 'COMPLAINT', 'MANUAL')",
            name="ck_email_suppression_reason",
        ),
        sa.UniqueConstraint("email_hash", name="uq_email_suppressions_email_hash"),
    )


def downgrade() -> None:
    op.drop_table("email_suppressions")

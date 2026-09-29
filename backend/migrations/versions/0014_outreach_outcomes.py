"""Allow idempotent outcome activities for sent outreach executions.

Downgrade is data-dependent: it refuses to restore the old one-activity-per-
execution unique index when multiple linked activities have been recorded.
"""
from alembic import op
import sqlalchemy as sa


revision = "0014_outreach_outcomes"
down_revision = "0013_outreach_operations"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("uq_sales_activities_execution", table_name="sales_activities")
    op.add_column(
        "sales_activities",
        sa.Column("outreach_idempotency_key", sa.String(length=100), nullable=True),
    )
    op.create_index(
        "uq_sales_activities_execution_idempotency",
        "sales_activities",
        ["outreach_execution_id", "outreach_idempotency_key"],
        unique=True,
        postgresql_where=sa.text("outreach_idempotency_key IS NOT NULL"),
        sqlite_where=sa.text("outreach_idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_sales_activities_execution_occurred",
        "sales_activities",
        ["outreach_execution_id", "occurred_at"],
    )


def downgrade():
    bind = op.get_bind()
    if op.get_context().as_sql:
        # Keep the data guard in generated PostgreSQL downgrade scripts too.
        op.execute(sa.text("""DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM sales_activities
                WHERE outreach_execution_id IS NOT NULL
                GROUP BY outreach_execution_id
                HAVING COUNT(*) > 1
            ) THEN
                RAISE EXCEPTION 'Cannot downgrade 0014_outreach_outcomes: multiple sales activities are linked to one outreach execution; preserve the outcome records and resolve them before restoring uq_sales_activities_execution.';
            END IF;
        END
        $$"""))
    else:
        duplicate_execution = bind.execute(sa.text(
            "SELECT outreach_execution_id FROM sales_activities "
            "WHERE outreach_execution_id IS NOT NULL "
            "GROUP BY outreach_execution_id HAVING COUNT(*) > 1 LIMIT 1"
        )).first()
        if duplicate_execution is not None:
            raise RuntimeError(
                "Cannot downgrade 0014_outreach_outcomes: multiple sales activities "
                "are linked to one outreach execution; preserve the outcome records "
                "and resolve them before restoring uq_sales_activities_execution."
            )

    op.drop_index("ix_sales_activities_execution_occurred", table_name="sales_activities")
    op.drop_index("uq_sales_activities_execution_idempotency", table_name="sales_activities")
    op.drop_column("sales_activities", "outreach_idempotency_key")
    op.create_index(
        "uq_sales_activities_execution",
        "sales_activities",
        ["outreach_execution_id"],
        unique=True,
    )

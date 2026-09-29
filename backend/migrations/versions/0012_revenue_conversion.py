"""Add commercial values, collection ledger, targets, and structured stage history."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0012_revenue_conversion"
down_revision = "0011_sales_tasks"
branch_labels = None
depends_on = None

sales_status = postgresql.ENUM(
    "NEW", "QUALIFIED", "CONTACTED", "ENGAGED", "PROPOSAL", "NEGOTIATION", "WON", "LOST",
    name="salesstatus", create_type=False,
)


def upgrade():
    op.add_column("leads", sa.Column("estimated_value", sa.Numeric(18, 2), nullable=True))
    op.add_column("leads", sa.Column("proposal_value", sa.Numeric(18, 2), nullable=True))
    op.add_column("leads", sa.Column("negotiation_value", sa.Numeric(18, 2), nullable=True))
    op.add_column("leads", sa.Column("won_value", sa.Numeric(18, 2), nullable=True))
    op.add_column("leads", sa.Column("expected_collection", sa.Numeric(18, 2), nullable=True))
    op.add_column("leads", sa.Column("currency", sa.String(3), server_default="INR", nullable=False))
    op.create_check_constraint("ck_leads_estimated_value_nonnegative", "leads", "estimated_value IS NULL OR estimated_value >= 0")
    op.create_check_constraint("ck_leads_proposal_value_nonnegative", "leads", "proposal_value IS NULL OR proposal_value >= 0")
    op.create_check_constraint("ck_leads_negotiation_value_nonnegative", "leads", "negotiation_value IS NULL OR negotiation_value >= 0")
    op.create_check_constraint("ck_leads_won_value_nonnegative", "leads", "won_value IS NULL OR won_value >= 0")
    op.create_check_constraint("ck_leads_expected_collection_nonnegative", "leads", "expected_collection IS NULL OR expected_collection >= 0")
    op.create_check_constraint("ck_leads_currency_code", "leads", "currency = upper(currency) AND length(currency) = 3")

    op.add_column("sales_activities", sa.Column("old_sales_status", sales_status, nullable=True))
    op.add_column("sales_activities", sa.Column("new_sales_status", sales_status, nullable=True))
    op.create_check_constraint(
        "ck_sales_activities_structured_status_change", "sales_activities",
        "(old_sales_status IS NULL AND new_sales_status IS NULL) OR (old_sales_status IS NOT NULL AND new_sales_status IS NOT NULL AND activity_type = 'STATUS_CHANGE')",
    )

    op.create_table(
        "sales_collections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("collection_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payment_reference", sa.String(250), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_sales_collections_amount_positive"),
        sa.CheckConstraint("currency = upper(currency) AND length(currency) = 3", name="ck_sales_collections_currency_code"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sales_collections_lead_date", "sales_collections", ["lead_id", "collection_date"])
    op.create_index("ix_sales_collections_currency_date", "sales_collections", ["currency", "collection_date"])

    op.create_table(
        "revenue_targets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("target_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(3), server_default="INR", nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("target_amount >= 0", name="ck_revenue_targets_amount_nonnegative"),
        sa.CheckConstraint("period_start <= period_end", name="ck_revenue_targets_period_order"),
        sa.CheckConstraint("currency = upper(currency) AND length(currency) = 3", name="ck_revenue_targets_currency_code"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_revenue_targets_active_currency", "revenue_targets", ["currency"], unique=True,
        postgresql_where=sa.text("active = true"), sqlite_where=sa.text("active = 1"),
    )
    op.create_index("ix_revenue_targets_period", "revenue_targets", ["currency", "period_start", "period_end"])


def downgrade():
    op.drop_index("ix_revenue_targets_period", table_name="revenue_targets")
    op.drop_index("uq_revenue_targets_active_currency", table_name="revenue_targets")
    op.drop_table("revenue_targets")
    op.drop_index("ix_sales_collections_currency_date", table_name="sales_collections")
    op.drop_index("ix_sales_collections_lead_date", table_name="sales_collections")
    op.drop_table("sales_collections")
    op.drop_constraint("ck_sales_activities_structured_status_change", "sales_activities", type_="check")
    op.drop_column("sales_activities", "new_sales_status")
    op.drop_column("sales_activities", "old_sales_status")
    for name in (
        "ck_leads_currency_code", "ck_leads_expected_collection_nonnegative",
        "ck_leads_won_value_nonnegative", "ck_leads_negotiation_value_nonnegative",
        "ck_leads_proposal_value_nonnegative", "ck_leads_estimated_value_nonnegative",
    ):
        op.drop_constraint(name, "leads", type_="check")
    op.drop_column("leads", "currency")
    op.drop_column("leads", "expected_collection")
    op.drop_column("leads", "won_value")
    op.drop_column("leads", "negotiation_value")
    op.drop_column("leads", "proposal_value")
    op.drop_column("leads", "estimated_value")

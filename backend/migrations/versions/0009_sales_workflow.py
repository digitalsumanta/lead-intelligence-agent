"""Add persistent sales workflow fields and activity history."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009_sales_workflow"
down_revision = "0008_personalization"
branch_labels = None
depends_on = None

sales_status = sa.Enum("NEW", "QUALIFIED", "CONTACTED", "ENGAGED", "PROPOSAL", "NEGOTIATION", "WON", "LOST", name="salesstatus")
sales_priority = sa.Enum("LOW", "MEDIUM", "HIGH", "URGENT", name="salespriority")
activity_type = postgresql.ENUM("NOTE", "CALL", "EMAIL", "WHATSAPP", "MEETING", "STATUS_CHANGE", "FOLLOW_UP", "PROPOSAL", "OTHER", name="salesactivitytype", create_type=False)


def upgrade():
    bind = op.get_bind()
    sales_status.create(bind, checkfirst=False)
    sales_priority.create(bind, checkfirst=False)
    activity_type.create(bind, checkfirst=False)
    op.add_column("leads", sa.Column("sales_status", sales_status, nullable=False, server_default="NEW"))
    op.add_column("leads", sa.Column("priority", sales_priority, nullable=False, server_default="MEDIUM"))
    op.add_column("leads", sa.Column("assigned_to", sa.String(120), nullable=True))
    op.add_column("leads", sa.Column("next_follow_up_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("leads", sa.Column("lost_reason", sa.String(1000), nullable=True))
    op.create_index("ix_leads_sales_status", "leads", ["sales_status"])
    op.create_index("ix_leads_priority", "leads", ["priority"])
    op.create_index("ix_leads_next_follow_up_at", "leads", ["next_follow_up_at"])
    op.create_table(
        "sales_activities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("activity_type", activity_type, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sales_activities_lead_occurred", "sales_activities", ["lead_id", "occurred_at"])


def downgrade():
    op.drop_index("ix_sales_activities_lead_occurred", table_name="sales_activities")
    op.drop_table("sales_activities")
    op.drop_index("ix_leads_next_follow_up_at", table_name="leads")
    op.drop_index("ix_leads_priority", table_name="leads")
    op.drop_index("ix_leads_sales_status", table_name="leads")
    op.drop_column("leads", "lost_reason")
    op.drop_column("leads", "next_follow_up_at")
    op.drop_column("leads", "assigned_to")
    op.drop_column("leads", "priority")
    op.drop_column("leads", "sales_status")
    bind = op.get_bind()
    activity_type.drop(bind, checkfirst=False)
    sales_priority.drop(bind, checkfirst=False)
    sales_status.drop(bind, checkfirst=False)

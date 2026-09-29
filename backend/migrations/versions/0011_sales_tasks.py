"""Add deterministic sales task management and activity outcomes."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0011_sales_tasks"
down_revision = "0010_outreach_preparation"
branch_labels = None
depends_on = None

task_type = postgresql.ENUM(
    "FOLLOW_UP", "CALL", "EMAIL", "WHATSAPP", "MEETING", "PROPOSAL", "REVIEW_OUTREACH", "OTHER",
    name="salestasktype", create_type=False,
)
task_status = postgresql.ENUM(
    "OPEN", "IN_PROGRESS", "COMPLETED", "CANCELLED", name="salestaskstatus", create_type=False,
)
activity_outcome = postgresql.ENUM(
    "NO_RESPONSE", "CONNECTED", "INTERESTED", "NOT_INTERESTED", "CALLBACK_REQUESTED",
    "MEETING_BOOKED", "PROPOSAL_REQUESTED", "PROPOSAL_SENT", "NEGOTIATION_STARTED",
    "WON", "LOST", "INVALID_CONTACT", name="salesactivityoutcome", create_type=False,
)
sales_priority = postgresql.ENUM("LOW", "MEDIUM", "HIGH", "URGENT", name="salespriority", create_type=False)


def upgrade():
    bind = op.get_bind()
    task_type.create(bind, checkfirst=False)
    task_status.create(bind, checkfirst=False)
    activity_outcome.create(bind, checkfirst=False)
    op.add_column("sales_activities", sa.Column("outcome", activity_outcome, nullable=True))
    op.create_index("uq_sales_activities_id_lead", "sales_activities", ["id", "lead_id"], unique=True)
    op.create_index("uq_outreach_artifacts_id_lead", "outreach_artifacts", ["id", "lead_id"], unique=True)
    op.create_table(
        "sales_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("assigned_to", sa.String(120), nullable=True),
        sa.Column("task_type", task_type, nullable=False),
        sa.Column("title", sa.String(250), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("priority", sales_priority, nullable=False),
        sa.Column("status", task_status, server_default="OPEN", nullable=False),
        sa.Column("related_activity_id", sa.Uuid(), nullable=True),
        sa.Column("related_outreach_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["related_activity_id", "lead_id"], ["sales_activities.id", "sales_activities.lead_id"],
            name="fk_sales_tasks_activity_lead",
        ),
        sa.ForeignKeyConstraint(
            ["related_outreach_artifact_id", "lead_id"], ["outreach_artifacts.id", "outreach_artifacts.lead_id"],
            name="fk_sales_tasks_outreach_lead",
        ),
        sa.CheckConstraint(
            "(status = 'COMPLETED' AND completed_at IS NOT NULL) OR (status <> 'COMPLETED' AND completed_at IS NULL)",
            name="ck_sales_tasks_completed_at_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sales_tasks_lead", "sales_tasks", ["lead_id"])
    op.create_index("ix_sales_tasks_status", "sales_tasks", ["status"])
    op.create_index("ix_sales_tasks_due_at", "sales_tasks", ["due_at"])
    op.create_index("ix_sales_tasks_assigned_to", "sales_tasks", ["assigned_to"])
    op.create_index("uq_sales_tasks_id_lead", "sales_tasks", ["id", "lead_id"], unique=True)


def downgrade():
    op.drop_index("uq_sales_tasks_id_lead", table_name="sales_tasks")
    op.drop_index("ix_sales_tasks_assigned_to", table_name="sales_tasks")
    op.drop_index("ix_sales_tasks_due_at", table_name="sales_tasks")
    op.drop_index("ix_sales_tasks_status", table_name="sales_tasks")
    op.drop_index("ix_sales_tasks_lead", table_name="sales_tasks")
    op.drop_table("sales_tasks")
    op.drop_index("uq_outreach_artifacts_id_lead", table_name="outreach_artifacts")
    op.drop_index("uq_sales_activities_id_lead", table_name="sales_activities")
    op.drop_column("sales_activities", "outcome")
    activity_outcome.drop(op.get_bind(), checkfirst=False)
    task_status.drop(op.get_bind(), checkfirst=False)
    task_type.drop(op.get_bind(), checkfirst=False)

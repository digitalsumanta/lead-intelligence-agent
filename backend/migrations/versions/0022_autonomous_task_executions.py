"""Add internal Task Agent provenance and append-only execution audits."""

from alembic import op
import sqlalchemy as sa


revision = "0022_autonomous_task_executions"
down_revision = "0021_ai_sales_supervisor"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sales_tasks", sa.Column("created_by_agent", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("sales_tasks", sa.Column("agent_source_type", sa.String(length=40), nullable=True))
    op.add_column("sales_tasks", sa.Column("agent_source_id", sa.Uuid(), nullable=True))
    op.add_column("sales_tasks", sa.Column("agent_source_action_id", sa.String(length=180), nullable=True))
    op.add_column("sales_tasks", sa.Column("agent_evidence_refs", sa.JSON(), server_default=sa.text("'[]'"), nullable=False))
    op.create_check_constraint("ck_sales_tasks_agent_source_type", "sales_tasks", "agent_source_type IS NULL OR agent_source_type IN ('AI_SALES_ACTION_PLAN','AI_SALES_SUPERVISOR')")
    op.create_index(
        "uq_sales_tasks_agent_source_action", "sales_tasks",
        ["lead_id", "agent_source_type", "agent_source_id", "agent_source_action_id"], unique=True,
        postgresql_where=sa.text("agent_source_id IS NOT NULL"),
        sqlite_where=sa.text("agent_source_id IS NOT NULL"),
    )
    op.create_table(
        "autonomous_task_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("action_type", sa.String(length=60), nullable=False),
        sa.Column("source_type", sa.String(length=40), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("source_action_id", sa.String(length=180), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("policy_evaluation_id", sa.Uuid(), nullable=True),
        sa.Column("policy_decision", sa.String(length=30), nullable=True),
        sa.Column("context_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("execution_status", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("requested_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("action_type IN ('FOLLOW_UP','CALL_PROSPECT','REVIEW_SEO_OPPORTUNITY','REVIEW_OUTREACH','PREPARE_PROPOSAL','INVESTIGATE_MISSING_INFORMATION','REVIEW_COMMERCIAL_CONTEXT')", name="ck_autonomous_task_action"),
        sa.CheckConstraint("source_type IN ('AI_SALES_ACTION_PLAN','AI_SALES_SUPERVISOR')", name="ck_autonomous_task_source_type"),
        sa.CheckConstraint("execution_status IN ('CREATED','ALREADY_EXISTS','DENIED','REQUIRES_HUMAN_APPROVAL','STALE_CONTEXT','INVALID_REQUEST','FAILED')", name="ck_autonomous_task_status"),
        sa.CheckConstraint("policy_decision IS NULL OR policy_decision IN ('ALLOW','DENY','REQUIRE_HUMAN_APPROVAL')", name="ck_autonomous_task_policy_decision"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], name="fk_autonomous_task_execution_lead", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id", "lead_id"], ["sales_tasks.id", "sales_tasks.lead_id"], name="fk_autonomous_task_execution_task_lead"),
        sa.ForeignKeyConstraint(["policy_evaluation_id"], ["agent_policy_evaluations.id"], name="fk_autonomous_task_execution_policy"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_autonomous_task_executions_lead_created", "autonomous_task_executions", ["lead_id", "created_at"])
    op.create_index("ix_autonomous_task_executions_source", "autonomous_task_executions", ["source_type", "source_id"])
    op.create_index(
        "uq_autonomous_task_executions_created_key", "autonomous_task_executions", ["idempotency_key"], unique=True,
        postgresql_where=sa.text("execution_status = 'CREATED'"),
        sqlite_where=sa.text("execution_status = 'CREATED'"),
    )


def downgrade():
    op.drop_index("uq_autonomous_task_executions_created_key", table_name="autonomous_task_executions")
    op.drop_index("ix_autonomous_task_executions_source", table_name="autonomous_task_executions")
    op.drop_index("ix_autonomous_task_executions_lead_created", table_name="autonomous_task_executions")
    op.drop_table("autonomous_task_executions")
    op.drop_index("uq_sales_tasks_agent_source_action", table_name="sales_tasks")
    op.drop_constraint("ck_sales_tasks_agent_source_type", "sales_tasks", type_="check")
    op.drop_column("sales_tasks", "agent_evidence_refs")
    op.drop_column("sales_tasks", "agent_source_action_id")
    op.drop_column("sales_tasks", "agent_source_id")
    op.drop_column("sales_tasks", "agent_source_type")
    op.drop_column("sales_tasks", "created_by_agent")

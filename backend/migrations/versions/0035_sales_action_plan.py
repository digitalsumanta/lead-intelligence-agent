"""sales_action_plan

Revision ID: 0035_sales_action_plan
Revises: 0034_sales_opportunity
Create Date: 2026-10-04 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0035_sales_action_plan'
down_revision = '0034_sales_opportunity'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.drop_constraint('ck_sales_tasks_agent_source_type', 'sales_tasks', type_='check')
    op.create_check_constraint(
        'ck_sales_tasks_agent_source_type',
        'sales_tasks',
        "agent_source_type IS NULL OR agent_source_type IN ('AI_SALES_ACTION_PLAN','AI_SALES_SUPERVISOR', 'SALES_OPPORTUNITY')"
    )

def downgrade() -> None:
    op.drop_constraint('ck_sales_tasks_agent_source_type', 'sales_tasks', type_='check')
    op.create_check_constraint(
        'ck_sales_tasks_agent_source_type',
        'sales_tasks',
        "agent_source_type IS NULL OR agent_source_type IN ('AI_SALES_ACTION_PLAN','AI_SALES_SUPERVISOR')"
    )

"""sales_opportunity

Revision ID: 0034_sales_opportunity
Revises: 0033_buying_signal_detect
Create Date: 2026-10-03 23:30:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '0034_sales_opportunity'
down_revision = '0033_buying_signal_detect'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column('lead_intelligence_opportunities', sa.Column('opportunity_type', sa.String(length=100), server_default='GENERAL', nullable=True))
    op.add_column('lead_intelligence_opportunities', sa.Column('score', sa.Integer(), nullable=True))
    op.add_column('lead_intelligence_opportunities', sa.Column('priority', sa.String(length=50), nullable=True))
    op.add_column('lead_intelligence_opportunities', sa.Column('recommended_action', sa.String(length=100), nullable=True))
    op.add_column('lead_intelligence_opportunities', sa.Column('evidence', sa.JSON(), nullable=True))

def downgrade() -> None:
    op.drop_column('lead_intelligence_opportunities', 'evidence')
    op.drop_column('lead_intelligence_opportunities', 'recommended_action')
    op.drop_column('lead_intelligence_opportunities', 'priority')
    op.drop_column('lead_intelligence_opportunities', 'score')
    op.drop_column('lead_intelligence_opportunities', 'opportunity_type')

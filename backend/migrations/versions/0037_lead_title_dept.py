"""lead_title_dept

Revision ID: 0037_lead_title_dept
Revises: 0036_contact_intel
Create Date: 2026-10-06 09:35:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0037_lead_title_dept'
down_revision = '0036_contact_intel'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column('leads', sa.Column('job_title', sa.String(length=250), nullable=True))
    op.add_column('leads', sa.Column('department', sa.String(length=250), nullable=True))

def downgrade() -> None:
    op.drop_column('leads', 'department')
    op.drop_column('leads', 'job_title')

"""contact_intelligence

Revision ID: 0036_contact_intel
Revises: 0035_sales_action_plan
Create Date: 2026-10-05 16:35:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '0036_contact_intel'
down_revision = '0035_sales_action_plan'
branch_labels = None
depends_on = None

def upgrade() -> None:
    # Create enums
    senioritylevel = postgresql.ENUM('UNKNOWN', 'INDIVIDUAL_CONTRIBUTOR', 'MANAGER', 'LEADERSHIP', 'EXECUTIVE', name='senioritylevel')
    senioritylevel.create(op.get_bind(), checkfirst=True)
    
    functionalarea = postgresql.ENUM('UNKNOWN', 'EXECUTIVE', 'MARKETING', 'DIGITAL_MARKETING', 'SEO', 'SALES', 'IT', 'TECHNOLOGY', 'FINANCE', 'HR', 'OPERATIONS', 'PROCUREMENT', name='functionalarea')
    functionalarea.create(op.get_bind(), checkfirst=True)
    
    buyinginfluencelevel = postgresql.ENUM('UNKNOWN', 'LOW', 'MEDIUM', 'HIGH', 'DECISION_MAKER', name='buyinginfluencelevel')
    buyinginfluencelevel.create(op.get_bind(), checkfirst=True)

    # Create table
    op.create_table(
        'contact_intelligence_profiles',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('contact_id', sa.Uuid(), nullable=False),
        sa.Column('department', sa.String(length=255), nullable=True),
        sa.Column('functional_area', postgresql.ENUM('UNKNOWN', 'EXECUTIVE', 'MARKETING', 'DIGITAL_MARKETING', 'SEO', 'SALES', 'IT', 'TECHNOLOGY', 'FINANCE', 'HR', 'OPERATIONS', 'PROCUREMENT', name='functionalarea', create_type=False), nullable=False),
        sa.Column('seniority_level', postgresql.ENUM('UNKNOWN', 'INDIVIDUAL_CONTRIBUTOR', 'MANAGER', 'LEADERSHIP', 'EXECUTIVE', name='senioritylevel', create_type=False), nullable=False),
        sa.Column('decision_maker_score', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('buying_influence', postgresql.ENUM('UNKNOWN', 'LOW', 'MEDIUM', 'HIGH', 'DECISION_MAKER', name='buyinginfluencelevel', create_type=False), nullable=False),
        sa.Column('confidence', postgresql.ENUM('UNKNOWN', 'LOW', 'MEDIUM', 'HIGH', name='confidencelevel', create_type=False), nullable=False),
        sa.Column('source_type', postgresql.ENUM('LEAD_RECORD', 'COMPANY_RECORD', 'CONTACT_RECORD', 'USER_PROVIDED', 'SYSTEM_DERIVED', name='sourcetype', create_type=False), nullable=False),
        sa.Column('evidence', sa.JSON(), nullable=True),
        sa.Column('evaluated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['contact_id'], ['leads.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('contact_id')
    )

def downgrade() -> None:
    op.drop_table('contact_intelligence_profiles')
    
    # Drop enums
    senioritylevel = postgresql.ENUM('UNKNOWN', 'INDIVIDUAL_CONTRIBUTOR', 'MANAGER', 'LEADERSHIP', 'EXECUTIVE', name='senioritylevel')
    senioritylevel.drop(op.get_bind(), checkfirst=True)
    
    functionalarea = postgresql.ENUM('UNKNOWN', 'EXECUTIVE', 'MARKETING', 'DIGITAL_MARKETING', 'SEO', 'SALES', 'IT', 'TECHNOLOGY', 'FINANCE', 'HR', 'OPERATIONS', 'PROCUREMENT', name='functionalarea')
    functionalarea.drop(op.get_bind(), checkfirst=True)
    
    buyinginfluencelevel = postgresql.ENUM('UNKNOWN', 'LOW', 'MEDIUM', 'HIGH', 'DECISION_MAKER', name='buyinginfluencelevel')
    buyinginfluencelevel.drop(op.get_bind(), checkfirst=True)

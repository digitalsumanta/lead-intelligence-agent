"""Lead Intelligence Core."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0029_lead_intelligence_core"
down_revision = "0028_delivery_reconcile"
branch_labels = None
depends_on = None

def upgrade():
    # Enums
    sourcetype = postgresql.ENUM("LEAD_RECORD", "COMPANY_RECORD", "CONTACT_RECORD", "USER_PROVIDED", "SYSTEM_DERIVED", name="sourcetype", create_type=False)
    sourcetype.create(op.get_bind(), checkfirst=True)

    confidencelevel = postgresql.ENUM("UNKNOWN", "LOW", "MEDIUM", "HIGH", name="confidencelevel", create_type=False)
    confidencelevel.create(op.get_bind(), checkfirst=True)

    # LeadIntelligenceProfile
    op.create_table(
        "lead_intelligence_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("industry", sa.String(length=255), nullable=True),
        sa.Column("industry_source", sourcetype, nullable=True),
        sa.Column("industry_confidence", confidencelevel, nullable=True),
        sa.Column("company_size", sa.String(length=255), nullable=True),
        sa.Column("company_size_source", sourcetype, nullable=True),
        sa.Column("company_size_confidence", confidencelevel, nullable=True),
        sa.Column("website", sa.String(length=1000), nullable=True),
        sa.Column("location", sa.String(length=500), nullable=True),
        sa.Column("country", sa.String(length=120), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("last_researched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completeness_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lead_id")
    )

    # LeadIntelligencePainPoint
    op.create_table(
        "lead_intelligence_pain_points",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_type", sourcetype, nullable=False),
        sa.Column("source_reference", sa.String(length=255), nullable=True),
        sa.Column("confidence", confidencelevel, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id")
    )

    # LeadIntelligenceOpportunity
    op.create_table(
        "lead_intelligence_opportunities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_type", sourcetype, nullable=False),
        sa.Column("source_reference", sa.String(length=255), nullable=True),
        sa.Column("confidence", confidencelevel, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id")
    )

def downgrade():
    op.drop_table("lead_intelligence_opportunities")
    op.drop_table("lead_intelligence_pain_points")
    op.drop_table("lead_intelligence_profiles")
    
    sourcetype = postgresql.ENUM("LEAD_RECORD", "COMPANY_RECORD", "CONTACT_RECORD", "USER_PROVIDED", "SYSTEM_DERIVED", name="sourcetype", create_type=False)
    sourcetype.drop(op.get_bind(), checkfirst=True)

    confidencelevel = postgresql.ENUM("UNKNOWN", "LOW", "MEDIUM", "HIGH", name="confidencelevel", create_type=False)
    confidencelevel.drop(op.get_bind(), checkfirst=True)

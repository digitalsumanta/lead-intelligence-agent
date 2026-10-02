"""Company Enrichment Profile."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0030_company_enrichment"
down_revision = "0029_lead_intelligence_core"
branch_labels = None
depends_on = None

def upgrade():
    sourcetype = postgresql.ENUM("LEAD_RECORD", "COMPANY_RECORD", "CONTACT_RECORD", "USER_PROVIDED", "SYSTEM_DERIVED", name="sourcetype", create_type=False)
    confidencelevel = postgresql.ENUM("UNKNOWN", "LOW", "MEDIUM", "HIGH", name="confidencelevel", create_type=False)

    op.create_table(
        "company_enrichment_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("legal_name", sa.String(length=255), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.Column("website", sa.String(length=1000), nullable=True),
        sa.Column("email_domain", sa.String(length=255), nullable=True),
        sa.Column("linkedin_url", sa.String(length=1000), nullable=True),
        sa.Column("phone", sa.String(length=80), nullable=True),
        sa.Column("location", sa.String(length=500), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=120), nullable=True),
        sa.Column("country", sa.String(length=120), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("industry", sa.String(length=255), nullable=True),
        sa.Column("normalized_industry", sa.String(length=255), nullable=True),
        sa.Column("company_size", sa.String(length=255), nullable=True),
        sa.Column("normalized_company_size", sa.String(length=255), nullable=True),
        sa.Column("source", sourcetype, nullable=False),
        sa.Column("confidence", confidencelevel, nullable=False),
        sa.Column("completeness_percent", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lead_id")
    )

def downgrade():
    op.drop_table("company_enrichment_profiles")

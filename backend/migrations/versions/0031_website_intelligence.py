"""Website Intelligence Profile."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0031_website_intelligence"
down_revision = "0030_company_enrichment"
branch_labels = None
depends_on = None

def upgrade():
    sourcetype = postgresql.ENUM("LEAD_RECORD", "COMPANY_RECORD", "CONTACT_RECORD", "USER_PROVIDED", "SYSTEM_DERIVED", name="sourcetype", create_type=False)
    confidencelevel = postgresql.ENUM("UNKNOWN", "LOW", "MEDIUM", "HIGH", name="confidencelevel", create_type=False)

    op.create_table(
        "website_intelligence_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("website", sa.String(length=1000), nullable=True),
        sa.Column("normalized_website", sa.String(length=1000), nullable=True),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.Column("normalized_domain", sa.String(length=255), nullable=True),
        sa.Column("website_present", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("domain_present", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("technology_signals", sa.JSON(), nullable=True),
        sa.Column("content_signals", sa.JSON(), nullable=True),
        sa.Column("source_type", sourcetype, nullable=False),
        sa.Column("confidence", confidencelevel, nullable=False),
        sa.Column("completeness", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["company_id"], ["leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id")
    )

def downgrade():
    op.drop_table("website_intelligence_profiles")

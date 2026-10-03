"""Digital Marketing Intelligence Profile."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0032_digital_marketing_intel"
down_revision = "0031_website_intelligence"
branch_labels = None
depends_on = None

def upgrade():
    sourcetype = postgresql.ENUM("LEAD_RECORD", "COMPANY_RECORD", "CONTACT_RECORD", "USER_PROVIDED", "SYSTEM_DERIVED", name="sourcetype", create_type=False)
    confidencelevel = postgresql.ENUM("UNKNOWN", "LOW", "MEDIUM", "HIGH", name="confidencelevel", create_type=False)

    op.create_table(
        "digital_marketing_intelligence_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("seo_signals", sa.JSON(), nullable=True),
        sa.Column("paid_advertising_signals", sa.JSON(), nullable=True),
        sa.Column("social_media_signals", sa.JSON(), nullable=True),
        sa.Column("content_marketing_signals", sa.JSON(), nullable=True),
        sa.Column("marketing_maturity", sa.String(length=50), server_default=sa.text("'UNKNOWN'"), nullable=False),
        sa.Column("marketing_gaps", sa.JSON(), nullable=True),
        sa.Column("marketing_opportunities", sa.JSON(), nullable=True),
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
    op.drop_table("digital_marketing_intelligence_profiles")

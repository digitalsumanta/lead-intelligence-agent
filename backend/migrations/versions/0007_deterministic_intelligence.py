"""Persist deterministic opportunities, recommendations, and versioned scores."""
from alembic import op
import sqlalchemy as sa
from datetime import datetime, timezone


revision = "0007_deterministic_intelligence"
down_revision = "0006_resumable_enrichment_stages"
branch_labels = None
depends_on = None


SERVICES = (
    ("seo", "SEO", "Improve organic search visibility and technical on-page signals."),
    ("local_seo", "Local SEO", "Improve local business profile quality and geographic search visibility."),
    ("google_ads", "Google Ads", "Plan and manage paid search campaigns."),
    ("meta_ads", "Meta Ads", "Plan and manage paid campaigns across Meta platforms."),
    ("performance_marketing", "Performance Marketing", "Manage measurable cross-channel acquisition campaigns."),
    ("website_development", "Website Development", "Build or improve secure, reliable business websites."),
    ("website_cro", "Website CRO", "Improve website conversion paths using measured performance and UX signals."),
    ("ai_automation", "AI Automation", "Automate suitable business workflows with AI-enabled systems."),
    ("ai_chatbot", "AI Chatbot", "Provide automated conversational support and lead capture."),
    ("lead_generation", "Lead Generation", "Build measurable programs to acquire qualified leads."),
    ("social_media_management", "Social Media Management", "Plan and manage consistent social media publishing."),
)
SEED_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def upgrade():
    op.create_table(
        "service_catalog",
        sa.Column("service_id", sa.String(80), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.String(1000), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("version", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("severity IN ('low','medium','high','critical')", name="ck_opportunity_severity"),
        sa.CheckConstraint("status IN ('open','acknowledged','resolved','dismissed')", name="ck_opportunity_status"),
        sa.CheckConstraint("origin IN ('deterministic','ai')", name="ck_opportunity_origin"),
    )
    service_table = sa.table(
        "service_catalog",
        sa.column("service_id", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
        sa.column("active", sa.Boolean),
        sa.column("version", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(service_table, [
        {"service_id": key, "name": name, "description": description, "active": True, "version": "1", "created_at": SEED_TIME, "updated_at": SEED_TIME}
        for key, name, description in SERVICES
    ])
    op.create_table(
        "opportunities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("enrichment_run_id", sa.Uuid(), nullable=False),
        sa.Column("identity", sa.String(180), nullable=False),
        sa.Column("category", sa.String(80), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("title", sa.String(250), nullable=False),
        sa.Column("description", sa.String(2000), nullable=False),
        sa.Column("evidence_references", sa.JSON(), nullable=False),
        sa.Column("rule_version", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["enrichment_run_id"], ["enrichment_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_opportunities_lead_created", "opportunities", ["lead_id", "created_at"])
    op.create_index("ix_opportunities_run_category", "opportunities", ["enrichment_run_id", "category"])
    op.create_index("uq_opportunities_run_identity", "opportunities", ["enrichment_run_id", "identity"], unique=True)

    op.create_table(
        "service_recommendations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("enrichment_run_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.String(80), nullable=False),
        sa.Column("service_name", sa.String(160), nullable=False),
        sa.Column("service_description", sa.String(1000), nullable=False),
        sa.Column("service_version", sa.String(80), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("rationale", sa.String(2000), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("opportunity_references", sa.JSON(), nullable=False),
        sa.Column("evidence_references", sa.JSON(), nullable=False),
        sa.Column("rule_version", sa.String(100), nullable=False),
        sa.Column("ranking_score", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_service_recommendation_confidence"),
        sa.CheckConstraint("rank >= 1", name="ck_service_recommendation_rank"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["enrichment_run_id"], ["enrichment_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_id"], ["service_catalog.service_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_service_recommendations_lead_rank", "service_recommendations", ["lead_id", "rank"])
    op.create_index("uq_service_recommendations_run_service_rule", "service_recommendations", ["enrichment_run_id", "service_id", "rule_version"], unique=True)

    op.create_table(
        "lead_scores",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("lead_id", sa.Uuid(), nullable=False),
        sa.Column("enrichment_run_id", sa.Uuid(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("score_version", sa.String(100), nullable=False),
        sa.Column("components", sa.JSON(), nullable=False),
        sa.Column("evidence_references", sa.JSON(), nullable=False),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("score IS NULL OR (score >= 0 AND score <= 100)", name="ck_lead_score_range"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["enrichment_run_id"], ["enrichment_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_lead_scores_lead_calculated", "lead_scores", ["lead_id", "calculated_at"])
    op.create_index("uq_lead_scores_run_version", "lead_scores", ["enrichment_run_id", "score_version"], unique=True)

    op.create_table(
        "opportunity_evidence_links",
        sa.Column("opportunity_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["evidence_id"], ["evidence.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("opportunity_id", "evidence_id"),
    )
    op.create_index("ix_opportunity_evidence_evidence", "opportunity_evidence_links", ["evidence_id"])
    op.create_table(
        "recommendation_evidence_links",
        sa.Column("recommendation_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["recommendation_id"], ["service_recommendations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["evidence_id"], ["evidence.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("recommendation_id", "evidence_id"),
    )
    op.create_index("ix_recommendation_evidence_evidence", "recommendation_evidence_links", ["evidence_id"])
    op.create_table(
        "recommendation_opportunity_links",
        sa.Column("recommendation_id", sa.Uuid(), nullable=False),
        sa.Column("opportunity_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["recommendation_id"], ["service_recommendations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("recommendation_id", "opportunity_id"),
    )
    op.create_index("ix_recommendation_opportunity_opportunity", "recommendation_opportunity_links", ["opportunity_id"])
    op.create_table(
        "score_evidence_links",
        sa.Column("score_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["score_id"], ["lead_scores.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["evidence_id"], ["evidence.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("score_id", "evidence_id"),
    )
    op.create_index("ix_score_evidence_evidence", "score_evidence_links", ["evidence_id"])


def downgrade():
    op.drop_index("ix_score_evidence_evidence", table_name="score_evidence_links")
    op.drop_table("score_evidence_links")
    op.drop_index("ix_recommendation_opportunity_opportunity", table_name="recommendation_opportunity_links")
    op.drop_table("recommendation_opportunity_links")
    op.drop_index("ix_recommendation_evidence_evidence", table_name="recommendation_evidence_links")
    op.drop_table("recommendation_evidence_links")
    op.drop_index("ix_opportunity_evidence_evidence", table_name="opportunity_evidence_links")
    op.drop_table("opportunity_evidence_links")
    op.drop_index("uq_lead_scores_run_version", table_name="lead_scores")
    op.drop_index("ix_lead_scores_lead_calculated", table_name="lead_scores")
    op.drop_table("lead_scores")
    op.drop_index("uq_service_recommendations_run_service_rule", table_name="service_recommendations")
    op.drop_index("ix_service_recommendations_lead_rank", table_name="service_recommendations")
    op.drop_table("service_recommendations")
    op.drop_index("uq_opportunities_run_identity", table_name="opportunities")
    op.drop_index("ix_opportunities_run_category", table_name="opportunities")
    op.drop_index("ix_opportunities_lead_created", table_name="opportunities")
    op.drop_table("opportunities")
    op.drop_table("service_catalog")

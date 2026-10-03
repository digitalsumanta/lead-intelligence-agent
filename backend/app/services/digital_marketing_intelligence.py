import uuid
from datetime import datetime, UTC
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.models.entities import Lead, CompanyEnrichmentProfile, WebsiteIntelligenceProfile, SeoAuditRun, DigitalMarketingIntelligenceProfile
from app.domain.digital_marketing_intelligence import build_digital_marketing_profile
from app.schemas.digital_marketing_intelligence import DigitalMarketingIntelligenceProfileSchema

async def get_digital_marketing_intelligence(db: AsyncSession, company_id: uuid.UUID) -> DigitalMarketingIntelligenceProfileSchema | None:
    stmt = select(DigitalMarketingIntelligenceProfile).where(DigitalMarketingIntelligenceProfile.company_id == company_id)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    if not profile:
        return None
    return DigitalMarketingIntelligenceProfileSchema.model_validate(profile)

async def refresh_digital_marketing_intelligence(db: AsyncSession, company_id: uuid.UUID) -> DigitalMarketingIntelligenceProfileSchema | None:
    lead = await db.get(Lead, company_id)
    if not lead:
        return None

    # Get associated profiles
    stmt_enrich = select(CompanyEnrichmentProfile).where(CompanyEnrichmentProfile.lead_id == company_id)
    enrich_result = await db.execute(stmt_enrich)
    enrichment = enrich_result.scalar_one_or_none()

    stmt_web = select(WebsiteIntelligenceProfile).where(WebsiteIntelligenceProfile.company_id == company_id)
    web_result = await db.execute(stmt_web)
    website_intel = web_result.scalar_one_or_none()

    # Check if any SEO run exists
    stmt_seo = select(SeoAuditRun.id).where(SeoAuditRun.lead_id == company_id).limit(1)
    seo_result = await db.execute(stmt_seo)
    has_seo_run = seo_result.scalar_one_or_none() is not None

    # Delete existing
    await db.execute(delete(DigitalMarketingIntelligenceProfile).where(DigitalMarketingIntelligenceProfile.company_id == company_id))

    # Build new
    profile = build_digital_marketing_profile(lead, enrichment, website_intel, has_seo_run)
    db.add(profile)
    await db.commit()

    return DigitalMarketingIntelligenceProfileSchema.model_validate(profile)

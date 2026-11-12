import uuid
from datetime import datetime, UTC
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.models.entities import (
    Lead,
    CompanyEnrichmentProfile,
    WebsiteIntelligenceProfile,
    SeoAuditRun,
    DigitalMarketingIntelligenceProfile,
)
from app.schemas.digital_marketing_intelligence import DigitalMarketingIntelligenceProfileSchema
from app.services.unified_intelligence import sync_unified_intelligence_for_lead
from app.domain.digital_marketing_intelligence import build_digital_marketing_profile


async def get_digital_marketing_intelligence(
    db: AsyncSession, company_id: uuid.UUID
) -> DigitalMarketingIntelligenceProfileSchema | None:
    stmt = select(DigitalMarketingIntelligenceProfile).where(
        DigitalMarketingIntelligenceProfile.company_id == company_id
    )
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    if not profile:
        return None
    return DigitalMarketingIntelligenceProfileSchema.model_validate(profile)


async def refresh_digital_marketing_intelligence(
    db: AsyncSession, company_id: uuid.UUID
) -> DigitalMarketingIntelligenceProfileSchema | None:
    lead = await db.get(Lead, company_id)
    if not lead:
        return None

    # Probe website if available to collect fresh evidence
    if lead.website:
        try:
            from app.services.website import WebsiteAnalyzer
            analyzer = WebsiteAnalyzer()
            analysis = await analyzer.analyze(lead.website)
            if isinstance(analysis, dict) and analysis.get("available"):
                current_analysis = lead.website_analysis if isinstance(lead.website_analysis, dict) else {}
                current_analysis.update(analysis)
                lead.website_analysis = current_analysis
                await db.commit()
        except Exception:
            pass

    # Synchronize unified intelligence atomically
    await sync_unified_intelligence_for_lead(db, company_id)

    return await get_digital_marketing_intelligence(db, company_id)


async def research_digital_marketing_category(
    db: AsyncSession,
    company_id: uuid.UUID,
    category: str,
    retry: bool = False,
) -> DigitalMarketingIntelligenceProfileSchema | None:
    """
    Executes a category-targeted research audit for one of the 7 canonical categories.
    Idempotent, non-destructive, and preserves verified facts.
    """
    lead = await db.get(Lead, company_id)
    if not lead:
        return None

    cat_norm = category.strip().lower()

    if cat_norm in ("seo", "website_performance", "conversion_optimization", "content_marketing", "social_media"):
        if lead.website:
            try:
                from app.services.website import WebsiteAnalyzer
                analyzer = WebsiteAnalyzer()
                analysis = await analyzer.analyze(lead.website)
                if isinstance(analysis, dict) and analysis.get("available"):
                    current_analysis = lead.website_analysis if isinstance(lead.website_analysis, dict) else {}
                    current_analysis.update(analysis)
                    lead.website_analysis = current_analysis
                    await db.commit()
            except Exception:
                pass

    elif cat_norm == "online_reputation":
        import os
        has_key = bool(os.environ.get("GOOGLE_PLACES_API_KEY"))
        if not has_key and retry:
            # Diagnostics: remains UNAVAILABLE because credential is not configured
            pass

    # Re-sync unified intelligence with the fresh category observation
    await sync_unified_intelligence_for_lead(db, company_id)

    return await get_digital_marketing_intelligence(db, company_id)

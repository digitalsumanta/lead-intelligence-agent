import uuid
from datetime import datetime, UTC
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from typing import Optional

from app.models.entities import Lead, CompanyEnrichmentProfile, WebsiteIntelligenceProfile, SourceType, ConfidenceLevel
from app.domain.website_intelligence import (
    normalize_website,
    normalize_domain,
    calculate_website_completeness
)
from app.schemas.website_intelligence import WebsiteIntelligenceProfileSchema

def build_deterministic_website_profile(lead: Lead, enrichment: Optional[CompanyEnrichmentProfile] = None) -> WebsiteIntelligenceProfile:
    now = datetime.now(UTC)
    
    # Extract raw data from Lead or Enrichment
    raw_website = lead.website
    raw_domain = None
    if enrichment:
        raw_website = raw_website or enrichment.website
        raw_domain = enrichment.domain
        
    normalized_web, extracted_domain = normalize_website(raw_website)
    
    if not extracted_domain and raw_domain:
        extracted_domain = normalize_domain(raw_domain)
        
    website_present = bool(normalized_web)
    domain_present = bool(extracted_domain)
    
    # We only populate technology signals if they already exist in the Lead record somehow.
    # Currently, Lead has `website_analysis`, `ai_analysis`, `competitors`.
    technology_signals = None
    content_signals = None
    
    # If there's structured website analysis, we can extract non-AI generated hints
    if isinstance(lead.website_analysis, dict):
        techs = lead.website_analysis.get("technologies")
        if techs:
            technology_signals = {"technologies": techs}
            
        content = lead.website_analysis.get("meta_description")
        if content:
            content_signals = {"meta_description": content}
            
    has_tech = bool(technology_signals)
    has_content = bool(content_signals)
    
    completeness = calculate_website_completeness(website_present, domain_present, has_tech, has_content)
    
    confidence = ConfidenceLevel.HIGH if (website_present or domain_present) else ConfidenceLevel.UNKNOWN
    
    return WebsiteIntelligenceProfile(
        id=uuid.uuid4(),
        company_id=lead.id,
        website=raw_website,
        normalized_website=normalized_web,
        domain=raw_domain or extracted_domain,
        normalized_domain=extracted_domain,
        website_present=website_present,
        domain_present=domain_present,
        technology_signals=technology_signals,
        content_signals=content_signals,
        source_type=SourceType.LEAD_RECORD,
        confidence=confidence,
        completeness=completeness,
        created_at=now,
        updated_at=now
    )

async def get_website_intelligence(db: AsyncSession, company_id: uuid.UUID) -> WebsiteIntelligenceProfileSchema | None:
    stmt = select(WebsiteIntelligenceProfile).where(WebsiteIntelligenceProfile.company_id == company_id)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    if not profile:
        return None
    return WebsiteIntelligenceProfileSchema.model_validate(profile)

async def refresh_website_intelligence(db: AsyncSession, company_id: uuid.UUID) -> WebsiteIntelligenceProfileSchema | None:
    lead = await db.get(Lead, company_id)
    if not lead:
        return None
        
    # Get company enrichment if available
    stmt_enrich = select(CompanyEnrichmentProfile).where(CompanyEnrichmentProfile.lead_id == company_id)
    result_enrich = await db.execute(stmt_enrich)
    enrichment = result_enrich.scalar_one_or_none()
        
    # Delete existing
    await db.execute(delete(WebsiteIntelligenceProfile).where(WebsiteIntelligenceProfile.company_id == company_id))
    
    # Build new
    profile = build_deterministic_website_profile(lead, enrichment)
    db.add(profile)
    await db.commit()
    
    # Return schema
    return WebsiteIntelligenceProfileSchema.model_validate(profile)

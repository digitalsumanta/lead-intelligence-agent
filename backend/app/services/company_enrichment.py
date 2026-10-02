import uuid
from datetime import datetime, UTC
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.models.entities import Lead, CompanyEnrichmentProfile, SourceType, ConfidenceLevel
from app.domain.company_enrichment import (
    normalize_company_name,
    normalize_website,
    extract_email_domain,
    normalize_location,
    calculate_company_completeness,
    _normalize_industry,
    _normalize_company_size
)
from app.schemas.company_enrichment import CompanyEnrichmentProfileSchema

def build_deterministic_company_profile(lead: Lead) -> CompanyEnrichmentProfile:
    now = datetime.now(UTC)
    
    legal_name = normalize_company_name(lead.company_name)
    display_name = legal_name
    
    normalized_website, domain = normalize_website(lead.website)
    email_domain = extract_email_domain(lead.email)
    
    # If no website domain, fallback to email domain
    if not domain and email_domain:
        domain = email_domain
        
    linkedin_url = lead.linkedin_url.strip() if lead.linkedin_url else None
    
    city = lead.city.strip() if lead.city else None
    state = lead.state.strip() if lead.state else None
    country = lead.country.strip() if lead.country else None
    location = normalize_location(city, state, country)
    
    phone = lead.phone.strip() if lead.phone else None
    description = lead.ai_summary.strip() if lead.ai_summary else None
    
    industry_raw = lead.industry.strip() if lead.industry else None
    normalized_industry = _normalize_industry(industry_raw) if industry_raw else None
    
    company_size_raw = None # Not available on Lead natively
    normalized_company_size = None
    
    fields_for_completeness = {
        "legal_name": legal_name,
        "domain": domain,
        "website": normalized_website,
        "linkedin_url": linkedin_url,
        "location": location,
        "description": description,
        "industry": normalized_industry,
        "phone": phone
    }
    
    completeness = calculate_company_completeness(fields_for_completeness)
    
    # Determine overall confidence
    # Since we are deriving this strictly from the persisted LEAD_RECORD, we use HIGH confidence, 
    # except if the data is mostly missing.
    confidence = ConfidenceLevel.HIGH if completeness > 30 else ConfidenceLevel.MEDIUM
    
    return CompanyEnrichmentProfile(
        id=uuid.uuid4(),
        lead_id=lead.id,
        legal_name=legal_name,
        display_name=display_name,
        domain=domain,
        website=normalized_website,
        email_domain=email_domain,
        linkedin_url=linkedin_url,
        phone=phone,
        location=location,
        city=city,
        state=state,
        country=country,
        description=description,
        industry=industry_raw,
        normalized_industry=normalized_industry,
        company_size=company_size_raw,
        normalized_company_size=normalized_company_size,
        source=SourceType.LEAD_RECORD,
        confidence=confidence,
        completeness_percent=completeness,
        created_at=now,
        updated_at=now
    )

async def get_company_enrichment(db: AsyncSession, lead_id: uuid.UUID) -> CompanyEnrichmentProfileSchema | None:
    stmt = select(CompanyEnrichmentProfile).where(CompanyEnrichmentProfile.lead_id == lead_id)
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()
    if not profile:
        return None
    return CompanyEnrichmentProfileSchema.model_validate(profile)

async def refresh_company_enrichment(db: AsyncSession, lead_id: uuid.UUID) -> CompanyEnrichmentProfileSchema | None:
    lead = await db.get(Lead, lead_id)
    if not lead:
        return None
        
    # Delete existing
    await db.execute(delete(CompanyEnrichmentProfile).where(CompanyEnrichmentProfile.lead_id == lead_id))
    
    # Build new
    profile = build_deterministic_company_profile(lead)
    db.add(profile)
    await db.commit()
    
    # Return schema
    return CompanyEnrichmentProfileSchema.model_validate(profile)

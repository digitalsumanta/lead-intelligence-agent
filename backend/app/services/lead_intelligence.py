import uuid
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.models.entities import (
    Lead,
    LeadIntelligenceProfile,
    LeadIntelligencePainPoint,
    LeadIntelligenceOpportunity
)
from app.domain.lead_intelligence import build_deterministic_profile
from app.schemas.lead_intelligence import (
    LeadIntelligenceProfileSchema,
    IntelligenceCompanyProfile,
    IntelligenceContactProfile,
    IntelligenceDigitalPresence,
    IntelligenceBusinessContext,
    IntelligenceQuality,
    IntelligencePainPoint,
    IntelligenceOpportunity
)

def _to_schema(
    lead: Lead,
    profile: LeadIntelligenceProfile,
    pains: list[LeadIntelligencePainPoint],
    opps: list[LeadIntelligenceOpportunity]
) -> LeadIntelligenceProfileSchema:
    return LeadIntelligenceProfileSchema(
        lead_id=lead.id,
        company=IntelligenceCompanyProfile(
            name=lead.company_name,
            legal_name=None,
            website=profile.website,
            industry=profile.industry,
            industry_source=profile.industry_source,
            company_size=profile.company_size,
            company_size_source=profile.company_size_source,
            location=profile.location,
            country=profile.country,
            description=profile.description,
            products_services=lead.recommended_services if isinstance(lead.recommended_services, list) else []
        ),
        contact=IntelligenceContactProfile(
            name=lead.decision_maker,
            job_title=None,
            email=lead.email,
            phone=lead.phone,
            location=profile.location
        ),
        digital_presence=IntelligenceDigitalPresence(
            website=profile.website,
            linkedin_url=lead.linkedin_url,
            facebook_url=None,
            instagram_url=None,
            youtube_url=None,
            other_urls=[]
        ),
        business_context=IntelligenceBusinessContext(
            pain_points=[
                IntelligencePainPoint.model_validate(p) for p in pains
            ],
            opportunities=[
                IntelligenceOpportunity.model_validate(o) for o in opps
            ],
            known_services=lead.recommended_services if isinstance(lead.recommended_services, list) else [],
            known_products=[]
        ),
        intelligence_quality=IntelligenceQuality(
            completeness_score=float(profile.completeness_score) if profile.completeness_score is not None else 0.0,
            last_researched_at=profile.last_researched_at,
            field_count=12,  # based on deterministic calculate_completeness
            populated_field_count=int(float(profile.completeness_score) * 12 / 100) if profile.completeness_score else 0
        )
    )

async def get_lead_intelligence(session: AsyncSession, lead_id: uuid.UUID) -> Optional[LeadIntelligenceProfileSchema]:
    lead = await session.get(Lead, lead_id)
    if not lead:
        return None

    result = await session.execute(
        select(LeadIntelligenceProfile).where(LeadIntelligenceProfile.lead_id == lead_id)
    )
    profile = result.scalar_one_or_none()

    if not profile:
        # Generate an empty/unknown profile deterministically if none exists
        profile, pains, opps = build_deterministic_profile(lead)
        # We don't save it on GET, we just return it
        return _to_schema(lead, profile, pains, opps)

    # Fetch related
    pains = (await session.scalars(
        select(LeadIntelligencePainPoint).where(LeadIntelligencePainPoint.lead_id == lead_id)
    )).all()

    opps = (await session.scalars(
        select(LeadIntelligenceOpportunity).where(LeadIntelligenceOpportunity.lead_id == lead_id)
    )).all()

    return _to_schema(lead, profile, list(pains), list(opps))

async def refresh_lead_intelligence(session: AsyncSession, lead_id: uuid.UUID) -> Optional[LeadIntelligenceProfileSchema]:
    lead = await session.get(Lead, lead_id)
    if not lead:
        return None

    # Clear old intelligence
    await session.execute(delete(LeadIntelligenceOpportunity).where(LeadIntelligenceOpportunity.lead_id == lead_id))
    await session.execute(delete(LeadIntelligencePainPoint).where(LeadIntelligencePainPoint.lead_id == lead_id))
    await session.execute(delete(LeadIntelligenceProfile).where(LeadIntelligenceProfile.lead_id == lead_id))

    # Build new
    profile, pains, opps = build_deterministic_profile(lead)

    session.add(profile)
    for p in pains: session.add(p)
    for o in opps: session.add(o)

    await session.commit()

    return _to_schema(lead, profile, pains, opps)

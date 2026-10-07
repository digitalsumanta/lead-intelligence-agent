import uuid
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime, timezone

from app.models.entities import Lead, ContactIntelligenceProfile, ConfidenceLevel, SourceType
from app.domain.contact_intelligence import build_contact_intelligence

async def get_contact_intelligence(session: AsyncSession, contact_id: uuid.UUID) -> Optional[ContactIntelligenceProfile]:
    result = await session.execute(select(ContactIntelligenceProfile).where(ContactIntelligenceProfile.contact_id == contact_id))
    return result.scalar_one_or_none()

async def refresh_contact_intelligence(session: AsyncSession, contact_id: uuid.UUID) -> Optional[ContactIntelligenceProfile]:
    # In our architecture, the Lead acts as the Contact entity.
    lead = await session.get(Lead, contact_id)
    if not lead:
        return None
        
    intel_data = build_contact_intelligence(lead)
    
    profile = await get_contact_intelligence(session, contact_id)
    now = datetime.now(timezone.utc)
    
    if profile:
        profile.department = intel_data["department"]
        profile.functional_area = intel_data["functional_area"]
        profile.seniority_level = intel_data["seniority_level"]
        profile.decision_maker_score = intel_data["decision_maker_score"]
        profile.buying_influence = intel_data["buying_influence"]
        profile.evidence = intel_data["evidence"]
        profile.evaluated_at = now
        profile.confidence = intel_data["confidence"]
        profile.source_type = SourceType.SYSTEM_DERIVED
    else:
        profile = ContactIntelligenceProfile(
            contact_id=contact_id,
            department=intel_data["department"],
            functional_area=intel_data["functional_area"],
            seniority_level=intel_data["seniority_level"],
            decision_maker_score=intel_data["decision_maker_score"],
            buying_influence=intel_data["buying_influence"],
            evidence=intel_data["evidence"],
            confidence=intel_data["confidence"],
            source_type=SourceType.SYSTEM_DERIVED,
            evaluated_at=now
        )
        session.add(profile)
        
    await session.commit()
    await session.refresh(profile)
    return profile

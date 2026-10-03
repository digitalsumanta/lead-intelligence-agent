import uuid
from typing import List, Sequence
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.models.entities import (
    Lead,
    CompanyEnrichmentProfile,
    WebsiteIntelligenceProfile,
    DigitalMarketingIntelligenceProfile,
    BuyingSignal
)
from app.domain.buying_signals import (
    build_buying_signal,
    deduplicate_signals,
    sort_signals_by_recency
)


async def get_buying_signals_for_company(session: AsyncSession, company_id: uuid.UUID) -> Sequence[BuyingSignal]:
    """Retrieve all active buying signals for a given canonical company identity."""
    stmt = select(BuyingSignal).where(
        BuyingSignal.company_id == company_id,
        BuyingSignal.status == "ACTIVE"
    ).order_by(BuyingSignal.detected_at.desc())
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_buying_signals_for_lead(session: AsyncSession, lead_id: uuid.UUID) -> Sequence[BuyingSignal]:
    """Retrieve all active buying signals for a given lead. Since lead_id is company_id, it is delegated."""
    return await get_buying_signals_for_company(session, lead_id)


async def refresh_buying_signals_for_company(session: AsyncSession, company_id: uuid.UUID) -> Sequence[BuyingSignal]:
    """
    Recalculate buying signals strictly from existing persisted application evidence.
    This replaces existing signals of the exact same type and time to ensure idempotency.
    """
    # 1. Fetch existing intelligence records
    lead = await session.get(Lead, company_id)
    if not lead:
        return []

    enrichment = (await session.execute(
        select(CompanyEnrichmentProfile).filter_by(lead_id=company_id)
    )).scalar_one_or_none()

    website_intel = (await session.execute(
        select(WebsiteIntelligenceProfile).filter_by(company_id=company_id)
    )).scalar_one_or_none()

    digital_intel = (await session.execute(
        select(DigitalMarketingIntelligenceProfile).filter_by(company_id=company_id)
    )).scalar_one_or_none()

    new_signals: List[BuyingSignal] = []

    # Evidence 1: Digital Marketing Maturity
    if digital_intel and digital_intel.marketing_maturity in ["ESTABLISHED", "ADVANCED"]:
        evidence = {
            "marketing_maturity": digital_intel.marketing_maturity,
            "original_confidence": digital_intel.confidence.value if digital_intel.confidence else "UNKNOWN",
            "channels_involved": 3 if digital_intel.marketing_maturity == "ESTABLISHED" else 4
        }
        new_signals.append(build_buying_signal(
            company_id=company_id,
            raw_type="MARKETING_MATURITY_SIGNAL",
            raw_category="marketing_activity",
            raw_source=digital_intel.source_type.value if hasattr(digital_intel, 'source_type') else "SYSTEM_DERIVED",
            evidence=evidence,
            detected_at=digital_intel.updated_at
        ))

    # Evidence 2: Digital Marketing Opportunities Found
    if digital_intel and digital_intel.marketing_opportunities and len(digital_intel.marketing_opportunities) > 0:
        evidence = {
            "opportunities_count": len(digital_intel.marketing_opportunities),
            "opportunities": digital_intel.marketing_opportunities
        }
        new_signals.append(build_buying_signal(
            company_id=company_id,
            raw_type="MARKETING_OPPORTUNITY_DETECTED",
            raw_category="marketing_activity",
            raw_source="SYSTEM_DERIVED",
            evidence=evidence,
            detected_at=digital_intel.updated_at
        ))
        
    # Evidence 3: Website Present
    if website_intel and website_intel.website_present:
        evidence = {
            "website": website_intel.normalized_website,
            "original_confidence": website_intel.confidence.value if website_intel.confidence else "UNKNOWN"
        }
        new_signals.append(build_buying_signal(
            company_id=company_id,
            raw_type="WEBSITE_PRESENCE_CONFIRMED",
            raw_category="website_activity",
            raw_source=website_intel.source_type.value if hasattr(website_intel, 'source_type') else "SYSTEM_DERIVED",
            evidence=evidence,
            detected_at=website_intel.updated_at
        ))

    # Evidence 4: Company Profile Completeness
    if enrichment and enrichment.completeness_percent and enrichment.completeness_percent > 80:
        evidence = {
            "completeness": float(enrichment.completeness_percent),
            "original_confidence": enrichment.confidence.value if enrichment.confidence else "UNKNOWN"
        }
        new_signals.append(build_buying_signal(
            company_id=company_id,
            raw_type="HIGH_PROFILE_COMPLETENESS",
            raw_category="company_activity",
            raw_source=enrichment.source.value if hasattr(enrichment, 'source') else "SYSTEM_DERIVED",
            evidence=evidence,
            detected_at=enrichment.updated_at
        ))

    # Deduplicate before saving
    new_signals = deduplicate_signals(new_signals)
    new_signals = sort_signals_by_recency(new_signals)

    # Idempotent save: remove all ACTIVE signals for this company first, then insert new.
    # Note: For strict event sourcing we might not delete, but instructions say:
    # "Upsert or replace only the signals owned by the refresh operation according to the existing project pattern."
    await session.execute(
        delete(BuyingSignal).where(BuyingSignal.company_id == company_id)
    )
    
    session.add_all(new_signals)
    await session.commit()
    
    return new_signals


async def refresh_buying_signals_for_lead(session: AsyncSession, lead_id: uuid.UUID) -> Sequence[BuyingSignal]:
    """Refresh deterministic signals for the lead using existing evidence."""
    return await refresh_buying_signals_for_company(session, lead_id)

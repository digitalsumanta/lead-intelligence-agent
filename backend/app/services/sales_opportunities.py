import uuid
from typing import List, Sequence
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.models.entities import Lead, BuyingSignal, LeadIntelligenceOpportunity, SourceType
from app.domain.sales_opportunities import derive_opportunity_from_signals

async def get_sales_opportunities_for_lead(session: AsyncSession, lead_id: uuid.UUID) -> Sequence[LeadIntelligenceOpportunity]:
    """Retrieve all sales opportunities for a lead (filtered for those derived from buying signals)."""
    stmt = select(LeadIntelligenceOpportunity).where(
        LeadIntelligenceOpportunity.lead_id == lead_id,
        LeadIntelligenceOpportunity.source_reference.like("buying_signals.%")
    ).order_by(LeadIntelligenceOpportunity.score.desc())
    result = await session.execute(stmt)
    return result.scalars().all()

async def refresh_sales_opportunities_for_lead(session: AsyncSession, lead_id: uuid.UUID) -> Sequence[LeadIntelligenceOpportunity]:
    """
    Refresh sales opportunities based purely on persisted Buying Signals.
    Idempotent: removes existing derived sales opportunities and recreates them.
    """
    lead = await session.get(Lead, lead_id)
    if not lead:
        return []

    # Get active buying signals
    signals_stmt = select(BuyingSignal).where(
        BuyingSignal.company_id == lead_id,
        BuyingSignal.status == "ACTIVE"
    )
    signals_result = await session.execute(signals_stmt)
    signals = signals_result.scalars().all()

    # Derive new opportunities
    new_opportunities = derive_opportunity_from_signals(lead_id, list(signals))

    # Delete only the opportunities that were derived from buying signals
    await session.execute(
        delete(LeadIntelligenceOpportunity).where(
            LeadIntelligenceOpportunity.lead_id == lead_id,
            LeadIntelligenceOpportunity.source_reference.like("buying_signals.%")
        )
    )

    session.add_all(new_opportunities)
    await session.commit()

    return new_opportunities

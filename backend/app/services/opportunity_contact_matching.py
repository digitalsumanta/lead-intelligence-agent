import uuid
from typing import Optional, Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import or_, and_, func

from app.models.entities import Lead, LeadIntelligenceOpportunity, ContactIntelligenceProfile
from app.domain.opportunity_contact_matching import rank_opportunity_contacts

async def get_matched_contacts_for_opportunity(
    session: AsyncSession,
    opportunity_id: uuid.UUID,
    limit: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """
    Finds and deterministically ranks all contacts belonging to the same company
    as the requested Sales Opportunity.
    """
    # 1. Retrieve the Sales Opportunity
    opp_stmt = select(LeadIntelligenceOpportunity).where(LeadIntelligenceOpportunity.id == opportunity_id)
    opp_res = await session.execute(opp_stmt)
    opportunity = opp_res.scalar_one_or_none()
    if not opportunity:
        return None

    # 2. Retrieve the associated company lead
    company_lead = await session.get(Lead, opportunity.lead_id)
    if not company_lead:
        return {
            "opportunity_id": opportunity_id,
            "contacts": []
        }

    # 3. Handle missing or ambiguous company name
    raw_comp_name = (company_lead.company_name or "").strip()
    if not raw_comp_name:
        # Fallback to only the single known lead record if company_name is absent
        candidates_filter = (Lead.id == company_lead.id)
    else:
        # Retrieve all candidate contacts belonging to the same company
        candidates_filter = or_(
            Lead.id == company_lead.id,
            func.lower(Lead.company_name) == func.lower(raw_comp_name)
        )

    stmt = (
        select(Lead, ContactIntelligenceProfile)
        .outerjoin(ContactIntelligenceProfile, ContactIntelligenceProfile.contact_id == Lead.id)
        .where(candidates_filter)
    )
    res = await session.execute(stmt)
    candidates = res.all()  # Sequence of (Lead, ContactIntelligenceProfile | None)

    # 4. Rank candidates deterministically
    ranked = rank_opportunity_contacts(opportunity, candidates, limit=limit)

    return {
        "opportunity_id": opportunity.id,
        "contacts": ranked
    }

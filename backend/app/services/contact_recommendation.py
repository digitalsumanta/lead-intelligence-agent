import uuid
from typing import Optional, Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.entities import Lead, LeadIntelligenceOpportunity, ContactIntelligenceProfile, SalesTask, SalesTaskStatus
from app.services.opportunity_contact_matching import get_matched_contacts_for_opportunity
from app.domain.contact_recommendation import evaluate_contact_recommendation

async def get_contact_recommendation_for_opportunity(
    session: AsyncSession,
    opportunity_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    """
    Computes a deterministic contact recommendation and recommended sales action
    for a given Sales Opportunity based purely on existing intelligence.
    Read-only: does not persist or mutate state.
    """
    # 1. Retrieve the Sales Opportunity
    opp_stmt = select(LeadIntelligenceOpportunity).where(LeadIntelligenceOpportunity.id == opportunity_id)
    opp_res = await session.execute(opp_stmt)
    opportunity = opp_res.scalar_one_or_none()
    if not opportunity:
        return None

    # 2. Retrieve matched contacts from 12H-D matching service
    matched_data = await get_matched_contacts_for_opportunity(session, opportunity_id)
    if matched_data is None:
        return None

    matched_contacts = matched_data.get("contacts", [])

    # 3. Check for active/previous sales tasks for follow-up detection
    task_stmt = select(SalesTask).where(
        SalesTask.agent_source_id == opportunity_id,
        SalesTask.status.in_([SalesTaskStatus.OPEN, SalesTaskStatus.IN_PROGRESS])
    )
    task_res = await session.execute(task_stmt)
    has_previous_task = task_res.scalar_one_or_none() is not None

    # 4. Load candidate Lead and ContactIntelligenceProfile objects for rich evaluation
    contact_uuids = [
        uuid.UUID(str(c["contact_id"])) for c in matched_contacts if c.get("contact_id")
    ]

    leads_map: Dict[uuid.UUID, Any] = {}
    profiles_map: Dict[uuid.UUID, Any] = {}

    if contact_uuids:
        lead_stmt = select(Lead).where(Lead.id.in_(contact_uuids))
        lead_res = await session.execute(lead_stmt)
        leads_map = {l.id: l for l in lead_res.scalars().all()}

        prof_stmt = select(ContactIntelligenceProfile).where(ContactIntelligenceProfile.contact_id.in_(contact_uuids))
        prof_res = await session.execute(prof_stmt)
        profiles_map = {p.contact_id: p for p in prof_res.scalars().all()}

    # 5. Evaluate deterministic recommendation
    return evaluate_contact_recommendation(
        opportunity=opportunity,
        ranked_matched_contacts=matched_contacts,
        candidate_leads_map=leads_map,
        candidate_profiles_map=profiles_map,
        has_previous_task=has_previous_task
    )

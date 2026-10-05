import uuid
from typing import List, Sequence
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.models.entities import Lead, BuyingSignal, LeadIntelligenceOpportunity, SourceType
from app.domain.sales_opportunities import derive_opportunity_from_signals

async def get_sales_opportunities_for_lead(session: AsyncSession, lead_id: uuid.UUID) -> List[dict]:
    """Retrieve all sales opportunities for a lead and attach their tasks."""
    stmt = select(LeadIntelligenceOpportunity).where(
        LeadIntelligenceOpportunity.lead_id == lead_id,
        LeadIntelligenceOpportunity.source_reference.like("buying_signals.%")
    ).order_by(LeadIntelligenceOpportunity.score.desc())
    result = await session.execute(stmt)
    opps = result.scalars().all()

    # Load matching tasks
    from app.models.entities import SalesTask
    task_stmt = select(SalesTask).where(
        SalesTask.lead_id == lead_id,
        SalesTask.agent_source_type == 'SALES_OPPORTUNITY'
    )
    task_result = await session.execute(task_stmt)
    tasks_by_opp = {t.agent_source_id: t for t in task_result.scalars().all() if t.agent_source_id}

    response = []
    for opp in opps:
        opp_dict = {
            "id": opp.id,
            "title": opp.title,
            "description": opp.description,
            "opportunity_type": opp.opportunity_type,
            "score": opp.score,
            "priority": opp.priority,
            "recommended_action": opp.recommended_action,
            "evidence": opp.evidence,
            "source_type": opp.source_type,
            "source_reference": opp.source_reference,
            "confidence": opp.confidence,
            "created_at": opp.created_at,
            "updated_at": opp.updated_at,
        }
        task = tasks_by_opp.get(opp.id)
        if task:
            opp_dict["related_task_id"] = task.id
            opp_dict["related_task_status"] = task.status.value
        response.append(opp_dict)

    return response

async def refresh_sales_opportunities_for_lead(session: AsyncSession, lead_id: uuid.UUID) -> List[dict]:
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

    from app.services.sales_action_planning import synchronize_opportunity_tasks
    await synchronize_opportunity_tasks(session, lead_id)

    return await get_sales_opportunities_for_lead(session, lead_id)

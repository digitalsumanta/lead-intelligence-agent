import uuid
from typing import List, Sequence
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, or_

from app.models.entities import (
    Lead,
    BuyingSignal,
    LeadIntelligenceOpportunity,
    DigitalMarketingIntelligenceProfile,
    SourceType,
)
from app.domain.sales_opportunities import derive_opportunity_from_signals
from app.domain.digital_marketing_intelligence import derive_opportunities_from_seven_categories


async def get_sales_opportunities_for_lead(session: AsyncSession, lead_id: uuid.UUID) -> List[dict]:
    """Retrieve all sales opportunities for a lead (buying signals + 7-category digital marketing) and attach their tasks."""
    stmt = select(LeadIntelligenceOpportunity).where(
        LeadIntelligenceOpportunity.lead_id == lead_id,
        or_(
            LeadIntelligenceOpportunity.source_reference.like("buying_signals.%"),
            LeadIntelligenceOpportunity.source_reference.like("digital_marketing.%"),
        )
    ).order_by(LeadIntelligenceOpportunity.score.desc())
    result = await session.execute(stmt)
    opps = list(result.scalars().all())

    # If no buying-signal / digital-marketing opportunities exist yet, check if active signals exist and auto-derive
    if not opps:
        signals_stmt = select(BuyingSignal).where(
            BuyingSignal.company_id == lead_id,
            BuyingSignal.status == "ACTIVE"
        )
        signals_result = await session.execute(signals_stmt)
        signals = list(signals_result.scalars().all())

        new_opps: List[LeadIntelligenceOpportunity] = []
        if signals:
            derived = derive_opportunity_from_signals(lead_id, signals)
            new_opps.extend(derived)

        # Also check digital marketing profile
        stmt_dm = select(DigitalMarketingIntelligenceProfile).where(DigitalMarketingIntelligenceProfile.company_id == lead_id)
        dm_res = await session.execute(stmt_dm)
        dm_prof = dm_res.scalar_one_or_none()
        if dm_prof and dm_prof.seven_category_coverage:
            dm_opps = derive_opportunities_from_seven_categories(lead_id, dm_prof.seven_category_coverage)
            new_opps.extend(dm_opps)

        if new_opps:
            session.add_all(new_opps)
            await session.commit()
            res = await session.execute(stmt)
            opps = list(res.scalars().all())

            from app.services.sales_action_planning import synchronize_opportunity_tasks
            await synchronize_opportunity_tasks(session, lead_id)

    # Fallback: if still no opps, check general LeadIntelligenceOpportunity records
    if not opps:
        fallback_stmt = select(LeadIntelligenceOpportunity).where(
            LeadIntelligenceOpportunity.lead_id == lead_id
        ).order_by(LeadIntelligenceOpportunity.score.desc())
        fallback_res = await session.execute(fallback_stmt)
        opps = list(fallback_res.scalars().all())

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
        ev = opp.evidence if isinstance(opp.evidence, dict) else {}
        opp_dict = {
            "id": opp.id,
            "title": opp.title,
            "description": opp.description,
            "opportunity_type": opp.opportunity_type,
            "opportunity_category": ev.get("opportunity_category", opp.opportunity_type),
            "score": opp.score,
            "priority": opp.priority,
            "recommended_action": opp.recommended_action,
            "suggested_next_action": ev.get("suggested_next_action", opp.recommended_action),
            "recommended_service": ev.get("recommended_service"),
            "commercial_relevance": ev.get("commercial_relevance", opp.description),
            "status": ev.get("status", "ACTIVE"),
            "opportunity_status": ev.get("opportunity_status", ev.get("status", "ACTIVE")),
            "evidence": opp.evidence,
            "evidence_source": ev.get("evidence_source", str(opp.source_reference)),
            "source_type": opp.source_type,
            "source_reference": opp.source_reference,
            "confidence": opp.confidence,
            "last_evaluated": ev.get("last_evaluated") or (opp.updated_at.isoformat() if opp.updated_at else None),
            "created_at": opp.created_at,
            "updated_at": opp.updated_at,
        }
        task = tasks_by_opp.get(opp.id)
        if task:
            opp_dict["related_task_id"] = task.id
            opp_dict["related_task_status"] = task.status.value if hasattr(task.status, 'value') else str(task.status)
        response.append(opp_dict)

    return response


async def refresh_sales_opportunities_for_lead(session: AsyncSession, lead_id: uuid.UUID) -> List[dict]:
    """
    Refresh sales opportunities based on persisted Buying Signals and 7-Category findings.
    Idempotent: removes existing derived sales opportunities and recreates them.
    Preserves historical evidence and unrelated database records.
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
    signals = list(signals_result.scalars().all())

    # Derive new opportunities from buying signals
    new_opportunities: List[LeadIntelligenceOpportunity] = []
    if signals:
        new_opportunities.extend(derive_opportunity_from_signals(lead_id, signals))

    # Derive opportunities from 7-Category digital marketing coverage
    stmt_dm = select(DigitalMarketingIntelligenceProfile).where(DigitalMarketingIntelligenceProfile.company_id == lead_id)
    dm_res = await session.execute(stmt_dm)
    dm_prof = dm_res.scalar_one_or_none()
    if dm_prof and dm_prof.seven_category_coverage:
        dm_opps = derive_opportunities_from_seven_categories(lead_id, dm_prof.seven_category_coverage)
        new_opportunities.extend(dm_opps)

    # Delete only the opportunities that were derived from buying signals or digital marketing
    await session.execute(
        delete(LeadIntelligenceOpportunity).where(
            LeadIntelligenceOpportunity.lead_id == lead_id,
            or_(
                LeadIntelligenceOpportunity.source_reference.like("buying_signals.%"),
                LeadIntelligenceOpportunity.source_reference.like("digital_marketing.%"),
            )
        )
    )

    session.add_all(new_opportunities)
    await session.commit()

    from app.services.sales_action_planning import synchronize_opportunity_tasks
    await synchronize_opportunity_tasks(session, lead_id)

    return await get_sales_opportunities_for_lead(session, lead_id)

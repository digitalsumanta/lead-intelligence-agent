import uuid
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.entities import (
    Lead,
    LeadIntelligenceOpportunity,
    SalesTask,
    SalesTaskType,
    SalesTaskStatus,
    SalesPriority,
)
from app.domain.contact_recommendation import (
    RecommendationPriority,
    RecommendedSalesAction,
)
from app.services.contact_recommendation import get_contact_recommendation_for_opportunity

ACTION_TO_TASK_TYPE: Dict[RecommendedSalesAction, SalesTaskType] = {
    RecommendedSalesAction.DISCOVERY_CALL: SalesTaskType.CALL,
    RecommendedSalesAction.PERSONALIZED_EMAIL: SalesTaskType.EMAIL,
    RecommendedSalesAction.DIRECT_OUTREACH: SalesTaskType.REVIEW_OUTREACH,
    RecommendedSalesAction.FOLLOW_UP: SalesTaskType.FOLLOW_UP,
    RecommendedSalesAction.RESEARCH_CONTACT: SalesTaskType.OTHER,
}

PRIORITY_TO_SALES_PRIORITY: Dict[RecommendationPriority, SalesPriority] = {
    RecommendationPriority.CRITICAL: SalesPriority.URGENT,
    RecommendationPriority.HIGH: SalesPriority.HIGH,
    RecommendationPriority.MEDIUM: SalesPriority.MEDIUM,
    RecommendationPriority.LOW: SalesPriority.LOW,
    RecommendationPriority.VERY_LOW: SalesPriority.LOW,
}

ACTION_DISPLAY_MAP = {
    RecommendedSalesAction.DISCOVERY_CALL: "Discovery Call",
    RecommendedSalesAction.PERSONALIZED_EMAIL: "Personalized Email",
    RecommendedSalesAction.DIRECT_OUTREACH: "Direct Outreach",
    RecommendedSalesAction.FOLLOW_UP: "Follow Up",
    RecommendedSalesAction.RESEARCH_CONTACT: "Research Contact",
    RecommendedSalesAction.NO_ACTION: "No Action",
}


def build_task_title(action: RecommendedSalesAction, contact_name: str, opp_title: str) -> str:
    action_display = ACTION_DISPLAY_MAP.get(action, action.value.replace("_", " ").title())
    title = f"{action_display}: {contact_name} — {opp_title}"
    return title[:250]


def build_task_description(
    contact_name: str,
    recommendation_score: int,
    match_score: int,
    decision_maker_score: int,
    buying_influence: str,
    recommended_action: str,
    reasons: List[str]
) -> str:
    reasons_formatted = "\n".join(f"- {r}" for r in reasons) if reasons else "- Deterministic recommendation."
    return (
        f"Recommended contact:\n"
        f"{contact_name}\n\n"
        f"Recommendation score:\n"
        f"{recommendation_score}/100\n\n"
        f"Match score:\n"
        f"{match_score}/100\n\n"
        f"Decision-maker score:\n"
        f"{decision_maker_score}/100\n\n"
        f"Buying influence:\n"
        f"{buying_influence}\n\n"
        f"Recommended action:\n"
        f"{recommended_action}\n\n"
        f"Reasons:\n"
        f"{reasons_formatted}"
    )


async def synchronize_recommended_contact_task(
    session: AsyncSession,
    opportunity_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    """
    Connects the Phase 12H-E contact recommendation intelligence to the existing SalesTask system.
    Idempotently creates or reuses an active SalesTask.
    Never executes outreach or triggers external communication.
    """
    # 1. Load the Opportunity
    opp_stmt = select(LeadIntelligenceOpportunity).where(LeadIntelligenceOpportunity.id == opportunity_id)
    opp_res = await session.execute(opp_stmt)
    opportunity = opp_res.scalar_one_or_none()
    if not opportunity:
        return None

    # 2. Compute/read Phase 12H-E recommendation
    rec = await get_contact_recommendation_for_opportunity(session, opportunity_id)
    if not rec or not rec.get("recommended_contact"):
        return {
            "opportunity_id": opportunity_id,
            "contact_id": None,
            "task_id": None,
            "created": False,
            "reused": False,
            "recommendation_score": 0,
            "priority": None,
            "recommended_action": RecommendedSalesAction.NO_ACTION.value,
            "task_type": None,
            "evidence": None
        }

    raw_action = rec.get("recommended_action")
    if isinstance(raw_action, str):
        try:
            recommended_action = RecommendedSalesAction(raw_action)
        except ValueError:
            recommended_action = RecommendedSalesAction.NO_ACTION
    else:
        recommended_action = raw_action or RecommendedSalesAction.NO_ACTION

    if recommended_action == RecommendedSalesAction.NO_ACTION:
        return {
            "opportunity_id": opportunity_id,
            "contact_id": None,
            "task_id": None,
            "created": False,
            "reused": False,
            "recommendation_score": rec.get("recommendation_score", 0),
            "priority": None,
            "recommended_action": RecommendedSalesAction.NO_ACTION.value,
            "task_type": None,
            "evidence": rec.get("evidence")
        }

    # Extract recommendation values
    contact_item = rec["recommended_contact"]
    contact_id = uuid.UUID(str(contact_item["contact_id"]))
    recommendation_score = rec.get("recommendation_score", 0)
    match_score = rec.get("match_score", 0)
    decision_maker_score = rec.get("decision_maker_score", 0)

    raw_bi = rec.get("buying_influence")
    buying_influence_str = raw_bi.value if hasattr(raw_bi, "value") else str(raw_bi or "UNKNOWN")

    raw_prio = rec.get("priority")
    if isinstance(raw_prio, str):
        try:
            rec_priority = RecommendationPriority(raw_prio)
        except ValueError:
            rec_priority = RecommendationPriority.MEDIUM
    else:
        rec_priority = raw_prio or RecommendationPriority.MEDIUM

    # Map task type & priority
    mapped_task_type = ACTION_TO_TASK_TYPE.get(recommended_action, SalesTaskType.OTHER)
    mapped_priority = PRIORITY_TO_SALES_PRIORITY.get(rec_priority, SalesPriority.MEDIUM)

    # 3. Resolve Contact Name
    contact_lead_stmt = select(Lead).where(Lead.id == contact_id)
    contact_lead_res = await session.execute(contact_lead_stmt)
    contact_lead = contact_lead_res.scalar_one_or_none()

    contact_name = (
        contact_lead.decision_maker
        if (contact_lead and contact_lead.decision_maker)
        else (contact_lead.job_title if (contact_lead and contact_lead.job_title) else (contact_lead.company_name if contact_lead else str(contact_id)))
    )

    reasons = rec.get("reasons", [])
    title = build_task_title(recommended_action, contact_name, opportunity.title)
    description = build_task_description(
        contact_name=contact_name,
        recommendation_score=recommendation_score,
        match_score=match_score,
        decision_maker_score=decision_maker_score,
        buying_influence=buying_influence_str,
        recommended_action=recommended_action.value,
        reasons=reasons
    )

    action_identity = f"contact_recommendation:{contact_id}:{recommended_action.value}"
    evidence_refs = [
        f"contact:{contact_id}",
        f"opportunity:{opportunity.id}",
        f"recommendation:{action_identity}"
    ]

    # 4. Check for existing tasks for this lead/opportunity
    existing_tasks_stmt = select(SalesTask).where(
        SalesTask.lead_id == opportunity.lead_id,
        SalesTask.agent_source_type == 'SALES_OPPORTUNITY'
    )
    existing_tasks_res = await session.execute(existing_tasks_stmt)
    existing_tasks = list(existing_tasks_res.scalars().all())

    # Find active task (OPEN or IN_PROGRESS) for this opportunity
    active_task = None
    for t in existing_tasks:
        if t.status in (SalesTaskStatus.OPEN, SalesTaskStatus.IN_PROGRESS):
            if t.agent_source_id == opportunity.id or t.agent_source_action_id == action_identity:
                active_task = t
                break

    if active_task:
        # Reuse existing active task and enrich with contact recommendation
        active_task.task_type = mapped_task_type
        active_task.title = title
        active_task.description = description
        active_task.priority = mapped_priority
        active_task.agent_source_id = opportunity.id
        active_task.agent_source_action_id = action_identity

        current_refs = list(active_task.agent_evidence_refs) if active_task.agent_evidence_refs else []
        for ref in evidence_refs:
            if ref not in current_refs:
                current_refs.append(ref)
        active_task.agent_evidence_refs = current_refs

        await session.commit()
        await session.refresh(active_task)

        return {
            "opportunity_id": opportunity_id,
            "contact_id": contact_id,
            "task_id": active_task.id,
            "created": False,
            "reused": True,
            "recommendation_score": recommendation_score,
            "priority": mapped_priority.value,
            "recommended_action": recommended_action.value,
            "task_type": mapped_task_type.value,
            "evidence": rec.get("evidence")
        }

    # Check if an exact historical task (COMPLETED or CANCELLED) exists for this recommendation
    historical_task = None
    for t in existing_tasks:
        if t.agent_source_id == opportunity.id and t.agent_source_action_id == action_identity:
            historical_task = t
            break

    if historical_task:
        # Exact historical task exists - do not create a duplicate
        return {
            "opportunity_id": opportunity_id,
            "contact_id": contact_id,
            "task_id": historical_task.id,
            "created": False,
            "reused": True,
            "recommendation_score": recommendation_score,
            "priority": mapped_priority.value,
            "recommended_action": recommended_action.value,
            "task_type": mapped_task_type.value,
            "evidence": rec.get("evidence")
        }

    # 5. Create new SalesTask in OPEN status
    new_task = SalesTask(
        lead_id=opportunity.lead_id,
        task_type=mapped_task_type,
        title=title,
        description=description,
        status=SalesTaskStatus.OPEN,
        priority=mapped_priority,
        due_at=datetime.now(timezone.utc),
        created_by_agent=True,
        agent_source_type='SALES_OPPORTUNITY',
        agent_source_id=opportunity.id,
        agent_source_action_id=action_identity,
        agent_evidence_refs=evidence_refs
    )
    session.add(new_task)
    await session.commit()
    await session.refresh(new_task)

    return {
        "opportunity_id": opportunity_id,
        "contact_id": contact_id,
        "task_id": new_task.id,
        "created": True,
        "reused": False,
        "recommendation_score": recommendation_score,
        "priority": mapped_priority.value,
        "recommended_action": recommended_action.value,
        "task_type": mapped_task_type.value,
        "evidence": rec.get("evidence")
    }

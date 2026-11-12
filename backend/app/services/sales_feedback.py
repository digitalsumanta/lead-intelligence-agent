import uuid
from typing import Optional, Dict, Any, List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.entities import (
    Lead,
    LeadIntelligenceOpportunity,
    SalesTask,
    SalesTaskStatus,
    SalesActivity,
    SalesActivityOutcome,
)
from app.domain.sales_feedback import (
    FeedbackSentiment,
    AttributionLevel,
    classify_outcome_sentiment,
    calculate_feedback_rates,
)
from app.schemas.sales_feedback import (
    SalesFeedbackItem,
    ActionPerformanceSummary,
    SalesFeedbackSummaryResponse,
)


def extract_task_attribution(
    task: SalesTask,
    activity: Optional[SalesActivity]
) -> Tuple[Optional[uuid.UUID], Optional[uuid.UUID], Optional[str], AttributionLevel]:
    """
    Deterministically parses causal attribution metadata from SalesTask and SalesActivity.
    """
    opportunity_id: Optional[uuid.UUID] = None
    contact_id: Optional[uuid.UUID] = None
    recommended_action: Optional[str] = None

    # 1. Check direct agent metadata
    if task.agent_source_type == "SALES_OPPORTUNITY" and task.agent_source_id:
        opportunity_id = task.agent_source_id

    if task.agent_source_action_id and task.agent_source_action_id.startswith("contact_recommendation:"):
        parts = task.agent_source_action_id.split(":")
        if len(parts) >= 3:
            try:
                contact_id = uuid.UUID(parts[1])
            except (ValueError, TypeError):
                pass
            recommended_action = parts[2]

    # 2. Check evidence refs fallback
    if task.agent_evidence_refs:
        for ref in task.agent_evidence_refs:
            if not opportunity_id and ref.startswith("opportunity:"):
                try:
                    opportunity_id = uuid.UUID(ref.split("opportunity:")[1])
                except (ValueError, TypeError):
                    pass
            if not contact_id and ref.startswith("contact:"):
                try:
                    contact_id = uuid.UUID(ref.split("contact:")[1])
                except (ValueError, TypeError):
                    pass
            if not recommended_action and ref.startswith("recommendation:contact_recommendation:"):
                rem = ref.split("recommendation:contact_recommendation:")[1]
                parts = rem.split(":")
                if len(parts) >= 2:
                    recommended_action = parts[1]

    # In this architecture, task.lead_id is the primary contact entity
    if not contact_id and task.lead_id:
        contact_id = task.lead_id

    outcome = activity.outcome if activity else None
    has_outcome = outcome is not None

    if opportunity_id and contact_id and recommended_action and has_outcome and task.related_activity_id:
        level = AttributionLevel.FULL
    elif (opportunity_id or contact_id or recommended_action or task.related_activity_id) and has_outcome:
        level = AttributionLevel.PARTIAL
    else:
        level = AttributionLevel.UNATTRIBUTED

    return opportunity_id, contact_id, recommended_action, level


def build_feedback_summary(
    items: List[SalesFeedbackItem],
    scope: str,
    scope_id: Optional[str] = None
) -> SalesFeedbackSummaryResponse:
    """
    Constructs deterministic summary metrics, action breakdown, and rate calculations.
    """
    total_executions = len(items)
    positive_count = sum(1 for item in items if item.sentiment == FeedbackSentiment.POSITIVE)
    negative_count = sum(1 for item in items if item.sentiment == FeedbackSentiment.NEGATIVE)
    neutral_count = sum(1 for item in items if item.sentiment == FeedbackSentiment.NEUTRAL)
    unknown_count = sum(1 for item in items if item.sentiment == FeedbackSentiment.UNKNOWN)

    rates = calculate_feedback_rates(total_executions, positive_count, negative_count, neutral_count)

    outcome_dist: Dict[str, int] = {}
    for item in items:
        if item.outcome:
            outcome_dist[item.outcome] = outcome_dist.get(item.outcome, 0) + 1

    action_map: Dict[str, Dict[str, int]] = {}
    for item in items:
        action_name = item.recommended_action or (item.task_type or "OTHER")
        if action_name not in action_map:
            action_map[action_name] = {
                "total": 0, "positive": 0, "negative": 0, "neutral": 0, "unknown": 0
            }
        action_map[action_name]["total"] += 1
        if item.sentiment == FeedbackSentiment.POSITIVE:
            action_map[action_name]["positive"] += 1
        elif item.sentiment == FeedbackSentiment.NEGATIVE:
            action_map[action_name]["negative"] += 1
        elif item.sentiment == FeedbackSentiment.NEUTRAL:
            action_map[action_name]["neutral"] += 1
        else:
            action_map[action_name]["unknown"] += 1

    action_performance: Dict[str, ActionPerformanceSummary] = {}
    for act_name, stats in sorted(action_map.items()):
        act_rates = calculate_feedback_rates(
            stats["total"], stats["positive"], stats["negative"], stats["neutral"]
        )
        act_verified = stats["positive"] + stats["negative"] + stats["neutral"]
        act_verified_rate = round((stats["positive"] / act_verified) * 100.0, 2) if act_verified > 0 else None
        action_performance[act_name] = ActionPerformanceSummary(
            action=act_name,
            total_executions=stats["total"],
            positive_count=stats["positive"],
            negative_count=stats["negative"],
            neutral_count=stats["neutral"],
            unknown_count=stats["unknown"],
            positive_rate=act_rates["positive_rate"],
            negative_rate=act_rates["negative_rate"],
            neutral_rate=act_rates["neutral_rate"],
            verified_executions=act_verified,
            verified_positive_rate=act_verified_rate,
        )

    attribution_summary = {
        AttributionLevel.FULL.value: sum(1 for i in items if i.attribution_level == AttributionLevel.FULL),
        AttributionLevel.PARTIAL.value: sum(1 for i in items if i.attribution_level == AttributionLevel.PARTIAL),
        AttributionLevel.UNATTRIBUTED.value: sum(1 for i in items if i.attribution_level == AttributionLevel.UNATTRIBUTED),
    }

    verified_executions = positive_count + negative_count + neutral_count
    verified_positive_rate = round((positive_count / verified_executions) * 100.0, 2) if verified_executions > 0 else None

    return SalesFeedbackSummaryResponse(
        scope=scope,
        scope_id=scope_id,
        total_executions=total_executions,
        positive_count=positive_count,
        negative_count=negative_count,
        neutral_count=neutral_count,
        unknown_count=unknown_count,
        positive_rate=rates["positive_rate"],
        negative_rate=rates["negative_rate"],
        neutral_rate=rates["neutral_rate"],
        verified_executions=verified_executions,
        verified_positive_rate=verified_positive_rate,
        outcome_distribution=outcome_dist,
        action_performance=action_performance,
        attribution_summary=attribution_summary,
        items=items,
    )


async def _load_feedback_items(
    session: AsyncSession,
    tasks: List[SalesTask]
) -> List[SalesFeedbackItem]:
    """Helper to load activities and produce structured feedback items with provenance deduplication."""
    if not tasks:
        return []

    activity_ids = [t.related_activity_id for t in tasks if t.related_activity_id is not None]
    activities_map: Dict[uuid.UUID, SalesActivity] = {}
    if activity_ids:
        act_stmt = select(SalesActivity).where(SalesActivity.id.in_(activity_ids))
        act_res = await session.execute(act_stmt)
        for act in act_res.scalars().all():
            activities_map[act.id] = act

    items: List[SalesFeedbackItem] = []
    seen_provenance_keys: set = set()

    for task in tasks:
        # Exclude non-completed tasks from outcome feedback
        if task.status != SalesTaskStatus.COMPLETED:
            continue

        # Prevent duplicate records from inflating feedback when multiple tasks
        # point to the same activity or represent duplicate task records.
        if task.related_activity_id is not None:
            prov_key = ("activity", task.related_activity_id)
        else:
            prov_key = ("task", task.id)

        if prov_key in seen_provenance_keys:
            continue
        seen_provenance_keys.add(prov_key)

        activity = activities_map.get(task.related_activity_id) if task.related_activity_id else None
        opp_id, contact_id, rec_action, attr_level = extract_task_attribution(task, activity)

        outcome_val = None
        if activity and activity.outcome:
            outcome_val = activity.outcome.value if hasattr(activity.outcome, "value") else str(activity.outcome)
        elif task.agent_evidence_refs:
            for ref in task.agent_evidence_refs:
                if ref.startswith("outcome:"):
                    outcome_val = ref.split("outcome:")[1]
                    break

        sentiment = classify_outcome_sentiment(outcome_val)

        item = SalesFeedbackItem(
            task_id=task.id,
            activity_id=activity.id if activity else task.related_activity_id,
            opportunity_id=opp_id,
            contact_id=contact_id,
            recommended_action=rec_action,
            task_type=task.task_type.value if hasattr(task.task_type, "value") else str(task.task_type),
            outcome=outcome_val,
            sentiment=sentiment,
            attribution_level=attr_level,
            occurred_at=activity.occurred_at if activity else task.completed_at,
            notes=activity.note if activity else task.description,
            evidence={
                "task_title": task.title,
                "task_status": task.status.value if hasattr(task.status, "value") else str(task.status),
                "agent_evidence_refs": task.agent_evidence_refs,
            },
        )
        items.append(item)

    return items


async def get_opportunity_sales_feedback(
    session: AsyncSession,
    opportunity_id: uuid.UUID
) -> Optional[SalesFeedbackSummaryResponse]:
    """
    Deterministic feedback aggregation for a specific Sales Opportunity.
    """
    # 1. Validate opportunity existence
    opp_stmt = select(LeadIntelligenceOpportunity).where(LeadIntelligenceOpportunity.id == opportunity_id)
    opp_res = await session.execute(opp_stmt)
    opportunity = opp_res.scalar_one_or_none()
    if not opportunity:
        return None

    # 2. Query matching tasks
    task_stmt = select(SalesTask).where(
        SalesTask.agent_source_type == "SALES_OPPORTUNITY",
        SalesTask.agent_source_id == opportunity_id,
        SalesTask.status == SalesTaskStatus.COMPLETED
    ).order_by(SalesTask.completed_at.desc().nullslast())
    task_res = await session.execute(task_stmt)
    tasks = list(task_res.scalars().all())

    items = await _load_feedback_items(session, tasks)
    return build_feedback_summary(items, scope="opportunity", scope_id=str(opportunity_id))


async def get_contact_sales_feedback(
    session: AsyncSession,
    contact_id: uuid.UUID
) -> Optional[SalesFeedbackSummaryResponse]:
    """
    Deterministic feedback aggregation for a specific Contact / Lead.
    """
    # 1. Validate contact existence
    lead_stmt = select(Lead).where(Lead.id == contact_id)
    lead_res = await session.execute(lead_stmt)
    lead = lead_res.scalar_one_or_none()
    if not lead:
        return None

    # 2. Query completed tasks for this contact
    task_stmt = select(SalesTask).where(
        SalesTask.lead_id == contact_id,
        SalesTask.status == SalesTaskStatus.COMPLETED
    ).order_by(SalesTask.completed_at.desc().nullslast())
    task_res = await session.execute(task_stmt)
    tasks = list(task_res.scalars().all())

    items = await _load_feedback_items(session, tasks)
    return build_feedback_summary(items, scope="contact", scope_id=str(contact_id))


async def get_recommendations_sales_feedback(
    session: AsyncSession,
    action: Optional[str] = None,
    sentiment: Optional[str] = None
) -> SalesFeedbackSummaryResponse:
    """
    Global recommendation and action feedback aggregation across all executed opportunities and contacts.
    """
    task_stmt = select(SalesTask).where(
        SalesTask.status == SalesTaskStatus.COMPLETED
    ).order_by(SalesTask.completed_at.desc().nullslast())
    task_res = await session.execute(task_stmt)
    tasks = list(task_res.scalars().all())

    items = await _load_feedback_items(session, tasks)

    # Optional filtering
    if action:
        items = [i for i in items if i.recommended_action == action or i.task_type == action]
    if sentiment:
        items = [i for i in items if i.sentiment.value == sentiment.upper()]

    return build_feedback_summary(items, scope="recommendations", scope_id=None)

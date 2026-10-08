import uuid
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.entities import (
    Lead,
    SalesTask,
    SalesTaskType,
    SalesTaskStatus,
    SalesActivity,
    SalesActivityType,
    SalesActivityOutcome,
    SalesStatus,
)


class SalesExecutionError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


TASK_TYPE_TO_ACTIVITY_TYPE: Dict[SalesTaskType, SalesActivityType] = {
    SalesTaskType.CALL: SalesActivityType.CALL,
    SalesTaskType.EMAIL: SalesActivityType.EMAIL,
    SalesTaskType.WHATSAPP: SalesActivityType.WHATSAPP,
    SalesTaskType.MEETING: SalesActivityType.MEETING,
    SalesTaskType.PROPOSAL: SalesActivityType.PROPOSAL,
    SalesTaskType.FOLLOW_UP: SalesActivityType.FOLLOW_UP,
    SalesTaskType.REVIEW_OUTREACH: SalesActivityType.NOTE,
    SalesTaskType.OTHER: SalesActivityType.OTHER,
}


async def record_sales_execution_outcome(
    session: AsyncSession,
    task_id: uuid.UUID,
    outcome: SalesActivityOutcome,
    notes: Optional[str] = None,
    activity_id: Optional[uuid.UUID] = None,
    opportunity_stage: Optional[str] = None,
    new_sales_status: Optional[SalesStatus] = None,
) -> Optional[Dict[str, Any]]:
    """
    Connects the SalesTask execution layer with deterministic recording
    of sales action outcomes.
    - Idempotently creates or reuses SalesActivity.
    - Transitions task to COMPLETED if active.
    - Updates evidence references and optionally updates lead sales status.
    - Does NOT send external communications.
    """
    # 1. Load the task
    task_stmt = select(SalesTask).where(SalesTask.id == task_id)
    task_res = await session.execute(task_stmt)
    task = task_res.scalar_one_or_none()
    if not task:
        return None

    if task.status == SalesTaskStatus.CANCELLED:
        raise SalesExecutionError(
            "TASK_CANCELLED",
            "Cannot record execution outcome for a cancelled task",
            status_code=409,
        )

    # 2. Activity Resolution (create or link)
    activity = None
    created = False
    reused = False

    if activity_id is not None:
        act_stmt = select(SalesActivity).where(SalesActivity.id == activity_id)
        act_res = await session.execute(act_stmt)
        existing_act = act_res.scalar_one_or_none()
        if not existing_act or existing_act.lead_id != task.lead_id:
            raise SalesExecutionError(
                "INVALID_ACTIVITY",
                "Specified activity was not found or does not belong to this lead",
                status_code=404,
            )
        existing_act.outcome = outcome
        if notes:
            existing_act.note = notes
        activity = existing_act
        created = False
        reused = True
    else:
        if task.related_activity_id is not None:
            act_stmt = select(SalesActivity).where(SalesActivity.id == task.related_activity_id)
            act_res = await session.execute(act_stmt)
            existing_act = act_res.scalar_one_or_none()
            if existing_act and existing_act.lead_id == task.lead_id:
                existing_act.outcome = outcome
                if notes:
                    existing_act.note = notes
                activity = existing_act
                created = False
                reused = True

        if activity is None:
            activity_type = TASK_TYPE_TO_ACTIVITY_TYPE.get(task.task_type, SalesActivityType.OTHER)
            note_content = notes if notes else f"Executed sales action: {task.title}"
            activity = SalesActivity(
                lead_id=task.lead_id,
                activity_type=activity_type,
                occurred_at=datetime.now(timezone.utc),
                note=note_content,
                outcome=outcome,
            )
            session.add(activity)
            await session.flush()
            created = True
            reused = False

    # 3. Update task
    task.related_activity_id = activity.id
    now = datetime.now(timezone.utc)
    if task.status in (SalesTaskStatus.OPEN, SalesTaskStatus.IN_PROGRESS):
        task.status = SalesTaskStatus.COMPLETED
        task.completed_at = now
    task.updated_at = now

    # 4. Update evidence refs
    outcome_val = outcome.value if hasattr(outcome, "value") else str(outcome)
    refs = list(task.agent_evidence_refs) if task.agent_evidence_refs else []
    outcome_ref = f"outcome:{outcome_val}"
    activity_ref = f"activity:{activity.id}"
    if outcome_ref not in refs:
        refs.append(outcome_ref)
    if activity_ref not in refs:
        refs.append(activity_ref)
    task.agent_evidence_refs = refs

    # 5. Optional Lead progression
    lead_stmt = select(Lead).where(Lead.id == task.lead_id)
    lead_res = await session.execute(lead_stmt)
    lead = lead_res.scalar_one_or_none()
    if lead:
        if new_sales_status is not None:
            lead.sales_status = new_sales_status
            lead.updated_at = now
        else:
            if outcome == SalesActivityOutcome.WON:
                lead.sales_status = SalesStatus.WON
                lead.updated_at = now
            elif outcome == SalesActivityOutcome.LOST:
                lead.sales_status = SalesStatus.LOST
                lead.updated_at = now
            elif outcome == SalesActivityOutcome.MEETING_BOOKED and lead.sales_status in (
                SalesStatus.NEW,
                SalesStatus.QUALIFIED,
                SalesStatus.CONTACTED,
            ):
                lead.sales_status = SalesStatus.ENGAGED
                lead.updated_at = now
            elif outcome == SalesActivityOutcome.PROPOSAL_SENT and lead.sales_status in (
                SalesStatus.NEW,
                SalesStatus.QUALIFIED,
                SalesStatus.CONTACTED,
                SalesStatus.ENGAGED,
            ):
                lead.sales_status = SalesStatus.PROPOSAL
                lead.updated_at = now
            elif outcome == SalesActivityOutcome.NEGOTIATION_STARTED and lead.sales_status in (
                SalesStatus.NEW,
                SalesStatus.QUALIFIED,
                SalesStatus.CONTACTED,
                SalesStatus.ENGAGED,
                SalesStatus.PROPOSAL,
            ):
                lead.sales_status = SalesStatus.NEGOTIATION
                lead.updated_at = now

    await session.commit()
    await session.refresh(task)
    await session.refresh(activity)

    opportunity_id = (
        task.agent_source_id if task.agent_source_type == "SALES_OPPORTUNITY" else None
    )

    return {
        "task_id": task.id,
        "activity_id": activity.id,
        "outcome": outcome_val,
        "task_status": task.status.value,
        "created": created,
        "reused": reused,
        "lead_id": task.lead_id,
        "opportunity_id": opportunity_id,
        "occurred_at": activity.occurred_at,
        "evidence": {
            "outcome": outcome_val,
            "task_id": str(task.id),
            "activity_id": str(activity.id),
            "agent_evidence_refs": task.agent_evidence_refs,
            "notes": notes,
            "opportunity_stage": opportunity_stage,
        },
    }

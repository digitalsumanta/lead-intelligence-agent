"""Deterministic task lifecycle, ownership checks, and due-state queries."""
from datetime import datetime, time, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Lead, OutreachArtifact, SalesActivity, SalesActivityOutcome, SalesActivityType,
    SalesPriority, SalesStatus,
    SalesTask, SalesTaskStatus, SalesTaskType,
)
from app.models.entities import now_utc
from app.schemas.sales_tasks import SalesTaskCreate, SalesTaskPatch


class SalesTaskError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


TASK_TRANSITIONS = {
    SalesTaskStatus.OPEN: {SalesTaskStatus.IN_PROGRESS, SalesTaskStatus.COMPLETED, SalesTaskStatus.CANCELLED},
    SalesTaskStatus.IN_PROGRESS: {SalesTaskStatus.OPEN, SalesTaskStatus.COMPLETED, SalesTaskStatus.CANCELLED},
    SalesTaskStatus.COMPLETED: set(),
    SalesTaskStatus.CANCELLED: set(),
}

NEXT_TASK_BY_OUTCOME = {
    SalesActivityOutcome.CALLBACK_REQUESTED: SalesTaskType.FOLLOW_UP,
    SalesActivityOutcome.MEETING_BOOKED: SalesTaskType.MEETING,
    SalesActivityOutcome.PROPOSAL_REQUESTED: SalesTaskType.PROPOSAL,
    SalesActivityOutcome.PROPOSAL_SENT: SalesTaskType.FOLLOW_UP,
    SalesActivityOutcome.NEGOTIATION_STARTED: SalesTaskType.FOLLOW_UP,
    SalesActivityOutcome.NO_RESPONSE: SalesTaskType.FOLLOW_UP,
}


def stage_commercial_context(lead: Lead):
    """Return the existing single current-stage commercial value for Action Center views."""
    if lead.sales_status in {SalesStatus.NEW, SalesStatus.QUALIFIED, SalesStatus.CONTACTED, SalesStatus.ENGAGED}:
        return lead.estimated_value, "estimated_value"
    if lead.sales_status == SalesStatus.PROPOSAL:
        return lead.proposal_value, "proposal_value"
    if lead.sales_status == SalesStatus.NEGOTIATION:
        return lead.negotiation_value, "negotiation_value"
    if lead.sales_status == SalesStatus.WON:
        return lead.won_value, "won_value"
    return None, None


def suggested_task_type(outcome: SalesActivityOutcome | None) -> SalesTaskType | None:
    """Return a human-action suggestion only; this never creates a task."""
    return NEXT_TASK_BY_OUTCOME.get(outcome)


def task_due_state(task: SalesTask, now: datetime | None = None) -> tuple[bool, bool]:
    now = now or now_utc()
    due_at = task.due_at
    # SQLite may return naive values for timezone=True columns. They were
    # required to be timezone-aware at the API boundary and are stored as UTC.
    if due_at.tzinfo is None or due_at.utcoffset() is None:
        due_at = due_at.replace(tzinfo=timezone.utc)
    active = task.status not in {SalesTaskStatus.COMPLETED, SalesTaskStatus.CANCELLED}
    return active and due_at <= now, active and due_at < now


async def _validate_relationships(session: AsyncSession, lead_id: UUID, activity_id, outreach_id):
    if await session.get(Lead, lead_id) is None:
        raise SalesTaskError("LEAD_NOT_FOUND", "Lead not found", 404)
    if activity_id is not None:
        activity = await session.scalar(select(SalesActivity).where(
            SalesActivity.id == activity_id, SalesActivity.lead_id == lead_id,
        ))
        if activity is None:
            raise SalesTaskError("ACTIVITY_NOT_FOUND", "Related activity does not belong to this lead", 422)
    if outreach_id is not None:
        artifact = await session.scalar(select(OutreachArtifact).where(
            OutreachArtifact.id == outreach_id, OutreachArtifact.lead_id == lead_id,
        ))
        if artifact is None:
            raise SalesTaskError("OUTREACH_NOT_FOUND", "Related outreach artifact does not belong to this lead", 422)


async def create_task(session: AsyncSession, lead_id: UUID, body: SalesTaskCreate) -> SalesTask:
    if body.seo_opportunity_id is not None:
        # Resolve against current persisted SEO data under a lead lock. This branch
        # only runs after the user explicitly submits the existing task form.
        lead = await session.scalar(select(Lead).where(Lead.id == lead_id).with_for_update())
        if lead is None:
            raise SalesTaskError("LEAD_NOT_FOUND", "Lead not found", 404)
        from app.services.seo_opportunities import read_seo_sales_opportunities

        opportunities = await read_seo_sales_opportunities(session, lead_id=lead_id)
        opportunity = next((item for item in opportunities if item["opportunity_id"] == body.seo_opportunity_id), None)
        if opportunity is None:
            raise SalesTaskError("SEO_OPPORTUNITY_STALE", "This SEO opportunity is no longer current", 409)
        if opportunity["current_task_state"] != "NO_TASK":
            raise SalesTaskError("SEO_OPPORTUNITY_ALREADY_HANDLED", "This SEO opportunity already has a linked task", 409)
        if body.related_activity_id is not None:
            raise SalesTaskError("SEO_TASK_ACTIVITY_CONFLICT", "SEO opportunity tasks create their own related activity", 422)
        await _validate_relationships(session, lead_id, None, body.related_outreach_artifact_id)
        marker = f"[seo-opportunity:{body.seo_opportunity_id}]"
        activity = SalesActivity(
            lead_id=lead_id,
            activity_type=SalesActivityType.NOTE,
            occurred_at=now_utc(),
            note=f"SEO opportunity task created {marker}: {opportunity['title']}",
        )
        session.add(activity)
        await session.flush()
        fields = body.model_dump(exclude={"seo_opportunity_id", "related_activity_id"})
        task = SalesTask(lead_id=lead_id, related_activity_id=activity.id, **fields)
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task
    await _validate_relationships(session, lead_id, body.related_activity_id, body.related_outreach_artifact_id)
    task = SalesTask(lead_id=lead_id, **body.model_dump(exclude={"seo_opportunity_id"}))
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def update_task(session: AsyncSession, task: SalesTask, body: SalesTaskPatch) -> SalesTask:
    supplied = body.model_fields_set
    if not supplied:
        raise SalesTaskError("EMPTY_TASK_UPDATE", "At least one task field must be provided")
    if task.status in {SalesTaskStatus.COMPLETED, SalesTaskStatus.CANCELLED}:
        raise SalesTaskError("TASK_TERMINAL", "Completed or cancelled tasks cannot be changed")
    for field in ("task_type", "title", "due_at", "priority"):
        if field in supplied and getattr(body, field) is None:
            raise SalesTaskError("INVALID_TASK_UPDATE", f"{field} cannot be null")
    if "status" in supplied:
        target = body.status
        if target is None or target not in TASK_TRANSITIONS[task.status]:
            raise SalesTaskError("INVALID_TASK_TRANSITION", f"Cannot change task from {task.status.value} to {target.value if target else 'null'}")
    await _validate_relationships(
        session, task.lead_id,
        body.related_activity_id if "related_activity_id" in supplied else task.related_activity_id,
        body.related_outreach_artifact_id if "related_outreach_artifact_id" in supplied else task.related_outreach_artifact_id,
    )
    for field in supplied:
        value = getattr(body, field)
        if field == "status":
            task.status = value
            task.completed_at = now_utc() if value == SalesTaskStatus.COMPLETED else None
        else:
            setattr(task, field, value)
    task.updated_at = now_utc()
    await session.commit()
    await session.refresh(task)
    return task


async def finish_task(session: AsyncSession, task: SalesTask, *, cancel: bool) -> SalesTask:
    target = SalesTaskStatus.CANCELLED if cancel else SalesTaskStatus.COMPLETED
    if target not in TASK_TRANSITIONS[task.status]:
        action = "cancelled" if cancel else "completed"
        raise SalesTaskError("INVALID_TASK_TRANSITION", f"Task in {task.status.value} state cannot be {action}")
    task.status = target
    task.completed_at = None if cancel else now_utc()
    task.updated_at = now_utc()
    await session.commit()
    await session.refresh(task)
    return task


def list_tasks_query(*, status=None, task_type=None, priority=None, assigned_to=None,
                     due=None, overdue=None, lead_id=None, now=None):
    now = now or now_utc()
    query = select(SalesTask)
    if status is not None:
        query = query.where(SalesTask.status == status)
    if task_type is not None:
        query = query.where(SalesTask.task_type == task_type)
    if priority is not None:
        query = query.where(SalesTask.priority == priority)
    if assigned_to is not None:
        query = query.where(SalesTask.assigned_to == assigned_to)
    if lead_id is not None:
        query = query.where(SalesTask.lead_id == lead_id)
    active = SalesTask.status.not_in([SalesTaskStatus.COMPLETED, SalesTaskStatus.CANCELLED])
    if due is True:
        query = query.where(active, SalesTask.due_at <= now)
    elif due is False:
        query = query.where(SalesTask.due_at > now)
    if overdue is True:
        query = query.where(active, SalesTask.due_at < now)
    elif overdue is False:
        query = query.where((~active) | (SalesTask.due_at >= now))
    return query.order_by(SalesTask.due_at.asc(), SalesTask.id.asc())


def task_day_windows(now: datetime | None = None):
    now = now or now_utc()
    day_start = datetime.combine(now.astimezone(timezone.utc).date(), time.min, tzinfo=timezone.utc)
    next_day = day_start + timedelta(days=1)
    seven_days = next_day + timedelta(days=7)
    active = SalesTask.status.not_in([SalesTaskStatus.COMPLETED, SalesTaskStatus.CANCELLED])
    return (
        select(SalesTask).where(active, SalesTask.due_at >= now, SalesTask.due_at < next_day).order_by(SalesTask.due_at.asc(), SalesTask.id.asc()),
        select(SalesTask).where(active, SalesTask.due_at < now).order_by(SalesTask.due_at.asc(), SalesTask.id.asc()),
        select(SalesTask).where(active, SalesTask.due_at >= next_day, SalesTask.due_at < seven_days).order_by(SalesTask.due_at.asc(), SalesTask.id.asc()),
    )


async def action_center_rows(session: AsyncSession, now: datetime | None = None):
    """Return existing task windows plus unresolved mapped outcome suggestions."""
    today, overdue, upcoming = task_day_windows(now)
    result = {}
    for name, query in (("today", today), ("overdue", overdue), ("upcoming", upcoming)):
        result[name] = (await session.execute(
            query.join(Lead, Lead.id == SalesTask.lead_id).add_columns(Lead).limit(500)
        )).all()

    handled_activity = select(SalesTask.id).where(
        SalesTask.related_activity_id == SalesActivity.id,
    ).exists()
    suggestion_outcomes = tuple(NEXT_TASK_BY_OUTCOME)
    result["outcome_suggestions"] = (await session.execute(
        select(SalesActivity, Lead)
        .join(Lead, Lead.id == SalesActivity.lead_id)
        .where(
            SalesActivity.outcome.in_(suggestion_outcomes),
            ~handled_activity,
        )
        .order_by(SalesActivity.occurred_at.asc(), SalesActivity.id.asc())
        .limit(200)
    )).all()
    return result

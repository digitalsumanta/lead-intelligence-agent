"""Sales pipeline: lead sales state, activities, commercial values, collections, revenue and tasks."""
import uuid
from datetime import datetime, timezone

from app.api.common import sales_activity_payload
from app.database import (
    get_db,
)
from app.models import (
    Lead,
    RevenueTarget,
    SalesActivity,
    SalesActivityType,
    SalesCollection,
    SalesPriority,
    SalesStatus,
    SalesTask,
    SalesTaskStatus,
    SalesTaskType,
)
from app.schemas.revenue import (
    CommercialOut,
    CommercialPatch,
    ConversionOut,
    PipelineStageOut,
    RevenueSummaryOut,
    RevenueTargetCreate,
    RevenueTargetOut,
    RevenueTargetPatch,
    SalesCollectionCreate,
    SalesCollectionOut,
    SalesCollectionPatch,
    SourceAnalyticsOut,
)
from app.schemas.sales import (
    SalesActivityCreate,
    SalesActivityOut,
    SalesOut,
    SalesPatch,
)
from app.schemas.sales_tasks import (
    OutcomeSuggestionOut,
    SalesActionTaskOut,
    SalesTaskCreate,
    SalesTaskOut,
    SalesTaskPatch,
    SalesTaskSummaryOut,
)
from app.services.outreach import (
    mark_outreach_stale,
)
from app.services.revenue import (
    RevenueError,
    commercial_values,
    conversion_analytics,
    create_collection,
    create_target,
    pipeline_summary,
    require_currency,
    revenue_summary,
    update_collection,
    update_commercial,
    update_target,
)
from app.services.sales_tasks import (
    SalesTaskError,
    action_center_rows,
    create_task,
    finish_task,
    list_tasks_query,
    stage_commercial_context,
    suggested_task_type,
    task_due_state,
    update_task,
)
from app.services.seo_opportunities import read_seo_sales_opportunities
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


def sales_collection_payload(collection, lead_company_name=None):
    collection_date = collection.collection_date
    if collection_date.tzinfo is None or collection_date.utcoffset() is None:
        collection_date = collection_date.replace(tzinfo=timezone.utc)
    return SalesCollectionOut.model_validate({
        "id": collection.id, "lead_id": collection.lead_id,
        "lead_company_name": lead_company_name, "amount": collection.amount,
        "currency": collection.currency, "collection_date": collection_date,
        "payment_reference": collection.payment_reference, "notes": collection.notes,
        "created_at": collection.created_at, "updated_at": collection.updated_at,
    })


def raise_revenue_error(error: RevenueError):
    raise HTTPException(error.status_code, detail={"code": error.code, "message": str(error)})


def sales_task_payload(task, lead_company_name=None):
    is_due, is_overdue = task_due_state(task)
    due_at = task.due_at
    if due_at.tzinfo is None or due_at.utcoffset() is None:
        due_at = due_at.replace(tzinfo=timezone.utc)
    return SalesTaskOut.model_validate({
        "id": task.id, "lead_id": task.lead_id, "assigned_to": task.assigned_to,
        "task_type": task.task_type, "title": task.title, "description": task.description,
        "due_at": due_at, "priority": task.priority, "status": task.status,
        "related_activity_id": task.related_activity_id,
        "related_outreach_artifact_id": task.related_outreach_artifact_id,
        "created_at": task.created_at, "updated_at": task.updated_at,
        "completed_at": task.completed_at, "is_due": is_due, "is_overdue": is_overdue,
        "lead_company_name": lead_company_name,
    })


def _raise_task_error(error: SalesTaskError):
    raise HTTPException(error.status_code, detail={"code": error.code, "message": str(error)})


async def _task_by_id(db: AsyncSession, task_id: uuid.UUID, *, lock=False):
    query = select(SalesTask).where(SalesTask.id == task_id)
    if lock:
        query = query.with_for_update()
    task = await db.scalar(query)
    if task is None:
        raise HTTPException(404, detail={"code": "TASK_NOT_FOUND", "message": "Task not found"})
    return task


def _utc_or_none(value):
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _action_task_payload(task: SalesTask, lead: Lead):
    value, value_field = stage_commercial_context(lead)
    base = sales_task_payload(task, lead.company_name).model_dump()
    return SalesActionTaskOut.model_validate({
        **base,
        "lead_sales_status": lead.sales_status,
        "lead_priority": lead.priority,
        "lead_assigned_to": lead.assigned_to,
        "next_follow_up_at": _utc_or_none(lead.next_follow_up_at),
        "current_stage_value": value,
        "current_stage_value_field": value_field,
        "currency": lead.currency,
    })


def _outcome_suggestion_payload(activity: SalesActivity, lead: Lead):
    value, value_field = stage_commercial_context(lead)
    return OutcomeSuggestionOut.model_validate({
        "activity_id": activity.id,
        "lead_id": lead.id,
        "lead_company_name": lead.company_name,
        "activity_type": activity.activity_type,
        "outcome": activity.outcome,
        "occurred_at": _utc_or_none(activity.occurred_at),
        "suggested_task_type": suggested_task_type(activity.outcome),
        "lead_sales_status": lead.sales_status,
        "lead_priority": lead.priority,
        "lead_assigned_to": lead.assigned_to,
        "next_follow_up_at": _utc_or_none(lead.next_follow_up_at),
        "current_stage_value": value,
        "current_stage_value_field": value_field,
        "currency": lead.currency,
    })


@router.get("/api/leads/{lead_id}/sales", response_model=SalesOut)
async def get_lead_sales(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    activities = (await db.scalars(select(SalesActivity).where(SalesActivity.lead_id == lead_id).order_by(SalesActivity.occurred_at.desc(), SalesActivity.id.desc()).limit(50))).all()
    return SalesOut(sales_status=lead.sales_status, priority=lead.priority, assigned_to=lead.assigned_to, next_follow_up_at=lead.next_follow_up_at, lost_reason=lead.lost_reason, recent_activities=[sales_activity_payload(item) for item in activities])


@router.patch("/api/leads/{lead_id}/sales", response_model=SalesOut)
async def update_lead_sales(lead_id: uuid.UUID, body: SalesPatch, db: AsyncSession = Depends(get_db)):
    lead = await db.scalar(select(Lead).where(Lead.id == lead_id).with_for_update())
    if lead is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    supplied = body.model_fields_set
    if "sales_status" in supplied and body.sales_status is None:
        raise HTTPException(422, detail={"code": "VALIDATION_ERROR", "message": "sales_status cannot be null"})
    if "priority" in supplied and body.priority is None:
        raise HTTPException(422, detail={"code": "VALIDATION_ERROR", "message": "priority cannot be null"})
    transitions = {
        SalesStatus.NEW: {SalesStatus.QUALIFIED, SalesStatus.CONTACTED, SalesStatus.LOST},
        SalesStatus.QUALIFIED: {SalesStatus.CONTACTED, SalesStatus.LOST},
        SalesStatus.CONTACTED: {SalesStatus.ENGAGED, SalesStatus.QUALIFIED, SalesStatus.LOST},
        SalesStatus.ENGAGED: {SalesStatus.PROPOSAL, SalesStatus.CONTACTED, SalesStatus.LOST},
        SalesStatus.PROPOSAL: {SalesStatus.NEGOTIATION, SalesStatus.ENGAGED, SalesStatus.WON, SalesStatus.LOST},
        SalesStatus.NEGOTIATION: {SalesStatus.WON, SalesStatus.PROPOSAL, SalesStatus.LOST},
        SalesStatus.WON: set(),
        SalesStatus.LOST: {SalesStatus.NEW, SalesStatus.QUALIFIED},
    }
    old_status = lead.sales_status
    target = body.sales_status if "sales_status" in supplied else old_status
    changed_status = target != lead.sales_status
    if changed_status and target not in transitions[lead.sales_status]:
        raise HTTPException(422, detail={"code": "INVALID_SALES_TRANSITION", "message": f"Cannot change sales status from {lead.sales_status.value} to {target.value}"})
    reason = body.lost_reason if "lost_reason" in supplied else lead.lost_reason
    if target == SalesStatus.LOST and not reason:
        raise HTTPException(422, detail={"code": "LOST_REASON_REQUIRED", "message": "lost_reason is required when changing status to LOST"})
    if target != SalesStatus.LOST and "lost_reason" in supplied and body.lost_reason:
        raise HTTPException(422, detail={"code": "INVALID_LOST_REASON", "message": "lost_reason is only allowed while sales_status is LOST"})
    if changed_status and target != SalesStatus.LOST:
        reason = None
    if target != SalesStatus.LOST:
        reason = None
    for field in ("priority", "assigned_to", "next_follow_up_at"):
        if field in supplied:
            setattr(lead, field, getattr(body, field))
    lead.sales_status = target
    lead.lost_reason = reason
    if changed_status:
        db.add(SalesActivity(lead_id=lead.id, activity_type=SalesActivityType.STATUS_CHANGE, note=f"Sales status changed from {old_status.value} to {target.value}.", old_sales_status=old_status, new_sales_status=target))
        await mark_outreach_stale(db, lead.id)
    await db.commit()
    await db.refresh(lead)
    activities = (await db.scalars(select(SalesActivity).where(SalesActivity.lead_id == lead.id).order_by(SalesActivity.occurred_at.desc(), SalesActivity.id.desc()).limit(50))).all()
    return SalesOut(sales_status=lead.sales_status, priority=lead.priority, assigned_to=lead.assigned_to, next_follow_up_at=lead.next_follow_up_at, lost_reason=lead.lost_reason, recent_activities=[sales_activity_payload(item) for item in activities])


@router.get("/api/leads/{lead_id}/activities", response_model=list[SalesActivityOut])
async def list_sales_activities(lead_id: uuid.UUID, limit: int = Query(50, ge=1, le=200), db: AsyncSession = Depends(get_db)):
    if await db.get(Lead, lead_id) is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    activities = (await db.scalars(select(SalesActivity).where(SalesActivity.lead_id == lead_id).order_by(SalesActivity.occurred_at.desc(), SalesActivity.id.desc()).limit(limit))).all()
    return [sales_activity_payload(item) for item in activities]


@router.post("/api/leads/{lead_id}/activities", response_model=SalesActivityOut, status_code=201)
async def create_sales_activity(lead_id: uuid.UUID, body: SalesActivityCreate, db: AsyncSession = Depends(get_db)):
    if await db.get(Lead, lead_id) is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    activity = SalesActivity(lead_id=lead_id, activity_type=body.activity_type, note=body.note, outcome=body.outcome)
    db.add(activity)
    await db.commit()
    await db.refresh(activity)
    return sales_activity_payload(activity)


@router.get("/api/leads/{lead_id}/commercial", response_model=CommercialOut)
async def get_lead_commercial(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    return await commercial_values(db, lead)


@router.patch("/api/leads/{lead_id}/commercial", response_model=CommercialOut)
async def patch_lead_commercial(lead_id: uuid.UUID, body: CommercialPatch, db: AsyncSession = Depends(get_db)):
    lead = await db.scalar(select(Lead).where(Lead.id == lead_id).with_for_update())
    if lead is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    try:
        lead = await update_commercial(db, lead, body)
    except RevenueError as error:
        raise_revenue_error(error)
    return await commercial_values(db, lead)


@router.get("/api/leads/{lead_id}/collections", response_model=list[SalesCollectionOut])
async def list_lead_collections(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    rows = (await db.scalars(select(SalesCollection).where(SalesCollection.lead_id == lead_id).order_by(SalesCollection.collection_date.desc(), SalesCollection.id.desc()))).all()
    return [sales_collection_payload(row, lead.company_name) for row in rows]


@router.post("/api/leads/{lead_id}/collections", response_model=SalesCollectionOut, status_code=201)
async def create_lead_collection(lead_id: uuid.UUID, body: SalesCollectionCreate, db: AsyncSession = Depends(get_db)):
    lead = await db.scalar(select(Lead).where(Lead.id == lead_id).with_for_update())
    if lead is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    try:
        collection = await create_collection(db, lead, body)
    except RevenueError as error:
        raise_revenue_error(error)
    return sales_collection_payload(collection, lead.company_name)


@router.get("/api/revenue/collections", response_model=list[SalesCollectionOut])
async def list_revenue_collections(
    currency: str = Query("INR", min_length=3, max_length=3),
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    limit: int = Query(200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    try:
        currency = require_currency(currency)
    except RevenueError as error:
        raise_revenue_error(error)
    query = select(SalesCollection, Lead.company_name).join(Lead, Lead.id == SalesCollection.lead_id).where(SalesCollection.currency == currency)
    if period_start is not None:
        if period_start.tzinfo is None or period_start.utcoffset() is None:
            raise HTTPException(422, detail={"code": "VALIDATION_ERROR", "message": "period_start must include a timezone"})
        query = query.where(SalesCollection.collection_date >= period_start.astimezone(timezone.utc))
    if period_end is not None:
        if period_end.tzinfo is None or period_end.utcoffset() is None:
            raise HTTPException(422, detail={"code": "VALIDATION_ERROR", "message": "period_end must include a timezone"})
        query = query.where(SalesCollection.collection_date <= period_end.astimezone(timezone.utc))
    if period_start and period_end and period_start > period_end:
        raise HTTPException(422, detail={"code": "VALIDATION_ERROR", "message": "period_start cannot exceed period_end"})
    rows = (await db.execute(query.order_by(SalesCollection.collection_date.desc(), SalesCollection.id.desc()).limit(limit))).all()
    return [sales_collection_payload(collection, company_name) for collection, company_name in rows]


@router.patch("/api/collections/{collection_id}", response_model=SalesCollectionOut)
async def patch_sales_collection(collection_id: uuid.UUID, body: SalesCollectionPatch, db: AsyncSession = Depends(get_db)):
    collection = await db.scalar(select(SalesCollection).where(SalesCollection.id == collection_id).with_for_update())
    if collection is None:
        raise HTTPException(404, detail={"code": "COLLECTION_NOT_FOUND", "message": "Collection not found"})
    lead = await db.get(Lead, collection.lead_id)
    try:
        collection = await update_collection(db, collection, body)
    except RevenueError as error:
        raise_revenue_error(error)
    return sales_collection_payload(collection, lead.company_name if lead else None)


@router.delete("/api/collections/{collection_id}", status_code=204)
async def delete_sales_collection(collection_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    collection = await db.get(SalesCollection, collection_id)
    if collection is None:
        raise HTTPException(404, detail={"code": "COLLECTION_NOT_FOUND", "message": "Collection not found"})
    await db.delete(collection)
    await db.commit()
    return None


@router.get("/api/revenue/summary", response_model=RevenueSummaryOut)
async def get_revenue_summary(currency: str = Query("INR", min_length=3, max_length=3), db: AsyncSession = Depends(get_db)):
    try:
        return await revenue_summary(db, currency)
    except RevenueError as error:
        raise_revenue_error(error)


@router.get("/api/revenue/pipeline", response_model=list[PipelineStageOut])
async def get_revenue_pipeline(currency: str = Query("INR", min_length=3, max_length=3), db: AsyncSession = Depends(get_db)):
    try:
        return await pipeline_summary(db, currency)
    except RevenueError as error:
        raise_revenue_error(error)


@router.get("/api/revenue/conversion", response_model=ConversionOut)
async def get_revenue_conversion(currency: str = Query("INR", min_length=3, max_length=3), db: AsyncSession = Depends(get_db)):
    try:
        return await conversion_analytics(db, currency)
    except RevenueError as error:
        raise_revenue_error(error)


@router.get("/api/revenue/sources", response_model=SourceAnalyticsOut)
async def get_revenue_sources():
    return SourceAnalyticsOut(available=False, reason="Lead records do not store a reliable acquisition source, so source attribution is not calculated.", sources=[])


@router.get("/api/revenue/targets", response_model=list[RevenueTargetOut])
async def list_revenue_targets(currency: str | None = Query(None, min_length=3, max_length=3), db: AsyncSession = Depends(get_db)):
    if currency is not None:
        try:
            currency = require_currency(currency)
        except RevenueError as error:
            raise_revenue_error(error)
    query = select(RevenueTarget)
    if currency:
        query = query.where(RevenueTarget.currency == currency)
    return (await db.scalars(query.order_by(RevenueTarget.active.desc(), RevenueTarget.period_start.desc(), RevenueTarget.created_at.desc()))).all()


@router.post("/api/revenue/targets", response_model=RevenueTargetOut, status_code=201)
async def post_revenue_target(body: RevenueTargetCreate, db: AsyncSession = Depends(get_db)):
    try:
        return await create_target(db, body)
    except RevenueError as error:
        raise_revenue_error(error)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code": "ACTIVE_TARGET_EXISTS", "message": "An active target already exists for this currency"})


@router.patch("/api/revenue/targets/{target_id}", response_model=RevenueTargetOut)
async def patch_revenue_target(target_id: uuid.UUID, body: RevenueTargetPatch, db: AsyncSession = Depends(get_db)):
    target = await db.scalar(select(RevenueTarget).where(RevenueTarget.id == target_id).with_for_update())
    if target is None:
        raise HTTPException(404, detail={"code": "TARGET_NOT_FOUND", "message": "Revenue target not found"})
    try:
        return await update_target(db, target, body)
    except RevenueError as error:
        raise_revenue_error(error)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code": "ACTIVE_TARGET_EXISTS", "message": "An active target already exists for this currency"})


@router.get("/api/leads/{lead_id}/tasks", response_model=list[SalesActionTaskOut])
async def list_lead_tasks(
    lead_id: uuid.UUID,
    status: SalesTaskStatus | None = None,
    task_type: SalesTaskType | None = None,
    priority: SalesPriority | None = None,
    assigned_to: str | None = Query(None, max_length=120),
    due: bool | None = None,
    overdue: bool | None = None,
    db: AsyncSession = Depends(get_db),
):
    if await db.get(Lead, lead_id) is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    query = list_tasks_query(status=status, task_type=task_type, priority=priority,
                             assigned_to=assigned_to, due=due, overdue=overdue, lead_id=lead_id)
    rows = (await db.execute(query.join(Lead, Lead.id == SalesTask.lead_id).add_columns(Lead).limit(500))).all()
    return [_action_task_payload(task, lead) for task, lead in rows]


@router.post("/api/leads/{lead_id}/tasks", response_model=SalesTaskOut, status_code=201)
async def create_lead_task(lead_id: uuid.UUID, body: SalesTaskCreate, db: AsyncSession = Depends(get_db)):
    try:
        task = await create_task(db, lead_id, body)
    except SalesTaskError as error:
        _raise_task_error(error)
    return sales_task_payload(task)


@router.get("/api/tasks", response_model=list[SalesActionTaskOut])
async def list_all_tasks(
    status: SalesTaskStatus | None = None,
    task_type: SalesTaskType | None = None,
    priority: SalesPriority | None = None,
    assigned_to: str | None = Query(None, max_length=120),
    due: bool | None = None,
    overdue: bool | None = None,
    lead_id: uuid.UUID | None = None,
    limit: int = Query(200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    query = list_tasks_query(status=status, task_type=task_type, priority=priority,
                             assigned_to=assigned_to, due=due, overdue=overdue, lead_id=lead_id)
    query = query.join(Lead, Lead.id == SalesTask.lead_id).add_columns(Lead)
    rows = (await db.execute(query.limit(limit))).all()
    return [_action_task_payload(task, lead) for task, lead in rows]


@router.get("/api/tasks/summary", response_model=SalesTaskSummaryOut)
async def task_summary(db: AsyncSession = Depends(get_db)):
    rows = await action_center_rows(db)
    return {
        name: [_action_task_payload(task, lead) for task, lead in rows[name]]
        for name in ("today", "overdue", "upcoming")
    } | {
        "outcome_suggestions": [_outcome_suggestion_payload(activity, lead) for activity, lead in rows["outcome_suggestions"]],
        "seo_opportunities": await read_seo_sales_opportunities(db),
    }


@router.patch("/api/tasks/{task_id}", response_model=SalesTaskOut)
async def patch_sales_task(task_id: uuid.UUID, body: SalesTaskPatch, db: AsyncSession = Depends(get_db)):
    task = await _task_by_id(db, task_id, lock=True)
    try:
        task = await update_task(db, task, body)
    except SalesTaskError as error:
        _raise_task_error(error)
    return sales_task_payload(task)


@router.post("/api/tasks/{task_id}/complete", response_model=SalesTaskOut)
async def complete_sales_task(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    task = await _task_by_id(db, task_id, lock=True)
    try:
        task = await finish_task(db, task, cancel=False)
    except SalesTaskError as error:
        _raise_task_error(error)
    return sales_task_payload(task)


@router.post("/api/tasks/{task_id}/cancel", response_model=SalesTaskOut)
async def cancel_sales_task(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    task = await _task_by_id(db, task_id, lock=True)
    try:
        task = await finish_task(db, task, cancel=True)
    except SalesTaskError as error:
        _raise_task_error(error)
    return sales_task_payload(task)

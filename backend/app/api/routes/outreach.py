"""Prepared outreach (Phase 8), sending/execution operations and e-mail delivery events/analytics."""
import uuid
from datetime import date, datetime
from typing import Literal

from app.api.common import sales_activity_payload
from app.database import (
    get_db,
)
from app.domain.delivery_reconciliation import normalize_delivery_event_type
from app.models import (
    Lead,
    OutreachArtifact,
    OutreachExecution,
    OutreachStatus,
)
from app.models.entities import now_utc
from app.providers import get_provider
from app.schemas.email_delivery_event import (
    DeliveryAnalyticsProviderSummary,
    DeliveryAnalyticsSummary,
    DeliveryAnalyticsTimeSeriesPoint,
    DeliveryReconciliationOut,
    EmailDeliveryEventCreate,
    EmailDeliveryEventHistory,
    EmailDeliveryEventIngestOut,
    EmailDeliveryEventIngestRequest,
    EmailDeliveryEventOut,
    ExecutionDeliveryAnalytics,
    NormalizedEmailDeliveryEvent,
)
from app.schemas.outreach import (
    OutreachArtifactOut,
    OutreachContextOut,
    OutreachPatch,
    OutreachReadOut,
    OutreachReject,
)
from app.schemas.outreach_analytics import (
    OutreachAnalyticsChannel,
    OutreachAnalyticsResponse,
)
from app.schemas.outreach_operations import (
    ManualSendConfirmation,
    OutreachExecutionOut,
    OutreachOutcomeCreate,
    OutreachRetryRequest,
    OutreachSendRequest,
)
from app.schemas.sales import (
    SalesActivityOut,
)
from app.services.email_delivery_analytics import (
    get_analytics_summary,
    get_execution_analytics,
    get_provider_analytics,
    get_timeseries_analytics,
)
from app.services.email_delivery_events import (
    EmailDeliveryEventError,
    email_delivery_event_ingestion_service,
    email_delivery_event_service,
)
from app.services.lead_intelligence_api import read_lead_intelligence
from app.services.outreach import (
    OutreachUnavailable,
    current_artifact_response,
    generate_outreach,
    mark_outreach_stale,
    source_state,
    validate_outreach,
)
from app.services.outreach_analytics import outreach_analytics
from app.services.outreach_operations import (
    OutreachOperationError,
    cancel_manual_execution,
    confirm_manual_send,
    record_outreach_outcome,
    retry_outreach_execution,
    send_approved_outreach,
)
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Response,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

router = APIRouter()


async def _latest_outreach(db: AsyncSession, lead_id: uuid.UUID):
    return await db.scalar(select(OutreachArtifact).where(
        OutreachArtifact.lead_id == lead_id,
    ).order_by(OutreachArtifact.created_at.desc(), OutreachArtifact.id.desc()).limit(1))


async def _outreach_is_current(db: AsyncSession, lead: Lead, artifact: OutreachArtifact):
    intelligence = await read_lead_intelligence(db, lead.id)
    if intelligence is None:
        return False, None
    fingerprint, _, _ = source_state(intelligence, lead)
    return fingerprint == artifact.source_fingerprint and artifact.status != OutreachStatus.STALE, intelligence


def _outreach_payload(artifact: OutreachArtifact, is_current: bool, *, sales_status=None):
    try:
        return current_artifact_response(artifact, is_current, current_sales_status=sales_status)
    except (TypeError, ValueError):
        return OutreachArtifactOut(
            id=artifact.id, lead_id=artifact.lead_id, enrichment_run_id=artifact.enrichment_run_id,
            status=OutreachStatus.STALE if not is_current else OutreachStatus.DRAFT,
            is_current=is_current, sales_status=sales_status or artifact.sales_context["sales_status"],
            sales_context_version=artifact.sales_context_version, contract_version=artifact.contract_version,
            prompt_version=artifact.prompt_version, content=None,
            validation_result={"valid": False, "error_code": "PERSISTED_CONTENT_INVALID"},
            generation_error="Stored outreach content failed validation.", generated_at=artifact.generated_at,
            reviewed_at=artifact.reviewed_at, reviewed_by=artifact.reviewed_by,
            rejection_reason=artifact.rejection_reason,
        )


def _raise_outreach_operation_error(error: OutreachOperationError):
    raise HTTPException(error.status_code, detail={"code": error.code, "message": str(error)})


def _outreach_execution_payload(execution: OutreachExecution):
    return OutreachExecutionOut.model_validate(execution)


@router.get("/api/leads/{lead_id}/outreach", response_model=OutreachReadOut)
async def get_lead_outreach(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    intelligence = await read_lead_intelligence(db, lead_id)
    context = None
    if intelligence is not None:
        context = OutreachContextOut(
            sales_status=lead.sales_status, score=intelligence.score.value,
            opportunity_count=len(intelligence.opportunities),
            recommended_services=tuple(item.service_name for item in intelligence.recommendations),
            personalization_available=intelligence.personalization_available,
        )
    artifact = await _latest_outreach(db, lead_id)
    if artifact is None:
        return OutreachReadOut(current_context=context, artifact=None)
    is_current = False
    if intelligence is not None:
        fingerprint, _, _ = source_state(intelligence, lead)
        is_current = fingerprint == artifact.source_fingerprint and artifact.status != OutreachStatus.STALE
    return OutreachReadOut(current_context=context, artifact=_outreach_payload(artifact, is_current, sales_status=lead.sales_status))


@router.post("/api/leads/{lead_id}/outreach/generate", response_model=OutreachArtifactOut)
async def generate_lead_outreach(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    try:
        provider = get_provider(session_factory=factory)
    except Exception:
        provider = None
    try:
        artifact = await generate_outreach(factory, lead_id, provider)
    except OutreachUnavailable as exc:
        message = str(exc)
        if message == "Lead not found":
            raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": message})
        code = "OUTREACH_NOT_ALLOWED" if "WON or LOST" in message else "INTELLIGENCE_NOT_AVAILABLE"
        raise HTTPException(409, detail={"code": code, "message": message})
    return _outreach_payload(artifact, artifact.status != OutreachStatus.STALE)


@router.patch("/api/leads/{lead_id}/outreach", response_model=OutreachArtifactOut)
async def edit_lead_outreach(lead_id: uuid.UUID, body: OutreachPatch, db: AsyncSession = Depends(get_db)):
    lead = await db.get(Lead, lead_id)
    artifact = await _latest_outreach(db, lead_id)
    if lead is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    if artifact is None:
        raise HTTPException(404, detail={"code": "OUTREACH_NOT_FOUND", "message": "No outreach artifact is available"})
    current, intelligence = await _outreach_is_current(db, lead, artifact)
    if not current:
        await mark_outreach_stale(db, lead_id)
        await db.commit()
        raise HTTPException(409, detail={"code": "OUTREACH_STALE", "message": "This outreach artifact is stale and cannot be edited"})
    if artifact.status not in {OutreachStatus.DRAFT, OutreachStatus.READY_FOR_REVIEW}:
        raise HTTPException(409, detail={"code": "OUTREACH_NOT_EDITABLE", "message": "Only draft or reviewable outreach can be edited"})
    try:
        validate_outreach(body.content, intelligence, lead.sales_status)
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "INVALID_OUTREACH_CONTENT", "message": str(exc)})
    artifact.content = body.content.model_dump(mode="json")
    artifact.status = OutreachStatus.READY_FOR_REVIEW
    artifact.validation_result = {"valid": True, "edited_by_human": True}
    artifact.generation_error = None
    artifact.updated_at = now_utc()
    await db.commit()
    await db.refresh(artifact)
    return _outreach_payload(artifact, True, sales_status=lead.sales_status)


@router.post("/api/leads/{lead_id}/outreach/approve", response_model=OutreachArtifactOut)
async def approve_lead_outreach(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    lead = await db.get(Lead, lead_id)
    artifact = await _latest_outreach(db, lead_id)
    if lead is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    if artifact is None:
        raise HTTPException(404, detail={"code": "OUTREACH_NOT_FOUND", "message": "No outreach artifact is available"})
    current, _ = await _outreach_is_current(db, lead, artifact)
    if not current:
        await mark_outreach_stale(db, lead_id)
        await db.commit()
        raise HTTPException(409, detail={"code": "OUTREACH_STALE", "message": "Stale outreach cannot be approved"})
    if artifact.status != OutreachStatus.READY_FOR_REVIEW or artifact.content is None:
        raise HTTPException(409, detail={"code": "OUTREACH_NOT_REVIEWABLE", "message": "Only validated outreach awaiting review can be approved"})
    artifact.status = OutreachStatus.APPROVED
    artifact.reviewed_at = now_utc()
    artifact.reviewed_by = None
    artifact.rejection_reason = None
    artifact.updated_at = now_utc()
    await db.commit()
    await db.refresh(artifact)
    return _outreach_payload(artifact, True, sales_status=lead.sales_status)


@router.post("/api/leads/{lead_id}/outreach/reject", response_model=OutreachArtifactOut)
async def reject_lead_outreach(lead_id: uuid.UUID, body: OutreachReject, db: AsyncSession = Depends(get_db)):
    lead = await db.get(Lead, lead_id)
    artifact = await _latest_outreach(db, lead_id)
    if lead is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    if artifact is None:
        raise HTTPException(404, detail={"code": "OUTREACH_NOT_FOUND", "message": "No outreach artifact is available"})
    current, _ = await _outreach_is_current(db, lead, artifact)
    if not current:
        await mark_outreach_stale(db, lead_id)
        await db.commit()
        raise HTTPException(409, detail={"code": "OUTREACH_STALE", "message": "Stale outreach cannot be rejected"})
    if artifact.status not in {OutreachStatus.DRAFT, OutreachStatus.READY_FOR_REVIEW}:
        raise HTTPException(409, detail={"code": "OUTREACH_NOT_REJECTABLE", "message": "Only draft or reviewable outreach can be rejected"})
    artifact.status = OutreachStatus.REJECTED
    artifact.reviewed_at = now_utc()
    artifact.reviewed_by = None
    artifact.rejection_reason = body.reason
    artifact.updated_at = now_utc()
    await db.commit()
    await db.refresh(artifact)
    return _outreach_payload(artifact, True, sales_status=lead.sales_status)


@router.get("/api/leads/{lead_id}/outreach/executions", response_model=list[OutreachExecutionOut])
async def list_lead_outreach_executions(lead_id: uuid.UUID, limit: int = Query(100, ge=1, le=500), db: AsyncSession = Depends(get_db)):
    if await db.get(Lead, lead_id) is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    rows = (await db.scalars(select(OutreachExecution).where(
        OutreachExecution.lead_id == lead_id,
    ).order_by(OutreachExecution.requested_at.desc(), OutreachExecution.id.desc()).limit(limit))).all()
    return [_outreach_execution_payload(item) for item in rows]


@router.post("/api/outreach/{artifact_id}/send", response_model=OutreachExecutionOut, status_code=201)
async def send_outreach_artifact(artifact_id: uuid.UUID, body: OutreachSendRequest, db: AsyncSession = Depends(get_db)):
    try:
        execution = await send_approved_outreach(db, artifact_id, body)
    except OutreachOperationError as error:
        _raise_outreach_operation_error(error)
    return _outreach_execution_payload(execution)


@router.post("/api/outreach/executions/{execution_id}/retry", response_model=OutreachExecutionOut, status_code=201)
async def retry_outreach_send(execution_id: uuid.UUID, body: OutreachRetryRequest, db: AsyncSession = Depends(get_db)):
    try:
        execution = await retry_outreach_execution(db, execution_id, body)
    except OutreachOperationError as error:
        _raise_outreach_operation_error(error)
    return _outreach_execution_payload(execution)


@router.post("/api/outreach/executions/{execution_id}/outcomes", response_model=SalesActivityOut, status_code=201)
async def create_outreach_outcome(execution_id: uuid.UUID, body: OutreachOutcomeCreate, db: AsyncSession = Depends(get_db)):
    try:
        activity = await record_outreach_outcome(db, execution_id, body)
    except OutreachOperationError as error:
        _raise_outreach_operation_error(error)
    return sales_activity_payload(activity)


@router.get("/api/outreach/analytics", response_model=OutreachAnalyticsResponse)
async def get_outreach_analytics(
    from_date: date | None = Query(None, alias="from"),
    to: date | None = Query(None),
    channel: OutreachAnalyticsChannel | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Return aggregate sent-execution and human-recorded outcome metrics."""
    if from_date is not None and to is not None and to < from_date:
        raise HTTPException(422, detail={"code": "INVALID_DATE_RANGE", "message": "to must be on or after from"})
    return await outreach_analytics(db, from_date=from_date, to=to, channel=channel)


@router.post("/api/outreach/executions/{execution_id}/mark-manual", response_model=OutreachExecutionOut)
async def mark_outreach_manually_sent(execution_id: uuid.UUID, body: ManualSendConfirmation, db: AsyncSession = Depends(get_db)):
    try:
        execution = await confirm_manual_send(db, execution_id, body)
    except OutreachOperationError as error:
        _raise_outreach_operation_error(error)
    return _outreach_execution_payload(execution)


@router.post("/api/outreach/executions/{execution_id}/cancel", response_model=OutreachExecutionOut)
async def cancel_outreach_execution(execution_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    try:
        execution = await cancel_manual_execution(db, execution_id)
    except OutreachOperationError as error:
        _raise_outreach_operation_error(error)
    return _outreach_execution_payload(execution)


@router.post("/api/email-delivery-events", response_model=EmailDeliveryEventOut)
async def create_email_delivery_event(
    body: EmailDeliveryEventCreate, response: Response, db: AsyncSession = Depends(get_db),
):
    try:
        event, created = await email_delivery_event_service.record(db, body)
    except EmailDeliveryEventError as error:
        raise HTTPException(error.status_code, detail={"code": error.code, "message": str(error)}) from None
    response.status_code = 201 if created else 200
    return event


@router.post("/api/email-delivery-events/ingest", response_model=EmailDeliveryEventIngestOut)
async def ingest_email_delivery_event(
    body: EmailDeliveryEventIngestRequest, response: Response, db: AsyncSession = Depends(get_db),
):
    normalized_type = normalize_delivery_event_type(body.event_type)
    if normalized_type.event_type is None:
        raise HTTPException(422, detail={
            "code": "UNSUPPORTED_EVENT_TYPE",
            "message": f"Unsupported delivery event type: {normalized_type.normalized_value}",
        })
    try:
        normalized = NormalizedEmailDeliveryEvent(
            source="PROVIDER",
            provider=body.provider,
            provider_event_id=body.provider_event_id,
            provider_message_id=body.provider_message_id,
            event_type=normalized_type.event_type,
            recipient_email=body.recipient_email,
            provider_status=body.provider_status,
            occurred_at=body.occurred_at,
            metadata=body.metadata,
        )
        event, execution_id, created = await email_delivery_event_ingestion_service.ingest(db, normalized)
    except EmailDeliveryEventError as error:
        raise HTTPException(error.status_code, detail={"code": error.code, "message": str(error)}) from None
    response.status_code = 201 if created else 200
    return {"status": "CREATED" if created else "DUPLICATE", "event": event, "execution_id": execution_id}


@router.get(
    "/api/outreach-executions/{execution_id}/delivery-events",
    response_model=EmailDeliveryEventHistory,
)
async def list_email_delivery_events(execution_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    try:
        return {"items": await email_delivery_event_service.history(db, execution_id)}
    except EmailDeliveryEventError as error:
        raise HTTPException(error.status_code, detail={"code": error.code, "message": str(error)}) from None


@router.get(
    "/api/outreach-executions/{execution_id}/delivery-status",
    response_model=DeliveryReconciliationOut,
)
async def get_email_delivery_status(execution_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    try:
        return await email_delivery_event_service.delivery_status(db, execution_id)
    except EmailDeliveryEventError as error:
        raise HTTPException(error.status_code, detail={"code": error.code, "message": str(error)}) from None


@router.get("/api/email-delivery-analytics/summary", response_model=DeliveryAnalyticsSummary)
async def endpoint_email_delivery_analytics_summary(
    scope: Literal["global", "date_range", "provider", "execution"],
    start: datetime | None = None,
    end: datetime | None = None,
    provider: str | None = None,
    execution_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db)
):
    return await get_analytics_summary(db, scope, start, end, provider, execution_id)


@router.get("/api/email-delivery-analytics/providers", response_model=list[DeliveryAnalyticsProviderSummary])
async def endpoint_email_delivery_analytics_providers(
    db: AsyncSession = Depends(get_db)
):
    return await get_provider_analytics(db)


@router.get("/api/email-delivery-analytics/timeseries", response_model=list[DeliveryAnalyticsTimeSeriesPoint])
async def endpoint_email_delivery_analytics_timeseries(
    granularity: Literal["day", "week"] = "day",
    start: datetime | None = None,
    end: datetime | None = None,
    db: AsyncSession = Depends(get_db)
):
    return await get_timeseries_analytics(db, granularity, start, end)


@router.get("/api/outreach-executions/{execution_id}/delivery-analytics", response_model=ExecutionDeliveryAnalytics)
async def endpoint_outreach_execution_delivery_analytics(
    execution_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    result = await get_execution_analytics(db, execution_id)
    if result is None:
        raise HTTPException(404, detail={"code": "EXECUTION_NOT_FOUND", "message": "Execution not found"})
    return result

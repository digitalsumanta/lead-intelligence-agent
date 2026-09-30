"""FastAPI application and REST endpoints."""
import logging, uuid, hmac
from datetime import date, datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, UploadFile, File, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select, func, case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from app.config import settings
from app.database import get_db, SessionLocal, engine, verify_database_schema, worker_is_available
from app.models import (
    Lead, ImportJob, ProcessingJob, LeadEvent, LeadStatus, JobStatus, LeadBatch,
    EnrichmentRun, LeadScore, Opportunity, ServiceRecommendation, Personalization,
    SalesActivity, SalesStatus, SalesPriority, SalesActivityType, SalesActivityOutcome, SalesTask, SalesTaskType, SalesTaskStatus, OutreachArtifact, OutreachStatus, OutreachExecution, SalesCollection, RevenueTarget,
)
from app.models.entities import now_utc
from app.schemas.lead import LeadOut, ProcessBatchIn
from app.schemas.sales import SalesPatch, SalesActivityCreate, SalesOut, SalesActivityOut
from app.schemas.outreach import OutreachArtifactOut, OutreachReadOut, OutreachContextOut, OutreachPatch, OutreachReject
from app.schemas.outreach_operations import (
    OutreachSendRequest, OutreachRetryRequest, OutreachOutcomeCreate, ManualSendConfirmation,
    OutreachExecutionOut,
)
from app.schemas.sales_tasks import (
    OutcomeSuggestionOut, SalesActionTaskOut, SalesTaskCreate, SalesTaskPatch,
    SalesTaskOut, SalesTaskSummaryOut,
)
from app.schemas.revenue import (
    CommercialPatch, CommercialOut, SalesCollectionCreate, SalesCollectionPatch,
    SalesCollectionOut, RevenueTargetCreate, RevenueTargetPatch, RevenueTargetOut,
    RevenueSummaryOut, PipelineStageOut, ConversionOut, SourceAnalyticsOut,
)
from app.schemas.outreach_analytics import OutreachAnalyticsChannel, OutreachAnalyticsResponse
from app.schemas.intelligence import IntelligenceResponse
from app.schemas.seo import LatestSeoOut, SeoRunCreatedOut, SeoRunDetailOut, SeoRunListOut, SeoRunOut
from app.schemas.seo_opportunities import SeoSalesOpportunityList
from app.schemas.ai_sales_intelligence import AISalesIntelligenceHistoryOut, AISalesIntelligenceOut
from app.schemas.ai_sales_copilot import CopilotAsk, CopilotHistoryOut, CopilotInteractionOut
from app.schemas.ai_sales_action_planner import ActionPlanHistoryOut, ActionPlanOut
from app.schemas.ai_proposal_intelligence import ProposalIntelligenceHistoryOut, ProposalIntelligenceOut
from app.schemas.agent_policy import PolicyEvaluationHistory, PolicyEvaluationOut, PolicyEvaluationRequest
from app.services.lead_intelligence_api import read_intelligence_summary, read_lead_intelligence
from app.services.csv_import import import_csv
from app.services.jobs import create_job, update_batch_status
from app.services.key_pool import ApiKeyPool
from app.providers import get_provider, validate_provider_model
from app.utils.logging_config import configure_logging
from app.services.key_config import sync_environment_keys
from app.services.outreach import (
    OutreachUnavailable, current_artifact_response, generate_outreach,
    mark_outreach_stale, source_state, validate_outreach,
)
from app.services.outreach_operations import (
    OutreachOperationError, send_approved_outreach, retry_outreach_execution,
    confirm_manual_send, cancel_manual_execution, record_outreach_outcome,
)
from app.services.sales_tasks import (
    SalesTaskError, create_task, finish_task, list_tasks_query, suggested_task_type,
    action_center_rows, task_due_state, update_task, stage_commercial_context,
)
from app.services.revenue import (
    RevenueError, commercial_values, update_commercial, create_collection,
    update_collection, create_target, update_target, revenue_summary,
    pipeline_summary, conversion_analytics, require_currency,
)
from app.services.outreach_analytics import outreach_analytics
from app.services.seo import SeoServiceError, create_seo_run
from app.models.entities import SeoAuditRun, SeoFinding, SeoObservation
from app.services.seo_opportunities import read_seo_sales_opportunities
from app.services.ai_sales_intelligence import (
    ai_sales_intelligence_by_id, ai_sales_intelligence_history,
    current_ai_sales_intelligence, generate_ai_sales_intelligence,
)
from app.services.ai_sales_copilot import ai_sales_copilot_history, ask_ai_sales_copilot
from app.services.ai_sales_action_planner import (
    action_plan_by_id, action_plan_history, current_action_plan, generate_action_plan,
)
from app.services.ai_proposal_intelligence import (
    current_proposal, generate_proposal, proposal_by_id, proposal_history,
)
from app.services.agent_policy import evaluate_policy, policy_history

configure_logging()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Verify the migrated database schema; Alembic owns all schema changes."""
    await verify_database_schema()
    async with SessionLocal() as session:
        await sync_environment_keys(session)
    yield
    await engine.dispose()

app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


async def evaluate_provider_readiness(provider, session_factory=SessionLocal) -> tuple[dict, bool]:
    """Check only the selected provider and avoid leasing keys or making generations."""
    status = await provider.health_check()
    ready = bool(status.get("healthy"))
    if provider.name == "ollama":
        ready = ready and status.get("model_available") is True
        status["serviceable"] = ready
        return status, ready
    if provider.name in {"gemini", "openai_compatible"}:
        status["key_pool"] = {"configured": 0, "healthy": 0}
        if ready and status.get("configured"):
            async with session_factory() as session:
                status["key_pool"] = await ApiKeyPool().health(session, provider.name, provider.operation)
        ready = ready and status.get("configured") is True and status["key_pool"]["healthy"] > 0
    status["serviceable"] = ready
    return status, ready

@app.middleware("http")
async def local_api_auth(request: Request, call_next):
    """Require the configured bearer token for application data and mutation routes."""
    if request.url.path.startswith("/api/"):
        token = settings.api_auth_token
        supplied = request.headers.get("authorization", "")
        supplied = supplied[7:].strip() if supplied.lower().startswith("bearer ") else ""
        if not token or not supplied or not hmac.compare_digest(token, supplied):
            return JSONResponse(status_code=401, content={"success": False, "error": {"code": "UNAUTHORIZED", "message": "Valid API bearer token required", "details": {}}})
    return await call_next(request)

@app.exception_handler(Exception)
async def global_error_handler(request, exc):
    """Return a stable JSON error envelope."""
    logging.getLogger("lead_agent").exception("request failed")
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=500, content={"success": False, "error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred", "details": {}}})

@app.exception_handler(HTTPException)
async def http_error_handler(request, exc: HTTPException):
    """Wrap route errors in the consistent error contract."""
    from fastapi.responses import JSONResponse
    detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    code = detail.get("code", "HTTP_ERROR")
    message = detail.get("message", "Request failed")
    return JSONResponse(status_code=exc.status_code, content={"success": False, "error": {"code": code, "message": message, "details": {k:v for k,v in detail.items() if k not in {"code", "message"}}}})

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc: RequestValidationError):
    """Return field level details for invalid request payloads."""
    from fastapi.responses import JSONResponse
    details = [{"field": ".".join(str(x) for x in error["loc"]), "message": error["msg"]} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"success": False, "error": {"code": "VALIDATION_ERROR", "message": "Request validation failed", "details": {"fields": details}}})

@app.get("/health")
async def health():
    """Basic process liveness check."""
    return {"success": True, "status": "healthy", "service": settings.app_name}

@app.get("/ready")
async def readiness():
    """Report readiness including the database schema and configured provider."""
    try:
        await verify_database_schema()
    except Exception:
        return JSONResponse(status_code=503, content={"success": False, "ready": False, "checks": {"database": "unavailable_or_unmigrated", "provider": "not_checked", "worker": "unavailable"}})
    try:
        provider = get_provider()
        provider_status, provider_ready = await evaluate_provider_readiness(provider, SessionLocal)
    except Exception:
        provider_status = {"healthy": False, "error": "Provider health check failed"}
        provider_ready = False
    try:
        worker_ready = await worker_is_available()
    except Exception:
        worker_ready = False
    ready = provider_ready and worker_ready
    payload = {"success": ready, "ready": ready, "checks": {"database": "healthy", "provider": provider_status, "worker": "healthy" if worker_ready else "unavailable"}}
    return payload if ready else JSONResponse(status_code=503, content=payload)

@app.get("/api/leads", response_model=list[LeadOut])
async def list_leads(
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
    status: LeadStatus | None = None,
    min_score: int | None = Query(None, ge=0, le=100),
    max_score: int | None = Query(None, ge=0, le=100),
    intelligence_status: str | None = Query(None, pattern="^(available|unavailable)$"),
    personalization_available: bool | None = None,
    opportunity_present: bool | None = None,
    sales_status: SalesStatus | None = None,
    priority: SalesPriority | None = None,
    follow_up_due: bool | None = None,
    db: AsyncSession = Depends(get_db),
):
    """List leads newest first, with stable created_at/id ordering and one-based pages."""
    latest_run = select(EnrichmentRun.id).where(
        EnrichmentRun.lead_id == Lead.id,
        func.lower(EnrichmentRun.status) == "completed",
        select(LeadScore.id).where(
            LeadScore.lead_id == Lead.id,
            LeadScore.enrichment_run_id == EnrichmentRun.id,
        ).exists(),
    ).order_by(func.coalesce(EnrichmentRun.completed_at, EnrichmentRun.created_at).desc(), EnrichmentRun.id.desc()).limit(1).correlate(Lead).scalar_subquery()
    score_value = select(LeadScore.score).where(
        LeadScore.lead_id == Lead.id, LeadScore.enrichment_run_id == latest_run,
    ).order_by(LeadScore.calculated_at.desc(), LeadScore.id.desc()).limit(1).correlate(Lead).scalar_subquery()
    opportunity_count = select(func.count(Opportunity.id)).where(
        Opportunity.lead_id == Lead.id, Opportunity.enrichment_run_id == latest_run,
        Opportunity.origin == "deterministic",
    ).correlate(Lead).scalar_subquery()
    recommendation_count = select(func.count(ServiceRecommendation.id)).where(
        ServiceRecommendation.lead_id == Lead.id, ServiceRecommendation.enrichment_run_id == latest_run,
    ).correlate(Lead).scalar_subquery()
    has_personalization = select(Personalization.id).where(
        Personalization.lead_id == Lead.id, Personalization.enrichment_run_id == latest_run,
        Personalization.status == "validated", Personalization.generated.is_not(None),
        Personalization.contract_version == "personalization-v2",
    ).exists().correlate(Lead)
    latest_at = select(func.coalesce(EnrichmentRun.completed_at, EnrichmentRun.created_at)).where(
        EnrichmentRun.id == latest_run,
    ).correlate(Lead).scalar_subquery()
    available = latest_run.is_not(None)
    query = select(
        Lead, case((available, "available"), else_="unavailable").label("intelligence_status"),
        score_value.label("intelligence_score"), opportunity_count.label("opportunity_count"),
        recommendation_count.label("recommendation_count"), has_personalization.label("personalization_available"),
        latest_at.label("latest_enrichment_at"),
    )
    if status:
        query = query.where(Lead.status == status)
    if min_score is not None:
        query = query.where(score_value >= min_score)
    if max_score is not None:
        query = query.where(score_value <= max_score)
    if intelligence_status == "available":
        query = query.where(available)
    elif intelligence_status == "unavailable":
        query = query.where(~available)
    if personalization_available is not None:
        query = query.where(has_personalization == personalization_available)
    if opportunity_present is not None:
        query = query.where((opportunity_count > 0) == opportunity_present)
    if sales_status is not None:
        query = query.where(Lead.sales_status == sales_status)
    if priority is not None:
        query = query.where(Lead.priority == priority)
    if follow_up_due is True:
        query = query.where(Lead.next_follow_up_at.is_not(None), Lead.next_follow_up_at <= now_utc())
    elif follow_up_due is False:
        query = query.where((Lead.next_follow_up_at.is_(None)) | (Lead.next_follow_up_at > now_utc()))
    if min_score is not None and max_score is not None and min_score > max_score:
        raise HTTPException(422, detail={"code": "VALIDATION_ERROR", "message": "min_score cannot exceed max_score"})
    rows = (await db.execute(query.order_by(Lead.created_at.desc(), Lead.id.desc())
        .offset((page - 1) * page_size).limit(page_size))).all()
    results = []
    for lead, intel_status, score, opportunities, recommendations, has_personalization_value, enriched_at in rows:
        result = LeadOut.model_validate({
            **{column.name: getattr(lead, column.name) for column in Lead.__table__.columns},
            "id": str(lead.id), "status": lead.status.value,
            "sales_status": lead.sales_status.value, "priority": lead.priority.value,
            "intelligence_status": intel_status, "intelligence_score": score,
            "opportunity_count": opportunities, "recommendation_count": recommendations,
            "personalization_available": has_personalization_value,
            "latest_enrichment_at": enriched_at,
        })
        results.append(result)
    return results

@app.get("/api/leads/{lead_id}", response_model=LeadOut)
async def get_lead(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Fetch full lead intelligence and recent processing history."""
    lead = await db.get(Lead, lead_id)
    if not lead: raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    summary = await read_intelligence_summary(db, lead_id)
    return LeadOut.model_validate({**{c.name: getattr(lead, c.name) for c in Lead.__table__.columns}, "id": str(lead.id), "status": lead.status.value, **summary.model_dump()})


def sales_activity_payload(activity):
    suggestion = suggested_task_type(activity.outcome) if activity.outcome else None
    return SalesActivityOut.model_validate({"id": str(activity.id), "lead_id": str(activity.lead_id), "activity_type": activity.activity_type, "occurred_at": activity.occurred_at, "note": activity.note, "outcome": activity.outcome, "suggested_task_type": suggestion.value if suggestion else None, "old_sales_status": activity.old_sales_status, "new_sales_status": activity.new_sales_status, "outreach_execution_id": str(activity.outreach_execution_id) if activity.outreach_execution_id else None})


@app.get("/api/leads/{lead_id}/sales", response_model=SalesOut)
async def get_lead_sales(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    activities = (await db.scalars(select(SalesActivity).where(SalesActivity.lead_id == lead_id).order_by(SalesActivity.occurred_at.desc(), SalesActivity.id.desc()).limit(50))).all()
    return SalesOut(sales_status=lead.sales_status, priority=lead.priority, assigned_to=lead.assigned_to, next_follow_up_at=lead.next_follow_up_at, lost_reason=lead.lost_reason, recent_activities=[sales_activity_payload(item) for item in activities])


@app.patch("/api/leads/{lead_id}/sales", response_model=SalesOut)
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


@app.get("/api/leads/{lead_id}/activities", response_model=list[SalesActivityOut])
async def list_sales_activities(lead_id: uuid.UUID, limit: int = Query(50, ge=1, le=200), db: AsyncSession = Depends(get_db)):
    if await db.get(Lead, lead_id) is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    activities = (await db.scalars(select(SalesActivity).where(SalesActivity.lead_id == lead_id).order_by(SalesActivity.occurred_at.desc(), SalesActivity.id.desc()).limit(limit))).all()
    return [sales_activity_payload(item) for item in activities]


@app.post("/api/leads/{lead_id}/activities", response_model=SalesActivityOut, status_code=201)
async def create_sales_activity(lead_id: uuid.UUID, body: SalesActivityCreate, db: AsyncSession = Depends(get_db)):
    if await db.get(Lead, lead_id) is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    activity = SalesActivity(lead_id=lead_id, activity_type=body.activity_type, note=body.note, outcome=body.outcome)
    db.add(activity)
    await db.commit()
    await db.refresh(activity)
    return sales_activity_payload(activity)


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


@app.get("/api/leads/{lead_id}/commercial", response_model=CommercialOut)
async def get_lead_commercial(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    return await commercial_values(db, lead)


@app.patch("/api/leads/{lead_id}/commercial", response_model=CommercialOut)
async def patch_lead_commercial(lead_id: uuid.UUID, body: CommercialPatch, db: AsyncSession = Depends(get_db)):
    lead = await db.scalar(select(Lead).where(Lead.id == lead_id).with_for_update())
    if lead is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    try:
        lead = await update_commercial(db, lead, body)
    except RevenueError as error:
        raise_revenue_error(error)
    return await commercial_values(db, lead)


@app.get("/api/leads/{lead_id}/collections", response_model=list[SalesCollectionOut])
async def list_lead_collections(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    rows = (await db.scalars(select(SalesCollection).where(SalesCollection.lead_id == lead_id).order_by(SalesCollection.collection_date.desc(), SalesCollection.id.desc()))).all()
    return [sales_collection_payload(row, lead.company_name) for row in rows]


@app.post("/api/leads/{lead_id}/collections", response_model=SalesCollectionOut, status_code=201)
async def create_lead_collection(lead_id: uuid.UUID, body: SalesCollectionCreate, db: AsyncSession = Depends(get_db)):
    lead = await db.scalar(select(Lead).where(Lead.id == lead_id).with_for_update())
    if lead is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    try:
        collection = await create_collection(db, lead, body)
    except RevenueError as error:
        raise_revenue_error(error)
    return sales_collection_payload(collection, lead.company_name)


@app.get("/api/revenue/collections", response_model=list[SalesCollectionOut])
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


@app.patch("/api/collections/{collection_id}", response_model=SalesCollectionOut)
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


@app.delete("/api/collections/{collection_id}", status_code=204)
async def delete_sales_collection(collection_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    collection = await db.get(SalesCollection, collection_id)
    if collection is None:
        raise HTTPException(404, detail={"code": "COLLECTION_NOT_FOUND", "message": "Collection not found"})
    await db.delete(collection)
    await db.commit()
    return None


@app.get("/api/revenue/summary", response_model=RevenueSummaryOut)
async def get_revenue_summary(currency: str = Query("INR", min_length=3, max_length=3), db: AsyncSession = Depends(get_db)):
    try:
        return await revenue_summary(db, currency)
    except RevenueError as error:
        raise_revenue_error(error)


@app.get("/api/revenue/pipeline", response_model=list[PipelineStageOut])
async def get_revenue_pipeline(currency: str = Query("INR", min_length=3, max_length=3), db: AsyncSession = Depends(get_db)):
    try:
        return await pipeline_summary(db, currency)
    except RevenueError as error:
        raise_revenue_error(error)


@app.get("/api/revenue/conversion", response_model=ConversionOut)
async def get_revenue_conversion(currency: str = Query("INR", min_length=3, max_length=3), db: AsyncSession = Depends(get_db)):
    try:
        return await conversion_analytics(db, currency)
    except RevenueError as error:
        raise_revenue_error(error)


@app.get("/api/revenue/sources", response_model=SourceAnalyticsOut)
async def get_revenue_sources():
    return SourceAnalyticsOut(available=False, reason="Lead records do not store a reliable acquisition source, so source attribution is not calculated.", sources=[])


@app.get("/api/revenue/targets", response_model=list[RevenueTargetOut])
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


@app.post("/api/revenue/targets", response_model=RevenueTargetOut, status_code=201)
async def post_revenue_target(body: RevenueTargetCreate, db: AsyncSession = Depends(get_db)):
    try:
        return await create_target(db, body)
    except RevenueError as error:
        raise_revenue_error(error)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code": "ACTIVE_TARGET_EXISTS", "message": "An active target already exists for this currency"})


@app.patch("/api/revenue/targets/{target_id}", response_model=RevenueTargetOut)
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


@app.get("/api/leads/{lead_id}/tasks", response_model=list[SalesActionTaskOut])
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


@app.post("/api/leads/{lead_id}/tasks", response_model=SalesTaskOut, status_code=201)
async def create_lead_task(lead_id: uuid.UUID, body: SalesTaskCreate, db: AsyncSession = Depends(get_db)):
    try:
        task = await create_task(db, lead_id, body)
    except SalesTaskError as error:
        _raise_task_error(error)
    return sales_task_payload(task)


@app.get("/api/tasks", response_model=list[SalesActionTaskOut])
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


@app.get("/api/tasks/summary", response_model=SalesTaskSummaryOut)
async def task_summary(db: AsyncSession = Depends(get_db)):
    rows = await action_center_rows(db)
    return {
        name: [_action_task_payload(task, lead) for task, lead in rows[name]]
        for name in ("today", "overdue", "upcoming")
    } | {
        "outcome_suggestions": [_outcome_suggestion_payload(activity, lead) for activity, lead in rows["outcome_suggestions"]],
        "seo_opportunities": await read_seo_sales_opportunities(db),
    }


@app.patch("/api/tasks/{task_id}", response_model=SalesTaskOut)
async def patch_sales_task(task_id: uuid.UUID, body: SalesTaskPatch, db: AsyncSession = Depends(get_db)):
    task = await _task_by_id(db, task_id, lock=True)
    try:
        task = await update_task(db, task, body)
    except SalesTaskError as error:
        _raise_task_error(error)
    return sales_task_payload(task)


@app.post("/api/tasks/{task_id}/complete", response_model=SalesTaskOut)
async def complete_sales_task(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    task = await _task_by_id(db, task_id, lock=True)
    try:
        task = await finish_task(db, task, cancel=False)
    except SalesTaskError as error:
        _raise_task_error(error)
    return sales_task_payload(task)


@app.post("/api/tasks/{task_id}/cancel", response_model=SalesTaskOut)
async def cancel_sales_task(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    task = await _task_by_id(db, task_id, lock=True)
    try:
        task = await finish_task(db, task, cancel=True)
    except SalesTaskError as error:
        _raise_task_error(error)
    return sales_task_payload(task)




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


@app.get("/api/leads/{lead_id}/outreach", response_model=OutreachReadOut)
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


@app.post("/api/leads/{lead_id}/outreach/generate", response_model=OutreachArtifactOut)
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


@app.patch("/api/leads/{lead_id}/outreach", response_model=OutreachArtifactOut)
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


@app.post("/api/leads/{lead_id}/outreach/approve", response_model=OutreachArtifactOut)
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


@app.post("/api/leads/{lead_id}/outreach/reject", response_model=OutreachArtifactOut)
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


def _raise_outreach_operation_error(error: OutreachOperationError):
    raise HTTPException(error.status_code, detail={"code": error.code, "message": str(error)})


def _outreach_execution_payload(execution: OutreachExecution):
    return OutreachExecutionOut.model_validate(execution)


@app.get("/api/leads/{lead_id}/outreach/executions", response_model=list[OutreachExecutionOut])
async def list_lead_outreach_executions(lead_id: uuid.UUID, limit: int = Query(100, ge=1, le=500), db: AsyncSession = Depends(get_db)):
    if await db.get(Lead, lead_id) is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    rows = (await db.scalars(select(OutreachExecution).where(
        OutreachExecution.lead_id == lead_id,
    ).order_by(OutreachExecution.requested_at.desc(), OutreachExecution.id.desc()).limit(limit))).all()
    return [_outreach_execution_payload(item) for item in rows]


@app.post("/api/outreach/{artifact_id}/send", response_model=OutreachExecutionOut, status_code=201)
async def send_outreach_artifact(artifact_id: uuid.UUID, body: OutreachSendRequest, db: AsyncSession = Depends(get_db)):
    try:
        execution = await send_approved_outreach(db, artifact_id, body)
    except OutreachOperationError as error:
        _raise_outreach_operation_error(error)
    return _outreach_execution_payload(execution)


@app.post("/api/outreach/executions/{execution_id}/retry", response_model=OutreachExecutionOut, status_code=201)
async def retry_outreach_send(execution_id: uuid.UUID, body: OutreachRetryRequest, db: AsyncSession = Depends(get_db)):
    try:
        execution = await retry_outreach_execution(db, execution_id, body)
    except OutreachOperationError as error:
        _raise_outreach_operation_error(error)
    return _outreach_execution_payload(execution)


@app.post("/api/outreach/executions/{execution_id}/outcomes", response_model=SalesActivityOut, status_code=201)
async def create_outreach_outcome(execution_id: uuid.UUID, body: OutreachOutcomeCreate, db: AsyncSession = Depends(get_db)):
    try:
        activity = await record_outreach_outcome(db, execution_id, body)
    except OutreachOperationError as error:
        _raise_outreach_operation_error(error)
    return sales_activity_payload(activity)


@app.get("/api/outreach/analytics", response_model=OutreachAnalyticsResponse)
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


@app.post("/api/outreach/executions/{execution_id}/mark-manual", response_model=OutreachExecutionOut)
async def mark_outreach_manually_sent(execution_id: uuid.UUID, body: ManualSendConfirmation, db: AsyncSession = Depends(get_db)):
    try:
        execution = await confirm_manual_send(db, execution_id, body)
    except OutreachOperationError as error:
        _raise_outreach_operation_error(error)
    return _outreach_execution_payload(execution)


@app.post("/api/outreach/executions/{execution_id}/cancel", response_model=OutreachExecutionOut)
async def cancel_outreach_execution(execution_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    try:
        execution = await cancel_manual_execution(db, execution_id)
    except OutreachOperationError as error:
        _raise_outreach_operation_error(error)
    return _outreach_execution_payload(execution)


@app.get("/api/leads/{lead_id}/intelligence", response_model=IntelligenceResponse)
async def get_lead_intelligence(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Return persisted intelligence from one completed enrichment run; never invokes a provider."""
    if await db.get(Lead, lead_id) is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    response = await read_lead_intelligence(db, lead_id)
    if response is None:
        raise HTTPException(409, detail={
            "code": "INTELLIGENCE_NOT_AVAILABLE",
            "message": "No completed enrichment run with valid persisted intelligence is available",
        })
    return response


@app.post("/api/leads/{lead_id}/seo/runs", response_model=SeoRunCreatedOut, status_code=202)
async def start_seo_audit(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Queue a manually requested, independent SEO audit."""
    try:
        run = await create_seo_run(db, lead_id)
    except SeoServiceError as error:
        raise HTTPException(error.status_code, detail={"code": error.code, "message": error.message})
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code": "SEO_AUDIT_ALREADY_ACTIVE", "message": "An SEO audit is already queued or running for this lead"})
    return {"run": run}


@app.get("/api/leads/{lead_id}/seo", response_model=LatestSeoOut)
async def latest_seo_audit(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    if await db.get(Lead, lead_id) is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    run = await db.scalar(select(SeoAuditRun).where(SeoAuditRun.lead_id == lead_id, SeoAuditRun.status == "COMPLETED").order_by(SeoAuditRun.created_at.desc(), SeoAuditRun.id.desc()).limit(1))
    findings = [] if run is None else (await db.execute(select(SeoFinding).where(SeoFinding.seo_run_id == run.id).order_by(SeoFinding.severity, SeoFinding.finding_code, SeoFinding.id))).scalars().all()
    return {"audit": run, "findings": findings}


def _ai_sales_intelligence_payload(artifact, stale: bool):
    current = artifact.status == "validated" and not stale
    return AISalesIntelligenceOut(
        id=artifact.id, lead_id=artifact.lead_id,
        context_version=artifact.context_version,
        contract_version=artifact.contract_version,
        prompt_version=artifact.prompt_version,
        provider=artifact.provider, model=artifact.model, status=artifact.status,
        source_fingerprint=artifact.source_fingerprint, is_stale=stale,
        is_current=current, generated_at=artifact.generated_at,
        created_at=artifact.created_at, generated=artifact.generated,
        evidence_catalog=tuple(artifact.evidence_catalog or ()),
        evidence_refs=tuple(artifact.evidence_refs or ()),
        usage_metadata=artifact.usage_metadata or {},
        error_code=artifact.error_code, error_message=artifact.error_message,
    )


@app.post("/api/leads/{lead_id}/ai-sales-intelligence", response_model=AISalesIntelligenceOut, status_code=201)
async def generate_lead_ai_sales_intelligence(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    try:
        artifact = await generate_ai_sales_intelligence(db, lead_id)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    return _ai_sales_intelligence_payload(artifact, False)


@app.get("/api/leads/{lead_id}/ai-sales-intelligence", response_model=AISalesIntelligenceOut | None)
async def get_lead_ai_sales_intelligence(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    try:
        artifact, stale = await current_ai_sales_intelligence(db, lead_id)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    return _ai_sales_intelligence_payload(artifact, stale) if artifact is not None else None


@app.get("/api/leads/{lead_id}/ai-sales-intelligence/history", response_model=AISalesIntelligenceHistoryOut)
async def get_lead_ai_sales_intelligence_history(
    lead_id: uuid.UUID, limit: int = Query(50, ge=1, le=100), db: AsyncSession = Depends(get_db),
):
    try:
        rows = await ai_sales_intelligence_history(db, lead_id, limit)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    return {"items": [_ai_sales_intelligence_payload(artifact, stale) for artifact, stale in rows]}


@app.get("/api/leads/{lead_id}/ai-sales-intelligence/history/{artifact_id}", response_model=AISalesIntelligenceOut)
async def get_lead_ai_sales_intelligence_artifact(
    lead_id: uuid.UUID, artifact_id: uuid.UUID, db: AsyncSession = Depends(get_db),
):
    try:
        artifact, stale = await ai_sales_intelligence_by_id(db, lead_id, artifact_id)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    if artifact is None:
        raise HTTPException(404, detail={"code": "AI_SALES_INTELLIGENCE_NOT_FOUND", "message": "AI sales intelligence artifact not found for this lead"})
    return _ai_sales_intelligence_payload(artifact, stale)


def _ai_sales_copilot_payload(artifact, fingerprint: str) -> CopilotInteractionOut:
    return CopilotInteractionOut(
        id=artifact.id, lead_id=artifact.lead_id, question=artifact.question,
        intent=artifact.intent, context_version=artifact.context_version,
        contract_version=artifact.contract_version, prompt_version=artifact.prompt_version,
        provider=artifact.provider, model=artifact.model, status=artifact.status,
        source_fingerprint=artifact.source_fingerprint,
        is_stale=artifact.source_fingerprint != fingerprint,
        response=artifact.response,
        evidence_catalog=tuple(artifact.evidence_catalog or ()),
        usage_metadata=artifact.usage_metadata or {}, error_code=artifact.error_code,
        error_message=artifact.error_message, created_at=artifact.created_at,
    )


@app.post("/api/leads/{lead_id}/ai-copilot", response_model=CopilotInteractionOut, status_code=201)
async def ask_lead_ai_copilot(
    lead_id: uuid.UUID, request: CopilotAsk, db: AsyncSession = Depends(get_db),
):
    try:
        artifact = await ask_ai_sales_copilot(db, lead_id, request.question)
        return _ai_sales_copilot_payload(artifact, artifact.source_fingerprint)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None


@app.get("/api/leads/{lead_id}/ai-copilot/history", response_model=CopilotHistoryOut)
async def get_lead_ai_copilot_history(
    lead_id: uuid.UUID, limit: int = Query(50, ge=1, le=100), db: AsyncSession = Depends(get_db),
):
    try:
        artifacts, fingerprint = await ai_sales_copilot_history(db, lead_id, limit)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    return {"items": [_ai_sales_copilot_payload(item, fingerprint) for item in artifacts]}


def _ai_sales_action_plan_payload(artifact, fingerprint: str) -> ActionPlanOut:
    stale = artifact.source_fingerprint != fingerprint
    return ActionPlanOut(
        id=artifact.id, lead_id=artifact.lead_id,
        contract_version=artifact.contract_version, prompt_version=artifact.prompt_version,
        context_version=artifact.context_version, provider=artifact.provider, model=artifact.model,
        status=artifact.status, source_fingerprint=artifact.source_fingerprint,
        is_stale=stale, is_current=not stale and artifact.status == "validated",
        action_plan=artifact.action_plan,
        evidence_catalog=tuple(artifact.evidence_catalog or ()),
        evidence_refs=tuple(artifact.evidence_refs or ()),
        usage_metadata=artifact.usage_metadata or {}, error_code=artifact.error_code,
        error_message=artifact.error_message, created_at=artifact.created_at,
        updated_at=artifact.updated_at,
    )


@app.post("/api/leads/{lead_id}/ai-sales-action-plan", response_model=ActionPlanOut, status_code=201)
async def create_lead_ai_sales_action_plan(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    try:
        artifact = await generate_action_plan(db, lead_id)
        return _ai_sales_action_plan_payload(artifact, artifact.source_fingerprint)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None


@app.get("/api/leads/{lead_id}/ai-sales-action-plan", response_model=ActionPlanOut | None)
async def get_lead_ai_sales_action_plan(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    try:
        artifact, fingerprint = await current_action_plan(db, lead_id)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    return _ai_sales_action_plan_payload(artifact, fingerprint) if artifact else None


@app.get("/api/leads/{lead_id}/ai-sales-action-plan/history", response_model=ActionPlanHistoryOut)
async def get_lead_ai_sales_action_plan_history(
    lead_id: uuid.UUID, limit: int = Query(50, ge=1, le=100), db: AsyncSession = Depends(get_db),
):
    try:
        rows, fingerprint = await action_plan_history(db, lead_id, limit)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    return {"items": [_ai_sales_action_plan_payload(item, fingerprint) for item in rows]}


@app.get("/api/leads/{lead_id}/ai-sales-action-plan/history/{plan_id}", response_model=ActionPlanOut)
async def get_lead_ai_sales_action_plan_artifact(
    lead_id: uuid.UUID, plan_id: uuid.UUID, db: AsyncSession = Depends(get_db),
):
    try:
        artifact, fingerprint = await action_plan_by_id(db, lead_id, plan_id)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    if artifact is None:
        raise HTTPException(404, detail={"code": "AI_SALES_ACTION_PLAN_NOT_FOUND", "message": "Action plan not found for this lead"})
    return _ai_sales_action_plan_payload(artifact, fingerprint)


def _ai_proposal_intelligence_payload(artifact, fingerprint: str) -> ProposalIntelligenceOut:
    stale = artifact.source_fingerprint != fingerprint
    return ProposalIntelligenceOut(
        id=artifact.id, lead_id=artifact.lead_id,
        contract_version=artifact.contract_version, prompt_version=artifact.prompt_version,
        context_version=artifact.context_version, provider=artifact.provider, model=artifact.model,
        status=artifact.status, source_fingerprint=artifact.source_fingerprint,
        is_stale=stale, is_current=not stale and artifact.status == "validated",
        proposal_brief=artifact.proposal_brief,
        evidence_catalog=tuple(artifact.evidence_catalog or ()),
        evidence_refs=tuple(artifact.evidence_refs or ()),
        usage_metadata=artifact.usage_metadata or {}, error_code=artifact.error_code,
        error_message=artifact.error_message, created_at=artifact.created_at,
        updated_at=artifact.updated_at,
    )


@app.post("/api/leads/{lead_id}/ai-proposal", response_model=ProposalIntelligenceOut, status_code=201)
async def create_lead_ai_proposal(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    try:
        artifact = await generate_proposal(db, lead_id)
        _, current_fingerprint = await proposal_history(db, lead_id, limit=1)
        return _ai_proposal_intelligence_payload(artifact, current_fingerprint)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None


@app.get("/api/leads/{lead_id}/ai-proposal", response_model=ProposalIntelligenceOut | None)
async def get_lead_ai_proposal(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    try:
        artifact, fingerprint = await current_proposal(db, lead_id)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    return _ai_proposal_intelligence_payload(artifact, fingerprint) if artifact else None


@app.get("/api/leads/{lead_id}/ai-proposal/history", response_model=ProposalIntelligenceHistoryOut)
async def get_lead_ai_proposal_history(
    lead_id: uuid.UUID, limit: int = Query(50, ge=1, le=100), db: AsyncSession = Depends(get_db),
):
    try:
        rows, fingerprint = await proposal_history(db, lead_id, limit)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    return {"items": [_ai_proposal_intelligence_payload(item, fingerprint) for item in rows]}


@app.get("/api/leads/{lead_id}/ai-proposal/history/{artifact_id}", response_model=ProposalIntelligenceOut)
async def get_lead_ai_proposal_artifact(
    lead_id: uuid.UUID, artifact_id: uuid.UUID, db: AsyncSession = Depends(get_db),
):
    try:
        artifact, fingerprint = await proposal_by_id(db, lead_id, artifact_id)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None
    if artifact is None:
        raise HTTPException(404, detail={"code": "AI_PROPOSAL_NOT_FOUND", "message": "Proposal intelligence artifact not found for this lead"})
    return _ai_proposal_intelligence_payload(artifact, fingerprint)


@app.post("/api/leads/{lead_id}/agent-policy/evaluate", response_model=PolicyEvaluationOut, status_code=201)
async def evaluate_lead_agent_policy(
    lead_id: uuid.UUID, body: PolicyEvaluationRequest, db: AsyncSession = Depends(get_db),
):
    try:
        return await evaluate_policy(db, lead_id, body)
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None


@app.get("/api/leads/{lead_id}/agent-policy/history", response_model=PolicyEvaluationHistory)
async def get_lead_agent_policy_history(
    lead_id: uuid.UUID, limit: int = Query(50, ge=1, le=100), db: AsyncSession = Depends(get_db),
):
    try:
        return {"items": await policy_history(db, lead_id, limit)}
    except LookupError as error:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": str(error)}) from None


@app.get("/api/leads/{lead_id}/seo/opportunities", response_model=SeoSalesOpportunityList)
async def lead_seo_opportunities(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    if await db.get(Lead, lead_id) is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    return {"items": await read_seo_sales_opportunities(db, lead_id=lead_id)}


@app.get("/api/leads/{lead_id}/seo/runs", response_model=SeoRunListOut)
async def list_seo_runs(lead_id: uuid.UUID, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    if await db.get(Lead, lead_id) is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    rows = (await db.execute(select(SeoAuditRun).where(SeoAuditRun.lead_id == lead_id).order_by(SeoAuditRun.created_at.desc(), SeoAuditRun.id.desc()).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"items": rows, "page": page, "page_size": page_size}


@app.get("/api/seo/runs/{run_id}", response_model=SeoRunDetailOut)
async def seo_run_detail(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    run = await db.get(SeoAuditRun, run_id)
    if run is None:
        raise HTTPException(404, detail={"code": "SEO_RUN_NOT_FOUND", "message": "SEO audit run not found"})
    observations = (await db.execute(select(SeoObservation).where(SeoObservation.seo_run_id == run_id).order_by(SeoObservation.observation_type, SeoObservation.page_url, SeoObservation.id))).scalars().all()
    findings = (await db.execute(select(SeoFinding).where(SeoFinding.seo_run_id == run_id).order_by(SeoFinding.severity, SeoFinding.finding_code, SeoFinding.id))).scalars().all()
    return {"run": run, "observations": observations, "findings": findings}

@app.post("/api/leads/import")
async def import_leads(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    """Validate and import an uploaded CSV file."""
    if not file.filename or not file.filename.lower().endswith(".csv"): raise HTTPException(400, detail={"code":"INVALID_FILE","message":"Upload a CSV file"})
    content = await file.read(settings.max_csv_bytes + 1)
    if len(content) > settings.max_csv_bytes: raise HTTPException(413, detail={"code":"FILE_TOO_LARGE","message":"CSV exceeds maximum size"})
    try: return await import_csv(db, content, file.filename)
    except (ValueError, UnicodeDecodeError) as exc: raise HTTPException(422, detail={"code":"INVALID_CSV","message":str(exc)})

@app.post("/api/leads/{lead_id}/process")
async def process_lead(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Queue a single lead for durable worker processing."""
    lead = await db.get(Lead, lead_id)
    if not lead: raise HTTPException(404, detail={"code":"LEAD_NOT_FOUND","message":"Lead not found"})
    try:
        provider, model = validate_provider_model(None, None)
        job = await create_job(db, lead, provider, model)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(409, detail={"code":"JOB_NOT_CREATED","message":str(exc)})
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code":"LEAD_ALREADY_QUEUED","message":"This lead already has an active job"})
    return job_payload(job)

@app.post("/api/leads/process-batch")
async def process_batch(body: ProcessBatchIn, db: AsyncSession = Depends(get_db)):
    """Create a batch of durable jobs for worker execution."""
    size = min(body.batch_size, settings.max_batch_size)
    try: provider, model = validate_provider_model(body.provider, body.model)
    except ValueError as exc: raise HTTPException(422, detail={"code":"INVALID_PROVIDER_MODEL","message":str(exc)})
    leads = (await db.scalars(select(Lead).where(Lead.status.in_([LeadStatus.IMPORTED, LeadStatus.FAILED])).order_by(Lead.created_at).limit(size))).all()
    batch = LeadBatch(requested_count=len(leads), status="QUEUED"); db.add(batch); await db.flush()
    jobs = []
    try:
        for lead in leads: jobs.append(await create_job(db, lead, provider, model, batch.id, commit=False))
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(409, detail={"code":"JOB_NOT_CREATED","message":str(exc)})
    if not jobs: batch.status = "COMPLETED"
    try: await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code":"LEAD_ALREADY_QUEUED","message":"One or more leads already have active jobs"})
    return {"batch_id": str(batch.id), "requested": len(jobs), "jobs": [job_payload(j) for j in jobs]}

@app.get("/api/jobs/{job_id}")
async def get_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Fetch job status and checkpoint."""
    job = await db.get(ProcessingJob, job_id)
    if not job: raise HTTPException(404, detail={"code":"JOB_NOT_FOUND","message":"Job not found"})
    return job_payload(job)

@app.get("/api/jobs")
async def list_jobs(limit: int = Query(50, ge=1, le=200), db: AsyncSession = Depends(get_db)):
    """List newest processing jobs and associated lead names."""
    rows = (await db.execute(select(ProcessingJob, Lead.company_name).join(Lead).order_by(ProcessingJob.created_at.desc()).limit(limit))).all()
    return [{**job_payload(job), "lead": company} for job, company in rows]

@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Queue a failed job for worker execution from its saved checkpoint."""
    job = await db.get(ProcessingJob, job_id)
    if not job: raise HTTPException(404, detail={"code":"JOB_NOT_FOUND","message":"Job not found"})
    if job.status != JobStatus.FAILED: raise HTTPException(409, detail={"code":"JOB_NOT_RETRYABLE","message":"Only failed jobs can be manually retried"})
    competing = await db.scalar(select(ProcessingJob.id).where(ProcessingJob.lead_id == job.lead_id, ProcessingJob.id != job.id, ProcessingJob.status.in_([JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.RETRYING])))
    if competing: raise HTTPException(409, detail={"code":"LEAD_ALREADY_QUEUED","message":"This lead already has an active job"})
    job.status = JobStatus.QUEUED; job.attempt_count = 0; job.next_retry_at = None
    lead = await db.get(Lead, job.lead_id); lead.status = LeadStatus.QUEUED
    db.add(LeadEvent(lead_id=lead.id, job_id=job.id, event_type="JOB_MANUAL_RETRY", details={"checkpoint": job.checkpoint}))
    try: await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code":"LEAD_ALREADY_QUEUED","message":"This lead already has an active job"})
    await update_batch_status(db, job.batch_id)
    return job_payload(job)

@app.get("/api/dashboard/stats")
async def dashboard_stats(db: AsyncSession = Depends(get_db)):
    """Return summary counters for the dashboard."""
    total = await db.scalar(select(func.count()).select_from(Lead)) or 0
    counts = {status.value.lower(): await db.scalar(select(func.count()).select_from(Lead).where(Lead.status == status)) or 0 for status in LeadStatus}
    average = await db.scalar(select(func.avg(Lead.lead_score)))
    return {"total_leads": total, **counts, "average_lead_score": round(average, 1) if average is not None else None}

@app.get("/api/providers/status")
async def providers_status(db: AsyncSession = Depends(get_db)):
    """Report configured provider health without exposing credentials."""
    provider = get_provider()
    pool = ApiKeyPool()
    key_pools = {name: await pool.health(db, name) for name in ("openai", "gemini", "openai_compatible")}
    return {"active_provider": provider.name, "providers": [await provider.health_check()], "key_pools": key_pools}

def job_payload(job):
    """Serialize job fields safely."""
    safe_error = "Processing failed; details are available in server logs" if job.last_error else None
    return {"id": str(job.id), "lead_id": str(job.lead_id), "status": job.status.value, "attempt_count": job.attempt_count, "max_attempts": job.max_attempts, "last_error": safe_error, "started_at": job.started_at, "completed_at": job.completed_at, "next_retry_at": job.next_retry_at, "checkpoint": job.checkpoint, "provider": job.provider, "model": job.model, "created_at": job.created_at, "updated_at": job.updated_at}

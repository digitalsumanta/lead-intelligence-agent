"""FastAPI application and REST endpoints."""
import logging, uuid, hmac
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, UploadFile, File, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select, func, case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.database import get_db, SessionLocal, engine, verify_database_schema, worker_is_available
from app.models import (
    Lead, ImportJob, ProcessingJob, LeadEvent, LeadStatus, JobStatus, LeadBatch,
    EnrichmentRun, LeadScore, Opportunity, ServiceRecommendation, Personalization,
    SalesActivity, SalesStatus, SalesPriority, SalesActivityType,
)
from app.models.entities import now_utc
from app.schemas.lead import LeadOut, ProcessBatchIn
from app.schemas.sales import SalesPatch, SalesActivityCreate, SalesOut, SalesActivityOut
from app.schemas.intelligence import IntelligenceResponse
from app.services.lead_intelligence_api import read_intelligence_summary, read_lead_intelligence
from app.services.csv_import import import_csv
from app.services.jobs import create_job, update_batch_status
from app.services.key_pool import ApiKeyPool
from app.providers import get_provider, validate_provider_model
from app.utils.logging_config import configure_logging
from app.services.key_config import sync_environment_keys

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
    return SalesActivityOut.model_validate({"id": str(activity.id), "lead_id": str(activity.lead_id), "activity_type": activity.activity_type, "occurred_at": activity.occurred_at, "note": activity.note})


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
        db.add(SalesActivity(lead_id=lead.id, activity_type=SalesActivityType.STATUS_CHANGE, note=f"Sales status changed from {old_status.value} to {target.value}."))
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
    activity = SalesActivity(lead_id=lead_id, activity_type=body.activity_type, note=body.note)
    db.add(activity)
    await db.commit()
    await db.refresh(activity)
    return sales_activity_payload(activity)


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

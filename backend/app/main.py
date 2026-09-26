"""FastAPI application and REST endpoints."""
import logging, uuid, hmac
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, UploadFile, File, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.database import get_db, SessionLocal, engine, verify_database_schema, worker_is_available
from app.models import Lead, ImportJob, ProcessingJob, LeadEvent, LeadStatus, JobStatus, LeadBatch
from app.schemas.lead import LeadOut, ProcessBatchIn
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
async def list_leads(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), status: LeadStatus | None = None, db: AsyncSession = Depends(get_db)):
    """List leads newest first."""
    query = select(Lead)
    if status: query = query.where(Lead.status == status)
    rows = (await db.scalars(query.order_by(Lead.created_at.desc()).offset((page-1)*page_size).limit(page_size))).all()
    return [LeadOut.model_validate({**{c.name: getattr(x, c.name) for c in Lead.__table__.columns}, "id": str(x.id), "status": x.status.value}) for x in rows]

@app.get("/api/leads/{lead_id}", response_model=LeadOut)
async def get_lead(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Fetch full lead intelligence and recent processing history."""
    lead = await db.get(Lead, lead_id)
    if not lead: raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    return LeadOut.model_validate({**{c.name: getattr(lead, c.name) for c in Lead.__table__.columns}, "id": str(lead.id), "status": lead.status.value})

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

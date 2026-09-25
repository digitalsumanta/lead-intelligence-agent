"""FastAPI application and REST endpoints."""
import logging, uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, UploadFile, File, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.database import get_db, SessionLocal, engine
from app.models import Base, Lead, ImportJob, ProcessingJob, LeadEvent, LeadStatus, JobStatus, LeadBatch
from app.schemas.lead import LeadOut, ProcessBatchIn
from app.services.csv_import import import_csv
from app.services.jobs import create_job, run_job, process_many
from app.services.key_pool import ApiKeyPool
from app.providers import get_provider
from app.utils.logging_config import configure_logging
from app.services.key_config import sync_environment_keys

configure_logging()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables for local development; production schema uses Alembic."""
    # Dev convenience when a local database is already available. Production migrations remain authoritative.
    try:
        async with engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as session: await sync_environment_keys(session)
    except Exception as exc:
        logging.getLogger("lead_agent").warning("database startup check failed: %s", exc)
    yield
    await engine.dispose()

app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

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

@app.get("/api/leads", response_model=list[LeadOut])
async def list_leads(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100), status: str | None = None, db: AsyncSession = Depends(get_db)):
    """List leads newest first."""
    query = select(Lead)
    if status: query = query.where(Lead.status == status.upper())
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
    """Queue and run a single lead through the intelligence pipeline."""
    lead = await db.get(Lead, lead_id)
    if not lead: raise HTTPException(404, detail={"code":"LEAD_NOT_FOUND","message":"Lead not found"})
    job = await create_job(db, lead, settings.ai_provider, settings.ollama_model)
    await run_job(db, job)
    await db.refresh(job)
    return job_payload(job)

@app.post("/api/leads/process-batch")
async def process_batch(body: ProcessBatchIn, db: AsyncSession = Depends(get_db)):
    """Create and process a bounded batch of imported leads."""
    size = min(body.batch_size, settings.max_batch_size)
    leads = (await db.scalars(select(Lead).where(Lead.status.in_([LeadStatus.IMPORTED, LeadStatus.FAILED])).order_by(Lead.created_at).limit(size))).all()
    batch = LeadBatch(requested_count=len(leads)); db.add(batch); await db.flush()
    jobs = []
    for lead in leads: jobs.append(await create_job(db, lead, body.provider or settings.ai_provider, body.model or settings.ollama_model, batch.id))
    provider_instance = get_provider(body.provider, body.model) if jobs else None
    results = await process_many(SessionLocal, jobs, min(body.max_concurrency, settings.max_concurrency), provider_instance=provider_instance) if jobs else []
    return {"batch_id": str(batch.id), "requested": len(jobs), "jobs": [job_payload(j) for j in results]}

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
    """Reset an exhausted job and resume it from its saved checkpoint."""
    job = await db.get(ProcessingJob, job_id)
    if not job: raise HTTPException(404, detail={"code":"JOB_NOT_FOUND","message":"Job not found"})
    if job.status not in {JobStatus.FAILED, JobStatus.RETRYING}: raise HTTPException(409, detail={"code":"JOB_NOT_RETRYABLE","message":"Only failed jobs can be retried"})
    job.status = JobStatus.QUEUED; job.attempt_count = 0; job.next_retry_at = None
    lead = await db.get(Lead, job.lead_id); lead.status = LeadStatus.QUEUED
    await db.commit(); await run_job(db, job); await db.refresh(job)
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
    key_pools = {name: await pool.health(db, name) for name in ("openai", "gemini")}
    return {"active_provider": provider.name, "providers": [await provider.health_check()], "key_pools": key_pools}

def job_payload(job):
    """Serialize job fields safely."""
    return {"id": str(job.id), "lead_id": str(job.lead_id), "status": job.status.value, "attempt_count": job.attempt_count, "max_attempts": job.max_attempts, "last_error": job.last_error, "started_at": job.started_at, "completed_at": job.completed_at, "next_retry_at": job.next_retry_at, "checkpoint": job.checkpoint, "provider": job.provider, "model": job.model, "created_at": job.created_at, "updated_at": job.updated_at}

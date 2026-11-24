"""Processing jobs, dashboard statistics and provider status."""
import uuid

from app.api.common import job_payload
from app.database import (
    get_db,
)
from app.models import (
    JobStatus,
    Lead,
    LeadEvent,
    LeadStatus,
    ProcessingJob,
)
from app.providers import get_provider
from app.services.jobs import update_batch_status
from app.services.key_pool import ApiKeyPool
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Fetch job status and checkpoint."""
    job = await db.get(ProcessingJob, job_id)
    if not job: raise HTTPException(404, detail={"code":"JOB_NOT_FOUND","message":"Job not found"})
    return job_payload(job)


@router.get("/api/jobs")
async def list_jobs(limit: int = Query(50, ge=1, le=200), db: AsyncSession = Depends(get_db)):
    """List newest processing jobs and associated lead names."""
    rows = (await db.execute(select(ProcessingJob, Lead.company_name).join(Lead).order_by(ProcessingJob.created_at.desc()).limit(limit))).all()
    return [{**job_payload(job), "lead": company} for job, company in rows]


@router.post("/api/jobs/{job_id}/retry")
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


@router.get("/api/dashboard/stats")
async def dashboard_stats(db: AsyncSession = Depends(get_db)):
    """Return summary counters for the dashboard."""
    total = await db.scalar(select(func.count()).select_from(Lead)) or 0
    counts = {status.value.lower(): await db.scalar(select(func.count()).select_from(Lead).where(Lead.status == status)) or 0 for status in LeadStatus}
    average = await db.scalar(select(func.avg(Lead.lead_score)))
    return {"total_leads": total, **counts, "average_lead_score": round(average, 1) if average is not None else None}


@router.get("/api/providers/status")
async def providers_status(db: AsyncSession = Depends(get_db)):
    """Report configured provider health without exposing credentials."""
    provider = get_provider()
    pool = ApiKeyPool()
    key_pools = {name: await pool.health(db, name) for name in ("openai", "gemini", "openai_compatible")}
    return {"active_provider": provider.name, "providers": [await provider.health_check()], "key_pools": key_pools}

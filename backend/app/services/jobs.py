"""Durable lead processing, checkpointing, and retry policy."""
import asyncio, logging
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Lead, ProcessingJob, LeadEvent, LeadStatus, JobStatus, AgentRun, ProviderUsage
from app.models.entities import now_utc
from app.providers import get_provider
from app.services.website import WebsiteAnalyzer
from app.services.scoring import score_lead

logger = logging.getLogger("lead_agent")
RETRY_DELAYS = [5, 15, 60]
CHECKPOINTS = ["CREATED", "DATA_VALIDATED", "WEBSITE_ANALYZED", "AI_ANALYSIS_STARTED", "AI_ANALYSIS_COMPLETED", "SCORE_CALCULATED", "RESULT_SAVED", "COMPLETED"]

def retry_delay(attempt_count: int) -> int:
    """Return exponential retry delay for one based attempt count."""
    return RETRY_DELAYS[min(max(attempt_count - 1, 0), len(RETRY_DELAYS)-1)]

async def create_job(session: AsyncSession, lead: Lead, provider: str | None = None, model: str | None = None, batch_id=None) -> ProcessingJob:
    """Persist a new processing job and transition its lead to queued."""
    job = ProcessingJob(lead_id=lead.id, provider=provider or "ollama", model=model or "qwen2.5-coder:latest", batch_id=batch_id)
    lead.status = LeadStatus.QUEUED
    session.add(job)
    await session.flush()
    session.add(LeadEvent(lead_id=lead.id, job_id=job.id, event_type="JOB_CREATED", details={"checkpoint": job.checkpoint}))
    await session.commit()
    await session.refresh(job)
    return job

async def run_job(session: AsyncSession, job: ProcessingJob, analyzer: WebsiteAnalyzer | None = None, provider_instance=None) -> ProcessingJob:
    """Run/resume a job, persisting each expensive stage and safe retry state."""
    lead = await session.get(Lead, job.lead_id)
    if not lead: raise ValueError("Lead not found")
    analyzer = analyzer or WebsiteAnalyzer()
    job.attempt_count += 1; job.started_at = now_utc(); job.status = JobStatus.PROCESSING; job.last_error = None
    lead.status = LeadStatus.PROCESSING
    run = AgentRun(job_id=job.id); session.add(run); await session.commit()
    started = now_utc()
    try:
        if CHECKPOINTS.index(job.checkpoint) < CHECKPOINTS.index("DATA_VALIDATED"):
            if not lead.company_name.strip(): raise ValueError("Company name is required")
            job.checkpoint = "DATA_VALIDATED"; await session.commit()
        if CHECKPOINTS.index(job.checkpoint) < CHECKPOINTS.index("WEBSITE_ANALYZED"):
            lead.website_analysis = await analyzer.analyze(lead.website)
            job.checkpoint = "WEBSITE_ANALYZED"; await session.commit()
        if CHECKPOINTS.index(job.checkpoint) < CHECKPOINTS.index("AI_ANALYSIS_COMPLETED"):
            job.checkpoint = "AI_ANALYSIS_STARTED"; await session.commit()
            provider = provider_instance or get_provider(job.provider, job.model or None)
            payload = {k: getattr(lead, k) for k in ("company_name", "website", "industry", "city", "state", "country")}
            result = await provider.analyze_lead(payload, lead.website_analysis or {})
            data = result.model_dump()
            lead.ai_analysis = data; lead.ai_summary = data["company_summary"]
            lead.pain_points = data["pain_points"]; lead.keyword_opportunities = data["suggested_keywords"]
            lead.competitors = data["competitors"]; lead.recommended_services = data["recommended_services"]
            lead.sales_opening = data["personalized_sales_opening"]; lead.audit_video_script = data["audit_video_script"]
            lead.proposed_package = data["proposed_package"]; lead.confidence_score = data["confidence_score"]
            lead.decision_maker = data.get("decision_maker")
            job.checkpoint = "AI_ANALYSIS_COMPLETED"; await session.commit()
        if CHECKPOINTS.index(job.checkpoint) < CHECKPOINTS.index("SCORE_CALCULATED"):
            lead.lead_score, lead.score_factors = score_lead(lead, lead.website_analysis or {}, lead.ai_analysis or {})
            job.checkpoint = "SCORE_CALCULATED"; await session.commit()
        job.checkpoint = "RESULT_SAVED"; await session.commit()
        job.checkpoint = "COMPLETED"; job.status = JobStatus.COMPLETED; job.completed_at = now_utc(); job.next_retry_at = None
        lead.status = LeadStatus.COMPLETED; run.status = "COMPLETED"; run.completed_at = now_utc()
        session.add(LeadEvent(lead_id=lead.id, job_id=job.id, event_type="JOB_COMPLETED", details={"score": lead.lead_score}))
        session.add(ProviderUsage(provider=job.provider, model=job.model, job_id=job.id, success=True, duration_ms=int((now_utc()-started).total_seconds()*1000)))
        await session.commit()
        logger.info("job completed", extra={"job_id": str(job.id), "lead_id": str(lead.id), "provider": job.provider, "model": job.model, "checkpoint": job.checkpoint, "duration_ms": int((now_utc()-started).total_seconds()*1000)})
    except Exception as exc:
        retryable = not isinstance(exc, (ValueError,)) and job.attempt_count < job.max_attempts
        job.last_error = (str(exc) or type(exc).__name__)[:2000]
        job.status = JobStatus.RETRYING if retryable else JobStatus.FAILED
        lead.status = LeadStatus.RETRYING if retryable else LeadStatus.FAILED
        job.next_retry_at = now_utc() + timedelta(seconds=retry_delay(job.attempt_count)) if retryable else None
        run.status = job.status.value; run.completed_at = now_utc()
        session.add(LeadEvent(lead_id=lead.id, job_id=job.id, event_type="JOB_ERROR", details={"retryable": retryable, "error": job.last_error}))
        session.add(ProviderUsage(provider=job.provider, model=job.model, job_id=job.id, success=False, duration_ms=int((now_utc()-started).total_seconds()*1000)))
        await session.commit()
        logger.exception("job failed", extra={"job_id": str(job.id), "lead_id": str(lead.id), "provider": job.provider, "model": job.model, "checkpoint": job.checkpoint, "duration_ms": int((now_utc()-started).total_seconds()*1000)})
    return job

async def process_many(session_factory, jobs: list[ProcessingJob], concurrency: int, analyzer=None, provider_instance=None):
    """Run persisted jobs under a bounded semaphore using isolated sessions."""
    semaphore = asyncio.Semaphore(concurrency)
    async def one(job_id):
        async with semaphore:
            async with session_factory() as session:
                job = await session.get(ProcessingJob, job_id)
                return await run_job(session, job, analyzer=analyzer, provider_instance=provider_instance)
    return await asyncio.gather(*(one(job.id) for job in jobs))

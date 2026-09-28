"""Durable lead processing, checkpointing, and retry policy."""
import asyncio, logging
from datetime import timedelta
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from app.models import Lead, ProcessingJob, LeadEvent, LeadStatus, JobStatus, AgentRun, ProviderUsage, LeadBatch, LeadScore, EnrichmentRun, ServiceRecommendation, Opportunity
from app.models.entities import now_utc
from app.providers import get_provider
from app.providers import validate_provider_model
from app.services.website import WebsiteAnalyzer
from app.services.scoring import score_lead
from app.services.enrichment_pipeline import mark_expired_stage_retryable, run_enrichment_pipeline
from app.services.intelligence import derive_and_persist_intelligence
from app.services.personalization import generate_personalization

logger = logging.getLogger("lead_agent")
RETRY_DELAYS = [5, 15, 60]
CHECKPOINTS = ["CREATED", "DATA_VALIDATED", "WEBSITE_ANALYZED", "AI_ANALYSIS_STARTED", "AI_ANALYSIS_COMPLETED", "SCORE_CALCULATED", "RESULT_SAVED", "COMPLETED"]

def validate_checkpoint(checkpoint: str) -> int:
    """Return a known checkpoint position or raise a controlled permanent error."""
    try: return CHECKPOINTS.index(checkpoint)
    except ValueError as exc: raise ValueError("Unknown processing checkpoint") from exc

def advance_checkpoint(job: ProcessingJob, checkpoint: str) -> None:
    """Advance monotonically through the declared processing state machine."""
    target = validate_checkpoint(checkpoint)
    current = validate_checkpoint(job.checkpoint)
    if target < current:
        raise ValueError("Processing checkpoint cannot move backwards")
    job.checkpoint = checkpoint

def retry_delay(attempt_count: int) -> int:
    """Return exponential retry delay for one based attempt count."""
    return RETRY_DELAYS[min(max(attempt_count - 1, 0), len(RETRY_DELAYS)-1)]

async def create_job(session: AsyncSession, lead: Lead, provider: str | None = None, model: str | None = None, batch_id=None, commit: bool = True) -> ProcessingJob:
    """Add a job in the caller's transaction and transition its lead to queued."""
    chosen, chosen_model = validate_provider_model(provider, model)
    active = await session.scalar(select(ProcessingJob.id).where(ProcessingJob.lead_id == lead.id, ProcessingJob.status.in_([JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.RETRYING])))
    if active: raise ValueError("Lead already has an active processing job")
    job = ProcessingJob(lead_id=lead.id, provider=chosen, model=chosen_model, batch_id=batch_id)
    lead.status = LeadStatus.QUEUED
    session.add(job)
    await session.flush()
    session.add(LeadEvent(lead_id=lead.id, job_id=job.id, event_type="JOB_CREATED", details={"checkpoint": job.checkpoint}))
    if commit: await session.commit()
    else: await session.flush()
    return job

async def run_job(
    session: AsyncSession,
    job: ProcessingJob,
    analyzer: WebsiteAnalyzer | None = None,
    provider_instance=None,
    attempt_already_counted: bool = False,
    *,
    session_factory=None,
    google_places_provider=None,
    pagespeed_provider=None,
) -> ProcessingJob:
    """Run/resume a job, persisting each expensive stage and safe retry state."""
    lead = await session.get(Lead, job.lead_id)
    if not lead: raise ValueError("Lead not found")
    analyzer = analyzer or WebsiteAnalyzer()
    if not attempt_already_counted: job.attempt_count += 1
    job.started_at = now_utc(); job.status = JobStatus.PROCESSING; job.last_error = None
    lead.status = LeadStatus.PROCESSING
    run = AgentRun(job_id=job.id); session.add(run); await session.commit()
    started = now_utc()
    intelligence_score_applied = False
    try:
        pos = validate_checkpoint(job.checkpoint)
        if pos == CHECKPOINTS.index("COMPLETED"):
            job.status = JobStatus.COMPLETED
            job.completed_at = job.completed_at or now_utc()
            lead.status = LeadStatus.COMPLETED
            run.status = "COMPLETED"
            run.completed_at = now_utc()
            session.add(LeadEvent(lead_id=lead.id, job_id=job.id, event_type="JOB_RESUMED_FROM_COMPLETED_CHECKPOINT", details={}))
            await session.commit()
            await update_batch_status(session, job.batch_id)
            return job
        if pos < CHECKPOINTS.index("DATA_VALIDATED"):
            if not lead.company_name.strip(): raise ValueError("Company name is required")
            advance_checkpoint(job, "DATA_VALIDATED"); await session.commit()
        if validate_checkpoint(job.checkpoint) < CHECKPOINTS.index("WEBSITE_ANALYZED"):
            factory = session_factory or async_sessionmaker(session.bind, expire_on_commit=False)
            enrichment_run = await run_enrichment_pipeline(
                factory,
                job.id,
                lead.id,
                analyzer=analyzer,
                google_provider=google_places_provider,
                pagespeed_provider=pagespeed_provider,
            )
            await derive_and_persist_intelligence(factory, lead.id, enrichment_run.id)
            intelligence_score_applied = True
            await session.refresh(lead)
            advance_checkpoint(job, "WEBSITE_ANALYZED"); await session.commit()
        else:
            existing_score = await session.scalar(
                select(LeadScore.id)
                .join(EnrichmentRun, LeadScore.enrichment_run_id == EnrichmentRun.id)
                .where(EnrichmentRun.processing_job_id == job.id)
                .limit(1)
            )
            intelligence_score_applied = existing_score is not None
        if validate_checkpoint(job.checkpoint) < CHECKPOINTS.index("AI_ANALYSIS_COMPLETED"):
            advance_checkpoint(job, "AI_ANALYSIS_STARTED"); await session.commit()
            factory = session_factory or async_sessionmaker(session.bind, expire_on_commit=False)
            current_enrichment_run = await session.scalar(select(EnrichmentRun).where(
                EnrichmentRun.processing_job_id == job.id,
                EnrichmentRun.lead_id == lead.id,
            ).order_by(EnrichmentRun.created_at.desc()).limit(1))
            if current_enrichment_run is not None:
                provider = provider_instance or get_provider(job.provider, job.model)
                # Remove denormalized copy from an older run before generating this run's validated artifact.
                lead.ai_analysis = None
                lead.ai_summary = None
                lead.pain_points = None
                lead.keyword_opportunities = None
                lead.competitors = None
                lead.recommended_services = None
                lead.sales_opening = None
                lead.audit_video_script = None
                lead.proposed_package = None
                lead.confidence_score = None
                lead.decision_maker = None
                await session.commit()
                artifact = await generate_personalization(factory, lead.id, current_enrichment_run.id, provider,
                                                          raise_on_failure=True)
                if artifact is not None and artifact.status == "validated" and artifact.generated:
                    generated = artifact.generated
                    recommendations = list((await session.scalars(select(ServiceRecommendation).where(
                        ServiceRecommendation.enrichment_run_id == current_enrichment_run.id,
                        ServiceRecommendation.lead_id == lead.id,
                    ).order_by(ServiceRecommendation.rank))).all())
                    opportunities = list((await session.scalars(select(Opportunity).where(
                        Opportunity.enrichment_run_id == current_enrichment_run.id,
                        Opportunity.lead_id == lead.id,
                        Opportunity.origin == "deterministic",
                    ).order_by(Opportunity.category))).all())
                    sales_opening = generated["sales_opening"]["text"]
                    audit_summary = generated["audit_summary"]["text"]
                    video_script = generated["audit_video_script"]["text"]
                    package_language = generated["recommended_package_language"]["text"]
                    service_names = [row.service_name for row in recommendations]
                    data = {
                        "company_summary": audit_summary,
                        "business_category": lead.industry or "Unknown",
                        "website_problems": [row.title for row in opportunities if row.category in {"website_performance", "seo", "website_security"}],
                        "seo_opportunities": [row.title for row in opportunities if row.category == "seo"],
                        "digital_marketing_opportunities": [row.title for row in opportunities],
                        "pain_points": [row.title for row in opportunities],
                        "recommended_services": service_names,
                        "suggested_keywords": [], "competitors": [],
                        "personalized_sales_opening": sales_opening,
                        "audit_video_script": video_script,
                        "proposed_package": package_language,
                        "confidence_score": generated["confidence_score"],
                        "decision_maker": None,
                    }
                    lead.ai_analysis = data
                    lead.ai_summary = audit_summary
                    lead.pain_points = data["pain_points"]
                    lead.keyword_opportunities = data["suggested_keywords"]
                    lead.competitors = data["competitors"]
                    lead.recommended_services = service_names
                    lead.sales_opening = sales_opening
                    lead.audit_video_script = video_script
                    lead.proposed_package = package_language
                    lead.confidence_score = generated["confidence_score"]
                    lead.decision_maker = None
                else:
                    # Do not leave copy from an older run presented as current personalization.
                    lead.ai_analysis = None
                    lead.ai_summary = None
                    lead.pain_points = None
                    lead.keyword_opportunities = None
                    lead.competitors = None
                    lead.recommended_services = None
                    lead.sales_opening = None
                    lead.audit_video_script = None
                    lead.proposed_package = None
                    lead.confidence_score = None
                    lead.decision_maker = None
            else:
                # Historical jobs predating enrichment-run persistence retain their legacy recovery path.
                provider = provider_instance or get_provider(job.provider, job.model)
                payload = {k: getattr(lead, k) for k in ("company_name", "website", "industry", "city", "state", "country")}
                result = await provider.analyze_lead(payload, lead.website_analysis or {})
                data = result.model_dump()
                lead.ai_analysis = data; lead.ai_summary = data["company_summary"]
                lead.pain_points = data["pain_points"]; lead.keyword_opportunities = data["suggested_keywords"]
                lead.competitors = data["competitors"]; lead.recommended_services = data["recommended_services"]
                lead.sales_opening = data["personalized_sales_opening"]; lead.audit_video_script = data["audit_video_script"]
                lead.proposed_package = data["proposed_package"]; lead.confidence_score = data["confidence_score"]
                lead.decision_maker = data.get("decision_maker")
            advance_checkpoint(job, "AI_ANALYSIS_COMPLETED"); await session.commit()
        if validate_checkpoint(job.checkpoint) < CHECKPOINTS.index("SCORE_CALCULATED"):
            if not intelligence_score_applied:
                # Legacy checkpoints without an enrichment run retain the Phase 1 score path.
                lead.lead_score, lead.score_factors = score_lead(lead, lead.website_analysis or {}, lead.ai_analysis or {})
            advance_checkpoint(job, "SCORE_CALCULATED"); await session.commit()
        advance_checkpoint(job, "RESULT_SAVED"); await session.commit()
        advance_checkpoint(job, "COMPLETED"); job.status = JobStatus.COMPLETED; job.completed_at = now_utc(); job.next_retry_at = None
        lead.status = LeadStatus.COMPLETED; run.status = "COMPLETED"; run.completed_at = now_utc()
        session.add(LeadEvent(lead_id=lead.id, job_id=job.id, event_type="JOB_COMPLETED", details={"score": lead.lead_score}))
        # Job summary: request-level attempts are separately recorded by ProviderExecutionService
        # with operation/outcome/key fingerprint populated. Keep this operation-null row for job outcome history.
        session.add(ProviderUsage(provider=job.provider, model=job.model, job_id=job.id, success=True, duration_ms=int((now_utc()-started).total_seconds()*1000)))
        await session.commit()
        logger.info("job completed", extra={"job_id": str(job.id), "lead_id": str(lead.id), "provider": job.provider, "model": job.model, "checkpoint": job.checkpoint, "duration_ms": int((now_utc()-started).total_seconds()*1000)})
        await update_batch_status(session, job.batch_id)
    except Exception as exc:
        retryable = not isinstance(exc, (ValueError,)) and job.attempt_count < job.max_attempts
        job.last_error = (str(exc) or type(exc).__name__)[:2000]
        job.status = JobStatus.RETRYING if retryable else JobStatus.FAILED
        lead.status = LeadStatus.RETRYING if retryable else LeadStatus.FAILED
        job.next_retry_at = now_utc() + timedelta(seconds=retry_delay(job.attempt_count)) if retryable else None
        run.status = job.status.value; run.completed_at = now_utc()
        session.add(LeadEvent(lead_id=lead.id, job_id=job.id, event_type="JOB_ERROR", details={"retryable": retryable, "error": job.last_error}))
        # Job summary row; request-level failures retain their own operation-scoped executor rows.
        session.add(ProviderUsage(provider=job.provider, model=job.model, job_id=job.id, success=False, duration_ms=int((now_utc()-started).total_seconds()*1000)))
        await session.commit()
        await update_batch_status(session, job.batch_id)
        logger.exception("job failed", extra={"job_id": str(job.id), "lead_id": str(lead.id), "provider": job.provider, "model": job.model, "checkpoint": job.checkpoint, "duration_ms": int((now_utc()-started).total_seconds()*1000)})
    return job

async def update_batch_status(session: AsyncSession, batch_id) -> None:
    if batch_id is None: return
    batch = await session.get(LeadBatch, batch_id)
    if batch is None: return
    jobs = (await session.scalars(select(ProcessingJob).where(ProcessingJob.batch_id == batch_id))).all()
    statuses = [job.status for job in jobs]
    if any(s in {JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.RETRYING} for s in statuses):
        batch.status = "PROCESSING" if any(s == JobStatus.PROCESSING for s in statuses) else "QUEUED"
    elif statuses and all(s == JobStatus.COMPLETED for s in statuses): batch.status = "COMPLETED"
    elif statuses and all(s == JobStatus.FAILED for s in statuses): batch.status = "FAILED"
    else: batch.status = "PARTIALLY_FAILED"
    await session.commit()

async def recover_stale_jobs(session: AsyncSession, stale_before, now=None) -> None:
    """Count abandoned attempts and terminally fail exhausted jobs."""
    now = now or now_utc()
    jobs = (await session.scalars(select(ProcessingJob).where(ProcessingJob.status == JobStatus.PROCESSING, or_(ProcessingJob.started_at < stale_before, ProcessingJob.started_at.is_(None))))).all()
    for job in jobs:
        job.last_error = "Worker lease expired"
        job.status = JobStatus.RETRYING if job.attempt_count < job.max_attempts else JobStatus.FAILED
        job.next_retry_at = now + timedelta(seconds=retry_delay(job.attempt_count)) if job.status == JobStatus.RETRYING else None
        lead = await session.get(Lead, job.lead_id)
        if lead: lead.status = LeadStatus.RETRYING if job.status == JobStatus.RETRYING else LeadStatus.FAILED
        runs = (await session.scalars(select(AgentRun).where(AgentRun.job_id == job.id, AgentRun.status == "RUNNING"))).all()
        for run in runs:
            run.status = "ABANDONED"
            run.completed_at = now
        await mark_expired_stage_retryable(session, job.id, now)
        session.add(LeadEvent(lead_id=job.lead_id, job_id=job.id, event_type="STALE_JOB_RECOVERED", details={"attempt_count": job.attempt_count, "status": job.status.value}))
        await update_batch_status(session, job.batch_id)
    await session.commit()

async def claim_jobs(session: AsyncSession, limit: int, now=None) -> list:
    """Atomically claim due jobs using row locks with SKIP LOCKED on PostgreSQL."""
    now = now or now_utc()
    stmt = select(ProcessingJob).where(or_(ProcessingJob.status == JobStatus.QUEUED, (ProcessingJob.status == JobStatus.RETRYING) & (ProcessingJob.next_retry_at <= now))).order_by(ProcessingJob.created_at).limit(limit).with_for_update(skip_locked=True)
    jobs = (await session.scalars(stmt)).all()
    for job in jobs:
        job.status = JobStatus.PROCESSING
        job.started_at = now
        job.attempt_count += 1
        if job.batch_id:
            batch = await session.get(LeadBatch, job.batch_id)
            if batch: batch.status = "PROCESSING"
    await session.commit()
    return [job.id for job in jobs]

async def process_many(
    session_factory,
    jobs: list[ProcessingJob],
    concurrency: int,
    analyzer=None,
    provider_instance=None,
    attempt_already_counted: bool = False,
    *,
    google_places_provider=None,
    pagespeed_provider=None,
):
    """Run persisted jobs under a bounded semaphore using isolated sessions."""
    semaphore = asyncio.Semaphore(concurrency)
    async def one(job_id):
        async with semaphore:
            async with session_factory() as session:
                job = await session.get(ProcessingJob, job_id)
                return await run_job(
                    session,
                    job,
                    analyzer=analyzer,
                    provider_instance=provider_instance,
                    attempt_already_counted=attempt_already_counted,
                    session_factory=session_factory,
                    google_places_provider=google_places_provider,
                    pagespeed_provider=pagespeed_provider,
                )
    return await asyncio.gather(*(one(job.id) for job in jobs))

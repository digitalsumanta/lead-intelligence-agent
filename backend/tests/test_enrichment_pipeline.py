"""Durable checkpoint tests for the Phase 2 enrichment pipeline."""

from datetime import timedelta

import pytest
from sqlalchemy import select, func

from app.domain.enrichment import (
    EnrichmentStage,
    EnrichmentStageState,
    EvidenceInput,
    EvidenceSource,
    EvidenceSourceType,
    ProviderError,
    ProviderErrorType,
    ProviderIdentity,
    ProviderKind,
    ProviderRequestMetadata,
    ProviderResult,
    ProviderResultStatus,
)
from app.models import Evidence, Lead, ProcessingJob
from app.models.entities import EnrichmentRun as EnrichmentRunModel, now_utc
from app.services import enrichment_pipeline as pipeline
from app.services.jobs import recover_stale_jobs


IDENTITY = ProviderIdentity(name="mock", kind=ProviderKind.EXTERNAL_DATA, implementation="test")


class FakeAnalyzer:
    def __init__(self):
        self.calls = 0

    async def analyze(self, url):
        self.calls += 1
        return {
            "available": True,
            "url": url,
            "title": "Sample",
            "response_time_ms": 999,
        }


class FakeProvider:
    def __init__(self, *, error=None):
        self.calls = 0
        self.error = error

    async def collect(self, lead):
        self.calls += 1
        if self.error is not None:
            return ProviderResult(
                provider=IDENTITY,
                status=ProviderResultStatus.FAILED,
                metadata=ProviderRequestMetadata(operation="mock"),
                errors=(self.error,),
            )
        fetched = now_utc()
        evidence = EvidenceInput(
            lead_id=lead.lead_id,
            enrichment_run_id=lead.enrichment_run_id,
            evidence_type="mock_signal",
            source=EvidenceSource(source_type=EvidenceSourceType.OTHER_VERIFIED_SOURCE, provider=IDENTITY),
            observed_value="verified",
            observed_at=fetched,
            fetched_at=fetched,
            confidence=0.9,
        )
        return ProviderResult(
            provider=IDENTITY,
            status=ProviderResultStatus.SUCCEEDED,
            metadata=ProviderRequestMetadata(operation="mock"),
            evidence=(evidence,),
        )


async def setup_job(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Acme", website="https://example.com", city="Boston")
        session.add(lead)
        await session.flush()
        job = ProcessingJob(lead_id=lead.id)
        session.add(job)
        await session.commit()
        return lead.id, job.id


async def get_run(session_factory, job_id):
    async with session_factory() as session:
        return await session.scalar(select(EnrichmentRunModel).where(EnrichmentRunModel.processing_job_id == job_id))


@pytest.mark.asyncio
async def test_three_stage_pipeline_persists_evidence_before_completed_state(session_factory, monkeypatch):
    lead_id, job_id = await setup_job(session_factory)
    analyzer = FakeAnalyzer()
    google, pagespeed = FakeProvider(), FakeProvider()
    finish = pipeline._finish_stage
    checked = []

    async def check_evidence_first(factory, run_id, stage, token, state, **kwargs):
        if state == EnrichmentStageState.COMPLETED:
            async with factory() as session:
                count = await session.scalar(select(func.count(Evidence.id)).where(Evidence.enrichment_run_id == run_id))
                checked.append((stage, count))
                assert count > 0
        await finish(factory, run_id, stage, token, state, **kwargs)

    monkeypatch.setattr(pipeline, "_finish_stage", check_evidence_first)
    run = await pipeline.run_enrichment_pipeline(
        session_factory, job_id, lead_id, analyzer=analyzer,
        google_provider=google, pagespeed_provider=pagespeed,
    )
    assert run.status == "completed"
    assert analyzer.calls == google.calls == pagespeed.calls == 1
    assert {stage.value for stage, _ in checked} == {stage.value for stage in EnrichmentStage}
    assert all(record["state"] == "completed" for record in run.stage_states.values())
    async with session_factory() as session:
        rows = (await session.scalars(select(Evidence).where(Evidence.enrichment_run_id == run.id))).all()
        assert all(row.evidence_type != "website_response_time_ms" for row in rows)


@pytest.mark.asyncio
async def test_quota_failure_retries_only_page_speed_and_deduplicates_prior_evidence(session_factory):
    lead_id, job_id = await setup_job(session_factory)
    analyzer, google = FakeAnalyzer(), FakeProvider()
    pagespeed = FakeProvider(error=ProviderError(
        error_type=ProviderErrorType.QUOTA_EXHAUSTED,
        code="QUOTA_EXHAUSTED",
        message="Quota temporarily exhausted",
    ))
    with pytest.raises(pipeline.EnrichmentStageRetryable):
        await pipeline.run_enrichment_pipeline(
            session_factory, job_id, lead_id, analyzer=analyzer,
            google_provider=google, pagespeed_provider=pagespeed,
        )
    first = await get_run(session_factory, job_id)
    assert first.stage_states[EnrichmentStage.WEBSITE.value]["state"] == "completed"
    assert first.stage_states[EnrichmentStage.GOOGLE_PLACES.value]["state"] == "completed"
    assert first.stage_states[EnrichmentStage.PAGESPEED.value]["state"] == "retryable"
    pagespeed.error = None
    await pipeline.run_enrichment_pipeline(
        session_factory, job_id, lead_id, analyzer=analyzer,
        google_provider=google, pagespeed_provider=pagespeed,
    )
    assert analyzer.calls == 1 and google.calls == 1 and pagespeed.calls == 2
    async with session_factory() as session:
        rows = (await session.scalars(select(Evidence).where(Evidence.enrichment_run_id == first.id))).all()
        assert len(rows) == 4  # two website facts and one fact per successful provider


@pytest.mark.asyncio
async def test_no_match_is_completed_but_permanent_provider_error_is_recorded(session_factory):
    lead_id, job_id = await setup_job(session_factory)
    google = FakeProvider(error=ProviderError(
        error_type=ProviderErrorType.PERMANENT, code="NO_MATCH", message="No business matched",
    ))
    pagespeed = FakeProvider(error=ProviderError(
        error_type=ProviderErrorType.PERMANENT, code="BAD_REQUEST", message="Invalid request",
    ))
    run = await pipeline.run_enrichment_pipeline(
        session_factory, job_id, lead_id, analyzer=FakeAnalyzer(),
        google_provider=google, pagespeed_provider=pagespeed,
    )
    assert run.stage_states[EnrichmentStage.GOOGLE_PLACES.value]["state"] == "completed"
    assert run.stage_states[EnrichmentStage.GOOGLE_PLACES.value]["outcome"] == "no_match"
    assert run.stage_states[EnrichmentStage.PAGESPEED.value]["state"] == "failed"
    assert run.status == "partial"
    assert run.failure_summary["stages"][EnrichmentStage.PAGESPEED.value]["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_crash_after_evidence_commit_retries_stage_without_duplicate_rows(session_factory, monkeypatch):
    lead_id, job_id = await setup_job(session_factory)
    analyzer, google, pagespeed = FakeAnalyzer(), FakeProvider(), FakeProvider()
    original_finish = pipeline._finish_stage

    class SimulatedCrash(BaseException):
        pass

    async def crash_after_website_evidence(factory, run_id, stage, token, state, **kwargs):
        if stage == EnrichmentStage.WEBSITE:
            raise SimulatedCrash()
        await original_finish(factory, run_id, stage, token, state, **kwargs)

    monkeypatch.setattr(pipeline, "_finish_stage", crash_after_website_evidence)
    with pytest.raises(SimulatedCrash):
        await pipeline.run_enrichment_pipeline(
            session_factory, job_id, lead_id, analyzer=analyzer,
            google_provider=google, pagespeed_provider=pagespeed,
        )
    run = await get_run(session_factory, job_id)
    async with session_factory() as session:
        before = await session.scalar(select(func.count(Evidence.id)).where(
            Evidence.enrichment_run_id == run.id, Evidence.provider_name == "website_analyzer",
        ))
        run_row = await session.get(EnrichmentRunModel, run.id)
        run_row.stage_lease_expires_at = now_utc() - timedelta(seconds=1)
        await session.commit()
    monkeypatch.setattr(pipeline, "_finish_stage", original_finish)
    await pipeline.run_enrichment_pipeline(
        session_factory, job_id, lead_id, analyzer=analyzer,
        google_provider=google, pagespeed_provider=pagespeed,
    )
    async with session_factory() as session:
        after = await session.scalar(select(func.count(Evidence.id)).where(
            Evidence.enrichment_run_id == run.id, Evidence.provider_name == "website_analyzer",
        ))
    assert before == after
    assert analyzer.calls == 2


@pytest.mark.asyncio
async def test_stale_job_recovery_releases_expired_enrichment_stage(session_factory):
    lead_id, job_id = await setup_job(session_factory)
    run = await pipeline.ensure_enrichment_run(session_factory, job_id, lead_id)
    async with session_factory() as session:
        stored = await session.get(EnrichmentRunModel, run.id)
        stored.active_stage = EnrichmentStage.GOOGLE_PLACES.value
        stored.stage_lease_token = "expired"
        stored.stage_lease_expires_at = now_utc() - timedelta(seconds=2)
        stored.stage_states[EnrichmentStage.GOOGLE_PLACES.value] = {
            "stage": EnrichmentStage.GOOGLE_PLACES.value,
            "state": "running",
            "attempt": 1,
            "updated_at": now_utc().isoformat(),
        }
        job = await session.get(ProcessingJob, job_id)
        job.status = "PROCESSING"
        job.started_at = now_utc() - timedelta(minutes=20)
        await session.commit()
        await recover_stale_jobs(session, stale_before=now_utc() - timedelta(minutes=10))
        await session.commit()
        await session.refresh(stored)
        assert stored.active_stage is None
        assert stored.stage_states[EnrichmentStage.GOOGLE_PLACES.value]["state"] == "retryable"
        assert stored.stage_states[EnrichmentStage.GOOGLE_PLACES.value]["error_code"] == "STAGE_LEASE_EXPIRED"


@pytest.mark.asyncio
async def test_second_worker_cannot_claim_an_already_leased_stage(session_factory):
    lead_id, job_id = await setup_job(session_factory)
    run = await pipeline.ensure_enrichment_run(session_factory, job_id, lead_id)
    first = await pipeline._claim_stage(session_factory, run.id, EnrichmentStage.WEBSITE)
    assert first
    with pytest.raises(pipeline.EnrichmentStageBusy):
        await pipeline._claim_stage(session_factory, run.id, EnrichmentStage.WEBSITE)


@pytest.mark.asyncio
async def test_a_new_processing_job_gets_a_historical_enrichment_run(session_factory):
    lead_id, first_job_id = await setup_job(session_factory)
    providers = (FakeProvider(), FakeProvider())
    await pipeline.run_enrichment_pipeline(
        session_factory, first_job_id, lead_id, analyzer=FakeAnalyzer(),
        google_provider=providers[0], pagespeed_provider=providers[1],
    )
    async with session_factory() as session:
        first_job = await session.get(ProcessingJob, first_job_id)
        first_job.status = "COMPLETED"
        second_job = ProcessingJob(lead_id=lead_id)
        session.add(second_job)
        await session.commit()
        second_job_id = second_job.id
    await pipeline.run_enrichment_pipeline(
        session_factory, second_job_id, lead_id, analyzer=FakeAnalyzer(),
        google_provider=FakeProvider(), pagespeed_provider=FakeProvider(),
    )
    async with session_factory() as session:
        runs = (await session.scalars(select(EnrichmentRunModel).where(EnrichmentRunModel.lead_id == lead_id))).all()
        assert len(runs) == 2
        assert {run.processing_job_id for run in runs} == {first_job_id, second_job_id}

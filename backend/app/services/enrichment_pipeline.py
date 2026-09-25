"""Durable, stage-checkpointed orchestration for source evidence collection."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import logging
import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.domain.enrichment import (
    EnrichmentStage,
    EnrichmentStageRecord,
    EnrichmentStageState,
    EvidenceInput,
    EvidenceSource,
    EvidenceSourceType,
    LeadInput,
    ProviderErrorType,
    ProviderIdentity,
    ProviderKind,
    ProviderResult,
    ProviderResultStatus,
)
from app.models import Evidence, Lead
from app.models.entities import EnrichmentRun as EnrichmentRunModel, now_utc
from app.providers.google_places import GooglePlacesProvider
from app.providers.pagespeed import PageSpeedProvider
from app.services.provider_execution import ProviderExecutionService
from app.services.website import WebsiteAnalyzer


logger = logging.getLogger("lead_agent.enrichment")
PIPELINE_VERSION = "phase2-enrichment-v1"
STAGE_LEASE_SECONDS = 300
STAGE_ORDER = (EnrichmentStage.WEBSITE, EnrichmentStage.GOOGLE_PLACES, EnrichmentStage.PAGESPEED)

WEBSITE_IDENTITY = ProviderIdentity(
    name="website_analyzer",
    kind=ProviderKind.WEBSITE_ANALYZER,
    implementation="website_analyzer_v1",
)


class EnrichmentStageBusy(RuntimeError):
    """Another worker owns an unexpired durable lease for the active stage."""


class EnrichmentStageRetryable(RuntimeError):
    """The current stage should be retried by the existing durable job worker."""


def _stage_record(states: dict, stage: EnrichmentStage) -> EnrichmentStageRecord:
    raw = states.get(stage.value)
    if raw is None:
        return EnrichmentStageRecord(stage=stage, state=EnrichmentStageState.PENDING, updated_at=now_utc())
    if isinstance(raw, str):
        return EnrichmentStageRecord(stage=stage, state=EnrichmentStageState(raw), updated_at=now_utc())
    return EnrichmentStageRecord.model_validate(raw)


def _with_state(states: dict, record: EnrichmentStageRecord) -> dict:
    updated = dict(states or {})
    updated[record.stage.value] = record.model_dump(mode="json")
    return updated


def _failure_summary(states: dict) -> dict | None:
    failures = {}
    for stage in STAGE_ORDER:
        record = _stage_record(states or {}, stage)
        if record.state in {EnrichmentStageState.FAILED, EnrichmentStageState.RETRYABLE}:
            failures[stage.value] = {
                "state": record.state.value,
                "code": record.error_code,
                "message": record.failure_message,
            }
    return {"stages": failures} if failures else None


async def ensure_enrichment_run(session_factory: async_sessionmaker, job_id, lead_id) -> EnrichmentRunModel:
    """Return the job's existing run or create its single resumable run."""
    async with session_factory() as session:
        existing = await session.scalar(
            select(EnrichmentRunModel)
            .where(EnrichmentRunModel.processing_job_id == job_id)
            .order_by(EnrichmentRunModel.created_at.desc())
            .limit(1)
        )
        if existing is not None:
            return existing
        now = now_utc()
        initial_states = {
            stage.value: EnrichmentStageRecord(stage=stage, state=EnrichmentStageState.PENDING, updated_at=now).model_dump(mode="json")
            for stage in STAGE_ORDER
        }
        run = EnrichmentRunModel(
            lead_id=lead_id,
            processing_job_id=job_id,
            status="running",
            requested_stages=[stage.value for stage in STAGE_ORDER],
            completed_stages=[],
            started_at=now,
            pipeline_version=PIPELINE_VERSION,
            stage_states=initial_states,
        )
        session.add(run)
        try:
            await session.commit()
            await session.refresh(run)
            return run
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                select(EnrichmentRunModel)
                .where(EnrichmentRunModel.processing_job_id == job_id)
                .order_by(EnrichmentRunModel.created_at.desc())
                .limit(1)
            )
            if existing is None:
                raise
            return existing


async def mark_expired_stage_retryable(session, processing_job_id, now=None) -> None:
    """Release an expired stage lease when the durable job is recovered."""
    now = now or now_utc()
    runs = (await session.scalars(
        select(EnrichmentRunModel).where(
            EnrichmentRunModel.processing_job_id == processing_job_id,
            EnrichmentRunModel.status == "running",
            EnrichmentRunModel.active_stage.is_not(None),
            EnrichmentRunModel.stage_lease_expires_at <= now,
        )
    )).all()
    for run in runs:
        try:
            stage = EnrichmentStage(run.active_stage)
        except ValueError:
            continue
        current = _stage_record(run.stage_states or {}, stage)
        retryable = current.model_copy(update={
            "state": EnrichmentStageState.RETRYABLE,
            "updated_at": now,
            "outcome": "stage_lease_expired",
            "error_code": "STAGE_LEASE_EXPIRED",
            "failure_message": "Stage owner lease expired before checkpoint",
        })
        run.stage_states = _with_state(run.stage_states or {}, retryable)
        run.failure_summary = _failure_summary(run.stage_states)
        run.active_stage = None
        run.stage_lease_token = None
        run.stage_lease_expires_at = None
        run.updated_at = now


async def _claim_stage(session_factory: async_sessionmaker, run_id, stage: EnrichmentStage) -> str | None:
    """Atomically claim a pending/retryable stage or reclaim its expired lease."""
    async with session_factory() as session:
        run = await session.get(EnrichmentRunModel, run_id)
        if run is None:
            raise RuntimeError("Enrichment run not found")
        states = run.stage_states or {}
        current = _stage_record(states, stage)
        if current.state in {EnrichmentStageState.COMPLETED, EnrichmentStageState.SKIPPED, EnrichmentStageState.FAILED}:
            return None
        now = now_utc()
        token = str(uuid.uuid4())
        running = current.model_copy(update={
            "state": EnrichmentStageState.RUNNING,
            "attempt": current.attempt + 1,
            "updated_at": now,
            "outcome": None,
            "error_code": None,
            "failure_message": None,
        })
        result = await session.execute(
            update(EnrichmentRunModel)
            .where(
                EnrichmentRunModel.id == run_id,
                or_(EnrichmentRunModel.active_stage.is_(None), EnrichmentRunModel.stage_lease_expires_at <= now),
            )
            .values(
                status="running",
                completed_at=None,
                active_stage=stage.value,
                stage_lease_token=token,
                stage_lease_expires_at=now + timedelta(seconds=STAGE_LEASE_SECONDS),
                stage_states=_with_state(states, running),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await session.rollback()
            raise EnrichmentStageBusy("An enrichment stage is already leased by another worker")
        await session.commit()
        return token


async def _renew_stage_lease(session_factory: async_sessionmaker, run_id, token: str) -> None:
    now = now_utc()
    async with session_factory() as session:
        result = await session.execute(
            update(EnrichmentRunModel)
            .where(EnrichmentRunModel.id == run_id, EnrichmentRunModel.stage_lease_token == token)
            .values(stage_lease_expires_at=now + timedelta(seconds=STAGE_LEASE_SECONDS), updated_at=now)
        )
        if result.rowcount != 1:
            await session.rollback()
            raise EnrichmentStageBusy("Enrichment stage lease was superseded before result persistence")
        await session.commit()


def _content_hash(stage: EnrichmentStage, evidence: EvidenceInput) -> str:
    identity = evidence.source.provider
    stable = {
        "stage": stage.value,
        "evidence_type": evidence.evidence_type,
        "source_type": evidence.source.source_type.value,
        "provider": identity.model_dump(mode="json") if identity else None,
        "source_url": evidence.source_url,
        "external_provider_reference": evidence.external_provider_reference,
        "observed_value": evidence.observed_value,
        "confidence": evidence.confidence,
        "metadata": evidence.metadata,
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evidence_model(lead_id, run_id, stage: EnrichmentStage, record: EvidenceInput) -> Evidence:
    source = record.source
    provider = source.provider
    return Evidence(
        lead_id=lead_id,
        enrichment_run_id=run_id,
        evidence_type=record.evidence_type,
        source_type=source.source_type.value,
        provider_name=provider.name if provider else "unknown",
        provider_kind=provider.kind.value if provider else ProviderKind.EXTERNAL_DATA.value,
        provider_implementation=provider.implementation if provider else None,
        source_url=record.source_url,
        external_provider_reference=record.external_provider_reference,
        observed_value=record.observed_value,
        observed_at=record.observed_at,
        fetched_at=record.fetched_at,
        expires_at=record.expires_at,
        confidence=record.confidence,
        content_hash=_content_hash(stage, record),
        metadata_json={**record.metadata, "enrichment_stage": stage.value},
    )


async def _persist_stage_result(
    session_factory: async_sessionmaker,
    lead_id,
    run_id,
    stage: EnrichmentStage,
    evidence: tuple[EvidenceInput, ...] = (),
    website_analysis: dict | None = None,
) -> None:
    """Commit idempotent evidence (and website output) before stage completion."""
    async with session_factory() as session:
        hashes = {_content_hash(stage, record) for record in evidence}
        existing = set()
        if hashes:
            existing = set((await session.scalars(
                select(Evidence.content_hash).where(
                    Evidence.enrichment_run_id == run_id,
                    Evidence.content_hash.in_(hashes),
                )
            )).all())
        for record in evidence:
            if _content_hash(stage, record) not in existing:
                session.add(_evidence_model(lead_id, run_id, stage, record))
        if website_analysis is not None:
            lead = await session.get(Lead, lead_id)
            if lead is None:
                raise RuntimeError("Lead disappeared before website result persistence")
            lead.website_analysis = website_analysis
        await session.commit()


async def _finish_stage(
    session_factory: async_sessionmaker,
    run_id,
    stage: EnrichmentStage,
    token: str,
    state: EnrichmentStageState,
    *,
    outcome: str | None = None,
    error_code: str | None = None,
    failure_message: str | None = None,
) -> None:
    async with session_factory() as session:
        run = await session.get(EnrichmentRunModel, run_id)
        if run is None or run.stage_lease_token != token:
            raise EnrichmentStageBusy("Enrichment stage lease was superseded before checkpoint")
        current = _stage_record(run.stage_states or {}, stage)
        finished = EnrichmentStageRecord(
            stage=stage,
            state=state,
            attempt=current.attempt,
            updated_at=now_utc(),
            outcome=outcome,
            error_code=error_code,
            failure_message=failure_message[:1000] if failure_message else None,
        )
        run.stage_states = _with_state(run.stage_states or {}, finished)
        completed = set(run.completed_stages or [])
        if state in {EnrichmentStageState.COMPLETED, EnrichmentStageState.SKIPPED}:
            completed.add(stage.value)
        else:
            completed.discard(stage.value)
        run.completed_stages = [candidate.value for candidate in STAGE_ORDER if candidate.value in completed]
        run.failure_summary = _failure_summary(run.stage_states)
        run.active_stage = None
        run.stage_lease_token = None
        run.stage_lease_expires_at = None
        run.updated_at = now_utc()
        await session.commit()


def _website_evidence(lead: LeadInput, result: dict) -> tuple[EvidenceInput, ...]:
    fetched_at = now_utc()
    source_url = result.get("url") if isinstance(result.get("url"), str) else None
    # Analyzer failures can include the input URL; only attribute it if safe.
    if source_url:
        try:
            from app.services.website import validate_url_syntax
            validate_url_syntax(source_url)
        except ValueError:
            source_url = None
    source = EvidenceSource(source_type=EvidenceSourceType.WEBSITE, provider=WEBSITE_IDENTITY)
    values: list[tuple[str, object]] = [
        ("website_available", result.get("available")),
        ("website_http_status", result.get("status_code")),
        ("website_title", result.get("title")),
        ("website_meta_description", result.get("meta_description")),
        ("website_h1", result.get("h1")),
        ("website_https", result.get("https")),
    ]
    for issue in result.get("issues", []) if isinstance(result.get("issues"), list) else []:
        if isinstance(issue, str):
            values.append(("website_issue", issue[:500]))
    for link in result.get("social_links", []) if isinstance(result.get("social_links"), list) else []:
        if isinstance(link, str):
            values.append(("website_social_link", link[:1000]))
    records = []
    for evidence_type, value in values:
        if value is None or isinstance(value, (dict, list)):
            continue
        if not isinstance(value, (str, int, float, bool)):
            continue
        records.append(EvidenceInput(
            lead_id=lead.lead_id,
            enrichment_run_id=lead.enrichment_run_id,
            evidence_type=evidence_type,
            source=source,
            source_url=source_url,
            observed_value=value,
            observed_at=fetched_at,
            fetched_at=fetched_at,
            confidence=1.0,
            metadata={"enrichment_stage": EnrichmentStage.WEBSITE.value},
        ))
    return tuple(records)


def _provider_error_details(result: ProviderResult) -> tuple[bool, bool, str, str, str]:
    errors = result.errors
    retryable_types = {
        ProviderErrorType.TRANSIENT,
        ProviderErrorType.TIMEOUT,
        ProviderErrorType.RATE_LIMITED,
        ProviderErrorType.QUOTA_EXHAUSTED,
    }
    retryable = any(error.error_type in retryable_types for error in errors)
    skipped = any(error.code in {"PROVIDER_DISABLED", "MISSING_API_KEY"} for error in errors)
    no_match = bool(errors) and all(error.code in {"NO_MATCH", "AMBIGUOUS_MATCH"} for error in errors)
    first = errors[0] if errors else None
    code = first.code or first.error_type.value if first else "PROVIDER_PARTIAL"
    message = "; ".join(error.message for error in errors)[:1000]
    return retryable, skipped, "no_match" if no_match else code, code, message


async def run_enrichment_pipeline(
    session_factory: async_sessionmaker,
    job_id,
    lead_id,
    *,
    analyzer: WebsiteAnalyzer | None = None,
    google_provider=None,
    pagespeed_provider=None,
) -> EnrichmentRunModel:
    """Run or resume website, Google Places, and PageSpeed stages for one job."""
    analyzer = analyzer or WebsiteAnalyzer()
    run = await ensure_enrichment_run(session_factory, job_id, lead_id)
    if google_provider is None or pagespeed_provider is None:
        execution = ProviderExecutionService(session_factory)
        google_provider = google_provider or GooglePlacesProvider(execution)
        pagespeed_provider = pagespeed_provider or PageSpeedProvider(execution)
    async with session_factory() as session:
        lead_row = await session.get(Lead, lead_id)
        if lead_row is None:
            raise ValueError("Lead not found")
        lead_input = LeadInput(
            lead_id=lead_row.id,
            enrichment_run_id=run.id,
            company_name=lead_row.company_name,
            website=lead_row.website,
            phone=lead_row.phone,
            address=lead_row.address,
            city=lead_row.city,
            state=lead_row.state,
            country=lead_row.country,
        )

    for stage in STAGE_ORDER:
        token = await _claim_stage(session_factory, run.id, stage)
        if token is None:
            continue
        try:
            if stage == EnrichmentStage.WEBSITE:
                website_result = await analyzer.analyze(lead_input.website)
                if not isinstance(website_result, dict):
                    raise RuntimeError("Website analyzer returned an invalid result")
                records = _website_evidence(lead_input, website_result)
                await _renew_stage_lease(session_factory, run.id, token)
                await _persist_stage_result(session_factory, lead_id, run.id, stage, records, website_result)
                await _finish_stage(session_factory, run.id, stage, token, EnrichmentStageState.COMPLETED, outcome="website_analyzed")
            else:
                provider = google_provider if stage == EnrichmentStage.GOOGLE_PLACES else pagespeed_provider
                provider_result = await provider.collect(lead_input)
                if not isinstance(provider_result, ProviderResult):
                    raise RuntimeError("Enrichment provider returned an invalid result")
                await _renew_stage_lease(session_factory, run.id, token)
                await _persist_stage_result(session_factory, lead_id, run.id, stage, provider_result.evidence)
                if provider_result.status == ProviderResultStatus.FAILED or provider_result.errors:
                    retryable, skipped, outcome, code, message = _provider_error_details(provider_result)
                    if skipped:
                        final_state = EnrichmentStageState.SKIPPED
                    elif outcome == "no_match":
                        final_state = EnrichmentStageState.COMPLETED
                    elif retryable:
                        final_state = EnrichmentStageState.RETRYABLE
                    else:
                        final_state = EnrichmentStageState.FAILED
                    await _finish_stage(
                        session_factory, run.id, stage, token, final_state,
                        outcome=outcome, error_code=None if final_state == EnrichmentStageState.COMPLETED else code,
                        failure_message=message if final_state in {EnrichmentStageState.FAILED, EnrichmentStageState.RETRYABLE} else None,
                    )
                    if retryable:
                        raise EnrichmentStageRetryable(f"{stage.value} enrichment stage can be retried")
                else:
                    await _finish_stage(session_factory, run.id, stage, token, EnrichmentStageState.COMPLETED, outcome=provider_result.status.value)
        except EnrichmentStageRetryable:
            raise
        except EnrichmentStageBusy:
            raise
        except Exception as exc:
            # Keep the exception safe: provider/network exception text can contain URLs or credentials.
            try:
                await _finish_stage(
                    session_factory, run.id, stage, token, EnrichmentStageState.RETRYABLE,
                    outcome="stage_exception", error_code="STAGE_EXECUTION_ERROR",
                    failure_message=f"Stage execution raised {type(exc).__name__}",
                )
            except EnrichmentStageBusy:
                pass
            raise EnrichmentStageRetryable(f"{stage.value} enrichment stage execution was interrupted") from None

    async with session_factory() as session:
        completed_run = await session.get(EnrichmentRunModel, run.id)
        states = completed_run.stage_states or {}
        has_failures = any(_stage_record(states, stage).state == EnrichmentStageState.FAILED for stage in STAGE_ORDER)
        completed_run.status = "partial" if has_failures else "completed"
        completed_run.completed_at = completed_run.completed_at or now_utc()
        completed_run.failure_summary = _failure_summary(states)
        completed_run.active_stage = None
        completed_run.stage_lease_token = None
        completed_run.stage_lease_expires_at = None
        completed_run.updated_at = now_utc()
        await session.commit()
        await session.refresh(completed_run)
        logger.info(
            "enrichment run finished",
            extra={"job_id": str(job_id), "lead_id": str(lead_id), "enrichment_run_id": str(run.id), "status": completed_run.status},
        )
        return completed_run

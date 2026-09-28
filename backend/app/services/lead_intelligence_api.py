"""Read-only API projection of one internally consistent enrichment run."""

from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.personalization import PersonalizationResponse
from app.models import (
    EnrichmentRun, Evidence, Lead, LeadScore, Opportunity, Personalization,
    ProcessingJob, ServiceRecommendation,
)
from app.models.entities import personalization_evidence_links
from app.schemas.intelligence import (
    ClaimOut, EnrichmentRunSummary, EvidenceOut, IntelligenceIdentity,
    IntelligenceResponse, IntelligenceSummary, OpportunityOut,
    PersonalizationOut, PersonalizationSectionOut, ProcessingMetadata,
    RecommendationOut, ScoreOut,
)


def _uuid_values(values) -> set[UUID]:
    return {UUID(str(value)) for value in (values or ())}


def _public_source_url(value: str | None) -> str | None:
    """Expose a source origin without credentials, path tokens, query strings, or fragments."""
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit((parsed.scheme, f"{host}{port}", "/", "", ""))
    except ValueError:
        return None


async def _load_valid_run(db: AsyncSession, lead_id: UUID):
    """Return the newest completed run with a score and internally owned intelligence."""
    runs = (await db.scalars(
        select(EnrichmentRun).where(
            EnrichmentRun.lead_id == lead_id,
            func.lower(EnrichmentRun.status) == "completed",
            select(LeadScore.id).where(
                LeadScore.lead_id == lead_id,
                LeadScore.enrichment_run_id == EnrichmentRun.id,
            ).exists(),
        ).order_by(
            func.coalesce(EnrichmentRun.completed_at, EnrichmentRun.created_at).desc(),
            EnrichmentRun.id.desc(),
        )
    )).all()
    for run in runs:
        score = await db.scalar(select(LeadScore).where(
            LeadScore.lead_id == lead_id, LeadScore.enrichment_run_id == run.id,
        ).order_by(LeadScore.calculated_at.desc(), LeadScore.id.desc()).limit(1))
        if score is None:  # protects against concurrent score deletion between the candidate and row reads
            continue
        evidence = list((await db.scalars(select(Evidence).where(
            Evidence.lead_id == lead_id, Evidence.enrichment_run_id == run.id,
        ).order_by(Evidence.id))).all())
        evidence_ids = {row.id for row in evidence}
        if not _uuid_values(score.evidence_references).issubset(evidence_ids):
            continue
        opportunities = list((await db.scalars(select(Opportunity).where(
            Opportunity.lead_id == lead_id,
            Opportunity.enrichment_run_id == run.id,
            Opportunity.origin == "deterministic",
        ).order_by(Opportunity.category, Opportunity.id))).all())
        opportunity_ids = {row.id for row in opportunities}
        if any(not _uuid_values(row.evidence_references)
               or not _uuid_values(row.evidence_references).issubset(evidence_ids)
               for row in opportunities):
            continue
        recommendations = list((await db.scalars(select(ServiceRecommendation).where(
            ServiceRecommendation.lead_id == lead_id,
            ServiceRecommendation.enrichment_run_id == run.id,
        ).order_by(ServiceRecommendation.rank, ServiceRecommendation.id))).all())
        valid_recommendations = all(
            _uuid_values(row.evidence_references)
            and _uuid_values(row.evidence_references).issubset(evidence_ids)
            and _uuid_values(row.opportunity_references)
            and _uuid_values(row.opportunity_references).issubset(opportunity_ids)
            for row in recommendations
        )
        if not valid_recommendations:
            continue
        return run, score, evidence, opportunities, recommendations
    return None


async def _project_personalization(db, lead_id, run_id, evidence_ids, opportunities, recommendations):
    record = await db.scalar(select(Personalization).where(
        Personalization.lead_id == lead_id,
        Personalization.enrichment_run_id == run_id,
        Personalization.status == "validated",
    ).order_by(Personalization.created_at.desc(), Personalization.id.desc()).limit(1))
    if record is None or not record.generated:
        return None
    if (record.validation_result or {}).get("valid") is not True:
        return None
    try:
        generated = PersonalizationResponse.model_validate(record.generated)
    except (ValidationError, TypeError, ValueError):
        return None
    if (generated.contract_version != record.contract_version
            or generated.prompt_version != record.prompt_version):
        return None
    all_claims = [claim for section in (
        generated.sales_opening, generated.audit_summary,
        generated.audit_video_script, generated.recommended_package_language,
    ) for claim in section.claims]
    used_evidence = {ref for claim in all_claims for ref in claim.evidence_ids}
    used_opportunities = {ref for claim in all_claims for ref in claim.opportunity_ids}
    evidence_by_id = {row.id: row for row in await _evidence_for_ids(db, evidence_ids)}
    opportunity_by_id = {row.id: row for row in opportunities}
    if not used_evidence.issubset(evidence_by_id) or not used_opportunities.issubset(opportunity_by_id):
        return None
    if any(evidence_by_id[ref].lead_id != lead_id or evidence_by_id[ref].enrichment_run_id != run_id
           for ref in used_evidence):
        return None
    if any(opportunity_by_id[ref].lead_id != lead_id or opportunity_by_id[ref].enrichment_run_id != run_id
           for ref in used_opportunities):
        return None
    if generated.recommended_service_ids != tuple(row.service_id for row in recommendations):
        return None
    persisted_links = set((await db.scalars(select(personalization_evidence_links.c.evidence_id).where(
        personalization_evidence_links.c.personalization_id == record.id,
    ))).all())
    if persisted_links != used_evidence:
        return None

    def section(value):
        return PersonalizationSectionOut(
            text=value.text,
            claims=tuple(ClaimOut.model_validate(item.model_dump()) for item in value.claims),
        )

    return PersonalizationOut(
        id=record.id, contract_version=record.contract_version,
        prompt_version=record.prompt_version, created_at=record.created_at,
        confidence_score=generated.confidence_score,
        sales_opening=section(generated.sales_opening),
        audit_summary=section(generated.audit_summary),
        audit_video_script=section(generated.audit_video_script),
        recommended_package_language=section(generated.recommended_package_language),
    )


async def _evidence_for_ids(db, evidence_ids):
    if not evidence_ids:
        return []
    return list((await db.scalars(select(Evidence).where(Evidence.id.in_(evidence_ids)))).all())


async def read_lead_intelligence(db: AsyncSession, lead_id: UUID) -> IntelligenceResponse | None:
    lead = await db.get(Lead, lead_id)
    if lead is None:
        return None
    selected = await _load_valid_run(db, lead_id)
    if selected is None:
        return None
    run, score, evidence, opportunities, recommendations = selected
    personalization = await _project_personalization(
        db, lead_id, run.id, {row.id for row in evidence}, opportunities, recommendations,
    )
    job = await db.get(ProcessingJob, run.processing_job_id) if run.processing_job_id else None
    return IntelligenceResponse(
        lead=IntelligenceIdentity(
            id=lead.id, company_name=lead.company_name, website=lead.website,
            industry=lead.industry, city=lead.city, state=lead.state, country=lead.country,
        ),
        enrichment_status=run.status,
        run=EnrichmentRunSummary(
            id=run.id, status=run.status, pipeline_version=run.pipeline_version,
            started_at=run.started_at, completed_at=run.completed_at,
        ),
        score=ScoreOut(
            value=score.score, version=score.score_version, breakdown=score.components,
            evidence_ids=tuple(sorted(_uuid_values(score.evidence_references), key=str)),
            calculated_at=score.calculated_at,
        ),
        opportunities=tuple(OpportunityOut(
            id=row.id, category=row.category, severity=row.severity, status=row.status,
            title=row.title, description=row.description,
            evidence_ids=tuple(sorted(_uuid_values(row.evidence_references), key=str)),
            rule_version=row.rule_version,
        ) for row in opportunities),
        recommendations=tuple(RecommendationOut(
            id=row.id, service_id=row.service_id, service_name=row.service_name,
            service_description=row.service_description, service_version=row.service_version,
            rank=row.rank, rationale=row.rationale, confidence=row.confidence,
            opportunity_ids=tuple(sorted(_uuid_values(row.opportunity_references), key=str)),
            evidence_ids=tuple(sorted(_uuid_values(row.evidence_references), key=str)),
            rule_version=row.rule_version,
        ) for row in recommendations),
        evidence=tuple(EvidenceOut(
            id=row.id, evidence_type=row.evidence_type, source_type=row.source_type,
            provider_name=row.provider_name, source_url=_public_source_url(row.source_url),
            observed_value=row.observed_value, observed_at=row.observed_at,
            fetched_at=row.fetched_at, confidence=row.confidence,
        ) for row in evidence),
        personalization=personalization,
        personalization_available=personalization is not None,
        processing=ProcessingMetadata(
            job_id=job.id if job else run.processing_job_id,
            status=job.status.value if job else run.status,
            checkpoint=job.checkpoint if job else None,
            attempt_count=job.attempt_count if job else None,
            max_attempts=job.max_attempts if job else None,
            started_at=job.started_at if job else run.started_at,
            completed_at=job.completed_at if job else run.completed_at,
            updated_at=job.updated_at if job else run.updated_at,
        ),
        generated_at=datetime.now(timezone.utc),
    )


async def read_intelligence_summary(db: AsyncSession, lead_id: UUID) -> IntelligenceSummary:
    selected = await _load_valid_run(db, lead_id)
    if selected is None:
        return IntelligenceSummary(intelligence_status="unavailable")
    run, score, evidence, opportunities, recommendations = selected
    personalization = await _project_personalization(
        db, lead_id, run.id, {row.id for row in evidence}, opportunities, recommendations,
    )
    return IntelligenceSummary(
        intelligence_status="available", intelligence_score=score.score,
        opportunity_count=len(opportunities), recommendation_count=len(recommendations),
        personalization_available=personalization is not None,
        latest_enrichment_at=run.completed_at or run.created_at,
    )

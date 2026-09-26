"""Deterministic intelligence derived only from persisted source evidence."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import mean
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.domain.intelligence import (
    IntelligenceOrigin,
    OpportunityCategory,
    OpportunitySeverity,
    OpportunityStatus,
    ScoreFactor,
    ServiceDefinition,
    ServiceRecommendationContract,
    VersionedLeadScore,
)
from app.models import Evidence, Lead, LeadScore, Opportunity, ServiceCatalogEntry, ServiceRecommendation
from app.models.entities import (
    EnrichmentRun,
    now_utc,
    opportunity_evidence_links,
    recommendation_evidence_links,
    recommendation_opportunity_links,
    score_evidence_links,
)


OPPORTUNITY_RULE_VERSION = "deterministic-opportunities-v1"
RECOMMENDATION_RULE_VERSION = "service-mapping-v1"
SCORE_VERSION = "evidence-lead-score-v1"

# PageSpeed score thresholds use the provider's normalized 0..1 scale.
# <= .25 critical, <= .49 high, <= .74 medium, <= .89 low; >= .90 is not flagged.
PAGESPEED_SEVERITY_THRESHOLDS = ((0.25, OpportunitySeverity.CRITICAL), (0.49, OpportunitySeverity.HIGH), (0.74, OpportunitySeverity.MEDIUM), (0.89, OpportunitySeverity.LOW))
# Two or more missing website SEO tags is high; one is medium. PageSpeed SEO
# below .50 is high, below .75 medium, and .75 through .89 low.
SEO_HIGH_MISSING_TAG_COUNT = 2
SEO_HIGH_SCORE_LIMIT = 0.50
SEO_LOW_SCORE_LIMIT = 0.75
SEO_OPPORTUNITY_SCORE_LIMIT = 0.90
# Google rating below 2.5 is high, below 3.5 is medium. Fewer than 3 reviews
# is medium and 3 through 9 is low; 10 or more is not flagged.
GOOGLE_RATING_HIGH_LIMIT = 2.5
GOOGLE_RATING_MEDIUM_LIMIT = 3.5
GOOGLE_REVIEW_MEDIUM_LIMIT = 3
GOOGLE_REVIEW_LOW_LIMIT = 10
SEVERITY_POINTS = {
    OpportunitySeverity.LOW: 20,
    OpportunitySeverity.MEDIUM: 45,
    OpportunitySeverity.HIGH: 70,
    OpportunitySeverity.CRITICAL: 100,
}

DEFAULT_SERVICE_CATALOG = (
    ServiceDefinition(service_id="seo", name="SEO", description="Improve organic search visibility and technical on-page signals.", version="1"),
    ServiceDefinition(service_id="local_seo", name="Local SEO", description="Improve local business profile quality and geographic search visibility.", version="1"),
    ServiceDefinition(service_id="google_ads", name="Google Ads", description="Plan and manage paid search campaigns.", version="1"),
    ServiceDefinition(service_id="meta_ads", name="Meta Ads", description="Plan and manage paid campaigns across Meta platforms.", version="1"),
    ServiceDefinition(service_id="performance_marketing", name="Performance Marketing", description="Manage measurable cross-channel acquisition campaigns.", version="1"),
    ServiceDefinition(service_id="website_development", name="Website Development", description="Build or improve secure, reliable business websites.", version="1"),
    ServiceDefinition(service_id="website_cro", name="Website CRO", description="Improve website conversion paths using measured performance and UX signals.", version="1"),
    ServiceDefinition(service_id="ai_automation", name="AI Automation", description="Automate suitable business workflows with AI-enabled systems.", version="1"),
    ServiceDefinition(service_id="ai_chatbot", name="AI Chatbot", description="Provide automated conversational support and lead capture.", version="1"),
    ServiceDefinition(service_id="lead_generation", name="Lead Generation", description="Build measurable programs to acquire qualified leads.", version="1"),
    ServiceDefinition(service_id="social_media_management", name="Social Media Management", description="Plan and manage consistent social media publishing.", version="1"),
)

# Relevance is explicit so recommendation order is reproducible and reviewable.
SERVICE_RULES = {
    OpportunityCategory.WEBSITE_PERFORMANCE.value: {"website_development": 1.0, "website_cro": 0.9},
    OpportunityCategory.SEO.value: {"seo": 1.0},
    OpportunityCategory.LOCAL_SEO.value: {"local_seo": 1.0},
    OpportunityCategory.REPUTATION.value: {"local_seo": 0.75},
    OpportunityCategory.WEBSITE_SECURITY.value: {"website_development": 0.9},
    OpportunityCategory.WEBSITE_QUALITY.value: {"website_development": 0.75, "website_cro": 0.8},
    OpportunityCategory.CONVERSION.value: {"website_cro": 1.0},
    OpportunityCategory.DIGITAL_PRESENCE.value: {"social_media_management": 0.8, "lead_generation": 0.55},
}


def _severity_for_pagespeed(score: float) -> OpportunitySeverity | None:
    for threshold, severity in PAGESPEED_SEVERITY_THRESHOLDS:
        if score <= threshold:
            return severity
    return None


def _evidence_value(record: Evidence) -> object:
    return record.observed_value


def _candidate(category: str, severity: OpportunitySeverity, title: str, description: str, evidence: list[Evidence]) -> dict:
    selected = sorted({record.id for record in evidence}, key=str)
    return {
        "lead_id": evidence[0].lead_id if evidence else None,
        "enrichment_run_id": evidence[0].enrichment_run_id if evidence else None,
        "identity": f"{category}:{OPPORTUNITY_RULE_VERSION}",
        "category": category,
        "severity": severity.value,
        "status": OpportunityStatus.OPEN.value,
        "origin": IntelligenceOrigin.DETERMINISTIC.value,
        "title": title,
        "description": description,
        "evidence_references": [str(item) for item in selected],
        "rule_version": OPPORTUNITY_RULE_VERSION,
    }


def derive_opportunities(lead_id: UUID, enrichment_run_id: UUID, evidence: list[Evidence]) -> list[dict]:
    """Apply versioned rules to facts present in evidence; absence alone is not a fact."""
    grouped: dict[str, list[Evidence]] = defaultdict(list)
    for item in evidence:
        grouped[item.evidence_type].append(item)
    output: list[dict] = []

    for record in grouped.get("pagespeed_performance_score", []):
        value = _evidence_value(record)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
            severity = _severity_for_pagespeed(float(value))
            if severity:
                output.append(_candidate(
                    OpportunityCategory.WEBSITE_PERFORMANCE.value, severity,
                    "Website performance needs improvement",
                    f"PageSpeed recorded a performance score of {float(value):.2f} on a 0–1 scale.", [record],
                ))

    seo_evidence = []
    for evidence_type in ("website_title", "website_meta_description", "website_h1"):
        for record in grouped.get(evidence_type, []):
            if record.observed_value is None and record.metadata_json.get("observation_state") == "missing":
                seo_evidence.append(record)
    seo_score_evidence = []
    for record in grouped.get("pagespeed_seo_score", []):
        value = record.observed_value
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < SEO_OPPORTUNITY_SCORE_LIMIT:
            seo_score_evidence.append(record)
    if seo_evidence or seo_score_evidence:
        refs = seo_evidence + seo_score_evidence
        severity = OpportunitySeverity.HIGH if len(seo_evidence) >= SEO_HIGH_MISSING_TAG_COUNT or any(float(r.observed_value) < SEO_HIGH_SCORE_LIMIT for r in seo_score_evidence) else OpportunitySeverity.MEDIUM
        if severity == OpportunitySeverity.MEDIUM and not seo_evidence and all(float(r.observed_value) >= SEO_LOW_SCORE_LIMIT for r in seo_score_evidence):
            severity = OpportunitySeverity.LOW
        findings = [
            {"website_title": "missing page title", "website_meta_description": "missing meta description", "website_h1": "missing H1"}[record.evidence_type]
            for record in seo_evidence
        ]
        if seo_score_evidence:
            findings.extend(f"PageSpeed SEO score {float(item.observed_value):.2f}" for item in seo_score_evidence)
        output.append(_candidate(
            OpportunityCategory.SEO.value, severity, "Search optimization needs attention",
            "Verified SEO findings: " + "; ".join(sorted(findings)) + ".", refs,
        ))

    for record in grouped.get("website_https", []):
        if record.observed_value is False:
            output.append(_candidate(
                OpportunityCategory.WEBSITE_SECURITY.value, OpportunitySeverity.HIGH,
                "Website does not use HTTPS", "Website analysis observed an HTTP URL without HTTPS.", [record],
            ))

    rating_records = [r for r in grouped.get("google_rating", []) if isinstance(r.observed_value, (int, float)) and not isinstance(r.observed_value, bool) and 0 <= r.observed_value <= 5]
    review_records = [r for r in grouped.get("google_review_count", []) if isinstance(r.observed_value, int) and not isinstance(r.observed_value, bool) and r.observed_value >= 0]
    reputation_evidence = []
    severities = []
    for record in rating_records:
        if record.observed_value < GOOGLE_RATING_HIGH_LIMIT:
            severities.append(OpportunitySeverity.HIGH)
            reputation_evidence.append(record)
        elif record.observed_value < GOOGLE_RATING_MEDIUM_LIMIT:
            severities.append(OpportunitySeverity.MEDIUM)
            reputation_evidence.append(record)
    for record in review_records:
        if record.observed_value < GOOGLE_REVIEW_LOW_LIMIT:
            severities.append(OpportunitySeverity.MEDIUM if record.observed_value < GOOGLE_REVIEW_MEDIUM_LIMIT else OpportunitySeverity.LOW)
            reputation_evidence.append(record)
    if reputation_evidence:
        severity_order = {item: rank for rank, item in enumerate((OpportunitySeverity.LOW, OpportunitySeverity.MEDIUM, OpportunitySeverity.HIGH, OpportunitySeverity.CRITICAL))}
        severity = max(severities, key=severity_order.get)
        details = []
        if rating_records and rating_records[0] in reputation_evidence:
            details.append(f"Google rating is {rating_records[0].observed_value}/5")
        if review_records and review_records[0] in reputation_evidence:
            details.append(f"Google profile has {review_records[0].observed_value} reviews")
        output.append(_candidate(
            OpportunityCategory.REPUTATION.value, severity,
            "Business reputation signals are limited", "; ".join(details) + ".", reputation_evidence,
        ))

    return sorted(output, key=lambda item: (item["category"], item["identity"]))


def recommend_services(
    opportunities: list[Opportunity],
    catalog: dict[str, ServiceDefinition],
    evidence_by_id: dict[UUID, Evidence] | None = None,
) -> list[ServiceRecommendationContract]:
    """Map opportunities to services and rank with severity, relevance, and evidence confidence."""
    accumulated: dict[str, dict] = {}
    severity_value = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    for opportunity in opportunities:
        for service_id, relevance in SERVICE_RULES.get(opportunity.category, {}).items():
            service = catalog.get(service_id)
            if service is None or not service.active:
                continue
            linked = [UUID(str(ref)) for ref in (opportunity.evidence_references or [])]
            evidence_confidences = [
                float(evidence_by_id[ref].confidence) for ref in linked
                if evidence_by_id and ref in evidence_by_id and evidence_by_id[ref].confidence is not None
            ]
            confidence = mean(evidence_confidences) if evidence_confidences else 0.0
            weighted = severity_value.get(opportunity.severity, 1) * 25 * relevance * confidence
            entry = accumulated.setdefault(service_id, {"score": 0.0, "confidence_values": [], "opportunities": [], "evidence_ids": [], "relevance": relevance})
            entry["score"] += weighted
            entry["confidence_values"].append(confidence)
            entry["opportunities"].append(opportunity)
            entry["evidence_ids"].extend(opportunity.evidence_references or [])
            entry["relevance"] = max(entry["relevance"], relevance)

    ordered = sorted(accumulated.items(), key=lambda pair: (-pair[1]["score"], pair[0]))
    result = []
    for rank, (service_id, data) in enumerate(ordered, 1):
        linked_opportunities = sorted({item.id for item in data["opportunities"] if item.id}, key=str)
        evidence_ids = sorted({UUID(str(item)) for item in data["evidence_ids"]}, key=str)
        categories = sorted({item.category for item in data["opportunities"]})
        confidence = min(1.0, mean(data["confidence_values"]) * data["relevance"])
        result.append(ServiceRecommendationContract(
            lead_id=data["opportunities"][0].lead_id,
            enrichment_run_id=data["opportunities"][0].enrichment_run_id,
            service=catalog[service_id],
            rank=rank,
            rationale=f"Recommended from {', '.join(categories)} opportunities using severity-weighted rule relevance.",
            confidence=confidence,
            opportunity_references=tuple(linked_opportunities),
            evidence_references=tuple(evidence_ids),
            rule_version=RECOMMENDATION_RULE_VERSION,
            ranking_score=round(data["score"], 6),
        ))
    return result


def calculate_versioned_score(
    lead_id: UUID,
    enrichment_run_id: UUID,
    opportunities: list[Opportunity],
    evidence: list[Evidence],
    *,
    calculated_at: datetime | None = None,
    score_version: str = SCORE_VERSION,
) -> VersionedLeadScore:
    """Score verified marketing need; unmeasured fit and buying intent stay unavailable."""
    by_type: dict[str, list[Evidence]] = defaultdict(list)
    for item in evidence:
        by_type[item.evidence_type].append(item)
    relevant_types = {
        "pagespeed_performance_score", "pagespeed_seo_score", "website_https",
        "website_title", "website_meta_description", "website_h1", "google_rating", "google_review_count",
    }
    signal_records = [item for item in evidence if item.evidence_type in relevant_types]
    opportunity_points = sum(
        SEVERITY_POINTS[OpportunitySeverity(item.get("severity") if isinstance(item, dict) else item.severity)]
        for item in opportunities
    )
    opportunity_factor = ScoreFactor(
        value=float(min(100, opportunity_points)) if signal_records else None,
        available=bool(signal_records),
        explanation="Sum of evidence-backed opportunity severity points, capped at 100; unavailable without evaluated signals.",
        evidence_references=tuple(sorted({UUID(str(ref)) for opportunity in opportunities for ref in (opportunity.get("evidence_references", []) if isinstance(opportunity, dict) else opportunity.evidence_references)}, key=str)) or tuple(sorted({item.id for item in signal_records}, key=str)),
    )

    maturity_values: list[float] = []
    maturity_refs = []
    for evidence_type in ("pagespeed_performance_score", "pagespeed_seo_score", "pagespeed_accessibility_score", "pagespeed_best_practices_score"):
        for item in by_type.get(evidence_type, []):
            if isinstance(item.observed_value, (int, float)) and not isinstance(item.observed_value, bool) and 0 <= item.observed_value <= 1:
                maturity_values.append(float(item.observed_value) * 100)
                maturity_refs.append(item.id)
    for item in by_type.get("website_https", []):
        if isinstance(item.observed_value, bool):
            maturity_values.append(100.0 if item.observed_value else 0.0)
            maturity_refs.append(item.id)
    for evidence_type in ("website_title", "website_meta_description", "website_h1"):
        for item in by_type.get(evidence_type, []):
            if isinstance(item.observed_value, str):
                maturity_values.append(100.0)
                maturity_refs.append(item.id)
            elif item.observed_value is None and item.metadata_json.get("observation_state") == "missing":
                maturity_values.append(0.0)
                maturity_refs.append(item.id)
    maturity = ScoreFactor(
        value=round(mean(maturity_values), 2) if maturity_values else None,
        available=bool(maturity_values),
        explanation="Mean of observed PageSpeed category scores, HTTPS state, and explicitly observed website metadata presence.",
        evidence_references=tuple(sorted(set(maturity_refs), key=str)),
    )
    components = {
        "opportunity_score": opportunity_factor,
        "digital_maturity": maturity,
        "lead_completeness_fit": ScoreFactor(value=None, available=False, explanation="Unavailable: no verified fit rule is defined for this evidence set."),
        "buying_opportunity": ScoreFactor(value=None, available=False, explanation="Unavailable: source evidence does not establish buying intent."),
    }
    overall_parts = []
    if opportunity_factor.available:
        overall_parts.append((opportunity_factor.value, 0.6))
    if maturity.available:
        # A low maturity score is a larger addressable digital gap.
        overall_parts.append((100 - maturity.value, 0.4))
    overall = round(sum(value * weight for value, weight in overall_parts) / sum(weight for _, weight in overall_parts)) if overall_parts else None
    return VersionedLeadScore(
        lead_id=lead_id,
        enrichment_run_id=enrichment_run_id,
        score=overall,
        score_version=score_version,
        components=components,
        calculated_at=calculated_at or now_utc(),
    )


async def _insert_link_rows(session, table, rows: list[dict]) -> None:
    if not rows:
        return
    dialect = session.bind.dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(table).values(rows).on_conflict_do_nothing()
        await session.execute(statement)
    elif dialect == "postgresql":
        statement = pg_insert(table).values(rows).on_conflict_do_nothing()
        await session.execute(statement)
    else:
        await session.execute(table.insert().values(rows))


async def derive_and_persist_intelligence(
    session_factory: async_sessionmaker,
    lead_id: UUID,
    enrichment_run_id: UUID,
    *,
    service_catalog: tuple[ServiceDefinition, ...] = DEFAULT_SERVICE_CATALOG,
) -> VersionedLeadScore:
    """Persist deterministic run outputs idempotently and update legacy score fields."""
    async with session_factory() as session:
        run = await session.get(EnrichmentRun, enrichment_run_id)
        if run is None or run.lead_id != lead_id:
            raise ValueError("Enrichment run not found for lead")
        evidence = list((await session.scalars(select(Evidence).where(Evidence.enrichment_run_id == enrichment_run_id).order_by(Evidence.id))).all())
        candidates = derive_opportunities(lead_id, enrichment_run_id, evidence)
        existing = (await session.scalars(select(Opportunity).where(Opportunity.enrichment_run_id == enrichment_run_id))).all()
        existing_by_identity = {item.identity: item for item in existing}
        for item in candidates:
            if item["identity"] not in existing_by_identity:
                row = Opportunity(**item)
                session.add(row)
        await session.flush()
        opportunities = list((await session.scalars(select(Opportunity).where(Opportunity.enrichment_run_id == enrichment_run_id).order_by(Opportunity.category, Opportunity.identity))).all())
        await _insert_link_rows(session, opportunity_evidence_links, [
            {"opportunity_id": opportunity.id, "evidence_id": UUID(str(reference))}
            for opportunity in opportunities
            for reference in (opportunity.evidence_references or [])
        ])

        catalog_by_id = {item.service_id: item for item in service_catalog}
        catalog_rows = (await session.scalars(select(ServiceCatalogEntry).where(ServiceCatalogEntry.service_id.in_(catalog_by_id)))).all()
        stored_catalog = {item.service_id: item for item in catalog_rows}
        missing_catalog = [definition for service_id, definition in catalog_by_id.items() if service_id not in stored_catalog]
        if missing_catalog:
            rows = [{
                "service_id": item.service_id, "name": item.name, "description": item.description,
                "active": item.active, "version": item.version,
                "created_at": now_utc(), "updated_at": now_utc(),
            } for item in missing_catalog]
            dialect = session.bind.dialect.name
            if dialect == "sqlite":
                statement = sqlite_insert(ServiceCatalogEntry).values(rows).on_conflict_do_nothing(index_elements=["service_id"])
                await session.execute(statement)
            elif dialect == "postgresql":
                statement = pg_insert(ServiceCatalogEntry).values(rows).on_conflict_do_nothing(index_elements=["service_id"])
                await session.execute(statement)
            else:
                for row in rows:
                    session.add(ServiceCatalogEntry(**row))
            await session.flush()
            catalog_rows = (await session.scalars(select(ServiceCatalogEntry).where(ServiceCatalogEntry.service_id.in_(catalog_by_id)))).all()
            stored_catalog = {item.service_id: item for item in catalog_rows}
        await session.flush()
        effective_catalog = {
            service_id: ServiceDefinition(
                service_id=service_id, name=row.name, description=row.description,
                active=row.active, version=row.version,
            )
            for service_id, row in stored_catalog.items()
        }
        ranked = recommend_services(opportunities, effective_catalog, {item.id: item for item in evidence})
        stored_recs = (await session.scalars(select(ServiceRecommendation).where(ServiceRecommendation.enrichment_run_id == enrichment_run_id))).all()
        rec_keys = {(item.service_id, item.rule_version) for item in stored_recs}
        for item in ranked:
            key = (item.service.service_id, item.rule_version)
            if key not in rec_keys:
                session.add(ServiceRecommendation(
                    lead_id=lead_id,
                    enrichment_run_id=enrichment_run_id,
                    service_id=item.service.service_id,
                    service_name=item.service.name,
                    service_description=item.service.description,
                    service_version=item.service.version,
                    rank=item.rank,
                    rationale=item.rationale,
                    confidence=item.confidence,
                    opportunity_references=[str(value) for value in item.opportunity_references],
                    evidence_references=[str(value) for value in item.evidence_references],
                    rule_version=item.rule_version,
                    ranking_score=item.ranking_score,
                ))
        await session.flush()
        stored_recommendations = (await session.scalars(select(ServiceRecommendation).where(ServiceRecommendation.enrichment_run_id == enrichment_run_id))).all()
        await _insert_link_rows(session, recommendation_evidence_links, [
            {"recommendation_id": recommendation.id, "evidence_id": UUID(str(reference))}
            for recommendation in stored_recommendations
            for reference in (recommendation.evidence_references or [])
        ])
        await _insert_link_rows(session, recommendation_opportunity_links, [
            {"recommendation_id": recommendation.id, "opportunity_id": UUID(str(reference))}
            for recommendation in stored_recommendations
            for reference in (recommendation.opportunity_references or [])
        ])
        score_contract = calculate_versioned_score(lead_id, enrichment_run_id, opportunities, evidence)
        score_row = await session.scalar(select(LeadScore).where(
            LeadScore.enrichment_run_id == enrichment_run_id,
            LeadScore.score_version == SCORE_VERSION,
        ))
        if score_row is None:
            score_row = LeadScore(
                lead_id=lead_id,
                enrichment_run_id=enrichment_run_id,
                score=score_contract.score,
                score_version=score_contract.score_version,
                components={key: value.model_dump(mode="json") for key, value in score_contract.components.items()},
                evidence_references=sorted({str(ref) for factor in score_contract.components.values() for ref in factor.evidence_references}),
                calculated_at=score_contract.calculated_at,
            )
            session.add(score_row)
        await session.flush()
        await _insert_link_rows(session, score_evidence_links, [
            {"score_id": score_row.id, "evidence_id": UUID(str(item))}
            for item in (score_row.evidence_references or [])
        ])
        lead = await session.get(Lead, lead_id)
        lead.lead_score = score_contract.score
        lead.score_factors = {
            "score_version": score_contract.score_version,
            "components": {key: value.model_dump(mode="json") for key, value in score_contract.components.items()},
        }
        https_records = [item for item in evidence if item.evidence_type == "website_https" and isinstance(item.observed_value, bool)]
        if https_records:
            lead.score_factors["https"] = 10 if https_records[-1].observed_value else 0
        await session.commit()
        return score_contract

"""Evidence-grounded opportunity, service recommendation, and score tests."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, func

from app.domain.intelligence import OpportunitySeverity
from app.models import Evidence, Lead, LeadScore, Opportunity, ServiceCatalogEntry, ServiceRecommendation
from app.models.entities import (
    EnrichmentRun,
    opportunity_evidence_links,
    recommendation_evidence_links,
    recommendation_opportunity_links,
    score_evidence_links,
)
from app.services.intelligence import (
    DEFAULT_SERVICE_CATALOG,
    SCORE_VERSION,
    calculate_versioned_score,
    derive_and_persist_intelligence,
    derive_opportunities,
    recommend_services,
)


def evidence_row(lead_id, run_id, evidence_type, value, *, confidence=1.0, metadata=None, provider="pagespeed"):
    return Evidence(
        lead_id=lead_id,
        enrichment_run_id=run_id,
        evidence_type=evidence_type,
        source_type="performance_audit" if provider == "pagespeed" else "website",
        provider_name=provider,
        provider_kind="external_data" if provider != "website_analyzer" else "website_analyzer",
        observed_value=value,
        observed_at=datetime.now(timezone.utc),
        fetched_at=datetime.now(timezone.utc),
        confidence=confidence,
        metadata_json=metadata or {},
    )


async def seed_evidence(session_factory, *, new_run=False):
    async with session_factory() as session:
        lead = Lead(company_name="Verified Co", website="https://verified.example")
        session.add(lead)
        await session.flush()
        run = EnrichmentRun(
            lead_id=lead.id,
            status="completed",
            requested_stages=["website", "google_places", "pagespeed"],
            completed_stages=["website", "google_places", "pagespeed"],
            pipeline_version="test-v1",
        )
        session.add(run)
        await session.flush()
        records = [
            evidence_row(lead.id, run.id, "pagespeed_performance_score", 0.2, provider="pagespeed"),
            evidence_row(lead.id, run.id, "pagespeed_seo_score", 0.65, provider="pagespeed"),
            evidence_row(lead.id, run.id, "website_title", None, provider="website_analyzer", metadata={"observation_state": "missing"}),
            evidence_row(lead.id, run.id, "website_meta_description", None, provider="website_analyzer", metadata={"observation_state": "missing"}),
            evidence_row(lead.id, run.id, "website_h1", "Verified heading", provider="website_analyzer"),
            evidence_row(lead.id, run.id, "website_https", False, provider="website_analyzer"),
            evidence_row(lead.id, run.id, "google_rating", 2.4, provider="google_places"),
            evidence_row(lead.id, run.id, "google_review_count", 2, provider="google_places"),
        ]
        session.add_all(records)
        await session.commit()
        return lead.id, run.id


@pytest.mark.asyncio
async def test_opportunities_detect_pagespeed_seo_https_and_google_signals(session_factory):
    lead_id, run_id = await seed_evidence(session_factory)
    async with session_factory() as session:
        evidence = (await session.scalars(select(Evidence).where(Evidence.enrichment_run_id == run_id))).all()
        opportunities = derive_opportunities(lead_id, run_id, evidence)
    by_category = {item["category"]: item for item in opportunities}
    assert set(by_category) == {"website_performance", "seo", "website_security", "reputation"}
    assert by_category["website_performance"]["severity"] == OpportunitySeverity.CRITICAL.value
    assert by_category["seo"]["severity"] == OpportunitySeverity.HIGH.value
    assert by_category["website_security"]["evidence_references"]
    assert len(by_category["reputation"]["evidence_references"]) == 2
    assert all(item["rule_version"] for item in opportunities)


@pytest.mark.asyncio
async def test_opportunity_and_recommendation_persistence_is_idempotent_and_run_scoped(session_factory):
    lead_id, run_id = await seed_evidence(session_factory)
    score = await derive_and_persist_intelligence(session_factory, lead_id, run_id)
    again = await derive_and_persist_intelligence(session_factory, lead_id, run_id)
    assert score.score == again.score
    async with session_factory() as session:
        opportunity_count = await session.scalar(select(func.count(Opportunity.id)).where(Opportunity.enrichment_run_id == run_id))
        recommendations = (await session.scalars(select(ServiceRecommendation).where(ServiceRecommendation.enrichment_run_id == run_id).order_by(ServiceRecommendation.rank))).all()
        rows = (await session.scalars(select(Opportunity).where(Opportunity.enrichment_run_id == run_id))).all()
        assert opportunity_count == 4
        assert recommendations
        assert [item.rank for item in recommendations] == list(range(1, len(recommendations) + 1))
        assert all(item.evidence_references and item.opportunity_references for item in recommendations)
        assert recommendations[0].service_id == "website_development"  # critical performance/security coverage ranks strongly
        assert recommendations[0].service_name == "Website Development"
        assert recommendations[0].service_version == "1"
        assert await session.scalar(select(func.count()).select_from(opportunity_evidence_links)) >= 4
        assert await session.scalar(select(func.count()).select_from(recommendation_evidence_links)) > 0
        assert await session.scalar(select(func.count()).select_from(recommendation_opportunity_links)) > 0
        assert await session.scalar(select(func.count()).select_from(score_evidence_links)) > 0
        rows[0].status = "acknowledged"
        await session.commit()
    await derive_and_persist_intelligence(session_factory, lead_id, run_id)
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Opportunity.id)).where(Opportunity.enrichment_run_id == run_id)) == 4
        assert await session.scalar(select(Opportunity.status).where(Opportunity.enrichment_run_id == run_id, Opportunity.id == rows[0].id)) == "acknowledged"
        lead = await session.get(Lead, lead_id)
        assert lead.lead_score == score.score
        assert lead.score_factors["score_version"] == SCORE_VERSION
        assert await session.scalar(select(func.count(LeadScore.id)).where(LeadScore.enrichment_run_id == run_id)) == 1

    async with session_factory() as session:
        previous = await session.get(EnrichmentRun, run_id)
        evidence_rows = (await session.scalars(select(Evidence).where(Evidence.enrichment_run_id == run_id))).all()
        second = EnrichmentRun(
            lead_id=lead_id, status="completed", requested_stages=previous.requested_stages,
            completed_stages=previous.completed_stages, pipeline_version="test-v1",
        )
        session.add(second)
        await session.commit()
        second_id = second.id
        session.add_all([
            Evidence(
                lead_id=lead_id,
                enrichment_run_id=second_id,
                evidence_type=item.evidence_type,
                source_type=item.source_type,
                provider_name=item.provider_name,
                provider_kind=item.provider_kind,
                observed_value=item.observed_value,
                observed_at=item.observed_at,
                fetched_at=item.fetched_at,
                confidence=item.confidence,
                metadata_json=item.metadata_json,
            ) for item in evidence_rows
        ])
        await session.commit()
    await derive_and_persist_intelligence(session_factory, lead_id, second_id)
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Opportunity.id)).where(Opportunity.lead_id == lead_id)) == 8


def test_service_catalog_and_recommendation_ranking_are_deterministic():
    from types import SimpleNamespace

    catalog = {item.service_id: item for item in DEFAULT_SERVICE_CATALOG}
    lead_id, run_id = uuid4(), uuid4()
    performance = SimpleNamespace(
        id=uuid4(), lead_id=lead_id, enrichment_run_id=run_id,
        category="website_performance", severity="critical",
        evidence_references=[str(uuid4())],
    )
    seo = SimpleNamespace(
        id=uuid4(), lead_id=lead_id, enrichment_run_id=run_id,
        category="seo", severity="high", evidence_references=[str(uuid4())],
    )
    evidence_by_id = {
        UUID(performance.evidence_references[0]): SimpleNamespace(confidence=1.0),
        UUID(seo.evidence_references[0]): SimpleNamespace(confidence=1.0),
    }
    first = recommend_services([performance, seo], catalog, evidence_by_id)
    second = recommend_services([performance, seo], catalog, evidence_by_id)
    assert [item.service.service_id for item in first] == [item.service.service_id for item in second]
    assert first[0].service.service_id == "website_development"
    assert {item.service.service_id for item in first} == {"website_development", "website_cro", "seo"}
    assert len(DEFAULT_SERVICE_CATALOG) == 11


@pytest.mark.asyncio
async def test_missing_evidence_does_not_create_facts_or_fabricated_score(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="No signals")
        session.add(lead)
        await session.flush()
        run = EnrichmentRun(lead_id=lead.id, status="completed", requested_stages=[], completed_stages=[], pipeline_version="test-v1")
        session.add(run)
        await session.commit()
        lead_id, run_id = lead.id, run.id
    result = await derive_and_persist_intelligence(session_factory, lead_id, run_id)
    assert result.score is None
    assert all(not factor.available and factor.value is None for factor in result.components.values())
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Opportunity.id)).where(Opportunity.enrichment_run_id == run_id)) == 0
        assert await session.scalar(select(func.count(ServiceRecommendation.id)).where(ServiceRecommendation.enrichment_run_id == run_id)) == 0
        lead = await session.get(Lead, lead_id)
        assert lead.lead_score is None


@pytest.mark.asyncio
async def test_score_is_deterministic_versioned_and_has_factor_provenance(session_factory):
    lead_id, run_id = await seed_evidence(session_factory)
    async with session_factory() as session:
        evidence = (await session.scalars(select(Evidence).where(Evidence.enrichment_run_id == run_id))).all()
        opportunities = derive_opportunities(lead_id, run_id, evidence)
    first = calculate_versioned_score(lead_id, run_id, opportunities, evidence, calculated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    second = calculate_versioned_score(lead_id, run_id, opportunities, evidence, calculated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    newer = calculate_versioned_score(lead_id, run_id, opportunities, evidence, score_version="evidence-lead-score-v2")
    assert first == second
    assert first.score_version != newer.score_version
    assert first.score is not None and 0 <= first.score <= 100
    assert first.components["digital_maturity"].evidence_references
    assert first.components["opportunity_score"].evidence_references
    assert not first.components["buying_opportunity"].available
    assert not first.components["lead_completeness_fit"].available


@pytest.mark.asyncio
async def test_service_catalog_can_be_deactivated_by_configuration(session_factory):
    lead_id, run_id = await seed_evidence(session_factory)
    await derive_and_persist_intelligence(session_factory, lead_id, run_id)
    async with session_factory() as session:
        service = await session.get(ServiceCatalogEntry, "seo")
        assert service and service.active
        service.active = False
        await session.commit()
    await derive_and_persist_intelligence(session_factory, lead_id, run_id)
    async with session_factory() as session:
        assert await session.scalar(select(func.count(ServiceRecommendation.id)).where(
            ServiceRecommendation.enrichment_run_id == run_id,
            ServiceRecommendation.service_id == "seo",
        )) == 1  # an existing historical recommendation is retained, never silently deleted

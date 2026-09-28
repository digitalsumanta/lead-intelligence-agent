"""Read-only API projection tests for Phase 8C."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from app.config import settings
from app.database import get_db
from app.main import app
from app.models import (
    Evidence, Lead, LeadScore, Opportunity, Personalization, ServiceRecommendation, LeadStatus,
)
from app.models.entities import EnrichmentRun, personalization_evidence_links


@pytest.fixture
def api_client(session_factory, monkeypatch):
    async def override_db():
        async with session_factory() as session:
            yield session

    async def forbidden_provider(*args, **kwargs):
        raise AssertionError("GET intelligence must not invoke an AI provider")

    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    monkeypatch.setattr("app.main.get_provider", forbidden_provider)
    app.dependency_overrides[get_db] = override_db
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    app.dependency_overrides.pop(get_db, None)


async def make_completed_run(session_factory, *, lead_name="API Lead", score_value=73, marker="new"):
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        lead = Lead(company_name=lead_name, website="https://api.example", status=LeadStatus.COMPLETED)
        session.add(lead)
        await session.flush()
        run = EnrichmentRun(
            lead_id=lead.id, status="completed", pipeline_version="pipeline-test",
            started_at=now, completed_at=now,
        )
        session.add(run)
        await session.flush()
        evidence = Evidence(
            lead_id=lead.id, enrichment_run_id=run.id, evidence_type="pagespeed_score",
            source_type="performance_audit", provider_name="pagespeed", provider_kind="external_data",
            source_url="https://user:secret@pagespeed.example/private?key=secret#fragment",
            observed_value=marker, observed_at=now, fetched_at=now, confidence=0.9,
        )
        session.add(evidence)
        await session.flush()
        opportunity = Opportunity(
            lead_id=lead.id, enrichment_run_id=run.id, identity=f"{marker}-opportunity",
            category="website_performance", severity="high", status="open", origin="deterministic",
            title=f"{marker} opportunity", description="Current run issue",
            evidence_references=[str(evidence.id)], rule_version="rules-v1",
        )
        session.add(opportunity)
        await session.flush()
        recommendation = ServiceRecommendation(
            lead_id=lead.id, enrichment_run_id=run.id, service_id="seo", service_name="SEO",
            service_description="Search optimization", service_version="catalog-v1", rank=1,
            rationale="Deterministic recommendation", confidence=0.8,
            opportunity_references=[str(opportunity.id)], evidence_references=[str(evidence.id)],
            rule_version="rules-v1", ranking_score=10,
        )
        score = LeadScore(
            lead_id=lead.id, enrichment_run_id=run.id, score=score_value,
            score_version="score-v1", components={"performance": {"value": score_value}},
            evidence_references=[str(evidence.id)], calculated_at=now,
        )
        session.add_all([recommendation, score])
        await session.commit()
        return lead.id, run.id, evidence.id, opportunity.id


def personalization_payload(evidence_id, opportunity_id):
    from app.domain.personalization import CONTRACT_VERSION, PROMPT_VERSION

    fact_text = f'pagespeed_score = "new"'
    inference_text = "Potential website performance opportunity: new opportunity."
    language = "Would you be open to a brief conversation?"
    package = "Would you like to explore the recommended services?"
    claim = lambda kind, text, ev=(), op=(), svc=(): {
        "type": kind, "text": text, "evidence_ids": [str(x) for x in ev],
        "opportunity_ids": [str(x) for x in op], "service_ids": list(svc),
    }
    return {
        "contract_version": CONTRACT_VERSION, "prompt_version": PROMPT_VERSION,
        "recommended_service_ids": ["seo"],
        "sales_opening": {"text": language, "claims": [claim("suggested_language", language)]},
        "audit_summary": {"text": fact_text + "\n" + inference_text, "claims": [
            claim("verified_fact", fact_text, [evidence_id]),
            claim("supported_inference", inference_text, [evidence_id], [opportunity_id]),
        ]},
        "audit_video_script": {"text": fact_text, "claims": [claim("verified_fact", fact_text, [evidence_id])]},
        "recommended_package_language": {"text": package, "claims": [claim("suggested_language", package, svc=["seo"])]},
        "confidence_score": 0.8,
    }


@pytest.mark.asyncio
async def test_intelligence_endpoint_projects_single_latest_valid_run_and_personalization(api_client, session_factory):
    lead_id, run_id, evidence_id, opportunity_id = await make_completed_run(session_factory)
    # A newer incomplete run must not shadow a completed, score-backed run.
    async with session_factory() as session:
        session.add(EnrichmentRun(lead_id=lead_id, status="completed", pipeline_version="newer-without-score"))
        record = Personalization(
            lead_id=lead_id, enrichment_run_id=run_id, contract_version="personalization-v2",
            prompt_version="evidence-grounded-prompt-v2", status="validated",
            validation_result={"valid": True}, generated=personalization_payload(evidence_id, opportunity_id),
        )
        session.add(record)
        await session.flush()
        await session.execute(personalization_evidence_links.insert().values(
            personalization_id=record.id, evidence_id=evidence_id,
        ))
        await session.commit()
    async with api_client as client:
        response = await client.get(f"/api/leads/{lead_id}/intelligence", headers={"Authorization": "Bearer api-test-token"})
    assert response.status_code == 200
    body = response.json()
    assert body["api_version"] == "lead-intelligence-v1"
    assert body["run"]["id"] == str(run_id)
    assert body["score"]["value"] == 73
    assert body["opportunities"][0]["title"] == "new opportunity"
    assert body["recommendations"][0]["service_id"] == "seo"
    assert [row["id"] for row in body["evidence"]] == [str(evidence_id)]
    assert body["evidence"][0]["source_url"] == "https://pagespeed.example/"
    assert body["personalization"]["audit_summary"]["claims"][0]["type"] == "verified_fact"


@pytest.mark.asyncio
async def test_intelligence_not_available_and_lead_not_found_errors(api_client, session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Not ready")
        session.add(lead)
        await session.commit()
        lead_id = lead.id
    async with api_client as client:
        unavailable = await client.get(f"/api/leads/{lead_id}/intelligence", headers={"Authorization": "Bearer api-test-token"})
        missing = await client.get(f"/api/leads/{uuid4()}/intelligence", headers={"Authorization": "Bearer api-test-token"})
        invalid_id = await client.get("/api/leads/not-a-uuid/intelligence", headers={"Authorization": "Bearer api-test-token"})
    assert unavailable.status_code == 409
    assert unavailable.json()["error"]["code"] == "INTELLIGENCE_NOT_AVAILABLE"
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "LEAD_NOT_FOUND"
    assert invalid_id.status_code == 422 and invalid_id.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_stale_personalization_is_not_projected_and_foreign_lead_evidence_isolated(api_client, session_factory):
    lead_id, run_id, evidence_id, opportunity_id = await make_completed_run(session_factory)
    other_id, _, other_evidence_id, _ = await make_completed_run(session_factory, lead_name="Other", marker="foreign")
    async with session_factory() as session:
        record = Personalization(
            lead_id=lead_id, enrichment_run_id=run_id, contract_version="personalization-v2",
            prompt_version="evidence-grounded-prompt-v2", status="validated",
            validation_result={"valid": True},
            generated=personalization_payload(other_evidence_id, opportunity_id),
        )
        session.add(record)
        await session.flush()
        await session.execute(personalization_evidence_links.insert().values(
            personalization_id=record.id, evidence_id=other_evidence_id,
        ))
        await session.commit()
    async with api_client as client:
        response = await client.get(f"/api/leads/{lead_id}/intelligence", headers={"Authorization": "Bearer api-test-token"})
    body = response.json()
    assert response.status_code == 200
    assert body["personalization"] is None and body["personalization_available"] is False
    assert {row["id"] for row in body["evidence"]} == {str(evidence_id)}
    assert str(other_id) != body["lead"]["id"]


@pytest.mark.asyncio
async def test_cross_run_personalization_evidence_is_not_projected(api_client, session_factory):
    lead_id, run_id, current_evidence_id, opportunity_id = await make_completed_run(session_factory)
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        old_run = EnrichmentRun(lead_id=lead_id, status="completed", pipeline_version="old")
        session.add(old_run)
        await session.flush()
        old_evidence = Evidence(
            lead_id=lead_id, enrichment_run_id=old_run.id, evidence_type="old_signal",
            source_type="website", provider_name="old", provider_kind="website_analyzer",
            observed_value="stale", observed_at=now, fetched_at=now,
        )
        session.add(old_evidence)
        await session.flush()
        record = Personalization(
            lead_id=lead_id, enrichment_run_id=run_id, contract_version="personalization-v2",
            prompt_version="evidence-grounded-prompt-v2", status="validated",
            validation_result={"valid": True},
            generated=personalization_payload(old_evidence.id, opportunity_id),
        )
        session.add(record)
        await session.flush()
        await session.execute(personalization_evidence_links.insert().values(
            personalization_id=record.id, evidence_id=old_evidence.id,
        ))
        await session.commit()
    async with api_client as client:
        response = await client.get(f"/api/leads/{lead_id}/intelligence", headers={"Authorization": "Bearer api-test-token"})
    body = response.json()
    assert response.status_code == 200
    assert body["personalization"] is None
    assert {row["id"] for row in body["evidence"]} == {str(current_evidence_id)}


@pytest.mark.asyncio
async def test_list_summary_filters_and_stable_page_pagination(api_client, session_factory):
    first, _, _, _ = await make_completed_run(session_factory, lead_name="Scored", score_value=73)
    async with session_factory() as session:
        session.add(Lead(company_name="No intelligence"))
        await session.commit()
    headers = {"Authorization": "Bearer api-test-token"}
    async with api_client as client:
        all_rows = await client.get("/api/leads?page=1&page_size=1&intelligence_status=available", headers=headers)
        filtered = await client.get("/api/leads?min_score=70&max_score=80&intelligence_status=available&opportunity_present=true", headers=headers)
        invalid = await client.get("/api/leads?min_score=90&max_score=10", headers=headers)
        detail = await client.get(f"/api/leads/{first}", headers=headers)
    assert all_rows.status_code == 200 and len(all_rows.json()) == 1
    assert all_rows.json()[0]["intelligence_status"] == "available"
    assert all_rows.json()[0]["opportunity_count"] == 1
    assert filtered.status_code == 200 and [row["id"] for row in filtered.json()] == [str(first)]
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "VALIDATION_ERROR"
    assert detail.status_code == 200 and detail.json()["intelligence_score"] == 73


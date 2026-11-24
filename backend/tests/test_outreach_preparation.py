"""Grounding, persistence, review, and isolation tests for outreach preparation."""
from datetime import datetime, timezone
from uuid import uuid4
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from app.config import settings
from app.database import get_db
from app.domain.outreach import OutreachClaim, OutreachContent, OutreachText
from app.main import app
from app.models import (
    EnrichmentRun, Evidence, Lead, LeadScore, Opportunity, OutreachArtifact,
    OutreachStatus, SalesStatus, ServiceCatalogEntry, ServiceRecommendation,
)
from app.services.lead_intelligence_api import read_lead_intelligence
from app.services.outreach import allowed_claims


class StubProvider:
    name = "mock"
    model = "mock-model"

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def generate(self, request):
        self.calls += 1
        assert "UNTRUSTED DATA" in request.system_prompt
        assert "tools are available" in request.system_prompt
        return type("Result", (), {"parsed_json": self.payload, "content": ""})()


@pytest.fixture
def outreach_client(session_factory, monkeypatch):
    async def override_db():
        async with session_factory() as session:
            yield session
    monkeypatch.setattr(settings, "api_auth_token", "outreach-test-token")
    app.dependency_overrides[get_db] = override_db
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    app.dependency_overrides.pop(get_db, None)


async def make_outreach_lead(session_factory, *, status=SalesStatus.NEW, name="Grounded Business"):
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        lead = Lead(company_name=name, website="https://grounded.example", industry="Retail", sales_status=status)
        session.add(lead)
        await session.flush()
        run = EnrichmentRun(lead_id=lead.id, status="completed", pipeline_version="test-pipeline", started_at=now, completed_at=now)
        session.add(run)
        await session.flush()
        evidence = Evidence(
            lead_id=lead.id, enrichment_run_id=run.id, evidence_type="site_marker",
            source_type="website", provider_name="test", provider_kind="test",
            observed_value={"marker": "validated"}, observed_at=now, fetched_at=now,
        )
        session.add(evidence)
        await session.flush()
        opportunity = Opportunity(
            lead_id=lead.id, enrichment_run_id=run.id, identity="opportunity-one",
            category="website_performance", severity="medium", status="open", origin="deterministic",
            title="Improve page performance", description="A persisted deterministic finding.",
            evidence_references=[str(evidence.id)], rule_version="test-rules",
        )
        session.add(opportunity)
        session.add(LeadScore(
            lead_id=lead.id, enrichment_run_id=run.id, score=70, score_version="test-score",
            components={"test": 1}, evidence_references=[str(evidence.id)], calculated_at=now,
        ))
        catalog_entry = await session.get(ServiceCatalogEntry, "web_audit")
        if catalog_entry is None:
            session.add(ServiceCatalogEntry(service_id="web_audit", name="Website Audit", description="A review", version="v1"))
        await session.flush()
        session.add(ServiceRecommendation(
            lead_id=lead.id, enrichment_run_id=run.id, service_id="web_audit",
            service_name="Website Audit", service_description="A review", service_version="v1",
            rank=1, rationale="Deterministic test recommendation", confidence=0.8,
            opportunity_references=[str(opportunity.id)], evidence_references=[str(evidence.id)],
            rule_version="test-rules", ranking_score=0.8,
        ))
        await session.commit()
        return lead.id, run.id


def valid_payload(intelligence):
    fact = next(item for item in allowed_claims(intelligence) if item.type == "verified_fact")
    inference = next(item for item in allowed_claims(intelligence) if item.type == "supported_inference")
    service = next(item for item in allowed_claims(intelligence) if item.service_ids)

    def text(*claims):
        return OutreachText(text="\n".join(item.text for item in claims), claims=claims)

    content = OutreachContent.model_validate({
        "email": {"subject": "A quick introduction", "opening": text(fact), "body": text(inference, service), "call_to_action": text(next(item for item in allowed_claims(intelligence) if item.text == "Would you be open to a brief conversation?"))},
        "whatsapp": {"opening": text(fact), "message": text(inference), "call_to_action": text(next(item for item in allowed_claims(intelligence) if item.text == "Would you be open to a brief conversation?"))},
        "call_script": {"opening": text(fact), "key_problem": text(inference), "recommended_service_ids": ["web_audit"], "discovery_questions": ["Which business outcome matters most to you right now?"], "objections_and_talking_points": text(service), "closing": text(next(item for item in allowed_claims(intelligence) if item.text == "Thank you for your time."))},
        "follow_ups": [{"step": "OFFER_DISCOVERY_CALL", "timing": "WITHIN_THREE_DAYS", "purpose": "CONFIRM_NEXT_STEP"}],
        "recommended_service_ids": ["web_audit"],
    })
    return {"contract_version": "outreach-v1", "content": content.model_dump(mode="json")}


async def generate_via_api(client, session_factory, lead_id, monkeypatch, payload=None):
    async with session_factory() as session:
        intelligence = await read_lead_intelligence(session, lead_id)
    provider = StubProvider(payload if payload is not None else valid_payload(intelligence))
    monkeypatch.setattr("app.api.routes.outreach.get_provider", lambda **kwargs: provider)
    return await client.post(
        f"/api/leads/{lead_id}/outreach/generate",
        headers={"Authorization": "Bearer outreach-test-token"},
    ), provider


def test_outreach_contract_is_strict_and_factual_claims_require_evidence():
    with pytest.raises(ValueError):
        OutreachClaim(type="verified_fact", text="unsupported fact")
    with pytest.raises(ValueError):
        OutreachText(text="freeform unsupported factual copy", claims=(OutreachClaim(type="suggested_language", text="Thank you for your time."),))
    with pytest.raises(ValueError):
        OutreachClaim(type="suggested_language", text="We guarantee a 50% increase")


@pytest.mark.asyncio
async def test_valid_generation_is_run_scoped_grounded_and_awaits_review(outreach_client, session_factory, monkeypatch):
    lead_id, run_id = await make_outreach_lead(session_factory)
    async with outreach_client as client:
        response, provider = await generate_via_api(client, session_factory, lead_id, monkeypatch)
        read = await client.get(f"/api/leads/{lead_id}/outreach", headers={"Authorization": "Bearer outreach-test-token"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "READY_FOR_REVIEW" and body["is_current"] is True
    assert body["enrichment_run_id"] == str(run_id)
    assert provider.calls == 1
    assert read.status_code == 200 and read.json()["artifact"]["id"] == body["id"]
    assert read.json()["current_context"]["recommended_services"] == ["Website Audit"]
    async with session_factory() as session:
        artifact = await session.get(OutreachArtifact, UUID(body["id"]))
        assert artifact.content["recommended_service_ids"] == ["web_audit"]
        assert artifact.prompt_version == "grounded-outreach-prompt-v1"


@pytest.mark.asyncio
async def test_malformed_output_and_unsupported_claims_persist_failed_drafts(outreach_client, session_factory, monkeypatch):
    lead_id, _ = await make_outreach_lead(session_factory)
    headers = {"Authorization": "Bearer outreach-test-token"}
    async with outreach_client as client:
        malformed, _ = await generate_via_api(client, session_factory, lead_id, monkeypatch, {"content": {"bad": True}})
        unsupported = valid_payload(await _intelligence(session_factory, lead_id))
        unsupported["content"]["email"]["body"] = {"text": "Our customers grow 50%.", "claims": [{"type": "verified_fact", "text": "Our customers grow 50%.", "evidence_ids": [str(uuid4())]}]}
        invalid_reference, _ = await generate_via_api(client, session_factory, lead_id, monkeypatch, unsupported)
    assert malformed.json()["status"] == "DRAFT" and malformed.json()["content"] is None
    assert invalid_reference.json()["status"] == "DRAFT" and invalid_reference.json()["content"] is None
    async with session_factory() as session:
        artifacts = (await session.scalars(select(OutreachArtifact).where(OutreachArtifact.lead_id == lead_id))).all()
        assert len(artifacts) == 2
        assert sorted(item.status.value for item in artifacts) == ["DRAFT", "STALE"]
        assert all(item.validation_result["valid"] is False for item in artifacts)


async def _intelligence(session_factory, lead_id):
    async with session_factory() as session:
        return await read_lead_intelligence(session, lead_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [SalesStatus.WON, SalesStatus.LOST])
async def test_terminal_sales_status_cannot_generate(outreach_client, session_factory, monkeypatch, status):
    lead_id, _ = await make_outreach_lead(session_factory, status=status)
    provider = StubProvider({})
    monkeypatch.setattr("app.api.routes.outreach.get_provider", lambda **kwargs: provider)
    async with outreach_client as client:
        response = await client.post(f"/api/leads/{lead_id}/outreach/generate", headers={"Authorization": "Bearer outreach-test-token"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "OUTREACH_NOT_ALLOWED"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_approval_rejection_edit_restrictions_and_cross_lead_isolation(outreach_client, session_factory, monkeypatch):
    lead_id, _ = await make_outreach_lead(session_factory)
    other_id, _ = await make_outreach_lead(session_factory, name="Other Business")
    headers = {"Authorization": "Bearer outreach-test-token"}
    async with outreach_client as client:
        generated, _ = await generate_via_api(client, session_factory, lead_id, monkeypatch)
        empty_other = await client.get(f"/api/leads/{other_id}/outreach", headers=headers)
        approved = await client.post(f"/api/leads/{lead_id}/outreach/approve", headers=headers)
        edit_approved = await client.patch(f"/api/leads/{lead_id}/outreach", headers=headers, json={"status": "DRAFT"})
        rejected = await client.post(f"/api/leads/{other_id}/outreach/generate", headers=headers)
    assert generated.status_code == 200
    assert empty_other.status_code == 200 and empty_other.json()["artifact"] is None
    assert approved.status_code == 200 and approved.json()["status"] == "APPROVED"
    assert edit_approved.status_code == 422
    assert rejected.status_code == 200
    assert "/api/outreach/{artifact_id}/send" in app.openapi()["paths"]


@pytest.mark.asyncio
async def test_rejection_requires_reason_and_staleness_blocks_approval(outreach_client, session_factory, monkeypatch):
    lead_id, _ = await make_outreach_lead(session_factory)
    headers = {"Authorization": "Bearer outreach-test-token"}
    async with outreach_client as client:
        generated, _ = await generate_via_api(client, session_factory, lead_id, monkeypatch)
        empty_reason = await client.post(f"/api/leads/{lead_id}/outreach/reject", headers=headers, json={"reason": "  "})
        rejected = await client.post(f"/api/leads/{lead_id}/outreach/reject", headers=headers, json={"reason": "Needs a different approach"})
        generated_again, _ = await generate_via_api(client, session_factory, lead_id, monkeypatch)
        async with session_factory() as session:
            lead = await session.get(Lead, lead_id)
            lead.sales_status = SalesStatus.QUALIFIED
            await session.commit()
        current = await client.get(f"/api/leads/{lead_id}/outreach", headers=headers)
        approve_stale = await client.post(f"/api/leads/{lead_id}/outreach/approve", headers=headers)
    assert generated.status_code == 200 and empty_reason.status_code == 422
    assert rejected.status_code == 200 and rejected.json()["status"] == "REJECTED"
    assert generated_again.status_code == 200
    assert current.json()["artifact"]["status"] == "STALE" and current.json()["artifact"]["is_current"] is False
    assert approve_stale.status_code == 409


def test_outreach_migration_integrity_and_revision_chain():
    from pathlib import Path
    import importlib.util
    migration_path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0010_outreach_preparation.py"
    spec = importlib.util.spec_from_file_location("0010_outreach_preparation", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == "0010_outreach_preparation"
    assert migration.down_revision == "0009_sales_workflow"
    source = migration_path.read_text(encoding="utf-8")
    assert "fk_outreach_artifacts_run_lead" in source
    assert "ix_outreach_artifacts_lead_created" in source
    assert "outreach_status.drop" in source

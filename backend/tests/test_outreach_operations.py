"""Authenticated send, retry, manual confirmation, and event-boundary tests."""
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import httpx
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import get_db
from app.domain.outreach import OutreachClaim, OutreachContent, OutreachText
from app.main import app
from app.models import (
    EnrichmentRun, Evidence, Lead, LeadScore, Opportunity, OutreachArtifact,
    OutreachExecution, OutreachProviderEvent, OutreachStatus, SalesActivity,
    SalesActivityType, SalesStatus, SalesTask, ServiceCatalogEntry, ServiceRecommendation,
)
from app.providers.outreach import (
    HttpJsonOutreachProvider, OutreachMessage, OutreachProviderFailure,
    OutreachSendResult,
)
from app.schemas.outreach_operations import OutreachProviderEventIn
from app.services.lead_intelligence_api import read_lead_intelligence
from app.services.outreach import allowed_claims, source_state
from app.services.outreach_operations import record_provider_event


@pytest.fixture
def operations_client(session_factory, monkeypatch):
    async def override_db():
        async with session_factory() as session:
            yield session
    monkeypatch.setattr(settings, "api_auth_token", "operations-test-token")
    app.dependency_overrides[get_db] = override_db
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    app.dependency_overrides.pop(get_db, None)


class FakeProvider:
    name = "mock_gateway"

    def __init__(self, *, error=None, message_id="provider-msg-1"):
        self.error = error
        self.message_id = message_id
        self.calls = []

    async def send(self, message):
        self.calls.append(message)
        if self.error:
            raise self.error
        return OutreachSendResult(provider_message_id=self.message_id)


async def make_approved_artifact(session_factory, *, email="person@example.com", phone="+14155552671", status=OutreachStatus.APPROVED):
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        lead = Lead(
            company_name="Operations Business", website="https://ops.example", industry="Retail",
            email=email, phone=phone, sales_status=SalesStatus.NEW,
        )
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
            lead_id=lead.id, enrichment_run_id=run.id, identity="operation-opportunity",
            category="website_performance", severity="medium", status="open", origin="deterministic",
            title="Improve page performance", description="Persisted finding.",
            evidence_references=[str(evidence.id)], rule_version="test-rules",
        )
        session.add(opportunity)
        session.add(LeadScore(
            lead_id=lead.id, enrichment_run_id=run.id, score=70, score_version="test-score",
            components={"test": 1}, evidence_references=[str(evidence.id)], calculated_at=now,
        ))
        if await session.get(ServiceCatalogEntry, "web_audit") is None:
            session.add(ServiceCatalogEntry(service_id="web_audit", name="Website Audit", description="A review", version="v1"))
        await session.flush()
        session.add(ServiceRecommendation(
            lead_id=lead.id, enrichment_run_id=run.id, service_id="web_audit",
            service_name="Website Audit", service_description="A review", service_version="v1",
            rank=1, rationale="Test recommendation", confidence=0.8,
            opportunity_references=[str(opportunity.id)], evidence_references=[str(evidence.id)],
            rule_version="test-rules", ranking_score=0.8,
        ))
        await session.commit()
        lead_id, artifact_id = lead.id, UUID(int=0)

    async with session_factory() as session:
        lead = await session.get(Lead, lead_id)
        intelligence = await read_lead_intelligence(session, lead_id)
        claims = allowed_claims(intelligence)
        fact = next(item for item in claims if item.type == "verified_fact")
        inference = next(item for item in claims if item.type == "supported_inference")
        service_claim = next(item for item in claims if item.service_ids)
        safe_phrase = next(item for item in claims if item.text == "Would you be open to a brief conversation?")

        def text(*items):
            return OutreachText(text="\n".join(item.text for item in items), claims=items)

        content = OutreachContent.model_validate({
            "email": {"subject": "A quick introduction", "opening": text(fact), "body": text(inference, service_claim), "call_to_action": text(safe_phrase)},
            "whatsapp": {"opening": text(fact), "message": text(inference), "call_to_action": text(safe_phrase)},
            "call_script": {"opening": text(fact), "key_problem": text(inference), "recommended_service_ids": ["web_audit"], "discovery_questions": ["Which business outcome matters most to you right now?"], "objections_and_talking_points": text(service_claim), "closing": text(next(item for item in claims if item.text == "Thank you for your time."))},
            "follow_ups": [{"step": "OFFER_DISCOVERY_CALL", "timing": "WITHIN_THREE_DAYS", "purpose": "CONFIRM_NEXT_STEP"}],
            "recommended_service_ids": ["web_audit"],
        })
        fingerprint, snapshot, sales_context = source_state(intelligence, lead)
        artifact = OutreachArtifact(
            lead_id=lead_id, enrichment_run_id=intelligence.run.id, status=status,
            sales_context_version="sales-context-v1", sales_context=sales_context,
            source_fingerprint=fingerprint, source_snapshot=snapshot,
            contract_version="outreach-v1", prompt_version="grounded-outreach-prompt-v1",
            content=content.model_dump(mode="json"), validation_result={"valid": True},
        )
        session.add(artifact)
        await session.commit()
        artifact_id = artifact.id
    return lead_id, artifact_id


def auth():
    return {"Authorization": "Bearer operations-test-token"}


def send_body(channel="EMAIL", key="send-key-0001", **values):
    return {"channel": channel, "idempotency_key": key, **values}


def outcome_body(key="outcome-key-0001", **values):
    return {
        "activity_type": "CALL", "outcome": "CALLBACK_REQUESTED",
        "note": "Buyer asked for a follow-up call.",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "idempotency_key": key, **values,
    }


async def send_for_outcome(client, session_factory, monkeypatch, *, channel="EMAIL"):
    lead_id, artifact_id = await make_approved_artifact(session_factory)
    monkeypatch.setattr("app.services.outreach_operations.resolve_outreach_provider", lambda channel: FakeProvider(message_id=f"provider-{uuid4()}"))
    sent = await client.post(
        f"/api/outreach/{artifact_id}/send", headers=auth(), json=send_body(channel),
    )
    assert sent.status_code == 201, sent.text
    return lead_id, sent.json()


@pytest.mark.asyncio
async def test_send_requires_authentication(operations_client, session_factory):
    _, artifact_id = await make_approved_artifact(session_factory)
    async with operations_client as client:
        response = await client.post(f"/api/outreach/{artifact_id}/send", json=send_body())
    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [OutreachStatus.DRAFT, OutreachStatus.READY_FOR_REVIEW, OutreachStatus.REJECTED, OutreachStatus.STALE])
async def test_send_requires_approved_current_artifact(operations_client, session_factory, status):
    _, artifact_id = await make_approved_artifact(session_factory, status=status)
    async with operations_client as client:
        response = await client.post(f"/api/outreach/{artifact_id}/send", headers=auth(), json=send_body())
    assert response.status_code == 409
    assert response.json()["error"]["code"] in {"OUTREACH_NOT_APPROVED", "OUTREACH_STALE"}


@pytest.mark.asyncio
async def test_approved_but_outdated_artifact_is_marked_stale_and_rejected(operations_client, session_factory):
    lead_id, artifact_id = await make_approved_artifact(session_factory)
    async with session_factory() as session:
        lead = await session.get(Lead, lead_id)
        lead.sales_status = SalesStatus.QUALIFIED
        await session.commit()
    async with operations_client as client:
        response = await client.post(f"/api/outreach/{artifact_id}/send", headers=auth(), json=send_body())
        artifact_response = await client.get(f"/api/leads/{lead_id}/outreach", headers=auth())
    assert response.status_code == 409 and response.json()["error"]["code"] == "OUTREACH_STALE"
    assert artifact_response.json()["artifact"]["status"] == "STALE"


@pytest.mark.asyncio
async def test_edited_or_malformed_approved_content_cannot_bypass_revalidation(operations_client, session_factory):
    _, artifact_id = await make_approved_artifact(session_factory)
    async with session_factory() as session:
        artifact = await session.get(OutreachArtifact, artifact_id)
        artifact.content = {"email": {"body": "unsupported free text"}}
        await session.commit()
    async with operations_client as client:
        response = await client.post(f"/api/outreach/{artifact_id}/send", headers=auth(), json=send_body())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OUTREACH_CONTENT_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(("channel", "email", "phone", "code"), [
    ("EMAIL", "not-an-email", "+14155552671", "INVALID_EMAIL_DESTINATION"),
    ("WHATSAPP", "person@example.com", "4155552671", "INVALID_WHATSAPP_DESTINATION"),
])
async def test_invalid_destination_is_rejected(operations_client, session_factory, channel, email, phone, code):
    _, artifact_id = await make_approved_artifact(session_factory, email=email, phone=phone)
    async with operations_client as client:
        response = await client.post(f"/api/outreach/{artifact_id}/send", headers=auth(), json=send_body(channel))
    assert response.status_code == 422 and response.json()["error"]["code"] == code


@pytest.mark.asyncio
@pytest.mark.parametrize(("enabled", "code", "status_code"), [(False, "PROVIDER_DISABLED", 409), (True, "PROVIDER_NOT_CONFIGURED", 503)])
async def test_provider_unavailable_persists_safe_failure_without_activity(operations_client, session_factory, monkeypatch, enabled, code, status_code):
    _, artifact_id = await make_approved_artifact(session_factory)
    monkeypatch.setattr(settings, "outreach_email_enabled", enabled)
    monkeypatch.setattr(settings, "outreach_email_provider", "")
    monkeypatch.setattr(settings, "outreach_email_api_url", "")
    monkeypatch.setattr(settings, "outreach_email_api_key", "never-return-this-secret")
    monkeypatch.setattr(settings, "outreach_email_from", "")
    async with operations_client as client:
        response = await client.post(f"/api/outreach/{artifact_id}/send", headers=auth(), json=send_body())
        history = await client.get(f"/api/leads/{(await _artifact_lead(session_factory, artifact_id))}/outreach/executions", headers=auth())
    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code
    assert len(history.json()) == 1 and history.json()[0]["status"] == "FAILED"
    assert "never-return-this-secret" not in history.text
    async with session_factory() as session:
        assert await session.scalar(select(SalesActivity.id).where(SalesActivity.outreach_execution_id == UUID(history.json()[0]["id"]))) is None


@pytest.mark.asyncio
async def test_email_send_snapshot_activity_message_id_and_idempotency(operations_client, session_factory, monkeypatch):
    lead_id, artifact_id = await make_approved_artifact(session_factory)
    provider = FakeProvider()
    monkeypatch.setattr("app.services.outreach_operations.resolve_outreach_provider", lambda channel: provider)
    async with operations_client as client:
        response = await client.post(f"/api/outreach/{artifact_id}/send", headers=auth(), json=send_body())
        duplicate = await client.post(f"/api/outreach/{artifact_id}/send", headers=auth(), json=send_body())
        history = await client.get(f"/api/leads/{lead_id}/outreach/executions", headers=auth())
        another_key = await client.post(f"/api/outreach/{artifact_id}/send", headers=auth(), json=send_body(key="send-key-0002"))
    assert response.status_code == duplicate.status_code == 201
    assert response.json()["status"] == "SENT"
    assert response.json()["provider_message_id"] == "provider-msg-1"
    assert response.json()["recipient"] == "person@example.com"
    assert response.json()["content_snapshot"]["text"]
    assert len(history.json()) == 1 and len(provider.calls) == 1
    assert another_key.status_code == 409 and another_key.json()["error"]["code"] == "OUTREACH_ALREADY_EXECUTED"
    async with session_factory() as session:
        activity = await session.scalar(select(SalesActivity).where(SalesActivity.outreach_execution_id == UUID(response.json()["id"])))
        assert activity.activity_type == SalesActivityType.EMAIL
        assert str(response.json()["id"]) in activity.note


@pytest.mark.asyncio
async def test_whatsapp_send_uses_phone_and_mock_provider(operations_client, session_factory, monkeypatch):
    _, artifact_id = await make_approved_artifact(session_factory)
    provider = FakeProvider(message_id="wa-msg-22")
    monkeypatch.setattr("app.services.outreach_operations.resolve_outreach_provider", lambda channel: provider)
    async with operations_client as client:
        response = await client.post(f"/api/outreach/{artifact_id}/send", headers=auth(), json=send_body("WHATSAPP"))
    assert response.status_code == 201
    assert response.json()["recipient"] == "+14155552671"
    assert response.json()["subject"] is None
    assert response.json()["provider_message_id"] == "wa-msg-22"
    assert provider.calls[0].channel == "WHATSAPP"


@pytest.mark.asyncio
async def test_manual_execution_requires_confirmation_and_records_activity(operations_client, session_factory):
    _, artifact_id = await make_approved_artifact(session_factory)
    async with operations_client as client:
        prepared = await client.post(f"/api/outreach/{artifact_id}/send", headers=auth(), json=send_body("MANUAL", manual_method="EMAIL"))
        missing_confirmation = await client.post(f"/api/outreach/executions/{prepared.json()['id']}/mark-manual", headers=auth(), json={"confirmed": False})
        confirmed = await client.post(f"/api/outreach/executions/{prepared.json()['id']}/mark-manual", headers=auth(), json={"confirmed": True})
        repeated = await client.post(f"/api/outreach/executions/{prepared.json()['id']}/mark-manual", headers=auth(), json={"confirmed": True})
    assert prepared.status_code == 201 and prepared.json()["status"] == "QUEUED"
    assert missing_confirmation.status_code == 422
    assert confirmed.status_code == repeated.status_code == 200
    assert confirmed.json()["status"] == "SENT" and confirmed.json()["provider"] == "manual"
    async with session_factory() as session:
        activity = await session.scalar(select(SalesActivity).where(SalesActivity.outreach_execution_id == UUID(confirmed.json()["id"])))
        assert activity.activity_type == SalesActivityType.EMAIL


@pytest.mark.asyncio
async def test_manual_preparation_can_be_cancelled_without_activity(operations_client, session_factory):
    lead_id, artifact_id = await make_approved_artifact(session_factory)
    async with operations_client as client:
        prepared = await client.post(f"/api/outreach/{artifact_id}/send", headers=auth(), json=send_body("MANUAL", manual_method="WHATSAPP"))
        cancelled = await client.post(f"/api/outreach/executions/{prepared.json()['id']}/cancel", headers=auth())
        next_prepared = await client.post(f"/api/outreach/{artifact_id}/send", headers=auth(), json=send_body("MANUAL", key="manual-next-0001"))
    assert prepared.json()["status"] == "QUEUED"
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
    assert next_prepared.status_code == 201 and next_prepared.json()["status"] == "QUEUED"
    async with session_factory() as session:
        assert await session.scalar(select(SalesActivity.id).where(SalesActivity.lead_id == lead_id, SalesActivity.outreach_execution_id.is_not(None))) is None


@pytest.mark.asyncio
async def test_failed_send_is_safe_and_explicit_retry_preserves_history(operations_client, session_factory, monkeypatch):
    lead_id, artifact_id = await make_approved_artifact(session_factory)
    failing = FakeProvider(error=OutreachProviderFailure("PROVIDER_REJECTED", "safe failure"))
    monkeypatch.setattr("app.services.outreach_operations.resolve_outreach_provider", lambda channel: failing)
    async with operations_client as client:
        failed = await client.post(f"/api/outreach/{artifact_id}/send", headers=auth(), json=send_body())
        history_failed = await client.get(f"/api/leads/{lead_id}/outreach/executions", headers=auth())
        success = FakeProvider(message_id="retry-message")
        monkeypatch.setattr("app.services.outreach_operations.resolve_outreach_provider", lambda channel: success)
        retried = await client.post(f"/api/outreach/executions/{history_failed.json()[0]['id']}/retry", headers=auth(), json={"idempotency_key": "retry-key-0001"})
        history = await client.get(f"/api/leads/{lead_id}/outreach/executions", headers=auth())
    assert failed.status_code == 502 and failed.json()["error"]["code"] == "PROVIDER_REJECTED"
    assert len(history_failed.json()) == 1 and history_failed.json()[0]["status"] == "FAILED"
    assert "rejected" in history_failed.json()[0]["error_message"]
    assert retried.status_code == 201 and retried.json()["status"] == "SENT"
    assert retried.json()["attempt_number"] == 2
    assert retried.json()["retry_of_execution_id"] == history_failed.json()[0]["id"]
    assert len(history.json()) == 2
    async with session_factory() as session:
        activities = (await session.scalars(select(SalesActivity).where(SalesActivity.lead_id == lead_id, SalesActivity.outreach_execution_id.is_not(None)))).all()
        assert len(activities) == 1


@pytest.mark.asyncio
async def test_retry_revalidates_approval_and_currentness(operations_client, session_factory, monkeypatch):
    lead_id, artifact_id = await make_approved_artifact(session_factory)
    failing = FakeProvider(error=RuntimeError("secret=must-not-leak"))
    monkeypatch.setattr("app.services.outreach_operations.resolve_outreach_provider", lambda channel: failing)
    async with operations_client as client:
        await client.post(f"/api/outreach/{artifact_id}/send", headers=auth(), json=send_body())
        history = await client.get(f"/api/leads/{lead_id}/outreach/executions", headers=auth())
        async with session_factory() as session:
            lead = await session.get(Lead, lead_id)
            lead.sales_status = SalesStatus.QUALIFIED
            await session.commit()
        retry = await client.post(f"/api/outreach/executions/{history.json()[0]['id']}/retry", headers=auth(), json={"idempotency_key": "retry-key-0001"})
    assert retry.status_code == 409 and retry.json()["error"]["code"] == "OUTREACH_STALE"
    assert "secret" not in history.json()[0]["error_message"]


@pytest.mark.asyncio
async def test_cross_lead_execution_relationship_is_rejected(session_factory):
    lead_id, artifact_id = await make_approved_artifact(session_factory)
    other_id, _ = await make_approved_artifact(session_factory, email="other@example.com")
    engine = session_factory.kw["bind"]
    async with engine.connect() as connection:
        await connection.execute(text("PRAGMA foreign_keys=ON"))
    async with session_factory() as session:
        session.add(OutreachExecution(
            lead_id=other_id, outreach_artifact_id=artifact_id, channel="MANUAL", provider="manual",
            status="QUEUED", idempotency_key="cross-lead-key", content_snapshot={"text": ""},
        ))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
        first = OutreachExecution(
            lead_id=lead_id, outreach_artifact_id=artifact_id, channel="EMAIL", provider="test",
            status="FAILED", idempotency_key="retry-parent-1", content_snapshot={"text": ""},
        )
        session.add(first)
        await session.flush()
        session.add(OutreachExecution(
            lead_id=other_id, outreach_artifact_id=await _artifact_for_lead(session, other_id),
            retry_of_execution_id=first.id, channel="EMAIL", provider="test", status="FAILED",
            idempotency_key="retry-child-1", content_snapshot={"text": ""},
        ))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
    assert lead_id != other_id


@pytest.mark.asyncio
async def test_delivery_event_boundary_is_idempotent_and_only_provider_reported(operations_client, session_factory, monkeypatch):
    lead_id, artifact_id = await make_approved_artifact(session_factory)
    provider = FakeProvider()
    monkeypatch.setattr("app.services.outreach_operations.resolve_outreach_provider", lambda channel: provider)
    async with operations_client as client:
        sent = await client.post(f"/api/outreach/{artifact_id}/send", headers=auth(), json=send_body())
    assert sent.json()["status"] == "SENT" and sent.json()["delivered_at"] is None
    async with session_factory() as session:
        body = OutreachProviderEventIn(provider="mock_gateway", provider_event_id="event-1", event_type="DELIVERED", occurred_at=datetime.now(timezone.utc))
        first = await record_provider_event(session, UUID(sent.json()["id"]), body)
        duplicate = await record_provider_event(session, UUID(sent.json()["id"]), body)
        execution = await session.get(OutreachExecution, UUID(sent.json()["id"]))
        events = (await session.scalars(select(OutreachProviderEvent).where(OutreachProviderEvent.execution_id == execution.id))).all()
    assert first.id == duplicate.id and len(events) == 1
    assert execution.status == "DELIVERED" and execution.delivered_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["EMAIL", "WHATSAPP"])
async def test_http_json_channel_adapter_uses_mock_transport_without_real_send(monkeypatch, channel):
    received = []

    async def respond(request):
        received.append(request)
        return httpx.Response(202, json={"message_id": f"mock-{channel.lower()}"})

    transport = httpx.MockTransport(respond)
    provider = HttpJsonOutreachProvider(
        channel, "https://gateway.example/send", "test-provider-key",
        sender="sender@example.com" if channel == "EMAIL" else None,
        phone_number_id="test-phone-id" if channel == "WHATSAPP" else None,
        client_factory=lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs),
    )
    result = await provider.send(OutreachMessage(
        channel=channel, recipient="person@example.com" if channel == "EMAIL" else "+14155552671",
        subject="Subject" if channel == "EMAIL" else None, content="approved text",
        provider="http_json", idempotency_key="mock-idempotency-1",
    ))
    request = received[0]
    payload = __import__("json").loads(request.content)
    assert result.provider_message_id == f"mock-{channel.lower()}"
    assert request.headers["authorization"] == "Bearer test-provider-key"
    assert request.headers["idempotency-key"] == "mock-idempotency-1"
    assert payload["text"] == "approved text"
    assert payload["to"] == ("person@example.com" if channel == "EMAIL" else "+14155552671")
    if channel == "EMAIL":
        assert payload["from"] == "sender@example.com" and payload["subject"] == "Subject"
    else:
        assert payload["phone_number_id"] == "test-phone-id"


async def _artifact_lead(session_factory, artifact_id):
    async with session_factory() as session:
        artifact = await session.get(OutreachArtifact, artifact_id)
        return artifact.lead_id


async def _artifact_for_lead(session, lead_id):
    return await session.scalar(select(OutreachArtifact.id).where(OutreachArtifact.lead_id == lead_id))


@pytest.mark.asyncio
async def test_outreach_outcome_is_execution_scoped_idempotent_and_preserves_send_activity(operations_client, session_factory, monkeypatch):
    async with operations_client as client:
        lead_id, execution = await send_for_outcome(client, session_factory, monkeypatch)
        body = outcome_body()
        first = await client.post(f"/api/outreach/executions/{execution['id']}/outcomes", headers=auth(), json=body)
        duplicate = await client.post(f"/api/outreach/executions/{execution['id']}/outcomes", headers=auth(), json={**body, "note": "Changed retry payload is ignored."})
        second = await client.post(f"/api/outreach/executions/{execution['id']}/outcomes", headers=auth(), json=outcome_body("outcome-key-0002", outcome="MEETING_BOOKED"))
        timeline = await client.get(f"/api/leads/{lead_id}/activities", headers=auth())

    assert first.status_code == duplicate.status_code == second.status_code == 201
    assert first.json()["id"] == duplicate.json()["id"]
    assert first.json()["lead_id"] == str(lead_id)
    assert first.json()["outreach_execution_id"] == execution["id"]
    assert first.json()["suggested_task_type"] == "FOLLOW_UP"
    assert second.json()["id"] != first.json()["id"]
    linked = [item for item in timeline.json() if item["outreach_execution_id"] == execution["id"]]
    assert len(linked) == 3
    async with session_factory() as session:
        activities = (await session.scalars(select(SalesActivity).where(
            SalesActivity.outreach_execution_id == UUID(execution["id"]),
        ))).all()
        send_activity = next(item for item in activities if item.outreach_idempotency_key is None)
        outcomes = [item for item in activities if item.outreach_idempotency_key is not None]
        stored_execution = await session.get(OutreachExecution, UUID(execution["id"]))
        assert send_activity.outcome is None
        assert send_activity.note == f"Approved outreach confirmed sent via email. Execution {execution['id']}."
        assert send_activity.occurred_at == stored_execution.sent_at
        assert {item.outreach_idempotency_key for item in outcomes} == {"outcome-key-0001", "outcome-key-0002"}
        assert await session.scalar(select(SalesTask.id).where(SalesTask.lead_id == lead_id)) is None


@pytest.mark.asyncio
async def test_outreach_outcome_rejects_nonexistent_execution(operations_client):
    async with operations_client as client:
        response = await client.post(
            f"/api/outreach/executions/{UUID(int=999)}/outcomes",
            headers=auth(), json=outcome_body(),
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "EXECUTION_NOT_FOUND"


@pytest.mark.asyncio
async def test_outreach_idempotency_key_is_scoped_to_execution(operations_client, session_factory, monkeypatch):
    async with operations_client as client:
        first_lead, first_execution = await send_for_outcome(client, session_factory, monkeypatch)
        second_lead, second_execution = await send_for_outcome(client, session_factory, monkeypatch)
        first = await client.post(
            f"/api/outreach/executions/{first_execution['id']}/outcomes",
            headers=auth(), json=outcome_body("shared-outcome-key"),
        )
        second = await client.post(
            f"/api/outreach/executions/{second_execution['id']}/outcomes",
            headers=auth(), json=outcome_body("shared-outcome-key"),
        )
    assert first.status_code == second.status_code == 201
    assert first.json()["lead_id"] == str(first_lead)
    assert second.json()["lead_id"] == str(second_lead)
    assert first.json()["id"] != second.json()["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["QUEUED", "SENDING", "FAILED", "CANCELLED"])
async def test_outreach_outcome_rejects_unsent_execution(operations_client, session_factory, status):
    lead_id, artifact_id = await make_approved_artifact(session_factory)
    async with session_factory() as session:
        execution = OutreachExecution(
            lead_id=lead_id, outreach_artifact_id=artifact_id, channel="MANUAL",
            provider="manual", status=status, attempt_number=1,
            idempotency_key=f"unsent-key-{status.lower()}", content_snapshot={"text": "not sent"},
        )
        session.add(execution)
        await session.commit()
        execution_id = execution.id
    async with operations_client as client:
        response = await client.post(
            f"/api/outreach/executions/{execution_id}/outcomes",
            headers=auth(), json=outcome_body(),
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EXECUTION_NOT_SENT"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    {"outcome": None}, {"activity_type": "STATUS_CHANGE"},
    {"activity_type": "MADE_UP"}, {"outcome": "MADE_UP"},
    {"note": "   "}, {"occurred_at": "2026-09-29T10:00:00"},
    {"idempotency_key": "short"}, {"lead_id": "00000000-0000-0000-0000-000000000000"},
])
async def test_outreach_outcome_request_is_strict(operations_client, session_factory, monkeypatch, invalid):
    async with operations_client as client:
        _, execution = await send_for_outcome(client, session_factory, monkeypatch)
        response = await client.post(
            f"/api/outreach/executions/{execution['id']}/outcomes",
            headers=auth(), json={**outcome_body(), **invalid},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("required_field", [
    "activity_type", "outcome", "note", "occurred_at", "idempotency_key",
])
async def test_outreach_outcome_requires_all_contract_fields(operations_client, session_factory, monkeypatch, required_field):
    async with operations_client as client:
        _, execution = await send_for_outcome(client, session_factory, monkeypatch)
        body = outcome_body()
        del body[required_field]
        response = await client.post(
            f"/api/outreach/executions/{execution['id']}/outcomes",
            headers=auth(), json=body,
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_manual_outcome_requires_explicit_send_confirmation(operations_client, session_factory):
    lead_id, artifact_id = await make_approved_artifact(session_factory)
    async with operations_client as client:
        queued = await client.post(
            f"/api/outreach/{artifact_id}/send", headers=auth(),
            json=send_body("MANUAL", manual_method="EMAIL"),
        )
        rejected = await client.post(
            f"/api/outreach/executions/{queued.json()['id']}/outcomes",
            headers=auth(), json=outcome_body(),
        )
        confirmed = await client.post(
            f"/api/outreach/executions/{queued.json()['id']}/mark-manual",
            headers=auth(), json={"confirmed": True},
        )
        accepted = await client.post(
            f"/api/outreach/executions/{queued.json()['id']}/outcomes",
            headers=auth(), json=outcome_body(),
        )
    assert rejected.status_code == 409
    assert confirmed.status_code == 200
    assert accepted.status_code == 201
    assert accepted.json()["lead_id"] == str(lead_id)


@pytest.mark.asyncio
async def test_outreach_outcome_requires_bearer_auth(operations_client, session_factory, monkeypatch):
    async with operations_client as client:
        _, execution = await send_for_outcome(client, session_factory, monkeypatch)
        response = await client.post(
            f"/api/outreach/executions/{execution['id']}/outcomes", json=outcome_body(),
        )
    assert response.status_code == 401

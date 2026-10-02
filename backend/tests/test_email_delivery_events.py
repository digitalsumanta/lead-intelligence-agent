"""Focused service/API tests for append-only email delivery events."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app import main as main_module
from app.config import settings
from app.database import get_db
from app.models import (
    AiOutreachExecution, EmailDeliveryEvent, Lead, SalesActivity, SalesTask,
)
from app.schemas.email_delivery_event import (
    EmailDeliveryEventCreate, EmailDeliveryEventIngestRequest, NormalizedEmailDeliveryEvent,
)
from app.services.email_delivery_events import (
    EmailDeliveryEventError, email_delivery_event_ingestion_service, email_delivery_event_service,
)
from test_ai_outreach_execution import FakeEmailProvider, approve, prepared_email


def test_delivery_event_migration_and_model_are_current():
    import importlib.util
    from pathlib import Path
    from app.database import SCHEMA_REVISION
    from app.models import Base

    versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    migration_26_spec = importlib.util.spec_from_file_location(
        "phase11b_migration", versions / "0026_email_delivery_events.py",
    )
    migration_26 = importlib.util.module_from_spec(migration_26_spec)
    migration_26_spec.loader.exec_module(migration_26)
    migration_27_spec = importlib.util.spec_from_file_location(
        "phase11c_migration", versions / "0027_delivery_event_provider_identity.py",
    )
    migration_27 = importlib.util.module_from_spec(migration_27_spec)
    migration_27_spec.loader.exec_module(migration_27)
    assert (migration_26.revision, migration_26.down_revision) == (
        "0026_email_delivery_events", "0025_email_provider_status",
    )
    assert (migration_27.revision, migration_27.down_revision) == (
        "0027_delivery_provider_identity", "0026_email_delivery_events",
    )
    assert SCHEMA_REVISION == migration_27.revision
    table = Base.metadata.tables["email_delivery_events"]
    assert {"execution_id", "provider", "provider_event_id", "recipient_email", "event_type", "occurred_at", "recorded_at"}.issubset(table.c.keys())


async def sent_execution(session_factory):
    _, draft = await prepared_email(session_factory)
    approval = await approve(session_factory, draft)
    async with session_factory() as session:
        execution = await main_module.execute_approved_email(
            session, approval.id, provider=FakeEmailProvider(),
        )
    assert execution.status == "SENT"
    return execution


def event_request(execution_id, **overrides):
    values = {
        "execution_id": execution_id,
        "event_type": "DELIVERED",
        "provider_event_id": "delivery-event-1",
        "provider_status": "delivered",
        "source": "PROVIDER",
    }
    values.update(overrides)
    return EmailDeliveryEventCreate.model_validate(values)


def normalized_request(execution, **overrides):
    values = {
        "source": "PROVIDER", "provider": execution.provider,
        "provider_message_id": execution.provider_message_id,
        "provider_event_id": "normalized-event-1", "event_type": "DELIVERED",
        "recipient_email": execution.recipient, "provider_status": "delivered",
        "metadata": {"region": "us-east"},
    }
    values.update(overrides)
    return NormalizedEmailDeliveryEvent.model_validate(values)


@pytest.mark.parametrize("event_type", ["SENT", "DELIVERED", "BOUNCED", "FAILED", "COMPLAINED", "UNSUBSCRIBED"])
def test_normalized_contract_accepts_supported_event_types(event_type):
    event = NormalizedEmailDeliveryEvent(
        source="PROVIDER", provider="mock_email", provider_message_id="message-1", event_type=event_type,
    )
    assert event.event_type == event_type


def test_ingest_api_contract_rejects_unknown_type_execution_id_and_unsafe_metadata():
    values = {
        "provider": "smtp", "provider_message_id": "message-1", "event_type": "DELIVERED",
    }
    with pytest.raises(ValidationError):
        EmailDeliveryEventIngestRequest.model_validate({**values, "event_type": "OPENED"})
    with pytest.raises(ValidationError):
        EmailDeliveryEventIngestRequest.model_validate({**values, "execution_id": str(uuid4())})
    for key in ("signature", "webhook_secret", "headers", "raw", "raw_payload", "payload"):
        with pytest.raises(ValidationError):
            EmailDeliveryEventIngestRequest.model_validate({**values, "metadata": {key: "must not persist"}})


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["SENT", "DELIVERED", "BOUNCED", "FAILED", "COMPLAINED", "UNSUBSCRIBED"])
async def test_supported_delivery_event_types_are_append_only_facts(session_factory, event_type):
    execution = await sent_execution(session_factory)
    async with session_factory() as session:
        lead_before = await session.get(Lead, execution.lead_id)
        sales_snapshot = (
            lead_before.sales_status, lead_before.priority, lead_before.estimated_value,
            lead_before.proposal_value, lead_before.negotiation_value, lead_before.won_value,
            lead_before.expected_collection,
        )
        before_tasks = await session.scalar(select(func.count()).select_from(SalesTask))
        before_activities = await session.scalar(select(func.count()).select_from(SalesActivity))
        event, created = await email_delivery_event_service.record(
            session,
            event_request(execution.id, event_type=event_type, provider_event_id=f"event-{event_type}"),
        )
        assert created is True
        assert event.event_type == event_type
        assert event.recipient_email == execution.recipient
        assert event.provider_message_id == execution.provider_message_id
        assert event.provider_event_id == f"event-{event_type}"
        assert event.source == "PROVIDER"
        assert event.recorded_at.tzinfo is not None
        assert await session.scalar(select(func.count()).select_from(SalesTask)) == before_tasks
        assert await session.scalar(select(func.count()).select_from(SalesActivity)) == before_activities
        lead = await session.get(Lead, execution.lead_id)
        assert (
            lead.sales_status, lead.priority, lead.estimated_value, lead.proposal_value,
            lead.negotiation_value, lead.won_value, lead.expected_collection,
        ) == sales_snapshot


@pytest.mark.asyncio
async def test_delivery_event_duplicate_provider_id_returns_existing_event(session_factory):
    execution = await sent_execution(session_factory)
    request = event_request(execution.id)
    async with session_factory() as session:
        first, created = await email_delivery_event_service.record(session, request)
    async with session_factory() as session:
        replay, created = await email_delivery_event_service.record(
            session, request.model_copy(update={"provider_status": "different-on-replay"}),
        )
    assert created is False
    assert replay.id == first.id
    assert replay.provider_status == first.provider_status


@pytest.mark.asyncio
async def test_ingestion_resolves_execution_and_is_idempotent_but_rejects_conflicts(session_factory):
    execution = await sent_execution(session_factory)
    request = normalized_request(execution)
    async with session_factory() as session:
        first, execution_id, created = await email_delivery_event_ingestion_service.ingest(session, request)
    async with session_factory() as session:
        duplicate, duplicate_execution_id, replay_created = await email_delivery_event_ingestion_service.ingest(session, request)
    assert created and not replay_created
    assert first.id == duplicate.id
    assert execution_id == duplicate_execution_id == execution.id
    assert first.provider == execution.provider
    assert first.provider_event_id == request.provider_event_id
    assert first.provider_status == request.provider_status

    with pytest.raises(EmailDeliveryEventError) as conflict:
        async with session_factory() as session:
            await email_delivery_event_ingestion_service.ingest(
                session, request.model_copy(update={"event_type": "BOUNCED"}),
            )
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    assert conflict.value.status_code == 409

    with pytest.raises(EmailDeliveryEventError) as wrong_recipient:
        async with session_factory() as session:
            await email_delivery_event_ingestion_service.ingest(
                session, request.model_copy(update={"provider_event_id": "other-event", "recipient_email": "wrong@example.test"}),
            )
    assert wrong_recipient.value.code == "RECIPIENT_MISMATCH"

    with pytest.raises(EmailDeliveryEventError) as wrong_provider:
        async with session_factory() as session:
            await email_delivery_event_ingestion_service.ingest(
                session, request.model_copy(update={"provider_event_id": "provider-mismatch", "provider": "other-provider"}),
            )
    assert wrong_provider.value.code == "PROVIDER_MISMATCH"

    with pytest.raises(EmailDeliveryEventError) as unknown:
        async with session_factory() as session:
            await email_delivery_event_ingestion_service.ingest(
                session, request.model_copy(update={"provider_event_id": "unknown", "provider_message_id": "missing-message"}),
            )
    assert unknown.value.code == "EXECUTION_NOT_FOUND"


@pytest.mark.asyncio
async def test_ingestion_rejects_conflicting_reuse_of_provider_event_id(session_factory):
    execution = await sent_execution(session_factory)
    original = normalized_request(execution)
    async with session_factory() as session:
        first, _, _ = await email_delivery_event_ingestion_service.ingest(session, original)
    conflicting = original.model_copy(update={"provider_message_id": "different-message"})
    with pytest.raises(EmailDeliveryEventError) as conflict:
        async with session_factory() as session:
            await email_delivery_event_ingestion_service.ingest(session, conflicting)
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(EmailDeliveryEvent).where(
            EmailDeliveryEvent.provider == execution.provider,
            EmailDeliveryEvent.provider_event_id == original.provider_event_id,
        )) == 1
    assert first.execution_id == execution.id


@pytest.mark.asyncio
async def test_ingestion_rejects_ambiguous_execution_resolution():
    class AmbiguousRows:
        def all(self):
            return [object(), object()]

    class AmbiguousSession:
        async def scalars(self, _statement):
            return AmbiguousRows()

    request = NormalizedEmailDeliveryEvent(
        source="PROVIDER", provider="mock_email", provider_message_id="ambiguous-message",
        event_type="DELIVERED",
    )
    with pytest.raises(EmailDeliveryEventError) as ambiguous:
        await email_delivery_event_ingestion_service.ingest(AmbiguousSession(), request)
    assert ambiguous.value.code == "AMBIGUOUS_EXECUTION"
    assert ambiguous.value.status_code == 409


@pytest.mark.asyncio
async def test_execution_send_stages_sent_event_and_history_is_chronological(session_factory):
    execution = await sent_execution(session_factory)
    async with session_factory() as session:
        sent = await session.scalar(select(EmailDeliveryEvent).where(
            EmailDeliveryEvent.execution_id == execution.id,
            EmailDeliveryEvent.event_type == "SENT",
        ))
        assert sent is not None
        start = sent.occurred_at.replace(tzinfo=timezone.utc) if sent.occurred_at.tzinfo is None else sent.occurred_at
        later = event_request(execution.id, provider_event_id="chronology-later", occurred_at=start + timedelta(minutes=2))
        earlier = event_request(execution.id, provider_event_id="chronology-earlier", occurred_at=start + timedelta(minutes=1))
        first, _ = await email_delivery_event_service.record(session, later)
        unchanged = first.model_copy()
    async with session_factory() as session:
        await email_delivery_event_service.record(session, earlier)
        history = await email_delivery_event_service.history(session, execution.id)
    assert [item.event_type for item in history] == ["SENT", "DELIVERED", "DELIVERED"]
    assert history[1].id == history[2].id or history[1].occurred_at < history[2].occurred_at
    async with session_factory() as session:
        persisted = await session.get(EmailDeliveryEvent, first.id)
    assert persisted.provider_event_id == unchanged.provider_event_id


@pytest.mark.asyncio
async def test_unknown_execution_and_invalid_event_timestamps_are_rejected(session_factory):
    with pytest.raises(EmailDeliveryEventError) as missing:
        async with session_factory() as session:
            await email_delivery_event_service.record(session, event_request(uuid4()))
    assert missing.value.status_code == 404

    execution = await sent_execution(session_factory)
    invalid_time = event_request(execution.id, provider_event_id="old-event", occurred_at=datetime(2000, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(EmailDeliveryEventError, match="occurred_at"):
        async with session_factory() as session:
            await email_delivery_event_service.record(session, invalid_time)


@pytest.mark.asyncio
async def test_request_cannot_forge_recipient_and_rejects_secret_or_unbounded_metadata(session_factory):
    execution = await sent_execution(session_factory)
    with pytest.raises(ValidationError):
        event_request(execution.id, recipient_email="attacker@example.test")
    with pytest.raises(ValidationError, match="secret-like"):
        event_request(execution.id, metadata={"nested": {"smtp_password": "do-not-store"}})
    with pytest.raises(ValidationError, match="secret-like"):
        event_request(execution.id, metadata={"email_body": "must remain on execution"})
    with pytest.raises(ValidationError, match="4096 bytes"):
        event_request(execution.id, metadata={"note": "x" * 5000})
    with pytest.raises(ValidationError, match="nesting"):
        event_request(execution.id, metadata={"a": {"b": {"c": {"d": {"e": "too deep"}}}}})
    with pytest.raises(ValidationError):
        event_request(execution.id, event_type="OPENED")
    with pytest.raises(ValidationError, match="timezone"):
        event_request(execution.id, occurred_at=datetime(2026, 10, 1, 12, 0))


@pytest.mark.asyncio
async def test_delivery_event_api_requires_bearer_and_returns_idempotent_event(session_factory, monkeypatch):
    execution = await sent_execution(session_factory)
    monkeypatch.setattr(settings, "api_auth_token", "delivery-event-test-token")

    async def override_db():
        async with session_factory() as session:
            yield session

    main_module.app.dependency_overrides[get_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=main_module.app), base_url="http://test") as client:
            path = f"/api/outreach-executions/{execution.id}/delivery-events"
            assert (await client.get(path)).status_code == 401
            headers = {"Authorization": "Bearer delivery-event-test-token"}
            payload = {
                "execution_id": str(execution.id), "event_type": "DELIVERED",
                "provider_event_id": "api-provider-event", "provider_status": "delivered",
                "source": "PROVIDER", "metadata": {"region": "us-east"},
            }
            created = await client.post("/api/email-delivery-events", json=payload, headers=headers)
            replay = await client.post("/api/email-delivery-events", json=payload, headers=headers)
            ingest_payload = {
                "provider": execution.provider, "provider_message_id": execution.provider_message_id,
                "provider_event_id": "normalized-api-event", "event_type": "DELIVERED",
                "recipient_email": execution.recipient, "provider_status": "delivered",
                "metadata": {"region": "us-east"},
            }
            unauth_ingest = await client.post("/api/email-delivery-events/ingest", json=ingest_payload)
            ingested = await client.post("/api/email-delivery-events/ingest", json=ingest_payload, headers=headers)
            ingest_replay = await client.post("/api/email-delivery-events/ingest", json=ingest_payload, headers=headers)
            invalid_ingest = await client.post(
                "/api/email-delivery-events/ingest",
                json={**ingest_payload, "event_type": "OPENED"}, headers=headers,
            )
            history = await client.get(path, headers=headers)
        assert created.status_code == 201
        assert replay.status_code == 200
        assert replay.json()["id"] == created.json()["id"]
        assert unauth_ingest.status_code == 401
        assert invalid_ingest.status_code == 422
        assert ingested.status_code == 201 and ingested.json()["status"] == "CREATED"
        assert ingest_replay.status_code == 200 and ingest_replay.json()["status"] == "DUPLICATE"
        assert ingested.json()["event"]["provider"] == execution.provider
        assert created.json()["recipient_email"] == execution.recipient
        assert created.json()["provider_message_id"] == execution.provider_message_id
        assert "metadata" not in created.json()
        assert any(item["event_type"] == "SENT" for item in history.json()["items"])
    finally:
        main_module.app.dependency_overrides.pop(get_db, None)

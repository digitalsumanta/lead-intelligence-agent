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
from app.schemas.email_delivery_event import EmailDeliveryEventCreate
from app.services.email_delivery_events import (
    EmailDeliveryEventError, email_delivery_event_service,
)
from test_ai_outreach_execution import FakeEmailProvider, approve, prepared_email


def test_delivery_event_migration_and_model_are_current():
    import importlib.util
    from pathlib import Path
    from app.database import SCHEMA_REVISION
    from app.models import Base

    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0026_email_delivery_events.py"
    spec = importlib.util.spec_from_file_location("phase11b_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert (migration.revision, migration.down_revision) == (
        "0026_email_delivery_events", "0025_email_provider_status",
    )
    assert SCHEMA_REVISION == migration.revision
    table = Base.metadata.tables["email_delivery_events"]
    assert {"execution_id", "recipient_email", "event_type", "occurred_at", "recorded_at"}.issubset(table.c.keys())


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
            history = await client.get(path, headers=headers)
        assert created.status_code == 201
        assert replay.status_code == 200
        assert replay.json()["id"] == created.json()["id"]
        assert created.json()["recipient_email"] == execution.recipient
        assert created.json()["provider_message_id"] == execution.provider_message_id
        assert "metadata" not in created.json()
        assert any(item["event_type"] == "SENT" for item in history.json()["items"])
    finally:
        main_module.app.dependency_overrides.pop(get_db, None)

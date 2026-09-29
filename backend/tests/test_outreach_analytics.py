"""Read-only aggregate analytics for sent outreach executions."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import (
    EnrichmentRun, Lead, OutreachArtifact, OutreachExecution, OutreachStatus,
    SalesActivity, SalesActivityOutcome, SalesActivityType, SalesStatus,
)


@pytest.fixture
def analytics_client(session_factory, monkeypatch):
    async def override_db():
        async with session_factory() as session:
            yield session
    monkeypatch.setattr(settings, "api_auth_token", "analytics-test-token")
    app.dependency_overrides[get_db] = override_db
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    app.dependency_overrides.pop(get_db, None)


def auth():
    return {"Authorization": "Bearer analytics-test-token"}


async def add_execution(session_factory, *, sent_at, channel="EMAIL", status="SENT", lead_id=None, retry_of=None):
    async with session_factory() as session:
        lead = await session.get(Lead, lead_id) if lead_id else None
        if lead is None:
            lead = Lead(company_name="Analytics fixture", sales_status=SalesStatus.NEW)
            session.add(lead)
            await session.flush()
        if retry_of:
            parent = await session.get(OutreachExecution, retry_of)
            artifact = await session.get(OutreachArtifact, parent.outreach_artifact_id)
        else:
            run = EnrichmentRun(lead_id=lead.id, status="completed", pipeline_version="test")
            session.add(run)
            await session.flush()
            artifact = OutreachArtifact(
                lead_id=lead.id, enrichment_run_id=run.id, status=OutreachStatus.APPROVED,
                sales_context_version="test", sales_context={}, source_fingerprint="0" * 64,
                source_snapshot={}, contract_version="test", prompt_version="test",
                validation_result={},
            )
            session.add(artifact)
            await session.flush()
        execution = OutreachExecution(
            lead_id=lead.id, outreach_artifact_id=artifact.id,
            retry_of_execution_id=retry_of, channel=channel, provider="test", status=status,
            attempt_number=2 if retry_of else 1, idempotency_key=f"send-{uuid4()}",
            content_snapshot={}, requested_at=sent_at or datetime(2025, 1, 1, tzinfo=timezone.utc),
            sent_at=sent_at,
        )
        session.add(execution)
        await session.commit()
        return execution.id, lead.id


async def add_outcome(session_factory, execution_id, lead_id, *, outcome=SalesActivityOutcome.CONNECTED, key="key", occurred_at=None):
    async with session_factory() as session:
        session.add(SalesActivity(
            lead_id=lead_id, activity_type=SalesActivityType.CALL,
            occurred_at=occurred_at or datetime(2025, 1, 3, tzinfo=timezone.utc),
            note="fixture", outcome=outcome, outreach_execution_id=execution_id,
            outreach_idempotency_key=key,
        ))
        await session.commit()


@pytest.mark.asyncio
async def test_empty_cohort_is_complete_zero_filled_aggregate(analytics_client):
    async with analytics_client as client:
        response = await client.get("/api/outreach/analytics", headers=auth())
    assert response.status_code == 200
    body = response.json()
    assert body["attempts_total"] == body["recorded_outcomes_total"] == 0
    assert body["attempts_by_channel"] == [{"channel": x, "count": 0} for x in ("EMAIL", "WHATSAPP", "MANUAL")]
    assert body["attempts_by_status"] == [{"status": x, "count": 0} for x in ("QUEUED", "SENDING", "SENT", "DELIVERED", "FAILED", "CANCELLED")]
    assert [row["outcome"] for row in body["outcomes_by_type"]] == ["NO_RESPONSE", "CONNECTED", "INTERESTED", "NOT_INTERESTED", "CALLBACK_REQUESTED", "MEETING_BOOKED", "PROPOSAL_REQUESTED", "PROPOSAL_SENT", "NEGOTIATION_STARTED", "WON", "LOST", "INVALID_CONTACT"]
    assert all(row["activity_count"] == row["executions_with_outcome"] == row["leads_represented"] == 0 for row in body["outcomes_by_type"])
    assert body["outcome_coverage"] == {"eligible_sent_executions": 0, "executions_with_outcomes": 0, "executions_without_outcomes": 0}
    assert set(body) == {"cohort", "attempts_total", "attempts_by_channel", "attempts_by_status", "recorded_outcomes_total", "outcomes_by_type", "executions_with_recorded_outcomes", "leads_represented", "leads_with_recorded_outcomes", "outcome_coverage", "limitations"}


@pytest.mark.asyncio
async def test_utc_date_boundaries_and_one_sided_filters(analytics_client, session_factory):
    await add_execution(session_factory, sent_at=datetime(2025, 4, 1, 0, 0, tzinfo=timezone.utc))
    await add_execution(session_factory, sent_at=datetime(2025, 4, 2, 23, 59, 59, tzinfo=timezone.utc))
    await add_execution(session_factory, sent_at=datetime(2025, 4, 3, 0, 0, tzinfo=timezone.utc))
    async with analytics_client as client:
        bounded = await client.get("/api/outreach/analytics?from=2025-04-01&to=2025-04-02", headers=auth())
        from_only = await client.get("/api/outreach/analytics?from=2025-04-02", headers=auth())
        to_only = await client.get("/api/outreach/analytics?to=2025-04-01", headers=auth())
    assert bounded.json()["attempts_total"] == 2
    assert from_only.json()["attempts_total"] == 2
    assert to_only.json()["attempts_total"] == 1
    assert bounded.json()["cohort"] == {"from": "2025-04-01", "to": "2025-04-02", "channel": None, "date_field": "sent_at", "timezone": "UTC"}


@pytest.mark.asyncio
async def test_invalid_dates_channel_and_missing_auth_are_rejected(analytics_client):
    async with analytics_client as client:
        reversed_dates = await client.get("/api/outreach/analytics?from=2025-05-02&to=2025-05-01", headers=auth())
        invalid_channel = await client.get("/api/outreach/analytics?channel=SMS", headers=auth())
        invalid_date = await client.get("/api/outreach/analytics?from=not-a-date", headers=auth())
        unauthenticated = await client.get("/api/outreach/analytics")
    assert reversed_dates.status_code == 422
    assert invalid_channel.status_code == invalid_date.status_code == 422
    assert unauthenticated.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["EMAIL", "WHATSAPP", "MANUAL"])
async def test_supported_channel_filter_and_sent_only_cohort(analytics_client, session_factory, channel):
    await add_execution(session_factory, sent_at=datetime(2025, 6, 1, tzinfo=timezone.utc), channel=channel)
    await add_execution(session_factory, sent_at=datetime(2025, 6, 1, tzinfo=timezone.utc), channel="EMAIL" if channel != "EMAIL" else "MANUAL")
    await add_execution(session_factory, sent_at=None, channel=channel, status="FAILED")
    async with analytics_client as client:
        response = await client.get(f"/api/outreach/analytics?channel={channel}", headers=auth())
    assert response.status_code == 200
    body = response.json()
    assert body["attempts_total"] == 1
    expected = [{"channel": name, "count": 1 if name == channel else 0} for name in ("EMAIL", "WHATSAPP", "MANUAL")]
    assert body["attempts_by_channel"] == expected


@pytest.mark.asyncio
async def test_current_status_counts_include_each_status(analytics_client, session_factory):
    for status in ("QUEUED", "SENDING", "SENT", "DELIVERED", "FAILED", "CANCELLED"):
        await add_execution(session_factory, sent_at=datetime(2025, 7, 1, tzinfo=timezone.utc), status=status)
    async with analytics_client as client:
        response = await client.get("/api/outreach/analytics", headers=auth())
    body = response.json()
    assert body["attempts_total"] == 6
    assert body["attempts_by_status"] == [{"status": s, "count": 1} for s in ("QUEUED", "SENDING", "SENT", "DELIVERED", "FAILED", "CANCELLED")]
    assert "current persisted status, not historical status" in body["limitations"][2]


@pytest.mark.asyncio
async def test_retries_and_multiple_outcomes_do_not_duplicate_execution_or_lead_counts(analytics_client, session_factory):
    first, lead_id = await add_execution(session_factory, sent_at=datetime(2025, 8, 1, tzinfo=timezone.utc), status="FAILED")
    retry, _ = await add_execution(session_factory, sent_at=datetime(2025, 8, 1, tzinfo=timezone.utc), lead_id=lead_id, retry_of=first)
    await add_outcome(session_factory, retry, lead_id, outcome=SalesActivityOutcome.CONNECTED, key="first", occurred_at=datetime(2025, 8, 20, tzinfo=timezone.utc))
    await add_outcome(session_factory, retry, lead_id, outcome=SalesActivityOutcome.CONNECTED, key="second")
    async with analytics_client as client:
        response = await client.get("/api/outreach/analytics?from=2025-08-01&to=2025-08-01", headers=auth())
    body = response.json()
    connected = next(row for row in body["outcomes_by_type"] if row["outcome"] == "CONNECTED")
    assert body["attempts_total"] == 2
    assert body["leads_represented"] == 1
    assert body["recorded_outcomes_total"] == connected["activity_count"] == 2
    assert connected["executions_with_outcome"] == connected["leads_represented"] == 1
    assert body["outcome_coverage"] == {"eligible_sent_executions": 2, "executions_with_outcomes": 1, "executions_without_outcomes": 1}
    assert body["leads_with_recorded_outcomes"] == 1


@pytest.mark.asyncio
async def test_outcome_cohort_uses_execution_sent_at_not_activity_time(analytics_client, session_factory):
    inside, lead_inside = await add_execution(session_factory, sent_at=datetime(2025, 9, 1, tzinfo=timezone.utc))
    outside, lead_outside = await add_execution(session_factory, sent_at=datetime(2025, 8, 31, 23, 59, tzinfo=timezone.utc))
    await add_outcome(session_factory, inside, lead_inside, occurred_at=datetime(2025, 10, 1, tzinfo=timezone.utc))
    await add_outcome(session_factory, outside, lead_outside, occurred_at=datetime(2025, 9, 1, 12, tzinfo=timezone.utc))
    async with analytics_client as client:
        response = await client.get("/api/outreach/analytics?from=2025-09-01&to=2025-09-01", headers=auth())
    body = response.json()
    assert body["attempts_total"] == body["recorded_outcomes_total"] == 1
    assert body["leads_represented"] == body["leads_with_recorded_outcomes"] == 1


@pytest.mark.asyncio
async def test_unkeyed_and_unstructured_activities_are_excluded_and_no_response_is_explicit(analytics_client, session_factory):
    execution_id, lead_id = await add_execution(session_factory, sent_at=datetime(2025, 10, 1, tzinfo=timezone.utc))
    await add_outcome(session_factory, execution_id, lead_id, outcome=SalesActivityOutcome.NO_RESPONSE, key="no-response")
    async with session_factory() as session:
        session.add_all([
            SalesActivity(lead_id=lead_id, activity_type=SalesActivityType.CALL, occurred_at=datetime.now(timezone.utc), note="ordinary activity", outcome=SalesActivityOutcome.CONNECTED, outreach_execution_id=execution_id),
            SalesActivity(lead_id=lead_id, activity_type=SalesActivityType.CALL, occurred_at=datetime.now(timezone.utc), note="no structured outcome", outreach_execution_id=execution_id, outreach_idempotency_key="no-outcome"),
        ])
        await session.commit()
    async with analytics_client as client:
        response = await client.get("/api/outreach/analytics", headers=auth())
    body = response.json()
    no_response = next(row for row in body["outcomes_by_type"] if row["outcome"] == "NO_RESPONSE")
    connected = next(row for row in body["outcomes_by_type"] if row["outcome"] == "CONNECTED")
    assert body["recorded_outcomes_total"] == no_response["activity_count"] == 1
    assert connected["activity_count"] == 0
    assert body["outcome_coverage"]["executions_with_outcomes"] == 1
    assert body["outcome_coverage"]["executions_without_outcomes"] == 0
    assert "do not establish causal attribution" in body["limitations"][0]

"""Focused API/service coverage for deterministic sales task management."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import (
    EnrichmentRun, Lead, OutreachArtifact, OutreachStatus, SalesActivity,
    SalesActivityOutcome, SalesActivityType, SalesPriority, SalesStatus, SalesTaskStatus,
    SalesTaskType,
)
from app.services.sales_tasks import suggested_task_type, task_due_state


@pytest.fixture
def task_client(session_factory, monkeypatch):
    async def override_db():
        async with session_factory() as session:
            yield session
    monkeypatch.setattr(settings, "api_auth_token", "task-test-token")
    app.dependency_overrides[get_db] = override_db
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    app.dependency_overrides.pop(get_db, None)


async def make_lead(session_factory, name="Task Lead"):
    async with session_factory() as session:
        lead = Lead(company_name=name)
        session.add(lead)
        await session.flush()
        run = EnrichmentRun(lead_id=lead.id, status="completed", pipeline_version="test", started_at=datetime.now(timezone.utc))
        session.add(run)
        await session.flush()
        activity = SalesActivity(lead_id=lead.id, activity_type=SalesActivityType.CALL, note="Connected")
        session.add(activity)
        await session.flush()
        artifact = OutreachArtifact(
            lead_id=lead.id, enrichment_run_id=run.id, status=OutreachStatus.READY_FOR_REVIEW,
            sales_context_version="test", sales_context={"sales_status": "NEW"}, source_fingerprint="x" * 64,
            source_snapshot={}, contract_version="test", prompt_version="test", content=None,
            validation_result={"valid": True},
        )
        session.add(artifact)
        await session.commit()
        return lead.id, activity.id, artifact.id


def payload(due_at=None, **overrides):
    result = {
        "task_type": "FOLLOW_UP", "title": "Call back the lead",
        "due_at": due_at if isinstance(due_at, str) else (due_at or datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "priority": "HIGH", "assigned_to": "Rep A",
    }
    result.update(overrides)
    return result


@pytest.mark.asyncio
async def test_create_update_complete_and_terminal_transition_rules(task_client, session_factory):
    lead_id, _, _ = await make_lead(session_factory)
    headers = {"Authorization": "Bearer task-test-token"}
    async with task_client as client:
        created = await client.post(f"/api/leads/{lead_id}/tasks", headers=headers, json=payload())
        task_id = created.json()["id"]
        updated = await client.patch(f"/api/tasks/{task_id}", headers=headers, json={"title": "Prepare a call"})
        started = await client.patch(f"/api/tasks/{task_id}", headers=headers, json={"status": "IN_PROGRESS"})
        completed = await client.post(f"/api/tasks/{task_id}/complete", headers=headers)
        complete_again = await client.post(f"/api/tasks/{task_id}/complete", headers=headers)
        cancel_completed = await client.post(f"/api/tasks/{task_id}/cancel", headers=headers)
        patch_completed = await client.patch(f"/api/tasks/{task_id}", headers=headers, json={"title": "Change"})
    assert created.status_code == 201 and created.json()["status"] == "OPEN"
    assert updated.json()["title"] == "Prepare a call"
    assert started.json()["status"] == "IN_PROGRESS"
    assert completed.json()["status"] == "COMPLETED" and completed.json()["completed_at"]
    assert not completed.json()["is_due"] and not completed.json()["is_overdue"]
    assert complete_again.status_code == 422 and cancel_completed.status_code == 422
    assert patch_completed.status_code == 422


@pytest.mark.asyncio
async def test_cancel_transitions_and_overdue_calculation(task_client, session_factory):
    lead_id, _, _ = await make_lead(session_factory)
    headers = {"Authorization": "Bearer task-test-token"}
    async with task_client as client:
        overdue = await client.post(f"/api/leads/{lead_id}/tasks", headers=headers, json=payload(datetime.now(timezone.utc) - timedelta(hours=2)))
        cancelled_task = await client.post(f"/api/leads/{lead_id}/tasks", headers=headers, json=payload(title="Cancel this"))
        cancelled = await client.post(f"/api/tasks/{cancelled_task.json()['id']}/cancel", headers=headers)
        complete_cancelled = await client.post(f"/api/tasks/{cancelled_task.json()['id']}/complete", headers=headers)
        overdue_rows = await client.get("/api/tasks?overdue=true", headers=headers)
        due_rows = await client.get("/api/tasks?due=true", headers=headers)
    assert overdue.json()["is_due"] is True and overdue.json()["is_overdue"] is True
    assert cancelled.json()["status"] == "CANCELLED" and cancelled.json()["completed_at"] is None
    assert complete_cancelled.status_code == 422
    assert [item["id"] for item in overdue_rows.json()] == [overdue.json()["id"]]
    assert overdue.json()["id"] in [item["id"] for item in due_rows.json()]


@pytest.mark.asyncio
async def test_timezone_validation_and_relationship_ownership(task_client, session_factory):
    lead_id, activity_id, artifact_id = await make_lead(session_factory)
    other_lead, _, _ = await make_lead(session_factory, "Other Task Lead")
    headers = {"Authorization": "Bearer task-test-token"}
    async with task_client as client:
        naive = await client.post(f"/api/leads/{lead_id}/tasks", headers=headers, json=payload("2026-09-30T12:00:00"))
        bad_activity = await client.post(f"/api/leads/{other_lead}/tasks", headers=headers, json=payload(related_activity_id=str(activity_id)))
        bad_outreach = await client.post(f"/api/leads/{other_lead}/tasks", headers=headers, json=payload(related_outreach_artifact_id=str(artifact_id)))
        good = await client.post(f"/api/leads/{lead_id}/tasks", headers=headers, json=payload(related_activity_id=str(activity_id), related_outreach_artifact_id=str(artifact_id)))
    assert naive.status_code == 422
    assert bad_activity.status_code == 422 and bad_activity.json()["error"]["code"] == "ACTIVITY_NOT_FOUND"
    assert bad_outreach.status_code == 422 and bad_outreach.json()["error"]["code"] == "OUTREACH_NOT_FOUND"
    assert good.status_code == 201


@pytest.mark.asyncio
async def test_activity_outcomes_and_human_next_action_suggestions(task_client, session_factory):
    lead_id, _, _ = await make_lead(session_factory)
    headers = {"Authorization": "Bearer task-test-token"}
    async with task_client as client:
        recorded = await client.post(f"/api/leads/{lead_id}/activities", headers=headers, json={
            "activity_type": "CALL", "note": "Asked for a callback", "outcome": "CALLBACK_REQUESTED",
        })
        legacy = await client.post(f"/api/leads/{lead_id}/activities", headers=headers, json={
            "activity_type": "NOTE", "note": "No outcome supplied",
        })
    assert recorded.status_code == 201
    assert recorded.json()["outcome"] == "CALLBACK_REQUESTED"
    assert recorded.json()["suggested_task_type"] == "FOLLOW_UP"
    assert legacy.status_code == 201 and legacy.json()["outcome"] is None
    assert suggested_task_type(SalesActivityOutcome.MEETING_BOOKED) == SalesTaskType.MEETING
    assert suggested_task_type(SalesActivityOutcome.PROPOSAL_REQUESTED) == SalesTaskType.PROPOSAL
    assert suggested_task_type(SalesActivityOutcome.CONNECTED) is None


@pytest.mark.asyncio
async def test_filters_lead_scoping_and_authentication(task_client, session_factory):
    lead_id, _, _ = await make_lead(session_factory)
    other_lead, _, _ = await make_lead(session_factory, "Other Filter Lead")
    headers = {"Authorization": "Bearer task-test-token"}
    async with task_client as client:
        first = await client.post(f"/api/leads/{lead_id}/tasks", headers=headers, json=payload(task_type="CALL"))
        second = await client.post(f"/api/leads/{other_lead}/tasks", headers=headers, json=payload(task_type="MEETING"))
        await client.patch(f"/api/tasks/{second.json()['id']}", headers=headers, json={"status": "IN_PROGRESS"})
        filtered = await client.get("/api/tasks?status=OPEN&task_type=CALL&priority=HIGH&assigned_to=Rep%20A", headers=headers)
        by_lead = await client.get(f"/api/leads/{lead_id}/tasks", headers=headers)
        unauthorized = await client.get("/api/tasks")
        summary = await client.get("/api/tasks/summary", headers=headers)
    assert first.status_code == second.status_code == 201
    assert len(filtered.json()) == 1 and filtered.json()[0]["id"] == first.json()["id"]
    assert [item["lead_id"] for item in by_lead.json()] == [str(lead_id)]
    assert unauthorized.status_code == 401
    assert set(summary.json()) == {"today", "overdue", "upcoming", "outcome_suggestions"}


@pytest.mark.asyncio
async def test_action_center_adds_sales_commercial_context_and_unhandled_outcome_suggestions(task_client, session_factory):
    lead_id, _, _ = await make_lead(session_factory, "Action Center Lead")
    async with session_factory() as session:
        lead = await session.get(Lead, lead_id)
        lead.sales_status = SalesStatus.PROPOSAL
        lead.priority = SalesPriority.URGENT
        lead.assigned_to = "Rep A"
        lead.proposal_value = Decimal("1250.50")
        lead.currency = "USD"
        lead.next_follow_up_at = datetime.now(timezone.utc) + timedelta(days=1)
        await session.commit()

    headers = {"Authorization": "Bearer task-test-token"}
    async with task_client as client:
        due_task = await client.post(
            f"/api/leads/{lead_id}/tasks", headers=headers,
            json=payload(datetime.now(timezone.utc) + timedelta(days=2), task_type="PROPOSAL"),
        )
        suggestion_response = await client.post(
            f"/api/leads/{lead_id}/activities", headers=headers,
            json={"activity_type": "CALL", "outcome": "PROPOSAL_REQUESTED", "note": "Private note is not in summary"},
        )
        summary = await client.get("/api/tasks/summary", headers=headers)

    assert due_task.status_code == 201
    assert suggestion_response.status_code == 201
    body = summary.json()
    task = next(row for row in body["upcoming"] if row["id"] == due_task.json()["id"])
    assert task["lead_sales_status"] == "PROPOSAL"
    assert task["lead_priority"] == "URGENT"
    assert task["lead_assigned_to"] == "Rep A"
    assert task["current_stage_value"] == "1250.50"
    assert task["current_stage_value_field"] == "proposal_value"
    assert task["currency"] == "USD"
    assert task["next_follow_up_at"] is not None

    suggestions = body["outcome_suggestions"]
    assert len(suggestions) == 1
    assert suggestions[0]["activity_id"] == suggestion_response.json()["id"]
    assert suggestions[0]["outcome"] == "PROPOSAL_REQUESTED"
    assert suggestions[0]["suggested_task_type"] == "PROPOSAL"
    assert "note" not in suggestions[0]
    assert "Private note" not in str(suggestions)


@pytest.mark.asyncio
@pytest.mark.parametrize(("sales_status", "expected_field", "expected_value"), [
    (SalesStatus.NEW, "estimated_value", "100.10"),
    (SalesStatus.QUALIFIED, "estimated_value", "100.10"),
    (SalesStatus.CONTACTED, "estimated_value", "100.10"),
    (SalesStatus.ENGAGED, "estimated_value", "100.10"),
    (SalesStatus.PROPOSAL, "proposal_value", "200.20"),
    (SalesStatus.NEGOTIATION, "negotiation_value", "300.30"),
    (SalesStatus.WON, "won_value", "400.40"),
    (SalesStatus.LOST, None, None),
])
async def test_action_center_uses_only_the_commercial_field_for_current_stage(
    task_client, session_factory, sales_status, expected_field, expected_value,
):
    lead_id, _, _ = await make_lead(session_factory, f"Stage {sales_status.value}")
    async with session_factory() as session:
        lead = await session.get(Lead, lead_id)
        lead.sales_status = sales_status
        lead.estimated_value = Decimal("100.10")
        lead.proposal_value = Decimal("200.20")
        lead.negotiation_value = Decimal("300.30")
        lead.won_value = Decimal("400.40")
        lead.currency = "EUR"
        await session.commit()

    headers = {"Authorization": "Bearer task-test-token"}
    async with task_client as client:
        created = await client.post(
            f"/api/leads/{lead_id}/tasks", headers=headers,
            json=payload(datetime.now(timezone.utc) + timedelta(days=2)),
        )
        summary = await client.get("/api/tasks/summary", headers=headers)
    task = next(row for row in summary.json()["upcoming"] if row["id"] == created.json()["id"])
    assert task["current_stage_value_field"] == expected_field
    assert task["current_stage_value"] == expected_value
    assert task["currency"] == "EUR"


@pytest.mark.asyncio
async def test_action_center_suggestions_use_existing_mapping_and_hide_linked_activities(task_client, session_factory):
    lead_id, _, _ = await make_lead(session_factory, "Outcome Suggestions Lead")
    headers = {"Authorization": "Bearer task-test-token"}
    async with task_client as client:
        callback = await client.post(
            f"/api/leads/{lead_id}/activities", headers=headers,
            json={"activity_type": "CALL", "outcome": "CALLBACK_REQUESTED", "note": "Callback requested"},
        )
        meeting = await client.post(
            f"/api/leads/{lead_id}/activities", headers=headers,
            json={"activity_type": "MEETING", "outcome": "MEETING_BOOKED", "note": "Meeting booked"},
        )
        unsupported = await client.post(
            f"/api/leads/{lead_id}/activities", headers=headers,
            json={"activity_type": "CALL", "outcome": "CONNECTED", "note": "Connected"},
        )
        task_for_callback = await client.post(
            f"/api/leads/{lead_id}/tasks", headers=headers,
            json=payload(related_activity_id=callback.json()["id"]),
        )
        task_for_meeting = await client.post(
            f"/api/leads/{lead_id}/tasks", headers=headers,
            json=payload(related_activity_id=meeting.json()["id"]),
        )
        await client.post(f"/api/tasks/{task_for_callback.json()['id']}/complete", headers=headers)
        await client.post(f"/api/tasks/{task_for_meeting.json()['id']}/cancel", headers=headers)
        summary = await client.get("/api/tasks/summary", headers=headers)

    assert unsupported.status_code == 201
    assert summary.status_code == 200
    assert summary.json()["outcome_suggestions"] == []


def test_sales_task_migration_follows_outreach_and_is_reversible():
    from pathlib import Path
    import importlib.util
    migration_path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0011_sales_tasks.py"
    spec = importlib.util.spec_from_file_location("0011_sales_tasks", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    source = migration_path.read_text(encoding="utf-8")
    assert migration.revision == "0011_sales_tasks"
    assert migration.down_revision == "0010_outreach_preparation"
    assert "fk_sales_tasks_activity_lead" in source
    assert "fk_sales_tasks_outreach_lead" in source
    assert "op.drop_table(\"sales_tasks\")" in source
    assert "activity_outcome.drop" in source


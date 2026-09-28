"""Sales workflow API, validation, and isolation coverage."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from app.config import settings
from app.database import get_db
from app.main import app
from app.models import Lead, SalesActivity, SalesActivityType, SalesPriority, SalesStatus


@pytest.fixture
def sales_client(session_factory, monkeypatch):
    async def override_db():
        async with session_factory() as session:
            yield session
    monkeypatch.setattr(settings, "api_auth_token", "sales-test-token")
    app.dependency_overrides[get_db] = override_db
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_sales_workflow_transitions_activities_and_filters(sales_client, session_factory):
    async with session_factory() as session:
        first = Lead(company_name="Sales One")
        second = Lead(company_name="Sales Two")
        session.add_all([first, second])
        await session.commit()
        lead_id, other_id = first.id, second.id
    headers = {"Authorization": "Bearer sales-test-token"}
    async with sales_client as client:
        initial = await client.get(f"/api/leads/{lead_id}/sales", headers=headers)
        bad_transition = await client.patch(f"/api/leads/{lead_id}/sales", headers=headers, json={"sales_status": "WON"})
        bad_priority = await client.patch(f"/api/leads/{lead_id}/sales", headers=headers, json={"priority": "ASAP"})
        update = await client.patch(f"/api/leads/{lead_id}/sales", headers=headers, json={"sales_status": "QUALIFIED", "priority": "HIGH", "assigned_to": "Rep A", "next_follow_up_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()})
        created = await client.post(f"/api/leads/{lead_id}/activities", headers=headers, json={"activity_type": "CALL", "note": "  Introductory call  "})
        forged = await client.post(f"/api/leads/{lead_id}/activities", headers=headers, json={"activity_type": "NOTE", "note": "bad", "occurred_at": datetime.now(timezone.utc).isoformat()})
        system = await client.post(f"/api/leads/{lead_id}/activities", headers=headers, json={"activity_type": "STATUS_CHANGE", "note": "forged"})
        timeline = await client.get(f"/api/leads/{lead_id}/activities", headers=headers)
        due = await client.get("/api/leads?follow_up_due=true&sales_status=QUALIFIED&priority=HIGH", headers=headers)
        lost = await client.patch(f"/api/leads/{lead_id}/sales", headers=headers, json={"sales_status": "LOST"})
        lost_with_reason = await client.patch(f"/api/leads/{lead_id}/sales", headers=headers, json={"sales_status": "LOST", "lost_reason": "No budget"})
        resumed = await client.patch(f"/api/leads/{lead_id}/sales", headers=headers, json={"sales_status": "NEW"})
        isolated = await client.get(f"/api/leads/{other_id}/activities", headers=headers)
    assert initial.status_code == 200 and initial.json()["sales_status"] == "NEW"
    assert bad_transition.status_code == 422 and bad_transition.json()["error"]["code"] == "INVALID_SALES_TRANSITION"
    assert bad_priority.status_code == 422
    assert update.status_code == 200 and update.json()["sales_status"] == "QUALIFIED"
    assert created.status_code == 201 and created.json()["note"] == "Introductory call"
    assert forged.status_code == 422 and system.status_code == 422
    assert [row["activity_type"] for row in timeline.json()] == ["CALL", "STATUS_CHANGE"]
    assert due.status_code == 200 and [row["id"] for row in due.json()] == [str(lead_id)]
    assert due.json()[0]["sales_status"] == "QUALIFIED" and due.json()[0]["priority"] == "HIGH"
    assert lost.status_code == 422 and lost_with_reason.status_code == 200
    assert resumed.status_code == 200 and resumed.json()["lost_reason"] is None
    assert isolated.status_code == 200 and isolated.json() == []
    async with session_factory() as session:
        activities = (await session.scalars(select(SalesActivity).where(SalesActivity.lead_id == lead_id))).all()
        assert sum(row.activity_type == SalesActivityType.STATUS_CHANGE for row in activities) == 3
        lead = await session.get(Lead, lead_id)
        assert lead.sales_status == SalesStatus.NEW and lead.priority == SalesPriority.HIGH


@pytest.mark.asyncio
async def test_sales_activity_rejects_cross_lead_and_unknown_lead(sales_client, session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Only lead")
        session.add(lead)
        await session.commit()
        lead_id = lead.id
    headers = {"Authorization": "Bearer sales-test-token"}
    async with sales_client as client:
        missing = await client.get(f"/api/leads/{uuid4()}/activities", headers=headers)
        activity = await client.post(f"/api/leads/{lead_id}/activities", headers=headers, json={"activity_type": "UNRECOGNIZED", "note": "x"})
    assert missing.status_code == 404
    assert activity.status_code == 422

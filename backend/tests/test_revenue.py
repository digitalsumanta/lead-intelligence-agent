"""Deterministic commercial value, collection, target, and conversion tests."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import importlib.util

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import Lead, SalesStatus


@pytest.fixture
def revenue_client(session_factory, monkeypatch):
    async def override_db():
        async with session_factory() as session:
            yield session
    monkeypatch.setattr(settings, "api_auth_token", "revenue-test-token")
    app.dependency_overrides[get_db] = override_db
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    app.dependency_overrides.pop(get_db, None)


async def make_lead(session_factory, *, status=SalesStatus.NEW, currency="INR", company="Revenue Lead"):
    async with session_factory() as session:
        lead = Lead(company_name=company, sales_status=status, currency=currency)
        session.add(lead)
        await session.commit()
        return lead.id


def commercial(**values):
    return {"estimated_value": "1000.10", "proposal_value": "800.25", "negotiation_value": "750.50", "won_value": "700.75", "expected_collection": "650.25", **values}


def test_revenue_migration_follows_tasks_and_contains_reversible_money_schema():
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0012_revenue_conversion.py"
    spec = importlib.util.spec_from_file_location("0012_revenue_conversion", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    source = path.read_text(encoding="utf-8")
    assert migration.revision == "0012_revenue_conversion"
    assert migration.down_revision == "0011_sales_tasks"
    assert "sa.Numeric(18, 2)" in source
    assert 'op.drop_table("sales_collections")' in source
    assert 'op.drop_table("revenue_targets")' in source


@pytest.mark.asyncio
async def test_commercial_precision_currency_lock_and_auth(revenue_client, session_factory):
    lead_id = await make_lead(session_factory)
    headers = {"Authorization": "Bearer revenue-test-token"}
    async with revenue_client as client:
        unauthorized = await client.get(f"/api/leads/{lead_id}/commercial")
        updated = await client.patch(f"/api/leads/{lead_id}/commercial", headers=headers, json=commercial())
        currency_change = await client.patch(f"/api/leads/{lead_id}/commercial", headers=headers, json={"currency": "USD"})
        bad_money = await client.patch(f"/api/leads/{lead_id}/commercial", headers=headers, json={"estimated_value": "-0.01"})
    assert unauthorized.status_code == 401
    assert updated.status_code == 200
    assert updated.json()["estimated_value"] == "1000.10"
    assert updated.json()["proposal_value"] == "800.25"
    assert updated.json()["currency"] == "INR"
    assert currency_change.status_code == 422 and currency_change.json()["error"]["code"] == "CURRENCY_LOCKED"
    assert bad_money.status_code == 422


@pytest.mark.asyncio
async def test_collection_ledger_partial_payments_outstanding_and_validation(revenue_client, session_factory):
    lead_id = await make_lead(session_factory, status=SalesStatus.WON)
    other_id = await make_lead(session_factory, status=SalesStatus.WON, company="Other Won Lead")
    headers = {"Authorization": "Bearer revenue-test-token"}
    async with revenue_client as client:
        await client.patch(f"/api/leads/{lead_id}/commercial", headers=headers, json={"won_value": "100.00", "expected_collection": "100.00"})
        first = await client.post(f"/api/leads/{lead_id}/collections", headers=headers, json={"amount": "30.10", "collection_date": datetime.now(timezone.utc).isoformat(), "payment_reference": "R1"})
        second = await client.post(f"/api/leads/{lead_id}/collections", headers=headers, json={"amount": "69.90", "collection_date": datetime.now(timezone.utc).isoformat(), "payment_reference": "R2"})
        negative = await client.post(f"/api/leads/{lead_id}/collections", headers=headers, json={"amount": "-1", "collection_date": datetime.now(timezone.utc).isoformat()})
        over_limit = await client.post(f"/api/leads/{lead_id}/collections", headers=headers, json={"amount": "0.01", "collection_date": datetime.now(timezone.utc).isoformat()})
        cross_lead = await client.post(f"/api/leads/{other_id}/collections", headers=headers, json={"amount": "1.00", "collection_date": datetime.now(timezone.utc).isoformat()})
        commercial_read = await client.get(f"/api/leads/{lead_id}/commercial", headers=headers)
        rows = await client.get(f"/api/leads/{lead_id}/collections", headers=headers)
    assert first.status_code == second.status_code == 201
    assert negative.status_code == 422
    assert over_limit.status_code == 422
    assert cross_lead.status_code == 409
    assert commercial_read.json()["actual_collected"] == "100.00"
    assert commercial_read.json()["outstanding_collection"] == "0.00"
    assert len(rows.json()) == 2


@pytest.mark.asyncio
async def test_collection_patch_rejects_null_required_fields_and_supports_edit_delete(revenue_client, session_factory):
    lead_id = await make_lead(session_factory, status=SalesStatus.WON)
    headers = {"Authorization": "Bearer revenue-test-token"}
    async with revenue_client as client:
        await client.patch(f"/api/leads/{lead_id}/commercial", headers=headers, json={"won_value": "50.00"})
        created = await client.post(f"/api/leads/{lead_id}/collections", headers=headers, json={
            "amount": "20.00", "collection_date": datetime.now(timezone.utc).isoformat(),
        })
        collection_id = created.json()["id"]
        null_amount = await client.patch(f"/api/collections/{collection_id}", headers=headers, json={"amount": None})
        null_date = await client.patch(f"/api/collections/{collection_id}", headers=headers, json={"collection_date": None})
        edited = await client.patch(f"/api/collections/{collection_id}", headers=headers, json={"amount": "25.00", "payment_reference": "edited"})
        deleted = await client.delete(f"/api/collections/{collection_id}", headers=headers)
        listed = await client.get(f"/api/leads/{lead_id}/collections", headers=headers)
    assert created.status_code == 201
    assert null_amount.status_code == null_date.status_code == 422
    assert edited.status_code == 200 and edited.json()["amount"] == "25.00"
    assert deleted.status_code == 204
    assert listed.json() == []


@pytest.mark.asyncio
async def test_target_validation_zero_target_and_period_progress(revenue_client, session_factory):
    lead_id = await make_lead(session_factory, status=SalesStatus.WON)
    headers = {"Authorization": "Bearer revenue-test-token"}
    today = datetime.now(timezone.utc).date()
    start, end = today.replace(month=1, day=1), today.replace(month=12, day=31)
    async with revenue_client as client:
        await client.patch(f"/api/leads/{lead_id}/commercial", headers=headers, json={"won_value": "1000.00", "expected_collection": "1000.00"})
        await client.post(f"/api/leads/{lead_id}/collections", headers=headers, json={"amount": "350.00", "collection_date": datetime.now(timezone.utc).isoformat()})
        invalid = await client.post("/api/revenue/targets", headers=headers, json={"label": "Bad period", "target_amount": "100", "period_start": str(end), "period_end": str(start)})
        target = await client.post("/api/revenue/targets", headers=headers, json={"label": "Annual", "target_amount": "1000.00", "currency": "INR", "period_start": str(start), "period_end": str(end)})
        duplicate = await client.post("/api/revenue/targets", headers=headers, json={"label": "Second", "target_amount": "200", "period_start": str(start), "period_end": str(end)})
        summary = await client.get("/api/revenue/summary", headers=headers)
        await client.patch(f"/api/revenue/targets/{target.json()['id']}", headers=headers, json={"target_amount": "0.00"})
        zero_target = await client.get("/api/revenue/summary", headers=headers)
    assert invalid.status_code == 422
    assert target.status_code == 201 and duplicate.status_code == 409
    assert summary.json()["target_collected"] == "350.00"
    assert summary.json()["target_remaining"] == "650.00"
    assert summary.json()["achievement_percentage"] == "35.00"
    assert zero_target.json()["achievement_percentage"] is None
    assert zero_target.json()["target_remaining"] == "0.00"


@pytest.mark.asyncio
async def test_pipeline_uses_one_current_stage_value_and_currency_isolation(revenue_client, session_factory):
    ids = [
        await make_lead(session_factory, status=SalesStatus.QUALIFIED),
        await make_lead(session_factory, status=SalesStatus.PROPOSAL, company="Proposal Lead"),
        await make_lead(session_factory, status=SalesStatus.NEGOTIATION, company="Negotiation Lead"),
        await make_lead(session_factory, status=SalesStatus.WON, company="Won Lead"),
        await make_lead(session_factory, status=SalesStatus.LOST, company="Lost Lead"),
        await make_lead(session_factory, status=SalesStatus.QUALIFIED, currency="USD", company="USD Lead"),
    ]
    headers = {"Authorization": "Bearer revenue-test-token"}
    async with revenue_client as client:
        for lead_id in ids:
            await client.patch(f"/api/leads/{lead_id}/commercial", headers=headers, json={"estimated_value": "100.00", "proposal_value": "80.00", "negotiation_value": "70.00", "won_value": "60.00"})
        pipeline = await client.get("/api/revenue/pipeline", headers=headers)
        summary = await client.get("/api/revenue/summary", headers=headers)
        usd = await client.get("/api/revenue/summary?currency=USD", headers=headers)
        sources = await client.get("/api/revenue/sources", headers=headers)
    stage = {row["sales_status"]: row for row in pipeline.json()}
    assert stage["QUALIFIED"]["current_opportunity_value"] == "100.00"
    assert stage["PROPOSAL"]["current_opportunity_value"] == "80.00"
    assert stage["NEGOTIATION"]["current_opportunity_value"] == "70.00"
    assert stage["WON"]["won_value"] == "60.00"
    assert stage["LOST"]["current_opportunity_value"] == "0.00"
    assert summary.json()["pipeline_value"] == "250.00"
    assert summary.json()["won_value"] == "60.00"
    assert usd.json()["pipeline_value"] == "100.00"
    assert sources.json()["available"] is False


@pytest.mark.asyncio
async def test_conversion_uses_structured_history_and_not_current_status(revenue_client, session_factory):
    lead_id = await make_lead(session_factory)
    headers = {"Authorization": "Bearer revenue-test-token"}
    async with revenue_client as client:
        no_history_before = await client.get("/api/revenue/conversion", headers=headers)
        qualified = await client.patch(f"/api/leads/{lead_id}/sales", headers=headers, json={"sales_status": "QUALIFIED"})
        contacted = await client.patch(f"/api/leads/{lead_id}/sales", headers=headers, json={"sales_status": "CONTACTED"})
        analytics = await client.get("/api/revenue/conversion", headers=headers)
    assert no_history_before.json()["tracked_lead_count"] == 0
    assert qualified.status_code == contacted.status_code == 200
    assert qualified.json()["recent_activities"][-1]["old_sales_status"] == "NEW"
    assert qualified.json()["recent_activities"][-1]["new_sales_status"] == "QUALIFIED"
    body = analytics.json()
    assert body["tracked_lead_count"] == 1
    assert body["steps"][0]["source_count"] == 1 and body["steps"][0]["converted_count"] == 1
    assert body["steps"][1]["source_count"] == 1 and body["steps"][1]["converted_count"] == 1
    assert body["history_limitation"].startswith("Funnel transitions")


@pytest.mark.asyncio
async def test_activity_transition_history_is_structured_on_each_change(revenue_client, session_factory):
    lead_id = await make_lead(session_factory)
    headers = {"Authorization": "Bearer revenue-test-token"}
    async with revenue_client as client:
        await client.patch(f"/api/leads/{lead_id}/sales", headers=headers, json={"sales_status": "QUALIFIED"})
        response = await client.get(f"/api/leads/{lead_id}/activities", headers=headers)
    status_change = response.json()[0]
    assert status_change["old_sales_status"] == "NEW"
    assert status_change["new_sales_status"] == "QUALIFIED"
    assert status_change["outcome"] is None


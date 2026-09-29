"""Deterministic SEO-to-sales opportunity and explicit-task integration tests."""
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import (
    Lead, SalesActivity, SalesTask, SeoAuditRun, SeoFinding, ServiceCatalogEntry,
)
from app.schemas.seo_opportunities import SeoSalesOpportunity


@pytest.fixture
def seo_sales_client(session_factory, monkeypatch):
    async def override_db():
        async with session_factory() as session:
            yield session
    monkeypatch.setattr(settings, "api_auth_token", "seo-sales-test-token")
    app.dependency_overrides[get_db] = override_db
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    app.dependency_overrides.pop(get_db, None)


def headers():
    return {"Authorization": "Bearer seo-sales-test-token"}


async def seed_lead(session_factory, name="SEO Opportunity Lead"):
    async with session_factory() as db:
        lead = Lead(company_name=name, sales_status="PROPOSAL", priority="HIGH", assigned_to="Rep SEO", proposal_value="1250.00", currency="USD")
        db.add(lead)
        for service_id, active in (("seo", True), ("website_development", True), ("website_cro", False)):
            db.add(ServiceCatalogEntry(service_id=service_id, name=service_id, description="existing service", active=active, version="1"))
        await db.commit()
        return lead.id


async def add_run(session_factory, lead_id, *, status="COMPLETED", created_at=None, audit_version="seo-audit-v1", findings=()):
    async with session_factory() as db:
        run = SeoAuditRun(
            lead_id=lead_id, status=status, audit_version=audit_version,
            root_url="https://example.test/", normalized_root_url="https://example.test/",
            metadata_json={}, created_at=created_at or datetime.now(timezone.utc),
        )
        db.add(run)
        await db.flush()
        rows = []
        for code, severity, page_url, evidence in findings:
            row = SeoFinding(
                seo_run_id=run.id, finding_code=code, category="ON_PAGE", severity=severity,
                title=code.replace("_", " ").title(), description="Persisted deterministic finding.",
                page_url=page_url, evidence=evidence,
            )
            db.add(row)
            rows.append(row)
        await db.commit()
        return run.id, [item.id for item in rows]


@pytest.mark.asyncio
async def test_no_completed_run_and_authentication_return_empty_or_reject(seo_sales_client, session_factory):
    lead_id = await seed_lead(session_factory)
    await add_run(session_factory, lead_id, status="RUNNING")
    async with seo_sales_client as client:
        unauthorized = await client.get(f"/api/leads/{lead_id}/seo/opportunities")
        response = await client.get(f"/api/leads/{lead_id}/seo/opportunities", headers=headers())
    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json() == {"items": []}


@pytest.mark.asyncio
async def test_latest_completed_run_grouping_evidence_service_mapping_and_stable_order(seo_sales_client, session_factory):
    lead_id = await seed_lead(session_factory)
    old_time = datetime.now(timezone.utc) - timedelta(days=3)
    run_id, finding_ids = await add_run(session_factory, lead_id, created_at=old_time, findings=(
        ("META_DESCRIPTION_MISSING", "MEDIUM", "https://example.test/", {"observed": None}),
        ("META_DESCRIPTION_MISSING", "LOW", "https://example.test/about", {"observed": None}),
        ("META_DESCRIPTION_MISSING", "MEDIUM", "https://example.test/", {"observed": None}),
        ("PAGE_NOT_HTTPS", "HIGH", "http://example.test/", {"scheme": "http"}),
    ))
    await add_run(session_factory, lead_id, status="RUNNING", created_at=old_time + timedelta(days=1), findings=(("IMAGE_ALT_MISSING", "HIGH", "https://example.test/image", {}),))
    await add_run(session_factory, lead_id, status="FAILED", created_at=old_time + timedelta(days=2), findings=(("BROKEN_INTERNAL_LINK", "HIGH", "https://example.test/old", {}),))
    async with seo_sales_client as client:
        first = await client.get(f"/api/leads/{lead_id}/seo/opportunities", headers=headers())
        second = await client.get(f"/api/leads/{lead_id}/seo/opportunities", headers=headers())
    assert first.status_code == second.status_code == 200
    items = first.json()["items"]
    assert items == second.json()["items"]
    assert len(items) == 2  # grouped types, not one item per finding
    assert [item["type"] for item in items] == ["TECHNICAL_SEO", "ON_PAGE_SEO"]
    on_page = items[1]
    assert on_page["seo_run_id"] == str(run_id)
    assert on_page["finding_count"] == 3
    assert on_page["affected_page_count"] == 2
    assert {item["finding_id"] for item in on_page["evidence"]} <= set(map(str, finding_ids))
    assert on_page["severity"] == "MEDIUM"
    assert on_page["recommended_service_ids"] == ["seo"]
    assert items[0]["recommended_service_ids"] == ["website_development"]
    assert all(item["current_task_state"] == "NO_TASK" for item in items)
    assert all(SeoSalesOpportunity.model_validate(item) for item in items)


@pytest.mark.asyncio
async def test_read_is_read_only_and_summary_additive_with_null_commercial_context(seo_sales_client, session_factory):
    lead_id = await seed_lead(session_factory, "Null Context")
    async with session_factory() as db:
        lead = await db.get(Lead, lead_id)
        lead.sales_status = "NEW"
        lead.proposal_value = None
        lead.estimated_value = None
        await db.commit()
    await add_run(session_factory, lead_id, findings=(("IMAGE_ALT_EMPTY", "INFO", "https://example.test/", {"empty_alt": 1}),))
    async with seo_sales_client as client:
        summary = await client.get("/api/tasks/summary", headers=headers())
        opportunities = await client.get(f"/api/leads/{lead_id}/seo/opportunities", headers=headers())
    assert summary.status_code == opportunities.status_code == 200
    assert set(summary.json()) == {"today", "overdue", "upcoming", "outcome_suggestions", "seo_opportunities"}
    item = opportunities.json()["items"][0]
    assert item["current_stage_value"] is None
    assert item["current_stage_value_field"] == "estimated_value"
    assert item["currency"] == "USD"
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(SalesTask)) == 0
        assert await db.scalar(select(func.count()).select_from(SalesActivity)) == 0


@pytest.mark.asyncio
async def test_explicit_task_creation_links_and_handles_opportunity(seo_sales_client, session_factory):
    lead_id = await seed_lead(session_factory)
    run_id, _ = await add_run(session_factory, lead_id, findings=(("BROKEN_INTERNAL_LINK", "HIGH", "https://example.test/", {"status": 404}),))
    async with seo_sales_client as client:
        before = await client.get(f"/api/leads/{lead_id}/seo/opportunities", headers=headers())
        opportunity = before.json()["items"][0]
        created = await client.post(f"/api/leads/{lead_id}/tasks", headers=headers(), json={
            "task_type": opportunity["suggested_task_type"],
            "title": "Review internal link findings",
            "description": "Human-reviewed action.",
            "due_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "priority": "HIGH",
            "seo_opportunity_id": opportunity["opportunity_id"],
        })
        after_create = await client.get(f"/api/leads/{lead_id}/seo/opportunities", headers=headers())
        duplicate = await client.post(f"/api/leads/{lead_id}/tasks", headers=headers(), json={
            "task_type": "OTHER", "title": "Duplicate", "due_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "seo_opportunity_id": opportunity["opportunity_id"],
        })
        completed = await client.post(f"/api/tasks/{created.json()['id']}/complete", headers=headers())
        after_complete = await client.get(f"/api/leads/{lead_id}/seo/opportunities", headers=headers())
    assert created.status_code == 201
    assert created.json()["related_activity_id"] is not None
    assert after_create.json()["items"][0]["seo_run_id"] == str(run_id)
    assert after_create.json()["items"][0]["current_task_state"] == "OPEN"
    assert after_create.json()["items"][0]["related_task_id"] == created.json()["id"]
    assert duplicate.status_code == 409
    async with session_factory() as db:
        note = await db.get(SalesActivity, UUID(created.json()["related_activity_id"]))
        assert opportunity["opportunity_id"] in note.note

    assert completed.status_code == 200
    assert after_complete.json()["items"][0]["current_task_state"] == "COMPLETED"


@pytest.mark.asyncio
async def test_new_completed_run_replaces_old_opportunity_and_old_task_id_is_stale(seo_sales_client, session_factory):
    lead_id = await seed_lead(session_factory)
    old_run, _ = await add_run(session_factory, lead_id, created_at=datetime.now(timezone.utc) - timedelta(days=1), findings=(("TITLE_MISSING", "MEDIUM", "https://example.test/", {}),))
    async with seo_sales_client as client:
        old = (await client.get(f"/api/leads/{lead_id}/seo/opportunities", headers=headers())).json()["items"][0]
        await add_run(session_factory, lead_id, findings=(("IMAGE_ALT_MISSING", "LOW", "https://example.test/", {}),))
        current = await client.get(f"/api/leads/{lead_id}/seo/opportunities", headers=headers())
        stale_task = await client.post(f"/api/leads/{lead_id}/tasks", headers=headers(), json={
            "task_type": "OTHER", "title": "Old opportunity", "due_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "seo_opportunity_id": old["opportunity_id"],
        })
    assert old["seo_run_id"] == str(old_run)
    assert len(current.json()["items"]) == 1
    assert current.json()["items"][0]["type"] == "IMAGES"
    assert current.json()["items"][0]["seo_run_id"] != old["seo_run_id"]
    assert stale_task.status_code == 409

"""SQLite-backed persistence checks for enrichment runs and evidence history."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.database import SCHEMA_REVISION
from app.models import Base, EnrichmentRun, Evidence, Lead


NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def make_evidence(lead_id, run_id, value, *, confidence=0.9, fetched_at=NOW):
    return Evidence(
        lead_id=lead_id,
        enrichment_run_id=run_id,
        evidence_type="website.title",
        source_type="website",
        provider_name="website-analyzer",
        provider_kind="website_analyzer",
        provider_implementation="v1",
        source_url="https://example.test/",
        external_provider_reference="page-home",
        observed_value={"title": value},
        observed_at=NOW,
        fetched_at=fetched_at,
        expires_at=None,
        confidence=confidence,
        content_hash="a" * 64,
        metadata_json={"http_status": 200},
    )


@pytest.mark.asyncio
async def test_create_enrichment_run_with_optional_processing_job(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Run lifecycle")
        session.add(lead)
        await session.flush()
        run = EnrichmentRun(
            lead_id=lead.id,
            status="running",
            requested_stages=["website", "places"],
            completed_stages=["website"],
            started_at=NOW,
            pipeline_version="phase2-v1",
            failure_summary=None,
        )
        session.add(run)
        await session.commit()

        assert run.id is not None
        assert run.lead_id == lead.id
        assert run.processing_job_id is None
        assert run.created_at is not None and run.updated_at is not None


@pytest.mark.asyncio
async def test_evidence_links_to_lead_and_run_with_provider_provenance(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Evidence owner")
        session.add(lead)
        await session.flush()
        run = EnrichmentRun(lead_id=lead.id, status="completed", requested_stages=["website"], completed_stages=["website"], pipeline_version="v1")
        session.add(run)
        await session.flush()
        item = make_evidence(lead.id, run.id, "First title")
        session.add(item)
        await session.commit()

        stored = await session.scalar(select(Evidence).where(Evidence.id == item.id))
        assert stored.lead_id == lead.id and stored.enrichment_run_id == run.id
        assert stored.source_type == "website"
        assert stored.provider_name == "website-analyzer"
        assert stored.provider_kind == "website_analyzer"
        assert stored.source_url == "https://example.test/"
        assert stored.external_provider_reference == "page-home"
        assert stored.observed_value == {"title": "First title"}
        assert stored.metadata_json == {"http_status": 200}


@pytest.mark.asyncio
async def test_multiple_observations_across_runs_preserve_evidence_history(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Historical evidence")
        session.add(lead)
        await session.flush()
        first_run = EnrichmentRun(lead_id=lead.id, status="completed", requested_stages=["website"], completed_stages=["website"], pipeline_version="v1")
        second_run = EnrichmentRun(lead_id=lead.id, status="completed", requested_stages=["website"], completed_stages=["website"], pipeline_version="v1")
        session.add_all([first_run, second_run])
        await session.flush()
        first = make_evidence(lead.id, first_run.id, "Old title")
        second = make_evidence(lead.id, second_run.id, "New title", fetched_at=NOW.replace(day=27))
        session.add_all([first, second])
        await session.commit()

        rows = (await session.scalars(select(Evidence).where(Evidence.lead_id == lead.id).order_by(Evidence.fetched_at))).all()
        assert len(rows) == 2
        assert [row.observed_value["title"] for row in rows] == ["Old title", "New title"]
        assert rows[0].id != rows[1].id
        assert rows[0].enrichment_run_id != rows[1].enrichment_run_id


@pytest.mark.asyncio
async def test_confidence_check_rejects_values_outside_zero_to_one(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Confidence constraint")
        session.add(lead)
        await session.flush()
        run = EnrichmentRun(lead_id=lead.id, status="completed", requested_stages=["website"], completed_stages=["website"], pipeline_version="v1")
        session.add(run)
        await session.flush()
        session.add(make_evidence(lead.id, run.id, "Invalid", confidence=1.5))
        with pytest.raises(IntegrityError, match="ck_evidence_confidence_range"):
            await session.commit()


@pytest.mark.asyncio
async def test_lead_run_and_evidence_relationships_load(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Relationship graph")
        session.add(lead)
        await session.flush()
        run = EnrichmentRun(lead_id=lead.id, status="completed", requested_stages=["website"], completed_stages=["website"], pipeline_version="v1")
        session.add(run)
        await session.flush()
        item = make_evidence(lead.id, run.id, "Relationship title")
        session.add(item)
        await session.commit()
        lead_id = lead.id

        loaded = await session.scalar(
            select(Lead)
            .where(Lead.id == lead_id)
            .options(selectinload(Lead.enrichment_runs).selectinload(EnrichmentRun.evidence), selectinload(Lead.evidence))
        )
        assert len(loaded.enrichment_runs) == 1
        assert len(loaded.evidence) == 1
        assert loaded.enrichment_runs[0].evidence[0].id == loaded.evidence[0].id
        assert loaded.enrichment_runs[0].lead_id == loaded.id


def test_enrichment_migration_is_the_single_head_after_worker_heartbeat():
    migrations = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    expected = [
        ("0001_initial", None),
        ("0002_active_job_guard", "0001_initial"),
        ("0003_worker_heartbeat", "0002_active_job_guard"),
        ("0004_enrichment_evidence", "0003_worker_heartbeat"),
        ("0005_provider_execution_controls", "0004_enrichment_evidence"),
        ("0006_resumable_enrichment_stages", "0005_provider_execution_controls"),
        ("0007_deterministic_intelligence", "0006_resumable_enrichment_stages"),
        ("0008_personalization", "0007_deterministic_intelligence"),
        ("0009_sales_workflow", "0008_personalization"),
        ("0010_outreach_preparation", "0009_sales_workflow"),
        ("0011_sales_tasks", "0010_outreach_preparation"),
        ("0012_revenue_conversion", "0011_sales_tasks"),
        ("0013_outreach_operations", "0012_revenue_conversion"),
    ]
    actual = []
    for revision, _ in expected:
        file = next(migrations.glob(f"{revision}*.py"))
        spec = importlib.util.spec_from_file_location(revision, file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        actual.append((module.revision, module.down_revision))

    assert actual == expected
    assert SCHEMA_REVISION == "0014_outreach_outcomes"

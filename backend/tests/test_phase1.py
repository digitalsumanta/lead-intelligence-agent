"""Stabilization tests using SQLite only for unit-level behavior."""
import pytest
from sqlalchemy import select
from app.models import Lead, ProcessingJob, JobStatus, LeadStatus, LeadEvent, AgentRun, WorkerHeartbeat
from app.services.jobs import create_job, claim_jobs, recover_stale_jobs, run_job, validate_checkpoint
from app.providers import get_provider, validate_provider_model
from app.services.website import UnsafeWebsiteUrl, WebsiteAnalyzer, validate_url_syntax

@pytest.mark.asyncio
async def test_duplicate_active_job_is_rejected(session_factory):
    async with session_factory() as db:
        lead = Lead(company_name="Same lead"); db.add(lead); await db.commit()
        await create_job(db, lead)
        with pytest.raises(ValueError, match="active processing job"):
            await create_job(db, lead)

@pytest.mark.asyncio
async def test_claim_jobs_claims_due_queue_once(session_factory):
    async with session_factory() as db:
        lead = Lead(company_name="Claim me"); db.add(lead); await db.commit()
        job = await create_job(db, lead)
        claimed = await claim_jobs(db, 5)
        assert claimed == [job.id]
        await db.refresh(job); assert job.attempt_count == 1
        assert await claim_jobs(db, 5) == []

@pytest.mark.asyncio
async def test_stale_job_at_attempt_limit_becomes_terminal(session_factory):
    from datetime import timedelta
    from app.models.entities import now_utc
    async with session_factory() as db:
        lead = Lead(company_name="Stale"); db.add(lead); await db.commit()
        job = ProcessingJob(lead_id=lead.id, status=JobStatus.PROCESSING, attempt_count=4, max_attempts=4, started_at=now_utc()-timedelta(minutes=20)); db.add(job); await db.flush()
        run = AgentRun(job_id=job.id); db.add(run); await db.commit()
        await recover_stale_jobs(db, now_utc()-timedelta(minutes=10))
        await db.refresh(job); await db.refresh(lead)
        assert job.status == JobStatus.FAILED and job.attempt_count == 4
        assert lead.status == LeadStatus.FAILED
        await db.refresh(run); assert run.status == "ABANDONED"
        assert (await db.scalar(select(LeadEvent).where(LeadEvent.event_type == "STALE_JOB_RECOVERED"))) is not None

@pytest.mark.asyncio
async def test_normal_failure_at_attempt_limit_is_not_requeued(session_factory):
    class BrokenProvider:
        async def analyze_lead(self, lead, website): raise OSError("provider temporarily failed")
    class Analyzer:
        async def analyze(self, url): return {"available": False, "issues": []}
    async with session_factory() as db:
        lead = Lead(company_name="Retry cap"); db.add(lead); await db.commit()
        job = ProcessingJob(lead_id=lead.id, attempt_count=3, max_attempts=4, checkpoint="WEBSITE_ANALYZED"); db.add(job); await db.commit()
        await run_job(db, job, analyzer=Analyzer(), provider_instance=BrokenProvider())
        assert job.attempt_count == 4 and job.status == JobStatus.FAILED and job.next_retry_at is None

@pytest.mark.asyncio
async def test_unknown_checkpoint_fails_as_controlled_job_error(session_factory):
    async with session_factory() as db:
        lead = Lead(company_name="Bad checkpoint"); db.add(lead); await db.commit()
        job = ProcessingJob(lead_id=lead.id, checkpoint="NOT_A_REAL_STEP"); db.add(job); await db.commit()
        result = await run_job(db, job)
        assert result.status == JobStatus.FAILED and "Unknown processing checkpoint" in result.last_error
    with pytest.raises(ValueError, match="Unknown processing checkpoint"):
        validate_checkpoint("NOPE")

@pytest.mark.asyncio
async def test_completed_checkpoint_is_idempotent(session_factory):
    class NeverRun:
        async def analyze(self, url): raise AssertionError("completed checkpoint must not repeat website work")
    async with session_factory() as db:
        lead = Lead(company_name="Already done", ai_analysis={"company_summary": "saved"}); db.add(lead); await db.commit()
        job = ProcessingJob(lead_id=lead.id, checkpoint="COMPLETED", status=JobStatus.QUEUED); db.add(job); await db.commit()
        result = await run_job(db, job, analyzer=NeverRun())
        assert result.status == JobStatus.COMPLETED and result.checkpoint == "COMPLETED"

def test_provider_model_is_preserved_and_cloud_is_not_selectable():
    assert validate_provider_model("ollama", "custom-qwen:7b") == ("ollama", "custom-qwen:7b")
    assert get_provider("ollama", "custom-qwen:7b").model == "custom-qwen:7b"
    with pytest.raises(ValueError, match="not configured"):
        validate_provider_model("gemini", "model")

@pytest.mark.asyncio
async def test_job_persists_requested_provider_and_model(session_factory):
    async with session_factory() as db:
        lead = Lead(company_name="Model propagation"); db.add(lead); await db.commit()
        job = await create_job(db, lead, provider="ollama", model="custom-model:latest")
        assert job.provider == "ollama" and job.model == "custom-model:latest"

@pytest.mark.parametrize("url", ["http://localhost", "http://127.1.2.3", "http://10.0.0.1", "http://169.254.169.254/latest/meta-data", "http://[::1]", "http://[fc00::1]", "http://router.local"])
def test_website_rejects_local_private_and_metadata_hosts(url):
    with pytest.raises(UnsafeWebsiteUrl): validate_url_syntax(url)

def test_website_allows_public_https_url():
    assert validate_url_syntax("https://example.com/path") == "example.com"

def test_website_rejects_nonstandard_service_ports():
    with pytest.raises(UnsafeWebsiteUrl, match="standard HTTP"):
        validate_url_syntax("http://example.com:8080")

@pytest.mark.asyncio
async def test_redirect_to_private_address_is_rejected(monkeypatch):
    import app.services.website as module
    async def resolve(host, port): return ["93.184.216.34"]
    monkeypatch.setattr(module, "resolve_public_host", resolve)
    class Response:
        status_code = 302
        headers = {"location": "http://127.0.0.1/admin"}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        def stream(self, *args, **kwargs): return Response()
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: Client())
    with pytest.raises(UnsafeWebsiteUrl): await WebsiteAnalyzer()._fetch("https://example.com")

@pytest.mark.asyncio
async def test_api_auth_protects_data_routes_but_not_health(monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    from app.config import settings
    monkeypatch.setattr(settings, "api_auth_token", "unit-secret")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/api/not-a-route")).status_code == 401
        response = await client.get("/api/not-a-route", headers={"Authorization": "Bearer unit-secret"})
        assert response.status_code == 404

@pytest.mark.asyncio
async def test_single_lead_api_creates_job_without_processing(session_factory, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    from app.config import settings
    from app.database import get_db
    async with session_factory() as db:
        lead = Lead(company_name="Queued by API"); db.add(lead); await db.commit(); lead_id = lead.id
    async def override_db():
        async with session_factory() as db:
            yield db
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(settings, "api_auth_token", "unit-secret")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/leads/{lead_id}/process", headers={"Authorization": "Bearer unit-secret"})
        assert response.status_code == 200 and response.json()["status"] == "QUEUED"
        async with session_factory() as db:
            job = await db.scalar(select(ProcessingJob).where(ProcessingJob.lead_id == lead_id))
            assert job.status == JobStatus.QUEUED
    finally:
        app.dependency_overrides.pop(get_db, None)

@pytest.mark.asyncio
async def test_readiness_reports_database_failure(monkeypatch):
    from app.main import readiness
    async def fail(): raise RuntimeError("not migrated")
    monkeypatch.setattr("app.main.verify_database_schema", fail)
    response = await readiness()
    assert response.status_code == 503

@pytest.mark.asyncio
async def test_readiness_reports_database_and_provider_success(monkeypatch):
    import asyncio
    from app.main import readiness
    class Provider:
        name = "ollama"
        async def health_check(self): return {"provider": "ollama", "healthy": True, "model_available": True}
    async def ok(): return None
    monkeypatch.setattr("app.main.verify_database_schema", ok)
    monkeypatch.setattr("app.main.get_provider", lambda: Provider())
    monkeypatch.setattr("app.main.worker_is_available", lambda: asyncio.sleep(0, result=True))
    result = await readiness()
    assert result["ready"] is True and result["checks"]["database"] == "healthy"

@pytest.mark.asyncio
async def test_readiness_reports_missing_worker(monkeypatch):
    import asyncio
    from app.main import readiness
    class Provider:
        async def health_check(self): return {"provider": "ollama", "healthy": True}
    async def ok(): return None
    monkeypatch.setattr("app.main.verify_database_schema", ok)
    monkeypatch.setattr("app.main.get_provider", lambda: Provider())
    monkeypatch.setattr("app.main.worker_is_available", lambda: asyncio.sleep(0, result=False))
    response = await readiness()
    assert response.status_code == 503 and response.body.find(b'"worker":"unavailable"') >= 0

def test_initial_migration_is_explicit_not_metadata_driven():
    from pathlib import Path
    migration = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0001_initial.py"
    source = migration.read_text(encoding="utf-8")
    assert "op.create_table(\"leads\"" in source
    assert "Base.metadata.create_all" not in source
    assert "ForeignKey" in source and "timezone=True" in source

@pytest.mark.asyncio
async def test_database_schema_verification_requires_alembic_head(session_factory, monkeypatch):
    from sqlalchemy import text
    from app.database import verify_database_schema
    engine = session_factory.kw["bind"]
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        await connection.execute(text("INSERT INTO alembic_version(version_num) VALUES ('0001_initial')"))
    monkeypatch.setattr("app.database.engine", engine)
    with pytest.raises(RuntimeError, match="not at the required revision"):
        await verify_database_schema()
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE alembic_version SET version_num='0016_ai_sales_intelligence'"))
    await verify_database_schema()

@pytest.mark.asyncio
async def test_worker_heartbeat_drives_availability(session_factory, monkeypatch):
    from datetime import timedelta
    from app.database import update_worker_heartbeat, worker_is_available
    from app.models.entities import now_utc
    monkeypatch.setattr("app.database.SessionLocal", session_factory)
    await update_worker_heartbeat("unit-worker")
    assert await worker_is_available()
    async with session_factory() as db:
        row = await db.get(WorkerHeartbeat, "unit-worker")
        row.last_seen = now_utc() - timedelta(minutes=1)
        await db.commit()
    assert not await worker_is_available()

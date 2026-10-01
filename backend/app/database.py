"""Async database setup."""
from sqlalchemy import inspect, text
from sqlalchemy import select, delete
from datetime import timedelta
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.config import settings
from app.models import WorkerHeartbeat
from app.models.entities import now_utc

SCHEMA_REVISION = "0025_email_provider_status"

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db():
    """Yield one transactional request session."""
    async with SessionLocal() as session:
        yield session


async def verify_database_schema() -> None:
    """Fail startup if the database is unavailable or has not been migrated."""
    required = {"leads", "lead_import_jobs", "lead_batches", "lead_processing_jobs", "lead_events", "provider_usage", "api_keys", "provider_operation_state", "agent_runs", "worker_heartbeats", "enrichment_runs", "evidence", "service_catalog", "opportunities", "service_recommendations", "lead_scores", "personalizations", "personalization_evidence_links", "opportunity_evidence_links", "recommendation_evidence_links", "recommendation_opportunity_links", "score_evidence_links", "sales_activities", "outreach_artifacts", "outreach_executions", "outreach_provider_events", "sales_tasks", "sales_collections", "revenue_targets", "seo_audit_runs", "seo_observations", "seo_findings", "ai_sales_intelligence", "ai_sales_copilot", "ai_sales_action_plans", "ai_proposal_intelligence", "agent_policy_evaluations", "ai_sales_supervisor_runs", "autonomous_task_executions", "ai_outreach_preparations", "ai_outreach_approvals", "ai_outreach_executions", "alembic_version"}
    async with engine.connect() as connection:
        tables = await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))
        missing = sorted(required - tables)
        if missing:
            raise RuntimeError("Database schema is not migrated; missing tables: " + ", ".join(missing))
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    if revision != SCHEMA_REVISION:
        raise RuntimeError(f"Database migrations are not at the required revision {SCHEMA_REVISION}")

async def update_worker_heartbeat(worker_id: str) -> None:
    now = now_utc()
    async with SessionLocal() as session:
        heartbeat = await session.get(WorkerHeartbeat, worker_id)
        if heartbeat is None: session.add(WorkerHeartbeat(worker_id=worker_id, last_seen=now))
        else: heartbeat.last_seen = now
        await session.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.last_seen < now - timedelta(days=7), WorkerHeartbeat.worker_id != worker_id))
        await session.commit()

async def worker_is_available(max_age_seconds: int = 30) -> bool:
    async with SessionLocal() as session:
        count = await session.scalar(select(WorkerHeartbeat.worker_id).where(WorkerHeartbeat.last_seen >= now_utc() - timedelta(seconds=max_age_seconds)).limit(1))
        return count is not None

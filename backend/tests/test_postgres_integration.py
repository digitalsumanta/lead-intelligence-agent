"""Opt-in checks against an already migrated, real PostgreSQL database."""
import os
import asyncio
import pytest
from sqlalchemy import text, delete, func, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.models import ApiKey, AiSalesActionPlan, AiSalesSupervisorRun, AgentPolicyEvaluation, AutonomousTaskExecution, Lead, ProcessingJob, JobStatus, SalesTask
from app.services.key_pool import ApiKeyPool
from app.services.jobs import claim_jobs

pytestmark = pytest.mark.postgres_integration

@pytest.mark.asyncio
async def test_postgresql_schema_is_migrated_to_head():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL to an explicitly provisioned PostgreSQL database")
    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_POSTGRES_URL must use the asyncpg PostgreSQL dialect")
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert version == "0024_ai_outreach_executions"
            tables = set(await connection.run_sync(lambda conn: __import__("sqlalchemy").inspect(conn).get_table_names()))
            assert {"leads", "lead_processing_jobs", "lead_batches", "lead_events", "enrichment_runs", "evidence", "provider_operation_state", "personalizations", "personalization_evidence_links", "sales_activities", "outreach_artifacts", "outreach_executions", "outreach_provider_events", "sales_tasks", "sales_collections", "revenue_targets", "seo_audit_runs", "seo_observations", "seo_findings", "ai_sales_intelligence", "ai_sales_copilot", "ai_sales_action_plans", "ai_proposal_intelligence", "agent_policy_evaluations", "ai_sales_supervisor_runs", "autonomous_task_executions", "ai_outreach_preparations", "ai_outreach_approvals", "ai_outreach_executions"}.issubset(tables)
            task_columns = await connection.run_sync(lambda conn: {item["name"] for item in __import__("sqlalchemy").inspect(conn).get_columns("sales_tasks")})
            assert {"created_by_agent", "agent_source_type", "agent_source_id", "agent_source_action_id", "agent_evidence_refs"}.issubset(task_columns)
            lead_columns = await connection.run_sync(lambda conn: {item["name"] for item in __import__("sqlalchemy").inspect(conn).get_columns("leads")})
            assert {"email_opted_out", "whatsapp_opted_out", "phone_opted_out"}.issubset(lead_columns)
            lead_indexes = await connection.run_sync(lambda conn: __import__("sqlalchemy").inspect(conn).get_indexes("leads"))
            assert {"ix_leads_sales_status", "ix_leads_priority", "ix_leads_next_follow_up_at"}.issubset({index["name"] for index in lead_indexes})
            sales_indexes = await connection.run_sync(lambda conn: __import__("sqlalchemy").inspect(conn).get_indexes("sales_activities"))
            assert "ix_sales_activities_lead_occurred" in {index["name"] for index in sales_indexes}
            indexes = await connection.run_sync(lambda conn: __import__("sqlalchemy").inspect(conn).get_indexes("evidence"))
            assert {"ix_evidence_lead", "ix_evidence_enrichment_run", "ix_evidence_type", "ix_evidence_provider_source", "ix_evidence_observed_at", "ix_evidence_fetched_at", "ix_evidence_provider_reference"}.issubset({index["name"] for index in indexes})
            api_key_columns = await connection.run_sync(lambda conn: {column["name"] for column in __import__("sqlalchemy").inspect(conn).get_columns("api_keys")})
            usage_columns = await connection.run_sync(lambda conn: {column["name"] for column in __import__("sqlalchemy").inspect(conn).get_columns("provider_usage")})
            assert {"enabled", "operation_scope", "lease_token", "lease_expires_at", "quota_exhausted_until"}.issubset(api_key_columns)
            assert {"operation", "key_fingerprint", "outcome", "retry_count", "error_category", "rate_limit_reset_at"}.issubset(usage_columns)
            inspector = __import__("sqlalchemy").inspect
            personalization_indexes = await connection.run_sync(lambda conn: {item["name"] for item in inspector(conn).get_indexes("personalizations")})
            link_indexes = await connection.run_sync(lambda conn: {item["name"] for item in inspector(conn).get_indexes("personalization_evidence_links")})
            run_indexes = await connection.run_sync(lambda conn: {item["name"] for item in inspector(conn).get_indexes("enrichment_runs")})
            assert "uq_personalizations_run_contract" in personalization_indexes
            assert "ix_personalization_evidence_evidence" in link_indexes
            assert "uq_enrichment_runs_id_lead" in run_indexes
            personalization_fks = await connection.run_sync(lambda conn: inspector(conn).get_foreign_keys("personalizations"))
            assert any(item["constrained_columns"] == ["enrichment_run_id", "lead_id"]
                       and item["referred_table"] == "enrichment_runs"
                       and item["referred_columns"] == ["id", "lead_id"] for item in personalization_fks)
            assert any(item["constrained_columns"] == ["lead_id"] and item["referred_table"] == "leads"
                       for item in personalization_fks)
            link_fks = await connection.run_sync(lambda conn: inspector(conn).get_foreign_keys("personalization_evidence_links"))
            assert {item["referred_table"] for item in link_fks} == {"personalizations", "evidence"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgresql_supervisor_run_persistence_and_history():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL to an explicitly provisioned PostgreSQL database")
    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_POSTGRES_URL must use the asyncpg PostgreSQL dialect")
    from types import SimpleNamespace
    from app.services.ai_sales_supervisor import run_supervisor, supervisor_history

    class EmptyProvider:
        name = "integration-test"
        model = "no-network"
        async def generate(self, _request):
            return SimpleNamespace(parsed_json={"findings": []}, usage=None, request_metadata=None)

    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_ids = []
    try:
        async with factory() as session:
            first = await run_supervisor(session, provider=EmptyProvider())
            second = await run_supervisor(session, provider=EmptyProvider())
            run_ids = [first.id, second.id]
            rows, _ = await supervisor_history(session, limit=50)
            assert first.status == second.status == "COMPLETED"
            assert first.id != second.id
            assert len([row for row in rows if row.id in set(run_ids)]) == 2
            assert first.scope == second.scope == "PIPELINE"
            assert first.lead_id is second.lead_id is None
    finally:
        if run_ids:
            async with factory() as session:
                await session.execute(delete(AiSalesSupervisorRun).where(AiSalesSupervisorRun.id.in_(run_ids)))
                await session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgresql_outreach_preparation_persists_audit_and_returns_idempotently():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL to an explicitly provisioned PostgreSQL database")
    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_POSTGRES_URL must use the asyncpg PostgreSQL dialect")
    from types import SimpleNamespace
    from app.domain.ai_outreach_preparation import OutreachPreparationRequest, PreparationChannel, PreparationSourceType
    from app.domain.ai_sales_action_planner import ActionPlanOutput, RecommendedAction, ActionType
    from app.models import AiOutreachPreparation, AiSalesActionPlan
    from app.services.ai_outreach_preparation import prepare_outreach
    from app.services.ai_sales_intelligence import AISalesContextBuilder
    from app.services.autonomous_task_agent import _plan_action_id

    class SelectionProvider:
        name = "integration-test"
        model = "no-network"
        calls = 0
        async def generate(self, _request):
            self.calls += 1
            return SimpleNamespace(parsed_json={
                "selected_evidence_ids": [evidence_id], "selected_opportunity_ids": [], "selected_service_ids": [],
                "approved_opening": "Hello,",
                "approved_call_to_action": "Would you be open to a brief conversation?",
                "discovery_questions": [],
            })

    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    lead_id = plan_id = None
    try:
        async with factory() as session:
            lead = Lead(company_name="PostgreSQL Preparation integration", email="prospect@example.test")
            session.add(lead)
            await session.flush()
            lead_id = lead.id
            context, fingerprint, _ = await AISalesContextBuilder().build(session, lead.id)
            evidence_id = next(item.id for item in context.commercial_data if item.source_type == "commercial_context")
            action = RecommendedAction(
                action_type=ActionType.REVIEW_COMMERCIAL_CONTEXT,
                priority=next(item.data["priority"] for item in context.commercial_data if item.source_type == "commercial_context"),
                title="Review commercial context", rationale="The cited records may warrant human review.",
                evidence_ids=(evidence_id,), opportunity_ids=(), service_ids=(),
                preparation_items=("Check the current sales and commercial context.",),
            )
            plan = AiSalesActionPlan(
                lead_id=lead.id, contract_version="ai-sales-action-planner-v1",
                prompt_version="ai-sales-action-planner-prompt-v1", context_version="ai-sales-context-v1",
                provider="integration-test", model="no-network", status="validated", source_fingerprint=fingerprint,
                action_plan=ActionPlanOutput(actions=(action,)).model_dump(mode="json"),
                evidence_catalog=[], evidence_refs=[evidence_id], usage_metadata={},
            )
            session.add(plan)
            await session.commit()
            plan_id = plan.id
        request = OutreachPreparationRequest(
            lead_id=lead_id, source_type=PreparationSourceType.AI_SALES_ACTION_PLAN,
            source_id=plan_id, source_action_id=_plan_action_id(action),
            action_type=ActionType.REVIEW_COMMERCIAL_CONTEXT, channel=PreparationChannel.EMAIL,
        )
        provider = SelectionProvider()
        async with factory() as session:
            first = await prepare_outreach(session, request, provider=provider)
            retry = await prepare_outreach(session, request, provider=provider)
            rows = (await session.scalars(select(AiOutreachPreparation).where(AiOutreachPreparation.lead_id == lead_id))).all()
            assert first.status == "READY_FOR_HUMAN_REVIEW" and first.body
            assert retry.id == first.id and provider.calls == 1
            assert len(rows) == 1 and rows[0].policy_evaluation_id is not None
    finally:
        if lead_id is not None:
            async with factory() as session:
                await session.execute(delete(AiOutreachPreparation).where(AiOutreachPreparation.lead_id == lead_id))
                await session.execute(delete(AgentPolicyEvaluation).where(AgentPolicyEvaluation.lead_id == lead_id))
                await session.execute(delete(AiSalesActionPlan).where(AiSalesActionPlan.lead_id == lead_id))
                await session.execute(delete(Lead).where(Lead.id == lead_id))
                await session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgresql_human_approved_email_execution_is_single_attempt():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL to an explicitly provisioned PostgreSQL database")
    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_POSTGRES_URL must use the asyncpg PostgreSQL dialect")
    from types import SimpleNamespace
    from app.domain.ai_outreach_preparation import OutreachPreparationRequest, PreparationChannel, PreparationSourceType
    from app.domain.ai_sales_action_planner import ActionPlanOutput, RecommendedAction, ActionType
    from app.models import AiOutreachApproval, AiOutreachExecution, AiOutreachPreparation
    from app.providers.outreach import OutreachSendResult
    from app.services.ai_outreach_execution import approve_preparation, execute_approved_email
    from app.services.ai_outreach_preparation import prepare_outreach
    from app.services.ai_sales_intelligence import AISalesContextBuilder
    from app.services.autonomous_task_agent import _plan_action_id

    class SelectionProvider:
        name = "integration-test"
        model = "no-network"
        async def generate(self, _request):
            return SimpleNamespace(parsed_json={
                "selected_evidence_ids": [evidence_id], "selected_opportunity_ids": [], "selected_service_ids": [],
                "approved_opening": "Hello,",
                "approved_call_to_action": "Would you be open to a brief conversation?",
                "discovery_questions": [],
            })

    class MockEmailProvider:
        name = "postgres-mock"
        def __init__(self):
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()
        async def send(self, _message):
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return OutreachSendResult(provider_message_id="postgres-mock-message")

    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    lead_id = plan_id = None
    try:
        async with factory() as session:
            lead = Lead(company_name="PostgreSQL 10E execution", email="execution@example.test")
            session.add(lead)
            await session.flush()
            lead_id = lead.id
            context, fingerprint, _ = await AISalesContextBuilder().build(session, lead.id)
            evidence_id = next(item.id for item in context.commercial_data if item.source_type == "commercial_context")
            action = RecommendedAction(
                action_type=ActionType.REVIEW_COMMERCIAL_CONTEXT, priority=lead.priority,
                title="Review commercial context", rationale="The cited records may warrant human review.",
                evidence_ids=(evidence_id,), opportunity_ids=(), service_ids=(),
                preparation_items=("Check the current sales and commercial context.",),
            )
            plan = AiSalesActionPlan(
                lead_id=lead.id, contract_version="ai-sales-action-planner-v1",
                prompt_version="ai-sales-action-planner-prompt-v1", context_version="ai-sales-context-v1",
                provider="integration-test", model="no-network", status="validated", source_fingerprint=fingerprint,
                action_plan=ActionPlanOutput(actions=(action,)).model_dump(mode="json"),
                evidence_catalog=[], evidence_refs=[evidence_id], usage_metadata={},
            )
            session.add(plan)
            await session.commit()
            plan_id = plan.id
        request = OutreachPreparationRequest(
            lead_id=lead_id, source_type=PreparationSourceType.AI_SALES_ACTION_PLAN,
            source_id=plan_id, source_action_id=_plan_action_id(action),
            action_type=ActionType.REVIEW_COMMERCIAL_CONTEXT, channel=PreparationChannel.EMAIL,
        )
        async with factory() as session:
            preparation = await prepare_outreach(session, request, provider=SelectionProvider())
            assert preparation.status == "READY_FOR_HUMAN_REVIEW"
        async with factory() as session:
            approval = await approve_preparation(session, preparation.id, approved_by_user_id="postgres_test_user")
        email_provider = MockEmailProvider()
        async def execute_once():
            async with factory() as session:
                return await execute_approved_email(session, approval.id, provider=email_provider)
        sending = asyncio.create_task(execute_once())
        await asyncio.wait_for(email_provider.started.wait(), timeout=5)
        concurrent = await execute_once()
        assert concurrent.status == "EXECUTING"
        email_provider.release.set()
        first = await sending
        async with factory() as session:
            replay = await execute_approved_email(session, approval.id, provider=email_provider)
            assert first.status == "SENT" and replay.id == first.id
            assert await session.scalar(select(func.count()).select_from(AiOutreachApproval).where(AiOutreachApproval.lead_id == lead_id)) == 1
            assert await session.scalar(select(func.count()).select_from(AiOutreachExecution).where(AiOutreachExecution.lead_id == lead_id)) == 1
            assert await session.scalar(select(func.count()).select_from(AiOutreachPreparation).where(AiOutreachPreparation.lead_id == lead_id)) == 1
        assert email_provider.calls == 1 and first.provider_message_id == "postgres-mock-message"
    finally:
        if lead_id is not None:
            async with factory() as session:
                await session.execute(delete(AiOutreachExecution).where(AiOutreachExecution.lead_id == lead_id))
                await session.execute(delete(AiOutreachApproval).where(AiOutreachApproval.lead_id == lead_id))
                await session.execute(delete(AiOutreachPreparation).where(AiOutreachPreparation.lead_id == lead_id))
                await session.execute(delete(AgentPolicyEvaluation).where(AgentPolicyEvaluation.lead_id == lead_id))
                await session.execute(delete(AiSalesActionPlan).where(AiSalesActionPlan.lead_id == lead_id))
                await session.execute(delete(Lead).where(Lead.id == lead_id))
                await session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgresql_task_agent_creation_audit_and_idempotent_retry():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL to an explicitly provisioned PostgreSQL database")
    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_POSTGRES_URL must use the asyncpg PostgreSQL dialect")
    from app.domain.ai_sales_action_planner import ActionPlanOutput, RecommendedAction, ActionType
    from app.schemas.autonomous_task_agent import TaskAgentSourceType, TaskExecutionRequest
    from app.services.ai_sales_intelligence import AISalesContextBuilder
    from app.services.autonomous_task_agent import _plan_action_id, execute_task_action

    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    lead_id = plan_id = None
    try:
        async with factory() as session:
            lead = Lead(company_name="PostgreSQL Task Agent integration")
            session.add(lead)
            await session.flush()
            lead_id = lead.id
            context, fingerprint, _ = await AISalesContextBuilder().build(session, lead.id)
            evidence_id = next(item.id for item in context.commercial_data if item.source_type == "commercial_context")
            lead_priority = next(item.data["priority"] for item in context.commercial_data if item.source_type == "commercial_context")
            action = RecommendedAction(
                action_type=ActionType.REVIEW_COMMERCIAL_CONTEXT, priority=lead_priority,
                title="Review commercial context", rationale="The cited records may warrant human review.",
                evidence_ids=(evidence_id,), opportunity_ids=(), service_ids=(),
                preparation_items=("Check the current sales and commercial context.",),
            )
            plan = AiSalesActionPlan(
                lead_id=lead.id, contract_version="ai-sales-action-planner-v1",
                prompt_version="ai-sales-action-planner-prompt-v1", context_version="ai-sales-context-v1",
                provider="integration-test", model="no-network", status="validated",
                source_fingerprint=fingerprint, action_plan=ActionPlanOutput(actions=(action,)).model_dump(mode="json"),
                evidence_catalog=[], evidence_refs=[evidence_id], usage_metadata={},
            )
            session.add(plan)
            await session.commit()
            plan_id = plan.id
            request = TaskExecutionRequest(
                lead_id=lead.id, action_type=action.action_type, source_type=TaskAgentSourceType.AI_SALES_ACTION_PLAN,
                source_id=plan.id, source_action_id=_plan_action_id(action), context_fingerprint=fingerprint,
                evidence_ids=(evidence_id,),
            )
            created = await execute_task_action(session, request)
            retried = await execute_task_action(session, request)
            task = await session.get(SalesTask, created.task_id)
            attempts = (await session.scalars(
                select(AutonomousTaskExecution).where(AutonomousTaskExecution.lead_id == lead.id)
            )).all()
            assert created.execution_status == "CREATED" and task is not None
            assert task.created_by_agent is True and task.agent_source_id == plan.id
            assert retried.execution_status == "ALREADY_EXISTS" and retried.existing_task_id == task.id
            assert len(attempts) == 2
            assert {attempt.execution_status for attempt in attempts} == {"CREATED", "ALREADY_EXISTS"}
            assert all(attempt.task_id == task.id for attempt in attempts)
            assert await session.scalar(
                select(func.count()).select_from(AgentPolicyEvaluation).where(AgentPolicyEvaluation.lead_id == lead.id)
            ) == 1
    finally:
        if lead_id is not None:
            async with factory() as session:
                await session.execute(delete(Lead).where(Lead.id == lead_id))
                await session.commit()
        await engine.dispose()

@pytest.mark.asyncio
async def test_postgresql_concurrent_workers_only_claim_job_once():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL to an explicitly provisioned PostgreSQL database")
    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_POSTGRES_URL must use the asyncpg PostgreSQL dialect")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    lead_id = job_id = None
    try:
        async with factory() as session:
            lead = Lead(company_name="PostgreSQL claim integration")
            session.add(lead); await session.flush()
            job = ProcessingJob(lead_id=lead.id, status=JobStatus.QUEUED)
            session.add(job); await session.commit()
            lead_id, job_id = lead.id, job.id
        async def claim():
            async with factory() as session:
                return await claim_jobs(session, 1)
        claims = await asyncio.gather(claim(), claim())
        assert sum(job_id in claimed for claimed in claims) == 1
    finally:
        async with factory() as session:
            if job_id: await session.execute(delete(ProcessingJob).where(ProcessingJob.id == job_id))
            if lead_id: await session.execute(delete(Lead).where(Lead.id == lead_id))
            await session.commit()
        await engine.dispose()

@pytest.mark.asyncio
async def test_postgresql_concurrent_provider_requests_only_lease_key_once():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL to an explicitly provisioned PostgreSQL database")
    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_POSTGRES_URL must use the asyncpg PostgreSQL dialect")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    key_id = None
    try:
        async with factory() as session:
            key = ApiKey(provider="lease-integration", key="integration-secret")
            session.add(key)
            await session.commit()
            key_id = key.id

        async def lease():
            async with factory() as session:
                return await ApiKeyPool().lease(session, "lease-integration", "lookup")

        first, second = await asyncio.gather(lease(), lease())
        assert (first is None) != (second is None)
    finally:
        if key_id is not None:
            async with factory() as session:
                await session.execute(delete(ApiKey).where(ApiKey.id == key_id))
                await session.commit()
        await engine.dispose()

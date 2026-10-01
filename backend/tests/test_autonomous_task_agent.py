"""Focused tests for controlled internal SalesTask execution in Phase 10C."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import importlib.util

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app import main as main_module
from app.config import settings
from app.database import SCHEMA_REVISION, get_db
from app.domain.ai_sales_action_planner import ActionPlanOutput, RecommendedAction, ActionType
from app.domain.ai_sales_supervisor import PipelineSummary, SupervisorAttentionType, SupervisorFinding, SupervisorResult, SupervisorUrgency
from app.models import (
    AgentPolicyEvaluation, AiSalesActionPlan, AiSalesSupervisorRun, AutonomousTaskExecution,
    Lead, SalesActivity, SalesPriority, SalesTask, SalesTaskStatus, SalesTaskType,
)
from app.schemas.autonomous_task_agent import TaskAgentSourceType, TaskExecutionRequest, TaskExecutionStatus
from app.services import autonomous_task_agent as agent
from app.services.ai_sales_intelligence import AISalesContextBuilder
from app.services.ai_sales_supervisor import build_pipeline_context
from test_ai_sales_intelligence import NOW, seed_context


def _action(evidence_id: str, priority="HIGH"):
    return RecommendedAction(
        action_type=ActionType.REVIEW_COMMERCIAL_CONTEXT,
        priority=priority, title="Review commercial context",
        rationale="The cited records may warrant human review.",
        evidence_ids=(evidence_id,), opportunity_ids=(), service_ids=(),
        preparation_items=("Check the current sales and commercial context.",),
    )


async def _saved_plan(session_factory, lead_id, *, other_lead_id=None):
    async with session_factory() as session:
        context, fingerprint, _ = await AISalesContextBuilder().build(session, lead_id)
        evidence_id = next(item.id for item in context.commercial_data if item.source_type == "commercial_context")
        action = _action(evidence_id)
        output = ActionPlanOutput(actions=(action,))
        row = AiSalesActionPlan(
            lead_id=other_lead_id or lead_id, contract_version="ai-sales-action-planner-v1",
            prompt_version="ai-sales-action-planner-prompt-v1", context_version="ai-sales-context-v1",
            provider="test", model="no-provider", status="validated", source_fingerprint=fingerprint,
            action_plan=output.model_dump(mode="json"), evidence_catalog=[], evidence_refs=[evidence_id],
            usage_metadata={},
        )
        session.add(row)
        await session.commit()
        return row.id, fingerprint, action, evidence_id


def _request(lead_id, source_id, fingerprint, action, evidence_id, *, source_type=TaskAgentSourceType.AI_SALES_ACTION_PLAN, source_action_id=None, **extra):
    source_action_id = source_action_id or agent._plan_action_id(action)
    return TaskExecutionRequest(
        lead_id=lead_id, action_type=action.action_type, source_type=source_type,
        source_id=source_id, source_action_id=source_action_id,
        context_fingerprint=fingerprint, evidence_ids=(evidence_id,), requested_by="client-spoofed-user", **extra,
    )


@pytest.mark.asyncio
async def test_allowed_policy_creates_deterministic_internal_task_and_audit(session_factory, monkeypatch):
    ids = await seed_context(session_factory)
    source_id, fingerprint, action, evidence_id = await _saved_plan(session_factory, ids["lead_id"])
    invoked = []
    async def no_provider(*_args, **_kwargs):
        invoked.append(True)
    monkeypatch.setattr(agent, "get_provider", no_provider, raising=False)
    async with session_factory() as session:
        before = await session.get(Lead, ids["lead_id"])
        sales_values = (before.sales_status, before.priority, before.estimated_value, before.proposal_value, before.negotiation_value, before.won_value)
        result = await agent.execute_task_action(session, _request(ids["lead_id"], source_id, fingerprint, action, evidence_id), now=NOW)
        task = await session.get(SalesTask, result.task_id)
        audit = await session.get(AutonomousTaskExecution, result.execution_id)
        lead = await session.get(Lead, ids["lead_id"])
        assert result.execution_status == TaskExecutionStatus.CREATED and result.created
        assert result.policy_decision == "ALLOW"
        assert task.title == "Review commercial context: Northwind Studio"
        assert task.task_type == SalesTaskType.OTHER
        assert task.priority == SalesPriority.HIGH
        assert task.due_at.replace(tzinfo=timezone.utc) == NOW + timedelta(days=1)
        assert task.status == SalesTaskStatus.OPEN
        assert task.created_by_agent is True
        assert task.agent_source_type == "AI_SALES_ACTION_PLAN"
        assert task.agent_source_id == source_id and task.agent_source_action_id == agent._plan_action_id(action)
        assert task.agent_evidence_refs == [evidence_id]
        assert "No prospect communication is performed" in task.description
        assert audit.requested_by == agent.TASK_AGENT_REQUESTER
        assert audit.policy_evaluation_id is not None
        assert (lead.sales_status, lead.priority, lead.estimated_value, lead.proposal_value, lead.negotiation_value, lead.won_value) == sales_values
        assert await session.scalar(select(func.count()).select_from(AgentPolicyEvaluation)) == 1
        assert await session.scalar(select(func.count()).select_from(SalesActivity)) == 1
    assert invoked == []
    assert not hasattr(agent, "get_provider") or not invoked


@pytest.mark.asyncio
async def test_retry_is_idempotent_and_existing_active_task_is_referenced(session_factory):
    ids = await seed_context(session_factory)
    source_id, fingerprint, action, evidence_id = await _saved_plan(session_factory, ids["lead_id"])
    body = _request(ids["lead_id"], source_id, fingerprint, action, evidence_id)
    async with session_factory() as session:
        first = await agent.execute_task_action(session, body, now=NOW)
        second = await agent.execute_task_action(session, body, now=NOW)
        assert first.execution_status == TaskExecutionStatus.CREATED
        assert second.execution_status == TaskExecutionStatus.ALREADY_EXISTS
        assert second.existing_task_id == first.task_id
        assert await session.scalar(select(func.count()).select_from(SalesTask).where(SalesTask.created_by_agent.is_(True))) == 1
        assert await session.scalar(select(func.count()).select_from(AutonomousTaskExecution)) == 2


@pytest.mark.asyncio
async def test_duplicate_manual_active_task_is_not_duplicated(session_factory):
    ids = await seed_context(session_factory)
    async with session_factory() as session:
        existing = SalesTask(lead_id=ids["lead_id"], task_type=SalesTaskType.OTHER,
            title="Existing review", due_at=NOW + timedelta(days=1), priority=SalesPriority.HIGH,
            status=SalesTaskStatus.OPEN)
        session.add(existing); await session.commit(); existing_id = existing.id
        source_id, fingerprint, action, evidence_id = await _saved_plan(session_factory, ids["lead_id"])
    async with session_factory() as session:
        result = await agent.execute_task_action(session, _request(ids["lead_id"], source_id, fingerprint, action, evidence_id), now=NOW)
        assert result.execution_status == TaskExecutionStatus.ALREADY_EXISTS
        assert result.existing_task_id == existing_id
        assert await session.scalar(select(func.count()).select_from(SalesTask)) == 2


@pytest.mark.asyncio
async def test_follow_up_requires_human_approval_and_does_not_create_task(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Approval Example", email="person@example.test")
        session.add(lead); await session.commit(); lead_id = lead.id
        context, fingerprint, _ = await AISalesContextBuilder().build(session, lead_id)
        evidence_id = next(item.id for item in context.commercial_data)
        output = ActionPlanOutput(actions=(RecommendedAction(
            action_type=ActionType.FOLLOW_UP, priority="MEDIUM", title="Follow up with prospect",
            rationale="The cited records may support a follow-up discussion.", evidence_ids=(evidence_id,),
            opportunity_ids=(), service_ids=(), preparation_items=(),
        ),))
        artifact = AiSalesActionPlan(lead_id=lead_id, contract_version="ai-sales-action-planner-v1",
            prompt_version="ai-sales-action-planner-prompt-v1", context_version="ai-sales-context-v1",
            provider="test", model="no-provider", status="validated", source_fingerprint=fingerprint,
            action_plan=output.model_dump(mode="json"), evidence_catalog=[], evidence_refs=[evidence_id], usage_metadata={})
        session.add(artifact); await session.commit()
        action = output.actions[0]
        result = await agent.execute_task_action(session, _request(lead_id, artifact.id, fingerprint, action, evidence_id), now=NOW)
        assert result.execution_status == TaskExecutionStatus.REQUIRES_HUMAN_APPROVAL
        assert result.policy_decision == "REQUIRE_HUMAN_APPROVAL"
        assert result.task_id is None and not result.created
        assert await session.scalar(select(func.count()).select_from(SalesTask)) == 0
        assert await session.scalar(select(func.count()).select_from(AutonomousTaskExecution)) == 1


@pytest.mark.asyncio
async def test_policy_failure_is_audited_and_creates_no_task(session_factory, monkeypatch):
    ids = await seed_context(session_factory)
    source_id, fingerprint, action, evidence_id = await _saved_plan(session_factory, ids["lead_id"])
    async def fail_policy(*_args, **_kwargs):
        raise RuntimeError("provider or policy secret must not be exposed")
    monkeypatch.setattr(agent, "evaluate_policy", fail_policy)
    async with session_factory() as session:
        result = await agent.execute_task_action(session, _request(ids["lead_id"], source_id, fingerprint, action, evidence_id))
        assert result.execution_status == TaskExecutionStatus.FAILED
        assert result.task_id is None
        assert "secret" not in result.reason
        assert await session.scalar(select(func.count()).select_from(AutonomousTaskExecution)) == 1
        assert await session.scalar(select(func.count()).select_from(SalesTask).where(SalesTask.created_by_agent.is_(True))) == 0


@pytest.mark.asyncio
async def test_phase10a_policy_denial_is_audited_without_task_creation(session_factory):
    ids = await seed_context(session_factory)
    async with session_factory() as session:
        lead = await session.get(Lead, ids["lead_id"])
        lead.phone_opted_out = True
        await session.commit()
        context, fingerprint, _ = await AISalesContextBuilder().build(session, ids["lead_id"])
        activity = next(item for item in context.historical_events if item.source_type == "sales_activity")
        action = RecommendedAction(
            action_type=ActionType.CALL_PROSPECT, priority="HIGH", title="Call prospect",
            rationale="The cited records may warrant human review.", evidence_ids=(activity.id,),
            opportunity_ids=(), service_ids=(), preparation_items=(),
        )
        output = ActionPlanOutput(actions=(action,))
        plan = AiSalesActionPlan(lead_id=ids["lead_id"], contract_version="ai-sales-action-planner-v1",
            prompt_version="ai-sales-action-planner-prompt-v1", context_version="ai-sales-context-v1",
            provider="test", model="no-provider", status="validated", source_fingerprint=fingerprint,
            action_plan=output.model_dump(mode="json"), evidence_catalog=[], evidence_refs=[activity.id], usage_metadata={})
        session.add(plan); await session.commit()
        result = await agent.execute_task_action(session, _request(ids["lead_id"], plan.id, fingerprint, action, activity.id), now=NOW)
        assert result.execution_status == TaskExecutionStatus.DENIED
        assert result.policy_decision == "DENY"
        assert result.policy_evaluation_id is not None
        assert await session.scalar(select(func.count()).select_from(SalesTask).where(SalesTask.created_by_agent.is_(True))) == 0
        assert await session.scalar(select(func.count()).select_from(AutonomousTaskExecution).where(AutonomousTaskExecution.execution_status == "DENIED")) == 1


@pytest.mark.asyncio
async def test_stale_supervisor_artifact_is_audited_before_creation(session_factory):
    ids = await seed_context(session_factory)
    async with session_factory() as session:
        context, current_fingerprint, _ = await AISalesContextBuilder().build(session, ids["lead_id"])
        evidence_id = next(item.id for item in context.commercial_data)
        action = _action(evidence_id)
        finding = SupervisorFinding(
            finding_id="d" * 64, lead_id=ids["lead_id"], attention_type=SupervisorAttentionType.COMMERCIAL_REVIEW,
            urgency=SupervisorUrgency.LOW, title="Review", summary="Review current commercial data.",
            rationale="Cited commercial data is available.", evidence_ids=(evidence_id,),
            action_type=ActionType.REVIEW_COMMERCIAL_CONTEXT, action_priority=SupervisorUrgency.LOW,
            policy_decision=None, autonomy_level=None, review_required=False,
        )
        _, data, _ = await build_pipeline_context(session)
        result = SupervisorResult(artifact_version="ai-sales-supervisor-v1", prompt_version="ai-sales-supervisor-prompt-v1",
            context_version="ai-sales-context-v1", generated_at=NOW.isoformat(), context_fingerprint="a" * 64,
            findings=(finding,), pipeline_summary=data["summary"], review_notes=(), evidence_refs=(evidence_id,), usage={})
        run = AiSalesSupervisorRun(scope="PIPELINE", status="COMPLETED", artifact_version="ai-sales-supervisor-v1",
            prompt_version="ai-sales-supervisor-prompt-v1", context_version="ai-sales-context-v1",
            context_fingerprint="a" * 64, result_json=result.model_dump(mode="json"), evidence_refs=[evidence_id],
            usage_json={}, scope_metadata={}, provider="fake", model="fake", created_at=NOW, updated_at=NOW)
        session.add(run); await session.commit()
        request = TaskExecutionRequest(lead_id=ids["lead_id"], action_type=ActionType.REVIEW_COMMERCIAL_CONTEXT,
            source_type=TaskAgentSourceType.AI_SALES_SUPERVISOR, source_id=run.id, source_action_id=finding.finding_id,
            context_fingerprint=current_fingerprint, evidence_ids=(evidence_id,))
        outcome = await agent.execute_task_action(session, request, now=NOW)
        assert outcome.execution_status == TaskExecutionStatus.STALE_CONTEXT
        assert await session.scalar(select(func.count()).select_from(SalesTask).where(SalesTask.created_by_agent.is_(True))) == 0
        assert await session.scalar(select(func.count()).select_from(AutonomousTaskExecution).where(AutonomousTaskExecution.execution_status == "STALE_CONTEXT")) == 1


@pytest.mark.asyncio
async def test_stale_invalid_missing_and_cross_lead_sources_are_audited_without_tasks(session_factory):
    ids = await seed_context(session_factory)
    source_id, fingerprint, action, evidence_id = await _saved_plan(session_factory, ids["lead_id"])
    async with session_factory() as session:
        lead = await session.get(Lead, ids["lead_id"]); lead.company_name = "Changed Context"; await session.commit()
        stale = await agent.execute_task_action(session, _request(ids["lead_id"], source_id, fingerprint, action, evidence_id))
        invalid = await agent.execute_task_action(session, _request(ids["lead_id"], uuid4(), "a" * 64, action, "invented-evidence"))
        foreign_source, foreign_fingerprint, foreign_action, foreign_evidence = await _saved_plan(session_factory, ids["lead_id"], other_lead_id=ids["other_lead_id"])
    async with session_factory() as session:
        context, current_fp, _ = await AISalesContextBuilder().build(session, ids["lead_id"])
        foreign = await agent.execute_task_action(session, _request(ids["lead_id"], foreign_source, current_fp, foreign_action, foreign_evidence))
        assert stale.execution_status == TaskExecutionStatus.STALE_CONTEXT
        assert invalid.execution_status == TaskExecutionStatus.INVALID_REQUEST
        assert foreign.execution_status == TaskExecutionStatus.INVALID_REQUEST
        assert await session.scalar(select(func.count()).select_from(SalesTask).where(SalesTask.created_by_agent.is_(True))) == 0
        assert await session.scalar(select(func.count()).select_from(AutonomousTaskExecution)) == 3


@pytest.mark.asyncio
async def test_request_cannot_force_policy_or_agent_provenance_and_invalid_action_is_rejected():
    with pytest.raises(Exception):
        TaskExecutionRequest.model_validate({
            "lead_id": str(uuid4()), "action_type": "SEND_EMAIL", "source_type": "AI_SALES_ACTION_PLAN",
            "source_id": str(uuid4()), "source_action_id": "x", "context_fingerprint": "a" * 64,
            "evidence_ids": ["evidence"], "policy_decision": "ALLOW", "created_by_agent": True,
        })


@pytest.mark.asyncio
async def test_supervisor_batch_executes_mixed_actions_and_is_audited(session_factory):
    ids = await seed_context(session_factory)
    async with session_factory() as session:
        prompt, data, fingerprint = await build_pipeline_context(session)
        lead_candidate = next(item for item in data["candidates"] if item["lead_id"] == str(ids["lead_id"]))
        other_candidate = next(item for item in data["candidates"] if item["lead_id"] == str(ids["other_lead_id"]))
        evidence_id = next(item.id for item in lead_candidate["context_object"].commercial_data)
        finding = SupervisorFinding(
            finding_id="b" * 64, lead_id=ids["lead_id"], attention_type=SupervisorAttentionType.COMMERCIAL_REVIEW,
            urgency=SupervisorUrgency.LOW, title="Commercial review", summary="Review recorded commercial context.",
            rationale="Evidence supports internal review.", evidence_ids=(evidence_id,),
            action_type=ActionType.REVIEW_COMMERCIAL_CONTEXT, action_priority=SupervisorUrgency.LOW,
            policy_decision=None, autonomy_level=None, review_required=False,
        )
        other_commercial = next(item for item in other_candidate["context_object"].commercial_data)
        second_finding = SupervisorFinding(
            finding_id="c" * 64, lead_id=ids["other_lead_id"], attention_type=SupervisorAttentionType.OUTREACH_RESPONSE_PENDING,
            urgency=SupervisorUrgency.HIGH, title="Pending response", summary="A response needs human review.",
            rationale="A recorded activity supports follow-up review.", evidence_ids=(other_commercial.id,),
            action_type=ActionType.CALL_PROSPECT, action_priority=SupervisorUrgency.HIGH,
            policy_decision=None, autonomy_level=None, review_required=False,
        )
        result = SupervisorResult(
            artifact_version="ai-sales-supervisor-v1", prompt_version="ai-sales-supervisor-prompt-v1",
            context_version="ai-sales-context-v1", generated_at=NOW.isoformat(), context_fingerprint=fingerprint,
            findings=(finding, second_finding), pipeline_summary=data["summary"], review_notes=(), evidence_refs=(evidence_id, other_commercial.id), usage={},
        )
        session.add(AiSalesSupervisorRun(scope="PIPELINE", status="COMPLETED", artifact_version="ai-sales-supervisor-v1",
            prompt_version="ai-sales-supervisor-prompt-v1", context_version="ai-sales-context-v1",
            context_fingerprint=fingerprint, result_json=result.model_dump(mode="json"), evidence_refs=[evidence_id, other_commercial.id],
            usage_json={}, scope_metadata={}, provider="fake", model="fake", created_at=NOW, updated_at=NOW))
        await session.commit()
        total, eligible, executions = await agent.execute_supervisor_run_tasks(session)
        assert total == eligible == 2
        assert {item.execution_status for item in executions} == {TaskExecutionStatus.CREATED, TaskExecutionStatus.REQUIRES_HUMAN_APPROVAL}


@pytest.mark.asyncio
async def test_authenticated_execution_history_and_supervisor_batch_endpoint(session_factory, monkeypatch):
    async def override_db():
        async with session_factory() as session: yield session
    monkeypatch.setattr(settings, "api_auth_token", "task-agent-test-token")
    app = main_module.app
    app.dependency_overrides[get_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/agent/tasks/executions")).status_code == 401
            headers = {"Authorization": "Bearer task-agent-test-token"}
            history = await client.get("/api/agent/tasks/executions", headers=headers)
            assert history.status_code == 200 and history.json()["items"] == []
            batch = await client.post("/api/sales-supervisor/tasks/execute", headers=headers)
            assert batch.status_code == 201 and batch.json()["eligible"] == 0
            assert (await client.get(f"/api/leads/{uuid4()}/agent/tasks/executions", headers=headers)).status_code == 404
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_task_agent_migration_head_and_database_registration():
    from app.models import Base
    migration_path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0022_autonomous_task_executions.py"
    spec = importlib.util.spec_from_file_location("phase10c_migration", migration_path)
    migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    assert (migration.revision, migration.down_revision) == ("0022_autonomous_task_executions", "0021_ai_sales_supervisor")
    assert SCHEMA_REVISION == "0024_ai_outreach_executions"
    assert {"autonomous_task_executions", "sales_tasks", "ai_outreach_preparations"}.issubset(Base.metadata.tables)
    assert {"created_by_agent", "agent_source_id", "agent_source_action_id", "agent_evidence_refs"}.issubset(Base.metadata.tables["sales_tasks"].columns.keys())

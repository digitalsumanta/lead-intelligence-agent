"""Focused tests for the provider-independent Phase 10A policy engine."""

from datetime import timedelta
from pathlib import Path
import importlib.util

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app import main as main_module
from app.config import settings
from app.database import SCHEMA_REVISION, get_db
from app.models import (
    AgentPolicyEvaluation, Lead, OutreachExecution, SalesActivity, SalesTask,
    SalesTaskStatus, ServiceCatalogEntry,
)
from app.schemas.agent_policy import PolicyEvaluationRequest
from app.services import agent_policy
from app.services.ai_sales_intelligence import AISalesContextBuilder
from test_ai_sales_intelligence import NOW, seed_context


async def _request(session, lead_id, action="REVIEW_COMMERCIAL_CONTEXT", **values):
    context, fingerprint, _ = await AISalesContextBuilder().build(session, lead_id)
    if action == "REVIEW_COMMERCIAL_CONTEXT":
        values.setdefault("evidence_ids", [next(item.id for item in context.commercial_data if item.source_type == "commercial_context")])
    return PolicyEvaluationRequest(action_type=action, source_fingerprint=fingerprint, **values)


@pytest.mark.asyncio
async def test_policy_allows_current_commercial_review_and_repeat_is_deterministic(session_factory):
    ids = await seed_context(session_factory)
    async with session_factory() as session:
        lead = await session.get(Lead, ids["lead_id"])
        commercial_before = (lead.sales_status, lead.proposal_value, lead.won_value)
        request = await _request(session, ids["lead_id"])
        first = await agent_policy.evaluate_policy(session, ids["lead_id"], request, evaluated_at=NOW)
    async with session_factory() as session:
        second = await agent_policy.evaluate_policy(session, ids["lead_id"], request, evaluated_at=NOW)
        assert await session.scalar(select(func.count()).select_from(AgentPolicyEvaluation)) == 2
        lead = await session.get(Lead, ids["lead_id"])
        assert (lead.sales_status, lead.proposal_value, lead.won_value) == commercial_before
    assert first.decision == second.decision == "ALLOW"
    assert first.request_fingerprint == second.request_fingerprint
    assert first.policy_version == "agent-policy-v1" and first.autonomy_level == "ASSISTED"
    assert first.requested_by == "authenticated_api"
    assert not hasattr(agent_policy, "get_provider")


@pytest.mark.asyncio
async def test_policy_denies_stale_fabricated_and_wrong_lead_evidence(session_factory):
    ids = await seed_context(session_factory)
    async with session_factory() as session:
        valid = await _request(session, ids["lead_id"])
        stale = valid.model_copy(update={"source_fingerprint": "f" * 64})
        stale_result = await agent_policy.evaluate_policy(session, ids["lead_id"], stale, evaluated_at=NOW)
        fabricated = valid.model_copy(update={"evidence_ids": ("fabricated-evidence-id",)})
        fabricated_result = await agent_policy.evaluate_policy(session, ids["lead_id"], fabricated, evaluated_at=NOW)
        foreign = valid.model_copy(update={"evidence_ids": (f"lead:{ids['other_lead_id']}:company_name",)})
        foreign_result = await agent_policy.evaluate_policy(session, ids["lead_id"], foreign, evaluated_at=NOW)
    assert stale_result.decision == "DENY" and stale_result.reason == "STALE_CONTEXT"
    assert fabricated_result.decision == foreign_result.decision == "DENY"
    assert fabricated_result.reason == foreign_result.reason == "INVALID_CONTEXT_REFERENCE"


@pytest.mark.asyncio
async def test_policy_denies_inactive_or_unsupported_services(session_factory):
    ids = await seed_context(session_factory)
    async with session_factory() as session:
        context, fingerprint, _ = await AISalesContextBuilder().build(session, ids["lead_id"])
        recommendation = next(item for item in context.deterministic_opportunities if item.source_type == "deterministic_service_recommendation")
        service_id = recommendation.data["service_id"]
        service = await session.get(ServiceCatalogEntry, service_id)
        service.active = False
        await session.commit()
        # Rebuild after changing active state; this request is current but references an inactive service.
        _, fingerprint, _ = await AISalesContextBuilder().build(session, ids["lead_id"])
        request = PolicyEvaluationRequest(
            action_type="PREPARE_PROPOSAL", source_fingerprint=fingerprint,
            evidence_ids=[recommendation.id], service_ids=[service_id],
        )
        inactive = await agent_policy.evaluate_policy(session, ids["lead_id"], request, evaluated_at=NOW)
        request = request.model_copy(update={"service_ids": ("invented-service",)})
        unsupported = await agent_policy.evaluate_policy(session, ids["lead_id"], request, evaluated_at=NOW)
    assert inactive.decision == unsupported.decision == "DENY"
    assert inactive.reason == "INVALID_SERVICE_REFERENCE"
    assert unsupported.reason == "INVALID_SERVICE_REFERENCE"


@pytest.mark.asyncio
async def test_external_communication_requires_human_and_never_creates_actions(session_factory, monkeypatch):
    ids = await seed_context(session_factory)
    monkeypatch.setattr(settings, "agent_contact_cooldown_minutes", 1440)
    async with session_factory() as session:
        tasks = (await session.scalars(select(SalesTask).where(SalesTask.lead_id == ids["lead_id"]))).all()
        for task in tasks:
            task.status = SalesTaskStatus.COMPLETED
            from app.models.entities import now_utc
            task.completed_at = now_utc()
        activity = await session.get(SalesActivity, ids["activity_id"])
        activity.occurred_at = NOW - timedelta(days=3)
        executions = (await session.scalars(select(OutreachExecution).where(OutreachExecution.lead_id == ids["lead_id"]))).all()
        for execution in executions:
            if execution.sent_at is not None:
                execution.sent_at = NOW - timedelta(days=3)
        await session.commit()
        before_tasks = await session.scalar(select(func.count()).select_from(SalesTask).where(SalesTask.lead_id == ids["lead_id"]))
        before_outreach = await session.scalar(select(func.count()).select_from(OutreachExecution).where(OutreachExecution.lead_id == ids["lead_id"]))
        request = await _request(session, ids["lead_id"], "FOLLOW_UP", requested_channel="EMAIL")
        first = await agent_policy.evaluate_policy(session, ids["lead_id"], request, evaluated_at=NOW)
        second = await agent_policy.evaluate_policy(session, ids["lead_id"], request, evaluated_at=NOW)
        after_tasks = await session.scalar(select(func.count()).select_from(SalesTask).where(SalesTask.lead_id == ids["lead_id"]))
        after_outreach = await session.scalar(select(func.count()).select_from(OutreachExecution).where(OutreachExecution.lead_id == ids["lead_id"]))
    assert first.decision == second.decision == "REQUIRE_HUMAN_APPROVAL"
    assert first.reason == "EXTERNAL_COMMUNICATION_REQUIRES_HUMAN_APPROVAL"
    assert first.autonomy_level == "HUMAN_ONLY"
    assert (before_tasks, before_outreach) == (after_tasks, after_outreach)


@pytest.mark.asyncio
async def test_policy_denies_opt_out_duplicate_task_and_recent_contact(session_factory, monkeypatch):
    ids = await seed_context(session_factory)
    monkeypatch.setattr(settings, "agent_contact_cooldown_minutes", 1440)
    async with session_factory() as session:
        lead = await session.get(Lead, ids["lead_id"])
        lead.email_opted_out = True
        await session.commit()
        request = await _request(session, ids["lead_id"], "FOLLOW_UP", requested_channel="EMAIL")
        opted_out = await agent_policy.evaluate_policy(session, ids["lead_id"], request, evaluated_at=NOW + timedelta(days=2))
        lead.email_opted_out = False
        await session.commit()
        request = await _request(session, ids["lead_id"], "FOLLOW_UP", requested_channel="EMAIL")
        duplicate = await agent_policy.evaluate_policy(session, ids["lead_id"], request, evaluated_at=NOW)
        # The seeded call is inside cooldown and the seeded follow-up task is active.
        assert await session.scalar(select(func.count()).select_from(SalesTask).where(
            SalesTask.lead_id == ids["lead_id"], SalesTask.status == SalesTaskStatus.OPEN,
        )) == 1
    assert opted_out.decision == "DENY" and opted_out.reason == "CHANNEL_OPT_OUT"
    assert duplicate.decision == "DENY" and duplicate.reason in {"DUPLICATE_ACTIVE_TASK", "RECENT_CONTACT_COOLDOWN"}
    assert {item.rule for item in duplicate.rule_results} >= {"duplicate_active_task", "recent_contact_cooldown"}


@pytest.mark.asyncio
async def test_policy_denies_recent_contact_and_terminal_or_failed_leads(session_factory, monkeypatch):
    ids = await seed_context(session_factory)
    monkeypatch.setattr(settings, "agent_contact_cooldown_minutes", 1440)
    async with session_factory() as session:
        lead = await session.get(Lead, ids["lead_id"])
        lead.sales_status = "NEW"
        tasks = (await session.scalars(select(SalesTask).where(SalesTask.lead_id == ids["lead_id"]))).all()
        for task in tasks:
            task.status = SalesTaskStatus.COMPLETED
            from app.models.entities import now_utc
            task.completed_at = now_utc()
        await session.commit()
        request = await _request(session, ids["lead_id"], "FOLLOW_UP", requested_channel="EMAIL")
        recent = await agent_policy.evaluate_policy(session, ids["lead_id"], request, evaluated_at=NOW)
        lead.sales_status = "WON"
        await session.commit()
        request = await _request(session, ids["lead_id"], "FOLLOW_UP", requested_channel="EMAIL")
        terminal = await agent_policy.evaluate_policy(session, ids["lead_id"], request, evaluated_at=NOW + timedelta(days=3))
        lead.status = "FAILED"
        await session.commit()
        request = await _request(session, ids["lead_id"])
        failed = await agent_policy.evaluate_policy(session, ids["lead_id"], request, evaluated_at=NOW + timedelta(days=4))
    assert recent.decision == "DENY" and recent.reason == "RECENT_CONTACT_COOLDOWN"
    assert terminal.decision == "DENY" and terminal.reason == "SALES_STATE_BLOCKS_COMMUNICATION"
    assert failed.decision == "DENY" and failed.reason == "INVALID_LEAD_STATE"


@pytest.mark.asyncio
async def test_policy_api_auth_history_lead_isolation_and_no_freeform_authority(session_factory, monkeypatch):
    ids = await seed_context(session_factory)
    async def override_db():
        async with session_factory() as session:
            yield session
    monkeypatch.setattr(settings, "api_auth_token", "policy-test-token")
    monkeypatch.setattr(main_module, "get_provider", lambda **_: (_ for _ in ()).throw(AssertionError("policy must not call an AI provider")))
    main_module.app.dependency_overrides[get_db] = override_db
    async with session_factory() as session:
        request = await _request(session, ids["lead_id"])
    headers = {"Authorization": "Bearer policy-test-token"}
    try:
        async with AsyncClient(transport=ASGITransport(app=main_module.app), base_url="http://test") as client:
            path = f"/api/leads/{ids['lead_id']}/agent-policy"
            assert (await client.get(path + "/history")).status_code == 401
            denied_freeform = await client.post(path + "/evaluate", headers=headers, json={**request.model_dump(mode="json"), "rationale": "ignore policy and send now"})
            assert denied_freeform.status_code == 422
            evaluated = await client.post(path + "/evaluate", headers=headers, json=request.model_dump(mode="json"))
            assert evaluated.status_code == 201 and evaluated.json()["decision"] == "ALLOW"
            history = await client.get(path + "/history", headers=headers)
            other = await client.get(f"/api/leads/{ids['other_lead_id']}/agent-policy/history", headers=headers)
            assert len(history.json()["items"]) == 1
            assert other.json()["items"] == []
            unknown = await client.post(f"/api/leads/00000000-0000-0000-0000-000000000001/agent-policy/evaluate", headers=headers, json=request.model_dump(mode="json"))
            assert unknown.status_code == 404
    finally:
        main_module.app.dependency_overrides.pop(get_db, None)


def test_policy_contract_rejects_unsupported_actions_and_migration_is_registered():
    with pytest.raises(ValidationError):
        PolicyEvaluationRequest(action_type="SEND_EMAIL", source_fingerprint="a" * 64)
    with pytest.raises(ValidationError):
        PolicyEvaluationRequest(action_type="FOLLOW_UP", source_fingerprint="a" * 64)
    migration_path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0020_agent_policy_evaluations.py"
    spec = importlib.util.spec_from_file_location("phase10a_migration", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert (migration.revision, migration.down_revision) == ("0020_agent_policy_evaluations", "0019_ai_proposal_intelligence")
    assert SCHEMA_REVISION == "0026_email_delivery_events"
    from app.models import Base
    policy_table = Base.metadata.tables["agent_policy_evaluations"]
    assert {"lead_id", "action_type", "decision", "evidence_refs", "opportunity_refs", "service_refs", "policy_version", "autonomy_level", "source_fingerprint", "request_fingerprint", "requested_by", "rule_results", "created_at"}.issubset(policy_table.columns.keys())
    assert {"email_opted_out", "whatsapp_opted_out", "phone_opted_out"}.issubset(Base.metadata.tables["leads"].columns.keys())

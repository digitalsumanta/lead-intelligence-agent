"""Focused tests for explicit, single-attempt Phase 10E email execution."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app import main as main_module
from app.config import settings
from app.database import SCHEMA_REVISION, get_db
from app.models import (
    AgentPolicyEvaluation, AiOutreachApproval, AiOutreachExecution,
    AiOutreachPreparation, Lead, SalesActivity, SalesActivityType,
    SalesTask, SalesTaskStatus,
)
from app.providers.outreach import OutreachProviderFailure, OutreachSendResult
from app.services.ai_outreach_execution import (
    OutreachExecutionError, approve_preparation, execute_approved_email,
)
from app.services.ai_outreach_preparation import prepare_outreach
from test_ai_outreach_preparation import FakeDraftProvider, body, saved_plan, selection
from test_ai_sales_intelligence import seed_context


class FakeEmailProvider:
    name = "mock_email"

    def __init__(self, *, error=None):
        self.error = error
        self.calls = []

    async def send(self, message):
        self.calls.append(message)
        if self.error:
            raise self.error
        return OutreachSendResult(provider_message_id="mock-message-10e")


async def prepared_email(session_factory):
    ids = await seed_context(session_factory)
    # The fixture intentionally includes a recent call and open follow-up task;
    # clear them so this test exercises the allowed contact path.
    async with session_factory() as session:
        lead = await session.get(Lead, ids["lead_id"])
        lead.email = "approved-recipient@example.test"
        lead.phone = "+14155550123"
        tasks = (await session.scalars(select(SalesTask).where(SalesTask.lead_id == lead.id))).all()
        for task in tasks:
            task.status = SalesTaskStatus.CANCELLED
        await session.execute(
            SalesActivity.__table__.delete().where(SalesActivity.lead_id == lead.id)
        )
        await session.commit()
    source_id, _, action, evidence_id = await saved_plan(session_factory, ids["lead_id"])
    async with session_factory() as session:
        draft = await prepare_outreach(
            session, body(ids["lead_id"], source_id, action),
            provider=FakeDraftProvider(selection(evidence_id)),
        )
    assert draft.status == "READY_FOR_HUMAN_REVIEW"
    return ids, draft


async def approve(session_factory, draft, user_id="sales_user_17"):
    async with session_factory() as session:
        return await approve_preparation(session, draft.id, approved_by_user_id=user_id)


@pytest.mark.asyncio
async def test_approval_is_authenticated_bound_to_exact_draft_and_execution_is_idempotent(session_factory, monkeypatch):
    ids, draft = await prepared_email(session_factory)
    mailer = FakeEmailProvider()
    monkeypatch.setattr("app.services.ai_outreach_execution.resolve_outreach_provider", lambda _channel: mailer)
    monkeypatch.setattr(settings, "api_auth_token", "phase-10e-test-token")
    monkeypatch.setattr(settings, "api_auth_user_id", "sales_user_17")

    async def override_db():
        async with session_factory() as session:
            yield session

    main_module.app.dependency_overrides[get_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=main_module.app), base_url="http://test") as client:
            approve_url = f"/api/agent/outreach/preparations/{draft.id}/approve"
            assert (await client.post(approve_url, json={})).status_code == 401
            headers = {"Authorization": "Bearer phase-10e-test-token"}
            forged = await client.post(approve_url, json={"approved_by_user_id": "attacker"}, headers=headers)
            assert forged.status_code == 422
            approved = await client.post(approve_url, json={}, headers=headers)
            assert approved.status_code == 200
            approval = approved.json()
            assert approval["approved_by_user_id"] == "sales_user_17"
            assert approval["recipient"] == "prospect@example.test"
            assert approval["preparation_id"] == str(draft.id)
            assert approval["content_hash"]
            assert approval["policy_decision"] in {"ALLOW", "REQUIRE_HUMAN_APPROVAL"}

            execute_url = f"/api/agent/outreach/approvals/{approval['id']}/execute"
            assert (await client.post(execute_url, json={"recipient": "attacker@example.test"}, headers=headers)).status_code == 422
            sent = await client.post(execute_url, json={}, headers=headers)
            assert sent.status_code == 200
            execution = sent.json()
            assert execution["status"] == "SENT"
            assert execution["provider_message_id"] == "mock-message-10e"
            assert execution["recipient"] == approval["recipient"]
            assert execution["subject"] == draft.subject
            assert execution["policy_decision"] == "REQUIRE_HUMAN_APPROVAL"
            assert execution["policy_evaluation_id"] != approval["policy_evaluation_id"]
            replay = await client.post(execute_url, json={}, headers=headers)
            assert replay.status_code == 200 and replay.json()["id"] == execution["id"]
            history = await client.get(f"/api/agent/outreach/preparations/{draft.id}/executions", headers=headers)
            assert len(history.json()["items"]) == 1
    finally:
        main_module.app.dependency_overrides.pop(get_db, None)

    assert len(mailer.calls) == 1
    assert mailer.calls[0].recipient == "prospect@example.test"
    assert mailer.calls[0].subject == draft.subject
    assert mailer.calls[0].content == draft.body
    assert mailer.calls[0].idempotency_key == execution["idempotency_key"]
    async with session_factory() as session:
        activity = await session.scalar(select(SalesActivity).where(
            SalesActivity.lead_id == ids["lead_id"], SalesActivity.activity_type == SalesActivityType.EMAIL,
        ))
        assert activity is not None and "Phase 10E" in activity.note
        assert await session.scalar(select(func.count()).select_from(AiOutreachExecution)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["context", "opt_out"])
async def test_stale_context_and_opt_out_block_execution_without_provider_call(session_factory, change):
    _, draft = await prepared_email(session_factory)
    approval = await approve(session_factory, draft)
    async with session_factory() as session:
        lead = await session.get(Lead, draft.lead_id)
        if change == "context":
            lead.city = "Changed after approval"
        else:
            lead.email_opted_out = True
        await session.commit()
    mailer = FakeEmailProvider()
    async with session_factory() as session:
        execution = await execute_approved_email(session, approval.id, provider=mailer)
    assert execution.status in {"STALE", "BLOCKED"}
    assert mailer.calls == []


@pytest.mark.asyncio
async def test_email_content_change_after_approval_is_blocked(session_factory):
    _, draft = await prepared_email(session_factory)
    approval = await approve(session_factory, draft)
    async with session_factory() as session:
        preparation = await session.get(AiOutreachPreparation, draft.id)
        preparation.body = "Forged altered email body"
        await session.commit()
    mailer = FakeEmailProvider()
    async with session_factory() as session:
        result = await execute_approved_email(session, approval.id, provider=mailer)
    assert result.status == "BLOCKED"
    assert result.failure_code == "APPROVED_CONTENT_CHANGED"
    assert mailer.calls == []


@pytest.mark.asyncio
async def test_non_email_preparation_cannot_be_approved(session_factory):
    ids = await seed_context(session_factory)
    source_id, _, action, evidence_id = await saved_plan(session_factory, ids["lead_id"])
    async with session_factory() as session:
        draft = await prepare_outreach(
            session, body(ids["lead_id"], source_id, action, channel="WHATSAPP"),
            provider=FakeDraftProvider(selection(evidence_id)),
        )
    with pytest.raises(OutreachExecutionError, match="Only EMAIL"):
        await approve(session_factory, draft)
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(AiOutreachApproval)) == 0


@pytest.mark.asyncio
async def test_provider_timeout_is_uncertain_and_never_retried(session_factory):
    _, draft = await prepared_email(session_factory)
    approval = await approve(session_factory, draft)
    mailer = FakeEmailProvider(error=OutreachProviderFailure("PROVIDER_TIMEOUT", "provider timed out"))
    async with session_factory() as session:
        first = await execute_approved_email(session, approval.id, provider=mailer)
    async with session_factory() as session:
        replay = await execute_approved_email(session, approval.id, provider=mailer)
    assert first.status == replay.status == "UNCERTAIN"
    assert first.failure_code == "PROVIDER_TIMEOUT"
    assert len(mailer.calls) == 1


@pytest.mark.asyncio
async def test_provider_rejection_is_failed_and_never_automatically_retried(session_factory):
    _, draft = await prepared_email(session_factory)
    approval = await approve(session_factory, draft)
    mailer = FakeEmailProvider(error=OutreachProviderFailure("PROVIDER_REJECTED", "provider rejected message"))
    async with session_factory() as session:
        first = await execute_approved_email(session, approval.id, provider=mailer)
    async with session_factory() as session:
        replay = await execute_approved_email(session, approval.id, provider=FakeEmailProvider())
    assert first.status == replay.status == "FAILED"
    assert first.failure_code == "PROVIDER_REJECTED"
    assert len(mailer.calls) == 1


@pytest.mark.asyncio
async def test_concurrent_execution_request_observes_reservation_without_sending_twice(session_factory):
    _, draft = await prepared_email(session_factory)
    approval = await approve(session_factory, draft)

    class WaitingEmailProvider(FakeEmailProvider):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def send(self, message):
            self.calls.append(message)
            self.started.set()
            await self.release.wait()
            return OutreachSendResult(provider_message_id="concurrent-message")

    mailer = WaitingEmailProvider()

    async def execute():
        async with session_factory() as session:
            return await execute_approved_email(session, approval.id, provider=mailer)

    first_request = asyncio.create_task(execute())
    await asyncio.wait_for(mailer.started.wait(), timeout=2)
    concurrent_request = await execute()
    assert concurrent_request.status == "EXECUTING"
    mailer.release.set()
    first = await first_request
    assert first.status == "SENT"
    assert len(mailer.calls) == 1


def test_phase_10e_migration_is_reversible_and_models_match():
    from app.models import Base
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0024_ai_outreach_executions.py"
    spec = importlib.util.spec_from_file_location("phase10e_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert (migration.revision, migration.down_revision) == ("0024_ai_outreach_executions", "0023_ai_outreach_preparations")
    assert SCHEMA_REVISION == migration.revision
    assert {"ai_outreach_approvals", "ai_outreach_executions"}.issubset(Base.metadata.tables)
    assert "UNIQUE" in str(Base.metadata.tables["ai_outreach_executions"].constraints).upper()

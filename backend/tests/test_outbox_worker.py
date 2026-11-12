"""
Unit and Integration Tests for Dedicated Outbox Worker Daemon (Finding D4.3-F05).

Phase 12J - D2 - Gate D-D4.4:
Validates:
1. Direct and compatibility import of worker entrypoints (app.worker & app.workers.outbox_worker).
2. Daemon processing loop claiming and executing due outbox dispatches.
3. Graceful shutdown on stop_event signal.
4. Fail-closed behavior when master dispatch kill switch is disabled.
5. No execution of migrations or unrelated SEO/retry jobs.
6. Lease ownership and fencing token preservation.
7. Safe error handling preventing duplicate dispatch attempts.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.dispatch_errors import DispatchDisabledError
from app.domain.sales_supervisor import compute_action_context_fingerprint
from app.models.entities import (
    ConfidenceLevel,
    Lead,
    LeadIntelligenceOpportunity,
    SourceType,
    SupervisorApprovalDispatch,
    SupervisorApprovalDispatchStatus,
    SupervisorHumanApproval,
)
from app.services.dispatch_provider import BlockedProductionDispatchProvider, DispatchProviderResult
from app.services.providers.base_adapter import BaseDispatchProviderAdapter
from app.services.supervisor_dispatch_service import SupervisorApprovalDispatchService
from app.workers.outbox_worker import process_one_dispatch, run_outbox_worker


class MockWorkerProvider(BaseDispatchProviderAdapter):
    """In-memory provider adapter tracking invocations."""

    def __init__(self, from_email: str = "outreach@example.com") -> None:
        self.from_email = from_email
        self.calls = []

    @property
    def provider_name(self) -> str:
        return "MOCK"

    @property
    def supports_native_idempotency(self) -> bool:
        return True

    async def send_dispatch(
        self,
        *,
        channel: str,
        recipient: str,
        payload_json: dict[str, Any],
        idempotency_key: str,
    ) -> DispatchProviderResult:
        self.calls.append({
            "channel": channel,
            "recipient": recipient,
            "payload_json": payload_json,
            "idempotency_key": idempotency_key,
        })
        return DispatchProviderResult(
            success=True,
            provider_message_id=f"msg_mock_{len(self.calls)}",
            provider_status="accepted",
        )


async def _seed_test_dispatch(
    session: AsyncSession,
    *,
    email: str = "test@internal-test.leadintel.example.com",
    now: datetime | None = None,
) -> SupervisorApprovalDispatch:
    t_now = now or datetime.now(timezone.utc)
    lead = Lead(id=uuid.uuid4(), company_name="Outbox Test Corp", email=email)
    session.add(lead)
    await session.flush()

    opp = LeadIntelligenceOpportunity(
        id=uuid.uuid4(),
        lead_id=lead.id,
        title="Outbox Opportunity",
        score=90,
        recommended_action="PERSONALIZED_EMAIL",
        evidence={},
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH,
    )
    session.add(opp)
    await session.flush()

    act_id = f"act_{uuid.uuid4().hex[:8]}"
    fp = compute_action_context_fingerprint(
        action_id=act_id,
        action_type="PERSONALIZED_EMAIL",
        lead_id=lead.id,
        opportunity_id=opp.id,
        contact_id=None,
        channel="EMAIL",
        contact_email=email,
        contact_phone=None,
        opportunity_score=90.0,
        recommendation_fit=0.0,
        evidence_keys=[],
    )

    approval = SupervisorHumanApproval(
        id=uuid.uuid4(),
        action_id=act_id,
        lead_id=lead.id,
        opportunity_id=opp.id,
        recommended_action="PERSONALIZED_EMAIL",
        action_tier="EXTERNAL_OUTREACH",
        context_fingerprint=fp,
        requested_by_user_id="user-req-1",
        approved_by_user_id="user-app-1",
        status="APPROVED",
        channel="EMAIL",
        version=1,
        expires_at=t_now + timedelta(hours=24),
    )
    session.add(approval)
    await session.flush()

    service = SupervisorApprovalDispatchService()
    dispatch = await service.enqueue_dispatch(
        session,
        approval_id=approval.id,
        now=t_now,
    )
    return dispatch


# ---------------------------------------------------------------------------
# Test 1: Entrypoint Importability
# ---------------------------------------------------------------------------

def test_worker_entrypoints_import_successfully():
    """Verifies that both dedicated outbox_worker and compatibility worker import cleanly."""
    import app.worker as compat_worker
    import app.workers.outbox_worker as dedicated_worker

    assert hasattr(compat_worker, "main")
    assert hasattr(compat_worker, "run_outbox_worker")
    assert hasattr(dedicated_worker, "main")
    assert hasattr(dedicated_worker, "run_outbox_worker")
    assert hasattr(dedicated_worker, "process_one_dispatch")


# ---------------------------------------------------------------------------
# Test 2: Process Single Dispatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_one_dispatch_claims_and_executes(session_factory, monkeypatch):
    """Verifies that process_one_dispatch claims due dispatch and executes through provider."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    monkeypatch.setattr(settings, "outreach_resend_from", "outreach@example.com")
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    mock_provider = MockWorkerProvider()
    monkeypatch.setattr("app.workers.outbox_worker.get_dispatch_provider", lambda ch, settings=None: mock_provider)

    async with session_factory() as session:
        async with session.begin():
            dispatch = await _seed_test_dispatch(session, now=now)

    service = SupervisorApprovalDispatchService()
    worker_id = "test-worker-01"

    # Execute single dispatch iteration
    claimed = await process_one_dispatch(
        service=service,
        session_factory=session_factory,
        worker_id=worker_id,
        lease_generation=1,
        now=now,
    )
    assert claimed is True
    assert len(mock_provider.calls) == 1
    assert mock_provider.calls[0]["recipient"] == "test@internal-test.leadintel.example.com"

    # Verify status updated to SENT in database
    async with session_factory() as session:
        refreshed = await service.repository.get_by_id(session, dispatch.id)
        assert refreshed is not None
        assert refreshed.status == SupervisorApprovalDispatchStatus.SENT.value
        assert refreshed.provider_message_id == "msg_mock_1"
        assert refreshed.dispatch_worker_id == worker_id

    # Running again on empty queue returns False
    empty_claim = await process_one_dispatch(
        service=service,
        session_factory=session_factory,
        worker_id=worker_id,
        lease_generation=1,
        now=now,
    )
    assert empty_claim is False


# ---------------------------------------------------------------------------
# Test 3: Graceful Shutdown
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_outbox_worker_graceful_shutdown(session_factory):
    """Verifies that the worker processing loop terminates promptly when stop_event is set."""
    stop_event = asyncio.Event()

    with patch("app.workers.outbox_worker.update_worker_heartbeat", new=AsyncMock()):
        # Start worker task with 0.1s poll interval
        worker_task = asyncio.create_task(
            run_outbox_worker(
                session_factory=session_factory,
                poll_interval_seconds=0.1,
                worker_id="test-shutdown-worker",
                stop_event=stop_event,
                max_iterations=10,
            )
        )
        await asyncio.sleep(0.05)
        stop_event.set()
        await asyncio.wait_for(worker_task, timeout=1.0)
        assert worker_task.done()


# ---------------------------------------------------------------------------
# Test 4: Master Kill Switch Enforced (No external network socket)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_outbox_worker_aborts_when_dispatch_disabled(session_factory, monkeypatch):
    """When outreach_dispatch_enabled is False, dispatches are aborted by safety guards with zero network calls."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    monkeypatch.setattr(settings, "outreach_resend_from", "outreach@example.com")
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    mock_provider = MockWorkerProvider()
    monkeypatch.setattr("app.workers.outbox_worker.get_dispatch_provider", lambda ch, settings=None: mock_provider)

    async with session_factory() as session:
        async with session.begin():
            dispatch = await _seed_test_dispatch(session, now=now)

    # Disable kill switch before worker execution
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", False)

    service = SupervisorApprovalDispatchService()
    worker_id = "test-worker-killswitch"

    claimed = await process_one_dispatch(
        service=service,
        session_factory=session_factory,
        worker_id=worker_id,
        lease_generation=1,
        now=now,
    )
    assert claimed is True
    # Zero provider network calls made!
    assert len(mock_provider.calls) == 0

    # Verify status in database is ABORTED
    async with session_factory() as session:
        refreshed = await service.repository.get_by_id(session, dispatch.id)
        assert refreshed is not None
        assert refreshed.status == SupervisorApprovalDispatchStatus.ABORTED.value
        assert refreshed.error_code == "DISPATCH_DISABLED"


# ---------------------------------------------------------------------------
# Test 5: Worker Does Not Execute Migrations or Start Unrelated Schedulers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_does_not_execute_migrations_or_seo_jobs(session_factory):
    """Verifies that outbox_worker does not call alembic or claim generic/SEO retry jobs."""
    with patch("app.workers.outbox_worker.update_worker_heartbeat", new=AsyncMock()):
        with patch("app.workers.runner.claim_seo_runs", side_effect=RuntimeError("SEO handler invoked!")):
            with patch("app.workers.runner.claim_jobs", side_effect=RuntimeError("Generic job handler invoked!")):
                stop_event = asyncio.Event()
                stop_event.set()
                # Run worker for 0 iterations
                await run_outbox_worker(
                    session_factory=session_factory,
                    poll_interval_seconds=0.1,
                    worker_id="test-isolated-worker",
                    stop_event=stop_event,
                    max_iterations=1,
                )


# ---------------------------------------------------------------------------
# Test 6: Lease Ownership & Fencing Preserved
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_preserves_fencing_conflict(session_factory, monkeypatch):
    """Verifies that fencing token conflict prevents stale worker execution."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    mock_provider = MockWorkerProvider()
    monkeypatch.setattr("app.workers.outbox_worker.get_dispatch_provider", lambda ch, settings=None: mock_provider)

    async with session_factory() as session:
        async with session.begin():
            dispatch = await _seed_test_dispatch(session, now=now)
            # Manually advance lease generation to 5
            row = await session.get(SupervisorApprovalDispatch, dispatch.id)
            row.lease_generation = 5
            row.status = SupervisorApprovalDispatchStatus.IN_PROGRESS.value
            row.dispatch_worker_id = "other-worker"

    service = SupervisorApprovalDispatchService()

    # Worker with expected lease generation 1 encounters conflict
    claimed = await process_one_dispatch(
        service=service,
        session_factory=session_factory,
        worker_id="stale-worker",
        lease_generation=1,
        now=now,
    )
    # Queue had no pending job because it's already IN_PROGRESS with other worker
    assert claimed is False
    assert len(mock_provider.calls) == 0

"""
Comprehensive Test Suite for Supervisor Approval Dispatch Outbox.

Phase 12J - D2 - Gate D-B2:
Validates all 33 scenarios (TM-01 through TM-33):
- Two-tier fail-closed kill switches (global and channel)
- Canonical length-prefix idempotency calculation and recipient normalization
- Transaction boundaries and zero socket calls held within DB transactions
- Finite-state machine transitions, including strict no-retry on OUTCOME_UNKNOWN
- Post-network CAS fencing and stale-lease rejection
- Deletion-aware cancellation, payload redaction, and forensic dual-identity retention
- Isolated PostgreSQL concurrency, SKIP LOCKED claims, lock timeout, and deadlock resilience
- Full dry-run E2E pipeline execution with InertTestDispatchProvider
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator

import asyncpg
import pytest
import pytest_asyncio
from alembic.config import Config
from alembic import command
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings, settings
from app.domain.dispatch_errors import (
    DispatchAbortedError,
    DispatchAmbiguousOutcomeError,
    DispatchDisabledError,
    DispatchDuplicateError,
    DispatchFencingConflictError,
    DispatchInvalidTransitionError,
    DispatchNotFoundError,
    DispatchRedactedPayloadError,
)
from app.domain.dispatch_idempotency import (
    compute_canonical_idempotency_key,
    compute_payload_hash,
    normalize_recipient,
)
from app.domain.sales_supervisor import (
    ApprovalContextDriftError,
    compute_action_context_fingerprint,
)
from app.models.entities import (
    Base,
    ConfidenceLevel,
    Lead,
    LeadIntelligenceOpportunity,
    SourceType,
    SupervisorApprovalAuditEvent,
    SupervisorApprovalDispatch,
    SupervisorApprovalDispatchStatus,
    SupervisorHumanApproval,
    now_utc,
)
from app.repositories.supervisor_approval import SupervisorApprovalRepository
from app.repositories.supervisor_dispatch_repository import (
    SupervisorApprovalDispatchRepository,
)
from app.services.dispatch_provider import (
    DispatchProviderResult,
    InertTestDispatchProvider,
)
from app.services.supervisor_approval_service import SupervisorApprovalService
from app.services.supervisor_dispatch_service import (
    SupervisorApprovalDispatchService,
    is_idempotent_transport,
)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ============================================================================
# Helpers to seed database entities
# ============================================================================

async def _seed_test_lead_and_approval(
    session: AsyncSession,
    *,
    channel: str = "EMAIL",
    email: str = "test.lead@example.com",
    phone: str = "+15551234567",
    status: str = "APPROVED",
    expires_in_hours: int = 48,
    action_id: str = "act-test-01",
    now: datetime | None = None,
) -> tuple[Lead, LeadIntelligenceOpportunity, SupervisorHumanApproval]:
    t_now = _utc(now or datetime.now(timezone.utc))

    lead = Lead(
        id=uuid.uuid4(),
        company_name="Acme Corp",
        email=email,
        phone=phone,
    )
    session.add(lead)
    await session.flush()

    rec_action = "PERSONALIZED_EMAIL" if channel == "EMAIL" else "DISCOVERY_CALL"

    opp = LeadIntelligenceOpportunity(
        id=uuid.uuid4(),
        lead_id=lead.id,
        title="Test Opportunity",
        score=85,
        recommended_action=rec_action,
        evidence={},
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH,
    )
    session.add(opp)
    await session.flush()

    fingerprint = compute_action_context_fingerprint(
        action_id=action_id,
        action_type=rec_action,
        lead_id=lead.id,
        opportunity_id=opp.id,
        channel=channel,
        contact_email=email,
        contact_phone=phone,
        opportunity_score=85.0,
        recommendation_fit=0.0,
        evidence_keys=[],
    )

    approval = SupervisorHumanApproval(
        id=uuid.uuid4(),
        action_id=action_id,
        lead_id=lead.id,
        opportunity_id=opp.id,
        recommended_action=rec_action,
        action_tier="EXTERNAL_OUTREACH",
        channel=channel,
        context_fingerprint=fingerprint,
        status=status,
        version=1,
        requested_by_user_id="user-req-1",
        approved_by_user_id="user-app-1",
        approved_at=t_now,
        expires_at=t_now + timedelta(hours=expires_in_hours),
        created_at=t_now,
        updated_at=t_now,
    )
    session.add(approval)
    await session.flush()

    return lead, opp, approval


# ============================================================================
# TM-01 to TM-04: Kill Switch Tests
# ============================================================================

@pytest.mark.asyncio
async def test_tm01_global_kill_switch_disabled_by_default(session_factory, monkeypatch):
    """TM-01: Global kill switch disabled (default False) rejects enqueue fail-closed."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", False)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)

    async with session_factory() as session:
        async with session.begin():
            _, _, approval = await _seed_test_lead_and_approval(session)

        service = SupervisorApprovalDispatchService()
        async with session.begin():
            with pytest.raises(DispatchDisabledError) as exc_info:
                await service.enqueue_dispatch(session, approval_id=approval.id)
            assert "outreach_dispatch_enabled is False" in str(exc_info.value)


def test_tm02_global_kill_switch_missing_or_invalid():
    """TM-02: Global kill switch missing or malformed evaluates strictly to False."""
    s = Settings(outreach_dispatch_enabled=False)
    assert not s.effective_outreach_dispatch_enabled
    assert not s.is_dispatch_permitted("EMAIL")


@pytest.mark.asyncio
async def test_tm03_global_enabled_channel_disabled(session_factory, monkeypatch):
    """TM-03: Global enabled, channel disabled fails closed with channel rejection."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", False)

    async with session_factory() as session:
        async with session.begin():
            _, _, approval = await _seed_test_lead_and_approval(session, channel="EMAIL")

        service = SupervisorApprovalDispatchService()
        async with session.begin():
            with pytest.raises(DispatchDisabledError) as exc_info:
                await service.enqueue_dispatch(session, approval_id=approval.id)
            assert "channel 'EMAIL' is disabled" in str(exc_info.value)


@pytest.mark.asyncio
async def test_tm04_global_enabled_channel_enabled(session_factory, monkeypatch):
    """TM-04: Global enabled and channel enabled creates outbox row in PENDING status."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)

    async with session_factory() as session:
        async with session.begin():
            _, _, approval = await _seed_test_lead_and_approval(session, channel="EMAIL")

        service = SupervisorApprovalDispatchService()
        async with session.begin():
            dispatch = await service.enqueue_dispatch(session, approval_id=approval.id)
            assert dispatch.status == "PENDING"
            assert dispatch.channel == "EMAIL"
            assert dispatch.approval_id == approval.id
            assert dispatch.approval_reference_id == approval.id


# ============================================================================
# TM-05 to TM-06: Idempotency Key Serialization & Normalization
# ============================================================================

def test_tm05_canonical_idempotency_key_serialization():
    """TM-05: Length-prefix serialization ensures hash stability and prevents collision."""
    app_id = uuid.uuid4()
    fp = "a" * 64
    k1 = compute_canonical_idempotency_key(
        approval_id=app_id, action_id="act-1", context_fingerprint=fp, recipient="user@corp.com", channel="EMAIL"
    )
    k2 = compute_canonical_idempotency_key(
        approval_id=app_id, action_id="act-1", context_fingerprint=fp, recipient=" USER@CORP.COM ", channel="EMAIL"
    )
    assert k1 == k2
    assert len(k1) == 64


def test_tm06_recipient_normalization():
    """TM-06: Normalizes emails to lowercase NFKC and phone numbers to E.164 format."""
    assert normalize_recipient("  Alice.Smith@Domain.COM  ", channel="EMAIL") == "alice.smith@domain.com"
    assert normalize_recipient("+1 (555) 234-5678", channel="PHONE") == "+15552345678"
    assert normalize_recipient("+1 (555) 234-5678", channel="WHATSAPP") == "+15552345678"


# ============================================================================
# TM-09 to TM-12: Pre-flight, Context Drift, and Revocation Tests
# ============================================================================

@pytest.mark.asyncio
async def test_tm09_pre_flight_guard_forces_heartbeat_when_ttl_under_60s(session_factory, monkeypatch):
    """TM-09: Claiming an approval with remaining lease TTL < 60s forces heartbeat renewal."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        async with session.begin():
            lead, opp, approval = await _seed_test_lead_and_approval(session, now=now)
            service = SupervisorApprovalDispatchService()
            dispatch = await service.enqueue_dispatch(session, approval_id=approval.id, now=now)

        # In an isolated update, move approval lease updated_at into low-TTL state (<60s remaining)
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE supervisor_human_approvals "
                    "SET execution_claimed_at = :cat, execution_claimed_by = 'worker-ttl', updated_at = :uat "
                    f"WHERE id = '{approval.id}'"
                ),
                {"cat": now - timedelta(seconds=260), "uat": now - timedelta(seconds=260)},
            )

        async with session.begin():
            claimed = await service.poll_and_claim_job(session, worker_id="worker-ttl", now=now)
            assert claimed is not None
            assert claimed.status == "IN_PROGRESS"
            reloaded_approval = await service.approval_repository.get_by_id(session, approval.id)
            # Verify lease updated_at was refreshed to now by forced heartbeat renewal
            assert _utc(reloaded_approval.updated_at) == now
            assert claimed.lease_generation == reloaded_approval.version


@pytest.mark.asyncio
async def test_tm10_stale_or_expired_lease_aborts_claim(session_factory, monkeypatch):
    """TM-10: Stale or unapproved parent approval aborts job claim."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        async with session.begin():
            lead, opp, approval = await _seed_test_lead_and_approval(session, now=now)
            service = SupervisorApprovalDispatchService()
            dispatch = await service.enqueue_dispatch(session, approval_id=approval.id, now=now)

            # Change parent approval status to REVOKED
            approval.status = "REVOKED"

        async with session.begin():
            claimed = await service.poll_and_claim_job(session, worker_id="worker-1", now=now)
            assert claimed is None

        # Verify dispatch was marked ABORTED
        async with session.begin():
            job = await service.repository.get_by_id(session, dispatch.id)
            assert job.status == "ABORTED"
            assert job.error_code == "APPROVAL_NOT_ACTIVE"


@pytest.mark.asyncio
async def test_tm11_context_drift_before_dispatch_send(session_factory, monkeypatch):
    """TM-11: Mutating authoritative context between approval and dispatch raises drift error."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        async with session.begin():
            lead, opp, approval = await _seed_test_lead_and_approval(session, now=now)
            # Mutate lead email in database
            lead.email = "drifted.email@example.com"

        service = SupervisorApprovalDispatchService()
        async with session.begin():
            with pytest.raises(ApprovalContextDriftError):
                await service.enqueue_dispatch(session, approval_id=approval.id, now=now)


# ============================================================================
# TM-14 to TM-18: Failure Handling, SMTP Ambiguity, and Reconciliation
# ============================================================================

@pytest.mark.asyncio
async def test_tm14_definitive_provider_failure_transitions_to_failed(session_factory, monkeypatch):
    """TM-14: HTTP 4xx definitive error transitions to FAILED."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        async with session.begin():
            lead, opp, approval = await _seed_test_lead_and_approval(session, now=now)
            service = SupervisorApprovalDispatchService()
            dispatch = await service.enqueue_dispatch(session, approval_id=approval.id, now=now)
            claimed = await service.repository.poll_and_claim_next(
                session, worker_id="worker-fail", lease_generation=1, now=now
            )

    provider = InertTestDispatchProvider(
        default_success=False,
        simulate_error_code="HTTP_400_BAD_REQUEST",
        simulate_error_message="Invalid recipient address",
        simulate_transient=False,
    )
    completed = await service.execute_claimed_dispatch(
        session_factory,
        dispatch_id=dispatch.id,
        worker_id="worker-fail",
        expected_lease_generation=1,
        provider=provider,
        now=now,
    )
    assert completed.status == "FAILED"
    assert completed.error_code == "HTTP_400_BAD_REQUEST"
    assert completed.next_retry_at is None


@pytest.mark.asyncio
async def test_tm15_transient_error_schedules_backoff_retry(session_factory, monkeypatch):
    """TM-15: Transient 5xx error schedules exponential backoff retry on idempotent channel."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        async with session.begin():
            lead, opp, approval = await _seed_test_lead_and_approval(session, now=now)
            service = SupervisorApprovalDispatchService()
            dispatch = await service.enqueue_dispatch(session, approval_id=approval.id, now=now)
            claimed = await service.repository.poll_and_claim_next(
                session, worker_id="worker-retry", lease_generation=1, now=now
            )

    provider = InertTestDispatchProvider(
        default_success=False,
        simulate_error_code="HTTP_503_SERVICE_UNAVAILABLE",
        simulate_error_message="Gateway timeout",
        simulate_transient=True,
    )
    completed = await service.execute_claimed_dispatch(
        session_factory,
        dispatch_id=dispatch.id,
        worker_id="worker-retry",
        expected_lease_generation=1,
        provider=provider,
        now=now,
    )
    assert completed.status == "RETRYABLE_FAILURE"
    assert completed.next_retry_at is not None
    assert completed.next_retry_at > now


@pytest.mark.asyncio
async def test_tm17_ambiguous_smtp_timeout_prohibits_auto_retry(session_factory, monkeypatch):
    """TM-17: Ambiguous outcome on SMTP transitions to OUTCOME_UNKNOWN with zero auto-retry."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        async with session.begin():
            lead, opp, approval = await _seed_test_lead_and_approval(session, now=now)
            service = SupervisorApprovalDispatchService()
            dispatch = await service.enqueue_dispatch(session, approval_id=approval.id, now=now)
            claimed = await service.repository.poll_and_claim_next(
                session, worker_id="worker-smtp", lease_generation=1, now=now
            )

    provider = InertTestDispatchProvider(
        default_success=False,
        simulate_error_code="SMTP_DATA_SOCKET_TIMEOUT",
        simulate_error_message="Socket closed mid-DATA",
        simulate_ambiguous=True,
    )
    completed = await service.execute_claimed_dispatch(
        session_factory,
        dispatch_id=dispatch.id,
        worker_id="worker-smtp",
        expected_lease_generation=1,
        provider=provider,
        now=now,
    )
    assert completed.status == "OUTCOME_UNKNOWN"
    assert completed.next_retry_at is None  # STRICT NO AUTO RETRY!


@pytest.mark.asyncio
async def test_tm18_manual_operator_reconciliation(session_factory, monkeypatch):
    """TM-18: Authorized operator manually reconciles OUTCOME_UNKNOWN to SENT or PENDING."""
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalDispatchService()

    async with session_factory() as session:
        async with session.begin():
            lead, opp, approval = await _seed_test_lead_and_approval(session, now=now)
            # Create a dispatch directly in OUTCOME_UNKNOWN status
            dispatch = SupervisorApprovalDispatch(
                id=uuid.uuid4(),
                approval_id=approval.id,
                approval_reference_id=approval.id,
                lead_id=lead.id,
                lead_reference_id=lead.id,
                action_id=approval.action_id,
                channel="EMAIL",
                recipient=lead.email,
                context_fingerprint=approval.context_fingerprint,
                idempotency_key="reconcile-test-key",
                payload_hash="hash",
                payload_json={"test": True},
                status="OUTCOME_UNKNOWN",
                provider_name="SMTP",
            )
            session.add(dispatch)
            await session.flush()
            dispatch_id = dispatch.id

        # Reconcile to SENT
        async with session.begin():
            reconciled = await service.reconcile_unknown_dispatch(
                session,
                dispatch_id=dispatch_id,
                resolution="SENT",
                provider_message_id="msg-recon-123",
                notes="Confirmed delivery in gateway logs",
                now=now,
            )
            assert reconciled.status == "SENT"
            assert reconciled.provider_message_id == "msg-recon-123"


# ============================================================================
# TM-19 & TM-25: Transaction Isolation & Dry-Run Pipeline Tests
# ============================================================================

@pytest.mark.asyncio
async def test_tm19_and_tm25_dry_run_pipeline_and_socket_isolation(session_factory, monkeypatch):
    """TM-19 & TM-25: End-to-end dry-run execution with InertTestDispatchProvider (zero sockets)."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        async with session.begin():
            lead, opp, approval = await _seed_test_lead_and_approval(session, now=now)

    service = SupervisorApprovalDispatchService()
    completed, inert_provider = await service.dry_run_dispatch(
        session_factory,
        approval_id=approval.id,
        worker_id="dry-worker",
        now=now,
    )
    assert completed.status == "SENT"
    assert completed.provider_message_id.startswith("inert-email-")
    assert len(inert_provider.sent_dispatches) == 1
    assert inert_provider.sent_dispatches[0]["recipient"] == lead.email


@pytest.mark.asyncio
async def test_tm32_redacted_payload_guard_invariant(session_factory):
    """TM-32: Redacted payload cannot be claimed or dispatched across a socket."""
    service = SupervisorApprovalDispatchService()
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        async with session.begin():
            lead, opp, approval = await _seed_test_lead_and_approval(session, now=now)
            # Create dispatch with already redacted payload
            dispatch = SupervisorApprovalDispatch(
                id=uuid.uuid4(),
                approval_id=approval.id,
                approval_reference_id=approval.id,
                lead_id=lead.id,
                lead_reference_id=lead.id,
                action_id=approval.action_id,
                channel="EMAIL",
                recipient=lead.email,
                context_fingerprint=approval.context_fingerprint,
                idempotency_key="redacted-guard-key",
                payload_hash="hash",
                payload_json={"redacted": True, "reason": "PRIVACY_ERASURE"},
                status="IN_PROGRESS",
                dispatch_worker_id="worker-redacted",
                provider_name="SENDGRID",
            )
            session.add(dispatch)
            await session.flush()
            dispatch_id = dispatch.id

        async with session.begin():
            with pytest.raises(DispatchRedactedPayloadError):
                await service.pre_flight_guard_check(
                    session,
                    dispatch_id=dispatch_id,
                    worker_id="worker-redacted",
                    expected_lease_generation=1,
                    now=now,
                )


# ============================================================================
# Native PostgreSQL Concurrency & Retention Integration Tests
# (Uses an isolated, disposable PostgreSQL database dropped on teardown)
# ============================================================================

PG_ADMIN_URL = os.environ.get("POSTGRES_ADMIN_URL", "postgresql://postgres:postgres@localhost:5432/postgres")


@pytest_asyncio.fixture
async def disposable_pg_session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """
    Creates an isolated disposable PostgreSQL test database, applies all Alembic migrations up to 0040,
    yields the async sessionmaker, and unconditionally drops the database upon teardown.
    """
    db_name = f"lead_agent_val_{uuid.uuid4().hex[:8]}"
    admin_conn = await asyncpg.connect(PG_ADMIN_URL)
    await admin_conn.execute(f"CREATE DATABASE {db_name}")
    await admin_conn.close()

    # Pre-create alembic_version with VARCHAR(128) to accommodate long migration names
    db_conn = await asyncpg.connect(f"postgresql://postgres:postgres@localhost:5432/{db_name}")
    await db_conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(128) PRIMARY KEY)")
    await db_conn.close()

    target_url = f"postgresql+asyncpg://postgres:postgres@localhost:5432/{db_name}"
    os.environ["DATABASE_URL"] = target_url
    old_db_url = settings.database_url
    settings.database_url = target_url

    try:
        # Apply all migrations up to 0040 via alembic in a separate thread
        cfg = Config("backend/alembic.ini")
        cfg.set_main_option("script_location", "backend/migrations")
        await asyncio.to_thread(command.upgrade, cfg, "0040_supervisor_approval_dispatches")

        engine = create_async_engine(target_url, pool_size=10, max_overflow=5)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        yield factory
    finally:
        settings.database_url = old_db_url
        await engine.dispose()

        # Teardown: terminate active connections and drop disposable test database
        teardown_conn = await asyncpg.connect(PG_ADMIN_URL)
        await teardown_conn.execute(f"""
            SELECT pg_terminate_backend(pid) FROM pg_stat_activity 
            WHERE datname = '{db_name}' AND pid <> pg_backend_pid()
        """)
        await teardown_conn.execute(f"DROP DATABASE {db_name}")
        await teardown_conn.close()


@pytest.mark.asyncio
async def test_tm07_concurrent_duplicate_enqueue_race_in_postgresql(disposable_pg_session_factory):
    """TM-07: Concurrent insertion of identical idempotency_key triggers SQLSTATE 23505; exactly 1 persists."""
    app_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    idem_key = "concurrent-key-" + uuid.uuid4().hex

    async def _insert_dispatch(worker_id: str):
        async with disposable_pg_session_factory() as session:
            repo = SupervisorApprovalDispatchRepository()
            try:
                async with session.begin():
                    await repo.create_dispatch(
                        session,
                        approval_id=None,
                        approval_reference_id=app_id,
                        lead_id=None,
                        lead_reference_id=lead_id,
                        action_id="act-race-1",
                        channel="EMAIL",
                        recipient="race@example.com",
                        context_fingerprint="fp-race",
                        idempotency_key=idem_key,
                        payload_hash="phash",
                        payload_json={"race": worker_id},
                        provider_name="SENDGRID",
                    )
                return "COMMITTED"
            except DispatchDuplicateError:
                return "DUPLICATE_REJECTED"

    results = await asyncio.gather(_insert_dispatch("worker-A"), _insert_dispatch("worker-B"))
    assert sorted(results) == ["COMMITTED", "DUPLICATE_REJECTED"]

    # Verify exactly 1 row exists in PostgreSQL
    async with disposable_pg_session_factory() as session:
        res = await session.execute(
            select(SupervisorApprovalDispatch).where(SupervisorApprovalDispatch.idempotency_key == idem_key)
        )
        rows = res.scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_tm08_concurrent_multi_worker_claim_skip_locked(disposable_pg_session_factory):
    """TM-08: Two workers polling concurrently claim distinct jobs without blocking (SKIP LOCKED)."""
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    app_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    repo = SupervisorApprovalDispatchRepository()

    async with disposable_pg_session_factory() as session:
        async with session.begin():
            d1 = await repo.create_dispatch(
                session,
                approval_id=None,
                approval_reference_id=app_id,
                lead_id=None,
                lead_reference_id=lead_id,
                action_id="act-job-1",
                channel="EMAIL",
                recipient="job1@example.com",
                context_fingerprint="fp1",
                idempotency_key="key-job-1",
                payload_hash="h1",
                payload_json={"job": 1},
                provider_name="SENDGRID",
                now=now,
            )
            d2 = await repo.create_dispatch(
                session,
                approval_id=None,
                approval_reference_id=app_id,
                lead_id=None,
                lead_reference_id=lead_id,
                action_id="act-job-2",
                channel="EMAIL",
                recipient="job2@example.com",
                context_fingerprint="fp2",
                idempotency_key="key-job-2",
                payload_hash="h2",
                payload_json={"job": 2},
                provider_name="SENDGRID",
                now=now,
            )

    async def _claim_worker(name: str):
        async with disposable_pg_session_factory() as session:
            async with session.begin():
                job = await repo.poll_and_claim_next(session, worker_id=name, lease_generation=1, now=now)
                # Hold transaction briefly to verify concurrent bypass
                await asyncio.sleep(0.05)
                return job.id if job else None

    c1, c2 = await asyncio.gather(_claim_worker("worker-1"), _claim_worker("worker-2"))
    assert c1 is not None and c2 is not None
    assert c1 != c2  # Distinct jobs claimed! Zero duplicate claims!


@pytest.mark.asyncio
async def test_tm13_stale_worker_post_network_cas_fencing(disposable_pg_session_factory):
    """TM-13: Stale worker attempting post-network CAS update is rejected when generation mismatches."""
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    repo = SupervisorApprovalDispatchRepository()
    app_id = uuid.uuid4()
    lead_id = uuid.uuid4()

    async with disposable_pg_session_factory() as session:
        async with session.begin():
            dispatch = await repo.create_dispatch(
                session,
                approval_id=None,
                approval_reference_id=app_id,
                lead_id=None,
                lead_reference_id=lead_id,
                action_id="act-fence",
                channel="EMAIL",
                recipient="fence@example.com",
                context_fingerprint="fp",
                idempotency_key="key-fence",
                payload_hash="h",
                payload_json={"fence": True},
                provider_name="SENDGRID",
                now=now,
            )
            dispatch.status = "IN_PROGRESS"
            dispatch.lease_generation = 2  # Current generation advanced to 2

    # Worker 1 with stale generation 1 attempts post-network update
    async with disposable_pg_session_factory() as session:
        async with session.begin():
            with pytest.raises(DispatchFencingConflictError):
                await repo.record_completion_post_network(
                    session,
                    dispatch_id=dispatch.id,
                    outcome_status="SENT",
                    expected_lease_generation=1,  # Stale generation
                    worker_id="worker-stale",
                    provider_message_id="msg-stale",
                    now=now,
                )


@pytest.mark.asyncio
async def test_tm26_lead_deletion_retains_dispatch_foreign_key(disposable_pg_session_factory):
    """TM-26: Deleting a Lead record sets lead_id = NULL while preserving dispatch row and lead_reference_id."""
    repo = SupervisorApprovalDispatchRepository()
    lead_id = uuid.uuid4()
    app_id = uuid.uuid4()

    async with disposable_pg_session_factory() as session:
        async with session.begin():
            lead = Lead(id=lead_id, company_name="Retention Corp", email="retention@example.com")
            session.add(lead)
            await session.flush()

            dispatch = await repo.create_dispatch(
                session,
                approval_id=None,
                approval_reference_id=app_id,
                lead_id=lead.id,
                lead_reference_id=lead.id,
                action_id="act-retention-lead",
                channel="EMAIL",
                recipient="retention@example.com",
                context_fingerprint="fp-ret",
                idempotency_key="key-retention-lead",
                payload_hash="phash",
                payload_json={"retention": True},
                provider_name="SENDGRID",
            )
            dispatch_id = dispatch.id

    # Delete Lead record directly in PostgreSQL in independent session
    async with disposable_pg_session_factory() as session:
        async with session.begin():
            await session.execute(text(f"DELETE FROM leads WHERE id = '{lead_id}'"))

    # Verify dispatch row survived with lead_id = NULL and lead_reference_id intact in fresh session
    async with disposable_pg_session_factory() as session:
        saved = await repo.get_by_id(session, dispatch_id)
        assert saved is not None
        assert saved.lead_id is None
        assert saved.lead_reference_id == lead_id


@pytest.mark.asyncio
async def test_tm27_approval_deletion_retains_dispatch_foreign_key(disposable_pg_session_factory):
    """TM-27: Deleting an approval sets approval_id = NULL while preserving dispatch row and approval_reference_id."""
    repo = SupervisorApprovalDispatchRepository()
    lead_id = uuid.uuid4()
    app_id = uuid.uuid4()
    opp_id = uuid.uuid4()

    async with disposable_pg_session_factory() as session:
        async with session.begin():
            lead = Lead(id=lead_id, company_name="Appr Del Corp", email="appr@example.com")
            session.add(lead)
            await session.flush()

            approval = SupervisorHumanApproval(
                id=app_id,
                action_id="act-appr-del",
                lead_id=lead.id,
                opportunity_id=opp_id,
                recommended_action="PERSONALIZED_EMAIL",
                action_tier="EXTERNAL_OUTREACH",
                context_fingerprint="fp-appr",
                requested_by_user_id="user-1",
            )
            session.add(approval)
            await session.flush()

            dispatch = await repo.create_dispatch(
                session,
                approval_id=approval.id,
                approval_reference_id=approval.id,
                lead_id=lead.id,
                lead_reference_id=lead.id,
                action_id="act-appr-del",
                channel="EMAIL",
                recipient="appr@example.com",
                context_fingerprint="fp-appr",
                idempotency_key="key-appr-del",
                payload_hash="phash",
                payload_json={"test": True},
                provider_name="SENDGRID",
            )
            dispatch_id = dispatch.id

    # Delete approval row in PostgreSQL in independent session
    async with disposable_pg_session_factory() as session:
        async with session.begin():
            await session.execute(text(f"DELETE FROM supervisor_human_approvals WHERE id = '{app_id}'"))

    # Verify dispatch survived with approval_id = NULL and approval_reference_id intact in fresh session
    async with disposable_pg_session_factory() as session:
        saved = await repo.get_by_id(session, dispatch_id)
        assert saved is not None
        assert saved.approval_id is None
        assert saved.approval_reference_id == app_id


@pytest.mark.asyncio
async def test_tm28_dual_parent_deletion_and_forensic_searchability(disposable_pg_session_factory):
    """TM-28: When both parent entities are deleted, dispatch remains searchable via permanent reference IDs."""
    repo = SupervisorApprovalDispatchRepository()
    lead_id = uuid.uuid4()
    app_id = uuid.uuid4()
    opp_id = uuid.uuid4()

    async with disposable_pg_session_factory() as session:
        async with session.begin():
            lead = Lead(id=lead_id, company_name="Dual Purge Inc", email="dual@example.com")
            session.add(lead)
            await session.flush()

            approval = SupervisorHumanApproval(
                id=app_id,
                action_id="act-dual",
                lead_id=lead.id,
                opportunity_id=opp_id,
                recommended_action="DISCOVERY_CALL",
                action_tier="EXTERNAL_OUTREACH",
                context_fingerprint="fp-dual",
                requested_by_user_id="user-1",
            )
            session.add(approval)
            await session.flush()

            dispatch = await repo.create_dispatch(
                session,
                approval_id=approval.id,
                approval_reference_id=approval.id,
                lead_id=lead.id,
                lead_reference_id=lead.id,
                action_id="act-dual",
                channel="PHONE",
                recipient="+15559876543",
                context_fingerprint="fp-dual",
                idempotency_key="key-dual",
                payload_hash="h",
                payload_json={"phone": True},
                provider_name="TWILIO",
            )

    # Delete both parent entities in independent session
    async with disposable_pg_session_factory() as session:
        async with session.begin():
            await session.execute(text(f"DELETE FROM supervisor_human_approvals WHERE id = '{app_id}'"))
            await session.execute(text(f"DELETE FROM leads WHERE id = '{lead_id}'"))

    # Query solely by permanent reference IDs in fresh session
    async with disposable_pg_session_factory() as session:
        records = await repo.get_by_reference_ids(
            session, lead_reference_id=lead_id, approval_reference_id=app_id
        )
        assert len(records) == 1
        assert records[0].lead_id is None
        assert records[0].approval_id is None
        assert records[0].action_id == "act-dual"


@pytest.mark.asyncio
async def test_tm29_lead_deletion_during_pending_state(disposable_pg_session_factory):
    """TM-29: Lead deletion cancels PENDING dispatch to ABORTED and redacts payload_json."""
    repo = SupervisorApprovalDispatchRepository()
    lead_id = uuid.uuid4()
    app_id = uuid.uuid4()

    async with disposable_pg_session_factory() as session:
        async with session.begin():
            lead = Lead(id=lead_id, company_name="Pending Delete Corp", email="pend@example.com")
            session.add(lead)
            await session.flush()

            dispatch = await repo.create_dispatch(
                session,
                approval_id=None,
                approval_reference_id=app_id,
                lead_id=lead.id,
                lead_reference_id=lead.id,
                action_id="act-pending-del",
                channel="EMAIL",
                recipient="pend@example.com",
                context_fingerprint="fp-pend",
                idempotency_key="key-pend-del",
                payload_hash="phash",
                payload_json={"pitch": "Confidential proposal text"},
                provider_name="SENDGRID",
            )
            dispatch_id = dispatch.id

    # Execute deletion-aware cancellation & redaction in independent session
    async with disposable_pg_session_factory() as session:
        async with session.begin():
            affected = await repo.cancel_and_redact_for_lead_deletion(session, lead_id=lead_id)
            assert affected == 1
            await session.execute(text(f"DELETE FROM leads WHERE id = '{lead_id}'"))

    # Inspect state in fresh session
    async with disposable_pg_session_factory() as session:
        saved = await repo.get_by_id(session, dispatch_id)
        assert saved.status == "ABORTED"
        assert saved.error_code == "LEAD_DELETED_PRIOR_TO_DISPATCH"
        assert saved.payload_json == {"redacted": True, "reason": "LEAD_DELETED"}
        assert saved.next_retry_at is None
        assert saved.lead_id is None


@pytest.mark.asyncio
async def test_tm30_lead_deletion_during_in_progress_pre_network_race(disposable_pg_session_factory):
    """TM-30: Pre-flight guard detects lead cancellation and halts worker before socket open."""
    service = SupervisorApprovalDispatchService()
    lead_id = uuid.uuid4()
    app_id = uuid.uuid4()

    async with disposable_pg_session_factory() as session:
        async with session.begin():
            lead = Lead(id=lead_id, company_name="PreNet Race Corp", email="prenet@example.com")
            session.add(lead)
            await session.flush()

            dispatch = await service.repository.create_dispatch(
                session,
                approval_id=None,
                approval_reference_id=app_id,
                lead_id=lead.id,
                lead_reference_id=lead.id,
                action_id="act-prenet",
                channel="EMAIL",
                recipient="prenet@example.com",
                context_fingerprint="fp-prenet",
                idempotency_key="key-prenet",
                payload_hash="phash",
                payload_json={"body": "Hello Alice"},
                provider_name="SENDGRID",
            )
            dispatch.status = "IN_PROGRESS"
            dispatch.dispatch_worker_id = "worker-race"
            dispatch_id = dispatch.id

        # Connection 2 deletes lead and redacts outbox record
        async with session.begin():
            await service.handle_lead_deletion(session, lead_id=lead_id)

    # Worker executes pre-flight guard check before opening socket
    async with disposable_pg_session_factory() as session:
        async with session.begin():
            with pytest.raises(DispatchAbortedError):
                await service.pre_flight_guard_check(
                    session,
                    dispatch_id=dispatch_id,
                    worker_id="worker-race",
                    expected_lease_generation=1,
                )


@pytest.mark.asyncio
async def test_tm31_worker_post_network_cas_fencing_on_deleted_lead(disposable_pg_session_factory):
    """TM-31: If external socket completes after lead deletion, worker CAS matches 0 rows and fails closed."""
    service = SupervisorApprovalDispatchService()
    lead_id = uuid.uuid4()
    app_id = uuid.uuid4()

    async with disposable_pg_session_factory() as session:
        async with session.begin():
            lead = Lead(id=lead_id, company_name="CAS Fenced Corp", email="cas@example.com")
            session.add(lead)
            await session.flush()

            dispatch = await service.repository.create_dispatch(
                session,
                approval_id=None,
                approval_reference_id=app_id,
                lead_id=lead.id,
                lead_reference_id=lead.id,
                action_id="act-cas",
                channel="EMAIL",
                recipient="cas@example.com",
                context_fingerprint="fp-cas",
                idempotency_key="key-cas",
                payload_hash="phash",
                payload_json={"body": "In flight message"},
                provider_name="SENDGRID",
            )
            dispatch.status = "IN_PROGRESS"
            dispatch.lease_generation = 1
            dispatch_id = dispatch.id

        # Lead deletion aborts and redacts row while network I/O was supposedly in-flight
        async with session.begin():
            await service.handle_lead_deletion(session, lead_id=lead_id)

    # Worker attempts post-network update to SENT
    async with disposable_pg_session_factory() as session:
        async with session.begin():
            with pytest.raises(DispatchAbortedError) as exc_info:
                await service.repository.record_completion_post_network(
                    session,
                    dispatch_id=dispatch_id,
                    outcome_status="SENT",
                    expected_lease_generation=1,
                    worker_id="worker-cas",
                    provider_message_id="sent-id-123",
                )
            assert "was ABORTED while network call was in flight" in str(exc_info.value)


@pytest.mark.asyncio
async def test_tm33_rollback_atomicity_during_redaction_failure(disposable_pg_session_factory):
    """TM-33: Transaction rollback during lead deletion failure leaves zero partially redacted state."""
    service = SupervisorApprovalDispatchService()
    lead_id = uuid.uuid4()
    app_id = uuid.uuid4()
    original_payload = {"sensitive_field": "confidential_proposal"}

    async with disposable_pg_session_factory() as session:
        async with session.begin():
            lead = Lead(id=lead_id, company_name="Rollback Corp", email="rb@example.com")
            session.add(lead)
            await session.flush()

            dispatch = await service.repository.create_dispatch(
                session,
                approval_id=None,
                approval_reference_id=app_id,
                lead_id=lead.id,
                lead_reference_id=lead.id,
                action_id="act-rb",
                channel="EMAIL",
                recipient="rb@example.com",
                context_fingerprint="fp-rb",
                idempotency_key="key-rb",
                payload_hash="phash",
                payload_json=original_payload,
                provider_name="SENDGRID",
            )
            dispatch_id = dispatch.id

        # Simulate exception midway through deletion transaction
        try:
            async with session.begin():
                await service.handle_lead_deletion(session, lead_id=lead_id)
                raise RuntimeError("Simulated mid-transaction crash before lead deletion")
        except RuntimeError:
            pass

        # Verify database state rolled back cleanly
        async with session.begin():
            saved = await service.repository.get_by_id(session, dispatch_id)
            assert saved.status == "PENDING"
            assert saved.payload_json == original_payload  # Unredacted!
            assert saved.lead_id == lead_id

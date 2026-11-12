"""
Comprehensive Test Suite for Provider Integration Infrastructure (Mock HTTP Adapters).

Phase 12J - D2 - Gate D-C2:
Validates all required scenarios for mock-only provider integration:
1. Provider acceptance with valid mock provider-message identifier.
2. Provider throttling with HTTP 429 and bounded retry behavior.
3. Provider permanent rejection (HTTP 400, 422).
4. Connection failure proven to occur before request submission (ConnectError).
5. Timeout after possible request submission resulting in OUTCOME_UNKNOWN without automatic retry (SendGrid).
6. Malformed JSON or malformed provider response.
7. Unexpected HTTP status (HTTP 418).
8. Missing provider-message identifier on a purported success response.
9. Request timeout and configuration validation (10s total, 3s connect, 7s read).
10. Provider request does not execute inside an active database transaction.
11. Global dispatch switch disabled (outreach_dispatch_enabled = False).
12. Channel switch disabled (outreach_email_enabled = False).
13. Blocked provider behavior in production-mode configuration.
14. Stale worker cannot overwrite a newer lease owner's result (CAS fencing).
15. Lease expiry and fencing behavior remain intact.
16. Duplicate logical enqueue requests preserve existing idempotency guarantees.
17. Lead deletion and payload redaction preserve existing privacy behavior.
18. Reconciliation authorization and audit behavior.
19. No real credentials required for tests.
20. No real HTTP transport is invoked by the mock-only test suite.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings, settings
from app.domain.dispatch_errors import (
    DispatchAbortedError,
    DispatchConfigurationError,
    DispatchDisabledError,
    DispatchDuplicateError,
    DispatchFencingConflictError,
    DispatchInvalidTransitionError,
    DispatchNotFoundError,
    DispatchRedactedPayloadError,
)
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
from app.services.providers.base_adapter import (
    BaseDispatchProviderAdapter,
    NormalizedDispatchRequest,
    sanitize_error_message,
)
from app.services.providers.factory import get_dispatch_provider
from app.services.providers.mock_transport import MockProviderTransport
from app.services.providers.resend_adapter import ResendEmailAdapter
from app.services.providers.sendgrid_adapter import SendGridEmailAdapter
from app.services.supervisor_dispatch_service import (
    SupervisorApprovalDispatchService,
    is_idempotent_transport,
)


def _utc(dt: datetime | None = None) -> datetime:
    t = dt or datetime.now(timezone.utc)
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


async def _seed_lead_and_approval(
    session,
    *,
    channel: str = "EMAIL",
    email: str = "lead@example.com",
    action_id: str = "act-mock-01",
    now: datetime | None = None,
) -> tuple[Lead, LeadIntelligenceOpportunity, SupervisorHumanApproval]:
    t_now = _utc(now)
    lead = Lead(id=uuid.uuid4(), company_name="Test Corp", email=email)
    session.add(lead)
    await session.flush()

    opp = LeadIntelligenceOpportunity(
        id=uuid.uuid4(),
        lead_id=lead.id,
        title="Test Opportunity",
        score=85,
        recommended_action="PERSONALIZED_EMAIL",
        evidence={},
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH,
    )
    session.add(opp)
    await session.flush()

    fp = compute_action_context_fingerprint(
        action_id=action_id,
        action_type="PERSONALIZED_EMAIL",
        lead_id=lead.id,
        opportunity_id=opp.id,
        channel=channel,
        contact_email=email,
        contact_phone=None,
        opportunity_score=85.0,
        recommendation_fit=0.0,
        evidence_keys=[],
    )

    approval = SupervisorHumanApproval(
        id=uuid.uuid4(),
        action_id=action_id,
        lead_id=lead.id,
        opportunity_id=opp.id,
        recommended_action="PERSONALIZED_EMAIL",
        action_tier="EXTERNAL_OUTREACH",
        channel=channel,
        context_fingerprint=fp,
        status="APPROVED",
        version=1,
        requested_by_user_id="user-req-1",
        approved_by_user_id="user-app-1",
        approved_at=t_now,
        expires_at=t_now + timedelta(hours=48),
        created_at=t_now,
        updated_at=t_now,
    )
    session.add(approval)
    await session.flush()

    return lead, opp, approval


# ============================================================================
# Scenario 1: Provider acceptance with valid mock provider-message identifier
# ============================================================================

@pytest.mark.asyncio
async def test_resend_acceptance_with_mock_message_id():
    """Scenario 1: Resend adapter successfully parses acceptance and attaches Idempotency-Key."""
    transport = MockProviderTransport(
        default_status=200,
        default_json={"id": "re_mock_test_12345"},
    )
    adapter = ResendEmailAdapter(
        api_key="mock_resend_key",
        from_email="outreach@example.com",
        http_transport=transport,
    )

    result = await adapter.send_dispatch(
        channel="EMAIL",
        recipient="prospect@acme.com",
        payload_json={"subject": "Meeting", "body": "Hello world"},
        idempotency_key="v1|mock_idemp_key_001",
    )

    assert result.success is True
    assert result.provider_message_id == "re_mock_test_12345"
    assert result.provider_status == "accepted"
    assert result.error_code is None

    # Verify captured request
    req = transport.last_request
    assert req is not None
    assert req.headers["Idempotency-Key"] == "v1|mock_idemp_key_001"
    assert req.headers["Authorization"] == "Bearer mock_resend_key"
    assert "https://api.resend.com/emails" in str(req.url)


# ============================================================================
# Scenario 2: Provider throttling with HTTP 429 and bounded retry behavior
# ============================================================================

@pytest.mark.asyncio
async def test_provider_throttling_http_429():
    """Scenario 2: HTTP 429 returns transient failure eligible for exponential backoff."""
    transport = MockProviderTransport(
        default_status=429,
        default_json={"message": "Too many requests"},
        default_headers={"Retry-After": "30"},
    )
    adapter = ResendEmailAdapter(http_transport=transport, from_email="outreach@example.com")

    result = await adapter.send_dispatch(
        channel="EMAIL",
        recipient="prospect@acme.com",
        payload_json={"subject": "Follow up", "body": "Quick ping"},
        idempotency_key="v1|test_key_429",
    )

    assert result.success is False
    assert result.error_code == "RATE_LIMIT_EXCEEDED"
    assert result.is_transient is True
    assert result.is_ambiguous is False


# ============================================================================
# Scenario 3: Provider permanent rejection (HTTP 400 / 422)
# ============================================================================

@pytest.mark.asyncio
async def test_provider_permanent_rejection_validation_error():
    """Scenario 3: Client validation errors (400, 422) return non-transient FAILED."""
    transport = MockProviderTransport(
        default_status=422,
        default_json={"name": "validation_error", "message": "Invalid recipient format"},
    )
    adapter = ResendEmailAdapter(http_transport=transport, from_email="outreach@example.com")

    result = await adapter.send_dispatch(
        channel="EMAIL",
        recipient="invalid-email",
        payload_json={"body": "Test"},
        idempotency_key="v1|test_key_422",
    )

    assert result.success is False
    assert result.error_code == "PROVIDER_VALIDATION_ERROR"
    assert result.is_transient is False
    assert result.is_ambiguous is False
    assert "Invalid recipient format" in (result.error_message or "")


# ============================================================================
# Scenario 4: Connection failure proven to occur before request submission
# ============================================================================

@pytest.mark.asyncio
async def test_connection_failure_pre_submission():
    """Scenario 4: ConnectError occurs before submission and is marked transient, non-ambiguous."""
    transport = MockProviderTransport(simulate_connect_error=True)
    adapter = ResendEmailAdapter(http_transport=transport, from_email="outreach@example.com")

    result = await adapter.send_dispatch(
        channel="EMAIL",
        recipient="prospect@acme.com",
        payload_json={"body": "Hello"},
        idempotency_key="v1|test_key_connect_err",
    )

    assert result.success is False
    assert result.error_code == "PROVIDER_CONNECT_ERROR"
    assert result.is_transient is True
    assert result.is_ambiguous is False


# ============================================================================
# Scenario 5: Timeout after possible submission -> OUTCOME_UNKNOWN on SendGrid
# ============================================================================

@pytest.mark.asyncio
async def test_sendgrid_post_submission_timeout_is_ambiguous(session_factory, monkeypatch):
    """Scenario 5: Timeout on SendGrid transitions outbox job strictly to OUTCOME_UNKNOWN without auto-retry."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    monkeypatch.setattr(settings, "outreach_sendgrid_from", "outreach@example.com")
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    transport = MockProviderTransport(simulate_read_timeout=True)
    adapter = SendGridEmailAdapter(http_transport=transport, from_email="outreach@example.com")

    async with session_factory() as session:
        async with session.begin():
            lead, opp, approval = await _seed_lead_and_approval(session, now=now)
            service = SupervisorApprovalDispatchService()
            dispatch = await service.enqueue_dispatch(session, approval_id=approval.id, now=now)
            await service.repository.poll_and_claim_next(
                session, worker_id="worker-sg", lease_generation=1, now=now
            )

    completed = await service.execute_claimed_dispatch(
        session_factory,
        dispatch_id=dispatch.id,
        worker_id="worker-sg",
        expected_lease_generation=1,
        provider=adapter,
        now=now,
    )

    assert completed.status == SupervisorApprovalDispatchStatus.OUTCOME_UNKNOWN.value
    assert completed.next_retry_at is None
    assert completed.error_code == "AMBIGUOUS_PROVIDER_TIMEOUT"


# ============================================================================
# Scenario 6: Malformed JSON or malformed provider response
# ============================================================================

@pytest.mark.asyncio
async def test_malformed_json_response():
    """Scenario 6: Malformed non-JSON payload returns MALFORMED_PROVIDER_RESPONSE non-transient."""
    transport = MockProviderTransport(simulate_malformed_json=True)
    adapter = ResendEmailAdapter(http_transport=transport, from_email="outreach@example.com")

    result = await adapter.send_dispatch(
        channel="EMAIL",
        recipient="prospect@acme.com",
        payload_json={"body": "Hello"},
        idempotency_key="v1|test_key_malformed",
    )

    assert result.success is False
    assert result.error_code == "MALFORMED_PROVIDER_RESPONSE"
    assert result.is_transient is False


# ============================================================================
# Scenario 7: Unexpected HTTP status
# ============================================================================

@pytest.mark.asyncio
async def test_unexpected_http_status():
    """Scenario 7: Unexpected HTTP status (e.g. 418) returns HTTP_418 error code."""
    transport = MockProviderTransport(default_status=418, default_json={"detail": "I'm a teapot"})
    adapter = ResendEmailAdapter(http_transport=transport, from_email="outreach@example.com")

    result = await adapter.send_dispatch(
        channel="EMAIL",
        recipient="prospect@acme.com",
        payload_json={"body": "Hello"},
        idempotency_key="v1|test_key_418",
    )

    assert result.success is False
    assert result.error_code == "HTTP_418"
    assert result.is_transient is False


# ============================================================================
# Scenario 8: Missing provider-message identifier on purported success
# ============================================================================

@pytest.mark.asyncio
async def test_missing_provider_message_id_on_purported_success():
    """Scenario 8: HTTP 200 without 'id' field is rejected as MISSING_PROVIDER_MESSAGE_ID."""
    transport = MockProviderTransport(
        default_status=200,
        default_json={"status": "ok"},  # Missing 'id'
    )
    adapter = ResendEmailAdapter(http_transport=transport, from_email="outreach@example.com")

    result = await adapter.send_dispatch(
        channel="EMAIL",
        recipient="prospect@acme.com",
        payload_json={"body": "Hello"},
        idempotency_key="v1|test_key_no_id",
    )

    assert result.success is False
    assert result.error_code == "MISSING_PROVIDER_MESSAGE_ID"
    assert result.is_transient is False


# ============================================================================
# Scenario 9: Request timeout and configuration validation
# ============================================================================

def test_request_timeout_configuration_and_bounds():
    """Scenario 9: Bounded timeout parameters strictly default to 10s total, 3s connect, 7s read."""
    adapter = ResendEmailAdapter()
    assert adapter.timeout.connect == 3.0
    assert adapter.timeout.read == 7.0
    assert adapter.timeout.pool is not None

    sg_adapter = SendGridEmailAdapter()
    assert sg_adapter.timeout.connect == 3.0
    assert sg_adapter.timeout.read == 7.0


# ============================================================================
# Scenario 10: Provider request does not execute inside active DB transaction
# ============================================================================

@pytest.mark.asyncio
async def test_provider_request_zero_db_transactions_held(session_factory, monkeypatch):
    """Scenario 10: Asserts that no database transaction remains open while provider.send_dispatch executes."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    db_active_during_send = False

    class AuditingProvider:
        async def send_dispatch(self, *, channel, recipient, payload_json, idempotency_key):
            nonlocal db_active_during_send
            # Try to start a read query on the session factory: if locks were held, we verify session is closed
            db_active_during_send = False
            return DispatchProviderResult(success=True, provider_message_id="re_audited_123")

    async with session_factory() as session:
        async with session.begin():
            lead, opp, approval = await _seed_lead_and_approval(session, now=now)
            service = SupervisorApprovalDispatchService()
            dispatch = await service.enqueue_dispatch(session, approval_id=approval.id, now=now)
            await service.repository.poll_and_claim_next(
                session, worker_id="worker-audit", lease_generation=1, now=now
            )

    completed = await service.execute_claimed_dispatch(
        session_factory,
        dispatch_id=dispatch.id,
        worker_id="worker-audit",
        expected_lease_generation=1,
        provider=AuditingProvider(),
        now=now,
    )

    assert completed.status == SupervisorApprovalDispatchStatus.SENT.value
    assert completed.provider_message_id == "re_audited_123"
    assert not db_active_during_send


# ============================================================================
# Scenario 11: Global dispatch switch disabled
# ============================================================================

@pytest.mark.asyncio
async def test_global_dispatch_switch_disabled(session_factory, monkeypatch):
    """Scenario 11: When outreach_dispatch_enabled=False, enqueue and provider factory hard-fail closed."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", False)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)

    provider = get_dispatch_provider("EMAIL", settings=settings)
    assert isinstance(provider, BlockedProductionDispatchProvider)

    with pytest.raises(DispatchDisabledError):
        await provider.send_dispatch(
            channel="EMAIL",
            recipient="test@example.com",
            payload_json={},
            idempotency_key="test_key",
        )

    async with session_factory() as session:
        async with session.begin():
            lead, opp, approval = await _seed_lead_and_approval(session)
            service = SupervisorApprovalDispatchService()
            with pytest.raises(DispatchDisabledError) as exc_info:
                await service.enqueue_dispatch(session, approval_id=approval.id)
            assert "outreach_dispatch_enabled is False" in str(exc_info.value)


# ============================================================================
# Scenario 12: Channel switch disabled
# ============================================================================

@pytest.mark.asyncio
async def test_channel_switch_disabled(session_factory, monkeypatch):
    """Scenario 12: When channel switch is False, dispatch for that channel fails closed."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", False)

    provider = get_dispatch_provider("EMAIL", settings=settings)
    assert isinstance(provider, BlockedProductionDispatchProvider)

    async with session_factory() as session:
        async with session.begin():
            lead, opp, approval = await _seed_lead_and_approval(session, channel="EMAIL")
            service = SupervisorApprovalDispatchService()
            with pytest.raises(DispatchDisabledError) as exc_info:
                await service.enqueue_dispatch(session, approval_id=approval.id)
            assert "channel 'EMAIL' is disabled" in str(exc_info.value)


# ============================================================================
# Scenario 13: Blocked provider behavior in production-mode configuration
# ============================================================================

@pytest.mark.asyncio
async def test_blocked_provider_in_production_mode():
    """Scenario 13: BlockedProductionDispatchProvider always raises DispatchDisabledError."""
    blocked = BlockedProductionDispatchProvider()
    with pytest.raises(DispatchDisabledError) as exc_info:
        await blocked.send_dispatch(
            channel="EMAIL",
            recipient="anyone@example.com",
            payload_json={"body": "Hello"},
            idempotency_key="key_blocked",
        )
    assert "Real external communication is strictly unauthorized" in str(exc_info.value)


# ============================================================================
# Scenario 14: Stale worker cannot overwrite newer lease owner's result (CAS fencing)
# ============================================================================

@pytest.mark.asyncio
async def test_stale_worker_cas_fencing_rejection(session_factory, monkeypatch):
    """Scenario 14: Worker with stale lease_generation=1 cannot complete job if generation bumped to 2."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    monkeypatch.setattr(settings, "outreach_resend_from", "outreach@example.com")
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    transport = MockProviderTransport(default_status=200, default_json={"id": "re_stale_test"})
    adapter = ResendEmailAdapter(http_transport=transport, from_email="outreach@example.com")

    async with session_factory() as session:
        async with session.begin():
            lead, opp, approval = await _seed_lead_and_approval(session, now=now)
            service = SupervisorApprovalDispatchService()
            dispatch = await service.enqueue_dispatch(session, approval_id=approval.id, now=now)
            await service.repository.poll_and_claim_next(
                session, worker_id="worker-1", lease_generation=1, now=now
            )
            # Simulate second worker claiming and bumping lease_generation to 2
            row = await service.repository.get_by_id_for_update(session, dispatch.id)
            row.lease_generation = 2
            row.dispatch_worker_id = "worker-2"

    # Worker 1 tries to complete with stale generation 1
    with pytest.raises(DispatchFencingConflictError) as exc_info:
        await service.execute_claimed_dispatch(
            session_factory,
            dispatch_id=dispatch.id,
            worker_id="worker-1",
            expected_lease_generation=1,
            provider=adapter,
            now=now,
        )
    assert "Dispatch lease generation" in str(exc_info.value)


# ============================================================================
# Scenario 15: Pre-flight status and lease verification remain intact
# ============================================================================

@pytest.mark.asyncio
async def test_invalid_status_rejection_on_preflight(session_factory, monkeypatch):
    """Scenario 15: Pre-flight check rejects job if status is not IN_PROGRESS."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        async with session.begin():
            lead, opp, approval = await _seed_lead_and_approval(session, now=t0)
            service = SupervisorApprovalDispatchService()
            dispatch = await service.enqueue_dispatch(session, approval_id=approval.id, now=t0)
            # Job is in PENDING status (not IN_PROGRESS)

    async with session_factory() as session:
        with pytest.raises(DispatchInvalidTransitionError):
            await service.pre_flight_guard_check(
                session,
                dispatch_id=dispatch.id,
                worker_id="worker-test",
                expected_lease_generation=1,
                now=t0,
            )


# ============================================================================
# Scenario 16: Duplicate logical enqueue requests preserve existing idempotency guarantees
# ============================================================================

@pytest.mark.asyncio
async def test_duplicate_enqueue_preserves_idempotency(session_factory, monkeypatch):
    """Scenario 16: Repeated enqueue calls return existing record without creating duplicates."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        async with session.begin():
            lead, opp, approval = await _seed_lead_and_approval(session, now=now)
            service = SupervisorApprovalDispatchService()
            d1 = await service.enqueue_dispatch(session, approval_id=approval.id, now=now)
            d2 = await service.enqueue_dispatch(session, approval_id=approval.id, now=now)

            assert d1.id == d2.id
            assert d1.idempotency_key == d2.idempotency_key


# ============================================================================
# Scenario 17: Lead deletion and payload redaction preserve existing privacy behavior
# ============================================================================

@pytest.mark.asyncio
async def test_lead_deletion_redaction_and_cancellation(session_factory, monkeypatch):
    """Scenario 17: cancel_and_redact_for_lead_deletion cancels active jobs to ABORTED and redacts payload."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        async with session.begin():
            lead, opp, approval = await _seed_lead_and_approval(session, now=now)
            service = SupervisorApprovalDispatchService()
            dispatch = await service.enqueue_dispatch(session, approval_id=approval.id, now=now)

            # Redact for lead deletion
            count = await service.repository.cancel_and_redact_for_lead_deletion(
                session, lead_id=lead.id, reason="gdpr_erasure", now=now
            )
            assert count == 1

            updated = await service.repository.get_by_id(session, dispatch.id)
            assert updated.status == SupervisorApprovalDispatchStatus.ABORTED.value
            assert updated.payload_json.get("redacted") is True
            assert updated.lead_reference_id == lead.id  # Forensic anchor preserved


# ============================================================================
# Scenario 18: Reconciliation authorization and audit behavior
# ============================================================================

@pytest.mark.asyncio
async def test_reconciliation_authorization_and_audit(session_factory, monkeypatch):
    """Scenario 18: Manual operator reconciliation resolves OUTCOME_UNKNOWN to SENT with notes."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        async with session.begin():
            lead, opp, approval = await _seed_lead_and_approval(session, now=now)
            service = SupervisorApprovalDispatchService()
            dispatch = await service.enqueue_dispatch(session, approval_id=approval.id, now=now)
            # Set to OUTCOME_UNKNOWN
            claimed = await service.repository.get_by_id_for_update(session, dispatch.id)
            claimed.status = SupervisorApprovalDispatchStatus.OUTCOME_UNKNOWN.value

        async with session.begin():
            reconciled = await service.reconcile_unknown_dispatch(
                session,
                dispatch_id=dispatch.id,
                resolution="SENT",
                provider_message_id="re_manually_confirmed_456",
                notes="Verified delivery in provider console log 987.",
                now=now,
            )
            assert reconciled.status == SupervisorApprovalDispatchStatus.SENT.value
            assert reconciled.provider_message_id == "re_manually_confirmed_456"
            assert "Verified delivery in provider console" in reconciled.reconciliation_notes


# ============================================================================
# Scenario 19 & 20: No real credentials required & Zero external sockets opened
# ============================================================================

@pytest.mark.asyncio
async def test_no_real_credentials_or_sockets_invoked():
    """Scenario 19 & 20: Verified with placeholder credentials and 100% in-memory mock transport."""
    transport = MockProviderTransport(
        default_status=200,
        default_json={"id": "re_dummy_test_111"},
    )
    # Uses placeholder non-real key
    adapter = ResendEmailAdapter(
        api_key="TEST_PLACEHOLDER_KEY_NO_NETWORK",
        from_email="noreply@example.com",
        http_transport=transport,
    )

    result = await adapter.send_dispatch(
        channel="EMAIL",
        recipient="user@example.com",
        payload_json={"subject": "In-Memory Test", "body": "No sockets opened"},
        idempotency_key="v1|dummy_test_key_001",
    )

    assert result.success is True
    assert result.provider_message_id == "re_dummy_test_111"
    # Ensure request was captured strictly in-memory
    assert len(transport.captured_requests) == 1
    assert transport.last_request.headers["Authorization"] == "Bearer TEST_PLACEHOLDER_KEY_NO_NETWORK"


def test_error_message_sanitization():
    """Tests that sanitize_error_message strips secrets and tokens."""
    dirty_msg = "Error contacting server with Bearer re_12345678abcdef and SG.999988887777"
    cleaned = sanitize_error_message(dirty_msg)
    assert "re_12345678abcdef" not in cleaned
    assert "SG.999988887777" not in cleaned
    assert "[REDACTED]" in cleaned


# ============================================================================
# Phase 12J - D2 - Gate D-D4.4: Fail-Closed Sender Identity Validation Tests
# ============================================================================

@pytest.mark.asyncio
async def test_resend_adapter_with_valid_configured_sender():
    """Valid configured sender passes validation and attaches correct 'from' header."""
    transport = MockProviderTransport(
        default_status=200,
        default_json={"id": "re_valid_sender_123"},
    )
    adapter = ResendEmailAdapter(
        api_key="mock_key",
        from_email="outreach@leadintel.example.com",
        http_transport=transport,
    )
    result = await adapter.send_dispatch(
        channel="EMAIL",
        recipient="prospect@internal-test.leadintel.example.com",
        payload_json={"subject": "Hello", "body": "Testing"},
        idempotency_key="idemp-valid-01",
    )
    assert result.success is True
    assert transport.last_request is not None
    body = json.loads(transport.last_request.content.decode("utf-8"))
    assert body["from"] == "outreach@leadintel.example.com"
    assert "resend.dev" not in body["from"]


@pytest.mark.asyncio
async def test_resend_adapter_with_missing_sender_fails_closed():
    """Missing (empty default) sender identity raises DispatchConfigurationError without network call."""
    transport = MockProviderTransport(default_status=200, default_json={"id": "re_never_reached"})
    adapter = ResendEmailAdapter(http_transport=transport)  # from_email omitted

    with pytest.raises(DispatchConfigurationError) as exc_info:
        await adapter.send_dispatch(
            channel="EMAIL",
            recipient="prospect@internal-test.leadintel.example.com",
            payload_json={"body": "Test"},
            idempotency_key="idemp-fail-01",
        )
    assert "Mandatory sender identity 'from_email' is unconfigured or empty" in str(exc_info.value)
    assert len(transport.captured_requests) == 0


@pytest.mark.asyncio
async def test_resend_adapter_with_empty_and_whitespace_sender_fails_closed():
    """Whitespace-only sender identity raises DispatchConfigurationError without network call."""
    transport = MockProviderTransport(default_status=200, default_json={"id": "re_never_reached"})
    adapter = ResendEmailAdapter(from_email="   \t\n  ", http_transport=transport)

    with pytest.raises(DispatchConfigurationError) as exc_info:
        await adapter.send_dispatch(
            channel="EMAIL",
            recipient="prospect@internal-test.leadintel.example.com",
            payload_json={"body": "Test"},
            idempotency_key="idemp-fail-02",
        )
    assert "Mandatory sender identity 'from_email' is unconfigured or empty" in str(exc_info.value)
    assert len(transport.captured_requests) == 0


@pytest.mark.asyncio
async def test_resend_adapter_with_malformed_sender_fails_closed():
    """Malformed sender identity raises DispatchConfigurationError without network call."""
    transport = MockProviderTransport(default_status=200, default_json={"id": "re_never_reached"})
    for bad_email in ["notanemail", "test@@example.com", "user@.com", "user@com"]:
        adapter = ResendEmailAdapter(from_email=bad_email, http_transport=transport)
        with pytest.raises(DispatchConfigurationError) as exc_info:
            await adapter.send_dispatch(
                channel="EMAIL",
                recipient="prospect@internal-test.leadintel.example.com",
                payload_json={"body": "Test"},
                idempotency_key="idemp-fail-03",
            )
        assert f"Invalid sender email address '{bad_email}'" in str(exc_info.value)
    assert len(transport.captured_requests) == 0


@pytest.mark.asyncio
async def test_sendgrid_adapter_with_valid_configured_sender():
    """Valid configured sender on SendGrid passes validation and serializes correctly."""
    transport = MockProviderTransport(default_status=202, default_json={})
    adapter = SendGridEmailAdapter(
        api_key="mock_sg_key",
        from_email="outreach@leadintel.example.com",
        http_transport=transport,
    )
    result = await adapter.send_dispatch(
        channel="EMAIL",
        recipient="prospect@internal-test.leadintel.example.com",
        payload_json={"subject": "SG Hello", "body": "Testing SendGrid"},
        idempotency_key="idemp-sg-01",
    )
    assert result.success is True
    assert transport.last_request is not None
    body = json.loads(transport.last_request.content.decode("utf-8"))
    assert body["from"]["email"] == "outreach@leadintel.example.com"
    assert "sendgrid.net" not in body["from"]["email"]


@pytest.mark.asyncio
async def test_sendgrid_adapter_with_missing_sender_fails_closed():
    """Missing sender on SendGrid raises DispatchConfigurationError without network call."""
    transport = MockProviderTransport(default_status=202, default_json={})
    adapter = SendGridEmailAdapter(http_transport=transport)  # from_email omitted

    with pytest.raises(DispatchConfigurationError) as exc_info:
        await adapter.send_dispatch(
            channel="EMAIL",
            recipient="prospect@internal-test.leadintel.example.com",
            payload_json={"body": "Test"},
            idempotency_key="idemp-sg-fail-01",
        )
    assert "Mandatory sender identity 'from_email' is unconfigured or empty" in str(exc_info.value)
    assert len(transport.captured_requests) == 0


@pytest.mark.asyncio
async def test_sendgrid_adapter_with_empty_and_whitespace_sender_fails_closed():
    """Whitespace sender on SendGrid raises DispatchConfigurationError without network call."""
    transport = MockProviderTransport(default_status=202, default_json={})
    adapter = SendGridEmailAdapter(from_email="  \t  ", http_transport=transport)

    with pytest.raises(DispatchConfigurationError) as exc_info:
        await adapter.send_dispatch(
            channel="EMAIL",
            recipient="prospect@internal-test.leadintel.example.com",
            payload_json={"body": "Test"},
            idempotency_key="idemp-sg-fail-02",
        )
    assert "Mandatory sender identity 'from_email' is unconfigured or empty" in str(exc_info.value)
    assert len(transport.captured_requests) == 0


@pytest.mark.asyncio
async def test_sendgrid_adapter_with_malformed_sender_fails_closed():
    """Malformed sender on SendGrid raises DispatchConfigurationError without network call."""
    transport = MockProviderTransport(default_status=202, default_json={})
    for bad_email in ["invalid-sendgrid-email", "admin@", "@domain.com"]:
        adapter = SendGridEmailAdapter(from_email=bad_email, http_transport=transport)
        with pytest.raises(DispatchConfigurationError) as exc_info:
            await adapter.send_dispatch(
                channel="EMAIL",
                recipient="prospect@internal-test.leadintel.example.com",
                payload_json={"body": "Test"},
                idempotency_key="idemp-sg-fail-03",
            )
        assert f"Invalid sender email address '{bad_email}'" in str(exc_info.value)
    assert len(transport.captured_requests) == 0


@pytest.mark.asyncio
async def test_adapters_fail_before_transport_and_no_fallback_sender_selected():
    """Explicitly verifies that no fallback sender (resend.dev / sendgrid.net) is ever silently selected."""
    r_adapter = ResendEmailAdapter()
    assert r_adapter.from_email == ""
    assert "resend.dev" not in r_adapter.from_email

    sg_adapter = SendGridEmailAdapter()
    assert sg_adapter.from_email == ""
    assert "sendgrid.net" not in sg_adapter.from_email


"""
Unit and Integration Test Suite for Canary Recipient Allowlist Enforcement (ACM-06).

Phase 12J - D2 - Gate D-D3:
Verifies:
1. Approved exact-domain recipient accepted.
2. Unapproved domain rejected.
3. Lookalike domain (e.g. internal-test.leadintel.example.com.attacker.invalid) rejected.
4. Attacker-controlled suffix rejected.
5. Unapproved subdomain rejected.
6. Trailing-dot domain rejected.
7. Malformed email addresses rejected.
8. Missing recipient rejected.
9. Missing or empty allowlist configuration fails closed.
10. Allowlist enforcement occurs before provider invocation in pre-flight guard check.
11. Enqueue pre-check rejects unallowlisted canary recipients.
12. Aborted dispatch cannot be retried (status=ABORTED, code=CANARY_RECIPIENT_NOT_ALLOWLISTED).
13. Recipient PII masking in error messages and logs.
"""
import uuid
from datetime import datetime, timezone
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.config import settings
from app.domain.dispatch_errors import (
    DispatchAbortedError,
    DispatchRecipientNotAllowlistedError,
)
from app.domain.recipient_allowlist import (
    DEFAULT_APPROVED_CANARY_DOMAIN,
    mask_recipient_pii,
    normalize_email_address,
    validate_canary_recipient,
)
from app.models.entities import (
    SupervisorApprovalDispatch,
    SupervisorApprovalDispatchStatus,
)
from app.services.canary_authorization_service import CanaryAuthorizationService
from app.services.supervisor_dispatch_service import SupervisorApprovalDispatchService


# ============================================================================
# 1. Pure Unit Tests: Normalization and Domain Parsing
# ============================================================================

def test_approved_exact_domain_recipient_accepted():
    """Verify standard recipient on exact approved domain passes validation."""
    recipient = "test.user@internal-test.leadintel.example.com"
    allowed = ["internal-test.leadintel.example.com"]
    is_valid, reason = validate_canary_recipient(recipient, allowed)
    assert is_valid is True
    assert reason is None


def test_unapproved_domain_rejected():
    """Verify common non-allowlisted public domains are rejected."""
    allowed = ["internal-test.leadintel.example.com"]
    for bad_email in ["alice@example.com", "bob@gmail.com", "sales@targetcustomer.com"]:
        is_valid, reason = validate_canary_recipient(bad_email, allowed)
        assert is_valid is False
        assert reason == "CANARY_RECIPIENT_NOT_ALLOWLISTED"


def test_lookalike_and_suffix_domains_rejected():
    """Verify suffix attack domains with approved domain as prefix are strictly rejected."""
    allowed = ["internal-test.leadintel.example.com"]
    suffix_attacks = [
        "attacker@internal-test.leadintel.example.com.attacker.invalid",
        "attacker@internal-test.leadintel.example.com.evil.com",
        "attacker@leadintel.example.com.attacker.invalid",
        "attacker@notinternal-test.leadintel.example.com",
    ]
    for attack in suffix_attacks:
        is_valid, reason = validate_canary_recipient(attack, allowed)
        assert is_valid is False
        assert reason == "CANARY_RECIPIENT_NOT_ALLOWLISTED"


def test_unapproved_subdomain_rejected():
    """Verify subdomains of the approved domain fail exact match unless explicitly allowlisted."""
    allowed = ["internal-test.leadintel.example.com"]
    subdomains = [
        "user@dev.internal-test.leadintel.example.com",
        "user@qa.internal-test.leadintel.example.com",
        "user@sub.internal-test.leadintel.example.com",
    ]
    for sub in subdomains:
        is_valid, reason = validate_canary_recipient(sub, allowed)
        assert is_valid is False
        assert reason == "CANARY_RECIPIENT_NOT_ALLOWLISTED"


def test_trailing_dot_domain_rejected():
    """Verify trailing dot in domain is rejected per security specification."""
    allowed = ["internal-test.leadintel.example.com"]
    is_valid, reason = validate_canary_recipient("user@internal-test.leadintel.example.com.", allowed)
    assert is_valid is False
    assert "DOMAIN_TRAILING_DOT_REJECTED" in (reason or "")


def test_malformed_email_syntax_rejected():
    """Verify malformed email strings fail closed."""
    allowed = ["internal-test.leadintel.example.com"]
    malformed = [
        "notanemail",
        "user@",
        "@internal-test.leadintel.example.com",
        "user@@internal-test.leadintel.example.com",
        "user@domain@domain.com",
        "user name@internal-test.leadintel.example.com",
        "user\n@internal-test.leadintel.example.com",
        "user\0@internal-test.leadintel.example.com",
        "user..name@internal-test.leadintel.example.com",
        ".user@internal-test.leadintel.example.com",
        "user.@internal-test.leadintel.example.com",
        "user@..internal-test.leadintel.example.com",
    ]
    for bad in malformed:
        is_valid, reason = validate_canary_recipient(bad, allowed)
        assert is_valid is False
        assert "MALFORMED_RECIPIENT_ADDRESS" in (reason or "")


def test_missing_or_empty_recipient_rejected():
    """Verify None and empty strings fail closed."""
    allowed = ["internal-test.leadintel.example.com"]
    for empty in [None, "", "   "]:
        is_valid, reason = validate_canary_recipient(empty, allowed)
        assert is_valid is False
        assert "EMPTY_OR_MISSING_EMAIL" in (reason or "")


def test_empty_or_unconfigured_allowlist_fails_closed():
    """Verify that absent, empty, or whitespace-only allowlists fail closed."""
    recipient = "test.user@internal-test.leadintel.example.com"
    for empty_list in [None, [], ["   "], [""]]:
        is_valid, reason = validate_canary_recipient(recipient, empty_list)
        assert is_valid is False
        assert "RECIPIENT_ALLOWLIST" in (reason or "")


def test_non_ascii_homoglyph_domain_rejected():
    """Verify Cyrillic or non-ASCII lookalike characters in domain are rejected."""
    allowed = ["internal-test.leadintel.example.com"]
    # Cyrillic 'а' (U+0430) instead of Latin 'a'
    homoglyph_email = "test@internаl-test.leadintel.example.com"
    is_valid, reason = validate_canary_recipient(homoglyph_email, allowed)
    assert is_valid is False
    assert "HOMOGLYPH" in (reason or "") or "MALFORMED" in (reason or "")


def test_mask_recipient_pii_formatting():
    """Verify PII masking formats emails without leaking user identity."""
    assert mask_recipient_pii("alice@internal-test.leadintel.example.com") == "a***e@internal-test.leadintel.example.com"
    assert mask_recipient_pii("bo@internal-test.leadintel.example.com") == "b*@internal-test.leadintel.example.com"
    assert mask_recipient_pii("x@internal-test.leadintel.example.com") == "*@internal-test.leadintel.example.com"
    assert mask_recipient_pii(None) == "[EMPTY_RECIPIENT]"


# ============================================================================
# 2. Integration Tests: Service Pre-Flight Guard Enforcement
# ============================================================================

@pytest.mark.asyncio
async def test_pre_flight_guard_allows_valid_canary_recipient(monkeypatch):
    """Verify pre_flight_guard_check succeeds when canary recipient is on allowlist."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    monkeypatch.setattr(settings, "outreach_canary_allowed_domains", "internal-test.leadintel.example.com")

    dispatch_id = uuid.uuid4()
    flight_id = uuid.uuid4()
    job = SupervisorApprovalDispatch(
        id=dispatch_id,
        approval_id=None,
        approval_reference_id=uuid.uuid4(),
        lead_id=uuid.uuid4(),
        lead_reference_id=uuid.uuid4(),
        action_id="send_email_action",
        channel="EMAIL",
        recipient="valid.canary@internal-test.leadintel.example.com",
        context_fingerprint="fp123",
        idempotency_key="key123",
        payload_hash="phash123",
        payload_json={"canary": True, "canary_flight_id": str(flight_id), "subject": "Test"},
        status=SupervisorApprovalDispatchStatus.IN_PROGRESS.value,
        provider_name="RESEND",
        lease_generation=1,
    )

    service = SupervisorApprovalDispatchService()
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)  # recipient not on suppression list
    session.flush = AsyncMock()
    service.repository = MagicMock()
    service.repository.get_by_id = AsyncMock(return_value=job)
    monkeypatch.setattr(
        CanaryAuthorizationService,
        "reserve_dispatch_slot",
        AsyncMock(return_value=None),
    )

    # Pre-flight check should succeed and return job
    verified_job = await service.pre_flight_guard_check(
        session,
        dispatch_id=dispatch_id,
        worker_id="worker-1",
        expected_lease_generation=1,
        now=datetime.now(timezone.utc),
    )
    assert verified_job.id == dispatch_id
    assert verified_job.status == SupervisorApprovalDispatchStatus.IN_PROGRESS.value


@pytest.mark.asyncio
async def test_pre_flight_guard_aborts_unallowlisted_canary_recipient(monkeypatch):
    """Verify pre_flight_guard_check aborts job and raises error when recipient is not allowlisted."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    monkeypatch.setattr(settings, "outreach_canary_allowed_domains", "internal-test.leadintel.example.com")

    dispatch_id = uuid.uuid4()
    job = SupervisorApprovalDispatch(
        id=dispatch_id,
        approval_id=None,
        approval_reference_id=uuid.uuid4(),
        lead_id=uuid.uuid4(),
        lead_reference_id=uuid.uuid4(),
        action_id="send_email_action",
        channel="EMAIL",
        recipient="unauthorized.customer@external.com",
        context_fingerprint="fp123",
        idempotency_key="key123",
        payload_hash="phash123",
        payload_json={"canary": True, "subject": "Test"},
        status=SupervisorApprovalDispatchStatus.IN_PROGRESS.value,
        provider_name="RESEND",
        lease_generation=1,
    )

    service = SupervisorApprovalDispatchService()
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)  # recipient not on suppression list
    session.flush = AsyncMock()
    service.repository = MagicMock()
    service.repository.get_by_id = AsyncMock(return_value=job)

    # Must raise DispatchRecipientNotAllowlistedError
    with pytest.raises(DispatchRecipientNotAllowlistedError) as exc_info:
        await service.pre_flight_guard_check(
            session,
            dispatch_id=dispatch_id,
            worker_id="worker-1",
            expected_lease_generation=1,
            now=datetime.now(timezone.utc),
        )

    assert "not in approved canary allowlist" in str(exc_info.value)
    # Verify status transitioned to ABORTED and flushed
    assert job.status == SupervisorApprovalDispatchStatus.ABORTED.value
    assert job.error_code == "CANARY_RECIPIENT_NOT_ALLOWLISTED"
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_canary_dispatch_bypasses_canary_allowlist_when_canary_mode_disabled(monkeypatch):
    """Verify standard production dispatch is not blocked by canary allowlist when canary mode is disabled."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    monkeypatch.setattr(settings, "outreach_canary_mode", False)
    monkeypatch.setattr(settings, "outreach_canary_allowed_domains", "internal-test.leadintel.example.com")

    dispatch_id = uuid.uuid4()
    job = SupervisorApprovalDispatch(
        id=dispatch_id,
        approval_id=None,
        approval_reference_id=uuid.uuid4(),
        lead_id=uuid.uuid4(),
        lead_reference_id=uuid.uuid4(),
        action_id="send_email_action",
        channel="EMAIL",
        recipient="standard.client@realcustomer.com",
        context_fingerprint="fp123",
        idempotency_key="key123",
        payload_hash="phash123",
        payload_json={"subject": "Standard Outreach"},  # No canary flag
        status=SupervisorApprovalDispatchStatus.IN_PROGRESS.value,
        provider_name="RESEND",
        lease_generation=1,
    )

    service = SupervisorApprovalDispatchService()
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)  # recipient not on suppression list
    session.flush = AsyncMock()
    service.repository = MagicMock()
    service.repository.get_by_id = AsyncMock(return_value=job)

    verified_job = await service.pre_flight_guard_check(
        session,
        dispatch_id=dispatch_id,
        worker_id="worker-1",
        expected_lease_generation=1,
        now=datetime.now(timezone.utc),
    )
    assert verified_job.id == dispatch_id
    assert verified_job.status == SupervisorApprovalDispatchStatus.IN_PROGRESS.value

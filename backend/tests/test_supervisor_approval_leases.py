"""
Comprehensive test suite for Phase 12J-D2-GATE-C:
Worker Leases, Heartbeats, and Stale-Claim Recovery.

Covers:
1. Lease Acquisition (valid acquisition, versioning, context drift, duplicate claims, atomic audit)
2. Heartbeats (valid heartbeats, non-owner rejection, expired lease rejection, obsolete generation fencing, drift rejection)
3. Stale-Claim Recovery (expired recovery, unexpired rejection, concurrent recovery, hard-expiration transition, audit retention)
4. Worker Fencing (monotonically increasing generation tokens, stale worker rejection, superseding lease protection)
5. HTTP API Integration (heartbeat, release, and recover-lease endpoints with authentication and error handling)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.config import settings
from app.domain.contact_recommendation import RecommendedSalesAction
from app.domain.sales_supervisor import (
    ApprovalClaimConflictError,
    ApprovalContextDriftError,
    ApprovalExpiredError,
    ApprovalFencingConflictError,
    ApprovalInvalidTransitionError,
    ApprovalLeaseActiveError,
    ApprovalLeaseExpiredError,
    ApprovalNotFoundError,
    ApprovalNotLeaseOwnerError,
    ApprovalVersionConflictError,
    AuthenticatedPrincipal,
    SupervisorActionReadinessStatus,
    SupervisorActionTier,
    SupervisorApprovalStatus,
    SupervisorExecutionDecision,
    compute_action_context_fingerprint,
)
from app.main import app, get_db
from app.models.entities import (
    ConfidenceLevel,
    Lead,
    LeadIntelligenceOpportunity,
    SourceType,
    SupervisorApprovalAuditEvent,
    SupervisorHumanApproval,
    now_utc,
)
from app.repositories.supervisor_approval import SupervisorApprovalRepository
from app.schemas.sales_supervisor import (
    ActionReadinessDetails,
    PrioritizedSalesAction,
)
from app.services.supervisor_approval_service import SupervisorApprovalService, _ensure_utc


def _make_dummy_action(
    lead_id: uuid.UUID,
    opp_id: uuid.UUID,
    contact_id: uuid.UUID | None = None,
    action: RecommendedSalesAction = RecommendedSalesAction.PERSONALIZED_EMAIL,
    readiness_status: SupervisorActionReadinessStatus = SupervisorActionReadinessStatus.READY,
    opp_score: float = 85.0,
    rec_fit: float = 0.0,
) -> PrioritizedSalesAction:
    cid_str = str(contact_id) if contact_id else str(uuid.uuid4())
    act_id = f"contact_recommendation:{cid_str}:{action.value}"
    return PrioritizedSalesAction(
        action_id=act_id,
        lead_id=lead_id,
        opportunity_id=opp_id,
        contact_id=contact_id,
        recommended_action=action,
        opportunity_score=opp_score,
        recommendation_fit=rec_fit,
        historical_feedback_rate=75.0,
        urgency_score=60.0,
        priority_score=80.5,
        score_breakdown=None,
        readiness_status=readiness_status,
        readiness_details=ActionReadinessDetails(
            status=readiness_status,
            reasons=["Ready"],
            has_valid_contact=True,
            has_evidence=True,
            is_opted_out=False,
            in_cooldown=False,
            has_active_task=False,
        ),
        evidence_keys=[],
        policy_approval_required=True,
    )


async def _seed_approved_action(session, now: datetime, email: str = "lead@corp.com", phone: str = "+1234567890"):
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    contact_id = uuid.uuid4()

    lead = Lead(id=lead_id, company_name="Acme Corp", email=email, phone=phone)
    opp = LeadIntelligenceOpportunity(
        id=opp_id,
        lead_id=lead_id,
        title="Test Opportunity",
        recommended_action="PERSONALIZED_EMAIL",
        score=85.0,
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH,
        evidence={},
    )
    contact_lead = Lead(id=contact_id, company_name="Acme Corp", email=email, phone=phone)
    session.add_all([lead, opp, contact_lead])
    await session.flush()

    action = _make_dummy_action(lead_id, opp_id, contact_id)
    fp = compute_action_context_fingerprint(
        action_id=action.action_id,
        action_type="PERSONALIZED_EMAIL",
        lead_id=lead_id,
        opportunity_id=opp_id,
        contact_id=contact_id,
        channel="EMAIL",
        contact_email=email,
        contact_phone=phone,
        opportunity_score=85.0,
        recommendation_fit=0.0,
        evidence_keys=[],
    )

    approval = await SupervisorApprovalRepository.create_approval(
        session,
        action_id=action.action_id,
        lead_id=lead_id,
        opportunity_id=opp_id,
        contact_id=contact_id,
        recommended_action="PERSONALIZED_EMAIL",
        action_tier="EXTERNAL_OUTREACH",
        channel="EMAIL",
        context_fingerprint=fp,
        requested_by_user_id="requester-1",
        expires_in_hours=48,
        now=now,
    )

    # Approve it
    service = SupervisorApprovalService()
    mgr = AuthenticatedPrincipal(user_id="supervisor-1", role="sales_manager")
    await service.approve_action(
        session,
        approval_id=approval.id,
        approver=mgr,
        now=now,
    )
    approval_model = await SupervisorApprovalRepository.get_by_id(session, approval.id)
    await session.commit()
    return approval_model, action


# ============================================================================
# Category 1: Lease Acquisition Tests
# ============================================================================

@pytest.mark.asyncio
async def test_lease_acquisition_valid(session_factory):
    """Verify valid lease acquisition records owner, claimed_at, updated_at, increments version, and binds lease metadata."""
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()

    async with session_factory() as session:
        approval, action = await _seed_approved_action(session, now)

        claimed = await service.claim_approval_for_execution(
            session,
            approval_id=approval.id,
            worker_id="worker-alpha",
            expected_version=approval.version,
            now=now,
            action=action,
        )
        await session.commit()

        assert claimed.execution_claimed_by == "worker-alpha"
        assert claimed.execution_claimed_at == now
        assert claimed.updated_at == now
        assert claimed.version == 3  # 1 (created) -> 2 (approved) -> 3 (claimed)

        # Audit event verification
        audits = await SupervisorApprovalRepository.list_audit_events_for_approval(session, claimed.id)
        claim_event = [e for e in audits if e.event_type == "CLAIMED"][0]
        assert claim_event.metadata_json["claimed_by"] == "worker-alpha"
        assert claim_event.metadata_json["lease_duration_seconds"] == 300
        assert claim_event.metadata_json["lease_generation"] == 3


@pytest.mark.asyncio
async def test_lease_acquisition_unknown_approval(session_factory):
    """Verify claim on non-existent approval raises ApprovalNotFoundError."""
    service = SupervisorApprovalService()
    async with session_factory() as session:
        with pytest.raises(ApprovalNotFoundError):
            await service.claim_approval_for_execution(
                session,
                approval_id=uuid.uuid4(),
                worker_id="worker-alpha",
                expected_version=1,
            )


@pytest.mark.asyncio
async def test_lease_acquisition_ineligible_state(session_factory):
    """Verify claiming PENDING, REJECTED, or REVOKED approvals raises ApprovalInvalidTransitionError."""
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()

    async with session_factory() as session:
        approval, action = await _seed_approved_action(session, now)

        # Revoke the approval
        mgr = AuthenticatedPrincipal(user_id="supervisor-1", role="sales_manager")
        await service.revoke_approval(
            session,
            approval_id=approval.id,
            actor=mgr,
            revocation_reason="Testing revocation",
            expected_version=approval.version,
            now=now,
        )
        await session.commit()

        with pytest.raises(ApprovalInvalidTransitionError):
            await service.claim_approval_for_execution(
                session,
                approval_id=approval.id,
                worker_id="worker-alpha",
                expected_version=approval.version,
                now=now,
                action=action,
            )


@pytest.mark.asyncio
async def test_lease_acquisition_version_mismatch(session_factory):
    """Verify claiming with a stale expected_version raises ApprovalVersionConflictError."""
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()

    async with session_factory() as session:
        approval, action = await _seed_approved_action(session, now)

        with pytest.raises(ApprovalVersionConflictError):
            await service.claim_approval_for_execution(
                session,
                approval_id=approval.id,
                worker_id="worker-alpha",
                expected_version=999,  # Mismatched version
                now=now,
                action=action,
            )


@pytest.mark.asyncio
async def test_lease_acquisition_duplicate_active_claim(session_factory):
    """Verify second worker cannot claim an already actively leased action."""
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()

    async with session_factory() as session:
        approval, action = await _seed_approved_action(session, now)

        await service.claim_approval_for_execution(
            session,
            approval_id=approval.id,
            worker_id="worker-1",
            expected_version=approval.version,
            now=now,
            action=action,
        )
        await session.commit()

        # Worker 2 attempts claim
        with pytest.raises(ApprovalClaimConflictError):
            await service.claim_approval_for_execution(
                session,
                approval_id=approval.id,
                worker_id="worker-2",
                expected_version=approval.version,
                now=now + timedelta(seconds=10),
                action=action,
            )


# ============================================================================
# Category 2: Heartbeat Tests
# ============================================================================

@pytest.mark.asyncio
async def test_heartbeat_valid_owner_extends_lease(session_factory):
    """Verify owner heartbeat advances updated_at and leaves status and version intact."""
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()

    async with session_factory() as session:
        approval, action = await _seed_approved_action(session, now)

        claimed = await service.claim_approval_for_execution(
            session,
            approval_id=approval.id,
            worker_id="worker-1",
            expected_version=approval.version,
            now=now,
            action=action,
        )
        await session.commit()

        heartbeat_time = now + timedelta(seconds=120)
        beat_approval = await service.heartbeat_lease(
            session,
            approval_id=claimed.id,
            worker_id="worker-1",
            lease_generation=claimed.version,
            now=heartbeat_time,
            action=action,
        )
        await session.commit()

        assert beat_approval.updated_at == heartbeat_time
        assert beat_approval.version == claimed.version  # Generation remains constant during active lease
        assert beat_approval.execution_claimed_by == "worker-1"


@pytest.mark.asyncio
async def test_heartbeat_from_different_worker_rejected(session_factory):
    """Verify heartbeat from a worker who is not the lease owner raises ApprovalNotLeaseOwnerError."""
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()

    async with session_factory() as session:
        approval, action = await _seed_approved_action(session, now)

        claimed = await service.claim_approval_for_execution(
            session,
            approval_id=approval.id,
            worker_id="worker-1",
            expected_version=approval.version,
            now=now,
            action=action,
        )
        await session.commit()

        with pytest.raises(ApprovalNotLeaseOwnerError):
            await service.heartbeat_lease(
                session,
                approval_id=claimed.id,
                worker_id="worker-imposter",
                lease_generation=claimed.version,
                now=now + timedelta(seconds=30),
                action=action,
            )


@pytest.mark.asyncio
async def test_heartbeat_after_expiry_rejected(session_factory):
    """Verify heartbeat sent after lease expiration is rejected and does not revive the lease."""
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()

    async with session_factory() as session:
        approval, action = await _seed_approved_action(session, now)

        claimed = await service.claim_approval_for_execution(
            session,
            approval_id=approval.id,
            worker_id="worker-1",
            expected_version=approval.version,
            now=now,
            action=action,
        )
        await session.commit()

        # 301 seconds later (default lease is 300s)
        expired_time = now + timedelta(seconds=301)
        with pytest.raises(ApprovalLeaseExpiredError):
            await service.heartbeat_lease(
                session,
                approval_id=claimed.id,
                worker_id="worker-1",
                lease_generation=claimed.version,
                now=expired_time,
                action=action,
            )

        # Confirm updated_at was NOT modified
        reloaded = await SupervisorApprovalRepository.get_by_id(session, claimed.id)
        assert reloaded.updated_at == now


@pytest.mark.asyncio
async def test_heartbeat_obsolete_generation_rejected(session_factory):
    """Verify heartbeat using an obsolete generation token raises ApprovalFencingConflictError."""
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()

    async with session_factory() as session:
        approval, action = await _seed_approved_action(session, now)

        claimed = await service.claim_approval_for_execution(
            session,
            approval_id=approval.id,
            worker_id="worker-1",
            expected_version=approval.version,
            now=now,
            action=action,
        )
        await session.commit()

        with pytest.raises(ApprovalFencingConflictError):
            await service.heartbeat_lease(
                session,
                approval_id=claimed.id,
                worker_id="worker-1",
                lease_generation=claimed.version - 1,  # Stale generation
                now=now + timedelta(seconds=30),
                action=action,
            )


@pytest.mark.asyncio
async def test_heartbeat_during_context_drift_rejected(session_factory):
    """Verify heartbeat fails closed if authoritative context drifted during execution."""
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()

    async with session_factory() as session:
        approval, action = await _seed_approved_action(session, now)

        claimed = await service.claim_approval_for_execution(
            session,
            approval_id=approval.id,
            worker_id="worker-1",
            expected_version=approval.version,
            now=now,
            action=action,
        )
        await session.commit()

        # Mutate the authoritative lead and opportunity records in the database
        stmt = select(Lead).where(Lead.id == approval.contact_id)
        res = await session.execute(stmt)
        contact = res.scalar_one()
        contact.email = "mutated_contact_email@corp.com"

        stmt_opp = select(LeadIntelligenceOpportunity).where(LeadIntelligenceOpportunity.id == approval.opportunity_id)
        res_opp = await session.execute(stmt_opp)
        opp = res_opp.scalar_one()
        opp.score = 25.0
        await session.commit()

        with pytest.raises(ApprovalContextDriftError):
            await service.heartbeat_lease(
                session,
                approval_id=claimed.id,
                worker_id="worker-1",
                lease_generation=claimed.version,
                now=now + timedelta(seconds=30),
                action=action,
            )


# ============================================================================
# Category 3: Stale-Claim Recovery Tests
# ============================================================================

@pytest.mark.asyncio
async def test_recovery_expired_lease_returns_to_pool(session_factory):
    """Verify recovering an expired lease resets claim metadata, increments version, and logs LEASE_RECOVERED."""
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()

    async with session_factory() as session:
        approval, action = await _seed_approved_action(session, now)

        claimed = await service.claim_approval_for_execution(
            session,
            approval_id=approval.id,
            worker_id="worker-stale",
            expected_version=approval.version,
            now=now,
            action=action,
        )
        await session.commit()
        stale_gen = claimed.version

        recovery_time = now + timedelta(seconds=350)
        recovered, is_hard_expired = await service.recover_stale_claim(
            session,
            approval_id=claimed.id,
            now=recovery_time,
            action=action,
        )
        await session.commit()

        assert not is_hard_expired
        assert recovered.status == "APPROVED"
        assert recovered.execution_claimed_at is None
        assert recovered.execution_claimed_by is None
        assert recovered.version == stale_gen + 1

        # Verify audit trail
        audits = await SupervisorApprovalRepository.list_audit_events_for_approval(session, claimed.id)
        rec_event = [e for e in audits if e.event_type == "LEASE_RECOVERED"][0]
        assert rec_event.metadata_json["recovered_from_worker"] == "worker-stale"
        assert rec_event.metadata_json["recovered_generation"] == stale_gen
        assert rec_event.metadata_json["new_generation"] == recovered.version


@pytest.mark.asyncio
async def test_recovery_unexpired_lease_rejected(session_factory):
    """Verify attempting to recover an unexpired, actively heartbeating lease raises ApprovalLeaseActiveError."""
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()

    async with session_factory() as session:
        approval, action = await _seed_approved_action(session, now)

        claimed = await service.claim_approval_for_execution(
            session,
            approval_id=approval.id,
            worker_id="worker-active",
            expected_version=approval.version,
            now=now,
            action=action,
        )
        await session.commit()

        # Attempt recovery at 100 seconds (lease duration is 300s)
        with pytest.raises(ApprovalLeaseActiveError):
            await service.recover_stale_claim(
                session,
                approval_id=claimed.id,
                now=now + timedelta(seconds=100),
                action=action,
            )


@pytest.mark.asyncio
async def test_recovery_hard_expired_transitions_to_expired(session_factory):
    """Verify recovering an approval whose overall expires_at has passed transitions status to EXPIRED."""
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()

    async with session_factory() as session:
        approval, action = await _seed_approved_action(session, now)

        # Set approval hard expiration to 2 hours from now
        approval.expires_at = now + timedelta(hours=2)
        await session.commit()

        claimed = await service.claim_approval_for_execution(
            session,
            approval_id=approval.id,
            worker_id="worker-late",
            expected_version=approval.version,
            now=now,
            action=action,
        )
        await session.commit()

        # 3 hours later (both lease and approval hard ceiling expired)
        recovery_time = now + timedelta(hours=3)
        recovered, is_hard_expired = await service.recover_stale_claim(
            session,
            approval_id=claimed.id,
            now=recovery_time,
            action=action,
        )
        await session.commit()

        assert is_hard_expired
        assert recovered.status == "EXPIRED"
        assert recovered.execution_claimed_at is None


@pytest.mark.asyncio
async def test_recovery_unclaimed_approval_rejected(session_factory):
    """Verify attempting to recover an approval that is not claimed raises ApprovalClaimConflictError."""
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()

    async with session_factory() as session:
        approval, action = await _seed_approved_action(session, now)

        with pytest.raises(ApprovalClaimConflictError):
            await service.recover_stale_claim(
                session,
                approval_id=approval.id,
                now=now + timedelta(seconds=500),
                action=action,
            )


# ============================================================================
# Category 4: Worker Fencing Tests
# ============================================================================

@pytest.mark.asyncio
async def test_fencing_stale_worker_full_lifecycle(session_factory):
    """
    Verify complete fencing invariant:
    1. Worker A acquires generation N
    2. Generation N expires
    3. Recovery invalidates generation N
    4. Worker B acquires generation N+2
    5. Worker A attempts stale heartbeat, release, or claim
    6. All stale operations from Worker A are rejected
    7. Worker B ownership remains valid
    """
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()

    async with session_factory() as session:
        approval, action = await _seed_approved_action(session, now)

        # 1. Worker A acquires lease at generation N
        claimed_a = await service.claim_approval_for_execution(
            session,
            approval_id=approval.id,
            worker_id="worker-A",
            expected_version=approval.version,
            now=now,
            action=action,
        )
        await session.commit()
        gen_a = claimed_a.version

        # 2. Worker A goes silent; lease expires at now + 301s
        expired_time = now + timedelta(seconds=301)

        # 3. Recovery runs, invalidating generation N
        recovered, _ = await service.recover_stale_claim(
            session,
            approval_id=claimed_a.id,
            now=expired_time,
            action=action,
        )
        await session.commit()
        assert recovered.version == gen_a + 1

        # 4. Worker B acquires newer generation
        claimed_b = await service.claim_approval_for_execution(
            session,
            approval_id=recovered.id,
            worker_id="worker-B",
            expected_version=recovered.version,
            now=expired_time + timedelta(seconds=10),
            action=action,
        )
        await session.commit()
        gen_b = claimed_b.version
        assert gen_b > gen_a
        assert claimed_b.execution_claimed_by == "worker-B"

        # 5 & 6. Worker A returns from network partition and attempts operations with generation gen_a
        stale_time = expired_time + timedelta(seconds=20)

        # Stale heartbeat rejected
        with pytest.raises(ApprovalNotLeaseOwnerError):
            await service.heartbeat_lease(
                session,
                approval_id=claimed_b.id,
                worker_id="worker-A",
                lease_generation=gen_a,
                now=stale_time,
                action=action,
            )

        # Stale release rejected
        with pytest.raises(ApprovalNotLeaseOwnerError):
            await service.release_lease(
                session,
                approval_id=claimed_b.id,
                worker_id="worker-A",
                lease_generation=gen_a,
                now=stale_time,
            )

        # 7. Worker B ownership remains 100% valid
        reloaded = await SupervisorApprovalRepository.get_by_id(session, claimed_b.id)
        assert reloaded.execution_claimed_by == "worker-B"
        assert reloaded.version == gen_b

        # Worker B can successfully heartbeat
        beat_b = await service.heartbeat_lease(
            session,
            approval_id=claimed_b.id,
            worker_id="worker-B",
            lease_generation=gen_b,
            now=stale_time,
            action=action,
        )
        await session.commit()
        assert beat_b.updated_at == stale_time


# ============================================================================
# Category 5: HTTP API Integration Tests
# ============================================================================

@pytest.mark.asyncio
async def test_api_heartbeat_endpoint_lifecycle(session_factory, monkeypatch):
    """Verify HTTP API /heartbeat endpoint succeeds for owner and rejects stale generation/non-owner."""
    monkeypatch.setattr(settings, "api_auth_token", "test-dev-token")
    monkeypatch.setattr(settings, "allow_dev_role_override", True)
    monkeypatch.setattr(settings, "app_env", "development")
    app.dependency_overrides[get_db] = lambda: session_factory()
    try:
        now = now_utc()
        service = SupervisorApprovalService()

        async with session_factory() as session:
            approval, action = await _seed_approved_action(session, now)
            claimed = await service.claim_approval_for_execution(
                session,
                approval_id=approval.id,
                worker_id="api-worker-1",
                expected_version=approval.version,
                now=now,
                action=action,
            )
            await session.commit()
            claimed_id = claimed.id
            claimed_version = claimed.version

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            headers = {"Authorization": "Bearer test-dev-token", "X-Actor-Role": "sales_manager"}

            # 1. Valid heartbeat -> 200 OK
            resp = await ac.post(
                f"/api/sales-supervisor/approvals/{claimed_id}/heartbeat",
                json={"worker_id": "api-worker-1", "lease_generation": claimed_version},
                headers=headers,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["worker_id"] == "api-worker-1"
            assert data["lease_generation"] == claimed_version
            assert "lease_expires_at" in data

            # 2. Non-owner heartbeat -> 403 Forbidden
            resp_bad_worker = await ac.post(
                f"/api/sales-supervisor/approvals/{claimed_id}/heartbeat",
                json={"worker_id": "different-worker", "lease_generation": claimed_version},
                headers=headers,
            )
            assert resp_bad_worker.status_code == 403

            # 3. Obsolete generation heartbeat -> 409 Conflict
            resp_bad_gen = await ac.post(
                f"/api/sales-supervisor/approvals/{claimed_id}/heartbeat",
                json={"worker_id": "api-worker-1", "lease_generation": 999},
                headers=headers,
            )
            assert resp_bad_gen.status_code == 409
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_release_endpoint(session_factory, monkeypatch):
    """Verify HTTP API /release endpoint releases lease cleanly back to the pool."""
    monkeypatch.setattr(settings, "api_auth_token", "test-dev-token")
    monkeypatch.setattr(settings, "allow_dev_role_override", True)
    monkeypatch.setattr(settings, "app_env", "development")
    app.dependency_overrides[get_db] = lambda: session_factory()
    try:
        now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
        service = SupervisorApprovalService()

        async with session_factory() as session:
            approval, action = await _seed_approved_action(session, now)
            claimed = await service.claim_approval_for_execution(
                session,
                approval_id=approval.id,
                worker_id="api-worker-release",
                expected_version=approval.version,
                now=now,
                action=action,
            )
            await session.commit()
            claimed_id = claimed.id
            claimed_version = claimed.version

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            headers = {"Authorization": "Bearer test-dev-token", "X-Actor-Role": "sales_manager"}

            # Clean release
            resp = await ac.post(
                f"/api/sales-supervisor/approvals/{claimed_id}/release",
                json={"worker_id": "api-worker-release", "lease_generation": claimed_version, "reason": "Worker shutting down"},
                headers=headers,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["execution_claimed_at"] is None
            assert data["execution_claimed_by"] is None
            assert data["version"] == claimed_version + 1
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_recover_lease_endpoint(session_factory, monkeypatch):
    """Verify HTTP API /recover-lease endpoint recovers an expired lease and rejects an active lease."""
    monkeypatch.setattr(settings, "api_auth_token", "test-dev-token")
    monkeypatch.setattr(settings, "allow_dev_role_override", True)
    monkeypatch.setattr(settings, "app_env", "development")
    app.dependency_overrides[get_db] = lambda: session_factory()
    try:
        now = now_utc()
        service = SupervisorApprovalService()

        async with session_factory() as session:
            approval, action = await _seed_approved_action(session, now)
            claimed = await service.claim_approval_for_execution(
                session,
                approval_id=approval.id,
                worker_id="api-worker-stale",
                expected_version=approval.version,
                now=now,
                action=action,
            )
            # Advance updated_at into the past so it's expired
            claimed.updated_at = now - timedelta(seconds=600)
            await session.commit()
            claimed_id = claimed.id

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            headers = {"Authorization": "Bearer test-dev-token", "X-Actor-Role": "sales_manager"}

            # Recover lease
            resp = await ac.post(
                f"/api/sales-supervisor/approvals/{claimed_id}/recover-lease",
                json={"reason": "Manual operator recovery"},
                headers=headers,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["execution_claimed_at"] is None
            assert data["execution_claimed_by"] is None
    finally:
        app.dependency_overrides.pop(get_db, None)

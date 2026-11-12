"""
Comprehensive test suite for Phase 12J-D2-B:
Durable Approval Repository and Service Integration.

Covers:
1. Repository CRUD, queries, and session isolation.
2. Approval lifecycle transitions, terminal states, and transition guards.
3. Testable clock and boundary expiration semantics.
4. Cryptographic context binding, normalization, and context drift invalidation.
5. Atomic compare-and-swap (CAS) execution claiming and concurrency safety.
6. Forensic audit trail transactionality and retention.
7. Role and authentication trust boundaries.
8. Execution authorization for external outreach vs internal operations.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.domain.contact_recommendation import RecommendedSalesAction
from app.domain.sales_supervisor import (
    ApprovalClaimConflictError,
    ApprovalContextDriftError,
    ApprovalExpiredError,
    ApprovalInvalidTransitionError,
    ApprovalNotFoundError,
    ApprovalPermissionDeniedError,
    ApprovalSelfApprovalError,
    ApprovalUnverifiedIdentityError,
    ApprovalVersionConflictError,
    AuthenticatedPrincipal,
    SupervisorActionReadinessStatus,
    SupervisorActionTier,
    SupervisorApprovalStatus,
    SupervisorExecutionDecision,
    compute_action_context_fingerprint,
    validate_approval_transition,
)
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
    SupervisorExecutionAuthorizationResult,
)
from app.services.supervisor_approval_service import SupervisorApprovalService, _ensure_utc


def _make_dummy_action(
    lead_id: uuid.UUID,
    opp_id: uuid.UUID,
    contact_id: uuid.UUID | None = None,
    action: RecommendedSalesAction = RecommendedSalesAction.PERSONALIZED_EMAIL,
    readiness_status: SupervisorActionReadinessStatus = SupervisorActionReadinessStatus.READY,
    opp_score: float = 85.0,
    rec_fit: float = 90.0,
) -> PrioritizedSalesAction:
    """Helper to create a deterministic PrioritizedSalesAction candidate for testing."""
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
            reasons=["All readiness gates passed" if readiness_status == SupervisorActionReadinessStatus.READY else "Gate failed"],
            has_valid_contact=True,
            has_evidence=True,
            is_opted_out=readiness_status == SupervisorActionReadinessStatus.BLOCKED_OPT_OUT,
            in_cooldown=readiness_status == SupervisorActionReadinessStatus.BLOCKED_COOLDOWN,
            has_active_task=readiness_status == SupervisorActionReadinessStatus.ACTIVE_TASK_EXISTS,
        ),
        evidence_keys=[f"opportunity:{opp_id}", f"contact:{cid_str}"],
        policy_approval_required=True,
    )


# ============================================================================
# Category 1: Repository Tests
# ============================================================================

@pytest.mark.asyncio
async def test_repository_create_and_retrieve_approval(session_factory):
    """Verify repository creates PENDING approval and initial audit event, and retrieves across sessions."""
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    contact_id = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)

    # 1. Create in session 1
    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Test Repo Corp")
        session.add(lead)
        await session.commit()

        approval = await SupervisorApprovalRepository.create_approval(
            session,
            action_id="act-repo-1",
            lead_id=lead_id,
            opportunity_id=opp_id,
            contact_id=contact_id,
            recommended_action="PERSONALIZED_EMAIL",
            action_tier="EXTERNAL_OUTREACH",
            channel="EMAIL",
            context_fingerprint="fp-repo-hash-1",
            requested_by_user_id="rep-alice",
            expires_in_hours=48,
            notes="Initial request",
            now=now,
        )
        await session.commit()
        approval_id = approval.id

    # 2. Retrieve in session 2 (cross-session isolation verification)
    async with session_factory() as session:
        loaded = await SupervisorApprovalRepository.get_by_id(session, approval_id)
        assert loaded is not None
        assert loaded.id == approval_id
        assert loaded.status == "PENDING"
        assert loaded.version == 1
        assert loaded.requested_by_user_id == "rep-alice"
        assert _ensure_utc(loaded.expires_at) == now + timedelta(hours=48)
        assert loaded.notes == "Initial request"

        # Check initial CREATED audit event
        audit_events = await SupervisorApprovalRepository.list_audit_events_for_approval(session, approval_id)
        assert len(audit_events) == 1
        assert audit_events[0].event_type == "CREATED"
        assert audit_events[0].from_status is None
        assert audit_events[0].to_status == "PENDING"
        assert audit_events[0].actor_id == "rep-alice"
        assert audit_events[0].approval_reference_id == approval_id


@pytest.mark.asyncio
async def test_repository_active_approved_and_uniqueness_conflict(session_factory):
    """Verify repository partial uniqueness: only 1 APPROVED status per action & fingerprint."""
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Uniqueness Corp")
        session.add(lead)
        await session.commit()

        app1 = await SupervisorApprovalRepository.create_approval(
            session,
            action_id="act-unique-1",
            lead_id=lead_id,
            opportunity_id=opp_id,
            recommended_action="DISCOVERY_CALL",
            action_tier="EXTERNAL_OUTREACH",
            channel="PHONE",
            context_fingerprint="fp-unique-1",
            requested_by_user_id="rep-1",
            now=now,
        )
        app2 = await SupervisorApprovalRepository.create_approval(
            session,
            action_id="act-unique-1",
            lead_id=lead_id,
            opportunity_id=opp_id,
            recommended_action="DISCOVERY_CALL",
            action_tier="EXTERNAL_OUTREACH",
            channel="PHONE",
            context_fingerprint="fp-unique-1",
            requested_by_user_id="rep-2",
            now=now,
        )
        await session.commit()
        app1_id = app1.id
        app2_id = app2.id

    # Approve app1
    async with session_factory() as session:
        await SupervisorApprovalRepository.transition_status(
            session,
            approval_id=app1_id,
            to_status="APPROVED",
            actor_id="mgr-jane",
            actor_role="sales_manager",
            reason="Approved for call window",
            now=now,
        )
        await session.commit()

    # Attempt to approve app2 with identical action_id & context_fingerprint
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await SupervisorApprovalRepository.transition_status(
                session,
                approval_id=app2_id,
                to_status="APPROVED",
                actor_id="mgr-jane",
                actor_role="sales_manager",
                reason="Duplicate approval attempt",
                now=now,
            )
            await session.commit()


# ============================================================================
# Category 2: Lifecycle & Transition Tests
# ============================================================================

@pytest.mark.asyncio
async def test_lifecycle_pending_to_approved_to_revoked(session_factory):
    """Verify legal transition: PENDING -> APPROVED -> REVOKED, and verify audit records."""
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()
    rep = AuthenticatedPrincipal(user_id="rep-sam", role="sales_rep")
    mgr = AuthenticatedPrincipal(user_id="mgr-karen", role="sales_manager")

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Lifecycle Dynamics")
        session.add(lead)
        await session.commit()

        action = _make_dummy_action(lead_id, opp_id)

        # 1. Request approval (PENDING)
        app = await service.request_approval(
            session,
            action=action,
            requested_by=rep,
            channel="EMAIL",
            notes="Ready for senior review",
            now=now,
        )
        await session.commit()
        app_id = app.id
        assert app.status == "PENDING"
        assert app.version == 1

        # 2. Approve (APPROVED)
        app_approved = await service.approve_action(
            session,
            approval_id=app_id,
            approver=mgr,
            decision_reason="Account verified in ICP",
            expected_version=1,
            now=now + timedelta(hours=1),
        )
        await session.commit()
        assert app_approved.status == "APPROVED"
        assert app_approved.version == 2
        assert app_approved.approved_by_user_id == "mgr-karen"

        # 3. Revoke (REVOKED)
        app_revoked = await service.revoke_approval(
            session,
            approval_id=app_id,
            actor=mgr,
            revocation_reason="Lead requested pause on outreach",
            expected_version=2,
            now=now + timedelta(hours=2),
        )
        await session.commit()
        assert app_revoked.status == "REVOKED"
        assert app_revoked.version == 3
        assert app_revoked.revoked_by_user_id == "mgr-karen"

        # 4. Verify 3 sequential audit records
        audit_events = await SupervisorApprovalRepository.list_audit_events_for_approval(session, app_id)
        assert len(audit_events) == 3
        assert [e.event_type for e in audit_events] == ["CREATED", "APPROVED", "REVOKED"]
        assert [e.from_status for e in audit_events] == [None, "PENDING", "APPROVED"]
        assert [e.to_status for e in audit_events] == ["PENDING", "APPROVED", "REVOKED"]


@pytest.mark.asyncio
async def test_lifecycle_terminal_states_cannot_be_resurrected(session_factory):
    """Verify that terminal states (REJECTED, REVOKED, EXPIRED) reject all further transitions."""
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()
    rep = AuthenticatedPrincipal(user_id="rep-sam", role="sales_rep")
    mgr = AuthenticatedPrincipal(user_id="mgr-karen", role="sales_manager")

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Terminal State LLC")
        session.add(lead)
        await session.commit()

        action = _make_dummy_action(lead_id, opp_id)

        # Create and Reject
        app = await service.request_approval(session, action=action, requested_by=rep, now=now)
        await session.commit()

        await service.reject_action(
            session,
            approval_id=app.id,
            approver=mgr,
            decision_reason="Bad timing",
            now=now,
        )
        await session.commit()

        # Attempt to approve a REJECTED approval -> Must raise ApprovalInvalidTransitionError
        with pytest.raises(ApprovalInvalidTransitionError):
            await service.approve_action(
                session,
                approval_id=app.id,
                approver=mgr,
                decision_reason="Reconsidered",
                now=now + timedelta(hours=1),
            )

        # Attempt to revoke a REJECTED approval -> Must raise ApprovalInvalidTransitionError
        with pytest.raises(ApprovalInvalidTransitionError):
            await service.revoke_approval(
                session,
                approval_id=app.id,
                actor=mgr,
                revocation_reason="Revoking rejection",
                now=now + timedelta(hours=1),
            )


@pytest.mark.asyncio
async def test_lifecycle_expiration_boundary_semantics(session_factory):
    """Verify expiration evaluation before, at, and after the expiry boundary."""
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    t0 = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    validity_hours = 24
    expiry_boundary = t0 + timedelta(hours=validity_hours)

    service = SupervisorApprovalService()
    rep = AuthenticatedPrincipal(user_id="rep-1", role="sales_rep")
    mgr = AuthenticatedPrincipal(user_id="mgr-1", role="sales_manager")

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Expiry Test Corp")
        session.add(lead)
        await session.commit()

        action = _make_dummy_action(lead_id, opp_id)
        app = await service.request_approval(
            session,
            action=action,
            requested_by=rep,
            channel="EMAIL",
            expires_in_hours=validity_hours,
            now=t0,
        )
        await service.approve_action(
            session,
            approval_id=app.id,
            approver=mgr,
            expires_in_hours=validity_hours,
            now=t0,
        )
        await session.commit()

        # 1. 1 second BEFORE expiry boundary -> Authorized
        res_before = await service.authorize_action_execution(
            session,
            action=action,
            channel="EMAIL",
            now=expiry_boundary - timedelta(seconds=1),
        )
        assert res_before.authorized is True
        assert res_before.decision == SupervisorExecutionDecision.AUTHORIZED

        # 2. 1 second AFTER expiry boundary -> Fails closed with APPROVAL_EXPIRED
        res_after = await service.authorize_action_execution(
            session,
            action=action,
            channel="EMAIL",
            now=expiry_boundary + timedelta(seconds=1),
        )
        assert res_after.authorized is False
        assert res_after.decision == SupervisorExecutionDecision.DENIED
        assert res_after.denial_code == "APPROVAL_EXPIRED"


# ============================================================================
# Category 3: Context Binding & Drift Tests
# ============================================================================

def test_canonical_fingerprint_stability_and_determinism():
    """Verify compute_action_context_fingerprint produces identical hashes for equivalent inputs."""
    fp1 = compute_action_context_fingerprint(
        action_id="act-fp-1",
        action_type="PERSONALIZED_EMAIL",
        lead_id="11111111-1111-1111-1111-111111111111",
        opportunity_id="22222222-2222-2222-2222-222222222222",
        contact_id="33333333-3333-3333-3333-333333333333",
        channel="email",
        contact_email="User@Domain.COM ",  # uppercase with trailing whitespace
        contact_phone="+1 (555) 019-2834",
        opportunity_score=85.000,
        recommendation_fit=92.5,
        evidence_keys=["opp:1", "signal:2"],
    )
    fp2 = compute_action_context_fingerprint(
        action_id="act-fp-1 ",
        action_type="personalized_email",
        lead_id="11111111-1111-1111-1111-111111111111",
        opportunity_id="22222222-2222-2222-2222-222222222222",
        contact_id="33333333-3333-3333-3333-333333333333",
        channel="EMAIL",
        contact_email="user@domain.com",  # lowercase cleaned
        contact_phone="+15550192834",     # digits and plus only
        opportunity_score=85.0,
        recommendation_fit=92.50,
        evidence_keys=["signal:2", "opp:1"],  # reversed order
    )
    assert fp1 == fp2, "Canonicalization failed to produce identical hashes for equivalent inputs"


@pytest.mark.asyncio
async def test_context_drift_causes_fail_closed_denial(session_factory):
    """Verify that any material change to email, phone, action, or scores triggers CONTEXT_DRIFT."""
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    cid = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()
    rep = AuthenticatedPrincipal(user_id="rep-1", role="sales_rep")
    mgr = AuthenticatedPrincipal(user_id="mgr-1", role="sales_manager")

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Drift Test Corp")
        session.add(lead)
        await session.commit()

        action = _make_dummy_action(lead_id, opp_id, contact_id=cid)
        app = await service.request_approval(
            session,
            action=action,
            requested_by=rep,
            channel="EMAIL",
            contact_email="original_sdr@client.com",
            now=now,
        )
        await service.approve_action(session, approval_id=app.id, approver=mgr, now=now)
        await session.commit()

        # 1. Matching context -> Authorized
        res_ok = await service.authorize_action_execution(
            session,
            action=action,
            channel="EMAIL",
            contact_email="original_sdr@client.com",
            now=now,
        )
        assert res_ok.authorized is True

        # 2. Changed contact email -> CONTEXT_DRIFT
        res_drift_email = await service.authorize_action_execution(
            session,
            action=action,
            channel="EMAIL",
            contact_email="different_recipient@client.com",
            now=now,
        )
        assert res_drift_email.authorized is False
        assert res_drift_email.denial_code == "CONTEXT_DRIFT"

        # 3. Changed channel -> CONTEXT_DRIFT
        res_drift_channel = await service.authorize_action_execution(
            session,
            action=action,
            channel="PHONE",
            contact_email="original_sdr@client.com",
            now=now,
        )
        assert res_drift_channel.authorized is False
        assert res_drift_channel.denial_code == "CONTEXT_DRIFT"

        # 4. Changed opportunity score -> CONTEXT_DRIFT
        action_mutated = _make_dummy_action(lead_id, opp_id, contact_id=cid, opp_score=40.0)
        res_drift_score = await service.authorize_action_execution(
            session,
            action=action_mutated,
            channel="EMAIL",
            contact_email="original_sdr@client.com",
            now=now,
        )
        assert res_drift_score.authorized is False
        assert res_drift_score.denial_code == "CONTEXT_DRIFT"


# ============================================================================
# Category 4: Concurrency & CAS Execution Claiming
# ============================================================================

@pytest.mark.asyncio
async def test_concurrent_claim_second_worker_fails_closed(session_factory):
    """Verify atomic CAS execution claim: exactly one worker succeeds, second worker gets ApprovalClaimConflictError."""
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()
    rep = AuthenticatedPrincipal(user_id="rep-1", role="sales_rep")
    mgr = AuthenticatedPrincipal(user_id="mgr-1", role="sales_manager")

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Claim Test LLC", email="claim@test.com", phone="+15551234567")
        session.add(lead)
        opp = LeadIntelligenceOpportunity(
            id=opp_id,
            lead_id=lead_id,
            title="Claim Test Opportunity",
            score=85,
            recommended_action="PERSONALIZED_EMAIL",
            evidence={"seo_score": "poor"},
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add(opp)
        await session.commit()

        action = PrioritizedSalesAction(
            action_id=f"contact_recommendation:{lead_id}:PERSONALIZED_EMAIL",
            lead_id=lead_id,
            opportunity_id=opp_id,
            contact_id=None,
            recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
            opportunity_score=85.0,
            recommendation_fit=0.0,
            urgency_score=50.0,
            priority_score=85.0,
            readiness_status=SupervisorActionReadinessStatus.READY,
            readiness_details=ActionReadinessDetails(
                status=SupervisorActionReadinessStatus.READY,
                reasons=["All readiness gates passed"],
                has_valid_contact=True,
                has_evidence=True,
            ),
            evidence_keys=["seo_score"],
        )
        app = await service.request_approval(
            session,
            action=action,
            requested_by=rep,
            channel="EMAIL",
            contact_email="claim@test.com",
            contact_phone="+15551234567",
            now=now,
        )
        approved_app = await service.approve_action(session, approval_id=app.id, approver=mgr, now=now)
        await session.commit()
        app_id = approved_app.id
        expected_version = approved_app.version

    # Worker 1 claims with expected version
    async with session_factory() as session1:
        claimed1 = await service.claim_approval_for_execution(
            session1,
            approval_id=app_id,
            worker_id="worker-pod-alpha",
            expected_version=expected_version,
            now=now,
        )
        await session1.commit()
        assert claimed1.execution_claimed_by == "worker-pod-alpha"
        assert claimed1.version == expected_version + 1

    # Worker 2 attempts concurrent claim with original expected version -> Fails closed
    async with session_factory() as session2:
        with pytest.raises(ApprovalClaimConflictError):
            await service.claim_approval_for_execution(
                session2,
                approval_id=app_id,
                worker_id="worker-pod-beta",
                expected_version=expected_version,
                now=now,
            )

    # Worker 2 attempts claim with updated version -> Also fails because execution_claimed_at is already set!
    async with session_factory() as session3:
        with pytest.raises(ApprovalClaimConflictError):
            await service.claim_approval_for_execution(
                session3,
                approval_id=app_id,
                worker_id="worker-pod-beta",
                expected_version=expected_version + 1,
                now=now,
            )

    # Verify CLAIMED audit event was recorded
    async with session_factory() as session4:
        audit_events = await SupervisorApprovalRepository.list_audit_events_for_approval(session4, app_id)
        claim_events = [e for e in audit_events if e.event_type == "CLAIMED"]
        assert len(claim_events) == 1
        assert claim_events[0].actor_id == "worker-pod-alpha"
        assert claim_events[0].actor_role == "SYSTEM_WORKER"


@pytest.mark.asyncio
async def test_optimistic_concurrency_version_conflict(session_factory):
    """Verify that updating an approval with stale expected_version raises ApprovalVersionConflictError."""
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()
    rep = AuthenticatedPrincipal(user_id="rep-1", role="sales_rep")
    mgr = AuthenticatedPrincipal(user_id="mgr-1", role="sales_manager")

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Version Conflict LLC")
        session.add(lead)
        await session.commit()

        action = _make_dummy_action(lead_id, opp_id)
        app = await service.request_approval(session, action=action, requested_by=rep, now=now)
        await session.commit()
        app_id = app.id

        # Pass wrong expected_version (e.g. version=99 instead of 1)
        with pytest.raises(ApprovalVersionConflictError) as exc_info:
            await service.approve_action(
                session,
                approval_id=app_id,
                approver=mgr,
                expected_version=99,
                now=now,
            )
        assert exc_info.value.expected_version == 99
        assert exc_info.value.actual_version == 1


# ============================================================================
# Category 5: Readiness Gate Precedence Tests
# ============================================================================

@pytest.mark.asyncio
async def test_approval_cannot_override_blocked_readiness_gates(session_factory):
    """Verify that a valid active approval NEVER overrides a BLOCKED readiness gate."""
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()
    rep = AuthenticatedPrincipal(user_id="rep-1", role="sales_rep")
    mgr = AuthenticatedPrincipal(user_id="mgr-1", role="sales_manager")

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Precedence Corp")
        session.add(lead)
        await session.commit()

        # Create action originally READY
        action_ready = _make_dummy_action(lead_id, opp_id, readiness_status=SupervisorActionReadinessStatus.READY)
        app = await service.request_approval(session, action=action_ready, requested_by=rep, now=now)
        await service.approve_action(session, approval_id=app.id, approver=mgr, now=now)
        await session.commit()

        # 1. Action readiness transitions to BLOCKED_OPT_OUT
        action_opt_out = _make_dummy_action(lead_id, opp_id, readiness_status=SupervisorActionReadinessStatus.BLOCKED_OPT_OUT)
        res_opt = await service.authorize_action_execution(session, action=action_opt_out, now=now)
        assert res_opt.authorized is False
        assert res_opt.decision == SupervisorExecutionDecision.DENIED
        assert res_opt.denial_code == "READINESS_BLOCKED_OPT_OUT"

        # 2. Action readiness transitions to ACTIVE_TASK_EXISTS
        action_colliding = _make_dummy_action(lead_id, opp_id, readiness_status=SupervisorActionReadinessStatus.ACTIVE_TASK_EXISTS)
        res_coll = await service.authorize_action_execution(session, action=action_colliding, now=now)
        assert res_coll.authorized is False
        assert res_coll.decision == SupervisorExecutionDecision.DENIED
        assert res_coll.denial_code == "READINESS_ACTIVE_TASK_EXISTS"

        # 3. Action readiness transitions to BLOCKED_COOLDOWN
        action_cooldown = _make_dummy_action(lead_id, opp_id, readiness_status=SupervisorActionReadinessStatus.BLOCKED_COOLDOWN)
        res_cd = await service.authorize_action_execution(session, action=action_cooldown, now=now)
        assert res_cd.authorized is False
        assert res_cd.decision == SupervisorExecutionDecision.DENIED
        assert res_cd.denial_code == "READINESS_BLOCKED_COOLDOWN"


# ============================================================================
# Category 6: Internal Operation vs External Outreach Boundaries
# ============================================================================

@pytest.mark.asyncio
async def test_internal_operation_authorization_and_human_veto(session_factory):
    """Verify internal operations are authorized without human signature, but respect explicit human veto."""
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()
    mgr = AuthenticatedPrincipal(user_id="mgr-1", role="sales_manager")

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Internal Ops LLC")
        session.add(lead)
        await session.commit()

        internal_action = _make_dummy_action(
            lead_id,
            opp_id,
            action=RecommendedSalesAction.RESEARCH_CONTACT,
            readiness_status=SupervisorActionReadinessStatus.READY,
        )

        # 1. System authorizes without any human approval record
        res1 = await service.authorize_action_execution(session, action=internal_action, now=now)
        assert res1.authorized is True
        assert res1.decision == SupervisorExecutionDecision.AUTHORIZED
        assert res1.action_tier == SupervisorActionTier.INTERNAL_OPERATION
        assert res1.approval_required is False

        # 2. Manager registers an explicit REJECTED decision
        rep = AuthenticatedPrincipal(user_id="rep-1", role="sales_rep")
        app = await service.request_approval(session, action=internal_action, requested_by=rep, now=now)
        await service.reject_action(session, approval_id=app.id, approver=mgr, decision_reason="Lead already well researched", now=now)
        await session.commit()

        # 3. Authorization is now DENIED due to human supervisor explicit veto
        res2 = await service.authorize_action_execution(session, action=internal_action, now=now)
        assert res2.authorized is False
        assert res2.decision == SupervisorExecutionDecision.DENIED
        assert res2.denial_code == "APPROVAL_REJECTED"


# ============================================================================
# Category 7: Role and Permission Boundary Tests
# ============================================================================

@pytest.mark.asyncio
async def test_role_and_permission_enforcement(session_factory):
    """Verify unauthenticated callers and non-manager roles cannot approve, reject, or revoke."""
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()
    rep = AuthenticatedPrincipal(user_id="rep-1", role="sales_rep")
    unauth = AuthenticatedPrincipal(user_id="anon", role="sales_rep", is_authenticated=False)

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Auth Boundary Corp")
        session.add(lead)
        await session.commit()

        action = _make_dummy_action(lead_id, opp_id)

        # 1. Unauthenticated principal cannot request approval
        with pytest.raises(ApprovalPermissionDeniedError):
            await service.request_approval(session, action=action, requested_by=unauth, now=now)

        app = await service.request_approval(session, action=action, requested_by=rep, now=now)
        await session.commit()

        # 2. Sales rep cannot approve (requires sales_manager or admin)
        with pytest.raises(ApprovalPermissionDeniedError):
            await service.approve_action(session, approval_id=app.id, approver=rep, now=now)

        # 3. Sales rep cannot reject
        with pytest.raises(ApprovalPermissionDeniedError):
            await service.reject_action(
                session,
                approval_id=app.id,
                approver=rep,
                decision_reason="Rep rejecting",
                now=now,
            )


# ============================================================================
# Category 8: Sweeper for Expired Records
# ============================================================================

@pytest.mark.asyncio
async def test_sweeper_transitions_expired_approvals_with_audit_events(session_factory):
    """Verify background sweeper identifies past-due approvals and transitions them to EXPIRED with audit log."""
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    t0 = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()
    rep = AuthenticatedPrincipal(user_id="rep-1", role="sales_rep")

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Sweeper Test Corp")
        session.add(lead)
        await session.commit()

        action = _make_dummy_action(lead_id, opp_id)
        # Expires in 2 hours
        app = await service.request_approval(session, action=action, requested_by=rep, expires_in_hours=2, now=t0)
        await session.commit()
        app_id = app.id

        # At t0 + 1 hour -> Not swept
        swept_early = await SupervisorApprovalRepository.sweep_expired_approvals(session, now=t0 + timedelta(hours=1))
        await session.commit()
        assert len(swept_early) == 0

        # At t0 + 3 hours -> Swept to EXPIRED
        swept_late = await SupervisorApprovalRepository.sweep_expired_approvals(session, now=t0 + timedelta(hours=3))
        await session.commit()
        assert len(swept_late) == 1
        assert swept_late[0].id == app_id
        assert swept_late[0].status == "EXPIRED"

        # Verify audit trail contains EXPIRED event
        events = await SupervisorApprovalRepository.list_audit_events_for_approval(session, app_id)
        assert any(e.event_type == "EXPIRED" for e in events)


# ============================================================================
# Category 9: Audit Retention Across Lead / Approval Deletion
# ============================================================================

def test_audit_retention_via_repository_survives_lead_deletion():
    """Verify approval audit events survive deletion of associated Lead record using repository queries."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.models.entities import Base

    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON;"))
        conn.commit()
        Base.metadata.create_all(conn)
        conn.commit()

        with Session(conn) as session:
            session.execute(text("PRAGMA foreign_keys = ON;"))
            lead = Lead(company_name="Cascade Repository Retention Corp")
            session.add(lead)
            session.commit()
            lead_id = lead.id

            app_id = uuid.uuid4()
            approval = SupervisorHumanApproval(
                id=app_id,
                action_id="act-retention-repo-1",
                lead_id=lead_id,
                opportunity_id=uuid.uuid4(),
                recommended_action="SCHEDULE_DISCOVERY_CALL",
                action_tier="EXTERNAL_OUTREACH",
                channel="EMAIL",
                context_fingerprint="retention-fp-repo-1",
                requested_by_user_id="user-sys",
            )
            session.add(approval)
            session.commit()

            event = SupervisorApprovalAuditEvent(
                id=uuid.uuid4(),
                approval_id=app_id,
                approval_reference_id=app_id,
                action_id="act-retention-repo-1",
                lead_id=lead_id,
                actor_id="user-sys",
                actor_role="SUPERVISOR_ENGINE",
                event_type="CREATED",
                to_status="PENDING",
                context_fingerprint="retention-fp-repo-1",
            )
            session.add(event)
            session.commit()
            event_id = event.id

            # Delete lead -> cascades to approval -> audit event survives with approval_id = NULL
            session.delete(lead)
            session.commit()

            assert session.get(Lead, lead_id) is None
            assert session.get(SupervisorHumanApproval, app_id) is None

            survived = session.get(SupervisorApprovalAuditEvent, event_id)
            assert survived is not None
            assert survived.approval_id is None
            assert survived.approval_reference_id == app_id
            assert survived.lead_id == lead_id
    engine.dispose()


@pytest.mark.asyncio
async def test_authorize_sales_supervisor_action_durable_bridge(session_factory):
    """Verify authorize_sales_supervisor_action_durable bridge function connects seamlessly with durable service."""
    from app.services.sales_supervisor import authorize_sales_supervisor_action_durable

    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()
    rep = AuthenticatedPrincipal(user_id="rep-1", role="sales_rep")
    mgr = AuthenticatedPrincipal(user_id="mgr-1", role="sales_manager")

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Bridge Integration LLC")
        session.add(lead)
        await session.commit()

        action = _make_dummy_action(lead_id, opp_id)

        # 1. Unapproved -> REQUIRES_APPROVAL
        res_unapproved = await authorize_sales_supervisor_action_durable(session, action=action, now=now)
        assert res_unapproved.authorized is False
        assert res_unapproved.decision == SupervisorExecutionDecision.REQUIRES_APPROVAL

        # 2. Approved -> AUTHORIZED
        app = await service.request_approval(session, action=action, requested_by=rep, channel="EMAIL", now=now)
        await service.approve_action(session, approval_id=app.id, approver=mgr, now=now)
        await session.commit()

        res_approved = await authorize_sales_supervisor_action_durable(
            session, action=action, channel="EMAIL", now=now
        )
        assert res_approved.authorized is True
        assert res_approved.decision == SupervisorExecutionDecision.AUTHORIZED
        assert res_approved.approval_details is not None
        assert res_approved.approval_details.approval_id == str(app.id)


# ============================================================================
# Category 9: Gate A - Self-Approval Separation of Duties Tests
# ============================================================================

@pytest.mark.asyncio
async def test_self_approval_denied_for_identical_individual_identities(session_factory):
    """
    Gate A: When requester and approver have the same trusted stable individual identity,
    approval MUST be denied with ApprovalSelfApprovalError (SELF_APPROVAL_PROHIBITED).
    The approval record remains in PENDING status with unchanged version.
    """
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()

    # Both requester and approver represent the same individual identity (e.g. sales_mgr_alice)
    alice = AuthenticatedPrincipal(user_id="sales_mgr_alice", role="sales_manager")

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Self Approval Corp")
        session.add(lead)
        await session.commit()

        action = _make_dummy_action(lead_id, opp_id)

        # 1. Alice creates the approval request
        app = await service.request_approval(session, action=action, requested_by=alice, now=now)
        await session.commit()
        app_id = app.id
        assert app.status == "PENDING"
        assert app.version == 1
        assert app.requested_by_user_id == "sales_mgr_alice"

        # 2. Alice attempts to approve her own request -> Must raise ApprovalSelfApprovalError
        with pytest.raises(ApprovalSelfApprovalError) as exc_info:
            await service.approve_action(
                session,
                approval_id=app_id,
                approver=alice,
                decision_reason="Attempting self-approval",
                now=now,
            )
        assert exc_info.value.code == "SELF_APPROVAL_PROHIBITED"
        assert "sales_mgr_alice" in str(exc_info.value)

        # 3. Rollback session and verify database state was NOT modified
        await session.rollback()
        persisted = await service.repository.get_by_id(session, app_id)
        assert persisted.status == "PENDING"
        assert persisted.version == 1
        assert persisted.approved_by_user_id is None

        # Verify no APPROVED audit event was created
        events = await service.repository.list_audit_events_for_approval(session, app_id)
        assert len(events) == 1
        assert events[0].event_type == "CREATED"


@pytest.mark.asyncio
async def test_approval_allowed_for_distinct_individual_identities(session_factory):
    """
    Gate A: When requester and approver have different trusted stable individual identities,
    approval proceeds successfully to APPROVED status.
    """
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()

    bob = AuthenticatedPrincipal(user_id="sales_rep_bob", role="sales_rep")
    alice = AuthenticatedPrincipal(user_id="sales_mgr_alice", role="sales_manager")

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Distinct Identities Corp")
        session.add(lead)
        await session.commit()

        action = _make_dummy_action(lead_id, opp_id)

        # 1. Bob creates the request
        app = await service.request_approval(session, action=action, requested_by=bob, now=now)
        await session.commit()
        app_id = app.id

        # 2. Alice approves Bob's request -> Succeeds
        approved = await service.approve_action(
            session,
            approval_id=app_id,
            approver=alice,
            decision_reason="Verified by manager Alice",
            now=now,
        )
        await session.commit()

        assert approved.status == "APPROVED"
        assert approved.version == 2
        assert approved.approved_by_user_id == "sales_mgr_alice"
        assert approved.requested_by_user_id == "sales_rep_bob"


@pytest.mark.asyncio
async def test_self_approval_fails_closed_when_requester_identity_missing(session_factory):
    """
    Gate A: When requester identity is missing or unverified placeholder (e.g. '', 'UNKNOWN'),
    approval fails closed with ApprovalUnverifiedIdentityError (UNVERIFIED_IDENTITY_PROHIBITED).
    """
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()
    alice = AuthenticatedPrincipal(user_id="sales_mgr_alice", role="sales_manager")

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Missing Requester Corp")
        session.add(lead)
        await session.commit()

        # Create approval directly with blank/unknown requester
        app_id = uuid.uuid4()
        approval_missing_req = SupervisorHumanApproval(
            id=app_id,
            action_id="act-missing-req",
            lead_id=lead_id,
            opportunity_id=opp_id,
            recommended_action="PERSONALIZED_EMAIL",
            action_tier="EXTERNAL_OUTREACH",
            context_fingerprint="fp-missing",
            status="PENDING",
            requested_by_user_id="UNKNOWN",  # Unverified placeholder
            version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(approval_missing_req)
        await session.commit()

        # Attempt approval -> must fail closed with ApprovalUnverifiedIdentityError
        with pytest.raises(ApprovalUnverifiedIdentityError) as exc_info:
            await service.approve_action(
                session,
                approval_id=app_id,
                approver=alice,
                decision_reason="Attempting approval",
                now=now,
            )
        assert exc_info.value.code == "UNVERIFIED_IDENTITY_PROHIBITED"


@pytest.mark.asyncio
async def test_requester_can_reject_and_revoke_own_action(session_factory):
    """
    Gate A: Separation of duties restricts self-approval, but does NOT block
    the requester from rejecting or revoking their own action.
    """
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()

    alice = AuthenticatedPrincipal(user_id="sales_mgr_alice", role="sales_manager")
    bob = AuthenticatedPrincipal(user_id="sales_mgr_bob", role="sales_manager")

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Self Rejection Corp")
        session.add(lead)
        await session.commit()

        # 1. Alice creates and then self-rejects her own request
        action1 = _make_dummy_action(lead_id, opp_id, action=RecommendedSalesAction.PERSONALIZED_EMAIL)
        app1 = await service.request_approval(session, action=action1, requested_by=alice, now=now)
        await session.commit()

        rejected = await service.reject_action(
            session,
            approval_id=app1.id,
            approver=alice,
            decision_reason="Alice decided not to proceed",
            now=now,
        )
        await session.commit()
        assert rejected.status == "REJECTED"
        assert rejected.rejected_by_user_id == "sales_mgr_alice"

        # 2. Alice creates, Bob approves, and Alice revokes
        action2 = _make_dummy_action(lead_id, opp_id, action=RecommendedSalesAction.DISCOVERY_CALL)
        app2 = await service.request_approval(session, action=action2, requested_by=alice, now=now)
        await session.commit()

        await service.approve_action(
            session,
            approval_id=app2.id,
            approver=bob,
            decision_reason="Approved by Bob",
            now=now,
        )
        await session.commit()

        # Alice revokes the approved action
        revoked = await service.revoke_approval(
            session,
            approval_id=app2.id,
            actor=alice,
            revocation_reason="Alice revoking after reconsidering",
            now=now,
        )
        await session.commit()
        assert revoked.status == "REVOKED"
        assert revoked.revoked_by_user_id == "sales_mgr_alice"


@pytest.mark.asyncio
async def test_shared_development_identity_handling(session_factory):
    """
    Gate A: Under the shared development identity ('local_api_user'), the system recognizes
    the identity cannot prove individual human identity. In normal dev mode, it logs a warning
    and does not fail closed unless strict_separation=True is explicitly enforced.
    """
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()

    dev_user = AuthenticatedPrincipal(user_id="local_api_user", role="sales_manager")

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Shared Dev Identity Corp")
        session.add(lead)
        await session.commit()

        action = _make_dummy_action(lead_id, opp_id)

        # 1. Create with shared dev user
        app = await service.request_approval(session, action=action, requested_by=dev_user, now=now)
        await session.commit()

        # 2. Normal mode: allows dev environment to function without false claims of individual SoD
        app_normal = await service.approve_action(
            session,
            approval_id=app.id,
            approver=dev_user,
            decision_reason="Dev approval",
            strict_separation=False,
            now=now,
        )
        await session.commit()
        assert app_normal.status == "APPROVED"

        # 3. Create another request and test strict_separation=True -> Must raise ApprovalUnverifiedIdentityError
        app2 = await service.request_approval(session, action=action, requested_by=dev_user, now=now)
        await session.commit()

        with pytest.raises(ApprovalUnverifiedIdentityError) as exc_info:
            await service.approve_action(
                session,
                approval_id=app2.id,
                approver=dev_user,
                decision_reason="Strict mode self approval",
                strict_separation=True,
                now=now,
            )
        assert exc_info.value.code == "UNVERIFIED_IDENTITY_PROHIBITED"


@pytest.mark.asyncio
async def test_shared_development_identity_enforced_separation_default(session_factory):
    """
    Gate A Remediation: By default, SupervisorApprovalService enforces strict separation
    and prohibits shared development identities from approving actions.
    """
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()  # Default: allow_dev_shared_approval=False

    dev_user = AuthenticatedPrincipal(user_id="local_api_user", role="sales_manager")

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Strict Shared Corp")
        session.add(lead)
        await session.commit()

        action = _make_dummy_action(lead_id, opp_id)
        app = await service.request_approval(session, action=action, requested_by=dev_user, now=now)
        await session.commit()

        # Calling approve_action with default parameters (strict_separation=True) fails closed
        with pytest.raises(ApprovalUnverifiedIdentityError) as exc_info:
            await service.approve_action(
                session,
                approval_id=app.id,
                approver=dev_user,
                decision_reason="Default strict separation rejection",
                now=now,
            )
        assert exc_info.value.code == "UNVERIFIED_IDENTITY_PROHIBITED"


@pytest.mark.asyncio
async def test_shared_development_identity_allowed_with_explicit_flag(session_factory, monkeypatch):
    """
    Gate A Remediation: In development mode, setting allow_dev_shared_approval=True
    permits shared identity approvals while logging a security warning.
    """
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "allow_dev_shared_approval", True)

    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService(allow_dev_shared_approval=True)

    dev_user = AuthenticatedPrincipal(user_id="local_api_user", role="sales_manager")

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Dev Explicit Shared Corp")
        session.add(lead)
        await session.commit()

        action = _make_dummy_action(lead_id, opp_id)
        app = await service.request_approval(session, action=action, requested_by=dev_user, now=now)
        await session.commit()

        approved = await service.approve_action(
            session,
            approval_id=app.id,
            approver=dev_user,
            decision_reason="Dev shared approved",
            now=now,
        )
        await session.commit()
        assert approved.status == "APPROVED"
        assert approved.approved_by_user_id == "local_api_user"


@pytest.mark.asyncio
async def test_shared_development_identity_in_production_fails_closed(session_factory, monkeypatch):
    """
    Gate A Remediation: In production, even if allow_dev_shared_approval is requested,
    it is strictly ignored and fails closed with UNVERIFIED_IDENTITY_PROHIBITED.
    """
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "allow_dev_shared_approval", True)

    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService(allow_dev_shared_approval=True)
    assert service.allow_dev_shared_approval is False  # Clamped in production

    dev_user = AuthenticatedPrincipal(user_id="local_api_user", role="sales_manager")

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Prod Shared Rejection Corp")
        session.add(lead)
        await session.commit()

        action = _make_dummy_action(lead_id, opp_id)
        app = await service.request_approval(session, action=action, requested_by=dev_user, now=now)
        await session.commit()

        with pytest.raises(ApprovalUnverifiedIdentityError) as exc_info:
            await service.approve_action(
                session,
                approval_id=app.id,
                approver=dev_user,
                decision_reason="Attempting shared approval in prod",
                now=now,
            )
        assert exc_info.value.code == "UNVERIFIED_IDENTITY_PROHIBITED"


# ============================================================================
# Category 9: Gate B — Atomic Approval Context Verification and Claim Binding Tests
# ============================================================================

async def _create_gate_b_fixture(
    session_factory,
    *,
    email="valid@domain.com",
    phone="+15551234567",
    score=85,
    evidence=None,
):
    """Helper to create a fully persistent Lead, Opportunity, Action, and Approved record for Gate B tests."""
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
    service = SupervisorApprovalService()
    rep = AuthenticatedPrincipal(user_id="rep-alice", role="sales_rep")
    mgr = AuthenticatedPrincipal(user_id="mgr-bob", role="sales_manager")

    evidence_dict = evidence if evidence is not None else {"seo_score": "poor"}

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Gate B Verified Corp", email=email, phone=phone)
        session.add(lead)
        opp = LeadIntelligenceOpportunity(
            id=opp_id,
            lead_id=lead_id,
            title="Gate B Opportunity",
            score=score,
            recommended_action="PERSONALIZED_EMAIL",
            evidence=evidence_dict,
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add(opp)
        await session.commit()

        action = PrioritizedSalesAction(
            action_id=f"contact_recommendation:{lead_id}:PERSONALIZED_EMAIL",
            lead_id=lead_id,
            opportunity_id=opp_id,
            contact_id=None,
            recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
            opportunity_score=float(score),
            recommendation_fit=0.0,
            urgency_score=50.0,
            priority_score=float(score),
            readiness_status=SupervisorActionReadinessStatus.READY,
            readiness_details=ActionReadinessDetails(
                status=SupervisorActionReadinessStatus.READY,
                reasons=["All readiness gates passed"],
                has_valid_contact=True,
                has_evidence=True,
            ),
            evidence_keys=sorted(evidence_dict.keys()),
        )
        app = await service.request_approval(
            session,
            action=action,
            requested_by=rep,
            channel="EMAIL",
            contact_email=email,
            contact_phone=phone,
            now=now,
        )
        approved = await service.approve_action(
            session,
            approval_id=app.id,
            approver=mgr,
            decision_reason="Approved for Gate B verification test",
            now=now,
        )
        await session.commit()
        app_id = approved.id
        version = approved.version
        fingerprint = approved.context_fingerprint

    return lead_id, opp_id, app_id, version, fingerprint, now


@pytest.mark.asyncio
async def test_gate_b_claim_succeeds_when_context_matches(session_factory):
    """Gate B: Claim succeeds when authoritative database context matches the approved fingerprint."""
    lead_id, opp_id, app_id, version, fingerprint, now = await _create_gate_b_fixture(session_factory)
    service = SupervisorApprovalService()

    async with session_factory() as session:
        claimed = await service.claim_approval_for_execution(
            session,
            approval_id=app_id,
            worker_id="worker-gate-b-1",
            expected_version=version,
            now=now,
        )
        await session.commit()

        assert claimed.execution_claimed_by == "worker-gate-b-1"
        assert claimed.execution_claimed_at is not None
        assert claimed.version == version + 1
        assert claimed.context_fingerprint == fingerprint

        # Verify CLAIMED audit event exists
        audit_events = await SupervisorApprovalRepository.list_audit_events_for_approval(session, app_id)
        claim_events = [e for e in audit_events if e.event_type == "CLAIMED"]
        assert len(claim_events) == 1
        assert claim_events[0].actor_id == "worker-gate-b-1"
        assert claim_events[0].context_fingerprint == fingerprint


@pytest.mark.asyncio
async def test_gate_b_claim_rejected_when_lead_email_drifts(session_factory):
    """Gate B: Claim fails closed with ApprovalContextDriftError when lead email changes after approval."""
    lead_id, opp_id, app_id, version, fingerprint, now = await _create_gate_b_fixture(
        session_factory, email="original@domain.com"
    )
    service = SupervisorApprovalService()

    # Mutate lead email out-of-band in database
    async with session_factory() as session:
        lead = await session.get(Lead, lead_id)
        lead.email = "tampered_email@attacker.com"
        await session.commit()

    # Worker attempts claim -> Fails closed with CONTEXT_DRIFT
    async with session_factory() as session:
        with pytest.raises(ApprovalContextDriftError) as exc_info:
            await service.claim_approval_for_execution(
                session,
                approval_id=app_id,
                worker_id="worker-gate-b-2",
                expected_version=version,
                now=now,
            )
        assert exc_info.value.code == "CONTEXT_DRIFT"

    # Verify state invariants: approval untouched, version untouched, zero CLAIMED audit events
    async with session_factory() as session:
        reloaded = await SupervisorApprovalRepository.get_by_id(session, app_id)
        assert reloaded.status == "APPROVED"
        assert reloaded.execution_claimed_at is None
        assert reloaded.execution_claimed_by is None
        assert reloaded.version == version

        audit_events = await SupervisorApprovalRepository.list_audit_events_for_approval(session, app_id)
        assert not any(e.event_type == "CLAIMED" for e in audit_events)


@pytest.mark.asyncio
async def test_gate_b_claim_rejected_when_lead_phone_drifts(session_factory):
    """Gate B: Claim fails closed when lead phone changes after approval."""
    lead_id, opp_id, app_id, version, fingerprint, now = await _create_gate_b_fixture(
        session_factory, phone="+15551234567"
    )
    service = SupervisorApprovalService()

    # Mutate lead phone in database
    async with session_factory() as session:
        lead = await session.get(Lead, lead_id)
        lead.phone = "+19998887777"
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(ApprovalContextDriftError) as exc_info:
            await service.claim_approval_for_execution(
                session,
                approval_id=app_id,
                worker_id="worker-gate-b-3",
                expected_version=version,
                now=now,
            )
        assert exc_info.value.code == "CONTEXT_DRIFT"


@pytest.mark.asyncio
async def test_gate_b_claim_rejected_when_opportunity_score_drifts(session_factory):
    """Gate B: Claim fails closed when opportunity score changes after approval."""
    lead_id, opp_id, app_id, version, fingerprint, now = await _create_gate_b_fixture(
        session_factory, score=85
    )
    service = SupervisorApprovalService()

    # Mutate opportunity score in database
    async with session_factory() as session:
        opp = await session.get(LeadIntelligenceOpportunity, opp_id)
        opp.score = 20
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(ApprovalContextDriftError) as exc_info:
            await service.claim_approval_for_execution(
                session,
                approval_id=app_id,
                worker_id="worker-gate-b-4",
                expected_version=version,
                now=now,
            )
        assert exc_info.value.code == "CONTEXT_DRIFT"


@pytest.mark.asyncio
async def test_gate_b_claim_rejected_when_opportunity_evidence_drifts(session_factory):
    """Gate B: Claim fails closed when opportunity evidence changes after approval."""
    lead_id, opp_id, app_id, version, fingerprint, now = await _create_gate_b_fixture(
        session_factory, evidence={"seo_score": "poor"}
    )
    service = SupervisorApprovalService()

    # Mutate opportunity evidence in database
    async with session_factory() as session:
        opp = await session.get(LeadIntelligenceOpportunity, opp_id)
        opp.evidence = {"seo_score": "poor", "mobile_score": "critical"}
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(ApprovalContextDriftError) as exc_info:
            await service.claim_approval_for_execution(
                session,
                approval_id=app_id,
                worker_id="worker-gate-b-5",
                expected_version=version,
                now=now,
            )
        assert exc_info.value.code == "CONTEXT_DRIFT"


@pytest.mark.asyncio
async def test_gate_b_claim_rejected_when_lead_deleted(session_factory):
    """Gate B: Claim fails closed when the target Lead record is deleted."""
    lead_id, opp_id, app_id, version, fingerprint, now = await _create_gate_b_fixture(session_factory)
    service = SupervisorApprovalService()

    # Delete lead from database
    async with session_factory() as session:
        lead = await session.get(Lead, lead_id)
        await session.delete(lead)
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(ApprovalContextDriftError) as exc_info:
            await service.claim_approval_for_execution(
                session,
                approval_id=app_id,
                worker_id="worker-gate-b-6",
                expected_version=version,
                now=now,
            )
        assert exc_info.value.code == "CONTEXT_DRIFT"
        assert "not found or deleted" in str(exc_info.value)


@pytest.mark.asyncio
async def test_gate_b_claim_rejected_when_opportunity_deleted(session_factory):
    """Gate B: Claim fails closed when the target Opportunity record is deleted."""
    lead_id, opp_id, app_id, version, fingerprint, now = await _create_gate_b_fixture(session_factory)
    service = SupervisorApprovalService()

    # Delete opportunity from database
    async with session_factory() as session:
        opp = await session.get(LeadIntelligenceOpportunity, opp_id)
        await session.delete(opp)
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(ApprovalContextDriftError) as exc_info:
            await service.claim_approval_for_execution(
                session,
                approval_id=app_id,
                worker_id="worker-gate-b-7",
                expected_version=version,
                now=now,
            )
        assert exc_info.value.code == "CONTEXT_DRIFT"
        assert "not found or deleted" in str(exc_info.value)


@pytest.mark.asyncio
async def test_gate_b_claim_rejected_when_opportunity_cross_lead_mismatch(session_factory):
    """Gate B: Claim fails closed if opportunity lead_id is tampered/substituted."""
    lead_id, opp_id, app_id, version, fingerprint, now = await _create_gate_b_fixture(session_factory)
    service = SupervisorApprovalService()

    # Tamper opportunity lead_id
    async with session_factory() as session:
        opp = await session.get(LeadIntelligenceOpportunity, opp_id)
        opp.lead_id = uuid.uuid4()
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(ApprovalContextDriftError) as exc_info:
            await service.claim_approval_for_execution(
                session,
                approval_id=app_id,
                worker_id="worker-gate-b-8",
                expected_version=version,
                now=now,
            )
        assert exc_info.value.code == "CONTEXT_DRIFT"
        assert "does not belong to lead" in str(exc_info.value)


@pytest.mark.asyncio
async def test_gate_b_claim_client_action_payload_cannot_override_db_context(session_factory):
    """Gate B: Client-supplied action candidate cannot override authoritative database records."""
    lead_id, opp_id, app_id, version, fingerprint, now = await _create_gate_b_fixture(session_factory)
    service = SupervisorApprovalService()

    # Create an action candidate with conflicting email
    tampered_action = PrioritizedSalesAction(
        action_id=f"contact_recommendation:{lead_id}:PERSONALIZED_EMAIL",
        lead_id=lead_id,
        opportunity_id=opp_id,
        contact_id=None,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        opportunity_score=10.0,  # Conflicts with DB score of 85.0
        recommendation_fit=0.0,
        urgency_score=50.0,
        priority_score=10.0,
        readiness_status=SupervisorActionReadinessStatus.READY,
        readiness_details=ActionReadinessDetails(
            status=SupervisorActionReadinessStatus.READY,
            reasons=["All readiness gates passed"],
            has_valid_contact=True,
            has_evidence=True,
        ),
        evidence_keys=["seo_score"],
    )

    async with session_factory() as session:
        with pytest.raises(ApprovalContextDriftError):
            await service.claim_approval_for_execution(
                session,
                approval_id=app_id,
                worker_id="worker-gate-b-9",
                expected_version=version,
                action=tampered_action,
                now=now,
            )



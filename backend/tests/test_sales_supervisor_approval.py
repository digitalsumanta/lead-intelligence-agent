"""
Unit and Integration Tests for Phase 12J-D Sales Supervisor Approval and Execution Control.

Covers:
- Internal vs External Action Classification (classify_supervisor_action, is_policy_approval_required)
- Fail-Closed Behavior on Unknown or Malformed Action Types
- Readiness Gate Prerequisite: Failed gates block execution even when priority_score == 100.0 or approval exists
- Approval Independence: Approval alone NEVER bypasses opt-out, cooldown, collision, or missing data
- Missing Approval Rejection: External actions require explicit human approval (REQUIRES_APPROVAL)
- Identity Mismatch Rejection: action_id, lead_id, opportunity_id, contact_id, and channel mismatches
- Malformed & Lifecycle Approval Rejection: empty approver, REJECTED, REVOKED, PENDING, EXPIRED
- Internal Operation Authorization: RESEARCH_CONTACT without approval when READY vs human rejection
- Pipeline Batch Execution Authorization Evaluation
- Zero External Dispatch & Read-Only Guarantees
"""

import uuid
import pytest
from datetime import datetime, timedelta, timezone

from app.domain.contact_recommendation import RecommendedSalesAction
from app.domain.sales_supervisor import (
    SupervisorActionReadinessStatus,
    SupervisorActionTier,
    SupervisorApprovalStatus,
    SupervisorExecutionDecision,
    classify_supervisor_action,
    is_policy_approval_required,
)
from app.models.entities import (
    BuyingInfluenceLevel,
    ConfidenceLevel,
    ContactIntelligenceProfile,
    FunctionalArea,
    Lead,
    LeadIntelligenceOpportunity,
    SalesPriority,
    SalesStatus,
    SeniorityLevel,
    SourceType,
    now_utc,
)
from app.schemas.sales_supervisor import (
    ActionReadinessDetails,
    PrioritizedSalesAction,
    SupervisorExecutionAuthorizationResult,
    SupervisorHumanApproval,
)
from app.services.sales_supervisor import (
    authorize_supervisor_action_execution,
    build_action_identity,
    evaluate_pipeline_execution_authorizations,
    evaluate_sales_supervisor_pipeline,
)


# ============================================================================
# Helpers for Constructing Prioritized Candidate Actions
# ============================================================================

def _dummy_prioritized_action(
    recommended_action: RecommendedSalesAction = RecommendedSalesAction.PERSONALIZED_EMAIL,
    readiness_status: SupervisorActionReadinessStatus = SupervisorActionReadinessStatus.READY,
    priority_score: float = 85.0,
    lead_id: uuid.UUID | None = None,
    opportunity_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
    action_id: str | None = None,
) -> PrioritizedSalesAction:
    lid = lead_id or uuid.uuid4()
    oid = opportunity_id or uuid.uuid4()
    cid = contact_id or uuid.uuid4()
    aid = action_id or build_action_identity(cid, recommended_action)

    readiness = ActionReadinessDetails(
        status=readiness_status,
        reasons=[f"Readiness evaluated: {readiness_status.value}"],
        has_valid_contact=readiness_status != SupervisorActionReadinessStatus.MISSING_TARGET_DATA,
        has_evidence=readiness_status != SupervisorActionReadinessStatus.INSUFFICIENT_EVIDENCE,
        is_opted_out=readiness_status == SupervisorActionReadinessStatus.BLOCKED_OPT_OUT,
        in_cooldown=readiness_status == SupervisorActionReadinessStatus.BLOCKED_COOLDOWN,
        has_active_task=readiness_status == SupervisorActionReadinessStatus.ACTIVE_TASK_EXISTS,
    )

    return PrioritizedSalesAction(
        action_id=aid,
        lead_id=lid,
        opportunity_id=oid,
        contact_id=cid,
        recommended_action=recommended_action,
        opportunity_score=80.0,
        recommendation_fit=85.0,
        historical_feedback_rate=75.0,
        urgency_score=60.0,
        priority_score=priority_score,
        score_breakdown=None,
        readiness_status=readiness_status,
        readiness_details=readiness,
        evidence_keys=[f"opportunity:{oid}", f"contact:{cid}"],
        policy_approval_required=is_policy_approval_required(recommended_action),
        created_at=now_utc(),
    )


def _valid_matching_approval(
    action: PrioritizedSalesAction,
    approver_id: str = "sarah.connor@enterprise.com",
    channel: str | None = None,
    expires_at: datetime | None = None,
) -> SupervisorHumanApproval:
    return SupervisorHumanApproval(
        action_id=action.action_id,
        lead_id=action.lead_id,
        opportunity_id=action.opportunity_id,
        contact_id=action.contact_id,
        channel=channel,
        approver_id=approver_id,
        status=SupervisorApprovalStatus.APPROVED,
        approved_at=now_utc(),
        notes="Approved for priority prospect outreach",
        expires_at=expires_at,
    )


# ============================================================================
# 1. Action Classification Unit Tests
# ============================================================================

def test_internal_action_classification():
    """RESEARCH_CONTACT and TASK_SYNCHRONIZATION must classify as INTERNAL_OPERATION."""
    assert classify_supervisor_action(RecommendedSalesAction.RESEARCH_CONTACT) == SupervisorActionTier.INTERNAL_OPERATION
    assert classify_supervisor_action("RESEARCH_CONTACT") == SupervisorActionTier.INTERNAL_OPERATION
    assert classify_supervisor_action("TASK_SYNCHRONIZATION") == SupervisorActionTier.INTERNAL_OPERATION

    assert is_policy_approval_required(RecommendedSalesAction.RESEARCH_CONTACT) is False
    assert is_policy_approval_required("RESEARCH_CONTACT") is False
    assert is_policy_approval_required("TASK_SYNCHRONIZATION") is False


def test_external_action_classification():
    """Outreach actions must classify as EXTERNAL_OUTREACH and require approval."""
    external_actions = [
        RecommendedSalesAction.PERSONALIZED_EMAIL,
        RecommendedSalesAction.DISCOVERY_CALL,
        RecommendedSalesAction.DIRECT_OUTREACH,
        RecommendedSalesAction.FOLLOW_UP,
    ]
    for act in external_actions:
        assert classify_supervisor_action(act) == SupervisorActionTier.EXTERNAL_OUTREACH
        assert classify_supervisor_action(act.value) == SupervisorActionTier.EXTERNAL_OUTREACH
        assert is_policy_approval_required(act) is True


def test_unknown_action_fails_closed():
    """Unclassified or unknown actions must fail closed by raising ValueError."""
    with pytest.raises(ValueError):
        classify_supervisor_action("UNSUPPORTED_OUTREACH")

    with pytest.raises(ValueError):
        classify_supervisor_action("")

    with pytest.raises(ValueError):
        classify_supervisor_action(None)

    with pytest.raises(ValueError):
        is_policy_approval_required("RANDOM_ACTION")


# ============================================================================
# 2. Readiness Pre-Flight Prerequisite Tests (Score 100 Cannot Override)
# ============================================================================

@pytest.mark.parametrize(
    "blocked_status, expected_denial_code",
    [
        (SupervisorActionReadinessStatus.BLOCKED_OPT_OUT, "READINESS_BLOCKED_OPT_OUT"),
        (SupervisorActionReadinessStatus.BLOCKED_COOLDOWN, "READINESS_BLOCKED_COOLDOWN"),
        (SupervisorActionReadinessStatus.ACTIVE_TASK_EXISTS, "READINESS_ACTIVE_TASK_EXISTS"),
        (SupervisorActionReadinessStatus.MISSING_TARGET_DATA, "READINESS_MISSING_TARGET_DATA"),
        (SupervisorActionReadinessStatus.INSUFFICIENT_EVIDENCE, "READINESS_INSUFFICIENT_EVIDENCE"),
    ],
)
def test_blocked_readiness_prevents_authorization_even_with_perfect_score_and_approval(
    blocked_status, expected_denial_code
):
    """
    Critical Invariant: Priority score of 100.0 or a valid human approval CANNOT
    override any failed readiness gate. Authorization MUST be DENIED.
    """
    action = _dummy_prioritized_action(
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        readiness_status=blocked_status,
        priority_score=100.0,
    )
    approval = _valid_matching_approval(action)

    result = authorize_supervisor_action_execution(action, approval)

    assert result.authorized is False
    assert result.decision == SupervisorExecutionDecision.DENIED
    assert result.denial_code == expected_denial_code
    assert any("readiness" in r.lower() for r in result.reasons)


# ============================================================================
# 3. External Outreach Approval Requirement Tests
# ============================================================================

def test_external_action_without_approval_requires_approval():
    """External action with status READY but no approval returns REQUIRES_APPROVAL."""
    action = _dummy_prioritized_action(
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        readiness_status=SupervisorActionReadinessStatus.READY,
    )

    result = authorize_supervisor_action_execution(action, approval=None)

    assert result.authorized is False
    assert result.decision == SupervisorExecutionDecision.REQUIRES_APPROVAL
    assert result.denial_code == "APPROVAL_REQUIRED"
    assert result.approval_required is True


def test_valid_approval_authorizes_external_action():
    """External action with valid matching approval is authorized."""
    action = _dummy_prioritized_action(
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        readiness_status=SupervisorActionReadinessStatus.READY,
    )
    approval = _valid_matching_approval(action)

    result = authorize_supervisor_action_execution(action, approval=approval)

    assert result.authorized is True
    assert result.decision == SupervisorExecutionDecision.AUTHORIZED
    assert result.denial_code is None
    assert result.approval_details == approval


# ============================================================================
# 4. Approval Identity Mismatch Tests
# ============================================================================

def test_mismatched_action_id_rejected():
    action = _dummy_prioritized_action()
    approval = _valid_matching_approval(action)
    # Alter action_id
    mismatched_approval = approval.model_copy(update={"action_id": "contact_recommendation:other_id:action"})

    result = authorize_supervisor_action_execution(action, approval=mismatched_approval)

    assert result.authorized is False
    assert result.decision == SupervisorExecutionDecision.DENIED
    assert result.denial_code == "ACTION_MISMATCH"


def test_mismatched_lead_id_rejected():
    action = _dummy_prioritized_action()
    approval = _valid_matching_approval(action)
    mismatched_approval = approval.model_copy(update={"lead_id": uuid.uuid4()})

    result = authorize_supervisor_action_execution(action, approval=mismatched_approval)

    assert result.authorized is False
    assert result.decision == SupervisorExecutionDecision.DENIED
    assert result.denial_code == "LEAD_MISMATCH"


def test_mismatched_opportunity_id_rejected():
    action = _dummy_prioritized_action()
    approval = _valid_matching_approval(action)
    mismatched_approval = approval.model_copy(update={"opportunity_id": uuid.uuid4()})

    result = authorize_supervisor_action_execution(action, approval=mismatched_approval)

    assert result.authorized is False
    assert result.decision == SupervisorExecutionDecision.DENIED
    assert result.denial_code == "OPPORTUNITY_MISMATCH"


def test_mismatched_contact_id_rejected():
    action = _dummy_prioritized_action()
    approval = _valid_matching_approval(action)
    mismatched_approval = approval.model_copy(update={"contact_id": uuid.uuid4()})

    result = authorize_supervisor_action_execution(action, approval=mismatched_approval)

    assert result.authorized is False
    assert result.decision == SupervisorExecutionDecision.DENIED
    assert result.denial_code == "CONTACT_MISMATCH"


def test_mismatched_channel_rejected():
    """Approving for PHONE when action is EMAIL must be rejected."""
    action = _dummy_prioritized_action(recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL)
    approval = _valid_matching_approval(action, channel="PHONE")

    result = authorize_supervisor_action_execution(action, approval=approval)

    assert result.authorized is False
    assert result.decision == SupervisorExecutionDecision.DENIED
    assert result.denial_code == "CHANNEL_MISMATCH"


# ============================================================================
# 5. Malformed & Approval Lifecycle State Tests
# ============================================================================

def test_empty_approver_identity_rejected():
    action = _dummy_prioritized_action()
    approval = _valid_matching_approval(action, approver_id="   ")

    result = authorize_supervisor_action_execution(action, approval=approval)

    assert result.authorized is False
    assert result.decision == SupervisorExecutionDecision.DENIED
    assert result.denial_code == "INVALID_APPROVER_IDENTITY"


def test_rejected_and_revoked_approvals_are_denied():
    action = _dummy_prioritized_action()
    approval_rejected = _valid_matching_approval(action).model_copy(update={"status": SupervisorApprovalStatus.REJECTED})
    approval_revoked = _valid_matching_approval(action).model_copy(update={"status": SupervisorApprovalStatus.REVOKED})
    approval_pending = _valid_matching_approval(action).model_copy(update={"status": SupervisorApprovalStatus.PENDING})

    res_rej = authorize_supervisor_action_execution(action, approval_rejected)
    assert res_rej.authorized is False
    assert res_rej.decision == SupervisorExecutionDecision.DENIED
    assert res_rej.denial_code == "APPROVAL_REJECTED"

    res_rev = authorize_supervisor_action_execution(action, approval_revoked)
    assert res_rev.authorized is False
    assert res_rev.decision == SupervisorExecutionDecision.DENIED
    assert res_rev.denial_code == "APPROVAL_REVOKED"

    res_pen = authorize_supervisor_action_execution(action, approval_pending)
    assert res_pen.authorized is False
    assert res_pen.decision == SupervisorExecutionDecision.DENIED
    assert res_pen.denial_code == "APPROVAL_PENDING"


def test_expired_approval_rejected():
    eval_time = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    expired_at = eval_time - timedelta(minutes=15)  # 15 minutes in past

    action = _dummy_prioritized_action()
    approval = _valid_matching_approval(action, expires_at=expired_at)

    result = authorize_supervisor_action_execution(action, approval=approval, eval_time=eval_time)

    assert result.authorized is False
    assert result.decision == SupervisorExecutionDecision.DENIED
    assert result.denial_code == "APPROVAL_EXPIRED"


# ============================================================================
# 6. Internal Operation Authorization Tests
# ============================================================================

def test_internal_research_contact_authorized_without_human_approval():
    """RESEARCH_CONTACT when READY is authorized for internal execution without human signature."""
    action = _dummy_prioritized_action(
        recommended_action=RecommendedSalesAction.RESEARCH_CONTACT,
        readiness_status=SupervisorActionReadinessStatus.READY,
    )

    result = authorize_supervisor_action_execution(action, approval=None)

    assert result.authorized is True
    assert result.decision == SupervisorExecutionDecision.AUTHORIZED
    assert result.approval_required is False
    assert result.action_tier == SupervisorActionTier.INTERNAL_OPERATION


def test_internal_operation_blocked_if_readiness_fails():
    """Internal operation must still be DENIED if readiness status is not READY."""
    action = _dummy_prioritized_action(
        recommended_action=RecommendedSalesAction.RESEARCH_CONTACT,
        readiness_status=SupervisorActionReadinessStatus.ACTIVE_TASK_EXISTS,
    )

    result = authorize_supervisor_action_execution(action, approval=None)

    assert result.authorized is False
    assert result.decision == SupervisorExecutionDecision.DENIED
    assert result.denial_code == "READINESS_ACTIVE_TASK_EXISTS"


def test_internal_operation_denied_if_human_supervisor_explicitly_rejected():
    """If a human explicitly marks an internal operation as REJECTED, it is denied."""
    action = _dummy_prioritized_action(
        recommended_action=RecommendedSalesAction.RESEARCH_CONTACT,
        readiness_status=SupervisorActionReadinessStatus.READY,
    )
    approval_rejected = _valid_matching_approval(action).model_copy(update={"status": SupervisorApprovalStatus.REJECTED})

    result = authorize_supervisor_action_execution(action, approval=approval_rejected)

    assert result.authorized is False
    assert result.decision == SupervisorExecutionDecision.DENIED
    assert result.denial_code == "APPROVAL_REJECTED"


# ============================================================================
# 7. Pipeline Batch Authorization Tests
# ============================================================================

def test_pipeline_batch_execution_authorizations():
    """Evaluates multiple pipeline actions with matching and missing approvals."""
    # Action 1: Ready Email with valid approval -> AUTHORIZED
    act1 = _dummy_prioritized_action(RecommendedSalesAction.PERSONALIZED_EMAIL)
    appr1 = _valid_matching_approval(act1)

    # Action 2: Ready Email without approval -> REQUIRES_APPROVAL
    act2 = _dummy_prioritized_action(RecommendedSalesAction.PERSONALIZED_EMAIL)

    # Action 3: Blocked by Opt-Out (with approval) -> DENIED
    act3 = _dummy_prioritized_action(
        RecommendedSalesAction.PERSONALIZED_EMAIL,
        readiness_status=SupervisorActionReadinessStatus.BLOCKED_OPT_OUT,
    )
    appr3 = _valid_matching_approval(act3)

    # Action 4: Internal Research Contact without approval -> AUTHORIZED
    act4 = _dummy_prioritized_action(RecommendedSalesAction.RESEARCH_CONTACT)

    actions = [act1, act2, act3, act4]
    approvals_map = {
        act1.action_id: appr1,
        act3.action_id: appr3,
    }

    results = evaluate_pipeline_execution_authorizations(actions, approvals_map)

    assert len(results) == 4
    assert results[0].decision == SupervisorExecutionDecision.AUTHORIZED
    assert results[0].authorized is True

    assert results[1].decision == SupervisorExecutionDecision.REQUIRES_APPROVAL
    assert results[1].authorized is False

    assert results[2].decision == SupervisorExecutionDecision.DENIED
    assert results[2].authorized is False

    assert results[3].decision == SupervisorExecutionDecision.AUTHORIZED
    assert results[3].authorized is True


# ============================================================================
# 8. Integration Test: Pipeline PrioritizedSalesAction Schema Wire-Up
# ============================================================================

@pytest.mark.asyncio
async def test_pipeline_action_policy_approval_required_flag(session_factory):
    """Verifies policy_approval_required is accurately populated from action classification."""
    async with session_factory() as session:
        lead = Lead(
            id=uuid.uuid4(),
            company_name="Policy Test Corp",
            email="policy@test.com",
            phone="1234567890",
            sales_status=SalesStatus.NEW,
            currency="USD",
            decision_maker="VP Tech",
        )
        session.add(lead)

        prof = ContactIntelligenceProfile(
            contact_id=lead.id,
            decision_maker_score=90,
            functional_area=FunctionalArea.SALES,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.SYSTEM_DERIVED,
        )
        session.add(prof)

        opp = LeadIntelligenceOpportunity(
            id=uuid.uuid4(),
            lead_id=lead.id,
            title="Policy Wireup Opp",
            opportunity_type="SALES",
            score=85,
            priority="HIGH",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
            evidence={"signals": [{"type": "expansion"}]},
        )
        session.add(opp)
        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1
        action = response.actions[0]
        # For SALES opportunity with decision maker, recommended action is DISCOVERY_CALL (external outreach)
        assert action.policy_approval_required is True
        assert classify_supervisor_action(action.recommended_action) == SupervisorActionTier.EXTERNAL_OUTREACH

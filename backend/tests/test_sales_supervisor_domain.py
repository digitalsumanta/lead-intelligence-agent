"""
Unit tests for Phase 12J-A Sales Supervisor Domain Foundation and Scoring.

Covers:
- Prioritization scoring formula and exact weights (0.35, 0.30, 0.20, 0.15)
- Boundary values (0.0, 100.0) and precision rounding
- Documented missing/insufficient feedback fallback policy (50.0 neutral prior)
- Input validation, non-numeric, out-of-range, and NaN rejection
- Urgency band mappings and numeric urgency inputs
- Deterministic stable sorting and tie-breaking
- Separation of scoring and action readiness / execution permission
- Pydantic schema validation, immutability, and serialization
"""

import math
import uuid
import pytest
from datetime import datetime, timezone
from pydantic import ValidationError

from app.domain.contact_recommendation import RecommendedSalesAction
from app.domain.sales_supervisor import (
    DEFAULT_MIN_FEEDBACK_EXECUTIONS,
    DEFAULT_NEUTRAL_FEEDBACK_SCORE,
    URGENCY_BAND_SCORES,
    WEIGHT_HISTORICAL_FEEDBACK,
    WEIGHT_OPPORTUNITY_SCORE,
    WEIGHT_RECOMMENDATION_FIT,
    WEIGHT_URGENCY,
    SupervisorActionReadinessStatus,
    SupervisorActionScoreBreakdown,
    SupervisorUrgencyBand,
    calculate_supervisor_action_priority,
    is_action_ready_for_execution,
    sort_prioritized_actions,
)
from app.schemas.sales_supervisor import (
    ActionReadinessDetails,
    PrioritizedSalesAction,
    SalesSupervisorPipelineResponse,
    SupervisorActionScoreBreakdownOut,
)


# ============================================================================
# 1. Weights and Configuration Integrity
# ============================================================================

def test_scoring_weights_sum_to_one():
    """Verify that prioritization weights sum strictly to 1.0."""
    total = (
        WEIGHT_OPPORTUNITY_SCORE
        + WEIGHT_RECOMMENDATION_FIT
        + WEIGHT_HISTORICAL_FEEDBACK
        + WEIGHT_URGENCY
    )
    assert math.isclose(total, 1.0, rel_tol=1e-9)
    assert WEIGHT_OPPORTUNITY_SCORE == 0.35
    assert WEIGHT_RECOMMENDATION_FIT == 0.30
    assert WEIGHT_HISTORICAL_FEEDBACK == 0.20
    assert WEIGHT_URGENCY == 0.15


# ============================================================================
# 2. Valid Scoring and Calculation Mechanics
# ============================================================================

def test_calculate_priority_all_max():
    """All maximum scores (100.0) should produce exactly 100.0."""
    result = calculate_supervisor_action_priority(
        opportunity_score=100.0,
        recommendation_fit=100.0,
        historical_feedback=100.0,
        urgency=100.0,
        total_feedback_executions=10,
    )
    assert result.final_score == 100.0
    assert result.opportunity_component == 35.0
    assert result.recommendation_component == 30.0
    assert result.feedback_component == 20.0
    assert result.urgency_component == 15.0
    assert result.feedback_available is True
    assert result.fallback_applied is False


def test_calculate_priority_all_zeros():
    """All zero scores should produce exactly 0.0 with feedback provided."""
    result = calculate_supervisor_action_priority(
        opportunity_score=0.0,
        recommendation_fit=0.0,
        historical_feedback=0.0,
        urgency=0.0,
        total_feedback_executions=5,
    )
    assert result.final_score == 0.0
    assert result.opportunity_component == 0.0
    assert result.recommendation_component == 0.0
    assert result.feedback_component == 0.0
    assert result.urgency_component == 0.0
    assert result.feedback_available is True
    assert result.fallback_applied is False


def test_calculate_priority_exact_formula():
    """Verify exact formula calculation on known intermediate values."""
    # S_opp = 80 -> 0.35 * 80 = 28.0
    # S_rec = 70 -> 0.30 * 70 = 21.0
    # S_fb  = 60 -> 0.20 * 60 = 12.0
    # S_urg = 40 -> 0.15 * 40 = 6.0
    # Total = 28.0 + 21.0 + 12.0 + 6.0 = 67.0
    result = calculate_supervisor_action_priority(
        opportunity_score=80.0,
        recommendation_fit=70.0,
        historical_feedback=60.0,
        urgency=40.0,
        total_feedback_executions=3,
    )
    assert result.final_score == 67.0
    assert result.opportunity_component == 28.0
    assert result.recommendation_component == 21.0
    assert result.feedback_component == 12.0
    assert result.urgency_component == 6.0


def test_calculate_priority_rounding_behavior():
    """Verify deterministic rounding to 2 decimal places."""
    # S_opp = 73 -> 0.35 * 73 = 25.55
    # S_rec = 67 -> 0.30 * 67 = 20.10
    # S_fb  = 53 -> 0.20 * 53 = 10.60
    # S_urg = 41 -> 0.15 * 41 = 6.15
    # Sum = 25.55 + 20.10 + 10.60 + 6.15 = 62.40
    result = calculate_supervisor_action_priority(
        opportunity_score=73,
        recommendation_fit=67,
        historical_feedback=53,
        urgency=41,
        total_feedback_executions=1,
    )
    assert result.final_score == 62.40
    assert result.opportunity_component == 25.55
    assert result.recommendation_component == 20.10
    assert result.feedback_component == 10.60
    assert result.urgency_component == 6.15


# ============================================================================
# 3. Missing and Insufficient Feedback Policy
# ============================================================================

def test_missing_feedback_uses_neutral_fallback_score():
    """
    When historical feedback is None, a documented neutral prior of 50.0 is used.
    It must not default to 0.0 (penalizing cold leads) or 100.0 (unjustified boost).
    """
    result = calculate_supervisor_action_priority(
        opportunity_score=100.0,
        recommendation_fit=100.0,
        historical_feedback=None,
        urgency=100.0,
    )
    # Opp: 35.0, Rec: 30.0, Fb: 0.20 * 50.0 = 10.0, Urg: 15.0 -> Total = 90.0
    assert result.final_score == 90.0
    assert result.historical_feedback == DEFAULT_NEUTRAL_FEEDBACK_SCORE
    assert result.feedback_component == 10.0
    assert result.feedback_available is False
    assert result.fallback_applied is True


def test_insufficient_executions_triggers_fallback():
    """
    When total_feedback_executions is below min_feedback_executions,
    the fallback is applied even if a feedback rate is passed.
    """
    result = calculate_supervisor_action_priority(
        opportunity_score=50.0,
        recommendation_fit=50.0,
        historical_feedback=100.0,  # Single anomalous 100% conversion
        total_feedback_executions=2,
        min_feedback_executions=5,  # Requires at least 5 executions
        urgency=50.0,
    )
    # Feedback should fallback to 50.0 neutral prior
    assert result.historical_feedback == 50.0
    assert result.feedback_component == 10.0
    assert result.feedback_available is False
    assert result.fallback_applied is True


def test_sufficient_executions_uses_actual_rate():
    """When executions meet the threshold, the actual feedback rate is used."""
    result = calculate_supervisor_action_priority(
        opportunity_score=50.0,
        recommendation_fit=50.0,
        historical_feedback=25.0,
        total_feedback_executions=5,
        min_feedback_executions=5,
        urgency=50.0,
    )
    assert result.historical_feedback == 25.0
    assert result.feedback_component == 5.0  # 0.20 * 25.0 = 5.0
    assert result.feedback_available is True
    assert result.fallback_applied is False


# ============================================================================
# 4. Urgency Bands and Numeric Urgency
# ============================================================================

@pytest.mark.parametrize(
    "band, expected_score",
    [
        (SupervisorUrgencyBand.OVERDUE, 100.0),
        (SupervisorUrgencyBand.DUE_TODAY, 80.0),
        (SupervisorUrgencyBand.HIGH_SIGNAL, 60.0),
        (SupervisorUrgencyBand.STANDARD, 20.0),
        (SupervisorUrgencyBand.NONE, 0.0),
    ],
)
def test_urgency_bands_mapping(band, expected_score):
    """Verify that standardized urgency bands map to expected numeric values."""
    assert URGENCY_BAND_SCORES[band] == expected_score
    result = calculate_supervisor_action_priority(
        opportunity_score=0.0,
        recommendation_fit=0.0,
        historical_feedback=0.0,
        total_feedback_executions=1,
        urgency=band,
    )
    assert result.urgency == expected_score
    assert result.urgency_component == round(0.15 * expected_score, 4)


def test_explicit_numeric_urgency():
    """Verify passing an explicit custom float for urgency."""
    result = calculate_supervisor_action_priority(
        opportunity_score=50.0,
        recommendation_fit=50.0,
        historical_feedback=50.0,
        total_feedback_executions=1,
        urgency=45.5,
    )
    assert result.urgency == 45.5
    assert result.urgency_component == round(0.15 * 45.5, 4)


# ============================================================================
# 5. Input Validation and Malformed Value Rejection
# ============================================================================

@pytest.mark.parametrize(
    "field_kwargs",
    [
        {"opportunity_score": -1.0, "recommendation_fit": 50.0},
        {"opportunity_score": 100.1, "recommendation_fit": 50.0},
        {"opportunity_score": 50.0, "recommendation_fit": -0.1},
        {"opportunity_score": 50.0, "recommendation_fit": 105.0},
        {"opportunity_score": 50.0, "recommendation_fit": 50.0, "historical_feedback": -10.0, "total_feedback_executions": 5},
        {"opportunity_score": 50.0, "recommendation_fit": 50.0, "historical_feedback": 100.5, "total_feedback_executions": 5},
        {"opportunity_score": 50.0, "recommendation_fit": 50.0, "urgency": -5.0},
        {"opportunity_score": 50.0, "recommendation_fit": 50.0, "urgency": 120.0},
    ],
)
def test_out_of_range_inputs_raise_value_error(field_kwargs):
    """Scores strictly outside [0, 100] must raise ValueError, not silently clamp."""
    with pytest.raises(ValueError, match="must be between 0.0 and 100.0"):
        calculate_supervisor_action_priority(**field_kwargs)


@pytest.mark.parametrize(
    "bad_value",
    [float("nan"), float("inf"), float("-inf")],
)
def test_non_finite_inputs_raise_value_error(bad_value):
    """NaN and Infinite floats must be rejected."""
    with pytest.raises(ValueError, match="must be finite"):
        calculate_supervisor_action_priority(
            opportunity_score=bad_value,
            recommendation_fit=50.0,
        )


@pytest.mark.parametrize(
    "bad_type",
    ["50", "high", [50], {"score": 50}, True, False],
)
def test_invalid_types_raise_type_error(bad_type):
    """Non-numeric types and booleans must raise TypeError."""
    with pytest.raises(TypeError):
        calculate_supervisor_action_priority(
            opportunity_score=bad_type,
            recommendation_fit=50.0,
        )


# ============================================================================
# 6. Determinism and Tie-Breaking Sorting
# ============================================================================

def test_deterministic_scoring_reproducibility():
    """Repeated calculations on identical inputs must yield identical bitwise results."""
    run1 = calculate_supervisor_action_priority(75.5, 82.3, 44.1, 60.0, total_feedback_executions=3)
    run2 = calculate_supervisor_action_priority(75.5, 82.3, 44.1, 60.0, total_feedback_executions=3)
    assert run1 == run2
    assert run1.as_dict() == run2.as_dict()


def test_sort_prioritized_actions_order():
    """Verify sorting prioritizes highest scores and breaks ties deterministically."""
    action_high = {
        "action_id": "act-1",
        "priority_score": 85.0,
        "opportunity_score": 90.0,
        "recommendation_fit": 80.0,
        "urgency_score": 80.0,
    }
    action_mid = {
        "action_id": "act-2",
        "priority_score": 65.0,
        "opportunity_score": 70.0,
        "recommendation_fit": 60.0,
        "urgency_score": 60.0,
    }
    action_low = {
        "action_id": "act-3",
        "priority_score": 45.0,
        "opportunity_score": 50.0,
        "recommendation_fit": 40.0,
        "urgency_score": 40.0,
    }

    # Input in shuffled order
    sorted_actions = sort_prioritized_actions(
        [action_low, action_high, action_mid],
        score_key=lambda x: x["priority_score"],
        opp_key=lambda x: x["opportunity_score"],
        rec_key=lambda x: x["recommendation_fit"],
        urg_key=lambda x: x["urgency_score"],
        id_key=lambda x: x["action_id"],
    )
    assert [a["action_id"] for a in sorted_actions] == ["act-1", "act-2", "act-3"]


def test_sort_prioritized_actions_tie_breaking():
    """
    When priority_score is identical:
    Tie-break 1: opportunity_score
    Tie-break 2: recommendation_fit
    Tie-break 3: urgency_score
    Tie-break 4: action_id (alphanumeric ascending)
    """
    # Two actions with same final score, but act-tie-1 has higher opportunity score
    act_a = {"action_id": "act-b", "priority_score": 70.0, "opportunity_score": 80.0, "recommendation_fit": 60.0, "urgency_score": 50.0}
    act_b = {"action_id": "act-a", "priority_score": 70.0, "opportunity_score": 70.0, "recommendation_fit": 70.0, "urgency_score": 50.0}

    sorted_res = sort_prioritized_actions(
        [act_b, act_a],
        score_key=lambda x: x["priority_score"],
        opp_key=lambda x: x["opportunity_score"],
        rec_key=lambda x: x["recommendation_fit"],
        urg_key=lambda x: x["urgency_score"],
        id_key=lambda x: x["action_id"],
    )
    assert sorted_res[0]["action_id"] == "act-b"

    # Exact tie on all scores breaks on action_id ascending ("act-01" before "act-02")
    act_same_1 = {"action_id": "act-02", "priority_score": 70.0, "opportunity_score": 80.0, "recommendation_fit": 60.0, "urgency_score": 50.0}
    act_same_2 = {"action_id": "act-01", "priority_score": 70.0, "opportunity_score": 80.0, "recommendation_fit": 60.0, "urgency_score": 50.0}

    sorted_ties = sort_prioritized_actions(
        [act_same_1, act_same_2],
        score_key=lambda x: x["priority_score"],
        opp_key=lambda x: x["opportunity_score"],
        rec_key=lambda x: x["recommendation_fit"],
        urg_key=lambda x: x["urgency_score"],
        id_key=lambda x: x["action_id"],
    )
    assert [a["action_id"] for a in sorted_ties] == ["act-01", "act-02"]


# ============================================================================
# 7. Separation of Scoring and Readiness
# ============================================================================

def test_scoring_does_not_override_readiness_status():
    """A 100.0 priority score must NEVER override an unready or blocked status."""
    breakdown = calculate_supervisor_action_priority(100.0, 100.0, 100.0, 100.0, total_feedback_executions=10)
    assert breakdown.final_score == 100.0

    # Blocked by opt-out
    assert is_action_ready_for_execution(SupervisorActionReadinessStatus.BLOCKED_OPT_OUT) is False
    assert is_action_ready_for_execution(SupervisorActionReadinessStatus.BLOCKED_COOLDOWN) is False
    assert is_action_ready_for_execution(SupervisorActionReadinessStatus.MISSING_TARGET_DATA) is False
    assert is_action_ready_for_execution(SupervisorActionReadinessStatus.INSUFFICIENT_EVIDENCE) is False
    assert is_action_ready_for_execution(SupervisorActionReadinessStatus.ACTIVE_TASK_EXISTS) is False

    # Only READY is executable
    assert is_action_ready_for_execution(SupervisorActionReadinessStatus.READY) is True


# ============================================================================
# 8. Pydantic Schemas Serialization and Validation
# ============================================================================

def test_supervisor_breakdown_out_from_breakdown():
    """Verify Pydantic model creation from domain dataclass."""
    breakdown = calculate_supervisor_action_priority(80.0, 70.0, 60.0, 50.0, total_feedback_executions=2)
    schema = SupervisorActionScoreBreakdownOut.from_breakdown(breakdown)
    assert schema.final_score == breakdown.final_score
    assert schema.opportunity_component == breakdown.opportunity_component
    assert schema.feedback_available is True
    assert schema.fallback_applied is False


def test_prioritized_sales_action_schema():
    """Verify PrioritizedSalesAction serialization, validation, and immutability."""
    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    contact_id = uuid.uuid4()

    breakdown = calculate_supervisor_action_priority(85.0, 75.0, 65.0, SupervisorUrgencyBand.HIGH_SIGNAL, total_feedback_executions=3)
    breakdown_out = SupervisorActionScoreBreakdownOut.from_breakdown(breakdown)

    readiness = ActionReadinessDetails(
        status=SupervisorActionReadinessStatus.READY,
        reasons=["All 5 readiness gates passed"],
        has_valid_contact=True,
        has_evidence=True,
        is_opted_out=False,
        in_cooldown=False,
        has_active_task=False,
    )

    action = PrioritizedSalesAction(
        action_id="act-test-001",
        lead_id=lead_id,
        opportunity_id=opp_id,
        contact_id=contact_id,
        recommended_action=RecommendedSalesAction.DISCOVERY_CALL,
        opportunity_score=85.0,
        recommendation_fit=75.0,
        historical_feedback_rate=65.0,
        urgency_score=60.0,
        priority_score=breakdown.final_score,
        score_breakdown=breakdown_out,
        readiness_status=SupervisorActionReadinessStatus.READY,
        readiness_details=readiness,
        evidence_keys=["signal_marketing_expansion"],
        policy_approval_required=True,
    )

    assert action.priority_score == breakdown.final_score
    assert action.recommended_action == RecommendedSalesAction.DISCOVERY_CALL
    assert action.readiness_status == SupervisorActionReadinessStatus.READY
    assert action.policy_approval_required is True

    # Immutability check (frozen=True)
    with pytest.raises(ValidationError):
        action.priority_score = 99.0  # type: ignore

    # Dump and load JSON
    data = action.model_dump()
    assert data["action_id"] == "act-test-001"
    assert data["recommended_action"] == "DISCOVERY_CALL"
    assert data["readiness_status"] == "READY"


def test_pipeline_response_schema():
    """Verify aggregated pipeline response schema structure and validation."""
    response = SalesSupervisorPipelineResponse(
        total_candidates=1,
        ready_count=1,
        blocked_count=0,
        actions=[],
        summary={"avg_priority": 75.0},
    )
    assert response.total_candidates == 1
    assert response.ready_count == 1
    assert response.blocked_count == 0
    assert response.actions == []
    assert response.summary["avg_priority"] == 75.0

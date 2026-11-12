"""
Deterministic Domain Foundation for Controlled Autonomous Sales Supervisor.

Provides typed domain representation for readiness states, urgency bands,
scoring weights, input validation, and deterministic prioritization calculations.
"""

from __future__ import annotations

import enum
import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Sequence, TypeVar


# ============================================================================
# Scoring Constants and Weights
# ============================================================================

WEIGHT_OPPORTUNITY_SCORE: float = 0.35
WEIGHT_RECOMMENDATION_FIT: float = 0.30
WEIGHT_HISTORICAL_FEEDBACK: float = 0.20
WEIGHT_URGENCY: float = 0.15

# Total weight must sum to 1.0
assert math.isclose(
    WEIGHT_OPPORTUNITY_SCORE + WEIGHT_RECOMMENDATION_FIT + WEIGHT_HISTORICAL_FEEDBACK + WEIGHT_URGENCY,
    1.0,
    rel_tol=1e-9,
), "Prioritization weights must sum to 1.0"

MIN_SCORE: float = 0.0
MAX_SCORE: float = 100.0

# Documented policy for cold/untested action types with missing or insufficient feedback:
# A neutral prior score of 50.0 is used (neither penalizing cold actions at 0% nor over-boosting at 100%).
DEFAULT_NEUTRAL_FEEDBACK_SCORE: float = 50.0
DEFAULT_MIN_FEEDBACK_EXECUTIONS: int = 1


# ============================================================================
# Domain Enums
# ============================================================================

class SupervisorActionReadinessStatus(str, enum.Enum):
    """
    Readiness gate evaluation statuses for proposed sales actions.
    Separated from prioritization scoring: an action may have high priority
    yet be blocked by compliance, lack of coordinates, or active tasks.
    """
    READY = "READY"
    BLOCKED_OPT_OUT = "BLOCKED_OPT_OUT"
    BLOCKED_COOLDOWN = "BLOCKED_COOLDOWN"
    MISSING_TARGET_DATA = "MISSING_TARGET_DATA"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    ACTIVE_TASK_EXISTS = "ACTIVE_TASK_EXISTS"


class SupervisorActionTier(str, enum.Enum):
    """
    Execution tier separating internal operations from consequential external outreach.
    External outreach requires explicit human approval; internal operations can be
    system-authorized when readiness and action semantics are verified.
    """
    INTERNAL_OPERATION = "INTERNAL_OPERATION"
    EXTERNAL_OUTREACH = "EXTERNAL_OUTREACH"


class SupervisorApprovalStatus(str, enum.Enum):
    """States of a human approval decision for a candidate sales action."""
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PENDING = "PENDING"
    REVOKED = "REVOKED"


class SupervisorExecutionDecision(str, enum.Enum):
    """Deterministic authorization decision for candidate action execution."""
    AUTHORIZED = "AUTHORIZED"
    DENIED = "DENIED"
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"


VERIFIED_EXTERNAL_ACTIONS = frozenset({
    "PERSONALIZED_EMAIL",
    "DISCOVERY_CALL",
    "DIRECT_OUTREACH",
    "FOLLOW_UP",
})

VERIFIED_INTERNAL_ACTIONS = frozenset({
    "RESEARCH_CONTACT",
    "TASK_SYNCHRONIZATION",
})


def classify_supervisor_action(action: Any) -> SupervisorActionTier:
    """
    Deterministically classifies a proposed action as INTERNAL_OPERATION or EXTERNAL_OUTREACH.
    Unknown or unsupported action types fail closed by raising ValueError.
    """
    if action is None:
        raise ValueError("Action cannot be None")

    action_val = action.value if hasattr(action, "value") else str(action)
    action_val = action_val.strip().upper()

    if action_val in VERIFIED_EXTERNAL_ACTIONS:
        return SupervisorActionTier.EXTERNAL_OUTREACH
    elif action_val in VERIFIED_INTERNAL_ACTIONS:
        return SupervisorActionTier.INTERNAL_OPERATION
    else:
        raise ValueError(f"Unknown or unclassified action type '{action_val}' (fails closed)")


def is_policy_approval_required(action: Any) -> bool:
    """
    Determines whether a proposed action requires explicit human approval before execution.
    External outreach strictly requires human approval. Internal operations do not.
    Unknown action types fail closed.
    """
    tier = classify_supervisor_action(action)
    return tier == SupervisorActionTier.EXTERNAL_OUTREACH


class SupervisorUrgencyBand(str, enum.Enum):
    """Standardized urgency bands mapped to deterministic scores on a 0-100 scale."""
    OVERDUE = "OVERDUE"          # 100.0 - Overdue follow-up or SLA breach
    DUE_TODAY = "DUE_TODAY"      # 80.0  - Due today or urgent prospect response
    HIGH_SIGNAL = "HIGH_SIGNAL"  # 60.0  - Fresh high-priority buying signal
    STANDARD = "STANDARD"        # 20.0  - Standard outreach cadence
    NONE = "NONE"                # 0.0   - Informational / no temporal urgency


URGENCY_BAND_SCORES: dict[SupervisorUrgencyBand, float] = {
    SupervisorUrgencyBand.OVERDUE: 100.0,
    SupervisorUrgencyBand.DUE_TODAY: 80.0,
    SupervisorUrgencyBand.HIGH_SIGNAL: 60.0,
    SupervisorUrgencyBand.STANDARD: 20.0,
    SupervisorUrgencyBand.NONE: 0.0,
}


# ============================================================================
# Domain Data Structures
# ============================================================================

@dataclass(frozen=True)
class SupervisorActionScoreBreakdown:
    """
    Deterministic breakdown of the components contributing to the final supervisor priority score.
    All components and inputs are normalized to the 0-100 scale.
    """
    opportunity_score: float
    recommendation_fit: float
    historical_feedback: float
    urgency: float
    opportunity_component: float
    recommendation_component: float
    feedback_component: float
    urgency_component: float
    final_score: float
    feedback_available: bool
    fallback_applied: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "opportunity_score": self.opportunity_score,
            "recommendation_fit": self.recommendation_fit,
            "historical_feedback": self.historical_feedback,
            "urgency": self.urgency,
            "opportunity_component": self.opportunity_component,
            "recommendation_component": self.recommendation_component,
            "feedback_component": self.feedback_component,
            "urgency_component": self.urgency_component,
            "final_score": self.final_score,
            "feedback_available": self.feedback_available,
            "fallback_applied": self.fallback_applied,
        }


# ============================================================================
# Validation and Scoring Calculation
# ============================================================================

def _validate_score_input(name: str, value: Any) -> float:
    """Validate that value is numeric (not bool), finite, and in [0.0, 100.0]."""
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a number, not bool")
    if not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a numeric value, got {type(value).__name__}")
    fval = float(value)
    if not math.isfinite(fval):
        raise ValueError(f"{name} must be finite, got {fval}")
    if fval < MIN_SCORE or fval > MAX_SCORE:
        raise ValueError(f"{name} must be between {MIN_SCORE} and {MAX_SCORE}, got {fval}")
    return fval


def calculate_supervisor_action_priority(
    opportunity_score: float | int,
    recommendation_fit: float | int,
    historical_feedback: float | int | None = None,
    urgency: float | int | SupervisorUrgencyBand = SupervisorUrgencyBand.STANDARD,
    total_feedback_executions: int = 0,
    min_feedback_executions: int = DEFAULT_MIN_FEEDBACK_EXECUTIONS,
) -> SupervisorActionScoreBreakdown:
    """
    Calculates a deterministic prioritization score on a 0-100 scale:
        SupervisorScore = (0.35 * S_opp) + (0.30 * S_rec) + (0.20 * S_feedback) + (0.15 * S_urgency)

    Missing Feedback Policy:
        If historical_feedback is None or total_feedback_executions < min_feedback_executions:
            - Feedback is treated as unavailable (feedback_available = False, fallback_applied = True).
            - A neutral prior score of 50.0 is used (neither penalizing cold actions at 0% nor over-boosting at 100%).
        Otherwise:
            - The provided historical_feedback rate (validated between 0 and 100) is used.
            - feedback_available = True, fallback_applied = False.

    Rounding:
        The final score is rounded to 2 decimal places. Component values are rounded to 4 decimal places.

    Raises:
        TypeError: If an input is not numeric (or bool).
        ValueError: If an input is out of [0, 100] or non-finite.
    """
    opp_val = _validate_score_input("opportunity_score", opportunity_score)
    rec_val = _validate_score_input("recommendation_fit", recommendation_fit)

    # Validate urgency
    if isinstance(urgency, SupervisorUrgencyBand):
        urg_val = URGENCY_BAND_SCORES[urgency]
    else:
        urg_val = _validate_score_input("urgency", urgency)

    # Validate / normalize historical feedback
    if historical_feedback is None or total_feedback_executions < min_feedback_executions:
        feedback_val = DEFAULT_NEUTRAL_FEEDBACK_SCORE
        feedback_available = False
        fallback_applied = True
    else:
        feedback_val = _validate_score_input("historical_feedback", historical_feedback)
        feedback_available = True
        fallback_applied = False

    # Component calculations
    opp_comp = round(WEIGHT_OPPORTUNITY_SCORE * opp_val, 4)
    rec_comp = round(WEIGHT_RECOMMENDATION_FIT * rec_val, 4)
    fb_comp = round(WEIGHT_HISTORICAL_FEEDBACK * feedback_val, 4)
    urg_comp = round(WEIGHT_URGENCY * urg_val, 4)

    raw_final = opp_comp + rec_comp + fb_comp + urg_comp
    final_score = round(max(MIN_SCORE, min(MAX_SCORE, raw_final)), 2)

    return SupervisorActionScoreBreakdown(
        opportunity_score=round(opp_val, 2),
        recommendation_fit=round(rec_val, 2),
        historical_feedback=round(feedback_val, 2),
        urgency=round(urg_val, 2),
        opportunity_component=opp_comp,
        recommendation_component=rec_comp,
        feedback_component=fb_comp,
        urgency_component=urg_comp,
        final_score=final_score,
        feedback_available=feedback_available,
        fallback_applied=fallback_applied,
    )


# ============================================================================
# Action Readiness vs Execution Separation
# ============================================================================

def is_action_ready_for_execution(status: SupervisorActionReadinessStatus) -> bool:
    """
    Check if a proposed action is ready for execution.
    A high prioritization score NEVER overrides an unready status.
    """
    return status == SupervisorActionReadinessStatus.READY


# ============================================================================
# Stable Deterministic Ranking / Sorting
# ============================================================================

T = TypeVar("T")

def sort_prioritized_actions(
    actions: Sequence[T],
    *,
    score_key: Callable[[T], float] = lambda x: getattr(x, "priority_score", 0.0),
    opp_key: Callable[[T], float] = lambda x: getattr(x, "opportunity_score", 0.0),
    rec_key: Callable[[T], float] = lambda x: getattr(x, "recommendation_fit", 0.0),
    urg_key: Callable[[T], float] = lambda x: getattr(x, "urgency_score", 0.0),
    id_key: Callable[[T], str] = lambda x: str(getattr(x, "action_id", "")),
) -> list[T]:
    """
    Deterministically sort prioritized actions:
    1. priority_score (descending)
    2. opportunity_score (descending)
    3. recommendation_fit (descending)
    4. urgency_score (descending)
    5. action_id (ascending string comparison for stable tie-breaking)
    """
    return sorted(
        actions,
        key=lambda x: (
            -score_key(x),
            -opp_key(x),
            -rec_key(x),
            -urg_key(x),
            id_key(x),
        ),
    )


# ============================================================================
# Authenticated Principal and Trust Boundary Representation
# ============================================================================

@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """
    Typed domain representation of an authenticated principal.
    In the current internal development architecture, this represents a configured
    development principal (derived from the shared bearer token or environment),
    NOT an independently verified individual human identity via SSO/mTLS.
    """
    user_id: str
    role: str = "sales_rep"
    display_name: str = ""
    is_authenticated: bool = True


# Shared development service identities that do not represent an individual human identity
SHARED_UNVERIFIED_DEVELOPMENT_IDENTITIES: frozenset[str] = frozenset({
    "local_api_user",
    "system",
    "system_worker",
    "system_sweeper",
})


def is_shared_development_identity(user_id: Optional[str]) -> bool:
    """
    Checks if a user_id represents a shared unverified development/system identity,
    which cannot reliably establish individual human identity.
    """
    if not user_id:
        return False
    return user_id.strip().lower() in SHARED_UNVERIFIED_DEVELOPMENT_IDENTITIES


# ============================================================================
# Domain Exceptions for Durable Human Approvals
# ============================================================================

class SupervisorApprovalError(Exception):
    """Base exception for supervisor human approval and authorization failures."""
    def __init__(self, message: str, code: str = "APPROVAL_ERROR"):
        super().__init__(message)
        self.message = message
        self.code = code


class ApprovalNotFoundError(SupervisorApprovalError):
    def __init__(self, message: str = "Supervisor approval record not found"):
        super().__init__(message, code="APPROVAL_NOT_FOUND")


class ApprovalInvalidTransitionError(SupervisorApprovalError):
    def __init__(self, from_status: str, to_status: str, reason: str = ""):
        msg = f"Cannot transition approval from '{from_status}' to '{to_status}'"
        if reason:
            msg += f": {reason}"
        super().__init__(msg, code="INVALID_LIFECYCLE_TRANSITION")
        self.from_status = from_status
        self.to_status = to_status


class ApprovalVersionConflictError(SupervisorApprovalError):
    def __init__(self, expected_version: int, actual_version: int):
        super().__init__(
            f"Approval version conflict: expected version {expected_version}, but found {actual_version}",
            code="VERSION_CONFLICT",
        )
        self.expected_version = expected_version
        self.actual_version = actual_version


class ApprovalClaimConflictError(SupervisorApprovalError):
    def __init__(self, message: str = "Approval has already been claimed for execution"):
        super().__init__(message, code="ALREADY_CLAIMED")


class ApprovalContextDriftError(SupervisorApprovalError):
    def __init__(self, message: str = "Action context has drifted since approval was granted"):
        super().__init__(message, code="CONTEXT_DRIFT")


class ApprovalExpiredError(SupervisorApprovalError):
    def __init__(self, message: str = "Approval has expired"):
        super().__init__(message, code="APPROVAL_EXPIRED")


class ApprovalPermissionDeniedError(SupervisorApprovalError):
    def __init__(self, message: str = "Actor lacks permission for this approval operation"):
        super().__init__(message, code="PERMISSION_DENIED")


class ApprovalSelfApprovalError(SupervisorApprovalError):
    """Raised when an actor attempts to approve their own approval request (Separation of Duties violation)."""
    def __init__(self, message: str = "Self-approval prohibited: Requester cannot approve their own action"):
        super().__init__(message, code="SELF_APPROVAL_PROHIBITED")


class ApprovalUnverifiedIdentityError(SupervisorApprovalError):
    """Raised when an approval is attempted with a missing, placeholder, or shared development identity that cannot satisfy separation of duties."""
    def __init__(self, message: str = "Approval prohibited: Shared or unverified identity cannot satisfy separation of duties"):
        super().__init__(message, code="UNVERIFIED_IDENTITY_PROHIBITED")


class ApprovalLeaseExpiredError(SupervisorApprovalError):
    """Raised when a worker lease has expired and cannot be heartbeated, completed, or extended."""
    def __init__(self, message: str = "Execution lease has expired"):
        super().__init__(message, code="LEASE_EXPIRED")


class ApprovalNotLeaseOwnerError(SupervisorApprovalError):
    """Raised when an operation is attempted by a worker who is not the active lease owner."""
    def __init__(self, message: str = "Worker is not the current lease owner"):
        super().__init__(message, code="NOT_LEASE_OWNER")


class ApprovalFencingConflictError(SupervisorApprovalError):
    """Raised when a worker attempts an operation using an obsolete or superseded fencing generation."""
    def __init__(self, message: str = "Operation rejected: Lease generation is obsolete or superseded by newer generation"):
        super().__init__(message, code="STALE_GENERATION_FENCED")


class ApprovalLeaseActiveError(SupervisorApprovalError):
    """Raised when stale claim recovery is attempted on an unexpired, actively leased approval."""
    def __init__(self, message: str = "Execution lease is still active and cannot be recovered"):
        super().__init__(message, code="LEASE_STILL_ACTIVE")


# ============================================================================
# Approval Lifecycle Transition Rules
# ============================================================================

VALID_APPROVAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING": frozenset({"APPROVED", "REJECTED", "EXPIRED"}),
    "APPROVED": frozenset({"REVOKED", "EXPIRED"}),
    "REJECTED": frozenset(),  # Terminal
    "REVOKED": frozenset(),   # Terminal
    "EXPIRED": frozenset(),   # Terminal
}


def validate_approval_transition(from_status: str, to_status: str) -> None:
    """
    Validates that a transition between approval statuses is legally permitted.
    Terminal statuses (REJECTED, REVOKED, EXPIRED) cannot be transitioned to any other status.
    Raises ApprovalInvalidTransitionError if the transition is illegal.
    """
    from_upper = (from_status or "").strip().upper()
    to_upper = (to_status or "").strip().upper()

    allowed = VALID_APPROVAL_TRANSITIONS.get(from_upper)
    if allowed is None:
        raise ApprovalInvalidTransitionError(
            from_upper, to_upper, f"Unknown source status '{from_upper}' (fails closed)"
        )
    if to_upper not in allowed:
        raise ApprovalInvalidTransitionError(
            from_upper, to_upper, f"Status transition from '{from_upper}' to '{to_upper}' is not permitted"
        )


# ============================================================================
# Action Context Fingerprint Calculation
# ============================================================================

def compute_action_context_fingerprint(
    action_id: str,
    action_type: str,
    lead_id: str | uuid.UUID,
    opportunity_id: str | uuid.UUID,
    contact_id: str | uuid.UUID | None = None,
    channel: str | None = None,
    contact_email: str | None = None,
    contact_phone: str | None = None,
    opportunity_score: float | int = 0.0,
    recommendation_fit: float | int = 0.0,
    evidence_keys: Sequence[str] | None = None,
    schema_version: str = "supervisor_context_v1",
) -> str:
    """
    Computes a deterministic, canonical SHA-256 fingerprint over material action context.
    Any alteration to target contact coordinates, action identity, opportunity score,
    recommendation fit, or evidence produces a distinct hash, causing execution authorization
    to fail closed due to context drift.
    """
    clean_email = (contact_email or "").strip().lower()
    clean_phone = "".join(c for c in (contact_phone or "") if c.isdigit() or c == "+")
    clean_channel = (channel or "").strip().upper()
    clean_action_type = (action_type or "").strip().upper()

    canonical_payload = {
        "action_id": str(action_id).strip(),
        "action_type": clean_action_type,
        "channel": clean_channel,
        "contact_email": clean_email,
        "contact_id": str(contact_id) if contact_id else "",
        "contact_phone": clean_phone,
        "evidence_keys": sorted(str(k) for k in (evidence_keys or [])),
        "lead_id": str(lead_id),
        "opportunity_id": str(opportunity_id),
        "opportunity_score": round(float(opportunity_score), 2),
        "recommendation_fit": round(float(recommendation_fit), 2),
        "schema_version": schema_version,
    }
    serialized = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

"""
Strict Pydantic schemas for Controlled Autonomous Sales Supervisor.

Provides request, response, and action models for supervisor prioritization,
action readiness evaluation details, and pipeline-level summaries.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.contact_recommendation import RecommendedSalesAction
from app.domain.sales_supervisor import (
    SupervisorActionReadinessStatus,
    SupervisorActionScoreBreakdown,
    SupervisorActionTier,
    SupervisorApprovalStatus,
    SupervisorExecutionDecision,
)
from app.models.entities import now_utc


class SupervisorSchemaBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)


class SupervisorActionScoreBreakdownOut(SupervisorSchemaBase):
    """Pydantic representation of the prioritization score components."""
    opportunity_score: float = Field(ge=0.0, le=100.0)
    recommendation_fit: float = Field(ge=0.0, le=100.0)
    historical_feedback: float = Field(ge=0.0, le=100.0)
    urgency: float = Field(ge=0.0, le=100.0)
    opportunity_component: float = Field(ge=0.0, le=100.0)
    recommendation_component: float = Field(ge=0.0, le=100.0)
    feedback_component: float = Field(ge=0.0, le=100.0)
    urgency_component: float = Field(ge=0.0, le=100.0)
    final_score: float = Field(ge=0.0, le=100.0)
    feedback_available: bool
    fallback_applied: bool

    @classmethod
    def from_breakdown(cls, breakdown: SupervisorActionScoreBreakdown) -> SupervisorActionScoreBreakdownOut:
        return cls(
            opportunity_score=breakdown.opportunity_score,
            recommendation_fit=breakdown.recommendation_fit,
            historical_feedback=breakdown.historical_feedback,
            urgency=breakdown.urgency,
            opportunity_component=breakdown.opportunity_component,
            recommendation_component=breakdown.recommendation_component,
            feedback_component=breakdown.feedback_component,
            urgency_component=breakdown.urgency_component,
            final_score=breakdown.final_score,
            feedback_available=breakdown.feedback_available,
            fallback_applied=breakdown.fallback_applied,
        )


class ActionReadinessDetails(SupervisorSchemaBase):
    """Detailed audit information regarding readiness gate evaluations."""
    status: SupervisorActionReadinessStatus
    reasons: list[str] = Field(default_factory=list)
    has_valid_contact: bool = True
    has_evidence: bool = True
    is_opted_out: bool = False
    in_cooldown: bool = False
    has_active_task: bool = False
    blocking_details: dict[str, Any] | None = None


class PrioritizedSalesAction(SupervisorSchemaBase):
    """
    A single evaluated sales action candidate prioritized by the supervisor.
    Separates the mathematical ranking score from the readiness status and approval controls.
    """
    action_id: str = Field(min_length=1, max_length=128)
    lead_id: UUID
    opportunity_id: UUID
    contact_id: UUID | None = None
    recommended_action: RecommendedSalesAction
    opportunity_score: float = Field(ge=0.0, le=100.0)
    recommendation_fit: float = Field(ge=0.0, le=100.0)
    historical_feedback_rate: float | None = Field(default=None, ge=0.0, le=100.0)
    urgency_score: float = Field(ge=0.0, le=100.0)
    priority_score: float = Field(ge=0.0, le=100.0)
    score_breakdown: SupervisorActionScoreBreakdownOut | None = None
    readiness_status: SupervisorActionReadinessStatus
    readiness_details: ActionReadinessDetails
    evidence_keys: list[str] = Field(default_factory=list)
    policy_approval_required: bool = True
    created_at: datetime = Field(default_factory=now_utc)


class SalesSupervisorPipelineResponse(SupervisorSchemaBase):
    """Aggregated pipeline-level supervisor evaluation response."""
    evaluated_at: datetime = Field(default_factory=now_utc)
    total_candidates: int = Field(ge=0)
    ready_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    actions: list[PrioritizedSalesAction] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class SupervisorHumanApproval(SupervisorSchemaBase):
    """
    Typed representation of a human approval decision for a candidate sales action.
    Binds the human approver identity, timestamp, decision, and exact target identity.
    """
    approval_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    action_id: str = Field(min_length=1, max_length=180)
    lead_id: UUID
    opportunity_id: UUID
    contact_id: UUID | None = None
    channel: str | None = None
    approver_id: str = Field(min_length=1, max_length=120)
    status: SupervisorApprovalStatus = SupervisorApprovalStatus.APPROVED
    approved_at: datetime = Field(default_factory=now_utc)
    notes: str | None = None
    expires_at: datetime | None = None


class SupervisorExecutionAuthorizationResult(SupervisorSchemaBase):
    """
    Typed authorization decision output determining whether an action may proceed.
    Separates readiness, approval verification, and execution authorization.
    """
    authorized: bool
    decision: SupervisorExecutionDecision
    action_id: str
    reasons: list[str] = Field(default_factory=list)
    readiness_status: SupervisorActionReadinessStatus
    action_tier: SupervisorActionTier
    approval_required: bool
    approval_details: SupervisorHumanApproval | None = None
    denial_code: str | None = None
    evaluated_at: datetime = Field(default_factory=now_utc)


class SupervisorApprovalCreateRequest(SupervisorSchemaBase):
    """Request payload to create a new pending durable supervisor approval."""
    action_id: str = Field(min_length=1, max_length=180)
    lead_id: UUID
    opportunity_id: UUID
    contact_id: UUID | None = None
    recommended_action: str = Field(min_length=1, max_length=60)
    action_tier: SupervisorActionTier = SupervisorActionTier.EXTERNAL_OUTREACH
    channel: str | None = None
    context_fingerprint: str = Field(min_length=64, max_length=64)
    requested_by_user_id: str = Field(min_length=1, max_length=120)
    expires_in_hours: int = Field(default=48, ge=1, le=720)
    notes: str | None = None


class SupervisorApprovalDecisionRequest(SupervisorSchemaBase):
    """Request payload for an approver decision (approve, reject, revoke)."""
    expected_version: int | None = None
    decision_reason: str | None = Field(default=None, max_length=500)
    notes: str | None = None
    expires_in_hours: int = Field(default=48, ge=1, le=720)


class SupervisorApprovalClaimRequest(SupervisorSchemaBase):
    """Request payload to atomically claim an approved action for execution."""
    worker_id: str = Field(min_length=1, max_length=120)
    expected_version: int = Field(ge=1)


class SupervisorApprovalRecordOut(SupervisorSchemaBase):
    """Pydantic representation of a persisted SupervisorHumanApproval entity."""
    id: UUID
    action_id: str
    lead_id: UUID
    opportunity_id: UUID
    contact_id: UUID | None = None
    recommended_action: str
    action_tier: str
    channel: str | None = None
    context_fingerprint: str
    status: str
    requested_by_user_id: str
    approved_by_user_id: str | None = None
    rejected_by_user_id: str | None = None
    revoked_by_user_id: str | None = None
    created_at: datetime
    approved_at: datetime | None = None
    expires_at: datetime | None = None
    updated_at: datetime
    notes: str | None = None
    decision_reason: str | None = None
    version: int
    execution_claimed_at: datetime | None = None
    execution_claimed_by: str | None = None
    lease_generation: int | None = None
    lease_duration_seconds: int | None = None
    lease_expires_at: datetime | None = None


class SupervisorApprovalHeartbeatRequest(SupervisorSchemaBase):
    """Request payload to report heartbeat and extend an active worker lease."""
    worker_id: str = Field(min_length=1, max_length=120)
    lease_generation: int = Field(ge=1)


class SupervisorApprovalHeartbeatResponse(SupervisorSchemaBase):
    """Response payload acknowledging a valid worker heartbeat."""
    approval_id: UUID
    worker_id: str
    lease_generation: int
    lease_expires_at: datetime
    last_heartbeat_at: datetime
    status: str = "APPROVED"


class SupervisorApprovalReleaseRequest(SupervisorSchemaBase):
    """Request payload to cleanly release an active worker lease back to the pool."""
    worker_id: str = Field(min_length=1, max_length=120)
    lease_generation: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=500)


class SupervisorApprovalRecoverRequest(SupervisorSchemaBase):
    """Request payload to recover a stale execution lease."""
    expected_worker_id: str | None = Field(default=None, max_length=120)
    expected_generation: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=500)


class SupervisorApprovalAuditRecordOut(SupervisorSchemaBase):
    """Pydantic representation of a persisted SupervisorApprovalAuditEvent entity."""
    id: UUID
    approval_id: UUID | None = None
    approval_reference_id: UUID
    action_id: str
    lead_id: UUID
    actor_id: str
    actor_role: str
    event_type: str
    from_status: str | None = None
    to_status: str
    context_fingerprint: str
    reason: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class SupervisorApprovalRequestCreate(SupervisorSchemaBase):
    """
    Client request to initiate a human approval for a candidate action.
    Authoritative contact coordinates, scores, evidence, and fingerprint
    are derived server-side from database records.
    """
    lead_id: UUID
    opportunity_id: UUID
    contact_id: UUID | None = None
    action_id: str | None = Field(default=None, max_length=180)
    recommended_action: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=2000)
    expires_in_hours: int = Field(default=48, ge=1, le=720)


class SupervisorApprovalApproveRequest(SupervisorSchemaBase):
    """Request payload to approve a pending supervisor action."""
    expected_version: int | None = None
    decision_reason: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)
    expires_in_hours: int = Field(default=48, ge=1, le=720)


class SupervisorApprovalRejectRequest(SupervisorSchemaBase):
    """Request payload to reject a pending supervisor action."""
    expected_version: int | None = None
    decision_reason: str = Field(min_length=1, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)


class SupervisorApprovalRevokeRequest(SupervisorSchemaBase):
    """Request payload to revoke an active supervisor approval."""
    expected_version: int | None = None
    revocation_reason: str = Field(min_length=1, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)


class SupervisorApprovalListResponse(SupervisorSchemaBase):
    """Paginated list of supervisor human approval records."""
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    approvals: list[SupervisorApprovalRecordOut] = Field(default_factory=list)


class SupervisorApprovalAuditListResponse(SupervisorSchemaBase):
    """Audit event trail for an approval."""
    approval_id: UUID
    approval_reference_id: UUID
    total: int = Field(ge=0)
    events: list[SupervisorApprovalAuditRecordOut] = Field(default_factory=list)


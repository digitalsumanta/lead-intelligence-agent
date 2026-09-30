"""Strict API contracts for deterministic agent policy evaluations."""

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


POLICY_VERSION = "agent-policy-v1"


class PolicyContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PolicyAction(StrEnum):
    FOLLOW_UP = "FOLLOW_UP"
    CALL_PROSPECT = "CALL_PROSPECT"
    REVIEW_SEO_OPPORTUNITY = "REVIEW_SEO_OPPORTUNITY"
    REVIEW_OUTREACH = "REVIEW_OUTREACH"
    PREPARE_PROPOSAL = "PREPARE_PROPOSAL"
    INVESTIGATE_MISSING_INFORMATION = "INVESTIGATE_MISSING_INFORMATION"
    REVIEW_COMMERCIAL_CONTEXT = "REVIEW_COMMERCIAL_CONTEXT"


class PolicyDecision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_HUMAN_APPROVAL = "REQUIRE_HUMAN_APPROVAL"


class AutonomyLevel(StrEnum):
    HUMAN_ONLY = "HUMAN_ONLY"
    ASSISTED = "ASSISTED"
    SUPERVISED = "SUPERVISED"
    AUTONOMOUS = "AUTONOMOUS"


class PolicyChannel(StrEnum):
    EMAIL = "EMAIL"
    WHATSAPP = "WHATSAPP"
    PHONE = "PHONE"


class PolicyEvaluationRequest(PolicyContract):
    action_type: PolicyAction
    requested_channel: PolicyChannel | None = None
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=100)
    opportunity_ids: tuple[str, ...] = Field(default=(), max_length=50)
    service_ids: tuple[str, ...] = Field(default=(), max_length=50)
    source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def require_channel_for_communication(self):
        if self.action_type in {PolicyAction.FOLLOW_UP, PolicyAction.CALL_PROSPECT} and self.requested_channel is None:
            raise ValueError("requested_channel is required for communication actions")
        if self.action_type not in {PolicyAction.FOLLOW_UP, PolicyAction.CALL_PROSPECT} and self.requested_channel is not None:
            raise ValueError("requested_channel is only valid for communication actions")
        if self.action_type == PolicyAction.CALL_PROSPECT and self.requested_channel not in {None, PolicyChannel.PHONE}:
            raise ValueError("CALL_PROSPECT requires the PHONE channel")
        return self


class PolicyRuleResult(PolicyContract):
    rule: str = Field(min_length=1, max_length=80)
    passed: bool
    result: Literal["PASS", "DENY", "REQUIRE_HUMAN_APPROVAL"]
    reason: str = Field(min_length=1, max_length=240)


class PolicyEvaluationOut(PolicyContract):
    evaluation_id: UUID
    lead_id: UUID
    action_type: PolicyAction
    requested_channel: PolicyChannel | None
    decision: PolicyDecision
    reason: str
    evidence_ids: tuple[str, ...]
    opportunity_ids: tuple[str, ...]
    service_ids: tuple[str, ...]
    policy_version: Literal["agent-policy-v1"]
    autonomy_level: AutonomyLevel
    source_fingerprint: str | None
    request_fingerprint: str
    requested_by: str
    rule_results: tuple[PolicyRuleResult, ...]
    evaluated_at: datetime


class PolicyEvaluationHistory(PolicyContract):
    items: tuple[PolicyEvaluationOut, ...] = Field(max_length=100)

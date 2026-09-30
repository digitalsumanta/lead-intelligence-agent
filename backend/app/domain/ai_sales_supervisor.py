"""Strict contracts for evidence-grounded AI Sales Supervisor output."""

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.ai_sales_action_planner import ActionType
from app.domain.ai_sales_intelligence import CONTEXT_VERSION


ARTIFACT_VERSION = "ai-sales-supervisor-v1"
PROMPT_VERSION = "ai-sales-supervisor-prompt-v1"


class SupervisorAttentionType(StrEnum):
    FOLLOW_UP_DUE = "FOLLOW_UP_DUE"
    FOLLOW_UP_OVERDUE = "FOLLOW_UP_OVERDUE"
    RECENT_OUTREACH = "RECENT_OUTREACH"
    OUTREACH_RESPONSE_PENDING = "OUTREACH_RESPONSE_PENDING"
    PROPOSAL_REVIEW = "PROPOSAL_REVIEW"
    SEO_OPPORTUNITY = "SEO_OPPORTUNITY"
    MISSING_INFORMATION = "MISSING_INFORMATION"
    COMMERCIAL_REVIEW = "COMMERCIAL_REVIEW"
    STALE_INTELLIGENCE = "STALE_INTELLIGENCE"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"
    NO_ACTION_REQUIRED = "NO_ACTION_REQUIRED"


class SupervisorUrgency(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class SupervisorPolicyDecision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_HUMAN_APPROVAL = "REQUIRE_HUMAN_APPROVAL"


class SupervisorAutonomyLevel(StrEnum):
    HUMAN_ONLY = "HUMAN_ONLY"
    ASSISTED = "ASSISTED"
    SUPERVISED = "SUPERVISED"
    AUTONOMOUS = "AUTONOMOUS"


class SupervisorRationale(StrEnum):
    FOLLOW_UP_DUE = "FOLLOW_UP_DUE"
    RECENT_OUTREACH = "RECENT_OUTREACH"
    PROPOSAL_REVIEW = "PROPOSAL_REVIEW"
    SEO_OPPORTUNITY = "SEO_OPPORTUNITY"
    MISSING_INFORMATION = "MISSING_INFORMATION"
    COMMERCIAL_REVIEW = "COMMERCIAL_REVIEW"
    STALE_INTELLIGENCE = "STALE_INTELLIGENCE"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    HUMAN_APPROVAL = "HUMAN_APPROVAL"
    NO_ACTION = "NO_ACTION"


class SupervisorContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Ref = Annotated[str, Field(min_length=1, max_length=180)]
ServiceRef = Annotated[str, Field(min_length=1, max_length=80)]


class SupervisorSelection(SupervisorContract):
    """Provider selection contract: enums and supplied IDs only, no generated prose."""

    lead_id: UUID
    attention_type: SupervisorAttentionType
    urgency: SupervisorUrgency
    rationale_code: SupervisorRationale
    evidence_ids: tuple[Ref, ...] = Field(min_length=1, max_length=12)
    action_type: ActionType | None
    requested_channel: Literal["EMAIL", "WHATSAPP", "PHONE"] | None = None
    opportunity_ids: tuple[Ref, ...] = Field(default=(), max_length=10)
    service_ids: tuple[ServiceRef, ...] = Field(default=(), max_length=5)
    missing_information: tuple[Ref, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def unique_references_and_action_channel(self):
        for values in (self.evidence_ids, self.opportunity_ids, self.service_ids, self.missing_information):
            if len(set(values)) != len(values):
                raise ValueError("references must be unique")
        if self.action_type in {ActionType.FOLLOW_UP, ActionType.CALL_PROSPECT} and self.requested_channel is None:
            raise ValueError("communication action requires a channel")
        if self.action_type not in {ActionType.FOLLOW_UP, ActionType.CALL_PROSPECT} and self.requested_channel is not None:
            raise ValueError("channel only applies to communication actions")
        if self.action_type is None and self.attention_type != SupervisorAttentionType.NO_ACTION_REQUIRED:
            raise ValueError("a finding without an action must be NO_ACTION_REQUIRED")
        if self.action_type is not None and self.attention_type == SupervisorAttentionType.NO_ACTION_REQUIRED:
            raise ValueError("NO_ACTION_REQUIRED cannot recommend an action")
        return self


class SupervisorSelectionOutput(SupervisorContract):
    findings: tuple[SupervisorSelection, ...] = Field(max_length=30)


class SupervisorFinding(SupervisorContract):
    finding_id: str = Field(min_length=64, max_length=64)
    lead_id: UUID
    attention_type: SupervisorAttentionType
    urgency: SupervisorUrgency
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=320)
    rationale: str = Field(min_length=1, max_length=500)
    evidence_ids: tuple[Ref, ...] = Field(min_length=1, max_length=12)
    action_type: ActionType | None
    action_priority: SupervisorUrgency
    opportunity_ids: tuple[Ref, ...] = Field(default=(), max_length=10)
    service_ids: tuple[ServiceRef, ...] = Field(default=(), max_length=5)
    missing_information: tuple[Ref, ...] = Field(default=(), max_length=10)
    policy_decision: SupervisorPolicyDecision | None
    autonomy_level: SupervisorAutonomyLevel | None
    review_required: bool


class PipelineSummary(SupervisorContract):
    pipeline_total_leads: int = Field(ge=0)
    total_leads_reviewed: int = Field(ge=0)
    leads_needing_attention: int = Field(ge=0)
    overdue_followups: int = Field(ge=0)
    followups_due: int = Field(ge=0)
    proposal_reviews: int = Field(ge=0)
    seo_opportunity_reviews: int = Field(ge=0)
    missing_information_cases: int = Field(ge=0)
    policy_blocked_cases: int = Field(ge=0)
    human_approval_cases: int = Field(ge=0)


class SupervisorResult(SupervisorContract):
    artifact_version: Literal["ai-sales-supervisor-v1"]
    prompt_version: Literal["ai-sales-supervisor-prompt-v1"]
    context_version: Literal["ai-sales-context-v1"]
    generated_at: str = Field(min_length=1, max_length=40)
    context_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    findings: tuple[SupervisorFinding, ...] = Field(max_length=30)
    pipeline_summary: PipelineSummary
    review_notes: tuple[str, ...] = Field(max_length=8)
    evidence_refs: tuple[Ref, ...] = Field(max_length=360)
    usage: dict = Field(default_factory=dict)

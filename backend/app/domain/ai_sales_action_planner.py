"""Strict, bounded contract for evidence-grounded AI sales action plans."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


CONTRACT_VERSION = "ai-sales-action-planner-v1"
PROMPT_VERSION = "ai-sales-action-planner-prompt-v1"
CONTEXT_VERSION = "ai-sales-context-v1"


class ActionType(StrEnum):
    FOLLOW_UP = "FOLLOW_UP"
    CALL_PROSPECT = "CALL_PROSPECT"
    REVIEW_SEO_OPPORTUNITY = "REVIEW_SEO_OPPORTUNITY"
    REVIEW_OUTREACH = "REVIEW_OUTREACH"
    PREPARE_PROPOSAL = "PREPARE_PROPOSAL"
    INVESTIGATE_MISSING_INFORMATION = "INVESTIGATE_MISSING_INFORMATION"
    REVIEW_COMMERCIAL_CONTEXT = "REVIEW_COMMERCIAL_CONTEXT"


class ActionPriority(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"


class PlannerContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Ref = Annotated[str, Field(min_length=1, max_length=180)]
ServiceRef = Annotated[str, Field(min_length=1, max_length=80)]

# The model selects a bounded interpretation; rationale wording is rendered from
# this allowlist so a provider cannot introduce an uncited factual assertion.
RATIONALES = (
    "The cited records may warrant human review.",
    "The cited records may support a follow-up discussion.",
    "The cited records may support reviewing an existing opportunity.",
)

PREPARATION_ITEMS = (
    "Review the cited evidence.",
    "Confirm the relevant details with the lead.",
    "Check the current sales and commercial context.",
    "Review the current SEO findings.",
    "Review the recorded outreach history.",
    "Verify the missing information before proceeding.",
    "Review the existing service recommendation.",
)

ACTION_TITLES = {
    ActionType.FOLLOW_UP: "Follow up with prospect",
    ActionType.CALL_PROSPECT: "Call prospect",
    ActionType.REVIEW_SEO_OPPORTUNITY: "Review SEO opportunity",
    ActionType.REVIEW_OUTREACH: "Review outreach history",
    ActionType.PREPARE_PROPOSAL: "Review proposal next step",
    ActionType.INVESTIGATE_MISSING_INFORMATION: "Investigate missing information",
    ActionType.REVIEW_COMMERCIAL_CONTEXT: "Review commercial context",
}


class RecommendedAction(PlannerContract):
    action_type: ActionType
    priority: ActionPriority
    title: Annotated[str, Field(min_length=1, max_length=160)]
    rationale: Annotated[str, Field(min_length=1, max_length=200)]
    evidence_ids: tuple[Ref, ...] = Field(min_length=1, max_length=10)
    opportunity_ids: tuple[Ref, ...] = Field(max_length=10)
    service_ids: tuple[ServiceRef, ...] = Field(max_length=5)
    preparation_items: tuple[Annotated[str, Field(min_length=1, max_length=120)], ...] = Field(max_length=7)

    @model_validator(mode="after")
    def bounded_text_and_refs(self):
        if self.title != ACTION_TITLES[self.action_type]:
            raise ValueError("title must use its server-approved action label")
        if self.rationale not in RATIONALES:
            raise ValueError("rationale must use an approved non-factual phrase")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("evidence_ids must be unique")
        if len(set(self.opportunity_ids)) != len(self.opportunity_ids):
            raise ValueError("opportunity_ids must be unique")
        if len(set(self.service_ids)) != len(self.service_ids):
            raise ValueError("service_ids must be unique")
        if len(set(self.preparation_items)) != len(self.preparation_items):
            raise ValueError("preparation_items must be unique")
        if any(item not in PREPARATION_ITEMS for item in self.preparation_items):
            raise ValueError("preparation items must use approved labels")
        return self


class ActionPlanOutput(PlannerContract):
    contract_version: Literal["ai-sales-action-planner-v1"] = CONTRACT_VERSION
    prompt_version: Literal["ai-sales-action-planner-prompt-v1"] = PROMPT_VERSION
    actions: tuple[RecommendedAction, ...] = Field(max_length=10)

    @model_validator(mode="after")
    def no_duplicate_recommendations(self):
        identities = [(a.action_type, a.evidence_ids, a.opportunity_ids, a.service_ids) for a in self.actions]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate recommendations are not allowed")
        return self

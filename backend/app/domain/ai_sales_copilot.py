"""Strict contracts for the bounded AI Sales Copilot."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


COPILOT_CONTRACT_VERSION = "ai-sales-copilot-v1"
COPILOT_PROMPT_VERSION = "ai-sales-copilot-prompt-v1"

ANSWER_TEMPLATES = {
    "NEXT_STEP": "Review the current facts and opportunities below, then choose a human next step.",
    "WHY_FOLLOW_UP": "Use the cited records to decide whether a follow-up is appropriate.",
    "MAIN_OPPORTUNITIES": "These are the current deterministic opportunities relevant to your question.",
    "MISSING_INFORMATION": "These are the available missing or uncertain information markers.",
    "PREVIOUS_OUTREACH": "Review the cited outreach events; this answer reflects recorded history only.",
    "CURRENT_SEO_OPPORTUNITIES": "These are opportunities from the latest completed SEO audit.",
    "REVIEW_BEFORE_CONTACT": "Review the cited lead, sales, and outreach records before contacting the lead.",
    "UNSUPPORTED": "This question is outside the current Sales Copilot scope. Choose one of the supported questions.",
}


class CopilotIntent(StrEnum):
    NEXT_STEP = "NEXT_STEP"
    WHY_FOLLOW_UP = "WHY_FOLLOW_UP"
    MAIN_OPPORTUNITIES = "MAIN_OPPORTUNITIES"
    MISSING_INFORMATION = "MISSING_INFORMATION"
    PREVIOUS_OUTREACH = "PREVIOUS_OUTREACH"
    CURRENT_SEO_OPPORTUNITIES = "CURRENT_SEO_OPPORTUNITIES"
    REVIEW_BEFORE_CONTACT = "REVIEW_BEFORE_CONTACT"
    UNSUPPORTED = "UNSUPPORTED"


class CopilotReviewActionType(StrEnum):
    REVIEW_LEAD = "REVIEW_LEAD"
    REVIEW_OPPORTUNITY = "REVIEW_OPPORTUNITY"
    REVIEW_SEO = "REVIEW_SEO"
    REVIEW_OUTREACH = "REVIEW_OUTREACH"
    REVIEW_COMMERCIAL_CONTEXT = "REVIEW_COMMERCIAL_CONTEXT"
    VERIFY_MISSING_INFORMATION = "VERIFY_MISSING_INFORMATION"


class CopilotContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


EvidenceId = Annotated[str, Field(min_length=1, max_length=180)]


class CopilotReviewAction(CopilotContract):
    action: CopilotReviewActionType
    evidence_refs: tuple[EvidenceId, ...] = Field(min_length=1, max_length=10)
    service_ids: tuple[Annotated[str, Field(min_length=1, max_length=80)], ...] = Field(max_length=5)


class CopilotModelOutput(CopilotContract):
    """Provider contract contains selections only, never free-form factual prose."""

    intent: Literal[
        "NEXT_STEP", "WHY_FOLLOW_UP", "MAIN_OPPORTUNITIES", "MISSING_INFORMATION",
        "PREVIOUS_OUTREACH", "CURRENT_SEO_OPPORTUNITIES", "REVIEW_BEFORE_CONTACT",
    ]
    key_facts: tuple[EvidenceId, ...] = Field(max_length=10)
    relevant_opportunities: tuple[EvidenceId, ...] = Field(max_length=10)
    recommended_review_actions: tuple[CopilotReviewAction, ...] = Field(max_length=10)
    missing_information: tuple[EvidenceId, ...] = Field(max_length=20)
    evidence_refs: tuple[EvidenceId, ...] = Field(max_length=50)


class CopilotResponse(CopilotContract):
    contract_version: Literal["ai-sales-copilot-v1"] = COPILOT_CONTRACT_VERSION
    prompt_version: Literal["ai-sales-copilot-prompt-v1"] = COPILOT_PROMPT_VERSION
    intent: CopilotIntent
    answer: Annotated[str, Field(min_length=1, max_length=300)]
    key_facts: tuple[EvidenceId, ...] = Field(max_length=10)
    relevant_opportunities: tuple[EvidenceId, ...] = Field(max_length=10)
    recommended_review_actions: tuple[CopilotReviewAction, ...] = Field(max_length=10)
    missing_information: tuple[EvidenceId, ...] = Field(max_length=20)
    evidence_refs: tuple[EvidenceId, ...] = Field(max_length=50)

    @model_validator(mode="after")
    def answer_is_server_template_and_refs_match(self):
        if self.answer != ANSWER_TEMPLATES[self.intent.value]:
            raise ValueError("copilot answer must use its fixed server template")
        refs = set(self.key_facts) | set(self.relevant_opportunities) | set(self.missing_information)
        for action in self.recommended_review_actions:
            refs.update(action.evidence_refs)
        if set(self.evidence_refs) != refs or len(self.evidence_refs) != len(refs):
            raise ValueError("copilot evidence_refs must exactly match unique selected evidence")
        if self.intent == CopilotIntent.UNSUPPORTED and refs:
            raise ValueError("unsupported questions cannot contain evidence-based recommendations")
        return self

"""Strict contracts for evidence-grounded AI sales interpretation."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import SalesTaskType


CONTRACT_VERSION = "ai-sales-intelligence-v1"
PROMPT_VERSION = "ai-sales-intelligence-prompt-v1"
CONTEXT_VERSION = "ai-sales-context-v1"


class AIClaimType(StrEnum):
    VERIFIED_FACT = "VERIFIED_FACT"
    SUPPORTED_INFERENCE = "SUPPORTED_INFERENCE"
    SUGGESTED_LANGUAGE = "SUGGESTED_LANGUAGE"
    UNCERTAINTY = "UNCERTAINTY"


class AIActionType(StrEnum):
    REVIEW_SEO_OPPORTUNITY = "REVIEW_SEO_OPPORTUNITY"
    CALL_PROSPECT = "CALL_PROSPECT"
    FOLLOW_UP = "FOLLOW_UP"
    REVIEW_OUTREACH = "REVIEW_OUTREACH"
    PREPARE_PROPOSAL = "PREPARE_PROPOSAL"
    INVESTIGATE_MISSING_INFORMATION = "INVESTIGATE_MISSING_INFORMATION"
    REVIEW_COMMERCIAL_CONTEXT = "REVIEW_COMMERCIAL_CONTEXT"


class AISalesContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContextEvidence(AISalesContract):
    id: Annotated[str, Field(min_length=1, max_length=180)]
    source_type: Annotated[str, Field(min_length=1, max_length=80)]
    label: Annotated[str, Field(min_length=1, max_length=250)]
    data: dict[str, Any]


class ContextItem(AISalesContract):
    id: Annotated[str, Field(min_length=1, max_length=180)]
    source_type: Annotated[str, Field(min_length=1, max_length=80)]
    label: Annotated[str, Field(min_length=1, max_length=250)]
    evidence_refs: tuple[str, ...] = ()
    data: dict[str, Any]


class AISalesContext(AISalesContract):
    context_version: Literal["ai-sales-context-v1"] = CONTEXT_VERSION
    lead: dict[str, Any]
    verified_facts: tuple[ContextEvidence, ...]
    deterministic_opportunities: tuple[ContextItem, ...]
    historical_events: tuple[ContextItem, ...]
    commercial_data: tuple[ContextItem, ...]
    seo_data: tuple[ContextItem, ...]
    uncertain_or_missing: tuple[ContextEvidence, ...]
    active_service_ids: tuple[str, ...]


SUGGESTED_LANGUAGE_ALLOWLIST = frozenset({
    "Would you be open to discussing these business improvement areas?",
    "Would it be useful to review the available findings together?",
    "Would you like to discuss a possible next step?",
})

# Model-authored prose cannot be semantically proven by reference validation alone.
# Keep interpretive statements in a small, explicitly non-factual vocabulary.
SUPPORTED_INFERENCE_ALLOWLIST = frozenset({
    "The cited records may warrant human review.",
    "The cited records may support a follow-up discussion.",
    "The cited records may support reviewing an existing opportunity.",
})
UNCERTAINTY_ALLOWLIST = frozenset({
    "The supplied information is insufficient to determine this; verify with the lead.",
})


class AIClaim(AISalesContract):
    kind: AIClaimType
    text: Annotated[str | None, Field(max_length=2000)] = None
    evidence_refs: tuple[Annotated[str, Field(min_length=1, max_length=180)], ...] = ()

    @model_validator(mode="after")
    def enforce_grounding_shape(self):
        if self.kind == AIClaimType.VERIFIED_FACT:
            if self.text is not None or len(self.evidence_refs) != 1:
                raise ValueError("verified facts select exactly one evidence item and contain no model-written claim")
        elif self.kind in {AIClaimType.SUPPORTED_INFERENCE, AIClaimType.UNCERTAINTY}:
            if not self.text or not self.evidence_refs:
                raise ValueError("inferences and uncertainties require text and evidence references")
            allowed = SUPPORTED_INFERENCE_ALLOWLIST if self.kind == AIClaimType.SUPPORTED_INFERENCE else UNCERTAINTY_ALLOWLIST
            if self.text not in allowed:
                raise ValueError("model-authored interpretive text must use an approved non-factual phrase")
        elif self.kind == AIClaimType.SUGGESTED_LANGUAGE:
            if self.text not in SUGGESTED_LANGUAGE_ALLOWLIST or self.evidence_refs:
                raise ValueError("suggested language must use an approved non-factual phrase")
        return self


class AIOpportunity(AISalesContract):
    title: Annotated[str, Field(min_length=1, max_length=250)]
    rationale: AIClaim
    source_refs: tuple[Annotated[str, Field(min_length=1, max_length=180)], ...] = Field(min_length=1, max_length=10)
    action_type: AIActionType
    service_ids: tuple[Annotated[str, Field(min_length=1, max_length=80)], ...] = Field(max_length=5)

    @model_validator(mode="after")
    def rationale_is_interpretive(self):
        if self.rationale.kind != AIClaimType.SUPPORTED_INFERENCE:
            raise ValueError("opportunity rationale must be a cited supported inference")
        return self


class AIRecommendedAction(AISalesContract):
    action_type: AIActionType
    rationale: AIClaim
    source_refs: tuple[Annotated[str, Field(min_length=1, max_length=180)], ...] = Field(min_length=1, max_length=10)
    task_type: SalesTaskType | None = None
    service_ids: tuple[Annotated[str, Field(min_length=1, max_length=80)], ...] = Field(max_length=5)

    @model_validator(mode="after")
    def rationale_is_cited(self):
        if self.rationale.kind not in {AIClaimType.SUPPORTED_INFERENCE, AIClaimType.UNCERTAINTY}:
            raise ValueError("recommended actions require an inference or uncertainty rationale")
        return self


class AISalesIntelligenceResponse(AISalesContract):
    contract_version: Literal["ai-sales-intelligence-v1"] = CONTRACT_VERSION
    prompt_version: Literal["ai-sales-intelligence-prompt-v1"] = PROMPT_VERSION
    summary: tuple[AIClaim, ...] = Field(min_length=1, max_length=8)
    insights: tuple[AIClaim, ...] = Field(max_length=20)
    opportunities: tuple[AIOpportunity, ...] = Field(max_length=10)
    recommended_actions: tuple[AIRecommendedAction, ...] = Field(max_length=15)
    uncertainties: tuple[AIClaim, ...] = Field(max_length=10)
    evidence_refs: tuple[Annotated[str, Field(min_length=1, max_length=180)], ...] = Field(max_length=100)

    @model_validator(mode="after")
    def uncertainties_are_uncertainty_claims(self):
        if any(claim.kind != AIClaimType.UNCERTAINTY for claim in self.uncertainties):
            raise ValueError("uncertainties must use the UNCERTAINTY claim kind")
        return self


class AIValidationError(ValueError):
    """Generated AI sales intelligence is malformed or not grounded in its context."""

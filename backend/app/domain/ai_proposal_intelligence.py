"""Strict selection and rendered contracts for human-reviewed proposal briefs."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


CONTRACT_VERSION = "ai-proposal-intelligence-v1"
PROMPT_VERSION = "ai-proposal-intelligence-prompt-v1"
CONTEXT_VERSION = "ai-sales-context-v1"


class ProposalType(StrEnum):
    SEO_REVIEW = "SEO_REVIEW"
    WEBSITE_IMPROVEMENT = "WEBSITE_IMPROVEMENT"
    COMMERCIAL_REVIEW = "COMMERCIAL_REVIEW"
    GENERAL_REVIEW = "GENERAL_REVIEW"


class NeedType(StrEnum):
    SEO_FINDING = "SEO_FINDING"
    DETERMINISTIC_OPPORTUNITY = "DETERMINISTIC_OPPORTUNITY"
    RECORDED_OUTREACH = "RECORDED_OUTREACH"
    COMMERCIAL_REVIEW = "COMMERCIAL_REVIEW"
    MISSING_INFORMATION = "MISSING_INFORMATION"
    LEAD_FACT = "LEAD_FACT"


class ScopeType(StrEnum):
    REVIEW_CURRENT_SEO_FINDINGS = "REVIEW_CURRENT_SEO_FINDINGS"
    REVIEW_EXISTING_DETERMINISTIC_OPPORTUNITY = "REVIEW_EXISTING_DETERMINISTIC_OPPORTUNITY"
    DISCUSS_EXISTING_SERVICE_RECOMMENDATION = "DISCUSS_EXISTING_SERVICE_RECOMMENDATION"
    REVIEW_RECORDED_OUTREACH = "REVIEW_RECORDED_OUTREACH"
    REVIEW_COMMERCIAL_CONTEXT = "REVIEW_COMMERCIAL_CONTEXT"
    VERIFY_MISSING_INFORMATION = "VERIFY_MISSING_INFORMATION"


class DeliverableType(StrEnum):
    FINDINGS_REVIEW = "FINDINGS_REVIEW"
    RECOMMENDATION_REVIEW = "RECOMMENDATION_REVIEW"
    SCOPE_DISCUSSION = "SCOPE_DISCUSSION"
    INFORMATION_CHECKLIST = "INFORMATION_CHECKLIST"
    COMMERCIAL_REVIEW = "COMMERCIAL_REVIEW"


class AssumptionType(StrEnum):
    HUMAN_SCOPE_CONFIRMATION = "HUMAN_SCOPE_CONFIRMATION"
    SEPARATE_IMPLEMENTATION_REVIEW = "SEPARATE_IMPLEMENTATION_REVIEW"


class ExclusionType(StrEnum):
    NO_PRICE_OR_PAYMENT_TERMS = "NO_PRICE_OR_PAYMENT_TERMS"
    NO_DELIVERY_COMMITMENT = "NO_DELIVERY_COMMITMENT"
    NO_EXTERNAL_QUOTE = "NO_EXTERNAL_QUOTE"
    NO_GUARANTEED_RESULTS = "NO_GUARANTEED_RESULTS"


class ReviewNoteType(StrEnum):
    VERIFY_SCOPE_WITH_LEAD = "VERIFY_SCOPE_WITH_LEAD"
    REVIEW_CURRENT_EVIDENCE = "REVIEW_CURRENT_EVIDENCE"
    CONFIRM_COMMERCIAL_VALUES = "CONFIRM_COMMERCIAL_VALUES"
    VERIFY_MISSING_INFORMATION = "VERIFY_MISSING_INFORMATION"


class ProposalContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Ref = Annotated[str, Field(min_length=1, max_length=180)]
ServiceId = Annotated[str, Field(min_length=1, max_length=80)]


class NeedSelection(ProposalContract):
    need_type: NeedType
    evidence_refs: tuple[Ref, ...] = Field(min_length=1, max_length=10)


class ServiceSelection(ProposalContract):
    service_id: ServiceId
    evidence_refs: tuple[Ref, ...] = Field(min_length=1, max_length=10)


class OpportunityEvidenceSelection(ProposalContract):
    opportunity_id: Ref
    evidence_refs: tuple[Ref, ...] = Field(min_length=1, max_length=10)


class ScopeSelection(ProposalContract):
    scope_type: ScopeType
    evidence_refs: tuple[Ref, ...] = Field(min_length=1, max_length=10)


class ProposalSelection(ProposalContract):
    """Provider output is selections only; prose and values are rendered server-side."""

    lead_id: UUID
    proposal_type: ProposalType
    executive_summary: Literal["CURRENT_CONTEXT_REVIEW"]
    identified_needs: tuple[NeedSelection, ...] = Field(max_length=10)
    recommended_services: tuple[ServiceSelection, ...] = Field(max_length=5)
    opportunity_evidence: tuple[OpportunityEvidenceSelection, ...] = Field(max_length=10)
    proposed_scope: tuple[ScopeSelection, ...] = Field(max_length=10)
    deliverables: tuple[DeliverableType, ...] = Field(max_length=5)
    assumptions: tuple[AssumptionType, ...] = Field(max_length=5)
    exclusions: tuple[ExclusionType, ...] = Field(max_length=5)
    timeline_guidance: Literal["NO_DETERMINISTIC_TIMELINE"]
    commercial_context: tuple[Ref, ...] = Field(max_length=10)
    missing_information: tuple[Ref, ...] = Field(max_length=20)
    evidence_refs: tuple[Ref, ...] = Field(min_length=1, max_length=100)
    review_notes: tuple[ReviewNoteType, ...] = Field(max_length=8)

    @model_validator(mode="after")
    def references_are_exact_and_unique(self):
        refs: set[str] = set(self.commercial_context) | set(self.missing_information)
        for item in self.identified_needs:
            refs.update(item.evidence_refs)
        for item in self.recommended_services:
            refs.update(item.evidence_refs)
        for item in self.opportunity_evidence:
            refs.add(item.opportunity_id)
            refs.update(item.evidence_refs)
        for item in self.proposed_scope:
            refs.update(item.evidence_refs)
        if len(self.evidence_refs) != len(set(self.evidence_refs)) or set(self.evidence_refs) != refs:
            raise ValueError("evidence_refs must exactly match all selected unique references")
        if len(set(self.deliverables)) != len(self.deliverables):
            raise ValueError("deliverables must be unique")
        if len(set(self.assumptions)) != len(self.assumptions):
            raise ValueError("assumptions must be unique")
        if len(set(self.exclusions)) != len(self.exclusions):
            raise ValueError("exclusions must be unique")
        if len(set(self.review_notes)) != len(self.review_notes):
            raise ValueError("review_notes must be unique")
        return self


class ProposalItem(ProposalContract):
    item_type: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=500)
    evidence_refs: tuple[Ref, ...] = Field(min_length=1, max_length=10)


class RecommendedService(ProposalContract):
    service_id: ServiceId
    evidence_refs: tuple[Ref, ...] = Field(min_length=1, max_length=10)


class OpportunityEvidence(ProposalContract):
    opportunity_id: Ref
    label: str = Field(min_length=1, max_length=250)
    evidence_refs: tuple[Ref, ...] = Field(min_length=1, max_length=10)


class CommercialReference(ProposalContract):
    evidence_ref: Ref
    label: str = Field(min_length=1, max_length=250)
    display_value: str = Field(max_length=1000)


class ProposalBrief(ProposalContract):
    """Persisted proposal contract, composed from validated selections and context."""

    lead_id: UUID
    proposal_type: ProposalType
    executive_summary: str = Field(min_length=1, max_length=500)
    identified_needs: tuple[ProposalItem, ...] = Field(max_length=10)
    recommended_services: tuple[RecommendedService, ...] = Field(max_length=5)
    opportunity_evidence: tuple[OpportunityEvidence, ...] = Field(max_length=10)
    proposed_scope: tuple[ProposalItem, ...] = Field(max_length=10)
    deliverables: tuple[str, ...] = Field(max_length=5)
    assumptions: tuple[str, ...] = Field(max_length=5)
    exclusions: tuple[str, ...] = Field(max_length=5)
    timeline_guidance: str = Field(min_length=1, max_length=300)
    commercial_context: tuple[CommercialReference, ...] = Field(max_length=10)
    missing_information: tuple[ProposalItem, ...] = Field(max_length=20)
    evidence_refs: tuple[Ref, ...] = Field(max_length=100)
    review_notes: tuple[str, ...] = Field(max_length=8)


class ProposalIntelligenceError(ValueError):
    """Generated proposal selections do not satisfy the current evidence contract."""

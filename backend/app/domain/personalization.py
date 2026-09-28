"""Strict, versioned contracts for evidence-grounded AI personalization."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


CONTRACT_VERSION = "personalization-v2"
PROMPT_VERSION = "evidence-grounded-prompt-v2"
SAFE_SUGGESTED_LANGUAGE = frozenset({
    "Would you be open to a brief conversation?",
    "Would you like to discuss the opportunities identified in this audit?",
    "Would you like to explore the recommended services?",
})


class PersonalizationContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PersonalizationClaim(PersonalizationContract):
    type: Literal["verified_fact", "supported_inference", "suggested_language"]
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    evidence_ids: tuple[UUID, ...] = ()
    opportunity_ids: tuple[UUID, ...] = ()
    service_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def citations_match_claim_kind(self):
        if self.type in {"verified_fact", "supported_inference"} and not self.evidence_ids:
            raise ValueError("factual claims and inferences require evidence references")
        if self.type == "supported_inference" and not self.opportunity_ids:
            raise ValueError("supported inferences must reference deterministic opportunities")
        if self.type == "verified_fact" and self.opportunity_ids and not self.evidence_ids:
            raise ValueError("opportunity-linked facts require evidence")
        if self.type == "suggested_language" and self.evidence_ids:
            raise ValueError("suggested language cannot cite evidence as a verified fact")
        if self.type == "suggested_language" and self.text not in SAFE_SUGGESTED_LANGUAGE:
            raise ValueError("suggested language must use an approved non-factual phrase")
        if self.type == "suggested_language" and "opportunities" in self.text.lower() and not self.opportunity_ids:
            raise ValueError("opportunity-focused language must reference current opportunities")
        if self.type == "suggested_language" and self.opportunity_ids and "opportunities" not in self.text.lower():
            raise ValueError("opportunity references require opportunity-focused language")
        if self.type != "suggested_language" and self.service_ids:
            raise ValueError("service references belong only on suggested service language")
        return self


class PersonalizationText(PersonalizationContract):
    text: Annotated[str, Field(min_length=1, max_length=8000)]
    claims: tuple[PersonalizationClaim, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def text_is_fully_claim_annotated(self):
        if self.text != "\n".join(claim.text for claim in self.claims):
            raise ValueError("personalization text must consist exactly of its annotated claims")
        return self


class PersonalizationResponse(PersonalizationContract):
    contract_version: Literal["personalization-v2"] = CONTRACT_VERSION
    prompt_version: Literal["evidence-grounded-prompt-v2"] = PROMPT_VERSION
    recommended_service_ids: tuple[str, ...]
    sales_opening: PersonalizationText
    audit_summary: PersonalizationText
    audit_video_script: PersonalizationText
    recommended_package_language: PersonalizationText
    confidence_score: Annotated[float, Field(ge=0, le=1)] = 0.5

    @model_validator(mode="after")
    def package_services_match_declared_recommendations(self):
        package_claims = self.recommended_package_language.claims
        other_sections = (self.sales_opening, self.audit_summary, self.audit_video_script)
        if any(claim.service_ids for section in other_sections for claim in section.claims):
            raise ValueError("service references are only allowed in package language")
        if any(claim.type != "suggested_language" for claim in package_claims):
            raise ValueError("package language must use approved suggested language")
        linked_services = tuple(service_id for claim in package_claims for service_id in claim.service_ids)
        if linked_services != self.recommended_service_ids:
            raise ValueError("package language service references must exactly match recommendations")
        return self


class EvidenceContext(PersonalizationContract):
    id: UUID
    evidence_type: str
    source_type: str
    provider_name: str
    source_url: str | None = None
    observed_value: Any
    observed_at: str
    confidence: float | None = None


class OpportunityContext(PersonalizationContract):
    id: UUID
    category: str
    severity: str
    title: str
    description: str
    evidence_references: tuple[UUID, ...]


class RecommendationContext(PersonalizationContract):
    service_id: str
    service_name: str
    service_description: str
    rank: int
    rationale: str
    confidence: float
    evidence_references: tuple[UUID, ...]
    opportunity_references: tuple[UUID, ...]


class LeadPersonalizationContext(PersonalizationContract):
    company_name: str
    website: str | None = None
    industry: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None


class ScoreContext(PersonalizationContract):
    score: int | None
    version: str


class PersonalizationRequest(PersonalizationContract):
    contract_version: Literal["personalization-v2"] = CONTRACT_VERSION
    prompt_version: Literal["evidence-grounded-prompt-v2"] = PROMPT_VERSION
    lead: LeadPersonalizationContext
    evidence: tuple[EvidenceContext, ...]
    opportunities: tuple[OpportunityContext, ...]
    recommendations: tuple[RecommendationContext, ...]
    score: ScoreContext | None = None


class PersonalizationValidationError(ValueError):
    """Generated personalization failed grounding or ownership validation."""


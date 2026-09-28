"""Versioned public response contracts for persisted lead intelligence."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IntelligenceIdentity(PublicModel):
    id: UUID
    company_name: str
    website: str | None = None
    industry: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None


class EnrichmentRunSummary(PublicModel):
    id: UUID
    status: str
    pipeline_version: str
    started_at: datetime | None = None
    completed_at: datetime | None = None


class EvidenceOut(PublicModel):
    id: UUID
    evidence_type: str
    source_type: str
    provider_name: str
    source_url: str | None = None
    observed_value: Any
    observed_at: datetime
    fetched_at: datetime
    confidence: float | None = None


class ScoreOut(PublicModel):
    value: int | None
    version: str
    breakdown: dict[str, Any]
    evidence_ids: tuple[UUID, ...]
    calculated_at: datetime


class OpportunityOut(PublicModel):
    id: UUID
    category: str
    severity: str
    status: str
    title: str
    description: str
    evidence_ids: tuple[UUID, ...]
    rule_version: str


class RecommendationOut(PublicModel):
    id: UUID
    service_id: str
    service_name: str
    service_description: str
    service_version: str
    rank: int
    rationale: str
    confidence: float
    opportunity_ids: tuple[UUID, ...]
    evidence_ids: tuple[UUID, ...]
    rule_version: str


class ClaimOut(PublicModel):
    type: Literal["verified_fact", "supported_inference", "suggested_language"]
    text: str
    evidence_ids: tuple[UUID, ...] = ()
    opportunity_ids: tuple[UUID, ...] = ()
    service_ids: tuple[str, ...] = ()


class PersonalizationSectionOut(PublicModel):
    text: str
    claims: tuple[ClaimOut, ...]


class PersonalizationOut(PublicModel):
    id: UUID
    contract_version: str
    prompt_version: str
    created_at: datetime
    confidence_score: float = Field(ge=0, le=1)
    sales_opening: PersonalizationSectionOut
    audit_summary: PersonalizationSectionOut
    audit_video_script: PersonalizationSectionOut
    recommended_package_language: PersonalizationSectionOut


class ProcessingMetadata(PublicModel):
    job_id: UUID | None = None
    status: str | None = None
    checkpoint: str | None = None
    attempt_count: int | None = None
    max_attempts: int | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime | None = None


class IntelligenceResponse(PublicModel):
    api_version: Literal["lead-intelligence-v1"] = "lead-intelligence-v1"
    lead: IntelligenceIdentity
    enrichment_status: str
    run: EnrichmentRunSummary
    score: ScoreOut
    opportunities: tuple[OpportunityOut, ...]
    recommendations: tuple[RecommendationOut, ...]
    evidence: tuple[EvidenceOut, ...]
    personalization: PersonalizationOut | None = None
    personalization_available: bool
    processing: ProcessingMetadata
    generated_at: datetime


class IntelligenceSummary(PublicModel):
    intelligence_status: Literal["available", "unavailable"]
    intelligence_score: int | None = None
    opportunity_count: int = 0
    recommendation_count: int = 0
    personalization_available: bool = False
    latest_enrichment_at: datetime | None = None


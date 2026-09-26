"""Deterministic opportunity, recommendation, and score contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import Field, model_validator

from app.domain.enrichment import ContractModel


class OpportunityCategory(StrEnum):
    WEBSITE_PERFORMANCE = "website_performance"
    SEO = "seo"
    LOCAL_SEO = "local_seo"
    REPUTATION = "reputation"
    WEBSITE_SECURITY = "website_security"
    CONVERSION = "conversion"
    WEBSITE_QUALITY = "website_quality"
    DIGITAL_PRESENCE = "digital_presence"


class OpportunitySeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class OpportunityStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class IntelligenceOrigin(StrEnum):
    DETERMINISTIC = "deterministic"
    AI = "ai"


class OpportunityContract(ContractModel):
    id: UUID | None = None
    lead_id: UUID
    enrichment_run_id: UUID
    category: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,79}$")]
    severity: OpportunitySeverity
    status: OpportunityStatus = OpportunityStatus.OPEN
    origin: IntelligenceOrigin = IntelligenceOrigin.DETERMINISTIC
    title: Annotated[str, Field(min_length=1, max_length=250)]
    description: Annotated[str, Field(min_length=1, max_length=2000)]
    evidence_references: Annotated[tuple[UUID, ...], Field(min_length=1)]
    rule_version: Annotated[str, Field(min_length=1, max_length=100)]
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ServiceDefinition(ContractModel):
    service_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,79}$")]
    name: Annotated[str, Field(min_length=1, max_length=160)]
    description: Annotated[str, Field(min_length=1, max_length=1000)]
    active: bool = True
    version: Annotated[str, Field(min_length=1, max_length=80)]


class ServiceRecommendationContract(ContractModel):
    id: UUID | None = None
    lead_id: UUID
    enrichment_run_id: UUID
    service: ServiceDefinition
    rank: Annotated[int, Field(ge=1)]
    rationale: Annotated[str, Field(min_length=1, max_length=2000)]
    confidence: Annotated[float, Field(ge=0, le=1)]
    opportunity_references: Annotated[tuple[UUID, ...], Field(min_length=1)]
    evidence_references: Annotated[tuple[UUID, ...], Field(min_length=1)]
    rule_version: Annotated[str, Field(min_length=1, max_length=100)]
    ranking_score: Annotated[float, Field(ge=0)]
    created_at: datetime | None = None


class ScoreFactor(ContractModel):
    value: Annotated[float, Field(ge=0, le=100)] | None = None
    available: bool
    explanation: Annotated[str, Field(min_length=1, max_length=1000)]
    evidence_references: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def validate_availability(self) -> ScoreFactor:
        if self.available and not self.evidence_references:
            raise ValueError("available score factors must reference supporting evidence")
        if not self.available and self.value is not None:
            raise ValueError("unavailable score factors cannot have an invented value")
        return self


class VersionedLeadScore(ContractModel):
    id: UUID | None = None
    lead_id: UUID
    enrichment_run_id: UUID
    score: Annotated[int, Field(ge=0, le=100)] | None = None
    score_version: Annotated[str, Field(min_length=1, max_length=100)]
    components: dict[str, ScoreFactor]
    calculated_at: datetime

"""Database- and transport-independent contracts for lead enrichment.

Evidence in this module describes source-backed facts and deterministic
observations. AI-generated interpretations belong in analysis results and
cannot be represented as verified evidence.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


JsonScalar = str | int | float | bool | None


class ContractModel(BaseModel):
    """Strict, immutable Pydantic value object with JSON serialization."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderKind(StrEnum):
    """Broad provider role; implementations remain independently selectable."""

    WEBSITE_ANALYZER = "website_analyzer"
    EXTERNAL_DATA = "external_data"
    AI_MODEL = "ai_model"


class ProviderIdentity(ContractModel):
    """Stable provider identity independent of URL, SDK, or database model."""

    name: Annotated[str, Field(min_length=1, max_length=100)]
    kind: ProviderKind
    implementation: Annotated[str | None, Field(max_length=100)] = None


class EvidenceSourceType(StrEnum):
    """Source categories allowed to establish evidence.

    AI models are deliberately absent: their outputs are inferences, not
    source evidence. The provider ID remains open-ended for future adapters.
    """

    WEBSITE = "website"
    BUSINESS_DIRECTORY = "business_directory"
    PERFORMANCE_AUDIT = "performance_audit"
    PUBLIC_RECORD = "public_record"
    OTHER_VERIFIED_SOURCE = "other_verified_source"


class EvidenceSource(ContractModel):
    """Source attribution for a collected fact or deterministic observation."""

    source_type: EvidenceSourceType
    provider: ProviderIdentity | None = None

    @model_validator(mode="after")
    def reject_ai_provider(self) -> EvidenceSource:
        if self.provider is not None and self.provider.kind == ProviderKind.AI_MODEL:
            raise ValueError("AI model output cannot be attributed as verified evidence")
        return self


class ObservationKind(StrEnum):
    """Distinguishes supplied facts from deterministic derived observations."""

    FACT = "fact"
    DETERMINISTIC = "deterministic_observation"


class NormalizedObservation(ContractModel):
    """Provider-neutral, normalized signal emitted by an enrichment source."""

    key: Annotated[str, Field(min_length=1, max_length=160)]
    value: JsonScalar | list[JsonScalar] | dict[str, JsonScalar]
    kind: ObservationKind
    observed_at: datetime
    confidence: Annotated[float, Field(ge=0, le=1)] | None = None

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value


class EvidenceInput(ContractModel):
    """Unpersisted evidence payload suitable for later database storage."""

    lead_id: UUID
    enrichment_run_id: UUID | None = None
    evidence_type: Annotated[str, Field(min_length=1, max_length=160)]
    source: EvidenceSource
    source_url: Annotated[str | None, Field(max_length=2048)] = None
    external_provider_reference: Annotated[str | None, Field(max_length=512)] = None
    observed_value: JsonScalar | list[JsonScalar] | dict[str, JsonScalar]
    observed_at: datetime
    fetched_at: datetime
    expires_at: datetime | None = None
    confidence: Annotated[float, Field(ge=0, le=1)] | None = None
    content_hash: Annotated[str | None, Field(pattern=r"^[0-9a-fA-F]{64}$")] = None
    metadata: dict[str, JsonScalar] = Field(default_factory=dict)

    @field_validator("observed_at", "fetched_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("evidence timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_freshness(self) -> EvidenceInput:
        if self.expires_at is not None and self.expires_at < self.fetched_at:
            raise ValueError("expires_at cannot precede fetched_at")
        return self


class EvidenceRecord(EvidenceInput):
    """Persisted evidence representation, identified independently of input."""

    id: UUID = Field(default_factory=uuid4)


class ProviderErrorType(StrEnum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION = "authentication"
    QUOTA_EXHAUSTED = "quota_exhausted"
    INVALID_RESPONSE = "invalid_response"
    PERMANENT = "permanent"
    TIMEOUT = "timeout"


class ProviderError(ContractModel):
    """Sanitized, typed provider failure suitable for retry policy decisions."""

    error_type: ProviderErrorType
    code: Annotated[str | None, Field(max_length=120)] = None
    message: Annotated[str, Field(min_length=1, max_length=1000)]
    retry_after_seconds: Annotated[float | None, Field(ge=0)] = None
    metadata: dict[str, JsonScalar] = Field(default_factory=dict)


class ProviderRequestMetadata(ContractModel):
    """Transport-neutral request telemetry; contains no credentials."""

    operation: Annotated[str, Field(min_length=1, max_length=160)]
    request_id: Annotated[str | None, Field(max_length=200)] = None
    attempt: Annotated[int, Field(ge=1)] = 1
    duration_ms: Annotated[int | None, Field(ge=0)] = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    response_status: Annotated[int | None, Field(ge=100, le=599)] = None
    attributes: dict[str, JsonScalar] = Field(default_factory=dict)

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("request timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_timing(self) -> ProviderRequestMetadata:
        if self.started_at and self.completed_at and self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        return self


class ProviderResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class ProviderResult(ContractModel):
    """One source provider's normalized output, evidence, and typed failures."""

    provider: ProviderIdentity
    status: ProviderResultStatus
    observations: tuple[NormalizedObservation, ...] = ()
    evidence: tuple[EvidenceInput, ...] = ()
    metadata: ProviderRequestMetadata
    errors: tuple[ProviderError, ...] = ()

    @model_validator(mode="after")
    def validate_result_state(self) -> ProviderResult:
        if self.provider.kind == ProviderKind.AI_MODEL:
            raise ValueError("AI inference providers do not implement enrichment evidence results")
        has_output = bool(self.observations or self.evidence)
        if self.status == ProviderResultStatus.SUCCEEDED and self.errors:
            raise ValueError("a succeeded result cannot contain errors")
        if self.status == ProviderResultStatus.PARTIAL and (not self.errors or not has_output):
            raise ValueError("a partial result requires output and at least one error")
        if self.status == ProviderResultStatus.FAILED and (not self.errors or has_output):
            raise ValueError("a failed result requires errors and cannot contain output")
        return self


class LeadInput(ContractModel):
    """Minimum lead context supplied to enrichment providers."""

    lead_id: UUID
    enrichment_run_id: UUID | None = None
    company_name: Annotated[str, Field(min_length=1, max_length=250)]
    website: Annotated[str | None, Field(max_length=1000)] = None
    phone: Annotated[str | None, Field(max_length=80)] = None
    address: Annotated[str | None, Field(max_length=500)] = None
    city: Annotated[str | None, Field(max_length=120)] = None
    state: Annotated[str | None, Field(max_length=120)] = None
    country: Annotated[str | None, Field(max_length=120)] = None


class EnrichmentProvider(Protocol):
    """Interface for independently implemented fact-collecting providers."""

    name: str

    async def collect(self, lead: LeadInput) -> ProviderResult:
        """Collect normalized source observations for a lead."""
        ...


class EnrichmentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class EnrichmentFailureSummary(ContractModel):
    """Run-level summary without coupling to persistence or exception types."""

    message: Annotated[str, Field(min_length=1, max_length=1000)]
    errors: tuple[ProviderError, ...] = ()


class EnrichmentRun(ContractModel):
    """Lifecycle contract for a future persisted enrichment execution."""

    id: UUID = Field(default_factory=uuid4)
    lead_id: UUID
    processing_job_id: UUID | None = None
    status: EnrichmentRunStatus
    requested_stages: tuple[Annotated[str, Field(min_length=1, max_length=100)], ...]
    completed_stages: tuple[Annotated[str, Field(min_length=1, max_length=100)], ...] = ()
    started_at: datetime | None = None
    completed_at: datetime | None = None
    pipeline_version: Annotated[str, Field(min_length=1, max_length=100)]
    failure_summary: EnrichmentFailureSummary | None = None

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("run timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_stages_and_timing(self) -> EnrichmentRun:
        if len(set(self.requested_stages)) != len(self.requested_stages):
            raise ValueError("requested_stages cannot contain duplicates")
        if not set(self.completed_stages).issubset(self.requested_stages):
            raise ValueError("completed_stages must be requested stages")
        if self.started_at and self.completed_at and self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        return self

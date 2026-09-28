"""Strict API contracts for outreach generation and human review."""
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.domain.outreach import OutreachContent
from app.models import OutreachStatus, SalesStatus


class OutreachPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: OutreachContent


class OutreachReject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("rejection reason cannot be blank")
        return value


class OutreachArtifactOut(BaseModel):
    id: UUID
    lead_id: UUID
    enrichment_run_id: UUID
    status: OutreachStatus
    is_current: bool
    sales_status: SalesStatus
    sales_context_version: str
    contract_version: str
    prompt_version: str
    content: OutreachContent | None
    validation_result: dict
    generation_error: str | None
    generated_at: datetime
    reviewed_at: datetime | None
    reviewed_by: str | None
    rejection_reason: str | None


class OutreachContextOut(BaseModel):
    sales_status: SalesStatus
    score: int | None
    opportunity_count: int
    recommended_services: tuple[str, ...]
    personalization_available: bool


class OutreachReadOut(BaseModel):
    current_context: OutreachContextOut | None
    artifact: OutreachArtifactOut | None

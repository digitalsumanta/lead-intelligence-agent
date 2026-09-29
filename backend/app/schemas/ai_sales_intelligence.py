"""API contracts for versioned AI sales intelligence artifacts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.ai_sales_intelligence import AISalesIntelligenceResponse


class AISalesIntelligenceAPIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AISalesEvidenceCatalogEntry(AISalesIntelligenceAPIModel):
    id: str = Field(min_length=1, max_length=180)
    source_type: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=250)
    display_value: str = Field(max_length=1000)


class AISalesIntelligenceOut(AISalesIntelligenceAPIModel):
    id: UUID
    lead_id: UUID
    context_version: str
    contract_version: str
    prompt_version: str
    provider: str
    model: str
    status: Literal["validated", "failed"]
    source_fingerprint: str = Field(min_length=64, max_length=64)
    is_stale: bool
    is_current: bool
    generated_at: datetime
    created_at: datetime
    generated: AISalesIntelligenceResponse | None
    evidence_catalog: tuple[AISalesEvidenceCatalogEntry, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    usage_metadata: dict = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


class AISalesIntelligenceHistoryOut(AISalesIntelligenceAPIModel):
    items: tuple[AISalesIntelligenceOut, ...] = Field(max_length=100)

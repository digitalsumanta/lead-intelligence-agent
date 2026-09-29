"""Request and response models for the authenticated AI Sales Copilot API."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.ai_sales_copilot import CopilotIntent, CopilotResponse
from app.schemas.ai_sales_intelligence import AISalesEvidenceCatalogEntry


class CopilotAPIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CopilotAsk(CopilotAPIModel):
    question: str = Field(min_length=1, max_length=500)

    @field_validator("question")
    @classmethod
    def trim_question(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("question cannot be blank")
        return value


class CopilotInteractionOut(CopilotAPIModel):
    id: UUID
    lead_id: UUID
    question: str
    intent: CopilotIntent
    context_version: Literal["ai-sales-context-v1"]
    contract_version: Literal["ai-sales-copilot-v1"]
    prompt_version: Literal["ai-sales-copilot-prompt-v1"]
    provider: str
    model: str
    status: Literal["validated", "failed", "unsupported"]
    source_fingerprint: str = Field(min_length=64, max_length=64)
    is_stale: bool
    response: CopilotResponse | None
    evidence_catalog: tuple[AISalesEvidenceCatalogEntry, ...] = ()
    usage_metadata: dict = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime


class CopilotHistoryOut(CopilotAPIModel):
    items: tuple[CopilotInteractionOut, ...] = Field(max_length=100)

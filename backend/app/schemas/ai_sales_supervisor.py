"""Strict API contracts for AI Sales Supervisor runs."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.ai_sales_supervisor import SupervisorResult


class SupervisorAPIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SupervisorRunRequest(SupervisorAPIModel):
    scope: Literal["PIPELINE"] = "PIPELINE"


class SupervisorRunOut(SupervisorAPIModel):
    id: UUID
    lead_id: UUID | None
    scope: Literal["PIPELINE", "LEAD"]
    status: Literal["COMPLETED", "FAILED"]
    artifact_version: Literal["ai-sales-supervisor-v1"]
    prompt_version: Literal["ai-sales-supervisor-prompt-v1"]
    context_version: Literal["ai-sales-context-v1"]
    context_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    result: SupervisorResult | None
    evidence_refs: tuple[str, ...] = ()
    usage: dict = Field(default_factory=dict)
    provider: str
    model: str
    error_code: str | None
    error_message: str | None
    is_current: bool
    is_stale: bool
    created_at: datetime
    updated_at: datetime


class SupervisorRunHistory(SupervisorAPIModel):
    items: tuple[SupervisorRunOut, ...] = Field(max_length=50)

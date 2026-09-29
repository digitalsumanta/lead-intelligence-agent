"""Authenticated API contracts for AI Sales Action Planner artifacts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.ai_sales_action_planner import ActionPlanOutput
from app.schemas.ai_sales_intelligence import AISalesEvidenceCatalogEntry


class PlannerAPIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ActionPlanOut(PlannerAPIModel):
    id: UUID
    lead_id: UUID
    contract_version: Literal["ai-sales-action-planner-v1"]
    prompt_version: Literal["ai-sales-action-planner-prompt-v1"]
    context_version: Literal["ai-sales-context-v1"]
    provider: str
    model: str
    status: Literal["validated", "failed"]
    source_fingerprint: str = Field(min_length=64, max_length=64)
    is_stale: bool
    is_current: bool
    action_plan: ActionPlanOutput | None
    evidence_catalog: tuple[AISalesEvidenceCatalogEntry, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    usage_metadata: dict = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class ActionPlanHistoryOut(PlannerAPIModel):
    items: tuple[ActionPlanOut, ...] = Field(max_length=100)

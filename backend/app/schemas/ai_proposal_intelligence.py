"""Authenticated API contracts for proposal intelligence artifacts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.ai_proposal_intelligence import ProposalBrief
from app.schemas.ai_sales_intelligence import AISalesEvidenceCatalogEntry


class ProposalAPIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProposalIntelligenceOut(ProposalAPIModel):
    id: UUID
    lead_id: UUID
    contract_version: Literal["ai-proposal-intelligence-v1"]
    prompt_version: Literal["ai-proposal-intelligence-prompt-v1"]
    context_version: Literal["ai-sales-context-v1"]
    provider: str
    model: str
    status: Literal["validated", "failed"]
    source_fingerprint: str = Field(min_length=64, max_length=64)
    is_stale: bool
    is_current: bool
    proposal_brief: ProposalBrief | None
    evidence_catalog: tuple[AISalesEvidenceCatalogEntry, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    usage_metadata: dict = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class ProposalIntelligenceHistoryOut(ProposalAPIModel):
    items: tuple[ProposalIntelligenceOut, ...] = Field(max_length=100)

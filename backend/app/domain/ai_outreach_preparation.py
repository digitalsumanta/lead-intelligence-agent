"""Strict, non-executing outreach preparation contracts."""

from enum import StrEnum
from typing import Literal
from uuid import UUID
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.ai_sales_action_planner import ActionType


PREPARATION_VERSION = "ai-outreach-preparation-v1"
PROMPT_VERSION = "ai-outreach-preparation-prompt-v1"
CONTEXT_VERSION = "ai-sales-context-v1"


class PreparationContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PreparationChannel(StrEnum):
    EMAIL = "EMAIL"
    WHATSAPP = "WHATSAPP"
    CALL_SCRIPT = "CALL_SCRIPT"


class PreparationSourceType(StrEnum):
    AI_SALES_ACTION_PLAN = "AI_SALES_ACTION_PLAN"
    AI_SALES_SUPERVISOR = "AI_SALES_SUPERVISOR"


class PreparationStatus(StrEnum):
    READY_FOR_HUMAN_REVIEW = "READY_FOR_HUMAN_REVIEW"
    REQUIRES_HUMAN_APPROVAL = "REQUIRES_HUMAN_APPROVAL"
    REJECTED = "REJECTED"
    STALE_CONTEXT = "STALE_CONTEXT"
    INVALID_REQUEST = "INVALID_REQUEST"
    POLICY_DENIED = "POLICY_DENIED"


class OutreachDraftSelection(PreparationContract):
    """AI selects only current catalog references and server-approved language tokens."""

    selected_evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=8)
    selected_opportunity_ids: tuple[str, ...] = Field(max_length=5)
    selected_service_ids: tuple[str, ...] = Field(max_length=5)
    approved_opening: Literal["Hello,", "Good morning,", "Good afternoon,"]
    approved_call_to_action: Literal[
        "Would you be open to a brief conversation?",
        "Would you like to discuss this further?",
        "Would a short call to review this be useful?",
    ]
    discovery_questions: tuple[Literal[
        "Which business outcome matters most to you right now?",
        "How are you currently evaluating this area?",
        "What would a useful next step look like?",
        "Who else should be involved in evaluating next steps?",
    ], ...] = Field(max_length=4)

    @model_validator(mode="after")
    def unique_references(self):
        if any(len(set(values)) != len(values) for values in (
            self.selected_evidence_ids, self.selected_opportunity_ids, self.selected_service_ids,
            self.discovery_questions,
        )):
            raise ValueError("selected references and questions must be unique")
        return self


class OutreachPreparationRequest(PreparationContract):
    lead_id: UUID
    source_type: PreparationSourceType
    source_id: UUID
    source_action_id: str = Field(min_length=1, max_length=180)
    action_type: ActionType
    channel: PreparationChannel


class OutreachPreparationOut(PreparationContract):
    id: UUID
    lead_id: UUID
    source_type: PreparationSourceType
    source_id: UUID
    source_action_id: str
    action_type: ActionType
    channel: PreparationChannel
    purpose: str
    subject: str | None
    body: str | None
    call_script: dict | None
    evidence_ids: tuple[str, ...]
    opportunity_ids: tuple[str, ...]
    service_ids: tuple[str, ...]
    context_fingerprint: str
    context_version: str
    preparation_version: str
    prompt_version: str
    provider: str | None
    model: str | None
    status: PreparationStatus
    review_required: bool
    policy_decision: str | None
    policy_evaluation_id: UUID | None
    validation_result: dict
    created_at: datetime

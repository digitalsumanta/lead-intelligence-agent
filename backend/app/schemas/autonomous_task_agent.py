"""Strict API contracts for the internal-only Sales Task Agent."""

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.ai_sales_action_planner import ActionType
from app.schemas.agent_policy import PolicyDecision


class TaskAgentContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaskAgentSourceType(StrEnum):
    AI_SALES_ACTION_PLAN = "AI_SALES_ACTION_PLAN"
    AI_SALES_SUPERVISOR = "AI_SALES_SUPERVISOR"
    SALES_OPPORTUNITY = "SALES_OPPORTUNITY"


class TaskExecutionStatus(StrEnum):
    CREATED = "CREATED"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    DENIED = "DENIED"
    REQUIRES_HUMAN_APPROVAL = "REQUIRES_HUMAN_APPROVAL"
    STALE_CONTEXT = "STALE_CONTEXT"
    INVALID_REQUEST = "INVALID_REQUEST"
    FAILED = "FAILED"


class TaskExecutionRequest(TaskAgentContract):
    lead_id: UUID
    action_type: ActionType
    source_type: TaskAgentSourceType
    source_id: UUID
    source_action_id: str = Field(min_length=1, max_length=180)
    context_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=12)
    requested_by: str | None = Field(default=None, max_length=80)


class TaskExecutionResult(TaskAgentContract):
    execution_id: UUID
    lead_id: UUID
    action_type: ActionType
    execution_status: TaskExecutionStatus
    policy_decision: PolicyDecision | None
    task_id: UUID | None
    created: bool
    existing_task_id: UUID | None
    context_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_ids: tuple[str, ...]
    source_artifact_id: UUID
    source_action_id: str
    policy_evaluation_id: UUID | None
    reason: str = Field(max_length=500)
    executed_at: datetime | None
    created_at: datetime


class TaskExecutionHistory(TaskAgentContract):
    items: tuple[TaskExecutionResult, ...] = Field(max_length=100)


class SupervisorTaskExecutionSummary(TaskAgentContract):
    total_findings: int = Field(ge=0)
    eligible: int = Field(ge=0)
    created: int = Field(ge=0)
    already_exists: int = Field(ge=0)
    denied: int = Field(ge=0)
    requires_human_approval: int = Field(ge=0)
    stale_context: int = Field(ge=0)
    failed: int = Field(ge=0)
    execution_ids: tuple[UUID, ...] = Field(max_length=100)

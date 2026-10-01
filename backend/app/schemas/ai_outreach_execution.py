"""Strict API contracts for human-approved email execution."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


EXECUTION_VERSION = "ai-outreach-execution-v1"
APPROVAL_VERSION = "ai-outreach-approval-v1"


class ExecutionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EmptyActionRequest(ExecutionContract):
    """Explicit action request; client-supplied authority fields are rejected."""


class OutreachApprovalOut(ExecutionContract):
    id: UUID
    preparation_id: UUID
    lead_id: UUID
    status: Literal["APPROVED", "STALE"]
    approved_by_user_id: str
    channel: Literal["EMAIL"]
    recipient: str
    context_fingerprint: str
    preparation_version: str
    content_hash: str
    approval_version: str
    policy_evaluation_id: UUID
    policy_decision: Literal["ALLOW", "REQUIRE_HUMAN_APPROVAL"]
    approved_at: datetime


class OutreachExecutionOut(ExecutionContract):
    id: UUID
    approval_id: UUID
    preparation_id: UUID
    lead_id: UUID
    channel: Literal["EMAIL"]
    recipient: str
    subject: str
    content_hash: str
    context_fingerprint: str
    execution_version: str
    policy_evaluation_id: UUID
    policy_decision: Literal["ALLOW", "DENY", "REQUIRE_HUMAN_APPROVAL"]
    provider: str
    provider_message_id: str | None
    status: Literal["EXECUTING", "SENT", "FAILED", "UNCERTAIN", "STALE", "BLOCKED"]
    failure_code: str | None
    failure_reason: str | None
    idempotency_key: str
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime


class OutreachExecutionHistory(ExecutionContract):
    items: tuple[OutreachExecutionOut, ...]

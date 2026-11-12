"""
Pydantic schemas for Supervisor Approval Dispatches.

Phase 12J - D2 - Gate D-B2:
Typed schemas for outbox serialization, manual reconciliation, and diagnostic reporting.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SupervisorDispatchSchemaBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)


class SupervisorDispatchRecordOut(SupervisorDispatchSchemaBase):
    """Pydantic model representing a durable supervisor approval dispatch outbox row."""
    id: UUID
    approval_id: Optional[UUID] = None
    approval_reference_id: UUID
    lead_id: Optional[UUID] = None
    lead_reference_id: UUID
    action_id: str
    channel: str
    recipient: str
    context_fingerprint: str
    idempotency_key: str
    payload_hash: str
    payload_json: dict[str, Any]
    status: str
    dispatch_worker_id: Optional[str] = None
    lease_generation: int
    provider_name: str
    provider_message_id: Optional[str] = None
    attempt_count: int
    max_attempts: int
    last_attempt_at: Optional[datetime] = None
    next_retry_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    reconciliation_notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class SupervisorDispatchReconcileRequest(SupervisorDispatchSchemaBase):
    """Request model for manual operator reconciliation of OUTCOME_UNKNOWN records."""
    resolution: str = Field(description="'SENT', 'FAILED_CAN_RETRY', or 'FAILED'")
    provider_message_id: Optional[str] = None
    notes: Optional[str] = None

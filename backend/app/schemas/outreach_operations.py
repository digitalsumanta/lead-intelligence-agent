"""Contracts for explicit, authenticated outreach execution."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


OutreachChannel = Literal["EMAIL", "WHATSAPP", "MANUAL"]
ManualMethod = Literal["EMAIL", "WHATSAPP"]
ExecutionStatus = Literal["QUEUED", "SENDING", "SENT", "DELIVERED", "FAILED", "CANCELLED"]


class OutreachSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: OutreachChannel
    idempotency_key: str = Field(min_length=8, max_length=100)
    manual_method: ManualMethod | None = None

    @field_validator("idempotency_key")
    @classmethod
    def nonblank_idempotency_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("idempotency_key cannot be blank")
        return value

    @model_validator(mode="after")
    def manual_method_only_for_manual(self):
        if self.channel != "MANUAL" and self.manual_method is not None:
            raise ValueError("manual_method is only valid for the MANUAL channel")
        return self


class OutreachRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=100)

    @field_validator("idempotency_key")
    @classmethod
    def nonblank_idempotency_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("idempotency_key cannot be blank")
        return value


class ManualSendConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]


class OutreachExecutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    lead_id: UUID
    outreach_artifact_id: UUID
    retry_of_execution_id: UUID | None
    channel: OutreachChannel
    manual_method: ManualMethod | None
    provider: str
    status: ExecutionStatus
    attempt_number: int
    recipient: str | None
    subject: str | None
    content_snapshot: dict
    provider_message_id: str | None
    error_code: str | None
    error_message: str | None
    requested_at: datetime
    sent_at: datetime | None
    delivered_at: datetime | None
    failed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class OutreachProviderEventIn(BaseModel):
    """Normalized trusted-provider event; never accepts raw webhook payloads."""
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=80)
    provider_event_id: str = Field(min_length=1, max_length=512)
    event_type: Literal["DELIVERED", "READ", "REPLIED", "BOUNCED", "FAILED"]
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value

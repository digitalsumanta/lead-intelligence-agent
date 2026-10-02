"""Strict API contracts for append-only email delivery lifecycle facts."""

import json
import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


DeliveryEventType = Literal["SENT", "DELIVERED", "BOUNCED", "FAILED", "COMPLAINED", "UNSUBSCRIBED"]
DeliveryEventSource = Literal["PROVIDER", "INTERNAL"]
_SECRET_KEY_PARTS = (
    "password", "passwd", "secret", "token", "apikey", "authorization",
    "accesstoken", "refreshtoken", "privatekey", "credential",
    "subject", "body", "content", "message", "rawheaders", "headers",
    "raw", "payload", "signature", "webhook", "recipient",
)
_IDENTIFIER_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$"


class EmailDeliveryEventContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class EmailDeliveryEventCreate(EmailDeliveryEventContract):
    execution_id: UUID
    event_type: DeliveryEventType
    provider_message_id: str | None = Field(default=None, min_length=1, max_length=512)
    provider_event_id: str | None = Field(default=None, min_length=1, max_length=512)
    provider_status: str | None = Field(default=None, min_length=1, max_length=80)
    occurred_at: datetime | None = None
    metadata: dict[str, Any] | None = None
    source: DeliveryEventSource

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("occurred_at must include a timezone")
        return value

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value):
        return _validate_metadata(value)


class EmailDeliveryEventOut(EmailDeliveryEventContract):
    id: UUID
    execution_id: UUID
    event_type: DeliveryEventType
    recipient_email: str
    provider: str
    provider_message_id: str | None
    provider_event_id: str | None
    provider_status: str | None
    occurred_at: datetime
    recorded_at: datetime
    source: DeliveryEventSource


class EmailDeliveryEventHistory(EmailDeliveryEventContract):
    items: tuple[EmailDeliveryEventOut, ...]


class NormalizedEmailDeliveryEvent(EmailDeliveryEventContract):
    """Provider-neutral lifecycle observation after provider-specific parsing."""

    source: DeliveryEventSource
    provider: str | None = Field(default=None, min_length=1, max_length=80, pattern=_IDENTIFIER_PATTERN)
    provider_event_id: str | None = Field(default=None, min_length=1, max_length=512)
    provider_message_id: str | None = Field(default=None, min_length=1, max_length=512)
    event_type: DeliveryEventType
    recipient_email: str | None = Field(default=None, min_length=3, max_length=320)
    provider_status: str | None = Field(default=None, min_length=1, max_length=80)
    occurred_at: datetime | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("occurred_at must include a timezone")
        return value

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value):
        return _validate_metadata(value)


class EmailDeliveryEventIngestRequest(EmailDeliveryEventContract):
    """Authenticated API shape; public callers cannot assert INTERNAL provenance."""

    provider: str = Field(min_length=1, max_length=80, pattern=_IDENTIFIER_PATTERN)
    provider_event_id: str | None = Field(default=None, min_length=1, max_length=512)
    provider_message_id: str = Field(min_length=1, max_length=512)
    event_type: str = Field(min_length=1, max_length=80)
    recipient_email: str | None = Field(default=None, min_length=3, max_length=320)
    provider_status: str | None = Field(default=None, min_length=1, max_length=80)
    occurred_at: datetime | None = None
    metadata: dict[str, Any] | None = None

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("occurred_at must include a timezone")
        return value

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value):
        return _validate_metadata(value)


class EmailDeliveryEventIngestOut(EmailDeliveryEventContract):
    status: Literal["CREATED", "DUPLICATE"]
    event: EmailDeliveryEventOut
    execution_id: UUID


class DeliveryReconciliationOut(EmailDeliveryEventContract):
    execution_id: UUID
    provider: str
    provider_message_id: str | None
    current_state: Literal["UNSUBSCRIBED", "COMPLAINED", "BOUNCED", "FAILED", "DELIVERED", "SENT", "UNKNOWN"]
    terminal_state: Literal["UNSUBSCRIBED", "COMPLAINED", "BOUNCED", "FAILED"] | None
    has_terminal_event: bool
    event_count: int
    has_conflict: bool
    first_event_at: datetime | None
    last_event_at: datetime | None
    has_out_of_order_events: bool
    conflict_types: tuple[Literal[
        "MULTIPLE_TERMINAL_STATES", "DELIVERED_AFTER_TERMINAL", "SENT_AFTER_TERMINAL",
        "EVENT_TIMESTAMP_OUT_OF_ORDER", "UNSUPPORTED_EVENT_TYPE",
    ], ...]
    unsupported_event_count: int


def _metadata_depth(value: Any, depth: int = 1) -> int:
    if isinstance(value, dict):
        return max([depth, *(_metadata_depth(child, depth + 1) for child in value.values())])
    if isinstance(value, list):
        return max([depth, *(_metadata_depth(child, depth + 1) for child in value)])
    return depth


def _validate_metadata(value: dict[str, Any] | None):
    if value is None:
        return value
    if _metadata_depth(value) > 4:
        raise ValueError("metadata nesting exceeds the maximum depth")
    if _contains_secret_key(value):
        raise ValueError("metadata contains a prohibited secret-like key")
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise ValueError("metadata must contain JSON-compatible values") from None
    if len(encoded) > 4096:
        raise ValueError("metadata exceeds 4096 bytes")
    return value


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if any(part in normalized for part in _SECRET_KEY_PARTS) or normalized in {"auth", "key"}:
                return True
            if _contains_secret_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_key(child) for child in value)
    return False

class DeliveryAnalyticsSummary(BaseModel):
    scope: Literal["global", "date_range", "provider", "execution"]
    start: datetime | None = None
    end: datetime | None = None
    provider: str | None = None
    execution_id: UUID | None = None
    sent_count: int
    delivered_count: int
    bounced_count: int
    failed_count: int
    complained_count: int
    unsubscribed_count: int
    delivery_rate: float | None
    bounce_rate: float | None
    failure_rate: float | None
    complaint_rate: float | None
    unsubscribe_rate: float | None
    unknown_execution_count: int
    sent_execution_count: int
    delivered_execution_count: int
    bounced_execution_count: int
    failed_execution_count: int
    complained_execution_count: int
    unsubscribed_execution_count: int
    conflicted_execution_count: int
    out_of_order_execution_count: int
    unsupported_event_count: int


class DeliveryAnalyticsProviderSummary(BaseModel):
    provider: str
    sent_count: int
    delivered_count: int
    bounced_count: int
    failed_count: int
    complained_count: int
    unsubscribed_count: int
    delivery_rate: float | None
    bounce_rate: float | None
    failure_rate: float | None
    complaint_rate: float | None
    unsubscribe_rate: float | None


class DeliveryAnalyticsTimeSeriesPoint(BaseModel):
    period_start: datetime
    sent_count: int
    delivered_count: int
    bounced_count: int
    failed_count: int
    complained_count: int
    unsubscribed_count: int


class ExecutionDeliveryAnalytics(BaseModel):
    execution_id: UUID
    provider: str
    provider_message_id: str | None
    recipient: str | None
    current_reconciled_state: Literal["UNSUBSCRIBED", "COMPLAINED", "BOUNCED", "FAILED", "DELIVERED", "SENT", "UNKNOWN"]
    event_count: int
    first_event_at: datetime | None
    last_event_at: datetime | None
    has_conflict: bool
    conflict_types: tuple[str, ...]
    has_out_of_order_events: bool
    unsupported_event_count: int

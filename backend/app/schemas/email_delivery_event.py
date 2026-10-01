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
    "subject", "body", "content", "message", "rawheaders", "recipient",
)


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


class EmailDeliveryEventOut(EmailDeliveryEventContract):
    id: UUID
    execution_id: UUID
    event_type: DeliveryEventType
    recipient_email: str
    provider_message_id: str | None
    provider_event_id: str | None
    provider_status: str | None
    occurred_at: datetime
    recorded_at: datetime
    source: DeliveryEventSource


class EmailDeliveryEventHistory(EmailDeliveryEventContract):
    items: tuple[EmailDeliveryEventOut, ...]


def _metadata_depth(value: Any, depth: int = 1) -> int:
    if isinstance(value, dict):
        return max([depth, *(_metadata_depth(child, depth + 1) for child in value.values())])
    if isinstance(value, list):
        return max([depth, *(_metadata_depth(child, depth + 1) for child in value)])
    return depth


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

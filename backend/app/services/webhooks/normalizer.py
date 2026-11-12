"""
Webhook Event Normalizer and Privacy Masking Utilities.

Phase 12J - D2 - Gate D-C4:
Normalizes heterogeneous provider event formats (Resend vs SendGrid) into standard
delivery telemetry models while enforcing strict PII masking and data minimization.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from app.models.entities import now_utc


def mask_recipient(recipient: Optional[str]) -> Optional[str]:
    """
    Masks recipient email address or phone number to preserve privacy.
    Example: 'alice@example.com' -> 'a***@example.com'
    """
    if not recipient:
        return None
    val = recipient.strip()
    if "@" in val:
        parts = val.split("@", 1)
        user = parts[0]
        domain = parts[1]
        masked_user = (user[0] + "***") if len(user) > 1 else "***"
        return f"{masked_user}@{domain}"
    if len(val) > 4:
        return f"{val[:2]}***{val[-2:]}"
    return "***"


def sanitize_bounce_reason(reason: Optional[str]) -> Optional[str]:
    """
    Strips raw email addresses and authentication tokens from bounce explanations.
    """
    if not reason:
        return None
    sanitized = re.sub(r"[\w\.-]+@[\w\.-]+\.\w+", "[REDACTED_EMAIL]", str(reason))
    sanitized = re.sub(r"Bearer\s+[a-zA-Z0-9_\-\.]+", "Bearer [REDACTED_TOKEN]", sanitized, flags=re.IGNORECASE)
    return sanitized[:500]


def parse_event_timestamp(raw_val: Any) -> datetime:
    """Parses various provider timestamp formats into a UTC timezone-aware datetime."""
    if isinstance(raw_val, (int, float)):
        # Unix timestamp in seconds
        return datetime.fromtimestamp(float(raw_val), tz=timezone.utc)
    if isinstance(raw_val, str):
        # ISO string or string epoch
        val_clean = raw_val.strip()
        try:
            return datetime.fromtimestamp(float(val_clean), tz=timezone.utc)
        except ValueError:
            pass
        try:
            # Replace Z with +00:00 for ISO format
            iso_clean = val_clean.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_clean)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return now_utc()


class NormalizedDeliveryEvent:
    """Standardized delivery event extracted from provider webhook payload."""

    def __init__(
        self,
        *,
        provider_name: str,
        provider_message_id: str,
        provider_event_id: str,
        event_type: str,
        raw_event_type: str,
        event_timestamp: datetime,
        recipient_masked: Optional[str] = None,
        bounce_type: Optional[str] = None,
        bounce_code: Optional[str] = None,
        bounce_reason: Optional[str] = None,
        correlation_dispatch_id: Optional[str] = None,
        metadata_json: Optional[dict[str, Any]] = None,
        is_ignored_event: bool = False,
    ) -> None:
        self.provider_name = provider_name.upper()
        self.provider_message_id = provider_message_id
        self.provider_event_id = provider_event_id
        self.event_type = event_type.upper()
        self.raw_event_type = raw_event_type
        self.event_timestamp = event_timestamp
        self.recipient_masked = recipient_masked
        self.bounce_type = bounce_type.upper() if bounce_type else None
        self.bounce_code = bounce_code
        self.bounce_reason = bounce_reason
        self.correlation_dispatch_id = correlation_dispatch_id
        self.metadata_json = metadata_json or {}
        self.is_ignored_event = is_ignored_event


def normalize_resend_event(payload: Mapping[str, Any], headers: Mapping[str, str]) -> NormalizedDeliveryEvent:
    """
    Normalizes a single Resend/Svix webhook payload.
    Svix headers provide svix-id and svix-timestamp.
    Payload format: { "type": "email.delivered", "created_at": "...", "data": { "email_id": "...", "to": ["..."] } }
    """
    raw_type = str(payload.get("type") or "").strip()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}

    # Event extraction
    provider_message_id = str(data.get("email_id") or payload.get("id") or "").strip()
    svix_id = headers.get("svix-id") or headers.get("Svix-Id") or str(payload.get("id") or "")

    # DC3-02: Defer open and click tracking
    if raw_type in {"email.opened", "email.clicked"}:
        return NormalizedDeliveryEvent(
            provider_name="RESEND",
            provider_message_id=provider_message_id,
            provider_event_id=svix_id,
            event_type="OTHER",
            raw_event_type=raw_type,
            event_timestamp=now_utc(),
            is_ignored_event=True,
        )

    # Status mapping
    bounce_type = None
    bounce_reason = None
    bounce_code = None
    if raw_type == "email.delivered":
        event_type = "DELIVERED"
    elif raw_type == "email.sent":
        event_type = "ACCEPTED"
    elif raw_type == "email.bounced":
        event_type = "BOUNCED"
        bounce_type = "PERMANENT"
        bounce_reason = sanitize_bounce_reason(data.get("bounce_reason") or "Mailbox delivery bounced")
    elif raw_type in {"email.delivery_delayed", "email.rejected"}:
        event_type = "DROPPED"
        bounce_type = "TRANSIENT"
        bounce_reason = sanitize_bounce_reason(data.get("reason"))
    elif raw_type == "email.complained":
        event_type = "COMPLAINT"
        bounce_reason = "Recipient spam / abuse complaint"
    else:
        event_type = "OTHER"

    # Correlation dispatch_id from tags
    correlation_dispatch_id = None
    tags = data.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, dict) and tag.get("name") == "dispatch_id":
                correlation_dispatch_id = str(tag.get("value") or "").strip()

    # Recipient
    recipients = data.get("to")
    raw_rec = recipients[0] if isinstance(recipients, list) and recipients else None

    # Timestamp
    ts = parse_event_timestamp(payload.get("created_at") or headers.get("svix-timestamp"))

    return NormalizedDeliveryEvent(
        provider_name="RESEND",
        provider_message_id=provider_message_id,
        provider_event_id=svix_id,
        event_type=event_type,
        raw_event_type=raw_type,
        event_timestamp=ts,
        recipient_masked=mask_recipient(raw_rec),
        bounce_type=bounce_type,
        bounce_code=bounce_code,
        bounce_reason=bounce_reason,
        correlation_dispatch_id=correlation_dispatch_id,
    )


def normalize_sendgrid_event(item: Mapping[str, Any]) -> NormalizedDeliveryEvent:
    """
    Normalizes a single SendGrid event item from the webhook array.
    Payload format: { "event": "delivered", "sg_message_id": "...", "sg_event_id": "...", "timestamp": 123... }
    """
    raw_type = str(item.get("event") or "").strip()
    sg_event_id = str(item.get("sg_event_id") or "").strip()
    sg_message_id = str(item.get("sg_message_id") or "").strip()

    # DC3-02: Defer open and click tracking
    if raw_type in {"open", "click"}:
        return NormalizedDeliveryEvent(
            provider_name="SENDGRID",
            provider_message_id=sg_message_id,
            provider_event_id=sg_event_id,
            event_type="OTHER",
            raw_event_type=raw_type,
            event_timestamp=now_utc(),
            is_ignored_event=True,
        )

    # Status mapping
    bounce_type = None
    bounce_reason = None
    bounce_code = None
    if raw_type == "delivered":
        event_type = "DELIVERED"
    elif raw_type in {"processed", "deferred"}:
        event_type = "ACCEPTED" if raw_type == "processed" else "DROPPED"
        if raw_type == "deferred":
            bounce_type = "TRANSIENT"
            bounce_reason = sanitize_bounce_reason(item.get("response"))
    elif raw_type == "bounce":
        event_type = "BOUNCED"
        bounce_type = "PERMANENT"
        bounce_code = str(item.get("status") or "")
        bounce_reason = sanitize_bounce_reason(item.get("reason"))
    elif raw_type in {"dropped", "blocked"}:
        event_type = "DROPPED"
        bounce_type = "SUPPRESSION"
        bounce_reason = sanitize_bounce_reason(item.get("reason"))
    elif raw_type == "spamreport":
        event_type = "COMPLAINT"
        bounce_reason = "Recipient spam / abuse report"
    else:
        event_type = "OTHER"

    # Correlation dispatch_id from custom_args
    correlation_dispatch_id = str(item.get("dispatch_id") or "").strip() or None

    # Recipient
    raw_rec = item.get("email")

    # Timestamp
    ts = parse_event_timestamp(item.get("timestamp"))

    return NormalizedDeliveryEvent(
        provider_name="SENDGRID",
        provider_message_id=sg_message_id,
        provider_event_id=sg_event_id,
        event_type=event_type,
        raw_event_type=raw_type,
        event_timestamp=ts,
        recipient_masked=mask_recipient(raw_rec),
        bounce_type=bounce_type,
        bounce_code=bounce_code,
        bounce_reason=bounce_reason,
        correlation_dispatch_id=correlation_dispatch_id,
    )

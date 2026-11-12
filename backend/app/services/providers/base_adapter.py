"""
Base Provider Adapter Abstraction for Supervisor Approval Outbox Dispatch.

Phase 12J - D2 - Gate D-C2:
Defines the provider-neutral adapter protocol, normalized request/result containers,
and base client logic enforcing bounded timeouts, mock transport injection, and error sanitization.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

import httpx

from app.services.dispatch_provider import DispatchProviderResult

logger = logging.getLogger("lead_agent.providers.base")

# Regex to detect and mask API keys / Bearer tokens and recipient emails in error messages
SECRET_MASK_PATTERNS = [
    (re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE), "Bearer [REDACTED]"),
    (re.compile(r"re_[A-Za-z0-9_\-]{8,}", re.IGNORECASE), "re_[REDACTED]"),
    (re.compile(r"SG\.[A-Za-z0-9_\-\.]{12,}", re.IGNORECASE), "SG.[REDACTED]"),
    (re.compile(r"[A-Fa-f0-9]{32,}", re.IGNORECASE), "[REDACTED_HEX_KEY]"),
    (re.compile(r"[\w\.-]+@[\w\.-]+\.\w+"), "[REDACTED_EMAIL]"),
]



def sanitize_error_message(msg: str) -> str:
    """Masks authorization tokens, API keys, and secret values in diagnostic strings."""
    if not msg:
        return ""
    sanitized = str(msg)
    for pattern, replacement in SECRET_MASK_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


@dataclass(frozen=True)
class NormalizedDispatchRequest:
    """
    Authoritative, minimal dispatch payload passed to a provider adapter.
    Derived strictly from verified database records.
    """
    channel: str
    recipient: str
    subject: Optional[str]
    body_text: str
    body_html: Optional[str] = None
    template_name: Optional[str] = None
    template_variables: dict[str, str] = field(default_factory=dict)
    idempotency_key: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseDispatchProviderAdapter(Protocol):
    """Protocol governing all external dispatch provider transport adapters."""

    @property
    def provider_name(self) -> str:
        """Unique provider identifier (e.g., 'RESEND', 'SENDGRID')."""
        ...

    @property
    def supports_native_idempotency(self) -> bool:
        """True if the provider natively honors client-supplied idempotency keys."""
        ...

    async def send_dispatch(
        self,
        *,
        channel: str,
        recipient: str,
        payload_json: dict[str, Any],
        idempotency_key: str,
    ) -> DispatchProviderResult:
        """
        Executes outbound network transmission with strict timeout bounds.
        MUST NOT hold or interact with database sessions or connections.
        """
        ...

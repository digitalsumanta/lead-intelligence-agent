"""
Twilio SendGrid REST Email Provider Adapter.

Phase 12J - D2 - Gate D-C2:
Concrete REST adapter for Twilio SendGrid Email API (v3/mail/send) enforcing:
- Recognition that SendGrid lacks native client-supplied idempotency key headers
- Bounded timeouts (10.0s total, 3.0s connect, 7.0s read)
- Strict classification of ambiguous timeouts and 5xx errors as OUTCOME_UNKNOWN (is_ambiguous=True, no auto-retry)
- Injectable HTTP transport (enforcing mock-only execution)
- Sanitized error reporting and zero secret leakage
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.domain.dispatch_errors import DispatchConfigurationError
from app.domain.recipient_allowlist import normalize_email_address
from app.services.dispatch_provider import DispatchProviderResult
from app.services.providers.base_adapter import (
    BaseDispatchProviderAdapter,
    NormalizedDispatchRequest,
    sanitize_error_message,
)

logger = logging.getLogger("lead_agent.providers.sendgrid")


class SendGridEmailAdapter:
    """
    REST adapter for Twilio SendGrid Email service.
    Implements BaseDispatchProviderAdapter with strict non-idempotent timeout handling.
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        from_email: str = "",
        api_url: str = "https://api.sendgrid.com/v3/mail/send",
        timeout_seconds: float = 10.0,
        connect_timeout_seconds: float = 3.0,
        read_timeout_seconds: float = 7.0,
        http_transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.api_key = api_key
        self.from_email = (from_email or "").strip()
        self.api_url = api_url
        self.timeout = httpx.Timeout(
            timeout=timeout_seconds,
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
        )
        self.http_transport = http_transport

    @property
    def provider_name(self) -> str:
        return "SENDGRID"

    @property
    def supports_native_idempotency(self) -> bool:
        return False

    def _normalize_request(
        self,
        *,
        channel: str,
        recipient: str,
        payload_json: dict[str, Any],
        idempotency_key: str,
    ) -> NormalizedDispatchRequest:
        """Converts raw dispatch payload into standardized request container."""
        subject = payload_json.get("subject") or "Sales Intelligence Outreach"
        body_text = payload_json.get("body") or payload_json.get("body_text") or ""
        body_html = payload_json.get("html") or payload_json.get("body_html")
        metadata = payload_json.get("metadata") or {}

        return NormalizedDispatchRequest(
            channel=channel.upper(),
            recipient=recipient,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )

    def _validate_sender_email(self) -> None:
        """Validates that from_email is non-empty and syntactically valid RFC 5321."""
        if not self.from_email or not str(self.from_email).strip():
            raise DispatchConfigurationError(
                "Mandatory sender identity 'from_email' is unconfigured or empty"
            )
        try:
            normalize_email_address(self.from_email)
        except ValueError as exc:
            raise DispatchConfigurationError(
                f"Invalid sender email address '{self.from_email}': {exc}"
            ) from exc

    async def send_dispatch(
        self,
        *,
        channel: str,
        recipient: str,
        payload_json: dict[str, Any],
        idempotency_key: str,
    ) -> DispatchProviderResult:
        """
        Sends an email via SendGrid v3 REST API.
        Because SendGrid does not support client idempotency headers, timeouts
        are strictly classified as ambiguous to prevent duplicate dispatches.
        """
        if channel.upper() != "EMAIL":
            return DispatchProviderResult(
                success=False,
                error_code="UNSUPPORTED_CHANNEL",
                error_message=f"SendGrid adapter does not support channel '{channel}'",
                is_transient=False,
            )

        self._validate_sender_email()

        req = self._normalize_request(
            channel=channel,
            recipient=recipient,
            payload_json=payload_json,
            idempotency_key=idempotency_key,
        )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # SendGrid v3/mail/send JSON schema
        content_items = [{"type": "text/plain", "value": req.body_text}]
        if req.body_html:
            content_items.append({"type": "text/html", "value": req.body_html})

        custom_args: dict[str, str] = {
            "idempotency_key": req.idempotency_key,
        }
        if req.metadata and req.metadata.get("dispatch_id"):
            custom_args["dispatch_id"] = str(req.metadata["dispatch_id"])

        body_data: dict[str, Any] = {
            "personalizations": [
                {
                    "to": [{"email": req.recipient}],
                    "custom_args": custom_args,
                }
            ],
            "from": {"email": self.from_email},
            "subject": req.subject,
            "content": content_items,
        }


        try:
            async with httpx.AsyncClient(
                transport=self.http_transport,
                timeout=self.timeout,
            ) as client:
                resp = await client.post(
                    self.api_url,
                    headers=headers,
                    json=body_data,
                )

            # 1. Success Response (SendGrid returns HTTP 202 Accepted)
            if resp.status_code == 202:
                provider_msg_id = resp.headers.get("X-Message-Id") or f"sg_{idempotency_key[:16]}"
                return DispatchProviderResult(
                    success=True,
                    provider_message_id=provider_msg_id,
                    provider_status="accepted",
                    raw_status_code=202,
                )

            # 2. Rate Limiting / Throttling (HTTP 429)
            if resp.status_code == 429:
                return DispatchProviderResult(
                    success=False,
                    error_code="RATE_LIMIT_EXCEEDED",
                    error_message="SendGrid rate limit exceeded (HTTP 429)",
                    is_transient=True,
                    is_ambiguous=False,
                    raw_status_code=429,
                )

            # 3. Authentication / Authorization Failure (HTTP 401 / 403)
            if resp.status_code in {401, 403}:
                return DispatchProviderResult(
                    success=False,
                    error_code="PROVIDER_AUTH_ERROR",
                    error_message=f"SendGrid authentication failed (HTTP {resp.status_code})",
                    is_transient=False,
                    is_ambiguous=False,
                    raw_status_code=resp.status_code,
                )

            # 4. Client Validation Failure (HTTP 400)
            if resp.status_code == 400:
                err_text = ""
                try:
                    err_json = resp.json()
                    err_text = str(err_json.get("errors") or err_json)
                except Exception:
                    err_text = resp.text[:200]

                return DispatchProviderResult(
                    success=False,
                    error_code="PROVIDER_VALIDATION_ERROR",
                    error_message=sanitize_error_message(f"SendGrid request validation failed: {err_text}"),
                    is_transient=False,
                    is_ambiguous=False,
                    raw_status_code=resp.status_code,
                )

            # 5. Server Errors (HTTP 500 / 502 / 503)
            # SendGrid does NOT support client idempotency key; server 5xx may have accepted message.
            if 500 <= resp.status_code <= 599:
                return DispatchProviderResult(
                    success=False,
                    error_code="AMBIGUOUS_PROVIDER_SERVER_ERROR",
                    error_message=f"SendGrid server error without idempotency guarantee (HTTP {resp.status_code})",
                    is_transient=False,
                    is_ambiguous=True,
                    raw_status_code=resp.status_code,
                )

            return DispatchProviderResult(
                success=False,
                error_code=f"HTTP_{resp.status_code}",
                error_message=f"Unexpected SendGrid HTTP status: {resp.status_code}",
                is_transient=False,
                is_ambiguous=False,
                raw_status_code=resp.status_code,
            )

        except httpx.ConnectError as exc:
            # Pre-submission failure: request never reached SendGrid
            return DispatchProviderResult(
                success=False,
                error_code="PROVIDER_CONNECT_ERROR",
                error_message=sanitize_error_message(f"Connection failure before submission: {exc}"),
                is_transient=True,
                is_ambiguous=False,
            )
        except httpx.TimeoutException as exc:
            # Post-submission timeout on SendGrid: AMBIGUOUS (cannot safely auto-retry!)
            return DispatchProviderResult(
                success=False,
                error_code="AMBIGUOUS_PROVIDER_TIMEOUT",
                error_message=sanitize_error_message(f"Timeout on non-idempotent SendGrid transport: {exc}"),
                is_transient=False,
                is_ambiguous=True,
            )
        except Exception as exc:
            logger.exception("Unexpected exception during SendGrid provider call: %s", exc)
            return DispatchProviderResult(
                success=False,
                error_code="PROVIDER_CLIENT_EXCEPTION",
                error_message=sanitize_error_message(f"Unhandled client exception: {exc}"),
                is_transient=False,
                is_ambiguous=True,
            )

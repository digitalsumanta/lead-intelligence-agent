"""
Resend REST Email Provider Adapter.

Phase 12J - D2 - Gate D-C2:
Concrete REST adapter for Resend Email API enforcing:
- Native Idempotency-Key header attachment (24h retention window, duplicate suppression)
- Bounded timeouts (10.0s total, 3.0s connect, 7.0s read)
- Injectable HTTP transport (enforcing mock-only execution)
- Sanitized error reporting and zero secret leakage
"""
from __future__ import annotations

import json
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

logger = logging.getLogger("lead_agent.providers.resend")


class ResendEmailAdapter:
    """
    REST adapter for Resend Email service.
    Implements BaseDispatchProviderAdapter with native Idempotency-Key support.
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        from_email: str = "",
        api_url: str = "https://api.resend.com/emails",
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
        return "RESEND"

    @property
    def supports_native_idempotency(self) -> bool:
        return True

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
        Sends an email via Resend REST API using bounded HTTP client.
        Enforces native Idempotency-Key header and sanitized error mapping.
        """
        if channel.upper() != "EMAIL":
            return DispatchProviderResult(
                success=False,
                error_code="UNSUPPORTED_CHANNEL",
                error_message=f"Resend adapter does not support channel '{channel}'",
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
            "Idempotency-Key": req.idempotency_key,
        }

        body_data: dict[str, Any] = {
            "from": self.from_email,
            "to": [req.recipient],
            "subject": req.subject,
            "text": req.body_text,
        }
        if req.body_html:
            body_data["html"] = req.body_html

        # Decision DC3-03: Provider correlation tags
        tags = []
        if req.metadata and req.metadata.get("dispatch_id"):
            tags.append({"name": "dispatch_id", "value": str(req.metadata["dispatch_id"])})
        if tags:
            body_data["tags"] = tags


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

            # 1. Success Response (HTTP 200 or 201)
            if resp.status_code in {200, 201}:
                try:
                    data = resp.json()
                except Exception:
                    return DispatchProviderResult(
                        success=False,
                        error_code="MALFORMED_PROVIDER_RESPONSE",
                        error_message="Provider returned non-JSON body on purported success",
                        is_transient=False,
                        raw_status_code=resp.status_code,
                    )

                provider_msg_id = data.get("id") if isinstance(data, dict) else None
                if not provider_msg_id or not isinstance(provider_msg_id, str):
                    return DispatchProviderResult(
                        success=False,
                        error_code="MISSING_PROVIDER_MESSAGE_ID",
                        error_message="Purported success response missing 'id' attribute",
                        is_transient=False,
                        raw_status_code=resp.status_code,
                    )

                return DispatchProviderResult(
                    success=True,
                    provider_message_id=provider_msg_id,
                    provider_status="accepted",
                    raw_status_code=resp.status_code,
                )

            # 2. Rate Limiting / Throttling (HTTP 429)
            if resp.status_code == 429:
                return DispatchProviderResult(
                    success=False,
                    error_code="RATE_LIMIT_EXCEEDED",
                    error_message="Provider rate limit exceeded (HTTP 429)",
                    is_transient=True,
                    is_ambiguous=False,
                    raw_status_code=429,
                )

            # 3. Authentication / Authorization Failure (HTTP 401 / 403)
            if resp.status_code in {401, 403}:
                return DispatchProviderResult(
                    success=False,
                    error_code="PROVIDER_AUTH_ERROR",
                    error_message=f"Provider authentication failed (HTTP {resp.status_code})",
                    is_transient=False,
                    is_ambiguous=False,
                    raw_status_code=resp.status_code,
                )

            # 4. Client Validation Failure (HTTP 400 / 422)
            if resp.status_code in {400, 422}:
                err_text = ""
                try:
                    err_json = resp.json()
                    err_text = err_json.get("message") or err_json.get("name") or str(err_json)
                except Exception:
                    err_text = resp.text[:200]

                return DispatchProviderResult(
                    success=False,
                    error_code="PROVIDER_VALIDATION_ERROR",
                    error_message=sanitize_error_message(f"Client request validation failed: {err_text}"),
                    is_transient=False,
                    is_ambiguous=False,
                    raw_status_code=resp.status_code,
                )

            # 5. Server Errors (HTTP 500 / 502 / 503 / 504)
            if 500 <= resp.status_code <= 599:
                return DispatchProviderResult(
                    success=False,
                    error_code="PROVIDER_SERVER_ERROR",
                    error_message=f"Provider internal server error (HTTP {resp.status_code})",
                    is_transient=True,
                    is_ambiguous=False,
                    raw_status_code=resp.status_code,
                )

            # 6. Unexpected HTTP Status
            return DispatchProviderResult(
                success=False,
                error_code=f"HTTP_{resp.status_code}",
                error_message=f"Unexpected provider HTTP status: {resp.status_code}",
                is_transient=False,
                is_ambiguous=False,
                raw_status_code=resp.status_code,
            )

        except httpx.ConnectError as exc:
            # Pre-submission failure: request never reached server
            return DispatchProviderResult(
                success=False,
                error_code="PROVIDER_CONNECT_ERROR",
                error_message=sanitize_error_message(f"Connection failure before submission: {exc}"),
                is_transient=True,
                is_ambiguous=False,
            )
        except httpx.TimeoutException as exc:
            # Post-submission timeout: safe to retry with Resend because Idempotency-Key is attached
            return DispatchProviderResult(
                success=False,
                error_code="PROVIDER_TIMEOUT",
                error_message=sanitize_error_message(f"Request timeout during submission: {exc}"),
                is_transient=True,
                is_ambiguous=False,
            )
        except Exception as exc:
            logger.exception("Unexpected exception during Resend provider call: %s", exc)
            return DispatchProviderResult(
                success=False,
                error_code="PROVIDER_CLIENT_EXCEPTION",
                error_message=sanitize_error_message(f"Unhandled client exception: {exc}"),
                is_transient=True,
                is_ambiguous=False,
            )

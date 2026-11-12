"""
Dispatch Provider Abstraction and Inert Test Provider.

Phase 12J - D2 - Gate D-B2:
Provides a secure provider contract isolating network execution from database transactions.
Includes an InertTestDispatchProvider for safe in-memory test execution with zero external socket activity.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from app.domain.dispatch_errors import DispatchDisabledError

logger = logging.getLogger("lead_agent.dispatch_provider")


@dataclass(frozen=True)
class DispatchProviderResult:
    """Outcome representation returned by a dispatch transport provider."""
    success: bool
    provider_message_id: Optional[str] = None
    provider_status: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    is_transient: bool = False
    is_ambiguous: bool = False
    raw_status_code: Optional[int] = None


class DispatchProviderInterface(Protocol):
    """Protocol contract for dispatch transport providers."""
    async def send_dispatch(
        self,
        *,
        channel: str,
        recipient: str,
        payload_json: dict[str, Any],
        idempotency_key: str,
    ) -> DispatchProviderResult: ...


class InertTestDispatchProvider:
    """
    Test-only inert provider that records calls in memory without opening external network sockets.
    Guarantees zero outbound network packets reach SendGrid, Twilio, SMTP, or webhooks.
    """

    def __init__(
        self,
        *,
        default_success: bool = True,
        simulate_error_code: Optional[str] = None,
        simulate_error_message: Optional[str] = None,
        simulate_transient: bool = False,
        simulate_ambiguous: bool = False,
    ) -> None:
        self.default_success = default_success
        self.simulate_error_code = simulate_error_code
        self.simulate_error_message = simulate_error_message
        self.simulate_transient = simulate_transient
        self.simulate_ambiguous = simulate_ambiguous
        self.sent_dispatches: list[dict[str, Any]] = []

    async def send_dispatch(
        self,
        *,
        channel: str,
        recipient: str,
        payload_json: dict[str, Any],
        idempotency_key: str,
    ) -> DispatchProviderResult:
        """Records payload in memory and returns configured test outcome."""
        record = {
            "channel": channel,
            "recipient": recipient,
            "payload_json": payload_json,
            "idempotency_key": idempotency_key,
        }
        self.sent_dispatches.append(record)

        if not self.default_success:
            return DispatchProviderResult(
                success=False,
                error_code=self.simulate_error_code or "PROVIDER_TEST_ERROR",
                error_message=self.simulate_error_message or "Simulated test error",
                is_transient=self.simulate_transient,
                is_ambiguous=self.simulate_ambiguous,
            )

        provider_id = f"inert-{channel.lower()}-{idempotency_key[:12]}"
        return DispatchProviderResult(
            success=True,
            provider_message_id=provider_id,
        )


class BlockedProductionDispatchProvider:
    """
    Fail-closed provider representing production transport where real execution
    is strictly unauthorized. Always raises DispatchDisabledError.
    """

    async def send_dispatch(
        self,
        *,
        channel: str,
        recipient: str,
        payload_json: dict[str, Any],
        idempotency_key: str,
    ) -> DispatchProviderResult:
        raise DispatchDisabledError(
            "Real external communication is strictly unauthorized in Phase 12J - D2 - Gate D-B2."
        )

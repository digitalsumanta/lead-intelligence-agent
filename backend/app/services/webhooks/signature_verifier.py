"""
Base Interfaces and Exceptions for Webhook Signature Verification.

Phase 12J - D2 - Gate D-C4:
Defines the verification contract, timestamp tolerance, and fail-closed error hierarchy.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Mapping

DEFAULT_REPLAY_WINDOW_SECONDS: int = 300  # 5 minutes tolerance window


class WebhookVerificationError(Exception):
    """Base exception for all webhook verification failures."""

    def __init__(self, message: str, code: str = "WEBHOOK_VERIFICATION_FAILED") -> None:
        super().__init__(message)
        self.code = code


class WebhookSignatureMissingError(WebhookVerificationError):
    """Raised when one or more required signature headers are absent."""

    def __init__(self, message: str = "Required webhook signature headers are missing") -> None:
        super().__init__(message, code="SIGNATURE_MISSING")


class WebhookSignatureInvalidError(WebhookVerificationError):
    """Raised when cryptographic signature does not match payload digest."""

    def __init__(self, message: str = "Webhook signature verification failed") -> None:
        super().__init__(message, code="SIGNATURE_INVALID")


class WebhookTimestampExpiredError(WebhookVerificationError):
    """Raised when webhook timestamp falls outside the allowed replay window."""

    def __init__(self, message: str = "Webhook timestamp is outside allowed replay window") -> None:
        super().__init__(message, code="TIMESTAMP_EXPIRED")


class WebhookConfigurationError(WebhookVerificationError):
    """Raised when webhook secrets or public keys are misconfigured or unconfigured."""

    def __init__(self, message: str = "Webhook verification secret is unconfigured") -> None:
        super().__init__(message, code="CONFIGURATION_ERROR")


class BaseWebhookVerifier(ABC):
    """Abstract base class for provider webhook verifiers."""

    @abstractmethod
    def verify(
        self,
        *,
        raw_body: bytes,
        headers: Mapping[str, str],
        current_time_epoch: float | None = None,
    ) -> bool:
        """
        Cryptographically verifies the raw request bytes against headers.
        Raises WebhookVerificationError subclasses on failure.
        Returns True on successful verification.
        """
        raise NotImplementedError

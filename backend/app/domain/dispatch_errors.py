"""
Domain exceptions for Supervisor Approval Dispatch Outbox and Worker Execution.

Phase 12J - D2 - Gate D-B2:
Standardized error codes, failure classifications, and message formatting.
"""
from __future__ import annotations


class DispatchBaseError(Exception):
    """Base exception for all dispatch-related errors."""
    code: str = "DISPATCH_ERROR"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class DispatchDisabledError(DispatchBaseError):
    """Raised when master dispatch kill switch or channel kill switch is disabled."""
    code = "DISPATCH_DISABLED"


class DispatchNotFoundError(DispatchBaseError):
    """Raised when a dispatch outbox record cannot be found."""
    code = "DISPATCH_NOT_FOUND"


class DispatchInvalidTransitionError(DispatchBaseError):
    """Raised when an illegal FSM state transition is attempted."""
    code = "DISPATCH_INVALID_TRANSITION"


class DispatchDuplicateError(DispatchBaseError):
    """Raised when a dispatch with the same idempotency key already exists."""
    code = "DISPATCH_DUPLICATE_IDEMPOTENCY_KEY"


class DispatchAbortedError(DispatchBaseError):
    """Raised when a dispatch was aborted prior to external network socket transmission."""
    code = "DISPATCH_ABORTED"


class DispatchRedactedPayloadError(DispatchBaseError):
    """Raised when a dispatch row contains a redacted payload and cannot be sent."""
    code = "DISPATCH_REDACTED_PAYLOAD"


class DispatchLeaseExpiredError(DispatchBaseError):
    """Raised when an approval execution lease has expired or remaining TTL is under 60 seconds."""
    code = "DISPATCH_LEASE_EXPIRED"


class DispatchFencingConflictError(DispatchBaseError):
    """Raised when a stale worker's fencing token/version is rejected post-network."""
    code = "DISPATCH_FENCING_CONFLICT"


class DispatchAmbiguousOutcomeError(DispatchBaseError):
    """Raised when an ambiguous outcome (e.g. SMTP socket drop) occurs, prohibiting auto-retry."""
    code = "DISPATCH_AMBIGUOUS_OUTCOME"


class DispatchConfigurationError(DispatchBaseError):
    """Raised when dispatch configuration or provider allowlist check fails."""
    code = "DISPATCH_CONFIGURATION_ERROR"


class DispatchRecipientNotAllowlistedError(DispatchAbortedError):
    """Raised when an outbound canary dispatch is attempted to an unapproved recipient."""
    code = "CANARY_RECIPIENT_NOT_ALLOWLISTED"


class CanaryAuthorizationError(DispatchBaseError):
    """Base exception for multi-party canary flight authorization failures."""
    code = "CANARY_AUTHORIZATION_ERROR"


class CanaryDualApprovalRequiredError(CanaryAuthorizationError):
    """Raised when an action requires two distinct approvers and one or both are missing."""
    code = "CANARY_DUAL_APPROVAL_REQUIRED"


class CanarySelfApprovalError(CanaryAuthorizationError):
    """Raised when the same identity attempts to provide both canary flight signatures."""
    code = "CANARY_SELF_APPROVAL_PROHIBITED"


class CanaryFlightExpiredError(CanaryAuthorizationError):
    """Raised when a canary flight authorization window has expired."""
    code = "CANARY_FLIGHT_EXPIRED"


class CanaryFlightRevokedError(CanaryAuthorizationError):
    """Raised when an authorized canary flight has been revoked."""
    code = "CANARY_FLIGHT_REVOKED"


class CanaryScopeDriftError(CanaryAuthorizationError):
    """Raised when a canary flight scope fingerprint does not match signed scope."""
    code = "CANARY_SCOPE_DRIFT"


class CanaryMessageCapExceededError(CanaryAuthorizationError):
    """Raised when the cumulative message cap for a canary flight has been exhausted."""
    code = "CANARY_MESSAGE_CAP_EXHAUSTED"



class DispatchRecipientSuppressedError(DispatchAbortedError):
    """Recipient is on the global email suppression list (unsubscribed, bounced, complained)."""
    code = "RECIPIENT_SUPPRESSED"


class DispatchComplianceConfigError(DispatchAbortedError):
    """Required email compliance configuration (unsubscribe, postal address) is missing."""
    code = "EMAIL_COMPLIANCE_NOT_CONFIGURED"

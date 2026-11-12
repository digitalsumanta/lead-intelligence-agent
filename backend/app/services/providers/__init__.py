"""
Provider Integration Adapters for Outbound Sales Outreach.

Phase 12J - D2 - Gate D-C2:
Exports provider-neutral interfaces, concrete REST email adapters (Resend, SendGrid),
in-memory mock transport, and provider factory.
"""
from __future__ import annotations

from app.services.dispatch_provider import (
    BlockedProductionDispatchProvider,
    DispatchProviderInterface,
    DispatchProviderResult,
    InertTestDispatchProvider,
)
from app.services.providers.base_adapter import (
    BaseDispatchProviderAdapter,
    NormalizedDispatchRequest,
    sanitize_error_message,
)
from app.services.providers.factory import get_dispatch_provider
from app.services.providers.mock_transport import MockProviderTransport
from app.services.providers.resend_adapter import ResendEmailAdapter
from app.services.providers.sendgrid_adapter import SendGridEmailAdapter

__all__ = [
    "BaseDispatchProviderAdapter",
    "BlockedProductionDispatchProvider",
    "DispatchProviderInterface",
    "DispatchProviderResult",
    "InertTestDispatchProvider",
    "MockProviderTransport",
    "NormalizedDispatchRequest",
    "ResendEmailAdapter",
    "SendGridEmailAdapter",
    "get_dispatch_provider",
    "sanitize_error_message",
]

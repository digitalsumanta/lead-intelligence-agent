"""
Provider Factory for Dispatch Transport Adapters.

Phase 12J - D2 - Gate D-C2:
Resolves concrete dispatch provider adapters based on channel and settings.
Guarantees fail-closed isolation: returns BlockedProductionDispatchProvider
whenever global or channel kill switches are disabled.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import settings as default_settings
from app.domain.dispatch_errors import DispatchConfigurationError
from app.services.dispatch_provider import (
    BlockedProductionDispatchProvider,
    DispatchProviderInterface,
)
from app.services.providers.resend_adapter import ResendEmailAdapter
from app.services.providers.sendgrid_adapter import SendGridEmailAdapter

logger = logging.getLogger("lead_agent.providers.factory")

ALLOWED_EMAIL_PROVIDERS = frozenset({"RESEND", "SENDGRID"})


def get_dispatch_provider(
    channel: str,
    *,
    settings: Any = None,
    http_transport: Optional[httpx.AsyncBaseTransport] = None,
) -> DispatchProviderInterface:
    """
    Returns an authorized dispatch provider adapter for the requested channel.
    Fails closed if the master or channel kill switch is disabled.
    """
    app_settings = settings or default_settings
    ch = (channel or "").strip().upper()

    # Fail-closed check: if dispatch is not permitted, return BlockedProductionDispatchProvider
    if not app_settings.is_dispatch_permitted(ch):
        logger.info("Dispatch for channel '%s' is not permitted; using BlockedProductionDispatchProvider", ch)
        return BlockedProductionDispatchProvider()

    # Resolve Email Provider
    if ch == "EMAIL":
        provider_type = getattr(app_settings, "outreach_email_provider", "RESEND").strip().upper()
        if not provider_type:
            provider_type = "RESEND"

        if provider_type not in ALLOWED_EMAIL_PROVIDERS:
            raise DispatchConfigurationError(
                f"Unsupported email provider '{provider_type}'. Allowed providers: {sorted(ALLOWED_EMAIL_PROVIDERS)}"
            )

        timeout = getattr(app_settings, "outreach_dispatch_timeout_seconds", 10.0)
        conn_timeout = getattr(app_settings, "outreach_dispatch_connect_timeout_seconds", 3.0)
        read_timeout = getattr(app_settings, "outreach_dispatch_read_timeout_seconds", 7.0)

        if provider_type == "SENDGRID":
            api_key = getattr(app_settings, "outreach_sendgrid_api_key", "")
            from_email = getattr(app_settings, "outreach_sendgrid_from", "")
            api_url = getattr(app_settings, "outreach_sendgrid_api_url", "https://api.sendgrid.com/v3/mail/send")
            return SendGridEmailAdapter(
                api_key=api_key,
                from_email=from_email,
                api_url=api_url,
                timeout_seconds=timeout,
                connect_timeout_seconds=conn_timeout,
                read_timeout_seconds=read_timeout,
                http_transport=http_transport,
            )

        # Provider: RESEND
        api_key = getattr(app_settings, "outreach_resend_api_key", "")
        from_email = getattr(app_settings, "outreach_resend_from", "")
        api_url = getattr(app_settings, "outreach_resend_api_url", "https://api.resend.com/emails")
        return ResendEmailAdapter(
            api_key=api_key,
            from_email=from_email,
            api_url=api_url,
            timeout_seconds=timeout,
            connect_timeout_seconds=conn_timeout,
            read_timeout_seconds=read_timeout,
            http_transport=http_transport,
        )

    # Any other channel (e.g. WHATSAPP, PHONE) is not authorized in Gate D-C4
    logger.warning("Channel '%s' is not supported in Gate D-C4; returning BlockedProductionDispatchProvider", ch)
    return BlockedProductionDispatchProvider()


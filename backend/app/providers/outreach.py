"""Vendor-neutral channel adapter boundary for explicitly requested outreach sends."""
from dataclasses import dataclass
import re
from typing import Protocol
from urllib.parse import urlparse

import httpx

from app.config import settings


@dataclass(frozen=True)
class OutreachMessage:
    channel: str
    recipient: str
    subject: str | None
    content: str
    provider: str
    idempotency_key: str


@dataclass(frozen=True)
class OutreachSendResult:
    provider_message_id: str | None = None


class OutreachProviderFailure(Exception):
    """Failure with safe metadata; never include response bodies or credentials."""

    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class OutreachProvider(Protocol):
    name: str

    async def send(self, message: OutreachMessage) -> OutreachSendResult: ...


class HttpJsonOutreachProvider:
    """Generic configured HTTP adapter for a trusted email/WhatsApp gateway.

    The endpoint must accept the documented JSON request shape and may return a
    JSON `message_id`. Vendor-specific implementations can satisfy the same
    protocol without changing sales or outreach execution logic.
    """

    name = "http_json"

    def __init__(self, channel: str, api_url: str, api_key: str, *, sender: str | None = None, phone_number_id: str | None = None, client_factory=httpx.AsyncClient):
        self.channel = channel
        self.api_url = api_url
        self.api_key = api_key
        self.sender = sender
        self.phone_number_id = phone_number_id
        self.client_factory = client_factory

    async def send(self, message: OutreachMessage) -> OutreachSendResult:
        payload: dict = {"to": message.recipient, "text": message.content}
        if self.channel == "EMAIL":
            payload.update({"from": self.sender, "subject": message.subject})
        else:
            payload["phone_number_id"] = self.phone_number_id
        try:
            async with self.client_factory(timeout=20.0) as client:
                response = await client.post(
                    self.api_url,
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "Idempotency-Key": message.idempotency_key},
                )
        except httpx.TimeoutException:
            raise OutreachProviderFailure("PROVIDER_TIMEOUT", "The configured outreach provider timed out") from None
        except httpx.HTTPError:
            raise OutreachProviderFailure("PROVIDER_UNAVAILABLE", "The configured outreach provider could not be reached") from None
        if response.status_code < 200 or response.status_code >= 300:
            raise OutreachProviderFailure("PROVIDER_REJECTED", "The configured outreach provider rejected the send request")
        message_id = None
        try:
            value = response.json().get("message_id")
            if isinstance(value, str) and value.strip():
                message_id = value.strip()[:512]
        except (ValueError, AttributeError):
            pass
        return OutreachSendResult(provider_message_id=message_id)


def _validate_provider_url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(parsed.hostname and parsed.scheme == "https" and not parsed.username and not parsed.password)


def _valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", value))


def resolve_outreach_provider(channel: str, config=settings) -> OutreachProvider:
    if channel == "EMAIL":
        enabled, provider, api_url, api_key = config.outreach_email_enabled, config.outreach_email_provider.strip().lower(), config.outreach_email_api_url.strip(), config.outreach_email_api_key.strip()
        sender = config.outreach_email_from.strip()
        phone_number_id = None
    elif channel == "WHATSAPP":
        enabled, provider, api_url, api_key = config.outreach_whatsapp_enabled, config.outreach_whatsapp_provider.strip().lower(), config.outreach_whatsapp_api_url.strip(), config.outreach_whatsapp_api_key.strip()
        sender = None
        phone_number_id = config.outreach_whatsapp_phone_number_id.strip()
    else:
        raise OutreachProviderFailure("INVALID_CHANNEL", "This channel does not use an external provider", 422)
    if not enabled:
        raise OutreachProviderFailure("PROVIDER_DISABLED", f"{channel.title()} sending is disabled", 409)
    if not provider or not api_url or not api_key or (channel == "EMAIL" and not sender) or (channel == "WHATSAPP" and not phone_number_id):
        raise OutreachProviderFailure("PROVIDER_NOT_CONFIGURED", f"No complete {channel.title()} provider configuration is available", 503)
    if channel == "EMAIL" and not _valid_email(sender):
        raise OutreachProviderFailure("PROVIDER_CONFIGURATION_INVALID", "The email sender address is invalid", 503)
    if provider != "http_json":
        raise OutreachProviderFailure("PROVIDER_NOT_CONFIGURED", f"The configured {channel.title()} provider adapter is not installed", 503)
    if not _validate_provider_url(api_url):
        raise OutreachProviderFailure("PROVIDER_CONFIGURATION_INVALID", "The configured outreach provider URL must use HTTPS", 503)
    return HttpJsonOutreachProvider(channel, api_url, api_key, sender=sender, phone_number_id=phone_number_id)

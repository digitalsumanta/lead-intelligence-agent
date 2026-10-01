"""Provider-neutral contract and deterministic adapters for approved email sends."""

from __future__ import annotations

import asyncio
import re
import smtplib
import socket
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Literal, Protocol

from app.config import settings
from app.providers.outreach import (
    OutreachMessage,
    OutreachProviderFailure,
    resolve_outreach_provider,
)


EmailOutcome = Literal["SENT", "FAILED", "UNCERTAIN"]


@dataclass(frozen=True)
class EmailSendRequest:
    recipient: str
    subject: str
    body: str
    idempotency_key: str
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EmailProviderResult:
    outcome: EmailOutcome
    provider: str
    provider_status: str | None = None
    provider_message_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class EmailProvider(Protocol):
    name: str

    async def send(self, request: EmailSendRequest) -> EmailProviderResult: ...


class SMTPEmailProvider:
    """SMTP transport. Ambiguous failures after message submission are UNCERTAIN."""

    name = "smtp"

    def __init__(self, config=settings):
        self.host = config.smtp_host.strip()
        self.port = config.smtp_port
        self.username = config.smtp_username
        self.password = config.smtp_password
        self.sender = config.smtp_from_email.strip()
        self.use_tls = config.smtp_use_tls
        self.use_ssl = config.smtp_use_ssl
        self.timeout = config.smtp_timeout_seconds
        if not self.host or not (1 <= self.port <= 65535):
            raise ValueError("SMTP host and a valid port are required")
        if not self.username or not self.password:
            raise ValueError("SMTP username and password are required")
        if not _valid_email(self.sender):
            raise ValueError("A valid SMTP from email address is required")
        if self.use_tls and self.use_ssl:
            raise ValueError("SMTP_USE_TLS and SMTP_USE_SSL cannot both be enabled")
        if not (0 < self.timeout <= 60):
            raise ValueError("SMTP timeout must be greater than 0 and at most 60 seconds")

    async def send(self, request: EmailSendRequest) -> EmailProviderResult:
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._send_sync, request), timeout=self.timeout + 2)
        except asyncio.TimeoutError:
            return _error(self.name, "UNCERTAIN", "PROVIDER_TIMEOUT", "SMTP send timed out; delivery status is unknown")
        except _SMTPFailure as failure:
            return _error(self.name, failure.outcome, failure.code, failure.message, failure.status)
        except Exception:
            # Unexpected transport errors are deliberately bounded and never expose
            # exception text, which may contain host or credential details.
            return _error(self.name, "UNCERTAIN", "PROVIDER_OUTCOME_UNKNOWN", "SMTP outcome is unknown")

    def _send_sync(self, request: EmailSendRequest) -> EmailProviderResult:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = request.recipient
        message["Subject"] = request.subject
        message.set_content(request.body)
        submitting = False
        client = None
        try:
            if self.use_ssl:
                client = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=ssl.create_default_context())
            else:
                client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            client.ehlo()
            if self.use_tls:
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            client.login(self.username, self.password)
            submitting = True
            refused = client.send_message(message, from_addr=self.sender, to_addrs=[request.recipient])
            if refused:
                return _error(self.name, "FAILED", "PROVIDER_REJECTED", "SMTP server rejected the recipient")
            return EmailProviderResult(outcome="SENT", provider=self.name, provider_status="accepted")
        except smtplib.SMTPResponseException as exc:
            # An explicit SMTP response confirms rejection, including DATA errors.
            return _error(self.name, "FAILED", "PROVIDER_REJECTED", f"SMTP server rejected the message (code {exc.smtp_code})", str(exc.smtp_code))
        except (socket.timeout, TimeoutError, smtplib.SMTPServerDisconnected, OSError):
            if submitting:
                raise _SMTPFailure("UNCERTAIN", "PROVIDER_OUTCOME_UNKNOWN", "SMTP connection ended during submission") from None
            raise _SMTPFailure("FAILED", "PROVIDER_UNAVAILABLE", "SMTP server could not be reached") from None
        except smtplib.SMTPException:
            raise _SMTPFailure("FAILED", "PROVIDER_REJECTED", "SMTP request was rejected") from None
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    try:
                        client.close()
                    except Exception:
                        pass


class _SMTPFailure(Exception):
    def __init__(self, outcome: EmailOutcome, code: str, message: str, status: str | None = None):
        self.outcome, self.code, self.message, self.status = outcome, code, message, status


class HttpJsonEmailProvider:
    """Compatibility adapter over the existing configured HTTPS outreach provider."""

    name = "http_json"

    def __init__(self, adapter):
        self.adapter = adapter

    async def send(self, request: EmailSendRequest) -> EmailProviderResult:
        message = OutreachMessage(
            channel="EMAIL", recipient=request.recipient, subject=request.subject,
            content=request.body, provider=self.name, idempotency_key=request.idempotency_key,
        )
        try:
            result = await self.adapter.send(message)
        except OutreachProviderFailure as error:
            uncertain = error.code in {"PROVIDER_TIMEOUT", "PROVIDER_UNAVAILABLE"}
            return _error(self.name, "UNCERTAIN" if uncertain else "FAILED", error.code, str(error))
        return EmailProviderResult(
            outcome="SENT", provider=self.name, provider_status="accepted",
            provider_message_id=result.provider_message_id if result else None,
        )


def resolve_email_provider(config=settings) -> EmailProvider:
    """Select only the explicit server-configured adapter; fail closed otherwise."""
    if not config.outreach_email_enabled:
        raise ValueError("Email sending is disabled")
    selected = config.email_provider.strip().lower()
    if selected == "smtp":
        return SMTPEmailProvider(config)
    if selected == "http_json":
        try:
            return HttpJsonEmailProvider(resolve_outreach_provider("EMAIL", config))
        except OutreachProviderFailure as error:
            raise ValueError(str(error)) from None
    if not selected:
        raise ValueError("EMAIL_PROVIDER is required")
    raise ValueError("EMAIL_PROVIDER is unsupported")


def _valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", value))


def _error(provider: str, outcome: EmailOutcome, code: str, message: str, status: str | None = None) -> EmailProviderResult:
    return EmailProviderResult(
        outcome=outcome, provider=provider, provider_status=status,
        error_code=code[:80], error_message=message[:500],
    )

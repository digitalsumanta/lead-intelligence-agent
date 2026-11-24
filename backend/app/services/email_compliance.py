"""Outbound email compliance: suppression list, unsubscribe links/headers, and footer.

Every email send path (supervisor outbox, Phase 10E approved execution, Phase 8F outreach
operations) must call into this module so that:

* recipients on the global suppression list are never emailed,
* every message carries a working unsubscribe mechanism (RFC 2369 ``List-Unsubscribe`` and,
  when an HTTPS link is configured, RFC 8058 ``List-Unsubscribe-Post`` one-click), and
* every message ends with the sender's identity and postal address (CAN-SPAM).

When ``OUTREACH_COMPLIANCE_ENFORCED`` is true (the default), sends fail closed if this
configuration is incomplete.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from html import escape
from typing import Optional
from urllib.parse import quote, urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.entities import EmailSuppression, EmailSuppressionReason, now_utc

logger = logging.getLogger("lead_agent.email_compliance")

_EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_SIG_LEN = 32  # base64url chars of the HMAC-SHA256 signature kept in the token
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Headers adapters may forward from a payload; anything else is dropped (no header injection).
ALLOWED_COMPLIANCE_HEADERS = ("List-Unsubscribe", "List-Unsubscribe-Post")


class EmailComplianceError(Exception):
    """Base class for compliance failures. ``code`` is a stable machine-readable value."""

    code = "EMAIL_COMPLIANCE_ERROR"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class ComplianceConfigurationError(EmailComplianceError):
    code = "EMAIL_COMPLIANCE_NOT_CONFIGURED"


class RecipientSuppressedError(EmailComplianceError):
    code = "RECIPIENT_SUPPRESSED"


# ---------------------------------------------------------------------------
# Address normalization / hashing
# ---------------------------------------------------------------------------

def normalize_email(email: str) -> str:
    value = (email or "").strip()
    # Accept "Name <addr@example.com>" forms.
    if "<" in value and value.endswith(">"):
        value = value[value.rindex("<") + 1 : -1].strip()
    value = value.lower()
    if not _EMAIL_RE.match(value):
        raise ValueError("Invalid email address")
    return value


def email_hash(email: str) -> str:
    return hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()


def mask_email(email: str) -> str:
    try:
        local, domain = normalize_email(email).split("@", 1)
    except ValueError:
        return "[invalid]"
    if len(local) <= 2:
        return f"{local[0]}***@{domain}"
    return f"{local[0]}***{local[-1]}@{domain}"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ComplianceConfig:
    secret: str
    base_url: str
    mailto: str
    business_name: str
    postal_address: str


def _valid_base_url(value: str) -> bool:
    parsed = urlparse(value)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.scheme == "https":
        return True
    # Plain HTTP is only acceptable for local development links.
    return parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS


def get_compliance_config(config=settings) -> Optional[ComplianceConfig]:
    """Return the compliance configuration, or ``None`` if enforcement is disabled.

    Raises ``ComplianceConfigurationError`` when enforcement is on and configuration is
    missing or invalid.
    """
    enforced = bool(getattr(config, "outreach_compliance_enforced", True))
    secret = (getattr(config, "outreach_unsubscribe_secret", "") or "").strip()
    base_url = (getattr(config, "outreach_unsubscribe_base_url", "") or "").strip().rstrip("/")
    mailto = (getattr(config, "outreach_unsubscribe_mailto", "") or "").strip()
    business_name = (getattr(config, "outreach_sender_business_name", "") or "").strip()
    postal_address = (getattr(config, "outreach_sender_postal_address", "") or "").strip()

    problems: list[str] = []
    if len(secret) < 32:
        problems.append("OUTREACH_UNSUBSCRIBE_SECRET must be at least 32 characters")
    if not base_url and not mailto:
        problems.append("set OUTREACH_UNSUBSCRIBE_BASE_URL and/or OUTREACH_UNSUBSCRIBE_MAILTO")
    if base_url and not _valid_base_url(base_url):
        problems.append("OUTREACH_UNSUBSCRIBE_BASE_URL must be an https URL with no query or credentials")
    if mailto and not _EMAIL_RE.match(mailto):
        problems.append("OUTREACH_UNSUBSCRIBE_MAILTO must be a valid email address")
    if not postal_address:
        problems.append("OUTREACH_SENDER_POSTAL_ADDRESS is required")

    if problems:
        if not enforced:
            return None
        raise ComplianceConfigurationError("Email compliance configuration incomplete: " + "; ".join(problems))
    return ComplianceConfig(secret, base_url, mailto, business_name, postal_address)


# ---------------------------------------------------------------------------
# Unsubscribe tokens / links / headers / footer
# ---------------------------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _sign(secret: str, value: str) -> str:
    return _b64(hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).digest())[:_TOKEN_SIG_LEN]


def make_unsubscribe_token(email: str, config: ComplianceConfig) -> str:
    """Token = <sha256 of address>.<HMAC signature>. Carries no readable address."""
    digest = email_hash(email)
    return f"{digest}.{_sign(config.secret, digest)}"


def verify_unsubscribe_token(token: str, config: ComplianceConfig) -> Optional[str]:
    """Return the address hash if the token is authentic, else ``None``."""
    digest, sep, signature = (token or "").partition(".")
    if not sep or not _HASH_RE.match(digest) or len(signature) != _TOKEN_SIG_LEN:
        return None
    if not hmac.compare_digest(_sign(config.secret, digest), signature):
        return None
    return digest


def unsubscribe_url(email: str, config: ComplianceConfig) -> Optional[str]:
    if not config.base_url:
        return None
    return f"{config.base_url}/unsubscribe/{make_unsubscribe_token(email, config)}"


def list_unsubscribe_headers(email: str, config: ComplianceConfig) -> dict[str, str]:
    parts: list[str] = []
    url = unsubscribe_url(email, config)
    if url:
        parts.append(f"<{url}>")
    if config.mailto:
        parts.append(f"<mailto:{config.mailto}?subject={quote('unsubscribe')}>")
    headers = {"List-Unsubscribe": ", ".join(parts)}
    if url:
        headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    return headers


def _footer_lines(email: str, config: ComplianceConfig) -> tuple[str, Optional[str]]:
    url = unsubscribe_url(email, config)
    if url:
        optout = f"If you'd prefer not to hear from us again, unsubscribe here: {url}"
    else:
        optout = f"If you'd prefer not to hear from us again, email {config.mailto} with the subject \"unsubscribe\"."
    identity = ", ".join(p for p in (config.business_name, config.postal_address) if p)
    return f"{optout}\n{identity}", url


def apply_compliance_footer(
    body_text: str, body_html: Optional[str], email: str, config: ComplianceConfig
) -> tuple[str, Optional[str]]:
    footer_text, url = _footer_lines(email, config)
    text = f"{(body_text or '').rstrip()}\n\n--\n{footer_text}\n"
    html = None
    if body_html:
        identity = escape(", ".join(p for p in (config.business_name, config.postal_address) if p))
        if url:
            link = f'<a href="{escape(url, quote=True)}">unsubscribe here</a>'
            optout = f"If you'd prefer not to hear from us again, {link}."
        else:
            optout = (
                f"If you'd prefer not to hear from us again, email {escape(config.mailto)} "
                "with the subject &quot;unsubscribe&quot;."
            )
        html = (
            f"{body_html}\n<hr style=\"border:none;border-top:1px solid #ddd;margin:24px 0 12px\">"
            f"<p style=\"font-size:12px;color:#666\">{optout}<br>{identity}</p>"
        )
    return text, html


@dataclass(frozen=True)
class CompliantEmail:
    body_text: str
    body_html: Optional[str]
    headers: dict[str, str]


def prepare_compliant_email(
    recipient: str, body_text: str, body_html: Optional[str] = None, config=settings
) -> CompliantEmail:
    """Apply footer and unsubscribe headers. Raises ComplianceConfigurationError if misconfigured."""
    cfg = get_compliance_config(config)
    if cfg is None:
        return CompliantEmail(body_text, body_html, {})
    text, html = apply_compliance_footer(body_text, body_html, recipient, cfg)
    return CompliantEmail(text, html, list_unsubscribe_headers(recipient, cfg))


def filter_compliance_headers(headers: object) -> dict[str, str]:
    """Keep only allow-listed compliance headers with single-line string values."""
    if not isinstance(headers, dict):
        return {}
    out: dict[str, str] = {}
    for name in ALLOWED_COMPLIANCE_HEADERS:
        value = headers.get(name)
        if isinstance(value, str) and value and "\r" not in value and "\n" not in value:
            out[name] = value
    return out


# ---------------------------------------------------------------------------
# Suppression list
# ---------------------------------------------------------------------------

async def is_suppressed(session: AsyncSession, email: str) -> bool:
    try:
        digest = email_hash(email)
    except ValueError:
        return False
    found = await session.scalar(select(EmailSuppression.id).where(EmailSuppression.email_hash == digest))
    return found is not None


async def ensure_not_suppressed(session: AsyncSession, email: str) -> None:
    if await is_suppressed(session, email):
        raise RecipientSuppressedError(f"Recipient {mask_email(email)} is on the suppression list")


async def add_suppression(
    session: AsyncSession,
    *,
    reason: EmailSuppressionReason | str,
    source: str,
    email: Optional[str] = None,
    digest: Optional[str] = None,
    details: Optional[str] = None,
) -> tuple[EmailSuppression, bool]:
    """Idempotently add an address (or a pre-computed hash) to the suppression list.

    Returns ``(row, created)``. Does not commit.
    """
    if email:
        digest = email_hash(email)
    if not digest or not _HASH_RE.match(digest):
        raise ValueError("An email address or valid address hash is required")
    reason_value = reason.value if isinstance(reason, EmailSuppressionReason) else EmailSuppressionReason(reason).value

    existing = await session.scalar(select(EmailSuppression).where(EmailSuppression.email_hash == digest))
    if existing is not None:
        return existing, False
    row = EmailSuppression(
        email_hash=digest,
        email_masked=mask_email(email) if email else None,
        reason=reason_value,
        source=source[:60],
        details=(details or None) and details[:500],
        created_at=now_utc(),
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(select(EmailSuppression).where(EmailSuppression.email_hash == digest))
        if existing is None:
            raise
        return existing, False
    logger.info("Added %s to email suppression list (reason=%s, source=%s)", row.email_masked or "[hash]", reason_value, source)
    return row, True

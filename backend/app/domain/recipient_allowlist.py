"""
Domain Logic and Cryptographic Normalization for Canary Recipient Allowlist Enforcement.

Phase 12J - D2 - Gate D-D3 (ACM-06 Remediation):
Enforces strict fail-closed recipient domain validation on all canary dispatch paths.
Guarantees:
- Exact domain matching against an authoritative allowed domains set.
- Rejection of suffix attacks (e.g. 'internal-test.leadintel.example.com.attacker.invalid').
- Rejection of unapproved subdomains (e.g. 'sub.internal-test.leadintel.example.com').
- Rejection of lookalike domains, homoglyphs/IDNA, trailing dots, control chars, and malformed syntax.
- Fail-closed behavior on missing or empty allowlist configuration.
- Strict PII masking in all diagnostic logging and rejection messages.
"""
from __future__ import annotations

import logging
import re
from typing import Sequence

logger = logging.getLogger("lead_agent.domain.recipient_allowlist")

# Standard approved canary test domain
DEFAULT_APPROVED_CANARY_DOMAIN = "internal-test.leadintel.example.com"

# RFC 5321 length limits
MAX_EMAIL_LENGTH = 254
MAX_LOCAL_PART_LENGTH = 64
MAX_DOMAIN_LABEL_LENGTH = 63

# Strict regex for ASCII local part (letters, digits, permitted dots/hyphens/underscores/plus)
LOCAL_PART_REGEX = re.compile(r"^[a-zA-Z0-9_\-\+\.]+$")

# Strict regex for ASCII domain labels
DOMAIN_LABEL_REGEX = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?$")


def mask_recipient_pii(email: str | None) -> str:
    """
    Masks recipient email address to prevent PII exposure in logs and metrics.
    Example: 'test.user@internal-test.leadintel.example.com' -> 't***r@internal-test.leadintel.example.com'
    """
    if not email:
        return "[EMPTY_RECIPIENT]"
    val = email.strip()
    if "@" not in val:
        return "***"
    parts = val.split("@", 1)
    user = parts[0]
    domain = parts[1]
    if len(user) <= 1:
        masked_user = "*"
    elif len(user) == 2:
        masked_user = f"{user[0]}*"
    else:
        masked_user = f"{user[0]}***{user[-1]}"
    return f"{masked_user}@{domain}"


def normalize_email_address(raw_email: str | None) -> tuple[str, str, str]:
    """
    Normalizes and validates an email address.
    Returns (canonical_email, local_part, clean_domain).
    Raises ValueError with a machine-readable reason on any syntax or validation defect.
    """
    if not raw_email or not str(raw_email).strip():
        raise ValueError("EMPTY_OR_MISSING_EMAIL")

    val = str(raw_email).strip()

    if len(val) > MAX_EMAIL_LENGTH:
        raise ValueError("EMAIL_EXCEEDS_MAX_LENGTH")

    # Reject control characters, newlines, tabs, and spaces
    if any(c in val for c in ("\r", "\n", "\t", "\0", " ")):
        raise ValueError("EMAIL_CONTAINS_CONTROL_CHARS_OR_SPACES")

    # Must contain exactly one @
    if val.count("@") != 1:
        raise ValueError("EMAIL_MUST_HAVE_EXACTLY_ONE_AT_SYMBOL")

    local_part, domain = val.split("@", 1)

    # 1. Local part validation
    if not local_part or len(local_part) > MAX_LOCAL_PART_LENGTH:
        raise ValueError("INVALID_LOCAL_PART_LENGTH")

    if local_part.startswith(".") or local_part.endswith("."):
        raise ValueError("LOCAL_PART_CANNOT_START_OR_END_WITH_DOT")

    if ".." in local_part:
        raise ValueError("LOCAL_PART_CONTAINS_CONSECUTIVE_DOTS")

    if not LOCAL_PART_REGEX.match(local_part):
        raise ValueError("LOCAL_PART_CONTAINS_ILLEGAL_CHARACTERS")

    # 2. Domain validation
    if not domain:
        raise ValueError("EMPTY_DOMAIN")

    # Trailing dot is rejected per specification
    if domain.endswith("."):
        raise ValueError("DOMAIN_TRAILING_DOT_REJECTED")

    if domain.startswith("."):
        raise ValueError("DOMAIN_LEADING_DOT_REJECTED")

    if ".." in domain:
        raise ValueError("DOMAIN_CONTAINS_CONSECUTIVE_DOTS")

    # Reject non-ASCII or homoglyph / IDNA punycode attempts
    try:
        domain_ascii = domain.encode("ascii").decode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("DOMAIN_NON_ASCII_HOMOGLYPH_REJECTED") from exc

    if "xn--" in domain_ascii.lower():
        raise ValueError("DOMAIN_PUNYCODE_REJECTED")

    clean_domain = domain_ascii.lower()
    labels = clean_domain.split(".")

    if len(labels) < 2:
        raise ValueError("DOMAIN_MUST_CONTAIN_AT_LEAST_TWO_LABELS")

    for label in labels:
        if not label or len(label) > MAX_DOMAIN_LABEL_LENGTH:
            raise ValueError("DOMAIN_LABEL_INVALID_LENGTH")
        if not DOMAIN_LABEL_REGEX.match(label):
            raise ValueError("DOMAIN_LABEL_ILLEGAL_CHARACTERS")

    canonical_email = f"{local_part}@{clean_domain}"
    return canonical_email, local_part, clean_domain


def validate_canary_recipient(
    recipient: str | None,
    allowed_domains: Sequence[str] | None,
) -> tuple[bool, str | None]:
    """
    Authoritative fail-closed validator for canary recipient addresses.
    Performs:
    1. Check that allowed_domains is non-empty (fails closed if empty).
    2. Syntax normalization and validation of the recipient email.
    3. Strict exact domain comparison against allowed_domains.
    Returns:
    - (True, None) if recipient is approved.
    - (False, reason_code) if recipient is rejected.
    """
    # 1. Fail closed on absent or empty allowlist configuration
    if not allowed_domains:
        return False, "RECIPIENT_ALLOWLIST_UNCONFIGURED"

    clean_allowed = set()
    for d in allowed_domains:
        if d and str(d).strip():
            clean_allowed.add(str(d).strip().lower())

    if not clean_allowed:
        return False, "RECIPIENT_ALLOWLIST_EMPTY"

    # 2. Syntax validation
    try:
        _, _, recipient_domain = normalize_email_address(recipient)
    except ValueError as exc:
        return False, f"MALFORMED_RECIPIENT_ADDRESS: {exc}"

    # 3. Exact domain match
    if recipient_domain in clean_allowed:
        return True, None

    # Domain rejected (suffix tricks, lookalikes, subdomains all fail exact match)
    return False, "CANARY_RECIPIENT_NOT_ALLOWLISTED"

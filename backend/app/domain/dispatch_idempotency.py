"""
Canonical Idempotency Key Formulation and Recipient Normalization.

Phase 12J - D2 - Gate D-B2:
Enforces deterministic SHA-256 idempotency calculation with length-prefix serialization
and transport-specific recipient normalization.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any
from uuid import UUID


def normalize_recipient(recipient: str, channel: str | None = None) -> str:
    """
    Normalizes recipient coordinates deterministically.
    - EMAIL: Lowercased, stripped, NFKC unicode normalized.
    - PHONE / WHATSAPP: Formatted to E.164 (removes whitespace/punctuation, retains leading +).
    """
    raw = (recipient or "").strip()
    ch = (channel or "").strip().upper()

    if ch == "EMAIL" or "@" in raw:
        normalized = unicodedata.normalize("NFKC", raw.lower())
        return normalized.strip()

    if ch in {"PHONE", "WHATSAPP"}:
        # Retain leading + if present, strip all non-digit characters
        has_plus = raw.startswith("+")
        digits = re.sub(r"\D", "", raw)
        return f"+{digits}" if has_plus else digits

    return raw


def compute_canonical_idempotency_key(
    *,
    approval_id: UUID | str,
    action_id: str,
    context_fingerprint: str,
    recipient: str,
    channel: str | None = None,
) -> str:
    """
    Computes deterministic SHA-256 idempotency key:
    Formula: SHA256("v1" || approval_id || action_id || context_fingerprint || canonical_recipient)
    Using explicit length-prefix / delimited encoding:
    v1|{len(approval_id)}:{approval_id}|{len(action_id)}:{action_id}|{len(context_fingerprint)}:{context_fingerprint}|{len(canonical_recipient)}:{canonical_recipient}
    
    Dynamic metadata (worker_id, lease_generation, attempt_count) is strictly excluded.
    """
    app_id_str = str(approval_id).strip()
    act_id_str = str(action_id).strip()
    ctx_fp_str = str(context_fingerprint).strip()
    canon_rec = normalize_recipient(recipient, channel=channel)

    canonical_payload = (
        f"v1|"
        f"{len(app_id_str)}:{app_id_str}|"
        f"{len(act_id_str)}:{act_id_str}|"
        f"{len(ctx_fp_str)}:{ctx_fp_str}|"
        f"{len(canon_rec)}:{canon_rec}"
    )

    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def compute_payload_hash(payload_json: dict[str, Any]) -> str:
    """
    Computes deterministic SHA-256 digest of payload_json using sorted keys and compact separators.
    """
    canonical_bytes = json.dumps(payload_json, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()

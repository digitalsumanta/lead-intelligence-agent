"""
Resend (Svix) Webhook Cryptographic Signature Verifier.

Phase 12J - D2 - Gate D-C4:
Implements Svix HMAC-SHA256 signature verification according to Svix standard:
- Required headers: svix-id, svix-timestamp, svix-signature
- Signed payload: f"{svix_id}.{svix_timestamp}.".encode('utf-8') + raw_body
- Secret format: whsec_<base64>
- Constant-time comparison and 300s replay window validation.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Mapping, Optional

from app.services.webhooks.signature_verifier import (
    DEFAULT_REPLAY_WINDOW_SECONDS,
    BaseWebhookVerifier,
    WebhookConfigurationError,
    WebhookSignatureInvalidError,
    WebhookSignatureMissingError,
    WebhookTimestampExpiredError,
)


class ResendWebhookVerifier(BaseWebhookVerifier):
    """Verifies Svix-based webhooks dispatched by Resend."""

    def __init__(
        self,
        secret: Optional[str] = None,
        tolerance_seconds: int = DEFAULT_REPLAY_WINDOW_SECONDS,
    ) -> None:
        self.secret = (secret or "").strip()
        self.tolerance_seconds = tolerance_seconds

    def _get_key_bytes(self) -> bytes:
        if not self.secret:
            raise WebhookConfigurationError("Resend webhook secret is unconfigured")
        secret_str = self.secret
        if secret_str.startswith("whsec_"):
            secret_str = secret_str[len("whsec_") :]
        try:
            return base64.b64decode(secret_str)
        except Exception as exc:
            raise WebhookConfigurationError(f"Invalid base64 encoding in Resend webhook secret: {exc}") from exc

    def verify(
        self,
        *,
        raw_body: bytes,
        headers: Mapping[str, str],
        current_time_epoch: Optional[float] = None,
    ) -> bool:
        """
        Cryptographically verifies incoming Resend/Svix webhook headers.
        """
        key_bytes = self._get_key_bytes()

        # Normalize header keys to lowercase
        norm_headers = {k.lower(): v for k, v in headers.items()}
        msg_id = norm_headers.get("svix-id")
        msg_timestamp = norm_headers.get("svix-timestamp")
        msg_signatures = norm_headers.get("svix-signature")

        if not msg_id or not msg_timestamp or not msg_signatures:
            raise WebhookSignatureMissingError(
                "Missing required Svix headers: svix-id, svix-timestamp, or svix-signature"
            )

        # 1. Replay window check
        try:
            ts_epoch = float(msg_timestamp)
        except (ValueError, TypeError) as exc:
            raise WebhookTimestampExpiredError("Invalid non-numeric svix-timestamp") from exc

        now = current_time_epoch if current_time_epoch is not None else time.time()
        if abs(now - ts_epoch) > self.tolerance_seconds:
            raise WebhookTimestampExpiredError(
                f"Webhook timestamp drift ({abs(now - ts_epoch):.1f}s) exceeds tolerance ({self.tolerance_seconds}s)"
            )

        # 2. Canonical payload: f"{msg_id}.{msg_timestamp}." + raw_body
        canonical_prefix = f"{msg_id}.{msg_timestamp}.".encode("utf-8")
        canonical_payload = canonical_prefix + raw_body

        # 3. Compute expected signature
        mac = hmac.new(key_bytes, canonical_payload, hashlib.sha256)
        expected_sig = base64.b64encode(mac.digest()).decode("utf-8")

        # 4. Check against provided signatures (format: "v1,<sig1> v1,<sig2>")
        signatures = msg_signatures.strip().split()
        for versioned_sig in signatures:
            parts = versioned_sig.split(",", 1)
            if len(parts) == 2 and parts[0] == "v1":
                candidate_sig = parts[1].strip()
                if hmac.compare_digest(expected_sig, candidate_sig):
                    return True

        raise WebhookSignatureInvalidError("Svix signature mismatch for Resend webhook payload")

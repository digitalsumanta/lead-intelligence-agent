"""
SendGrid Webhook Cryptographic Signature Verifier (ECDSA).

Phase 12J - D2 - Gate D-C4:
Implements SendGrid Event Webhook signature verification:
- Required headers: X-Twilio-Email-Event-Webhook-Signature, X-Twilio-Email-Event-Webhook-Timestamp
- Signed payload: f"{timestamp}".encode('utf-8') + raw_body
- Signature algorithm: ECDSA using SHA-256 (NIST P-256 curve)
- Verification public key: PEM or base64-encoded SubjectPublicKeyInfo (DER)
- Strict 300s replay window validation.
"""
from __future__ import annotations

import base64
import time
from typing import Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.services.webhooks.signature_verifier import (
    DEFAULT_REPLAY_WINDOW_SECONDS,
    BaseWebhookVerifier,
    WebhookConfigurationError,
    WebhookSignatureInvalidError,
    WebhookSignatureMissingError,
    WebhookTimestampExpiredError,
)


class SendGridWebhookVerifier(BaseWebhookVerifier):
    """Verifies ECDSA signatures dispatched by SendGrid Event Webhook."""

    def __init__(
        self,
        public_key: Optional[str] = None,
        tolerance_seconds: int = DEFAULT_REPLAY_WINDOW_SECONDS,
    ) -> None:
        self.public_key_str = (public_key or "").strip()
        self.tolerance_seconds = tolerance_seconds
        self._parsed_key: Optional[ec.EllipticCurvePublicKey] = None

    def _get_public_key(self) -> ec.EllipticCurvePublicKey:
        if self._parsed_key is not None:
            return self._parsed_key

        if not self.public_key_str:
            raise WebhookConfigurationError("SendGrid webhook verification public key is unconfigured")

        raw_str = self.public_key_str
        try:
            if "BEGIN PUBLIC KEY" in raw_str:
                key = serialization.load_pem_public_key(raw_str.encode("utf-8"))
            else:
                # SendGrid dashboard provides raw base64 DER SubjectPublicKeyInfo
                der_bytes = base64.b64decode(raw_str)
                key = serialization.load_der_public_key(der_bytes)

            if not isinstance(key, ec.EllipticCurvePublicKey):
                raise WebhookConfigurationError("SendGrid public key must be an EllipticCurvePublicKey")

            self._parsed_key = key
            return self._parsed_key
        except Exception as exc:
            raise WebhookConfigurationError(f"Failed to load SendGrid public key: {exc}") from exc

    def verify(
        self,
        *,
        raw_body: bytes,
        headers: Mapping[str, str],
        current_time_epoch: Optional[float] = None,
    ) -> bool:
        """
        Cryptographically verifies incoming SendGrid webhook headers.
        """
        public_key = self._get_public_key()

        norm_headers = {k.lower(): v for k, v in headers.items()}
        signature_b64 = norm_headers.get("x-twilio-email-event-webhook-signature")
        timestamp_str = norm_headers.get("x-twilio-email-event-webhook-timestamp")

        if not signature_b64 or not timestamp_str:
            raise WebhookSignatureMissingError(
                "Missing required SendGrid headers: X-Twilio-Email-Event-Webhook-Signature or X-Twilio-Email-Event-Webhook-Timestamp"
            )

        # 1. Replay window check
        try:
            ts_epoch = float(timestamp_str)
        except (ValueError, TypeError) as exc:
            raise WebhookTimestampExpiredError("Invalid non-numeric SendGrid timestamp") from exc

        now = current_time_epoch if current_time_epoch is not None else time.time()
        if abs(now - ts_epoch) > self.tolerance_seconds:
            raise WebhookTimestampExpiredError(
                f"SendGrid webhook timestamp drift ({abs(now - ts_epoch):.1f}s) exceeds tolerance ({self.tolerance_seconds}s)"
            )

        # 2. Canonical payload: timestamp + raw_body
        canonical_payload = timestamp_str.encode("utf-8") + raw_body

        # 3. Decode signature
        try:
            signature_bytes = base64.b64decode(signature_b64)
        except Exception as exc:
            raise WebhookSignatureInvalidError("Invalid base64 encoding in SendGrid signature") from exc

        # 4. ECDSA verification
        try:
            public_key.verify(
                signature_bytes,
                canonical_payload,
                ec.ECDSA(hashes.SHA256()),
            )
            return True
        except InvalidSignature as exc:
            raise WebhookSignatureInvalidError("ECDSA signature mismatch for SendGrid webhook payload") from exc

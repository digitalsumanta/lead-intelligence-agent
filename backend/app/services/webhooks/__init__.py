"""
Webhook Verification and Telemetry Ingestion Package.

Phase 12J - D2 - Gate D-C4:
Provides cryptographic signature verification for Resend (Svix HMAC-SHA256)
and SendGrid (ECDSA secp256r1 SHA-256), timestamp replay window enforcement,
payload normalization, and durable ingestion.
"""

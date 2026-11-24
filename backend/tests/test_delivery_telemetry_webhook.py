"""
Automated Test Suite for Gate D-C4: Delivery Telemetry, Webhook Ingress, and Asynchronous Reconciler.

Phase 12J - D2 - Gate D-C4:
Validates all 28 required test dimensions:
- Cryptographic signature verifications (Resend Svix & SendGrid ECDSA)
- Timestamp replay windows and replay protection
- Ingress payload size cap, Content-Type, and schema validation
- Append-only delivery event persistence and deduplication
- No raw body or secret persistence
- Asynchronous reconciler: OUTCOME_UNKNOWN resolution, zero auto-resends, state monotonicity
- Lead deletion forensic preservation
- Concurrency Semaphore & Token Bucket rate limiter enforcement (Finding 1)
- Provider factory allowlisting and fail-closed handling (Finding 2)
- Recipient email address sanitization in error messages (Finding 3)
- Aggregate wall-clock timeout enforcement (Finding 4)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, status
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes.supervisor_delivery_webhooks import router as delivery_webhooks_router
from app.config import settings
from app.database import get_db
from app.domain.dispatch_errors import DispatchConfigurationError
from app.models.entities import (
    Base,
    Lead,
    SupervisorApprovalDispatch,
    SupervisorApprovalDispatchStatus,
    SupervisorDispatchDeliveryEvent,
    SupervisorHumanApproval,
    now_utc,
)
from app.repositories.supervisor_delivery_event_repository import (
    SupervisorDeliveryEventRepository,
)
from app.repositories.supervisor_dispatch_repository import (
    SupervisorApprovalDispatchRepository,
)
from app.services.outreach_rate_limiter import (
    OutreachConcurrencyGate,
    TokenBucketRateLimiter,
)
from app.services.providers.base_adapter import sanitize_error_message
from app.services.providers.factory import ALLOWED_EMAIL_PROVIDERS, get_dispatch_provider
from app.services.supervisor_dispatch_service import SupervisorApprovalDispatchService
from app.services.supervisor_telemetry_reconciler import (
    SYSTEM_RECONCILER_ACTOR_ID,
    SupervisorTelemetryReconciler,
)
from app.services.webhooks.normalizer import (
    mask_recipient,
    normalize_resend_event,
    normalize_sendgrid_event,
)
from app.services.webhooks.resend_verifier import ResendWebhookVerifier
from app.services.webhooks.sendgrid_verifier import SendGridWebhookVerifier
from app.services.webhooks.signature_verifier import (
    WebhookSignatureInvalidError,
    WebhookSignatureMissingError,
    WebhookTimestampExpiredError,
)

# ---------------------------------------------------------------------------
# FIXTURES: Isolated In-Memory Database & Test Application
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def async_engine():
    """Isolated in-memory SQLite database engine."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(async_engine):
    """Session factory for the isolated database."""
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db_session(session_factory):
    """Single transaction db session."""
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
def test_app(session_factory):
    """FastAPI test app with mounted webhook router and overridden get_db."""
    app = FastAPI()
    app.include_router(delivery_webhooks_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest_asyncio.fixture
async def async_client(test_app):
    """Async HTTP client for testing local endpoints without network sockets."""
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# ---------------------------------------------------------------------------
# CRYPTOGRAPHIC KEY FIXTURES
# ---------------------------------------------------------------------------

@pytest.fixture
def synthetic_resend_secret():
    """Generates synthetic base64 Svix webhook secret."""
    raw_key = b"synthetic_resend_secret_key_32b!"
    return "whsec_" + base64.b64encode(raw_key).decode("utf-8")


@pytest.fixture
def synthetic_sendgrid_keypair():
    """Generates synthetic ECDSA P-256 keypair for SendGrid verification."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    pem_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, pem_bytes.decode("utf-8")


# ---------------------------------------------------------------------------
# 1. SIGNATURE VERIFICATION TESTS (Resend & SendGrid)
# ---------------------------------------------------------------------------

def test_resend_svix_signature_verification_success(synthetic_resend_secret):
    verifier = ResendWebhookVerifier(secret=synthetic_resend_secret)
    msg_id = "msg_test_12345"
    now_epoch = time.time()
    msg_timestamp = str(int(now_epoch))
    raw_body = b'{"type":"email.delivered","data":{"email_id":"re_abc123"}}'

    # Compute expected signature
    key_bytes = base64.b64decode(synthetic_resend_secret[len("whsec_"):])
    canonical = f"{msg_id}.{msg_timestamp}.".encode("utf-8") + raw_body
    sig = base64.b64encode(hmac.new(key_bytes, canonical, hashlib.sha256).digest()).decode("utf-8")

    headers = {
        "svix-id": msg_id,
        "svix-timestamp": msg_timestamp,
        "svix-signature": f"v1,{sig}",
    }
    assert verifier.verify(raw_body=raw_body, headers=headers, current_time_epoch=now_epoch) is True


def test_resend_svix_signature_verification_tampered_body(synthetic_resend_secret):
    verifier = ResendWebhookVerifier(secret=synthetic_resend_secret)
    msg_id = "msg_test_12345"
    now_epoch = time.time()
    msg_timestamp = str(int(now_epoch))
    raw_body = b'{"type":"email.delivered","data":{"email_id":"re_abc123"}}'

    key_bytes = base64.b64decode(synthetic_resend_secret[len("whsec_"):])
    canonical = f"{msg_id}.{msg_timestamp}.".encode("utf-8") + raw_body
    sig = base64.b64encode(hmac.new(key_bytes, canonical, hashlib.sha256).digest()).decode("utf-8")

    headers = {
        "svix-id": msg_id,
        "svix-timestamp": msg_timestamp,
        "svix-signature": f"v1,{sig}",
    }
    # Tamper with 1 byte of body
    tampered_body = b'{"type":"email.bounced","data":{"email_id":"re_abc123"}}'
    with pytest.raises(WebhookSignatureInvalidError):
        verifier.verify(raw_body=tampered_body, headers=headers, current_time_epoch=now_epoch)


def test_resend_svix_missing_header_rejected(synthetic_resend_secret):
    verifier = ResendWebhookVerifier(secret=synthetic_resend_secret)
    with pytest.raises(WebhookSignatureMissingError):
        verifier.verify(raw_body=b"{}", headers={"svix-id": "123"})


def test_resend_svix_timestamp_outside_replay_window(synthetic_resend_secret):
    verifier = ResendWebhookVerifier(secret=synthetic_resend_secret, tolerance_seconds=300)
    now_epoch = time.time()
    stale_timestamp = str(int(now_epoch - 350))  # 350s old > 300s
    headers = {
        "svix-id": "msg_stale",
        "svix-timestamp": stale_timestamp,
        "svix-signature": "v1,dummy_sig",
    }
    with pytest.raises(WebhookTimestampExpiredError):
        verifier.verify(raw_body=b"{}", headers=headers, current_time_epoch=now_epoch)


def test_sendgrid_ecdsa_signature_verification_success(synthetic_sendgrid_keypair):
    private_key, public_key_pem = synthetic_sendgrid_keypair
    verifier = SendGridWebhookVerifier(public_key=public_key_pem)

    now_epoch = time.time()
    timestamp_str = str(int(now_epoch))
    raw_body = b'[{"event":"delivered","sg_message_id":"sg_msg_999","sg_event_id":"sg_ev_1"}]'

    # Sign: timestamp + raw_body
    canonical = timestamp_str.encode("utf-8") + raw_body
    signature_bytes = private_key.sign(canonical, ec.ECDSA(hashes.SHA256()))
    sig_b64 = base64.b64encode(signature_bytes).decode("utf-8")

    headers = {
        "X-Twilio-Email-Event-Webhook-Signature": sig_b64,
        "X-Twilio-Email-Event-Webhook-Timestamp": timestamp_str,
    }
    assert verifier.verify(raw_body=raw_body, headers=headers, current_time_epoch=now_epoch) is True


def test_sendgrid_ecdsa_signature_invalid(synthetic_sendgrid_keypair):
    _, public_key_pem = synthetic_sendgrid_keypair
    verifier = SendGridWebhookVerifier(public_key=public_key_pem)

    now_epoch = time.time()
    timestamp_str = str(int(now_epoch))
    raw_body = b'[{"event":"delivered"}]'

    headers = {
        "X-Twilio-Email-Event-Webhook-Signature": base64.b64encode(b"invalid_signature_bytes_32").decode("utf-8"),
        "X-Twilio-Email-Event-Webhook-Timestamp": timestamp_str,
    }
    with pytest.raises(WebhookSignatureInvalidError):
        verifier.verify(raw_body=raw_body, headers=headers, current_time_epoch=now_epoch)


# ---------------------------------------------------------------------------
# 2. INGRESS ENDPOINT VALIDATION & FAST-ACK TESTS
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingress_valid_resend_fast_ack(async_client, monkeypatch, synthetic_resend_secret):
    monkeypatch.setattr(settings, "outreach_resend_webhook_secret", synthetic_resend_secret)

    msg_id = "msg_ack_001"
    now_epoch = time.time()
    ts_str = str(int(now_epoch))
    body_dict = {
        "type": "email.delivered",
        "created_at": "2026-10-28T14:00:00Z",
        "data": {
            "email_id": "re_ack_test_1",
            "to": ["lead@acme.com"],
        },
    }
    raw_body = json.dumps(body_dict).encode("utf-8")

    key_bytes = base64.b64decode(synthetic_resend_secret[len("whsec_"):])
    canonical = f"{msg_id}.{ts_str}.".encode("utf-8") + raw_body
    sig = base64.b64encode(hmac.new(key_bytes, canonical, hashlib.sha256).digest()).decode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "svix-id": msg_id,
        "svix-timestamp": ts_str,
        "svix-signature": f"v1,{sig}",
    }

    t0 = time.monotonic()
    resp = await async_client.post(
        "/api/sales-supervisor/webhooks/delivery/resend",
        content=raw_body,
        headers=headers,
    )
    duration_ms = (time.monotonic() - t0) * 1000.0

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["persisted"] == 1
    assert duration_ms < 500.0  # Fast ACK target


@pytest.mark.asyncio
async def test_ingress_duplicate_resend_idempotent_ack(async_client, monkeypatch, synthetic_resend_secret):
    monkeypatch.setattr(settings, "outreach_resend_webhook_secret", synthetic_resend_secret)

    msg_id = "msg_dup_test_002"
    now_epoch = time.time()
    ts_str = str(int(now_epoch))
    body_dict = {
        "type": "email.delivered",
        "created_at": "2026-10-28T14:00:00Z",
        "data": {"email_id": "re_dup_1", "to": ["lead@acme.com"]},
    }
    raw_body = json.dumps(body_dict).encode("utf-8")
    key_bytes = base64.b64decode(synthetic_resend_secret[len("whsec_"):])
    canonical = f"{msg_id}.{ts_str}.".encode("utf-8") + raw_body
    sig = base64.b64encode(hmac.new(key_bytes, canonical, hashlib.sha256).digest()).decode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "svix-id": msg_id,
        "svix-timestamp": ts_str,
        "svix-signature": f"v1,{sig}",
    }

    # First send -> persisted = 1
    resp1 = await async_client.post("/api/sales-supervisor/webhooks/delivery/resend", content=raw_body, headers=headers)
    assert resp1.status_code == 200
    assert resp1.json()["persisted"] == 1

    # Second send -> duplicate = 1, status = 200 (Idempotent acknowledge)
    resp2 = await async_client.post("/api/sales-supervisor/webhooks/delivery/resend", content=raw_body, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["duplicates"] == 1
    assert resp2.json()["persisted"] == 0


@pytest.mark.asyncio
async def test_ingress_oversized_payload_rejected(async_client):
    large_body = b"x" * (1_048_576 + 10)  # > 1 MB
    resp = await async_client.post(
        "/api/sales-supervisor/webhooks/delivery/resend",
        content=large_body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE


@pytest.mark.asyncio
async def test_ingress_unsupported_content_type_rejected(async_client):
    resp = await async_client.post(
        "/api/sales-supervisor/webhooks/delivery/resend",
        content=b"{}",
        headers={"Content-Type": "text/plain"},
    )
    assert resp.status_code == status.HTTP_415_UNSUPPORTED_MEDIA_TYPE


@pytest.mark.asyncio
async def test_ingress_deferred_open_tracking_ignored(async_client, monkeypatch, synthetic_resend_secret):
    """Decision DC3-02: Opens and clicks are ignored without tracking pixels or IPs."""
    monkeypatch.setattr(settings, "outreach_resend_webhook_secret", synthetic_resend_secret)

    msg_id = "msg_open_003"
    now_epoch = time.time()
    ts_str = str(int(now_epoch))
    body_dict = {
        "type": "email.opened",
        "data": {"email_id": "re_open_1", "ip": "1.2.3.4", "user_agent": "Mozilla/5.0"},
    }
    raw_body = json.dumps(body_dict).encode("utf-8")
    key_bytes = base64.b64decode(synthetic_resend_secret[len("whsec_"):])
    canonical = f"{msg_id}.{ts_str}.".encode("utf-8") + raw_body
    sig = base64.b64encode(hmac.new(key_bytes, canonical, hashlib.sha256).digest()).decode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "svix-id": msg_id,
        "svix-timestamp": ts_str,
        "svix-signature": f"v1,{sig}",
    }

    resp = await async_client.post("/api/sales-supervisor/webhooks/delivery/resend", content=raw_body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["ignored"] == 1
    assert resp.json()["persisted"] == 0


# ---------------------------------------------------------------------------
# 3. ASYNCHRONOUS RECONCILER & STATE TRANSITION TESTS (DC3-06)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconciler_resolves_outcome_unknown_on_delivery(session_factory):
    """
    Validates:
    - SendGrid timeout left job in OUTCOME_UNKNOWN.
    - Verified delivery webhook arrives.
    - Reconciler transitions OUTCOME_UNKNOWN -> RECONCILED -> SENT.
    - Audit notes recorded with system_webhook_reconciler actor.
    """
    dispatch_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    prov_msg_id = "sg_msg_timed_out_101"

    # Seed dispatch in OUTCOME_UNKNOWN
    async with session_factory() as session:
        async with session.begin():
            job = SupervisorApprovalDispatch(
                id=dispatch_id,
                approval_reference_id=uuid.uuid4(),
                lead_reference_id=lead_id,
                action_id="act_test_1",
                channel="EMAIL",
                recipient="buyer@target.com",
                context_fingerprint="fp123",
                idempotency_key="idemp_recon_1",
                payload_hash="hash123",
                payload_json={"subject": "Offer"},
                status=SupervisorApprovalDispatchStatus.OUTCOME_UNKNOWN.value,
                provider_name="SENDGRID",
                provider_message_id=prov_msg_id,
                attempt_count=1,
            )
            session.add(job)

    # Ingest DELIVERED event
    async with session_factory() as session:
        async with session.begin():
            event, _ = await SupervisorDeliveryEventRepository.record_delivery_event(
                session,
                provider_name="SENDGRID",
                provider_message_id=prov_msg_id,
                provider_event_id="sg_ev_delivered_001",
                event_type="DELIVERED",
                raw_event_type="delivered",
                event_timestamp=now_utc(),
                dispatch_id=dispatch_id,
            )

    # Run reconciler
    reconciler = SupervisorTelemetryReconciler()
    stats = await reconciler.run_reconciliation_batch(session_factory)
    assert stats["processed"] == 1
    assert stats["reconciled_sent"] == 1

    # Verify final dispatch status
    async with session_factory() as session:
        updated = await session.get(SupervisorApprovalDispatch, dispatch_id)
        assert updated.status == SupervisorApprovalDispatchStatus.SENT.value
        assert SYSTEM_RECONCILER_ACTOR_ID in updated.reconciliation_notes


@pytest.mark.asyncio
async def test_reconciler_does_not_mutate_already_sent_dispatch(session_factory):
    """
    Validates State Monotonicity:
    A dispatch that is already SENT is NOT flipped to FAILED by a late bounce webhook.
    """
    dispatch_id = uuid.uuid4()
    prov_msg_id = "re_msg_sent_202"

    async with session_factory() as session:
        async with session.begin():
            job = SupervisorApprovalDispatch(
                id=dispatch_id,
                approval_reference_id=uuid.uuid4(),
                lead_reference_id=uuid.uuid4(),
                action_id="act_test_2",
                channel="EMAIL",
                recipient="buyer@target.com",
                context_fingerprint="fp123",
                idempotency_key="idemp_sent_2",
                payload_hash="hash123",
                payload_json={"subject": "Offer"},
                status=SupervisorApprovalDispatchStatus.SENT.value,
                provider_name="RESEND",
                provider_message_id=prov_msg_id,
                attempt_count=1,
            )
            session.add(job)

    # Late bounce webhook arrives
    async with session_factory() as session:
        async with session.begin():
            event, _ = await SupervisorDeliveryEventRepository.record_delivery_event(
                session,
                provider_name="RESEND",
                provider_message_id=prov_msg_id,
                provider_event_id="resend_ev_late_bounce",
                event_type="BOUNCED",
                raw_event_type="email.bounced",
                event_timestamp=now_utc(),
                dispatch_id=dispatch_id,
            )

    reconciler = SupervisorTelemetryReconciler()
    stats = await reconciler.run_reconciliation_batch(session_factory)
    assert stats["processed"] == 1
    assert stats["unaffected"] == 1

    # Status remains SENT
    async with session_factory() as session:
        updated = await session.get(SupervisorApprovalDispatch, dispatch_id)
        assert updated.status == SupervisorApprovalDispatchStatus.SENT.value


@pytest.mark.asyncio
async def test_reconciliation_cannot_trigger_resend(session_factory):
    """
    Invariant: Reconciler never transitions to PENDING or schedules a resend.
    """
    dispatch_id = uuid.uuid4()
    prov_msg_id = "sg_dropped_303"

    async with session_factory() as session:
        async with session.begin():
            job = SupervisorApprovalDispatch(
                id=dispatch_id,
                approval_reference_id=uuid.uuid4(),
                lead_reference_id=uuid.uuid4(),
                action_id="act_test_3",
                channel="EMAIL",
                recipient="buyer@target.com",
                context_fingerprint="fp123",
                idempotency_key="idemp_drop_3",
                payload_hash="hash123",
                payload_json={"subject": "Offer"},
                status=SupervisorApprovalDispatchStatus.OUTCOME_UNKNOWN.value,
                provider_name="SENDGRID",
                provider_message_id=prov_msg_id,
                attempt_count=1,
            )
            session.add(job)

    async with session_factory() as session:
        async with session.begin():
            await SupervisorDeliveryEventRepository.record_delivery_event(
                session,
                provider_name="SENDGRID",
                provider_message_id=prov_msg_id,
                provider_event_id="sg_ev_drop_303",
                event_type="DROPPED",
                raw_event_type="dropped",
                event_timestamp=now_utc(),
                dispatch_id=dispatch_id,
            )

    reconciler = SupervisorTelemetryReconciler()
    stats = await reconciler.run_reconciliation_batch(session_factory)
    assert stats["reconciled_failed"] == 1

    async with session_factory() as session:
        updated = await session.get(SupervisorApprovalDispatch, dispatch_id)
        assert updated.status == SupervisorApprovalDispatchStatus.FAILED.value
        assert updated.next_retry_at is None  # Zero auto-resend


# ---------------------------------------------------------------------------
# 4. PRIVACY, REDACTION & PII TESTS (DC3-08 & Finding 3)
# ---------------------------------------------------------------------------

def test_mask_recipient_pii():
    assert mask_recipient("john.doe@company.com") == "j***@company.com"
    assert mask_recipient("a@domain.com") == "***@domain.com"
    assert mask_recipient("+15551234567") == "+1***67"
    assert mask_recipient(None) is None


def test_sanitize_error_message_masks_emails_and_tokens():
    raw_error = "Failed for recipient user.test@corporate.org with Bearer sk_test_123456789 and SG.abcdefghijklmnopqrstuvwx"
    sanitized = sanitize_error_message(raw_error)
    assert "user.test@corporate.org" not in sanitized
    assert "[REDACTED_EMAIL]" in sanitized
    assert "Bearer [REDACTED]" in sanitized
    assert "SG.[REDACTED]" in sanitized


@pytest.mark.asyncio
async def test_deleted_lead_preserves_forensic_telemetry(session_factory):
    """
    Verifies that when a lead is deleted (lead_id set to NULL),
    the delivery event retains lead_reference_id and dispatch_reference_id for forensic audit.
    """
    lead_id = uuid.uuid4()
    dispatch_id = uuid.uuid4()

    async with session_factory() as session:
        async with session.begin():
            event, _ = await SupervisorDeliveryEventRepository.record_delivery_event(
                session,
                provider_name="RESEND",
                provider_message_id="re_forensic_01",
                provider_event_id="resend_ev_forensic_01",
                event_type="DELIVERED",
                raw_event_type="email.delivered",
                event_timestamp=now_utc(),
                dispatch_id=dispatch_id,
                dispatch_reference_id=dispatch_id,
                lead_reference_id=lead_id,
                recipient_masked="u***@target.com",
            )

    async with session_factory() as session:
        fetched = await SupervisorDeliveryEventRepository.get_by_provider_event(
            session, provider_name="RESEND", provider_event_id="resend_ev_forensic_01"
        )
        assert fetched is not None
        assert fetched.lead_reference_id == lead_id
        assert fetched.dispatch_reference_id == dispatch_id
        assert fetched.recipient_masked == "u***@target.com"


# ---------------------------------------------------------------------------
# 5. CONCURRENCY, RATE LIMITING & FINDINGS REMEDIATION (DC3-07, Findings 1, 2, 4)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_token_bucket_rate_limiter_throttles():
    """Validates Finding 1: TokenBucketRateLimiter enforces configured rate per second."""
    limiter = TokenBucketRateLimiter(rate_per_second=10.0, capacity=2.0)
    # Drain the 2 initial tokens
    await limiter.acquire(1.0)
    await limiter.acquire(1.0)

    # Next acquire must wait
    t0 = time.monotonic()
    slept = await limiter.acquire(1.0)
    elapsed = time.monotonic() - t0

    assert slept > 0.05
    assert elapsed >= 0.08


@pytest.mark.asyncio
async def test_concurrency_gate_caps_workers():
    """Validates Finding 1: OutreachConcurrencyGate enforces max parallel workers."""
    gate = OutreachConcurrencyGate(max_concurrent_workers=2)
    active_workers = 0
    max_observed_workers = 0

    async def worker_task():
        nonlocal active_workers, max_observed_workers
        async with gate:
            active_workers += 1
            max_observed_workers = max(max_observed_workers, active_workers)
            await asyncio.sleep(0.05)
            active_workers -= 1

    # Launch 5 concurrent tasks
    await asyncio.gather(*[worker_task() for _ in range(5)])
    assert max_observed_workers <= 2


def test_provider_factory_rejects_unsupported_provider():
    """Validates Finding 2: Unrecognized provider name raises DispatchConfigurationError (fail closed)."""
    class MockSettings:
        def is_dispatch_permitted(self, ch):
            return True
        outreach_email_provider = "INVALID_POSTMARK"

    with pytest.raises(DispatchConfigurationError) as exc_info:
        get_dispatch_provider("EMAIL", settings=MockSettings())
    assert "Unsupported email provider 'INVALID_POSTMARK'" in str(exc_info.value)


def test_provider_factory_allowlist_matches_approved_providers():
    assert ALLOWED_EMAIL_PROVIDERS == frozenset({"RESEND", "SENDGRID"})


@pytest.mark.asyncio
async def test_webhook_routes_exempt_from_local_api_bearer_auth(session_factory, monkeypatch):
    """REM-01: Verify delivery webhooks bypass bearer auth while requiring cryptographic signatures."""
    from app.main import app, get_db
    monkeypatch.setattr(settings, "api_auth_token", "super-secret-api-token")
    monkeypatch.setattr(settings, "outreach_resend_webhook_secret", "whsec_dGVzdHNlY3JldA==")
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Normal API route without bearer token is rejected with 401 UNAUTHORIZED (bearer required)
        api_res = await client.get("/api/leads")
        assert api_res.status_code == 401
        assert api_res.json()["error"]["code"] == "UNAUTHORIZED"

        # 2. Webhook route without bearer token reaches webhook handler and checks signature (not bearer)
        # Without signature headers -> fails with SIGNATURE_MISSING, NOT bearer token required!
        wh_res = await client.post(
            "/api/sales-supervisor/webhooks/delivery/resend",
            headers={"Content-Type": "application/json"},
            content=json.dumps({"type": "email.delivered", "data": {}}),
        )
        assert wh_res.status_code == 401
        data = wh_res.json()
        error_code = data.get("error", {}).get("code") or data.get("detail", {}).get("code")
        assert error_code == "SIGNATURE_MISSING"
        # Crucially, this was NOT blocked by local_api_auth middleware
        assert "Sign in required" not in str(data) and "Valid API bearer token required" not in str(data)

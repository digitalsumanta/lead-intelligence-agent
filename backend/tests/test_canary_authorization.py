"""
Automated Test Suite for Gate D-D3: Two-Person Canary Flight Authorization (ACM-05).

Phase 12J - D2 - Gate D-D3:
Validates all 16 required test dimensions:
1. Deterministic canonical scope fingerprinting
2. Flight scope creation in PENDING_APPROVAL status
3. Invalid scope parameter validation errors
4. Signer 1 approval records first signature, flight remains PENDING_APPROVAL
5. Signer 2 approval completes dual authorization, transitions to AUTHORIZED
6. Self-approval prohibited (Signer 2 cannot equal Signer 1)
7. Shared / unverified development identities rejected
8. Scope drift rejection via fingerprint mismatch
9. Blank or empty signer role rejected
10. Approval attempt after flight expiration rejected
11. Flight revocation lifecycle and subsequent dispatch prevention
12. Atomic cumulative message cap reservation and transition to EXHAUSTED
13. Recipient allowlist enforcement during slot reservation
14. Dispatch reservation rejected on un-authorized flight (PENDING_APPROVAL)
15. FastAPI REST API endpoints lifecycle (create, get, approve, revoke)
16. Full integration with SupervisorApprovalDispatchService pre-flight guard check
"""
from __future__ import annotations

import asyncio
import hmac
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes.supervisor_canary import router as canary_router
from app.config import settings
from app.database import get_db
from app.domain.dispatch_errors import (
    CanaryAuthorizationError,
    CanaryDualApprovalRequiredError,
    CanaryFlightExpiredError,
    CanaryFlightRevokedError,
    CanaryMessageCapExceededError,
    CanaryScopeDriftError,
    CanarySelfApprovalError,
    DispatchConfigurationError,
    DispatchRecipientNotAllowlistedError,
)
from app.domain.sales_supervisor import compute_action_context_fingerprint
from app.models.entities import (
    Base,
    CanaryFlightAuthorization,
    CanaryFlightStatus,
    ConfidenceLevel,
    Lead,
    LeadIntelligenceOpportunity,
    SourceType,
    SupervisorApprovalDispatch,
    SupervisorApprovalDispatchStatus,
    SupervisorHumanApproval,
    now_utc,
)
from app.repositories.supervisor_approval import SupervisorApprovalRepository
from app.repositories.supervisor_dispatch_repository import SupervisorApprovalDispatchRepository
from app.services.canary_authorization_service import (
    CanaryAuthorizationService,
    compute_canary_scope_fingerprint,
    is_shared_development_identity,
)
from app.services.supervisor_dispatch_service import SupervisorApprovalDispatchService


async def _seed_lead_opp_and_approval(
    session: AsyncSession,
    email: str = "lead1@internal-test.leadintel.example.com",
    now: datetime | None = None,
) -> tuple[Lead, LeadIntelligenceOpportunity, SupervisorHumanApproval]:
    t_now = now or datetime.now(timezone.utc)
    lead = Lead(id=uuid.uuid4(), company_name="Test Company", email=email)
    session.add(lead)
    await session.flush()

    opp = LeadIntelligenceOpportunity(
        id=uuid.uuid4(),
        lead_id=lead.id,
        title="Test Opportunity",
        score=85,
        recommended_action="PERSONALIZED_EMAIL",
        evidence={},
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH,
    )
    session.add(opp)
    await session.flush()

    act_id = f"action_{uuid.uuid4().hex[:8]}"
    fp = compute_action_context_fingerprint(
        action_id=act_id,
        action_type="PERSONALIZED_EMAIL",
        lead_id=lead.id,
        opportunity_id=opp.id,
        contact_id=None,
        channel="EMAIL",
        contact_email=email,
        contact_phone=None,
        opportunity_score=85.0,
        recommendation_fit=0.0,
        evidence_keys=[],
    )

    approval = SupervisorHumanApproval(
        id=uuid.uuid4(),
        action_id=act_id,
        lead_id=lead.id,
        opportunity_id=opp.id,
        recommended_action="PERSONALIZED_EMAIL",
        action_tier="EXTERNAL_OUTREACH",
        context_fingerprint=fp,
        requested_by_user_id="user1",
        status="APPROVED",
        channel="EMAIL",
        version=1,
        expires_at=t_now + timedelta(hours=1),
    )
    session.add(approval)
    await session.flush()
    return lead, opp, approval


# ---------------------------------------------------------------------------
# FIXTURES
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
    """Single db session fixture."""
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
def test_app(session_factory):
    """FastAPI test app with mounted canary router and auth middleware."""
    app = FastAPI()

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            token = settings.api_auth_token
            supplied = request.headers.get("authorization", "")
            supplied = supplied[7:].strip() if supplied.lower().startswith("bearer ") else ""
            if not token or not supplied or not hmac.compare_digest(token, supplied):
                return JSONResponse(
                    status_code=401,
                    content={"code": "UNAUTHORIZED", "message": "Valid API bearer token required"},
                )
            request.state.authenticated_user_id = settings.api_auth_user_id.strip() or None
            request.state.authenticated_role = settings.api_auth_role.strip() or "viewer"
        return await call_next(request)

    app.include_router(canary_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app


# ---------------------------------------------------------------------------
# TESTS
# ---------------------------------------------------------------------------

def test_canary_scope_fingerprint_deterministic_and_canonical():
    """Test 1: Scope fingerprint calculation is deterministic and sensitive to all parameters."""
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 28, 14, 0, 0, tzinfo=timezone.utc)

    fp1 = compute_canary_scope_fingerprint(
        flight_name="Flight Alpha",
        environment="staging",
        provider_name="RESEND",
        allowed_domains=["internal-test.leadintel.example.com", "canary.example.com"],
        max_messages=5,
        max_concurrency=1,
        rate_limit_per_second=0.2,
        valid_from=t0,
        expires_at=t1,
    )

    # Identical with different domain list ordering -> must produce SAME fingerprint
    fp2 = compute_canary_scope_fingerprint(
        flight_name="Flight Alpha",
        environment="staging",
        provider_name="RESEND",
        allowed_domains=["canary.example.com", "internal-test.leadintel.example.com"],
        max_messages=5,
        max_concurrency=1,
        rate_limit_per_second=0.2,
        valid_from=t0,
        expires_at=t1,
    )
    assert fp1 == fp2

    # Modified max_messages -> must produce DIFFERENT fingerprint
    fp3 = compute_canary_scope_fingerprint(
        flight_name="Flight Alpha",
        environment="staging",
        provider_name="RESEND",
        allowed_domains=["internal-test.leadintel.example.com"],
        max_messages=10,
        max_concurrency=1,
        rate_limit_per_second=0.2,
        valid_from=t0,
        expires_at=t1,
    )
    assert fp1 != fp3


@pytest.mark.asyncio
async def test_canary_flight_creation_pending_approval(db_session: AsyncSession):
    """Test 2: Flight creation initializes status as PENDING_APPROVAL and execution_authorized=False."""
    t0 = datetime.now(timezone.utc)
    t1 = t0 + timedelta(hours=2)

    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Flight Test 1",
        environment="staging",
        provider_name="RESEND",
        allowed_domains=["internal-test.leadintel.example.com"],
        max_messages=5,
        max_concurrency=1,
        rate_limit_per_second=0.2,
        valid_from=t0,
        expires_at=t1,
    )
    assert flight.id is not None
    assert flight.status == CanaryFlightStatus.PENDING_APPROVAL.value
    assert flight.execution_authorized is False
    assert flight.dispatched_count == 0
    assert flight.signer_1_user_id is None
    assert flight.signer_2_user_id is None


@pytest.mark.asyncio
async def test_canary_flight_creation_validation_errors(db_session: AsyncSession):
    """Test 3: Invalid flight parameters are rejected at creation."""
    t0 = datetime.now(timezone.utc)
    t1 = t0 + timedelta(hours=2)

    # Empty name
    with pytest.raises(CanaryAuthorizationError, match="flight_name cannot be empty"):
        await CanaryAuthorizationService.create_flight_scope(
            db_session,
            flight_name="",
            allowed_domains=["example.com"],
            valid_from=t0,
            expires_at=t1,
        )

    # max_messages <= 0
    with pytest.raises(CanaryAuthorizationError, match="max_messages must be greater than 0"):
        await CanaryAuthorizationService.create_flight_scope(
            db_session,
            flight_name="Flight",
            allowed_domains=["example.com"],
            max_messages=0,
            valid_from=t0,
            expires_at=t1,
        )

    # valid_from >= expires_at
    with pytest.raises(CanaryAuthorizationError, match="valid_from must be earlier than expires_at"):
        await CanaryAuthorizationService.create_flight_scope(
            db_session,
            flight_name="Flight",
            allowed_domains=["example.com"],
            valid_from=t1,
            expires_at=t0,
        )

    # empty domains
    with pytest.raises(CanaryAuthorizationError, match="allowed_domains must contain at least one valid domain"):
        await CanaryAuthorizationService.create_flight_scope(
            db_session,
            flight_name="Flight",
            allowed_domains=[],
            valid_from=t0,
            expires_at=t1,
        )


@pytest.mark.asyncio
async def test_canary_flight_first_signature_records_signer_1(db_session: AsyncSession):
    """Test 4: First approval signature is recorded, flight remains in PENDING_APPROVAL."""
    t0 = datetime.now(timezone.utc)
    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Flight 2-Signer",
        allowed_domains=["internal-test.leadintel.example.com"],
        valid_from=t0,
        expires_at=t0 + timedelta(hours=2),
    )

    flight = await CanaryAuthorizationService.submit_flight_approval(
        db_session,
        flight_id=flight.id,
        signer_user_id="lead_architect_42",
        signer_role="engineering_lead",
    )
    assert flight.signer_1_user_id == "lead_architect_42"
    assert flight.signer_1_role == "engineering_lead"
    assert flight.signer_1_signed_at is not None
    assert flight.signer_2_user_id is None
    assert flight.status == CanaryFlightStatus.PENDING_APPROVAL.value
    assert flight.execution_authorized is False


@pytest.mark.asyncio
async def test_canary_flight_second_signature_dual_authorizes(db_session: AsyncSession):
    """Test 5: Second distinct signature completes dual authorization."""
    t0 = datetime.now(timezone.utc)
    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Flight Dual",
        allowed_domains=["internal-test.leadintel.example.com"],
        valid_from=t0,
        expires_at=t0 + timedelta(hours=2),
    )

    await CanaryAuthorizationService.submit_flight_approval(
        db_session,
        flight_id=flight.id,
        signer_user_id="lead_architect_42",
        signer_role="engineering_lead",
    )

    flight = await CanaryAuthorizationService.submit_flight_approval(
        db_session,
        flight_id=flight.id,
        signer_user_id="compliance_officer_99",
        signer_role="compliance_officer",
    )
    assert flight.signer_2_user_id == "compliance_officer_99"
    assert flight.signer_2_role == "compliance_officer"
    assert flight.signer_2_signed_at is not None
    assert flight.status == CanaryFlightStatus.AUTHORIZED.value
    assert flight.execution_authorized is True


@pytest.mark.asyncio
async def test_canary_flight_self_approval_prohibited(db_session: AsyncSession):
    """Test 6: Second approval with the same identity as first signer is rejected."""
    t0 = datetime.now(timezone.utc)
    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Flight Self-Approval",
        allowed_domains=["internal-test.leadintel.example.com"],
        valid_from=t0,
        expires_at=t0 + timedelta(hours=2),
    )

    await CanaryAuthorizationService.submit_flight_approval(
        db_session,
        flight_id=flight.id,
        signer_user_id="lead_architect_42",
        signer_role="engineering_lead",
    )

    with pytest.raises(CanarySelfApprovalError, match="must be a distinct human principal"):
        await CanaryAuthorizationService.submit_flight_approval(
            db_session,
            flight_id=flight.id,
            signer_user_id="lead_architect_42",
            signer_role="compliance_officer",
        )


@pytest.mark.asyncio
async def test_canary_flight_shared_development_identity_rejected(db_session: AsyncSession):
    """Test 7: Shared or unverified development identities are rejected for approval."""
    t0 = datetime.now(timezone.utc)
    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Flight Shared Id",
        allowed_domains=["internal-test.leadintel.example.com"],
        valid_from=t0,
        expires_at=t0 + timedelta(hours=2),
    )

    for invalid_id in ["local_api_user", "admin", "dev", "system", "test", "bot"]:
        with pytest.raises(CanaryAuthorizationError, match="Shared or generic development identity"):
            await CanaryAuthorizationService.submit_flight_approval(
                db_session,
                flight_id=flight.id,
                signer_user_id=invalid_id,
                signer_role="engineer",
            )


@pytest.mark.asyncio
async def test_canary_flight_scope_drift_rejected(db_session: AsyncSession):
    """Test 8: Approval with mismatched scope fingerprint is rejected as scope drift."""
    t0 = datetime.now(timezone.utc)
    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Flight Anti-Drift",
        allowed_domains=["internal-test.leadintel.example.com"],
        valid_from=t0,
        expires_at=t0 + timedelta(hours=2),
    )

    fake_fingerprint = "0" * 64
    with pytest.raises(CanaryScopeDriftError, match="Scope fingerprint mismatch"):
        await CanaryAuthorizationService.submit_flight_approval(
            db_session,
            flight_id=flight.id,
            signer_user_id="lead_architect_42",
            signer_role="engineering_lead",
            scope_fingerprint=fake_fingerprint,
        )


@pytest.mark.asyncio
async def test_canary_flight_empty_role_rejected(db_session: AsyncSession):
    """Test 9: Empty or blank signer role is rejected."""
    t0 = datetime.now(timezone.utc)
    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Flight Empty Role",
        allowed_domains=["internal-test.leadintel.example.com"],
        valid_from=t0,
        expires_at=t0 + timedelta(hours=2),
    )

    with pytest.raises(CanaryAuthorizationError, match="Signer role must not be empty"):
        await CanaryAuthorizationService.submit_flight_approval(
            db_session,
            flight_id=flight.id,
            signer_user_id="lead_architect_42",
            signer_role="   ",
        )


@pytest.mark.asyncio
async def test_canary_flight_approval_after_expiration_rejected(db_session: AsyncSession):
    """Test 10: Approval attempt after expiration timestamp raises CanaryFlightExpiredError."""
    t0 = datetime.now(timezone.utc) - timedelta(hours=2)
    t1 = datetime.now(timezone.utc) - timedelta(minutes=5)

    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Flight Expired",
        allowed_domains=["internal-test.leadintel.example.com"],
        valid_from=t0,
        expires_at=t1,
    )

    with pytest.raises(CanaryFlightExpiredError, match="expired at"):
        await CanaryAuthorizationService.submit_flight_approval(
            db_session,
            flight_id=flight.id,
            signer_user_id="lead_architect_42",
            signer_role="engineering_lead",
            now=datetime.now(timezone.utc),
        )


@pytest.mark.asyncio
async def test_canary_flight_revocation_lifecycle(db_session: AsyncSession):
    """Test 11: Flight revocation sets status to REVOKED and blocks slot reservations."""
    t0 = datetime.now(timezone.utc)
    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Flight Revoke Test",
        allowed_domains=["internal-test.leadintel.example.com"],
        valid_from=t0,
        expires_at=t0 + timedelta(hours=2),
    )

    await CanaryAuthorizationService.submit_flight_approval(
        db_session,
        flight_id=flight.id,
        signer_user_id="lead_architect_42",
        signer_role="engineering_lead",
    )
    await CanaryAuthorizationService.submit_flight_approval(
        db_session,
        flight_id=flight.id,
        signer_user_id="compliance_officer_99",
        signer_role="compliance_officer",
    )

    # Revoke
    revoked = await CanaryAuthorizationService.revoke_flight_authorization(
        db_session,
        flight_id=flight.id,
        revoked_by_user_id="security_officer_77",
        revocation_reason="Telemetry anomaly observed in upstream network",
    )
    assert revoked.status == CanaryFlightStatus.REVOKED.value
    assert revoked.execution_authorized is False
    assert revoked.revoked_by_user_id == "security_officer_77"
    assert "Telemetry anomaly" in revoked.revocation_reason

    # Subsequent reservation attempt fails
    with pytest.raises(CanaryFlightRevokedError, match="revoked"):
        await CanaryAuthorizationService.reserve_dispatch_slot(
            db_session,
            flight_id=flight.id,
            recipient="test@internal-test.leadintel.example.com",
        )


@pytest.mark.asyncio
async def test_canary_flight_atomic_quota_reservation_and_exhaustion(db_session: AsyncSession):
    """Test 12: Atomic cumulative message cap reservation transitions to EXHAUSTED when cap reached."""
    t0 = datetime.now(timezone.utc)
    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Flight Quota",
        allowed_domains=["internal-test.leadintel.example.com"],
        max_messages=3,
        valid_from=t0,
        expires_at=t0 + timedelta(hours=2),
    )
    await CanaryAuthorizationService.submit_flight_approval(
        db_session,
        flight_id=flight.id,
        signer_user_id="lead_architect_42",
        signer_role="engineering_lead",
    )
    await CanaryAuthorizationService.submit_flight_approval(
        db_session,
        flight_id=flight.id,
        signer_user_id="compliance_officer_99",
        signer_role="compliance_officer",
    )

    # Slot 1
    r1 = await CanaryAuthorizationService.reserve_dispatch_slot(
        db_session, flight_id=flight.id, recipient="test1@internal-test.leadintel.example.com"
    )
    assert r1.dispatched_count == 1
    assert r1.status == CanaryFlightStatus.AUTHORIZED.value

    # Slot 2
    r2 = await CanaryAuthorizationService.reserve_dispatch_slot(
        db_session, flight_id=flight.id, recipient="test2@internal-test.leadintel.example.com"
    )
    assert r2.dispatched_count == 2
    assert r2.status == CanaryFlightStatus.AUTHORIZED.value

    # Slot 3 (exhausts cap)
    r3 = await CanaryAuthorizationService.reserve_dispatch_slot(
        db_session, flight_id=flight.id, recipient="test3@internal-test.leadintel.example.com"
    )
    assert r3.dispatched_count == 3
    assert r3.status == CanaryFlightStatus.EXHAUSTED.value

    # Slot 4 (exceeded)
    with pytest.raises(CanaryMessageCapExceededError, match="cumulative cap .* exhausted"):
        await CanaryAuthorizationService.reserve_dispatch_slot(
            db_session, flight_id=flight.id, recipient="test4@internal-test.leadintel.example.com"
        )


@pytest.mark.asyncio
async def test_canary_flight_recipient_allowlist_enforcement_in_reservation(db_session: AsyncSession):
    """Test 13: Recipient allowlist check fails closed during slot reservation."""
    t0 = datetime.now(timezone.utc)
    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Flight Allowlist",
        allowed_domains=["internal-test.leadintel.example.com"],
        max_messages=5,
        valid_from=t0,
        expires_at=t0 + timedelta(hours=2),
    )
    await CanaryAuthorizationService.submit_flight_approval(
        db_session,
        flight_id=flight.id,
        signer_user_id="lead_architect_42",
        signer_role="engineering_lead",
    )
    await CanaryAuthorizationService.submit_flight_approval(
        db_session,
        flight_id=flight.id,
        signer_user_id="compliance_officer_99",
        signer_role="compliance_officer",
    )

    # Allowed recipient
    r = await CanaryAuthorizationService.reserve_dispatch_slot(
        db_session, flight_id=flight.id, recipient="alice@internal-test.leadintel.example.com"
    )
    assert r.dispatched_count == 1

    # Rejected recipient
    with pytest.raises(DispatchRecipientNotAllowlistedError, match="not allowed by canary flight"):
        await CanaryAuthorizationService.reserve_dispatch_slot(
            db_session, flight_id=flight.id, recipient="attacker@external-evil.com"
        )


@pytest.mark.asyncio
async def test_canary_flight_unauthorized_reservation_rejected(db_session: AsyncSession):
    """Test 14: Slot reservation attempted on flight with only 1 signature is rejected."""
    t0 = datetime.now(timezone.utc)
    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Flight Pending",
        allowed_domains=["internal-test.leadintel.example.com"],
        max_messages=5,
        valid_from=t0,
        expires_at=t0 + timedelta(hours=2),
    )
    await CanaryAuthorizationService.submit_flight_approval(
        db_session,
        flight_id=flight.id,
        signer_user_id="lead_architect_42",
        signer_role="engineering_lead",
    )

    with pytest.raises(CanaryDualApprovalRequiredError, match="is not fully dual-authorized"):
        await CanaryAuthorizationService.reserve_dispatch_slot(
            db_session, flight_id=flight.id, recipient="alice@internal-test.leadintel.example.com"
        )


@pytest.mark.asyncio
async def test_canary_flight_api_lifecycle(test_app: FastAPI, monkeypatch):
    """
    Test 15: Full REST API lifecycle with trusted authentication binding (ACM-05, Finding D3.1-01).
    - Unauthenticated request is rejected (401 Unauthorized)
    - Shared / generic development identity ('local_api_user') fails closed (403 UNVERIFIED_SHARED_IDENTITY)
    - Body impersonation mismatch is rejected (403 IMPERSONATION_FORBIDDEN)
    - Distinct verified human identities successfully approve
    - Self-approval is rejected (403 FORBIDDEN)
    - Revocation impersonation is rejected and verified revoke succeeds
    """
    monkeypatch.setattr(settings, "api_auth_token", "test-canary-api-token")
    monkeypatch.setattr(settings, "app_env", "development")

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        t0 = datetime.now(timezone.utc)
        t1 = t0 + timedelta(hours=2)

        # 0a. Missing Bearer Token -> 401 Unauthorized
        unauth_res = await client.post(
            "/api/sales-supervisor/canary/flights",
            json={
                "flight_name": "API Canary Flight",
                "environment": "staging",
                "provider_name": "RESEND",
                "allowed_domains": ["internal-test.leadintel.example.com"],
                "max_messages": 5,
                "max_concurrency": 1,
                "rate_limit_per_second": 0.2,
                "valid_from": t0.isoformat(),
                "expires_at": t1.isoformat(),
            },
        )
        assert unauth_res.status_code == status.HTTP_401_UNAUTHORIZED

        # 1. Create flight with valid token
        auth_headers = {"Authorization": "Bearer test-canary-api-token"}
        monkeypatch.setattr(settings, "api_auth_user_id", "architect_alice")
        monkeypatch.setattr(settings, "api_auth_role", "engineering_lead")

        create_res = await client.post(
            "/api/sales-supervisor/canary/flights",
            json={
                "flight_name": "API Canary Flight",
                "environment": "staging",
                "provider_name": "RESEND",
                "allowed_domains": ["internal-test.leadintel.example.com"],
                "max_messages": 5,
                "max_concurrency": 1,
                "rate_limit_per_second": 0.2,
                "valid_from": t0.isoformat(),
                "expires_at": t1.isoformat(),
            },
            headers=auth_headers,
        )
        assert create_res.status_code == status.HTTP_201_CREATED
        flight_data = create_res.json()
        flight_id = flight_data["id"]
        fingerprint = flight_data["scope_fingerprint"]
        assert flight_data["status"] == "PENDING_APPROVAL"

        # 2. Get flight
        get_res = await client.get(
            f"/api/sales-supervisor/canary/flights/{flight_id}",
            headers=auth_headers,
        )
        assert get_res.status_code == status.HTTP_200_OK
        assert get_res.json()["id"] == flight_id

        # 3a. Shared development identity ('local_api_user') fails closed
        monkeypatch.setattr(settings, "api_auth_user_id", "local_api_user")
        shared_appr_res = await client.post(
            f"/api/sales-supervisor/canary/flights/{flight_id}/approve",
            json={"signer_role": "engineering_lead", "scope_fingerprint": fingerprint},
            headers=auth_headers,
        )
        assert shared_appr_res.status_code == status.HTTP_403_FORBIDDEN
        assert shared_appr_res.json()["detail"]["code"] == "UNVERIFIED_SHARED_IDENTITY"

        # 3b. Impersonation attempt: authenticated as alice, body claims bob -> 403
        monkeypatch.setattr(settings, "api_auth_user_id", "architect_alice")
        impersonate_res = await client.post(
            f"/api/sales-supervisor/canary/flights/{flight_id}/approve",
            json={
                "signer_user_id": "impersonated_user_bob",
                "signer_role": "engineering_lead",
                "scope_fingerprint": fingerprint,
            },
            headers=auth_headers,
        )
        assert impersonate_res.status_code == status.HTTP_403_FORBIDDEN
        assert impersonate_res.json()["detail"]["code"] == "IMPERSONATION_FORBIDDEN"

        # 3c. Valid Signer 1 approval -> 200 PENDING_APPROVAL
        appr1_res = await client.post(
            f"/api/sales-supervisor/canary/flights/{flight_id}/approve",
            json={
                "signer_user_id": "architect_alice",
                "signer_role": "engineering_lead",
                "scope_fingerprint": fingerprint,
            },
            headers=auth_headers,
        )
        assert appr1_res.status_code == status.HTTP_200_OK
        assert appr1_res.json()["status"] == "PENDING_APPROVAL"
        assert appr1_res.json()["signer_1_user_id"] == "architect_alice"

        # 4. Signer 1 attempts self-approval -> 403 Forbidden
        self_res = await client.post(
            f"/api/sales-supervisor/canary/flights/{flight_id}/approve",
            json={
                "signer_user_id": "architect_alice",
                "signer_role": "compliance_lead",
                "scope_fingerprint": fingerprint,
            },
            headers=auth_headers,
        )
        assert self_res.status_code == status.HTTP_403_FORBIDDEN

        # 5. Distinct Signer 2 approves -> 200 AUTHORIZED
        monkeypatch.setattr(settings, "api_auth_user_id", "officer_bob")
        monkeypatch.setattr(settings, "api_auth_role", "compliance_lead")
        appr2_res = await client.post(
            f"/api/sales-supervisor/canary/flights/{flight_id}/approve",
            json={
                "signer_user_id": "officer_bob",
                "signer_role": "compliance_lead",
                "scope_fingerprint": fingerprint,
            },
            headers=auth_headers,
        )
        assert appr2_res.status_code == status.HTTP_200_OK
        assert appr2_res.json()["status"] == "AUTHORIZED"
        assert appr2_res.json()["execution_authorized"] is True
        assert appr2_res.json()["signer_2_user_id"] == "officer_bob"

        # 6a. Impersonation attempt on revoke -> 403
        monkeypatch.setattr(settings, "api_auth_user_id", "operator_carol")
        bad_revoke_res = await client.post(
            f"/api/sales-supervisor/canary/flights/{flight_id}/revoke",
            json={
                "revoked_by_user_id": "someone_else",
                "revocation_reason": "Attempted forged revocation",
            },
            headers=auth_headers,
        )
        assert bad_revoke_res.status_code == status.HTTP_403_FORBIDDEN
        assert bad_revoke_res.json()["detail"]["code"] == "IMPERSONATION_FORBIDDEN"

        # 6b. Valid Revoke by operator_carol -> 200 REVOKED
        revoke_res = await client.post(
            f"/api/sales-supervisor/canary/flights/{flight_id}/revoke",
            json={
                "revoked_by_user_id": "operator_carol",
                "revocation_reason": "Emergency flight halt requested",
            },
            headers=auth_headers,
        )
        assert revoke_res.status_code == status.HTTP_200_OK
        assert revoke_res.json()["status"] == "REVOKED"
        assert revoke_res.json()["execution_authorized"] is False


@pytest.mark.asyncio
async def test_canary_flight_dispatch_service_integration(db_session: AsyncSession, monkeypatch):
    """Test 16: End-to-end integration between CanaryAuthorizationService and SupervisorApprovalDispatchService."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    monkeypatch.setattr(settings, "outreach_canary_allowed_domains", "internal-test.leadintel.example.com")

    t0 = datetime.now(timezone.utc)
    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Integration Flight",
        allowed_domains=["internal-test.leadintel.example.com"],
        max_messages=1,
        valid_from=t0,
        expires_at=t0 + timedelta(hours=2),
    )
    await CanaryAuthorizationService.submit_flight_approval(
        db_session,
        flight_id=flight.id,
        signer_user_id="architect_alice",
        signer_role="engineering_lead",
    )
    await CanaryAuthorizationService.submit_flight_approval(
        db_session,
        flight_id=flight.id,
        signer_user_id="officer_bob",
        signer_role="compliance_lead",
    )

    # Create dummy lead and approval
    lead = Lead(id=uuid.uuid4(), company_name="Test Company", email="lead1@internal-test.leadintel.example.com")
    db_session.add(lead)
    approval = SupervisorHumanApproval(
        id=uuid.uuid4(),
        action_id="action_int_1",
        lead_id=lead.id,
        opportunity_id=uuid.uuid4(),
        recommended_action="EMAIL_OUTREACH",
        action_tier="EXTERNAL_OUTREACH",
        context_fingerprint="fp1",
        requested_by_user_id="user1",
        status="APPROVED",
        channel="EMAIL",
        version=1,
        execution_claimed_by="test-worker",
        execution_claimed_at=t0,
        expires_at=t0 + timedelta(hours=1),
    )
    db_session.add(approval)
    await db_session.flush()

    dispatch_repo = SupervisorApprovalDispatchRepository()
    approval_repo = SupervisorApprovalRepository()
    service = SupervisorApprovalDispatchService(repository=dispatch_repo, approval_repository=approval_repo)

    # Create claimed canary dispatch
    outbox = await dispatch_repo.create_dispatch(
        db_session,
        approval_id=approval.id,
        approval_reference_id=approval.id,
        lead_id=lead.id,
        lead_reference_id=lead.id,
        action_id=approval.action_id,
        channel="EMAIL",
        recipient="test@internal-test.leadintel.example.com",
        context_fingerprint="fp1",
        idempotency_key="key1",
        payload_hash="hash1",
        payload_json={"canary_flight_id": str(flight.id)},
        provider_name="RESEND",
        now=t0,
    )
    assert outbox.id is not None

    # Pre-flight guard check reserves the slot
    outbox.worker_id = "test-worker"
    outbox.status = SupervisorApprovalDispatchStatus.IN_PROGRESS.value
    outbox.lease_expires_at = t0 + timedelta(minutes=5)
    await db_session.flush()

    guarded_job = await service.pre_flight_guard_check(
        db_session,
        dispatch_id=outbox.id,
        worker_id="test-worker",
        expected_lease_generation=1,
        now=t0,
    )
    assert guarded_job.id == outbox.id

    # Check that flight quota was incremented to 1 and status transitioned to EXHAUSTED
    updated_flight = await CanaryAuthorizationService.get_flight(db_session, flight.id)
    assert updated_flight.dispatched_count == 1
    assert updated_flight.status == CanaryFlightStatus.EXHAUSTED.value

    # Subsequent pre-flight guard check on a second job fails with cap exhausted
    outbox2 = await dispatch_repo.create_dispatch(
        db_session,
        approval_id=approval.id,
        approval_reference_id=approval.id,
        lead_id=lead.id,
        lead_reference_id=lead.id,
        action_id="action_int_2",
        channel="EMAIL",
        recipient="test@internal-test.leadintel.example.com",
        context_fingerprint="fp2",
        idempotency_key="key2",
        payload_hash="hash2",
        payload_json={"canary_flight_id": str(flight.id)},
        provider_name="RESEND",
        now=t0,
    )
    outbox2.worker_id = "test-worker"
    outbox2.status = SupervisorApprovalDispatchStatus.IN_PROGRESS.value
    outbox2.lease_expires_at = t0 + timedelta(minutes=5)
    await db_session.flush()

    with pytest.raises(CanaryMessageCapExceededError):
        await service.pre_flight_guard_check(
            db_session,
            dispatch_id=outbox2.id,
            worker_id="test-worker",
            expected_lease_generation=1,
            now=t0,
        )
    # Job 2 transitioned to ABORTED
    assert outbox2.status == SupervisorApprovalDispatchStatus.ABORTED.value
    assert outbox2.error_code == "CANARY_MESSAGE_CAP_EXHAUSTED"


@pytest.mark.asyncio
async def test_canary_dispatch_enqueue_rejects_missing_flight_id(db_session: AsyncSession, monkeypatch):
    """Test 17: In canary mode, enqueue_dispatch strictly requires canary_flight_id (Finding D3.1-02)."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    monkeypatch.setattr(settings, "outreach_canary_mode", True)
    monkeypatch.setattr(settings, "outreach_canary_allowed_domains", "internal-test.leadintel.example.com")

    t0 = datetime.now(timezone.utc)
    lead, opp, approval = await _seed_lead_opp_and_approval(db_session, now=t0)

    service = SupervisorApprovalDispatchService(
        repository=SupervisorApprovalDispatchRepository(),
        approval_repository=SupervisorApprovalRepository(),
    )

    # 1. Enqueue without payload_json -> rejects missing canary_flight_id
    with pytest.raises(CanaryAuthorizationError) as exc_info:
        await service.enqueue_dispatch(db_session, approval_id=approval.id, now=t0)
    assert "mandatory canary_flight_id is missing" in str(exc_info.value)

    # 2. Enqueue with payload missing canary_flight_id -> rejects
    with pytest.raises(CanaryAuthorizationError) as exc_info2:
        await service.enqueue_dispatch(
            db_session,
            approval_id=approval.id,
            payload_json={"canary": True},
            now=t0,
        )
    assert "mandatory canary_flight_id is missing" in str(exc_info2.value)


@pytest.mark.asyncio
async def test_canary_dispatch_enqueue_rejects_unauthorized_or_malformed_flight(db_session: AsyncSession, monkeypatch):
    """Test 18: In canary mode, enqueue_dispatch rejects malformed and non-authorized flights."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    monkeypatch.setattr(settings, "outreach_canary_mode", True)
    monkeypatch.setattr(settings, "outreach_canary_allowed_domains", "internal-test.leadintel.example.com")

    t0 = datetime.now(timezone.utc)
    lead, opp, approval = await _seed_lead_opp_and_approval(db_session, now=t0)

    # Flight in PENDING_APPROVAL status (only 1 signer)
    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Pending Flight",
        allowed_domains=["internal-test.leadintel.example.com"],
        valid_from=t0,
        expires_at=t0 + timedelta(hours=2),
    )
    await CanaryAuthorizationService.submit_flight_approval(
        db_session, flight_id=flight.id, signer_user_id="architect_alice", signer_role="engineering_lead"
    )
    await db_session.flush()

    service = SupervisorApprovalDispatchService(
        repository=SupervisorApprovalDispatchRepository(),
        approval_repository=SupervisorApprovalRepository(),
    )

    # 1. Malformed UUID
    with pytest.raises(CanaryAuthorizationError) as malformed_err:
        await service.enqueue_dispatch(
            db_session,
            approval_id=approval.id,
            payload_json={"canary_flight_id": "not-a-uuid"},
            now=t0,
        )
    assert "Invalid canary_flight_id format" in str(malformed_err.value)

    # 2. Flight not fully authorized
    with pytest.raises(CanaryAuthorizationError) as unauth_err:
        await service.enqueue_dispatch(
            db_session,
            approval_id=approval.id,
            payload_json={"canary_flight_id": str(flight.id)},
            now=t0,
        )
    assert "is not authorized" in str(unauth_err.value)


@pytest.mark.asyncio
async def test_canary_dispatch_guard_check_fails_closed_without_flight_id(db_session: AsyncSession, monkeypatch):
    """Test 19: Guard 6 aborts job and raises CanaryAuthorizationError if canary_flight_id is omitted (Finding D3.1-02)."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    monkeypatch.setattr(settings, "outreach_canary_mode", True)
    monkeypatch.setattr(settings, "outreach_canary_allowed_domains", "internal-test.leadintel.example.com")

    t0 = datetime.now(timezone.utc)
    lead = Lead(id=uuid.uuid4(), company_name="Test Company", email="lead1@internal-test.leadintel.example.com")
    db_session.add(lead)
    approval = SupervisorHumanApproval(
        id=uuid.uuid4(),
        action_id="action_guard_1",
        lead_id=lead.id,
        opportunity_id=uuid.uuid4(),
        recommended_action="EMAIL_OUTREACH",
        action_tier="EXTERNAL_OUTREACH",
        context_fingerprint="fp1",
        requested_by_user_id="user1",
        status="APPROVED",
        channel="EMAIL",
        version=1,
        expires_at=t0 + timedelta(hours=1),
    )
    db_session.add(approval)
    await db_session.flush()

    dispatch_repo = SupervisorApprovalDispatchRepository()
    approval_repo = SupervisorApprovalRepository()
    service = SupervisorApprovalDispatchService(repository=dispatch_repo, approval_repository=approval_repo)

    # Outbox row in canary mode but lacking canary_flight_id
    outbox = await dispatch_repo.create_dispatch(
        db_session,
        approval_id=approval.id,
        approval_reference_id=approval.id,
        lead_id=lead.id,
        lead_reference_id=lead.id,
        action_id=approval.action_id,
        channel="EMAIL",
        recipient="test@internal-test.leadintel.example.com",
        context_fingerprint="fp1",
        idempotency_key="guard_test_key_1",
        payload_hash="hash_guard_1",
        payload_json={"canary": True},  # No canary_flight_id!
        provider_name="RESEND",
        now=t0,
    )
    outbox.worker_id = "guard-worker"
    outbox.status = SupervisorApprovalDispatchStatus.IN_PROGRESS.value
    outbox.lease_expires_at = t0 + timedelta(minutes=5)
    await db_session.flush()

    # Pre-flight guard check must abort the job fail-closed
    with pytest.raises(CanaryAuthorizationError) as err:
        await service.pre_flight_guard_check(
            db_session,
            dispatch_id=outbox.id,
            worker_id="guard-worker",
            expected_lease_generation=1,
            now=t0,
        )
    assert "mandatory canary_flight_id is missing" in str(err.value)
    assert outbox.status == SupervisorApprovalDispatchStatus.ABORTED.value
    assert outbox.error_code == "CANARY_FLIGHT_ID_REQUIRED"


@pytest.mark.asyncio
async def test_canary_dispatch_guard_check_aborts_on_malformed_flight_id(db_session: AsyncSession, monkeypatch):
    """Test 20: Guard 6 aborts job on malformed canary_flight_id."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    monkeypatch.setattr(settings, "outreach_canary_mode", True)
    monkeypatch.setattr(settings, "outreach_canary_allowed_domains", "internal-test.leadintel.example.com")

    t0 = datetime.now(timezone.utc)
    lead = Lead(id=uuid.uuid4(), company_name="Test Company", email="lead1@internal-test.leadintel.example.com")
    db_session.add(lead)
    approval = SupervisorHumanApproval(
        id=uuid.uuid4(),
        action_id="action_guard_2",
        lead_id=lead.id,
        opportunity_id=uuid.uuid4(),
        recommended_action="EMAIL_OUTREACH",
        action_tier="EXTERNAL_OUTREACH",
        context_fingerprint="fp1",
        requested_by_user_id="user1",
        status="APPROVED",
        channel="EMAIL",
        version=1,
        expires_at=t0 + timedelta(hours=1),
    )
    db_session.add(approval)
    await db_session.flush()

    dispatch_repo = SupervisorApprovalDispatchRepository()
    approval_repo = SupervisorApprovalRepository()
    service = SupervisorApprovalDispatchService(repository=dispatch_repo, approval_repository=approval_repo)

    outbox = await dispatch_repo.create_dispatch(
        db_session,
        approval_id=approval.id,
        approval_reference_id=approval.id,
        lead_id=lead.id,
        lead_reference_id=lead.id,
        action_id=approval.action_id,
        channel="EMAIL",
        recipient="test@internal-test.leadintel.example.com",
        context_fingerprint="fp1",
        idempotency_key="guard_test_key_2",
        payload_hash="hash_guard_2",
        payload_json={"canary_flight_id": "malformed-flight-uuid"},
        provider_name="RESEND",
        now=t0,
    )
    outbox.worker_id = "guard-worker"
    outbox.status = SupervisorApprovalDispatchStatus.IN_PROGRESS.value
    outbox.lease_expires_at = t0 + timedelta(minutes=5)
    await db_session.flush()

    with pytest.raises(CanaryAuthorizationError) as err:
        await service.pre_flight_guard_check(
            db_session,
            dispatch_id=outbox.id,
            worker_id="guard-worker",
            expected_lease_generation=1,
            now=t0,
        )
    assert "malformed canary_flight_id" in str(err.value)
    assert outbox.status == SupervisorApprovalDispatchStatus.ABORTED.value
    assert outbox.error_code == "CANARY_INVALID_FLIGHT_ID"


# ---------------------------------------------------------------------------
# PHASE 12J - D2 - GATE D-D4.4 TESTS (Findings D4.3-F03 and D4.3-F04)
# ---------------------------------------------------------------------------

def test_canary_scope_fingerprint_sender_and_channel_sensitivity():
    """Test 21: Scope fingerprint calculation is sensitive to sender identity and channel."""
    t0 = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 28, 14, 0, 0, tzinfo=timezone.utc)

    # Base fingerprint
    fp_base = compute_canary_scope_fingerprint(
        flight_name="Flight Scope Test",
        environment="staging",
        provider_name="RESEND",
        allowed_domains=["internal-test.leadintel.example.com"],
        max_messages=5,
        max_concurrency=1,
        rate_limit_per_second=0.2,
        valid_from=t0,
        expires_at=t1,
        channel="EMAIL",
        sender_identity="outreach@internal-test.leadintel.example.com",
    )

    # Different sender identity -> must produce DIFFERENT fingerprint
    fp_diff_sender = compute_canary_scope_fingerprint(
        flight_name="Flight Scope Test",
        environment="staging",
        provider_name="RESEND",
        allowed_domains=["internal-test.leadintel.example.com"],
        max_messages=5,
        max_concurrency=1,
        rate_limit_per_second=0.2,
        valid_from=t0,
        expires_at=t1,
        channel="EMAIL",
        sender_identity="other@internal-test.leadintel.example.com",
    )
    assert fp_base != fp_diff_sender

    # None sender identity -> must produce DIFFERENT fingerprint
    fp_no_sender = compute_canary_scope_fingerprint(
        flight_name="Flight Scope Test",
        environment="staging",
        provider_name="RESEND",
        allowed_domains=["internal-test.leadintel.example.com"],
        max_messages=5,
        max_concurrency=1,
        rate_limit_per_second=0.2,
        valid_from=t0,
        expires_at=t1,
        channel="EMAIL",
        sender_identity=None,
    )
    assert fp_base != fp_no_sender

    # Different channel -> must produce DIFFERENT fingerprint
    fp_diff_channel = compute_canary_scope_fingerprint(
        flight_name="Flight Scope Test",
        environment="staging",
        provider_name="RESEND",
        allowed_domains=["internal-test.leadintel.example.com"],
        max_messages=5,
        max_concurrency=1,
        rate_limit_per_second=0.2,
        valid_from=t0,
        expires_at=t1,
        channel="SMS",
        sender_identity="outreach@internal-test.leadintel.example.com",
    )
    assert fp_base != fp_diff_channel

    # Normalization (casing, whitespace) -> must produce IDENTICAL fingerprint
    fp_normalized = compute_canary_scope_fingerprint(
        flight_name="Flight Scope Test",
        environment="staging",
        provider_name="RESEND",
        allowed_domains=["internal-test.leadintel.example.com"],
        max_messages=5,
        max_concurrency=1,
        rate_limit_per_second=0.2,
        valid_from=t0,
        expires_at=t1,
        channel=" email  ",
        sender_identity="  OUTREACH@internal-test.leadintel.example.com  ",
    )
    assert fp_base == fp_normalized


@pytest.mark.asyncio
async def test_canary_flight_creation_with_sender_and_channel(db_session: AsyncSession):
    """Test 22: CanaryFlightAuthorization persists normalized channel and sender identity."""
    t0 = datetime.now(timezone.utc)
    t1 = t0 + timedelta(hours=2)

    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Flight Scope Persistence",
        environment="staging",
        provider_name="RESEND",
        allowed_domains=["internal-test.leadintel.example.com"],
        max_messages=5,
        max_concurrency=1,
        rate_limit_per_second=0.2,
        valid_from=t0,
        expires_at=t1,
        channel="email",
        sender_identity=" Outreach@internal-test.leadintel.example.com ",
    )
    assert flight.channel == "EMAIL"
    assert flight.sender_identity == "outreach@internal-test.leadintel.example.com"
    assert flight.scope_fingerprint is not None


@pytest.mark.asyncio
async def test_canary_reservation_fails_on_provider_mismatch(db_session: AsyncSession):
    """Test 23: Preflight slot reservation rejects provider mismatch (CANARY_PROVIDER_MISMATCH)."""
    t0 = datetime.now(timezone.utc)
    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Flight Provider Mismatch",
        allowed_domains=["internal-test.leadintel.example.com"],
        provider_name="RESEND",
        valid_from=t0,
        expires_at=t0 + timedelta(hours=2),
        channel="EMAIL",
        sender_identity="outreach@internal-test.leadintel.example.com",
    )
    # Dual authorize
    await CanaryAuthorizationService.submit_flight_approval(db_session, flight_id=flight.id, signer_user_id="user1", signer_role="eng_lead")
    await CanaryAuthorizationService.submit_flight_approval(db_session, flight_id=flight.id, signer_user_id="user2", signer_role="sec_lead")

    with pytest.raises(CanaryAuthorizationError) as exc_info:
        await CanaryAuthorizationService.reserve_dispatch_slot(
            db_session,
            flight_id=flight.id,
            recipient="test@internal-test.leadintel.example.com",
            channel="EMAIL",
            provider_name="SENDGRID",  # Mismatched provider
            sender_identity="outreach@internal-test.leadintel.example.com",
            now=t0,
        )
    assert exc_info.value.code == "CANARY_PROVIDER_MISMATCH"
    assert "Provider mismatch" in str(exc_info.value)


@pytest.mark.asyncio
async def test_canary_reservation_fails_on_channel_mismatch(db_session: AsyncSession):
    """Test 24: Preflight slot reservation rejects channel mismatch (CANARY_CHANNEL_MISMATCH)."""
    t0 = datetime.now(timezone.utc)
    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Flight Channel Mismatch",
        allowed_domains=["internal-test.leadintel.example.com"],
        provider_name="RESEND",
        valid_from=t0,
        expires_at=t0 + timedelta(hours=2),
        channel="EMAIL",
        sender_identity="outreach@internal-test.leadintel.example.com",
    )
    await CanaryAuthorizationService.submit_flight_approval(db_session, flight_id=flight.id, signer_user_id="user1", signer_role="eng_lead")
    await CanaryAuthorizationService.submit_flight_approval(db_session, flight_id=flight.id, signer_user_id="user2", signer_role="sec_lead")

    with pytest.raises(CanaryAuthorizationError) as exc_info:
        await CanaryAuthorizationService.reserve_dispatch_slot(
            db_session,
            flight_id=flight.id,
            recipient="test@internal-test.leadintel.example.com",
            channel="SMS",  # Mismatched channel
            provider_name="RESEND",
            sender_identity="outreach@internal-test.leadintel.example.com",
            now=t0,
        )
    assert exc_info.value.code == "CANARY_CHANNEL_MISMATCH"
    assert "Channel mismatch" in str(exc_info.value)


@pytest.mark.asyncio
async def test_canary_reservation_fails_on_sender_mismatch(db_session: AsyncSession):
    """Test 25: Preflight slot reservation rejects sender mismatch (CANARY_SENDER_MISMATCH)."""
    t0 = datetime.now(timezone.utc)
    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Flight Sender Mismatch",
        allowed_domains=["internal-test.leadintel.example.com"],
        provider_name="RESEND",
        valid_from=t0,
        expires_at=t0 + timedelta(hours=2),
        channel="EMAIL",
        sender_identity="outreach@internal-test.leadintel.example.com",
    )
    await CanaryAuthorizationService.submit_flight_approval(db_session, flight_id=flight.id, signer_user_id="user1", signer_role="eng_lead")
    await CanaryAuthorizationService.submit_flight_approval(db_session, flight_id=flight.id, signer_user_id="user2", signer_role="sec_lead")

    with pytest.raises(CanaryAuthorizationError) as exc_info:
        await CanaryAuthorizationService.reserve_dispatch_slot(
            db_session,
            flight_id=flight.id,
            recipient="test@internal-test.leadintel.example.com",
            channel="EMAIL",
            provider_name="RESEND",
            sender_identity="attacker@unauthorized.com",  # Mismatched sender
            now=t0,
        )
    assert exc_info.value.code == "CANARY_SENDER_MISMATCH"
    assert "Sender mismatch" in str(exc_info.value)


@pytest.mark.asyncio
async def test_canary_reservation_fails_on_scope_drift(db_session: AsyncSession):
    """Test 26: Preflight slot reservation detects database scope tampering and aborts with CANARY_SCOPE_DRIFT."""
    t0 = datetime.now(timezone.utc)
    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Flight Tamper Test",
        allowed_domains=["internal-test.leadintel.example.com"],
        provider_name="RESEND",
        valid_from=t0,
        expires_at=t0 + timedelta(hours=2),
        channel="EMAIL",
        sender_identity="outreach@internal-test.leadintel.example.com",
    )
    await CanaryAuthorizationService.submit_flight_approval(db_session, flight_id=flight.id, signer_user_id="user1", signer_role="eng_lead")
    await CanaryAuthorizationService.submit_flight_approval(db_session, flight_id=flight.id, signer_user_id="user2", signer_role="sec_lead")

    # Simulate unauthorized DB mutation: tampering max_messages from 5 to 500 without updating fingerprint
    flight.max_messages = 500
    await db_session.flush()

    with pytest.raises(CanaryScopeDriftError) as exc_info:
        await CanaryAuthorizationService.reserve_dispatch_slot(
            db_session,
            flight_id=flight.id,
            recipient="test@internal-test.leadintel.example.com",
            channel="EMAIL",
            provider_name="RESEND",
            sender_identity="outreach@internal-test.leadintel.example.com",
            now=t0,
        )
    assert exc_info.value.code == "CANARY_SCOPE_DRIFT"
    assert "scope fingerprint mismatch" in str(exc_info.value)


@pytest.mark.asyncio
async def test_preflight_guard_check_fails_closed_on_unknown_or_missing_provider(db_session: AsyncSession, monkeypatch):
    """Test 27: Pre-flight guard check aborts job on missing or unknown provider (Finding D4.3-F04)."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)

    t0 = datetime.now(timezone.utc)
    lead = Lead(id=uuid.uuid4(), company_name="Test Company", email="lead1@internal-test.leadintel.example.com")
    db_session.add(lead)
    approval = SupervisorHumanApproval(
        id=uuid.uuid4(),
        action_id="action_prov_test",
        lead_id=lead.id,
        opportunity_id=uuid.uuid4(),
        recommended_action="EMAIL_OUTREACH",
        action_tier="EXTERNAL_OUTREACH",
        context_fingerprint="fp1",
        requested_by_user_id="user1",
        status="APPROVED",
        channel="EMAIL",
        version=1,
        expires_at=t0 + timedelta(hours=1),
    )
    db_session.add(approval)
    await db_session.flush()

    dispatch_repo = SupervisorApprovalDispatchRepository()
    approval_repo = SupervisorApprovalRepository()
    service = SupervisorApprovalDispatchService(repository=dispatch_repo, approval_repository=approval_repo)

    # 1. Unknown provider
    outbox_unknown = await dispatch_repo.create_dispatch(
        db_session,
        approval_id=approval.id,
        approval_reference_id=approval.id,
        lead_id=lead.id,
        lead_reference_id=lead.id,
        action_id=approval.action_id,
        channel="EMAIL",
        recipient="test@internal-test.leadintel.example.com",
        context_fingerprint="fp1",
        idempotency_key="prov_test_key_1",
        payload_hash="hash_1",
        payload_json={"message": "hello"},
        provider_name="ROGUE_SMTP",
        now=t0,
    )
    outbox_unknown.worker_id = "guard-worker"
    outbox_unknown.status = SupervisorApprovalDispatchStatus.IN_PROGRESS.value
    outbox_unknown.lease_expires_at = t0 + timedelta(minutes=5)
    await db_session.flush()

    with pytest.raises(DispatchConfigurationError) as err1:
        await service.pre_flight_guard_check(
            db_session,
            dispatch_id=outbox_unknown.id,
            worker_id="guard-worker",
            expected_lease_generation=1,
            now=t0,
        )
    assert "unknown provider 'ROGUE_SMTP'" in str(err1.value)
    assert outbox_unknown.status == SupervisorApprovalDispatchStatus.ABORTED.value
    assert outbox_unknown.error_code == "DISPATCH_CONFIGURATION_ERROR"

    # 2. Empty provider
    outbox_empty = await dispatch_repo.create_dispatch(
        db_session,
        approval_id=approval.id,
        approval_reference_id=approval.id,
        lead_id=lead.id,
        lead_reference_id=lead.id,
        action_id=approval.action_id,
        channel="EMAIL",
        recipient="test@internal-test.leadintel.example.com",
        context_fingerprint="fp1",
        idempotency_key="prov_test_key_2",
        payload_hash="hash_2",
        payload_json={"message": "hello"},
        provider_name="",
        now=t0,
    )
    outbox_empty.worker_id = "guard-worker"
    outbox_empty.status = SupervisorApprovalDispatchStatus.IN_PROGRESS.value
    outbox_empty.lease_expires_at = t0 + timedelta(minutes=5)
    await db_session.flush()

    with pytest.raises(DispatchConfigurationError) as err2:
        await service.pre_flight_guard_check(
            db_session,
            dispatch_id=outbox_empty.id,
            worker_id="guard-worker",
            expected_lease_generation=1,
            now=t0,
        )
    assert "provider_name is missing" in str(err2.value)
    assert outbox_empty.status == SupervisorApprovalDispatchStatus.ABORTED.value
    assert outbox_empty.error_code == "DISPATCH_CONFIGURATION_ERROR"


@pytest.mark.asyncio
async def test_preflight_guard_check_aborts_on_canary_sender_mismatch(db_session: AsyncSession, monkeypatch):
    """Test 28: Pre-flight guard check aborts canary dispatch if configured sender mismatches authorized flight sender."""
    monkeypatch.setattr(settings, "outreach_dispatch_enabled", True)
    monkeypatch.setattr(settings, "outreach_email_enabled", True)
    monkeypatch.setattr(settings, "outreach_canary_mode", True)
    monkeypatch.setattr(settings, "outreach_canary_allowed_domains", "internal-test.leadintel.example.com")
    monkeypatch.setattr(settings, "outreach_resend_from", "configured@internal-test.leadintel.example.com")

    t0 = datetime.now(timezone.utc)
    lead = Lead(id=uuid.uuid4(), company_name="Test Company", email="lead1@internal-test.leadintel.example.com")
    db_session.add(lead)
    approval = SupervisorHumanApproval(
        id=uuid.uuid4(),
        action_id="action_canary_sender_mismatch",
        lead_id=lead.id,
        opportunity_id=uuid.uuid4(),
        recommended_action="EMAIL_OUTREACH",
        action_tier="EXTERNAL_OUTREACH",
        context_fingerprint="fp1",
        requested_by_user_id="user1",
        status="APPROVED",
        channel="EMAIL",
        version=1,
        expires_at=t0 + timedelta(hours=1),
    )
    db_session.add(approval)
    await db_session.flush()

    # Create and dual-authorize flight with DIFFERENT authorized sender
    flight = await CanaryAuthorizationService.create_flight_scope(
        db_session,
        flight_name="Flight Strict Sender",
        allowed_domains=["internal-test.leadintel.example.com"],
        provider_name="RESEND",
        valid_from=t0,
        expires_at=t0 + timedelta(hours=2),
        channel="EMAIL",
        sender_identity="different_authorized@internal-test.leadintel.example.com",
    )
    await CanaryAuthorizationService.submit_flight_approval(db_session, flight_id=flight.id, signer_user_id="user1", signer_role="eng_lead")
    await CanaryAuthorizationService.submit_flight_approval(db_session, flight_id=flight.id, signer_user_id="user2", signer_role="sec_lead")

    dispatch_repo = SupervisorApprovalDispatchRepository()
    approval_repo = SupervisorApprovalRepository()
    service = SupervisorApprovalDispatchService(repository=dispatch_repo, approval_repository=approval_repo)

    outbox = await dispatch_repo.create_dispatch(
        db_session,
        approval_id=approval.id,
        approval_reference_id=approval.id,
        lead_id=lead.id,
        lead_reference_id=lead.id,
        action_id=approval.action_id,
        channel="EMAIL",
        recipient="test@internal-test.leadintel.example.com",
        context_fingerprint="fp1",
        idempotency_key="sender_mismatch_key",
        payload_hash="hash_mismatch",
        payload_json={"canary_flight_id": str(flight.id)},
        provider_name="RESEND",
        now=t0,
    )
    outbox.worker_id = "guard-worker"
    outbox.status = SupervisorApprovalDispatchStatus.IN_PROGRESS.value
    outbox.lease_expires_at = t0 + timedelta(minutes=5)
    await db_session.flush()

    with pytest.raises(CanaryAuthorizationError) as err:
        await service.pre_flight_guard_check(
            db_session,
            dispatch_id=outbox.id,
            worker_id="guard-worker",
            expected_lease_generation=1,
            now=t0,
        )
    assert outbox.status == SupervisorApprovalDispatchStatus.ABORTED.value
    assert outbox.error_code == "CANARY_SENDER_MISMATCH"
    assert "Sender mismatch" in str(err.value)


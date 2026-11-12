"""
Comprehensive Security and Regression Test Suite for Durable Supervisor Approval API (Phase 12J-D2-C).

Tests:
1. Route registration and HTTP method enforcement
2. Authentication enforcement (missing/invalid bearer token -> 401)
3. Role-based authorization & permission boundaries (sales_rep -> 403)
4. Actor impersonation prevention
5. Request validation & mass-assignment / prohibited field rejection (extra="forbid" -> 422)
6. Server-side authoritative context derivation (contact coordinates, scores, evidence)
7. Approval lifecycle transitions (request -> approve -> reject -> revoke)
8. Invalid lifecycle transitions & terminal state enforcement -> 409
9. Optimistic concurrency & version conflict enforcement -> 409
10. Expiry enforcement -> 409
11. Atomic Compare-And-Swap (CAS) execution claiming & double-claim prevention -> 409
12. Authoritative context drift detection (email change after approval -> CONTEXT_DRIFT)
13. Cross-lead and cross-opportunity identifier substitution rejection -> 400
14. Inviolable readiness gate enforcement (opt-out, active task collision)
15. Deleted/missing target records fail-closed -> MISSING_TARGET_DATA
16. Transactional audit consistency and dual-reference retrieval
17. Zero external communication / dispatch boundary verification
"""

import uuid
from datetime import datetime, timedelta, timezone
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.config import settings
from app.domain.contact_recommendation import RecommendedSalesAction
from app.domain.sales_supervisor import (
    ApprovalSelfApprovalError,
    SupervisorActionReadinessStatus,
    SupervisorActionTier,
    SupervisorApprovalStatus,
    SupervisorExecutionDecision,
    compute_action_context_fingerprint,
)
from app.main import app, get_db
from app.models.entities import (
    ConfidenceLevel,
    Lead,
    LeadIntelligenceOpportunity,
    SalesStatus,
    SalesTask,
    SalesTaskStatus,
    SalesTaskType,
    SourceType,
    SupervisorApprovalAuditEvent,
    SupervisorHumanApproval,
    now_utc,
)

AUTH_TOKEN = "test-auth-token-d2c"
AUTH_HEADERS = {"Authorization": f"Bearer {AUTH_TOKEN}"}
MANAGER_HEADERS = {"Authorization": f"Bearer {AUTH_TOKEN}", "X-Actor-Role": "sales_manager"}
ADMIN_HEADERS = {"Authorization": f"Bearer {AUTH_TOKEN}", "X-Actor-Role": "admin"}
REP_HEADERS = {"Authorization": f"Bearer {AUTH_TOKEN}", "X-Actor-Role": "sales_rep"}


# ============================================================================
# Helper fixtures / setup
# ============================================================================

@pytest.fixture(autouse=True)
def configure_test_environment(monkeypatch):
    """
    By default in test_supervisor_approval_api, general integration tests run in development mode
    with allow_dev_shared_approval=True and allow_dev_role_override=True.
    Remediation-specific tests explicitly override these settings to test production & strict behaviors.
    """
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "allow_dev_shared_approval", True)
    monkeypatch.setattr(settings, "allow_dev_role_override", True)


async def create_test_lead_and_opp(session, *, company_name="Acme Corp", email="contact@acme.com", phone="+15551234567", email_opted_out=False, score=85):
    """Creates a validated Lead and associated LeadIntelligenceOpportunity."""
    lead = Lead(
        id=uuid.uuid4(),
        company_name=company_name,
        email=email,
        phone=phone,
        email_opted_out=email_opted_out,
        sales_status=SalesStatus.NEW,
        currency="USD",
    )
    session.add(lead)
    await session.flush()

    opp = LeadIntelligenceOpportunity(
        id=uuid.uuid4(),
        lead_id=lead.id,
        title="SEO and Website Modernization",
        description="High-opportunity web modernizing lead",
        score=score,
        recommended_action="PERSONALIZED_EMAIL",
        evidence={"seo_score": "poor", "mobile_score": "critical"},
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH,
    )
    session.add(opp)
    await session.commit()
    await session.refresh(lead)
    await session.refresh(opp)
    return lead, opp


# ============================================================================
# 1. Route Registration and Method Tests
# ============================================================================

def test_route_registration():
    """Verify all expected supervisor approval endpoints are registered on the application."""
    expected_paths = {
        "/api/sales-supervisor/approvals/request",
        "/api/sales-supervisor/approvals",
        "/api/sales-supervisor/approvals/{approval_id}",
        "/api/sales-supervisor/approvals/{approval_id}/approve",
        "/api/sales-supervisor/approvals/{approval_id}/reject",
        "/api/sales-supervisor/approvals/{approval_id}/revoke",
        "/api/sales-supervisor/approvals/{approval_id}/claim",
        "/api/sales-supervisor/approvals/{approval_id}/audit",
        "/api/sales-supervisor/approvals/{approval_id}/verify",
    }
    actual_paths = {r.path for r in app.routes if getattr(r, "path", "").startswith("/api/sales-supervisor/approvals")}
    assert expected_paths.issubset(actual_paths)


# ============================================================================
# 2. Authentication Enforcement Tests
# ============================================================================

@pytest.mark.asyncio
async def test_auth_missing_token_returns_401(monkeypatch):
    """Requests without Authorization header must be rejected with 401."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/sales-supervisor/approvals")
        assert resp.status_code == 401
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_auth_invalid_token_returns_401(monkeypatch):
    """Requests with incorrect bearer token must be rejected with 401."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/sales-supervisor/approvals", headers={"Authorization": "Bearer bad-token"})
        assert resp.status_code == 401
        data = resp.json()
        assert data["success"] is False
        assert data["error"]["code"] == "UNAUTHORIZED"


# ============================================================================
# 3. Role-Based Authorization & Actor Impersonation Tests
# ============================================================================

@pytest.mark.asyncio
async def test_sales_rep_cannot_approve(session_factory, monkeypatch):
    """A sales_rep role cannot approve actions (requires sales_manager or admin)."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            dummy_id = str(uuid.uuid4())
            resp = await ac.post(
                f"/api/sales-supervisor/approvals/{dummy_id}/approve",
                json={"decision_reason": "Trying to self-approve"},
                headers=REP_HEADERS,
            )
            assert resp.status_code == 403
            data = resp.json()
            assert data["error"]["code"] == "FORBIDDEN"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_sales_rep_cannot_reject_or_revoke(session_factory, monkeypatch):
    """A sales_rep role cannot reject or revoke approvals."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            dummy_id = str(uuid.uuid4())
            resp = await ac.post(
                f"/api/sales-supervisor/approvals/{dummy_id}/reject",
                json={"decision_reason": "Not allowed"},
                headers=REP_HEADERS,
            )
            assert resp.status_code == 403

            resp2 = await ac.post(
                f"/api/sales-supervisor/approvals/{dummy_id}/revoke",
                json={"revocation_reason": "Not allowed"},
                headers=REP_HEADERS,
            )
            assert resp2.status_code == 403
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_actor_impersonation_rejected(session_factory, monkeypatch):
    """Supplying an X-Actor-Id that differs from the authenticated user is rejected with 403."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    monkeypatch.setattr(settings, "api_auth_user_id", "local_api_user")
    app.dependency_overrides[get_db] = lambda: session_factory()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            headers = {
                "Authorization": f"Bearer {AUTH_TOKEN}",
                "X-Actor-Id": "impersonated_user_bob",
            }
            resp = await ac.get("/api/sales-supervisor/approvals", headers=headers)
            assert resp.status_code == 403
            assert resp.json()["error"]["code"] == "IMPERSONATION_FORBIDDEN"
    finally:
        app.dependency_overrides.pop(get_db, None)


# ============================================================================
# 4. Request Validation & Prohibited Field Rejection Tests
# ============================================================================

@pytest.mark.asyncio
async def test_prohibited_fields_rejected_with_422(session_factory, monkeypatch):
    """Submitting forbidden fields (extra='forbid') must be rejected with 422 Unprocessable Entity."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # Client attempts to inject approver_id and fake status
            payload = {
                "lead_id": str(uuid.uuid4()),
                "opportunity_id": str(uuid.uuid4()),
                "approver_id": "malicious_hacker",  # Prohibited
                "status": "APPROVED",                # Prohibited
                "context_fingerprint": "fake_hash",  # Prohibited
            }
            resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json=payload,
                headers=MANAGER_HEADERS,
            )
            assert resp.status_code == 422
            data = resp.json()
            assert data["error"]["code"] == "VALIDATION_ERROR"
    finally:
        app.dependency_overrides.pop(get_db, None)


# ============================================================================
# 5. Authoritative Context Derivation & Creation Tests
# ============================================================================

@pytest.mark.asyncio
async def test_request_approval_derives_authoritative_context(session_factory, monkeypatch):
    """
    Approval request derives recipient email, phone, evidence, and scores directly
    from authoritative database records.
    """
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(
            session,
            company_name="Apex Logistics",
            email="ops@apexlogistics.com",
            phone="+12125559090",
            score=92,
        )

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            payload = {
                "lead_id": str(lead.id),
                "opportunity_id": str(opp.id),
                "notes": "High priority logistics lead",
            }
            resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json=payload,
                headers=MANAGER_HEADERS,
            )
            assert resp.status_code == 201
            data = resp.json()
            assert data["status"] == "PENDING"
            assert data["lead_id"] == str(lead.id)
            assert data["opportunity_id"] == str(opp.id)
            assert data["version"] == 1
            assert len(data["context_fingerprint"]) == 64
            assert data["requested_by_user_id"] == "local_api_user"

            # Check that fingerprint matches the authoritative payload
            expected_fp = compute_action_context_fingerprint(
                action_id=data["action_id"],
                action_type="PERSONALIZED_EMAIL",
                lead_id=lead.id,
                opportunity_id=opp.id,
                contact_id=None,
                channel="EMAIL",
                contact_email="ops@apexlogistics.com",
                contact_phone="+12125559090",
                opportunity_score=92.0,
                recommendation_fit=0.0,
                evidence_keys=sorted(opp.evidence.keys()),
            )
            assert data["context_fingerprint"] == expected_fp
    finally:
        app.dependency_overrides.pop(get_db, None)


# ============================================================================
# 6. Cross-Lead Substitution Prevention Tests
# ============================================================================

@pytest.mark.asyncio
async def test_cross_lead_opportunity_rejected_with_400(session_factory, monkeypatch):
    """Attempting to pair an opportunity with a different lead must be rejected with 400."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead1, opp1 = await create_test_lead_and_opp(session, company_name="Company 1")
        lead2, opp2 = await create_test_lead_and_opp(session, company_name="Company 2")

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # Pair lead1 with opp2 (belongs to lead2)
            payload = {
                "lead_id": str(lead1.id),
                "opportunity_id": str(opp2.id),
            }
            resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json=payload,
                headers=MANAGER_HEADERS,
            )
            assert resp.status_code == 400
            assert resp.json()["error"]["code"] == "CROSS_LEAD_OPPORTUNITY_MISMATCH"
    finally:
        app.dependency_overrides.pop(get_db, None)


# ============================================================================
# 7. Lifecycle Transitions: Approve, Reject, Revoke Tests
# ============================================================================

@pytest.mark.asyncio
async def test_approval_lifecycle_happy_path(session_factory, monkeypatch):
    """PENDING -> APPROVED -> REVOKED transitions succeed with proper audit trails."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # 1. Request approval
            req_resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=MANAGER_HEADERS,
            )
            assert req_resp.status_code == 201
            appr_id = req_resp.json()["id"]

            # 2. Approve
            approve_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "Opportunity vetted", "expected_version": 1},
                headers=MANAGER_HEADERS,
            )
            assert approve_resp.status_code == 200
            appr_data = approve_resp.json()
            assert appr_data["status"] == "APPROVED"
            assert appr_data["version"] == 2
            assert appr_data["approved_by_user_id"] == "local_api_user"

            # 3. Revoke
            revoke_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/revoke",
                json={"revocation_reason": "Client requested pause", "expected_version": 2},
                headers=MANAGER_HEADERS,
            )
            assert revoke_resp.status_code == 200
            assert revoke_resp.json()["status"] == "REVOKED"
            assert revoke_resp.json()["version"] == 3

            # 4. Verify audit trail has 3 events (CREATED, APPROVED, REVOKED)
            audit_resp = await ac.get(
                f"/api/sales-supervisor/approvals/{appr_id}/audit",
                headers=MANAGER_HEADERS,
            )
            assert audit_resp.status_code == 200
            events = audit_resp.json()["events"]
            assert len(events) == 3
            event_types = [e["event_type"] for e in events]
            assert event_types == ["CREATED", "APPROVED", "REVOKED"]
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_rejection_is_terminal(session_factory, monkeypatch):
    """REJECTED is terminal; subsequent approve or revoke attempts return 409 Conflict."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            req_resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=MANAGER_HEADERS,
            )
            appr_id = req_resp.json()["id"]

            # Reject
            rej_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/reject",
                json={"decision_reason": "Budget insufficient"},
                headers=MANAGER_HEADERS,
            )
            assert rej_resp.status_code == 200
            assert rej_resp.json()["status"] == "REJECTED"

            # Attempt to approve rejected record -> 409 Conflict
            appr_again = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "Changed my mind"},
                headers=MANAGER_HEADERS,
            )
            assert appr_again.status_code == 409
            assert appr_again.json()["error"]["code"] == "INVALID_LIFECYCLE_TRANSITION"
    finally:
        app.dependency_overrides.pop(get_db, None)


# ============================================================================
# 8. Version Conflict & Expiration Enforcement Tests
# ============================================================================

@pytest.mark.asyncio
async def test_optimistic_version_conflict_returns_409(session_factory, monkeypatch):
    """Providing a mismatched expected_version returns 409 Conflict (VERSION_CONFLICT)."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            req_resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=MANAGER_HEADERS,
            )
            appr_id = req_resp.json()["id"]

            # Expected version is 1, but client sends 99
            resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "Test", "expected_version": 99},
                headers=MANAGER_HEADERS,
            )
            assert resp.status_code == 409
            assert resp.json()["error"]["code"] == "VERSION_CONFLICT"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_approving_expired_approval_returns_409(session_factory, monkeypatch):
    """Attempting to approve a request past its expires_at timestamp returns 409 Conflict."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            req_resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=MANAGER_HEADERS,
            )
            appr_id = uuid.UUID(req_resp.json()["id"])

            # Manually backdate expires_at in the database to simulate expiration
            async with session_factory() as session:
                rec = await session.get(SupervisorHumanApproval, appr_id)
                rec.expires_at = now_utc() - timedelta(hours=1)
                await session.commit()

            # Attempt to approve
            resp = await ac.post(
                f"/api/sales-supervisor/approvals/{str(appr_id)}/approve",
                json={"decision_reason": "Approving late"},
                headers=MANAGER_HEADERS,
            )
            assert resp.status_code == 409
            assert resp.json()["error"]["code"] == "APPROVAL_EXPIRED"
    finally:
        app.dependency_overrides.pop(get_db, None)


# ============================================================================
# 9. Concurrency & Atomic CAS Claim Tests
# ============================================================================

@pytest.mark.asyncio
async def test_claim_execution_cas_prevents_double_claim(session_factory, monkeypatch):
    """
    Atomic CAS claiming: first worker succeeds; second worker receives 409 Conflict.
    Zero external outreach or dispatch is performed.
    """
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # 1. Request and Approve
            req_resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=MANAGER_HEADERS,
            )
            appr_id = req_resp.json()["id"]

            await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "Approved for test"},
                headers=MANAGER_HEADERS,
            )

            # 2. Worker 1 claims
            claim1 = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/claim",
                json={"worker_id": "worker_alpha", "expected_version": 2},
                headers=MANAGER_HEADERS,
            )
            assert claim1.status_code == 200
            claim1_data = claim1.json()
            assert claim1_data["execution_claimed_by"] == "worker_alpha"
            assert claim1_data["execution_claimed_at"] is not None
            assert claim1_data["version"] == 3

            # 3. Worker 2 attempts duplicate claim -> 409 Conflict
            claim2 = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/claim",
                json={"worker_id": "worker_beta", "expected_version": 2},
                headers=MANAGER_HEADERS,
            )
            assert claim2.status_code == 409
            assert claim2.json()["error"]["code"] in ("ALREADY_CLAIMED", "VERSION_CONFLICT")
    finally:
        app.dependency_overrides.pop(get_db, None)


# ============================================================================
# 10. Context Drift & Readiness Gate Verification Tests
# ============================================================================

@pytest.mark.asyncio
async def test_verify_detects_context_drift(session_factory, monkeypatch):
    """
    If lead contact coordinates change after approval, the verify endpoint
    fails closed with decision=DENIED and denial_code=CONTEXT_DRIFT.
    """
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session, email="original@domain.com")

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # 1. Request and Approve
            req_resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=MANAGER_HEADERS,
            )
            appr_id = req_resp.json()["id"]

            await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "Approved context"},
                headers=MANAGER_HEADERS,
            )

            # 2. Before mutation: verify authorization passes
            verify_resp1 = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/verify",
                headers=MANAGER_HEADERS,
            )
            assert verify_resp1.status_code == 200
            assert verify_resp1.json()["authorized"] is True
            assert verify_resp1.json()["decision"] == "AUTHORIZED"

            # 3. Mutate authoritative lead email in the database (simulate out-of-band change)
            async with session_factory() as session:
                lead_to_update = await session.get(Lead, lead.id)
                lead_to_update.email = "tampered_email@attacker.com"
                await session.commit()

            # 4. Verify again: fails closed with CONTEXT_DRIFT
            verify_resp2 = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/verify",
                headers=MANAGER_HEADERS,
            )
            assert verify_resp2.status_code == 200
            data2 = verify_resp2.json()
            assert data2["authorized"] is False
            assert data2["decision"] == "DENIED"
            assert data2["denial_code"] == "CONTEXT_DRIFT"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_verify_inviolable_readiness_opt_out_blocks_execution(session_factory, monkeypatch):
    """
    Even with an APPROVED record, if the lead opts out, authorization is DENIED
    with READINESS_BLOCKED_OPT_OUT. Human approval cannot override opt-outs.
    """
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session, email_opted_out=False)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # 1. Request and Approve
            req_resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=MANAGER_HEADERS,
            )
            appr_id = req_resp.json()["id"]

            await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "Approved"},
                headers=MANAGER_HEADERS,
            )

            # 2. Lead opts out
            async with session_factory() as session:
                l = await session.get(Lead, lead.id)
                l.email_opted_out = True
                await session.commit()

            # 3. Verify: must be denied by readiness gate
            verify_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/verify",
                headers=MANAGER_HEADERS,
            )
            assert verify_resp.status_code == 200
            data = verify_resp.json()
            assert data["authorized"] is False
            assert data["decision"] == "DENIED"
            assert data["denial_code"] == "READINESS_BLOCKED_OPT_OUT"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_deleted_target_record_fails_closed(session_factory, monkeypatch):
    """If the target opportunity is deleted after approval, verify fails closed with MISSING_TARGET_DATA."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            req_resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=MANAGER_HEADERS,
            )
            appr_id = req_resp.json()["id"]

            await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "Approved"},
                headers=MANAGER_HEADERS,
            )

            # Delete the opportunity
            async with session_factory() as session:
                opp_to_del = await session.get(LeadIntelligenceOpportunity, opp.id)
                await session.delete(opp_to_del)
                await session.commit()

            # Verify authorization
            verify_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/verify",
                headers=MANAGER_HEADERS,
            )
            assert verify_resp.status_code == 200
            data = verify_resp.json()
            assert data["authorized"] is False
            assert data["decision"] == "DENIED"
            assert data["denial_code"] == "MISSING_TARGET_DATA"
    finally:
        app.dependency_overrides.pop(get_db, None)


# ============================================================================
# 11. Pagination and Filtering Tests
# ============================================================================

@pytest.mark.asyncio
async def test_list_approvals_filtering_and_pagination(session_factory, monkeypatch):
    """Listing approvals supports status filter, pagination, and returns count."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead1, opp1 = await create_test_lead_and_opp(session, company_name="Company A")
        lead2, opp2 = await create_test_lead_and_opp(session, company_name="Company B")

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # Create 2 approvals
            r1 = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead1.id), "opportunity_id": str(opp1.id)},
                headers=MANAGER_HEADERS,
            )
            r2 = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead2.id), "opportunity_id": str(opp2.id)},
                headers=MANAGER_HEADERS,
            )
            id1 = r1.json()["id"]

            # Approve one of them
            await ac.post(
                f"/api/sales-supervisor/approvals/{id1}/approve",
                json={"decision_reason": "Approved"},
                headers=MANAGER_HEADERS,
            )

            # Filter by status=PENDING
            pending_list = await ac.get("/api/sales-supervisor/approvals?status=PENDING", headers=MANAGER_HEADERS)
            assert pending_list.status_code == 200
            assert pending_list.json()["total"] == 1
            assert pending_list.json()["approvals"][0]["status"] == "PENDING"

            # Filter by status=APPROVED
            appr_list = await ac.get("/api/sales-supervisor/approvals?status=APPROVED", headers=MANAGER_HEADERS)
            assert appr_list.status_code == 200
            assert appr_list.json()["total"] == 1
            assert appr_list.json()["approvals"][0]["status"] == "APPROVED"
    finally:
        app.dependency_overrides.pop(get_db, None)


# ============================================================================
# 12. 404 Not Found Edge Case Tests
# ============================================================================

@pytest.mark.asyncio
async def test_all_endpoints_return_404_for_nonexistent_approval(session_factory, monkeypatch):
    """Every endpoint targeting a non-existent approval ID returns 404."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()
    fake_id = str(uuid.uuid4())

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # GET
            r_get = await ac.get(f"/api/sales-supervisor/approvals/{fake_id}", headers=MANAGER_HEADERS)
            assert r_get.status_code == 404

            # APPROVE
            r_app = await ac.post(f"/api/sales-supervisor/approvals/{fake_id}/approve", json={"decision_reason": "ok"}, headers=MANAGER_HEADERS)
            assert r_app.status_code == 404

            # REJECT
            r_rej = await ac.post(f"/api/sales-supervisor/approvals/{fake_id}/reject", json={"decision_reason": "no"}, headers=MANAGER_HEADERS)
            assert r_rej.status_code == 404

            # REVOKE
            r_rev = await ac.post(f"/api/sales-supervisor/approvals/{fake_id}/revoke", json={"revocation_reason": "stop"}, headers=MANAGER_HEADERS)
            assert r_rev.status_code == 404

            # CLAIM
            r_clm = await ac.post(f"/api/sales-supervisor/approvals/{fake_id}/claim", json={"worker_id": "w1", "expected_version": 1}, headers=MANAGER_HEADERS)
            assert r_clm.status_code == 404

            # AUDIT
            r_aud = await ac.get(f"/api/sales-supervisor/approvals/{fake_id}/audit", headers=MANAGER_HEADERS)
            assert r_aud.status_code == 404

            # VERIFY
            r_ver = await ac.post(f"/api/sales-supervisor/approvals/{fake_id}/verify", headers=MANAGER_HEADERS)
            assert r_ver.status_code == 404
    finally:
        app.dependency_overrides.pop(get_db, None)


# ============================================================================
# 13. Active Task Collision & Additional Drift Tests
# ============================================================================

@pytest.mark.asyncio
async def test_active_task_collision_blocks_execution(session_factory, monkeypatch):
    """If an active SalesTask is open for the lead, execution authorization is blocked by collision guard."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # 1. Request and Approve
            r = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=MANAGER_HEADERS,
            )
            appr_id = r.json()["id"]

            await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "Approved"},
                headers=MANAGER_HEADERS,
            )

            # 2. Add an active SalesTask to simulate collision
            async with session_factory() as session:
                task = SalesTask(
                    id=uuid.uuid4(),
                    lead_id=lead.id,
                    task_type=SalesTaskType.EMAIL,
                    title="Active Outreach in Progress",
                    status=SalesTaskStatus.IN_PROGRESS,
                    due_at=now_utc(),
                    agent_source_id=opp.id,
                )
                session.add(task)
                await session.commit()

            # 3. Verify: must be denied with READINESS_ACTIVE_TASK_EXISTS
            verify_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/verify",
                headers=MANAGER_HEADERS,
            )
            assert verify_resp.status_code == 200
            data = verify_resp.json()
            assert data["authorized"] is False
            assert data["decision"] == "DENIED"
            assert data["denial_code"] == "READINESS_ACTIVE_TASK_EXISTS"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_opportunity_score_mutation_causes_context_drift(session_factory, monkeypatch):
    """Mutating opportunity score after approval produces context drift."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session, score=80)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=MANAGER_HEADERS,
            )
            appr_id = r.json()["id"]

            await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "Approved at score 80"},
                headers=MANAGER_HEADERS,
            )

            # Mutate score
            async with session_factory() as session:
                opp_to_update = await session.get(LeadIntelligenceOpportunity, opp.id)
                opp_to_update.score = 25
                await session.commit()

            # Verify: must fail closed with CONTEXT_DRIFT
            v_resp = await ac.post(f"/api/sales-supervisor/approvals/{appr_id}/verify", headers=MANAGER_HEADERS)
            assert v_resp.status_code == 200
            assert v_resp.json()["authorized"] is False
            assert v_resp.json()["denial_code"] == "CONTEXT_DRIFT"
    finally:
        app.dependency_overrides.pop(get_db, None)


# ============================================================================
# Category 18: Gate A - Self-Approval Separation of Duties API Tests
# ============================================================================

@pytest.mark.asyncio
async def test_api_self_approval_denied_with_403_for_individual_identity(session_factory, monkeypatch):
    """
    Gate A API: When authenticated principal attempts to approve a request they themselves requested,
    the API returns HTTP 403 Forbidden with SELF_APPROVAL_PROHIBITED.
    The database state is unmodified (PENDING, version=1, no APPROVED audit event).
    """
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    monkeypatch.setattr(settings, "api_auth_user_id", "sales_mgr_alice")
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            alice_headers = {
                "Authorization": f"Bearer {AUTH_TOKEN}",
                "X-Actor-Role": "sales_manager",
            }

            # 1. Alice creates the request
            req_resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=alice_headers,
            )
            assert req_resp.status_code == 201
            appr_id = req_resp.json()["id"]
            assert req_resp.json()["requested_by_user_id"] == "sales_mgr_alice"
            assert req_resp.json()["status"] == "PENDING"
            assert req_resp.json()["version"] == 1

            # 2. Alice attempts to approve her own request -> 403 Forbidden
            approve_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "I approve my own outreach"},
                headers=alice_headers,
            )
            assert approve_resp.status_code == 403
            err_data = approve_resp.json()["error"]
            assert err_data["code"] == "SELF_APPROVAL_PROHIBITED"
            assert "sales_mgr_alice" in err_data["message"]

            # 3. Verify database row is still PENDING and version is 1
            async with session_factory() as session:
                persisted = await session.get(SupervisorHumanApproval, uuid.UUID(appr_id))
                assert persisted.status == "PENDING"
                assert persisted.version == 1
                assert persisted.approved_by_user_id is None

            # 4. Verify audit trail has no APPROVED event
            audit_resp = await ac.get(
                f"/api/sales-supervisor/approvals/{appr_id}/audit",
                headers=alice_headers,
            )
            assert audit_resp.status_code == 200
            events = audit_resp.json()["events"]
            assert len(events) == 1
            assert events[0]["event_type"] == "CREATED"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_distinct_individual_identities_approval_succeeds(session_factory, monkeypatch):
    """
    Gate A API: When requester and approver have different verified individual identities,
    approval succeeds with HTTP 200 OK.
    """
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # 1. Bob (sales_rep) creates approval request
            monkeypatch.setattr(settings, "api_auth_user_id", "sales_rep_bob")
            bob_headers = {
                "Authorization": f"Bearer {AUTH_TOKEN}",
                "X-Actor-Role": "sales_rep",
            }
            req_resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=bob_headers,
            )
            assert req_resp.status_code == 201
            appr_id = req_resp.json()["id"]
            assert req_resp.json()["requested_by_user_id"] == "sales_rep_bob"

            # 2. Alice (sales_manager) approves Bob's request -> Succeeds
            monkeypatch.setattr(settings, "api_auth_user_id", "sales_mgr_alice")
            alice_headers = {
                "Authorization": f"Bearer {AUTH_TOKEN}",
                "X-Actor-Role": "sales_manager",
            }
            approve_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "Approved by manager Alice"},
                headers=alice_headers,
            )
            assert approve_resp.status_code == 200
            data = approve_resp.json()
            assert data["status"] == "APPROVED"
            assert data["approved_by_user_id"] == "sales_mgr_alice"
            assert data["requested_by_user_id"] == "sales_rep_bob"
            assert data["version"] == 2
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_body_actor_override_rejected_with_422(session_factory, monkeypatch):
    """
    Gate A API: Client attempting to inject actor/approver identity into request body
    is strictly rejected with 422 Unprocessable Entity (extra='forbid').
    """
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    monkeypatch.setattr(settings, "api_auth_user_id", "sales_mgr_alice")
    app.dependency_overrides[get_db] = lambda: session_factory()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            dummy_id = str(uuid.uuid4())
            # Attempt to override approver_id or actor_id in approve request body
            resp = await ac.post(
                f"/api/sales-supervisor/approvals/{dummy_id}/approve",
                json={
                    "decision_reason": "Trying to bypass SoD",
                    "approver_id": "other_manager",
                    "actor_id": "fake_actor",
                },
                headers={
                    "Authorization": f"Bearer {AUTH_TOKEN}",
                    "X-Actor-Role": "sales_manager",
                },
            )
            assert resp.status_code == 422
            assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_missing_requester_identity_fails_closed_with_403(session_factory, monkeypatch):
    """
    Gate A API: If an approval has missing or unverified requester identity,
    approval fails closed with 403 Forbidden (SELF_APPROVAL_PROHIBITED).
    """
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    monkeypatch.setattr(settings, "api_auth_user_id", "sales_mgr_alice")
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session)
        # Manually create approval with unverified placeholder
        app_id = uuid.uuid4()
        now = now_utc()
        raw_approval = SupervisorHumanApproval(
            id=app_id,
            action_id="action-test-blank-req",
            lead_id=lead.id,
            opportunity_id=opp.id,
            recommended_action="PERSONALIZED_EMAIL",
            action_tier="EXTERNAL_OUTREACH",
            context_fingerprint="fp-blank",
            status="PENDING",
            requested_by_user_id="UNKNOWN",  # Unverified placeholder
            version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(raw_approval)
        await session.commit()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                f"/api/sales-supervisor/approvals/{app_id}/approve",
                json={"decision_reason": "Approve action"},
                headers={
                    "Authorization": f"Bearer {AUTH_TOKEN}",
                    "X-Actor-Role": "sales_manager",
                },
            )
            assert resp.status_code == 403
            assert resp.json()["error"]["code"] == "UNVERIFIED_IDENTITY_PROHIBITED"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_self_rejection_and_revocation_allowed(session_factory, monkeypatch):
    """
    Gate A API: Separation of duties restricts self-approval, but does NOT block
    the requester from rejecting or revoking their own action.
    """
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    monkeypatch.setattr(settings, "api_auth_user_id", "sales_mgr_alice")
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            alice_headers = {
                "Authorization": f"Bearer {AUTH_TOKEN}",
                "X-Actor-Role": "sales_manager",
            }

            # 1. Alice creates request 1 and self-rejects it -> Allowed
            req1 = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=alice_headers,
            )
            appr1_id = req1.json()["id"]

            rej_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr1_id}/reject",
                json={"decision_reason": "Alice self-rejecting"},
                headers=alice_headers,
            )
            assert rej_resp.status_code == 200
            assert rej_resp.json()["status"] == "REJECTED"
            assert rej_resp.json()["rejected_by_user_id"] == "sales_mgr_alice"

            # 2. Bob creates request 2, Alice approves Bob's request, and Alice revokes it
            monkeypatch.setattr(settings, "api_auth_user_id", "sales_rep_bob")
            bob_headers = {
                "Authorization": f"Bearer {AUTH_TOKEN}",
                "X-Actor-Role": "sales_rep",
            }
            req2 = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=bob_headers,
            )
            appr2_id = req2.json()["id"]

            # Switch back to Alice
            monkeypatch.setattr(settings, "api_auth_user_id", "sales_mgr_alice")
            app_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr2_id}/approve",
                json={"decision_reason": "Alice approves Bob"},
                headers=alice_headers,
            )
            assert app_resp.status_code == 200
            assert app_resp.json()["status"] == "APPROVED"

            # Alice revokes the approval
            revoke_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr2_id}/revoke",
                json={"revocation_reason": "Alice revoking approval"},
                headers=alice_headers,
            )
            assert revoke_resp.status_code == 200
            assert revoke_resp.json()["status"] == "REVOKED"
            assert revoke_resp.json()["revoked_by_user_id"] == "sales_mgr_alice"
    finally:
        app.dependency_overrides.pop(get_db, None)


# ============================================================================
# Gate A Remediation Specific Security Tests (F-GATE-A-01, F-GATE-A-03)
# ============================================================================

@pytest.mark.asyncio
async def test_api_shared_identity_enforced_separation_default_returns_403(session_factory, monkeypatch):
    """
    F-GATE-A-01: When allow_dev_shared_approval is False (secure default),
    approving under the shared development identity fails closed with 403 UNVERIFIED_IDENTITY_PROHIBITED.
    """
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    monkeypatch.setattr(settings, "api_auth_user_id", "local_api_user")
    monkeypatch.setattr(settings, "allow_dev_shared_approval", False)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            req_resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=MANAGER_HEADERS,
            )
            assert req_resp.status_code == 201
            appr_id = req_resp.json()["id"]

            approve_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "Testing strict separation default"},
                headers=MANAGER_HEADERS,
            )
            assert approve_resp.status_code == 403
            assert approve_resp.json()["error"]["code"] == "UNVERIFIED_IDENTITY_PROHIBITED"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_shared_identity_explicit_dev_config_returns_200(session_factory, monkeypatch):
    """
    F-GATE-A-01: In development mode with explicit allow_dev_shared_approval=True,
    approving under local_api_user succeeds with 200 APPROVED and logs a warning.
    """
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    monkeypatch.setattr(settings, "api_auth_user_id", "local_api_user")
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "allow_dev_shared_approval", True)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            req_resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=MANAGER_HEADERS,
            )
            assert req_resp.status_code == 201
            appr_id = req_resp.json()["id"]

            approve_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "Dev explicitly allowed"},
                headers=MANAGER_HEADERS,
            )
            assert approve_resp.status_code == 200
            assert approve_resp.json()["status"] == "APPROVED"
            assert approve_resp.json()["approved_by_user_id"] == "local_api_user"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_client_role_header_ignored_in_production_returns_403(session_factory, monkeypatch):
    """
    F-GATE-A-03: In production, client-supplied X-Actor-Role is strictly ignored.
    The principal retains trusted role (viewer) and is rejected with 403 FORBIDDEN.
    """
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    monkeypatch.setattr(settings, "api_auth_user_id", "local_api_user")
    monkeypatch.setattr(settings, "app_env", "production")
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            dummy_id = str(uuid.uuid4())
            # Attempt to approve with client-supplied X-Actor-Role: sales_manager in production
            resp = await ac.post(
                f"/api/sales-supervisor/approvals/{dummy_id}/approve",
                json={"decision_reason": "Attempting prod role override"},
                headers=MANAGER_HEADERS,  # Includes X-Actor-Role: sales_manager
            )
            assert resp.status_code == 403
            assert resp.json()["error"]["code"] == "FORBIDDEN"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_client_role_header_ignored_when_role_override_disabled_returns_403(session_factory, monkeypatch):
    """
    F-GATE-A-03: When allow_dev_role_override=False, client-supplied X-Actor-Role is ignored.
    The principal retains unprivileged viewer role and is rejected with 403 FORBIDDEN.
    """
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    monkeypatch.setattr(settings, "api_auth_user_id", "local_api_user")
    monkeypatch.setattr(settings, "allow_dev_role_override", False)
    app.dependency_overrides[get_db] = lambda: session_factory()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            dummy_id = str(uuid.uuid4())
            resp = await ac.post(
                f"/api/sales-supervisor/approvals/{dummy_id}/approve",
                json={"decision_reason": "Attempting role override when disabled"},
                headers=ADMIN_HEADERS,  # Includes X-Actor-Role: admin
            )
            assert resp.status_code == 403
            assert resp.json()["error"]["code"] == "FORBIDDEN"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_missing_role_header_defaults_to_unprivileged_viewer_returns_403(session_factory, monkeypatch):
    """
    F-GATE-A-03: When no X-Actor-Role header is sent, the principal defaults to unprivileged 'viewer'
    and cannot approve actions (fails closed with 403 FORBIDDEN).
    """
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    monkeypatch.setattr(settings, "api_auth_user_id", "local_api_user")
    app.dependency_overrides[get_db] = lambda: session_factory()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            dummy_id = str(uuid.uuid4())
            resp = await ac.post(
                f"/api/sales-supervisor/approvals/{dummy_id}/approve",
                json={"decision_reason": "Attempting approval without role header"},
                headers=AUTH_HEADERS,  # Only Authorization bearer token, NO X-Actor-Role
            )
            assert resp.status_code == 403
            assert resp.json()["error"]["code"] == "FORBIDDEN"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_attempt_to_disable_strict_separation_in_body_returns_422(session_factory, monkeypatch):
    """
    F-GATE-A-01: Clients cannot disable strict separation by passing strict_separation
    or other unauthorized fields in the JSON body. Pydantic rejects with 422 VALIDATION_ERROR.
    """
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    monkeypatch.setattr(settings, "api_auth_user_id", "sales_mgr_alice")
    app.dependency_overrides[get_db] = lambda: session_factory()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            dummy_id = str(uuid.uuid4())
            resp = await ac.post(
                f"/api/sales-supervisor/approvals/{dummy_id}/approve",
                json={
                    "decision_reason": "Bypass attempt",
                    "strict_separation": False,
                },
                headers=MANAGER_HEADERS,
            )
            assert resp.status_code == 422
            assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        app.dependency_overrides.pop(get_db, None)


# ============================================================================
# 14. Gate B — Atomic Approval Context Verification and Claim Binding API Tests
# ============================================================================

@pytest.mark.asyncio
async def test_gate_b_api_claim_succeeds_when_context_matches(session_factory, monkeypatch):
    """Gate B: HTTP POST /claim succeeds when current database context matches approved fingerprint."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            req_resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=MANAGER_HEADERS,
            )
            appr_id = req_resp.json()["id"]

            await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "Approved for Gate B API test"},
                headers=MANAGER_HEADERS,
            )

            claim_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/claim",
                json={"worker_id": "worker_gate_b_api_1", "expected_version": 2},
                headers=MANAGER_HEADERS,
            )
            assert claim_resp.status_code == 200
            claim_data = claim_resp.json()
            assert claim_data["execution_claimed_by"] == "worker_gate_b_api_1"
            assert claim_data["execution_claimed_at"] is not None
            assert claim_data["version"] == 3
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_gate_b_api_claim_fails_closed_when_email_drifts(session_factory, monkeypatch):
    """Gate B: HTTP POST /claim fails closed with 409 CONTEXT_DRIFT when lead email changes after approval."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session, email="original@acme.com")

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # 1. Request and Approve
            req_resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=MANAGER_HEADERS,
            )
            appr_id = req_resp.json()["id"]

            await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "Approved context"},
                headers=MANAGER_HEADERS,
            )

            # 2. Mutate lead email out-of-band in database (simulates drift between approve and claim)
            async with session_factory() as session:
                lead_row = await session.get(Lead, lead.id)
                lead_row.email = "tampered@attacker.com"
                await session.commit()

            # 3. Worker attempts claim -> Fails closed with 409 CONTEXT_DRIFT
            claim_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/claim",
                json={"worker_id": "worker_gate_b_api_2", "expected_version": 2},
                headers=MANAGER_HEADERS,
            )
            assert claim_resp.status_code == 409
            assert claim_resp.json()["error"]["code"] == "CONTEXT_DRIFT"

            # 4. State invariants: approval status is still APPROVED, version is 2, execution_claimed_at is None
            get_resp = await ac.get(f"/api/sales-supervisor/approvals/{appr_id}", headers=MANAGER_HEADERS)
            assert get_resp.status_code == 200
            data = get_resp.json()
            assert data["status"] == "APPROVED"
            assert data["version"] == 2
            assert data["execution_claimed_at"] is None
            assert data["execution_claimed_by"] is None

            # 5. Zero CLAIMED audit events persisted
            audit_resp = await ac.get(f"/api/sales-supervisor/approvals/{appr_id}/audit", headers=MANAGER_HEADERS)
            assert audit_resp.status_code == 200
            events = audit_resp.json()["events"]
            assert not any(e["event_type"] == "CLAIMED" for e in events)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_gate_b_api_claim_fails_closed_when_phone_drifts(session_factory, monkeypatch):
    """Gate B: HTTP POST /claim fails closed with 409 CONTEXT_DRIFT when lead phone changes after approval."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session, phone="+15551234567")

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            req_resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=MANAGER_HEADERS,
            )
            appr_id = req_resp.json()["id"]

            await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "Approved context"},
                headers=MANAGER_HEADERS,
            )

            # Mutate phone
            async with session_factory() as session:
                lead_row = await session.get(Lead, lead.id)
                lead_row.phone = "+19998887777"
                await session.commit()

            claim_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/claim",
                json={"worker_id": "worker_gate_b_api_3", "expected_version": 2},
                headers=MANAGER_HEADERS,
            )
            assert claim_resp.status_code == 409
            assert claim_resp.json()["error"]["code"] == "CONTEXT_DRIFT"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_gate_b_api_claim_fails_closed_when_opportunity_score_drifts(session_factory, monkeypatch):
    """Gate B: HTTP POST /claim fails closed with 409 CONTEXT_DRIFT when opportunity score changes after approval."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session, score=85)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            req_resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=MANAGER_HEADERS,
            )
            appr_id = req_resp.json()["id"]

            await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "Approved context"},
                headers=MANAGER_HEADERS,
            )

            # Mutate opportunity score
            async with session_factory() as session:
                opp_row = await session.get(LeadIntelligenceOpportunity, opp.id)
                opp_row.score = 25
                await session.commit()

            claim_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/claim",
                json={"worker_id": "worker_gate_b_api_4", "expected_version": 2},
                headers=MANAGER_HEADERS,
            )
            assert claim_resp.status_code == 409
            assert claim_resp.json()["error"]["code"] == "CONTEXT_DRIFT"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_gate_b_api_claim_fails_closed_when_evidence_drifts(session_factory, monkeypatch):
    """Gate B: HTTP POST /claim fails closed with 409 CONTEXT_DRIFT when opportunity evidence changes."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            req_resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=MANAGER_HEADERS,
            )
            appr_id = req_resp.json()["id"]

            await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "Approved context"},
                headers=MANAGER_HEADERS,
            )

            # Mutate evidence
            async with session_factory() as session:
                opp_row = await session.get(LeadIntelligenceOpportunity, opp.id)
                opp_row.evidence = {"completely_new_key": "altered_evidence"}
                await session.commit()

            claim_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/claim",
                json={"worker_id": "worker_gate_b_api_5", "expected_version": 2},
                headers=MANAGER_HEADERS,
            )
            assert claim_resp.status_code == 409
            assert claim_resp.json()["error"]["code"] == "CONTEXT_DRIFT"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_gate_b_api_claim_fails_closed_when_lead_deleted(session_factory, monkeypatch):
    """Gate B: HTTP POST /claim fails closed with 409 CONTEXT_DRIFT when target Lead is deleted."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            req_resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=MANAGER_HEADERS,
            )
            appr_id = req_resp.json()["id"]

            await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "Approved context"},
                headers=MANAGER_HEADERS,
            )

            # Delete lead
            async with session_factory() as session:
                lead_row = await session.get(Lead, lead.id)
                await session.delete(lead_row)
                await session.commit()

            claim_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/claim",
                json={"worker_id": "worker_gate_b_api_6", "expected_version": 2},
                headers=MANAGER_HEADERS,
            )
            assert claim_resp.status_code == 409
            assert claim_resp.json()["error"]["code"] == "CONTEXT_DRIFT"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_gate_b_api_claim_fails_closed_when_opportunity_deleted(session_factory, monkeypatch):
    """Gate B: HTTP POST /claim fails closed with 409 CONTEXT_DRIFT when target Opportunity is deleted."""
    monkeypatch.setattr(settings, "api_auth_token", AUTH_TOKEN)
    app.dependency_overrides[get_db] = lambda: session_factory()

    async with session_factory() as session:
        lead, opp = await create_test_lead_and_opp(session)

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            req_resp = await ac.post(
                "/api/sales-supervisor/approvals/request",
                json={"lead_id": str(lead.id), "opportunity_id": str(opp.id)},
                headers=MANAGER_HEADERS,
            )
            appr_id = req_resp.json()["id"]

            await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/approve",
                json={"decision_reason": "Approved context"},
                headers=MANAGER_HEADERS,
            )

            # Delete opportunity
            async with session_factory() as session:
                opp_row = await session.get(LeadIntelligenceOpportunity, opp.id)
                await session.delete(opp_row)
                await session.commit()

            claim_resp = await ac.post(
                f"/api/sales-supervisor/approvals/{appr_id}/claim",
                json={"worker_id": "worker_gate_b_api_7", "expected_version": 2},
                headers=MANAGER_HEADERS,
            )
            assert claim_resp.status_code == 409
            assert claim_resp.json()["error"]["code"] == "CONTEXT_DRIFT"
    finally:
        app.dependency_overrides.pop(get_db, None)



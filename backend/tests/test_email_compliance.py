"""Email compliance: suppression list, unsubscribe tokens/links/headers, footer, and wiring."""
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings, settings
from app.database import get_db
from app.domain.dispatch_errors import DispatchComplianceConfigError, DispatchRecipientSuppressedError
from app.models.entities import EmailSuppression, EmailSuppressionReason
from app.services import email_compliance as ec
from app.services.providers.resend_adapter import ResendEmailAdapter
from app.services.supervisor_dispatch_service import SupervisorApprovalDispatchService

ADDR = "Prospect.Person@Example.com"


def cfg(**overrides) -> ec.ComplianceConfig:
    base = dict(
        secret="s" * 40, base_url="https://leads.example.com", mailto="",
        business_name="Acme Digital", postal_address="12 Park Street, Kolkata 700016",
    )
    base.update(overrides)
    return ec.ComplianceConfig(**base)


def make_settings(**env) -> Settings:
    values = {
        "OUTREACH_UNSUBSCRIBE_SECRET": "x" * 40,
        "OUTREACH_UNSUBSCRIBE_BASE_URL": "https://leads.example.com",
        "OUTREACH_UNSUBSCRIBE_MAILTO": "",
        "OUTREACH_SENDER_POSTAL_ADDRESS": "12 Park Street",
        "OUTREACH_COMPLIANCE_ENFORCED": True,
    }
    values.update(env)
    return Settings(**values)


# --- tokens -----------------------------------------------------------------

def test_token_round_trip_and_carries_no_readable_address():
    c = cfg()
    token = ec.make_unsubscribe_token(ADDR, c)
    assert "example" not in token.lower() and "@" not in token
    assert ec.verify_unsubscribe_token(token, c) == ec.email_hash(ADDR)
    # Normalization: case and display-name forms map to the same hash.
    assert ec.email_hash(f"Someone <{ADDR.lower()}>") == ec.email_hash(ADDR)


@pytest.mark.parametrize("mutate", [
    lambda t: t[:-1] + ("A" if t[-1] != "A" else "B"),       # tampered signature
    lambda t: ("0" * 64) + t[64:],                             # swapped hash
    lambda t: t.split(".")[0],                                 # missing signature
    lambda t: "garbage",
])
def test_tampered_tokens_are_rejected(mutate):
    c = cfg()
    assert ec.verify_unsubscribe_token(mutate(ec.make_unsubscribe_token(ADDR, c)), c) is None


def test_token_from_other_secret_is_rejected():
    token = ec.make_unsubscribe_token(ADDR, cfg(secret="a" * 40))
    assert ec.verify_unsubscribe_token(token, cfg(secret="b" * 40)) is None


# --- configuration ------------------------------------------------------------

def test_complete_config_loads():
    c = ec.get_compliance_config(make_settings())
    assert c.base_url == "https://leads.example.com"


@pytest.mark.parametrize("env", [
    {"OUTREACH_UNSUBSCRIBE_SECRET": "short"},
    {"OUTREACH_UNSUBSCRIBE_BASE_URL": "", "OUTREACH_UNSUBSCRIBE_MAILTO": ""},
    {"OUTREACH_UNSUBSCRIBE_BASE_URL": "http://leads.example.com"},
    {"OUTREACH_UNSUBSCRIBE_BASE_URL": "https://leads.example.com/?x=1"},
    {"OUTREACH_UNSUBSCRIBE_MAILTO": "not-an-email"},
    {"OUTREACH_SENDER_POSTAL_ADDRESS": ""},
])
def test_incomplete_config_fails_closed(env):
    with pytest.raises(ec.ComplianceConfigurationError):
        ec.get_compliance_config(make_settings(**env))


def test_enforcement_can_be_disabled_explicitly():
    assert ec.get_compliance_config(make_settings(OUTREACH_SENDER_POSTAL_ADDRESS="", OUTREACH_COMPLIANCE_ENFORCED=False)) is None


def test_localhost_http_link_allowed_for_development():
    assert ec.get_compliance_config(make_settings(OUTREACH_UNSUBSCRIBE_BASE_URL="http://localhost:8000")) is not None


# --- headers / footer -----------------------------------------------------------

def test_one_click_headers_with_https_link_and_mailto():
    headers = ec.list_unsubscribe_headers(ADDR, cfg(mailto="unsubscribe@acme.example"))
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert headers["List-Unsubscribe"].startswith("<https://leads.example.com/unsubscribe/")
    assert "<mailto:unsubscribe@acme.example?subject=unsubscribe>" in headers["List-Unsubscribe"]


def test_mailto_only_has_no_one_click_post_header():
    headers = ec.list_unsubscribe_headers(ADDR, cfg(base_url="", mailto="unsubscribe@acme.example"))
    assert "List-Unsubscribe-Post" not in headers
    assert headers["List-Unsubscribe"] == "<mailto:unsubscribe@acme.example?subject=unsubscribe>"


def test_footer_contains_link_identity_and_address_in_text_and_html():
    text, html = ec.apply_compliance_footer("Hi there", "<p>Hi there</p>", ADDR, cfg())
    assert text.startswith("Hi there\n\n--\n")
    assert "https://leads.example.com/unsubscribe/" in text
    assert "Acme Digital, 12 Park Street, Kolkata 700016" in text
    assert html.startswith("<p>Hi there</p>") and 'href="https://leads.example.com/unsubscribe/' in html


def test_footer_html_escapes_configuration():
    _, html = ec.apply_compliance_footer("x", "<p>x</p>", ADDR, cfg(business_name="<script>", base_url=""
                                                                         , mailto="u@acme.example"))
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_header_filter_drops_unknown_and_crlf_values():
    out = ec.filter_compliance_headers({
        "List-Unsubscribe": "<https://x.example/u>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click\r\nBcc: victim@example.com",
        "Bcc": "victim@example.com",
    })
    assert out == {"List-Unsubscribe": "<https://x.example/u>"}
    assert ec.filter_compliance_headers("nope") == {}


# --- suppression list -------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_suppression_is_idempotent_and_stores_no_plain_address(session_factory):
    async with session_factory() as db:
        assert not await ec.is_suppressed(db, ADDR)
        row, created = await ec.add_suppression(db, email=ADDR, reason=EmailSuppressionReason.HARD_BOUNCE, source="test")
        again, created_again = await ec.add_suppression(db, email=ADDR.lower(), reason="COMPLAINT", source="test")
        await db.commit()
        assert created and not created_again and again.id == row.id
        assert row.email_masked == "p***n@example.com" and ADDR.lower() not in (row.email_hash, row.email_masked)
        assert await ec.is_suppressed(db, f"<{ADDR.upper()}>")
        with pytest.raises(ec.RecipientSuppressedError):
            await ec.ensure_not_suppressed(db, ADDR)


# --- public unsubscribe endpoints + API ---------------------------------------------

@pytest.fixture
def client_factory(session_factory, monkeypatch):
    from app.main import app
    monkeypatch.setattr(settings, "api_auth_token", "compliance-test-token")

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return lambda: AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_unsubscribe_get_confirms_without_suppressing_and_post_suppresses(client_factory, session_factory):
    token = ec.make_unsubscribe_token(ADDR, ec.get_compliance_config())
    async with client_factory() as client:
        page = await client.get(f"/unsubscribe/{token}")
        assert page.status_code == 200 and "<form method=\"post\"" in page.text
        async with session_factory() as db:
            assert not await ec.is_suppressed(db, ADDR)

        # RFC 8058 one-click POST (no bearer token; body as mailbox providers send it).
        done = await client.post(f"/unsubscribe/{token}", content="List-Unsubscribe=One-Click",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert done.status_code == 200 and "unsubscribed" in done.text
        assert (await client.post(f"/unsubscribe/{token}")).status_code == 200  # idempotent

    async with session_factory() as db:
        assert await ec.is_suppressed(db, ADDR)


@pytest.mark.asyncio
async def test_unsubscribe_rejects_forged_token(client_factory, session_factory):
    forged = ec.email_hash(ADDR) + "." + "A" * 32
    async with client_factory() as client:
        assert (await client.get(f"/unsubscribe/{forged}")).status_code == 400
        assert (await client.post(f"/unsubscribe/{forged}")).status_code == 400
    async with session_factory() as db:
        assert not await ec.is_suppressed(db, ADDR)


@pytest.mark.asyncio
async def test_suppression_api_requires_auth_and_supports_crud(client_factory):
    auth = {"Authorization": "Bearer compliance-test-token"}
    async with client_factory() as client:
        assert (await client.get("/api/email-suppressions")).status_code == 401
        created = await client.post("/api/email-suppressions", json={"email": ADDR, "details": "replied STOP"}, headers=auth)
        assert created.status_code == 201 and created.json()["reason"] == "MANUAL"
        assert (await client.post("/api/email-suppressions", json={"email": "bad"}, headers=auth)).status_code == 422
        listing = (await client.get("/api/email-suppressions", headers=auth)).json()
        assert listing["total"] == 1 and listing["items"][0]["email_masked"] == "p***n@example.com"
        sid = created.json()["id"]
        assert (await client.delete(f"/api/email-suppressions/{sid}", headers=auth)).status_code == 204
        assert (await client.delete(f"/api/email-suppressions/{sid}", headers=auth)).status_code == 404


# --- provider adapters ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_resend_adapter_forwards_only_compliance_headers():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "msg_1"})

    adapter = ResendEmailAdapter(api_key="re_test_key_123456", from_email="sales@acme.example",
                                 http_transport=httpx.MockTransport(handler))
    headers = ec.list_unsubscribe_headers(ADDR, cfg())
    result = await adapter.send_dispatch(channel="EMAIL", recipient=ADDR, idempotency_key="k1",
                                         payload_json={"subject": "Hi", "body": "Hello", "headers": {**headers, "Bcc": "x@y.z"}})
    assert result.success
    assert captured["headers"] == headers


# --- supervisor dispatch guard ---------------------------------------------------------

@pytest.mark.asyncio
async def test_supervisor_guard_blocks_suppressed_recipient(session_factory):
    async with session_factory() as db:
        await SupervisorApprovalDispatchService._enforce_email_compliance(db, ADDR)  # not suppressed: passes
        await ec.add_suppression(db, email=ADDR, reason="UNSUBSCRIBED", source="test")
        with pytest.raises(DispatchRecipientSuppressedError):
            await SupervisorApprovalDispatchService._enforce_email_compliance(db, ADDR)


@pytest.mark.asyncio
async def test_supervisor_guard_fails_closed_without_compliance_config(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "outreach_sender_postal_address", "")
    async with session_factory() as db:
        with pytest.raises(DispatchComplianceConfigError):
            await SupervisorApprovalDispatchService._enforce_email_compliance(db, ADDR)


# --- webhook auto-suppression ----------------------------------------------------------

def _event(event_type, raw_type="x", bounce_type=None):
    return SimpleNamespace(event_type=event_type, raw_event_type=raw_type, bounce_type=bounce_type,
                           provider_name="RESEND", bounce_reason=None)


@pytest.mark.parametrize("event,expected", [
    (_event("COMPLAINT", "email.complained"), EmailSuppressionReason.COMPLAINT),
    (_event("BOUNCED", "email.bounced", "PERMANENT"), EmailSuppressionReason.HARD_BOUNCE),
    (_event("OTHER", "unsubscribe"), EmailSuppressionReason.UNSUBSCRIBED),
    (_event("DROPPED", "email.delivery_delayed", "TRANSIENT"), None),
    (_event("DELIVERED", "email.delivered"), None),
])
def test_webhook_suppression_reason_mapping(event, expected):
    from app.api.routes.supervisor_delivery_webhooks import _suppression_reason
    assert _suppression_reason(event) == expected


@pytest.mark.asyncio
async def test_webhook_auto_suppresses_correlated_recipient(session_factory):
    from app.api.routes.supervisor_delivery_webhooks import _auto_suppress
    dispatch_id = uuid.uuid4()
    async with session_factory() as db:
        db.get = AsyncMock(return_value=SimpleNamespace(channel="EMAIL", recipient=ADDR))
        await _auto_suppress(db, _event("COMPLAINT", "email.complained"), dispatch_id)
        await db.commit()
        row = (await db.scalars(__import__("sqlalchemy").select(EmailSuppression))).one()
        assert row.reason == "COMPLAINT" and row.source == "webhook_resend"


# --- Phase 8F outreach operations path ------------------------------------------------

@pytest.mark.asyncio
async def test_outreach_operations_refuses_suppressed_email(session_factory):
    from app.services.outreach_operations import OutreachOperationError, _send_attempt
    async with session_factory() as db:
        await ec.add_suppression(db, email=ADDR, reason="UNSUBSCRIBED", source="test")
        await db.commit()
        execution = SimpleNamespace(channel="EMAIL", recipient=ADDR, content_snapshot={"text": "Hi"}, subject="Hi",
                                    provider="http_json", idempotency_key="k", status="SENDING")
        provider = SimpleNamespace(name="mock", send=AsyncMock())
        with pytest.raises(OutreachOperationError) as err:
            await _send_attempt(db, execution, provider=provider)
        assert err.value.code == "RECIPIENT_SUPPRESSED" and err.value.status_code == 409
        assert execution.status == "FAILED"
        provider.send.assert_not_called()


# --- supervisor outbox send step ------------------------------------------------------

@pytest.mark.asyncio
async def test_supervisor_execute_adds_footer_and_unsubscribe_headers(session_factory, monkeypatch):
    from app.services.dispatch_provider import DispatchProviderResult

    service = SupervisorApprovalDispatchService()
    job = SimpleNamespace(channel="EMAIL", recipient=ADDR, payload_json={"subject": "Hi", "body_text": "Hello there"},
                          idempotency_key="k", attempt_count=1, max_attempts=3, provider_name="RESEND")
    monkeypatch.setattr(service, "pre_flight_guard_check", AsyncMock(return_value=job))
    service.repository = SimpleNamespace(record_completion_post_network=AsyncMock(return_value="done"))

    sent = {}

    class Provider:
        async def send_dispatch(self, *, channel, recipient, payload_json, idempotency_key):
            sent.update(payload_json)
            return DispatchProviderResult(success=True, provider_message_id="m1")

    result = await service.execute_claimed_dispatch(
        session_factory, dispatch_id=uuid.uuid4(), worker_id="w", expected_lease_generation=1, provider=Provider(),
    )
    assert result == "done"
    assert sent["body"].startswith("Hello there\n\n--\n") and "/unsubscribe/" in sent["body"]
    assert "body_text" not in sent
    assert sent["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert job.payload_json == {"subject": "Hi", "body_text": "Hello there"}  # stored payload untouched

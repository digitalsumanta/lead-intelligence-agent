"""Request IDs, structured log correlation, Prometheus metrics and Sentry initialisation."""
import json
import logging

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import observability as obs
from app.config import settings
from app.database import get_db
from app.utils.logging_config import JsonFormatter


@pytest_asyncio.fixture
async def client(session_factory, monkeypatch):
    from app.main import app
    monkeypatch.setattr(settings, "api_auth_token", "obs-test-token")

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as c:
        yield c


AUTH = {"Authorization": "Bearer obs-test-token"}


@pytest.mark.asyncio
async def test_request_id_generated_reused_and_sanitised(client):
    generated = (await client.get("/health")).headers["x-request-id"]
    assert len(generated) == 32

    supplied = "trace-abc-12345678"
    assert (await client.get("/health", headers={"X-Request-ID": supplied})).headers["x-request-id"] == supplied

    hostile = "bad id\nwith newline"
    assert (await client.get("/health", headers={"X-Request-ID": hostile})).headers["x-request-id"] != hostile


def test_log_lines_include_current_request_id():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
    token = obs.request_id_var.set("req-0123456789")
    try:
        obs.RequestIdLogFilter().filter(record)
    finally:
        obs.request_id_var.reset(token)
    assert json.loads(JsonFormatter().format(record))["request_id"] == "req-0123456789"


@pytest.mark.asyncio
async def test_unhandled_error_returns_request_id_and_is_counted(client):
    from app.main import app

    async def boom():
        raise RuntimeError("kaboom")

    app.add_api_route("/__test_boom", boom)
    try:
        res = await client.get("/__test_boom", headers={"X-Request-ID": "req-boom-12345"})
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/__test_boom"]
    assert res.status_code == 500
    assert res.json()["error"]["details"]["request_id"] == "req-boom-12345"
    assert "kaboom" not in res.text
    assert obs.HTTP_UNHANDLED_ERRORS.labels("/__test_boom")._value.get() >= 1


@pytest.mark.asyncio
async def test_metrics_disabled_without_token(client, monkeypatch):
    monkeypatch.setattr(settings, "metrics_token", "")
    assert (await client.get("/metrics")).status_code == 404


@pytest.mark.asyncio
async def test_metrics_requires_token_and_exposes_http_and_database_metrics(client, monkeypatch, session_factory):
    from app.services.email_compliance import add_suppression
    monkeypatch.setattr(settings, "metrics_token", "scrape-secret-123")
    async with session_factory() as db:
        await add_suppression(db, email="x@example.com", reason="COMPLAINT", source="test")
        await db.commit()

    assert (await client.get("/metrics")).status_code == 401
    assert (await client.get("/metrics", headers={"Authorization": "Bearer wrong"})).status_code == 401

    await client.get("/api/leads", headers=AUTH)
    res = await client.get("/metrics", headers={"Authorization": "Bearer scrape-secret-123"})
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/plain")
    body = res.text
    assert 'lead_agent_http_requests_total{method="GET",route="/api/leads",status="200"}' in body
    assert 'lead_agent_email_suppressions{reason="COMPLAINT"} 1.0' in body
    assert "lead_agent_worker_heartbeat_age_seconds -1.0" in body
    assert "lead_agent_metrics_collection_errors_total 0.0" in body


def test_route_label_uses_template_not_raw_path():
    class Route:
        path = "/api/leads/{lead_id}"
    assert obs.route_label({"route": Route()}) == "/api/leads/{lead_id}"
    assert obs.route_label({}) == "unmatched"


def test_sentry_disabled_without_dsn(monkeypatch):
    monkeypatch.setattr(settings, "sentry_dsn", "")
    monkeypatch.setattr(obs, "_sentry_initialized", False)
    assert obs.init_sentry() is False


def test_sentry_enabled_without_pii(monkeypatch):
    import sentry_sdk
    captured = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr(obs, "_sentry_initialized", False)
    monkeypatch.setattr(settings, "sentry_dsn", "https://public@o0.ingest.sentry.io/0")
    assert obs.init_sentry() is True
    assert captured["send_default_pii"] is False and captured["max_request_body_size"] == "never"
    assert captured["traces_sample_rate"] == 0.0

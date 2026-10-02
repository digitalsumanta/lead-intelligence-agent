"""Focused tests for schema readiness and the public website fetch boundary."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Base
from app.services import website
from app.services.website import (
    PinnedAddressTransport,
    UnsafeWebsiteUrl,
    WebsiteAnalyzer,
    resolve_public_host,
    validate_url_syntax,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://10.1.2.3/",
        "http://[fc00::1]/",
        "http://169.254.10.20/",
        "http://[ff02::1]/",
        "http://192.0.2.1/",
        "http://[::ffff:127.0.0.1]/",
        "http://metadata.google.internal/",
        "http://metadata/",
        "http://2130706433/",
        "http://0177.0.0.1/",
        "http://0x7f000001/",
        "ftp://example.com/",
        "https://user:password@example.com/",
        "https://example.com:8443/",
    ],
)
def test_website_rejects_unsafe_url_forms(url):
    with pytest.raises(UnsafeWebsiteUrl):
        validate_url_syntax(url)


def test_website_accepts_and_normalizes_public_hostname():
    assert validate_url_syntax("https://Example.COM./path") == "example.com"
    assert validate_url_syntax("https://bücher.de/") == "xn--bcher-kva.de"
    assert validate_url_syntax("https://8.8.8.8/") == "8.8.8.8"


@pytest.mark.asyncio
async def test_dns_rejects_any_nonpublic_a_or_aaaa_answer(monkeypatch):
    async def mixed_answers(_host, _port):
        return ["93.184.216.34", "2606:4700:4700::1111", "10.0.0.8"]

    monkeypatch.setattr(website, "_lookup_addresses", mixed_answers)
    with pytest.raises(UnsafeWebsiteUrl, match="non-public"):
        await resolve_public_host("example.com", 443)


@pytest.mark.asyncio
async def test_dns_accepts_and_returns_public_a_and_aaaa_answers(monkeypatch):
    async def public_answers(_host, _port):
        return ["93.184.216.34", "2606:4700:4700::1111"]

    monkeypatch.setattr(website, "_lookup_addresses", public_answers)
    assert await resolve_public_host("example.com", 443) == [
        "93.184.216.34", "2606:4700:4700::1111",
    ]


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1", "::1", "10.0.0.1", "fd00::1", "169.254.169.254",
        "fe80::1", "224.0.0.1", "ff02::1", "192.0.2.1", "::ffff:127.0.0.1",
    ],
)
@pytest.mark.asyncio
async def test_dns_rejects_nonpublic_address_literals(address):
    with pytest.raises(UnsafeWebsiteUrl):
        await resolve_public_host(address, 443)


class AsyncMockTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self.handler = handler

    async def handle_async_request(self, request):
        return self.handler(request)


@pytest.mark.asyncio
async def test_each_redirect_is_resolved_and_connection_uses_pinned_ip(monkeypatch):
    dns_calls = []
    requests = []

    async def public_dns(host, _port):
        dns_calls.append(host)
        return ["93.184.216.34"]

    def handler(request):
        requests.append(request)
        if request.headers["host"] == "first.example":
            return httpx.Response(302, headers={"location": "https://second.example/page"}, request=request)
        return httpx.Response(200, text="<title>Example</title><h1>Hello</h1>", request=request)

    monkeypatch.setattr(website, "_lookup_addresses", public_dns)
    analyzer = WebsiteAnalyzer(
        transport_factory=lambda host, address, _scheme: PinnedAddressTransport(
            host, address, inner=AsyncMockTransport(handler)
        ),
    )

    final_url, status, _body = await analyzer._fetch("https://first.example/")

    assert final_url == "https://second.example/page"
    assert status == 200
    assert dns_calls == ["first.example", "second.example"]
    assert [request.url.host for request in requests] == ["93.184.216.34", "93.184.216.34"]
    assert [request.headers["host"] for request in requests] == ["first.example", "second.example"]
    assert all(request.extensions["sni_hostname"] == host.encode() for request, host in zip(requests, dns_calls))


@pytest.mark.parametrize("location", ["http://127.0.0.1/admin", "http://localhost/admin"])
@pytest.mark.asyncio
async def test_redirect_to_private_or_localhost_never_connects(monkeypatch, location):
    requests = []

    async def public_dns(_host, _port):
        return ["93.184.216.34"]

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": location}, request=request)

    monkeypatch.setattr(website, "_lookup_addresses", public_dns)
    analyzer = WebsiteAnalyzer(
        transport_factory=lambda host, address, _scheme: PinnedAddressTransport(
            host, address, inner=AsyncMockTransport(handler)
        ),
    )
    with pytest.raises(UnsafeWebsiteUrl):
        await analyzer._fetch("https://public.example/")
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_proxy_environment_is_disabled_and_timeout_bound_is_preserved(monkeypatch):
    captured = {}

    async def public_dns(_host, _port):
        return ["93.184.216.34"]

    def handler(request):
        return httpx.Response(200, text="ok", request=request)

    def client_factory(**kwargs):
        captured.update(kwargs)
        return httpx.AsyncClient(**kwargs)

    monkeypatch.setattr(website, "_lookup_addresses", public_dns)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    analyzer = WebsiteAnalyzer(
        timeout=2.5,
        client_factory=client_factory,
        transport_factory=lambda host, address, _scheme: PinnedAddressTransport(
            host, address, inner=AsyncMockTransport(handler)
        ),
    )

    await analyzer._fetch("https://public.example/")

    assert captured["trust_env"] is False
    assert isinstance(captured["transport"], PinnedAddressTransport)
    assert captured["timeout"].connect == 2.5
    assert captured["timeout"].read == 2.5


@pytest.mark.asyncio
async def test_response_size_limit_remains_enforced(monkeypatch):
    async def public_dns(_host, _port):
        return ["93.184.216.34"]

    def handler(request):
        return httpx.Response(200, content=b"123456", request=request)

    monkeypatch.setattr(website, "_lookup_addresses", public_dns)
    analyzer = WebsiteAnalyzer(
        max_response_bytes=5,
        transport_factory=lambda host, address, _scheme: PinnedAddressTransport(
            host, address, inner=AsyncMockTransport(handler)
        ),
    )
    with pytest.raises(ValueError, match="size limit"):
        await analyzer._fetch("https://public.example/")


@pytest.mark.asyncio
async def test_database_readiness_accepts_current_revision(monkeypatch):
    from app import database

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(database, "engine", engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        await connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('0028_delivery_reconcile')")
        )

    assert database.SCHEMA_REVISION == "0028_delivery_reconcile"
    await database.verify_database_schema()
    await engine.dispose()

"""
Deterministic In-Memory Mock Transport for Provider Adapter Tests.

Phase 12J - D2 - Gate D-C2:
Enforces zero external socket transmission by intercepting all HTTP calls in-memory.
Provides deterministic simulation of provider acceptance, throttling, pre-submission failure,
post-submission timeout, malformed responses, and unexpected status codes.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional, Sequence

import httpx


class MockProviderTransport(httpx.AsyncBaseTransport):
    """
    Deterministic async in-memory transport for mock provider testing.
    Records all requests and returns preconfigured responses without network I/O.
    """

    def __init__(
        self,
        *,
        default_status: int = 200,
        default_json: Optional[dict[str, Any]] = None,
        default_headers: Optional[dict[str, str]] = None,
        simulate_connect_error: bool = False,
        simulate_read_timeout: bool = False,
        simulate_malformed_json: bool = False,
        custom_handler: Optional[Callable[[httpx.Request], httpx.Response]] = None,
    ) -> None:
        self.default_status = default_status
        self.default_json = default_json or {"id": "re_mock_test_msg_001"}
        self.default_headers = default_headers or {"Content-Type": "application/json"}
        self.simulate_connect_error = simulate_connect_error
        self.simulate_read_timeout = simulate_read_timeout
        self.simulate_malformed_json = simulate_malformed_json
        self.custom_handler = custom_handler
        self.captured_requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Intercepts request and returns mock response or raises simulated exception."""
        # Read the request stream into memory so headers and body are fully captured
        await request.aread()
        self.captured_requests.append(request)

        if self.simulate_connect_error:
            raise httpx.ConnectError(f"Simulated pre-submission connection failure to {request.url.host}")

        if self.simulate_read_timeout:
            raise httpx.ReadTimeout(f"Simulated post-submission read timeout from {request.url.host}")

        if self.custom_handler:
            return self.custom_handler(request)

        if self.simulate_malformed_json:
            return httpx.Response(
                status_code=self.default_status,
                headers={"Content-Type": "application/json"},
                content=b"INVALID_JSON_BODY{{{",
                request=request,
            )

        content = json.dumps(self.default_json).encode("utf-8")
        return httpx.Response(
            status_code=self.default_status,
            headers=self.default_headers,
            content=content,
            request=request,
        )

    @property
    def last_request(self) -> Optional[httpx.Request]:
        """Returns the most recent captured request, or None."""
        return self.captured_requests[-1] if self.captured_requests else None

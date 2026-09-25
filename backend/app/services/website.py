"""Bounded public-web inspection with destination validation at every hop."""
import asyncio
import ipaddress
import re
import socket
import time
from urllib.parse import urljoin, urlparse
import httpx

MAX_RESPONSE_BYTES = 1_000_000
MAX_REDIRECTS = 5

class UnsafeWebsiteUrl(ValueError):
    """Raised when a URL does not resolve exclusively to public web addresses."""

def validate_url_syntax(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeWebsiteUrl("Only public HTTP(S) website URLs are allowed")
    host = parsed.hostname.rstrip(".").lower()
    if parsed.port not in {None, 80, 443}:
        raise UnsafeWebsiteUrl("Only standard HTTP(S) ports are allowed")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".test", ".home", ".lan")):
        raise UnsafeWebsiteUrl("Local hostnames are not allowed")
    if host in {"metadata.google.internal", "metadata", "instance-data"}:
        raise UnsafeWebsiteUrl("Cloud metadata hosts are not allowed")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if not address.is_global:
        raise UnsafeWebsiteUrl("Non-public IP addresses are not allowed")
    return host

async def resolve_public_host(host: str, port: int) -> None:
    """Reject DNS names that resolve to any non-public address."""
    try:
        address = ipaddress.ip_address(host)
        addresses = [address]
    except ValueError:
        records = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout=5)
        addresses = [ipaddress.ip_address(record[4][0].split("%", 1)[0]) for record in records]
    if not addresses or any(not address.is_global for address in addresses):
        raise UnsafeWebsiteUrl("Website host resolves to a non-public address")

class WebsiteAnalyzer:
    """Inspect public websites with redirect, time, and response-size bounds."""
    def __init__(self, timeout: float = 10, max_response_bytes: int = MAX_RESPONSE_BYTES, max_redirects: int = MAX_REDIRECTS):
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.max_redirects = max_redirects

    async def _fetch(self, url: str) -> tuple[str, int, str]:
        current = url
        timeout = httpx.Timeout(self.timeout)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers={"User-Agent": "LeadIntelligenceBot/1.0"}) as client:
            for hop in range(self.max_redirects + 1):
                host = validate_url_syntax(current)
                parsed = urlparse(current)
                await resolve_public_host(host, parsed.port or (443 if parsed.scheme == "https" else 80))
                async with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or hop >= self.max_redirects:
                            raise UnsafeWebsiteUrl("Website exceeded the redirect limit")
                        target = urljoin(current, location)
                        validate_url_syntax(target)
                        current = target
                        continue
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > self.max_response_bytes:
                            raise ValueError("Website response exceeded size limit")
                        body.extend(chunk)
                    return str(response.url), response.status_code, bytes(body).decode(response.encoding or "utf-8", errors="replace")
        raise UnsafeWebsiteUrl("Website redirect limit exceeded")

    async def analyze(self, url: str | None) -> dict:
        """Return HTTP, metadata, HTTPS, SEO, and social-link observations."""
        if not url: return {"available": False, "issues": ["No website supplied"]}
        started = time.perf_counter()
        try:
            final_url, status_code, html = await self._fetch(url)
            title = self._capture(r"<title[^>]*>(.*?)</title>", html)
            description = self._capture(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)', html, re.I)
            h1 = self._capture(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
            issues = []
            if not title: issues.append("Missing page title")
            if not description: issues.append("Missing meta description")
            if not h1: issues.append("Missing H1 heading")
            if status_code >= 400: issues.append(f"HTTP status {status_code}")
            social = re.findall(r'https?://(?:www\.)?(?:linkedin|facebook|instagram|x|twitter)\.com/[^"\'\s<>]+', html, re.I)
            return {"available": True, "url": final_url, "status_code": status_code, "title": self._strip(title), "meta_description": self._strip(description), "h1": self._strip(h1), "https": urlparse(final_url).scheme == "https", "issues": issues, "social_links": list(dict.fromkeys(social)), "response_time_ms": round((time.perf_counter()-started)*1000)}
        except (httpx.HTTPError, ValueError, OSError):
            return {"available": False, "url": url, "error": "Website could not be reached safely", "issues": ["Website could not be reached"]}

    @staticmethod
    def _capture(pattern, text, flags=0):
        match = re.search(pattern, text, flags)
        return match.group(1).strip() if match else None
    @staticmethod
    def _strip(value):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip() or None

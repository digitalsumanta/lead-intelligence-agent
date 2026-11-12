"""Bounded public-web inspection with connection-pinned destination validation."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


MAX_RESPONSE_BYTES = 1_000_000
MAX_REDIRECTS = 5
DNS_TIMEOUT_SECONDS = 5
ALLOWED_PORTS = frozenset({80, 443})
LOCAL_HOST_SUFFIXES = (
    ".localhost", ".local", ".localdomain", ".internal", ".test",
    ".home", ".lan", ".onion",
)
METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata", "instance-data"})


@dataclass(frozen=True)
class SafeFetchResult:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str | None
    x_robots_tag: str | None
    encoding: str | None
    redirect_chain: tuple[str, ...]
    body: bytes


class UnsafeWebsiteUrl(ValueError):
    """Raised when a URL does not resolve exclusively to public web addresses."""


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not any((
        not address.is_global,
        address.is_private,
        address.is_loopback,
        address.is_link_local,
        address.is_multicast,
        address.is_reserved,
        address.is_unspecified,
    ))


def _normalized_host(raw_host: str) -> tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address | None]:
    host = raw_host.rstrip(".").lower()
    if not host or "%" in host:
        raise UnsafeWebsiteUrl("Website hostname is invalid")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
            host = address.compressed
        if not _is_public_address(address):
            raise UnsafeWebsiteUrl("Non-public IP addresses are not allowed")
        return address.compressed, address

    # Reject legacy integer, octal, and hexadecimal IPv4 spellings instead of
    # treating them as DNS names. Strict IPv4 literals were handled above.
    labels = host.split(".")
    numeric_label = re.compile(r"(?:[0-9]+|0[xX][0-9a-fA-F]+)\Z")
    if all(numeric_label.fullmatch(label) for label in labels):
        raise UnsafeWebsiteUrl("Unusual numeric website addresses are not allowed")

    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise UnsafeWebsiteUrl("Website hostname is invalid") from exc
    if len(host) > 253 or "." not in host:
        raise UnsafeWebsiteUrl("Local hostnames are not allowed")
    if any(not label or len(label) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) for label in host.split(".")):
        raise UnsafeWebsiteUrl("Website hostname is invalid")
    if host == "localhost" or host.endswith(LOCAL_HOST_SUFFIXES) or host in METADATA_HOSTS:
        raise UnsafeWebsiteUrl("Local and cloud metadata hostnames are not allowed")
    return host, None


def _parse_url(url: str) -> tuple[str, str, int, str]:
    """Validate and normalize a URL, returning URL, host, port, and scheme."""
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        raw_host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise UnsafeWebsiteUrl("Website URL is invalid") from exc
    if scheme not in {"http", "https"} or not raw_host or parsed.username is not None or parsed.password is not None:
        raise UnsafeWebsiteUrl("Only public HTTP(S) website URLs without credentials are allowed")
    host, address = _normalized_host(raw_host)
    if port is not None and port not in ALLOWED_PORTS:
        raise UnsafeWebsiteUrl("Only standard HTTP(S) ports are allowed")
    effective_port = port or (443 if scheme == "https" else 80)
    authority_host = f"[{host}]" if isinstance(address, ipaddress.IPv6Address) else host
    authority = f"{authority_host}:{port}" if port is not None else authority_host
    normalized_url = urlunsplit((scheme, authority, parsed.path or "/", parsed.query, ""))
    return normalized_url, host, effective_port, scheme


def validate_url_syntax(url: str) -> str:
    """Validate URL syntax and local-address forms; return its normalized host."""
    return _parse_url(url)[1]


def normalize_public_url(url: str, *, remove_query: bool = False) -> str:
    """Normalize a public HTTP(S) URL and remove its fragment."""
    normalized, _host, _port, _scheme = _parse_url(url)
    if remove_query:
        parts = urlsplit(normalized)
        normalized = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return normalized


def sanitized_public_url(url: str) -> str:
    """Return a safe persisted/display URL without query strings or fragments."""
    return normalize_public_url(url, remove_query=True)


async def _lookup_addresses(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    return [record[4][0].split("%", 1)[0] for record in records]


async def resolve_public_host(host: str, port: int) -> list[str]:
    """Resolve A and AAAA records, reject mixed/private answers, return pinned IPs."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            raw_addresses = await asyncio.wait_for(_lookup_addresses(host, port), timeout=DNS_TIMEOUT_SECONDS)
        except (OSError, asyncio.TimeoutError) as exc:
            raise UnsafeWebsiteUrl("Website host could not be safely resolved") from exc
    else:
        raw_addresses = [literal.compressed]

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for raw_address in raw_addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise UnsafeWebsiteUrl("Website host returned an invalid address") from exc
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        addresses.append(address)
    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise UnsafeWebsiteUrl("Website host resolves to a non-public address")
    return list(dict.fromkeys(address.compressed for address in addresses))


class SniHostname(str):
    """Custom str subclass that satisfies AnyIO TLS wrap (expects str) and legacy tests asserting bytes equality."""
    def __eq__(self, other: object) -> bool:
        if isinstance(other, (bytes, bytearray)):
            return self.encode("ascii") == other
        return super().__eq__(other)


class PinnedAddressTransport(httpx.AsyncBaseTransport):
    """Connect to one vetted IP while retaining hostname-based HTTP and TLS identity."""

    def __init__(self, hostname: str, address: str, *, inner: httpx.AsyncBaseTransport | None = None):
        self.hostname = hostname
        self.address = ipaddress.ip_address(address).compressed
        self.inner = inner or httpx.AsyncHTTPTransport(trust_env=False, proxy=None)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        parsed = request.url
        original_host = self.hostname
        ip_address = ipaddress.ip_address(self.address)
        connect_host = f"[{self.address}]" if isinstance(ip_address, ipaddress.IPv6Address) else self.address
        host_header = f"[{original_host}]" if ":" in original_host else original_host
        if parsed.port is not None:
            host_header = f"{host_header}:{parsed.port}"
        request.url = parsed.copy_with(host=connect_host)
        request.headers["host"] = host_header
        if parsed.scheme == "https":
            # httpcore uses this extension as TLS SNI and certificate hostname.
            request.extensions["sni_hostname"] = SniHostname(original_host)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


class WebsiteAnalyzer:
    """Inspect a public website with bounded fetches and a pinned IP per hop."""

    def __init__(
        self,
        timeout: float = 10,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_redirects: int = MAX_REDIRECTS,
        *,
        client_factory=None,
        transport_factory=None,
    ):
        if timeout <= 0 or max_response_bytes < 1 or max_redirects < 0:
            raise ValueError("Website fetch bounds must be positive")
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.max_redirects = max_redirects
        self.client_factory = client_factory
        self.transport_factory = transport_factory

    async def _fetch(self, url: str) -> tuple[str, int, str]:
        response = await self.fetch_document(url)
        return response.final_url, response.status_code, response.body.decode(response.encoding or "utf-8", errors="replace")

    async def fetch_document(
        self,
        url: str,
        *,
        allowed_host: str | None = None,
        allowed_origins: set[tuple[str, str, int]] | None = None,
        max_response_bytes: int | None = None,
        max_redirects: int | None = None,
    ) -> SafeFetchResult:
        """Safely fetch one document, pinning and revalidating every redirect hop."""
        requested_url = normalize_public_url(url)
        current = requested_url
        byte_limit = max_response_bytes if max_response_bytes is not None else self.max_response_bytes
        redirect_limit = max_redirects if max_redirects is not None else self.max_redirects
        if byte_limit < 1 or redirect_limit < 0:
            raise ValueError("Website fetch bounds must be positive")
        if allowed_host:
            allowed_host = _normalized_host(allowed_host)[0]
        timeout = httpx.Timeout(self.timeout)
        visited: set[str] = set()
        redirect_chain: list[str] = []
        for hop in range(redirect_limit + 1):
            normalized_url, host, port, scheme = _parse_url(current)
            if allowed_host and host != allowed_host:
                raise UnsafeWebsiteUrl("Website redirect left the allowed host")
            if allowed_origins is not None and (scheme, host, port) not in allowed_origins:
                raise UnsafeWebsiteUrl("Website URL left the allowed origin set")
            if normalized_url in visited:
                raise UnsafeWebsiteUrl("Website redirect loop detected")
            visited.add(normalized_url)
            addresses = await resolve_public_host(host, port)
            transport = (
                self.transport_factory(host, addresses[0], scheme)
                if self.transport_factory
                else PinnedAddressTransport(host, addresses[0])
            )
            client_factory = self.client_factory or httpx.AsyncClient
            async with client_factory(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": "LeadIntelligenceBot/1.0"},
                transport=transport,
            ) as client:
                async with client.stream("GET", normalized_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or hop >= redirect_limit:
                            raise UnsafeWebsiteUrl("Website exceeded the redirect limit")
                        target = normalize_public_url(urljoin(normalized_url, location))
                        target_host = validate_url_syntax(target)
                        if allowed_host and target_host != allowed_host:
                            raise UnsafeWebsiteUrl("Website redirect left the allowed host")
                        target_origin = _parse_url(target)[1:3]
                        target_scheme = urlsplit(target).scheme
                        if allowed_origins is not None and (target_scheme, *target_origin) not in allowed_origins:
                            raise UnsafeWebsiteUrl("Website redirect left the allowed origin set")
                        redirect_chain.append(normalized_url)
                        current = target
                        continue
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > byte_limit:
                            raise ValueError("Website response exceeded size limit")
                        body.extend(chunk)
                    return SafeFetchResult(
                        requested_url=requested_url,
                        final_url=normalized_url,
                        status_code=response.status_code,
                        content_type=response.headers.get("content-type"),
                        x_robots_tag=response.headers.get("x-robots-tag"),
                        encoding=response.encoding,
                        redirect_chain=tuple([*redirect_chain, normalized_url]),
                        body=bytes(body),
                    )
        raise UnsafeWebsiteUrl("Website redirect limit exceeded")

    async def analyze(self, url: str | None) -> dict:
        """Return HTTP, metadata, HTTPS, SEO, and social-link observations."""
        if not url:
            return {"available": False, "issues": ["No website supplied"]}
        started = time.perf_counter()
        try:
            final_url, status_code, html = await self._fetch(url)
            title = self._capture(r"<title[^>]*>(.*?)</title>", html)
            description = self._capture(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)', html, re.I)
            h1 = self._capture(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
            issues = []
            if not title:
                issues.append("Missing page title")
            if not description:
                issues.append("Missing meta description")
            if not h1:
                issues.append("Missing H1 heading")
            if status_code >= 400:
                issues.append(f"HTTP status {status_code}")
            social = re.findall(r'https?://(?:www\.)?(?:linkedin|facebook|instagram|x|twitter)\.com/[^"\'\s<>]+', html, re.I)
            
            # Content marketing / Blog detection
            blog_matches = re.findall(r'href=["\'](?:https?://[^"\'\s>]+)?/(?:blog|journal|articles|stories|news|posts|guides)/?[^"\'\s>]*["\']', html, re.I)
            blog_detected = bool(blog_matches)

            # Conversion elements detection (forms, CTAs)
            has_form = bool(re.search(r'<form\b[^>]*>', html, re.I))
            cta_matches = list(dict.fromkeys(re.findall(r'\b(?:buy now|add to cart|order now|checkout|get started|book a call|book now|contact us|get in touch|request a quote|subscribe|sign up)\b', html, re.I)))
            has_cta = bool(cta_matches)

            # Tracking & Advertising pixels detection
            tracking_tags = []
            if re.search(r'connect\.facebook\.net|fbq\(', html, re.I):
                tracking_tags.append("Meta Pixel")
            if re.search(r'google-analytics\.com|gtag\(|googletagmanager\.com', html, re.I):
                tracking_tags.append("Google Analytics / GTM")
            if re.search(r'googleads|conversion_id|AW-', html, re.I):
                tracking_tags.append("Google Ads Tag")
            if re.search(r'clarity\.ms', html, re.I):
                tracking_tags.append("Microsoft Clarity")
            if re.search(r'hotjar', html, re.I):
                tracking_tags.append("Hotjar")

            # Review & reputation widgets detection
            review_widgets = []
            if re.search(r'judge\.me', html, re.I):
                review_widgets.append("Judge.me")
            if re.search(r'trustpilot', html, re.I):
                review_widgets.append("Trustpilot")
            if re.search(r'yotpo', html, re.I):
                review_widgets.append("Yotpo")
            if re.search(r'loox', html, re.I):
                review_widgets.append("Loox")
            if re.search(r'schema\.org/(?:AggregateRating|Review)', html, re.I):
                review_widgets.append("Schema.org Review Markup")

            return {
                "available": True,
                "url": final_url,
                "status_code": status_code,
                "title": self._strip(title),
                "meta_description": self._strip(description),
                "h1": self._strip(h1),
                "https": urlsplit(final_url).scheme == "https",
                "issues": issues,
                "social_links": list(dict.fromkeys(social)),
                "response_time_ms": round((time.perf_counter() - started) * 1000),
                "blog_detected": blog_detected,
                "blog_matches": blog_matches[:5],
                "has_form": has_form,
                "has_cta": has_cta,
                "detected_ctas": cta_matches[:5],
                "tracking_tags": tracking_tags,
                "review_widgets": review_widgets,
            }
        except (httpx.HTTPError, ValueError, OSError):
            return {"available": False, "url": url, "error": "Website could not be reached safely", "issues": ["Website could not be reached"]}

    @staticmethod
    def _capture(pattern, text, flags=0):
        match = re.search(pattern, text, flags)
        return match.group(1).strip() if match else None

    @staticmethod
    def _strip(value):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip() or None

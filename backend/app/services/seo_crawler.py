"""Bounded same-site crawler and deterministic page observations."""

from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.services.website import (
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
    SafeFetchResult,
    UnsafeWebsiteUrl,
    WebsiteAnalyzer,
    normalize_public_url,
    sanitized_public_url,
    validate_url_syntax,
)


MAX_PAGES = 25
MAX_DEPTH = 2
MAX_DISCOVERED_URLS = 500
MAX_SITEMAP_URLS = 500
MAX_SITEMAPS = 5
MAX_SITEMAP_DEPTH = 1
MAX_ROBOTS_BYTES = 256_000
MAX_SITEMAP_BYTES = 512_000
MAX_AUDIT_SECONDS = 180
CRAWLER_USER_AGENT = "LeadIntelligenceBot"
TITLE_MIN_CHARS = 10
TITLE_MAX_CHARS = 70
DESCRIPTION_MIN_CHARS = 50
DESCRIPTION_MAX_CHARS = 160
MAX_HTML_TEXT_FIELD = 500
MAX_PAGE_LINKS = 500
MAX_JSONLD_BLOCKS = 30
MAX_JSONLD_TYPES = 50
MAX_JSONLD_BLOCK_CHARS = 64_000
MAX_FINDINGS = 2_000


class SeoCrawlError(RuntimeError):
    """Safe root-crawl failure with bounded public diagnostic fields."""

    def __init__(self, code: str, message: str):
        self.code = code[:120]
        self.message = message[:1000]
        super().__init__(self.message)


@dataclass(frozen=True)
class PageObservation:
    requested_url: str
    page_url: str
    depth: int
    status_code: int
    content_type: str | None
    x_robots_tag: str | None
    redirect_chain: tuple[str, ...]
    title: str | None = None
    title_present: bool = False
    meta_description: str | None = None
    meta_description_present: bool = False
    headings: tuple[tuple[int, str], ...] = ()
    h1_count: int = 0
    h1_empty_count: int = 0
    h2_count: int = 0
    image_count: int = 0
    image_alt_missing: int = 0
    image_alt_empty: int = 0
    language: str | None = None
    canonical_values: tuple[str, ...] = ()
    robots_directives: tuple[str, ...] = ()
    internal_links: tuple[str, ...] = ()
    external_link_count: int = 0
    jsonld_blocks: int = 0
    jsonld_types: tuple[str, ...] = ()
    microdata_count: int = 0
    rdfa_count: int = 0
    parsed_html: bool = True


class PageHTMLParser(HTMLParser):
    """Tolerant standard-library HTML parser that retains only bounded signals."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.description: str | None = None
        self.description_present = False
        self.language: str | None = None
        self.canonicals: list[str] = []
        self.robots: list[str] = []
        self.headings: list[tuple[int, str]] = []
        self._heading_level: int | None = None
        self._heading_parts: list[str] = []
        self._heading_length = 0
        self.heading_counts = [0] * 7
        self.h1_empty_count = 0
        self._in_title = False
        self.title_present = False
        self._title_length = 0
        self._in_jsonld = False
        self._script_parts: list[str] = []
        self._script_length = 0
        self.jsonld_blocks = 0
        self.jsonld_types: set[str] = set()
        self.microdata_count = 0
        self.rdfa_count = 0
        self.image_count = 0
        self.image_alt_missing = 0
        self.image_alt_empty = 0
        self.internal_links: set[str] = set()
        self.external_links: set[str] = set()

    @staticmethod
    def _attrs(attrs):
        return {key.lower(): value for key, value in attrs if key}

    def handle_starttag(self, tag, attrs):
        attributes = self._attrs(attrs)
        tag = tag.lower()
        if tag == "html":
            self.language = self._clean(attributes.get("lang"))
        elif tag == "title":
            self._in_title = True
            self.title_present = True
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading_level = int(tag[1])
            self._heading_parts = []
            self._heading_length = 0
        elif tag == "meta":
            name = (attributes.get("name") or attributes.get("http-equiv") or "").strip().lower()
            content = self._clean(attributes.get("content"))
            if name == "description" and not self.description_present:
                self.description = content
                self.description_present = True
            if name in {"robots", "googlebot", "bingbot"} and content:
                self.robots.extend(item.strip().lower() for item in re.split(r"[,\s]+", content) if item.strip())
        elif tag == "link" and "canonical" in (attributes.get("rel") or "").lower().split():
            href = (attributes.get("href") or "").strip()
            if href and len(self.canonicals) < 10:
                self.canonicals.append(href[:2048])
        elif tag == "img":
            self.image_count += 1
            if "alt" not in attributes:
                self.image_alt_missing += 1
            elif attributes["alt"] == "":
                self.image_alt_empty += 1
        elif tag == "a":
            href = (attributes.get("href") or "").strip()
            if href and len(self.internal_links) + len(self.external_links) < MAX_PAGE_LINKS:
                self._collect_link(href)
        if "itemscope" in attributes:
            self.microdata_count += 1
        if any(key in attributes for key in ("typeof", "vocab", "property")):
            self.rdfa_count += 1
        if tag == "script" and (attributes.get("type") or "").split(";", 1)[0].strip().lower() == "application/ld+json":
            self._in_jsonld = self.jsonld_blocks < MAX_JSONLD_BLOCKS
            if self._in_jsonld:
                self.jsonld_blocks += 1
                self._script_parts = []
                self._script_length = 0

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self._heading_level is not None:
            value = self._clean("".join(self._heading_parts)) or ""
            self.heading_counts[self._heading_level] += 1
            if self._heading_level == 1 and not value:
                self.h1_empty_count += 1
            if len(self.headings) < 100:
                self.headings.append((self._heading_level, value[:MAX_HTML_TEXT_FIELD]))
            self._heading_level = None
            self._heading_parts = []
            self._heading_length = 0
        elif tag == "script" and self._in_jsonld:
            self._read_jsonld("".join(self._script_parts))
            self._in_jsonld = False
            self._script_parts = []
            self._script_length = 0

    def handle_data(self, data):
        if self._in_title and self._title_length < MAX_HTML_TEXT_FIELD:
            chunk = data[:MAX_HTML_TEXT_FIELD - self._title_length]
            self.title_parts.append(chunk)
            self._title_length += len(chunk)
        if self._heading_level is not None and self._heading_length < MAX_HTML_TEXT_FIELD:
            chunk = data[:MAX_HTML_TEXT_FIELD - self._heading_length]
            self._heading_parts.append(chunk)
            self._heading_length += len(chunk)
        if self._in_jsonld and self._script_length < MAX_JSONLD_BLOCK_CHARS:
            chunk = data[:MAX_JSONLD_BLOCK_CHARS - self._script_length]
            self._script_parts.append(chunk)
            self._script_length += len(chunk)

    @staticmethod
    def _clean(value: str | None) -> str | None:
        if value is None:
            return None
        return re.sub(r"\s+", " ", value).strip()[:MAX_HTML_TEXT_FIELD] or None

    def _collect_link(self, href: str) -> None:
        # URL resolution is performed by the crawler, which knows the audited origin.
        self.internal_links.add(href[:2048])

    def _read_jsonld(self, text: str) -> None:
        if not text.strip():
            return
        try:
            payload = json.loads(text[:MAX_JSONLD_BLOCK_CHARS])
        except (ValueError, TypeError):
            return
        stack = [(payload, 0)]
        visited = 0
        while stack and visited < 500:
            value, depth = stack.pop()
            visited += 1
            if depth > 20:
                continue
            if isinstance(value, dict):
                types = value.get("@type")
                if isinstance(types, str):
                    self.jsonld_types.add(types[:120])
                elif isinstance(types, list):
                    self.jsonld_types.update(item[:120] for item in types if isinstance(item, str))
                stack.extend((item, depth + 1) for item in value.values() if isinstance(item, (dict, list)))
            elif isinstance(value, list):
                stack.extend((item, depth + 1) for item in value if isinstance(item, (dict, list)))
            if len(self.jsonld_types) >= MAX_JSONLD_TYPES:
                break

    @property
    def title(self) -> str | None:
        return self._clean("".join(self.title_parts))


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(normalize_public_url(url))
    return parsed.scheme, parsed.hostname or "", parsed.port or (443 if parsed.scheme == "https" else 80)


def _same_site(url: str, root_host: str, permitted_origins: set[tuple[str, str, int]]) -> bool:
    try:
        normalized = normalize_public_url(url)
        return validate_url_syntax(normalized) == root_host and _origin(normalized) in permitted_origins
    except (UnsafeWebsiteUrl, ValueError):
        return False


@dataclass(frozen=True)
class SeoCrawlResult:
    root_url: str
    pages: tuple[PageObservation, ...]
    observations: tuple[dict, ...]
    findings: tuple[dict, ...]
    pages_discovered: int
    metadata: dict


def _finding(code, category, severity, title, description, page_url=None, evidence=None):
    return {
        "finding_code": code,
        "category": category,
        "severity": severity,
        "title": title[:250],
        "description": description[:1000],
        "page_url": page_url,
        "evidence": evidence or {},
    }


def _finding_for_page(page: PageObservation) -> list[dict]:
    url = page.page_url
    findings = []
    title = page.title
    if not page.title_present:
        findings.append(_finding("TITLE_MISSING", "ON_PAGE", "MEDIUM", "Page has no title", "No non-empty title element was observed.", url, {"title": title}))
    elif not title:
        findings.append(_finding("TITLE_EMPTY", "ON_PAGE", "MEDIUM", "Page title is empty", "A title element is present but contains no text.", url, {"title": None}))
    elif len(title) < TITLE_MIN_CHARS:
        findings.append(_finding("TITLE_SHORT", "ON_PAGE", "LOW", "Page title is unusually short", f"The title has {len(title)} characters; this audit heuristic flags fewer than {TITLE_MIN_CHARS}.", url, {"characters": len(title), "heuristic_threshold": TITLE_MIN_CHARS}))
    elif len(title) > TITLE_MAX_CHARS:
        findings.append(_finding("TITLE_LONG", "ON_PAGE", "LOW", "Page title is unusually long", f"The title has {len(title)} characters; this audit heuristic flags more than {TITLE_MAX_CHARS}.", url, {"characters": len(title), "heuristic_threshold": TITLE_MAX_CHARS}))

    description = page.meta_description
    if not page.meta_description_present:
        findings.append(_finding("META_DESCRIPTION_MISSING", "ON_PAGE", "MEDIUM", "Page has no meta description", "No non-empty meta description was observed.", url, {"meta_description": description}))
    elif not description:
        findings.append(_finding("META_DESCRIPTION_EMPTY", "ON_PAGE", "MEDIUM", "Meta description is empty", "A meta description element is present but contains no text.", url, {"meta_description": None}))
    elif len(description) < DESCRIPTION_MIN_CHARS:
        findings.append(_finding("META_DESCRIPTION_SHORT", "ON_PAGE", "LOW", "Meta description is unusually short", f"The description has {len(description)} characters; this audit heuristic flags fewer than {DESCRIPTION_MIN_CHARS}.", url, {"characters": len(description), "heuristic_threshold": DESCRIPTION_MIN_CHARS}))
    elif len(description) > DESCRIPTION_MAX_CHARS:
        findings.append(_finding("META_DESCRIPTION_LONG", "ON_PAGE", "LOW", "Meta description is unusually long", f"The description has {len(description)} characters; this audit heuristic flags more than {DESCRIPTION_MAX_CHARS}.", url, {"characters": len(description), "heuristic_threshold": DESCRIPTION_MAX_CHARS}))

    if page.h1_count == 0:
        findings.append(_finding("H1_MISSING", "ON_PAGE", "MEDIUM", "Page has no H1 heading", "No H1 element was observed.", url, {"h1_count": 0}))
    elif page.h1_count > 1:
        findings.append(_finding("H1_MULTIPLE", "ON_PAGE", "LOW", "Page has multiple H1 headings", f"{page.h1_count} H1 elements were observed.", url, {"h1_count": page.h1_count}))
    if page.h1_empty_count:
        findings.append(_finding("H1_EMPTY", "ON_PAGE", "LOW", "Page has an empty H1 heading", "At least one H1 element contains no text.", url, {"h1_count": page.h1_count, "empty_h1_count": page.h1_empty_count}))
    heading_levels = [level for level, _ in page.headings]
    if any(current > previous + 1 for previous, current in zip(heading_levels, heading_levels[1:])):
        findings.append(_finding("HEADING_LEVEL_SKIP", "ON_PAGE", "INFO", "Heading levels skip a level", "A heading level increases by more than one between adjacent headings.", url, {"heading_levels": heading_levels[:50]}))

    if page.status_code >= 400:
        findings.append(_finding("HTTP_ERROR_STATUS", "TECHNICAL", "HIGH" if page.status_code >= 500 else "MEDIUM", "Page returned an HTTP error status", f"The fetched page returned HTTP {page.status_code}.", url, {"status_code": page.status_code}))
    if len(page.redirect_chain) > 2:
        findings.append(_finding("REDIRECT_CHAIN_LONG", "TECHNICAL", "LOW", "Page required multiple redirects", f"The fetch followed {len(page.redirect_chain) - 1} redirects before reaching the final response.", url, {"redirect_count": len(page.redirect_chain) - 1}))
    if urlsplit(page.page_url).scheme != "https":
        findings.append(_finding("PAGE_NOT_HTTPS", "TECHNICAL", "LOW", "Page was fetched over HTTP", "The observed page URL does not use HTTPS.", url, {"scheme": urlsplit(page.page_url).scheme}))
    if not page.language:
        findings.append(_finding("LANGUAGE_MISSING", "ON_PAGE", "LOW", "HTML language is not declared", "No non-empty lang attribute was observed on the html element.", url, {"language": page.language}))

    if not page.canonical_values:
        findings.append(_finding("CANONICAL_MISSING", "INDEXABILITY", "INFO", "Page has no canonical declaration", "No canonical link declaration was observed." , url, {"canonical_count": 0}))
    elif len(page.canonical_values) > 1:
        findings.append(_finding("CANONICAL_MULTIPLE", "INDEXABILITY", "MEDIUM", "Page has multiple canonical declarations", f"{len(page.canonical_values)} canonical declarations were observed.", url, {"canonical_count": len(page.canonical_values)}))
    for raw in page.canonical_values:
        try:
            target = normalize_public_url(urljoin(page.page_url, raw), remove_query=True)
            same_origin = _origin(target) == _origin(page.page_url)
            if not same_origin:
                findings.append(_finding("CANONICAL_EXTERNAL", "INDEXABILITY", "INFO", "Canonical points outside the audited origin", "An external canonical target was observed; this is recorded without assuming it is incorrect.", url, {"canonical_url": sanitized_public_url(target)}))
        except (UnsafeWebsiteUrl, ValueError):
            findings.append(_finding("CANONICAL_MALFORMED", "INDEXABILITY", "MEDIUM", "Page has a malformed canonical target", "A canonical href could not be normalized as a public HTTP(S) URL.", url, {"canonical_value": "[unusable]"}))

    directives = {item.split(":", 1)[-1].strip().lower() for item in page.robots_directives}
    header_directives = set(re.split(r"[,\s]+", (page.x_robots_tag or "").lower()))
    observed_directives = sorted((directives | header_directives) & {"noindex", "nofollow", "none"})
    if observed_directives:
        findings.append(_finding("ROBOTS_DIRECTIVE_OBSERVED", "INDEXABILITY", "INFO", "Robots directives were observed", "The page or response header declares index or link-following directives; this observation does not assume they are erroneous.", url, {"directives": observed_directives}))
    if page.image_alt_missing:
        findings.append(_finding("IMAGE_ALT_MISSING", "IMAGES", "LOW", "Images are missing ALT attributes", f"{page.image_alt_missing} of {page.image_count} images had no ALT attribute.", url, {"image_count": page.image_count, "alt_missing": page.image_alt_missing, "alt_empty": page.image_alt_empty}))
    if page.image_alt_empty:
        findings.append(_finding("IMAGE_ALT_EMPTY", "IMAGES", "INFO", "Images have empty ALT attributes", f"{page.image_alt_empty} images had an empty ALT attribute, which can be intentional for decorative images.", url, {"image_count": page.image_count, "alt_empty": page.image_alt_empty}))
    if not page.internal_links:
        findings.append(_finding("NO_INTERNAL_LINKS", "LINKS", "INFO", "Page has no internal links", "No normalized in-scope anchor links were observed on this page.", url, {"internal_link_count": 0}))
    return findings


def _page_observation(page: PageObservation) -> dict:
    return {
        "status_code": page.status_code,
        "content_type": page.content_type,
        "x_robots_tag": page.x_robots_tag,
        "redirect_chain": [sanitized_public_url(item) for item in page.redirect_chain],
        "title": page.title,
        "title_present": page.title_present,
        "meta_description": page.meta_description,
        "meta_description_present": page.meta_description_present,
        "headings": [{"level": level, "text": text} for level, text in page.headings[:100]],
        "h1_count": page.h1_count,
        "h2_count": page.h2_count,
        "image_count": page.image_count,
        "image_alt_missing": page.image_alt_missing,
        "image_alt_empty": page.image_alt_empty,
        "language": page.language,
        "canonical_values": [_safe_canonical(page.page_url, item) for item in page.canonical_values],
        "robots_directives": list(page.robots_directives),
        "internal_link_count": len(page.internal_links),
        "internal_links": [sanitized_public_url(link) for link in page.internal_links[:50]],
        "external_link_count": page.external_link_count,
        "jsonld_blocks": page.jsonld_blocks,
        "jsonld_types": list(page.jsonld_types),
        "microdata_count": page.microdata_count,
        "rdfa_count": page.rdfa_count,
    }


def _safe_canonical(page_url: str, value: str) -> str:
    try:
        return sanitized_public_url(urljoin(page_url, value)) if value else ""
    except (UnsafeWebsiteUrl, ValueError):
        return "[invalid canonical URL]"


class SeoCrawler:
    def __init__(self, analyzer: WebsiteAnalyzer | None = None):
        self.analyzer = analyzer or WebsiteAnalyzer()

    async def crawl(self, root_url: str) -> SeoCrawlResult:
        try:
            return await asyncio.wait_for(self._crawl(root_url), timeout=MAX_AUDIT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise SeoCrawlError("CRAWL_TOTAL_TIMEOUT", "Website crawl exceeded its total time limit") from exc

    async def _crawl(self, root_url: str) -> SeoCrawlResult:
        try:
            root_url = normalize_public_url(root_url, remove_query=True)
            root_host = validate_url_syntax(root_url)
            root_origin = _origin(root_url)
        except (UnsafeWebsiteUrl, ValueError) as exc:
            raise SeoCrawlError("UNSAFE_ROOT_URL", "Lead website is not a valid public HTTP(S) URL") from exc
        same_site_origins = {root_origin}
        fetch_origins = {root_origin}
        scheme, host, port = root_origin
        if port == (80 if scheme == "http" else 443):
            upgrade_scheme = "https" if scheme == "http" else "http"
            fetch_origins.add((upgrade_scheme, host, 443 if upgrade_scheme == "https" else 80))

        observations: list[dict] = []
        findings: list[dict] = []
        sitemap_urls: list[str] = []
        sitemap_summary = {"discovered": False, "reachable": False, "valid": False, "sitemap_count": 0, "url_count": 0, "sitemap_urls": [], "errors": []}
        robots_summary = {"discovered": True, "exists": None, "status_code": None, "accessible": False, "disallow_count": 0, "rules_applied": False, "sitemaps": []}
        robots_parser = RobotFileParser()
        robots_url = urljoin(root_url, "/robots.txt")
        try:
            robots = await self.analyzer.fetch_document(
                robots_url, allowed_origins=fetch_origins, max_response_bytes=MAX_ROBOTS_BYTES,
            )
            robots_summary["status_code"] = robots.status_code
            robots_summary["exists"] = 200 <= robots.status_code < 300
            if 200 <= robots.status_code < 300 and self._text_type(robots.content_type):
                lines = robots.body.decode(robots.encoding or "utf-8", errors="replace").splitlines()[:5000]
                robots_parser.set_url(robots_url)
                robots_parser.parse(lines)
                robots_summary["accessible"] = True
                robots_summary["disallow_count"] = sum(1 for line in lines if line.strip().lower().startswith("disallow:") and line.split(":", 1)[-1].strip())
                robots_summary["rules_applied"] = True
                for line in lines:
                    name, sep, value = line.partition(":")
                    if sep and name.strip().lower() == "sitemap" and value.strip():
                        candidate = normalize_public_url(urljoin(robots_url, value.strip()))
                        if _same_site(candidate, root_host, same_site_origins):
                            if len(robots_summary["sitemaps"]) < MAX_SITEMAPS:
                                robots_summary["sitemaps"].append(sanitized_public_url(candidate))
            observations.append(self._observation("robots", {**robots_summary, "redirect_chain": [sanitized_public_url(item) for item in robots.redirect_chain]}))
        except (UnsafeWebsiteUrl, httpx.HTTPError, ValueError, OSError) as exc:
            robots_summary["error"] = type(exc).__name__
            observations.append(self._observation("robots", robots_summary))

        sitemap_candidates = [*robots_summary["sitemaps"], *(
            urljoin(root_url, path) for path in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml")
        )]
        sitemap_seen: set[str] = set()
        sitemap_queue = deque((normalize_public_url(url), 0) for url in sitemap_candidates)
        while sitemap_queue and sitemap_summary["sitemap_count"] < MAX_SITEMAPS:
            sitemap_url, sitemap_depth = sitemap_queue.popleft()
            if sitemap_url in sitemap_seen:
                continue
            sitemap_seen.add(sitemap_url)
            if not _same_site(sitemap_url, root_host, same_site_origins):
                sitemap_summary["errors"].append("out_of_scope_sitemap")
                continue
            sitemap_summary["discovered"] = True
            sitemap_summary["sitemap_count"] += 1
            sitemap_summary["sitemap_urls"].append(sanitized_public_url(sitemap_url))
            try:
                fetched = await self.analyzer.fetch_document(
                    sitemap_url, allowed_origins=fetch_origins, max_response_bytes=MAX_SITEMAP_BYTES,
                )
                if not 200 <= fetched.status_code < 300:
                    sitemap_summary["errors"].append(f"http_{fetched.status_code}")
                    continue
                if not self._xml_type(fetched.content_type):
                    sitemap_summary["errors"].append("unsupported_content_type")
                    continue
                root = ET.fromstring(fetched.body)
                root_name = root.tag.rsplit("}", 1)[-1].lower()
                locs = [element.text.strip() for element in root.iter() if element.tag.rsplit("}", 1)[-1].lower() == "loc" and element.text and element.text.strip()]
                sitemap_summary["reachable"] = True
                sitemap_summary["valid"] = True
                if root_name == "sitemapindex" and sitemap_depth < MAX_SITEMAP_DEPTH:
                    for loc in locs[:MAX_SITEMAPS]:
                        candidate = normalize_public_url(urljoin(sitemap_url, loc))
                        if _same_site(candidate, root_host, same_site_origins):
                            sitemap_queue.append((candidate, sitemap_depth + 1))
                elif root_name == "urlset":
                    for loc in locs[:max(0, MAX_SITEMAP_URLS - len(sitemap_urls))]:
                        candidate = normalize_public_url(urljoin(sitemap_url, loc))
                        if _same_site(candidate, root_host, same_site_origins):
                            sitemap_urls.append(candidate)
                else:
                    sitemap_summary["valid"] = False
                    sitemap_summary["errors"].append("unknown_root_element")
                sitemap_summary["url_count"] = len(sitemap_urls)
            except (UnsafeWebsiteUrl, httpx.HTTPError, ValueError, OSError, ET.ParseError) as exc:
                sitemap_summary["errors"].append(type(exc).__name__)
        sitemap_summary["errors"] = list(dict.fromkeys(sitemap_summary["errors"]))[:20]
        observations.append(self._observation("sitemap", sitemap_summary))
        if sitemap_summary["discovered"] and not sitemap_summary["valid"]:
            findings.append(_finding("SITEMAP_UNAVAILABLE_OR_INVALID", "CRAWLABILITY", "LOW", "A discovered sitemap was unavailable or invalid", "The sitemap could not be fetched and parsed as a supported XML sitemap within the audit bounds.", root_url, sitemap_summary))

        queue = deque([(root_url, 0)])
        discovered: set[str] = {root_url}
        for sitemap_url in sitemap_urls:
            if len(discovered) >= MAX_DISCOVERED_URLS:
                break
            if sitemap_url not in discovered:
                discovered.add(sitemap_url)
                queue.append((sitemap_url, 1))
        pages: list[PageObservation] = []
        status_by_url: dict[str, int] = {}
        urls_considered = 0
        while queue and urls_considered < MAX_PAGES:
            page_url, depth = queue.popleft()
            urls_considered += 1
            if robots_summary["rules_applied"] and not robots_parser.can_fetch(CRAWLER_USER_AGENT, page_url):
                observations.append(self._observation("robots_blocked_page", {"page_url": sanitized_public_url(page_url), "blocked": True}, page_url))
                continue
            try:
                fetched = await self.analyzer.fetch_document(
                    page_url, allowed_origins=fetch_origins,
                    max_response_bytes=MAX_RESPONSE_BYTES, max_redirects=MAX_REDIRECTS,
                )
            except (UnsafeWebsiteUrl, httpx.HTTPError, ValueError, OSError) as exc:
                if page_url == root_url:
                    raise SeoCrawlError("ROOT_FETCH_FAILED", f"Root website could not be safely fetched ({type(exc).__name__})") from exc
                findings.append(_finding("INTERNAL_PAGE_FETCH_FAILED", "LINKS", "MEDIUM", "An in-scope page could not be fetched", f"A discovered page could not be fetched safely ({type(exc).__name__}).", sanitized_public_url(page_url), {"error_type": type(exc).__name__}))
                continue
            status_by_url[page_url] = fetched.status_code
            if not self._html_type(fetched.content_type):
                observations.append(self._observation("unsupported_page_content_type", {
                    "status_code": fetched.status_code,
                    "content_type": (fetched.content_type or "")[:200],
                    "parsed": False,
                    "redirect_chain": [sanitized_public_url(item) for item in fetched.redirect_chain],
                }, page_url))
                if fetched.status_code >= 400:
                    findings.append(_finding("INTERNAL_PAGE_HTTP_ERROR", "TECHNICAL", "MEDIUM", "An in-scope URL returned an HTTP error", f"The fetched URL returned HTTP {fetched.status_code}.", sanitized_public_url(page_url), {"status_code": fetched.status_code}))
                continue
            parser = PageHTMLParser()
            try:
                parser.feed(fetched.body.decode(fetched.encoding or "utf-8", errors="replace"))
                parser.close()
            except (ValueError, RecursionError):
                findings.append(_finding("HTML_PARSE_FAILED", "TECHNICAL", "LOW", "Page HTML could not be fully parsed", "The response was HTML but the bounded parser could not finish processing it.", sanitized_public_url(page_url), {}))
                continue
            internal_links: set[str] = set()
            external_links: set[str] = set()
            for href in parser.internal_links:
                try:
                    target = normalize_public_url(urljoin(fetched.final_url, href))
                    if _same_site(target, root_host, same_site_origins):
                        internal_links.add(target)
                    else:
                        if urlsplit(target).scheme in {"http", "https"}:
                            external_links.add(target)
                except (UnsafeWebsiteUrl, ValueError):
                    continue
            page = PageObservation(
                requested_url=page_url,
                page_url=sanitized_public_url(fetched.final_url),
                depth=depth,
                status_code=fetched.status_code,
                content_type=fetched.content_type,
                x_robots_tag=fetched.x_robots_tag[:500] if fetched.x_robots_tag else None,
                redirect_chain=fetched.redirect_chain,
                title=parser.title,
                title_present=parser.title_present,
                meta_description=parser.description,
                meta_description_present=parser.description_present,
                headings=tuple(parser.headings[:100]),
                h1_count=parser.heading_counts[1],
                h1_empty_count=parser.h1_empty_count,
                h2_count=parser.heading_counts[2],
                image_count=parser.image_count,
                image_alt_missing=parser.image_alt_missing,
                image_alt_empty=parser.image_alt_empty,
                language=parser.language,
                canonical_values=tuple(parser.canonicals),
                robots_directives=tuple(parser.robots[:50]),
                internal_links=tuple(sorted(internal_links)),
                external_link_count=len(external_links),
                jsonld_blocks=parser.jsonld_blocks,
                jsonld_types=tuple(sorted(parser.jsonld_types)[:MAX_JSONLD_TYPES]),
                microdata_count=parser.microdata_count,
                rdfa_count=parser.rdfa_count,
            )
            pages.append(page)
            observation = _page_observation(page)
            observations.append(self._observation("page", observation, page.page_url))
            findings.extend(_finding_for_page(page))
            if page.requested_url == root_url and urlsplit(root_url).scheme == "http":
                if urlsplit(fetched.final_url).scheme == "https":
                    findings.append(_finding("HTTP_TO_HTTPS_REDIRECT", "TECHNICAL", "INFO", "HTTP redirects to HTTPS", "The root URL was redirected to HTTPS on the same audited host.", page.page_url, {"redirect_chain": [sanitized_public_url(item) for item in fetched.redirect_chain]}))
            if depth < MAX_DEPTH:
                for target in page.internal_links:
                    if len(discovered) >= MAX_DISCOVERED_URLS:
                        break
                    if target in discovered:
                        continue
                    discovered.add(target)
                    queue.append((target, depth + 1))

        # Only report link failures for targets actually requested within the bounds.
        description_counts: dict[str, list[str]] = {}
        for page in pages:
            if page.meta_description:
                description_counts.setdefault(page.meta_description.casefold(), []).append(page.page_url)
        for urls in description_counts.values():
            if len(urls) > 1:
                for page_url in urls:
                    findings.append(_finding("META_DESCRIPTION_DUPLICATE", "ON_PAGE", "LOW", "Meta description is duplicated across crawled pages", f"The same normalized meta description was observed on {len(urls)} pages in this bounded crawl.", page_url, {"duplicate_page_count": len(urls)}))
        for page in pages:
            for target in page.internal_links:
                status = status_by_url.get(target)
                if status is not None and status >= 400:
                    findings.append(_finding("BROKEN_INTERNAL_LINK", "LINKS", "MEDIUM", "An internal link points to an HTTP error", f"The linked page returned HTTP {status} when fetched during this bounded audit.", page.page_url, {"target_url": sanitized_public_url(target), "status_code": status}))
        observations.append(self._observation("crawl_summary", {
            "pages_crawled": len(pages), "pages_discovered": len(discovered),
            "max_pages": MAX_PAGES, "max_depth": MAX_DEPTH,
            "max_discovered_urls": MAX_DISCOVERED_URLS,
        }))
        findings = findings[:MAX_FINDINGS]
        if not pages:
            raise SeoCrawlError("NO_HTML_PAGES", "No HTML pages could be crawled within the configured limits")
        return SeoCrawlResult(
            root_url=sanitized_public_url(root_url),
            pages=tuple(pages),
            observations=tuple(observations),
            findings=tuple(findings),
            pages_discovered=len(discovered),
            metadata={"robots": robots_summary, "sitemap": sitemap_summary, "max_pages": MAX_PAGES, "max_depth": MAX_DEPTH},
        )

    @staticmethod
    def _observation(observation_type: str, value: dict, page_url: str | None = None) -> dict:
        return {"observation_type": observation_type, "page_url": sanitized_public_url(page_url) if page_url else None, "observed_value": value}

    @staticmethod
    def _html_type(content_type: str | None) -> bool:
        return (content_type or "").split(";", 1)[0].strip().lower() == "text/html"

    @staticmethod
    def _text_type(content_type: str | None) -> bool:
        kind = (content_type or "").split(";", 1)[0].strip().lower()
        return kind in {"text/plain", "text/robots"}

    @staticmethod
    def _xml_type(content_type: str | None) -> bool:
        kind = (content_type or "").split(";", 1)[0].strip().lower()
        return kind in {"application/xml", "text/xml", "application/rss+xml"} or kind.endswith("+xml")

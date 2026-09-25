"""Bounded HTTP website inspection service."""
import re, time
from urllib.parse import urlparse
import httpx

class WebsiteAnalyzer:
    """Inspect a public company website without relying on external APIs."""
    def __init__(self, timeout: float = 10): self.timeout = timeout
    async def analyze(self, url: str | None) -> dict:
        """Return HTTP, metadata, HTTPS, SEO, and social-link observations."""
        if not url: return {"available": False, "issues": ["No website supplied"]}
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True, headers={"User-Agent": "LeadIntelligenceBot/1.0"}) as client:
                response = await client.get(url)
            html = response.text[:1_000_000]
            title = self._capture(r"<title[^>]*>(.*?)</title>", html)
            description = self._capture(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)', html, re.I)
            h1 = self._capture(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
            issues = []
            if not title: issues.append("Missing page title")
            if not description: issues.append("Missing meta description")
            if not h1: issues.append("Missing H1 heading")
            if response.status_code >= 400: issues.append(f"HTTP status {response.status_code}")
            social = re.findall(r'https?://(?:www\.)?(?:linkedin|facebook|instagram|x|twitter)\.com/[^"\'\s<>]+', html, re.I)
            return {"available": True, "url": str(response.url), "status_code": response.status_code, "title": self._strip(title), "meta_description": self._strip(description), "h1": self._strip(h1), "https": urlparse(str(response.url)).scheme == "https", "issues": issues, "social_links": list(dict.fromkeys(social)), "response_time_ms": round((time.perf_counter()-started)*1000)}
        except (httpx.HTTPError, ValueError) as exc:
            return {"available": False, "url": url, "error": str(exc)[:300], "issues": ["Website could not be reached"]}
    @staticmethod
    def _capture(pattern, text, flags=0):
        match = re.search(pattern, text, flags)
        return match.group(1).strip() if match else None
    @staticmethod
    def _strip(value):
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip() or None

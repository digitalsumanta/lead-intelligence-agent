"""Focused SEO contracts, crawler boundaries, and durable lifecycle tests."""
from uuid import UUID

import pytest
from sqlalchemy import select
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import Base, Lead, SeoAuditRun, SeoFinding, SeoObservation
from app.services.seo import claim_seo_runs, create_seo_run, execute_seo_run
from app.services.seo_crawler import PageHTMLParser, SeoCrawler, SeoCrawlResult, _finding_for_page, PageObservation
from app.services import seo_crawler
from app.services.website import SafeFetchResult


def _response(url: str, body: bytes = b"", *, status=200, kind="text/html", final=None, chain=None):
    return SafeFetchResult(url, final or url, status, kind, None, "utf-8", tuple(chain or [final or url]), body)


class FakeAnalyzer:
    def __init__(self, docs):
        self.docs = docs
        self.calls = []

    async def fetch_document(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.docs.get(url)
        if isinstance(item, Exception):
            raise item
        return item or _response(url, b"", status=404, kind="text/plain")


def test_empty_title_description_canonical_and_heading_checks_are_deterministic():
    parser = PageHTMLParser()
    parser.feed('<html><head><title></title><meta name="description" content=""></head><body><h1></h1><link rel="canonical" href="javascript:alert(1)"></body></html>')
    page = PageObservation(
        requested_url="https://example.com/", page_url="https://example.com/", depth=0,
        status_code=200, content_type="text/html", x_robots_tag=None, redirect_chain=("https://example.com/",),
        title=parser.title, title_present=parser.title_present,
        meta_description=parser.description, meta_description_present=parser.description_present,
        headings=tuple(parser.headings), h1_count=parser.heading_counts[1], h1_empty_count=parser.h1_empty_count,
        canonical_values=tuple(parser.canonicals), parsed_html=True,
    )
    codes = {item["finding_code"] for item in _finding_for_page(page)}
    assert {"TITLE_EMPTY", "META_DESCRIPTION_EMPTY", "H1_EMPTY", "CANONICAL_MALFORMED"} <= codes
    assert "javascript" not in repr(_finding_for_page(page)).lower()


@pytest.mark.asyncio
async def test_crawler_is_same_host_bounded_and_uses_pinned_fetch_boundary():
    root = "https://example.com/"
    docs = {
        "https://example.com/robots.txt": _response("https://example.com/robots.txt", b"User-agent: *\nDisallow: /private", kind="text/plain"),
        "https://example.com/sitemap.xml": _response("https://example.com/sitemap.xml", b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.com/sitemap-page</loc></url></urlset>', kind="application/xml"),
        root: _response(root, b'<html lang="en"><head><title>Business home page</title><meta name="description" content="A sufficiently long description for this example business website content."><script type="application/ld+json">{"@type":"Organization"}</script></head><body><h1>Welcome</h1><img src="/logo.png"><a href="/about#top">About</a><a href="/about#second">About again</a><a href="/private">Private</a><a href="https://elsewhere.test/path">Outside</a></body></html>'),
        "https://example.com/about": _response("https://example.com/about", b"<html><title>About this business page</title><h1>About</h1></html>"),
        "https://example.com/sitemap-page": _response("https://example.com/sitemap-page", b"<html><title>Sitemap page title</title><h1>Sitemap</h1></html>"),
    }
    analyzer = FakeAnalyzer(docs)
    result = await SeoCrawler(analyzer).crawl(root)
    fetched_pages = [url for url, _ in analyzer.calls if url not in {"https://example.com/robots.txt", "https://example.com/sitemap.xml"}]
    assert len(result.pages) <= 25
    assert all("elsewhere.test" not in url for url in fetched_pages)
    assert fetched_pages.count("https://example.com/about") == 1
    assert "https://example.com/private" not in fetched_pages
    assert all(call[1].get("allowed_origins") for call in analyzer.calls)
    assert result.pages_discovered <= 500
    assert not any("<html" in repr(item) for item in result.observations)
    root_page = next(page for page in result.pages if page.page_url == root)
    assert root_page.language == "en"
    assert root_page.image_alt_missing == 1
    assert root_page.jsonld_types == ("Organization",)


@pytest.mark.asyncio
async def test_crawler_page_cap_limits_fetch_attempts_not_only_successful_html(monkeypatch):
    monkeypatch.setattr(seo_crawler, "MAX_PAGES", 2)
    root = "https://example.com/"
    links = "".join(f'<a href="/p{i}">Page</a>' for i in range(10))
    docs = {
        "https://example.com/robots.txt": _response("https://example.com/robots.txt", b"User-agent: *", kind="text/plain"),
        root: _response(root, f"<html><title>Home page title</title>{links}</html>".encode()),
        "https://example.com/p0": _response("https://example.com/p0", b"bad", kind="application/pdf"),
    }
    analyzer = FakeAnalyzer(docs)
    await SeoCrawler(analyzer).crawl(root)
    page_requests = [url for url, _ in analyzer.calls if url != "https://example.com/robots.txt" and "sitemap" not in url]
    assert len(page_requests) <= 2


@pytest.mark.asyncio
async def test_seo_api_auth_and_run_lifecycle(session_factory, monkeypatch):
    async def override_db():
        async with session_factory() as session:
            yield session
    monkeypatch.setattr(settings, "api_auth_token", "seo-test-token")
    app.dependency_overrides[get_db] = override_db
    try:
        async with session_factory() as db:
            lead = Lead(company_name="SEO test", website="https://example.com")
            db.add(lead)
            await db.commit()
            lead_id = lead.id
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            denied = await client.post(f"/api/leads/{lead_id}/seo/runs")
            assert denied.status_code == 401
            queued = await client.post(f"/api/leads/{lead_id}/seo/runs", headers={"Authorization": "Bearer seo-test-token"})
            assert queued.status_code == 202
            assert queued.json()["run"]["audit_version"] == "seo-audit-v1"
            duplicate = await client.post(f"/api/leads/{lead_id}/seo/runs", headers={"Authorization": "Bearer seo-test-token"})
            assert duplicate.status_code == 409
        async with session_factory() as db:
            run = await db.get(SeoAuditRun, UUID(queued.json()["run"]["id"]))
            assert (await claim_seo_runs(db, 1)) == [run.id]
        class FailedCrawler:
            async def crawl(self, _):
                from app.services.seo_crawler import SeoCrawlError
                raise SeoCrawlError("ROOT_FETCH_FAILED", "safe failure")
        class DisabledPageSpeed:
            async def collect(self, _):
                raise AssertionError("PageSpeed must not run after crawl failure")
        await execute_seo_run(session_factory, run.id, crawler=FailedCrawler(), pagespeed_provider=DisabledPageSpeed())
        async with session_factory() as db:
            failed = await db.get(SeoAuditRun, run.id)
            assert failed.status == "FAILED"
            assert failed.error_code == "ROOT_FETCH_FAILED"
            assert failed.completed_at is not None
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_seo_models_enforce_run_and_observation_integrity():
    assert {"seo_audit_runs", "seo_observations", "seo_findings"} <= set(Base.metadata.tables)
    run_fk_targets = {fk.target_fullname for fk in SeoObservation.__table__.foreign_keys}
    finding_fk_targets = {fk.target_fullname for fk in SeoFinding.__table__.foreign_keys}
    assert "seo_audit_runs.id" in run_fk_targets
    assert "seo_observations.id" in finding_fk_targets
    assert any(index.unique and "seo_run_id" in {column.name for column in index.columns} for index in SeoObservation.__table__.indexes)


@pytest.mark.asyncio
async def test_completed_run_persists_only_normalized_run_scoped_records(session_factory):
    async with session_factory() as db:
        lead = Lead(company_name="SEO persistence", website="https://example.com")
        db.add(lead)
        await db.commit()
        run = await create_seo_run(db, lead.id)
        assert await claim_seo_runs(db, 1) == [run.id]

    page_url = "https://example.com/"
    observation = {"observation_type": "page", "page_url": page_url, "observed_value": {"title": "A normalized title"}}
    finding = {"finding_code": "TITLE_SHORT", "category": "ON_PAGE", "severity": "LOW", "title": "Short title", "description": "A deterministic heuristic matched.", "page_url": page_url, "evidence": {"characters": 5}}
    class Crawler:
        async def crawl(self, _):
            return SeoCrawlResult(page_url, (object(),), (observation,), (finding,), 1, {"bounded": True})
    class PageSpeedUnavailable:
        async def collect(self, _):
            raise RuntimeError("mocked provider unavailable")
    await execute_seo_run(session_factory, run.id, crawler=Crawler(), pagespeed_provider=PageSpeedUnavailable())
    async with session_factory() as db:
        saved_run = await db.get(SeoAuditRun, run.id)
        saved_observations = (await db.execute(select(SeoObservation).where(SeoObservation.seo_run_id == run.id))).scalars().all()
        saved_findings = (await db.execute(select(SeoFinding).where(SeoFinding.seo_run_id == run.id))).scalars().all()
        assert saved_run.status == "COMPLETED"
        assert saved_run.metadata_json["pagespeed_status"] == "unavailable"
        assert all("html" not in repr(item.observed_value).lower() for item in saved_observations)
        page_observation = next(item for item in saved_observations if item.observation_type == "page")
        assert saved_findings[0].observation_id == page_observation.id
        assert saved_findings[0].seo_run_id == saved_run.id

"""Independent durable SEO audit lifecycle and persistence."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.enrichment import LeadInput, ProviderResultStatus
from app.models.entities import Lead, SeoAuditRun, SeoFinding, SeoObservation, now_utc
from app.providers.pagespeed import PageSpeedProvider
from app.services.provider_execution import ProviderExecutionService
from app.services.seo_crawler import SeoCrawler, SeoCrawlError
from app.services.website import UnsafeWebsiteUrl, normalize_public_url, sanitized_public_url

SEO_AUDIT_VERSION = "seo-audit-v1"
SEO_RUN_LEASE_SECONDS = 300
PAGESPEED_TIMEOUT_SECONDS = 50


class SeoServiceError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 422):
        self.code, self.message, self.status_code = code, message, status_code
        super().__init__(message)


async def create_seo_run(db: AsyncSession, lead_id: UUID) -> SeoAuditRun:
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise SeoServiceError("LEAD_NOT_FOUND", "Lead not found", 404)
    if not lead.website:
        raise SeoServiceError("SEO_WEBSITE_REQUIRED", "Lead has no website to audit", 422)
    try:
        normalized = normalize_public_url(lead.website, remove_query=True)
    except (UnsafeWebsiteUrl, ValueError) as exc:
        raise SeoServiceError("SEO_WEBSITE_INVALID", "Lead website is not a valid public HTTP(S) URL", 422) from exc
    run = SeoAuditRun(
        lead_id=lead.id,
        status="QUEUED",
        audit_version=SEO_AUDIT_VERSION,
        root_url=sanitized_public_url(lead.website)[:2048],
        normalized_root_url=normalized[:2048],
        metadata_json={"execution": "durable_worker"},
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def claim_seo_runs(db: AsyncSession, limit: int, now=None) -> list[UUID]:
    now = now or now_utc()
    rows = (await db.execute(
        select(SeoAuditRun).where(SeoAuditRun.status == "QUEUED")
        .order_by(SeoAuditRun.created_at, SeoAuditRun.id).limit(limit)
        .with_for_update(skip_locked=True)
    )).scalars().all()
    ids = []
    for run in rows:
        run.status = "RUNNING"
        run.started_at = now
        run.lease_expires_at = now + timedelta(seconds=SEO_RUN_LEASE_SECONDS)
        ids.append(run.id)
    if ids:
        await db.commit()
    return ids


async def recover_expired_seo_runs(db: AsyncSession, now=None) -> int:
    now = now or now_utc()
    result = await db.execute(
        update(SeoAuditRun)
        .where(SeoAuditRun.status == "RUNNING", SeoAuditRun.lease_expires_at < now)
        .values(status="FAILED", error_code="WORKER_INTERRUPTED", error_message="SEO audit worker lease expired", completed_at=now, lease_expires_at=None)
    )
    await db.commit()
    return result.rowcount or 0


def _observation_key(observation: dict) -> str:
    canonical = json.dumps(observation, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def execute_seo_run(session_factory: async_sessionmaker, run_id: UUID, *, crawler=None, pagespeed_provider=None) -> None:
    """Execute a bounded crawl, optional normalized PageSpeed request, then persist atomically."""
    async with session_factory() as db:
        run = await db.get(SeoAuditRun, run_id)
        if run is None or run.status != "RUNNING":
            return
        lead = await db.get(Lead, run.lead_id)
        if lead is None:
            run.status = "FAILED"
            run.error_code, run.error_message = "LEAD_NOT_FOUND", "Lead no longer exists"
            run.completed_at, run.lease_expires_at = now_utc(), None
            await db.commit()
            return
        lead_context = LeadInput(lead_id=lead.id, enrichment_run_id=None, company_name=lead.company_name, website=run.normalized_root_url)
        db.expunge(run)
        db.expunge(lead)

    try:
        crawl_result = await (crawler or SeoCrawler()).crawl(lead_context.website)
        observations = list(crawl_result.observations)
        findings = list(crawl_result.findings)
        pagespeed_status = "disabled"
        if pagespeed_provider is None:
            pagespeed_provider = PageSpeedProvider(ProviderExecutionService(session_factory))
        try:
            result = await asyncio.wait_for(pagespeed_provider.collect(lead_context), timeout=PAGESPEED_TIMEOUT_SECONDS)
            if result.status == ProviderResultStatus.SUCCEEDED:
                pagespeed_status = "available"
                for item in result.observations:
                    observations.append({
                        "observation_type": item.key,
                        "page_url": crawl_result.root_url,
                        "observed_value": {"value": item.value, "kind": item.kind.value, "observed_at": item.observed_at.isoformat()},
                    })
                findings.extend(_pagespeed_findings(result.observations, crawl_result.root_url))
            else:
                pagespeed_status = "unavailable"
                observations.append({"observation_type": "pagespeed_status", "page_url": crawl_result.root_url, "observed_value": {"status": "unavailable"}})
        except Exception as exc:
            pagespeed_status = "unavailable"
            observations.append({"observation_type": "pagespeed_status", "page_url": crawl_result.root_url, "observed_value": {"status": "unavailable", "error_type": type(exc).__name__[:100]}})

        metadata = {**crawl_result.metadata, "pagespeed_status": pagespeed_status}
        async with session_factory() as db:
            run = await db.get(SeoAuditRun, run_id)
            if run is None or run.status != "RUNNING":
                return
            observation_rows: dict[tuple[str, str | None], SeoObservation] = {}
            seen_observation_keys: set[str] = set()
            for item in observations:
                key = _observation_key(item)
                if key in seen_observation_keys:
                    continue
                seen_observation_keys.add(key)
                row = SeoObservation(
                    seo_run_id=run.id,
                    observation_key=key,
                    observation_type=str(item["observation_type"])[:120],
                    page_url=item.get("page_url"),
                    observed_value=item["observed_value"],
                )
                db.add(row)
                observation_rows[(row.observation_type, row.page_url)] = row
            await db.flush()
            for finding in findings:
                observation = observation_rows.get(("page", finding.get("page_url")))
                db.add(SeoFinding(
                    seo_run_id=run.id,
                    observation_id=observation.id if observation else None,
                    page_url=finding.get("page_url"),
                    finding_code=finding["finding_code"][:120],
                    category=finding["category"], severity=finding["severity"],
                    title=finding["title"][:250], description=finding["description"][:1000],
                    evidence=finding.get("evidence") or {},
                ))
            run.status = "COMPLETED"
            run.pages_crawled = len(crawl_result.pages)
            run.pages_discovered = crawl_result.pages_discovered
            run.findings_count = len(findings)
            run.metadata_json = metadata
            run.completed_at, run.lease_expires_at = now_utc(), None
            await db.commit()
    except Exception as exc:
        async with session_factory() as db:
            run = await db.get(SeoAuditRun, run_id)
            if run and run.status == "RUNNING":
                run.status = "FAILED"
                run.error_code = exc.code if isinstance(exc, SeoCrawlError) else "SEO_AUDIT_FAILED"
                run.error_message = exc.message if isinstance(exc, SeoCrawlError) else f"SEO audit failed ({type(exc).__name__})"
                run.completed_at, run.lease_expires_at = now_utc(), None
                await db.commit()


def _pagespeed_findings(observations, page_url: str) -> list[dict]:
    results = []
    for observation in observations:
        kind = observation.key
        value = observation.value
        score_names = {
            "pagespeed_performance_score": ("performance", "PERFORMANCE"),
            "pagespeed_seo_score": ("SEO", "ON_PAGE"),
        }
        if kind in score_names and isinstance(value, (int, float)) and value < 0.5:
            label, category = score_names[kind]
            results.append({"finding_code": f"PAGESPEED_{kind.upper()}", "category": category, "severity": "MEDIUM", "title": f"PageSpeed {label} category score is below 0.50", "description": "The normalized PageSpeed category score was below the audit's configured heuristic threshold.", "page_url": page_url, "evidence": {"score": value, "heuristic_threshold": 0.5}})
    return results

"""Read-time mapping from the current completed SEO run to sales actions."""
from __future__ import annotations

from collections import defaultdict
from uuid import UUID, uuid5, NAMESPACE_URL

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Lead, SalesActivity, SalesActivityType, SalesTask, ServiceCatalogEntry,
)
from app.models.entities import SeoAuditRun, SeoFinding
from app.schemas.seo_opportunities import SeoSalesOpportunity
from app.services.sales_tasks import stage_commercial_context


MAX_SEO_SALES_OPPORTUNITIES = 10
MAX_GLOBAL_SEO_SALES_OPPORTUNITIES = 200
MAX_GLOBAL_CURRENT_RUNS = 100
MAX_FINDINGS_PER_RUN = 2500
MAX_GLOBAL_FINDINGS = 20_000
MAX_GLOBAL_FINDINGS_PER_RUN = 200
MAX_OPPORTUNITY_EVIDENCE = 10
MAX_AFFECTED_PAGES = 10
AUDIT_VERSION = "seo-audit-v1"

SEVERITY_ORDER = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}

# Only finding codes with deterministic, actionable meanings enter these groups.
FINDING_TO_TYPE = {
    "PAGE_NOT_HTTPS": "TECHNICAL_SEO",
    "HTTP_ERROR_STATUS": "TECHNICAL_SEO",
    "REDIRECT_CHAIN_LONG": "TECHNICAL_SEO",
    "TITLE_MISSING": "ON_PAGE_SEO", "TITLE_EMPTY": "ON_PAGE_SEO",
    "TITLE_SHORT": "ON_PAGE_SEO", "TITLE_LONG": "ON_PAGE_SEO",
    "META_DESCRIPTION_MISSING": "ON_PAGE_SEO", "META_DESCRIPTION_EMPTY": "ON_PAGE_SEO",
    "META_DESCRIPTION_SHORT": "ON_PAGE_SEO", "META_DESCRIPTION_LONG": "ON_PAGE_SEO",
    "META_DESCRIPTION_DUPLICATE": "ON_PAGE_SEO",
    "H1_MISSING": "ON_PAGE_SEO", "H1_EMPTY": "ON_PAGE_SEO",
    "H1_MULTIPLE": "ON_PAGE_SEO", "HEADING_LEVEL_SKIP": "ON_PAGE_SEO",
    "LANGUAGE_MISSING": "ON_PAGE_SEO",
    "CANONICAL_MISSING": "INDEXABILITY", "CANONICAL_MULTIPLE": "INDEXABILITY",
    "CANONICAL_MALFORMED": "INDEXABILITY", "CANONICAL_EXTERNAL": "INDEXABILITY",
    "PAGESPEED_PAGESPEED_SEO_SCORE": "ON_PAGE_SEO",
    "PAGESPEED_PAGESPEED_PERFORMANCE_SCORE": "PERFORMANCE",
    "BROKEN_INTERNAL_LINK": "LINKS", "NO_INTERNAL_LINKS": "LINKS",
    "IMAGE_ALT_MISSING": "IMAGES", "IMAGE_ALT_EMPTY": "IMAGES",
    "SITEMAP_UNAVAILABLE_OR_INVALID": "CRAWLABILITY",
    "INTERNAL_PAGE_FETCH_FAILED": "CRAWLABILITY",
    "INTERNAL_PAGE_HTTP_ERROR": "CRAWLABILITY",
    "HTML_PARSE_FAILED": "CRAWLABILITY",
}

TYPE_COPY = {
    "TECHNICAL_SEO": ("Technical SEO review", "Technical website findings indicate areas for a human review."),
    "ON_PAGE_SEO": ("On-page SEO coverage issue", "Page titles, descriptions, headings, or language declarations triggered deterministic checks."),
    "INDEXABILITY": ("Indexability review opportunity", "Canonical or indexability declarations triggered deterministic checks."),
    "PERFORMANCE": ("Website performance optimization opportunity", "Normalized PageSpeed findings indicate a performance review may be useful."),
    "STRUCTURED_DATA": ("Structured data review opportunity", "Persisted structured-data findings indicate a review may be useful."),
    "LINKS": ("Internal link health opportunity", "Internal-link findings indicate pages or links may need review."),
    "IMAGES": ("Image ALT coverage issue", "Image ALT findings indicate missing or empty attributes across crawled pages."),
    "CRAWLABILITY": ("Website crawlability review opportunity", "Crawl and sitemap findings indicate the website may need technical review."),
}


def _latest_completed_runs(lead_id: UUID | None):
    if lead_id is not None:
        return select(SeoAuditRun, Lead).join(Lead, Lead.id == SeoAuditRun.lead_id).where(
            SeoAuditRun.lead_id == lead_id, SeoAuditRun.status == "COMPLETED",
        ).order_by(SeoAuditRun.created_at.desc(), SeoAuditRun.id.desc()).limit(1)
    ranked = select(
        SeoAuditRun.id.label("run_id"),
        func.row_number().over(
            partition_by=SeoAuditRun.lead_id,
            order_by=(SeoAuditRun.created_at.desc(), SeoAuditRun.id.desc()),
        ).label("run_rank"),
    ).where(SeoAuditRun.status == "COMPLETED").subquery()
    return select(SeoAuditRun, Lead).join(
        ranked, ranked.c.run_id == SeoAuditRun.id,
    ).join(Lead, Lead.id == SeoAuditRun.lead_id).where(
        ranked.c.run_rank == 1,
    ).order_by(SeoAuditRun.created_at.desc(), SeoAuditRun.id.desc()).limit(MAX_GLOBAL_CURRENT_RUNS)


def _marker_id(note: str) -> str | None:
    start = note.find("[seo-opportunity:")
    if start < 0:
        return None
    start += len("[seo-opportunity:")
    end = note.find("]", start)
    return note[start:end] if end > start else None


async def read_seo_sales_opportunities(
    session: AsyncSession,
    *,
    lead_id: UUID | None = None,
) -> list[dict]:
    """Derive a bounded current view; never crawls, calls providers, or mutates state."""
    run_rows = (await session.execute(_latest_completed_runs(lead_id))).all()
    current = [(run, lead) for run, lead in run_rows if run.audit_version == AUDIT_VERSION]
    if not current:
        return []
    runs_by_id = {run.id: (run, lead) for run, lead in current}
    run_ids = list(runs_by_id)
    if lead_id is not None:
        findings = (await session.execute(
            select(SeoFinding).where(SeoFinding.seo_run_id.in_(run_ids))
            .order_by(SeoFinding.seo_run_id, SeoFinding.id).limit(MAX_FINDINGS_PER_RUN)
        )).scalars().all()
    else:
        ranked_findings = select(
            SeoFinding.id.label("finding_id"),
            func.row_number().over(
                partition_by=SeoFinding.seo_run_id,
                order_by=(
                    case((SeoFinding.severity == "HIGH", 0), (SeoFinding.severity == "MEDIUM", 1), (SeoFinding.severity == "LOW", 2), else_=3),
                    SeoFinding.finding_code,
                    SeoFinding.id,
                ),
            ).label("finding_rank"),
        ).where(SeoFinding.seo_run_id.in_(run_ids)).subquery()
        findings = (await session.execute(
            select(SeoFinding).join(ranked_findings, ranked_findings.c.finding_id == SeoFinding.id)
            .where(ranked_findings.c.finding_rank <= MAX_GLOBAL_FINDINGS_PER_RUN)
            .order_by(SeoFinding.seo_run_id, SeoFinding.id).limit(MAX_GLOBAL_FINDINGS)
        )).scalars().all()

    grouped: dict[tuple[UUID, str], list[SeoFinding]] = defaultdict(list)
    for finding in findings:
        opportunity_type = FINDING_TO_TYPE.get(finding.finding_code)
        if opportunity_type is None and finding.finding_code.startswith("PAGESPEED_"):
            opportunity_type = "PERFORMANCE"
        if opportunity_type:
            grouped[(finding.seo_run_id, opportunity_type)].append(finding)

    candidates = []
    for (seo_run_id, opportunity_type), rows in grouped.items():
        run, lead = runs_by_id[seo_run_id]
        title, explanation = TYPE_COPY[opportunity_type]
        rows = sorted(rows, key=lambda item: (item.finding_code, item.page_url or "", str(item.id)))
        page_urls = sorted({item.page_url for item in rows if item.page_url})
        opportunity_id = uuid5(
            NAMESPACE_URL,
            f"lead-intelligence:seo:{lead.id}:{run.id}:{opportunity_type}:SITE",
        )
        evidence_rows = sorted(
            rows,
            key=lambda item: (-SEVERITY_ORDER[item.severity], item.finding_code, item.page_url or "", str(item.id)),
        )[:MAX_OPPORTUNITY_EVIDENCE]
        current_value, current_value_field = stage_commercial_context(lead)
        candidates.append({
            "opportunity_id": opportunity_id,
            "lead_id": lead.id,
            "seo_run_id": run.id,
            "type": opportunity_type,
            "scope": "SITE",
            "title": title,
            "summary": f"{len(rows)} persisted finding(s) across {len(page_urls)} page(s). {explanation}",
            "severity": max((item.severity for item in rows), key=lambda item: SEVERITY_ORDER[item]),
            "affected_page_count": len(page_urls),
            "affected_pages": page_urls[:MAX_AFFECTED_PAGES],
            "finding_count": len(rows),
            "evidence": [{
                "finding_id": item.id,
                "finding_code": item.finding_code,
                "title": item.title,
                "page_url": item.page_url,
                "severity": item.severity,
                "observed_values": item.evidence,
            } for item in evidence_rows],
            "recommended_service_ids": [],
            "suggested_task_type": "OTHER",
            "suggested_action": "Review the linked SEO findings and decide whether to create a sales task.",
            "current_sales_status": lead.sales_status,
            "lead_priority": lead.priority,
            "assigned_to": lead.assigned_to,
            "next_follow_up_at": lead.next_follow_up_at,
            "current_stage_value": current_value,
            "current_stage_value_field": current_value_field,
            "currency": lead.currency,
            "current_task_state": "NO_TASK",
            "related_task_id": None,
            "_finding_rows": rows,
        })

    if not candidates:
        return []

    # Map only services already implied by existing deterministic intelligence rules.
    required_service_ids = set()
    for item in candidates:
        codes = {finding.finding_code for finding in item["_finding_rows"]}
        if item["type"] in {"ON_PAGE_SEO", "INDEXABILITY", "LINKS", "IMAGES", "CRAWLABILITY", "STRUCTURED_DATA"}:
            required_service_ids.add("seo")
        if "PAGE_NOT_HTTPS" in codes:
            required_service_ids.add("website_development")
        if any(code.startswith("PAGESPEED_") and "PERFORMANCE_SCORE" in code for code in codes):
            required_service_ids.update({"website_development", "website_cro"})
    active_services = set((await session.scalars(
        select(ServiceCatalogEntry.service_id).where(
            ServiceCatalogEntry.active.is_(True), ServiceCatalogEntry.service_id.in_(required_service_ids),
        )
    )).all()) if required_service_ids else set()
    for item in candidates:
        codes = {finding.finding_code for finding in item.pop("_finding_rows")}
        services = set()
        if item["type"] in {"ON_PAGE_SEO", "INDEXABILITY", "LINKS", "IMAGES", "CRAWLABILITY", "STRUCTURED_DATA"}:
            services.add("seo")
        if "PAGE_NOT_HTTPS" in codes:
            services.add("website_development")
        if any(code.startswith("PAGESPEED_") and "PERFORMANCE_SCORE" in code for code in codes):
            services.update({"website_development", "website_cro"})
        item["recommended_service_ids"] = sorted(services & active_services)

    candidates.sort(key=lambda item: (
        -SEVERITY_ORDER[item["severity"]], -item["finding_count"], item["type"], str(item["opportunity_id"]),
    ))
    per_lead_counts: dict[UUID, int] = defaultdict(int)
    bounded = []
    for item in candidates:
        if per_lead_counts[item["lead_id"]] >= MAX_SEO_SALES_OPPORTUNITIES:
            continue
        per_lead_counts[item["lead_id"]] += 1
        bounded.append(item)
    bounded = bounded[:MAX_GLOBAL_SEO_SALES_OPPORTUNITIES] if lead_id is None else bounded

    lead_ids = {item["lead_id"] for item in bounded}
    opportunity_ids = {str(item["opportunity_id"]) for item in bounded}
    activities = (await session.scalars(
        select(SalesActivity).where(
            SalesActivity.lead_id.in_(lead_ids),
            SalesActivity.activity_type == SalesActivityType.NOTE,
            SalesActivity.note.like("SEO opportunity task created [%"),
        ).order_by(SalesActivity.occurred_at.desc(), SalesActivity.id.desc()).limit(5000)
    )).all() if lead_ids else []
    activity_by_opportunity = {}
    for activity in activities:
        marker = _marker_id(activity.note)
        if marker in opportunity_ids and marker not in activity_by_opportunity:
            activity_by_opportunity[marker] = activity
    activity_ids = [activity.id for activity in activity_by_opportunity.values()]
    tasks = (await session.scalars(
        select(SalesTask).where(SalesTask.related_activity_id.in_(activity_ids))
        .order_by(SalesTask.created_at.desc(), SalesTask.id.desc()).limit(5000)
    )).all() if activity_ids else []
    task_by_activity = {}
    for task in tasks:
        task_by_activity.setdefault(task.related_activity_id, task)
    for item in bounded:
        activity = activity_by_opportunity.get(str(item["opportunity_id"]))
        task = task_by_activity.get(activity.id) if activity else None
        if task:
            item["current_task_state"] = task.status.value
            item["related_task_id"] = task.id

    return [SeoSalesOpportunity.model_validate(item).model_dump() for item in bounded]

"""Business Research Engine implementation for MindArtDigital lead discovery."""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.models import Lead
from app.services.business_research.providers import (
    MissingProviderConfigurationError,
    provider_registry,
)
from app.services.business_research.schemas import (
    ResearchInput,
    ResearchJobState,
    ResearchResultItem,
    SaveLeadsRequest,
    SaveLeadsResponse,
    SourceEvidence,
    now_utc_iso,
)
from app.services.csv_import import (
    MINDART_TEMPLATE_HEADERS,
    map_row_to_lead_data,
)
from app.services.website import WebsiteAnalyzer

logger = logging.getLogger("lead_agent.business_research.engine")

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")

MINDART_SERVICES = [
    "SEO",
    "Local SEO",
    "Website Development",
    "Website Maintenance",
    "Performance Marketing",
    "Google Ads",
    "Meta Ads",
    "Social Media Marketing",
    "AI Automation",
    "AI Chatbots",
    "Marketing Automation",
]


class BusinessResearchEngine:
    """Orchestrates business discovery, website research, evidence collection, and lead saving."""

    def __init__(
        self,
        session_factory: async_sessionmaker | None = None,
        analyzer: WebsiteAnalyzer | None = None,
    ) -> None:
        self._jobs: dict[str, ResearchJobState] = {}
        self._cancelled_jobs: set[str] = set()
        self._tasks: dict[str, asyncio.Task] = {}
        self._session_factory = session_factory
        self._analyzer = analyzer or WebsiteAnalyzer(timeout=8.0, max_response_bytes=1_000_000, max_redirects=3)

    def create_job(self, input_params: ResearchInput) -> ResearchJobState:
        """Create and register a new research job state."""
        job = ResearchJobState(
            job_id=str(uuid4()),
            status="QUEUED",
            input_params=input_params,
            target_count=input_params.target_business_count,
        )
        self._jobs[job.job_id] = job
        return job

    def get_job(self, job_id: str) -> ResearchJobState | None:
        """Fetch job state by ID."""
        return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 20) -> list[ResearchJobState]:
        """List newest research jobs."""
        jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def cancel_job(self, job_id: str) -> bool:
        """Mark job as cancelled and abort running background task."""
        if job_id in self._jobs:
            self._cancelled_jobs.add(job_id)
            job = self._jobs[job_id]
            if job.status in ("QUEUED", "RUNNING"):
                job.status = "CANCELLED"
                job.updated_at = now_utc_iso()
            task = self._tasks.get(job_id)
            if task and not task.done():
                task.cancel()
            return True
        return False

    def start_job(self, job_id: str) -> None:
        """Launch asynchronous background research task."""
        task = asyncio.create_task(self._execute_research(job_id))
        self._tasks[job_id] = task

    async def _execute_research(self, job_id: str) -> None:
        """Background execution: discover businesses, analyze evidence, check duplicates."""
        job = self.get_job(job_id)
        if not job:
            return

        job.status = "RUNNING"
        job.updated_at = now_utc_iso()

        # Step 1: Check active provider
        try:
            provider = provider_registry.get_active_provider()
            job.provider_name = provider.name
        except MissingProviderConfigurationError as exc:
            job.status = "FAILED"
            job.error_message = str(exc)
            job.updated_at = now_utc_iso()
            logger.warning("Research job %s failed: %s", job_id, exc)
            return

        # Step 2: Discover candidate businesses
        try:
            candidates = await provider.discover(
                industry=job.input_params.industry,
                location=job.input_params.location,
                sub_category=job.input_params.sub_category,
                count=job.input_params.target_business_count,
            )
        except Exception as exc:
            job.status = "FAILED"
            job.error_message = f"Discovery provider error: {exc}"
            job.updated_at = now_utc_iso()
            logger.exception("Provider error in job %s: %s", job_id, exc)
            return

        job.found_count = len(candidates)
        if not candidates:
            job.status = "COMPLETED"
            job.progress_percent = 100.0
            job.updated_at = now_utc_iso()
            return

        # Step 3: Process each candidate lead
        session_maker = self._session_factory or SessionLocal
        try:
            async with session_maker() as db:
                for idx, cand in enumerate(candidates):
                    if job_id in self._cancelled_jobs:
                        job.status = "CANCELLED"
                        job.updated_at = now_utc_iso()
                        break

                    try:
                        result_item = await self._research_single_business(cand, job.input_params, db)
                        job.results.append(result_item)
                    except Exception as exc:
                        logger.warning("Error researching business %s: %s", cand.get("company_name"), exc)

                    job.processed_count = idx + 1
                    job.progress_percent = round((job.processed_count / max(job.found_count, 1)) * 100, 1)
                    job.updated_at = now_utc_iso()

                    # Polite rate-limiting between candidate inquiries
                    await asyncio.sleep(0.05)
        except Exception as conn_err:
            logger.warning("Database session error during research job %s: %s, falling back to offline candidate processing", job_id, conn_err)
            for idx, cand in enumerate(candidates):
                if job_id in self._cancelled_jobs:
                    job.status = "CANCELLED"
                    job.updated_at = now_utc_iso()
                    break
                try:
                    result_item = await self._research_single_business(cand, job.input_params, None)
                    job.results.append(result_item)
                except Exception as exc:
                    logger.warning("Error researching business %s: %s", cand.get("company_name"), exc)
                job.processed_count = idx + 1
                job.progress_percent = round((job.processed_count / max(job.found_count, 1)) * 100, 1)
                job.updated_at = now_utc_iso()

        if job.status == "RUNNING":
            job.status = "COMPLETED"
            job.progress_percent = 100.0
            job.updated_at = now_utc_iso()

    async def _research_single_business(
        self,
        candidate: dict[str, Any],
        params: ResearchInput,
        db: AsyncSession,
    ) -> ResearchResultItem:
        """Inspect a single candidate business, gather evidence, and match services."""
        company_name = (candidate.get("company_name") or "").strip()
        website = candidate.get("website")
        if website:
            website = website.strip()
            if not website.startswith(("http://", "https://")):
                website = "https://" + website

        city = candidate.get("city") or params.location
        country = candidate.get("country") or "India"
        industry = candidate.get("industry") or params.industry
        sub_category = candidate.get("sub_category") or params.sub_category
        phone = candidate.get("phone")

        verified_facts: list[str] = []
        inferred_opportunities: list[str] = []
        unavailable_fields: list[str] = ["decision_maker", "decision_maker_role"]

        verified_facts.append(f"Business discovered via {candidate.get('source_url') or 'Directory listing'}")

        # Duplicate detection using canonical matching rules: company_name + city (BR-12, BR-13)
        is_duplicate = False
        existing_lead_id: str | None = None
        if db is not None:
            try:
                existing_lead = await db.scalar(
                    select(Lead).where(
                        func.lower(Lead.company_name) == company_name.lower(),
                        func.lower(func.coalesce(Lead.city, "")) == (city or "").lower(),
                    )
                )
                if existing_lead:
                    is_duplicate = True
                    existing_lead_id = str(existing_lead.id)
            except Exception as db_exc:
                logger.debug("Database duplicate check skipped: %s", db_exc)

        # Website analysis & pixel detection
        ad_platforms: list[str] = []
        ad_evidence_notes: list[str] = []
        primary_problem: str | None = None
        recommended_service: str | None = None
        business_email: str | None = None
        linkedin_url: str | None = None

        if website:
            try:
                web_doc = await self._analyzer.analyze(website)
                if web_doc.get("available"):
                    status_code = web_doc.get("status_code", 200)
                    is_https = web_doc.get("https", False)
                    meta_desc = web_doc.get("meta_description")
                    h1 = web_doc.get("h1")
                    socials = web_doc.get("social_links", [])

                    verified_facts.append(f"Website verified reachable (HTTP {status_code}, HTTPS={is_https})")

                    # Extract LinkedIn
                    for link in socials:
                        if "linkedin.com" in link.lower() and not linkedin_url:
                            linkedin_url = link
                            verified_facts.append(f"Verified LinkedIn presence: {link}")

                    # HTML inspection for ad pixels
                    final_url, _, html_content = await self._analyzer._fetch(website)

                    # 1. Google Ads / Tag Manager
                    if any(t in html_content.lower() for t in ("googletagmanager.com", "googleads.g.doubleclick.net", "gtag(")):
                        ad_platforms.append("Google Ads")
                        ad_evidence_notes.append("Google Tag Manager / Google Ads tag detected in script tags")
                        verified_facts.append("Active Google Ads / GTM tracking tag detected on website")

                    # 2. Meta Ads
                    if any(t in html_content.lower() for t in ("connect.facebook.net", "fbq(", "facebook.com/tr")):
                        ad_platforms.append("Meta Ads")
                        ad_evidence_notes.append("Meta Pixel (fbq) tracking snippet detected in source")
                        verified_facts.append("Active Meta Pixel (fbq) detected on website")

                    # 3. LinkedIn Ads
                    if any(t in html_content.lower() for t in ("snap.licdn.com", "linkedin.com/insight")):
                        ad_platforms.append("LinkedIn Ads")
                        ad_evidence_notes.append("LinkedIn Insight Tag detected on source")
                        verified_facts.append("Active LinkedIn Insight Tag detected on website")

                    # 4. Extract public mailto emails
                    mail_matches = EMAIL_REGEX.findall(html_content)
                    valid_emails = [m for m in mail_matches if not m.endswith((".png", ".jpg", ".js", ".css"))]
                    if valid_emails:
                        business_email = valid_emails[0].lower()
                        verified_facts.append(f"Public contact email found on website: {business_email}")
                    else:
                        unavailable_fields.append("business_email")

                    # Problem and MindArtDigital service recommendation matching
                    if not is_https:
                        primary_problem = "Website lacks SSL/TLS encryption (HTTP), triggering security warnings in modern browsers."
                        recommended_service = "Website Development & Maintenance"
                        inferred_opportunities.append("Immediate migration to HTTPS and security certificate setup required.")
                    elif not meta_desc or not h1:
                        primary_problem = "Landing page lacks meta description or H1 header structure, limiting search engine visibility."
                        recommended_service = "SEO & Content Optimization"
                        inferred_opportunities.append("On-page SEO optimization and content structuring opportunity identified.")
                    elif ad_platforms and "Meta Ads" in ad_platforms:
                        primary_problem = "Active paid advertising traffic without dedicated high-conversion funnel or chat capture."
                        recommended_service = "Performance Marketing & Meta Ads"
                        inferred_opportunities.append("Conversion rate optimization (CRO) and full-funnel paid advertising campaign restructuring.")
                    elif ad_platforms and "Google Ads" in ad_platforms:
                        primary_problem = "Running search ad campaigns without automated conversational lead capture."
                        recommended_service = "AI Automation & Chatbots"
                        inferred_opportunities.append("Deploy AI conversational chatbot for instant lead qualification and appointment scheduling.")
                    elif socials and not ad_platforms:
                        primary_problem = "Active brand social media accounts with absence of retargeting pixel infrastructure."
                        recommended_service = "Social Media Marketing & Meta Ads"
                        inferred_opportunities.append("Setup audience retargeting pixels and high-intent social advertising.")
                    else:
                        primary_problem = f"Opportunity to scale local market visibility and organic lead generation in {city}."
                        recommended_service = "Local SEO & Business Growth"
                        inferred_opportunities.append(f"Targeted Local SEO optimization for {industry} in {city}.")
                else:
                    verified_facts.append(f"Website unreachable or timed out: {website}")
                    primary_problem = "Business domain is unresponsive or experiencing server/DNS downtime."
                    recommended_service = "Website Development & Maintenance"
                    inferred_opportunities.append("Domain recreation, hosting stabilization, and modern website deployment.")
                    unavailable_fields.append("business_email")
            except Exception as err:
                logger.debug("Website fetch failed for %s: %s", website, err)
                verified_facts.append(f"Website could not be reached safely ({err})")
                primary_problem = "Business website has connectivity or accessibility issues."
                recommended_service = "Website Development & Maintenance"
                unavailable_fields.append("business_email")
        else:
            unavailable_fields.extend(["website", "business_email"])
            primary_problem = f"Business does not have a verified website presence for {industry} in {city}."
            recommended_service = "Website Development & Local SEO"
            inferred_opportunities.append("Create first-party digital web presence and local directory listings.")

        if not phone:
            unavailable_fields.append("phone")

        # Compile advertising platforms & evidence
        ad_platform_str = "; ".join(ad_platforms) if ad_platforms else "Unknown"
        ad_evidence_str = "; ".join(ad_evidence_notes) if ad_evidence_notes else None

        source_url = website or candidate.get("source_url") or "https://maps.google.com"
        evidence = SourceEvidence(
            source_url=source_url,
            evidence_type="web_and_place_observation",
            verified_facts=verified_facts,
            inferred_opportunities=inferred_opportunities,
            unavailable_fields=list(dict.fromkeys(unavailable_fields)),
            raw_details={"candidate_raw": candidate.get("address")},
        )

        # Classification status: verified, inferred, duplicate, or partial
        if is_duplicate:
            research_status = "duplicate"
        elif verified_facts and ad_platforms:
            research_status = "verified"
        elif verified_facts:
            research_status = "inferred"
        else:
            research_status = "partial"

        return ResearchResultItem(
            company_name=company_name,
            website=website,
            industry=industry,
            sub_category=sub_category,
            country=country,
            city=city,
            decision_maker=None,  # Do not invent public professional contacts without verified individual lookup (BR-09)
            decision_maker_role=None,
            business_email=business_email,
            phone=phone,
            linkedin=linkedin_url,
            advertising_platforms=ad_platform_str,
            advertising_evidence=ad_evidence_str,
            primary_problem=primary_problem,
            recommended_mindart_service=recommended_service,
            research_status=research_status,
            source_evidence=evidence,
            is_duplicate=is_duplicate,
            existing_lead_id=existing_lead_id,
            selected=not is_duplicate,  # Default selection on non-duplicates
        )

    async def save_leads(self, db: AsyncSession, request: SaveLeadsRequest) -> SaveLeadsResponse:
        """Save approved researched leads into the database with duplicate preservation (BR-11, BR-14)."""
        saved_count = 0
        updated_count = 0
        skipped_count = 0
        lead_ids: list[str] = []

        for item in request.leads:
            if item.rejected:
                skipped_count += 1
                continue

            company_name = item.company_name.strip()
            city = (item.city or "").strip()

            # Check duplicate matching by company_name + city
            existing = await db.scalar(
                select(Lead).where(
                    func.lower(Lead.company_name) == company_name.lower(),
                    func.lower(func.coalesce(Lead.city, "")) == city.lower(),
                ).with_for_update()
            )

            # Convert item to CSV row format for canonical mapping
            row_dict = {
                "company_name": company_name,
                "website": item.website,
                "industry": item.industry,
                "sub_category": item.sub_category,
                "country": item.country,
                "city": item.city,
                "decision_maker": item.decision_maker,
                "decision_maker_role": item.decision_maker_role,
                "business_email": item.business_email,
                "phone": item.phone,
                "linkedin": item.linkedin,
                "advertising_platforms": item.advertising_platforms,
                "advertising_evidence": item.advertising_evidence,
                "primary_problem": item.primary_problem,
                "recommended_mindart_service": item.recommended_mindart_service,
            }
            lead_data = map_row_to_lead_data(row_dict)

            if existing:
                # Update ONLY unpopulated fields without overwriting existing data (BR-14)
                updated_any = False
                for field in (
                    "website", "industry", "country", "phone", "email",
                    "decision_maker", "job_title", "linkedin_url"
                ):
                    current_val = getattr(existing, field, None)
                    new_val = lead_data.get(field)
                    if not current_val and new_val:
                        setattr(existing, field, new_val)
                        updated_any = True

                if not existing.pain_points and lead_data.get("pain_points"):
                    existing.pain_points = lead_data.get("pain_points")
                    updated_any = True

                if not existing.recommended_services and lead_data.get("recommended_services"):
                    existing.recommended_services = lead_data.get("recommended_services")
                    updated_any = True

                if not existing.website_analysis and lead_data.get("website_analysis"):
                    existing.website_analysis = lead_data.get("website_analysis")
                    updated_any = True

                if updated_any:
                    existing.updated_at = datetime.now(timezone.utc)
                    updated_count += 1
                else:
                    skipped_count += 1
                lead_ids.append(str(existing.id))
            else:
                # New lead insertion
                new_lead = Lead(**lead_data)
                db.add(new_lead)
                await db.flush()
                saved_count += 1
                lead_ids.append(str(new_lead.id))

        await db.commit()
        return SaveLeadsResponse(
            success=True,
            total_submitted=len(request.leads),
            saved_count=saved_count,
            updated_count=updated_count,
            skipped_count=skipped_count,
            lead_ids=lead_ids,
        )

    def export_job_csv(self, job_id: str) -> bytes:
        """Export research results formatted in the exact 15-field MindArtDigital template."""
        job = self.get_job(job_id)
        if not job:
            raise ValueError("Research job not found")

        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(MINDART_TEMPLATE_HEADERS)

        for item in job.results:
            if item.rejected:
                continue
            writer.writerow([
                item.company_name,
                item.website or "",
                item.industry or "",
                item.sub_category or "",
                item.country or "",
                item.city or "",
                item.decision_maker or "",
                item.decision_maker_role or "",
                item.business_email or "",
                item.phone or "",
                item.linkedin or "",
                item.advertising_platforms or "",
                item.advertising_evidence or "",
                item.primary_problem or "",
                item.recommended_mindart_service or "",
            ])

        return output.getvalue().encode("utf-8")


# Global engine instance
research_engine = BusinessResearchEngine()

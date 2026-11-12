"""
Unified Lead and Company Intelligence Service.

Grounds company intelligence, lead intelligence, website intelligence,
digital marketing intelligence, and buying signals strictly in persisted
external source evidence and canonical opportunities.

Target Architecture & Data Integrity Invariants:
1. Never invent company facts or fabricate evidence sources.
2. Distinguish VERIFIED (external provider/web), INFERRED (derived opportunity/heuristics),
   and UNKNOWN/UNAVAILABLE.
3. Every researched fact has source provenance (SourceType and ConfidenceLevel).
4. Synchronize Company Profile and Lead Intelligence with canonical Evidence and Opportunities.
5. Idempotent non-destructive persistence: upserts unified profiles without broken foreign keys.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, UTC
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.company_enrichment import (
    calculate_company_completeness,
    classify_industry_from_evidence,
    extract_email_domain,
    normalize_company_name,
    normalize_location,
    normalize_website,
    _normalize_company_size,
    _normalize_industry,
)
from app.domain.lead_intelligence import _normalize_string
from app.domain.website_intelligence import (
    calculate_website_completeness,
    normalize_domain,
)
from app.models.entities import (
    BuyingSignal,
    CompanyEnrichmentProfile,
    ConfidenceLevel,
    DigitalMarketingIntelligenceProfile,
    EnrichmentRun,
    Evidence,
    Lead,
    LeadIntelligenceOpportunity,
    LeadIntelligencePainPoint,
    LeadIntelligenceProfile,
    LeadScore,
    Opportunity,
    SeoAuditRun,
    ServiceRecommendation,
    SignalStrength,
    SourceType,
    WebsiteIntelligenceProfile,
    now_utc,
)
from app.schemas.company_enrichment import CompanyEnrichmentProfileSchema
from app.schemas.lead_intelligence import (
    IntelligenceBusinessContext,
    IntelligenceCompanyProfile,
    IntelligenceContactProfile,
    IntelligenceDigitalPresence,
    IntelligenceOpportunity,
    IntelligencePainPoint,
    IntelligenceQuality,
    LeadIntelligenceProfileSchema,
)
from app.services.enrichment_pipeline import run_enrichment_pipeline
from app.services.intelligence import derive_and_persist_intelligence
from app.services.website import WebsiteAnalyzer

logger = logging.getLogger("lead_agent.unified_intelligence")


async def get_all_lead_evidence(session: AsyncSession, lead_id: uuid.UUID) -> list[Evidence]:
    """Retrieve all evidence records for a lead."""
    return list((await session.scalars(
        select(Evidence).where(Evidence.lead_id == lead_id).order_by(Evidence.observed_at.desc(), Evidence.id)
    )).all())


def extract_evidence_by_type(evidence_list: list[Evidence]) -> dict[str, list[Evidence]]:
    """Group evidence records by their evidence_type."""
    by_type: dict[str, list[Evidence]] = {}
    for ev in evidence_list:
        by_type.setdefault(ev.evidence_type, []).append(ev)
    return by_type


def build_unified_company_profile(
    lead: Lead,
    evidence_list: list[Evidence],
    existing_profile_id: uuid.UUID | None = None,
) -> CompanyEnrichmentProfile:
    """
    Build an evidence-grounded CompanyEnrichmentProfile.
    Reconciles public website crawl, Google Places metadata, and user lead records.
    Never fabricates unknown fields.
    """
    now = datetime.now(UTC)
    by_type = extract_evidence_by_type(evidence_list)

    # 1. Company names
    legal_name = normalize_company_name(lead.company_name)
    display_name = legal_name

    # 2. Domains and websites
    raw_website = lead.website
    norm_website, domain = normalize_website(raw_website)
    email_domain = extract_email_domain(lead.email)
    if not domain and email_domain:
        domain = email_domain

    linkedin_url = lead.linkedin_url.strip() if lead.linkedin_url else None
    if not linkedin_url and "website_social_link" in by_type:
        for ev in by_type["website_social_link"]:
            if isinstance(ev.observed_value, str) and "linkedin.com" in ev.observed_value.lower():
                linkedin_url = ev.observed_value
                break

    # 3. Location: prefer Google Places verified address, fallback to lead record
    city = lead.city.strip() if lead.city else None
    state = lead.state.strip() if lead.state else None
    country = lead.country.strip() if lead.country else None
    location = normalize_location(city, state, country)

    if "google_places_formatted_address" in by_type:
        addr_ev = by_type["google_places_formatted_address"][0]
        if isinstance(addr_ev.observed_value, str) and addr_ev.observed_value.strip():
            location = addr_ev.observed_value.strip()

    # 4. Phone: prefer verified phone, fallback to lead
    phone = lead.phone.strip() if lead.phone else None
    if "google_places_phone" in by_type:
        phone_ev = by_type["google_places_phone"][0]
        if isinstance(phone_ev.observed_value, str) and phone_ev.observed_value.strip():
            phone = phone_ev.observed_value.strip()

    # 5. Description: prefer verified website meta_description / title, fallback to ai_summary or website_analysis
    description = None
    if "website_meta_description" in by_type:
        meta_desc = by_type["website_meta_description"][0].observed_value
        if isinstance(meta_desc, str) and meta_desc.strip():
            description = meta_desc.strip()
    if not description and "website_title" in by_type:
        title_val = by_type["website_title"][0].observed_value
        if isinstance(title_val, str) and title_val.strip():
            description = f"Website title: {title_val.strip()}"
    if not description and lead.ai_summary:
        description = lead.ai_summary.strip()
    if not description and isinstance(lead.website_analysis, dict):
        if lead.website_analysis.get("meta_description"):
            description = str(lead.website_analysis["meta_description"]).strip()
        elif lead.website_analysis.get("description"):
            description = str(lead.website_analysis["description"]).strip()
        elif lead.website_analysis.get("sub_category"):
            sub_cat = str(lead.website_analysis["sub_category"]).strip()
            loc_str = f" based in {location}" if location else ""
            description = f"{sub_cat} brand{loc_str}."

    # 6. Industry: classify from evidence hierarchy
    gp_types = None
    if "google_places_types" in by_type:
        val_gp = by_type["google_places_types"][0].observed_value
        gp_types = val_gp if isinstance(val_gp, list) else [val_gp]

    meta_desc_text = None
    if "website_meta_description" in by_type:
        meta_desc_text = str(by_type["website_meta_description"][0].observed_value)
    title_text = None
    if "website_title" in by_type:
        title_text = str(by_type["website_title"][0].observed_value)

    industry_raw, normalized_industry, ind_source, ind_confidence = classify_industry_from_evidence(
        lead_industry=lead.industry,
        website_analysis=lead.website_analysis if isinstance(lead.website_analysis, dict) else None,
        google_places_types=gp_types,
        website_title=title_text,
        meta_description=meta_desc_text,
        company_name=lead.company_name,
    )

    # 7. Company size & revenue: leave unknown unless evidenced
    company_size_raw = None
    normalized_company_size = None
    if isinstance(lead.website_analysis, dict) and lead.website_analysis.get("employee_count"):
        emp_raw = str(lead.website_analysis["employee_count"]).strip()
        if emp_raw and emp_raw.upper() != "UNKNOWN":
            company_size_raw = emp_raw
            normalized_company_size = _normalize_company_size(emp_raw)

    if not phone and isinstance(lead.website_analysis, dict):
        wb_phone = lead.website_analysis.get("phone") or lead.website_analysis.get("contact_phone")
        if wb_phone and str(wb_phone).strip().upper() != "UNKNOWN":
            phone = str(wb_phone).strip()

    if not linkedin_url and isinstance(lead.website_analysis, dict):
        wb_li = lead.website_analysis.get("linkedin") or lead.website_analysis.get("contact_linkedin")
        if wb_li and str(wb_li).strip().upper() != "UNKNOWN":
            linkedin_url = str(wb_li).strip()

    # 8. Provenance and Confidence
    has_ext_evidence = bool(
        "google_places_rating" in by_type or
        "google_places_formatted_address" in by_type or
        "pagespeed_performance_score" in by_type
    )
    has_web_evidence = bool(
        "website_available" in by_type or
        "website_http_status" in by_type
    )

    if has_ext_evidence:
        source = SourceType.SYSTEM_DERIVED
        confidence = ConfidenceLevel.HIGH
    elif has_web_evidence:
        source = SourceType.SYSTEM_DERIVED
        confidence = ConfidenceLevel.HIGH if (norm_website and domain) else ConfidenceLevel.MEDIUM
    else:
        source = SourceType.LEAD_RECORD
        confidence = ConfidenceLevel.LOW if not norm_website else ConfidenceLevel.MEDIUM

    fields_for_completeness = {
        "legal_name": legal_name,
        "domain": domain,
        "website": norm_website,
        "linkedin_url": linkedin_url,
        "location": location,
        "description": description,
        "industry": normalized_industry,
        "phone": phone,
    }
    completeness = calculate_company_completeness(fields_for_completeness)

    return CompanyEnrichmentProfile(
        id=existing_profile_id or uuid.uuid4(),
        lead_id=lead.id,
        legal_name=legal_name,
        display_name=display_name,
        domain=domain,
        website=norm_website,
        email_domain=email_domain,
        linkedin_url=linkedin_url,
        phone=phone,
        location=location,
        city=city,
        state=state,
        country=country,
        description=description,
        industry=industry_raw,
        normalized_industry=normalized_industry,
        company_size=company_size_raw,
        normalized_company_size=normalized_company_size,
        source=source,
        confidence=confidence,
        completeness_percent=completeness,
        created_at=now,
        updated_at=now,
    )


def build_unified_website_profile(
    lead: Lead,
    evidence_list: list[Evidence],
    existing_id: uuid.UUID | None = None,
) -> WebsiteIntelligenceProfile:
    """Build WebsiteIntelligenceProfile from collected website & tracking evidence."""
    now = datetime.now(UTC)
    by_type = extract_evidence_by_type(evidence_list)

    raw_website = lead.website
    norm_website, extracted_domain = normalize_website(raw_website)
    website_present = bool(norm_website)
    domain_present = bool(extracted_domain)

    # Technology & pixel signals
    technology_signals: dict[str, Any] = {}
    content_signals: dict[str, Any] = {}

    # Extract detected tracking pixels from website_analysis or evidence
    if isinstance(lead.website_analysis, dict):
        if "ad_platforms" in lead.website_analysis:
            technology_signals["ad_platforms"] = lead.website_analysis["ad_platforms"]
        if "advertising_platforms" in lead.website_analysis:
            technology_signals["advertising_platforms"] = lead.website_analysis["advertising_platforms"]
        if "technologies" in lead.website_analysis:
            technology_signals["technologies"] = lead.website_analysis["technologies"]
        if "meta_description" in lead.website_analysis:
            content_signals["meta_description"] = lead.website_analysis["meta_description"]

    if "website_meta_description" in by_type:
        desc_val = by_type["website_meta_description"][0].observed_value
        if desc_val:
            content_signals["meta_description"] = desc_val

    if "website_h1" in by_type:
        h1_val = by_type["website_h1"][0].observed_value
        if h1_val:
            content_signals["h1"] = h1_val

    if "website_https" in by_type:
        https_val = by_type["website_https"][0].observed_value
        if isinstance(https_val, bool):
            technology_signals["https"] = https_val

    has_tech = bool(technology_signals)
    has_content = bool(content_signals)
    completeness = calculate_website_completeness(website_present, domain_present, has_tech, has_content)

    confidence = ConfidenceLevel.HIGH if (website_present and ("website_http_status" in by_type or "website_available" in by_type)) else ConfidenceLevel.MEDIUM if website_present else ConfidenceLevel.UNKNOWN

    return WebsiteIntelligenceProfile(
        id=existing_id or uuid.uuid4(),
        company_id=lead.id,
        website=raw_website,
        normalized_website=norm_website,
        domain=extracted_domain,
        normalized_domain=extracted_domain,
        website_present=website_present,
        domain_present=domain_present,
        technology_signals=technology_signals if technology_signals else None,
        content_signals=content_signals if content_signals else None,
        source_type=SourceType.SYSTEM_DERIVED if has_tech or has_content else SourceType.LEAD_RECORD,
        confidence=confidence,
        completeness=completeness,
        created_at=now,
        updated_at=now,
    )


def build_unified_digital_marketing_profile(
    lead: Lead,
    evidence_list: list[Evidence],
    opportunities: list[Opportunity],
    has_seo_run: bool = False,
    existing_id: uuid.UUID | None = None,
    existing_coverage: dict[str, dict[str, Any]] | None = None,
) -> DigitalMarketingIntelligenceProfile:
    """Build DigitalMarketingIntelligenceProfile reflecting observed digital presence, research status, and gaps across all 7 canonical categories."""
    now = datetime.now(UTC)
    by_type = extract_evidence_by_type(evidence_list)

    from app.domain.digital_marketing_intelligence import (
        evaluate_seven_categories,
        calculate_canonical_completeness,
        build_coverage_summary,
        derive_marketing_maturity,
        derive_marketing_gaps,
        SEO, PAID_ADVERTISING, SOCIAL_MEDIA, CONTENT_MARKETING,
        WEBSITE_PERFORMANCE, CONVERSION_OPTIMIZATION, ONLINE_REPUTATION
    )

    coverage = evaluate_seven_categories(
        lead=lead,
        evidence_by_type=by_type,
        opportunities=opportunities,
        has_seo_run=has_seo_run,
        existing_coverage=existing_coverage,
    )

    completeness_score = calculate_canonical_completeness(coverage)
    completeness = Decimal(str(completeness_score))
    coverage_summary = build_coverage_summary(coverage)

    category_statuses = {}
    for cat, data in coverage.items():
        st = data.get("status", "NOT_RESEARCHED")
        if st == "RESEARCHED":
            category_statuses[cat] = "SIGNAL_DETECTED" if data.get("signal_detected", True) else "NO_SIGNAL_FOUND"
        else:
            category_statuses[cat] = st

    active_channels = sum(
        1 for c in coverage.values()
        if c.get("signal_detected")
    )
    if lead.website:
        active_channels = max(1, active_channels)
    maturity = derive_marketing_maturity(active_channels)

    marketing_gaps = [
        c["problem_summary"]
        for c in coverage.values()
        if c.get("problem_identified") and c.get("problem_summary")
    ]
    if not marketing_gaps:
        marketing_gaps = derive_marketing_gaps(
            has_seo=bool(coverage.get(SEO, {}).get("signal_detected")),
            has_paid=bool(coverage.get(PAID_ADVERTISING, {}).get("signal_detected")),
            has_social=bool(coverage.get(SOCIAL_MEDIA, {}).get("signal_detected")),
            has_content=bool(coverage.get(CONTENT_MARKETING, {}).get("signal_detected")),
        )

    marketing_opps = [
        f"{c['title']}: {c['recommended_service']}"
        for c in coverage.values()
        if c.get("problem_identified")
    ]

    # Map signals for top 4 categories and attach 7-category coverage
    def _make_legacy_signal(cat_key: str) -> dict[str, Any]:
        item = dict(coverage.get(cat_key, {}))
        st = item.get("status", "NOT_RESEARCHED")
        if st == "RESEARCHED":
            item["status"] = "SIGNAL_DETECTED" if item.get("signal_detected", True) else "NO_SIGNAL_FOUND"
        return item

    seo_signals = _make_legacy_signal(SEO)
    paid_ads_signals = _make_legacy_signal(PAID_ADVERTISING)
    social_signals = _make_legacy_signal(SOCIAL_MEDIA)
    content_signals = _make_legacy_signal(CONTENT_MARKETING)

    paid_ads_signals["_seven_category_coverage"] = coverage
    paid_ads_signals["_coverage_summary"] = coverage_summary
    paid_ads_signals["_category_statuses"] = category_statuses

    seo_signals["_seven_category_coverage"] = coverage
    seo_signals["_coverage_summary"] = coverage_summary
    seo_signals["_category_statuses"] = category_statuses

    has_detected = any(st in ("SIGNAL_DETECTED", "RESEARCHED") for st in category_statuses.values())

    return DigitalMarketingIntelligenceProfile(
        id=existing_id or uuid.uuid4(),
        company_id=lead.id,
        seo_signals=seo_signals,
        paid_advertising_signals=paid_ads_signals,
        social_media_signals=social_signals,
        content_marketing_signals=content_signals,
        marketing_maturity=maturity,
        marketing_gaps=marketing_gaps if marketing_gaps else None,
        marketing_opportunities=marketing_opps if marketing_opps else None,
        source_type=SourceType.SYSTEM_DERIVED if has_detected else SourceType.LEAD_RECORD,
        confidence=ConfidenceLevel.HIGH if has_detected else ConfidenceLevel.MEDIUM,
        completeness=completeness,
        created_at=now,
        updated_at=now,
    )


def build_unified_buying_signals(
    lead: Lead,
    evidence_list: list[Evidence],
    opportunities: list[Opportunity],
) -> list[BuyingSignal]:
    """Derive grounded buying signals from verified gaps and technical signals."""
    now = datetime.now(UTC)
    by_type = extract_evidence_by_type(evidence_list)
    signals: list[BuyingSignal] = []

    # 1. Website presence confirmed
    if lead.website:
        signals.append(BuyingSignal(
            id=uuid.uuid4(),
            company_id=lead.id,
            signal_type="WEBSITE_PRESENCE_CONFIRMED",
            signal_category="website_activity",
            confidence=ConfidenceLevel.HIGH if "website_available" in by_type else ConfidenceLevel.MEDIUM,
            strength=SignalStrength.MEDIUM,
            source_type=SourceType.SYSTEM_DERIVED,
            detected_at=now,
            evidence={"website": lead.website},
            status="ACTIVE",
            created_at=now,
        ))

    # 2. Critical/High Opportunity detected (buying intent / agency service need)
    critical_opps = [opp for opp in opportunities if opp.severity in {"critical", "high"}]
    if critical_opps:
        signals.append(BuyingSignal(
            id=uuid.uuid4(),
            company_id=lead.id,
            signal_type="HIGH_IMPACT_DIGITAL_GAP",
            signal_category="marketing_opportunity",
            confidence=ConfidenceLevel.HIGH,
            strength=SignalStrength.HIGH,
            source_type=SourceType.SYSTEM_DERIVED,
            detected_at=now,
            evidence={
                "opportunities": [opp.title for opp in critical_opps[:3]],
                "count": len(critical_opps),
            },
            status="ACTIVE",
            created_at=now,
        ))

    # 3. Active advertising detected without complete conversion instrumentation
    if isinstance(lead.website_analysis, dict) and lead.website_analysis.get("advertising_platforms"):
        signals.append(BuyingSignal(
            id=uuid.uuid4(),
            company_id=lead.id,
            signal_type="PAID_ACQUISITION_INVESTMENT",
            signal_category="advertising_activity",
            confidence=ConfidenceLevel.HIGH,
            strength=SignalStrength.HIGH,
            source_type=SourceType.SYSTEM_DERIVED,
            detected_at=now,
            evidence={
                "platforms": lead.website_analysis.get("advertising_platforms"),
                "notes": lead.website_analysis.get("advertising_evidence"),
            },
            status="ACTIVE",
            created_at=now,
        ))

    return signals


def build_unified_lead_intelligence_schema(
    lead: Lead,
    comp_profile: CompanyEnrichmentProfile,
    pains: list[LeadIntelligencePainPoint],
    opps: list[LeadIntelligenceOpportunity],
) -> LeadIntelligenceProfileSchema:
    """Project unified profile models into LeadIntelligenceProfileSchema."""
    schema_industry = None
    if comp_profile.industry and comp_profile.industry != "UNKNOWN":
        schema_industry = comp_profile.industry
    elif comp_profile.normalized_industry and comp_profile.normalized_industry != "UNKNOWN":
        schema_industry = comp_profile.normalized_industry
    else:
        schema_industry = comp_profile.normalized_industry or comp_profile.industry

    return LeadIntelligenceProfileSchema(
        lead_id=lead.id,
        company=IntelligenceCompanyProfile(
            name=comp_profile.legal_name or lead.company_name,
            legal_name=comp_profile.legal_name,
            website=comp_profile.website,
            industry=schema_industry,
            industry_source=comp_profile.source,
            company_size=comp_profile.normalized_company_size or comp_profile.company_size,
            company_size_source=comp_profile.source if comp_profile.company_size else None,
            location=comp_profile.location,
            country=comp_profile.country,
            description=comp_profile.description,
            products_services=lead.recommended_services if isinstance(lead.recommended_services, list) else [],
        ),
        contact=IntelligenceContactProfile(
            name=lead.decision_maker,
            job_title=getattr(lead, "job_title", None),
            email=lead.email,
            phone=lead.phone or comp_profile.phone,
            location=comp_profile.location,
        ),
        digital_presence=IntelligenceDigitalPresence(
            website=comp_profile.website,
            linkedin_url=comp_profile.linkedin_url or lead.linkedin_url,
            facebook_url=None,
            instagram_url=None,
            youtube_url=None,
            other_urls=[],
        ),
        business_context=IntelligenceBusinessContext(
            pain_points=[
                IntelligencePainPoint(
                    id=p.id,
                    title=p.title,
                    description=p.description,
                    source_type=p.source_type if isinstance(p.source_type, SourceType) else SourceType(str(p.source_type)),
                    source_reference=p.source_reference,
                    confidence=p.confidence if isinstance(p.confidence, ConfidenceLevel) else ConfidenceLevel(str(p.confidence)),
                    created_at=p.created_at,
                    updated_at=p.updated_at,
                )
                for p in pains
            ],
            opportunities=[
                IntelligenceOpportunity(
                    id=o.id,
                    title=o.title,
                    description=o.description,
                    opportunity_type=o.opportunity_type,
                    score=o.score,
                    priority=o.priority,
                    recommended_action=o.recommended_action,
                    evidence=o.evidence or {},
                    source_type=o.source_type if isinstance(o.source_type, SourceType) else SourceType(str(o.source_type)),
                    source_reference=o.source_reference,
                    confidence=o.confidence if isinstance(o.confidence, ConfidenceLevel) else ConfidenceLevel(str(o.confidence)),
                    related_task_id=getattr(o, "related_task_id", None),
                    related_task_status=getattr(o, "related_task_status", None),
                    created_at=o.created_at,
                    updated_at=o.updated_at,
                )
                for o in opps
            ],
            known_services=lead.recommended_services if isinstance(lead.recommended_services, list) else [],
            known_products=[],
        ),
        intelligence_quality=IntelligenceQuality(
            completeness_score=float(comp_profile.completeness_percent),
            last_researched_at=comp_profile.updated_at,
            field_count=12,
            populated_field_count=int(float(comp_profile.completeness_percent) * 12 / 100),
        ),
    )


async def sync_unified_intelligence_for_lead(
    session: AsyncSession,
    lead_id: uuid.UUID,
    session_factory: async_sessionmaker | None = None,
) -> LeadIntelligenceProfileSchema:
    """
    Core engine: reconciles all persisted evidence, opportunities, and lead data,
    then updates all domain profile tables within a single atomic database transaction.
    """
    lead = await session.get(Lead, lead_id)
    if not lead:
        raise ValueError(f"Lead {lead_id} not found")

    now = datetime.now(UTC)

    # 1. Load all existing Evidence rows for this lead
    evidence_list = list((await session.scalars(
        select(Evidence).where(Evidence.lead_id == lead_id).order_by(Evidence.observed_at.desc(), Evidence.id)
    )).all())

    # 2. Load all canonical Opportunity rows for this lead
    opportunities = list((await session.scalars(
        select(Opportunity).where(Opportunity.lead_id == lead_id).order_by(Opportunity.category, Opportunity.id)
    )).all())

    # 3. Check for SEO runs
    has_seo_run = bool(await session.scalar(
        select(func.count(SeoAuditRun.id)).where(SeoAuditRun.lead_id == lead_id)
    ))

    # 4. Check existing profile IDs to perform clean in-place updates (no cascading breaks)
    existing_comp = await session.scalar(
        select(CompanyEnrichmentProfile).where(CompanyEnrichmentProfile.lead_id == lead_id)
    )
    existing_web = await session.scalar(
        select(WebsiteIntelligenceProfile).where(WebsiteIntelligenceProfile.company_id == lead_id)
    )
    existing_digital = await session.scalar(
        select(DigitalMarketingIntelligenceProfile).where(DigitalMarketingIntelligenceProfile.company_id == lead_id)
    )
    existing_lead_intel = await session.scalar(
        select(LeadIntelligenceProfile).where(LeadIntelligenceProfile.lead_id == lead_id)
    )

    # 5. Build unified domain models
    comp_profile = build_unified_company_profile(
        lead=lead,
        evidence_list=evidence_list,
        existing_profile_id=existing_comp.id if existing_comp else None,
    )

    web_profile = build_unified_website_profile(
        lead=lead,
        evidence_list=evidence_list,
        existing_id=existing_web.id if existing_web else None,
    )

    existing_cov = existing_digital.seven_category_coverage if existing_digital else None
    digital_profile = build_unified_digital_marketing_profile(
        lead=lead,
        evidence_list=evidence_list,
        opportunities=opportunities,
        has_seo_run=has_seo_run,
        existing_id=existing_digital.id if existing_digital else None,
        existing_coverage=existing_cov,
    )

    # Persist Company Profile
    if existing_comp:
        for attr in ("legal_name", "display_name", "domain", "website", "email_domain", "linkedin_url",
                     "phone", "location", "city", "state", "country", "description", "company_size",
                     "normalized_company_size", "source", "confidence", "completeness_percent", "updated_at"):
            new_val = getattr(comp_profile, attr)
            if new_val is not None:
                setattr(existing_comp, attr, new_val)
        # For industry and normalized_industry, never overwrite a known value with UNKNOWN or None
        if comp_profile.industry and comp_profile.industry != "UNKNOWN":
            existing_comp.industry = comp_profile.industry
        if comp_profile.normalized_industry and comp_profile.normalized_industry != "UNKNOWN":
            existing_comp.normalized_industry = comp_profile.normalized_industry
        active_comp = existing_comp
    else:
        session.add(comp_profile)
        active_comp = comp_profile

    # Persist Website Intelligence
    if existing_web:
        for attr in ("website", "normalized_website", "domain", "normalized_domain", "website_present",
                     "domain_present", "technology_signals", "content_signals", "source_type",
                     "confidence", "completeness", "updated_at"):
            setattr(existing_web, attr, getattr(web_profile, attr))
    else:
        session.add(web_profile)

    # Persist Digital Marketing Intelligence
    if existing_digital:
        for attr in ("seo_signals", "paid_advertising_signals", "social_media_signals", "content_marketing_signals",
                     "marketing_maturity", "marketing_gaps", "marketing_opportunities", "source_type",
                     "confidence", "completeness", "updated_at"):
            setattr(existing_digital, attr, getattr(digital_profile, attr))
    else:
        session.add(digital_profile)

    # Persist Buying Signals (delete older signals and re-derive from fresh evidence)
    await session.execute(delete(BuyingSignal).where(BuyingSignal.company_id == lead_id))
    new_signals = build_unified_buying_signals(lead, evidence_list, opportunities)
    for sig in new_signals:
        session.add(sig)

    # 6. Build & Persist LeadIntelligenceProfile
    chosen_industry = None
    if active_comp.industry and active_comp.industry != "UNKNOWN":
        chosen_industry = active_comp.industry
    elif active_comp.normalized_industry and active_comp.normalized_industry != "UNKNOWN":
        chosen_industry = active_comp.normalized_industry
    elif existing_lead_intel and existing_lead_intel.industry and existing_lead_intel.industry != "UNKNOWN":
        chosen_industry = existing_lead_intel.industry
    else:
        chosen_industry = "UNKNOWN"

    if existing_lead_intel:
        existing_lead_intel.industry = chosen_industry
        existing_lead_intel.industry_source = active_comp.source
        existing_lead_intel.industry_confidence = active_comp.confidence
        existing_lead_intel.company_size = active_comp.normalized_company_size or existing_lead_intel.company_size
        existing_lead_intel.company_size_source = active_comp.source if active_comp.company_size else existing_lead_intel.company_size_source
        existing_lead_intel.company_size_confidence = active_comp.confidence if active_comp.company_size else existing_lead_intel.company_size_confidence
        existing_lead_intel.website = active_comp.website or existing_lead_intel.website
        existing_lead_intel.location = active_comp.location or existing_lead_intel.location
        existing_lead_intel.country = active_comp.country or existing_lead_intel.country
        existing_lead_intel.description = active_comp.description or existing_lead_intel.description
        existing_lead_intel.last_researched_at = now
        existing_lead_intel.completeness_score = active_comp.completeness_percent
        existing_lead_intel.updated_at = now
        active_lead_intel = existing_lead_intel
    else:
        active_lead_intel = LeadIntelligenceProfile(
            id=uuid.uuid4(),
            lead_id=lead.id,
            industry=chosen_industry,
            industry_source=active_comp.source,
            industry_confidence=active_comp.confidence,
            company_size=active_comp.normalized_company_size,
            company_size_source=active_comp.source if active_comp.company_size else None,
            company_size_confidence=active_comp.confidence if active_comp.company_size else ConfidenceLevel.UNKNOWN,
            website=active_comp.website,
            location=active_comp.location,
            country=active_comp.country,
            description=active_comp.description,
            last_researched_at=now,
            completeness_score=active_comp.completeness_percent,
            created_at=now,
            updated_at=now,
        )
        session.add(active_lead_intel)

    # Re-link child pain points and opportunities
    await session.execute(delete(LeadIntelligencePainPoint).where(LeadIntelligencePainPoint.lead_id == lead_id))
    await session.execute(delete(LeadIntelligenceOpportunity).where(LeadIntelligenceOpportunity.lead_id == lead_id))

    pains: list[LeadIntelligencePainPoint] = []
    # From Lead.pain_points
    if isinstance(lead.pain_points, list):
        for raw_p in lead.pain_points:
            p_str = _normalize_string(str(raw_p))
            if p_str:
                p_item = LeadIntelligencePainPoint(
                    id=uuid.uuid4(),
                    lead_id=lead.id,
                    title=p_str[:255],
                    description=None,
                    source_type=SourceType.LEAD_RECORD,
                    source_reference="lead.pain_points",
                    confidence=ConfidenceLevel.HIGH,
                    created_at=now,
                    updated_at=now,
                )
                session.add(p_item)
                pains.append(p_item)

    # From canonical Opportunity rows
    opp_models: list[LeadIntelligenceOpportunity] = []
    for opp in opportunities:
        p_item = LeadIntelligencePainPoint(
            id=uuid.uuid4(),
            lead_id=lead.id,
            title=opp.title[:255],
            description=opp.description,
            source_type=SourceType.SYSTEM_DERIVED,
            source_reference=f"opportunity:{opp.id}",
            confidence=ConfidenceLevel.HIGH if opp.severity in {"critical", "high"} else ConfidenceLevel.MEDIUM,
            created_at=now,
            updated_at=now,
        )
        session.add(p_item)
        pains.append(p_item)

        o_item = LeadIntelligenceOpportunity(
            id=uuid.uuid4(),
            lead_id=lead.id,
            title=opp.title[:255],
            description=opp.description,
            opportunity_type=opp.category.upper(),
            score=70 if opp.severity == "high" else 100 if opp.severity == "critical" else 40,
            priority=opp.severity.upper(),
            recommended_action=f"Consulting on {opp.category}",
            evidence={"identity": opp.identity, "references": opp.evidence_references or []},
            source_type=SourceType.SYSTEM_DERIVED,
            source_reference=f"opportunity:{opp.id}",
            confidence=ConfidenceLevel.HIGH,
            created_at=now,
            updated_at=now,
        )
        session.add(o_item)
        opp_models.append(o_item)

    # Fallback to research recommendations if no pipeline opportunities exist yet
    if not opp_models and isinstance(lead.recommended_services, list):
        for idx, svc in enumerate(lead.recommended_services):
            svc_str = _normalize_string(str(svc))
            if svc_str:
                problem_str = None
                if isinstance(lead.pain_points, list) and len(lead.pain_points) > idx:
                    problem_str = str(lead.pain_points[idx])
                elif isinstance(lead.website_analysis, dict):
                    problem_str = lead.website_analysis.get("primary_problem")

                o_item = LeadIntelligenceOpportunity(
                    id=uuid.uuid4(),
                    lead_id=lead.id,
                    title=svc_str[:255],
                    description=problem_str,
                    opportunity_type="DIGITAL_GROWTH",
                    score=75,
                    priority="HIGH",
                    recommended_action=f"Deliver service: {svc_str}",
                    evidence=lead.website_analysis if isinstance(lead.website_analysis, dict) else {},
                    source_type=SourceType.LEAD_RECORD,
                    source_reference="lead.recommended_services",
                    confidence=ConfidenceLevel.HIGH if problem_str else ConfidenceLevel.MEDIUM,
                    created_at=now,
                    updated_at=now,
                )
                session.add(o_item)
                opp_models.append(o_item)

    # Derive and persist deterministic Sales Opportunities from fresh Buying Signals
    from app.domain.sales_opportunities import derive_opportunity_from_signals
    derived_opps = derive_opportunity_from_signals(lead.id, new_signals)
    for d_opp in derived_opps:
        session.add(d_opp)
        opp_models.append(d_opp)

    # Derive and persist evidence-grounded Sales Opportunities from 7-Category findings
    from app.domain.digital_marketing_intelligence import derive_opportunities_from_seven_categories
    cov_opps = derive_opportunities_from_seven_categories(lead.id, digital_profile.seven_category_coverage)
    for c_opp in cov_opps:
        is_dup = any(
            (c_opp.description and ex.description and c_opp.description.strip().lower() == ex.description.strip().lower())
            or c_opp.title.strip().lower() == ex.title.strip().lower()
            for ex in opp_models
        )
        if not is_dup:
            session.add(c_opp)
            opp_models.append(c_opp)

    await session.commit()
    await session.refresh(active_comp)

    try:
        from app.services.sales_action_planning import synchronize_opportunity_tasks
        await synchronize_opportunity_tasks(session, lead_id)
    except Exception as exc:
        logger.debug('Task synchronization non-fatal: %s', exc)

    return build_unified_lead_intelligence_schema(lead, active_comp, pains, opp_models)


async def execute_lead_research_and_refresh(
    session_factory: async_sessionmaker,
    lead_id: uuid.UUID,
) -> LeadIntelligenceProfileSchema:
    """
    Executes actual lead research:
    1. Inspects public website and gathers evidence via enrichment pipeline / website analyzer.
    2. Derives and persists canonical opportunities, recommendations, and lead score.
    3. Builds and synchronizes all unified intelligence layers.
    """
    async with session_factory() as session:
        lead = await session.get(Lead, lead_id)
        if not lead:
            raise ValueError(f"Lead {lead_id} not found")

        # If lead has a website, inspect it with WebsiteAnalyzer to gather real web evidence
        if lead.website:
            try:
                analyzer = WebsiteAnalyzer()
                web_result = await analyzer.analyze(lead.website)
                if isinstance(web_result, dict) and web_result.get("available"):
                    # Check if an enrichment run exists or create one
                    now = now_utc()
                    run = await session.scalar(
                        select(EnrichmentRun)
                        .where(EnrichmentRun.lead_id == lead_id)
                        .order_by(EnrichmentRun.created_at.desc())
                        .limit(1)
                    )
                    if not run:
                        run = EnrichmentRun(
                            lead_id=lead_id,
                            processing_job_id=None,
                            status="completed",
                            completed_at=now,
                            started_at=now,
                            pipeline_version="unified-refresh-v1",
                        )
                        session.add(run)
                        await session.commit()

                    # Persist evidence inputs
                    from app.services.enrichment_pipeline import _website_evidence, _persist_stage_result, EnrichmentStage
                    from app.domain.enrichment import LeadInput
                    lead_input = LeadInput(
                        lead_id=lead.id,
                        enrichment_run_id=run.id,
                        company_name=lead.company_name,
                        website=lead.website,
                        phone=lead.phone,
                        city=lead.city,
                        state=lead.state,
                        country=lead.country,
                    )
                    evidence_inputs = _website_evidence(lead_input, web_result)
                    await _persist_stage_result(session_factory, lead.id, run.id, EnrichmentStage.WEBSITE, evidence_inputs, web_result)

                    # Derive canonical opportunities, recommendations, and scores
                    await derive_and_persist_intelligence(session_factory, lead.id, run.id)
            except Exception as err:
                logger.warning("Website analysis during refresh encountered non-fatal error: %s", err)

    # Sync unified intelligence profile
    async with session_factory() as session:
        return await sync_unified_intelligence_for_lead(session, lead_id, session_factory=session_factory)

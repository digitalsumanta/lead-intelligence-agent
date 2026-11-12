"""
Regression and quality validation suite for Company Enrichment and Digital Marketing Intelligence.

Covers all 9 required verification scenarios:
1. Company with clear industry evidence
2. Company with insufficient industry evidence
3. Company with website but no SEO evidence
4. Company with verified advertising signals
5. Company with inaccessible / missing social profiles
6. Company with no content marketing signals
7. Company with failed / unavailable external research
8. Company with partial research results
9. Company with complete research results
10. Repeated refresh operations never overwrite identified industry with UNKNOWN
11. End-to-end Rubye Beauty profile evaluation
"""

import uuid
from decimal import Decimal
import pytest
from sqlalchemy import select

from app.models.entities import (
    ConfidenceLevel,
    Evidence,
    Lead,
    Opportunity,
    SourceType,
    CompanyEnrichmentProfile,
    LeadIntelligenceProfile,
    WebsiteIntelligenceProfile,
    DigitalMarketingIntelligenceProfile,
    BuyingSignal,
)
from app.domain.company_enrichment import (
    classify_industry_from_evidence,
    calculate_company_completeness,
)
from app.domain.lead_intelligence import _normalize_industry
from app.services.unified_intelligence import (
    build_unified_company_profile,
    build_unified_digital_marketing_profile,
    sync_unified_intelligence_for_lead,
)


# 1. Company with clear industry evidence
def test_company_with_clear_industry_evidence():
    lead_ind = "Skincare & Personal Care"
    norm = _normalize_industry(lead_ind)
    assert norm == "BEAUTY_AND_PERSONAL_CARE"

    readable, normalized, src, conf = classify_industry_from_evidence(
        lead_industry=lead_ind,
        company_name="Rubye Beauty Pvt Ltd",
    )
    assert readable == "Skincare & Personal Care"
    assert normalized == "BEAUTY_AND_PERSONAL_CARE"
    assert src == SourceType.LEAD_RECORD
    assert conf == ConfidenceLevel.HIGH


# 2. Company with insufficient industry evidence
def test_company_with_insufficient_industry_evidence():
    readable, normalized, src, conf = classify_industry_from_evidence(
        lead_industry=None,
        company_name="Global Acme Holdings Inc",
        website_analysis={},
    )
    assert readable is None
    assert normalized == "UNKNOWN"
    assert conf == ConfidenceLevel.UNKNOWN


# 3. Company with website but no SEO evidence
def test_company_with_website_but_no_seo_evidence():
    lead = Lead(
        id=uuid.uuid4(),
        company_name="Alpha Tech",
        website="https://alphatech.example.com",
    )
    dm = build_unified_digital_marketing_profile(lead=lead, evidence_list=[], opportunities=[])
    assert dm.seo_signals is not None
    assert dm.seo_signals["status"] == "NOT_RESEARCHED"
    assert dm.seo_signals["researched"] is False
    assert "not been conducted" in dm.seo_signals["reason"]


# 4. Company with verified advertising signals
def test_company_with_verified_advertising_signals():
    lead = Lead(
        id=uuid.uuid4(),
        company_name="Rubye Beauty Pvt Ltd",
        website="https://rubyeorganics.com",
        website_analysis={
            "advertising_platforms": "Meta Ads, Google Ads, Google Shopping",
            "advertising_evidence": "Active ad campaigns detected across Meta Ad Library and Google Shopping",
        },
    )
    dm = build_unified_digital_marketing_profile(lead=lead, evidence_list=[], opportunities=[])
    assert dm.paid_advertising_signals is not None
    assert dm.paid_advertising_signals["status"] == "SIGNAL_DETECTED"
    assert dm.paid_advertising_signals["researched"] is True
    assert dm.paid_advertising_signals["platforms"] == "Meta Ads, Google Ads, Google Shopping"
    assert dm.paid_advertising_signals["evidence"] == "Active ad campaigns detected across Meta Ad Library and Google Shopping"
    assert dm.category_research_status["paid_advertising"] == "SIGNAL_DETECTED"


# 5. Company with inaccessible / checked but empty social profiles
def test_company_with_inaccessible_social_profiles():
    lead = Lead(
        id=uuid.uuid4(),
        company_name="Solo Builder",
        website="https://solobuilder.example.com",
        website_analysis={"social_profiles_checked": True},
    )
    dm = build_unified_digital_marketing_profile(lead=lead, evidence_list=[], opportunities=[])
    assert dm.social_media_signals is not None
    assert dm.social_media_signals["status"] == "NO_SIGNAL_FOUND"
    assert dm.social_media_signals["researched"] is True
    assert dm.category_research_status["social_media"] == "NO_SIGNAL_FOUND"


# 6. Company with no content marketing signals
def test_company_with_no_content_marketing_signals():
    lead = Lead(
        id=uuid.uuid4(),
        company_name="Quick Plumber",
        website="https://quickplumber.example.com",
        website_analysis={"content_marketing_checked": True},
    )
    dm = build_unified_digital_marketing_profile(lead=lead, evidence_list=[], opportunities=[])
    assert dm.content_marketing_signals is not None
    assert dm.content_marketing_signals["status"] == "NO_SIGNAL_FOUND"
    assert dm.content_marketing_signals["researched"] is True
    assert dm.category_research_status["content_marketing"] == "NO_SIGNAL_FOUND"


# 7. Company with failed / unavailable external research
def test_company_with_failed_external_research():
    lead_no_web = Lead(
        id=uuid.uuid4(),
        company_name="Offline Store",
        website=None,
    )
    dm = build_unified_digital_marketing_profile(lead=lead_no_web, evidence_list=[], opportunities=[])
    assert dm.seo_signals["status"] == "UNAVAILABLE"
    assert dm.paid_advertising_signals["status"] == "UNAVAILABLE"
    assert dm.social_media_signals["status"] == "UNAVAILABLE"
    assert dm.content_marketing_signals["status"] == "UNAVAILABLE"


# 8. Company with partial research results (e.g. only 2 of 7 categories researched)
def test_company_with_partial_research_results():
    lead = Lead(
        id=uuid.uuid4(),
        company_name="Rubye Beauty Pvt Ltd",
        website="https://rubyeorganics.com",
        website_analysis={
            "advertising_platforms": "Meta Ads, Google Ads",
            "primary_problem": "High paid ad spend with unoptimized product landing page conversion paths",
        },
    )
    dm = build_unified_digital_marketing_profile(lead=lead, evidence_list=[], opportunities=[])
    # Paid advertising (SIGNAL_DETECTED) and conversion_optimization (SIGNAL_DETECTED) are researched -> 2/7 = 28.57%
    assert dm.completeness == Decimal("28.57")
    status = dm.category_research_status
    assert status["paid_advertising"] == "SIGNAL_DETECTED"
    assert status["conversion_optimization"] == "SIGNAL_DETECTED"
    assert status["seo"] == "NOT_RESEARCHED"
    assert status["social_media"] == "NOT_RESEARCHED"
    assert status["content_marketing"] == "NOT_RESEARCHED"
    assert status["online_reputation"] in ("NOT_RESEARCHED", "UNAVAILABLE")


# 9. Company with complete research results (all 7 categories researched)
def test_company_with_complete_research_results():
    lead_id = uuid.uuid4()
    lead = Lead(
        id=lead_id,
        company_name="Omni Enterprise",
        website="https://omni.example.com",
        website_analysis={
            "advertising_platforms": "Google Ads",
            "social_profiles": ["https://linkedin.com/company/omni"],
            "content_marketing": {"active": True, "blog": "https://omni.example.com/blog"},
            "primary_problem": "CRO optimization",
        },
    )
    ev_seo = Evidence(id=uuid.uuid4(), lead_id=lead_id, evidence_type="pagespeed_seo_score", observed_value=92, source_type=SourceType.SYSTEM_DERIVED)
    ev_perf = Evidence(id=uuid.uuid4(), lead_id=lead_id, evidence_type="pagespeed_performance_score", observed_value=85, source_type=SourceType.SYSTEM_DERIVED)
    ev_rep = Evidence(id=uuid.uuid4(), lead_id=lead_id, evidence_type="google_places_rating", observed_value=4.8, source_type=SourceType.SYSTEM_DERIVED)

    dm = build_unified_digital_marketing_profile(
        lead=lead,
        evidence_list=[ev_seo, ev_perf, ev_rep],
        opportunities=[],
    )
    assert dm.completeness == Decimal("100.00")
    status = dm.category_research_status
    assert all(st == "SIGNAL_DETECTED" for st in status.values())


# 10. Repeated refresh operations never overwrite identified industry with UNKNOWN
@pytest.mark.asyncio
async def test_repeat_refresh_preserves_industry(session_factory):
    async with session_factory() as session:
        lead_id = uuid.uuid4()
        lead = Lead(
            id=lead_id,
            company_name="Rubye Beauty Pvt Ltd",
            website="https://rubyeorganics.com",
            industry="Skincare & Personal Care",
            city="Mumbai",
            country="India",
            website_analysis={
                "sub_category": "Artisanal Organic Lip & Face Balms",
                "advertising_platforms": "Meta Ads, Google Ads, Google Shopping",
            },
        )
        session.add(lead)
        await session.commit()

        # First synchronization
        schema1 = await sync_unified_intelligence_for_lead(session, lead_id)
        assert schema1.company.industry == "Skincare & Personal Care"

        # Check DB entities
        comp1 = (await session.scalars(select(CompanyEnrichmentProfile).where(CompanyEnrichmentProfile.lead_id == lead_id))).first()
        intel1 = (await session.scalars(select(LeadIntelligenceProfile).where(LeadIntelligenceProfile.lead_id == lead_id))).first()
        assert comp1.industry == "Skincare & Personal Care"
        assert comp1.normalized_industry == "BEAUTY_AND_PERSONAL_CARE"
        assert intel1.industry == "Skincare & Personal Care"

        # Second synchronization (refresh)
        schema2 = await sync_unified_intelligence_for_lead(session, lead_id)
        assert schema2.company.industry == "Skincare & Personal Care"

        comp2 = (await session.scalars(select(CompanyEnrichmentProfile).where(CompanyEnrichmentProfile.lead_id == lead_id))).first()
        intel2 = (await session.scalars(select(LeadIntelligenceProfile).where(LeadIntelligenceProfile.lead_id == lead_id))).first()
        assert comp2.industry == "Skincare & Personal Care"
        assert comp2.normalized_industry == "BEAUTY_AND_PERSONAL_CARE"
        assert intel2.industry == "Skincare & Personal Care"
        assert intel2.industry != "UNKNOWN"


# 11. Rubye Beauty Pvt Ltd end-to-end profile evaluation
@pytest.mark.asyncio
async def test_rubye_beauty_e2e_reconciliation(session_factory):
    async with session_factory() as session:
        lead_id = uuid.uuid4()
        lead = Lead(
            id=lead_id,
            company_name="Rubye Beauty Pvt Ltd",
            website="https://rubyeorganics.com",
            industry="Skincare & Personal Care",
            city="Mumbai",
            country="India",
            email="care@rubyeorganics.com",
            linkedin_url="https://www.linkedin.com/search/results/all/?keywords=Ritu%20Sethi%20Rubye%20Organics",
            decision_maker="Ritu Sethi",
            website_analysis={
                "sub_category": "Artisanal Organic Lip & Face Balms",
                "advertising_platforms": "Meta Ads, Google Ads, Google Shopping",
                "advertising_evidence": "Active ad campaigns detected across Meta Ad Library and Google Shopping for Rubye Organics; running multi-angle video and catalog assets.",
                "primary_problem": "High paid ad spend with unoptimized product landing page conversion paths; missing retargeting sequences",
                "employee_count": "11-50",
            },
        )
        session.add(lead)
        await session.commit()

        schema = await sync_unified_intelligence_for_lead(session, lead_id)

        comp = (await session.scalars(select(CompanyEnrichmentProfile).where(CompanyEnrichmentProfile.lead_id == lead_id))).first()
        assert comp is not None
        assert comp.industry == "Skincare & Personal Care"
        assert comp.normalized_industry == "BEAUTY_AND_PERSONAL_CARE"
        assert comp.description == "Artisanal Organic Lip & Face Balms brand based in Mumbai, India."
        assert comp.company_size == "11-50"
        assert comp.normalized_company_size == "SMALL"
        # 7 out of 8 fields populated: legal_name, domain, website, linkedin_url, location, description, industry -> 87.5%
        assert comp.completeness_percent == Decimal("87.50")

        dm = (await session.scalars(select(DigitalMarketingIntelligenceProfile).where(DigitalMarketingIntelligenceProfile.company_id == lead_id))).first()
        assert dm is not None
        assert dm.paid_advertising_signals["status"] == "SIGNAL_DETECTED"
        assert dm.paid_advertising_signals["platforms"] == "Meta Ads, Google Ads, Google Shopping"
        assert dm.seo_signals["status"] == "NOT_RESEARCHED"
        assert dm.social_media_signals["status"] == "NOT_RESEARCHED"
        assert dm.content_marketing_signals["status"] == "NOT_RESEARCHED"
        assert dm.marketing_maturity == "DEVELOPING"
        # 2 of 7 categories researched (paid ads + conversion optimization) -> 28.57%
        assert dm.completeness == Decimal("28.57")

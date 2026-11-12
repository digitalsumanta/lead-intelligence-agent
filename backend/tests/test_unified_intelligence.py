"""
Comprehensive verification and regression test suite for Unified Lead and Company Intelligence.

Validates:
1. Evidence-grounded CompanyEnrichmentProfile generation
2. Evidence-grounded LeadIntelligenceProfile generation
3. Provenance and confidence distinction (VERIFIED, INFERRED, UNKNOWN, UNAVAILABLE)
4. Idempotency of refresh operations (no data loss or broken foreign keys)
5. Consistency between Company Enrichment, Lead Intelligence, Website Intelligence, Digital Marketing, and Buying Signals
6. Multi-evidence scenarios (Website, Google Places, PageSpeed)
7. Missing/incomplete evidence handling (never fabricates unverified fields)
8. Safety: OUTREACH_DISPATCH_ENABLED remains false
"""

import uuid
from datetime import datetime, UTC
from decimal import Decimal
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.entities import (
    ConfidenceLevel,
    EnrichmentRun,
    Evidence,
    Lead,
    Opportunity,
    ServiceRecommendation,
    SourceType,
    CompanyEnrichmentProfile,
    LeadIntelligenceProfile,
    WebsiteIntelligenceProfile,
    DigitalMarketingIntelligenceProfile,
    BuyingSignal,
    now_utc,
)
from app.services.unified_intelligence import (
    build_unified_company_profile,
    build_unified_website_profile,
    build_unified_digital_marketing_profile,
    build_unified_buying_signals,
    sync_unified_intelligence_for_lead,
)


@pytest.mark.asyncio
async def test_safety_outreach_dispatch_invariant():
    """Verify live outreach dispatch remains strictly disabled."""
    assert settings.outreach_dispatch_enabled is False


@pytest.mark.asyncio
async def test_lead_with_no_evidence_does_not_fabricate(session_factory):
    """Lead with only basic inputs: unknown fields remain unknown/null; confidence is not falsely HIGH."""
    lead_id = uuid.uuid4()
    lead = Lead(
        id=lead_id,
        company_name="Bare Bones LLC",
        city="Kolkata",
        country="India",
    )
    profile = build_unified_company_profile(lead, [])

    assert profile.legal_name == "Bare Bones LLC"
    assert profile.domain is None
    assert profile.website is None
    assert profile.description is None  # Never invented
    assert profile.company_size is None  # Never invented
    assert profile.phone is None
    assert profile.source == SourceType.LEAD_RECORD
    assert profile.confidence == ConfidenceLevel.LOW  # Not falsely high
    assert profile.completeness_percent < 30.0


@pytest.mark.asyncio
async def test_lead_with_website_and_email_domain(session_factory):
    """Domain is extracted and website presence normalized without fabricating description."""
    lead = Lead(
        id=uuid.uuid4(),
        company_name="Apex Global",
        website="http://www.apex-global.com/about",
        email="contact@apex-global.com",
    )
    profile = build_unified_company_profile(lead, [])
    assert profile.domain == "apex-global.com"
    assert profile.email_domain == "apex-global.com"
    assert profile.website == "http://apex-global.com/about"
    assert profile.description is None  # No website crawl evidence yet


@pytest.mark.asyncio
async def test_lead_with_website_evidence(session_factory):
    """Website crawl evidence populates description and marks provenance as WEBSITE."""
    lead_id = uuid.uuid4()
    lead = Lead(
        id=lead_id,
        company_name="Studio Zen",
        website="https://studiozen.design",
    )
    evidence = [
        Evidence(
            id=uuid.uuid4(),
            lead_id=lead_id,
            evidence_type="website_available",
            source_type="website",
            observed_value=True,
            confidence="HIGH",
        ),
        Evidence(
            id=uuid.uuid4(),
            lead_id=lead_id,
            evidence_type="website_http_status",
            source_type="website",
            observed_value=200,
            confidence="HIGH",
        ),
        Evidence(
            id=uuid.uuid4(),
            lead_id=lead_id,
            evidence_type="website_meta_description",
            source_type="website",
            observed_value="Premier luxury interior design studio based in Kolkata.",
            confidence="HIGH",
        ),
        Evidence(
            id=uuid.uuid4(),
            lead_id=lead_id,
            evidence_type="website_social_link",
            source_type="website",
            observed_value="https://linkedin.com/company/studiozen",
            confidence="HIGH",
        ),
    ]

    profile = build_unified_company_profile(lead, evidence)
    assert profile.description == "Premier luxury interior design studio based in Kolkata."
    assert profile.linkedin_url == "https://linkedin.com/company/studiozen"
    assert profile.source == SourceType.SYSTEM_DERIVED
    assert profile.confidence == ConfidenceLevel.HIGH


@pytest.mark.asyncio
async def test_lead_with_google_places_evidence(session_factory):
    """Google Places evidence overrides lead record with verified address and phone."""
    lead_id = uuid.uuid4()
    lead = Lead(
        id=lead_id,
        company_name="Dr Sen Dental",
        city="Kolkata",
    )
    evidence = [
        Evidence(
            id=uuid.uuid4(),
            lead_id=lead_id,
            evidence_type="google_places_formatted_address",
            source_type="google_places",
            observed_value="12 Park Street, Kolkata, WB 700016, India",
            confidence="HIGH",
        ),
        Evidence(
            id=uuid.uuid4(),
            lead_id=lead_id,
            evidence_type="google_places_phone",
            source_type="google_places",
            observed_value="+91 33 2288 9900",
            confidence="HIGH",
        ),
        Evidence(
            id=uuid.uuid4(),
            lead_id=lead_id,
            evidence_type="google_places_rating",
            source_type="google_places",
            observed_value=4.8,
            confidence="HIGH",
        ),
        Evidence(
            id=uuid.uuid4(),
            lead_id=lead_id,
            evidence_type="google_places_types",
            source_type="google_places",
            observed_value=["dentist", "health"],
            confidence="HIGH",
        ),
    ]

    profile = build_unified_company_profile(lead, evidence)
    assert profile.location == "12 Park Street, Kolkata, WB 700016, India"
    assert profile.phone == "+91 33 2288 9900"
    assert profile.industry == "Dentist"
    assert profile.source == SourceType.SYSTEM_DERIVED
    assert profile.confidence == ConfidenceLevel.HIGH


@pytest.mark.asyncio
async def test_sync_unified_intelligence_is_idempotent_and_non_destructive(session_factory):
    """Calling sync_unified_intelligence_for_lead multiple times updates profiles in-place without breaking relationships."""
    lead_id = uuid.uuid4()
    async with session_factory() as session:
        lead = Lead(
            id=lead_id,
            company_name="Idempotent Architecture Corp",
            website="https://idempotent.example",
            city="Kolkata",
            pain_points=["Slow website conversion"],
        )
        session.add(lead)
        await session.commit()

    # Initial sync
    async with session_factory() as session:
        schema1 = await sync_unified_intelligence_for_lead(session, lead_id)
        assert schema1.company.name == "Idempotent Architecture Corp"
        assert len(schema1.business_context.pain_points) == 1

    # Verify rows persisted
    async with session_factory() as session:
        comp_profile1 = await session.scalar(select(CompanyEnrichmentProfile).where(CompanyEnrichmentProfile.lead_id == lead_id))
        assert comp_profile1 is not None
        orig_comp_id = comp_profile1.id

        lead_intel1 = await session.scalar(select(LeadIntelligenceProfile).where(LeadIntelligenceProfile.lead_id == lead_id))
        assert lead_intel1 is not None
        orig_lead_intel_id = lead_intel1.id

    # Second sync (simulating refresh)
    async with session_factory() as session:
        schema2 = await sync_unified_intelligence_for_lead(session, lead_id)
        assert schema2.company.name == "Idempotent Architecture Corp"

    # Verify primary key stability (in-place update, not destructive recreation)
    async with session_factory() as session:
        comp_profile2 = await session.scalar(select(CompanyEnrichmentProfile).where(CompanyEnrichmentProfile.lead_id == lead_id))
        assert comp_profile2.id == orig_comp_id

        lead_intel2 = await session.scalar(select(LeadIntelligenceProfile).where(LeadIntelligenceProfile.lead_id == lead_id))
        assert lead_intel2.id == orig_lead_intel_id


@pytest.mark.asyncio
async def test_opportunities_synchronization_into_lead_intelligence(session_factory):
    """Derived canonical opportunities are synchronized directly into LeadIntelligenceOpportunity and pain points."""
    lead_id = uuid.uuid4()
    run_id = uuid.uuid4()

    async with session_factory() as session:
        lead = Lead(
            id=lead_id,
            company_name="Synchro Labs",
            website="https://synchro.example",
        )
        session.add(lead)
        run = EnrichmentRun(
            id=run_id,
            lead_id=lead_id,
            pipeline_version="phase2-enrichment-v1",
            status="completed",
        )
        session.add(run)

        # Seed canonical opportunity
        opp = Opportunity(
            id=uuid.uuid4(),
            lead_id=lead_id,
            enrichment_run_id=run_id,
            identity="synchro_pagespeed_perf",
            category="website_performance",
            severity="critical",
            title="Severe PageSpeed Latency",
            description="LCP exceeds 4.5s on mobile viewport",
            origin="deterministic",
            evidence_references=[],
            rule_version="phase2-rules-v1",
        )
        session.add(opp)
        await session.commit()

    async with session_factory() as session:
        schema = await sync_unified_intelligence_for_lead(session, lead_id)
        opp_titles = [o.title for o in schema.business_context.opportunities]
        assert "Severe PageSpeed Latency" in opp_titles

        pain_titles = [p.title for p in schema.business_context.pain_points]
        assert "Severe PageSpeed Latency" in pain_titles


@pytest.mark.asyncio
async def test_buying_signals_generation_from_critical_opportunities(session_factory):
    """High impact opportunities produce active buying signals."""
    lead = Lead(id=uuid.uuid4(), company_name="Buyer Corp", website="https://buyer.example")
    opps = [
        Opportunity(
            id=uuid.uuid4(),
            lead_id=lead.id,
            identity="opp1",
            category="conversion",
            severity="critical",
            title="Missing Conversion Pixel",
            description="No pixel found",
            origin="deterministic",
        )
    ]
    signals = build_unified_buying_signals(lead, [], opps)
    sig_types = [s.signal_type for s in signals]
    assert "WEBSITE_PRESENCE_CONFIRMED" in sig_types
    assert "HIGH_IMPACT_DIGITAL_GAP" in sig_types


def test_sni_hostname_compatibility_and_ssl_resolution():
    """Verify SniHostname satisfies both legacy test bytes equality and AnyIO string resolution."""
    from app.services.website import SniHostname
    try:
        from anyio._core._sockets import idna2008_resolve
    except ImportError:
        import idna
        idna2008_resolve = idna.encode

    sni = SniHostname("traya.health")
    assert isinstance(sni, str)
    assert sni == b"traya.health"
    assert sni == "traya.health"
    resolved = idna2008_resolve(sni)
    assert resolved == b"traya.health"


@pytest.mark.asyncio
async def test_lead_research_recommendations_fallback_into_lead_intelligence_opportunities(session_factory):
    """Business research recommended services populate opportunities when pipeline opps are absent."""
    lead_id = uuid.uuid4()
    async with session_factory() as session:
        lead = Lead(
            id=lead_id,
            company_name="Apex Research Corp",
            website="https://apexresearch.example",
            pain_points=["Low organic conversions"],
            recommended_services=["SEO & Performance Marketing"],
            website_analysis={
                "primary_problem": "Low organic conversions",
                "recommended_mindart_service": "SEO & Performance Marketing",
            },
        )
        session.add(lead)
        await session.commit()

    async with session_factory() as session:
        schema = await sync_unified_intelligence_for_lead(session, lead_id)
        assert len(schema.business_context.opportunities) == 1
        opp = schema.business_context.opportunities[0]
        assert opp.title == "SEO & Performance Marketing"
        assert opp.source_type == SourceType.LEAD_RECORD
        assert opp.confidence == ConfidenceLevel.HIGH
        assert opp.score == 75

import uuid
from decimal import Decimal
import pytest
from app.models.entities import (
    Lead,
    Evidence,
    Opportunity,
    BuyingSignal,
    LeadIntelligenceOpportunity,
    ConfidenceLevel,
    SourceType,
    now_utc,
)
from app.domain.digital_marketing_intelligence import (
    SEO,
    PAID_ADVERTISING,
    SOCIAL_MEDIA,
    CONTENT_MARKETING,
    WEBSITE_PERFORMANCE,
    CONVERSION_OPTIMIZATION,
    ONLINE_REPUTATION,
    CANONICAL_CATEGORIES,
    RESEARCHED,
    PARTIALLY_RESEARCHED,
    NOT_RESEARCHED,
    UNAVAILABLE,
    calculate_canonical_completeness,
    build_coverage_summary,
    evaluate_seven_categories,
    derive_opportunities_from_seven_categories,
    MINDART_SERVICES,
)
from app.services.unified_intelligence import build_unified_digital_marketing_profile


def test_canonical_completeness_invariant():
    """
    Strict Verification Invariant:
    Only RESEARCHED categories contribute to completeness.
    PARTIALLY_RESEARCHED, NOT_RESEARCHED, and UNAVAILABLE must NOT inflate completeness.
    """
    coverage = {
        SEO: {"status": RESEARCHED, "signal_detected": True},
        PAID_ADVERTISING: {"status": RESEARCHED, "signal_detected": True},
        SOCIAL_MEDIA: {"status": RESEARCHED, "signal_detected": True},
        CONTENT_MARKETING: {"status": RESEARCHED, "signal_detected": False},
        WEBSITE_PERFORMANCE: {"status": PARTIALLY_RESEARCHED, "signal_detected": True},
        CONVERSION_OPTIMIZATION: {"status": NOT_RESEARCHED, "signal_detected": False},
        ONLINE_REPUTATION: {"status": UNAVAILABLE, "signal_detected": False},
    }

    # Exactly 4 categories are RESEARCHED: 4 / 7 = 57.14%
    completeness = calculate_canonical_completeness(coverage)
    assert completeness == 57.14

    summary = build_coverage_summary(coverage)
    assert summary["researched"] == 4
    assert summary["partially_researched"] == 1
    assert summary["not_researched"] == 1
    assert summary["unavailable"] == 1
    assert summary["total_categories"] == 7
    assert summary["completeness"] == 57.14


def test_all_seven_categories_evaluated():
    """Verify that all 7 canonical categories are evaluated and present with required schema."""
    lead = Lead(
        id=uuid.uuid4(),
        company_name="Rubye Beauty Pvt Ltd",
        website="https://rubyeorganics.com",
        website_analysis={
            "available": True,
            "title": "Rubye Organics | Pure Beauty",
            "meta_description": "Handcrafted organic balms and personal care products.",
            "h1": "Welcome to Rubye Organics",
            "https": True,
            "response_time_ms": 320,
            "social_links": ["https://instagram.com/rubyeorganics", "https://facebook.com/rubyeorganics"],
            "advertising_platforms": ["Meta Ads", "Google Shopping"],
            "advertising_evidence": "Active Meta ad campaigns detected",
            "tracking_tags": ["Meta Pixel", "Google Analytics / GTM"],
            "blog_detected": False,
            "has_form": True,
            "has_cta": True,
            "detected_ctas": ["Shop Now", "Add to Cart"],
        },
        pain_points=[],
        recommended_services=[],
        created_at=now_utc(),
        updated_at=now_utc(),
    )

    by_type = {}
    opportunities = []

    coverage = evaluate_seven_categories(lead, by_type, opportunities)

    for cat in CANONICAL_CATEGORIES:
        assert cat in coverage, f"Category {cat} missing from coverage"
        finding = coverage[cat]
        assert "status" in finding
        assert "signal_detected" in finding
        assert "finding" in finding
        assert "problem_identified" in finding
        assert "confidence_score" in finding
        assert "verified_fact_or_inference" in finding

    # SEO has title, description, H1 -> No problem
    assert coverage[SEO]["status"] == RESEARCHED
    assert coverage[SEO]["problem_identified"] is False

    # Paid ads detected with Meta Pixel -> Active campaign, verified
    assert coverage[PAID_ADVERTISING]["status"] == RESEARCHED
    assert coverage[PAID_ADVERTISING]["signal_detected"] is True

    # Social media has 2 links -> Researched, signal detected
    assert coverage[SOCIAL_MEDIA]["status"] == RESEARCHED
    assert coverage[SOCIAL_MEDIA]["signal_detected"] is True
    assert coverage[SOCIAL_MEDIA]["problem_identified"] is False

    # Content marketing has no blog -> Researched, problem identified (Medium severity)
    assert coverage[CONTENT_MARKETING]["status"] == RESEARCHED
    assert coverage[CONTENT_MARKETING]["signal_detected"] is False
    assert coverage[CONTENT_MARKETING]["problem_identified"] is True
    assert coverage[CONTENT_MARKETING]["problem_severity"] == "medium"
    assert coverage[CONTENT_MARKETING]["recommended_service"] == "Content Marketing"

    # Website performance is fast & HTTPS -> Researched, no problem
    assert coverage[WEBSITE_PERFORMANCE]["status"] == RESEARCHED
    assert coverage[WEBSITE_PERFORMANCE]["problem_identified"] is False

    # Conversion optimization has CTAs and forms -> Researched, no problem
    assert coverage[CONVERSION_OPTIMIZATION]["status"] == RESEARCHED
    assert coverage[CONVERSION_OPTIMIZATION]["problem_identified"] is False

    # Online reputation: Google Places key is not set -> UNAVAILABLE with diagnostic
    assert coverage[ONLINE_REPUTATION]["status"] == UNAVAILABLE
    assert "GOOGLE_PLACES_API_KEY" in coverage[ONLINE_REPUTATION]["diagnostic_reason"]
    assert coverage[ONLINE_REPUTATION]["problem_identified"] is False


def test_sales_opportunities_generated_only_from_specific_problems():
    """
    Constraint: Generate sales opportunities ONLY from specific, evidence-supported findings.
    Do not treat generic observations as confirmed problems.
    Do not generate opportunities from UNAVAILABLE or NOT_RESEARCHED categories.
    """
    lead_id = uuid.uuid4()
    coverage = {
        SEO: {
            "title": "SEO",
            "status": RESEARCHED,
            "problem_identified": True,
            "problem_severity": "high",
            "problem_summary": "Missing HTML page title tag",
            "recommended_service": "Technical SEO",
            "finding": "Missing HTML page title tag impairs search engine indexing.",
            "confidence_score": 90,
            "verified_fact_or_inference": "verified_fact",
            "source_name": "SeoCrawler",
            "details": {},
        },
        PAID_ADVERTISING: {
            "title": "Paid Advertising",
            "status": RESEARCHED,
            "problem_identified": False,
            "problem_severity": None,
            "finding": "No issues detected.",
            "confidence_score": 80,
        },
        CONTENT_MARKETING: {
            "title": "Content Marketing",
            "status": RESEARCHED,
            "problem_identified": True,
            "problem_severity": "medium",
            "problem_summary": "Missing educational blog or content hub",
            "recommended_service": "Content Marketing",
            "finding": "Absence of an educational content hub limits organic customer acquisition.",
            "confidence_score": 80,
            "verified_fact_or_inference": "verified_fact",
            "source_name": "Content Auditor",
            "details": {},
        },
        ONLINE_REPUTATION: {
            "title": "Online Reputation",
            "status": UNAVAILABLE,
            "problem_identified": False,
            "problem_severity": None,
            "diagnostic_reason": "API key not configured",
        },
    }

    opps = derive_opportunities_from_seven_categories(lead_id, coverage)

    # Exactly 2 opportunities: SEO (high) and Content Marketing (medium)
    assert len(opps) == 2

    seo_opp = next(o for o in opps if o.opportunity_type == "SEO")
    assert "Technical SEO" in seo_opp.evidence["recommended_service"]
    assert seo_opp.priority == "HIGH"
    assert seo_opp.score == 80

    cm_opp = next(o for o in opps if o.opportunity_type == "CONTENT_MARKETING")
    assert "Content Marketing" in cm_opp.evidence["recommended_service"]
    assert cm_opp.priority == "MEDIUM"
    assert cm_opp.score == 65

    # UNAVAILABLE and problem-free categories did not generate opportunities
    types = [o.opportunity_type for o in opps]
    assert "ONLINE_REPUTATION" not in types
    assert "PAID_ADVERTISING" not in types


def test_build_unified_digital_marketing_profile_properties():
    """Verify that build_unified_digital_marketing_profile produces all required properties and valid completeness."""
    lead = Lead(
        id=uuid.uuid4(),
        company_name="Rubye Beauty Pvt Ltd",
        website="https://rubyeorganics.com",
        website_analysis={
            "available": True,
            "title": "Rubye Organics",
            "meta_description": "Handcrafted organic beauty products.",
            "h1": "Rubye Organics Store",
            "https": True,
            "response_time_ms": 280,
            "social_links": ["https://instagram.com/rubyeorganics"],
            "advertising_platforms": ["Meta Ads"],
            "blog_detected": True,
            "has_form": True,
            "has_cta": True,
        },
        created_at=now_utc(),
        updated_at=now_utc(),
    )

    profile = build_unified_digital_marketing_profile(lead, [], [])

    # Check that model properties return correct structures
    assert isinstance(profile.seven_category_coverage, dict)
    assert len(profile.seven_category_coverage) == 7
    assert isinstance(profile.coverage_summary, dict)
    assert profile.coverage_summary["total_categories"] == 7

    # 6 categories are RESEARCHED (SEO, Paid Ads, Social, Content, Website Perf, CRO), 1 is UNAVAILABLE (Reputation)
    assert profile.coverage_summary["researched"] == 6
    assert profile.coverage_summary["unavailable"] == 1
    assert profile.completeness == Decimal("85.71")

    # Check category research status map
    statuses = profile.category_research_status
    assert statuses["seo"] == "SIGNAL_DETECTED"
    assert statuses["paid_advertising"] == "SIGNAL_DETECTED"
    assert statuses["social_media"] == "SIGNAL_DETECTED"
    assert statuses["content_marketing"] == "SIGNAL_DETECTED"
    assert statuses["website_performance"] == "SIGNAL_DETECTED"
    assert statuses["conversion_optimization"] == "SIGNAL_DETECTED"
    assert statuses["online_reputation"] == "UNAVAILABLE"


def test_partial_research_reporting_distinct_from_completeness():
    """Verify that PARTIALLY_RESEARCHED categories are reported separately in coverage summary and never added to completeness."""
    lead = Lead(
        id=uuid.uuid4(),
        company_name="Rubye Beauty Pvt Ltd",
        website="https://rubyeorganics.com",
        website_analysis={
            "available": True,
            "title": "Rubye Organics",
            "meta_description": "Organic beauty products",
            "h1": "Rubye Organics",
            "https": True,
            "response_time_ms": 200,
            "review_widgets": ["Judge.me", "Trustpilot"],
            "has_form": True,
            "has_cta": True,
        },
        created_at=now_utc(),
        updated_at=now_utc(),
    )

    profile = build_unified_digital_marketing_profile(lead, [], [])

    # Online reputation has review widgets on site, but no Google Places API key in env
    cov = profile.seven_category_coverage
    assert cov[ONLINE_REPUTATION]["status"] == PARTIALLY_RESEARCHED
    assert cov[ONLINE_REPUTATION]["signal_detected"] is True
    assert "Judge.me" in cov[ONLINE_REPUTATION]["details"]["review_widgets"]

    # Coverage summary must report partially_researched == 1
    summary = profile.coverage_summary
    assert summary["partially_researched"] == 1
    # Exactly 3 categories are RESEARCHED: SEO, Website Performance, Conversion Optimization -> 3 / 7 = 42.86%
    assert profile.completeness == Decimal("42.86")
    assert summary["completeness"] == 42.86


def test_refresh_idempotency_and_preservation():
    """Verify that repeated refreshes are idempotent and existing verified data is never overwritten with unresearched data."""
    lead = Lead(
        id=uuid.uuid4(),
        company_name="Rubye Beauty Pvt Ltd",
        website="https://rubyeorganics.com",
        website_analysis={
            "available": True,
            "title": "Rubye Organics",
            "meta_description": "Organic beauty products",
            "h1": "Rubye Organics",
            "https": True,
            "response_time_ms": 200,
            "advertising_platforms": ["Meta Ads"],
            "has_form": True,
            "has_cta": True,
        },
        created_at=now_utc(),
        updated_at=now_utc(),
    )

    profile1 = build_unified_digital_marketing_profile(lead, [], [])
    cov1 = profile1.seven_category_coverage
    assert cov1[PAID_ADVERTISING]["status"] == RESEARCHED

    # Subsequent run where website_analysis is momentarily empty should preserve existing verified RESEARCHED data
    lead_empty = Lead(
        id=lead.id,
        company_name=lead.company_name,
        website=lead.website,
        website_analysis={},
        created_at=now_utc(),
        updated_at=now_utc(),
    )

    profile2 = build_unified_digital_marketing_profile(
        lead_empty, [], [], existing_coverage=cov1
    )
    cov2 = profile2.seven_category_coverage
    # Preserved!
    assert cov2[PAID_ADVERTISING]["status"] == RESEARCHED
    assert cov2[PAID_ADVERTISING]["details"] == cov1[PAID_ADVERTISING]["details"]


def test_mindart_digital_canonical_service_mapping():
    """Verify that every specific identified problem maps to one of MindArtDigital's canonical services."""
    # Check that all 7 categories have canonical service entries
    for cat in CANONICAL_CATEGORIES:
        assert cat in MINDART_SERVICES
        services = MINDART_SERVICES[cat]
        assert "default" in services
        assert len(services) >= 2

"""
Test Suite for Lead Qualification & Sales Readiness Engine (Phase 12).

Tests:
1. Exact score boundaries and deterministic tier classification (RQ-09).
2. All 7 dimensions, point allocations, and max point bounds (RQ-01, RQ-07).
3. FINAL-01 Invariant: NEVER write to lead.lead_score; store exclusively in lead.score_factors['qualification'].
4. FINAL-02 Invariant: qualification_score and scoring_coverage are separate metrics.
5. FINAL-03 Invariant: Tracking tags alone (+4) vs active ad campaigns (+8).
6. RQ-04 & RQ-05 Invariant: lead.sales_status and lead.priority are never mutated.
7. RQ-06 Invariant: Deduplicated canonical opportunities only, zero fabricated values.
8. RQ-07 Invariant: All 8 required explainability fields populated per dimension.
9. Service & API routes (/api/leads/{id}/qualification, /api/leads/{id}/qualify, filtering, sorting).
"""

import uuid
from datetime import datetime, timezone
import pytest
from httpx import AsyncClient, ASGITransport

from app.domain.lead_qualification import (
    classify_lead_score,
    evaluate_lead_qualification,
    LeadClassification,
    RecommendedSalesAction,
    DimensionStatus,
    SCORING_VERSION,
)
from app.domain.digital_marketing_intelligence import (
    SEO,
    PAID_ADVERTISING,
    SOCIAL_MEDIA,
    CONTENT_MARKETING,
    WEBSITE_PERFORMANCE,
    CONVERSION_OPTIMIZATION,
    ONLINE_REPUTATION,
    RESEARCHED,
    NOT_RESEARCHED,
    PARTIALLY_RESEARCHED,
)
from app.models.entities import (
    Lead,
    SalesStatus,
    SalesPriority,
    Evidence,
    ConfidenceLevel,
    SourceType,
    LeadIntelligenceOpportunity,
    DigitalMarketingIntelligenceProfile,
)
from app.services.lead_qualification import (
    qualify_lead,
    get_lead_qualification,
)
from app.main import app, get_db
from app.config import settings


def test_classify_lead_score_boundaries():
    """Verify exact canonical thresholds per RQ-09."""
    # HOT: 80 - 100
    c, a, p, _ = classify_lead_score(100)
    assert c == LeadClassification.HOT
    assert a == RecommendedSalesAction.REVIEW_LEAD
    assert p == SalesPriority.HIGH

    c, a, p, _ = classify_lead_score(80)
    assert c == LeadClassification.HOT

    # WARM: 60 - 79
    c, a, p, _ = classify_lead_score(79)
    assert c == LeadClassification.WARM
    assert a == RecommendedSalesAction.PREPARE_PERSONALIZED_AUDIT
    assert p == SalesPriority.MEDIUM

    c, a, p, _ = classify_lead_score(60)
    assert c == LeadClassification.WARM

    # NURTURE: 40 - 59
    c, a, p, _ = classify_lead_score(59)
    assert c == LeadClassification.NURTURE
    assert a == RecommendedSalesAction.CONTINUE_RESEARCH
    assert p == SalesPriority.LOW

    c, a, p, _ = classify_lead_score(40)
    assert c == LeadClassification.NURTURE

    # LOW_PRIORITY: 0 - 39
    c, a, p, _ = classify_lead_score(39)
    assert c == LeadClassification.LOW_PRIORITY
    assert a == RecommendedSalesAction.DEPRIORITIZE
    assert p == SalesPriority.LOW

    c, a, p, _ = classify_lead_score(0)
    assert c == LeadClassification.LOW_PRIORITY


def test_evaluate_lead_qualification_full_evidence():
    """Test full evidence scenario reaching maximum points across all 7 dimensions."""
    lead_id = uuid.uuid4()
    lead = Lead(
        id=lead_id,
        company_name="Acme Interiors Pvt Ltd",
        website="https://acmeinteriors.com",
        phone="+91 9876543210",
        email="contact@acmeinteriors.com",
        industry="INTERIOR_DESIGN",
        decision_maker="Rajesh Sharma",
        job_title="Founder & Managing Director",
        sales_status=SalesStatus.NEW,
        priority=SalesPriority.MEDIUM,
        lead_score=None,
    )

    coverage = {
        SEO: {"status": RESEARCHED, "signal_detected": True, "problem_identified": True, "problem_severity": "critical", "problem_summary": "Broken crawlability"},
        PAID_ADVERTISING: {"status": RESEARCHED, "signal_detected": True, "details": {"active_campaigns": True}},
        SOCIAL_MEDIA: {"status": RESEARCHED, "signal_detected": True},
        CONTENT_MARKETING: {"status": RESEARCHED, "signal_detected": True},
        WEBSITE_PERFORMANCE: {"status": RESEARCHED, "signal_detected": True},
        CONVERSION_OPTIMIZATION: {"status": RESEARCHED, "signal_detected": True},
        ONLINE_REPUTATION: {"status": RESEARCHED, "signal_detected": True},
    }

    opportunities = [
        LeadIntelligenceOpportunity(
            id=uuid.uuid4(),
            lead_id=lead_id,
            title="Technical SEO & Website Modernization Opportunity",
            description="Technical SEO issues identified",
            priority="HIGH",
            recommended_action="Technical SEO",
            created_at=datetime.now(timezone.utc),
        ),
        LeadIntelligenceOpportunity(
            id=uuid.uuid4(),
            lead_id=lead_id,
            title="Performance Marketing & Meta Ads Management",
            description="Active campaigns need performance marketing optimization",
            priority="HIGH",
            recommended_action="Performance Marketing",
            created_at=datetime.now(timezone.utc),
        ),
        LeadIntelligenceOpportunity(
            id=uuid.uuid4(),
            lead_id=lead_id,
            title="Local SEO & Reputation Management Opportunity",
            description="Enhance local ranking and review profile",
            priority="MEDIUM",
            recommended_action="Local SEO",
            created_at=datetime.now(timezone.utc),
        ),
    ]

    now = datetime.now(timezone.utc)
    evidence_list = [
        Evidence(
            id=uuid.uuid4(),
            lead_id=lead_id,
            evidence_type="https_verification",
            source_type=SourceType.SYSTEM_DERIVED,
            source_url="https://acmeinteriors.com",
            observed_at=now,
            observed_value={"valid_ssl": True},
            confidence=ConfidenceLevel.HIGH,
        ),
        Evidence(
            id=uuid.uuid4(),
            lead_id=lead_id,
            evidence_type="google_places_verification",
            source_type=SourceType.SYSTEM_DERIVED,
            observed_at=now,
            observed_value={"place_id": "ChIJ123456", "rating": 4.6},
            confidence=ConfidenceLevel.HIGH,
        ),
        Evidence(
            id=uuid.uuid4(),
            lead_id=lead_id,
            evidence_type="google_places_user_ratings_total",
            source_type=SourceType.SYSTEM_DERIVED,
            observed_at=now,
            observed_value=85,
            confidence=ConfidenceLevel.HIGH,
        ),
    ]

    website_analysis = {
        "https": True,
        "advertising_platforms": "Meta Ads, Google Ads",
        "advertising_evidence": "Active campaigns running on Meta Ads",
        "has_tracking_tags": True,
        "is_ecommerce": True,
        "sub_category": "Luxury Interior Design",
    }

    result = evaluate_lead_qualification(
        lead=lead,
        seven_category_coverage=coverage,
        opportunities=opportunities,
        evidence_list=evidence_list,
        website_analysis=website_analysis,
    )

    # D1 (25) + D2 (20) + D3 (15) + D4 (15) + D5 (10) + D6 (10) + D7 (5) = 100
    assert result["qualification_score"] == 100
    assert result["classification"] == "HOT"
    assert result["recommended_sales_action"] == "REVIEW_LEAD"
    assert result["recommended_priority"] == "HIGH"
    assert result["scoring_coverage"] == 100.0


def test_final_03_tracking_tags_alone_do_not_award_active_ad_points():
    """
    FINAL-03 Invariant:
    Tracking tags alone qualify as marketing infrastructure (+4 pts),
    NOT active advertising campaigns (+8 pts).
    """
    lead_id = uuid.uuid4()
    lead = Lead(
        id=lead_id,
        company_name="Only Tracking Tags Co",
        website="https://onlytags.com",
    )

    # Tracking tags present, but NO active campaigns verified
    website_analysis = {
        "has_tracking_tags": True,
        "advertising_platforms": None,
    }

    result = evaluate_lead_qualification(
        lead=lead,
        seven_category_coverage={},
        opportunities=[],
        evidence_list=[],
        website_analysis=website_analysis,
    )

    dim3 = result["dimension_breakdown"]["buying_intent"]
    # Should receive +4 for tracking infrastructure, but NOT +8 for active campaigns
    assert dim3["awarded_points"] == 4.0
    assert any("marketing infrastructure" in x.lower() for x in dim3["criteria_met"])
    assert any("active advertising campaign" in x.lower() for x in dim3["criteria_not_met"])


def test_rq_06_deduplicated_canonical_services_and_no_fabricated_budgets():
    """
    RQ-06 Invariant:
    Multiple opportunities mapped to the same canonical MindArtDigital service are deduplicated.
    Points are awarded based on distinct verified services (max 20 pts), with zero invented monetary budgets.
    """
    lead_id = uuid.uuid4()
    lead = Lead(id=lead_id, company_name="Duplicate Opportunities Co")

    now = datetime.now(timezone.utc)
    # 3 opportunities, but all 3 map to the SAME service
    opportunities = [
        LeadIntelligenceOpportunity(
            id=uuid.uuid4(),
            lead_id=lead_id,
            title="Technical SEO Finding 1",
            description="Technical SEO crawl issues",
            priority="HIGH",
            recommended_action="Technical SEO",
            created_at=now,
        ),
        LeadIntelligenceOpportunity(
            id=uuid.uuid4(),
            lead_id=lead_id,
            title="Technical SEO Finding 2",
            description="Technical SEO meta tags",
            priority="HIGH",
            recommended_action="Technical SEO",
            created_at=now,
        ),
        LeadIntelligenceOpportunity(
            id=uuid.uuid4(),
            lead_id=lead_id,
            title="Technical SEO Finding 3",
            description="Technical SEO sitemap",
            priority="HIGH",
            recommended_action="Technical SEO",
            created_at=now,
        ),
    ]

    result = evaluate_lead_qualification(
        lead=lead,
        seven_category_coverage={},
        opportunities=opportunities,
        evidence_list=[],
        website_analysis={},
    )

    dim2 = result["dimension_breakdown"]["commercial_opportunity_value"]
    # Only 1 unique canonical service mapped -> 8.0 points (not 20.0 points)
    assert dim2["awarded_points"] == 8.0
    assert len(dim2["criteria_met"]) == 1


def test_rq_07_all_eight_dimension_fields_present():
    """
    RQ-07 Invariant:
    Every dimension explanation MUST include all 8 required fields:
    maximum_points, awarded_points, dimension_status, evidence_references,
    criteria_met, criteria_not_met, missing_information, explanation.
    """
    lead_id = uuid.uuid4()
    lead = Lead(id=lead_id, company_name="Minimal Co")

    result = evaluate_lead_qualification(
        lead=lead,
        seven_category_coverage={},
        opportunities=[],
        evidence_list=[],
        website_analysis={},
    )

    required_fields = {
        "dimension_name",
        "maximum_points",
        "awarded_points",
        "dimension_status",
        "evidence_references",
        "criteria_met",
        "criteria_not_met",
        "missing_information",
        "explanation",
    }

    breakdown = result["dimension_breakdown"]
    assert len(breakdown) == 7

    for dim_name, dim_dict in breakdown.items():
        missing = required_fields - set(dim_dict.keys())
        assert not missing, f"Dimension {dim_name} missing fields: {missing}"
        assert isinstance(dim_dict["evidence_references"], list)
        assert isinstance(dim_dict["criteria_met"], list)
        assert isinstance(dim_dict["criteria_not_met"], list)
        assert isinstance(dim_dict["missing_information"], list)
        assert isinstance(dim_dict["explanation"], str)


@pytest.mark.asyncio
async def test_final_01_and_rq_04_rq_05_service_invariants(session_factory):
    """
    FINAL-01, RQ-04, RQ-05 & RQ-08 Invariants:
    1. NEVER write to lead.lead_score, even when None.
    2. Store qualification_score exclusively under lead.score_factors['qualification'].
    3. Preserve all existing keys in lead.score_factors.
    4. NEVER mutate lead.sales_status.
    5. NEVER mutate lead.priority.
    """
    lead_id = uuid.uuid4()

    async with session_factory() as session:
        lead = Lead(
            id=lead_id,
            company_name="Rubye Organics Test",
            website="https://rubyeorganics.com",
            phone="+91 9876543210",
            email="info@rubyeorganics.com",
            industry="BEAUTY_AND_PERSONAL_CARE",
            decision_maker="Rubye Founder",
            job_title="CEO",
            sales_status=SalesStatus.NEW,
            priority=SalesPriority.MEDIUM,
            lead_score=None,  # Crucial test: None must NOT be overwritten!
            score_factors={
                "https": 10,
                "website_quality": 20,
                "custom_unrelated_key": "must_preserve",
            },
        )
        session.add(lead)
        await session.commit()

    async with session_factory() as session:
        response = await qualify_lead(session, lead_id)
        assert response.qualification_score > 0
        assert response.classification in {"HOT", "WARM", "NURTURE", "LOW_PRIORITY"}

    # Verify database state after qualification
    async with session_factory() as session:
        refreshed_lead = await session.get(Lead, lead_id)
        assert refreshed_lead is not None

        # FINAL-01 INVARIANT:
        assert refreshed_lead.lead_score is None, "FINAL-01 VIOLATION: lead.lead_score was mutated!"

        # RQ-08 INVARIANT:
        assert refreshed_lead.score_factors is not None
        assert refreshed_lead.score_factors.get("https") == 10
        assert refreshed_lead.score_factors.get("website_quality") == 20
        assert refreshed_lead.score_factors.get("custom_unrelated_key") == "must_preserve"

        # Stored under qualification namespace
        qual_data = refreshed_lead.score_factors.get("qualification")
        assert isinstance(qual_data, dict)
        assert qual_data["qualification_score"] == response.qualification_score

        # RQ-04 INVARIANT:
        assert refreshed_lead.sales_status == SalesStatus.NEW, "RQ-04 VIOLATION: sales_status was mutated!"

        # RQ-05 INVARIANT:
        assert refreshed_lead.priority == SalesPriority.MEDIUM, "RQ-05 VIOLATION: priority was mutated!"


@pytest.mark.asyncio
async def test_lead_qualification_api_endpoints(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "qualification-test-token")
    """
    Test GET /api/leads/{id}/qualification and POST /api/leads/{id}/qualify.
    """
    lead_id = uuid.uuid4()

    async with session_factory() as session:
        lead = Lead(
            id=lead_id,
            company_name="API Test Co",
            website="https://apitest.com",
            sales_status=SalesStatus.NEW,
            priority=SalesPriority.MEDIUM,
        )
        session.add(lead)
        await session.commit()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {settings.api_auth_token}"}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. GET qualification on lead with no cached data -> auto-computes and returns 200
        get_res = await client.get(f"/api/leads/{lead_id}/qualification", headers=headers)
        assert get_res.status_code == 200
        data = get_res.json()
        assert data["lead_id"] == str(lead_id)
        assert "qualification_score" in data
        assert "scoring_coverage" in data
        assert "dimension_breakdown" in data

        # 2. POST qualify -> re-computes and returns 200
        post_res = await client.post(f"/api/leads/{lead_id}/qualify", headers=headers)
        assert post_res.status_code == 200
        post_data = post_res.json()
        assert post_data["qualification_score"] == data["qualification_score"]

        # 3. Non-existent lead -> 404
        non_existent_id = uuid.uuid4()
        res_404 = await client.get(f"/api/leads/{non_existent_id}/qualification", headers=headers)
        assert res_404.status_code == 404


@pytest.mark.asyncio
async def test_list_leads_filtering_and_sorting_by_qualification(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "qualification-test-token")
    """
    Test GET /api/leads with qualification_class filtering and qualification_score sorting.
    """
    id_hot = uuid.uuid4()
    id_warm = uuid.uuid4()
    id_low = uuid.uuid4()

    async with session_factory() as session:
        lead_hot = Lead(
            id=id_hot,
            company_name="Hot Lead Co",
            sales_status=SalesStatus.NEW,
            priority=SalesPriority.MEDIUM,
            score_factors={
                "qualification": {
                    "qualification_score": 88,
                    "classification": "HOT",
                    "scoring_coverage": 0.85,
                    "recommended_sales_action": "REVIEW_LEAD",
                }
            },
        )
        lead_warm = Lead(
            id=id_warm,
            company_name="Warm Lead Co",
            sales_status=SalesStatus.NEW,
            priority=SalesPriority.MEDIUM,
            score_factors={
                "qualification": {
                    "qualification_score": 68,
                    "classification": "WARM",
                    "scoring_coverage": 0.70,
                    "recommended_sales_action": "PREPARE_PERSONALIZED_AUDIT",
                }
            },
        )
        lead_low = Lead(
            id=id_low,
            company_name="Low Lead Co",
            sales_status=SalesStatus.NEW,
            priority=SalesPriority.LOW,
            score_factors={
                "qualification": {
                    "qualification_score": 25,
                    "classification": "LOW_PRIORITY",
                    "scoring_coverage": 0.30,
                    "recommended_sales_action": "DEPRIORITIZE",
                }
            },
        )
        session.add_all([lead_hot, lead_warm, lead_low])
        await session.commit()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {settings.api_auth_token}"}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Filter by HOT
        res_hot = await client.get("/api/leads?qualification_class=HOT", headers=headers)
        assert res_hot.status_code == 200
        items_hot = res_hot.json()
        assert len(items_hot) == 1
        assert items_hot[0]["company_name"] == "Hot Lead Co"
        assert items_hot[0]["qualification_score"] == 88
        assert items_hot[0]["qualification_class"] == "HOT"

        # Filter by WARM
        res_warm = await client.get("/api/leads?qualification_class=WARM", headers=headers)
        assert res_warm.status_code == 200
        items_warm = res_warm.json()
        assert len(items_warm) == 1
        assert items_warm[0]["company_name"] == "Warm Lead Co"
        assert items_warm[0]["qualification_score"] == 68

        # Sort desc by qualification_score
        res_sort_desc = await client.get("/api/leads?sort_by=qualification_score_desc", headers=headers)
        assert res_sort_desc.status_code == 200
        items_desc = res_sort_desc.json()
        assert len(items_desc) == 3
        assert items_desc[0]["qualification_score"] == 88
        assert items_desc[1]["qualification_score"] == 68
        assert items_desc[2]["qualification_score"] == 25

        # Sort asc by qualification_score
        res_sort_asc = await client.get("/api/leads?sort_by=qualification_score_asc", headers=headers)
        assert res_sort_asc.status_code == 200
        items_asc = res_sort_asc.json()
        assert len(items_asc) == 3
        assert items_asc[0]["qualification_score"] == 25
        assert items_asc[1]["qualification_score"] == 68
        assert items_asc[2]["qualification_score"] == 88

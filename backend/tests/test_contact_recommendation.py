import pytest
import uuid
from httpx import AsyncClient, ASGITransport

from app.models.entities import (
    Lead,
    LeadIntelligenceOpportunity,
    ContactIntelligenceProfile,
    SeniorityLevel,
    FunctionalArea,
    BuyingInfluenceLevel,
    ConfidenceLevel,
    SourceType,
    SalesTask,
    SalesTaskType,
    SalesTaskStatus,
)
from app.domain.contact_recommendation import (
    RecommendationPriority,
    RecommendedSalesAction,
    calculate_recommendation_priority,
    calculate_contact_recommendation_score,
    determine_recommended_action,
    evaluate_contact_recommendation,
)
from app.services.contact_recommendation import get_contact_recommendation_for_opportunity
from app.main import app, get_db

class MockOpportunity:
    def __init__(self, id=None, lead_id=None, opportunity_type="SEO", title="SEO Audit Opportunity"):
        self.id = id or uuid.uuid4()
        self.lead_id = lead_id or uuid.uuid4()
        self.opportunity_type = opportunity_type
        self.title = title

class MockLead:
    def __init__(self, id=None, company_name="Acme Corp", job_title=None, department=None, email=None, phone=None, linkedin_url=None):
        self.id = id or uuid.uuid4()
        self.company_name = company_name
        self.job_title = job_title
        self.department = department
        self.email = email
        self.phone = phone
        self.linkedin_url = linkedin_url

class MockProfile:
    def __init__(self, contact_id, decision_maker_score=0, functional_area=FunctionalArea.UNKNOWN, seniority_level=SeniorityLevel.UNKNOWN, buying_influence=BuyingInfluenceLevel.UNKNOWN, department=None):
        self.id = uuid.uuid4()
        self.contact_id = contact_id
        self.decision_maker_score = decision_maker_score
        self.functional_area = functional_area
        self.seniority_level = seniority_level
        self.buying_influence = buying_influence
        self.department = department

# ----------------------------------------------------
# 1. Priority Threshold Tests
# ----------------------------------------------------
def test_recommendation_priority_thresholds():
    assert calculate_recommendation_priority(100) == RecommendationPriority.CRITICAL
    assert calculate_recommendation_priority(90) == RecommendationPriority.CRITICAL
    assert calculate_recommendation_priority(89) == RecommendationPriority.HIGH
    assert calculate_recommendation_priority(75) == RecommendationPriority.HIGH
    assert calculate_recommendation_priority(74) == RecommendationPriority.MEDIUM
    assert calculate_recommendation_priority(60) == RecommendationPriority.MEDIUM
    assert calculate_recommendation_priority(59) == RecommendationPriority.LOW
    assert calculate_recommendation_priority(40) == RecommendationPriority.LOW
    assert calculate_recommendation_priority(39) == RecommendationPriority.VERY_LOW
    assert calculate_recommendation_priority(0) == RecommendationPriority.VERY_LOW

# ----------------------------------------------------
# 2. Recommended Action Determination Tests
# ----------------------------------------------------
def test_recommended_action_rules():
    # 1. Prior task -> FOLLOW_UP
    act, reasons = determine_recommended_action(90, 80, BuyingInfluenceLevel.DECISION_MAKER, True, True, True, has_previous_task=True)
    assert act == RecommendedSalesAction.FOLLOW_UP
    assert "Prior actionable sales task exists" in reasons[0]

    # 2. Decision maker + strong match + email + phone -> DISCOVERY_CALL
    act, reasons = determine_recommended_action(85, 80, BuyingInfluenceLevel.DECISION_MAKER, True, True, True, has_previous_task=False)
    assert act == RecommendedSalesAction.DISCOVERY_CALL

    # 3. Decision maker + strong match + email only -> PERSONALIZED_EMAIL
    act, reasons = determine_recommended_action(85, 80, BuyingInfluenceLevel.DECISION_MAKER, True, False, False, has_previous_task=False)
    assert act == RecommendedSalesAction.PERSONALIZED_EMAIL

    # 4. Decision maker + strong match + phone only -> DISCOVERY_CALL
    act, reasons = determine_recommended_action(85, 80, BuyingInfluenceLevel.DECISION_MAKER, False, True, False, has_previous_task=False)
    assert act == RecommendedSalesAction.DISCOVERY_CALL

    # 5. Strong match + buying influence + missing email & phone -> RESEARCH_CONTACT
    act, reasons = determine_recommended_action(75, 60, BuyingInfluenceLevel.HIGH, False, False, False, has_previous_task=False)
    assert act == RecommendedSalesAction.RESEARCH_CONTACT

    # 6. Strong match + LinkedIn only -> DIRECT_OUTREACH
    act, reasons = determine_recommended_action(75, 60, BuyingInfluenceLevel.HIGH, False, False, True, has_previous_task=False)
    assert act == RecommendedSalesAction.DIRECT_OUTREACH

    # 7. Insufficient evidence -> NO_ACTION
    act, reasons = determine_recommended_action(10, 10, BuyingInfluenceLevel.UNKNOWN, False, False, False, has_previous_task=False)
    assert act == RecommendedSalesAction.NO_ACTION

# ----------------------------------------------------
# 3. Recommendation Scoring Components Tests
# ----------------------------------------------------
def test_recommendation_score_formula_and_normalization():
    score, breakdown = calculate_contact_recommendation_score(
        match_score=100,
        decision_maker_score=100,
        buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
        seniority=SeniorityLevel.EXECUTIVE,
        has_email=True,
        has_phone=True,
        has_linkedin=True
    )
    # 40 (match) + 25 (dm) + 15 (bi) + 10 (sen) + 10 (comp) = 100
    assert score == 100
    assert breakdown["match_component"] == 40
    assert breakdown["decision_maker_component"] == 25
    assert breakdown["buying_influence_component"] == 15
    assert breakdown["seniority_component"] == 10
    assert breakdown["completeness_component"] == 10
    assert score == sum([
        breakdown["match_component"],
        breakdown["decision_maker_component"],
        breakdown["buying_influence_component"],
        breakdown["seniority_component"],
        breakdown["completeness_component"],
    ])

def test_recommendation_score_breakdown_consistency():
    # Test arbitrary candidate components summing up exactly
    # e.g. match=91 -> pts=36, dm=75 -> pts=19, bi=DECISION_MAKER -> pts=15, sen=EXECUTIVE -> pts=10, email+phone -> pts=8
    score, breakdown = calculate_contact_recommendation_score(
        match_score=91,
        decision_maker_score=75,
        buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
        seniority=SeniorityLevel.EXECUTIVE,
        has_email=True,
        has_phone=True,
        has_linkedin=False
    )
    assert breakdown["match_component"] == 36
    assert breakdown["decision_maker_component"] == 19
    assert breakdown["buying_influence_component"] == 15
    assert breakdown["seniority_component"] == 10
    assert breakdown["completeness_component"] == 8
    expected_sum = 36 + 19 + 15 + 10 + 8  # 88
    assert score == expected_sum
    assert breakdown["total"] == expected_sum

def test_buying_influence_and_seniority_weighting():
    # Comparing a high-influence contact vs low-influence contact with same match score
    score_high, _ = calculate_contact_recommendation_score(80, 70, BuyingInfluenceLevel.DECISION_MAKER, SeniorityLevel.LEADERSHIP, True, True, False)
    score_low, _ = calculate_contact_recommendation_score(80, 70, BuyingInfluenceLevel.LOW, SeniorityLevel.INDIVIDUAL_CONTRIBUTOR, True, True, False)
    assert score_high > score_low

# ----------------------------------------------------
# 4. Domain Recommendation Evaluation Tests
# ----------------------------------------------------
def test_strongest_contact_wins():
    opp = MockOpportunity(opportunity_type="SEO")
    id1 = uuid.uuid4()
    id2 = uuid.uuid4()

    lead1 = MockLead(id=id1, job_title="SEO Manager", department="Marketing", email="seo@acme.com", phone="123")
    prof1 = MockProfile(id1, decision_maker_score=71, functional_area=FunctionalArea.SEO, seniority_level=SeniorityLevel.MANAGER, buying_influence=BuyingInfluenceLevel.HIGH, department="Marketing")

    lead2 = MockLead(id=id2, job_title="HR Specialist", department="HR", email="hr@acme.com")
    prof2 = MockProfile(id2, decision_maker_score=20, functional_area=FunctionalArea.HR, seniority_level=SeniorityLevel.INDIVIDUAL_CONTRIBUTOR, buying_influence=BuyingInfluenceLevel.LOW, department="HR")

    match1 = {"contact_id": id1, "match_score": 91, "decision_maker_score": 71}
    match2 = {"contact_id": id2, "match_score": 25, "decision_maker_score": 20}

    rec = evaluate_contact_recommendation(
        opportunity=opp,
        ranked_matched_contacts=[match1, match2],
        candidate_leads_map={id1: lead1, id2: lead2},
        candidate_profiles_map={id1: prof1, id2: prof2}
    )

    assert rec["recommended_contact"]["contact_id"] == id1
    assert rec["recommendation_score"] >= 80
    assert rec["priority"] in [RecommendationPriority.CRITICAL, RecommendationPriority.HIGH]
    assert rec["recommended_action"] == RecommendedSalesAction.PERSONALIZED_EMAIL
    assert rec["evidence"]["recommendation"]["contact_id"] == str(id1)

def test_no_contact_scenario():
    opp = MockOpportunity()
    rec = evaluate_contact_recommendation(
        opportunity=opp,
        ranked_matched_contacts=[],
        candidate_leads_map={},
        candidate_profiles_map={}
    )

    assert rec["recommended_contact"] is None
    assert rec["recommendation_score"] == 0
    assert rec["priority"] == RecommendationPriority.VERY_LOW
    assert rec["recommended_action"] == RecommendedSalesAction.NO_ACTION
    assert "No candidate contacts" in rec["reasons"][0]

def test_deterministic_tie_breaking_and_idempotency():
    opp = MockOpportunity()
    id1 = uuid.UUID("11111111-1111-1111-1111-111111111111")
    id2 = uuid.UUID("22222222-2222-2222-2222-222222222222")

    lead1 = MockLead(id=id1, job_title="SEO Lead", email="a@acme.com")
    lead2 = MockLead(id=id2, job_title="SEO Lead", email="b@acme.com")

    prof1 = MockProfile(id1, decision_maker_score=60, functional_area=FunctionalArea.SEO, seniority_level=SeniorityLevel.MANAGER, buying_influence=BuyingInfluenceLevel.MEDIUM)
    prof2 = MockProfile(id2, decision_maker_score=60, functional_area=FunctionalArea.SEO, seniority_level=SeniorityLevel.MANAGER, buying_influence=BuyingInfluenceLevel.MEDIUM)

    match1 = {"contact_id": id1, "match_score": 80, "decision_maker_score": 60}
    match2 = {"contact_id": id2, "match_score": 80, "decision_maker_score": 60}

    # Pass in reverse order to ensure sorting is fully deterministic
    rec1 = evaluate_contact_recommendation(opp, [match2, match1], {id1: lead1, id2: lead2}, {id1: prof1, id2: prof2})
    rec2 = evaluate_contact_recommendation(opp, [match1, match2], {id1: lead1, id2: lead2}, {id1: prof1, id2: prof2})

    assert rec1 == rec2
    assert rec1["recommended_contact"]["contact_id"] == id1

# ----------------------------------------------------
# 5. Service & API Integration Tests
# ----------------------------------------------------
@pytest.mark.asyncio
async def test_get_contact_recommendation_service(session_factory):
    async with session_factory() as session:
        # Create company lead
        lead_id = uuid.uuid4()
        lead = Lead(
            id=lead_id,
            company_name="Vanguard Logistics",
            currency="USD",
            job_title="CEO",
            email="ceo@vanguard.com",
            phone="1234567890"
        )
        session.add(lead)

        # Create second contact at same company
        lead2_id = uuid.uuid4()
        lead2 = Lead(
            id=lead2_id,
            company_name="Vanguard Logistics",
            currency="USD",
            job_title="Director of Marketing",
            email="mktg@vanguard.com",
            phone="0987654321"
        )
        session.add(lead2)

        # Create contact at DIFFERENT company (company boundary check)
        other_lead_id = uuid.uuid4()
        other_lead = Lead(
            id=other_lead_id,
            company_name="Unrelated Corp",
            currency="USD",
            job_title="Chief Marketing Officer",
            email="cmo@unrelated.com"
        )
        session.add(other_lead)

        # Create Opportunity for Vanguard Logistics
        opp_id = uuid.uuid4()
        opp = LeadIntelligenceOpportunity(
            id=opp_id,
            lead_id=lead_id,
            title="Digital Marketing Expansion",
            opportunity_type="DIGITAL_MARKETING",
            score=85,
            priority="HIGH",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH
        )
        session.add(opp)
        await session.commit()

        # Run recommendation service
        rec = await get_contact_recommendation_for_opportunity(session, opp_id)
        assert rec is not None
        assert rec["opportunity_id"] == opp_id
        assert rec["recommended_contact"] is not None
        assert rec["recommended_contact"]["contact_id"] in [lead_id, lead2_id]
        assert rec["recommended_contact"]["contact_id"] != other_lead_id  # strictly excluded
        assert rec["recommendation_score"] > 0
        assert rec["recommended_action"] in [RecommendedSalesAction.PERSONALIZED_EMAIL, RecommendedSalesAction.DISCOVERY_CALL]

@pytest.mark.asyncio
async def test_contact_recommendation_api_endpoints(session_factory, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")

    async with session_factory() as session:
        lead_id = uuid.uuid4()
        lead = Lead(
            id=lead_id,
            company_name="Apex Global",
            currency="USD",
            job_title="Head of SEO",
            email="seo@apexglobal.com",
            phone="1122334455"
        )
        session.add(lead)

        opp_id = uuid.uuid4()
        opp = LeadIntelligenceOpportunity(
            id=opp_id,
            lead_id=lead_id,
            title="Technical SEO Restructuring",
            opportunity_type="SEO",
            score=90,
            priority="HIGH",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH
        )
        session.add(opp)
        await session.commit()

    app.dependency_overrides[get_db] = lambda: session_factory()
    headers = {"Authorization": "Bearer api-test-token"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Valid opportunity recommendation
        res = await client.get(f"/api/sales-opportunities/{opp_id}/contact-recommendation", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["opportunity_id"] == str(opp_id)
        assert data["recommended_contact"] is not None
        assert data["recommended_contact"]["contact_id"] == str(lead_id)
        assert data["recommendation_score"] >= 75
        assert data["priority"] in ["HIGH", "CRITICAL"]
        assert len(data["reasons"]) > 0
        assert "recommendation" in data["evidence"]

        # Invalid opportunity recommendation
        bad_id = uuid.uuid4()
        res_bad = await client.get(f"/api/sales-opportunities/{bad_id}/contact-recommendation", headers=headers)
        assert res_bad.status_code == 404

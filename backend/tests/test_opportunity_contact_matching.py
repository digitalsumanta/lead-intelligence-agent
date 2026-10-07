import pytest
import uuid
from httpx import AsyncClient, ASGITransport

from app.models.entities import (
    Lead,
    LeadIntelligenceOpportunity,
    ContactIntelligenceProfile,
    SeniorityLevel,
    FunctionalArea,
    ConfidenceLevel,
    SourceType
)
from app.domain.opportunity_contact_matching import (
    MatchLevel,
    calculate_match_level,
    score_functional_relevance,
    score_role_relevance,
    score_decision_maker_authority,
    score_seniority_relevance,
    score_department_relevance,
    score_contact_completeness,
    match_opportunity_contact,
    rank_opportunity_contacts,
)
from app.services.opportunity_contact_matching import get_matched_contacts_for_opportunity
from app.main import app, get_db

class MockOpportunity:
    def __init__(self, id=None, lead_id=None, opportunity_type="SEO", title="Test Opp"):
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
    def __init__(self, contact_id, decision_maker_score=0, functional_area=FunctionalArea.UNKNOWN, seniority_level=SeniorityLevel.UNKNOWN, department=None):
        self.id = uuid.uuid4()
        self.contact_id = contact_id
        self.decision_maker_score = decision_maker_score
        self.functional_area = functional_area
        self.seniority_level = seniority_level
        self.department = department

# ----------------------------------------------------
# 1. Match Level Threshold Tests
# ----------------------------------------------------
def test_match_level_thresholds():
    assert calculate_match_level(100) == MatchLevel.EXCELLENT
    assert calculate_match_level(90) == MatchLevel.EXCELLENT
    assert calculate_match_level(89) == MatchLevel.STRONG
    assert calculate_match_level(75) == MatchLevel.STRONG
    assert calculate_match_level(74) == MatchLevel.GOOD
    assert calculate_match_level(60) == MatchLevel.GOOD
    assert calculate_match_level(59) == MatchLevel.MODERATE
    assert calculate_match_level(40) == MatchLevel.MODERATE
    assert calculate_match_level(39) == MatchLevel.WEAK
    assert calculate_match_level(20) == MatchLevel.WEAK
    assert calculate_match_level(19) == MatchLevel.POOR
    assert calculate_match_level(0) == MatchLevel.POOR

# ----------------------------------------------------
# 2. Persona Match Tests
# ----------------------------------------------------
def test_seo_opportunity_personas():
    opp = MockOpportunity(opportunity_type="SEO")

    # 1. SEO Manager -> Very High Match (EXCELLENT)
    seo_mgr = MockLead(job_title="SEO Manager", department="Marketing", email="seo@acme.com", phone="123")
    seo_prof = MockProfile(seo_mgr.id, decision_maker_score=71, functional_area=FunctionalArea.SEO, seniority_level=SeniorityLevel.MANAGER, department="Marketing")
    res_seo = match_opportunity_contact(opp, seo_mgr, seo_prof)

    assert res_seo["match_score"] >= 90
    assert res_seo["match_level"] == MatchLevel.EXCELLENT.value
    assert res_seo["functional_relevance_score"] == 30
    assert res_seo["role_relevance_score"] == 20

    # 2. Digital Marketing Manager -> High Match (STRONG)
    dm_mgr = MockLead(job_title="Digital Marketing Manager", department="Marketing", email="dm@acme.com", phone="123")
    dm_prof = MockProfile(dm_mgr.id, decision_maker_score=71, functional_area=FunctionalArea.DIGITAL_MARKETING, seniority_level=SeniorityLevel.MANAGER, department="Marketing")
    res_dm = match_opportunity_contact(opp, dm_mgr, dm_prof)

    assert 75 <= res_dm["match_score"] < 90
    assert res_dm["match_level"] == MatchLevel.STRONG.value
    assert res_dm["functional_relevance_score"] == 30
    assert res_dm["role_relevance_score"] == 15

    # 3. CEO -> High Authority, but lower functional/role match than SEO/DM specialists
    ceo = MockLead(job_title="CEO", department="Executive", email="ceo@acme.com", phone="123", linkedin_url="https://li")
    ceo_prof = MockProfile(ceo.id, decision_maker_score=90, functional_area=FunctionalArea.EXECUTIVE, seniority_level=SeniorityLevel.EXECUTIVE, department="Executive")
    res_ceo = match_opportunity_contact(opp, ceo, ceo_prof)

    # CEO has higher decision maker authority (18 vs 14) but lower total match score
    assert res_ceo["decision_maker_score"] == 90
    assert res_ceo["match_score"] < res_seo["match_score"]
    assert res_ceo["match_score"] < res_dm["match_score"]

    # 4. HR Manager -> Low Match (WEAK)
    hr_mgr = MockLead(job_title="HR Manager", department="HR", email="hr@acme.com", phone="123")
    hr_prof = MockProfile(hr_mgr.id, decision_maker_score=45, functional_area=FunctionalArea.HR, seniority_level=SeniorityLevel.MANAGER, department="HR")
    res_hr = match_opportunity_contact(opp, hr_mgr, hr_prof)

    assert res_hr["match_score"] < 40
    assert res_hr["match_level"] == MatchLevel.WEAK.value

def test_website_opportunity_personas():
    opp = MockOpportunity(opportunity_type="WEBSITE")

    # 5. Website opportunity + CTO -> Very High Match (EXCELLENT)
    cto = MockLead(job_title="CTO", department="Technology", email="cto@acme.com", phone="123")
    cto_prof = MockProfile(cto.id, decision_maker_score=77, functional_area=FunctionalArea.TECHNOLOGY, seniority_level=SeniorityLevel.EXECUTIVE, department="Technology")
    res_cto = match_opportunity_contact(opp, cto, cto_prof)

    assert res_cto["match_score"] >= 90
    assert res_cto["match_level"] == MatchLevel.EXCELLENT.value
    assert res_cto["functional_relevance_score"] == 30
    assert res_cto["role_relevance_score"] == 20

    # 6. Website opportunity + Digital Marketing Manager -> Relevant but below CTO
    dm_mgr = MockLead(job_title="Digital Marketing Manager", department="Marketing", email="dm@acme.com", phone="123")
    dm_prof = MockProfile(dm_mgr.id, decision_maker_score=71, functional_area=FunctionalArea.DIGITAL_MARKETING, seniority_level=SeniorityLevel.MANAGER, department="Marketing")
    res_dm = match_opportunity_contact(opp, dm_mgr, dm_prof)

    assert res_dm["match_score"] < res_cto["match_score"]
    assert res_dm["match_level"] == MatchLevel.STRONG.value

def test_sales_opportunity_personas():
    # 7. Sales opportunity + Sales Manager -> Very High Match (EXCELLENT)
    opp = MockOpportunity(opportunity_type="SALES")
    sales_mgr = MockLead(job_title="Sales Manager", department="Sales", email="sales@acme.com", phone="123")
    sales_prof = MockProfile(sales_mgr.id, decision_maker_score=71, functional_area=FunctionalArea.SALES, seniority_level=SeniorityLevel.MANAGER, department="Sales")
    res_sales = match_opportunity_contact(opp, sales_mgr, sales_prof)

    assert res_sales["match_score"] >= 90
    assert res_sales["match_level"] == MatchLevel.EXCELLENT.value
    assert res_sales["functional_relevance_score"] == 30

# ----------------------------------------------------
# 3. Robustness, Missing Data & Fallbacks
# ----------------------------------------------------
def test_unknown_opportunity_type():
    # 8. Unknown opportunity type -> deterministic safe result
    opp = MockOpportunity(opportunity_type="UNKNOWN_FUTURE_TYPE")
    lead = MockLead(job_title="Operations Lead", email="ops@acme.com")
    res = match_opportunity_contact(opp, lead)

    assert 0 <= res["match_score"] <= 100
    assert res["match_level"] in [m.value for m in MatchLevel]

def test_missing_job_title():
    # 9. Missing job title -> 0 role points
    opp = MockOpportunity(opportunity_type="SEO")
    lead = MockLead(job_title=None, email="a@b.com")
    res = match_opportunity_contact(opp, lead)

    assert res["role_relevance_score"] == 0
    assert "No job title available" in res["evidence"]["role_relevance"]["reason"]

def test_missing_contact_profile():
    # 10. Missing ContactIntelligenceProfile -> calculated on-the-fly dynamically
    opp = MockOpportunity(opportunity_type="SEO")
    lead = MockLead(job_title="SEO Specialist", department="Marketing", email="seo@test.com")
    res = match_opportunity_contact(opp, lead, contact_profile=None)

    assert res["functional_relevance_score"] == 30
    assert res["role_relevance_score"] == 20
    assert res["decision_maker_score"] > 0

def test_missing_contact_completeness():
    # 11. Missing email/phone/LinkedIn -> completeness score is 0
    opp = MockOpportunity(opportunity_type="SEO")
    lead = MockLead(job_title="SEO Lead", email=None, phone=None, linkedin_url=None)
    res = match_opportunity_contact(opp, lead)

    assert res["completeness_score"] == 0
    assert res["evidence"]["completeness"]["email"] == 0
    assert res["evidence"]["completeness"]["phone"] == 0
    assert res["evidence"]["completeness"]["linkedin"] == 0

def test_score_boundaries_and_evidence_consistency():
    # 16. Score never exceeds 100
    # 17. Score never falls below 0
    # 18. Evidence total equals score
    opp = MockOpportunity(opportunity_type="SEO")
    lead = MockLead(job_title="SEO Director", department="Marketing", email="a@b.com", phone="123", linkedin_url="li")
    prof = MockProfile(lead.id, decision_maker_score=100, functional_area=FunctionalArea.SEO, seniority_level=SeniorityLevel.LEADERSHIP, department="Marketing")
    res = match_opportunity_contact(opp, lead, prof)

    assert 0 <= res["match_score"] <= 100
    ev = res["evidence"]
    assert ev["total"] == res["match_score"]

    # Verify sum of parts
    computed_sum = (
        ev["functional_relevance"]["points"] +
        ev["role_relevance"]["points"] +
        ev["decision_maker"]["points"] +
        ev["seniority"]["points"] +
        ev["department"]["points"] +
        ev["completeness"]["points"]
    )
    assert min(100, max(0, computed_sum)) == res["match_score"]

def test_idempotency():
    # 15. Repeated calculation -> identical result
    opp = MockOpportunity(opportunity_type="DIGITAL_MARKETING")
    lead = MockLead(job_title="Growth Marketing Manager", email="growth@acme.com")
    res1 = match_opportunity_contact(opp, lead)
    res2 = match_opportunity_contact(opp, lead)
    assert res1 == res2

def test_deterministic_ranking_and_tie_breaking():
    # 14. Two contacts with identical scores -> deterministic tie-breaking (by DM score DESC, contact_id ASC)
    opp = MockOpportunity(opportunity_type="SEO")
    id1 = uuid.UUID("11111111-1111-1111-1111-111111111111")
    id2 = uuid.UUID("22222222-2222-2222-2222-222222222222")

    lead1 = MockLead(id=id1, job_title="SEO Specialist", email="a@b.com")
    lead2 = MockLead(id=id2, job_title="SEO Specialist", email="a@b.com")

    # Identical match score and DM score
    candidates = [(lead2, None), (lead1, None)]
    ranked = rank_opportunity_contacts(opp, candidates)

    assert ranked[0]["contact_id"] == id1
    assert ranked[1]["contact_id"] == id2

# ----------------------------------------------------
# 4. Service & API Integration Tests
# ----------------------------------------------------
@pytest.mark.asyncio
async def test_get_matched_contacts_service(session_factory):
    async with session_factory() as session:
        # Create company lead
        lead_id = uuid.uuid4()
        lead = Lead(
            id=lead_id,
            company_name="Alpha Tech",
            currency="USD",
            job_title="CEO",
            email="ceo@alphatech.com"
        )
        session.add(lead)

        # Create second contact at same company
        lead2_id = uuid.uuid4()
        lead2 = Lead(
            id=lead2_id,
            company_name="Alpha Tech",
            currency="USD",
            job_title="Head of SEO",
            email="seo@alphatech.com",
            phone="9876543210"
        )
        session.add(lead2)

        # Create contact at DIFFERENT company (should be excluded)
        other_lead_id = uuid.uuid4()
        other_lead = Lead(
            id=other_lead_id,
            company_name="Beta Logistics",
            currency="USD",
            job_title="SEO Manager",
            email="seo@betalogistics.com"
        )
        session.add(other_lead)

        # Create Opportunity for Alpha Tech
        opp_id = uuid.uuid4()
        opp = LeadIntelligenceOpportunity(
            id=opp_id,
            lead_id=lead_id,
            title="SEO Audit Enhancement",
            opportunity_type="SEO",
            score=80,
            priority="HIGH",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH
        )
        session.add(opp)
        await session.commit()

        # Run service
        result = await get_matched_contacts_for_opportunity(session, opp_id)
        assert result is not None
        assert result["opportunity_id"] == opp_id
        contacts = result["contacts"]

        # Only Alpha Tech contacts eligible (2 contacts), Beta Logistics excluded
        contact_ids = [c["contact_id"] for c in contacts]
        assert len(contacts) == 2
        assert lead2_id in contact_ids
        assert lead_id in contact_ids
        assert other_lead_id not in contact_ids

        # Head of SEO should rank above CEO for SEO opportunity
        assert contacts[0]["contact_id"] == lead2_id
        assert contacts[0]["match_score"] > contacts[1]["match_score"]

@pytest.mark.asyncio
async def test_opportunity_contacts_api_endpoints(session_factory, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")

    async with session_factory() as session:
        lead_id = uuid.uuid4()
        lead = Lead(
            id=lead_id,
            company_name="OmniCorp",
            currency="USD",
            job_title="CTO",
            email="cto@omnicorp.com",
            phone="1234567890"
        )
        session.add(lead)

        opp_id = uuid.uuid4()
        opp = LeadIntelligenceOpportunity(
            id=opp_id,
            lead_id=lead_id,
            title="Cloud Infrastructure Upgrade",
            opportunity_type="WEBSITE",
            score=85,
            priority="HIGH",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH
        )
        session.add(opp)
        await session.commit()

    app.dependency_overrides[get_db] = lambda: session_factory()
    headers = {"Authorization": "Bearer api-test-token"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Valid opportunity
        res = await client.get(f"/api/sales-opportunities/{opp_id}/contacts", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["opportunity_id"] == str(opp_id)
        assert len(data["contacts"]) == 1
        assert data["contacts"][0]["contact_id"] == str(lead_id)
        assert data["contacts"][0]["match_level"] == "EXCELLENT"

        # Invalid opportunity
        bad_id = uuid.uuid4()
        res_bad = await client.get(f"/api/sales-opportunities/{bad_id}/contacts", headers=headers)
        assert res_bad.status_code == 404

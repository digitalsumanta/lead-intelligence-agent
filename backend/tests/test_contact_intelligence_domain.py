import pytest
from app.domain.contact_intelligence import (
    normalize_title,
    classify_seniority,
    classify_functional_area,
    calculate_decision_maker_score,
    build_contact_intelligence,
    determine_buying_influence,
    score_seniority,
    score_functional_area,
    score_contact_completeness,
    score_role_strength,
    score_department_relevance,
    SeniorityLevel,
    FunctionalArea,
    BuyingInfluenceLevel,
)
from app.models.entities import ConfidenceLevel

def test_normalize_title():
    assert normalize_title("  CEO  ") == "ceo"
    assert normalize_title("Vice-President, Sales") == "vice president sales"
    assert normalize_title(None) is None
    assert normalize_title("   ") is None

def test_classify_seniority():
    # Executive
    assert classify_seniority("CEO")[0] == SeniorityLevel.EXECUTIVE
    assert classify_seniority("Chief Executive Officer")[0] == SeniorityLevel.EXECUTIVE
    assert classify_seniority("Managing Director")[0] == SeniorityLevel.EXECUTIVE
    assert classify_seniority("Founder and CEO")[0] == SeniorityLevel.EXECUTIVE

    # Leadership
    assert classify_seniority("VP of Sales")[0] == SeniorityLevel.LEADERSHIP
    assert classify_seniority("Director of Marketing")[0] == SeniorityLevel.LEADERSHIP
    assert classify_seniority("Head of IT")[0] == SeniorityLevel.LEADERSHIP

    # Manager
    assert classify_seniority("Senior Manager")[0] == SeniorityLevel.MANAGER
    assert classify_seniority("Marketing Manager")[0] == SeniorityLevel.MANAGER
    assert classify_seniority("Team Lead")[0] == SeniorityLevel.MANAGER

    # Individual Contributor
    assert classify_seniority("Sales Executive")[0] == SeniorityLevel.INDIVIDUAL_CONTRIBUTOR
    assert classify_seniority("Marketing Specialist")[0] == SeniorityLevel.INDIVIDUAL_CONTRIBUTOR
    assert classify_seniority("Data Analyst")[0] == SeniorityLevel.INDIVIDUAL_CONTRIBUTOR

    # Unknown
    assert classify_seniority("Unknown Title")[0] == SeniorityLevel.UNKNOWN
    assert classify_seniority(None)[0] == SeniorityLevel.UNKNOWN

def test_classify_functional_area():
    assert classify_functional_area("Marketing Manager")[0] == FunctionalArea.MARKETING
    assert classify_functional_area("Digital Marketing Specialist")[0] == FunctionalArea.DIGITAL_MARKETING
    assert classify_functional_area("VP of Sales")[0] == FunctionalArea.SALES
    assert classify_functional_area("Head of SEO")[0] == FunctionalArea.SEO
    assert classify_functional_area("Software Engineer")[0] == FunctionalArea.TECHNOLOGY
    assert classify_functional_area("CFO")[0] == FunctionalArea.FINANCE
    assert classify_functional_area("CEO")[0] == FunctionalArea.EXECUTIVE
    assert classify_functional_area("Something Else")[0] == FunctionalArea.UNKNOWN
    assert classify_functional_area(None)[0] == FunctionalArea.UNKNOWN

def test_calculate_decision_maker_score():
    score, details, ev = calculate_decision_maker_score(
        seniority=SeniorityLevel.EXECUTIVE,
        func_area=FunctionalArea.SALES,
        has_email=True,
        has_phone=True,
        has_linkedin=True,
        title="CEO",
        department="Sales"
    )
    # 40 (seniority) + 25 (func) + 15 (contact) + 10 (role) + 10 (dept) = 100
    assert score == 100
    assert details["total"] == 100
    assert not details["capped"]
    assert len(ev) == 5

    score2, details2, ev2 = calculate_decision_maker_score(
        seniority=SeniorityLevel.MANAGER,
        func_area=FunctionalArea.IT,
        has_email=True,
        has_phone=False,
        has_linkedin=False,
        title="IT Manager",
        department="IT"
    )
    # 20 (seniority) + 12 (func) + 5 (contact) + 6 (role) + 5 (dept) = 48
    assert score2 == 48
    assert details2["total"] == 48

def test_semantic_unknown_title_no_false_positive():
    score, details, _ = calculate_decision_maker_score(
        SeniorityLevel.UNKNOWN,
        FunctionalArea.UNKNOWN,
        True,
        False,
        False,
        title=None,
        department=None
    )
    # Score should just be 5 (from email)
    assert score == 5
    inf = determine_buying_influence(SeniorityLevel.UNKNOWN, score)
    assert inf == BuyingInfluenceLevel.UNKNOWN

def test_semantic_seniority_weight():
    high_score, _, _ = calculate_decision_maker_score(SeniorityLevel.LEADERSHIP, FunctionalArea.MARKETING, True, True, True, "Director", "Marketing")
    low_score, _, _ = calculate_decision_maker_score(SeniorityLevel.INDIVIDUAL_CONTRIBUTOR, FunctionalArea.MARKETING, True, True, True, "Specialist", "Marketing")
    assert high_score > low_score

# Phase 12H-B required tests
def test_phase_12hb_required_cases():
    assert classify_seniority("CEO")[0] == SeniorityLevel.EXECUTIVE

    assert classify_seniority("Chief Marketing Officer")[0] == SeniorityLevel.EXECUTIVE
    assert classify_functional_area("Chief Marketing Officer")[0] == FunctionalArea.MARKETING

    assert classify_seniority("Head of Digital Marketing")[0] == SeniorityLevel.LEADERSHIP
    assert classify_functional_area("Head of Digital Marketing")[0] == FunctionalArea.DIGITAL_MARKETING

    assert classify_seniority("SEO Manager")[0] == SeniorityLevel.MANAGER
    assert classify_functional_area("SEO Manager")[0] == FunctionalArea.SEO

    assert classify_seniority("Performance Marketing Manager")[0] == SeniorityLevel.MANAGER
    assert classify_functional_area("Performance Marketing Manager")[0] == FunctionalArea.DIGITAL_MARKETING

    # Marketing Executive -> Individual Contributor (must not confuse 'Executive' word with Executive level)
    assert classify_seniority("Marketing Executive")[0] == SeniorityLevel.INDIVIDUAL_CONTRIBUTOR

    assert classify_seniority("Sales Director")[0] == SeniorityLevel.LEADERSHIP
    assert classify_functional_area("Sales Director")[0] == FunctionalArea.SALES

    assert classify_seniority("IT Manager")[0] == SeniorityLevel.MANAGER
    assert classify_functional_area("IT Manager")[0] == FunctionalArea.IT

    assert classify_seniority("CTO")[0] == SeniorityLevel.EXECUTIVE
    assert classify_functional_area("CTO")[0] == FunctionalArea.TECHNOLOGY

    assert classify_seniority("HR Manager")[0] == SeniorityLevel.MANAGER
    assert classify_functional_area("HR Manager")[0] == FunctionalArea.HR

    assert classify_seniority("Procurement Manager")[0] == SeniorityLevel.MANAGER
    assert classify_functional_area("Procurement Manager")[0] == FunctionalArea.PROCUREMENT

    assert classify_seniority("Generic unknown title")[0] == SeniorityLevel.UNKNOWN

class MockLead:
    def __init__(self, job_title, department=None, email=None, phone=None, linkedin=None):
        self.job_title = job_title
        self.department = department
        self.email = email
        self.phone = phone
        self.linkedin_url = linkedin

def test_build_contact_intelligence():
    lead = MockLead("VP of Sales", "Sales Dept", "a@b.com", "123", "url")
    result = build_contact_intelligence(lead)

    assert result["seniority_level"] == SeniorityLevel.LEADERSHIP
    assert result["functional_area"] == FunctionalArea.SALES
    assert result["department"] == "Sales Dept"  # explicit override
    assert "Insufficient persisted data" not in result["evidence"]["classification_evidence"][0]
    assert "decision_maker_scoring" in result["evidence"]

def test_build_contact_intelligence_derived_department():
    # Performance Marketing Manager -> department "Marketing", functional_area DIGITAL_MARKETING
    lead = MockLead("Performance Marketing Manager", None, "a@b.com", "123", "url")
    result = build_contact_intelligence(lead)
    assert result["seniority_level"] == SeniorityLevel.MANAGER
    assert result["functional_area"] == FunctionalArea.DIGITAL_MARKETING
    assert result["department"] == "Marketing"

    # SEO Manager -> department "Marketing", functional_area SEO
    lead2 = MockLead("SEO Manager", None, "a@b.com", "123", "url")
    result2 = build_contact_intelligence(lead2)
    assert result2["functional_area"] == FunctionalArea.SEO
    assert result2["department"] == "Marketing"

    # Sales Director -> department "Sales", functional_area SALES
    lead3 = MockLead("Sales Director", None, "a@b.com", "123", "url")
    result3 = build_contact_intelligence(lead3)
    assert result3["functional_area"] == FunctionalArea.SALES
    assert result3["department"] == "Sales"

    # CTO -> department "Technology", functional_area TECHNOLOGY
    lead4 = MockLead("CTO", None, "a@b.com", "123", "url")
    result4 = build_contact_intelligence(lead4)
    assert result4["functional_area"] == FunctionalArea.TECHNOLOGY
    assert result4["department"] == "Technology"

    # unknown title -> department None
    lead5 = MockLead("Generic Title", None, "a@b.com", "123", "url")
    result5 = build_contact_intelligence(lead5)
    assert result5["functional_area"] == FunctionalArea.UNKNOWN
    assert result5["department"] is None

# Phase 12H-C Comprehensive Tests
def test_phase_12hc_scoring_personas():
    # 1. CEO with email, phone, and LinkedIn receives very high score (90)
    ceo = MockLead("CEO", None, "ceo@test.com", "1234567890", "https://linkedin.com/in/ceo")
    ceo_res = build_contact_intelligence(ceo)
    # Seniority: 40, Func: 20, Contact: 15, Role: 10, Dept: 5 -> 90
    assert ceo_res["decision_maker_score"] == 90
    assert ceo_res["buying_influence"] == BuyingInfluenceLevel.DECISION_MAKER
    assert ceo_res["confidence"] == ConfidenceLevel.HIGH

    # 2. Chief Marketing Officer is EXECUTIVE + MARKETING and scores strongly (100)
    cmo = MockLead("Chief Marketing Officer", None, "cmo@test.com", "1234567890", "https://linkedin.com/in/cmo")
    cmo_res = build_contact_intelligence(cmo)
    # Seniority: 40, Func: 25, Contact: 15, Role: 10, Dept: 10 -> 100
    assert cmo_res["decision_maker_score"] == 100
    assert cmo_res["buying_influence"] == BuyingInfluenceLevel.DECISION_MAKER
    assert cmo_res["confidence"] == ConfidenceLevel.HIGH

    # 3. Head of Digital Marketing is LEADERSHIP + DIGITAL_MARKETING and scores strongly (90)
    head_dm = MockLead("Head of Digital Marketing", None, "dm@test.com", "1234567890", "https://linkedin.com/in/dm")
    head_dm_res = build_contact_intelligence(head_dm)
    # Seniority: 30, Func: 25, Contact: 15, Role: 10, Dept: 10 -> 90
    assert head_dm_res["decision_maker_score"] == 90
    assert head_dm_res["buying_influence"] == BuyingInfluenceLevel.DECISION_MAKER

    # 4. SEO Manager is MANAGER + SEO (71)
    seo_mgr = MockLead("SEO Manager", None, "seo@test.com", "1234567890", None)
    seo_res = build_contact_intelligence(seo_mgr)
    # Seniority: 20, Func: 25, Contact: 10, Role: 6, Dept: 10 -> 71
    assert seo_res["decision_maker_score"] == 71
    assert seo_res["buying_influence"] == BuyingInfluenceLevel.HIGH

    # 5. Performance Marketing Manager is MANAGER + DIGITAL_MARKETING
    pm_mgr = MockLead("Performance Marketing Manager", None, "pm@test.com", "1234567890", None)
    pm_res = build_contact_intelligence(pm_mgr)
    # Seniority: 20, Func: 25, Contact: 10, Role: 6, Dept: 10 -> 71
    assert pm_res["decision_maker_score"] == 71
    assert pm_res["functional_area"] == FunctionalArea.DIGITAL_MARKETING

    # 6. Marketing Executive remains INDIVIDUAL_CONTRIBUTOR despite the word Executive
    mkt_exec = MockLead("Marketing Executive", None, "mkt@test.com", "1234567890", None)
    mkt_exec_res = build_contact_intelligence(mkt_exec)
    # Seniority: 8, Func: 25, Contact: 10, Role: 2, Dept: 10 -> 55
    assert mkt_exec_res["seniority_level"] == SeniorityLevel.INDIVIDUAL_CONTRIBUTOR
    assert mkt_exec_res["decision_maker_score"] == 55
    assert mkt_exec_res["buying_influence"] == BuyingInfluenceLevel.MEDIUM

    # 7. Sales Director is LEADERSHIP + SALES
    sales_dir = MockLead("Sales Director", None, "sales@test.com", "1234567890", None)
    sales_dir_res = build_contact_intelligence(sales_dir)
    # Seniority: 30, Func: 25, Contact: 10, Role: 10, Dept: 10 -> 85
    assert sales_dir_res["decision_maker_score"] == 85
    assert sales_dir_res["buying_influence"] == BuyingInfluenceLevel.DECISION_MAKER

    # 8. CTO is EXECUTIVE + TECHNOLOGY
    cto = MockLead("CTO", None, "cto@test.com", "1234567890", None)
    cto_res = build_contact_intelligence(cto)
    # Seniority: 40, Func: 12, Contact: 10, Role: 10, Dept: 5 -> 77
    assert cto_res["seniority_level"] == SeniorityLevel.EXECUTIVE
    assert cto_res["functional_area"] == FunctionalArea.TECHNOLOGY
    assert cto_res["decision_maker_score"] == 77
    assert cto_res["buying_influence"] == BuyingInfluenceLevel.DECISION_MAKER

    # 9. Unknown title with only email must not become high decision-maker score
    unk = MockLead(None, None, "unk@test.com", None, None)
    unk_res = build_contact_intelligence(unk)
    assert unk_res["decision_maker_score"] == 5
    assert unk_res["buying_influence"] == BuyingInfluenceLevel.UNKNOWN
    assert unk_res["confidence"] == ConfidenceLevel.UNKNOWN

def test_phase_12hc_evidence_structure():
    lead = MockLead("Head of Digital Marketing", "Custom Mktg", "dm@test.com", "123", "https://li")
    res = build_contact_intelligence(lead)
    ev = res["evidence"]

    assert "decision_maker_scoring" in ev
    dm_scoring = ev["decision_maker_scoring"]

    assert dm_scoring["seniority"]["value"] == "LEADERSHIP"
    assert dm_scoring["seniority"]["points"] == 30
    assert dm_scoring["functional_area"]["value"] == "DIGITAL_MARKETING"
    assert dm_scoring["functional_area"]["points"] == 25
    assert dm_scoring["contact_completeness"]["email"] == 5
    assert dm_scoring["contact_completeness"]["phone"] == 5
    assert dm_scoring["contact_completeness"]["linkedin"] == 5
    assert dm_scoring["contact_completeness"]["points"] == 15
    assert dm_scoring["role_strength"]["points"] == 10
    assert dm_scoring["department_relevance"]["value"] == "Custom Mktg"
    assert dm_scoring["department_relevance"]["points"] == 3  # custom dept -> 3
    assert dm_scoring["total"] == 83
    assert not dm_scoring["capped"]

def test_phase_12hc_idempotency_and_range():
    lead = MockLead("CEO", None, "a@b.com", "123", "url")
    res1 = build_contact_intelligence(lead)
    res2 = build_contact_intelligence(lead)

    assert res1 == res2
    assert 0 <= res1["decision_maker_score"] <= 100

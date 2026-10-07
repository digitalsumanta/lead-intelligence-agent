import pytest
from app.domain.contact_intelligence import (
    normalize_title,
    classify_seniority,
    classify_functional_area,
    calculate_decision_maker_score,
    build_contact_intelligence,
    SeniorityLevel,
    FunctionalArea,
    BuyingInfluenceLevel,
    determine_buying_influence
)

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
    score, ev = calculate_decision_maker_score(SeniorityLevel.EXECUTIVE, FunctionalArea.SALES, True, True, True)
    assert score == 100
    assert len(ev) == 5

    score, ev = calculate_decision_maker_score(SeniorityLevel.MANAGER, FunctionalArea.IT, True, False, False)
    assert score == 40

    score, ev = calculate_decision_maker_score(SeniorityLevel.EXECUTIVE, FunctionalArea.EXECUTIVE, True, True, True)
    assert score == 100

def test_semantic_unknown_title_no_false_positive():
    score, _ = calculate_decision_maker_score(SeniorityLevel.UNKNOWN, FunctionalArea.UNKNOWN, True, False, False)
    assert score == 10
    inf = determine_buying_influence(SeniorityLevel.UNKNOWN, score)
    assert inf == BuyingInfluenceLevel.UNKNOWN

def test_semantic_seniority_weight():
    high_score, _ = calculate_decision_maker_score(SeniorityLevel.LEADERSHIP, FunctionalArea.MARKETING, True, True, True)
    low_score, _ = calculate_decision_maker_score(SeniorityLevel.INDIVIDUAL_CONTRIBUTOR, FunctionalArea.MARKETING, True, True, True)
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
    def __init__(self, job_title, department, email, phone, linkedin):
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

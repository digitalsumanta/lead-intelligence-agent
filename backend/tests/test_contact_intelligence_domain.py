import pytest
from app.domain.contact_intelligence import (
    normalize_title,
    classify_seniority,
    classify_functional_area,
    calculate_decision_maker_score,
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
    assert classify_seniority("CEO") == SeniorityLevel.EXECUTIVE
    assert classify_seniority("Chief Executive Officer") == SeniorityLevel.EXECUTIVE
    assert classify_seniority("Managing Director") == SeniorityLevel.EXECUTIVE
    assert classify_seniority("Founder and CEO") == SeniorityLevel.EXECUTIVE

    # Leadership
    assert classify_seniority("VP of Sales") == SeniorityLevel.LEADERSHIP
    assert classify_seniority("Director of Marketing") == SeniorityLevel.LEADERSHIP
    assert classify_seniority("Head of IT") == SeniorityLevel.LEADERSHIP

    # Manager
    assert classify_seniority("Senior Manager") == SeniorityLevel.MANAGER
    assert classify_seniority("Marketing Manager") == SeniorityLevel.MANAGER
    assert classify_seniority("Team Lead") == SeniorityLevel.MANAGER

    # Individual Contributor
    assert classify_seniority("Sales Executive") == SeniorityLevel.INDIVIDUAL_CONTRIBUTOR
    assert classify_seniority("Marketing Specialist") == SeniorityLevel.INDIVIDUAL_CONTRIBUTOR
    assert classify_seniority("Data Analyst") == SeniorityLevel.INDIVIDUAL_CONTRIBUTOR

    # Unknown
    assert classify_seniority("Unknown Title") == SeniorityLevel.UNKNOWN
    assert classify_seniority(None) == SeniorityLevel.UNKNOWN

def test_classify_functional_area():
    assert classify_functional_area("Marketing Manager") == FunctionalArea.MARKETING
    assert classify_functional_area("Digital Marketing Specialist") == FunctionalArea.DIGITAL_MARKETING
    assert classify_functional_area("VP of Sales") == FunctionalArea.SALES
    assert classify_functional_area("Head of SEO") == FunctionalArea.SEO
    assert classify_functional_area("Software Engineer") == FunctionalArea.TECHNOLOGY
    assert classify_functional_area("CFO") == FunctionalArea.FINANCE
    assert classify_functional_area("CEO") == FunctionalArea.EXECUTIVE
    assert classify_functional_area("Something Else") == FunctionalArea.UNKNOWN
    assert classify_functional_area(None) == FunctionalArea.UNKNOWN

def test_calculate_decision_maker_score():
    # Executive + Sales + All info = 50 + 20 + 30 = 100
    score, ev = calculate_decision_maker_score(SeniorityLevel.EXECUTIVE, FunctionalArea.SALES, True, True, True)
    assert score == 100
    assert len(ev) == 5

    # Manager + IT + Email only = 20 + 10 + 10 = 40
    score, ev = calculate_decision_maker_score(SeniorityLevel.MANAGER, FunctionalArea.IT, True, False, False)
    assert score == 40
    
    # Cap at 100
    score, ev = calculate_decision_maker_score(SeniorityLevel.EXECUTIVE, FunctionalArea.EXECUTIVE, True, True, True)
    assert score == 100

def test_semantic_unknown_title_no_false_positive():
    # An unknown title must not become a decision maker merely because a contact has an email.
    score, _ = calculate_decision_maker_score(SeniorityLevel.UNKNOWN, FunctionalArea.UNKNOWN, True, False, False)
    # Score should just be 10 (from email)
    assert score == 10
    inf = determine_buying_influence(SeniorityLevel.UNKNOWN, score)
    assert inf == BuyingInfluenceLevel.UNKNOWN

def test_semantic_seniority_weight():
    # High seniority scores higher than low seniority when other evidence is equal
    high_score, _ = calculate_decision_maker_score(SeniorityLevel.LEADERSHIP, FunctionalArea.MARKETING, True, True, True)
    low_score, _ = calculate_decision_maker_score(SeniorityLevel.INDIVIDUAL_CONTRIBUTOR, FunctionalArea.MARKETING, True, True, True)
    
    assert high_score > low_score

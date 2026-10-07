import re
from typing import Optional, Tuple, List, Dict, Any
import enum

from app.models.entities import SeniorityLevel, FunctionalArea, BuyingInfluenceLevel

def normalize_title(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    # Lowercase and remove extra whitespace/punctuation
    normalized = title.lower().strip()
    normalized = re.sub(r'[^a-z0-9\s]', ' ', normalized)
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized if normalized else None

def classify_seniority(title: Optional[str]) -> Tuple[SeniorityLevel, Optional[str]]:
    norm = normalize_title(title)
    if not norm:
        return SeniorityLevel.UNKNOWN, None

    rules = [
        # Executive
        (r'\b(ceo|chief executive officer|cfo|chief financial officer|coo|chief operating officer|cto|chief technology officer|cmo|chief marketing officer|cio|chief information officer|chro|chief human resources officer|founder|co founder|co-founder|president|owner|managing director)\b', SeniorityLevel.EXECUTIVE),
        # Leadership
        (r'\b(vp|vice president|head of|head|director|partner|principal|general manager)\b', SeniorityLevel.LEADERSHIP),
        # Manager
        (r'\b(senior manager|manager|team lead|lead|supervisor)\b', SeniorityLevel.MANAGER),
        # Individual Contributor
        (r'\b(executive|specialist|analyst|coordinator|associate|officer|engineer|developer|consultant|representative)\b', SeniorityLevel.INDIVIDUAL_CONTRIBUTOR)
    ]

    for pattern, level in rules:
        match = re.search(pattern, norm)
        if match:
            return level, match.group(0)

    return SeniorityLevel.UNKNOWN, None

def classify_functional_area(title: Optional[str]) -> Tuple[FunctionalArea, Optional[str]]:
    norm = normalize_title(title)
    if not norm:
        return FunctionalArea.UNKNOWN, None

    rules = [
        (r'\b(ceo|chief executive|founder|president|owner|managing director)\b', FunctionalArea.EXECUTIVE),
        (r'\b(digital marketing|performance marketing|social media|paid media|growth marketing)\b', FunctionalArea.DIGITAL_MARKETING),
        (r'\b(seo|search engine optimization)\b', FunctionalArea.SEO),
        (r'\b(cmo|marketing)\b', FunctionalArea.MARKETING),
        (r'\b(sales|account|revenue|business development)\b', FunctionalArea.SALES),
        (r'\b(cto|technology|software|engineering|tech|developer|engineer)\b', FunctionalArea.TECHNOLOGY),
        (r'\b(cio|it|information technology)\b', FunctionalArea.IT),
        (r'\b(cfo|finance|accounting|controller)\b', FunctionalArea.FINANCE),
        (r'\b(chro|hr|human resources|people)\b', FunctionalArea.HR),
        (r'\b(coo|operations)\b', FunctionalArea.OPERATIONS),
        (r'\b(procurement|purchasing|buyer)\b', FunctionalArea.PROCUREMENT)
    ]

    for pattern, area in rules:
        match = re.search(pattern, norm)
        if match:
            return area, match.group(0)

    return FunctionalArea.UNKNOWN, None

# Scoring Component Maps & Functions

SENIORITY_SCORE_MAP: Dict[SeniorityLevel, Tuple[int, str]] = {
    SeniorityLevel.EXECUTIVE: (40, "Executive-level role"),
    SeniorityLevel.LEADERSHIP: (30, "Leadership-level role"),
    SeniorityLevel.MANAGER: (20, "Manager-level role"),
    SeniorityLevel.INDIVIDUAL_CONTRIBUTOR: (8, "Individual contributor role"),
    SeniorityLevel.UNKNOWN: (0, "Unknown seniority level"),
}

def score_seniority(seniority: SeniorityLevel) -> Tuple[int, str]:
    return SENIORITY_SCORE_MAP.get(seniority, (0, "Unknown seniority level"))

FUNCTIONAL_AREA_SCORE_MAP: Dict[FunctionalArea, Tuple[int, str]] = {
    FunctionalArea.MARKETING: (25, "Highly relevant functional area"),
    FunctionalArea.DIGITAL_MARKETING: (25, "Highly relevant functional area"),
    FunctionalArea.SEO: (25, "Highly relevant functional area"),
    FunctionalArea.SALES: (25, "Highly relevant functional area"),
    FunctionalArea.EXECUTIVE: (20, "Executive functional area with broad organizational relevance"),
    FunctionalArea.TECHNOLOGY: (12, "Technical/engineering functional area"),
    FunctionalArea.IT: (12, "Information technology functional area"),
    FunctionalArea.FINANCE: (12, "Finance and accounting functional area"),
    FunctionalArea.HR: (12, "Human resources functional area"),
    FunctionalArea.OPERATIONS: (12, "Operations functional area"),
    FunctionalArea.PROCUREMENT: (12, "Procurement/purchasing functional area"),
    FunctionalArea.UNKNOWN: (0, "Unknown functional area"),
}

def score_functional_area(func_area: FunctionalArea) -> Tuple[int, str]:
    return FUNCTIONAL_AREA_SCORE_MAP.get(func_area, (0, "Unknown functional area"))

def score_contact_completeness(has_email: bool, has_phone: bool, has_linkedin: bool) -> Tuple[int, Dict[str, int], str]:
    email_pts = 5 if has_email else 0
    phone_pts = 5 if has_phone else 0
    linkedin_pts = 5 if has_linkedin else 0
    total = email_pts + phone_pts + linkedin_pts

    parts = []
    if has_email:
        parts.append("email")
    if has_phone:
        parts.append("phone")
    if has_linkedin:
        parts.append("LinkedIn")

    if parts:
        reason = f"Persisted contact coordinates present: {', '.join(parts)}"
    else:
        reason = "No contact coordinates present"

    details = {
        "email": email_pts,
        "phone": phone_pts,
        "linkedin": linkedin_pts,
        "points": total
    }
    return total, details, reason

def score_role_strength(title: Optional[str]) -> Tuple[int, str]:
    norm = normalize_title(title)
    if not norm:
        return 0, "No role title available"

    high_impact_pattern = r'\b(ceo|chief executive officer|cfo|chief financial officer|coo|chief operating officer|cto|chief technology officer|cmo|chief marketing officer|cio|chief information officer|chro|chief human resources officer|founder|co founder|co-founder|president|owner|managing director|vp|vice president|head of|head|director|partner|principal|general manager)\b'
    if re.search(high_impact_pattern, norm):
        return 10, "Strong buying-influence title signal"

    managerial_pattern = r'\b(senior manager|manager|team lead|lead|supervisor)\b'
    if re.search(managerial_pattern, norm):
        return 6, "Moderate buying-influence title signal"

    standard_pattern = r'\b(executive|specialist|analyst|coordinator|associate|officer|engineer|developer|consultant|representative)\b'
    if re.search(standard_pattern, norm):
        return 2, "Standard role title signal"

    return 0, "No recognized role strength keywords"

def score_department_relevance(department: Optional[str]) -> Tuple[int, str]:
    if not department:
        return 0, "No department available"

    dept_norm = department.strip().lower()

    if dept_norm in ["marketing", "digital marketing", "digital_marketing", "seo", "sales"]:
        return 10, "Highly relevant department"

    if dept_norm in ["executive", "technology", "it", "finance", "hr", "operations", "procurement"]:
        return 5, "Supporting operational department"

    return 3, "General/other department"

def calculate_decision_maker_score(
    seniority: SeniorityLevel,
    func_area: FunctionalArea,
    has_email: bool,
    has_phone: bool,
    has_linkedin: bool,
    title: Optional[str] = None,
    department: Optional[str] = None
) -> Tuple[int, Dict[str, Any], List[str]]:
    """
    Deterministically score a contact's decision making power based on persisted data.
    """
    seniority_pts, seniority_reason = score_seniority(seniority)
    func_pts, func_reason = score_functional_area(func_area)
    completeness_pts, completeness_details, completeness_reason = score_contact_completeness(has_email, has_phone, has_linkedin)
    role_pts, role_reason = score_role_strength(title)
    dept_pts, dept_reason = score_department_relevance(department)

    raw_total = seniority_pts + func_pts + completeness_pts + role_pts + dept_pts
    final_score = min(100, max(0, raw_total))
    capped = raw_total > 100

    scoring_details: Dict[str, Any] = {
        "seniority": {
            "value": seniority.value,
            "points": seniority_pts,
            "reason": seniority_reason
        },
        "functional_area": {
            "value": func_area.value,
            "points": func_pts,
            "reason": func_reason
        },
        "contact_completeness": completeness_details,
        "role_strength": {
            "points": role_pts,
            "reason": role_reason
        },
        "department_relevance": {
            "value": department,
            "points": dept_pts,
            "reason": dept_reason
        },
        "total": final_score,
        "capped": capped
    }

    summary_components = [
        f"Seniority {seniority.value}: {seniority_pts} pts ({seniority_reason})",
        f"Functional Area {func_area.value}: {func_pts} pts ({func_reason})",
        f"Contact Completeness: {completeness_pts} pts ({completeness_reason})",
        f"Role Strength: {role_pts} pts ({role_reason})",
        f"Department Relevance ({department}): {dept_pts} pts ({dept_reason})"
    ]
    if capped:
        summary_components.append("Total score capped at 100.")

    return final_score, scoring_details, summary_components

def determine_buying_influence(seniority: SeniorityLevel, score: int) -> BuyingInfluenceLevel:
    if seniority == SeniorityLevel.UNKNOWN and score < 30:
        return BuyingInfluenceLevel.UNKNOWN

    if seniority == SeniorityLevel.EXECUTIVE and score >= 70:
        return BuyingInfluenceLevel.DECISION_MAKER
    elif score >= 85 and seniority in (SeniorityLevel.EXECUTIVE, SeniorityLevel.LEADERSHIP):
        return BuyingInfluenceLevel.DECISION_MAKER
    elif score >= 65 or (seniority in (SeniorityLevel.EXECUTIVE, SeniorityLevel.LEADERSHIP) and score >= 50):
        return BuyingInfluenceLevel.HIGH
    elif score >= 40 or seniority == SeniorityLevel.MANAGER:
        return BuyingInfluenceLevel.MEDIUM
    elif score >= 15 or seniority == SeniorityLevel.INDIVIDUAL_CONTRIBUTOR:
        return BuyingInfluenceLevel.LOW

    return BuyingInfluenceLevel.UNKNOWN

def build_contact_intelligence(lead: Any) -> Dict[str, Any]:
    """
    Orchestrate the deterministic evaluation for a contact (lead).
    `lead` is the Lead model instance which acts as the contact entity.
    """
    title = getattr(lead, "job_title", None)
    department_from_lead = getattr(lead, "department", None)

    seniority, sen_match = classify_seniority(title)
    func_area, func_match = classify_functional_area(title)

    # "When department is absent, derive it deterministically from normalized role/title signals where appropriate."
    department = department_from_lead
    if not department:
        department_map = {
            FunctionalArea.EXECUTIVE: "Executive",
            FunctionalArea.MARKETING: "Marketing",
            FunctionalArea.DIGITAL_MARKETING: "Marketing",
            FunctionalArea.SEO: "Marketing",
            FunctionalArea.SALES: "Sales",
            FunctionalArea.IT: "IT",
            FunctionalArea.TECHNOLOGY: "Technology",
            FunctionalArea.FINANCE: "Finance",
            FunctionalArea.HR: "HR",
            FunctionalArea.OPERATIONS: "Operations",
            FunctionalArea.PROCUREMENT: "Procurement",
            FunctionalArea.UNKNOWN: None
        }
        department = department_map.get(func_area, None)

    has_email = bool(getattr(lead, "email", None))
    has_phone = bool(getattr(lead, "phone", None))
    has_linkedin = bool(getattr(lead, "linkedin_url", None))

    score, scoring_details, score_components = calculate_decision_maker_score(
        seniority=seniority,
        func_area=func_area,
        has_email=has_email,
        has_phone=has_phone,
        has_linkedin=has_linkedin,
        title=title,
        department=department
    )
    influence = determine_buying_influence(seniority, score)

    # Confidence Level
    from app.models.entities import ConfidenceLevel
    confidence = ConfidenceLevel.UNKNOWN
    if title and title.strip():
        role_pts, _ = score_role_strength(title)
        if seniority != SeniorityLevel.UNKNOWN and func_area != FunctionalArea.UNKNOWN and role_pts > 0:
            confidence = ConfidenceLevel.HIGH
        elif seniority != SeniorityLevel.UNKNOWN or func_area != FunctionalArea.UNKNOWN:
            confidence = ConfidenceLevel.MEDIUM
        else:
            confidence = ConfidenceLevel.LOW

    classification_evidence = []
    if not title or not title.strip():
        classification_evidence.append("Insufficient persisted data available (missing job_title).")
    else:
        if sen_match:
            classification_evidence.append(f"Seniority '{seniority.value}' assigned due to match '{sen_match}' in title.")
        if func_match:
            classification_evidence.append(f"Functional Area '{func_area.value}' assigned due to match '{func_match}' in title.")

    return {
        "department": department,
        "functional_area": func_area,
        "seniority_level": seniority,
        "decision_maker_score": score,
        "buying_influence": influence,
        "confidence": confidence,
        "evidence": {
            "score_components": score_components,
            "decision_maker_scoring": scoring_details,
            "classification_evidence": classification_evidence,
            "has_email": has_email,
            "has_phone": has_phone,
            "has_linkedin": has_linkedin,
            "persisted_title": title,
            "persisted_department": department_from_lead
        }
    }

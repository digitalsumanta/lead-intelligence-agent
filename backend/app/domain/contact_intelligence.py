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

def calculate_decision_maker_score(
    seniority: SeniorityLevel,
    func_area: FunctionalArea,
    has_email: bool,
    has_phone: bool,
    has_linkedin: bool
) -> Tuple[int, List[str]]:
    """
    Deterministically score a contact's decision making power based on persisted data.
    """
    score = 0
    evidence = []

    seniority_scores = {
        SeniorityLevel.EXECUTIVE: 50,
        SeniorityLevel.LEADERSHIP: 35,
        SeniorityLevel.MANAGER: 20,
        SeniorityLevel.INDIVIDUAL_CONTRIBUTOR: 5,
        SeniorityLevel.UNKNOWN: 0
    }
    s_score = seniority_scores.get(seniority, 0)
    score += s_score
    evidence.append(f"Seniority {seniority.value} adds {s_score} points.")

    if func_area in [FunctionalArea.EXECUTIVE, FunctionalArea.MARKETING, FunctionalArea.DIGITAL_MARKETING, FunctionalArea.SALES]:
        score += 20
        evidence.append(f"Functional Area {func_area.value} adds 20 points.")
    elif func_area != FunctionalArea.UNKNOWN:
        score += 10
        evidence.append(f"Functional Area {func_area.value} adds 10 points.")

    if has_email:
        score += 10
        evidence.append("Persisted email presence adds 10 points.")
    if has_phone:
        score += 10
        evidence.append("Persisted phone presence adds 10 points.")
    if has_linkedin:
        score += 10
        evidence.append("Persisted LinkedIn presence adds 10 points.")

    if score > 100:
        score = 100
        evidence.append("Score capped at 100.")

    return score, evidence

def determine_buying_influence(seniority: SeniorityLevel, score: int) -> BuyingInfluenceLevel:
    if seniority == SeniorityLevel.EXECUTIVE or score >= 80:
        return BuyingInfluenceLevel.DECISION_MAKER
    elif seniority == SeniorityLevel.LEADERSHIP or score >= 60:
        return BuyingInfluenceLevel.HIGH
    elif seniority == SeniorityLevel.MANAGER or score >= 40:
        return BuyingInfluenceLevel.MEDIUM
    elif seniority == SeniorityLevel.INDIVIDUAL_CONTRIBUTOR or score >= 20:
        return BuyingInfluenceLevel.LOW

    return BuyingInfluenceLevel.UNKNOWN

def build_contact_intelligence(lead: Any) -> Dict[str, Any]:
    """
    Orchestrate the deterministic evaluation for a contact (lead).
    `lead` is the Lead model instance which acts as the contact entity.
    """
    title = lead.job_title
    department_from_lead = lead.department

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

    has_email = bool(lead.email)
    has_phone = bool(lead.phone)
    has_linkedin = bool(lead.linkedin_url)

    score, evidence = calculate_decision_maker_score(seniority, func_area, has_email, has_phone, has_linkedin)
    influence = determine_buying_influence(seniority, score)

    score_components = evidence

    # Confidence Level
    from app.models.entities import ConfidenceLevel
    confidence = ConfidenceLevel.UNKNOWN
    if title:
        if seniority != SeniorityLevel.UNKNOWN and func_area != FunctionalArea.UNKNOWN:
            confidence = ConfidenceLevel.HIGH
        elif seniority != SeniorityLevel.UNKNOWN or func_area != FunctionalArea.UNKNOWN:
            confidence = ConfidenceLevel.MEDIUM
        else:
            confidence = ConfidenceLevel.LOW

    classification_evidence = []
    if not title:
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
            "classification_evidence": classification_evidence,
            "has_email": has_email,
            "has_phone": has_phone,
            "has_linkedin": has_linkedin,
            "persisted_title": title,
            "persisted_department": department_from_lead
        }
    }

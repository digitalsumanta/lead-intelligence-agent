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

def classify_seniority(title: Optional[str]) -> SeniorityLevel:
    norm = normalize_title(title)
    if not norm:
        return SeniorityLevel.UNKNOWN

    # Match exact or distinct words to avoid substring false positives (e.g., "manager" in "office manager")
    # Actually, we can check for keywords with word boundaries
    
    # Executive
    exec_keywords = [r'\bceo\b', r'\bchief executive officer\b', r'\bfounder\b', r'\bco founder\b', r'\bowner\b', r'\bmanaging director\b', r'\bmd\b', r'\bpresident\b', r'\bcfo\b', r'\bcoo\b', r'\bcmo\b', r'\bcto\b']
    if any(re.search(kw, norm) for kw in exec_keywords):
        return SeniorityLevel.EXECUTIVE
        
    # Leadership
    leadership_keywords = [r'\bvp\b', r'\bvice president\b', r'\bhead of\b', r'\bdirector\b']
    if any(re.search(kw, norm) for kw in leadership_keywords):
        return SeniorityLevel.LEADERSHIP
        
    # Manager
    manager_keywords = [r'\bmanager\b', r'\bsenior manager\b', r'\bteam lead\b', r'\blead\b']
    if any(re.search(kw, norm) for kw in manager_keywords):
        return SeniorityLevel.MANAGER
        
    # Individual Contributor
    ic_keywords = [r'\bexecutive\b', r'\bspecialist\b', r'\banalyst\b', r'\bcoordinator\b', r'\bassociate\b', r'\bofficer\b', r'\bengineer\b', r'\bdeveloper\b', r'\bconsultant\b']
    if any(re.search(kw, norm) for kw in ic_keywords):
        return SeniorityLevel.INDIVIDUAL_CONTRIBUTOR
        
    return SeniorityLevel.UNKNOWN

def classify_functional_area(title: Optional[str]) -> FunctionalArea:
    norm = normalize_title(title)
    if not norm:
        return FunctionalArea.UNKNOWN
        
    if re.search(r'\b(marketing|cmo)\b', norm):
        if re.search(r'\b(digital|online)\b', norm):
            return FunctionalArea.DIGITAL_MARKETING
        return FunctionalArea.MARKETING
    
    if re.search(r'\bseo\b', norm):
        return FunctionalArea.SEO
        
    if re.search(r'\b(sales|account|revenue)\b', norm):
        return FunctionalArea.SALES
        
    if re.search(r'\b(it|information technology)\b', norm):
        return FunctionalArea.IT
        
    if re.search(r'\b(tech|technology|engineering|software|cto)\b', norm):
        return FunctionalArea.TECHNOLOGY
        
    if re.search(r'\b(finance|cfo|accounting)\b', norm):
        return FunctionalArea.FINANCE
        
    if re.search(r'\b(hr|human resources|people)\b', norm):
        return FunctionalArea.HR
        
    if re.search(r'\b(operations|coo)\b', norm):
        return FunctionalArea.OPERATIONS
        
    if re.search(r'\b(procurement|purchasing|buyer)\b', norm):
        return FunctionalArea.PROCUREMENT
        
    if re.search(r'\b(ceo|chief executive|founder|president|owner|md|managing director)\b', norm):
        return FunctionalArea.EXECUTIVE

    return FunctionalArea.UNKNOWN

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
    
    # 1. Seniority (Strongest signal)
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
    
    # 2. Functional Area Relevance
    # In B2B sales context, Executives and specific departments might be more relevant.
    if func_area in [FunctionalArea.EXECUTIVE, FunctionalArea.MARKETING, FunctionalArea.DIGITAL_MARKETING, FunctionalArea.SALES]:
        score += 20
        evidence.append(f"Functional Area {func_area.value} adds 20 points.")
    elif func_area != FunctionalArea.UNKNOWN:
        score += 10
        evidence.append(f"Functional Area {func_area.value} adds 10 points.")
        
    # 3. Completeness of persisted contact information
    if has_email:
        score += 10
        evidence.append("Persisted email presence adds 10 points.")
    if has_phone:
        score += 10
        evidence.append("Persisted phone presence adds 10 points.")
    if has_linkedin:
        score += 10
        evidence.append("Persisted LinkedIn presence adds 10 points.")
        
    # Cap at 100
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
    # Since the existing Lead model does not persist a job_title field,
    # we use None. The rules are designed to gracefully handle this and evaluate to UNKNOWN.
    title = None 
    
    seniority = classify_seniority(title)
    func_area = classify_functional_area(title)
    
    has_email = bool(lead.email)
    has_phone = bool(lead.phone)
    has_linkedin = bool(lead.linkedin_url)
    
    score, evidence = calculate_decision_maker_score(seniority, func_area, has_email, has_phone, has_linkedin)
    influence = determine_buying_influence(seniority, score)
    
    return {
        "department": func_area.value if func_area != FunctionalArea.UNKNOWN else None,
        "functional_area": func_area,
        "seniority_level": seniority,
        "decision_maker_score": score,
        "buying_influence": influence,
        "evidence": {
            "score_components": evidence,
            "has_email": has_email,
            "has_phone": has_phone,
            "has_linkedin": has_linkedin,
            "persisted_title": title
        }
    }

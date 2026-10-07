import re
import uuid
import enum
from typing import Optional, Dict, Any, List, Tuple, Sequence

from app.models.entities import SeniorityLevel, FunctionalArea
from app.domain.contact_intelligence import (
    normalize_title,
    build_contact_intelligence,
)

class MatchLevel(str, enum.Enum):
    EXCELLENT = "EXCELLENT"  # 90-100
    STRONG = "STRONG"        # 75-89
    GOOD = "GOOD"            # 60-74
    MODERATE = "MODERATE"    # 40-59
    WEAK = "WEAK"            # 20-39
    POOR = "POOR"            # 0-19


def calculate_match_level(score: int) -> MatchLevel:
    """Deterministic conversion from integer score 0-100 to MatchLevel enum."""
    if score >= 90:
        return MatchLevel.EXCELLENT
    elif score >= 75:
        return MatchLevel.STRONG
    elif score >= 60:
        return MatchLevel.GOOD
    elif score >= 40:
        return MatchLevel.MODERATE
    elif score >= 20:
        return MatchLevel.WEAK
    else:
        return MatchLevel.POOR


def normalize_opportunity_type(opportunity_type: Optional[str]) -> str:
    """Normalize opportunity type string for deterministic category matching."""
    if not opportunity_type:
        return "GENERAL"
    norm = opportunity_type.strip().upper()
    if norm in ["SEO", "SEARCH_ENGINE_OPTIMIZATION"]:
        return "SEO"
    elif norm in ["DIGITAL_MARKETING", "DIGITAL_MARKETING_INTELLIGENCE", "PAID_ADVERTISING", "PERFORMANCE_MARKETING", "MARKETING"]:
        return "DIGITAL_MARKETING"
    elif norm in ["WEBSITE", "WEBSITE_DEVELOPMENT", "WEBSITE_INTELLIGENCE", "TECH", "TECHNOLOGY", "IT", "WEB"]:
        return "WEBSITE"
    elif norm in ["SALES", "LEAD_GENERATION", "REVENUE", "COMMERCIAL"]:
        return "SALES"
    return norm


# ==========================================
# 1. Functional Relevance (MAX 30)
# ==========================================

def score_functional_relevance(
    opportunity_type: Optional[str],
    func_area: FunctionalArea
) -> Tuple[int, str]:
    cat = normalize_opportunity_type(opportunity_type)

    if cat == "SEO":
        mapping = {
            FunctionalArea.SEO: (30, "Direct domain match for SEO opportunity"),
            FunctionalArea.DIGITAL_MARKETING: (30, "Directly aligned digital marketing functional area"),
            FunctionalArea.MARKETING: (22, "Broad marketing functional area alignment"),
            FunctionalArea.SALES: (14, "Commercial/sales functional alignment"),
            FunctionalArea.EXECUTIVE: (14, "Executive leadership oversight"),
            FunctionalArea.TECHNOLOGY: (10, "Technical supporting functional area"),
            FunctionalArea.IT: (10, "Technical supporting functional area"),
            FunctionalArea.OPERATIONS: (4, "Indirect operational functional area"),
            FunctionalArea.FINANCE: (4, "Indirect operational functional area"),
            FunctionalArea.HR: (4, "Indirect operational functional area"),
            FunctionalArea.PROCUREMENT: (4, "Indirect operational functional area"),
            FunctionalArea.UNKNOWN: (0, "Unknown functional area"),
        }
        return mapping.get(func_area, (0, "Unknown functional area"))

    elif cat == "DIGITAL_MARKETING":
        mapping = {
            FunctionalArea.DIGITAL_MARKETING: (30, "Direct domain match for digital marketing opportunity"),
            FunctionalArea.MARKETING: (30, "Directly aligned marketing functional area"),
            FunctionalArea.SEO: (24, "Closely related search marketing functional area"),
            FunctionalArea.SALES: (14, "Commercial/sales functional alignment"),
            FunctionalArea.EXECUTIVE: (14, "Executive leadership oversight"),
            FunctionalArea.TECHNOLOGY: (10, "Technical supporting functional area"),
            FunctionalArea.IT: (10, "Technical supporting functional area"),
            FunctionalArea.OPERATIONS: (4, "Indirect operational functional area"),
            FunctionalArea.FINANCE: (4, "Indirect operational functional area"),
            FunctionalArea.HR: (4, "Indirect operational functional area"),
            FunctionalArea.PROCUREMENT: (4, "Indirect operational functional area"),
            FunctionalArea.UNKNOWN: (0, "Unknown functional area"),
        }
        return mapping.get(func_area, (0, "Unknown functional area"))

    elif cat == "WEBSITE":
        mapping = {
            FunctionalArea.TECHNOLOGY: (30, "Direct domain match for technical/website opportunity"),
            FunctionalArea.IT: (30, "Direct domain match for IT/web infrastructure opportunity"),
            FunctionalArea.DIGITAL_MARKETING: (22, "Digital channel alignment for web presence"),
            FunctionalArea.MARKETING: (20, "Marketing alignment for website experience"),
            FunctionalArea.EXECUTIVE: (14, "Executive leadership oversight"),
            FunctionalArea.SALES: (10, "Commercial alignment"),
            FunctionalArea.OPERATIONS: (4, "Indirect operational functional area"),
            FunctionalArea.FINANCE: (4, "Indirect operational functional area"),
            FunctionalArea.HR: (4, "Indirect operational functional area"),
            FunctionalArea.PROCUREMENT: (4, "Indirect operational functional area"),
            FunctionalArea.UNKNOWN: (0, "Unknown functional area"),
        }
        return mapping.get(func_area, (0, "Unknown functional area"))

    elif cat == "SALES":
        mapping = {
            FunctionalArea.SALES: (30, "Direct domain match for sales opportunity"),
            FunctionalArea.MARKETING: (24, "Marketing lead generation alignment"),
            FunctionalArea.DIGITAL_MARKETING: (24, "Digital demand generation alignment"),
            FunctionalArea.EXECUTIVE: (16, "Executive revenue oversight"),
            FunctionalArea.SEO: (16, "Inbound search traffic alignment"),
            FunctionalArea.TECHNOLOGY: (6, "Supporting technical functional area"),
            FunctionalArea.IT: (6, "Supporting IT functional area"),
            FunctionalArea.FINANCE: (6, "Supporting finance functional area"),
            FunctionalArea.OPERATIONS: (6, "Supporting operations functional area"),
            FunctionalArea.HR: (6, "Supporting HR functional area"),
            FunctionalArea.PROCUREMENT: (6, "Supporting procurement functional area"),
            FunctionalArea.UNKNOWN: (0, "Unknown functional area"),
        }
        return mapping.get(func_area, (0, "Unknown functional area"))

    else:
        # General / Unknown opportunity type fallback
        mapping = {
            FunctionalArea.EXECUTIVE: (20, "Executive general oversight"),
            FunctionalArea.SALES: (18, "Commercial general relevance"),
            FunctionalArea.MARKETING: (18, "Marketing general relevance"),
            FunctionalArea.DIGITAL_MARKETING: (18, "Digital marketing general relevance"),
            FunctionalArea.SEO: (12, "Search marketing functional area"),
            FunctionalArea.TECHNOLOGY: (10, "Technical functional area"),
            FunctionalArea.IT: (10, "IT functional area"),
            FunctionalArea.OPERATIONS: (8, "Operational functional area"),
            FunctionalArea.FINANCE: (8, "Finance functional area"),
            FunctionalArea.HR: (6, "Human resources functional area"),
            FunctionalArea.PROCUREMENT: (6, "Procurement functional area"),
            FunctionalArea.UNKNOWN: (0, "Unknown functional area"),
        }
        return mapping.get(func_area, (0, "Unknown functional area"))


# ==========================================
# 2. Role / Title Relevance (MAX 20)
# ==========================================

def score_role_relevance(
    opportunity_type: Optional[str],
    job_title: Optional[str]
) -> Tuple[int, str]:
    norm = normalize_title(job_title)
    if not norm:
        return 0, "No job title available"

    cat = normalize_opportunity_type(opportunity_type)

    if cat == "SEO":
        # Very Strong (20)
        if re.search(r'\b(seo manager|seo head|head of seo|seo director|director of seo|vp of seo|seo specialist|seo lead|search engine optimization)\b', norm):
            return 20, "Directly matched SEO specialist/leadership title"
        # Strong (15)
        if re.search(r'\b(digital marketing manager|head of digital marketing|digital marketing director|marketing manager|marketing head|head of marketing|director of marketing|vp of marketing|cmo|performance marketing|growth marketing)\b', norm):
            return 15, "Strong title relevance to marketing domain"
        # Moderate (10)
        if re.search(r'\b(ceo|founder|co founder|co-founder|owner|managing director|president|general manager)\b', norm):
            return 10, "Executive management title relevance"
        # Low (4)
        if re.search(r'\b(sales|technology|tech lead|it manager|developer|engineer)\b', norm):
            return 4, "Secondary functional title relevance"
        # Irrelevant / Very Low (1)
        return 1, "Low relevance functional title"

    elif cat == "DIGITAL_MARKETING":
        # Very Strong (20)
        if re.search(r'\b(digital marketing manager|head of digital marketing|digital marketing director|director of digital marketing|vp of digital marketing|performance marketing manager|growth marketing manager|social media manager|paid media manager|cmo|chief marketing officer)\b', norm):
            return 20, "Directly matched digital marketing leadership title"
        # Strong (15)
        if re.search(r'\b(marketing manager|head of marketing|director of marketing|vp of marketing|seo manager|head of seo|seo director|seo specialist|marketing specialist|marketing coordinator)\b', norm):
            return 15, "Strong title relevance to marketing/SEO channels"
        # Moderate (10)
        if re.search(r'\b(ceo|founder|co founder|co-founder|owner|managing director|president|general manager)\b', norm):
            return 10, "Executive management title relevance"
        # Low (4)
        if re.search(r'\b(sales manager|sales director|tech lead|it manager|developer)\b', norm):
            return 4, "Secondary functional title relevance"
        return 1, "Low relevance functional title"

    elif cat == "WEBSITE":
        # Very Strong (20)
        if re.search(r'\b(cto|chief technology officer|technology head|head of technology|it head|head of it|it manager|technology manager|web development manager|lead developer|engineering manager|director of engineering|vp of engineering)\b', norm):
            return 20, "Directly matched technical/web infrastructure title"
        # Strong (15)
        if re.search(r'\b(digital marketing manager|head of digital marketing|marketing manager|webmaster|product manager|frontend developer|software engineer)\b', norm):
            return 15, "Strong title relevance to web channels"
        # Moderate (10)
        if re.search(r'\b(ceo|founder|co founder|co-founder|owner|managing director|president)\b', norm):
            return 10, "Executive management title relevance"
        # Low (4)
        if re.search(r'\b(sales manager|finance manager|operations manager)\b', norm):
            return 4, "Secondary operational title relevance"
        return 1, "Low relevance functional title"

    elif cat == "SALES":
        # Very Strong (20)
        if re.search(r'\b(sales director|head of sales|vp of sales|sales manager|chief revenue officer|cro|director of sales|business development manager|head of business development)\b', norm):
            return 20, "Directly matched sales leadership title"
        # Strong (15)
        if re.search(r'\b(marketing manager|head of marketing|digital marketing manager|account executive|sales executive|business development)\b', norm):
            return 15, "Strong title relevance to revenue generation"
        # Moderate (10)
        if re.search(r'\b(ceo|founder|co founder|co-founder|owner|managing director|president)\b', norm):
            return 10, "Executive management title relevance"
        # Low (4)
        if re.search(r'\b(it manager|tech lead|operations manager)\b', norm):
            return 4, "Secondary title relevance"
        return 1, "Low relevance functional title"

    else:
        # General / Unknown
        if re.search(r'\b(ceo|founder|co founder|owner|managing director|president|cmo|cto|cro)\b', norm):
            return 15, "General executive leadership title"
        if re.search(r'\b(director|head of|vp|manager)\b', norm):
            return 10, "General management title"
        if re.search(r'\b(executive|specialist|lead|analyst|coordinator|associate)\b', norm):
            return 4, "General operational title"
        return 1, "Unrecognized title relevance"


# ==========================================
# 3. Decision-Maker Authority (MAX 20)
# ==========================================

def score_decision_maker_authority(decision_maker_score: int) -> Tuple[int, str]:
    """
    Deterministically normalize Phase 12H-C decision_maker_score (0-100)
    into authority component (0-20).
    """
    clamped_score = max(0, min(100, decision_maker_score))
    points = int(round((clamped_score / 100.0) * 20))

    if clamped_score >= 80:
        reason = "High decision-making authority"
    elif clamped_score >= 60:
        reason = "Strong decision-making authority"
    elif clamped_score >= 40:
        reason = "Moderate decision-making authority"
    elif clamped_score > 0:
        reason = "Limited decision-making authority"
    else:
        reason = "No decision-making authority recorded"

    return points, reason


# ==========================================
# 4. Seniority Relevance (MAX 10)
# ==========================================

def score_seniority_relevance(seniority: SeniorityLevel) -> Tuple[int, str]:
    """
    Seniority relevance component (MAX 10).
    Leadership and Managers receive top operational implementation marks,
    followed by Executive and Individual Contributors.
    """
    mapping = {
        SeniorityLevel.LEADERSHIP: (10, "Leadership-level authority to sponsor and implement initiatives"),
        SeniorityLevel.MANAGER: (9, "Managerial role with operational execution capability"),
        SeniorityLevel.EXECUTIVE: (8, "Executive-level organizational sponsor"),
        SeniorityLevel.INDIVIDUAL_CONTRIBUTOR: (6, "Operational practitioner role"),
        SeniorityLevel.UNKNOWN: (0, "Unknown seniority level"),
    }
    return mapping.get(seniority, (0, "Unknown seniority level"))


# ==========================================
# 5. Department Relevance (MAX 10)
# ==========================================

def score_department_relevance(
    opportunity_type: Optional[str],
    department: Optional[str]
) -> Tuple[int, str]:
    if not department:
        return 0, "No department available"

    cat = normalize_opportunity_type(opportunity_type)
    dept_norm = department.strip().lower()

    if cat in ["SEO", "DIGITAL_MARKETING"]:
        if dept_norm in ["marketing", "digital marketing", "digital_marketing", "seo"]:
            return 10, "Department directly manages digital & marketing channels"
        elif dept_norm in ["sales"]:
            return 6, "Commercial sales department alignment"
        elif dept_norm in ["executive"]:
            return 6, "Executive management department"
        elif dept_norm in ["technology", "it"]:
            return 4, "Technical infrastructure department"
        else:
            return 2, "Indirect operational department"

    elif cat == "WEBSITE":
        if dept_norm in ["technology", "it"]:
            return 10, "Department directly owns web & technical infrastructure"
        elif dept_norm in ["marketing", "digital marketing", "digital_marketing"]:
            return 8, "Department manages web presence and customer experience"
        elif dept_norm in ["executive"]:
            return 6, "Executive management department"
        elif dept_norm in ["operations", "sales"]:
            return 4, "Operational department"
        else:
            return 2, "Indirect operational department"

    elif cat == "SALES":
        if dept_norm in ["sales"]:
            return 10, "Department directly owns revenue and sales operations"
        elif dept_norm in ["marketing", "digital marketing", "digital_marketing"]:
            return 8, "Department generates pipeline and demand"
        elif dept_norm in ["executive"]:
            return 6, "Executive management department"
        elif dept_norm in ["operations", "finance"]:
            return 4, "Operational department"
        else:
            return 2, "Indirect operational department"

    else:
        # General / Unknown
        if dept_norm in ["marketing", "sales", "executive"]:
            return 8, "Core commercial department"
        elif dept_norm in ["technology", "it", "operations", "finance"]:
            return 5, "Supporting operational department"
        else:
            return 2, "General department"


# ==========================================
# 6. Contact Completeness (MAX 10)
# ==========================================

def score_contact_completeness(
    has_email: bool,
    has_phone: bool,
    has_linkedin: bool
) -> Tuple[int, Dict[str, int], str]:
    email_pts = 4 if has_email else 0
    phone_pts = 4 if has_phone else 0
    linkedin_pts = 2 if has_linkedin else 0
    total = email_pts + phone_pts + linkedin_pts

    parts = []
    if has_email:
        parts.append("email")
    if has_phone:
        parts.append("phone")
    if has_linkedin:
        parts.append("LinkedIn")

    if parts:
        reason = f"Contact coordinates available: {', '.join(parts)}"
    else:
        reason = "No direct contact coordinates available"

    details = {
        "email": email_pts,
        "phone": phone_pts,
        "linkedin": linkedin_pts,
        "points": total
    }
    return total, details, reason


# ==========================================
# 7. Opportunity - Contact Matcher
# ==========================================

def match_opportunity_contact(
    opportunity: Any,
    contact_lead: Any,
    contact_profile: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Pure deterministic function calculating the match between a single Sales Opportunity
    and a candidate Contact (Lead).
    """
    opp_id = getattr(opportunity, "id", None)
    opp_type = getattr(opportunity, "opportunity_type", "GENERAL")

    contact_id = getattr(contact_lead, "id", None)
    job_title = getattr(contact_lead, "job_title", None)
    department = getattr(contact_lead, "department", None)
    has_email = bool(getattr(contact_lead, "email", None))
    has_phone = bool(getattr(contact_lead, "phone", None))
    has_linkedin = bool(getattr(contact_lead, "linkedin_url", None))

    # Obtain contact intelligence signals
    if contact_profile:
        dm_score = getattr(contact_profile, "decision_maker_score", 0)
        func_area = getattr(contact_profile, "functional_area", FunctionalArea.UNKNOWN)
        seniority = getattr(contact_profile, "seniority_level", SeniorityLevel.UNKNOWN)
        if not department:
            department = getattr(contact_profile, "department", None)
    else:
        # Evaluate contact intelligence on-the-fly if profile is not yet persisted
        intel = build_contact_intelligence(contact_lead)
        dm_score = intel.get("decision_maker_score", 0)
        func_area = intel.get("functional_area", FunctionalArea.UNKNOWN)
        seniority = intel.get("seniority_level", SeniorityLevel.UNKNOWN)
        if not department:
            department = intel.get("department", None)

    # 1. Functional relevance (0-30)
    func_pts, func_reason = score_functional_relevance(opp_type, func_area)

    # 2. Role / Title relevance (0-20)
    role_pts, role_reason = score_role_relevance(opp_type, job_title)

    # 3. Decision-maker authority (0-20)
    dm_pts, dm_reason = score_decision_maker_authority(dm_score)

    # 4. Seniority relevance (0-10)
    sen_pts, sen_reason = score_seniority_relevance(seniority)

    # 5. Department relevance (0-10)
    dept_pts, dept_reason = score_department_relevance(opp_type, department)

    # 6. Contact completeness (0-10)
    comp_pts, comp_details, comp_reason = score_contact_completeness(has_email, has_phone, has_linkedin)

    # Sum components
    raw_total = func_pts + role_pts + dm_pts + sen_pts + dept_pts + comp_pts
    match_score = max(0, min(100, raw_total))
    match_level = calculate_match_level(match_score)

    evidence = {
        "functional_relevance": {
            "contact_area": func_area.value if isinstance(func_area, FunctionalArea) else str(func_area),
            "opportunity_type": opp_type,
            "points": func_pts,
            "reason": func_reason
        },
        "role_relevance": {
            "title": job_title,
            "points": role_pts,
            "reason": role_reason
        },
        "decision_maker": {
            "score": dm_score,
            "points": dm_pts,
            "reason": dm_reason
        },
        "seniority": {
            "value": seniority.value if isinstance(seniority, SeniorityLevel) else str(seniority),
            "points": sen_pts,
            "reason": sen_reason
        },
        "department": {
            "value": department,
            "points": dept_pts,
            "reason": dept_reason
        },
        "completeness": {
            **comp_details,
            "reason": comp_reason
        },
        "total": match_score
    }

    match_reasons = [
        f"{func_reason} ({func_pts} pts)",
        f"{role_reason} ({role_pts} pts)",
        f"{dm_reason} ({dm_pts} pts)",
        f"{sen_reason} ({sen_pts} pts)",
        f"{dept_reason} ({dept_pts} pts)",
        f"{comp_reason} ({comp_pts} pts)"
    ]

    return {
        "opportunity_id": opp_id,
        "contact_id": contact_id,
        "match_score": match_score,
        "match_level": match_level.value,
        "decision_maker_score": dm_score,
        "functional_relevance_score": func_pts,
        "role_relevance_score": role_pts,
        "seniority_relevance_score": sen_pts,
        "department_relevance_score": dept_pts,
        "completeness_score": comp_pts,
        "match_reasons": match_reasons,
        "evidence": evidence
    }


def rank_opportunity_contacts(
    opportunity: Any,
    candidates: Sequence[Tuple[Any, Optional[Any]]],
    limit: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Ranks candidates for a given opportunity.
    Sort order:
    1. match_score DESC
    2. decision_maker_score DESC
    3. str(contact_id) ASC (deterministic tie-breaker)
    """
    results = []
    for contact_lead, contact_profile in candidates:
        match_res = match_opportunity_contact(opportunity, contact_lead, contact_profile)
        results.append(match_res)

    # Sort deterministically
    sorted_results = sorted(
        results,
        key=lambda item: (
            -item["match_score"],
            -item["decision_maker_score"],
            str(item["contact_id"] or "")
        )
    )

    if limit is not None and limit > 0:
        return sorted_results[:limit]
    return sorted_results

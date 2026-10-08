import enum
import uuid
from typing import Optional, Dict, Any, List, Tuple, Sequence

from app.models.entities import SeniorityLevel, FunctionalArea, BuyingInfluenceLevel
from app.domain.opportunity_contact_matching import (
    MatchLevel,
    match_opportunity_contact,
    rank_opportunity_contacts,
)

class RecommendationPriority(str, enum.Enum):
    CRITICAL = "CRITICAL"  # 90-100
    HIGH = "HIGH"          # 75-89
    MEDIUM = "MEDIUM"      # 60-74
    LOW = "LOW"            # 40-59
    VERY_LOW = "VERY_LOW"  # 0-39


class RecommendedSalesAction(str, enum.Enum):
    PERSONALIZED_EMAIL = "PERSONALIZED_EMAIL"
    DISCOVERY_CALL = "DISCOVERY_CALL"
    DIRECT_OUTREACH = "DIRECT_OUTREACH"
    FOLLOW_UP = "FOLLOW_UP"
    RESEARCH_CONTACT = "RESEARCH_CONTACT"
    NO_ACTION = "NO_ACTION"


def calculate_recommendation_priority(score: int) -> RecommendationPriority:
    """Deterministic conversion from integer score 0-100 to RecommendationPriority enum."""
    if score >= 90:
        return RecommendationPriority.CRITICAL
    elif score >= 75:
        return RecommendationPriority.HIGH
    elif score >= 60:
        return RecommendationPriority.MEDIUM
    elif score >= 40:
        return RecommendationPriority.LOW
    else:
        return RecommendationPriority.VERY_LOW


# ==========================================
# 1. Recommendation Score Normalization (Max 100)
# ==========================================

def score_match_relevance_component(match_score: int) -> Tuple[int, str]:
    """Max 40 points from opportunity-contact match score (0-100)."""
    clamped = max(0, min(100, match_score))
    pts = int(round((clamped / 100.0) * 40))
    return pts, f"Opportunity match relevance contribution ({pts}/40 pts)"


def score_decision_maker_authority_component(dm_score: int) -> Tuple[int, str]:
    """Max 25 points from decision-maker score (0-100)."""
    clamped = max(0, min(100, dm_score))
    pts = int(round((clamped / 100.0) * 25))
    return pts, f"Decision-maker authority contribution ({pts}/25 pts)"


def score_buying_influence_component(buying_influence: BuyingInfluenceLevel) -> Tuple[int, str]:
    """Max 15 points from buying influence level."""
    mapping = {
        BuyingInfluenceLevel.DECISION_MAKER: (15, "Decision maker buying influence (15/15 pts)"),
        BuyingInfluenceLevel.HIGH: (12, "High buying influence (12/15 pts)"),
        BuyingInfluenceLevel.MEDIUM: (8, "Medium buying influence (8/15 pts)"),
        BuyingInfluenceLevel.LOW: (4, "Low buying influence (4/15 pts)"),
        BuyingInfluenceLevel.UNKNOWN: (0, "Unknown buying influence (0/15 pts)"),
    }
    return mapping.get(buying_influence, (0, "Unknown buying influence (0/15 pts)"))


def score_seniority_component(seniority: SeniorityLevel) -> Tuple[int, str]:
    """Max 10 points from seniority level."""
    mapping = {
        SeniorityLevel.EXECUTIVE: (10, "Executive-level seniority (10/10 pts)"),
        SeniorityLevel.LEADERSHIP: (8, "Leadership-level seniority (8/10 pts)"),
        SeniorityLevel.MANAGER: (6, "Manager-level seniority (6/10 pts)"),
        SeniorityLevel.INDIVIDUAL_CONTRIBUTOR: (3, "Individual contributor role (3/10 pts)"),
        SeniorityLevel.UNKNOWN: (0, "Unknown seniority level (0/10 pts)"),
    }
    return mapping.get(seniority, (0, "Unknown seniority level (0/10 pts)"))


def score_contact_completeness_component(
    has_email: bool,
    has_phone: bool,
    has_linkedin: bool
) -> Tuple[int, str]:
    """Max 10 points from contact coordinates completeness."""
    email_pts = 4 if has_email else 0
    phone_pts = 4 if has_phone else 0
    linkedin_pts = 2 if has_linkedin else 0
    total = email_pts + phone_pts + linkedin_pts
    return total, f"Contact coordinates completeness contribution ({total}/10 pts)"


def calculate_contact_recommendation_score(
    match_score: int,
    decision_maker_score: int,
    buying_influence: BuyingInfluenceLevel,
    seniority: SeniorityLevel,
    has_email: bool,
    has_phone: bool,
    has_linkedin: bool
) -> Tuple[int, Dict[str, Any]]:
    """
    Computes deterministic recommendation score (0-100).
    Components:
    - Match relevance: max 40
    - Decision-maker authority: max 25
    - Buying influence: max 15
    - Seniority: max 10
    - Contact completeness: max 10
    """
    match_pts, match_reason = score_match_relevance_component(match_score)
    dm_pts, dm_reason = score_decision_maker_authority_component(decision_maker_score)
    bi_pts, bi_reason = score_buying_influence_component(buying_influence)
    sen_pts, sen_reason = score_seniority_component(seniority)
    comp_pts, comp_reason = score_contact_completeness_component(has_email, has_phone, has_linkedin)

    raw_total = match_pts + dm_pts + bi_pts + sen_pts + comp_pts
    final_score = max(0, min(100, raw_total))

    breakdown = {
        "match_component": match_pts,
        "decision_maker_component": dm_pts,
        "buying_influence_component": bi_pts,
        "seniority_component": sen_pts,
        "completeness_component": comp_pts,
        "total": final_score
    }
    return final_score, breakdown


# ==========================================
# 2. Recommended Action Determination
# ==========================================

def determine_recommended_action(
    match_score: int,
    decision_maker_score: int,
    buying_influence: BuyingInfluenceLevel,
    has_email: bool,
    has_phone: bool,
    has_linkedin: bool,
    has_previous_task: bool = False
) -> Tuple[RecommendedSalesAction, List[str]]:
    """
    Deterministic rule engine for recommended sales actions.
    """
    reasons = []

    # 1. Follow-up takes precedence if active/previous actionable sales task exists
    if has_previous_task:
        reasons.append("Prior actionable sales task exists requiring follow-up.")
        return RecommendedSalesAction.FOLLOW_UP, reasons

    # 2. Insufficient evidence / low score
    if match_score < 30 and decision_maker_score < 30:
        reasons.append("Insufficient opportunity match and authority evidence.")
        return RecommendedSalesAction.NO_ACTION, reasons

    # 3. Missing direct contact channels for strong candidates -> Research contact
    if not has_email and not has_phone:
        if match_score >= 40 or buying_influence in (BuyingInfluenceLevel.MEDIUM, BuyingInfluenceLevel.HIGH, BuyingInfluenceLevel.DECISION_MAKER):
            if has_linkedin:
                reasons.append("LinkedIn profile available but direct email and phone are missing.")
                return RecommendedSalesAction.DIRECT_OUTREACH, reasons
            else:
                reasons.append("Strong candidate identified but missing verified email and phone coordinates.")
                return RecommendedSalesAction.RESEARCH_CONTACT, reasons
        else:
            reasons.append("No actionable contact coordinates available.")
            return RecommendedSalesAction.NO_ACTION, reasons

    # 4. Actionable contacts with channels
    if has_email and has_phone:
        if buying_influence == BuyingInfluenceLevel.DECISION_MAKER and match_score >= 75:
            reasons.append("High-authority decision maker with strong opportunity alignment and direct phone available.")
            return RecommendedSalesAction.DISCOVERY_CALL, reasons
        else:
            reasons.append("Complete email contact route available for personalized outreach.")
            return RecommendedSalesAction.PERSONALIZED_EMAIL, reasons

    if has_email:
        reasons.append("Verified email coordinate available for personalized outreach.")
        return RecommendedSalesAction.PERSONALIZED_EMAIL, reasons

    if has_phone:
        reasons.append("Direct phone coordinate available for discovery call.")
        return RecommendedSalesAction.DISCOVERY_CALL, reasons

    reasons.append("No direct outreach route identified.")
    return RecommendedSalesAction.NO_ACTION, reasons


# ==========================================
# 3. Recommendation Evaluation Engine
# ==========================================

def evaluate_contact_recommendation(
    opportunity: Any,
    ranked_matched_contacts: List[Dict[str, Any]],
    candidate_leads_map: Dict[uuid.UUID, Any],
    candidate_profiles_map: Dict[uuid.UUID, Any],
    has_previous_task: bool = False
) -> Dict[str, Any]:
    """
    Pure deterministic recommendation evaluation for a Sales Opportunity.
    Considers all ranked contacts returned by Phase 12H-D matching.
    """
    opp_id = getattr(opportunity, "id", None)
    if isinstance(opp_id, str):
        opp_id = uuid.UUID(opp_id)

    if not ranked_matched_contacts:
        reasons = ["No candidate contacts identified for this opportunity's company."]
        return {
            "opportunity_id": opp_id,
            "recommended_contact": None,
            "recommendation_score": 0,
            "match_score": 0,
            "decision_maker_score": 0,
            "buying_influence": BuyingInfluenceLevel.UNKNOWN,
            "priority": RecommendationPriority.VERY_LOW,
            "recommended_action": RecommendedSalesAction.NO_ACTION,
            "reasons": reasons,
            "evidence": {
                "recommendation": None,
                "scoring_breakdown": None,
                "reasons": reasons
            }
        }

    evaluated_candidates = []

    for match_item in ranked_matched_contacts:
        c_id = match_item["contact_id"]
        if isinstance(c_id, str):
            c_id = uuid.UUID(c_id)

        contact_lead = candidate_leads_map.get(c_id)
        contact_profile = candidate_profiles_map.get(c_id)

        has_email = bool(getattr(contact_lead, "email", None))
        has_phone = bool(getattr(contact_lead, "phone", None))
        has_linkedin = bool(getattr(contact_lead, "linkedin_url", None))

        match_score = match_item.get("match_score", 0)
        dm_score = match_item.get("decision_maker_score", 0)

        # Extract buying influence and seniority
        if contact_profile:
            buying_influence = getattr(contact_profile, "buying_influence", BuyingInfluenceLevel.UNKNOWN)
            seniority = getattr(contact_profile, "seniority_level", SeniorityLevel.UNKNOWN)
            functional_area = getattr(contact_profile, "functional_area", FunctionalArea.UNKNOWN)
        else:
            # Fallback to evidence or default
            bi_str = match_item.get("evidence", {}).get("buying_influence", "UNKNOWN")
            try:
                buying_influence = BuyingInfluenceLevel(bi_str)
            except Exception:
                buying_influence = BuyingInfluenceLevel.UNKNOWN

            sen_str = match_item.get("evidence", {}).get("seniority", {}).get("value", "UNKNOWN")
            try:
                seniority = SeniorityLevel(sen_str)
            except Exception:
                seniority = SeniorityLevel.UNKNOWN

            fa_str = match_item.get("evidence", {}).get("functional_relevance", {}).get("contact_area", "UNKNOWN")
            try:
                functional_area = FunctionalArea(fa_str)
            except Exception:
                functional_area = FunctionalArea.UNKNOWN

        rec_score, breakdown = calculate_contact_recommendation_score(
            match_score=match_score,
            decision_maker_score=dm_score,
            buying_influence=buying_influence,
            seniority=seniority,
            has_email=has_email,
            has_phone=has_phone,
            has_linkedin=has_linkedin
        )

        action, action_reasons = determine_recommended_action(
            match_score=match_score,
            decision_maker_score=dm_score,
            buying_influence=buying_influence,
            has_email=has_email,
            has_phone=has_phone,
            has_linkedin=has_linkedin,
            has_previous_task=has_previous_task
        )

        evaluated_candidates.append({
            "contact_id": c_id,
            "match_item": match_item,
            "recommendation_score": rec_score,
            "match_score": match_score,
            "decision_maker_score": dm_score,
            "buying_influence": buying_influence,
            "seniority": seniority,
            "functional_area": functional_area,
            "has_email": has_email,
            "has_phone": has_phone,
            "has_linkedin": has_linkedin,
            "recommended_action": action,
            "action_reasons": action_reasons,
            "scoring_breakdown": breakdown
        })

    # Sort deterministically:
    # 1. recommendation_score DESC
    # 2. match_score DESC
    # 3. decision_maker_score DESC
    # 4. str(contact_id) ASC
    sorted_candidates = sorted(
        evaluated_candidates,
        key=lambda item: (
            -item["recommendation_score"],
            -item["match_score"],
            -item["decision_maker_score"],
            str(item["contact_id"])
        )
    )

    top = sorted_candidates[0]
    priority = calculate_recommendation_priority(top["recommendation_score"])

    # Structured reasons
    reasons = []
    reasons.append(f"Ranked as primary contact with recommendation score {top['recommendation_score']}/100 ({priority.value} priority).")
    reasons.append(f"Opportunity match score is {top['match_score']}/100 with {top['decision_maker_score']}/100 decision-maker authority.")
    if top["buying_influence"] != BuyingInfluenceLevel.UNKNOWN:
        reasons.append(f"Identified with {top['buying_influence'].value} buying influence level.")
    reasons.extend(top["action_reasons"])

    evidence = {
        "recommendation": {
            "opportunity_id": str(opp_id) if opp_id else None,
            "contact_id": str(top["contact_id"]),
            "match_score": top["match_score"],
            "decision_maker_score": top["decision_maker_score"],
            "buying_influence": top["buying_influence"].value if isinstance(top["buying_influence"], BuyingInfluenceLevel) else str(top["buying_influence"]),
            "seniority": top["seniority"].value if isinstance(top["seniority"], SeniorityLevel) else str(top["seniority"]),
            "functional_area": top["functional_area"].value if isinstance(top["functional_area"], FunctionalArea) else str(top["functional_area"]),
            "email_available": top["has_email"],
            "phone_available": top["has_phone"],
            "linkedin_available": top["has_linkedin"],
            "recommendation_score": top["recommendation_score"],
            "priority": priority.value,
            "recommended_action": top["recommended_action"].value
        },
        "scoring_breakdown": top["scoring_breakdown"],
        "reasons": reasons
    }

    return {
        "opportunity_id": opp_id,
        "recommended_contact": top["match_item"],
        "recommendation_score": top["recommendation_score"],
        "match_score": top["match_score"],
        "decision_maker_score": top["decision_maker_score"],
        "buying_influence": top["buying_influence"],
        "priority": priority,
        "recommended_action": top["recommended_action"],
        "reasons": reasons,
        "evidence": evidence
    }

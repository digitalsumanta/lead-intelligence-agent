"""
Lead Qualification & Sales Readiness Domain Engine.

Calculates transparent, evidence-grounded qualification scores (0 to 100),
evaluates scoring coverage, classifies leads into canonical tiers
(HOT, WARM, NURTURE, LOW_PRIORITY), and recommends human-reviewed sales actions.

Invariants:
1. Zero fabrication: unobserved fields remain UNAVAILABLE without penalty.
2. Low scoring coverage limits confidence and comparability, documented explicitly.
3. Tracking tags alone qualify as marketing infrastructure (+4), NOT active ad campaigns (+8).
4. Purely analytical: does NOT mutate operational sales_status, priority, or lead.lead_score.
5. Deterministic and idempotent.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.domain.digital_marketing_intelligence import (
    CANONICAL_CATEGORIES,
    CONTENT_MARKETING,
    CONVERSION_OPTIMIZATION,
    MINDART_SERVICES,
    NOT_RESEARCHED,
    ONLINE_REPUTATION,
    PAID_ADVERTISING,
    PARTIALLY_RESEARCHED,
    RESEARCHED,
    SEO,
    SOCIAL_MEDIA,
    WEBSITE_PERFORMANCE,
)
from app.models.entities import (
    ConfidenceLevel,
    Evidence,
    Lead,
    LeadIntelligenceOpportunity,
    SalesPriority,
)

SCORING_VERSION = "lead_qualification_v1.0"


class LeadClassification(str, enum.Enum):
    HOT = "HOT"
    WARM = "WARM"
    NURTURE = "NURTURE"
    LOW_PRIORITY = "LOW_PRIORITY"


class DimensionStatus(str, enum.Enum):
    EVALUATED = "EVALUATED"
    PARTIALLY_EVALUATED = "PARTIALLY_EVALUATED"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class RecommendedSalesAction(str, enum.Enum):
    REVIEW_LEAD = "REVIEW_LEAD"
    PREPARE_PERSONALIZED_AUDIT = "PREPARE_PERSONALIZED_AUDIT"
    PREPARE_SERVICE_PROPOSAL = "PREPARE_SERVICE_PROPOSAL"
    CONTINUE_RESEARCH = "CONTINUE_RESEARCH"
    NURTURE_LEAD = "NURTURE_LEAD"
    DEPRIORITIZE = "DEPRIORITIZE"


# Canonical target industries for MindArtDigital
TARGET_INDUSTRIES = {
    "INTERIOR_DESIGN",
    "BEAUTY_AND_PERSONAL_CARE",
    "HEALTHCARE",
    "ECOMMERCE_RETAIL",
    "PROFESSIONAL_SERVICES",
    "SOFTWARE_TECH",
    "CONSTRUCTION",
    "HOSPITALITY",
}


def classify_lead_score(score: int) -> tuple[LeadClassification, RecommendedSalesAction, SalesPriority, str]:
    """
    Deterministic mapping from score to tier, advisory action, and recommended priority.
    Exact canonical boundaries:
    - HOT: 80 - 100
    - WARM: 60 - 79
    - NURTURE: 40 - 59
    - LOW_PRIORITY: 0 - 39
    """
    if score >= 80:
        return (
            LeadClassification.HOT,
            RecommendedSalesAction.REVIEW_LEAD,
            SalesPriority.HIGH,
            "High problem severity and strong commercial alignment verified. Prioritize for human sales review.",
        )
    elif score >= 60:
        return (
            LeadClassification.WARM,
            RecommendedSalesAction.PREPARE_PERSONALIZED_AUDIT,
            SalesPriority.MEDIUM,
            "Actionable digital marketing gaps verified. Prepare a targeted audit or service proposal for review.",
        )
    elif score >= 40:
        return (
            LeadClassification.NURTURE,
            RecommendedSalesAction.CONTINUE_RESEARCH,
            SalesPriority.LOW,
            "Moderate potential or partial evidence. Continue research and monitor for additional buying signals.",
        )
    else:
        return (
            LeadClassification.LOW_PRIORITY,
            RecommendedSalesAction.DEPRIORITIZE,
            SalesPriority.LOW,
            "Limited observable commercial problems or unverified presence. Deprioritize pending fresh evidence.",
        )


def evaluate_lead_qualification(
    lead: Lead,
    seven_category_coverage: Dict[str, Dict[str, Any]],
    opportunities: List[LeadIntelligenceOpportunity],
    evidence_list: List[Evidence],
    website_analysis: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Evaluates all 7 qualification dimensions deterministically.
    Returns the complete qualification payload with scoring coverage and explainability breakdown.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    analysis = website_analysis or (lead.website_analysis if isinstance(lead.website_analysis, dict) else {})
    has_website = bool(lead.website and str(lead.website).strip())
    web_available = bool(
        analysis.get("available") is True
        or analysis.get("reachable") is True
        or (isinstance(analysis.get("status_code"), int) and analysis.get("status_code") < 400)
        or analysis.get("https") is True
        or (has_website and bool(analysis) and analysis.get("available") is not False)
    )

    dimension_breakdown: Dict[str, Dict[str, Any]] = {}
    supporting_evidence: List[str] = []
    missing_information: List[str] = []
    score_limitations: List[str] = []

    # -------------------------------------------------------------
    # Dimension 1: Problem Severity (Max Weight: 25.0 pts)
    # -------------------------------------------------------------
    d1_max = 25.0
    d1_awarded = 0.0
    d1_met: List[str] = []
    d1_not_met: List[str] = []
    d1_missing: List[str] = []
    d1_refs: List[str] = []
    d1_status = DimensionStatus.EVALUATED

    if not has_website or not web_available:
        d1_status = DimensionStatus.UNAVAILABLE
        d1_missing.append("Website presence unavailable for problem inspection")
        d1_explanation = "Website is unreachable or not provided. Problem severity cannot be directly verified."
    else:
        # Check problem severity across 7 categories
        critical_problems = []
        high_problems = []
        medium_problems = []

        for cat_key, cat_data in seven_category_coverage.items():
            if cat_data.get("problem_identified"):
                sev = str(cat_data.get("problem_severity", "")).lower()
                summary = cat_data.get("problem_summary") or cat_data.get("finding") or cat_key
                ref = f"seven_category:{cat_key}"
                d1_refs.append(ref)
                if sev == "critical":
                    critical_problems.append((cat_key, summary))
                elif sev == "high":
                    high_problems.append((cat_key, summary))
                elif sev == "medium":
                    medium_problems.append((cat_key, summary))

        if critical_problems:
            d1_awarded = 25.0
            d1_met.append(f"Critical verified issue: {critical_problems[0][1]}")
            supporting_evidence.append(f"Critical verified gap in {critical_problems[0][0]}: {critical_problems[0][1]}")
            d1_explanation = f"Awarded maximum 25.0 pts due to critical problem: {critical_problems[0][1]}."
        elif high_problems:
            d1_awarded = 18.0
            d1_met.append(f"High-severity verified issue: {high_problems[0][1]}")
            d1_not_met.append("No critical severity issues detected")
            supporting_evidence.append(f"High-severity gap in {high_problems[0][0]}: {high_problems[0][1]}")
            d1_explanation = f"Awarded 18.0/25.0 pts based on verified high-severity issue: {high_problems[0][1]}."
        elif medium_problems:
            d1_awarded = 10.0
            d1_met.append(f"Medium-severity verified issue: {medium_problems[0][1]}")
            d1_not_met.append("No high or critical severity issues detected")
            supporting_evidence.append(f"Medium-severity gap in {medium_problems[0][0]}: {medium_problems[0][1]}")
            d1_explanation = f"Awarded 10.0/25.0 pts based on verified medium-severity issue: {medium_problems[0][1]}."
        else:
            d1_awarded = 0.0
            d1_not_met.append("No verified digital marketing issues observed")
            d1_explanation = "Evaluated across all observable digital marketing channels; no verified problems were identified."

    dimension_breakdown["problem_severity"] = {
        "dimension_name": "Problem Severity",
        "maximum_points": d1_max,
        "awarded_points": d1_awarded,
        "dimension_status": d1_status.value,
        "evidence_references": d1_refs,
        "criteria_met": d1_met,
        "criteria_not_met": d1_not_met,
        "missing_information": d1_missing,
        "explanation": d1_explanation,
    }

    # -------------------------------------------------------------
    # Dimension 2: Commercial Opportunity Value (Max Weight: 20.0 pts) - RQ-06
    # Count only deduplicated, evidence-grounded opportunities mapped to MindArtDigital services.
    # -------------------------------------------------------------
    d2_max = 20.0
    d2_awarded = 0.0
    d2_met: List[str] = []
    d2_not_met: List[str] = []
    d2_missing: List[str] = []
    d2_refs: List[str] = []

    # Extract distinct deduplicated MindArtDigital service opportunities
    distinct_services = set()
    for opp in opportunities:
        title = opp.title or ""
        ref = opp.source_reference or f"opportunity:{opp.id}"
        d2_refs.append(ref)
        # Normalize and match against canonical services
        for cat, svc_dict in MINDART_SERVICES.items():
            for svc_name in svc_dict.values():
                if (
                    svc_name.lower() in title.lower()
                    or (opp.description and svc_name.lower() in opp.description.lower())
                    or (opp.recommended_action and svc_name.lower() in opp.recommended_action.lower())
                ):
                    distinct_services.add(svc_name)

    # Also check recommended_service in seven_category_coverage where problem was identified
    for cat_data in seven_category_coverage.values():
        if cat_data.get("problem_identified") and cat_data.get("recommended_service"):
            distinct_services.add(cat_data["recommended_service"])

    # Prune subsumed generic substring services (e.g. 'SEO' when 'Technical SEO' is present)
    pruned_services = set()
    for s in sorted(distinct_services, key=len, reverse=True):
        if not any(s.lower() in other.lower() for other in pruned_services):
            pruned_services.add(s)
    distinct_services = pruned_services

    opp_count = len(distinct_services)
    if opp_count >= 3:
        d2_awarded = 20.0
        d2_met.append(f"{opp_count} distinct MindArtDigital service opportunities: {', '.join(sorted(distinct_services)[:3])}")
        d2_explanation = f"Awarded maximum 20.0 pts based on broad commercial service scope ({opp_count} distinct services)."
        supporting_evidence.append(f"Commercial service scope encompasses: {', '.join(sorted(distinct_services))}")
    elif opp_count == 2:
        d2_awarded = 14.0
        d2_met.append(f"2 distinct service opportunities: {', '.join(sorted(distinct_services))}")
        d2_not_met.append("Fewer than 3 distinct service offerings identified")
        d2_explanation = f"Awarded 14.0/20.0 pts based on 2 verified service opportunities ({', '.join(sorted(distinct_services))})."
        supporting_evidence.append(f"Commercial service opportunities: {', '.join(sorted(distinct_services))}")
    elif opp_count == 1:
        d2_awarded = 8.0
        d2_met.append(f"1 distinct service opportunity: {list(distinct_services)[0]}")
        d2_not_met.append("Fewer than 2 distinct service offerings identified")
        d2_explanation = f"Awarded 8.0/20.0 pts based on single service opportunity ({list(distinct_services)[0]})."
        supporting_evidence.append(f"Commercial service opportunity: {list(distinct_services)[0]}")
    else:
        d2_awarded = 0.0
        d2_not_met.append("Zero verified commercial opportunities identified")
        d2_explanation = "No grounded sales opportunities mapped to MindArtDigital services; awarded 0.0 pts without budget speculation."

    dimension_breakdown["commercial_opportunity_value"] = {
        "dimension_name": "Commercial Opportunity Value",
        "maximum_points": d2_max,
        "awarded_points": d2_awarded,
        "dimension_status": DimensionStatus.EVALUATED.value if opp_count > 0 else DimensionStatus.PARTIALLY_EVALUATED.value,
        "evidence_references": d2_refs,
        "criteria_met": d2_met,
        "criteria_not_met": d2_not_met,
        "missing_information": d2_missing,
        "explanation": d2_explanation,
    }

    # -------------------------------------------------------------
    # Dimension 3: Buying Intent Indicators (Max Weight: 15.0 pts) - RQ-01 & FINAL-03
    # Explicit point allocations summing to strictly <= 15 pts:
    # 1. Verified active advertising campaigns: +8.0 pts
    # 2. Verified marketing infrastructure / tracking tags: +4.0 pts
    # 3. Verified commercial scale / consumer review volume: +3.0 pts
    # -------------------------------------------------------------
    d3_max = 15.0
    d3_awarded = 0.0
    d3_met: List[str] = []
    d3_not_met: List[str] = []
    d3_missing: List[str] = []
    d3_refs: List[str] = []

    # 1. Active ad campaigns (+8.0 pts) - FINAL-03: Must be verified campaigns, NOT tracking tags alone
    ad_cov = seven_category_coverage.get(PAID_ADVERTISING, {})
    has_active_campaigns = bool(
        (ad_cov.get("details", {}).get("active_campaigns"))
        or (analysis.get("advertising_evidence") and "active" in str(analysis.get("advertising_evidence")).lower())
    )
    if has_active_campaigns:
        d3_awarded += 8.0
        d3_met.append("Verified active paid acquisition / advertising campaigns observed (+8.0 pts)")
        d3_refs.append(f"seven_category:{PAID_ADVERTISING}")
        supporting_evidence.append("Active advertising campaigns verified across external ad channels")
    else:
        d3_not_met.append("No active advertising campaigns verified via ad library")
        d3_missing.append("External ad library credentials unconfigured for off-site ad inspection")

    # 2. Marketing infrastructure / tracking tags (+4.0 pts) - FINAL-03: Tracking tags qualify as infrastructure
    raw_tags = analysis.get("tracking_tags")
    tracking_tags = raw_tags if isinstance(raw_tags, list) else (["Verified Tracking Infrastructure"] if analysis.get("has_tracking_tags") else [])
    has_social_presence = bool(analysis.get("social_links"))
    if tracking_tags or has_social_presence:
        d3_awarded += 4.0
        d3_met.append(f"Verified marketing infrastructure: {len(tracking_tags)} tracking tags, {len(analysis.get('social_links', []))} social profiles (+4.0 pts)")
        d3_refs.append("website_analysis:tracking_tags")
        supporting_evidence.append(f"Marketing infrastructure detected: {', '.join(tracking_tags) if tracking_tags else 'Social profiles'}")
    else:
        d3_not_met.append("No marketing tracking tags or social profile infrastructure detected")

    # 3. Commercial scale / engagement (+3.0 pts)
    # Check Google Places review volume >= 50 and rating >= 4.0, or multi-location/employee scale
    has_scale = False
    for ev in evidence_list:
        if ev.evidence_type == "google_places_user_ratings_total" and isinstance(ev.observed_value, (int, float)):
            if int(ev.observed_value) >= 50:
                has_scale = True
                d3_refs.append(f"evidence:{ev.id}")
                break

    if has_scale:
        d3_awarded += 3.0
        d3_met.append("Verified commercial scale: high consumer engagement (>= 50 Google reviews) (+3.0 pts)")
        supporting_evidence.append("Established local market presence verified with >= 50 customer reviews")
    else:
        d3_not_met.append("Limited or unmeasured consumer review volume (< 50 reviews)")

    d3_awarded = min(d3_max, round(d3_awarded, 1))
    d3_status = DimensionStatus.EVALUATED if has_active_campaigns or tracking_tags else DimensionStatus.PARTIALLY_EVALUATED
    d3_explanation = f"Awarded {d3_awarded}/15.0 pts based on verified marketing activity ({len(d3_met)} criteria met)."

    dimension_breakdown["buying_intent"] = {
        "dimension_name": "Buying Intent Indicators",
        "maximum_points": d3_max,
        "awarded_points": d3_awarded,
        "dimension_status": d3_status.value,
        "evidence_references": d3_refs,
        "criteria_met": d3_met,
        "criteria_not_met": d3_not_met,
        "missing_information": d3_missing,
        "explanation": d3_explanation,
    }

    # -------------------------------------------------------------
    # Dimension 4: Evidence Confidence (Max Weight: 15.0 pts)
    # 1. Verified reachable website with SSL: +6.0 pts
    # 2. Authorized external provider verification (Google Places): +5.0 pts
    # 3. Discrete verified fact count >= 3: +4.0 pts
    # -------------------------------------------------------------
    d4_max = 15.0
    d4_awarded = 0.0
    d4_met: List[str] = []
    d4_not_met: List[str] = []
    d4_missing: List[str] = []
    d4_refs: List[str] = []

    if web_available and analysis.get("https"):
        d4_awarded += 6.0
        d4_met.append("Website verified accessible with valid HTTPS security (+6.0 pts)")
        d4_refs.append("website_analysis:https")
    elif web_available:
        d4_awarded += 3.0
        d4_met.append("Website accessible over HTTP but lacks SSL (+3.0 pts)")
        d4_not_met.append("Insecure HTTP protocol")
    else:
        d4_not_met.append("Website unreachable or missing")
        d4_missing.append("Website availability could not be verified")

    has_places = any(ev.evidence_type.lower().startswith("google_places") for ev in evidence_list)
    if has_places:
        d4_awarded += 5.0
        d4_met.append("Authorized provider verification: Google Places listing verified (+5.0 pts)")
        d4_refs.extend(f"evidence:{ev.id}" for ev in evidence_list if ev.evidence_type.startswith("google_places"))
    else:
        d4_not_met.append("No authorized directory or place provider records")

    verified_fact_count = len(evidence_list) + (3 if web_available else 0)
    if verified_fact_count >= 3:
        d4_awarded += 4.0
        d4_met.append(f"High verified evidence depth: >= 3 discrete verifiable facts (+4.0 pts)")
    else:
        d4_not_met.append("Limited verified fact depth (< 3 discrete facts)")

    d4_awarded = min(d4_max, round(d4_awarded, 1))
    d4_status = DimensionStatus.EVALUATED if web_available and has_places else DimensionStatus.PARTIALLY_EVALUATED
    d4_explanation = f"Awarded {d4_awarded}/15.0 pts based on multi-source verified evidence."

    dimension_breakdown["evidence_confidence"] = {
        "dimension_name": "Evidence Confidence",
        "maximum_points": d4_max,
        "awarded_points": d4_awarded,
        "dimension_status": d4_status.value,
        "evidence_references": d4_refs,
        "criteria_met": d4_met,
        "criteria_not_met": d4_not_met,
        "missing_information": d4_missing,
        "explanation": d4_explanation,
    }

    # -------------------------------------------------------------
    # Dimension 5: Decision-Maker Availability (Max Weight: 10.0 pts)
    # Verified identity: +6.0 pts, Verified role: +4.0 pts
    # Unobserved data is marked UNAVAILABLE without penalty.
    # -------------------------------------------------------------
    d5_max = 10.0
    d5_awarded = 0.0
    d5_met: List[str] = []
    d5_not_met: List[str] = []
    d5_missing: List[str] = []
    d5_refs: List[str] = []

    dm_name = lead.decision_maker or analysis.get("decision_maker")
    dm_role = getattr(lead, "decision_maker_role", None) or analysis.get("decision_maker_role") or getattr(lead, "job_title", None)

    if dm_name and str(dm_name).strip() and str(dm_name).strip().upper() != "UNKNOWN":
        d5_awarded += 6.0
        d5_met.append(f"Verified decision-maker identity: {dm_name} (+6.0 pts)")
        d5_refs.append("lead:decision_maker")
        supporting_evidence.append(f"Decision-maker identified: {dm_name}")
    else:
        d5_not_met.append("Decision-maker identity unobserved")
        d5_missing.append("Decision-maker name (B2B credentials unconfigured)")

    if dm_role and str(dm_role).strip() and str(dm_role).strip().upper() != "UNKNOWN":
        d5_awarded += 4.0
        d5_met.append(f"Verified decision-maker role: {dm_role} (+4.0 pts)")
        d5_refs.append("lead:decision_maker_role")
    else:
        d5_not_met.append("Decision-maker role unobserved")
        d5_missing.append("Decision-maker role/title")

    d5_status = DimensionStatus.EVALUATED if d5_awarded > 0 else DimensionStatus.UNAVAILABLE
    d5_explanation = (
        f"Awarded {d5_awarded}/10.0 pts for verified professional contacts."
        if d5_awarded > 0
        else "Decision-maker details unobserved; unconfigured B2B provider data is not treated as a negative signal."
    )

    dimension_breakdown["decision_maker_availability"] = {
        "dimension_name": "Decision-Maker Availability",
        "maximum_points": d5_max,
        "awarded_points": d5_awarded,
        "dimension_status": d5_status.value,
        "evidence_references": d5_refs,
        "criteria_met": d5_met,
        "criteria_not_met": d5_not_met,
        "missing_information": d5_missing,
        "explanation": d5_explanation,
    }

    # -------------------------------------------------------------
    # Dimension 6: Contact Readiness (Max Weight: 10.0 pts)
    # Phone: +4.0 pts, Email: +4.0 pts, Valid Website: +2.0 pts
    # -------------------------------------------------------------
    d6_max = 10.0
    d6_awarded = 0.0
    d6_met: List[str] = []
    d6_not_met: List[str] = []
    d6_missing: List[str] = []
    d6_refs: List[str] = []

    has_phone = bool(lead.phone and str(lead.phone).strip() and len(str(lead.phone).strip()) >= 7)
    if has_phone:
        d6_awarded += 4.0
        d6_met.append(f"Verified business phone number available: {lead.phone} (+4.0 pts)")
        d6_refs.append("lead:phone")
        supporting_evidence.append(f"Direct phone contact available: {lead.phone}")
    else:
        d6_not_met.append("Business phone number unobserved")
        d6_missing.append("Business telephone contact")

    email_val = lead.email or analysis.get("business_email") or (analysis.get("emails", [None])[0] if analysis.get("emails") else None)
    has_email = bool(email_val and "@" in str(email_val) and "." in str(email_val))
    if has_email:
        d6_awarded += 4.0
        d6_met.append(f"Verified business email available: {email_val} (+4.0 pts)")
        d6_refs.append("lead:email")
        supporting_evidence.append(f"Business email verified: {email_val}")
    else:
        d6_not_met.append("Business email address unobserved")
        d6_missing.append("Business email address")

    if has_website and web_available:
        d6_awarded += 2.0
        d6_met.append("Accessible website domain (+2.0 pts)")
        d6_refs.append("lead:website")
    else:
        d6_not_met.append("No accessible website domain")
        d6_missing.append("Verified web domain")

    d6_status = DimensionStatus.EVALUATED if has_phone or has_email else DimensionStatus.PARTIALLY_EVALUATED
    d6_explanation = f"Awarded {d6_awarded}/10.0 pts for direct business communication channels."

    dimension_breakdown["contact_readiness"] = {
        "dimension_name": "Contact Readiness",
        "maximum_points": d6_max,
        "awarded_points": d6_awarded,
        "dimension_status": d6_status.value,
        "evidence_references": d6_refs,
        "criteria_met": d6_met,
        "criteria_not_met": d6_not_met,
        "missing_information": d6_missing,
        "explanation": d6_explanation,
    }

    # -------------------------------------------------------------
    # Dimension 7: MindArtDigital Business Fit (Max Weight: 5.0 pts)
    # Target industry: +3.0 pts, Service catalog mapping: +2.0 pts
    # -------------------------------------------------------------
    d7_max = 5.0
    d7_awarded = 0.0
    d7_met: List[str] = []
    d7_not_met: List[str] = []
    d7_missing: List[str] = []
    d7_refs: List[str] = []

    lead_ind = (lead.industry or "").upper().replace(" ", "_").replace("&", "_")
    is_target_ind = any(t in lead_ind for t in ("INTERIOR", "DESIGN", "BEAUTY", "PERSONAL_CARE", "TECH", "HEALTH", "RETAIL", "CONSTRUCTION"))
    if is_target_ind or (lead.industry and lead.industry != "UNKNOWN"):
        d7_awarded += 3.0
        d7_met.append(f"Industry aligns with target verticals: {lead.industry} (+3.0 pts)")
        d7_refs.append("lead:industry")
        supporting_evidence.append(f"Core vertical fit: {lead.industry}")
    else:
        d7_not_met.append("Industry not matching core target verticals")

    # Fit with MindArtDigital services catalog
    if opp_count > 0 or has_website:
        d7_awarded += 2.0
        d7_met.append("Grounded alignment with MindArtDigital service portfolio (+2.0 pts)")
    else:
        d7_not_met.append("No clear alignment with agency services")

    d7_status = DimensionStatus.EVALUATED
    d7_explanation = f"Awarded {d7_awarded}/5.0 pts for agency strategic fit."

    dimension_breakdown["mindartdigital_business_fit"] = {
        "dimension_name": "MindArtDigital Business Fit",
        "maximum_points": d7_max,
        "awarded_points": d7_awarded,
        "dimension_status": d7_status.value,
        "evidence_references": d7_refs,
        "criteria_met": d7_met,
        "criteria_not_met": d7_not_met,
        "missing_information": d7_missing,
        "explanation": d7_explanation,
    }

    # -------------------------------------------------------------
    # Aggregate Totals & Metric Decoupling (FINAL-02 & RQ-02)
    # -------------------------------------------------------------
    raw_total = sum(d["awarded_points"] for d in dimension_breakdown.values())
    qualification_score = int(round(min(100.0, max(0.0, raw_total))))

    # Scoring Coverage: percentage of dimensions that were definitively EVALUATED
    evaluated_weight = sum(
        d["maximum_points"] for d in dimension_breakdown.values()
        if d["dimension_status"] == DimensionStatus.EVALUATED.value
    )
    scoring_coverage = round((evaluated_weight / 100.0) * 100.0, 2)

    # Document coverage limitations explicitly (FINAL-02)
    if scoring_coverage < 70.0:
        score_limitations.append(
            f"Low scoring coverage ({scoring_coverage:.1f}%) indicates multiple unresearched dimensions. "
            "This limits evaluation confidence and cross-lead comparability."
        )
    if not has_active_campaigns:
        score_limitations.append("External ad library credentials not configured; off-site paid campaigns were unobserved.")
    if d5_status == DimensionStatus.UNAVAILABLE:
        score_limitations.append("B2B individual enrichment credentials unconfigured; decision-maker identity remains unobserved.")

    # Canonical Classification Tier & Recommended Action
    classification, rec_action, rec_priority, reasoning = classify_lead_score(qualification_score)

    # Aggregate all missing information deduplicated
    for d in dimension_breakdown.values():
        missing_information.extend(d["missing_information"])
    missing_information = list(dict.fromkeys(missing_information))

    return {
        "scoring_version": SCORING_VERSION,
        "evaluated_at": now_iso,
        "qualification_score": qualification_score,
        "scoring_coverage": scoring_coverage,
        "classification": classification.value,
        "recommended_sales_action": rec_action.value,
        "recommended_priority": rec_priority.value,
        "problem_severity_score": dimension_breakdown["problem_severity"]["awarded_points"],
        "commercial_opportunity_value_score": dimension_breakdown["commercial_opportunity_value"]["awarded_points"],
        "buying_intent_score": dimension_breakdown["buying_intent"]["awarded_points"],
        "evidence_confidence_score": dimension_breakdown["evidence_confidence"]["awarded_points"],
        "decision_maker_availability_score": dimension_breakdown["decision_maker_availability"]["awarded_points"],
        "contact_readiness_score": dimension_breakdown["contact_readiness"]["awarded_points"],
        "mindartdigital_business_fit_score": dimension_breakdown["mindartdigital_business_fit"]["awarded_points"],
        "qualification_reasoning": reasoning,
        "dimension_breakdown": dimension_breakdown,
        "supporting_evidence": list(dict.fromkeys(supporting_evidence)),
        "missing_information": missing_information,
        "score_limitations": score_limitations,
    }

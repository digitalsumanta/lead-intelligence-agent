import uuid
from typing import List, Dict, Any, Tuple
from datetime import datetime
from app.models.entities import BuyingSignal, LeadIntelligenceOpportunity, SourceType, ConfidenceLevel, now_utc


def derive_opportunity_from_signals(
    lead_id: uuid.UUID, 
    signals: List[BuyingSignal]
) -> List[LeadIntelligenceOpportunity]:
    """
    Deterministically convert Buying Signals into explicit, evidence-grounded Sales Opportunities.
    Website presence and profile completeness act as supporting evidence only.
    Distinguishes confirmed opportunities from investigative opportunities.
    Does not fabricate campaign failure or assume advertising proves poor performance.
    """
    opportunities: List[LeadIntelligenceOpportunity] = []
    if not signals:
        return opportunities

    now = now_utc()

    # Segregate signal categories and types
    advertising_signals = [
        s for s in signals 
        if s.signal_category == "advertising_activity" or s.signal_type == "PAID_ACQUISITION_INVESTMENT"
    ]
    marketing_signals = [
        s for s in signals 
        if s.signal_category in ["marketing_activity", "seo_activity", "marketing_opportunity"]
        or s.signal_type in ["MARKETING_MATURITY_SIGNAL", "MARKETING_OPPORTUNITY_DETECTED", "SEO_OPPORTUNITY_DETECTED", "HIGH_IMPACT_DIGITAL_GAP"]
    ]
    website_signals = [
        s for s in signals 
        if s.signal_category == "website_activity" or s.signal_type == "WEBSITE_PRESENCE_CONFIRMED"
    ]
    company_signals = [
        s for s in signals 
        if s.signal_type == "HIGH_PROFILE_COMPLETENESS"
    ]

    # Invariant: Website and Completeness alone do not constitute a standalone sales opportunity
    if not advertising_signals and not marketing_signals:
        return opportunities

    # 1. Evaluate Advertising Signals (e.g. PAID_ACQUISITION_INVESTMENT)
    if advertising_signals:
        platforms_list = []
        notes_list = []
        for s in advertising_signals:
            ev = s.evidence if isinstance(s.evidence, dict) else {}
            if ev.get("platforms"):
                platforms_list.append(str(ev["platforms"]))
            if ev.get("notes"):
                notes_list.append(str(ev["notes"]))
            elif ev.get("evidence"):
                notes_list.append(str(ev["evidence"]))

        platforms_str = ", ".join(dict.fromkeys(platforms_list)) if platforms_list else "Digital Advertising"
        notes_str = "; ".join(dict.fromkeys(notes_list)) if notes_list else "Active ad campaigns detected"

        # Evidence-grounded score: active paid advertising indicates verified budget and commercial readiness
        ad_score = 75
        if website_signals:
            ad_score = min(100, ad_score + 10)  # 85
        if len(platforms_list) > 1 or any(s.strength and s.strength.value == "HIGH" for s in advertising_signals):
            ad_score = min(100, ad_score + 5)   # 90
        if company_signals:
            ad_score = min(100, ad_score + 5)   # 95

        ad_priority = "HIGH"
        ad_action = "AUDIT_AD_PERFORMANCE_AND_CRO"
        ad_status = "INVESTIGATE"
        recommended_svc = "Performance Marketing & Landing Page CRO"
        commercial_rel = (
            f"Verified commercial investment in paid customer acquisition across {platforms_str} "
            f"demonstrates active budget, commercial readiness, and growth focus. Opportunity exists to audit ad creative fatigue, "
            f"ROAS efficiency, and landing page conversion without disrupting performing campaigns."
        )
        suggested_step = "Audit ad creative variants, copy messaging, and landing page funnel conversion before initiating outreach."
        opp_title = "Paid Acquisition & Performance Marketing Optimization"
        opp_desc = (
            f"Active advertising investment detected across {platforms_str}. Validated ad investment "
            f"indicates commercial readiness; audit campaign efficiency, creative fatigue, and landing page conversion before outreach."
        )

        has_high_conf = any(s.confidence and s.confidence.value == "HIGH" for s in advertising_signals)
        ad_confidence = ConfidenceLevel.HIGH if has_high_conf else ConfidenceLevel.MEDIUM

        ad_evidence = {
            "opportunity_title": opp_title,
            "opportunity_category": "PAID_ADVERTISING",
            "recommended_service": recommended_svc,
            "opportunity_status": ad_status,
            "status": ad_status,
            "evidence_source": f"Ad platforms detected ({platforms_str})",
            "commercial_relevance": commercial_rel,
            "suggested_next_action": suggested_step,
            "platforms": platforms_str,
            "notes": notes_str,
            "signals": [{"type": s.signal_type, "category": s.signal_category, "detected_at": s.detected_at.isoformat()} for s in advertising_signals + website_signals],
            "advertising_count": len(advertising_signals),
            "has_website": len(website_signals) > 0,
            "is_complete": len(company_signals) > 0,
            "last_evaluated": now.isoformat(),
        }

        opportunities.append(
            LeadIntelligenceOpportunity(
                id=uuid.uuid4(),
                lead_id=lead_id,
                title=opp_title,
                description=opp_desc,
                opportunity_type="PAID_ADVERTISING",
                score=ad_score,
                priority=ad_priority,
                recommended_action=ad_action,
                evidence=ad_evidence,
                source_type=SourceType.SYSTEM_DERIVED,
                source_reference="buying_signals.paid_acquisition",
                confidence=ad_confidence,
                created_at=now,
                updated_at=now
            )
        )

    # 2. Evaluate Marketing & SEO Signals
    if marketing_signals:
        combined_signals = marketing_signals + website_signals + company_signals
        mkt_score = min(100, len(marketing_signals) * 25)
        if website_signals:
            mkt_score = min(100, mkt_score + 10)
        if company_signals:
            mkt_score = min(100, mkt_score + 20)

        if mkt_score >= 75:
            mkt_action = "PREPARE_OUTREACH"
            mkt_priority = "HIGH"
        elif mkt_score >= 50:
            mkt_action = "REVIEW_MARKETING"
            mkt_priority = "HIGH"
        else:
            mkt_action = "RESEARCH_BEFORE_CONTACT"
            mkt_priority = "MEDIUM"

        has_gap = any(s.signal_type == "HIGH_IMPACT_DIGITAL_GAP" for s in marketing_signals)
        mkt_status = "QUALIFIED" if has_gap else "ACTIVE"
        mkt_svc = "SEO & Full-Funnel Digital Strategy" if has_gap else "Digital Marketing & SEO Strategy"
        mkt_commercial_rel = (
            "Detected actionable gaps or maturity changes in digital marketing presence, supported by profile data."
        )

        mkt_evidence = {
            "opportunity_title": f"Digital Marketing & SEO Enhancement ({len(marketing_signals)} signals)",
            "opportunity_category": "DIGITAL_MARKETING",
            "recommended_service": mkt_svc,
            "opportunity_status": mkt_status,
            "status": mkt_status,
            "evidence_source": "buying_signals",
            "commercial_relevance": mkt_commercial_rel,
            "suggested_next_action": mkt_action.replace('_', ' ').title(),
            "signals": [{"type": s.signal_type, "detected_at": s.detected_at.isoformat()} for s in combined_signals],
            "marketing_count": len(marketing_signals),
            "has_website": len(website_signals) > 0,
            "is_complete": len(company_signals) > 0,
            "last_evaluated": now.isoformat(),
        }

        opportunities.append(
            LeadIntelligenceOpportunity(
                id=uuid.uuid4(),
                lead_id=lead_id,
                title=f"Digital Marketing & SEO Enhancement ({len(marketing_signals)} signals)",
                description="Detected actionable gaps or maturity changes in digital marketing presence, supported by profile data.",
                opportunity_type="DIGITAL_MARKETING",
                score=mkt_score,
                priority=mkt_priority,
                recommended_action=mkt_action,
                evidence=mkt_evidence,
                source_type=SourceType.SYSTEM_DERIVED,
                source_reference="buying_signals.marketing_combo",
                confidence=ConfidenceLevel.HIGH,
                created_at=now,
                updated_at=now
            )
        )

    return sort_opportunities_by_score(opportunities)


def sort_opportunities_by_score(opportunities: List[LeadIntelligenceOpportunity]) -> List[LeadIntelligenceOpportunity]:
    """Sort descending by score."""
    return sorted(opportunities, key=lambda o: (o.score or 0), reverse=True)

import uuid
from typing import List, Dict, Any, Tuple
from datetime import datetime
from app.models.entities import BuyingSignal, LeadIntelligenceOpportunity, SourceType, ConfidenceLevel, now_utc

def derive_opportunity_from_signals(
    lead_id: uuid.UUID, 
    signals: List[BuyingSignal]
) -> List[LeadIntelligenceOpportunity]:
    """
    Deterministically convert Buying Signals into explicit Sales Opportunities.
    Website presence and profile completeness act as supporting evidence only.
    """
    opportunities = []
    if not signals:
        return opportunities

    now = now_utc()

    marketing_signals = [s for s in signals if s.signal_category in ["marketing_activity", "seo_activity"]]
    website_signals = [s for s in signals if s.signal_category == "website_activity"]
    company_signals = [s for s in signals if s.signal_type == "HIGH_PROFILE_COMPLETENESS"]

    if not marketing_signals:
        # Website and Completeness alone do not constitute a standalone sales opportunity
        return opportunities

    # Combine evidence
    combined_signals = marketing_signals + website_signals + company_signals
    evidence = {
        "signals": [{"type": s.signal_type, "detected_at": s.detected_at.isoformat()} for s in combined_signals],
        "marketing_count": len(marketing_signals),
        "has_website": len(website_signals) > 0,
        "is_complete": len(company_signals) > 0
    }

    # Calculate score based on primary and supporting evidence
    score = min(100, len(marketing_signals) * 25)
    if website_signals:
        score = min(100, score + 10)
    if company_signals:
        score = min(100, score + 20)

    # Derive action
    if score >= 75:
        action = "PREPARE_OUTREACH"
        priority = "HIGH"
    elif score >= 50:
        action = "REVIEW_MARKETING"
        priority = "HIGH"
    else:
        action = "RESEARCH_BEFORE_CONTACT"
        priority = "MEDIUM"

    opportunities.append(
        LeadIntelligenceOpportunity(
            id=uuid.uuid4(),
            lead_id=lead_id,
            title=f"Digital Marketing & SEO Enhancement ({len(marketing_signals)} signals)",
            description="Detected actionable gaps or maturity changes in digital marketing presence, supported by profile data.",
            opportunity_type="DIGITAL_MARKETING",
            score=score,
            priority=priority,
            recommended_action=action,
            evidence=evidence,
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

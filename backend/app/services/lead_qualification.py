"""
Lead Qualification & Sales Readiness Service.

Orchestrates deterministic lead qualification, non-destructive persistence under
lead.score_factors['qualification'], and retrieval.

CRITICAL INVARIANTS:
1. NEVER writes to lead.lead_score, even when None (FINAL-01).
2. NEVER mutates lead.sales_status (RQ-04).
3. NEVER mutates lead.priority (RQ-05).
4. Preserves all existing keys in lead.score_factors (RQ-08).
5. Qualification score and scoring coverage are separate metrics (FINAL-02).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.lead_qualification import (
    SCORING_VERSION,
    evaluate_lead_qualification,
)
from app.models.entities import (
    DigitalMarketingIntelligenceProfile,
    Evidence,
    Lead,
    LeadIntelligenceOpportunity,
)
from app.schemas.lead_qualification import (
    DimensionScoreExplanation,
    LeadQualificationResponse,
)

logger = logging.getLogger("lead_agent.lead_qualification")


async def qualify_lead(session: AsyncSession, lead_id: UUID) -> LeadQualificationResponse:
    """
    Executes deterministic lead qualification for a lead, stores the result
    under lead.score_factors['qualification'], and commits the transaction.
    """
    lead = await session.get(Lead, lead_id)
    if not lead:
        raise ValueError(f"Lead {lead_id} not found")

    # Load 7-category digital marketing coverage
    digital_profile = await session.scalar(
        select(DigitalMarketingIntelligenceProfile).where(
            DigitalMarketingIntelligenceProfile.company_id == lead_id
        )
    )
    coverage = digital_profile.seven_category_coverage if digital_profile else {}

    # Load deduplicated canonical opportunities
    opportunities = list((await session.scalars(
        select(LeadIntelligenceOpportunity).where(
            LeadIntelligenceOpportunity.lead_id == lead_id
        ).order_by(LeadIntelligenceOpportunity.created_at)
    )).all())

    # Load evidence
    evidence_list = list((await session.scalars(
        select(Evidence).where(Evidence.lead_id == lead_id).order_by(Evidence.observed_at.desc())
    )).all())

    website_analysis = lead.website_analysis if isinstance(lead.website_analysis, dict) else {}

    # Execute deterministic domain evaluation
    result_dict = evaluate_lead_qualification(
        lead=lead,
        seven_category_coverage=coverage,
        opportunities=opportunities,
        evidence_list=evidence_list,
        website_analysis=website_analysis,
    )

    # Non-destructive persistence under lead.score_factors['qualification'] (FINAL-01 & RQ-08)
    # Strictly preserves all existing unrelated keys (e.g. 'components', 'https', 'website_quality')
    current_factors = dict(lead.score_factors or {})
    current_factors["qualification"] = result_dict
    lead.score_factors = current_factors

    # CRITICAL INVARIANT VERIFICATIONS:
    # 1. Do NOT modify lead.lead_score (FINAL-01)
    # 2. Do NOT modify lead.sales_status (RQ-04)
    # 3. Do NOT modify lead.priority (RQ-05)

    await session.commit()
    await session.refresh(lead)

    return _build_response_schema(lead_id, result_dict)


async def get_lead_qualification(
    session: AsyncSession,
    lead_id: UUID,
    auto_compute_if_missing: bool = True,
) -> Optional[LeadQualificationResponse]:
    """
    Retrieves existing qualification from lead.score_factors['qualification'].
    If missing and auto_compute_if_missing is True, calculates and persists it.
    """
    lead = await session.get(Lead, lead_id)
    if not lead:
        return None

    cached = (lead.score_factors or {}).get("qualification") if isinstance(lead.score_factors, dict) else None
    if cached and isinstance(cached, dict):
        return _build_response_schema(lead_id, cached)

    if auto_compute_if_missing:
        return await qualify_lead(session, lead_id)

    return None


def _build_response_schema(lead_id: UUID, data: Dict[str, Any]) -> LeadQualificationResponse:
    dims = {
        name: DimensionScoreExplanation.model_validate(dim_data)
        for name, dim_data in data.get("dimension_breakdown", {}).items()
    }

    eval_at = data.get("evaluated_at")
    if isinstance(eval_at, str):
        try:
            evaluated_datetime = datetime.fromisoformat(eval_at)
        except Exception:
            evaluated_datetime = datetime.now(timezone.utc)
    elif isinstance(eval_at, datetime):
        evaluated_datetime = eval_at
    else:
        evaluated_datetime = datetime.now(timezone.utc)

    return LeadQualificationResponse(
        lead_id=lead_id,
        qualification_score=data.get("qualification_score", 0),
        scoring_coverage=data.get("scoring_coverage", 0.0),
        classification=data.get("classification", "LOW_PRIORITY"),
        recommended_sales_action=data.get("recommended_sales_action", "DEPRIORITIZE"),
        recommended_priority=data.get("recommended_priority", "LOW"),
        problem_severity_score=float(data.get("problem_severity_score", 0.0)),
        commercial_opportunity_value_score=float(data.get("commercial_opportunity_value_score", 0.0)),
        buying_intent_score=float(data.get("buying_intent_score", 0.0)),
        evidence_confidence_score=float(data.get("evidence_confidence_score", 0.0)),
        decision_maker_availability_score=float(data.get("decision_maker_availability_score", 0.0)),
        contact_readiness_score=float(data.get("contact_readiness_score", 0.0)),
        mindartdigital_business_fit_score=float(data.get("mindartdigital_business_fit_score", 0.0)),
        dimension_breakdown=dims,
        qualification_reasoning=data.get("qualification_reasoning", ""),
        supporting_evidence=data.get("supporting_evidence", []),
        missing_information=data.get("missing_information", []),
        score_limitations=data.get("score_limitations", []),
        scoring_version=data.get("scoring_version", SCORING_VERSION),
        evaluated_at=evaluated_datetime,
    )

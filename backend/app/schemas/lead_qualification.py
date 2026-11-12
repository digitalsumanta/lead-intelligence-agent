"""Pydantic schemas for Lead Qualification & Sales Readiness Engine."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DimensionScoreExplanation(BaseModel):
    dimension_name: str
    maximum_points: float
    awarded_points: float
    dimension_status: str
    evidence_references: List[str] = Field(default_factory=list)
    criteria_met: List[str] = Field(default_factory=list)
    criteria_not_met: List[str] = Field(default_factory=list)
    missing_information: List[str] = Field(default_factory=list)
    explanation: str

    model_config = ConfigDict(from_attributes=True)


class LeadQualificationResponse(BaseModel):
    lead_id: UUID
    qualification_score: int
    scoring_coverage: float
    classification: str
    recommended_sales_action: str
    recommended_priority: str

    problem_severity_score: float
    commercial_opportunity_value_score: float
    buying_intent_score: float
    evidence_confidence_score: float
    decision_maker_availability_score: float
    contact_readiness_score: float
    mindartdigital_business_fit_score: float

    dimension_breakdown: Dict[str, DimensionScoreExplanation]
    qualification_reasoning: str
    supporting_evidence: List[str] = Field(default_factory=list)
    missing_information: List[str] = Field(default_factory=list)
    score_limitations: List[str] = Field(default_factory=list)

    scoring_version: str
    evaluated_at: datetime

    model_config = ConfigDict(from_attributes=True)

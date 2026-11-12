import uuid
from typing import Optional, Dict, Any, List
from datetime import datetime
from pydantic import BaseModel
from app.domain.sales_feedback import FeedbackSentiment, AttributionLevel


class SalesFeedbackItem(BaseModel):
    task_id: uuid.UUID
    activity_id: Optional[uuid.UUID] = None
    opportunity_id: Optional[uuid.UUID] = None
    contact_id: Optional[uuid.UUID] = None
    recommended_action: Optional[str] = None
    task_type: Optional[str] = None
    outcome: Optional[str] = None
    sentiment: FeedbackSentiment
    attribution_level: AttributionLevel
    occurred_at: Optional[datetime] = None
    notes: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None


class ActionPerformanceSummary(BaseModel):
    action: str
    total_executions: int
    positive_count: int
    negative_count: int
    neutral_count: int
    unknown_count: int
    positive_rate: float
    negative_rate: float
    neutral_rate: float
    verified_executions: int = 0
    verified_positive_rate: Optional[float] = None


class SalesFeedbackSummaryResponse(BaseModel):
    scope: str
    scope_id: Optional[str] = None
    total_executions: int
    positive_count: int
    negative_count: int
    neutral_count: int
    unknown_count: int
    positive_rate: float
    negative_rate: float
    neutral_rate: float
    verified_executions: int = 0
    verified_positive_rate: Optional[float] = None
    outcome_distribution: Dict[str, int]
    action_performance: Dict[str, ActionPerformanceSummary]
    attribution_summary: Dict[str, int]
    items: List[SalesFeedbackItem]

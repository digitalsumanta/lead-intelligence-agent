import uuid
from typing import Optional, Dict, Any, List
from pydantic import BaseModel


class OrchestrationOpportunityResult(BaseModel):
    opportunity_id: uuid.UUID
    opportunity_title: str
    opportunity_type: str
    score: int
    priority: str
    status: str
    matched_contacts_count: int
    recommended_contact: Optional[Dict[str, Any]] = None
    recommendation: Optional[Dict[str, Any]] = None
    sales_task: Optional[Dict[str, Any]] = None
    feedback: Optional[Dict[str, Any]] = None
    decision_trace: List[str] = []
    error: Optional[str] = None
    failed_phase: Optional[str] = None
    error_code: Optional[str] = None
    retryable: Optional[bool] = None
    duration_ms: Optional[float] = None
    stage_durations_ms: Optional[Dict[str, float]] = None


class OrchestrationSummary(BaseModel):
    total_opportunities: int
    tasks_created: int
    tasks_reused: int
    no_contact_count: int
    no_recommendation_count: int
    failed_count: int


class LeadIntelligenceOrchestrationResponse(BaseModel):
    lead_id: uuid.UUID
    company_name: str
    status: str
    opportunities: List[OrchestrationOpportunityResult]
    summary: OrchestrationSummary
    decision_trace: List[str]
    evidence: List[Dict[str, Any]]
    duration_ms: Optional[float] = None

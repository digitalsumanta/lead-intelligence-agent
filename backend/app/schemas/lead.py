"""Lead and AI response schemas."""
from pydantic import BaseModel, ConfigDict, Field
from typing import Any, Literal
from datetime import datetime

class LeadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    company_name: str
    website: str | None = None
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    industry: str | None = None
    status: str
    sales_status: str = "NEW"
    priority: str = "MEDIUM"
    assigned_to: str | None = None
    next_follow_up_at: datetime | None = None
    lost_reason: str | None = None
    lead_score: int | None = None
    score_factors: dict[str, Any] | None = None
    ai_summary: str | None = None
    pain_points: list[str] | None = None
    keyword_opportunities: list[str] | None = None
    competitors: list[str] | None = None
    recommended_services: list[str] | None = None
    decision_maker: str | None = None
    linkedin_url: str | None = None
    website_analysis: dict[str, Any] | None = None
    ai_analysis: dict[str, Any] | None = None
    sales_opening: str | None = None
    audit_video_script: str | None = None
    proposed_package: str | None = None
    confidence_score: float | None = None
    intelligence_status: Literal["available", "unavailable"] = "unavailable"
    intelligence_score: int | None = None
    opportunity_count: int = 0
    recommendation_count: int = 0
    personalization_available: bool = False
    latest_enrichment_at: datetime | None = None
    created_at: Any
    updated_at: Any

class AIAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_summary: str
    business_category: str
    website_problems: list[str] = Field(default_factory=list)
    seo_opportunities: list[str] = Field(default_factory=list)
    digital_marketing_opportunities: list[str] = Field(default_factory=list)
    pain_points: list[str] = Field(default_factory=list)
    recommended_services: list[str] = Field(default_factory=list)
    suggested_keywords: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)
    decision_maker: str | None = None
    personalized_sales_opening: str
    audit_video_script: str
    proposed_package: str
    confidence_score: float = Field(ge=0, le=1)

class ProcessBatchIn(BaseModel):
    batch_size: int = Field(default=10, ge=1, le=100)
    max_concurrency: int = Field(default=2, ge=1, le=10)
    provider: str | None = None
    model: str | None = None

class ErrorEnvelope(BaseModel):
    success: bool = False
    error: dict[str, Any]

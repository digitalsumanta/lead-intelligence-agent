"""Schemas for MindArtDigital Business Research and Lead Discovery Engine."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4
from pydantic import BaseModel, Field


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResearchInput(BaseModel):
    industry: str = Field(..., min_length=1, max_length=160, description="Target business industry")
    location: str = Field(..., min_length=1, max_length=120, description="Target geographic area")
    sub_category: str | None = Field(default=None, max_length=160, description="Optional narrower category")
    target_business_count: int = Field(default=50, ge=1, le=500, description="Maximum businesses to research")
    research_depth: Literal["basic", "standard", "deep"] = Field(default="standard")


class SourceEvidence(BaseModel):
    source_url: str | None = None
    observation_date: str = Field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    evidence_type: str = "web_observation"
    verified_facts: list[str] = Field(default_factory=list)
    inferred_opportunities: list[str] = Field(default_factory=list)
    unavailable_fields: list[str] = Field(default_factory=list)
    raw_details: dict[str, Any] = Field(default_factory=dict)


class ResearchResultItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    company_name: str
    website: str | None = None
    industry: str | None = None
    sub_category: str | None = None
    country: str | None = None
    city: str | None = None
    decision_maker: str | None = None
    decision_maker_role: str | None = None
    business_email: str | None = None
    phone: str | None = None
    linkedin: str | None = None
    advertising_platforms: str | None = None
    advertising_evidence: str | None = None
    primary_problem: str | None = None
    recommended_mindart_service: str | None = None
    research_status: Literal["verified", "inferred", "partial", "duplicate"] = "partial"
    source_evidence: SourceEvidence | None = None
    is_duplicate: bool = False
    existing_lead_id: str | None = None
    selected: bool = True
    rejected: bool = False


class ResearchJobState(BaseModel):
    job_id: str = Field(default_factory=lambda: str(uuid4()))
    status: Literal["QUEUED", "RUNNING", "COMPLETED", "CANCELLED", "FAILED"] = "QUEUED"
    created_at: str = Field(default_factory=now_utc_iso)
    updated_at: str = Field(default_factory=now_utc_iso)
    input_params: ResearchInput
    target_count: int
    found_count: int = 0
    processed_count: int = 0
    progress_percent: float = 0.0
    error_message: str | None = None
    provider_name: str = "none"
    results: list[ResearchResultItem] = Field(default_factory=list)


class SaveLeadsRequest(BaseModel):
    job_id: str | None = None
    leads: list[ResearchResultItem]


class SaveLeadsResponse(BaseModel):
    success: bool = True
    total_submitted: int
    saved_count: int
    updated_count: int
    skipped_count: int
    lead_ids: list[str]


class ResearchProvidersStatus(BaseModel):
    active_discovery_provider: str
    is_configured: bool
    message: str
    providers: list[dict[str, Any]]

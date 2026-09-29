"""Strict contracts for deterministic, read-only SEO sales opportunities."""
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models import SalesPriority, SalesStatus, SalesTaskStatus, SalesTaskType


class SeoOpportunityContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SeoOpportunityEvidence(SeoOpportunityContract):
    finding_id: UUID
    finding_code: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=250)
    page_url: str | None = Field(default=None, max_length=2048)
    severity: Literal["INFO", "LOW", "MEDIUM", "HIGH"]
    observed_values: dict


class SeoSalesOpportunity(SeoOpportunityContract):
    opportunity_id: UUID
    lead_id: UUID
    seo_run_id: UUID
    type: Literal[
        "TECHNICAL_SEO", "ON_PAGE_SEO", "INDEXABILITY", "PERFORMANCE",
        "STRUCTURED_DATA", "LINKS", "IMAGES", "CRAWLABILITY",
    ]
    scope: Literal["SITE"]
    title: str = Field(min_length=1, max_length=250)
    summary: str = Field(min_length=1, max_length=1000)
    severity: Literal["INFO", "LOW", "MEDIUM", "HIGH"]
    affected_page_count: int = Field(ge=0)
    affected_pages: list[str] = Field(max_length=10)
    finding_count: int = Field(ge=1)
    evidence: list[SeoOpportunityEvidence] = Field(min_length=1, max_length=10)
    recommended_service_ids: list[str] = Field(max_length=5)
    suggested_task_type: SalesTaskType
    suggested_action: str = Field(min_length=1, max_length=500)
    current_sales_status: SalesStatus
    lead_priority: SalesPriority
    assigned_to: str | None
    next_follow_up_at: datetime | None
    current_stage_value: Decimal | None
    current_stage_value_field: str | None
    currency: str = Field(min_length=3, max_length=3)
    current_task_state: Literal["NO_TASK", "OPEN", "IN_PROGRESS", "COMPLETED", "CANCELLED"]
    related_task_id: UUID | None


class SeoSalesOpportunityList(SeoOpportunityContract):
    items: list[SeoSalesOpportunity] = Field(max_length=10)

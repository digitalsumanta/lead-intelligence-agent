"""Strict API contracts for human-managed sales tasks."""
from datetime import datetime, timezone
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from decimal import Decimal

from app.models import SalesActivityOutcome, SalesActivityType, SalesPriority, SalesStatus, SalesTaskStatus, SalesTaskType


class SalesTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assigned_to: str | None = Field(default=None, max_length=120)
    task_type: SalesTaskType
    title: str = Field(min_length=1, max_length=250)
    description: str | None = Field(default=None, max_length=10000)
    due_at: datetime
    priority: SalesPriority = SalesPriority.MEDIUM
    related_activity_id: UUID | None = None
    related_outreach_artifact_id: UUID | None = None

    @field_validator("due_at")
    @classmethod
    def require_timezone(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("due_at must include a timezone")
        return value.astimezone(timezone.utc)

    @field_validator("title", "assigned_to", "description")
    @classmethod
    def normalize_text(cls, value):
        if value is None:
            return None
        value = value.strip()
        if not value and isinstance(value, str):
            return None
        return value


class SalesTaskPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assigned_to: str | None = Field(default=None, max_length=120)
    task_type: SalesTaskType | None = None
    title: str | None = Field(default=None, min_length=1, max_length=250)
    description: str | None = Field(default=None, max_length=10000)
    due_at: datetime | None = None
    priority: SalesPriority | None = None
    status: SalesTaskStatus | None = None
    related_activity_id: UUID | None = None
    related_outreach_artifact_id: UUID | None = None

    @field_validator("due_at")
    @classmethod
    def require_timezone(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("due_at must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None

    @field_validator("title", "assigned_to", "description")
    @classmethod
    def normalize_text(cls, value):
        if value is None:
            return None
        value = value.strip()
        return value or None


class SalesTaskOut(BaseModel):
    id: UUID
    lead_id: UUID
    assigned_to: str | None
    task_type: SalesTaskType
    title: str
    description: str | None
    due_at: datetime
    priority: SalesPriority
    status: SalesTaskStatus
    related_activity_id: UUID | None
    related_outreach_artifact_id: UUID | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    is_due: bool
    is_overdue: bool
    lead_company_name: str | None = None


class SalesActionTaskOut(SalesTaskOut):
    lead_sales_status: SalesStatus
    lead_priority: SalesPriority
    lead_assigned_to: str | None
    next_follow_up_at: datetime | None
    current_stage_value: Decimal | None
    current_stage_value_field: str | None
    currency: str


class OutcomeSuggestionOut(BaseModel):
    activity_id: UUID
    lead_id: UUID
    lead_company_name: str
    activity_type: SalesActivityType
    outcome: SalesActivityOutcome
    occurred_at: datetime
    suggested_task_type: SalesTaskType
    lead_sales_status: SalesStatus
    lead_priority: SalesPriority
    lead_assigned_to: str | None
    next_follow_up_at: datetime | None
    current_stage_value: Decimal | None
    current_stage_value_field: str | None
    currency: str


class SalesTaskSummaryOut(BaseModel):
    today: list[SalesActionTaskOut]
    overdue: list[SalesActionTaskOut]
    upcoming: list[SalesActionTaskOut]
    outcome_suggestions: list[OutcomeSuggestionOut]

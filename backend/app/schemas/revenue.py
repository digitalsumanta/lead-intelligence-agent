"""Strict monetary, collection, target, and revenue analytics contracts."""
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import SalesStatus


SUPPORTED_CURRENCIES = frozenset({"INR", "USD", "EUR", "GBP", "AED", "AUD", "CAD", "SGD", "JPY", "CHF", "CNY", "NZD", "ZAR"})


def validate_currency(value: str) -> str:
    value = value.strip().upper()
    if value not in SUPPORTED_CURRENCIES:
        raise ValueError("currency must be a supported ISO 4217 code")
    return value


class CommercialPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    estimated_value: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    proposal_value: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    negotiation_value: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    won_value: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    expected_collection: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    currency: str | None = None

    @field_validator("currency")
    @classmethod
    def currency_code(cls, value):
        return validate_currency(value) if value is not None else None


class CommercialOut(BaseModel):
    lead_id: UUID
    sales_status: SalesStatus
    estimated_value: Decimal | None
    proposal_value: Decimal | None
    negotiation_value: Decimal | None
    won_value: Decimal | None
    expected_collection: Decimal | None
    actual_collected: Decimal
    outstanding_collection: Decimal
    currency: str


class SalesCollectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    collection_date: datetime
    payment_reference: str | None = Field(default=None, max_length=250)
    notes: str | None = Field(default=None, max_length=5000)

    @field_validator("collection_date")
    @classmethod
    def require_timezone(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("collection_date must include a timezone")
        return value

    @field_validator("payment_reference", "notes")
    @classmethod
    def clean_optional_text(cls, value):
        return value.strip() or None if value is not None else None


class SalesCollectionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    collection_date: datetime | None = None
    payment_reference: str | None = Field(default=None, max_length=250)
    notes: str | None = Field(default=None, max_length=5000)

    @field_validator("collection_date")
    @classmethod
    def require_timezone(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("collection_date must include a timezone")
        return value

    @field_validator("payment_reference", "notes")
    @classmethod
    def clean_optional_text(cls, value):
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def reject_null_required_updates(self):
        if "amount" in self.model_fields_set and self.amount is None:
            raise ValueError("amount cannot be null")
        if "collection_date" in self.model_fields_set and self.collection_date is None:
            raise ValueError("collection_date cannot be null")
        return self


class SalesCollectionOut(BaseModel):
    id: UUID
    lead_id: UUID
    lead_company_name: str | None = None
    amount: Decimal
    currency: str
    collection_date: datetime
    payment_reference: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class RevenueTargetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=200)
    target_amount: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    currency: str = "INR"
    period_start: date
    period_end: date
    active: bool = True

    @field_validator("label")
    @classmethod
    def clean_label(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("label cannot be blank")
        return value

    @field_validator("currency")
    @classmethod
    def currency_code(cls, value):
        return validate_currency(value)

    @field_validator("period_end")
    @classmethod
    def valid_period(cls, value, info):
        start = info.data.get("period_start")
        if start is not None and value < start:
            raise ValueError("period_end must be on or after period_start")
        return value


class RevenueTargetPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, min_length=1, max_length=200)
    target_amount: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    currency: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    active: bool | None = None

    @field_validator("label")
    @classmethod
    def clean_label(cls, value):
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("currency")
    @classmethod
    def currency_code(cls, value):
        return validate_currency(value) if value is not None else None


class RevenueTargetOut(BaseModel):
    id: UUID
    label: str
    target_amount: Decimal
    currency: str
    period_start: date
    period_end: date
    active: bool
    created_at: datetime
    updated_at: datetime


class RevenueSummaryOut(BaseModel):
    currency: str
    pipeline_value: Decimal
    proposal_value: Decimal
    negotiation_value: Decimal
    won_value: Decimal
    expected_collection: Decimal
    actual_collected: Decimal
    outstanding_collection: Decimal
    target_id: UUID | None
    target_label: str | None
    target_amount: Decimal | None
    target_collected: Decimal | None
    target_remaining: Decimal | None
    achievement_percentage: Decimal | None


class PipelineStageOut(BaseModel):
    sales_status: SalesStatus
    lead_count: int
    current_opportunity_value: Decimal
    won_value: Decimal
    collected_value: Decimal


class ConversionStepOut(BaseModel):
    from_status: SalesStatus
    to_status: SalesStatus
    source_count: int
    converted_count: int
    conversion_percentage: Decimal | None


class ConversionOut(BaseModel):
    steps: list[ConversionStepOut]
    tracked_lead_count: int
    overall_conversion_percentage: Decimal | None
    won_deal_count: int
    lost_deal_count: int
    open_opportunity_count: int
    average_won_deal_value: Decimal | None
    average_proposal_value: Decimal | None
    history_limitation: str


class SourceAnalyticsOut(BaseModel):
    available: bool
    reason: str
    sources: list[dict]

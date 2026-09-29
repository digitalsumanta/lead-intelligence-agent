"""Strict public contracts for independent SEO audit history."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class SeoContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)


class SeoRunOut(SeoContract):
    id: UUID
    lead_id: UUID
    status: Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED"]
    audit_version: str
    root_url: str
    normalized_root_url: str
    pages_crawled: int
    pages_discovered: int
    findings_count: int
    metadata: dict = Field(validation_alias=AliasChoices("metadata_json", "metadata"))
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class SeoObservationOut(SeoContract):
    id: UUID
    observation_type: str
    page_url: str | None
    observed_value: dict
    created_at: datetime


class SeoFindingOut(SeoContract):
    id: UUID
    observation_id: UUID | None
    page_url: str | None
    finding_code: str
    category: Literal["TECHNICAL", "ON_PAGE", "CRAWLABILITY", "INDEXABILITY", "PERFORMANCE", "STRUCTURED_DATA", "LINKS", "IMAGES"]
    severity: Literal["INFO", "LOW", "MEDIUM", "HIGH"]
    title: str
    description: str
    evidence: dict
    created_at: datetime


class SeoRunDetailOut(SeoContract):
    run: SeoRunOut
    observations: list[SeoObservationOut]
    findings: list[SeoFindingOut]


class SeoRunListOut(SeoContract):
    items: list[SeoRunOut]
    page: int
    page_size: int


class LatestSeoOut(SeoContract):
    audit: SeoRunOut | None
    findings: list[SeoFindingOut]


class SeoRunCreatedOut(SeoContract):
    run: SeoRunOut

from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict
from typing import Optional
from app.models.entities import ConfidenceLevel, SourceType

class CompanyEnrichmentProfileSchema(BaseModel):
    id: UUID
    lead_id: UUID
    legal_name: Optional[str] = None
    display_name: Optional[str] = None
    domain: Optional[str] = None
    website: Optional[str] = None
    email_domain: Optional[str] = None
    linkedin_url: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    description: Optional[str] = None
    industry: Optional[str] = None
    normalized_industry: Optional[str] = None
    company_size: Optional[str] = None
    normalized_company_size: Optional[str] = None
    source: SourceType
    confidence: ConfidenceLevel
    completeness_percent: float
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

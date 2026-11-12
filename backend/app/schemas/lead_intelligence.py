import uuid
from datetime import datetime, timezone
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field
from typing import List, Optional, Dict, Any
from app.models.entities import ConfidenceLevel, SourceType

def _utc_now():
    return datetime.now(timezone.utc)

class IntelligenceCompanyProfile(BaseModel):
    name: Optional[str] = None
    legal_name: Optional[str] = None
    website: Optional[str] = None
    industry: Optional[str] = None
    industry_source: Optional[SourceType] = None
    company_size: Optional[str] = None
    company_size_source: Optional[SourceType] = None
    location: Optional[str] = None
    country: Optional[str] = None
    description: Optional[str] = None
    products_services: List[str] = []

class IntelligenceContactProfile(BaseModel):
    name: Optional[str] = None
    job_title: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None

class IntelligenceDigitalPresence(BaseModel):
    website: Optional[str] = None
    linkedin_url: Optional[str] = None
    facebook_url: Optional[str] = None
    instagram_url: Optional[str] = None
    youtube_url: Optional[str] = None
    other_urls: List[str] = []

class IntelligencePainPoint(BaseModel):
    id: UUID = Field(default_factory=uuid.uuid4)
    title: str
    description: Optional[str] = None
    source_type: SourceType
    source_reference: Optional[str] = None
    confidence: ConfidenceLevel
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    model_config = ConfigDict(from_attributes=True)

class IntelligenceOpportunity(BaseModel):
    id: UUID = Field(default_factory=uuid.uuid4)
    title: str
    description: Optional[str] = None
    opportunity_type: Optional[str] = None
    opportunity_category: Optional[str] = None
    recommended_service: Optional[str] = None
    commercial_relevance: Optional[str] = None
    suggested_next_action: Optional[str] = None
    status: Optional[str] = "ACTIVE"
    opportunity_status: Optional[str] = "ACTIVE"
    score: Optional[int] = None
    priority: Optional[str] = None
    recommended_action: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None
    evidence_source: Optional[str] = None
    source_type: SourceType
    source_reference: Optional[str] = None
    confidence: ConfidenceLevel
    related_task_id: Optional[UUID] = None
    related_task_status: Optional[str] = None
    last_evaluated: Optional[Any] = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    model_config = ConfigDict(from_attributes=True)

class IntelligenceBusinessContext(BaseModel):
    pain_points: List[IntelligencePainPoint] = []
    opportunities: List[IntelligenceOpportunity] = []
    known_services: List[str] = []
    known_products: List[str] = []

class IntelligenceQuality(BaseModel):
    completeness_score: float
    last_researched_at: Optional[datetime] = None
    field_count: int
    populated_field_count: int

class LeadIntelligenceProfileSchema(BaseModel):
    lead_id: UUID
    company: IntelligenceCompanyProfile
    contact: IntelligenceContactProfile
    digital_presence: IntelligenceDigitalPresence
    business_context: IntelligenceBusinessContext
    intelligence_quality: IntelligenceQuality

    model_config = ConfigDict(from_attributes=True)

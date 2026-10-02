from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict
from typing import Optional, Dict, Any
from app.models.entities import ConfidenceLevel, SourceType

class WebsiteIntelligenceProfileSchema(BaseModel):
    id: UUID
    company_id: UUID
    website: Optional[str] = None
    normalized_website: Optional[str] = None
    domain: Optional[str] = None
    normalized_domain: Optional[str] = None
    website_present: bool
    domain_present: bool
    technology_signals: Optional[Dict[str, Any]] = None
    content_signals: Optional[Dict[str, Any]] = None
    source_type: SourceType
    confidence: ConfidenceLevel
    completeness: float
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

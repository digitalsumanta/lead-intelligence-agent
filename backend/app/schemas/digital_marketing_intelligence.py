from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict
from typing import Optional, Dict, Any, List
from app.models.entities import ConfidenceLevel, SourceType

class DigitalMarketingIntelligenceProfileSchema(BaseModel):
    id: UUID
    company_id: UUID
    seo_signals: Optional[Dict[str, Any]] = None
    paid_advertising_signals: Optional[Dict[str, Any]] = None
    social_media_signals: Optional[Dict[str, Any]] = None
    content_marketing_signals: Optional[Dict[str, Any]] = None
    marketing_maturity: str
    marketing_gaps: Optional[List[str]] = None
    marketing_opportunities: Optional[List[str]] = None
    source_type: SourceType
    confidence: ConfidenceLevel
    completeness: float
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, Any, Dict
from uuid import UUID
from datetime import datetime

from app.domain.contact_intelligence import SeniorityLevel, FunctionalArea, BuyingInfluenceLevel
from app.models.entities import ConfidenceLevel, SourceType

class ContactIntelligenceProfileSchema(BaseModel):
    id: UUID
    contact_id: UUID
    department: Optional[str] = None
    functional_area: FunctionalArea
    seniority_level: SeniorityLevel
    decision_maker_score: int
    buying_influence: BuyingInfluenceLevel
    confidence: ConfidenceLevel
    source_type: SourceType
    evidence: Optional[Dict[str, Any]] = None
    evaluated_at: datetime
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

from pydantic import BaseModel, ConfigDict, Field
from typing import Any, Dict, Optional, List
from datetime import datetime
from uuid import UUID

from app.models.entities import ConfidenceLevel, SourceType, SignalStrength


class BuyingSignalSchema(BaseModel):
    id: UUID
    company_id: UUID
    signal_type: str = Field(..., description="The exact deterministic signal type, e.g. WEBSITE_INTELLIGENCE_CHANGE")
    signal_category: str = Field(..., description="The broad category, e.g. website_activity")
    confidence: ConfidenceLevel = Field(..., description="The confidence in the evidence")
    strength: Optional[SignalStrength] = Field(None, description="The strength of the signal")
    source_type: SourceType = Field(..., description="The source that produced this evidence")
    detected_at: datetime = Field(..., description="When the evidence was originally detected")
    evidence: Dict[str, Any] = Field(default_factory=dict, description="Structured evidence proving the signal")
    status: str = Field("ACTIVE", description="ACTIVE or ARCHIVED")
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BuyingSignalsResponse(BaseModel):
    signals: List[BuyingSignalSchema]

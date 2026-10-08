import uuid
from typing import Optional, Dict, Any
from pydantic import BaseModel

class ContactRecommendationTaskResponse(BaseModel):
    opportunity_id: uuid.UUID
    contact_id: Optional[uuid.UUID] = None
    task_id: Optional[uuid.UUID] = None
    created: bool
    reused: bool
    recommendation_score: int
    priority: Optional[str] = None
    recommended_action: str
    task_type: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None

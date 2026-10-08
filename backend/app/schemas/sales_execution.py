import uuid
from typing import Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel
from app.models.entities import SalesActivityOutcome, SalesStatus

class SalesTaskExecutionRequest(BaseModel):
    outcome: SalesActivityOutcome
    notes: Optional[str] = None
    activity_id: Optional[uuid.UUID] = None
    opportunity_stage: Optional[str] = None
    new_sales_status: Optional[SalesStatus] = None

class SalesTaskExecutionResponse(BaseModel):
    task_id: uuid.UUID
    activity_id: uuid.UUID
    outcome: str
    task_status: str
    created: bool
    reused: bool
    lead_id: uuid.UUID
    opportunity_id: Optional[uuid.UUID] = None
    occurred_at: datetime
    evidence: Optional[Dict[str, Any]] = None

from pydantic import BaseModel, ConfigDict
from typing import Optional, Any, Dict, List
from uuid import UUID

from app.domain.opportunity_contact_matching import MatchLevel

class OpportunityContactMatchItem(BaseModel):
    contact_id: UUID
    match_score: int
    match_level: MatchLevel
    decision_maker_score: int
    functional_relevance_score: int
    role_relevance_score: int
    seniority_relevance_score: int
    department_relevance_score: int
    completeness_score: int
    match_reasons: List[str]
    evidence: Dict[str, Any]

    model_config = ConfigDict(from_attributes=True)


class OpportunityContactsResponse(BaseModel):
    opportunity_id: UUID
    contacts: List[OpportunityContactMatchItem]

    model_config = ConfigDict(from_attributes=True)

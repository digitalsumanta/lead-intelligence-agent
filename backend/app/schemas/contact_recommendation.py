from pydantic import BaseModel, ConfigDict
from typing import Optional, Any, Dict, List
from uuid import UUID

from app.models.entities import BuyingInfluenceLevel
from app.domain.contact_recommendation import RecommendationPriority, RecommendedSalesAction
from app.schemas.opportunity_contact_matching import OpportunityContactMatchItem

class ContactRecommendationResponse(BaseModel):
    opportunity_id: UUID
    recommended_contact: Optional[OpportunityContactMatchItem] = None
    recommendation_score: int
    match_score: int
    decision_maker_score: int
    buying_influence: BuyingInfluenceLevel
    priority: RecommendationPriority
    recommended_action: RecommendedSalesAction
    reasons: List[str]
    evidence: Dict[str, Any]

    model_config = ConfigDict(from_attributes=True)

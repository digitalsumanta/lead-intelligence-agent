from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from app.database import get_db
from app.schemas.contact_recommendation import ContactRecommendationResponse
from app.services.contact_recommendation import get_contact_recommendation_for_opportunity

router = APIRouter()

@router.get(
    "/api/sales-opportunities/{opportunity_id}/contact-recommendation",
    response_model=ContactRecommendationResponse,
    tags=["Sales Opportunities"]
)
async def read_opportunity_contact_recommendation(
    opportunity_id: UUID,
    db: AsyncSession = Depends(get_db)
):
    result = await get_contact_recommendation_for_opportunity(db, opportunity_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sales opportunity not found"
        )
    return result

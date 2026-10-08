from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from app.database import get_db
from app.schemas.contact_recommendation_execution import ContactRecommendationTaskResponse
from app.services.contact_recommendation_execution import synchronize_recommended_contact_task

router = APIRouter()

@router.post(
    "/api/sales-opportunities/{opportunity_id}/contact-recommendation/task",
    response_model=ContactRecommendationTaskResponse,
    tags=["Sales Opportunities"]
)
async def create_or_reuse_opportunity_contact_recommendation_task(
    opportunity_id: UUID,
    db: AsyncSession = Depends(get_db)
):
    result = await synchronize_recommended_contact_task(db, opportunity_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sales opportunity not found"
        )
    return result

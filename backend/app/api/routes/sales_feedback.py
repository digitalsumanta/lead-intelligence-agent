from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID
from typing import Optional

from app.database import get_db
from app.schemas.sales_feedback import SalesFeedbackSummaryResponse
from app.services.sales_feedback import (
    get_opportunity_sales_feedback,
    get_contact_sales_feedback,
    get_recommendations_sales_feedback,
)

router = APIRouter(prefix="/api/sales-feedback", tags=["Sales Feedback"])


@router.get(
    "/opportunities/{opportunity_id}",
    response_model=SalesFeedbackSummaryResponse,
)
async def get_opportunity_feedback_endpoint(
    opportunity_id: UUID,
    db: AsyncSession = Depends(get_db)
):
    result = await get_opportunity_sales_feedback(db, opportunity_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sales opportunity not found"
        )
    return result


@router.get(
    "/contacts/{contact_id}",
    response_model=SalesFeedbackSummaryResponse,
)
async def get_contact_feedback_endpoint(
    contact_id: UUID,
    db: AsyncSession = Depends(get_db)
):
    result = await get_contact_sales_feedback(db, contact_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Contact not found"
        )
    return result


@router.get(
    "/recommendations",
    response_model=SalesFeedbackSummaryResponse,
)
async def get_recommendations_feedback_endpoint(
    action: Optional[str] = Query(None, description="Filter by recommended action or task type"),
    sentiment: Optional[str] = Query(None, description="Filter by sentiment (POSITIVE, NEGATIVE, NEUTRAL, UNKNOWN)"),
    db: AsyncSession = Depends(get_db)
):
    return await get_recommendations_sales_feedback(
        session=db,
        action=action,
        sentiment=sentiment,
    )

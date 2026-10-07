from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID
from typing import Optional

from app.database import get_db
from app.schemas.opportunity_contact_matching import OpportunityContactsResponse
from app.services.opportunity_contact_matching import get_matched_contacts_for_opportunity

router = APIRouter()

@router.get(
    "/api/sales-opportunities/{opportunity_id}/contacts",
    response_model=OpportunityContactsResponse,
    tags=["Sales Opportunities"]
)
async def read_opportunity_matched_contacts(
    opportunity_id: UUID,
    limit: Optional[int] = Query(default=None, ge=1, le=100),
    db: AsyncSession = Depends(get_db)
):
    result = await get_matched_contacts_for_opportunity(db, opportunity_id, limit=limit)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sales opportunity not found"
        )
    return result

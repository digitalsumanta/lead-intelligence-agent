from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from app.database import get_db
from app.schemas.contact_intelligence import ContactIntelligenceProfileSchema
from app.services.contact_intelligence import get_contact_intelligence, refresh_contact_intelligence

router = APIRouter()

@router.get("/api/contacts/{contact_id}/intelligence", response_model=ContactIntelligenceProfileSchema, tags=["Contacts"])
async def read_contact_intelligence(contact_id: UUID, db: AsyncSession = Depends(get_db)):
    profile = await get_contact_intelligence(db, contact_id)
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact intelligence profile not found")
    return profile

@router.post("/api/contacts/{contact_id}/intelligence/refresh", response_model=ContactIntelligenceProfileSchema, tags=["Contacts"])
async def recalculate_contact_intelligence(contact_id: UUID, db: AsyncSession = Depends(get_db)):
    profile = await refresh_contact_intelligence(db, contact_id)
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found")
    return profile

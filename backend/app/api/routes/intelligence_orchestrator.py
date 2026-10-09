from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from app.database import get_db
from app.schemas.intelligence_orchestrator import LeadIntelligenceOrchestrationResponse
from app.services.intelligence_orchestrator import run_lead_intelligence_orchestration

router = APIRouter(tags=["Intelligence Orchestrator"])


@router.post(
    "/api/leads/{lead_id}/intelligence/orchestrate",
    response_model=LeadIntelligenceOrchestrationResponse,
)
async def orchestrate_lead_intelligence_endpoint(
    lead_id: UUID,
    db: AsyncSession = Depends(get_db)
):
    result = await run_lead_intelligence_orchestration(db, lead_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lead not found"
        )
    return result

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from app.database import get_db
from app.schemas.sales_execution import SalesTaskExecutionRequest, SalesTaskExecutionResponse
from app.services.sales_execution import (
    record_sales_execution_outcome,
    SalesExecutionError,
)

router = APIRouter()

@router.post(
    "/api/sales-tasks/{task_id}/execution",
    response_model=SalesTaskExecutionResponse,
    tags=["Sales Tasks"]
)
async def record_sales_task_execution_endpoint(
    task_id: UUID,
    body: SalesTaskExecutionRequest,
    db: AsyncSession = Depends(get_db)
):
    try:
        result = await record_sales_execution_outcome(
            session=db,
            task_id=task_id,
            outcome=body.outcome,
            notes=body.notes,
            activity_id=body.activity_id,
            opportunity_stage=body.opportunity_stage,
            new_sales_status=body.new_sales_status,
        )
    except SalesExecutionError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.message
        )

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sales task not found"
        )

    return result

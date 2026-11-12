"""API routes for MindArtDigital Business Research and Lead Discovery Engine."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.business_research import (
    ResearchInput,
    ResearchJobState,
    ResearchProvidersStatus,
    SaveLeadsRequest,
    SaveLeadsResponse,
    provider_registry,
    research_engine,
)

router = APIRouter(tags=["Business Research"])


@router.get("/api/research/providers/status", response_model=ResearchProvidersStatus)
async def get_research_providers_status():
    """Report status of configured discovery and research providers."""
    providers_info = provider_registry.list_providers()
    configured_any = any(p["is_configured"] for p in providers_info)
    try:
        active = provider_registry.get_active_provider().name
        msg = "Discovery provider configured and ready."
    except Exception as exc:
        active = "none"
        msg = str(exc)

    return ResearchProvidersStatus(
        active_discovery_provider=active,
        is_configured=configured_any,
        message=msg,
        providers=providers_info,
    )


@router.post("/api/research/jobs", response_model=ResearchJobState, status_code=status.HTTP_201_CREATED)
async def create_research_job(input_params: ResearchInput):
    """Initiate an evidence-based business research and lead discovery job."""
    job = research_engine.create_job(input_params)
    research_engine.start_job(job.job_id)
    return job


@router.get("/api/research/jobs", response_model=list[ResearchJobState])
async def list_research_jobs(limit: int = Query(20, ge=1, le=100)):
    """List recent research jobs."""
    return research_engine.list_jobs(limit=limit)


@router.get("/api/research/jobs/{job_id}", response_model=ResearchJobState)
async def get_research_job(job_id: str):
    """Get research job status, live progress, and collected candidate leads."""
    job = research_engine.get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Research job not found")
    return job


@router.post("/api/research/jobs/{job_id}/cancel", response_model=ResearchJobState)
async def cancel_research_job(job_id: str):
    """Cancel an ongoing research job."""
    success = research_engine.cancel_job(job_id)
    job = research_engine.get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Research job not found")
    return job


@router.post("/api/research/save-leads", response_model=SaveLeadsResponse)
async def save_researched_leads(
    request: SaveLeadsRequest,
    db: AsyncSession = Depends(get_db),
):
    """Save selected researched leads into the database with duplicate preservation."""
    if not request.leads:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No leads provided to save")
    return await research_engine.save_leads(db, request)


@router.get("/api/research/export/{job_id}")
async def export_research_job_csv(job_id: str):
    """Export research job results as mindartdigital_lead_research_template.csv."""
    try:
        csv_bytes = research_engine.export_job_csv(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="mindartdigital_lead_research_template.csv"'
        },
    )

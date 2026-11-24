"""SEO audit runs, findings and opportunities."""
import uuid

from app.database import (
    get_db,
)
from app.models import (
    Lead,
)
from app.models.entities import SeoAuditRun, SeoFinding, SeoObservation
from app.schemas.seo import (
    LatestSeoOut,
    SeoRunCreatedOut,
    SeoRunDetailOut,
    SeoRunListOut,
)
from app.schemas.seo_opportunities import SeoSalesOpportunityList
from app.services.seo import SeoServiceError, create_seo_run
from app.services.seo_opportunities import read_seo_sales_opportunities
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


@router.post("/api/leads/{lead_id}/seo/runs", response_model=SeoRunCreatedOut, status_code=202)
async def start_seo_audit(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Queue a manually requested, independent SEO audit."""
    try:
        run = await create_seo_run(db, lead_id)
    except SeoServiceError as error:
        raise HTTPException(error.status_code, detail={"code": error.code, "message": error.message})
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code": "SEO_AUDIT_ALREADY_ACTIVE", "message": "An SEO audit is already queued or running for this lead"})
    return {"run": run}


@router.get("/api/leads/{lead_id}/seo", response_model=LatestSeoOut)
async def latest_seo_audit(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    if await db.get(Lead, lead_id) is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    run = await db.scalar(select(SeoAuditRun).where(SeoAuditRun.lead_id == lead_id, SeoAuditRun.status == "COMPLETED").order_by(SeoAuditRun.created_at.desc(), SeoAuditRun.id.desc()).limit(1))
    findings = [] if run is None else (await db.execute(select(SeoFinding).where(SeoFinding.seo_run_id == run.id).order_by(SeoFinding.severity, SeoFinding.finding_code, SeoFinding.id))).scalars().all()
    return {"audit": run, "findings": findings}


@router.get("/api/leads/{lead_id}/seo/opportunities", response_model=SeoSalesOpportunityList)
async def lead_seo_opportunities(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    if await db.get(Lead, lead_id) is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    return {"items": await read_seo_sales_opportunities(db, lead_id=lead_id)}


@router.get("/api/leads/{lead_id}/seo/runs", response_model=SeoRunListOut)
async def list_seo_runs(lead_id: uuid.UUID, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    if await db.get(Lead, lead_id) is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    rows = (await db.execute(select(SeoAuditRun).where(SeoAuditRun.lead_id == lead_id).order_by(SeoAuditRun.created_at.desc(), SeoAuditRun.id.desc()).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"items": rows, "page": page, "page_size": page_size}


@router.get("/api/seo/runs/{run_id}", response_model=SeoRunDetailOut)
async def seo_run_detail(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    run = await db.get(SeoAuditRun, run_id)
    if run is None:
        raise HTTPException(404, detail={"code": "SEO_RUN_NOT_FOUND", "message": "SEO audit run not found"})
    observations = (await db.execute(select(SeoObservation).where(SeoObservation.seo_run_id == run_id).order_by(SeoObservation.observation_type, SeoObservation.page_url, SeoObservation.id))).scalars().all()
    findings = (await db.execute(select(SeoFinding).where(SeoFinding.seo_run_id == run_id).order_by(SeoFinding.severity, SeoFinding.finding_code, SeoFinding.id))).scalars().all()
    return {"run": run, "observations": observations, "findings": findings}

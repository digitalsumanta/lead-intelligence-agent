"""Lead records: listing, details, CSV import/template, processing and stored intelligence."""
import uuid

from app.api.common import job_payload
from app.config import settings
from app.database import get_db
from app.models import (
    EnrichmentRun,
    Lead,
    LeadBatch,
    LeadScore,
    LeadStatus,
    Opportunity,
    Personalization,
    SalesPriority,
    SalesStatus,
    ServiceRecommendation,
)
from app.models.entities import now_utc
from app.providers import validate_provider_model
from app.schemas.intelligence import IntelligenceResponse
from app.schemas.lead import LeadOut, LeadUpdate, ProcessBatchIn
from app.schemas.lead_intelligence import LeadIntelligenceProfileSchema
from app.services.csv_import import EMAIL_RE, get_template_csv_bytes, import_csv
from app.services.jobs import create_job
from app.services.lead_intelligence import (
    get_lead_intelligence,
    refresh_lead_intelligence,
)
from app.services.lead_intelligence_api import (
    read_intelligence_summary,
    read_lead_intelligence,
)
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


@router.get("/api/leads", response_model=list[LeadOut])
async def list_leads(
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
    status: LeadStatus | None = None,
    min_score: int | None = Query(None, ge=0, le=100),
    max_score: int | None = Query(None, ge=0, le=100),
    intelligence_status: str | None = Query(None, pattern="^(available|unavailable)$"),
    personalization_available: bool | None = None,
    opportunity_present: bool | None = None,
    sales_status: SalesStatus | None = None,
    priority: SalesPriority | None = None,
    follow_up_due: bool | None = None,
    qualification_class: str | None = Query(None, pattern="^(HOT|WARM|NURTURE|LOW_PRIORITY)$"),
    sort_by: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """List leads newest first, with stable created_at/id ordering and one-based pages."""
    latest_run = select(EnrichmentRun.id).where(
        EnrichmentRun.lead_id == Lead.id,
        func.lower(EnrichmentRun.status) == "completed",
        select(LeadScore.id).where(
            LeadScore.lead_id == Lead.id,
            LeadScore.enrichment_run_id == EnrichmentRun.id,
        ).exists(),
    ).order_by(func.coalesce(EnrichmentRun.completed_at, EnrichmentRun.created_at).desc(), EnrichmentRun.id.desc()).limit(1).correlate(Lead).scalar_subquery()
    score_value = select(LeadScore.score).where(
        LeadScore.lead_id == Lead.id, LeadScore.enrichment_run_id == latest_run,
    ).order_by(LeadScore.calculated_at.desc(), LeadScore.id.desc()).limit(1).correlate(Lead).scalar_subquery()
    opportunity_count = select(func.count(Opportunity.id)).where(
        Opportunity.lead_id == Lead.id, Opportunity.enrichment_run_id == latest_run,
        Opportunity.origin == "deterministic",
    ).correlate(Lead).scalar_subquery()
    recommendation_count = select(func.count(ServiceRecommendation.id)).where(
        ServiceRecommendation.lead_id == Lead.id, ServiceRecommendation.enrichment_run_id == latest_run,
    ).correlate(Lead).scalar_subquery()
    has_personalization = select(Personalization.id).where(
        Personalization.lead_id == Lead.id, Personalization.enrichment_run_id == latest_run,
        Personalization.status == "validated", Personalization.generated.is_not(None),
        Personalization.contract_version == "personalization-v2",
    ).exists().correlate(Lead)
    latest_at = select(func.coalesce(EnrichmentRun.completed_at, EnrichmentRun.created_at)).where(
        EnrichmentRun.id == latest_run,
    ).correlate(Lead).scalar_subquery()
    available = latest_run.is_not(None)
    query = select(
        Lead, case((available, "available"), else_="unavailable").label("intelligence_status"),
        score_value.label("intelligence_score"), opportunity_count.label("opportunity_count"),
        recommendation_count.label("recommendation_count"), has_personalization.label("personalization_available"),
        latest_at.label("latest_enrichment_at"),
    )
    if status:
        query = query.where(Lead.status == status)
    if min_score is not None:
        query = query.where(score_value >= min_score)
    if max_score is not None:
        query = query.where(score_value <= max_score)
    if intelligence_status == "available":
        query = query.where(available)
    elif intelligence_status == "unavailable":
        query = query.where(~available)
    if personalization_available is not None:
        query = query.where(has_personalization == personalization_available)
    if opportunity_present is not None:
        query = query.where((opportunity_count > 0) == opportunity_present)
    if sales_status is not None:
        query = query.where(Lead.sales_status == sales_status)
    if priority is not None:
        query = query.where(Lead.priority == priority)
    if follow_up_due is True:
        query = query.where(Lead.next_follow_up_at.is_not(None), Lead.next_follow_up_at <= now_utc())
    elif follow_up_due is False:
        query = query.where((Lead.next_follow_up_at.is_(None)) | (Lead.next_follow_up_at > now_utc()))
    if min_score is not None and max_score is not None and min_score > max_score:
        raise HTTPException(422, detail={"code": "VALIDATION_ERROR", "message": "min_score cannot exceed max_score"})
    if qualification_class is not None:
        query = query.where(Lead.score_factors['qualification']['classification'].as_string() == qualification_class)
    order_clauses = [Lead.created_at.desc(), Lead.id.desc()]
    if sort_by == "qualification_score_desc":
        order_clauses = [Lead.score_factors['qualification']['qualification_score'].as_integer().desc(), Lead.id.desc()]
    elif sort_by == "qualification_score_asc":
        order_clauses = [Lead.score_factors['qualification']['qualification_score'].as_integer().asc(), Lead.id.desc()]
    rows = (await db.execute(query.order_by(*order_clauses)
        .offset((page - 1) * page_size).limit(page_size))).all()
    results = []
    for lead, intel_status, score, opportunities, recommendations, has_personalization_value, enriched_at in rows:
        wa = lead.website_analysis if isinstance(lead.website_analysis, dict) else {}
        sf = lead.score_factors if isinstance(lead.score_factors, dict) else {}
        qual = sf.get("qualification") if isinstance(sf.get("qualification"), dict) else None
        result = LeadOut.model_validate({
            **{column.name: getattr(lead, column.name) for column in Lead.__table__.columns},
            "id": str(lead.id), "status": lead.status.value,
            "sales_status": lead.sales_status.value, "priority": lead.priority.value,
            "sub_category": wa.get("sub_category"),
            "advertising_platforms": wa.get("advertising_platforms"),
            "advertising_evidence": wa.get("advertising_evidence"),
            "primary_problem": wa.get("primary_problem") or (lead.pain_points[0] if lead.pain_points else None),
            "recommended_mindart_service": wa.get("recommended_mindart_service") or (lead.recommended_services[0] if lead.recommended_services else None),
            "intelligence_status": intel_status, "intelligence_score": score,
            "opportunity_count": opportunities, "recommendation_count": recommendations,
            "personalization_available": has_personalization_value,
            "latest_enrichment_at": enriched_at,
            "qualification_score": qual.get("qualification_score") if qual else None,
            "scoring_coverage": qual.get("scoring_coverage") if qual else None,
            "qualification_class": qual.get("classification") if qual else None,
            "recommended_sales_action": qual.get("recommended_sales_action") if qual else None,
            "qualification_details": qual,
        })
        results.append(result)
    return results


@router.get("/api/leads/{lead_id}", response_model=LeadOut)
async def get_lead(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Fetch full lead intelligence and recent processing history."""
    lead = await db.get(Lead, lead_id)
    if not lead: raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    summary = await read_intelligence_summary(db, lead_id)
    wa = lead.website_analysis if isinstance(lead.website_analysis, dict) else {}
    sf = lead.score_factors if isinstance(lead.score_factors, dict) else {}
    qual = sf.get("qualification") if isinstance(sf.get("qualification"), dict) else None
    return LeadOut.model_validate({
        **{c.name: getattr(lead, c.name) for c in Lead.__table__.columns},
        "id": str(lead.id),
        "status": lead.status.value,
        "sub_category": wa.get("sub_category"),
        "advertising_platforms": wa.get("advertising_platforms"),
        "advertising_evidence": wa.get("advertising_evidence"),
        "primary_problem": wa.get("primary_problem") or (lead.pain_points[0] if lead.pain_points else None),
        "recommended_mindart_service": wa.get("recommended_mindart_service") or (lead.recommended_services[0] if lead.recommended_services else None),
        "qualification_score": qual.get("qualification_score") if qual else None,
        "scoring_coverage": qual.get("scoring_coverage") if qual else None,
        "qualification_class": qual.get("classification") if qual else None,
        "recommended_sales_action": qual.get("recommended_sales_action") if qual else None,
        "qualification_details": qual,
        **summary.model_dump()
    })


@router.patch("/api/leads/{lead_id}", response_model=LeadOut)
async def update_lead(lead_id: uuid.UUID, body: LeadUpdate, db: AsyncSession = Depends(get_db)):
    """Manually update lead details such as phone number without creating duplicate records."""
    lead = await db.scalar(select(Lead).where(Lead.id == lead_id).with_for_update())
    if lead is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    supplied = body.model_fields_set
    if "phone" in supplied:
        lead.phone = body.phone.strip() if body.phone else None
    if "email" in supplied:
        if body.email and not EMAIL_RE.fullmatch(body.email.strip()):
            raise HTTPException(422, detail={"code": "VALIDATION_ERROR", "message": "email is invalid"})
        lead.email = body.email.strip().lower() if body.email else None
    if "website" in supplied:
        if body.website:
            w = body.website.strip()
            if not w.startswith(("http://", "https://")):
                w = "https://" + w
            lead.website = w.rstrip("/")
        else:
            lead.website = None
    if "decision_maker" in supplied:
        lead.decision_maker = body.decision_maker.strip() if body.decision_maker else None
    if "job_title" in supplied:
        lead.job_title = body.job_title.strip() if body.job_title else None
    if "industry" in supplied:
        lead.industry = body.industry.strip() if body.industry else None
    if "city" in supplied:
        lead.city = body.city.strip() if body.city else None
    if "country" in supplied:
        lead.country = body.country.strip() if body.country else None
    lead.updated_at = now_utc()
    await db.commit()
    summary = await read_intelligence_summary(db, lead_id)
    wa = lead.website_analysis if isinstance(lead.website_analysis, dict) else {}
    sf = lead.score_factors if isinstance(lead.score_factors, dict) else {}
    qual = sf.get("qualification") if isinstance(sf.get("qualification"), dict) else None
    return LeadOut.model_validate({
        **{c.name: getattr(lead, c.name) for c in Lead.__table__.columns},
        "id": str(lead.id),
        "status": lead.status.value,
        "sub_category": wa.get("sub_category"),
        "advertising_platforms": wa.get("advertising_platforms"),
        "advertising_evidence": wa.get("advertising_evidence"),
        "primary_problem": wa.get("primary_problem") or (lead.pain_points[0] if lead.pain_points else None),
        "recommended_mindart_service": wa.get("recommended_mindart_service") or (lead.recommended_services[0] if lead.recommended_services else None),
        "qualification_score": qual.get("qualification_score") if qual else None,
        "scoring_coverage": qual.get("scoring_coverage") if qual else None,
        "qualification_class": qual.get("classification") if qual else None,
        "recommended_sales_action": qual.get("recommended_sales_action") if qual else None,
        "qualification_details": qual,
        **summary.model_dump(),
    })


@router.get("/api/leads/{lead_id}/enrichment", response_model=IntelligenceResponse, tags=["Leads"])
async def get_lead_enrichment(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Return persisted intelligence from one completed enrichment run; never invokes a provider."""
    if await db.get(Lead, lead_id) is None:
        raise HTTPException(404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    response = await read_lead_intelligence(db, lead_id)
    if response is None:
        raise HTTPException(409, detail={
            "code": "INTELLIGENCE_NOT_AVAILABLE",
            "message": "No completed enrichment run with valid persisted intelligence is available",
        })
    return response


@router.get("/api/leads/{lead_id}/intelligence", response_model=LeadIntelligenceProfileSchema, tags=["Leads"])
async def endpoint_get_lead_intelligence(
    lead_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    profile = await get_lead_intelligence(db, lead_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Lead not found")
    return profile


@router.get("/api/leads/import/template")
@router.get("/api/leads/template")
async def download_lead_template():
    """Download the MindArtDigital Lead Research CSV template."""
    content = get_template_csv_bytes()
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="mindartdigital_lead_research_template.csv"'
        },
    )


@router.post("/api/leads/import")
async def import_leads(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    """Validate and import an uploaded CSV file."""
    if not file.filename or not file.filename.lower().endswith(".csv"): raise HTTPException(400, detail={"code":"INVALID_FILE","message":"Upload a CSV file"})
    content = await file.read(settings.max_csv_bytes + 1)
    if len(content) > settings.max_csv_bytes: raise HTTPException(413, detail={"code":"FILE_TOO_LARGE","message":"CSV exceeds maximum size"})
    try: return await import_csv(db, content, file.filename)
    except (ValueError, UnicodeDecodeError) as exc: raise HTTPException(422, detail={"code":"INVALID_CSV","message":str(exc)})


@router.post("/api/leads/{lead_id}/process")
async def process_lead(lead_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Queue a single lead for durable worker processing."""
    lead = await db.get(Lead, lead_id)
    if not lead: raise HTTPException(404, detail={"code":"LEAD_NOT_FOUND","message":"Lead not found"})
    try:
        provider, model = validate_provider_model(None, None)
        job = await create_job(db, lead, provider, model)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(409, detail={"code":"JOB_NOT_CREATED","message":str(exc)})
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code":"LEAD_ALREADY_QUEUED","message":"This lead already has an active job"})
    return job_payload(job)


@router.post("/api/leads/process-batch")
async def process_batch(body: ProcessBatchIn, db: AsyncSession = Depends(get_db)):
    """Create a batch of durable jobs for worker execution."""
    size = min(body.batch_size, settings.max_batch_size)
    try: provider, model = validate_provider_model(body.provider, body.model)
    except ValueError as exc: raise HTTPException(422, detail={"code":"INVALID_PROVIDER_MODEL","message":str(exc)})
    leads = (await db.scalars(select(Lead).where(Lead.status.in_([LeadStatus.IMPORTED, LeadStatus.FAILED])).order_by(Lead.created_at).limit(size))).all()
    batch = LeadBatch(requested_count=len(leads), status="QUEUED"); db.add(batch); await db.flush()
    jobs = []
    try:
        for lead in leads: jobs.append(await create_job(db, lead, provider, model, batch.id, commit=False))
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(409, detail={"code":"JOB_NOT_CREATED","message":str(exc)})
    if not jobs: batch.status = "COMPLETED"
    try: await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code":"LEAD_ALREADY_QUEUED","message":"One or more leads already have active jobs"})
    return {"batch_id": str(batch.id), "requested": len(jobs), "jobs": [job_payload(j) for j in jobs]}


@router.post("/api/leads/{lead_id}/intelligence/refresh", response_model=LeadIntelligenceProfileSchema, tags=["Leads"])
async def endpoint_refresh_lead_intelligence(
    lead_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    profile = await refresh_lead_intelligence(db, lead_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Lead not found")
    return profile

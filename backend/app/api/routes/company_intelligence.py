"""Company enrichment, website and digital-marketing intelligence, buying signals, sales opportunities and lead qualification."""
import uuid

from app.database import (
    get_db,
)
from app.models import (
    Lead,
)
from app.schemas.buying_signals import BuyingSignalsResponse
from app.schemas.company_enrichment import CompanyEnrichmentProfileSchema
from app.schemas.digital_marketing_intelligence import (
    DigitalMarketingIntelligenceProfileSchema,
)
from app.schemas.lead_intelligence import (
    IntelligenceOpportunity,
)
from app.schemas.lead_qualification import LeadQualificationResponse
from app.schemas.website_intelligence import WebsiteIntelligenceProfileSchema
from app.services.buying_signals import (
    get_buying_signals_for_company,
    get_buying_signals_for_lead,
    refresh_buying_signals_for_company,
    refresh_buying_signals_for_lead,
)
from app.services.company_enrichment import (
    get_company_enrichment,
    refresh_company_enrichment,
)
from app.services.digital_marketing_intelligence import (
    get_digital_marketing_intelligence,
    refresh_digital_marketing_intelligence,
    research_digital_marketing_category,
)
from app.services.lead_qualification import get_lead_qualification, qualify_lead
from app.services.sales_opportunities import (
    get_sales_opportunities_for_lead,
    refresh_sales_opportunities_for_lead,
)
from app.services.website_intelligence import (
    get_website_intelligence,
    refresh_website_intelligence,
)
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
)
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


class SalesOpportunitiesResponse(BaseModel):
    opportunities: list[IntelligenceOpportunity]


@router.get("/api/companies/{company_id}/enrichment", response_model=CompanyEnrichmentProfileSchema, tags=["Companies"])
async def endpoint_get_company_enrichment(
    company_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    profile = await get_company_enrichment(db, company_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Company not found")
    return profile


@router.post("/api/companies/{company_id}/enrichment/refresh", response_model=CompanyEnrichmentProfileSchema, tags=["Companies"])
async def endpoint_refresh_company_enrichment(
    company_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    profile = await refresh_company_enrichment(db, company_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Company not found")
    return profile


@router.get("/api/leads/{lead_id}/company-enrichment", response_model=CompanyEnrichmentProfileSchema, tags=["Leads"])
async def endpoint_get_lead_company_enrichment(
    lead_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    profile = await get_company_enrichment(db, lead_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Lead not found")
    return profile


@router.get("/api/companies/{company_id}/website-intelligence", response_model=WebsiteIntelligenceProfileSchema, tags=["Companies"])
async def endpoint_get_website_intelligence(
    company_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    profile = await get_website_intelligence(db, company_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Company not found")
    return profile


@router.post("/api/companies/{company_id}/website-intelligence/refresh", response_model=WebsiteIntelligenceProfileSchema, tags=["Companies"])
async def endpoint_refresh_website_intelligence(
    company_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    profile = await refresh_website_intelligence(db, company_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Company not found")
    return profile


@router.get("/api/leads/{lead_id}/website-intelligence", response_model=WebsiteIntelligenceProfileSchema, tags=["Leads"])
async def endpoint_get_lead_website_intelligence(
    lead_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    profile = await get_website_intelligence(db, lead_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Lead not found")
    return profile


@router.get("/api/companies/{company_id}/digital-marketing-intelligence", response_model=DigitalMarketingIntelligenceProfileSchema, tags=["Companies"])
async def endpoint_get_digital_marketing_intelligence(
    company_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    profile = await get_digital_marketing_intelligence(db, company_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Company not found")
    return profile


@router.post("/api/companies/{company_id}/digital-marketing-intelligence/refresh", response_model=DigitalMarketingIntelligenceProfileSchema, tags=["Companies"])
async def endpoint_refresh_digital_marketing_intelligence(
    company_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    profile = await refresh_digital_marketing_intelligence(db, company_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Company not found")
    return profile


@router.post("/api/companies/{company_id}/digital-marketing-intelligence/research-category", response_model=DigitalMarketingIntelligenceProfileSchema, tags=["Companies"])
async def endpoint_research_company_digital_marketing_category(
    company_id: uuid.UUID,
    category: str = Query(..., description="Canonical category name"),
    retry: bool = Query(False, description="Retry flag for unavailable providers"),
    db: AsyncSession = Depends(get_db)
):
    profile = await research_digital_marketing_category(db, company_id, category, retry=retry)
    if not profile:
        raise HTTPException(status_code=404, detail="Company not found")
    return profile


@router.get("/api/leads/{lead_id}/digital-marketing-intelligence", response_model=DigitalMarketingIntelligenceProfileSchema, tags=["Leads"])
async def endpoint_get_lead_digital_marketing_intelligence(
    lead_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    profile = await get_digital_marketing_intelligence(db, lead_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Lead not found")
    return profile


@router.post("/api/leads/{lead_id}/digital-marketing-intelligence/refresh", response_model=DigitalMarketingIntelligenceProfileSchema, tags=["Leads"])
async def endpoint_refresh_lead_digital_marketing_intelligence(
    lead_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    profile = await refresh_digital_marketing_intelligence(db, lead_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Lead not found")
    return profile


@router.post("/api/leads/{lead_id}/digital-marketing-intelligence/research-category", response_model=DigitalMarketingIntelligenceProfileSchema, tags=["Leads"])
async def endpoint_research_lead_digital_marketing_category(
    lead_id: uuid.UUID,
    category: str = Query(..., description="Canonical category name"),
    retry: bool = Query(False, description="Retry flag for unavailable providers"),
    db: AsyncSession = Depends(get_db)
):
    profile = await research_digital_marketing_category(db, lead_id, category, retry=retry)
    if not profile:
        raise HTTPException(status_code=404, detail="Lead not found")
    return profile


@router.get("/api/companies/{company_id}/buying-signals", response_model=BuyingSignalsResponse, tags=["Companies"])
async def endpoint_get_buying_signals_company(
    company_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    lead = await db.get(Lead, company_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Company not found")
    signals = await get_buying_signals_for_company(db, company_id)
    return {"signals": signals}


@router.post("/api/companies/{company_id}/buying-signals/refresh", response_model=BuyingSignalsResponse, tags=["Companies"])
async def endpoint_refresh_buying_signals_company(
    company_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    lead = await db.get(Lead, company_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Company not found")
    signals = await refresh_buying_signals_for_company(db, company_id)
    return {"signals": signals}


@router.get("/api/leads/{lead_id}/buying-signals", response_model=BuyingSignalsResponse, tags=["Leads"])
async def endpoint_get_buying_signals_lead(
    lead_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    signals = await get_buying_signals_for_lead(db, lead_id)
    return {"signals": signals}


@router.post("/api/leads/{lead_id}/buying-signals/refresh", response_model=BuyingSignalsResponse, tags=["Leads"])
async def endpoint_refresh_buying_signals_lead(
    lead_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    signals = await refresh_buying_signals_for_lead(db, lead_id)
    return {"signals": signals}


@router.get("/api/leads/{lead_id}/sales-opportunities", response_model=SalesOpportunitiesResponse, tags=["Leads"])
async def endpoint_get_sales_opportunities(
    lead_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    opps = await get_sales_opportunities_for_lead(db, lead_id)
    return {"opportunities": opps}


@router.post("/api/leads/{lead_id}/sales-opportunities/refresh", response_model=SalesOpportunitiesResponse, tags=["Leads"])
async def endpoint_refresh_sales_opportunities(
    lead_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    opps = await refresh_sales_opportunities_for_lead(db, lead_id)
    return {"opportunities": opps}


@router.get("/api/leads/{lead_id}/qualification", response_model=LeadQualificationResponse, tags=["Leads"])
async def endpoint_get_lead_qualification(
    lead_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    qualification = await get_lead_qualification(db, lead_id, auto_compute_if_missing=True)
    if not qualification:
        raise HTTPException(status_code=404, detail={"code": "QUALIFICATION_NOT_FOUND", "message": "Lead qualification could not be computed"})
    return qualification


@router.post("/api/leads/{lead_id}/qualify", response_model=LeadQualificationResponse, tags=["Leads"])
async def endpoint_qualify_lead(
    lead_id: uuid.UUID,
    db: AsyncSession = Depends(get_db)
):
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail={"code": "LEAD_NOT_FOUND", "message": "Lead not found"})
    qualification = await qualify_lead(db, lead_id)
    return qualification

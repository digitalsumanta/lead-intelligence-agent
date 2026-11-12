import uuid
import logging
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.models.entities import (
    Lead,
    LeadIntelligenceProfile,
    LeadIntelligencePainPoint,
    LeadIntelligenceOpportunity,
    CompanyEnrichmentProfile
)
from app.domain.lead_intelligence import build_deterministic_profile
from app.schemas.lead_intelligence import (
    LeadIntelligenceProfileSchema,
    IntelligenceCompanyProfile,
    IntelligenceContactProfile,
    IntelligenceDigitalPresence,
    IntelligenceBusinessContext,
    IntelligenceQuality,
    IntelligencePainPoint,
    IntelligenceOpportunity
)
from app.services.unified_intelligence import sync_unified_intelligence_for_lead

def _to_schema(
    lead: Lead,
    profile: LeadIntelligenceProfile,
    pains: list[LeadIntelligencePainPoint],
    opps: list[LeadIntelligenceOpportunity]
) -> LeadIntelligenceProfileSchema:
    comp_size_str = str(profile.company_size) if profile.company_size else None
    return LeadIntelligenceProfileSchema(
        lead_id=lead.id,
        company=IntelligenceCompanyProfile(
            name=lead.company_name,
            legal_name=None,
            website=profile.website,
            industry=profile.industry,
            industry_source=profile.industry_source,
            company_size=comp_size_str,
            company_size_source=profile.company_size_source,
            location=profile.location,
            country=profile.country,
            description=profile.description,
            products_services=lead.recommended_services if isinstance(lead.recommended_services, list) else []
        ),
        contact=IntelligenceContactProfile(
            name=lead.decision_maker,
            job_title=getattr(lead, "job_title", None),
            email=lead.email,
            phone=lead.phone,
            location=profile.location
        ),
        digital_presence=IntelligenceDigitalPresence(
            website=profile.website,
            linkedin_url=lead.linkedin_url,
            facebook_url=None,
            instagram_url=None,
            youtube_url=None,
            other_urls=[]
        ),
        business_context=IntelligenceBusinessContext(
            pain_points=[
                IntelligencePainPoint(
                    id=getattr(p, 'id', None) or uuid.uuid4(),
                    title=p.title,
                    description=p.description,
                    source_type=p.source_type.value if hasattr(p.source_type, 'value') else str(p.source_type),
                    source_reference=p.source_reference,
                    confidence=p.confidence.value if hasattr(p.confidence, 'value') else str(p.confidence),
                    created_at=getattr(p, 'created_at', None) or profile.created_at,
                    updated_at=getattr(p, 'updated_at', None) or profile.updated_at,
                )
                for p in pains
            ],
            opportunities=[
                IntelligenceOpportunity(
                    id=getattr(o, 'id', None) or uuid.uuid4(),
                    title=o.title,
                    description=o.description,
                    opportunity_type=o.opportunity_type,
                    opportunity_category=getattr(o, 'opportunity_type', None),
                    recommended_service=(o.evidence.get('recommended_service') if isinstance(o.evidence, dict) else None),
                    commercial_relevance=(o.evidence.get('commercial_relevance') if isinstance(o.evidence, dict) else o.description),
                    suggested_next_action=(o.evidence.get('suggested_next_action') if isinstance(o.evidence, dict) else o.recommended_action),
                    status=(o.evidence.get('status', 'ACTIVE') if isinstance(o.evidence, dict) else 'ACTIVE'),
                    opportunity_status=(o.evidence.get('opportunity_status', 'ACTIVE') if isinstance(o.evidence, dict) else 'ACTIVE'),
                    score=o.score,
                    priority=o.priority,
                    recommended_action=o.recommended_action,
                    evidence=o.evidence or {},
                    evidence_source=(o.evidence.get('evidence_source', str(o.source_reference)) if isinstance(o.evidence, dict) else str(o.source_reference)),
                    source_type=o.source_type.value if hasattr(o.source_type, 'value') else str(o.source_type),
                    source_reference=o.source_reference,
                    confidence=o.confidence.value if hasattr(o.confidence, 'value') else str(o.confidence),
                    last_evaluated=(o.evidence.get('last_evaluated') if isinstance(o.evidence, dict) else (o.updated_at.isoformat() if getattr(o, 'updated_at', None) else None)),
                    created_at=getattr(o, 'created_at', None) or profile.created_at,
                    updated_at=getattr(o, 'updated_at', None) or profile.updated_at,
                )
                for o in opps
            ],
            known_services=lead.recommended_services if isinstance(lead.recommended_services, list) else [],
            known_products=[]
        ),
        intelligence_quality=IntelligenceQuality(
            completeness_score=float(profile.completeness_score) if profile.completeness_score is not None else 0.0,
            last_researched_at=profile.last_researched_at,
            field_count=12,
            populated_field_count=int(float(profile.completeness_score) * 12 / 100) if profile.completeness_score else 0
        )
    )

async def get_lead_intelligence(session: AsyncSession, lead_id: uuid.UUID) -> Optional[LeadIntelligenceProfileSchema]:
    """Retrieve persisted lead intelligence, generating and persisting a unified profile if none exists."""
    lead = await session.get(Lead, lead_id)
    if not lead:
        return None

    result = await session.execute(
        select(LeadIntelligenceProfile).where(LeadIntelligenceProfile.lead_id == lead_id)
    )
    profile = result.scalar_one_or_none()

    if not profile:
        # Reconcile and persist unified intelligence
        return await sync_unified_intelligence_for_lead(session, lead_id)

    # Fetch related
    pains = (await session.scalars(
        select(LeadIntelligencePainPoint).where(LeadIntelligencePainPoint.lead_id == lead_id)
    )).all()

    opps = (await session.scalars(
        select(LeadIntelligenceOpportunity).where(LeadIntelligenceOpportunity.lead_id == lead_id)
    )).all()

    return _to_schema(lead, profile, list(pains), list(opps))

async def refresh_lead_intelligence(session: AsyncSession, lead_id: uuid.UUID) -> Optional[LeadIntelligenceProfileSchema]:
    """Execute evidence-backed refresh and return unified lead intelligence."""
    lead = await session.get(Lead, lead_id)
    if not lead:
        return None

    # Inspect website if available to collect live evidence
    if lead.website:
        try:
            from app.services.website import WebsiteAnalyzer
            from app.models.entities import EnrichmentRun, now_utc
            from app.services.enrichment_pipeline import _website_evidence, _persist_stage_result, EnrichmentStage
            from app.domain.enrichment import LeadInput
            from app.services.intelligence import derive_and_persist_intelligence
            from sqlalchemy.ext.asyncio import async_sessionmaker

            analyzer = WebsiteAnalyzer()
            web_result = await analyzer.analyze(lead.website)
            if isinstance(web_result, dict) and web_result.get("available"):
                now = now_utc()
                run = await session.scalar(
                    select(EnrichmentRun)
                    .where(EnrichmentRun.lead_id == lead_id)
                    .order_by(EnrichmentRun.created_at.desc())
                    .limit(1)
                )
                if not run:
                    run = EnrichmentRun(
                        lead_id=lead_id,
                        processing_job_id=None,
                        status="completed",
                        completed_at=now,
                        started_at=now,
                        pipeline_version="unified-refresh-v1",
                    )
                    session.add(run)
                    await session.commit()

                factory = async_sessionmaker(session.bind, expire_on_commit=False)
                lead_input = LeadInput(
                    lead_id=lead.id,
                    enrichment_run_id=run.id,
                    company_name=lead.company_name,
                    website=lead.website,
                    phone=lead.phone,
                    city=lead.city,
                    state=lead.state,
                    country=lead.country,
                )
                evidence_inputs = _website_evidence(lead_input, web_result)
                await _persist_stage_result(factory, lead.id, run.id, EnrichmentStage.WEBSITE, evidence_inputs, web_result)
                await derive_and_persist_intelligence(factory, lead.id, run.id)
        except Exception as exc:
            logger = logging.getLogger("lead_agent.intelligence")
            logger.debug("Website analysis during refresh non-fatal: %s", exc)

    return await sync_unified_intelligence_for_lead(session, lead_id)

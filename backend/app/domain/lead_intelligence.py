import uuid
from typing import Optional, List, Tuple
from datetime import datetime, UTC
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.models.entities import (
    Lead,
    LeadIntelligenceProfile,
    LeadIntelligencePainPoint,
    LeadIntelligenceOpportunity,
    ConfidenceLevel,
    SourceType
)
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

def _normalize_string(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    cleaned = s.strip()
    return cleaned if cleaned else None

def _normalize_industry(industry: Optional[str]) -> Optional[str]:
    # deterministic mapping based on string match
    if not industry:
        return None
    val = industry.lower().strip()
    mapping = {
        "software": "TECHNOLOGY",
        "it": "TECHNOLOGY",
        "tech": "TECHNOLOGY",
        "technology": "TECHNOLOGY",
        "agency": "AGENCY",
        "marketing": "AGENCY",
        "advertising": "AGENCY",
        "ecommerce": "ECOMMERCE",
        "retail": "RETAIL",
        "finance": "FINANCE",
        "banking": "FINANCE",
        "manufacturing": "MANUFACTURING",
        "healthcare": "HEALTHCARE",
        "medical": "HEALTHCARE",
        "education": "EDUCATION",
        "real estate": "REAL_ESTATE",
    }
    for key, mapped in mapping.items():
        if key in val:
            return mapped
    return "UNKNOWN"

def _normalize_company_size(size: Optional[str]) -> Optional[str]:
    if not size:
        return None
    val = size.lower().strip()
    # Simple deterministic mapping
    if "1-10" in val or "solo" in val or "micro" in val:
        return "MICRO"
    if "11-50" in val or "small" in val:
        return "SMALL"
    if "51-200" in val or "medium" in val:
        return "MEDIUM"
    if "201-1000" in val or "large" in val:
        return "LARGE"
    if "1000+" in val or "enterprise" in val:
        return "ENTERPRISE"
    return "UNKNOWN"

def _calculate_completeness(company: IntelligenceCompanyProfile, contact: IntelligenceContactProfile, dp: IntelligenceDigitalPresence, desc: Optional[str]) -> tuple[float, int, int]:
    required_fields = [
        company.name,
        company.website,
        company.industry,
        company.company_size,
        company.location,
        desc,
        contact.name,
        contact.job_title,
        contact.email,
        contact.phone,
        dp.website or dp.linkedin_url, # digital_presence populated
        company.products_services # products_services populated
    ]
    total_fields = len(required_fields)
    populated = 0
    for field in required_fields:
        if isinstance(field, list):
            if len(field) > 0:
                populated += 1
        elif field and str(field).strip() != "" and field != "UNKNOWN":
            populated += 1

    score = (populated / total_fields) * 100.0 if total_fields > 0 else 0.0
    return score, total_fields, populated

def build_deterministic_profile(lead: Lead) -> tuple[LeadIntelligenceProfile, list[LeadIntelligencePainPoint], list[LeadIntelligenceOpportunity]]:
    now = datetime.now(UTC)

    # Extract Company Profile
    comp_name = _normalize_string(lead.company_name)
    website = _normalize_string(lead.website)
    industry_raw = _normalize_string(lead.industry)
    industry_norm = _normalize_industry(industry_raw) if industry_raw else None

    # We don't have company_size natively on lead, we just use null for now
    comp_size_norm = None

    location_parts = [p for p in [_normalize_string(lead.city), _normalize_string(lead.state), _normalize_string(lead.country)] if p]
    location = ", ".join(location_parts) if location_parts else None

    products_services = lead.recommended_services or []
    if not isinstance(products_services, list): products_services = []

    # Extract Contact Profile
    contact_name = _normalize_string(lead.decision_maker)
    contact_title = None # not on Lead
    contact_email = _normalize_string(lead.email)
    contact_phone = _normalize_string(lead.phone)
    contact_location = location # default to company location for now if not separate

    # Extract Digital Presence
    linkedin = _normalize_string(lead.linkedin_url)

    # Extract Pain Points & Opportunities
    raw_pains = lead.pain_points if isinstance(lead.pain_points, list) else []
    raw_opps = lead.keyword_opportunities if isinstance(lead.keyword_opportunities, list) else []

    pain_points = []
    for p in raw_pains:
        p_str = _normalize_string(str(p))
        if p_str:
            pain_points.append(
                LeadIntelligencePainPoint(
                    id=uuid.uuid4(),
                    lead_id=lead.id,
                    title=p_str[:255],
                    description=None,
                    source_type=SourceType.LEAD_RECORD,
                    source_reference="lead.pain_points",
                    confidence=ConfidenceLevel.HIGH,
                    created_at=now,
                    updated_at=now
                )
            )

    opportunities = []
    for o in raw_opps:
        o_str = _normalize_string(str(o))
        if o_str:
            opportunities.append(
                LeadIntelligenceOpportunity(
                    id=uuid.uuid4(),
                    lead_id=lead.id,
                    title=o_str[:255],
                    description=None,
                    source_type=SourceType.LEAD_RECORD,
                    source_reference="lead.keyword_opportunities",
                    confidence=ConfidenceLevel.HIGH,
                    created_at=now,
                    updated_at=now
                )
            )

    desc = _normalize_string(lead.ai_summary)

    # Calculate Completeness using Pydantic mocks for logic sharing
    comp_mock = IntelligenceCompanyProfile(
        name=comp_name, website=website, industry=industry_norm,
        company_size=comp_size_norm, location=location, products_services=products_services
    )
    cont_mock = IntelligenceContactProfile(
        name=contact_name, job_title=contact_title, email=contact_email, phone=contact_phone
    )
    dp_mock = IntelligenceDigitalPresence(website=website, linkedin_url=linkedin)

    score, t_count, p_count = _calculate_completeness(comp_mock, cont_mock, dp_mock, desc)

    profile = LeadIntelligenceProfile(
        id=uuid.uuid4(),
        lead_id=lead.id,
        industry=industry_norm,
        industry_source=SourceType.LEAD_RECORD if industry_raw else None,
        industry_confidence=ConfidenceLevel.MEDIUM if industry_norm != "UNKNOWN" and industry_norm else ConfidenceLevel.UNKNOWN,
        company_size=comp_size_norm,
        company_size_source=None,
        company_size_confidence=ConfidenceLevel.UNKNOWN,
        website=website,
        location=location,
        country=_normalize_string(lead.country),
        description=desc,
        last_researched_at=now,
        completeness_score=score,
        created_at=now,
        updated_at=now
    )

    return profile, pain_points, opportunities

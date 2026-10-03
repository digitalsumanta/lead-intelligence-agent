import uuid
from datetime import datetime, UTC
from typing import Optional, Dict, Any, List, Tuple

from app.models.entities import (
    Lead,
    CompanyEnrichmentProfile,
    WebsiteIntelligenceProfile,
    SourceType,
    ConfidenceLevel,
    DigitalMarketingIntelligenceProfile
)

MATURITY_LEVELS = ["UNKNOWN", "FOUNDATIONAL", "DEVELOPING", "ESTABLISHED", "ADVANCED"]

def normalize_marketing_signal(signal_data: Any) -> Optional[Dict[str, Any]]:
    if not signal_data:
        return None
    if isinstance(signal_data, dict) and len(signal_data) > 0:
        return signal_data
    if isinstance(signal_data, list) and len(signal_data) > 0:
        return {"items": signal_data}
    return None

def normalize_signal_source(source: str) -> str:
    return source.strip().upper() if source else "UNKNOWN"

def derive_marketing_maturity(has_seo: bool, has_paid: bool, has_social: bool, has_content: bool) -> str:
    count = sum([has_seo, has_paid, has_social, has_content])
    if count == 0:
        return "UNKNOWN"
    elif count == 1:
        return "FOUNDATIONAL"
    elif count == 2:
        return "DEVELOPING"
    elif count == 3:
        return "ESTABLISHED"
    else:
        return "ADVANCED"

def derive_marketing_gaps(has_seo: bool, has_paid: bool, has_social: bool, has_content: bool) -> List[str]:
    gaps = []
    if not has_seo:
        gaps.append("No persisted SEO signal found")
    if not has_paid:
        gaps.append("No persisted paid-advertising signal found")
    if not has_social:
        gaps.append("No persisted social-channel signal found")
    if not has_content:
        gaps.append("No persisted content-marketing signal found")
    return gaps

def derive_marketing_opportunities(has_seo: bool, has_paid: bool, has_social: bool, has_content: bool) -> List[str]:
    ops = []
    if not has_seo:
        ops.append("SEO opportunity signal")
    if not has_paid:
        ops.append("Paid acquisition opportunity signal")
    if not has_social:
        ops.append("Social presence opportunity signal")
    if not has_content:
        ops.append("Content marketing opportunity signal")
        
    count = sum([has_seo, has_paid, has_social, has_content])
    if count >= 2:
        ops.append("Cross-channel marketing opportunity signal")
        
    return ops

def calculate_digital_marketing_completeness(has_seo: bool, has_paid: bool, has_social: bool, has_content: bool) -> float:
    return float(sum([has_seo, has_paid, has_social, has_content]) * 25.0)

def build_digital_marketing_profile(
    lead: Lead, 
    enrichment: Optional[CompanyEnrichmentProfile] = None,
    website_intel: Optional[WebsiteIntelligenceProfile] = None,
    has_seo_run: bool = False
) -> DigitalMarketingIntelligenceProfile:
    
    # Extract social signals from enrichment or lead
    social_data = {}
    if enrichment and enrichment.linkedin_url:
        social_data["linkedin_present"] = True
        social_data["linkedin_url"] = enrichment.linkedin_url
        
    if lead.website_analysis and isinstance(lead.website_analysis, dict):
        socials = lead.website_analysis.get("social_links", [])
        if socials:
            social_data["social_links"] = socials

    social_media_signals = normalize_marketing_signal(social_data)
    
    # Extract SEO signals
    seo_data = {}
    if has_seo_run:
        seo_data["seo_audit_present"] = True
    if lead.website_analysis and isinstance(lead.website_analysis, dict):
        if "meta_description" in lead.website_analysis:
            seo_data["seo_metadata_present"] = True
            
    seo_signals = normalize_marketing_signal(seo_data)
    
    # Extract Paid Advertising signals
    paid_data = {}
    if website_intel and website_intel.technology_signals:
        techs = website_intel.technology_signals.get("technologies", [])
        if any("ads" in t.lower() or "pixel" in t.lower() for t in techs):
            paid_data["advertising_account_signal"] = True
    paid_advertising_signals = normalize_marketing_signal(paid_data)
    
    # Extract Content Marketing signals
    content_data = {}
    if lead.ai_analysis:
        content_data["content_strategy_signal"] = True
    content_marketing_signals = normalize_marketing_signal(content_data)
    
    # Booleans for derivations
    has_seo = bool(seo_signals)
    has_paid = bool(paid_advertising_signals)
    has_social = bool(social_media_signals)
    has_content = bool(content_marketing_signals)
    
    maturity = derive_marketing_maturity(has_seo, has_paid, has_social, has_content)
    gaps = derive_marketing_gaps(has_seo, has_paid, has_social, has_content)
    opportunities = derive_marketing_opportunities(has_seo, has_paid, has_social, has_content)
    completeness = calculate_digital_marketing_completeness(has_seo, has_paid, has_social, has_content)
    
    if completeness >= 75:
        confidence = ConfidenceLevel.HIGH
    elif completeness >= 25:
        confidence = ConfidenceLevel.MEDIUM
    else:
        confidence = ConfidenceLevel.UNKNOWN
    
    now = datetime.now(UTC)
    return DigitalMarketingIntelligenceProfile(
        id=uuid.uuid4(),
        company_id=lead.id,
        seo_signals=seo_signals,
        paid_advertising_signals=paid_advertising_signals,
        social_media_signals=social_media_signals,
        content_marketing_signals=content_marketing_signals,
        marketing_maturity=maturity,
        marketing_gaps=gaps,
        marketing_opportunities=opportunities,
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=confidence,
        completeness=completeness,
        created_at=now,
        updated_at=now
    )

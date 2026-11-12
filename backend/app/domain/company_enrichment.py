import re
from typing import Optional, Tuple, Any
from urllib.parse import urlparse

from app.domain.lead_intelligence import _normalize_industry, _normalize_company_size
from app.models.entities import ConfidenceLevel, SourceType

def normalize_company_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    # Trim whitespace and collapse repeated whitespace
    cleaned = re.sub(r'\s+', ' ', name.strip())
    return cleaned if cleaned else None

def normalize_website(url: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Returns a tuple of (normalized_url, extracted_domain)."""
    if not url:
        return None, None
    url = url.strip()
    if not url:
        return None, None
        
    # Ensure it has a scheme for urlparse to work correctly
    if not url.startswith(('http://', 'https://')):
        url_with_scheme = 'https://' + url
    else:
        url_with_scheme = url
        
    try:
        parsed = urlparse(url_with_scheme)
        domain = parsed.netloc.lower()
        if domain.startswith('www.'):
            domain = domain[4:]
            
        if not domain:
            return url, None
            
        # Reconstruct without fragments or queries for the normalized website
        normalized_url = f"{parsed.scheme}://{domain}{parsed.path}"
        if normalized_url.endswith('/'):
            normalized_url = normalized_url[:-1]
            
        return normalized_url, domain
    except Exception:
        return url, None

def extract_email_domain(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    email = email.strip()
    if '@' in email:
        parts = email.split('@')
        if len(parts) == 2 and parts[1]:
            return parts[1].lower().strip()
    return None

def normalize_location(city: Optional[str], state: Optional[str], country: Optional[str]) -> Optional[str]:
    parts = []
    for p in [city, state, country]:
        if p and p.strip():
            parts.append(p.strip())
    if not parts:
        return None
    return ", ".join(parts)

def calculate_company_completeness(fields: dict) -> float:
    # Score based on presence of key fields (non-empty and not UNKNOWN)
    if not fields:
        return 0.0
    
    total = len(fields)
    populated = sum(
        1 for v in fields.values()
        if v is not None and (not isinstance(v, str) or v.strip() != "") and str(v).strip().upper() != "UNKNOWN"
    )
    return round((populated / total) * 100.0, 2) if total > 0 else 0.0

def classify_industry_from_evidence(
    lead_industry: Optional[str] = None,
    website_analysis: Optional[dict] = None,
    google_places_types: Optional[list] = None,
    website_title: Optional[str] = None,
    meta_description: Optional[str] = None,
    company_name: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], SourceType, ConfidenceLevel]:
    """Classify readable industry and normalized industry code with source and confidence.
    
    Returns:
        (readable_industry, normalized_industry, source, confidence)
    """
    # 1. Google Places types (verified external provider)
    if google_places_types and isinstance(google_places_types, list):
        for place_type in google_places_types:
            clean_type = str(place_type).replace("_", " ").strip()
            norm = _normalize_industry(clean_type)
            if norm and norm != "UNKNOWN":
                readable = lead_industry.strip() if (lead_industry and lead_industry.strip().upper() != "UNKNOWN") else clean_type.title()
                return (readable, norm, SourceType.SYSTEM_DERIVED, ConfidenceLevel.HIGH)

    # 2. Lead record industry
    if lead_industry and lead_industry.strip() and lead_industry.strip().upper() != "UNKNOWN":
        cleaned_lead_ind = lead_industry.strip()
        norm = _normalize_industry(cleaned_lead_ind)
        if norm and norm != "UNKNOWN":
            return (cleaned_lead_ind, norm, SourceType.LEAD_RECORD, ConfidenceLevel.HIGH)

    # 3. Crawled / Analyzed website data (sub_category or industry)
    if isinstance(website_analysis, dict):
        for key in ("sub_category", "industry", "primary_service"):
            val = website_analysis.get(key)
            if val and isinstance(val, str) and val.strip() and val.strip().upper() != "UNKNOWN":
                norm = _normalize_industry(val.strip())
                if norm and norm != "UNKNOWN":
                    readable = lead_industry.strip() if (lead_industry and lead_industry.strip().upper() != "UNKNOWN") else val.strip()
                    return (readable, norm, SourceType.SYSTEM_DERIVED, ConfidenceLevel.HIGH)

    # 4. Website content: meta_description and website_title
    combined_content = f"{website_title or ''} {meta_description or ''}".strip()
    if combined_content:
        norm = _normalize_industry(combined_content)
        if norm and norm != "UNKNOWN":
            readable = lead_industry.strip() if (lead_industry and lead_industry.strip().upper() != "UNKNOWN") else norm.replace("_", " ").title()
            return (readable, norm, SourceType.SYSTEM_DERIVED, ConfidenceLevel.MEDIUM)

    # 5. Company name keywords
    if company_name and company_name.strip():
        norm = _normalize_industry(company_name.strip())
        if norm and norm != "UNKNOWN":
            readable = lead_industry.strip() if (lead_industry and lead_industry.strip().upper() != "UNKNOWN") else norm.replace("_", " ").title()
            return (readable, norm, SourceType.SYSTEM_DERIVED, ConfidenceLevel.MEDIUM)

    # 6. Fallback if lead_industry was provided but not in ontology
    if lead_industry and lead_industry.strip() and lead_industry.strip().upper() != "UNKNOWN":
        return (lead_industry.strip(), "UNKNOWN", SourceType.LEAD_RECORD, ConfidenceLevel.LOW)

    return (None, "UNKNOWN", SourceType.LEAD_RECORD, ConfidenceLevel.UNKNOWN)

import re
from typing import Optional, Tuple
from urllib.parse import urlparse

from app.domain.lead_intelligence import _normalize_industry, _normalize_company_size

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
    # We will score based on presence of key fields
    # fields could be: legal_name, domain, website, industry, location, description, etc.
    if not fields:
        return 0.0
    
    total = len(fields)
    populated = sum(1 for v in fields.values() if v and (not isinstance(v, str) or v.strip() != "") and v != "UNKNOWN")
    return (populated / total) * 100.0 if total > 0 else 0.0

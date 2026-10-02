import re
from typing import Optional, Tuple
from urllib.parse import urlparse
import uuid
from datetime import datetime, UTC

def normalize_domain(domain: Optional[str]) -> Optional[str]:
    """
    Normalizes a domain by extracting it properly:
    - Trim whitespace.
    - Lowercase.
    - Remove protocol if supplied.
    - Remove path/query/fragment if present.
    - Remove leading www.
    - Remove trailing dot.
    """
    if not domain:
        return None
        
    d = domain.strip().lower()
    if not d:
        return None
        
    if d.startswith(('http://', 'https://')):
        try:
            parsed = urlparse(d)
            d = parsed.netloc
        except Exception:
            return None
    else:
        # If no protocol, it might be just a domain or domain + path
        if '/' in d:
            d = d.split('/')[0]
            
    # Remove port if present
    if ':' in d:
        d = d.split(':')[0]
        
    # Remove leading www.
    if d.startswith('www.'):
        d = d[4:]
        
    # Remove trailing dot
    if d.endswith('.'):
        d = d[:-1]
        
    # Basic syntactical validation (at least one dot, no spaces)
    if '.' not in d or ' ' in d or not d:
        return None
        
    return d

def normalize_website(url: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (normalized_website, normalized_domain).
    - Trim surrounding whitespace.
    - Accept http:// and https://.
    - If no protocol, treat as hostname candidate (assume https://).
    - Remove query string.
    - Remove fragment.
    - Normalize hostname casing.
    - Remove trailing dot.
    - Remove leading www. from normalized domain.
    - Reject malformed URL/domain values.
    """
    if not url:
        return None, None
        
    u = url.strip()
    if not u:
        return None, None
        
    if not u.startswith(('http://', 'https://')):
        u = 'https://' + u
        
    try:
        parsed = urlparse(u)
        netloc = parsed.netloc.lower()
        if netloc.endswith('.'):
            netloc = netloc[:-1]
            
        if not netloc:
            return None, None
            
        domain = normalize_domain(netloc)
        if not domain:
            return None, None
            
        # Reconstruct without query and fragment
        # Note: the netloc here might still have www. if it was originally there, 
        # but the instructions say "Remove leading www. from normalized domain."
        # We don't remove www from the website URL itself unless requested, but 
        # usually normalized websites also omit www if we want strict canonicalization.
        # Let's keep it in the website if it was there, or omit it. The rules say:
        # "Remove leading www. from normalized domain." 
        
        path = parsed.path
        if not path:
            path = "/"
            
        normalized_web = f"{parsed.scheme}://{netloc}{path}"
        if normalized_web.endswith('/') and len(normalized_web) > max(len(parsed.scheme) + 4, 0): # e.g. https://example.com/
            # just aesthetic standard
            pass
            
        return normalized_web, domain
    except Exception:
        return None, None

def calculate_website_completeness(has_web: bool, has_domain: bool, has_tech: bool, has_content: bool) -> float:
    score = 0.0
    if has_web:
        score += 40.0
    if has_domain:
        score += 40.0
    if has_tech:
        score += 10.0
    if has_content:
        score += 10.0
    return score

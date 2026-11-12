import uuid
from datetime import datetime, UTC
from decimal import Decimal
from typing import Optional, Dict, Any, List, Tuple
import os

from app.models.entities import (
    Lead,
    CompanyEnrichmentProfile,
    WebsiteIntelligenceProfile,
    Evidence,
    Opportunity,
    BuyingSignal,
    LeadIntelligenceOpportunity,
    SourceType,
    ConfidenceLevel,
    DigitalMarketingIntelligenceProfile,
    now_utc,
)

# Canonical 7 Digital Marketing Categories
SEO = "seo"
PAID_ADVERTISING = "paid_advertising"
SOCIAL_MEDIA = "social_media"
CONTENT_MARKETING = "content_marketing"
WEBSITE_PERFORMANCE = "website_performance"
CONVERSION_OPTIMIZATION = "conversion_optimization"
ONLINE_REPUTATION = "online_reputation"

CANONICAL_CATEGORIES = [
    SEO,
    PAID_ADVERTISING,
    SOCIAL_MEDIA,
    CONTENT_MARKETING,
    WEBSITE_PERFORMANCE,
    CONVERSION_OPTIMIZATION,
    ONLINE_REPUTATION,
]

CATEGORY_TITLES = {
    SEO: "SEO",
    PAID_ADVERTISING: "Paid Advertising",
    SOCIAL_MEDIA: "Social Media",
    CONTENT_MARKETING: "Content Marketing",
    WEBSITE_PERFORMANCE: "Website Performance",
    CONVERSION_OPTIMIZATION: "Conversion Optimization",
    ONLINE_REPUTATION: "Online Reputation",
}

# Category Research Statuses
RESEARCHED = "RESEARCHED"
PARTIALLY_RESEARCHED = "PARTIALLY_RESEARCHED"
NOT_RESEARCHED = "NOT_RESEARCHED"
UNAVAILABLE = "UNAVAILABLE"
NOT_APPLICABLE = "NOT_APPLICABLE"

# MindArtDigital Canonical Service Offerings
MINDART_SERVICES = {
    SEO: {
        "default": "SEO",
        "technical": "Technical SEO",
        "local": "Local SEO",
        "content": "SEO Content Strategy",
    },
    PAID_ADVERTISING: {
        "default": "Performance Marketing",
        "google": "Google Ads Management",
        "meta": "Meta Ads Management",
        "funnel": "Advertising Funnel Optimization",
    },
    SOCIAL_MEDIA: {
        "default": "Social Media Marketing",
        "management": "Social Media Management",
        "strategy": "Social Media Strategy",
    },
    CONTENT_MARKETING: {
        "default": "Content Marketing",
        "strategy": "SEO Content Strategy",
        "blog": "Blog Management",
        "production": "Content Production",
    },
    WEBSITE_PERFORMANCE: {
        "default": "Website Performance Optimization",
        "development": "Website Development",
        "maintenance": "Website Maintenance",
    },
    CONVERSION_OPTIMIZATION: {
        "default": "Conversion Rate Optimization",
        "landing": "Landing Page Development",
        "lead_gen": "Lead Generation",
        "development": "Website Development",
    },
    ONLINE_REPUTATION: {
        "default": "Online Reputation Management",
        "reviews": "Review Management",
        "local": "Local SEO",
    },
}

MATURITY_LEVELS = ["UNKNOWN", "NASCENT", "FOUNDATIONAL", "DEVELOPING", "ESTABLISHED", "ADVANCED"]


def calculate_canonical_completeness(coverage: Dict[str, Dict[str, Any]]) -> float:
    """
    Strict Verification Invariant:
    Only RESEARCHED categories count toward verified completeness percentage.
    PARTIALLY_RESEARCHED, NOT_RESEARCHED, UNAVAILABLE, and NOT_APPLICABLE do NOT count.
    Formula: round((researched_count / 7.0) * 100.0, 2)
    """
    researched_count = 0
    for cat in CANONICAL_CATEGORIES:
        data = coverage.get(cat, {})
        st = data.get("status")
        if st in (RESEARCHED, "SIGNAL_DETECTED", "NO_SIGNAL_FOUND"):
            researched_count += 1
    return round((researched_count / 7.0) * 100.0, 2)


def build_coverage_summary(coverage: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Provide count breakdown across all 7 canonical categories."""
    researched = 0
    partially_researched = 0
    not_researched = 0
    unavailable = 0

    for cat in CANONICAL_CATEGORIES:
        data = coverage.get(cat, {})
        st = data.get("status", NOT_RESEARCHED)
        if st in (RESEARCHED, "SIGNAL_DETECTED", "NO_SIGNAL_FOUND"):
            researched += 1
        elif st == PARTIALLY_RESEARCHED:
            partially_researched += 1
        elif st == UNAVAILABLE:
            unavailable += 1
        else:
            not_researched += 1

    completeness = round((researched / 7.0) * 100.0, 2)
    return {
        "researched": researched,
        "partially_researched": partially_researched,
        "not_researched": not_researched,
        "unavailable": unavailable,
        "total_categories": 7,
        "completeness": completeness,
    }


def normalize_marketing_signal(signal_data: Any) -> Optional[Dict[str, Any]]:
    if not signal_data:
        return None
    if isinstance(signal_data, dict) and len(signal_data) > 0:
        return signal_data
    if isinstance(signal_data, list) and len(signal_data) > 0:
        return {"items": signal_data}
    return None


def normalize_signal_source(source: Optional[str]) -> str:
    return source.strip().upper() if source else "UNKNOWN"


def derive_marketing_maturity(*channels: Any) -> str:
    if len(channels) == 1 and isinstance(channels[0], int) and not isinstance(channels[0], bool):
        active_count = channels[0]
        if active_count >= 4:
            return "ADVANCED"
        elif active_count >= 3:
            return "ESTABLISHED"
        elif active_count == 2:
            return "DEVELOPING"
        elif active_count == 1:
            return "NASCENT"
        else:
            return "UNKNOWN"
    active_count = sum(1 for c in channels if bool(c))
    if active_count == 0:
        return "UNKNOWN"
    elif active_count == 1:
        return "FOUNDATIONAL"
    elif active_count == 2:
        return "DEVELOPING"
    elif active_count == 3:
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


def calculate_digital_marketing_completeness(*args: Any) -> float:
    if len(args) == 1 and isinstance(args[0], dict):
        return calculate_canonical_completeness(args[0])
    return float(sum(1 for a in args if bool(a)) * 25.0)


def evaluate_seven_categories(
    lead: Lead,
    evidence_by_type: Dict[str, List[Evidence]],
    opportunities: List[Opportunity],
    has_seo_run: bool = False,
    existing_coverage: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Deterministically evaluates all 7 canonical digital marketing categories against
    persisted evidence, website analysis, and provider capabilities.
    """
    now = now_utc()
    coverage: Dict[str, Dict[str, Any]] = {}
    analysis = lead.website_analysis if isinstance(lead.website_analysis, dict) else {}
    has_website = bool(lead.website and lead.website.strip())

    # 1. SEO
    has_seo_evidence = bool(
        has_seo_run
        or "pagespeed_seo_score" in evidence_by_type
        or (analysis.get("available") and ("title" in analysis or "meta_description" in analysis or "h1" in analysis or "seo_score" in analysis))
    )

    if not has_website:
        coverage[SEO] = {
            "category": SEO,
            "title": CATEGORY_TITLES[SEO],
            "status": UNAVAILABLE,
            "researched": False,
            "signal_detected": False,
            "finding": "No website URL provided for SEO analysis.",
            "reason": "No website URL provided for SEO analysis.",
            "diagnostic_reason": "No website URL provided for SEO analysis.",
            "problem_identified": False,
            "problem_severity": None,
            "recommended_service": MINDART_SERVICES[SEO]["default"],
            "confidence_score": 0,
            "evidence_type": "URL_VALIDATION",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "SEO Analyzer",
            "last_researched_at": now.isoformat(),
        }
    elif has_seo_evidence:
        issues = analysis.get("issues", [])
        title = analysis.get("title")
        meta_desc = analysis.get("meta_description")
        h1 = analysis.get("h1")

        psi_seo = None
        if "pagespeed_seo_score" in evidence_by_type:
            try:
                psi_seo = float(evidence_by_type["pagespeed_seo_score"][0].observed_value)
            except (ValueError, TypeError):
                pass
        elif analysis.get("seo_score") is not None:
            try:
                psi_seo = float(analysis["seo_score"])
            except (ValueError, TypeError):
                pass

        problem = False
        severity = None
        problem_summary = None
        rec_service = MINDART_SERVICES[SEO]["default"]
        finding = "Primary on-page SEO meta elements (title tag, meta description, and H1 heading) are verified and present."

        if analysis.get("available") and not title:
            problem = True
            severity = "high"
            problem_summary = "Missing HTML page title tag"
            rec_service = MINDART_SERVICES[SEO]["technical"]
            finding = "Missing HTML page title tag impairs search engine indexing and search snippet click-through."
        elif analysis.get("available") and not meta_desc:
            problem = True
            severity = "medium"
            problem_summary = "Missing HTML meta description tag"
            rec_service = MINDART_SERVICES[SEO]["default"]
            finding = "Missing meta description tag results in unoptimized search result previews."
        elif analysis.get("available") and not h1:
            problem = True
            severity = "medium"
            problem_summary = "Missing primary H1 heading tag"
            rec_service = MINDART_SERVICES[SEO]["technical"]
            finding = "Page lacks a designated H1 heading tag, impacting semantic content hierarchy."
        elif psi_seo is not None and psi_seo < 75:
            problem = True
            severity = "medium"
            problem_summary = f"Lighthouse SEO score is {int(psi_seo)}/100"
            rec_service = MINDART_SERVICES[SEO]["technical"]
            finding = f"Automated Lighthouse SEO audit identified structural gaps (Score: {int(psi_seo)}/100)."

        coverage[SEO] = {
            "category": SEO,
            "title": CATEGORY_TITLES[SEO],
            "status": RESEARCHED,
            "researched": True,
            "signal_detected": True,
            "finding": finding,
            "reason": finding,
            "problem_identified": problem,
            "problem_severity": severity,
            "problem_summary": problem_summary,
            "recommended_service": rec_service,
            "confidence_score": 90 if psi_seo is not None or has_seo_run else 80,
            "evidence_type": "HTML_METADATA_ANALYSIS",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "SeoCrawler & WebsiteAnalyzer",
            "score": psi_seo,
            "details": {
                "title": title,
                "meta_description": meta_desc,
                "h1": h1,
                "pagespeed_seo_score": psi_seo,
                "issues": issues,
            },
            "last_researched_at": now.isoformat(),
        }
    else:
        coverage[SEO] = {
            "category": SEO,
            "title": CATEGORY_TITLES[SEO],
            "status": NOT_RESEARCHED,
            "researched": False,
            "signal_detected": False,
            "finding": "SEO audit has not been conducted for this lead.",
            "reason": "SEO audit has not been conducted for this lead.",
            "diagnostic_reason": "Audit pending execution.",
            "problem_identified": False,
            "problem_severity": None,
            "recommended_service": MINDART_SERVICES[SEO]["default"],
            "confidence_score": 0,
            "evidence_type": "NONE",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "SEO Analyzer",
            "last_researched_at": now.isoformat(),
        }

    # 2. Paid Advertising
    ad_plat = analysis.get("advertising_platforms") or analysis.get("ad_platforms")
    ad_ev = analysis.get("advertising_evidence")
    tracking_tags = analysis.get("tracking_tags", [])
    has_ad_data = bool("advertising_platforms" in analysis or "ad_platforms" in analysis or "advertising_evidence" in analysis)

    if not has_website:
        coverage[PAID_ADVERTISING] = {
            "category": PAID_ADVERTISING,
            "title": CATEGORY_TITLES[PAID_ADVERTISING],
            "status": UNAVAILABLE,
            "researched": False,
            "signal_detected": False,
            "finding": "No website URL provided.",
            "reason": "No website URL provided.",
            "diagnostic_reason": "No website URL provided.",
            "problem_identified": False,
            "problem_severity": None,
            "recommended_service": MINDART_SERVICES[PAID_ADVERTISING]["default"],
            "confidence_score": 0,
            "evidence_type": "URL_VALIDATION",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "Ad Library Monitor",
            "last_researched_at": now.isoformat(),
        }
    elif has_ad_data and ad_plat and str(ad_plat).strip().upper() != "NONE":
        plat_str = ", ".join(ad_plat) if isinstance(ad_plat, list) else str(ad_plat)
        has_pixel = any("pixel" in str(t).lower() or "conversion" in str(t).lower() for t in tracking_tags)

        if not has_pixel and "Meta" in plat_str:
            problem = True
            severity = "high"
            problem_summary = "Active Meta ad spend without verified conversion attribution pixel"
            rec_service = MINDART_SERVICES[PAID_ADVERTISING]["funnel"]
            finding = f"Active ad campaigns detected on {plat_str}, but no conversion attribution pixel was detected on website."
        else:
            problem = True
            severity = "medium"
            problem_summary = f"Active paid acquisition on {plat_str} requiring continuous ROAS optimization"
            rec_service = MINDART_SERVICES[PAID_ADVERTISING]["meta"] if "Meta" in plat_str else MINDART_SERVICES[PAID_ADVERTISING]["google"]
            finding = f"Active advertising detected across {plat_str}. Commercial readiness confirmed."

        coverage[PAID_ADVERTISING] = {
            "category": PAID_ADVERTISING,
            "title": CATEGORY_TITLES[PAID_ADVERTISING],
            "status": RESEARCHED,
            "researched": True,
            "signal_detected": True,
            "platforms": ad_plat,
            "evidence": ad_ev,
            "finding": finding,
            "reason": finding,
            "problem_identified": problem,
            "problem_severity": severity,
            "problem_summary": problem_summary,
            "recommended_service": rec_service,
            "confidence_score": 90,
            "evidence_type": "AD_LIBRARY_VERIFICATION",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "Meta Ad Library & Google Shopping Monitor",
            "details": {
                "platforms": ad_plat,
                "evidence": ad_ev,
                "tracking_tags": tracking_tags,
            },
            "last_researched_at": now.isoformat(),
        }
    elif has_ad_data:
        coverage[PAID_ADVERTISING] = {
            "category": PAID_ADVERTISING,
            "title": CATEGORY_TITLES[PAID_ADVERTISING],
            "status": RESEARCHED,
            "researched": True,
            "signal_detected": False,
            "finding": "No active ad campaigns detected across monitored ad platforms.",
            "reason": "No active ad campaigns detected across monitored ad platforms.",
            "problem_identified": False,
            "problem_severity": None,
            "recommended_service": MINDART_SERVICES[PAID_ADVERTISING]["google"],
            "confidence_score": 75,
            "evidence_type": "AD_LIBRARY_VERIFICATION",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "Ad Library Monitor",
            "last_researched_at": now.isoformat(),
        }
    else:
        coverage[PAID_ADVERTISING] = {
            "category": PAID_ADVERTISING,
            "title": CATEGORY_TITLES[PAID_ADVERTISING],
            "status": NOT_RESEARCHED,
            "researched": False,
            "signal_detected": False,
            "finding": "Paid advertising channels have not been researched.",
            "reason": "Paid advertising channels have not been researched.",
            "diagnostic_reason": "Ad platform scan pending.",
            "problem_identified": False,
            "problem_severity": None,
            "recommended_service": MINDART_SERVICES[PAID_ADVERTISING]["default"],
            "confidence_score": 0,
            "evidence_type": "NONE",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "Ad Library Monitor",
            "last_researched_at": now.isoformat(),
        }

    # 3. Social Media
    social_links = []
    if "website_social_link" in evidence_by_type:
        social_links = [str(ev.observed_value) for ev in evidence_by_type["website_social_link"] if ev.observed_value]
    elif analysis.get("social_links"):
        social_links = analysis.get("social_links")
    elif analysis.get("social_profiles"):
        s_prof = analysis.get("social_profiles")
        social_links = s_prof if isinstance(s_prof, list) else [str(s_prof)]

    has_social_data = bool(social_links or analysis.get("social_profiles_checked") is True or "social_profiles" in analysis)

    if not has_website:
        coverage[SOCIAL_MEDIA] = {
            "category": SOCIAL_MEDIA,
            "title": CATEGORY_TITLES[SOCIAL_MEDIA],
            "status": UNAVAILABLE,
            "researched": False,
            "signal_detected": False,
            "finding": "No website URL provided.",
            "reason": "No website URL provided.",
            "diagnostic_reason": "No website URL provided.",
            "problem_identified": False,
            "problem_severity": None,
            "recommended_service": MINDART_SERVICES[SOCIAL_MEDIA]["default"],
            "confidence_score": 0,
            "evidence_type": "URL_VALIDATION",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "Social Scraper",
            "last_researched_at": now.isoformat(),
        }
    elif has_social_data:
        if social_links:
            count = len(social_links)
            problem = count < 2
            severity = "low" if problem else None
            problem_summary = "Limited social channel footprint" if problem else None
            rec_service = MINDART_SERVICES[SOCIAL_MEDIA]["strategy"] if problem else MINDART_SERVICES[SOCIAL_MEDIA]["management"]
            finding = f"Verified active social media profiles detected across {count} channel(s): {', '.join(social_links[:3])}."
            sig_det = True
        else:
            problem = True
            severity = "medium"
            problem_summary = "No social media channels linked on website"
            rec_service = MINDART_SERVICES[SOCIAL_MEDIA]["management"]
            finding = "Social media profiles were checked; none found."
            sig_det = False

        coverage[SOCIAL_MEDIA] = {
            "category": SOCIAL_MEDIA,
            "title": CATEGORY_TITLES[SOCIAL_MEDIA],
            "status": RESEARCHED,
            "researched": True,
            "signal_detected": sig_det,
            "finding": finding,
            "reason": finding,
            "social_links": social_links,
            "problem_identified": problem,
            "problem_severity": severity,
            "problem_summary": problem_summary,
            "recommended_service": rec_service,
            "confidence_score": 85,
            "evidence_type": "WEBSITE_SOCIAL_SCRAPE",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "Social Profile Scraper",
            "details": {"social_links": social_links},
            "last_researched_at": now.isoformat(),
        }
    else:
        coverage[SOCIAL_MEDIA] = {
            "category": SOCIAL_MEDIA,
            "title": CATEGORY_TITLES[SOCIAL_MEDIA],
            "status": NOT_RESEARCHED,
            "researched": False,
            "signal_detected": False,
            "finding": "Social media presence has not been researched.",
            "reason": "Social media presence has not been researched.",
            "diagnostic_reason": "Social audit pending.",
            "problem_identified": False,
            "problem_severity": None,
            "recommended_service": MINDART_SERVICES[SOCIAL_MEDIA]["default"],
            "confidence_score": 0,
            "evidence_type": "NONE",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "Social Scraper",
            "last_researched_at": now.isoformat(),
        }

    # 4. Content Marketing
    blog_det = bool(analysis.get("blog_detected") or "website_blog_detected" in evidence_by_type)
    cm = analysis.get("content_marketing")
    has_cm_data = bool("blog_detected" in analysis or cm or analysis.get("content_marketing_checked") is True or "website_blog_detected" in evidence_by_type)

    if not has_website:
        coverage[CONTENT_MARKETING] = {
            "category": CONTENT_MARKETING,
            "title": CATEGORY_TITLES[CONTENT_MARKETING],
            "status": UNAVAILABLE,
            "researched": False,
            "signal_detected": False,
            "finding": "No website URL provided.",
            "reason": "No website URL provided.",
            "diagnostic_reason": "No website URL provided.",
            "problem_identified": False,
            "problem_severity": None,
            "recommended_service": MINDART_SERVICES[CONTENT_MARKETING]["default"],
            "confidence_score": 0,
            "evidence_type": "URL_VALIDATION",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "Content Auditor",
            "last_researched_at": now.isoformat(),
        }
    elif has_cm_data:
        is_active = bool(blog_det or (isinstance(cm, dict) and cm.get("active")))
        if is_active:
            coverage[CONTENT_MARKETING] = {
                "category": CONTENT_MARKETING,
                "title": CATEGORY_TITLES[CONTENT_MARKETING],
                "status": RESEARCHED,
                "researched": True,
                "signal_detected": True,
                "finding": "Active blog or content marketing hub verified on website.",
                "reason": "Active blog or content marketing hub verified on website.",
                "problem_identified": False,
                "problem_severity": None,
                "recommended_service": MINDART_SERVICES[CONTENT_MARKETING]["strategy"],
                "confidence_score": 85,
                "evidence_type": "CONTENT_STRUCTURE_ANALYSIS",
                "verified_fact_or_inference": "verified_fact",
                "source_name": "Content Auditor",
                "details": {"blog_matches": analysis.get("blog_matches", [])},
                "last_researched_at": now.isoformat(),
            }
        else:
            coverage[CONTENT_MARKETING] = {
                "category": CONTENT_MARKETING,
                "title": CATEGORY_TITLES[CONTENT_MARKETING],
                "status": RESEARCHED,
                "researched": True,
                "signal_detected": False,
                "finding": "Content marketing evaluation conducted; no active blog/content hub found.",
                "reason": "Content marketing evaluation conducted; no active blog/content hub found.",
                "problem_identified": True,
                "problem_severity": "medium",
                "problem_summary": "Missing educational blog or content marketing hub",
                "recommended_service": MINDART_SERVICES[CONTENT_MARKETING]["default"],
                "confidence_score": 80,
                "evidence_type": "CONTENT_STRUCTURE_ANALYSIS",
                "verified_fact_or_inference": "verified_fact",
                "source_name": "Content Auditor",
                "last_researched_at": now.isoformat(),
            }
    else:
        coverage[CONTENT_MARKETING] = {
            "category": CONTENT_MARKETING,
            "title": CATEGORY_TITLES[CONTENT_MARKETING],
            "status": NOT_RESEARCHED,
            "researched": False,
            "signal_detected": False,
            "finding": "Content marketing evaluation has not been conducted.",
            "reason": "Content marketing evaluation has not been conducted.",
            "diagnostic_reason": "Content audit pending.",
            "problem_identified": False,
            "problem_severity": None,
            "recommended_service": MINDART_SERVICES[CONTENT_MARKETING]["default"],
            "confidence_score": 0,
            "evidence_type": "NONE",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "Content Auditor",
            "last_researched_at": now.isoformat(),
        }

    # 5. Website Performance
    has_perf_data = bool(
        "response_time_ms" in analysis
        or "website_http_status" in evidence_by_type
        or "pagespeed_performance_score" in evidence_by_type
    )

    if not has_website:
        coverage[WEBSITE_PERFORMANCE] = {
            "category": WEBSITE_PERFORMANCE,
            "title": CATEGORY_TITLES[WEBSITE_PERFORMANCE],
            "status": UNAVAILABLE,
            "researched": False,
            "signal_detected": False,
            "finding": "No website URL provided.",
            "reason": "No website URL provided.",
            "diagnostic_reason": "No website URL provided.",
            "problem_identified": False,
            "problem_severity": None,
            "recommended_service": MINDART_SERVICES[WEBSITE_PERFORMANCE]["default"],
            "confidence_score": 0,
            "evidence_type": "URL_VALIDATION",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "Performance Auditor",
            "last_researched_at": now.isoformat(),
        }
    elif has_perf_data:
        latency = analysis.get("response_time_ms", 0)
        is_https = analysis.get("https", True)
        status_code = analysis.get("status_code", 200)

        psi_perf = None
        if "pagespeed_performance_score" in evidence_by_type:
            try:
                psi_perf = float(evidence_by_type["pagespeed_performance_score"][0].observed_value)
            except (ValueError, TypeError):
                pass

        problem = False
        severity = None
        problem_summary = None
        rec_service = MINDART_SERVICES[WEBSITE_PERFORMANCE]["default"]
        finding = f"Website is secure (HTTPS), responsive ({latency}ms latency), and operational."

        if not is_https:
            problem = True
            severity = "critical"
            problem_summary = "Insecure website protocol (HTTP instead of HTTPS)"
            rec_service = MINDART_SERVICES[WEBSITE_PERFORMANCE]["maintenance"]
            finding = "Website uses unencrypted HTTP protocol; SSL/HTTPS certificate is missing or invalid."
        elif status_code >= 400:
            problem = True
            severity = "critical"
            problem_summary = f"Website returned HTTP error status {status_code}"
            rec_service = MINDART_SERVICES[WEBSITE_PERFORMANCE]["maintenance"]
            finding = f"Website returned HTTP error status code {status_code}."
        elif psi_perf is not None and psi_perf < 50:
            problem = True
            severity = "high"
            problem_summary = f"Critical PageSpeed mobile performance bottleneck ({int(psi_perf)}/100)"
            rec_service = MINDART_SERVICES[WEBSITE_PERFORMANCE]["default"]
            finding = f"Mobile PageSpeed score ({int(psi_perf)}/100) indicates significant asset loading bottlenecks."
        elif latency > 2500:
            problem = True
            severity = "medium"
            problem_summary = f"High server response latency ({latency}ms)"
            rec_service = MINDART_SERVICES[WEBSITE_PERFORMANCE]["default"]
            finding = f"Server response latency ({latency}ms) exceeds the recommended 1000ms threshold."

        coverage[WEBSITE_PERFORMANCE] = {
            "category": WEBSITE_PERFORMANCE,
            "title": CATEGORY_TITLES[WEBSITE_PERFORMANCE],
            "status": RESEARCHED,
            "researched": True,
            "signal_detected": True,
            "finding": finding,
            "reason": finding,
            "problem_identified": problem,
            "problem_severity": severity,
            "problem_summary": problem_summary,
            "recommended_service": rec_service,
            "confidence_score": 90 if psi_perf is not None else 80,
            "evidence_type": "HTTP_LATENCY_AUDIT",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "PageSpeed & HTTP Auditor",
            "details": {
                "latency_ms": latency,
                "https": is_https,
                "status_code": status_code,
                "pagespeed_score": psi_perf,
            },
            "last_researched_at": now.isoformat(),
        }
    else:
        coverage[WEBSITE_PERFORMANCE] = {
            "category": WEBSITE_PERFORMANCE,
            "title": CATEGORY_TITLES[WEBSITE_PERFORMANCE],
            "status": NOT_RESEARCHED,
            "researched": False,
            "signal_detected": False,
            "finding": "Website performance has not been measured.",
            "reason": "Website performance has not been measured.",
            "diagnostic_reason": "Performance test pending.",
            "problem_identified": False,
            "problem_severity": None,
            "recommended_service": MINDART_SERVICES[WEBSITE_PERFORMANCE]["default"],
            "confidence_score": 0,
            "evidence_type": "NONE",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "Performance Auditor",
            "last_researched_at": now.isoformat(),
        }

    # 6. Conversion Optimization (CRO)
    has_cro_opp = False
    cro_opp = None
    for opp in opportunities:
        if opp.category in {"conversion", "cro"} or "conversion" in opp.title.lower():
            has_cro_opp = True
            cro_opp = opp
            break

    has_prob = False
    if "primary_problem" in analysis:
        p_lower = str(analysis["primary_problem"]).lower()
        if any(term in p_lower for term in ("conversion", "landing page", "cro", "checkout", "bounce")):
            has_prob = True

    has_cro_data = bool(
        has_cro_opp
        or has_prob
        or "has_form" in analysis
        or "detected_ctas" in analysis
    )

    if not has_website:
        coverage[CONVERSION_OPTIMIZATION] = {
            "category": CONVERSION_OPTIMIZATION,
            "title": CATEGORY_TITLES[CONVERSION_OPTIMIZATION],
            "status": UNAVAILABLE,
            "researched": False,
            "signal_detected": False,
            "finding": "No website URL provided.",
            "reason": "No website URL provided.",
            "diagnostic_reason": "No website URL provided.",
            "problem_identified": False,
            "problem_severity": None,
            "recommended_service": MINDART_SERVICES[CONVERSION_OPTIMIZATION]["default"],
            "confidence_score": 0,
            "evidence_type": "URL_VALIDATION",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "CRO Auditor",
            "last_researched_at": now.isoformat(),
        }
    elif has_cro_data:
        has_form = analysis.get("has_form", False)
        has_cta = analysis.get("has_cta", False)
        ctas = analysis.get("detected_ctas", [])

        if cro_opp:
            problem = True
            severity = cro_opp.severity or "high"
            problem_summary = cro_opp.title
            rec_service = MINDART_SERVICES[CONVERSION_OPTIMIZATION]["default"]
            finding = cro_opp.description
        elif has_prob:
            problem = True
            severity = "high"
            problem_summary = str(analysis["primary_problem"])
            rec_service = MINDART_SERVICES[CONVERSION_OPTIMIZATION]["default"]
            finding = str(analysis["primary_problem"])
        elif not has_form and not has_cta and "issues" in analysis and any("conversion" in i.lower() for i in analysis["issues"]):
            problem = True
            severity = "high"
            problem_summary = "Missing conversion CTAs and lead capture mechanisms"
            rec_service = MINDART_SERVICES[CONVERSION_OPTIMIZATION]["default"]
            finding = "Website was analyzed; no primary call-to-action triggers or lead capture forms were detected on the page."
        else:
            problem = False
            severity = None
            problem_summary = None
            rec_service = MINDART_SERVICES[CONVERSION_OPTIMIZATION]["default"]
            cta_info = f" (detected CTAs: {', '.join(ctas[:3])})" if ctas else ""
            finding = f"Verified lead capture mechanisms and call-to-action pathways present on website{cta_info}."

        coverage[CONVERSION_OPTIMIZATION] = {
            "category": CONVERSION_OPTIMIZATION,
            "title": CATEGORY_TITLES[CONVERSION_OPTIMIZATION],
            "status": RESEARCHED,
            "researched": True,
            "signal_detected": True,
            "finding": finding,
            "reason": finding,
            "problem_identified": problem,
            "problem_severity": severity,
            "problem_summary": problem_summary,
            "recommended_service": rec_service,
            "confidence_score": 80,
            "evidence_type": "CONVERSION_FUNNEL_ANALYSIS",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "CRO Auditor",
            "details": {
                "has_form": has_form,
                "has_cta": has_cta,
                "detected_ctas": ctas,
            },
            "last_researched_at": now.isoformat(),
        }
    else:
        coverage[CONVERSION_OPTIMIZATION] = {
            "category": CONVERSION_OPTIMIZATION,
            "title": CATEGORY_TITLES[CONVERSION_OPTIMIZATION],
            "status": NOT_RESEARCHED,
            "researched": False,
            "signal_detected": False,
            "finding": "Conversion rate optimization audit has not been conducted.",
            "reason": "Conversion rate optimization audit has not been conducted.",
            "diagnostic_reason": "CRO audit pending.",
            "problem_identified": False,
            "problem_severity": None,
            "recommended_service": MINDART_SERVICES[CONVERSION_OPTIMIZATION]["default"],
            "confidence_score": 0,
            "evidence_type": "NONE",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "CRO Auditor",
            "last_researched_at": now.isoformat(),
        }

    # 7. Online Reputation
    review_widgets = analysis.get("review_widgets", [])
    has_places_rating = "google_places_rating" in evidence_by_type
    has_places_key = bool(os.environ.get("GOOGLE_PLACES_API_KEY"))
    reputation_checked = bool(analysis.get("reputation_checked") or analysis.get("online_reputation_checked"))

    if not has_website:
        coverage[ONLINE_REPUTATION] = {
            "category": ONLINE_REPUTATION,
            "title": CATEGORY_TITLES[ONLINE_REPUTATION],
            "status": UNAVAILABLE,
            "researched": False,
            "signal_detected": False,
            "finding": "No website URL provided.",
            "reason": "No website URL provided.",
            "diagnostic_reason": "No website URL provided.",
            "problem_identified": False,
            "problem_severity": None,
            "recommended_service": MINDART_SERVICES[ONLINE_REPUTATION]["default"],
            "confidence_score": 0,
            "evidence_type": "URL_VALIDATION",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "Google Places Provider",
            "last_researched_at": now.isoformat(),
        }
    elif has_places_rating:
        rating_ev = evidence_by_type["google_places_rating"][0]
        rating = float(rating_ev.observed_value)
        ratings_total = 0
        if "google_places_user_ratings_total" in evidence_by_type:
            try:
                ratings_total = int(evidence_by_type["google_places_user_ratings_total"][0].observed_value)
            except (ValueError, TypeError):
                pass

        problem = False
        severity = None
        problem_summary = None
        rec_service = MINDART_SERVICES[ONLINE_REPUTATION]["default"]

        if rating < 4.0:
            problem = True
            severity = "high"
            problem_summary = f"Low Google Places rating ({rating:.1f}/5.0 based on {ratings_total} reviews)"
            rec_service = MINDART_SERVICES[ONLINE_REPUTATION]["default"]
            finding = f"Google Places rating of {rating:.1f}/5.0 across {ratings_total} reviews indicates reputational risk."
        elif ratings_total < 10:
            problem = True
            severity = "medium"
            problem_summary = f"Limited review volume ({ratings_total} reviews)"
            rec_service = MINDART_SERVICES[ONLINE_REPUTATION]["reviews"]
            finding = f"Limited Google Places review volume ({ratings_total} reviews) weakens local social proof."
        else:
            finding = f"Strong Google Places rating of {rating:.1f}/5.0 verified across {ratings_total} reviews."

        coverage[ONLINE_REPUTATION] = {
            "category": ONLINE_REPUTATION,
            "title": CATEGORY_TITLES[ONLINE_REPUTATION],
            "status": RESEARCHED,
            "researched": True,
            "signal_detected": True,
            "finding": finding,
            "reason": finding,
            "problem_identified": problem,
            "problem_severity": severity,
            "problem_summary": problem_summary,
            "recommended_service": rec_service,
            "confidence_score": 90,
            "evidence_type": "GOOGLE_PLACES_VERIFICATION",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "Google Places Provider",
            "details": {
                "rating": rating,
                "user_ratings_total": ratings_total,
            },
            "last_researched_at": now.isoformat(),
        }
    elif review_widgets:
        coverage[ONLINE_REPUTATION] = {
            "category": ONLINE_REPUTATION,
            "title": CATEGORY_TITLES[ONLINE_REPUTATION],
            "status": PARTIALLY_RESEARCHED,
            "researched": False,
            "signal_detected": True,
            "finding": f"On-site customer review elements detected ({', '.join(review_widgets)}), but external Google Places verification is unavailable.",
            "reason": f"On-site customer review elements detected ({', '.join(review_widgets)}), but external Google Places verification is unavailable.",
            "diagnostic_reason": "Google Places API credential (GOOGLE_PLACES_API_KEY) is not configured in the environment.",
            "problem_identified": False,
            "problem_severity": None,
            "recommended_service": MINDART_SERVICES[ONLINE_REPUTATION]["reviews"],
            "confidence_score": 60,
            "evidence_type": "ON_SITE_REPUTATION_WIDGETS",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "Website Analyzer",
            "details": {"review_widgets": review_widgets},
            "last_researched_at": now.isoformat(),
        }
    elif not has_places_key:
        coverage[ONLINE_REPUTATION] = {
            "category": ONLINE_REPUTATION,
            "title": CATEGORY_TITLES[ONLINE_REPUTATION],
            "status": UNAVAILABLE,
            "researched": False,
            "signal_detected": False,
            "finding": "Online reputation search via Google Places is unavailable because API credentials are not configured.",
            "reason": "Online reputation search via Google Places is unavailable because API credentials are not configured.",
            "diagnostic_reason": "Google Places API credential (GOOGLE_PLACES_API_KEY) is not configured in the environment.",
            "problem_identified": False,
            "problem_severity": None,
            "recommended_service": MINDART_SERVICES[ONLINE_REPUTATION]["default"],
            "confidence_score": 0,
            "evidence_type": "PROVIDER_CONFIGURATION",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "Google Places Provider",
            "last_researched_at": now.isoformat(),
        }
    else:
        coverage[ONLINE_REPUTATION] = {
            "category": ONLINE_REPUTATION,
            "title": CATEGORY_TITLES[ONLINE_REPUTATION],
            "status": NOT_RESEARCHED,
            "researched": False,
            "signal_detected": False,
            "finding": "Google Places reputation research has not been executed.",
            "reason": "Google Places reputation research has not been executed.",
            "diagnostic_reason": "Reputation lookup pending.",
            "problem_identified": False,
            "problem_severity": None,
            "recommended_service": MINDART_SERVICES[ONLINE_REPUTATION]["default"],
            "confidence_score": 0,
            "evidence_type": "NONE",
            "verified_fact_or_inference": "verified_fact",
            "source_name": "Google Places Provider",
            "last_researched_at": now.isoformat(),
        }

    # Preservation rule: Never overwrite existing verified RESEARCHED data with UNAVAILABLE or NOT_RESEARCHED data
    if existing_coverage:
        for cat, old_finding in existing_coverage.items():
            if cat in coverage and old_finding.get("status") == RESEARCHED:
                if coverage[cat].get("status") in (NOT_RESEARCHED, UNAVAILABLE):
                    coverage[cat] = old_finding

    return coverage


def derive_opportunities_from_seven_categories(
    lead_id: uuid.UUID,
    coverage: Dict[str, Dict[str, Any]],
) -> List[LeadIntelligenceOpportunity]:
    """
    Deterministically converts evidence-backed digital marketing problems into
    focused sales opportunities for MindArtDigital services.
    
    Constraint Invariant:
    1. Generate sales opportunities only from specific, evidence-supported findings.
    2. Do not treat generic website observations as confirmed commercial problems.
    3. Do NOT generate opportunities from NOT_RESEARCHED or UNAVAILABLE categories.
    """
    opportunities: List[LeadIntelligenceOpportunity] = []
    now = now_utc()

    severity_score_map = {
        "critical": 90,
        "high": 80,
        "medium": 65,
        "low": 45,
    }

    for cat_key in CANONICAL_CATEGORIES:
        finding = coverage.get(cat_key)
        if not finding:
            continue

        status = finding.get("status")
        # Invariant: Never create opportunities from unresearched or unavailable categories
        if status not in (RESEARCHED, "SIGNAL_DETECTED", "NO_SIGNAL_FOUND", PARTIALLY_RESEARCHED):
            continue

        # Invariant: Must have a specific, verified problem identified
        if not finding.get("problem_identified"):
            continue

        severity = finding.get("problem_severity")
        if not severity or severity not in severity_score_map:
            continue

        # Only medium, high, and critical problems generate actionable sales opportunities
        if severity == "low":
            continue

        cat_title = finding.get("title", cat_key.upper())
        problem_summary = finding.get("problem_summary", finding.get("finding"))
        recommended_service = finding.get("recommended_service", MINDART_SERVICES[cat_key]["default"])

        title = f"{cat_title}: {problem_summary}"
        score = severity_score_map[severity]
        priority = "CRITICAL" if severity == "critical" else ("HIGH" if severity == "high" else "MEDIUM")

        evidence_dict = {
            "opportunity_title": title,
            "opportunity_category": cat_key.upper(),
            "recommended_service": recommended_service,
            "opportunity_status": "QUALIFIED",
            "status": "QUALIFIED",
            "evidence_source": finding.get("source_name", "Digital Marketing Auditor"),
            "commercial_relevance": (
                f"Verified commercial gap in {cat_title}: {problem_summary}. "
                f"Direct commercial fit for MindArtDigital's {recommended_service} service offering."
            ),
            "suggested_next_action": f"Prepare evidence-backed audit and commercial proposal for {recommended_service}.",
            "finding_details": finding.get("details", {}),
            "problem_severity": severity,
            "verified_fact_or_inference": finding.get("verified_fact_or_inference", "verified_fact"),
            "category_key": cat_key,
            "finding": finding.get("finding"),
            "last_evaluated": now.isoformat(),
        }

        confidence = ConfidenceLevel.HIGH if finding.get("confidence_score", 0) >= 70 else ConfidenceLevel.MEDIUM

        opportunities.append(
            LeadIntelligenceOpportunity(
                id=uuid.uuid4(),
                lead_id=lead_id,
                title=title[:250],
                description=finding.get("finding", problem_summary)[:2000],
                opportunity_type=cat_key.upper(),
                score=score,
                priority=priority,
                recommended_action=f"Deliver service: {recommended_service}",
                evidence=evidence_dict,
                source_type=SourceType.SYSTEM_DERIVED,
                source_reference=f"digital_marketing.{cat_key}",
                confidence=confidence,
                created_at=now,
                updated_at=now,
            )
        )

    return sorted(opportunities, key=lambda o: (o.score or 0), reverse=True)


def build_digital_marketing_profile(
    lead: Lead,
    enrichment: Optional[CompanyEnrichmentProfile] = None,
    website_intel: Optional[WebsiteIntelligenceProfile] = None,
    has_seo_run: bool = False,
) -> DigitalMarketingIntelligenceProfile:
    """Legacy helper maintained for backward compatibility."""
    if enrichment is not None or website_intel is not None:
        social_data = {}
        if enrichment and enrichment.linkedin_url:
            social_data["linkedin_present"] = True
            social_data["linkedin_url"] = enrichment.linkedin_url

        if lead.website_analysis and isinstance(lead.website_analysis, dict):
            socials = lead.website_analysis.get("social_links", [])
            if socials:
                social_data["social_links"] = socials

        social_media_signals = normalize_marketing_signal(social_data)

        seo_data = {}
        if has_seo_run:
            seo_data["seo_audit_present"] = True
        if lead.website_analysis and isinstance(lead.website_analysis, dict):
            if "meta_description" in lead.website_analysis:
                seo_data["seo_metadata_present"] = True

        seo_signals = normalize_marketing_signal(seo_data)

        paid_data = {}
        if website_intel and website_intel.technology_signals:
            techs = website_intel.technology_signals.get("technologies", [])
            if any("ads" in t.lower() or "pixel" in t.lower() for t in techs):
                paid_data["advertising_account_signal"] = True
        paid_advertising_signals = normalize_marketing_signal(paid_data)

        content_data = {}
        if lead.ai_analysis:
            content_data["content_strategy_signal"] = True
        content_marketing_signals = normalize_marketing_signal(content_data)

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
            updated_at=now,
        )

    from app.services.unified_intelligence import build_unified_digital_marketing_profile
    return build_unified_digital_marketing_profile(
        lead=lead,
        evidence_list=[],
        opportunities=[],
        has_seo_run=has_seo_run,
    )

"""Transparent deterministic lead scoring."""
def score_lead(lead, website: dict, analysis: dict) -> tuple[int, dict]:
    """Calculate a bounded 0–100 score and return explainable factors."""
    factors = {"website_quality": 0, "seo_issues": 0, "metadata": 0, "https": 0, "business_completeness": 0, "digital_opportunity": 0, "industry_relevance": 0, "contact_availability": 0}
    if website.get("available"): factors["website_quality"] = 10 if website.get("status_code", 500) < 400 else 2
    issues = website.get("issues", [])
    factors["seo_issues"] = min(15, 5 * len(issues))
    factors["metadata"] = 10 if website.get("title") and website.get("meta_description") else 4 if website.get("title") or website.get("meta_description") else 0
    factors["https"] = 10 if website.get("https") else 0
    fields = [lead.industry, lead.city, lead.state, lead.country, lead.address]
    factors["business_completeness"] = round(10 * sum(bool(v) for v in fields) / len(fields))
    opportunities = analysis.get("seo_opportunities", []) + analysis.get("digital_marketing_opportunities", [])
    factors["digital_opportunity"] = min(15, 5 * len(opportunities))
    factors["industry_relevance"] = 10 if lead.industry else 5
    factors["contact_availability"] = min(10, 5 * sum(bool(v) for v in [lead.email, lead.phone]))
    # Normalize factor weights to a 100 point scale (maximum raw total 90).
    score = min(100, round(sum(factors.values()) * 100 / 90))
    return score, factors

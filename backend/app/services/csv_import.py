"""CSV validation, normalization, duplicate detection, and durable import."""
import csv
import io
import re
from typing import Any
from urllib.parse import urlparse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Lead, ImportJob

MINDART_TEMPLATE_HEADERS = [
    "company_name",
    "website",
    "industry",
    "sub_category",
    "country",
    "city",
    "decision_maker",
    "decision_maker_role",
    "business_email",
    "phone",
    "linkedin",
    "advertising_platforms",
    "advertising_evidence",
    "primary_problem",
    "recommended_mindart_service",
]

LEGACY_COLUMNS = {"email", "address", "state", "job_title", "department", "linkedin_url"}
ALLOWED_COLUMNS = set(MINDART_TEMPLATE_HEADERS) | LEGACY_COLUMNS
REQUIRED_COLUMNS = {"company_name"}
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def get_template_csv_bytes() -> bytes:
    """Return UTF-8 bytes for the MindArtDigital lead research CSV template with sample rows."""
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(MINDART_TEMPLATE_HEADERS)
    writer.writerow([
        "Apex Innovations",
        "https://apexinnovations.example",
        "Technology",
        "B2B SaaS",
        "India",
        "Bengaluru",
        "Rajesh Mehta",
        "Chief Marketing Officer",
        "rajesh.mehta@apexinnovations.example",
        "+919876543210",
        "https://linkedin.com/in/rajesh-mehta-apex",
        "Google Ads; LinkedIn Ads",
        "Active Google Search ad tags and LinkedIn Insight tag detected",
        "High cost per lead with low inbound organic conversions",
        "Performance Marketing & SEO Optimization",
    ])
    writer.writerow([
        "Zenith Healthcare Labs",
        "https://zenithhealth.example",
        "Healthcare",
        "Diagnostic Services",
        "India",
        "Mumbai",
        "Priya Sharma",
        "Founder & CEO",
        "priya.sharma@zenithhealth.example",
        "",  # Optional phone blank
        "https://linkedin.com/in/priyasharma-zenith",
        "Meta Ads; Google Ads",
        "Meta Pixel and Google Tag Manager found on landing page",
        "Low patient appointment booking rate from mobile ad campaigns",
        "Landing Page CRO & Social Media Performance",
    ])
    writer.writerow([
        "Aura Home Living",
        "https://aurahome.example",
        "Retail",
        "Home Decor & Furnishing",
        "India",
        "Delhi",
        "Vikram Sen",
        "Director of Growth",
        "vikram@aurahome.example",
        "",  # Optional phone blank
        "https://linkedin.com/in/vikram-sen-aura",
        "",  # Optional advertising blank
        "",  # Optional advertising blank
        "Absence of structured search marketing presence against regional competitors",
        "Local SEO & Full-Funnel Digital Strategy",
    ])
    return output.getvalue().encode("utf-8")


def normalize_row(row: dict[str, str | None]) -> dict[str, str | None]:
    """Trim values and normalize website, email, and empty fields."""
    result = {k: (v.strip() if isinstance(v, str) else None) or None for k, v in row.items()}
    if result.get("email"):
        result["email"] = result["email"].lower()
    if result.get("business_email"):
        result["business_email"] = result["business_email"].lower()
    if result.get("website"):
        website = result["website"].strip()
        if not website.startswith(("http://", "https://")):
            website = "https://" + website
        result["website"] = website.rstrip("/")
    return result


def validate_row(row: dict[str, str | None]) -> list[str]:
    """Return row validation errors without throwing on user data."""
    errors: list[str] = []
    if not row.get("company_name"):
        errors.append("company_name is required")
    email_val = row.get("business_email") or row.get("email")
    if email_val and not EMAIL_RE.fullmatch(email_val):
        errors.append("email is invalid")
    if row.get("website"):
        parsed = urlparse(row["website"] or "")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or "." not in parsed.netloc:
            errors.append("website URL is invalid")
    # Phone number policy: optional, allow blank, allow null, allow absent
    return errors


def map_row_to_lead_data(row: dict[str, str | None]) -> dict[str, Any]:
    """Map normalized CSV headers to Lead model attributes without discarding data."""
    lead_data: dict[str, Any] = {
        "company_name": row.get("company_name"),
        "website": row.get("website"),
        "industry": row.get("industry"),
        "country": row.get("country"),
        "city": row.get("city"),
        "address": row.get("address"),
        "state": row.get("state"),
        "phone": row.get("phone"),
        "decision_maker": row.get("decision_maker"),
        "job_title": row.get("decision_maker_role") or row.get("job_title"),
        "email": row.get("business_email") or row.get("email"),
        "linkedin_url": row.get("linkedin") or row.get("linkedin_url"),
        "department": row.get("department"),
    }

    primary_problem = row.get("primary_problem")
    if primary_problem:
        lead_data["pain_points"] = [primary_problem]

    recommended_service = row.get("recommended_mindart_service")
    if recommended_service:
        lead_data["recommended_services"] = [recommended_service]

    # Preserve research metadata in website_analysis JSON so nothing is silently discarded
    research_meta: dict[str, Any] = {}
    if row.get("sub_category"):
        research_meta["sub_category"] = row.get("sub_category")
    if row.get("advertising_platforms"):
        research_meta["advertising_platforms"] = row.get("advertising_platforms")
    if row.get("advertising_evidence"):
        research_meta["advertising_evidence"] = row.get("advertising_evidence")
    if primary_problem:
        research_meta["primary_problem"] = primary_problem
    if recommended_service:
        research_meta["recommended_mindart_service"] = recommended_service

    if research_meta:
        lead_data["website_analysis"] = research_meta

    return lead_data


async def import_csv(session: AsyncSession, content: bytes, filename: str) -> dict:
    """Import valid unique rows; retain invalid row diagnostics in import job."""
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    headers = {h.strip().lower() for h in (reader.fieldnames or []) if h}
    if not REQUIRED_COLUMNS.issubset(headers):
        raise ValueError("CSV must include company_name")
    unknown = headers - ALLOWED_COLUMNS
    if unknown:
        raise ValueError("Unsupported CSV columns: " + ", ".join(sorted(unknown)))
    job = ImportJob(filename=filename)
    session.add(job)
    await session.flush()
    invalid: list[dict] = []
    for number, source in enumerate(reader, start=2):
        job.total_rows += 1
        row = normalize_row({(k or "").strip().lower(): v for k, v in source.items() if k})
        errors = validate_row(row)
        if errors:
            job.invalid_rows += 1
            invalid.append({"row": number, "errors": errors})
            continue
        lead_data = map_row_to_lead_data(row)
        name = lead_data["company_name"] or ""
        existing = await session.scalar(
            select(Lead.id).where(
                func.lower(Lead.company_name) == name.lower(),
                func.lower(func.coalesce(Lead.city, "")) == (lead_data.get("city") or "").lower()
            )
        )
        if existing:
            job.duplicate_rows += 1
            continue
        session.add(Lead(**lead_data))
        job.accepted_rows += 1
    job.invalid_details = invalid
    await session.commit()
    return {
        "job_id": str(job.id),
        "total_rows": job.total_rows,
        "accepted_rows": job.accepted_rows,
        "duplicate_rows": job.duplicate_rows,
        "invalid_rows": job.invalid_rows,
        "invalid_details": invalid,
    }

"""CSV validation, normalization, duplicate detection, and durable import."""
import csv
import io
import re
from urllib.parse import urlparse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Lead, ImportJob

REQUIRED_COLUMNS = {"company_name"}
ALLOWED_COLUMNS = {"company_name", "website", "phone", "email", "address", "city", "state", "country", "industry"}
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

def normalize_row(row: dict[str, str | None]) -> dict[str, str | None]:
    """Trim values and normalize website, email, and empty fields."""
    result = {k: (v.strip() if isinstance(v, str) else None) or None for k, v in row.items()}
    if result.get("email"):
        result["email"] = result["email"].lower()
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
    if row.get("email") and not EMAIL_RE.fullmatch(row["email"] or ""):
        errors.append("email is invalid")
    if row.get("website"):
        parsed = urlparse(row["website"] or "")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or "." not in parsed.netloc:
            errors.append("website URL is invalid")
    return errors

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
        name = row["company_name"] or ""
        existing = await session.scalar(select(Lead.id).where(func.lower(Lead.company_name) == name.lower(), func.lower(func.coalesce(Lead.city, "")) == (row.get("city") or "").lower()))
        if existing:
            job.duplicate_rows += 1
            continue
        session.add(Lead(**row))
        job.accepted_rows += 1
    job.invalid_details = invalid
    await session.commit()
    return {"job_id": str(job.id), "total_rows": job.total_rows, "accepted_rows": job.accepted_rows, "duplicate_rows": job.duplicate_rows, "invalid_rows": job.invalid_rows, "invalid_details": invalid}

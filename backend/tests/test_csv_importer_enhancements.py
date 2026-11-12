"""Automated tests for MindArtDigital Lead Research CSV template and importer enhancements."""
import csv
import io
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, func
from app.main import app
from app.database import get_db
from app.config import settings
from app.models import Lead
from app.services.csv_import import (
    MINDART_TEMPLATE_HEADERS,
    get_template_csv_bytes,
    import_csv,
    normalize_row,
    validate_row,
)


def test_template_headers_count_and_order():
    """Verify exact 15 headers and exact order required for MindArtDigital template."""
    expected_headers = [
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
    assert MINDART_TEMPLATE_HEADERS == expected_headers
    assert len(MINDART_TEMPLATE_HEADERS) == 15

    raw_bytes = get_template_csv_bytes()
    text = raw_bytes.decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    header_row = next(reader)
    assert header_row == expected_headers


@pytest.mark.asyncio
async def test_import_with_all_15_fields_populated(session_factory):
    """Test importing lead with all 15 fields populated."""
    async with session_factory() as db:
        csv_content = (
            "company_name,website,industry,sub_category,country,city,decision_maker,"
            "decision_maker_role,business_email,phone,linkedin,advertising_platforms,"
            "advertising_evidence,primary_problem,recommended_mindart_service\n"
            "Apex Innovations,https://apexinnovations.example,Technology,B2B SaaS,India,Bengaluru,"
            "Rajesh Mehta,Chief Marketing Officer,rajesh@apexinnovations.example,+919876543210,"
            "https://linkedin.com/in/rajesh,Google Ads; LinkedIn Ads,Active Google Ads detected,"
            "High cost per lead,SEO & Performance Marketing\n"
        ).encode("utf-8")

        result = await import_csv(db, csv_content, "test_15_fields.csv")
        assert result["accepted_rows"] == 1
        assert result["invalid_rows"] == 0
        assert result["duplicate_rows"] == 0

        lead = (await db.scalars(select(Lead).where(Lead.company_name == "Apex Innovations"))).first()
        assert lead is not None
        assert lead.website == "https://apexinnovations.example"
        assert lead.industry == "Technology"
        assert lead.country == "India"
        assert lead.city == "Bengaluru"
        assert lead.decision_maker == "Rajesh Mehta"
        assert lead.job_title == "Chief Marketing Officer"
        assert lead.email == "rajesh@apexinnovations.example"
        assert lead.phone == "+919876543210"
        assert lead.linkedin_url == "https://linkedin.com/in/rajesh"
        assert lead.pain_points == ["High cost per lead"]
        assert lead.recommended_services == ["SEO & Performance Marketing"]
        assert lead.website_analysis is not None
        assert lead.website_analysis.get("sub_category") == "B2B SaaS"
        assert lead.website_analysis.get("advertising_platforms") == "Google Ads; LinkedIn Ads"
        assert lead.website_analysis.get("advertising_evidence") == "Active Google Ads detected"


@pytest.mark.asyncio
async def test_import_with_phone_blank(session_factory):
    """Test importing lead with phone blank."""
    async with session_factory() as db:
        csv_content = (
            "company_name,website,industry,sub_category,country,city,decision_maker,"
            "decision_maker_role,business_email,phone,linkedin,advertising_platforms,"
            "advertising_evidence,primary_problem,recommended_mindart_service\n"
            "Zenith Labs,https://zenith.example,Healthcare,Diagnostic Services,India,Mumbai,"
            "Priya Sharma,Founder,priya@zenith.example,  ,https://linkedin.com/in/priya,"
            "Meta Ads,Meta Pixel active,Low conversions,Landing Page CRO\n"
        ).encode("utf-8")

        result = await import_csv(db, csv_content, "test_phone_blank.csv")
        assert result["accepted_rows"] == 1
        assert result["invalid_rows"] == 0

        lead = (await db.scalars(select(Lead).where(Lead.company_name == "Zenith Labs"))).first()
        assert lead is not None
        assert lead.phone is None


@pytest.mark.asyncio
async def test_import_with_phone_column_absent(session_factory):
    """Test importing lead when phone column is completely absent from CSV headers."""
    async with session_factory() as db:
        csv_content = (
            "company_name,website,industry,city,business_email\n"
            "Aura Living,https://aura.example,Retail,Delhi,contact@aura.example\n"
        ).encode("utf-8")

        result = await import_csv(db, csv_content, "test_no_phone_col.csv")
        assert result["accepted_rows"] == 1
        assert result["invalid_rows"] == 0

        lead = (await db.scalars(select(Lead).where(Lead.company_name == "Aura Living"))).first()
        assert lead is not None
        assert lead.phone is None
        assert lead.email == "contact@aura.example"


@pytest.mark.asyncio
async def test_import_with_company_name_only(session_factory):
    """Test importing lead with company_name only."""
    async with session_factory() as db:
        csv_content = b"company_name\nMinimal Corp\n"
        result = await import_csv(db, csv_content, "test_minimal.csv")
        assert result["accepted_rows"] == 1
        assert result["invalid_rows"] == 0

        lead = (await db.scalars(select(Lead).where(Lead.company_name == "Minimal Corp"))).first()
        assert lead is not None
        assert lead.company_name == "Minimal Corp"
        assert lead.phone is None
        assert lead.email is None


@pytest.mark.asyncio
async def test_import_without_company_name_header(session_factory):
    """Test CSV file without company_name header is rejected."""
    async with session_factory() as db:
        csv_content = b"website,phone\nhttps://example.com,555-1234\n"
        with pytest.raises(ValueError, match="CSV must include company_name"):
            await import_csv(db, csv_content, "invalid_headers.csv")


@pytest.mark.asyncio
async def test_import_without_company_name_in_row(session_factory):
    """Test row without company_name is rejected with clear error."""
    async with session_factory() as db:
        csv_content = (
            "company_name,phone\n"
            "Valid Lead,555-0001\n"
            ",555-0002\n"
        ).encode("utf-8")
        result = await import_csv(db, csv_content, "missing_row_company.csv")
        assert result["accepted_rows"] == 1
        assert result["invalid_rows"] == 1
        assert len(result["invalid_details"]) == 1
        assert "company_name is required" in result["invalid_details"][0]["errors"]


@pytest.mark.asyncio
async def test_import_duplicate_company(session_factory):
    """Test existing duplicate detection behavior is preserved."""
    async with session_factory() as db:
        csv_content = (
            "company_name,city\n"
            "Duplicate Corp,Kolkata\n"
            "duplicate corp,kolkata\n"
        ).encode("utf-8")
        result = await import_csv(db, csv_content, "duplicates.csv")
        assert result["accepted_rows"] == 1
        assert result["duplicate_rows"] == 1

        leads = (await db.scalars(select(Lead).where(func.lower(Lead.company_name) == "duplicate corp"))).all()
        assert len(leads) == 1


@pytest.mark.asyncio
async def test_add_phone_number_to_existing_lead_manually(session_factory, monkeypatch):
    """Test adding phone number to an existing lead manually without creating a duplicate lead."""
    async def override_db():
        async with session_factory() as session:
            yield session

    monkeypatch.setattr(settings, "api_auth_token", "test-token")
    app.dependency_overrides[get_db] = override_db
    headers = {"Authorization": "Bearer test-token"}

    # 1. Create lead with no phone
    async with session_factory() as db:
        lead = Lead(company_name="Cloud Solutions", city="Austin")
        db.add(lead)
        await db.commit()
        lead_id = lead.id

    # 2. Patch phone number via API client
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.patch(
            f"/api/leads/{lead_id}",
            headers=headers,
            json={"phone": "+1-512-555-0199"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(lead_id)
        assert data["phone"] == "+1-512-555-0199"

    # 3. Verify in database: same lead, updated phone, exactly 1 lead in DB
    async with session_factory() as db:
        leads = (await db.scalars(select(Lead))).all()
        assert len(leads) == 1
        assert leads[0].id == lead_id
        assert leads[0].phone == "+1-512-555-0199"

    app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_backward_compatibility_with_legacy_headers(session_factory):
    """Test backward compatibility with legacy columns from previous CSV imports."""
    async with session_factory() as db:
        legacy_csv = (
            "company_name,website,phone,email,address,city,state,country,industry,job_title,department,linkedin_url\n"
            "Legacy Co,https://legacy.example,555-9999,info@legacy.example,100 Main St,Boston,MA,USA,Legal,General Counsel,Legal Ops,https://linkedin.com/in/legal\n"
        ).encode("utf-8")
        result = await import_csv(db, legacy_csv, "legacy.csv")
        assert result["accepted_rows"] == 1
        assert result["invalid_rows"] == 0

        lead = (await db.scalars(select(Lead).where(Lead.company_name == "Legacy Co"))).first()
        assert lead is not None
        assert lead.email == "info@legacy.example"
        assert lead.job_title == "General Counsel"
        assert lead.department == "Legal Ops"
        assert lead.linkedin_url == "https://linkedin.com/in/legal"


@pytest.mark.asyncio
async def test_unsupported_csv_columns_rejected(session_factory):
    """Test clear validation error when unsupported headers are provided."""
    async with session_factory() as db:
        bad_csv = b"company_name,unsupported_column_x,another_bad_col\nBad,1,2\n"
        with pytest.raises(ValueError) as exc_info:
            await import_csv(db, bad_csv, "bad.csv")
        msg = str(exc_info.value)
        assert "Unsupported CSV columns" in msg
        assert "unsupported_column_x" in msg


@pytest.mark.asyncio
async def test_template_download_endpoint(monkeypatch):
    """Test the template download endpoint returns CSV file with attachment header."""
    monkeypatch.setattr(settings, "api_auth_token", "test-token")
    headers = {"Authorization": "Bearer test-token"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/leads/import/template", headers=headers)
        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]
        assert 'filename="mindartdigital_lead_research_template.csv"' in response.headers["content-disposition"]

        content = response.content.decode("utf-8")
        reader = csv.reader(io.StringIO(content))
        header = next(reader)
        assert header == MINDART_TEMPLATE_HEADERS
        # Check that demo records exist
        rows = list(reader)
        assert len(rows) >= 3
        # Check first row is Apex Innovations
        assert rows[0][0] == "Apex Innovations"

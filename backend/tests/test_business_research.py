"""Tests for MindArtDigital Business Research and Lead Discovery Engine."""
import csv
import io
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import Lead
from app.services.business_research import (
    BusinessResearchEngine,
    MissingProviderConfigurationError,
    ResearchInput,
    ResearchResultItem,
    SaveLeadsRequest,
    provider_registry,
    research_engine,
)
from app.services.business_research.providers import MockDiscoveryProvider
from app.services.csv_import import MINDART_TEMPLATE_HEADERS


@pytest.fixture(autouse=True)
def ensure_test_provider():
    """Ensure mock discovery provider is registered and available for tests."""
    mock = MockDiscoveryProvider(is_enabled=True)
    provider_registry.register(mock)
    yield
    provider_registry.unregister("mock_discovery")


def test_research_input_validation():
    """Test validation of research inputs according to BR specifications."""
    # Valid input with defaults
    inp = ResearchInput(industry="Healthcare", location="Kolkata")
    assert inp.target_business_count == 50
    assert inp.research_depth == "standard"
    assert inp.sub_category is None

    # Target business count boundaries: 1 to 500
    inp_min = ResearchInput(industry="Real Estate", location="Howrah", target_business_count=1)
    assert inp_min.target_business_count == 1

    inp_max = ResearchInput(industry="Education", location="Hooghly", target_business_count=500)
    assert inp_max.target_business_count == 500

    with pytest.raises(ValueError):
        ResearchInput(industry="Tech", location="Delhi", target_business_count=0)

    with pytest.raises(ValueError):
        ResearchInput(industry="Tech", location="Delhi", target_business_count=501)

    with pytest.raises(ValueError):
        ResearchInput(industry="Tech", location="Delhi", research_depth="unsupported_depth")  # type: ignore


def test_missing_provider_configuration_enforcement():
    """BR-06: Do not pretend research was completed when no external provider is configured."""
    # Temporarily remove mock provider and google places key
    provider_registry.unregister("mock_discovery")
    orig_key = settings.google_places_api_key
    settings.google_places_api_key = ""
    try:
        with pytest.raises(MissingProviderConfigurationError) as exc_info:
            provider_registry.get_active_provider()
        assert "No external business discovery provider is configured" in str(exc_info.value)

        # Run engine with unconfigured provider
        local_engine = BusinessResearchEngine()
        job = local_engine.create_job(ResearchInput(industry="Dental", location="Kolkata"))
        # Execute research
        import asyncio
        asyncio.run(local_engine._execute_research(job.job_id))
        assert job.status == "FAILED"
        assert "No external business discovery provider is configured" in (job.error_message or "")
        assert job.found_count == 0
    finally:
        settings.google_places_api_key = orig_key


@pytest.mark.asyncio
async def test_business_discovery_and_research_workflow(session_factory):
    """Test business research discovers candidates, analyzes opportunities, and yields 15-field items."""
    async with session_factory() as db:
        local_engine = BusinessResearchEngine(session_factory=session_factory)
        input_params = ResearchInput(
            industry="Interior Design",
            location="Kolkata",
            sub_category="Commercial Offices",
            target_business_count=4,
            research_depth="standard",
        )
        job = local_engine.create_job(input_params)
        await local_engine._execute_research(job.job_id)

        assert job.status == "COMPLETED"
        assert job.found_count == 4
        assert job.processed_count == 4
        assert len(job.results) == 4

        for item in job.results:
            # Check 15 field output structure
            assert item.company_name
            assert item.industry == "Interior Design"
            assert item.city == "Kolkata"
            assert item.country == "India"
            assert item.primary_problem is not None
            assert item.recommended_mindart_service is not None
            assert item.source_evidence is not None
            assert item.source_evidence.source_url is not None
            # Decision maker must not be invented (BR-09)
            assert item.decision_maker is None
            # Phone is optional
            assert item.phone is None or isinstance(item.phone, str)


@pytest.mark.asyncio
async def test_duplicate_detection_against_database(session_factory):
    """BR-12, BR-13: Preserve existing duplicate detection logic matching company_name and city."""
    async with session_factory() as db:
        # Seed an existing lead
        existing = Lead(company_name="Apex Solutions Kolkata", city="Kolkata", phone="+91-1111-2222")
        db.add(existing)
        await db.commit()
        existing_id = existing.id

    local_engine = BusinessResearchEngine()
    input_params = ResearchInput(
        industry="Technology",
        location="Kolkata",
        target_business_count=2,
    )
    job = local_engine.create_job(input_params)

    # Mock provider generates "Apex Solutions Kolkata" as candidate 0
    # Override SessionLocal in engine during test
    import app.services.business_research.engine as engine_mod
    orig_session_local = engine_mod.SessionLocal
    engine_mod.SessionLocal = session_factory
    try:
        await local_engine._execute_research(job.job_id)
        assert job.status == "COMPLETED"
        match = next((item for item in job.results if "Apex Solutions" in item.company_name), None)
        assert match is not None
        assert match.is_duplicate is True
        assert match.existing_lead_id == str(existing_id)
        assert match.research_status == "duplicate"
    finally:
        engine_mod.SessionLocal = orig_session_local


@pytest.mark.asyncio
async def test_save_leads_preserves_populated_fields(session_factory):
    """BR-14: Do not overwrite existing populated lead fields with empty or unverified research values."""
    async with session_factory() as db:
        # 1. Seed existing lead with verified email and phone
        seed_lead = Lead(
            company_name="Apex Tech",
            city="Kolkata",
            phone="+919800012345",
            email="verified@apextech.com",
            website="https://original.example",
        )
        db.add(seed_lead)
        await db.commit()

        # 2. Researched item has new recommendations, but empty phone and email
        research_item = ResearchResultItem(
            company_name="Apex Tech",
            city="Kolkata",
            website="https://new-researched.example",
            phone=None,  # Empty in research! Must NOT overwrite +919800012345
            business_email=None,  # Empty in research! Must NOT overwrite verified@apextech.com
            industry="Software",
            primary_problem="Lacks instant chat lead capture",
            recommended_mindart_service="AI Automation & Chatbots",
        )

        # Non-duplicate new item
        new_item = ResearchResultItem(
            company_name="Brand New Studio",
            city="Kolkata",
            website="https://brandnew.example",
            phone="+919811122233",
            business_email="contact@brandnew.example",
            industry="Design",
            primary_problem="Missing Meta Pixel on landing page",
            recommended_mindart_service="Performance Marketing & Meta Ads",
        )

        request = SaveLeadsRequest(leads=[research_item, new_item])
        response = await research_engine.save_leads(db, request)

        assert response.saved_count == 1
        assert response.updated_count == 1
        assert response.skipped_count == 0

        # Verify existing lead preserved phone and email
        check_lead = (await db.scalars(select(Lead).where(Lead.company_name == "Apex Tech"))).first()
        assert check_lead is not None
        assert check_lead.phone == "+919800012345"  # Preserved!
        assert check_lead.email == "verified@apextech.com"  # Preserved!
        # But received new pain points and recommendations
        assert check_lead.pain_points == ["Lacks instant chat lead capture"]
        assert check_lead.recommended_services == ["AI Automation & Chatbots"]

        # Verify new lead was created
        new_lead = (await db.scalars(select(Lead).where(Lead.company_name == "Brand New Studio"))).first()
        assert new_lead is not None
        assert new_lead.phone == "+919811122233"
        assert new_lead.email == "contact@brandnew.example"


def test_research_job_cancellation():
    """BR-17: Support cancellation of an ongoing research job."""
    local_engine = BusinessResearchEngine()
    job = local_engine.create_job(ResearchInput(industry="Retail", location="Kolkata"))
    assert job.status == "QUEUED"

    success = local_engine.cancel_job(job.job_id)
    assert success is True
    assert job.status == "CANCELLED"
    assert job.job_id in local_engine._cancelled_jobs


def test_export_job_csv_format():
    """Test exporting research results formatted in the 15-field MindArtDigital template."""
    local_engine = BusinessResearchEngine()
    job = local_engine.create_job(ResearchInput(industry="Healthcare", location="Kolkata"))
    job.results.append(
        ResearchResultItem(
            company_name="Care Hospital",
            website="https://care.example",
            industry="Healthcare",
            sub_category="Super Specialty",
            country="India",
            city="Kolkata",
            decision_maker=None,
            decision_maker_role=None,
            business_email="info@care.example",
            phone="+919800033333",
            linkedin="https://linkedin.com/in/care",
            advertising_platforms="Google Ads",
            advertising_evidence="GTM tag found",
            primary_problem="High cost per booking",
            recommended_mindart_service="Performance Marketing & Google Ads",
        )
    )

    csv_bytes = local_engine.export_job_csv(job.job_id)
    reader = csv.reader(io.StringIO(csv_bytes.decode("utf-8")))
    header = next(reader)
    assert header == MINDART_TEMPLATE_HEADERS

    row = next(reader)
    assert row[0] == "Care Hospital"
    assert row[1] == "https://care.example"
    assert row[2] == "Healthcare"
    assert row[3] == "Super Specialty"
    assert row[4] == "India"
    assert row[5] == "Kolkata"
    assert row[6] == ""  # decision_maker empty
    assert row[7] == ""  # decision_maker_role empty
    assert row[8] == "info@care.example"
    assert row[9] == "+919800033333"
    assert row[10] == "https://linkedin.com/in/care"
    assert row[11] == "Google Ads"
    assert row[12] == "GTM tag found"
    assert row[13] == "High cost per booking"
    assert row[14] == "Performance Marketing & Google Ads"


@pytest.mark.asyncio
async def test_api_research_endpoints(session_factory, monkeypatch):
    """Test API endpoints for business research with auth token."""
    async def override_db():
        async with session_factory() as session:
            yield session

    monkeypatch.setattr(settings, "api_auth_token", "test-token")
    app.dependency_overrides[get_db] = override_db
    headers = {"Authorization": "Bearer test-token"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Status endpoint
        status_res = await client.get("/api/research/providers/status", headers=headers)
        assert status_res.status_code == 200
        status_data = status_res.json()
        assert "active_discovery_provider" in status_data
        assert status_data["is_configured"] is True

        # 2. Create job endpoint
        create_res = await client.post(
            "/api/research/jobs",
            headers=headers,
            json={
                "industry": "Hospitality",
                "location": "Kolkata",
                "target_business_count": 3,
                "research_depth": "standard",
            },
        )
        assert create_res.status_code == 201
        job_data = create_res.json()
        job_id = job_data["job_id"]
        assert job_data["target_count"] == 3

        # 3. Get job endpoint
        get_res = await client.get(f"/api/research/jobs/{job_id}", headers=headers)
        assert get_res.status_code == 200
        assert get_res.json()["job_id"] == job_id

        # 4. List jobs endpoint
        list_res = await client.get("/api/research/jobs", headers=headers)
        assert list_res.status_code == 200
        assert any(j["job_id"] == job_id for j in list_res.json())

        # 5. Cancel endpoint
        cancel_res = await client.post(f"/api/research/jobs/{job_id}/cancel", headers=headers)
        assert cancel_res.status_code == 200
        assert cancel_res.json()["status"] == "CANCELLED"

    app.dependency_overrides.pop(get_db, None)

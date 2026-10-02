import uuid
import pytest
from datetime import datetime, UTC
from sqlalchemy import select
from fastapi.testclient import TestClient

from httpx import AsyncClient, ASGITransport
from app.main import app
from app.models.entities import Lead, CompanyEnrichmentProfile, SourceType, ConfidenceLevel
from app.domain.company_enrichment import (
    normalize_company_name,
    normalize_website,
    extract_email_domain,
    normalize_location,
    calculate_company_completeness
)

def test_company_name_normalization():
    assert normalize_company_name("  Test   Company  LLC. ") == "Test Company LLC."
    assert normalize_company_name("") is None
    assert normalize_company_name(None) is None

def test_website_domain_normalization():
    assert normalize_website("http://www.example.com/") == ("http://example.com", "example.com")
    assert normalize_website("https://example.com/path?track=1") == ("https://example.com/path", "example.com")
    assert normalize_website("example.com") == ("https://example.com", "example.com")
    assert normalize_website("   https://www.example.co.uk  ") == ("https://example.co.uk", "example.co.uk")
    assert normalize_website(None) == (None, None)

def test_email_domain_extraction():
    assert extract_email_domain("test@example.com") == "example.com"
    assert extract_email_domain("  USER@Sub.Domain.org ") == "sub.domain.org"
    assert extract_email_domain("invalid") is None

def test_location_normalization():
    assert normalize_location("San Francisco", "CA", "USA") == "San Francisco, CA, USA"
    assert normalize_location(" ", "NY", None) == "NY"
    assert normalize_location(None, None, None) is None

def test_completeness_calculation():
    assert calculate_company_completeness({}) == 0.0
    fields = {"a": "filled", "b": None, "c": "UNKNOWN", "d": " "}
    assert calculate_company_completeness(fields) == 25.0

@pytest.mark.asyncio
async def test_build_deterministic_company_profile(session_factory):
    async with session_factory() as session:
        lead = Lead(
            company_name="  Test Corp  ",
            website="http://www.test-corp.com/home",
            email="ceo@test-corp.com",
            city="Seattle",
            state="WA",
            industry="IT Services"
        )
        session.add(lead)
        await session.commit()
        lead_id = lead.id

    from app.services.company_enrichment import build_deterministic_company_profile
    async with session_factory() as session:
        lead_record = await session.get(Lead, lead_id)
        profile = build_deterministic_company_profile(lead_record)
        
        assert profile.legal_name == "Test Corp"
        assert profile.domain == "test-corp.com"
        assert profile.website == "http://test-corp.com/home"
        assert profile.email_domain == "test-corp.com"
        assert profile.location == "Seattle, WA"
        assert profile.normalized_industry == "TECHNOLOGY"
        assert profile.confidence == ConfidenceLevel.HIGH

@pytest.mark.asyncio
async def test_api_company_enrichment_refresh_and_get(session_factory, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    from app.main import get_db
    app.dependency_overrides[get_db] = lambda: session_factory()
    from app.config import settings
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    headers = {"Authorization": "Bearer api-test-token"}
    
    async with session_factory() as session:
        lead = Lead(
            company_name="API Corp",
            website="https://api-corp.io"
        )
        session.add(lead)
        await session.commit()
        lead_id = lead.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # GET should return 404 initially
        res1 = await client.get(f"/api/companies/{lead_id}/enrichment", headers=headers)
        assert res1.status_code == 404
        
        # POST refresh should create and return
        res2 = await client.post(f"/api/companies/{lead_id}/enrichment/refresh", headers=headers)
        assert res2.status_code == 200
        data = res2.json()
        assert data["legal_name"] == "API Corp"
        assert data["domain"] == "api-corp.io"
        assert data["completeness_percent"] > 0
        
        # GET should now return 200
        res3 = await client.get(f"/api/companies/{lead_id}/enrichment", headers=headers)
        assert res3.status_code == 200
        assert res3.json()["id"] == data["id"]
        
        # POST refresh again should be idempotent
        res4 = await client.post(f"/api/companies/{lead_id}/enrichment/refresh", headers=headers)
        assert res4.status_code == 200
        assert res4.json()["id"] != data["id"] # It creates a new ID on replace, but data is same
        assert res4.json()["legal_name"] == "API Corp"

@pytest.mark.asyncio
async def test_api_company_enrichment_404_for_unknown(session_factory, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    from app.main import get_db
    app.dependency_overrides[get_db] = lambda: session_factory()
    from app.config import settings
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    headers = {"Authorization": "Bearer api-test-token"}
    
    unknown_id = str(uuid.uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res1 = await client.get(f"/api/companies/{unknown_id}/enrichment", headers=headers)
        assert res1.status_code == 404
        
        res2 = await client.post(f"/api/companies/{unknown_id}/enrichment/refresh", headers=headers)
        assert res2.status_code == 404

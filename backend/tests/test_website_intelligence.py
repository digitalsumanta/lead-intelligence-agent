import uuid
import pytest
from httpx import AsyncClient, ASGITransport

from app.models.entities import Lead, CompanyEnrichmentProfile, WebsiteIntelligenceProfile, SourceType, ConfidenceLevel
from app.domain.website_intelligence import (
    normalize_website,
    normalize_domain,
    calculate_website_completeness
)

def test_domain_normalization():
    assert normalize_domain("  example.com  ") == "example.com"
    assert normalize_domain("HTTP://WWW.EXAMPLE.COM/PATH?q=1") == "example.com"
    assert normalize_domain("www.example.co.uk.") == "example.co.uk"
    assert normalize_domain("invalid domain") is None
    assert normalize_domain(None) is None
    assert normalize_domain("http://") is None

def test_website_normalization():
    assert normalize_website("  example.com  ") == ("https://example.com/", "example.com")
    assert normalize_website("http://www.example.com/path?track=1#anchor") == ("http://www.example.com/path", "example.com")
    assert normalize_website("www.example.co.uk") == ("https://www.example.co.uk/", "example.co.uk")
    assert normalize_website(None) == (None, None)

def test_completeness_calculation():
    assert calculate_website_completeness(True, True, False, False) == 80.0
    assert calculate_website_completeness(False, False, False, False) == 0.0
    assert calculate_website_completeness(True, True, True, True) == 100.0

@pytest.mark.asyncio
async def test_build_deterministic_website_profile(session_factory):
    async with session_factory() as session:
        lead = Lead(
            company_name="Test Corp",
            website="http://www.test-corp.com/home",
            website_analysis={"technologies": ["React"], "meta_description": "We do testing"}
        )
        session.add(lead)
        await session.commit()
        lead_id = lead.id

    from app.services.website_intelligence import build_deterministic_website_profile
    async with session_factory() as session:
        lead_record = await session.get(Lead, lead_id)
        profile = build_deterministic_website_profile(lead_record)
        
        assert profile.normalized_website == "http://www.test-corp.com/home"
        assert profile.normalized_domain == "test-corp.com"
        assert profile.website_present is True
        assert profile.domain_present is True
        assert profile.technology_signals == {"technologies": ["React"]}
        assert profile.content_signals == {"meta_description": "We do testing"}
        assert profile.completeness == 100.0

@pytest.mark.asyncio
async def test_api_website_intelligence_refresh_and_get(session_factory, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    from app.main import get_db, app
    app.dependency_overrides[get_db] = lambda: session_factory()
    
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
        res1 = await client.get(f"/api/companies/{lead_id}/website-intelligence", headers=headers)
        assert res1.status_code == 404
        
        # POST refresh should create and return
        res2 = await client.post(f"/api/companies/{lead_id}/website-intelligence/refresh", headers=headers)
        assert res2.status_code == 200
        data = res2.json()
        assert data["normalized_domain"] == "api-corp.io"
        assert data["website_present"] is True
        
        # GET should now return 200
        res3 = await client.get(f"/api/companies/{lead_id}/website-intelligence", headers=headers)
        assert res3.status_code == 200
        assert res3.json()["id"] == data["id"]
        
        # POST refresh again should be idempotent
        res4 = await client.post(f"/api/companies/{lead_id}/website-intelligence/refresh", headers=headers)
        assert res4.status_code == 200
        assert res4.json()["id"] != data["id"] 

@pytest.mark.asyncio
async def test_api_website_intelligence_404_for_unknown(session_factory, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    from app.main import get_db, app
    app.dependency_overrides[get_db] = lambda: session_factory()
    
    headers = {"Authorization": "Bearer api-test-token"}
    unknown_id = str(uuid.uuid4())
    
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res1 = await client.get(f"/api/companies/{unknown_id}/website-intelligence", headers=headers)
        assert res1.status_code == 404
        
        res2 = await client.post(f"/api/companies/{unknown_id}/website-intelligence/refresh", headers=headers)
        assert res2.status_code == 404

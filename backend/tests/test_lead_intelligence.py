import uuid
import pytest
from httpx import AsyncClient, ASGITransport
from datetime import datetime, UTC
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.main import app, get_db
from app.models.entities import Lead, LeadIntelligenceProfile, LeadIntelligencePainPoint, LeadIntelligenceOpportunity
from app.domain.lead_intelligence import build_deterministic_profile, _normalize_industry, _normalize_company_size

@pytest.mark.asyncio
async def test_lead_intelligence_normalization():
    assert _normalize_industry("  Software Development  ") == "TECHNOLOGY"
    assert _normalize_industry("Finance") == "FINANCE"
    assert _normalize_industry("Space Mining") == "UNKNOWN"
    assert _normalize_industry("") is None
    
    assert _normalize_company_size("1-10 employees") == "MICRO"
    assert _normalize_company_size("51-200") == "MEDIUM"
    assert _normalize_company_size("Huge") == "UNKNOWN"

@pytest.mark.asyncio
async def test_build_deterministic_profile_empty():
    lead = Lead(id=uuid.uuid4(), company_name="Empty Lead")
    profile, pains, opps = build_deterministic_profile(lead)
    
    assert profile.completeness_score > 0  # 1/12 populated (company_name)
    assert profile.industry is None
    assert profile.industry_confidence == "UNKNOWN"
    assert len(pains) == 0
    assert len(opps) == 0

@pytest.mark.asyncio
async def test_build_deterministic_profile_populated():
    lead = Lead(
        id=uuid.uuid4(),
        company_name="Tech Corp",
        website="https://techcorp.com",
        industry="Software",
        city="San Francisco",
        country="USA",
        email="hello@techcorp.com",
        phone="555-1234",
        decision_maker="Alice",
        linkedin_url="https://linkedin.com/company/techcorp",
        ai_summary="A great tech company.",
        pain_points=["Need better leads"],
        keyword_opportunities=["CRM"],
        recommended_services=["Consulting"]
    )
    profile, pains, opps = build_deterministic_profile(lead)
    
    assert profile.completeness_score == pytest.approx(83.33, rel=1e-2)
    assert profile.industry == "TECHNOLOGY"
    assert profile.industry_source == "LEAD_RECORD"
    assert profile.industry_confidence == "MEDIUM"
    assert profile.location == "San Francisco, USA"
    assert profile.country == "USA"
    
    assert len(pains) == 1
    assert pains[0].title == "Need better leads"
    assert pains[0].confidence == "HIGH"
    assert pains[0].source_reference == "lead.pain_points"

    assert len(opps) == 1
    assert opps[0].title == "CRM"


@pytest.mark.asyncio
async def test_api_lead_intelligence_refresh(session_factory, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "api_auth_token", "test-token")
    async with session_factory() as session:
        lead = Lead(id=uuid.uuid4(), company_name="API Refresh Corp", pain_points=["Pain 1"])
        session.add(lead)
        await session.commit()
        lead_id = lead.id

    def override_get_db():
        return session_factory()
        
    app.dependency_overrides[get_db] = override_get_db
    
    headers = {"Authorization": "Bearer test-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # GET empty profile first (returns derived, but doesn't store)
        res = await client.get(f"/api/leads/{lead_id}/intelligence", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["company"]["name"] == "API Refresh Corp"
        assert len(data["business_context"]["pain_points"]) == 1

        # POST refresh
        res2 = await client.post(f"/api/leads/{lead_id}/intelligence/refresh", headers=headers)
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["company"]["name"] == "API Refresh Corp"

        # Verify idempotency by posting again
        res3 = await client.post(f"/api/leads/{lead_id}/intelligence/refresh", headers=headers)
        assert res3.status_code == 200

    # Ensure no duplicates in DB
    async with session_factory() as session:
        p_count = await session.scalar(select(LeadIntelligencePainPoint).where(LeadIntelligencePainPoint.lead_id == lead_id))
        # Should only be one pain point! (I need to check count, wait scalar just returns the first, I need count)
        from sqlalchemy import func
        count_p = await session.scalar(select(func.count()).select_from(LeadIntelligencePainPoint).where(LeadIntelligencePainPoint.lead_id == lead_id))
        assert count_p == 1

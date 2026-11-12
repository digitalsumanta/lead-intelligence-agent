import uuid
import pytest
from httpx import AsyncClient, ASGITransport

from app.models.entities import Lead, CompanyEnrichmentProfile, WebsiteIntelligenceProfile, SeoAuditRun, SourceType, ConfidenceLevel
from app.domain.digital_marketing_intelligence import (
    normalize_marketing_signal,
    normalize_signal_source,
    derive_marketing_maturity,
    derive_marketing_gaps,
    derive_marketing_opportunities,
    calculate_digital_marketing_completeness
)

def test_normalization():
    assert normalize_marketing_signal({}) is None
    assert normalize_marketing_signal(None) is None
    assert normalize_marketing_signal([]) is None
    assert normalize_marketing_signal({"key": "val"}) == {"key": "val"}
    assert normalize_marketing_signal(["val"]) == {"items": ["val"]}

    assert normalize_signal_source("test") == "TEST"
    assert normalize_signal_source(None) == "UNKNOWN"

def test_derivation_logic():
    assert derive_marketing_maturity(False, False, False, False) == "UNKNOWN"
    assert derive_marketing_maturity(True, False, False, False) == "FOUNDATIONAL"
    assert derive_marketing_maturity(True, True, True, True) == "ADVANCED"
    
    gaps = derive_marketing_gaps(False, True, True, True)
    assert len(gaps) == 1
    assert "No persisted SEO signal found" in gaps[0]
    
    ops = derive_marketing_opportunities(False, True, True, True)
    assert "SEO opportunity signal" in ops
    assert "Cross-channel marketing opportunity signal" in ops

    assert calculate_digital_marketing_completeness(True, True, False, False) == 50.0
    assert calculate_digital_marketing_completeness(True, True, True, True) == 100.0

@pytest.mark.asyncio
async def test_build_deterministic_marketing_profile(session_factory):
    async with session_factory() as session:
        lead = Lead(
            company_name="Digital Corp",
            website_analysis={"technologies": ["Google Ads", "React"], "social_links": ["https://twitter.com/test"]},
            ai_analysis={"strategy": "active"}
        )
        session.add(lead)
        await session.flush()
        
        enrich = CompanyEnrichmentProfile(
            lead_id=lead.id,
            linkedin_url="https://linkedin.com/company/test",
            source=SourceType.LEAD_RECORD,
            confidence=ConfidenceLevel.HIGH
        )
        session.add(enrich)
        
        web = WebsiteIntelligenceProfile(
            company_id=lead.id,
            technology_signals={"technologies": ["Meta Pixel"]},
            source_type=SourceType.LEAD_RECORD,
            confidence=ConfidenceLevel.HIGH
        )
        session.add(web)
        
        await session.commit()
        lead_id = lead.id

    from app.services.digital_marketing_intelligence import build_digital_marketing_profile
    async with session_factory() as session:
        l = await session.get(Lead, lead_id)
        from sqlalchemy import select; e = (await session.execute(select(CompanyEnrichmentProfile).filter_by(lead_id=lead_id))).scalar_one_or_none()
        w = (await session.execute(select(WebsiteIntelligenceProfile).filter_by(company_id=lead_id))).scalar_one_or_none()
        
        profile = build_digital_marketing_profile(l, e, w, has_seo_run=True)
        
        assert profile.seo_signals == {"seo_audit_present": True}
        assert profile.social_media_signals == {"linkedin_present": True, "linkedin_url": "https://linkedin.com/company/test", "social_links": ["https://twitter.com/test"]}
        assert profile.paid_advertising_signals == {"advertising_account_signal": True}
        assert profile.content_marketing_signals == {"content_strategy_signal": True}
        assert profile.marketing_maturity == "ADVANCED"
        assert profile.completeness == 100.0

@pytest.mark.asyncio
async def test_api_digital_marketing_intelligence_refresh_and_get(session_factory, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    from app.main import get_db, app
    app.dependency_overrides[get_db] = lambda: session_factory()
    
    headers = {"Authorization": "Bearer api-test-token"}
    
    async with session_factory() as session:
        lead = Lead(
            company_name="API DM Corp"
        )
        session.add(lead)
        await session.commit()
        lead_id = lead.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # GET should return 404 initially
        res1 = await client.get(f"/api/companies/{lead_id}/digital-marketing-intelligence", headers=headers)
        assert res1.status_code == 404
        
        # POST refresh should create and return
        res2 = await client.post(f"/api/companies/{lead_id}/digital-marketing-intelligence/refresh", headers=headers)
        assert res2.status_code == 200
        data = res2.json()
        assert data["marketing_maturity"] == "UNKNOWN"
        assert data["completeness"] == 0.0
        assert len(data["marketing_gaps"]) == 4
        
        # GET should now return 200
        res3 = await client.get(f"/api/companies/{lead_id}/digital-marketing-intelligence", headers=headers)
        assert res3.status_code == 200
        assert res3.json()["id"] == data["id"]
        
        # POST refresh again should be idempotent
        res4 = await client.post(f"/api/companies/{lead_id}/digital-marketing-intelligence/refresh", headers=headers)
        assert res4.status_code == 200
        assert res4.json()["id"] == data["id"]

@pytest.mark.asyncio
async def test_api_digital_marketing_intelligence_404_for_unknown(session_factory, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    from app.main import get_db, app
    app.dependency_overrides[get_db] = lambda: session_factory()
    
    headers = {"Authorization": "Bearer api-test-token"}
    unknown_id = str(uuid.uuid4())
    
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        res1 = await client.get(f"/api/companies/{unknown_id}/digital-marketing-intelligence", headers=headers)
        assert res1.status_code == 404
        
        res2 = await client.post(f"/api/companies/{unknown_id}/digital-marketing-intelligence/refresh", headers=headers)
        assert res2.status_code == 404

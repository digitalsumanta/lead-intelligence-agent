import pytest
import uuid
from datetime import datetime, timezone
from httpx import AsyncClient, ASGITransport

from app.domain.sales_opportunities import derive_opportunity_from_signals
from app.models.entities import BuyingSignal, ConfidenceLevel, SourceType, Lead
from app.services.sales_opportunities import refresh_sales_opportunities_for_lead

def test_derive_opportunity_from_signals_empty():
    lead_id = uuid.uuid4()
    assert len(derive_opportunity_from_signals(lead_id, [])) == 0

def test_derive_opportunity_standalone_website_rejected():
    lead_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    s1 = BuyingSignal(signal_type="WEBSITE_PRESENCE_CONFIRMED", signal_category="website_activity", detected_at=now, source_type=SourceType.SYSTEM_DERIVED, confidence=ConfidenceLevel.HIGH)
    
    opps = derive_opportunity_from_signals(lead_id, [s1])
    assert len(opps) == 0

def test_derive_opportunity_standalone_completeness_rejected():
    lead_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    s1 = BuyingSignal(signal_type="HIGH_PROFILE_COMPLETENESS", signal_category="company_activity", detected_at=now, source_type=SourceType.SYSTEM_DERIVED, confidence=ConfidenceLevel.HIGH)
    
    opps = derive_opportunity_from_signals(lead_id, [s1])
    assert len(opps) == 0

def test_derive_opportunity_combined_success():
    lead_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    s1 = BuyingSignal(signal_type="MARKETING_MATURITY_SIGNAL", signal_category="marketing_activity", detected_at=now, source_type=SourceType.SYSTEM_DERIVED, confidence=ConfidenceLevel.HIGH)
    s2 = BuyingSignal(signal_type="WEBSITE_PRESENCE_CONFIRMED", signal_category="website_activity", detected_at=now, source_type=SourceType.SYSTEM_DERIVED, confidence=ConfidenceLevel.HIGH)
    s3 = BuyingSignal(signal_type="HIGH_PROFILE_COMPLETENESS", signal_category="company_activity", detected_at=now, source_type=SourceType.SYSTEM_DERIVED, confidence=ConfidenceLevel.HIGH)

    opps = derive_opportunity_from_signals(lead_id, [s1, s2, s3])
    
    # Expect 1 combined opportunity
    assert len(opps) == 1
    
    assert opps[0].opportunity_type == "DIGITAL_MARKETING"
    assert opps[0].score == 55 # 25 (1 marketing) + 10 (website) + 20 (complete)
    assert opps[0].recommended_action == "REVIEW_MARKETING"
    assert opps[0].priority == "HIGH"
    
    # Verify evidence chain
    assert opps[0].evidence["marketing_count"] == 1
    assert opps[0].evidence["has_website"] is True
    assert opps[0].evidence["is_complete"] is True
    assert len(opps[0].evidence["signals"]) == 3

def test_derive_opportunity_prepare_outreach():
    lead_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    # 3 marketing signals (75 score) + complete (20) = 95
    s1 = BuyingSignal(signal_type="MARKETING_MATURITY_SIGNAL", signal_category="marketing_activity", detected_at=now, source_type=SourceType.SYSTEM_DERIVED, confidence=ConfidenceLevel.HIGH)
    s2 = BuyingSignal(signal_type="MARKETING_OPPORTUNITY_DETECTED", signal_category="marketing_activity", detected_at=now, source_type=SourceType.SYSTEM_DERIVED, confidence=ConfidenceLevel.HIGH)
    s3 = BuyingSignal(signal_type="SEO_OPPORTUNITY_DETECTED", signal_category="seo_activity", detected_at=now, source_type=SourceType.SYSTEM_DERIVED, confidence=ConfidenceLevel.HIGH)
    s4 = BuyingSignal(signal_type="HIGH_PROFILE_COMPLETENESS", signal_category="company_activity", detected_at=now, source_type=SourceType.SYSTEM_DERIVED, confidence=ConfidenceLevel.HIGH)

    opps = derive_opportunity_from_signals(lead_id, [s1, s2, s3, s4])
    assert len(opps) == 1
    assert opps[0].score == 95
    assert opps[0].recommended_action == "PREPARE_OUTREACH"

@pytest.mark.asyncio
async def test_api_sales_opportunities_404(session_factory, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    from app.main import get_db, app
    app.dependency_overrides[get_db] = lambda: session_factory()
    headers = {"Authorization": "Bearer api-test-token"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        random_id = str(uuid.uuid4())
        res = await client.get(f"/api/leads/{random_id}/sales-opportunities", headers=headers)
        assert res.status_code == 404
        res = await client.post(f"/api/leads/{random_id}/sales-opportunities/refresh", headers=headers)
        assert res.status_code == 404

@pytest.mark.asyncio
async def test_api_sales_opportunities_flow(session_factory, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    from app.main import get_db, app
    app.dependency_overrides[get_db] = lambda: session_factory()
    headers = {"Authorization": "Bearer api-test-token"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with session_factory() as session:
            lead = Lead(company_name="Opp Corp")
            session.add(lead)
            await session.commit()
            lead_id = lead.id

        res = await client.post(f"/api/leads/{lead_id}/sales-opportunities/refresh", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["opportunities"] == []

        res = await client.get(f"/api/leads/{lead_id}/sales-opportunities", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["opportunities"] == []

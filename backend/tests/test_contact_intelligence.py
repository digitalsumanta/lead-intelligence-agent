import pytest
from httpx import AsyncClient
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.entities import Lead, ContactIntelligenceProfile
from app.services.contact_intelligence import refresh_contact_intelligence

@pytest.mark.asyncio
async def test_refresh_contact_intelligence_idempotent(session_factory):
    async with session_factory() as session:
        # Create lead acting as contact
        lead_id = uuid.uuid4()
        lead = Lead(
            id=lead_id,
            company_name="Test Company",
            email="ceo@test.com",
            phone="1234567890",
            decision_maker="John Doe",
            currency="USD"
        )
        session.add(lead)
        await session.commit()

        # Refresh
        profile1 = await refresh_contact_intelligence(session, lead_id)
        assert profile1 is not None
        assert profile1.contact_id == lead_id
        # No job_title stored in DB so it should evaluate to UNKNOWN but get email/phone score
        assert profile1.decision_maker_score == 10
        assert profile1.seniority_level.value == "UNKNOWN"

        # Refresh again
        profile2 = await refresh_contact_intelligence(session, lead_id)
        assert profile2 is not None
        assert profile1.id == profile2.id

        # Verify 1:1 relationship in DB
        result = await session.execute(select(ContactIntelligenceProfile).where(ContactIntelligenceProfile.contact_id == lead_id))
        profiles = result.scalars().all()
        assert len(profiles) == 1

@pytest.mark.asyncio
async def test_refresh_contact_intelligence_unknown_contact(session_factory):
    async with session_factory() as session:
        profile = await refresh_contact_intelligence(session, uuid.uuid4())
        assert profile is None

from httpx import AsyncClient, ASGITransport
from app.main import app

@pytest.mark.asyncio
async def test_get_contact_intelligence_api(session_factory, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    
    async with session_factory() as session:
        lead_id = uuid.uuid4()
        lead = Lead(
            id=lead_id,
            company_name="Test API",
            currency="USD"
        )
        session.add(lead)
        await session.commit()

    from app.main import get_db, app
    app.dependency_overrides[get_db] = lambda: session_factory()
    headers = {"Authorization": "Bearer api-test-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Refresh via API
        res = await client.post(f"/api/contacts/{lead_id}/intelligence/refresh", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["contact_id"] == str(lead_id)

        # Get via API
        res = await client.get(f"/api/contacts/{lead_id}/intelligence", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["contact_id"] == str(lead_id)
        assert data["seniority_level"] == "UNKNOWN"

@pytest.mark.asyncio
async def test_api_unknown_contact(session_factory, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    
    from app.main import get_db, app
    app.dependency_overrides[get_db] = lambda: session_factory()

    headers = {"Authorization": "Bearer api-test-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        bad_id = str(uuid.uuid4())
        res = await client.get(f"/api/contacts/{bad_id}/intelligence", headers=headers)
        assert res.status_code == 404
        
        res = await client.post(f"/api/contacts/{bad_id}/intelligence/refresh", headers=headers)
        assert res.status_code == 404

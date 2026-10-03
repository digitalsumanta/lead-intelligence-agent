import pytest
from datetime import datetime, timezone
import uuid
from httpx import AsyncClient, ASGITransport

from app.domain.buying_signals import (
    normalize_signal_type,
    normalize_signal_category,
    normalize_signal_source,
    calculate_signal_confidence,
    calculate_signal_strength,
    build_buying_signal,
    deduplicate_signals,
    sort_signals_by_recency
)
from app.models.entities import ConfidenceLevel, SourceType, SignalStrength, BuyingSignal, Lead
from app.services.buying_signals import get_buying_signals_for_company, refresh_buying_signals_for_company

def test_normalize_signal_type():
    assert normalize_signal_type("  WEBSITE   presence ") == "WEBSITE___PRESENCE"
    assert normalize_signal_type("NEW_SEO_ACTIVITY") == "NEW_SEO_ACTIVITY"

def test_normalize_signal_category():
    assert normalize_signal_category(" MARKETING  Activity ") == "marketing__activity"

def test_normalize_signal_source():
    assert normalize_signal_source("LEAD_RECORD") == SourceType.LEAD_RECORD
    assert normalize_signal_source("UNKNOWN_SOURCE") == SourceType.SYSTEM_DERIVED

def test_calculate_signal_confidence():
    assert calculate_signal_confidence({}) == ConfidenceLevel.UNKNOWN
    assert calculate_signal_confidence({"original_confidence": "HIGH"}) == ConfidenceLevel.HIGH
    assert calculate_signal_confidence({"k1": 1, "k2": 2}) == ConfidenceLevel.MEDIUM
    assert calculate_signal_confidence({"k1": 1, "k2": 2, "k3": 3, "k4": 4}) == ConfidenceLevel.HIGH

def test_calculate_signal_strength():
    assert calculate_signal_strength({}) == SignalStrength.LOW
    assert calculate_signal_strength({"channels_involved": 1}) == SignalStrength.LOW
    assert calculate_signal_strength({"channels_involved": 2}) == SignalStrength.MEDIUM
    assert calculate_signal_strength({"channels_involved": 3}) == SignalStrength.HIGH

def test_build_buying_signal():
    dt = datetime(2023, 1, 1, tzinfo=timezone.utc)
    company_id = uuid.uuid4()
    signal = build_buying_signal(
        company_id=company_id,
        raw_type="TEST_SIGNAL",
        raw_category="test_category",
        raw_source="LEAD_RECORD",
        evidence={"k1": "v1"},
        detected_at=dt
    )
    assert signal.signal_type == "TEST_SIGNAL"
    assert signal.signal_category == "test_category"
    assert signal.source_type == SourceType.LEAD_RECORD
    assert signal.confidence == ConfidenceLevel.LOW
    assert signal.strength == SignalStrength.LOW
    assert signal.detected_at == dt
    assert signal.company_id == company_id

def test_deduplicate_signals():
    dt = datetime(2023, 1, 1, tzinfo=timezone.utc)
    s1 = BuyingSignal(signal_type="TEST", detected_at=dt)
    s2 = BuyingSignal(signal_type="TEST", detected_at=dt)
    s3 = BuyingSignal(signal_type="OTHER", detected_at=dt)
    unique = deduplicate_signals([s1, s2, s3])
    assert len(unique) == 2
    types = [u.signal_type for u in unique]
    assert "TEST" in types and "OTHER" in types

def test_sort_signals_by_recency():
    dt1 = datetime(2023, 1, 1, tzinfo=timezone.utc)
    dt2 = datetime(2023, 1, 2, tzinfo=timezone.utc)
    s1 = BuyingSignal(detected_at=dt1)
    s2 = BuyingSignal(detected_at=dt2)
    sorted_sigs = sort_signals_by_recency([s1, s2])
    assert sorted_sigs[0] == s2
    assert sorted_sigs[1] == s1


@pytest.mark.asyncio
async def test_api_buying_signals_404(session_factory, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    from app.main import get_db, app
    app.dependency_overrides[get_db] = lambda: session_factory()
    headers = {"Authorization": "Bearer api-test-token"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        random_id = str(uuid.uuid4())
        res = await client.get(f"/api/companies/{random_id}/buying-signals", headers=headers)
        assert res.status_code == 404
        res = await client.post(f"/api/companies/{random_id}/buying-signals/refresh", headers=headers)
        assert res.status_code == 404
        res = await client.get(f"/api/leads/{random_id}/buying-signals", headers=headers)
        assert res.status_code == 404
        res = await client.post(f"/api/leads/{random_id}/buying-signals/refresh", headers=headers)
        assert res.status_code == 404

@pytest.mark.asyncio
async def test_api_buying_signals_flow(session_factory, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    from app.main import get_db, app
    app.dependency_overrides[get_db] = lambda: session_factory()
    headers = {"Authorization": "Bearer api-test-token"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with session_factory() as session:
            lead = Lead(company_name="Buying Corp")
            session.add(lead)
            await session.commit()
            lead_id = lead.id

        res = await client.post(f"/api/companies/{lead_id}/buying-signals/refresh", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["signals"] == []

        res = await client.get(f"/api/companies/{lead_id}/buying-signals", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["signals"] == []

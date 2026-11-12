import pytest
import uuid
from datetime import datetime, timezone
from httpx import AsyncClient, ASGITransport

from app.domain.sales_opportunities import derive_opportunity_from_signals
from app.models.entities import BuyingSignal, ConfidenceLevel, SourceType, SignalStrength, Lead
from app.services.sales_opportunities import refresh_sales_opportunities_for_lead, get_sales_opportunities_for_lead


def test_derive_opportunity_from_signals_empty():
    lead_id = uuid.uuid4()
    assert len(derive_opportunity_from_signals(lead_id, [])) == 0


def test_derive_opportunity_standalone_website_rejected():
    lead_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    s1 = BuyingSignal(
        signal_type="WEBSITE_PRESENCE_CONFIRMED",
        signal_category="website_activity",
        detected_at=now,
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH
    )
    opps = derive_opportunity_from_signals(lead_id, [s1])
    assert len(opps) == 0


def test_derive_opportunity_standalone_completeness_rejected():
    lead_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    s1 = BuyingSignal(
        signal_type="HIGH_PROFILE_COMPLETENESS",
        signal_category="company_activity",
        detected_at=now,
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH
    )
    opps = derive_opportunity_from_signals(lead_id, [s1])
    assert len(opps) == 0


def test_derive_opportunity_combined_success():
    lead_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    s1 = BuyingSignal(
        signal_type="MARKETING_MATURITY_SIGNAL",
        signal_category="marketing_activity",
        detected_at=now,
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH
    )
    s2 = BuyingSignal(
        signal_type="WEBSITE_PRESENCE_CONFIRMED",
        signal_category="website_activity",
        detected_at=now,
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH
    )
    s3 = BuyingSignal(
        signal_type="HIGH_PROFILE_COMPLETENESS",
        signal_category="company_activity",
        detected_at=now,
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH
    )

    opps = derive_opportunity_from_signals(lead_id, [s1, s2, s3])
    
    assert len(opps) == 1
    assert opps[0].opportunity_type == "DIGITAL_MARKETING"
    assert opps[0].score == 55  # 25 (1 marketing) + 10 (website) + 20 (complete)
    assert opps[0].recommended_action == "REVIEW_MARKETING"
    assert opps[0].priority == "HIGH"
    assert opps[0].evidence["marketing_count"] == 1
    assert opps[0].evidence["has_website"] is True
    assert opps[0].evidence["is_complete"] is True
    assert len(opps[0].evidence["signals"]) == 3


def test_derive_opportunity_prepare_outreach():
    lead_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    s1 = BuyingSignal(
        signal_type="MARKETING_MATURITY_SIGNAL",
        signal_category="marketing_activity",
        detected_at=now,
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH
    )
    s2 = BuyingSignal(
        signal_type="MARKETING_OPPORTUNITY_DETECTED",
        signal_category="marketing_activity",
        detected_at=now,
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH
    )
    s3 = BuyingSignal(
        signal_type="SEO_OPPORTUNITY_DETECTED",
        signal_category="seo_activity",
        detected_at=now,
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH
    )
    s4 = BuyingSignal(
        signal_type="HIGH_PROFILE_COMPLETENESS",
        signal_category="company_activity",
        detected_at=now,
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH
    )

    opps = derive_opportunity_from_signals(lead_id, [s1, s2, s3, s4])
    assert len(opps) == 1
    assert opps[0].score == 95
    assert opps[0].recommended_action == "PREPARE_OUTREACH"


# === REGRESSION SUITE: 7 SCENARIOS ===

def test_regression_scenario_1_lead_without_buying_signals():
    """Scenario 1: Lead with no buying signals produces 0 opportunities."""
    lead_id = uuid.uuid4()
    opps = derive_opportunity_from_signals(lead_id, [])
    assert opps == []


def test_regression_scenario_2_lead_with_website_only_signals():
    """Scenario 2: Lead with website-only presence cannot create standalone opportunity."""
    lead_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    signals = [
        BuyingSignal(
            signal_type="WEBSITE_PRESENCE_CONFIRMED",
            signal_category="website_activity",
            detected_at=now,
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH
        )
    ]
    opps = derive_opportunity_from_signals(lead_id, signals)
    assert opps == []


def test_regression_scenario_3_rubye_beauty_advertising_signal():
    """
    Scenario 3: Target Lead 'Rubye Beauty Pvt Ltd' with verified PAID_ACQUISITION_INVESTMENT
    and WEBSITE_PRESENCE_CONFIRMED.
    Must generate evidence-grounded investigative opportunity without fabricating campaign failure.
    """
    lead_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    s1 = BuyingSignal(
        signal_type="WEBSITE_PRESENCE_CONFIRMED",
        signal_category="website_activity",
        detected_at=now,
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.MEDIUM,
        strength=SignalStrength.MEDIUM,
        evidence={"website": "https://rubyeorganics.com"}
    )
    s2 = BuyingSignal(
        signal_type="PAID_ACQUISITION_INVESTMENT",
        signal_category="advertising_activity",
        detected_at=now,
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH,
        strength=SignalStrength.HIGH,
        evidence={
            "platforms": "Meta Ads, Google Ads, Google Shopping",
            "notes": "Active ad campaigns detected across Meta Ad Library and Google Shopping for Rubye Organics; running multi-angle video and catalog assets."
        }
    )

    opps = derive_opportunity_from_signals(lead_id, [s1, s2])
    assert len(opps) == 1
    opp = opps[0]

    assert opp.opportunity_type == "PAID_ADVERTISING"
    assert opp.priority == "HIGH"
    assert opp.score >= 85
    assert opp.source_reference == "buying_signals.paid_acquisition"
    assert opp.confidence == ConfidenceLevel.HIGH
    assert opp.recommended_action == "AUDIT_AD_PERFORMANCE_AND_CRO"
    assert "Paid Acquisition & Performance Marketing Optimization" in opp.title

    ev = opp.evidence
    assert ev["opportunity_status"] == "INVESTIGATE"
    assert ev["recommended_service"] == "Performance Marketing & Landing Page CRO"
    assert "Meta Ads, Google Ads, Google Shopping" in ev["platforms"]
    assert "commercial readiness" in ev["commercial_relevance"].lower()
    assert "audit" in ev["suggested_next_action"].lower()
    assert ev["has_website"] is True


def test_regression_scenario_4_lead_with_seo_signals():
    """Scenario 4: Lead with SEO signals generates actionable digital growth opportunity."""
    lead_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    signals = [
        BuyingSignal(
            signal_type="WEBSITE_PRESENCE_CONFIRMED",
            signal_category="website_activity",
            detected_at=now,
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH
        ),
        BuyingSignal(
            signal_type="SEO_OPPORTUNITY_DETECTED",
            signal_category="seo_activity",
            detected_at=now,
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
            evidence={"issue": "Missing meta tags and low mobile score"}
        )
    ]
    opps = derive_opportunity_from_signals(lead_id, signals)
    assert len(opps) == 1
    assert opps[0].opportunity_type == "DIGITAL_MARKETING"
    assert opps[0].score >= 35


def test_regression_scenario_5_lead_with_multiple_signals():
    """Scenario 5: Lead with both advertising and digital marketing gap signals generates prioritized opportunities."""
    lead_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    s_web = BuyingSignal(
        signal_type="WEBSITE_PRESENCE_CONFIRMED",
        signal_category="website_activity",
        detected_at=now,
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH
    )
    s_ad = BuyingSignal(
        signal_type="PAID_ACQUISITION_INVESTMENT",
        signal_category="advertising_activity",
        detected_at=now,
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH,
        strength=SignalStrength.HIGH,
        evidence={"platforms": "Meta Ads, Google Ads"}
    )
    s_gap = BuyingSignal(
        signal_type="HIGH_IMPACT_DIGITAL_GAP",
        signal_category="marketing_opportunity",
        detected_at=now,
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH,
        evidence={"opportunities": ["SEO issue", "Speed issue"], "count": 2}
    )

    opps = derive_opportunity_from_signals(lead_id, [s_web, s_ad, s_gap])
    assert len(opps) == 2
    types = {o.opportunity_type for o in opps}
    assert "PAID_ADVERTISING" in types
    assert "DIGITAL_MARKETING" in types
    assert opps[0].score >= opps[1].score


def test_regression_scenario_6_lead_with_insufficient_evidence():
    """Scenario 6: Handle empty/invalid evidence payloads gracefully without crashing."""
    lead_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    s1 = BuyingSignal(
        signal_type="PAID_ACQUISITION_INVESTMENT",
        signal_category="advertising_activity",
        detected_at=now,
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.LOW,
        evidence={}
    )
    opps = derive_opportunity_from_signals(lead_id, [s1])
    assert len(opps) == 1
    assert opps[0].opportunity_type == "PAID_ADVERTISING"
    assert opps[0].evidence["opportunity_status"] == "INVESTIGATE"


@pytest.mark.asyncio
async def test_regression_scenario_7_idempotent_refresh_and_persistence(session_factory):
    """Scenario 7: Repeated refresh operations do not duplicate records or delete unrelated evidence."""
    lead_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    async with session_factory() as session:
        lead = Lead(id=lead_id, company_name="Rubye Beauty Test", website="https://rubyeorganics.com")
        session.add(lead)
        sig1 = BuyingSignal(
            company_id=lead_id,
            signal_type="WEBSITE_PRESENCE_CONFIRMED",
            signal_category="website_activity",
            detected_at=now,
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
            evidence={"website": "https://rubyeorganics.com"}
        )
        sig2 = BuyingSignal(
            company_id=lead_id,
            signal_type="PAID_ACQUISITION_INVESTMENT",
            signal_category="advertising_activity",
            detected_at=now,
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
            strength=SignalStrength.HIGH,
            evidence={"platforms": "Meta Ads, Google Ads", "notes": "Active campaigns"}
        )
        session.add_all([sig1, sig2])
        await session.commit()

    # First refresh
    async with session_factory() as session:
        opps_1 = await refresh_sales_opportunities_for_lead(session, lead_id)
        assert len(opps_1) == 1
        assert opps_1[0]["title"] == "Paid Acquisition & Performance Marketing Optimization"
        assert opps_1[0]["opportunity_status"] == "INVESTIGATE"
        assert opps_1[0]["recommended_service"] == "Performance Marketing & Landing Page CRO"

    # Second refresh: must be idempotent, no duplicates
    async with session_factory() as session:
        opps_2 = await refresh_sales_opportunities_for_lead(session, lead_id)
        assert len(opps_2) == 1
        assert opps_2[0]["title"] == "Paid Acquisition & Performance Marketing Optimization"

    # GET endpoint retrieval: must return the exact same opportunity
    async with session_factory() as session:
        opps_get = await get_sales_opportunities_for_lead(session, lead_id)
        assert len(opps_get) == 1
        assert opps_get[0]["title"] == "Paid Acquisition & Performance Marketing Optimization"
        assert opps_get[0]["score"] >= 85


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

import pytest
import uuid
from datetime import datetime, timezone
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from app.models.entities import (
    Lead,
    LeadIntelligenceOpportunity,
    SalesTask,
    SalesTaskType,
    SalesTaskStatus,
    SalesPriority,
    SalesActivity,
    SalesActivityType,
    SalesActivityOutcome,
    SalesStatus,
    SourceType,
    ConfidenceLevel,
)
from app.domain.sales_feedback import (
    FeedbackSentiment,
    AttributionLevel,
    classify_outcome_sentiment,
    calculate_feedback_rates,
)
from app.services.sales_feedback import (
    extract_task_attribution,
    build_feedback_summary,
    get_opportunity_sales_feedback,
    get_contact_sales_feedback,
    get_recommendations_sales_feedback,
)
from app.config import settings
from app.main import app, get_db

AUTH_HEADERS = {"Authorization": "Bearer api-test-token"}


# ----------------------------------------------------
# 1. Pure Unit Tests: Sentiment & Rate Calculations
# ----------------------------------------------------
def test_positive_outcome_classification():
    positive_outcomes = [
        SalesActivityOutcome.CONNECTED,
        SalesActivityOutcome.INTERESTED,
        SalesActivityOutcome.CALLBACK_REQUESTED,
        SalesActivityOutcome.MEETING_BOOKED,
        SalesActivityOutcome.PROPOSAL_REQUESTED,
        SalesActivityOutcome.PROPOSAL_SENT,
        SalesActivityOutcome.NEGOTIATION_STARTED,
        SalesActivityOutcome.WON,
    ]
    for outcome in positive_outcomes:
        assert classify_outcome_sentiment(outcome) == FeedbackSentiment.POSITIVE
        assert classify_outcome_sentiment(outcome.value) == FeedbackSentiment.POSITIVE


def test_negative_outcome_classification():
    negative_outcomes = [
        SalesActivityOutcome.NOT_INTERESTED,
        SalesActivityOutcome.LOST,
        SalesActivityOutcome.INVALID_CONTACT,
    ]
    for outcome in negative_outcomes:
        assert classify_outcome_sentiment(outcome) == FeedbackSentiment.NEGATIVE
        assert classify_outcome_sentiment(outcome.value) == FeedbackSentiment.NEGATIVE


def test_neutral_outcome_classification():
    assert classify_outcome_sentiment(SalesActivityOutcome.NO_RESPONSE) == FeedbackSentiment.NEUTRAL
    assert classify_outcome_sentiment("NO_RESPONSE") == FeedbackSentiment.NEUTRAL


def test_unknown_outcome_classification():
    assert classify_outcome_sentiment(None) == FeedbackSentiment.UNKNOWN
    assert classify_outcome_sentiment("UNKNOWN_CUSTOM_OUTCOME") == FeedbackSentiment.UNKNOWN


def test_calculate_feedback_rates_normal():
    rates = calculate_feedback_rates(
        total_executions=10,
        positive_count=6,
        negative_count=3,
        neutral_count=1,
    )
    assert rates["positive_rate"] == 60.0
    assert rates["negative_rate"] == 30.0
    assert rates["neutral_rate"] == 10.0


def test_calculate_feedback_rates_zero_executions():
    rates = calculate_feedback_rates(
        total_executions=0,
        positive_count=0,
        negative_count=0,
        neutral_count=0,
    )
    assert rates["positive_rate"] == 0.0
    assert rates["negative_rate"] == 0.0
    assert rates["neutral_rate"] == 0.0


def test_extract_task_attribution_full():
    opp_id = uuid.uuid4()
    contact_id = uuid.uuid4()
    task = SalesTask(
        id=uuid.uuid4(),
        lead_id=contact_id,
        task_type=SalesTaskType.CALL,
        title="Discovery Call",
        status=SalesTaskStatus.COMPLETED,
        due_at=datetime.now(timezone.utc),
        agent_source_type="SALES_OPPORTUNITY",
        agent_source_id=opp_id,
        agent_source_action_id=f"contact_recommendation:{contact_id}:DISCOVERY_CALL",
        agent_evidence_refs=[
            f"contact:{contact_id}",
            f"opportunity:{opp_id}",
            f"recommendation:contact_recommendation:{contact_id}:DISCOVERY_CALL",
        ],
        related_activity_id=uuid.uuid4(),
    )
    act = SalesActivity(
        id=task.related_activity_id,
        lead_id=contact_id,
        activity_type=SalesActivityType.CALL,
        note="Call connected",
        outcome=SalesActivityOutcome.CONNECTED,
    )

    o_id, c_id, rec_action, attr_level = extract_task_attribution(task, act)
    assert o_id == opp_id
    assert c_id == contact_id
    assert rec_action == "DISCOVERY_CALL"
    assert attr_level == AttributionLevel.FULL


def test_extract_task_attribution_partial_and_unattributed():
    # Partial: Task has lead_id and activity with outcome, but no opportunity source
    contact_id = uuid.uuid4()
    task = SalesTask(
        id=uuid.uuid4(),
        lead_id=contact_id,
        task_type=SalesTaskType.CALL,
        title="Manual Call",
        status=SalesTaskStatus.COMPLETED,
        due_at=datetime.now(timezone.utc),
        related_activity_id=uuid.uuid4(),
    )
    act = SalesActivity(
        id=task.related_activity_id,
        lead_id=contact_id,
        activity_type=SalesActivityType.CALL,
        note="Spoke with lead",
        outcome=SalesActivityOutcome.CONNECTED,
    )
    o_id, c_id, rec_action, attr_level = extract_task_attribution(task, act)
    assert o_id is None
    assert c_id == contact_id
    assert rec_action is None
    assert attr_level == AttributionLevel.PARTIAL

    # Unattributed: No activity / outcome
    task_no_act = SalesTask(
        id=uuid.uuid4(),
        lead_id=contact_id,
        task_type=SalesTaskType.CALL,
        title="Open Call",
        status=SalesTaskStatus.OPEN,
        due_at=datetime.now(timezone.utc),
    )
    _, _, _, attr_level_unattr = extract_task_attribution(task_no_act, None)
    assert attr_level_unattr == AttributionLevel.UNATTRIBUTED


# ----------------------------------------------------
# 2. Service-Level Aggregation Tests
# ----------------------------------------------------
@pytest.mark.asyncio
async def test_opportunity_sales_feedback_aggregation(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Acme Corp", currency="USD")
        session.add(lead)
        await session.flush()

        opp = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="SEO Overhaul",
            opportunity_type="SEO",
            score=85,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add(opp)
        await session.flush()

        # Create 2 completed tasks with execution activities
        act1 = SalesActivity(
            lead_id=lead.id,
            activity_type=SalesActivityType.CALL,
            note="Discovery call succeeded",
            outcome=SalesActivityOutcome.MEETING_BOOKED,
        )
        act2 = SalesActivity(
            lead_id=lead.id,
            activity_type=SalesActivityType.EMAIL,
            note="Email sent",
            outcome=SalesActivityOutcome.NO_RESPONSE,
        )
        session.add_all([act1, act2])
        await session.flush()

        task1 = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Discovery Call: Jane",
            status=SalesTaskStatus.COMPLETED,
            completed_at=datetime.now(timezone.utc),
            due_at=datetime.now(timezone.utc),
            agent_source_type="SALES_OPPORTUNITY",
            agent_source_id=opp.id,
            agent_source_action_id=f"contact_recommendation:{lead.id}:DISCOVERY_CALL",
            agent_evidence_refs=[
                f"contact:{lead.id}",
                f"opportunity:{opp.id}",
                f"recommendation:contact_recommendation:{lead.id}:DISCOVERY_CALL",
            ],
            related_activity_id=act1.id,
        )
        task2 = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.EMAIL,
            title="Follow-up Email",
            status=SalesTaskStatus.COMPLETED,
            completed_at=datetime.now(timezone.utc),
            due_at=datetime.now(timezone.utc),
            agent_source_type="SALES_OPPORTUNITY",
            agent_source_id=opp.id,
            agent_source_action_id=f"contact_recommendation:{lead.id}:PERSONALIZED_EMAIL",
            agent_evidence_refs=[
                f"contact:{lead.id}",
                f"opportunity:{opp.id}",
                f"recommendation:contact_recommendation:{lead.id}:PERSONALIZED_EMAIL",
            ],
            related_activity_id=act2.id,
        )
        session.add_all([task1, task2])
        await session.commit()

        summary = await get_opportunity_sales_feedback(session, opp.id)
        assert summary is not None
        assert summary.scope == "opportunity"
        assert summary.scope_id == str(opp.id)
        assert summary.total_executions == 2
        assert summary.positive_count == 1
        assert summary.neutral_count == 1
        assert summary.negative_count == 0
        assert summary.positive_rate == 50.0
        assert summary.neutral_rate == 50.0
        assert summary.negative_rate == 0.0
        assert summary.outcome_distribution.get("MEETING_BOOKED") == 1
        assert summary.outcome_distribution.get("NO_RESPONSE") == 1
        assert summary.attribution_summary.get("FULL") == 2

        # Check action breakdown
        disc_call = summary.action_performance.get("DISCOVERY_CALL")
        assert disc_call is not None
        assert disc_call.total_executions == 1
        assert disc_call.positive_count == 1
        assert disc_call.positive_rate == 100.0

        email_act = summary.action_performance.get("PERSONALIZED_EMAIL")
        assert email_act is not None
        assert email_act.total_executions == 1
        assert email_act.neutral_count == 1
        assert email_act.neutral_rate == 100.0


@pytest.mark.asyncio
async def test_contact_sales_feedback_aggregation(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Beta Corp", currency="USD")
        session.add(lead)
        await session.flush()

        act = SalesActivity(
            lead_id=lead.id,
            activity_type=SalesActivityType.CALL,
            note="Lost to competitor",
            outcome=SalesActivityOutcome.LOST,
        )
        session.add(act)
        await session.flush()

        task = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Closing Call",
            status=SalesTaskStatus.COMPLETED,
            completed_at=datetime.now(timezone.utc),
            due_at=datetime.now(timezone.utc),
            related_activity_id=act.id,
        )
        session.add(task)
        await session.commit()

        summary = await get_contact_sales_feedback(session, lead.id)
        assert summary is not None
        assert summary.scope == "contact"
        assert summary.scope_id == str(lead.id)
        assert summary.total_executions == 1
        assert summary.negative_count == 1
        assert summary.negative_rate == 100.0
        assert summary.positive_count == 0
        assert summary.outcome_distribution.get("LOST") == 1


@pytest.mark.asyncio
async def test_global_recommendations_feedback_filtering(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Gamma LLC", currency="USD")
        session.add(lead)
        await session.flush()

        act1 = SalesActivity(lead_id=lead.id, activity_type=SalesActivityType.CALL, note="Won deal", outcome=SalesActivityOutcome.WON)
        act2 = SalesActivity(lead_id=lead.id, activity_type=SalesActivityType.EMAIL, note="Invalid email", outcome=SalesActivityOutcome.INVALID_CONTACT)
        session.add_all([act1, act2])
        await session.flush()

        task1 = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Won task",
            status=SalesTaskStatus.COMPLETED,
            completed_at=datetime.now(timezone.utc),
            due_at=datetime.now(timezone.utc),
            agent_source_action_id=f"contact_recommendation:{lead.id}:DISCOVERY_CALL",
            related_activity_id=act1.id,
        )
        task2 = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.EMAIL,
            title="Invalid task",
            status=SalesTaskStatus.COMPLETED,
            completed_at=datetime.now(timezone.utc),
            due_at=datetime.now(timezone.utc),
            agent_source_action_id=f"contact_recommendation:{lead.id}:PERSONALIZED_EMAIL",
            related_activity_id=act2.id,
        )

        session.add_all([task1, task2])
        await session.commit()

        # Unfiltered global
        all_summary = await get_recommendations_sales_feedback(session)
        assert all_summary.total_executions >= 2

        # Filter by action DISCOVERY_CALL
        call_summary = await get_recommendations_sales_feedback(session, action="DISCOVERY_CALL")
        assert call_summary.total_executions == 1
        assert call_summary.positive_count == 1

        # Filter by sentiment NEGATIVE
        neg_summary = await get_recommendations_sales_feedback(session, sentiment="NEGATIVE")
        assert neg_summary.total_executions == 1
        assert neg_summary.negative_count == 1
        assert neg_summary.items[0].outcome == "INVALID_CONTACT"


@pytest.mark.asyncio
async def test_nonexistent_opportunity_and_contact_returns_none(session_factory):
    async with session_factory() as session:
        fake_id = uuid.uuid4()
        opp_res = await get_opportunity_sales_feedback(session, fake_id)
        assert opp_res is None

        contact_res = await get_contact_sales_feedback(session, fake_id)
        assert contact_res is None


# ----------------------------------------------------
# 3. HTTP API Route Tests
# ----------------------------------------------------
@pytest.mark.asyncio
async def test_api_get_opportunity_feedback(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    async with session_factory() as session:
        lead = Lead(company_name="API Opp Lead", currency="USD")
        session.add(lead)
        await session.flush()

        opp = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="SEO Audit Opp",
            opportunity_type="SEO",
            score=90,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add(opp)
        await session.flush()

        act = SalesActivity(
            lead_id=lead.id,
            activity_type=SalesActivityType.CALL,
            note="Client agreed to proposal",
            outcome=SalesActivityOutcome.PROPOSAL_REQUESTED,
        )
        session.add(act)
        await session.flush()

        task = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Discovery Call",
            status=SalesTaskStatus.COMPLETED,
            completed_at=datetime.now(timezone.utc),
            due_at=datetime.now(timezone.utc),
            agent_source_type="SALES_OPPORTUNITY",
            agent_source_id=opp.id,
            agent_source_action_id=f"contact_recommendation:{lead.id}:DISCOVERY_CALL",
            related_activity_id=act.id,
        )
        session.add(task)
        await session.commit()
        opp_id = str(opp.id)

    app.dependency_overrides[get_db] = lambda: session_factory()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(f"/api/sales-feedback/opportunities/{opp_id}", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            data = resp.json()
            assert data["scope"] == "opportunity"
            assert data["scope_id"] == opp_id
            assert data["total_executions"] == 1
            assert data["positive_count"] == 1
            assert data["positive_rate"] == 100.0
            assert len(data["items"]) == 1
            assert data["items"][0]["outcome"] == "PROPOSAL_REQUESTED"
            assert data["items"][0]["sentiment"] == "POSITIVE"
            assert data["items"][0]["attribution_level"] == "FULL"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_get_opportunity_feedback_not_found(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    app.dependency_overrides[get_db] = lambda: session_factory()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            fake_id = str(uuid.uuid4())
            resp = await ac.get(f"/api/sales-feedback/opportunities/{fake_id}", headers=AUTH_HEADERS)
            assert resp.status_code == 404
            data = resp.json()
            assert data["error"]["message"] == "Sales opportunity not found"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_get_contact_feedback(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    async with session_factory() as session:
        lead = Lead(company_name="API Contact Lead", currency="USD")
        session.add(lead)
        await session.flush()

        act = SalesActivity(
            lead_id=lead.id,
            activity_type=SalesActivityType.CALL,
            note="Client interested",
            outcome=SalesActivityOutcome.INTERESTED,
        )
        session.add(act)
        await session.flush()

        task = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Contact call",
            status=SalesTaskStatus.COMPLETED,
            completed_at=datetime.now(timezone.utc),
            due_at=datetime.now(timezone.utc),
            related_activity_id=act.id,
        )
        session.add(task)
        await session.commit()
        lead_id = str(lead.id)

    app.dependency_overrides[get_db] = lambda: session_factory()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(f"/api/sales-feedback/contacts/{lead_id}", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            data = resp.json()
            assert data["scope"] == "contact"
            assert data["scope_id"] == lead_id
            assert data["total_executions"] == 1
            assert data["positive_count"] == 1
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_get_recommendations_feedback(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    async with session_factory() as session:
        lead = Lead(company_name="Global Rec Lead", currency="USD")
        session.add(lead)
        await session.flush()

        act = SalesActivity(
            lead_id=lead.id,
            activity_type=SalesActivityType.CALL,
            note="Meeting booked successfully",
            outcome=SalesActivityOutcome.MEETING_BOOKED,
        )
        session.add(act)
        await session.flush()

        task = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Meeting Booking",
            status=SalesTaskStatus.COMPLETED,
            completed_at=datetime.now(timezone.utc),
            due_at=datetime.now(timezone.utc),
            agent_source_action_id=f"contact_recommendation:{lead.id}:DISCOVERY_CALL",
            related_activity_id=act.id,
        )
        session.add(task)
        await session.commit()

    app.dependency_overrides[get_db] = lambda: session_factory()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/sales-feedback/recommendations?action=DISCOVERY_CALL", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            data = resp.json()
            assert data["scope"] == "recommendations"
            assert data["total_executions"] >= 1
            assert data["positive_count"] >= 1
            assert "DISCOVERY_CALL" in data["action_performance"]
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_get_contact_feedback_not_found(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    app.dependency_overrides[get_db] = lambda: session_factory()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            fake_id = str(uuid.uuid4())
            resp = await ac.get(f"/api/sales-feedback/contacts/{fake_id}", headers=AUTH_HEADERS)
            assert resp.status_code == 404
            data = resp.json()
            assert data["error"]["message"] == "Contact not found"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_read_only_invariants(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Invariant Corp", currency="USD", lead_score=88)
        session.add(lead)
        await session.flush()

        opp = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="SEO Opportunity",
            opportunity_type="SEO",
            score=95,
            priority="CRITICAL",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add(opp)
        await session.flush()

        act = SalesActivity(
            lead_id=lead.id,
            activity_type=SalesActivityType.CALL,
            note="Call done",
            outcome=SalesActivityOutcome.CONNECTED,
        )
        session.add(act)
        await session.flush()

        task = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Discovery Call",
            status=SalesTaskStatus.COMPLETED,
            completed_at=datetime.now(timezone.utc),
            due_at=datetime.now(timezone.utc),
            agent_source_type="SALES_OPPORTUNITY",
            agent_source_id=opp.id,
            agent_source_action_id=f"contact_recommendation:{lead.id}:DISCOVERY_CALL",
            related_activity_id=act.id,
        )
        session.add(task)
        await session.commit()

        # Query feedback multiple times
        res1 = await get_opportunity_sales_feedback(session, opp.id)
        res2 = await get_contact_sales_feedback(session, lead.id)
        res3 = await get_recommendations_sales_feedback(session)

        assert res1 is not None
        assert res2 is not None
        assert res3 is not None

        # Verify no mutations to Lead or Opportunity scores
        await session.refresh(lead)
        await session.refresh(opp)
        assert lead.lead_score == 88
        assert opp.score == 95
        assert opp.priority == "CRITICAL"
        assert opp.recommended_action == "DISCOVERY_CALL"


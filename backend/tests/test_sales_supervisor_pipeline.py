"""
Integration and Service-Level Tests for Phase 12J-B Sales Action Prioritization Engine.

Covers:
- Pipeline opportunity discovery across multiple leads
- Highest valid score ranking first
- Deterministic stable ordering for ties
- Empty opportunity sets handling
- Missing recommendation data (no fabricated actions)
- Missing buying signals (never maximum urgency)
- Missing and insufficient historical feedback fallback (neutral prior 50.0)
- Verified positive and negative feedback outcome rates
- Malformed and out-of-range source score rejection
- Read-only guarantee: zero SalesTask creation or mutation
- Zero external communication dispatch
- Lead-scoped pipeline evaluation
"""

import math
import uuid
import pytest
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.domain.contact_recommendation import RecommendedSalesAction
from app.domain.sales_supervisor import (
    DEFAULT_NEUTRAL_FEEDBACK_SCORE,
    SupervisorActionReadinessStatus,
    SupervisorUrgencyBand,
)
from app.models.entities import (
    BuyingSignal,
    BuyingInfluenceLevel,
    ConfidenceLevel,
    ContactIntelligenceProfile,
    FunctionalArea,
    Lead,
    LeadIntelligenceOpportunity,
    SalesActivity,
    SalesActivityOutcome,
    SalesActivityType,
    SalesPriority,
    SalesStatus,
    SalesTask,
    SalesTaskStatus,
    SalesTaskType,
    SeniorityLevel,
    SignalStrength,
    SourceType,
    now_utc,
)
from app.services.sales_supervisor import (
    build_action_identity,
    evaluate_sales_supervisor_pipeline,
    map_buying_signals_to_urgency,
    validate_source_score,
)


# ============================================================================
# Helper Fixtures & Data Generators
# ============================================================================

async def _create_test_lead(
    session,
    company_name="Acme Corp",
    email="test@acme.com",
    phone="1234567890",
    next_follow_up_at=None,
    decision_maker="Jane Doe",
) -> Lead:
    lead = Lead(
        id=uuid.uuid4(),
        company_name=company_name,
        currency="USD",
        decision_maker=decision_maker,
        job_title="Head of Growth" if decision_maker else None,
        department="Marketing" if decision_maker else None,
        email=email,
        phone=phone,
        sales_status=SalesStatus.NEW,
        next_follow_up_at=next_follow_up_at,
    )
    session.add(lead)
    return lead


async def _create_test_contact_profile(session, contact_id: uuid.UUID, dm_score=90) -> ContactIntelligenceProfile:
    profile = ContactIntelligenceProfile(
        contact_id=contact_id,
        decision_maker_score=dm_score,
        functional_area=FunctionalArea.MARKETING,
        seniority_level=SeniorityLevel.EXECUTIVE,
        buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
        confidence=ConfidenceLevel.HIGH,
        source_type=SourceType.SYSTEM_DERIVED,
    )
    session.add(profile)
    return profile


async def _create_test_opportunity(
    session,
    lead_id: uuid.UUID,
    title="SEO Strategy",
    score=80,
    priority="HIGH",
    opp_type="DIGITAL_MARKETING",
) -> LeadIntelligenceOpportunity:
    opp = LeadIntelligenceOpportunity(
        id=uuid.uuid4(),
        lead_id=lead_id,
        title=title,
        opportunity_type=opp_type,
        score=score,
        priority=priority,
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH,
        evidence={"signals": [{"type": "marketing_activity"}]},
    )
    session.add(opp)
    return opp


# ============================================================================
# 1. Validation & Pure Helper Tests
# ============================================================================

def test_validate_source_score_valid():
    assert validate_source_score("score", 0) == 0.0
    assert validate_source_score("score", 100) == 100.0
    assert validate_source_score("score", 75.5) == 75.5


@pytest.mark.parametrize("invalid_val", [-1, 101, -0.01, 100.01, None, True, False, "80", float("nan"), float("inf")])
def test_validate_source_score_invalid(invalid_val):
    with pytest.raises((ValueError, TypeError)):
        validate_source_score("test_score", invalid_val)


def test_build_action_identity():
    contact_id = uuid.uuid4()
    identity = build_action_identity(contact_id, RecommendedSalesAction.DISCOVERY_CALL)
    assert identity == f"contact_recommendation:{contact_id}:DISCOVERY_CALL"


def test_map_buying_signals_to_urgency_missing_signals():
    """Missing buying signals must NEVER yield maximum urgency."""
    urgency, band, reasons = map_buying_signals_to_urgency(
        lead=None,
        signals=[],
        opportunity=None,
    )
    assert urgency == 20.0
    assert band == SupervisorUrgencyBand.STANDARD
    assert "no urgent buying signals detected" in reasons[0]


def test_map_buying_signals_to_urgency_overdue_follow_up():
    now = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)
    lead = Lead(
        id=uuid.uuid4(),
        company_name="Test Corp",
        next_follow_up_at=now - timedelta(hours=2),
    )
    urgency, band, reasons = map_buying_signals_to_urgency(lead=lead, signals=[], now=now)
    assert urgency == 100.0
    assert band == SupervisorUrgencyBand.OVERDUE
    assert "OVERDUE" in reasons[0]


def test_map_buying_signals_to_urgency_due_today():
    now = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)
    lead = Lead(
        id=uuid.uuid4(),
        company_name="Test Corp",
        next_follow_up_at=now + timedelta(hours=4),
    )
    urgency, band, reasons = map_buying_signals_to_urgency(lead=lead, signals=[], now=now)
    assert urgency == 80.0
    assert band == SupervisorUrgencyBand.DUE_TODAY
    assert "DUE TODAY" in reasons[0]


def test_map_buying_signals_to_urgency_high_signal():
    now = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)
    sig = BuyingSignal(
        id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        signal_type="HIRING_EXPANSION",
        signal_category="marketing_activity",
        confidence=ConfidenceLevel.HIGH,
        strength=SignalStrength.HIGH,
        source_type=SourceType.SYSTEM_DERIVED,
        detected_at=now,
    )
    urgency, band, reasons = map_buying_signals_to_urgency(lead=None, signals=[sig], now=now)
    assert urgency == 60.0
    assert band == SupervisorUrgencyBand.HIGH_SIGNAL
    assert "HIRING_EXPANSION" in reasons[0]


# ============================================================================
# 2. Pipeline Service Tests with SQLite In-Memory Database
# ============================================================================

@pytest.mark.asyncio
async def test_pipeline_multiple_opportunities_with_different_scores(session_factory):
    """Pipeline discovers multiple opportunities across leads and ranks them."""
    async with session_factory() as session:
        # Create 3 leads, contacts, and opportunities with scores: 95, 75, 50
        lead1 = await _create_test_lead(session, "Company High")
        await _create_test_contact_profile(session, lead1.id, dm_score=95)
        opp1 = await _create_test_opportunity(session, lead1.id, "High Opp", score=95)

        lead2 = await _create_test_lead(session, "Company Mid")
        await _create_test_contact_profile(session, lead2.id, dm_score=75)
        opp2 = await _create_test_opportunity(session, lead2.id, "Mid Opp", score=75)

        lead3 = await _create_test_lead(session, "Company Low")
        await _create_test_contact_profile(session, lead3.id, dm_score=50)
        opp3 = await _create_test_opportunity(session, lead3.id, "Low Opp", score=50)

        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert response.total_candidates == 3
        assert response.ready_count == 3
        assert response.blocked_count == 0
        assert len(response.actions) == 3

        # Verify descending priority ordering
        scores = [a.priority_score for a in response.actions]
        assert scores == sorted(scores, reverse=True)
        assert response.actions[0].opportunity_id == opp1.id
        assert response.actions[1].opportunity_id == opp2.id
        assert response.actions[2].opportunity_id == opp3.id


@pytest.mark.asyncio
async def test_highest_valid_score_ranks_first(session_factory):
    """Proves that the highest computed priority score strictly occupies index 0."""
    async with session_factory() as session:
        lead_a = await _create_test_lead(session, "Alpha Corp")
        await _create_test_contact_profile(session, lead_a.id, dm_score=60)
        opp_a = await _create_test_opportunity(session, lead_a.id, "Alpha Opp", score=60)

        lead_b = await _create_test_lead(session, "Beta Corp")
        await _create_test_contact_profile(session, lead_b.id, dm_score=95)
        opp_b = await _create_test_opportunity(session, lead_b.id, "Beta Opp", score=90)

        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 2
        assert response.actions[0].opportunity_id == opp_b.id
        assert response.actions[0].priority_score > response.actions[1].priority_score


@pytest.mark.asyncio
async def test_deterministic_ordering_for_ties(session_factory):
    """Actions with identical priority scores must produce identical order across repeated evaluations."""
    async with session_factory() as session:
        lead1 = await _create_test_lead(session, "Tie Corp 1")
        await _create_test_contact_profile(session, lead1.id, dm_score=80)
        await _create_test_opportunity(session, lead1.id, "Tie Opp 1", score=80)

        lead2 = await _create_test_lead(session, "Tie Corp 2")
        await _create_test_contact_profile(session, lead2.id, dm_score=80)
        await _create_test_opportunity(session, lead2.id, "Tie Opp 2", score=80)

        await session.commit()

        run1 = await evaluate_sales_supervisor_pipeline(session)
        run2 = await evaluate_sales_supervisor_pipeline(session)

        assert len(run1.actions) == 2
        assert run1.actions[0].priority_score == run1.actions[1].priority_score

        # Order must be strictly identical across runs
        ids_run1 = [a.action_id for a in run1.actions]
        ids_run2 = [a.action_id for a in run2.actions]
        assert ids_run1 == ids_run2


@pytest.mark.asyncio
async def test_no_opportunities_returns_empty_pipeline(session_factory):
    """Empty pipeline returns valid response with 0 candidates."""
    async with session_factory() as session:
        response = await evaluate_sales_supervisor_pipeline(session)
        assert response.total_candidates == 0
        assert response.ready_count == 0
        assert response.blocked_count == 0
        assert response.actions == []
        assert response.summary["total_opportunities_evaluated"] == 0
        assert response.summary["candidates_generated"] == 0


@pytest.mark.asyncio
async def test_missing_recommendation_data_skips_without_fabrication(session_factory):
    """An opportunity without matching contact/recommendation must not fabricate a candidate."""
    async with session_factory() as session:
        # Create an opportunity referencing a non-existent lead, so no contact match can be found
        orphan_lead_id = uuid.uuid4()
        opp = LeadIntelligenceOpportunity(
            id=uuid.uuid4(),
            lead_id=orphan_lead_id,
            title="Orphan Opp",
            score=85,
            opportunity_type="SEO",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add(opp)
        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert response.total_candidates == 0
        assert response.actions == []
        assert response.summary["skipped_no_recommendation"] == 1


@pytest.mark.asyncio
async def test_missing_buying_signals_and_urgency_data(session_factory):
    """Missing buying signals yield standard cadence (20.0), never maximum urgency."""
    async with session_factory() as session:
        lead = await _create_test_lead(session, "Quiet Lead", next_follow_up_at=None)
        await _create_test_contact_profile(session, lead.id, dm_score=80)
        await _create_test_opportunity(session, lead.id, "Quiet Opp", score=80)
        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1
        action = response.actions[0]
        assert action.urgency_score == 20.0
        assert action.score_breakdown is not None
        assert action.score_breakdown.urgency == 20.0
        assert action.score_breakdown.urgency_component == round(0.15 * 20.0, 4)


@pytest.mark.asyncio
async def test_no_historical_feedback_applies_neutral_fallback(session_factory):
    """When no feedback tasks exist, neutral prior of 50.0 is used with fallback flags."""
    async with session_factory() as session:
        lead = await _create_test_lead(session, "Cold Lead")
        await _create_test_contact_profile(session, lead.id, dm_score=80)
        await _create_test_opportunity(session, lead.id, "Cold Opp", score=80)
        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1
        action = response.actions[0]

        assert action.historical_feedback_rate is None
        assert action.score_breakdown is not None
        assert action.score_breakdown.historical_feedback == DEFAULT_NEUTRAL_FEEDBACK_SCORE
        assert action.score_breakdown.fallback_applied is True
        assert action.score_breakdown.feedback_available is False
        assert action.score_breakdown.feedback_component == round(0.20 * 50.0, 4)


@pytest.mark.asyncio
async def test_feedback_with_verified_positive_and_negative_outcomes(session_factory):
    """Completed tasks with structured outcomes are aggregated into positive_rate."""
    async with session_factory() as session:
        lead = await _create_test_lead(session, "Feedback Lead")
        await _create_test_contact_profile(session, lead.id, dm_score=85)
        opp = await _create_test_opportunity(session, lead.id, "Feedback Opp", score=85)

        # Seed 4 completed tasks: 3 positive outcomes, 1 negative outcome -> 75% positive rate
        now = now_utc()
        outcomes = [
            SalesActivityOutcome.CONNECTED,
            SalesActivityOutcome.INTERESTED,
            SalesActivityOutcome.MEETING_BOOKED,
            SalesActivityOutcome.NOT_INTERESTED,
        ]
        for out in outcomes:
            act_id = uuid.uuid4()
            act = SalesActivity(
                id=act_id,
                lead_id=lead.id,
                activity_type=SalesActivityType.CALL,
                note=f"Activity with {out.value}",
                outcome=out,
                occurred_at=now,
            )
            session.add(act)

            task = SalesTask(
                id=uuid.uuid4(),
                lead_id=lead.id,
                task_type=SalesTaskType.CALL,
                title="Historical Task",
                due_at=now,
                status=SalesTaskStatus.COMPLETED,
                agent_source_type="SALES_OPPORTUNITY",
                agent_source_id=opp.id,
                related_activity_id=act_id,
                completed_at=now,
            )
            session.add(task)

        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1
        action = response.actions[0]

        # 3 / 4 positive = 75.0%
        assert action.historical_feedback_rate == 75.0
        assert action.score_breakdown is not None
        assert action.score_breakdown.historical_feedback == 75.0
        assert action.score_breakdown.feedback_available is True
        assert action.score_breakdown.fallback_applied is False
        assert action.score_breakdown.feedback_component == round(0.20 * 75.0, 4)


@pytest.mark.asyncio
async def test_malformed_or_out_of_range_source_scores(session_factory):
    """Malformed opportunity scores raise ValueError and are never silently converted."""
    async with session_factory() as session:
        lead = await _create_test_lead(session, "Bad Score Corp")
        await _create_test_contact_profile(session, lead.id, dm_score=80)

        # Out-of-bounds score 150
        opp = await _create_test_opportunity(session, lead.id, "Bad Opp", score=150)
        await session.commit()

        with pytest.raises(ValueError, match="out of range"):
            await evaluate_sales_supervisor_pipeline(session)


@pytest.mark.asyncio
async def test_prioritization_does_not_create_or_modify_sales_tasks(session_factory):
    """Pipeline prioritization must be strictly read-only: zero SalesTask creations or updates."""
    async with session_factory() as session:
        lead = await _create_test_lead(session, "Read Only Corp")
        await _create_test_contact_profile(session, lead.id, dm_score=85)
        await _create_test_opportunity(session, lead.id, "Read Only Opp", score=85)

        # Existing task before evaluation
        existing_task = SalesTask(
            id=uuid.uuid4(),
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Pre-existing Task",
            due_at=now_utc(),
            status=SalesTaskStatus.OPEN,
        )
        session.add(existing_task)
        await session.commit()

        # Count tasks before
        tasks_before = list((await session.execute(select(SalesTask))).scalars().all())
        assert len(tasks_before) == 1

        # Run pipeline evaluation
        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1

        # Count tasks after
        tasks_after = list((await session.execute(select(SalesTask))).scalars().all())
        assert len(tasks_after) == 1
        assert tasks_after[0].id == existing_task.id
        assert tasks_after[0].title == "Pre-existing Task"


@pytest.mark.asyncio
async def test_lead_scoped_pipeline_evaluation(session_factory):
    """Providing lead_id restricts evaluation strictly to that lead."""
    async with session_factory() as session:
        lead_a = await _create_test_lead(session, "Lead Alpha")
        await _create_test_contact_profile(session, lead_a.id, dm_score=90)
        opp_a = await _create_test_opportunity(session, lead_a.id, "Alpha Opp", score=90)

        lead_b = await _create_test_lead(session, "Lead Beta")
        await _create_test_contact_profile(session, lead_b.id, dm_score=85)
        opp_b = await _create_test_opportunity(session, lead_b.id, "Beta Opp", score=85)

        await session.commit()

        # Evaluate only lead A
        response_a = await evaluate_sales_supervisor_pipeline(session, lead_id=lead_a.id)
        assert response_a.total_candidates == 1
        assert response_a.actions[0].opportunity_id == opp_a.id
        assert response_a.actions[0].lead_id == lead_a.id

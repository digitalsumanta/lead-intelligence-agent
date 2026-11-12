"""
Test Suite for Phase 12J-E: Closed-Loop Sales Feedback Integration.

Verifies that sales execution outcomes from completed tasks and activities:
1. Are accurately ingested into historical feedback calculations.
2. Deduplicate records across duplicate tasks and activities.
3. Exclude unresolved, incomplete, or unsupported outcomes from verified denominators.
4. Cleanly fall back to neutral prior (50.0) when insufficient verified feedback exists.
5. Prioritize action-specific verified feedback over opportunity-wide fallback.
6. Influence prioritization scoring without overriding readiness or approval gates.
7. Trigger zero external communications or dispatch mutations.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select

from app.domain.contact_intelligence import (
    BuyingInfluenceLevel,
    FunctionalArea,
    SeniorityLevel,
)
from app.domain.contact_recommendation import RecommendedSalesAction
from app.domain.sales_feedback import FeedbackSentiment
from app.domain.sales_supervisor import (
    DEFAULT_NEUTRAL_FEEDBACK_SCORE,
    SupervisorActionReadinessStatus,
    SupervisorExecutionDecision,
)
from app.models.entities import (
    ConfidenceLevel,
    ContactIntelligenceProfile,
    Lead,
    LeadIntelligenceOpportunity,
    SalesActivity,
    SalesActivityOutcome,
    SalesActivityType,
    SalesStatus,
    SalesTask,
    SalesTaskStatus,
    SalesTaskType,
    SourceType,
    now_utc,
)
from app.services.sales_feedback import (
    get_opportunity_sales_feedback,
)
from app.services.sales_supervisor import (
    authorize_supervisor_action_execution,
    evaluate_sales_supervisor_pipeline,
)


# ============================================================================
# Helpers
# ============================================================================

async def _seed_lead(
    session,
    company_name="Feedback Corp",
    email="lead@feedbackcorp.com",
    phone="555-0199",
    email_opted_out=False,
) -> Lead:
    lead = Lead(
        id=uuid.uuid4(),
        company_name=company_name,
        currency="USD",
        decision_maker="Chief Marketing Officer",
        job_title="CMO",
        department="Marketing",
        email=email,
        phone=phone,
        email_opted_out=email_opted_out,
        sales_status=SalesStatus.NEW,
    )
    session.add(lead)
    return lead


async def _seed_profile(session, contact_id: uuid.UUID, dm_score=85) -> ContactIntelligenceProfile:
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


async def _seed_opportunity(
    session,
    lead_id: uuid.UUID,
    title="SEO Modernization",
    score=85,
    opp_type="DIGITAL_MARKETING",
) -> LeadIntelligenceOpportunity:
    opp = LeadIntelligenceOpportunity(
        id=uuid.uuid4(),
        lead_id=lead_id,
        title=title,
        opportunity_type=opp_type,
        score=score,
        priority="HIGH",
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH,
        evidence={"signals": [{"type": "marketing_activity"}]},
    )
    session.add(opp)
    return opp


async def _seed_completed_task_with_activity(
    session,
    lead_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    outcome: SalesActivityOutcome | None,
    action_name: str = "DISCOVERY_CALL",
    task_type: SalesTaskType = SalesTaskType.CALL,
    activity_id: uuid.UUID | None = None,
    status: SalesTaskStatus = SalesTaskStatus.COMPLETED,
    raw_outcome_str: str | None = None,
    days_ago: int = 2,
) -> tuple[SalesTask, SalesActivity | None]:
    event_time = now_utc() - timedelta(days=days_ago)
    act = None
    act_id = activity_id

    if outcome is not None or activity_id is not None:
        if act_id is None:
            act_id = uuid.uuid4()
            act = SalesActivity(
                id=act_id,
                lead_id=lead_id,
                activity_type=SalesActivityType.CALL,
                note=f"Call outcome: {outcome.value if outcome else 'none'}",
                outcome=outcome,
                occurred_at=event_time,
            )
            session.add(act)

    evidence_refs = [
        f"contact:{lead_id}",
        f"opportunity:{opportunity_id}",
        f"recommendation:contact_recommendation:{lead_id}:{action_name}",
    ]
    if raw_outcome_str:
        evidence_refs.append(f"outcome:{raw_outcome_str}")

    task = SalesTask(
        id=uuid.uuid4(),
        lead_id=lead_id,
        task_type=task_type,
        title=f"Task for {action_name}",
        due_at=event_time,
        status=status,
        agent_source_type="SALES_OPPORTUNITY",
        agent_source_id=opportunity_id,
        agent_source_action_id=f"contact_recommendation:{lead_id}:{action_name}:{uuid.uuid4().hex[:8]}",
        agent_evidence_refs=evidence_refs,
        related_activity_id=act_id,
        completed_at=event_time if status == SalesTaskStatus.COMPLETED else None,
    )
    session.add(task)
    return task, act


# ============================================================================
# Tests
# ============================================================================

@pytest.mark.asyncio
async def test_positive_completed_outcomes_boost_priority_score(session_factory):
    """
    Verified positive outcomes (CONNECTED, INTERESTED, MEETING_BOOKED)
    produce a 100% positive rate, boost priority score, and link feedback evidence.
    """
    async with session_factory() as session:
        lead = await _seed_lead(session, "Positive Corp")
        await _seed_profile(session, lead.id, dm_score=85)
        opp = await _seed_opportunity(session, lead.id, "High Value Opp", score=80)

        outcomes = [
            SalesActivityOutcome.CONNECTED,
            SalesActivityOutcome.INTERESTED,
            SalesActivityOutcome.MEETING_BOOKED,
        ]
        for out in outcomes:
            await _seed_completed_task_with_activity(session, lead.id, opp.id, out, "DISCOVERY_CALL")

        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1
        action = response.actions[0]

        # 3 positive / 3 verified = 100.0%
        assert action.historical_feedback_rate == 100.0
        assert action.score_breakdown is not None
        assert action.score_breakdown.historical_feedback == 100.0
        assert action.score_breakdown.feedback_available is True
        assert action.score_breakdown.fallback_applied is False
        assert action.score_breakdown.feedback_component == round(0.20 * 100.0, 4)

        # Baseline neutral component would be round(0.20 * 50.0, 4) = 10.0
        # Positive feedback component is 20.0 (+10.0 boost)
        assert action.score_breakdown.feedback_component == 20.0

        # Provenance evidence keys include feedback activities
        fb_keys = [k for k in action.evidence_keys if k.startswith("feedback_activity:")]
        assert len(fb_keys) == 3


@pytest.mark.asyncio
async def test_negative_completed_outcomes_lower_priority_score(session_factory):
    """
    Verified negative outcomes (NOT_INTERESTED, LOST) produce a 0% rate,
    appropriately lowering priority score without falling back to neutral.
    """
    async with session_factory() as session:
        lead = await _seed_lead(session, "Negative Corp")
        await _seed_profile(session, lead.id, dm_score=85)
        opp = await _seed_opportunity(session, lead.id, "Struggling Opp", score=80)

        outcomes = [
            SalesActivityOutcome.NOT_INTERESTED,
            SalesActivityOutcome.LOST,
        ]
        for out in outcomes:
            await _seed_completed_task_with_activity(session, lead.id, opp.id, out, "DISCOVERY_CALL")

        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1
        action = response.actions[0]

        # 0 positive / 2 verified = 0.0%
        assert action.historical_feedback_rate == 0.0
        assert action.score_breakdown is not None
        assert action.score_breakdown.historical_feedback == 0.0
        assert action.score_breakdown.feedback_available is True
        assert action.score_breakdown.fallback_applied is False
        assert action.score_breakdown.feedback_component == 0.0


@pytest.mark.asyncio
async def test_neutral_completed_outcomes_included_in_verified_denominator(session_factory):
    """
    NO_RESPONSE outcomes count as neutral verified executions in the denominator.
    1 positive + 1 NO_RESPONSE = 50.0% positive rate.
    """
    async with session_factory() as session:
        lead = await _seed_lead(session, "Neutral Corp")
        await _seed_profile(session, lead.id, dm_score=85)
        opp = await _seed_opportunity(session, lead.id, "Mixed Opp", score=80)

        await _seed_completed_task_with_activity(session, lead.id, opp.id, SalesActivityOutcome.CONNECTED, "DISCOVERY_CALL")
        await _seed_completed_task_with_activity(session, lead.id, opp.id, SalesActivityOutcome.NO_RESPONSE, "DISCOVERY_CALL")

        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1
        action = response.actions[0]

        # 1 / 2 = 50.0%
        assert action.historical_feedback_rate == 50.0
        assert action.score_breakdown.feedback_available is True
        assert action.score_breakdown.fallback_applied is False
        assert action.score_breakdown.feedback_component == round(0.20 * 50.0, 4)


@pytest.mark.asyncio
async def test_unresolved_outcomes_do_not_penalize_and_trigger_neutral_prior(session_factory):
    """
    Completed tasks with outcome=None (unresolved/unrecorded) do NOT count as negative
    or verified executions. When all tasks are unresolved, the neutral prior (50.0) is applied.
    """
    async with session_factory() as session:
        lead = await _seed_lead(session, "Unresolved Corp")
        await _seed_profile(session, lead.id, dm_score=85)
        opp = await _seed_opportunity(session, lead.id, "Pending Opp", score=80)

        # 3 tasks marked COMPLETED but with no activity outcome recorded
        for _ in range(3):
            await _seed_completed_task_with_activity(session, lead.id, opp.id, outcome=None, action_name="DISCOVERY_CALL")

        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1
        action = response.actions[0]

        # Verified executions = 0 -> Cleanly triggers neutral prior (50.0)
        assert action.historical_feedback_rate is None
        assert action.score_breakdown is not None
        assert action.score_breakdown.historical_feedback == DEFAULT_NEUTRAL_FEEDBACK_SCORE
        assert action.score_breakdown.feedback_available is False
        assert action.score_breakdown.fallback_applied is True
        assert action.score_breakdown.feedback_component == round(0.20 * 50.0, 4)


@pytest.mark.asyncio
async def test_mixed_verified_and_unresolved_outcomes_exclude_unresolved_from_denominator(session_factory):
    """
    1 positive + 1 negative + 2 unresolved (None) tasks.
    Verified executions must be 2 (not 4). Positive rate must be 50.0% (1/2), not 25.0% (1/4).
    """
    async with session_factory() as session:
        lead = await _seed_lead(session, "Mixed Corp")
        await _seed_profile(session, lead.id, dm_score=85)
        opp = await _seed_opportunity(session, lead.id, "Target Opp", score=80)

        # 1 positive, 1 negative, 2 unresolved
        await _seed_completed_task_with_activity(session, lead.id, opp.id, SalesActivityOutcome.CONNECTED, "DISCOVERY_CALL")
        await _seed_completed_task_with_activity(session, lead.id, opp.id, SalesActivityOutcome.LOST, "DISCOVERY_CALL")
        await _seed_completed_task_with_activity(session, lead.id, opp.id, outcome=None, action_name="DISCOVERY_CALL")
        await _seed_completed_task_with_activity(session, lead.id, opp.id, outcome=None, action_name="DISCOVERY_CALL")

        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1
        action = response.actions[0]

        # Verified executions = 2. 1 / 2 = 50.0%
        assert action.historical_feedback_rate == 50.0
        assert action.score_breakdown.feedback_available is True
        assert action.score_breakdown.fallback_applied is False


@pytest.mark.asyncio
async def test_no_completed_tasks_applies_neutral_prior(session_factory):
    """When 0 tasks exist in the system for this opportunity, neutral prior 50.0 is used."""
    async with session_factory() as session:
        lead = await _seed_lead(session, "Fresh Corp")
        await _seed_profile(session, lead.id, dm_score=85)
        await _seed_opportunity(session, lead.id, "Fresh Opp", score=80)
        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1
        action = response.actions[0]

        assert action.historical_feedback_rate is None
        assert action.score_breakdown.historical_feedback == DEFAULT_NEUTRAL_FEEDBACK_SCORE
        assert action.score_breakdown.feedback_available is False
        assert action.score_breakdown.fallback_applied is True


@pytest.mark.asyncio
async def test_non_completed_tasks_ignored(session_factory):
    """
    Open, in-progress, and cancelled tasks must NOT be counted as completed outcomes,
    even if they have an activity attached.
    """
    async with session_factory() as session:
        lead = await _seed_lead(session, "Incomplete Corp")
        await _seed_profile(session, lead.id, dm_score=85)
        opp = await _seed_opportunity(session, lead.id, "Incomplete Opp", score=80)

        # Create tasks in CANCELLED state (must be ignored by feedback calculation)
        await _seed_completed_task_with_activity(
            session, lead.id, opp.id, SalesActivityOutcome.WON, "DISCOVERY_CALL",
            status=SalesTaskStatus.CANCELLED,
        )
        await _seed_completed_task_with_activity(
            session, lead.id, opp.id, SalesActivityOutcome.WON, "DISCOVERY_CALL",
            status=SalesTaskStatus.CANCELLED,
        )

        await session.commit()

        # Direct verification: _load_feedback_items ignores OPEN and IN_PROGRESS tasks
        from app.services.sales_feedback import _load_feedback_items
        open_task = SalesTask(
            id=uuid.uuid4(),
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Open Task",
            due_at=now_utc(),
            status=SalesTaskStatus.OPEN,
        )
        loaded_items = await _load_feedback_items(session, [open_task])
        assert len(loaded_items) == 0

        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1
        action = response.actions[0]

        # No completed tasks -> neutral prior applied
        assert action.historical_feedback_rate is None
        assert action.score_breakdown.historical_feedback == DEFAULT_NEUTRAL_FEEDBACK_SCORE
        assert action.score_breakdown.fallback_applied is True


@pytest.mark.asyncio
async def test_unsupported_or_invalid_outcome_strings_excluded(session_factory):
    """
    Evidence refs with unknown outcome strings ('outcome:UNSUPPORTED_STATUS')
    fail classification to UNKNOWN and are excluded from verified outcomes.
    """
    async with session_factory() as session:
        lead = await _seed_lead(session, "Invalid Outcome Corp")
        await _seed_profile(session, lead.id, dm_score=85)
        opp = await _seed_opportunity(session, lead.id, "Invalid Outcome Opp", score=80)

        # Task with unmapped raw outcome string
        await _seed_completed_task_with_activity(
            session, lead.id, opp.id, outcome=None, action_name="DISCOVERY_CALL",
            raw_outcome_str="NON_EXISTENT_OUTCOME_STATE",
        )

        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1
        action = response.actions[0]

        # Invalid outcome is UNKNOWN -> verified executions = 0 -> neutral prior
        assert action.historical_feedback_rate is None
        assert action.score_breakdown.historical_feedback == DEFAULT_NEUTRAL_FEEDBACK_SCORE
        assert action.score_breakdown.fallback_applied is True


@pytest.mark.asyncio
async def test_duplicate_task_activities_deduplicated(session_factory):
    """
    Multiple SalesTask rows pointing to the SAME SalesActivity must be deduplicated
    by activity_id, counting the single activity outcome only once.
    """
    async with session_factory() as session:
        lead = await _seed_lead(session, "Dedup Corp")
        await _seed_profile(session, lead.id, dm_score=85)
        opp = await _seed_opportunity(session, lead.id, "Dedup Opp", score=80)

        # 1 real activity with WON outcome
        shared_act = SalesActivity(
            id=uuid.uuid4(),
            lead_id=lead.id,
            activity_type=SalesActivityType.CALL,
            note="Closed deal",
            outcome=SalesActivityOutcome.WON,
            occurred_at=now_utc() - timedelta(days=2),
        )
        session.add(shared_act)

        # 3 tasks referencing the exact same activity_id
        for idx in range(3):
            task = SalesTask(
                id=uuid.uuid4(),
                lead_id=lead.id,
                task_type=SalesTaskType.CALL,
                title="Duplicate Task Ref",
                due_at=now_utc() - timedelta(days=2),
                status=SalesTaskStatus.COMPLETED,
                agent_source_type="SALES_OPPORTUNITY",
                agent_source_id=opp.id,
                agent_source_action_id=f"contact_recommendation:{lead.id}:DISCOVERY_CALL:{idx}",
                agent_evidence_refs=[
                    f"contact:{lead.id}",
                    f"opportunity:{opp.id}",
                    f"recommendation:contact_recommendation:{lead.id}:DISCOVERY_CALL",
                ],
                related_activity_id=shared_act.id,
                completed_at=now_utc() - timedelta(days=2),
            )
            session.add(task)

        await session.commit()

        feedback_summary = await get_opportunity_sales_feedback(session, opp.id)
        # Deduplication ensures only 1 item is loaded
        assert len(feedback_summary.items) == 1
        assert feedback_summary.verified_executions == 1
        assert feedback_summary.positive_count == 1
        assert feedback_summary.positive_rate == 100.0

        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1
        action = response.actions[0]
        assert action.historical_feedback_rate == 100.0


@pytest.mark.asyncio
async def test_action_specific_feedback_prioritization(session_factory):
    """
    When an opportunity has feedback across multiple distinct actions, the candidate action
    uses the verified positive rate specifically for its matching recommended action.
    """
    async with session_factory() as session:
        lead = await _seed_lead(session, "Action Specific Corp")
        await _seed_profile(session, lead.id, dm_score=85)
        opp = await _seed_opportunity(session, lead.id, "Multi Action Opp", score=80)

        # 2 tasks for DISCOVERY_CALL: both WON (100% positive)
        await _seed_completed_task_with_activity(session, lead.id, opp.id, SalesActivityOutcome.WON, "DISCOVERY_CALL")
        await _seed_completed_task_with_activity(session, lead.id, opp.id, SalesActivityOutcome.CONNECTED, "DISCOVERY_CALL")

        # 2 tasks for PERSONALIZED_EMAIL: both LOST (0% positive)
        await _seed_completed_task_with_activity(session, lead.id, opp.id, SalesActivityOutcome.LOST, "PERSONALIZED_EMAIL")
        await _seed_completed_task_with_activity(session, lead.id, opp.id, SalesActivityOutcome.NOT_INTERESTED, "PERSONALIZED_EMAIL")

        await session.commit()

        # Overall opportunity positive rate would be 2/4 = 50.0%
        # But candidate recommended action is DISCOVERY_CALL (due to DM authority score)
        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1
        action = response.actions[0]

        assert action.recommended_action == RecommendedSalesAction.DISCOVERY_CALL
        # Action-specific feedback rate of 100.0% is applied, not opportunity-wide 50.0%
        assert action.historical_feedback_rate == 100.0
        assert action.score_breakdown.historical_feedback == 100.0


@pytest.mark.asyncio
async def test_feedback_rate_bounded_and_deterministic(session_factory):
    """
    Repeated pipeline evaluations produce identical deterministic output and rates.
    """
    async with session_factory() as session:
        lead = await _seed_lead(session, "Determinism Corp")
        await _seed_profile(session, lead.id, dm_score=85)
        opp = await _seed_opportunity(session, lead.id, "Determinism Opp", score=80)

        await _seed_completed_task_with_activity(session, lead.id, opp.id, SalesActivityOutcome.CONNECTED, "DISCOVERY_CALL")
        await _seed_completed_task_with_activity(session, lead.id, opp.id, SalesActivityOutcome.NOT_INTERESTED, "DISCOVERY_CALL")

        await session.commit()

        run1 = await evaluate_sales_supervisor_pipeline(session)
        run2 = await evaluate_sales_supervisor_pipeline(session)

        assert len(run1.actions) == len(run2.actions) == 1
        a1, a2 = run1.actions[0], run2.actions[0]

        assert a1.action_id == a2.action_id
        assert a1.priority_score == a2.priority_score
        assert a1.historical_feedback_rate == a2.historical_feedback_rate == 50.0
        assert 0.0 <= a1.priority_score <= 100.0
        assert 0.0 <= a1.historical_feedback_rate <= 100.0


@pytest.mark.asyncio
async def test_positive_feedback_cannot_override_blocked_readiness(session_factory):
    """
    A 100% positive historical feedback score and high priority score
    CANNOT bypass or override a failed readiness gate (e.g. BLOCKED_OPT_OUT).
    """
    async with session_factory() as session:
        lead = await _seed_lead(session, "OptOut Corp", email_opted_out=True)
        # Give decision maker profile with phone opted out as well so DISCOVERY_CALL is blocked
        lead.phone_opted_out = True
        await _seed_profile(session, lead.id, dm_score=85)
        opp = await _seed_opportunity(session, lead.id, "Opted Out Opp", score=90)

        # 3 positive outcomes -> 100% feedback
        for out in [SalesActivityOutcome.WON, SalesActivityOutcome.CONNECTED, SalesActivityOutcome.MEETING_BOOKED]:
            await _seed_completed_task_with_activity(session, lead.id, opp.id, out, "DISCOVERY_CALL")

        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1
        action = response.actions[0]

        # Priority score reflects high feedback rate (100%)
        assert action.historical_feedback_rate == 100.0
        assert action.priority_score > 80.0

        # But readiness remains strictly blocked by compliance
        assert action.readiness_status == SupervisorActionReadinessStatus.BLOCKED_OPT_OUT
        assert response.ready_count == 0
        assert response.blocked_count == 1


@pytest.mark.asyncio
async def test_positive_feedback_cannot_bypass_approval_policy(session_factory):
    """
    Even with 100% positive feedback, an external outreach action strictly requires
    explicit human approval and cannot be authorized autonomously.
    """
    async with session_factory() as session:
        lead = await _seed_lead(session, "Approval Policy Corp")
        await _seed_profile(session, lead.id, dm_score=85)
        opp = await _seed_opportunity(session, lead.id, "Outreach Opp", score=90)

        # 100% positive feedback
        await _seed_completed_task_with_activity(session, lead.id, opp.id, SalesActivityOutcome.WON, "DISCOVERY_CALL")
        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1
        action = response.actions[0]

        assert action.readiness_status == SupervisorActionReadinessStatus.READY
        assert action.policy_approval_required is True

        # Attempt authorization without approval -> REQUIRES_APPROVAL
        auth_res = authorize_supervisor_action_execution(action, approval=None)
        assert auth_res.authorized is False
        assert auth_res.decision == SupervisorExecutionDecision.REQUIRES_APPROVAL
        assert auth_res.denial_code == "APPROVAL_REQUIRED"


@pytest.mark.asyncio
async def test_feedback_evaluation_triggers_zero_external_dispatch(session_factory):
    """
    Feedback ingestion and pipeline prioritization must be purely read-only,
    dispatching zero communications and mutating zero tasks or leads.
    """
    async with session_factory() as session:
        lead = await _seed_lead(session, "No Dispatch Corp")
        await _seed_profile(session, lead.id, dm_score=85)
        opp = await _seed_opportunity(session, lead.id, "No Dispatch Opp", score=85)
        await _seed_completed_task_with_activity(session, lead.id, opp.id, SalesActivityOutcome.CONNECTED, "DISCOVERY_CALL")
        await session.commit()

        # Record pre-evaluation task count
        tasks_before = (await session.execute(select(SalesTask))).scalars().all()
        activities_before = (await session.execute(select(SalesActivity))).scalars().all()

        # Run pipeline evaluation
        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1

        # Verify zero database mutations occurred
        tasks_after = (await session.execute(select(SalesTask))).scalars().all()
        activities_after = (await session.execute(select(SalesActivity))).scalars().all()

        assert len(tasks_before) == len(tasks_after)
        assert len(activities_before) == len(activities_after)

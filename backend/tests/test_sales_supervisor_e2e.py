"""
End-to-End Validation and Verification Test Suite for Controlled Autonomous Sales Supervisor (Phase 12J-F).

Validates the integrated Sales Supervisor pipeline across:
- E2E-01: Baseline prioritization (ranking, math, bounds, tie-breaking, readiness independence)
- E2E-02: Positive action-specific feedback (action-targeted boost, no cross-action/opp leakage)
- E2E-03: Negative, neutral, and unresolved outcomes (proper denominators, neutral prior fallback)
- E2E-04: Duplicate provenance (deduplication of multiple tasks referencing same activity)
- E2E-05: Readiness cannot be overridden (all 5 gates hold against high scores & approvals)
- E2E-06: External action approval enforcement (lifecycle states, identity binding, expiry, fail-closed)
- E2E-07: Internal operation authorization (autonomous for ready, respects human veto, fails closed)
- E2E-08: Batch consistency (1:1 candidate mapping, zero cross-candidate bleed, deterministic order)
- E2E-09: No execution side effects (zero network calls, zero communications, read-only DB invariants)
- E2E-10: Regression integrity (Phase 12J models, legacy 10B models, weight integrity, schema invariants)
"""

from __future__ import annotations

import math
import socket
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.domain.contact_intelligence import (
    BuyingInfluenceLevel,
    FunctionalArea,
    SeniorityLevel,
)
from app.domain.contact_recommendation import (
    RecommendationPriority,
    RecommendedSalesAction,
)
from app.domain.sales_feedback import FeedbackSentiment
from app.domain.sales_supervisor import (
    DEFAULT_MIN_FEEDBACK_EXECUTIONS,
    DEFAULT_NEUTRAL_FEEDBACK_SCORE,
    MAX_SCORE,
    MIN_SCORE,
    WEIGHT_HISTORICAL_FEEDBACK,
    WEIGHT_OPPORTUNITY_SCORE,
    WEIGHT_RECOMMENDATION_FIT,
    WEIGHT_URGENCY,
    SupervisorActionReadinessStatus,
    SupervisorActionTier,
    SupervisorApprovalStatus,
    SupervisorExecutionDecision,
    SupervisorUrgencyBand,
    calculate_supervisor_action_priority,
    classify_supervisor_action,
    is_action_ready_for_execution,
    is_policy_approval_required,
    sort_prioritized_actions,
)
from app.models.entities import (
    AgentPolicyEvaluation,
    AiSalesSupervisorRun,
    AutonomousTaskExecution,
    BuyingSignal,
    ConfidenceLevel,
    ContactIntelligenceProfile,
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
    SignalStrength,
    SourceType,
    now_utc,
)
from app.schemas.sales_supervisor import (
    ActionReadinessDetails,
    PrioritizedSalesAction,
    SalesSupervisorPipelineResponse,
    SupervisorExecutionAuthorizationResult,
    SupervisorHumanApproval,
)
from app.services.sales_feedback import (
    get_opportunity_sales_feedback,
)
from app.services.sales_supervisor import (
    authorize_supervisor_action_execution,
    build_action_identity,
    evaluate_pipeline_execution_authorizations,
    evaluate_sales_supervisor_pipeline,
)


# ============================================================================
# Shared Helper Fixtures
# ============================================================================

async def _seed_lead(
    session,
    company_name: str = "Enterprise Corp",
    email: str | None = "contact@enterprise.com",
    phone: str | None = "555-0100",
    linkedin_url: str | None = "https://linkedin.com/in/enterprise",
    email_opted_out: bool = False,
    phone_opted_out: bool = False,
    whatsapp_opted_out: bool = False,
    next_follow_up_at: datetime | None = None,
    decision_maker: str = "VP Sales",
) -> Lead:
    lead = Lead(
        id=uuid.uuid4(),
        company_name=company_name,
        currency="USD",
        decision_maker=decision_maker,
        job_title=decision_maker,
        department="Sales",
        email=email,
        phone=phone,
        linkedin_url=linkedin_url,
        email_opted_out=email_opted_out,
        phone_opted_out=phone_opted_out,
        whatsapp_opted_out=whatsapp_opted_out,
        sales_status=SalesStatus.NEW,
        next_follow_up_at=next_follow_up_at,
    )
    session.add(lead)
    return lead


async def _seed_profile(
    session,
    contact_id: uuid.UUID,
    dm_score: int = 85,
    influence: BuyingInfluenceLevel = BuyingInfluenceLevel.DECISION_MAKER,
) -> ContactIntelligenceProfile:
    profile = ContactIntelligenceProfile(
        contact_id=contact_id,
        decision_maker_score=dm_score,
        functional_area=FunctionalArea.SALES,
        seniority_level=SeniorityLevel.EXECUTIVE,
        buying_influence=influence,
        confidence=ConfidenceLevel.HIGH,
        source_type=SourceType.SYSTEM_DERIVED,
    )
    session.add(profile)
    return profile


async def _seed_opportunity(
    session,
    lead_id: uuid.UUID,
    title: str = "Enterprise CRM Modernization",
    score: int = 85,
    opp_type: str = "DIGITAL_MARKETING",
    evidence: dict | list | None = None,
    source_reference: str | None = None,
) -> LeadIntelligenceOpportunity:
    if evidence is None and source_reference is None:
        evidence = {"signals": [{"type": "expansion", "detail": "Budget allocated"}]}

    opp = LeadIntelligenceOpportunity(
        id=uuid.uuid4(),
        lead_id=lead_id,
        title=title,
        opportunity_type=opp_type,
        score=score,
        priority="HIGH",
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH,
        evidence=evidence,
        source_reference=source_reference,
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
                activity_type=SalesActivityType.CALL if task_type == SalesTaskType.CALL else SalesActivityType.EMAIL,
                note=f"Historical outreach outcome: {outcome.value if outcome else 'none'}",
                outcome=outcome,
                occurred_at=event_time,
            )
            session.add(act)

    evidence_refs = [
        f"contact:{lead_id}",
        f"opportunity:{opportunity_id}",
        f"recommendation:contact_recommendation:{lead_id}:{action_name}",
    ]

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


def _create_matching_approval(
    action: PrioritizedSalesAction,
    approver_id: str = "supervisor@enterprise.com",
    status: SupervisorApprovalStatus = SupervisorApprovalStatus.APPROVED,
    expires_at: datetime | None = None,
    channel: str | None = None,
) -> SupervisorHumanApproval:
    if channel is None:
        act_val = action.recommended_action.value if hasattr(action.recommended_action, "value") else str(action.recommended_action)
        if act_val == "PERSONALIZED_EMAIL":
            resolved_channel = "EMAIL"
        elif act_val == "DISCOVERY_CALL":
            resolved_channel = "PHONE"
        elif act_val == "DIRECT_OUTREACH":
            resolved_channel = "DIRECT"
        else:
            resolved_channel = None
    else:
        resolved_channel = channel

    return SupervisorHumanApproval(
        action_id=action.action_id,
        lead_id=action.lead_id,
        opportunity_id=action.opportunity_id,
        contact_id=action.contact_id,
        channel=resolved_channel,
        approver_id=approver_id,
        status=status,
        expires_at=expires_at,
        notes="Verified human supervisor authorization",
    )


# ============================================================================
# E2E-01: Baseline Prioritization
# ============================================================================

@pytest.mark.asyncio
async def test_e2e_01_baseline_prioritization(session_factory):
    """
    E2E-01: Validates deterministic prioritization across multiple CRM opportunities:
    1. Highest valid score ranks first.
    2. Exact mathematical weighting: S = 0.35*opp + 0.30*rec + 0.20*fb + 0.15*urg.
    3. Scores are strictly bounded [0.0, 100.0].
    4. Stable tie-breaking via action_id sorting.
    5. Prioritization score is calculated independently of readiness status.
    """
    async with session_factory() as session:
        # Lead 1: High priority
        lead_a = await _seed_lead(session, "Alpha Inc", "alpha@example.com")
        await _seed_profile(session, lead_a.id, dm_score=95)
        await _seed_opportunity(session, lead_a.id, "Alpha Modernization", score=90)

        # Lead 2: Medium priority
        lead_b = await _seed_lead(session, "Beta Corp", "beta@example.com")
        await _seed_profile(session, lead_b.id, dm_score=75)
        await _seed_opportunity(session, lead_b.id, "Beta Migration", score=70)

        # Lead 3: Low priority
        lead_c = await _seed_lead(session, "Gamma LLC", "gamma@example.com")
        await _seed_profile(session, lead_c.id, dm_score=50)
        await _seed_opportunity(session, lead_c.id, "Gamma Review", score=45)

        # Lead 4: High score but opted out (to verify readiness independence)
        lead_opt = await _seed_lead(
            session, "OptedOut Corp", "opted@example.com", email_opted_out=True
        )
        await _seed_profile(session, lead_opt.id, dm_score=98)
        await _seed_opportunity(session, lead_opt.id, "Opted Out High Value", score=95)

        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)

        assert response.total_candidates == 4
        assert len(response.actions) == 4

        # 1. Verify descending order by priority_score
        scores = [a.priority_score for a in response.actions]
        assert scores == sorted(scores, reverse=True), "Actions must be sorted descending by priority_score"

        # 2. Verify score bounds and formula correctness for every candidate
        for action in response.actions:
            assert MIN_SCORE <= action.priority_score <= MAX_SCORE
            assert action.score_breakdown is not None
            bd = action.score_breakdown

            expected_opp = round(WEIGHT_OPPORTUNITY_SCORE * action.opportunity_score, 4)
            expected_rec = round(WEIGHT_RECOMMENDATION_FIT * action.recommendation_fit, 4)
            expected_fb = round(WEIGHT_HISTORICAL_FEEDBACK * bd.historical_feedback, 4)
            expected_urg = round(WEIGHT_URGENCY * action.urgency_score, 4)

            assert bd.opportunity_component == expected_opp
            assert bd.recommendation_component == expected_rec
            assert bd.feedback_component == expected_fb
            assert bd.urgency_component == expected_urg

            raw_sum = expected_opp + expected_rec + expected_fb + expected_urg
            expected_final = round(max(MIN_SCORE, min(MAX_SCORE, raw_sum)), 2)
            assert action.priority_score == expected_final

        # 3. Verify readiness independence: Opted-out candidate ranks #1 by score, but readiness is BLOCKED_OPT_OUT
        opt_action = next(a for a in response.actions if a.lead_id == lead_opt.id)
        assert opt_action == response.actions[0], "Highest scoring candidate should rank first regardless of readiness"
        assert opt_action.readiness_status == SupervisorActionReadinessStatus.BLOCKED_OPT_OUT
        assert opt_action.priority_score > 70.0

        # 4. Verify stable tie-breaking on identical scores
        tied_actions = sort_prioritized_actions(
            [
                response.actions[0].model_copy(update={"action_id": "z_action", "priority_score": 75.0}),
                response.actions[0].model_copy(update={"action_id": "a_action", "priority_score": 75.0}),
            ]
        )
        assert tied_actions[0].action_id == "a_action"
        assert tied_actions[1].action_id == "z_action"


# ============================================================================
# E2E-02: Positive Action-Specific Feedback
# ============================================================================

@pytest.mark.asyncio
async def test_e2e_02_positive_action_specific_feedback(session_factory):
    """
    E2E-02: Verified positive outcomes for a specific action boost its priority score
    via historical feedback weighting without leaking across actions or opportunities.
    """
    async with session_factory() as session:
        # Lead A: identical baseline score, with positive historical feedback for DISCOVERY_CALL
        lead_a = await _seed_lead(session, "Proven Corp", "proven@example.com")
        await _seed_profile(session, lead_a.id, dm_score=85)
        opp_a = await _seed_opportunity(session, lead_a.id, "Cloud Migration", score=85)

        # 3 positive completed tasks for DISCOVERY_CALL (2 days ago)
        outcomes = [
            SalesActivityOutcome.CONNECTED,
            SalesActivityOutcome.INTERESTED,
            SalesActivityOutcome.MEETING_BOOKED,
        ]
        for out in outcomes:
            await _seed_completed_task_with_activity(
                session, lead_a.id, opp_a.id, out, "DISCOVERY_CALL", days_ago=2
            )

        # Lead B: identical baseline score, but zero historical feedback
        lead_b = await _seed_lead(session, "Cold Corp", "cold@example.com")
        await _seed_profile(session, lead_b.id, dm_score=85)
        await _seed_opportunity(session, lead_b.id, "Cloud Migration", score=85)

        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 2

        action_a = next(a for a in response.actions if a.lead_id == lead_a.id)
        action_b = next(a for a in response.actions if a.lead_id == lead_b.id)

        # Action A: has 100% positive feedback rate
        assert action_a.historical_feedback_rate == 100.0
        assert action_a.score_breakdown.historical_feedback == 100.0
        assert action_a.score_breakdown.feedback_component == 20.0  # 0.20 * 100.0
        assert action_a.score_breakdown.feedback_available is True
        assert action_a.score_breakdown.fallback_applied is False
        assert any(k.startswith("feedback_activity:") for k in action_a.evidence_keys)

        # Action B: has neutral prior fallback (50.0)
        assert action_b.historical_feedback_rate is None
        assert action_b.score_breakdown.historical_feedback == DEFAULT_NEUTRAL_FEEDBACK_SCORE
        assert action_b.score_breakdown.feedback_component == 10.0  # 0.20 * 50.0
        assert action_b.score_breakdown.feedback_available is False
        assert action_b.score_breakdown.fallback_applied is True
        assert not any(k.startswith("feedback_activity:") for k in action_b.evidence_keys)

        # Priority difference is exactly 10.0 points (20.0 - 10.0)
        expected_diff = round(0.20 * (100.0 - 50.0), 2)
        actual_diff = round(action_a.priority_score - action_b.priority_score, 2)
        assert actual_diff == expected_diff

        # Rank #1 is Action A
        assert response.actions[0].action_id == action_a.action_id


# ============================================================================
# E2E-03: Negative, Neutral, and Unresolved Outcomes
# ============================================================================

@pytest.mark.asyncio
async def test_e2e_03_negative_neutral_and_unresolved_outcomes(session_factory):
    """
    E2E-03: Verifies that:
    1. Negative completed outcomes yield 0% positive rate, lowering priority.
    2. Neutral completed outcomes are included in verified denominator (producing 0% rate), distinct from missing feedback.
    3. Unresolved tasks (CANCELLED/no outcome) are excluded from the verified denominator.
    4. When verified executions == 0, neutral prior fallback (50.0) is triggered.
    """
    async with session_factory() as session:
        # Opp 1: Negative completed outcomes (NOT_INTERESTED, LOST, INVALID_CONTACT)
        lead_neg = await _seed_lead(session, "Negative Corp", "neg@example.com")
        await _seed_profile(session, lead_neg.id, dm_score=80)
        opp_neg = await _seed_opportunity(session, lead_neg.id, "Negative Opp", score=80)
        for out in [
            SalesActivityOutcome.NOT_INTERESTED,
            SalesActivityOutcome.LOST,
            SalesActivityOutcome.INVALID_CONTACT,
        ]:
            await _seed_completed_task_with_activity(
                session, lead_neg.id, opp_neg.id, out, "DISCOVERY_CALL", days_ago=2
            )

        # Opp 2: Neutral completed outcomes (NO_RESPONSE)
        lead_neu = await _seed_lead(session, "Neutral Corp", "neu@example.com")
        await _seed_profile(session, lead_neu.id, dm_score=80)
        opp_neu = await _seed_opportunity(session, lead_neu.id, "Neutral Opp", score=80)
        for _ in range(3):
            await _seed_completed_task_with_activity(
                session, lead_neu.id, opp_neu.id, SalesActivityOutcome.NO_RESPONSE, "DISCOVERY_CALL", days_ago=2
            )

        # Opp 3: Unresolved tasks only (CANCELLED tasks, outcome=None)
        lead_unres = await _seed_lead(session, "Unresolved Corp", "unres@example.com")
        await _seed_profile(session, lead_unres.id, dm_score=80)
        opp_unres = await _seed_opportunity(session, lead_unres.id, "Unresolved Opp", score=80)
        for _ in range(2):
            await _seed_completed_task_with_activity(
                session,
                lead_unres.id,
                opp_unres.id,
                outcome=None,
                action_name="DISCOVERY_CALL",
                status=SalesTaskStatus.CANCELLED,
                days_ago=2,
            )

        # Opp 4: Mixed (2 positive, 1 neutral, 2 cancelled)
        lead_mix = await _seed_lead(session, "Mixed Corp", "mix@example.com")
        await _seed_profile(session, lead_mix.id, dm_score=80)
        opp_mix = await _seed_opportunity(session, lead_mix.id, "Mixed Opp", score=80)
        for out in [SalesActivityOutcome.CONNECTED, SalesActivityOutcome.INTERESTED]:
            await _seed_completed_task_with_activity(session, lead_mix.id, opp_mix.id, out, "DISCOVERY_CALL", days_ago=2)
        await _seed_completed_task_with_activity(session, lead_mix.id, opp_mix.id, SalesActivityOutcome.NO_RESPONSE, "DISCOVERY_CALL", days_ago=2)
        for _ in range(2):
            await _seed_completed_task_with_activity(session, lead_mix.id, opp_mix.id, outcome=None, action_name="DISCOVERY_CALL", status=SalesTaskStatus.CANCELLED, days_ago=2)

        await session.commit()

        # Check feedback summary for mixed opportunity: denominator is 3 (2 pos + 1 neu), cancelled excluded
        mix_fb = await get_opportunity_sales_feedback(session, opp_mix.id)
        assert mix_fb is not None
        act_summary = mix_fb.action_performance.get("DISCOVERY_CALL")
        assert act_summary is not None
        assert act_summary.verified_executions == 3
        assert act_summary.positive_count == 2
        assert act_summary.verified_positive_rate == round((2 / 3) * 100, 2)

        response = await evaluate_sales_supervisor_pipeline(session)
        act_neg = next(a for a in response.actions if a.lead_id == lead_neg.id)
        act_neu = next(a for a in response.actions if a.lead_id == lead_neu.id)
        act_unres = next(a for a in response.actions if a.lead_id == lead_unres.id)
        act_mix_cand = next(a for a in response.actions if a.lead_id == lead_mix.id)

        # Negative: 0% positive rate, feedback component = 0.0
        assert act_neg.historical_feedback_rate == 0.0
        assert act_neg.score_breakdown.feedback_component == 0.0
        assert act_neg.score_breakdown.fallback_applied is False

        # Neutral: counted in verified denominator (3 executions), 0% positive rate, feedback component = 0.0
        assert act_neu.historical_feedback_rate == 0.0
        assert act_neu.score_breakdown.feedback_component == 0.0
        assert act_neu.score_breakdown.fallback_applied is False

        # Unresolved: 0 verified executions, neutral prior fallback 50.0 applied
        assert act_unres.historical_feedback_rate is None
        assert act_unres.score_breakdown.historical_feedback == DEFAULT_NEUTRAL_FEEDBACK_SCORE
        assert act_unres.score_breakdown.feedback_component == 10.0  # 0.20 * 50.0
        assert act_unres.score_breakdown.fallback_applied is True

        # Mixed: 66.67% rate, feedback component = round(0.20 * 66.67, 4) = 13.334
        assert act_mix_cand.historical_feedback_rate == round((2 / 3) * 100, 2)
        assert act_mix_cand.score_breakdown.fallback_applied is False
        assert act_mix_cand.score_breakdown.feedback_component == round(0.20 * round((2 / 3) * 100, 2), 4)


# ============================================================================
# E2E-04: Duplicate Provenance
# ============================================================================

@pytest.mark.asyncio
async def test_e2e_04_duplicate_provenance(session_factory):
    """
    E2E-04: Multiple sales tasks sharing an identical underlying activity_id
    do not inflate the verified execution count. Missing activity references
    are keyed by task_id and not silently merged.
    """
    async with session_factory() as session:
        lead = await _seed_lead(session, "Dedupe Corp", "dedupe@example.com")
        await _seed_profile(session, lead.id, dm_score=85)
        opp = await _seed_opportunity(session, lead.id, "Dedupe Opp", score=80)

        # 1. Single underlying activity (positive CONNECTED)
        act1 = SalesActivity(
            id=uuid.uuid4(),
            lead_id=lead.id,
            activity_type=SalesActivityType.CALL,
            note="Original connected call",
            outcome=SalesActivityOutcome.CONNECTED,
            occurred_at=now_utc() - timedelta(days=2),
        )
        session.add(act1)

        # 2. Two distinct tasks referencing the SAME activity_id (e.g. retry or dual tracking)
        for i in range(2):
            task = SalesTask(
                id=uuid.uuid4(),
                lead_id=lead.id,
                task_type=SalesTaskType.CALL,
                title=f"Task {i} referencing act1",
                due_at=now_utc() - timedelta(days=2),
                status=SalesTaskStatus.COMPLETED,
                agent_source_type="SALES_OPPORTUNITY",
                agent_source_id=opp.id,
                agent_source_action_id=f"contact_recommendation:{lead.id}:DISCOVERY_CALL:dup_{i}",
                related_activity_id=act1.id,
                completed_at=now_utc() - timedelta(days=2),
            )
            session.add(task)

        # 3. Another distinct activity (INTERESTED)
        act2 = SalesActivity(
            id=uuid.uuid4(),
            lead_id=lead.id,
            activity_type=SalesActivityType.CALL,
            note="Second call",
            outcome=SalesActivityOutcome.INTERESTED,
            occurred_at=now_utc() - timedelta(days=2),
        )
        session.add(act2)

        task3 = SalesTask(
            id=uuid.uuid4(),
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Task referencing act2",
            due_at=now_utc() - timedelta(days=2),
            status=SalesTaskStatus.COMPLETED,
            agent_source_type="SALES_OPPORTUNITY",
            agent_source_id=opp.id,
            agent_source_action_id=f"contact_recommendation:{lead.id}:DISCOVERY_CALL:distinct",
            related_activity_id=act2.id,
            completed_at=now_utc() - timedelta(days=2),
        )
        session.add(task3)

        await session.commit()

        # Query feedback summary
        fb_summary = await get_opportunity_sales_feedback(session, opp.id)
        assert fb_summary is not None
        act_summary = fb_summary.action_performance.get("DISCOVERY_CALL")
        assert act_summary is not None

        # Despite 3 completed tasks in the DB, only 2 unique activities exist -> 2 verified executions
        assert act_summary.verified_executions == 2, (
            f"Expected 2 verified executions after deduplication, got {act_summary.verified_executions}"
        )
        assert act_summary.positive_count == 2
        assert act_summary.verified_positive_rate == 100.0


# ============================================================================
# E2E-05: Readiness Cannot Be Overridden
# ============================================================================

@pytest.mark.asyncio
async def test_e2e_05_readiness_cannot_be_overridden(session_factory):
    """
    E2E-05: Verifies that readiness gates cannot be bypassed by high priority scores
    or human approval across all 5 pre-flight gates:
    - G1: INSUFFICIENT_EVIDENCE
    - G2: MISSING_TARGET_DATA
    - G3: BLOCKED_OPT_OUT
    - G4: BLOCKED_COOLDOWN
    - G5: ACTIVE_TASK_EXISTS
    """
    async with session_factory() as session:
        # G1: Empty evidence & no source_reference
        lead_g1 = await _seed_lead(session, "G1 Corp", "g1@example.com")
        await _seed_profile(session, lead_g1.id, dm_score=90)
        await _seed_opportunity(
            session, lead_g1.id, "G1 Opp", score=90, evidence={}, source_reference=None
        )

        # G2: Missing target data (email=None for email action)
        lead_g2 = await _seed_lead(session, "G2 Corp", email=None, phone=None)
        await _seed_profile(session, lead_g2.id, dm_score=90)
        # Note: with no email/phone/linkedin, contact_recommendation recommends RESEARCH_CONTACT unless forced.
        # To test G2 on email outreach, give lead an invalid email string:
        lead_g2_invalid = await _seed_lead(session, "G2 Invalid", email="notanemail", phone="123")
        await _seed_profile(session, lead_g2_invalid.id, dm_score=90)
        await _seed_opportunity(session, lead_g2_invalid.id, "G2 Opp", score=90)

        # G3: Opt-Out
        lead_g3 = await _seed_lead(session, "G3 Corp", "g3@example.com", email_opted_out=True)
        await _seed_profile(session, lead_g3.id, dm_score=90)
        await _seed_opportunity(session, lead_g3.id, "G3 Opp", score=90)

        # G4: Cooldown (activity occurred 1 hour ago)
        lead_g4 = await _seed_lead(session, "G4 Corp", "g4@example.com")
        await _seed_profile(session, lead_g4.id, dm_score=90)
        await _seed_opportunity(session, lead_g4.id, "G4 Opp", score=90)
        session.add(
            SalesActivity(
                id=uuid.uuid4(),
                lead_id=lead_g4.id,
                activity_type=SalesActivityType.EMAIL,
                note="Recent email",
                occurred_at=now_utc() - timedelta(hours=1),
            )
        )

        # G5: Active Task Collision (OPEN task exists)
        lead_g5 = await _seed_lead(session, "G5 Corp", "g5@example.com")
        await _seed_profile(session, lead_g5.id, dm_score=90)
        opp_g5 = await _seed_opportunity(session, lead_g5.id, "G5 Opp", score=90)
        session.add(
            SalesTask(
                id=uuid.uuid4(),
                lead_id=lead_g5.id,
                task_type=SalesTaskType.EMAIL,
                title="Active open outreach",
                due_at=now_utc() + timedelta(days=1),
                status=SalesTaskStatus.OPEN,
                agent_source_type="SALES_OPPORTUNITY",
                agent_source_id=opp_g5.id,
            )
        )

        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)

        action_g1 = next(a for a in response.actions if a.lead_id == lead_g1.id)
        action_g2 = next(a for a in response.actions if a.lead_id == lead_g2_invalid.id)
        action_g3 = next(a for a in response.actions if a.lead_id == lead_g3.id)
        action_g4 = next(a for a in response.actions if a.lead_id == lead_g4.id)
        action_g5 = next(a for a in response.actions if a.lead_id == lead_g5.id)

        assert action_g1.readiness_status == SupervisorActionReadinessStatus.INSUFFICIENT_EVIDENCE
        assert action_g2.readiness_status == SupervisorActionReadinessStatus.MISSING_TARGET_DATA
        assert action_g3.readiness_status == SupervisorActionReadinessStatus.BLOCKED_OPT_OUT
        assert action_g4.readiness_status == SupervisorActionReadinessStatus.BLOCKED_COOLDOWN
        assert action_g5.readiness_status == SupervisorActionReadinessStatus.ACTIVE_TASK_EXISTS

        # Now test that human approval CANNOT authorize any of these blocked actions
        blocked_candidates = [action_g1, action_g2, action_g3, action_g4, action_g5]
        for candidate in blocked_candidates:
            assert candidate.priority_score > 60.0, "Score should remain high independently of readiness"
            # Create a fully approved matching human approval
            approval = _create_matching_approval(candidate, status=SupervisorApprovalStatus.APPROVED)

            auth_result = authorize_supervisor_action_execution(candidate, approval=approval)

            assert auth_result.authorized is False, f"Candidate {candidate.action_id} must not be authorized"
            assert auth_result.decision == SupervisorExecutionDecision.DENIED
            assert auth_result.denial_code == f"READINESS_{candidate.readiness_status.value}"
            assert is_action_ready_for_execution(candidate.readiness_status) is False


# ============================================================================
# E2E-06: External Action Approval Enforcement
# ============================================================================

@pytest.mark.asyncio
async def test_e2e_06_external_action_approval_enforcement(session_factory):
    """
    E2E-06: External outreach actions strictly require valid, unexpired human approval
    with matching action identity. Rejections, revocations, and mismatches are denied.
    """
    async with session_factory() as session:
        lead = await _seed_lead(session, "Auth Corp", "auth@example.com", phone="555-0188")
        await _seed_profile(session, lead.id, dm_score=85)
        await _seed_opportunity(session, lead.id, "Auth Opp", score=85)
        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1
        action = response.actions[0]
        assert action.readiness_status == SupervisorActionReadinessStatus.READY
        assert action.policy_approval_required is True

        # Case 1: Missing approval -> REQUIRES_APPROVAL
        res_no_appr = authorize_supervisor_action_execution(action, approval=None)
        assert res_no_appr.authorized is False
        assert res_no_appr.decision == SupervisorExecutionDecision.REQUIRES_APPROVAL
        assert res_no_appr.denial_code == "APPROVAL_REQUIRED"

        # Case 2: Valid approved approval -> AUTHORIZED
        valid_appr = _create_matching_approval(action, approver_id="sarah_director")
        res_valid = authorize_supervisor_action_execution(action, approval=valid_appr)
        assert res_valid.authorized is True
        assert res_valid.decision == SupervisorExecutionDecision.AUTHORIZED

        # Case 3: Empty approver identity -> DENIED (INVALID_APPROVER_IDENTITY)
        empty_appr = _create_matching_approval(action, approver_id="   ")
        res_empty = authorize_supervisor_action_execution(action, approval=empty_appr)
        assert res_empty.authorized is False
        assert res_empty.decision == SupervisorExecutionDecision.DENIED
        assert res_empty.denial_code == "INVALID_APPROVER_IDENTITY"

        # Case 4: Status REJECTED -> DENIED
        rej_appr = _create_matching_approval(action, status=SupervisorApprovalStatus.REJECTED)
        res_rej = authorize_supervisor_action_execution(action, approval=rej_appr)
        assert res_rej.authorized is False
        assert res_rej.decision == SupervisorExecutionDecision.DENIED
        assert res_rej.denial_code == "APPROVAL_REJECTED"

        # Case 5: Status REVOKED -> DENIED
        rev_appr = _create_matching_approval(action, status=SupervisorApprovalStatus.REVOKED)
        res_rev = authorize_supervisor_action_execution(action, approval=rev_appr)
        assert res_rev.authorized is False
        assert res_rev.decision == SupervisorExecutionDecision.DENIED
        assert res_rev.denial_code == "APPROVAL_REVOKED"

        # Case 6: Expired approval -> DENIED
        now = now_utc()
        expired_appr = _create_matching_approval(
            action, expires_at=now - timedelta(minutes=10)
        )
        res_exp = authorize_supervisor_action_execution(action, approval=expired_appr, eval_time=now)
        assert res_exp.authorized is False
        assert res_exp.decision == SupervisorExecutionDecision.DENIED
        assert res_exp.denial_code == "APPROVAL_EXPIRED"

        # Case 7: Action identity mismatch -> DENIED
        mismatch_appr = valid_appr.model_copy(update={"action_id": "different:action:id"})
        res_mismatch = authorize_supervisor_action_execution(action, approval=mismatch_appr)
        assert res_mismatch.authorized is False
        assert res_mismatch.decision == SupervisorExecutionDecision.DENIED
        assert res_mismatch.denial_code == "ACTION_MISMATCH"


# ============================================================================
# E2E-07: Internal Operation Authorization
# ============================================================================

@pytest.mark.asyncio
async def test_e2e_07_internal_operation_authorization(session_factory):
    """
    E2E-07: Internal operations (RESEARCH_CONTACT) are authorized without human approval
    when READY, but respect human veto (REJECTED/REVOKED) and fail closed on invalid types.
    """
    async with session_factory() as session:
        # Create a lead with NO email and NO phone, but strong buying influence -> RESEARCH_CONTACT
        lead = await _seed_lead(
            session, "Internal Research Lead", email=None, phone=None, linkedin_url=None
        )
        await _seed_profile(session, lead.id, dm_score=85, influence=BuyingInfluenceLevel.DECISION_MAKER)
        await _seed_opportunity(session, lead.id, "Internal Research Opp", score=85)
        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert len(response.actions) == 1
        action = response.actions[0]

        assert action.recommended_action == RecommendedSalesAction.RESEARCH_CONTACT
        assert action.readiness_status == SupervisorActionReadinessStatus.READY
        assert action.policy_approval_required is False

        # 1. System authorized without human approval
        res_auto = authorize_supervisor_action_execution(action, approval=None)
        assert res_auto.authorized is True
        assert res_auto.decision == SupervisorExecutionDecision.AUTHORIZED
        assert res_auto.action_tier == SupervisorActionTier.INTERNAL_OPERATION
        assert res_auto.approval_required is False

        # 2. Human supervisor veto is respected (explicit REJECTED)
        veto_appr = _create_matching_approval(action, status=SupervisorApprovalStatus.REJECTED)
        res_veto = authorize_supervisor_action_execution(action, approval=veto_appr)
        assert res_veto.authorized is False
        assert res_veto.decision == SupervisorExecutionDecision.DENIED
        assert res_veto.denial_code == "APPROVAL_REJECTED"

        # 3. Blocked readiness prevents authorization even for internal operation
        blocked_internal = action.model_copy(
            update={"readiness_status": SupervisorActionReadinessStatus.ACTIVE_TASK_EXISTS}
        )
        res_blocked = authorize_supervisor_action_execution(blocked_internal, approval=None)
        assert res_blocked.authorized is False
        assert res_blocked.decision == SupervisorExecutionDecision.DENIED
        assert res_blocked.denial_code == "READINESS_ACTIVE_TASK_EXISTS"

        # 4. Unknown action type fails closed
        unknown_action = action.model_copy(
            update={"recommended_action": "FABRICATED_ACTION_TYPE"}  # type: ignore
        )
        res_unknown = authorize_supervisor_action_execution(unknown_action, approval=None)
        assert res_unknown.authorized is False
        assert res_unknown.decision == SupervisorExecutionDecision.DENIED
        assert res_unknown.denial_code == "UNKNOWN_ACTION_TYPE"


# ============================================================================
# E2E-08: Batch Consistency
# ============================================================================

@pytest.mark.asyncio
async def test_e2e_08_batch_consistency(session_factory):
    """
    E2E-08: Batch evaluation evaluates multiple candidate actions simultaneously:
    1. Exact 1:1 mapping between input candidates and output authorizations.
    2. Deterministic ordering preserved.
    3. Zero authorization bleed across candidates.
    4. Blocked readiness on one candidate does not taint other candidates.
    """
    async with session_factory() as session:
        # Candidate 1: Ready Email with valid approval
        lead_1 = await _seed_lead(session, "Batch Corp 1", "c1@example.com")
        await _seed_profile(session, lead_1.id, dm_score=90)
        await _seed_opportunity(session, lead_1.id, "Opp 1", score=90)

        # Candidate 2: Ready Email without approval
        lead_2 = await _seed_lead(session, "Batch Corp 2", "c2@example.com")
        await _seed_profile(session, lead_2.id, dm_score=80)
        await _seed_opportunity(session, lead_2.id, "Opp 2", score=80)

        # Candidate 3: Ready Internal Research (no email/phone)
        lead_3 = await _seed_lead(session, "Batch Corp 3", email=None, phone=None, linkedin_url=None)
        await _seed_profile(session, lead_3.id, dm_score=85, influence=BuyingInfluenceLevel.DECISION_MAKER)
        await _seed_opportunity(session, lead_3.id, "Opp 3", score=75)

        # Candidate 4: Blocked by Opt-Out (with approval provided)
        lead_4 = await _seed_lead(session, "Batch Corp 4", "c4@example.com", email_opted_out=True)
        await _seed_profile(session, lead_4.id, dm_score=85)
        await _seed_opportunity(session, lead_4.id, "Opp 4", score=70)

        # Candidate 5: Ready Email with REJECTED approval
        lead_5 = await _seed_lead(session, "Batch Corp 5", "c5@example.com")
        await _seed_profile(session, lead_5.id, dm_score=85)
        await _seed_opportunity(session, lead_5.id, "Opp 5", score=65)

        await session.commit()

        pipeline_res = await evaluate_sales_supervisor_pipeline(session)
        assert len(pipeline_res.actions) == 5

        # Map candidates by lead
        c1 = next(a for a in pipeline_res.actions if a.lead_id == lead_1.id)
        c2 = next(a for a in pipeline_res.actions if a.lead_id == lead_2.id)
        c3 = next(a for a in pipeline_res.actions if a.lead_id == lead_3.id)
        c4 = next(a for a in pipeline_res.actions if a.lead_id == lead_4.id)
        c5 = next(a for a in pipeline_res.actions if a.lead_id == lead_5.id)

        # Build approvals map
        approvals_map = {
            c1.action_id: _create_matching_approval(c1, approver_id="supervisor_c1"),
            # c2 has NO approval
            # c3 is internal (no approval needed)
            c4.action_id: _create_matching_approval(c4, approver_id="supervisor_c4"),
            c5.action_id: _create_matching_approval(c5, approver_id="supervisor_c5", status=SupervisorApprovalStatus.REJECTED),
        }

        # Run batch authorization evaluation
        batch_results = evaluate_pipeline_execution_authorizations(
            pipeline_res.actions, approvals_map
        )

        assert len(batch_results) == 5
        # Verify deterministic order: results match candidate order 1:1
        for cand, res in zip(pipeline_res.actions, batch_results):
            assert cand.action_id == res.action_id

        # Verify decisions
        res_by_id = {r.action_id: r for r in batch_results}

        # c1: AUTHORIZED
        assert res_by_id[c1.action_id].authorized is True
        assert res_by_id[c1.action_id].decision == SupervisorExecutionDecision.AUTHORIZED

        # c2: REQUIRES_APPROVAL (did NOT bleed from c1's approval)
        assert res_by_id[c2.action_id].authorized is False
        assert res_by_id[c2.action_id].decision == SupervisorExecutionDecision.REQUIRES_APPROVAL

        # c3: AUTHORIZED (internal autonomous)
        assert res_by_id[c3.action_id].authorized is True
        assert res_by_id[c3.action_id].decision == SupervisorExecutionDecision.AUTHORIZED

        # c4: DENIED (readiness block held despite having valid approval)
        assert res_by_id[c4.action_id].authorized is False
        assert res_by_id[c4.action_id].decision == SupervisorExecutionDecision.DENIED
        assert res_by_id[c4.action_id].denial_code == "READINESS_BLOCKED_OPT_OUT"

        # c5: DENIED (human rejected)
        assert res_by_id[c5.action_id].authorized is False
        assert res_by_id[c5.action_id].decision == SupervisorExecutionDecision.DENIED
        assert res_by_id[c5.action_id].denial_code == "APPROVAL_REJECTED"


# ============================================================================
# E2E-09: No Execution Side Effects
# ============================================================================

@pytest.mark.asyncio
async def test_e2e_09_no_execution_side_effects(session_factory, monkeypatch):
    """
    E2E-09: Verifies zero execution side effects:
    1. Zero external communications dispatched (network calls raise error if attempted).
    2. Read-only guarantee: zero database mutations (record counts identical before/after).
    """
    # Guard against accidental socket/network calls
    def _fail_on_network(*args, **kwargs):
        raise RuntimeError("Prohibited external network call detected during supervisor evaluation!")

    monkeypatch.setattr(socket, "create_connection", _fail_on_network)

    async with session_factory() as session:
        lead = await _seed_lead(session, "SideEffect Free Corp", "se@example.com")
        await _seed_profile(session, lead.id, dm_score=85)
        opp = await _seed_opportunity(session, lead.id, "SideEffect Opp", score=85)
        await session.commit()

        # Snapshot initial database record counts
        lead_count_before = await session.scalar(select(func.count()).select_from(Lead))
        opp_count_before = await session.scalar(select(func.count()).select_from(LeadIntelligenceOpportunity))
        task_count_before = await session.scalar(select(func.count()).select_from(SalesTask))
        act_count_before = await session.scalar(select(func.count()).select_from(SalesActivity))
        prof_count_before = await session.scalar(select(func.count()).select_from(ContactIntelligenceProfile))

        # Execute full supervisor pipeline and authorization
        pipeline_res = await evaluate_sales_supervisor_pipeline(session)
        assert len(pipeline_res.actions) == 1
        action = pipeline_res.actions[0]

        appr = _create_matching_approval(action)
        auth_res = authorize_supervisor_action_execution(action, approval=appr)
        assert auth_res.authorized is True

        batch_res = evaluate_pipeline_execution_authorizations(
            pipeline_res.actions, {action.action_id: appr}
        )
        assert len(batch_res) == 1

        # Re-check database record counts
        lead_count_after = await session.scalar(select(func.count()).select_from(Lead))
        opp_count_after = await session.scalar(select(func.count()).select_from(LeadIntelligenceOpportunity))
        task_count_after = await session.scalar(select(func.count()).select_from(SalesTask))
        act_count_after = await session.scalar(select(func.count()).select_from(SalesActivity))
        prof_count_after = await session.scalar(select(func.count()).select_from(ContactIntelligenceProfile))

        assert lead_count_after == lead_count_before
        assert opp_count_after == opp_count_before
        assert task_count_after == task_count_before
        assert act_count_after == act_count_before
        assert prof_count_after == prof_count_before


# ============================================================================
# E2E-10: Regression Integrity
# ============================================================================

def test_e2e_10_regression_integrity():
    """
    E2E-10: Regression integrity across Phase 12J models, legacy Phase 10B models,
    weights, enums, schemas, and backward compatibility.
    """
    # 1. Weights integrity
    total_weights = (
        WEIGHT_OPPORTUNITY_SCORE
        + WEIGHT_RECOMMENDATION_FIT
        + WEIGHT_HISTORICAL_FEEDBACK
        + WEIGHT_URGENCY
    )
    assert math.isclose(total_weights, 1.0, rel_tol=1e-9)
    assert WEIGHT_OPPORTUNITY_SCORE == 0.35
    assert WEIGHT_RECOMMENDATION_FIT == 0.30
    assert WEIGHT_HISTORICAL_FEEDBACK == 0.20
    assert WEIGHT_URGENCY == 0.15

    # 2. Domain constants
    assert DEFAULT_NEUTRAL_FEEDBACK_SCORE == 50.0
    assert DEFAULT_MIN_FEEDBACK_EXECUTIONS == 1
    assert MIN_SCORE == 0.0
    assert MAX_SCORE == 100.0

    # 3. Enum completeness
    assert set(s.value for s in SupervisorActionReadinessStatus) == {
        "READY",
        "BLOCKED_OPT_OUT",
        "BLOCKED_COOLDOWN",
        "MISSING_TARGET_DATA",
        "INSUFFICIENT_EVIDENCE",
        "ACTIVE_TASK_EXISTS",
    }
    assert set(d.value for d in SupervisorExecutionDecision) == {
        "AUTHORIZED",
        "DENIED",
        "REQUIRES_APPROVAL",
    }
    assert set(t.value for t in SupervisorActionTier) == {
        "INTERNAL_OPERATION",
        "EXTERNAL_OUTREACH",
    }
    assert set(s.value for s in SupervisorApprovalStatus) == {
        "APPROVED",
        "REJECTED",
        "PENDING",
        "REVOKED",
    }

    # 4. Backward compatibility with legacy Phase 10B models
    assert hasattr(AiSalesSupervisorRun, "__tablename__")
    assert hasattr(AutonomousTaskExecution, "__tablename__")
    assert hasattr(AgentPolicyEvaluation, "__tablename__")
    assert hasattr(SalesTask, "__tablename__")

    # 5. Domain calculate_supervisor_action_priority pure function stability
    bd = calculate_supervisor_action_priority(
        opportunity_score=80.0,
        recommendation_fit=80.0,
        historical_feedback=None,
        urgency=20.0,
    )
    # Expected: 0.35*80 + 0.30*80 + 0.20*50 + 0.15*20 = 28 + 24 + 10 + 3 = 65.0
    assert bd.final_score == 65.0
    assert bd.fallback_applied is True
    assert bd.feedback_available is False

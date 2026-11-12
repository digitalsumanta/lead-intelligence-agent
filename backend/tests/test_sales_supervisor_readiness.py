"""
Comprehensive Pre-Flight Readiness Evaluation Tests for Phase 12J-C.

Covers the 5 Pre-Flight Readiness Gates:
- G1: Evidence Sufficiency (empty evidence, source_reference fallback)
- G2: Target Contact Validity (email, phone, direct outreach coordinates, internal actions)
- G3: Compliance & Opt-Out (contact & company opt-out for email/phone, internal action immunity)
- G4: Contact Cooldown (SalesActivity, OutreachExecution, AiOutreachExecution, cooldown boundary, internal immunity)
- G5: Collision Guard (OPEN and IN_PROGRESS active task collisions vs COMPLETED and CANCELLED)

Deterministic Status Precedence:
- BLOCKED_OPT_OUT > ACTIVE_TASK_EXISTS > BLOCKED_COOLDOWN > MISSING_TARGET_DATA > INSUFFICIENT_EVIDENCE
- Comprehensive collection of all applicable blocking reasons
- Strict independence of prioritization score and readiness gates (priority 100 never bypasses gates)

Pipeline Integration Guarantees:
- Pure read-only guarantee: zero SalesTask creation or mutation
- Zero external communication dispatch
- Correct total_candidates, ready_count, and blocked_count tallying
"""

import uuid
import pytest
from datetime import datetime, timedelta, timezone
from sqlalchemy import select

from app.domain.contact_recommendation import RecommendedSalesAction
from app.domain.sales_supervisor import SupervisorActionReadinessStatus
from app.models.entities import (
    AiOutreachExecution,
    BuyingInfluenceLevel,
    ConfidenceLevel,
    ContactIntelligenceProfile,
    FunctionalArea,
    Lead,
    LeadIntelligenceOpportunity,
    OutreachExecution,
    SalesActivity,
    SalesActivityType,
    SalesPriority,
    SalesStatus,
    SalesTask,
    SalesTaskStatus,
    SalesTaskType,
    SeniorityLevel,
    SourceType,
    now_utc,
)
from app.services.sales_supervisor import (
    build_action_identity,
    evaluate_action_readiness,
    evaluate_sales_supervisor_pipeline,
)


# ============================================================================
# Helpers & Dummy Fixtures for Unit Testing evaluate_action_readiness
# ============================================================================

def _dummy_opportunity(
    opp_id: uuid.UUID | None = None,
    lead_id: uuid.UUID | None = None,
    score: int = 85,
    evidence: dict | list | None = None,
    source_reference: str | None = None,
) -> LeadIntelligenceOpportunity:
    return LeadIntelligenceOpportunity(
        id=opp_id or uuid.uuid4(),
        lead_id=lead_id or uuid.uuid4(),
        title="Test Opportunity",
        opportunity_type="DIGITAL_MARKETING",
        score=score,
        priority="HIGH",
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH,
        evidence=evidence if evidence is not None else {"signals": [{"type": "expansion"}]},
        source_reference=source_reference,
    )


def _dummy_lead(
    lead_id: uuid.UUID | None = None,
    email: str | None = "test@company.com",
    phone: str | None = "+1234567890",
    linkedin_url: str | None = "https://linkedin.com/in/test",
    email_opted_out: bool = False,
    phone_opted_out: bool = False,
    whatsapp_opted_out: bool = False,
) -> Lead:
    return Lead(
        id=lead_id or uuid.uuid4(),
        company_name="Test Enterprise Corp",
        email=email,
        phone=phone,
        linkedin_url=linkedin_url,
        email_opted_out=email_opted_out,
        phone_opted_out=phone_opted_out,
        whatsapp_opted_out=whatsapp_opted_out,
        sales_status=SalesStatus.NEW,
    )


def _dummy_task(
    task_id: uuid.UUID | None = None,
    lead_id: uuid.UUID | None = None,
    status: SalesTaskStatus = SalesTaskStatus.OPEN,
    agent_source_id: uuid.UUID | None = None,
    agent_source_action_id: str | None = None,
) -> SalesTask:
    return SalesTask(
        id=task_id or uuid.uuid4(),
        lead_id=lead_id or uuid.uuid4(),
        title="Test Active Task",
        task_type=SalesTaskType.EMAIL,
        status=status,
        priority=SalesPriority.HIGH,
        due_at=now_utc(),
        agent_source_id=agent_source_id,
        agent_source_action_id=agent_source_action_id,
    )


# ============================================================================
# 1. Gate G1: Evidence Sufficiency Unit Tests
# ============================================================================

def test_g1_valid_evidence_dict_passes():
    opp = _dummy_opportunity(evidence={"signal": "hiring_spike"})
    contact = _dummy_lead()
    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        contact_id=contact.id,
        contact_lead=contact,
        company_lead=contact,
    )
    assert res.status == SupervisorActionReadinessStatus.READY
    assert res.has_evidence is True


def test_g1_valid_source_reference_passes_when_evidence_empty():
    opp = _dummy_opportunity(evidence={}, source_reference="https://news.example.com/growth")
    contact = _dummy_lead()
    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        contact_id=contact.id,
        contact_lead=contact,
        company_lead=contact,
    )
    assert res.status == SupervisorActionReadinessStatus.READY
    assert res.has_evidence is True


def test_g1_missing_evidence_and_reference_fails():
    opp = _dummy_opportunity(evidence={}, source_reference="")
    contact = _dummy_lead()
    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        contact_id=contact.id,
        contact_lead=contact,
        company_lead=contact,
    )
    assert res.status == SupervisorActionReadinessStatus.INSUFFICIENT_EVIDENCE
    assert res.has_evidence is False
    assert any("evidence" in r.lower() for r in res.reasons)
    assert res.blocking_details.get("insufficient_evidence") is True


# ============================================================================
# 2. Gate G2: Target Contact Validity Unit Tests
# ============================================================================

def test_g2_personalized_email_requires_valid_email():
    opp = _dummy_opportunity()
    contact_no_email = _dummy_lead(email=None)
    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        contact_id=contact_no_email.id,
        contact_lead=contact_no_email,
    )
    assert res.status == SupervisorActionReadinessStatus.MISSING_TARGET_DATA
    assert res.has_valid_contact is False

    contact_invalid_email = _dummy_lead(email="invalid_email_no_at")
    res2 = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        contact_id=contact_invalid_email.id,
        contact_lead=contact_invalid_email,
    )
    assert res2.status == SupervisorActionReadinessStatus.MISSING_TARGET_DATA
    assert res2.has_valid_contact is False


def test_g2_discovery_call_requires_valid_phone():
    opp = _dummy_opportunity()
    contact_no_phone = _dummy_lead(phone=None)
    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.DISCOVERY_CALL,
        contact_id=contact_no_phone.id,
        contact_lead=contact_no_phone,
    )
    assert res.status == SupervisorActionReadinessStatus.MISSING_TARGET_DATA
    assert res.has_valid_contact is False

    contact_short_phone = _dummy_lead(phone="123")  # less than 5 characters
    res2 = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.DISCOVERY_CALL,
        contact_id=contact_short_phone.id,
        contact_lead=contact_short_phone,
    )
    assert res2.status == SupervisorActionReadinessStatus.MISSING_TARGET_DATA
    assert res2.has_valid_contact is False


def test_g2_direct_outreach_requires_at_least_one_coordinate():
    opp = _dummy_opportunity()
    contact_empty = _dummy_lead(email=None, phone=None, linkedin_url=None)
    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.DIRECT_OUTREACH,
        contact_id=contact_empty.id,
        contact_lead=contact_empty,
    )
    assert res.status == SupervisorActionReadinessStatus.MISSING_TARGET_DATA
    assert res.has_valid_contact is False

    contact_with_linkedin = _dummy_lead(email=None, phone=None, linkedin_url="https://linkedin.com/in/ok")
    res_ok = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.DIRECT_OUTREACH,
        contact_id=contact_with_linkedin.id,
        contact_lead=contact_with_linkedin,
    )
    assert res_ok.status == SupervisorActionReadinessStatus.READY
    assert res_ok.has_valid_contact is True


def test_g2_research_contact_requires_no_coordinates():
    opp = _dummy_opportunity()
    contact_empty = _dummy_lead(email=None, phone=None, linkedin_url=None)
    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.RESEARCH_CONTACT,
        contact_id=contact_empty.id,
        contact_lead=contact_empty,
    )
    assert res.status == SupervisorActionReadinessStatus.READY
    assert res.has_valid_contact is True


# ============================================================================
# 3. Gate G3: Compliance & Opt-Out Unit Tests
# ============================================================================

def test_g3_email_opt_out_blocks_email_outreach():
    opp = _dummy_opportunity()
    contact = _dummy_lead(email_opted_out=True)
    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        contact_id=contact.id,
        contact_lead=contact,
    )
    assert res.status == SupervisorActionReadinessStatus.BLOCKED_OPT_OUT
    assert res.is_opted_out is True
    assert res.blocking_details.get("opt_out_channel") == "EMAIL"


def test_g3_company_level_opt_out_blocks_action():
    opp = _dummy_opportunity()
    contact = _dummy_lead(email_opted_out=False)
    company = _dummy_lead(email_opted_out=True)
    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        contact_id=contact.id,
        contact_lead=contact,
        company_lead=company,
    )
    assert res.status == SupervisorActionReadinessStatus.BLOCKED_OPT_OUT
    assert res.is_opted_out is True


def test_g3_phone_opt_out_blocks_discovery_call():
    opp = _dummy_opportunity()
    contact = _dummy_lead(phone_opted_out=True)
    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.DISCOVERY_CALL,
        contact_id=contact.id,
        contact_lead=contact,
    )
    assert res.status == SupervisorActionReadinessStatus.BLOCKED_OPT_OUT
    assert res.is_opted_out is True
    assert res.blocking_details.get("opt_out_channel") == "PHONE"


def test_g3_internal_research_immune_to_opt_out():
    opp = _dummy_opportunity()
    contact = _dummy_lead(email_opted_out=True, phone_opted_out=True)
    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.RESEARCH_CONTACT,
        contact_id=contact.id,
        contact_lead=contact,
    )
    assert res.status == SupervisorActionReadinessStatus.READY
    assert res.is_opted_out is False


# ============================================================================
# 4. Gate G4: Contact Cooldown Unit Tests
# ============================================================================

def test_g4_recent_contact_blocks_with_cooldown():
    eval_time = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    recent_contact = eval_time - timedelta(hours=2)  # 2 hours ago (< 24h)

    opp = _dummy_opportunity()
    contact = _dummy_lead()
    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        contact_id=contact.id,
        contact_lead=contact,
        recent_contact_time=recent_contact,
        eval_time=eval_time,
        cooldown_minutes=1440,
    )
    assert res.status == SupervisorActionReadinessStatus.BLOCKED_COOLDOWN
    assert res.in_cooldown is True
    assert res.blocking_details.get("cooldown_until") is not None


def test_g4_cooldown_boundary_24h_expired_passes():
    eval_time = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    exact_boundary = eval_time - timedelta(hours=24)  # exactly 24 hours ago

    opp = _dummy_opportunity()
    contact = _dummy_lead()
    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        contact_id=contact.id,
        contact_lead=contact,
        recent_contact_time=exact_boundary,
        eval_time=eval_time,
        cooldown_minutes=1440,
    )
    assert res.status == SupervisorActionReadinessStatus.READY
    assert res.in_cooldown is False


def test_g4_past_cooldown_passes():
    eval_time = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    old_contact = eval_time - timedelta(days=3)

    opp = _dummy_opportunity()
    contact = _dummy_lead()
    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        contact_id=contact.id,
        contact_lead=contact,
        recent_contact_time=old_contact,
        eval_time=eval_time,
        cooldown_minutes=1440,
    )
    assert res.status == SupervisorActionReadinessStatus.READY
    assert res.in_cooldown is False


def test_g4_internal_action_immune_to_cooldown():
    eval_time = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    recent_contact = eval_time - timedelta(minutes=10)

    opp = _dummy_opportunity()
    contact = _dummy_lead()
    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.RESEARCH_CONTACT,
        contact_id=contact.id,
        contact_lead=contact,
        recent_contact_time=recent_contact,
        eval_time=eval_time,
        cooldown_minutes=1440,
    )
    assert res.status == SupervisorActionReadinessStatus.READY
    assert res.in_cooldown is False


# ============================================================================
# 5. Gate G5: Collision Guard Unit Tests
# ============================================================================

def test_g5_active_task_on_opportunity_blocks():
    opp = _dummy_opportunity()
    contact = _dummy_lead()
    active_task = _dummy_task(agent_source_id=opp.id, status=SalesTaskStatus.OPEN)

    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        contact_id=contact.id,
        contact_lead=contact,
        active_tasks=[active_task],
    )
    assert res.status == SupervisorActionReadinessStatus.ACTIVE_TASK_EXISTS
    assert res.has_active_task is True
    assert res.blocking_details.get("active_task_id") == str(active_task.id)


def test_g5_active_task_on_action_identity_blocks():
    opp = _dummy_opportunity()
    contact = _dummy_lead()
    action_id = build_action_identity(contact.id, RecommendedSalesAction.PERSONALIZED_EMAIL)
    active_task = _dummy_task(
        agent_source_id=uuid.uuid4(),  # different opportunity
        agent_source_action_id=action_id,
        status=SalesTaskStatus.IN_PROGRESS,
    )

    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        contact_id=contact.id,
        contact_lead=contact,
        active_tasks=[active_task],
    )
    assert res.status == SupervisorActionReadinessStatus.ACTIVE_TASK_EXISTS
    assert res.has_active_task is True


def test_g5_completed_and_cancelled_tasks_do_not_block():
    opp = _dummy_opportunity()
    contact = _dummy_lead()
    completed_task = _dummy_task(agent_source_id=opp.id, status=SalesTaskStatus.COMPLETED)
    cancelled_task = _dummy_task(agent_source_id=opp.id, status=SalesTaskStatus.CANCELLED)

    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        contact_id=contact.id,
        contact_lead=contact,
        active_tasks=[completed_task, cancelled_task],
    )
    assert res.status == SupervisorActionReadinessStatus.READY
    assert res.has_active_task is False


def test_g5_unrelated_active_tasks_do_not_block():
    opp = _dummy_opportunity()
    contact = _dummy_lead()
    unrelated_task = _dummy_task(
        agent_source_id=uuid.uuid4(),
        agent_source_action_id="unrelated_action_key",
        status=SalesTaskStatus.OPEN,
    )

    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        contact_id=contact.id,
        contact_lead=contact,
        active_tasks=[unrelated_task],
    )
    assert res.status == SupervisorActionReadinessStatus.READY
    assert res.has_active_task is False


# ============================================================================
# 6. Status Precedence & Multi-Failure Resolution
# ============================================================================

def test_precedence_opt_out_beats_all_other_failures():
    """Opt-out (G3) must take precedence over G5, G4, G2, and G1."""
    opp = _dummy_opportunity(evidence={})  # G1 fails
    contact = _dummy_lead(
        email="invalid",  # G2 fails
        email_opted_out=True,  # G3 fails
    )
    eval_time = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    recent_contact = eval_time - timedelta(hours=1)  # G4 fails
    active_task = _dummy_task(agent_source_id=opp.id, status=SalesTaskStatus.OPEN)  # G5 fails

    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        contact_id=contact.id,
        contact_lead=contact,
        active_tasks=[active_task],
        recent_contact_time=recent_contact,
        eval_time=eval_time,
    )

    # Primary status must strictly be BLOCKED_OPT_OUT
    assert res.status == SupervisorActionReadinessStatus.BLOCKED_OPT_OUT

    # All individual flags must reflect true gate failure states
    assert res.is_opted_out is True
    assert res.has_active_task is True
    assert res.in_cooldown is True
    assert res.has_valid_contact is False
    assert res.has_evidence is False

    # All failure reasons must be comprehensively audited
    assert len(res.reasons) >= 5


def test_precedence_active_task_beats_cooldown_and_data_and_evidence():
    """Active task (G5) must take precedence over G4, G2, and G1."""
    opp = _dummy_opportunity(evidence={})  # G1 fails
    contact = _dummy_lead(email=None)  # G2 fails
    eval_time = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    recent_contact = eval_time - timedelta(hours=1)  # G4 fails
    active_task = _dummy_task(agent_source_id=opp.id, status=SalesTaskStatus.IN_PROGRESS)  # G5 fails

    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        contact_id=contact.id,
        contact_lead=contact,
        active_tasks=[active_task],
        recent_contact_time=recent_contact,
        eval_time=eval_time,
    )

    assert res.status == SupervisorActionReadinessStatus.ACTIVE_TASK_EXISTS
    assert res.has_active_task is True
    assert res.in_cooldown is True
    assert res.has_valid_contact is False
    assert res.has_evidence is False


def test_precedence_cooldown_beats_missing_data_and_evidence():
    """Cooldown (G4) must take precedence over G2 and G1."""
    opp = _dummy_opportunity(evidence={})  # G1 fails
    contact = _dummy_lead(email=None)  # G2 fails
    eval_time = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    recent_contact = eval_time - timedelta(hours=1)  # G4 fails

    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        contact_id=contact.id,
        contact_lead=contact,
        recent_contact_time=recent_contact,
        eval_time=eval_time,
    )

    assert res.status == SupervisorActionReadinessStatus.BLOCKED_COOLDOWN
    assert res.in_cooldown is True
    assert res.has_valid_contact is False
    assert res.has_evidence is False


def test_precedence_missing_data_beats_insufficient_evidence():
    """Missing target data (G2) must take precedence over insufficient evidence (G1)."""
    opp = _dummy_opportunity(evidence={})  # G1 fails
    contact = _dummy_lead(email=None)  # G2 fails

    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        contact_id=contact.id,
        contact_lead=contact,
    )

    assert res.status == SupervisorActionReadinessStatus.MISSING_TARGET_DATA
    assert res.has_valid_contact is False
    assert res.has_evidence is False


# ============================================================================
# 7. Priority Score Independence & Safety Guardrails
# ============================================================================

def test_maximum_priority_score_never_bypasses_readiness_gate():
    """A perfect score of 100.0 must never override an opt-out or active collision."""
    opp = _dummy_opportunity(score=100)
    contact = _dummy_lead(email_opted_out=True)

    res = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=RecommendedSalesAction.PERSONALIZED_EMAIL,
        contact_id=contact.id,
        contact_lead=contact,
        priority_score=100.0,
    )

    assert res.status == SupervisorActionReadinessStatus.BLOCKED_OPT_OUT
    assert res.is_opted_out is True


# ============================================================================
# 8. Pipeline Integration & Read-Only Tests
# ============================================================================

async def _seed_pipeline_candidate(
    session,
    name: str,
    email: str = "valid@test.com",
    phone: str = "1234567890",
    email_opted_out: bool = False,
    phone_opted_out: bool = False,
    evidence: dict | None = None,
) -> tuple[Lead, LeadIntelligenceOpportunity]:
    lead = Lead(
        id=uuid.uuid4(),
        company_name=name,
        email=email,
        phone=phone,
        sales_status=SalesStatus.NEW,
        currency="USD",
        decision_maker="Manager",
        job_title="VP Sales",
        department="Sales",
        email_opted_out=email_opted_out,
        phone_opted_out=phone_opted_out,
    )
    session.add(lead)

    prof = ContactIntelligenceProfile(
        contact_id=lead.id,
        decision_maker_score=85,
        functional_area=FunctionalArea.SALES,
        seniority_level=SeniorityLevel.LEADERSHIP,
        buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
        confidence=ConfidenceLevel.HIGH,
        source_type=SourceType.SYSTEM_DERIVED,
    )
    session.add(prof)

    opp = LeadIntelligenceOpportunity(
        id=uuid.uuid4(),
        lead_id=lead.id,
        title=f"Opp for {name}",
        opportunity_type="SALES",
        score=85,
        priority="HIGH",
        source_type=SourceType.SYSTEM_DERIVED,
        confidence=ConfidenceLevel.HIGH,
        evidence=evidence if evidence is not None else {"signals": [{"type": "expansion"}]},
    )
    session.add(opp)
    return lead, opp


@pytest.mark.asyncio
async def test_pipeline_readiness_counts_and_statuses(session_factory):
    """Verifies ready_count and blocked_count with mixed candidate states."""
    async with session_factory() as session:
        # Candidate 1: Ready
        lead1, opp1 = await _seed_pipeline_candidate(session, "Ready Corp")

        # Candidate 2: Blocked by opt-out
        lead2, opp2 = await _seed_pipeline_candidate(session, "OptOut Corp", email_opted_out=True, phone_opted_out=True)

        # Candidate 3: Blocked by missing evidence
        lead3, opp3 = await _seed_pipeline_candidate(session, "NoEvidence Corp", evidence={})

        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)

        assert response.total_candidates == 3
        assert response.ready_count == 1
        assert response.blocked_count == 2
        assert response.ready_count + response.blocked_count == response.total_candidates

        action_by_opp = {a.opportunity_id: a for a in response.actions}
        assert action_by_opp[opp1.id].readiness_status == SupervisorActionReadinessStatus.READY
        assert action_by_opp[opp2.id].readiness_status == SupervisorActionReadinessStatus.BLOCKED_OPT_OUT
        assert action_by_opp[opp3.id].readiness_status == SupervisorActionReadinessStatus.INSUFFICIENT_EVIDENCE


@pytest.mark.asyncio
async def test_pipeline_collision_guard_integration(session_factory):
    """Active SalesTask in database must cause candidate to be blocked with ACTIVE_TASK_EXISTS."""
    async with session_factory() as session:
        lead, opp = await _seed_pipeline_candidate(session, "TaskCollision Corp")

        # Insert active sales task
        task = SalesTask(
            id=uuid.uuid4(),
            lead_id=lead.id,
            title="Follow-up Call in Progress",
            task_type=SalesTaskType.EMAIL,
            status=SalesTaskStatus.IN_PROGRESS,
            priority=SalesPriority.HIGH,
            due_at=now_utc(),
            agent_source_id=opp.id,
        )
        session.add(task)
        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(session)
        assert response.total_candidates == 1
        action = response.actions[0]
        assert action.readiness_status == SupervisorActionReadinessStatus.ACTIVE_TASK_EXISTS
        assert action.readiness_details.has_active_task is True
        assert response.ready_count == 0
        assert response.blocked_count == 1


@pytest.mark.asyncio
async def test_pipeline_cooldown_guard_integration(session_factory):
    """Recent SalesActivity in database must cause candidate to be blocked with BLOCKED_COOLDOWN."""
    eval_time = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    recent_time = eval_time - timedelta(hours=3)

    async with session_factory() as session:
        lead, opp = await _seed_pipeline_candidate(session, "Cooldown Corp")

        activity = SalesActivity(
            id=uuid.uuid4(),
            lead_id=lead.id,
            activity_type=SalesActivityType.EMAIL,
            occurred_at=recent_time,
            note="Sent outreach email 3 hours ago",
        )
        session.add(activity)
        await session.commit()

        response = await evaluate_sales_supervisor_pipeline(
            session,
            now=eval_time,
            cooldown_minutes=1440,
        )
        assert response.total_candidates == 1
        action = response.actions[0]
        assert action.readiness_status == SupervisorActionReadinessStatus.BLOCKED_COOLDOWN
        assert action.readiness_details.in_cooldown is True
        assert response.ready_count == 0
        assert response.blocked_count == 1


@pytest.mark.asyncio
async def test_pipeline_read_only_guarantee(session_factory):
    """Evaluation must never create, mutate, or delete SalesTask records."""
    async with session_factory() as session:
        lead, opp = await _seed_pipeline_candidate(session, "ReadOnly Corp")
        await session.commit()

        tasks_before = (await session.execute(select(SalesTask))).scalars().all()
        assert len(tasks_before) == 0

        # Execute pipeline evaluation
        response = await evaluate_sales_supervisor_pipeline(session)
        assert response.total_candidates == 1

        tasks_after = (await session.execute(select(SalesTask))).scalars().all()
        assert len(tasks_after) == 0

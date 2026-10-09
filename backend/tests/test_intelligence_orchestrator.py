import pytest
import uuid
from datetime import datetime, timezone
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.entities import (
    Lead,
    LeadIntelligenceOpportunity,
    ContactIntelligenceProfile,
    SeniorityLevel,
    FunctionalArea,
    BuyingInfluenceLevel,
    ConfidenceLevel,
    SourceType,
    SalesTask,
    SalesTaskType,
    SalesTaskStatus,
    SalesPriority,
    SalesActivity,
    SalesActivityType,
    SalesActivityOutcome,
    SalesStatus,
)
import app.services.intelligence_orchestrator as orchestrator_module
from app.services.intelligence_orchestrator import run_lead_intelligence_orchestration
from app.config import settings
from app.main import app, get_db

AUTH_HEADERS = {"Authorization": "Bearer api-test-token"}


# ----------------------------------------------------
# 1. Service-Level Orchestration Tests
# ----------------------------------------------------
@pytest.mark.asyncio
async def test_orchestrate_lead_not_found(session_factory):
    async with session_factory() as session:
        fake_id = uuid.uuid4()
        res = await run_lead_intelligence_orchestration(session, fake_id)
        assert res is None


@pytest.mark.asyncio
async def test_orchestrate_lead_with_no_opportunities(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Empty Lead Corp", currency="USD")
        session.add(lead)
        await session.commit()

        res = await run_lead_intelligence_orchestration(session, lead.id)
        assert res is not None
        assert res.lead_id == lead.id
        assert res.status == "NO_OPPORTUNITY"
        assert res.summary.total_opportunities == 0
        assert len(res.opportunities) == 0
        assert f"lead:{lead.id}" in res.decision_trace
        assert "status:NO_OPPORTUNITY" in res.decision_trace


@pytest.mark.asyncio
async def test_orchestrate_opportunity_with_no_contacts(session_factory, monkeypatch):
    async with session_factory() as session:
        lead = Lead(company_name="No Contacts Inc", currency="USD")
        session.add(lead)
        await session.flush()

        opp = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="SEO Opportunity",
            opportunity_type="SEO",
            score=80,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add(opp)
        await session.commit()

    async def mock_no_contacts(session, opp_id, limit=None):
        return {"opportunity_id": opp_id, "contacts": []}

    monkeypatch.setattr(orchestrator_module, "get_matched_contacts_for_opportunity", mock_no_contacts)

    async with session_factory() as session:
        res = await run_lead_intelligence_orchestration(session, lead.id)
        assert res is not None
        assert res.status == "NO_CONTACT"
        assert res.summary.total_opportunities == 1
        assert res.summary.no_contact_count == 1
        assert len(res.opportunities) == 1
        opp_res = res.opportunities[0]
        assert opp_res.status == "NO_CONTACT"
        assert opp_res.feedback is not None
        assert opp_res.feedback["total_executions"] == 0


@pytest.mark.asyncio
async def test_orchestrate_opportunity_no_contacts_with_existing_feedback(session_factory, monkeypatch):
    async with session_factory() as session:
        lead = Lead(company_name="Historical Feedback Corp", currency="USD")
        session.add(lead)
        await session.flush()

        opp = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Legacy Service Opportunity",
            opportunity_type="LEGACY",
            score=75,
            priority="MEDIUM",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.MEDIUM,
        )
        session.add(opp)
        await session.flush()

        # Historical activity and completed task for this opportunity
        act = SalesActivity(
            lead_id=lead.id,
            activity_type=SalesActivityType.CALL,
            note="Historical discovery call completed with positive response",
            outcome=SalesActivityOutcome.MEETING_BOOKED,
        )
        session.add(act)
        await session.flush()

        task = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Historical Discovery Call",
            status=SalesTaskStatus.COMPLETED,
            completed_at=datetime.now(timezone.utc),
            due_at=datetime.now(timezone.utc),
            agent_source_type="SALES_OPPORTUNITY",
            agent_source_id=opp.id,
            related_activity_id=act.id,
        )
        session.add(task)
        await session.commit()

    async def mock_no_contacts(session, opp_id, limit=None):
        return {"opportunity_id": opp_id, "contacts": []}

    monkeypatch.setattr(orchestrator_module, "get_matched_contacts_for_opportunity", mock_no_contacts)

    async with session_factory() as session:
        res = await run_lead_intelligence_orchestration(session, lead.id)
        assert res is not None
        assert res.status == "NO_CONTACT"
        assert len(res.opportunities) == 1
        opp_res = res.opportunities[0]
        assert opp_res.status == "NO_CONTACT"
        assert opp_res.feedback is not None
        assert opp_res.feedback["total_executions"] == 1
        assert opp_res.feedback["positive_count"] == 1
        assert opp_res.feedback["positive_rate"] == 100.0


@pytest.mark.asyncio
async def test_orchestrate_single_opportunity_end_to_end(session_factory):
    async with session_factory() as session:
        lead = Lead(
            company_name="Apex Digital",
            currency="USD",
            decision_maker="Sarah Connor",
            job_title="VP of Marketing",
            department="Marketing",
            email="sarah@apex.com",
            phone="5551234567",
        )
        session.add(lead)
        await session.flush()

        profile = ContactIntelligenceProfile(
            contact_id=lead.id,
            decision_maker_score=95,
            functional_area=FunctionalArea.MARKETING,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        opp = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Enterprise SEO Package",
            opportunity_type="SEO",
            score=90,
            priority="CRITICAL",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add_all([profile, opp])
        await session.commit()

        res = await run_lead_intelligence_orchestration(session, lead.id)
        assert res is not None
        assert res.lead_id == lead.id
        assert res.status == "TASK_CREATED"
        assert res.summary.total_opportunities == 1
        assert res.summary.tasks_created == 1
        assert res.summary.tasks_reused == 0

        opp_res = res.opportunities[0]
        assert opp_res.opportunity_id == opp.id
        assert opp_res.status == "TASK_CREATED"
        assert opp_res.matched_contacts_count >= 1
        assert opp_res.recommended_contact is not None
        assert str(opp_res.recommended_contact["contact_id"]) == str(lead.id)
        assert opp_res.recommendation is not None
        assert opp_res.recommendation["recommended_action"] == "DISCOVERY_CALL"
        assert opp_res.sales_task is not None
        assert opp_res.sales_task["created"] is True
        assert opp_res.feedback is not None

        # Check decision trace
        assert f"lead:{lead.id}" in res.decision_trace
        assert f"opportunity:{opp.id}" in res.decision_trace
        assert f"contact:{lead.id}" in res.decision_trace
        assert "action:DISCOVERY_CALL" in res.decision_trace


@pytest.mark.asyncio
async def test_orchestrate_all_task_created(session_factory):
    async with session_factory() as session:
        # Lead A and Lead B in the same company
        lead_a = Lead(
            company_name="All Created Corp",
            currency="USD",
            decision_maker="Alice Cooper",
            job_title="VP Sales",
            department="Sales",
            email="alice@allcreated.com",
            phone="1234567890",
        )
        lead_b = Lead(
            company_name="All Created Corp",
            currency="USD",
            decision_maker="Bob Dylan",
            job_title="VP Marketing",
            department="Marketing",
            email="bob@allcreated.com",
            phone="0987654321",
        )
        session.add_all([lead_a, lead_b])
        await session.flush()

        prof_a = ContactIntelligenceProfile(
            contact_id=lead_a.id,
            decision_maker_score=95,
            functional_area=FunctionalArea.SALES,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        prof_b = ContactIntelligenceProfile(
            contact_id=lead_b.id,
            decision_maker_score=95,
            functional_area=FunctionalArea.MARKETING,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        opp1 = LeadIntelligenceOpportunity(
            lead_id=lead_a.id,
            title="Sales CRM Modernization",
            opportunity_type="SALES",
            score=95,
            priority="CRITICAL",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        opp2 = LeadIntelligenceOpportunity(
            lead_id=lead_a.id,
            title="Marketing Automation Retainer",
            opportunity_type="MARKETING",
            score=90,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add_all([prof_a, prof_b, opp1, opp2])
        await session.commit()

        res = await run_lead_intelligence_orchestration(session, lead_a.id)
        assert res is not None
        assert res.summary.total_opportunities == 2
        assert res.summary.tasks_created == 2
        assert res.summary.tasks_reused == 0
        assert res.summary.failed_count == 0
        assert res.status == "TASK_CREATED"


@pytest.mark.asyncio
async def test_orchestrate_idempotent_task_reuse(session_factory):
    async with session_factory() as session:
        lead = Lead(
            company_name="Idempotent Co",
            currency="USD",
            decision_maker="John Smith",
            job_title="Director of IT",
            department="Technology",
            email="john@idempotent.com",
            phone="1112223333",
        )
        session.add(lead)
        await session.flush()

        profile = ContactIntelligenceProfile(
            contact_id=lead.id,
            decision_maker_score=85,
            functional_area=FunctionalArea.TECHNOLOGY,
            seniority_level=SeniorityLevel.LEADERSHIP,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        opp = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Tech Infrastructure Modernization",
            opportunity_type="TECHNOLOGY",
            score=88,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add_all([profile, opp])
        await session.commit()

        # First run -> Task Created
        res1 = await run_lead_intelligence_orchestration(session, lead.id)
        assert res1.status == "TASK_CREATED"
        assert res1.summary.tasks_created == 1
        task_id_1 = res1.opportunities[0].sales_task["task_id"]

        # Second run -> Task Reused (Idempotent, no duplicate task created)
        res2 = await run_lead_intelligence_orchestration(session, lead.id)
        assert res2.status == "TASK_REUSED"
        assert res2.summary.tasks_created == 0
        assert res2.summary.tasks_reused == 1
        task_id_2 = res2.opportunities[0].sales_task["task_id"]
        assert task_id_1 == task_id_2

        # Verify only 1 task in database
        tasks_stmt = select(SalesTask).where(SalesTask.lead_id == lead.id)
        tasks_in_db = list((await session.execute(tasks_stmt)).scalars().all())
        assert len(tasks_in_db) == 1


@pytest.mark.asyncio
async def test_orchestrate_all_failed_opportunities(session_factory, monkeypatch):
    async with session_factory() as session:
        lead = Lead(company_name="Fail Corp", currency="USD", decision_maker="Dave")
        session.add(lead)
        await session.flush()

        opp1 = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Opp Fail 1",
            opportunity_type="SALES",
            score=90,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        opp2 = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Opp Fail 2",
            opportunity_type="SEO",
            score=80,
            priority="MEDIUM",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.MEDIUM,
        )
        session.add_all([opp1, opp2])
        await session.commit()

    async def mock_matching_fail(session, opp_id, limit=None):
        raise RuntimeError("Deterministic matching database timeout")

    monkeypatch.setattr(orchestrator_module, "get_matched_contacts_for_opportunity", mock_matching_fail)

    async with session_factory() as session:
        res = await run_lead_intelligence_orchestration(session, lead.id)
        assert res is not None
        assert res.status == "FAILED"
        assert res.summary.total_opportunities == 2
        assert res.summary.failed_count == 2
        assert res.summary.tasks_created == 0
        assert res.opportunities[0].status == "FAILED"
        assert "Deterministic matching database timeout" in res.opportunities[0].error
        assert res.opportunities[1].status == "FAILED"


@pytest.mark.asyncio
async def test_orchestrate_feedback_failure_after_task_sync_leads_to_failed_status(session_factory, monkeypatch):
    """
    Verifies that when task synchronization succeeds and creates a task in DB,
    but feedback collection subsequently fails with an exception:
    1. The opportunity is marked FAILED.
    2. summary.tasks_created accurately reflects that 1 task was created.
    3. summary.failed_count is 1.
    4. For a single-opportunity lead, overall status is FAILED.
    5. Partial task metadata is preserved in sales_task on the failed opportunity.
    """
    async with session_factory() as session:
        lead = Lead(company_name="Feedback Fail Corp", currency="USD", decision_maker="Dave", job_title="VP", email="dave@ff.com")
        session.add(lead)
        await session.flush()

        profile = ContactIntelligenceProfile(
            contact_id=lead.id,
            decision_maker_score=90,
            functional_area=FunctionalArea.SALES,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        opp = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Opp Feedback Fail",
            opportunity_type="SALES",
            score=90,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add_all([profile, opp])
        await session.commit()

    async def mock_feedback_fail(session, opp_id):
        raise ValueError("Feedback aggregation service error")

    monkeypatch.setattr(orchestrator_module, "get_opportunity_sales_feedback", mock_feedback_fail)

    async with session_factory() as session:
        res = await run_lead_intelligence_orchestration(session, lead.id)
        assert res is not None
        assert res.status == "FAILED"
        assert res.summary.failed_count == 1
        assert res.summary.tasks_created == 1
        assert res.opportunities[0].status == "FAILED"
        assert "Feedback aggregation service error" in res.opportunities[0].error
        assert res.opportunities[0].sales_task is not None
        assert res.opportunities[0].sales_task["created"] is True


@pytest.mark.asyncio
async def test_orchestrate_multi_opportunity_partial_feedback_failure(session_factory, monkeypatch):
    """
    Verifies that when Opp 1 succeeds and Opp 2 fails at feedback after task creation:
    1. Overall status is PARTIAL.
    2. tasks_created is 2.
    3. failed_count is 1.
    4. Opp 1 is TASK_CREATED and Opp 2 is FAILED.
    """
    async with session_factory() as session:
        lead_a = Lead(
            company_name="Multi Feedback Fail Corp",
            currency="USD",
            decision_maker="Alice",
            job_title="VP Sales",
            department="Sales",
            email="alice@mff.com",
            phone="111",
        )
        lead_b = Lead(
            company_name="Multi Feedback Fail Corp",
            currency="USD",
            decision_maker="Bob",
            job_title="VP Marketing",
            department="Marketing",
            email="bob@mff.com",
            phone="222",
        )
        session.add_all([lead_a, lead_b])
        await session.flush()

        prof_a = ContactIntelligenceProfile(
            contact_id=lead_a.id,
            decision_maker_score=90,
            functional_area=FunctionalArea.SALES,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        prof_b = ContactIntelligenceProfile(
            contact_id=lead_b.id,
            decision_maker_score=90,
            functional_area=FunctionalArea.MARKETING,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        opp1 = LeadIntelligenceOpportunity(
            lead_id=lead_a.id,
            title="Opp 1 Success",
            opportunity_type="SALES",
            score=95,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        opp2 = LeadIntelligenceOpportunity(
            lead_id=lead_a.id,
            title="Opp 2 Feedback Fail",
            opportunity_type="MARKETING",
            score=85,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add_all([prof_a, prof_b, opp1, opp2])
        await session.commit()

    original_feedback = orchestrator_module.get_opportunity_sales_feedback

    async def mock_feedback_selective(session, opp_id):
        if opp_id == opp2.id:
            raise ValueError("Selective feedback failure on opp2")
        return await original_feedback(session, opp_id)

    monkeypatch.setattr(orchestrator_module, "get_opportunity_sales_feedback", mock_feedback_selective)

    async with session_factory() as session:
        res = await run_lead_intelligence_orchestration(session, lead_a.id)
        assert res is not None
        assert res.status == "PARTIAL"
        assert res.summary.total_opportunities == 2
        assert res.summary.tasks_created == 2
        assert res.summary.failed_count == 1
        assert res.opportunities[0].status == "TASK_CREATED"
        assert res.opportunities[1].status == "FAILED"


@pytest.mark.asyncio
async def test_orchestrate_mixed_outcomes_created_and_reused(session_factory):
    async with session_factory() as session:
        lead_a = Lead(
            company_name="Mixed Corp",
            currency="USD",
            decision_maker="Charlie",
            job_title="VP Sales",
            department="Sales",
            email="charlie@mixed.com",
            phone="888",
        )
        lead_b = Lead(
            company_name="Mixed Corp",
            currency="USD",
            decision_maker="Dana",
            job_title="VP Marketing",
            department="Marketing",
            email="dana@mixed.com",
            phone="777",
        )
        session.add_all([lead_a, lead_b])
        await session.flush()

        prof_a = ContactIntelligenceProfile(
            contact_id=lead_a.id,
            decision_maker_score=90,
            functional_area=FunctionalArea.SALES,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        prof_b = ContactIntelligenceProfile(
            contact_id=lead_b.id,
            decision_maker_score=90,
            functional_area=FunctionalArea.MARKETING,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        opp1 = LeadIntelligenceOpportunity(
            lead_id=lead_a.id,
            title="Opp 1 Existing Task",
            opportunity_type="SALES",
            score=95,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        opp2 = LeadIntelligenceOpportunity(
            lead_id=lead_a.id,
            title="Opp 2 New Task",
            opportunity_type="MARKETING",
            score=85,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add_all([prof_a, prof_b, opp1, opp2])
        await session.flush()

        # Pre-create a task for opp1
        task1 = SalesTask(
            lead_id=lead_a.id,
            task_type=SalesTaskType.CALL,
            title="Discovery Call: Charlie",
            status=SalesTaskStatus.OPEN,
            due_at=datetime.now(timezone.utc),
            agent_source_type="SALES_OPPORTUNITY",
            agent_source_id=opp1.id,
            agent_source_action_id=f"contact_recommendation:{lead_a.id}:DISCOVERY_CALL",
        )
        session.add(task1)
        await session.commit()

        res = await run_lead_intelligence_orchestration(session, lead_a.id)
        assert res is not None
        assert res.status == "PARTIAL"
        assert res.summary.total_opportunities == 2
        assert res.summary.tasks_reused == 1
        assert res.summary.tasks_created == 1
        assert res.opportunities[0].status == "TASK_REUSED"
        assert res.opportunities[1].status == "TASK_CREATED"


@pytest.mark.asyncio
async def test_orchestrate_mixed_outcomes_created_and_failed(session_factory, monkeypatch):
    async with session_factory() as session:
        lead = Lead(company_name="Mixed Fail", currency="USD", decision_maker="Dan", email="dan@mix.com", phone="123")
        session.add(lead)
        await session.flush()

        profile = ContactIntelligenceProfile(
            contact_id=lead.id,
            decision_maker_score=90,
            functional_area=FunctionalArea.TECHNOLOGY,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        opp1 = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Opp 1 Success",
            opportunity_type="TECH",
            score=95,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        opp2 = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Opp 2 Fail",
            opportunity_type="FAIL",
            score=80,
            priority="LOW",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.LOW,
        )
        session.add_all([profile, opp1, opp2])
        await session.commit()

    original_match = orchestrator_module.get_matched_contacts_for_opportunity

    async def mock_match_selective(session, opp_id, limit=None):
        if opp_id == opp2.id:
            raise RuntimeError("Opp 2 mock failure")
        return await original_match(session, opp_id, limit=limit)

    monkeypatch.setattr(orchestrator_module, "get_matched_contacts_for_opportunity", mock_match_selective)

    async with session_factory() as session:
        res = await run_lead_intelligence_orchestration(session, lead.id)
        assert res is not None
        assert res.status == "PARTIAL"
        assert res.summary.tasks_created == 1
        assert res.summary.failed_count == 1


@pytest.mark.asyncio
async def test_orchestrate_mixed_outcomes_reused_and_no_contact(session_factory, monkeypatch):
    async with session_factory() as session:
        lead = Lead(company_name="Reused NoContact Corp", currency="USD", decision_maker="Eve", email="eve@rnc.com", phone="111")
        session.add(lead)
        await session.flush()

        profile = ContactIntelligenceProfile(
            contact_id=lead.id,
            decision_maker_score=90,
            functional_area=FunctionalArea.SALES,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        opp1 = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Opp 1 Reused",
            opportunity_type="SALES",
            score=90,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        opp2 = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Opp 2 No Contact",
            opportunity_type="SEO",
            score=80,
            priority="LOW",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.LOW,
        )
        session.add_all([profile, opp1, opp2])
        await session.commit()

        # Add a pre-existing task for opp1
        task1 = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Call",
            status=SalesTaskStatus.OPEN,
            due_at=datetime.now(timezone.utc),
            agent_source_type="SALES_OPPORTUNITY",
            agent_source_id=opp1.id,
            agent_source_action_id=f"contact_recommendation:{lead.id}:DISCOVERY_CALL",
        )
        session.add(task1)
        await session.commit()

    original_match = orchestrator_module.get_matched_contacts_for_opportunity

    async def mock_match_opp2_empty(session, opp_id, limit=None):
        if opp_id == opp2.id:
            return {"opportunity_id": opp_id, "contacts": []}
        return await original_match(session, opp_id, limit=limit)

    monkeypatch.setattr(orchestrator_module, "get_matched_contacts_for_opportunity", mock_match_opp2_empty)

    async with session_factory() as session:
        res = await run_lead_intelligence_orchestration(session, lead.id)
        assert res is not None
        assert res.status == "PARTIAL"
        assert res.summary.tasks_reused == 1
        assert res.summary.no_contact_count == 1
        assert res.opportunities[0].status == "TASK_REUSED"
        assert res.opportunities[1].status == "NO_CONTACT"


@pytest.mark.asyncio
async def test_orchestrate_mixed_outcomes_no_contact_and_no_recommendation(session_factory, monkeypatch):
    async with session_factory() as session:
        lead = Lead(company_name="Mix Rec NoContact", currency="USD")
        session.add(lead)
        await session.flush()

        profile = ContactIntelligenceProfile(
            contact_id=lead.id,
            decision_maker_score=10,
            functional_area=FunctionalArea.UNKNOWN,
            seniority_level=SeniorityLevel.UNKNOWN,
            buying_influence=BuyingInfluenceLevel.UNKNOWN,
            confidence=ConfidenceLevel.LOW,
            source_type=SourceType.LEAD_RECORD,
        )
        opp1 = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Opp No Contact",
            opportunity_type="UNKNOWN1",
            score=85,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.LOW,
        )
        opp2 = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Opp No Rec",
            opportunity_type="UNKNOWN2",
            score=20,
            priority="LOW",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.LOW,
        )
        session.add_all([profile, opp1, opp2])
        await session.commit()

    original_match = orchestrator_module.get_matched_contacts_for_opportunity

    async def mock_match_opp1_empty(session, opp_id, limit=None):
        if opp_id == opp1.id:
            return {"opportunity_id": opp_id, "contacts": []}
        return await original_match(session, opp_id, limit=limit)

    monkeypatch.setattr(orchestrator_module, "get_matched_contacts_for_opportunity", mock_match_opp1_empty)

    async with session_factory() as session:
        res = await run_lead_intelligence_orchestration(session, lead.id)
        assert res is not None
        assert res.status == "PARTIAL"
        assert res.summary.no_contact_count == 1
        assert res.summary.no_recommendation_count == 1
        assert res.opportunities[0].status == "NO_CONTACT"
        assert res.opportunities[1].status == "NO_RECOMMENDATION"


@pytest.mark.asyncio
async def test_orchestrate_feedback_integration(session_factory):
    async with session_factory() as session:
        lead = Lead(
            company_name="Feedback Integration Co",
            currency="USD",
            decision_maker="David Copperfield",
            job_title="CEO",
            email="david@copper.com",
            phone="7778889999",
        )
        session.add(lead)
        await session.flush()

        profile = ContactIntelligenceProfile(
            contact_id=lead.id,
            decision_maker_score=100,
            functional_area=FunctionalArea.EXECUTIVE,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        opp = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Strategic Consulting",
            opportunity_type="STRATEGY",
            score=92,
            priority="CRITICAL",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add_all([profile, opp])
        await session.commit()

        # Pre-record a completed task and activity
        act = SalesActivity(
            lead_id=lead.id,
            activity_type=SalesActivityType.CALL,
            note="Meeting booked with David",
            outcome=SalesActivityOutcome.MEETING_BOOKED,
        )
        session.add(act)
        await session.flush()

        task = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Discovery Call: David",
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

        res = await run_lead_intelligence_orchestration(session, lead.id)
        opp_res = res.opportunities[0]
        assert opp_res.feedback is not None
        assert opp_res.feedback["total_executions"] == 1
        assert opp_res.feedback["positive_count"] == 1
        assert opp_res.feedback["positive_rate"] == 100.0


@pytest.mark.asyncio
async def test_orchestrate_partial_status_resilience(session_factory, monkeypatch):
    async with session_factory() as session:
        lead_a = Lead(
            company_name="Partial Co",
            currency="USD",
            decision_maker="Bob",
            job_title="VP Sales",
            department="Sales",
            email="bob@partial.com",
            phone="123",
        )
        session.add(lead_a)
        await session.flush()

        profile_a = ContactIntelligenceProfile(
            contact_id=lead_a.id,
            decision_maker_score=90,
            functional_area=FunctionalArea.SALES,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        opp1 = LeadIntelligenceOpportunity(
            lead_id=lead_a.id,
            title="Opp 1 Success",
            opportunity_type="SALES",
            score=90,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        opp2 = LeadIntelligenceOpportunity(
            lead_id=lead_a.id,
            title="Opp 2 Low Score No Action",
            opportunity_type="UNKNOWN_TYPE",
            score=10,
            priority="LOW",
            recommended_action="NO_ACTION",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.LOW,
        )
        session.add_all([profile_a, opp1, opp2])
        await session.commit()

    original_rec = orchestrator_module.get_contact_recommendation_for_opportunity

    async def mock_rec(session, opp_id):
        if opp_id == opp2.id:
            return {
                "opportunity_id": opp_id,
                "recommended_contact": None,
                "recommended_action": "NO_ACTION",
                "recommendation_score": 0,
            }
        return await original_rec(session, opp_id)

    monkeypatch.setattr(orchestrator_module, "get_contact_recommendation_for_opportunity", mock_rec)

    async with session_factory() as session:
        res = await run_lead_intelligence_orchestration(session, lead_a.id)
        assert res is not None
        assert res.summary.total_opportunities == 2
        assert res.status == "PARTIAL"
        assert len(res.opportunities) == 2
        assert res.opportunities[0].status == "TASK_CREATED"
        assert res.opportunities[1].status == "NO_RECOMMENDATION"


@pytest.mark.asyncio
async def test_orchestration_read_only_invariants(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Invariant Co", currency="USD", lead_score=85)
        session.add(lead)
        await session.flush()

        profile = ContactIntelligenceProfile(
            contact_id=lead.id,
            decision_maker_score=90,
            functional_area=FunctionalArea.MARKETING,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        opp = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Invariant Opp",
            opportunity_type="MARKETING",
            score=95,
            priority="CRITICAL",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add_all([profile, opp])
        await session.commit()

        # Run orchestration twice
        res1 = await run_lead_intelligence_orchestration(session, lead.id)
        res2 = await run_lead_intelligence_orchestration(session, lead.id)

        assert res1 is not None and res2 is not None
        await session.refresh(lead)
        await session.refresh(profile)
        await session.refresh(opp)

        # Invariant checks: scores and weights must not be mutated
        assert lead.lead_score == 85
        assert profile.decision_maker_score == 90
        assert opp.score == 95


# ----------------------------------------------------
# 2. HTTP API Route Tests
# ----------------------------------------------------

@pytest.mark.asyncio
async def test_api_orchestrate_lead_success(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    async with session_factory() as session:
        lead = Lead(
            company_name="API Orchestration Lead",
            currency="USD",
            decision_maker="Grace Hopper",
            job_title="Lead Architect",
            email="grace@navy.mil",
            phone="9998887777",
        )
        session.add(lead)
        await session.flush()

        profile = ContactIntelligenceProfile(
            contact_id=lead.id,
            decision_maker_score=95,
            functional_area=FunctionalArea.TECHNOLOGY,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        opp = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Compiler Modernization",
            opportunity_type="TECHNOLOGY",
            score=95,
            priority="CRITICAL",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add_all([profile, opp])
        await session.commit()
        lead_id = str(lead.id)

    app.dependency_overrides[get_db] = lambda: session_factory()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(f"/api/leads/{lead_id}/intelligence/orchestrate", headers=AUTH_HEADERS)
            assert resp.status_code == 200
            data = resp.json()
            assert data["lead_id"] == lead_id
            assert data["company_name"] == "API Orchestration Lead"
            assert data["status"] == "TASK_CREATED"
            assert len(data["opportunities"]) == 1
            assert data["summary"]["total_opportunities"] == 1
            assert len(data["decision_trace"]) > 0
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_orchestrate_lead_not_found(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    app.dependency_overrides[get_db] = lambda: session_factory()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            fake_id = str(uuid.uuid4())
            resp = await ac.post(f"/api/leads/{fake_id}/intelligence/orchestrate", headers=AUTH_HEADERS)
            assert resp.status_code == 404
            data = resp.json()
            assert data["error"]["message"] == "Lead not found"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_orchestrate_opportunity_with_zero_score(session_factory):
    """
    TASK 2: Verifies that an opportunity with score 0.0 is orchestrated deterministically
    without crashing or altering scoring rules.
    """
    async with session_factory() as session:
        lead = Lead(
            company_name="Zero Score Co",
            currency="USD",
            decision_maker="Jane Doe",
            job_title="Head of Digital",
            department="Marketing",
            email="jane@zeroscore.com",
            phone="1112223333",
        )
        session.add(lead)
        await session.flush()

        profile = ContactIntelligenceProfile(
            contact_id=lead.id,
            decision_maker_score=80,
            functional_area=FunctionalArea.DIGITAL_MARKETING,
            seniority_level=SeniorityLevel.LEADERSHIP,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        opp = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Zero Score SEO Audit",
            opportunity_type="SEO",
            score=0,
            priority="LOW",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.LOW,
        )
        session.add_all([profile, opp])
        await session.commit()

        res = await run_lead_intelligence_orchestration(session, lead.id)
        assert res is not None
        assert res.status == "TASK_CREATED"
        assert res.summary.total_opportunities == 1
        assert res.summary.tasks_created == 1
        assert len(res.opportunities) == 1

        opp_res = res.opportunities[0]
        assert opp_res.opportunity_id == opp.id
        assert opp_res.score == 0
        assert opp_res.status == "TASK_CREATED"
        assert f"score:0" in opp_res.decision_trace
        assert opp_res.sales_task is not None
        assert opp_res.sales_task["created"] is True


@pytest.mark.asyncio
async def test_orchestrate_inactive_contact_no_feedback(session_factory):
    """
    TASK 3: Verifies that an inactive contact with no contact coordinates and no feedback history
    yields NO_RECOMMENDATION and 0 feedback executions without crashing.
    """
    async with session_factory() as session:
        lead = Lead(
            company_name="Inactive Contact Co",
            currency="USD",
            decision_maker="Inactive Person",
            job_title=None,
            department=None,
            email=None,
            phone=None,
            linkedin_url=None,
            sales_status=SalesStatus.LOST,
        )
        session.add(lead)
        await session.flush()

        profile = ContactIntelligenceProfile(
            contact_id=lead.id,
            decision_maker_score=10,
            functional_area=FunctionalArea.UNKNOWN,
            seniority_level=SeniorityLevel.INDIVIDUAL_CONTRIBUTOR,
            buying_influence=BuyingInfluenceLevel.LOW,
            confidence=ConfidenceLevel.LOW,
            source_type=SourceType.LEAD_RECORD,
        )
        opp = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="General Advisory",
            opportunity_type="GENERAL",
            score=25,
            priority="LOW",
            recommended_action="NO_ACTION",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.LOW,
        )
        session.add_all([profile, opp])
        await session.commit()

        res = await run_lead_intelligence_orchestration(session, lead.id)
        assert res is not None
        assert res.status == "NO_RECOMMENDATION"
        assert res.summary.total_opportunities == 1
        assert res.summary.no_recommendation_count == 1
        assert res.summary.tasks_created == 0
        assert len(res.opportunities) == 1

        opp_res = res.opportunities[0]
        assert opp_res.status == "NO_RECOMMENDATION"
        assert opp_res.feedback is not None
        assert opp_res.feedback["total_executions"] == 0
        assert opp_res.sales_task is None


@pytest.mark.asyncio
async def test_api_orchestrate_lead_malformed_uuid(session_factory, monkeypatch):
    """
    TASK 1: Verifies that a malformed lead_id path parameter returns HTTP 422 with VALIDATION_ERROR.
    """
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    app.dependency_overrides[get_db] = lambda: session_factory()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/leads/not-a-valid-uuid-12345/intelligence/orchestrate", headers=AUTH_HEADERS)
            assert resp.status_code == 422
            data = resp.json()
            assert data["success"] is False
            assert data["error"]["code"] == "VALIDATION_ERROR"
            assert "lead_id" in str(data["error"]["details"])
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_orchestrate_multi_opportunity_task_conflict_recovery_preserves_session(session_factory, monkeypatch):
    """
    Test C: Multi-opportunity orchestration where Opportunity 1 experiences a simulated task-creation
    conflict (IntegrityError with rollback), allowing the session to remain completely valid so that
    Opportunity 2 processes successfully without session corruption.
    """
    async with session_factory() as session:
        lead = Lead(
            company_name="Multi Conflict Corp",
            currency="USD",
            decision_maker="Dana Chief",
            job_title="VP Sales",
            department="Sales",
            email="dana@multiconflict.com",
            phone="4445556666"
        )
        session.add(lead)
        await session.flush()

        profile = ContactIntelligenceProfile(
            contact_id=lead.id,
            decision_maker_score=90,
            functional_area=FunctionalArea.SALES,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        opp1 = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Opp 1 Conflict Fail",
            opportunity_type="SALES",
            score=95,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        opp2 = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Opp 2 Normal Success",
            opportunity_type="MARKETING",
            score=85,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add_all([profile, opp1, opp2])
        await session.commit()

    # Strengthened Test: Exercise the PRODUCTION synchronize_recommended_contact_task recovery handler.
    # We do NOT mock synchronize_recommended_contact_task. Instead, we simulate an unrecoverable IntegrityError
    # during Opportunity 1's commit inside the production synchronize function.
    async with session_factory() as session:
        original_commit = session.commit
        opp1_failed = False

        async def mock_commit_opp1_integrity_failure():
            nonlocal opp1_failed
            if not opp1_failed:
                opp1_failed = True
                # Simulate a DB constraint violation during commit
                raise IntegrityError("duplicate key", params=None, orig=Exception("uq_sales_tasks_agent_source_action"))
            return await original_commit()

        monkeypatch.setattr(session, "commit", mock_commit_opp1_integrity_failure)

        # Run orchestration with production synchronize_recommended_contact_task
        res = await run_lead_intelligence_orchestration(session, lead.id)
        assert res is not None
        assert res.status == "PARTIAL"
        assert res.summary.total_opportunities == 2
        assert res.summary.failed_count == 1
        assert res.summary.tasks_created == 1
        assert res.opportunities[0].status == "FAILED"
        assert res.opportunities[0].error_code == "DATABASE_INTEGRITY_ERROR"
        assert res.opportunities[0].failed_phase == "TASK_SYNCHRONIZATION"
        assert res.opportunities[0].retryable is False
        assert res.opportunities[1].status == "TASK_CREATED"


@pytest.mark.asyncio
async def test_orchestrate_multi_opportunity_concurrent_task_conflict_auto_recovered(session_factory, monkeypatch):
    """
    Test: Multi-opportunity orchestration where Opportunity 1 experiences a concurrent task insertion.
    The production synchronize_recommended_contact_task IntegrityError handler catches the conflict,
    rolls back, recovers the competing task as reused=True, and Opportunity 2 completes normally.
    """
    async with session_factory() as session:
        lead = Lead(
            company_name="Auto Recovery Corp",
            currency="USD",
            decision_maker="Dan Auto",
            job_title="COO",
            email="dan@autorecover.com",
            phone="1112223333"
        )
        session.add(lead)
        await session.flush()

        profile = ContactIntelligenceProfile(
            contact_id=lead.id,
            decision_maker_score=90,
            functional_area=FunctionalArea.OPERATIONS,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        opp1 = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Opp 1 Concurrent Opp",
            opportunity_type="OPERATIONS",
            score=95,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        opp2 = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Opp 2 Normal Opp",
            opportunity_type="TECHNOLOGY",
            score=80,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add_all([profile, opp1, opp2])
        await session.commit()

    async with session_factory() as session:
        original_commit = session.commit
        opp1_recovered = False
        competing_task_id = None

        async def mock_commit_with_concurrent_insert():
            nonlocal opp1_recovered, competing_task_id
            if not opp1_recovered:
                opp1_recovered = True
                # In a separate session, simulate a competing worker committing the task first
                async with session_factory() as competing_session:
                    action_identity = f"contact_recommendation:{lead.id}:DISCOVERY_CALL"
                    competing_task = SalesTask(
                        lead_id=lead.id,
                        task_type=SalesTaskType.CALL,
                        title="Discovery Call: Dan Auto — Opp 1",
                        description="Competitively created task",
                        status=SalesTaskStatus.OPEN,
                        priority=SalesPriority.HIGH,
                        due_at=datetime.now(timezone.utc),
                        created_by_agent=True,
                        agent_source_type="SALES_OPPORTUNITY",
                        agent_source_id=opp1.id,
                        agent_source_action_id=action_identity,
                        agent_evidence_refs=[f"contact:{lead.id}", f"opportunity:{opp1.id}"]
                    )
                    competing_session.add(competing_task)
                    await competing_session.commit()
                    competing_task_id = competing_task.id
                raise IntegrityError("duplicate key", params=None, orig=Exception("uq_sales_tasks_agent_source_action"))
            return await original_commit()

        monkeypatch.setattr(session, "commit", mock_commit_with_concurrent_insert)

        res = await run_lead_intelligence_orchestration(session, lead.id)
        assert res is not None
        assert res.status == "PARTIAL"
        assert res.summary.total_opportunities == 2
        assert res.summary.failed_count == 0
        assert res.summary.tasks_reused == 1
        assert res.summary.tasks_created == 1
        assert res.opportunities[0].status == "TASK_REUSED"
        assert res.opportunities[0].sales_task["task_id"] == str(competing_task_id)
        assert res.opportunities[1].status == "TASK_CREATED"


@pytest.mark.asyncio
async def test_orchestrate_success_includes_timing_and_null_failed_phase(session_factory):
    """
    Test A & D: Successful orchestration includes monotonic execution timings
    (total duration_ms and per-stage timings) and sets failed_phase/error_code/retryable to None.
    """
    async with session_factory() as session:
        lead = Lead(
            company_name="Timing Diagnostics Corp",
            currency="USD",
            decision_maker="Alice Timing",
            job_title="VP Engineering",
            email="alice@timing.com",
            phone="1234567890"
        )
        session.add(lead)
        await session.flush()

        profile = ContactIntelligenceProfile(
            contact_id=lead.id,
            decision_maker_score=85,
            functional_area=FunctionalArea.TECHNOLOGY,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        opp = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Timing Tech Opp",
            opportunity_type="TECHNOLOGY",
            score=90,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add_all([profile, opp])
        await session.commit()

        res = await run_lead_intelligence_orchestration(session, lead.id)
        assert res is not None
        assert res.status == "TASK_CREATED"
        assert res.duration_ms is not None
        assert res.duration_ms >= 0.0

        assert len(res.opportunities) == 1
        opp_res = res.opportunities[0]
        assert opp_res.failed_phase is None
        assert opp_res.error_code is None
        assert opp_res.retryable is None
        assert opp_res.error is None
        assert opp_res.duration_ms is not None
        assert opp_res.duration_ms >= 0.0
        assert opp_res.stage_durations_ms is not None
        assert "CONTACT_MATCHING" in opp_res.stage_durations_ms
        assert "RECOMMENDATION_GENERATION" in opp_res.stage_durations_ms
        assert "TASK_SYNCHRONIZATION" in opp_res.stage_durations_ms
        assert "FEEDBACK_COLLECTION" in opp_res.stage_durations_ms
        for stage, duration in opp_res.stage_durations_ms.items():
            assert duration >= 0.0


@pytest.mark.asyncio
async def test_orchestrate_failure_diagnostics_identifies_stages_and_redacts_secrets(session_factory, monkeypatch):
    """
    Test B: Simulated stage failures identify the correct failed_phase, produce structured
    error_code / retryable flags, and sanitize sensitive information like credentials/tokens.
    """
    import app.services.intelligence_orchestrator as orch_mod

    async with session_factory() as session:
        lead = Lead(
            company_name="Redaction Corp",
            currency="USD",
            decision_maker="Bob Redact",
            job_title="CTO",
            email="bob@redact.com",
            phone="1234567890"
        )
        session.add(lead)
        await session.flush()

        profile = ContactIntelligenceProfile(
            contact_id=lead.id,
            decision_maker_score=85,
            functional_area=FunctionalArea.TECHNOLOGY,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        opp = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Redaction Opp",
            opportunity_type="TECHNOLOGY",
            score=90,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add_all([profile, opp])
        await session.commit()

        # 1. Failure in CONTACT_MATCHING with sensitive credentials in exception message
        async def mock_match_fail(session, opp_id):
            raise Exception("DB Auth failed: postgresql://admin:super_secret_password_123@db.prod.internal:5432/leads?api_key=token_xyz_999")

        monkeypatch.setattr(orch_mod, "get_matched_contacts_for_opportunity", mock_match_fail)

        res1 = await run_lead_intelligence_orchestration(session, lead.id)
        assert res1 is not None
        assert res1.status == "FAILED"
        opp1_res = res1.opportunities[0]
        assert opp1_res.status == "FAILED"
        assert opp1_res.failed_phase == "CONTACT_MATCHING"
        assert opp1_res.error_code == "CONTACT_MATCH_ERROR"
        assert opp1_res.retryable is False
        # Sensitive credentials MUST NOT be in error, decision_trace, or evidence
        assert "super_secret_password_123" not in opp1_res.error
        assert "token_xyz_999" not in opp1_res.error
        assert "super_secret_password_123" not in str(res1.decision_trace)
        assert "token_xyz_999" not in str(res1.evidence)
        assert "admin:***@" in opp1_res.error
        assert "api_key=***" in opp1_res.error

        # 2. Failure in RECOMMENDATION_GENERATION with ValueError
        monkeypatch.undo()
        async def mock_rec_fail(session, opp_id):
            raise ValueError("Invalid recommendation parameters")

        monkeypatch.setattr(orch_mod, "get_contact_recommendation_for_opportunity", mock_rec_fail)
        res2 = await run_lead_intelligence_orchestration(session, lead.id)
        assert res2 is not None
        opp2_res = res2.opportunities[0]
        assert opp2_res.status == "FAILED"
        assert opp2_res.failed_phase == "RECOMMENDATION_GENERATION"
        assert opp2_res.error_code == "DATA_VALIDATION_ERROR"
        assert opp2_res.retryable is False


@pytest.mark.asyncio
async def test_orchestrate_retryable_classification_transient_vs_permanent(session_factory, monkeypatch):
    """
    Test C: Transient errors (TimeoutError, OperationalError, ConnectionError) are marked retryable=True,
    permanent errors (IntegrityError, ValueError) are marked retryable=False, and no automatic retry is executed.
    """
    import app.services.intelligence_orchestrator as orch_mod
    from sqlalchemy.exc import OperationalError

    async with session_factory() as session:
        lead = Lead(
            company_name="Retryable Corp",
            currency="USD",
            decision_maker="Charlie Retry",
            job_title="CIO",
            email="charlie@retry.com",
            phone="1234567890"
        )
        session.add(lead)
        await session.flush()

        profile = ContactIntelligenceProfile(
            contact_id=lead.id,
            decision_maker_score=85,
            functional_area=FunctionalArea.TECHNOLOGY,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        opp = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Retry Opp",
            opportunity_type="TECHNOLOGY",
            score=90,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add_all([profile, opp])
        await session.commit()

        call_count = 0

        # 1. Transient TimeoutError
        async def mock_timeout(session, opp_id):
            nonlocal call_count
            call_count += 1
            raise TimeoutError("External microservice RPC timed out")

        monkeypatch.setattr(orch_mod, "get_contact_recommendation_for_opportunity", mock_timeout)
        res = await run_lead_intelligence_orchestration(session, lead.id)
        assert res is not None
        opp_res = res.opportunities[0]
        assert opp_res.status == "FAILED"
        assert opp_res.failed_phase == "RECOMMENDATION_GENERATION"
        assert opp_res.error_code == "TIMEOUT_ERROR"
        assert opp_res.retryable is True
        # Verify no automatic retry occurred
        assert call_count == 1

        # 2. Transient OperationalError (DB connection dropped)
        monkeypatch.undo()
        async def mock_operational_error(session, opp_id):
            raise OperationalError("connection lost", params=None, orig=Exception("server closed connection unexpectedly"))

        monkeypatch.setattr(orch_mod, "synchronize_recommended_contact_task", mock_operational_error)
        res_op = await run_lead_intelligence_orchestration(session, lead.id)
        assert res_op is not None
        opp_res_op = res_op.opportunities[0]
        assert opp_res_op.status == "FAILED"
        assert opp_res_op.failed_phase == "TASK_SYNCHRONIZATION"
        assert opp_res_op.error_code == "DATABASE_OPERATIONAL_ERROR"
        assert opp_res_op.retryable is True


@pytest.mark.asyncio
async def test_orchestrate_no_opportunity_has_duration_and_clean_response(session_factory):
    """
    Test D: When a lead has no opportunities, response timing is populated and valid.
    """
    async with session_factory() as session:
        lead = Lead(
            company_name="Zero Opp Corp",
            currency="USD",
            decision_maker="Zara Zero",
            job_title="Founder",
            email="zara@zero.com",
            phone="1234567890"
        )
        session.add(lead)
        await session.commit()

        res = await run_lead_intelligence_orchestration(session, lead.id)
        assert res is not None
        assert res.status == "NO_OPPORTUNITY"
        assert res.duration_ms is not None
        assert res.duration_ms >= 0.0
        assert res.opportunities == []
        assert res.summary.total_opportunities == 0


def test_sanitize_error_message_all_formats():
    """
    Test B: Secret sanitization covers database URIs, Bearer tokens, and key-value formats
    with case insensitivity while preserving non-sensitive diagnostic context.
    """
    from app.services.intelligence_orchestrator import sanitize_error_message

    # 1. Database URIs
    uri_input = "Failed to connect to postgresql://lead_user:SuperPass999!@db-host.internal:5432/leads_db: connection refused"
    uri_out = sanitize_error_message(uri_input)
    assert "SuperPass999!" not in uri_out
    assert "postgresql://lead_user:***@db-host.internal:5432/leads_db" in uri_out
    assert "connection refused" in uri_out

    # 2. Authorization headers and Bearer tokens
    bearer_input = "HTTP 401: Request rejected with header Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.secretpayload.sig"
    bearer_out = sanitize_error_message(bearer_input)
    assert "eyJhbGciOiJIUzI1NiJ9.secretpayload.sig" not in bearer_out
    assert "Authorization: Bearer ***" in bearer_out

    simple_bearer = "Unauthorized access using Bearer my_raw_secret_token_123"
    simple_bearer_out = sanitize_error_message(simple_bearer)
    assert "my_raw_secret_token_123" not in simple_bearer_out
    assert "Bearer ***" in simple_bearer_out

    # 3. Key-value formats (case-insensitive, colon and equals separated)
    kv_inputs = [
        ("Auth error: password=P@ssw0rd123 for user admin", "P@ssw0rd123", "password=***"),
        ("Auth error: PASSWORD: MyCapitalPassword for user admin", "MyCapitalPassword", "PASSWORD: ***"),
        ("Error with API_KEY=sk-live-abcdef123456 in request", "sk-live-abcdef123456", "API_KEY=***"),
        ("Error with api-key: sk-test-999888777 in request", "sk-test-999888777", "api-key: ***"),
        ("Header X-API-KEY: custom_key_111 failed check", "custom_key_111", "X-API-KEY: ***"),
        ("client_secret='secret_client_val' is invalid", "secret_client_val", 'client_secret="***"'),
        ('token="jwt.secret.string" expired', "jwt.secret.string", 'token="***"'),
        ("pwd=my_short_pwd failure", "my_short_pwd", "pwd=***"),
    ]
    for raw_msg, secret_sub, expected_sub in kv_inputs:
        sanitized = sanitize_error_message(raw_msg)
        assert secret_sub not in sanitized
        assert expected_sub in sanitized


def test_classify_orchestration_error_transient_vs_permanent_details():
    """
    Test C: OperationalError with known transient patterns is retryable=True,
    whereas non-transient OperationalError and unknown exceptions are retryable=False.
    """
    from sqlalchemy.exc import OperationalError, IntegrityError, DatabaseError
    from app.services.intelligence_orchestrator import classify_orchestration_error

    # 1. Transient OperationalError cases
    transient_cases = [
        "deadlock detected on transaction 123",
        "could not obtain lock on relation leads",
        "lock timeout exceeded",
        "database is locked",
        "canceling statement due to lock timeout",
        "server closed the connection unexpectedly",
        "connection to server was lost",
        "connection reset by peer",
        "could not connect to server: Connection refused",
        "terminating connection due to administrator command",
        "the database system is starting up",
        "too many connections for role",
    ]
    for msg in transient_cases:
        exc = OperationalError(msg, params=None, orig=Exception(msg))
        code, retryable, safe_msg = classify_orchestration_error(exc, "TASK_SYNCHRONIZATION")
        assert code == "DATABASE_OPERATIONAL_ERROR"
        assert retryable is True, f"Expected retryable=True for: {msg}"

    # 2. Non-transient OperationalError cases (permanent / schema / auth errors)
    permanent_op_cases = [
        "syntax error at or near 'SELEKT'",
        "permission denied for table leads",
        "column lead.nonexistent does not exist",
        "relation 'missing_table' does not exist",
        "password authentication failed for user lead_agent",
        "database 'unknown_db' does not exist",
        "invalid input syntax for type uuid",
    ]
    for msg in permanent_op_cases:
        exc = OperationalError(msg, params=None, orig=Exception(msg))
        code, retryable, safe_msg = classify_orchestration_error(exc, "TASK_SYNCHRONIZATION")
        assert code == "DATABASE_OPERATIONAL_ERROR"
        assert retryable is False, f"Expected retryable=False for: {msg}"

    # 4. ConnectionError cases (distinguish transient network drops from permanent/DNS/SSL errors)
    transient_conn_cases = [
        ConnectionResetError("Connection reset by peer"),
        ConnectionRefusedError("Connection refused by target machine"),
        ConnectionAbortedError("Software caused connection abort"),
        BrokenPipeError("Broken pipe"),
        ConnectionError("connection closed by server"),
        ConnectionError("network is unreachable"),
        ConnectionError("temporary failure in name resolution"),
    ]
    for exc in transient_conn_cases:
        code, retryable, safe_msg = classify_orchestration_error(exc, "CONTACT_MATCHING")
        assert code == "CONNECTION_ERROR"
        assert retryable is True, f"Expected retryable=True for connection error: {exc}"

    permanent_conn_cases = [
        ConnectionError("getaddrinfo failed: unknown host 'invalid-lead-service.internal'"),
        ConnectionError("SSL certificate verify failed: certificate has expired"),
        ConnectionError("unsupported protocol scheme 'ftp'"),
        ConnectionError("no route to host: destination network not configured"),
    ]
    for exc in permanent_conn_cases:
        code, retryable, safe_msg = classify_orchestration_error(exc, "CONTACT_MATCHING")
        assert code == "CONNECTION_ERROR"
        assert retryable is False, f"Expected retryable=False for permanent connection error: {exc}"

    # 5. TimeoutError (transient)
    timeout_exc = TimeoutError("External RPC timeout")
    code, retryable, safe_msg = classify_orchestration_error(timeout_exc, "RECOMMENDATION_GENERATION")
    assert code == "TIMEOUT_ERROR"
    assert retryable is True

    # 6. Unknown generic Exception (conservative: non-retryable)
    unknown_exc = RuntimeError("Some internal unmapped runtime error")
    code, retryable, safe_msg = classify_orchestration_error(unknown_exc, "CONTACT_MATCHING")
    assert code == "CONTACT_MATCH_ERROR"
    assert retryable is False


@pytest.mark.asyncio
async def test_orchestrate_safe_logging_never_emits_secrets_to_logs(session_factory, monkeypatch, caplog):
    """
    Test A: When an exception containing secrets occurs, application logs record
    diagnostic context (opportunity ID, stage, error code) while ensuring secrets
    are never written in raw form to logs.
    """
    import logging
    import app.services.intelligence_orchestrator as orch_mod

    secret_db_pass = "SuperSecretDbPassword999!"
    secret_bearer = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.super_secret_jwt_signature"
    secret_api_key = "sk-live-secret_api_key_888"

    async with session_factory() as session:
        lead = Lead(
            company_name="Safe Log Corp",
            currency="USD",
            decision_maker="Eve LogSafe",
            job_title="CISO",
            email="eve@logsafe.com",
            phone="1234567890"
        )
        session.add(lead)
        await session.flush()

        profile = ContactIntelligenceProfile(
            contact_id=lead.id,
            decision_maker_score=85,
            functional_area=FunctionalArea.TECHNOLOGY,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.LEAD_RECORD,
        )
        opp = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="Safe Log Opp",
            opportunity_type="TECHNOLOGY",
            score=90,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add_all([profile, opp])
        await session.commit()

        # Simulate a failure containing multiple secret formats
        async def mock_match_fail_secrets(session, opp_id):
            raise Exception(
                f"Connection failed: postgresql://admin:{secret_db_pass}@internal.db:5432/leads?api_key={secret_api_key} "
                f"Authorization: Bearer {secret_bearer}"
            )

        monkeypatch.setattr(orch_mod, "get_matched_contacts_for_opportunity", mock_match_fail_secrets)

        with caplog.at_level(logging.INFO):
            res = await run_lead_intelligence_orchestration(session, lead.id)

        assert res is not None
        assert res.status == "FAILED"
        opp_res = res.opportunities[0]
        assert opp_res.status == "FAILED"
        assert opp_res.failed_phase == "CONTACT_MATCHING"

        # 1. Assert secrets NEVER appear in caplog
        assert secret_db_pass not in caplog.text
        assert secret_bearer not in caplog.text
        assert secret_api_key not in caplog.text

        # 2. Assert secrets NEVER appear in API response / traces / evidence
        assert secret_db_pass not in opp_res.error
        assert secret_bearer not in str(res.decision_trace)
        assert secret_api_key not in str(res.evidence)

        # 3. Assert diagnostic context is logged
        error_logs = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(error_logs) >= 1
        log_message = error_logs[0].getMessage()
        assert str(opp.id) in log_message
        assert "CONTACT_MATCHING" in log_message
        assert "CONTACT_MATCH_ERROR" in log_message
        assert "admin:***@" in log_message
        assert "Bearer ***" in log_message
        assert "api_key=***" in log_message

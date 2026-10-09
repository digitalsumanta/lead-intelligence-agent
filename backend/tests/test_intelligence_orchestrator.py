import pytest
import uuid
from datetime import datetime, timezone
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

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

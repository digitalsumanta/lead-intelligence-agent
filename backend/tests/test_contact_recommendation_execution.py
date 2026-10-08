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
)
from app.domain.contact_recommendation import (
    RecommendationPriority,
    RecommendedSalesAction,
)
from app.services.contact_recommendation_execution import (
    ACTION_TO_TASK_TYPE,
    PRIORITY_TO_SALES_PRIORITY,
    build_task_title,
    build_task_description,
    synchronize_recommended_contact_task,
)
from app.main import app, get_db

# ----------------------------------------------------
# 1. Pure Mapping and Helper Unit Tests
# ----------------------------------------------------
def test_action_to_task_type_mappings():
    assert ACTION_TO_TASK_TYPE[RecommendedSalesAction.DISCOVERY_CALL] == SalesTaskType.CALL
    assert ACTION_TO_TASK_TYPE[RecommendedSalesAction.PERSONALIZED_EMAIL] == SalesTaskType.EMAIL
    assert ACTION_TO_TASK_TYPE[RecommendedSalesAction.DIRECT_OUTREACH] == SalesTaskType.REVIEW_OUTREACH
    assert ACTION_TO_TASK_TYPE[RecommendedSalesAction.FOLLOW_UP] == SalesTaskType.FOLLOW_UP
    assert ACTION_TO_TASK_TYPE[RecommendedSalesAction.RESEARCH_CONTACT] == SalesTaskType.OTHER

def test_priority_to_sales_priority_mappings():
    assert PRIORITY_TO_SALES_PRIORITY[RecommendationPriority.CRITICAL] == SalesPriority.URGENT
    assert PRIORITY_TO_SALES_PRIORITY[RecommendationPriority.HIGH] == SalesPriority.HIGH
    assert PRIORITY_TO_SALES_PRIORITY[RecommendationPriority.MEDIUM] == SalesPriority.MEDIUM
    assert PRIORITY_TO_SALES_PRIORITY[RecommendationPriority.LOW] == SalesPriority.LOW
    assert PRIORITY_TO_SALES_PRIORITY[RecommendationPriority.VERY_LOW] == SalesPriority.LOW

def test_task_title_construction():
    title = build_task_title(RecommendedSalesAction.DISCOVERY_CALL, "Jane Doe", "SEO Opportunity")
    assert title == "Discovery Call: Jane Doe — SEO Opportunity"
    assert len(title) <= 250

def test_task_description_evidence_grounded():
    desc = build_task_description(
        contact_name="Jane Doe",
        recommendation_score=88,
        match_score=91,
        decision_maker_score=75,
        buying_influence="DECISION_MAKER",
        recommended_action="DISCOVERY_CALL",
        reasons=["Strong opportunity alignment.", "Direct phone available."]
    )
    assert "Recommended contact:\nJane Doe" in desc
    assert "Recommendation score:\n88/100" in desc
    assert "Match score:\n91/100" in desc
    assert "Decision-maker score:\n75/100" in desc
    assert "Buying influence:\nDECISION_MAKER" in desc
    assert "Recommended action:\nDISCOVERY_CALL" in desc
    assert "- Strong opportunity alignment." in desc
    assert "- Direct phone available." in desc

# ----------------------------------------------------
# 2. Service-Level Tests with SQLite session_factory
# ----------------------------------------------------
@pytest.mark.asyncio
async def test_create_task_from_discovery_call(session_factory):
    async with session_factory() as session:
        # Create lead / contact
        lead_id = uuid.uuid4()
        lead = Lead(
            id=lead_id,
            company_name="Acme Corp",
            currency="USD",
            decision_maker="Jane Doe",
            job_title="Head of SEO",
            department="Marketing",
            email="jane@acme.com",
            phone="1234567890"
        )
        session.add(lead)

        profile = ContactIntelligenceProfile(
            contact_id=lead_id,
            decision_maker_score=95,
            functional_area=FunctionalArea.SEO,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.SYSTEM_DERIVED
        )
        session.add(profile)

        opp_id = uuid.uuid4()
        opp = LeadIntelligenceOpportunity(
            id=opp_id,
            lead_id=lead_id,
            title="SEO Audit Expansion",
            opportunity_type="SEO",
            score=95,
            priority="HIGH",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH
        )
        session.add(opp)
        await session.commit()

        # Synchronize task
        res = await synchronize_recommended_contact_task(session, opp_id)
        assert res is not None
        assert res["created"] is True
        assert res["reused"] is False
        assert res["opportunity_id"] == opp_id
        assert res["contact_id"] == lead_id
        assert res["task_type"] == SalesTaskType.CALL.value
        assert res["task_id"] is not None

        # Verify task in DB
        task = await session.get(SalesTask, res["task_id"])
        assert task is not None
        assert task.status == SalesTaskStatus.OPEN
        assert task.task_type == SalesTaskType.CALL
        assert task.priority in (SalesPriority.HIGH, SalesPriority.URGENT)
        assert task.agent_source_type == "SALES_OPPORTUNITY"
        assert task.agent_source_id == opp_id
        assert f"contact:{lead_id}" in task.agent_evidence_refs
        assert f"opportunity:{opp_id}" in task.agent_evidence_refs
        assert "Jane Doe" in task.title

@pytest.mark.asyncio
async def test_personalized_email_task_creation(session_factory):
    async with session_factory() as session:
        lead_id = uuid.uuid4()
        lead = Lead(
            id=lead_id,
            company_name="Apex Media",
            currency="USD",
            decision_maker="Bob Smith",
            job_title="Marketing Director",
            email="bob@apexmedia.com",
            phone=None  # Missing phone -> Personalized Email
        )
        session.add(lead)

        profile = ContactIntelligenceProfile(
            contact_id=lead_id,
            decision_maker_score=70,
            functional_area=FunctionalArea.MARKETING,
            seniority_level=SeniorityLevel.MANAGER,
            buying_influence=BuyingInfluenceLevel.HIGH,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.SYSTEM_DERIVED
        )
        session.add(profile)

        opp_id = uuid.uuid4()
        opp = LeadIntelligenceOpportunity(
            id=opp_id,
            lead_id=lead_id,
            title="Digital Marketing Retainer",
            opportunity_type="DIGITAL_MARKETING",
            score=80,
            priority="HIGH",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH
        )
        session.add(opp)
        await session.commit()

        res = await synchronize_recommended_contact_task(session, opp_id)
        assert res["task_type"] == SalesTaskType.EMAIL.value
        assert res["created"] is True

@pytest.mark.asyncio
async def test_direct_outreach_for_linkedin_only(session_factory):
    async with session_factory() as session:
        lead_id = uuid.uuid4()
        lead = Lead(
            id=lead_id,
            company_name="Nexus Cloud",
            currency="USD",
            job_title="Head of Tech",
            email=None,
            phone=None,
            linkedin_url="https://linkedin.com/in/nexustech"
        )
        session.add(lead)

        profile = ContactIntelligenceProfile(
            contact_id=lead_id,
            decision_maker_score=65,
            functional_area=FunctionalArea.TECHNOLOGY,
            seniority_level=SeniorityLevel.LEADERSHIP,
            buying_influence=BuyingInfluenceLevel.HIGH,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.SYSTEM_DERIVED
        )
        session.add(profile)

        opp_id = uuid.uuid4()
        opp = LeadIntelligenceOpportunity(
            id=opp_id,
            lead_id=lead_id,
            title="Cloud Infrastructure Modernization",
            opportunity_type="TECHNOLOGY",
            score=75,
            priority="HIGH",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH
        )
        session.add(opp)
        await session.commit()

        res = await synchronize_recommended_contact_task(session, opp_id)
        assert res["task_type"] == SalesTaskType.REVIEW_OUTREACH.value

@pytest.mark.asyncio
async def test_no_action_for_insufficient_evidence(session_factory):
    async with session_factory() as session:
        lead_id = uuid.uuid4()
        lead = Lead(
            id=lead_id,
            company_name="Quiet Corp",
            currency="USD",
            job_title="Junior Assistant",
            email=None,
            phone=None
        )
        session.add(lead)

        opp_id = uuid.uuid4()
        opp = LeadIntelligenceOpportunity(
            id=opp_id,
            lead_id=lead_id,
            title="Unrelated Opportunity",
            opportunity_type="LEGAL",
            score=10,
            priority="LOW",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.LOW
        )
        session.add(opp)
        await session.commit()

        res = await synchronize_recommended_contact_task(session, opp_id)
        assert res["created"] is False
        assert res["reused"] is False
        assert res["recommended_action"] == "NO_ACTION"
        assert res["task_id"] is None

        # Confirm no task in database
        tasks = (await session.execute(select(SalesTask).where(SalesTask.lead_id == lead_id))).scalars().all()
        assert len(tasks) == 0

@pytest.mark.asyncio
async def test_idempotent_repeated_execution_and_reuse(session_factory):
    async with session_factory() as session:
        lead_id = uuid.uuid4()
        lead = Lead(
            id=lead_id,
            company_name="Alpha Tech",
            currency="USD",
            decision_maker="Alice CEO",
            job_title="CEO",
            email="alice@alpha.com",
            phone="5551234"
        )
        session.add(lead)

        opp_id = uuid.uuid4()
        opp = LeadIntelligenceOpportunity(
            id=opp_id,
            lead_id=lead_id,
            title="Strategic Transformation",
            opportunity_type="EXECUTIVE",
            score=95,
            priority="HIGH",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH
        )
        session.add(opp)
        await session.commit()

        # First run -> Created
        res1 = await synchronize_recommended_contact_task(session, opp_id)
        assert res1["created"] is True
        assert res1["reused"] is False
        task_id_1 = res1["task_id"]

        # Second run -> Reused (Idempotent)
        res2 = await synchronize_recommended_contact_task(session, opp_id)
        assert res2["created"] is False
        assert res2["reused"] is True
        assert res2["task_id"] == task_id_1

        # Check total tasks in DB is strictly 1
        tasks = (await session.execute(select(SalesTask).where(SalesTask.lead_id == lead_id))).scalars().all()
        assert len(tasks) == 1

@pytest.mark.asyncio
async def test_12g_generic_task_enrichment_and_compatibility(session_factory):
    async with session_factory() as session:
        lead_id = uuid.uuid4()
        lead = Lead(
            id=lead_id,
            company_name="Beta Retail",
            currency="USD",
            decision_maker="Carl Director",
            job_title="Sales Director",
            email="carl@betaretail.com",
            phone="8889990"
        )
        session.add(lead)

        opp_id = uuid.uuid4()
        opp = LeadIntelligenceOpportunity(
            id=opp_id,
            lead_id=lead_id,
            title="E-commerce Optimization",
            opportunity_type="DIGITAL_MARKETING",
            score=85,
            priority="HIGH",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH
        )
        session.add(opp)

        # Simulate existing 12G generic task
        existing_12g_task = SalesTask(
            lead_id=lead_id,
            task_type=SalesTaskType.OTHER,
            title="Review: E-commerce Optimization",
            description="12G generic description",
            status=SalesTaskStatus.OPEN,
            priority=SalesPriority.MEDIUM,
            due_at=datetime.now(timezone.utc),
            agent_source_type="SALES_OPPORTUNITY",
            agent_source_id=opp_id,
            agent_source_action_id="DIGITAL_MARKETING",
            agent_evidence_refs=[f"opportunity:{opp_id}"]
        )
        session.add(existing_12g_task)
        await session.commit()

        # Run 12H-F bridge
        res = await synchronize_recommended_contact_task(session, opp_id)
        assert res["created"] is False
        assert res["reused"] is True
        assert res["task_id"] == existing_12g_task.id

        # Verify task was enriched
        await session.refresh(existing_12g_task)
        assert existing_12g_task.task_type == SalesTaskType.FOLLOW_UP
        assert "Carl Director" in existing_12g_task.title
        assert f"contact:{lead_id}" in existing_12g_task.agent_evidence_refs

@pytest.mark.asyncio
async def test_completed_historical_task_does_not_duplicate(session_factory):
    async with session_factory() as session:
        lead_id = uuid.uuid4()
        lead = Lead(
            id=lead_id,
            company_name="Gamma Industries",
            currency="USD",
            decision_maker="Dana SEO",
            job_title="Head of SEO",
            department="Marketing",
            email="dana@gamma.com",
            phone="1122334"
        )
        session.add(lead)

        opp_id = uuid.uuid4()
        opp = LeadIntelligenceOpportunity(
            id=opp_id,
            lead_id=lead_id,
            title="SEO Infrastructure Audit",
            opportunity_type="SEO",
            score=95,
            priority="HIGH",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH
        )
        session.add(opp)

        profile = ContactIntelligenceProfile(
            contact_id=lead_id,
            decision_maker_score=95,
            functional_area=FunctionalArea.SEO,
            seniority_level=SeniorityLevel.EXECUTIVE,
            buying_influence=BuyingInfluenceLevel.DECISION_MAKER,
            confidence=ConfidenceLevel.HIGH,
            source_type=SourceType.SYSTEM_DERIVED
        )
        session.add(profile)

        # Simulate already completed task for this exact recommendation
        action_id = f"contact_recommendation:{lead_id}:DISCOVERY_CALL"
        completed_task = SalesTask(
            lead_id=lead_id,
            task_type=SalesTaskType.CALL,
            title="Discovery Call: Dana SEO — SEO Infrastructure Audit",
            description="Completed task",
            status=SalesTaskStatus.COMPLETED,
            completed_at=datetime.now(timezone.utc),
            priority=SalesPriority.HIGH,
            due_at=datetime.now(timezone.utc),
            agent_source_type="SALES_OPPORTUNITY",
            agent_source_id=opp_id,
            agent_source_action_id=action_id,
            agent_evidence_refs=[f"contact:{lead_id}", f"opportunity:{opp_id}"]
        )
        session.add(completed_task)
        await session.commit()

        # Run 12H-F bridge
        res = await synchronize_recommended_contact_task(session, opp_id)
        assert res["created"] is False
        assert res["reused"] is True
        assert res["task_id"] == completed_task.id

        # Assert no new task was created
        all_tasks = (await session.execute(select(SalesTask).where(SalesTask.lead_id == lead_id))).scalars().all()
        assert len(all_tasks) == 1

@pytest.mark.asyncio
async def test_company_boundary_enforced(session_factory):
    async with session_factory() as session:
        company_lead_id = uuid.uuid4()
        company_lead = Lead(
            id=company_lead_id,
            company_name="Target Company",
            currency="USD",
            decision_maker="Target CEO",
            job_title="CEO",
            email="ceo@target.com",
            phone="111"
        )
        session.add(company_lead)

        # Contact at unrelated company
        other_company_lead_id = uuid.uuid4()
        other_lead = Lead(
            id=other_company_lead_id,
            company_name="Unrelated Company",
            currency="USD",
            decision_maker="Foreign Lead",
            job_title="CEO",
            email="ceo@unrelated.com",
            phone="222"
        )
        session.add(other_lead)

        opp_id = uuid.uuid4()
        opp = LeadIntelligenceOpportunity(
            id=opp_id,
            lead_id=company_lead_id,
            title="Targeted Campaign",
            opportunity_type="MARKETING",
            score=80,
            priority="HIGH",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH
        )
        session.add(opp)
        await session.commit()

        res = await synchronize_recommended_contact_task(session, opp_id)
        assert res["contact_id"] == company_lead_id
        assert res["contact_id"] != other_company_lead_id

# ----------------------------------------------------
# 3. HTTP API Endpoint Tests
# ----------------------------------------------------
@pytest.mark.asyncio
async def test_contact_recommendation_task_api_endpoint(session_factory, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")

    async with session_factory() as session:
        lead_id = uuid.uuid4()
        lead = Lead(
            id=lead_id,
            company_name="Zeta Global",
            currency="USD",
            decision_maker="Zoe VP",
            job_title="VP of Sales",
            email="zoe@zetaglobal.com",
            phone="99887766"
        )
        session.add(lead)

        opp_id = uuid.uuid4()
        opp = LeadIntelligenceOpportunity(
            id=opp_id,
            lead_id=lead_id,
            title="Sales Pipeline Expansion",
            opportunity_type="SALES",
            score=88,
            priority="HIGH",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH
        )
        session.add(opp)
        await session.commit()

    app.dependency_overrides[get_db] = lambda: session_factory()
    headers = {"Authorization": "Bearer api-test-token"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Successful POST endpoint
        res = await client.post(
            f"/api/sales-opportunities/{opp_id}/contact-recommendation/task",
            headers=headers
        )
        assert res.status_code == 200
        data = res.json()
        assert data["opportunity_id"] == str(opp_id)
        assert data["contact_id"] == str(lead_id)
        assert data["created"] is True
        assert data["task_id"] is not None
        assert data["task_type"] == "EMAIL"

        # 2. Repeated POST endpoint (idempotent reuse)
        res_repeat = await client.post(
            f"/api/sales-opportunities/{opp_id}/contact-recommendation/task",
            headers=headers
        )
        assert res_repeat.status_code == 200
        data_repeat = res_repeat.json()
        assert data_repeat["created"] is False
        assert data_repeat["reused"] is True
        assert data_repeat["task_id"] == data["task_id"]

        # 3. 404 for unknown opportunity
        bad_id = uuid.uuid4()
        res_bad = await client.post(
            f"/api/sales-opportunities/{bad_id}/contact-recommendation/task",
            headers=headers
        )
        assert res_bad.status_code == 404

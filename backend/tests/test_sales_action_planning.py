import uuid
from datetime import datetime, timezone
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

import pytest_asyncio
from app.models.entities import Lead, SalesTask, SalesTaskStatus, LeadIntelligenceOpportunity, SourceType, ConfidenceLevel
from app.services.sales_action_planning import synchronize_opportunity_tasks

@pytest_asyncio.fixture
async def sample_lead(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Task Test Co")
        session.add(lead)
        await session.commit()
        await session.refresh(lead)
        return lead

@pytest.mark.asyncio
async def test_synchronize_creates_task(session_factory, sample_lead):
    async with session_factory() as session:
        # Create opportunity
        opp_id = uuid.uuid4()
        opp = LeadIntelligenceOpportunity(
            id=opp_id,
            lead_id=sample_lead.id,
            title="Marketing Enhance",
            opportunity_type="DIGITAL_MARKETING",
            score=65,
            priority="HIGH",
            recommended_action="REVIEW_MARKETING",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        session.add(opp)
        await session.commit()

        # Sync
        tasks = await synchronize_opportunity_tasks(session, sample_lead.id)
        assert len(tasks) == 1
        
        # Assert task properties
        task = tasks[0]
        assert task.lead_id == sample_lead.id
        assert task.agent_source_type == "SALES_OPPORTUNITY"
        assert task.agent_source_id == opp_id
        assert task.agent_source_action_id == opp.source_reference or opp.opportunity_type
        assert "opportunity" in task.agent_evidence_refs[0]
        assert task.title.startswith("Review Marketing:")
        assert task.status == SalesTaskStatus.OPEN
        assert task.priority == "HIGH"

@pytest.mark.asyncio
async def test_synchronize_idempotent_refresh(session_factory, sample_lead):
    async with session_factory() as session:
        # Setup initial opp and sync
        opp_type = "DIGITAL_MARKETING"
        opp_ref = "buying_signals.marketing_combo"
        opp1 = LeadIntelligenceOpportunity(
            id=uuid.uuid4(),
            lead_id=sample_lead.id,
            title="Marketing Enhance 1",
            opportunity_type=opp_type,
            source_reference=opp_ref,
            score=60,
            priority="HIGH",
            recommended_action="REVIEW_MARKETING",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        session.add(opp1)
        await session.commit()
        
        await synchronize_opportunity_tasks(session, sample_lead.id)

        # Simulate refresh: opp1 deleted, opp2 created with new UUID but same source_reference
        await session.delete(opp1)
        opp2_id = uuid.uuid4()
        opp2 = LeadIntelligenceOpportunity(
            id=opp2_id,
            lead_id=sample_lead.id,
            title="Marketing Enhance 2",
            opportunity_type=opp_type,
            source_reference=opp_ref,
            score=85,
            priority="URGENT",
            recommended_action="PREPARE_OUTREACH",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        session.add(opp2)
        await session.commit()

        # Sync again
        tasks = await synchronize_opportunity_tasks(session, sample_lead.id)
        assert len(tasks) == 1
        
        # Assert task was reused and relinked
        task = tasks[0]
        assert task.agent_source_action_id == opp_ref
        assert task.agent_source_id == opp2_id # Updated to point to new opp
        
        # Assert DB count is strictly 1
        db_tasks = (await session.execute(select(SalesTask).where(SalesTask.lead_id == sample_lead.id))).scalars().all()
        assert len(db_tasks) == 1

@pytest.mark.asyncio
async def test_synchronize_preserves_completed_task_but_spawns_new_for_fresh_opp(session_factory, sample_lead):
    async with session_factory() as session:
        # Setup opp
        opp_ref = "buying_signals.marketing_combo"
        opp1 = LeadIntelligenceOpportunity(
            id=uuid.uuid4(),
            lead_id=sample_lead.id,
            title="Marketing",
            opportunity_type="DIGITAL_MARKETING",
            source_reference=opp_ref,
            score=50,
            priority="MEDIUM",
            recommended_action="REVIEW_MARKETING",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        session.add(opp1)
        await session.commit()

        # Sync to create task
        tasks = await synchronize_opportunity_tasks(session, sample_lead.id)
        task = tasks[0]
        
        # Complete the task
        task.status = SalesTaskStatus.COMPLETED
        task.completed_at = datetime.now(timezone.utc)
        await session.commit()

        # Sync again without refreshing (same Opp ID). Should not spawn a new task.
        tasks = await synchronize_opportunity_tasks(session, sample_lead.id)
        assert len(tasks) == 0 # no new task spawned for the already synced opportunity
        
        db_tasks = (await session.execute(select(SalesTask).where(SalesTask.lead_id == sample_lead.id))).scalars().all()
        assert len(db_tasks) == 1 # still just the one completed task
        
        # Simulate fresh evidence creating a new opportunity later
        await session.delete(opp1)
        opp2 = LeadIntelligenceOpportunity(
            id=uuid.uuid4(),
            lead_id=sample_lead.id,
            title="Marketing Fresh",
            opportunity_type="DIGITAL_MARKETING",
            source_reference=opp_ref,
            score=70,
            priority="HIGH",
            recommended_action="REVIEW_MARKETING",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        session.add(opp2)
        await session.commit()
        
        # Sync again
        tasks = await synchronize_opportunity_tasks(session, sample_lead.id)
        assert len(tasks) == 1 # a new OPEN task is returned
        
        db_tasks = (await session.execute(select(SalesTask).where(SalesTask.lead_id == sample_lead.id))).scalars().all()
        assert len(db_tasks) == 2 # 1 completed, 1 open
        
        # Verify the new task points to the fresh opp
        open_tasks = [t for t in db_tasks if t.status == SalesTaskStatus.OPEN]
        assert len(open_tasks) == 1
        assert open_tasks[0].agent_source_id == opp2.id

@pytest.mark.asyncio
async def test_two_legitimate_opportunities_do_not_collide(session_factory, sample_lead):
    async with session_factory() as session:
        # Setup two distinct opps with same opportunity_type but different source_reference
        opp_type = "DIGITAL_MARKETING"
        opp1 = LeadIntelligenceOpportunity(
            id=uuid.uuid4(),
            lead_id=sample_lead.id,
            title="Marketing Ads",
            opportunity_type=opp_type,
            source_reference="buying_signals.ads_combo",
            score=60,
            priority="HIGH",
            recommended_action="REVIEW_MARKETING",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        opp2 = LeadIntelligenceOpportunity(
            id=uuid.uuid4(),
            lead_id=sample_lead.id,
            title="Marketing SEO",
            opportunity_type=opp_type,
            source_reference="buying_signals.seo_combo",
            score=70,
            priority="HIGH",
            recommended_action="REVIEW_MARKETING",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        session.add_all([opp1, opp2])
        await session.commit()
        
        # Sync
        tasks = await synchronize_opportunity_tasks(session, sample_lead.id)
        assert len(tasks) == 2 # Both spawned distinctly
        
        db_tasks = (await session.execute(select(SalesTask).where(SalesTask.lead_id == sample_lead.id))).scalars().all()
        assert len(db_tasks) == 2

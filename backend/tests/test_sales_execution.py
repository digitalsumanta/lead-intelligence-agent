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
from app.services.sales_execution import (
    record_sales_execution_outcome,
    TASK_TYPE_TO_ACTIVITY_TYPE,
    SalesExecutionError,
)
from app.main import app, get_db

AUTH_HEADERS = {"Authorization": "Bearer api-test-token"}


# ----------------------------------------------------
# 1. Pure Mapping and Unit Tests
# ----------------------------------------------------
def test_task_type_to_activity_type_mapping():
    assert TASK_TYPE_TO_ACTIVITY_TYPE[SalesTaskType.CALL] == SalesActivityType.CALL
    assert TASK_TYPE_TO_ACTIVITY_TYPE[SalesTaskType.EMAIL] == SalesActivityType.EMAIL
    assert TASK_TYPE_TO_ACTIVITY_TYPE[SalesTaskType.WHATSAPP] == SalesActivityType.WHATSAPP
    assert TASK_TYPE_TO_ACTIVITY_TYPE[SalesTaskType.MEETING] == SalesActivityType.MEETING
    assert TASK_TYPE_TO_ACTIVITY_TYPE[SalesTaskType.PROPOSAL] == SalesActivityType.PROPOSAL
    assert TASK_TYPE_TO_ACTIVITY_TYPE[SalesTaskType.FOLLOW_UP] == SalesActivityType.FOLLOW_UP
    assert TASK_TYPE_TO_ACTIVITY_TYPE[SalesTaskType.REVIEW_OUTREACH] == SalesActivityType.NOTE
    assert TASK_TYPE_TO_ACTIVITY_TYPE[SalesTaskType.OTHER] == SalesActivityType.OTHER


# ----------------------------------------------------
# 2. Service-Level Lifecycle & Outcome Tests
# ----------------------------------------------------
@pytest.mark.asyncio
async def test_record_outcome_on_open_task_completes_task(session_factory):
    async with session_factory() as session:
        lead = Lead(
            company_name="Test Company",
            currency="USD",
            decision_maker="Alice",
            sales_status=SalesStatus.NEW,
        )
        session.add(lead)
        await session.flush()

        task = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Discovery Call: Alice",
            status=SalesTaskStatus.OPEN,
            priority=SalesPriority.HIGH,
            due_at=datetime.now(timezone.utc),
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)

        res = await record_sales_execution_outcome(
            session=session,
            task_id=task.id,
            outcome=SalesActivityOutcome.CONNECTED,
            notes="Spoke with Alice, very interested in SEO audit.",
        )

        assert res is not None
        assert res["task_id"] == task.id
        assert res["task_status"] == SalesTaskStatus.COMPLETED.value
        assert res["created"] is True
        assert res["reused"] is False
        assert res["outcome"] == SalesActivityOutcome.CONNECTED.value
        assert res["activity_id"] is not None

        # Verify DB state
        task_db = await session.scalar(select(SalesTask).where(SalesTask.id == task.id))
        assert task_db.status == SalesTaskStatus.COMPLETED
        assert task_db.completed_at is not None
        assert task_db.related_activity_id == res["activity_id"]
        assert f"outcome:{SalesActivityOutcome.CONNECTED.value}" in task_db.agent_evidence_refs
        assert f"activity:{res['activity_id']}" in task_db.agent_evidence_refs

        act_db = await session.scalar(select(SalesActivity).where(SalesActivity.id == res["activity_id"]))
        assert act_db.activity_type == SalesActivityType.CALL
        assert act_db.outcome == SalesActivityOutcome.CONNECTED
        assert act_db.note == "Spoke with Alice, very interested in SEO audit."


@pytest.mark.asyncio
async def test_record_outcome_on_in_progress_task(session_factory):
    async with session_factory() as session:
        lead = Lead(
            company_name="Beta Corp",
            currency="USD",
            decision_maker="Bob",
            sales_status=SalesStatus.CONTACTED,
        )
        session.add(lead)
        await session.flush()

        task = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.EMAIL,
            title="Personalized Email: Bob",
            status=SalesTaskStatus.IN_PROGRESS,
            priority=SalesPriority.MEDIUM,
            due_at=datetime.now(timezone.utc),
        )
        session.add(task)
        await session.commit()

        res = await record_sales_execution_outcome(
            session=session,
            task_id=task.id,
            outcome=SalesActivityOutcome.PROPOSAL_REQUESTED,
        )

        assert res["task_status"] == SalesTaskStatus.COMPLETED.value
        task_db = await session.scalar(select(SalesTask).where(SalesTask.id == task.id))
        assert task_db.status == SalesTaskStatus.COMPLETED
        assert task_db.completed_at is not None


@pytest.mark.asyncio
async def test_record_outcome_on_cancelled_task_raises_error(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Gamma LLC", currency="USD")
        session.add(lead)
        await session.flush()

        task = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Call Gamma",
            status=SalesTaskStatus.CANCELLED,
            priority=SalesPriority.LOW,
            due_at=datetime.now(timezone.utc),
        )
        session.add(task)
        await session.commit()

        with pytest.raises(SalesExecutionError) as exc_info:
            await record_sales_execution_outcome(
                session=session,
                task_id=task.id,
                outcome=SalesActivityOutcome.NO_RESPONSE,
            )
        assert exc_info.value.code == "TASK_CANCELLED"
        assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_idempotent_recording_on_completed_task(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Delta Inc", currency="USD")
        session.add(lead)
        await session.flush()

        task = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Call Delta",
            status=SalesTaskStatus.OPEN,
            priority=SalesPriority.HIGH,
            due_at=datetime.now(timezone.utc),
        )
        session.add(task)
        await session.commit()

        # First recording
        res1 = await record_sales_execution_outcome(
            session=session,
            task_id=task.id,
            outcome=SalesActivityOutcome.CONNECTED,
            notes="Initial call notes",
        )
        assert res1["created"] is True
        assert res1["reused"] is False
        act_id = res1["activity_id"]

        # Second recording (idempotent update)
        res2 = await record_sales_execution_outcome(
            session=session,
            task_id=task.id,
            outcome=SalesActivityOutcome.INTERESTED,
            notes="Updated call notes - showed strong interest",
        )
        assert res2["created"] is False
        assert res2["reused"] is True
        assert res2["activity_id"] == act_id
        assert res2["outcome"] == SalesActivityOutcome.INTERESTED.value

        # Verify activity was updated
        act_db = await session.scalar(select(SalesActivity).where(SalesActivity.id == act_id))
        assert act_db.outcome == SalesActivityOutcome.INTERESTED
        assert act_db.note == "Updated call notes - showed strong interest"


@pytest.mark.asyncio
async def test_explicit_activity_linking(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Epsilon Co", currency="USD")
        session.add(lead)
        await session.flush()

        # Pre-create an activity
        existing_act = SalesActivity(
            lead_id=lead.id,
            activity_type=SalesActivityType.MEETING,
            occurred_at=datetime.now(timezone.utc),
            note="Pre-existing discovery meeting",
            outcome=SalesActivityOutcome.CONNECTED,
        )
        session.add(existing_act)
        await session.flush()

        task = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.MEETING,
            title="Meeting with Epsilon",
            status=SalesTaskStatus.OPEN,
            priority=SalesPriority.URGENT,
            due_at=datetime.now(timezone.utc),
        )
        session.add(task)
        await session.commit()

        res = await record_sales_execution_outcome(
            session=session,
            task_id=task.id,
            outcome=SalesActivityOutcome.MEETING_BOOKED,
            activity_id=existing_act.id,
        )

        assert res["created"] is False
        assert res["reused"] is True
        assert res["activity_id"] == existing_act.id

        task_db = await session.scalar(select(SalesTask).where(SalesTask.id == task.id))
        assert task_db.related_activity_id == existing_act.id
        assert task_db.status == SalesTaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_explicit_activity_linking_mismatched_lead_fails(session_factory):
    async with session_factory() as session:
        lead1 = Lead(company_name="Lead 1", currency="USD")
        lead2 = Lead(company_name="Lead 2", currency="USD")
        session.add_all([lead1, lead2])
        await session.flush()

        act_lead2 = SalesActivity(
            lead_id=lead2.id,
            activity_type=SalesActivityType.CALL,
            occurred_at=datetime.now(timezone.utc),
            note="Call on lead 2",
            outcome=SalesActivityOutcome.CONNECTED,
        )
        session.add(act_lead2)
        await session.flush()

        task_lead1 = SalesTask(
            lead_id=lead1.id,
            task_type=SalesTaskType.CALL,
            title="Call lead 1",
            status=SalesTaskStatus.OPEN,
            priority=SalesPriority.HIGH,
            due_at=datetime.now(timezone.utc),
        )
        session.add(task_lead1)
        await session.commit()

        with pytest.raises(SalesExecutionError) as exc_info:
            await record_sales_execution_outcome(
                session=session,
                task_id=task_lead1.id,
                outcome=SalesActivityOutcome.CONNECTED,
                activity_id=act_lead2.id,
            )
        assert exc_info.value.code == "INVALID_ACTIVITY"
        assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_lead_sales_status_progression(session_factory):
    async with session_factory() as session:
        lead = Lead(
            company_name="Progression Corp",
            currency="USD",
            sales_status=SalesStatus.QUALIFIED,
        )
        session.add(lead)
        await session.flush()

        # Meeting booked progresses QUALIFIED -> ENGAGED
        task1 = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.MEETING,
            title="Meeting Booking Task",
            status=SalesTaskStatus.OPEN,
            priority=SalesPriority.HIGH,
            due_at=datetime.now(timezone.utc),
        )
        session.add(task1)
        await session.commit()

        await record_sales_execution_outcome(
            session=session,
            task_id=task1.id,
            outcome=SalesActivityOutcome.MEETING_BOOKED,
        )
        lead_db = await session.scalar(select(Lead).where(Lead.id == lead.id))
        assert lead_db.sales_status == SalesStatus.ENGAGED

        # Won outcome progresses to WON
        task2 = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.PROPOSAL,
            title="Closing Deal",
            status=SalesTaskStatus.OPEN,
            priority=SalesPriority.HIGH,
            due_at=datetime.now(timezone.utc),
        )
        session.add(task2)
        await session.commit()

        await record_sales_execution_outcome(
            session=session,
            task_id=task2.id,
            outcome=SalesActivityOutcome.WON,
        )
        lead_db = await session.scalar(select(Lead).where(Lead.id == lead.id))
        assert lead_db.sales_status == SalesStatus.WON


@pytest.mark.asyncio
async def test_explicit_new_sales_status_override(session_factory):
    async with session_factory() as session:
        lead = Lead(
            company_name="Override Corp",
            currency="USD",
            sales_status=SalesStatus.NEW,
        )
        session.add(lead)
        await session.flush()

        task = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Discovery Call",
            status=SalesTaskStatus.OPEN,
            priority=SalesPriority.HIGH,
            due_at=datetime.now(timezone.utc),
        )
        session.add(task)
        await session.commit()

        await record_sales_execution_outcome(
            session=session,
            task_id=task.id,
            outcome=SalesActivityOutcome.CONNECTED,
            new_sales_status=SalesStatus.PROPOSAL,
        )
        lead_db = await session.scalar(select(Lead).where(Lead.id == lead.id))
        assert lead_db.sales_status == SalesStatus.PROPOSAL


@pytest.mark.asyncio
async def test_opportunity_source_linked_response(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Opportunity Lead", currency="USD")
        session.add(lead)
        await session.flush()

        opp = LeadIntelligenceOpportunity(
            lead_id=lead.id,
            title="High Value SEO Package",
            opportunity_type="SEO",
            score=90,
            priority="HIGH",
            recommended_action="DISCOVERY_CALL",
            source_type=SourceType.SYSTEM_DERIVED,
            confidence=ConfidenceLevel.HIGH,
        )
        session.add(opp)
        await session.flush()

        task = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Discovery Call: Opp",
            status=SalesTaskStatus.OPEN,
            priority=SalesPriority.HIGH,
            due_at=datetime.now(timezone.utc),
            created_by_agent=True,
            agent_source_type="SALES_OPPORTUNITY",
            agent_source_id=opp.id,
        )
        session.add(task)
        await session.commit()

        res = await record_sales_execution_outcome(
            session=session,
            task_id=task.id,
            outcome=SalesActivityOutcome.INTERESTED,
        )
        assert res["opportunity_id"] == opp.id


@pytest.mark.asyncio
async def test_various_task_type_activity_creations(session_factory):

    async with session_factory() as session:
        lead = Lead(company_name="Multi Task Co", currency="USD")
        session.add(lead)
        await session.flush()

        task_whatsapp = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.WHATSAPP,
            title="WhatsApp outreach",
            status=SalesTaskStatus.OPEN,
            priority=SalesPriority.MEDIUM,
            due_at=datetime.now(timezone.utc),
        )
        task_review = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.REVIEW_OUTREACH,
            title="Review outreach draft",
            status=SalesTaskStatus.OPEN,
            priority=SalesPriority.MEDIUM,
            due_at=datetime.now(timezone.utc),
        )
        task_proposal = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.PROPOSAL,
            title="Send proposal doc",
            status=SalesTaskStatus.OPEN,
            priority=SalesPriority.HIGH,
            due_at=datetime.now(timezone.utc),
        )
        session.add_all([task_whatsapp, task_review, task_proposal])
        await session.commit()

        res_wa = await record_sales_execution_outcome(session, task_whatsapp.id, SalesActivityOutcome.NO_RESPONSE)
        act_wa = await session.scalar(select(SalesActivity).where(SalesActivity.id == res_wa["activity_id"]))
        assert act_wa.activity_type == SalesActivityType.WHATSAPP

        res_rev = await record_sales_execution_outcome(session, task_review.id, SalesActivityOutcome.CONNECTED)
        act_rev = await session.scalar(select(SalesActivity).where(SalesActivity.id == res_rev["activity_id"]))
        assert act_rev.activity_type == SalesActivityType.NOTE

        res_prop = await record_sales_execution_outcome(session, task_proposal.id, SalesActivityOutcome.PROPOSAL_SENT)
        act_prop = await session.scalar(select(SalesActivity).where(SalesActivity.id == res_prop["activity_id"]))
        assert act_prop.activity_type == SalesActivityType.PROPOSAL


@pytest.mark.asyncio
async def test_more_status_progressions(session_factory):
    async with session_factory() as session:
        lead = Lead(company_name="Progression Co 2", currency="USD", sales_status=SalesStatus.ENGAGED)
        session.add(lead)
        await session.flush()

        # Proposal Sent -> PROPOSAL
        task1 = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.PROPOSAL,
            title="Send proposal",
            status=SalesTaskStatus.OPEN,
            priority=SalesPriority.HIGH,
            due_at=datetime.now(timezone.utc),
        )
        session.add(task1)
        await session.commit()

        await record_sales_execution_outcome(session, task1.id, SalesActivityOutcome.PROPOSAL_SENT)
        await session.refresh(lead)
        assert lead.sales_status == SalesStatus.PROPOSAL

        # Negotiation Started -> NEGOTIATION
        task2 = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Negotiation Call",
            status=SalesTaskStatus.OPEN,
            priority=SalesPriority.HIGH,
            due_at=datetime.now(timezone.utc),
        )
        session.add(task2)
        await session.commit()

        await record_sales_execution_outcome(session, task2.id, SalesActivityOutcome.NEGOTIATION_STARTED)
        await session.refresh(lead)
        assert lead.sales_status == SalesStatus.NEGOTIATION

        # Lost -> LOST
        task3 = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Closing Call",
            status=SalesTaskStatus.OPEN,
            priority=SalesPriority.HIGH,
            due_at=datetime.now(timezone.utc),
        )
        session.add(task3)
        await session.commit()

        await record_sales_execution_outcome(session, task3.id, SalesActivityOutcome.LOST)
        await session.refresh(lead)
        assert lead.sales_status == SalesStatus.LOST


@pytest.mark.asyncio
async def test_nonexistent_task_returns_none(session_factory):
    async with session_factory() as session:
        res = await record_sales_execution_outcome(session, uuid.uuid4(), SalesActivityOutcome.CONNECTED)
        assert res is None


from app.config import settings


# ----------------------------------------------------
# 3. HTTP API Route Tests
# ----------------------------------------------------
@pytest.mark.asyncio
async def test_api_record_execution_success(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    async with session_factory() as session:
        lead = Lead(company_name="API Test Lead", currency="USD")
        session.add(lead)
        await session.flush()

        task = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.EMAIL,
            title="Send Proposal Email",
            status=SalesTaskStatus.OPEN,
            priority=SalesPriority.HIGH,
            due_at=datetime.now(timezone.utc),
        )
        session.add(task)
        await session.commit()
        task_id = str(task.id)

    app.dependency_overrides[get_db] = lambda: session_factory()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            payload = {
                "outcome": "MEETING_BOOKED",
                "notes": "Booked discovery call for next Tuesday.",
            }
            resp = await ac.post(
                f"/api/sales-tasks/{task_id}/execution",
                json=payload,
                headers=AUTH_HEADERS,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["task_id"] == task_id
            assert data["outcome"] == "MEETING_BOOKED"
            assert data["task_status"] == "COMPLETED"
            assert data["created"] is True
            assert data["reused"] is False
            assert data["activity_id"] is not None
            assert data["evidence"]["outcome"] == "MEETING_BOOKED"
            assert data["evidence"]["notes"] == "Booked discovery call for next Tuesday."
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_record_execution_task_not_found(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    app.dependency_overrides[get_db] = lambda: session_factory()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            fake_id = str(uuid.uuid4())
            resp = await ac.post(
                f"/api/sales-tasks/{fake_id}/execution",
                json={"outcome": "CONNECTED"},
                headers=AUTH_HEADERS,
            )
            assert resp.status_code == 404
            data = resp.json()
            assert data["error"]["message"] == "Sales task not found"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_api_record_execution_cancelled_task_error(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "api_auth_token", "api-test-token")
    async with session_factory() as session:
        lead = Lead(company_name="Cancelled Task Lead", currency="USD")
        session.add(lead)
        await session.flush()

        task = SalesTask(
            lead_id=lead.id,
            task_type=SalesTaskType.CALL,
            title="Cancelled Task",
            status=SalesTaskStatus.CANCELLED,
            priority=SalesPriority.LOW,
            due_at=datetime.now(timezone.utc),
        )
        session.add(task)
        await session.commit()
        task_id = str(task.id)

    app.dependency_overrides[get_db] = lambda: session_factory()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                f"/api/sales-tasks/{task_id}/execution",
                json={"outcome": "CONNECTED"},
                headers=AUTH_HEADERS,
            )
            assert resp.status_code == 409
            data = resp.json()
            assert "cancelled" in data["error"]["message"].lower()
    finally:
        app.dependency_overrides.pop(get_db, None)



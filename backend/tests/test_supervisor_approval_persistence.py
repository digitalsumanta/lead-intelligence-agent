"""Focused tests for durable human approval persistence, check constraints, partial uniqueness, and Alembic 0038 migration."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid
import pytest
from sqlalchemy import create_engine, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

from app.models.entities import (
    Base,
    Lead,
    SupervisorHumanApproval,
    SupervisorApprovalAuditEvent,
    DEFAULT_APPROVAL_VALIDITY_HOURS,
    now_utc,
)


def test_supervisor_approval_model_defaults():
    """Verify default values and constants for human approval models."""
    assert DEFAULT_APPROVAL_VALIDITY_HOURS == 48

    lead_id = uuid.uuid4()
    opp_id = uuid.uuid4()
    contact_id = uuid.uuid4()

    approval = SupervisorHumanApproval(
        action_id="act-lead-1",
        lead_id=lead_id,
        opportunity_id=opp_id,
        contact_id=contact_id,
        recommended_action="SCHEDULE_DISCOVERY_CALL",
        action_tier="EXTERNAL_OUTREACH",
        channel="EMAIL",
        context_fingerprint="abc123def456",
        status="PENDING",
        version=1,
        requested_by_user_id="user-sys-1",
    )

    assert approval.status == "PENDING"
    assert approval.version == 1
    assert approval.approved_by_user_id is None
    assert approval.rejected_by_user_id is None
    assert approval.revoked_by_user_id is None
    assert approval.execution_claimed_at is None
    assert approval.execution_claimed_by is None

    audit_event = SupervisorApprovalAuditEvent(
        approval_id=approval.id,
        action_id=approval.action_id,
        lead_id=lead_id,
        actor_id="user-sys-1",
        actor_role="SUPERVISOR_ENGINE",
        event_type="CREATED",
        from_status=None,
        to_status="PENDING",
        context_fingerprint="abc123def456",
        metadata_json={"source": "auto_evaluator"},
    )
    assert audit_event.metadata_json == {"source": "auto_evaluator"}
    assert audit_event.event_type == "CREATED"


@pytest.mark.asyncio
async def test_supervisor_approval_database_defaults(session_factory):
    """Verify database server defaults apply when status, version, and metadata_json are omitted."""
    async with session_factory() as session:
        lead = Lead(company_name="Defaults Test LLC")
        session.add(lead)
        await session.commit()

        approval = SupervisorHumanApproval(
            action_id="act-defaults-1",
            lead_id=lead.id,
            opportunity_id=uuid.uuid4(),
            recommended_action="SEND_EMAIL",
            action_tier="EXTERNAL_OUTREACH",
            context_fingerprint="fp-defaults",
            requested_by_user_id="sys-user",
        )
        session.add(approval)
        await session.commit()
        approval_id = approval.id

        event = SupervisorApprovalAuditEvent(
            approval_id=approval_id,
            action_id="act-defaults-1",
            lead_id=lead.id,
            actor_id="sys-user",
            actor_role="SUPERVISOR_ENGINE",
            event_type="CREATED",
            to_status="PENDING",
            context_fingerprint="fp-defaults",
        )
        session.add(event)
        await session.commit()
        event_id = event.id

    async with session_factory() as session:
        res = await session.execute(
            select(SupervisorHumanApproval).where(SupervisorHumanApproval.id == approval_id)
        )
        saved = res.scalar_one()
        assert saved.status == "PENDING"
        assert saved.version == 1
        assert saved.created_at is not None
        assert saved.updated_at is not None

        res_event = await session.execute(
            select(SupervisorApprovalAuditEvent).where(SupervisorApprovalAuditEvent.id == event_id)
        )
        saved_event = res_event.scalar_one()
        assert saved_event.metadata_json == {}
        assert saved_event.created_at is not None


@pytest.mark.asyncio
async def test_supervisor_approval_crud_and_persistence(session_factory):
    """Verify persisting, retrieving, updating and querying human approval records."""
    async with session_factory() as session:
        lead = Lead(company_name="Acme Corp")
        session.add(lead)
        await session.commit()
        lead_id = lead.id

        opp_id = uuid.uuid4()
        approval_id = uuid.uuid4()

        approval = SupervisorHumanApproval(
            id=approval_id,
            action_id="action-acme-outreach-1",
            lead_id=lead_id,
            opportunity_id=opp_id,
            contact_id=None,
            recommended_action="SEND_VALUE_PROPOSITION_EMAIL",
            action_tier="EXTERNAL_OUTREACH",
            channel="EMAIL",
            context_fingerprint="fp-acme-hash-1",
            requested_by_user_id="supervisor-daemon",
            notes="Ready for senior SDR review.",
        )
        session.add(approval)
        await session.commit()

    async with session_factory() as session:
        res = await session.execute(
            select(SupervisorHumanApproval).where(SupervisorHumanApproval.id == approval_id)
        )
        saved = res.scalar_one_or_none()
        assert saved is not None
        assert saved.action_id == "action-acme-outreach-1"
        assert saved.lead_id == lead_id
        assert saved.status == "PENDING"
        assert saved.version == 1
        assert saved.notes == "Ready for senior SDR review."
        assert saved.created_at is not None

        # Human approver approves action with 48h validity
        now = now_utc()
        saved.status = "APPROVED"
        saved.approved_by_user_id = "sdr-manager-jane"
        saved.approved_at = now
        saved.expires_at = now + timedelta(hours=DEFAULT_APPROVAL_VALIDITY_HOURS)
        saved.decision_reason = "Target matches current ICP and buyer persona."
        await session.commit()

    async with session_factory() as session:
        res = await session.execute(
            select(SupervisorHumanApproval).where(SupervisorHumanApproval.id == approval_id)
        )
        approved_saved = res.scalar_one_or_none()
        assert approved_saved is not None
        assert approved_saved.status == "APPROVED"
        assert approved_saved.approved_by_user_id == "sdr-manager-jane"
        assert approved_saved.decision_reason == "Target matches current ICP and buyer persona."
        assert approved_saved.expires_at > approved_saved.approved_at


@pytest.mark.asyncio
async def test_supervisor_approval_audit_trail_relationship(session_factory):
    """Verify bidirectional relationship between approval and audit event trail."""
    async with session_factory() as session:
        lead = Lead(company_name="Beta Dynamics")
        session.add(lead)
        await session.commit()

        approval = SupervisorHumanApproval(
            action_id="act-beta-1",
            lead_id=lead.id,
            opportunity_id=uuid.uuid4(),
            recommended_action="SCHEDULE_DISCOVERY_CALL",
            action_tier="EXTERNAL_OUTREACH",
            channel="PHONE",
            context_fingerprint="beta-fp-001",
            requested_by_user_id="pipeline-runner",
        )
        session.add(approval)
        await session.flush()

        event1 = SupervisorApprovalAuditEvent(
            approval_id=approval.id,
            action_id=approval.action_id,
            lead_id=lead.id,
            actor_id="pipeline-runner",
            actor_role="SUPERVISOR_ENGINE",
            event_type="CREATED",
            from_status=None,
            to_status="PENDING",
            context_fingerprint=approval.context_fingerprint,
            reason="Automated candidate discovery",
            metadata_json={"priority_score": 92.5},
        )
        event2 = SupervisorApprovalAuditEvent(
            approval_id=approval.id,
            action_id=approval.action_id,
            lead_id=lead.id,
            actor_id="rep-alex",
            actor_role="HUMAN_SUPERVISOR",
            event_type="APPROVED",
            from_status="PENDING",
            to_status="APPROVED",
            context_fingerprint=approval.context_fingerprint,
            reason="Reviewed and approved for calling window",
            metadata_json={"call_window": "afternoon"},
        )
        session.add_all([event1, event2])
        await session.commit()

    async with session_factory() as session:
        res = await session.execute(
            select(SupervisorHumanApproval).where(SupervisorHumanApproval.action_id == "act-beta-1")
        )
        loaded_approval = res.scalar_one()
        assert len(loaded_approval.audit_events) == 2
        assert loaded_approval.audit_events[0].event_type == "CREATED"
        assert loaded_approval.audit_events[0].metadata_json["priority_score"] == 92.5
        assert loaded_approval.audit_events[1].event_type == "APPROVED"
        assert loaded_approval.audit_events[1].actor_id == "rep-alex"
        assert loaded_approval.audit_events[1].approval.id == loaded_approval.id


@pytest.mark.asyncio
async def test_supervisor_approval_status_check_constraint(session_factory):
    """Verify status CheckConstraint rejects invalid statuses."""
    async with session_factory() as session:
        lead = Lead(company_name="Gamma LLC")
        session.add(lead)
        await session.commit()

        approval = SupervisorHumanApproval(
            action_id="act-gamma-1",
            lead_id=lead.id,
            opportunity_id=uuid.uuid4(),
            recommended_action="SCHEDULE_DISCOVERY_CALL",
            action_tier="EXTERNAL_OUTREACH",
            channel="EMAIL",
            context_fingerprint="gamma-fp",
            status="INVALID_STATUS_VALUE",
            requested_by_user_id="user-1",
        )
        session.add(approval)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_supervisor_approval_action_tier_check_constraint(session_factory):
    """Verify action_tier CheckConstraint rejects invalid tiers."""
    async with session_factory() as session:
        lead = Lead(company_name="Delta Systems")
        session.add(lead)
        await session.commit()

        approval = SupervisorHumanApproval(
            action_id="act-delta-1",
            lead_id=lead.id,
            opportunity_id=uuid.uuid4(),
            recommended_action="SCHEDULE_DISCOVERY_CALL",
            action_tier="UNCONTROLLED_EXECUTION",
            channel="EMAIL",
            context_fingerprint="delta-fp",
            requested_by_user_id="user-1",
        )
        session.add(approval)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_supervisor_approval_channel_check_constraint(session_factory):
    """Verify channel CheckConstraint accepts valid channels and rejects invalid ones."""
    async with session_factory() as session:
        lead = Lead(company_name="Epsilon Enterprises")
        session.add(lead)
        await session.commit()

        # Valid channels
        for valid_channel in ("EMAIL", "PHONE", "DIRECT", "MULTI_CHANNEL", None):
            approval = SupervisorHumanApproval(
                action_id=f"act-eps-{valid_channel}",
                lead_id=lead.id,
                opportunity_id=uuid.uuid4(),
                recommended_action="SCHEDULE_DISCOVERY_CALL",
                action_tier="INTERNAL_OPERATION",
                channel=valid_channel,
                context_fingerprint="eps-fp",
                requested_by_user_id="user-1",
            )
            session.add(approval)
        await session.commit()

        # Invalid channel
        bad_approval = SupervisorHumanApproval(
            action_id="act-eps-bad",
            lead_id=lead.id,
            opportunity_id=uuid.uuid4(),
            recommended_action="SCHEDULE_DISCOVERY_CALL",
            action_tier="EXTERNAL_OUTREACH",
            channel="SLACK_BOT_BROADCAST",
            context_fingerprint="eps-fp",
            requested_by_user_id="user-1",
        )
        session.add(bad_approval)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_supervisor_approval_active_uniqueness_partial_index(session_factory):
    """Verify partial unique index prevents concurrent active approvals for identical action & context."""
    async with session_factory() as session:
        lead = Lead(company_name="Zeta Technologies")
        session.add(lead)
        await session.commit()

        # Multiple PENDING approvals with same action_id and fingerprint are allowed
        app1 = SupervisorHumanApproval(
            action_id="zeta-act-dup",
            lead_id=lead.id,
            opportunity_id=uuid.uuid4(),
            recommended_action="SEND_PROPOSAL",
            action_tier="EXTERNAL_OUTREACH",
            channel="EMAIL",
            context_fingerprint="shared-zeta-fp",
            status="PENDING",
            requested_by_user_id="agent-1",
        )
        app2 = SupervisorHumanApproval(
            action_id="zeta-act-dup",
            lead_id=lead.id,
            opportunity_id=uuid.uuid4(),
            recommended_action="SEND_PROPOSAL",
            action_tier="EXTERNAL_OUTREACH",
            channel="EMAIL",
            context_fingerprint="shared-zeta-fp",
            status="PENDING",
            requested_by_user_id="agent-2",
        )
        session.add_all([app1, app2])
        await session.commit()
        app1_id = app1.id
        app2_id = app2.id

        # Approve the first one
        app1.status = "APPROVED"
        await session.commit()

        # Attempt to approve the second one with identical action_id & context_fingerprint
        app2.status = "APPROVED"
        with pytest.raises(IntegrityError):
            await session.commit()

        await session.rollback()

    # Verify that revoking or expiring frees the active slot
    async with session_factory() as session:
        res = await session.execute(
            select(SupervisorHumanApproval).where(SupervisorHumanApproval.id == app1_id)
        )
        loaded_app1 = res.scalar_one()
        loaded_app1.status = "REVOKED"
        loaded_app1.revoked_by_user_id = "security-admin"
        await session.commit()

        # Now app2 can be approved without collision
        res2 = await session.execute(
            select(SupervisorHumanApproval).where(SupervisorHumanApproval.id == app2_id)
        )
        loaded_app2 = res2.scalar_one()
        loaded_app2.status = "APPROVED"
        loaded_app2.approved_by_user_id = "manager-bob"
        await session.commit()
        assert loaded_app2.status == "APPROVED"


@pytest.mark.asyncio
async def test_supervisor_approval_optimistic_concurrency_cas(session_factory):
    """Verify atomic compare-and-swap (CAS) claiming semantics using version increment."""
    async with session_factory() as session:
        lead = Lead(company_name="Theta Logistics")
        session.add(lead)
        await session.commit()

        approval = SupervisorHumanApproval(
            action_id="act-theta-exec",
            lead_id=lead.id,
            opportunity_id=uuid.uuid4(),
            recommended_action="SEND_FOLLOW_UP_EMAIL",
            action_tier="EXTERNAL_OUTREACH",
            channel="EMAIL",
            context_fingerprint="theta-fp-123",
            status="APPROVED",
            requested_by_user_id="agent-1",
            approved_by_user_id="approver-1",
            version=1,
        )
        session.add(approval)
        await session.commit()
        approval_id = approval.id

    # Simulated Worker 1 claims execution atomically with expected version = 1
    now = now_utc()
    async with session_factory() as session:
        stmt = (
            update(SupervisorHumanApproval)
            .where(
                SupervisorHumanApproval.id == approval_id,
                SupervisorHumanApproval.version == 1,
                SupervisorHumanApproval.status == "APPROVED",
                SupervisorHumanApproval.execution_claimed_at.is_(None),
            )
            .values(
                version=SupervisorHumanApproval.version + 1,
                execution_claimed_at=now,
                execution_claimed_by="worker-pod-alpha",
            )
        )
        result1 = await session.execute(stmt)
        await session.commit()
        assert result1.rowcount == 1

    # Simulated Worker 2 attempts concurrent claim with version = 1
    async with session_factory() as session:
        stmt = (
            update(SupervisorHumanApproval)
            .where(
                SupervisorHumanApproval.id == approval_id,
                SupervisorHumanApproval.version == 1,
                SupervisorHumanApproval.status == "APPROVED",
                SupervisorHumanApproval.execution_claimed_at.is_(None),
            )
            .values(
                version=SupervisorHumanApproval.version + 1,
                execution_claimed_at=now,
                execution_claimed_by="worker-pod-beta",
            )
        )
        result2 = await session.execute(stmt)
        await session.commit()
        # Rowcount 0 indicates concurrency conflict detected; worker safely yields
        assert result2.rowcount == 0

    # Verify Worker 1 maintains claim
    async with session_factory() as session:
        res = await session.execute(
            select(SupervisorHumanApproval).where(SupervisorHumanApproval.id == approval_id)
        )
        claimed = res.scalar_one()
        assert claimed.version == 2
        assert claimed.execution_claimed_by == "worker-pod-alpha"


def test_audit_events_survive_lead_deletion():
    """Verify approval audit events survive deletion of associated Lead record while operational records cascade."""
    from sqlalchemy.orm import Session

    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON;"))
        conn.commit()
        Base.metadata.create_all(conn)
        conn.commit()

        with Session(conn) as session:
            session.execute(text("PRAGMA foreign_keys = ON;"))
            lead = Lead(company_name="Cascade Retention Corp")
            session.add(lead)
            session.commit()
            lead_id = lead.id

            approval = SupervisorHumanApproval(
                action_id="act-retention-1",
                lead_id=lead_id,
                opportunity_id=uuid.uuid4(),
                recommended_action="SCHEDULE_DISCOVERY_CALL",
                action_tier="EXTERNAL_OUTREACH",
                channel="EMAIL",
                context_fingerprint="retention-fp-1",
                requested_by_user_id="user-sys",
            )
            session.add(approval)
            session.commit()
            approval_id = approval.id

            event1 = SupervisorApprovalAuditEvent(
                approval_id=approval_id,
                action_id="act-retention-1",
                lead_id=lead_id,
                actor_id="user-sys",
                actor_role="SUPERVISOR_ENGINE",
                event_type="CREATED",
                to_status="PENDING",
                context_fingerprint="retention-fp-1",
                metadata_json={"priority": "HIGH"},
            )
            event2 = SupervisorApprovalAuditEvent(
                approval_id=approval_id,
                action_id="act-retention-1",
                lead_id=lead_id,
                actor_id="human-mgr",
                actor_role="HUMAN_SUPERVISOR",
                event_type="APPROVED",
                from_status="PENDING",
                to_status="APPROVED",
                context_fingerprint="retention-fp-1",
                metadata_json={"notes": "Approved for outreach"},
            )
            session.add_all([event1, event2])
            session.commit()
            event1_id = event1.id
            event2_id = event2.id

            # Verify operational lead and approval exist prior to deletion
            assert session.get(Lead, lead_id) is not None
            assert session.get(SupervisorHumanApproval, approval_id) is not None

            # Operational lead deletion: operational lead and approval must be deleted
            session.delete(lead)
            session.commit()

            # Verify operational records are gone
            assert session.get(Lead, lead_id) is None
            assert session.get(SupervisorHumanApproval, approval_id) is None

            # Verify audit events survived!
            survived_event1 = session.get(SupervisorApprovalAuditEvent, event1_id)
            survived_event2 = session.get(SupervisorApprovalAuditEvent, event2_id)
            assert survived_event1 is not None
            assert survived_event2 is not None

            # Verify foreign key was set to NULL
            assert survived_event1.approval_id is None
            assert survived_event2.approval_id is None

            # Verify immutable approval reference is preserved!
            assert survived_event1.approval_reference_id == approval_id
            assert survived_event2.approval_reference_id == approval_id

            # Verify lead_id, action_id, metadata are preserved intact!
            assert survived_event1.lead_id == lead_id
            assert survived_event2.lead_id == lead_id
            assert survived_event1.action_id == "act-retention-1"
            assert survived_event1.metadata_json == {"priority": "HIGH"}
            assert survived_event2.metadata_json == {"notes": "Approved for outreach"}
    engine.dispose()


def test_audit_events_survive_direct_approval_deletion():
    """Verify approval audit events survive direct deletion of parent approval record."""
    from sqlalchemy.orm import Session

    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON;"))
        conn.commit()
        Base.metadata.create_all(conn)
        conn.commit()

        with Session(conn) as session:
            session.execute(text("PRAGMA foreign_keys = ON;"))
            lead = Lead(company_name="Direct Deletion LLC")
            session.add(lead)
            session.commit()
            lead_id = lead.id

            approval = SupervisorHumanApproval(
                action_id="act-direct-del-1",
                lead_id=lead_id,
                opportunity_id=uuid.uuid4(),
                recommended_action="SEND_PROPOSAL",
                action_tier="EXTERNAL_OUTREACH",
                channel="EMAIL",
                context_fingerprint="direct-fp-1",
                requested_by_user_id="user-sys",
            )
            session.add(approval)
            session.commit()
            approval_id = approval.id

            event = SupervisorApprovalAuditEvent(
                approval_id=approval_id,
                action_id="act-direct-del-1",
                lead_id=lead_id,
                actor_id="user-sys",
                actor_role="SUPERVISOR_ENGINE",
                event_type="CREATED",
                to_status="PENDING",
                context_fingerprint="direct-fp-1",
            )
            session.add(event)
            session.commit()
            event_id = event.id

            # Delete approval directly
            session.delete(approval)
            session.commit()

            # Verify approval is deleted
            assert session.get(SupervisorHumanApproval, approval_id) is None
            # Lead remains unaffected
            assert session.get(Lead, lead_id) is not None

            # Verify audit event survives with immutable approval reference
            survived = session.get(SupervisorApprovalAuditEvent, event_id)
            assert survived is not None
            assert survived.approval_id is None
            assert survived.approval_reference_id == approval_id
            assert survived.lead_id == lead_id
    engine.dispose()


def test_alembic_0038_upgrade_and_downgrade():
    """Verify Alembic migration 0038 upgrades and downgrades cleanly on isolated database."""
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0038_supervisor_human_approvals.py"
    )
    spec = importlib.util.spec_from_file_location("m0038", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        # Simulate baseline database state prior to 0038 (leads table present)
        conn.execute(
            text(
                "CREATE TABLE leads ("
                "id CHAR(32) PRIMARY KEY, "
                "company_name VARCHAR(255)"
                ")"
            )
        )
        conn.commit()

        ctx = MigrationContext.configure(conn)
        op = Operations(ctx)
        orig_op = migration.op
        migration.op = op
        try:
            # Upgrade
            migration.upgrade()
            conn.commit()

            insp = inspect(conn)
            tables = set(insp.get_table_names())
            assert "supervisor_human_approvals" in tables
            assert "supervisor_approval_audit_events" in tables

            # Verify indexes on supervisor_human_approvals
            approval_indexes = {ix["name"] for ix in insp.get_indexes("supervisor_human_approvals")}
            assert "uq_supervisor_approvals_active_action" in approval_indexes
            assert "ix_supervisor_approvals_lead_created" in approval_indexes
            assert "ix_supervisor_approvals_status_expiry" in approval_indexes
            assert "ix_supervisor_approvals_action_id" in approval_indexes

            # Verify indexes on supervisor_approval_audit_events
            audit_indexes = {ix["name"] for ix in insp.get_indexes("supervisor_approval_audit_events")}
            assert "ix_supervisor_audit_approval_created" in audit_indexes
            assert "ix_supervisor_audit_action_created" in audit_indexes
            assert "ix_supervisor_audit_lead_created" in audit_indexes

            # Downgrade
            migration.downgrade()
            conn.commit()

            insp_after = inspect(conn)
            tables_after = set(insp_after.get_table_names())
            assert "supervisor_human_approvals" not in tables_after
            assert "supervisor_approval_audit_events" not in tables_after
            # Baseline table remains intact
            assert "leads" in tables_after
        finally:
            migration.op = orig_op
    engine.dispose()


def test_alembic_0039_upgrade_and_downgrade_preserves_audit_rows():
    """Verify Alembic migration 0039 upgrades, backfills existing rows, supports audit retention, and downgrades cleanly."""
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0039_supervisor_approval_audit_retention.py"
    )
    spec = importlib.util.spec_from_file_location("m0039", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON;"))
        # Set up schema as created by 0038
        conn.execute(text("CREATE TABLE leads (id CHAR(32) PRIMARY KEY, company_name VARCHAR(255))"))
        conn.execute(
            text(
                "CREATE TABLE supervisor_human_approvals ("
                "id CHAR(32) PRIMARY KEY, "
                "action_id VARCHAR(180), "
                "lead_id CHAR(32) REFERENCES leads(id) ON DELETE CASCADE"
                ")"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE supervisor_approval_audit_events ("
                "id CHAR(32) PRIMARY KEY, "
                "approval_id CHAR(32) NOT NULL, "
                "action_id VARCHAR(180) NOT NULL, "
                "lead_id CHAR(32) NOT NULL, "
                "actor_id VARCHAR(120) NOT NULL, "
                "actor_role VARCHAR(60) NOT NULL, "
                "event_type VARCHAR(40) NOT NULL, "
                "from_status VARCHAR(20), "
                "to_status VARCHAR(20) NOT NULL, "
                "context_fingerprint VARCHAR(64) NOT NULL, "
                "reason VARCHAR(500), "
                "metadata_json JSON NOT NULL, "
                "created_at DATETIME NOT NULL, "
                "CONSTRAINT fk_supervisor_audit_approval FOREIGN KEY (approval_id) REFERENCES supervisor_human_approvals(id) ON DELETE CASCADE"
                ")"
            )
        )

        # Insert pre-existing row to verify row preservation during 0039 migration
        appr_id = "appr1111222233334444555566667777"
        event_id = "ev1111222233334444555566667777"
        lead_id = "lead1111222233334444555566667777"
        conn.execute(text(f"INSERT INTO leads VALUES ('{lead_id}', 'Migrate Corp')"))
        conn.execute(text(f"INSERT INTO supervisor_human_approvals VALUES ('{appr_id}', 'act-mig-1', '{lead_id}')"))
        conn.execute(
            text(
                f"INSERT INTO supervisor_approval_audit_events VALUES ("
                f"'{event_id}', '{appr_id}', 'act-mig-1', '{lead_id}', 'actor-1', 'ENGINE', "
                f"'CREATED', NULL, 'PENDING', 'mig-fp-1', 'reason', '{{}}', '2026-10-26 12:00:00')"
            )
        )
        conn.commit()

        ctx = MigrationContext.configure(conn)
        op = Operations(ctx)
        orig_op = migration.op
        migration.op = op
        try:
            # Upgrade to 0039
            migration.upgrade()
            conn.commit()

            # Verify existing row preserved and approval_reference_id was backfilled from approval_id
            row = conn.execute(
                text("SELECT id, approval_id, approval_reference_id FROM supervisor_approval_audit_events WHERE id = :eid"),
                {"eid": event_id},
            ).fetchone()
            assert row is not None, "Pre-existing audit event was lost during migration!"
            assert row[1] == appr_id, "approval_id changed unexpectedly!"
            assert row[2] == appr_id, "approval_reference_id was not backfilled from approval_id!"

            # Verify index on approval_reference_id exists
            insp = inspect(conn)
            audit_indexes = {ix["name"] for ix in insp.get_indexes("supervisor_approval_audit_events")}
            assert "ix_supervisor_audit_approval_ref_created" in audit_indexes

            # Test retention behavior on migrated database: delete lead -> approval deleted -> audit event preserved
            conn.execute(text(f"DELETE FROM leads WHERE id = '{lead_id}'"))
            conn.commit()

            # Approval should be deleted by cascade from leads
            appr_row = conn.execute(text(f"SELECT id FROM supervisor_human_approvals WHERE id = '{appr_id}'")).fetchone()
            assert appr_row is None

            # Audit event must survive, approval_id becomes NULL, approval_reference_id remains!
            event_row_after = conn.execute(
                text("SELECT id, approval_id, approval_reference_id FROM supervisor_approval_audit_events WHERE id = :eid"),
                {"eid": event_id},
            ).fetchone()
            assert event_row_after is not None, "Audit event did not survive deletion!"
            assert event_row_after[1] is None, "approval_id was not set to NULL!"
            assert event_row_after[2] == appr_id, "approval_reference_id was not retained!"

            # Downgrade 0039 back to 0038
            migration.downgrade()
            conn.commit()

            insp_after = inspect(conn)
            cols_after = [c["name"] for c in insp_after.get_columns("supervisor_approval_audit_events")]
            assert "approval_reference_id" not in cols_after
        finally:
            migration.op = orig_op
    engine.dispose()

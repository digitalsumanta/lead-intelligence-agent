"""
Durable repository for SupervisorHumanApproval and SupervisorApprovalAuditEvent entities.

Encapsulates database persistence, transactional lifecycle state transitions,
atomic CAS execution claiming, partial uniqueness conflict handling,
and immutable audit log preservation.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sales_supervisor import (
    ApprovalClaimConflictError,
    ApprovalContextDriftError,
    ApprovalFencingConflictError,
    ApprovalInvalidTransitionError,
    ApprovalLeaseActiveError,
    ApprovalLeaseExpiredError,
    ApprovalNotFoundError,
    ApprovalNotLeaseOwnerError,
    ApprovalVersionConflictError,
    validate_approval_transition,
)
from app.models.entities import (
    DEFAULT_APPROVAL_VALIDITY_HOURS,
    SupervisorApprovalAuditEvent,
    SupervisorHumanApproval,
    now_utc,
)

logger = logging.getLogger("lead_agent.supervisor_approval_repository")


def _ensure_utc(dt: Optional[datetime]) -> datetime:
    """Ensure datetime is non-null and timezone-aware UTC."""
    if dt is None:
        return now_utc()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class SupervisorApprovalRepository:
    """
    Data access repository for durable supervisor human approvals and audit events.
    All state mutations and their corresponding audit events occur in the same
    database transaction boundary.
    """

    @staticmethod
    async def create_approval(
        session: AsyncSession,
        *,
        action_id: str,
        lead_id: uuid.UUID,
        opportunity_id: uuid.UUID,
        contact_id: Optional[uuid.UUID] = None,
        recommended_action: str,
        action_tier: str,
        channel: Optional[str] = None,
        context_fingerprint: str,
        requested_by_user_id: str,
        expires_in_hours: int = DEFAULT_APPROVAL_VALIDITY_HOURS,
        notes: Optional[str] = None,
        actor_role: str = "SUPERVISOR_ENGINE",
        metadata: Optional[dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> SupervisorHumanApproval:
        """
        Creates and persists a new PENDING approval record along with its initial
        CREATED audit event in a single atomic database transaction.
        """
        eval_time = _ensure_utc(now)
        expires_at = eval_time + timedelta(hours=expires_in_hours) if expires_in_hours > 0 else None
        approval_id = uuid.uuid4()

        approval = SupervisorHumanApproval(
            id=approval_id,
            action_id=action_id.strip(),
            lead_id=lead_id,
            opportunity_id=opportunity_id,
            contact_id=contact_id,
            recommended_action=recommended_action.strip(),
            action_tier=action_tier.strip(),
            channel=channel.strip().upper() if channel else None,
            context_fingerprint=context_fingerprint.strip(),
            status="PENDING",
            requested_by_user_id=requested_by_user_id.strip(),
            created_at=eval_time,
            updated_at=eval_time,
            expires_at=expires_at,
            notes=notes,
            version=1,
        )

        audit_event = SupervisorApprovalAuditEvent(
            id=uuid.uuid4(),
            approval_id=approval_id,
            approval_reference_id=approval_id,
            action_id=action_id.strip(),
            lead_id=lead_id,
            actor_id=requested_by_user_id.strip(),
            actor_role=actor_role.strip(),
            event_type="CREATED",
            from_status=None,
            to_status="PENDING",
            context_fingerprint=context_fingerprint.strip(),
            reason="Approval request registered",
            metadata_json=metadata or {},
            created_at=eval_time,
        )

        session.add(approval)
        session.add(audit_event)
        await session.flush()
        return approval

    @staticmethod
    async def get_by_id(
        session: AsyncSession,
        approval_id: uuid.UUID,
    ) -> Optional[SupervisorHumanApproval]:
        """Loads an approval record by its primary key UUID."""
        stmt = select(SupervisorHumanApproval).where(SupervisorHumanApproval.id == approval_id)
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def get_by_id_for_update(
        session: AsyncSession,
        approval_id: uuid.UUID,
    ) -> Optional[SupervisorHumanApproval]:
        """Loads an approval record with pessimistic row-level locking (SELECT ... FOR UPDATE)."""
        stmt = (
            select(SupervisorHumanApproval)
            .where(SupervisorHumanApproval.id == approval_id)
            .with_for_update()
        )
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def get_active_approved(
        session: AsyncSession,
        action_id: str,
        context_fingerprint: Optional[str] = None,
    ) -> Optional[SupervisorHumanApproval]:
        """
        Retrieves the active APPROVED approval for an action.
        If context_fingerprint is given, filters strictly by fingerprint match.
        """
        conditions = [
            SupervisorHumanApproval.action_id == action_id.strip(),
            SupervisorHumanApproval.status == "APPROVED",
        ]
        if context_fingerprint is not None:
            conditions.append(SupervisorHumanApproval.context_fingerprint == context_fingerprint.strip())

        stmt = select(SupervisorHumanApproval).where(*conditions).order_by(SupervisorHumanApproval.created_at.desc())
        res = await session.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def list_approvals_for_action(
        session: AsyncSession,
        action_id: str,
        status: Optional[str] = None,
    ) -> list[SupervisorHumanApproval]:
        """Lists all approvals for an action ID ordered by created_at descending."""
        conditions = [SupervisorHumanApproval.action_id == action_id.strip()]
        if status is not None:
            conditions.append(SupervisorHumanApproval.status == status.strip().upper())

        stmt = select(SupervisorHumanApproval).where(*conditions).order_by(SupervisorHumanApproval.created_at.desc())
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def list_approvals(
        session: AsyncSession,
        *,
        lead_id: Optional[uuid.UUID] = None,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SupervisorHumanApproval]:
        """Lists approvals with optional lead_id and status filters."""
        conditions = []
        if lead_id is not None:
            conditions.append(SupervisorHumanApproval.lead_id == lead_id)
        if status is not None:
            conditions.append(SupervisorHumanApproval.status == status.strip().upper())

        stmt = (
            select(SupervisorHumanApproval)
            .where(*conditions)
            .order_by(SupervisorHumanApproval.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def transition_status(
        session: AsyncSession,
        *,
        approval_id: uuid.UUID,
        to_status: str,
        actor_id: str,
        actor_role: str,
        reason: Optional[str] = None,
        notes: Optional[str] = None,
        expected_version: Optional[int] = None,
        expires_at: Optional[datetime] = None,
        metadata: Optional[dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> SupervisorHumanApproval:
        """
        Transitions an approval record's status, increments version, updates actor metadata,
        and atomically records the lifecycle audit event.
        Guarantees that terminal states cannot be resurrected and conflicting versions fail closed.
        """
        eval_time = _ensure_utc(now)
        target_status = to_status.strip().upper()

        approval = await SupervisorApprovalRepository.get_by_id_for_update(session, approval_id)
        if approval is None:
            raise ApprovalNotFoundError(f"Approval record with ID '{approval_id}' does not exist")

        # Concurrency version check
        if expected_version is not None and approval.version != expected_version:
            raise ApprovalVersionConflictError(expected_version, approval.version)

        # Validate legal lifecycle transition
        from_status = approval.status
        validate_approval_transition(from_status, target_status)

        # Apply state changes to approval record
        approval.status = target_status
        approval.version = approval.version + 1
        approval.updated_at = eval_time

        if notes is not None:
            approval.notes = notes
        if reason is not None:
            approval.decision_reason = reason

        if target_status == "APPROVED":
            approval.approved_by_user_id = actor_id.strip()
            approval.approved_at = eval_time
            if expires_at is not None:
                approval.expires_at = _ensure_utc(expires_at)
        elif target_status == "REJECTED":
            approval.rejected_by_user_id = actor_id.strip()
        elif target_status == "REVOKED":
            approval.revoked_by_user_id = actor_id.strip()
        elif target_status == "EXPIRED":
            approval.expires_at = eval_time

        # Create corresponding audit event
        audit_event = SupervisorApprovalAuditEvent(
            id=uuid.uuid4(),
            approval_id=approval.id,
            approval_reference_id=approval.id,
            action_id=approval.action_id,
            lead_id=approval.lead_id,
            actor_id=actor_id.strip(),
            actor_role=actor_role.strip(),
            event_type=target_status,
            from_status=from_status,
            to_status=target_status,
            context_fingerprint=approval.context_fingerprint,
            reason=reason,
            metadata_json=metadata or {},
            created_at=eval_time,
        )

        session.add(audit_event)
        await session.flush()
        return approval

    @staticmethod
    async def claim_execution(
        session: AsyncSession,
        *,
        approval_id: uuid.UUID,
        worker_id: str,
        expected_version: int,
        expected_context_fingerprint: Optional[str] = None,
        now: Optional[datetime] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SupervisorHumanApproval:
        """
        Atomically claims an APPROVED approval for execution using an atomic compare-and-swap (CAS)
        UPDATE condition bound to version and context fingerprint.
        Ensures exactly one worker succeeds and all competing claims fail closed.
        """
        eval_time = _ensure_utc(now)

        # 1. Execute atomic conditional CAS update
        where_conditions = [
            SupervisorHumanApproval.id == approval_id,
            SupervisorHumanApproval.status == "APPROVED",
            SupervisorHumanApproval.version == expected_version,
            SupervisorHumanApproval.execution_claimed_at.is_(None),
        ]
        if expected_context_fingerprint is not None:
            where_conditions.append(
                SupervisorHumanApproval.context_fingerprint == expected_context_fingerprint
            )

        stmt = (
            update(SupervisorHumanApproval)
            .where(*where_conditions)
            .values(
                version=SupervisorHumanApproval.version + 1,
                execution_claimed_at=eval_time,
                execution_claimed_by=worker_id.strip(),
                updated_at=eval_time,
            )
        )
        result = await session.execute(stmt)

        # 2. Check if CAS claim succeeded
        if result.rowcount == 0:
            # Query existing row state to return precise diagnostics
            existing = await SupervisorApprovalRepository.get_by_id(session, approval_id)
            if existing is None:
                raise ApprovalNotFoundError(f"Approval record with ID '{approval_id}' does not exist")
            if existing.status != "APPROVED":
                raise ApprovalInvalidTransitionError(
                    existing.status, "CLAIMED", f"Approval is in status '{existing.status}', expected 'APPROVED'"
                )
            if existing.execution_claimed_at is not None:
                raise ApprovalClaimConflictError(
                    f"Approval was already claimed at {existing.execution_claimed_at.isoformat()} by '{existing.execution_claimed_by}'"
                )
            if existing.version != expected_version:
                raise ApprovalVersionConflictError(expected_version, existing.version)
            if (
                expected_context_fingerprint is not None
                and existing.context_fingerprint != expected_context_fingerprint
            ):
                raise ApprovalContextDriftError(
                    f"Action context drifted: expected fingerprint '{expected_context_fingerprint}', but approval has '{existing.context_fingerprint}'"
                )
            raise ApprovalClaimConflictError("Claim failed due to concurrency conflict")

        # 3. Reload claimed record
        claimed = await SupervisorApprovalRepository.get_by_id(session, approval_id)
        if claimed is None:
            raise ApprovalNotFoundError(f"Claimed approval '{approval_id}' could not be reloaded")

        # 4. Atomically insert CLAIMED audit event in the same transaction
        audit_meta = dict(metadata or {})
        audit_meta["claimed_by"] = worker_id.strip()
        audit_meta["claimed_at"] = eval_time.isoformat()

        audit_event = SupervisorApprovalAuditEvent(
            id=uuid.uuid4(),
            approval_id=claimed.id,
            approval_reference_id=claimed.id,
            action_id=claimed.action_id,
            lead_id=claimed.lead_id,
            actor_id=worker_id.strip(),
            actor_role="SYSTEM_WORKER",
            event_type="CLAIMED",
            from_status="APPROVED",
            to_status="APPROVED",
            context_fingerprint=claimed.context_fingerprint,
            reason=f"Execution claimed by worker '{worker_id.strip()}'",
            metadata_json=audit_meta,
            created_at=eval_time,
        )
        session.add(audit_event)
        await session.flush()
        return claimed

    @staticmethod
    async def heartbeat_lease(
        session: AsyncSession,
        *,
        approval_id: uuid.UUID,
        worker_id: str,
        lease_generation: int,
        lease_duration_seconds: int = 300,
        now: Optional[datetime] = None,
    ) -> SupervisorHumanApproval:
        """
        Validates and records a heartbeat from the active lease owner, extending updated_at.
        Enforces fencing: rejects stale generation, non-owner, expired lease, or non-APPROVED status.
        """
        eval_time = _ensure_utc(now)

        # 1. Pessimistic lock on the approval row
        stmt = (
            select(SupervisorHumanApproval)
            .where(SupervisorHumanApproval.id == approval_id)
            .with_for_update()
        )
        res = await session.execute(stmt)
        approval = res.scalar_one_or_none()
        if approval is None:
            raise ApprovalNotFoundError(f"Approval record with ID '{approval_id}' does not exist")

        # 2. Status check
        if approval.status != "APPROVED":
            raise ApprovalInvalidTransitionError(
                approval.status, "HEARTBEAT", f"Cannot heartbeat approval in status '{approval.status}', expected 'APPROVED'"
            )

        # 3. Ownership check
        if approval.execution_claimed_by is None or approval.execution_claimed_by.strip() != worker_id.strip():
            raise ApprovalNotLeaseOwnerError(
                f"Worker '{worker_id.strip()}' is not the current lease owner (current owner: '{approval.execution_claimed_by}')"
            )

        # 4. Fencing token / generation check
        if approval.version != lease_generation:
            raise ApprovalFencingConflictError(
                f"Heartbeat rejected: Lease generation {lease_generation} does not match current version {approval.version}"
            )

        # 5. Lease expiration check: Cannot revive an expired lease
        lease_cutoff = _ensure_utc(approval.updated_at) + timedelta(seconds=lease_duration_seconds)
        if eval_time > lease_cutoff:
            raise ApprovalLeaseExpiredError(
                f"Execution lease expired at {lease_cutoff.isoformat()}; cannot extend via heartbeat"
            )

        # Also check hard approval expires_at
        if approval.expires_at is not None and eval_time > _ensure_utc(approval.expires_at):
            raise ApprovalLeaseExpiredError(
                f"Approval hard expiration passed at {approval.expires_at.isoformat()}; cannot extend lease"
            )

        # 6. Update updated_at to eval_time
        approval.updated_at = eval_time
        await session.flush()
        return approval

    @staticmethod
    async def recover_stale_claim(
        session: AsyncSession,
        *,
        approval_id: uuid.UUID,
        lease_duration_seconds: int = 300,
        expected_worker_id: Optional[str] = None,
        expected_generation: Optional[int] = None,
        actor_id: str = "SYSTEM_RECOVERY",
        actor_role: str = "SYSTEM",
        reason: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> tuple[SupervisorHumanApproval, bool]:
        """
        Recovers an expired execution lease under a pessimistic lock.
        Atomically clears execution_claimed_at and execution_claimed_by, increments version (fencing out stale worker),
        and records a LEASE_RECOVERED audit event.
        If overall expires_at has passed, transitions to EXPIRED instead.
        Returns (approval, is_hard_expired).
        """
        eval_time = _ensure_utc(now)

        # 1. Pessimistic lock
        stmt = (
            select(SupervisorHumanApproval)
            .where(SupervisorHumanApproval.id == approval_id)
            .with_for_update()
        )
        res = await session.execute(stmt)
        approval = res.scalar_one_or_none()
        if approval is None:
            raise ApprovalNotFoundError(f"Approval record with ID '{approval_id}' does not exist")

        if approval.status != "APPROVED":
            raise ApprovalInvalidTransitionError(
                approval.status, "RECOVER", f"Cannot recover lease for approval in status '{approval.status}'"
            )

        if approval.execution_claimed_at is None:
            raise ApprovalClaimConflictError(
                f"Approval '{approval_id}' is not currently claimed; no lease to recover"
            )

        # If expected worker or generation supplied, validate
        if expected_worker_id is not None and approval.execution_claimed_by != expected_worker_id.strip():
            raise ApprovalNotLeaseOwnerError(
                f"Expected worker '{expected_worker_id}', but approval is owned by '{approval.execution_claimed_by}'"
            )
        if expected_generation is not None and approval.version != expected_generation:
            raise ApprovalFencingConflictError(
                f"Expected generation {expected_generation}, but approval is at version {approval.version}"
            )

        # 2. Expiration verification: Must be genuinely expired
        lease_cutoff = _ensure_utc(approval.updated_at) + timedelta(seconds=lease_duration_seconds)
        hard_expired = approval.expires_at is not None and eval_time > _ensure_utc(approval.expires_at)
        lease_expired = eval_time > lease_cutoff

        if not lease_expired and not hard_expired:
            raise ApprovalLeaseActiveError(
                f"Lease for approval '{approval_id}' is still active until {lease_cutoff.isoformat()}; cannot recover"
            )

        stale_worker = approval.execution_claimed_by or "unknown"
        stale_generation = approval.version

        # 3. Handle hard expiration vs returning to claim pool
        if hard_expired:
            approval.status = "EXPIRED"
            approval.expires_at = eval_time
            approval.execution_claimed_at = None
            approval.execution_claimed_by = None
            approval.version = approval.version + 1
            approval.updated_at = eval_time
            if reason:
                approval.decision_reason = reason

            event_type = "EXPIRED"
            audit_reason = reason or f"Approval hard expired during lease held by worker '{stale_worker}'"
            to_status = "EXPIRED"
        else:
            approval.execution_claimed_at = None
            approval.execution_claimed_by = None
            approval.version = approval.version + 1
            approval.updated_at = eval_time

            event_type = "LEASE_RECOVERED"
            audit_reason = reason or f"Stale execution lease recovered from worker '{stale_worker}' (expired at {lease_cutoff.isoformat()})"
            to_status = "APPROVED"

        audit_meta = {
            "recovered_from_worker": stale_worker,
            "recovered_generation": stale_generation,
            "new_generation": approval.version,
            "lease_duration_seconds": lease_duration_seconds,
            "recovered_at": eval_time.isoformat(),
        }

        audit_event = SupervisorApprovalAuditEvent(
            id=uuid.uuid4(),
            approval_id=approval.id,
            approval_reference_id=approval.id,
            action_id=approval.action_id,
            lead_id=approval.lead_id,
            actor_id=actor_id.strip(),
            actor_role=actor_role.strip(),
            event_type=event_type,
            from_status="APPROVED",
            to_status=to_status,
            context_fingerprint=approval.context_fingerprint,
            reason=audit_reason,
            metadata_json=audit_meta,
            created_at=eval_time,
        )
        session.add(audit_event)
        await session.flush()
        return approval, hard_expired

    @staticmethod
    async def release_lease(
        session: AsyncSession,
        *,
        approval_id: uuid.UUID,
        worker_id: str,
        lease_generation: int,
        reason: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> SupervisorHumanApproval:
        """
        Cleanly releases a worker lease back to the pool before expiration.
        Atomically clears execution_claimed_at and execution_claimed_by, increments version,
        and logs a LEASE_RELEASED audit event.
        """
        eval_time = _ensure_utc(now)

        stmt = (
            select(SupervisorHumanApproval)
            .where(SupervisorHumanApproval.id == approval_id)
            .with_for_update()
        )
        res = await session.execute(stmt)
        approval = res.scalar_one_or_none()
        if approval is None:
            raise ApprovalNotFoundError(f"Approval record with ID '{approval_id}' does not exist")

        if approval.status != "APPROVED":
            raise ApprovalInvalidTransitionError(
                approval.status, "RELEASE", f"Cannot release lease for approval in status '{approval.status}'"
            )

        if approval.execution_claimed_by is None or approval.execution_claimed_by.strip() != worker_id.strip():
            raise ApprovalNotLeaseOwnerError(
                f"Worker '{worker_id.strip()}' is not the current lease owner (current owner: '{approval.execution_claimed_by}')"
            )

        if approval.version != lease_generation:
            raise ApprovalFencingConflictError(
                f"Release rejected: Lease generation {lease_generation} does not match current version {approval.version}"
            )

        old_generation = approval.version
        approval.execution_claimed_at = None
        approval.execution_claimed_by = None
        approval.version = approval.version + 1
        approval.updated_at = eval_time

        audit_meta = {
            "released_by_worker": worker_id.strip(),
            "released_generation": old_generation,
            "new_generation": approval.version,
            "released_at": eval_time.isoformat(),
        }

        audit_event = SupervisorApprovalAuditEvent(
            id=uuid.uuid4(),
            approval_id=approval.id,
            approval_reference_id=approval.id,
            action_id=approval.action_id,
            lead_id=approval.lead_id,
            actor_id=worker_id.strip(),
            actor_role="SYSTEM_WORKER",
            event_type="LEASE_RELEASED",
            from_status="APPROVED",
            to_status="APPROVED",
            context_fingerprint=approval.context_fingerprint,
            reason=reason or f"Execution lease voluntarily released by worker '{worker_id.strip()}'",
            metadata_json=audit_meta,
            created_at=eval_time,
        )
        session.add(audit_event)
        await session.flush()
        return approval

    @staticmethod
    async def list_stale_claims(
        session: AsyncSession,
        *,
        lease_duration_seconds: int = 300,
        now: Optional[datetime] = None,
        limit: int = 100,
    ) -> Sequence[SupervisorHumanApproval]:
        """
        Lists all APPROVED records with an active claim whose updated_at + lease_duration_seconds has passed.
        """
        eval_time = _ensure_utc(now)
        stale_threshold = eval_time - timedelta(seconds=lease_duration_seconds)

        stmt = (
            select(SupervisorHumanApproval)
            .where(
                SupervisorHumanApproval.status == "APPROVED",
                SupervisorHumanApproval.execution_claimed_at.is_not(None),
                or_(
                    SupervisorHumanApproval.updated_at <= stale_threshold,
                    and_(
                        SupervisorHumanApproval.expires_at.is_not(None),
                        SupervisorHumanApproval.expires_at <= eval_time,
                    ),
                ),
            )
            .order_by(SupervisorHumanApproval.updated_at.asc())
            .limit(limit)
        )
        res = await session.execute(stmt)
        return res.scalars().all()

    @staticmethod
    async def list_audit_events_for_approval(
        session: AsyncSession,
        approval_reference_id: uuid.UUID,
    ) -> list[SupervisorApprovalAuditEvent]:
        """
        Retrieves all historical audit events for an approval UUID using approval_reference_id.
        Preserves complete forensic auditability even after the operational approval is deleted.
        """
        stmt = (
            select(SupervisorApprovalAuditEvent)
            .where(
                or_(
                    SupervisorApprovalAuditEvent.approval_reference_id == approval_reference_id,
                    SupervisorApprovalAuditEvent.approval_id == approval_reference_id,
                )
            )
            .order_by(SupervisorApprovalAuditEvent.created_at.asc())
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def list_audit_events_for_lead(
        session: AsyncSession,
        lead_id: uuid.UUID,
    ) -> list[SupervisorApprovalAuditEvent]:
        """Retrieves audit events associated with a lead UUID."""
        stmt = (
            select(SupervisorApprovalAuditEvent)
            .where(SupervisorApprovalAuditEvent.lead_id == lead_id)
            .order_by(SupervisorApprovalAuditEvent.created_at.asc())
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def list_audit_events_for_action(
        session: AsyncSession,
        action_id: str,
    ) -> list[SupervisorApprovalAuditEvent]:
        """Retrieves audit events associated with an action ID."""
        stmt = (
            select(SupervisorApprovalAuditEvent)
            .where(SupervisorApprovalAuditEvent.action_id == action_id.strip())
            .order_by(SupervisorApprovalAuditEvent.created_at.asc())
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def sweep_expired_approvals(
        session: AsyncSession,
        now: Optional[datetime] = None,
    ) -> list[SupervisorHumanApproval]:
        """
        Sweeps PENDING and APPROVED approvals whose expires_at is past eval_time,
        transitions them to EXPIRED, and logs audit events.
        """
        eval_time = _ensure_utc(now)
        stmt = (
            select(SupervisorHumanApproval)
            .where(
                SupervisorHumanApproval.status.in_(["PENDING", "APPROVED"]),
                SupervisorHumanApproval.expires_at.is_not(None),
                SupervisorHumanApproval.expires_at <= eval_time,
            )
            .with_for_update()
        )
        res = await session.execute(stmt)
        expired_records = list(res.scalars().all())

        results = []
        for rec in expired_records:
            from_status = rec.status
            rec.status = "EXPIRED"
            rec.version = rec.version + 1
            rec.updated_at = eval_time

            audit = SupervisorApprovalAuditEvent(
                id=uuid.uuid4(),
                approval_id=rec.id,
                approval_reference_id=rec.id,
                action_id=rec.action_id,
                lead_id=rec.lead_id,
                actor_id="system_sweeper",
                actor_role="SYSTEM_SERVICE",
                event_type="EXPIRED",
                from_status=from_status,
                to_status="EXPIRED",
                context_fingerprint=rec.context_fingerprint,
                reason="Approval expired by scheduled sweeper",
                metadata_json={"expired_at": eval_time.isoformat()},
                created_at=eval_time,
            )
            session.add(audit)
            results.append(rec)

        if results:
            await session.flush()
        return results

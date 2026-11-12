"""
Durable repository for SupervisorApprovalDispatch entities.

Phase 12J - D2 - Gate D-B2:
Encapsulates PostgreSQL outbox querying with FOR UPDATE SKIP LOCKED,
atomic CAS claiming, strict FSM state transitions, post-network CAS fencing,
and deletion-aware payload redaction and cancellation.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.dispatch_errors import (
    DispatchAbortedError,
    DispatchAmbiguousOutcomeError,
    DispatchDuplicateError,
    DispatchFencingConflictError,
    DispatchInvalidTransitionError,
    DispatchNotFoundError,
    DispatchRedactedPayloadError,
)
from app.models.entities import (
    SupervisorApprovalDispatch,
    SupervisorApprovalDispatchStatus,
    now_utc,
)

logger = logging.getLogger("lead_agent.supervisor_dispatch_repository")

# Strict Finite-State Machine Transition Map
LEGAL_TRANSITIONS: dict[str, set[str]] = {
    SupervisorApprovalDispatchStatus.PENDING.value: {
        SupervisorApprovalDispatchStatus.IN_PROGRESS.value,
        SupervisorApprovalDispatchStatus.ABORTED.value,
    },
    SupervisorApprovalDispatchStatus.IN_PROGRESS.value: {
        SupervisorApprovalDispatchStatus.SENT.value,
        SupervisorApprovalDispatchStatus.FAILED.value,
        SupervisorApprovalDispatchStatus.RETRYABLE_FAILURE.value,
        SupervisorApprovalDispatchStatus.OUTCOME_UNKNOWN.value,
        SupervisorApprovalDispatchStatus.ABORTED.value,
    },
    SupervisorApprovalDispatchStatus.RETRYABLE_FAILURE.value: {
        SupervisorApprovalDispatchStatus.PENDING.value,
        SupervisorApprovalDispatchStatus.ABORTED.value,
    },
    SupervisorApprovalDispatchStatus.OUTCOME_UNKNOWN.value: {
        SupervisorApprovalDispatchStatus.RECONCILED.value,
        SupervisorApprovalDispatchStatus.ABORTED.value,
    },
    SupervisorApprovalDispatchStatus.RECONCILED.value: {
        SupervisorApprovalDispatchStatus.SENT.value,
        SupervisorApprovalDispatchStatus.FAILED.value,
        SupervisorApprovalDispatchStatus.PENDING.value,
        SupervisorApprovalDispatchStatus.ABORTED.value,
    },
    SupervisorApprovalDispatchStatus.SENT.value: set(),      # Terminal
    SupervisorApprovalDispatchStatus.FAILED.value: set(),    # Terminal
    SupervisorApprovalDispatchStatus.ABORTED.value: set(),   # Terminal
}


def _ensure_utc(dt: Optional[datetime]) -> datetime:
    """Ensure datetime is non-null and timezone-aware UTC."""
    if dt is None:
        return now_utc()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class SupervisorApprovalDispatchRepository:
    """Repository handling all database operations for supervisor approval dispatches."""

    @staticmethod
    async def create_dispatch(
        session: AsyncSession,
        *,
        id: Optional[uuid.UUID] = None,
        approval_id: Optional[uuid.UUID],
        approval_reference_id: uuid.UUID,
        lead_id: Optional[uuid.UUID],
        lead_reference_id: uuid.UUID,
        action_id: str,
        channel: str,
        recipient: str,
        context_fingerprint: str,
        idempotency_key: str,
        payload_hash: str,
        payload_json: dict[str, Any],
        provider_name: str,
        max_attempts: int = 3,
        now: Optional[datetime] = None,
    ) -> SupervisorApprovalDispatch:
        """
        Creates and persists a new dispatch intent in PENDING status.
        Enforces database uniqueness on idempotency_key.
        """
        t_now = _ensure_utc(now)
        record = SupervisorApprovalDispatch(
            id=id or uuid.uuid4(),
            approval_id=approval_id,
            approval_reference_id=approval_reference_id,
            lead_id=lead_id,
            lead_reference_id=lead_reference_id,
            action_id=action_id,
            channel=channel.upper(),
            recipient=recipient,
            context_fingerprint=context_fingerprint,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            payload_json=payload_json,
            status=SupervisorApprovalDispatchStatus.PENDING.value,
            provider_name=provider_name,
            max_attempts=max_attempts,
            attempt_count=0,
            created_at=t_now,
            updated_at=t_now,
        )
        session.add(record)
        try:
            await session.flush()
        except IntegrityError as exc:
            orig_msg = str(getattr(exc, "orig", exc)).lower()
            if "idempotency" in orig_msg or "unique" in orig_msg:
                raise DispatchDuplicateError(
                    f"A dispatch with idempotency key '{idempotency_key}' already exists."
                ) from exc
            raise
        return record

    @staticmethod
    async def get_by_id(
        session: AsyncSession,
        dispatch_id: uuid.UUID,
    ) -> Optional[SupervisorApprovalDispatch]:
        """Fetch dispatch record by primary key."""
        return await session.get(SupervisorApprovalDispatch, dispatch_id)

    @staticmethod
    async def get_by_id_for_update(
        session: AsyncSession,
        dispatch_id: uuid.UUID,
    ) -> Optional[SupervisorApprovalDispatch]:
        """Fetch dispatch record by primary key with row-level lock."""
        return await session.get(
            SupervisorApprovalDispatch,
            dispatch_id,
            with_for_update=True,
        )

    @staticmethod
    async def get_by_idempotency_key(
        session: AsyncSession,
        idempotency_key: str,
    ) -> Optional[SupervisorApprovalDispatch]:
        """Lookup dispatch record by unique idempotency key."""
        stmt = select(SupervisorApprovalDispatch).where(
            SupervisorApprovalDispatch.idempotency_key == idempotency_key
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def poll_and_claim_next(
        session: AsyncSession,
        *,
        worker_id: str,
        lease_generation: int,
        now: Optional[datetime] = None,
    ) -> Optional[SupervisorApprovalDispatch]:
        """
        Polls the next eligible dispatch job using FOR UPDATE SKIP LOCKED.
        Excludes head-of-line blocking and ignores redacted payloads.
        Atomically updates row to IN_PROGRESS.
        """
        t_now = _ensure_utc(now)
        stmt = (
            select(SupervisorApprovalDispatch)
            .where(
                SupervisorApprovalDispatch.status.in_([
                    SupervisorApprovalDispatchStatus.PENDING.value,
                    SupervisorApprovalDispatchStatus.RETRYABLE_FAILURE.value,
                ]),
                or_(
                    SupervisorApprovalDispatch.next_retry_at.is_(None),
                    SupervisorApprovalDispatch.next_retry_at <= t_now,
                ),
            )
            .order_by(SupervisorApprovalDispatch.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = (await session.execute(stmt)).scalar_one_or_none()
        if not job:
            return None

        # Redacted payload guard: If payload was redacted, do not claim for network transmission
        if isinstance(job.payload_json, dict) and job.payload_json.get("redacted") is True:
            job.status = SupervisorApprovalDispatchStatus.ABORTED.value
            job.error_code = "CLAIMED_REDACTED_PAYLOAD"
            job.error_message = "Dispatch aborted because payload was previously redacted."
            job.updated_at = t_now
            await session.flush()
            return None

        job.status = SupervisorApprovalDispatchStatus.IN_PROGRESS.value
        job.dispatch_worker_id = worker_id
        job.lease_generation = lease_generation
        job.last_attempt_at = t_now
        job.attempt_count += 1
        job.updated_at = t_now
        await session.flush()
        return job

    @staticmethod
    async def transition_state(
        session: AsyncSession,
        *,
        dispatch_id: uuid.UUID,
        to_status: str,
        expected_status: Optional[str] = None,
        expected_lease_generation: Optional[int] = None,
        worker_id: Optional[str] = None,
        provider_message_id: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        next_retry_at: Optional[datetime] = None,
        reconciliation_notes: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> SupervisorApprovalDispatch:
        """
        Atomically transitions dispatch outbox status according to legal FSM rules.
        Enforces CAS checking on expected_status and expected_lease_generation.
        """
        t_now = _ensure_utc(now)
        record = await SupervisorApprovalDispatchRepository.get_by_id_for_update(session, dispatch_id)
        if not record:
            raise DispatchNotFoundError(f"Dispatch {dispatch_id} not found.")

        # Check CAS constraints if supplied
        if expected_status is not None and record.status != expected_status:
            if record.status == SupervisorApprovalDispatchStatus.ABORTED.value:
                raise DispatchAbortedError(
                    f"Dispatch {dispatch_id} was already ABORTED (expected {expected_status})."
                )
            raise DispatchInvalidTransitionError(
                f"Dispatch {dispatch_id} in status '{record.status}', expected '{expected_status}'."
            )

        if expected_lease_generation is not None and record.lease_generation != expected_lease_generation:
            raise DispatchFencingConflictError(
                f"Dispatch {dispatch_id} lease generation {record.lease_generation} does not match expected {expected_lease_generation}."
            )

        # Validate legal transition
        allowed = LEGAL_TRANSITIONS.get(record.status, set())
        if to_status not in allowed:
            raise DispatchInvalidTransitionError(
                f"Illegal dispatch transition: '{record.status}' -> '{to_status}'. "
                f"Allowed from '{record.status}': {sorted(allowed)}"
            )

        record.status = to_status
        record.updated_at = t_now

        if worker_id is not None:
            record.dispatch_worker_id = worker_id
        if provider_message_id is not None:
            record.provider_message_id = provider_message_id
        if error_code is not None:
            record.error_code = error_code
        if error_message is not None:
            record.error_message = error_message
        if next_retry_at is not None:
            record.next_retry_at = _ensure_utc(next_retry_at)
        elif to_status in {SupervisorApprovalDispatchStatus.SENT.value, SupervisorApprovalDispatchStatus.FAILED.value, SupervisorApprovalDispatchStatus.ABORTED.value}:
            record.next_retry_at = None
        if reconciliation_notes is not None:
            record.reconciliation_notes = reconciliation_notes

        if to_status in {SupervisorApprovalDispatchStatus.SENT.value, SupervisorApprovalDispatchStatus.FAILED.value, SupervisorApprovalDispatchStatus.ABORTED.value}:
            record.completed_at = t_now

        await session.flush()
        return record

    @staticmethod
    async def record_completion_post_network(
        session: AsyncSession,
        *,
        dispatch_id: uuid.UUID,
        outcome_status: str,
        expected_lease_generation: int,
        worker_id: str,
        provider_message_id: Optional[str] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        next_retry_at: Optional[datetime] = None,
        now: Optional[datetime] = None,
    ) -> SupervisorApprovalDispatch:
        """
        Post-network atomic CAS completion.
        If the dispatch row was ABORTED while network I/O was in-flight,
        the CAS update fails closed and raises DispatchAbortedError.
        """
        t_now = _ensure_utc(now)
        record = await SupervisorApprovalDispatchRepository.get_by_id_for_update(session, dispatch_id)
        if not record:
            raise DispatchNotFoundError(f"Dispatch {dispatch_id} not found.")

        # Check if row was aborted (e.g. by lead deletion or cancellation during network I/O)
        if record.status == SupervisorApprovalDispatchStatus.ABORTED.value:
            logger.warning(
                "Post-network update rejected: dispatch %s was previously ABORTED.",
                dispatch_id,
            )
            raise DispatchAbortedError(
                f"Dispatch {dispatch_id} was ABORTED while network call was in flight."
            )

        if record.status != SupervisorApprovalDispatchStatus.IN_PROGRESS.value:
            raise DispatchInvalidTransitionError(
                f"Cannot record completion for dispatch {dispatch_id} in status '{record.status}' (expected IN_PROGRESS)."
            )

        if record.lease_generation != expected_lease_generation:
            logger.warning(
                "Post-network fencing failure: dispatch %s generation %d != expected %d",
                dispatch_id, record.lease_generation, expected_lease_generation
            )
            record.status = SupervisorApprovalDispatchStatus.ABORTED.value
            record.error_code = "WORKER_FENCED_STALE_LEASE"
            record.error_message = f"Worker {worker_id} generation {expected_lease_generation} fenced by {record.lease_generation}"
            record.updated_at = t_now
            await session.flush()
            raise DispatchFencingConflictError(
                f"Fencing conflict: generation {expected_lease_generation} is stale."
            )

        return await SupervisorApprovalDispatchRepository.transition_state(
            session,
            dispatch_id=dispatch_id,
            to_status=outcome_status,
            expected_status=SupervisorApprovalDispatchStatus.IN_PROGRESS.value,
            expected_lease_generation=expected_lease_generation,
            worker_id=worker_id,
            provider_message_id=provider_message_id,
            error_code=error_code,
            error_message=error_message,
            next_retry_at=next_retry_at,
            now=t_now,
        )

    @staticmethod
    async def cancel_and_redact_for_lead_deletion(
        session: AsyncSession,
        *,
        lead_id: uuid.UUID,
        reason: str = "LEAD_DELETED",
        now: Optional[datetime] = None,
    ) -> int:
        """
        Transactional deletion-aware cancellation and payload redaction.
        - For PENDING, IN_PROGRESS, and RETRYABLE_FAILURE: transitions to ABORTED,
          redacts payload_json, and clears next_retry_at.
        - For completed/terminal dispatches: preserves status as evidence, redacts payload_json.
        Returns total number of dispatch rows affected.
        """
        t_now = _ensure_utc(now)
        redacted_payload = {"redacted": True, "reason": reason}

        # 1. Cancel and redact queued or in-progress jobs
        stmt_cancel = (
            update(SupervisorApprovalDispatch)
            .where(
                SupervisorApprovalDispatch.lead_id == lead_id,
                SupervisorApprovalDispatch.status.in_([
                    SupervisorApprovalDispatchStatus.PENDING.value,
                    SupervisorApprovalDispatchStatus.IN_PROGRESS.value,
                    SupervisorApprovalDispatchStatus.RETRYABLE_FAILURE.value,
                ]),
            )
            .values(
                status=SupervisorApprovalDispatchStatus.ABORTED.value,
                error_code="LEAD_DELETED_PRIOR_TO_DISPATCH",
                error_message=f"Dispatch cancelled due to lead deletion / privacy erasure ({reason})",
                payload_json=redacted_payload,
                next_retry_at=None,
                completed_at=t_now,
                updated_at=t_now,
            )
        )
        res_cancel = await session.execute(stmt_cancel)

        # 2. Redact payload for historical / completed records while preserving evidentiary status
        stmt_redact = (
            update(SupervisorApprovalDispatch)
            .where(
                SupervisorApprovalDispatch.lead_id == lead_id,
                SupervisorApprovalDispatch.status.in_([
                    SupervisorApprovalDispatchStatus.SENT.value,
                    SupervisorApprovalDispatchStatus.FAILED.value,
                    SupervisorApprovalDispatchStatus.OUTCOME_UNKNOWN.value,
                    SupervisorApprovalDispatchStatus.RECONCILED.value,
                ]),
            )
            .values(
                payload_json=redacted_payload,
                updated_at=t_now,
            )
        )
        res_redact = await session.execute(stmt_redact)
        await session.flush()

        total = (res_cancel.rowcount or 0) + (res_redact.rowcount or 0)
        logger.info(
            "Lead %s deleted: cancelled %d active dispatches, redacted %d completed dispatches",
            lead_id, res_cancel.rowcount or 0, res_redact.rowcount or 0
        )
        return total

    @staticmethod
    async def get_by_reference_ids(
        session: AsyncSession,
        *,
        lead_reference_id: Optional[uuid.UUID] = None,
        approval_reference_id: Optional[uuid.UUID] = None,
    ) -> Sequence[SupervisorApprovalDispatch]:
        """
        Forensic lookup using permanent immutable reference IDs.
        Retains queryability even after parent lead or approval rows are purged.
        """
        conditions = []
        if lead_reference_id is not None:
            conditions.append(SupervisorApprovalDispatch.lead_reference_id == lead_reference_id)
        if approval_reference_id is not None:
            conditions.append(SupervisorApprovalDispatch.approval_reference_id == approval_reference_id)

        if not conditions:
            return []

        stmt = select(SupervisorApprovalDispatch).where(and_(*conditions)).order_by(
            SupervisorApprovalDispatch.created_at.asc()
        )
        return (await session.execute(stmt)).scalars().all()

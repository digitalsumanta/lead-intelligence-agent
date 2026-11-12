"""
Asynchronous Telemetry Reconciler for Outbox Dispatches.

Phase 12J - D2 - Gate D-C4:
Evaluates un-reconciled delivery events and resolves ambiguous OUTCOME_UNKNOWN
dispatch records without human intervention.
Strict Guarantees:
- Zero outbound messaging or resends initiated under any circumstance.
- Preserves compare-and-swap (CAS) transitions and outbox state monotonicity.
- Never mutates already established terminal states (e.g. SENT does not flip to FAILED).
- Enforces system_webhook_reconciler identity in audit trails.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import (
    SupervisorApprovalDispatch,
    SupervisorApprovalDispatchStatus,
    SupervisorDispatchDeliveryEvent,
    now_utc,
)
from app.repositories.supervisor_delivery_event_repository import (
    SupervisorDeliveryEventRepository,
)
from app.repositories.supervisor_dispatch_repository import (
    SupervisorApprovalDispatchRepository,
)

logger = logging.getLogger("lead_agent.services.telemetry_reconciler")

SYSTEM_RECONCILER_ACTOR_ID = "system_webhook_reconciler"
SYSTEM_RECONCILER_ROLE = "system_reconciler"


class SupervisorTelemetryReconciler:
    """
    Reconciles delivery telemetry events against outbox dispatch records.
    """

    def __init__(
        self,
        dispatch_repository: type[SupervisorApprovalDispatchRepository] = SupervisorApprovalDispatchRepository,
        event_repository: type[SupervisorDeliveryEventRepository] = SupervisorDeliveryEventRepository,
    ) -> None:
        self.dispatch_repo = dispatch_repository
        self.event_repo = event_repository

    async def reconcile_single_event(
        self,
        session: AsyncSession,
        event: SupervisorDispatchDeliveryEvent,
    ) -> Optional[SupervisorApprovalDispatch]:
        """
        Evaluates a single delivery event and reconciles matching OUTCOME_UNKNOWN dispatches.
        """
        # Find dispatch by dispatch_id or provider_message_id
        dispatch: Optional[SupervisorApprovalDispatch] = None
        if event.dispatch_id:
            dispatch = await self.dispatch_repo.get_by_id_for_update(session, event.dispatch_id)
        elif event.provider_message_id:
            stmt = (
                select(SupervisorApprovalDispatch)
                .where(SupervisorApprovalDispatch.provider_message_id == event.provider_message_id)
                .with_for_update()
            )
            res = await session.execute(stmt)
            dispatch = res.scalar_one_or_none()

        if not dispatch:
            # No matching dispatch found; mark event processed and return
            await self.event_repo.mark_event_processed(session, event.id)
            return None

        # Link event references if not set yet
        if not event.dispatch_id:
            event.dispatch_id = dispatch.id
            event.dispatch_reference_id = dispatch.id
            event.lead_reference_id = dispatch.lead_reference_id

        # Monotonicity check: Only OUTCOME_UNKNOWN is eligible for automated status resolution
        if dispatch.status != SupervisorApprovalDispatchStatus.OUTCOME_UNKNOWN.value:
            logger.debug(
                "Dispatch '%s' is in status '%s' (not OUTCOME_UNKNOWN); preserving outbox state.",
                dispatch.id,
                dispatch.status,
            )
            await self.event_repo.mark_event_processed(session, event.id)
            return dispatch, False

        # Evaluate event type
        now = now_utc()
        target_status: Optional[str] = None
        resolution_notes: str = ""

        if event.event_type in {"DELIVERED", "ACCEPTED"}:
            # Upstream provider confirmation received!
            target_status = SupervisorApprovalDispatchStatus.SENT.value
            resolution_notes = (
                f"Auto-reconciled by {SYSTEM_RECONCILER_ACTOR_ID}: "
                f"Delivery event '{event.event_type}' confirmed by {event.provider_name}."
            )
        elif event.event_type in {"BOUNCED", "DROPPED", "COMPLAINT", "REJECTED"}:
            # Definitive delivery failure confirmed by provider!
            target_status = SupervisorApprovalDispatchStatus.FAILED.value
            resolution_notes = (
                f"Auto-reconciled by {SYSTEM_RECONCILER_ACTOR_ID}: "
                f"Delivery failure '{event.event_type}' confirmed by {event.provider_name}. "
                f"Reason: {event.bounce_reason or 'No reason specified'}."
            )

        if target_status:
            # Step 1: Transition OUTCOME_UNKNOWN -> RECONCILED
            await self.dispatch_repo.transition_state(
                session,
                dispatch_id=dispatch.id,
                to_status=SupervisorApprovalDispatchStatus.RECONCILED.value,
                reconciliation_notes=resolution_notes,
                now=now,
            )

            # Step 2: Transition RECONCILED -> final status (SENT or FAILED)
            # Never transition to PENDING (strictly NO auto-resend!)
            updated_dispatch = await self.dispatch_repo.transition_state(
                session,
                dispatch_id=dispatch.id,
                to_status=target_status,
                provider_message_id=event.provider_message_id,
                reconciliation_notes=resolution_notes,
                now=now,
            )
            await self.event_repo.mark_event_processed(session, event.id)
            logger.info(
                "Successfully auto-reconciled dispatch '%s' to '%s' via event '%s'",
                dispatch.id,
                target_status,
                event.id,
            )
            return updated_dispatch, True

        # Unrecognized / inconclusive event: keep OUTCOME_UNKNOWN and mark event evaluated
        await self.event_repo.mark_event_processed(session, event.id)
        return dispatch, False

    async def run_reconciliation_batch(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        limit: int = 50,
    ) -> dict[str, int]:
        """
        Polls and evaluates a batch of unprocessed delivery events.
        Runs each event in its own transaction for strict isolation.
        """
        unprocessed_ids: list[uuid.UUID] = []
        async with session_factory() as session:
            events = await self.event_repo.get_unprocessed_events(session, limit=limit)
            unprocessed_ids = [e.id for e in events]

        stats = {"processed": 0, "reconciled_sent": 0, "reconciled_failed": 0, "unaffected": 0}

        for event_id in unprocessed_ids:
            async with session_factory() as session:
                async with session.begin():
                    event = await self.event_repo.get_by_id(session, event_id)
                    if not event or event.processed_for_reconciliation:
                        continue

                    reconciled_result = await self.reconcile_single_event(session, event)
                    stats["processed"] += 1
                    if reconciled_result:
                        dispatch, did_transition = reconciled_result
                        if did_transition and dispatch:
                            if dispatch.status == SupervisorApprovalDispatchStatus.SENT.value:
                                stats["reconciled_sent"] += 1
                            elif dispatch.status == SupervisorApprovalDispatchStatus.FAILED.value:
                                stats["reconciled_failed"] += 1
                        else:
                            stats["unaffected"] += 1
                    else:
                        stats["unaffected"] += 1

        return stats


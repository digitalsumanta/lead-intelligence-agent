"""
Dedicated Outbox Worker Daemon for Supervisor Approval Dispatches.

Phase 12J - D2 - Gate D-D4.4 (Finding D4.3-F05):
Polls and processes supervisor_approval_dispatches under PostgreSQL row-level locks (FOR UPDATE SKIP LOCKED).
Guarantees:
- Single-replica serialized execution
- Strict adherence to master dispatch kill switch (outreach_dispatch_enabled)
- Native lease ownership, fencing, quota reservation, and idempotency
- Clean signal handling (SIGTERM, SIGINT) and graceful shutdown
- Explicit, sanitized logging without secret leakage
- Zero execution of unrelated SEO or generic retry jobs
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.database import SessionLocal, update_worker_heartbeat
from app.domain.dispatch_errors import (
    DispatchAbortedError,
    DispatchDisabledError,
    DispatchFencingConflictError,
    DispatchRecipientNotAllowlistedError,
    DispatchRecipientSuppressedError,
    DispatchComplianceConfigError,
)
from app.models.entities import SupervisorApprovalDispatchStatus, now_utc
from app.services.providers.factory import get_dispatch_provider
from app.services.supervisor_dispatch_service import SupervisorApprovalDispatchService
from app.utils.logging_config import configure_logging

logger = logging.getLogger("lead_agent.workers.outbox_worker")


async def _heartbeat_loop(
    worker_id: str,
    interval_seconds: float,
    stop_event: asyncio.Event,
) -> None:
    """Periodically emits heartbeats until stop_event is signaled."""
    while not stop_event.is_set():
        try:
            await update_worker_heartbeat(worker_id)
        except Exception as exc:
            logger.warning("Outbox worker '%s' heartbeat update failed: %s", worker_id, exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass


async def process_one_dispatch(
    service: SupervisorApprovalDispatchService,
    session_factory: async_sessionmaker[AsyncSession],
    worker_id: str,
    lease_generation: int = 1,
    now: Optional[datetime] = None,
) -> bool:
    """
    Attempts to poll, claim, and process a single eligible dispatch outbox job.
    Returns True if a job was claimed and processed, False if queue was empty.
    """
    eval_time = now or now_utc()
    if not eval_time.tzinfo:
        eval_time = eval_time.replace(tzinfo=timezone.utc)

    # 1. Claim next pending job under row-level lock
    async with session_factory() as session:
        async with session.begin():
            job = await service.repository.poll_and_claim_next(
                session,
                worker_id=worker_id,
                lease_generation=lease_generation,
                now=eval_time,
            )

    if not job:
        return False

    dispatch_id = job.id
    channel = job.channel
    logger.info(
        "Outbox worker '%s' claimed dispatch '%s' (channel: %s)",
        worker_id,
        dispatch_id,
        channel,
    )

    # 2. Resolve provider via factory (fails closed if disabled or unknown)
    provider = get_dispatch_provider(channel, settings=settings)

    # 3. Execute claimed dispatch through standard service pipeline
    try:
        completed = await service.execute_claimed_dispatch(
            session_factory,
            dispatch_id=dispatch_id,
            worker_id=worker_id,
            expected_lease_generation=lease_generation,
            provider=provider,
            now=eval_time,
        )
        logger.info(
            "Outbox worker '%s' completed dispatch '%s' with status: %s",
            worker_id,
            dispatch_id,
            completed.status,
        )
    except DispatchDisabledError as e:
        logger.warning(
            "Outbox worker '%s' dispatch '%s' safely halted by kill switch: %s",
            worker_id,
            dispatch_id,
            e,
        )
        try:
            async with session_factory() as session:
                async with session.begin():
                    job = await service.repository.get_by_id(session, dispatch_id)
                    if job:
                        job.status = SupervisorApprovalDispatchStatus.ABORTED.value
                        job.error_code = "DISPATCH_DISABLED"
                        job.error_message = str(e)
                        job.updated_at = eval_time
        except Exception as exc:
            logger.exception("Failed to update status to ABORTED: %s", exc)
    except (DispatchRecipientSuppressedError, DispatchComplianceConfigError) as e:
        logger.warning(
            "Outbox worker '%s' dispatch '%s' blocked by email compliance guard: %s",
            worker_id,
            dispatch_id,
            e,
        )
        try:
            async with session_factory() as session:
                async with session.begin():
                    job = await service.repository.get_by_id(session, dispatch_id)
                    if job:
                        job.status = SupervisorApprovalDispatchStatus.ABORTED.value
                        job.error_code = e.code
                        job.error_message = str(e)[:500]
                        job.updated_at = eval_time
        except Exception as exc:
            logger.exception("Failed to update status to ABORTED: %s", exc)
    except (DispatchAbortedError, DispatchRecipientNotAllowlistedError) as e:
        logger.warning(
            "Outbox worker '%s' dispatch '%s' safely halted by safety guard: %s",
            worker_id,
            dispatch_id,
            e,
        )
    except DispatchFencingConflictError as e:
        logger.warning(
            "Outbox worker '%s' dispatch '%s' fencing conflict rejected: %s",
            worker_id,
            dispatch_id,
            e,
        )
    except Exception as exc:
        logger.exception(
            "Outbox worker '%s' dispatch '%s' encountered unexpected error: %s",
            worker_id,
            dispatch_id,
            exc,
        )

    return True


async def run_outbox_worker(
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
    poll_interval_seconds: float = 2.0,
    worker_id: Optional[str] = None,
    stop_event: Optional[asyncio.Event] = None,
    max_iterations: Optional[int] = None,
) -> None:
    """
    Main durable outbox worker processing loop.
    Polls and processes supervisor approval dispatches until stop_event is set.
    """
    factory = session_factory or SessionLocal
    effective_worker_id = worker_id or f"outbox-worker:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    effective_stop_event = stop_event or asyncio.Event()

    logger.info(
        "Starting supervisor outbox worker daemon (worker_id: %s, poll_interval: %ss)",
        effective_worker_id,
        poll_interval_seconds,
    )

    service = SupervisorApprovalDispatchService()

    # Initial heartbeat
    try:
        await update_worker_heartbeat(effective_worker_id)
    except Exception as exc:
        logger.warning("Initial heartbeat update failed for '%s': %s", effective_worker_id, exc)

    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(
            effective_worker_id,
            interval_seconds=min(poll_interval_seconds * 5, 30.0),
            stop_event=effective_stop_event,
        )
    )

    iterations = 0
    try:
        while not effective_stop_event.is_set():
            if max_iterations is not None and iterations >= max_iterations:
                break

            processed = await process_one_dispatch(
                service=service,
                session_factory=factory,
                worker_id=effective_worker_id,
            )
            iterations += 1

            if not processed:
                try:
                    await asyncio.wait_for(
                        effective_stop_event.wait(),
                        timeout=poll_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
    finally:
        effective_stop_event.set()
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        logger.info("Supervisor outbox worker daemon '%s' shutdown cleanly", effective_worker_id)


def main() -> None:
    """Entrypoint function for CLI and container execution."""
    configure_logging()
    stop_event = asyncio.Event()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _handle_shutdown_signal(*_args):
        logger.info("Received termination signal, requesting worker shutdown...")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_shutdown_signal)
        except (NotImplementedError, AttributeError):
            # Windows does not support add_signal_handler for all signals
            signal.signal(sig, lambda *args: stop_event.set())

    try:
        loop.run_until_complete(run_outbox_worker(stop_event=stop_event))
    finally:
        loop.close()


if __name__ == "__main__":
    main()

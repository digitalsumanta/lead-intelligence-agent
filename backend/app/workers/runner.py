"""Poll and execute due retry jobs with bounded concurrency."""
import asyncio
import logging
import os
import socket
import uuid
from datetime import timedelta
from app.config import settings
from app.database import SessionLocal, verify_database_schema, update_worker_heartbeat
from app.models.entities import now_utc
from app.services.jobs import process_many, claim_jobs, recover_stale_jobs
from app.utils.logging_config import configure_logging

logger = logging.getLogger("lead_agent.worker")

async def _heartbeat_loop(worker_id: str, interval: float) -> None:
    while True:
        try:
            await update_worker_heartbeat(worker_id)
        except Exception:
            logger.exception("worker heartbeat update failed")
        await asyncio.sleep(interval)

async def run_retry_worker(poll_seconds: float = 5) -> None:
    """Claim due retry jobs atomically and process them up to configured capacity."""
    await verify_database_schema()
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"
    await update_worker_heartbeat(worker_id)
    heartbeat_task = asyncio.create_task(_heartbeat_loop(worker_id, min(poll_seconds, 10)))
    try:
        while True:
            async with SessionLocal() as session:
                now = now_utc()
                stale_before = now - timedelta(minutes=10)
                await recover_stale_jobs(session, stale_before, now)
                claimed = await claim_jobs(session, settings.max_concurrency, now)
            if claimed:
                logger.info("claimed due jobs", extra={"checkpoint": "PROCESSING", "duration_ms": 0})
                await process_many(SessionLocal, [type("JobRef", (), {"id": job_id}) for job_id in claimed], settings.max_concurrency, attempt_already_counted=True)
            await asyncio.sleep(poll_seconds)
    finally:
        heartbeat_task.cancel()
        try: await heartbeat_task
        except asyncio.CancelledError: pass

def main() -> None:
    """Start the durable retry worker process."""
    configure_logging()
    asyncio.run(run_retry_worker())

if __name__ == "__main__":
    main()

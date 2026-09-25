"""Poll and execute due retry jobs with bounded concurrency."""
import asyncio
import logging
from datetime import timedelta
from sqlalchemy import select, update
from app.config import settings
from app.database import SessionLocal, engine
from app.models import Base, ProcessingJob, JobStatus
from app.models.entities import now_utc
from app.services.jobs import process_many
from app.utils.logging_config import configure_logging

logger = logging.getLogger("lead_agent.worker")

async def run_retry_worker(poll_seconds: float = 5) -> None:
    """Claim due retry jobs atomically and process them up to configured capacity."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    while True:
        claimed = []
        async with SessionLocal() as session:
            now = now_utc()
            stale_before = now - timedelta(minutes=10)
            # Recover work abandoned by a killed worker; run_job resumes its last committed checkpoint.
            await session.execute(update(ProcessingJob).where(ProcessingJob.status == JobStatus.PROCESSING, ProcessingJob.started_at < stale_before).values(status=JobStatus.RETRYING, next_retry_at=now))
            ids = (await session.scalars(select(ProcessingJob.id).where(ProcessingJob.status == JobStatus.RETRYING, ProcessingJob.next_retry_at <= now).order_by(ProcessingJob.next_retry_at).limit(settings.max_concurrency))).all()
            for job_id in ids:
                result = await session.execute(update(ProcessingJob).where(ProcessingJob.id == job_id, ProcessingJob.status == JobStatus.RETRYING).values(status=JobStatus.PROCESSING))
                if result.rowcount: claimed.append(job_id)
            await session.commit()
        if claimed:
            logger.info("claimed due retry jobs", extra={"checkpoint": "RETRYING", "duration_ms": 0})
            await process_many(SessionLocal, [type("JobRef", (), {"id": job_id}) for job_id in claimed], settings.max_concurrency)
        await asyncio.sleep(poll_seconds)

def main() -> None:
    """Start the durable retry worker process."""
    configure_logging()
    asyncio.run(run_retry_worker())

if __name__ == "__main__":
    main()

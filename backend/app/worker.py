"""
Container Worker Entrypoint (python -m app.worker).

Executes both background capabilities concurrently:
1. Lead processing and durable retry runner (app.workers.runner.run_retry_worker).
2. Outbox dispatch worker (app.workers.outbox_worker.run_outbox_worker).
"""
import asyncio
import logging

from app.utils.logging_config import configure_logging
from app.workers.outbox_worker import process_one_dispatch, run_outbox_worker
from app.workers.runner import run_retry_worker

logger = logging.getLogger("lead_agent.worker")


async def run_all_workers() -> None:
    logger.info("Starting dual worker: lead processing runner + outbox dispatch worker")
    await asyncio.gather(
        run_retry_worker(poll_seconds=5.0),
        run_outbox_worker(poll_interval_seconds=2.0),
    )


def main() -> None:
    configure_logging()
    asyncio.run(run_all_workers())


if __name__ == "__main__":
    main()

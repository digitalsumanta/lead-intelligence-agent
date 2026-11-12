"""
Process-Local Concurrency and Rate Limiting Controls for Outreach Dispatch.

Phase 12J - D2 - Gate D-C4:
Implements runtime concurrency gating using asyncio.Semaphore and outbound
request rate limiting using an in-memory Token Bucket algorithm.

Architecture & Scope Note:
These controls operate strictly within the memory space of the local process.
In a multi-container or distributed deployment, an external shared coordinator
(e.g., Redis or database advisory locks) is required for deployment-wide limits.
Admission controls must execute outside database transactions to avoid holding
connection locks during rate-limit delays or network I/O.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from app.config import settings

logger = logging.getLogger("lead_agent.outreach_rate_limiter")


class TokenBucketRateLimiter:
    """
    Process-local token bucket rate limiter.
    Refills tokens at rate_per_second up to maximum capacity.
    """

    def __init__(
        self,
        rate_per_second: float = 5.0,
        capacity: Optional[float] = None,
        clock: Optional[callable] = None,
    ) -> None:
        self.rate_per_second = max(0.1, float(rate_per_second))
        self.capacity = float(capacity) if capacity is not None else max(1.0, self.rate_per_second)
        self.tokens = self.capacity
        self.last_refill = (clock or time.monotonic)()
        self.clock = clock or time.monotonic
        self._lock = asyncio.Lock()

    def _refill(self, now: float) -> None:
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_second)
            self.last_refill = now

    async def acquire(self, tokens: float = 1.0) -> float:
        """
        Acquires tokens from the bucket.
        If tokens are insufficient, sleeps asynchronously until available.
        Returns the duration slept in seconds.
        """
        slept = 0.0
        while True:
            async with self._lock:
                now = self.clock()
                self._refill(now)
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return slept

                # Calculate required sleep duration
                missing = tokens - self.tokens
                wait_time = missing / self.rate_per_second

            await asyncio.sleep(wait_time)
            slept += wait_time


class OutreachConcurrencyGate:
    """
    Process-local worker concurrency limiter wrapping an asyncio.Semaphore.
    Ensures that active outbound network requests do not exceed the configured worker cap.
    """

    def __init__(self, max_concurrent_workers: int = 2) -> None:
        self.max_workers = max(1, int(max_concurrent_workers))
        self._semaphore = asyncio.Semaphore(self.max_workers)

    async def __aenter__(self):
        await self._semaphore.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._semaphore.release()

    @property
    def current_available_workers(self) -> int:
        return self._semaphore._value


# Global process-local singletons initialized with application settings
_global_rate_limiter: Optional[TokenBucketRateLimiter] = None
_global_concurrency_gate: Optional[OutreachConcurrencyGate] = None


def get_outreach_rate_limiter() -> TokenBucketRateLimiter:
    """Returns the process-local singleton token-bucket rate limiter."""
    global _global_rate_limiter
    if _global_rate_limiter is None:
        _global_rate_limiter = TokenBucketRateLimiter(
            rate_per_second=settings.outreach_email_rate_limit_per_second,
            capacity=settings.outreach_email_rate_limit_per_second,
        )
    return _global_rate_limiter


def get_outreach_concurrency_gate() -> OutreachConcurrencyGate:
    """Returns the process-local singleton concurrency gate."""
    global _global_concurrency_gate
    if _global_concurrency_gate is None:
        _global_concurrency_gate = OutreachConcurrencyGate(
            max_concurrent_workers=settings.outreach_dispatch_max_workers
        )
    return _global_concurrency_gate

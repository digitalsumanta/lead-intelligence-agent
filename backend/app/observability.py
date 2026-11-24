"""Request correlation IDs, Prometheus metrics, and optional Sentry error tracking.

* Every HTTP request gets an ``X-Request-ID`` (a safe client-supplied value is reused, otherwise
  one is generated). It is returned in the response and added to every JSON log line written
  while handling the request, so one failing request can be traced across log lines.
* ``GET /metrics`` exposes Prometheus metrics when ``METRICS_TOKEN`` is set (scrapers send
  ``Authorization: Bearer <METRICS_TOKEN>``); otherwise the endpoint returns 404.
* Sentry is enabled only when ``SENTRY_DSN`` is set. ``send_default_pii`` stays off, so request
  bodies, cookies and user details are not sent.
"""
from __future__ import annotations

import contextvars
import hmac
import logging
import re
import time
import uuid
from datetime import timedelta

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from app.config import settings

logger = logging.getLogger("lead_agent.observability")

# ---------------------------------------------------------------------------
# Request IDs
# ---------------------------------------------------------------------------

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def resolve_request_id(supplied: str | None) -> str:
    if supplied and _REQUEST_ID_RE.match(supplied):
        return supplied
    return uuid.uuid4().hex


class RequestIdLogFilter(logging.Filter):
    """Attach the current request ID to log records (read by the JSON formatter)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "request_id", None) is None:
            record.request_id = request_id_var.get()
        return True


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS = Counter(
    "lead_agent_http_requests_total", "HTTP requests handled", ["method", "route", "status"], registry=REGISTRY,
)
HTTP_LATENCY = Histogram(
    "lead_agent_http_request_duration_seconds", "HTTP request latency", ["method", "route"], registry=REGISTRY,
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 120, 480),
)
HTTP_UNHANDLED_ERRORS = Counter(
    "lead_agent_http_unhandled_errors_total", "Requests that ended in an unhandled exception", ["route"], registry=REGISTRY,
)
PROCESSING_JOBS = Gauge(
    "lead_agent_processing_jobs", "Lead processing jobs by status", ["status"], registry=REGISTRY,
)
DISPATCHES = Gauge(
    "lead_agent_outreach_dispatches", "Supervisor outreach dispatches by status", ["status"], registry=REGISTRY,
)
WORKER_HEARTBEAT_AGE = Gauge(
    "lead_agent_worker_heartbeat_age_seconds", "Seconds since the most recent worker heartbeat (-1 if none)", registry=REGISTRY,
)
PROVIDER_CALLS_15M = Gauge(
    "lead_agent_provider_requests_15m", "Provider request attempts in the last 15 minutes", ["provider", "outcome"],
    registry=REGISTRY,
)
EMAIL_SUPPRESSIONS = Gauge(
    "lead_agent_email_suppressions", "Entries on the email suppression list by reason", ["reason"], registry=REGISTRY,
)
METRICS_COLLECTION_ERRORS = Counter(
    "lead_agent_metrics_collection_errors_total", "Failures while collecting database-backed metrics", registry=REGISTRY,
)


def route_label(scope: dict) -> str:
    """Use the route template (/api/leads/{lead_id}) to keep metric cardinality bounded."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


def observe_request(method: str, route: str, status: int, seconds: float) -> None:
    HTTP_REQUESTS.labels(method, route, str(status)).inc()
    HTTP_LATENCY.labels(method, route).observe(seconds)


def _set_gauge_values(gauge: Gauge, values: dict[str, float], label_name: str) -> None:
    gauge.clear()
    for label, value in values.items():
        gauge.labels(**{label_name: label}).set(value)


async def collect_database_metrics(session) -> None:
    """Refresh database-backed gauges. Called on each scrape; failures never break the scrape."""
    from sqlalchemy import func, select

    from app.models.entities import (
        EmailSuppression, ProcessingJob, ProviderUsage, SupervisorApprovalDispatch, WorkerHeartbeat, now_utc,
    )

    try:
        rows = (await session.execute(select(ProcessingJob.status, func.count()).group_by(ProcessingJob.status))).all()
        _set_gauge_values(PROCESSING_JOBS, {getattr(s, "value", str(s)): n for s, n in rows}, "status")

        rows = (await session.execute(
            select(SupervisorApprovalDispatch.status, func.count()).group_by(SupervisorApprovalDispatch.status))).all()
        _set_gauge_values(DISPATCHES, {str(s): n for s, n in rows}, "status")

        rows = (await session.execute(
            select(EmailSuppression.reason, func.count()).group_by(EmailSuppression.reason))).all()
        _set_gauge_values(EMAIL_SUPPRESSIONS, {str(r): n for r, n in rows}, "reason")

        latest = await session.scalar(select(func.max(WorkerHeartbeat.last_seen)))
        if latest is None:
            WORKER_HEARTBEAT_AGE.set(-1)
        else:
            if latest.tzinfo is None:
                from datetime import timezone
                latest = latest.replace(tzinfo=timezone.utc)
            WORKER_HEARTBEAT_AGE.set(max(0.0, (now_utc() - latest).total_seconds()))

        since = now_utc() - timedelta(minutes=15)
        rows = (await session.execute(
            select(ProviderUsage.provider, ProviderUsage.success, func.count())
            .where(ProviderUsage.created_at >= since, ProviderUsage.operation.is_not(None))
            .group_by(ProviderUsage.provider, ProviderUsage.success))).all()
        PROVIDER_CALLS_15M.clear()
        for provider, success, count in rows:
            PROVIDER_CALLS_15M.labels(provider=provider, outcome="success" if success else "failure").set(count)
    except Exception:  # metrics must never take the API down
        METRICS_COLLECTION_ERRORS.inc()
        logger.exception("Failed to collect database metrics")


def metrics_authorized(authorization: str | None) -> bool:
    token = (settings.metrics_token or "").strip()
    if not token:
        return False
    supplied = (authorization or "")
    supplied = supplied[7:].strip() if supplied.lower().startswith("bearer ") else ""
    return bool(supplied) and hmac.compare_digest(token, supplied)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


# ---------------------------------------------------------------------------
# Sentry
# ---------------------------------------------------------------------------

_sentry_initialized = False


def init_sentry() -> bool:
    """Initialise Sentry once if SENTRY_DSN is configured. Returns True when enabled."""
    global _sentry_initialized
    dsn = (settings.sentry_dsn or "").strip()
    if not dsn or _sentry_initialized:
        return _sentry_initialized
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=settings.app_env,
        release=settings.sentry_release or None,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        max_request_body_size="never",
    )
    _sentry_initialized = True
    logger.info("Sentry error tracking enabled (environment=%s)", settings.app_env)
    return True


def monotonic() -> float:
    return time.perf_counter()

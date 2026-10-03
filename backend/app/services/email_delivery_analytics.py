import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EmailDeliveryEvent, AiOutreachExecution
from app.schemas.email_delivery_event import (
    DeliveryAnalyticsSummary,
    DeliveryAnalyticsProviderSummary,
    DeliveryAnalyticsTimeSeriesPoint,
    ExecutionDeliveryAnalytics,
)
from app.domain.delivery_reconciliation import reconcile_delivery_events


class _MockEvent:
    __slots__ = ("id", "event_type", "recorded_at", "provider_occurred_at")

    def __init__(self, id, event_type, recorded_at, provider_occurred_at):
        self.id = id
        self.event_type = event_type
        self.recorded_at = recorded_at
        self.provider_occurred_at = provider_occurred_at


def _safe_rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


async def get_analytics_summary(
    session: AsyncSession,
    scope: Literal["global", "date_range", "provider", "execution"],
    start: datetime | None = None,
    end: datetime | None = None,
    provider: str | None = None,
    execution_id: uuid.UUID | None = None,
) -> DeliveryAnalyticsSummary:
    exec_filters = []
    if scope == "date_range":
        if start:
            exec_filters.append(AiOutreachExecution.created_at >= start)
        if end:
            exec_filters.append(AiOutreachExecution.created_at < end)
    elif scope == "provider" and provider:
        exec_filters.append(func.lower(AiOutreachExecution.provider) == provider.casefold())
    elif scope == "execution" and execution_id:
        exec_filters.append(AiOutreachExecution.id == execution_id)

    # 1. Database aggregation for event counts
    counts_stmt = (
        select(EmailDeliveryEvent.event_type, func.count(EmailDeliveryEvent.id))
        .join(AiOutreachExecution, EmailDeliveryEvent.execution_id == AiOutreachExecution.id)
        .where(*exec_filters)
        .group_by(EmailDeliveryEvent.event_type)
    )
    counts_result = (await session.execute(counts_stmt)).all()
    event_counts = {row[0]: row[1] for row in counts_result}

    sent = event_counts.get("SENT", 0)
    delivered = event_counts.get("DELIVERED", 0)
    bounced = event_counts.get("BOUNCED", 0)
    failed = event_counts.get("FAILED", 0)
    complained = event_counts.get("COMPLAINED", 0)
    unsubscribed = event_counts.get("UNSUBSCRIBED", 0)

    # 2. Execution states via Phase 11D reconciliation
    # To remain reasonable for large datasets, we only select the minimum columns required.
    stmt = (
        select(
            AiOutreachExecution.id,
            AiOutreachExecution.provider,
            AiOutreachExecution.provider_message_id,
            EmailDeliveryEvent.id,
            EmailDeliveryEvent.event_type,
            EmailDeliveryEvent.recorded_at,
            EmailDeliveryEvent.provider_occurred_at,
        )
        .outerjoin(EmailDeliveryEvent, EmailDeliveryEvent.execution_id == AiOutreachExecution.id)
        .where(*exec_filters)
        .order_by(AiOutreachExecution.id)
    )

    rows = (await session.execute(stmt)).all()

    # Group by execution
    execution_states = {
        "UNKNOWN": 0,
        "SENT": 0,
        "DELIVERED": 0,
        "BOUNCED": 0,
        "FAILED": 0,
        "COMPLAINED": 0,
        "UNSUBSCRIBED": 0,
    }
    conflicted = 0
    out_of_order = 0
    unsupported = 0

    current_exec_id = None
    current_provider = None
    current_message_id = None
    current_events = []

    def process_current():
        nonlocal conflicted, out_of_order, unsupported
        if current_exec_id is None:
            return
        result = reconcile_delivery_events(current_exec_id, current_provider, current_message_id, current_events)
        execution_states[result["current_state"]] += 1
        if result["has_conflict"]:
            conflicted += 1
        if result["has_out_of_order_events"]:
            out_of_order += 1
        unsupported += result["unsupported_event_count"]

    for row in rows:
        exec_id, prov, msg_id, ev_id, ev_type, rec_at, prov_at = row
        if exec_id != current_exec_id:
            process_current()
            current_exec_id = exec_id
            current_provider = prov
            current_message_id = msg_id
            current_events = []

        if ev_id is not None:
            current_events.append(_MockEvent(ev_id, ev_type, rec_at, prov_at))

    process_current()

    return DeliveryAnalyticsSummary(
        scope=scope,
        start=start,
        end=end,
        provider=provider,
        execution_id=execution_id,
        sent_count=sent,
        delivered_count=delivered,
        bounced_count=bounced,
        failed_count=failed,
        complained_count=complained,
        unsubscribed_count=unsubscribed,
        delivery_rate=_safe_rate(delivered, sent),
        bounce_rate=_safe_rate(bounced, sent),
        failure_rate=_safe_rate(failed, sent),
        complaint_rate=_safe_rate(complained, sent),
        unsubscribe_rate=_safe_rate(unsubscribed, sent),
        unknown_execution_count=execution_states["UNKNOWN"],
        sent_execution_count=execution_states["SENT"],
        delivered_execution_count=execution_states["DELIVERED"],
        bounced_execution_count=execution_states["BOUNCED"],
        failed_execution_count=execution_states["FAILED"],
        complained_execution_count=execution_states["COMPLAINED"],
        unsubscribed_execution_count=execution_states["UNSUBSCRIBED"],
        conflicted_execution_count=conflicted,
        out_of_order_execution_count=out_of_order,
        unsupported_event_count=unsupported,
    )


async def get_provider_analytics(session: AsyncSession) -> list[DeliveryAnalyticsProviderSummary]:
    # Aggregate event counts per provider
    stmt = (
        select(
            func.lower(AiOutreachExecution.provider).label("prov"),
            EmailDeliveryEvent.event_type,
            func.count(EmailDeliveryEvent.id)
        )
        .join(EmailDeliveryEvent, EmailDeliveryEvent.execution_id == AiOutreachExecution.id)
        .group_by(func.lower(AiOutreachExecution.provider), EmailDeliveryEvent.event_type)
        .order_by(func.lower(AiOutreachExecution.provider))
    )

    rows = (await session.execute(stmt)).all()

    provider_data = {}
    for prov, ev_type, count in rows:
        if prov not in provider_data:
            provider_data[prov] = {
                "SENT": 0, "DELIVERED": 0, "BOUNCED": 0,
                "FAILED": 0, "COMPLAINED": 0, "UNSUBSCRIBED": 0
            }
        if ev_type in provider_data[prov]:
            provider_data[prov][ev_type] = count

    results = []
    # Alphabetical order
    for prov in sorted(provider_data.keys()):
        data = provider_data[prov]
        sent = data["SENT"]
        results.append(DeliveryAnalyticsProviderSummary(
            provider=prov,
            sent_count=sent,
            delivered_count=data["DELIVERED"],
            bounced_count=data["BOUNCED"],
            failed_count=data["FAILED"],
            complained_count=data["COMPLAINED"],
            unsubscribed_count=data["UNSUBSCRIBED"],
            delivery_rate=_safe_rate(data["DELIVERED"], sent),
            bounce_rate=_safe_rate(data["BOUNCED"], sent),
            failure_rate=_safe_rate(data["FAILED"], sent),
            complaint_rate=_safe_rate(data["COMPLAINED"], sent),
            unsubscribe_rate=_safe_rate(data["UNSUBSCRIBED"], sent),
        ))

    return results


async def get_timeseries_analytics(
    session: AsyncSession,
    granularity: Literal["day", "week"] = "day",
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[DeliveryAnalyticsTimeSeriesPoint]:
    # Use database date truncation on AiOutreachExecution.created_at
    period_expr = func.date_trunc(granularity, AiOutreachExecution.created_at)

    filters = []
    if start:
        filters.append(AiOutreachExecution.created_at >= start)
    if end:
        filters.append(AiOutreachExecution.created_at < end)

    stmt = (
        select(
            period_expr,
            EmailDeliveryEvent.event_type,
            func.count(EmailDeliveryEvent.id)
        )
        .join(EmailDeliveryEvent, EmailDeliveryEvent.execution_id == AiOutreachExecution.id)
        .where(*filters)
        .group_by(period_expr, EmailDeliveryEvent.event_type)
        .order_by(period_expr)
    )

    rows = (await session.execute(stmt)).all()

    periods = {}
    for period, ev_type, count in rows:
        if period not in periods:
            periods[period] = {
                "SENT": 0, "DELIVERED": 0, "BOUNCED": 0,
                "FAILED": 0, "COMPLAINED": 0, "UNSUBSCRIBED": 0
            }
        if ev_type in periods[period]:
            periods[period][ev_type] = count

    results = []
    for period in sorted(periods.keys()):
        data = periods[period]
        results.append(DeliveryAnalyticsTimeSeriesPoint(
            period_start=period,
            sent_count=data["SENT"],
            delivered_count=data["DELIVERED"],
            bounced_count=data["BOUNCED"],
            failed_count=data["FAILED"],
            complained_count=data["COMPLAINED"],
            unsubscribed_count=data["UNSUBSCRIBED"],
        ))

    return results


async def get_execution_analytics(session: AsyncSession, execution_id: uuid.UUID) -> ExecutionDeliveryAnalytics | None:
    execution = await session.get(AiOutreachExecution, execution_id)
    if not execution:
        return None

    events = (await session.scalars(select(EmailDeliveryEvent).where(EmailDeliveryEvent.execution_id == execution_id))).all()

    result = reconcile_delivery_events(execution_id, execution.provider, execution.provider_message_id, events)

    return ExecutionDeliveryAnalytics(
        execution_id=execution_id,
        provider=execution.provider,
        provider_message_id=execution.provider_message_id,
        recipient=execution.recipient,
        current_reconciled_state=result["current_state"],
        event_count=result["event_count"],
        first_event_at=result["first_event_at"],
        last_event_at=result["last_event_at"],
        has_conflict=result["has_conflict"],
        conflict_types=result["conflict_types"],
        has_out_of_order_events=result["has_out_of_order_events"],
        unsupported_event_count=result["unsupported_event_count"],
    )

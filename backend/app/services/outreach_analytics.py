"""Read-only aggregates over sent outreach executions and recorded outcomes."""
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OutreachExecution, SalesActivity, SalesActivityOutcome
from app.schemas.outreach_analytics import (
    OutreachAnalyticsChannel,
    OutreachAnalyticsOutcome,
    OutreachAnalyticsResponse,
    OutreachAnalyticsStatus,
)


CHANNEL_ORDER = tuple(OutreachAnalyticsChannel)
STATUS_ORDER = tuple(OutreachAnalyticsStatus)
OUTCOME_ORDER = tuple(OutreachAnalyticsOutcome)
LIMITATIONS = [
    "Metrics describe recorded sent executions and human-recorded outcomes; they do not establish causal attribution.",
    "Executions without recorded outcomes do not mean no response.",
    "Execution status is the current persisted status, not historical status.",
]


def _utc_midnight(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


async def outreach_analytics(
    db: AsyncSession,
    *,
    from_date: date | None = None,
    to: date | None = None,
    channel: OutreachAnalyticsChannel | None = None,
) -> OutreachAnalyticsResponse:
    """Compute execution and qualifying activity aggregates without a fan-out join."""
    conditions = [OutreachExecution.sent_at.is_not(None)]
    if from_date is not None:
        conditions.append(OutreachExecution.sent_at >= _utc_midnight(from_date))
    if to is not None:
        conditions.append(OutreachExecution.sent_at < _utc_midnight(to + timedelta(days=1)))
    if channel is not None:
        conditions.append(OutreachExecution.channel == channel.value)

    cohort = select(
        OutreachExecution.id.label("execution_id"),
        OutreachExecution.lead_id.label("lead_id"),
    ).where(*conditions).subquery()

    attempts_total = await db.scalar(select(func.count(func.distinct(cohort.c.execution_id)))) or 0
    leads_represented = await db.scalar(select(func.count(func.distinct(cohort.c.lead_id)))) or 0

    channel_rows = (await db.execute(
        select(OutreachExecution.channel, func.count(func.distinct(OutreachExecution.id)))
        .join(cohort, cohort.c.execution_id == OutreachExecution.id)
        .group_by(OutreachExecution.channel)
    )).all()
    channel_counts = {name: count for name, count in channel_rows}

    status_rows = (await db.execute(
        select(OutreachExecution.status, func.count(func.distinct(OutreachExecution.id)))
        .join(cohort, cohort.c.execution_id == OutreachExecution.id)
        .group_by(OutreachExecution.status)
    )).all()
    status_counts = {name: count for name, count in status_rows}

    qualifying = (
        select(
            SalesActivity.outcome.label("outcome"),
            SalesActivity.id.label("activity_id"),
            OutreachExecution.id.label("execution_id"),
            OutreachExecution.lead_id.label("lead_id"),
        )
        .join(cohort, cohort.c.execution_id == SalesActivity.outreach_execution_id)
        .join(OutreachExecution, OutreachExecution.id == SalesActivity.outreach_execution_id)
        .where(
            SalesActivity.outreach_execution_id.is_not(None),
            SalesActivity.outcome.is_not(None),
            SalesActivity.outreach_idempotency_key.is_not(None),
        )
        .subquery()
    )
    outcome_rows = (await db.execute(
        select(
            qualifying.c.outcome,
            func.count(qualifying.c.activity_id),
            func.count(func.distinct(qualifying.c.execution_id)),
            func.count(func.distinct(qualifying.c.lead_id)),
        ).group_by(qualifying.c.outcome)
    )).all()
    outcome_counts = {
        outcome: (activity_count, execution_count, lead_count)
        for outcome, activity_count, execution_count, lead_count in outcome_rows
    }
    recorded_outcomes_total = sum(value[0] for value in outcome_counts.values())
    executions_with_outcomes = await db.scalar(
        select(func.count(func.distinct(qualifying.c.execution_id)))
    ) or 0
    leads_with_outcomes = await db.scalar(
        select(func.count(func.distinct(qualifying.c.lead_id)))
    ) or 0

    # SQLAlchemy's Enum values can be returned as enum members or strings depending
    # on dialect; normalize map keys so the response order is stable.
    channel_counts = {getattr(key, "value", key): value for key, value in channel_counts.items()}
    status_counts = {getattr(key, "value", key): value for key, value in status_counts.items()}
    outcome_counts = {getattr(key, "value", key): value for key, value in outcome_counts.items()}

    return OutreachAnalyticsResponse(
        cohort={"from": from_date, "to": to, "channel": channel, "date_field": "sent_at", "timezone": "UTC"},
        attempts_total=attempts_total,
        attempts_by_channel=[{"channel": item, "count": channel_counts.get(item.value, 0)} for item in CHANNEL_ORDER],
        attempts_by_status=[{"status": item, "count": status_counts.get(item.value, 0)} for item in STATUS_ORDER],
        recorded_outcomes_total=recorded_outcomes_total,
        outcomes_by_type=[
            {"outcome": item, "activity_count": outcome_counts.get(item.value, (0, 0, 0))[0],
             "executions_with_outcome": outcome_counts.get(item.value, (0, 0, 0))[1],
             "leads_represented": outcome_counts.get(item.value, (0, 0, 0))[2]}
            for item in OUTCOME_ORDER
        ],
        executions_with_recorded_outcomes=executions_with_outcomes,
        leads_represented=leads_represented,
        leads_with_recorded_outcomes=leads_with_outcomes,
        outcome_coverage={
            "eligible_sent_executions": attempts_total,
            "executions_with_outcomes": executions_with_outcomes,
            "executions_without_outcomes": attempts_total - executions_with_outcomes,
        },
        limitations=LIMITATIONS,
    )

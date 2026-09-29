"""Deterministic revenue, target, collection, and conversion calculations."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy import case, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Lead, OutreachStatus, RevenueTarget, SalesActivity, SalesCollection,
    SalesStatus,
)
from app.models.entities import now_utc
from app.schemas.revenue import (
    CommercialPatch, RevenueTargetCreate, RevenueTargetPatch,
    SalesCollectionCreate, SalesCollectionPatch, SUPPORTED_CURRENCIES,
)

ZERO = Decimal("0.00")
OPEN_STATUSES = (
    SalesStatus.NEW, SalesStatus.QUALIFIED, SalesStatus.CONTACTED,
    SalesStatus.ENGAGED, SalesStatus.PROPOSAL, SalesStatus.NEGOTIATION,
)
CONVERSION_TRANSITIONS = (
    (SalesStatus.NEW, SalesStatus.QUALIFIED),
    (SalesStatus.QUALIFIED, SalesStatus.CONTACTED),
    (SalesStatus.CONTACTED, SalesStatus.ENGAGED),
    (SalesStatus.ENGAGED, SalesStatus.PROPOSAL),
    (SalesStatus.PROPOSAL, SalesStatus.NEGOTIATION),
    (SalesStatus.NEGOTIATION, SalesStatus.WON),
)


class RevenueError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def require_currency(currency: str) -> str:
    normalized = currency.upper()
    if normalized not in SUPPORTED_CURRENCIES:
        raise RevenueError("INVALID_CURRENCY", "Currency is not supported")
    return normalized


async def commercial_values(session: AsyncSession, lead: Lead) -> dict:
    collected = await session.scalar(select(func.coalesce(func.sum(SalesCollection.amount), ZERO)).where(SalesCollection.lead_id == lead.id))
    actual = collected or ZERO
    collectible = lead.expected_collection if lead.expected_collection is not None else lead.won_value
    outstanding = max((collectible or ZERO) - actual, ZERO)
    return {
        "lead_id": lead.id, "sales_status": lead.sales_status,
        "estimated_value": lead.estimated_value, "proposal_value": lead.proposal_value,
        "negotiation_value": lead.negotiation_value, "won_value": lead.won_value,
        "expected_collection": lead.expected_collection, "actual_collected": actual,
        "outstanding_collection": outstanding, "currency": lead.currency,
    }


async def update_commercial(session: AsyncSession, lead: Lead, body: CommercialPatch) -> Lead:
    supplied = body.model_fields_set
    if not supplied:
        raise RevenueError("EMPTY_COMMERCIAL_UPDATE", "At least one commercial field must be provided")
    if "currency" in supplied and body.currency is None:
        raise RevenueError("INVALID_CURRENCY", "Currency cannot be null")
    if "currency" in supplied and body.currency != lead.currency:
        has_values = any(getattr(lead, field) is not None for field in (
            "estimated_value", "proposal_value", "negotiation_value", "won_value", "expected_collection",
        ))
        has_collections = await session.scalar(select(SalesCollection.id).where(SalesCollection.lead_id == lead.id).limit(1))
        if has_values or has_collections:
            raise RevenueError("CURRENCY_LOCKED", "Currency cannot change after commercial values or collections are recorded")
    new_values = {field: getattr(body, field) for field in supplied if field != "currency"}
    collectible = new_values.get("expected_collection", lead.expected_collection)
    if collectible is None:
        collectible = new_values.get("won_value", lead.won_value)
    collected = await session.scalar(select(func.coalesce(func.sum(SalesCollection.amount), ZERO)).where(SalesCollection.lead_id == lead.id)) or ZERO
    if collectible is not None and collected > collectible:
        raise RevenueError("COLLECTED_EXCEEDS_RECEIVABLE", "Commercial values cannot be reduced below collections already recorded")
    for field in supplied:
        setattr(lead, field, getattr(body, field))
    lead.updated_at = now_utc()
    await session.commit()
    await session.refresh(lead)
    return lead


def _collection_limit(lead: Lead) -> Decimal | None:
    return lead.expected_collection if lead.expected_collection is not None else lead.won_value


async def _validate_collection(session: AsyncSession, lead: Lead, amount: Decimal, *, exclude_id: UUID | None = None) -> None:
    if lead.sales_status != SalesStatus.WON:
        raise RevenueError("COLLECTION_REQUIRES_WON", "Collections can be recorded only for WON leads", 409)
    limit = _collection_limit(lead)
    if limit is None:
        raise RevenueError("RECEIVABLE_REQUIRED", "Set won_value or expected_collection before recording collections", 409)
    query = select(func.coalesce(func.sum(SalesCollection.amount), ZERO)).where(SalesCollection.lead_id == lead.id)
    if exclude_id is not None:
        query = query.where(SalesCollection.id != exclude_id)
    existing = await session.scalar(query) or ZERO
    if existing + amount > limit:
        raise RevenueError("COLLECTION_EXCEEDS_RECEIVABLE", "Collection total cannot exceed the expected collectible amount")


async def create_collection(session: AsyncSession, lead: Lead, body: SalesCollectionCreate) -> SalesCollection:
    await _validate_collection(session, lead, body.amount)
    collection = SalesCollection(
        lead_id=lead.id, amount=body.amount, currency=lead.currency,
        collection_date=body.collection_date.astimezone(timezone.utc),
        payment_reference=body.payment_reference, notes=body.notes,
    )
    session.add(collection)
    await session.commit()
    await session.refresh(collection)
    return collection


async def update_collection(session: AsyncSession, collection: SalesCollection, body: SalesCollectionPatch) -> SalesCollection:
    supplied = body.model_fields_set
    if not supplied:
        raise RevenueError("EMPTY_COLLECTION_UPDATE", "At least one collection field must be provided")
    # Serialize collection edits with new collections and commercial updates.
    lead = await session.scalar(select(Lead).where(Lead.id == collection.lead_id).with_for_update())
    if lead is None:
        raise RevenueError("LEAD_NOT_FOUND", "Collection lead no longer exists", 404)
    amount = body.amount if "amount" in supplied else collection.amount
    await _validate_collection(session, lead, amount, exclude_id=collection.id)
    for field in supplied:
        value = getattr(body, field)
        if field == "collection_date" and value is not None:
            value = value.astimezone(timezone.utc)
        setattr(collection, field, value)
    collection.updated_at = now_utc()
    await session.commit()
    await session.refresh(collection)
    return collection


async def create_target(session: AsyncSession, body: RevenueTargetCreate) -> RevenueTarget:
    currency = require_currency(body.currency)
    if body.period_start > body.period_end:
        raise RevenueError("INVALID_TARGET_PERIOD", "period_start must be on or before period_end")
    if body.active and await session.scalar(select(RevenueTarget.id).where(RevenueTarget.currency == currency, RevenueTarget.active.is_(True)).limit(1)):
        raise RevenueError("ACTIVE_TARGET_EXISTS", "Deactivate the current target in this currency before creating another", 409)
    target = RevenueTarget(**body.model_dump())
    session.add(target)
    await session.commit()
    await session.refresh(target)
    return target


async def update_target(session: AsyncSession, target: RevenueTarget, body: RevenueTargetPatch) -> RevenueTarget:
    supplied = body.model_fields_set
    if not supplied:
        raise RevenueError("EMPTY_TARGET_UPDATE", "At least one target field must be provided")
    values = {field: getattr(body, field) for field in supplied}
    if any(value is None for field, value in values.items() if field not in {"label", "currency", "period_start", "period_end"}):
        raise RevenueError("INVALID_TARGET_UPDATE", "Target amount and active state cannot be null")
    currency = values.get("currency", target.currency)
    if currency is None:
        raise RevenueError("INVALID_CURRENCY", "Currency cannot be null")
    if "label" in supplied and not values["label"]:
        raise RevenueError("INVALID_TARGET_UPDATE", "Target label cannot be blank")
    currency = require_currency(currency)
    start = values.get("period_start", target.period_start)
    end = values.get("period_end", target.period_end)
    if start is None or end is None or start > end:
        raise RevenueError("INVALID_TARGET_PERIOD", "period_start must be on or before period_end")
    active = values.get("active", target.active)
    if active and await session.scalar(select(RevenueTarget.id).where(
        RevenueTarget.currency == currency, RevenueTarget.active.is_(True), RevenueTarget.id != target.id,
    ).limit(1)):
        raise RevenueError("ACTIVE_TARGET_EXISTS", "Another active target already exists for this currency", 409)
    for field, value in values.items():
        setattr(target, field, currency if field == "currency" else value)
    target.updated_at = now_utc()
    await session.commit()
    await session.refresh(target)
    return target


async def revenue_summary(session: AsyncSession, currency: str, today: date | None = None) -> dict:
    currency = require_currency(currency)
    today = today or datetime.now(timezone.utc).date()
    zero = literal(ZERO)
    pipeline_case = case(
        (Lead.sales_status == SalesStatus.PROPOSAL, func.coalesce(Lead.proposal_value, zero)),
        (Lead.sales_status == SalesStatus.NEGOTIATION, func.coalesce(Lead.negotiation_value, zero)),
        (Lead.sales_status.in_((SalesStatus.NEW, SalesStatus.QUALIFIED, SalesStatus.CONTACTED, SalesStatus.ENGAGED)), func.coalesce(Lead.estimated_value, zero)),
        else_=zero,
    )
    row = (await session.execute(select(
        func.coalesce(func.sum(pipeline_case), ZERO),
        func.coalesce(func.sum(case((Lead.sales_status == SalesStatus.PROPOSAL, func.coalesce(Lead.proposal_value, zero)), else_=zero)), ZERO),
        func.coalesce(func.sum(case((Lead.sales_status == SalesStatus.NEGOTIATION, func.coalesce(Lead.negotiation_value, zero)), else_=zero)), ZERO),
        func.coalesce(func.sum(case((Lead.sales_status == SalesStatus.WON, func.coalesce(Lead.won_value, zero)), else_=zero)), ZERO),
        func.coalesce(func.sum(case((Lead.sales_status == SalesStatus.WON, func.coalesce(Lead.expected_collection, zero)), else_=zero)), ZERO),
    ).where(Lead.currency == currency))).one()
    collected = await session.scalar(select(func.coalesce(func.sum(SalesCollection.amount), ZERO)).where(SalesCollection.currency == currency)) or ZERO
    collection_total = select(func.coalesce(func.sum(SalesCollection.amount), ZERO)).where(SalesCollection.lead_id == Lead.id).correlate(Lead).scalar_subquery()
    receivable = case(
        (Lead.expected_collection.is_not(None), Lead.expected_collection),
        else_=func.coalesce(Lead.won_value, zero),
    )
    outstanding = await session.scalar(select(func.coalesce(func.sum(case(
        (receivable > collection_total, receivable - collection_total), else_=zero,
    )), ZERO)).where(Lead.currency == currency, Lead.sales_status == SalesStatus.WON)) or ZERO
    target = await session.scalar(select(RevenueTarget).where(
        RevenueTarget.currency == currency, RevenueTarget.active.is_(True),
        RevenueTarget.period_start <= today, RevenueTarget.period_end >= today,
    ).order_by(RevenueTarget.period_start.desc(), RevenueTarget.id).limit(1))
    target_amount = target.target_amount if target else None
    target_collected = target_remaining = achievement = None
    if target:
        start_at = datetime.combine(target.period_start, time.min, tzinfo=timezone.utc)
        end_at = datetime.combine(target.period_end + timedelta(days=1), time.min, tzinfo=timezone.utc)
        target_collected = await session.scalar(select(func.coalesce(func.sum(SalesCollection.amount), ZERO)).where(
            SalesCollection.currency == currency,
            SalesCollection.collection_date >= start_at,
            SalesCollection.collection_date < end_at,
        )) or ZERO
        target_remaining = max(target_amount - target_collected, ZERO)
        achievement = (target_collected * Decimal("100") / target_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if target_amount else None
    return {
        "currency": currency, "pipeline_value": row[0], "proposal_value": row[1],
        "negotiation_value": row[2], "won_value": row[3], "expected_collection": row[4],
        "actual_collected": collected, "outstanding_collection": outstanding,
        "target_id": target.id if target else None, "target_label": target.label if target else None,
        "target_amount": target_amount, "target_collected": target_collected,
        "target_remaining": target_remaining, "achievement_percentage": achievement,
    }


async def pipeline_summary(session: AsyncSession, currency: str) -> list[dict]:
    currency = require_currency(currency)
    zero = literal(ZERO)
    collected_by_lead = select(func.coalesce(func.sum(SalesCollection.amount), ZERO)).where(SalesCollection.lead_id == Lead.id).correlate(Lead).scalar_subquery()
    amount = case(
        (Lead.sales_status == SalesStatus.PROPOSAL, func.coalesce(Lead.proposal_value, zero)),
        (Lead.sales_status == SalesStatus.NEGOTIATION, func.coalesce(Lead.negotiation_value, zero)),
        (Lead.sales_status.in_((SalesStatus.NEW, SalesStatus.QUALIFIED, SalesStatus.CONTACTED, SalesStatus.ENGAGED)), func.coalesce(Lead.estimated_value, zero)),
        else_=zero,
    )
    rows = (await session.execute(select(
        Lead.sales_status, func.count(Lead.id), func.coalesce(func.sum(amount), ZERO),
        func.coalesce(func.sum(case((Lead.sales_status == SalesStatus.WON, func.coalesce(Lead.won_value, zero)), else_=zero)), ZERO),
        func.coalesce(func.sum(case((Lead.sales_status == SalesStatus.WON, collected_by_lead), else_=zero)), ZERO),
    ).where(Lead.currency == currency).group_by(Lead.sales_status))).all()
    by_status = {row[0]: row[1:] for row in rows}
    return [{
        "sales_status": status, "lead_count": by_status.get(status, (0, ZERO, ZERO, ZERO))[0],
        "current_opportunity_value": by_status.get(status, (0, ZERO, ZERO, ZERO))[1],
        "won_value": by_status.get(status, (0, ZERO, ZERO, ZERO))[2],
        "collected_value": by_status.get(status, (0, ZERO, ZERO, ZERO))[3],
    } for status in SalesStatus]


async def conversion_analytics(session: AsyncSession, currency: str) -> dict:
    currency = require_currency(currency)
    rows = (await session.execute(select(
        SalesActivity.lead_id, SalesActivity.old_sales_status, SalesActivity.new_sales_status,
    ).join(Lead, Lead.id == SalesActivity.lead_id).where(
        Lead.currency == currency, SalesActivity.activity_type == "STATUS_CHANGE",
        SalesActivity.old_sales_status.is_not(None), SalesActivity.new_sales_status.is_not(None),
    ))).all()
    entering: dict[SalesStatus, set[UUID]] = {status: set() for status in SalesStatus}
    transitions: dict[tuple[SalesStatus, SalesStatus], set[UUID]] = {}
    tracked: set[UUID] = set()
    won_tracked: set[UUID] = set()
    for lead_id, old, new in rows:
        tracked.add(lead_id)
        entering[new].add(lead_id)
        transitions.setdefault((old, new), set()).add(lead_id)
        if new == SalesStatus.WON:
            won_tracked.add(lead_id)
    # NEW is the initial application state, so its auditable source cohort is
    # leads with an explicit structured transition out of NEW.
    entering[SalesStatus.NEW] = {lead_id for (old, _), ids in transitions.items() if old == SalesStatus.NEW for lead_id in ids}
    steps = []
    for source, target in CONVERSION_TRANSITIONS:
        source_ids = entering[source]
        converted_ids = transitions.get((source, target), set())
        pct = (Decimal(len(converted_ids)) * Decimal(100) / Decimal(len(source_ids))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if source_ids else None
        steps.append({"from_status": source, "to_status": target, "source_count": len(source_ids), "converted_count": len(converted_ids), "conversion_percentage": pct})
    tracked_won = len(tracked)
    overall = (Decimal(len(won_tracked)) * Decimal(100) / Decimal(tracked_won)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if tracked_won else None
    won_count = await session.scalar(select(func.count(Lead.id)).where(Lead.currency == currency, Lead.sales_status == SalesStatus.WON)) or 0
    lost_count = await session.scalar(select(func.count(Lead.id)).where(Lead.currency == currency, Lead.sales_status == SalesStatus.LOST)) or 0
    open_count = await session.scalar(select(func.count(Lead.id)).where(Lead.currency == currency, Lead.sales_status.in_(OPEN_STATUSES))) or 0
    won_values = (await session.scalars(select(Lead.won_value).where(Lead.currency == currency, Lead.sales_status == SalesStatus.WON, Lead.won_value.is_not(None)))).all()
    proposal_values = (await session.scalars(select(Lead.proposal_value).where(Lead.currency == currency, Lead.sales_status == SalesStatus.PROPOSAL, Lead.proposal_value.is_not(None)))).all()
    average_won = sum(won_values, ZERO) / len(won_values) if won_values else None
    average_proposal = sum(proposal_values, ZERO) / len(proposal_values) if proposal_values else None
    return {
        "steps": steps, "tracked_lead_count": tracked_won,
        "overall_conversion_percentage": overall, "won_deal_count": won_count,
        "lost_deal_count": lost_count, "open_opportunity_count": open_count,
        "average_won_deal_value": average_won, "average_proposal_value": average_proposal,
        "history_limitation": "Funnel transitions and overall conversion use structured status history recorded from migration 0012 onward; older note-only status history is not inferred.",
    }

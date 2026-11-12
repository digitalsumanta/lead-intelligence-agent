"""
Repository layer for supervisor dispatch delivery events (Delivery Telemetry Ledger).

Phase 12J - D2 - Gate D-C4:
Provides append-only persistence, idempotent deduplication by (provider_name, provider_event_id),
and safe query interfaces for asynchronous reconciliation and forensic auditing.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import SupervisorDispatchDeliveryEvent, now_utc

logger = logging.getLogger("lead_agent.repositories.delivery_events")


class SupervisorDeliveryEventRepository:
    """Repository handling delivery telemetry event persistence and reconciliation queries."""

    @classmethod
    async def record_delivery_event(
        cls,
        session: AsyncSession,
        *,
        provider_name: str,
        provider_message_id: str,
        provider_event_id: str,
        event_type: str,
        raw_event_type: str,
        event_timestamp: datetime,
        dispatch_id: Optional[uuid.UUID] = None,
        dispatch_reference_id: Optional[uuid.UUID] = None,
        lead_reference_id: Optional[uuid.UUID] = None,
        recipient_masked: Optional[str] = None,
        bounce_type: Optional[str] = None,
        bounce_code: Optional[str] = None,
        bounce_reason: Optional[str] = None,
        metadata_json: Optional[dict[str, Any]] = None,
        received_at: Optional[datetime] = None,
    ) -> tuple[SupervisorDispatchDeliveryEvent, bool]:
        """
        Idempotently records a provider delivery event.
        Returns tuple (event, is_duplicate).
        If (provider_name, provider_event_id) already exists, returns (existing_event, True).
        """
        existing = await cls.get_by_provider_event(
            session,
            provider_name=provider_name,
            provider_event_id=provider_event_id,
        )
        if existing:
            logger.info(
                "Duplicate provider event detected: provider=%s, event_id=%s. Acknowledging idempotently.",
                provider_name,
                provider_event_id,
            )
            return existing, True

        event_rec = SupervisorDispatchDeliveryEvent(
            id=uuid.uuid4(),
            dispatch_id=dispatch_id,
            dispatch_reference_id=dispatch_reference_id or dispatch_id,
            lead_reference_id=lead_reference_id,
            provider_name=provider_name.upper(),
            provider_message_id=provider_message_id,
            provider_event_id=provider_event_id,
            event_type=event_type.upper(),
            raw_event_type=raw_event_type,
            event_timestamp=event_timestamp,
            received_at=received_at or now_utc(),
            recipient_masked=recipient_masked,
            bounce_type=bounce_type.upper() if bounce_type else None,
            bounce_code=bounce_code,
            bounce_reason=bounce_reason,
            metadata_json=metadata_json or {},
            processed_for_reconciliation=False,
            created_at=now_utc(),
        )

        session.add(event_rec)
        try:
            await session.flush()
            return event_rec, False
        except IntegrityError:
            # Race condition on concurrent duplicate insert
            await session.rollback()
            existing = await cls.get_by_provider_event(
                session,
                provider_name=provider_name,
                provider_event_id=provider_event_id,
            )
            if existing:
                return existing, True
            raise

    @classmethod
    async def get_by_id(
        cls,
        session: AsyncSession,
        event_id: uuid.UUID,
    ) -> Optional[SupervisorDispatchDeliveryEvent]:
        """Fetches a delivery event by primary key."""
        stmt = select(SupervisorDispatchDeliveryEvent).where(SupervisorDispatchDeliveryEvent.id == event_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @classmethod
    async def get_by_provider_event(
        cls,
        session: AsyncSession,
        *,
        provider_name: str,
        provider_event_id: str,
    ) -> Optional[SupervisorDispatchDeliveryEvent]:
        """Look up by composite unique key (provider_name, provider_event_id)."""
        stmt = select(SupervisorDispatchDeliveryEvent).where(
            SupervisorDispatchDeliveryEvent.provider_name == provider_name.upper(),
            SupervisorDispatchDeliveryEvent.provider_event_id == provider_event_id,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @classmethod
    async def get_by_provider_message_id(
        cls,
        session: AsyncSession,
        provider_message_id: str,
    ) -> Sequence[SupervisorDispatchDeliveryEvent]:
        """Fetch all chronological events for a provider message ID."""
        stmt = (
            select(SupervisorDispatchDeliveryEvent)
            .where(SupervisorDispatchDeliveryEvent.provider_message_id == provider_message_id)
            .order_by(SupervisorDispatchDeliveryEvent.created_at.asc())
        )
        result = await session.execute(stmt)
        return result.scalars().all()

    @classmethod
    async def get_by_dispatch_reference_id(
        cls,
        session: AsyncSession,
        dispatch_reference_id: uuid.UUID,
    ) -> Sequence[SupervisorDispatchDeliveryEvent]:
        """Fetch all chronological events for a dispatch reference ID."""
        stmt = (
            select(SupervisorDispatchDeliveryEvent)
            .where(SupervisorDispatchDeliveryEvent.dispatch_reference_id == dispatch_reference_id)
            .order_by(SupervisorDispatchDeliveryEvent.created_at.asc())
        )
        result = await session.execute(stmt)
        return result.scalars().all()

    @classmethod
    async def get_unprocessed_events(
        cls,
        session: AsyncSession,
        *,
        limit: int = 100,
    ) -> Sequence[SupervisorDispatchDeliveryEvent]:
        """Fetch un-reconciled events in order of arrival."""
        stmt = (
            select(SupervisorDispatchDeliveryEvent)
            .where(SupervisorDispatchDeliveryEvent.processed_for_reconciliation.is_(False))
            .order_by(SupervisorDispatchDeliveryEvent.created_at.asc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return result.scalars().all()

    @classmethod
    async def mark_event_processed(
        cls,
        session: AsyncSession,
        event_id: uuid.UUID,
    ) -> None:
        """Mark an event as evaluated by the reconciler."""
        stmt = (
            update(SupervisorDispatchDeliveryEvent)
            .where(SupervisorDispatchDeliveryEvent.id == event_id)
            .values(processed_for_reconciliation=True)
        )
        await session.execute(stmt)

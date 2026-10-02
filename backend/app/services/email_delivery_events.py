"""Validated append-only recording and retrieval of email delivery events."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiOutreachExecution, EmailDeliveryEvent
from app.models.entities import now_utc
from app.schemas.email_delivery_event import (
    EmailDeliveryEventCreate, EmailDeliveryEventOut, NormalizedEmailDeliveryEvent,
)


class EmailDeliveryEventError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class EmailDeliveryEventService:
    """Append-only delivery-event operations. No sales or task mutations occur here."""

    async def record(self, session: AsyncSession, request: EmailDeliveryEventCreate) -> tuple[EmailDeliveryEventOut, bool]:
        execution = await session.get(AiOutreachExecution, request.execution_id)
        if execution is None:
            raise EmailDeliveryEventError("EXECUTION_NOT_FOUND", "Email execution not found", 404)

        provider = execution.provider.casefold()

        occurred_at = _as_utc(request.occurred_at or now_utc())
        now = now_utc()
        started_at = _as_utc(execution.started_at)
        if occurred_at < started_at - timedelta(minutes=5) or occurred_at > now + timedelta(minutes=5):
            raise EmailDeliveryEventError(
                "INVALID_EVENT_TIMESTAMP",
                "occurred_at must be close to or later than the execution start and not in the future",
            )
        if (execution.provider_message_id and request.provider_message_id
                and execution.provider_message_id != request.provider_message_id):
            raise EmailDeliveryEventError(
                "PROVIDER_MESSAGE_MISMATCH", "Provider message ID does not match the email execution", 409,
            )

        event = EmailDeliveryEvent(
            execution_id=execution.id,
            event_type=request.event_type,
            recipient_email=execution.recipient,
            provider=provider.casefold(),
            provider_message_id=execution.provider_message_id or request.provider_message_id,
            provider_event_id=request.provider_event_id,
            provider_status=request.provider_status,
            occurred_at=occurred_at,
            recorded_at=now,
            metadata_json=request.metadata,
            source=request.source,
        )
        session.add(event)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            if request.provider_event_id:
                duplicate = await session.scalar(select(EmailDeliveryEvent).where(
                    EmailDeliveryEvent.provider == provider,
                    EmailDeliveryEvent.provider_event_id == request.provider_event_id,
                ))
                if duplicate is not None:
                    if duplicate.execution_id == request.execution_id and duplicate.source == request.source:
                        return _output(duplicate), False
                    raise EmailDeliveryEventError(
                        "PROVIDER_EVENT_ID_CONFLICT",
                        "Provider event ID is already associated with a different execution", 409,
                    ) from None
            raise
        await session.refresh(event)
        return _output(event), True

    async def history(self, session: AsyncSession, execution_id: UUID) -> list[EmailDeliveryEventOut]:
        if await session.get(AiOutreachExecution, execution_id) is None:
            raise EmailDeliveryEventError("EXECUTION_NOT_FOUND", "Email execution not found", 404)
        rows = (await session.scalars(select(EmailDeliveryEvent).where(
            EmailDeliveryEvent.execution_id == execution_id,
        ).order_by(EmailDeliveryEvent.occurred_at, EmailDeliveryEvent.recorded_at, EmailDeliveryEvent.id))).all()
        return [_output(row) for row in rows]


class EmailDeliveryEventIngestionService:
    """Resolve and persist normalized observations without downstream side effects."""

    def __init__(self, persistence: EmailDeliveryEventService):
        self.persistence = persistence

    async def ingest(
        self, session: AsyncSession, request: NormalizedEmailDeliveryEvent,
    ) -> tuple[EmailDeliveryEventOut, UUID, bool]:
        provider = request.provider.strip().casefold() if request.provider else None
        if not provider:
            raise EmailDeliveryEventError("PROVIDER_REQUIRED", "A provider identifier is required")
        if not request.provider_message_id:
            raise EmailDeliveryEventError(
                "PROVIDER_MESSAGE_ID_REQUIRED", "A provider message ID is required to resolve the execution",
            )

        existing = None
        if request.provider_event_id:
            existing = await session.scalar(select(EmailDeliveryEvent).where(
                func.lower(EmailDeliveryEvent.provider) == provider,
                EmailDeliveryEvent.provider_event_id == request.provider_event_id,
            ))
            if existing is not None:
                if not _same_normalized_event(existing, request, provider):
                    raise EmailDeliveryEventError(
                        "IDEMPOTENCY_CONFLICT",
                        "Provider event ID was already used for different normalized content", 409,
                    )
                return _output(existing), existing.execution_id, False

        matches = (await session.scalars(select(AiOutreachExecution).where(
            func.lower(AiOutreachExecution.provider) == provider,
            AiOutreachExecution.provider_message_id == request.provider_message_id,
        ).limit(2))).all()
        if len(matches) > 1:
            raise EmailDeliveryEventError(
                "AMBIGUOUS_EXECUTION", "Provider message ID matches multiple executions", 409,
            )
        if not matches:
            other_provider = await session.scalar(select(AiOutreachExecution.id).where(
                AiOutreachExecution.provider_message_id == request.provider_message_id,
            ).limit(1))
            if other_provider is not None:
                raise EmailDeliveryEventError(
                    "PROVIDER_MISMATCH", "Provider does not match the execution owning this message ID", 409,
                )
            raise EmailDeliveryEventError(
                "EXECUTION_NOT_FOUND", "No email execution matches the provider message ID", 404,
            )
        execution = matches[0]
        execution_id = execution.id
        if request.recipient_email and request.recipient_email.strip().casefold() != (execution.recipient or "").casefold():
            raise EmailDeliveryEventError(
                "RECIPIENT_MISMATCH", "Recipient does not match the email execution", 409,
            )

        persistence_request = EmailDeliveryEventCreate(
            execution_id=execution_id,
            event_type=request.event_type,
            provider_message_id=request.provider_message_id,
            provider_event_id=request.provider_event_id,
            provider_status=request.provider_status,
            occurred_at=request.occurred_at,
            metadata=request.metadata,
            source=request.source,
        )
        try:
            event, created = await self.persistence.record(session, persistence_request)
        except IntegrityError:
            # The provider/event unique index is the concurrency authority. Re-read after
            # the losing transaction has been rolled back by record().
            if request.provider_event_id:
                duplicate = await session.scalar(select(EmailDeliveryEvent).where(
                    func.lower(EmailDeliveryEvent.provider) == provider,
                    EmailDeliveryEvent.provider_event_id == request.provider_event_id,
                ))
                if duplicate is not None:
                    if not _same_normalized_event(duplicate, request, provider):
                        raise EmailDeliveryEventError(
                            "IDEMPOTENCY_CONFLICT",
                            "Provider event ID was already used for different normalized content", 409,
                        ) from None
                    return _output(duplicate), duplicate.execution_id, False
            raise
        if request.provider_event_id and not created:
            # The older Phase 11B uniqueness key may win first; enforce Phase 11C's
            # conflicting-content semantics even on that path.
            persisted = await session.get(EmailDeliveryEvent, event.id)
            if persisted is not None and not _same_normalized_event(persisted, request, provider):
                raise EmailDeliveryEventError(
                    "IDEMPOTENCY_CONFLICT",
                    "Provider event ID was already used for different normalized content", 409,
                )
        return event, execution_id, created


def add_execution_delivery_event(
    session: AsyncSession,
    execution: AiOutreachExecution,
    event_type: str,
) -> EmailDeliveryEvent:
    """Stage a trusted provider result in the execution's existing DB transaction."""
    event = EmailDeliveryEvent(
        execution_id=execution.id,
        event_type=event_type,
        recipient_email=execution.recipient,
        provider=execution.provider.casefold(),
        provider_message_id=execution.provider_message_id,
        provider_status=execution.provider_status,
        occurred_at=_as_utc(execution.completed_at or now_utc()),
        recorded_at=now_utc(),
        source="PROVIDER",
    )
    session.add(event)
    return event


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _output(row: EmailDeliveryEvent) -> EmailDeliveryEventOut:
    return EmailDeliveryEventOut(
        id=row.id, execution_id=row.execution_id, event_type=row.event_type,
        recipient_email=row.recipient_email, provider_message_id=row.provider_message_id,
        provider=row.provider,
        provider_event_id=row.provider_event_id, provider_status=row.provider_status,
        occurred_at=_as_utc(row.occurred_at), recorded_at=_as_utc(row.recorded_at),
        source=row.source,
    )


email_delivery_event_service = EmailDeliveryEventService()
email_delivery_event_ingestion_service = EmailDeliveryEventIngestionService(email_delivery_event_service)


def _same_normalized_event(
    existing: EmailDeliveryEvent,
    request: NormalizedEmailDeliveryEvent,
    provider: str,
) -> bool:
    if existing.provider is None or existing.provider.casefold() != provider:
        return False
    if (
        existing.source != request.source
        or existing.event_type != request.event_type
        or existing.provider_message_id != request.provider_message_id
        or (request.recipient_email is not None and existing.recipient_email.casefold() != request.recipient_email.strip().casefold())
        or existing.provider_status != request.provider_status
        or (existing.metadata_json or None) != (request.metadata or None)
    ):
        return False
    if request.occurred_at is not None:
        return _as_utc(existing.occurred_at) == _as_utc(request.occurred_at)
    return True

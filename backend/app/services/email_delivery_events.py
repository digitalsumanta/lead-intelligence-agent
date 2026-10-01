"""Validated append-only recording and retrieval of email delivery events."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiOutreachExecution, EmailDeliveryEvent
from app.models.entities import now_utc
from app.schemas.email_delivery_event import EmailDeliveryEventCreate, EmailDeliveryEventOut


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
                    EmailDeliveryEvent.execution_id == request.execution_id,
                    EmailDeliveryEvent.source == request.source,
                    EmailDeliveryEvent.provider_event_id == request.provider_event_id,
                ))
                if duplicate is not None:
                    return _output(duplicate), False
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
        provider_event_id=row.provider_event_id, provider_status=row.provider_status,
        occurred_at=_as_utc(row.occurred_at), recorded_at=_as_utc(row.recorded_at),
        source=row.source,
    )


email_delivery_event_service = EmailDeliveryEventService()

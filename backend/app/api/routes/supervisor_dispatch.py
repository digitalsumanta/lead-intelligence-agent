"""
REST API Router for Supervisor Approval Dispatches (Transactional Outbox).

Phase 12J - D2 - Gate D-B2:
Provides secure endpoints for enqueueing approved actions, querying outbox records,
operator reconciliation of ambiguous outcomes, and safe inert dry-run testing.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.supervisor_approval import get_authenticated_principal
from app.config import settings
from app.database import get_db
from app.domain.dispatch_errors import (
    DispatchAbortedError,
    DispatchAmbiguousOutcomeError,
    DispatchDisabledError,
    DispatchDuplicateError,
    DispatchFencingConflictError,
    DispatchInvalidTransitionError,
    DispatchNotFoundError,
    DispatchRedactedPayloadError,
)
from app.domain.sales_supervisor import AuthenticatedPrincipal
from app.models.entities import SupervisorApprovalDispatch
from app.schemas.supervisor_dispatch import (
    SupervisorDispatchRecordOut,
    SupervisorDispatchReconcileRequest,
)
from app.services.supervisor_approval_service import SUPERVISOR_APPROVER_ROLES
from app.services.supervisor_dispatch_service import SupervisorApprovalDispatchService

logger = logging.getLogger("lead_agent.api.supervisor_dispatch")

router = APIRouter(prefix="/api/sales-supervisor/dispatches", tags=["Sales Supervisor Dispatches"])


class SupervisorDispatchEnqueueBody(BaseModel):
    approval_id: UUID
    payload_json: Optional[dict[str, Any]] = None


class SupervisorDispatchDryRunBody(BaseModel):
    approval_id: UUID
    worker_id: Optional[str] = "dry-run-worker"


def _to_dispatch_out(record: SupervisorApprovalDispatch) -> SupervisorDispatchRecordOut:
    """Converts ORM entity to Pydantic response model."""
    return SupervisorDispatchRecordOut(
        id=record.id,
        approval_id=record.approval_id,
        approval_reference_id=record.approval_reference_id,
        lead_id=record.lead_id,
        lead_reference_id=record.lead_reference_id,
        action_id=record.action_id,
        channel=record.channel,
        recipient=record.recipient,
        context_fingerprint=record.context_fingerprint,
        idempotency_key=record.idempotency_key,
        payload_hash=record.payload_hash,
        payload_json=record.payload_json,
        status=record.status,
        dispatch_worker_id=record.dispatch_worker_id,
        lease_generation=record.lease_generation,
        provider_name=record.provider_name,
        provider_message_id=record.provider_message_id,
        attempt_count=record.attempt_count,
        max_attempts=record.max_attempts,
        last_attempt_at=record.last_attempt_at,
        next_retry_at=record.next_retry_at,
        completed_at=record.completed_at,
        error_code=record.error_code,
        error_message=record.error_message,
        reconciliation_notes=record.reconciliation_notes,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


@router.post(
    "/enqueue",
    response_model=SupervisorDispatchRecordOut,
    summary="Enqueue an approved sales action into the transactional dispatch outbox",
)
async def enqueue_dispatch_endpoint(
    body: SupervisorDispatchEnqueueBody,
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
    db: AsyncSession = Depends(get_db),
):
    """
    Atomically creates an outbox dispatch record for an approved action.
    Fails closed if the global or channel kill switch is disabled.
    """
    service = SupervisorApprovalDispatchService()
    try:
        record = await service.enqueue_dispatch(
            db,
            approval_id=body.approval_id,
            payload_json=body.payload_json,
        )
        await db.commit()
        await db.refresh(record)
        return _to_dispatch_out(record)
    except DispatchDisabledError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": exc.code, "message": str(exc)})
    except DispatchNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": exc.code, "message": str(exc)})
    except (DispatchInvalidTransitionError, DispatchDuplicateError) as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)})


@router.get(
    "/{dispatch_id}",
    response_model=SupervisorDispatchRecordOut,
    summary="Query dispatch outbox record by primary key",
)
async def get_dispatch_endpoint(
    dispatch_id: UUID,
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
    db: AsyncSession = Depends(get_db),
):
    """Fetches dispatch outbox record details."""
    service = SupervisorApprovalDispatchService()
    record = await service.repository.get_by_id(db, dispatch_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "DISPATCH_NOT_FOUND", "message": f"Dispatch '{dispatch_id}' not found"},
        )
    return _to_dispatch_out(record)


@router.post(
    "/{dispatch_id}/reconcile",
    response_model=SupervisorDispatchRecordOut,
    summary="Manual operator reconciliation of an OUTCOME_UNKNOWN dispatch record",
)
async def reconcile_dispatch_endpoint(
    dispatch_id: UUID,
    body: SupervisorDispatchReconcileRequest,
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
    db: AsyncSession = Depends(get_db),
):
    """
    Allows an authorized manager/admin to manually reconcile an ambiguous outcome.
    Requires role: sales_manager, admin, or sales_admin.
    """
    role = (principal.role or "").strip().lower()
    if role not in SUPERVISOR_APPROVER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "PERMISSION_DENIED", "message": f"Role '{role}' is not authorized to reconcile dispatches"},
        )

    service = SupervisorApprovalDispatchService()
    try:
        record = await service.reconcile_unknown_dispatch(
            db,
            dispatch_id=dispatch_id,
            resolution=body.resolution,
            provider_message_id=body.provider_message_id,
            notes=body.notes,
        )
        await db.commit()
        await db.refresh(record)
        return _to_dispatch_out(record)
    except DispatchNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": exc.code, "message": str(exc)})
    except DispatchInvalidTransitionError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)})

"""
HTTP API Router for Canary Flight Two-Person Authorization.

Phase 12J - D2 - Gate D-D3:
Provides REST endpoints for defining canary flight scopes, submitting multi-party
approval signatures (ACM-05), revoking flights, and querying flight status.
"""
from __future__ import annotations

import logging
import uuid
from typing import Sequence

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.supervisor_approval import get_authenticated_principal
from app.database import get_db
from app.domain.dispatch_errors import (
    CanaryAuthorizationError,
    CanaryDualApprovalRequiredError,
    CanaryFlightExpiredError,
    CanaryFlightRevokedError,
    CanaryMessageCapExceededError,
    CanaryScopeDriftError,
    CanarySelfApprovalError,
)
from app.domain.sales_supervisor import AuthenticatedPrincipal, is_shared_development_identity
from app.schemas.supervisor_canary import (
    CanaryFlightApproveRequest,
    CanaryFlightCreateRequest,
    CanaryFlightResponse,
    CanaryFlightRevokeRequest,
)
from app.services.canary_authorization_service import CanaryAuthorizationService

logger = logging.getLogger("lead_agent.api.supervisor_canary")

router = APIRouter(prefix="/api/sales-supervisor/canary/flights", tags=["Sales Supervisor Canary Flights"])


@router.post("", response_model=CanaryFlightResponse, status_code=status.HTTP_201_CREATED)
async def create_canary_flight(
    payload: CanaryFlightCreateRequest,
    session: AsyncSession = Depends(get_db),
) -> CanaryFlightResponse:
    """Creates a proposed canary flight scope in PENDING_APPROVAL status."""
    try:
        flight = await CanaryAuthorizationService.create_flight_scope(
            session,
            flight_name=payload.flight_name,
            environment=payload.environment,
            provider_name=payload.provider_name,
            channel=payload.channel,
            sender_identity=payload.sender_identity,
            allowed_domains=payload.allowed_domains,
            max_messages=payload.max_messages,
            max_concurrency=payload.max_concurrency,
            rate_limit_per_second=payload.rate_limit_per_second,
            valid_from=payload.valid_from,
            expires_at=payload.expires_at,
        )
        await session.commit()
        return CanaryFlightResponse.model_validate(flight)
    except CanaryAuthorizationError as err:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": err.code, "message": str(err)},
        )
    except Exception as err:
        await session.rollback()
        logger.exception("Failed to create canary flight")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "INTERNAL_ERROR", "message": str(err)},
        )


@router.get("/{flight_id}", response_model=CanaryFlightResponse)
async def get_canary_flight(
    flight_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> CanaryFlightResponse:
    """Retrieves current flight authorization state."""
    flight = await CanaryAuthorizationService.get_flight(session, flight_id)
    if not flight:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "FLIGHT_NOT_FOUND", "message": f"Canary flight '{flight_id}' not found"},
        )
    return CanaryFlightResponse.model_validate(flight)


@router.get("", response_model=list[CanaryFlightResponse])
async def list_canary_flights(
    limit: int = Query(50, ge=1, le=100),
    session: AsyncSession = Depends(get_db),
) -> list[CanaryFlightResponse]:
    """Lists recent canary flight authorizations."""
    flights = await CanaryAuthorizationService.list_flights(session, limit=limit)
    return [CanaryFlightResponse.model_validate(f) for f in flights]


@router.post("/{flight_id}/approve", response_model=CanaryFlightResponse)
async def approve_canary_flight(
    flight_id: uuid.UUID,
    payload: CanaryFlightApproveRequest,
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
    session: AsyncSession = Depends(get_db),
) -> CanaryFlightResponse:
    """
    Submits an approval signature for a canary flight.
    Requires two distinct authenticated human identities. Prevents self-approval.
    Binds signer identity strictly to verified authentication context (ACM-05, Finding D3.1-01).
    """
    # 1. Fail closed on unverified, generic, or shared development identities
    if is_shared_development_identity(principal.user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "UNVERIFIED_SHARED_IDENTITY",
                "message": f"Generic or shared development identity '{principal.user_id}' cannot sign canary flight authorizations",
            },
        )

    # 2. Reject untrusted request-body signer overrides / impersonation attempts
    if payload.signer_user_id and payload.signer_user_id.strip() != principal.user_id.strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "IMPERSONATION_FORBIDDEN",
                "message": f"Signer identity '{payload.signer_user_id}' does not match authenticated principal '{principal.user_id}'",
            },
        )

    # 3. Derive authoritative signer identity and role
    signer_user_id = principal.user_id.strip()
    signer_role = (
        payload.signer_role.strip()
        if payload.signer_role and payload.signer_role.strip()
        else (principal.role.strip() if principal.role else "")
    )
    if not signer_role:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "ROLE_REQUIRED", "message": "Signer role must not be empty"},
        )

    try:
        flight = await CanaryAuthorizationService.submit_flight_approval(
            session,
            flight_id=flight_id,
            signer_user_id=signer_user_id,
            signer_role=signer_role,
            scope_fingerprint=payload.scope_fingerprint,
        )
        await session.commit()
        return CanaryFlightResponse.model_validate(flight)
    except CanaryFlightExpiredError as err:
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"code": err.code, "message": str(err)},
        )
    except CanaryFlightRevokedError as err:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": err.code, "message": str(err)},
        )
    except CanarySelfApprovalError as err:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": err.code, "message": str(err)},
        )
    except CanaryScopeDriftError as err:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": err.code, "message": str(err)},
        )
    except (CanaryAuthorizationError, CanaryMessageCapExceededError) as err:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": err.code, "message": str(err)},
        )
    except Exception as err:
        await session.rollback()
        logger.exception("Failed to approve canary flight %s", flight_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "INTERNAL_ERROR", "message": str(err)},
        )


@router.post("/{flight_id}/revoke", response_model=CanaryFlightResponse)
async def revoke_canary_flight(
    flight_id: uuid.UUID,
    payload: CanaryFlightRevokeRequest,
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
    session: AsyncSession = Depends(get_db),
) -> CanaryFlightResponse:
    """Revokes a canary flight authorization immediately."""
    if is_shared_development_identity(principal.user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "UNVERIFIED_SHARED_IDENTITY",
                "message": f"Generic or shared development identity '{principal.user_id}' cannot revoke canary flight authorizations",
            },
        )
    if payload.revoked_by_user_id and payload.revoked_by_user_id.strip() != principal.user_id.strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "IMPERSONATION_FORBIDDEN",
                "message": f"Revoking user '{payload.revoked_by_user_id}' does not match authenticated principal '{principal.user_id}'",
            },
        )
    revoked_by_user_id = principal.user_id.strip()

    try:
        flight = await CanaryAuthorizationService.revoke_flight_authorization(
            session,
            flight_id=flight_id,
            revoked_by_user_id=revoked_by_user_id,
            revocation_reason=payload.revocation_reason,
        )
        await session.commit()
        return CanaryFlightResponse.model_validate(flight)
    except CanaryAuthorizationError as err:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND if "not found" in str(err).lower() else status.HTTP_400_BAD_REQUEST,
            detail={"code": err.code, "message": str(err)},
        )
    except Exception as err:
        await session.rollback()
        logger.exception("Failed to revoke canary flight %s", flight_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "INTERNAL_ERROR", "message": str(err)},
        )

"""
HTTP API Endpoints for Durable Sales Supervisor Human Approvals.

Implements controlled API lifecycle integration:
- Authentication via local_api_auth middleware
- Role-based authorization for supervisor operations
- Authoritative server-side database context derivation (zero client-supplied coordinates)
- Context-fingerprint verification & context-drift detection
- Transactional audit log persistence and dual-reference retrieval
- Atomic Compare-And-Swap (CAS) execution claiming
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import uuid
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.config import settings
from app.domain.contact_recommendation import RecommendedSalesAction
from app.domain.sales_supervisor import (
    ApprovalClaimConflictError,
    ApprovalContextDriftError,
    ApprovalExpiredError,
    ApprovalFencingConflictError,
    ApprovalInvalidTransitionError,
    ApprovalLeaseActiveError,
    ApprovalLeaseExpiredError,
    ApprovalNotFoundError,
    ApprovalNotLeaseOwnerError,
    ApprovalPermissionDeniedError,
    ApprovalSelfApprovalError,
    ApprovalUnverifiedIdentityError,
    ApprovalVersionConflictError,
    AuthenticatedPrincipal,
    SupervisorActionReadinessStatus,
    SupervisorActionTier,
    SupervisorApprovalStatus,
    SupervisorExecutionDecision,
    classify_supervisor_action,
    compute_action_context_fingerprint,
)
from app.models.entities import (
    DEFAULT_APPROVAL_VALIDITY_HOURS,
    Lead,
    LeadIntelligenceOpportunity,
    SalesTask,
    SalesTaskStatus,
    SupervisorApprovalAuditEvent,
    SupervisorHumanApproval,
    now_utc,
)
from app.repositories.supervisor_approval import SupervisorApprovalRepository
from app.schemas.sales_supervisor import (
    ActionReadinessDetails,
    PrioritizedSalesAction,
    SupervisorApprovalApproveRequest,
    SupervisorApprovalAuditListResponse,
    SupervisorApprovalAuditRecordOut,
    SupervisorApprovalClaimRequest,
    SupervisorApprovalHeartbeatRequest,
    SupervisorApprovalHeartbeatResponse,
    SupervisorApprovalListResponse,
    SupervisorApprovalRecordOut,
    SupervisorApprovalRecoverRequest,
    SupervisorApprovalRejectRequest,
    SupervisorApprovalReleaseRequest,
    SupervisorApprovalRequestCreate,
    SupervisorApprovalRevokeRequest,
    SupervisorExecutionAuthorizationResult,
    SupervisorHumanApproval as SupervisorHumanApprovalSchema,
)
from app.services.sales_supervisor import (
    _derive_expected_channel,
    build_action_identity,
    evaluate_action_readiness,
)
from app.services.supervisor_approval_service import (
    SUPERVISOR_APPROVER_ROLES,
    SupervisorApprovalService,
    _ensure_utc,
    _to_approval_schema,
)

logger = logging.getLogger("lead_agent.api.supervisor_approval")

router = APIRouter(prefix="/api/sales-supervisor/approvals", tags=["Sales Supervisor Approvals"])


# ============================================================================
# Authentication and Authorization Dependencies
# ============================================================================

def get_authenticated_principal(request: Request) -> AuthenticatedPrincipal:
    """
    Extract authenticated principal from request state populated by local_api_auth middleware.
    In the internal development architecture, a configured bearer token maps to local_api_user.
    Never trusts actor identity or role supplied solely in a request body.
    """
    user_id = getattr(request.state, "authenticated_user_id", None)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "Valid API bearer token required"},
        )

    # Prevent actor impersonation attempts via headers
    claimed_actor = request.headers.get("x-actor-id")
    if claimed_actor and claimed_actor.strip() != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "IMPERSONATION_FORBIDDEN", "message": "Actor impersonation is strictly forbidden"},
        )

    # Authoritative role resolution:
    # 1. Derive trusted role from request.state or server configuration (defaulting to unprivileged "viewer")
    trusted_role = getattr(request.state, "authenticated_role", None) or settings.api_auth_role.strip() or "viewer"
    role = trusted_role

    # 2. X-Actor-Role header is strictly ignored in production/staging environments
    claimed_role = request.headers.get("x-actor-role")
    if claimed_role:
        if settings.effective_allow_dev_role_override:
            role = claimed_role.strip()
        else:
            if claimed_role.strip().lower() in SUPERVISOR_APPROVER_ROLES:
                logger.warning(
                    "Ignoring client-supplied X-Actor-Role '%s' for principal '%s' (dev role override disabled in environment '%s')",
                    claimed_role,
                    user_id,
                    settings.app_env,
                )
            role = trusted_role

    return AuthenticatedPrincipal(user_id=user_id, role=role, is_authenticated=True)


def require_approver_role(
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
) -> AuthenticatedPrincipal:
    """Requires principal to possess a supervisor approver role (e.g. sales_manager, admin)."""
    if principal.role.lower() not in SUPERVISOR_APPROVER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN",
                "message": f"Role '{principal.role}' is not authorized to perform supervisor approvals",
            },
        )
    return principal


# ============================================================================
# Authoritative Context Derivation Helper
# ============================================================================

async def resolve_authoritative_action_context(
    session: AsyncSession,
    *,
    lead_id: UUID,
    opportunity_id: UUID,
    contact_id: Optional[UUID] = None,
    requested_action: Optional[str] = None,
) -> tuple[PrioritizedSalesAction, Optional[str], Optional[str], Optional[str]]:
    """
    Resolves the lead, opportunity, contact, recipient coordinates, action type,
    evidence, and live readiness strictly from authoritative server-side database records.
    Client-supplied recipient coordinates or readiness overrides are not accepted.
    """
    # 1. Fetch authoritative Lead
    lead = await session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "LEAD_NOT_FOUND", "message": f"Lead '{lead_id}' not found"},
        )

    # 2. Fetch authoritative LeadIntelligenceOpportunity
    opp = await session.get(LeadIntelligenceOpportunity, opportunity_id)
    if opp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "OPPORTUNITY_NOT_FOUND", "message": f"Opportunity '{opportunity_id}' not found"},
        )

    # 3. Cross-validate lead and opportunity (prevent cross-lead identifier substitution)
    if opp.lead_id != lead.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "CROSS_LEAD_OPPORTUNITY_MISMATCH",
                "message": f"Opportunity '{opportunity_id}' does not belong to lead '{lead_id}'",
            },
        )

    # 4. Resolve contact coordinates authoritatively from database
    contact_lead: Optional[Lead] = None
    if contact_id is not None:
        contact_lead = await session.get(Lead, contact_id)
        if contact_lead is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "CONTACT_NOT_FOUND", "message": f"Contact lead '{contact_id}' not found"},
            )
        contact_email = contact_lead.email or lead.email
        contact_phone = contact_lead.phone or lead.phone
        target_contact_id = contact_id
    else:
        contact_email = lead.email
        contact_phone = lead.phone
        target_contact_id = lead.id

    # 5. Resolve action parameters
    rec_action_str = (requested_action or opp.recommended_action or "PERSONALIZED_EMAIL").strip()
    try:
        rec_action_enum = RecommendedSalesAction(rec_action_str)
    except ValueError:
        rec_action_enum = RecommendedSalesAction.PERSONALIZED_EMAIL

    action_id = build_action_identity(target_contact_id, rec_action_enum)
    channel = _derive_expected_channel(rec_action_enum)
    opp_score = float(opp.score or 0.0)
    evidence_keys = sorted(opp.evidence.keys()) if (opp.evidence and isinstance(opp.evidence, dict)) else []

    # 6. Evaluate live readiness gates
    tasks_res = await session.execute(
        select(SalesTask).where(
            SalesTask.lead_id.in_([lead.id, target_contact_id]),
            SalesTask.status.in_([SalesTaskStatus.OPEN, SalesTaskStatus.IN_PROGRESS]),
        )
    )
    active_tasks = list(tasks_res.scalars().all())

    readiness = evaluate_action_readiness(
        opportunity=opp,
        recommended_action=rec_action_enum,
        contact_id=target_contact_id,
        contact_lead=contact_lead or lead,
        company_lead=lead,
        active_tasks=active_tasks,
        priority_score=opp_score,
    )

    action = PrioritizedSalesAction(
        action_id=action_id,
        lead_id=lead.id,
        opportunity_id=opp.id,
        contact_id=contact_id,
        recommended_action=rec_action_enum,
        opportunity_score=opp_score,
        recommendation_fit=0.0,
        urgency_score=50.0,
        priority_score=opp_score,
        readiness_status=readiness.status,
        readiness_details=readiness,
        evidence_keys=evidence_keys,
        policy_approval_required=True,
    )

    return action, channel, contact_email, contact_phone


def _to_record_out(model: SupervisorHumanApproval) -> SupervisorApprovalRecordOut:
    """Map ORM SupervisorHumanApproval to Pydantic response schema."""
    lease_duration = getattr(settings, "supervisor_lease_duration_seconds", 300)
    lease_expires_at = (
        _ensure_utc(model.updated_at) + timedelta(seconds=lease_duration)
        if model.execution_claimed_at is not None
        else None
    )
    return SupervisorApprovalRecordOut(
        id=model.id,
        action_id=model.action_id,
        lead_id=model.lead_id,
        opportunity_id=model.opportunity_id,
        contact_id=model.contact_id,
        recommended_action=model.recommended_action,
        action_tier=model.action_tier,
        channel=model.channel,
        context_fingerprint=model.context_fingerprint,
        status=model.status,
        requested_by_user_id=model.requested_by_user_id,
        approved_by_user_id=model.approved_by_user_id,
        rejected_by_user_id=model.rejected_by_user_id,
        revoked_by_user_id=model.revoked_by_user_id,
        created_at=model.created_at,
        approved_at=model.approved_at,
        expires_at=model.expires_at,
        updated_at=model.updated_at,
        notes=model.notes,
        decision_reason=model.decision_reason,
        version=model.version,
        execution_claimed_at=model.execution_claimed_at,
        execution_claimed_by=model.execution_claimed_by,
        lease_generation=model.version if model.execution_claimed_at is not None else None,
        lease_duration_seconds=lease_duration if model.execution_claimed_at is not None else None,
        lease_expires_at=lease_expires_at,
    )


def _to_audit_out(event: SupervisorApprovalAuditEvent) -> SupervisorApprovalAuditRecordOut:
    """Map ORM SupervisorApprovalAuditEvent to Pydantic response schema."""
    return SupervisorApprovalAuditRecordOut(
        id=event.id,
        approval_id=event.approval_id,
        approval_reference_id=event.approval_reference_id,
        action_id=event.action_id,
        lead_id=event.lead_id,
        actor_id=event.actor_id,
        actor_role=event.actor_role,
        event_type=event.event_type,
        from_status=event.from_status,
        to_status=event.to_status,
        context_fingerprint=event.context_fingerprint,
        reason=event.reason,
        metadata_json=event.metadata_json or {},
        created_at=event.created_at,
    )


# ============================================================================
# API Endpoints
# ============================================================================

@router.post(
    "/request",
    response_model=SupervisorApprovalRecordOut,
    status_code=status.HTTP_201_CREATED,
    summary="Request a new durable human approval",
)
async def request_approval_endpoint(
    body: SupervisorApprovalRequestCreate,
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
    db: AsyncSession = Depends(get_db),
):
    """
    Creates and persists a new durable PENDING approval request.
    Authoritative contact coordinates, scores, evidence, and fingerprint
    are derived server-side from database records.
    """
    action, channel, contact_email, contact_phone = await resolve_authoritative_action_context(
        db,
        lead_id=body.lead_id,
        opportunity_id=body.opportunity_id,
        contact_id=body.contact_id,
        requested_action=body.recommended_action,
    )

    if body.action_id and body.action_id.strip() != action.action_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "ACTION_ID_MISMATCH",
                "message": f"Provided action_id '{body.action_id}' does not match authoritative '{action.action_id}'",
            },
        )

    service = SupervisorApprovalService()
    try:
        approval = await service.request_approval(
            db,
            action=action,
            requested_by=principal,
            channel=channel,
            contact_email=contact_email,
            contact_phone=contact_phone,
            expires_in_hours=body.expires_in_hours,
            notes=body.notes,
        )
        await db.commit()
        await db.refresh(approval)
        return _to_record_out(approval)
    except ApprovalPermissionDeniedError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": exc.code, "message": str(exc)})
    except Exception as exc:
        await db.rollback()
        logger.exception("Failed to create approval request")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail={"code": "INTERNAL_ERROR", "message": "Failed to create approval request"})


@router.get(
    "",
    response_model=SupervisorApprovalListResponse,
    summary="List supervisor approvals with filtering",
)
async def list_approvals_endpoint(
    lead_id: Optional[UUID] = Query(None, description="Filter by lead ID"),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by approval status"),
    action_tier: Optional[str] = Query(None, description="Filter by action tier"),
    limit: int = Query(50, ge=1, le=200, description="Max records to return"),
    offset: int = Query(0, ge=0, description="Records offset"),
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
    db: AsyncSession = Depends(get_db),
):
    """Retrieves paginated list of supervisor human approvals."""
    approvals = await SupervisorApprovalRepository.list_approvals(
        db,
        lead_id=lead_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    if action_tier:
        tier_clean = action_tier.strip().upper()
        approvals = [a for a in approvals if a.action_tier == tier_clean]

    return SupervisorApprovalListResponse(
        total=len(approvals),
        limit=limit,
        offset=offset,
        approvals=[_to_record_out(a) for a in approvals],
    )



@router.get(
    "/{approval_id}",
    response_model=SupervisorApprovalRecordOut,
    summary="Get supervisor approval by ID",
)
async def get_approval_endpoint(
    approval_id: UUID,
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
    db: AsyncSession = Depends(get_db),
):
    """Retrieves a single supervisor approval record by primary key UUID."""
    approval = await SupervisorApprovalRepository.get_by_id(db, approval_id)
    if approval is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "APPROVAL_NOT_FOUND", "message": f"Approval with ID '{approval_id}' not found"},
        )
    return _to_record_out(approval)


@router.post(
    "/{approval_id}/approve",
    response_model=SupervisorApprovalRecordOut,
    summary="Approve a pending supervisor action",
)
async def approve_action_endpoint(
    approval_id: UUID,
    body: SupervisorApprovalApproveRequest,
    approver: AuthenticatedPrincipal = Depends(require_approver_role),
    db: AsyncSession = Depends(get_db),
):
    """
    Transitions a PENDING approval record to APPROVED.
    Requires an authenticated principal with an approver role (e.g. sales_manager, admin).
    """
    service = SupervisorApprovalService()
    try:
        approval = await service.approve_action(
            db,
            approval_id=approval_id,
            approver=approver,
            decision_reason=body.decision_reason,
            notes=body.notes,
            expected_version=body.expected_version,
            expires_in_hours=body.expires_in_hours,
        )
        await db.commit()
        await db.refresh(approval)
        return _to_record_out(approval)
    except ApprovalNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": exc.code, "message": str(exc)})
    except (ApprovalSelfApprovalError, ApprovalUnverifiedIdentityError) as exc:
        await db.rollback()
        logger.warning("Approval prohibited for %s by %s: %s", approval_id, approver.user_id, exc)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": exc.code, "message": str(exc)})
    except (ApprovalInvalidTransitionError, ApprovalVersionConflictError, ApprovalExpiredError) as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)})
    except ApprovalPermissionDeniedError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": exc.code, "message": str(exc)})


@router.post(
    "/{approval_id}/reject",
    response_model=SupervisorApprovalRecordOut,
    summary="Reject a pending supervisor action",
)
async def reject_action_endpoint(
    approval_id: UUID,
    body: SupervisorApprovalRejectRequest,
    approver: AuthenticatedPrincipal = Depends(require_approver_role),
    db: AsyncSession = Depends(get_db),
):
    """
    Transitions a PENDING approval record to REJECTED (terminal state).
    Requires a non-empty decision_reason.
    """
    service = SupervisorApprovalService()
    try:
        approval = await service.reject_action(
            db,
            approval_id=approval_id,
            approver=approver,
            decision_reason=body.decision_reason,
            notes=body.notes,
            expected_version=body.expected_version,
        )
        await db.commit()
        await db.refresh(approval)
        return _to_record_out(approval)
    except ApprovalNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": exc.code, "message": str(exc)})
    except (ApprovalInvalidTransitionError, ApprovalVersionConflictError) as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)})
    except ApprovalPermissionDeniedError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": exc.code, "message": str(exc)})


@router.post(
    "/{approval_id}/revoke",
    response_model=SupervisorApprovalRecordOut,
    summary="Revoke an approved supervisor action",
)
async def revoke_approval_endpoint(
    approval_id: UUID,
    body: SupervisorApprovalRevokeRequest,
    actor: AuthenticatedPrincipal = Depends(require_approver_role),
    db: AsyncSession = Depends(get_db),
):
    """
    Transitions an APPROVED approval record to REVOKED (terminal state).
    Immediately invalidates execution authorization across all workers.
    """
    service = SupervisorApprovalService()
    try:
        approval = await service.revoke_approval(
            db,
            approval_id=approval_id,
            actor=actor,
            revocation_reason=body.revocation_reason,
            notes=body.notes,
            expected_version=body.expected_version,
        )
        await db.commit()
        await db.refresh(approval)
        return _to_record_out(approval)
    except ApprovalNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": exc.code, "message": str(exc)})
    except (ApprovalInvalidTransitionError, ApprovalVersionConflictError) as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)})
    except ApprovalPermissionDeniedError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": exc.code, "message": str(exc)})


@router.post(
    "/{approval_id}/claim",
    response_model=SupervisorApprovalRecordOut,
    summary="Atomically claim an approved action for execution",
)
async def claim_approval_endpoint(
    approval_id: UUID,
    body: SupervisorApprovalClaimRequest,
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
    db: AsyncSession = Depends(get_db),
):
    """
    Atomically claims an approved record using Compare-And-Swap (CAS) update.
    Rejects claimed, expired, or non-approved records. Does NOT dispatch any external action.
    """
    service = SupervisorApprovalService()
    try:
        approval = await service.claim_approval_for_execution(
            db,
            approval_id=approval_id,
            worker_id=body.worker_id,
            expected_version=body.expected_version,
        )
        await db.commit()
        await db.refresh(approval)
        return _to_record_out(approval)
    except ApprovalNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": exc.code, "message": str(exc)})
    except ApprovalContextDriftError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)})
    except (ApprovalClaimConflictError, ApprovalVersionConflictError, ApprovalInvalidTransitionError, ApprovalExpiredError) as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)})


@router.post(
    "/{approval_id}/heartbeat",
    response_model=SupervisorApprovalHeartbeatResponse,
    summary="Report worker heartbeat and extend active execution lease",
)
async def heartbeat_approval_endpoint(
    approval_id: UUID,
    body: SupervisorApprovalHeartbeatRequest,
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
    db: AsyncSession = Depends(get_db),
):
    """
    Records a heartbeat from the active worker to renew the execution lease.
    Rejects expired, revoked, non-owner, or stale-generation requests.
    """
    service = SupervisorApprovalService()
    try:
        approval = await service.heartbeat_lease(
            db,
            approval_id=approval_id,
            worker_id=body.worker_id,
            lease_generation=body.lease_generation,
        )
        await db.commit()
        await db.refresh(approval)
        lease_duration = getattr(settings, "supervisor_lease_duration_seconds", 300)
        lease_expires_at = _ensure_utc(approval.updated_at) + timedelta(seconds=lease_duration)
        return SupervisorApprovalHeartbeatResponse(
            approval_id=approval.id,
            worker_id=body.worker_id,
            lease_generation=approval.version,
            lease_expires_at=lease_expires_at,
            last_heartbeat_at=approval.updated_at,
            status=approval.status,
        )
    except ApprovalNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": exc.code, "message": str(exc)})
    except ApprovalNotLeaseOwnerError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": exc.code, "message": str(exc)})
    except ApprovalFencingConflictError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)})
    except ApprovalLeaseExpiredError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)})
    except ApprovalContextDriftError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)})
    except (ApprovalInvalidTransitionError, ApprovalVersionConflictError) as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)})


@router.post(
    "/{approval_id}/release",
    response_model=SupervisorApprovalRecordOut,
    summary="Release an active worker lease cleanly back to the pool",
)
async def release_approval_endpoint(
    approval_id: UUID,
    body: SupervisorApprovalReleaseRequest,
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
    db: AsyncSession = Depends(get_db),
):
    """
    Voluntarily releases an active worker lease back to the pool before expiration.
    Enforces fencing token and owner identity.
    """
    service = SupervisorApprovalService()
    try:
        approval = await service.release_lease(
            db,
            approval_id=approval_id,
            worker_id=body.worker_id,
            lease_generation=body.lease_generation,
            reason=body.reason,
        )
        await db.commit()
        await db.refresh(approval)
        return _to_record_out(approval)
    except ApprovalNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": exc.code, "message": str(exc)})
    except ApprovalNotLeaseOwnerError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": exc.code, "message": str(exc)})
    except ApprovalFencingConflictError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)})
    except ApprovalInvalidTransitionError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)})


@router.post(
    "/{approval_id}/recover-lease",
    response_model=SupervisorApprovalRecordOut,
    summary="Recover an expired execution lease back to the pool",
)
async def recover_lease_endpoint(
    approval_id: UUID,
    body: Optional[SupervisorApprovalRecoverRequest] = None,
    actor: AuthenticatedPrincipal = Depends(require_approver_role),
    db: AsyncSession = Depends(get_db),
):
    """
    Recovers an expired execution lease under row-level lock.
    Rejects recovery if the lease is still active or status is not APPROVED.
    """
    service = SupervisorApprovalService()
    req = body or SupervisorApprovalRecoverRequest()
    try:
        approval, is_hard_expired = await service.recover_stale_claim(
            db,
            approval_id=approval_id,
            expected_worker_id=req.expected_worker_id,
            expected_generation=req.expected_generation,
            actor_id=actor.user_id,
            actor_role=actor.role,
            reason=req.reason,
        )
        await db.commit()
        await db.refresh(approval)
        return _to_record_out(approval)
    except ApprovalNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": exc.code, "message": str(exc)})
    except ApprovalLeaseActiveError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)})
    except ApprovalNotLeaseOwnerError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": exc.code, "message": str(exc)})
    except ApprovalFencingConflictError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)})
    except ApprovalContextDriftError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)})
    except (ApprovalClaimConflictError, ApprovalInvalidTransitionError) as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": exc.code, "message": str(exc)})


@router.get(
    "/{approval_id}/audit",
    response_model=SupervisorApprovalAuditListResponse,
    summary="Retrieve audit trail for an approval",
)
async def get_approval_audit_trail_endpoint(
    approval_id: UUID,
    actor: AuthenticatedPrincipal = Depends(require_approver_role),
    db: AsyncSession = Depends(get_db),
):
    """Retrieves the immutable append-only audit event trail for an approval."""
    approval = await SupervisorApprovalRepository.get_by_id(db, approval_id)
    if approval is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "APPROVAL_NOT_FOUND", "message": f"Approval with ID '{approval_id}' not found"},
        )

    events = await SupervisorApprovalRepository.list_audit_events_for_approval(db, approval_id)
    return SupervisorApprovalAuditListResponse(
        approval_id=approval_id,
        approval_reference_id=approval.id,
        total=len(events),
        events=[_to_audit_out(e) for e in events],
    )


@router.post(
    "/{approval_id}/verify",
    response_model=SupervisorExecutionAuthorizationResult,
    summary="Verify durable execution authorization against live database context",
)
async def verify_approval_authorization_endpoint(
    approval_id: UUID,
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
    db: AsyncSession = Depends(get_db),
):
    """
    Evaluates whether an approval is currently authorized for execution.
    - Re-evaluates live readiness gates (blocking gates cannot be bypassed by approval).
    - Resolves authoritative contact coordinates and deal parameters from the database.
    - Recomputes the action-context fingerprint and checks for context drift.
    - Fails closed if the target records are missing or context has drifted.
    - Does NOT execute or dispatch any external outreach.
    """
    approval = await SupervisorApprovalRepository.get_by_id(db, approval_id)
    if approval is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "APPROVAL_NOT_FOUND", "message": f"Approval with ID '{approval_id}' not found"},
        )

    # 1. Authoritatively resolve current context from database
    try:
        action, channel, contact_email, contact_phone = await resolve_authoritative_action_context(
            db,
            lead_id=approval.lead_id,
            opportunity_id=approval.opportunity_id,
            contact_id=approval.contact_id,
            requested_action=approval.recommended_action,
        )
    except HTTPException as exc:
        # Target record was deleted or became invalid -> fail closed
        now = now_utc()
        return SupervisorExecutionAuthorizationResult(
            authorized=False,
            decision=SupervisorExecutionDecision.DENIED,
            action_id=approval.action_id,
            reasons=[f"Execution authorization denied: Target context cannot be reconstructed: {exc.detail}"],
            readiness_status=SupervisorActionReadinessStatus.MISSING_TARGET_DATA,
            action_tier=SupervisorActionTier.EXTERNAL_OUTREACH,
            approval_required=True,
            approval_details=_to_approval_schema(approval),
            denial_code="MISSING_TARGET_DATA",
            evaluated_at=now,
        )

    # 2. Check context fingerprint drift
    current_fp = compute_action_context_fingerprint(
        action_id=action.action_id,
        action_type=action.recommended_action.value,
        lead_id=action.lead_id,
        opportunity_id=action.opportunity_id,
        contact_id=action.contact_id,
        channel=channel or approval.channel,
        contact_email=contact_email,
        contact_phone=contact_phone,
        opportunity_score=action.opportunity_score,
        recommendation_fit=action.recommendation_fit,
        evidence_keys=action.evidence_keys,
    )

    if approval.context_fingerprint != current_fp:
        now = now_utc()
        return SupervisorExecutionAuthorizationResult(
            authorized=False,
            decision=SupervisorExecutionDecision.DENIED,
            action_id=action.action_id,
            reasons=[f"Execution authorization denied: Action context drifted since approval: stored '{approval.context_fingerprint}' != current '{current_fp}'"],
            readiness_status=action.readiness_status,
            action_tier=SupervisorActionTier.EXTERNAL_OUTREACH,
            approval_required=True,
            approval_details=_to_approval_schema(approval),
            denial_code="CONTEXT_DRIFT",
            evaluated_at=now,
        )

    # 3. Evaluate durable execution authorization via service
    service = SupervisorApprovalService()
    return await service.authorize_action_execution(
        db,
        action=action,
        channel=channel,
        contact_email=contact_email,
        contact_phone=contact_phone,
    )

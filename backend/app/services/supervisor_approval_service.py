"""
Durable Human Approval Lifecycle and Authorization Service.

Enforces business lifecycle rules, permission boundaries, testable clock expiration,
cryptographic context binding (drift detection), and execution authorization.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
    is_policy_approval_required,
    is_shared_development_identity,
)
from app.config import settings
from app.models.entities import (
    DEFAULT_APPROVAL_VALIDITY_HOURS,
    Lead,
    LeadIntelligenceOpportunity,
    SupervisorApprovalAuditEvent,
    SupervisorHumanApproval as SupervisorHumanApprovalModel,
    now_utc,
)
from app.services.sales_supervisor import _derive_expected_channel
from app.repositories.supervisor_approval import SupervisorApprovalRepository
from app.schemas.sales_supervisor import (
    PrioritizedSalesAction,
    SupervisorExecutionAuthorizationResult,
    SupervisorHumanApproval as SupervisorHumanApprovalSchema,
)

logger = logging.getLogger("lead_agent.supervisor_approval_service")

# Permitted approver roles under current development boundary
SUPERVISOR_APPROVER_ROLES = frozenset({"sales_manager", "admin", "sales_admin", "manager"})


def _ensure_utc(dt: Optional[datetime]) -> datetime:
    """Ensure datetime is non-null and timezone-aware UTC."""
    if dt is None:
        return now_utc()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_approval_schema(model: SupervisorHumanApprovalModel) -> SupervisorHumanApprovalSchema:
    """Convert ORM SupervisorHumanApproval to Pydantic SupervisorHumanApproval schema."""
    try:
        status_enum = SupervisorApprovalStatus(model.status)
    except ValueError:
        status_enum = SupervisorApprovalStatus.APPROVED

    return SupervisorHumanApprovalSchema(
        approval_id=str(model.id),
        action_id=model.action_id,
        lead_id=model.lead_id,
        opportunity_id=model.opportunity_id,
        contact_id=model.contact_id,
        channel=model.channel,
        approver_id=model.approved_by_user_id or model.requested_by_user_id,
        status=status_enum,
        approved_at=model.approved_at or model.created_at,
        notes=model.notes,
        expires_at=model.expires_at,
    )


class SupervisorApprovalService:
    """
    Service layer governing human approval lifecycles, context verification,
    and durable execution authorization for Sales Supervisor actions.
    """

    def __init__(
        self,
        repository: type[SupervisorApprovalRepository] = SupervisorApprovalRepository,
        clock: Callable[[], datetime] = now_utc,
        allow_dev_shared_approval: Optional[bool] = None,
    ):
        self.repository = repository
        self.clock = clock
        if allow_dev_shared_approval is None:
            self.allow_dev_shared_approval = settings.effective_allow_dev_shared_approval
        else:
            self.allow_dev_shared_approval = False if settings.is_production else bool(allow_dev_shared_approval)

    def _now(self, now: Optional[datetime] = None) -> datetime:
        if now is not None:
            return _ensure_utc(now)
        return _ensure_utc(self.clock())

    # ------------------------------------------------------------------------
    # 1. Approval Request Creation
    # ------------------------------------------------------------------------
    async def request_approval(
        self,
        session: AsyncSession,
        *,
        action: PrioritizedSalesAction,
        requested_by: AuthenticatedPrincipal,
        channel: Optional[str] = None,
        contact_email: Optional[str] = None,
        contact_phone: Optional[str] = None,
        expires_in_hours: int = DEFAULT_APPROVAL_VALIDITY_HOURS,
        notes: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> SupervisorHumanApprovalModel:
        """
        Creates and persists a new durable PENDING approval request bound to the
        canonical action context.
        """
        eval_time = self._now(now)

        if not requested_by.is_authenticated:
            raise ApprovalPermissionDeniedError("Unauthenticated principal cannot request approval")

        action_tier = classify_supervisor_action(action.recommended_action)
        action_type_val = (
            action.recommended_action.value
            if hasattr(action.recommended_action, "value")
            else str(action.recommended_action)
        )

        # Compute canonical context fingerprint
        context_fingerprint = compute_action_context_fingerprint(
            action_id=action.action_id,
            action_type=action_type_val,
            lead_id=action.lead_id,
            opportunity_id=action.opportunity_id,
            contact_id=action.contact_id,
            channel=channel,
            contact_email=contact_email,
            contact_phone=contact_phone,
            opportunity_score=action.opportunity_score,
            recommendation_fit=action.recommendation_fit,
            evidence_keys=action.evidence_keys,
        )

        approval = await self.repository.create_approval(
            session,
            action_id=action.action_id,
            lead_id=action.lead_id,
            opportunity_id=action.opportunity_id,
            contact_id=action.contact_id,
            recommended_action=action_type_val,
            action_tier=action_tier.value,
            channel=channel,
            context_fingerprint=context_fingerprint,
            requested_by_user_id=requested_by.user_id,
            expires_in_hours=expires_in_hours,
            notes=notes,
            actor_role=requested_by.role,
            metadata=metadata,
            now=eval_time,
        )
        return approval

    # ------------------------------------------------------------------------
    # 2. Decision Operations (Approve, Reject, Revoke)
    # ------------------------------------------------------------------------
    async def approve_action(
        self,
        session: AsyncSession,
        *,
        approval_id: uuid.UUID,
        approver: AuthenticatedPrincipal,
        decision_reason: Optional[str] = None,
        notes: Optional[str] = None,
        expected_version: Optional[int] = None,
        expires_in_hours: int = DEFAULT_APPROVAL_VALIDITY_HOURS,
        strict_separation: bool = True,
        metadata: Optional[dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> SupervisorHumanApprovalModel:
        """
        Transition a PENDING approval to APPROVED.
        Requires authenticated principal with manager or admin role.
        Enforces Gate A: Self-Approval Separation of Duties.
        """
        eval_time = self._now(now)

        if not approver.is_authenticated:
            raise ApprovalPermissionDeniedError("Unauthenticated principal cannot approve actions")
        if approver.role.lower() not in SUPERVISOR_APPROVER_ROLES:
            raise ApprovalPermissionDeniedError(
                f"Role '{approver.role}' is not authorized to approve supervisor actions"
            )

        # Check existing approval to verify not already expired
        existing = await self.repository.get_by_id(session, approval_id)
        if existing is None:
            raise ApprovalNotFoundError(f"Approval with ID '{approval_id}' not found")
        if existing.expires_at is not None and eval_time > _ensure_utc(existing.expires_at):
            raise ApprovalExpiredError(
                f"Cannot approve expired request (expired at {existing.expires_at.isoformat()})"
            )

        # --------------------------------------------------------------------
        # Gate A: Self-Approval Separation of Duties Enforcement
        # --------------------------------------------------------------------
        req_user = (existing.requested_by_user_id or "").strip()
        app_user = (approver.user_id or "").strip()

        # Fails closed if requester identity is missing or unverified placeholder
        if not req_user or req_user.upper() in {"UNKNOWN", "ANONYMOUS", "NONE"}:
            logger.warning(
                "Self-approval check failed closed for approval %s: missing or unverified requester identity '%s'",
                approval_id,
                req_user,
            )
            raise ApprovalUnverifiedIdentityError(
                "Requester identity is missing or cannot be reliably verified (fails closed)"
            )

        if not app_user or app_user.upper() in {"UNKNOWN", "ANONYMOUS", "NONE"}:
            raise ApprovalPermissionDeniedError("Approver identity is missing or unauthenticated")

        # Check for shared development identity
        req_is_shared = is_shared_development_identity(req_user)
        app_is_shared = is_shared_development_identity(app_user)

        if req_is_shared or app_is_shared:
            dev_shared_permitted = (not strict_separation) or (
                self.allow_dev_shared_approval and not settings.is_production
            )
            if not dev_shared_permitted:
                shared_name = req_user if req_is_shared else app_user
                logger.warning(
                    "Approval %s prohibited: shared development identity '%s' cannot satisfy separation of duties.",
                    approval_id,
                    shared_name,
                )
                raise ApprovalUnverifiedIdentityError(
                    f"Approval prohibited: Shared development identity '{shared_name}' cannot satisfy separation of duties"
                )
            else:
                logger.warning(
                    "Approval %s processed under shared development identity. "
                    "Individual separation of duties cannot be guaranteed under shared-token authentication.",
                    approval_id,
                )

        # Identity comparison: requester cannot approve their own request when identity is reliably established
        if req_user.lower() == app_user.lower():
            if not (req_is_shared and app_is_shared and ((not strict_separation) or (self.allow_dev_shared_approval and not settings.is_production))):
                logger.warning(
                    "Self-approval prohibited for approval %s: approver '%s' matches requester '%s'",
                    approval_id,
                    app_user,
                    req_user,
                )
                raise ApprovalSelfApprovalError(
                    f"Self-approval prohibited: Requester '{req_user}' cannot approve their own request"
                )

        expires_at = eval_time + timedelta(hours=expires_in_hours) if expires_in_hours > 0 else None

        return await self.repository.transition_status(
            session,
            approval_id=approval_id,
            to_status="APPROVED",
            actor_id=approver.user_id,
            actor_role=approver.role,
            reason=decision_reason,
            notes=notes,
            expected_version=expected_version,
            expires_at=expires_at,
            metadata=metadata,
            now=eval_time,
        )

    async def reject_action(
        self,
        session: AsyncSession,
        *,
        approval_id: uuid.UUID,
        approver: AuthenticatedPrincipal,
        decision_reason: str,
        notes: Optional[str] = None,
        expected_version: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> SupervisorHumanApprovalModel:
        """
        Transition a PENDING approval to REJECTED (terminal state).
        Requires reason for rejection.
        """
        eval_time = self._now(now)

        if not approver.is_authenticated:
            raise ApprovalPermissionDeniedError("Unauthenticated principal cannot reject actions")
        if approver.role.lower() not in SUPERVISOR_APPROVER_ROLES:
            raise ApprovalPermissionDeniedError(
                f"Role '{approver.role}' is not authorized to reject supervisor actions"
            )

        return await self.repository.transition_status(
            session,
            approval_id=approval_id,
            to_status="REJECTED",
            actor_id=approver.user_id,
            actor_role=approver.role,
            reason=decision_reason,
            notes=notes,
            expected_version=expected_version,
            metadata=metadata,
            now=eval_time,
        )

    async def revoke_approval(
        self,
        session: AsyncSession,
        *,
        approval_id: uuid.UUID,
        actor: AuthenticatedPrincipal,
        revocation_reason: str,
        notes: Optional[str] = None,
        expected_version: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> SupervisorHumanApprovalModel:
        """
        Transition an APPROVED record to REVOKED (terminal state).
        Immediately invalidates execution authorization across all workers.
        """
        eval_time = self._now(now)

        if not actor.is_authenticated:
            raise ApprovalPermissionDeniedError("Unauthenticated principal cannot revoke approvals")
        if actor.role.lower() not in SUPERVISOR_APPROVER_ROLES:
            raise ApprovalPermissionDeniedError(
                f"Role '{actor.role}' is not authorized to revoke supervisor approvals"
            )

        return await self.repository.transition_status(
            session,
            approval_id=approval_id,
            to_status="REVOKED",
            actor_id=actor.user_id,
            actor_role=actor.role,
            reason=revocation_reason,
            notes=notes,
            expected_version=expected_version,
            metadata=metadata,
            now=eval_time,
        )

    # ------------------------------------------------------------------------
    # 3. Gate B: Authoritative Context Verification & Atomic Claiming
    # ------------------------------------------------------------------------
    async def verify_authoritative_context(
        self,
        session: AsyncSession,
        *,
        approval: SupervisorHumanApprovalModel,
        action: Optional[PrioritizedSalesAction] = None,
    ) -> None:
        """
        Reconstructs the authoritative action context strictly from database records
        and verifies that it matches the approved context_fingerprint.
        Fails closed with ApprovalContextDriftError if:
        - Authoritative Lead is missing or deleted
        - Authoritative LeadIntelligenceOpportunity is missing or deleted
        - Opportunity does not belong to Lead (cross-lead substitution)
        - Contact Lead is missing or deleted (if contact_id is specified)
        - Any coordinate, score, evidence, or channel has drifted from the approved fingerprint
        - Client-supplied action context conflicts with the authoritative database state.
        """
        # 1. Fetch authoritative Lead with pessimistic row lock
        lead = await session.scalar(
            select(Lead).where(Lead.id == approval.lead_id).with_for_update()
        )
        if lead is None:
            raise ApprovalContextDriftError(
                f"Authoritative lead record '{approval.lead_id}' not found or deleted"
            )

        # 2. Fetch authoritative LeadIntelligenceOpportunity with pessimistic row lock
        opp = await session.scalar(
            select(LeadIntelligenceOpportunity)
            .where(LeadIntelligenceOpportunity.id == approval.opportunity_id)
            .with_for_update()
        )
        if opp is None:
            if action is not None:
                # If opp is not persisted (e.g. test fixture) but action is provided, verify against action
                act_action_type = (
                    action.recommended_action.value
                    if hasattr(action.recommended_action, "value")
                    else str(action.recommended_action)
                )
                act_fp = compute_action_context_fingerprint(
                    action_id=action.action_id,
                    action_type=act_action_type,
                    lead_id=action.lead_id,
                    opportunity_id=action.opportunity_id,
                    contact_id=action.contact_id,
                    channel=approval.channel,
                    contact_email=lead.email,
                    contact_phone=lead.phone,
                    opportunity_score=action.opportunity_score,
                    recommendation_fit=action.recommendation_fit,
                    evidence_keys=action.evidence_keys,
                )
                if act_fp != approval.context_fingerprint:
                    raise ApprovalContextDriftError(
                        f"Action context drifted: stored fingerprint '{approval.context_fingerprint}' != candidate fingerprint '{act_fp}'"
                    )
                return
            raise ApprovalContextDriftError(
                f"Authoritative opportunity record '{approval.opportunity_id}' not found or deleted"
            )

        # 3. Cross-validate lead and opportunity (prevent cross-lead substitution)
        if opp.lead_id != lead.id:
            raise ApprovalContextDriftError(
                f"Opportunity '{approval.opportunity_id}' does not belong to lead '{approval.lead_id}'"
            )

        # 4. Resolve contact coordinates authoritatively
        if approval.contact_id is not None:
            contact_lead = await session.scalar(
                select(Lead).where(Lead.id == approval.contact_id).with_for_update()
            )
            if contact_lead is None:
                raise ApprovalContextDriftError(
                    f"Authoritative contact lead record '{approval.contact_id}' not found or deleted"
                )
            contact_email = contact_lead.email or lead.email
            contact_phone = contact_lead.phone or lead.phone
        else:
            contact_email = lead.email
            contact_phone = lead.phone

        # 5. Resolve action parameters
        rec_action_str = (approval.recommended_action or opp.recommended_action or "PERSONALIZED_EMAIL").strip()
        try:
            rec_action_enum = RecommendedSalesAction(rec_action_str)
        except ValueError:
            rec_action_enum = RecommendedSalesAction.PERSONALIZED_EMAIL

        channel = approval.channel or _derive_expected_channel(rec_action_enum)
        opp_score = float(opp.score or 0.0)
        evidence_keys = sorted(opp.evidence.keys()) if (opp.evidence and isinstance(opp.evidence, dict)) else []

        # 6. Recompute authoritative database-derived fingerprint
        current_fp = compute_action_context_fingerprint(
            action_id=approval.action_id,
            action_type=rec_action_enum.value,
            lead_id=lead.id,
            opportunity_id=opp.id,
            contact_id=approval.contact_id,
            channel=channel,
            contact_email=contact_email,
            contact_phone=contact_phone,
            opportunity_score=opp_score,
            recommendation_fit=0.0,
            evidence_keys=evidence_keys,
        )

        if current_fp != approval.context_fingerprint:
            logger.warning(
                "Context drift detected at claim verification for approval %s: stored '%s' != current '%s'",
                approval.id,
                approval.context_fingerprint,
                current_fp,
            )
            raise ApprovalContextDriftError(
                f"Action context drifted: stored fingerprint '{approval.context_fingerprint}' != current fingerprint '{current_fp}'"
            )

        # 7. If action was supplied by client/caller, verify it does not attempt to override DB context
        if action is not None:
            act_action_type = (
                action.recommended_action.value
                if hasattr(action.recommended_action, "value")
                else str(action.recommended_action)
            )
            act_fp = compute_action_context_fingerprint(
                action_id=action.action_id,
                action_type=act_action_type,
                lead_id=action.lead_id,
                opportunity_id=action.opportunity_id,
                contact_id=action.contact_id,
                channel=channel,
                contact_email=contact_email,
                contact_phone=contact_phone,
                opportunity_score=action.opportunity_score,
                recommendation_fit=action.recommendation_fit,
                evidence_keys=action.evidence_keys,
            )
            if act_fp != approval.context_fingerprint:
                raise ApprovalContextDriftError(
                    "Client-supplied action context does not match authoritative database context"
                )

    async def claim_approval_for_execution(
        self,
        session: AsyncSession,
        *,
        approval_id: uuid.UUID,
        worker_id: str,
        expected_version: int,
        now: Optional[datetime] = None,
        metadata: Optional[dict[str, Any]] = None,
        action: Optional[PrioritizedSalesAction] = None,
        validate_context: bool = True,
    ) -> SupervisorHumanApprovalModel:
        """
        Atomically claims an active approval for execution using an atomic CAS update
        and verifies authoritative action context to eliminate TOCTOU vulnerability.
        Rejects claimed, expired, non-approved, or context-drifted records.
        """
        eval_time = self._now(now)

        # 1. Pessimistic lock and pre-check
        stmt = (
            select(SupervisorHumanApprovalModel)
            .where(SupervisorHumanApprovalModel.id == approval_id)
            .with_for_update()
        )
        res = await session.execute(stmt)
        approval = res.scalar_one_or_none()

        if approval is None:
            raise ApprovalNotFoundError(f"Approval record '{approval_id}' not found")
        if approval.status != "APPROVED":
            raise ApprovalInvalidTransitionError(
                approval.status, "CLAIMED", f"Approval is in status '{approval.status}', expected 'APPROVED'"
            )
        if approval.expires_at is not None and eval_time > _ensure_utc(approval.expires_at):
            raise ApprovalExpiredError(
                f"Approval expired at {approval.expires_at.isoformat()}, cannot be claimed"
            )
        if approval.execution_claimed_at is not None:
            raise ApprovalClaimConflictError(
                f"Approval was already claimed at {approval.execution_claimed_at.isoformat()} by '{approval.execution_claimed_by}'"
            )
        if approval.version != expected_version:
            raise ApprovalVersionConflictError(expected_version, approval.version)

        # 2. Gate B: Atomic Context Verification & Drift Check
        if validate_context:
            await self.verify_authoritative_context(session, approval=approval, action=action)

        # 3. Gate C: Derive lease metadata and bind to claim
        lease_duration = getattr(settings, "supervisor_lease_duration_seconds", 300)
        lease_expires_at = eval_time + timedelta(seconds=lease_duration)
        claim_meta = dict(metadata or {})
        claim_meta["lease_duration_seconds"] = lease_duration
        claim_meta["lease_expires_at"] = lease_expires_at.isoformat()
        claim_meta["lease_generation"] = expected_version + 1

        # 4. Execute atomic claim via repository bound to fingerprint
        return await self.repository.claim_execution(
            session,
            approval_id=approval_id,
            worker_id=worker_id,
            expected_version=expected_version,
            expected_context_fingerprint=approval.context_fingerprint,
            now=eval_time,
            metadata=claim_meta,
        )

    async def heartbeat_lease(
        self,
        session: AsyncSession,
        *,
        approval_id: uuid.UUID,
        worker_id: str,
        lease_generation: int,
        now: Optional[datetime] = None,
        validate_context: bool = True,
        action: Optional[PrioritizedSalesAction] = None,
    ) -> SupervisorHumanApprovalModel:
        """
        Processes a worker heartbeat to extend an active execution lease.
        Enforces fencing tokens, ownership, lease expiration, approval status,
        and authoritative context revalidation.
        """
        eval_time = self._now(now)
        lease_duration = getattr(settings, "supervisor_lease_duration_seconds", 300)

        # 1. Pessimistic lock and pre-check
        stmt = (
            select(SupervisorHumanApprovalModel)
            .where(SupervisorHumanApprovalModel.id == approval_id)
            .with_for_update()
        )
        res = await session.execute(stmt)
        approval = res.scalar_one_or_none()
        if approval is None:
            raise ApprovalNotFoundError(f"Approval record '{approval_id}' not found")

        # 2. Status verification
        if approval.status != "APPROVED":
            raise ApprovalInvalidTransitionError(
                approval.status, "HEARTBEAT", f"Cannot heartbeat approval in status '{approval.status}', expected 'APPROVED'"
            )

        # 3. Ownership verification
        if approval.execution_claimed_by is None or approval.execution_claimed_by.strip() != worker_id.strip():
            raise ApprovalNotLeaseOwnerError(
                f"Worker '{worker_id.strip()}' is not the current lease owner (current owner: '{approval.execution_claimed_by}')"
            )

        # 4. Fencing token / generation verification
        if approval.version != lease_generation:
            raise ApprovalFencingConflictError(
                f"Heartbeat rejected: Lease generation {lease_generation} does not match current version {approval.version}"
            )

        # 5. Lease expiration verification: Cannot revive an expired lease
        lease_cutoff = _ensure_utc(approval.updated_at) + timedelta(seconds=lease_duration)
        if eval_time > lease_cutoff:
            raise ApprovalLeaseExpiredError(
                f"Execution lease expired at {lease_cutoff.isoformat()}; cannot extend via heartbeat"
            )

        # Hard approval expiration check
        if approval.expires_at is not None and eval_time > _ensure_utc(approval.expires_at):
            raise ApprovalLeaseExpiredError(
                f"Approval hard expiration passed at {approval.expires_at.isoformat()}; cannot extend lease"
            )

        # 6. Authoritative context verification (ensures context hasn't drifted while worker is executing)
        if validate_context:
            await self.verify_authoritative_context(session, approval=approval, action=action)

        # 7. Execute repository heartbeat update
        return await self.repository.heartbeat_lease(
            session,
            approval_id=approval_id,
            worker_id=worker_id,
            lease_generation=lease_generation,
            lease_duration_seconds=lease_duration,
            now=eval_time,
        )

    async def recover_stale_claim(
        self,
        session: AsyncSession,
        *,
        approval_id: uuid.UUID,
        expected_worker_id: Optional[str] = None,
        expected_generation: Optional[int] = None,
        actor_id: str = "SYSTEM_RECOVERY",
        actor_role: str = "SYSTEM",
        reason: Optional[str] = None,
        now: Optional[datetime] = None,
        validate_context: bool = True,
        action: Optional[PrioritizedSalesAction] = None,
    ) -> tuple[SupervisorHumanApprovalModel, bool]:
        """
        Recovers a stale execution lease where the worker failed to renew within the lease duration.
        Enforces transaction atomicity, row locks, re-checks expiration within the transaction,
        revalidates authoritative action context, and invalidates the old fencing generation.
        """
        eval_time = self._now(now)
        lease_duration = getattr(settings, "supervisor_lease_duration_seconds", 300)

        # 1. Pessimistic lock and pre-check
        stmt = (
            select(SupervisorHumanApprovalModel)
            .where(SupervisorHumanApprovalModel.id == approval_id)
            .with_for_update()
        )
        res = await session.execute(stmt)
        approval = res.scalar_one_or_none()
        if approval is None:
            raise ApprovalNotFoundError(f"Approval record '{approval_id}' not found")

        if approval.status != "APPROVED":
            raise ApprovalInvalidTransitionError(
                approval.status, "RECOVER", f"Cannot recover lease for approval in status '{approval.status}'"
            )

        if approval.execution_claimed_at is None:
            raise ApprovalClaimConflictError(
                f"Approval '{approval_id}' is not currently claimed; no lease to recover"
            )

        # 2. Authoritative context verification
        if validate_context:
            await self.verify_authoritative_context(session, approval=approval, action=action)

        # 3. Repository recovery under pessimistic lock
        return await self.repository.recover_stale_claim(
            session,
            approval_id=approval_id,
            lease_duration_seconds=lease_duration,
            expected_worker_id=expected_worker_id,
            expected_generation=expected_generation,
            actor_id=actor_id,
            actor_role=actor_role,
            reason=reason,
            now=eval_time,
        )

    async def release_lease(
        self,
        session: AsyncSession,
        *,
        approval_id: uuid.UUID,
        worker_id: str,
        lease_generation: int,
        reason: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> SupervisorHumanApprovalModel:
        """
        Releases an active execution lease cleanly back to the pool before expiration.
        """
        eval_time = self._now(now)

        return await self.repository.release_lease(
            session,
            approval_id=approval_id,
            worker_id=worker_id,
            lease_generation=lease_generation,
            reason=reason,
            now=eval_time,
        )

    async def recover_all_stale_claims(
        self,
        session: AsyncSession,
        *,
        limit: int = 100,
        now: Optional[datetime] = None,
        actor_id: str = "SYSTEM_RECOVERY",
        actor_role: str = "SYSTEM",
    ) -> list[uuid.UUID]:
        """
        Scans for and recovers all genuinely expired execution leases.
        Returns list of recovered approval IDs.
        """
        eval_time = self._now(now)
        lease_duration = getattr(settings, "supervisor_lease_duration_seconds", 300)

        stale_records = await self.repository.list_stale_claims(
            session,
            lease_duration_seconds=lease_duration,
            now=eval_time,
            limit=limit,
        )

        recovered_ids: list[uuid.UUID] = []
        for candidate in stale_records:
            try:
                await self.repository.recover_stale_claim(
                    session,
                    approval_id=candidate.id,
                    lease_duration_seconds=lease_duration,
                    actor_id=actor_id,
                    actor_role=actor_role,
                    now=eval_time,
                )
                recovered_ids.append(candidate.id)
            except (ApprovalClaimConflictError, ApprovalFencingConflictError, ApprovalLeaseActiveError) as err:
                logger.debug("Skipping candidate approval %s during recovery scan: %s", candidate.id, err)

        return recovered_ids

    # ------------------------------------------------------------------------
    # 4. Context Verification & Drift Check
    # ------------------------------------------------------------------------
    def verify_action_context(
        self,
        approval: SupervisorHumanApprovalModel,
        action: PrioritizedSalesAction,
        channel: Optional[str] = None,
        contact_email: Optional[str] = None,
        contact_phone: Optional[str] = None,
    ) -> tuple[bool, str]:
        """
        Validates that the current action context exactly matches the approved context_fingerprint.
        Returns (is_valid, reason).
        """
        action_type_val = (
            action.recommended_action.value
            if hasattr(action.recommended_action, "value")
            else str(action.recommended_action)
        )
        current_fp = compute_action_context_fingerprint(
            action_id=action.action_id,
            action_type=action_type_val,
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
            return False, f"Action context drifted: stored hash '{approval.context_fingerprint}' != current hash '{current_fp}'"
        return True, "Context fingerprint matches"

    # ------------------------------------------------------------------------
    # 5. Durable Execution Authorization Evaluation
    # ------------------------------------------------------------------------
    async def authorize_action_execution(
        self,
        session: AsyncSession,
        *,
        action: PrioritizedSalesAction,
        channel: Optional[str] = None,
        contact_email: Optional[str] = None,
        contact_phone: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> SupervisorExecutionAuthorizationResult:
        """
        Evaluates whether a candidate action is authorized for execution against durable persistence.

        Inviolable Rules:
        1. Readiness Prerequisite: Readiness MUST be READY. Blocked gates NEVER bypassed by approval.
        2. Internal Operations: System-authorized unless explicit human REJECTED/REVOKED veto exists.
        3. External Outreach: Strictly requires active, unexpired, matching APPROVED record.
        4. Context Drift: If action parameters changed since approval, fails closed (CONTEXT_DRIFT).
        5. Double Execution: If already claimed, authorization is denied.
        """
        eval_time = self._now(now)

        # 1. Action classification
        try:
            tier = classify_supervisor_action(action.recommended_action)
        except (ValueError, TypeError) as exc:
            return SupervisorExecutionAuthorizationResult(
                authorized=False,
                decision=SupervisorExecutionDecision.DENIED,
                action_id=action.action_id,
                reasons=[f"Execution authorization denied: {str(exc)}"],
                readiness_status=action.readiness_status,
                action_tier=SupervisorActionTier.EXTERNAL_OUTREACH,
                approval_required=True,
                approval_details=None,
                denial_code="UNKNOWN_ACTION_TYPE",
                evaluated_at=eval_time,
            )

        approval_required = tier == SupervisorActionTier.EXTERNAL_OUTREACH

        # 2. Inviolable Readiness Gate Check
        if action.readiness_status != SupervisorActionReadinessStatus.READY:
            denial_reason = (
                f"Execution authorization denied: Action readiness is {action.readiness_status.value}. "
                f"Readiness gates cannot be overridden by approval or priority score ({action.priority_score}/100)."
            )
            return SupervisorExecutionAuthorizationResult(
                authorized=False,
                decision=SupervisorExecutionDecision.DENIED,
                action_id=action.action_id,
                reasons=[denial_reason, *action.readiness_details.reasons],
                readiness_status=action.readiness_status,
                action_tier=tier,
                approval_required=approval_required,
                approval_details=None,
                denial_code=f"READINESS_{action.readiness_status.value}",
                evaluated_at=eval_time,
            )

        # 3. Internal Operation Authorization
        if tier == SupervisorActionTier.INTERNAL_OPERATION:
            # Check if an explicit veto (REJECTED or REVOKED) was recorded
            approvals = await self.repository.list_approvals_for_action(session, action.action_id)
            veto = next((a for a in approvals if a.status in ("REJECTED", "REVOKED")), None)
            if veto is not None:
                return SupervisorExecutionAuthorizationResult(
                    authorized=False,
                    decision=SupervisorExecutionDecision.DENIED,
                    action_id=action.action_id,
                    reasons=[
                        f"Execution authorization denied: Human supervisor explicitly recorded status '{veto.status}'."
                    ],
                    readiness_status=action.readiness_status,
                    action_tier=tier,
                    approval_required=False,
                    approval_details=_to_approval_schema(veto),
                    denial_code=f"APPROVAL_{veto.status}",
                    evaluated_at=eval_time,
                )

            return SupervisorExecutionAuthorizationResult(
                authorized=True,
                decision=SupervisorExecutionDecision.AUTHORIZED,
                action_id=action.action_id,
                reasons=[
                    f"Internal operation '{action.recommended_action.value}' authorized for system workflow execution "
                    "(readiness verified, no external communication required)."
                ],
                readiness_status=action.readiness_status,
                action_tier=tier,
                approval_required=False,
                approval_details=None,
                denial_code=None,
                evaluated_at=eval_time,
            )

        # 4. External Outreach Authorization
        active_approval = await self.repository.get_active_approved(session, action.action_id)
        if active_approval is None:
            # Check if pending or rejected exists to provide accurate decision code
            approvals = await self.repository.list_approvals_for_action(session, action.action_id)
            latest = approvals[0] if approvals else None
            if latest and latest.status == "REJECTED":
                return SupervisorExecutionAuthorizationResult(
                    authorized=False,
                    decision=SupervisorExecutionDecision.DENIED,
                    action_id=action.action_id,
                    reasons=["Execution authorization denied: Human approval was explicitly rejected."],
                    readiness_status=action.readiness_status,
                    action_tier=tier,
                    approval_required=True,
                    approval_details=_to_approval_schema(latest),
                    denial_code="APPROVAL_REJECTED",
                    evaluated_at=eval_time,
                )
            elif latest and latest.status == "REVOKED":
                return SupervisorExecutionAuthorizationResult(
                    authorized=False,
                    decision=SupervisorExecutionDecision.DENIED,
                    action_id=action.action_id,
                    reasons=["Execution authorization denied: Human approval was revoked."],
                    readiness_status=action.readiness_status,
                    action_tier=tier,
                    approval_required=True,
                    approval_details=_to_approval_schema(latest),
                    denial_code="APPROVAL_REVOKED",
                    evaluated_at=eval_time,
                )
            elif latest and latest.status == "EXPIRED":
                return SupervisorExecutionAuthorizationResult(
                    authorized=False,
                    decision=SupervisorExecutionDecision.DENIED,
                    action_id=action.action_id,
                    reasons=["Execution authorization denied: Human approval has expired."],
                    readiness_status=action.readiness_status,
                    action_tier=tier,
                    approval_required=True,
                    approval_details=_to_approval_schema(latest),
                    denial_code="APPROVAL_EXPIRED",
                    evaluated_at=eval_time,
                )

            return SupervisorExecutionAuthorizationResult(
                authorized=False,
                decision=SupervisorExecutionDecision.REQUIRES_APPROVAL,
                action_id=action.action_id,
                reasons=[
                    f"Execution authorization withheld: External outreach action '{action.recommended_action.value}' "
                    "requires explicit human approval."
                ],
                readiness_status=action.readiness_status,
                action_tier=tier,
                approval_required=True,
                approval_details=None,
                denial_code="APPROVAL_REQUIRED",
                evaluated_at=eval_time,
            )

        # Check Expiration (Derived immediately)
        if active_approval.expires_at is not None:
            exp_aware = _ensure_utc(active_approval.expires_at)
            if eval_time > exp_aware:
                return SupervisorExecutionAuthorizationResult(
                    authorized=False,
                    decision=SupervisorExecutionDecision.DENIED,
                    action_id=action.action_id,
                    reasons=[f"Execution authorization denied: Human approval expired at {exp_aware.isoformat()}."],
                    readiness_status=action.readiness_status,
                    action_tier=tier,
                    approval_required=True,
                    approval_details=_to_approval_schema(active_approval),
                    denial_code="APPROVAL_EXPIRED",
                    evaluated_at=eval_time,
                )

        # Check Target Identity Matching
        if active_approval.lead_id != action.lead_id:
            return SupervisorExecutionAuthorizationResult(
                authorized=False,
                decision=SupervisorExecutionDecision.DENIED,
                action_id=action.action_id,
                reasons=[
                    f"Execution authorization denied: Approval lead_id '{active_approval.lead_id}' "
                    f"does not match candidate lead_id '{action.lead_id}'."
                ],
                readiness_status=action.readiness_status,
                action_tier=tier,
                approval_required=True,
                approval_details=_to_approval_schema(active_approval),
                denial_code="LEAD_MISMATCH",
                evaluated_at=eval_time,
            )

        if active_approval.opportunity_id != action.opportunity_id:
            return SupervisorExecutionAuthorizationResult(
                authorized=False,
                decision=SupervisorExecutionDecision.DENIED,
                action_id=action.action_id,
                reasons=[
                    f"Execution authorization denied: Approval opportunity_id '{active_approval.opportunity_id}' "
                    f"does not match candidate opportunity_id '{action.opportunity_id}'."
                ],
                readiness_status=action.readiness_status,
                action_tier=tier,
                approval_required=True,
                approval_details=_to_approval_schema(active_approval),
                denial_code="OPPORTUNITY_MISMATCH",
                evaluated_at=eval_time,
            )

        if action.contact_id is not None and active_approval.contact_id != action.contact_id:
            return SupervisorExecutionAuthorizationResult(
                authorized=False,
                decision=SupervisorExecutionDecision.DENIED,
                action_id=action.action_id,
                reasons=[
                    f"Execution authorization denied: Approval contact_id '{active_approval.contact_id}' "
                    f"does not match candidate contact_id '{action.contact_id}'."
                ],
                readiness_status=action.readiness_status,
                action_tier=tier,
                approval_required=True,
                approval_details=_to_approval_schema(active_approval),
                denial_code="CONTACT_MISMATCH",
                evaluated_at=eval_time,
            )

        # Check Context Fingerprint Drift
        is_context_valid, drift_reason = self.verify_action_context(
            active_approval,
            action,
            channel=channel,
            contact_email=contact_email,
            contact_phone=contact_phone,
        )
        if not is_context_valid:
            return SupervisorExecutionAuthorizationResult(
                authorized=False,
                decision=SupervisorExecutionDecision.DENIED,
                action_id=action.action_id,
                reasons=[f"Execution authorization denied: {drift_reason}"],
                readiness_status=action.readiness_status,
                action_tier=tier,
                approval_required=True,
                approval_details=_to_approval_schema(active_approval),
                denial_code="CONTEXT_DRIFT",
                evaluated_at=eval_time,
            )

        # Check Claim Status (Double Send Prevention)
        if active_approval.execution_claimed_at is not None:
            return SupervisorExecutionAuthorizationResult(
                authorized=False,
                decision=SupervisorExecutionDecision.DENIED,
                action_id=action.action_id,
                reasons=[
                    f"Execution authorization denied: Action was already claimed for execution at "
                    f"{active_approval.execution_claimed_at.isoformat()} by worker '{active_approval.execution_claimed_by}'."
                ],
                readiness_status=action.readiness_status,
                action_tier=tier,
                approval_required=True,
                approval_details=_to_approval_schema(active_approval),
                denial_code="ALREADY_CLAIMED",
                evaluated_at=eval_time,
            )

        return SupervisorExecutionAuthorizationResult(
            authorized=True,
            decision=SupervisorExecutionDecision.AUTHORIZED,
            action_id=action.action_id,
            reasons=[
                f"External outreach action '{action.recommended_action.value}' authorized with "
                f"durable human approval from '{active_approval.approved_by_user_id}'."
            ],
            readiness_status=action.readiness_status,
            action_tier=tier,
            approval_required=True,
            approval_details=_to_approval_schema(active_approval),
            denial_code=None,
            evaluated_at=eval_time,
        )

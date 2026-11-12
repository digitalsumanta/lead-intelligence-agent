"""
Supervisor Approval Dispatch Service.

Phase 12J - D2 - Gate D-B2:
Orchestrates transactional outbox enqueuing, pre-flight guard checks,
non-transactional network I/O isolation, post-network CAS completion,
deletion-aware cancellation/redaction, and fail-closed kill-switch gating.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.services.outreach_rate_limiter import (
    get_outreach_concurrency_gate,
    get_outreach_rate_limiter,
)

from app.domain.dispatch_errors import (
    CanaryAuthorizationError,
    DispatchAbortedError,
    DispatchAmbiguousOutcomeError,
    DispatchConfigurationError,
    DispatchDisabledError,
    DispatchDuplicateError,
    DispatchFencingConflictError,
    DispatchInvalidTransitionError,
    DispatchLeaseExpiredError,
    DispatchNotFoundError,
    DispatchRecipientNotAllowlistedError,
    DispatchRedactedPayloadError,
)
from app.domain.recipient_allowlist import mask_recipient_pii, validate_canary_recipient
from app.services.canary_authorization_service import CanaryAuthorizationService
from app.domain.dispatch_idempotency import (
    compute_canonical_idempotency_key,
    compute_payload_hash,
    normalize_recipient,
)
from app.models.entities import (
    Lead,
    SupervisorApprovalDispatch,
    SupervisorApprovalDispatchStatus,
    SupervisorHumanApproval,
    now_utc,
)
from app.repositories.supervisor_approval import SupervisorApprovalRepository
from app.repositories.supervisor_dispatch_repository import (
    SupervisorApprovalDispatchRepository,
)
from app.services.dispatch_provider import (
    DispatchProviderInterface,
    DispatchProviderResult,
    InertTestDispatchProvider,
)
from app.services.supervisor_approval_service import SupervisorApprovalService

logger = logging.getLogger("lead_agent.supervisor_dispatch_service")


def _ensure_utc(dt: Optional[datetime]) -> datetime:
    """Ensure datetime is non-null and timezone-aware UTC."""
    if dt is None:
        return now_utc()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_idempotent_transport(channel: str, provider_name: Optional[str] = None) -> bool:
    """
    Returns True if the transport natively honors client-supplied idempotency keys.
    Verified native idempotency:
    - RESEND: Honors Idempotency-Key header with 24h duplicate suppression.
    - INERT / DEFAULT_PROVIDER / MOCK: Test and dry-run providers.
    - WEBHOOK: Webhook endpoints with idempotency headers.
    Non-idempotent endpoints (strictly OUTCOME_UNKNOWN on ambiguous timeouts):
    - SMTP: Raw SMTP protocol lacks native idempotency.
    - SENDGRID: v3/mail/send lacks native client idempotency token.
    - TWILIO: Outbound Messages API lacks native client idempotency token.
    """
    p_name = (provider_name or "").upper()
    if any(non_idemp in p_name for non_idemp in ("SMTP", "SENDGRID", "TWILIO")):
        return False
    if any(idemp in p_name for idemp in ("RESEND", "INERT", "DEFAULT_PROVIDER", "MOCK")):
        return True
    ch = (channel or "").upper()
    if ch == "WEBHOOK":
        return True
    return ch in {"EMAIL", "WHATSAPP"}


class SupervisorApprovalDispatchService:
    """
    Service layer governing the transactional dispatch outbox lifecycle,
    pre-flight guard verification, provider execution isolation, and reconciliation.
    """

    def __init__(
        self,
        repository: type[SupervisorApprovalDispatchRepository] = SupervisorApprovalDispatchRepository,
        approval_repository: type[SupervisorApprovalRepository] = SupervisorApprovalRepository,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.repository = repository
        self.approval_repository = approval_repository
        self.clock = clock

    def _now(self, now: Optional[datetime] = None) -> datetime:
        if now is not None:
            return _ensure_utc(now)
        return _ensure_utc(self.clock())

    async def enqueue_dispatch(
        self,
        session: AsyncSession,
        *,
        approval_id: uuid.UUID,
        payload_json: Optional[dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> SupervisorApprovalDispatch:
        """
        Atomically enqueues an approved Sales Supervisor action into the dispatch outbox.
        Enforces:
        - Two-tier fail-closed kill switch.
        - Approval status == 'APPROVED' and not expired.
        - Authoritative context verification and fingerprint match.
        - Deterministic idempotency key calculation.
        - Safe idempotency deduplication if already enqueued.
        """
        eval_time = self._now(now)

        # 1. Global Kill Switch Check
        if not settings.effective_outreach_dispatch_enabled:
            logger.warning("Dispatch enqueue rejected: master kill switch is ACTIVE")
            raise DispatchDisabledError("Master dispatch switch outreach_dispatch_enabled is False")

        # 2. Lock Approval and re-verify status
        approval = await self.approval_repository.get_by_id_for_update(session, approval_id)
        if not approval:
            raise DispatchNotFoundError(f"Approval record '{approval_id}' not found")

        if approval.status != "APPROVED":
            raise DispatchInvalidTransitionError(
                f"Cannot enqueue dispatch for approval in status '{approval.status}' (expected 'APPROVED')"
            )

        if approval.expires_at is not None and eval_time > _ensure_utc(approval.expires_at):
            raise DispatchInvalidTransitionError(
                f"Approval '{approval_id}' has expired at {approval.expires_at.isoformat()}"
            )

        # 3. Channel Kill Switch Check
        channel = (approval.channel or "EMAIL").upper()
        if not settings.is_channel_enabled(channel):
            logger.warning("Dispatch enqueue rejected: channel '%s' is DISABLED", channel)
            raise DispatchDisabledError(f"Outreach channel '{channel}' is disabled in configuration")

        # 4. Authoritative Context Verification
        approval_service = SupervisorApprovalService()
        await approval_service.verify_authoritative_context(session, approval=approval)

        # 5. Authoritatively resolve Lead and Recipient Coordinates
        lead = await session.get(Lead, approval.lead_id)
        if not lead:
            raise DispatchNotFoundError(f"Lead '{approval.lead_id}' not found for approval '{approval_id}'")

        contact_email = None
        contact_phone = None
        if approval.contact_id is not None:
            contact_lead = await session.get(Lead, approval.contact_id)
            if contact_lead:
                contact_email = contact_lead.email
                contact_phone = contact_lead.phone

        raw_recipient = (contact_email or lead.email) if channel == "EMAIL" else (contact_phone or lead.phone or "")
        canonical_recipient = normalize_recipient(raw_recipient, channel=channel)

        if not canonical_recipient:
            raise DispatchInvalidTransitionError(
                f"Lead '{lead.id}' has no valid coordinates for channel '{channel}'"
            )

        # 5b. Canary Recipient Allowlist & Flight Authorization Enqueue Pre-Check (ACM-05, ACM-06, Finding D3.1-02)
        is_canary = (
            getattr(settings, "outreach_canary_mode", False)
            or (isinstance(payload_json, dict) and (payload_json.get("canary") is True or payload_json.get("canary_flight_id")))
            or (approval and isinstance(approval.notes, str) and "canary" in approval.notes.lower())
        )
        if is_canary and channel == "EMAIL":
            allowed_domains = getattr(settings, "canary_allowed_domain_list", [])
            is_valid, rejection_reason = validate_canary_recipient(canonical_recipient, allowed_domains)
            if not is_valid:
                raise DispatchRecipientNotAllowlistedError(
                    f"Cannot enqueue canary dispatch: recipient '{mask_recipient_pii(canonical_recipient)}' is not in approved canary allowlist ({rejection_reason})"
                )

        if is_canary:
            raw_fid = payload_json.get("canary_flight_id") if isinstance(payload_json, dict) else None
            if not raw_fid:
                raise CanaryAuthorizationError("Cannot enqueue canary dispatch: mandatory canary_flight_id is missing")
            try:
                fid = uuid.UUID(str(raw_fid))
            except (ValueError, AttributeError):
                raise CanaryAuthorizationError(f"Invalid canary_flight_id format: '{raw_fid}'")

            flight = await CanaryAuthorizationService.get_flight(session, fid)
            if not flight:
                raise CanaryAuthorizationError(f"Canary flight '{fid}' does not exist")
            if not flight.execution_authorized or flight.status != "AUTHORIZED":
                raise CanaryAuthorizationError(f"Canary flight '{fid}' is not authorized (status: {flight.status})")
            if flight.expires_at is not None and eval_time > _ensure_utc(flight.expires_at):
                raise CanaryAuthorizationError(f"Canary flight '{fid}' has expired")
            if flight.dispatched_count >= flight.max_messages:
                raise CanaryAuthorizationError(f"Canary flight '{fid}' message cap reached ({flight.dispatched_count}/{flight.max_messages})")
            provider_name = (settings.outreach_email_provider or "DEFAULT_PROVIDER").upper()
            if flight.provider_name.upper() != provider_name:
                raise CanaryAuthorizationError(
                    f"Canary flight provider mismatch: flight requires '{flight.provider_name}', current provider is '{provider_name}'"
                )

        # 6. Render payload and compute deterministic hashes
        if payload_json is None:
            payload_json = {
                "action_id": approval.action_id,
                "recommended_action": approval.recommended_action,
                "channel": channel,
                "recipient": canonical_recipient,
                "notes": approval.notes,
                "context_fingerprint": approval.context_fingerprint,
            }

        payload_hash = compute_payload_hash(payload_json)
        idempotency_key = compute_canonical_idempotency_key(
            approval_id=approval.id,
            action_id=approval.action_id,
            context_fingerprint=approval.context_fingerprint,
            recipient=canonical_recipient,
            channel=channel,
        )

        # 7. Deduplication: return existing outbox record if identical logical intent exists
        existing = await self.repository.get_by_idempotency_key(session, idempotency_key)
        if existing:
            logger.info("Dispatch intent for key %s already exists (status: %s)", idempotency_key, existing.status)
            return existing

        # 8. Insert new outbox row in PENDING status
        provider_name = (settings.outreach_email_provider or "DEFAULT_PROVIDER").upper()
        outbox_row = await self.repository.create_dispatch(
            session,
            approval_id=approval.id,
            approval_reference_id=approval.id,
            lead_id=lead.id,
            lead_reference_id=lead.id,
            action_id=approval.action_id,
            channel=channel,
            recipient=canonical_recipient,
            context_fingerprint=approval.context_fingerprint,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash,
            payload_json=payload_json,
            provider_name=provider_name,
            now=eval_time,
        )
        return outbox_row

    async def poll_and_claim_job(
        self,
        session: AsyncSession,
        *,
        worker_id: str,
        now: Optional[datetime] = None,
    ) -> Optional[SupervisorApprovalDispatch]:
        """
        Polls the queue for the next eligible job, verifies worker lease on parent approval,
        and claims the job atomically using FOR UPDATE SKIP LOCKED.
        """
        eval_time = self._now(now)

        # Kill switch check
        if not settings.effective_outreach_dispatch_enabled:
            return None

        # 1. Fetch parent approval or evaluate claim
        # First poll outbox
        job = await self.repository.poll_and_claim_next(
            session,
            worker_id=worker_id,
            lease_generation=1,
            now=eval_time,
        )
        if not job:
            return None

        # 2. Re-verify parent approval lease & TTL if linked
        if job.approval_id is not None:
            approval = await self.approval_repository.get_by_id_for_update(session, job.approval_id)
            if not approval or approval.status != "APPROVED":
                job.status = SupervisorApprovalDispatchStatus.ABORTED.value
                job.error_code = "APPROVAL_NOT_ACTIVE"
                job.error_message = "Parent approval is no longer active."
                await session.flush()
                return None

            # Verify lease ownership
            lease_duration = getattr(settings, "supervisor_lease_duration_seconds", 300)
            remaining_ttl = (_ensure_utc(approval.updated_at) + timedelta(seconds=lease_duration)) - eval_time
            if remaining_ttl.total_seconds() < 60:
                # Force heartbeat renewal before accepting job
                approval_service = SupervisorApprovalService()
                approval = await approval_service.heartbeat_lease(
                    session,
                    approval_id=approval.id,
                    worker_id=worker_id,
                    lease_generation=approval.version,
                    now=eval_time,
                )

            job.lease_generation = approval.version
            await session.flush()

        return job

    async def pre_flight_guard_check(
        self,
        session: AsyncSession,
        *,
        dispatch_id: uuid.UUID,
        worker_id: str,
        expected_lease_generation: int,
        now: Optional[datetime] = None,
    ) -> SupervisorApprovalDispatch:
        """
        Authoritative pre-flight guard check executed immediately prior to opening an external socket.
        Verifies:
        1. Status remains IN_PROGRESS (fails closed if cancelled/ABORTED).
        2. Payload is NOT redacted (fails closed on privacy erasure).
        3. Kill switches remain enabled.
        4. Parent lease version has not been revoked or stolen.
        """
        eval_time = self._now(now)
        job = await self.repository.get_by_id(session, dispatch_id)
        if not job:
            raise DispatchNotFoundError(f"Dispatch record '{dispatch_id}' missing")

        # Guard 1: Status Check
        if job.status == SupervisorApprovalDispatchStatus.ABORTED.value:
            raise DispatchAbortedError(
                f"Dispatch '{dispatch_id}' was aborted prior to socket open (reason: {job.error_code})"
            )
        if job.status != SupervisorApprovalDispatchStatus.IN_PROGRESS.value:
            raise DispatchInvalidTransitionError(
                f"Dispatch '{dispatch_id}' in status '{job.status}', expected 'IN_PROGRESS'"
            )

        # Guard 2: Redacted Payload Check
        if isinstance(job.payload_json, dict) and job.payload_json.get("redacted") is True:
            raise DispatchRedactedPayloadError(
                f"Dispatch '{dispatch_id}' payload has been redacted; socket execution forbidden"
            )

        # Guard 3: Kill Switch Check
        if not settings.effective_outreach_dispatch_enabled:
            raise DispatchDisabledError("Master outreach dispatch switch was deactivated")
        if not settings.is_channel_enabled(job.channel):
            raise DispatchDisabledError(f"Channel '{job.channel}' was deactivated")

        # Guard 4: Fencing / Lease Generation Check
        if job.lease_generation != expected_lease_generation:
            raise DispatchFencingConflictError(
                f"Dispatch lease generation {job.lease_generation} != expected {expected_lease_generation}"
            )

        if job.approval_id is not None:
            approval = await self.approval_repository.get_by_id(session, job.approval_id)
            if approval and approval.version != expected_lease_generation:
                raise DispatchFencingConflictError(
                    f"Parent approval version {approval.version} != expected generation {expected_lease_generation}"
                )

        # Provider Validation Guard (Pre-flight fail-closed on missing or unknown provider)
        if not job.provider_name or not str(job.provider_name).strip():
            job.status = SupervisorApprovalDispatchStatus.ABORTED.value
            job.error_code = "DISPATCH_CONFIGURATION_ERROR"
            job.error_message = "Dispatch preflight failed: provider_name is missing"
            await session.flush()
            raise DispatchConfigurationError("Dispatch preflight failed: provider_name is missing")

        valid_providers = {"RESEND", "SENDGRID", "INERT", "DEFAULT_PROVIDER", "MOCK"}
        if job.provider_name.strip().upper() not in valid_providers:
            job.status = SupervisorApprovalDispatchStatus.ABORTED.value
            job.error_code = "DISPATCH_CONFIGURATION_ERROR"
            job.error_message = f"Dispatch preflight failed: unknown provider '{job.provider_name}'"
            await session.flush()
            raise DispatchConfigurationError(f"Dispatch preflight failed: unknown provider '{job.provider_name}'")

        # Guard 5: Recipient Allowlist Guard (Canary / Restricted Dispatch Protection - ACM-06)
        if self.is_canary_dispatch(job) and job.channel == "EMAIL":
            allowed_domains = getattr(settings, "canary_allowed_domain_list", [])
            is_valid, rejection_reason = validate_canary_recipient(job.recipient, allowed_domains)
            if not is_valid:
                # Transition status to ABORTED to prevent retries and preserve audit trail
                job.status = SupervisorApprovalDispatchStatus.ABORTED.value
                job.error_code = "CANARY_RECIPIENT_NOT_ALLOWLISTED"
                job.error_message = f"Canary dispatch rejected: recipient '{mask_recipient_pii(job.recipient)}' domain not in approved allowlist ({rejection_reason})"
                await session.flush()
                logger.warning(
                    "Canary dispatch '%s' aborted: recipient '%s' rejected by allowlist (%s)",
                    dispatch_id,
                    mask_recipient_pii(job.recipient),
                    rejection_reason,
                )
                raise DispatchRecipientNotAllowlistedError(
                    f"Canary dispatch '{dispatch_id}' aborted: recipient '{mask_recipient_pii(job.recipient)}' is not in approved canary allowlist ({rejection_reason})"
                )

        # Guard 6: Canary Flight Dual-Authorization & Message Cap Reservation (ACM-05, Finding D3.1-02)
        payload = job.payload_json if isinstance(job.payload_json, dict) else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        raw_flight_id = payload.get("canary_flight_id") or metadata.get("canary_flight_id")

        active_sender = None
        if job.channel == "EMAIL":
            if job.provider_name.strip().upper() == "RESEND":
                active_sender = getattr(settings, "outreach_resend_from", "")
            elif job.provider_name.strip().upper() == "SENDGRID":
                active_sender = getattr(settings, "outreach_sendgrid_from", "")

        if self.is_canary_dispatch(job):
            if not raw_flight_id:
                job.status = SupervisorApprovalDispatchStatus.ABORTED.value
                job.error_code = "CANARY_FLIGHT_ID_REQUIRED"
                job.error_message = "Canary dispatch rejected: mandatory canary_flight_id is missing"
                await session.flush()
                logger.warning("Canary dispatch '%s' aborted: mandatory canary_flight_id is missing", dispatch_id)
                raise CanaryAuthorizationError("Canary dispatch rejected: mandatory canary_flight_id is missing")

            try:
                flight_id = uuid.UUID(str(raw_flight_id))
            except (ValueError, AttributeError):
                job.status = SupervisorApprovalDispatchStatus.ABORTED.value
                job.error_code = "CANARY_INVALID_FLIGHT_ID"
                job.error_message = f"Canary dispatch rejected: malformed canary_flight_id '{raw_flight_id}'"
                await session.flush()
                logger.warning("Canary dispatch '%s' aborted: malformed canary_flight_id '%s'", dispatch_id, raw_flight_id)
                raise CanaryAuthorizationError(f"Canary dispatch rejected: malformed canary_flight_id '{raw_flight_id}'")

            try:
                await CanaryAuthorizationService.reserve_dispatch_slot(
                    session,
                    flight_id=flight_id,
                    recipient=job.recipient if job.channel == "EMAIL" else None,
                    channel=job.channel,
                    provider_name=job.provider_name,
                    sender_identity=active_sender,
                    now=eval_time,
                )
            except (CanaryAuthorizationError, DispatchRecipientNotAllowlistedError) as err:
                job.status = SupervisorApprovalDispatchStatus.ABORTED.value
                job.error_code = getattr(err, "code", "CANARY_AUTHORIZATION_FAILED")
                job.error_message = f"Canary authorization rejected: {err}"
                await session.flush()
                logger.warning(
                    "Canary dispatch '%s' aborted due to canary authorization failure: %s",
                    dispatch_id,
                    err,
                )
                raise
        elif raw_flight_id:
            try:
                flight_id = uuid.UUID(str(raw_flight_id))
            except (ValueError, AttributeError):
                job.status = SupervisorApprovalDispatchStatus.ABORTED.value
                job.error_code = "CANARY_INVALID_FLIGHT_ID"
                job.error_message = f"Canary dispatch rejected: malformed canary_flight_id '{raw_flight_id}'"
                await session.flush()
                logger.warning("Canary dispatch '%s' aborted: malformed canary_flight_id '%s'", dispatch_id, raw_flight_id)
                raise CanaryAuthorizationError(f"Canary dispatch rejected: malformed canary_flight_id '{raw_flight_id}'")

            try:
                await CanaryAuthorizationService.reserve_dispatch_slot(
                    session,
                    flight_id=flight_id,
                    recipient=job.recipient if job.channel == "EMAIL" else None,
                    channel=job.channel,
                    provider_name=job.provider_name,
                    sender_identity=active_sender,
                    now=eval_time,
                )
            except (CanaryAuthorizationError, DispatchRecipientNotAllowlistedError) as err:
                job.status = SupervisorApprovalDispatchStatus.ABORTED.value
                job.error_code = getattr(err, "code", "CANARY_AUTHORIZATION_FAILED")
                job.error_message = f"Canary authorization rejected: {err}"
                await session.flush()
                logger.warning(
                    "Canary dispatch '%s' aborted due to canary authorization failure: %s",
                    dispatch_id,
                    err,
                )
                raise

        return job

    @staticmethod
    def is_canary_dispatch(job: SupervisorApprovalDispatch) -> bool:
        """Determines whether a dispatch job is part of a canary rollout."""
        if getattr(settings, "outreach_canary_mode", False):
            return True
        payload = job.payload_json if isinstance(job.payload_json, dict) else {}
        if payload.get("canary") is True or payload.get("is_canary") is True:
            return True
        if payload.get("canary_flight_id"):
            return True
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        if metadata.get("canary_authorized") is True or metadata.get("canary_flight_id"):
            return True
        return False

    async def execute_claimed_dispatch(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        dispatch_id: uuid.UUID,
        worker_id: str,
        expected_lease_generation: int,
        provider: DispatchProviderInterface,
        now: Optional[datetime] = None,
    ) -> SupervisorApprovalDispatch:
        """
        Executes a claimed dispatch with strict transaction isolation:
        - Transaction 1: Pre-flight guard verification.
        - Non-transactional provider invocation outside DB locks.
        - Transaction 2: Atomic post-network CAS completion update.
        """
        eval_time = self._now(now)

        # -------------------------------------------------------------
        # STEP 1: Pre-Flight Guard Check (Inside short read transaction)
        # -------------------------------------------------------------
        async with session_factory() as session:
            job = await self.pre_flight_guard_check(
                session,
                dispatch_id=dispatch_id,
                worker_id=worker_id,
                expected_lease_generation=expected_lease_generation,
                now=eval_time,
            )
            # Capture job attributes in memory
            channel = job.channel
            recipient = job.recipient
            payload_json = dict(job.payload_json)
            idempotency_key = job.idempotency_key
            attempt_count = job.attempt_count
            max_attempts = job.max_attempts
            provider_name = job.provider_name

        # -------------------------------------------------------------
        # STEP 2: Non-Transactional External Network Execution
        # (Zero DB locks or transactions held!)
        # -------------------------------------------------------------
        # Decision DC3-03: Inject dispatch_id into payload metadata for provider correlation
        effective_payload = dict(payload_json or {})
        meta = dict(effective_payload.get("metadata") or {})
        meta["dispatch_id"] = str(dispatch_id)
        effective_payload["metadata"] = meta

        timeout_sec = getattr(settings, "outreach_dispatch_timeout_seconds", 10.0)
        try:
            # Decision DC3-07: Concurrency gate and Token Bucket rate limiter
            async with get_outreach_concurrency_gate():
                await get_outreach_rate_limiter().acquire()

                # Finding 4: Enforce aggregate wall-clock timeout
                async with asyncio.timeout(timeout_sec):
                    provider_result: DispatchProviderResult = await provider.send_dispatch(
                        channel=channel,
                        recipient=recipient,
                        payload_json=effective_payload,
                        idempotency_key=idempotency_key,
                    )
        except TimeoutError:
            logger.warning("Aggregate wall-clock timeout (%ss) reached for dispatch '%s'", timeout_sec, dispatch_id)
            provider_result = DispatchProviderResult(
                success=False,
                error_code="AGGREGATE_WALL_CLOCK_TIMEOUT",
                error_message=f"Outbound dispatch exceeded aggregate wall-clock deadline ({timeout_sec}s)",
                is_transient=False,
                is_ambiguous=True,
            )
        except Exception as exc:
            logger.exception("Unexpected exception during dispatch provider call: %s", exc)
            provider_result = DispatchProviderResult(
                success=False,
                error_code="PROVIDER_UNHANDLED_EXCEPTION",
                error_message=str(exc),
                is_transient=True,
            )


        # -------------------------------------------------------------
        # STEP 3: Post-Network CAS Completion (Inside atomic transaction)
        # -------------------------------------------------------------
        eval_completion_time = self._now(now)
        async with session_factory() as session:
            async with session.begin():
                if provider_result.success:
                    outcome_status = SupervisorApprovalDispatchStatus.SENT.value
                    completed_job = await self.repository.record_completion_post_network(
                        session,
                        dispatch_id=dispatch_id,
                        outcome_status=outcome_status,
                        expected_lease_generation=expected_lease_generation,
                        worker_id=worker_id,
                        provider_message_id=provider_result.provider_message_id,
                        now=eval_completion_time,
                    )
                elif provider_result.is_ambiguous or (
                    not is_idempotent_transport(channel, provider_name)
                    and any(term in (provider_result.error_code or "").upper() for term in ("TIMEOUT", "AMBIGUOUS", "SOCKET"))
                ):
                    # Ambiguous timeouts on non-idempotent transports: STRICTLY NO AUTO-RETRY!
                    outcome_status = SupervisorApprovalDispatchStatus.OUTCOME_UNKNOWN.value
                    completed_job = await self.repository.record_completion_post_network(
                        session,
                        dispatch_id=dispatch_id,
                        outcome_status=outcome_status,
                        expected_lease_generation=expected_lease_generation,
                        worker_id=worker_id,
                        error_code=provider_result.error_code or "AMBIGUOUS_PROVIDER_TIMEOUT",
                        error_message=provider_result.error_message or "Ambiguous provider outcome; automated retry prohibited.",
                        next_retry_at=None,  # No auto retry
                        now=eval_completion_time,
                    )
                elif provider_result.is_transient and attempt_count < max_attempts:
                    # Exponential backoff retry on idempotent transports
                    outcome_status = SupervisorApprovalDispatchStatus.RETRYABLE_FAILURE.value
                    backoff_sec = 30 * (2 ** (attempt_count - 1))
                    next_retry = eval_completion_time + timedelta(seconds=backoff_sec)
                    completed_job = await self.repository.record_completion_post_network(
                        session,
                        dispatch_id=dispatch_id,
                        outcome_status=outcome_status,
                        expected_lease_generation=expected_lease_generation,
                        worker_id=worker_id,
                        error_code=provider_result.error_code or "TRANSIENT_PROVIDER_ERROR",
                        error_message=provider_result.error_message,
                        next_retry_at=next_retry,
                        now=eval_completion_time,
                    )
                else:
                    # Definitive client error or max attempts exceeded
                    outcome_status = SupervisorApprovalDispatchStatus.FAILED.value
                    completed_job = await self.repository.record_completion_post_network(
                        session,
                        dispatch_id=dispatch_id,
                        outcome_status=outcome_status,
                        expected_lease_generation=expected_lease_generation,
                        worker_id=worker_id,
                        error_code=provider_result.error_code or "PROVIDER_FAILURE",
                        error_message=provider_result.error_message,
                        now=eval_completion_time,
                    )

        return completed_job

    async def dry_run_dispatch(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        approval_id: uuid.UUID,
        worker_id: str = "dry-run-worker",
        now: Optional[datetime] = None,
    ) -> tuple[SupervisorApprovalDispatch, InertTestDispatchProvider]:
        """
        Executes an end-to-end dry-run dispatch using InertTestDispatchProvider.
        Guarantees zero outbound external network activity.
        """
        eval_time = self._now(now)
        inert_provider = InertTestDispatchProvider()

        # 1. Enqueue
        async with session_factory() as session:
            async with session.begin():
                dispatch = await self.enqueue_dispatch(session, approval_id=approval_id, now=eval_time)
                dispatch_id = dispatch.id

        # 2. Claim
        async with session_factory() as session:
            async with session.begin():
                claimed = await self.poll_and_claim_job(session, worker_id=worker_id, now=eval_time)
                if not claimed or claimed.id != dispatch_id:
                    claimed = await self.repository.get_by_id_for_update(session, dispatch_id)
                    claimed.status = SupervisorApprovalDispatchStatus.IN_PROGRESS.value
                    claimed.dispatch_worker_id = worker_id
                    claimed.attempt_count += 1
                    await session.flush()
                lease_gen = claimed.lease_generation

        # 3. Execute with inert provider
        completed = await self.execute_claimed_dispatch(
            session_factory,
            dispatch_id=dispatch_id,
            worker_id=worker_id,
            expected_lease_generation=lease_gen,
            provider=inert_provider,
            now=eval_time,
        )
        return completed, inert_provider

    async def handle_lead_deletion(
        self,
        session: AsyncSession,
        *,
        lead_id: uuid.UUID,
        reason: str = "LEAD_DELETED",
        now: Optional[datetime] = None,
    ) -> int:
        """
        Transactional deletion-aware cancellation and payload redaction for a lead.
        """
        eval_time = self._now(now)
        return await self.repository.cancel_and_redact_for_lead_deletion(
            session, lead_id=lead_id, reason=reason, now=eval_time
        )

    async def reconcile_unknown_dispatch(
        self,
        session: AsyncSession,
        *,
        dispatch_id: uuid.UUID,
        resolution: str,
        provider_message_id: Optional[str] = None,
        notes: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> SupervisorApprovalDispatch:
        """
        Allows authorized operators to reconcile OUTCOME_UNKNOWN dispatch records.
        - resolution == "SENT": transitions to RECONCILED -> SENT.
        - resolution == "FAILED_CAN_RETRY": transitions to RECONCILED -> PENDING.
        - resolution == "FAILED": transitions to RECONCILED -> FAILED.
        """
        eval_time = self._now(now)
        record = await self.repository.get_by_id_for_update(session, dispatch_id)
        if not record:
            raise DispatchNotFoundError(f"Dispatch '{dispatch_id}' not found")

        if record.status != SupervisorApprovalDispatchStatus.OUTCOME_UNKNOWN.value:
            raise DispatchInvalidTransitionError(
                f"Cannot reconcile dispatch in status '{record.status}' (expected 'OUTCOME_UNKNOWN')"
            )

        res_upper = resolution.strip().upper()
        if res_upper == "SENT":
            to_status = SupervisorApprovalDispatchStatus.SENT.value
        elif res_upper in {"FAILED_CAN_RETRY", "RETRY"}:
            to_status = SupervisorApprovalDispatchStatus.PENDING.value
        elif res_upper == "FAILED":
            to_status = SupervisorApprovalDispatchStatus.FAILED.value
        else:
            raise DispatchInvalidTransitionError(f"Unsupported reconciliation resolution: '{resolution}'")

        # Step 1: Transition to RECONCILED
        await self.repository.transition_state(
            session,
            dispatch_id=dispatch_id,
            to_status=SupervisorApprovalDispatchStatus.RECONCILED.value,
            reconciliation_notes=notes,
            now=eval_time,
        )

        # Step 2: Transition from RECONCILED to final target status
        return await self.repository.transition_state(
            session,
            dispatch_id=dispatch_id,
            to_status=to_status,
            provider_message_id=provider_message_id,
            reconciliation_notes=notes,
            now=eval_time,
        )

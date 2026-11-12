"""
Canary Flight Two-Person Authorization and Cumulative Message Cap Service.

Phase 12J - D2 - Gate D-D3:
Enforces multi-party authorization (ACM-05), scope fingerprinting (immutability),
rejection of shared development identities, self-approval prevention,
and atomic reservation of cumulative message caps via PostgreSQL row-level locks.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.dispatch_errors import (
    CanaryAuthorizationError,
    CanaryDualApprovalRequiredError,
    CanaryFlightExpiredError,
    CanaryFlightRevokedError,
    CanaryMessageCapExceededError,
    CanaryScopeDriftError,
    CanarySelfApprovalError,
    DispatchRecipientNotAllowlistedError,
)
from app.domain.recipient_allowlist import (
    mask_recipient_pii,
    normalize_email_address,
    validate_canary_recipient,
)
from app.models.entities import (
    CanaryFlightAuthorization,
    CanaryFlightStatus,
    now_utc,
)

logger = logging.getLogger("lead_agent.canary_authorization")

# Disallowed shared or unverified development identities for multi-party signatures
DISALLOWED_SHARED_IDENTITIES: frozenset[str] = frozenset({
    "dev", "developer", "admin", "administrator", "test", "tester",
    "root", "anonymous", "system", "default", "user", "demo",
    "stage", "staging", "local", "ci", "bot", "guest", "mock",
    "local_api_user", "system_worker", "system_sweeper",
})


def is_shared_development_identity(user_id: Optional[str]) -> bool:
    """Checks if a user_id represents a shared unverified development/system identity."""
    if not user_id:
        return True
    return user_id.strip().lower() in DISALLOWED_SHARED_IDENTITIES


def compute_canary_scope_fingerprint(
    *,
    flight_name: str,
    environment: str,
    provider_name: str,
    allowed_domains: Sequence[str],
    max_messages: int,
    max_concurrency: int,
    rate_limit_per_second: float,
    valid_from: datetime,
    expires_at: datetime,
    channel: str = "EMAIL",
    sender_identity: Optional[str] = None,
) -> str:
    """
    Computes a deterministic canonical SHA-256 fingerprint over material canary scope parameters.
    Any modification to domains, message counts, rate limits, validity window, channel, or sender changes the fingerprint.
    """
    vf_utc = valid_from if valid_from.tzinfo else valid_from.replace(tzinfo=timezone.utc)
    exp_utc = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)

    canonical_payload: dict[str, Any] = {
        "allowed_domains": sorted(list({d.strip().lower() for d in allowed_domains if d and d.strip()})),
        "channel": (channel or "EMAIL").strip().upper(),
        "environment": environment.strip().lower(),
        "expires_at": exp_utc.astimezone(timezone.utc).isoformat(),
        "flight_name": flight_name.strip(),
        "max_concurrency": int(max_concurrency),
        "max_messages": int(max_messages),
        "provider_name": provider_name.strip().upper(),
        "rate_limit_per_second": round(float(rate_limit_per_second), 4),
        "sender_identity": sender_identity.strip().lower() if sender_identity and sender_identity.strip() else "",
        "valid_from": vf_utc.astimezone(timezone.utc).isoformat(),
    }
    serialized = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class CanaryAuthorizationService:
    """Service managing canary flight authorization lifecycle and atomic quota enforcement."""

    @staticmethod
    def compute_fingerprint(
        *,
        flight_name: str,
        environment: str,
        provider_name: str,
        allowed_domains: Sequence[str],
        max_messages: int,
        max_concurrency: int,
        rate_limit_per_second: float,
        valid_from: datetime,
        expires_at: datetime,
        channel: str = "EMAIL",
        sender_identity: Optional[str] = None,
    ) -> str:
        return compute_canary_scope_fingerprint(
            flight_name=flight_name,
            environment=environment,
            provider_name=provider_name,
            allowed_domains=allowed_domains,
            max_messages=max_messages,
            max_concurrency=max_concurrency,
            rate_limit_per_second=rate_limit_per_second,
            valid_from=valid_from,
            expires_at=expires_at,
            channel=channel,
            sender_identity=sender_identity,
        )

    @classmethod
    async def create_flight_scope(
        cls,
        session: AsyncSession,
        *,
        flight_name: str,
        environment: str = "staging",
        provider_name: str = "RESEND",
        channel: str = "EMAIL",
        sender_identity: Optional[str] = None,
        allowed_domains: Sequence[str],
        max_messages: int = 5,
        max_concurrency: int = 1,
        rate_limit_per_second: float = 0.2,
        valid_from: datetime,
        expires_at: datetime,
    ) -> CanaryFlightAuthorization:
        """Defines a proposed canary flight scope in PENDING_APPROVAL status."""
        if not flight_name or not flight_name.strip():
            raise CanaryAuthorizationError("flight_name cannot be empty")
        if not channel or not channel.strip():
            raise CanaryAuthorizationError("channel cannot be empty")
        if max_messages <= 0:
            raise CanaryAuthorizationError("max_messages must be greater than 0")
        if max_concurrency <= 0:
            raise CanaryAuthorizationError("max_concurrency must be greater than 0")
        if rate_limit_per_second <= 0:
            raise CanaryAuthorizationError("rate_limit_per_second must be greater than 0")
        if valid_from >= expires_at:
            raise CanaryAuthorizationError("valid_from must be earlier than expires_at")

        clean_domains = sorted(list({d.strip().lower() for d in allowed_domains if d and d.strip()}))
        if not clean_domains:
            raise CanaryAuthorizationError("allowed_domains must contain at least one valid domain")

        clean_channel = channel.strip().upper()
        clean_sender = None
        if sender_identity and sender_identity.strip():
            if clean_channel == "EMAIL":
                try:
                    canonical_email, _, _ = normalize_email_address(sender_identity)
                    clean_sender = canonical_email.lower()
                except ValueError as exc:
                    raise CanaryAuthorizationError(f"Invalid sender email address '{sender_identity}': {exc}") from exc
            else:
                clean_sender = sender_identity.strip()

        fingerprint = cls.compute_fingerprint(
            flight_name=flight_name,
            environment=environment,
            provider_name=provider_name,
            allowed_domains=clean_domains,
            max_messages=max_messages,
            max_concurrency=max_concurrency,
            rate_limit_per_second=rate_limit_per_second,
            valid_from=valid_from,
            expires_at=expires_at,
            channel=clean_channel,
            sender_identity=clean_sender,
        )

        flight = CanaryFlightAuthorization(
            flight_name=flight_name.strip(),
            environment=environment.strip().lower(),
            provider_name=provider_name.strip().upper(),
            channel=clean_channel,
            sender_identity=clean_sender,
            allowed_domains_json=clean_domains,
            max_messages=max_messages,
            dispatched_count=0,
            max_concurrency=max_concurrency,
            rate_limit_per_second=float(rate_limit_per_second),
            valid_from=valid_from,
            expires_at=expires_at,
            scope_fingerprint=fingerprint,
            status=CanaryFlightStatus.PENDING_APPROVAL.value,
            execution_authorized=False,
        )
        session.add(flight)
        await session.flush()
        logger.info(
            "Created canary flight '%s' (%s) with fingerprint %s (channel: %s, sender: %s)",
            flight.flight_name,
            flight.id,
            fingerprint,
            clean_channel,
            clean_sender,
        )
        return flight

    @classmethod
    async def submit_flight_approval(
        cls,
        session: AsyncSession,
        *,
        flight_id: uuid.UUID,
        signer_user_id: str,
        signer_role: str,
        scope_fingerprint: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> CanaryFlightAuthorization:
        """
        Submits an approval signature for a canary flight scope.
        Requires two distinct authenticated human identities. Prevents self-approval.
        """
        eval_time = (now or now_utc())
        if not eval_time.tzinfo:
            eval_time = eval_time.replace(tzinfo=timezone.utc)

        stmt = (
            select(CanaryFlightAuthorization)
            .where(CanaryFlightAuthorization.id == flight_id)
            .with_for_update()
        )
        result = await session.execute(stmt)
        flight = result.scalar_one_or_none()
        if not flight:
            raise CanaryAuthorizationError(f"Canary flight '{flight_id}' not found")

        if flight.status == CanaryFlightStatus.REVOKED.value:
            raise CanaryFlightRevokedError(f"Canary flight '{flight_id}' has been revoked: {flight.revocation_reason}")

        flight_expires = flight.expires_at if flight.expires_at.tzinfo else flight.expires_at.replace(tzinfo=timezone.utc)
        if flight.status == CanaryFlightStatus.EXPIRED.value or eval_time >= flight_expires:
            flight.status = CanaryFlightStatus.EXPIRED.value
            flight.execution_authorized = False
            await session.flush()
            raise CanaryFlightExpiredError(f"Canary flight '{flight_id}' expired at {flight.expires_at}")

        if flight.status == CanaryFlightStatus.EXHAUSTED.value:
            raise CanaryMessageCapExceededError(f"Canary flight '{flight_id}' is already exhausted")

        # Identity validation
        if is_shared_development_identity(signer_user_id):
            raise CanaryAuthorizationError(
                f"Shared or generic development identity '{signer_user_id}' cannot sign canary authorizations"
            )

        if not signer_role or not signer_role.strip():
            raise CanaryAuthorizationError("Signer role must not be empty")

        # Scope fingerprint validation (anti-drift)
        if scope_fingerprint and scope_fingerprint != flight.scope_fingerprint:
            raise CanaryScopeDriftError(
                f"Scope fingerprint mismatch: provided '{scope_fingerprint}', actual '{flight.scope_fingerprint}'"
            )

        clean_signer = signer_user_id.strip()
        clean_role = signer_role.strip()

        if flight.signer_1_user_id is None:
            flight.signer_1_user_id = clean_signer
            flight.signer_1_role = clean_role
            flight.signer_1_signed_at = eval_time
            flight.status = CanaryFlightStatus.PENDING_APPROVAL.value
            logger.info("Canary flight '%s' signed by Signer 1: %s (%s)", flight.id, clean_signer, clean_role)
        elif flight.signer_2_user_id is None:
            if clean_signer.lower() == flight.signer_1_user_id.lower():
                raise CanarySelfApprovalError(
                    f"Signer 2 ('{clean_signer}') must be a distinct human principal from Signer 1 ('{flight.signer_1_user_id}')"
                )
            flight.signer_2_user_id = clean_signer
            flight.signer_2_role = clean_role
            flight.signer_2_signed_at = eval_time
            flight.status = CanaryFlightStatus.AUTHORIZED.value
            flight.execution_authorized = True
            logger.info(
                "Canary flight '%s' dual-authorized by Signer 1 (%s) and Signer 2 (%s)",
                flight.id,
                flight.signer_1_user_id,
                clean_signer,
            )
        else:
            raise CanaryAuthorizationError(f"Canary flight '{flight_id}' already has dual signatures")

        await session.flush()
        return flight

    @classmethod
    async def revoke_flight_authorization(
        cls,
        session: AsyncSession,
        *,
        flight_id: uuid.UUID,
        revoked_by_user_id: str,
        revocation_reason: str,
        now: Optional[datetime] = None,
    ) -> CanaryFlightAuthorization:
        """Revokes a canary flight authorization immediately and prevents any future dispatches."""
        eval_time = (now or now_utc())
        if not eval_time.tzinfo:
            eval_time = eval_time.replace(tzinfo=timezone.utc)

        stmt = (
            select(CanaryFlightAuthorization)
            .where(CanaryFlightAuthorization.id == flight_id)
            .with_for_update()
        )
        result = await session.execute(stmt)
        flight = result.scalar_one_or_none()
        if not flight:
            raise CanaryAuthorizationError(f"Canary flight '{flight_id}' not found")

        flight.status = CanaryFlightStatus.REVOKED.value
        flight.execution_authorized = False
        flight.revoked_by_user_id = revoked_by_user_id.strip() if revoked_by_user_id else "unknown"
        flight.revoked_at = eval_time
        flight.revocation_reason = revocation_reason.strip() if revocation_reason else "No reason provided"

        await session.flush()
        logger.warning(
            "Canary flight '%s' revoked by '%s': %s",
            flight.id,
            flight.revoked_by_user_id,
            flight.revocation_reason,
        )
        return flight

    @classmethod
    async def reserve_dispatch_slot(
        cls,
        session: AsyncSession,
        *,
        flight_id: uuid.UUID,
        recipient: Optional[str] = None,
        channel: Optional[str] = None,
        provider_name: Optional[str] = None,
        sender_identity: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> CanaryFlightAuthorization:
        """
        Atomically checks authorization, validates recipient domain allowlist,
        verifies provider/channel/sender scope against flight authorization,
        verifies canonical scope fingerprint integrity,
        and reserves one dispatch unit under PostgreSQL row-level lock.
        Transitions status to EXHAUSTED when dispatched_count reaches max_messages.
        """
        eval_time = (now or now_utc())
        if not eval_time.tzinfo:
            eval_time = eval_time.replace(tzinfo=timezone.utc)

        stmt = (
            select(CanaryFlightAuthorization)
            .where(CanaryFlightAuthorization.id == flight_id)
            .with_for_update()
        )
        result = await session.execute(stmt)
        flight = result.scalar_one_or_none()
        if not flight:
            raise CanaryAuthorizationError(f"Canary flight '{flight_id}' not found")

        if flight.status == CanaryFlightStatus.REVOKED.value:
            raise CanaryFlightRevokedError(f"Canary flight '{flight_id}' was revoked: {flight.revocation_reason}")

        flight_expires = flight.expires_at if flight.expires_at.tzinfo else flight.expires_at.replace(tzinfo=timezone.utc)
        if flight.status == CanaryFlightStatus.EXPIRED.value or eval_time >= flight_expires:
            flight.status = CanaryFlightStatus.EXPIRED.value
            flight.execution_authorized = False
            await session.flush()
            raise CanaryFlightExpiredError(f"Canary flight '{flight_id}' expired at {flight.expires_at}")

        if flight.status == CanaryFlightStatus.EXHAUSTED.value or flight.dispatched_count >= flight.max_messages:
            flight.status = CanaryFlightStatus.EXHAUSTED.value
            flight.execution_authorized = False
            await session.flush()
            raise CanaryMessageCapExceededError(
                f"Canary flight '{flight_id}' cumulative cap ({flight.max_messages}) exhausted"
            )

        if (
            flight.status != CanaryFlightStatus.AUTHORIZED.value
            or not flight.signer_1_user_id
            or not flight.signer_2_user_id
            or not flight.execution_authorized
        ):
            raise CanaryDualApprovalRequiredError(
                f"Canary flight '{flight_id}' is not fully dual-authorized (status: {flight.status})"
            )

        # 1. Canonical scope fingerprint verification (detects schema or database drift)
        flight_channel = (getattr(flight, "channel", "EMAIL") or "EMAIL").strip().upper()
        flight_sender = getattr(flight, "sender_identity", None)
        expected_fingerprint = compute_canary_scope_fingerprint(
            flight_name=flight.flight_name,
            environment=flight.environment,
            provider_name=flight.provider_name,
            allowed_domains=flight.allowed_domains_json,
            max_messages=flight.max_messages,
            max_concurrency=flight.max_concurrency,
            rate_limit_per_second=flight.rate_limit_per_second,
            valid_from=flight.valid_from,
            expires_at=flight.expires_at,
            channel=flight_channel,
            sender_identity=flight_sender,
        )
        if flight.scope_fingerprint != expected_fingerprint:
            raise CanaryScopeDriftError(
                f"Canary flight '{flight.id}' scope fingerprint mismatch: stored '{flight.scope_fingerprint}' != expected '{expected_fingerprint}'"
            )

        # 2. Provider match verification
        if provider_name and flight.provider_name.strip().upper() != provider_name.strip().upper():
            raise CanaryAuthorizationError(
                f"Provider mismatch: dispatch job provider '{provider_name}' does not match flight provider '{flight.provider_name}'",
                code="CANARY_PROVIDER_MISMATCH",
            )

        # 3. Channel match verification
        if channel and flight_channel != channel.strip().upper():
            raise CanaryAuthorizationError(
                f"Channel mismatch: dispatch job channel '{channel}' does not match flight channel '{flight_channel}'",
                code="CANARY_CHANNEL_MISMATCH",
            )

        # 4. Sender identity match verification
        if flight_sender and flight_sender.strip():
            clean_job_sender = (sender_identity or "").strip().lower()
            if clean_job_sender != flight_sender.strip().lower():
                raise CanaryAuthorizationError(
                    f"Sender mismatch: dispatch sender '{sender_identity or ''}' does not match flight authorized sender '{flight_sender}'",
                    code="CANARY_SENDER_MISMATCH",
                )

        # 5. Recipient allowlist check against the flight's own allowed domains
        if recipient:
            allowed = flight.allowed_domains_json or []
            is_valid, reason = validate_canary_recipient(recipient, allowed)
            if not is_valid:
                raise DispatchRecipientNotAllowlistedError(
                    f"Recipient '{mask_recipient_pii(recipient)}' not allowed by canary flight '{flight.flight_name}': {reason}"
                )

        # Atomically increment cumulative count
        flight.dispatched_count += 1
        if flight.dispatched_count >= flight.max_messages:
            flight.status = CanaryFlightStatus.EXHAUSTED.value
            flight.execution_authorized = False
            logger.info("Canary flight '%s' reached cap of %s messages; status set to EXHAUSTED", flight.id, flight.max_messages)

        await session.flush()
        return flight

    @classmethod
    async def get_flight(
        cls,
        session: AsyncSession,
        flight_id: uuid.UUID,
    ) -> Optional[CanaryFlightAuthorization]:
        """Fetches a flight by ID."""
        stmt = select(CanaryFlightAuthorization).where(CanaryFlightAuthorization.id == flight_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    @classmethod
    async def list_flights(
        cls,
        session: AsyncSession,
        *,
        limit: int = 50,
    ) -> Sequence[CanaryFlightAuthorization]:
        """Lists recent flights."""
        stmt = (
            select(CanaryFlightAuthorization)
            .order_by(CanaryFlightAuthorization.created_at.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return result.scalars().all()

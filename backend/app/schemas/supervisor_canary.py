"""
Pydantic schemas for Canary Flight Two-Person Authorization.

Phase 12J - D2 - Gate D-D3
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class CanaryFlightCreateRequest(BaseModel):
    flight_name: str = Field(..., min_length=1, max_length=120)
    environment: str = Field("staging", max_length=30)
    provider_name: str = Field("RESEND", max_length=60)
    channel: str = Field("EMAIL", max_length=30)
    sender_identity: Optional[str] = Field(None, max_length=255)
    allowed_domains: list[str] = Field(..., min_length=1)
    max_messages: int = Field(5, gt=0, le=100)
    max_concurrency: int = Field(1, gt=0, le=10)
    rate_limit_per_second: float = Field(0.2, gt=0.0, le=10.0)
    valid_from: datetime
    expires_at: datetime


class CanaryFlightApproveRequest(BaseModel):
    signer_user_id: Optional[str] = Field(None, min_length=1, max_length=120)
    signer_role: Optional[str] = Field(None, min_length=1, max_length=60)
    scope_fingerprint: Optional[str] = Field(None, min_length=64, max_length=64)


class CanaryFlightRevokeRequest(BaseModel):
    revoked_by_user_id: Optional[str] = Field(None, min_length=1, max_length=120)
    revocation_reason: str = Field(..., min_length=1, max_length=500)


class CanaryFlightResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    flight_name: str
    environment: str
    provider_name: str
    channel: str = "EMAIL"
    sender_identity: Optional[str] = None
    allowed_domains_json: list[str]
    max_messages: int
    dispatched_count: int
    max_concurrency: int
    rate_limit_per_second: float
    valid_from: datetime
    expires_at: datetime
    scope_fingerprint: str
    status: str
    execution_authorized: bool
    signer_1_user_id: Optional[str] = None
    signer_1_role: Optional[str] = None
    signer_1_signed_at: Optional[datetime] = None
    signer_2_user_id: Optional[str] = None
    signer_2_role: Optional[str] = None
    signer_2_signed_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    revoked_by_user_id: Optional[str] = None
    revoked_at: Optional[datetime] = None
    revocation_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime

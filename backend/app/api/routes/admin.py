"""Dashboard administration: user management (admins) and read-only system status (managers, admins).

Secrets are never returned: the status endpoint reports only whether things are configured.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.entities import USER_ROLES, AppUser, AuthSession, EmailSuppression, WorkerHeartbeat, now_utc
from app.services import auth

router = APIRouter(prefix="/api/admin", tags=["Administration"])

Role = Literal["viewer", "sales_rep", "sales_manager", "admin"]


# ---------------------------------------------------------------------------
# Role checks
# ---------------------------------------------------------------------------

def current_role(request: Request) -> str:
    return (getattr(request.state, "authenticated_role", None) or "viewer").strip().lower()


def require_roles(*roles: str):
    allowed = {r.lower() for r in roles}

    def dependency(request: Request) -> str:
        role = current_role(request)
        if role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail={
                "code": "FORBIDDEN", "message": "Your role does not allow this action"})
        return role
    return dependency


require_admin = require_roles("admin")
require_manager = require_roles("sales_manager", "admin")


def current_account_id(request: Request) -> Optional[uuid.UUID]:
    raw = getattr(request.state, "authenticated_account_id", None)
    try:
        return uuid.UUID(raw) if raw else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: Optional[str]
    role: str
    is_active: bool
    locked: bool
    last_login_at: Optional[datetime]
    created_at: datetime
    active_sessions: int


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    display_name: Optional[str] = Field(default=None, max_length=120)
    role: Role = "viewer"
    password: str = Field(min_length=1, max_length=256)


class UserUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=120)
    role: Optional[Role] = None
    is_active: Optional[bool] = None


class PasswordReset(BaseModel):
    password: str = Field(min_length=1, max_length=256)


def _bad_request(code: str, message: str) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail={"code": code, "message": message})


async def _user_out(db: AsyncSession, user: AppUser) -> UserOut:
    now = now_utc()
    sessions = await db.scalar(select(func.count()).select_from(AuthSession).where(
        AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None), AuthSession.expires_at > now))
    locked_until = auth._aware(user.locked_until)
    return UserOut(id=user.id, email=user.email, display_name=user.display_name, role=user.role,
                   is_active=user.is_active, locked=bool(locked_until and locked_until > now),
                   last_login_at=user.last_login_at, created_at=user.created_at, active_sessions=int(sessions or 0))


async def _get_user(db: AsyncSession, user_id: uuid.UUID) -> AppUser:
    user = await db.get(AppUser, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail={"code": "USER_NOT_FOUND", "message": "User not found"})
    return user


async def _other_active_admins(db: AsyncSession, user_id: uuid.UUID) -> int:
    return int(await db.scalar(select(func.count()).select_from(AppUser).where(
        AppUser.role == "admin", AppUser.is_active.is_(True), AppUser.id != user_id)) or 0)


@router.get("/users", response_model=list[UserOut], dependencies=[Depends(require_admin)])
async def list_users(db: AsyncSession = Depends(get_db)) -> list[UserOut]:
    users = (await db.scalars(select(AppUser).order_by(AppUser.email))).all()
    return [await _user_out(db, u) for u in users]


@router.post("/users", response_model=UserOut, status_code=201, dependencies=[Depends(require_admin)])
async def create_user(body: UserCreate, db: AsyncSession = Depends(get_db)) -> UserOut:
    try:
        user = await auth.create_user(db, email=body.email, password=body.password, role=body.role,
                                      display_name=(body.display_name or "").strip() or None)
    except ValueError as err:
        raise _bad_request("INVALID_USER", str(err)) from None
    await db.commit()
    return await _user_out(db, user)


@router.patch("/users/{user_id}", response_model=UserOut, dependencies=[Depends(require_admin)])
async def update_user(user_id: uuid.UUID, body: UserUpdate, request: Request, db: AsyncSession = Depends(get_db)) -> UserOut:
    user = await _get_user(db, user_id)
    is_self = current_account_id(request) == user.id
    end_sessions = False

    if body.role is not None and body.role != user.role:
        if user.role == "admin" and await _other_active_admins(db, user.id) == 0:
            raise _bad_request("LAST_ADMIN", "At least one active admin is required")
        user.role = auth.validate_role(body.role)
        end_sessions = True  # new role applies from the next sign-in
    if body.is_active is False and user.is_active:
        if is_self:
            raise _bad_request("CANNOT_DEACTIVATE_SELF", "You cannot deactivate your own account")
        if user.role == "admin" and await _other_active_admins(db, user.id) == 0:
            raise _bad_request("LAST_ADMIN", "At least one active admin is required")
        user.is_active = False
        end_sessions = True
    elif body.is_active is True:
        # Activating (or re-confirming an active account) also clears a failed-login lockout.
        user.is_active = True
        user.failed_login_count = 0
        user.locked_until = None
    if body.display_name is not None:
        user.display_name = body.display_name.strip() or None
    if end_sessions:
        await auth.revoke_user_sessions(db, user.id)
    await db.commit()
    return await _user_out(db, user)


@router.post("/users/{user_id}/reset-password", response_model=UserOut, dependencies=[Depends(require_admin)])
async def reset_password(user_id: uuid.UUID, body: PasswordReset, db: AsyncSession = Depends(get_db)) -> UserOut:
    user = await _get_user(db, user_id)
    try:
        await auth.set_password(db, user, body.password)
    except ValueError as err:
        raise _bad_request("WEAK_PASSWORD", str(err)) from None
    await db.commit()
    return await _user_out(db, user)


@router.post("/users/{user_id}/sign-out", response_model=UserOut, dependencies=[Depends(require_admin)])
async def sign_out_user(user_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> UserOut:
    user = await _get_user(db, user_id)
    await auth.revoke_user_sessions(db, user.id)
    await db.commit()
    return await _user_out(db, user)


# ---------------------------------------------------------------------------
# System status (read-only, no secret values)
# ---------------------------------------------------------------------------

@router.get("/system-status", dependencies=[Depends(require_manager)])
async def system_status(db: AsyncSession = Depends(get_db)) -> dict:
    from app.database import SCHEMA_REVISION
    from app.services import credential_crypto
    from app.services.email_compliance import ComplianceConfigurationError, get_compliance_config

    revision = await db.scalar(text("SELECT version_num FROM alembic_version")) if await _has_table(db, "alembic_version") else None

    compliance_problems: list[str] = []
    try:
        compliance_cfg = get_compliance_config()
    except ComplianceConfigurationError as err:
        compliance_cfg = None
        compliance_problems = [p.strip() for p in str(err).split(":", 1)[-1].split(";") if p.strip()]

    stored = (await db.execute(text("SELECT key FROM api_keys"))).scalars().all()
    encryption_ok = True
    try:
        credential_crypto._cipher()
    except credential_crypto.CredentialEncryptionError:
        encryption_ok = False

    latest_heartbeat = await db.scalar(select(func.max(WorkerHeartbeat.last_seen)))
    heartbeat_age = None
    if latest_heartbeat is not None:
        heartbeat_age = round((now_utc() - auth._aware(latest_heartbeat)).total_seconds())

    suppressions = (await db.execute(select(EmailSuppression.reason, func.count()).group_by(EmailSuppression.reason))).all()
    users_total = await db.scalar(select(func.count()).select_from(AppUser))
    users_active = await db.scalar(select(func.count()).select_from(AppUser).where(AppUser.is_active.is_(True)))
    admins = await db.scalar(select(func.count()).select_from(AppUser).where(AppUser.is_active.is_(True), AppUser.role == "admin"))

    return {
        "environment": settings.app_env,
        "database": {"schema_revision": revision, "required_revision": SCHEMA_REVISION,
                     "up_to_date": revision == SCHEMA_REVISION},
        "ai": {
            "default_provider": settings.ai_provider,
            "ollama_model": settings.ollama_model,
            "gemini_enabled": settings.ai_gemini_enabled,
            "gemini_model": settings.ai_gemini_model,
            "openai_compatible_enabled": settings.ai_openai_compatible_enabled,
        },
        "enrichment": {"google_places_enabled": settings.google_places_enabled, "pagespeed_enabled": settings.pagespeed_enabled},
        "outreach": {
            "dispatch_enabled": settings.effective_outreach_dispatch_enabled,
            "email_enabled": bool(settings.outreach_email_enabled),
            "email_provider": (settings.outreach_email_provider or "").upper() or None,
            "canary_mode": bool(settings.outreach_canary_mode),
            "canary_allowed_domains": settings.canary_allowed_domain_list,
            "unrestricted_live_sending": settings.is_unrestricted_live_dispatch,
        },
        "compliance": {
            "enforced": bool(settings.outreach_compliance_enforced),
            "configured": compliance_cfg is not None,
            "problems": compliance_problems,
            "unsubscribe_link": bool(compliance_cfg and compliance_cfg.base_url),
            "unsubscribe_mailto": bool(compliance_cfg and compliance_cfg.mailto),
        },
        "credentials": {
            "encryption_key_configured": encryption_ok,
            "stored_keys": len(stored),
            "encrypted_keys": sum(credential_crypto.is_encrypted(v) for v in stored),
        },
        "workers": {"last_heartbeat_seconds_ago": heartbeat_age, "healthy": heartbeat_age is not None and heartbeat_age <= 30},
        "monitoring": {"metrics_enabled": bool((settings.metrics_token or "").strip()), "sentry_enabled": bool((settings.sentry_dsn or "").strip())},
        "suppressions": {str(reason): count for reason, count in suppressions},
        "users": {"total": int(users_total or 0), "active": int(users_active or 0), "active_admins": int(admins or 0)},
        "roles": list(USER_ROLES),
    }


async def _has_table(db: AsyncSession, name: str) -> bool:
    def check(sync_conn):
        from sqlalchemy import inspect
        return inspect(sync_conn).has_table(name)
    conn = await db.connection()
    return await conn.run_sync(check)

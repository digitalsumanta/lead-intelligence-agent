"""Dashboard sign-in: POST /api/auth/login, POST /api/auth/logout, GET /api/auth/me."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.services.auth import (
    AuthError,
    TooManyAttemptsError,
    authenticate,
    create_session,
    login_rate_limiter,
    revoke_session,
)

logger = logging.getLogger("lead_agent.api.auth")

router = APIRouter(prefix="/api/auth", tags=["Authentication"])


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class CurrentUser(BaseModel):
    user_id: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    role: str
    auth_method: str


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"success": False, "error": {"code": code, "message": message, "details": {}}})


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        settings.auth_cookie_name, token,
        max_age=int(settings.auth_session_ttl_hours * 3600),
        httponly=True, secure=settings.effective_auth_cookie_secure, samesite="lax", path="/",
    )


def _clear_cookie(response: Response) -> None:
    response.delete_cookie(settings.auth_cookie_name, path="/", httponly=True,
                           secure=settings.effective_auth_cookie_secure, samesite="lax")


@router.post("/login", response_model=CurrentUser)
async def login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    from app.main import csrf_check_passes  # local import: main imports this router
    if not csrf_check_passes(request):
        return _error(403, "CSRF_CHECK_FAILED", "Request origin could not be verified")
    try:
        login_rate_limiter.check(_client_ip(request))
        user = await authenticate(db, body.email, body.password)
    except TooManyAttemptsError as err:
        return _error(429, err.code, str(err))
    except AuthError as err:
        logger.info("Failed sign-in for %s from %s (%s)", body.email.strip().lower()[:80], _client_ip(request), err.code)
        return _error(401 if err.code == "INVALID_CREDENTIALS" else 423, err.code, str(err))
    token, _ = await create_session(db, user, user_agent=request.headers.get("user-agent"), ip_address=_client_ip(request))
    await db.commit()
    logger.info("User %s signed in from %s", user.email, _client_ip(request))
    response = JSONResponse(CurrentUser(user_id=user.email, email=user.email, display_name=user.display_name,
                                        role=user.role, auth_method="session").model_dump())
    _set_cookie(response, token)
    return response


@router.post("/logout", status_code=204)
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    from app.main import csrf_check_passes
    if not csrf_check_passes(request):
        return _error(403, "CSRF_CHECK_FAILED", "Request origin could not be verified")
    token = request.cookies.get(settings.auth_cookie_name)
    if token:
        await revoke_session(db, token)
        await db.commit()
    response = Response(status_code=204)
    _clear_cookie(response)
    return response


@router.get("/me", response_model=CurrentUser)
async def me(request: Request, db: AsyncSession = Depends(get_db)) -> CurrentUser:
    method = getattr(request.state, "auth_method", "service_token")
    user_id = getattr(request.state, "authenticated_user_id", None) or "unknown"
    role = getattr(request.state, "authenticated_role", None) or "viewer"
    display_name = None
    if method == "session":
        from app.services.auth import get_user_by_email
        user = await get_user_by_email(db, user_id)
        display_name = user.display_name if user else None
    return CurrentUser(user_id=user_id, email=user_id if method == "session" else None,
                       display_name=display_name, role=role, auth_method=method)

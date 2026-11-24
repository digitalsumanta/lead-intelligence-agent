"""FastAPI application: middleware (auth, CSRF, request IDs, metrics), error handlers,
health/readiness/metrics endpoints, and router registration.

Endpoints live in app/api/routes/; shared response helpers in app/api/common.py.
"""
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import (
    SessionLocal,
    engine,
    get_db,
    verify_database_schema,
    worker_is_available,
)
from app.providers import get_provider
from app.services.key_config import sync_environment_keys
from app.services.key_pool import ApiKeyPool
from app.utils.logging_config import configure_logging

configure_logging()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Verify the migrated database schema; Alembic owns all schema changes."""
    await verify_database_schema()
    async with SessionLocal() as session:
        await sync_environment_keys(session)
    yield
    await engine.dispose()

app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)


async def evaluate_provider_readiness(provider, session_factory=SessionLocal) -> tuple[dict, bool]:
    """Check only the selected provider and avoid leasing keys or making generations."""
    status = await provider.health_check()
    ready = bool(status.get("healthy"))
    if provider.name == "ollama":
        ready = ready and status.get("model_available") is True
        status["serviceable"] = ready
        return status, ready
    if provider.name in {"gemini", "openai_compatible"}:
        status["key_pool"] = {"configured": 0, "healthy": 0}
        if ready and status.get("configured"):
            async with session_factory() as session:
                status["key_pool"] = await ApiKeyPool().health(session, provider.name, provider.operation)
        ready = ready and status.get("configured") is True and status["key_pool"]["healthy"] > 0
    status["serviceable"] = ready
    return status, ready

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Session-management endpoints that handle authentication themselves.
_SELF_AUTHENTICATING_API_PATHS = frozenset({"/api/auth/login", "/api/auth/logout"})


def _unauthorized() -> JSONResponse:
    return JSONResponse(status_code=401, content={"success": False, "error": {"code": "UNAUTHORIZED", "message": "Sign in required", "details": {}}})


def csrf_check_passes(request: Request) -> bool:
    """Cookie-authenticated state changes must come from our own dashboard.

    The custom header cannot be sent cross-origin without a CORS preflight, which only the
    configured origins pass; the Origin/Sec-Fetch-Site checks add defense in depth.
    """
    if request.headers.get("x-requested-with", "").strip().lower() != "lead-agent":
        return False
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in {o.rstrip("/") for o in settings.cors_origin_list}:
        return False
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site" and not origin:
        return False
    return True


async def _resolve_cookie_principal(request: Request, token: str):
    """Look up a session cookie. Uses the get_db dependency (honouring test overrides)."""
    from app.services.auth import resolve_session
    provider = request.app.dependency_overrides.get(get_db, get_db)
    generator = provider()
    db = await generator.__anext__()
    try:
        resolved = await resolve_session(db, token)
        if resolved is None:
            return None
        return {"user_id": resolved.user.email, "role": resolved.user.role, "account_id": str(resolved.user.id)}
    finally:
        await generator.aclose()


@app.middleware("http")
async def local_api_auth(request: Request, call_next):
    """Authenticate /api/* with either a dashboard session cookie or the automation bearer token."""
    if request.method == "OPTIONS":
        return await call_next(request)
    path = request.url.path
    if path.startswith("/api/sales-supervisor/webhooks/delivery") or path in _SELF_AUTHENTICATING_API_PATHS:
        return await call_next(request)
    if path.startswith("/api/"):
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            # Service token for scripts and automation (never shipped to browsers).
            token = settings.api_auth_token
            supplied = authorization[7:].strip()
            if not token or not supplied or not hmac.compare_digest(token, supplied):
                return JSONResponse(status_code=401, content={"success": False, "error": {"code": "UNAUTHORIZED", "message": "Valid API bearer token required", "details": {}}})
            request.state.authenticated_user_id = settings.api_auth_user_id.strip() or None
            request.state.authenticated_role = settings.api_auth_role.strip() or "viewer"
            request.state.auth_method = "service_token"
        else:
            cookie = request.cookies.get(settings.auth_cookie_name, "")
            principal = await _resolve_cookie_principal(request, cookie) if cookie else None
            if principal is None:
                return _unauthorized()
            if request.method not in _SAFE_METHODS and not csrf_check_passes(request):
                return JSONResponse(status_code=403, content={"success": False, "error": {"code": "CSRF_CHECK_FAILED", "message": "Request origin could not be verified", "details": {}}})
            request.state.authenticated_user_id = principal["user_id"]
            request.state.authenticated_role = principal["role"]
            request.state.authenticated_account_id = principal["account_id"]
            request.state.auth_method = "session"
    return await call_next(request)

app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True, allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Request-ID", "X-Total-Count"])


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Outermost middleware: request ID for log correlation, plus HTTP metrics."""
    from app import observability as obs
    request_id = obs.resolve_request_id(request.headers.get("x-request-id"))
    request.state.request_id = request_id
    token = obs.request_id_var.set(request_id)
    started = obs.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        route = obs.route_label(request.scope)
        obs.HTTP_UNHANDLED_ERRORS.labels(route).inc()
        obs.observe_request(request.method, route, 500, obs.monotonic() - started)
        raise
    finally:
        obs.request_id_var.reset(token)
    obs.observe_request(request.method, obs.route_label(request.scope), response.status_code, obs.monotonic() - started)
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request, db: AsyncSession = Depends(get_db)):
    """Prometheus metrics. Disabled (404) unless METRICS_TOKEN is set; scrapers send it as a bearer token."""
    from app import observability as obs
    if not (settings.metrics_token or "").strip():
        return JSONResponse(status_code=404, content={"success": False, "error": {"code": "NOT_FOUND", "message": "Not found", "details": {}}})
    if not obs.metrics_authorized(request.headers.get("authorization")):
        return JSONResponse(status_code=401, content={"success": False, "error": {"code": "UNAUTHORIZED", "message": "Valid metrics token required", "details": {}}})
    await obs.collect_database_metrics(db)
    body, content_type = obs.render_metrics()
    return Response(content=body, media_type=content_type)

@app.exception_handler(Exception)
async def global_error_handler(request, exc):
    """Return a stable JSON error envelope."""
    request_id = getattr(request.state, "request_id", None)
    logging.getLogger("lead_agent").exception("request failed", extra={"request_id": request_id})
    from fastapi.responses import JSONResponse
    details = {"request_id": request_id} if request_id else {}
    headers = {"X-Request-ID": request_id} if request_id else None
    return JSONResponse(status_code=500, headers=headers, content={"success": False, "error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred", "details": details}})

@app.exception_handler(HTTPException)
async def http_error_handler(request, exc: HTTPException):
    """Wrap route errors in the consistent error contract."""
    from fastapi.responses import JSONResponse
    detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    code = detail.get("code", "HTTP_ERROR")
    message = detail.get("message", "Request failed")
    return JSONResponse(status_code=exc.status_code, content={"success": False, "error": {"code": code, "message": message, "details": {k:v for k,v in detail.items() if k not in {"code", "message"}}}})

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc: RequestValidationError):
    """Return field level details for invalid request payloads."""
    from fastapi.responses import JSONResponse
    details = [{"field": ".".join(str(x) for x in error["loc"]), "message": error["msg"]} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"success": False, "error": {"code": "VALIDATION_ERROR", "message": "Request validation failed", "details": {"fields": details}}})

@app.get("/health")
async def health():
    """Basic process liveness check."""
    return {"success": True, "status": "healthy", "service": settings.app_name}

@app.get("/ready")
async def readiness():
    """Report readiness including the database schema and configured provider."""
    try:
        await verify_database_schema()
    except Exception:
        return JSONResponse(status_code=503, content={"success": False, "ready": False, "checks": {"database": "unavailable_or_unmigrated", "provider": "not_checked", "worker": "unavailable"}})
    try:
        provider = get_provider()
        provider_status, provider_ready = await evaluate_provider_readiness(provider, SessionLocal)
    except Exception:
        provider_status = {"healthy": False, "error": "Provider health check failed"}
        provider_ready = False
    try:
        worker_ready = await worker_is_available()
    except Exception:
        worker_ready = False
    ready = provider_ready and worker_ready
    payload = {"success": ready, "ready": ready, "checks": {"database": "healthy", "provider": provider_status, "worker": "healthy" if worker_ready else "unavailable"}}
    return payload if ready else JSONResponse(status_code=503, content=payload)


# ---------------------------------------------------------------------------
# Router registration. Order matters: routes are matched in registration order.
# ---------------------------------------------------------------------------
from app.api.routes import (  # noqa: E402  (routers import app-level helpers defined above)
    admin,
    ai_agents,
    auth,
    business_research,
    company_intelligence,
    contact_intelligence,
    contact_recommendation,
    contact_recommendation_execution,
    email_compliance,
    intelligence_orchestrator,
    jobs,
    leads,
    opportunity_contact_matching,
    outreach,
    sales_crm,
    sales_execution,
    sales_feedback,
    seo,
    supervisor_approval,
    supervisor_canary,
    supervisor_delivery_webhooks,
    supervisor_dispatch,
)

# Core lead, sales and AI endpoints (formerly defined in this module).
for _router in (leads, jobs, sales_crm, outreach, ai_agents, seo, company_intelligence):
    app.include_router(_router.router)

# Feature routers.
for _router in (
    contact_intelligence,
    opportunity_contact_matching,
    contact_recommendation,
    contact_recommendation_execution,
    sales_execution,
    sales_feedback,
    intelligence_orchestrator,
    supervisor_approval,
    supervisor_dispatch,
    supervisor_delivery_webhooks,
    supervisor_canary,
    business_research,
):
    app.include_router(_router.router)
app.include_router(email_compliance.public_router)
app.include_router(email_compliance.router)
app.include_router(auth.router)
app.include_router(admin.router)

# Legacy: also expose these routers' APIRoute objects directly on app.routes (some callers and
# tests inspect app.routes for them). FastAPI matches the included copies first, so this does not
# change request routing; it does duplicate their OpenAPI operation IDs.
for _router in (
    contact_intelligence,
    opportunity_contact_matching,
    contact_recommendation,
    contact_recommendation_execution,
    sales_execution,
    sales_feedback,
    intelligence_orchestrator,
    supervisor_approval,
    supervisor_dispatch,
    supervisor_delivery_webhooks,
    supervisor_canary,
    business_research,
):
    app.routes.extend([route for route in _router.router.routes if route not in app.routes])

"""Dashboard user accounts, cookie sessions, CSRF protection, lockout, and the users CLI."""
import argparse
from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.config import settings
from app.database import get_db
from app.models.entities import AppUser, AuthSession, now_utc
from app.services import auth

PASSWORD = "correct horse battery staple"
CSRF = {"X-Requested-With": "lead-agent"}


# --- passwords ---------------------------------------------------------------------

def test_password_hash_round_trip_and_salting():
    first, second = auth.hash_password(PASSWORD), auth.hash_password(PASSWORD)
    assert first != second and first.startswith("scrypt$")
    assert auth.verify_password(PASSWORD, first)
    assert not auth.verify_password(PASSWORD + "!", first)
    assert not auth.verify_password(PASSWORD, "garbage")


@pytest.mark.parametrize("password", ["short", "aaaaaaaaaaaaaaaa", "owner@example.com"])
def test_weak_passwords_rejected(password):
    with pytest.raises(ValueError):
        auth.validate_password_strength(password, "owner@example.com")


# --- HTTP flow --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def app_client(session_factory, monkeypatch):
    from app.main import app
    monkeypatch.setattr(settings, "api_auth_token", "service-token-for-tests")
    auth.login_rate_limiter.reset()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with session_factory() as db:
        await auth.create_user(db, email="Manager@Example.com", password=PASSWORD, role="sales_manager", display_name="Mia")
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    auth.login_rate_limiter.reset()


async def sign_in(client, email="manager@example.com", password=PASSWORD):
    return await client.post("/api/auth/login", json={"email": email, "password": password}, headers=CSRF)


@pytest.mark.asyncio
async def test_login_sets_httponly_cookie_and_session_authenticates(app_client):
    assert (await app_client.get("/api/auth/me")).status_code == 401
    assert (await app_client.get("/api/leads")).status_code == 401

    res = await sign_in(app_client, email="  MANAGER@example.com ")
    assert res.status_code == 200 and res.json()["role"] == "sales_manager"
    cookie_header = res.headers["set-cookie"].lower()
    assert "httponly" in cookie_header and "samesite=lax" in cookie_header

    me = (await app_client.get("/api/auth/me")).json()
    assert me == {"user_id": "manager@example.com", "email": "manager@example.com", "display_name": "Mia",
                  "role": "sales_manager", "auth_method": "session"}
    assert (await app_client.get("/api/leads")).status_code == 200


@pytest.mark.asyncio
async def test_session_token_is_stored_only_as_hash(app_client, session_factory):
    res = await sign_in(app_client)
    token = res.cookies.get(settings.auth_cookie_name)
    async with session_factory() as db:
        row = (await db.scalars(select(AuthSession))).one()
    assert token and row.token_hash != token and len(row.token_hash) == 64


@pytest.mark.asyncio
async def test_cookie_requests_that_change_state_require_csrf_header(app_client):
    await sign_in(app_client)
    # Missing custom header -> rejected before reaching the route.
    assert (await app_client.post("/api/email-suppressions", json={"email": "a@b.example"})).status_code == 403
    # Foreign origin -> rejected even with the header.
    evil = {**CSRF, "Origin": "https://evil.example"}
    assert (await app_client.post("/api/email-suppressions", json={"email": "a@b.example"}, headers=evil)).status_code == 403
    ok = {**CSRF, "Origin": settings.cors_origin_list[0]}
    assert (await app_client.post("/api/email-suppressions", json={"email": "a@b.example"}, headers=ok)).status_code == 201


@pytest.mark.asyncio
async def test_login_itself_requires_csrf_header(app_client):
    res = await app_client.post("/api/auth/login", json={"email": "manager@example.com", "password": PASSWORD})
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_logout_revokes_session(app_client):
    await sign_in(app_client)
    token = app_client.cookies.get(settings.auth_cookie_name)
    assert (await app_client.post("/api/auth/logout", headers=CSRF)).status_code == 204
    app_client.cookies.set(settings.auth_cookie_name, token)  # replaying the old cookie must fail
    assert (await app_client.get("/api/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_wrong_password_and_unknown_user_give_same_error(app_client):
    wrong = await sign_in(app_client, password="not the password")
    unknown = await sign_in(app_client, email="nobody@example.com")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"] == unknown.json()["error"]


@pytest.mark.asyncio
async def test_account_locks_after_repeated_failures(app_client, monkeypatch):
    monkeypatch.setattr(settings, "auth_max_failed_logins", 3)
    for _ in range(3):
        assert (await sign_in(app_client, password="wrong password!")).status_code == 401
    locked = await sign_in(app_client)  # even the right password is refused while locked
    assert locked.status_code == 423 and locked.json()["error"]["code"] == "ACCOUNT_LOCKED"


@pytest.mark.asyncio
async def test_ip_rate_limit(app_client, monkeypatch):
    monkeypatch.setattr(auth.login_rate_limiter, "max_attempts", 2)
    await sign_in(app_client, email="x@example.com")
    await sign_in(app_client, email="y@example.com")
    res = await sign_in(app_client, email="z@example.com")
    assert res.status_code == 429


@pytest.mark.asyncio
async def test_deactivation_and_password_change_end_existing_sessions(app_client, session_factory):
    await sign_in(app_client)
    async with session_factory() as db:
        user = await auth.get_user_by_email(db, "manager@example.com")
        await auth.set_password(db, user, "a brand new long password")
        await db.commit()
    assert (await app_client.get("/api/auth/me")).status_code == 401
    assert (await sign_in(app_client, password="a brand new long password")).status_code == 200

    async with session_factory() as db:
        user = await auth.get_user_by_email(db, "manager@example.com")
        user.is_active = False
        await db.commit()
    assert (await app_client.get("/api/auth/me")).status_code == 401
    assert (await sign_in(app_client, password="a brand new long password")).status_code == 401


@pytest.mark.asyncio
async def test_expired_session_rejected(app_client, session_factory):
    await sign_in(app_client)
    async with session_factory() as db:
        row = (await db.scalars(select(AuthSession))).one()
        row.expires_at = now_utc() - timedelta(minutes=1)
        await db.commit()
    assert (await app_client.get("/api/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_service_token_still_works_for_automation(app_client):
    res = await app_client.get("/api/auth/me", headers={"Authorization": "Bearer service-token-for-tests"})
    assert res.status_code == 200 and res.json()["auth_method"] == "service_token"
    bad = await app_client.get("/api/leads", headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 401


@pytest.mark.asyncio
async def test_session_role_is_trusted_and_header_override_ignored(app_client, session_factory):
    """A viewer cannot claim an approver role via X-Actor-Role."""
    from fastapi import Request
    from app.api.routes.supervisor_approval import get_authenticated_principal
    async with session_factory() as db:
        await auth.create_user(db, email="viewer@example.com", password=PASSWORD, role="viewer")
        await db.commit()
    await sign_in(app_client, email="viewer@example.com")

    scope = {"type": "http", "headers": [(b"x-actor-role", b"admin")], "method": "GET", "path": "/"}
    request = Request(scope)
    request.state.authenticated_user_id = "viewer@example.com"
    request.state.authenticated_role = "viewer"
    assert get_authenticated_principal(request).role == "viewer"


# --- CLI ------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cli_create_list_set_role_and_deactivate(session_factory, monkeypatch, capsys):
    from app import cli
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    args = cli.build_parser().parse_args(["users", "create", "--email", "Owner@Example.com", "--role", "admin", "--password-stdin"])
    assert await cli._run(args, session_factory) == 0

    assert await cli._run(cli.build_parser().parse_args(["users", "set-role", "--email", "owner@example.com", "--role", "sales_rep"]), session_factory) == 0
    assert await cli._run(cli.build_parser().parse_args(["users", "deactivate", "--email", "owner@example.com"]), session_factory) == 0
    assert await cli._run(cli.build_parser().parse_args(["users", "list"]), session_factory) == 0
    out = capsys.readouterr().out
    assert "owner@example.com" in out and "sales_rep" in out and "deactivated" in out

    async with session_factory() as db:
        user = (await db.scalars(select(AppUser))).one()
        assert user.role == "sales_rep" and not user.is_active and user.password_hash.startswith("scrypt$")

    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    weak = cli.build_parser().parse_args(["users", "create", "--email", "two@example.com", "--password-stdin"])
    assert await cli._run(weak, session_factory) == 1

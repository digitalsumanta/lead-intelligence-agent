"""Admin user management, self-service password change, and system status."""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.database import get_db
from app.services import auth

PW = "admin long password 1"
CSRF = {"X-Requested-With": "lead-agent"}


@pytest_asyncio.fixture
async def setup(session_factory):
    from app.main import app
    auth.login_rate_limiter.reset()

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with session_factory() as db:
        admin = await auth.create_user(db, email="admin@example.com", password=PW, role="admin")
        rep = await auth.create_user(db, email="rep@example.com", password=PW, role="sales_rep")
        mgr = await auth.create_user(db, email="mgr@example.com", password=PW, role="sales_manager")
        await db.commit()
        ids = {"admin": str(admin.id), "rep": str(rep.id), "mgr": str(mgr.id)}

    def client():
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    yield client, ids
    auth.login_rate_limiter.reset()


async def signed_in(client, email):
    res = await client.post("/api/auth/login", json={"email": email, "password": PW}, headers=CSRF)
    assert res.status_code == 200
    return client


@pytest.mark.asyncio
async def test_only_admins_manage_users(setup):
    make, _ = setup
    async with make() as c:
        await signed_in(c, "rep@example.com")
        assert (await c.get("/api/admin/users")).status_code == 403
        assert (await c.get("/api/admin/system-status")).status_code == 403
    async with make() as c:
        await signed_in(c, "mgr@example.com")
        assert (await c.get("/api/admin/users")).status_code == 403
        assert (await c.get("/api/admin/system-status")).status_code == 200  # managers can read status
    async with make() as c:
        await signed_in(c, "admin@example.com")
        users = (await c.get("/api/admin/users")).json()
        assert [u["email"] for u in users] == ["admin@example.com", "mgr@example.com", "rep@example.com"]
        assert all("password" not in key for u in users for key in u)


@pytest.mark.asyncio
async def test_admin_creates_updates_and_deactivates_users(setup):
    make, ids = setup
    async with make() as c:
        await signed_in(c, "admin@example.com")
        created = await c.post("/api/admin/users", headers=CSRF, json={
            "email": "New.Person@Example.com", "display_name": "New Person", "role": "viewer", "password": "brand new password"})
        assert created.status_code == 201 and created.json()["email"] == "new.person@example.com"
        weak = await c.post("/api/admin/users", headers=CSRF, json={"email": "w@example.com", "role": "viewer", "password": "short"})
        assert weak.status_code == 400
        dup = await c.post("/api/admin/users", headers=CSRF, json={"email": "rep@example.com", "role": "viewer", "password": "brand new password"})
        assert dup.status_code == 400

        uid = created.json()["id"]
        res = await c.patch(f"/api/admin/users/{uid}", headers=CSRF, json={"role": "sales_manager", "display_name": "Renamed"})
        assert res.json()["role"] == "sales_manager" and res.json()["display_name"] == "Renamed"
        bad_role = await c.patch(f"/api/admin/users/{uid}", headers=CSRF, json={"role": "root"})
        assert bad_role.status_code == 422

    # Deactivation ends the user's sessions.
    async with make() as rep:
        await signed_in(rep, "rep@example.com")
        async with make() as c:
            await signed_in(c, "admin@example.com")
            res = await c.patch(f"/api/admin/users/{ids['rep']}", headers=CSRF, json={"is_active": False})
            assert res.json()["is_active"] is False
        assert (await rep.get("/api/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_last_admin_and_self_deactivation_guards(setup):
    make, ids = setup
    async with make() as c:
        await signed_in(c, "admin@example.com")
        self_off = await c.patch(f"/api/admin/users/{ids['admin']}", headers=CSRF, json={"is_active": False})
        assert self_off.status_code == 400 and self_off.json()["error"]["code"] == "CANNOT_DEACTIVATE_SELF"
        demote = await c.patch(f"/api/admin/users/{ids['admin']}", headers=CSRF, json={"role": "viewer"})
        assert demote.status_code == 400 and demote.json()["error"]["code"] == "LAST_ADMIN"
        # With a second active admin, demotion is allowed.
        await c.patch(f"/api/admin/users/{ids['mgr']}", headers=CSRF, json={"role": "admin"})
        assert (await c.patch(f"/api/admin/users/{ids['admin']}", headers=CSRF, json={"role": "viewer"})).status_code == 200


@pytest.mark.asyncio
async def test_admin_reset_password_and_sign_out(setup):
    make, ids = setup
    async with make() as rep:
        await signed_in(rep, "rep@example.com")
        async with make() as c:
            await signed_in(c, "admin@example.com")
            res = await c.post(f"/api/admin/users/{ids['rep']}/sign-out", headers=CSRF)
            assert res.json()["active_sessions"] == 0
        assert (await rep.get("/api/auth/me")).status_code == 401

    async with make() as c:
        await signed_in(c, "admin@example.com")
        res = await c.post(f"/api/admin/users/{ids['rep']}/reset-password", headers=CSRF, json={"password": "admin chosen pass"})
        assert res.status_code == 200
    async with make() as rep:
        ok = await rep.post("/api/auth/login", json={"email": "rep@example.com", "password": "admin chosen pass"}, headers=CSRF)
        assert ok.status_code == 200


@pytest.mark.asyncio
async def test_admin_unlocks_locked_account(setup, monkeypatch):
    make, ids = setup
    monkeypatch.setattr(settings, "auth_max_failed_logins", 2)
    async with make() as c:
        for _ in range(2):
            await c.post("/api/auth/login", json={"email": "rep@example.com", "password": "wrong password!"}, headers=CSRF)
        assert (await c.post("/api/auth/login", json={"email": "rep@example.com", "password": PW}, headers=CSRF)).status_code == 423
    async with make() as c:
        await signed_in(c, "admin@example.com")
        users = {u["email"]: u for u in (await c.get("/api/admin/users")).json()}
        assert users["rep@example.com"]["locked"] is True
        res = await c.patch(f"/api/admin/users/{ids['rep']}", headers=CSRF, json={"is_active": True})
        assert res.json()["locked"] is False
    async with make() as c:
        assert (await c.post("/api/auth/login", json={"email": "rep@example.com", "password": PW}, headers=CSRF)).status_code == 200


@pytest.mark.asyncio
async def test_change_own_password_keeps_this_session_and_ends_others(setup):
    make, _ = setup
    async with make() as other:
        await signed_in(other, "rep@example.com")
        async with make() as me:
            await signed_in(me, "rep@example.com")
            wrong = await me.post("/api/auth/change-password", headers=CSRF,
                                  json={"current_password": "nope", "new_password": "another long password"})
            assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "INVALID_CURRENT_PASSWORD"
            ok = await me.post("/api/auth/change-password", headers=CSRF,
                               json={"current_password": PW, "new_password": "another long password"})
            assert ok.status_code == 200
            assert (await me.get("/api/auth/me")).status_code == 200      # this browser stays signed in
        assert (await other.get("/api/auth/me")).status_code == 401       # other sessions end


@pytest.mark.asyncio
async def test_system_status_reports_configuration_without_secrets(setup, monkeypatch):
    make, _ = setup
    monkeypatch.setattr(settings, "outreach_sender_postal_address", "")
    async with make() as c:
        await signed_in(c, "admin@example.com")
        body = (await c.get("/api/admin/system-status")).json()
    assert body["users"] == {"total": 3, "active": 3, "active_admins": 1}
    assert body["compliance"]["configured"] is False
    assert any("OUTREACH_SENDER_POSTAL_ADDRESS" in p for p in body["compliance"]["problems"])
    assert body["credentials"]["encryption_key_configured"] is True
    text = str(body)
    for secret in (settings.credential_encryption_key, settings.outreach_unsubscribe_secret):
        assert secret and secret not in text

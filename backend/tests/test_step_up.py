"""Step-up re-authentication (ADR-094 decision 9, plan PR 4b).

Dangerous admin actions (self-update start/rollback, later vault export and
user deletion) need a *fresh* password confirmation on top of the login:
``POST /api/v1/auth/step-up`` with the password mints a step-up token that is
valid for 5 minutes and consumed by the first protected request
(``require_recent_auth()``, header ``X-MC-Step-Up``).

Refused:
* callers authenticated with ``LOCAL_AUTH_TOKEN`` (or the host-side MCP
  service token) — they act as "the first admin" without anybody typing a
  password, so there is nobody to re-authenticate;
* accounts without a password (v1: no button, the UI points to the terminal);
* the HttpOnly session cookie or a ``?token=`` query JWT — the step-up must
  come from the logged-in UI tab, which always sends the Authorization header.

A missing/spent/expired step-up answers **403** ``step_up_required``, never
401: the UI's request() wrapper treats every 401 outside /auth/ as "session
expired" and signs the operator out (frontend-v2/src/lib/api.ts).
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlmodel.ext.asyncio.session import AsyncSession

import app.config
from app.auth import JWT_ALGORITHM, create_access_token, hash_password
from app.models.user import User
from app.services import step_up
from tests.conftest import test_engine

PASSWORD = "correct horse battery"
ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTHER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
NOPW_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a3")
LOCAL_TOKEN = "local-token-for-step-up-tests-0123456789"

# bcrypt(12) is slow; hash once per module.
_PW_HASH = hash_password(PASSWORD)


@pytest.fixture(autouse=True)
def _reset_login_rate_limit():
    from app.routers import auth as auth_router

    auth_router._login_attempts.clear()
    yield
    auth_router._login_attempts.clear()


async def _add_user(user_id: uuid.UUID, *, email: str, password: bool = True, role: str = "admin") -> None:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(
            User(
                id=user_id,
                email=email,
                name=email.split("@")[0],
                role=role,
                is_active=True,
                password_hash=_PW_HASH if password else None,
            )
        )
        await s.commit()


def _bearer(user_id: uuid.UUID, role: str = "admin", tv: int = 0) -> dict:
    return {"Authorization": f"Bearer {create_access_token(str(user_id), role, tv)}"}


@pytest.fixture
async def users(client):
    await _add_user(ADMIN_ID, email="admin@step.up")
    await _add_user(OTHER_ID, email="other@step.up")
    await _add_user(NOPW_ID, email="nopw@step.up", password=False)
    return client


@pytest.fixture
async def probe(client):
    """A tiny app with one route behind require_recent_auth() — the real
    consumers (self-update API) arrive in a later PR. It shares the main
    app's dependency overrides (test DB, fake Redis)."""
    from app.main import app as fastapi_app

    probe_app = FastAPI()
    probe_app.dependency_overrides = fastapi_app.dependency_overrides

    @probe_app.post("/api/v1/probe/dangerous")
    async def dangerous(user: User = Depends(step_up.require_recent_auth())):
        return {"ok": True, "user": str(user.id)}

    async with AsyncClient(transport=ASGITransport(app=probe_app), base_url="http://test") as ac:
        yield ac


async def _step_up(client, headers: dict, password: str = PASSWORD):
    return await client.post("/api/v1/auth/step-up", json={"password": password}, headers=headers)


async def _mint(client, user_id: uuid.UUID = ADMIN_ID) -> str:
    r = await _step_up(client, _bearer(user_id))
    assert r.status_code == 200, r.text
    return r.json()["step_up_token"]


def _code(r) -> str | None:
    detail = r.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


# ── Minting ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_up_with_password_returns_opaque_short_lived_token(users, fake_redis):
    r = await _step_up(users, _bearer(ADMIN_ID))
    assert r.status_code == 200, r.text
    body = r.json()
    token = body["step_up_token"]
    assert len(token) >= 32 and "." not in token, "opaque random token, not a JWT"
    assert body["expires_in"] == 300 == step_up.STEP_UP_TTL_SECONDS

    keys = [k async for k in fake_redis.scan_iter("mc:step-up:*")]
    assert len(keys) == 1
    assert token not in keys[0], "only the hash may be the Redis key"
    ttl = await fake_redis.ttl(keys[0])
    assert 0 < ttl <= 300


@pytest.mark.asyncio
async def test_wrong_password_is_401_and_mints_nothing(users, fake_redis):
    r = await _step_up(users, _bearer(ADMIN_ID), password="nope")
    assert r.status_code == 401
    assert _code(r) == "step_up_wrong_password"
    assert [k async for k in fake_redis.scan_iter("mc:step-up:*")] == []


@pytest.mark.asyncio
async def test_step_up_needs_a_login(users):
    r = await users.post("/api/v1/auth/step-up", json={"password": PASSWORD})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_step_up_rate_limit_after_five_failures(users):
    for _ in range(5):
        assert (await _step_up(users, _bearer(ADMIN_ID), password="nope")).status_code == 401
    # Even the right password is refused now — same brake as /login.
    r = await _step_up(users, _bearer(ADMIN_ID))
    assert r.status_code == 429


@pytest.mark.asyncio
async def test_step_up_shares_the_login_brake(users):
    """Failed logins and failed step-ups count against the same per-IP
    budget: an attacker cannot get 5 guesses at each door."""
    for _ in range(5):
        r = await users.post(
            "/api/v1/auth/login", json={"email": "admin@step.up", "password": "nope"}
        )
        assert r.status_code == 401
    assert (await _step_up(users, _bearer(ADMIN_ID))).status_code == 429


@pytest.mark.asyncio
async def test_step_up_brake_is_also_per_account(users):
    """A stolen session guessing from changing IPs is still braked."""
    from app.routers import auth as auth_router

    for _ in range(5):
        assert (await _step_up(users, _bearer(ADMIN_ID), password="nope")).status_code == 401
        # Forget the per-IP bucket — only the per-account one remains.
        for key in [k for k in auth_router._login_attempts if not k.startswith("user:")]:
            auth_router._login_attempts.pop(key)
    assert (await _step_up(users, _bearer(ADMIN_ID))).status_code == 429


@pytest.mark.asyncio
async def test_account_without_password_is_refused(users, fake_redis):
    r = await _step_up(users, _bearer(NOPW_ID))
    assert r.status_code == 403
    assert _code(r) == "step_up_no_password"
    assert [k async for k in fake_redis.scan_iter("mc:step-up:*")] == []


@pytest.mark.asyncio
async def test_local_auth_token_admin_is_refused_even_with_the_admins_password(users, monkeypatch):
    """LOCAL_AUTH_TOKEN resolves to the first admin account — the same User
    row a real login gets. Knowing that admin's password must still not
    turn the shared token into a step-up."""
    monkeypatch.setattr(app.config.settings, "local_auth_token", LOCAL_TOKEN)
    r = await _step_up(users, {"Authorization": f"Bearer {LOCAL_TOKEN}"})
    assert r.status_code == 403
    assert _code(r) == "step_up_local_token"


@pytest.mark.asyncio
async def test_mcp_service_token_is_refused(users):
    """scripts/mc-mcp.py self-signs a JWT with sub='mcp-server' that also
    acts as the first admin — no human typed a password for it."""
    token = jwt.encode(
        {"sub": "mcp-server", "role": "admin"},
        app.config.settings.jwt_secret_key,
        algorithm=JWT_ALGORITHM,
    )
    r = await _step_up(users, {"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    assert _code(r) == "step_up_local_token"


@pytest.mark.asyncio
async def test_cookie_or_query_session_is_refused(users, monkeypatch):
    token = create_access_token(str(ADMIN_ID), "admin")
    users.cookies.set("mc_sse_token", token)
    r = await users.post("/api/v1/auth/step-up", json={"password": PASSWORD})
    assert r.status_code == 403
    assert _code(r) == "step_up_header_required"
    users.cookies.clear()

    monkeypatch.setattr(app.config.settings, "allow_query_token_auth", True)
    r = await users.post(f"/api/v1/auth/step-up?token={token}", json={"password": PASSWORD})
    assert r.status_code == 403
    assert _code(r) == "step_up_header_required"


@pytest.mark.asyncio
async def test_redis_down_on_mint_is_503(users, monkeypatch):
    async def broken_redis():
        raise ConnectionError("redis down")

    monkeypatch.setattr(step_up, "get_redis", broken_redis)
    r = await _step_up(users, _bearer(ADMIN_ID))
    assert r.status_code == 503


# ── Availability (the UI hides the button when step-up is impossible) ──────


@pytest.mark.asyncio
async def test_availability_reports_why_step_up_is_impossible(users, monkeypatch):
    r = await users.get("/api/v1/auth/step-up", headers=_bearer(ADMIN_ID))
    assert r.json() == {"available": True, "reason": None, "ttl_seconds": 300}

    r = await users.get("/api/v1/auth/step-up", headers=_bearer(NOPW_ID))
    assert r.json()["available"] is False
    assert r.json()["reason"] == "step_up_no_password"

    monkeypatch.setattr(app.config.settings, "local_auth_token", LOCAL_TOKEN)
    r = await users.get("/api/v1/auth/step-up", headers={"Authorization": f"Bearer {LOCAL_TOKEN}"})
    assert r.json()["reason"] == "step_up_local_token"


# ── require_recent_auth() ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_protected_route_without_step_up_is_403_not_401(users, probe):
    r = await probe.post("/api/v1/probe/dangerous", headers=_bearer(ADMIN_ID))
    assert r.status_code == 403, "401 would sign the operator out in the UI"
    assert _code(r) == "step_up_required"


@pytest.mark.asyncio
async def test_protected_route_without_login_is_401(users, probe):
    token = await _mint(users)
    r = await probe.post("/api/v1/probe/dangerous", headers={"X-MC-Step-Up": token})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_step_up_token_opens_the_route_once(users, probe):
    token = await _mint(users)
    headers = {**_bearer(ADMIN_ID), "X-MC-Step-Up": token}
    r = await probe.post("/api/v1/probe/dangerous", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["user"] == str(ADMIN_ID)
    # Sabotage probe (a): the second use must fail.
    r = await probe.post("/api/v1/probe/dangerous", headers=headers)
    assert r.status_code == 403
    assert _code(r) == "step_up_required"


@pytest.mark.asyncio
async def test_step_up_token_is_bound_to_its_user_and_burnt_on_misuse(users, probe):
    token = await _mint(users, ADMIN_ID)
    r = await probe.post(
        "/api/v1/probe/dangerous", headers={**_bearer(OTHER_ID), "X-MC-Step-Up": token}
    )
    assert r.status_code == 403
    # Burnt: the rightful owner cannot use it afterwards either.
    r = await probe.post(
        "/api/v1/probe/dangerous", headers={**_bearer(ADMIN_ID), "X-MC-Step-Up": token}
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_step_up_dies_with_logout(users, probe):
    token = await _mint(users)
    assert (await users.post("/api/v1/auth/logout", headers=_bearer(ADMIN_ID))).status_code == 200
    # New login (token_version 1) — the old step-up belongs to the old session.
    r = await probe.post(
        "/api/v1/probe/dangerous", headers={**_bearer(ADMIN_ID, tv=1), "X-MC-Step-Up": token}
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_expired_step_up_is_refused(users, probe, fake_redis):
    token = await _mint(users)
    for key in [k async for k in fake_redis.scan_iter("mc:step-up:*")]:
        await fake_redis.delete(key)  # what the 300 s TTL does
    r = await probe.post(
        "/api/v1/probe/dangerous", headers={**_bearer(ADMIN_ID), "X-MC-Step-Up": token}
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_local_token_cannot_spend_a_real_step_up(users, probe, monkeypatch):
    """The first admin minted a step-up in the UI; a script holding
    LOCAL_AUTH_TOKEN (which resolves to the same admin row) must not be
    able to spend it."""
    token = await _mint(users, ADMIN_ID)
    monkeypatch.setattr(app.config.settings, "local_auth_token", LOCAL_TOKEN)
    r = await probe.post(
        "/api/v1/probe/dangerous",
        headers={"Authorization": f"Bearer {LOCAL_TOKEN}", "X-MC-Step-Up": token},
    )
    assert r.status_code == 403
    assert _code(r) == "step_up_local_token"


@pytest.mark.asyncio
async def test_password_removed_after_step_up_blocks_the_route(users, probe):
    """has_password is checked again on use, not only at mint time."""
    token = await _mint(users, ADMIN_ID)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        user = await s.get(User, ADMIN_ID)
        user.password_hash = None
        s.add(user)
        await s.commit()
    r = await probe.post(
        "/api/v1/probe/dangerous", headers={**_bearer(ADMIN_ID), "X-MC-Step-Up": token}
    )
    assert r.status_code == 403
    assert _code(r) == "step_up_no_password"


@pytest.mark.asyncio
async def test_redis_down_on_use_is_503_not_a_pass(users, probe, monkeypatch):
    token = await _mint(users)

    async def broken_redis():
        raise ConnectionError("redis down")

    monkeypatch.setattr(step_up, "get_redis", broken_redis)
    r = await probe.post(
        "/api/v1/probe/dangerous", headers={**_bearer(ADMIN_ID), "X-MC-Step-Up": token}
    )
    assert r.status_code == 503

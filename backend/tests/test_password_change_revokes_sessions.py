"""Changing a password signs out every other session.

A password change is the one thing a user does when they think someone else
has their login (a lost phone, a shared laptop). Before this fix the old JWTs
stayed valid until they expired — the thief kept access. Now:

- PATCH /api/v1/auth/me with a new password bumps ``token_version`` → every
  token issued before is refused (401), on every device;
- the calling session is NOT thrown out: the response carries a fresh
  ``access_token`` (and re-sets the ``mc_sse_token`` cookie) that the
  frontend stores right away;
- an admin setting another user's password signs that user out everywhere.
"""

import uuid

import pytest

from app.auth import hash_password

OLD_PW = "correct-horse"
NEW_PW = "battery-staple"


async def _make_user(role: str = "admin") -> dict:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.models.user import User
    from tests.conftest import test_engine

    email = f"pw-change-{uuid.uuid4().hex[:8]}@mc.local"
    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(
            User(
                id=uid,
                email=email,
                name="Password Changer",
                password_hash=hash_password(OLD_PW),
                role=role,
                is_active=True,
            )
        )
        await s.commit()
    return {"id": str(uid), "email": email}


async def _login(client, email: str, password: str) -> str:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _me(client, token: str) -> int:
    # Clear the cookie jar so only the Bearer header authenticates.
    client.cookies.clear()
    resp = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    return resp.status_code


@pytest.mark.asyncio
async def test_password_change_revokes_other_sessions_and_renews_own(client):
    user = await _make_user()
    this_device = await _login(client, user["email"], OLD_PW)
    other_device = await _login(client, user["email"], OLD_PW)
    assert await _me(client, this_device) == 200
    assert await _me(client, other_device) == 200

    client.cookies.clear()
    resp = await client.patch(
        "/api/v1/auth/me",
        json={"current_password": OLD_PW, "new_password": NEW_PW},
        headers={"Authorization": f"Bearer {this_device}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    new_token = body.get("access_token")
    assert new_token, f"password change must hand back a fresh token: {body}"
    # The profile fields are still there (frontend reads them as before).
    assert body["email"] == user["email"]

    # Old tokens — on this device and the other one — are dead.
    assert await _me(client, other_device) == 401
    assert await _me(client, this_device) == 401
    # The fresh token works.
    assert await _me(client, new_token) == 200

    # The cookie session credential is renewed too (it is a full session
    # credential for require_user, see test_sse_cookie_secure_flag.py).
    cookies = [
        v for k, v in resp.headers.multi_items()
        if k.lower() == "set-cookie" and v.startswith("mc_sse_token=")
    ]
    assert cookies and cookies[0].startswith(f"mc_sse_token={new_token}")

    # And the new password is the one that logs in.
    assert await _login(client, user["email"], NEW_PW)


@pytest.mark.asyncio
async def test_profile_update_without_password_keeps_sessions(client):
    user = await _make_user()
    token = await _login(client, user["email"], OLD_PW)

    client.cookies.clear()
    resp = await client.patch(
        "/api/v1/auth/me",
        json={"name": "New Name"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert "access_token" not in resp.json()
    assert await _me(client, token) == 200


@pytest.mark.asyncio
async def test_wrong_current_password_signs_nobody_out(client):
    user = await _make_user()
    token = await _login(client, user["email"], OLD_PW)

    client.cookies.clear()
    resp = await client.patch(
        "/api/v1/auth/me",
        json={"current_password": "wrong-one", "new_password": NEW_PW},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert await _me(client, token) == 200


@pytest.mark.asyncio
async def test_admin_password_reset_signs_target_user_out(client):
    admin = await _make_user("admin")
    target = await _make_user("operator")
    admin_token = await _login(client, admin["email"], OLD_PW)
    target_token = await _login(client, target["email"], OLD_PW)

    client.cookies.clear()
    resp = await client.patch(
        f"/api/v1/auth/users/{target['id']}",
        json={"password": NEW_PW},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert await _me(client, target_token) == 401
    # The admin's own session is untouched.
    assert await _me(client, admin_token) == 200


@pytest.mark.asyncio
async def test_admin_resetting_own_password_keeps_this_session(client):
    admin = await _make_user("admin")
    this_device = await _login(client, admin["email"], OLD_PW)
    other_device = await _login(client, admin["email"], OLD_PW)

    client.cookies.clear()
    resp = await client.patch(
        f"/api/v1/auth/users/{admin['id']}",
        json={"password": NEW_PW},
        headers={"Authorization": f"Bearer {this_device}"},
    )
    assert resp.status_code == 200, resp.text
    new_token = resp.json().get("access_token")
    assert new_token
    assert await _me(client, other_device) == 401
    assert await _me(client, new_token) == 200

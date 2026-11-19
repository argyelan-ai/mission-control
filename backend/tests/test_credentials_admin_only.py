"""The credentials Vault is admin-only, and masks never leak a password.

Round-3 user tests (R1, 23.10.2026) found two holes:

1. Every ``/api/v1/credentials`` route (list, get, create, update, delete)
   only checked "logged in". A viewer account could read, create, change
   and delete the logins and tokens agents use. ``GET /api/v1/secrets``
   (the masked API-key list) was open to viewers as well.
2. ``encryption.mask()`` showed the last 4 characters and the exact length
   of every secret field — including passwords. A 5-character password
   showed 4 of its 5 characters.

Each role test proves both halves of the gate: the refused roles get 403,
and an admin gets past the gate (to whatever the endpoint does next).
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import create_access_token
import app.services.encryption as enc
from tests.conftest import test_engine

ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
OPERATOR_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c2")
VIEWER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c3")
USERS = {"admin": ADMIN_ID, "operator": OPERATOR_ID, "viewer": VIEWER_ID}

UNKNOWN = "00000000-0000-0000-0000-00000000dead"  # no such credential

CREDENTIAL_ROUTES = [
    ("GET", "/api/v1/credentials", None),
    ("GET", f"/api/v1/credentials/{UNKNOWN}", None),
    (
        "POST",
        "/api/v1/credentials",
        {"name": "x", "credential_type": "token", "data": {"token": "t"}},
    ),
    ("PATCH", f"/api/v1/credentials/{UNKNOWN}", {"name": "y"}),
    ("DELETE", f"/api/v1/credentials/{UNKNOWN}", None),
]


async def _seed_users() -> None:
    from app.models.user import User

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for role, uid in USERS.items():
            if await s.get(User, uid) is None:
                s.add(User(id=uid, email=f"{role}-r1@mc.local", name=role, role=role, is_active=True))
        await s.commit()


async def _call(client: AsyncClient, role: str, method: str, path: str, body):
    await _seed_users()
    token = create_access_token(str(USERS[role]), role)
    return await client.request(
        method, path, json=body, headers={"Authorization": f"Bearer {token}"}
    )


# ── Credentials: admin only ──────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "operator"])
@pytest.mark.parametrize("method,path,body", CREDENTIAL_ROUTES)
async def test_credentials_refuse_non_admin(client: AsyncClient, role, method, path, body):
    resp = await _call(client, role, method, path, body)
    assert resp.status_code == 403, (role, method, path, resp.status_code, resp.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", CREDENTIAL_ROUTES)
async def test_credentials_let_admin_through(client: AsyncClient, method, path, body):
    resp = await _call(client, "admin", method, path, body)
    # Past the gate: list 200, create 201, unknown id 404.
    assert resp.status_code in (200, 201, 404), (method, path, resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_viewer_cannot_see_existing_credential(client: AsyncClient):
    """End to end: an admin stores a login, a viewer gets nothing back."""
    created = await _call(
        client,
        "admin",
        "POST",
        "/api/v1/credentials",
        {
            "name": "Router",
            "credential_type": "login",
            "data": {"username": "mark", "password": "hunter2"},
            "url": "https://router.local",
        },
    )
    assert created.status_code == 201
    cred_id = created.json()["id"]

    listed = await _call(client, "viewer", "GET", "/api/v1/credentials", None)
    assert listed.status_code == 403
    assert "Router" not in listed.text
    one = await _call(client, "viewer", "GET", f"/api/v1/credentials/{cred_id}", None)
    assert one.status_code == 403
    gone = await _call(client, "viewer", "DELETE", f"/api/v1/credentials/{cred_id}", None)
    assert gone.status_code == 403
    still = await _call(client, "admin", "GET", f"/api/v1/credentials/{cred_id}", None)
    assert still.status_code == 200


# ── Secrets list: operator or higher ─────────────────────────────────────


@pytest.mark.asyncio
async def test_secret_list_refuses_viewer(client: AsyncClient):
    resp = await _call(client, "viewer", "GET", "/api/v1/secrets", None)
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["operator", "admin"])
async def test_secret_list_allows_operator_and_admin(client: AsyncClient, role):
    # Operators pick an existing key when binding an agent or adding a
    # runtime — they see names and a masked hint, never a value.
    resp = await _call(client, role, "GET", "/api/v1/secrets", None)
    assert resp.status_code == 200, resp.text


# ── Masking: no password characters, no length ──────────────────────────


@pytest.mark.parametrize("password", ["a1b2c", "hunter2", "correct-horse-battery-staple-42"])
def test_password_mask_shows_no_character_and_no_length(password):
    masked = enc.mask_credential_field("password", password)
    assert not any(ch in masked for ch in set(password)), masked
    assert masked == enc.mask_credential_field("password", "x"), "length must not leak"


@pytest.mark.parametrize(
    "field", ["password", "passphrase", "pin", "secret", "db_password", "client_secret", "content"]
)
def test_password_like_fields_never_show_a_tail(field):
    value = "S" * 40 + "WXYZ"
    assert "WXYZ" not in enc.mask_credential_field(field, value)


def test_short_values_are_fully_hidden_with_fixed_length():
    assert enc.mask("abcde") == enc.mask("ab") == enc.mask("abcdefghijklmnopqrs")
    assert "e" not in enc.mask("abcde")


def test_long_token_shows_last_four_with_fixed_prefix():
    token = "ghp_" + "x" * 30 + "9f3a"
    other = "ghp_" + "x" * 60 + "9f3a"
    assert enc.mask(token).endswith("9f3a")
    assert enc.mask(token) == enc.mask(other), "length must not leak"
    assert "ghp_" not in enc.mask(token)


def test_token_field_uses_the_long_token_rule():
    assert enc.mask_credential_field("token", "ghp_" + "x" * 30 + "9f3a").endswith("9f3a")
    assert enc.mask_credential_field("token", "short1") == enc.mask("short1")


@pytest.mark.asyncio
async def test_login_response_hides_the_whole_password(auth_client: AsyncClient):
    resp = await auth_client.post(
        "/api/v1/credentials",
        json={
            "name": "Five",
            "credential_type": "login",
            "data": {"username": "mark", "password": "a1b2c"},
            "url": "https://example.com/login",
        },
    )
    assert resp.status_code == 201
    masked = resp.json()["data_masked"]
    assert masked["username"] == "mark"
    assert not any(ch in masked["password"] for ch in "a1b2c"), masked

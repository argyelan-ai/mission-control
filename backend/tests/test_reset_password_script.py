"""`make reset-password` — the way back in when the admin password is lost.

The script runs inside the backend container (`docker compose exec -u mcuser
backend python -m scripts.reset_password`). It must:

- set a new password the login endpoint accepts;
- sign out every existing session of that user (token_version bump), since a
  reset is what you do when a login may be in the wrong hands;
- pick the only admin by default, and refuse to guess when there are several;
- never take the password from argv (shell history, `ps`).
"""

import io
import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import create_access_token, hash_password, verify_password

OLD_PW = "correct-horse"
NEW_PW = "battery-staple"


async def _make_user(role: str = "admin", email: str | None = None):
    from app.models.user import User
    from tests.conftest import test_engine

    user = User(
        id=uuid.uuid4(),
        email=email or f"reset-{uuid.uuid4().hex[:8]}@mc.local",
        name="Reset Tester",
        password_hash=hash_password(OLD_PW),
        role=role,
        is_active=True,
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(user)
        await s.commit()
    return user


async def _reload(user_id):
    from app.models.user import User
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await s.get(User, user_id)


@pytest.mark.asyncio
async def test_reset_sets_password_and_signs_out_all_sessions(client):
    from scripts.reset_password import reset_password
    from tests.conftest import test_engine

    admin = await _make_user()
    old_token = create_access_token(str(admin.id), admin.role, admin.token_version)
    client.cookies.clear()
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old_token}"})
    assert me.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        changed = await reset_password(s, email=None, new_password=NEW_PW)
    assert changed.email == admin.email

    fresh = await _reload(admin.id)
    assert verify_password(NEW_PW, fresh.password_hash)

    # Every old session is gone ...
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old_token}"})
    assert me.status_code == 401
    # ... and the new password logs in.
    login = await client.post(
        "/api/v1/auth/login", json={"email": admin.email, "password": NEW_PW}
    )
    assert login.status_code == 200, login.text


@pytest.mark.asyncio
async def test_several_admins_need_an_explicit_email():
    from scripts.reset_password import ResetError, reset_password
    from tests.conftest import test_engine

    a = await _make_user()
    b = await _make_user()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with pytest.raises(ResetError) as err:
            await reset_password(s, email=None, new_password=NEW_PW)
    # The message lists the candidates so the operator can pick one.
    assert a.email in str(err.value) and b.email in str(err.value)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await reset_password(s, email=b.email, new_password=NEW_PW)
    assert verify_password(OLD_PW, (await _reload(a.id)).password_hash)
    assert verify_password(NEW_PW, (await _reload(b.id)).password_hash)


@pytest.mark.asyncio
async def test_unknown_email_and_short_password_change_nothing():
    from scripts.reset_password import ResetError, reset_password
    from tests.conftest import test_engine

    admin = await _make_user()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with pytest.raises(ResetError):
            await reset_password(s, email="nobody@mc.local", new_password=NEW_PW)
        with pytest.raises(ResetError):
            await reset_password(s, email=admin.email, new_password="12345")
    fresh = await _reload(admin.id)
    assert verify_password(OLD_PW, fresh.password_hash)
    assert fresh.token_version == admin.token_version


def test_password_is_never_an_argv_option():
    from scripts.reset_password import build_parser

    opts = {a for action in build_parser()._actions for a in action.option_strings}
    assert "--password" not in opts
    assert "--password-stdin" in opts


def test_read_password_from_stdin_strips_newline():
    from scripts.reset_password import read_new_password

    assert read_new_password(from_stdin=True, stdin=io.StringIO(NEW_PW + "\n")) == NEW_PW

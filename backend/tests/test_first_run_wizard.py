"""First-run wizard state (services/first_run.py).

Finding (17.10.2026, fresh install on a headless board): after registering
the first admin the operator landed on Home ("No board selected") instead of
the setup wizard. The only way into /setup was the one redirect right after
the register request; any later way in (reload, second device, logging in
again) went to Home. The frontend now asks the backend whether the wizard is
still pending — these tests pin that contract.
"""
from __future__ import annotations

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.app_setting import AppSetting
from app.services import first_run
from tests.conftest import test_engine

REGISTER = {"email": "first@example.com", "name": "First", "password": "first-run-pw-123"}


async def _register(client) -> str:
    r = await client.post("/api/v1/auth/register", json=REGISTER)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_fresh_registration_marks_wizard_pending(client):
    token = await _register(client)
    r = await client.get(
        "/api/v1/auth/first-run", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"wizard_pending": True}


@pytest.mark.asyncio
async def test_login_after_registration_still_sees_pending(client):
    """The measured case: register, close the tab, log in again later."""
    await _register(client)
    r = await client.post(
        "/api/v1/auth/login",
        json={"email": REGISTER["email"], "password": REGISTER["password"]},
    )
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    r = await client.get(
        "/api/v1/auth/first-run", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.json() == {"wizard_pending": True}


@pytest.mark.asyncio
async def test_complete_clears_pending_for_good(client):
    token = await _register(client)
    headers = {"Authorization": f"Bearer {token}"}
    r = await client.post("/api/v1/auth/first-run/complete", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"wizard_pending": False}
    r = await client.get("/api/v1/auth/first-run", headers=headers)
    assert r.json() == {"wizard_pending": False}


@pytest.mark.asyncio
async def test_existing_install_without_the_key_is_never_pending(auth_client):
    """Installs older than this key set up long ago — no wizard for them."""
    r = await auth_client.get("/api/v1/auth/first-run")
    assert r.status_code == 200, r.text
    assert r.json() == {"wizard_pending": False}


@pytest.mark.asyncio
async def test_first_run_requires_login(client):
    r = await client.get("/api/v1/auth/first-run")
    assert r.status_code == 401
    r = await client.post("/api/v1/auth/first-run/complete")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_complete_requires_admin(client):
    """A viewer must not be able to switch the operator's wizard off."""
    import uuid

    from app.auth import create_access_token
    from app.models.user import User

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await first_run.set_state(s, first_run.PENDING)
        viewer = User(id=uuid.uuid4(), email="v@example.com", name="V", role="viewer", is_active=True)
        s.add(viewer)
        await s.commit()
    token = create_access_token(str(viewer.id), "viewer")
    r = await client.post(
        "/api/v1/auth/first-run/complete", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 403
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert await first_run.wizard_pending(s) is True
    # ...and is never sent into it: the wizard writes provider keys.
    r = await client.get(
        "/api/v1/auth/first-run", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.json() == {"wizard_pending": False}


@pytest.mark.asyncio
async def test_first_run_key_does_not_warn_as_unknown_channel_setting(caplog):
    """app_settings is shared; channel_config warns on keys nobody owns."""
    from app.services import channel_config

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(AppSetting(key=first_run.FIRST_RUN_KEY, value=first_run.PENDING))
        await s.commit()
        with caplog.at_level("WARNING"):
            out = await channel_config.stored_overrides(s)
    assert first_run.FIRST_RUN_KEY not in out
    assert "first_run_wizard" not in caplog.text

"""Security regression tests for the agent-scoped credential endpoints
(routers/agent_scoped.py) — Phase 2 review finding #1 (30.08.2026, CRITICAL).

GET /agent/boards/{board_id}/credentials/{credential_id} returns credential
data fully decrypted BY DESIGN for login/token/custom types (an agent with
credentials:read is meant to get a real, usable secret). That is not the
same guarantee as "an ssh_key's private_key_pem never leaves the backend" —
a Vault-backed host SSH key (Fleet & Rezepte v2, Phase 2 Auto-Onboarding) is
root-level access to the fleet, not a website login. Before this fix, ANY
agent with credentials:read could pull it in cleartext.
"""
import json
import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board
from app.models.credential import Credential
from app.services.encryption import encrypt
from tests.conftest import test_engine


async def _setup_agent_with_credentials_read() -> dict:
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="CredTest", slug=f"ct-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="CredAgent", role="tester",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "credentials:read"],
            provision_status="provisioned",
        ))
        await s.commit()
    return {"board_id": board_id, "agent_id": agent_id, "token": token_raw}


async def _make_ssh_key_credential() -> uuid.UUID:
    cred_id = uuid.uuid4()
    # Deliberately NOT a real PEM header — gitleaks' built-in private-key
    # rule matches on the real key-type label ("OPENSSH PRIVATE KEY")
    # regardless of the fake content between markers, and
    # test_oss_scrub_hygiene.py scans this very file.
    encrypted = encrypt(json.dumps({
        "private_key_pem": "-----BEGIN KEY-----\nsecretkeymaterial\n-----END KEY-----\n",
        "public_key": "ssh-ed25519 AAAAC3Nz mc-fleet gx10",
        "username": "mcfleet",
    }))
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Credential(id=cred_id, name="GX10 SSH key", credential_type="ssh_key", encrypted_data=encrypted))
        await s.commit()
    return cred_id


@pytest.mark.asyncio
async def test_agent_get_credential_hides_private_key_for_ssh_key_type(client, fake_redis):
    data = await _setup_agent_with_credentials_read()
    cred_id = await _make_ssh_key_credential()

    resp = await client.get(
        f"/api/v1/agent/boards/{data['board_id']}/credentials/{cred_id}",
        headers={"Authorization": f"Bearer {data['token']}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["private_key_pem"] == "[hidden]"
    assert "secretkeymaterial" not in resp.text
    # Not secrets — an agent legitimately might want to know these.
    assert body["data"]["public_key"] == "ssh-ed25519 AAAAC3Nz mc-fleet gx10"
    assert body["data"]["username"] == "mcfleet"


@pytest.mark.asyncio
async def test_agent_get_credential_still_returns_real_login_secret(client, fake_redis):
    """The fix must not break the endpoint's actual purpose: a login
    credential's real password IS supposed to come back unmasked here."""
    data = await _setup_agent_with_credentials_read()
    cred_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Credential(
            id=cred_id, name="Some Login", credential_type="login",
            encrypted_data=encrypt(json.dumps({"username": "mark", "password": "s3cret-real-pw"})),
            url="http://caddy/login",
        ))
        await s.commit()

    resp = await client.get(
        f"/api/v1/agent/boards/{data['board_id']}/credentials/{cred_id}",
        headers={"Authorization": f"Bearer {data['token']}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["password"] == "s3cret-real-pw"


@pytest.mark.asyncio
async def test_agent_list_credentials_masks_private_key_for_ssh_key_type(client, fake_redis):
    """The list endpoint already used _mask_data — confirm ssh_key rides the
    SAME shared NEVER_EXPOSE_CREDENTIAL_FIELDS constant, not a second list
    that could drift from the single-credential endpoint's."""
    data = await _setup_agent_with_credentials_read()
    await _make_ssh_key_credential()

    resp = await client.get(
        f"/api/v1/agent/boards/{data['board_id']}/credentials",
        headers={"Authorization": f"Bearer {data['token']}"},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()
    ssh_item = next(i for i in items if i["credential_type"] == "ssh_key")
    assert ssh_item["data_masked"]["private_key_pem"] == "[hidden]"
    assert "secretkeymaterial" not in resp.text
